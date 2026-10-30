import gzip
import io
import json
import sqlite3
import tempfile
import traceback
import unittest
from contextlib import closing, redirect_stdout
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError

from cli import main
from ledger import ArchiveCheckpointConflict, ArchiveSafetyBlocked, Ledger, PublicationConflict
from mail_parser import parse_message
from nntp_source import MIN_FREE_BYTES, NNTPSourceError, _client, discover_nntp, discover_nntp_numbers, discover_nntp_thread, fetch_nntp, inspect_nntp_window, nntp_source
from public_inbox import PublicInboxError


class NNTPSourceTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.ledger = Ledger(Path(self.tempdir.name) / "ledger.sqlite3")
        self.now = datetime(2026, 10, 28, 11)
        self.since = []
        self.fail_response = False
        self.discovery_bytes = b"<mail@example.test>\r\n"
        self.article_bytes = (
            b"Newsgroups: org.xenproject.lists.xen-devel\r\n"
            b"Message-ID: <mail@example.test>\r\nFrom: Example <author@example.test>\r\n"
            b"Date: Mon, 1 Jan 1990 00:00:00 +0000\r\nSubject: Reply\r\n\r\nTest body.\r\n"
        )
        self.additional_articles = {}
        self.fetch_count = 0
        self.group_low = 1
        self.group_high = 100
        self.uid_validity = 1234
        self.number_articles = {}
        self.article_number = 123

    def factory(self, host):
        test = self

        class Client:
            def __enter__(self):
                return self

            def __exit__(self, *arguments):
                return False

            def getcapabilities(self):
                return {"NEWNEWS": []}

            def date(self):
                return "111 server date", test.now

            def group(self, group):
                test.selected_group = group
                return "211 group follows", 100, test.group_low, test.group_high, group

            def group_validity(self, group):
                return test.uid_validity

            def xhdr(self, header, span, *, file):
                first, last = (int(value) for value in span.split("-"))
                for number in range(first, last + 1):
                    if number in test.number_articles:
                        file.write(f"{number} <{test.number_articles[number]}>\r\n".encode())
                return "221 headers follow", []

            def newnews(self, group, since, *, file):
                test.since.append(since)
                file.write(test.discovery_bytes)
                if test.fail_response:
                    raise EOFError("disconnected")
                return "230 list follows", []

            def article(self, message_id, *, file):
                test.fetch_count += 1
                file.write(test.additional_articles.get(message_id, test.article_bytes))
                if test.fail_response:
                    raise EOFError("disconnected")
                self.last_message_id = message_id
                return "220 article follows", SimpleNamespace(number=test.article_number, message_id=message_id)

            def stat(self, number):
                return "223 article exists", number, self.last_message_id

        return Client()

    def test_discovery_uses_fixed_cutoff_and_overlap_without_duplicates(self):
        first = discover_nntp(self.ledger, client_factory=self.factory)
        self.assertEqual(first.checkpoint.cutoff, "2026-10-22T07:00:00+00:00")
        self.assertEqual((first.discovered, first.newly_queued), (1, 1))
        self.assertFalse(first.receipt_time_coverage_verified)
        self.assertIn("sender Date", first.timestamp_basis)
        self.assertEqual((first.numeric_cursor.baseline_high, first.numeric_cursor.next_article), (100, 101))
        self.now = datetime(2026, 10, 28, 12)
        second = discover_nntp(self.ledger, client_factory=self.factory)
        self.assertEqual(second.newly_queued, 0)
        self.assertEqual(self.since, [datetime(2026, 10, 22, 7), datetime(2026, 10, 28, 10, 59)])
        self.assertEqual(second.checkpoint.bootstrap_at, first.checkpoint.bootstrap_at)
        self.assertEqual(self.ledger.pending_nntp_messages(first.checkpoint.source), ("mail@example.test",))

    def test_window_inspection_uses_unverified_header_estimates_and_inclusive_bounds(self):
        cases = (
            (b"Date: Thu, 22 Oct 2026 07:00:00 +0000\r\n", "inside", "date"),
            (b"Date: Thu, 22 Oct 2026 06:59:59 +0000\r\n", "before", "date"),
            (b"Received: from relay; Wed, 28 Oct 2026 13:00:00 +0200\r\n", "inside", "received"),
            (b"Date: Wed, 28 Oct 2026 11:00:01 +0000\r\n", "after", "date"),
            (b"Date: Wed, 28 Oct 2026 11:00:00 -0000\r\n", "unknown", None),
        )
        original = self.article_bytes
        for index, (headers, expected, basis) in enumerate(cases):
            with self.subTest(position=expected, basis=basis):
                ledger = Ledger(Path(self.tempdir.name) / f"window-{index}.sqlite3")
                self.article_bytes = original.replace(b"Date: Mon, 1 Jan 1990 00:00:00 +0000\r\n", headers)
                discover_nntp(ledger, client_factory=self.factory)
                with self.assertRaisesRegex(ValueError, "Fetch this message"):
                    inspect_nntp_window(ledger, "mail@example.test")
                fetch_nntp(ledger, client_factory=self.factory)
                result = inspect_nntp_window(Ledger(ledger.path, read_only=True), "mail@example.test")
                self.assertEqual(result.estimated_window_position, expected)
                self.assertFalse(result.receipt_time_coverage_verified)
                self.assertEqual(result.time_estimate.source if result.time_estimate else None, basis)
                if result.time_estimate:
                    self.assertFalse(result.time_estimate.verified_lore_receipt)
                stored = ledger.list_messages().messages[0].metadata["time_estimate"]
                self.assertEqual(stored["source"] if stored else None, basis)
                before = ledger.path.read_bytes()
                with redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(main([
                        "inspect-nntp-window", "--ledger", str(ledger.path), "--message-id", "mail@example.test",
                    ]), 0)
                self.assertEqual(json.loads(output.getvalue())["estimated_window_position"], expected)
                self.assertEqual(ledger.path.read_bytes(), before)
        self.article_bytes = original

    def test_partial_discovery_never_advances_the_checkpoint(self):
        self.fail_response = True
        with self.assertRaisesRegex(NNTPSourceError, "EOFError"):
            discover_nntp(self.ledger, client_factory=self.factory)
        source = nntp_source("nntp.lore.kernel.org", "org.xenproject.lists.xen-devel")
        checkpoint = self.ledger.nntp_checkpoint(source)
        self.assertEqual(checkpoint.checked_through, checkpoint.cutoff)
        self.assertEqual(self.ledger.pending_nntp_messages(source), ())

    def test_transport_errors_do_not_expose_exception_text(self):
        for operation in ("discovery", "numeric", "fetch"):
            with self.subTest(operation=operation):
                self.group_high = 100
                ledger = Ledger(Path(self.tempdir.name) / f"transport-{operation}.sqlite3")
                if operation != "discovery":
                    discover_nntp(ledger, client_factory=self.factory)
                client = self.factory("nntp.lore.kernel.org")
                if operation == "numeric":
                    self.group_high = 101
                method = {"discovery": "newnews", "numeric": "xhdr", "fetch": "article"}[operation]
                action = {"discovery": discover_nntp, "numeric": discover_nntp_numbers, "fetch": fetch_nntp}[operation]
                with patch.object(client, method, side_effect=EOFError("secret-message")):
                    with self.assertRaises(NNTPSourceError) as error:
                        action(ledger, client_factory=lambda host: client)
                self.assertNotIn("secret-message", "".join(traceback.format_exception(error.exception)))

    def test_equal_time_discovery_restarts_deduplicates_and_rejects_clock_regression(self):
        self.discovery_bytes = b"<first@example.test>\r\n<second@example.test>\r\n"
        first = discover_nntp(self.ledger, client_factory=self.factory)
        reopened = Ledger(self.ledger.path)
        self.discovery_bytes += b"<late@example.test>\r\n"
        replay = discover_nntp(reopened, client_factory=self.factory)
        self.assertEqual((first.newly_queued, replay.newly_queued), (2, 1))
        self.assertEqual(replay.checkpoint, first.checkpoint)
        self.assertEqual(replay.queried_since, "2026-10-28T10:59:00+00:00")
        self.assertEqual(reopened.pending_nntp_messages(first.checkpoint.source), (
            "first@example.test", "second@example.test", "late@example.test",
        ))
        self.now = datetime(2026, 10, 28, 10, 59, 59)
        self.discovery_bytes += b"<uncommitted@example.test>\r\n"
        with self.assertRaisesRegex(ArchiveCheckpointConflict, "clock moved"):
            discover_nntp(reopened, client_factory=self.factory)
        self.assertEqual(reopened.nntp_checkpoint(first.checkpoint.source), first.checkpoint)
        self.assertNotIn("uncommitted@example.test", reopened.pending_nntp_messages(first.checkpoint.source))

    def test_cli_discovery_exposes_queue_progress(self):
        def discover(ledger, **options):
            return discover_nntp(ledger, **options, client_factory=self.factory)

        with patch("cli.discover_nntp", side_effect=discover), redirect_stdout(io.StringIO()) as output:
            result = main(["discover-nntp", "--ledger", str(self.ledger.path)])
        self.assertEqual(result, 0)
        summary = json.loads(output.getvalue())
        self.assertEqual(summary["newly_queued"], 1)
        self.assertEqual(summary["checkpoint"]["window_hours"], 148)

    def test_cli_fetch_exposes_bounded_ingestion_progress(self):
        discover_nntp(self.ledger, client_factory=self.factory)

        def fetch(ledger, **options):
            return fetch_nntp(ledger, **options, client_factory=self.factory)

        with patch("cli.fetch_nntp", side_effect=fetch), redirect_stdout(io.StringIO()) as output:
            result = main(["fetch-nntp", "--ledger", str(self.ledger.path), "--limit", "1", "--timeout-retries", "1"])
        self.assertEqual(result, 0)
        summary = json.loads(output.getvalue())
        self.assertEqual((summary["ingested"], summary["has_more"]), (1, False))
        self.assertFalse(summary["receipt_times_verified"])
        self.assertEqual(summary["timeout_retries"], 0)

    def test_numeric_and_queue_cli_commands_report_progress_without_queue_inspection_writes(self):
        discover_nntp(self.ledger, client_factory=self.factory)
        self.group_high = 101
        self.number_articles = {101: "new@example.test"}

        def discover(ledger, **options):
            return discover_nntp_numbers(ledger, **options, client_factory=self.factory)

        with patch("cli.discover_nntp_numbers", side_effect=discover), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["discover-nntp-numbers", "--ledger", str(self.ledger.path)]), 0)
        self.assertEqual(json.loads(output.getvalue())["newly_queued"], 1)
        before = self.ledger.path.read_bytes()
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["list-nntp-queue", "--ledger", str(self.ledger.path), "--limit", "1"]), 0)
        status = json.loads(output.getvalue())
        self.assertEqual((status["pending"], status["received"]), (2, 0))
        self.assertEqual(len(status["pending_message_ids"]), 1)
        self.assertEqual(status["numeric_cursor"]["next_article"], 102)
        self.assertIsNotNone(status["next_cursor"])
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main([
                "list-nntp-queue", "--ledger", str(self.ledger.path), "--limit", "1", "--after", str(status["next_cursor"]),
            ]), 0)
        next_page = json.loads(output.getvalue())
        self.assertEqual(next_page["pending_message_ids"], ["new@example.test"])
        self.assertIsNone(next_page["next_cursor"])
        self.assertEqual((next_page["pending"], next_page["received"]), (2, 0))
        self.assertEqual(self.ledger.path.read_bytes(), before)
        for cursor in (-1, 9223372036854775808, True):
            with self.subTest(cursor=cursor), self.assertRaisesRegex(ValueError, "cursor or limit"):
                self.ledger.nntp_queue_status(status["checkpoint"]["source"], after=cursor)

    def test_numeric_discovery_catches_late_indexed_articles_in_bounded_batches(self):
        discover_nntp(self.ledger, client_factory=self.factory)
        self.group_high = 103
        self.number_articles = {101: "late@example.test", 102: "second@example.test", 103: "third@example.test"}
        first = discover_nntp_numbers(self.ledger, limit=2, client_factory=self.factory)
        self.assertEqual((first.scanned, first.newly_queued, first.has_more), (2, 2, True))
        reopened = Ledger(self.ledger.path)
        second = discover_nntp_numbers(reopened, limit=2, client_factory=self.factory)
        self.assertEqual((second.scanned, second.newly_queued, second.has_more), (1, 1, False))
        self.assertEqual(second.cursor.next_article, 104)
        self.assertIn("late@example.test", reopened.pending_nntp_messages(second.cursor.source))
        self.now = datetime(2026, 10, 29, 11)
        self.discovery_bytes = b"<late@example.test>\r\n"
        replay = discover_nntp(Ledger(self.ledger.path), client_factory=self.factory)
        self.assertEqual(replay.newly_queued, 0)
        self.assertEqual(replay.numeric_cursor.next_article, 104)

    def test_numeric_discovery_latches_missing_numbers_and_changed_group_validity(self):
        discover_nntp(self.ledger, client_factory=self.factory)
        self.group_high = 102
        self.number_articles = {101: "new@example.test"}
        with self.assertRaisesRegex(ArchiveSafetyBlocked, "gap"):
            discover_nntp_numbers(self.ledger, client_factory=self.factory)
        status = self.ledger.status()
        self.assertEqual(status.nntp_incidents[0].kind, "range_gap")
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["archive-safety", "--ledger", str(self.ledger.path)]), 0)
        self.assertEqual(json.loads(output.getvalue())["nntp_incidents"][0]["kind"], "range_gap")
        self.assertEqual(self.ledger.nntp_number_cursor(status.nntp_incidents[0].source).next_article, 101)
        with self.assertRaises(ArchiveSafetyBlocked):
            fetch_nntp(self.ledger, client_factory=self.factory)

        other = Ledger(Path(self.tempdir.name) / "other.sqlite3")
        self.group_high = 100
        discover_nntp(other, client_factory=self.factory)
        self.uid_validity = 5678
        with self.assertRaisesRegex(ArchiveSafetyBlocked, "validity_changed"):
            discover_nntp_numbers(other, client_factory=self.factory)
        self.assertEqual(other.status().nntp_incidents[0].kind, "validity_changed")

    def test_oversize_and_malformed_discovery_retain_the_cutoff(self):
        for data, error in ((b"x" * 1025, NNTPSourceError), (b"<bad>\r\n", ValueError)):
            with self.subTest(data_length=len(data)):
                self.discovery_bytes = data
                with self.assertRaises(error):
                    discover_nntp(self.ledger, response_bytes=1024, client_factory=self.factory)
                source = nntp_source("nntp.lore.kernel.org", "org.xenproject.lists.xen-devel")
                checkpoint = self.ledger.nntp_checkpoint(source)
                self.assertEqual(checkpoint.checked_through, checkpoint.cutoff)
                self.assertEqual(self.ledger.pending_nntp_messages(source), ())

    def test_library_adapter_requests_utc_and_closes_without_quit(self):
        commands = []
        closed = []

        class LibraryClient:
            def __init__(self, host, **options):
                self.host = host

            def _longcmdstring(self, command, file):
                commands.append(command)
                return "230 list follows", []

            def _close(self):
                closed.append(True)

        library = SimpleNamespace(NNTP_SSL=LibraryClient, NNTPError=RuntimeError)
        with patch("nntp_source.importlib.import_module", return_value=library):
            with _client("nntp.lore.kernel.org") as client:
                client.newnews("org.xenproject.lists.xen-devel", self.now)
        self.assertEqual(commands, ["NEWNEWS org.xenproject.lists.xen-devel 20261028 110000 GMT"])
        self.assertEqual(closed, [True])

    def test_library_adapter_reports_only_valid_nntp_error_codes(self):
        closed = []

        class LibraryClient:
            def __init__(self, host, **options):
                pass

            def _close(self):
                closed.append(True)

        library = SimpleNamespace(NNTP_SSL=LibraryClient, NNTPError=RuntimeError)
        cases = (
            ("430 secret-message", "NNTP server rejected a request (430); pending work retained"),
            ("503 secret-message", "NNTP server rejected a request (503); pending work retained"),
            ("430secret-message", "NNTP server rejected a request; pending work retained"),
            ("unexpected secret-message", "NNTP server rejected a request; pending work retained"),
        )
        with patch("nntp_source.importlib.import_module", return_value=library):
            for response, expected in cases:
                with self.subTest(response=response), self.assertRaises(NNTPSourceError) as error:
                    with _client("nntp.lore.kernel.org"):
                        raise RuntimeError(response)
                self.assertEqual(str(error.exception), expected)
                self.assertNotIn("secret-message", str(error.exception))
                self.assertNotIn("secret-message", "".join(traceback.format_exception(error.exception)))
        self.assertEqual(len(closed), len(cases))

    def test_source_rejects_command_injection_and_group_wildcards(self):
        for host, group in (("host\r\nQUIT", "group"), ("host", "*"), ("host", "group\r\nQUIT")):
            with self.subTest(host=host, group=group), self.assertRaises(ValueError):
                nntp_source(host, group)

    def test_fetch_preserves_source_bytes_and_never_calls_sender_date_receipt_time(self):
        discovery = discover_nntp(self.ledger, client_factory=self.factory)
        result = fetch_nntp(self.ledger, client_factory=self.factory)
        self.assertEqual((result.ingested, result.replayed, result.has_more), (1, 0, False))
        with closing(sqlite3.connect(self.ledger.path)) as connection:
            stored = connection.execute("SELECT raw_message, metadata_json FROM messages").fetchone()
        self.assertEqual(stored[0], self.article_bytes)
        metadata = json.loads(stored[1])
        self.assertIsNone(metadata["lore_received_at"])
        self.assertEqual(metadata["receipt_time_status"], "unverified")
        self.assertEqual(metadata["from_address"], "author@example.test")
        self.assertEqual(metadata["kind"], "other")
        self.assertEqual(self.selected_group, "org.xenproject.lists.xen-devel")
        self.assertEqual(self.ledger.pending_nntp_messages(discovery.checkpoint.source), ())
        fetch_nntp(self.ledger, client_factory=self.factory)
        self.assertEqual(self.fetch_count, 1)

    def test_thread_discovery_queues_siblings_for_group_verified_fetch_and_replays(self):
        discovery = discover_nntp(self.ledger, client_factory=self.factory)
        fetch_nntp(self.ledger, client_factory=self.factory)
        record = b"From mboxrd@z Thu Jan  1 00:00:00 1970\nMessage-ID: <%s>\n\nBody.\n\n"
        compressed = gzip.compress(record % b"mail@example.test" + record % b"older-sibling@example.test")
        with patch("public_inbox.build_opener") as opener:
            opener.return_value.open.side_effect = lambda *args, **kwargs: io.BytesIO(compressed)
            first = discover_nntp_thread(self.ledger, "mail@example.test")
            replay = discover_nntp_thread(self.ledger, "mail@example.test")
            self.assertEqual(opener.return_value.open.call_args.args[0].get_header("User-agent"), "lore-gitea-mirror/0.1")
        self.assertEqual((first.discovered, first.newly_queued, replay.newly_queued), (2, 1, 0))
        self.assertFalse(first.thread_coverage_verified)
        self.assertEqual(self.ledger.pending_nntp_messages(discovery.checkpoint.source), ("older-sibling@example.test",))
        self.assertEqual(self.ledger.status().messages, 1)

    def test_thread_discovery_cli_reports_candidates_and_safe_byte_limits(self):
        discovery = discover_nntp(self.ledger, client_factory=self.factory)
        fetch_nntp(self.ledger, client_factory=self.factory)
        record = b"From mboxrd@z Thu Jan  1 00:00:00 1970\nMessage-ID: <%s>\n\nBody.\n\n"
        compressed = gzip.compress(record % b"mail@example.test" + record % b"sibling@example.test")
        with patch("public_inbox.build_opener") as opener, redirect_stdout(io.StringIO()) as output:
            opener.return_value.open.return_value = io.BytesIO(compressed)
            result = main([
                "discover-nntp-thread", "--ledger", str(self.ledger.path), "--message-id", "mail@example.test",
                "--max-compressed-bytes", "4096", "--max-uncompressed-bytes", "8192",
            ])
        self.assertEqual(result, 0)
        summary = json.loads(output.getvalue())
        self.assertEqual((summary["discovered"], summary["newly_queued"]), (2, 1))
        self.assertFalse(summary["thread_coverage_verified"])
        self.assertEqual(self.ledger.pending_nntp_messages(discovery.checkpoint.source), ("sibling@example.test",))

    def test_thread_range_cli_replays_candidates_and_reports_ambiguous_empty_results(self):
        discovery = discover_nntp(self.ledger, client_factory=self.factory)
        fetch_nntp(self.ledger, client_factory=self.factory)
        record = b"From mboxrd@z Thu Jan  1 00:00:00 1970\nMessage-ID: <sibling@example.test>\n\nBody.\n\n"
        compressed = gzip.compress(record)
        arguments = [
            "discover-nntp-thread", "--ledger", str(self.ledger.path), "--message-id", "mail@example.test",
            "--from-index-time", "10", "--through-index-time", "20",
        ]
        for expected_new in (1, 0):
            with patch("public_inbox.build_opener") as opener, redirect_stdout(io.StringIO()) as output:
                opener.return_value.open.return_value = io.BytesIO(compressed)
                self.assertEqual(main(arguments), 0)
            summary = json.loads(output.getvalue())
            self.assertEqual((summary["discovered"], summary["newly_queued"]), (1, expected_new))
            self.assertEqual((summary["from_index_time"], summary["through_index_time"]), (10, 20))
            self.assertFalse(summary["thread_coverage_verified"])

        error = HTTPError("https://lore.example/secret-id", 404, "Not Found", {}, io.BytesIO(b"No results found\n"))
        with patch("public_inbox.build_opener") as opener, redirect_stdout(io.StringIO()) as output:
            opener.return_value.open.side_effect = error
            self.assertEqual(main(arguments), 0)
        self.assertTrue(json.loads(output.getvalue())["empty_or_unavailable"])
        self.assertEqual(self.ledger.pending_nntp_messages(discovery.checkpoint.source), ("sibling@example.test",))
        reopened = Ledger(self.ledger.path, read_only=True)
        searches = reopened.list_nntp_thread_searches(discovery.checkpoint.source, "mail@example.test")
        self.assertEqual(len(searches.searches), 1)
        self.assertEqual((searches.searches[0].from_index_time, searches.searches[0].through_index_time), (10, 20))
        self.assertTrue(searches.searches[0].empty_or_unavailable)

        with patch("public_inbox.build_opener") as opener:
            opener.return_value.open.return_value = io.BytesIO(compressed)
            discover_nntp_thread(self.ledger, "mail@example.test", through_index_time=9)
        before = self.ledger.path.read_bytes()
        list_arguments = ["list-nntp-thread-searches", "--ledger", str(self.ledger.path), "--message-id", "mail@example.test"]
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main([*list_arguments, "--limit", "1"]), 0)
        page = json.loads(output.getvalue())
        self.assertEqual(len(page["searches"]), 1)
        self.assertIsNotNone(page["next_cursor"])
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main([*list_arguments, "--after", str(page["next_cursor"])]), 0)
        self.assertEqual(len(json.loads(output.getvalue())["searches"]), 1)
        self.assertEqual(self.ledger.path.read_bytes(), before)

    def test_thread_range_progress_and_candidates_roll_back_together(self):
        discovery = discover_nntp(self.ledger, client_factory=self.factory)
        fetch_nntp(self.ledger, client_factory=self.factory)
        record = b"From mboxrd@z Thu Jan  1 00:00:00 1970\nMessage-ID: <sibling@example.test>\n\nBody.\n\n"
        with self.ledger._connection() as connection:
            connection.execute(
                "CREATE TRIGGER fail_thread_search BEFORE INSERT ON nntp_thread_searches "
                "BEGIN SELECT RAISE(ABORT, 'storage failure'); END"
            )
        with patch("public_inbox.build_opener") as opener:
            opener.return_value.open.return_value = io.BytesIO(gzip.compress(record))
            with self.assertRaises(sqlite3.IntegrityError):
                discover_nntp_thread(self.ledger, "mail@example.test", from_index_time=10)
        self.assertEqual(self.ledger.pending_nntp_messages(discovery.checkpoint.source), ())
        self.assertEqual(self.ledger.list_nntp_thread_searches(discovery.checkpoint.source, "mail@example.test").searches, ())

    def test_thread_discovery_rejects_unverified_anchor_and_unrelated_export(self):
        discovery = discover_nntp(self.ledger, client_factory=self.factory)
        with patch("public_inbox.build_opener") as opener:
            with self.assertRaisesRegex(ValueError, "Fetch the thread anchor"):
                discover_nntp_thread(self.ledger, "mail@example.test")
            opener.assert_not_called()
        fetch_nntp(self.ledger, client_factory=self.factory)
        other = b"From mboxrd@z Thu Jan  1 00:00:00 1970\nMessage-ID: <other@example.test>\n\nBody.\n\n"
        with patch("public_inbox.build_opener") as opener:
            opener.return_value.open.return_value = io.BytesIO(gzip.compress(other))
            with self.assertRaisesRegex(PublicInboxError, "does not contain"):
                discover_nntp_thread(self.ledger, "mail@example.test")
        self.assertEqual(self.ledger.pending_nntp_messages(discovery.checkpoint.source), ())

    def test_fetch_restart_replays_stored_but_unacknowledged_article(self):
        discovery = discover_nntp(self.ledger, client_factory=self.factory)
        self.ledger.record_message("mail@example.test", self.article_bytes)
        result = fetch_nntp(self.ledger, client_factory=self.factory)
        self.assertEqual((result.ingested, result.replayed), (0, 1))
        self.assertEqual(self.ledger.pending_nntp_messages(discovery.checkpoint.source), ())

    def test_fetch_blocks_before_article_when_group_identity_changes(self):
        discovery = discover_nntp(self.ledger, client_factory=self.factory)
        self.uid_validity += 1
        with self.assertRaisesRegex(ArchiveSafetyBlocked, "validity_changed"):
            fetch_nntp(self.ledger, client_factory=self.factory)
        self.assertEqual(self.fetch_count, 0)
        self.assertEqual(self.ledger.pending_nntp_messages(discovery.checkpoint.source), ("mail@example.test",))
        self.assertEqual(self.ledger.status().nntp_incidents[0].kind, "validity_changed")

    def test_fetch_checks_group_bounds_and_requires_numeric_cursor(self):
        for case in ("high_decreased", "low_increased", "missing_cursor"):
            with self.subTest(case=case):
                self.group_low, self.group_high = 1, 100
                ledger = Ledger(Path(self.tempdir.name) / f"fetch-{case}.sqlite3")
                discovery = discover_nntp(ledger, client_factory=self.factory)
                if case == "high_decreased":
                    self.group_high = 99
                elif case == "low_increased":
                    self.group_low, self.group_high = 102, 102
                else:
                    with ledger._connection() as connection:
                        connection.execute("DELETE FROM nntp_number_cursors WHERE source = ?", (discovery.checkpoint.source,))
                expected_error = "establish a numeric baseline" if case == "missing_cursor" else case
                expected_type = ValueError if case == "missing_cursor" else ArchiveSafetyBlocked
                with self.assertRaisesRegex(expected_type, expected_error):
                    fetch_nntp(ledger, client_factory=self.factory)
                self.assertEqual(self.fetch_count, 0)
                self.assertEqual(ledger.pending_nntp_messages(discovery.checkpoint.source), ("mail@example.test",))
                if case != "missing_cursor":
                    self.assertEqual(ledger.status().nntp_incidents[0].kind, case)

    def test_fetch_replay_indexes_older_stored_message_before_acknowledging(self):
        discovery = discover_nntp(self.ledger, client_factory=self.factory)
        self.article_bytes = self.article_bytes.replace(
            b"Subject: Reply", b"References: <old@example.test>\r\nSubject: Reply",
        )
        self.ledger.record_message("mail@example.test", self.article_bytes)
        with self.ledger._connection() as connection:
            connection.execute("DELETE FROM message_links WHERE child_id = ?", ("mail@example.test",))
            connection.execute("UPDATE message_link_index_cursor SET last_rowid = 0 WHERE id = 1")
        result = fetch_nntp(Ledger(self.ledger.path), client_factory=self.factory)
        self.assertEqual((result.ingested, result.replayed, result.dependencies_queued), (0, 1, 1))
        self.assertEqual(self.ledger.pending_nntp_messages(discovery.checkpoint.source), ("old@example.test",))
        self.assertFalse(self.ledger.status().link_index_complete)
        self.additional_articles["<old@example.test>"] = self.article_bytes.replace(
            b"Message-ID: <mail@example.test>", b"Message-ID: <old@example.test>",
        ).replace(b"References: <old@example.test>", b"References: <older@example.test>")
        result = fetch_nntp(Ledger(self.ledger.path), limit=1, client_factory=self.factory)
        self.assertEqual((result.ingested, result.dependencies_queued), (1, 1))
        self.assertEqual(self.ledger.pending_nntp_messages(discovery.checkpoint.source), ("older@example.test",))

    def test_fetch_acknowledgment_rejects_mismatched_stored_message(self):
        discovery = discover_nntp(self.ledger, client_factory=self.factory)
        self.ledger.record_message("mail@example.test", self.article_bytes.replace(
            b"Message-ID: <mail@example.test>", b"Message-ID: <other@example.test>",
        ))
        with self.assertRaisesRegex(ArchiveCheckpointConflict, "does not match"):
            self.ledger.mark_nntp_received(discovery.checkpoint.source, "mail@example.test")
        self.assertEqual(self.ledger.pending_nntp_messages(discovery.checkpoint.source), ("mail@example.test",))

    def test_parent_repair_rescans_acknowledged_messages_in_idempotent_pages(self):
        self.discovery_bytes = b"<mail@example.test>\r\n<next@example.test>\r\n"
        discovery = discover_nntp(self.ledger, client_factory=self.factory)
        for identifier, parent in (("mail", "old"), ("next", "older")):
            article = self.article_bytes.replace(b"mail@example.test", f"{identifier}@example.test".encode()).replace(
                b"Subject: Reply", f"References: <{parent}@example.test>\r\nSubject: Reply".encode(),
            )
            self.ledger.record_message(f"{identifier}@example.test", article)
        with self.ledger._connection() as connection:
            connection.execute("UPDATE nntp_message_queue SET received = 1 WHERE source = ?", (discovery.checkpoint.source,))
            connection.execute("DELETE FROM message_links")
            connection.execute("UPDATE message_link_index_cursor SET last_rowid = 0 WHERE id = 1")
        acknowledge = self.ledger.mark_nntp_received

        def interrupt_after_first(source, message_id):
            if message_id == "next@example.test":
                raise OSError("interrupted")
            return acknowledge(source, message_id)

        with patch.object(self.ledger, "mark_nntp_received", side_effect=interrupt_after_first):
            with self.assertRaisesRegex(OSError, "interrupted"):
                self.ledger.repair_nntp_parents(discovery.checkpoint.source, limit=2)
        self.assertEqual(self.ledger.pending_nntp_messages(discovery.checkpoint.source), ("old@example.test",))
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["repair-nntp-parents", "--ledger", str(self.ledger.path), "--limit", "1"]), 0)
        first = json.loads(output.getvalue())
        self.assertEqual((first["processed"], first["newly_queued"]), (1, 0))
        self.assertIsNotNone(first["next_cursor"])
        second = Ledger(self.ledger.path).repair_nntp_parents(discovery.checkpoint.source, after=first["next_cursor"], limit=1)
        self.assertEqual((second.processed, second.newly_queued, second.next_cursor), (1, 1, None))
        self.assertEqual(self.ledger.pending_nntp_messages(discovery.checkpoint.source), ("old@example.test", "older@example.test"))
        replay = self.ledger.repair_nntp_parents(discovery.checkpoint.source)
        self.assertEqual((replay.processed, replay.newly_queued, replay.next_cursor), (2, 0, None))
        self.assertFalse(self.ledger.status().link_index_complete)
        for cursor in (-1, True, 9223372036854775808):
            with self.subTest(cursor=cursor), self.assertRaisesRegex(ValueError, "cursor or limit"):
                self.ledger.repair_nntp_parents(discovery.checkpoint.source, after=cursor)
        self.ledger.record_nntp_incident(discovery.checkpoint.source, "range_gap", 101, 102)
        with self.assertRaises(ArchiveSafetyBlocked):
            self.ledger.repair_nntp_parents(discovery.checkpoint.source)

    def test_parent_repair_cli_does_not_migrate_an_old_ledger(self):
        with self.ledger._connection() as connection:
            connection.execute("UPDATE schema_version SET version = 21")
        with patch("sys.stderr", new_callable=io.StringIO) as error:
            self.assertEqual(main(["repair-nntp-parents", "--ledger", str(self.ledger.path)]), 1)
        self.assertIn("current ledger schema", error.getvalue())
        with closing(sqlite3.connect(self.ledger.path)) as connection:
            self.assertEqual(connection.execute("SELECT version FROM schema_version").fetchone()[0], 21)

    def test_fetch_recursively_queues_older_parents_and_deduplicates_cycles(self):
        discovery = discover_nntp(self.ledger, client_factory=self.factory)
        self.article_bytes = self.article_bytes.replace(
            b"Subject: Reply", b"References: <old@example.test> <old@example.test>\r\nSubject: Reply",
        )
        self.additional_articles["<old@example.test>"] = self.article_bytes.replace(
            b"Message-ID: <mail@example.test>", b"Message-ID: <old@example.test>",
        ).replace(
            b"References: <old@example.test> <old@example.test>",
            b"References: <mail@example.test> <older@example.test>",
        )
        self.additional_articles["<older@example.test>"] = self.article_bytes.replace(
            b"Message-ID: <mail@example.test>", b"Message-ID: <older@example.test>",
        )
        for expected_parent in ("old@example.test", "older@example.test", None):
            result = fetch_nntp(self.ledger, limit=1, client_factory=self.factory)
            pending = self.ledger.pending_nntp_messages(discovery.checkpoint.source)
            self.assertEqual(pending, (expected_parent,) if expected_parent else ())
            self.assertEqual(result.dependencies_queued, int(expected_parent is not None))
        self.assertEqual(self.ledger.status().messages, 3)
        self.assertEqual(self.fetch_count, 3)

    def test_invalid_parent_does_not_acknowledge_the_child(self):
        discovery = discover_nntp(self.ledger, client_factory=self.factory)
        self.article_bytes = self.article_bytes.replace(
            b"Subject: Reply", b"References: <bad\x00@example.test>\r\nSubject: Reply",
        )
        with self.assertRaisesRegex(ValueError, "invalid Message-ID"):
            fetch_nntp(self.ledger, client_factory=self.factory)
        self.assertEqual(self.ledger.pending_nntp_messages(discovery.checkpoint.source), ("mail@example.test",))
        self.assertEqual(self.ledger.status().messages, 1)

    def test_fetch_errors_retain_pending_article(self):
        discovery = discover_nntp(self.ledger, client_factory=self.factory)
        cases = ("disconnect", "wrong-id", "wrong-group", "unknown-number", "negative-number", "boolean-number", "text-number", "oversize", "low-disk")
        original = self.article_bytes
        for case in cases:
            with self.subTest(case=case):
                self.fail_response = case == "disconnect"
                self.article_bytes = original.replace(b"mail@example.test", b"wrong@example.test") if case == "wrong-id" else original
                if case == "wrong-group":
                    self.article_bytes = original.replace(b"org.xenproject.lists.xen-devel", b"another.group")
                if case == "oversize":
                    self.article_bytes = b"x" * 1025
                self.article_number = {"unknown-number": 0, "negative-number": -1, "boolean-number": True, "text-number": "123"}.get(case, 123)
                free = 0 if case == "low-disk" else MIN_FREE_BYTES * 4
                with patch("nntp_source.shutil.disk_usage", return_value=SimpleNamespace(free=free)):
                    with self.assertRaises(NNTPSourceError):
                        fetch_nntp(self.ledger, article_bytes=1024, client_factory=self.factory)
                self.assertEqual(self.ledger.pending_nntp_messages(discovery.checkpoint.source), ("mail@example.test",))
                self.assertEqual(self.ledger.status().messages, 0)

    def test_fetch_checks_article_number_in_selected_group(self):
        discovery = discover_nntp(self.ledger, client_factory=self.factory)
        cases = (
            ("wrong-id", ("223 article exists", 123, "<other@example.test>")),
            ("wrong-number", ("223 article exists", 124, "<mail@example.test>")),
            ("wrong-code", ("220 article exists", 123, "<mail@example.test>")),
            ("timeout", TimeoutError("disconnected")),
            ("malformed-number", ValueError("secret-message")),
            ("missing-field", IndexError("secret-message")),
        )
        for case, response in cases:
            with self.subTest(case=case):
                client = self.factory("nntp.lore.kernel.org")
                options = {"side_effect": response} if isinstance(response, Exception) else {"return_value": response}
                with patch.object(client, "stat", **options) as stat:
                    with self.assertRaises(NNTPSourceError) as error:
                        fetch_nntp(self.ledger, timeout_retries=1, client_factory=lambda host: client)
                    stat.assert_called_once_with(123)
                    self.assertNotIn("secret-message", str(error.exception))
                self.assertEqual(self.ledger.pending_nntp_messages(discovery.checkpoint.source), ("mail@example.test",))
                self.assertEqual(self.ledger.status().messages, 0)
        client = self.factory("nntp.lore.kernel.org")
        with patch.object(client, "stat", wraps=client.stat) as stat:
            result = fetch_nntp(self.ledger, client_factory=lambda host: client)
            stat.assert_called_once_with(123)
        self.assertEqual((result.ingested, result.has_more), (1, False))

    def test_fetch_rejects_malformed_article_response_without_exposing_it(self):
        discovery = discover_nntp(self.ledger, client_factory=self.factory)
        for failure in (ValueError("secret-message"), IndexError("secret-message")):
            with self.subTest(failure=type(failure).__name__):
                client = self.factory("nntp.lore.kernel.org")
                with patch.object(client, "article", side_effect=failure) as article, patch.object(client, "stat") as stat:
                    with self.assertRaises(NNTPSourceError) as error:
                        fetch_nntp(self.ledger, timeout_retries=1, client_factory=lambda host: client)
                    article.assert_called_once()
                    stat.assert_not_called()
                self.assertNotIn("secret-message", str(error.exception))
                self.assertEqual(self.ledger.pending_nntp_messages(discovery.checkpoint.source), ("mail@example.test",))
                self.assertEqual(self.ledger.status().messages, 0)

    def test_fetch_resumes_after_a_mid_batch_timeout(self):
        self.discovery_bytes = b"<mail@example.test>\r\n<second@example.test>\r\n<third@example.test>\r\n"
        discovery = discover_nntp(self.ledger, client_factory=self.factory)
        for identifier in ("second", "third"):
            self.additional_articles[f"<{identifier}@example.test>"] = self.article_bytes.replace(
                b"mail@example.test", f"{identifier}@example.test".encode(),
            )
        client = self.factory("nntp.lore.kernel.org")
        original_article = client.article

        def interrupted_article(message_id, *, file):
            if message_id == "<second@example.test>":
                file.write(b"partial response")
                raise TimeoutError("disconnected")
            return original_article(message_id, file=file)

        with patch.object(client, "article", side_effect=interrupted_article):
            with self.assertRaisesRegex(NNTPSourceError, "TimeoutError; current item retained"):
                fetch_nntp(self.ledger, limit=3, client_factory=lambda host: client)
        self.assertEqual(self.ledger.status().messages, 1)
        self.assertEqual(self.ledger.pending_nntp_messages(discovery.checkpoint.source), (
            "second@example.test", "third@example.test",
        ))
        resumed = fetch_nntp(Ledger(self.ledger.path), limit=3, client_factory=self.factory)
        self.assertEqual((resumed.ingested, resumed.replayed, resumed.has_more), (2, 0, False))
        self.assertEqual(self.ledger.status().messages, 3)
        self.assertEqual(self.ledger.pending_nntp_messages(discovery.checkpoint.source), ())

    def test_fetch_reconnects_only_uncommitted_article_after_timeout(self):
        self.discovery_bytes = b"<mail@example.test>\r\n<second@example.test>\r\n"
        discovery = discover_nntp(self.ledger, client_factory=self.factory)
        second = self.article_bytes.replace(b"mail@example.test", b"second@example.test")
        self.additional_articles["<second@example.test>"] = second
        first_client = self.factory("nntp.lore.kernel.org")
        retry_client = self.factory("nntp.lore.kernel.org")
        first_article = first_client.article
        retry_article = retry_client.article
        requested = []

        def interrupted(message_id, *, file):
            requested.append(message_id)
            if message_id == "<second@example.test>":
                file.write(b"partial")
                raise TimeoutError("disconnected")
            return first_article(message_id, file=file)

        def resumed(message_id, *, file):
            requested.append(message_id)
            return retry_article(message_id, file=file)

        clients = iter((first_client, retry_client))
        with patch.object(first_client, "article", side_effect=interrupted), patch.object(retry_client, "article", side_effect=resumed):
            result = fetch_nntp(self.ledger, limit=2, article_bytes=1024, batch_bytes=4096,
                                timeout_retries=1, client_factory=lambda host: next(clients))
        self.assertEqual(requested, ["<mail@example.test>", "<second@example.test>", "<second@example.test>"])
        self.assertEqual((result.ingested, result.replayed, result.timeout_retries, result.has_more), (2, 0, 1, False))
        self.assertEqual(result.received_bytes, len(self.article_bytes) + len(second) + len(b"partial"))
        self.assertEqual(self.ledger.pending_nntp_messages(discovery.checkpoint.source), ())

    def test_fetch_timeout_retries_exhaust_or_stop_at_byte_budget(self):
        for case in ("exhausted", "budget"):
            with self.subTest(case=case):
                ledger = Ledger(Path(self.tempdir.name) / f"timeout-{case}.sqlite3")
                discovery = discover_nntp(ledger, client_factory=self.factory)
                clients = [self.factory("nntp.lore.kernel.org") for _ in range(2)]
                calls = []

                def timed_out(message_id, *, file):
                    calls.append(message_id)
                    file.write(b"partial")
                    raise TimeoutError("disconnected")

                with patch.object(clients[0], "article", side_effect=timed_out), patch.object(clients[1], "article", side_effect=timed_out):
                    available = iter(clients)
                    expected = "TimeoutError" if case == "exhausted" else "remaining batch byte budget"
                    with self.assertRaisesRegex(NNTPSourceError, expected):
                        fetch_nntp(ledger, article_bytes=1024, batch_bytes=4096 if case == "exhausted" else 1024,
                                   timeout_retries=1, client_factory=lambda host: next(available))
                self.assertEqual(len(calls), 2 if case == "exhausted" else 1)
                self.assertEqual(ledger.status().messages, 0)
                self.assertEqual(ledger.pending_nntp_messages(discovery.checkpoint.source), ("mail@example.test",))
        for retries in (-1, 4, True, "1"):
            with self.subTest(retries=retries), self.assertRaisesRegex(ValueError, "timeout retries"):
                fetch_nntp(self.ledger, timeout_retries=retries, client_factory=self.factory)

    def test_fetch_timeout_reconnect_blocks_changed_group_before_retry(self):
        discovery = discover_nntp(self.ledger, client_factory=self.factory)
        first_client = self.factory("nntp.lore.kernel.org")
        retry_client = self.factory("nntp.lore.kernel.org")

        def timed_out(message_id, *, file):
            file.write(b"partial")
            self.uid_validity += 1
            raise TimeoutError("disconnected")

        clients = iter((first_client, retry_client))
        with patch.object(first_client, "article", side_effect=timed_out), patch.object(retry_client, "article") as article:
            with self.assertRaisesRegex(ArchiveSafetyBlocked, "validity_changed"):
                fetch_nntp(self.ledger, timeout_retries=1, client_factory=lambda host: next(clients))
            article.assert_not_called()
        self.assertEqual(self.ledger.pending_nntp_messages(discovery.checkpoint.source), ("mail@example.test",))
        self.assertEqual(self.ledger.status().nntp_incidents[0].kind, "validity_changed")

    def test_fetch_timeout_retry_does_not_retry_other_failures(self):
        discovery = discover_nntp(self.ledger, client_factory=self.factory)
        for case in ("disconnect", "wrong_group"):
            with self.subTest(case=case):
                self.fail_response = case == "disconnect"
                if case == "wrong_group":
                    self.article_bytes = self.article_bytes.replace(b"org.xenproject.lists.xen-devel", b"another.group")
                connections = []

                def client(host):
                    connections.append(host)
                    return self.factory(host)

                with self.assertRaises(NNTPSourceError):
                    fetch_nntp(self.ledger, timeout_retries=1, client_factory=client)
                self.assertEqual(len(connections), 1)
                self.assertEqual(self.ledger.pending_nntp_messages(discovery.checkpoint.source), ("mail@example.test",))
                self.assertEqual(self.ledger.status().messages, 0)

    def test_fetch_timeout_retry_limit_applies_to_the_whole_batch(self):
        self.discovery_bytes = b"<mail@example.test>\r\n<second@example.test>\r\n"
        discovery = discover_nntp(self.ledger, client_factory=self.factory)
        first_client = self.factory("nntp.lore.kernel.org")
        retry_client = self.factory("nntp.lore.kernel.org")
        retry_article = retry_client.article
        requested = []

        def first_timeout(message_id, *, file):
            requested.append(message_id)
            raise TimeoutError("disconnected")

        def second_timeout(message_id, *, file):
            requested.append(message_id)
            if message_id == "<second@example.test>":
                raise TimeoutError("disconnected")
            return retry_article(message_id, file=file)

        clients = iter((first_client, retry_client))
        with patch.object(first_client, "article", side_effect=first_timeout), patch.object(retry_client, "article", side_effect=second_timeout):
            with self.assertRaisesRegex(NNTPSourceError, "TimeoutError; current item retained"):
                fetch_nntp(self.ledger, limit=2, timeout_retries=1, client_factory=lambda host: next(clients))
        self.assertEqual(requested, ["<mail@example.test>", "<mail@example.test>", "<second@example.test>"])
        self.assertEqual(self.ledger.status().messages, 1)
        self.assertEqual(self.ledger.pending_nntp_messages(discovery.checkpoint.source), ("second@example.test",))

    def test_fetch_selected_message_leaves_failed_parent_pending_and_blocks_ancestry(self):
        self.discovery_bytes = b"<mail@example.test>\r\n<later@example.test>\r\n"
        later = self.article_bytes.replace(b"mail@example.test", b"later@example.test").replace(
            b"Subject: Reply", b"In-Reply-To: <mail@example.test>\r\nSubject: Reply",
        )
        self.additional_articles["<later@example.test>"] = later
        self.article_bytes = self.article_bytes.replace(b"org.xenproject.lists.xen-devel", b"another.group")
        discovery = discover_nntp(self.ledger, client_factory=self.factory)
        with self.assertRaisesRegex(NNTPSourceError, "group"):
            fetch_nntp(self.ledger, client_factory=self.factory)

        def fetch(ledger, **options):
            return fetch_nntp(ledger, **options, client_factory=self.factory)

        with patch("cli.fetch_nntp", side_effect=fetch), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main([
                "fetch-nntp", "--ledger", str(self.ledger.path), "--message-id", "<later@example.test>",
            ]), 0)
        result = json.loads(output.getvalue())
        self.assertEqual((result["ingested"], result["has_more"]), (1, True))
        self.assertEqual(self.ledger.pending_nntp_messages(discovery.checkpoint.source), ("mail@example.test",))
        with self.assertRaisesRegex(PublicationConflict, "ancestry is incomplete"):
            self.ledger.assert_message_ancestry_available((parse_message(later),))
        fetch_count = self.fetch_count
        for selected in ("later@example.test", "unknown@example.test"):
            with self.subTest(selected=selected), self.assertRaisesRegex(ValueError, "not pending"):
                fetch_nntp(self.ledger, message_id=selected, client_factory=self.factory)
        self.assertEqual(self.fetch_count, fetch_count)

    def test_fetch_after_cursor_retains_earlier_pending_messages(self):
        self.discovery_bytes = b"<mail@example.test>\r\n<later@example.test>\r\n<last@example.test>\r\n"
        later = self.article_bytes.replace(b"mail@example.test", b"later@example.test").replace(
            b"Subject: Reply", b"In-Reply-To: <mail@example.test>\r\nSubject: Reply",
        )
        self.additional_articles["<later@example.test>"] = later
        self.additional_articles["<last@example.test>"] = self.article_bytes.replace(b"mail@example.test", b"last@example.test")
        self.article_bytes = self.article_bytes.replace(b"org.xenproject.lists.xen-devel", b"another.group")
        discovery = discover_nntp(self.ledger, client_factory=self.factory)
        with self.assertRaisesRegex(NNTPSourceError, "group"):
            fetch_nntp(self.ledger, client_factory=self.factory)
        cursor = self.ledger.nntp_queue_status(discovery.checkpoint.source, limit=1).next_cursor

        def fetch(ledger, **options):
            return fetch_nntp(ledger, **options, client_factory=self.factory)

        with patch("cli.fetch_nntp", side_effect=fetch), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main([
                "fetch-nntp", "--ledger", str(self.ledger.path), "--after", str(cursor), "--limit", "2",
            ]), 0)
        result = json.loads(output.getvalue())
        self.assertEqual((result["ingested"], result["has_more"]), (2, True))
        self.assertEqual(self.ledger.pending_nntp_messages(discovery.checkpoint.source), ("mail@example.test",))
        with self.assertRaisesRegex(PublicationConflict, "ancestry is incomplete"):
            self.ledger.assert_message_ancestry_available((parse_message(later),))
        fetch_count = self.fetch_count
        replay = fetch_nntp(self.ledger, after=cursor, client_factory=self.factory)
        self.assertEqual((replay.ingested, replay.has_more), (0, True))
        for cursor in (-1, True, 9223372036854775808):
            with self.subTest(cursor=cursor), self.assertRaises(ValueError):
                fetch_nntp(self.ledger, after=cursor, client_factory=self.factory)
        with self.assertRaises(ValueError):
            fetch_nntp(self.ledger, message_id="mail@example.test", after=1, client_factory=self.factory)
        self.assertEqual(self.fetch_count, fetch_count)
