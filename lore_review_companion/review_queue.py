# SPDX-License-Identifier: GPL-2.0-or-later

"""Queue threaded mail replies and exact inline matches for a recorded PR."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import mailbox
import tempfile
from dataclasses import asdict, dataclass
from collections.abc import Mapping
from pathlib import Path
from typing import Iterable
from urllib.parse import quote, urlsplit

from lore_review_companion.anchor import DiffIndex
from lore_review_companion.b4_adapter import B4ReviewError, assemble_local_series, extract_quoted_comments, read_local_review_drafts
from lore_review_companion.forge_api import ForgeAPIError
from lore_review_companion.forge_comments import ForgeCommentPublisher, StaleAnchorError, imported_mail_text, quote_untrusted_text
from lore_review_companion.forge_imports import ForgeImportClient
from lore_review_companion.ledger import DeliveryStateConflict, Ledger, Publication, PublicationConflict
from lore_review_companion.mail_parser import ParsedMessage, extract_quoted_remarks
from lore_review_companion.outbox import comment_destination
from lore_review_companion.preflight import PatchCommit, prepare_patch_mailbox
from lore_review_companion.series import discover_series
from lore_review_companion.threads import ThreadMatch, match_thread_replies


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


def _stable_pull_comparison(
    publisher: ForgeCommentPublisher, pull_index: int, expected_head: str,
) -> tuple[str, tuple[str, str, str]]:
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
        return diff.decode("utf-8"), before
    except UnicodeDecodeError as error:
        raise ValueError("PR diff is not valid UTF-8") from error


def _stable_pull_diff(publisher: ForgeCommentPublisher, pull_index: int, expected_head: str) -> str:
    return _stable_pull_comparison(publisher, pull_index, expected_head)[0]


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
    b4_python: str | None = None,
    inbox_url: str = "https://lore.kernel.org/xen-devel/",
) -> ReplyQueueSummary:
    """Retain the complete reply and queue only uniquely resolved inline excerpts."""
    with ledger.worker_lock():
        return _queue_thread_reply(
            ledger, publisher, pull_index, match, b4_python=b4_python, inbox_url=inbox_url,
        )


def _queue_thread_reply(
    ledger: Ledger, publisher: ForgeCommentPublisher, pull_index: int, match: ThreadMatch, *,
    b4_python: str | None, inbox_url: str,
) -> ReplyQueueSummary:
    publication = _verified_reply_publication(ledger, publisher, pull_index, match, inbox_url=inbox_url)
    return _queue_verified_thread_reply(
        ledger, publisher, pull_index, match, publication.head_commit, b4_python=b4_python, inbox_url=inbox_url,
    )


def _verified_reply_publication(
    ledger: Ledger, publisher: ForgeCommentPublisher | ForgeImportClient, pull_index: int, match: ThreadMatch, *, inbox_url: str,
) -> Publication:
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
    return publication


def queue_imported_reply(
    ledger: Ledger, client: ForgeImportClient, pull_index: int, match: ThreadMatch, *,
    patch_commits: tuple[PatchCommit, ...], patch_diffs: Mapping[str, str], max_inline_lines: int = 50,
) -> ReplyQueueSummary:
    if type(max_inline_lines) is not int or not 1 <= max_inline_lines <= 1000:
        raise ValueError("A bounded inline line limit is required")
    with ledger.worker_lock():
        publication = _verified_reply_publication(ledger, client, pull_index, match, inbox_url=client.source_url)
        destination = comment_destination(client, pull_index)
        message = match.message
        if ledger.comment_effect_types(message.message_id, destination) & {"ISSUE_COMMENT", "INLINE_COMMENT"}:
            raise DeliveryStateConflict("Legacy effects exist; imported authorship requires a separate reviewed migration")
        recorded = ledger.series_revision(match.series_key, match.revision)
        members = sorted((member for member in recorded.messages if member.kind == "patch"), key=lambda member: member.patch_index or 0)
        if tuple(item.message_id for item in patch_commits) != tuple(member.message_id for member in members):
            raise ValueError("Patch commit mapping does not match the recorded series membership")
        parent = publication.base_commit
        for item in patch_commits:
            if item.before_commit != parent:
                raise ValueError("Patch commit mapping is not one exact parent chain")
            parent = item.after_commit
        if parent != publication.head_commit:
            raise ValueError("Patch commit mapping does not reach the recorded publication head")
        if not message.from_address:
            raise ValueError("Imported replies require a retained source email identity")
        estimate = message.time_estimate
        common = {
            "message_id": message.message_id, "message_sha256": message.sha256,
            "address": message.from_address, "name": message.from_name or "",
            "source_time": estimate.timestamp if estimate else None,
            "source_time_basis": "unverified" if estimate else "unknown",
        }
        thread = {"series_key": match.series_key, "revision": match.revision, "patch_message_id": match.patch_message_id}
        existing = ledger.comment_effects(message.message_id, destination)
        body_format = existing[0].payload.get("body_format") if existing else "mail-text-v1"
        format_fields = {"body_format": body_format} if body_format is not None else {}
        identity = hashlib.sha256(f"{message.message_id}\0imported-full-message-v1".encode()).hexdigest()
        conversation = ledger.enqueue(message.message_id, "IMPORT_CONVERSATION", destination, "imported-full-message-v1", {
            "payload_version": 3, "thread": thread, **format_fields,
            "request": {**common, "operation_key": identity,
                        "body": imported_mail_text(message.body_text, message.message_id, client.source_url, body_format)},
        })
        remarks = extract_quoted_remarks(message.body_text)
        if not remarks or match.patch_message_id is None:
            return ReplyQueueSummary(conversation, 0, 0)
        parents = message.in_reply_to or message.references[-1:]
        if len(parents) != 1:
            return ReplyQueueSummary(conversation, 0, len(remarks), "Quoted reply has no unique explicit parent")
        if len(parents) == 1:
            parent_source = ledger.messages_by_id(parents)[0]
            if parent_source.kind == "reply":
                return _queue_imported_inline_replies(ledger, client, destination, message, parent_source, remarks,
                                                      common, thread, conversation, body_format)
        patch = next((item for item in patch_commits if item.message_id == match.patch_message_id), None)
        diff = patch_diffs.get(match.patch_message_id)
        if patch is None or not isinstance(diff, str):
            return ReplyQueueSummary(conversation, 0, len(remarks), "Exact patch comparison is unavailable")
        capabilities = client.capabilities()
        supported_limit = capabilities.get("max_inline_lines")
        if (
            type(supported_limit) is not int or supported_limit < 0
            or (supported_limit > 0 and max_inline_lines > supported_limit)
            or "review" not in capabilities["operations"]
        ):
            return ReplyQueueSummary(conversation, 0, len(remarks), "Requested review range capability is unavailable")
        try:
            snapshot = client.comparison(pull_index, after_commit=patch.after_commit)
            if (
                snapshot["head_commit"] != publication.head_commit or snapshot["before_commit"] != patch.before_commit
                or snapshot["diff"] != diff
            ):
                raise StaleAnchorError("Exact server patch comparison differs from the recorded commit mapping")
            comparison = tuple(snapshot[field] for field in ("head_commit", "base_commit", "merge_base"))
        except (ForgeAPIError, StaleAnchorError, ValueError) as error:
            return ReplyQueueSummary(conversation, 0, len(remarks), str(error))
        index = DiffIndex(diff)
        comments, evidence = [], []
        for remark in remarks:
            anchor = index.resolve_quote(remark, max_lines=max_inline_lines)
            if anchor is None or not remark.text.strip():
                continue
            comments.append({
                "path": anchor.path, "body": imported_mail_text(remark.text, message.message_id, client.source_url, body_format),
                "before_commit": patch.before_commit, "after_commit": patch.after_commit,
                "old_position": anchor.old_position, "new_position": anchor.new_position,
                "extra_lines_count": anchor.extra_lines_count,
            })
            evidence.append(asdict(remark))
        if comments:
            identity = hashlib.sha256(f"{message.message_id}\0imported-review-v1".encode()).hexdigest()
            ledger.enqueue(message.message_id, "IMPORT_REVIEW", destination, "imported-review-v1", {
                "payload_version": 3, "thread": thread, "max_inline_lines": max_inline_lines, **format_fields,
                "evidence": {
                    "parser": "quote-blocks-v1", "source_sha256": message.sha256,
                    "patch_message_id": patch.message_id,
                    "patch_source_sha256": next(member.sha256 for member in members if member.message_id == patch.message_id),
                    "patch_diff": diff, "patch_diff_sha256": hashlib.sha256(diff.encode("utf-8")).hexdigest(), "remarks": evidence,
                },
                "request": {
                    **common, "operation_key": identity,
                    "body": imported_mail_text("", message.message_id, client.source_url, body_format) if body_format else (message.subject or "Mail review"),
                    "head_commit": comparison[0], "base_commit": comparison[1], "merge_base": comparison[2],
                    "comments": comments,
                },
            })
        return ReplyQueueSummary(conversation, len(comments), len(remarks) - len(comments))


def _queue_imported_inline_replies(ledger, client, destination, message, parent, remarks, common, thread, conversation, body_format):
    targets = []
    for effect in ledger.comment_effects(parent.message_id, destination):
        if effect.event_type not in ("IMPORT_REVIEW", "IMPORT_REPLY"):
            continue
        if effect.payload.get("thread") != thread:
            raise PublicationConflict("Imported parent belongs to a different revision thread")
        request = effect.payload.get("request", {})
        remote = client.lookup_operation(request.get("operation_key"))
        if remote is None:
            continue
        expected_type = "review" if effect.event_type == "IMPORT_REVIEW" else "review-reply"
        if (remote.get("message_sha256") != parent.sha256 or remote.get("message_id") != parent.message_id
            or remote.get("type") != expected_type):
            raise PublicationConflict("Imported parent lookup does not match retained mail evidence")
        if effect.event_type == "IMPORT_REVIEW":
            parent_remarks = effect.payload.get("evidence", {}).get("remarks", [])
            identifiers = remote.get("code_comment_ids", [])
            comments = request.get("comments", [])
        else:
            parent_remarks = [effect.payload.get("evidence", {}).get("remark", {})]
            identifiers = [remote.get("comment_id")]
            comments = [request]
        if len(parent_remarks) != len(identifiers) or len(comments) != len(identifiers):
            raise PublicationConflict("Imported parent has no complete inline identifier mapping")
        for position, (parent_remark, identifier, comment) in enumerate(zip(parent_remarks, identifiers, comments, strict=True)):
            if type(identifier) is not int or identifier <= 0:
                raise PublicationConflict("Imported parent returned an invalid inline identifier")
            targets.append((parent_remark.get("text"), identifier, comment.get("body"), effect, position))
    queued = 0
    for position, remark in enumerate(remarks):
        candidates = [target for target in targets if "\n".join(remark.quote) == target[0]]
        if len(candidates) != 1:
            continue
        _, identifier, parent_body, parent_effect, parent_position = candidates[0]
        if not isinstance(parent_body, str):
            raise PublicationConflict("Imported parent has no retained comment body")
        serialized = json.dumps(asdict(remark), sort_keys=True, separators=(",", ":"))
        effect_key = f"imported-reply-v1:{position}:{hashlib.sha256(serialized.encode()).hexdigest()}"
        operation_key = hashlib.sha256(f"{message.message_id}\0{effect_key}".encode()).hexdigest()
        ledger.enqueue(message.message_id, "IMPORT_REPLY", destination, effect_key, {
            "payload_version": 3, "thread": thread,
            **({"body_format": body_format} if body_format is not None else {}),
            "evidence": {"parser": "quote-blocks-v1", "source_sha256": message.sha256, "remark": asdict(remark),
                         "parent_message_id": parent.message_id, "parent_effect_key": parent_effect.effect_key,
                         "parent_operation_key": parent_effect.payload["request"]["operation_key"], "parent_position": parent_position},
            "request": {**common, "operation_key": operation_key,
                        "body": imported_mail_text(remark.text, message.message_id, client.source_url, body_format),
                        "parent_id": identifier, "parent_body_sha256": hashlib.sha256(parent_body.encode()).hexdigest()},
        })
        queued += 1
    diagnostic = None if queued == len(remarks) else "Quoted reply does not identify exactly one accepted imported inline comment"
    return ReplyQueueSummary(conversation, queued, len(remarks) - queued, diagnostic)


def queue_imported_replies(
    ledger: Ledger, client: ForgeImportClient, pull_index: int, series_key: str, revision: int,
    repository: Path, messages: Iterable[ParsedMessage], *, max_inline_lines: int = 50,
) -> ReplyBatchSummary:
    recorded = ledger.series_revision(series_key, revision)
    publication = ledger.publication(client.api.base_url + client.repository_path, series_key, revision)
    if not recorded or not publication or publication.status != "complete" or publication.pull_index != pull_index:
        raise ValueError("A completed publication and recorded series are required")
    ledger.assert_publication_sources(client.api.base_url + client.repository_path, pull_index)
    selected = tuple(messages)
    if not selected or len(selected) > 1000:
        raise ValueError("Select between 1 and 1000 retained reply messages")
    for message in selected:
        if message.kind != "reply" or not message.message_id:
            raise ValueError("Imported queue input must contain retained replies with Message-IDs")
        ledger.record_parsed_message(message)
    members = tuple(message.message_id for message in recorded.messages if message.message_id)
    related = ledger._related_thread_messages(members)
    matches = match_thread_replies((*recorded.messages, *related, *selected), (recorded,))
    identifiers = {message.message_id for message in selected}
    matched = tuple(match for match in matches.matched if match.message.message_id in identifiers)
    unresolved = tuple(message.message_id for message in matches.unmatched if message.message_id in identifiers)
    with tempfile.TemporaryDirectory(prefix="lore-import-queue-") as temporary:
        mailbox_path = Path(temporary) / "series.mbox"
        box = mailbox.mbox(mailbox_path)
        try:
            for message in recorded.messages:
                box.add(message.raw_message)
            box.flush()
        finally:
            box.close()
        assembly = assemble_local_series(mailbox_path, revision=revision)
        with prepare_patch_mailbox(repository, publication.base_commit, assembly.patch_mailbox) as prepared:
            if prepared.result.head_commit != publication.head_commit:
                raise PublicationConflict("Reassembled series differs from the published immutable head")
            patches = sorted((message for message in recorded.messages if message.kind == "patch"), key=lambda message: message.patch_index or 0)
            comparisons = prepared.patch_commits(tuple(message.message_id for message in patches))
            diffs = {}
            for comparison in comparisons:
                if comparison.message_id not in {match.patch_message_id for match in matched}:
                    continue
                snapshot = client.comparison(pull_index, after_commit=comparison.after_commit)
                if snapshot["before_commit"] != comparison.before_commit or snapshot["head_commit"] != publication.head_commit:
                    raise PublicationConflict("Published patch comparison differs from the assembled source")
                diffs[comparison.message_id] = snapshot["diff"]
            results = tuple(queue_imported_reply(
                ledger, client, pull_index, match, patch_commits=comparisons, patch_diffs=diffs,
                max_inline_lines=max_inline_lines,
            ) for match in matched)
    return ReplyBatchSummary(len(matched), unresolved, 0, sum(result.inline_count for result in results),
                             sum(result.unresolved_count for result in results),
                             tuple(result.diagnostic for result in results if result.diagnostic))


def _queue_verified_thread_reply(
    ledger: Ledger, publisher: ForgeCommentPublisher, pull_index: int, match: ThreadMatch, head_commit: str, *,
    b4_python: str | None, inbox_url: str,
) -> ReplyQueueSummary:
    message = match.message
    message_id = message.message_id
    if not message_id:
        raise ValueError("A reply with a valid Message-ID is required")
    destination = comment_destination(publisher, pull_index)
    if ledger.comment_effect_types(message_id, destination) & {"IMPORT_CONVERSATION", "IMPORT_REVIEW", "IMPORT_REPLY"}:
        raise DeliveryStateConflict("Imported effects exist; legacy delivery requires a separate reviewed migration")
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
        comments = (
            extract_quoted_remarks(message.body_text) if b4_python is None
            else extract_quoted_comments(message.body_text, python_executable=b4_python)
        )
    except B4ReviewError as error:
        return ReplyQueueSummary(conversation_id, 0, 0, str(error))
    if not comments:
        return ReplyQueueSummary(conversation_id, 0, 0)
    if b4_python is None and ledger.has_legacy_inline_effects(message_id, destination):
        return ReplyQueueSummary(
            conversation_id, 0, len(comments),
            "Legacy inline effects exist for this message; parser migration requires a separate reviewed plan",
        )
    try:
        diff, comparison = _stable_pull_comparison(publisher, pull_index, head_commit)
        index = DiffIndex(diff)
    except (ForgeAPIError, StaleAnchorError, ValueError) as error:
        return ReplyQueueSummary(conversation_id, 0, len(comments), str(error))

    inline_count = unresolved = 0
    for position, comment in enumerate(comments):
        anchor = (
            index.resolve_quote(comment, max_lines=publisher.max_inline_lines) if b4_python is None
            else index.resolve(asdict(comment))
        )
        if anchor is None or not comment.text.strip():
            unresolved += 1
            continue
        identity = json.dumps(asdict(comment), sort_keys=True, separators=(",", ":")).encode("utf-8")
        if b4_python is None:
            effect_key = f"inline-v2:{position}:{hashlib.sha256(identity).hexdigest()}"
            evidence = {
                **asdict(comment), "parser": "quote-blocks-v1", "source_sha256": message.sha256,
                "source_url": source_url,
                "source_time": asdict(message.time_estimate) if message.time_estimate else None,
            }
            ledger.enqueue(message_id, "INLINE_COMMENT", destination, effect_key, {
                "payload_version": 2, "body": f"{prefix}\n\n{quote_untrusted_text(comment.text)}",
                "head_commit": head_commit, "base_commit": comparison[1], "merge_base": comparison[2],
                "anchor": asdict(anchor), "thread": thread, "evidence": evidence,
            })
            inline_count += 1
            continue
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
    b4_python: str | None = None, inbox_url: str = "https://lore.kernel.org/xen-devel/",
) -> ReplyBatchSummary:
    """Queue matching replies against the previously published series membership."""
    with ledger.worker_lock():
        return _queue_thread_replies(
            ledger, publisher, pull_index, series_key, revision, messages, b4_python=b4_python, inbox_url=inbox_url,
        )


def queue_archive_replies(
    ledger: Ledger, publisher: ForgeCommentPublisher, pull_index: int,
    series_key: str, revision: int, *, b4_python: str | None = None,
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
        if b4_python is None:
            scope["max_inline_lines"] = publisher.max_inline_lines
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
    series_key: str, revision: int, messages: Iterable[ParsedMessage], *, b4_python: str | None, inbox_url: str,
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