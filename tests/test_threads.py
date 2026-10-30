# SPDX-License-Identifier: GPL-2.0-or-later

import unittest

from lore_review_companion.series import discover_series
from test_series import message
from lore_review_companion.threads import match_thread_replies


class ThreadMatchingTests(unittest.TestCase):
    def test_nested_replies_match_patch_independent_of_arrival_order(self):
        patch = message("patch@example.test", "[PATCH 1/1] xen: change")
        first = message("first@example.test", "Re: patch", headers="In-Reply-To: <patch@example.test>\n")
        second = message("second@example.test", "Re: patch", headers="In-Reply-To: <first@example.test>\n")
        revision = discover_series([patch])[0]

        result = match_thread_replies([second, first, patch], [revision])

        self.assertEqual([item.message.message_id for item in result.matched], ["first@example.test", "second@example.test"])
        self.assertTrue(all(item.patch_message_id == "patch@example.test" for item in result.matched))
        self.assertEqual(result.unmatched, ())

    def test_deep_reply_chain_is_matched_without_a_total_depth_limit(self):
        patch = message("patch@example.test", "[PATCH 1/1] xen: change")
        replies = []
        parent = patch.message_id
        for index in range(1200):
            reply = message(f"reply-{1199 - index:04}@example.test", "Re: patch", headers=f"In-Reply-To: <{parent}>\n")
            replies.append(reply)
            parent = reply.message_id

        result = match_thread_replies(reversed([patch, *replies]), discover_series([patch]))

        self.assertEqual(len(result.matched), len(replies))
        self.assertTrue(all(item.patch_message_id == patch.message_id for item in result.matched))
        self.assertEqual(result.unmatched, ())

    def test_missing_direct_parent_can_use_nearest_known_reference(self):
        patch = message("patch@example.test", "[PATCH 1/1] xen: change")
        reply = message(
            "reply@example.test", "Re: patch",
            headers="References: <patch@example.test> <missing@example.test>\nIn-Reply-To: <missing@example.test>\n",
        )
        result = match_thread_replies([reply, patch], discover_series([patch]))
        self.assertEqual(result.matched[0].patch_message_id, "patch@example.test")

    def test_ambiguous_parents_cycles_and_same_subject_are_unmatched(self):
        first = message("one@example.test", "[PATCH 1/1] same subject")
        second = message("two@example.test", "[PATCH 1/1] same subject")
        ambiguous = message(
            "ambiguous@example.test", "Re: same subject",
            headers="In-Reply-To: <one@example.test> <two@example.test>\n",
        )
        cycle_a = message("cycle-a@example.test", "Re: same subject", headers="In-Reply-To: <cycle-b@example.test>\n")
        cycle_b = message("cycle-b@example.test", "Re: same subject", headers="In-Reply-To: <cycle-a@example.test>\n")
        unthreaded = message("unthreaded@example.test", "Re: same subject")
        result = match_thread_replies(
            [ambiguous, cycle_b, cycle_a, unthreaded, first, second], discover_series([first, second]),
        )
        self.assertEqual(result.matched, ())
        self.assertEqual(len(result.unmatched), 4)

    def test_conflicting_message_id_is_not_used_as_ancestry(self):
        patch = message("patch@example.test", "[PATCH 1/1] xen: change")
        conflicting = message("patch@example.test", "[PATCH 1/1] xen: different")
        reply = message("reply@example.test", "Re: patch", headers="In-Reply-To: <patch@example.test>\n")
        result = match_thread_replies([patch, reply, conflicting], discover_series([patch]))
        self.assertEqual(result.matched, ())
        self.assertEqual(result.unmatched[0].message_id, "reply@example.test")


if __name__ == "__main__":
    unittest.main()