import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lore_review_companion.mail_parser import nntp_matches_source, parse_message, parse_mbox


def make_message(
    *,
    message_id="<patch-1@example.test>",
    subject="[PATCH v2 1/3] xen: tighten bounds",
    headers="",
    body="Fix the bounds check.\n\nReviewed-by: Ada Maintainer <ada@example.test>\n---\ndiff --git a/x b/x\n",
):
    return (
        f"From: Ada Maintainer <ada@example.test>\n"
        f"Message-ID: {message_id}\n"
        f"Subject: {subject}\n"
        f"{headers}"
        "MIME-Version: 1.0\n"
        "Content-Type: text/plain; charset=utf-8\n"
        "\n"
        f"{body}"
    ).encode()


class MailParserTests(unittest.TestCase):
    def test_nntp_equivalence_only_accepts_known_transport_transformations(self):
        source = b"Message-ID: <mail@example.test>\nSubject: a folded\n subject\n\nbody\n"
        article = (
            b"Path: server\r\nNewsgroups: example.group\r\n"
            b"Message-ID: <mail@example.test>\r\nSubject: a folded subject\r\n"
            b"Xref: server example.group:1\r\n\r\nbody\r\n"
        )
        self.assertTrue(nntp_matches_source(article, source))
        changes = (
            (article.replace(b"body\r\n", b"changed\r\n"), source),
            (article.replace(b"folded subject", b"other subject"), source),
            (article.replace(b"Path: server\r\n", b""), source),
            (article.replace(b"Path: server\r\n", b"Path: server\r\nPath: other\r\n"), source),
            (article, b"Path: original\n" + source),
            (article, source.replace(b"\n\n", b"\n")),
        )
        for changed_article, changed_source in changes:
            with self.subTest(article=changed_article, source=changed_source):
                self.assertFalse(nntp_matches_source(changed_article, changed_source))

    def test_rejects_excessive_thread_headers(self):
        references = " ".join(f"<{index}@example.test>" for index in range(1001))
        raw = f"Message-ID: <reply@example.test>\nReferences: {references}\n\nbody\n".encode()
        with self.assertRaisesRegex(ValueError, "1000 Message-ID"):
            parse_message(raw)
        raw = b"Message-ID: <reply@example.test>\nReferences: " + b"x" * (64 * 1024 + 1) + b"\n\nbody\n"
        with self.assertRaisesRegex(ValueError, "64 KiB"):
            parse_message(raw)

    def test_patch_metadata_thread_headers_and_trailers(self):
        raw_message = make_message(
            headers=(
                "References: <cover@example.test>\n"
                " <patch-0@example.test>\n"
                "In-Reply-To: <patch-0@example.test>\n"
            )
        )

        parsed = parse_message(raw_message)

        self.assertEqual(parsed.raw_message, raw_message)
        self.assertEqual(parsed.sha256, hashlib.sha256(raw_message).hexdigest())
        self.assertEqual(parsed.message_id, "patch-1@example.test")
        self.assertEqual(parsed.from_address, "ada@example.test")
        self.assertEqual(parsed.kind, "patch")
        self.assertEqual(parsed.patch_version, 2)
        self.assertEqual(parsed.patch_index, 1)
        self.assertEqual(parsed.series_size, 3)
        self.assertEqual(parsed.references, ("cover@example.test", "patch-0@example.test"))
        self.assertEqual(parsed.in_reply_to, ("patch-0@example.test",))
        self.assertEqual(parsed.trailers[0].name, "Reviewed-by")
        self.assertIn("Ada Maintainer", parsed.trailers[0].value)
        self.assertFalse(parsed.errors)

    def test_cover_and_reply_classification(self):
        cover = parse_message(
            make_message(subject="[PATCH v3 0/4] xen: improve scheduling")
        )
        reply = parse_message(
            make_message(
                subject="Re: scheduling series",
                headers="In-Reply-To: <patch-1@example.test>\n",
                body="Looks good.\n",
            )
        )

        self.assertEqual((cover.kind, cover.patch_index, cover.series_size), ("cover", 0, 4))
        self.assertEqual(reply.kind, "reply")
        self.assertEqual(reply.in_reply_to, ("patch-1@example.test",))

    def test_missing_message_id_is_retained_with_diagnostic(self):
        parsed = parse_message(make_message(message_id=""))

        self.assertIsNone(parsed.message_id)
        self.assertIn("Missing or invalid Message-ID", parsed.errors)
        self.assertTrue(parsed.raw_message)

    def test_time_estimate_prefers_first_valid_received_then_sender_date(self):
        received = parse_message(make_message(headers=(
            "Received: malformed\n"
            "Received: from relay; Wed, 28 Oct 2026 12:34:56 +0200\n"
            "Received: from sender; Wed, 28 Oct 2026 08:00:00 +0000\n"
            "Date: Wed, 28 Oct 2026 09:00:00 +0000\n"
        ))).time_estimate
        self.assertEqual((received.timestamp, received.source, received.verified_lore_receipt),
                         ("2026-10-28T10:34:56+00:00", "received", False))
        sender_date = parse_message(make_message(headers=(
            "Received: from relay; invalid\nDate: Wed, 28 Oct 2026 09:00:00 -0500\n"
        ))).time_estimate
        self.assertEqual((sender_date.timestamp, sender_date.source, sender_date.verified_lore_receipt),
                         ("2026-10-28T14:00:00+00:00", "date", False))
        for headers in ("", "Date: invalid\n", "Date: Wed, 28 Oct 2026 09:00:00 -0000\n"):
            with self.subTest(headers=headers):
                self.assertIsNone(parse_message(make_message(headers=headers)).time_estimate)

    def test_parse_mbox_retains_each_message(self):
        first = make_message(message_id="<first@example.test>")
        second = make_message(
            message_id="<second@example.test>", subject="Re: first patch", body="Ack.\n"
        )
        mbox_content = (
            b"From archive@example.test Tue Oct 28 00:00:00 2026\n"
            + first
            + b"\nFrom archive@example.test Tue Oct 28 00:01:00 2026\n"
            + second
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "thread.mbox"
            path.write_bytes(mbox_content)
            parsed = parse_mbox(path)

        self.assertEqual([message.message_id for message in parsed], [
            "first@example.test",
            "second@example.test",
        ])
        self.assertEqual(len(parsed), 2)
        self.assertTrue(all(message.raw_message for message in parsed))

    def test_parse_mbox_rejects_excessive_size_and_message_count(self):
        content = (
            b"From archive@example.test Tue Oct 28 00:00:00 2026\n"
            + make_message(message_id="<one@example.test>")
            + b"\nFrom archive@example.test Tue Oct 28 00:01:00 2026\n"
            + make_message(message_id="<two@example.test>")
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "thread.mbox"
            path.write_bytes(content)
            with patch("lore_review_companion.mail_parser.MAX_MBOX_BYTES", len(content) - 1):
                with self.assertRaisesRegex(ValueError, "byte limit"):
                    parse_mbox(path)
            with patch("lore_review_companion.mail_parser.MAX_MBOX_MESSAGES", 1):
                with self.assertRaisesRegex(ValueError, "message limit"):
                    parse_mbox(path)


if __name__ == "__main__":
    unittest.main()