"""Queue threaded mail replies and exact inline matches for a recorded PR."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import quote, urlsplit

from anchor import DiffIndex
from b4_adapter import B4ReviewError, extract_quoted_comments, read_local_review_drafts
from forge_api import ForgeAPIError
from forge_comments import ForgeCommentPublisher, StaleAnchorError, quote_untrusted_text
from ledger import Ledger
from mail_parser import ParsedMessage
from outbox import comment_destination
from series import discover_series
from threads import ThreadMatch, match_thread_replies


@dataclass(frozen=True)
class ReplyQueueSummary:
    conversation_id: int
    inline_count: int
    unresolved_count: int
    diagnostic: str | None = None


@dataclass(frozen=True)
class ReplyBatchSummary:
    matched: int
    unmatched: tuple[str, ...]
    unkeyed: int
    inline_count: int
    unresolved_count: int
    diagnostics: tuple[str, ...]
    has_more: bool = False
    next_cursor: str | None = None


@dataclass(frozen=True)
class DraftQueueSummary:
    tracking_commit: str
    queued: int
    inline_count: int
    unresolved_count: int
    diagnostic: str | None


def _stable_pull_diff(publisher: ForgeCommentPublisher, pull_index: int, expected_head: str) -> str:
    path = f"{publisher.repository_path}/pulls/{pull_index}"

    def snapshot() -> tuple[str, str, str]:
        pull = publisher.api.request_json(path)
        head = pull.get("head") if isinstance(pull, dict) else None
        base = pull.get("base") if isinstance(pull, dict) else None
        merge_base = pull.get("merge_base") if isinstance(pull, dict) else None
        if (
            not isinstance(head, dict) or not isinstance(head.get("sha"), str)
            or not isinstance(base, dict) or not isinstance(base.get("sha"), str)
            or not isinstance(merge_base, str) or not merge_base
        ):
            raise StaleAnchorError("PR head or merge base is not available for inline anchoring")
        if head["sha"] != expected_head:
            raise StaleAnchorError("PR head changed from the reviewed commit")
        return head["sha"], base["sha"], merge_base

    before = snapshot()
    diff = publisher.api.request_bytes(f"{path}.diff")
    if snapshot() != before:
        raise StaleAnchorError("PR changed while its diff was being read")
    try:
        return diff.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("PR diff is not valid UTF-8") from error


def _validate_inbox_url(inbox_url: str) -> None:
    inbox = urlsplit(inbox_url)
    if (
        inbox.scheme != "https" or not inbox.hostname or inbox.username is not None
        or inbox.password is not None or inbox.query or inbox.fragment
    ):
        raise ValueError("Inbox URL must be a credential-free HTTPS URL")


def _source_time_provenance(message: ParsedMessage) -> str:
    estimate = message.time_estimate
    if estimate is None:
        return "\n\nSource time: unavailable"
    header = "Received" if estimate.source == "received" else "Date"
    return f"\n\nSource time (unverified {header} header): {estimate.timestamp}"


def queue_thread_reply(
    ledger: Ledger,
    publisher: ForgeCommentPublisher,
    pull_index: int,
    match: ThreadMatch,
    *,
    b4_python: str,
    inbox_url: str = "https://lore.kernel.org/xen-devel/",
) -> ReplyQueueSummary:
    """Retain the complete reply and queue only uniquely resolved inline excerpts."""
    with ledger.worker_lock():
        return _queue_thread_reply(
            ledger, publisher, pull_index, match, b4_python=b4_python, inbox_url=inbox_url,
        )


def _queue_thread_reply(
    ledger: Ledger, publisher: ForgeCommentPublisher, pull_index: int, match: ThreadMatch, *,
    b4_python: str, inbox_url: str,
) -> ReplyQueueSummary:
    message = match.message
    if message.kind != "reply" or not message.message_id:
        raise ValueError("A reply with a valid Message-ID is required")
    _validate_inbox_url(inbox_url)
    repository_destination = publisher.api.base_url + publisher.repository_path
    publication = ledger.publication(repository_destination, match.series_key, match.revision)
    if not publication or publication.status != "complete" or publication.pull_index != pull_index:
        raise ValueError("Reply does not match a recorded publication for this pull request")

    ledger.record_parsed_message(message)
    recorded = ledger.series_revision(match.series_key, match.revision)
    if not recorded:
        raise ValueError("Reply does not match a recorded series revision")
    ledger.assert_unconflicted((
        message.message_id, *(member.message_id for member in recorded.messages if member.message_id),
    ))
    ledger.assert_publication_sources(repository_destination, pull_index)
    ledger.assert_message_ancestry_available((message,))
    member_ids = tuple(member.message_id for member in recorded.messages if member.message_id)
    related = ledger._related_thread_messages(member_ids)
    current_matches = match_thread_replies((*recorded.messages, *related), (recorded,))
    if match not in current_matches.matched:
        raise ValueError("Reply thread ancestry changed; inspect before queueing")
    return _queue_verified_thread_reply(
        ledger, publisher, pull_index, match, publication.head_commit, b4_python=b4_python, inbox_url=inbox_url,
    )


def _queue_verified_thread_reply(
    ledger: Ledger, publisher: ForgeCommentPublisher, pull_index: int, match: ThreadMatch, head_commit: str, *,
    b4_python: str, inbox_url: str,
) -> ReplyQueueSummary:
    message = match.message
    message_id = message.message_id
    if not message_id:
        raise ValueError("A reply with a valid Message-ID is required")
    destination = comment_destination(publisher, pull_index)
    thread = {"series_key": match.series_key, "revision": match.revision, "patch_message_id": match.patch_message_id}
    source_url = f"{inbox_url.rstrip('/')}/{quote(message_id, safe='@')}/"
    sender = " ".join(part for part in (message.from_name, message.from_address) if part) or "unknown"
    prefix = f"Source: {source_url}\n\nFrom (unverified):\n\n{quote_untrusted_text(sender)}"
    try:
        mapping = ledger.identity_mapping(publisher.api.base_url, message.from_address) if message.from_address else None
    except ValueError:
        mapping = None
    if mapping:
        try:
            account = publisher.api.request_json(f"users/{quote(mapping.login, safe='')}")
        except (ForgeAPIError, ValueError):
            account = None
        if (
            isinstance(account, dict) and isinstance(account.get("id"), int)
            and not isinstance(account.get("id"), bool) and account["id"] == mapping.user_id
            and isinstance(account.get("login"), str) and account["login"].casefold() == mapping.login.casefold()
        ):
            prefix += f"\n\nOperator-mapped Gitea account (unverified sender):\n\n{quote_untrusted_text(mapping.login)}"
    legacy_prefix = prefix
    prefix += _source_time_provenance(message)
    body = f"{prefix}\n\n{quote_untrusted_text(message.body_text)}"
    conversation_id = ledger.enqueue(
        message_id, "ISSUE_COMMENT", destination, "full-message", {"body": body, "thread": thread},
        compatible_payload={"body": f"{legacy_prefix}\n\n{quote_untrusted_text(message.body_text)}", "thread": thread},
    )
    if match.patch_message_id is None:
        return ReplyQueueSummary(conversation_id, 0, 0)

    try:
        comments = extract_quoted_comments(message.body_text, python_executable=b4_python)
    except B4ReviewError as error:
        return ReplyQueueSummary(conversation_id, 0, 0, str(error))
    if not comments:
        return ReplyQueueSummary(conversation_id, 0, 0)
    try:
        index = DiffIndex(_stable_pull_diff(publisher, pull_index, head_commit))
    except (ForgeAPIError, StaleAnchorError, ValueError) as error:
        return ReplyQueueSummary(conversation_id, 0, len(comments), str(error))

    inline_count = unresolved = 0
    for position, comment in enumerate(comments):
        anchor = index.resolve(asdict(comment))
        if anchor is None or not comment.text.strip():
            unresolved += 1
            continue
        identity = json.dumps(asdict(comment), sort_keys=True, separators=(",", ":")).encode("utf-8")
        effect_key = f"inline:{position}:{hashlib.sha256(identity).hexdigest()}"
        inline_body = f"{prefix}\n\n{quote_untrusted_text(comment.text)}"
        ledger.enqueue(
            message_id, "INLINE_COMMENT", destination, effect_key,
            {"body": inline_body, "head_commit": head_commit, "anchor": asdict(anchor), "thread": thread},
            compatible_payload={"body": f"{legacy_prefix}\n\n{quote_untrusted_text(comment.text)}", "head_commit": head_commit,
                                "anchor": asdict(anchor), "thread": thread},
        )
        inline_count += 1
    return ReplyQueueSummary(conversation_id, inline_count, unresolved)


def queue_thread_replies(
    ledger: Ledger, publisher: ForgeCommentPublisher, pull_index: int,
    series_key: str, revision: int, messages: Iterable[ParsedMessage], *,
    b4_python: str, inbox_url: str = "https://lore.kernel.org/xen-devel/",
) -> ReplyBatchSummary:
    """Queue matching replies against the previously published series membership."""
    with ledger.worker_lock():
        return _queue_thread_replies(
            ledger, publisher, pull_index, series_key, revision, messages, b4_python=b4_python, inbox_url=inbox_url,
        )


def queue_archive_replies(
    ledger: Ledger, publisher: ForgeCommentPublisher, pull_index: int,
    series_key: str, revision: int, *, b4_python: str,
    inbox_url: str = "https://lore.kernel.org/xen-devel/", limit: int = 1000, cursor: str | None = None,
) -> ReplyBatchSummary:
    """Find and queue bounded stored replies while holding the ingestion lock."""
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1000:
        raise ValueError("Reply batch limit must be between 1 and 1000")
    with ledger.worker_lock():
        recorded = ledger.series_revision(series_key, revision)
        if not recorded:
            raise ValueError("A recorded series revision is required")
        members = tuple(message.message_id for message in recorded.messages if message.message_id)
        status = ledger.status()
        scope = {
            "ledger": str(ledger.path.resolve()), "destination": comment_destination(publisher, pull_index),
            "series_key": series_key, "revision": revision, "snapshot": status.link_index_last_rowid,
            "inbox_url": inbox_url, "b4_python": b4_python,
        }
        after = ""
        if cursor is not None:
            if not isinstance(cursor, str) or not cursor or len(cursor) > 8192:
                raise ValueError("Invalid reply continuation cursor")
            try:
                value = json.loads(base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True))
            except (ValueError, UnicodeDecodeError, binascii.Error) as error:
                raise ValueError("Invalid reply continuation cursor") from error
            if not isinstance(value, dict) or set(value) != {*scope, "after"}:
                raise ValueError("Invalid reply continuation cursor")
            after = value.pop("after")
            if value != scope or not isinstance(after, str) or not after:
                raise ValueError("Reply continuation cursor no longer matches the ledger snapshot or destination")
        messages = ledger._related_thread_messages(members, limit=limit)
        identifiers = sorted(message.message_id for message in messages if message.kind == "reply" and message.message_id)
        if after and after not in identifiers:
            raise ValueError("Reply continuation cursor position is no longer available")
        selected = tuple(identifier for identifier in identifiers if identifier > after)[:limit + 1]
        page = frozenset(selected[:limit])
        result = _queue_thread_replies(
            ledger, publisher, pull_index, series_key, revision, messages, b4_python=b4_python, inbox_url=inbox_url,
            selected_ids=page, stored=True,
        )
        has_more = len(selected) > limit
        next_cursor = None
        if has_more:
            payload = {**scope, "after": selected[limit - 1]}
            next_cursor = base64.urlsafe_b64encode(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).decode().rstrip("=")
        return ReplyBatchSummary(
            result.matched, result.unmatched, result.unkeyed, result.inline_count, result.unresolved_count,
            result.diagnostics, has_more, next_cursor,
        )


def queue_b4_drafts(
    ledger: Ledger, publisher: ForgeCommentPublisher, pull_index: int,
    series_key: str, revision: int, repository: Path, branch: str, reviewer_email: str, *, b4_python: str,
) -> DraftQueueSummary:
    """Queue an immutable local draft snapshot; delivery still requires explicit approval."""
    snapshot = read_local_review_drafts(repository, branch, reviewer_email, python_executable=b4_python)
    with ledger.worker_lock():
        recorded = ledger.series_revision(series_key, revision)
        repository_destination = publisher.api.base_url + publisher.repository_path
        publication = ledger.publication(repository_destination, series_key, revision)
        if not recorded or not publication or publication.status != "complete" or publication.pull_index != pull_index:
            raise ValueError("A recorded publication for this series and pull request is required")
        if snapshot.revision != revision:
            raise ValueError("b4 draft revision does not match the published series")
        ledger.assert_publication_sources(repository_destination, pull_index)
        members = {message.message_id: message for message in recorded.messages}
        positions = set()
        for draft in snapshot.drafts:
            member = members.get(draft.message_id)
            if not member or draft.patch_index in positions:
                raise ValueError("b4 draft source does not identify a unique published series member")
            if draft.patch_index != member.patch_index and not (
                draft.patch_index == 0 and recorded.cover_message_id is None and member.patch_index == 1
            ):
                raise ValueError("b4 draft patch index does not match its published source")
            positions.add(draft.patch_index)
        index = None
        diagnostic = None
        if any(draft.comments for draft in snapshot.drafts):
            try:
                index = DiffIndex(_stable_pull_diff(publisher, pull_index, publication.head_commit))
            except (ForgeAPIError, StaleAnchorError, ValueError) as error:
                diagnostic = str(error)
        destination = comment_destination(publisher, pull_index)
        queued = inline_count = unresolved = 0
        for draft in snapshot.drafts:
            identity = json.dumps(
                [snapshot.tracking_commit, snapshot.reviewer_email, series_key, revision, draft.patch_index, draft.message_id],
                separators=(",", ":"), ensure_ascii=True,
            ).encode("utf-8")
            synthetic_id = f"b4-draft-{hashlib.sha256(identity).hexdigest()}@local.invalid"
            raw = f"Message-ID: <{synthetic_id}>\nSubject: Local b4 draft snapshot\n\nTracking commit: {snapshot.tracking_commit}\n".encode()
            ledger.record_message(synthetic_id, raw)
            prefix = (
                f"Local b4 review draft (no email sent by this integration)\n\nTracking commit: `{snapshot.tracking_commit}`"
                f"\n\nReviewer:\n\n{quote_untrusted_text(snapshot.reviewer_email)}"
                f"\n\nSource Message-ID:\n\n{quote_untrusted_text(draft.message_id)}"
            )
            sections = [prefix]
            if draft.reply:
                sections.append(f"Reply:\n\n{quote_untrusted_text(draft.reply)}")
            if draft.trailers:
                sections.append(f"Trailers (not a Gitea review state):\n\n{quote_untrusted_text(chr(10).join(draft.trailers))}")
            for comment in draft.comments:
                sections.append(f"Comment location:\n\n{quote_untrusted_text(f'{comment.path}:{comment.line}')}\n\n{quote_untrusted_text(comment.text)}")
            ledger.enqueue(synthetic_id, "ISSUE_COMMENT", destination, "b4-draft", {"body": "\n\n".join(sections)})
            queued += 1
            for position, comment in enumerate(draft.comments):
                anchor = index.resolve(asdict(comment)) if index is not None else None
                if anchor is None or not comment.text.strip():
                    unresolved += 1
                    continue
                effect = f"b4-inline:{position}:{hashlib.sha256(json.dumps(asdict(comment), sort_keys=True).encode()).hexdigest()}"
                ledger.enqueue(
                    synthetic_id, "INLINE_COMMENT", destination, effect,
                    {"body": f"{prefix}\n\n{quote_untrusted_text(comment.text)}", "head_commit": publication.head_commit, "anchor": asdict(anchor)},
                )
                inline_count += 1
        return DraftQueueSummary(snapshot.tracking_commit, queued, inline_count, unresolved, diagnostic)


def _queue_thread_replies(
    ledger: Ledger, publisher: ForgeCommentPublisher, pull_index: int,
    series_key: str, revision: int, messages: Iterable[ParsedMessage], *, b4_python: str, inbox_url: str,
    selected_ids: frozenset[str] | None = None, stored: bool = False,
) -> ReplyBatchSummary:
    _validate_inbox_url(inbox_url)
    recorded = ledger.series_revision(series_key, revision)
    destination = publisher.api.base_url + publisher.repository_path
    publication = ledger.publication(destination, series_key, revision)
    if (
        not recorded or not recorded.complete or recorded.conflicts
        or discover_series(recorded.messages) != (recorded,)
    ):
        raise ValueError("A complete recorded series revision is required")
    if not publication or publication.status != "complete" or publication.pull_index != pull_index:
        raise ValueError("Series revision is not published to this pull request")
    incoming = tuple(messages)
    if not stored:
        for message in incoming:
            ledger.record_parsed_message(message)
    ledger.assert_unconflicted(tuple(message.message_id for message in (*recorded.messages, *incoming) if message.message_id))
    ledger.assert_publication_sources(destination, pull_index)
    matches = match_thread_replies((*recorded.messages, *incoming), (recorded,))
    if stored:
        verified_matches = matches
    else:
        members = tuple(message.message_id for message in recorded.messages if message.message_id)
        related = ledger._related_thread_messages(members)
        verified_matches = match_thread_replies((*recorded.messages, *related), (recorded,))
    verified = {match.message.message_id: match for match in verified_matches.matched}
    inline_count = unresolved_count = 0
    diagnostics = []
    matched = tuple(match for match in matches.matched if selected_ids is None or match.message.message_id in selected_ids)
    if any(verified.get(match.message.message_id) != match for match in matched):
        raise ValueError("Reply thread ancestry changed; inspect before queueing")
    ledger.assert_message_ancestry_available(tuple(match.message for match in matched))
    for match in matched:
        result = _queue_verified_thread_reply(
            ledger, publisher, pull_index, match, publication.head_commit, b4_python=b4_python, inbox_url=inbox_url,
        )
        inline_count += result.inline_count
        unresolved_count += result.unresolved_count
        if result.diagnostic:
            diagnostics.append(f"{match.message.message_id}: {result.diagnostic}")
    return ReplyBatchSummary(
        matched=len(matched),
        unmatched=tuple(
            message.message_id for message in matches.unmatched
            if message.message_id and (selected_ids is None or message.message_id in selected_ids)
        ),
        unkeyed=sum(message.message_id is None for message in incoming),
        inline_count=inline_count, unresolved_count=unresolved_count,
        diagnostics=tuple(diagnostics),
    )