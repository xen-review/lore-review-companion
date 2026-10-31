# SPDX-License-Identifier: GPL-2.0-or-later

"""Durable source-message and Gitea-delivery ledger."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sqlite3
from collections import deque
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit

from lore_review_companion.atom_feed import AtomFeed
from lore_review_companion.mail_parser import (
    ParsedMessage,
    nntp_matches_source,
    parse_message,
)
from lore_review_companion.series import (
    SeriesRevision,
    change_id_candidates,
    discover_series,
    has_invalid_change_id_claims,
)

SCHEMA_VERSION = 22

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    message_id TEXT PRIMARY KEY,
    sha256 TEXT NOT NULL,
    raw_message BLOB NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    received_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS message_source_variants (
    message_id TEXT NOT NULL REFERENCES messages(message_id) ON DELETE CASCADE,
    sha256 TEXT NOT NULL,
    raw_message BLOB NOT NULL,
    metadata_json TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    PRIMARY KEY (message_id, sha256)
);

CREATE TABLE IF NOT EXISTS message_conflicts (
    message_id TEXT NOT NULL,
    existing_sha256 TEXT NOT NULL,
    incoming_sha256 TEXT NOT NULL,
    incoming_message BLOB NOT NULL,
    detected_at TEXT NOT NULL,
    PRIMARY KEY (message_id, incoming_sha256)
);

CREATE TABLE IF NOT EXISTS message_links (
    child_id TEXT NOT NULL REFERENCES messages(message_id),
    parent_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('patch', 'cover', 'reply', 'other')),
    PRIMARY KEY (child_id, parent_id)
);

CREATE INDEX IF NOT EXISTS idx_message_links_parent ON message_links (parent_id, kind, child_id);

CREATE TABLE IF NOT EXISTS message_link_index_cursor (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_rowid INTEGER NOT NULL
);

INSERT OR IGNORE INTO message_link_index_cursor (id, last_rowid) VALUES (1, 0);

CREATE TABLE IF NOT EXISTS unmatched_messages (
    sha256 TEXT PRIMARY KEY,
    raw_message BLOB NOT NULL,
    errors_json TEXT NOT NULL,
    received_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS unmatched_retention_audit (
    approval_digest TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    actor TEXT NOT NULL,
    reason TEXT NOT NULL,
    cutoff TEXT NOT NULL,
    removed_at TEXT NOT NULL,
    PRIMARY KEY (approval_digest, sha256)
);

CREATE TABLE IF NOT EXISTS series (
    series_key TEXT PRIMARY KEY,
    root_message_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS series_revisions (
    series_key TEXT NOT NULL REFERENCES series(series_key),
    revision INTEGER NOT NULL CHECK (revision > 0),
    title TEXT NOT NULL,
    expected_patch_count INTEGER,
    cover_message_id TEXT REFERENCES messages(message_id),
    complete INTEGER NOT NULL CHECK (complete IN (0, 1)),
    conflicts_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (series_key, revision)
);

CREATE TABLE IF NOT EXISTS series_messages (
    message_id TEXT PRIMARY KEY REFERENCES messages(message_id),
    series_key TEXT NOT NULL,
    revision INTEGER NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('cover', 'patch')),
    patch_index INTEGER,
    FOREIGN KEY (series_key, revision)
        REFERENCES series_revisions(series_key, revision)
);

CREATE TABLE IF NOT EXISTS series_revision_links (
    source_root TEXT NOT NULL,
    revision INTEGER NOT NULL,
    series_key TEXT NOT NULL REFERENCES series(series_key),
    canonical_revision INTEGER NOT NULL,
    candidate_fingerprint TEXT NOT NULL,
    canonical_fingerprint TEXT NOT NULL,
    approval_digest TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (source_root, revision)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_series_revision_links_target ON series_revision_links (series_key, revision);

CREATE TABLE IF NOT EXISTS atom_entries (
    feed_url TEXT NOT NULL,
    entry_id TEXT NOT NULL,
    title TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    permalink TEXT,
    author_name TEXT,
    author_email TEXT,
    parent_entry_id TEXT,
    parent_url TEXT,
    errors_json TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY (feed_url, entry_id)
);

CREATE TABLE IF NOT EXISTS feed_checkpoints (
    feed_url TEXT PRIMARY KEY,
    feed_updated_at TEXT NOT NULL,
    last_polled_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS archive_checkpoints (
    source TEXT PRIMARY KEY,
    commit_id TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS archive_backfill_checkpoints (
    source TEXT PRIMARY KEY,
    commit_id TEXT NOT NULL,
    complete INTEGER NOT NULL CHECK (complete IN (0, 1)),
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS nntp_checkpoints (
    source TEXT PRIMARY KEY,
    bootstrap_at TEXT NOT NULL,
    cutoff TEXT NOT NULL,
    window_hours INTEGER NOT NULL CHECK (window_hours > 0),
    checked_through TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS nntp_message_queue (
    source TEXT NOT NULL REFERENCES nntp_checkpoints(source),
    message_id TEXT NOT NULL,
    received INTEGER NOT NULL DEFAULT 0 CHECK (received IN (0, 1)),
    PRIMARY KEY (source, message_id)
);

CREATE TABLE IF NOT EXISTS nntp_thread_searches (
    source TEXT NOT NULL REFERENCES nntp_checkpoints(source),
    anchor_message_id TEXT NOT NULL,
    inbox_url TEXT NOT NULL,
    from_index_time INTEGER NOT NULL,
    through_index_time INTEGER NOT NULL,
    candidate_count INTEGER NOT NULL CHECK (candidate_count >= 0),
    empty_or_unavailable INTEGER NOT NULL CHECK (empty_or_unavailable IN (0, 1)),
    scanned_at TEXT NOT NULL,
    PRIMARY KEY (source, anchor_message_id, inbox_url, from_index_time, through_index_time)
);

CREATE TABLE IF NOT EXISTS nntp_number_cursors (
    source TEXT PRIMARY KEY REFERENCES nntp_checkpoints(source),
    uid_validity INTEGER NOT NULL,
    baseline_high INTEGER NOT NULL,
    next_article INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS nntp_incidents (
    source TEXT PRIMARY KEY REFERENCES nntp_checkpoints(source),
    kind TEXT NOT NULL,
    expected INTEGER NOT NULL,
    observed INTEGER NOT NULL,
    detected_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS archive_deletions (
    source TEXT NOT NULL,
    commit_id TEXT NOT NULL,
    detected_at TEXT NOT NULL,
    PRIMARY KEY (source, commit_id)
);

CREATE TABLE IF NOT EXISTS archive_discontinuities (
    source TEXT NOT NULL,
    checkpoint TEXT NOT NULL,
    head TEXT NOT NULL,
    detected_at TEXT NOT NULL,
    PRIMARY KEY (source, checkpoint, head)
);

CREATE TABLE IF NOT EXISTS archive_discontinuity_resolution_audit (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    checkpoint TEXT NOT NULL,
    incident_head TEXT NOT NULL,
    ref TEXT NOT NULL,
    current_head TEXT NOT NULL,
    detected_at TEXT NOT NULL,
    actor TEXT NOT NULL,
    reason TEXT NOT NULL,
    approval_digest TEXT NOT NULL,
    resolved_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS identity_mappings (
    api_url TEXT NOT NULL,
    email TEXT NOT NULL,
    user_id INTEGER NOT NULL CHECK (user_id > 0),
    login TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (api_url, email)
);

CREATE TABLE IF NOT EXISTS identity_mapping_audit (
    id INTEGER PRIMARY KEY,
    api_url TEXT NOT NULL,
    email TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('set', 'remove')),
    user_id INTEGER NOT NULL,
    login TEXT NOT NULL,
    actor TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS publications (
    destination TEXT NOT NULL,
    series_key TEXT NOT NULL,
    revision INTEGER NOT NULL,
    approval_digest TEXT NOT NULL,
    base_commit TEXT NOT NULL,
    revision_branch TEXT NOT NULL,
    head_commit TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('prepared', 'branch_ready', 'pr_pending', 'uncertain', 'complete')),
    pull_index INTEGER,
    last_error TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (destination, series_key, revision),
    UNIQUE (destination, revision_branch),
    FOREIGN KEY (series_key, revision) REFERENCES series_revisions(series_key, revision)
);

CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY,
    message_id TEXT NOT NULL REFERENCES messages(message_id),
    event_type TEXT NOT NULL,
    destination TEXT NOT NULL,
    effect_key TEXT NOT NULL,
    payload TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    write_state TEXT NOT NULL DEFAULT 'pending' CHECK (write_state IN ('pending', 'sending', 'uncertain', 'complete')),
    created_at TEXT NOT NULL,
    delivered_at TEXT,
    UNIQUE (message_id, event_type, destination, effect_key)
);

CREATE TABLE IF NOT EXISTS outbox_destination_audit (
    delivery_id INTEGER NOT NULL REFERENCES outbox(id),
    previous_destination TEXT NOT NULL,
    destination TEXT NOT NULL,
    actor TEXT NOT NULL,
    reason TEXT NOT NULL,
    approval_digest TEXT NOT NULL,
    migrated_at TEXT NOT NULL,
    PRIMARY KEY (delivery_id, approval_digest)
);

CREATE INDEX IF NOT EXISTS idx_outbox_pending
    ON outbox (delivered_at, id);
"""


class MessageConflictError(ValueError):
    """An existing Message-ID was observed with different raw content."""


class ArchiveCheckpointConflict(RuntimeError):
    """Another worker changed the archive checkpoint."""


class ArchiveSafetyBlocked(RuntimeError):
    """An observed archive safety incident requires operator review."""


class WorkerBusyError(RuntimeError):
    """Another process is already using this ledger's worker lock."""


class PublicationConflict(RuntimeError):
    """A publication does not match its previously recorded intent."""


class DeliveryStateConflict(RuntimeError):
    """A queued delivery changed state or has an incompatible payload."""


@dataclass(frozen=True)
class Publication:
    destination: str
    series_key: str
    revision: int
    approval_digest: str
    base_commit: str
    revision_branch: str
    head_commit: str
    status: str
    pull_index: int | None
    last_error: str | None


@dataclass(frozen=True)
class PublicationEntry:
    cursor: int
    publication: Publication
    updated_at: str


@dataclass(frozen=True)
class PublicationPage:
    publications: tuple[PublicationEntry, ...]
    next_cursor: int | None


@dataclass(frozen=True)
class BackfillCheckpoint:
    commit_id: str
    complete: bool


@dataclass(frozen=True)
class NNTPCheckpoint:
    source: str
    bootstrap_at: str
    cutoff: str
    window_hours: int
    checked_through: str


@dataclass(frozen=True)
class NNTPNumberCursor:
    source: str
    uid_validity: int
    baseline_high: int
    next_article: int


@dataclass(frozen=True)
class NNTPIncident:
    source: str
    kind: str
    expected: int
    observed: int
    detected_at: str


@dataclass(frozen=True)
class NNTPQueueStatus:
    checkpoint: NNTPCheckpoint
    numeric_cursor: NNTPNumberCursor | None
    pending: int
    received: int
    pending_message_ids: tuple[str, ...]
    next_cursor: int | None


@dataclass(frozen=True)
class NNTPParentRepair:
    source: str
    processed: int
    newly_queued: int
    next_cursor: int | None


@dataclass(frozen=True)
class NNTPThreadSearch:
    cursor: int
    source: str
    anchor_message_id: str
    inbox_url: str
    from_index_time: int | None
    through_index_time: int | None
    candidate_count: int
    empty_or_unavailable: bool
    scanned_at: str


@dataclass(frozen=True)
class NNTPThreadSearchPage:
    searches: tuple[NNTPThreadSearch, ...]
    next_cursor: int | None


@dataclass(frozen=True)
class ArchiveDeletion:
    source: str
    commit_id: str
    detected_at: str


@dataclass(frozen=True)
class ArchiveDeletionPresence:
    message_stored: bool
    source_variants: int
    conflicting_versions: int
    direct_children: int
    series_memberships: int
    series_publications: int
    outbox_entries: int
    link_index_complete: bool
    indexed_descendants: int
    descendant_series_memberships: int
    descendant_series_publications: int
    descendant_outbox_entries: int
    indexed_links_scanned: int
    indexed_walk_complete: bool
    scope_complete: bool = False


@dataclass(frozen=True)
class ArchiveDiscontinuity:
    source: str
    checkpoint: str
    head: str
    detected_at: str


@dataclass(frozen=True)
class ArchiveDiscontinuityResolution:
    source: str
    checkpoint: str
    incident_head: str
    ref: str
    current_head: str
    detected_at: str
    actor: str
    reason: str
    approval_digest: str
    resolved_at: str


@dataclass(frozen=True)
class ManualRevisionLinkClaims:
    canonical_change_id: str | None
    candidate_change_id: str | None


@dataclass(frozen=True)
class RevisionLinkPlan:
    ledger_path: str
    series_key: str
    canonical_revision: int
    canonical_root: str
    canonical_title: str
    canonical_messages: tuple[tuple[str, str, str | None], ...]
    candidate_root: str
    candidate_revision: int
    candidate_title: str
    candidate_messages: tuple[tuple[str, str, str | None], ...]
    change_id: str | None
    actor: str
    reason: str
    manual_claims: ManualRevisionLinkClaims | None = None

    def audit_value(self) -> dict[str, Any]:
        value = asdict(self)
        if self.manual_claims is None:
            del value["manual_claims"]
        return value

    def approval_digest(self) -> str:
        value = json.dumps(
            self.audit_value(), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(value).hexdigest()


@dataclass(frozen=True)
class ChangeIDMatch:
    cursor: int
    message_id: str
    sha256: str
    subject: str
    sender: str | None
    revision: int
    invalid_claims: bool
    conflicted: bool


@dataclass(frozen=True)
class ChangeIDPage:
    matches: tuple[ChangeIDMatch, ...]
    scanned: int
    next_cursor: int | None


@dataclass(frozen=True)
class RevisionLinkRecord:
    cursor: int
    plan: dict[str, Any]
    approval_digest: str
    created_at: str


@dataclass(frozen=True)
class RevisionLinkPage:
    links: tuple[RevisionLinkRecord, ...]
    next_cursor: int | None


@dataclass(frozen=True)
class LinkIndexSummary:
    processed: int
    last_rowid: int
    complete: bool


@dataclass(frozen=True)
class LedgerStatus:
    schema_version: int
    messages: int
    unmatched_messages: int
    conflicted_message_ids: int
    link_index_last_rowid: int
    link_index_complete: bool
    archive_forward_checkpoints: int
    backfill_incomplete: int
    backfill_complete: int
    archive_deletions: tuple[ArchiveDeletion, ...]
    archive_discontinuities: tuple[ArchiveDiscontinuity, ...]
    nntp_incidents: tuple[NNTPIncident, ...]
    series_complete: int
    series_incomplete: int
    publications: dict[str, int]
    deliveries: dict[str, int]


@dataclass(frozen=True)
class RetentionEntry:
    sha256: str
    received_at: str


@dataclass(frozen=True)
class UnmatchedRetentionPlan:
    ledger_path: str
    cutoff: str
    actor: str
    reason: str
    limit: int
    entries: tuple[RetentionEntry, ...]

    def approval_digest(self) -> str:
        value = json.dumps(asdict(self), sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
        return hashlib.sha256(value).hexdigest()


@dataclass(frozen=True)
class RetentionResult:
    removed: int
    approval_digest: str


@dataclass(frozen=True)
class IdentityMapping:
    api_url: str
    email: str
    user_id: int
    login: str


@dataclass(frozen=True)
class Delivery:
    id: int
    message_id: str
    event_type: str
    destination: str
    effect_key: str
    payload: dict[str, Any]
    attempts: int
    last_error: str | None
    write_state: str


@dataclass(frozen=True)
class UnmatchedMessage:
    sha256: str
    raw_message: bytes
    errors: tuple[str, ...]


@dataclass(frozen=True)
class StoredMessage:
    cursor: int
    message_id: str
    sha256: str
    metadata: dict[str, Any]
    conflicted: bool


@dataclass(frozen=True)
class MessagePage:
    messages: tuple[StoredMessage, ...]
    next_cursor: int | None


@dataclass(frozen=True)
class FeedPollSummary:
    new_entries: int
    replayed_entries: int
    invalid_entries: int


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _create_private_file(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return
    os.close(descriptor)


def _normalize_message_id(message_id: str) -> str:
    normalized = message_id.strip()
    if normalized.startswith("<") and normalized.endswith(">"):
        normalized = normalized[1:-1]
    if not normalized or any(char.isspace() for char in normalized):
        raise ValueError("Message-ID must be non-empty and contain no whitespace")
    return normalized


def _normalize_nntp_message_id(message_id: str) -> str:
    if not isinstance(message_id, str):
        raise ValueError("NNTP contains an invalid Message-ID")
    normalized = _normalize_message_id(message_id)
    if (
        len(normalized) > 998
        or re.fullmatch(r"[^<>\s]+@[^<>\s]+", normalized) is None
        or any(ord(character) < 33 or ord(character) > 126 for character in normalized)
    ):
        raise ValueError("NNTP contains an invalid Message-ID")
    return normalized


def normalize_email(address: str) -> str:
    if not isinstance(address, str) or len(address) > 254:
        raise ValueError("A valid explicit email address is required")
    parts = address.strip().split("@")
    if len(parts) != 2:
        raise ValueError("A valid explicit email address is required")
    local, domain = parts
    labels = domain.split(".")
    if (
        not 1 <= len(local) <= 64
        or re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+", local) is None
        or local.startswith(".")
        or local.endswith(".")
        or ".." in local
        or any(
            re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
            is None
            for label in labels
        )
    ):
        raise ValueError("A valid explicit email address is required")
    return f"{local}@{domain.lower()}"


def _identity_audit_values(actor: str, reason: str) -> tuple[str, str]:
    if any(
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 500
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        for value in (actor, reason)
    ):
        raise ValueError(
            "Identity changes require an actor and a short, single-line reason"
        )
    return actor.strip(), reason.strip()


def _revision_fingerprint(revision: SeriesRevision) -> str:
    evidence = [
        revision.root_message_id,
        revision.revision,
        revision.expected_patch_count,
        [(message.message_id, message.sha256) for message in revision.messages],
    ]
    return hashlib.sha256(
        json.dumps(evidence, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _iter_link_ids(
    connection: sqlite3.Connection,
    message_id: str,
    *,
    parents: bool,
    kinds: tuple[str, ...],
    limit: int,
) -> Iterator[str]:
    source, neighbor = (
        ("child_id", "parent_id") if parents else ("parent_id", "child_id")
    )
    placeholders = ",".join("?" for _ in kinds)
    query = (
        f"SELECT {neighbor} FROM message_links WHERE {source} = ? AND {neighbor} > ?"
        f" AND kind IN ({placeholders}) ORDER BY {neighbor} LIMIT ?"
    )
    after = ""
    while True:
        rows = connection.execute(query, (message_id, after, *kinds, limit)).fetchall()
        yield from (row[0] for row in rows)
        if len(rows) < limit:
            break
        after = rows[-1][0]


class Ledger:
    def __init__(self, path: Path, *, read_only: bool = False):
        self.path = path
        self._read_only = read_only
        if read_only:
            if not path.is_file():
                raise ValueError("An existing ledger is required for read-only access")
            with self._connection() as connection:
                versions = connection.execute(
                    "SELECT version FROM schema_version"
                ).fetchall()
                if len(versions) != 1 or versions[0][0] != SCHEMA_VERSION:
                    raise ValueError(
                        "Read-only access requires the current ledger schema"
                    )
            return
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _create_private_file(self.path)
        with self._connection() as connection:
            connection.executescript("BEGIN IMMEDIATE;\n" + _SCHEMA)
            versions = connection.execute(
                "SELECT version FROM schema_version"
            ).fetchall()
            if not versions:
                connection.execute(
                    "INSERT INTO schema_version (version) VALUES (?)",
                    (SCHEMA_VERSION,),
                )
            elif len(versions) != 1:
                raise RuntimeError("Unsupported ledger schema version")
            elif versions[0][0] == 1:
                columns = {
                    row["name"]
                    for row in connection.execute("PRAGMA table_info(messages)")
                }
                if "metadata_json" not in columns:
                    connection.execute(
                        "ALTER TABLE messages ADD COLUMN metadata_json TEXT"
                        " NOT NULL DEFAULT '{}'"
                    )
                connection.execute("UPDATE schema_version SET version = 2")
                connection.execute(
                    "UPDATE schema_version SET version = ?", (SCHEMA_VERSION,)
                )
            elif versions[0][0] == 2:
                connection.execute(
                    "UPDATE schema_version SET version = ?", (SCHEMA_VERSION,)
                )
            elif versions[0][0] in (
                3,
                4,
                5,
                6,
                7,
                8,
                9,
                10,
                11,
                12,
                13,
                14,
                15,
                16,
                17,
                18,
                19,
                20,
                21,
            ):
                connection.execute(
                    "UPDATE schema_version SET version = ?", (SCHEMA_VERSION,)
                )
            elif versions[0][0] != SCHEMA_VERSION:
                raise RuntimeError("Unsupported ledger schema version")
            resolution_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(archive_discontinuity_resolution_audit)"
                )
            }
            if "ref" not in resolution_columns:
                connection.execute(
                    "ALTER TABLE archive_discontinuity_resolution_audit ADD COLUMN ref TEXT NOT NULL DEFAULT 'unknown'"
                )
            outbox_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(outbox)")
            }
            if "write_state" not in outbox_columns:
                connection.execute(
                    "ALTER TABLE outbox ADD COLUMN write_state TEXT NOT NULL DEFAULT 'pending'"
                    " CHECK (write_state IN ('pending', 'sending', 'uncertain', 'complete'))"
                )
                connection.execute(
                    "UPDATE outbox SET write_state = 'complete' WHERE delivered_at IS NOT NULL"
                )

    def _connect(self) -> sqlite3.Connection:
        database = (
            self.path.resolve().as_uri() + "?mode=ro" if self._read_only else self.path
        )
        connection = sqlite3.connect(database, timeout=15, uri=self._read_only)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 15000")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def snapshot(self, path: Path) -> Ledger:
        with path.open("xb"):
            path.chmod(0o600)
        destination = sqlite3.connect(path)
        try:
            with self._connection() as source:
                source.backup(destination)
        except BaseException:
            destination.close()
            path.unlink(missing_ok=True)
            raise
        finally:
            destination.close()
        return Ledger(path)

    def status(self) -> LedgerStatus:
        with self._connection() as connection:
            connection.execute("BEGIN")

            def count(query: str) -> int:
                return connection.execute(query).fetchone()[0]

            maximum = count("SELECT COALESCE(MAX(rowid), 0) FROM messages")
            indexed = count(
                "SELECT last_rowid FROM message_link_index_cursor WHERE id = 1"
            )
            rows = connection.execute(
                "SELECT source, commit_id, detected_at FROM archive_deletions ORDER BY detected_at, source, commit_id"
            ).fetchall()
            discontinuities = connection.execute(
                "SELECT source, checkpoint, head, detected_at FROM archive_discontinuities"
                " ORDER BY detected_at, source, checkpoint, head"
            ).fetchall()
            nntp_incidents = connection.execute(
                "SELECT source, kind, expected, observed, detected_at FROM nntp_incidents ORDER BY detected_at, source"
            ).fetchall()
            return LedgerStatus(
                schema_version=count("SELECT version FROM schema_version"),
                messages=count("SELECT COUNT(*) FROM messages"),
                unmatched_messages=count("SELECT COUNT(*) FROM unmatched_messages"),
                conflicted_message_ids=count(
                    "SELECT COUNT(DISTINCT message_id) FROM message_conflicts"
                ),
                link_index_last_rowid=indexed,
                link_index_complete=indexed >= maximum,
                archive_forward_checkpoints=count(
                    "SELECT COUNT(*) FROM archive_checkpoints"
                ),
                backfill_incomplete=count(
                    "SELECT COUNT(*) FROM archive_backfill_checkpoints WHERE complete = 0"
                ),
                backfill_complete=count(
                    "SELECT COUNT(*) FROM archive_backfill_checkpoints WHERE complete = 1"
                ),
                archive_deletions=tuple(
                    ArchiveDeletion(row["source"], row["commit_id"], row["detected_at"])
                    for row in rows
                ),
                archive_discontinuities=tuple(
                    ArchiveDiscontinuity(
                        row["source"],
                        row["checkpoint"],
                        row["head"],
                        row["detected_at"],
                    )
                    for row in discontinuities
                ),
                nntp_incidents=tuple(
                    NNTPIncident(**dict(row)) for row in nntp_incidents
                ),
                series_complete=count(
                    "SELECT COUNT(*) FROM series_revisions WHERE complete = 1"
                ),
                series_incomplete=count(
                    "SELECT COUNT(*) FROM series_revisions WHERE complete = 0"
                ),
                publications=dict(
                    connection.execute(
                        "SELECT status, COUNT(*) FROM publications GROUP BY status"
                    ).fetchall()
                ),
                deliveries=dict(
                    connection.execute(
                        "SELECT write_state, COUNT(*) FROM outbox GROUP BY write_state"
                    ).fetchall()
                ),
            )

    @contextmanager
    def worker_lock(self) -> Iterator[None]:
        """Serialize ingestion and remote writes by workers sharing this ledger."""
        lock_path = Path(str(self.path.resolve()) + ".worker-lock.sqlite3")
        _create_private_file(lock_path)
        connection = sqlite3.connect(lock_path, timeout=0, isolation_level=None)
        try:
            try:
                connection.execute("BEGIN EXCLUSIVE")
            except sqlite3.OperationalError as error:
                if "locked" in str(error).lower():
                    raise WorkerBusyError(
                        "Another worker holds the ledger worker lock"
                    ) from error
                raise
            yield
        finally:
            if connection.in_transaction:
                connection.rollback()
            connection.close()

    def record_message(
        self,
        message_id: str,
        raw_message: bytes,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        """Insert a source message; return False for an identical replay."""
        normalized_id = _normalize_message_id(message_id)
        raw_message = bytes(raw_message)
        digest = hashlib.sha256(raw_message).hexdigest()
        metadata = metadata or {}
        metadata_json = json.dumps(metadata, sort_keys=True, separators=(",", ":"))
        parsed = parse_message(raw_message)
        conflict = False
        with self._connection() as connection:
            existing = connection.execute(
                "SELECT sha256, raw_message, metadata_json FROM messages WHERE message_id = ?",
                (normalized_id,),
            ).fetchone()
            if existing:
                if existing["sha256"] == digest:
                    return False
                if connection.execute(
                    "SELECT 1 FROM message_source_variants WHERE message_id = ? AND sha256 = ?",
                    (normalized_id, digest),
                ).fetchone():
                    return False
                existing_nntp = (
                    json.loads(existing["metadata_json"]).get("transport") == "nntp"
                )
                incoming_nntp = metadata.get("transport") == "nntp"
                if existing_nntp != incoming_nntp and nntp_matches_source(
                    existing["raw_message"] if existing_nntp else raw_message,
                    raw_message if existing_nntp else existing["raw_message"],
                ):
                    connection.execute(
                        "INSERT OR IGNORE INTO message_source_variants"
                        " (message_id, sha256, raw_message, metadata_json, observed_at) VALUES (?, ?, ?, ?, ?)",
                        (normalized_id, digest, raw_message, metadata_json, _utc_now()),
                    )
                    return False
                connection.execute(
                    "INSERT OR IGNORE INTO message_conflicts"
                    " (message_id, existing_sha256, incoming_sha256,"
                    " incoming_message, detected_at) VALUES (?, ?, ?, ?, ?)",
                    (
                        normalized_id,
                        existing["sha256"],
                        digest,
                        raw_message,
                        _utc_now(),
                    ),
                )
                conflict = True
            else:
                previous_max = connection.execute(
                    "SELECT COALESCE(MAX(rowid), 0) FROM messages"
                ).fetchone()[0]
                cursor = connection.execute(
                    "INSERT INTO messages"
                    " (message_id, sha256, raw_message, metadata_json, received_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (normalized_id, digest, raw_message, metadata_json, _utc_now()),
                )
                self._index_message_links(connection, normalized_id, parsed)
                connection.execute(
                    "UPDATE message_link_index_cursor SET last_rowid = ? WHERE id = 1 AND last_rowid = ?",
                    (cursor.lastrowid, previous_max),
                )
        if conflict:
            raise MessageConflictError(
                f"Conflicting content received for Message-ID {normalized_id}"
            )
        return True

    @staticmethod
    def _index_message_links(
        connection: sqlite3.Connection, message_id: str, message: ParsedMessage
    ) -> None:
        parents = tuple(dict.fromkeys((*message.references, *message.in_reply_to)))
        connection.executemany(
            "INSERT OR IGNORE INTO message_links (child_id, parent_id, kind) VALUES (?, ?, ?)",
            ((message_id, parent_id, message.kind) for parent_id in parents),
        )

    def index_message_links(self, *, limit: int = 500) -> LinkIndexSummary:
        if not 1 <= limit <= 1000:
            raise ValueError("Link index limit must be between 1 and 1000")
        with self.worker_lock(), self._connection() as connection:
            last_rowid = connection.execute(
                "SELECT last_rowid FROM message_link_index_cursor WHERE id = 1"
            ).fetchone()[0]
            rows = connection.execute(
                "SELECT rowid, message_id, raw_message FROM messages WHERE rowid > ? ORDER BY rowid LIMIT ?",
                (last_rowid, limit),
            ).fetchall()
            for row in rows:
                self._index_message_links(
                    connection, row["message_id"], parse_message(row["raw_message"])
                )
            if rows:
                last_rowid = rows[-1]["rowid"]
                connection.execute(
                    "UPDATE message_link_index_cursor SET last_rowid = ? WHERE id = 1",
                    (last_rowid,),
                )
            maximum = connection.execute(
                "SELECT COALESCE(MAX(rowid), 0) FROM messages"
            ).fetchone()[0]
            return LinkIndexSummary(len(rows), last_rowid, last_rowid >= maximum)

    def discover_related_series(
        self, message_id: str, *, limit: int = 1000
    ) -> tuple[SeriesRevision, ...]:
        with self.worker_lock():
            return self._discover_related_series(message_id, limit=limit)

    def _discover_related_series(
        self,
        message_id: str,
        *,
        limit: int = 1000,
        apply_links: bool = True,
    ) -> tuple[SeriesRevision, ...]:
        seed = _normalize_message_id(message_id)
        if not 1 <= limit <= 1000:
            raise ValueError("Series discovery limit must be between 1 and 1000")
        with self._connection() as connection:
            indexed = connection.execute(
                "SELECT last_rowid FROM message_link_index_cursor WHERE id = 1"
            ).fetchone()[0]
            maximum = connection.execute(
                "SELECT COALESCE(MAX(rowid), 0) FROM messages"
            ).fetchone()[0]
            if indexed < maximum:
                raise ValueError(
                    "Message link index is incomplete; run index-links until complete"
                )
            row = connection.execute(
                "SELECT raw_message FROM messages WHERE message_id = ?", (seed,)
            ).fetchone()
            if not row or parse_message(row["raw_message"]).kind not in (
                "patch",
                "cover",
            ):
                raise ValueError(
                    "Series seed must identify a stored patch or cover message"
                )
            seen = {seed}
            pending = deque([seed])
            while pending:
                current = pending.popleft()
                for parents in (True, False):
                    for identifier in _iter_link_ids(
                        connection,
                        current,
                        parents=parents,
                        kinds=("patch", "cover"),
                        limit=limit,
                    ):
                        if identifier not in seen:
                            seen.add(identifier)
                            pending.append(identifier)
            self.assert_unconflicted(tuple(seen))
            identifiers = sorted(seen)
            rows = []
            for offset in range(0, len(identifiers), 500):
                subset = identifiers[offset : offset + 500]
                placeholders = ",".join("?" for _ in subset)
                rows.extend(
                    connection.execute(
                        f"SELECT message_id, raw_message FROM messages WHERE message_id IN ({placeholders})",
                        subset,
                    ).fetchall()
                )
            rows.sort(key=lambda row: row["message_id"])
            messages = tuple(parse_message(row["raw_message"]) for row in rows)
            if any(
                message.message_id != row["message_id"]
                for message, row in zip(messages, rows)
            ):
                raise ValueError("Stored message does not match its Message-ID")
            revisions = discover_series(messages)
            if not apply_links:
                return revisions
            linked = []
            for revision in revisions:
                link = connection.execute(
                    "SELECT series_key, canonical_revision, candidate_fingerprint, canonical_fingerprint"
                    " FROM series_revision_links WHERE source_root = ? AND revision = ?",
                    (revision.root_message_id, revision.revision),
                ).fetchone()
                if not link:
                    linked.append(revision)
                    continue
                if (
                    not revision.complete
                    or _revision_fingerprint(revision) != link["candidate_fingerprint"]
                ):
                    raise PublicationConflict(
                        "Linked revision membership changed; inspect its association"
                    )
                canonical = self.series_revision(
                    link["series_key"], link["canonical_revision"]
                )
                if (
                    not canonical
                    or _revision_fingerprint(canonical) != link["canonical_fingerprint"]
                ):
                    raise PublicationConflict(
                        "Linked canonical revision changed; inspect its association"
                    )
                canonical_seed = canonical.messages[0].message_id
                if not canonical_seed or canonical not in self._discover_related_series(
                    canonical_seed, limit=limit, apply_links=False
                ):
                    raise PublicationConflict(
                        "Linked canonical series membership changed; inspect its association"
                    )
                linked.append(
                    replace(
                        revision,
                        series_key=canonical.series_key,
                        root_message_id=canonical.root_message_id,
                    )
                )
            return tuple(linked)

    def plan_revision_link(
        self,
        series_key: str,
        canonical_revision: int,
        candidate_seed: str,
        candidate_revision: int,
        actor: str,
        reason: str,
        *,
        allow_unmatched_change_id: bool = False,
    ) -> RevisionLinkPlan:
        with self.worker_lock():
            return self._plan_revision_link(
                series_key,
                canonical_revision,
                candidate_seed,
                candidate_revision,
                actor,
                reason,
                allow_unmatched_change_id=allow_unmatched_change_id,
            )

    def search_change_id(
        self, change_id: str, *, after: int = 0, limit: int = 100
    ) -> ChangeIDPage:
        if (
            not isinstance(change_id, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,199}", change_id) is None
        ):
            raise ValueError("A valid standalone change-ID is required")
        if not 1 <= limit <= 1000 or after < 0:
            raise ValueError("Change-ID scan cursor or limit is invalid")
        matches = []
        scanned = last_cursor = 0
        has_more = False
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT m.rowid AS cursor, m.message_id, m.sha256, m.raw_message,"
                " EXISTS(SELECT 1 FROM message_conflicts c WHERE c.message_id = m.message_id) AS conflicted"
                " FROM messages m WHERE m.rowid > ? ORDER BY m.rowid LIMIT ?",
                (after, limit + 1),
            )
            for row in rows:
                if scanned == limit:
                    has_more = True
                    break
                scanned += 1
                last_cursor = row["cursor"]
                message = parse_message(row["raw_message"])
                if message.message_id != row["message_id"]:
                    raise ValueError("Stored message does not match its Message-ID")
                if message.kind not in ("cover", "patch"):
                    continue
                revision = discover_series((message,))[0]
                if change_id in change_id_candidates(revision):
                    matches.append(
                        ChangeIDMatch(
                            last_cursor,
                            row["message_id"],
                            row["sha256"],
                            message.subject,
                            message.from_address,
                            revision.revision,
                            has_invalid_change_id_claims(revision),
                            bool(row["conflicted"]),
                        )
                    )
        return ChangeIDPage(tuple(matches), scanned, last_cursor if has_more else None)

    def revision_links(self, *, after: int = 0, limit: int = 100) -> RevisionLinkPage:
        if not 1 <= limit <= 1000 or after < 0:
            raise ValueError("Revision-link cursor or limit is invalid")
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT rowid AS cursor, plan_json, approval_digest, created_at FROM series_revision_links"
                " WHERE rowid > ? ORDER BY rowid LIMIT ?",
                (after, limit + 1),
            ).fetchall()
        links = tuple(
            RevisionLinkRecord(
                row["cursor"],
                json.loads(row["plan_json"]),
                row["approval_digest"],
                row["created_at"],
            )
            for row in rows[:limit]
        )
        return RevisionLinkPage(links, links[-1].cursor if len(rows) > limit else None)

    def _plan_revision_link(
        self,
        series_key: str,
        canonical_revision: int,
        candidate_seed: str,
        candidate_revision: int,
        actor: str,
        reason: str,
        *,
        allow_unmatched_change_id: bool = False,
    ) -> RevisionLinkPlan:
        if not isinstance(allow_unmatched_change_id, bool):
            raise ValueError("The unmatched change-ID override must be a boolean")
        if any(
            not isinstance(value, str)
            or not value.strip()
            or len(value) > 500
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
            for value in (actor, reason)
        ):
            raise ValueError(
                "Revision linking requires an actor and a short, single-line reason"
            )
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 1
            for value in (canonical_revision, candidate_revision)
        ):
            raise ValueError("Revision numbers must be positive integers")
        canonical = self.series_revision(series_key, canonical_revision)
        if (
            not canonical
            or not canonical.complete
            or canonical.conflicts
            or not canonical.messages
        ):
            raise PublicationConflict(
                "A complete recorded canonical revision is required"
            )
        canonical_seed = canonical.messages[0].message_id
        if not canonical_seed or canonical not in self._discover_related_series(
            canonical_seed, apply_links=False
        ):
            raise PublicationConflict(
                "Canonical revision membership changed or is itself a linked revision"
            )
        seed = _normalize_message_id(candidate_seed)
        candidates = [
            revision
            for revision in self._discover_related_series(seed, apply_links=False)
            if revision.revision == candidate_revision
            and any(message.message_id == seed for message in revision.messages)
        ]
        if (
            len(candidates) != 1
            or not candidates[0].complete
            or candidates[0].conflicts
        ):
            raise PublicationConflict(
                "One complete, unambiguous candidate revision is required"
            )
        candidate = candidates[0]
        if (
            candidate.root_message_id == canonical.root_message_id
            or candidate_revision == canonical_revision
        ):
            raise PublicationConflict(
                "Candidate must be a different, independently threaded revision"
            )
        canonical_ids = change_id_candidates(canonical)
        candidate_ids = change_id_candidates(candidate)
        if (
            len(canonical_ids) > 1
            or len(candidate_ids) > 1
            or has_invalid_change_id_claims(canonical)
            or has_invalid_change_id_claims(candidate)
        ):
            raise PublicationConflict(
                "Revisions must not contain invalid or multiple change-ID claims"
            )
        matched = len(canonical_ids) == 1 and canonical_ids == candidate_ids
        if not matched and not allow_unmatched_change_id:
            raise PublicationConflict(
                "Revisions must each claim the same unique change-ID unless explicitly reviewed"
            )
        manual_claims = (
            ManualRevisionLinkClaims(
                canonical_ids[0] if canonical_ids else None,
                candidate_ids[0] if candidate_ids else None,
            )
            if allow_unmatched_change_id
            else None
        )
        linked_candidate = replace(
            candidate,
            series_key=canonical.series_key,
            root_message_id=canonical.root_message_id,
        )
        existing_revision = self.series_revision(series_key, candidate_revision)
        if existing_revision and existing_revision != linked_candidate:
            raise PublicationConflict(
                "Canonical series already has a different revision with this number"
            )
        with self._connection() as connection:
            linked = connection.execute(
                "SELECT source_root FROM series_revision_links WHERE series_key = ? AND revision = ?",
                (series_key, candidate_revision),
            ).fetchone()
            if linked and linked["source_root"] != candidate.root_message_id:
                raise PublicationConflict(
                    "Canonical revision is already linked to a different source thread"
                )
            for message in candidate.messages:
                row = connection.execute(
                    "SELECT series_key, revision FROM series_messages WHERE message_id = ?",
                    (message.message_id,),
                ).fetchone()
                if row and (row["series_key"], row["revision"]) != (
                    series_key,
                    candidate_revision,
                ):
                    raise PublicationConflict(
                        "Candidate source is already assigned to a different revision"
                    )

        def members(
            revision: SeriesRevision,
        ) -> tuple[tuple[str, str, str | None], ...]:
            return tuple(
                (message.message_id, message.sha256, message.from_address)
                for message in revision.messages
            )

        return RevisionLinkPlan(
            str(self.path.resolve()),
            series_key,
            canonical_revision,
            canonical.root_message_id,
            canonical.title,
            members(canonical),
            candidate.root_message_id,
            candidate_revision,
            candidate.title,
            members(candidate),
            canonical_ids[0] if matched else None,
            actor.strip(),
            reason.strip(),
            manual_claims,
        )

    def link_revision(
        self,
        series_key: str,
        canonical_revision: int,
        candidate_seed: str,
        candidate_revision: int,
        actor: str,
        reason: str,
        *,
        approved_digest: str,
        allow_unmatched_change_id: bool = False,
    ) -> RevisionLinkPlan:
        if (
            not isinstance(approved_digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", approved_digest) is None
        ):
            raise ValueError("A valid revision link approval digest is required")
        with self.worker_lock():
            plan = self._plan_revision_link(
                series_key,
                canonical_revision,
                candidate_seed,
                candidate_revision,
                actor,
                reason,
                allow_unmatched_change_id=allow_unmatched_change_id,
            )
            digest = plan.approval_digest()
            if not hmac.compare_digest(approved_digest, digest):
                raise ValueError(
                    "Approval digest does not match the current revision link plan"
                )
            canonical = self.series_revision(series_key, canonical_revision)
            candidate = next(
                revision
                for revision in self._discover_related_series(
                    candidate_seed, apply_links=False
                )
                if revision.revision == candidate_revision
                and revision.root_message_id == plan.candidate_root
            )
            value = json.dumps(
                plan.audit_value(), sort_keys=True, separators=(",", ":")
            )
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "INSERT INTO series_revision_links (source_root, revision, series_key, canonical_revision,"
                    " candidate_fingerprint, canonical_fingerprint, approval_digest, plan_json, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (source_root, revision) DO NOTHING",
                    (
                        plan.candidate_root,
                        candidate_revision,
                        series_key,
                        canonical_revision,
                        _revision_fingerprint(candidate),
                        _revision_fingerprint(canonical),
                        digest,
                        value,
                        _utc_now(),
                    ),
                )
                row = connection.execute(
                    "SELECT approval_digest, plan_json FROM series_revision_links WHERE source_root = ? AND revision = ?",
                    (plan.candidate_root, candidate_revision),
                ).fetchone()
                if (
                    not row
                    or row["approval_digest"] != digest
                    or row["plan_json"] != value
                ):
                    raise PublicationConflict(
                        "Candidate revision is already linked with a different decision"
                    )
            return plan

    def _related_thread_messages(
        self, member_ids: tuple[str, ...], *, limit: int = 1000
    ) -> tuple[ParsedMessage, ...]:
        members = {_normalize_message_id(message_id) for message_id in member_ids}
        if not members or not 1 <= limit <= 1000:
            raise ValueError(
                "Thread discovery requires members and a link query limit in 1..1000"
            )
        with self._connection() as connection:
            indexed = connection.execute(
                "SELECT last_rowid FROM message_link_index_cursor WHERE id = 1"
            ).fetchone()[0]
            maximum = connection.execute(
                "SELECT COALESCE(MAX(rowid), 0) FROM messages"
            ).fetchone()[0]
            if indexed < maximum:
                raise ValueError(
                    "Message link index is incomplete; run index-links until complete"
                )
            seen = set(members)
            pending = deque(sorted(members))
            messages = []
            while pending:
                current = pending.popleft()
                row = connection.execute(
                    "SELECT raw_message FROM messages WHERE message_id = ?", (current,)
                ).fetchone()
                if not row:
                    continue
                message = parse_message(row["raw_message"])
                if message.message_id != current:
                    raise ValueError("Stored message does not match its Message-ID")
                if current not in members:
                    messages.append(message)
                if current not in members and message.kind != "reply":
                    continue
                directions = (False, True) if message.kind == "reply" else (False,)
                for parents in directions:
                    for identifier in _iter_link_ids(
                        connection,
                        current,
                        parents=parents,
                        kinds=("reply",),
                        limit=limit,
                    ):
                        if identifier not in seen:
                            seen.add(identifier)
                            pending.append(identifier)
            self.assert_unconflicted(tuple(seen))
            return tuple(messages)

    def record_unmatched_message(
        self, raw_message: bytes, errors: tuple[str, ...]
    ) -> bool:
        """Retain a message that cannot be keyed by a valid Message-ID."""
        raw_message = bytes(raw_message)
        digest = hashlib.sha256(raw_message).hexdigest()
        with self._connection() as connection:
            cursor = connection.execute(
                "INSERT INTO unmatched_messages (sha256, raw_message, errors_json, received_at)"
                " VALUES (?, ?, ?, ?) ON CONFLICT (sha256) DO NOTHING",
                (
                    digest,
                    raw_message,
                    json.dumps(errors, separators=(",", ":")),
                    _utc_now(),
                ),
            )
            return cursor.rowcount == 1

    def _unmatched_retention_plan(
        self,
        connection: sqlite3.Connection,
        cutoff: str,
        actor: str,
        reason: str,
        limit: int,
    ) -> UnmatchedRetentionPlan:
        actor, reason = _identity_audit_values(actor, reason)
        if not 1 <= limit <= 1000:
            raise ValueError("Retention limit must be between 1 and 1000")
        try:
            parsed_cutoff = datetime.fromisoformat(cutoff.replace("Z", "+00:00"))
        except (AttributeError, ValueError) as error:
            raise ValueError(
                "Retention cutoff must be an ISO 8601 timestamp with timezone"
            ) from error
        if parsed_cutoff.tzinfo is None or parsed_cutoff.utcoffset() is None:
            raise ValueError("Retention cutoff must include a timezone")
        parsed_cutoff = parsed_cutoff.astimezone(timezone.utc)
        if parsed_cutoff > datetime.now(timezone.utc):
            raise ValueError("Retention cutoff cannot be in the future")
        normalized_cutoff = parsed_cutoff.isoformat()
        rows = connection.execute(
            "SELECT sha256, received_at FROM unmatched_messages WHERE received_at < ? ORDER BY received_at, sha256 LIMIT ?",
            (normalized_cutoff, limit),
        ).fetchall()
        return UnmatchedRetentionPlan(
            str(self.path.resolve()),
            normalized_cutoff,
            actor,
            reason,
            limit,
            tuple(RetentionEntry(row["sha256"], row["received_at"]) for row in rows),
        )

    def preview_unmatched_retention(
        self,
        cutoff: str,
        actor: str,
        reason: str,
        *,
        limit: int = 100,
    ) -> UnmatchedRetentionPlan:
        with self.worker_lock():
            self.assert_unconflicted(())
            with self._connection() as connection:
                return self._unmatched_retention_plan(
                    connection, cutoff, actor, reason, limit
                )

    def prune_unmatched(
        self,
        cutoff: str,
        actor: str,
        reason: str,
        *,
        approved_digest: str,
        limit: int = 100,
    ) -> RetentionResult:
        if (
            not isinstance(approved_digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", approved_digest) is None
        ):
            raise ValueError("A valid retention approval digest is required")
        with self.worker_lock():
            self.assert_unconflicted(())
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                plan = self._unmatched_retention_plan(
                    connection, cutoff, actor, reason, limit
                )
                digest = plan.approval_digest()
                if not hmac.compare_digest(approved_digest, digest):
                    raise ValueError(
                        "Approval digest does not match the current retention plan"
                    )
                for entry in plan.entries:
                    cursor = connection.execute(
                        "DELETE FROM unmatched_messages WHERE sha256 = ? AND received_at = ?",
                        (entry.sha256, entry.received_at),
                    )
                    if cursor.rowcount != 1:
                        raise ValueError(
                            "Unmatched mail changed during retention pruning"
                        )
                    connection.execute(
                        "INSERT INTO unmatched_retention_audit"
                        " (approval_digest, sha256, actor, reason, cutoff, removed_at) VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            digest,
                            entry.sha256,
                            plan.actor,
                            plan.reason,
                            plan.cutoff,
                            _utc_now(),
                        ),
                    )
                return RetentionResult(len(plan.entries), digest)

    def assert_unconflicted(self, message_ids: tuple[str, ...]) -> None:
        normalized = tuple(
            dict.fromkeys(
                _normalize_message_id(message_id) for message_id in message_ids
            )
        )
        with self._connection() as connection:
            deletion = connection.execute(
                "SELECT source, commit_id FROM archive_deletions ORDER BY detected_at, source, commit_id LIMIT 1"
            ).fetchone()
            if deletion:
                raise ArchiveSafetyBlocked(
                    f"Publication blocked by archive deletion {deletion['commit_id']} from {deletion['source']}; "
                    "review removal and retention policy"
                )
            discontinuity = connection.execute(
                "SELECT source, checkpoint, head FROM archive_discontinuities ORDER BY detected_at LIMIT 1"
            ).fetchone()
            if discontinuity:
                raise ArchiveSafetyBlocked(
                    f"Publication blocked because archive checkpoint {discontinuity['checkpoint']} is not "
                    f"verified against {discontinuity['head']} from {discontinuity['source']}; inspect source history"
                )
            nntp_incident = connection.execute(
                "SELECT source, kind FROM nntp_incidents ORDER BY detected_at LIMIT 1"
            ).fetchone()
            if nntp_incident:
                raise ArchiveSafetyBlocked(
                    f"Publication blocked by NNTP {nntp_incident['kind']} from {nntp_incident['source']}; inspect source history"
                )
            for offset in range(0, len(normalized), 500):
                subset = normalized[offset : offset + 500]
                placeholders = ",".join("?" for _ in subset)
                row = connection.execute(
                    f"SELECT message_id FROM message_conflicts WHERE message_id IN ({placeholders}) LIMIT 1",
                    subset,
                ).fetchone()
                if row:
                    raise MessageConflictError(
                        f"Quarantined conflicting Message-ID: {row['message_id']}"
                    )

    def assert_message_ancestry_available(
        self, messages: tuple[ParsedMessage, ...]
    ) -> None:
        supplied = tuple(
            message.message_id for message in messages if message.message_id
        )
        seeds = tuple(
            dict.fromkeys(
                identifier
                for message in messages
                for identifier in (
                    message.message_id,
                    *message.references,
                    *message.in_reply_to,
                )
                if identifier
            )
        )
        if not seeds:
            return
        with self._connection() as connection:
            indexed = connection.execute(
                "SELECT last_rowid FROM message_link_index_cursor WHERE id = 1"
            ).fetchone()[0]
            maximum = connection.execute(
                "SELECT COALESCE(MAX(rowid), 0) FROM messages"
            ).fetchone()[0]
            if indexed < maximum:
                raise ValueError(
                    "Message link index is incomplete; run index-links until complete"
                )
            row = connection.execute(
                "WITH RECURSIVE ancestry(message_id) AS ("
                " SELECT value FROM json_each(?)"
                " UNION"
                " SELECT links.parent_id FROM message_links AS links"
                " JOIN ancestry ON links.child_id = ancestry.message_id"
                ")"
                " SELECT ancestry.message_id,"
                " EXISTS(SELECT 1 FROM message_conflicts WHERE message_id = ancestry.message_id) AS conflicted,"
                " messages.message_id IS NULL AND ancestry.message_id NOT IN (SELECT value FROM json_each(?)) AS missing"
                " FROM ancestry LEFT JOIN messages ON messages.message_id = ancestry.message_id"
                " WHERE conflicted OR missing LIMIT 1",
                (json.dumps(seeds), json.dumps(supplied)),
            ).fetchone()
        if row and row["conflicted"]:
            raise MessageConflictError(
                f"Quarantined conflicting Message-ID: {row['message_id']}"
            )
        if row:
            raise PublicationConflict(
                "Message ancestry is incomplete; fetch referenced messages before publication or delivery"
            )

    def record_archive_deletion(self, source: str, commit_id: str) -> None:
        if (
            not source
            or re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", commit_id) is None
        ):
            raise ValueError(
                "An archive source and full deletion commit ID are required"
            )
        with self._connection() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO archive_deletions (source, commit_id, detected_at) VALUES (?, ?, ?)",
                (source, commit_id, _utc_now()),
            )

    def archive_deletions(self) -> tuple[ArchiveDeletion, ...]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT source, commit_id, detected_at FROM archive_deletions ORDER BY detected_at, source, commit_id"
            ).fetchall()
        return tuple(
            ArchiveDeletion(row["source"], row["commit_id"], row["detected_at"])
            for row in rows
        )

    def archive_deletion_presence(
        self, message_id: str, *, link_limit: int = 1000
    ) -> ArchiveDeletionPresence:
        message_id = _normalize_message_id(message_id)
        if (
            not isinstance(link_limit, int)
            or isinstance(link_limit, bool)
            or not 1 <= link_limit <= 10000
        ):
            raise ValueError(
                "Deletion inspection link limit must be between 1 and 10000"
            )
        with self._connection() as connection:
            connection.execute("BEGIN")
            row = connection.execute(
                "SELECT EXISTS(SELECT 1 FROM messages WHERE message_id = ?) AS message_stored,"
                " (SELECT COUNT(*) FROM message_source_variants WHERE message_id = ?) AS source_variants,"
                " (SELECT COUNT(*) FROM message_conflicts WHERE message_id = ?) AS conflicting_versions,"
                " (SELECT COUNT(*) FROM message_links WHERE parent_id = ?) AS direct_children,"
                " (SELECT COUNT(*) FROM series_messages WHERE message_id = ?) AS series_memberships,"
                " (SELECT COUNT(*) FROM series_messages JOIN publications USING (series_key, revision)"
                " WHERE series_messages.message_id = ?) AS series_publications,"
                " (SELECT COUNT(*) FROM outbox WHERE message_id = ?) AS outbox_entries,"
                " (SELECT last_rowid FROM message_link_index_cursor WHERE id = 1) >="
                " (SELECT COALESCE(MAX(rowid), 0) FROM messages) AS link_index_complete",
                (message_id,) * 7,
            ).fetchone()
            seen = {message_id}
            pending = deque([message_id])
            scanned = 0
            complete = True
            while pending:
                parent = pending.popleft()
                remaining = link_limit - scanned
                links = connection.execute(
                    "SELECT child_id FROM message_links WHERE parent_id = ? ORDER BY kind, child_id LIMIT ?",
                    (parent, remaining + 1),
                ).fetchall()
                for link in links[:remaining]:
                    scanned += 1
                    child = link["child_id"]
                    if child not in seen:
                        seen.add(child)
                        pending.append(child)
                if len(links) > remaining:
                    complete = False
                    break
            descendants = seen - {message_id}
            descendant_ids = json.dumps(sorted(descendants))
            descendant_counts = connection.execute(
                "SELECT (SELECT COUNT(*) FROM series_messages WHERE message_id IN (SELECT value FROM json_each(?))) AS memberships,"
                " (SELECT COUNT(DISTINCT publications.rowid) FROM series_messages JOIN publications USING (series_key, revision)"
                " WHERE series_messages.message_id IN (SELECT value FROM json_each(?))) AS publications,"
                " (SELECT COUNT(*) FROM outbox WHERE message_id IN (SELECT value FROM json_each(?))) AS outbox_entries",
                (descendant_ids,) * 3,
            ).fetchone()
        return ArchiveDeletionPresence(
            bool(row["message_stored"]),
            row["source_variants"],
            row["conflicting_versions"],
            row["direct_children"],
            row["series_memberships"],
            row["series_publications"],
            row["outbox_entries"],
            bool(row["link_index_complete"]),
            len(descendants),
            descendant_counts["memberships"],
            descendant_counts["publications"],
            descendant_counts["outbox_entries"],
            scanned,
            complete,
        )

    def has_archive_deletion(self, source: str, commit_id: str) -> bool:
        if (
            not source
            or not isinstance(commit_id, str)
            or re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", commit_id) is None
        ):
            raise ValueError(
                "An archive source and full deletion commit ID are required"
            )
        with self._connection() as connection:
            row = connection.execute(
                "SELECT 1 FROM archive_deletions WHERE source = ? AND commit_id = ?",
                (source, commit_id),
            ).fetchone()
        return bool(row)

    def record_archive_discontinuity(
        self, source: str, checkpoint: str, head: str
    ) -> None:
        if not source or any(
            re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value) is None
            for value in (checkpoint, head)
        ):
            raise ValueError(
                "An archive source and full checkpoint and head IDs are required"
            )
        with self._connection() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO archive_discontinuities (source, checkpoint, head, detected_at) VALUES (?, ?, ?, ?)",
                (source, checkpoint, head, _utc_now()),
            )

    def archive_discontinuities(self) -> tuple[ArchiveDiscontinuity, ...]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT source, checkpoint, head, detected_at FROM archive_discontinuities"
                " ORDER BY detected_at, source, checkpoint, head"
            ).fetchall()
        return tuple(
            ArchiveDiscontinuity(
                row["source"], row["checkpoint"], row["head"], row["detected_at"]
            )
            for row in rows
        )

    def archive_discontinuity_resolutions(
        self, *, limit: int = 100
    ) -> tuple[ArchiveDiscontinuityResolution, ...]:
        if not 1 <= limit <= 1000:
            raise ValueError("Archive resolution limit must be between 1 and 1000")
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT source, checkpoint, incident_head, ref, current_head, detected_at, actor, reason, approval_digest, resolved_at"
                " FROM archive_discontinuity_resolution_audit ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return tuple(ArchiveDiscontinuityResolution(**dict(row)) for row in rows)

    def resolve_archive_discontinuity(
        self,
        incident: ArchiveDiscontinuity,
        current_head: str,
        *,
        ref: str,
        actor: str,
        reason: str,
        digest: str,
    ) -> None:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            checkpoint = connection.execute(
                "SELECT commit_id FROM archive_checkpoints WHERE source = ?",
                (incident.source,),
            ).fetchone()
            if not checkpoint or checkpoint["commit_id"] != current_head:
                raise ArchiveCheckpointConflict(
                    "Forward ingestion must reach the verified archive head"
                )
            cursor = connection.execute(
                "DELETE FROM archive_discontinuities WHERE source = ? AND checkpoint = ? AND head = ? AND detected_at = ?",
                (
                    incident.source,
                    incident.checkpoint,
                    incident.head,
                    incident.detected_at,
                ),
            )
            if cursor.rowcount != 1:
                raise ArchiveCheckpointConflict(
                    "Archive discontinuity changed during resolution"
                )
            connection.execute(
                "INSERT INTO archive_discontinuity_resolution_audit"
                " (source, checkpoint, incident_head, ref, current_head, detected_at, actor, reason, approval_digest, resolved_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    incident.source,
                    incident.checkpoint,
                    incident.head,
                    ref,
                    current_head,
                    incident.detected_at,
                    actor,
                    reason,
                    digest,
                    _utc_now(),
                ),
            )

    def record_parsed_message(
        self, message: ParsedMessage, *, provenance: dict[str, Any] | None = None
    ) -> bool:
        """Persist parsed headers and provenance, quarantining unkeyed mail."""
        if message.message_id is None:
            self.record_unmatched_message(message.raw_message, message.errors)
            return True

        metadata = {
            "from_address": message.from_address,
            "from_name": message.from_name,
            "in_reply_to": message.in_reply_to,
            "kind": message.kind,
            "patch_index": message.patch_index,
            "patch_version": message.patch_version,
            "references": message.references,
            "series_size": message.series_size,
            "series_title": message.subject_title,
            "subject": message.subject,
            "time_estimate": asdict(message.time_estimate)
            if message.time_estimate
            else None,
            "trailers": [asdict(trailer) for trailer in message.trailers],
            "errors": message.errors,
        }
        if provenance:
            if metadata.keys() & provenance.keys():
                raise ValueError(
                    "Source provenance must not override parsed message metadata"
                )
            metadata.update(provenance)
        return self.record_message(message.message_id, message.raw_message, metadata)

    def unmatched_messages(self, limit: int = 100) -> list[UnmatchedMessage]:
        if limit < 1:
            raise ValueError("limit must be positive")
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT sha256, raw_message, errors_json FROM unmatched_messages"
                " ORDER BY received_at LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            UnmatchedMessage(
                sha256=row["sha256"],
                raw_message=row["raw_message"],
                errors=tuple(json.loads(row["errors_json"])),
            )
            for row in rows
        ]

    def messages_by_id(self, message_ids: tuple[str, ...]) -> tuple[ParsedMessage, ...]:
        normalized = tuple(
            _normalize_message_id(message_id) for message_id in message_ids
        )
        if not 1 <= len(normalized) <= 500 or len(set(normalized)) != len(normalized):
            raise ValueError("Select between 1 and 500 distinct Message-IDs")
        self.assert_unconflicted(normalized)
        placeholders = ",".join("?" for _ in normalized)
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT message_id, raw_message FROM messages WHERE message_id IN ({placeholders})",
                normalized,
            ).fetchall()
        by_id = {row["message_id"]: row["raw_message"] for row in rows}
        missing = [message_id for message_id in normalized if message_id not in by_id]
        if missing:
            raise ValueError(f"Source Message-ID is not in the ledger: {missing[0]}")
        return tuple(parse_message(by_id[message_id]) for message_id in normalized)

    def list_messages(
        self, *, kind: str | None = None, after: int = 0, limit: int = 100
    ) -> MessagePage:
        if kind not in (None, "patch", "cover", "reply", "other"):
            raise ValueError("Unsupported message kind")
        if not 1 <= limit <= 1000 or after < 0:
            raise ValueError("Message cursor or limit is invalid")
        conditions = ["m.rowid > ?"]
        parameters: list[Any] = [after]
        if kind is not None:
            conditions.append("json_extract(m.metadata_json, '$.kind') = ?")
            parameters.append(kind)
        parameters.append(limit + 1)
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT m.rowid AS cursor, m.message_id, m.sha256, m.metadata_json,"
                " EXISTS(SELECT 1 FROM message_conflicts c WHERE c.message_id = m.message_id) AS conflicted"
                f" FROM messages m WHERE {' AND '.join(conditions)} ORDER BY m.rowid LIMIT ?",
                parameters,
            ).fetchall()
        messages = tuple(
            StoredMessage(
                row["cursor"],
                row["message_id"],
                row["sha256"],
                json.loads(row["metadata_json"]),
                bool(row["conflicted"]),
            )
            for row in rows[:limit]
        )
        return MessagePage(messages, messages[-1].cursor if len(rows) > limit else None)

    def record_series_revision(self, revision: Any) -> None:
        """Persist one discovered revision and reject reassigned source mail."""
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for message in revision.messages:
                if message.message_id is None:
                    raise ValueError("series messages must have a valid Message-ID")
                source = connection.execute(
                    "SELECT sha256 FROM messages WHERE message_id = ?",
                    (message.message_id,),
                ).fetchone()
                if not source:
                    raise ValueError(
                        f"Source message is not ingested: {message.message_id}"
                    )
                if (
                    source["sha256"] != message.sha256
                    or hashlib.sha256(message.raw_message).hexdigest() != message.sha256
                ):
                    raise MessageConflictError(
                        f"Series source does not match stored Message-ID {message.message_id}"
                    )

            publication = connection.execute(
                "SELECT 1 FROM publications WHERE series_key = ? AND revision = ? LIMIT 1",
                (revision.series_key, revision.revision),
            ).fetchone()
            if publication:
                if (
                    self._series_revision(
                        connection, revision.series_key, revision.revision
                    )
                    != revision
                ):
                    raise PublicationConflict(
                        "Recorded publication source membership is immutable"
                    )
                return

            connection.execute(
                "INSERT INTO series (series_key, root_message_id, created_at)"
                " VALUES (?, ?, ?) ON CONFLICT (series_key) DO NOTHING",
                (revision.series_key, revision.root_message_id, _utc_now()),
            )
            series_row = connection.execute(
                "SELECT root_message_id FROM series WHERE series_key = ?",
                (revision.series_key,),
            ).fetchone()
            if (
                not series_row
                or series_row["root_message_id"] != revision.root_message_id
            ):
                raise ValueError(
                    "Series key is already assigned to a different thread root"
                )

            existing_revision = connection.execute(
                "SELECT title, expected_patch_count, cover_message_id FROM series_revisions"
                " WHERE series_key = ? AND revision = ?",
                (revision.series_key, revision.revision),
            ).fetchone()
            if existing_revision:
                title = existing_revision["title"] or revision.title
                expected_count = (
                    revision.expected_patch_count
                    if revision.expected_patch_count is not None
                    else existing_revision["expected_patch_count"]
                )
                cover_id = (
                    revision.cover_message_id or existing_revision["cover_message_id"]
                )
            else:
                title = revision.title
                expected_count = revision.expected_patch_count
                cover_id = revision.cover_message_id

            connection.execute(
                "INSERT INTO series_revisions"
                " (series_key, revision, title, expected_patch_count, cover_message_id,"
                " complete, conflicts_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (series_key, revision) DO UPDATE SET"
                " title = excluded.title, expected_patch_count = excluded.expected_patch_count,"
                " cover_message_id = excluded.cover_message_id, complete = excluded.complete,"
                " conflicts_json = excluded.conflicts_json",
                (
                    revision.series_key,
                    revision.revision,
                    title,
                    expected_count,
                    cover_id,
                    int(revision.complete),
                    json.dumps(revision.conflicts, separators=(",", ":")),
                    _utc_now(),
                ),
            )

            for message in revision.messages:
                role = "cover" if message.kind == "cover" else "patch"
                existing_member = connection.execute(
                    "SELECT series_key, revision FROM series_messages WHERE message_id = ?",
                    (message.message_id,),
                ).fetchone()
                if existing_member and (
                    existing_member["series_key"] != revision.series_key
                    or existing_member["revision"] != revision.revision
                ):
                    raise ValueError(
                        f"Source message is already assigned to another revision:"
                        f" {message.message_id}"
                    )
                connection.execute(
                    "INSERT INTO series_messages"
                    " (message_id, series_key, revision, role, patch_index)"
                    " VALUES (?, ?, ?, ?, ?) ON CONFLICT (message_id) DO NOTHING",
                    (
                        message.message_id,
                        revision.series_key,
                        revision.revision,
                        role,
                        message.patch_index,
                    ),
                )

    def series_revision(self, series_key: str, revision: int) -> SeriesRevision | None:
        with self._connection() as connection:
            return self._series_revision(connection, series_key, revision)

    @staticmethod
    def _series_revision(
        connection: sqlite3.Connection, series_key: str, revision: int
    ) -> SeriesRevision | None:
        row = connection.execute(
            "SELECT s.root_message_id, r.title, r.expected_patch_count, r.cover_message_id,"
            " r.complete, r.conflicts_json FROM series_revisions r"
            " JOIN series s ON s.series_key = r.series_key"
            " WHERE r.series_key = ? AND r.revision = ?",
            (series_key, revision),
        ).fetchone()
        if not row:
            return None
        members = connection.execute(
            "SELECT m.raw_message FROM series_messages sm"
            " JOIN messages m ON m.message_id = sm.message_id"
            " WHERE sm.series_key = ? AND sm.revision = ?"
            " ORDER BY sm.patch_index IS NULL, sm.patch_index, sm.message_id",
            (series_key, revision),
        ).fetchall()
        return SeriesRevision(
            series_key=series_key,
            root_message_id=row["root_message_id"],
            revision=revision,
            title=row["title"],
            expected_patch_count=row["expected_patch_count"],
            cover_message_id=row["cover_message_id"],
            messages=tuple(parse_message(member["raw_message"]) for member in members),
            complete=bool(row["complete"]),
            conflicts=tuple(json.loads(row["conflicts_json"])),
        )

    def record_atom_feed(self, feed_url: str, feed: AtomFeed) -> FeedPollSummary:
        """Store feed metadata and advance its replay-safe checkpoint."""
        if not feed.feed_id or not feed.updated_at:
            raise ValueError("Atom feed must have an ID and updated timestamp")
        now = _utc_now()
        new_entries = replayed_entries = invalid_entries = 0
        checkpoint_safe = True
        with self._connection() as connection:
            for entry in feed.entries:
                if entry.entry_id is None:
                    invalid_entries += 1
                    checkpoint_safe = False
                    continue
                invalid_entries += bool(entry.errors)
                existing = connection.execute(
                    "SELECT 1 FROM atom_entries WHERE feed_url = ? AND entry_id = ?",
                    (feed_url, entry.entry_id),
                ).fetchone()
                if existing:
                    replayed_entries += 1
                    connection.execute(
                        "UPDATE atom_entries SET title = ?, updated_at = ?, permalink = ?,"
                        " author_name = ?, author_email = ?, parent_entry_id = ?, parent_url = ?,"
                        " errors_json = ?, last_seen_at = ? WHERE feed_url = ? AND entry_id = ?",
                        (
                            entry.title,
                            entry.updated_at,
                            entry.permalink,
                            entry.author_name,
                            entry.author_email,
                            entry.parent_entry_id,
                            entry.parent_url,
                            json.dumps(entry.errors, separators=(",", ":")),
                            now,
                            feed_url,
                            entry.entry_id,
                        ),
                    )
                else:
                    new_entries += 1
                    connection.execute(
                        "INSERT INTO atom_entries"
                        " (feed_url, entry_id, title, updated_at, permalink, author_name,"
                        " author_email, parent_entry_id, parent_url, errors_json, first_seen_at,"
                        " last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            feed_url,
                            entry.entry_id,
                            entry.title,
                            entry.updated_at,
                            entry.permalink,
                            entry.author_name,
                            entry.author_email,
                            entry.parent_entry_id,
                            entry.parent_url,
                            json.dumps(entry.errors, separators=(",", ":")),
                            now,
                            now,
                        ),
                    )
            if checkpoint_safe:
                connection.execute(
                    "INSERT INTO feed_checkpoints (feed_url, feed_updated_at, last_polled_at)"
                    " VALUES (?, ?, ?) ON CONFLICT (feed_url) DO UPDATE SET"
                    " feed_updated_at = excluded.feed_updated_at,"
                    " last_polled_at = excluded.last_polled_at",
                    (feed_url, feed.updated_at, now),
                )
        return FeedPollSummary(new_entries, replayed_entries, invalid_entries)

    def nntp_checkpoint(self, source: str) -> NNTPCheckpoint | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT source, bootstrap_at, cutoff, window_hours, checked_through"
                " FROM nntp_checkpoints WHERE source = ?",
                (source,),
            ).fetchone()
        return NNTPCheckpoint(**dict(row)) if row else None

    def nntp_number_cursor(self, source: str) -> NNTPNumberCursor | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT source, uid_validity, baseline_high, next_article FROM nntp_number_cursors WHERE source = ?",
                (source,),
            ).fetchone()
        return NNTPNumberCursor(**dict(row)) if row else None

    def nntp_queue_status(
        self, source: str, *, limit: int = 100, after: int = 0
    ) -> NNTPQueueStatus:
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= 1000
            or not isinstance(after, int)
            or isinstance(after, bool)
            or not 0 <= after <= 9223372036854775807
        ):
            raise ValueError("NNTP queue inspection cursor or limit is invalid")
        with self._connection() as connection:
            connection.execute("BEGIN")
            checkpoint = connection.execute(
                "SELECT source, bootstrap_at, cutoff, window_hours, checked_through FROM nntp_checkpoints WHERE source = ?",
                (source,),
            ).fetchone()
            if not checkpoint:
                raise ValueError("No NNTP checkpoint exists for this source")
            cursor = connection.execute(
                "SELECT source, uid_validity, baseline_high, next_article FROM nntp_number_cursors WHERE source = ?",
                (source,),
            ).fetchone()
            pending, received = connection.execute(
                "SELECT COALESCE(SUM(received = 0), 0), COALESCE(SUM(received = 1), 0)"
                " FROM nntp_message_queue WHERE source = ?",
                (source,),
            ).fetchone()
            rows = connection.execute(
                "SELECT rowid, message_id FROM nntp_message_queue"
                " WHERE source = ? AND received = 0 AND rowid > ? ORDER BY rowid LIMIT ?",
                (source, after, limit + 1),
            ).fetchall()
        return NNTPQueueStatus(
            NNTPCheckpoint(**dict(checkpoint)),
            NNTPNumberCursor(**dict(cursor)) if cursor else None,
            pending,
            received,
            tuple(row["message_id"] for row in rows[:limit]),
            rows[limit - 1]["rowid"] if len(rows) > limit else None,
        )

    def begin_nntp_number_cursor(
        self, source: str, uid_validity: int, high: int
    ) -> NNTPNumberCursor:
        if (
            any(
                not isinstance(value, int)
                or isinstance(value, bool)
                or not 0 <= value < 9223372036854775807
                for value in (uid_validity, high)
            )
            or uid_validity == 0
        ):
            raise ValueError(
                "NNTP group validity and high article number must be valid integers"
            )
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO nntp_number_cursors (source, uid_validity, baseline_high, next_article)"
                " VALUES (?, ?, ?, ?) ON CONFLICT (source) DO NOTHING",
                (source, uid_validity, high, high + 1),
            )
            row = connection.execute(
                "SELECT source, uid_validity, baseline_high, next_article FROM nntp_number_cursors WHERE source = ?",
                (source,),
            ).fetchone()
        return NNTPNumberCursor(**dict(row))

    def record_nntp_number_batch(
        self,
        source: str,
        uid_validity: int,
        previous: int,
        through: int,
        articles: tuple[tuple[int, str], ...],
    ) -> int:
        if (
            any(
                not isinstance(value, int) or isinstance(value, bool)
                for value in (uid_validity, previous, through)
            )
            or not 1 <= previous <= through < 9223372036854775807
            or through - previous >= 1000
        ):
            raise ValueError(
                "NNTP article range must contain between 1 and 1000 valid numbers"
            )
        if tuple(number for number, _ in articles) != tuple(
            range(previous, through + 1)
        ):
            raise ArchiveCheckpointConflict(
                "NNTP article range is incomplete or out of order"
            )
        identifiers = tuple(
            _normalize_nntp_message_id(message_id) for _, message_id in articles
        )
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "UPDATE nntp_number_cursors SET next_article = ? WHERE source = ? AND uid_validity = ? AND next_article = ?",
                (through + 1, source, uid_validity, previous),
            )
            if cursor.rowcount != 1:
                raise ArchiveCheckpointConflict(
                    "NNTP numeric cursor changed during discovery"
                )
            before = connection.total_changes
            connection.executemany(
                "INSERT INTO nntp_message_queue (source, message_id) VALUES (?, ?) ON CONFLICT DO NOTHING",
                ((source, identifier) for identifier in identifiers),
            )
            return connection.total_changes - before

    def record_nntp_incident(
        self, source: str, kind: str, expected: int, observed: int
    ) -> None:
        if kind not in (
            "validity_changed",
            "high_decreased",
            "low_increased",
            "range_gap",
        ) or any(
            not isinstance(value, int)
            or isinstance(value, bool)
            or not 0 <= value < 9223372036854775807
            for value in (expected, observed)
        ):
            raise ValueError(
                "NNTP incident requires a recognized kind and valid article numbers"
            )
        with self._connection() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO nntp_incidents (source, kind, expected, observed, detected_at) VALUES (?, ?, ?, ?, ?)",
                (source, kind, expected, observed, _utc_now()),
            )

    def begin_nntp_bootstrap(
        self, source: str, now: datetime, *, hours: int = 240
    ) -> NNTPCheckpoint:
        if (
            not isinstance(source, str)
            or not source.startswith("nntps://")
            or any(character.isspace() for character in source)
        ):
            raise ValueError("NNTP source must be a non-empty TLS source identifier")
        if (
            not isinstance(now, datetime)
            or now.tzinfo is None
            or now.utcoffset() is None
        ):
            raise ValueError("Bootstrap time must include a timezone")
        if (
            not isinstance(hours, int)
            or isinstance(hours, bool)
            or not 1 <= hours <= 8760
        ):
            raise ValueError("Bootstrap hours must be between 1 and 8760")
        started = now.astimezone(timezone.utc).replace(microsecond=0)
        cutoff = (started - timedelta(hours=hours)).isoformat()
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO nntp_checkpoints (source, bootstrap_at, cutoff, window_hours, checked_through)"
                " VALUES (?, ?, ?, ?, ?) ON CONFLICT (source) DO NOTHING",
                (source, started.isoformat(), cutoff, hours, cutoff),
            )
            row = connection.execute(
                "SELECT source, bootstrap_at, cutoff, window_hours, checked_through"
                " FROM nntp_checkpoints WHERE source = ?",
                (source,),
            ).fetchone()
            if row["window_hours"] != hours:
                raise ArchiveCheckpointConflict(
                    "NNTP bootstrap window is already fixed with a different duration"
                )
        return NNTPCheckpoint(**dict(row))

    def extend_nntp_bootstrap(
        self, checkpoint: NNTPCheckpoint, hours: int, message_ids: tuple[str, ...]
    ) -> int:
        """Atomically widen a checkpoint and retain its backfill discoveries."""
        if type(hours) is not int or not checkpoint.window_hours < hours <= 8760:
            raise ValueError("Extended bootstrap hours must increase within 8760")
        cutoff = (
            datetime.fromisoformat(checkpoint.bootstrap_at) - timedelta(hours=hours)
        ).isoformat()
        normalized = tuple(_normalize_nntp_message_id(item) for item in message_ids)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "UPDATE nntp_checkpoints SET cutoff = ?, window_hours = ?"
                " WHERE source = ? AND bootstrap_at = ? AND cutoff = ?"
                " AND window_hours = ? AND checked_through = ?",
                (
                    cutoff,
                    hours,
                    checkpoint.source,
                    checkpoint.bootstrap_at,
                    checkpoint.cutoff,
                    checkpoint.window_hours,
                    checkpoint.checked_through,
                ),
            )
            if cursor.rowcount != 1:
                raise ArchiveCheckpointConflict(
                    "NNTP checkpoint changed during extension"
                )
            before = connection.total_changes
            connection.executemany(
                "INSERT INTO nntp_message_queue (source, message_id) VALUES (?, ?) ON CONFLICT DO NOTHING",
                ((checkpoint.source, message_id) for message_id in normalized),
            )
            return connection.total_changes - before

    def record_nntp_discovery(
        self,
        source: str,
        previous: str,
        through: datetime,
        message_ids: tuple[str, ...],
    ) -> int:
        if (
            not isinstance(through, datetime)
            or through.tzinfo is None
            or through.utcoffset() is None
        ):
            raise ValueError("NNTP discovery time must include a timezone")
        current = through.astimezone(timezone.utc).replace(microsecond=0).isoformat()
        if current < previous:
            raise ArchiveCheckpointConflict(
                "NNTP server clock moved behind its checkpoint"
            )
        normalized = tuple(
            _normalize_nntp_message_id(message_id) for message_id in message_ids
        )
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "UPDATE nntp_checkpoints SET checked_through = ? WHERE source = ? AND checked_through = ?",
                (current, source, previous),
            )
            if cursor.rowcount != 1:
                raise ArchiveCheckpointConflict(
                    "NNTP checkpoint changed during discovery"
                )
            before = connection.total_changes
            connection.executemany(
                "INSERT INTO nntp_message_queue (source, message_id) VALUES (?, ?) ON CONFLICT DO NOTHING",
                ((source, message_id) for message_id in normalized),
            )
            return connection.total_changes - before

    def pending_nntp_messages(
        self, source: str, *, limit: int = 100, after: int = 0
    ) -> tuple[str, ...]:
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= 1000
        ):
            raise ValueError("NNTP batch limit must be between 1 and 1000")
        if (
            not isinstance(after, int)
            or isinstance(after, bool)
            or not 0 <= after <= 9223372036854775807
        ):
            raise ValueError("NNTP queue cursor is invalid")
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT message_id FROM nntp_message_queue WHERE source = ? AND received = 0 AND rowid > ? ORDER BY rowid LIMIT ?",
                (source, after, limit),
            ).fetchall()
        return tuple(row["message_id"] for row in rows)

    def pending_nntp_message(self, source: str, message_id: str) -> str:
        normalized = _normalize_nntp_message_id(message_id)
        with self._connection() as connection:
            row = connection.execute(
                "SELECT message_id FROM nntp_message_queue WHERE source = ? AND message_id = ? AND received = 0",
                (source, normalized),
            ).fetchone()
        if not row:
            raise ValueError("Selected Message-ID is not pending for this NNTP source")
        return row["message_id"]

    def queue_nntp_message(self, source: str, message_id: str) -> int:
        """Queue one normalized article only after source initialization."""
        normalized = _normalize_nntp_message_id(message_id)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            checkpoint = connection.execute(
                "SELECT 1 FROM nntp_checkpoints WHERE source = ?", (source,)
            ).fetchone()
            if not checkpoint:
                raise ValueError("Run discover-nntp before queuing a selected article")
            before = connection.total_changes
            connection.execute(
                "INSERT INTO nntp_message_queue (source, message_id) VALUES (?, ?) ON CONFLICT DO NOTHING",
                (source, normalized),
            )
            return connection.total_changes - before

    def nntp_message_received(self, source: str, message_id: str) -> bool:
        message_id = _normalize_nntp_message_id(message_id)
        with self._connection() as connection:
            row = connection.execute(
                "SELECT received FROM nntp_message_queue WHERE source = ? AND message_id = ?",
                (source, message_id),
            ).fetchone()
        return bool(row and row["received"])

    def repair_nntp_parents(
        self, source: str, *, after: int = 0, limit: int = 100
    ) -> NNTPParentRepair:
        if (
            not isinstance(after, int)
            or isinstance(after, bool)
            or not 0 <= after <= 9223372036854775807
            or not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= 1000
        ):
            raise ValueError("NNTP parent repair cursor or limit is invalid")
        with self.worker_lock():
            self.assert_unconflicted(())
            if self.status().conflicted_message_ids:
                raise MessageConflictError(
                    "Message-ID conflicts require inspection before NNTP parent repair"
                )
            if not self.nntp_checkpoint(source):
                raise ValueError("No NNTP checkpoint exists for this source")
            with self._connection() as connection:
                rows = connection.execute(
                    "SELECT rowid, message_id FROM nntp_message_queue"
                    " WHERE source = ? AND received = 1 AND rowid > ? ORDER BY rowid LIMIT ?",
                    (source, after, limit + 1),
                ).fetchall()
            queued = sum(
                self.mark_nntp_received(source, row["message_id"])
                for row in rows[:limit]
            )
            return NNTPParentRepair(
                source,
                len(rows[:limit]),
                queued,
                rows[limit - 1]["rowid"] if len(rows) > limit else None,
            )

    def queue_nntp_thread_messages(
        self,
        source: str,
        message_id: str,
        identifiers: tuple[str, ...],
        *,
        search_range: tuple[str, int | None, int | None, bool] | None = None,
    ) -> int:
        anchor = _normalize_nntp_message_id(message_id)
        normalized = tuple(
            dict.fromkeys(
                _normalize_nntp_message_id(identifier) for identifier in identifiers
            )
        )
        if search_range is None and anchor not in normalized:
            raise ValueError("Thread discovery must include its requested Message-ID")
        if search_range is not None:
            inbox_url, first, last, empty = search_range
            parsed = urlsplit(inbox_url) if isinstance(inbox_url, str) else None
            if (
                not parsed
                or parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
                or any(
                    ord(character) < 32 or ord(character) == 127
                    for character in inbox_url
                )
                or not isinstance(empty, bool)
                or empty != (not normalized)
                or (first is None and last is None)
                or any(
                    value is not None
                    and (
                        not isinstance(value, int)
                        or isinstance(value, bool)
                        or not 0 <= value <= 9007199254740991
                    )
                    for value in (first, last)
                )
                or (first is not None and last is not None and first > last)
            ):
                raise ValueError("Invalid NNTP thread search range")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT received FROM nntp_message_queue WHERE source = ? AND message_id = ?",
                (source, anchor),
            ).fetchone()
            if not row or not row["received"]:
                raise ValueError(
                    "Fetch the thread anchor from this NNTP group before discovering its thread"
                )
            before = connection.total_changes
            connection.executemany(
                "INSERT INTO nntp_message_queue (source, message_id) VALUES (?, ?) ON CONFLICT DO NOTHING",
                ((source, identifier) for identifier in normalized),
            )
            queued = connection.total_changes - before
            if search_range is not None:
                connection.execute(
                    "INSERT INTO nntp_thread_searches (source, anchor_message_id, inbox_url, from_index_time,"
                    " through_index_time, candidate_count, empty_or_unavailable, scanned_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT (source, anchor_message_id, inbox_url, from_index_time, through_index_time)"
                    " DO UPDATE SET candidate_count = excluded.candidate_count,"
                    " empty_or_unavailable = excluded.empty_or_unavailable, scanned_at = excluded.scanned_at",
                    (
                        source,
                        anchor,
                        inbox_url.rstrip("/"),
                        first if first is not None else -1,
                        last if last is not None else -1,
                        len(normalized),
                        int(empty),
                        _utc_now(),
                    ),
                )
            return queued

    def list_nntp_thread_searches(
        self,
        source: str,
        message_id: str,
        *,
        after: int = 0,
        limit: int = 100,
    ) -> NNTPThreadSearchPage:
        anchor = _normalize_nntp_message_id(message_id)
        if (
            not isinstance(after, int)
            or isinstance(after, bool)
            or after < 0
            or not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= 1000
        ):
            raise ValueError("Thread search cursor or limit is invalid")
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT rowid, * FROM nntp_thread_searches WHERE source = ? AND anchor_message_id = ?"
                " AND rowid > ? ORDER BY rowid LIMIT ?",
                (source, anchor, after, limit + 1),
            ).fetchall()
        searches = tuple(
            NNTPThreadSearch(
                row["rowid"],
                row["source"],
                row["anchor_message_id"],
                row["inbox_url"],
                row["from_index_time"] if row["from_index_time"] != -1 else None,
                row["through_index_time"] if row["through_index_time"] != -1 else None,
                row["candidate_count"],
                bool(row["empty_or_unavailable"]),
                row["scanned_at"],
            )
            for row in rows[:limit]
        )
        return NNTPThreadSearchPage(
            searches, searches[-1].cursor if len(rows) > limit else None
        )

    def mark_nntp_received(self, source: str, message_id: str) -> int:
        message_id = _normalize_nntp_message_id(message_id)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT raw_message FROM messages WHERE message_id = ?",
                (message_id,),
            ).fetchone()
            if not row:
                raise ArchiveCheckpointConflict(
                    "NNTP source message must be stored before acknowledging it"
                )
            message = parse_message(row["raw_message"])
            if message.message_id != message_id:
                raise ArchiveCheckpointConflict(
                    "Stored NNTP source message does not match its Message-ID"
                )
            self._index_message_links(connection, message_id, message)
            cursor = connection.execute(
                "UPDATE nntp_message_queue SET received = 1 WHERE source = ? AND message_id = ?"
                " AND EXISTS(SELECT 1 FROM messages WHERE message_id = ?)",
                (source, message_id, message_id),
            )
            if cursor.rowcount != 1:
                raise ArchiveCheckpointConflict(
                    "NNTP source message must be stored before acknowledging it"
                )
            parents = tuple(
                _normalize_nntp_message_id(row[0])
                for row in connection.execute(
                    "SELECT DISTINCT parent_id FROM message_links WHERE child_id = ? ORDER BY parent_id",
                    (message_id,),
                )
                if row[0] != message_id
            )
            before = connection.total_changes
            connection.executemany(
                "INSERT INTO nntp_message_queue (source, message_id) VALUES (?, ?) ON CONFLICT DO NOTHING",
                ((source, parent) for parent in parents),
            )
            return connection.total_changes - before

    def archive_checkpoint(self, source: str) -> str | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT commit_id FROM archive_checkpoints WHERE source = ?", (source,)
            ).fetchone()
        return row["commit_id"] if row else None

    def advance_archive_checkpoint(
        self, source: str, previous: str | None, commit_id: str
    ) -> None:
        with self._connection() as connection:
            if previous is None:
                cursor = connection.execute(
                    "INSERT INTO archive_checkpoints (source, commit_id, updated_at)"
                    " VALUES (?, ?, ?) ON CONFLICT (source) DO NOTHING",
                    (source, commit_id, _utc_now()),
                )
            else:
                cursor = connection.execute(
                    "UPDATE archive_checkpoints SET commit_id = ?, updated_at = ?"
                    " WHERE source = ? AND commit_id = ?",
                    (commit_id, _utc_now(), source, previous),
                )
            if cursor.rowcount != 1:
                raise ArchiveCheckpointConflict(f"Archive checkpoint changed: {source}")

    def backfill_checkpoint(self, source: str) -> BackfillCheckpoint | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT commit_id, complete FROM archive_backfill_checkpoints WHERE source = ?",
                (source,),
            ).fetchone()
        return (
            BackfillCheckpoint(row["commit_id"], bool(row["complete"])) if row else None
        )

    def advance_backfill_checkpoint(
        self,
        source: str,
        previous: str | None,
        commit_id: str,
        *,
        complete: bool,
    ) -> None:
        with self._connection() as connection:
            if previous is None:
                cursor = connection.execute(
                    "INSERT INTO archive_backfill_checkpoints (source, commit_id, complete, updated_at)"
                    " VALUES (?, ?, ?, ?) ON CONFLICT (source) DO NOTHING",
                    (source, commit_id, int(complete), _utc_now()),
                )
            else:
                cursor = connection.execute(
                    "UPDATE archive_backfill_checkpoints SET commit_id = ?, complete = ?, updated_at = ?"
                    " WHERE source = ? AND commit_id = ?",
                    (commit_id, int(complete), _utc_now(), source, previous),
                )
            if cursor.rowcount != 1:
                raise ArchiveCheckpointConflict(
                    f"Archive backfill checkpoint changed: {source}"
                )

    def identity_mapping(self, api_url: str, email: str) -> IdentityMapping | None:
        normalized = normalize_email(email)
        with self._connection() as connection:
            row = connection.execute(
                "SELECT user_id, login FROM identity_mappings WHERE api_url = ? AND email = ?",
                (api_url, normalized),
            ).fetchone()
        return (
            IdentityMapping(api_url, normalized, row["user_id"], row["login"])
            if row
            else None
        )

    def set_identity_mapping(
        self,
        api_url: str,
        email: str,
        user_id: int,
        login: str,
        *,
        actor: str,
        reason: str,
        replace: bool = False,
    ) -> IdentityMapping:
        normalized = normalize_email(email)
        actor, reason = _identity_audit_values(actor, reason)
        if (
            not api_url
            or not isinstance(user_id, int)
            or isinstance(user_id, bool)
            or user_id < 1
            or not isinstance(login, str)
            or not login.strip()
            or len(login) > 255
            or any(ord(character) < 32 or ord(character) == 127 for character in login)
        ):
            raise ValueError("Identity mapping requires a server, user ID, and login")
        mapping = IdentityMapping(api_url, normalized, user_id, login)
        with self._connection() as connection:
            existing = connection.execute(
                "SELECT user_id, login FROM identity_mappings WHERE api_url = ? AND email = ?",
                (api_url, normalized),
            ).fetchone()
            if existing and (existing["user_id"], existing["login"]) == (
                user_id,
                login,
            ):
                return mapping
            if existing and not replace:
                raise ValueError(
                    "Email is already mapped; replacing it requires explicit approval"
                )
            now = _utc_now()
            if existing:
                cursor = connection.execute(
                    "UPDATE identity_mappings SET user_id = ?, login = ?, updated_at = ?"
                    " WHERE api_url = ? AND email = ? AND user_id = ? AND login = ?",
                    (
                        user_id,
                        login,
                        now,
                        api_url,
                        normalized,
                        existing["user_id"],
                        existing["login"],
                    ),
                )
            else:
                cursor = connection.execute(
                    "INSERT INTO identity_mappings (api_url, email, user_id, login, updated_at)"
                    " VALUES (?, ?, ?, ?, ?) ON CONFLICT (api_url, email) DO NOTHING",
                    (api_url, normalized, user_id, login, now),
                )
            if cursor.rowcount != 1:
                raise ValueError(
                    "Identity mapping changed concurrently; inspect and retry"
                )
            connection.execute(
                "INSERT INTO identity_mapping_audit (api_url, email, action, user_id, login, actor, reason, created_at)"
                " VALUES (?, ?, 'set', ?, ?, ?, ?, ?)",
                (api_url, normalized, user_id, login, actor, reason, now),
            )
        return mapping

    def remove_identity_mapping(
        self, api_url: str, email: str, *, actor: str, reason: str
    ) -> IdentityMapping:
        normalized = normalize_email(email)
        actor, reason = _identity_audit_values(actor, reason)
        with self._connection() as connection:
            row = connection.execute(
                "SELECT user_id, login FROM identity_mappings WHERE api_url = ? AND email = ?",
                (api_url, normalized),
            ).fetchone()
            if not row:
                raise ValueError("Email has no identity mapping for this server")
            cursor = connection.execute(
                "DELETE FROM identity_mappings WHERE api_url = ? AND email = ? AND user_id = ? AND login = ?",
                (api_url, normalized, row["user_id"], row["login"]),
            )
            if cursor.rowcount != 1:
                raise ValueError(
                    "Identity mapping changed concurrently; inspect and retry"
                )
            connection.execute(
                "INSERT INTO identity_mapping_audit (api_url, email, action, user_id, login, actor, reason, created_at)"
                " VALUES (?, ?, 'remove', ?, ?, ?, ?, ?)",
                (
                    api_url,
                    normalized,
                    row["user_id"],
                    row["login"],
                    actor,
                    reason,
                    _utc_now(),
                ),
            )
        return IdentityMapping(api_url, normalized, row["user_id"], row["login"])

    def list_publications(
        self,
        *,
        series_key: str | None = None,
        destination: str | None = None,
        status: str | None = None,
        after: int = 0,
        limit: int = 100,
    ) -> PublicationPage:
        if not isinstance(after, int) or isinstance(after, bool) or after < 0:
            raise ValueError("Publication cursor must be a non-negative integer")
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= 1000
        ):
            raise ValueError("Publication limit must be between 1 and 1000")
        if status is not None and status not in (
            "prepared",
            "branch_ready",
            "pr_pending",
            "uncertain",
            "complete",
        ):
            raise ValueError("Invalid publication status")
        conditions = ["rowid > ?"]
        values: list[Any] = [after]
        for field, value in (
            ("series_key", series_key),
            ("destination", destination),
            ("status", status),
        ):
            if value is not None:
                if not isinstance(value, str) or not value.strip():
                    raise ValueError("Publication filters must be non-empty strings")
                conditions.append(f"{field} = ?")
                values.append(value)
        values.append(limit + 1)
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT rowid AS cursor, destination, series_key, revision, approval_digest, base_commit,"
                " revision_branch, head_commit, status, pull_index, last_error, updated_at FROM publications"
                f" WHERE {' AND '.join(conditions)} ORDER BY rowid LIMIT ?",
                values,
            ).fetchall()
        entries = tuple(
            PublicationEntry(
                row["cursor"],
                Publication(
                    **{field: row[field] for field in Publication.__dataclass_fields__}
                ),
                row["updated_at"],
            )
            for row in rows[:limit]
        )
        return PublicationPage(
            entries, entries[-1].cursor if len(rows) > limit else None
        )

    def publication(
        self, destination: str, series_key: str, revision: int
    ) -> Publication | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT destination, series_key, revision, approval_digest, base_commit,"
                " revision_branch, head_commit, status, pull_index, last_error FROM publications"
                " WHERE destination = ? AND series_key = ? AND revision = ?",
                (destination, series_key, revision),
            ).fetchone()
        return Publication(**dict(row)) if row else None

    def publications_for_pull(
        self, destination: str, pull_index: int
    ) -> tuple[Publication, ...]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT destination, series_key, revision, approval_digest, base_commit,"
                " revision_branch, head_commit, status, pull_index, last_error FROM publications"
                " WHERE destination = ? AND pull_index = ? AND status = 'complete' ORDER BY series_key, revision",
                (destination, pull_index),
            ).fetchall()
        return tuple(Publication(**dict(row)) for row in rows)

    def move_legacy_deliveries(
        self,
        deliveries: tuple[Delivery, ...],
        destination: str,
        thread_bindings: dict[int, dict[str, Any]],
        *,
        actor: str,
        reason: str,
        digest: str,
    ) -> int:
        actor, reason = _identity_audit_values(actor, reason)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for delivery in deliveries:
                if delivery.write_state != "pending" or delivery.attempts != 0:
                    raise DeliveryStateConflict(
                        "Only untouched pending legacy deliveries can be migrated"
                    )
                payload = json.dumps(
                    delivery.payload, sort_keys=True, separators=(",", ":")
                )
                thread = thread_bindings.get(delivery.id)
                if not thread or (
                    "thread" in delivery.payload
                    and delivery.payload["thread"] != thread
                ):
                    raise DeliveryStateConflict(
                        "Legacy delivery thread provenance is not verified"
                    )
                updated_payload = json.dumps(
                    {**delivery.payload, "thread": thread},
                    sort_keys=True,
                    separators=(",", ":"),
                )
                collision = connection.execute(
                    "SELECT 1 FROM outbox WHERE message_id = ? AND event_type = ? AND destination = ? AND effect_key = ? AND id != ?",
                    (
                        delivery.message_id,
                        delivery.event_type,
                        destination,
                        delivery.effect_key,
                        delivery.id,
                    ),
                ).fetchone()
                if collision:
                    raise DeliveryStateConflict(
                        "Destination already contains the same delivery effect"
                    )
                cursor = connection.execute(
                    "UPDATE outbox SET destination = ?, payload = ? WHERE id = ? AND destination = ? AND message_id = ?"
                    " AND event_type = ? AND effect_key = ? AND payload = ? AND attempts = 0"
                    " AND write_state = 'pending' AND delivered_at IS NULL AND last_error IS ?",
                    (
                        destination,
                        updated_payload,
                        delivery.id,
                        delivery.destination,
                        delivery.message_id,
                        delivery.event_type,
                        delivery.effect_key,
                        payload,
                        delivery.last_error,
                    ),
                )
                if cursor.rowcount != 1:
                    raise DeliveryStateConflict(
                        "Legacy delivery changed during migration"
                    )
                connection.execute(
                    "INSERT INTO outbox_destination_audit"
                    " (delivery_id, previous_destination, destination, actor, reason, approval_digest, migrated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        delivery.id,
                        delivery.destination,
                        destination,
                        actor,
                        reason,
                        digest,
                        _utc_now(),
                    ),
                )
            return len(deliveries)

    def assert_publication_sources(self, destination: str, pull_index: int) -> None:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT series_key, revision FROM publications WHERE destination = ? AND pull_index = ? AND status = 'complete'",
                (destination, pull_index),
            ).fetchall()
        for row in rows:
            recorded = self.series_revision(row["series_key"], row["revision"])
            if (
                not recorded
                or not recorded.complete
                or recorded.conflicts
                or not recorded.messages
            ):
                raise PublicationConflict(
                    "Published series membership is unavailable or incomplete"
                )
            identifiers = tuple(
                message.message_id
                for message in recorded.messages
                if message.message_id
            )
            self.assert_unconflicted(identifiers)
            self.assert_message_ancestry_available(recorded.messages)
            if not identifiers or recorded not in self._discover_related_series(
                identifiers[0]
            ):
                raise PublicationConflict(
                    "Published series membership changed; inspect before delivering comments"
                )

    def begin_publication(
        self,
        destination: str,
        series_key: str,
        revision: int,
        approval_digest: str,
        base_commit: str,
        revision_branch: str,
        head_commit: str,
    ) -> Publication:
        values = (
            destination,
            series_key,
            revision,
            approval_digest,
            base_commit,
            revision_branch,
            head_commit,
        )
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO publications (destination, series_key, revision, approval_digest,"
                " base_commit, revision_branch, head_commit, status, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 'prepared', ?)"
                " ON CONFLICT (destination, series_key, revision) DO NOTHING",
                (*values, _utc_now()),
            )
            row = connection.execute(
                "SELECT destination, series_key, revision, approval_digest, base_commit,"
                " revision_branch, head_commit, status, pull_index, last_error FROM publications"
                " WHERE destination = ? AND series_key = ? AND revision = ?",
                values[:3],
            ).fetchone()
            publication = Publication(**dict(row))
            if (
                publication.approval_digest,
                publication.base_commit,
                publication.revision_branch,
                publication.head_commit,
            ) != values[3:]:
                raise PublicationConflict(
                    "Publication intent differs from the recorded revision"
                )
            return publication

    def transition_publication(
        self,
        destination: str,
        series_key: str,
        revision: int,
        *,
        expected: tuple[str, ...],
        status: str,
        pull_index: int | None = None,
        last_error: str | None = None,
    ) -> None:
        if not expected or status not in (
            "prepared",
            "branch_ready",
            "pr_pending",
            "uncertain",
            "complete",
        ):
            raise ValueError("Invalid publication transition")
        if status == "complete" and (
            not isinstance(pull_index, int)
            or isinstance(pull_index, bool)
            or pull_index < 1
        ):
            raise ValueError("Completed publication requires a pull request number")
        with self._connection() as connection:
            if pull_index is not None:
                conflicting = connection.execute(
                    "SELECT 1 FROM publications WHERE destination = ? AND pull_index = ?"
                    " AND series_key != ? LIMIT 1",
                    (destination, pull_index, series_key),
                ).fetchone()
                if conflicting:
                    raise PublicationConflict(
                        "Pull request is already mapped to another series"
                    )
            placeholders = ",".join("?" for _ in expected)
            cursor = connection.execute(
                "UPDATE publications SET status = ?, pull_index = COALESCE(?, pull_index),"
                " last_error = ?, updated_at = ? WHERE destination = ? AND series_key = ?"
                f" AND revision = ? AND status IN ({placeholders})"
                " AND (pull_index IS NULL OR ? IS NULL OR pull_index = ?)",
                (
                    status,
                    pull_index,
                    last_error,
                    _utc_now(),
                    destination,
                    series_key,
                    revision,
                    *expected,
                    pull_index,
                    pull_index,
                ),
            )
            if cursor.rowcount != 1:
                raise PublicationConflict(
                    "Publication state or pull request mapping changed"
                )

    def enqueue(
        self,
        message_id: str,
        event_type: str,
        destination: str,
        effect_key: str,
        payload: dict[str, Any],
        *,
        compatible_payload: dict[str, Any] | None = None,
    ) -> int:
        normalized_id = _normalize_message_id(message_id)
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        compatible = (
            json.dumps(compatible_payload, sort_keys=True, separators=(",", ":"))
            if compatible_payload is not None
            else None
        )
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO outbox"
                " (message_id, event_type, destination, effect_key, payload, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (message_id, event_type, destination, effect_key)"
                " DO NOTHING",
                (
                    normalized_id,
                    event_type,
                    destination,
                    effect_key,
                    serialized,
                    _utc_now(),
                ),
            )
            row = connection.execute(
                "SELECT id, payload FROM outbox WHERE message_id = ? AND event_type = ?"
                " AND destination = ? AND effect_key = ?",
                (normalized_id, event_type, destination, effect_key),
            ).fetchone()
            if row["payload"] != serialized and row["payload"] != compatible:
                raise DeliveryStateConflict(
                    "Queued delivery already has a different payload"
                )
            return int(row["id"])

    def has_legacy_inline_effects(self, message_id: str, destination: str) -> bool:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT payload FROM outbox WHERE message_id = ? AND destination = ? AND event_type = 'INLINE_COMMENT'",
                (_normalize_message_id(message_id), destination),
            )
            return any(
                json.loads(row[0]).get("payload_version", 1) != 2 for row in rows
            )

    def comment_effect_types(self, message_id: str, destination: str) -> frozenset[str]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT DISTINCT event_type FROM outbox WHERE message_id = ? AND destination = ?",
                (_normalize_message_id(message_id), destination),
            )
            return frozenset(row[0] for row in rows)

    def comment_effects(
        self, message_id: str, destination: str
    ) -> tuple[Delivery, ...]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT id, message_id, event_type, destination, effect_key, payload, attempts, last_error, write_state"
                " FROM outbox WHERE message_id = ? AND destination = ? ORDER BY id LIMIT 1001",
                (_normalize_message_id(message_id), destination),
            ).fetchall()
            if len(rows) > 1000:
                raise DeliveryStateConflict(
                    "Message effects exceed the bounded import lookup limit"
                )
            return tuple(
                Delivery(**{**dict(row), "payload": json.loads(row["payload"])})
                for row in rows
            )

    def pending(
        self,
        limit: int = 100,
        *,
        destination: str | None = None,
        event_type: str | None = None,
        after_id: int = 0,
        delivery_ids: tuple[int, ...] | None = None,
    ) -> list[Delivery]:
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= 1000
        ):
            raise ValueError("limit must be between 1 and 1000")
        if not isinstance(after_id, int) or isinstance(after_id, bool) or after_id < 0:
            raise ValueError("after_id must be a non-negative integer")
        if delivery_ids is not None and (
            not delivery_ids
            or len(delivery_ids) > limit
            or len(set(delivery_ids)) != len(delivery_ids)
            or any(
                not isinstance(value, int) or isinstance(value, bool) or value < 1
                for value in delivery_ids
            )
        ):
            raise ValueError(
                "Delivery IDs must be unique positive integers within the limit"
            )
        conditions = ["delivered_at IS NULL", "id > ?"]
        parameters: list[Any] = [after_id]
        if destination is not None:
            conditions.append("destination = ?")
            parameters.append(destination)
        if event_type is not None:
            conditions.append("event_type = ?")
            parameters.append(event_type)
        if delivery_ids is not None:
            conditions.append(f"id IN ({','.join('?' for _ in delivery_ids)})")
            parameters.extend(delivery_ids)
        parameters.append(limit)
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT id, message_id, event_type, destination, effect_key,"
                " payload, attempts, last_error, write_state FROM outbox"
                f" WHERE {' AND '.join(conditions)} ORDER BY id LIMIT ?",
                parameters,
            ).fetchall()
        return [
            Delivery(
                id=row["id"],
                message_id=row["message_id"],
                event_type=row["event_type"],
                destination=row["destination"],
                effect_key=row["effect_key"],
                payload=json.loads(row["payload"]),
                attempts=row["attempts"],
                last_error=row["last_error"],
                write_state=row["write_state"],
            )
            for row in rows
        ]

    def record_attempt(self, delivery_id: int) -> None:
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE outbox SET attempts = attempts + 1 WHERE id = ?"
                " AND delivered_at IS NULL",
                (delivery_id,),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"No pending delivery with id {delivery_id}")

    def begin_delivery(
        self, delivery_id: int, *, retry_uncertain: bool = False
    ) -> None:
        allowed = (
            ("pending", "sending", "uncertain") if retry_uncertain else ("pending",)
        )
        placeholders = ",".join("?" for _ in allowed)
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE outbox SET attempts = attempts + 1, write_state = 'sending'"
                " WHERE id = ? AND delivered_at IS NULL"
                f" AND write_state IN ({placeholders})",
                (delivery_id, *allowed),
            )
            if cursor.rowcount != 1:
                raise DeliveryStateConflict(
                    f"Delivery {delivery_id} is not safe to send"
                )

    def mark_delivery_uncertain(self, delivery_id: int, error: str) -> None:
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE outbox SET write_state = 'uncertain', last_error = ?"
                " WHERE id = ? AND delivered_at IS NULL AND write_state = 'sending'",
                (error, delivery_id),
            )
            if cursor.rowcount != 1:
                raise DeliveryStateConflict(f"Delivery {delivery_id} is not being sent")

    def record_error(self, delivery_id: int, error: str) -> None:
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE outbox SET last_error = ? WHERE id = ?"
                " AND delivered_at IS NULL",
                (error, delivery_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"No pending delivery with id {delivery_id}")

    def mark_delivered(self, delivery_id: int) -> None:
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE outbox SET delivered_at = ?, last_error = NULL, write_state = 'complete'"
                " WHERE id = ? AND delivered_at IS NULL",
                (_utc_now(), delivery_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"No pending delivery with id {delivery_id}")
