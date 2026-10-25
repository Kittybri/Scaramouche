"""Offline regression checks against the repository's actual anti-repeat module."""

import unittest
import anti_repeat


class AntiRepeatTests(unittest.TestCase):
    def setUp(self):
        for history in anti_repeat._RUNTIME_RECENT.values():
            history.clear()

    def test_normalized_duplicates_are_merged(self):
        self.assertEqual(
            anti_repeat.merge_recent_messages(["Hello there!", ""], ["hello there", "A different thought."]),
            ["Hello there!", "A different thought."],
        )

    def test_recent_history_is_limited(self):
        self.assertEqual(anti_repeat.merge_recent_messages(["first", "second", "third"], limit=2), ["first", "second"])

    def test_exact_and_same_opening_replies_are_detected(self):
        self.assertTrue(anti_repeat.looks_repetitive("You really thought that was clever.", ["You really thought that was clever."]))
        self.assertTrue(anti_repeat.looks_repetitive("You really thought that would help.", ["You really thought that was clever."]))

    def test_distinct_response_is_not_rejected(self):
        self.assertFalse(anti_repeat.looks_repetitive("We should review the evidence before deciding.", ["Your timing could use some work."]))

    def test_histories_are_isolated_between_bots(self):
        anti_repeat.remember_output("scaramouche", "A remembered line.")
        self.assertEqual(anti_repeat.get_runtime_recent("scaramouche"), ["A remembered line."])
        self.assertEqual(anti_repeat.get_runtime_recent("wanderer"), [])

    def test_history_is_bounded_and_newest_first(self):
        for number in range(100):
            anti_repeat.remember_output("wanderer", f"Line {number}")
        history = anti_repeat.get_runtime_recent("wanderer", limit=100)
        self.assertEqual(len(history), 80)
        self.assertEqual(history[0], "Line 99")

    def test_fresh_choice_avoids_recent_opening(self):
        self.assertEqual(
            anti_repeat.pick_fresh_option("scaramouche", ["Predictable. Try harder.", "A completely different answer."], ["Predictable. Try harder."]),
            "A completely different answer.",
        )


if __name__ == "__main__":
    unittest.main()
