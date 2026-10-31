# SPDX-License-Identifier: GPL-2.0-or-later

import unittest

from lore_review_companion.mail_parser import parse_message
from lore_review_companion.series import (
    change_id_candidates,
    discover_series,
    has_invalid_change_id_claims,
    missing_patch_signoffs,
)


def message(message_id, subject, *, headers=""):
    return parse_message(
        (
            f"From: Patch Author <author@example.test>\n"
            f"Message-ID: <{message_id}>\n"
            f"Subject: {subject}\n"
            f"{headers}"
            "Content-Type: text/plain; charset=utf-8\n\n"
            "Patch body.\n\nSigned-off-by: Patch Author <author@example.test>\n"
        ).encode()
    )


class DiscoverSeriesTests(unittest.TestCase):
    def test_signoff_validation_checks_each_patch_not_cover(self):
        cover = parse_message(b"Message-ID: <cover@example.test>\nSubject: [PATCH 0/2] test\n\nCover.\n")
        first = message("first@example.test", "[PATCH 1/2] test", headers="In-Reply-To: <cover@example.test>\n")
        second = parse_message(b"Message-ID: <second@example.test>\nSubject: [PATCH 2/2] test\nIn-Reply-To: <cover@example.test>\n\nNo signoff.\n---\n")
        (revision,) = discover_series((cover, first, second))
        self.assertTrue(revision.complete)
        self.assertEqual(missing_patch_signoffs(revision), (second,))

    def test_signoff_validation_rejects_malformed_or_quoted_trailers(self):
        for text in ("Signed-off-by: not-an-address", "> Signed-off-by: Author <author@example.test>"):
            patch_message = parse_message(
                ("Message-ID: <patch@example.test>\nSubject: [PATCH] test\n\n" + text + "\n---\n").encode()
            )
            (revision,) = discover_series((patch_message,))
            self.assertEqual(missing_patch_signoffs(revision), (patch_message,))

    def test_excluded_recipient_suppresses_entire_submission_revision(self):
        cover = message("excluded-cover@example.test", "[PATCH 0/1] xen: example",
                        headers='Cc: "List, Example" <list@VGER.KERNEL.ORG>\n')
        patch_message = message("excluded-patch@example.test", "[PATCH 1/1] xen: example",
                                headers="In-Reply-To: <excluded-cover@example.test>\n")
        self.assertEqual(discover_series((cover, patch_message)), ())
        allowed = message("allowed@example.test", "[PATCH] xen: allowed",
                          headers="From: list@vger.kernel.org\nTo: other@example.test\n")
        self.assertTrue(discover_series((allowed,))[0].complete)

    def test_excluded_cover_suppresses_unversioned_patches_in_thread(self):
        cover = message("cover@example.test", "[PATCH v2 0/1] xen: example",
                        headers="Cc: list@vger.kernel.org\n")
        patch_message = message("patch@example.test", "[PATCH 1/1] xen: example",
                                headers="In-Reply-To: <cover@example.test>\n")
        self.assertEqual(discover_series((cover, patch_message)), ())

    def test_unversioned_patches_under_later_cover_are_not_complete_v1(self):
        cover = message("cover@example.test", "[PATCH v2 0/1] xen: example")
        patch_message = message("patch@example.test", "[PATCH 1/1] xen: example",
                                headers="In-Reply-To: <cover@example.test>\n")
        revisions = discover_series((cover, patch_message))
        self.assertEqual([revision.revision for revision in revisions], [1, 2])
        self.assertFalse(any(revision.complete for revision in revisions))
        self.assertIn("unversioned patch references v2 cover: cover@example.test", revisions[0].conflicts)
        versioned = message("patch@example.test", "[PATCH v2 1/1] xen: example",
                            headers="In-Reply-To: <cover@example.test>\n")
        self.assertTrue(discover_series((cover, versioned))[0].complete)

    def test_unnumbered_patch_subjects_are_independent_complete_singletons(self):
        first = message("singleton@example.test", "[PATCH] xen: single change")
        second = message("revised@example.test", "[PATCH v5] xen: revised single change")
        revisions = discover_series((first, second))
        self.assertEqual(len(revisions), 2)
        self.assertTrue(all(revision.complete for revision in revisions))
        self.assertTrue(all(revision.expected_patch_count == 1 for revision in revisions))
        self.assertEqual((first.patch_index, first.series_size), (1, 1))
        self.assertEqual((second.patch_index, second.series_size, second.patch_version), (1, 1, 5))

    def test_groups_cover_and_patches_by_explicit_thread_root(self):
        cover = message(
            "cover@example.test",
            "[PATCH v2 0/2] xen: improve scheduling",
        )
        first = message(
            "patch-1@example.test",
            "[PATCH v2 1/2] xen: improve scheduling",
            headers="References: <cover@example.test>\nIn-Reply-To: <cover@example.test>\n",
        )
        second = message(
            "patch-2@example.test",
            "[PATCH v2 2/2] xen: improve scheduling",
            headers="References: <cover@example.test>\nIn-Reply-To: <cover@example.test>\n",
        )

        (revision,) = discover_series([second, cover, first])

        self.assertEqual(revision.series_key, "lore:cover@example.test")
        self.assertEqual(revision.revision, 2)
        self.assertEqual(revision.title, "xen: improve scheduling")
        self.assertEqual(revision.expected_patch_count, 2)
        self.assertEqual(revision.cover_message_id, "cover@example.test")
        self.assertEqual([item.patch_index for item in revision.messages], [0, 1, 2])
        self.assertTrue(revision.complete)
        self.assertFalse(revision.conflicts)

    def test_missing_direct_parent_does_not_produce_complete_series(self):
        cover = message("cover@example.test", "[PATCH 0/1] xen: example")
        patch_message = message(
            "patch@example.test", "[PATCH 1/1] xen: example",
            headers="References: <cover@example.test>\nIn-Reply-To: <missing@example.test>\n",
        )
        (revision,) = discover_series((cover, patch_message))
        self.assertFalse(revision.complete)
        self.assertIn("patch patch@example.test references missing parent missing@example.test", revision.conflicts)
        self.assertIn("cover cover@example.test has no directly linked patches", revision.conflicts)

    def test_missing_parent_without_cover_is_not_a_valid_singleton(self):
        patch_message = message(
            "patch@example.test", "[PATCH 1/1] xen: example",
            headers="In-Reply-To: <missing@example.test>\n",
        )
        (revision,) = discover_series((patch_message,))
        self.assertFalse(revision.complete)
        self.assertIn("patch patch@example.test references missing parent missing@example.test", revision.conflicts)

    def test_same_subject_without_shared_thread_roots_stays_separate(self):
        first = message("one@example.test", "[PATCH 1/1] xen: same title")
        second = message("two@example.test", "[PATCH 1/1] xen: same title")

        revisions = discover_series([first, second])

        self.assertEqual(len(revisions), 2)
        self.assertNotEqual(revisions[0].series_key, revisions[1].series_key)

    def test_change_id_is_a_hint_and_does_not_join_unthreaded_revisions(self):
        first = parse_message(
            b"Message-ID: <v1@example.test>\nSubject: [PATCH 1/1] change\n\n"
            b"change-id: 20260101-change-123\nprerequisite-change-id: other\n"
        )
        second = parse_message(
            b"Message-ID: <v2@example.test>\nSubject: [PATCH v2 1/1] change\n\n"
            b"Change-ID: 20260101-change-123\n"
        )
        revisions = discover_series((first, second))
        self.assertEqual(len(revisions), 2)
        self.assertNotEqual(revisions[0].series_key, revisions[1].series_key)
        self.assertEqual(
            [change_id_candidates(revision) for revision in revisions],
            [("20260101-change-123",), ("20260101-change-123",)],
        )
        ambiguous = parse_message(
            b"Message-ID: <ambiguous@example.test>\nSubject: [PATCH 1/1] change\n\n"
            b"change-id: first\nchange-id: second\n"
        )
        self.assertEqual(
            change_id_candidates(discover_series((ambiguous,))[0]), ("first", "second")
        )
        crlf = parse_message(
            b"Message-ID: <crlf@example.test>\r\nSubject: [PATCH 1/1] change\r\n\r\nchange-id: stable\r\n"
        )
        (revision,) = discover_series((crlf,))
        self.assertEqual(change_id_candidates(revision), ("stable",))
        self.assertFalse(has_invalid_change_id_claims(revision))

    def test_missing_patch_index_is_incomplete(self):
        cover = message("cover@example.test", "[PATCH 0/2] xen: incomplete")
        first = message(
            "patch-1@example.test",
            "[PATCH 1/2] xen: incomplete",
            headers="References: <cover@example.test>\n",
        )

        (revision,) = discover_series([cover, first])

        self.assertFalse(revision.complete)
        self.assertIn("missing patch indices: 2", revision.conflicts)

    def test_replies_are_not_mistaken_for_series_members(self):
        patch = message("patch@example.test", "[PATCH 1/1] xen: change")
        reply = message(
            "reply@example.test",
            "Re: [PATCH 1/1] xen: change",
            headers="In-Reply-To: <patch@example.test>\n",
        )

        revisions = discover_series([patch, reply])

        self.assertEqual(len(revisions), 1)
        self.assertEqual(
            [item.message_id for item in revisions[0].messages], ["patch@example.test"]
        )

    def test_cyclic_thread_ancestry_is_not_publishable(self):
        first = message(
            "first@example.test",
            "[PATCH 1/2] first",
            headers="In-Reply-To: <second@example.test>\n",
        )
        second = message(
            "second@example.test",
            "[PATCH 2/2] second",
            headers="In-Reply-To: <first@example.test>\n",
        )
        revisions = discover_series([first, second])
        self.assertTrue(all(not revision.complete for revision in revisions))
        self.assertTrue(
            any(
                "thread ancestry contains a cycle" in revision.conflicts
                for revision in revisions
            )
        )

    def test_multiple_known_thread_roots_and_duplicate_ids_are_not_publishable(self):
        first = message("first@example.test", "[PATCH 1/1] first")
        second = message("second@example.test", "[PATCH 1/1] second")
        ambiguous = message(
            "third@example.test",
            "[PATCH 1/1] third",
            headers="References: <first@example.test> <second@example.test>\n",
        )
        revisions = discover_series([first, second, ambiguous])
        self.assertTrue(
            any(
                "thread ancestry identifies multiple roots" in revision.conflicts
                for revision in revisions
            )
        )
        (duplicate,) = discover_series([first, first])
        self.assertFalse(duplicate.complete)
        self.assertIn("duplicate Message-ID: first@example.test", duplicate.conflicts)

    def test_oversized_patch_count_is_rejected_without_allocating_its_range(self):
        source = message("huge@example.test", f"[PATCH 1/{'9' * 5000}] oversized")
        (revision,) = discover_series([source])
        self.assertFalse(revision.complete)
        self.assertIn("series patch count is outside 1..500", revision.conflicts)

    def test_deep_ancestry_is_followed_without_a_total_depth_limit(self):
        messages = [message("0@example.test", "[PATCH 1/1] root")]
        for index in range(1, 1200):
            messages.append(
                message(
                    f"{index}@example.test",
                    f"[PATCH v{index + 1} 1/1] nested",
                    headers=f"In-Reply-To: <{index - 1}@example.test>\n",
                )
            )
        revisions = discover_series(reversed(messages))
        self.assertEqual(len(revisions), len(messages))
        self.assertTrue(
            all(revision.root_message_id == "0@example.test" for revision in revisions)
        )
        self.assertTrue(all(revision.complete for revision in revisions))


if __name__ == "__main__":
    unittest.main()
