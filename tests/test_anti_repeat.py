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

    def test_shared_opening_alone_is_not_repetition(self):
        self.assertFalse(anti_repeat.looks_repetitive(
            "I do not know the answer to that, honestly speaking.", ["I do not know where she went last night."]))

    def test_signature_openers_match_whole_words_only(self):
        counts = anti_repeat._phrase_counts("scaramouche", ["Tchaikovsky wrote it", "Tchaikovsky again", "Tch. no", "Tch, again"])
        self.assertEqual(dict(counts), {"tch": 2})

    def test_curly_apostrophe_openers_are_detected(self):
        self.assertEqual(anti_repeat.detect_opening_phrase("wanderer", "I\u2019ve got nothing for you"), "i've got nothing")

    def test_unknown_bot_name_still_gets_a_fallback(self):
        self.assertTrue(anti_repeat.fallback_reply("someone-else", []))

    def test_replaced_opener_keeps_the_rest_as_a_new_sentence(self):
        out = anti_repeat.diversify_reply("scaramouche", "How very quaint, you tried.", ["How quaint.", "How quaint, really."])
        self.assertTrue(out.endswith("You tried."), out)
        self.assertFalse(out.lower().startswith("how"))


if __name__ == "__main__":
    unittest.main()
