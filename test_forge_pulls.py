import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

from cli import main
from git_branches import RevisionBranchConflict, RevisionBranchResult, RevisionBranchUncertain, StagingSyncPlan, StagingSyncResult, revision_branch_name
from forge_api import ForgeAPI, ForgeAPIError
from forge_comments import DeliveryUncertainError
from forge_pulls import ForgePullPublisher, PullRequestConflict, inspect_review_staging, sync_review_staging
from ledger import ArchiveSafetyBlocked, Ledger, WorkerBusyError
from staging_supervisor import run_staging_sync


class FakeResponse:
    def __init__(self, status, value):
        self.status = status
        self.body = json.dumps(value).encode()
        self.headers = {}

    def read(self, size=-1):
        return self.body if size < 0 else self.body[:size]

    def close(self):
        pass


class PullTransport:
    def __init__(self):
        self.repository = {
            "id": 42, "mirror": False, "has_pull_requests": True, "permissions": {"push": True},
        }
        self.pulls = []
        self.requests = []
        self.head_commit = "a" * 40
        self.actor_id = 5
        self.fail_after_write = False
        self.fail_without_write = False

    def __call__(self, request, _timeout):
        self.requests.append(request)
        path = request.full_url.split("/api/v1/", 1)[-1].split("?", 1)[0]
        if request.get_method() == "GET" and path == "user":
            return FakeResponse(200, {"id": self.actor_id, "login": "mirror-bot"})
        if request.get_method() == "GET" and path == "repos/o/r":
            return FakeResponse(200, self.repository)
        if request.get_method() == "GET" and path == "repos/o/r/pulls":
            return FakeResponse(200, self.pulls)
        if request.get_method() == "POST" and path == "repos/o/r/pulls":
            if self.fail_without_write:
                raise URLError("connection lost")
            payload = json.loads(request.data)
            pull = {
                "number": len(self.pulls) + 1, "body": payload["body"],
                "head": {"ref": payload["head"], "repo_id": 42, "sha": self.head_commit},
                "base": {"ref": payload["base"], "repo_id": 42},
            }
            self.pulls.append(pull)
            if self.fail_after_write:
                self.fail_after_write = False
                raise URLError("connection lost after write")
            return FakeResponse(201, pull)
        raise AssertionError(f"Unexpected request: {request.get_method()} {path}")


class ForgePullPublisherTests(unittest.TestCase):
    def setUp(self):
        self.transport = PullTransport()
        self.api = ForgeAPI("https://gitea.test/api/v1/", transport=self.transport, max_retries=0)
        self.publisher = ForgePullPublisher(self.api, "o", "r", write_enabled=True)
        self.series_key = "lore:root@example.test"
        self.branch = RevisionBranchResult(revision_branch_name(self.series_key, 1), "a" * 40, False)
        self.arguments = {
            "series_key": self.series_key, "revision": 1, "branch": self.branch,
            "target_branch": "main", "title": "Patch series", "body": "Original mail", "labels": (7,),
        }

    def test_dry_run_never_posts(self):
        publisher = ForgePullPublisher(self.api, "o", "r")
        preview = publisher.ensure_pull_request(**self.arguments)
        self.assertTrue(preview.planned)
        self.assertEqual(preview.remote_object["labels"], [7])
        self.assertFalse(any(request.get_method() == "POST" for request in self.transport.requests))

    def test_access_inspection_reports_only_verified_account_permissions(self):
        with self.assertRaisesRegex(ValueError, "authenticated token"):
            self.publisher.inspect_access()
        self.assertEqual(self.transport.requests, [])
        self.api.token = "synthetic-token"
        access = self.publisher.inspect_access()
        self.assertEqual((access.actor.login, access.actor.user_id, access.repository_id), ("mirror-bot", 5, 42))
        self.assertTrue(access.account_push_reported)
        self.assertFalse(access.token_write_scopes_verified)
        self.assertFalse(access.branch_permissions_verified)
        self.assertEqual([request.get_method() for request in self.transport.requests], ["GET", "GET"])
        self.assertNotIn("synthetic-token", repr(access))
        self.transport.repository["permissions"]["push"] = False
        with self.assertRaises(PullRequestConflict):
            self.publisher.inspect_access()

    def test_access_cli_uses_environment_token_without_disclosing_it(self):
        self.api.token = "synthetic-token"
        with (
            patch.dict("os.environ", {"LORE_GITEA_TOKEN": "synthetic-token"}),
            patch("cli.ForgeAPI", return_value=self.api) as client,
            redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(main([
                "check-review-access", "--api-url", self.api.base_url, "--owner", "o", "--repo", "r",
            ]), 0)
        self.assertEqual(client.call_args.kwargs["token"], "synthetic-token")
        self.assertNotIn("synthetic-token", output.getvalue())
        result = json.loads(output.getvalue())
        self.assertEqual(result["actor"], {"login": "mirror-bot", "user_id": 5})
        self.assertFalse(result["token_write_scopes_verified"])
        self.assertFalse(result["branch_permissions_verified"])
        self.assertTrue(all(request.get_method() == "GET" for request in self.transport.requests))

    def test_staging_comparison_reports_matching_and_divergent_refs_without_writes(self):
        requests = []
        commits = {"source": "a" * 40, "review": "b" * 40}

        def transport(request, _timeout):
            requests.append(request)
            owner = request.full_url.split("/repos/", 1)[1].split("/", 1)[0]
            return FakeResponse(200, {"name": "staging", "commit": {"id": commits[owner]}})

        api = ForgeAPI("https://gitea.test/api/v1/", transport=transport)
        result = inspect_review_staging(api, "source", "xen", "review", "xen", "staging")
        self.assertEqual((result.source_commit, result.review_commit, result.same_tip), ("a" * 40, "b" * 40, False))
        commits["review"] = commits["source"]
        self.assertTrue(inspect_review_staging(api, "source", "xen", "review", "xen", "staging").same_tip)
        self.assertTrue(all(request.get_method() == "GET" for request in requests))
        commits["source"] = "invalid"
        with self.assertRaisesRegex(ForgeAPIError, "invalid branch"):
            inspect_review_staging(api, "source", "xen", "review", "xen", "staging")
        with self.assertRaises(ValueError):
            inspect_review_staging(api, "source", "xen", "source", "xen", "staging")

    def test_staging_comparison_cli_does_not_send_a_token_by_default(self):
        api = ForgeAPI(
            "https://gitea.test/api/v1/",
            transport=lambda request, _timeout: FakeResponse(200, {"name": "staging", "commit": {"id": "a" * 40}}),
        )
        arguments = [
            "inspect-review-staging", "--api-url", api.base_url, "--source-owner", "source", "--source-repo", "xen",
            "--review-owner", "review", "--review-repo", "xen",
        ]
        with patch.dict("os.environ", {"LORE_GITEA_TOKEN": "synthetic-token"}), patch("cli.ForgeAPI", return_value=api) as client:
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(arguments), 0)
            self.assertTrue(json.loads(output.getvalue())["same_tip"])
            self.assertIsNone(client.call_args.kwargs["token"])
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main([*arguments, "--token-env", "LORE_GITEA_TOKEN"]), 0)
            self.assertEqual(client.call_args.kwargs["token"], "synthetic-token")

    def test_staging_sync_checks_account_repositories_refs_and_safety_before_writing(self):
        requests = []
        remote = "https://gitea.test/review/xen.git"
        plan = StagingSyncPlan("/source", remote, "staging", "a" * 40, "b" * 40)

        def transport(request, _timeout):
            requests.append(request)
            path = request.full_url.split("/api/v1/", 1)[1]
            if path == "user":
                return FakeResponse(200, {"id": 5, "login": "mirror-bot"})
            if path == "repos/review/xen":
                return FakeResponse(200, {"id": 42, "mirror": False, "has_pull_requests": True, "permissions": {"push": True}})
            commit = "a" * 40 if path == "repos/source/xen/branches/staging" else "b" * 40
            return FakeResponse(200, {"name": "staging", "commit": {"id": commit}})

        api = ForgeAPI("https://gitea.test/api/v1/", token="synthetic-token", transport=transport)
        with tempfile.TemporaryDirectory() as directory:
            ledger = Ledger(Path(directory) / "ledger.sqlite3")
            with patch("forge_pulls.plan_staging_sync", return_value=plan), patch("forge_pulls.sync_staging") as sync:
                sync.return_value = StagingSyncResult(plan, updated=True)
                result = sync_review_staging(ledger, api, "source", "xen", "review", "xen", Path("/source"), remote)
                self.assertEqual((result.actor.login, result.repository_id, result.result.updated), ("mirror-bot", 42, True))
                self.assertEqual(sync.call_args.kwargs["credentials"].username, "mirror-bot")
                sync.reset_mock()
                with self.assertRaisesRegex(ValueError, "same HTTP"):
                    sync_review_staging(ledger, api, "source", "xen", "review", "xen", Path("/source"), "https://gitea.test/source/xen.git")
                ledger.record_archive_deletion("/archive", "d" * 40)
                with self.assertRaises(ArchiveSafetyBlocked):
                    sync_review_staging(ledger, api, "source", "xen", "review", "xen", Path("/source"), remote)
                pending = Path(f"{ledger.path}.staging-sync-pending")
                self.assertTrue(pending.exists())
                with self.assertRaisesRegex(RevisionBranchUncertain, "requires inspection"):
                    sync_review_staging(Ledger(ledger.path), api, "source", "xen", "review", "xen", Path("/source"), remote)
                sync.assert_not_called()
        self.assertTrue(all(request.get_method() == "GET" for request in requests))

    def test_staging_sync_cli_requires_write_and_reads_private_token_file(self):
        remote = "https://gitea.test/review/xen.git"
        plan = StagingSyncPlan("/source", remote, "staging", "a" * 40, "b" * 40)
        with tempfile.TemporaryDirectory() as directory:
            ledger = Ledger(Path(directory) / "ledger.sqlite3")
            token_file = Path(directory) / "token"
            token_file.write_text("synthetic-token\n")
            token_file.chmod(0o600)
            arguments = [
                "sync-review-staging", "--ledger", str(ledger.path), "--api-url", "https://gitea.test/api/v1/",
                "--source-owner", "source", "--source-repo", "xen", "--review-owner", "review", "--review-repo", "xen",
                "--source-repository", "/source", "--remote", remote, "--token-file", str(token_file),
            ]
            with patch("cli.sync_review_staging", return_value=StagingSyncResult(plan, updated=True)) as sync:
                with redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(main([*arguments, "--write"]), 0)
                self.assertTrue(json.loads(output.getvalue())["updated"])
                self.assertEqual(sync.call_args.args[1].token, "synthetic-token")
                self.assertNotIn("synthetic-token", output.getvalue())
                sync.reset_mock()
                with patch("sys.stderr", new_callable=io.StringIO) as error:
                    self.assertEqual(main(arguments), 1)
                self.assertIn("requires explicit --write", error.getvalue())
                sync.assert_not_called()

    def test_staging_supervisor_latches_failures_and_interruptions_before_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            pending = Path(directory) / "staging-service-pending"
            for outcome in (1, KeyboardInterrupt()):
                with self.subTest(outcome=outcome):
                    def fail(arguments):
                        self.assertEqual(arguments, ["sync-review-staging", "--write"])
                        self.assertEqual(pending.stat().st_mode & 0o777, 0o600)
                        if isinstance(outcome, BaseException):
                            raise outcome
                        return outcome

                    with patch("cli.main", side_effect=fail) as cli:
                        if isinstance(outcome, BaseException):
                            with self.assertRaises(KeyboardInterrupt):
                                run_staging_sync(pending, ["--write"])
                        else:
                            self.assertEqual(run_staging_sync(pending, ["--write"]), 1)
                        with self.assertRaises(FileExistsError):
                            run_staging_sync(pending, ["--write"])
                        self.assertEqual(cli.call_count, 1)
                    pending.unlink()

            with patch("cli.main", return_value=0):
                self.assertEqual(run_staging_sync(pending, ["--write"]), 0)
            self.assertFalse(pending.exists())

    def test_staging_supervisor_skips_a_busy_ledger_before_any_push(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Ledger(Path(directory) / "ledger.sqlite3")
            token_file = Path(directory) / "token"
            token_file.write_text("synthetic-token\n")
            token_file.chmod(0o600)
            pending = Path(directory) / "staging-service-pending"
            arguments = [
                "--ledger", str(ledger.path), "--api-url", "https://gitea.test/api/v1/",
                "--source-owner", "source", "--source-repo", "xen", "--review-owner", "review", "--review-repo", "xen",
                "--source-repository", "/source", "--remote", "https://gitea.test/review/xen.git",
                "--token-file", str(token_file), "--write",
            ]
            with ledger.worker_lock(), patch("forge_pulls.plan_staging_sync") as plan, patch("sys.stderr", new_callable=io.StringIO) as error:
                self.assertEqual(run_staging_sync(pending, arguments), 0)
            self.assertIn("worker", error.getvalue())
            self.assertNotIn("synthetic-token", error.getvalue())
            plan.assert_not_called()
            self.assertFalse(pending.exists())
            sync_pending = Path(f"{ledger.path}.staging-sync-pending")
            self.assertFalse(sync_pending.exists())
            with patch("forge_pulls.authenticated_user", side_effect=WorkerBusyError("later failure")), patch("sys.stderr", new_callable=io.StringIO):
                self.assertEqual(run_staging_sync(pending, arguments), 1)
            self.assertTrue(sync_pending.exists())
            self.assertTrue(pending.exists())
            sync_pending.unlink()
            pending.unlink()
            sync_pending.write_text("previous attempt")
            with ledger.worker_lock(), patch("forge_pulls.plan_staging_sync") as plan, patch("sys.stderr", new_callable=io.StringIO):
                self.assertEqual(run_staging_sync(pending, arguments), 1)
            plan.assert_not_called()
            self.assertTrue(sync_pending.exists())
            self.assertTrue(pending.exists())

    def test_creation_and_replay_use_series_provenance(self):
        created = self.publisher.ensure_pull_request(**self.arguments)
        replay = self.publisher.ensure_pull_request(**self.arguments)
        self.assertTrue(created.created)
        self.assertFalse(replay.created)
        self.assertEqual(created.remote_object["number"], replay.remote_object["number"])
        self.assertEqual(len(self.transport.pulls), 1)

    def test_reconciles_lost_acknowledgment_and_blocks_unconfirmed_write(self):
        self.transport.fail_after_write = True
        reconciled = self.publisher.ensure_pull_request(**self.arguments)
        self.assertFalse(reconciled.created)
        self.assertEqual(len(self.transport.pulls), 1)

        self.transport.pulls.clear()
        self.transport.fail_without_write = True
        with self.assertRaises(DeliveryUncertainError):
            self.publisher.ensure_pull_request(**self.arguments)

    def test_rejects_mirror_unmarked_pull_and_other_revision(self):
        self.transport.repository["mirror"] = True
        with self.assertRaisesRegex(PullRequestConflict, "non-mirror"):
            self.publisher.ensure_pull_request(**self.arguments)
        self.transport.repository["mirror"] = False
        self.publisher.ensure_pull_request(**self.arguments)

        other = dict(self.arguments, revision=2)
        other["branch"] = RevisionBranchResult(revision_branch_name(self.series_key, 2), "b" * 40, False)
        with self.assertRaisesRegex(PullRequestConflict, "different revision"):
            self.publisher.ensure_pull_request(**other)

        self.transport.pulls[0]["body"] = "human-created pull request"
        with self.assertRaisesRegex(PullRequestConflict, "unmarked"):
            self.publisher.ensure_pull_request(**self.arguments)

    def test_rejects_unpublished_branch_when_writes_enabled(self):
        self.arguments["branch"] = RevisionBranchResult(self.branch.branch, self.branch.head_commit, False, planned=True)
        with self.assertRaisesRegex(ValueError, "not been published"):
            self.publisher.ensure_pull_request(**self.arguments)

    def test_explicit_separate_revision_preserves_the_original_pull_request(self):
        original = self.publisher.ensure_pull_request(**self.arguments)
        other = dict(self.arguments, revision=2, revision_policy="separate")
        other["branch"] = RevisionBranchResult(revision_branch_name(self.series_key, 2), "b" * 40, False)
        self.transport.head_commit = "b" * 40
        preview = ForgePullPublisher(self.api, "o", "r").ensure_pull_request(**other)
        self.assertTrue(preview.planned)
        created = self.publisher.ensure_pull_request(**other)
        replay = self.publisher.ensure_pull_request(**other)
        self.assertEqual((original.remote_object["number"], created.remote_object["number"]), (1, 2))
        self.assertFalse(replay.created)
        self.assertFalse(self.publisher.ensure_pull_request(**self.arguments).created)
        self.assertEqual(self.transport.pulls[0]["head"]["ref"], self.branch.branch)
        self.assertEqual(len(self.transport.pulls), 2)

    def test_separate_revision_rejects_inconsistent_related_provenance(self):
        self.publisher.ensure_pull_request(**self.arguments)
        self.transport.pulls[0]["head"]["ref"] = "human-branch"
        other = dict(self.arguments, revision=2, revision_policy="separate")
        other["branch"] = RevisionBranchResult(revision_branch_name(self.series_key, 2), "b" * 40, False)
        with self.assertRaisesRegex(PullRequestConflict, "inconsistent"):
            self.publisher.ensure_pull_request(**other)


if __name__ == "__main__":
    unittest.main()