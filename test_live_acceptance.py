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

from cli import main
from git_branches import GitHttpCredentials
from forge_api import ForgeAPI, ForgeAPIError
from forge_comments import ForgeCommentPublisher
from forge_pulls import ForgePullPublisher, PullRequestConflict
from ingest import sync_public_inbox
from ledger import Ledger
from mail_parser import parse_mbox, parse_message
from outbox import deliver_approved_comments, plan_comment_delivery
from review_queue import queue_archive_replies, queue_b4_drafts
from series import discover_series
from series_sync import sync_series_revision


@unittest.skipUnless(os.environ.get("LORE_GITEA_ACCEPTANCE") == "1", "requires explicit disposable-server opt-in")
class LiveAcceptanceTests(unittest.TestCase):
    def test_preview_publish_and_replay_on_disposable_gitea(self):
        token = os.environ.get("LORE_GITEA_ACCEPTANCE_TOKEN")
        self.assertTrue(token, "LORE_GITEA_ACCEPTANCE_TOKEN is required")
        b4_python = os.environ.get("LORE_GITEA_ACCEPTANCE_B4_PYTHON")
        self.assertTrue(b4_python, "LORE_GITEA_ACCEPTANCE_B4_PYTHON is required")
        api_url = os.environ.get("LORE_GITEA_ACCEPTANCE_API", "http://127.0.0.1:30002/api/v1/")
        owner = os.environ.get("LORE_GITEA_ACCEPTANCE_OWNER", "lore-test")
        repo = os.environ.get("LORE_GITEA_ACCEPTANCE_REPO", "lore-acceptance")
        parsed = urlsplit(api_url)
        self.assertEqual(parsed.hostname, "127.0.0.1", "acceptance server must be isolated on loopback")
        self.assertNotEqual(parsed.port, 30001, "the existing Gitea instance must not be used")
        remote = f"{parsed.scheme}://{parsed.netloc}/{owner}/{repo}.git"
        credentials = GitHttpCredentials(owner, token)
        api = ForgeAPI(api_url, token=token, max_retries=0)
        ForgePullPublisher(api, owner, repo).check_repository()

        with tempfile.TemporaryDirectory(prefix="lore-gitea-acceptance-") as temp_dir:
            environment = {
                key: value for key, value in os.environ.items()
                if not key.startswith("GIT_") and key != "LORE_GITEA_ACCEPTANCE_TOKEN"
            }
            environment.update({
                "HOME": temp_dir, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_TERMINAL_PROMPT": "0", **credentials.environment(remote),
            })
            checkout = Path(temp_dir) / "checkout"

            def git(*arguments):
                return subprocess.run(
                    ["git", "-c", "core.hooksPath=" + os.devnull, "-c", "commit.gpgsign=false",
                     "-c", "user.name=Acceptance Test", "-c", "user.email=acceptance@example.test",
                     *arguments],
                    check=True, capture_output=True, env=environment,
                ).stdout

            git("clone", "--", remote, str(checkout))
            base = git("-C", str(checkout), "rev-parse", "refs/remotes/origin/main").decode().strip()
            git("-C", str(checkout), "checkout", "-B", "acceptance-fixture", base)
            unique = uuid.uuid4().hex
            (checkout / f"acceptance-{unique}.txt").write_text(f"Acceptance fixture {unique}\n")
            git("-C", str(checkout), "add", ".")
            git("-C", str(checkout), "commit", "-qm", f"Add acceptance fixture {unique}", "-m", f"change-id: acceptance-{unique}")
            mailbox_path = Path(temp_dir) / "series.mbox"
            mailbox_path.write_bytes(git(
                "-C", str(checkout), "format-patch", "--stdout", "--numbered", "--thread",
                "--no-signature", f"{base}..HEAD",
            ))
            (revision,) = discover_series(parse_mbox(mailbox_path))
            self.assertTrue(revision.complete)
            patch_message = revision.messages[0]
            diff = git("-C", str(checkout), "diff", "--no-ext-diff", f"{base}..HEAD").decode()
            quoted_diff = "\n".join(f"> {line}" for line in diff.splitlines())
            reply = parse_message((
                f"From: Reviewer <reviewer@example.test>\nMessage-ID: <reply-{unique}@example.test>\n"
                f"Subject: Re: patch\nIn-Reply-To: <{patch_message.message_id}>\n"
                "Received: from relay; Thu, 22 Oct 2026 15:30:00 +0200\n"
                "Content-Type: text/plain; charset=utf-8\n\n"
                f"{quoted_diff}\nPlease review this line.\n"
            ).encode())
            archive = Path(temp_dir) / "archive"
            git("init", "-q", "--initial-branch=master", str(archive))
            for source in (patch_message, reply):
                (archive / "m").write_bytes(source.raw_message)
                git("-C", str(archive), "add", "m")
                git("-C", str(archive), "commit", "-qm", "archive message")
            ledger = Ledger(Path(temp_dir) / "ledger.sqlite3")
            self.assertEqual(sync_public_inbox(archive, ledger, limit=2).messages.ingested, 2)
            (revision,) = ledger.discover_related_series(patch_message.message_id)
            arguments = {
                "revision": revision, "repository": checkout, "base_ref": base,
                "remote": remote, "target_branch": "main", "credentials": credentials,
                "ledger": ledger,
            }
            preview = sync_series_revision(
                publisher=ForgePullPublisher(api, owner, repo), **arguments,
            )
            self.assertFalse(preview.applied)
            self.assertTrue(preview.branch.planned)
            self.assertTrue(preview.pull_request.planned)

            writer = ForgePullPublisher(api, owner, repo, write_enabled=True)
            published = sync_series_revision(
                publisher=writer, approved_digest=preview.approval_digest, **arguments,
            )
            self.assertTrue(published.branch.created)
            self.assertTrue(published.pull_request.created)
            replay = sync_series_revision(
                publisher=writer, approved_digest=preview.approval_digest, **arguments,
            )
            self.assertFalse(replay.branch.created)
            self.assertFalse(replay.pull_request.created)
            self.assertEqual(published.pull_request.remote_object["number"], replay.pull_request.remote_object["number"])
            remote_main = git("ls-remote", "--heads", remote, "refs/heads/main").decode()
            self.assertTrue(remote_main.startswith(base))

            pull_index = published.pull_request.remote_object["number"]
            comment_preview = ForgeCommentPublisher(api, owner, repo)
            with patch.dict(os.environ, {"LORE_GITEA_TOKEN": token}), redirect_stdout(StringIO()):
                self.assertEqual(main([
                    "map-identity", "--ledger", str(ledger.path), "--api-url", api_url,
                    "--email", "reviewer@example.test", "--username", owner, "--reason", "acceptance test",
                ]), 0)
            queue = queue_archive_replies(
                ledger, comment_preview, pull_index, revision.series_key, revision.revision,
                b4_python=b4_python,
            )
            self.assertEqual((queue.matched, queue.inline_count, queue.unresolved_count, queue.diagnostics), (1, 1, 0, ()))
            self.assertIn("Operator-mapped Gitea account (unverified sender)", ledger.pending()[0].payload["body"])
            comment_writer = ForgeCommentPublisher(
                api, owner, repo, write_enabled=True, allow_inline_writes=True,
            )
            source_time = "Source time (unverified Received header): 2026-10-22T13:30:00+00:00"
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

            plan = plan_comment_delivery(ledger, comment_writer, pull_index, "ISSUE_COMMENT")
            self.assertEqual(len(plan.deliveries), 1)
            self.assertIn(source_time, plan.deliveries[0].payload["body"])
            with patch.object(api, "transport", side_effect=lose_first_response):
                uncertain = deliver_approved_comments(
                    ledger, comment_writer, pull_index, "ISSUE_COMMENT", approved_digest=plan.approval_digest(),
                )
            self.assertTrue(accepted and hidden)
            self.assertEqual((uncertain.delivered, uncertain.failed), (0, 1))
            self.assertEqual(ledger.pending()[0].write_state, "uncertain")

            worker_environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
            worker_environment["LORE_GITEA_TOKEN"] = token
            worker = [
                sys.executable, str(Path(__file__).with_name("cli.py")), "deliver-comments",
                "--ledger", str(ledger.path), "--api-url", api_url, "--owner", owner, "--repo", repo,
                "--pull-index", str(pull_index), "--kind", "conversation",
            ]
            preview_worker = subprocess.run(worker, check=True, capture_output=True, text=True, env=worker_environment)
            worker_plan = json.loads(preview_worker.stdout)
            self.assertIn(source_time, worker_plan["plan"]["deliveries"][0]["payload"]["body"])
            resumed_worker = subprocess.run(
                [*worker, "--write", "--approve", worker_plan["approval_digest"]],
                check=True, capture_output=True, text=True, env=worker_environment,
            )
            worker_result = json.loads(resumed_worker.stdout)
            self.assertEqual((worker_result["delivered"], worker_result["failed"]), (1, 0))

            ledger = Ledger(ledger.path)
            api = ForgeAPI(api_url, token=token, max_retries=0)
            comment_preview = ForgeCommentPublisher(api, owner, repo)
            comment_writer = ForgeCommentPublisher(api, owner, repo, write_enabled=True, allow_inline_writes=True)
            plan = plan_comment_delivery(ledger, comment_writer, pull_index, "INLINE_COMMENT")
            self.assertEqual(len(plan.deliveries), 1)
            self.assertIn(source_time, plan.deliveries[0].payload["body"])
            delivered = deliver_approved_comments(
                ledger, comment_writer, pull_index, "INLINE_COMMENT", approved_digest=plan.approval_digest(),
            )
            self.assertEqual((delivered.delivered, delivered.failed), (1, 0))

            def remote_reply_bodies():
                source_url = f"https://lore.kernel.org/xen-devel/reply-{unique}@example.test/"
                issue_path = f"repos/{owner}/{repo}/issues/{pull_index}/comments"
                review_path = f"repos/{owner}/{repo}/pulls/{pull_index}/reviews"
                issue_bodies = [comment["body"] for comment in api.get_pages(issue_path) if source_url in comment["body"]]
                inline_bodies = [
                    comment["body"] for review in api.get_pages(review_path)
                    for comment in api.get_pages(f"{review_path}/{review['id']}/comments")
                    if source_url in comment["body"]
                ]
                return issue_bodies, inline_bodies

            original_bodies = remote_reply_bodies()
            for bodies in original_bodies:
                self.assertEqual(len(bodies), 1)
                self.assertIn(source_time, bodies[0])
            queue_archive_replies(
                ledger, comment_preview, pull_index, revision.series_key, revision.revision,
                b4_python=b4_python,
            )
            self.assertEqual(ledger.pending(), [])
            for event_type in ("ISSUE_COMMENT", "INLINE_COMMENT"):
                plan = plan_comment_delivery(ledger, comment_writer, pull_index, event_type)
                self.assertEqual(plan.deliveries, ())
                delivered = deliver_approved_comments(
                    ledger, comment_writer, pull_index, event_type, approved_digest=plan.approval_digest(),
                )
                self.assertEqual(delivered.delivered, 0)
            self.assertEqual(remote_reply_bodies(), original_bodies)

            tracking = {
                "series": {"revision": revision.revision, "change-id": "acceptance"},
                "patches": [{
                    "header-info": {"msgid": patch_message.message_id},
                    "reviews": {"reviewer@example.test": {
                        "reply": "Local acceptance draft",
                        "comments": [{
                            "path": f"acceptance-{unique}.txt", "line": 1,
                            "content": f"+Acceptance fixture {unique}", "text": "Draft line review",
                        }],
                    }},
                }],
            }
            patch_commit = git("-C", str(checkout), "rev-parse", "HEAD").decode().strip()
            tracking_commit = git(
                "-C", str(checkout), "commit-tree", f"{patch_commit}^{{tree}}", "-p", patch_commit,
                "-m", f"Cover\n\n--- b4-review-tracking ---\n{json.dumps(tracking)}",
            ).decode().strip()
            git("-C", str(checkout), "update-ref", "refs/heads/b4/review/acceptance", tracking_commit)
            draft_arguments = (
                ledger, comment_preview, pull_index, revision.series_key, revision.revision,
                checkout, "b4/review/acceptance", "reviewer@example.test",
            )
            draft = queue_b4_drafts(*draft_arguments, b4_python=b4_python)
            self.assertEqual((draft.tracking_commit, draft.queued, draft.inline_count, draft.unresolved_count), (tracking_commit, 1, 1, 0))
            for event_type in ("ISSUE_COMMENT", "INLINE_COMMENT"):
                plan = plan_comment_delivery(ledger, comment_writer, pull_index, event_type)
                self.assertEqual(len(plan.deliveries), 1)
                delivered = deliver_approved_comments(
                    ledger, comment_writer, pull_index, event_type, approved_digest=plan.approval_digest(),
                )
                self.assertEqual(delivered.delivered, 1)
            queue_b4_drafts(*draft_arguments, b4_python=b4_python)
            self.assertEqual(ledger.pending(), [])
            self.assertEqual(git("-C", str(checkout), "rev-parse", "HEAD").decode().strip(), patch_commit)

            git("-C", str(checkout), "checkout", "-B", "acceptance-v2", base)
            (checkout / f"acceptance-{unique}.txt").write_text(f"Acceptance revision 2 {unique}\n")
            git("-C", str(checkout), "add", ".")
            git("-C", str(checkout), "commit", "-qm", f"Update acceptance fixture {unique}", "-m", f"change-id: acceptance-{unique}")
            second_mailbox = Path(temp_dir) / "revision-2.mbox"
            second_mailbox.write_bytes(git(
                "-C", str(checkout), "format-patch", "--stdout", "--numbered", "--thread",
                "--subject-prefix=PATCH v2", "--no-signature", f"{base}..HEAD",
            ))
            (second_patch,) = parse_mbox(second_mailbox)
            ledger.record_parsed_message(second_patch)
            (unlinked,) = ledger.discover_related_series(second_patch.message_id)
            self.assertNotEqual(unlinked.series_key, revision.series_key)
            link_arguments = (revision.series_key, 1, second_patch.message_id, 2, "acceptance operator", "reviewed change-ID")
            link = ledger.plan_revision_link(*link_arguments)
            self.assertEqual(link.change_id, f"acceptance-{unique}")
            ledger.link_revision(*link_arguments, approved_digest=link.approval_digest())
            (revision_two,) = ledger.discover_related_series(second_patch.message_id)
            self.assertEqual(revision_two.series_key, revision.series_key)
            arguments_two = {**arguments, "revision": revision_two}
            with self.assertRaises(PullRequestConflict):
                sync_series_revision(publisher=ForgePullPublisher(api, owner, repo), **arguments_two)
            preview_two = sync_series_revision(
                publisher=ForgePullPublisher(api, owner, repo), revision_policy="separate", **arguments_two,
            )
            published_two = sync_series_revision(
                publisher=writer, revision_policy="separate", approved_digest=preview_two.approval_digest, **arguments_two,
            )
            self.assertNotEqual(published_two.pull_request.remote_object["number"], pull_index)
            original_pull = api.request_json(f"repos/{owner}/{repo}/pulls/{pull_index}")
            self.assertEqual(original_pull["head"]["sha"], published.branch.head_commit)
            self.assertEqual(original_pull["head"]["ref"], published.branch.branch)

            if os.environ.get("LORE_GITEA_ACCEPTANCE_DELETIONS") == "1":
                self.assertFalse(api.request_json("user")["is_admin"])
                source_url = f"https://lore.kernel.org/xen-devel/reply-{unique}@example.test/"
                issue_comments = list(api.get_pages(f"repos/{owner}/{repo}/issues/{pull_index}/comments"))
                (issue_comment,) = [item for item in issue_comments if source_url in item["body"]]
                reviews_path = f"repos/{owner}/{repo}/pulls/{pull_index}/reviews"
                matching_reviews = [
                    review for review in api.get_pages(reviews_path)
                    if any(source_url in item["body"] for item in api.get_pages(f"{reviews_path}/{review['id']}/comments"))
                ]
                self.assertEqual(len(matching_reviews), 1)
                review_path = f"{reviews_path}/{matching_reviews[0]['id']}"
                comment_path = f"repos/{owner}/{repo}/issues/comments/{issue_comment['id']}"
                self.assertIsNone(api.request_json(comment_path, method="DELETE"))
                self.assertIsNone(api.request_json(review_path, method="DELETE"))
                for path in (comment_path, review_path):
                    with self.subTest(path=path), self.assertRaises(ForgeAPIError) as error:
                        api.request_json(path)
                    self.assertEqual(error.exception.status, 404)

                second_index = published_two.pull_request.remote_object["number"]
                second_branch = published_two.branch.branch
                self.assertTrue(second_branch.startswith("lore/") and second_branch.endswith("/v2"))
                branch_path = f"repos/{owner}/{repo}/branches/{quote(second_branch, safe='/')}"
                self.assertIsNone(api.request_json(branch_path, method="DELETE"))
                self.assertEqual(git("ls-remote", "--heads", remote, f"refs/heads/{second_branch}"), b"")
                self.assertTrue(git("ls-remote", "--heads", remote, "refs/heads/main").decode().startswith(base))
                self.assertIsNone(api.request_json(f"repos/{owner}/{repo}/issues/{second_index}", method="DELETE"))
                with self.assertRaises(ForgeAPIError) as error:
                    api.request_json(f"repos/{owner}/{repo}/pulls/{second_index}")
                self.assertEqual(error.exception.status, 404)


if __name__ == "__main__":
    unittest.main()