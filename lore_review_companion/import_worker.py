"""Policy-authorized bounded rounds; effect reconciliation stays in the ledger."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sqlite3
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from lore_review_companion.forge_api import ForgeAPI
from lore_review_companion.forge_imports import ForgeImportClient
from lore_review_companion.git_branches import GitHttpCredentials
from lore_review_companion.import_policy import ImportPolicy, read_import_policy
from lore_review_companion.ledger import Ledger
from lore_review_companion.nntp_source import (
    discover_nntp, discover_nntp_numbers, fetch_nntp, nntp_source,
)
from lore_review_companion.outbox import deliver_approved_imports, plan_import_delivery
from lore_review_companion.review_queue import queue_imported_replies
from lore_review_companion.series_sync import sync_imported_series_revision


@dataclass(frozen=True)
class ImportWorkerRound:
    policy_digest: str
    source_cursor: int
    publication_cursor: int
    considered: int
    published: int
    delivered: int
    diagnostics: tuple[str, ...]


def _private_descriptor(path: Path) -> int:
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        os.close(descriptor)
        raise ValueError("Worker cursor/lock must be private regular files owned by this user")
    return descriptor


@contextmanager
def _worker_state(path: Path, policy: ImportPolicy, ledger: Ledger):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = _private_descriptor(path.with_name(path.name + ".lock"))
    connection = None
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError("Another import worker round is already active") from error
        descriptor = _private_descriptor(path)
        os.close(descriptor)
        connection = sqlite3.connect(path, timeout=15)
        connection.execute("CREATE TABLE IF NOT EXISTS worker (id INTEGER PRIMARY KEY CHECK(id = 1), policy_digest TEXT NOT NULL, ledger_path TEXT NOT NULL, source_cursor INTEGER NOT NULL, publication_cursor INTEGER NOT NULL, revision_cursor INTEGER NOT NULL)")
        connection.execute("CREATE TABLE IF NOT EXISTS revisions (id INTEGER PRIMARY KEY, series_key TEXT NOT NULL, revision INTEGER NOT NULL, seed TEXT NOT NULL, UNIQUE(series_key, revision))")
        connection.execute("CREATE TABLE IF NOT EXISTS replies (series_key TEXT NOT NULL, revision INTEGER NOT NULL, position INTEGER NOT NULL, PRIMARY KEY(series_key, revision))")
        connection.execute("CREATE TABLE IF NOT EXISTS effects (delivery_id INTEGER PRIMARY KEY, fingerprint TEXT NOT NULL)")
        connection.execute("INSERT OR IGNORE INTO worker VALUES (1, ?, ?, 0, 0, 0)", (policy.digest(), str(ledger.path.resolve())))
        if connection.execute("SELECT policy_digest FROM worker WHERE id = 1").fetchone()[0] != policy.digest():
            raise ValueError("Worker policy changed; explicitly review and reset only the scheduling cursor database")
        if connection.execute("SELECT ledger_path FROM worker WHERE id = 1").fetchone()[0] != str(ledger.path.resolve()):
            raise ValueError("Worker scheduling state belongs to a different mail ledger")
        connection.commit()
        yield connection
    finally:
        if connection is not None:
            connection.close()
        os.close(lock)


def _effect_fingerprint(effect) -> str:
    value = (effect.message_id, effect.event_type, effect.destination, effect.effect_key, effect.payload)
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def run_import_worker_round(
    ledger: Ledger, repository: Path, policy_path: Path, state_path: Path,
    api: ForgeAPI, credentials: GitHttpCredentials, *, refresh_source: bool = True,
) -> ImportWorkerRound:
    incident = state_path.with_name(state_path.name + ".incident")
    if incident.exists():
        raise ValueError("Worker incident is latched; inspect the ledger before explicitly clearing its incident file")
    try:
        return _run_import_worker_round(ledger, repository, policy_path, state_path, api, credentials, refresh_source=refresh_source)
    except Exception:
        state_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            descriptor = os.open(incident, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            try:
                os.write(descriptor, (datetime.now(timezone.utc).isoformat() + "\nInspect durable publication/outbox state before recovery.\n").encode())
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except FileExistsError:
            pass
        raise


def _run_import_worker_round(
    ledger: Ledger, repository: Path, policy_path: Path, state_path: Path,
    api: ForgeAPI, credentials: GitHttpCredentials, *, refresh_source: bool = True,
) -> ImportWorkerRound:
    policy = read_import_policy(policy_path)
    if api.base_url != policy.api_url or credentials.username != policy.git_username:
        raise ValueError("Worker API/Git credentials do not match the reviewed destination policy")
    guard = lambda capabilities: policy.assert_current(policy_path, capabilities)
    reader = ForgeImportClient(api, policy.owner, policy.repository, source_url=policy.source_url,
                               unattended=True, capability_guard=guard)
    writer = ForgeImportClient(api, policy.owner, policy.repository, source_url=policy.source_url,
                               write_enabled=True, unattended=True, capability_guard=guard)
    with _worker_state(state_path, policy, ledger) as state:
        reader.capabilities()
        ledger.assert_unconflicted(())
        if ledger.status().conflicted_message_ids:
            raise ValueError("Source Message-ID conflicts require operator inspection before unattended imports")
        host, group = policy.nntp_host, policy.nntp_group
        source = nntp_source(host, group)
        if refresh_source:
            discover_nntp(ledger, host=host, group=group, hours=policy.lookback_hours)
            discover_nntp_numbers(ledger, host=host, group=group, limit=policy.message_limit)
            ledger.repair_nntp_parents(source, limit=policy.message_limit)
            fetch_nntp(ledger, host=host, group=group, limit=policy.message_limit)
        source_cursor, publication_cursor, revision_cursor = state.execute("SELECT source_cursor, publication_cursor, revision_cursor FROM worker WHERE id = 1").fetchone()
        page = ledger.list_messages(after=source_cursor, limit=policy.message_limit)
        destination = api.base_url + reader.repository_path
        publications = ledger.list_publications(destination=destination, after=publication_cursor, limit=policy.revision_limit)
        candidates = {}
        diagnostics = []
        for entry in publications.publications:
            published = entry.publication
            current = ledger.series_revision(published.series_key, published.revision)
            if current is None:
                raise ValueError("Worker publication has no retained source revision")
            candidates[(current.series_key, current.revision)] = current
        for item in page.messages:
            if item.metadata.get("kind") not in ("cover", "patch"):
                continue
            for current in ledger.discover_related_series(item.message_id):
                candidates[(current.series_key, current.revision)] = current
        published_count = delivered = 0
        for current in candidates.values():
            seed = current.cover_message_id or current.messages[0].message_id
            state.execute("INSERT OR IGNORE INTO revisions(series_key, revision, seed) VALUES (?, ?, ?)", (current.series_key, current.revision, seed))
        selected = state.execute("SELECT id, series_key, revision, seed FROM revisions WHERE id > ? ORDER BY id LIMIT ?",
                                 (revision_cursor, policy.revision_limit)).fetchall()
        if not selected:
            selected = state.execute("SELECT id, series_key, revision, seed FROM revisions ORDER BY id LIMIT ?", (policy.revision_limit,)).fetchall()
        for cursor, key, number, seed in selected:
            reader.capabilities()
            ledger.assert_unconflicted(())
            revisions = tuple(current for current in ledger.discover_related_series(seed) if current.series_key == key and current.revision == number)
            if len(revisions) != 1:
                raise ValueError("Worker revision no longer has one unambiguous retained source mapping")
            current = revisions[0]
            if not current.complete or current.conflicts:
                diagnostics.append(f"Incomplete revision: {current.series_key} v{current.revision}")
                continue
            previous = ledger.publication(destination, current.series_key, current.revision)
            if previous is not None and previous.status in ("pr_pending", "uncertain") and not policy.retry_uncertain:
                raise ValueError("Uncertain publication needs explicit policy authorization for same-key reconciliation")
            base = previous.base_commit if previous else policy.base_commit
            preview = sync_imported_series_revision(current, repository, base, policy.git_remote, reader,
                                                    target_branch=policy.target_branch, ledger=ledger, credentials=credentials,
                                                    retry_uncertain=policy.retry_uncertain)
            if previous is not None and previous.status == "complete" and (preview.refs.head.planned or preview.refs.base.planned):
                raise ValueError("Retained publication refs are missing; stop for operator recovery")
            reader.capabilities()
            result = sync_imported_series_revision(current, repository, base, policy.git_remote, writer,
                                                   target_branch=policy.target_branch, ledger=ledger, credentials=credentials,
                                                   approved_digest=preview.approval_digest, retry_uncertain=policy.retry_uncertain)
            published_count += int(previous is None or previous.status != "complete")
            pull_index = result.pull_request.remote_object["pull_index"]
            members = tuple(message.message_id for message in current.messages if message.message_id)
            replies = tuple(message for message in ledger._related_thread_messages(members) if message.kind == "reply")
            row = state.execute("SELECT position FROM replies WHERE series_key = ? AND revision = ?", (current.series_key, current.revision)).fetchone()
            position = row[0] if row is not None and row[0] < len(replies) else 0
            batch = replies[position:position + policy.message_limit]
            comment_destination = f"{destination}#{pull_index}"
            earlier_effects = {effect.id for message in batch for effect in ledger.comment_effects(message.message_id, comment_destination)}
            if batch:
                queued = queue_imported_replies(ledger, reader, pull_index, current.series_key, current.revision, repository,
                                                batch, max_inline_lines=policy.max_inline_lines)
                diagnostics.extend(queued.diagnostics)
            effects = tuple(effect for message in batch for effect in ledger.comment_effects(message.message_id, comment_destination))
            state.executemany("INSERT OR IGNORE INTO effects VALUES (?, ?)", ((effect.id, _effect_fingerprint(effect)) for effect in effects if effect.id not in earlier_effects))
            state.commit()
            enrolled = set()
            for offset in range(0, len(effects), 500):
                identifiers = tuple(effect.id for effect in effects[offset:offset + 500])
                retained = dict(state.execute(
                    f"SELECT delivery_id, fingerprint FROM effects WHERE delivery_id IN ({','.join('?' for _ in identifiers)})", identifiers))
                for effect in effects[offset:offset + 500]:
                    if effect.id in retained:
                        if retained[effect.id] != _effect_fingerprint(effect):
                            raise ValueError("Worker effect enrollment no longer matches the retained mail ledger")
                        enrolled.add(effect.id)
            if any(effect.id not in enrolled and effect.write_state != "complete" for effect in effects):
                diagnostics.append("Pre-existing import effects were not enrolled; keep them under explicit manual approval")
            for kind in ("IMPORT_CONVERSATION", "IMPORT_REVIEW", "IMPORT_REPLY"):
                identifiers = tuple(effect.id for effect in effects if effect.id in enrolled and effect.event_type == kind
                                    and effect.write_state != "complete")[:policy.message_limit]
                if not identifiers:
                    continue
                plan = plan_import_delivery(ledger, reader, pull_index, kind, limit=policy.message_limit,
                                            retry_uncertain=policy.retry_uncertain, delivery_ids=identifiers)
                if not plan.deliveries:
                    continue
                outcome = deliver_approved_imports(ledger, writer, pull_index, kind, approved_digest=plan.approval_digest(),
                                                  limit=policy.message_limit, retry_uncertain=policy.retry_uncertain, delivery_ids=identifiers)
                if outcome.failed:
                    raise ValueError("Import acceptance is uncertain; worker stopped with durable effects retained")
                delivered += outcome.delivered
            position = position + len(batch) if position + len(batch) < len(replies) else 0
            state.execute("INSERT INTO replies VALUES (?, ?, ?) ON CONFLICT(series_key, revision) DO UPDATE SET position = excluded.position",
                          (current.series_key, current.revision, position))
        next_source = page.messages[-1].cursor if page.next_cursor is not None else 0
        next_publication = publications.next_cursor or 0
        state.execute("UPDATE worker SET source_cursor = ?, publication_cursor = ?, revision_cursor = ? WHERE id = 1",
                  (next_source, next_publication, selected[-1][0] if selected else 0))
        state.commit()
        return ImportWorkerRound(policy.digest(), next_source, next_publication, len(selected), published_count, delivered, tuple(diagnostics))