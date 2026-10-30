# SPDX-License-Identifier: GPL-2.0-or-later

import unittest

from lore_review_companion.forge_comments import (
    imported_mail_text,
    quoted_parent_matches,
    reply_prose,
    submission_text,
)


class MailPresentationTests(unittest.TestCase):
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
