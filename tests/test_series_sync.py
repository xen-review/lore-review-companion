import hashlib
import json
import tempfile
import unittest
import sqlite3
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import replace
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from lore_review_companion.b4_adapter import AssemblyResult
from lore_review_companion.cli import main
from lore_review_companion.git_branches import RevisionBranchResult, revision_branch_name
from lore_review_companion.forge_api import ForgeAPI
from lore_review_companion.forge_pulls import ForgePullPublisher, PullRequestConflict, _series_marker
from lore_review_companion.forge_comments import DeliveryUncertainError
from lore_review_companion.ledger import ArchiveSafetyBlocked, Ledger, MessageConflictError, PublicationConflict
from lore_review_companion.mail_parser import parse_message
from lore_review_companion.preflight import PreflightResult
from lore_review_companion.series import discover_series
from lore_review_companion.series_sync import PublicationUncertain, sync_series_revision
from test_forge_pulls import PullTransport
from test_series import message


class SeriesSyncTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.repository = Path(self.tempdir.name)
        self.transport = PullTransport()
        self.api = ForgeAPI("https://gitea.test/api/v1/", transport=self.transport, max_retries=0)
        self.revision = discover_series([message("patch@example.test", "[PATCH 1/1] xen: change")])[0]
        self.remote = "https://gitea.test/o/r.git"
        self.branch_writes = []
        self.branch_exists = False
        self.ledger = Ledger(self.repository / "ledger.sqlite3")

    @contextmanager
    def prepare(self, _repository, _base_ref, _mailbox):
        yield SimpleNamespace(result=PreflightResult("b" * 40, "a" * 40, 1))

    def ensure_branch(self, prepared, remote, **kwargs):
        self.branch_writes.append(kwargs.get("write_enabled", False))
        created = kwargs.get("write_enabled", False) and not self.branch_exists
        if created:
            self.branch_exists = True
        return RevisionBranchResult(
            revision_branch_name(kwargs["series_key"], kwargs["revision"]),
            prepared.result.head_commit, created, planned=not self.branch_exists,
        )

    def sync(self, publisher, **kwargs):
        with (
            patch("lore_review_companion.series_sync.assemble_local_series", return_value=AssemblyResult(b"patch", "", "")),
            patch("lore_review_companion.series_sync.prepare_patch_mailbox", new=self.prepare),
            patch("lore_review_companion.series_sync.ensure_revision_branch", new=self.ensure_branch),
        ):
            return sync_series_revision(
                self.revision, self.repository, "HEAD", self.remote, publisher,
                target_branch="main", ledger=self.ledger, **kwargs,
            )

    def test_preview_and_approved_write_use_the_same_plan(self):
        preview = self.sync(ForgePullPublisher(self.api, "o", "r"), labels=(7,))
        self.assertFalse(preview.applied)
        self.assertTrue(preview.pull_request.planned)
        self.assertEqual(preview.attribution_status, "planned")
        self.assertTrue(preview.plan.body.startswith("Review mirror on behalf of @mirror-bot\n\n"))
        self.assertIn("From (unverified):\n\n```text\n", preview.plan.body)
        self.assertEqual(self.branch_writes, [False])
        self.assertEqual(self.transport.pulls, [])

        result = self.sync(
            ForgePullPublisher(self.api, "o", "r", write_enabled=True),
            labels=(7,), approved_digest=preview.approval_digest,
        )
        self.assertTrue(result.applied)
        self.assertEqual(result.attribution_status, "present")
        self.assertEqual(result.approval_digest, preview.approval_digest)
        self.assertEqual(self.branch_writes, [False, False, True])
        self.assertEqual(len(self.transport.pulls), 1)
        publication = self.ledger.publication("https://gitea.test/api/v1/repos/o/r", self.revision.series_key, 1)
        self.assertEqual((publication.status, publication.pull_index), ("complete", 1))

    def test_list_publications_is_read_only_filtered_and_paginated(self):
        preview = self.sync(ForgePullPublisher(self.api, "o", "r"))
        self.sync(ForgePullPublisher(self.api, "o", "r", write_enabled=True), approved_digest=preview.approval_digest)
        self.ledger.begin_publication("other", self.revision.series_key, 1, "other-digest", "b" * 40, "other-branch", "a" * 40)
        arguments = ["list-publications", "--ledger", str(self.ledger.path), "--series-key", self.revision.series_key]
        before = self.ledger.path.read_bytes()
        with redirect_stdout(StringIO()) as output:
            self.assertEqual(main([*arguments, "--limit", "1"]), 0)
        page = json.loads(output.getvalue())
        self.assertEqual(page["publications"][0]["publication"]["pull_index"], 1)
        self.assertIsNotNone(page["next_cursor"])
        with redirect_stdout(StringIO()) as output:
            self.assertEqual(main([*arguments, "--after", str(page["next_cursor"]), "--status", "prepared"]), 0)
        following = json.loads(output.getvalue())
        self.assertEqual(following["publications"][0]["publication"]["destination"], "other")
        self.assertIsNone(following["next_cursor"])
        self.assertEqual(self.ledger.path.read_bytes(), before)
        with self.assertRaisesRegex(ValueError, "limit"):
            self.ledger.list_publications(limit=1001)

    def test_changed_plan_does_not_write(self):
        preview = self.sync(ForgePullPublisher(self.api, "o", "r"), labels=(7,))
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.sync(
                ForgePullPublisher(self.api, "o", "r", write_enabled=True),
                labels=(8,), approved_digest=preview.approval_digest,
            )
        self.assertFalse(any(self.branch_writes))
        self.assertEqual(self.transport.pulls, [])

    def test_revision_policy_is_bound_to_the_approval_digest(self):
        preview = self.sync(ForgePullPublisher(self.api, "o", "r"))
        separate = self.sync(ForgePullPublisher(self.api, "o", "r"), revision_policy="separate")
        self.assertNotEqual(preview.approval_digest, separate.approval_digest)
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.sync(
                ForgePullPublisher(self.api, "o", "r", write_enabled=True),
                revision_policy="separate", approved_digest=preview.approval_digest,
            )
        self.assertFalse(any(self.branch_writes))

    def test_explicitly_linked_revision_requires_separate_pr_policy(self):
        first = parse_message(
            b"From: Author <author@example.test>\nMessage-ID: <v1@example.test>\n"
            b"Subject: [PATCH 1/1] change\n\nchange-id: stable-change\n"
        )
        second = parse_message(
            b"From: Author <author@example.test>\nMessage-ID: <v2@example.test>\n"
            b"Subject: [PATCH v2 1/1] change\n\nPatch without a change-ID.\n"
        )
        self.revision = discover_series((first,))[0]
        preview = self.sync(ForgePullPublisher(self.api, "o", "r"))
        writer = ForgePullPublisher(self.api, "o", "r", write_enabled=True)
        self.sync(writer, approved_digest=preview.approval_digest)
        original_head = self.transport.pulls[0]["head"].copy()
        self.ledger.record_parsed_message(second)
        arguments = [
            "link-revision", "--ledger", str(self.ledger.path), "--series-key", self.revision.series_key,
            "--canonical-revision", "1", "--candidate-seed", second.message_id, "--candidate-revision", "2",
            "--actor", "operator", "--reason", "reviewed patch history",
        ]
        with redirect_stderr(StringIO()):
            self.assertEqual(main(arguments), 1)
        arguments.append("--allow-unmatched-change-id")
        with redirect_stdout(StringIO()) as output:
            self.assertEqual(main(arguments), 0)
        preview_link = json.loads(output.getvalue())
        self.assertEqual(preview_link["plan"]["manual_claims"], {
            "canonical_change_id": "stable-change", "candidate_change_id": None,
        })
        digest = preview_link["approval_digest"]
        encoded = json.dumps(preview_link["plan"], sort_keys=True, separators=(",", ":")).encode("utf-8")
        self.assertEqual(digest, hashlib.sha256(encoded).hexdigest())
        with redirect_stdout(StringIO()):
            self.assertEqual(main([*arguments, "--write", "--approve", digest]), 0)
        self.revision = self.ledger.discover_related_series(second.message_id)[0]
        with self.assertRaises(PullRequestConflict):
            self.sync(ForgePullPublisher(self.api, "o", "r"))
        preview = self.sync(ForgePullPublisher(self.api, "o", "r"), revision_policy="separate")
        result = self.sync(writer, revision_policy="separate", approved_digest=preview.approval_digest)
        self.assertTrue(result.pull_request.created)
        self.assertEqual(len(self.transport.pulls), 2)
        self.assertEqual(self.transport.pulls[0]["head"], original_head)

    def test_api_account_id_is_bound_to_series_approval(self):
        preview = self.sync(ForgePullPublisher(self.api, "o", "r"))
        self.assertEqual(preview.plan.actor_id, 5)
        self.transport.actor_id = 6
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.sync(
                ForgePullPublisher(self.api, "o", "r", write_enabled=True),
                approved_digest=preview.approval_digest,
            )
        self.assertFalse(any(self.branch_writes))

    def test_completed_legacy_publication_replays_without_changing_its_intent(self):
        preview = self.sync(ForgePullPublisher(self.api, "o", "r"))
        writer = ForgePullPublisher(self.api, "o", "r", write_enabled=True)
        self.sync(writer, approved_digest=preview.approval_digest)
        legacy_digest = preview.plan.approval_digest(legacy=True)
        with self.ledger._connection() as connection:
            connection.execute("UPDATE publications SET approval_digest = ?", (legacy_digest,))
        replay = self.sync(writer, approved_digest=preview.approval_digest)
        self.assertFalse(replay.pull_request.created)
        self.assertEqual(len(self.transport.pulls), 1)
        recorded = self.ledger.publication("https://gitea.test/api/v1/repos/o/r", self.revision.series_key, 1)
        self.assertEqual(recorded.approval_digest, legacy_digest)

    def test_completed_pr_without_old_attribution_replays_without_rewriting(self):
        preview = self.sync(ForgePullPublisher(self.api, "o", "r"))
        writer = ForgePullPublisher(self.api, "o", "r", write_enabled=True)
        self.sync(writer, approved_digest=preview.approval_digest)
        legacy_body = (
            f"Source: https://lore.kernel.org/xen-devel/{preview.plan.source_message_id}/\n\n"
            f"Revision: v{preview.plan.revision}\nPatches: {self.revision.expected_patch_count}"
        )
        legacy_digest = replace(preview.plan, body=legacy_body).approval_digest()
        self.transport.pulls[0]["body"] = f"{legacy_body}\n\n{_series_marker(self.revision.series_key)}"
        with self.ledger._connection() as connection:
            connection.execute("UPDATE publications SET approval_digest = ?", (legacy_digest,))
        replay = self.sync(writer, approved_digest=preview.approval_digest)
        self.assertEqual(replay.attribution_status, "existing_pr_unmodified")
        self.assertEqual(len(self.transport.pulls), 1)
        self.assertTrue(self.transport.pulls[0]["body"].startswith(legacy_body))

    def test_completed_publication_rejects_a_changed_intent(self):
        original = self.sync(ForgePullPublisher(self.api, "o", "r"), labels=(7,))
        writer = ForgePullPublisher(self.api, "o", "r", write_enabled=True)
        self.sync(writer, labels=(7,), approved_digest=original.approval_digest)
        changed = self.sync(ForgePullPublisher(self.api, "o", "r"), labels=(8,))
        with self.assertRaisesRegex(PublicationConflict, "intent"):
            self.sync(writer, labels=(8,), approved_digest=changed.approval_digest)

    def test_observed_archive_deletion_blocks_previously_approved_publication(self):
        preview = self.sync(ForgePullPublisher(self.api, "o", "r"))
        self.ledger.record_archive_deletion("/archive/epoch", "d" * 40)
        with self.assertRaises(ArchiveSafetyBlocked):
            self.sync(
                ForgePullPublisher(self.api, "o", "r", write_enabled=True),
                approved_digest=preview.approval_digest,
            )
        self.assertFalse(any(self.branch_writes))
        self.assertEqual(self.transport.pulls, [])

    def test_publication_requires_referenced_parent_before_preflight(self):
        child = message(
            "child@example.test", "[PATCH 1/1] change", headers="In-Reply-To: <parent@example.test>\n",
        )
        self.revision = discover_series((child,))[0]
        with self.assertRaisesRegex(PublicationConflict, "ancestry is incomplete"):
            self.sync(ForgePullPublisher(self.api, "o", "r"))
        self.assertEqual(self.branch_writes, [])
        parent = message("parent@example.test", "older discussion")
        self.ledger.record_parsed_message(parent)
        preview = self.sync(ForgePullPublisher(self.api, "o", "r"))
        self.assertTrue(preview.pull_request.planned)

    def test_new_related_patch_blocks_stale_approved_publication(self):
        first = message("first@example.test", "[PATCH 1/1] original")
        self.revision = discover_series([first])[0]
        preview = self.sync(ForgePullPublisher(self.api, "o", "r"))
        duplicate = message(
            "duplicate@example.test", "[PATCH 1/1] conflicting sibling",
            headers="In-Reply-To: <first@example.test>\n",
        )
        self.ledger.record_parsed_message(duplicate)
        with self.assertRaisesRegex(ValueError, "membership changed"):
            self.sync(
                ForgePullPublisher(self.api, "o", "r", write_enabled=True),
                approved_digest=preview.approval_digest,
            )
        self.assertFalse(any(self.branch_writes))
        self.assertEqual(self.transport.pulls, [])

    def test_rejects_unapproved_or_mismatched_destination(self):
        with self.assertRaisesRegex(ValueError, "approval digest"):
            self.sync(ForgePullPublisher(self.api, "o", "r", write_enabled=True))
        self.remote = "https://other.test/o/r.git"
        with self.assertRaisesRegex(ValueError, "same HTTP"):
            self.sync(ForgePullPublisher(self.api, "o", "r"))

    def test_rejects_incomplete_revision_before_assembly(self):
        self.revision = discover_series([message("patch@example.test", "[PATCH 1/2] incomplete")])[0]
        with self.assertRaisesRegex(ValueError, "complete"):
            self.sync(ForgePullPublisher(self.api, "o", "r"))

    def test_quarantined_series_is_not_published_on_original_replay(self):
        patch_message = self.revision.messages[0]
        self.ledger.record_parsed_message(patch_message)
        with self.assertRaises(MessageConflictError):
            self.ledger.record_message(patch_message.message_id, b"conflicting mail")
        with self.assertRaisesRegex(MessageConflictError, "Quarantined"):
            self.sync(ForgePullPublisher(self.api, "o", "r"))
        self.assertEqual(self.branch_writes, [])

    def test_uncertain_pr_write_requires_explicit_retry(self):
        preview = self.sync(ForgePullPublisher(self.api, "o", "r"))
        writer = ForgePullPublisher(self.api, "o", "r", write_enabled=True)
        self.transport.fail_without_write = True
        with self.assertRaises(DeliveryUncertainError):
            self.sync(writer, approved_digest=preview.approval_digest)
        self.transport.fail_without_write = False
        with self.assertRaises(PublicationUncertain):
            self.sync(writer, approved_digest=preview.approval_digest)
        self.assertEqual(sum(request.get_method() == "POST" for request in self.transport.requests), 1)
        result = self.sync(writer, approved_digest=preview.approval_digest, retry_uncertain=True)
        self.assertTrue(result.pull_request.created)

    def test_reconciles_pr_after_remote_write_before_ledger_acknowledgment(self):
        preview = self.sync(ForgePullPublisher(self.api, "o", "r"))
        writer = ForgePullPublisher(self.api, "o", "r", write_enabled=True)
        transition = self.ledger.transition_publication

        def fail_acknowledgment(*args, **kwargs):
            if kwargs["status"] == "complete":
                raise sqlite3.OperationalError("simulated local acknowledgment failure")
            return transition(*args, **kwargs)

        with patch.object(self.ledger, "transition_publication", side_effect=fail_acknowledgment):
            with self.assertRaises(sqlite3.OperationalError):
                self.sync(writer, approved_digest=preview.approval_digest)
        self.assertEqual(len(self.transport.pulls), 1)
        replay = self.sync(writer, approved_digest=preview.approval_digest)
        self.assertFalse(replay.pull_request.created)
        self.assertEqual(len(self.transport.pulls), 1)

    def test_cli_requires_explicit_approval_and_reads_token_from_environment(self):
        arguments = [
            "sync-series", "--mbox", "series.mbox", "--repository", str(self.repository),
            "--base", "HEAD", "--remote", self.remote, "--api-url", self.api.base_url,
            "--owner", "o", "--repo", "r", "--target-branch", "main", "--label", "7",
            "--ledger", str(self.repository / "lore_review_companion.cli.sqlite3"),
        ]
        with redirect_stderr(StringIO()):
            self.assertEqual(main([*arguments, "--write"]), 1)
        with (
            patch.dict("os.environ", {"LORE_GITEA_TOKEN": "sample-token"}),
            patch("lore_review_companion.cli.parse_mbox", return_value=self.revision.messages),
            patch("lore_review_companion.cli.sync_series_revision") as sync,
            redirect_stdout(StringIO()),
        ):
            sync.return_value = SimpleNamespace()
            with patch("lore_review_companion.cli.asdict", return_value={}):
                self.assertEqual(main([*arguments, "--write", "--approve", "reviewed"]), 0)
            self.assertTrue(sync.call_args.args[4].write_enabled)
            self.assertEqual(sync.call_args.kwargs["approved_digest"], "reviewed")
            self.assertEqual(sync.call_args.kwargs["labels"], (7,))
            self.assertEqual(sync.call_args.kwargs["credentials"].token, "sample-token")

    def test_cli_can_preview_a_series_selected_from_the_archive_ledger(self):
        patch_message = self.revision.messages[0]
        self.ledger.record_parsed_message(patch_message)
        arguments = [
            "sync-series", "--message-id", patch_message.message_id, "--repository", str(self.repository),
            "--base", "HEAD", "--remote", self.remote, "--api-url", self.api.base_url,
            "--owner", "o", "--repo", "r", "--target-branch", "main", "--ledger", str(self.ledger.path),
        ]
        with (
            patch.dict("os.environ", {"LORE_GITEA_TOKEN": "sample-token"}),
            patch("lore_review_companion.cli.sync_series_revision") as sync,
            patch("lore_review_companion.cli.asdict", return_value={}), redirect_stdout(StringIO()),
        ):
            sync.return_value = SimpleNamespace()
            self.assertEqual(main(arguments), 0)
            self.assertEqual(sync.call_args.args[0], self.revision)
            self.assertIsNotNone(sync.call_args.kwargs["ledger"])

    def test_cli_can_discover_and_preview_a_series_from_one_stored_seed(self):
        patch_message = self.revision.messages[0]
        self.ledger.record_parsed_message(patch_message)
        with redirect_stdout(StringIO()) as output:
            self.assertEqual(main(["discover-series", "--ledger", str(self.ledger.path), "--message-id", patch_message.message_id]), 0)
        self.assertIn(patch_message.message_id, output.getvalue())
        arguments = [
            "sync-series", "--series-seed", patch_message.message_id, "--repository", str(self.repository),
            "--base", "HEAD", "--remote", self.remote, "--api-url", self.api.base_url,
            "--owner", "o", "--repo", "r", "--target-branch", "main", "--ledger", str(self.ledger.path),
        ]
        with (
            patch.dict("os.environ", {"LORE_GITEA_TOKEN": "sample-token"}),
            patch("lore_review_companion.cli.sync_series_revision") as sync,
            patch("lore_review_companion.cli.asdict", return_value={}), redirect_stdout(StringIO()),
        ):
            sync.return_value = SimpleNamespace()
            self.assertEqual(main(arguments), 0)
            self.assertEqual(sync.call_args.args[0], self.revision)


if __name__ == "__main__":
    unittest.main()