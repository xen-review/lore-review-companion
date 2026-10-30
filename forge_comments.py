"""Idempotent-by-reconciliation Gitea comment delivery helpers."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from anchor import Anchor
from forge_api import ForgeAPI, ForgeAPIError


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
    if not isinstance(login, str) or len(login) > 255 or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", login) is None:
        raise ValueError("Authenticated Gitea login cannot be safely used for attribution")
    return f"Review mirror on behalf of @{login}\n\n{body}"


def quote_untrusted_text(text: str) -> str:
    text = text.replace("<!-- lore-gitea-mirror:", "&lt;!-- lore-gitea-mirror:")
    longest = max((len(match.group()) for match in re.finditer(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}text\n{text.rstrip()}\n{fence}"


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
        self, api: ForgeAPI, owner: str, repo: str, *, write_enabled: bool = False,
        allow_inline_writes: bool = False,
    ):
        if not owner or not repo:
            raise ValueError("owner and repository must be non-empty")
        self.api = api
        self.owner = owner
        self.repo = repo
        self.repository_path = (
            f"repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
        )
        self.write_enabled = write_enabled
        self.allow_inline_writes = allow_inline_writes

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
            return DeliveryResult({"path": path, **payload}, created=False, planned=True)

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
        raise DeliveryUncertainError("Gitea accepted the comment but it is not yet visible")

    def ensure_inline_comment(
        self,
        pull_index: int,
        anchor: Anchor,
        *,
        source_message_id: str,
        effect_key: str,
        body: str,
        head_commit: str,
    ) -> DeliveryResult:
        if (
            not isinstance(anchor.path, str) or not anchor.path
            or not isinstance(anchor.old_position, int) or isinstance(anchor.old_position, bool)
            or not isinstance(anchor.new_position, int) or isinstance(anchor.new_position, bool)
            or (anchor.old_position > 0) == (anchor.new_position > 0)
            or anchor.old_position < 0 or anchor.new_position < 0
        ):
            raise ValueError("inline anchor must identify exactly one positive diff position")
        if not isinstance(head_commit, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", head_commit):
            raise ValueError("inline comment requires the full reviewed head commit")
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
        if not self.write_enabled:
            return DeliveryResult({"path": f"{path}/reviews", **payload}, created=False, planned=True)
        if not self.allow_inline_writes:
            raise InlineWritesDisabledError("Inline writes require an isolated account and explicit opt-in")
        for review in self.api.get_pages(f"{path}/reviews"):
            if str(review.get("state", "")).upper() == "PENDING":
                raise InlineWritesDisabledError("A pending review already exists on this pull request")

        try:
            review = self.api.request_json(
                f"{path}/reviews", method="POST", payload=payload
            )
        except ForgeAPIError as error:
            existing, matching_review = self._find_inline_comment(path, marker)
            if existing:
                return DeliveryResult(existing, created=False)
            if matching_review or error.status is None or error.status >= 500 or 200 <= error.status < 300:
                raise DeliveryUncertainError(
                    "Gitea may have accepted the review; retry only after reconciliation"
                ) from error
            raise

        existing, _ = self._find_inline_comment(path, marker)
        if existing:
            return DeliveryResult(existing, created=True)
        raise DeliveryUncertainError("Gitea accepted the review but its inline comment is not visible")

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
        if not isinstance(pull_index, int) or isinstance(pull_index, bool) or pull_index < 1:
            raise ValueError("pull request index must be a positive integer")
        return pull_index