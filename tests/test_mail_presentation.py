# SPDX-License-Identifier: GPL-2.0-or-later

import unittest
from unittest.mock import patch
from pathlib import Path
from types import SimpleNamespace

from lore_review_companion.build_identity import checkout_revision
from lore_review_companion.forge_api import ForgeAPI

from lore_review_companion.forge_comments import (
    imported_mail_text,
    quoted_parent_matches,
    reply_prose,
    submission_text,
)


class MailPresentationTests(unittest.TestCase):
    def test_source_free_body_and_build_header_do_not_change_payload(self):
        body = imported_mail_text(
            "Original prose.\n",
            "id",
            "https://lore.kernel.org/xen-devel/",
            "mail-text-v3",
        )
        self.assertEqual(body, "Original prose.")
        requests = []

        def transport(request, timeout):
            requests.append(request)
            return SimpleNamespace(
                status=204, headers={}, read=lambda size: b"", close=lambda: None
            )

        api = ForgeAPI("https://forge.example/api/v1", transport=transport)
        api.request_json(
            "repos/o/r/review-imports/pulls",
            method="POST",
            payload={"body": body},
            companion_revision="abcdef123456/remove-footer-link",
        )
        self.assertEqual(
            requests[0].get_header("X-lore-companion-revision"),
            "abcdef123456/remove-footer-link",
        )
        self.assertNotIn(b"companion", requests[0].data)
        with self.assertRaises(ValueError):
            api.request_json("repos/o/r", companion_revision="bad\r\nheader")

    def test_build_identity_has_real_commit_subject_and_dirty_indicator(self):
        with (
            patch.object(Path, "exists", return_value=True),
            patch(
                "lore_review_companion.build_identity.subprocess.run",
                side_effect=[
                    SimpleNamespace(stdout="a" * 40 + "\nRemove footer link!\n"),
                    SimpleNamespace(stdout=" M source.py\n"),
                ],
            ),
        ):
            self.assertEqual(
                checkout_revision(Path("/source")),
                "aaaaaaaaaaaa-dirty/remove-footer-link",
            )
        with patch.object(Path, "exists", return_value=False):
            self.assertEqual(checkout_revision(Path("/not-a-checkout")), "")

    def test_nested_quote_projection_and_exact_parent_words(self):
        source = "On 10/28/26, Jan wrote:\n\n> On earlier mail:\n> > Old point\n>\n> New point\n\nMy answer.\n\n-- \nAuthor"
        self.assertEqual(reply_prose(source), "My answer.\n\n-- \nAuthor")
        self.assertTrue(
            quoted_parent_matches(
                ("New point", "", "Second line"),
                "New point\nSecond line",
                "mail-text-v2",
            )
        )
        self.assertFalse(
            quoted_parent_matches(("New point",), "Different point", "mail-text-v2")
        )
        self.assertFalse(
            quoted_parent_matches(
                ("New point", "", "Second line"),
                "New point\nSecond line",
                "mail-text-v1",
            )
        )

    def test_quoted_parent_excerpt_ignores_only_optional_whitespace(self):
        parent = "First line.\nSecond line.\u00a0\nThird line.\n\nFourth line."
        for body_format in ("mail-text-v2", "mail-text-v3"):
            with self.subTest(body_format=body_format):
                self.assertTrue(
                    quoted_parent_matches(
                        ("", " First line. ", "Second line."),
                        parent,
                        body_format,
                        allow_excerpt=True,
                    )
                )
                self.assertTrue(
                    quoted_parent_matches(
                        ("Third line.", "", "Fourth line."),
                        parent,
                        body_format,
                        allow_excerpt=True,
                    )
                )
                self.assertFalse(
                    quoted_parent_matches(
                        ("First line.", "Third line."),
                        parent,
                        body_format,
                        allow_excerpt=True,
                    )
                )
                self.assertFalse(
                    quoted_parent_matches(
                        ("", "  "), parent, body_format, allow_excerpt=True
                    )
                )
                self.assertFalse(
                    quoted_parent_matches(
                        ("Changed line.",), parent, body_format, allow_excerpt=True
                    )
                )
                self.assertFalse(
                    quoted_parent_matches(("First line.",), parent, body_format)
                )
        self.assertFalse(
            quoted_parent_matches(("First line.",), parent, "mail-text-v1")
        )

    def test_submission_trailer_not_prose_or_patch_content(self):
        source = "Cover prose.\n\n xen/arm.c | 14 +\n xen/x.c   | 2 +-\n 2 files changed, 15 insertions(+), 1 deletion(-)\n create mode 100644 xen/arm.c\n\n-- \n2.53.8\n"
        self.assertEqual(submission_text(source), "Cover prose.")
        self.assertEqual(
            submission_text("Cover prose.\r\n\r\n--\r\n2.53.8\r\n"), "Cover prose."
        )
        patch = "Prose\n---\n xen/x.c | 1 +\n 1 file changed, 1 insertion(+)\n\ndiff --git a/xen/x.c b/xen/x.c\n+code\n-- \n2.53.8\n"
        self.assertIn("diff --git", submission_text(patch))
        self.assertIn("1 file changed", submission_text(patch))
        self.assertEqual(
            submission_text("Prose\n-- \nA human signature"),
            "Prose\n-- \nA human signature",
        )

    def test_named_source_link_is_escaped_and_not_a_footnote(self):
        body = imported_mail_text(
            "Original prose.",
            "mail@example.test",
            "https://lore.kernel.org/xen-devel/",
            "mail-text-v2",
            "Name <x&y>",
        )
        self.assertTrue(body.startswith("Original prose.\n\n---\n\n<small>"))
        self.assertIn("Original review comment by Name &lt;x&amp;y&gt;", body)
        self.assertIn(
            'href="https://lore.kernel.org/xen-devel/mail@example.test/"', body
        )
        self.assertNotIn("[^", body)
        self.assertIn(
            "[^lore-",
            imported_mail_text(
                "old", "id", "https://lore.kernel.org/xen-devel/", "mail-text-v1"
            ),
        )

    def test_cover_shortlog_and_diffstat_are_removed_together(self):
        prose = "Secure Boot support.\n\nChanges in v3:\n  Keep this explanation."
        shortlog = "Kevin Lampis (2):\n  Pass boot params\n  Zero GPRs\n\nRoss Lagerwall (1):\n  Add digest checks"
        diffstat = " xen/kexec.c | 3 +++\n 1 file changed, 3 insertions(+)\n create mode 100644 xen/kexec.c"
        cover = f"{prose}\n\n{shortlog}\n\n{diffstat}\n\n-- \n2.52.0\n"
        self.assertEqual(submission_text(cover), prose)
        b4_cover = f"{prose}\n\n---\n{shortlog}\n\n{diffstat}\n---\nbase-commit: {'a' * 40}\nchange-id: stable-series\n\nBest regards,\n--  \nSender"
        self.assertEqual(submission_text(b4_cover), prose)
        self.assertIn("base-commit:", submission_text(b4_cover, strip_shortlog=False))
        self.assertEqual(
            submission_text(cover, strip_shortlog=False), f"{prose}\n\n{shortlog}"
        )
        incomplete = cover.replace("Kevin Lampis (2):", "Kevin Lampis (3):").replace(
            "Ross Lagerwall (1):", "Ross Lagerwall (2):"
        )
        self.assertIn(shortlog.splitlines()[1], submission_text(incomplete))
        self.assertEqual(
            submission_text(f"{prose}\n\n{shortlog}"), f"{prose}\n\n{shortlog}"
        )
