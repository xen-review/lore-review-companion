# SPDX-License-Identifier: GPL-2.0-or-later

"""Deliver queued comments with locking and provenance reconciliation."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import asdict, dataclass, replace
from contextlib import nullcontext

from lore_review_companion.anchor import (
    Anchor,
    DiffIndex,
    RangeAnchor,
    segmented_review_effect_key,
)
from lore_review_companion.forge_api import ForgeAPIError, authenticated_user
from lore_review_companion.forge_comments import (
    DeliveryResult,
    DeliveryUncertainError,
    ForgeCommentPublisher,
    with_tool_attribution,
    imported_mail_text,
    quoted_parent_matches,
    quote_untrusted_text,
    reply_prose,
)
from lore_review_companion.forge_imports import ForgeImportClient
from lore_review_companion.forge_pulls import _series_marker
from lore_review_companion.ledger import (
    Delivery,
    DeliveryStateConflict,
    Ledger,
    MessageConflictError,
    PublicationConflict,
)
from lore_review_companion.mail_parser import extract_quoted_remarks
from lore_review_companion.threads import match_thread_replies


@dataclass(frozen=True)
class CommentBatchSummary:
    delivered: int = 0
    planned: int = 0
    failed: int = 0


@dataclass(frozen=True)
class ImportDeliveryPlan:
    destination: str
    source_url: str
    repository_id: int
    actor: str
    actor_id: int
    event_type: str
    max_inline_lines: int
    retry_uncertain: bool
    deliveries: tuple[Delivery, ...]

    def approval_digest(self) -> str:
        value = json.dumps(asdict(self), sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
        return hashlib.sha256(value).hexdigest()


def _assert_import_payload(
    ledger: Ledger, delivery: Delivery, *, source_url: str, depth: int = 0
) -> None:
    if depth > 100:
        raise PublicationConflict(
            "Imported reply evidence exceeds the bounded ancestry limit"
        )
    payload = delivery.payload
    body_format = payload.get("body_format")
    if body_format not in (None, "mail-text-v1", "mail-text-v2", "mail-text-v3"):
        raise PublicationConflict("Unsupported imported message body format")
    request = payload.get("request")
    source = ledger.messages_by_id((delivery.message_id,))[0]
    if (
        type(payload.get("payload_version")) is not int
        or payload["payload_version"] != 3
        or not isinstance(request, dict)
        or request.get("message_id") != delivery.message_id
        or request.get("message_sha256") != source.sha256
        or not source.from_address
        or request.get("address") != source.from_address
        or request.get("name") != (source.from_name or "")
        or request.get("source_time")
        != (source.time_estimate.timestamp if source.time_estimate else None)
        or request.get("source_time_basis")
        != ("unverified" if source.time_estimate else "unknown")
    ):
        raise PublicationConflict(
            "Imported delivery lacks matching raw-mail identity evidence"
        )
    marker = (
        "imported-full-message-v1"
        if delivery.event_type == "IMPORT_CONVERSATION"
        else delivery.effect_key
        if delivery.event_type == "IMPORT_REPLY"
        or delivery.effect_key.startswith("imported-review-segment-v1:")
        else "imported-review-v1"
    )
    expected_key = hashlib.sha256(
        f"{delivery.message_id}\0{marker}".encode()
    ).hexdigest()
    if request.get("operation_key") != expected_key:
        raise PublicationConflict(
            "Imported operation key does not match the retained source message"
        )
    if delivery.event_type == "IMPORT_CONVERSATION":
        text = source.body_text
        presentation = payload.get("presentation_parent")
        suffix = ""
        if presentation is not None:
            parents = source.in_reply_to or source.references[-1:]
            if (
                body_format not in ("mail-text-v2", "mail-text-v3")
                or len(parents) != 1
                or not isinstance(presentation, dict)
                or presentation.get("message_id") != parents[0]
            ):
                raise PublicationConflict(
                    "Concise reply lacks a unique explicit parent"
                )
            effects = [
                effect
                for effect in ledger.comment_effects(parents[0], delivery.destination)
                if effect.event_type
                in ("IMPORT_CONVERSATION", "IMPORT_REVIEW", "IMPORT_REPLY")
                and effect.effect_key == presentation.get("effect_key")
            ]
            if (
                len(effects) != 1
                or effects[0].payload.get("thread") != payload.get("thread")
                or presentation.get("operation_key")
                != effects[0].payload["request"]["operation_key"]
                or presentation.get("type")
                != {
                    "IMPORT_CONVERSATION": "conversation",
                    "IMPORT_REVIEW": "review",
                    "IMPORT_REPLY": "review-reply",
                }[effects[0].event_type]
            ):
                raise PublicationConflict(
                    "Concise reply parent does not match this revision"
                )
            _assert_import_payload(
                ledger, effects[0], source_url=source_url, depth=depth + 1
            )
            identifier = presentation.get("comment_id")
            site, index = delivery.destination.rsplit("#", 1)
            site = site.replace("/api/v1/repos/", "/")
            url = f"{site}/pulls/{index}#issuecomment-{identifier}"
            if (
                type(identifier) is not int
                or identifier <= 0
                or presentation.get("url") != url
            ):
                raise PublicationConflict(
                    "Concise reply parent URL differs from its destination"
                )
            text = reply_prose(source.body_text)
            suffix = f'\n\n<small><a href="{url}">In reply to the preceding review comment</a></small>'
        if (
            request.get("body")
            != imported_mail_text(
                text,
                source.message_id,
                source_url,
                body_format,
                source.from_name or source.from_address,
            )
            + suffix
        ):
            raise PublicationConflict(
                "Imported conversation differs from the retained complete message"
            )
        return
    if delivery.event_type == "IMPORT_REPLY":
        evidence = payload.get("evidence")
        parents = source.in_reply_to or source.references[-1:]
        if (
            not isinstance(evidence, dict)
            or evidence.get("parser") != "quote-blocks-v1"
            or evidence.get("source_sha256") != source.sha256
            or len(parents) != 1
            or evidence.get("parent_message_id") != parents[0]
            or type(evidence.get("parent_position")) is not int
            or evidence["parent_position"] < 0
        ):
            raise PublicationConflict(
                "Imported reply lacks explicit quoted parent evidence"
            )
        effects = [
            effect
            for effect in ledger.comment_effects(parents[0], delivery.destination)
            if effect.effect_key == evidence.get("parent_effect_key")
            and effect.event_type in ("IMPORT_REVIEW", "IMPORT_REPLY")
        ]
        if len(effects) != 1:
            raise PublicationConflict("Imported reply parent effect is unavailable")
        parent = effects[0]
        if parent.payload.get("thread") != payload.get("thread"):
            raise PublicationConflict(
                "Imported reply parent belongs to a different revision thread"
            )
        _assert_import_payload(ledger, parent, source_url=source_url, depth=depth + 1)
        parent_request = parent.payload["request"]
        if evidence.get("parent_operation_key") != parent_request["operation_key"]:
            raise PublicationConflict("Imported reply parent operation changed")
        position = evidence["parent_position"]
        parent_remarks = (
            parent.payload["evidence"]["remarks"]
            if parent.event_type == "IMPORT_REVIEW"
            else [parent.payload["evidence"]["remark"]]
        )
        comments = (
            parent_request["comments"]
            if parent.event_type == "IMPORT_REVIEW"
            else [parent_request]
        )
        remark = next(
            (
                item
                for item in extract_quoted_remarks(source.body_text)
                if json.loads(json.dumps(asdict(item))) == evidence.get("remark")
            ),
            None,
        )
        if (
            position >= len(parent_remarks)
            or position >= len(comments)
            or remark is None
            or (
                not quoted_parent_matches(
                    remark.quote, parent_remarks[position]["text"], body_format
                )
                or request.get("body")
                != imported_mail_text(
                    remark.text,
                    source.message_id,
                    source_url,
                    body_format,
                    source.from_name or source.from_address,
                )
                or request.get("parent_body_sha256")
                != hashlib.sha256(comments[position]["body"].encode()).hexdigest()
                or type(request.get("parent_id")) is not int
                or request["parent_id"] <= 0
            )
        ):
            raise PublicationConflict(
                "Imported reply no longer exactly quotes its retained parent remark"
            )
        remark_position = extract_quoted_remarks(source.body_text).index(remark)
        serialized = json.dumps(asdict(remark), sort_keys=True, separators=(",", ":"))
        if (
            delivery.effect_key
            != f"imported-reply-v1:{remark_position}:{hashlib.sha256(serialized.encode()).hexdigest()}"
        ):
            raise PublicationConflict("Imported reply effect identity changed")
        return
    evidence, comments = payload.get("evidence"), request.get("comments")
    if (
        not isinstance(evidence, dict)
        or evidence.get("parser")
        not in ("quote-blocks-v1", "quote-blocks-segmented-v1")
        or evidence.get("source_sha256") != source.sha256
        or not isinstance(evidence.get("patch_diff"), str)
        or len(evidence["patch_diff"]) > 8 * 1024 * 1024
        or hashlib.sha256(evidence["patch_diff"].encode()).hexdigest()
        != evidence.get("patch_diff_sha256")
        or not isinstance(evidence.get("remarks"), list)
        or not isinstance(comments, list)
        or not comments
        or len(evidence["remarks"]) != len(comments)
        or request.get("body")
        != (
            imported_mail_text(
                "",
                source.message_id,
                source_url,
                body_format,
                source.from_name or source.from_address,
            )
            if body_format
            else (source.subject or "Mail review")
        )
        or type(payload.get("max_inline_lines")) is not int
        or not 1 <= payload["max_inline_lines"] <= 1000
    ):
        raise PublicationConflict("Imported review lacks exact patch/quote evidence")
    patch = ledger.messages_by_id((evidence.get("patch_message_id"),))[0]
    if (
        patch.sha256 != evidence.get("patch_source_sha256")
        or patch.kind != "patch"
        or evidence.get("patch_message_id")
        != payload.get("thread", {}).get("patch_message_id")
    ):
        raise PublicationConflict("Imported review patch source changed")
    index = DiffIndex(evidence["patch_diff"])
    remarks = extract_quoted_remarks(source.body_text)
    segmented = evidence["parser"] == "quote-blocks-segmented-v1"
    if (segmented and len(comments) != 1) or (
        not segmented and delivery.effect_key != "imported-review-v1"
    ):
        raise PublicationConflict("Imported review effect identity is invalid")
    for retained, comment in zip(evidence["remarks"], comments, strict=True):
        remark = next(
            (
                item
                for item in remarks
                if json.loads(json.dumps(asdict(item))) == retained
            ),
            None,
        )
        anchor = (
            index.resolve_quote(remark, max_lines=payload["max_inline_lines"])
            if remark is not None
            else None
        )
        if segmented and remark is not None:
            if anchor is not None or delivery.effect_key != segmented_review_effect_key(
                remark, remarks.index(remark)
            ):
                raise PublicationConflict(
                    "Imported segmented review effect identity changed"
                )
            anchor = index.resolve_segmented_quote(
                remark, max_lines=payload["max_inline_lines"]
            )
        if (
            anchor is None
            or not isinstance(comment, dict)
            or any(
                type(comment.get(field)) is not int
                for field in ("old_position", "new_position", "extra_lines_count")
            )
            or any(
                comment.get(field) != value
                for field, value in {
                    "path": anchor.path,
                    "old_position": anchor.old_position,
                    "new_position": anchor.new_position,
                    "extra_lines_count": anchor.extra_lines_count,
                    "body": imported_mail_text(
                        remark.text,
                        source.message_id,
                        source_url,
                        body_format,
                        source.from_name or source.from_address,
                    ),
                }.items()
            )
        ):
            raise PublicationConflict(
                "Imported range no longer resolves exactly from the retained source"
            )
        if any(
            not isinstance(comment.get(field), str)
            or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", comment[field]) is None
            for field in ("before_commit", "after_commit")
        ):
            raise PublicationConflict(
                "Imported review requires full exact patch comparison commits"
            )


def plan_import_delivery(
    ledger: Ledger,
    client: ForgeImportClient,
    pull_index: int,
    event_type: str,
    *,
    limit: int = 100,
    retry_uncertain: bool = False,
    delivery_ids: tuple[int, ...] | None = None,
) -> ImportDeliveryPlan:
    if event_type not in ("IMPORT_CONVERSATION", "IMPORT_REVIEW", "IMPORT_REPLY"):
        raise ValueError("Unsupported imported event type")
    capabilities = client.capabilities()
    operation = {
        "IMPORT_CONVERSATION": "conversation",
        "IMPORT_REVIEW": "review",
        "IMPORT_REPLY": "review-reply",
    }[event_type]
    if operation not in capabilities["operations"]:
        raise ValueError("Imported operation is not enabled by repository capabilities")
    supported_limit = capabilities.get("max_inline_lines")
    if type(supported_limit) is not int or supported_limit < 0:
        raise ValueError("Repository review range capability is invalid")
    destination = comment_destination(client, pull_index)
    ledger.assert_publication_sources(
        client.api.base_url + client.repository_path, pull_index
    )
    deliveries = tuple(
        ledger.pending(
            limit,
            destination=destination,
            event_type=event_type,
            delivery_ids=delivery_ids,
        )
    )
    ledger.assert_unconflicted(tuple(delivery.message_id for delivery in deliveries))
    _assert_thread_sources(ledger, client, pull_index, deliveries)
    for delivery in deliveries:
        _assert_import_payload(ledger, delivery, source_url=client.source_url)
        presentation = delivery.payload.get("presentation_parent")
        if presentation is not None:
            remote = client.lookup_operation(presentation["operation_key"])
            parent_source = ledger.messages_by_id((presentation["message_id"],))[0]
            if (
                remote is None
                or remote.get("type") != presentation["type"]
                or remote.get("message_id") != parent_source.message_id
                or remote.get("message_sha256") != parent_source.sha256
                or remote.get("comment_id") != presentation["comment_id"]
            ):
                raise PublicationConflict(
                    "Concise reply target differs from its accepted parent"
                )
        if event_type == "IMPORT_REPLY":
            evidence = delivery.payload["evidence"]
            parent = next(
                effect
                for effect in ledger.comment_effects(
                    evidence["parent_message_id"], destination
                )
                if effect.effect_key == evidence["parent_effect_key"]
            )
            remote = client.lookup_operation(evidence["parent_operation_key"])
            parent_source = ledger.messages_by_id((evidence["parent_message_id"],))[0]
            identifiers = (
                (remote or {}).get("code_comment_ids", [])
                if parent.event_type == "IMPORT_REVIEW"
                else [(remote or {}).get("comment_id")]
            )
            position = evidence["parent_position"]
            expected_type = (
                "review" if parent.event_type == "IMPORT_REVIEW" else "review-reply"
            )
            if (
                remote is None
                or remote.get("message_sha256") != parent_source.sha256
                or remote.get("message_id") != parent_source.message_id
                or remote.get("type") != expected_type
                or position >= len(identifiers)
                or type(identifiers[position]) is not int
                or identifiers[position] != delivery.payload["request"]["parent_id"]
            ):
                raise PublicationConflict(
                    "Imported reply target no longer matches the accepted parent operation"
                )
        if event_type == "IMPORT_REVIEW":
            if (
                supported_limit
                and delivery.payload["max_inline_lines"] > supported_limit
            ):
                raise ValueError(
                    "Queued ranges exceed the current repository capability"
                )
            thread, request = delivery.payload["thread"], delivery.payload["request"]
            publication = ledger.publication(
                client.api.base_url + client.repository_path,
                thread["series_key"],
                thread["revision"],
            )
            if request.get("head_commit") != publication.head_commit:
                raise PublicationConflict(
                    "Imported review head differs from its recorded publication"
                )
            if any(
                not isinstance(request.get(field), str)
                or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", request[field]) is None
                for field in ("head_commit", "base_commit", "merge_base")
            ):
                raise PublicationConflict(
                    "Imported review requires a fully pinned PR comparison"
                )
            for after_commit in {
                comment["after_commit"] for comment in request["comments"]
            }:
                snapshot = client.comparison(pull_index, after_commit=after_commit)
                if any(
                    snapshot[field] != request[field]
                    for field in ("head_commit", "base_commit", "merge_base")
                ):
                    raise PublicationConflict(
                        "Imported review comparison changed before approval"
                    )
                if snapshot.get("diff_sha256") != delivery.payload["evidence"][
                    "patch_diff_sha256"
                ] or any(
                    comment["before_commit"] != snapshot["before_commit"]
                    for comment in request["comments"]
                    if comment["after_commit"] == after_commit
                ):
                    raise PublicationConflict(
                        "Imported exact patch comparison changed before approval"
                    )
    return ImportDeliveryPlan(
        destination,
        client.source_url,
        capabilities["repository_id"],
        capabilities["importer_login"],
        capabilities["importer_id"],
        event_type,
        supported_limit,
        retry_uncertain,
        deliveries,
    )


def deliver_approved_imports(
    ledger: Ledger,
    client: ForgeImportClient,
    pull_index: int,
    event_type: str,
    *,
    approved_digest: str,
    limit: int = 100,
    retry_uncertain: bool = False,
    delivery_ids: tuple[int, ...] | None = None,
) -> CommentBatchSummary:
    if (
        not client.write_enabled
        or re.fullmatch(r"[0-9a-f]{64}", approved_digest or "") is None
    ):
        raise ValueError(
            "Imported delivery requires write opt-in and a reviewed approval digest"
        )
    delivered = failed = 0
    with ledger.worker_lock():
        plan = plan_import_delivery(
            ledger,
            client,
            pull_index,
            event_type,
            limit=limit,
            retry_uncertain=retry_uncertain,
            delivery_ids=delivery_ids,
        )
        if not hmac.compare_digest(plan.approval_digest(), approved_digest):
            raise ValueError("Approval digest does not match the current import plan")
        for delivery in plan.deliveries:
            if delivery.write_state in ("sending", "uncertain") and not retry_uncertain:
                ledger.record_error(
                    delivery.id,
                    "Earlier import acceptance is unconfirmed; review idempotent retry first",
                )
                failed += 1
                continue
            ledger.begin_delivery(delivery.id, retry_uncertain=retry_uncertain)
            try:
                method = {
                    "IMPORT_CONVERSATION": client.import_conversation,
                    "IMPORT_REVIEW": client.import_review,
                    "IMPORT_REPLY": client.import_reply,
                }[event_type]
                result = method(pull_index, delivery.payload["request"])
                if result.planned:
                    raise DeliveryUncertainError("Imported delivery was not confirmed")
                ledger.mark_delivered(delivery.id)
                delivered += 1
            except (ForgeAPIError, DeliveryUncertainError, ValueError) as error:
                ledger.mark_delivery_uncertain(delivery.id, str(error))
                failed += 1
    return CommentBatchSummary(delivered=delivered, failed=failed)


@dataclass(frozen=True)
class CommentDeliveryPlan:
    destination: str
    actor: str
    actor_id: int
    event_type: str
    allow_inline_writes: bool
    retry_uncertain: bool
    deliveries: tuple[Delivery, ...]
    max_inline_lines: int = 1

    def approval_digest(self) -> str:
        data = asdict(self)
        if not any(
            delivery.payload.get("payload_version") == 2 for delivery in self.deliveries
        ):
            data.pop("max_inline_lines")
        value = json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")
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
        value = json.dumps(asdict(self), sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
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
    if (
        not isinstance(pull_index, int)
        or isinstance(pull_index, bool)
        or pull_index < 1
    ):
        raise ValueError("pull request index must be a positive integer")
    return f"{publisher.api.base_url}{publisher.repository_path}#{pull_index}"


def plan_legacy_migration(
    ledger: Ledger,
    publisher: ForgeCommentPublisher,
    pull_index: int,
    reason: str,
    *,
    limit: int = 100,
    scoped_delivery_ids: tuple[int, ...] | None = None,
) -> LegacyMigrationPlan:
    if not 1 <= limit <= 1000:
        raise ValueError("Migration limit must be between 1 and 1000")
    if any(
        re.fullmatch(r"[A-Za-z0-9._-]+", name) is None
        for name in (publisher.owner, publisher.repo)
    ):
        raise ValueError("Legacy migration requires simple repository owner and name")
    if (
        not isinstance(reason, str)
        or not reason.strip()
        or len(reason) > 500
        or any(ord(char) < 32 or ord(char) == 127 for char in reason)
    ):
        raise ValueError("Legacy migration requires a short single-line reason")
    destination = comment_destination(publisher, pull_index)
    repository_destination = publisher.api.base_url + publisher.repository_path
    publications = ledger.publications_for_pull(repository_destination, pull_index)
    if len(publications) != 1:
        raise ValueError(
            "Legacy migration requires exactly one recorded completed pull request mapping"
        )
    ledger.assert_publication_sources(repository_destination, pull_index)
    repository = publisher.api.request_json(publisher.repository_path)
    repository_id = repository.get("id") if isinstance(repository, dict) else None
    if (
        not isinstance(repository_id, int)
        or isinstance(repository_id, bool)
        or repository_id < 1
    ):
        raise ValueError("Gitea repository ID is not available")
    pull = publisher.api.request_json(f"{publisher.repository_path}/pulls/{pull_index}")
    head = pull.get("head") if isinstance(pull, dict) else None
    base = pull.get("base") if isinstance(pull, dict) else None
    if (
        not isinstance(pull, dict)
        or pull.get("number") != pull_index
        or not isinstance(head, dict)
        or head.get("ref") != publications[0].revision_branch
        or head.get("sha") != publications[0].head_commit
        or head.get("repo_id") != repository_id
        or not isinstance(base, dict)
        or base.get("repo_id") != repository_id
        or _series_marker(publications[0].series_key) not in str(pull.get("body", ""))
    ):
        raise ValueError("Gitea pull request does not match the recorded publication")
    actor = authenticated_user(publisher.api)
    previous = (
        destination
        if scoped_delivery_ids is not None
        else f"{publisher.owner}/{publisher.repo}#{pull_index}"
    )
    deliveries = tuple(
        ledger.pending(limit, destination=previous, delivery_ids=scoped_delivery_ids)
    )
    if scoped_delivery_ids is not None and (
        {item.id for item in deliveries} != set(scoped_delivery_ids)
        or any("thread" in item.payload for item in deliveries)
    ):
        raise DeliveryStateConflict(
            "Selected scoped deliveries must be pending and lack thread provenance"
        )
    if any(
        item.event_type not in ("ISSUE_COMMENT", "INLINE_COMMENT")
        or item.write_state != "pending"
        or item.attempts != 0
        for item in deliveries
    ):
        raise DeliveryStateConflict(
            "Only untouched pending legacy comment deliveries can be migrated"
        )
    ledger.assert_unconflicted(tuple(item.message_id for item in deliveries))
    publication = publications[0]
    recorded = ledger.series_revision(publication.series_key, publication.revision)
    if not recorded:
        raise PublicationConflict("Legacy destination has no recorded series")
    members = tuple(
        message.message_id for message in recorded.messages if message.message_id
    )
    related = ledger._related_thread_messages(members)
    matches = match_thread_replies((*recorded.messages, *related), (recorded,))
    matched = {match.message.message_id: match for match in matches.matched}
    bindings = []
    for delivery in deliveries:
        match = matched.get(delivery.message_id)
        if not match or (
            delivery.event_type == "INLINE_COMMENT" and match.patch_message_id is None
        ):
            raise DeliveryStateConflict(
                "Legacy delivery does not have a verified reply thread"
            )
        binding = LegacyThreadBinding(
            delivery.id, match.series_key, match.revision, match.patch_message_id
        )
        expected = {
            "series_key": binding.series_key,
            "revision": binding.revision,
            "patch_message_id": binding.patch_message_id,
        }
        if "thread" in delivery.payload and delivery.payload["thread"] != expected:
            raise DeliveryStateConflict(
                "Legacy delivery has different thread provenance"
            )
        bindings.append(binding)
    return LegacyMigrationPlan(
        str(ledger.path.resolve()),
        previous,
        destination,
        repository_id,
        publication.series_key,
        publication.revision,
        publication.revision_branch,
        publication.head_commit,
        actor.login,
        actor.user_id,
        reason.strip(),
        limit,
        deliveries,
        tuple(bindings),
    )


def migrate_legacy_deliveries(
    ledger: Ledger,
    publisher: ForgeCommentPublisher,
    pull_index: int,
    reason: str,
    *,
    approved_digest: str,
    limit: int = 100,
    scoped_delivery_ids: tuple[int, ...] | None = None,
) -> LegacyMigrationResult:
    if (
        not isinstance(approved_digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", approved_digest) is None
    ):
        raise ValueError("A valid legacy migration approval digest is required")
    with ledger.worker_lock():
        plan = plan_legacy_migration(
            ledger,
            publisher,
            pull_index,
            reason,
            limit=limit,
            scoped_delivery_ids=scoped_delivery_ids,
        )
        digest = plan.approval_digest()
        if not hmac.compare_digest(approved_digest, digest):
            raise ValueError(
                "Approval digest does not match the current legacy migration plan"
            )
        migrated = ledger.move_legacy_deliveries(
            plan.deliveries,
            plan.destination,
            {
                binding.delivery_id: {
                    "series_key": binding.series_key,
                    "revision": binding.revision,
                    "patch_message_id": binding.patch_message_id,
                }
                for binding in plan.thread_bindings
            },
            actor=f"{plan.actor} (id {plan.actor_id})",
            reason=plan.reason,
            digest=digest,
        )
        return LegacyMigrationResult(migrated, digest)


def _attributed_delivery(delivery: Delivery, login: str) -> Delivery:
    body = delivery.payload.get("body")
    if not isinstance(body, str) or not body.strip():
        return delivery
    return replace(
        delivery,
        payload={**delivery.payload, "body": with_tool_attribution(body, login)},
    )


def _publish_comment(
    publisher: ForgeCommentPublisher,
    pull_index: int,
    delivery: Delivery,
) -> DeliveryResult:
    body = delivery.payload.get("body")
    if not isinstance(body, str) or not body.strip():
        raise ValueError("queued comment body must be a non-empty string")
    version = delivery.payload.get("payload_version", 1)
    if (
        not isinstance(version, int)
        or isinstance(version, bool)
        or version not in (1, 2)
    ):
        raise ValueError("Unsupported comment payload version")
    arguments = {
        "source_message_id": delivery.message_id,
        "effect_key": delivery.effect_key,
        "body": body,
    }
    if delivery.event_type == "ISSUE_COMMENT":
        return publisher.ensure_issue_comment(pull_index, **arguments)
    if delivery.event_type != "INLINE_COMMENT":
        raise ValueError("Unsupported comment event type")
    anchor_data = delivery.payload.get("anchor")
    head_commit = delivery.payload.get("head_commit")
    fields = {"path", "old_position", "new_position"}
    if version == 2:
        fields.add("extra_lines_count")
    if not isinstance(anchor_data, dict) or set(anchor_data) != fields:
        raise ValueError("queued inline comment must have one complete anchor")
    if version == 2:
        if not isinstance(delivery.payload.get("base_commit"), str) or not isinstance(
            delivery.payload.get("merge_base"), str
        ):
            raise ValueError(
                "Versioned ranges require the complete reviewed comparison"
            )
        return publisher.ensure_inline_comment(
            pull_index,
            RangeAnchor(**anchor_data),
            head_commit=head_commit,
            base_commit=delivery.payload.get("base_commit"),
            merge_base=delivery.payload.get("merge_base"),
            **arguments,
        )
    anchor = Anchor(**anchor_data)
    return publisher.ensure_inline_comment(
        pull_index, anchor, head_commit=head_commit, **arguments
    )


def _assert_thread_sources(
    ledger: Ledger,
    publisher: ForgeCommentPublisher,
    pull_index: int,
    deliveries: tuple[Delivery, ...],
) -> None:
    destination = publisher.api.base_url + publisher.repository_path
    cached = {}
    sources = []
    publications = ledger.publications_for_pull(destination, pull_index)
    for delivery in deliveries:
        if delivery.payload.get("payload_version") == 2:
            evidence = delivery.payload.get("evidence")
            source = ledger.messages_by_id((delivery.message_id,))[0]
            if (
                not isinstance(evidence, dict)
                or evidence.get("parser") != "quote-blocks-v1"
                or evidence.get("source_sha256") != source.sha256
                or not isinstance(evidence.get("quote"), list)
                or not evidence["quote"]
                or any(not isinstance(line, str) for line in evidence["quote"])
            ):
                raise PublicationConflict(
                    "Queued range lacks matching source and parser evidence"
                )
        thread = delivery.payload.get("thread")
        if thread is None:
            if (
                publications
                and ledger.messages_by_id((delivery.message_id,))[0].kind == "reply"
            ):
                raise PublicationConflict(
                    "Queued reply lacks verified thread provenance; inspect before delivering"
                )
            continue
        if (
            not isinstance(thread, dict)
            or set(thread) != {"series_key", "revision", "patch_message_id"}
            or not isinstance(thread["series_key"], str)
            or not isinstance(thread["revision"], int)
            or isinstance(thread["revision"], bool)
            or (
                thread["patch_message_id"] is not None
                and not isinstance(thread["patch_message_id"], str)
            )
        ):
            raise PublicationConflict("Queued reply has invalid thread provenance")
        key = thread["series_key"], thread["revision"]
        if key not in cached:
            recorded = ledger.series_revision(*key)
            publication = ledger.publication(destination, *key)
            if (
                not recorded
                or not publication
                or publication.status != "complete"
                or publication.pull_index != pull_index
            ):
                raise PublicationConflict(
                    "Queued reply no longer matches its recorded publication"
                )
            members = tuple(
                message.message_id
                for message in recorded.messages
                if message.message_id
            )
            related = ledger._related_thread_messages(members)
            matches = match_thread_replies((*recorded.messages, *related), (recorded,))
            cached[key] = {match.message.message_id: match for match in matches.matched}
        match = cached[key].get(delivery.message_id)
        if not match or match.patch_message_id != thread["patch_message_id"]:
            raise PublicationConflict(
                "Queued reply thread ancestry changed; inspect before delivering"
            )
        sources.append(match.message)
    ledger.assert_message_ancestry_available(tuple(sources))


def _deliver_comments(
    ledger: Ledger,
    publisher: ForgeCommentPublisher,
    pull_index: int,
    event_type: str,
    *,
    limit: int,
    retry_uncertain: bool,
    selected: tuple[Delivery, ...] | None = None,
    lock_held: bool = False,
) -> CommentBatchSummary:
    destination = comment_destination(publisher, pull_index)
    if retry_uncertain and not publisher.write_enabled:
        raise ValueError("Retrying uncertain comments requires writes to be enabled")
    lock = (
        ledger.worker_lock()
        if publisher.write_enabled and not lock_held
        else nullcontext()
    )
    delivered = planned = failed = 0
    with lock:
        ledger.assert_publication_sources(
            publisher.api.base_url + publisher.repository_path, pull_index
        )
        pending = (
            selected
            if selected is not None
            else ledger.pending(limit, destination=destination, event_type=event_type)
        )
        _assert_thread_sources(ledger, publisher, pull_index, tuple(pending))
        if selected is None and pending:
            actor = authenticated_user(publisher.api)
            pending = tuple(
                _attributed_delivery(delivery, actor.login) for delivery in pending
            )
        preview = ForgeCommentPublisher(
            publisher.api,
            publisher.owner,
            publisher.repo,
            max_inline_lines=publisher.max_inline_lines,
        )
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
                    ledger.record_error(
                        delivery.id,
                        "Earlier comment write is unconfirmed; inspect before retrying",
                    )
                    failed += 1
                    continue

            body = delivery.payload.get("body")
            if not isinstance(body, str) or not body.strip():
                ledger.record_attempt(delivery.id)
                ledger.record_error(
                    delivery.id, "queued comment body must be a non-empty string"
                )
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
    ledger: Ledger,
    publisher: ForgeCommentPublisher,
    pull_index: int,
    event_type: str,
    *,
    limit: int = 100,
    retry_uncertain: bool = False,
) -> CommentDeliveryPlan:
    if event_type not in ("ISSUE_COMMENT", "INLINE_COMMENT"):
        raise ValueError("Unsupported comment event type")
    if not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000")
    destination = comment_destination(publisher, pull_index)
    ledger.assert_publication_sources(
        publisher.api.base_url + publisher.repository_path, pull_index
    )
    actor = authenticated_user(publisher.api)
    deliveries = tuple(
        ledger.pending(limit, destination=destination, event_type=event_type)
    )
    ledger.assert_unconflicted(tuple(delivery.message_id for delivery in deliveries))
    _assert_thread_sources(ledger, publisher, pull_index, deliveries)
    deliveries = tuple(
        _attributed_delivery(delivery, actor.login) for delivery in deliveries
    )
    return CommentDeliveryPlan(
        destination,
        actor.login,
        actor.user_id,
        event_type,
        publisher.allow_inline_writes,
        retry_uncertain,
        deliveries,
        publisher.max_inline_lines,
    )


def deliver_approved_comments(
    ledger: Ledger,
    publisher: ForgeCommentPublisher,
    pull_index: int,
    event_type: str,
    *,
    approved_digest: str,
    limit: int = 100,
    retry_uncertain: bool = False,
) -> CommentBatchSummary:
    if not publisher.write_enabled:
        raise ValueError("Approved comment delivery requires writes to be enabled")
    if (
        not isinstance(approved_digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", approved_digest) is None
    ):
        raise ValueError("A valid comment approval digest is required")
    with ledger.worker_lock():
        plan = plan_comment_delivery(
            ledger,
            publisher,
            pull_index,
            event_type,
            limit=limit,
            retry_uncertain=retry_uncertain,
        )
        if not hmac.compare_digest(approved_digest, plan.approval_digest()):
            raise ValueError("Approval digest does not match the current comment plan")
        return _deliver_comments(
            ledger,
            publisher,
            pull_index,
            event_type,
            limit=limit,
            retry_uncertain=retry_uncertain,
            selected=plan.deliveries,
            lock_held=True,
        )


def deliver_issue_comments(
    ledger: Ledger,
    publisher: ForgeCommentPublisher,
    pull_index: int,
    *,
    limit: int = 100,
    retry_uncertain: bool = False,
) -> CommentBatchSummary:
    """Deliver or preview one PR's queued conversation comments."""
    return _deliver_comments(
        ledger,
        publisher,
        pull_index,
        "ISSUE_COMMENT",
        limit=limit,
        retry_uncertain=retry_uncertain,
    )


def deliver_inline_comments(
    ledger: Ledger,
    publisher: ForgeCommentPublisher,
    pull_index: int,
    *,
    limit: int = 100,
    retry_uncertain: bool = False,
) -> CommentBatchSummary:
    """Deliver pinned inline comments, falling back when the PR head changed."""
    return _deliver_comments(
        ledger,
        publisher,
        pull_index,
        "INLINE_COMMENT",
        limit=limit,
        retry_uncertain=retry_uncertain,
    )
