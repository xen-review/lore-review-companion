# SPDX-License-Identifier: GPL-2.0-or-later

import json
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from contextlib import redirect_stdout
from io import BytesIO, StringIO
from pathlib import Path
from urllib.parse import quote, urlsplit
from unittest.mock import patch

from lore_review_companion.cli import main
from lore_review_companion.git_branches import GitHttpCredentials
from lore_review_companion.forge_api import ForgeAPI, ForgeAPIError
from lore_review_companion.forge_imports import ForgeImportClient
from lore_review_companion.forge_comments import ForgeCommentPublisher
from lore_review_companion.forge_pulls import ForgePullPublisher, PullRequestConflict
from lore_review_companion.ingest import sync_public_inbox
from lore_review_companion.import_worker import run_import_worker_round
from lore_review_companion.import_refresh import (
    apply_import_refresh,
    plan_import_refresh,
    refresh_digest,
)
from lore_review_companion.ledger import Ledger, PublicationConflict
from lore_review_companion.mail_parser import parse_mbox, parse_message
from lore_review_companion.outbox import (
    deliver_approved_comments,
    deliver_approved_imports,
    plan_comment_delivery,
    plan_import_delivery,
)
from lore_review_companion.review_queue import (
    queue_archive_replies,
    queue_b4_drafts,
    queue_imported_replies,
)
from lore_review_companion.series import discover_series
from lore_review_companion.series_sync import (
    sync_imported_series_revision,
    sync_series_revision,
)


@unittest.skipUnless(
    os.environ.get("LORE_FORGE_ACCEPTANCE") == "1",
    "requires explicit disposable-server opt-in",
)
class LiveAcceptanceTests(unittest.TestCase):
    def test_preview_publish_and_replay_on_disposable_gitea(self):
        token = os.environ.get("LORE_FORGE_ACCEPTANCE_TOKEN")
        self.assertTrue(token, "LORE_FORGE_ACCEPTANCE_TOKEN is required")
        b4_python = os.environ.get("LORE_FORGE_ACCEPTANCE_B4_PYTHON")
        self.assertTrue(b4_python, "LORE_FORGE_ACCEPTANCE_B4_PYTHON is required")
        api_url = os.environ.get(
            "LORE_FORGE_ACCEPTANCE_API", "http://127.0.0.1:30002/api/v1/"
        )
        owner = os.environ.get("LORE_FORGE_ACCEPTANCE_OWNER", "lore-test")
        repo = os.environ.get("LORE_FORGE_ACCEPTANCE_REPO", "lore-acceptance")
        parsed = urlsplit(api_url)
        self.assertEqual(
            parsed.hostname,
            "127.0.0.1",
            "acceptance server must be isolated on loopback",
        )
        self.assertNotEqual(
            parsed.port, 30001, "the existing Gitea instance must not be used"
        )
        remote = f"{parsed.scheme}://{parsed.netloc}/{owner}/{repo}.git"
        credentials = GitHttpCredentials(owner, token)
        api = ForgeAPI(api_url, token=token, max_retries=0)
        ForgePullPublisher(api, owner, repo).check_repository()

        with tempfile.TemporaryDirectory(prefix="lore-gitea-acceptance-") as temp_dir:
            environment = {
                key: value
                for key, value in os.environ.items()
                if not key.startswith("GIT_") and key != "LORE_FORGE_ACCEPTANCE_TOKEN"
            }
            environment.update(
                {
                    "HOME": temp_dir,
                    "GIT_CONFIG_GLOBAL": os.devnull,
                    "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_TERMINAL_PROMPT": "0",
                    **credentials.environment(remote),
                }
            )
            checkout = Path(temp_dir) / "checkout"

            def git(*arguments):
                return subprocess.run(
                    [
                        "git",
                        "-c",
                        "core.hooksPath=" + os.devnull,
                        "-c",
                        "commit.gpgsign=false",
                        "-c",
                        "user.name=Acceptance Test",
                        "-c",
                        "user.email=acceptance@example.test",
                        *arguments,
                    ],
                    check=True,
                    capture_output=True,
                    env=environment,
                ).stdout

            git("clone", "--", remote, str(checkout))
            base = (
                git("-C", str(checkout), "rev-parse", "refs/remotes/origin/main")
                .decode()
                .strip()
            )
            git("-C", str(checkout), "checkout", "-B", "acceptance-fixture", base)
            unique = uuid.uuid4().hex
            (checkout / f"acceptance-{unique}.txt").write_text(
                f"Acceptance fixture {unique}\n"
            )
            git("-C", str(checkout), "add", ".")
            git(
                "-C",
                str(checkout),
                "commit",
                "-qm",
                f"Add acceptance fixture {unique}",
                "-m",
                f"change-id: acceptance-{unique}",
            )
            mailbox_path = Path(temp_dir) / "series.mbox"
            mailbox_path.write_bytes(
                git(
                    "-C",
                    str(checkout),
                    "format-patch",
                    "--stdout",
                    "--numbered",
                    "--thread",
                    "--no-signature",
                    f"{base}..HEAD",
                )
            )
            (revision,) = discover_series(parse_mbox(mailbox_path))
            self.assertTrue(revision.complete)
            patch_message = revision.messages[0]
            assert patch_message.message_id is not None
            diff = git(
                "-C", str(checkout), "diff", "--no-ext-diff", f"{base}..HEAD"
            ).decode()
            quoted_diff = "\n".join(f"> {line}" for line in diff.splitlines())
            reply = parse_message(
                (
                    f"From: Reviewer <reviewer@example.test>\nMessage-ID: <reply-{unique}@example.test>\n"
                    f"Subject: Re: patch\nIn-Reply-To: <{patch_message.message_id}>\n"
                    "Received: from relay; Thu, 22 Oct 2026 15:30:00 +0200\n"
                    "Content-Type: text/plain; charset=utf-8\n\n"
                    f"{quoted_diff}\nPlease review this line.\n"
                ).encode()
            )
            archive = Path(temp_dir) / "archive"
            git("init", "-q", "--initial-branch=master", str(archive))
            for source in (patch_message, reply):
                (archive / "m").write_bytes(source.raw_message)
                git("-C", str(archive), "add", "m")
                git("-C", str(archive), "commit", "-qm", "archive message")
            ledger = Ledger(Path(temp_dir) / "ledger.sqlite3")
            self.assertEqual(
                sync_public_inbox(archive, ledger, limit=2).messages.ingested, 2
            )
            (revision,) = ledger.discover_related_series(patch_message.message_id)
            arguments = {
                "revision": revision,
                "repository": checkout,
                "base_ref": base,
                "remote": remote,
                "target_branch": "main",
                "credentials": credentials,
                "ledger": ledger,
            }
            preview = sync_series_revision(
                publisher=ForgePullPublisher(api, owner, repo),
                **arguments,
            )
            self.assertFalse(preview.applied)
            self.assertTrue(preview.branch.planned)
            self.assertTrue(preview.pull_request.planned)

            writer = ForgePullPublisher(api, owner, repo, write_enabled=True)
            published = sync_series_revision(
                publisher=writer,
                approved_digest=preview.approval_digest,
                **arguments,
            )
            self.assertTrue(published.branch.created)
            self.assertTrue(published.pull_request.created)
            replay = sync_series_revision(
                publisher=writer,
                approved_digest=preview.approval_digest,
                **arguments,
            )
            self.assertFalse(replay.branch.created)
            self.assertFalse(replay.pull_request.created)
            self.assertEqual(
                published.pull_request.remote_object["number"],
                replay.pull_request.remote_object["number"],
            )
            remote_main = git(
                "ls-remote", "--heads", remote, "refs/heads/main"
            ).decode()
            self.assertTrue(remote_main.startswith(base))

            pull_index = published.pull_request.remote_object["number"]
            comment_preview = ForgeCommentPublisher(api, owner, repo)
            with (
                patch.dict(os.environ, {"LORE_FORGE_TOKEN": token}),
                redirect_stdout(StringIO()),
            ):
                self.assertEqual(
                    main(
                        [
                            "map-identity",
                            "--ledger",
                            str(ledger.path),
                            "--api-url",
                            api_url,
                            "--email",
                            "reviewer@example.test",
                            "--username",
                            owner,
                            "--reason",
                            "acceptance test",
                        ]
                    ),
                    0,
                )
            queue = queue_archive_replies(
                ledger,
                comment_preview,
                pull_index,
                revision.series_key,
                revision.revision,
                b4_python=b4_python,
            )
            self.assertEqual(
                (
                    queue.matched,
                    queue.inline_count,
                    queue.unresolved_count,
                    queue.diagnostics,
                ),
                (1, 1, 0, ()),
            )
            self.assertIn(
                "Operator-mapped Gitea account (unverified sender)",
                ledger.pending()[0].payload["body"],
            )
            comment_writer = ForgeCommentPublisher(
                api,
                owner,
                repo,
                write_enabled=True,
                allow_inline_writes=True,
            )
            source_time = (
                "Source time (unverified Received header): 2026-10-22T13:30:00+00:00"
            )
            issue_path = f"{parsed.path.rstrip('/')}/repos/{owner}/{repo}/issues/{pull_index}/comments"
            transport = api.transport
            accepted = False
            hidden = False

            def lose_first_response(request, timeout):
                nonlocal accepted, hidden
                if urlsplit(request.full_url).path == issue_path:
                    if request.get_method() == "POST":
                        self.assertFalse(accepted)
                        response = transport(request, timeout)
                        try:
                            self.assertEqual(response.status, 201)
                            response.read()
                        finally:
                            response.close()
                        accepted = True
                        raise TimeoutError("simulated lost response")
                    if accepted and not hidden:
                        hidden = True
                        response = BytesIO(b"[]")
                        response.status = 200
                        response.headers = {}
                        return response
                return transport(request, timeout)

            plan = plan_comment_delivery(
                ledger, comment_writer, pull_index, "ISSUE_COMMENT"
            )
            self.assertEqual(len(plan.deliveries), 1)
            self.assertIn(source_time, plan.deliveries[0].payload["body"])
            with patch.object(api, "transport", side_effect=lose_first_response):
                uncertain = deliver_approved_comments(
                    ledger,
                    comment_writer,
                    pull_index,
                    "ISSUE_COMMENT",
                    approved_digest=plan.approval_digest(),
                )
            self.assertTrue(accepted and hidden)
            self.assertEqual((uncertain.delivered, uncertain.failed), (0, 1))
            self.assertEqual(ledger.pending()[0].write_state, "uncertain")

            worker_environment = {
                key: value
                for key, value in os.environ.items()
                if not key.startswith("GIT_")
            }
            worker_environment["LORE_FORGE_TOKEN"] = token
            worker = [
                sys.executable,
                "-m",
                "lore_review_companion",
                "deliver-comments",
                "--ledger",
                str(ledger.path),
                "--api-url",
                api_url,
                "--owner",
                owner,
                "--repo",
                repo,
                "--pull-index",
                str(pull_index),
                "--kind",
                "conversation",
            ]
            preview_worker = subprocess.run(
                worker,
                check=True,
                capture_output=True,
                text=True,
                env=worker_environment,
            )
            worker_plan = json.loads(preview_worker.stdout)
            self.assertIn(
                source_time, worker_plan["plan"]["deliveries"][0]["payload"]["body"]
            )
            resumed_worker = subprocess.run(
                [*worker, "--write", "--approve", worker_plan["approval_digest"]],
                check=True,
                capture_output=True,
                text=True,
                env=worker_environment,
            )
            worker_result = json.loads(resumed_worker.stdout)
            self.assertEqual(
                (worker_result["delivered"], worker_result["failed"]), (1, 0)
            )

            ledger = Ledger(ledger.path)
            api = ForgeAPI(api_url, token=token, max_retries=0)
            comment_preview = ForgeCommentPublisher(api, owner, repo)
            comment_writer = ForgeCommentPublisher(
                api, owner, repo, write_enabled=True, allow_inline_writes=True
            )
            plan = plan_comment_delivery(
                ledger, comment_writer, pull_index, "INLINE_COMMENT"
            )
            self.assertEqual(len(plan.deliveries), 1)
            self.assertIn(source_time, plan.deliveries[0].payload["body"])
            delivered = deliver_approved_comments(
                ledger,
                comment_writer,
                pull_index,
                "INLINE_COMMENT",
                approved_digest=plan.approval_digest(),
            )
            self.assertEqual((delivered.delivered, delivered.failed), (1, 0))

            def remote_reply_bodies():
                source_url = (
                    f"https://lore.kernel.org/xen-devel/reply-{unique}@example.test/"
                )
                issue_path = f"repos/{owner}/{repo}/issues/{pull_index}/comments"
                review_path = f"repos/{owner}/{repo}/pulls/{pull_index}/reviews"
                issue_bodies = [
                    comment["body"]
                    for comment in api.get_pages(issue_path)
                    if source_url in comment["body"]
                ]
                inline_bodies = [
                    comment["body"]
                    for review in api.get_pages(review_path)
                    for comment in api.get_pages(
                        f"{review_path}/{review['id']}/comments"
                    )
                    if source_url in comment["body"]
                ]
                return issue_bodies, inline_bodies

            original_bodies = remote_reply_bodies()
            for bodies in original_bodies:
                self.assertEqual(len(bodies), 1)
                self.assertIn(source_time, bodies[0])
            queue_archive_replies(
                ledger,
                comment_preview,
                pull_index,
                revision.series_key,
                revision.revision,
                b4_python=b4_python,
            )
            self.assertEqual(ledger.pending(), [])
            for event_type in ("ISSUE_COMMENT", "INLINE_COMMENT"):
                plan = plan_comment_delivery(
                    ledger, comment_writer, pull_index, event_type
                )
                self.assertEqual(plan.deliveries, ())
                delivered = deliver_approved_comments(
                    ledger,
                    comment_writer,
                    pull_index,
                    event_type,
                    approved_digest=plan.approval_digest(),
                )
                self.assertEqual(delivered.delivered, 0)
            self.assertEqual(remote_reply_bodies(), original_bodies)

            tracking = {
                "series": {"revision": revision.revision, "change-id": "acceptance"},
                "patches": [
                    {
                        "header-info": {"msgid": patch_message.message_id},
                        "reviews": {
                            "reviewer@example.test": {
                                "reply": "Local acceptance draft",
                                "comments": [
                                    {
                                        "path": f"acceptance-{unique}.txt",
                                        "line": 1,
                                        "content": f"+Acceptance fixture {unique}",
                                        "text": "Draft line review",
                                    }
                                ],
                            }
                        },
                    }
                ],
            }
            patch_commit = (
                git("-C", str(checkout), "rev-parse", "HEAD").decode().strip()
            )
            tracking_commit = (
                git(
                    "-C",
                    str(checkout),
                    "commit-tree",
                    f"{patch_commit}^{{tree}}",
                    "-p",
                    patch_commit,
                    "-m",
                    f"Cover\n\n--- b4-review-tracking ---\n{json.dumps(tracking)}",
                )
                .decode()
                .strip()
            )
            git(
                "-C",
                str(checkout),
                "update-ref",
                "refs/heads/b4/review/acceptance",
                tracking_commit,
            )
            draft_arguments = (
                ledger,
                comment_preview,
                pull_index,
                revision.series_key,
                revision.revision,
                checkout,
                "b4/review/acceptance",
                "reviewer@example.test",
            )
            draft = queue_b4_drafts(*draft_arguments, b4_python=b4_python)
            self.assertEqual(
                (
                    draft.tracking_commit,
                    draft.queued,
                    draft.inline_count,
                    draft.unresolved_count,
                ),
                (tracking_commit, 1, 1, 0),
            )
            for event_type in ("ISSUE_COMMENT", "INLINE_COMMENT"):
                plan = plan_comment_delivery(
                    ledger, comment_writer, pull_index, event_type
                )
                self.assertEqual(len(plan.deliveries), 1)
                delivered = deliver_approved_comments(
                    ledger,
                    comment_writer,
                    pull_index,
                    event_type,
                    approved_digest=plan.approval_digest(),
                )
                self.assertEqual(delivered.delivered, 1)
            queue_b4_drafts(*draft_arguments, b4_python=b4_python)
            self.assertEqual(ledger.pending(), [])
            self.assertEqual(
                git("-C", str(checkout), "rev-parse", "HEAD").decode().strip(),
                patch_commit,
            )

            git("-C", str(checkout), "checkout", "-B", "acceptance-v2", base)
            (checkout / f"acceptance-{unique}.txt").write_text(
                f"Acceptance revision 2 {unique}\n"
            )
            git("-C", str(checkout), "add", ".")
            git(
                "-C",
                str(checkout),
                "commit",
                "-qm",
                f"Update acceptance fixture {unique}",
                "-m",
                f"change-id: acceptance-{unique}",
            )
            second_mailbox = Path(temp_dir) / "revision-2.mbox"
            second_mailbox.write_bytes(
                git(
                    "-C",
                    str(checkout),
                    "format-patch",
                    "--stdout",
                    "--numbered",
                    "--thread",
                    "--subject-prefix=PATCH v2",
                    "--no-signature",
                    f"{base}..HEAD",
                )
            )
            (second_patch,) = parse_mbox(second_mailbox)
            assert second_patch.message_id is not None
            ledger.record_parsed_message(second_patch)
            (unlinked,) = ledger.discover_related_series(second_patch.message_id)
            self.assertNotEqual(unlinked.series_key, revision.series_key)
            link_arguments = (
                revision.series_key,
                1,
                second_patch.message_id,
                2,
                "acceptance operator",
                "reviewed change-ID",
            )
            link = ledger.plan_revision_link(*link_arguments)
            self.assertEqual(link.change_id, f"acceptance-{unique}")
            ledger.link_revision(
                *link_arguments, approved_digest=link.approval_digest()
            )
            (revision_two,) = ledger.discover_related_series(second_patch.message_id)
            self.assertEqual(revision_two.series_key, revision.series_key)
            arguments_two = {**arguments, "revision": revision_two}
            with self.assertRaises(PullRequestConflict):
                sync_series_revision(
                    publisher=ForgePullPublisher(api, owner, repo), **arguments_two
                )
            preview_two = sync_series_revision(
                publisher=ForgePullPublisher(api, owner, repo),
                revision_policy="separate",
                **arguments_two,
            )
            published_two = sync_series_revision(
                publisher=writer,
                revision_policy="separate",
                approved_digest=preview_two.approval_digest,
                **arguments_two,
            )
            self.assertNotEqual(
                published_two.pull_request.remote_object["number"], pull_index
            )
            original_pull = api.request_json(f"repos/{owner}/{repo}/pulls/{pull_index}")
            self.assertEqual(original_pull["head"]["sha"], published.branch.head_commit)
            self.assertEqual(original_pull["head"]["ref"], published.branch.branch)

            if os.environ.get("LORE_FORGE_ACCEPTANCE_DELETIONS") == "1":
                self.assertFalse(api.request_json("user")["is_admin"])
                source_url = (
                    f"https://lore.kernel.org/xen-devel/reply-{unique}@example.test/"
                )
                issue_comments = list(
                    api.get_pages(f"repos/{owner}/{repo}/issues/{pull_index}/comments")
                )
                (issue_comment,) = [
                    item for item in issue_comments if source_url in item["body"]
                ]
                reviews_path = f"repos/{owner}/{repo}/pulls/{pull_index}/reviews"
                matching_reviews = [
                    review
                    for review in api.get_pages(reviews_path)
                    if any(
                        source_url in item["body"]
                        for item in api.get_pages(
                            f"{reviews_path}/{review['id']}/comments"
                        )
                    )
                ]
                self.assertEqual(len(matching_reviews), 1)
                review_path = f"{reviews_path}/{matching_reviews[0]['id']}"
                comment_path = (
                    f"repos/{owner}/{repo}/issues/comments/{issue_comment['id']}"
                )
                self.assertIsNone(api.request_json(comment_path, method="DELETE"))
                self.assertIsNone(api.request_json(review_path, method="DELETE"))
                for path in (comment_path, review_path):
                    with (
                        self.subTest(path=path),
                        self.assertRaises(ForgeAPIError) as error,
                    ):
                        api.request_json(path)
                    self.assertEqual(error.exception.status, 404)

                second_index = published_two.pull_request.remote_object["number"]
                second_branch = published_two.branch.branch
                self.assertTrue(
                    second_branch.startswith("lore/") and second_branch.endswith("/v2")
                )
                branch_path = (
                    f"repos/{owner}/{repo}/branches/{quote(second_branch, safe='/')}"
                )
                self.assertIsNone(api.request_json(branch_path, method="DELETE"))
                self.assertEqual(
                    git("ls-remote", "--heads", remote, f"refs/heads/{second_branch}"),
                    b"",
                )
                self.assertTrue(
                    git("ls-remote", "--heads", remote, "refs/heads/main")
                    .decode()
                    .startswith(base)
                )
                self.assertIsNone(
                    api.request_json(
                        f"repos/{owner}/{repo}/issues/{second_index}", method="DELETE"
                    )
                )
                with self.assertRaises(ForgeAPIError) as error:
                    api.request_json(f"repos/{owner}/{repo}/pulls/{second_index}")
                self.assertEqual(error.exception.status, 404)


@unittest.skipUnless(
    os.environ.get("LORE_FORGEJO_IMPORT_FIXTURE"),
    "requires the isolated Forgejo integration fixture",
)
class ImportedWorkflowAcceptanceTests(unittest.TestCase):
    def test_two_patch_publication_and_replay_use_real_scoped_endpoints(self):
        config_path = Path(os.environ["LORE_FORGEJO_IMPORT_FIXTURE"])
        self.assertEqual(config_path.stat().st_mode & 0o777, 0o600)
        configuration = json.loads(config_path.read_text())
        self.assertEqual(configuration["fixture"], "isolated-forgejo-integration-v1")
        api_url = configuration["api_url"]
        parsed = urlsplit(api_url)
        self.assertIn(parsed.hostname, ("127.0.0.1", "localhost", "::1"))
        self.assertEqual(parsed.port, 3003)
        owner, repo = "user2", "repo1"
        remote = f"{parsed.scheme}://{parsed.netloc}/{owner}/{repo}.git"
        credentials = GitHttpCredentials(owner, configuration["git_token"])
        api = ForgeAPI(api_url, token=configuration["import_token"], max_retries=0)
        reader = ForgeImportClient(
            api, owner, repo, source_url="https://lore.kernel.org/xen-devel/"
        )
        writer = ForgeImportClient(
            api, owner, repo, source_url=reader.source_url, write_enabled=True
        )
        with tempfile.TemporaryDirectory(prefix="lore-import-acceptance-") as temporary:
            environment = {
                key: value
                for key, value in os.environ.items()
                if not key.startswith("GIT_")
            }
            environment.update(
                {
                    "HOME": temporary,
                    "GIT_CONFIG_GLOBAL": os.devnull,
                    "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_TERMINAL_PROMPT": "0",
                    **credentials.environment(remote),
                }
            )

            def git(*arguments):
                return subprocess.run(
                    [
                        "git",
                        "-c",
                        "core.hooksPath=" + os.devnull,
                        "-c",
                        "commit.gpgsign=false",
                        "-c",
                        "user.name=Acceptance Author",
                        "-c",
                        "user.email=author@example.test",
                        *arguments,
                    ],
                    check=True,
                    capture_output=True,
                    env=environment,
                ).stdout

            checkout = Path(temporary) / "checkout"
            git("clone", "--", remote, str(checkout))
            base = configuration["base_commit"]
            git("-C", str(checkout), "checkout", "-B", "acceptance-patches", base)
            (checkout / "review-fixture.c").write_text(
                "new 1\nnew 2\nnew 3\ncontext 4\ncontext 5\ncontext 6\n"
                "context 7\ncontext 8\ncontext 9\ncontext 10\ncontext 11\n"
                "context 12\ncontext 13\nnew tail\n"
            )
            git("-C", str(checkout), "add", ".")
            git("-C", str(checkout), "commit", "-q", "-s", "-m", "First acceptance patch")
            (checkout / "second-fixture.txt").write_text("second patch\n")
            git("-C", str(checkout), "add", ".")
            git("-C", str(checkout), "commit", "-q", "-s", "-m", "Second acceptance patch")
            mailbox_path = Path(temporary) / "series.mbox"
            mailbox_path.write_bytes(
                git(
                    "-C",
                    str(checkout),
                    "format-patch",
                    "--stdout",
                    "--numbered",
                    "--thread",
                    "--no-signature",
                    f"{base}..HEAD",
                )
            )
            (revision,) = discover_series(parse_mbox(mailbox_path))
            self.assertTrue(revision.complete)
            self.assertEqual(revision.expected_patch_count, 2)
            ledger = Ledger(Path(temporary) / "mail.sqlite3")
            for message in revision.messages:
                ledger.record_parsed_message(message)
            arguments = {
                "revision": revision,
                "repository": checkout,
                "base_ref": base,
                "remote": remote,
                "target_branch": "branch2",
                "ledger": ledger,
                "credentials": credentials,
            }
            preview = sync_imported_series_revision(client=reader, **arguments)
            self.assertFalse(preview.applied)
            self.assertEqual(len(preview.plan.patch_commits), 2)
            published = sync_imported_series_revision(
                client=writer, approved_digest=preview.approval_digest, **arguments
            )
            replay = sync_imported_series_revision(
                client=writer, approved_digest=preview.approval_digest, **arguments
            )
            self.assertEqual(
                published.pull_request.remote_object, replay.pull_request.remote_object
            )
            self.assertEqual(published.plan.base_commit, base)
            self.assertTrue(
                git("ls-remote", "--heads", remote, "refs/heads/branch2")
                .decode()
                .startswith(base)
            )
            pull_index = published.pull_request.remote_object["pull_index"]
            first_patch = next(
                message for message in revision.messages if message.patch_index == 1
            )
            root = parse_message(
                (
                    "From: Reviewer <reviewer@example.test>\nMessage-ID: <acceptance-review@example.test>\nSubject: Re: first\n"
                    f"In-Reply-To: <{first_patch.message_id}>\nReceived: from relay; Thu, 22 Oct 2026 15:30:00 +0200\n\n"
                    "> ---\nCommit description feedback.\n\n"
                    "> --- a/review-fixture.c\n> +++ b/review-fixture.c\n> -old 1\n> -old 2\n> -old 3\n"
                    "Please keep the removed context.\nIt explains the old behavior.\n\n"
                    "> --- a/review-fixture.c\n> +++ b/review-fixture.c\n> +new 1\n> +new 2\n> +new 3\n"
                    "Please clarify the new context.\nThe name needs an explanation.\n\n~Reviewer\n"
                ).encode()
            )
            assert root.message_id is not None
            ledger.record_parsed_message(root)
            queued = queue_imported_replies(
                ledger,
                reader,
                pull_index,
                revision.series_key,
                revision.revision,
                checkout,
                (root,),
            )
            self.assertEqual(
                (queued.inline_count, queued.unresolved_count, queued.standalone_count),
                (2, 0, 1),
            )
            review_plan = plan_import_delivery(
                ledger, writer, pull_index, "IMPORT_REVIEW"
            )
            review_request = review_plan.deliveries[0].payload["request"]
            self.assertEqual(
                [
                    (
                        comment["old_position"],
                        comment["new_position"],
                        comment["extra_lines_count"],
                    )
                    for comment in review_request["comments"]
                ],
                [(1, 0, 2), (0, 1, 2)],
            )
            self.assertNotEqual(
                review_request["comments"][0]["after_commit"],
                review_request["head_commit"],
            )
            with patch.object(writer, "import_review", side_effect=ForgeAPIError(422, "synthetic rejected review")):
                failed = deliver_approved_imports(
                    ledger, writer, pull_index, "IMPORT_REVIEW",
                    approved_digest=review_plan.approval_digest(),
                )
            self.assertEqual((failed.delivered, failed.failed), (0, 1))
            reports = [
                comment for comment in ForgeAPI(api_url).request_json(
                    f"repos/{owner}/{repo}/issues/{pull_index}/comments"
                )
                if "Companion import failure report" in comment.get("body", "")
            ]
            self.assertEqual(len(reports), 1)
            self.assertEqual(reports[0]["user"]["login"], "user2")
            self.assertIsNone(reports[0].get("review_import"))
            self.assertIn("synthetic rejected review", reports[0]["body"])
            self.assertIn(root.message_id, reports[0]["body"])
            review_plan = plan_import_delivery(
                ledger, writer, pull_index, "IMPORT_REVIEW", retry_uncertain=True
            )
            transport = api.transport
            accepted_requests = []

            def lose_accepted_response(request, timeout):
                response = transport(request, timeout)
                if request.get_method() == "POST" and urlsplit(
                    request.full_url
                ).path.endswith("/reviews"):
                    accepted_requests.append(json.loads(request.data))
                    if len(accepted_requests) == 1:
                        response.read()
                        response.close()
                        raise TimeoutError(
                            "synthetic response loss after server acceptance"
                        )
                return response

            with patch.object(api, "transport", side_effect=lose_accepted_response):
                outcome = deliver_approved_imports(
                    ledger,
                    writer,
                    pull_index,
                    "IMPORT_REVIEW",
                    approved_digest=review_plan.approval_digest(),
                    retry_uncertain=True,
                )
            self.assertEqual((outcome.delivered, outcome.failed), (1, 0))
            self.assertEqual(len(accepted_requests), 2)
            self.assertEqual(*accepted_requests)
            conversation_plan = plan_import_delivery(
                ledger, writer, pull_index, "IMPORT_CONVERSATION"
            )
            conversation_body = next(
                effect.payload["request"]["body"]
                for effect in conversation_plan.deliveries
                if effect.effect_key == "imported-full-message-v1"
            )
            standalone = next(
                effect for effect in conversation_plan.deliveries
                if effect.effect_key.startswith("quoted-remark-standalone-v1:")
            )
            self.assertEqual(standalone.payload["request"]["body"], "Commit description feedback.")
            self.assertIn(
                "```rust\n> ---\n```\nCommit description feedback.", conversation_body
            )
            self.assertIn(
                "```c\n--- a/review-fixture.c\n+++ b/review-fixture.c",
                conversation_body,
            )
            self.assertNotIn("~Reviewer", conversation_body)
            self.assertEqual(
                deliver_approved_imports(
                    ledger,
                    writer,
                    pull_index,
                    "IMPORT_CONVERSATION",
                    approved_digest=conversation_plan.approval_digest(),
                ).delivered,
                2,
            )
            standalone_remote = reader.lookup_operation(standalone.payload["request"]["operation_key"])
            self.assertEqual(standalone_remote["type"], "conversation")
            self.assertEqual(standalone_remote["message_id"], root.message_id)
            self.assertGreater(standalone_remote["comment_id"], 0)
            ordinary_root = parse_message(
                (
                    "From: Reviewer <reviewer@example.test>\nMessage-ID: <ordinary-root@example.test>\n"
                    "Subject: Re: first\n"
                    f"In-Reply-To: <{first_patch.message_id}>\n\n"
                    "This is a general concern about the change.\n"
                ).encode()
            )
            ledger.record_parsed_message(ordinary_root)
            queue_imported_replies(
                ledger,
                reader,
                pull_index,
                revision.series_key,
                revision.revision,
                checkout,
                (ordinary_root,),
            )
            ordinary_plan = plan_import_delivery(
                ledger, writer, pull_index, "IMPORT_CONVERSATION"
            )
            self.assertEqual(len(ordinary_plan.deliveries), 1)
            root_request = ordinary_plan.deliveries[0].payload["request"]
            self.assertEqual(
                deliver_approved_imports(
                    ledger,
                    writer,
                    pull_index,
                    "IMPORT_CONVERSATION",
                    approved_digest=ordinary_plan.approval_digest(),
                ).delivered,
                1,
            )
            root_comment = reader.lookup_operation(root_request["operation_key"])[
                "comment_id"
            ]
            parent_message, parent_comment = ordinary_root, root_comment
            ordinary_replies = []
            for message_id, quote, answer in (
                (
                    "ordinary-child@example.test",
                    "This is a general concern about the change.",
                    "I agree with that concern.",
                ),
                (
                    "ordinary-grandchild@example.test",
                    "I agree with that concern.",
                    "I will address it in the next revision.",
                ),
            ):
                child = parse_message(
                    (
                        f"From: Another Reviewer <another@example.test>\nMessage-ID: <{message_id}>\n"
                        f"Subject: Re: first\nIn-Reply-To: <{parent_message.message_id}>\n\n"
                        f"> {quote}\n{answer}\n"
                    ).encode()
                )
                ledger.record_parsed_message(child)
                queued_child = queue_imported_replies(
                    ledger,
                    reader,
                    pull_index,
                    revision.series_key,
                    revision.revision,
                    checkout,
                    (child,),
                )
                self.assertEqual(
                    (queued_child.inline_count, queued_child.unresolved_count), (1, 0)
                )
                child_plan = plan_import_delivery(
                    ledger, writer, pull_index, "IMPORT_REPLY"
                )
                self.assertEqual(len(child_plan.deliveries), 1)
                child_request = child_plan.deliveries[0].payload["request"]
                self.assertEqual(child_request["parent_id"], parent_comment)
                self.assertEqual(
                    child_plan.deliveries[0].payload["evidence"]["target_kind"],
                    "conversation",
                )
                self.assertEqual(
                    deliver_approved_imports(
                        ledger,
                        writer,
                        pull_index,
                        "IMPORT_REPLY",
                        approved_digest=child_plan.approval_digest(),
                    ).delivered,
                    1,
                )
                accepted_child = writer.import_conversation_reply(
                    pull_index, child_request
                ).remote_object
                self.assertEqual(accepted_child["review_id"], 0)
                ordinary_replies.append((accepted_child["comment_id"], parent_comment))
                queue_imported_replies(
                    ledger,
                    reader,
                    pull_index,
                    revision.series_key,
                    revision.revision,
                    checkout,
                    (child,),
                )
                self.assertEqual(
                    plan_import_delivery(
                        ledger, writer, pull_index, "IMPORT_REPLY"
                    ).deliveries,
                    (),
                )
                parent_message, parent_comment = child, accepted_child["comment_id"]
            ordinary = ForgeAPI(api_url)
            issue_comments = ordinary.request_json(
                f"repos/{owner}/{repo}/issues/{pull_index}/comments"
            )
            for comment_id, parent_id in ordinary_replies:
                comment = next(
                    item for item in issue_comments if item["id"] == comment_id
                )
                self.assertEqual(comment["discussion_parent_id"], parent_id)
            (root_effect,) = [
                effect
                for effect in ledger.comment_effects(
                    root.message_id,
                    f"{api.base_url}{reader.repository_path}#{pull_index}",
                )
                if effect.event_type == "IMPORT_REVIEW"
            ]
            accepted_root = reader.lookup_operation(
                root_effect.payload["request"]["operation_key"]
            )
            ordinary = ForgeAPI(api_url)
            reviews_path = f"repos/{owner}/{repo}/pulls/{pull_index}/reviews"
            reviews = ordinary.request_json(reviews_path)
            self.assertEqual(len(reviews), 1)
            self.assertFalse(reviews[0]["official"])
            self.assertEqual(reviews[0]["state"], "COMMENT")
            comments = ordinary.request_json(
                f"{reviews_path}/{accepted_root['review_id']}/comments"
            )
            self.assertEqual(len(comments), 2)
            for comment in comments:
                self.assertEqual(comment["review_import"]["importer_login"], "user2")
                self.assertEqual(
                    comment["review_import"]["participant_name"], "Reviewer"
                )
                self.assertEqual(
                    comment["review_import"]["source_time"], "2026-10-22T13:30:00Z"
                )
                self.assertEqual(comment["extra_lines_count"], 2)
            response = parse_message(
                b"From: Second Reviewer <second@example.test>\nMessage-ID: <acceptance-response@example.test>\nSubject: Re: first\n"
                b"In-Reply-To: <acceptance-review@example.test>\n\n"
                b">> -old 1\n> Please keep the removed context. \nAgreed with that context.\n\n"
                b"> The name needs an explanation.\nI will clarify the name.\n"
            )
            ledger.record_parsed_message(response)
            queued = queue_imported_replies(
                ledger,
                reader,
                pull_index,
                revision.series_key,
                revision.revision,
                checkout,
                (response,),
            )
            self.assertEqual((queued.inline_count, queued.unresolved_count), (2, 0))
            reply_plan = plan_import_delivery(
                ledger, writer, pull_index, "IMPORT_REPLY"
            )
            self.assertEqual(
                [
                    item.payload["request"]["parent_id"]
                    for item in reply_plan.deliveries
                ],
                accepted_root["code_comment_ids"],
            )
            self.assertEqual(
                deliver_approved_imports(
                    ledger,
                    writer,
                    pull_index,
                    "IMPORT_REPLY",
                    approved_digest=reply_plan.approval_digest(),
                ).delivered,
                2,
            )
            comments = ordinary.request_json(
                f"{reviews_path}/{accepted_root['review_id']}/comments"
            )
            self.assertEqual(len(comments), 4)
            (imported_response,) = [
                comment
                for comment in comments
                if comment["body"]
                == reply_plan.deliveries[0].payload["request"]["body"]
            ]
            self.assertNotEqual(
                accepted_root["participant_id"], imported_response["user"]["id"]
            )
            self.assertEqual(
                imported_response["review_import"]["participant_name"],
                "Second Reviewer",
            )
            same_reviewer = parse_message(
                (
                    "From: Reviewer <reviewer@example.test>\nMessage-ID: <acceptance-repeat-review@example.test>\nSubject: Re: first\n"
                    f"In-Reply-To: <{first_patch.message_id}>\n\n> --- a/review-fixture.c\n> +++ b/review-fixture.c\n"
                    "> +new 2\n> +new 3\nA second independent remark.\n"
                ).encode()
            )
            ledger.record_parsed_message(same_reviewer)
            queued = queue_imported_replies(
                ledger,
                reader,
                pull_index,
                revision.series_key,
                revision.revision,
                checkout,
                (same_reviewer,),
            )
            self.assertEqual(queued.inline_count, 1)
            plan = plan_import_delivery(ledger, writer, pull_index, "IMPORT_REVIEW")
            self.assertEqual(
                deliver_approved_imports(
                    ledger,
                    writer,
                    pull_index,
                    "IMPORT_REVIEW",
                    approved_digest=plan.approval_digest(),
                ).delivered,
                1,
            )
            self.assertEqual(len(ordinary.request_json(reviews_path)), 2)
            queue_imported_replies(
                ledger,
                reader,
                pull_index,
                revision.series_key,
                revision.revision,
                checkout,
                (root, response, same_reviewer),
            )
            self.assertEqual(
                plan_import_delivery(
                    ledger, writer, pull_index, "IMPORT_REVIEW"
                ).deliveries,
                (),
            )
            self.assertEqual(
                plan_import_delivery(
                    ledger, writer, pull_index, "IMPORT_REPLY"
                ).deliveries,
                (),
            )
            exact_patch = reader.comparison(
                pull_index, after_commit=published.plan.patch_commits[0].after_commit
            )["diff"]
            hunk_headers = [
                line for line in exact_patch.splitlines() if line.startswith("@@ ")
            ]
            self.assertEqual(len(hunk_headers), 2)
            segmented = parse_message(
                (
                    "From: Reviewer <reviewer@example.test>\nMessage-ID: <acceptance-segmented@example.test>\nSubject: Re: first\n"
                    f"In-Reply-To: <{first_patch.message_id}>\n\n"
                    "> --- a/review-fixture.c\n> +++ b/review-fixture.c\n> +new 3\n"
                    f"> {hunk_headers[1]}\n> +new tail\nPlease adjust the tail.\n"
                ).encode()
            )
            ledger.record_parsed_message(segmented)
            queued = queue_imported_replies(
                ledger,
                reader,
                pull_index,
                revision.series_key,
                revision.revision,
                checkout,
                (segmented,),
            )
            self.assertEqual((queued.inline_count, queued.unresolved_count), (1, 0))
            plan = plan_import_delivery(ledger, writer, pull_index, "IMPORT_REVIEW")
            self.assertEqual(len(plan.deliveries), 1)
            self.assertEqual(
                plan.deliveries[0].payload["request"]["comments"][0]["body"],
                "Please adjust the tail.",
            )
            self.assertEqual(
                plan.deliveries[0].payload["request"]["comments"][0]["new_position"], 14
            )
            self.assertEqual(
                plan.deliveries[0].payload["request"]["comments"][0][
                    "extra_lines_count"
                ],
                0,
            )
            self.assertEqual(
                deliver_approved_imports(
                    ledger,
                    writer,
                    pull_index,
                    "IMPORT_REVIEW",
                    approved_digest=plan.approval_digest(),
                ).delivered,
                1,
            )
            segment_result = reader.lookup_operation(
                plan.deliveries[0].payload["request"]["operation_key"]
            )
            segment_comments = ordinary.request_json(
                f"{reviews_path}/{segment_result['review_id']}/comments"
            )
            self.assertEqual(
                [comment["body"] for comment in segment_comments],
                ["Please adjust the tail."],
            )
            queue_imported_replies(
                ledger,
                reader,
                pull_index,
                revision.series_key,
                revision.revision,
                checkout,
                (segmented,),
            )
            self.assertEqual(
                plan_import_delivery(
                    ledger, writer, pull_index, "IMPORT_REVIEW"
                ).deliveries,
                (),
            )
            original_pull = ordinary.request_json(
                f"repos/{owner}/{repo}/pulls/{pull_index}"
            )
            original_refs = git(
                "ls-remote",
                "--heads",
                remote,
                f"refs/heads/{published.plan.head_branch}",
                f"refs/heads/{published.plan.base_branch}",
            )
            git("-C", str(checkout), "checkout", "-B", "acceptance-v2", base)
            (checkout / "review-fixture.c").write_text(
                "revision two 1\nrevision two 2\nrevision two 3\n"
            )
            git("-C", str(checkout), "add", ".")
            git("-C", str(checkout), "commit", "-q", "-s", "-m", "First acceptance patch, revised")
            (checkout / "second-fixture.txt").write_text("second revision patch\n")
            git("-C", str(checkout), "add", ".")
            git(
                "-C", str(checkout), "commit", "-q", "-s", "-m", "Second acceptance patch, revised"
            )
            mailbox_path.write_bytes(
                git(
                    "-C",
                    str(checkout),
                    "format-patch",
                    "--stdout",
                    "--numbered",
                    "--thread",
                    "--no-signature",
                    "-v2",
                    f"{base}..HEAD",
                )
            )
            (second_revision,) = discover_series(parse_mbox(mailbox_path))
            assert second_revision.messages[0].message_id is not None
            for message in second_revision.messages:
                ledger.record_parsed_message(message)
            linking = [
                "link-revision",
                "--ledger",
                str(ledger.path),
                "--series-key",
                revision.series_key,
                "--canonical-revision",
                "1",
                "--candidate-seed",
                second_revision.messages[0].message_id,
                "--candidate-revision",
                "2",
                "--actor",
                "acceptance operator",
                "--reason",
                "Synthetic explicit revision relationship",
                "--allow-unmatched-change-id",
            ]
            with redirect_stdout(StringIO()) as output:
                self.assertEqual(main(linking), 0)
            link_digest = json.loads(output.getvalue())["approval_digest"]
            with redirect_stdout(StringIO()):
                self.assertEqual(
                    main([*linking, "--write", "--approve", link_digest]), 0
                )
            (second_revision,) = [
                current
                for current in ledger.discover_related_series(
                    second_revision.messages[0].message_id
                )
                if current.revision == 2
            ]
            second_arguments = {**arguments, "revision": second_revision}
            second_preview = sync_imported_series_revision(
                client=reader, **second_arguments
            )
            self.assertIn(f"/pulls/{pull_index}", second_preview.plan.request["body"])
            second_published = sync_imported_series_revision(
                client=writer,
                approved_digest=second_preview.approval_digest,
                **second_arguments,
            )
            second_index = second_published.pull_request.remote_object["pull_index"]
            self.assertNotEqual(second_index, pull_index)
            self.assertNotEqual(
                second_published.plan.head_branch, published.plan.head_branch
            )
            self.assertEqual(
                ordinary.request_json(f"repos/{owner}/{repo}/pulls/{pull_index}")[
                    "body"
                ],
                original_pull["body"],
            )
            self.assertEqual(
                git(
                    "ls-remote",
                    "--heads",
                    remote,
                    f"refs/heads/{published.plan.head_branch}",
                    f"refs/heads/{published.plan.base_branch}",
                ),
                original_refs,
            )
            late = parse_message(
                b"From: Late Reviewer <late@example.test>\nMessage-ID: <acceptance-late@example.test>\nSubject: Re: first\n"
                b"In-Reply-To: <acceptance-response@example.test>\n\n> Agreed with that context.\nA late reply to revision one.\n"
            )
            ledger.record_parsed_message(late)
            queued = queue_imported_replies(
                ledger, reader, pull_index, revision.series_key, 1, checkout, (late,)
            )
            self.assertEqual((queued.inline_count, queued.unresolved_count), (1, 0))
            plan = plan_import_delivery(ledger, writer, pull_index, "IMPORT_REPLY")
            self.assertEqual(plan.deliveries[0].payload["thread"]["revision"], 1)
            self.assertEqual(
                plan.deliveries[0].payload["request"]["parent_id"],
                imported_response["id"],
            )
            self.assertEqual(
                deliver_approved_imports(
                    ledger,
                    writer,
                    pull_index,
                    "IMPORT_REPLY",
                    approved_digest=plan.approval_digest(),
                ).delivered,
                1,
            )
            self.assertEqual(
                ordinary.request_json(
                    f"repos/{owner}/{repo}/pulls/{second_index}/reviews"
                ),
                [],
            )
            conflicting = {
                **root_effect.payload["request"],
                "operation_key": "source-conflict",
                "message_sha256": "f" * 64,
            }
            with self.assertRaises(ForgeAPIError) as conflict:
                writer.import_review(pull_index, conflicting)
            self.assertEqual(conflict.exception.status, 409)
            self.assertEqual(len(ordinary.request_json(reviews_path)), 3)
            manual_pending = {effect.id for effect in ledger.pending()}
            worker_message = parse_message(
                (
                    "From: Scheduled Reviewer <scheduled@example.test>\nMessage-ID: <acceptance-worker@example.test>\nSubject: Re: second revision\n"
                    f"In-Reply-To: <{second_revision.messages[0].message_id}>\n\n"
                    "> --- a/review-fixture.c\n> +++ b/review-fixture.c\n> +revision two 1\n> +revision two 2\n"
                    "Worker-created exact remark.\n"
                ).encode()
            )
            ledger.record_parsed_message(worker_message)
            capabilities = reader.capabilities()
            policy = {
                "version": 1,
                "enabled": True,
                "expires_at": "2099-01-01T00:00:00+00:00",
                "api_url": api_url,
                "owner": owner,
                "repository": repo,
                "repository_id": capabilities["repository_id"],
                "importer_id": capabilities["importer_id"],
                "importer_login": capabilities["importer_login"],
                "source_url": reader.source_url,
                "nntp_host": "nntp.lore.kernel.org",
                "nntp_group": "org.xenproject.lists.xen-devel",
                "git_remote": remote,
                "git_username": owner,
                "base_commit": base,
                "target_branch": "branch2",
                "max_inline_lines": 50,
                "lookback_hours": 148,
                "notifications": "disabled",
                "identity_rule": "isolated-unverified",
                "retry_uncertain": False,
            }
            policy_path = Path(temporary) / "policy.json"
            policy_path.write_text(json.dumps(policy))
            policy_path.chmod(0o600)
            state_path = Path(temporary) / "worker.sqlite3"
            worker_round = run_import_worker_round(
                ledger,
                checkout,
                policy_path,
                state_path,
                api,
                credentials,
                refresh_source=False,
            )
            self.assertEqual(worker_round.published, 0)
            self.assertEqual(worker_round.delivered, 2, worker_round)
            self.assertEqual({effect.id for effect in ledger.pending()}, manual_pending)
            replay_round = run_import_worker_round(
                ledger,
                checkout,
                policy_path,
                state_path,
                api,
                credentials,
                refresh_source=False,
            )
            self.assertEqual((replay_round.published, replay_round.delivered), (0, 0))
            second_reviews = ordinary.request_json(
                f"repos/{owner}/{repo}/pulls/{second_index}/reviews"
            )
            self.assertEqual(len(second_reviews), 1)
            self.assertEqual(
                second_reviews[0]["review_import"]["participant_name"],
                "Scheduled Reviewer",
            )
            refresh = plan_import_refresh(
                ledger,
                reader,
                second_index,
                second_revision,
                second_published.plan.request,
                reason="Exercise complete retained-source presentation rebuild",
            )
            self.assertEqual(len(refresh["request"]["effects"]), 2)
            self.assertEqual(len(refresh["request"]["comments"]), 3)
            for _ in range(2):
                refreshed = apply_import_refresh(
                    ledger, writer, refresh, approved_digest=refresh_digest(refresh)
                )
                self.assertEqual(refreshed["pull_index"], second_index)
            self.assertEqual(
                writer.import_pull(second_published.plan.request).remote_object[
                    "pull_index"
                ],
                second_index,
            )
            self.assertEqual({effect.id for effect in ledger.pending()}, manual_pending)
            self.assertEqual(
                git(
                    "ls-remote",
                    "--heads",
                    remote,
                    f"refs/heads/{published.plan.head_branch}",
                    f"refs/heads/{published.plan.base_branch}",
                ),
                original_refs,
            )
            unavailable = parse_message(
                (
                    "From: Unavailable Reviewer <unavailable@example.test>\nMessage-ID: <acceptance-unavailable@example.test>\nSubject: Re: first\n"
                    f"References: <{first_patch.message_id}> <unavailable-parent@example.test>\n"
                    "In-Reply-To: <unavailable-parent@example.test>\n\nRetain this reply until its parent can be verified.\n"
                ).encode()
            )
            ledger.record_parsed_message(unavailable)
            before_effects = ledger.pending()
            with self.assertRaises(PublicationConflict):
                queue_imported_replies(
                    ledger,
                    reader,
                    pull_index,
                    revision.series_key,
                    1,
                    checkout,
                    (unavailable,),
                )
            self.assertEqual(ledger.pending(), before_effects)
            self.assertTrue(
                git("ls-remote", "--heads", remote, "refs/heads/branch2")
                .decode()
                .startswith(base)
            )


if __name__ == "__main__":
    unittest.main()
