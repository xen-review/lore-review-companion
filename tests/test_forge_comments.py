import json
import tempfile
import unittest
from pathlib import Path
from urllib.error import URLError
from unittest.mock import patch

from lore_review_companion.anchor import Anchor
from lore_review_companion.forge_api import ForgeAPI, ForgeAPIError
from lore_review_companion.forge_comments import DeliveryUncertainError, ForgeCommentPublisher, InlineWritesDisabledError, StaleAnchorError
from lore_review_companion.ledger import ArchiveSafetyBlocked, Ledger, MessageConflictError, WorkerBusyError
from lore_review_companion.outbox import (
    comment_destination, deliver_approved_comments, deliver_inline_comments,
    deliver_issue_comments, plan_comment_delivery,
)


class FakeResponse:
    def __init__(self, status, value):
        self.status = status
        self.body = value if isinstance(value, bytes) else json.dumps(value).encode()
        self.headers = {}

    def read(self, size=-1):
        return self.body if size < 0 else self.body[:size]

    def close(self):
        pass


class CommentTransport:
    def __init__(self):
        self.issue_comments = []
        self.reviews = []
        self.review_comments = {}
        self.requests = []
        self.fail_after_inline_write = False
        self.head_commit = "a" * 40
        self.fail_without_issue_write = False
        self.diff = b""
        self.change_head_after_diff = False
        self.users = {}
        self.actor_id = 5
        self.actor_login = "mirror-bot"

    def __call__(self, request, _timeout):
        self.requests.append(request)
        path = request.full_url.split("/api/v1/", 1)[-1].split("?", 1)[0]
        if request.get_method() == "GET":
            if path == "user":
                return FakeResponse(200, {"id": self.actor_id, "login": self.actor_login})
            if path.startswith("users/"):
                account = self.users.get(path.removeprefix("users/"))
                return FakeResponse(200, account) if account else FakeResponse(404, {"message": "not found"})
            if path.endswith("/issues/7/comments"):
                return FakeResponse(200, self.issue_comments)
            if path.endswith("/pulls/7/reviews"):
                return FakeResponse(200, self.reviews)
            if path.endswith("/pulls/7.diff"):
                if self.change_head_after_diff:
                    self.head_commit = "c" * 40
                return FakeResponse(200, self.diff)
            if path.endswith("/pulls/7"):
                return FakeResponse(200, {
                    "head": {"sha": self.head_commit}, "base": {"sha": "b" * 40}, "merge_base": "b" * 40,
                })
            if "/reviews/" in path and path.endswith("/comments"):
                review_id = int(path.split("/reviews/")[1].split("/")[0])
                return FakeResponse(200, self.review_comments.get(review_id, []))
            raise AssertionError(f"Unexpected GET {path}")

        payload = json.loads(request.data)
        if path.endswith("/issues/7/comments"):
            if self.fail_without_issue_write:
                raise URLError("connection lost before confirmation")
            comment = {"id": len(self.issue_comments) + 1, **payload}
            self.issue_comments.append(comment)
            return FakeResponse(201, comment)
        if path.endswith("/pulls/7/reviews"):
            review_id = len(self.reviews) + 1
            review = {"id": review_id, "body": payload["body"], "state": payload["event"]}
            self.reviews.append(review)
            self.review_comments[review_id] = [
                {
                    "id": review_id,
                    "body": comment["body"],
                    "path": comment["path"],
                    "position": comment["new_position"],
                    "original_position": comment["old_position"],
                }
                for comment in payload["comments"]
            ]
            if self.fail_after_inline_write:
                self.fail_after_inline_write = False
                raise URLError("connection dropped after server write")
            return FakeResponse(200, review)
        raise AssertionError(f"Unexpected {request.get_method()} {path}")


class ForgeCommentPublisherTests(unittest.TestCase):
    def setUp(self):
        self.transport = CommentTransport()
        self.api = ForgeAPI(
            "http://gitea.test/api/v1/",
            transport=self.transport,
            max_retries=0,
        )
        self.publisher = ForgeCommentPublisher(
            self.api, "xen-project", "xen", write_enabled=True, allow_inline_writes=True
        )

    def test_publisher_defaults_to_dry_run(self):
        publisher = ForgeCommentPublisher(self.api, "xen-project", "xen")

        result = publisher.ensure_issue_comment(
            7,
            source_message_id="review@example.test",
            effect_key="comment-1",
            body="Please check this boundary.",
        )

        self.assertTrue(result.planned)
        self.assertFalse(result.created)
        self.assertIn("lore-gitea-mirror:v1:", result.remote_object["body"])
        self.assertFalse(any(request.get_method() == "POST" for request in self.transport.requests))

    def test_observed_archive_deletion_blocks_previously_approved_comments(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Ledger(Path(directory) / "ledger.sqlite3")
            ledger.record_message("reply@example.test", b"raw")
            ledger.enqueue("reply@example.test", "ISSUE_COMMENT", comment_destination(self.publisher, 7), "full", {"body": "Review"})
            plan = plan_comment_delivery(ledger, self.publisher, 7, "ISSUE_COMMENT")
            ledger.record_archive_deletion("/archive/epoch", "d" * 40)
            with self.assertRaises(ArchiveSafetyBlocked):
                deliver_approved_comments(
                    ledger, self.publisher, 7, "ISSUE_COMMENT", approved_digest=plan.approval_digest(),
                )
            self.assertEqual(self.transport.issue_comments, [])

    def test_comment_approval_is_invalidated_by_authenticated_account_id_change(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Ledger(Path(directory) / "ledger.sqlite3")
            ledger.record_message("reply@example.test", b"raw")
            ledger.enqueue("reply@example.test", "ISSUE_COMMENT", comment_destination(self.publisher, 7), "full", {"body": "Review"})
            plan = plan_comment_delivery(ledger, self.publisher, 7, "ISSUE_COMMENT")
            self.assertEqual(plan.actor_id, 5)
            self.transport.actor_id = 6
            with self.assertRaisesRegex(ValueError, "does not match"):
                deliver_approved_comments(ledger, self.publisher, 7, "ISSUE_COMMENT", approved_digest=plan.approval_digest())
            self.assertEqual(self.transport.issue_comments, [])

    def test_issue_comment_creation_and_replay_reconcile_by_marker(self):
        created = self.publisher.ensure_issue_comment(
            7,
            source_message_id="review@example.test",
            effect_key="comment-1",
            body="Please check this boundary.",
        )
        replayed = self.publisher.ensure_issue_comment(
            7,
            source_message_id="review@example.test",
            effect_key="comment-1",
            body="Please check this boundary.",
        )

        self.assertTrue(created.created)
        self.assertFalse(replayed.created)
        self.assertEqual(created.remote_object["id"], replayed.remote_object["id"])
        self.assertEqual(
            [request.get_method() for request in self.transport.requests].count("POST"),
            1,
        )
        self.assertIn("lore-gitea-mirror:v1:", self.transport.issue_comments[0]["body"])

    def test_inline_comment_uses_comment_review_and_side_positions(self):
        result = self.publisher.ensure_inline_comment(
            7,
            Anchor("xen/common.c", old_position=0, new_position=42),
            source_message_id="inline@example.test",
            effect_key="line-42",
            body="This needs a bounds check.",
            head_commit=self.transport.head_commit,
        )

        self.assertTrue(result.created)
        request = next(
            request
            for request in self.transport.requests
            if request.get_method() == "POST"
        )
        payload = json.loads(request.data)
        self.assertEqual(payload["event"], "COMMENT")
        self.assertEqual(payload["commit_id"], self.transport.head_commit)
        self.assertEqual(payload["comments"][0]["path"], "xen/common.c")
        self.assertEqual(payload["comments"][0]["old_position"], 0)
        self.assertEqual(payload["comments"][0]["new_position"], 42)

    def test_inline_comment_replay_does_not_create_second_review(self):
        arguments = {
            "source_message_id": "inline@example.test",
            "effect_key": "line-42",
            "body": "This needs a bounds check.",
            "head_commit": self.transport.head_commit,
        }
        anchor = Anchor("xen/common.c", old_position=0, new_position=42)
        self.publisher.ensure_inline_comment(7, anchor, **arguments)
        replayed = self.publisher.ensure_inline_comment(7, anchor, **arguments)

        self.assertFalse(replayed.created)
        self.assertEqual(len(self.transport.reviews), 1)
        self.assertEqual(
            [request.get_method() for request in self.transport.requests].count("POST"),
            1,
        )

    def test_inline_reconciliation_requires_submitted_comment_review(self):
        arguments = {
            "source_message_id": "inline@example.test", "effect_key": "line-42",
            "body": "Review", "head_commit": self.transport.head_commit,
        }
        anchor = Anchor("x.c", old_position=0, new_position=42)
        self.publisher.ensure_inline_comment(7, anchor, **arguments)
        self.transport.reviews[0]["body"] = ""
        for state in ("PENDING", "APPROVED", ""):
            with self.subTest(state=state):
                self.transport.reviews[0]["state"] = state
                with self.assertRaises(DeliveryUncertainError):
                    self.publisher.ensure_inline_comment(7, anchor, **arguments)
        self.transport.reviews[0]["state"] = "COMMENT"
        self.assertFalse(self.publisher.ensure_inline_comment(7, anchor, **arguments).created)
        self.assertEqual(sum(request.get_method() == "POST" for request in self.transport.requests), 1)

    def test_outbox_does_not_complete_pending_inline_review(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Ledger(Path(directory) / "ledger.sqlite3")
            ledger.record_message("inline@example.test", b"raw")
            payload = {
                "body": "Review", "head_commit": self.transport.head_commit,
                "anchor": {"path": "x.c", "old_position": 0, "new_position": 42},
            }
            ledger.enqueue("inline@example.test", "INLINE_COMMENT", comment_destination(self.publisher, 7), "line-42", payload)
            self.publisher.ensure_inline_comment(
                7, Anchor("x.c", 0, 42), source_message_id="inline@example.test",
                effect_key="line-42", body="Review", head_commit=self.transport.head_commit,
            )
            self.transport.reviews[0]["state"] = "PENDING"
            self.assertEqual(deliver_inline_comments(ledger, self.publisher, 7).failed, 1)
            self.assertEqual(ledger.pending()[0].write_state, "uncertain")
            self.transport.reviews[0]["state"] = "COMMENT"
            self.assertEqual(deliver_inline_comments(ledger, self.publisher, 7).delivered, 1)
            self.assertEqual(len(self.transport.reviews), 1)

    def test_uncertain_success_is_found_before_retry(self):
        self.transport.fail_after_inline_write = True

        result = self.publisher.ensure_inline_comment(
            7,
            Anchor("xen/common.c", old_position=0, new_position=42),
            source_message_id="inline@example.test",
            effect_key="line-42",
            body="This needs a bounds check.",
            head_commit=self.transport.head_commit,
        )

        self.assertFalse(result.created)
        self.assertEqual(len(self.transport.reviews), 1)

    def test_inline_review_rejects_stale_head_and_invalid_anchor(self):
        arguments = {
            "source_message_id": "inline@example.test", "effect_key": "line-42",
            "body": "Review", "head_commit": "b" * 40,
        }
        with self.assertRaises(StaleAnchorError):
            self.publisher.ensure_inline_comment(7, Anchor("x.c", 0, 42), **arguments)
        arguments["head_commit"] = self.transport.head_commit
        with self.assertRaisesRegex(ValueError, "exactly one"):
            self.publisher.ensure_inline_comment(7, Anchor("x.c", 2, 42), **arguments)
        self.assertFalse(any(request.get_method() == "POST" for request in self.transport.requests))

    def test_inline_writes_require_opt_in_and_no_pending_review(self):
        arguments = {
            "source_message_id": "inline@example.test", "effect_key": "line-42",
            "body": "Review", "head_commit": self.transport.head_commit,
        }
        publisher = ForgeCommentPublisher(self.api, "xen-project", "xen", write_enabled=True)
        with self.assertRaisesRegex(InlineWritesDisabledError, "explicit opt-in"):
            publisher.ensure_inline_comment(7, Anchor("x.c", 0, 42), **arguments)
        self.transport.reviews.append({"id": 99, "state": "PENDING", "body": ""})
        with self.assertRaisesRegex(InlineWritesDisabledError, "pending review"):
            self.publisher.ensure_inline_comment(7, Anchor("x.c", 0, 42), **arguments)
        self.assertFalse(any(request.get_method() == "POST" for request in self.transport.requests))

    def test_unconfirmed_uncertain_write_is_not_retried_automatically(self):
        def transport(request, _timeout):
            if request.get_method() == "GET":
                return FakeResponse(200, [])
            raise URLError("connection dropped")

        publisher = ForgeCommentPublisher(
            ForgeAPI("http://gitea.test/api/v1/", transport=transport, max_retries=0),
            "xen-project",
            "xen",
            write_enabled=True,
        )

        with self.assertRaises(DeliveryUncertainError):
            publisher.ensure_issue_comment(
                7,
                source_message_id="review@example.test",
                effect_key="comment-1",
                body="Please check this boundary.",
            )

    def test_outbox_dry_run_does_not_mutate_or_starve_other_destinations(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Ledger(Path(directory) / "ledger.sqlite3")
            ledger.record_message("review@example.test", b"raw")
            ledger.enqueue("review@example.test", "ISSUE_COMMENT", "other/repo#7", "a", {"body": "other"})
            ledger.enqueue("review@example.test", "ISSUE_COMMENT", comment_destination(self.publisher, 7), "b", {"body": "preview"})
            publisher = ForgeCommentPublisher(self.api, "xen-project", "xen")

            result = deliver_issue_comments(ledger, publisher, 7, limit=1)

            self.assertEqual((result.planned, result.delivered), (1, 0))
            self.assertEqual(len(ledger.pending()), 2)
            self.assertEqual(ledger.pending()[1].attempts, 0)
            self.assertFalse(any(request.get_method() == "POST" for request in self.transport.requests))

    def test_outbox_reconciles_remote_effect_after_local_crash(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Ledger(Path(directory) / "ledger.sqlite3")
            ledger.record_message("review@example.test", b"raw")
            ledger.enqueue("review@example.test", "ISSUE_COMMENT", comment_destination(self.publisher, 7), "a", {"body": "review"})
            self.publisher.ensure_issue_comment(
                7, source_message_id="review@example.test", effect_key="a", body="review"
            )

            result = deliver_issue_comments(ledger, self.publisher, 7)

            self.assertEqual(result.delivered, 1)
            self.assertEqual(ledger.pending(), [])
            self.assertEqual(len(self.transport.issue_comments), 1)

    def test_outbox_reconciles_timeout_after_remote_write_on_restart(self):
        hidden = True

        def transport(request, timeout):
            nonlocal hidden
            response = self.transport(request, timeout)
            if request.get_method() == "POST" and request.full_url.endswith("/issues/7/comments"):
                raise TimeoutError("response lost")
            if request.get_method() == "GET" and "/issues/7/comments" in request.full_url and self.transport.issue_comments and hidden:
                hidden = False
                return FakeResponse(200, [])
            return response

        self.api.transport = transport
        with tempfile.TemporaryDirectory() as directory:
            ledger = Ledger(Path(directory) / "ledger.sqlite3")
            ledger.record_message("review@example.test", b"raw")
            ledger.enqueue("review@example.test", "ISSUE_COMMENT", comment_destination(self.publisher, 7), "a", {"body": "review"})
            self.assertEqual(deliver_issue_comments(ledger, self.publisher, 7).failed, 1)
            self.assertEqual(ledger.pending()[0].write_state, "uncertain")
            reopened = Ledger(ledger.path)
            self.assertEqual(deliver_issue_comments(reopened, self.publisher, 7).delivered, 1)
            self.assertEqual(reopened.pending(), [])
            self.assertEqual(len(self.transport.issue_comments), 1)
            self.assertEqual(sum(request.get_method() == "POST" for request in self.transport.requests), 1)

    def test_outbox_failure_remains_pending_with_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Ledger(Path(directory) / "ledger.sqlite3")
            ledger.record_message("review@example.test", b"raw")
            ledger.enqueue("review@example.test", "ISSUE_COMMENT", comment_destination(self.publisher, 7), "a", {"body": ""})

            result = deliver_issue_comments(ledger, self.publisher, 7)

            self.assertEqual(result.failed, 1)
            self.assertEqual(ledger.pending()[0].attempts, 1)
            self.assertIn("body", ledger.pending()[0].last_error)

    def test_outbox_uncertain_write_requires_reconciliation_or_explicit_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Ledger(Path(directory) / "ledger.sqlite3")
            ledger.record_message("review@example.test", b"raw")
            ledger.enqueue("review@example.test", "ISSUE_COMMENT", comment_destination(self.publisher, 7), "a", {"body": "review"})
            self.transport.fail_without_issue_write = True
            self.assertEqual(deliver_issue_comments(ledger, self.publisher, 7).failed, 1)
            self.assertEqual(ledger.pending()[0].write_state, "uncertain")
            self.transport.fail_without_issue_write = False
            self.assertEqual(deliver_issue_comments(ledger, self.publisher, 7).failed, 1)
            self.assertEqual(sum(request.get_method() == "POST" for request in self.transport.requests), 1)
            self.assertEqual(deliver_issue_comments(ledger, self.publisher, 7, retry_uncertain=True).delivered, 1)
            self.assertEqual(len(self.transport.issue_comments), 1)

    def test_outbox_inline_delivery_and_stale_head_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Ledger(Path(directory) / "ledger.sqlite3")
            ledger.record_message("inline@example.test", b"raw")
            payload = {
                "body": "Review with lore link", "head_commit": self.transport.head_commit,
                "anchor": {"path": "xen/common.c", "old_position": 0, "new_position": 42},
            }
            destination = comment_destination(self.publisher, 7)
            ledger.enqueue("inline@example.test", "INLINE_COMMENT", destination, "inline", payload)
            self.assertEqual(deliver_inline_comments(ledger, self.publisher, 7).delivered, 1)
            self.assertEqual(len(self.transport.reviews), 1)

            payload["head_commit"] = "b" * 40
            ledger.enqueue("inline@example.test", "INLINE_COMMENT", destination, "stale", payload)
            result = deliver_inline_comments(ledger, self.publisher, 7)
            self.assertEqual((result.delivered, result.failed), (0, 1))
            self.assertEqual(len(self.transport.reviews), 1)
            self.assertEqual(self.transport.issue_comments, [])
            self.assertEqual([item.effect_key for item in ledger.pending()], ["stale"])
            self.assertIn("differs from the reviewed commit", ledger.pending()[0].last_error)

    def test_approved_inline_delivery_never_falls_back_to_conversation(self):
        for unsafe in ("stale_head", "inline_disabled", "pending_review"):
            with self.subTest(unsafe=unsafe), tempfile.TemporaryDirectory() as directory:
                transport = CommentTransport()
                api = ForgeAPI("http://gitea.test/api/v1/", transport=transport, max_retries=0)
                publisher = ForgeCommentPublisher(
                    api, "xen-project", "xen", write_enabled=True,
                    allow_inline_writes=unsafe != "inline_disabled",
                )
                if unsafe == "pending_review":
                    transport.reviews.append({"id": 1, "state": "PENDING"})
                ledger = Ledger(Path(directory) / "ledger.sqlite3")
                ledger.record_message("review@example.test", b"raw")
                destination = comment_destination(publisher, 7)
                ledger.enqueue("review@example.test", "ISSUE_COMMENT", destination, "full", {"body": "Full reply"})
                ledger.enqueue("review@example.test", "INLINE_COMMENT", destination, "inline", {
                    "body": "Inline excerpt",
                    "head_commit": "b" * 40 if unsafe == "stale_head" else transport.head_commit,
                    "anchor": {"path": "xen/common.c", "old_position": 0, "new_position": 42},
                })
                plan = plan_comment_delivery(ledger, publisher, 7, "INLINE_COMMENT")

                result = deliver_approved_comments(
                    ledger, publisher, 7, "INLINE_COMMENT", approved_digest=plan.approval_digest(),
                )

                self.assertEqual((result.delivered, result.failed), (0, 1))
                self.assertFalse(any(request.get_method() == "POST" for request in transport.requests))
                pending = {item.effect_key: item for item in ledger.pending()}
                self.assertEqual(set(pending), {"full", "inline"})
                self.assertTrue(pending["inline"].last_error)
                self.assertEqual(pending["full"].attempts, 0)

                conversation_plan = plan_comment_delivery(ledger, publisher, 7, "ISSUE_COMMENT")
                result = deliver_approved_comments(
                    ledger, publisher, 7, "ISSUE_COMMENT", approved_digest=conversation_plan.approval_digest(),
                )
                self.assertEqual(result.delivered, 1)
                self.assertEqual(len(transport.issue_comments), 1)
                self.assertIn("Full reply", transport.issue_comments[0]["body"])
                self.assertEqual([item.effect_key for item in ledger.pending()], ["inline"])

    def test_outbox_write_uses_shared_worker_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Ledger(Path(directory) / "ledger.sqlite3")
            with ledger.worker_lock(), self.assertRaises(WorkerBusyError):
                deliver_issue_comments(ledger, self.publisher, 7)

    def test_comment_delivery_requires_unchanged_approved_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Ledger(Path(directory) / "ledger.sqlite3")
            ledger.record_message("review@example.test", b"raw")
            destination = comment_destination(self.publisher, 7)
            ledger.enqueue("review@example.test", "ISSUE_COMMENT", destination, "one", {"body": "Review one"})
            plan = plan_comment_delivery(ledger, self.publisher, 7, "ISSUE_COMMENT")
            self.assertEqual(plan.actor, "mirror-bot")
            self.assertEqual(plan.deliveries[0].payload["body"], "Review mirror on behalf of @mirror-bot\n\nReview one")
            ledger.enqueue("review@example.test", "ISSUE_COMMENT", destination, "two", {"body": "Review two"})
            with self.assertRaisesRegex(ValueError, "does not match"):
                deliver_approved_comments(ledger, self.publisher, 7, "ISSUE_COMMENT", approved_digest=plan.approval_digest())
            self.assertFalse(any(request.get_method() == "POST" for request in self.transport.requests))
            updated = plan_comment_delivery(ledger, self.publisher, 7, "ISSUE_COMMENT")
            delivered = deliver_approved_comments(
                ledger, self.publisher, 7, "ISSUE_COMMENT", approved_digest=updated.approval_digest(),
            )
            self.assertEqual(delivered.delivered, 2)
            self.assertTrue(all(comment["body"].startswith("Review mirror on behalf of @mirror-bot\n\n") for comment in self.transport.issue_comments))

    def test_attribution_rejects_an_unsafe_authenticated_login(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Ledger(Path(directory) / "ledger.sqlite3")
            ledger.record_message("review@example.test", b"raw")
            ledger.enqueue("review@example.test", "ISSUE_COMMENT", comment_destination(self.publisher, 7), "one", {"body": "Review"})
            self.transport.actor_login = "mirror-bot\n@other"
            with self.assertRaisesRegex(ValueError, "cannot be safely used"):
                plan_comment_delivery(ledger, self.publisher, 7, "ISSUE_COMMENT")
            self.assertEqual(self.transport.issue_comments, [])

    def test_approved_delivery_does_not_send_newly_queued_messages(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Ledger(Path(directory) / "ledger.sqlite3")
            ledger.record_message("review@example.test", b"raw")
            destination = comment_destination(self.publisher, 7)
            ledger.enqueue("review@example.test", "ISSUE_COMMENT", destination, "one", {"body": "Review one"})
            plan = plan_comment_delivery(ledger, self.publisher, 7, "ISSUE_COMMENT")
            pending = ledger.pending

            def enqueue_after_snapshot(*args, **kwargs):
                selected = pending(*args, **kwargs)
                ledger.enqueue("review@example.test", "ISSUE_COMMENT", destination, "two", {"body": "Review two"})
                return selected

            with patch.object(ledger, "pending", side_effect=enqueue_after_snapshot):
                result = deliver_approved_comments(
                    ledger, self.publisher, 7, "ISSUE_COMMENT", approved_digest=plan.approval_digest(),
                )
            self.assertEqual(result.delivered, 1)
            self.assertEqual([item.effect_key for item in ledger.pending()], ["two"])

    def test_comment_approval_binds_inline_and_retry_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Ledger(Path(directory) / "ledger.sqlite3")
            plan = plan_comment_delivery(ledger, self.publisher, 7, "INLINE_COMMENT")
            without_inline = ForgeCommentPublisher(self.api, "xen-project", "xen", write_enabled=True)
            with self.assertRaisesRegex(ValueError, "does not match"):
                deliver_approved_comments(ledger, without_inline, 7, "INLINE_COMMENT", approved_digest=plan.approval_digest())
            with self.assertRaisesRegex(ValueError, "does not match"):
                deliver_approved_comments(
                    ledger, self.publisher, 7, "INLINE_COMMENT", approved_digest=plan.approval_digest(),
                    retry_uncertain=True,
                )

    def test_conflict_detected_after_queueing_blocks_delivery(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Ledger(Path(directory) / "ledger.sqlite3")
            ledger.record_message("review@example.test", b"original")
            ledger.enqueue(
                "review@example.test", "ISSUE_COMMENT", comment_destination(self.publisher, 7),
                "comment", {"body": "Review"},
            )
            with self.assertRaises(MessageConflictError):
                ledger.record_message("review@example.test", b"conflicting")
            with self.assertRaisesRegex(MessageConflictError, "Quarantined"):
                plan_comment_delivery(ledger, self.publisher, 7, "ISSUE_COMMENT")
            self.assertEqual(deliver_issue_comments(ledger, self.publisher, 7).failed, 1)
            self.assertEqual(ledger.pending()[0].write_state, "pending")
            self.assertFalse(any(request.get_method() == "POST" for request in self.transport.requests))


if __name__ == "__main__":
    unittest.main()