import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from lore_review_companion import ledger as ledger_module
from lore_review_companion.ingest import ingest_mbox
from lore_review_companion.ledger import SCHEMA_VERSION, ArchiveCheckpointConflict, ArchiveSafetyBlocked, DeliveryStateConflict, Ledger, MessageConflictError, PublicationConflict, WorkerBusyError
from lore_review_companion.mail_parser import parse_message
from lore_review_companion.series import discover_series


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.ledger = Ledger(Path(self.tempdir.name) / "state.sqlite3")
        self.raw = b"Message-ID: <series@example.org>\n\noriginal mail\n"
        self.ledger.record_message("<series@example.org>", self.raw)

    def test_identical_message_replay_is_idempotent(self):
        self.assertFalse(self.ledger.record_message("series@example.org", self.raw))
        with closing(self.ledger._connect()) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM messages").fetchone()[0], 1)

    def test_nntp_equivalent_sources_keep_both_raw_versions_in_either_order(self):
        source = b"Message-ID: <mail@example.test>\nSubject: folded\n subject\n\nbody\n"
        article = (
            b"Path: server\r\nNewsgroups: example.group\r\nMessage-ID: <mail@example.test>\r\n"
            b"Subject: folded subject\r\nXref: server example.group:1\r\n\r\nbody\r\n"
        )
        for nntp_first in (False, True):
            with self.subTest(nntp_first=nntp_first):
                ledger = Ledger(Path(self.tempdir.name) / f"sources-{nntp_first}.sqlite3")
                first, second = (article, source) if nntp_first else (source, article)
                first_metadata = {"transport": "nntp"} if nntp_first else {}
                second_metadata = {} if nntp_first else {"transport": "nntp"}
                self.assertTrue(ledger.record_message("mail@example.test", first, first_metadata))
                self.assertFalse(ledger.record_message("mail@example.test", second, second_metadata))
                self.assertFalse(ledger.record_message("mail@example.test", second, second_metadata))
                self.assertFalse(ledger.record_message("mail@example.test", second))
                with closing(ledger._connect()) as connection:
                    primary = connection.execute("SELECT raw_message FROM messages").fetchone()[0]
                    variants = connection.execute("SELECT raw_message FROM message_source_variants").fetchall()
                self.assertEqual(primary, first)
                self.assertEqual([row[0] for row in variants], [second])
                with self.assertRaises(MessageConflictError):
                    ledger.record_message("mail@example.test", second.replace(b"body", b"changed"), second_metadata)
                self.assertEqual(ledger.status().conflicted_message_ids, 1)

    def test_new_ledger_files_are_private_without_changing_existing_permissions(self):
        path = Path(self.tempdir.name) / "private" / "state.sqlite3"
        previous = os.umask(0)
        try:
            ledger = Ledger(path)
            with ledger.worker_lock():
                pass
        finally:
            os.umask(previous)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        lock_path = Path(str(path) + ".worker-lock.sqlite3")
        self.assertEqual(lock_path.stat().st_mode & 0o777, 0o600)
        path.chmod(0o640)
        Ledger(path)
        self.assertEqual(path.stat().st_mode & 0o777, 0o640)

    def test_read_only_ledger_rejects_writes(self):
        read_only = Ledger(self.ledger.path, read_only=True)
        self.assertEqual(read_only.status().messages, 1)
        with self.assertRaises(sqlite3.OperationalError):
            read_only.record_unmatched_message(b"unkeyed", ("missing ID",))

    def test_schema_upgrade_preserves_queued_payload_and_delivery_identity(self):
        for version in (18, 21, 22):
            with self.subTest(version=version):
                path = Path(self.tempdir.name) / f"replay-{version}.sqlite3"
                ledger = Ledger(path)
                ledger.record_message("review@example.test", b"original")
                ledger.enqueue("review@example.test", "INLINE_COMMENT", "destination", "effect", {
                    "body": "review", "head_commit": "a" * 40,
                    "anchor": {"path": "x.c", "old_position": 0, "new_position": 3},
                })
                before = ledger.pending()
                with ledger._connection() as connection:
                    connection.execute("UPDATE schema_version SET version = ?", (version,))
                reopened = Ledger(path)
                self.assertEqual(reopened.pending(), before)
                self.assertEqual(Ledger(path, read_only=True).pending(), before)

    def test_schema_setup_failure_rolls_back_tables_and_version(self):
        with self.ledger._connection() as connection:
            connection.execute("UPDATE schema_version SET version = 18")
        broken_schema = ledger_module._SCHEMA + (
            "\nCREATE TABLE migration_probe (id INTEGER);\nSELECT * FROM missing_migration_table;"
        )
        with patch.object(ledger_module, "_SCHEMA", broken_schema):
            with self.assertRaises(sqlite3.OperationalError):
                Ledger(self.ledger.path)
        with closing(self.ledger._connect()) as connection:
            self.assertEqual(connection.execute("SELECT version FROM schema_version").fetchone()[0], 18)
            self.assertIsNone(connection.execute(
                "SELECT name FROM sqlite_master WHERE name = 'migration_probe'",
            ).fetchone())
        self.assertEqual(Ledger(self.ledger.path).status().messages, 1)

    def test_unsupported_schema_does_not_create_or_modify_tables(self):
        path = Path(self.tempdir.name) / "future.sqlite3"
        with closing(sqlite3.connect(path)) as connection:
            connection.executescript(
                "CREATE TABLE schema_version (version INTEGER NOT NULL);"
                "INSERT INTO schema_version VALUES (999);"
            )
        with self.assertRaisesRegex(RuntimeError, "Unsupported ledger schema"):
            Ledger(path)
        with closing(sqlite3.connect(path)) as connection:
            self.assertEqual(connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall(), [
                ("schema_version",),
            ])
            self.assertEqual(connection.execute("SELECT version FROM schema_version").fetchone()[0], 999)

    def test_nntp_bootstrap_freezes_148_hours_in_utc_across_restarts(self):
        source = "nntps://nntp.lore.kernel.org/org.xenproject.lists.xen-devel"
        started = datetime(2026, 10, 28, 12, tzinfo=timezone(timedelta(hours=1)))
        checkpoint = self.ledger.begin_nntp_bootstrap(source, started)
        self.assertEqual(checkpoint.bootstrap_at, "2026-10-28T11:00:00+00:00")
        self.assertEqual(checkpoint.cutoff, "2026-10-22T07:00:00+00:00")
        self.assertEqual(checkpoint.checked_through, checkpoint.cutoff)
        reopened = Ledger(self.ledger.path)
        self.assertEqual(reopened.begin_nntp_bootstrap(source, started + timedelta(days=1)), checkpoint)
        self.assertEqual(reopened.nntp_checkpoint(source), checkpoint)
        with self.assertRaisesRegex(ArchiveCheckpointConflict, "already fixed"):
            reopened.begin_nntp_bootstrap(source, started, hours=120)
        with self.assertRaisesRegex(ValueError, "timezone"):
            reopened.begin_nntp_bootstrap(source, datetime(2026, 10, 28))

    def test_nntp_discovery_durably_queues_before_advancing_and_acknowledging(self):
        source = "nntps://nntp.lore.kernel.org/group"
        now = datetime(2026, 10, 28, 11, tzinfo=timezone.utc)
        checkpoint = self.ledger.begin_nntp_bootstrap(source, now)
        self.assertEqual(self.ledger.record_nntp_discovery(source, checkpoint.cutoff, now, ("<new@example.test>",)), 1)
        reopened = Ledger(self.ledger.path)
        self.assertEqual(reopened.pending_nntp_messages(source), ("new@example.test",))
        self.assertEqual(reopened.record_nntp_discovery(source, now.isoformat(), now, ("new@example.test",)), 0)
        with self.assertRaisesRegex(ArchiveCheckpointConflict, "changed"):
            reopened.record_nntp_discovery(source, checkpoint.cutoff, now, ("other@example.test",))
        with self.assertRaisesRegex(ValueError, "invalid Message-ID"):
            reopened.record_nntp_discovery(source, now.isoformat(), now, ("invalid",))
        with self.assertRaisesRegex(ValueError, "invalid Message-ID"):
            reopened.record_nntp_discovery(source, now.isoformat(), now, ("bad\x00@example.test",))
        self.assertEqual(reopened.pending_nntp_messages(source), ("new@example.test",))
        with self.assertRaisesRegex(ArchiveCheckpointConflict, "must be stored"):
            reopened.mark_nntp_received(source, "new@example.test")
        reopened.record_message("new@example.test", b"Message-ID: <new@example.test>\n\nMail.\n")
        reopened.mark_nntp_received(source, "new@example.test")
        self.assertEqual(reopened.pending_nntp_messages(source), ())

    def test_nntp_numeric_cursor_queues_complete_ranges_and_latches_incidents(self):
        source = "nntps://nntp.lore.kernel.org/group"
        now = datetime(2026, 10, 28, 11, tzinfo=timezone.utc)
        self.ledger.begin_nntp_bootstrap(source, now)
        cursor = self.ledger.begin_nntp_number_cursor(source, 1234, 100)
        self.assertEqual((cursor.baseline_high, cursor.next_article), (100, 101))
        self.assertEqual(self.ledger.begin_nntp_number_cursor(source, 1234, 102), cursor)
        self.assertEqual(self.ledger.record_nntp_number_batch(source, 1234, 101, 102, (
            (101, "first@example.test"), (102, "second@example.test"),
        )), 2)
        reopened = Ledger(self.ledger.path)
        self.assertEqual(reopened.nntp_number_cursor(source).next_article, 103)
        with self.assertRaisesRegex(ArchiveCheckpointConflict, "changed"):
            reopened.record_nntp_number_batch(source, 1234, 101, 101, ((101, "first@example.test"),))
        with self.assertRaisesRegex(ArchiveCheckpointConflict, "incomplete"):
            reopened.record_nntp_number_batch(source, 1234, 103, 105, ((103, "third@example.test"), (105, "fifth@example.test")))
        self.assertEqual(reopened.nntp_number_cursor(source).next_article, 103)
        reopened.record_nntp_incident(source, "range_gap", 104, 105)
        self.assertEqual(reopened.status().nntp_incidents[0].kind, "range_gap")
        with self.assertRaisesRegex(ArchiveSafetyBlocked, "NNTP range_gap"):
            reopened.assert_unconflicted(())

    def test_status_reports_local_state_without_loading_message_bodies(self):
        self.ledger.record_unmatched_message(b"unkeyed", ("missing ID",))
        with self.assertRaises(MessageConflictError):
            self.ledger.record_message("series@example.org", b"different")
        self.ledger.enqueue("series@example.org", "ISSUE_COMMENT", "destination", "effect", {"body": "review"})
        self.ledger.record_archive_deletion("/archive/epoch", "d" * 40)
        status = self.ledger.status()
        self.assertEqual((status.messages, status.unmatched_messages, status.conflicted_message_ids), (1, 1, 1))
        self.assertTrue(status.link_index_complete)
        self.assertEqual(status.deliveries, {"pending": 1})
        self.assertEqual(status.archive_deletions[0].commit_id, "d" * 40)

    def test_unmatched_retention_requires_exact_approval_and_records_audit(self):
        self.ledger.record_unmatched_message(b"old unkeyed", ("missing ID",))
        with self.ledger._connection() as connection:
            connection.execute("UPDATE unmatched_messages SET received_at = ?", ("2020-01-01T00:00:00+00:00",))
        cutoff = "2021-01-01T00:00:00Z"
        plan = self.ledger.preview_unmatched_retention(cutoff, "operator", "retention policy")
        self.assertEqual(len(plan.entries), 1)
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.ledger.prune_unmatched(cutoff, "operator", "changed reason", approved_digest=plan.approval_digest())
        self.assertEqual(len(self.ledger.unmatched_messages()), 1)
        result = self.ledger.prune_unmatched(cutoff, "operator", "retention policy", approved_digest=plan.approval_digest())
        self.assertEqual(result.removed, 1)
        self.assertEqual(self.ledger.unmatched_messages(), [])
        with closing(self.ledger._connect()) as connection:
            audit = connection.execute("SELECT sha256, actor, reason FROM unmatched_retention_audit").fetchall()
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 1)
        self.assertEqual(tuple(audit[0]), (plan.entries[0].sha256, "operator", "retention policy"))
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.ledger.prune_unmatched(cutoff, "operator", "retention policy", approved_digest=plan.approval_digest())

    def test_select_and_page_recorded_mail_without_loading_unselected_bodies(self):
        first = parse_message(
            b"Message-ID: <patch-1@example.test>\nSubject: [PATCH 1/2] first\n\nPatch one.\n"
        )
        second = parse_message(
            b"Message-ID: <patch-2@example.test>\nSubject: [PATCH 2/2] second\n\nPatch two.\n"
        )
        self.ledger.record_parsed_message(first)
        self.ledger.record_parsed_message(second)
        page = self.ledger.list_messages(kind="patch", limit=1)
        self.assertEqual(page.messages[0].message_id, first.message_id)
        self.assertIsNotNone(page.next_cursor)
        next_page = self.ledger.list_messages(kind="patch", after=page.next_cursor, limit=1)
        self.assertEqual(next_page.messages[0].message_id, second.message_id)
        self.assertIsNone(next_page.next_cursor)
        self.assertEqual(self.ledger.messages_by_id((second.message_id, first.message_id)), (second, first))
        with self.assertRaisesRegex(ValueError, "not in the ledger"):
            self.ledger.messages_by_id(("missing@example.test",))
        with self.assertRaisesRegex(ValueError, "distinct"):
            self.ledger.messages_by_id((first.message_id, first.message_id))
        with self.assertRaises(MessageConflictError):
            self.ledger.record_message(first.message_id, b"conflicting")
        self.assertTrue(self.ledger.list_messages(kind="patch").messages[0].conflicted)
        with self.assertRaises(MessageConflictError):
            self.ledger.messages_by_id((first.message_id,))

    def test_revision_link_requires_approval_and_remains_bound_to_source_membership(self):
        first = parse_message(
            b"From: Author <author@example.test>\nMessage-ID: <v1@example.test>\n"
            b"Subject: [PATCH 1/1] change\n\nchange-id: stable-change\n"
        )
        second = parse_message(
            b"From: Author <author@example.test>\nMessage-ID: <v2@example.test>\n"
            b"Subject: [PATCH v2 1/1] change\n\nchange-id: stable-change\n"
        )
        for item in (first, second):
            self.ledger.record_parsed_message(item)
        canonical = discover_series((first,))[0]
        self.ledger.record_series_revision(canonical)
        arguments = (canonical.series_key, 1, second.message_id, 2, "operator", "reviewed change-ID")
        plan = self.ledger.plan_revision_link(*arguments)
        self.assertEqual(plan.change_id, "stable-change")
        self.assertNotEqual(self.ledger.discover_related_series(second.message_id)[0].series_key, canonical.series_key)
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.ledger.link_revision(*arguments, approved_digest="0" * 64)
        self.ledger.link_revision(*arguments, approved_digest=plan.approval_digest())
        (audit,) = self.ledger.revision_links().links
        self.assertEqual((audit.plan["actor"], audit.approval_digest), ("operator", plan.approval_digest()))
        self.assertNotIn("manual_claims", audit.plan)
        linked = self.ledger.discover_related_series(second.message_id)[0]
        self.assertEqual((linked.series_key, linked.root_message_id), (canonical.series_key, canonical.root_message_id))
        competing = parse_message(
            b"Message-ID: <competing@example.test>\nSubject: [PATCH v2 1/1] other\n\nchange-id: stable-change\n"
        )
        self.ledger.record_parsed_message(competing)
        with self.assertRaisesRegex(PublicationConflict, "already linked"):
            self.ledger.plan_revision_link(canonical.series_key, 1, competing.message_id, 2, "operator", "reviewed")
        self.ledger.record_series_revision(linked)
        self.assertEqual(Ledger(self.ledger.path).series_revision(canonical.series_key, 2), linked)
        self.ledger.link_revision(*arguments, approved_digest=plan.approval_digest())
        sibling = parse_message(
            b"Message-ID: <sibling@example.test>\nSubject: [PATCH v2 1/1] other\n"
            b"In-Reply-To: <v2@example.test>\n\nchange-id: stable-change\n"
        )
        self.ledger.record_parsed_message(sibling)
        with self.assertRaisesRegex(PublicationConflict, "membership changed"):
            self.ledger.discover_related_series(second.message_id)

    def test_change_id_scan_is_bounded_and_reports_untrusted_claims(self):
        first = parse_message(
            b"Message-ID: <first@example.test>\nSubject: [PATCH 1/1] change\n\nchange-id: stable\n"
        )
        second = parse_message(
            b"Message-ID: <second@example.test>\nSubject: [PATCH v2 1/1] change\n\n"
            b"change-id: stable\nchange-id: bad value\n"
        )
        for item in (first, second):
            self.ledger.record_parsed_message(item)
        with self.assertRaises(MessageConflictError):
            self.ledger.record_message(second.message_id, b"conflict")
        initial = self.ledger.search_change_id("stable", limit=2)
        self.assertEqual(initial.scanned, 2)
        self.assertEqual([item.message_id for item in initial.matches], [first.message_id])
        self.assertIsNotNone(initial.next_cursor)
        following = self.ledger.search_change_id("stable", after=initial.next_cursor, limit=2)
        (match,) = following.matches
        self.assertEqual(match.message_id, second.message_id)
        self.assertTrue(match.invalid_claims)
        self.assertTrue(match.conflicted)
        self.assertIsNone(following.next_cursor)

    def test_revision_link_rejects_mismatched_malformed_and_occupied_candidates(self):
        canonical_message = parse_message(
            b"Message-ID: <canonical@example.test>\nSubject: [PATCH 1/1] change\n\nchange-id: stable\n"
        )
        self.ledger.record_parsed_message(canonical_message)
        canonical = discover_series((canonical_message,))[0]
        self.ledger.record_series_revision(canonical)
        bodies = (b"change-id: other\n", b"change-id: stable\nchange-id: other\n", b"change-id: stable\nchange-id: bad value\n")
        for index, body in enumerate(bodies, 2):
            candidate = parse_message(
                f"Message-ID: <candidate-{index}@example.test>\nSubject: [PATCH v{index} 1/1] change\n\n".encode() + body
            )
            self.ledger.record_parsed_message(candidate)
            with self.subTest(body=body), self.assertRaises(PublicationConflict):
                self.ledger.plan_revision_link(canonical.series_key, 1, candidate.message_id, index, "operator", "reviewed")
            if index > 2:
                with self.subTest(body=body), self.assertRaisesRegex(PublicationConflict, "invalid or multiple"):
                    self.ledger.plan_revision_link(
                        canonical.series_key, 1, candidate.message_id, index, "operator", "reviewed",
                        allow_unmatched_change_id=True,
                    )
        assigned = parse_message(
            b"Message-ID: <assigned@example.test>\nSubject: [PATCH v5 1/1] change\n\nchange-id: stable\n"
        )
        self.ledger.record_parsed_message(assigned)
        self.ledger.record_series_revision(discover_series((assigned,))[0])
        with self.assertRaisesRegex(PublicationConflict, "already assigned"):
            self.ledger.plan_revision_link(canonical.series_key, 1, assigned.message_id, 5, "operator", "reviewed")

    def test_manual_revision_link_binds_claims_and_requires_explicit_approval(self):
        first = parse_message(b"Message-ID: <v1@example.test>\nSubject: [PATCH 1/1] change\n\nchange-id: original\n")
        second = parse_message(b"Message-ID: <v2@example.test>\nSubject: [PATCH v2 1/1] change\n\nchange-id: different\n")
        for item in (first, second):
            self.ledger.record_parsed_message(item)
        canonical = discover_series((first,))[0]
        self.ledger.record_series_revision(canonical)
        arguments = (canonical.series_key, 1, second.message_id, 2, "operator", "reviewed patch history")
        with self.assertRaisesRegex(PublicationConflict, "explicitly reviewed"):
            self.ledger.plan_revision_link(*arguments)
        plan = self.ledger.plan_revision_link(*arguments, allow_unmatched_change_id=True)
        self.assertIsNone(plan.change_id)
        self.assertEqual((plan.manual_claims.canonical_change_id, plan.manual_claims.candidate_change_id), ("original", "different"))
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.ledger.link_revision(*arguments, approved_digest="0" * 64, allow_unmatched_change_id=True)
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.ledger.link_revision(*arguments[:-1], "changed reason", approved_digest=plan.approval_digest(), allow_unmatched_change_id=True)
        self.ledger.link_revision(*arguments, approved_digest=plan.approval_digest(), allow_unmatched_change_id=True)
        (audit,) = self.ledger.revision_links().links
        self.assertEqual(audit.plan["manual_claims"], {"canonical_change_id": "original", "candidate_change_id": "different"})
        self.assertEqual(self.ledger.discover_related_series(second.message_id)[0].series_key, canonical.series_key)
        sibling = parse_message(
            b"Message-ID: <sibling@example.test>\nSubject: [PATCH v2 1/1] other\n"
            b"In-Reply-To: <v2@example.test>\n\nchange-id: different\n"
        )
        self.ledger.record_parsed_message(sibling)
        with self.assertRaisesRegex(PublicationConflict, "membership changed"):
            self.ledger.discover_related_series(second.message_id)

    def test_manual_revision_link_allows_absent_claims(self):
        for index, (first_body, second_body) in enumerate((
            (b"", b""), (b"change-id: original\n", b""), (b"", b"change-id: candidate\n"),
        ), 1):
            first = parse_message(
                f"Message-ID: <original-{index}@example.test>\nSubject: [PATCH 1/1] change\n\n".encode() + first_body
            )
            second = parse_message(
                f"Message-ID: <candidate-{index}@example.test>\nSubject: [PATCH v2 1/1] change\n\n".encode() + second_body
            )
            for item in (first, second):
                self.ledger.record_parsed_message(item)
            canonical = discover_series((first,))[0]
            self.ledger.record_series_revision(canonical)
            arguments = (canonical.series_key, 1, second.message_id, 2, "operator", "reviewed history")
            with self.subTest(first=first_body, second=second_body):
                with self.assertRaisesRegex(PublicationConflict, "explicitly reviewed"):
                    self.ledger.plan_revision_link(*arguments)
                plan = self.ledger.plan_revision_link(*arguments, allow_unmatched_change_id=True)
                self.ledger.link_revision(*arguments, approved_digest=plan.approval_digest(), allow_unmatched_change_id=True)
                self.assertEqual(self.ledger.discover_related_series(second.message_id)[0].series_key, canonical.series_key)

    def test_revision_link_rechecks_the_canonical_thread(self):
        first = parse_message(b"Message-ID: <v1@example.test>\nSubject: [PATCH 1/1] change\n\nchange-id: stable\n")
        second = parse_message(b"Message-ID: <v2@example.test>\nSubject: [PATCH v2 1/1] change\n\nchange-id: stable\n")
        for item in (first, second):
            self.ledger.record_parsed_message(item)
        canonical = discover_series((first,))[0]
        self.ledger.record_series_revision(canonical)
        arguments = (canonical.series_key, 1, second.message_id, 2, "operator", "reviewed")
        plan = self.ledger.plan_revision_link(*arguments)
        self.ledger.link_revision(*arguments, approved_digest=plan.approval_digest())
        sibling = parse_message(
            b"Message-ID: <extra@example.test>\nSubject: [PATCH 1/1] duplicate\n"
            b"In-Reply-To: <v1@example.test>\n\nchange-id: stable\n"
        )
        self.ledger.record_parsed_message(sibling)
        with self.assertRaisesRegex(PublicationConflict, "canonical series membership changed"):
            self.ledger.discover_related_series(second.message_id)

    def test_publication_source_membership_cannot_be_overwritten(self):
        first = parse_message(b"Message-ID: <v1@example.test>\nSubject: [PATCH 1/1] change\n\nPatch.\n")
        self.ledger.record_parsed_message(first)
        canonical = discover_series((first,))[0]
        self.ledger.record_series_revision(canonical)
        self.ledger.begin_publication("server/repository", canonical.series_key, 1, "digest", "b" * 40, "branch", "a" * 40)
        sibling = parse_message(
            b"Message-ID: <extra@example.test>\nSubject: [PATCH 1/1] duplicate\n"
            b"In-Reply-To: <v1@example.test>\n\nPatch.\n"
        )
        self.ledger.record_parsed_message(sibling)
        (changed,) = discover_series((first, sibling))
        with self.assertRaisesRegex(PublicationConflict, "membership is immutable"):
            self.ledger.record_series_revision(changed)
        self.assertEqual(self.ledger.series_revision(canonical.series_key, 1), canonical)

    def test_identity_mapping_is_server_scoped_audited_and_explicitly_replaced(self):
        server = "https://gitea.example/api/v1/"
        mapping = self.ledger.set_identity_mapping(
            server, "Reviewer@EXAMPLE.test", 7, "reviewer", actor="operator", reason="verified mapping",
        )
        self.assertEqual(mapping.email, "Reviewer@example.test")
        self.assertEqual(self.ledger.identity_mapping(server, "Reviewer@example.test"), mapping)
        self.assertIsNone(self.ledger.identity_mapping(server, "reviewer@example.test"))
        self.assertIsNone(self.ledger.identity_mapping("https://other.example/api/v1/", mapping.email))
        with self.assertRaisesRegex(ValueError, "explicit approval"):
            self.ledger.set_identity_mapping(server, mapping.email, 8, "other", actor="operator", reason="change")
        replacement = self.ledger.set_identity_mapping(
            server, mapping.email, 8, "other", actor="operator", reason="change", replace=True,
        )
        self.assertEqual(self.ledger.remove_identity_mapping(server, mapping.email, actor="operator", reason="retired"), replacement)
        self.assertIsNone(self.ledger.identity_mapping(server, mapping.email))
        with closing(self.ledger._connect()) as connection:
            actions = connection.execute("SELECT action, actor FROM identity_mapping_audit ORDER BY id").fetchall()
        self.assertEqual([tuple(row) for row in actions], [("set", "operator"), ("set", "operator"), ("remove", "operator")])

    def test_identity_mapping_rejects_ambiguous_addresses_and_missing_audit_reason(self):
        for address in ("", "a..b@example.test", "a@example.test,other@example.test", "a@-example.test"):
            with self.subTest(address=address), self.assertRaises(ValueError):
                self.ledger.set_identity_mapping("server", address, 1, "user", actor="operator", reason="valid")
        with self.assertRaisesRegex(ValueError, "reason"):
            self.ledger.set_identity_mapping("server", "a@example.test", 1, "user", actor="operator", reason="")

    def test_conflicting_message_id_is_quarantined(self):
        with self.assertRaises(MessageConflictError):
            self.ledger.record_message("series@example.org", b"different bytes")
        with closing(self.ledger._connect()) as connection:
            self.assertEqual(
                connection.execute("SELECT count(*) FROM message_conflicts").fetchone()[0],
                1,
            )
        self.assertFalse(self.ledger.record_message("series@example.org", self.raw))
        with self.assertRaisesRegex(MessageConflictError, "Quarantined"):
            self.ledger.assert_unconflicted(("series@example.org",))

    def test_outbox_deduplicates_effects_and_retries(self):
        delivery_id = self.ledger.enqueue(
            "series@example.org",
            "NEW_REPLY",
            "owner/repo#42",
            "reply@example.org",
            {"body": "Review comment", "url": "https://lore.example/reply"},
        )
        self.assertEqual(
            delivery_id,
            self.ledger.enqueue(
                "series@example.org",
                "NEW_REPLY",
                "owner/repo#42",
                "reply@example.org",
                {"url": "https://lore.example/reply", "body": "Review comment"},
            ),
        )
        self.ledger.record_attempt(delivery_id)
        self.ledger.record_error(delivery_id, "temporary API error")
        pending = self.ledger.pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].attempts, 1)
        self.assertEqual(pending[0].last_error, "temporary API error")

        self.ledger.mark_delivered(delivery_id)
        self.assertEqual(self.ledger.pending(), [])

    def test_outbox_requires_ingested_source(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.ledger.enqueue("unknown@example.org", "NEW_REPLY", "repo", "x", {})

    def test_outbox_tracks_uncertain_writes_and_rejects_changed_payload(self):
        delivery_id = self.ledger.enqueue("series@example.org", "ISSUE_COMMENT", "repo", "reply", {"body": "one"})
        with self.assertRaisesRegex(DeliveryStateConflict, "different payload"):
            self.ledger.enqueue("series@example.org", "ISSUE_COMMENT", "repo", "reply", {"body": "two"})
        self.ledger.begin_delivery(delivery_id)
        self.assertEqual(self.ledger.pending()[0].write_state, "sending")
        self.ledger.mark_delivery_uncertain(delivery_id, "network interrupted")
        with self.assertRaises(DeliveryStateConflict):
            self.ledger.begin_delivery(delivery_id)
        self.ledger.begin_delivery(delivery_id, retry_uncertain=True)
        self.assertEqual(self.ledger.pending()[0].attempts, 2)
        self.ledger.mark_delivered(delivery_id)
        self.assertEqual(self.ledger.pending(), [])

    def test_mbox_ingest_persists_metadata_unmatched_mail_and_conflicts(self):
        mailbox_path = Path(self.tempdir.name) / "thread.mbox"
        first = (
            b"From: Ada <ada@example.test>\n"
            b"Message-ID: <ingest@example.test>\n"
            b"Subject: [PATCH v2 1/1] xen: improve\n"
            b"Content-Type: text/plain; charset=utf-8\n\n"
            b"Change.\n\nReviewed-by: Ada <ada@example.test>\n---\ndiff --git a/x b/x\n"
        )
        missing_id = (
            b"From: Unknown <unknown@example.test>\n"
            b"Subject: unkeyed reply\n"
            b"Content-Type: text/plain; charset=utf-8\n\n"
            b"Could not identify this message.\n"
        )
        mailbox_path.write_bytes(
            b"From archive@example.test Tue Oct 28 00:00:00 2026\n"
            + first
            + b"\nFrom archive@example.test Tue Oct 28 00:01:00 2026\n"
            + missing_id
        )

        summary = ingest_mbox(mailbox_path, self.ledger)
        replay = ingest_mbox(mailbox_path, self.ledger)

        self.assertEqual((summary.ingested, summary.unmatched), (1, 1))
        self.assertEqual((summary.revisions_discovered, summary.complete_revisions), (1, 1))
        self.assertEqual((replay.replayed, replay.unmatched), (1, 1))
        self.assertEqual(replay.revisions_discovered, 1)
        unmatched = self.ledger.unmatched_messages()
        self.assertEqual(len(unmatched), 1)
        self.assertIn("Missing or invalid Message-ID", unmatched[0].errors)
        with closing(self.ledger._connect()) as connection:
            row = connection.execute(
                "SELECT metadata_json FROM messages WHERE message_id = ?",
                ("ingest@example.test",),
            ).fetchone()
        self.assertIn('"patch_version":2', row[0])

        mailbox_path.write_bytes(
            b"From archive@example.test Tue Oct 28 00:02:00 2026\n"
            + first.replace(b"Change.\n", b"Conflicting change.\n")
        )
        conflict = ingest_mbox(mailbox_path, self.ledger)
        self.assertEqual(conflict.conflicts, 1)
        self.assertEqual(conflict.revisions_discovered, 0)
        with closing(self.ledger._connect()) as connection:
            self.assertEqual(
                connection.execute("SELECT count(*) FROM message_conflicts").fetchone()[0],
                1,
            )

    def test_version_one_ledger_migrates_in_place(self):
        path = Path(self.tempdir.name) / "legacy.sqlite3"
        connection = sqlite3.connect(path)
        connection.executescript(
            "CREATE TABLE schema_version (version INTEGER NOT NULL);"
            "INSERT INTO schema_version VALUES (1);"
            "CREATE TABLE messages (message_id TEXT PRIMARY KEY, sha256 TEXT NOT NULL,"
            " raw_message BLOB NOT NULL, received_at TEXT NOT NULL);"
        )
        connection.close()

        migrated = Ledger(path)

        with closing(migrated._connect()) as connection:
            version = connection.execute("SELECT version FROM schema_version").fetchone()[0]
            columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(messages)")
            }
        self.assertEqual(version, SCHEMA_VERSION)
        self.assertIn("metadata_json", columns)

    def test_version_three_ledger_migrates_feed_tables(self):
        path = Path(self.tempdir.name) / "version_three.sqlite3"
        ledger = Ledger(path)
        with ledger._connection() as connection:
            connection.execute("DROP TABLE atom_entries")
            connection.execute("DROP TABLE feed_checkpoints")
            connection.execute("UPDATE schema_version SET version = 3")

        migrated = Ledger(path)

        with closing(migrated._connect()) as connection:
            version = connection.execute("SELECT version FROM schema_version").fetchone()[0]
            tables = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        self.assertEqual(version, SCHEMA_VERSION)
        self.assertIn("atom_entries", tables)
        self.assertIn("feed_checkpoints", tables)

    def test_recent_ledgers_migrate_safety_and_identity_tables(self):
        for old_version in (8, 9, 10):
            with self.subTest(version=old_version):
                path = Path(self.tempdir.name) / f"version-{old_version}.sqlite3"
                ledger = Ledger(path)
                ledger.record_message("kept@example.test", b"kept")
                with ledger._connection() as connection:
                    connection.execute("DROP TABLE archive_deletions")
                    if old_version == 8:
                        connection.execute("DROP TABLE identity_mappings")
                        connection.execute("DROP TABLE identity_mapping_audit")
                    connection.execute("UPDATE schema_version SET version = ?", (old_version,))
                migrated = Ledger(path)
                with closing(migrated._connect()) as connection:
                    version = connection.execute("SELECT version FROM schema_version").fetchone()[0]
                    tables = {row["name"] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
                    retained = connection.execute("SELECT raw_message FROM messages WHERE message_id = ?", ("kept@example.test",)).fetchone()[0]
                self.assertEqual(version, SCHEMA_VERSION)
                self.assertTrue({"archive_deletions", "identity_mappings", "identity_mapping_audit"} <= tables)
                self.assertEqual(retained, b"kept")

    def test_version_21_migrates_thread_search_progress(self):
        path = Path(self.tempdir.name) / "version-21.sqlite3"
        old = Ledger(path)
        old.record_message("kept@example.test", b"kept")
        with old._connection() as connection:
            connection.execute("DROP TABLE nntp_thread_searches")
            connection.execute("UPDATE schema_version SET version = 21")
        migrated = Ledger(path)
        with closing(migrated._connect()) as connection:
            self.assertEqual(connection.execute("SELECT version FROM schema_version").fetchone()[0], SCHEMA_VERSION)
            self.assertEqual(connection.execute("SELECT count(*) FROM nntp_thread_searches").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT raw_message FROM messages WHERE message_id = ?", ("kept@example.test",)).fetchone()[0], b"kept")

    def test_link_index_is_incremental_and_recovers_old_messages_in_bounded_batches(self):
        first = parse_message(
            b"Message-ID: <first@example.test>\nSubject: [PATCH 1/2] first\n"
            b"References: <cover@example.test>\n\nPatch.\n"
        )
        second = parse_message(
            b"Message-ID: <second@example.test>\nSubject: [PATCH 2/2] second\n"
            b"References: <cover@example.test>\n\nPatch.\n"
        )
        self.ledger.record_parsed_message(first)
        self.ledger.record_parsed_message(second)
        with self.ledger._connection() as connection:
            connection.execute("DELETE FROM message_links")
            connection.execute("UPDATE message_link_index_cursor SET last_rowid = 0")
        first_batch = self.ledger.index_message_links(limit=1)
        self.assertEqual(first_batch.processed, 1)
        self.assertFalse(first_batch.complete)
        second_batch = self.ledger.index_message_links(limit=2)
        self.assertEqual(second_batch.processed, 2)
        self.assertTrue(second_batch.complete)
        with closing(self.ledger._connect()) as connection:
            links = connection.execute(
                "SELECT child_id, parent_id, kind FROM message_links ORDER BY child_id"
            ).fetchall()
        self.assertEqual([tuple(row) for row in links], [
            ("first@example.test", "cover@example.test", "patch"),
            ("second@example.test", "cover@example.test", "patch"),
        ])
        third = parse_message(
            b"Message-ID: <third@example.test>\nSubject: Re: patch\n"
            b"In-Reply-To: <first@example.test>\n\nReview.\n"
        )
        self.ledger.record_parsed_message(third)
        self.assertEqual(self.ledger.index_message_links().processed, 0)

    def test_ancestry_requires_all_recursive_parents_and_checks_conflicts(self):
        child = parse_message(
            b"Message-ID: <child@example.test>\nSubject: [PATCH 1/1] child\n"
            b"In-Reply-To: <parent@example.test>\n\nPatch.\n"
        )
        parent = parse_message(
            b"Message-ID: <parent@example.test>\nSubject: older discussion\n"
            b"References: <grandparent@example.test>\n\nParent.\n"
        )
        grandparent = parse_message(b"Message-ID: <grandparent@example.test>\n\nGrandparent.\n")
        with self.assertRaisesRegex(PublicationConflict, "ancestry is incomplete"):
            self.ledger.assert_message_ancestry_available((child,))
        self.ledger.record_parsed_message(parent)
        with self.assertRaisesRegex(PublicationConflict, "ancestry is incomplete"):
            self.ledger.assert_message_ancestry_available((child,))
        self.ledger.assert_message_ancestry_available((child, grandparent))
        self.ledger.record_parsed_message(grandparent)
        self.ledger.assert_message_ancestry_available((child,))
        with self.assertRaises(MessageConflictError):
            self.ledger.record_message(parent.message_id, parent.raw_message.replace(b"Parent.", b"Different."))
        with self.assertRaises(MessageConflictError):
            self.ledger.assert_message_ancestry_available((child,))

    def test_ancestry_rejects_an_incomplete_link_index(self):
        child = parse_message(b"Message-ID: <child@example.test>\nIn-Reply-To: <series@example.org>\n\nBody.\n")
        with self.ledger._connection() as connection:
            connection.execute("UPDATE message_link_index_cursor SET last_rowid = 0")
        with self.assertRaisesRegex(ValueError, "link index is incomplete"):
            self.ledger.assert_message_ancestry_available((child,))
        self.ledger.index_message_links()
        self.ledger.assert_message_ancestry_available((child,))

    def test_ancestry_walk_handles_deep_chains_and_cycles(self):
        with self.ledger._connection() as connection:
            for index in range(1200):
                parent = f"node-{index + 1}@example.test" if index < 1199 else "node-0@example.test"
                item = parse_message(
                    f"Message-ID: <node-{index}@example.test>\nIn-Reply-To: <{parent}>\n\nBody.\n".encode()
                )
                connection.execute(
                    "INSERT INTO messages (message_id, sha256, raw_message, metadata_json, received_at) VALUES (?, ?, ?, ?, ?)",
                    (item.message_id, item.sha256, item.raw_message, "{}", "2026-01-01T00:00:00+00:00"),
                )
                self.ledger._index_message_links(connection, item.message_id, item)
            connection.execute("UPDATE message_link_index_cursor SET last_rowid = (SELECT MAX(rowid) FROM messages)")
        first = self.ledger.messages_by_id(("node-0@example.test",))[0]
        self.ledger.assert_message_ancestry_available((first,))

    def test_discovers_series_across_out_of_order_archive_batches(self):
        cover = parse_message(b"Message-ID: <cover@example.test>\nSubject: [PATCH 0/2] series\n\nCover.\n")
        first = parse_message(
            b"Message-ID: <first@example.test>\nSubject: [PATCH 1/2] first\n"
            b"References: <cover@example.test>\n\nPatch.\n"
        )
        second = parse_message(
            b"Message-ID: <second@example.test>\nSubject: [PATCH 2/2] second\n"
            b"References: <cover@example.test>\n\nPatch.\n"
        )
        unrelated = parse_message(b"Message-ID: <other@example.test>\nSubject: [PATCH 1/1] first\n\nPatch.\n")
        self.ledger.record_parsed_message(second)
        self.assertFalse(self.ledger.discover_related_series(second.message_id)[0].complete)
        for item in (unrelated, first, cover):
            self.ledger.record_parsed_message(item)
        (revision,) = self.ledger.discover_related_series(first.message_id)
        self.assertTrue(revision.complete)
        self.assertEqual([item.message_id for item in revision.messages], [cover.message_id, first.message_id, second.message_id])
        self.assertEqual(self.ledger.discover_related_series(first.message_id, limit=2), (revision,))
        with self.ledger._connection() as connection:
            connection.execute("UPDATE message_link_index_cursor SET last_rowid = 0")
        with self.assertRaisesRegex(ValueError, "index is incomplete"):
            self.ledger.discover_related_series(first.message_id)
        self.assertTrue(self.ledger.index_message_links(limit=10).complete)
        with self.assertRaises(MessageConflictError):
            self.ledger.record_message(second.message_id, b"conflict")
        with self.assertRaisesRegex(MessageConflictError, "Quarantined"):
            self.ledger.discover_related_series(first.message_id)

    def test_series_discovery_pages_more_than_a_thousand_related_messages(self):
        cover = parse_message(b"Message-ID: <cover@example.test>\nSubject: [PATCH 0/1] series\n\nCover.\n")
        self.ledger.record_parsed_message(cover)
        with self.ledger._connection() as connection:
            for index in range(1, 1051):
                item = parse_message(
                    f"Message-ID: <patch-{index:04}@example.test>\nSubject: [PATCH v{index} 1/1] series\n"
                    "References: <cover@example.test>\n\nPatch.\n".encode()
                )
                connection.execute(
                    "INSERT INTO messages (message_id, sha256, raw_message, metadata_json, received_at) VALUES (?, ?, ?, ?, ?)",
                    (item.message_id, item.sha256, item.raw_message, "{}", "2026-01-01T00:00:00+00:00"),
                )
                self.ledger._index_message_links(connection, item.message_id, item)
            connection.execute("UPDATE message_link_index_cursor SET last_rowid = (SELECT MAX(rowid) FROM messages)")

        revisions = self.ledger.discover_related_series(cover.message_id, limit=19)

        self.assertEqual(len(revisions), 1050)
        self.assertTrue(all(revision.complete and revision.root_message_id == cover.message_id for revision in revisions))

    def test_thread_discovery_pages_more_than_a_thousand_related_replies(self):
        patch = parse_message(b"Message-ID: <patch@example.test>\nSubject: [PATCH 1/1] series\n\nPatch.\n")
        self.ledger.record_parsed_message(patch)
        with self.ledger._connection() as connection:
            for index in range(1050):
                item = parse_message(
                    f"Message-ID: <reply-{index:04}@example.test>\nSubject: Re: series\n"
                    "In-Reply-To: <patch@example.test>\n\nReview.\n".encode()
                )
                connection.execute(
                    "INSERT INTO messages (message_id, sha256, raw_message, metadata_json, received_at) VALUES (?, ?, ?, ?, ?)",
                    (item.message_id, item.sha256, item.raw_message, "{}", "2026-01-01T00:00:00+00:00"),
                )
                self.ledger._index_message_links(connection, item.message_id, item)
            connection.execute("UPDATE message_link_index_cursor SET last_rowid = (SELECT MAX(rowid) FROM messages)")

        related = self.ledger._related_thread_messages((patch.message_id,), limit=19)

        self.assertEqual(len(related), 1050)
        self.assertEqual(len({item.message_id for item in related}), 1050)

    def test_series_revision_and_membership_persist(self):
        cover = parse_message(
            b"From: Ada <ada@example.test>\n"
            b"Message-ID: <cover@example.test>\n"
            b"Subject: [PATCH 0/1] xen: fix\n"
            b"Content-Type: text/plain; charset=utf-8\n\nCover.\n"
        )
        patch = parse_message(
            b"From: Ada <ada@example.test>\n"
            b"Message-ID: <patch@example.test>\n"
            b"Subject: [PATCH 1/1] xen: fix\n"
            b"References: <cover@example.test>\n"
            b"Content-Type: text/plain; charset=utf-8\n\nPatch.\n"
        )
        self.ledger.record_parsed_message(cover)
        self.ledger.record_parsed_message(patch)
        (revision,) = discover_series([cover, patch])

        self.ledger.record_series_revision(revision)
        self.ledger.record_series_revision(revision)

        with closing(self.ledger._connect()) as connection:
            stored_revision = connection.execute(
                "SELECT complete, expected_patch_count, cover_message_id"
                " FROM series_revisions WHERE series_key = ? AND revision = ?",
                (revision.series_key, revision.revision),
            ).fetchone()
            member_count = connection.execute(
                "SELECT count(*) FROM series_messages WHERE series_key = ?",
                (revision.series_key,),
            ).fetchone()[0]
        self.assertEqual(tuple(stored_revision), (1, 1, "cover@example.test"))
        self.assertEqual(member_count, 2)
        self.assertEqual(self.ledger.series_revision(revision.series_key, revision.revision), revision)
        self.assertIsNone(self.ledger.series_revision(revision.series_key, revision.revision + 1))

    def test_publication_intent_transitions_and_mapping_are_durable(self):
        patch = parse_message(
            b"From: Ada <ada@example.test>\nMessage-ID: <patch@example.test>\n"
            b"Subject: [PATCH 1/1] xen: fix\nContent-Type: text/plain\n\nPatch.\n"
        )
        self.ledger.record_parsed_message(patch)
        (revision,) = discover_series([patch])
        self.ledger.record_series_revision(revision)
        arguments = ("gitea/o/r", revision.series_key, 1, "digest", "base", "lore/branch", "head")

        intent = self.ledger.begin_publication(*arguments)
        self.assertEqual(intent.status, "prepared")
        self.assertEqual(self.ledger.begin_publication(*arguments), intent)
        with self.assertRaisesRegex(PublicationConflict, "differs"):
            self.ledger.begin_publication(*arguments[:-1], "different-head")
        key = arguments[:3]
        self.ledger.transition_publication(*key, expected=("prepared",), status="branch_ready")
        self.ledger.transition_publication(*key, expected=("branch_ready",), status="pr_pending")
        self.ledger.transition_publication(*key, expected=("pr_pending",), status="uncertain", last_error="timeout")
        self.assertEqual(self.ledger.publication(*key).last_error, "timeout")
        self.ledger.transition_publication(*key, expected=("uncertain",), status="complete", pull_index=7)
        self.assertEqual(self.ledger.publication(*key).pull_index, 7)
        with self.assertRaises(PublicationConflict):
            self.ledger.transition_publication(*key, expected=("pr_pending",), status="complete", pull_index=8)

    def test_worker_lock_is_exclusive_and_released_after_exception(self):
        other = Ledger(self.ledger.path)
        with self.assertRaisesRegex(RuntimeError, "interrupted"):
            with self.ledger.worker_lock():
                with self.assertRaises(WorkerBusyError), other.worker_lock():
                    pass
                raise RuntimeError("interrupted")
        with other.worker_lock():
            pass

    def test_version_five_ledger_migrates_publications(self):
        with self.ledger._connection() as connection:
            connection.execute("DROP TABLE publications")
            connection.execute("UPDATE schema_version SET version = 5")
        migrated = Ledger(self.ledger.path)
        with closing(migrated._connect()) as connection:
            self.assertEqual(connection.execute("SELECT version FROM schema_version").fetchone()[0], SCHEMA_VERSION)
            self.assertIsNotNone(connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'publications'"
            ).fetchone())

    def test_version_six_ledger_migrates_delivery_state(self):
        delivery_id = self.ledger.enqueue("series@example.org", "ISSUE_COMMENT", "repo", "reply", {"body": "x"})
        self.ledger.mark_delivered(delivery_id)
        with self.ledger._connection() as connection:
            connection.execute("ALTER TABLE outbox DROP COLUMN write_state")
            connection.execute("UPDATE schema_version SET version = 6")
        migrated = Ledger(self.ledger.path)
        with closing(migrated._connect()) as connection:
            state = connection.execute("SELECT write_state FROM outbox WHERE id = ?", (delivery_id,)).fetchone()[0]
        self.assertEqual(state, "complete")


if __name__ == "__main__":
    unittest.main()