# SPDX-License-Identifier: GPL-2.0-or-later

"""Bounded import planning against a private ledger snapshot."""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path

from lore_review_companion.b4_adapter import B4ReviewError
from lore_review_companion.forge_api import ForgeAPIError
from lore_review_companion.forge_imports import ForgeImportClient
from lore_review_companion.git_branches import GitHttpCredentials
from lore_review_companion.ledger import (
    ArchiveSafetyBlocked,
    Ledger,
    MessageConflictError,
    NNTPQueueStatus,
    PublicationConflict,
)
from lore_review_companion.nntp_source import (
    DEFAULT_GROUP,
    DEFAULT_HOST,
    DEFAULT_LOOKBACK_HOURS,
    NNTPSourceError,
    discover_nntp,
    discover_nntp_numbers,
    discover_nntp_thread,
    fetch_nntp,
    nntp_source,
)
from lore_review_companion.outbox import ImportDeliveryPlan, plan_import_delivery
from lore_review_companion.preflight import GitPreflightError
from lore_review_companion.public_inbox import PublicInboxError
from lore_review_companion.review_queue import ReplyBatchSummary, queue_imported_replies
from lore_review_companion.series_sync import (
    ImportedSeriesResult,
    sync_imported_series_revision,
)


@dataclass(frozen=True)
class RevisionSyncPreview:
    series_key: str
    revision: int
    status: str
    diagnostics: tuple[str, ...] = ()
    publication: ImportedSeriesResult | None = None
    reply_queue: ReplyBatchSummary | None = None
    deliveries: tuple[ImportDeliveryPlan, ...] = ()


@dataclass(frozen=True)
class ImportedSyncPreview:
    source: str
    source_refresh: tuple[object, ...]
    queue: NNTPQueueStatus | None
    revisions: tuple[RevisionSyncPreview, ...]
    diagnostics: tuple[str, ...]
    remote_writes: bool = False
    ledger_writes: bool = False


def plan_imported_sync(
    ledger: Ledger,
    client: ForgeImportClient,
    repository: Path,
    remote: str,
    *,
    series_seed: str,
    target_branch: str,
    credentials: GitHttpCredentials,
    base_ref: str | None = None,
    series_key: str | None = None,
    revision: int | None = None,
    host: str = DEFAULT_HOST,
    group: str = DEFAULT_GROUP,
    lookback_hours: int = DEFAULT_LOOKBACK_HOURS,
    refresh_source: bool = False,
    recover_thread: bool = False,
    message_limit: int = 100,
    revision_limit: int = 8,
    max_inline_lines: int = 50,
) -> ImportedSyncPreview:
    if client.write_enabled:
        raise ValueError("The sync coordinator accepts only a read-only import client")
    if type(message_limit) is not int or not 1 <= message_limit <= 1000:
        raise ValueError("Sync message limit must be between 1 and 1000")
    if type(revision_limit) is not int or not 1 <= revision_limit <= 32:
        raise ValueError("Sync revision limit must be between 1 and 32")
    if revision is not None and (type(revision) is not int or revision < 1):
        raise ValueError("Select a positive series revision")
    source = nntp_source(host, group)
    destination = client.api.base_url + client.repository_path
    with tempfile.TemporaryDirectory(prefix="lore-sync-preview-") as temporary:
        working = ledger.snapshot(Path(temporary) / "ledger.sqlite3")
        refresh = []
        if refresh_source:
            refresh.append(
                discover_nntp(working, host=host, group=group, hours=lookback_hours)
            )
            refresh.append(
                discover_nntp_numbers(
                    working, host=host, group=group, limit=message_limit
                )
            )
            refresh.append(working.repair_nntp_parents(source, limit=message_limit))
            refresh.append(
                fetch_nntp(working, host=host, group=group, limit=message_limit)
            )
        if recover_thread:
            refresh.append(
                discover_nntp_thread(
                    working,
                    series_seed,
                    inbox_url=client.source_url,
                    host=host,
                    group=group,
                )
            )
            refresh.append(
                fetch_nntp(working, host=host, group=group, limit=message_limit)
            )
        diagnostics = []
        try:
            revisions = tuple(
                item
                for item in working.discover_related_series(series_seed)
                if (series_key is None or item.series_key == series_key)
                and (revision is None or item.revision == revision)
            )
        except (ValueError, ArchiveSafetyBlocked, MessageConflictError) as error:
            revisions = ()
            diagnostics.append(
                f"Series selection blocked: {type(error).__name__}: {error}"
            )
        if not revisions:
            diagnostics.append(
                "No retained series matches this selection; fetch or recover its source dependencies"
            )
        if len(revisions) > revision_limit:
            raise ValueError(
                "Related revisions exceed the sync limit; select a series/revision explicitly"
            )
        previews = []
        ambiguous = len({item.series_key for item in revisions}) > 1
        for current in revisions:
            if ambiguous:
                previews.append(
                    RevisionSyncPreview(
                        current.series_key,
                        current.revision,
                        "ambiguous",
                        (
                            "Related mail identifies multiple series; select one series key",
                        ),
                    )
                )
                continue
            if not current.complete or current.conflicts:
                previews.append(
                    RevisionSyncPreview(
                        current.series_key,
                        current.revision,
                        "incomplete",
                        current.conflicts or ("Retained patch set is incomplete",),
                    )
                )
                continue
            publication = working.publication(
                destination, current.series_key, current.revision
            )
            if publication is not None and publication.status != "complete":
                previews.append(
                    RevisionSyncPreview(
                        current.series_key,
                        current.revision,
                        "publication_pending",
                        (
                            f"Publication state {publication.status} needs explicit reconciliation",
                        ),
                    )
                )
                continue
            selected_base = publication.base_commit if publication else base_ref
            if not selected_base:
                previews.append(
                    RevisionSyncPreview(
                        current.series_key,
                        current.revision,
                        "unknown_base",
                        (
                            "Select an explicit trusted base before preflight/publication",
                        ),
                    )
                )
                continue
            try:
                working.record_series_revision(current)
                planned = sync_imported_series_revision(
                    current,
                    repository,
                    selected_base,
                    remote,
                    client,
                    target_branch=target_branch,
                    ledger=working,
                    credentials=credentials,
                )
                if publication is not None and (
                    planned.refs.head.planned or planned.refs.base.planned
                ):
                    raise PublicationConflict(
                        "Published immutable revision refs are missing"
                    )
                if publication is None:
                    previews.append(
                        RevisionSyncPreview(
                            current.series_key,
                            current.revision,
                            "publication_ready",
                            publication=planned,
                            diagnostics=(
                                "Reply delivery waits for a confirmed PR publication",
                            ),
                        )
                    )
                    continue
                members = tuple(
                    item.message_id for item in current.messages if item.message_id
                )
                replies = tuple(
                    item
                    for item in working._related_thread_messages(members)
                    if item.kind == "reply"
                )
                if len(replies) > message_limit:
                    raise ValueError(
                        "Related replies exceed the sync message limit; queue explicit message batches"
                    )
                queued = (
                    queue_imported_replies(
                        working,
                        client,
                        publication.pull_index,
                        current.series_key,
                        current.revision,
                        repository,
                        replies,
                        max_inline_lines=max_inline_lines,
                    )
                    if replies
                    else None
                )
                batches = tuple(
                    plan_import_delivery(
                        working,
                        client,
                        publication.pull_index,
                        kind,
                        limit=message_limit,
                    )
                    for kind in ("IMPORT_CONVERSATION", "IMPORT_REVIEW", "IMPORT_REPLY")
                )
                pending = any(batch.deliveries for batch in batches)
                previews.append(
                    RevisionSyncPreview(
                        current.series_key,
                        current.revision,
                        "delivery_ready" if pending else "up_to_date",
                        publication=planned,
                        reply_queue=queued,
                        deliveries=batches,
                        diagnostics=queued.diagnostics if queued else (),
                    )
                )
            except (
                ValueError,
                ArchiveSafetyBlocked,
                MessageConflictError,
                PublicationConflict,
                B4ReviewError,
                ForgeAPIError,
                GitPreflightError,
                NNTPSourceError,
                PublicInboxError,
            ) as error:
                previews.append(
                    RevisionSyncPreview(
                        current.series_key,
                        current.revision,
                        "blocked",
                        (f"{type(error).__name__}: {error}",),
                    )
                )
        queue = (
            working.nntp_queue_status(source, limit=message_limit)
            if working.nntp_checkpoint(source)
            else None
        )
        if queue is None:
            diagnostics.append("NNTP discovery is not initialized for this source")
        return ImportedSyncPreview(
            source, tuple(refresh), queue, tuple(previews), tuple(diagnostics)
        )
