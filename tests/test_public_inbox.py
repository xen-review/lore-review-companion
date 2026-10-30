# SPDX-License-Identifier: GPL-2.0-or-later

import gzip
import io
import json
import subprocess
import tempfile
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

from lore_review_companion.cli import main
from lore_review_companion.ingest import EpochArchive, backfill_epoch_round, backfill_public_inbox, ingest_public_inbox, plan_archive_discontinuity_resolution, sync_epoch_round, sync_public_inbox, watch_epoch_rounds
from lore_review_companion.ledger import SCHEMA_VERSION, ArchiveCheckpointConflict, ArchiveSafetyBlocked, Ledger, MessageConflictError, WorkerBusyError
from lore_review_companion.public_inbox import (
    MAX_ARCHIVE_MESSAGE_BYTES, PublicInboxError, _NoRedirect, _read_archive_message, fetch_public_inbox_thread,
    inspect_public_inbox_deletion, parse_public_inbox_manifest, parse_public_inbox_thread, public_inbox_epochs,
    read_public_inbox, read_public_inbox_backfill,
)


class PublicInboxTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.repo = Path(self.tempdir.name) / "archive"
        subprocess.run(["git", "init", "-q", "--initial-branch=master", str(self.repo)], check=True)
        self.messages = (
            b"Message-ID: <first@example.test>\nSubject: first\n\nFirst.\n",
            b"Message-ID: <second@example.test>\nSubject: second\n\nSecond.\n",
            b"Message-ID: <third@example.test>\nSubject: third\n\nThird.\n",
        )
        for raw_message in self.messages:
            (self.repo / "m").write_bytes(raw_message)
            self.git("add", "m")
            self.git("-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "-qm", "mail")

    def git(self, *args):
        return subprocess.run(
            ["git", "-C", str(self.repo), *args], check=True, capture_output=True
        ).stdout

    def test_reads_exact_messages_in_oldest_first_order(self):
        batch = read_public_inbox(self.repo, limit=3)
        self.assertEqual(tuple(message.raw_message for message in batch.messages), self.messages)
        self.assertEqual(batch.head, self.git("rev-parse", "HEAD").decode().strip())
        self.assertFalse(batch.history_incomplete)

    def test_discovers_only_selected_inbox_epochs_and_reports_gaps(self):
        manifest = gzip.compress(json.dumps({
            "/xen-devel/git/0.git": {"fingerprint": "one"},
            "/xen-devel/git/2.git": {"fingerprint": "two"},
            "/other/git/1.git": {},
        }).encode())
        catalog = parse_public_inbox_manifest("https://lore.example/xen-devel/", manifest)
        self.assertEqual(catalog.source, "manifest")
        self.assertEqual([epoch.number for epoch in catalog.epochs], [0, 2])
        self.assertEqual(catalog.epochs[1].git_url, "https://lore.example/xen-devel/2")
        self.assertEqual(catalog.missing, (1,))
        with self.assertRaises(PublicInboxError):
            parse_public_inbox_manifest("https://lore.example/missing/", manifest)

    def test_epoch_catalog_rejects_unsafe_urls_and_malformed_manifests(self):
        for inbox in ("http://lore.example/list", "https://user@lore.example/list", "https://lore.example/../list"):
            with self.subTest(inbox=inbox), self.assertRaises(ValueError):
                public_inbox_epochs(inbox, (0, 1))
        for numbers in ((), (0, 0), (-1,), (10001,)):
            with self.subTest(numbers=numbers), self.assertRaises(ValueError):
                public_inbox_epochs("https://lore.example/list", numbers)
        for data in (
            b"not gzip", gzip.compress(b'{"/list/git/0.git":{},"/list/git/0.git":{}}'),
            gzip.compress(b'{"/list/git/01.git":{}}'),
        ):
            with self.subTest(data=data), self.assertRaises(PublicInboxError):
                parse_public_inbox_manifest("https://lore.example/list", data)

    def test_thread_export_extracts_ids_without_a_message_count_cap(self):
        record = b"From mboxrd@z Thu Jan  1 00:00:00 1970\nMessage-ID: <%d@example.test>\n\nBody.\n\n"
        compressed = gzip.compress(b"".join(record % index for index in range(10001)))
        thread = parse_public_inbox_thread(compressed, temporary_directory=Path(self.tempdir.name))
        self.assertEqual(len(thread.message_ids), 10001)
        self.assertEqual((thread.message_ids[0], thread.message_ids[-1]), ("0@example.test", "10000@example.test"))
        self.assertEqual(thread.compressed_bytes, len(compressed))

    def test_thread_export_rejects_unsafe_or_incomplete_data(self):
        valid = b"From mboxrd@z Thu Jan  1 00:00:00 1970\nMessage-ID: <one@example.test>\n\nBody.\n\n"
        for data, options in (
            (b"not gzip", {}),
            (gzip.compress(valid)[:-3], {}),
            (gzip.compress(valid), {"compressed_limit": 1}),
            (gzip.compress(valid), {"uncompressed_limit": 1}),
            (gzip.compress(b"not an mbox"), {}),
            (gzip.compress(valid + valid), {}),
            (gzip.compress(valid.replace(b"one@example.test", b"bad")), {}),
        ):
            with self.subTest(data_length=len(data), options=options), self.assertRaises(PublicInboxError):
                parse_public_inbox_thread(data, temporary_directory=Path(self.tempdir.name), **options)

    def test_thread_fetch_rejects_redirects_and_closes_failed_responses(self):
        handler = _NoRedirect()
        self.assertIsNone(handler.redirect_request(None, None, 302, "Found", {}, "https://other.example/"))
        response = io.BytesIO(b"not disclosed")
        error = HTTPError("https://lore.example/secret-id", 302, "Found", {}, response)
        with patch("lore_review_companion.public_inbox.build_opener") as opener:
            opener.return_value.open.side_effect = error
            with self.assertRaisesRegex(PublicInboxError, "HTTP 302") as caught:
                fetch_public_inbox_thread("https://lore.example/list/", "mail@example.test")
        self.assertTrue(response.closed)
        self.assertNotIn("secret-id", str(caught.exception))
        self.assertNotIn("not disclosed", str(caught.exception))

        response = io.BytesIO(b"")
        with patch("lore_review_companion.public_inbox.build_opener") as opener, patch.object(response, "read", side_effect=TimeoutError):
            opener.return_value.open.return_value = response
            with self.assertRaisesRegex(PublicInboxError, "TimeoutError"):
                fetch_public_inbox_thread("https://lore.example/list/", "mail@example.test")
        self.assertTrue(response.closed)

        with patch("lore_review_companion.public_inbox.build_opener") as opener:
            for inbox in ("http://lore.example/list", "https://user@lore.example/list"):
                with self.subTest(inbox=inbox), self.assertRaises(ValueError):
                    fetch_public_inbox_thread(inbox, "mail@example.test")
            opener.assert_not_called()

    def test_thread_search_ranges_are_bounded_and_empty_results_are_unverified(self):
        record = b"From mboxrd@z Thu Jan  1 00:00:00 1970\nMessage-ID: <sibling@example.test>\n\nBody.\n\n"
        with patch("lore_review_companion.public_inbox.build_opener") as opener:
            opener.return_value.open.return_value = io.BytesIO(gzip.compress(record))
            result = fetch_public_inbox_thread(
                "https://lore.example/list/", "anchor@example.test", from_index_time=0, through_index_time=12345,
            )
            request = opener.return_value.open.call_args.args[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(parse_qs(urlsplit(request.full_url).query), {"q": ["rt:00000..12345"], "x": ["m"]})
        self.assertEqual(result.message_ids, ("sibling@example.test",))
        self.assertFalse(result.empty_or_unavailable)

        error = HTTPError("https://lore.example/secret-id", 404, "Not Found", {}, io.BytesIO(b"No results found\n"))
        with patch("lore_review_companion.public_inbox.build_opener") as opener:
            opener.return_value.open.side_effect = error
            result = fetch_public_inbox_thread("https://lore.example/list/", "anchor@example.test", from_index_time=1)
        self.assertEqual(result.message_ids, ())
        self.assertTrue(result.empty_or_unavailable)
        self.assertTrue(error.fp is None or error.fp.closed)

        response = io.BytesIO(b"")
        error = HTTPError("https://lore.example/secret-id", 404, "Not Found", {}, response)
        with patch("lore_review_companion.public_inbox.build_opener") as opener, patch.object(response, "read", side_effect=TimeoutError):
            opener.return_value.open.side_effect = error
            with self.assertRaisesRegex(PublicInboxError, "TimeoutError") as caught:
                fetch_public_inbox_thread("https://lore.example/list/", "anchor@example.test", from_index_time=1)
        self.assertTrue(response.closed)
        self.assertNotIn("secret-id", str(caught.exception))

        with patch("lore_review_companion.public_inbox.build_opener") as opener:
            for bounds in ({"from_index_time": -1}, {"through_index_time": True},
                           {"from_index_time": 2, "through_index_time": 1}):
                with self.subTest(bounds=bounds), self.assertRaises(ValueError):
                    fetch_public_inbox_thread("https://lore.example/list/", "anchor@example.test", **bounds)
            opener.assert_not_called()

    def test_epoch_cli_accepts_explicit_numbers_without_network(self):
        output = io.StringIO()
        with redirect_stdout(output):
            result = main(["discover-epochs", "--epoch", "1", "--epoch", "0"])
        self.assertEqual(result, 0)
        catalog = json.loads(output.getvalue())
        self.assertEqual(catalog["source"], "operator")
        self.assertEqual([epoch["number"] for epoch in catalog["epochs"]], [0, 1])
        self.assertEqual(catalog["missing"], [])

    def test_epoch_rounds_bound_work_report_gaps_and_resume(self):
        second = Path(self.tempdir.name) / "epoch-2"
        subprocess.run(["git", "clone", "-q", "--no-hardlinks", str(self.repo), str(second)], check=True)
        ledger = Ledger(Path(self.tempdir.name) / "rounds.sqlite3")
        epochs = (EpochArchive(2, second), EpochArchive(0, self.repo))
        first = sync_epoch_round(epochs, ledger, limit=3)
        self.assertEqual([item.number for item in first.results], [0])
        self.assertEqual((first.missing, first.deferred), ((1,), (2,)))
        resumed = sync_epoch_round(epochs, ledger, limit=3)
        self.assertEqual([item.number for item in resumed.results], [0, 2])
        self.assertEqual(resumed.results[1].summary.messages.replayed, 3)
        reverse = backfill_epoch_round(epochs, ledger, limit=3)
        self.assertEqual([item.number for item in reverse.results], [2])
        self.assertEqual(reverse.deferred, (0,))
        with self.assertRaisesRegex(ValueError, "unique"):
            sync_epoch_round((EpochArchive(0, self.repo), EpochArchive(0, second)), ledger)

    def test_epoch_round_cli_accepts_configured_paths(self):
        ledger = Path(self.tempdir.name) / "rounds.sqlite3"
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["sync-epochs", "--ledger", str(ledger), "--epoch", f"1={self.repo}", "--limit", "3"]), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["missing"], [0])
        self.assertEqual(result["results"][0]["summary"]["messages"]["ingested"], 3)

    def test_watch_runs_bounded_rounds_and_stops_without_sleeping(self):
        ledger = Path(self.tempdir.name) / "watch.sqlite3"
        arguments = ["watch-epochs", "--ledger", str(ledger), "--epoch", f"1={self.repo}", "--limit", "3", "--backfill", "--once"]
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(arguments), 0)
        cycle = json.loads(output.getvalue())
        self.assertEqual(cycle["command"], "watch-epochs")
        self.assertEqual(cycle["forward"]["missing"], [0])
        self.assertEqual(cycle["forward"]["results"][0]["summary"]["messages"]["ingested"], 3)
        self.assertEqual(cycle["backfill"]["results"][0]["summary"]["messages"]["replayed"], 3)

        class Stop:
            def __init__(self):
                self.intervals = []

            def is_set(self):
                return False

            def wait(self, interval):
                self.intervals.append(interval)
                return True

        stop = Stop()
        cycles = list(watch_epoch_rounds((EpochArchive(1, self.repo),), Ledger(ledger), interval=5, stop=stop))
        self.assertEqual((len(cycles), stop.intervals), (1, [5]))
        with self.assertRaisesRegex(ValueError, "interval"):
            list(watch_epoch_rounds((EpochArchive(1, self.repo),), Ledger(ledger), interval=0))
        Ledger(ledger).record_archive_deletion(str(self.repo), "d" * 40)
        with redirect_stderr(io.StringIO()) as error:
            self.assertEqual(main(arguments), 1)
        self.assertIn("archive deletion", error.getvalue())

    def test_watch_exits_nonzero_on_new_message_conflicts(self):
        (self.repo / "m").write_bytes(b"Message-ID: <first@example.test>\nSubject: conflicting\n\nDifferent.\n")
        self.git("add", "m")
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "-qm", "conflict")
        ledger = Path(self.tempdir.name) / "watch-conflict.sqlite3"
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main([
                "watch-epochs", "--ledger", str(ledger), "--epoch", f"0={self.repo}", "--limit", "4", "--backfill", "--once",
            ]), 1)
        cycle = json.loads(output.getvalue())
        self.assertEqual(cycle["forward"]["results"][0]["summary"]["messages"]["conflicts"], 1)
        self.assertIsNone(cycle["backfill"])
        self.assertEqual(Ledger(ledger).status().conflicted_message_ids, 1)
        with redirect_stderr(io.StringIO()) as error:
            self.assertEqual(main([
                "watch-epochs", "--ledger", str(ledger), "--epoch", f"0={self.repo}", "--once",
            ]), 1)
        self.assertIn("conflicts require inspection", error.getvalue())

    def test_epoch_health_reports_local_coverage_and_is_read_only(self):
        path = Path(self.tempdir.name) / "health.sqlite3"
        ledger = Ledger(path)
        arguments = ["check-epochs", "--ledger", str(path), "--epoch", f"1={self.repo}"]
        before = path.read_bytes()
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(arguments), 1)
        report = json.loads(output.getvalue())
        self.assertEqual({item["code"] for item in report["alerts"]}, {
            "missing_epoch", "forward_not_started", "backfill_not_started",
        })
        self.assertEqual(path.read_bytes(), before)
        sync_public_inbox(self.repo, ledger, limit=3)
        backfill_public_inbox(self.repo, ledger, limit=3)
        arguments[-1] = f"0={self.repo}"
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(arguments), 0)
        report = json.loads(output.getvalue())
        self.assertTrue(report["healthy"])
        self.assertIn("remote freshness", report["scope"])
        self.assertEqual((report["epochs"][0]["forward"], report["epochs"][0]["backfill"]), ("caught_up", "complete"))
        (self.repo / "m").write_bytes(b"Message-ID: <new@example.test>\nSubject: new\n\nNew.\n")
        self.git("add", "m")
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "-qm", "new mail")
        before = path.read_bytes()
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(arguments), 1)
        report = json.loads(output.getvalue())
        self.assertEqual(report["alerts"], [{"code": "forward_lagging", "epoch": 0}])
        self.assertEqual(path.read_bytes(), before)

    def test_epoch_health_reports_incidents_and_unverified_backfill(self):
        path = Path(self.tempdir.name) / "health-incidents.sqlite3"
        ledger = Ledger(path)
        sync_public_inbox(self.repo, ledger, limit=3)
        backfill_public_inbox(self.repo, ledger, limit=3)
        ledger.record_unmatched_message(b"unkeyed", ("missing ID",))
        delivery = ledger.enqueue("first@example.test", "ISSUE_COMMENT", "destination", "effect", {"body": "review"})
        with ledger._connection() as connection:
            connection.execute("UPDATE archive_backfill_checkpoints SET commit_id = ?", ("0" * 40,))
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["check-epochs", "--ledger", str(path), "--epoch", f"0={self.repo}"]), 1)
        report = json.loads(output.getvalue())
        self.assertEqual({item["code"] for item in report["alerts"]}, {
            "unmatched_messages", "pending_deliveries", "backfill_unverified",
        })
        self.assertEqual(report["epochs"][0]["backfill"], "unverified")
        with self.assertRaises(MessageConflictError):
            ledger.record_message("first@example.test", b"conflicting")
        ledger.record_archive_deletion(str(self.repo), "d" * 40)
        ledger.begin_delivery(delivery)
        ledger.mark_delivery_uncertain(delivery, "unknown result")
        with ledger._connection() as connection:
            connection.execute("UPDATE archive_checkpoints SET commit_id = ?", ("0" * 40,))
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["check-epochs", "--ledger", str(path), "--epoch", f"0={self.repo}"]), 1)
        report = json.loads(output.getvalue())
        self.assertEqual({item["code"] for item in report["alerts"]}, {
            "unmatched_messages", "message_conflicts", "archive_deletions", "uncertain_deliveries",
            "forward_unverified", "backfill_unverified",
        })

    def test_epoch_health_rejects_missing_or_old_ledgers_without_upgrading(self):
        path = Path(self.tempdir.name) / "health-missing.sqlite3"
        arguments = ["check-epochs", "--ledger", str(path), "--epoch", f"0={self.repo}"]
        with redirect_stderr(io.StringIO()) as error:
            self.assertEqual(main(arguments), 1)
        self.assertIn("existing ledger", error.getvalue())
        self.assertFalse(path.exists())
        ledger = Ledger(path)
        with ledger._connection() as connection:
            connection.execute("UPDATE schema_version SET version = 17")
        before = path.read_bytes()
        with redirect_stderr(io.StringIO()) as error:
            self.assertEqual(main(arguments), 1)
        self.assertIn("current ledger schema", error.getvalue())
        self.assertEqual(path.read_bytes(), before)

    def test_marks_bounded_or_shallow_history_incomplete(self):
        batch = read_public_inbox(self.repo, limit=2)
        self.assertEqual(tuple(message.raw_message for message in batch.messages), self.messages[-2:])
        self.assertTrue(batch.history_incomplete)
        shallow = Path(self.tempdir.name) / "shallow"
        subprocess.run(
            ["git", "clone", "-q", "--depth=2", self.repo.as_uri(), str(shallow)], check=True
        )
        self.assertTrue(read_public_inbox(shallow, limit=3).history_incomplete)

    def test_reads_oldest_unprocessed_commits_in_bounded_batches(self):
        first_id = self.git("rev-parse", "HEAD~2").decode().strip()
        first = read_public_inbox(self.repo, limit=1, after=first_id)
        self.assertEqual(first.messages[0].raw_message, self.messages[1])
        self.assertTrue(first.has_more)

        second = read_public_inbox(self.repo, limit=1, after=first.messages[0].commit_id)
        self.assertEqual(second.messages[0].raw_message, self.messages[2])
        self.assertFalse(second.has_more)
        self.assertEqual(read_public_inbox(self.repo, after=second.head).messages, ())

    def test_backfill_walks_newest_to_oldest_without_gaps(self):
        first = read_public_inbox_backfill(self.repo, limit=2)
        self.assertEqual(tuple(item.raw_message for item in first.messages), self.messages[:0:-1])
        self.assertTrue(first.has_more)
        self.assertFalse(first.complete)
        second = read_public_inbox_backfill(self.repo, before=first.messages[-1].commit_id, limit=2)
        self.assertEqual(tuple(item.raw_message for item in second.messages), (self.messages[0],))
        self.assertTrue(second.complete)
        self.assertFalse(second.needs_deepen)
        self.assertEqual(read_public_inbox_backfill(self.repo, before=second.messages[-1].commit_id).messages, ())

    def test_backfill_distinguishes_shallow_boundary_from_root(self):
        shallow = Path(self.tempdir.name) / "shallow-backfill"
        subprocess.run(["git", "clone", "-q", "--depth=2", self.repo.as_uri(), str(shallow)], check=True)
        batch = read_public_inbox_backfill(shallow, limit=3)
        self.assertEqual(tuple(item.raw_message for item in batch.messages), self.messages[:0:-1])
        self.assertTrue(batch.needs_deepen)
        self.assertFalse(batch.complete)
        waiting = read_public_inbox_backfill(shallow, before=batch.messages[-1].commit_id)
        self.assertTrue(waiting.needs_deepen)
        self.assertEqual(waiting.messages, ())
        subprocess.run(["git", "-C", str(shallow), "fetch", "-q", "--deepen=1", "origin"], check=True)
        older = read_public_inbox_backfill(shallow, before=batch.messages[-1].commit_id)
        self.assertEqual(tuple(item.raw_message for item in older.messages), (self.messages[0],))
        self.assertTrue(older.complete)

    def test_backfill_checkpoints_replay_and_coexist_with_forward_sync(self):
        ledger = Ledger(Path(self.tempdir.name) / "ledger.sqlite3")
        sync_public_inbox(self.repo, ledger, limit=2, allow_incomplete_bootstrap=True)
        with patch.object(ledger, "advance_backfill_checkpoint", side_effect=RuntimeError("crash")):
            with self.assertRaisesRegex(RuntimeError, "crash"):
                backfill_public_inbox(self.repo, ledger, limit=2)
        self.assertIsNone(ledger.backfill_checkpoint(str(self.repo)))
        first = backfill_public_inbox(self.repo, ledger, limit=2)
        self.assertEqual((first.messages.ingested, first.messages.replayed), (0, 2))
        self.assertTrue(first.has_more)
        oldest = backfill_public_inbox(self.repo, ledger, limit=2)
        self.assertEqual(oldest.messages.ingested, 1)
        self.assertTrue(oldest.complete)
        self.assertTrue(ledger.backfill_checkpoint(str(self.repo)).complete)
        self.assertEqual(backfill_public_inbox(self.repo, ledger).messages.ingested, 0)
        with self.assertRaises(ArchiveCheckpointConflict):
            ledger.advance_backfill_checkpoint(str(self.repo), None, first.head, complete=False)

    def test_backfill_can_opt_in_to_bounded_deepening(self):
        shallow = Path(self.tempdir.name) / "shallow-sync"
        subprocess.run(["git", "clone", "-q", "--depth=2", self.repo.as_uri(), str(shallow)], check=True)
        ledger = Ledger(Path(self.tempdir.name) / "ledger.sqlite3")
        first = backfill_public_inbox(shallow, ledger, limit=3)
        self.assertTrue(first.needs_deepen)
        older = backfill_public_inbox(shallow, ledger, limit=3, deepen=1)
        self.assertEqual(older.messages.ingested, 1)
        self.assertTrue(older.complete)
        with self.assertRaisesRegex(ValueError, "deepen"):
            backfill_public_inbox(shallow, ledger, deepen=1001)

    def test_rejects_invalid_or_unavailable_checkpoint(self):
        with self.assertRaises(ValueError):
            read_public_inbox(self.repo, after="--all")
        with self.assertRaisesRegex(PublicInboxError, "not an available ancestor"):
            read_public_inbox(self.repo, after="0" * 40)

    def test_rejects_missing_message_blob_and_invalid_limits(self):
        self.git("rm", "-q", "m")
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "-qm", "remove")
        with self.assertRaises(PublicInboxError):
            read_public_inbox(self.repo)
        for limit in (0, 1001):
            with self.assertRaises(ValueError):
                read_public_inbox(self.repo, limit=limit)

    def test_rejects_oversized_mail_before_reading_the_blob(self):
        calls = []

        def fake_git(*args):
            calls.append(args)
            if args[0] == "ls-tree":
                return b"100644 blob " + b"a" * 40 + b"\tm\0"
            if args[:2] == ("cat-file", "-s"):
                return str(MAX_ARCHIVE_MESSAGE_BYTES + 1).encode()
            self.fail("oversized mail blob was read")

        with self.assertRaisesRegex(PublicInboxError, "16 MiB"):
            _read_archive_message(fake_git, "a" * 40)
        self.assertEqual(len(calls), 2)

    def test_deletion_record_stops_forward_and_backfill_without_checkpoint(self):
        ledger = Ledger(Path(self.tempdir.name) / "ledger.sqlite3")
        self.git("rm", "-q", "m")
        (self.repo / "d").write_bytes(self.messages[0])
        self.git("add", "d")
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "-qm", "delete")
        commit_id = self.git("rev-parse", "HEAD").decode().strip()
        with self.assertRaisesRegex(PublicInboxError, f"deletion record at {commit_id}"):
            sync_public_inbox(self.repo, ledger, limit=4)
        with self.assertRaisesRegex(PublicInboxError, f"deletion record at {commit_id}"):
            backfill_public_inbox(self.repo, ledger, limit=4)
        self.assertIsNone(ledger.archive_checkpoint(str(self.repo)))
        self.assertIsNone(ledger.backfill_checkpoint(str(self.repo)))
        reopened = Ledger(ledger.path)
        self.assertEqual([(entry.source, entry.commit_id) for entry in reopened.archive_deletions()], [(str(self.repo), commit_id)])
        with self.assertRaisesRegex(ArchiveSafetyBlocked, commit_id):
            reopened.assert_unconflicted(())
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["archive-safety", "--ledger", str(ledger.path)]), 0)
        self.assertEqual(json.loads(output.getvalue())["deletions"][0]["commit_id"], commit_id)

        before = self.git("status", "--porcelain")
        inspection = inspect_public_inbox_deletion(self.repo, commit_id)
        self.assertEqual((inspection.commit_id, inspection.message_id, inspection.size), (commit_id, "first@example.test", len(self.messages[0])))
        self.assertEqual(inspection.blob_id, self.git("rev-parse", f"{commit_id}:d").decode().strip())
        self.assertNotIn("First.", repr(inspection))
        self.assertEqual(self.git("status", "--porcelain"), before)
        before_ledger = ledger.path.read_bytes()
        arguments = ["inspect-archive-deletion", "--ledger", str(ledger.path), "--archive", str(self.repo), "--commit"]
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main([*arguments, commit_id]), 0)
        self.assertEqual(json.loads(output.getvalue())["message_id"], "first@example.test")
        self.assertNotIn("First.", output.getvalue())
        with redirect_stderr(io.StringIO()) as error:
            self.assertEqual(main([*arguments, "f" * 40]), 1)
        self.assertIn("No recorded archive deletion", error.getvalue())
        self.assertNotIn("First.", error.getvalue())
        self.assertEqual(ledger.path.read_bytes(), before_ledger)
        with self.assertRaises(ArchiveSafetyBlocked):
            reopened.assert_unconflicted(())

    def test_inspection_commands_refuse_an_old_schema_without_upgrading_it(self):
        ledger = Ledger(Path(self.tempdir.name) / "old-ledger.sqlite3")
        with ledger._connection() as connection:
            connection.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION - 1,))
        before = ledger.path.read_bytes()
        commands = (
            ("archive-safety",), ("status",), ("list-messages",), ("list-outbox",), ("list-revision-links",),
            ("search-change-id", "--change-id", "sample"), ("discover-series", "--message-id", "sample@example.test"),
            ("inspect-upstream", "--repository", str(self.repo), "--api-url", "https://gitea.test/api/v1/",
             "--owner", "owner", "--repo", "repo", "--pull-index", "1"),
              ("prune-unmatched", "--before", "2020-01-01T00:00:00Z", "--actor", "operator", "--reason", "review"),
              ("link-revision", "--series-key", "series", "--canonical-revision", "1", "--candidate-seed", "sample@example.test",
               "--candidate-revision", "2", "--actor", "operator", "--reason", "review"),
              ("resolve-discontinuity", "--archive", str(self.repo), "--checkpoint", "a" * 40, "--incident-head", "b" * 40,
               "--actor", "operator", "--reason", "review"),
            ("sync-series", "--series-seed", "sample@example.test", "--repository", str(self.repo), "--base", "HEAD",
             "--remote", "https://gitea.test/owner/repo.git", "--api-url", "https://gitea.test/api/v1/",
             "--owner", "owner", "--repo", "repo", "--target-branch", "staging"),
            ("deliver-comments", "--api-url", "https://gitea.test/api/v1/", "--owner", "owner", "--repo", "repo",
             "--pull-index", "1", "--kind", "conversation"),
            ("migrate-legacy-outbox", "--api-url", "https://gitea.test/api/v1/", "--owner", "owner", "--repo", "repo",
             "--pull-index", "1", "--reason", "review"),
        )
        with patch.dict("os.environ", {"LORE_FORGE_TOKEN": "synthetic-token"}), patch("lore_review_companion.forge_api.ForgeAPI.request_json") as request:
            for command in commands:
                with self.subTest(command=command[0]), redirect_stderr(io.StringIO()) as error:
                    self.assertEqual(main([*command, "--ledger", str(ledger.path)]), 1)
                    self.assertIn("Read-only access requires the current ledger schema", error.getvalue())
                    self.assertEqual(ledger.path.read_bytes(), before)
            request.assert_not_called()

    def test_retention_preview_does_not_change_the_current_ledger(self):
        ledger = Ledger(Path(self.tempdir.name) / "ledger.sqlite3")
        before = ledger.path.read_bytes()
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main([
                "prune-unmatched", "--ledger", str(ledger.path), "--before", "2020-01-01T00:00:00Z",
                "--actor", "operator", "--reason", "review",
            ]), 0)
        self.assertEqual(json.loads(output.getvalue())["plan"]["entries"], [])
        self.assertEqual(ledger.path.read_bytes(), before)

    def test_deletion_inspection_rejects_ambiguous_ids_without_exposing_content(self):
        self.git("rm", "-q", "m")
        for raw_message in (
            b"Message-ID: <first@example.test>\nMessage-ID: <second@example.test>\n\nSECRET BODY\n",
            b"Message-ID: <first@example.test> <second@example.test>\n\nSECRET BODY\n",
            b"Message-ID: <bad>\n\nSECRET BODY\n",
            b"Subject: no identity\n\nSECRET BODY\n",
        ):
            with self.subTest(raw_message=raw_message.splitlines()[0]):
                (self.repo / "d").write_bytes(raw_message)
                self.git("add", "d")
                self.git("-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "-qm", "delete")
                commit_id = self.git("rev-parse", "HEAD").decode().strip()
                with self.assertRaises(PublicInboxError) as caught:
                    inspect_public_inbox_deletion(self.repo, commit_id)
                self.assertNotIn("SECRET BODY", str(caught.exception))

    def test_deletion_inspection_reports_only_direct_local_presence(self):
        ledger = Ledger(Path(self.tempdir.name) / "ledger.sqlite3")
        ledger.record_message("first@example.test", self.messages[0])
        ledger.record_message(
            "reply@example.test",
            b"Message-ID: <reply@example.test>\nIn-Reply-To: <first@example.test>\nSubject: Re: first\n\nSECRET BODY\n",
        )
        ledger.record_message(
            "nested@example.test",
            b"Message-ID: <nested@example.test>\nIn-Reply-To: <reply@example.test>\nSubject: Re: first\n\nSECRET BODY\n",
        )
        ledger.record_message(
            "orphan@example.test",
            b"Message-ID: <orphan@example.test>\nReferences: <missing@example.test>\nSubject: Re: missing\n\nSECRET BODY\n",
        )
        ledger.enqueue("first@example.test", "ISSUE_COMMENT", "destination", "effect", {"body": "SECRET BODY"})
        ledger.enqueue("nested@example.test", "ISSUE_COMMENT", "destination", "effect", {"body": "SECRET BODY"})
        with self.assertRaises(MessageConflictError):
            ledger.record_message("first@example.test", self.messages[0] + b"SECRET BODY\n")
        with ledger._connection() as connection:
            connection.execute(
                "INSERT INTO message_source_variants VALUES (?, ?, ?, ?, ?)",
                ("first@example.test", "variant-hash", b"SECRET BODY", "{}", "2026-10-29"),
            )
            connection.execute("INSERT INTO series VALUES (?, ?, ?)", ("series", "first@example.test", "2026-10-29"))
            connection.execute(
                "INSERT INTO series_revisions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("series", 1, "title", 1, "first@example.test", 1, "[]", "2026-10-29"),
            )
            connection.execute("INSERT INTO series_messages VALUES (?, ?, ?, ?, ?)", ("first@example.test", "series", 1, "cover", 0))
            connection.execute("INSERT INTO series_messages VALUES (?, ?, ?, ?, ?)", ("reply@example.test", "series", 1, "patch", 1))
            connection.execute("INSERT INTO series_messages VALUES (?, ?, ?, ?, ?)", ("nested@example.test", "series", 1, "patch", 2))
            connection.execute(
                "INSERT INTO publications VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("destination", "series", 1, "approval", "base", "branch", "head", "complete", 1, None, "2026-10-29"),
            )
        self.git("rm", "-q", "m")
        (self.repo / "d").write_bytes(self.messages[0])
        self.git("add", "d")
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "-qm", "delete")
        commit_id = self.git("rev-parse", "HEAD").decode().strip()
        ledger.record_archive_deletion(str(self.repo.resolve()), commit_id)
        arguments = ["inspect-archive-deletion", "--ledger", str(ledger.path), "--archive", str(self.repo), "--commit", commit_id]
        for complete in (True, False):
            if not complete:
                with ledger._connection() as connection:
                    connection.execute("UPDATE message_link_index_cursor SET last_rowid = 0 WHERE id = 1")
            before = ledger.path.read_bytes()
            with self.subTest(index_complete=complete), redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(arguments), 0)
            result = json.loads(output.getvalue())
            self.assertEqual(result["local_presence"], {
                "message_stored": True,
                "source_variants": 1,
                "conflicting_versions": 1,
                "direct_children": 1,
                "series_memberships": 1,
                "series_publications": 1,
                "outbox_entries": 1,
                "link_index_complete": complete,
                "indexed_descendants": 2,
                "descendant_series_memberships": 2,
                "descendant_series_publications": 1,
                "descendant_outbox_entries": 1,
                "indexed_links_scanned": 2,
                "indexed_walk_complete": True,
                "scope_complete": False,
            })
            self.assertNotIn("SECRET BODY", output.getvalue())
            self.assertNotIn("First.", output.getvalue())
            self.assertEqual(ledger.path.read_bytes(), before)
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main([*arguments, "--link-limit", "1"]), 0)
        limited = json.loads(output.getvalue())["local_presence"]
        self.assertEqual((limited["indexed_descendants"], limited["indexed_links_scanned"]), (1, 1))
        self.assertFalse(limited["indexed_walk_complete"])
        missing = Ledger(ledger.path, read_only=True).archive_deletion_presence("missing@example.test")
        self.assertFalse(missing.message_stored)
        self.assertEqual(missing.direct_children, 1)
        self.assertFalse(missing.scope_complete)
        with self.assertRaises(ArchiveSafetyBlocked):
            ledger.assert_unconflicted(())

    def test_deletion_presence_bounds_descendants_and_handles_cycles(self):
        ledger = Ledger(Path(self.tempdir.name) / "ledger.sqlite3")
        for identifier in ("root", "child", "grandchild"):
            ledger.record_message(f"{identifier}@example.test", f"Message-ID: <{identifier}@example.test>\n\nSECRET BODY\n".encode())
        with ledger._connection() as connection:
            connection.executemany(
                "INSERT INTO message_links (child_id, parent_id, kind) VALUES (?, ?, 'reply')",
                (("child@example.test", "root@example.test"),
                 ("grandchild@example.test", "child@example.test"),
                 ("root@example.test", "grandchild@example.test")),
            )
        ledger.enqueue("grandchild@example.test", "ISSUE_COMMENT", "destination", "effect", {"body": "SECRET BODY"})
        before = ledger.path.read_bytes()
        limited = Ledger(ledger.path, read_only=True).archive_deletion_presence("root@example.test", link_limit=1)
        self.assertEqual((limited.indexed_descendants, limited.indexed_links_scanned), (1, 1))
        self.assertFalse(limited.indexed_walk_complete)
        complete = Ledger(ledger.path, read_only=True).archive_deletion_presence("root@example.test", link_limit=3)
        self.assertEqual((complete.indexed_descendants, complete.descendant_outbox_entries, complete.indexed_links_scanned), (2, 1, 3))
        self.assertTrue(complete.indexed_walk_complete)
        self.assertFalse(complete.scope_complete)
        self.assertNotIn("SECRET BODY", repr(complete))
        self.assertEqual(ledger.path.read_bytes(), before)
        for limit in (0, 10001, True):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                ledger.archive_deletion_presence("root@example.test", link_limit=limit)

    def test_deletion_inspection_checks_size_before_reading_the_blob(self):
        blob_id = "a" * 40
        calls = []

        def fake_run(command, **options):
            args = command[4:]
            calls.append(args)
            if args == ["cat-file", "-t", "b" * 40]:
                output = b"commit\n"
            elif args == ["cat-file", "-s", f"{'b' * 40}^{{tree}}"]:
                output = b"30\n"
            elif args == ["ls-tree", "-z", "b" * 40]:
                output = f"100644 blob {blob_id}\td\0".encode()
            elif args == ["cat-file", "-s", blob_id]:
                output = str(MAX_ARCHIVE_MESSAGE_BYTES + 1).encode()
            else:
                self.fail("oversized deletion blob was read")
            return subprocess.CompletedProcess(command, 0, output)

        with patch("lore_review_companion.public_inbox.subprocess.run", side_effect=fake_run):
            with self.assertRaisesRegex(PublicInboxError, "16 MiB"):
                inspect_public_inbox_deletion(self.repo, "b" * 40)
        self.assertEqual(len(calls), 4)
        with patch("lore_review_companion.public_inbox.subprocess.run") as run, self.assertRaises(ValueError):
            inspect_public_inbox_deletion(self.repo, "--all")
        run.assert_not_called()

    def test_deletion_cannot_be_hidden_by_a_mixed_archive_tree(self):
        ledger = Ledger(Path(self.tempdir.name) / "ledger.sqlite3")
        (self.repo / "d").write_bytes(self.messages[0])
        self.git("add", "d")
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "-qm", "mixed")
        commit_id = self.git("rev-parse", "HEAD").decode().strip()
        with self.assertRaisesRegex(PublicInboxError, f"deletion record at {commit_id}"):
            sync_public_inbox(self.repo, ledger, limit=4)
        self.assertEqual(ledger.archive_deletions()[0].commit_id, commit_id)
        with self.assertRaisesRegex(PublicInboxError, "exactly one d blob"):
            inspect_public_inbox_deletion(self.repo, commit_id)

    def test_unavailable_checkpoint_durably_blocks_publication(self):
        ledger = Ledger(Path(self.tempdir.name) / "ledger.sqlite3")
        forward = sync_public_inbox(self.repo, ledger, limit=3)
        backfill_public_inbox(self.repo, ledger, limit=3)
        tree = self.git("rev-parse", "HEAD^{tree}").decode().strip()
        rewritten = self.git(
            "-c", "user.name=Test", "-c", "user.email=test@example.test", "commit-tree", tree, "-m", "different history",
        ).decode().strip()
        self.git("update-ref", "refs/heads/master", rewritten)
        with self.assertRaisesRegex(PublicInboxError, "not an available ancestor"):
            sync_public_inbox(self.repo, ledger)
        with self.assertRaisesRegex(PublicInboxError, "not an available ancestor"):
            backfill_public_inbox(self.repo, ledger)
        reopened = Ledger(ledger.path)
        self.assertEqual(reopened.archive_checkpoint(str(self.repo)), forward.head)
        self.assertEqual(len(reopened.archive_discontinuities()), 2)
        with self.assertRaisesRegex(ArchiveSafetyBlocked, "not verified"):
            reopened.assert_unconflicted(())
        incident = reopened.archive_discontinuities()[0]
        with self.assertRaisesRegex(PublicInboxError, "cannot be proven"):
            plan_archive_discontinuity_resolution(
                self.repo, reopened, incident.checkpoint, incident.head, "operator", "reviewed history",
            )

    def test_missing_history_resolution_requires_continuity_catchup_and_approval(self):
        shallow = Path(self.tempdir.name) / "resolution"
        subprocess.run(["git", "clone", "-q", "--depth=1", self.repo.as_uri(), str(shallow)], check=True)
        ledger = Ledger(Path(self.tempdir.name) / "resolution.sqlite3")
        sync_public_inbox(shallow, ledger, allow_incomplete_bootstrap=True)
        for index in (4, 5):
            (self.repo / "m").write_bytes(f"Message-ID: <{index}@example.test>\n\nMail.\n".encode())
            self.git("add", "m")
            self.git("-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "-qm", "mail")
        subprocess.run(["git", "-C", str(shallow), "fetch", "-q", "--depth=1", "origin"], check=True)
        subprocess.run(["git", "-C", str(shallow), "update-ref", "refs/heads/master", "refs/remotes/origin/master"], check=True)
        with self.assertRaisesRegex(PublicInboxError, "not an available ancestor"):
            sync_public_inbox(shallow, ledger)
        (incident,) = ledger.archive_discontinuities()
        with self.assertRaisesRegex(PublicInboxError, "cannot be proven"):
            plan_archive_discontinuity_resolution(shallow, ledger, incident.checkpoint, incident.head, "operator", "missing history")
        subprocess.run(["git", "-C", str(shallow), "fetch", "-q", "--deepen=2", "origin"], check=True)
        with self.assertRaisesRegex(ArchiveCheckpointConflict, "Forward ingestion"):
            plan_archive_discontinuity_resolution(shallow, ledger, incident.checkpoint, incident.head, "operator", "missing history")
        self.assertEqual(sync_public_inbox(shallow, ledger).messages.ingested, 2)
        arguments = [
            "resolve-discontinuity", "--ledger", str(ledger.path), "--archive", str(shallow),
            "--checkpoint", incident.checkpoint, "--incident-head", incident.head,
            "--actor", "operator", "--reason", "missing history",
        ]
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(arguments), 0)
        digest = json.loads(output.getvalue())["approval_digest"]
        with redirect_stderr(io.StringIO()) as error:
            self.assertEqual(main([*arguments, "--write", "--approve", "0" * 64]), 1)
        self.assertIn("does not match", error.getvalue())
        self.assertEqual(ledger.archive_discontinuities(), (incident,))
        (self.repo / "m").write_bytes(b"Message-ID: <6@example.test>\n\nMail.\n")
        self.git("add", "m")
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "-qm", "mail")
        subprocess.run(["git", "-C", str(shallow), "fetch", "-q", "origin"], check=True)
        subprocess.run(["git", "-C", str(shallow), "update-ref", "refs/heads/master", "refs/remotes/origin/master"], check=True)
        with redirect_stderr(io.StringIO()) as error:
            self.assertEqual(main([*arguments, "--write", "--approve", digest]), 1)
        self.assertIn("Forward ingestion", error.getvalue())
        self.assertEqual(sync_public_inbox(shallow, ledger).messages.ingested, 1)
        with redirect_stderr(io.StringIO()) as error:
            self.assertEqual(main([*arguments, "--write", "--approve", digest]), 1)
        self.assertIn("does not match", error.getvalue())
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(arguments), 0)
        digest = json.loads(output.getvalue())["approval_digest"]
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main([*arguments, "--write", "--approve", digest]), 0)
        self.assertEqual(ledger.archive_discontinuities(), ())
        ledger.assert_unconflicted(())
        (audit,) = ledger.archive_discontinuity_resolutions()
        self.assertEqual((audit.checkpoint, audit.incident_head, audit.ref, audit.actor, audit.approval_digest),
                         (incident.checkpoint, incident.head, "HEAD", "operator", digest))
        ledger.record_archive_discontinuity(incident.source, incident.checkpoint, incident.head)
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(arguments), 0)
        digest = json.loads(output.getvalue())["approval_digest"]
        ledger.record_archive_deletion(str(shallow), "d" * 40)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main([*arguments, "--write", "--approve", digest]), 0)
        with self.assertRaisesRegex(ArchiveSafetyBlocked, "deletion"):
            ledger.assert_unconflicted(())

    def test_archive_ingestion_shares_the_publication_lock(self):
        ledger = Ledger(Path(self.tempdir.name) / "ledger.sqlite3")
        with ledger.worker_lock(), self.assertRaises(WorkerBusyError):
            sync_public_inbox(self.repo, ledger, limit=3)
        self.assertEqual(sync_public_inbox(self.repo, ledger, limit=3).messages.ingested, 3)

    def test_ingest_preserves_raw_mail_and_replays_without_duplicates(self):
        ledger = Ledger(Path(self.tempdir.name) / "ledger.sqlite3")
        first = ingest_public_inbox(self.repo, ledger, limit=3)
        replay = ingest_public_inbox(self.repo, ledger, limit=3)

        self.assertEqual(first.messages.ingested, 3)
        self.assertEqual(replay.messages.replayed, 3)
        self.assertFalse(first.history_incomplete)
        with closing(ledger._connect()) as connection:
            rows = connection.execute(
                "SELECT raw_message FROM messages ORDER BY message_id"
            ).fetchall()
            self.assertEqual(
                connection.execute("SELECT count(*) FROM series_revisions").fetchone()[0], 0
            )
        self.assertEqual(tuple(row[0] for row in rows), (self.messages[0], self.messages[1], self.messages[2]))

    def test_sync_requires_explicit_incomplete_bootstrap(self):
        ledger = Ledger(Path(self.tempdir.name) / "ledger.sqlite3")
        with self.assertRaisesRegex(PublicInboxError, "omit earlier history"):
            sync_public_inbox(self.repo, ledger, limit=2)
        self.assertIsNone(ledger.archive_checkpoint(str(self.repo)))

        result = sync_public_inbox(self.repo, ledger, limit=2, allow_incomplete_bootstrap=True)
        self.assertTrue(result.history_incomplete)
        self.assertEqual(result.messages.ingested, 2)
        self.assertEqual(ledger.archive_checkpoint(str(self.repo)), result.head)

    def test_sync_processes_large_backlog_without_gaps(self):
        ledger = Ledger(Path(self.tempdir.name) / "ledger.sqlite3")
        first_id = self.git("rev-parse", "HEAD~2").decode().strip()
        ledger.advance_archive_checkpoint(str(self.repo), None, first_id)

        first = sync_public_inbox(self.repo, ledger, limit=1)
        second = sync_public_inbox(self.repo, ledger, limit=1)
        final = sync_public_inbox(self.repo, ledger, limit=1)
        self.assertTrue(first.has_more)
        self.assertFalse(second.has_more)
        self.assertEqual((first.messages.ingested, second.messages.ingested), (1, 1))
        self.assertEqual(final.messages.ingested, 0)
        self.assertEqual(ledger.archive_checkpoint(str(self.repo)), second.head)

    def test_failed_checkpoint_replays_and_rejects_stale_worker(self):
        ledger = Ledger(Path(self.tempdir.name) / "ledger.sqlite3")
        with patch.object(ledger, "advance_archive_checkpoint", side_effect=RuntimeError("crash")):
            with self.assertRaisesRegex(RuntimeError, "crash"):
                sync_public_inbox(self.repo, ledger, limit=3)
        self.assertIsNone(ledger.archive_checkpoint(str(self.repo)))
        replay = sync_public_inbox(self.repo, ledger, limit=3)
        self.assertEqual(replay.messages.replayed, 3)
        with self.assertRaises(ArchiveCheckpointConflict):
            ledger.advance_archive_checkpoint(str(self.repo), None, replay.head)

    def test_fetch_sync_reads_remote_ref_without_moving_checkout(self):
        clone = Path(self.tempdir.name) / "mirror"
        subprocess.run(["git", "clone", "-q", str(self.repo), str(clone)], check=True)
        ledger = Ledger(Path(self.tempdir.name) / "ledger.sqlite3")
        first = sync_public_inbox(clone, ledger, limit=3, fetch=True)
        old_head = first.head

        new_message = b"Message-ID: <new@example.test>\nSubject: new\n\nNew.\n"
        (self.repo / "m").write_bytes(new_message)
        self.git("add", "m")
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "-qm", "new mail")

        updated = sync_public_inbox(clone, ledger, limit=3, fetch=True)
        checkout_head = subprocess.run(
            ["git", "-C", str(clone), "rev-parse", "HEAD"], check=True, capture_output=True
        ).stdout.decode().strip()
        self.assertEqual(checkout_head, old_head)
        self.assertNotEqual(updated.head, old_head)
        self.assertEqual(updated.messages.ingested, 1)
        self.assertEqual(ledger.archive_checkpoint(str(clone)), updated.head)

    def test_fetch_sync_supports_bare_public_inbox_mirrors(self):
        mirror = Path(self.tempdir.name) / "mirror.git"
        subprocess.run(["git", "clone", "-q", "--mirror", self.repo.as_uri(), str(mirror)], check=True)
        ledger = Ledger(Path(self.tempdir.name) / "bare-ledger.sqlite3")
        first = sync_public_inbox(mirror, ledger, limit=3, fetch=True)
        (self.repo / "m").write_bytes(b"Message-ID: <new@example.test>\n\nNew.\n")
        self.git("add", "m")
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "-qm", "new mail")
        updated = sync_public_inbox(mirror, ledger, limit=3, fetch=True)
        self.assertNotEqual(first.head, updated.head)
        self.assertEqual(updated.messages.ingested, 1)
        self.assertEqual(ledger.archive_checkpoint(str(mirror)), updated.head)

    def test_sync_cli_reports_json_and_refuses_implicit_partial_bootstrap(self):
        ledger_path = Path(self.tempdir.name) / "command.sqlite3"
        args = ["sync-archive", "--archive", str(self.repo), "--ledger", str(ledger_path), "--limit", "2"]
        error_output = io.StringIO()
        with redirect_stderr(error_output):
            self.assertEqual(main(args), 1)
        failure = json.loads(error_output.getvalue())
        self.assertEqual(failure["command"], "sync-archive")
        self.assertIn("omit earlier history", failure["error"])

        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main([*args, "--allow-incomplete-bootstrap"]), 0)
        result = json.loads(output.getvalue())
        self.assertTrue(result["history_incomplete"])
        self.assertEqual(result["messages"]["ingested"], 2)

        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(args), 0)
        self.assertEqual(json.loads(output.getvalue())["messages"]["ingested"], 0)


if __name__ == "__main__":
    unittest.main()