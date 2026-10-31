# SPDX-License-Identifier: GPL-2.0-or-later

import hashlib
import json
import tempfile
import unittest
import sqlite3
from contextlib import closing, contextmanager, redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import NotRequired, TypedDict
from unittest.mock import patch

from lore_review_companion.b4_adapter import AssemblyResult
from lore_review_companion.cli import main
from lore_review_companion.coordinator import plan_imported_sync
from lore_review_companion.import_worker import (
    _allowed_replies,
    _ancestry_order,
    _ensure_message_issue,
    _message_issue_exists,
    _deliver_reply_batch,
    _recover_submission_thread,
    run_import_worker,
    run_import_worker_round,
    wait_for_resources,
)
from lore_review_companion.import_refresh import (
    apply_import_refresh,
    plan_import_refresh,
    refresh_digest,
    save_refresh_plan,
)
from lore_review_companion.import_policy import read_import_policy
from lore_review_companion.git_branches import (
    GitHttpCredentials,
    RevisionBranchResult,
    RevisionRefsResult,
    revision_branch_name,
)
from lore_review_companion.forge_api import ForgeAPI
from lore_review_companion.forge_pulls import (
    ForgePullPublisher,
    PullRequestConflict,
    _series_marker,
)
from lore_review_companion.forge_comments import (
    DeliveryUncertainError,
    quote_untrusted_text,
    submission_text,
)
from lore_review_companion.forge_imports import ForgeImportClient
from lore_review_companion.ledger import (
    ArchiveSafetyBlocked,
    Ledger,
    MessageConflictError,
    PublicationConflict,
)
from lore_review_companion.mail_parser import parse_message
from lore_review_companion.preflight import (
    GitPreflightError,
    PatchCommit,
    PreflightResult,
)
from lore_review_companion.series import discover_series
from lore_review_companion.series_sync import (
    PublicationUncertain,
    sync_imported_series_revision,
    sync_series_revision,
)
from test_forge_comments import FakeResponse
from test_forge_pulls import PullTransport
from test_series import message


class ImportedSyncOptions(TypedDict):
    series_seed: str
    target_branch: str
    credentials: GitHttpCredentials
    base_ref: NotRequired[str]


class SeriesSyncTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.repository = Path(self.tempdir.name)
        self.transport = PullTransport()
        self.api = ForgeAPI(
            "https://gitea.test/api/v1/", transport=self.transport, max_retries=0
        )
        self.revision = discover_series(
            [message("patch@example.test", "[PATCH 1/1] xen: change")]
        )[0]
        self.remote = "https://gitea.test/o/r.git"
        self.branch_writes = []
        self.branch_exists = False
        self.imported_branch_exists = set()
        self.ledger = Ledger(self.repository / "ledger.sqlite3")

    @contextmanager
    def prepare(self, _repository, _base_ref, _mailbox, **_options):
        yield SimpleNamespace(
            result=PreflightResult("b" * 40, "a" * 40, 1),
            patch_commits=lambda identifiers: (
                PatchCommit(identifiers[0], "b" * 40, "a" * 40),
            ),
        )

    def test_worker_ignores_excluded_reply_descendants(self):
        excluded = message("filtered@example.test", "Re: patch", headers="To: list@vger.kernel.org\n")
        child = message("child@example.test", "Re: patch", headers="In-Reply-To: <filtered@example.test>\n")
        grandchild = message("grandchild@example.test", "Re: patch", headers="In-Reply-To: <child@example.test>\n")
        allowed = message("allowed@example.test", "Re: patch", headers="In-Reply-To: <patch@example.test>\n")
        self.assertEqual(_allowed_replies((grandchild, allowed, child, excluded)), (allowed,))

    def test_resource_guard_waits_for_load_and_forge_disk_space(self):
        with (
            patch("lore_review_companion.import_worker.os.getloadavg", side_effect=[(5, 0, 0), (4, 0, 0), (4, 0, 0)]),
            patch("lore_review_companion.import_worker.os.sysconf", return_value=4),
            patch("lore_review_companion.import_worker.shutil.disk_usage", side_effect=[SimpleNamespace(free=2_000_000_000), SimpleNamespace(free=999_999_999), SimpleNamespace(free=1_000_000_000)]) as disk,
            patch("lore_review_companion.import_worker.time.sleep") as sleeper,
        ):
            wait_for_resources(self.repository)
        self.assertEqual(sleeper.call_args_list, [unittest.mock.call(10), unittest.mock.call(10)])
        self.assertEqual(disk.call_args_list, [unittest.mock.call(self.repository)] * 3)

    def test_continuous_worker_keeps_processing_pages(self):
        result = SimpleNamespace(considered=32, published=32, delivered=0, diagnostics=())
        idle = SimpleNamespace(considered=0, published=0, delivered=0, diagnostics=())
        with (
            patch("lore_review_companion.import_worker.run_import_worker_round", side_effect=[result, result, idle, KeyboardInterrupt]) as run,
            patch("lore_review_companion.import_worker.time.sleep") as sleeper,
            redirect_stdout(StringIO()),
            self.assertRaises(KeyboardInterrupt),
        ):
            run_import_worker(self.ledger, self.repository, self.repository / "policy",
                              self.repository / "state", self.api, object(), forge_path=self.repository)
        self.assertEqual(run.call_count, 4)
        sleeper.assert_called_once_with(1)
        self.assertIsNotNone(self.api.before_write)

    def test_forge_api_checks_resources_before_each_write(self):
        guard_calls = []
        api = ForgeAPI("https://gitea.test/api/v1/", transport=lambda _request, _timeout: FakeResponse(200, {}),
                       before_write=lambda: guard_calls.append("checked"))
        api.request_json("repos/o/r/issues")
        api.request_json("repos/o/r/issues", method="POST", payload={"title": "one"})
        api.request_json("repos/o/r/issues", method="POST", payload={"title": "two"})
        self.assertEqual(guard_calls, ["checked", "checked"])

    def test_import_issues_use_exact_message_id_and_include_closed_issues(self):
        calls = []
        existing = {"title": "patch@example.test", "state": "closed"}

        def request(path, **options):
            calls.append((path, options))
            if options.get("method") == "POST":
                return {"number": 1}
            return [existing, {"title": "other@example.test"}]

        client = SimpleNamespace(api=SimpleNamespace(request_json=request), repository_path="repos/o/r",
                                 source_url="https://lore.kernel.org/xen-devel/", capabilities=lambda: {})
        self.assertTrue(_message_issue_exists(client, "patch@example.test"))
        self.assertFalse(_ensure_message_issue(client, "patch@example.test", ("broken parent",)))
        self.assertTrue(_ensure_message_issue(client, "new@example.test", ("missing parent other@example.test",)))
        posts = [options for _, options in calls if options.get("method") == "POST"]
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["payload"]["title"], "new@example.test")
        self.assertIn("https://lore.kernel.org/xen-devel/new@example.test/", posts[0]["payload"]["body"])
        self.assertIn("missing parent other@example.test", posts[0]["payload"]["body"])
        self.assertTrue(all(options.get("params", {}).get("state") == "all" for _, options in calls if "params" in options))

    def test_worker_rechecks_exclusion_after_thread_recovery(self):
        policy = {
            "version": 1, "enabled": True, "expires_at": "2099-01-01T00:00:00+00:00",
            "api_url": self.api.base_url, "owner": "o", "repository": "r",
            "repository_id": 1, "importer_id": 5, "importer_login": "mirror-bot",
            "source_url": "https://lore.kernel.org/xen-devel/", "nntp_host": "nntp.lore.kernel.org",
            "nntp_group": "org.xenproject.lists.xen-devel", "git_remote": self.remote,
            "git_username": "git-bot", "base_commit": "b" * 40, "target_branch": "main",
            "max_inline_lines": 50,
            "lookback_hours": 148, "notifications": "disabled",
            "identity_rule": "isolated-unverified", "retry_uncertain": False,
        }
        policy_path = self.repository / "worker-policy.json"
        policy_path.write_text(json.dumps(policy))
        policy_path.chmod(0o600)
        capabilities = {
            "version": 1, "repository_id": 1, "importer_id": 5,
            "importer_login": "mirror-bot", "source_url": policy["source_url"],
            "operations": ["pull", "conversation", "review", "review-reply"],
            "max_inline_lines": 50, "unattended_allowed": True,
        }
        self.api.transport = lambda request, _timeout: FakeResponse(
            200, [] if "/issues?" in request.full_url else capabilities
        )
        source = message("patch@example.test", "[PATCH 1/1] xen: change",
                         headers="In-Reply-To: <cover@example.test>\n")
        cover = message("cover@example.test", "[PATCH 0/1] xen: change",
                        headers="To: list@vger.kernel.org\n")
        self.ledger.record_parsed_message(source)

        def recover(*_arguments):
            self.ledger.record_parsed_message(cover)
            return True

        with (
            patch("lore_review_companion.import_worker.discover_nntp"),
            patch("lore_review_companion.import_worker.discover_nntp_numbers"),
            patch("lore_review_companion.import_worker.fetch_nntp"),
            patch.object(self.ledger, "repair_nntp_parents"),
            patch("lore_review_companion.import_worker._recover_submission_thread", side_effect=recover),
            patch("lore_review_companion.import_worker.sync_imported_series_revision") as publish,
        ):
            result = run_import_worker_round(
                self.ledger, self.repository, policy_path, self.repository / "worker.sqlite3",
                self.api, GitHttpCredentials("git-bot", "synthetic-git-token"),
            )
        publish.assert_not_called()
        self.assertEqual(result.published, 0)
        self.assertEqual(result.diagnostics, ())

    def test_worker_reports_missing_patch_signoff_without_creating_pr(self):
        policy = {
            "version": 1, "enabled": True, "expires_at": "2099-01-01T00:00:00+00:00",
            "api_url": self.api.base_url, "owner": "o", "repository": "r",
            "repository_id": 1, "importer_id": 5, "importer_login": "mirror-bot",
            "source_url": "https://lore.kernel.org/xen-devel/", "nntp_host": "nntp.lore.kernel.org",
            "nntp_group": "org.xenproject.lists.xen-devel", "git_remote": self.remote,
            "git_username": "git-bot", "base_commit": "b" * 40, "target_branch": "main",
            "max_inline_lines": 50, "lookback_hours": 148, "notifications": "disabled",
            "identity_rule": "isolated-unverified", "retry_uncertain": False,
        }
        policy_path = self.repository / "worker-policy.json"
        policy_path.write_text(json.dumps(policy))
        capabilities = {
            "version": 1, "repository_id": 1, "importer_id": 5, "importer_login": "mirror-bot",
            "source_url": policy["source_url"], "operations": ["pull", "conversation", "review", "review-reply"],
            "max_inline_lines": 50, "unattended_allowed": True,
        }
        issues = []
        def transport(request, _timeout):
            if "/issues" in request.full_url:
                if request.get_method() == "POST":
                    issues.append(json.loads(request.data))
                    return FakeResponse(201, issues[-1])
                return FakeResponse(200, issues)
            return FakeResponse(200, capabilities)
        self.api.transport = transport
        unsigned = parse_message(b"From: A <a@example.test>\nMessage-ID: <unsigned@example.test>\nSubject: [PATCH] example\n\nNo signoff.\n---\n")
        self.ledger.record_parsed_message(unsigned)
        with patch("lore_review_companion.import_worker.sync_imported_series_revision") as publish:
            result = run_import_worker_round(
                self.ledger, self.repository, policy_path, self.repository / "worker.sqlite3",
                self.api, GitHttpCredentials("git-bot", "synthetic-git-token"), refresh_source=False,
            )
        publish.assert_not_called()
        self.assertEqual(result.published, 0)
        self.assertEqual([issue["title"] for issue in issues], ["unsigned@example.test"])
        self.assertIn("Signed-off-by", issues[0]["body"])
        self.assertIn("https://lore.kernel.org/xen-devel/unsigned@example.test/", issues[0]["body"])

    def test_direct_publication_rejects_missing_patch_signoff(self):
        unsigned = parse_message(b"From: A <a@example.test>\nMessage-ID: <unsigned@example.test>\nSubject: [PATCH] example\n\nNo signoff.\n---\n")
        (self.revision,) = discover_series((unsigned,))
        self.api.transport = lambda request, timeout: FakeResponse(
            200, {"version": 1, "repository_id": 1, "importer_id": 5, "importer_login": "mirror-bot",
                  "source_url": "https://lore.kernel.org/xen-devel/", "operations": ["pull"]},
        )
        with self.assertRaisesRegex(ValueError, "Missing Signed-off-by.*unsigned@example.test"):
            self.imported_sync()
        self.assertEqual(self.branch_writes, [])

    def imported_sync(self, *, write_enabled=False, **kwargs):
        for source in self.revision.messages:
            self.ledger.record_parsed_message(source)
        client = ForgeImportClient(
            self.api,
            "o",
            "r",
            source_url="https://lore.kernel.org/xen-devel/",
            write_enabled=write_enabled,
        )

        def refs(prepared, remote, **arguments):
            name = revision_branch_name(arguments["series_key"], arguments["revision"])
            writing = arguments.get("write_enabled", False)
            self.branch_writes.append(writing)
            created = writing and name not in self.imported_branch_exists
            if writing:
                self.imported_branch_exists.add(name)
            head = RevisionBranchResult(
                name,
                prepared.result.head_commit,
                created,
                name not in self.imported_branch_exists,
            )
            return RevisionRefsResult(
                RevisionBranchResult(
                    head.branch + "-base",
                    prepared.result.base_commit,
                    head.created,
                    head.planned,
                ),
                head,
            )

        with (
            patch(
                "lore_review_companion.series_sync.assemble_local_series",
                return_value=AssemblyResult(b"patch", "", ""),
            ),
            patch(
                "lore_review_companion.series_sync.prepare_patch_mailbox",
                new=self.prepare,
            ),
            patch("lore_review_companion.series_sync.ensure_revision_refs", new=refs),
        ):
            return sync_imported_series_revision(
                self.revision,
                self.repository,
                "HEAD",
                self.remote,
                client,
                target_branch=kwargs.pop("target_branch", "main"),
                ledger=self.ledger,
                credentials=GitHttpCredentials("mirror-bot", "synthetic-git-token"),
                **kwargs,
            )

    def test_refresh_plan_preserves_human_discussion_and_requires_exact_approval(self):
        source = self.revision.messages[0]
        self.ledger.record_parsed_message(source)
        self.ledger.record_series_revision(self.revision)
        destination = self.api.base_url + "repos/o/r"
        self.ledger.begin_publication(
            destination,
            self.revision.series_key,
            1,
            "digest",
            "b" * 40,
            "head",
            "a" * 40,
        )
        self.ledger.transition_publication(
            destination,
            self.revision.series_key,
            1,
            expected=("prepared",),
            status="complete",
            pull_index=7,
        )
        snapshot = {
            "sha256": "c" * 64,
            "imports": [
                {
                    "Type": "pull",
                    "MessageID": source.message_id,
                    "MessageSHA256": source.sha256,
                    "OperationKey": "root",
                }
            ],
            "comments": [{"ID": 99, "Content": "Human discussion"}],
        }
        capabilities = {
            "version": 1,
            "repository_id": 1,
            "importer_id": 5,
            "importer_login": "mirror-bot",
            "source_url": "https://lore.kernel.org/xen-devel/",
            "operations": ["pull-refresh"],
        }
        writes = []

        def transport(request, _timeout):
            if request.full_url.endswith("/capabilities"):
                return FakeResponse(200, capabilities)
            if request.get_method() == "POST":
                writes.append(json.loads(request.data))
                return FakeResponse(
                    200, {"id": 1, "pull_index": 7, "operation_key": "root"}
                )
            return FakeResponse(200, snapshot)

        self.api.transport = transport
        client = ForgeImportClient(
            self.api, "o", "r", source_url=capabilities["source_url"]
        )
        plan = plan_import_refresh(
            self.ledger,
            client,
            7,
            self.revision,
            {"operation_key": "root", "message_id": source.message_id},
            reason="Rebuild retained presentation",
        )
        self.assertEqual(plan["preserved_comment_ids"], [99])
        self.assertEqual(plan["removed_comment_ids"], [])
        self.assertEqual(writes, [])
        path = self.repository / "refresh.json"
        save_refresh_plan(path, plan)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(FileExistsError):
            save_refresh_plan(path, plan)
        with self.assertRaisesRegex(ValueError, "write opt-in"):
            apply_import_refresh(
                self.ledger, client, plan, approved_digest=refresh_digest(plan)
            )
        client.write_enabled = True
        with self.assertRaisesRegex(ValueError, "exact retained"):
            apply_import_refresh(self.ledger, client, plan, approved_digest="f" * 64)
        self.assertEqual(writes, [])
        self.assertEqual(
            apply_import_refresh(
                self.ledger,
                client,
                json.loads(path.read_text()),
                approved_digest=refresh_digest(plan),
            ),
            {"id": 1, "pull_index": 7, "operation_key": "root"},
        )
        self.assertEqual(writes, [plan["request"]])
        token = self.repository / "import-token"
        token.write_text("synthetic-import-token")
        token.chmod(0o600)
        publication_path = self.repository / "publication.json"
        publication_path.write_text(
            json.dumps(
                {
                    "plan": {
                        "series_key": self.revision.series_key,
                        "revision": 1,
                        "request": {
                            "operation_key": "root",
                            "message_id": source.message_id,
                        },
                    }
                }
            )
        )
        cli_plan = self.repository / "cli-refresh.json"
        arguments = [
            "refresh-import",
            "--ledger",
            str(self.ledger.path),
            "--api-url",
            self.api.base_url,
            "--owner",
            "o",
            "--repo",
            "r",
            "--pull-index",
            "7",
            "--token-file",
            str(token),
            "--publication-plan",
            str(publication_path),
            "--plan-file",
            str(cli_plan),
            "--reason",
            "Rebuild via operator CLI",
        ]
        with patch("lore_review_companion.cli.ForgeAPI", return_value=self.api):
            with redirect_stdout(StringIO()) as output:
                self.assertEqual(main(arguments), 0)
            approval = json.loads(output.getvalue())["approval_digest"]
            self.assertEqual(len(writes), 1)
            with redirect_stdout(StringIO()):
                self.assertEqual(
                    main([*arguments, "--write", "--approve", approval]), 0
                )
            self.assertEqual(len(writes), 2)
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                self.assertEqual(
                    main([*arguments, "--write", "--approve", "f" * 64]), 1
                )
            self.assertEqual(len(writes), 2)
        snapshot["imports"].append(
            {
                "Type": "conversation",
                "MessageID": source.message_id,
                "MessageSHA256": source.sha256,
                "OperationKey": "unretained",
            }
        )
        with self.assertRaisesRegex(PublicationConflict, "complete retained effect"):
            plan_import_refresh(
                self.ledger,
                client,
                7,
                self.revision,
                {"operation_key": "root", "message_id": source.message_id},
                reason="Rebuild",
            )

    def test_thread_recovery_fetches_all_missing_members(self):
        received = {"seed@example.test"}
        ledger = SimpleNamespace(
            nntp_message_received=lambda source, identifier: identifier in received
        )
        policy = SimpleNamespace(
            nntp_host="nntp.lore.kernel.org",
            nntp_group="group",
            source_url="https://lore.kernel.org/xen-devel/",
        )
        discovery = SimpleNamespace(
            empty_or_unavailable=False,
            message_ids=(
                "seed@example.test",
                "reply-one@example.test",
                "reply-two@example.test",
            ),
        )

        def fetch(_ledger, **options):
            received.add(options["message_id"])

        with (
            patch(
                "lore_review_companion.import_worker.discover_nntp_thread",
                return_value=discovery,
            ),
            patch(
                "lore_review_companion.import_worker.fetch_nntp", side_effect=fetch
            ) as fetched,
        ):
            self.assertTrue(
                _recover_submission_thread(ledger, policy, "seed@example.test")
            )
        self.assertEqual(
            [call.kwargs["message_id"] for call in fetched.call_args_list],
            ["reply-one@example.test", "reply-two@example.test"],
        )

    def test_autonomous_reply_delivery_orders_parents_and_drains_every_effect(self):
        parent = parse_message(
            b"From: Reviewer <r@example.test>\nMessage-ID: <parent@example.test>\nSubject: Re: patch\nIn-Reply-To: <patch@example.test>\n\nParent\n"
        )
        child = parse_message(
            b"From: Reviewer <r@example.test>\nMessage-ID: <child@example.test>\nSubject: Re: patch\nIn-Reply-To: <parent@example.test>\n\nChild\n"
        )
        ordered = _ancestry_order((child, parent))
        self.assertEqual(ordered, (parent, child))
        client = ForgeImportClient(
            self.api, "o", "r", source_url="https://lore.kernel.org/xen-devel/"
        )
        destination = self.api.base_url + "repos/o/r#7"
        for source in ordered:
            self.ledger.record_parsed_message(source)
        accepted = []

        def queue(
            _ledger, _reader, _index, _key, _revision, _repository, selected, **options
        ):
            source = selected[0]
            if source == child:
                self.assertIn(parent.message_id, accepted)
            for position in range(3):
                self.ledger.enqueue(
                    source.message_id,
                    "IMPORT_REVIEW",
                    destination,
                    f"effect-{position}",
                    {"source": source.message_id},
                )
            return SimpleNamespace(diagnostics=())

        def plan(*arguments, **options):
            return SimpleNamespace(
                deliveries=options["delivery_ids"], approval_digest=lambda: "batch"
            )

        def deliver(*arguments, **options):
            for identifier in options["delivery_ids"]:
                effect = next(
                    effect
                    for source in ordered
                    for effect in self.ledger.comment_effects(
                        source.message_id, destination
                    )
                    if effect.id == identifier
                )
                accepted.append(effect.message_id)
                self.ledger.mark_delivered(identifier)
            return SimpleNamespace(delivered=len(options["delivery_ids"]), failed=0)

        with closing(sqlite3.connect(":memory:")) as state:
            state.execute(
                "CREATE TABLE effects (delivery_id INTEGER PRIMARY KEY, fingerprint TEXT)"
            )
            with (
                patch(
                    "lore_review_companion.import_worker.queue_imported_replies",
                    side_effect=queue,
                ),
                patch(
                    "lore_review_companion.import_worker.plan_import_delivery",
                    side_effect=plan,
                ),
                patch(
                    "lore_review_companion.import_worker.deliver_approved_imports",
                    side_effect=deliver,
                ),
            ):
                delivered, diagnostics = _deliver_reply_batch(
                    self.ledger,
                    state,
                    client,
                    client,
                    SimpleNamespace(
                        message_limit=1, retry_uncertain=False, max_inline_lines=50
                    ),
                    self.repository,
                    self.revision,
                    7,
                    ordered,
                )
        self.assertEqual(delivered, 6)
        self.assertEqual(diagnostics, [])
        self.assertEqual(self.ledger.pending(), [])

    def test_read_only_snapshot_is_private_independent_and_refuses_existing_files(self):
        self.ledger.record_parsed_message(self.revision.messages[0])
        before = self.ledger.path.read_bytes()
        snapshot_path = self.repository / "snapshot.sqlite3"
        snapshot = Ledger(self.ledger.path, read_only=True).snapshot(snapshot_path)
        self.assertEqual(snapshot.status().messages, self.ledger.status().messages)
        self.assertEqual(snapshot_path.stat().st_mode & 0o777, 0o600)
        snapshot.record_message(
            "copy-only@example.test",
            b"Message-ID: <copy-only@example.test>\n\nCopy only",
        )
        self.assertEqual(self.ledger.path.read_bytes(), before)
        self.assertEqual(snapshot.status().messages, self.ledger.status().messages + 1)
        copied = snapshot_path.read_bytes()
        with self.assertRaises(FileExistsError):
            self.ledger.snapshot(snapshot_path)
        self.assertEqual(snapshot_path.read_bytes(), copied)

    def test_coordinator_reports_unknown_base_incomplete_series_and_refuses_writers(
        self,
    ):
        for source in self.revision.messages:
            self.ledger.record_parsed_message(source)
        client = ForgeImportClient(
            self.api, "o", "r", source_url="https://lore.kernel.org/xen-devel/"
        )
        assert self.revision.messages[0].message_id is not None
        arguments: ImportedSyncOptions = {
            "series_seed": self.revision.messages[0].message_id,
            "target_branch": "main",
            "credentials": GitHttpCredentials("mirror-bot", "synthetic-git-token"),
        }
        before = self.ledger.path.read_bytes()
        result = plan_imported_sync(
            self.ledger, client, self.repository, self.remote, **arguments
        )
        self.assertEqual(result.revisions[0].status, "unknown_base")
        self.assertEqual(self.ledger.path.read_bytes(), before)
        self.assertFalse(result.remote_writes or result.ledger_writes)
        self.assertEqual(self.transport.requests, [])
        incomplete = message("partial@example.test", "[PATCH 1/2] xen: partial")
        assert incomplete.message_id is not None
        self.ledger.record_parsed_message(incomplete)
        arguments["series_seed"] = incomplete.message_id
        result = plan_imported_sync(
            self.ledger, client, self.repository, self.remote, **arguments
        )
        self.assertEqual(result.revisions[0].status, "incomplete")
        client.write_enabled = True
        with self.assertRaisesRegex(ValueError, "read-only"):
            plan_imported_sync(
                self.ledger, client, self.repository, self.remote, **arguments
            )

    def test_coordinator_composes_publication_plan_in_private_snapshot(self):
        for source in self.revision.messages:
            self.ledger.record_parsed_message(source)
        client = ForgeImportClient(
            self.api, "o", "r", source_url="https://lore.kernel.org/xen-devel/"
        )
        before = self.ledger.path.read_bytes()

        def preview(current, _repository, _base, _remote, importer, **options):
            self.assertFalse(importer.write_enabled)
            self.assertNotEqual(options["ledger"].path, self.ledger.path)
            self.assertIsNotNone(
                options["ledger"].series_revision(current.series_key, current.revision)
            )
            return "verified publication preview"

        with patch(
            "lore_review_companion.coordinator.sync_imported_series_revision",
            side_effect=preview,
        ):
            assert self.revision.messages[0].message_id is not None
            result = plan_imported_sync(
                self.ledger,
                client,
                self.repository,
                self.remote,
                series_seed=self.revision.messages[0].message_id,
                target_branch="main",
                base_ref="HEAD",
                credentials=GitHttpCredentials("mirror-bot", "synthetic-git-token"),
            )
        self.assertEqual(result.revisions[0].status, "publication_ready")
        self.assertEqual(
            result.revisions[0].publication, "verified publication preview"
        )
        self.assertEqual(self.ledger.path.read_bytes(), before)
        self.assertEqual(self.transport.requests, [])

    def test_coordinator_refreshes_only_the_snapshot_with_bounded_source_calls(self):
        client = ForgeImportClient(
            self.api, "o", "r", source_url="https://lore.kernel.org/xen-devel/"
        )
        before = self.ledger.path.read_bytes()

        def discover(working, **options):
            self.assertEqual(options["hours"], 24)
            from lore_review_companion.nntp_source import nntp_source

            working.begin_nntp_bootstrap(
                nntp_source(options["host"], options["group"]),
                datetime(2026, 10, 30, tzinfo=timezone.utc),
                hours=24,
            )
            return "discovered"

        def fetch(working, **options):
            self.assertEqual(options["limit"], 2)
            working.record_parsed_message(self.revision.messages[0])
            return "fetched"

        with (
            patch(
                "lore_review_companion.coordinator.discover_nntp", side_effect=discover
            ),
            patch(
                "lore_review_companion.coordinator.discover_nntp_numbers",
                return_value="number scan",
            ) as numbers,
            patch("lore_review_companion.coordinator.fetch_nntp", side_effect=fetch),
        ):
            assert self.revision.messages[0].message_id is not None
            result = plan_imported_sync(
                self.ledger,
                client,
                self.repository,
                self.remote,
                series_seed=self.revision.messages[0].message_id,
                target_branch="main",
                credentials=GitHttpCredentials("mirror-bot", "synthetic-git-token"),
                refresh_source=True,
                lookback_hours=24,
                message_limit=2,
            )
        self.assertEqual(result.revisions[0].status, "unknown_base")
        self.assertIsNotNone(result.queue)
        self.assertEqual(numbers.call_args.kwargs["limit"], 2)
        self.assertEqual(self.ledger.path.read_bytes(), before)

    def test_coordinator_previews_all_delivery_kinds_and_stops_pending_publications(
        self,
    ):
        for source in self.revision.messages:
            self.ledger.record_parsed_message(source)
        self.ledger.record_series_revision(self.revision)
        destination = self.api.base_url + "repos/o/r"
        key = destination, self.revision.series_key, self.revision.revision
        self.ledger.begin_publication(
            *key, "digest", "b" * 40, "lore/revision", "a" * 40
        )
        client = ForgeImportClient(
            self.api, "o", "r", source_url="https://lore.kernel.org/xen-devel/"
        )
        assert self.revision.messages[0].message_id is not None
        options: ImportedSyncOptions = {
            "series_seed": self.revision.messages[0].message_id,
            "target_branch": "main",
            "credentials": GitHttpCredentials("mirror-bot", "synthetic-git-token"),
        }
        pending = plan_imported_sync(
            self.ledger, client, self.repository, self.remote, **options
        )
        self.assertEqual(pending.revisions[0].status, "publication_pending")
        self.ledger.transition_publication(
            *key, expected=("prepared",), status="complete", pull_index=7
        )
        before = self.ledger.path.read_bytes()
        refs = SimpleNamespace(
            head=SimpleNamespace(planned=False), base=SimpleNamespace(planned=False)
        )
        with (
            patch(
                "lore_review_companion.coordinator.sync_imported_series_revision",
                return_value=SimpleNamespace(refs=refs),
            ),
            patch(
                "lore_review_companion.coordinator.plan_import_delivery",
                return_value=SimpleNamespace(deliveries=()),
            ) as delivery,
        ):
            result = plan_imported_sync(
                self.ledger, client, self.repository, self.remote, **options
            )
        self.assertEqual(result.revisions[0].status, "up_to_date")
        self.assertEqual(
            [call.args[3] for call in delivery.call_args_list],
            ["IMPORT_CONVERSATION", "IMPORT_REVIEW", "IMPORT_REPLY"],
        )
        self.assertEqual(self.ledger.path.read_bytes(), before)

    def test_coordinator_reports_missing_ambiguous_and_failed_preflight_states(self):
        client = ForgeImportClient(
            self.api, "o", "r", source_url="https://lore.kernel.org/xen-devel/"
        )
        assert self.revision.messages[0].message_id
        options: ImportedSyncOptions = {
            "series_seed": self.revision.messages[0].message_id,
            "target_branch": "main",
            "base_ref": "HEAD",
            "credentials": GitHttpCredentials("mirror-bot", "synthetic-git-token"),
        }
        missing = plan_imported_sync(
            self.ledger, client, self.repository, self.remote, **options
        )
        self.assertEqual(missing.revisions, ())
        self.assertIn("selection blocked", missing.diagnostics[0])
        self.ledger.record_parsed_message(self.revision.messages[0])
        with patch(
            "lore_review_companion.coordinator.sync_imported_series_revision",
            side_effect=GitPreflightError("Patch does not apply to the selected base"),
        ):
            blocked = plan_imported_sync(
                self.ledger, client, self.repository, self.remote, **options
            )
        self.assertEqual(blocked.revisions[0].status, "blocked")
        self.assertIn("Patch does not apply", blocked.revisions[0].diagnostics[0])
        with patch.object(
            Ledger,
            "discover_related_series",
            return_value=(self.revision, replace(self.revision, series_key="other")),
        ):
            ambiguous = plan_imported_sync(
                self.ledger, client, self.repository, self.remote, **options
            )
        self.assertEqual(
            [item.status for item in ambiguous.revisions], ["ambiguous", "ambiguous"]
        )
        with (
            self.assertRaisesRegex(ValueError, "exceed the sync limit"),
            patch.object(
                Ledger,
                "discover_related_series",
                return_value=(self.revision, replace(self.revision, revision=2)),
            ),
        ):
            plan_imported_sync(
                self.ledger,
                client,
                self.repository,
                self.remote,
                revision_limit=1,
                **options,
            )

    def test_policy_worker_refuses_changed_authorization_before_any_publication(self):
        policy = {
            "version": 1,
            "enabled": True,
            "expires_at": "2099-01-01T00:00:00+00:00",
            "api_url": self.api.base_url,
            "owner": "o",
            "repository": "r",
            "repository_id": 1,
            "importer_id": 5,
            "importer_login": "mirror-bot",
            "source_url": "https://lore.kernel.org/xen-devel/",
            "nntp_host": "nntp.lore.kernel.org",
            "nntp_group": "org.xenproject.lists.xen-devel",
            "git_remote": self.remote,
            "git_username": "git-bot",
            "base_commit": "b" * 40,
            "target_branch": "main",
            "max_inline_lines": 50,
            "lookback_hours": 148,
            "notifications": "disabled",
            "identity_rule": "isolated-unverified",
            "retry_uncertain": False,
        }
        path = self.repository / "worker-policy.json"
        path.write_text(json.dumps(policy))
        path.chmod(0o600)
        state = self.repository / "worker.sqlite3"
        capabilities = {
            "version": 1,
            "repository_id": 1,
            "importer_id": 5,
            "importer_login": "mirror-bot",
            "source_url": policy["source_url"],
            "operations": ["pull", "conversation", "review", "review-reply"],
            "max_inline_lines": 50,
            "unattended_allowed": True,
        }

        def transport(request, _timeout):
            if request.get_method() == "POST" and request.full_url.endswith("/issues"):
                return FakeResponse(201, {"number": 42})
            self.assertEqual(request.get_method(), "GET")
            if "/issues?" in request.full_url:
                return FakeResponse(200, [])
            return FakeResponse(200, capabilities)

        self.api.transport = transport
        credentials = GitHttpCredentials("git-bot", "synthetic-git-token")
        empty = run_import_worker_round(
            self.ledger,
            self.repository,
            path,
            state,
            self.api,
            credentials,
            refresh_source=False,
        )
        self.assertEqual(
            (empty.considered, empty.published, empty.delivered), (0, 0, 0)
        )
        self.assertEqual(state.stat().st_mode & 0o777, 0o600)
        self.ledger.record_parsed_message(self.revision.messages[0])
        reply = parse_message(
            b"From: Reviewer <reviewer@example.test>\nMessage-ID: <worker-reply@example.test>\n"
            b"Subject: Re: patch\nIn-Reply-To: <patch@example.test>\n\nWorker reply\n"
        )
        assert reply.message_id
        self.ledger.record_parsed_message(reply)
        child = parse_message(
            b"From: Reviewer <reviewer@example.test>\nMessage-ID: <worker-child@example.test>\n"
            b"Subject: Re: patch\nIn-Reply-To: <worker-reply@example.test>\n\nChild reply\n"
        )
        grandchild = parse_message(
            b"From: Reviewer <reviewer@example.test>\nMessage-ID: <worker-grandchild@example.test>\n"
            b"Subject: Re: patch\nIn-Reply-To: <worker-child@example.test>\n\nGrandchild reply\n"
        )
        self.ledger.record_parsed_message(grandchild)
        self.ledger.record_parsed_message(child)
        destination = self.api.base_url + "repos/o/r#7"
        manual = self.ledger.enqueue(
            reply.message_id,
            "IMPORT_CONVERSATION",
            destination,
            "manual-effect",
            {"manual": True},
        )

        with patch(
            "lore_review_companion.import_worker.sync_imported_series_revision",
            side_effect=GitPreflightError(
                "Applying: successful first patch\nPatch failed at 0004 failed fourth patch\n"
                "error: patch failed: xen/file.c:10"
            ),
        ) as blocked:
            skipped = run_import_worker_round(
                self.ledger,
                self.repository,
                path,
                state,
                self.api,
                credentials,
                refresh_source=False,
            )
        self.assertEqual(
            (skipped.considered, skipped.published, skipped.delivered), (1, 0, 0)
        )
        self.assertIn("Preflight blocked:", skipped.diagnostics[0])
        self.assertIn("PR creation", skipped.diagnostics[0])
        self.assertIn("https://lore.kernel.org/xen-devel/patch@example.test/", skipped.diagnostics[0])
        self.assertIn("Patch failed at 0004 failed fourth patch", skipped.diagnostics[0])
        self.assertIn("xen/file.c:10", skipped.diagnostics[0])
        self.assertNotIn("successful first patch", skipped.diagnostics[0])
        self.assertFalse(blocked.call_args.args[4].write_enabled)
        self.assertFalse(state.with_name(state.name + ".incident").exists())
        self.assertIsNone(
            self.ledger.publication(
                self.api.base_url + "repos/o/r",
                self.revision.series_key,
                self.revision.revision,
            )
        )

        def publication(current, _repository, _base, _remote, client, **options):
            if not client.write_enabled:
                return SimpleNamespace(
                    approval_digest="reviewed plan",
                    refs=SimpleNamespace(
                        head=SimpleNamespace(planned=False),
                        base=SimpleNamespace(planned=False),
                    ),
                )
            self.assertEqual(options["approved_digest"], "reviewed plan")
            self.ledger.record_series_revision(current)
            key = self.api.base_url + "repos/o/r", current.series_key, current.revision
            if self.ledger.publication(*key) is None:
                self.ledger.begin_publication(
                    *key, "digest", "b" * 40, "lore/revision", "a" * 40
                )
                self.ledger.transition_publication(
                    *key, expected=("prepared",), status="complete", pull_index=7
                )
            return SimpleNamespace(
                pull_request=SimpleNamespace(remote_object={"pull_index": 7})
            )

        def queue(*arguments, **options):
            selected = arguments[6][0]
            if selected in (child, grandchild):
                parent = selected.in_reply_to[0]
                self.assertTrue(
                    all(
                        effect.write_state == "complete"
                        for effect in self.ledger.comment_effects(parent, destination)
                        if effect.id != manual
                    )
                )
            self.ledger.enqueue(
                selected.message_id,
                "IMPORT_REVIEW",
                destination,
                "worker-effect",
                {"worker": True},
            )
            return SimpleNamespace(diagnostics=())

        def plan(*arguments, **options):
            self.assertNotIn(manual, options["delivery_ids"])
            return SimpleNamespace(
                deliveries=options["delivery_ids"], approval_digest=lambda: "batch-plan"
            )

        def deliver(*arguments, **options):
            self.assertNotIn(manual, options["delivery_ids"])
            for identifier in options["delivery_ids"]:
                self.ledger.mark_delivered(identifier)
            return SimpleNamespace(delivered=len(options["delivery_ids"]), failed=0)

        with (
            patch(
                "lore_review_companion.import_worker.sync_imported_series_revision",
                side_effect=publication,
            ),
            patch(
                "lore_review_companion.import_worker.queue_imported_replies",
                side_effect=queue,
            ),
            patch(
                "lore_review_companion.import_worker.plan_import_delivery",
                side_effect=plan,
            ),
            patch(
                "lore_review_companion.import_worker.deliver_approved_imports",
                side_effect=deliver,
            ),
        ):
            first = run_import_worker_round(
                self.ledger,
                self.repository,
                path,
                state,
                self.api,
                credentials,
                refresh_source=False,
            )
            second = run_import_worker_round(
                self.ledger,
                self.repository,
                path,
                state,
                self.api,
                credentials,
                refresh_source=False,
            )
        self.assertEqual((first.considered, first.published), (1, 1))
        self.assertEqual((second.considered, second.published), (1, 0))
        self.assertEqual((first.delivered, second.delivered), (3, 0))
        self.assertEqual([effect.id for effect in self.ledger.pending()], [manual])
        self.assertIn("Pre-existing", first.diagnostics[0])
        with closing(sqlite3.connect(state)) as schedule:
            self.assertEqual(
                schedule.execute("SELECT COUNT(*) FROM revisions").fetchone()[0], 1
            )
        different = Ledger(self.repository / "different-ledger.sqlite3")
        with self.assertRaisesRegex(ValueError, "different mail ledger"):
            run_import_worker_round(
                different,
                self.repository,
                path,
                state,
                self.api,
                credentials,
                refresh_source=False,
            )
        self.assertFalse(state.with_name(state.name + ".incident").exists())
        capabilities["unattended_allowed"] = False
        with self.assertRaisesRegex(ValueError, "disabled"):
            run_import_worker_round(
                self.ledger,
                self.repository,
                path,
                state,
                self.api,
                credentials,
                refresh_source=False,
            )
        incident = state.with_name(state.name + ".incident")
        self.assertFalse(incident.exists())
        incident.write_text("obsolete development latch\n")
        capabilities["unattended_allowed"] = True
        with patch(
            "lore_review_companion.import_worker.sync_imported_series_revision",
            side_effect=GitPreflightError("transient preflight failure"),
        ):
            with self.assertRaises(GitPreflightError):
                run_import_worker_round(
                    self.ledger,
                    self.repository,
                    path,
                    state,
                    self.api,
                    credentials,
                    refresh_source=False,
                )
        incident.unlink()
        path.write_text(json.dumps({**policy, "lookback_hours": 149}))
        with patch(
            "lore_review_companion.import_worker.sync_imported_series_revision",
            side_effect=GitPreflightError("still retryable"),
        ):
            with self.assertRaises(GitPreflightError):
                run_import_worker_round(
                    self.ledger,
                    self.repository,
                    path,
                    state,
                    self.api,
                    credentials,
                    refresh_source=False,
                )
        with closing(sqlite3.connect(state)) as schedule:
            self.assertEqual(
                schedule.execute("SELECT policy_digest FROM worker").fetchone()[0],
                read_import_policy(path).digest(),
            )
            self.assertEqual(
                schedule.execute("SELECT COUNT(*) FROM effects").fetchone()[0], 3
            )
        self.assertFalse(incident.exists())

    def test_worker_cli_binds_destinations_and_credentials_only_from_private_policy(
        self,
    ):
        options = SimpleNamespace(api_url=self.api.base_url, git_username="git-bot")
        arguments = [
            "run-import-worker",
            "--policy-file",
            str(self.repository / "policy.json"),
            "--ledger",
            str(self.ledger.path),
            "--state",
            str(self.repository / "worker.sqlite3"),
            "--repository",
            str(self.repository),
            "--token-file",
            "api-token",
            "--git-token-file",
            "git-token",
            "--forge-path",
            str(self.repository),
            "--no-refresh-source",
        ]
        with (
            patch("lore_review_companion.cli.read_import_policy", return_value=options),
            patch(
                "lore_review_companion.cli.read_private_token",
                side_effect=("synthetic-api-token", "synthetic-git-token"),
            ),
            patch("lore_review_companion.cli.run_import_worker") as run,
            patch("lore_review_companion.cli.asdict", return_value={}),
            redirect_stdout(StringIO()),
        ):
            self.assertEqual(main(arguments), 0)
        self.assertFalse(run.call_args.kwargs["refresh_source"])
        self.assertEqual(run.call_args.args[4].base_url, self.api.base_url)
        self.assertEqual(run.call_args.args[5].username, "git-bot")

    def test_imported_publication_preview_approval_replay_and_uncertain_retry(self):
        requests, accepted = [], {}
        fail_after_acceptance = False

        def transport(request, _timeout):
            requests.append(request)
            if request.full_url.endswith("/review-imports/capabilities"):
                return FakeResponse(
                    200,
                    {
                        "version": 1,
                        "repository_id": 1,
                        "importer_id": 5,
                        "importer_login": "mirror-bot",
                        "source_url": "https://lore.kernel.org/xen-devel/",
                        "operations": ["pull"],
                    },
                )
            self.assertEqual(request.get_method(), "POST")
            self.assertTrue(request.full_url.endswith("/review-imports/pulls"))
            payload = json.loads(request.data)
            if not accepted:
                accepted.update(
                    {
                        "id": 1,
                        "repository_id": 1,
                        "importer_id": 5,
                        "participant_id": 6,
                        "type": "pull",
                        "pull_index": 7,
                        "issue_id": 10,
                        "source_url": "https://lore.kernel.org/xen-devel/"
                        + payload["message_id"]
                        + "/",
                        **payload,
                    }
                )
            if fail_after_acceptance:
                return FakeResponse(503, {"message": "uncertain acceptance"})
            return FakeResponse(200, accepted)

        self.api.transport = transport
        preview = self.imported_sync()
        self.assertFalse(preview.applied)
        self.assertEqual(accepted, {})
        self.assertFalse(any(self.branch_writes))
        self.assertEqual(preview.plan.patch_commits[0].message_id, "patch@example.test")
        self.assertEqual(
            preview.plan.request["title"], "Patch Author: xen: change [v1]"
        )
        self.assertNotIn("Revision:", preview.plan.request["body"])
        self.assertNotIn(
            "synthetic-git-token",
            json.dumps(replace(preview.plan, request={}).__dict__, default=str),
        )
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.imported_sync(write_enabled=True, approved_digest="f" * 64)
        self.assertFalse(any(self.branch_writes))
        fail_after_acceptance = True
        with self.assertRaises(DeliveryUncertainError):
            self.imported_sync(
                write_enabled=True, approved_digest=preview.approval_digest
            )
        publication = self.ledger.publication(
            self.api.base_url + "repos/o/r", self.revision.series_key, 1
        )
        assert publication
        self.assertEqual(publication.status, "uncertain")
        count = len(requests)
        with self.assertRaises(PublicationUncertain):
            self.imported_sync(
                write_enabled=True, approved_digest=preview.approval_digest
            )
        self.assertFalse(
            any(request.get_method() == "POST" for request in requests[count:])
        )
        retry = self.imported_sync(retry_uncertain=True)
        self.assertNotEqual(retry.approval_digest, preview.approval_digest)
        fail_after_acceptance = False
        result = self.imported_sync(
            write_enabled=True,
            approved_digest=retry.approval_digest,
            retry_uncertain=True,
        )
        self.assertTrue(result.applied)
        self.assertEqual(result.pull_request.remote_object["pull_index"], 7)
        replay = self.imported_sync(
            write_enabled=True, approved_digest=preview.approval_digest
        )
        self.assertTrue(replay.applied)
        self.assertEqual(len(accepted), len(result.pull_request.remote_object))
        publication = self.ledger.publication(
            self.api.base_url + "repos/o/r", self.revision.series_key, 1
        )
        assert publication
        self.assertEqual((publication.status, publication.pull_index), ("complete", 7))

    def test_imported_publication_replays_historical_target_after_staging_correction(self):
        self.api.transport = lambda request, timeout: FakeResponse(
            200,
            {"version": 1, "repository_id": 1, "importer_id": 5,
             "importer_login": "mirror-bot", "source_url": "https://lore.kernel.org/xen-devel/",
             "operations": ["pull"]},
        )
        preview = self.imported_sync(target_branch="master")
        self.ledger.record_series_revision(self.revision)
        plan = preview.plan
        self.ledger.begin_publication(
            self.api.base_url + "repos/o/r", self.revision.series_key, 1,
            replace(plan, retry_uncertain=False).approval_digest(),
            plan.base_commit, plan.head_branch, plan.head_commit,
        )
        replay = self.imported_sync(target_branch="staging")
        self.assertEqual(replay.plan.request, preview.plan.request)
        self.assertEqual(replay.approval_digest, preview.approval_digest)
        self.assertEqual(replay.plan.head_commit, preview.plan.head_commit)
        with self.assertRaisesRegex(PublicationConflict, "supported presentation"):
            self.imported_sync(target_branch="unrelated")

    def test_imported_publication_warns_about_rejects_and_alternate_base(self):
        self.api.transport = lambda request, timeout: FakeResponse(
            200, {"version": 1, "repository_id": 1, "importer_id": 5,
                  "importer_login": "mirror-bot", "source_url": "https://lore.kernel.org/xen-devel/",
                  "operations": ["pull"]},
        )

        @contextmanager
        def prepare(_repository, _base_ref, _mailbox, **_options):
            yield SimpleNamespace(
                result=result,
                patch_commits=lambda identifiers: (PatchCommit(identifiers[0], result.base_commit, result.head_commit),),
            )

        with patch.object(self, "prepare", new=prepare):
            result = PreflightResult("b" * 40, "a" * 40, 1, 3, "b" * 40)
            rejects = self.imported_sync(target_branch="staging")
            self.assertTrue(rejects.plan.request["body"].startswith(
                "> **Patch application warning:** 3 hunk(s) could not be applied to `staging`"
            ))
            self.assertIn("`.rej`", rejects.plan.request["body"])
            result = PreflightResult("c" * 40, "a" * 40, 1, 0, "b" * 40)
            original_base = self.imported_sync(target_branch="staging")
            self.assertTrue(original_base.plan.request["body"].startswith(
                "> **Patch application warning:** This submission did not apply cleanly to `staging`."
            ))
            self.assertIn("`" + "c" * 40 + "`", original_base.plan.request["body"])

    def test_imported_publication_retains_both_previous_presentations(self):
        self.api.transport = lambda request, timeout: FakeResponse(
            200,
            {
                "version": 1,
                "repository_id": 1,
                "importer_id": 5,
                "importer_login": "mirror-bot",
                "source_url": "https://lore.kernel.org/xen-devel/",
                "operations": ["pull"],
            },
        )
        source = self.revision.messages[0]
        preview = self.imported_sync()
        self.ledger.record_series_revision(self.revision)
        for body in (
            submission_text(source.body_text, strip_shortlog=False)
            + "\n\nRevision: v1",
            "Revision: v1\n\n" + quote_untrusted_text(source.body_text),
        ):
            with self.subTest(body=body):
                old_request = {
                    **preview.plan.request,
                    "title": "[v1] xen: change",
                    "body": body,
                }
                old_plan = replace(preview.plan, request=old_request)
                with self.ledger._connection() as connection:
                    connection.execute("DELETE FROM publications")
                self.ledger.begin_publication(
                    self.api.base_url + "repos/o/r",
                    self.revision.series_key,
                    1,
                    old_plan.approval_digest(),
                    old_plan.base_commit,
                    old_plan.head_branch,
                    old_plan.head_commit,
                )
                replay = self.imported_sync()
                self.assertEqual(replay.plan.request, old_request)
                self.assertEqual(replay.approval_digest, old_plan.approval_digest())
                self.assertFalse(replay.applied)

    def test_imported_publication_replays_previous_plain_body(self):
        self.api.transport = lambda request, timeout: FakeResponse(
            200, {"version": 1, "repository_id": 1, "importer_id": 5,
                  "importer_login": "mirror-bot", "source_url": "https://lore.kernel.org/xen-devel/",
                  "operations": ["pull"]},
        )
        preview = self.imported_sync()
        self.assertTrue(preview.plan.request["body"].startswith("> "))
        old_request = {**preview.plan.request, "body": submission_text(self.revision.messages[0].body_text)}
        old_plan = replace(preview.plan, request=old_request)
        self.ledger.record_series_revision(self.revision)
        self.ledger.begin_publication(
            self.api.base_url + "repos/o/r", self.revision.series_key, 1,
            old_plan.approval_digest(), old_plan.base_commit, old_plan.head_branch, old_plan.head_commit,
        )
        replay = self.imported_sync()
        self.assertEqual(replay.plan.request, old_request)
        self.assertEqual(replay.approval_digest, old_plan.approval_digest())

    def test_imported_series_cli_uses_read_only_preview_and_separate_git_credentials(
        self,
    ):
        for source in self.revision.messages:
            self.ledger.record_parsed_message(source)
        token_path = self.repository / "api-token"
        token_path.write_text("synthetic-api-token")
        token_path.chmod(0o600)
        git_path = self.repository / "git-token"
        git_path.write_text("synthetic-git-token")
        git_path.chmod(0o600)
        assert self.revision.messages[0].message_id
        arguments = [
            "sync-imported-series",
            "--ledger",
            str(self.ledger.path),
            "--api-url",
            self.api.base_url,
            "--owner",
            "o",
            "--repo",
            "r",
            "--series-seed",
            self.revision.messages[0].message_id,
            "--revision",
            "1",
            "--repository",
            str(self.repository),
            "--base",
            "HEAD",
            "--target-branch",
            "main",
            "--remote",
            self.remote,
            "--token-file",
            str(token_path),
            "--git-token-file",
            str(git_path),
            "--git-username",
            "git-bot",
        ]
        before = self.ledger.path.read_bytes()
        with (
            patch("lore_review_companion.cli.sync_imported_series_revision") as sync,
            patch("lore_review_companion.cli.asdict", return_value={}),
            redirect_stdout(StringIO()),
        ):
            self.assertEqual(main(arguments), 0)
        self.assertFalse(sync.call_args.args[4].write_enabled)
        self.assertEqual(sync.call_args.kwargs["credentials"].username, "git-bot")
        self.assertEqual(self.ledger.path.read_bytes(), before)
        with (
            patch("lore_review_companion.cli.plan_imported_sync") as coordinated,
            patch("lore_review_companion.cli.asdict", return_value={}),
            redirect_stdout(StringIO()),
        ):
            self.assertEqual(main(["plan-sync", *arguments[1:]]), 0)
        self.assertFalse(coordinated.call_args.args[1].write_enabled)
        self.assertEqual(
            coordinated.call_args.kwargs["credentials"].username, "git-bot"
        )
        self.assertEqual(self.ledger.path.read_bytes(), before)
        with redirect_stderr(StringIO()):
            self.assertEqual(main([*arguments, "--write"]), 1)

    def test_imported_v2_links_v1_without_rewriting_original_pr(self):
        accepted = {}

        def transport(request, _timeout):
            if request.full_url.endswith("/capabilities"):
                return FakeResponse(
                    200,
                    {
                        "version": 1,
                        "repository_id": 1,
                        "importer_id": 5,
                        "importer_login": "mirror-bot",
                        "source_url": "https://lore.kernel.org/xen-devel/",
                        "operations": ["pull"],
                    },
                )
            payload = json.loads(request.data)
            key = payload["operation_key"]
            if key not in accepted:
                accepted[key] = {
                    "id": len(accepted) + 1,
                    "repository_id": 1,
                    "importer_id": 5,
                    "participant_id": 6,
                    "type": "pull",
                    "pull_index": len(accepted) + 7,
                    "issue_id": len(accepted) + 10,
                    "source_url": "https://lore.kernel.org/xen-devel/"
                    + payload["message_id"]
                    + "/",
                    **payload,
                }
            self.assertEqual(accepted[key]["body"], payload["body"])
            return FakeResponse(200, accepted[key])

        self.api.transport = transport
        first = self.imported_sync()
        self.imported_sync(write_enabled=True, approved_digest=first.approval_digest)
        original = json.loads(json.dumps(accepted))
        old_refs = self.imported_branch_exists.copy()
        second = parse_message(
            b"From: Author <author@example.test>\nMessage-ID: <v2@example.test>\nSubject: [PATCH v2 1/1] xen: change\n\nSecond revision\n\nSigned-off-by: Author <author@example.test>\n"
        )
        assert second.message_id
        self.ledger.record_parsed_message(second)
        arguments = [
            "link-revision",
            "--ledger",
            str(self.ledger.path),
            "--series-key",
            self.revision.series_key,
            "--canonical-revision",
            "1",
            "--candidate-seed",
            second.message_id,
            "--candidate-revision",
            "2",
            "--actor",
            "operator",
            "--reason",
            "Reviewed revision relationship",
            "--allow-unmatched-change-id",
        ]
        with redirect_stdout(StringIO()) as output:
            self.assertEqual(main(arguments), 0)
        digest = json.loads(output.getvalue())["approval_digest"]
        with redirect_stdout(StringIO()):
            self.assertEqual(main([*arguments, "--write", "--approve", digest]), 0)
        self.revision = self.ledger.discover_related_series(second.message_id)[0]
        preview = self.imported_sync()
        self.assertIn(
            "Earlier revision: [v1](https://gitea.test/o/r/pulls/7)",
            preview.plan.request["body"],
        )
        result = self.imported_sync(
            write_enabled=True, approved_digest=preview.approval_digest
        )
        self.assertEqual(result.pull_request.remote_object["pull_index"], 8)
        self.assertNotEqual(first.plan.head_branch, preview.plan.head_branch)
        self.assertTrue(old_refs < self.imported_branch_exists)
        self.assertEqual(len(accepted), 2)
        for key, payload in original.items():
            self.assertEqual(accepted[key], payload)

    def ensure_branch(self, prepared, remote, **kwargs):
        self.branch_writes.append(kwargs.get("write_enabled", False))
        created = kwargs.get("write_enabled", False) and not self.branch_exists
        if created:
            self.branch_exists = True
        return RevisionBranchResult(
            revision_branch_name(kwargs["series_key"], kwargs["revision"]),
            prepared.result.head_commit,
            created,
            planned=not self.branch_exists,
        )

    def sync(self, publisher, **kwargs):
        with (
            patch(
                "lore_review_companion.series_sync.assemble_local_series",
                return_value=AssemblyResult(b"patch", "", ""),
            ),
            patch(
                "lore_review_companion.series_sync.prepare_patch_mailbox",
                new=self.prepare,
            ),
            patch(
                "lore_review_companion.series_sync.ensure_revision_branch",
                new=self.ensure_branch,
            ),
        ):
            return sync_series_revision(
                self.revision,
                self.repository,
                "HEAD",
                self.remote,
                publisher,
                target_branch="main",
                ledger=self.ledger,
                **kwargs,
            )

    def test_preview_and_approved_write_use_the_same_plan(self):
        preview = self.sync(ForgePullPublisher(self.api, "o", "r"), labels=(7,))
        self.assertFalse(preview.applied)
        self.assertTrue(preview.pull_request.planned)
        self.assertEqual(preview.attribution_status, "planned")
        self.assertTrue(
            preview.plan.body.startswith("Review mirror on behalf of @mirror-bot\n\n")
        )
        self.assertIn("From (unverified):\n\n```text\n", preview.plan.body)
        self.assertEqual(self.branch_writes, [False])
        self.assertEqual(self.transport.pulls, [])

        result = self.sync(
            ForgePullPublisher(self.api, "o", "r", write_enabled=True),
            labels=(7,),
            approved_digest=preview.approval_digest,
        )
        self.assertTrue(result.applied)
        self.assertEqual(result.attribution_status, "present")
        self.assertEqual(result.approval_digest, preview.approval_digest)
        self.assertEqual(self.branch_writes, [False, False, True])
        self.assertEqual(len(self.transport.pulls), 1)
        publication = self.ledger.publication(
            "https://gitea.test/api/v1/repos/o/r", self.revision.series_key, 1
        )
        assert publication
        self.assertEqual((publication.status, publication.pull_index), ("complete", 1))

    def test_list_publications_is_read_only_filtered_and_paginated(self):
        preview = self.sync(ForgePullPublisher(self.api, "o", "r"))
        self.sync(
            ForgePullPublisher(self.api, "o", "r", write_enabled=True),
            approved_digest=preview.approval_digest,
        )
        self.ledger.begin_publication(
            "other",
            self.revision.series_key,
            1,
            "other-digest",
            "b" * 40,
            "other-branch",
            "a" * 40,
        )
        arguments = [
            "list-publications",
            "--ledger",
            str(self.ledger.path),
            "--series-key",
            self.revision.series_key,
        ]
        before = self.ledger.path.read_bytes()
        with redirect_stdout(StringIO()) as output:
            self.assertEqual(main([*arguments, "--limit", "1"]), 0)
        page = json.loads(output.getvalue())
        self.assertEqual(page["publications"][0]["publication"]["pull_index"], 1)
        self.assertIsNotNone(page["next_cursor"])
        with redirect_stdout(StringIO()) as output:
            self.assertEqual(
                main(
                    [
                        *arguments,
                        "--after",
                        str(page["next_cursor"]),
                        "--status",
                        "prepared",
                    ]
                ),
                0,
            )
        following = json.loads(output.getvalue())
        self.assertEqual(
            following["publications"][0]["publication"]["destination"], "other"
        )
        self.assertIsNone(following["next_cursor"])
        self.assertEqual(self.ledger.path.read_bytes(), before)
        with self.assertRaisesRegex(ValueError, "limit"):
            self.ledger.list_publications(limit=1001)

    def test_changed_plan_does_not_write(self):
        preview = self.sync(ForgePullPublisher(self.api, "o", "r"), labels=(7,))
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.sync(
                ForgePullPublisher(self.api, "o", "r", write_enabled=True),
                labels=(8,),
                approved_digest=preview.approval_digest,
            )
        self.assertFalse(any(self.branch_writes))
        self.assertEqual(self.transport.pulls, [])

    def test_revision_policy_is_bound_to_the_approval_digest(self):
        preview = self.sync(ForgePullPublisher(self.api, "o", "r"))
        separate = self.sync(
            ForgePullPublisher(self.api, "o", "r"), revision_policy="separate"
        )
        self.assertNotEqual(preview.approval_digest, separate.approval_digest)
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.sync(
                ForgePullPublisher(self.api, "o", "r", write_enabled=True),
                revision_policy="separate",
                approved_digest=preview.approval_digest,
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
        assert second.message_id
        self.revision = discover_series((first,))[0]
        preview = self.sync(ForgePullPublisher(self.api, "o", "r"))
        writer = ForgePullPublisher(self.api, "o", "r", write_enabled=True)
        self.sync(writer, approved_digest=preview.approval_digest)
        original_head = self.transport.pulls[0]["head"].copy()
        self.ledger.record_parsed_message(second)
        arguments = [
            "link-revision",
            "--ledger",
            str(self.ledger.path),
            "--series-key",
            self.revision.series_key,
            "--canonical-revision",
            "1",
            "--candidate-seed",
            second.message_id,
            "--candidate-revision",
            "2",
            "--actor",
            "operator",
            "--reason",
            "reviewed patch history",
        ]
        with redirect_stderr(StringIO()):
            self.assertEqual(main(arguments), 1)
        arguments.append("--allow-unmatched-change-id")
        with redirect_stdout(StringIO()) as output:
            self.assertEqual(main(arguments), 0)
        preview_link = json.loads(output.getvalue())
        self.assertEqual(
            preview_link["plan"]["manual_claims"],
            {
                "canonical_change_id": "stable-change",
                "candidate_change_id": None,
            },
        )
        digest = preview_link["approval_digest"]
        encoded = json.dumps(
            preview_link["plan"], sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        self.assertEqual(digest, hashlib.sha256(encoded).hexdigest())
        with redirect_stdout(StringIO()):
            self.assertEqual(main([*arguments, "--write", "--approve", digest]), 0)
        self.revision = self.ledger.discover_related_series(second.message_id)[0]
        with self.assertRaises(PullRequestConflict):
            self.sync(ForgePullPublisher(self.api, "o", "r"))
        preview = self.sync(
            ForgePullPublisher(self.api, "o", "r"), revision_policy="separate"
        )
        result = self.sync(
            writer, revision_policy="separate", approved_digest=preview.approval_digest
        )
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
            connection.execute(
                "UPDATE publications SET approval_digest = ?", (legacy_digest,)
            )
        replay = self.sync(writer, approved_digest=preview.approval_digest)
        self.assertFalse(replay.pull_request.created)
        self.assertEqual(len(self.transport.pulls), 1)
        recorded = self.ledger.publication(
            "https://gitea.test/api/v1/repos/o/r", self.revision.series_key, 1
        )
        assert recorded
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
        self.transport.pulls[0][
            "body"
        ] = f"{legacy_body}\n\n{_series_marker(self.revision.series_key)}"
        with self.ledger._connection() as connection:
            connection.execute(
                "UPDATE publications SET approval_digest = ?", (legacy_digest,)
            )
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
            "child@example.test",
            "[PATCH 1/1] change",
            headers="In-Reply-To: <parent@example.test>\n",
        )
        self.revision = discover_series((child,))[0]
        with self.assertRaisesRegex(ValueError, "complete, unambiguous"):
            self.sync(ForgePullPublisher(self.api, "o", "r"))
        self.assertEqual(self.branch_writes, [])
        parent = message("parent@example.test", "older discussion")
        self.ledger.record_parsed_message(parent)
        self.revision = discover_series((child,), available_message_ids=(parent.message_id,))[0]
        preview = self.sync(ForgePullPublisher(self.api, "o", "r"))
        self.assertTrue(preview.pull_request.planned)

    def test_new_related_patch_blocks_stale_approved_publication(self):
        first = message("first@example.test", "[PATCH 1/1] original")
        self.revision = discover_series([first])[0]
        preview = self.sync(ForgePullPublisher(self.api, "o", "r"))
        duplicate = message(
            "duplicate@example.test",
            "[PATCH 1/1] conflicting sibling",
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
        self.revision = discover_series(
            [message("patch@example.test", "[PATCH 1/2] incomplete")]
        )[0]
        with self.assertRaisesRegex(ValueError, "complete"):
            self.sync(ForgePullPublisher(self.api, "o", "r"))

    def test_quarantined_series_is_not_published_on_original_replay(self):
        patch_message = self.revision.messages[0]
        assert patch_message.message_id
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
        self.assertEqual(
            sum(request.get_method() == "POST" for request in self.transport.requests),
            1,
        )
        result = self.sync(
            writer, approved_digest=preview.approval_digest, retry_uncertain=True
        )
        self.assertTrue(result.pull_request.created)

    def test_reconciles_pr_after_remote_write_before_ledger_acknowledgment(self):
        preview = self.sync(ForgePullPublisher(self.api, "o", "r"))
        writer = ForgePullPublisher(self.api, "o", "r", write_enabled=True)
        transition = self.ledger.transition_publication

        def fail_acknowledgment(*args, **kwargs):
            if kwargs["status"] == "complete":
                raise sqlite3.OperationalError("simulated local acknowledgment failure")
            return transition(*args, **kwargs)

        with patch.object(
            self.ledger, "transition_publication", side_effect=fail_acknowledgment
        ):
            with self.assertRaises(sqlite3.OperationalError):
                self.sync(writer, approved_digest=preview.approval_digest)
        self.assertEqual(len(self.transport.pulls), 1)
        replay = self.sync(writer, approved_digest=preview.approval_digest)
        self.assertFalse(replay.pull_request.created)
        self.assertEqual(len(self.transport.pulls), 1)

    def test_cli_requires_explicit_approval_and_reads_token_from_environment(self):
        arguments = [
            "sync-series",
            "--mbox",
            "series.mbox",
            "--repository",
            str(self.repository),
            "--base",
            "HEAD",
            "--remote",
            self.remote,
            "--api-url",
            self.api.base_url,
            "--owner",
            "o",
            "--repo",
            "r",
            "--target-branch",
            "main",
            "--label",
            "7",
            "--ledger",
            str(self.repository / "lore_review_companion.cli.sqlite3"),
        ]
        with redirect_stderr(StringIO()):
            self.assertEqual(main([*arguments, "--write"]), 1)
        with (
            patch.dict("os.environ", {"LORE_FORGE_TOKEN": "sample-token"}),
            patch(
                "lore_review_companion.cli.parse_mbox",
                return_value=self.revision.messages,
            ),
            patch("lore_review_companion.cli.sync_series_revision") as sync,
            redirect_stdout(StringIO()),
        ):
            sync.return_value = SimpleNamespace()
            with patch("lore_review_companion.cli.asdict", return_value={}):
                self.assertEqual(
                    main([*arguments, "--write", "--approve", "reviewed"]), 0
                )
            self.assertTrue(sync.call_args.args[4].write_enabled)
            self.assertEqual(sync.call_args.kwargs["approved_digest"], "reviewed")
            self.assertEqual(sync.call_args.kwargs["labels"], (7,))
            self.assertEqual(sync.call_args.kwargs["credentials"].token, "sample-token")

    def test_cli_can_preview_a_series_selected_from_the_archive_ledger(self):
        patch_message = self.revision.messages[0]
        assert patch_message.message_id
        self.ledger.record_parsed_message(patch_message)
        arguments = [
            "sync-series",
            "--message-id",
            patch_message.message_id,
            "--repository",
            str(self.repository),
            "--base",
            "HEAD",
            "--remote",
            self.remote,
            "--api-url",
            self.api.base_url,
            "--owner",
            "o",
            "--repo",
            "r",
            "--target-branch",
            "main",
            "--ledger",
            str(self.ledger.path),
        ]
        with (
            patch.dict("os.environ", {"LORE_FORGE_TOKEN": "sample-token"}),
            patch("lore_review_companion.cli.sync_series_revision") as sync,
            patch("lore_review_companion.cli.asdict", return_value={}),
            redirect_stdout(StringIO()),
        ):
            sync.return_value = SimpleNamespace()
            self.assertEqual(main(arguments), 0)
            self.assertEqual(sync.call_args.args[0], self.revision)
            self.assertIsNotNone(sync.call_args.kwargs["ledger"])

    def test_cli_can_discover_and_preview_a_series_from_one_stored_seed(self):
        patch_message = self.revision.messages[0]
        assert patch_message.message_id
        self.ledger.record_parsed_message(patch_message)
        with redirect_stdout(StringIO()) as output:
            self.assertEqual(
                main(
                    [
                        "discover-series",
                        "--ledger",
                        str(self.ledger.path),
                        "--message-id",
                        patch_message.message_id,
                    ]
                ),
                0,
            )
        self.assertIn(patch_message.message_id, output.getvalue())
        arguments = [
            "sync-series",
            "--series-seed",
            patch_message.message_id,
            "--repository",
            str(self.repository),
            "--base",
            "HEAD",
            "--remote",
            self.remote,
            "--api-url",
            self.api.base_url,
            "--owner",
            "o",
            "--repo",
            "r",
            "--target-branch",
            "main",
            "--ledger",
            str(self.ledger.path),
        ]
        with (
            patch.dict("os.environ", {"LORE_FORGE_TOKEN": "sample-token"}),
            patch("lore_review_companion.cli.sync_series_revision") as sync,
            patch("lore_review_companion.cli.asdict", return_value={}),
            redirect_stdout(StringIO()),
        ):
            sync.return_value = SimpleNamespace()
            self.assertEqual(main(arguments), 0)
            self.assertEqual(sync.call_args.args[0], self.revision)


if __name__ == "__main__":
    unittest.main()
