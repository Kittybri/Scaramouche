"""Regression tests for the November audit fixes on main.

Run from the repository root (nothing beyond requirements.txt is needed):

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import atexit
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
# Every database these tests touch lives in a throwaway folder (memory.py picks its folder when first imported).
_DATA_DIR = tempfile.mkdtemp(prefix="scaramouche-tests-")
atexit.register(shutil.rmtree, _DATA_DIR, True)
os.environ["MEMORY_DATA_DIR"] = _DATA_DIR
os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("GROQ_API_KEY", "test-key")

import discord  # noqa: E402

import memory  # noqa: E402
import narration  # noqa: E402
import video_render_worker as worker  # noqa: E402
import bot  # noqa: E402

DOC = "https://docs.google.com/document/d/abcdefghijklmnopqrstuvwx/edit"
DOC_ID = "abcdefghijklmnopqrstuvwx"


# ── helpers ──────────────────────────────────────────────────────────────────
def use_fresh_memory(test: unittest.TestCase) -> memory.Memory:
    """A new, empty pair of databases for one test; bot.py's ``mem`` points at it until the test ends."""
    folder = tempfile.mkdtemp(prefix="mem-", dir=_DATA_DIR)
    for name, value in (
        ("_data_dir", folder),
        ("DB_PATH", os.path.join(folder, "scaramouche.db")),
        ("SHARED_DB_PATH", os.path.join(folder, "shared_state.db")),
    ):
        patcher = mock.patch.object(memory, name, value)
        patcher.start()
        test.addCleanup(patcher.stop)
    store = memory.Memory("scaramouche")
    patcher = mock.patch.object(bot, "mem", store)
    patcher.start()
    test.addCleanup(patcher.stop)
    return store


class Typing:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def make_ctx(author_id: int = 5, *, guild_id: int | None = None, members: dict | None = None, nsfw: bool = False):
    guild = None
    if guild_id is not None:
        roster = members or {}
        guild = NS(id=guild_id, get_member=lambda uid: roster.get(uid))
    author = NS(id=author_id, name=f"user{author_id}", display_name=f"user{author_id}", bot=False,
                mention=f"<@{author_id}>", send=AsyncMock())
    channel = NS(id=9000 + author_id, guild=guild, is_nsfw=lambda: nsfw)
    return NS(author=author, guild=guild, channel=channel, reply=AsyncMock(), send=AsyncMock(), typing=Typing)


def said(ctx) -> str:
    """Everything the bot posted where the command was used (not DMs)."""
    calls = ctx.reply.await_args_list + ctx.send.await_args_list
    return "\n".join(str(call.args[0]) for call in calls if call.args)


class BotTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.mem = use_fresh_memory(self)
        await self.mem.init()
        self.use(bot, "OWNER_ID", 999)
        self.use(bot, "DISCORD_SEND_MIN_INTERVAL_S", 0)
        self.use(bot, "_setup", self._fake_setup)

    def use(self, target, name, value):
        patcher = mock.patch.object(target, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)
        return value

    async def _fake_setup(self, ctx):
        await self.mem.upsert_user(ctx.author.id, ctx.author.name, ctx.author.display_name)
        return await self.mem.get_user(ctx.author.id)

    async def seed_messages(self):
        """User 1 talks in a DM, in server 100 (channel 10) and in server 200 (channel 20)."""
        await self.mem.track_channel(10, 100)
        await self.mem.track_channel(20, 200)
        await self.mem.add_message(1, 1, "user", "dm secret")  # DMs are stored under the user id, never tracked
        await self.mem.add_message(1, 10, "user", "said in server A")
        await self.mem.add_message(1, 20, "user", "said in server B")


# ── narration and mentions ───────────────────────────────────────────────────
class NarrationTests(unittest.TestCase):
    def test_stage_directions_are_removed(self):
        for raw, expected in [
            ("*smirks* Pathetic.", "Pathetic."),
            ("(sighs) Fine.", "Fine."),
            ("[he scoffs] Whatever.", "Whatever."),
            ('Scaramouche muttered, "Fine."', '"Fine."'),
        ]:
            with self.subTest(raw=raw):
                self.assertEqual(narration.strip_narration(raw), expected)

    def test_ordinary_content_survives(self):
        for text in [
            "The answer is **42**.",
            "I do *not* care.",
            "f(x) = (x+1)^2",
            "See (page 4).",
            "[ANSWER: Paris] Obviously.",
            "Read https://en.wikipedia.org/wiki/Boiling_(disambiguation) first.",
            "They said the library closes at 9.",
        ]:
            with self.subTest(text=text):
                self.assertEqual(narration.strip_narration(text), text)
                self.assertEqual(bot.strip_narration(text), text)

    def test_mass_and_role_mentions_never_survive(self):
        for text in ["@everyone look at this", "hi @here", "<@&123> role ping", "@@everyone", "<@&<@&1>1>"]:
            with self.subTest(text=text):
                cleaned = narration.strip_narration(text)
                self.assertNotIn("@everyone", cleaned)
                self.assertNotIn("@here", cleaned)
                self.assertIsNone(re.search(r"<@&\d+>", cleaned))  # leftovers such as "<@& 1>" cannot ping

    def test_the_bot_never_pings_everyone_or_roles(self):
        self.assertFalse(bot.bot.allowed_mentions.everyone)
        self.assertFalse(bot.bot.allowed_mentions.roles)

    def test_long_text_is_split_under_discords_limit(self):
        text = " ".join(f"word{i}" for i in range(900))
        pieces = narration.chunk_text(text, bot.MESSAGE_CHAR_LIMIT)
        self.assertGreater(len(pieces), 1)
        self.assertTrue(all(len(piece) <= 2000 for piece in pieces))
        self.assertEqual(" ".join(pieces).split(), text.split())


# ── render worker ────────────────────────────────────────────────────────────
class WorkerAuthTests(unittest.TestCase):
    SECRET = "s" * 32

    def test_no_secret_means_nobody_is_authorized(self):
        with mock.patch.object(worker, "VIDEO_RENDER_SECRET", ""):
            self.assertFalse(worker._authorized({}))
            self.assertFalse(worker._authorized({"X-Render-Secret": ""}))

    def test_a_short_secret_is_refused(self):
        with mock.patch.object(worker, "VIDEO_RENDER_SECRET", "short"):
            self.assertFalse(worker._authorized({"X-Render-Secret": "short"}))

    def test_the_secret_must_match(self):
        with mock.patch.object(worker, "VIDEO_RENDER_SECRET", self.SECRET):
            self.assertTrue(worker._authorized({"X-Render-Secret": self.SECRET}))
            self.assertTrue(worker._authorized({"Authorization": f"Bearer {self.SECRET}"}))
            self.assertFalse(worker._authorized({"X-Render-Secret": self.SECRET[:-1] + "x"}))
            self.assertFalse(worker._authorized({}))

    def test_render_requests_can_only_set_voice_settings(self):
        cleaned = worker._clean_env_overrides({
            "FISH_AUDIO_API_KEY": "k", "SCARAMOUCHE_VOICE_ID": "v", "PATH": "/tmp/evil", "PYTHONPATH": "/tmp",
            "LD_PRELOAD": "/tmp/x.so", "HTTPS_PROXY": "http://evil.example", "VOICE_ID": 5, "PARTNER_VOICE_ID": "a\x00b",
        })
        self.assertEqual(cleaned, {"FISH_AUDIO_API_KEY": "k", "SCARAMOUCHE_VOICE_ID": "v"})
        self.assertEqual(worker._clean_env_overrides(["PATH"]), {})

    def test_listens_on_loopback_unless_told_otherwise(self):
        if os.getenv("VIDEO_RENDER_HOST"):
            self.skipTest("VIDEO_RENDER_HOST is set in this environment")
        self.assertEqual(worker.VIDEO_RENDER_HOST, "127.0.0.1")

    def test_refuses_to_start_without_a_secret(self):
        folder = tempfile.mkdtemp(prefix="worker-", dir=_DATA_DIR)  # no video_render_worker.env or .env here
        for name in ("video_render_worker.py", "google_docs_bridge.py"):
            shutil.copy(ROOT / name, folder)
        env = {k: v for k, v in os.environ.items() if k not in {"VIDEO_RENDER_SECRET", "VIDEO_RENDER_SHARED_SECRET"}}
        proc = subprocess.run(
            [sys.executable, "video_render_worker.py"], cwd=folder, env=env, capture_output=True, text=True, timeout=60,
            check=False,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("VIDEO_RENDER_SECRET", proc.stderr)


class WorkerDocfixTests(unittest.TestCase):
    SECRET = "s" * 32

    def setUp(self):
        self.doc_text = "Intro line\nscaramouche-verify:abcd1234\nBody text"
        self.written: list[str] = []
        self.reads: list[dict] = []
        for name, value in (
            ("VIDEO_RENDER_SECRET", self.SECRET),
            ("fetch_google_doc", self._fetch),
            ("_worker_rewrite_google_doc_text", lambda bot_name, display_name, text, instructions="": text.upper()),
            ("overwrite_google_doc", self._overwrite),
        ):
            patcher = mock.patch.object(worker, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        quiet_log = mock.patch.object(worker.RenderRequestHandler, "log_message", lambda *args: None)
        quiet_log.start()
        self.addCleanup(quiet_log.stop)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), worker.RenderRequestHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never through a proxy

    def _fetch(self, url, **options):
        self.reads.append(options)
        return {"text": self.doc_text, "title": "Doc"}

    def _overwrite(self, url, text):
        self.written.append(text)
        return {"doc_id": DOC_ID, "title": "Doc", "chars_written": len(text)}

    def post(self, payload: dict, *, secret: str | None = SECRET) -> tuple[int, dict]:
        headers = {"Content-Type": "application/json"}
        if secret:
            headers["X-Render-Secret"] = secret
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.server.server_address[1]}/docfix",
            data=json.dumps(payload).encode(), headers=headers, method="POST",
        )
        try:
            with self.opener.open(request, timeout=10) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def test_requests_without_the_secret_are_refused(self):
        status, _ = self.post({"doc_url": DOC}, secret=None)
        self.assertEqual(status, 401)
        self.assertEqual(self.written, [])

    def test_a_missing_verification_code_stops_the_rewrite(self):
        status, body = self.post({"doc_url": DOC, "verify_marker": "scaramouche-verify:ffff0000"})
        self.assertEqual((status, body.get("error")), (403, "verification_code_missing"))
        self.assertEqual(self.written, [])

    def test_the_code_is_checked_and_removed_from_the_document(self):
        status, body = self.post({"doc_url": DOC, "verify_marker": "scaramouche-verify:abcd1234"})
        self.assertEqual(status, 200)
        self.assertTrue(body["verified"])
        self.assertEqual(self.written, ["INTRO LINE\nBODY TEXT"])
        # the whole document, without pending suggestions (a commenter could otherwise "type" the code)
        self.assertEqual(self.reads, [{"max_chars": 0, "without_suggestions": True}])

    def test_a_document_too_long_to_rewrite_whole_is_left_alone(self):
        self.doc_text = "word " * 2000
        status, body = self.post({"doc_url": DOC})
        self.assertEqual(status, 413)
        self.assertTrue(body["error"].startswith("document_too_long:"))
        self.assertEqual(self.written, [])

    def test_without_a_code_the_worker_says_nothing_was_verified(self):
        status, body = self.post({"doc_url": DOC})
        self.assertEqual(status, 200)
        self.assertFalse(body["verified"])


class WorkerRewriteTests(unittest.TestCase):
    @staticmethod
    def fake_groq(finish_reason: str, content: str = "Revised."):
        choice = NS(finish_reason=finish_reason, message=NS(content=content))
        client = NS(chat=NS(completions=NS(create=lambda **kwargs: NS(choices=[choice]))))
        return mock.patch("groq.Groq", lambda api_key: client)

    def test_a_cut_off_rewrite_is_never_written_back(self):
        with mock.patch.object(worker, "_load_groq_keys", lambda: ["key"]), self.fake_groq("length"), \
                self.assertRaises(RuntimeError):
            worker._worker_rewrite_google_doc_text("scaramouche", "you", "Original text.")

    def test_a_complete_rewrite_is_returned(self):
        with mock.patch.object(worker, "_load_groq_keys", lambda: ["key"]), self.fake_groq("stop"):
            self.assertEqual(worker._worker_rewrite_google_doc_text("scaramouche", "you", "Original text."), "Revised.")


# ── memory scoping ───────────────────────────────────────────────────────────
class MemoryScopingTests(BotTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.seed_messages()

    async def test_server_scope_leaves_out_dms_and_other_servers(self):
        self.assertEqual(await self.mem.get_recent_messages(1, 10, guild_id=100), ["said in server A"])

    async def test_public_scope_never_includes_dms(self):
        await self.mem.track_channel(30, None)  # a row without a server never counts as public
        await self.mem.add_message(1, 30, "user", "no server")
        self.assertCountEqual(
            await self.mem.get_recent_messages(1, 10, public_only=True), ["said in server A", "said in server B"],
        )

    async def test_unscoped_history_is_unchanged(self):
        self.assertEqual(len(await self.mem.get_recent_messages(1, 10)), 3)

    async def test_profiling_only_sees_what_the_audience_could_see(self):
        self.assertEqual(await bot._visible_messages(make_ctx(2, guild_id=100), 1, 10), ["said in server A"])
        self.assertEqual(len(await bot._visible_messages(make_ctx(1), 1, 10)), 3)  # your own DM with the bot
        self.assertCountEqual(
            await bot._visible_messages(make_ctx(2), 1, 10), ["said in server A", "said in server B"],
        )

    async def test_blackmail_never_quotes_dms_or_other_servers(self):
        qai = self.use(bot, "qai", AsyncMock(return_value="How incriminating."))
        target = NS(id=1, display_name="Target", mention="<@1>")
        await bot.blackmail_cmd.callback(make_ctx(2, guild_id=100), target)
        prompt = qai.await_args.args[0]
        self.assertIn("said in server A", prompt)
        self.assertNotIn("dm secret", prompt)
        self.assertNotIn("said in server B", prompt)

    async def test_rank_counts_only_this_servers_messages(self):
        await self.mem.add_message(2, 10, "user", "hi")
        await self.mem.add_message(2, 10, "assistant", "a bot reply is not counted")
        for _ in range(5):
            await self.mem.add_message(3, 20, "user", "busy elsewhere")
        members = {uid: NS(id=uid, display_name=f"member{uid}", bot=False) for uid in (1, 2, 3)}
        ctx = make_ctx(1, guild_id=100, members=members)
        self.use(bot, "qai", AsyncMock(return_value="Tolerable. Barely."))
        await bot.rank_cmd.callback(ctx)
        description = ctx.send.await_args.kwargs["embed"].description
        self.assertIn("member1** — 1 messages", description)
        self.assertIn("member2** — 1 messages", description)
        self.assertNotIn("member3", description)  # a member here, but only active in another server

    async def test_doc_verification_is_per_person_and_expires(self):
        self.assertFalse(await self.mem.is_doc_verified(DOC_ID, 5))
        await self.mem.mark_doc_verified(DOC_ID, 5)
        self.assertTrue(await self.mem.is_doc_verified(DOC_ID, 5))
        self.assertFalse(await self.mem.is_doc_verified(DOC_ID, 6))
        await self.mem.mark_doc_verified(DOC_ID, 6)
        self.assertTrue(await self.mem.is_doc_verified(DOC_ID, 5))
        self.assertTrue(await self.mem.is_doc_verified(DOC_ID, 6))
        with mock.patch.object(memory.Memory, "DOC_VERIFY_TTL_SECONDS", 0):
            self.assertFalse(await self.mem.is_doc_verified(DOC_ID, 5))

    async def test_adult_confirmation_is_stored(self):
        await self.mem.upsert_user(5, "user5", "user5")
        self.assertFalse((await self.mem.get_user(5))["adult_confirmed"])
        await self.mem.set_mode(5, "adult_confirmed", True)
        self.assertTrue((await self.mem.get_user(5))["adult_confirmed"])
        with self.assertRaises(ValueError):
            await self.mem.set_mode(5, "affection", True)


# ── unfiltered mode ──────────────────────────────────────────────────────────
class AdultModeTests(BotTestCase):
    def test_unfiltered_prompt_needs_confirmation_and_an_allowed_place(self):
        user = {"nsfw_mode": True, "romance_mode": False}
        self.assertNotIn(bot._NSFW, bot.build_system({**user, "adult_confirmed": False}, allow_nsfw=True))
        self.assertNotIn(bot._NSFW, bot.build_system({**user, "adult_confirmed": True}, allow_nsfw=False))
        self.assertNotIn(bot._NSFW, bot.build_system({**user, "adult_confirmed": True}))
        self.assertIn(bot._NSFW, bot.build_system({**user, "adult_confirmed": True}, allow_nsfw=True))
        romance = {**user, "romance_mode": True, "adult_confirmed": True}
        self.assertIn("Unfiltered Romance — Kai", bot.build_system(romance, "Kai", allow_nsfw=True))
        self.assertNotIn("Unfiltered", bot.build_system(romance, "Kai"))

    def test_only_dms_and_age_restricted_channels_allow_it(self):
        guild = NS(id=1)
        self.assertTrue(bot._channel_allows_nsfw(None, is_dm=True))
        self.assertFalse(bot._channel_allows_nsfw(None))
        self.assertFalse(bot._channel_allows_nsfw(NS(guild=guild, is_nsfw=lambda: False)))
        self.assertTrue(bot._channel_allows_nsfw(NS(guild=guild, is_nsfw=lambda: True)))
        confirmed = {"nsfw_mode": True, "adult_confirmed": True}
        self.assertFalse(bot._unfiltered(confirmed, False))
        self.assertFalse(bot._unfiltered({"nsfw_mode": True}, True))
        self.assertFalse(bot._unfiltered({"adult_confirmed": True}, True))
        self.assertTrue(bot._unfiltered(confirmed, True))

    async def test_command_refuses_public_channels(self):
        ctx = make_ctx(5, guild_id=100)
        await bot.nsfw_cmd.callback(ctx, "on")
        self.assertIn("age-restricted", said(ctx))
        self.assertFalse((await self.mem.get_user(5))["nsfw_mode"])

    async def test_command_asks_for_an_18_plus_confirmation_first(self):
        ctx = make_ctx(5)  # a DM
        await bot.nsfw_cmd.callback(ctx, "on")
        self.assertIsInstance(ctx.reply.await_args.kwargs.get("view"), bot.AdultConfirmView)
        self.assertFalse((await self.mem.get_user(5))["nsfw_mode"])

    async def test_once_confirmed_the_command_works_in_age_restricted_channels(self):
        await self.mem.upsert_user(5, "user5", "user5")
        await self.mem.set_mode(5, "adult_confirmed", True)
        await bot.nsfw_cmd.callback(make_ctx(5, guild_id=100, nsfw=True), "on")
        self.assertTrue((await self.mem.get_user(5))["nsfw_mode"])

    async def test_the_confirmation_button_belongs_to_the_person_who_asked(self):
        await self.mem.upsert_user(5, "user5", "user5")
        button = bot.AdultConfirmView(5).children[0]

        def interaction(uid):
            return NS(user=NS(id=uid), response=NS(send_message=AsyncMock(), edit_message=AsyncMock(), is_done=lambda: False))

        stranger = interaction(6)
        await button.callback(stranger)
        stranger.response.send_message.assert_awaited_once()
        self.assertFalse((await self.mem.get_user(5))["adult_confirmed"])
        asker = interaction(5)
        await button.callback(asker)
        user = await self.mem.get_user(5)
        self.assertTrue(user["adult_confirmed"] and user["nsfw_mode"])


# ── !fixdoc ──────────────────────────────────────────────────────────────────
class FixDocTests(BotTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        bot._DOC_CHALLENGES.clear()
        self.addCleanup(bot._DOC_CHALLENGES.clear)
        self.doc_text = "My essay\nIt are good."
        self.written: list[str] = []
        self.use(bot, "google_docs_ready", lambda: (True, ""))
        self.use(bot, "service_account_email", lambda: "bot@example.iam.gserviceaccount.com")
        self.reads: list[dict] = []
        self.use(bot, "fetch_google_doc", self._fetch)
        self.use(bot, "overwrite_google_doc", self._overwrite)
        self.use(bot, "_rewrite_google_doc_text", AsyncMock(side_effect=lambda name, text, instructions="": text.upper()))

    def _fetch(self, link, **options):
        self.reads.append(options)
        return {"text": self.doc_text, "title": "Essay"}

    def _overwrite(self, link, text):
        self.written.append(text)
        return {"doc_id": DOC_ID, "title": "Essay", "chars_written": len(text)}

    async def test_a_member_must_put_the_code_in_the_document_first(self):
        ctx = make_ctx(5, guild_id=100)
        await bot._run_docfix_request(ctx, f"{DOC} fix the grammar")
        self.assertEqual(self.written, [])
        code = re.search(r"scaramouche-verify:[0-9a-f]+", said(ctx)).group(0)

        ctx = make_ctx(5, guild_id=100)
        await bot._run_docfix_request(ctx, f"{DOC} fix the grammar")  # the code is not in the document yet
        self.assertEqual(self.written, [])
        self.assertIn(f"I don't see `{code}`", said(ctx))
        self.assertFalse(await self.mem.is_doc_verified(DOC_ID, 5))

        self.doc_text = f"My essay\n{code}\nIt are good."
        await bot._run_docfix_request(make_ctx(5, guild_id=100), f"{DOC} fix the grammar")
        self.assertEqual(self.written, ["MY ESSAY\nIT ARE GOOD."])  # rewritten without the line holding the code
        self.assertTrue(await self.mem.is_doc_verified(DOC_ID, 5))
        self.assertTrue(all(read == {"max_chars": 0, "without_suggestions": True} for read in self.reads))

        self.doc_text = "My essay\nIt is good."
        await bot._run_docfix_request(make_ctx(5, guild_id=100), DOC)  # verified: no new code needed
        self.assertEqual(len(self.written), 2)

    async def test_each_editor_proves_access_separately(self):
        await self.mem.mark_doc_verified(DOC_ID, 5)
        ctx = make_ctx(6, guild_id=100)
        await bot._run_docfix_request(ctx, DOC)
        self.assertEqual(self.written, [])
        self.assertIn("scaramouche-verify:", said(ctx))

    async def test_the_owner_is_not_asked(self):
        self.use(bot, "OWNER_ID", 5)
        await bot._run_docfix_request(make_ctx(5), DOC)
        self.assertEqual(len(self.written), 1)

    async def test_a_document_too_long_to_rewrite_whole_is_left_alone(self):
        self.use(bot, "OWNER_ID", 5)
        self.doc_text = "word " * 2000  # 10,000 characters: the old code rewrote the start and deleted the rest
        ctx = make_ctx(5)
        await bot._run_docfix_request(ctx, DOC)
        self.assertEqual(self.written, [])
        self.assertIn("too long", said(ctx))

    async def test_worker_mode_reports_a_document_that_is_too_long(self):
        self.use(bot, "OWNER_ID", 5)
        self.use(bot, "google_docs_ready", lambda: (False, "no Google credential here"))
        self.use(bot, "remote_fix_google_doc", AsyncMock(side_effect=RuntimeError(
            'Remote doc editing failed across all configured workers.\n[1] https://w -> Remote doc worker rejected '
            'the request (413): {"ok": false, "error": "document_too_long:9000"}'
        )))
        ctx = make_ctx(5)
        await bot._run_docfix_request(ctx, DOC)
        self.assertIn("9,000 characters", said(ctx))

    async def test_codes_expire(self):
        bot._DOC_CHALLENGES[(5, DOC_ID)] = ("scaramouche-verify:0ld", time.time() - 1)
        self.assertEqual(bot._docfix_challenge(5, DOC_ID, create=False), "")
        self.assertNotIn((5, DOC_ID), bot._DOC_CHALLENGES)

    async def test_worker_mode_sends_the_code_and_trusts_only_a_confirmed_check(self):
        self.use(bot, "google_docs_ready", lambda: (False, "no Google credential here"))
        remote = self.use(bot, "remote_fix_google_doc", AsyncMock(side_effect=RuntimeError(
            'Remote doc editing failed across all configured workers.\n[1] https://w -> Remote doc worker rejected '
            'the request (403): {"ok": false, "error": "verification_code_missing"}'
        )))
        await bot._run_docfix_request(make_ctx(5), DOC)  # hands out a code; the worker is not called yet
        remote.assert_not_awaited()
        code = bot._DOC_CHALLENGES[(5, DOC_ID)][0]

        ctx = make_ctx(5)
        await bot._run_docfix_request(ctx, DOC)
        self.assertEqual(remote.await_args.kwargs["verify_marker"], code)
        self.assertIn("I don't see", said(ctx))

        remote.side_effect = None
        remote.return_value = {"ok": True, "title": "Essay"}  # an outdated worker that ignored the code
        await bot._run_docfix_request(make_ctx(5), DOC)
        self.assertFalse(await self.mem.is_doc_verified(DOC_ID, 5))

        remote.return_value = {"ok": True, "title": "Essay", "verified": True}
        await bot._run_docfix_request(make_ctx(5), DOC)
        self.assertTrue(await self.mem.is_doc_verified(DOC_ID, 5))


# ── !memories and long replies ───────────────────────────────────────────────
class MemoriesCommandTests(BotTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.use(self.mem, "get_top_topics", AsyncMock(return_value=[]))
        self.use(self.mem, "get_memory_bank_entries", AsyncMock(return_value=[{"kind": "secret", "memory": "told you in a DM"}]))
        self.use(self.mem, "get_scene_state", AsyncMock(return_value=None))

    async def test_in_a_server_they_are_sent_privately(self):
        ctx = make_ctx(5, guild_id=100)
        await bot.memories_cmd.callback(ctx)
        self.assertIn("told you in a DM", ctx.author.send.await_args.args[0])
        self.assertNotIn("told you in a DM", said(ctx))

    async def test_closed_dms_are_no_reason_to_post_them_publicly(self):
        ctx = make_ctx(5, guild_id=100)
        ctx.author.send.side_effect = discord.Forbidden(NS(status=403, reason="Forbidden"), "Cannot send messages")
        await bot.memories_cmd.callback(ctx)
        self.assertNotIn("told you in a DM", said(ctx))
        self.assertIn("DMs are closed", said(ctx))

    async def test_in_a_dm_they_are_shown_directly(self):
        ctx = make_ctx(5)
        await bot.memories_cmd.callback(ctx)
        self.assertIn("told you in a DM", said(ctx))


class LongReplyTests(BotTestCase):
    TEXT = " ".join(f"word{i}" for i in range(900))  # about 6,000 characters

    def assert_delivered_in_pieces(self, ctx):
        pieces = [call.args[0] for call in ctx.reply.await_args_list + ctx.send.await_args_list]
        self.assertGreater(len(pieces), 1)
        self.assertTrue(all(len(piece) <= 2000 for piece in pieces))
        self.assertEqual(" ".join(pieces).split(), self.TEXT.split())

    async def test_safe_reply_splits_instead_of_failing(self):
        ctx = make_ctx(5)
        await bot.safe_reply(ctx, self.TEXT)
        ctx.reply.assert_awaited_once()
        self.assert_delivered_in_pieces(ctx)

    async def test_safe_send_splits_instead_of_failing(self):
        ctx = make_ctx(5)
        await bot.safe_send(ctx, self.TEXT)
        self.assert_delivered_in_pieces(ctx)


if __name__ == "__main__":
    unittest.main()
