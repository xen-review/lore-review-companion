# SPDX-License-Identifier: GPL-2.0-or-later

import json
import hashlib
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from dataclasses import replace
from unittest.mock import patch
from urllib.error import URLError

from lore_review_companion.anchor import DiffIndex
from lore_review_companion.b4_adapter import (
    B4ReviewError,
    DraftSnapshot,
    LocalDraft,
    QuotedComment,
)
from lore_review_companion.cli import main
from lore_review_companion.forge_api import ForgeAPI
from lore_review_companion.forge_comments import (
    ForgeCommentPublisher,
    imported_mail_text,
    quote_untrusted_text,
)
from lore_review_companion.forge_imports import ForgeImportClient, ImportDeliveryResult
from lore_review_companion.forge_pulls import _series_marker
from lore_review_companion.ledger import (
    DeliveryStateConflict,
    Ledger,
    MessageConflictError,
    PublicationConflict,
    WorkerBusyError,
)
from lore_review_companion.mail_parser import parse_message
from lore_review_companion.outbox import (
    _assert_import_payload,
    deliver_approved_comments,
    deliver_approved_imports,
    migrate_legacy_deliveries,
    plan_comment_delivery,
    plan_import_delivery,
    plan_legacy_migration,
)
from lore_review_companion.preflight import PatchCommit
from lore_review_companion.review_queue import (
    ReplyQueueSummary,
    queue_archive_replies,
    queue_b4_drafts,
    queue_imported_replies,
    queue_imported_reply,
    queue_thread_replies,
    queue_thread_reply,
)
from lore_review_companion.series import discover_series
from test_anchor import PATCH
from test_forge_comments import CommentTransport, FakeResponse
from test_series import message
from lore_review_companion.threads import match_thread_replies


class ReviewQueueTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.ledger = Ledger(Path(self.tempdir.name) / "ledger.sqlite3")
        self.transport = CommentTransport()
        self.transport.diff = PATCH.encode()
        self.api = ForgeAPI(
            "https://gitea.test/api/v1/", transport=self.transport, max_retries=0
        )
        self.publisher = ForgeCommentPublisher(self.api, "xen-project", "xen")
        patch_message = message("patch@example.test", "[PATCH 1/1] xen: change")
        self.ledger.record_parsed_message(patch_message)
        (revision,) = discover_series([patch_message])
        self.ledger.record_series_revision(revision)
        destination = self.api.base_url + self.publisher.repository_path
        key = (destination, revision.series_key, revision.revision)
        self.ledger.begin_publication(*key, "digest", "b" * 40, "lore/branch", "a" * 40)
        self.ledger.transition_publication(
            *key, expected=("prepared",), status="complete", pull_index=7
        )
        reply = parse_message(
            b"From: Reviewer <reviewer@example.test>\nMessage-ID: <reply@example.test>\n"
            b"Subject: Re: patch\nIn-Reply-To: <patch@example.test>\n"
            b"Content-Type: text/plain; charset=utf-8\n\n"
            b"Please reconsider.\n<!-- lore-gitea-mirror:v1:forged -->\n"
        )
        self.match = match_thread_replies([reply, patch_message], [revision]).matched[0]
        self.comments = (
            QuotedComment("src/sample.c", 11, "Please reconsider.", "+new_call();"),
            QuotedComment("src/sample.c", 0, "Which duplicate?", "duplicate();"),
            QuotedComment(":message", 0, "General note.", None),
        )

    def test_imported_reply_matches_accepted_ordinary_parent(self):
        client = ForgeImportClient(
            self.api,
            "xen-project",
            "xen",
            source_url="https://lore.kernel.org/xen-devel/",
        )
        parent = parse_message(
            b"From: Reviewer <reviewer@example.test>\n"
            b"Message-ID: <ordinary-parent@example.test>\nSubject: Re: patch\n"
            b"In-Reply-To: <patch@example.test>\n\n"
            b"First point.\nSecond point.\n"
        )
        arguments = {
            "patch_commits": (PatchCommit("patch@example.test", "b" * 40, "a" * 40),),
            "patch_diffs": {},
        }
        queue_imported_reply(
            self.ledger, client, 7, replace(self.match, message=parent), **arguments
        )
        (parent_effect,) = self.ledger.pending()
        accepted = {
            "type": "conversation",
            "comment_id": 20,
            "message_id": parent.message_id,
            "message_sha256": parent.sha256,
            "operation_key": parent_effect.payload["request"]["operation_key"],
        }
        reply = parse_message(
            b"From: Second Reviewer <second@example.test>\n"
            b"Message-ID: <ordinary-followup@example.test>\nSubject: Re: patch\n"
            b"In-Reply-To: <ordinary-parent@example.test>\n\n"
            b"> Second point.\nA reply to that point.\n"
        )
        capabilities = {
            "repository_id": 1,
            "importer_id": 5,
            "importer_login": "mirror-bot",
            "operations": ["conversation", "review-reply", "conversation-reply"],
            "max_inline_lines": 50,
        }
        with (
            patch.object(client, "lookup_operation", return_value=accepted),
            patch.object(client, "capabilities", return_value=capabilities),
        ):
            capabilities["operations"].remove("conversation-reply")
            unsupported = queue_imported_reply(
                self.ledger, client, 7, replace(self.match, message=reply), **arguments
            )
            self.assertEqual(
                (unsupported.inline_count, unsupported.unresolved_count), (0, 1)
            )
            self.assertFalse(
                any(item.event_type == "IMPORT_REPLY" for item in self.ledger.pending())
            )
            capabilities["operations"].append("conversation-reply")
            result = queue_imported_reply(
                self.ledger, client, 7, replace(self.match, message=reply), **arguments
            )
            replies = [
                item
                for item in self.ledger.pending()
                if item.event_type == "IMPORT_REPLY"
            ]
            self.assertEqual((result.inline_count, result.unresolved_count), (1, 0))
            self.assertEqual(len(replies), 1)
            self.assertEqual(replies[0].payload["request"]["parent_id"], 20)
            self.assertEqual(
                replies[0].payload["evidence"]["parent_effect_key"],
                parent_effect.effect_key,
            )
            plan = plan_import_delivery(self.ledger, client, 7, "IMPORT_REPLY")
            self.assertEqual(plan.deliveries, tuple(replies))
            before = self.ledger.pending()
            queue_imported_reply(
                self.ledger, client, 7, replace(self.match, message=reply), **arguments
            )
            self.assertEqual(self.ledger.pending(), before)
            capabilities["operations"].remove("conversation-reply")
            with self.assertRaisesRegex(ValueError, "not enabled"):
                plan_import_delivery(self.ledger, client, 7, "IMPORT_REPLY")
            capabilities["operations"].append("conversation-reply")
            accepted["comment_id"] = 99
            with self.assertRaisesRegex(
                PublicationConflict, "accepted parent operation"
            ):
                plan_import_delivery(self.ledger, client, 7, "IMPORT_REPLY")
            accepted["comment_id"] = 20
            self.ledger.enqueue(
                parent.message_id,
                "IMPORT_CONVERSATION",
                parent_effect.destination,
                "duplicate-ordinary-candidate",
                parent_effect.payload,
            )
            with self.assertRaisesRegex(PublicationConflict, "uniquely match"):
                plan_import_delivery(self.ledger, client, 7, "IMPORT_REPLY")

    def test_imported_ordinary_reply_to_cover_discussion(self):
        self.ledger = Ledger(Path(self.tempdir.name) / "cover-ledger.sqlite3")
        cover = message("cover@example.test", "[PATCH 0/1] xen: cover")
        patch_message = message(
            "cover-patch@example.test",
            "[PATCH 1/1] xen: cover",
            headers="In-Reply-To: <cover@example.test>\n",
        )
        (revision,) = discover_series((cover, patch_message))
        for source in revision.messages:
            self.ledger.record_parsed_message(source)
        self.ledger.record_series_revision(revision)
        destination = self.api.base_url + self.publisher.repository_path
        key = destination, revision.series_key, revision.revision
        self.ledger.begin_publication(*key, "digest", "b" * 40, "lore/cover", "a" * 40)
        self.ledger.transition_publication(
            *key, expected=("prepared",), status="complete", pull_index=7
        )
        parent = parse_message(
            b"From: Reviewer <reviewer@example.test>\nMessage-ID: <cover-parent@example.test>\n"
            b"Subject: Re: cover\nIn-Reply-To: <cover@example.test>\n\n"
            b"> Patch body.\n\nAn authored concern.\n"
        )
        child = parse_message(
            b"From: Second <second@example.test>\nMessage-ID: <cover-child@example.test>\n"
            b"Subject: Re: cover\nIn-Reply-To: <cover-parent@example.test>\n\n"
            b"> An authored concern.\nI agree with the concern.\n"
        )
        quoted_cover = parse_message(
            b"From: Second <second@example.test>\nMessage-ID: <cover-quote@example.test>\n"
            b"Subject: Re: cover\nIn-Reply-To: <cover-parent@example.test>\n\n"
            b"> Patch body.\nThis is not the reviewer's remark.\n"
        )
        matches = {
            item.message.message_id: item
            for item in match_thread_replies(
                (*revision.messages, parent, child, quoted_cover), (revision,)
            ).matched
        }
        self.assertIsNone(matches[child.message_id].patch_message_id)
        client = ForgeImportClient(
            self.api,
            "xen-project",
            "xen",
            source_url="https://lore.kernel.org/xen-devel/",
        )
        arguments = {
            "patch_commits": (
                PatchCommit(patch_message.message_id, "b" * 40, "a" * 40),
            ),
            "patch_diffs": {},
        }
        queue_imported_reply(
            self.ledger, client, 7, matches[parent.message_id], **arguments
        )
        (parent_effect,) = self.ledger.pending()
        accepted = {
            "type": "conversation",
            "comment_id": 20,
            "message_id": parent.message_id,
            "message_sha256": parent.sha256,
        }
        capabilities = {
            "repository_id": 1,
            "importer_id": 5,
            "importer_login": "mirror-bot",
            "operations": ["conversation", "review-reply", "conversation-reply"],
            "max_inline_lines": 50,
        }
        with (
            patch.object(client, "lookup_operation", return_value=accepted),
            patch.object(client, "capabilities", return_value=capabilities),
        ):
            result = queue_imported_reply(
                self.ledger, client, 7, matches[child.message_id], **arguments
            )
            self.assertEqual((result.inline_count, result.unresolved_count), (1, 0))
            plan = plan_import_delivery(self.ledger, client, 7, "IMPORT_REPLY")
            self.assertEqual(plan.deliveries[0].payload["request"]["parent_id"], 20)
            unresolved = queue_imported_reply(
                self.ledger, client, 7, matches[quoted_cover.message_id], **arguments
            )
            self.assertEqual(
                (unresolved.inline_count, unresolved.unresolved_count), (0, 1)
            )

    def test_direct_submission_conversation_formats_quotes_and_revalidates(self):
        self.ledger = Ledger(Path(self.tempdir.name) / "presentation-ledger.sqlite3")
        parent = parse_message(
            b"From: Patch Author <author@example.test>\nMessage-ID: <format-patch@example.test>\n"
            b"Subject: [PATCH 1/1] change\n\nCommit explanation.\n"
            b"diff --git a/file.c b/file.c\n+new\n"
        )
        (revision,) = discover_series((parent,))
        self.ledger.record_parsed_message(parent)
        self.ledger.record_series_revision(revision)
        destination = self.api.base_url + self.publisher.repository_path
        key = (destination, revision.series_key, revision.revision)
        self.ledger.begin_publication(*key, "digest", "b" * 40, "lore/branch", "a" * 40)
        self.ledger.transition_publication(
            *key, expected=("prepared",), status="complete", pull_index=7
        )
        reply = parse_message(
            b"From: Andrew Cooper <reviewer@example.test>\nMessage-ID: <format-reply@example.test>\n"
            b"Subject: Re: patch\nIn-Reply-To: <format-patch@example.test>\n\n"
            b"On Monday, Author wrote:\n> Commit explanation.\n\nPlease change this.\n\n~Andrew\n"
        )
        (match,) = match_thread_replies((parent, reply), (revision,)).matched
        client = ForgeImportClient(
            self.api,
            "xen-project",
            "xen",
            source_url="https://lore.kernel.org/xen-devel/",
        )
        arguments = {
            "patch_commits": (PatchCommit(parent.message_id, "b" * 40, "a" * 40),),
            "patch_diffs": {},
        }
        queue_imported_reply(self.ledger, client, 7, match, **arguments)
        (effect,) = self.ledger.pending()
        self.assertEqual(effect.payload["body_format"], "mail-text-v4")
        self.assertEqual(
            effect.payload["request"]["body"],
            "On Monday, Author wrote:\n```rust\n> Commit explanation.\n```\n\nPlease change this.",
        )
        _assert_import_payload(self.ledger, effect, source_url=client.source_url)
        tampered = json.loads(json.dumps(effect.payload))
        tampered["request"]["body"] += " Altered."
        with self.assertRaisesRegex(PublicationConflict, "retained complete message"):
            _assert_import_payload(
                self.ledger,
                replace(effect, payload=tampered),
                source_url=client.source_url,
            )
        queue_imported_reply(self.ledger, client, 7, match, **arguments)
        self.assertEqual(self.ledger.pending(), [effect])

    def test_native_multiline_parse_queue_preview_deliver_and_replay(self):
        reply = parse_message(
            b"From: Reviewer <reviewer@example.test>\nMessage-ID: <native@example.test>\n"
            b"Subject: Re: patch\nIn-Reply-To: <patch@example.test>\n\n"
            b"> --- a/src/sample.c\n> +++ b/src/sample.c\n"
            b"> +new_call();\n> +duplicate();\nPlease change both lines.\n"
        )
        match = replace(self.match, message=reply)
        publisher = ForgeCommentPublisher(
            self.api, "xen-project", "xen", max_inline_lines=50
        )
        result = queue_thread_reply(self.ledger, publisher, 7, match)
        before = self.ledger.pending()
        self.assertEqual((result.inline_count, result.unresolved_count), (1, 0))
        self.assertEqual(
            before[1].payload["anchor"],
            {
                "path": "src/sample.c",
                "old_position": 0,
                "new_position": 11,
                "extra_lines_count": 1,
            },
        )
        self.assertEqual(before[1].payload["evidence"]["source_sha256"], reply.sha256)
        self.assertEqual(
            before[1].payload["evidence"]["quote"][-2:],
            ["+new_call();", "+duplicate();"],
        )
        queue_thread_reply(self.ledger, publisher, 7, match)
        self.assertEqual(self.ledger.pending(), before)
        writer = ForgeCommentPublisher(
            self.api,
            "xen-project",
            "xen",
            write_enabled=True,
            allow_inline_writes=True,
            max_inline_lines=50,
        )
        plan = plan_comment_delivery(self.ledger, writer, 7, "INLINE_COMMENT")
        self.transport.fail_after_inline_write = True
        self.assertEqual(
            deliver_approved_comments(
                self.ledger,
                writer,
                7,
                "INLINE_COMMENT",
                approved_digest=plan.approval_digest(),
            ).delivered,
            1,
        )
        queue_thread_reply(self.ledger, publisher, 7, match)
        self.assertEqual(
            [item.event_type for item in self.ledger.pending()], ["ISSUE_COMMENT"]
        )
        self.assertEqual(len(self.transport.reviews), 1)

    def test_imported_queue_uses_exact_first_patch_not_cumulative_diff(self):
        self.ledger = Ledger(Path(self.tempdir.name) / "imported-ledger.sqlite3")
        first = message("first@example.test", "[PATCH 1/2] xen: first")
        second = parse_message(
            b"From: Author <author@example.test>\nMessage-ID: <second@example.test>\n"
            b"Subject: [PATCH 2/2] xen: second\nIn-Reply-To: <first@example.test>\n\nSecond patch\n"
        )
        (revision,) = discover_series((first, second))
        self.assertTrue(revision.complete)
        for source in revision.messages:
            self.ledger.record_parsed_message(source)
        self.ledger.record_series_revision(revision)
        destination = self.api.base_url + self.publisher.repository_path
        key = destination, revision.series_key, revision.revision
        self.ledger.begin_publication(
            *key, "digest", "b" * 40, "lore/imported", "a" * 40
        )
        self.ledger.transition_publication(
            *key, expected=("prepared",), status="complete", pull_index=7
        )
        reply = parse_message(
            b"From: Reviewer <reviewer@example.test>\nMessage-ID: <imported@example.test>\n"
            b"Subject: Re: first\nIn-Reply-To: <first@example.test>\n\n"
            b"> --- a/src/sample.c\n> +++ b/src/sample.c\n"
            b"> +new_call();\n> +duplicate();\nPlease change both lines.\n"
        )
        (match,) = match_thread_replies(
            (*revision.messages, reply), (revision,)
        ).matched
        client = ForgeImportClient(
            self.api,
            "xen-project",
            "xen",
            source_url="https://lore.kernel.org/xen-devel/",
        )
        commits = (
            PatchCommit(first.message_id, "b" * 40, "c" * 40),
            PatchCommit(second.message_id, "c" * 40, "a" * 40),
        )
        self.transport.diff = b"cumulative diff is intentionally not used for anchoring"
        snapshot = {
            "head_commit": "a" * 40,
            "base_commit": "b" * 40,
            "merge_base": "b" * 40,
            "before_commit": "b" * 40,
            "after_commit": "c" * 40,
            "diff": PATCH,
            "diff_sha256": hashlib.sha256(PATCH.encode()).hexdigest(),
        }
        with (
            patch.object(
                client,
                "capabilities",
                return_value={
                    "max_inline_lines": 50,
                    "operations": ["conversation", "review"],
                },
            ),
            patch.object(client, "comparison", return_value=snapshot),
        ):
            result = queue_imported_reply(
                self.ledger,
                client,
                7,
                match,
                patch_commits=commits,
                patch_diffs={first.message_id: PATCH},
            )
            before = self.ledger.pending()
            queue_imported_reply(
                self.ledger,
                client,
                7,
                match,
                patch_commits=commits,
                patch_diffs={first.message_id: PATCH},
            )
        self.assertEqual(self.ledger.pending(), before)
        self.assertEqual((result.inline_count, result.unresolved_count), (1, 0))
        self.assertEqual(
            [item.event_type for item in before],
            ["IMPORT_CONVERSATION", "IMPORT_REVIEW"],
        )
        request = before[1].payload["request"]
        self.assertEqual(before[0].payload["body_format"], "mail-text-v4")
        self.assertEqual(before[0].payload["request"]["body"], reply.body_text.rstrip())
        self.assertEqual(
            request["comments"][0]["body"],
            imported_mail_text(
                "Please change both lines.",
                reply.message_id,
                client.source_url,
                "mail-text-v4",
                "Reviewer",
            ),
        )
        self.assertNotIn("Re: first", request["body"])
        self.assertNotIn("```", request["comments"][0]["body"])
        self.assertEqual(request["head_commit"], "a" * 40)
        self.assertEqual(request["comments"][0]["before_commit"], "b" * 40)
        self.assertEqual(request["comments"][0]["after_commit"], "c" * 40)
        self.assertEqual(request["comments"][0]["extra_lines_count"], 1)
        self.assertEqual(
            before[1].payload["evidence"]["patch_source_sha256"], first.sha256
        )
        original_transport = self.api.transport
        capabilities = {
            "version": 1,
            "repository_id": 1,
            "importer_id": 5,
            "importer_login": "mirror-bot",
            "source_url": client.source_url,
            "operations": ["conversation", "review", "review-reply"],
            "max_inline_lines": 50,
        }
        accepted, posts = {}, []

        def transport(request, timeout):
            if request.full_url.endswith("/review-imports/capabilities"):
                return FakeResponse(200, capabilities)
            if "/review-imports/pulls/7/patches/" in request.full_url:
                return FakeResponse(200, snapshot)
            if "/review-imports/operations/" in request.full_url:
                return FakeResponse(200, accepted)
            if (
                request.get_method() == "POST"
                and "/review-imports/" in request.full_url
            ):
                posts.append(request)
                sent = json.loads(request.data)
                if request.full_url.endswith("/replies"):
                    return FakeResponse(
                        200,
                        {
                            **accepted,
                            "id": 11,
                            "comment_id": 31,
                            "type": "review-reply",
                            "operation_key": sent["operation_key"],
                            "message_id": sent["message_id"],
                            "message_sha256": sent["message_sha256"],
                            "source_url": client.source_url + sent["message_id"] + "/",
                        },
                    )
                if not accepted:
                    accepted.update(
                        {
                            "id": 10,
                            "repository_id": 1,
                            "importer_id": 5,
                            "participant_id": 6,
                            "comment_id": 20,
                            "review_id": 21,
                            "code_comment_ids": [30],
                            "type": "review",
                            "operation_key": sent["operation_key"],
                            "message_id": sent["message_id"],
                            "message_sha256": sent["message_sha256"],
                            "source_url": client.source_url + sent["message_id"] + "/",
                        }
                    )
                    raise URLError("response lost after accepted review")
                return FakeResponse(200, accepted)
            return original_transport(request, timeout)

        self.api.transport = transport
        writer = ForgeImportClient(
            self.api,
            client.owner,
            client.repo,
            source_url=client.source_url,
            write_enabled=True,
        )
        plan = plan_import_delivery(self.ledger, writer, 7, "IMPORT_REVIEW")
        snapshot["base_commit"] = "d" * 40
        with self.assertRaisesRegex(PublicationConflict, "changed before approval"):
            deliver_approved_imports(
                self.ledger,
                writer,
                7,
                "IMPORT_REVIEW",
                approved_digest=plan.approval_digest(),
            )
        self.assertEqual(posts, [])
        snapshot["base_commit"] = "b" * 40
        result = deliver_approved_imports(
            self.ledger,
            writer,
            7,
            "IMPORT_REVIEW",
            approved_digest=plan.approval_digest(),
        )
        self.assertEqual((result.delivered, result.failed), (1, 0))
        self.assertEqual(len(posts), 2)
        self.assertEqual(json.loads(posts[0].data), json.loads(posts[1].data))
        self.assertEqual(
            [item.event_type for item in self.ledger.pending()], ["IMPORT_CONVERSATION"]
        )
        self.assertEqual(accepted["code_comment_ids"], [30])
        followup = parse_message(
            b"From: Second Reviewer <second@example.test>\nMessage-ID: <followup@example.test>\nSubject: Re: first\n"
            b"In-Reply-To: <imported@example.test>\n\n> Please change both lines.\nAgreed, and keep this context.\n"
        )
        (followup_match,) = [
            item
            for item in match_thread_replies(
                (*revision.messages, reply, followup), (revision,)
            ).matched
            if item.message.message_id == followup.message_id
        ]
        with patch.object(client, "lookup_operation", return_value=accepted):
            queued = queue_imported_reply(
                self.ledger,
                client,
                7,
                followup_match,
                patch_commits=commits,
                patch_diffs={first.message_id: PATCH},
            )
        self.assertEqual((queued.inline_count, queued.unresolved_count), (1, 0))
        (inline_reply,) = [
            item for item in self.ledger.pending() if item.event_type == "IMPORT_REPLY"
        ]
        self.assertEqual(inline_reply.payload["request"]["parent_id"], 30)
        self.assertEqual(
            inline_reply.payload["request"]["parent_body_sha256"],
            hashlib.sha256(request["comments"][0]["body"].encode()).hexdigest(),
        )
        reply_plan = plan_import_delivery(self.ledger, writer, 7, "IMPORT_REPLY")
        accepted["code_comment_ids"] = [99]
        with self.assertRaisesRegex(PublicationConflict, "accepted parent operation"):
            deliver_approved_imports(
                self.ledger,
                writer,
                7,
                "IMPORT_REPLY",
                approved_digest=reply_plan.approval_digest(),
            )
        accepted["code_comment_ids"] = [30]
        delivered_reply = deliver_approved_imports(
            self.ledger,
            writer,
            7,
            "IMPORT_REPLY",
            approved_digest=reply_plan.approval_digest(),
        )
        self.assertEqual((delivered_reply.delivered, delivered_reply.failed), (1, 0))
        self.assertTrue(posts[-1].full_url.endswith("/review-imports/pulls/7/replies"))
        accepted_reply = {
            **accepted,
            "type": "review-reply",
            "comment_id": 31,
            "operation_key": inline_reply.payload["request"]["operation_key"],
            "message_id": followup.message_id,
            "message_sha256": followup.sha256,
        }
        nested = parse_message(
            b"From: Third Reviewer <third@example.test>\nMessage-ID: <nested@example.test>\nSubject: Re: first\n"
            b"In-Reply-To: <followup@example.test>\n\n> Agreed, and keep this context.\nDone in the next revision.\n"
        )
        (nested_match,) = [
            item
            for item in match_thread_replies(
                (*revision.messages, reply, followup, nested), (revision,)
            ).matched
            if item.message.message_id == nested.message_id
        ]
        with patch.object(client, "lookup_operation", return_value=accepted_reply):
            nested_result = queue_imported_reply(
                self.ledger,
                client,
                7,
                nested_match,
                patch_commits=commits,
                patch_diffs={first.message_id: PATCH},
            )
            queued_once = self.ledger.pending()
            queue_imported_reply(
                self.ledger,
                client,
                7,
                nested_match,
                patch_commits=commits,
                patch_diffs={first.message_id: PATCH},
            )
        self.assertEqual(self.ledger.pending(), queued_once)
        self.assertEqual(
            (nested_result.inline_count, nested_result.unresolved_count), (1, 0)
        )
        with patch.object(writer, "lookup_operation", return_value=accepted_reply):
            nested_plan = plan_import_delivery(self.ledger, writer, 7, "IMPORT_REPLY")
            self.assertEqual(
                nested_plan.deliveries[0].payload["request"]["parent_id"], 31
            )
            nested_delivery = deliver_approved_imports(
                self.ledger,
                writer,
                7,
                "IMPORT_REPLY",
                approved_digest=nested_plan.approval_digest(),
            )
        self.assertEqual((nested_delivery.delivered, nested_delivery.failed), (1, 0))
        conversation = self.ledger.pending()[0]
        altered = json.loads(json.dumps(conversation.payload))
        altered["request"]["address"] = "someone-else@example.test"
        with self.ledger._connection() as connection:
            connection.execute(
                "UPDATE outbox SET payload = ? WHERE id = ?",
                (json.dumps(altered), conversation.id),
            )
        with self.assertRaisesRegex(PublicationConflict, "identity evidence"):
            plan_import_delivery(self.ledger, writer, 7, "IMPORT_CONVERSATION")
        self.assertEqual(len(posts), 4)
        with self.assertRaisesRegex(DeliveryStateConflict, "migration"):
            queue_thread_reply(self.ledger, self.publisher, 7, match)
        with self.assertRaisesRegex(ValueError, "membership"):
            queue_imported_reply(
                self.ledger,
                client,
                7,
                match,
                patch_commits=commits[::-1],
                patch_diffs={},
            )

    def test_imported_inline_reply_excerpts_use_immediate_parent_only(self):
        client = ForgeImportClient(
            self.api,
            "xen-project",
            "xen",
            source_url="https://lore.kernel.org/xen-devel/",
        )
        parent = parse_message(
            b"From: Reviewer <reviewer@example.test>\nMessage-ID: <parent-excerpts@example.test>\nSubject: Re: patch\n"
            b"In-Reply-To: <patch@example.test>\n\n"
            b"> --- a/src/sample.c\n> +++ b/src/sample.c\n> +new_call();\n"
            b"First line of review.\nSecond line of review.\nLast line of review.\n\n~Reviewer\n\n"
            b"> +duplicate();\nAnother parent remark.\nA final sentence.\n"
        )
        commits = (PatchCommit("patch@example.test", "b" * 40, "a" * 40),)
        snapshot = {
            "head_commit": "a" * 40,
            "base_commit": "b" * 40,
            "merge_base": "b" * 40,
            "before_commit": "b" * 40,
            "after_commit": "a" * 40,
            "diff": PATCH,
            "diff_sha256": hashlib.sha256(PATCH.encode()).hexdigest(),
        }
        with (
            patch.object(client, "comparison", return_value=snapshot),
            patch.object(
                client,
                "capabilities",
                return_value={"max_inline_lines": 50, "operations": ["review"]},
            ),
        ):
            queued = queue_imported_reply(
                self.ledger,
                client,
                7,
                replace(self.match, message=parent),
                patch_commits=commits,
                patch_diffs={"patch@example.test": PATCH},
            )
        self.assertEqual(queued.inline_count, 2)
        followup = parse_message(
            b"From: Second Reviewer <second@example.test>\nMessage-ID: <reply-excerpts@example.test>\nSubject: Re: patch\n"
            b"In-Reply-To: <parent-excerpts@example.test>\n\n"
            b">> +new_call();\n> First line of review. \n> Second line of review.\nFirst reply.\n\n"
            b"> Last line of review.\nSecond reply.\n\n"
            b">> Another parent remark.\nThis quotes an older level.\n"
            b"> ~Reviewer\nThis quotes an omitted signature.\n"
        )
        remote = {
            "type": "review",
            "comment_id": 20,
            "message_id": parent.message_id,
            "message_sha256": parent.sha256,
            "code_comment_ids": [30, 31],
        }
        destination = self.api.base_url + client.repository_path + "#7"
        review_effect = next(
            effect
            for effect in self.ledger.comment_effects(parent.message_id, destination)
            if effect.event_type == "IMPORT_REVIEW"
        )
        review_key = review_effect.payload["request"]["operation_key"]
        with patch.object(
            client,
            "lookup_operation",
            side_effect=lambda key: remote if key == review_key else None,
        ):
            result = queue_imported_reply(
                self.ledger,
                client,
                7,
                replace(self.match, message=followup),
                patch_commits=commits,
                patch_diffs={"patch@example.test": PATCH},
            )
            original = self.ledger.pending()
            queue_imported_reply(
                self.ledger,
                client,
                7,
                replace(self.match, message=followup),
                patch_commits=commits,
                patch_diffs={"patch@example.test": PATCH},
            )
        self.assertEqual(self.ledger.pending(), original)
        self.assertEqual((result.inline_count, result.unresolved_count), (2, 2))
        replies = [item for item in original if item.event_type == "IMPORT_REPLY"]
        self.assertEqual(
            [
                (item.payload["request"]["parent_id"], item.payload["request"]["body"])
                for item in replies
            ],
            [(30, "First reply."), (30, "Second reply.")],
        )
        for item in replies:
            self.assertEqual(
                item.payload["evidence"]["parser"], "quote-blocks-reply-excerpt-v1"
            )
            _assert_import_payload(self.ledger, item, source_url=client.source_url)

        destination = self.api.base_url + client.repository_path + "#7"
        parent_effect = next(
            effect
            for effect in self.ledger.comment_effects(parent.message_id, destination)
            if effect.event_type == "IMPORT_REVIEW"
        )
        later_effect = json.loads(json.dumps(parent_effect.payload))
        later_effect["request"]["operation_key"] = "f" * 64
        self.ledger.enqueue(
            parent.message_id,
            "IMPORT_REVIEW",
            destination,
            "later-review",
            later_effect,
        )
        with (
            patch.object(client, "lookup_operation", return_value=remote),
            patch.object(
                client,
                "capabilities",
                return_value={
                    "repository_id": 1,
                    "importer_id": 5,
                    "importer_login": "mirror-bot",
                    "operations": ["review-reply"],
                    "max_inline_lines": 50,
                },
            ),
        ):
            with self.assertRaisesRegex(PublicationConflict, "uniquely match"):
                plan_import_delivery(self.ledger, client, 7, "IMPORT_REPLY")

    def test_imported_inline_replies_keep_ambiguous_and_unaccepted_targets_unresolved(
        self,
    ):
        client = ForgeImportClient(
            self.api,
            "xen-project",
            "xen",
            source_url="https://lore.kernel.org/xen-devel/",
        )
        quote = b"> --- a/src/sample.c\n> +++ b/src/sample.c\n> +new_call();\n> +duplicate();\nSame remark.\nMore context.\n\n"
        parent = parse_message(
            b"From: Reviewer <reviewer@example.test>\nMessage-ID: <parent@example.test>\nSubject: Re: patch\n"
            b"In-Reply-To: <patch@example.test>\n\n" + quote + quote
        )
        commits = (PatchCommit("patch@example.test", "b" * 40, "a" * 40),)
        snapshot = {
            "head_commit": "a" * 40,
            "base_commit": "b" * 40,
            "merge_base": "b" * 40,
            "before_commit": "b" * 40,
            "after_commit": "a" * 40,
            "diff": PATCH,
            "diff_sha256": hashlib.sha256(PATCH.encode()).hexdigest(),
        }
        with (
            patch.object(client, "comparison", return_value=snapshot),
            patch.object(
                client,
                "capabilities",
                return_value={"max_inline_lines": 50, "operations": ["review"]},
            ),
        ):
            queued = queue_imported_reply(
                self.ledger,
                client,
                7,
                replace(self.match, message=parent),
                patch_commits=commits,
                patch_diffs={"patch@example.test": PATCH},
            )
        self.assertEqual(queued.inline_count, 2)
        followup = parse_message(
            b"From: Second Reviewer <second@example.test>\nMessage-ID: <ambiguous@example.test>\nSubject: Re: patch\n"
            b"In-Reply-To: <parent@example.test>\n\n> Same remark.\nWhich occurrence?\n"
        )
        remote = {
            "type": "review",
            "comment_id": 20,
            "message_id": parent.message_id,
            "message_sha256": parent.sha256,
            "code_comment_ids": [30, 31],
        }
        for accepted in (None, remote):
            with (
                self.subTest(accepted=accepted is not None),
                patch.object(client, "lookup_operation", return_value=accepted),
            ):
                result = queue_imported_reply(
                    self.ledger,
                    client,
                    7,
                    replace(self.match, message=followup),
                    patch_commits=commits,
                    patch_diffs={"patch@example.test": PATCH},
                )
            self.assertEqual((result.inline_count, result.unresolved_count), (0, 1))
            self.assertIsNotNone(result.diagnostic)
            if accepted is None:
                self.assertEqual(result.conversation_id, 0)
            else:
                conversation = next(
                    effect
                    for effect in self.ledger.pending()
                    if effect.id == result.conversation_id
                )
                self.assertTrue(
                    conversation.payload["request"]["body"].startswith(
                        "Which occurrence?\n\n"
                    )
                )
                self.assertNotIn(
                    "> Same remark.", conversation.payload["request"]["body"]
                )
                self.assertIn(
                    "#issuecomment-20", conversation.payload["request"]["body"]
                )
        self.assertFalse(
            any(effect.event_type == "IMPORT_REPLY" for effect in self.ledger.pending())
        )

    def test_imported_legacy_conversation_requeues_without_changing_approved_bytes(
        self,
    ):
        client = ForgeImportClient(
            self.api,
            "xen-project",
            "xen",
            source_url="https://lore.kernel.org/xen-devel/",
        )
        source = self.match.message
        self.ledger.record_parsed_message(source)
        destination = self.api.base_url + client.repository_path + "#7"
        effect_key = "imported-full-message-v1"
        payload = {
            "payload_version": 3,
            "thread": {
                "series_key": self.match.series_key,
                "revision": self.match.revision,
                "patch_message_id": self.match.patch_message_id,
            },
            "request": {
                "operation_key": hashlib.sha256(
                    f"{source.message_id}\0{effect_key}".encode()
                ).hexdigest(),
                "message_id": source.message_id,
                "message_sha256": source.sha256,
                "address": source.from_address,
                "name": source.from_name or "",
                "source_time": source.time_estimate.timestamp
                if source.time_estimate
                else None,
                "source_time_basis": "unverified"
                if source.time_estimate
                else "unknown",
                "body": quote_untrusted_text(source.body_text),
            },
        }
        self.ledger.enqueue(
            source.message_id, "IMPORT_CONVERSATION", destination, effect_key, payload
        )
        original = self.ledger.pending()
        queue_imported_reply(
            self.ledger,
            client,
            7,
            self.match,
            patch_commits=(PatchCommit("patch@example.test", "b" * 40, "a" * 40),),
            patch_diffs={},
        )
        self.assertEqual(self.ledger.pending(), original)
        capabilities = {
            "repository_id": 1,
            "importer_id": 5,
            "importer_login": "mirror-bot",
            "operations": ["conversation"],
            "max_inline_lines": 50,
        }
        with patch.object(client, "capabilities", return_value=capabilities):
            self.assertEqual(
                plan_import_delivery(
                    self.ledger, client, 7, "IMPORT_CONVERSATION"
                ).deliveries,
                tuple(original),
            )

    def test_cli_native_queue_does_not_require_b4_and_passes_range_limit(self):
        arguments = [
            "queue-comments",
            "--related",
            "--ledger",
            str(self.ledger.path),
            "--api-url",
            self.api.base_url,
            "--owner",
            "xen-project",
            "--repo",
            "xen",
            "--pull-index",
            "7",
            "--series-key",
            self.match.series_key,
            "--revision",
            str(self.match.revision),
            "--max-inline-lines",
            "50",
        ]
        with (
            patch.dict("os.environ", {"LORE_FORGE_TOKEN": "synthetic-token"}),
            patch("lore_review_companion.cli.ForgeAPI", return_value=self.api),
            patch("lore_review_companion.cli.queue_archive_replies") as queue,
            patch("lore_review_companion.cli.asdict", return_value={}),
            redirect_stdout(StringIO()),
        ):
            self.assertEqual(main(arguments), 0)
        self.assertIsNone(queue.call_args.kwargs["b4_python"])
        self.assertEqual(queue.call_args.args[1].max_inline_lines, 50)

    def test_cli_import_queue_and_delivery_use_protected_scoped_client(self):
        self.ledger.record_parsed_message(self.match.message)
        token_path = Path(self.tempdir.name) / "token"
        token_path.write_text("synthetic-token")
        token_path.chmod(0o600)
        arguments = [
            "--ledger",
            str(self.ledger.path),
            "--api-url",
            self.api.base_url,
            "--owner",
            "xen-project",
            "--repo",
            "xen",
            "--pull-index",
            "7",
            "--token-file",
            str(token_path),
        ]
        with (
            patch("lore_review_companion.cli.ForgeAPI", return_value=self.api),
            patch("lore_review_companion.cli.queue_imported_replies") as queue,
            patch("lore_review_companion.cli.asdict", return_value={}),
            redirect_stdout(StringIO()),
        ):
            self.assertEqual(
                main(
                    [
                        "queue-imports",
                        *arguments,
                        "--message-id",
                        self.match.message.message_id,
                        "--series-key",
                        self.match.series_key,
                        "--revision",
                        "1",
                        "--repository",
                        self.tempdir.name,
                    ]
                ),
                0,
            )
        self.assertIsInstance(queue.call_args.args[1], ForgeImportClient)
        self.assertFalse(queue.call_args.args[1].write_enabled)
        self.assertEqual(queue.call_args.args[6], (self.match.message,))
        with redirect_stderr(StringIO()):
            self.assertEqual(
                main(["deliver-imports", *arguments, "--kind", "review", "--write"]), 1
            )

    def test_segmented_review_adds_only_missing_remark_and_replays(self):
        diff = (
            "diff --git a/x b/x\n--- a/x\n+++ b/x\n"
            "@@ -1,1 +1,2 @@\n context\n+first();\n"
            "@@ -9,1 +10,3 @@\n later\n+log_start();\n+log_finish();\n"
        )
        reply = parse_message(
            b"From: Reviewer <reviewer@example.test>\nMessage-ID: <segmented@example.test>\n"
            b"Subject: Re: patch\nIn-Reply-To: <patch@example.test>\n\n"
            b"> --- a/x\n> +++ b/x\n> +first();\nFirst remark.\n"
            b"> +first();\n> @@ -9,1 +10,3 @@\n>  later\n"
            b"> +log_start();\n> +log_finish();\nSecond remark.\n"
        )
        match = replace(self.match, message=reply)
        client = ForgeImportClient(
            self.api,
            "xen-project",
            "xen",
            source_url="https://lore.kernel.org/xen-devel/",
            write_enabled=True,
        )
        capabilities = {
            "repository_id": 1,
            "importer_id": 5,
            "importer_login": "mirror-bot",
            "operations": ["conversation", "review"],
            "max_inline_lines": 50,
        }
        snapshot = {
            "head_commit": "a" * 40,
            "base_commit": "b" * 40,
            "merge_base": "b" * 40,
            "before_commit": "b" * 40,
            "after_commit": "a" * 40,
            "diff": diff,
            "diff_sha256": hashlib.sha256(diff.encode()).hexdigest(),
        }
        arguments = {
            "patch_commits": (PatchCommit("patch@example.test", "b" * 40, "a" * 40),),
            "patch_diffs": {"patch@example.test": diff},
        }
        with (
            patch.object(client, "capabilities", return_value=capabilities),
            patch.object(client, "comparison", return_value=snapshot),
        ):
            with patch.object(DiffIndex, "resolve_segmented_quote", return_value=None):
                old = queue_imported_reply(self.ledger, client, 7, match, **arguments)
            self.assertEqual((old.inline_count, old.unresolved_count), (1, 1))
            original = self.ledger.pending()
            for effect in original:
                self.ledger.begin_delivery(effect.id)
                self.ledger.mark_delivered(effect.id)
            result = queue_imported_reply(self.ledger, client, 7, match, **arguments)
            self.assertEqual((result.inline_count, result.unresolved_count), (2, 0))
            (new_effect,) = self.ledger.pending()
            self.assertTrue(
                new_effect.effect_key.startswith("imported-review-segment-v1:1:")
            )
            self.assertEqual(
                new_effect.payload["request"]["comments"][0]["body"], "Second remark."
            )
            self.assertEqual(
                new_effect.payload["request"]["comments"][0]["new_position"], 10
            )
            self.assertEqual(
                new_effect.payload["request"]["comments"][0]["extra_lines_count"], 2
            )
            queue_imported_reply(self.ledger, client, 7, match, **arguments)
            self.assertEqual(self.ledger.pending(), [new_effect])
            old_review = next(
                effect for effect in original if effect.event_type == "IMPORT_REVIEW"
            )
            retained = self.ledger.comment_effects(
                reply.message_id, new_effect.destination
            )
            self.assertEqual(
                next(
                    effect for effect in retained if effect.id == old_review.id
                ).payload,
                old_review.payload,
            )
            plan = plan_import_delivery(self.ledger, client, 7, "IMPORT_REVIEW")
            altered = json.loads(json.dumps(new_effect.payload))
            altered["request"]["comments"][0]["new_position"] += 1
            with self.assertRaises(PublicationConflict):
                _assert_import_payload(
                    self.ledger,
                    replace(new_effect, payload=altered),
                    source_url=client.source_url,
                )
            altered = json.loads(json.dumps(new_effect.payload))
            altered["evidence"]["remarks"][0]["quote"][2] = "+changed();"
            with self.assertRaises(PublicationConflict):
                _assert_import_payload(
                    self.ledger,
                    replace(new_effect, payload=altered),
                    source_url=client.source_url,
                )
            with self.assertRaises(PublicationConflict):
                _assert_import_payload(
                    self.ledger,
                    replace(new_effect, effect_key="imported-review-v1"),
                    source_url=client.source_url,
                )
            with patch.object(
                client, "import_review", return_value=ImportDeliveryResult({"id": 1})
            ) as send:
                delivered = deliver_approved_imports(
                    self.ledger,
                    client,
                    7,
                    "IMPORT_REVIEW",
                    approved_digest=plan.approval_digest(),
                )
            self.assertEqual(
                (delivered.delivered, delivered.failed, send.call_count), (1, 0, 1)
            )
            queue_imported_reply(self.ledger, client, 7, match, **arguments)
            self.assertEqual(self.ledger.pending(), [])

    def test_import_queue_reconstruction_checks_head_before_creating_effects(self):
        client = ForgeImportClient(
            self.api,
            "xen-project",
            "xen",
            source_url="https://lore.kernel.org/xen-devel/",
        )
        comparisons = (PatchCommit("patch@example.test", "b" * 40, "a" * 40),)
        snapshot = {"before_commit": "b" * 40, "head_commit": "a" * 40, "diff": PATCH}
        with (
            patch(
                "lore_review_companion.review_queue.assemble_local_series"
            ) as assemble,
            patch(
                "lore_review_companion.review_queue.prepare_patch_mailbox"
            ) as prepare,
            patch(
                "lore_review_companion.review_queue.queue_imported_reply",
                return_value=ReplyQueueSummary(1, 1, 0),
            ) as queue,
            patch.object(client, "comparison", return_value=snapshot),
        ):
            assemble.return_value.patch_mailbox = b"assembled patch mailbox"
            prepared = prepare.return_value.__enter__.return_value
            prepared.result.head_commit = "c" * 40
            with self.assertRaisesRegex(PublicationConflict, "Reassembled"):
                queue_imported_replies(
                    self.ledger,
                    client,
                    7,
                    self.match.series_key,
                    self.match.revision,
                    Path(self.tempdir.name),
                    (self.match.message,),
                )
            queue.assert_not_called()
            self.assertEqual(self.ledger.pending(), [])
            prepared.result.head_commit = "a" * 40
            prepared.patch_commits.return_value = comparisons
            result = queue_imported_replies(
                self.ledger,
                client,
                7,
                self.match.series_key,
                self.match.revision,
                Path(self.tempdir.name),
                (self.match.message,),
            )
            self.assertEqual((result.matched, result.inline_count), (1, 1))
            self.assertEqual(queue.call_args.kwargs["patch_commits"], comparisons)
            self.assertEqual(
                queue.call_args.kwargs["patch_diffs"], {"patch@example.test": PATCH}
            )
            self.assertEqual(assemble.call_args.kwargs["revision"], self.match.revision)

    @patch("lore_review_companion.review_queue.extract_quoted_comments")
    def test_native_parser_will_not_duplicate_existing_legacy_effects(self, extract):
        extract.return_value = self.comments
        reply = parse_message(
            b"Message-ID: <native@example.test>\nSubject: Re: patch\nIn-Reply-To: <patch@example.test>\n\n"
            b"> --- a/src/sample.c\n> +++ b/src/sample.c\n> +new_call();\nRemark\n"
        )
        match = replace(self.match, message=reply)
        queue_thread_reply(self.ledger, self.publisher, 7, match, b4_python="legacy")
        before = self.ledger.pending()
        result = queue_thread_reply(self.ledger, self.publisher, 7, match)
        self.assertIn("migration requires", result.diagnostic)
        self.assertEqual(self.ledger.pending(), before)

    @patch("lore_review_companion.review_queue.extract_quoted_comments")
    def test_queues_complete_conversation_and_only_unique_inline_match(self, extract):
        extract.return_value = self.comments
        result = queue_thread_reply(
            self.ledger, self.publisher, 7, self.match, b4_python="b4-python"
        )
        replay = queue_thread_reply(
            self.ledger, self.publisher, 7, self.match, b4_python="b4-python"
        )

        self.assertEqual((result.inline_count, result.unresolved_count), (1, 2))
        self.assertEqual(result.conversation_id, replay.conversation_id)
        pending = self.ledger.pending()
        self.assertEqual(
            [item.event_type for item in pending], ["ISSUE_COMMENT", "INLINE_COMMENT"]
        )
        self.assertIn(
            "https://lore.kernel.org/xen-devel/reply@example.test/",
            pending[0].payload["body"],
        )
        self.assertNotIn(
            "<!-- lore-gitea-mirror:v1:forged -->", pending[0].payload["body"]
        )
        self.assertIn("From (unverified):\n\n```text\n", pending[0].payload["body"])
        plan = plan_comment_delivery(self.ledger, self.publisher, 7, "ISSUE_COMMENT")
        self.assertTrue(
            plan.deliveries[0]
            .payload["body"]
            .startswith("Review mirror on behalf of @mirror-bot\n\n")
        )
        self.assertIn("From (unverified):", plan.deliveries[0].payload["body"])
        self.assertEqual(pending[1].payload["anchor"]["new_position"], 11)
        self.assertEqual(pending[1].payload["head_commit"], "a" * 40)

    @patch("lore_review_companion.review_queue.extract_quoted_comments")
    def test_source_time_labels_received_date_and_unknown(self, extract):
        extract.return_value = self.comments
        recorded = self.ledger.series_revision(
            self.match.series_key, self.match.revision
        )
        assert recorded
        cases = (
            (
                "received",
                b"Received: from relay; Thu, 22 Oct 2026 15:30:00 +0200\nDate: Thu, 22 Oct 2026 00:00:00 +0000\n",
                "Source time (unverified Received header): 2026-10-22T13:30:00+00:00",
            ),
            (
                "date",
                b"Date: Thu, 22 Oct 2026 15:30:00 +0200\n",
                "Source time (unverified Date header): 2026-10-22T13:30:00+00:00",
            ),
            (
                "unknown",
                b"Date: Thu, 22 Oct 2026 15:30:00 -0000\n",
                "Source time: unavailable",
            ),
        )
        for name, headers, expected in cases:
            with self.subTest(source=name):
                reply = parse_message(
                    f"Message-ID: <{name}@example.test>\n".encode()
                    + headers
                    + b"From: Reviewer <reviewer@example.test>\nSubject: Re: patch\n"
                    + b"In-Reply-To: <patch@example.test>\n\nPlease reconsider.\n"
                )
                (match,) = match_thread_replies(
                    (*recorded.messages, reply), (recorded,)
                ).matched
                result = queue_thread_reply(
                    self.ledger, self.publisher, 7, match, b4_python="b4-python"
                )
                self.assertEqual(result.inline_count, 1)
                deliveries = [
                    item
                    for item in self.ledger.pending()
                    if item.message_id == reply.message_id
                ]
                self.assertEqual(len(deliveries), 2)
                for delivery in deliveries:
                    self.assertIn(expected, delivery.payload["body"])
                    self.assertNotIn("verified lore receipt", delivery.payload["body"])

    @patch("lore_review_companion.review_queue.extract_quoted_comments")
    def test_replay_preserves_existing_legacy_comment_bodies(self, extract):
        extract.return_value = self.comments
        with patch(
            "lore_review_companion.review_queue._source_time_provenance",
            return_value="",
        ):
            first = queue_thread_reply(
                self.ledger, self.publisher, 7, self.match, b4_python="b4-python"
            )
        before = self.ledger.pending()
        replay = queue_thread_reply(
            self.ledger, self.publisher, 7, self.match, b4_python="b4-python"
        )
        self.assertEqual(
            (first.conversation_id, first.inline_count),
            (replay.conversation_id, replay.inline_count),
        )
        self.assertEqual(self.ledger.pending(), before)
        self.assertEqual(len(before), 2)
        for delivery in before:
            self.ledger.begin_delivery(delivery.id)
            self.ledger.mark_delivered(delivery.id)
        replay = queue_thread_reply(
            self.ledger, self.publisher, 7, self.match, b4_python="b4-python"
        )
        self.assertEqual(replay.conversation_id, first.conversation_id)
        self.assertEqual(self.ledger.pending(), [])
        with self.ledger._connection() as connection:
            rows = connection.execute(
                "SELECT id, payload FROM outbox ORDER BY id"
            ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            [json.loads(row["payload"]) for row in rows],
            [delivery.payload for delivery in before],
        )

    @patch(
        "lore_review_companion.review_queue.extract_quoted_comments", return_value=()
    )
    def test_legacy_comment_replay_rejects_other_payload_changes(self, _extract):
        with patch(
            "lore_review_companion.review_queue._source_time_provenance",
            return_value="",
        ):
            result = queue_thread_reply(
                self.ledger, self.publisher, 7, self.match, b4_python="b4-python"
            )
        with self.ledger._connection() as connection:
            row = connection.execute(
                "SELECT payload FROM outbox WHERE id = ?", (result.conversation_id,)
            ).fetchone()
            payload = json.loads(row["payload"])
            payload["thread"]["revision"] = 999
            connection.execute(
                "UPDATE outbox SET payload = ? WHERE id = ?",
                (json.dumps(payload), result.conversation_id),
            )
        with self.assertRaisesRegex(DeliveryStateConflict, "different payload"):
            queue_thread_reply(
                self.ledger, self.publisher, 7, self.match, b4_python="b4-python"
            )
        self.assertEqual(len(self.ledger.pending()), 1)

    @patch("lore_review_companion.review_queue.extract_quoted_comments")
    def test_changed_pr_diff_retains_conversation_without_inline(self, extract):
        extract.return_value = self.comments
        self.transport.change_head_after_diff = True
        result = queue_thread_reply(
            self.ledger, self.publisher, 7, self.match, b4_python="b4-python"
        )
        self.assertEqual((result.inline_count, result.unresolved_count), (0, 3))
        self.assertIn("changed", result.diagnostic)
        self.assertEqual(
            [item.event_type for item in self.ledger.pending()], ["ISSUE_COMMENT"]
        )

    @patch(
        "lore_review_companion.review_queue.extract_quoted_comments",
        side_effect=B4ReviewError("unsupported b4"),
    )
    def test_parser_failure_preserves_full_reply(self, _extract):
        result = queue_thread_reply(
            self.ledger, self.publisher, 7, self.match, b4_python="b4-python"
        )
        self.assertIn("unsupported b4", result.diagnostic)
        self.assertEqual(len(self.ledger.pending()), 1)

    def test_rejects_unmapped_pull_request(self):
        with self.assertRaisesRegex(ValueError, "recorded publication"):
            queue_thread_reply(
                self.ledger, self.publisher, 8, self.match, b4_python="b4-python"
            )
        self.assertEqual(self.ledger.pending(), [])

    def test_reply_ingestion_uses_the_publication_lock(self):
        with self.ledger.worker_lock(), self.assertRaises(WorkerBusyError):
            queue_thread_reply(
                self.ledger, self.publisher, 7, self.match, b4_python="b4-python"
            )

    @patch(
        "lore_review_companion.review_queue.extract_quoted_comments", return_value=()
    )
    def test_discovers_nested_stored_replies_and_excludes_unrelated_mail(
        self, _extract
    ):
        nested = parse_message(
            b"Message-ID: <nested@example.test>\nSubject: Re: patch\n"
            b"In-Reply-To: <reply@example.test>\n\nNested.\n"
        )
        unrelated = parse_message(
            b"Message-ID: <unrelated@example.test>\nSubject: Re: patch\n"
            b"In-Reply-To: <other@example.test>\n\nUnrelated.\n"
        )
        for item in (nested, unrelated, self.match.message):
            self.ledger.record_parsed_message(item)
        result = queue_archive_replies(
            self.ledger,
            self.publisher,
            7,
            self.match.series_key,
            self.match.revision,
            b4_python="b4-python",
            limit=1,
        )
        self.assertEqual(
            (result.matched, result.unmatched, result.has_more), (1, (), True)
        )
        reopened = Ledger(self.ledger.path)
        continued = queue_archive_replies(
            reopened,
            self.publisher,
            7,
            self.match.series_key,
            self.match.revision,
            b4_python="b4-python",
            limit=1,
            cursor=result.next_cursor,
        )
        self.assertEqual(
            (continued.matched, continued.has_more, continued.next_cursor),
            (1, False, None),
        )
        self.assertEqual(
            {item.message_id for item in self.ledger.pending()},
            {"reply@example.test", "nested@example.test"},
        )
        replay = queue_archive_replies(
            reopened,
            self.publisher,
            7,
            self.match.series_key,
            self.match.revision,
            b4_python="b4-python",
            limit=1,
        )
        self.assertEqual(replay.next_cursor, result.next_cursor)
        self.assertEqual(len(self.ledger.pending()), 2)

    @patch(
        "lore_review_companion.review_queue.extract_quoted_comments", return_value=()
    )
    def test_related_reply_cursor_rejects_changed_ledger_and_destination(
        self, _extract
    ):
        nested = message(
            "nested@example.test",
            "Re: patch",
            headers="In-Reply-To: <reply@example.test>\n",
        )
        for item in (self.match.message, nested):
            self.ledger.record_parsed_message(item)
        first = queue_archive_replies(
            self.ledger,
            self.publisher,
            7,
            self.match.series_key,
            self.match.revision,
            b4_python="b4-python",
            limit=1,
        )
        other_publisher = ForgeCommentPublisher(self.api, "other", "xen")
        with self.assertRaisesRegex(ValueError, "snapshot or destination"):
            queue_archive_replies(
                self.ledger,
                other_publisher,
                7,
                self.match.series_key,
                self.match.revision,
                b4_python="b4-python",
                limit=1,
                cursor=first.next_cursor,
            )
        self.ledger.record_parsed_message(message("new@example.test", "Unrelated"))
        with self.assertRaisesRegex(ValueError, "snapshot or destination"):
            queue_archive_replies(
                self.ledger,
                self.publisher,
                7,
                self.match.series_key,
                self.match.revision,
                b4_python="b4-python",
                limit=1,
                cursor=first.next_cursor,
            )
        self.assertEqual(len(self.ledger.pending()), 1)

    @patch(
        "lore_review_companion.review_queue.extract_quoted_comments", return_value=()
    )
    def test_related_reply_batch_resolves_thread_once(self, _extract):
        nested = message(
            "nested@example.test",
            "Re: patch",
            headers="In-Reply-To: <reply@example.test>\n",
        )
        for item in (self.match.message, nested):
            self.ledger.record_parsed_message(item)
        with patch.object(
            self.ledger,
            "_related_thread_messages",
            wraps=self.ledger._related_thread_messages,
        ) as discover:
            result = queue_archive_replies(
                self.ledger,
                self.publisher,
                7,
                self.match.series_key,
                self.match.revision,
                b4_python="b4-python",
                limit=2,
            )
        self.assertEqual(result.matched, 2)
        self.assertEqual(discover.call_count, 1)

    @patch(
        "lore_review_companion.review_queue.extract_quoted_comments", return_value=()
    )
    def test_supplied_reply_batch_rechecks_known_parents_before_queueing(
        self, _extract
    ):
        unrelated = message("other@example.test", "Unrelated")
        reply = message(
            "waiting@example.test",
            "Re: patch",
            headers="References: <patch@example.test>\nIn-Reply-To: <other@example.test>\n",
        )
        self.ledger.record_parsed_message(unrelated)
        with self.assertRaisesRegex(ValueError, "thread ancestry changed"):
            queue_thread_replies(
                self.ledger,
                self.publisher,
                7,
                self.match.series_key,
                self.match.revision,
                [reply],
                b4_python="b4-python",
            )
        self.assertEqual(self.ledger.pending(), [])

    @patch(
        "lore_review_companion.review_queue.extract_quoted_comments", return_value=()
    )
    def test_discovery_does_not_ignore_a_known_unrelated_direct_parent(self, _extract):
        unrelated = parse_message(
            b"Message-ID: <other@example.test>\nSubject: unrelated\n\nOther.\n"
        )
        ambiguous = parse_message(
            b"Message-ID: <ambiguous@example.test>\nSubject: Re: patch\n"
            b"References: <patch@example.test>\nIn-Reply-To: <other@example.test>\n\nReview.\n"
        )
        for item in (ambiguous, unrelated):
            self.ledger.record_parsed_message(item)
        result = queue_archive_replies(
            self.ledger,
            self.publisher,
            7,
            self.match.series_key,
            self.match.revision,
            b4_python="b4-python",
        )
        self.assertEqual(result.matched, 0)
        self.assertEqual(result.unmatched, ("ambiguous@example.test",))
        self.assertEqual(self.ledger.pending(), [])

    @patch(
        "lore_review_companion.review_queue.extract_quoted_comments", return_value=()
    )
    def test_queued_comment_is_blocked_when_its_series_later_becomes_conflicted(
        self, _extract
    ):
        queue_thread_reply(
            self.ledger, self.publisher, 7, self.match, b4_python="b4-python"
        )
        writer = ForgeCommentPublisher(
            self.api, "xen-project", "xen", write_enabled=True
        )
        plan = plan_comment_delivery(self.ledger, writer, 7, "ISSUE_COMMENT")
        with self.assertRaises(MessageConflictError):
            self.ledger.record_message("patch@example.test", b"different patch")
        with self.assertRaises(MessageConflictError):
            deliver_approved_comments(
                self.ledger,
                writer,
                7,
                "ISSUE_COMMENT",
                approved_digest=plan.approval_digest(),
            )
        self.assertEqual(self.transport.issue_comments, [])

    @patch(
        "lore_review_companion.review_queue.extract_quoted_comments", return_value=()
    )
    def test_queued_comment_is_blocked_when_series_membership_changes(self, _extract):
        queue_thread_reply(
            self.ledger, self.publisher, 7, self.match, b4_python="b4-python"
        )
        extra = message(
            "extra@example.test",
            "[PATCH 1/1] duplicate",
            headers="In-Reply-To: <patch@example.test>\n",
        )
        self.ledger.record_parsed_message(extra)
        with self.assertRaisesRegex(PublicationConflict, "membership changed"):
            plan_comment_delivery(self.ledger, self.publisher, 7, "ISSUE_COMMENT")
        self.assertEqual(self.transport.issue_comments, [])

    @patch(
        "lore_review_companion.review_queue.extract_quoted_comments", return_value=()
    )
    def test_reply_waits_for_missing_parent_before_queueing(self, _extract):
        reply = parse_message(
            b"Message-ID: <waiting@example.test>\nSubject: Re: patch\n"
            b"References: <patch@example.test>\nIn-Reply-To: <missing@example.test>\n\nReview.\n"
        )
        recorded = self.ledger.series_revision(
            self.match.series_key, self.match.revision
        )
        assert recorded
        match = match_thread_replies((*recorded.messages, reply), (recorded,)).matched[
            0
        ]
        with self.assertRaisesRegex(PublicationConflict, "ancestry is incomplete"):
            queue_thread_reply(
                self.ledger, self.publisher, 7, match, b4_python="b4-python"
            )
        self.assertEqual(self.ledger.pending(), [])
        parent = parse_message(
            b"Message-ID: <missing@example.test>\nSubject: Re: patch\n"
            b"In-Reply-To: <patch@example.test>\n\nEarlier review.\n"
        )
        self.ledger.record_parsed_message(parent)
        match = next(
            item
            for item in match_thread_replies(
                (*recorded.messages, parent, reply), (recorded,)
            ).matched
            if item.message.message_id == reply.message_id
        )
        queue_thread_reply(self.ledger, self.publisher, 7, match, b4_python="b4-python")
        self.assertEqual(len(self.ledger.pending()), 1)

    @patch(
        "lore_review_companion.review_queue.extract_quoted_comments", return_value=()
    )
    def test_new_direct_parent_invalidates_previously_queued_reply(self, _extract):
        reply = parse_message(
            b"Message-ID: <waiting@example.test>\nSubject: Re: patch\n"
            b"References: <patch@example.test>\nIn-Reply-To: <missing@example.test>\n\nReview.\n"
        )
        recorded = self.ledger.series_revision(
            self.match.series_key, self.match.revision
        )
        assert recorded
        match = match_thread_replies((*recorded.messages, reply), (recorded,)).matched[
            0
        ]
        writer = ForgeCommentPublisher(
            self.api, "xen-project", "xen", write_enabled=True
        )
        with patch.object(self.ledger, "assert_message_ancestry_available"):
            queue_thread_reply(
                self.ledger, self.publisher, 7, match, b4_python="b4-python"
            )
            plan = plan_comment_delivery(self.ledger, writer, 7, "ISSUE_COMMENT")
        with self.assertRaisesRegex(PublicationConflict, "ancestry is incomplete"):
            plan_comment_delivery(self.ledger, writer, 7, "ISSUE_COMMENT")
        self.ledger.record_parsed_message(
            parse_message(
                b"Message-ID: <missing@example.test>\nSubject: unrelated\n\nOther thread.\n"
            )
        )
        with self.assertRaisesRegex(PublicationConflict, "thread ancestry changed"):
            deliver_approved_comments(
                self.ledger,
                writer,
                7,
                "ISSUE_COMMENT",
                approved_digest=plan.approval_digest(),
            )
        self.assertEqual(self.transport.issue_comments, [])

    @patch("lore_review_companion.review_queue.read_local_review_drafts")
    def test_queues_local_drafts_without_sending_or_inferring_approval(
        self, read_drafts
    ):
        read_drafts.return_value = DraftSnapshot(
            "b4/review/example",
            "d" * 40,
            "reviewer@example.test",
            1,
            "change",
            (
                LocalDraft(
                    1,
                    "patch@example.test",
                    "General draft",
                    ("Reviewed-by: Reviewer",),
                    self.comments,
                ),
            ),
        )
        arguments = (
            self.ledger,
            self.publisher,
            7,
            self.match.series_key,
            1,
            Path(self.tempdir.name),
            "b4/review/example",
            "reviewer@example.test",
        )
        result = queue_b4_drafts(*arguments, b4_python="b4-python")
        replay = queue_b4_drafts(*arguments, b4_python="b4-python")
        self.assertEqual(
            (result.queued, result.inline_count, result.unresolved_count), (1, 1, 2)
        )
        self.assertEqual(result, replay)
        pending = self.ledger.pending()
        self.assertEqual(
            [item.event_type for item in pending], ["ISSUE_COMMENT", "INLINE_COMMENT"]
        )
        self.assertIn("no email sent by this integration", pending[0].payload["body"])
        self.assertIn("Reviewed-by: Reviewer", pending[0].payload["body"])
        self.assertIn("not a Gitea review state", pending[0].payload["body"])
        self.assertEqual(self.transport.issue_comments, [])
        self.assertEqual(self.transport.reviews, [])

    @patch("lore_review_companion.review_queue.read_local_review_drafts")
    def test_rejects_drafts_for_wrong_source_or_revision(self, read_drafts):
        arguments = (
            self.ledger,
            self.publisher,
            7,
            self.match.series_key,
            1,
            Path(self.tempdir.name),
            "b4/review/example",
            "reviewer@example.test",
        )
        read_drafts.return_value = DraftSnapshot(
            "b4/review/example",
            "d" * 40,
            "reviewer@example.test",
            2,
            "change",
            (),
        )
        with self.assertRaisesRegex(ValueError, "revision"):
            queue_b4_drafts(*arguments, b4_python="b4-python")
        read_drafts.return_value = DraftSnapshot(
            "b4/review/example",
            "d" * 40,
            "reviewer@example.test",
            1,
            "change",
            (LocalDraft(1, "other@example.test", "Draft", (), ()),),
        )
        with self.assertRaisesRegex(ValueError, "source"):
            queue_b4_drafts(*arguments, b4_python="b4-python")
        self.assertEqual(self.ledger.pending(), [])

    def test_legacy_migration_requires_mapping_review_and_untouched_state(self):
        self.ledger.record_parsed_message(self.match.message)
        legacy = "xen-project/xen#7"
        delivery_id = self.ledger.enqueue(
            self.match.message.message_id,
            "ISSUE_COMMENT",
            legacy,
            "full",
            {"body": "Review"},
        )
        real_request = self.api.request_json

        def request(path, **kwargs):
            if path.endswith("/pulls/7"):
                return {
                    "number": 7,
                    "body": _series_marker(self.match.series_key),
                    "head": {"ref": "lore/branch", "sha": "a" * 40, "repo_id": 42},
                    "base": {"repo_id": 42},
                }
            if path == self.publisher.repository_path:
                return {"id": 42}
            return real_request(path, **kwargs)

        with patch.object(self.api, "request_json", side_effect=request):
            plan = plan_legacy_migration(
                self.ledger, self.publisher, 7, "verified source"
            )
            with self.assertRaisesRegex(ValueError, "does not match"):
                migrate_legacy_deliveries(
                    self.ledger,
                    self.publisher,
                    7,
                    "different reason",
                    approved_digest=plan.approval_digest(),
                )
            result = migrate_legacy_deliveries(
                self.ledger,
                self.publisher,
                7,
                "verified source",
                approved_digest=plan.approval_digest(),
            )
        self.assertEqual(result.migrated, 1)
        self.assertEqual(self.ledger.pending(destination=legacy), [])
        with self.ledger._connection() as connection:
            audit = connection.execute(
                "SELECT delivery_id, previous_destination, actor FROM outbox_destination_audit"
            ).fetchone()
        self.assertEqual(tuple(audit), (delivery_id, legacy, "mirror-bot (id 5)"))
        (migrated,) = self.ledger.pending(
            destination=self.api.base_url + self.publisher.repository_path + "#7"
        )
        self.assertEqual(
            migrated.payload["thread"]["patch_message_id"], "patch@example.test"
        )

    def test_old_queued_reply_without_thread_provenance_is_not_delivered(self):
        self.ledger.record_parsed_message(self.match.message)
        destination = self.api.base_url + self.publisher.repository_path + "#7"
        delivery_id = self.ledger.enqueue(
            self.match.message.message_id,
            "ISSUE_COMMENT",
            destination,
            "legacy",
            {"body": "Review"},
        )
        with self.assertRaisesRegex(
            PublicationConflict, "lacks verified thread provenance"
        ):
            plan_comment_delivery(self.ledger, self.publisher, 7, "ISSUE_COMMENT")

        real_request = self.api.request_json

        def request(path, **kwargs):
            if path.endswith("/pulls/7"):
                return {
                    "number": 7,
                    "body": _series_marker(self.match.series_key),
                    "head": {"ref": "lore/branch", "sha": "a" * 40, "repo_id": 42},
                    "base": {"repo_id": 42},
                }
            if path == self.publisher.repository_path:
                return {"id": 42}
            return real_request(path, **kwargs)

        with redirect_stdout(StringIO()) as output:
            self.assertEqual(
                main(
                    ["list-outbox", "--ledger", str(self.ledger.path), "--limit", "1"]
                ),
                0,
            )
        self.assertEqual(
            json.loads(output.getvalue())["deliveries"][0]["id"], delivery_id
        )
        with patch.object(self.api, "request_json", side_effect=request):
            plan = plan_legacy_migration(
                self.ledger,
                self.publisher,
                7,
                "verified source",
                scoped_delivery_ids=(delivery_id,),
            )
            result = migrate_legacy_deliveries(
                self.ledger,
                self.publisher,
                7,
                "verified source",
                approved_digest=plan.approval_digest(),
                scoped_delivery_ids=(delivery_id,),
            )
            self.assertEqual(result.migrated, 1)
            with self.assertRaisesRegex(
                DeliveryStateConflict, "lack thread provenance"
            ):
                plan_legacy_migration(
                    self.ledger,
                    self.publisher,
                    7,
                    "verified source",
                    scoped_delivery_ids=(delivery_id,),
                )
        self.assertEqual(
            self.ledger.pending()[0].payload["thread"]["patch_message_id"],
            "patch@example.test",
        )
        plan_comment_delivery(self.ledger, self.publisher, 7, "ISSUE_COMMENT")

    def test_legacy_migration_rejects_changed_pull_uncertain_rows_and_collisions(self):
        self.ledger.record_parsed_message(self.match.message)
        legacy = "xen-project/xen#7"
        destination = self.api.base_url + self.publisher.repository_path + "#7"
        uncertain = self.ledger.enqueue(
            self.match.message.message_id,
            "ISSUE_COMMENT",
            legacy,
            "uncertain",
            {"body": "Review"},
        )
        self.ledger.begin_delivery(uncertain)
        self.ledger.mark_delivery_uncertain(uncertain, "connection lost")
        real_request = self.api.request_json
        pull = {
            "number": 7,
            "body": _series_marker(self.match.series_key),
            "head": {"ref": "lore/branch", "sha": "a" * 40, "repo_id": 42},
            "base": {"repo_id": 42},
        }

        def request(path, **kwargs):
            if path.endswith("/pulls/7"):
                return pull
            if path == self.publisher.repository_path:
                return {"id": 42}
            return real_request(path, **kwargs)

        with patch.object(self.api, "request_json", side_effect=request):
            pull["body"] = "no provenance"
            with self.assertRaisesRegex(ValueError, "does not match"):
                plan_legacy_migration(self.ledger, self.publisher, 7, "verified source")
            pull["body"] = _series_marker(self.match.series_key)
            pull["head"]["sha"] = "b" * 40
            with self.assertRaisesRegex(ValueError, "does not match"):
                plan_legacy_migration(self.ledger, self.publisher, 7, "verified source")
            pull["head"]["sha"] = "a" * 40
            with self.assertRaisesRegex(DeliveryStateConflict, "untouched pending"):
                plan_legacy_migration(self.ledger, self.publisher, 7, "verified source")
            self.ledger.mark_delivered(uncertain)
            collision = self.ledger.enqueue(
                self.match.message.message_id,
                "ISSUE_COMMENT",
                legacy,
                "full",
                {"body": "Review"},
            )
            self.ledger.enqueue(
                self.match.message.message_id,
                "ISSUE_COMMENT",
                destination,
                "full",
                {"body": "Review"},
            )
            plan = plan_legacy_migration(
                self.ledger, self.publisher, 7, "verified source"
            )
            with self.assertRaisesRegex(DeliveryStateConflict, "already contains"):
                migrate_legacy_deliveries(
                    self.ledger,
                    self.publisher,
                    7,
                    "verified source",
                    approved_digest=plan.approval_digest(),
                )
        self.assertEqual(
            [item.id for item in self.ledger.pending(destination=legacy)], [collision]
        )
        with self.ledger._connection() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM outbox_destination_audit"
                ).fetchone()[0],
                0,
            )

    @patch(
        "lore_review_companion.review_queue.extract_quoted_comments", return_value=()
    )
    def test_batch_uses_recorded_series_and_preserves_unmatched_replies(self, _extract):
        unrelated = parse_message(
            b"Message-ID: <unmatched@example.test>\nSubject: Re: patch\n"
            b"In-Reply-To: <other@example.test>\n\nUnrelated.\n"
        )
        unkeyed = parse_message(b"Subject: Re: patch\n\nMissing Message-ID.\n")
        result = queue_thread_replies(
            self.ledger,
            self.publisher,
            7,
            self.match.series_key,
            self.match.revision,
            [unrelated, unkeyed, self.match.message],
            b4_python="b4-python",
        )
        self.assertEqual(result.matched, 1)
        self.assertEqual(result.unmatched, ("unmatched@example.test",))
        self.assertEqual(result.unkeyed, 1)
        self.assertEqual(len(self.ledger.pending()), 1)
        self.assertEqual(len(self.ledger.unmatched_messages()), 1)

    @patch(
        "lore_review_companion.review_queue.extract_quoted_comments", return_value=()
    )
    def test_batch_rejects_forged_series_member_before_queueing(self, _extract):
        forged_patch = message("patch@example.test", "[PATCH 1/1] different")
        with self.assertRaises(MessageConflictError):
            queue_thread_replies(
                self.ledger,
                self.publisher,
                7,
                self.match.series_key,
                self.match.revision,
                [forged_patch, self.match.message],
                b4_python="b4-python",
            )
        self.assertEqual(self.ledger.pending(), [])

    def test_quarantined_reply_cannot_be_queued_on_original_replay(self):
        self.ledger.record_parsed_message(self.match.message)
        with self.assertRaises(MessageConflictError):
            self.ledger.record_message(
                self.match.message.message_id, b"conflicting reply"
            )
        with self.assertRaisesRegex(MessageConflictError, "Quarantined"):
            queue_thread_reply(
                self.ledger, self.publisher, 7, self.match, b4_python="b4-python"
            )
        self.assertEqual(self.ledger.pending(), [])

    def test_batch_rejects_inconsistent_recorded_membership(self):
        extra = message(
            "extra@example.test",
            "[PATCH 1/1] xen: change",
            headers="References: <patch@example.test>\n",
        )
        self.ledger.record_parsed_message(extra)
        (revision,) = discover_series([extra])
        with self.assertRaisesRegex(PublicationConflict, "membership is immutable"):
            self.ledger.record_series_revision(revision)
        with self.ledger._connection() as connection:
            connection.execute(
                "INSERT INTO series_messages (message_id, series_key, revision, role, patch_index) VALUES (?, ?, ?, 'patch', 1)",
                (extra.message_id, self.match.series_key, self.match.revision),
            )
        with self.assertRaisesRegex(ValueError, "complete recorded series"):
            queue_thread_replies(
                self.ledger,
                self.publisher,
                7,
                self.match.series_key,
                self.match.revision,
                [self.match.message],
                b4_python="b4-python",
            )
        self.assertEqual(self.ledger.pending(), [])

    @patch(
        "lore_review_companion.review_queue.extract_quoted_comments", return_value=()
    )
    def test_cli_queues_previews_and_requires_approved_delivery(self, _extract):
        common = [
            "--ledger",
            str(self.ledger.path),
            "--api-url",
            self.api.base_url,
            "--owner",
            "xen-project",
            "--repo",
            "xen",
            "--pull-index",
            "7",
        ]
        queue = [
            "queue-comments",
            *common,
            "--mbox",
            "thread.mbox",
            "--series-key",
            self.match.series_key,
            "--revision",
            str(self.match.revision),
            "--b4-python",
            "b4-python",
        ]
        delivery = ["deliver-comments", *common, "--kind", "conversation"]
        reply = parse_message(
            b"Received: from relay; Thu, 22 Oct 2026 15:30:00 +0200\n"
            + self.match.message.raw_message,
        )
        source_time = (
            "Source time (unverified Received header): 2026-10-22T13:30:00+00:00"
        )
        with (
            patch.dict("os.environ", {"LORE_FORGE_TOKEN": "sample-token"}),
            patch("lore_review_companion.cli.ForgeAPI", return_value=self.api),
            patch("lore_review_companion.cli.parse_mbox", return_value=(reply,)),
        ):
            with redirect_stdout(StringIO()) as output:
                self.assertEqual(main(queue), 0)
            self.assertEqual(json.loads(output.getvalue())["matched"], 1)
            with redirect_stderr(StringIO()):
                self.assertEqual(main([*delivery, "--write"]), 1)
            with redirect_stdout(StringIO()) as output:
                self.assertEqual(main(delivery), 0)
            preview = json.loads(output.getvalue())
            self.assertEqual(preview["plan"]["actor"], "mirror-bot")
            self.assertEqual(len(preview["plan"]["deliveries"]), 1)
            self.assertIn(
                source_time, preview["plan"]["deliveries"][0]["payload"]["body"]
            )
            self.assertFalse(
                any(
                    request.get_method() == "POST"
                    for request in self.transport.requests
                )
            )
            with redirect_stdout(StringIO()) as output:
                self.assertEqual(
                    main(
                        [*delivery, "--write", "--approve", preview["approval_digest"]]
                    ),
                    0,
                )
            self.assertEqual(json.loads(output.getvalue())["delivered"], 1)
            self.assertEqual(len(self.transport.issue_comments), 1)
            self.assertIn(source_time, self.transport.issue_comments[0]["body"])
            with redirect_stdout(StringIO()) as output:
                self.assertEqual(main(queue), 0)
            self.assertEqual(json.loads(output.getvalue())["matched"], 1)
            self.assertEqual(len(self.transport.issue_comments), 1)
            self.assertEqual(
                sum(
                    request.get_method() == "POST"
                    for request in self.transport.requests
                ),
                1,
            )

    @patch(
        "lore_review_companion.review_queue.extract_quoted_comments", return_value=()
    )
    def test_cli_queues_replies_selected_from_the_archive_ledger(self, _extract):
        self.ledger.record_parsed_message(self.match.message)
        arguments = [
            "queue-comments",
            "--ledger",
            str(self.ledger.path),
            "--api-url",
            self.api.base_url,
            "--owner",
            "xen-project",
            "--repo",
            "xen",
            "--pull-index",
            "7",
            "--series-key",
            self.match.series_key,
            "--revision",
            str(self.match.revision),
            "--b4-python",
            "b4-python",
            "--message-id",
            self.match.message.message_id,
        ]
        with (
            patch.dict("os.environ", {"LORE_FORGE_TOKEN": "sample-token"}),
            patch("lore_review_companion.cli.ForgeAPI", return_value=self.api),
            redirect_stdout(StringIO()) as output,
        ):
            self.assertEqual(main(arguments), 0)
        self.assertEqual(json.loads(output.getvalue())["matched"], 1)
        self.assertEqual(len(self.ledger.pending()), 1)

    @patch(
        "lore_review_companion.review_queue.extract_quoted_comments", return_value=()
    )
    def test_cli_reports_failed_comment_delivery_with_nonzero_exit(self, _extract):
        queue_thread_reply(
            self.ledger, self.publisher, 7, self.match, b4_python="b4-python"
        )
        arguments = [
            "deliver-comments",
            "--ledger",
            str(self.ledger.path),
            "--api-url",
            self.api.base_url,
            "--owner",
            "xen-project",
            "--repo",
            "xen",
            "--pull-index",
            "7",
            "--kind",
            "conversation",
        ]
        with (
            patch.dict("os.environ", {"LORE_FORGE_TOKEN": "sample-token"}),
            patch("lore_review_companion.cli.ForgeAPI", return_value=self.api),
        ):
            with redirect_stdout(StringIO()) as output:
                self.assertEqual(main(arguments), 0)
            digest = json.loads(output.getvalue())["approval_digest"]
            self.transport.fail_without_issue_write = True
            with redirect_stdout(StringIO()) as output:
                self.assertEqual(main([*arguments, "--write", "--approve", digest]), 1)
        self.assertEqual(json.loads(output.getvalue())["failed"], 1)
        self.assertEqual(self.ledger.pending()[0].write_state, "uncertain")

    @patch(
        "lore_review_companion.review_queue.extract_quoted_comments", return_value=()
    )
    def test_identity_cli_verifies_account_and_comment_labels_unverified_sender(
        self, _extract
    ):
        self.transport.users["reviewer"] = {"id": 7, "login": "reviewer"}
        common = ["--ledger", str(self.ledger.path), "--api-url", self.api.base_url]
        with (
            patch.dict("os.environ", {"LORE_FORGE_TOKEN": "sample-token"}),
            patch("lore_review_companion.cli.ForgeAPI", return_value=self.api),
            redirect_stdout(StringIO()) as output,
        ):
            self.assertEqual(
                main(
                    [
                        "map-identity",
                        *common,
                        "--email",
                        "reviewer@example.test",
                        "--username",
                        "reviewer",
                        "--reason",
                        "operator verified account",
                    ]
                ),
                0,
            )
        self.assertEqual(json.loads(output.getvalue())["user_id"], 7)
        queue_thread_reply(
            self.ledger, self.publisher, 7, self.match, b4_python="b4-python"
        )
        body = self.ledger.pending()[0].payload["body"]
        self.assertIn(
            "Operator-mapped Gitea account (unverified sender):\n\n```text\nreviewer\n```",
            body,
        )

    @patch(
        "lore_review_companion.review_queue.extract_quoted_comments", return_value=()
    )
    def test_identity_mapping_falls_back_when_account_id_changes(self, _extract):
        self.ledger.set_identity_mapping(
            self.api.base_url,
            "reviewer@example.test",
            7,
            "reviewer",
            actor="operator",
            reason="verified",
        )
        self.transport.users["reviewer"] = {"id": 8, "login": "reviewer"}
        queue_thread_reply(
            self.ledger, self.publisher, 7, self.match, b4_python="b4-python"
        )
        body = self.ledger.pending()[0].payload["body"]
        self.assertNotIn("Operator-mapped Gitea account", body)
        self.assertIn("From (unverified)", body)


if __name__ == "__main__":
    unittest.main()
