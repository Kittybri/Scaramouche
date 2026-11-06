"""Offline checks for text_utils: narration stripping, keyword matching, mentions, chunking, parsers."""

import random
import re
import unittest

from text_utils import (
    chunk_text,
    clean_nickname,
    extract_search_query,
    has_keyword,
    keyword_regex,
    needs_search,
    neutralize_mass_mentions,
    normalize_text,
    parse_verdict,
    prompt_safe,
    resolve_name_mentions,
    sanitize_mentions,
    speech_text,
    split_sources,
    strip_narration,
)


class StripNarrationTests(unittest.TestCase):
    def test_actions_and_stage_directions_are_removed(self):
        self.assertEqual(
            strip_narration("*smirks* Pathetic. (sighs heavily) Try again. [he rolls his eyes] Fine."),
            "Pathetic. Try again. Fine.",
        )
        self.assertEqual(strip_narration("(Hmph.) Go on."), "Go on.")
        self.assertEqual(strip_narration("(sarcastically) Wonderful."), "Wonderful.")

    def test_emphasis_keeps_its_words(self):
        # Deleting these spans used to flip the meaning ("I do *not* care" became "I do care").
        for text in (
            "I do *not* care about that",
            "You are *never* going to win",
            "I trust *no one* here",
            "This is *very important*",
            "This is *very important*.",
            "*Never* say that again.",
            "*You* are impossible.",
            "Stop *staring* at me.",
            "Don't *laugh*!",
            "He said it *coldly*.",
            "*Absolutely not.* I refuse.",
            "Really. *Silence* is golden.",
            "I *love* this and *hate* that.",
        ):
            with self.subTest(text=text):
                self.assertEqual(strip_narration(text), text)

    def test_stage_directions_are_still_removed_in_every_position(self):
        cases = {
            "*smirks* Pathetic.": "Pathetic.",
            "Pathetic. *rolls his eyes*": "Pathetic.",
            "Hmph. *takes a step back* You wouldn't understand.": "Hmph. You wouldn't understand.",
            "I suppose *sighs* you may stay.": "I suppose you may stay.",
            "Well. *a long pause* Go on.": "Well. Go on.",
            "*Sighs.* Fine.": "Fine.",
            "*he scoffs* As if.": "As if.",
            "**bold** and *smirks* ok": "**bold** and ok",
            "Line one.\n*turns away*\nLine two.": "Line one.\n\nLine two.",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(strip_narration(text), expected)

    def test_speaker_tags_are_removed(self):
        self.assertEqual(strip_narration("Scaramouche muttered, Tch. How quaint."), "Tch. How quaint.")

    def test_ordinary_content_survives(self):
        for text in (
            "The answer is **42**, obviously.",
            "Compute f(x) = (x+1)^2 then check (see page 4).",
            "They said the library closes at 5.",
            "It was released in 2020 [1] and updated later [2].",
            "Who forged it? [ANSWER: Dottore] [SOURCE: Archon Quest]",
            "Mondstadt (the city of freedom) is nice. The nodes (nodes and edges) matter. (1969)",
            "* item one\n* item two\n2 * 3 * 4 = 24",
            "...How dreadfully quiet.",
        ):
            self.assertEqual(strip_narration(text), text)

    def test_sources_block_and_urls_are_untouched(self):
        text = "Water boils at 100C [1].\n\nSources:\n[1] Boiling - <https://en.wikipedia.org/wiki/Boiling_(disambiguation)>"
        self.assertEqual(strip_narration(text), text)

    def test_user_mentions_are_kept_unless_asked(self):
        self.assertEqual(strip_narration("Hello <@123456789012345678>, listen."), "Hello <@123456789012345678>, listen.")
        self.assertEqual(strip_narration("Hello <@123456789012345678>, listen.", strip_mentions=True), "Hello, listen.")

    def test_never_returns_nothing_for_non_empty_input(self):
        self.assertEqual(strip_narration("*sigh*"), "sigh")
        self.assertEqual(strip_narration("Ok"), "Ok")
        self.assertEqual(strip_narration(None), "")

    def test_voice_tags_are_removed(self):
        self.assertEqual(strip_narration("[voice message] Spare me."), "Spare me.")


class KeywordTests(unittest.TestCase):
    def test_word_boundaries(self):
        cases = [
            (["ei"], "ceiling", False), (["ei"], "Ei is here", True),
            (["date me"], "please update me", False), (["date me"], "date me?", True),
            (["ily"], "my family", False), (["ily"], "ily!!", True),
            (["hat"], "that", False), (["hat"], "nice hat!", True), (["hat"], "I hated it", False),
            (["mine"], "minecraft", False), (["mine"], "you are mine", True),
            (["tea"], "team", False), (["miss u"], "don't miss updates", False),
            (["ew"], "a new phone", False), (["ew"], "ew gross", True),
            (["love you"], "I love you so much", True), (["love you"], "I love your hat", False),
            (["annoying"], "so annoyingly", True),
        ]
        for words, text, expected in cases:
            self.assertEqual(has_keyword(text, words), expected, (words, text))

    def test_curly_apostrophes_match(self):
        self.assertTrue(has_keyword("I don’t care", ["i don't care"]))
        self.assertEqual(normalize_text("I’ve"), "i've")

    def test_multi_word_tokens_tolerate_extra_spaces(self):
        self.assertTrue(keyword_regex(["shut up"]).search("shut   up now"))


class MentionTests(unittest.TestCase):
    NAMES = {"al": 1, "alex": 2, "alex the great": 3, "bob": 4}

    def test_longest_name_wins(self):
        self.assertEqual(resolve_name_mentions("@Alex hi", self.NAMES), "<@2> hi")
        self.assertEqual(resolve_name_mentions("@Alex the Great: yes", self.NAMES), "<@3>: yes")
        self.assertEqual(resolve_name_mentions("@Al, hello and @Bob!", self.NAMES), "<@1>, hello and <@4>!")

    def test_unknown_names_and_emails_are_left_alone(self):
        self.assertEqual(resolve_name_mentions("@nobody here", self.NAMES), "@nobody here")
        self.assertEqual(resolve_name_mentions("email me@bob.com", self.NAMES), "email me@bob.com")

    def test_sanitize_neutralises_mass_and_bogus_mentions(self):
        out = sanitize_mentions("@everyone hi <@2> and <@999> <@&55> <@4> <@1>", {1, 2, 4})
        self.assertNotIn("@everyone", out)
        self.assertNotIn("<@999>", out)
        self.assertNotIn("<@&55>", out)
        self.assertEqual(out.count("<@"), 2)  # at most two real mentions survive
        self.assertEqual(sanitize_mentions("hi <@2>", set(), max_mentions=0), "hi")

    def test_mass_mentions_have_no_word_boundary_gap(self):
        # Discord matches the literal text, so "@herewhat" and "@everyone1" are pings too.
        for text in ("@herewhat", "@everyone1", "@everyone_x", "x@everyone", "@@everyone", "@@@@here", "@EVERYONE", "@Here"):
            with self.subTest(text=text):
                self.assertNotRegex(sanitize_mentions(text, {1}), r"(?i)@(everyone|here)")
                self.assertNotRegex(neutralize_mass_mentions(text), r"(?i)@(everyone|here)")
        self.assertEqual(sanitize_mentions("hi @everyone, listen", {1}), "hi everyone, listen")

    def test_a_removed_mention_cannot_rebuild_another_one(self):
        for text in ("<@&<@&1>1>", "<@<@7>7>", "<@ !7>", "<@<@5>!7>", "<@&<@&<@&1>1>1>", "@<@&1>everyone"):
            with self.subTest(text=text):
                out = sanitize_mentions(text, {1})
                self.assertNotRegex(out, r"(?i)@(everyone|here)")
                self.assertNotRegex(out, r"<@&\d+>")
                self.assertEqual(re.findall(r"<@!?(\d+)>", out), [])

    def test_no_random_input_keeps_a_ping_token(self):
        rng = random.Random(5)
        pieces = ["<@", "@", "&", "!", "5", "7", "12", ">", "<", "everyone", "here", "<@&", "<@!", " ", "\u200b", "@@", " !", "<@5>", "<@7>", "<@&9>"]
        for _ in range(20000):
            text = "".join(rng.choice(pieces) for _ in range(rng.randint(1, 14)))
            out = sanitize_mentions(text, {5}, max_mentions=2)
            users = re.findall(r"<@!?(\d+)>", out)
            self.assertIsNone(re.search(r"(?i)@(everyone|here)|<@&\d+>", out), (text, out))
            self.assertTrue(all(int(u) == 5 for u in users) and len(users) <= 2, (text, out))

    def test_neutralize_keeps_user_mentions(self):
        out = neutralize_mass_mentions("hi @everyone <@&5> <@5>")
        self.assertNotIn("@everyone", out)
        self.assertNotIn("<@&5>", out)
        self.assertIn("<@5>", out)
        self.assertEqual(neutralize_mass_mentions(None), "")


class ChunkTests(unittest.TestCase):
    def test_every_piece_fits_and_nothing_is_lost(self):
        text = "A fairly ordinary sentence about nothing. " * 200
        pieces = chunk_text(text, 1900)
        self.assertGreater(len(pieces), 1)
        self.assertTrue(all(len(p) <= 1900 for p in pieces))
        self.assertEqual(" ".join(pieces).split(), text.split())

    def test_short_and_empty(self):
        self.assertEqual(chunk_text("hello"), ["hello"])
        self.assertEqual(chunk_text("  "), [])

    def test_unbroken_text_is_hard_split(self):
        self.assertEqual([len(p) for p in chunk_text("x" * 4000, 1900)], [1900, 1900, 200])


class ParserTests(unittest.TestCase):
    def test_verdicts(self):
        self.assertEqual(parse_verdict("CORRECT. Nice job"), (True, "Nice job"))
        self.assertEqual(parse_verdict("Wrong, idiot"), (False, "idiot"))
        self.assertEqual(parse_verdict("Incorrect: no"), (False, "no"))
        self.assertEqual(parse_verdict("ACCEPT - fine"), (True, "fine"))
        self.assertEqual(parse_verdict("Refused!"), (False, ""))
        self.assertEqual(parse_verdict("Hmm, maybe"), (None, "Hmm, maybe"))

    def test_nicknames(self):
        self.assertEqual(clean_nickname('"Worm"'), "Worm")
        self.assertEqual(clean_nickname("Little Pest."), "Little Pest")
        for bad in ("Hmph.", "Go on.", "", None, "Fool 123", "a very long nickname that is far too long", "<@12345> pest"):
            self.assertIsNone(clean_nickname(bad), bad)

    def test_search_decisions(self):
        self.assertTrue(needs_search("what is the capital of france?"))
        self.assertTrue(needs_search("How many moons does Jupiter have?"))
        self.assertTrue(needs_search("latest news about genshin"))
        for personal in ("do you love me?", "tell me about yourself", "I feel sad today?", "hello", "what do you think about my day?"):
            self.assertFalse(needs_search(personal), personal)

    def test_search_query_is_clean(self):
        self.assertEqual(extract_search_query("Search the web for: <@123> release date https://x.y/z ."), "release date")
        self.assertLessEqual(len(extract_search_query("word " * 100)), 160)

    def test_prompt_hygiene(self):
        self.assertEqual(prompt_safe("evil | name ] [x\nnew"), "evil / name ) (x new")

    def test_speech_text_and_sources(self):
        self.assertEqual(speech_text("**Bold** text [1] with https://x.com/a link \U0001F608\n\nSources:\n[1] A - <https://a.b>"), "Bold text with link")
        body, sources = split_sources("Answer.\n\nSources:\n[1] A - <https://a.b>")
        self.assertEqual(body, "Answer.")
        self.assertTrue(sources.startswith("Sources:"))



class PathologicalInputTests(unittest.TestCase):
    """Reply text and user text pass through these helpers; hostile or broken input must stay cheap."""

    def assertFast(self, function, *args, limit=2.0):
        import time

        started = time.perf_counter()
        function(*args)
        self.assertLess(time.perf_counter() - started, limit, function.__name__)

    def test_long_runs_of_whitespace_and_markers(self):
        big = 100_000
        self.assertFast(split_sources, "\n" * big)
        self.assertFast(split_sources, " \n" * (big // 2))
        self.assertFast(strip_narration, " \n \t" * (big // 4))
        self.assertFast(strip_narration, "(" * big)
        self.assertFast(strip_narration, "* a" * (big // 3))
        self.assertFast(speech_text, " \n" * (big // 2))
        self.assertFast(parse_verdict, " " * big)
        self.assertFast(needs_search, "what is " * (big // 8))
        self.assertFast(resolve_name_mentions, "@" + "a " * big, {"a": 1})
        self.assertFast(sanitize_mentions, "<@1> hi @everyone " * 6000, {1})


if __name__ == "__main__":
    unittest.main()
