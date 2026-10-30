"""Ingest a bounded mbox into the local message ledger."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from typing import Iterable, Iterator

from lore_review_companion.ledger import ArchiveCheckpointConflict, ArchiveDiscontinuity, Ledger, LedgerStatus, MessageConflictError
from lore_review_companion.mail_parser import ParsedMessage, parse_mbox, parse_message
from lore_review_companion.public_inbox import ArchiveCheckpointUnavailable, ArchiveDeletionError, PublicInboxError, fetch_public_inbox, read_public_inbox, read_public_inbox_backfill, verify_archive_continuity
from lore_review_companion.series import discover_series


@dataclass(frozen=True)
class IngestSummary:
    ingested: int = 0
    replayed: int = 0
    unmatched: int = 0
    conflicts: int = 0
    revisions_discovered: int = 0
    complete_revisions: int = 0


@dataclass(frozen=True)
class ArchiveIngestSummary:
    head: str
    history_incomplete: bool
    messages: IngestSummary
    has_more: bool = False


@dataclass(frozen=True)
class ArchiveBackfillSummary:
    head: str
    messages: IngestSummary
    has_more: bool
    needs_deepen: bool
    complete: bool


@dataclass(frozen=True)
class EpochArchive:
    number: int
    path: Path


@dataclass(frozen=True)
class EpochResult:
    number: int
    source: str
    summary: ArchiveIngestSummary | ArchiveBackfillSummary


@dataclass(frozen=True)
class EpochRoundSummary:
    results: tuple[EpochResult, ...]
    missing: tuple[int, ...]
    deferred: tuple[int, ...]


@dataclass(frozen=True)
class EpochCycle:
    started_at: str
    forward: EpochRoundSummary
    backfill: EpochRoundSummary | None


@dataclass(frozen=True)
class EpochHealth:
    number: int
    source: str
    checkpoint: str | None
    local_head: str | None
    forward: str
    backfill: str


@dataclass(frozen=True)
class HealthAlert:
    code: str
    epoch: int | None = None


@dataclass(frozen=True)
class EpochHealthSummary:
    healthy: bool
    scope: str
    ref: str
    epochs: tuple[EpochHealth, ...]
    alerts: tuple[HealthAlert, ...]
    ledger: LedgerStatus


@dataclass(frozen=True)
class ArchiveDiscontinuityPlan:
    ledger_path: str
    incident: ArchiveDiscontinuity
    ref: str
    current_head: str
    actor: str
    reason: str

    def approval_digest(self) -> str:
        value = json.dumps(asdict(self), sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(value).hexdigest()


def plan_archive_discontinuity_resolution(
    path: Path, ledger: Ledger, checkpoint: str, incident_head: str, actor: str, reason: str, *, ref: str = "HEAD",
) -> ArchiveDiscontinuityPlan:
    if any(
        not isinstance(value, str) or not value.strip() or len(value) > 500
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        for value in (actor, reason)
    ):
        raise ValueError("Archive resolution requires an actor and a short, single-line reason")
    source = str(path.resolve(strict=True))
    incident = next((
        entry for entry in ledger.archive_discontinuities()
        if (entry.source, entry.checkpoint, entry.head) == (source, checkpoint, incident_head)
    ), None)
    if incident is None:
        raise ValueError("No matching archive discontinuity exists")
    head = verify_archive_continuity(path, incident.checkpoint, incident.head, ref)
    if ledger.archive_checkpoint(source) != head:
        raise ArchiveCheckpointConflict("Forward ingestion must reach the verified archive head")
    return ArchiveDiscontinuityPlan(str(ledger.path.resolve()), incident, ref, head, actor.strip(), reason.strip())


def resolve_archive_discontinuity(
    path: Path, ledger: Ledger, checkpoint: str, incident_head: str, actor: str, reason: str, *,
    approved_digest: str, ref: str = "HEAD",
) -> ArchiveDiscontinuityPlan:
    if not isinstance(approved_digest, str) or re.fullmatch(r"[0-9a-f]{64}", approved_digest) is None:
        raise ValueError("A valid archive resolution approval digest is required")
    with ledger.worker_lock():
        plan = plan_archive_discontinuity_resolution(path, ledger, checkpoint, incident_head, actor, reason, ref=ref)
        digest = plan.approval_digest()
        if not hmac.compare_digest(approved_digest, digest):
            raise ValueError("Approval digest does not match the current archive resolution plan")
        ledger.resolve_archive_discontinuity(
            plan.incident, plan.current_head, ref=plan.ref, actor=plan.actor, reason=plan.reason, digest=digest,
        )
        return plan


def _validate_epochs(epochs: tuple[EpochArchive, ...]) -> tuple[EpochArchive, ...]:
    if not epochs or len(epochs) > 1000:
        raise ValueError("Configure between 1 and 1000 archive epochs")
    if any(not isinstance(epoch.number, int) or isinstance(epoch.number, bool) or not 0 <= epoch.number <= 10000 for epoch in epochs):
        raise ValueError("Archive epoch numbers must be integers between 0 and 10000")
    resolved = tuple(EpochArchive(epoch.number, epoch.path.resolve(strict=True)) for epoch in epochs)
    if len({epoch.number for epoch in resolved}) != len(resolved) or len({epoch.path for epoch in resolved}) != len(resolved):
        raise ValueError("Archive epoch numbers and paths must be unique")
    return tuple(sorted(resolved, key=lambda epoch: epoch.number))


def check_epoch_health(epochs: tuple[EpochArchive, ...], ledger: Ledger, *, ref: str = "HEAD") -> EpochHealthSummary:
    if ref != "HEAD" and re.fullmatch(r"refs/(?:heads|remotes)/[A-Za-z0-9._/-]+", ref) is None:
        raise ValueError("Health checks require HEAD or a named branch ref")
    ordered = _validate_epochs(epochs)
    status = ledger.status()
    alerts = []
    for code, active in (
        ("unmatched_messages", status.unmatched_messages),
        ("message_conflicts", status.conflicted_message_ids),
        ("incomplete_link_index", not status.link_index_complete),
        ("archive_deletions", status.archive_deletions),
        ("archive_discontinuities", status.archive_discontinuities),
        ("nntp_incidents", status.nntp_incidents),
        ("incomplete_publications", sum(status.publications.get(state, 0) for state in ("prepared", "branch_ready", "pr_pending"))),
        ("uncertain_publications", status.publications.get("uncertain", 0)),
        ("pending_deliveries", status.deliveries.get("pending", 0)),
        ("uncertain_deliveries", status.deliveries.get("uncertain", 0)),
        ("sending_deliveries", status.deliveries.get("sending", 0)),
    ):
        if active:
            alerts.append(HealthAlert(code))
    numbers = {epoch.number for epoch in ordered}
    alerts.extend(HealthAlert("missing_epoch", number) for number in range(ordered[-1].number + 1) if number not in numbers)
    results = []
    for epoch in ordered:
        source = str(epoch.path)
        checkpoint = ledger.archive_checkpoint(source)
        backfill_checkpoint = ledger.backfill_checkpoint(source)
        backfill = "not_started" if backfill_checkpoint is None else "complete" if backfill_checkpoint.complete else "incomplete"
        head = None
        if checkpoint is None:
            forward = "not_started"
        else:
            try:
                head = verify_archive_continuity(epoch.path, checkpoint, checkpoint, ref)
                forward = "caught_up" if head == checkpoint else "lagging"
            except PublicInboxError:
                forward = "unverified"
        if backfill == "complete":
            try:
                backfill_head = verify_archive_continuity(epoch.path, backfill_checkpoint.commit_id, backfill_checkpoint.commit_id, ref)
                if head is None or backfill_head != head:
                    backfill = "unverified"
            except PublicInboxError:
                backfill = "unverified"
        if forward != "caught_up":
            alerts.append(HealthAlert(f"forward_{forward}", epoch.number))
        if backfill != "complete":
            alerts.append(HealthAlert(f"backfill_{backfill}", epoch.number))
        results.append(EpochHealth(epoch.number, source, checkpoint, head, forward, backfill))
    return EpochHealthSummary(
        not alerts, "Local configured epochs only; remote freshness and watcher liveness are not checked",
        ref, tuple(results), tuple(alerts), status,
    )


def sync_epoch_round(
    epochs: tuple[EpochArchive, ...], ledger: Ledger, *, limit: int = 128,
    fetch: bool = False, allow_incomplete_bootstrap: bool = False,
) -> EpochRoundSummary:
    """Poll explicitly configured epochs with one shared bounded message budget."""
    ordered = _validate_epochs(epochs)
    if not 1 <= limit <= 1000:
        raise ValueError("Epoch round limit must be between 1 and 1000")
    remaining = limit
    results = []
    for epoch in ordered:
        if remaining == 0:
            break
        summary = sync_public_inbox(
            epoch.path, ledger, limit=remaining, fetch=fetch,
            allow_incomplete_bootstrap=allow_incomplete_bootstrap,
        )
        results.append(EpochResult(epoch.number, str(epoch.path), summary))
        remaining -= summary.messages.ingested + summary.messages.replayed + summary.messages.unmatched + summary.messages.conflicts
    numbers = {epoch.number for epoch in ordered}
    return EpochRoundSummary(
        tuple(results), tuple(number for number in range(ordered[-1].number + 1) if number not in numbers),
        tuple(epoch.number for epoch in ordered[len(results):]),
    )


def backfill_epoch_round(
    epochs: tuple[EpochArchive, ...], ledger: Ledger, *, limit: int = 128, fetch: bool = False,
) -> EpochRoundSummary:
    """Backfill configured epochs newest-first without deepening any clone."""
    ordered = _validate_epochs(epochs)
    if not 1 <= limit <= 1000:
        raise ValueError("Epoch round limit must be between 1 and 1000")
    remaining = limit
    results = []
    descending = tuple(reversed(ordered))
    for epoch in descending:
        if remaining == 0:
            break
        summary = backfill_public_inbox(epoch.path, ledger, limit=remaining, fetch=fetch)
        results.append(EpochResult(epoch.number, str(epoch.path), summary))
        remaining -= summary.messages.ingested + summary.messages.replayed + summary.messages.unmatched + summary.messages.conflicts
    numbers = {epoch.number for epoch in ordered}
    return EpochRoundSummary(
        tuple(results), tuple(number for number in range(ordered[-1].number + 1) if number not in numbers),
        tuple(epoch.number for epoch in descending[len(results):]),
    )


def watch_epoch_rounds(
    epochs: tuple[EpochArchive, ...], ledger: Ledger, *, limit: int = 128, fetch: bool = False,
    allow_incomplete_bootstrap: bool = False, backfill: bool = False, interval: float = 60,
    once: bool = False, stop: Event | None = None,
) -> Iterator[EpochCycle]:
    if not isinstance(interval, (int, float)) or isinstance(interval, bool) or not math.isfinite(interval) or not 1 <= interval <= 86400:
        raise ValueError("Polling interval must be between 1 and 86400 seconds")
    ordered = _validate_epochs(epochs)
    stop = stop or Event()
    while not stop.is_set():
        ledger.assert_unconflicted(())
        if ledger.status().conflicted_message_ids:
            raise MessageConflictError("Message-ID conflicts require inspection before polling resumes")
        started = datetime.now(timezone.utc).isoformat()
        forward = sync_epoch_round(
            ordered, ledger, limit=limit, fetch=fetch, allow_incomplete_bootstrap=allow_incomplete_bootstrap,
        )
        has_conflicts = any(item.summary.messages.conflicts for item in forward.results)
        older = backfill_epoch_round(ordered, ledger, limit=limit, fetch=fetch) if backfill and not has_conflicts else None
        yield EpochCycle(started, forward, older)
        if once or stop.wait(interval):
            return


def ingest_mbox(path: Path, ledger: Ledger) -> IngestSummary:
    """Parse, record, and register revisions from one bounded mailbox."""
    with ledger.worker_lock():
        return _ingest_messages(parse_mbox(path), ledger, register_revisions=True)


def ingest_public_inbox(path: Path, ledger: Ledger, *, limit: int = 128) -> ArchiveIngestSummary:
    """Record a bounded local archive snapshot without updating its Git refs."""
    source = str(path.resolve(strict=True))
    with ledger.worker_lock():
        try:
            batch = read_public_inbox(path, limit=limit)
        except ArchiveDeletionError as error:
            ledger.record_archive_deletion(source, error.commit_id)
            raise
        summary = _ingest_messages(
            (parse_message(message.raw_message) for message in batch.messages),
            ledger,
            register_revisions=False,
        )
        return ArchiveIngestSummary(batch.head, batch.history_incomplete, summary)


def sync_public_inbox(
    path: Path, ledger: Ledger, *, limit: int = 128, allow_incomplete_bootstrap: bool = False,
    fetch: bool = False,
) -> ArchiveIngestSummary:
    """Ingest one bounded batch and advance its checkpoint only after recording mail."""
    source = str(path.resolve(strict=True))
    with ledger.worker_lock():
        checkpoint = ledger.archive_checkpoint(source)
        ref = fetch_public_inbox(path) if fetch else "HEAD"
        try:
            batch = read_public_inbox(path, limit=limit, after=checkpoint, ref=ref)
        except ArchiveDeletionError as error:
            ledger.record_archive_deletion(source, error.commit_id)
            raise
        except ArchiveCheckpointUnavailable as error:
            ledger.record_archive_discontinuity(source, error.checkpoint, error.head)
            raise
        if checkpoint is None and batch.history_incomplete and not allow_incomplete_bootstrap:
            raise PublicInboxError(
                "Archive bootstrap would omit earlier history; deepen the archive or explicitly "
                "allow an incomplete bootstrap"
            )
        summary = _ingest_messages(
            (parse_message(message.raw_message) for message in batch.messages),
            ledger,
            register_revisions=False,
        )
        if batch.messages:
            ledger.advance_archive_checkpoint(source, checkpoint, batch.messages[-1].commit_id)
        return ArchiveIngestSummary(batch.head, batch.history_incomplete, summary, batch.has_more)


def backfill_public_inbox(
    path: Path, ledger: Ledger, *, limit: int = 128, fetch: bool = False, deepen: int | None = None,
) -> ArchiveBackfillSummary:
    """Ingest one older batch, preserving a checkpoint independent of forward sync."""
    source = str(path.resolve(strict=True))
    with ledger.worker_lock():
        checkpoint = ledger.backfill_checkpoint(source)
        previous = checkpoint.commit_id if checkpoint else None
        ref = fetch_public_inbox(path, deepen=deepen) if fetch or deepen is not None else "HEAD"
        try:
            batch = read_public_inbox_backfill(path, limit=limit, before=previous, ref=ref)
        except ArchiveDeletionError as error:
            ledger.record_archive_deletion(source, error.commit_id)
            raise
        except ArchiveCheckpointUnavailable as error:
            ledger.record_archive_discontinuity(source, error.checkpoint, error.head)
            raise
        summary = _ingest_messages(
            (parse_message(message.raw_message) for message in batch.messages),
            ledger, register_revisions=False,
        )
        if batch.messages:
            ledger.advance_backfill_checkpoint(source, previous, batch.messages[-1].commit_id, complete=batch.complete)
        elif batch.complete and checkpoint and not checkpoint.complete:
            ledger.advance_backfill_checkpoint(source, previous, previous, complete=True)
        return ArchiveBackfillSummary(batch.head, summary, batch.has_more, batch.needs_deepen, batch.complete)


def _ingest_messages(
    messages: Iterable[ParsedMessage], ledger: Ledger, *, register_revisions: bool
) -> IngestSummary:
    ingested = replayed = unmatched = conflicts = 0
    accepted_messages = []
    for message in messages:
        if message.message_id is None:
            ledger.record_parsed_message(message)
            unmatched += 1
            continue
        try:
            is_new = ledger.record_parsed_message(message)
        except MessageConflictError:
            conflicts += 1
            continue
        accepted_messages.append(message)
        if is_new:
            ingested += 1
        else:
            replayed += 1
    revisions = discover_series(accepted_messages) if register_revisions else ()
    for revision in revisions:
        ledger.record_series_revision(revision)
    return IngestSummary(
        ingested=ingested,
        replayed=replayed,
        unmatched=unmatched,
        conflicts=conflicts,
        revisions_discovered=len(revisions),
        complete_revisions=sum(revision.complete for revision in revisions),
    )