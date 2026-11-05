"""Offline checks for relationship_engine: keyword matching must respect word boundaries."""

import time
import unittest

import relationship_engine as engine


class SubstringRegressionTests(unittest.TestCase):
    def test_lore_words_do_not_fire_inside_other_words(self):
        for text in ("I'm being weighed down by their ceiling", "the team had a great run during spring"):
            self.assertEqual(engine.describe_lore_hook("scaramouche", text), "", text)
            self.assertNotEqual(engine.detect_scenario(text), "lore_discussion", text)
        self.assertIn("Ei", engine.describe_lore_hook("scaramouche", "Ei is scary"))

    def test_romance_words_inside_other_words_do_not_count(self):
        for text in ("please update me", "Maybe I'll determine the winner"):
            self.assertNotEqual(engine.detect_scenario(text), "relationship_progression", text)
            self.assertNotIn("jealousy", engine.detect_emotional_triggers(text), text)
        self.assertEqual(engine.detect_scenario("be mine"), "relationship_progression")

    def test_hat_is_a_prop_only_as_a_word(self):
        self.assertNotIn("important_prop", engine.infer_scene_update("what was that about"))
        self.assertIn("important_prop", engine.infer_scene_update("nice hat"))
        self.assertEqual(engine.detect_banter_theme("that was fun"), "general")
        self.assertEqual(engine.detect_banter_theme("your hat is silly"), "hat")


class ConflictTests(unittest.TestCase):
    def test_preferences_and_complaints_are_not_conflicts(self):
        for text in ("I prefer tea, whatever you want", "this bug is annoying", "I will go to the store"):
            self.assertFalse(engine.detect_conflict_signal(text), text)

    def test_real_conflicts(self):
        for text in ("I prefer the other bot", "you are so annoying", "whatever.", "shut up", "switch to wanderer"):
            self.assertTrue(engine.detect_conflict_signal(text), text)

    def test_memory_events_are_specific(self):
        kinds = lambda text: [e[0] for e in engine.extract_memory_events(text + " and some padding words")]
        self.assertNotIn("promise", kinds("I will go to the store"))
        self.assertIn("promise", kinds("I promise I will never leave"))
        self.assertNotIn("unforgettable", kinds("this is an important meeting"))
        self.assertIn("unforgettable", kinds("remember this, it matters"))


class HooksAndContextTests(unittest.TestCase):
    def test_continuity_hooks_include_the_latest_earlier_message(self):
        history = [
            {"role": "user", "content": "I am so tired and scared today"},
            {"role": "assistant", "content": "x"},
            {"role": "user", "content": "thanks for caring about me earlier"},
        ]
        hooks = engine.extract_continuity_hooks(history, "hello again")
        self.assertIn("PAST_SOFTNESS:thanks for caring about me earlier", hooks)
        self.assertIn("PAST_VULNERABILITY:I am so tired and scared today", hooks)

    def test_current_message_is_never_echoed_back(self):
        history = [{"role": "user", "content": "I miss you so much, really I do"}]
        self.assertEqual(engine.extract_continuity_hooks(history, "I miss you so much, really I do"), [])

    def test_world_context_uses_the_given_timezone(self):
        text = engine.describe_live_world_context("scaramouche", tz_name="Asia/Tokyo", text="rain")
        self.assertIn("weather", text)
        self.assertIsInstance(engine.describe_live_world_context("scaramouche", tz_name="Not/AZone"), str)

    def test_conflict_aftermath_signature(self):
        out = engine.describe_conflict_aftermath("scaramouche", "they argued", time.time() - 3600, 1, conflict_open=True)
        self.assertIn("repair has started", out)


if __name__ == "__main__":
    unittest.main()
