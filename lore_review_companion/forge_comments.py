# SPDX-License-Identifier: GPL-2.0-or-later

"""Idempotent-by-reconciliation Gitea comment delivery helpers."""

from __future__ import annotations

import hashlib
import html
import re
from bisect import bisect_right
from dataclasses import dataclass
from email.utils import parseaddr
from typing import Any
from urllib.parse import quote

from lore_review_companion.anchor import Anchor, RangeAnchor
from lore_review_companion.forge_api import ForgeAPI, ForgeAPIError
from lore_review_companion.mail_parser import ParsedMessage, extract_quoted_remarks


class DeliveryUncertainError(RuntimeError):
    """A write may exist remotely but could not be confirmed safely."""


class WritesDisabledError(RuntimeError):
    """A remote write was requested without explicit publisher opt-in."""


class StaleAnchorError(ValueError):
    """The pull request head no longer matches an inline comment's anchor."""


class InlineWritesDisabledError(ValueError):
    """Inline review creation requires an isolated, explicitly enabled account."""


@dataclass(frozen=True)
class DeliveryResult:
    remote_object: dict[str, Any]
    created: bool
    planned: bool = False


def with_tool_attribution(body: str, login: str) -> str:
    if (
        not isinstance(login, str)
        or len(login) > 255
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", login) is None
    ):
        raise ValueError(
            "Authenticated Gitea login cannot be safely used for attribution"
        )
    return f"Review mirror on behalf of @{login}\n\n{body}"


def quote_untrusted_text(text: str) -> str:
    text = text.replace("<!-- lore-gitea-mirror:", "&lt;!-- lore-gitea-mirror:")
    longest = max((len(match.group()) for match in re.finditer(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}text\n{text.rstrip()}\n{fence}"


def submission_text(text: str, *, strip_shortlog: bool = True) -> str:
    text = text.replace("\r\n", "\n").rstrip()
    text = re.sub(r"\n-- ?\n[0-9][^\n]*\Z", "", text).rstrip()
    lines = text.splitlines()
    if strip_shortlog:
        for index in range(len(lines)):
            position = index
            while position < len(lines):
                heading = re.fullmatch(r"\S[^\n]* \(([1-9][0-9]*)\):", lines[position])
                if heading is None:
                    break
                count = int(heading.group(1))
                subjects = lines[position + 1 : position + 1 + count]
                if len(subjects) != count or any(
                    not line.startswith("  ") or not line.strip() for line in subjects
                ):
                    break
                position += count + 1
                while position < len(lines) and not lines[position].strip():
                    position += 1
            diffstat = position
            while position < len(lines) and re.fullmatch(
                r" .+\s+\|\s+(?:\d+\s*[-+]*|Bin .+)", lines[position]
            ):
                position += 1
            if index < diffstat < position < len(lines) and re.fullmatch(
                r"\s*\d+ files? changed(?:, \d+ (?:insertions?\(\+\)|deletions?\(-\)))*\s*",
                lines[position],
            ):
                return (
                    "\n".join(lines[:index]).rstrip().removesuffix("\n\n---").rstrip()
                )
    summary = next(
        (
            index
            for index in range(len(lines) - 1, -1, -1)
            if re.fullmatch(
                r"\s*\d+ files? changed(?:, \d+ (?:insertions?\(\+\)|deletions?\(-\)))*\s*",
                lines[index],
            )
        ),
        None,
    )
    if summary is not None:
        start = summary
        while start > 0 and re.fullmatch(
            r" .+\s+\|\s+(?:\d+\s*[-+]*|Bin .+)", lines[start - 1]
        ):
            start -= 1
        if start < summary and all(
            not line.strip()
            or re.fullmatch(
                r" (?:create|delete) mode .+| (?:rename|copy) .+| mode change .+", line
            )
            for line in lines[summary + 1 :]
        ):
            lines = lines[:start]
            if lines and lines[-1] == "---":
                lines.pop()
            text = "\n".join(lines).rstrip()
            if strip_shortlog:
                for index in range(len(lines)):
                    position = index
                    while position < len(lines):
                        heading = re.fullmatch(
                            r"\S[^\n]* \(([1-9][0-9]*)\):", lines[position]
                        )
                        if heading is None:
                            break
                        count = int(heading.group(1))
                        subjects = lines[position + 1 : position + 1 + count]
                        if len(subjects) != count or any(
                            not line.startswith("  ") or not line.strip()
                            for line in subjects
                        ):
                            break
                        position += count + 1
                        while position < len(lines) and not lines[position].strip():
                            position += 1
                    if position == len(lines) and position > index:
                        text = "\n".join(lines[:index]).rstrip()
                        break
    return text


def submission_description(text: str, submitter_address: str) -> str:
    lines = submission_text(text).splitlines()
    for index, line in enumerate(lines):
        if (
            re.fullmatch(r" .+\s+\|\s+(?:\d+\s*[-+]*|Bin .+)", line)
            or line.startswith(("diff --git ", "--- a/"))
        ):
            lines = lines[:index]
            break
    while lines and (not lines[-1].strip() or lines[-1] == "---"):
        lines.pop()
    signoffs = [index for index, line in enumerate(lines) if re.match(r"(?i)^signed-off-by:\s*", line)]
    if len(signoffs) == 1:
        _, address = parseaddr(lines[signoffs[0]].split(":", 1)[1].strip())
        if address and address.casefold() == submitter_address.casefold():
            del lines[signoffs[0]]
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(
        f"> {line}" if line else ">" for line in lines
    ).replace("<!-- lore-gitea-mirror:", "&lt;!-- lore-gitea-mirror:")


def _is_diff_header(line: str) -> bool:
    """Recognize supported diff file headers."""
    return (
        line.startswith("diff --git ")
        or re.fullmatch(r"diff a/.+ b/.+", line) is not None
    )


def format_submission_quotes(
    text: str,
    parent: str,
    parent_kind: str,
    *,
    quote_patch_code: bool = False,
    diff_quote_as_c: bool = False,
) -> str:
    """Format retained submission quotations with safe Markdown fences."""
    parent_lines = tuple(line.rstrip() for line in parent.splitlines())
    diff_boundary = None
    if parent_kind == "patch":
        diff_boundary = next(
            (index for index, line in enumerate(parent_lines) if _is_diff_header(line)),
            None,
        )
        if diff_boundary is None:
            diff_boundary = next(
                (
                    index
                    for index, line in enumerate(parent_lines[:-1])
                    if line.startswith("--- ")
                    and parent_lines[index + 1].startswith("+++ ")
                ),
                None,
            )
        boundary = len(parent_lines) if quote_patch_code else diff_boundary
        if boundary is None:
            return text
    elif parent_kind == "cover":
        boundary = len(parent_lines)
    else:
        return text

    searchable = "\n" + "\n".join(parent_lines) + "\n"
    offsets = []
    offset = 1
    for line in parent_lines:
        offsets.append(offset)
        offset += len(line) + 1

    lines = text.splitlines(keepends=True)
    output = []

    def append_code_quote(segment: list[str], *, is_diff: bool = False) -> None:
        """Append a fenced quotation with a delimiter longer than its content."""
        longest = max(
            (len(match.group()) for match in re.finditer(r"`+", "".join(segment))),
            default=0,
        )
        fence = "`" * max(3, longest + 1)
        language = "c" if diff_quote_as_c and is_diff else "rust"
        output.append(f"{fence}{language}\n")
        if diff_quote_as_c and is_diff:
            for line in segment:
                if line.startswith(">"):
                    line = line[1:]
                    if line.startswith(" "):
                        line = line[1:]
                output.append(line)
        else:
            output.extend(segment)
        if segment and not segment[-1].endswith(("\n", "\r")):
            output.append("\n")
        output.append(f"{fence}\n")

    def patch_like(quoted: list[str]) -> bool:
        """Identify quoted lines that resemble a patch."""
        return (
            any(
                _is_diff_header(line)
                or line.startswith(("index ", "--- ", "+++ ", "@@"))
                for line in quoted
            )
            or sum(line.startswith(("+", "-")) for line in quoted) >= 2
        )

    position = 0
    while position < len(lines):
        if not lines[position].startswith(">"):
            output.append(lines[position])
            position += 1
            continue
        end = position
        while end < len(lines) and lines[end].startswith(">"):
            end += 1
        block = lines[position:end]
        quoted = []
        for line in block:
            value = line[1:]
            if value.startswith(" "):
                value = value[1:]
            quoted.append(value.rstrip())
        if not any(quoted):
            output.extend(block)
            position = end
            continue
        needle = "\n" + "\n".join(quoted) + "\n"
        matched_boundary = None
        ambiguous = False
        match_starts = []
        cursor = searchable.find(needle)
        while cursor >= 0:
            start = bisect_right(offsets, cursor + 1) - 1
            candidate = min(max(boundary - start, 0), len(block))
            if matched_boundary is not None and candidate != matched_boundary:
                ambiguous = True
                break
            matched_boundary = candidate
            match_starts.append(start)
            cursor = searchable.find(needle, cursor + 1)
        if matched_boundary is None or ambiguous:
            if quote_patch_code and patch_like(quoted):
                append_code_quote(block, is_diff=diff_quote_as_c)
            else:
                output.extend(block)
            position = end
            continue
        matched_diff = (
            diff_quote_as_c
            and diff_boundary is not None
            and bool(match_starts)
            and all(start >= diff_boundary for start in match_starts)
        )
        split = 0
        while split < len(block):
            next_split = split + 1
            while next_split < len(block) and (next_split < matched_boundary) == (
                split < matched_boundary
            ):
                next_split += 1
            segment = block[split:next_split]
            if split < matched_boundary and any(quoted[split:next_split]):
                append_code_quote(
                    segment,
                    is_diff=matched_diff or patch_like(quoted[split:next_split]),
                )
            else:
                output.extend(segment)
            split = next_split
        position = end
    return "".join(output)


def format_direct_submission_reply(
    message: ParsedMessage,
    members: tuple[ParsedMessage, ...],
    *,
    quote_patch_code: bool = False,
    diff_quote_as_c: bool = False,
) -> str:
    """Format quotes only when the retained parent is a submission."""
    parents = message.in_reply_to or message.references[-1:]
    if len(parents) != 1:
        return message.body_text
    parent = next(
        (member for member in members if member.message_id == parents[0]), None
    )
    if parent is None or parent.kind not in ("patch", "cover"):
        return message.body_text
    return format_submission_quotes(
        message.body_text,
        parent.body_text,
        parent.kind,
        quote_patch_code=quote_patch_code,
        diff_quote_as_c=diff_quote_as_c,
    )


def imported_mail_text(
    text: str,
    message_id: str,
    source_url: str,
    body_format: str | None,
    author: str = "",
) -> str:
    """Render imported mail according to its versioned presentation format."""
    if body_format is None:
        return quote_untrusted_text(text)
    if body_format in ("mail-text-v4", "mail-text-v5", "mail-text-v6"):
        text = text.rstrip()
        before, separator, last = text.rpartition("\n")
        signature = last.strip()
        if signature and not signature[0].isalnum() and signature[0] not in "><`":
            signature = signature[1:].strip()
        words = author.casefold().split()
        name_parts = {
            " ".join(words[start:end])
            for start in range(len(words))
            for end in range(start + 1, len(words) + 1)
        }
        if separator and before.strip() and signature.casefold() in name_parts:
            return before.rstrip()
        return text
    if body_format == "mail-text-v3":
        return text.rstrip()
    if body_format == "mail-text-v2":
        url = html.escape(
            f"{source_url.rstrip('/')}/{quote(message_id, safe='@')}/", quote=True
        )
        label = html.escape(
            f"Original review comment by {author or 'the sender'}", quote=True
        )
        link = f'<small><a href="{url}" rel="nofollow noreferrer">{label}</a></small>'
        return f"{text.rstrip()}\n\n---\n\n{link}" if text.strip() else link
    if body_format != "mail-text-v1":
        raise ValueError("Unsupported imported message body format")
    label = "lore-" + hashlib.sha256(message_id.encode()).hexdigest()[:16]
    while f"[^{label}]" in text:
        label += "-source"
    footnote = f"[^{label}]\n\n[^{label}]: <{source_url.rstrip('/')}/{quote(message_id, safe='@')}/>"
    return f"{text.rstrip()}\n\n{footnote}" if text.strip() else footnote


def reply_prose(text: str) -> str:
    return "\n".join(
        line
        for line in text.splitlines()
        if not line.startswith(">") and not re.fullmatch(r"On .+ wrote:\s*", line)
    ).strip()


def format_nested_reply(text: str) -> str:
    """Preserve immediate-parent quotations alongside nested reply prose."""
    remarks = {
        remark.quote_start: remark
        for remark in extract_quoted_remarks(text)
        if remark.quote_depth > 0
    }
    output = []
    for number, line in enumerate(text.splitlines(), start=1):
        remark = remarks.get(number)
        if remark is not None:
            quote = list(remark.quote)
            while quote and not quote[0].strip():
                quote.pop(0)
            while quote and not quote[-1].strip():
                quote.pop()
            if quote:
                longest = max(
                    (
                        len(match.group())
                        for match in re.finditer(r"`+", "\n".join(quote))
                    ),
                    default=0,
                )
                fence = "`" * max(3, longest + 1)
                output.append(f"{fence}rust")
                output.extend(f"> {quoted}" if quoted else ">" for quoted in quote)
                output.append(fence)
        if line.startswith(">") or re.fullmatch(r"On .+ wrote:\s*", line):
            continue
        output.append(line)
    return "\n".join(output).strip()


def quoted_parent_matches(
    quoted: tuple[str, ...],
    parent: str,
    body_format: str | None,
    *,
    allow_excerpt: bool = False,
) -> bool:
    """Check whether quoted text matches the retained parent presentation."""
    if (
        body_format
        in (
            "mail-text-v2",
            "mail-text-v3",
            "mail-text-v4",
            "mail-text-v5",
            "mail-text-v6",
        )
        and allow_excerpt
    ):
        quote_lines = tuple(line.strip() for line in quoted if line.strip())
        parent_lines = tuple(
            line.strip() for line in parent.splitlines() if line.strip()
        )
        return bool(quote_lines) and any(
            parent_lines[position : position + len(quote_lines)] == quote_lines
            for position in range(len(parent_lines) - len(quote_lines) + 1)
        )
    if body_format in (
        "mail-text-v2",
        "mail-text-v3",
        "mail-text-v4",
        "mail-text-v5",
        "mail-text-v6",
    ):
        return tuple(line for line in quoted if line.strip()) == tuple(
            line for line in parent.splitlines() if line.strip()
        )
    return "\n".join(quoted) == parent


def _marker(source_message_id: str, effect_key: str) -> str:
    if not source_message_id.strip() or not effect_key.strip():
        raise ValueError("source message ID and effect key must be non-empty")
    identity = f"{source_message_id.strip()}\0{effect_key.strip()}".encode()
    digest = hashlib.sha256(identity).hexdigest()
    return f"<!-- lore-gitea-mirror:v1:{digest} -->"


def _with_marker(body: str, marker: str) -> str:
    body = body.rstrip()
    return f"{body}\n\n{marker}" if body else marker


class ForgeCommentPublisher:
    def __init__(
        self,
        api: ForgeAPI,
        owner: str,
        repo: str,
        *,
        write_enabled: bool = False,
        allow_inline_writes: bool = False,
        max_inline_lines: int = 1,
    ):
        if not owner or not repo:
            raise ValueError("owner and repository must be non-empty")
        if (
            not isinstance(max_inline_lines, int)
            or isinstance(max_inline_lines, bool)
            or max_inline_lines < 1
        ):
            raise ValueError("The configured inline line limit must be positive")
        self.api = api
        self.owner = owner
        self.repo = repo
        self.repository_path = f"repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
        self.write_enabled = write_enabled
        self.allow_inline_writes = allow_inline_writes
        self.max_inline_lines = max_inline_lines

    def ensure_issue_comment(
        self,
        pull_index: int,
        *,
        source_message_id: str,
        effect_key: str,
        body: str,
    ) -> DeliveryResult:
        marker = _marker(source_message_id, effect_key)
        path = f"{self.repository_path}/issues/{self._pull_index(pull_index)}/comments"
        existing = self._find_issue_comment(path, marker)
        if existing:
            return DeliveryResult(existing, created=False)

        payload = {"body": _with_marker(body, marker)}
        if not self.write_enabled:
            return DeliveryResult(
                {"path": path, **payload}, created=False, planned=True
            )

        try:
            created = self.api.request_json(
                path,
                method="POST",
                payload=payload,
            )
        except ForgeAPIError as error:
            existing = self._find_issue_comment(path, marker)
            if existing:
                return DeliveryResult(existing, created=False)
            if error.status is None or error.status >= 500 or 200 <= error.status < 300:
                raise DeliveryUncertainError(
                    "Gitea may have accepted the comment; retry only after reconciliation"
                ) from error
            raise

        if isinstance(created, dict) and marker in str(created.get("body", "")):
            return DeliveryResult(created, created=True)
        existing = self._find_issue_comment(path, marker)
        if existing:
            return DeliveryResult(existing, created=True)
        raise DeliveryUncertainError(
            "Gitea accepted the comment but it is not yet visible"
        )

    def ensure_inline_comment(
        self,
        pull_index: int,
        anchor: Anchor,
        *,
        source_message_id: str,
        effect_key: str,
        body: str,
        head_commit: str,
        base_commit: str | None = None,
        merge_base: str | None = None,
    ) -> DeliveryResult:
        if (
            not isinstance(anchor.path, str)
            or not anchor.path
            or not isinstance(anchor.old_position, int)
            or isinstance(anchor.old_position, bool)
            or not isinstance(anchor.new_position, int)
            or isinstance(anchor.new_position, bool)
            or (anchor.old_position > 0) == (anchor.new_position > 0)
            or anchor.old_position < 0
            or anchor.new_position < 0
        ):
            raise ValueError(
                "inline anchor must identify exactly one positive diff position"
            )
        if not isinstance(head_commit, str) or not re.fullmatch(
            r"[0-9a-f]{40}|[0-9a-f]{64}", head_commit
        ):
            raise ValueError("inline comment requires the full reviewed head commit")
        if isinstance(anchor, RangeAnchor) and (
            not isinstance(anchor.extra_lines_count, int)
            or isinstance(anchor.extra_lines_count, bool)
            or not 0 <= anchor.extra_lines_count < self.max_inline_lines
        ):
            raise ValueError("inline range exceeds the configured line limit")
        if (base_commit is None) != (merge_base is None) or any(
            not isinstance(commit, str)
            or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit)
            for commit in (base_commit, merge_base)
            if commit is not None
        ):
            raise ValueError(
                "inline comparison requires full base and merge-base commits"
            )
        marker = _marker(source_message_id, effect_key)
        path = f"{self.repository_path}/pulls/{self._pull_index(pull_index)}"
        existing, matching_review = self._find_inline_comment(path, marker)
        if existing:
            return DeliveryResult(existing, created=False)
        if matching_review:
            raise DeliveryUncertainError(
                "A provenance review exists but its inline comment cannot be confirmed"
            )
        pull = self.api.request_json(path)
        head = pull.get("head") if isinstance(pull, dict) else None
        if not isinstance(head, dict) or head.get("sha") != head_commit:
            raise StaleAnchorError("Pull request head differs from the reviewed commit")
        if base_commit is not None:
            base = pull.get("base")
            if (
                not isinstance(base, dict)
                or base.get("sha") != base_commit
                or pull.get("merge_base") != merge_base
            ):
                raise StaleAnchorError(
                    "Pull request base or merge base differs from the reviewed comparison"
                )

        payload = {
            "event": "COMMENT",
            "body": marker,
            "commit_id": head_commit,
            "comments": [
                {
                    "path": anchor.path,
                    "body": _with_marker(body, marker),
                    "old_position": anchor.old_position,
                    "new_position": anchor.new_position,
                }
            ],
        }
        if isinstance(anchor, RangeAnchor):
            payload["comments"][0]["extra_lines_count"] = anchor.extra_lines_count
        if not self.write_enabled:
            return DeliveryResult(
                {"path": f"{path}/reviews", **payload}, created=False, planned=True
            )
        if not self.allow_inline_writes:
            raise InlineWritesDisabledError(
                "Inline writes require an isolated account and explicit opt-in"
            )
        for review in self.api.get_pages(f"{path}/reviews"):
            if str(review.get("state", "")).upper() == "PENDING":
                raise InlineWritesDisabledError(
                    "A pending review already exists on this pull request"
                )

        try:
            review = self.api.request_json(
                f"{path}/reviews", method="POST", payload=payload
            )
        except ForgeAPIError as error:
            existing, matching_review = self._find_inline_comment(path, marker)
            if existing:
                return DeliveryResult(existing, created=False)
            if (
                matching_review
                or error.status is None
                or error.status >= 500
                or 200 <= error.status < 300
            ):
                raise DeliveryUncertainError(
                    "Gitea may have accepted the review; retry only after reconciliation"
                ) from error
            raise

        existing, _ = self._find_inline_comment(path, marker)
        if existing:
            return DeliveryResult(existing, created=True)
        raise DeliveryUncertainError(
            "Gitea accepted the review but its inline comment is not visible"
        )

    def _find_issue_comment(self, path: str, marker: str) -> dict[str, Any] | None:
        for comment in self.api.get_pages(path):
            if marker in str(comment.get("body", "")):
                return comment
        return None

    def _find_inline_comment(
        self, pull_path: str, marker: str
    ) -> tuple[dict[str, Any] | None, bool]:
        matching_review = False
        reviews_path = f"{pull_path}/reviews"
        for review in self.api.get_pages(reviews_path):
            review_id = review.get("id")
            review_has_marker = marker in str(review.get("body", ""))
            matching_review = matching_review or review_has_marker
            if not isinstance(review_id, int):
                continue
            comments_path = f"{reviews_path}/{review_id}/comments"
            for comment in self.api.get_pages(comments_path):
                if marker in str(comment.get("body", "")):
                    if str(review.get("state", "")).upper() == "COMMENT":
                        return comment, matching_review
                    matching_review = True
        return None, matching_review

    @staticmethod
    def _pull_index(pull_index: int) -> int:
        if (
            not isinstance(pull_index, int)
            or isinstance(pull_index, bool)
            or pull_index < 1
        ):
            raise ValueError("pull request index must be a positive integer")
        return pull_index
