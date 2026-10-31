"""Digest-bound, non-destructive rebuilds of retained imported discussion."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path

from lore_review_companion.forge_comments import (
    format_direct_submission_reply,
    format_nested_reply,
    imported_mail_text,
    submission_text,
)
from lore_review_companion.forge_imports import ForgeImportClient
from lore_review_companion.forge_comments import DeliveryUncertainError
from lore_review_companion.ledger import Ledger, PublicationConflict
from lore_review_companion.outbox import _assert_import_payload
from lore_review_companion.series import SeriesRevision


def refresh_digest(plan: dict) -> str:
    return hashlib.sha256(
        json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def plan_import_refresh(
    ledger: Ledger,
    client: ForgeImportClient,
    pull_index: int,
    revision: SeriesRevision,
    original_pull: dict,
    *,
    reason: str,
) -> dict:
    if not reason.strip() or len(reason) > 1000:
        raise ValueError("A bounded refresh reason is required")
    if "pull-refresh" not in client.capabilities()["operations"]:
        raise ValueError(
            "Server does not support safe in-place refresh; replacement is not authorized"
        )
    snapshot = client.api.request_json(f"{client.path}/pulls/{pull_index}/refresh")
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("sha256"), str):
        raise PublicationConflict("Invalid refresh snapshot")
    destination = client.api.base_url + client.repository_path
    publication = ledger.publication(
        destination, revision.series_key, revision.revision
    )
    if (
        publication is None
        or publication.status != "complete"
        or publication.pull_index != pull_index
    ):
        raise PublicationConflict(
            "Refresh requires the exact retained complete publication"
        )
    ledger.assert_publication_sources(destination, pull_index)
    entries = snapshot.get("imports") or []
    comments = {comment["ID"]: comment for comment in snapshot.get("comments") or []}
    sources = {message.message_id: message.sha256 for message in revision.messages}
    ledger.assert_unconflicted(tuple(sources))
    related = tuple(
        message
        for message in ledger._related_thread_messages(tuple(sources))
        if message.kind == "reply"
    )
    accepted_messages = {
        entry["MessageID"] for entry in entries if entry["Type"] == "conversation"
    }
    if any(message.message_id not in accepted_messages for message in related):
        raise PublicationConflict(
            "Retained replies are not imported yet; complete autonomous reply import before refreshing"
        )
    effects = []
    replacements = []
    owned = set()
    for entry in entries:
        source = ledger.messages_by_id((entry["MessageID"],))[0]
        ledger.assert_unconflicted((source.message_id,))
        if source.sha256 != entry["MessageSHA256"]:
            raise PublicationConflict("Server source bytes differ from retained mail")
        sources[source.message_id] = source.sha256
        if entry["Type"] == "pull":
            if entry["OperationKey"] != original_pull["operation_key"]:
                raise PublicationConflict(
                    "Refresh root does not match retained publication"
                )
            continue
        retained = [
            effect
            for effect in ledger.comment_effects(
                source.message_id, f"{destination}#{pull_index}"
            )
            if effect.payload.get("request", {}).get("operation_key")
            == entry["OperationKey"]
            and effect.write_state == "complete"
        ]
        if len(retained) != 1:
            raise PublicationConflict(
                "Every accepted operation must have one complete retained effect"
            )
        effect = retained[0]
        _assert_import_payload(ledger, effect, source_url=client.source_url)
        request = effect.payload["request"]
        effects.append({"type": entry["Type"], "request": request})
        body = request["body"]
        evidence = effect.payload.get("evidence", {})
        if entry["Type"] == "review-reply":
            body = imported_mail_text(
                evidence["remark"]["text"],
                source.message_id,
                client.source_url,
                "mail-text-v6",
                source.from_name or source.from_address,
            )
        if entry["Type"] == "conversation":
            parent = effect.payload.get("presentation_parent")
            text = (
                format_nested_reply(source.body_text)
                if parent
                else format_direct_submission_reply(
                    source,
                    revision.messages,
                    quote_patch_code=True,
                    diff_quote_as_c=True,
                )
            )
            body = imported_mail_text(
                text,
                source.message_id,
                client.source_url,
                "mail-text-v6",
                source.from_name or source.from_address,
            )
            if parent:
                body += f'\n\n<small><a href="{parent["url"]}">In reply to the preceding review comment</a></small>'
        identifiers = [entry["CommentID"], *(entry.get("CodeCommentIDs") or [])]
        inline_bodies = [
            imported_mail_text(
                remark["text"],
                source.message_id,
                client.source_url,
                "mail-text-v6",
                source.from_name or source.from_address,
            )
            for remark in evidence.get("remarks", [])
        ]
        bodies = [body, *inline_bodies]
        if len(identifiers) != len(bodies):
            raise PublicationConflict("Refresh inline mappings are incomplete")
        for identifier, rebuilt in zip(identifiers, bodies, strict=True):
            if identifier not in comments:
                raise PublicationConflict("An imported comment is missing")
            owned.add(identifier)
            replacements.append(
                {
                    "operation_key": entry["OperationKey"],
                    "comment_id": identifier,
                    "body": rebuilt,
                }
            )
    source = ledger.messages_by_id((original_pull["message_id"],))[0]
    body = submission_text(source.body_text)
    earlier = ledger.list_publications(
        destination=destination,
        series_key=revision.series_key,
        status="complete",
        limit=1000,
    )
    if earlier.next_cursor is not None:
        raise PublicationConflict("Revision links exceed the reviewed refresh bound")
    site = client.api.base_url.rstrip("/").removesuffix("/api/v1")
    for item in earlier.publications:
        previous = item.publication
        if previous.revision < revision.revision:
            body += f"\n\nEarlier revision: [v{previous.revision}]({site}/{client.owner}/{client.repo}/pulls/{previous.pull_index})"
    request = {
        "expected_sha256": snapshot["sha256"],
        "reason": reason,
        "pull": original_pull,
        "effects": effects,
        "title": f"{source.from_name or source.from_address}: {revision.title} [v{revision.revision}]",
        "body": body,
        "comments": replacements,
    }
    request["correction_key"] = refresh_digest(request)
    return {
        "version": 1,
        "destination": destination,
        "source_url": client.source_url,
        "pull_index": pull_index,
        "source_sha256": sources,
        "preserved_comment_ids": sorted(set(comments) - owned),
        "removed_comment_ids": [],
        "snapshot": snapshot,
        "request": request,
    }


def apply_import_refresh(
    ledger: Ledger, client: ForgeImportClient, plan: dict, *, approved_digest: str
) -> dict:
    if not client.write_enabled or not hmac.compare_digest(
        refresh_digest(plan), approved_digest
    ):
        raise ValueError(
            "Refresh requires write opt-in and the exact retained plan digest"
        )
    if (
        plan.get("version") != 1
        or plan.get("destination") != client.api.base_url + client.repository_path
        or plan.get("source_url") != client.source_url
    ):
        raise PublicationConflict(
            "Refresh plan destination differs from the selected client"
        )
    ledger.assert_unconflicted(tuple(plan["source_sha256"]))
    for source in ledger.messages_by_id(tuple(plan["source_sha256"])):
        if source.sha256 != plan["source_sha256"][source.message_id]:
            raise PublicationConflict("Refresh source evidence changed after approval")
    if "pull-refresh" not in client.capabilities()["operations"]:
        raise ValueError("Safe in-place refresh is not supported")
    with ledger.worker_lock():
        result = client.api.request_json(
            f'{client.path}/pulls/{plan["pull_index"]}/refresh',
            method="POST",
            payload=plan["request"],
            companion_revision=client.companion_revision,
        )
        if (
            not isinstance(result, dict)
            or type(result.get("id")) is not int
            or result["id"] <= 0
            or result.get("pull_index") != plan["pull_index"]
            or result.get("operation_key") != plan["request"]["pull"]["operation_key"]
        ):
            raise DeliveryUncertainError(
                "Refresh acceptance is uncertain; retry the same retained plan"
            )
        return result


def save_refresh_plan(path: Path, plan: dict) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as output:
        json.dump(plan, output, indent=2, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
