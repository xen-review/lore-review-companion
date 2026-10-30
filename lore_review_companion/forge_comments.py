# SPDX-License-Identifier: GPL-2.0-or-later

"""Idempotent-by-reconciliation Gitea comment delivery helpers."""

from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from lore_review_companion.anchor import Anchor, RangeAnchor
from lore_review_companion.forge_api import ForgeAPI, ForgeAPIError


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


def submission_text(text: str) -> str:
    text = text.replace("\r\n", "\n").rstrip()
    text = re.sub(r"\n-- ?\n[0-9][^\n]*\Z", "", text).rstrip()
    lines = text.splitlines()
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
    return text


def imported_mail_text(
    text: str,
    message_id: str,
    source_url: str,
    body_format: str | None,
    author: str = "",
) -> str:
    if body_format is None:
        return quote_untrusted_text(text)
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


def quoted_parent_matches(
    quoted: tuple[str, ...], parent: str, body_format: str | None
) -> bool:
    if body_format == "mail-text-v2":
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
