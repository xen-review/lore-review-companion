import unittest

from lore_review_companion.anchor import DiffIndex
from lore_review_companion.mail_parser import extract_quoted_remarks


PATCH = """diff --git a/src/sample.c b/src/sample.c
index 1111111..2222222 100644
--- a/src/sample.c
+++ b/src/sample.c
@@ -10,4 +10,5 @@
 keep();
-old_call();
+new_call();
+duplicate();
 duplicate();
 tail();
"""


class DiffIndexTests(unittest.TestCase):
    def setUp(self):
        self.index = DiffIndex(PATCH)

    def test_added_line_uses_new_position(self):
        anchor = self.index.resolve(
            {"path": "src/sample.c", "line": 12, "content": "> +new_call();"}
        )
        self.assertEqual((anchor.old_position, anchor.new_position), (0, 11))

    def test_deleted_line_uses_old_position(self):
        anchor = self.index.resolve(
            {"path": "src/sample.c", "line": 11, "content": "-old_call();"}
        )
        self.assertEqual((anchor.old_position, anchor.new_position), (11, 0))

    def test_context_line_uses_new_position(self):
        anchor = self.index.resolve(
            {"path": "src/sample.c", "line": 10, "content": " keep();"}
        )
        self.assertEqual((anchor.old_position, anchor.new_position), (0, 10))

    def test_quote_depth_drift_is_normalized(self):
        anchor = self.index.resolve(
            {"path": "src/sample.c", "content": "> > +new_call();"}
        )
        self.assertEqual(anchor.new_position, 11)

    def test_duplicate_text_requires_a_unique_line_hint(self):
        index = DiffIndex("diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -0,0 +1,2 @@\n+duplicate();\n+duplicate();\n")
        comment = {"path": "x", "content": "+duplicate();"}
        self.assertIsNone(index.resolve(comment))
        anchor = index.resolve({**comment, "line": 2})
        self.assertEqual(anchor.new_position, 2)

    def test_matching_respects_diff_side_for_identical_text(self):
        index = DiffIndex("diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1,2 +1,2 @@\n-same\n+same\n same\n")
        self.assertEqual(index.resolve({"path": "x", "content": "+same"}).new_position, 1)
        self.assertEqual(index.resolve({"path": "x", "content": "-same"}).old_position, 1)
        self.assertEqual(index.resolve({"path": "x", "content": " same"}).new_position, 2)

    def test_renamed_files_and_ambiguous_prefixed_paths_fail_closed(self):
        renamed = DiffIndex(
            "diff --git a/old b/new\n--- a/old\n+++ b/new\n@@ -1 +1 @@\n-before\n+after\n"
        )
        self.assertIsNone(renamed.resolve({"path": "new", "content": "+after"}))
        ambiguous = DiffIndex(
            "diff --git a/foo b/foo\n--- a/foo\n+++ b/foo\n@@ -0,0 +1 @@\n+same\n"
            "diff --git a/a/foo b/a/foo\n--- a/a/foo\n+++ b/a/foo\n@@ -0,0 +2 @@\n+same\n"
        )
        self.assertIsNone(ambiguous.resolve({"path": "a/foo", "content": "+same", "line": 1}))

    def test_unknown_text_is_not_anchored(self):
        self.assertIsNone(
            self.index.resolve(
                {"path": "src/sample.c", "content": "+not_in_patch();"}
            )
        )

    def test_exact_multiline_quote_selects_right_side_range(self):
        (remark,) = extract_quoted_remarks(
            "> --- a/src/sample.c\n> +++ b/src/sample.c\n"
            "> +new_call();\n> +duplicate();\nPlease change both.\n",
        )
        anchor = self.index.resolve_quote(remark)
        self.assertEqual((anchor.path, anchor.old_position, anchor.new_position, anchor.extra_lines_count),
                         ("src/sample.c", 0, 11, 1))
        self.assertIsNone(self.index.resolve_quote(remark, max_lines=1))

    def test_context_and_deletion_select_left_range(self):
        (remark,) = extract_quoted_remarks(
            "> --- a/src/sample.c\n> +++ b/src/sample.c\n"
            ">  keep();\n> -old_call();\nPlease retain this.\n",
        )
        anchor = self.index.resolve_quote(remark)
        self.assertEqual((anchor.old_position, anchor.new_position, anchor.extra_lines_count), (10, 0, 1))

    def test_complete_context_disambiguates_repeated_last_lines(self):
        index = DiffIndex(
            "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -0,0 +1,4 @@\n"
            "+first\n+repeat\n+other\n+repeat\n",
        )
        (remark,) = extract_quoted_remarks("> --- a/x\n> +++ b/x\n> +other\n> +repeat\nRemark\n")
        self.assertEqual(index.resolve_quote(remark).new_position, 3)
        (ambiguous,) = extract_quoted_remarks("> --- a/x\n> +++ b/x\n> +repeat\nRemark\n")
        self.assertIsNone(index.resolve_quote(ambiguous))

    def test_mixed_sides_disjoint_hunks_and_headerless_quotes_fail_closed(self):
        quotes = (
            "> --- a/src/sample.c\n> +++ b/src/sample.c\n> -old_call();\n> +new_call();\nRemark\n",
            "> --- a/src/sample.c\n> +++ b/src/sample.c\n> +new_call();\n> @@ -13 +14 @@\n>  tail();\nRemark\n",
            "> +new_call();\nRemark\n",
        )
        for body in quotes:
            with self.subTest(body=body):
                (remark,) = extract_quoted_remarks(body)
                self.assertIsNone(self.index.resolve_quote(remark))

    def test_range_context_only_defaults_to_right_and_renames_fail_closed(self):
        (remark,) = extract_quoted_remarks(
            "> --- a/src/sample.c\n> +++ b/src/sample.c\n>  duplicate();\n>  tail();\nRemark\n",
        )
        anchor = self.index.resolve_quote(remark)
        self.assertEqual((anchor.new_position, anchor.extra_lines_count), (13, 1))
        (renamed,) = extract_quoted_remarks("> --- a/old\n> +++ b/new\n> +new_call();\nRemark\n")
        self.assertIsNone(self.index.resolve_quote(renamed))


if __name__ == "__main__":
    unittest.main()