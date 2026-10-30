"""Deliver queued comments with locking and provenance reconciliation."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import asdict, dataclass, replace
from contextlib import nullcontext

from lore_review_companion.anchor import Anchor
from lore_review_companion.forge_api import ForgeAPIError, authenticated_user
from lore_review_companion.forge_comments import (
    DeliveryResult, DeliveryUncertainError, ForgeCommentPublisher, with_tool_attribution,
)
from lore_review_companion.forge_pulls import _series_marker
from lore_review_companion.ledger import Delivery, DeliveryStateConflict, Ledger, MessageConflictError, PublicationConflict
from lore_review_companion.threads import match_thread_replies


@dataclass(frozen=True)
class CommentBatchSummary:
    delivered: int = 0
    planned: int = 0
    failed: int = 0


@dataclass(frozen=True)
class CommentDeliveryPlan:
    destination: str
    actor: str
    actor_id: int
    event_type: str
    allow_inline_writes: bool
    retry_uncertain: bool
    deliveries: tuple[Delivery, ...]

    def approval_digest(self) -> str:
        value = json.dumps(asdict(self), sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(value).hexdigest()


@dataclass(frozen=True)
class LegacyMigrationPlan:
    ledger_path: str
    previous_destination: str
    destination: str
    repository_id: int
    series_key: str
    revision: int
    revision_branch: str
    head_commit: str
    actor: str
    actor_id: int
    reason: str
    limit: int
    deliveries: tuple[Delivery, ...]
    thread_bindings: tuple["LegacyThreadBinding", ...]

    def approval_digest(self) -> str:
        value = json.dumps(asdict(self), sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(value).hexdigest()


@dataclass(frozen=True)
class LegacyMigrationResult:
    migrated: int
    approval_digest: str


@dataclass(frozen=True)
class LegacyThreadBinding:
    delivery_id: int
    series_key: str
    revision: int
    patch_message_id: str | None


def comment_destination(publisher: ForgeCommentPublisher, pull_index: int) -> str:
    if not isinstance(pull_index, int) or isinstance(pull_index, bool) or pull_index < 1:
        raise ValueError("pull request index must be a positive integer")
    return f"{publisher.api.base_url}{publisher.repository_path}#{pull_index}"


def plan_legacy_migration(
    ledger: Ledger, publisher: ForgeCommentPublisher, pull_index: int, reason: str, *, limit: int = 100,
    scoped_delivery_ids: tuple[int, ...] | None = None,
) -> LegacyMigrationPlan:
    if not 1 <= limit <= 1000:
        raise ValueError("Migration limit must be between 1 and 1000")
    if any(re.fullmatch(r"[A-Za-z0-9._-]+", name) is None for name in (publisher.owner, publisher.repo)):
        raise ValueError("Legacy migration requires simple repository owner and name")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 500 or any(ord(char) < 32 or ord(char) == 127 for char in reason):
        raise ValueError("Legacy migration requires a short single-line reason")
    destination = comment_destination(publisher, pull_index)
    repository_destination = publisher.api.base_url + publisher.repository_path
    publications = ledger.publications_for_pull(repository_destination, pull_index)
    if len(publications) != 1:
        raise ValueError("Legacy migration requires exactly one recorded completed pull request mapping")
    ledger.assert_publication_sources(repository_destination, pull_index)
    repository = publisher.api.request_json(publisher.repository_path)
    repository_id = repository.get("id") if isinstance(repository, dict) else None
    if not isinstance(repository_id, int) or isinstance(repository_id, bool) or repository_id < 1:
        raise ValueError("Gitea repository ID is not available")
    pull = publisher.api.request_json(f"{publisher.repository_path}/pulls/{pull_index}")
    head = pull.get("head") if isinstance(pull, dict) else None
    base = pull.get("base") if isinstance(pull, dict) else None
    if (
        not isinstance(pull, dict) or pull.get("number") != pull_index or not isinstance(head, dict)
        or head.get("ref") != publications[0].revision_branch or head.get("sha") != publications[0].head_commit
        or head.get("repo_id") != repository_id or not isinstance(base, dict) or base.get("repo_id") != repository_id
        or _series_marker(publications[0].series_key) not in str(pull.get("body", ""))
    ):
        raise ValueError("Gitea pull request does not match the recorded publication")
    actor = authenticated_user(publisher.api)
    previous = destination if scoped_delivery_ids is not None else f"{publisher.owner}/{publisher.repo}#{pull_index}"
    deliveries = tuple(ledger.pending(limit, destination=previous, delivery_ids=scoped_delivery_ids))
    if scoped_delivery_ids is not None and (
        {item.id for item in deliveries} != set(scoped_delivery_ids) or any("thread" in item.payload for item in deliveries)
    ):
        raise DeliveryStateConflict("Selected scoped deliveries must be pending and lack thread provenance")
    if any(item.event_type not in ("ISSUE_COMMENT", "INLINE_COMMENT") or item.write_state != "pending" or item.attempts != 0 for item in deliveries):
        raise DeliveryStateConflict("Only untouched pending legacy comment deliveries can be migrated")
    ledger.assert_unconflicted(tuple(item.message_id for item in deliveries))
    publication = publications[0]
    recorded = ledger.series_revision(publication.series_key, publication.revision)
    if not recorded:
        raise PublicationConflict("Legacy destination has no recorded series")
    members = tuple(message.message_id for message in recorded.messages if message.message_id)
    related = ledger._related_thread_messages(members)
    matches = match_thread_replies((*recorded.messages, *related), (recorded,))
    matched = {match.message.message_id: match for match in matches.matched}
    bindings = []
    for delivery in deliveries:
        match = matched.get(delivery.message_id)
        if not match or (delivery.event_type == "INLINE_COMMENT" and match.patch_message_id is None):
            raise DeliveryStateConflict("Legacy delivery does not have a verified reply thread")
        binding = LegacyThreadBinding(delivery.id, match.series_key, match.revision, match.patch_message_id)
        expected = {"series_key": binding.series_key, "revision": binding.revision, "patch_message_id": binding.patch_message_id}
        if "thread" in delivery.payload and delivery.payload["thread"] != expected:
            raise DeliveryStateConflict("Legacy delivery has different thread provenance")
        bindings.append(binding)
    return LegacyMigrationPlan(
        str(ledger.path.resolve()), previous, destination, repository_id, publication.series_key,
        publication.revision, publication.revision_branch, publication.head_commit,
        actor.login, actor.user_id, reason.strip(), limit, deliveries, tuple(bindings),
    )


def migrate_legacy_deliveries(
    ledger: Ledger, publisher: ForgeCommentPublisher, pull_index: int, reason: str, *, approved_digest: str, limit: int = 100,
    scoped_delivery_ids: tuple[int, ...] | None = None,
) -> LegacyMigrationResult:
    if not isinstance(approved_digest, str) or re.fullmatch(r"[0-9a-f]{64}", approved_digest) is None:
        raise ValueError("A valid legacy migration approval digest is required")
    with ledger.worker_lock():
        plan = plan_legacy_migration(
            ledger, publisher, pull_index, reason, limit=limit, scoped_delivery_ids=scoped_delivery_ids,
        )
        digest = plan.approval_digest()
        if not hmac.compare_digest(approved_digest, digest):
            raise ValueError("Approval digest does not match the current legacy migration plan")
        migrated = ledger.move_legacy_deliveries(
            plan.deliveries, plan.destination,
            {binding.delivery_id: {
                "series_key": binding.series_key, "revision": binding.revision, "patch_message_id": binding.patch_message_id,
            } for binding in plan.thread_bindings},
            actor=f"{plan.actor} (id {plan.actor_id})", reason=plan.reason, digest=digest,
        )
        return LegacyMigrationResult(migrated, digest)


def _attributed_delivery(delivery: Delivery, login: str) -> Delivery:
    body = delivery.payload.get("body")
    if not isinstance(body, str) or not body.strip():
        return delivery
    return replace(delivery, payload={**delivery.payload, "body": with_tool_attribution(body, login)})


def _publish_comment(
    publisher: ForgeCommentPublisher, pull_index: int, delivery: Delivery,
) -> DeliveryResult:
    body = delivery.payload.get("body")
    if not isinstance(body, str) or not body.strip():
        raise ValueError("queued comment body must be a non-empty string")
    arguments = {"source_message_id": delivery.message_id, "effect_key": delivery.effect_key, "body": body}
    if delivery.event_type == "ISSUE_COMMENT":
        return publisher.ensure_issue_comment(pull_index, **arguments)
    anchor_data = delivery.payload.get("anchor")
    head_commit = delivery.payload.get("head_commit")
    if not isinstance(anchor_data, dict) or set(anchor_data) != {"path", "old_position", "new_position"}:
        raise ValueError("queued inline comment must have one complete anchor")
    anchor = Anchor(**anchor_data)
    return publisher.ensure_inline_comment(pull_index, anchor, head_commit=head_commit, **arguments)


def _assert_thread_sources(
    ledger: Ledger, publisher: ForgeCommentPublisher, pull_index: int, deliveries: tuple[Delivery, ...],
) -> None:
    destination = publisher.api.base_url + publisher.repository_path
    cached = {}
    sources = []
    publications = ledger.publications_for_pull(destination, pull_index)
    for delivery in deliveries:
        thread = delivery.payload.get("thread")
        if thread is None:
            if publications and ledger.messages_by_id((delivery.message_id,))[0].kind == "reply":
                raise PublicationConflict("Queued reply lacks verified thread provenance; inspect before delivering")
            continue
        if (
            not isinstance(thread, dict) or set(thread) != {"series_key", "revision", "patch_message_id"}
            or not isinstance(thread["series_key"], str) or not isinstance(thread["revision"], int)
            or isinstance(thread["revision"], bool)
            or (thread["patch_message_id"] is not None and not isinstance(thread["patch_message_id"], str))
        ):
            raise PublicationConflict("Queued reply has invalid thread provenance")
        key = thread["series_key"], thread["revision"]
        if key not in cached:
            recorded = ledger.series_revision(*key)
            publication = ledger.publication(destination, *key)
            if not recorded or not publication or publication.status != "complete" or publication.pull_index != pull_index:
                raise PublicationConflict("Queued reply no longer matches its recorded publication")
            members = tuple(message.message_id for message in recorded.messages if message.message_id)
            related = ledger._related_thread_messages(members)
            matches = match_thread_replies((*recorded.messages, *related), (recorded,))
            cached[key] = {match.message.message_id: match for match in matches.matched}
        match = cached[key].get(delivery.message_id)
        if not match or match.patch_message_id != thread["patch_message_id"]:
            raise PublicationConflict("Queued reply thread ancestry changed; inspect before delivering")
        sources.append(match.message)
    ledger.assert_message_ancestry_available(tuple(sources))


def _deliver_comments(
    ledger: Ledger, publisher: ForgeCommentPublisher, pull_index: int, event_type: str,
    *, limit: int, retry_uncertain: bool, selected: tuple[Delivery, ...] | None = None,
    lock_held: bool = False,
) -> CommentBatchSummary:
    destination = comment_destination(publisher, pull_index)
    if retry_uncertain and not publisher.write_enabled:
        raise ValueError("Retrying uncertain comments requires writes to be enabled")
    lock = ledger.worker_lock() if publisher.write_enabled and not lock_held else nullcontext()
    delivered = planned = failed = 0
    with lock:
        ledger.assert_publication_sources(publisher.api.base_url + publisher.repository_path, pull_index)
        pending = selected if selected is not None else ledger.pending(limit, destination=destination, event_type=event_type)
        _assert_thread_sources(ledger, publisher, pull_index, tuple(pending))
        if selected is None and pending:
            actor = authenticated_user(publisher.api)
            pending = tuple(_attributed_delivery(delivery, actor.login) for delivery in pending)
        preview = ForgeCommentPublisher(publisher.api, publisher.owner, publisher.repo)
        for delivery in pending:
            try:
                ledger.assert_unconflicted((delivery.message_id,))
            except MessageConflictError as error:
                if publisher.write_enabled:
                    ledger.record_error(delivery.id, str(error))
                failed += 1
                continue
            if not publisher.write_enabled:
                try:
                    _publish_comment(preview, pull_index, delivery)
                    planned += 1
                except (ForgeAPIError, DeliveryUncertainError, ValueError):
                    failed += 1
                continue

            if delivery.write_state in ("sending", "uncertain"):
                try:
                    existing = _publish_comment(preview, pull_index, delivery)
                except (ForgeAPIError, DeliveryUncertainError, ValueError) as error:
                    ledger.record_error(delivery.id, str(error))
                    failed += 1
                    continue
                if not existing.planned:
                    ledger.mark_delivered(delivery.id)
                    delivered += 1
                    continue
                if not retry_uncertain:
                    ledger.record_error(delivery.id, "Earlier comment write is unconfirmed; inspect before retrying")
                    failed += 1
                    continue

            body = delivery.payload.get("body")
            if not isinstance(body, str) or not body.strip():
                ledger.record_attempt(delivery.id)
                ledger.record_error(delivery.id, "queued comment body must be a non-empty string")
                failed += 1
                continue
            ledger.begin_delivery(delivery.id, retry_uncertain=retry_uncertain)
            try:
                result = _publish_comment(publisher, pull_index, delivery)
                if result.planned:
                    raise DeliveryUncertainError("comment delivery was not confirmed")
                ledger.mark_delivered(delivery.id)
                delivered += 1
            except (ForgeAPIError, DeliveryUncertainError, ValueError) as error:
                ledger.mark_delivery_uncertain(delivery.id, str(error))
                failed += 1
    return CommentBatchSummary(delivered, planned, failed)


def plan_comment_delivery(
    ledger: Ledger, publisher: ForgeCommentPublisher, pull_index: int, event_type: str,
    *, limit: int = 100, retry_uncertain: bool = False,
) -> CommentDeliveryPlan:
    if event_type not in ("ISSUE_COMMENT", "INLINE_COMMENT"):
        raise ValueError("Unsupported comment event type")
    if not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000")
    destination = comment_destination(publisher, pull_index)
    ledger.assert_publication_sources(publisher.api.base_url + publisher.repository_path, pull_index)
    actor = authenticated_user(publisher.api)
    deliveries = tuple(ledger.pending(limit, destination=destination, event_type=event_type))
    ledger.assert_unconflicted(tuple(delivery.message_id for delivery in deliveries))
    _assert_thread_sources(ledger, publisher, pull_index, deliveries)
    deliveries = tuple(_attributed_delivery(delivery, actor.login) for delivery in deliveries)
    return CommentDeliveryPlan(
        destination, actor.login, actor.user_id, event_type, publisher.allow_inline_writes, retry_uncertain, deliveries,
    )


def deliver_approved_comments(
    ledger: Ledger, publisher: ForgeCommentPublisher, pull_index: int, event_type: str,
    *, approved_digest: str, limit: int = 100, retry_uncertain: bool = False,
) -> CommentBatchSummary:
    if not publisher.write_enabled:
        raise ValueError("Approved comment delivery requires writes to be enabled")
    if not isinstance(approved_digest, str) or re.fullmatch(r"[0-9a-f]{64}", approved_digest) is None:
        raise ValueError("A valid comment approval digest is required")
    with ledger.worker_lock():
        plan = plan_comment_delivery(
            ledger, publisher, pull_index, event_type, limit=limit, retry_uncertain=retry_uncertain,
        )
        if not hmac.compare_digest(approved_digest, plan.approval_digest()):
            raise ValueError("Approval digest does not match the current comment plan")
        return _deliver_comments(
            ledger, publisher, pull_index, event_type, limit=limit, retry_uncertain=retry_uncertain,
            selected=plan.deliveries, lock_held=True,
        )


def deliver_issue_comments(
    ledger: Ledger, publisher: ForgeCommentPublisher, pull_index: int, *, limit: int = 100,
    retry_uncertain: bool = False,
) -> CommentBatchSummary:
    """Deliver or preview one PR's queued conversation comments."""
    return _deliver_comments(
        ledger, publisher, pull_index, "ISSUE_COMMENT", limit=limit, retry_uncertain=retry_uncertain,
    )


def deliver_inline_comments(
    ledger: Ledger, publisher: ForgeCommentPublisher, pull_index: int, *, limit: int = 100,
    retry_uncertain: bool = False,
) -> CommentBatchSummary:
    """Deliver pinned inline comments, falling back when the PR head changed."""
    return _deliver_comments(
        ledger, publisher, pull_index, "INLINE_COMMENT", limit=limit, retry_uncertain=retry_uncertain,
    )