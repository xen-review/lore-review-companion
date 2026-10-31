# SPDX-License-Identifier: GPL-2.0-or-later

"""Policy-authorized import processing with durable effect reconciliation."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from lore_review_companion.b4_adapter import B4SourceError
from lore_review_companion.forge_api import ForgeAPI, ForgeAPIError
from lore_review_companion.forge_imports import ForgeImportClient
from lore_review_companion.git_branches import GitHttpCredentials
from lore_review_companion.import_policy import ImportPolicy, read_import_policy
from lore_review_companion.ledger import Ledger, PublicationConflict
from lore_review_companion.mail_parser import has_excluded_recipient
from lore_review_companion.nntp_source import (
    discover_nntp,
    discover_nntp_numbers,
    discover_nntp_thread,
    fetch_nntp,
    nntp_source,
)
from lore_review_companion.outbox import (
    deliver_approved_imports, plan_import_delivery,
    deliver_import_failure_comments, queue_import_failure_comment,
)
from lore_review_companion.preflight import GitPreflightError
from lore_review_companion.review_queue import queue_imported_replies
from lore_review_companion.series import missing_patch_signoffs
from lore_review_companion.series_sync import sync_imported_series_revision

PAGE_SIZE = 1000
REVISION_PAGE_SIZE = 1000


def wait_for_resources(forge_path: Path) -> None:
    while True:
        load = os.getloadavg()[0]
        cpus = os.sysconf("SC_NPROCESSORS_ONLN")
        free = shutil.disk_usage(forge_path).free
        if load <= cpus and free >= 1_000_000_000:
            return
        time.sleep(10)


@dataclass(frozen=True)
class ImportWorkerRound:
    policy_digest: str
    source_cursor: int
    publication_cursor: int
    considered: int
    published: int
    delivered: int
    diagnostics: tuple[str, ...]
    backlog_pending: bool = False


def _private_descriptor(path: Path) -> int:
    descriptor = os.open(
        path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600
    )
    info = os.fstat(descriptor)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_mode & 0o077
    ):
        os.close(descriptor)
        raise ValueError(
            "Worker cursor/lock must be private regular files owned by this user"
        )
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
        connection.execute(
            "CREATE TABLE IF NOT EXISTS worker (id INTEGER PRIMARY KEY CHECK(id = 1), policy_digest TEXT NOT NULL, ledger_path TEXT NOT NULL, source_cursor INTEGER NOT NULL, publication_cursor INTEGER NOT NULL, revision_cursor INTEGER NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS revisions (id INTEGER PRIMARY KEY, series_key TEXT NOT NULL, revision INTEGER NOT NULL, seed TEXT NOT NULL, UNIQUE(series_key, revision))"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS replies (series_key TEXT NOT NULL, revision INTEGER NOT NULL, position INTEGER NOT NULL, PRIMARY KEY(series_key, revision))"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS effects (delivery_id INTEGER PRIMARY KEY, fingerprint TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT OR IGNORE INTO worker VALUES (1, ?, ?, 0, 0, 0)",
            (policy.digest(), str(ledger.path.resolve())),
        )
        if connection.execute("SELECT ledger_path FROM worker WHERE id = 1").fetchone()[
            0
        ] != str(ledger.path.resolve()):
            raise ValueError(
                "Worker scheduling state belongs to a different mail ledger"
            )
        connection.execute(
            "UPDATE worker SET policy_digest = ?, source_cursor = 0, publication_cursor = 0, revision_cursor = 0 WHERE id = 1 AND policy_digest != ?",
            (policy.digest(), policy.digest()),
        )
        connection.commit()
        yield connection
    finally:
        if connection is not None:
            connection.close()
        os.close(lock)


def _effect_fingerprint(effect) -> str:
    value = (
        effect.message_id,
        effect.event_type,
        effect.destination,
        effect.effect_key,
        effect.payload,
    )
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _ancestry_order(messages):
    pending = {message.message_id: message for message in messages}
    ordered = []
    while pending:
        ready = [
            message
            for message in pending.values()
            if not set(message.in_reply_to or message.references[-1:]) & pending.keys()
        ]
        if not ready:
            raise ValueError(
                "Reply ancestry is cyclic; publication discussion cannot be completed"
            )
        for message in ready:
            ordered.append(message)
            del pending[message.message_id]
    return tuple(ordered)


def _allowed_replies(messages):
    excluded = {
        message.message_id for message in messages if has_excluded_recipient(message)
    }
    while True:
        descendants = {
            message.message_id
            for message in messages
            if set(message.in_reply_to + message.references) & excluded
        } - excluded
        if not descendants:
            break
        excluded.update(descendants)
    return tuple(
        message for message in messages
        if message.kind == "reply" and message.message_id not in excluded
    )


def _message_issue_title(message):
    submitter = message.from_address or "unknown-sender"
    subject = message.subject.strip() or "(no subject)"
    return f"PR creation skipped for {submitter}: {subject}"


def _message_issue_exists(client, issue_title):
    page = 1
    while True:
        issues = client.api.request_json(
            f"{client.repository_path}/issues",
            params={"state": "all", "type": "issues", "q": issue_title, "limit": 50, "page": page},
        )
        if not isinstance(issues, list):
            raise ValueError("Forgejo returned an invalid issue list")
        if any(isinstance(issue, dict) and issue.get("title") == issue_title for issue in issues):
            return True
        if len(issues) < 50:
            return False
        page += 1


def _ensure_message_issue(client, message, reasons):
    issue_title = _message_issue_title(message)
    if _message_issue_exists(client, issue_title):
        return False
    client.capabilities()
    source = f"{client.source_url}{quote(message.message_id, safe='@')}/"
    body = f"{issue_title}\nLink: {source}\n\n" + "\n".join(
        f"- {reason}" for reason in reasons
    )
    if len(message.body_text.splitlines()) < 200:
        longest_fence = max(
            (len(match.group()) for match in re.finditer(r"`+", message.body_text)),
            default=0,
        )
        fence = "`" * max(3, longest_fence + 1)
        body += f"\n\n{fence}rust\n{message.body_text.rstrip()}\n{fence}"
    try:
        client.api.request_json(
            f"{client.repository_path}/issues", method="POST",
            payload={"title": issue_title, "body": body},
        )
    except ForgeAPIError:
        if not _message_issue_exists(client, issue_title):
            raise
    return True


def _recover_submission_thread(ledger, policy, seed, *, activity_guard=None):
    source = nntp_source(policy.nntp_host, policy.nntp_group)
    if not ledger.nntp_message_received(source, seed):
        fetch_nntp(
            ledger, host=policy.nntp_host, group=policy.nntp_group, message_id=seed
        )
    recovered = discover_nntp_thread(
        ledger,
        seed,
        inbox_url=policy.source_url,
        host=policy.nntp_host,
        group=policy.nntp_group,
    )
    if recovered.empty_or_unavailable or not recovered.message_ids:
        return False
    missing = tuple(
        identifier
        for identifier in recovered.message_ids
        if not ledger.nntp_message_received(source, identifier)
    )
    for identifier in missing:
        if activity_guard is not None:
            activity_guard()
        fetch_nntp(
            ledger,
            host=policy.nntp_host,
            group=policy.nntp_group,
            message_id=identifier,
        )
    return all(
        ledger.nntp_message_received(source, identifier)
        for identifier in recovered.message_ids
    )


def _deliver_reply_batch(
    ledger, state, reader, writer, policy, repository, current, pull_index, batch,
    activity_guard=None,
):
    destination = f"{reader.api.base_url}{reader.repository_path}#{pull_index}"
    delivered = 0
    diagnostics = []
    for message in batch:
        if activity_guard is not None:
            activity_guard()
        reports = deliver_import_failure_comments(ledger, writer, pull_index, message.message_id)
        delivered += reports.delivered
        if reports.failed:
            diagnostics.append(f"PR #{pull_index}: companion failure report delivery blocked for {message.message_id}")
        earlier = {
            effect.id
            for effect in ledger.comment_effects(message.message_id, destination)
        }
        queued = queue_imported_replies(
            ledger,
            reader,
            pull_index,
            current.series_key,
            current.revision,
            repository,
            (message,),
            max_inline_lines=policy.max_inline_lines,
        )
        diagnostics.extend(queued.diagnostics)
        effects = ledger.comment_effects(message.message_id, destination)
        state.executemany(
            "INSERT OR IGNORE INTO effects VALUES (?, ?)",
            (
                (effect.id, _effect_fingerprint(effect))
                for effect in effects
                if effect.id not in earlier
            ),
        )
        state.commit()
        enrolled = {}
        for offset in range(0, len(effects), 500):
            identifiers = tuple(effect.id for effect in effects[offset : offset + 500])
            enrolled.update(
                state.execute(
                    f"SELECT delivery_id, fingerprint FROM effects WHERE delivery_id IN ({','.join('?' for _ in identifiers)})",
                    identifiers,
                )
            )
        for effect in effects:
            if effect.id in enrolled and enrolled[effect.id] != _effect_fingerprint(
                effect
            ):
                raise ValueError(
                    "Worker effect enrollment no longer matches the retained mail ledger"
                )
        if any(
            effect.id not in enrolled and effect.write_state != "complete"
            and effect.event_type != "IMPORT_FAILURE_COMMENT"
            for effect in effects
        ):
            diagnostics.append(
                "Pre-existing import effects were not enrolled; keep them under explicit manual approval"
            )
        for kind in ("IMPORT_CONVERSATION", "IMPORT_REVIEW", "IMPORT_REPLY"):
            selected = tuple(
                effect.id
                for effect in effects
                if effect.id in enrolled
                and effect.event_type == kind
                and effect.write_state != "complete"
            )
            for offset in range(0, len(selected), PAGE_SIZE):
                identifiers = selected[offset : offset + PAGE_SIZE]
                try:
                    plan = plan_import_delivery(
                        ledger, reader, pull_index, kind,
                        limit=PAGE_SIZE,
                        retry_uncertain=policy.retry_uncertain,
                        delivery_ids=identifiers,
                    )
                except (ValueError, RuntimeError) as error:
                    for effect in effects:
                        if effect.id in identifiers:
                            queue_import_failure_comment(ledger, writer, pull_index, effect, error)
                    deliver_import_failure_comments(ledger, writer, pull_index, message.message_id)
                    raise
                if not plan.deliveries:
                    continue
                outcome = deliver_approved_imports(
                    ledger,
                    writer,
                    pull_index,
                    kind,
                    approved_digest=plan.approval_digest(),
                    limit=PAGE_SIZE,
                    retry_uncertain=policy.retry_uncertain,
                    delivery_ids=identifiers,
                )
                if outcome.failed:
                    raise ValueError(
                        "Import acceptance is uncertain; worker stopped with durable effects retained"
                    )
                delivered += outcome.delivered
    return delivered, diagnostics


def run_import_worker_round(
    ledger: Ledger,
    repository: Path,
    policy_path: Path,
    state_path: Path,
    api: ForgeAPI,
    credentials: GitHttpCredentials,
    *,
    refresh_source: bool = True,
    activity_guard=None,
    poll_source: bool = True,
) -> ImportWorkerRound:
    return _run_import_worker_round(
        ledger,
        repository,
        policy_path,
        state_path,
        api,
        credentials,
        refresh_source=refresh_source,
        activity_guard=activity_guard,
        poll_source=poll_source,
    )


def run_import_worker(
    ledger, repository, policy_path, state_path, api, credentials,
    *, forge_path: Path, refresh_source: bool = True,
):
    guard = lambda: wait_for_resources(forge_path)
    api.before_write = guard
    poll_source = True
    while True:
        try:
            result = run_import_worker_round(
                ledger, repository, policy_path, state_path, api, credentials,
                refresh_source=refresh_source, activity_guard=guard, poll_source=poll_source,
            )
            print(json.dumps({"considered": result.considered, "published": result.published,
                              "delivered": result.delivered, "diagnostics": result.diagnostics}), flush=True)
            poll_source = False
            if not result.backlog_pending:
                time.sleep(600)
                poll_source = True
        except Exception as error:
            print(json.dumps({"error_type": type(error).__name__, "error": str(error)}), flush=True)
            time.sleep(1)


def _run_import_worker_round(
    ledger: Ledger,
    repository: Path,
    policy_path: Path,
    state_path: Path,
    api: ForgeAPI,
    credentials: GitHttpCredentials,
    *,
    refresh_source: bool = True,
    activity_guard=None,
    poll_source: bool = True,
) -> ImportWorkerRound:
    if activity_guard is not None:
        activity_guard()
    policy = read_import_policy(policy_path)
    if api.base_url != policy.api_url or credentials.username != policy.git_username:
        raise ValueError(
            "Worker API/Git credentials do not match the reviewed destination policy"
        )
    guard = lambda capabilities: policy.assert_current(policy_path, capabilities)
    reader = ForgeImportClient(
        api,
        policy.owner,
        policy.repository,
        source_url=policy.source_url,
        unattended=True,
        capability_guard=guard,
    )
    writer = ForgeImportClient(
        api,
        policy.owner,
        policy.repository,
        source_url=policy.source_url,
        write_enabled=True,
        unattended=True,
        capability_guard=guard,
    )
    with _worker_state(state_path, policy, ledger) as state:
        reader.capabilities()
        ledger.assert_unconflicted(())
        if ledger.status().conflicted_message_ids:
            raise ValueError(
                "Source Message-ID conflicts require operator inspection before unattended imports"
            )
        host, group = policy.nntp_host, policy.nntp_group
        source = nntp_source(host, group)
        source_backlog = False
        if refresh_source:
            if poll_source:
                discover_nntp(ledger, host=host, group=group, hours=policy.lookback_hours)
            numeric = discover_nntp_numbers(
                ledger, host=host, group=group, limit=PAGE_SIZE
            )
            ledger.repair_nntp_parents(source, limit=PAGE_SIZE)
            fetched = fetch_nntp(ledger, host=host, group=group, limit=PAGE_SIZE)
            source_backlog = bool(numeric.has_more or (fetched.has_more and (fetched.ingested or fetched.replayed)))
        source_cursor, publication_cursor, revision_cursor = state.execute(
            "SELECT source_cursor, publication_cursor, revision_cursor FROM worker WHERE id = 1"
        ).fetchone()
        page = ledger.list_messages(after=source_cursor, limit=PAGE_SIZE)
        destination = api.base_url + reader.repository_path
        publications = ledger.list_publications(
            destination=destination,
            after=publication_cursor,
            limit=REVISION_PAGE_SIZE,
        )
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
            state.execute(
                "INSERT OR IGNORE INTO revisions(series_key, revision, seed) VALUES (?, ?, ?)",
                (current.series_key, current.revision, seed),
            )
        selected = state.execute(
            "SELECT id, series_key, revision, seed FROM revisions WHERE id > ? ORDER BY id LIMIT ?",
            (revision_cursor, REVISION_PAGE_SIZE),
        ).fetchall()
        if not selected:
            selected = state.execute(
                "SELECT id, series_key, revision, seed FROM revisions ORDER BY id LIMIT ?",
                (REVISION_PAGE_SIZE,),
            ).fetchall()
        for cursor, key, number, seed in selected:
            if activity_guard is not None:
                activity_guard()
            state.execute("UPDATE worker SET revision_cursor = ? WHERE id = 1", (cursor,))
            state.commit()
            reader.capabilities()
            ledger.assert_unconflicted(())
            revisions = tuple(
                current
                for current in ledger.discover_related_series(seed)
                if current.series_key == key and current.revision == number
            )
            if not revisions:
                continue
            if len(revisions) != 1:
                raise ValueError(
                    "Worker revision no longer has one unambiguous retained source mapping"
                )
            current = revisions[0]
            previous = ledger.publication(
                destination, current.series_key, current.revision
            )
            if previous is None and all(
                _message_issue_exists(reader, _message_issue_title(message))
                for message in current.messages if message.message_id
            ):
                continue
            if refresh_source and previous is None:
                recovered_thread = (
                    _recover_submission_thread(ledger, policy, seed)
                    if activity_guard is None else
                    _recover_submission_thread(ledger, policy, seed, activity_guard=activity_guard)
                )
                if not recovered_thread:
                    diagnostics.append(
                        f"Source recovery pending: {seed}; publication deferred"
                    )
                    continue
                recovered = tuple(
                    revision for revision in ledger.discover_related_series(seed)
                    if revision.series_key == key and revision.revision == number
                )
                if not recovered:
                    continue
                if len(recovered) != 1:
                    raise ValueError("Recovered submission mapping is ambiguous")
                current = recovered[0]
            if not current.complete or current.conflicts:
                for message in current.messages:
                    if message.message_id:
                        _ensure_message_issue(
                            writer, message,
                            current.conflicts or ("Submission members are missing",),
                        )
                diagnostics.append(
                    f"Incomplete submission revision; PR creation blocked: {current.title} "
                    f"[{current.series_key} v{current.revision}]; "
                    f"source: {policy.source_url}{seed}/; "
                    f"cause: {'; '.join(current.conflicts) or 'missing submission members'}"
                )
                continue
            if previous is None and any(
                _message_issue_exists(reader, _message_issue_title(message))
                for message in current.messages if message.message_id
            ):
                continue
            if previous is None:
                missing_signoffs = missing_patch_signoffs(current)
                for message in missing_signoffs:
                    if message.message_id:
                        _ensure_message_issue(writer, message, ("Patch has no valid Signed-off-by trailer",))
                if missing_signoffs:
                    diagnostics.append(
                        f"PR creation blocked: {current.title} v{current.revision}; missing Signed-off-by: "
                        + ", ".join(message.message_id or "unknown" for message in missing_signoffs)
                    )
                    continue
            if (
                previous is not None
                and previous.status in ("pr_pending", "uncertain")
                and not policy.retry_uncertain
            ):
                raise ValueError(
                    "Uncertain publication needs explicit policy authorization for same-key reconciliation"
                )
            base = previous.base_commit if previous else policy.base_commit
            try:
                preview = sync_imported_series_revision(
                    current,
                    repository,
                    base,
                    policy.git_remote,
                    reader,
                    target_branch=policy.target_branch,
                    ledger=ledger,
                    credentials=credentials,
                    retry_uncertain=policy.retry_uncertain,
                )
            except GitPreflightError as error:
                if previous is not None:
                    raise
                details = str(error).splitlines()
                failure = next(
                    (line for line in details if line.startswith("Patch failed at ")),
                    details[0] if details else type(error).__name__,
                )
                files = "; ".join(line for line in details if line.startswith("error:"))
                failed_message = next(
                    message for message in current.messages if message.message_id == seed
                )
                _ensure_message_issue(
                    writer, failed_message, tuple(part for part in (failure, files) if part)
                )
                diagnostics.append(
                    f"Preflight blocked: PR creation for {current.title} "
                    f"[{current.series_key} v{current.revision}]; "
                    f"source: {policy.source_url}{seed}/; {failure}; {files}"
                )
                continue
            except B4SourceError as error:
                if previous is not None:
                    raise
                reason = str(error)
                for message in current.messages:
                    if message.message_id:
                        _ensure_message_issue(writer, message, (reason,))
                diagnostics.append(
                    f"Assembly blocked: PR creation for {current.title} "
                    f"[{current.series_key} v{current.revision}]; "
                    f"source: {policy.source_url}{seed}/; cause: {reason}"
                )
                continue
            if (
                previous is not None
                and previous.status == "complete"
                and (preview.refs.head.planned or preview.refs.base.planned)
            ):
                raise ValueError(
                    "Retained publication refs are missing; stop for operator recovery"
                )
            members = tuple(
                message.message_id for message in current.messages if message.message_id
            )
            replies = _ancestry_order(
                _allowed_replies(ledger._related_thread_messages(members))
            )
            try:
                ledger.assert_message_ancestry_available(replies)
            except PublicationConflict as error:
                for message in current.messages:
                    if message.message_id:
                        _ensure_message_issue(writer, message, (str(error),))
                diagnostics.append(
                    f"PR creation blocked: {current.title} "
                    f"[{current.series_key} v{current.revision}]; "
                    f"source: {policy.source_url}{seed}/; cause: {error}"
                )
                continue
            reader.capabilities()
            if activity_guard is not None:
                activity_guard()
            result = sync_imported_series_revision(
                current,
                repository,
                base,
                policy.git_remote,
                writer,
                target_branch=policy.target_branch,
                ledger=ledger,
                credentials=credentials,
                approved_digest=preview.approval_digest,
                retry_uncertain=policy.retry_uncertain,
            )
            published_count += int(previous is None or previous.status != "complete")
            pull_index = result.pull_request.remote_object["pull_index"]
            row = state.execute(
                "SELECT position FROM replies WHERE series_key = ? AND revision = ?",
                (current.series_key, current.revision),
            ).fetchone()
            position = row[0] if row is not None and row[0] < len(replies) else 0
            batch = replies[position:]
            count, reply_diagnostics = _deliver_reply_batch(
                ledger,
                state,
                reader,
                writer,
                policy,
                repository,
                current,
                pull_index,
                batch,
                activity_guard=activity_guard,
            )
            delivered += count
            diagnostics.extend(reply_diagnostics)
            position = (
                position + len(batch) if position + len(batch) < len(replies) else 0
            )
            state.execute(
                "INSERT INTO replies VALUES (?, ?, ?) ON CONFLICT(series_key, revision) DO UPDATE SET position = excluded.position",
                (current.series_key, current.revision, position),
            )
        last_source = page.messages[-1].cursor if page.messages else source_cursor
        more_messages = bool(ledger.list_messages(after=last_source, limit=1).messages)
        next_source = last_source if page.next_cursor is not None or more_messages else 0
        next_publication = publications.next_cursor or 0
        last_revision = selected[-1][0] if selected else 0
        more_revisions = bool(state.execute(
            "SELECT 1 FROM revisions WHERE id > ? LIMIT 1", (last_revision,)
        ).fetchone())
        state.execute(
            "UPDATE worker SET source_cursor = ?, publication_cursor = ?, revision_cursor = ? WHERE id = 1",
            (next_source, next_publication, last_revision),
        )
        state.commit()
        return ImportWorkerRound(
            policy.digest(),
            next_source,
            next_publication,
            len(selected),
            published_count,
            delivered,
            tuple(diagnostics),
            bool(source_backlog or next_source or next_publication or more_revisions),
        )
