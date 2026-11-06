import os
from pathlib import Path
import tempfile
import unittest
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from awareness_features import (
    activity_snapshot, choose_duo_advice_mode, classify_safety,
    playful_negative_target, resolve_voice_state,
    select_relevant_recall, style_voice_text,
)
from integrations import (
    GitHubIssueService, GoogleCalendarService, GoogleSheetsService,
    GoogleTasksService, IntegrationConfig,
    LetterboxdService, SpotifyService, SteamService,
)
from integrations.http import AsyncJSONClient, IntegrationAuthError, IntegrationError
from memory import Memory


ROOT = Path(__file__).resolve().parents[1]
BOT_SOURCE = ROOT.joinpath("bot.py").read_text(encoding="utf-8")
VOICE_SOURCE = ROOT.joinpath("voice_handler.py").read_text(encoding="utf-8")


class AwarenessFeatureTests(unittest.TestCase):
    def test_duo_advice_is_bounded_and_safety_aware(self):
        self.assertEqual(choose_duo_advice_mode("How do I fix this assignment?", 0.01), "goodcop")
        self.assertEqual(choose_duo_advice_mode("Should I give them some space?", 0.07), "contradict")
        self.assertEqual(choose_duo_advice_mode("What is the capital of France?", 0.01), "")
        self.assertEqual(choose_duo_advice_mode("How do I calculate 18 percent of 40?", 0.01), "")
        self.assertEqual(choose_duo_advice_mode("I cannot breathe, call 911", 0.01), "protective")
        safety = classify_safety("I'm overwhelmed and having an awful day")
        self.assertTrue(safety.protective)
        self.assertFalse(classify_safety("Scaramouche is annoyingly dramatic").protective)

    def test_tattletale_requires_a_real_playful_target(self):
        names = {"scaramouche": ("scaramouche", "scara"), "wanderer": ("wanderer",)}
        self.assertEqual(playful_negative_target("Scaramouche was annoying today", names), "scaramouche")
        self.assertEqual(playful_negative_target("Scaramouche made me feel unsafe", names), "")
        self.assertEqual(playful_negative_target("Nothing happened", names), "")

    def test_this_you_uses_exact_provenance_and_filters_sensitive_text(self):
        candidates = [{"id": 91, "channel_id": 12, "content": "I always said I hated green apples", "ts": 100.0}]
        match = select_relevant_recall("I never said I hated green apples", candidates)
        self.assertIsNotNone(match)
        self.assertEqual(match.message_id, 91)
        self.assertEqual(match.content, candidates[0]["content"])
        self.assertIsNone(select_relevant_recall("My password is green apples", candidates))

    def test_voice_resolution_clamps_smooths_and_strips_controls(self):
        heated = resolve_voice_state({"conflict_open": True}, -9)
        concerned = resolve_voice_state({}, 0, delivery_intent="protective concern", previous=heated)
        self.assertEqual(heated.category, "heated")
        self.assertEqual(concerned.category, "concerned")
        self.assertGreaterEqual(concerned.temperature, 0.1)
        self.assertLessEqual(concerned.temperature, 1.0)
        styled = style_voice_text("[speed=9] <pitch high> Stay... here.", concerned)
        self.assertNotIn("speed", styled)
        self.assertNotIn("pitch", styled)

    def test_presence_uses_only_exposed_fields(self):
        class CustomActivity:
            name = "A custom status"
            type = "custom"
        class Spotify:
            title = "A Song"
            artist = "An Artist"
            album = "An Album"
            timestamps = None
        class Member:
            activities = [CustomActivity(), Spotify()]
        snapshot = activity_snapshot(Member())
        self.assertEqual(snapshot["kind"], "spotify")
        self.assertEqual(snapshot["name"], "A Song")
        self.assertNotIn("history", snapshot)
        Member.activities = []
        self.assertIsNone(activity_snapshot(Member()))

    def test_source_guards_and_turn_caps_are_present(self):
        self.assertIn('"goodcop": 1', BOT_SOURCE)
        self.assertIn('"protective": 1', BOT_SOURCE)
        self.assertIn("await message.channel.fetch_message(event[\"message_id\"])", BOT_SOURCE)
        self.assertIn("intents.presences = True", BOT_SOURCE)
        self.assertIn("if joined_here and voice and voice.is_connected()", BOT_SOURCE)
        self.assertIn("candidate.author.id != participant_id", BOT_SOURCE)
        self.assertIn("PARTICIPANT_LATEST_ANSWER", BOT_SOURCE)
        self.assertIn("chunk = 220", VOICE_SOURCE)
        self.assertNotIn("if mood <= -6:   chunk", VOICE_SOURCE)
        self.assertIn("if is_dm or not PARTNER_BOT_ID or not message.guild.get_member(PARTNER_BOT_ID)", BOT_SOURCE)
        self.assertIn("be the sharp bad cop", BOT_SOURCE)
        self.assertNotIn("qai", ROOT.joinpath("awareness_features.py").read_text(encoding="utf-8"))


class MemoryProvenanceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.memory = Memory("scaramouche", db_path=os.path.join(self.tmp.name, "local.db"), shared_db_path=os.path.join(self.tmp.name, "shared.db"))
        await self.memory.init()
        await self.memory.upsert_user(7, "user", "User")

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_tattletale_provenance_and_channel_boundary(self):
        await self.memory.record_tattletale_event(7, "wanderer", "scaramouche", 12, 99, "Wanderer was annoying today")
        event = await self.memory.get_tattletale_event(7, "wanderer", 12)
        self.assertEqual(event["message_id"], 99)
        self.assertEqual(event["content"], "Wanderer was annoying today")
        self.assertIsNone(await self.memory.get_tattletale_event(7, "wanderer", 13))

    async def test_recall_candidates_include_real_ids_and_are_bounded(self):
        await self.memory.add_message(7, 12, "user", "I always said I hated green apples")
        await self.memory.add_message(7, 13, "user", "This belongs to another channel")
        rows = await self.memory.get_user_message_candidates(7, 12, 1000)
        self.assertEqual(len(rows), 1)
        self.assertGreater(rows[0]["id"], 0)
        self.assertGreater(rows[0]["ts"], 0)

    async def test_shared_cooldown_is_atomic(self):
        results = await asyncio.gather(
            self.memory.consume_shared_cooldown("one-winner", 60),
            self.memory.consume_shared_cooldown("one-winner", 60),
        )
        self.assertEqual(sum(1 for allowed, _ in results if allowed), 1)


class SoundboardTests(unittest.IsolatedAsyncioTestCase):
    async def test_permission_failure_and_joined_connection_cleanup(self):
        # This test imports the runtime for the real soundboard helper. Remove it
        # afterward so lifecycle tests can import it with their own env fixture.
        self.addCleanup(sys.modules.pop, "bot", None)
        import bot as bot_module

        denied_channel = SimpleNamespace(permissions_for=lambda _member: SimpleNamespace(connect=False, speak=True))
        denied_ctx = SimpleNamespace(
            guild=SimpleNamespace(id=1, me=object(), voice_client=None),
            author=SimpleNamespace(voice=SimpleNamespace(channel=denied_channel)),
        )
        with patch.object(bot_module, "SOUNDBOARD_GUILD_IDS", {1}), patch.object(bot_module, "_soundboard_assets", return_value={"sigh":"/tmp/sigh.mp3"}):
            played, message = await bot_module._play_soundboard(denied_ctx, "sigh")
        self.assertFalse(played)
        self.assertIn("cannot connect", message)

        class Voice:
            def __init__(self): self.played=False; self.disconnected=False
            def play(self, _source): self.played=True
            def is_playing(self): return False
            def is_connected(self): return True
            async def disconnect(self, **_kwargs): self.disconnected=True
        voice = Voice()
        channel = SimpleNamespace(
            permissions_for=lambda _member: SimpleNamespace(connect=True, speak=True),
            connect=AsyncMock(return_value=voice),
        )
        ctx = SimpleNamespace(
            guild=SimpleNamespace(id=1, me=object(), voice_client=None),
            author=SimpleNamespace(voice=SimpleNamespace(channel=channel)),
        )
        cooldown = AsyncMock(return_value=(True, 0))
        with patch.object(bot_module, "SOUNDBOARD_GUILD_IDS", {1}), \
             patch.object(bot_module, "_soundboard_assets", return_value={"sigh":"/tmp/sigh.mp3"}), \
             patch.object(bot_module.mem, "consume_phrase_with_status", cooldown), \
             patch.object(bot_module.discord, "FFmpegPCMAudio", return_value=object()):
            played, message = await bot_module._play_soundboard(ctx, "sigh")
        self.assertTrue(played)
        self.assertEqual(message, "")
        self.assertTrue(voice.played)
        self.assertTrue(voice.disconnected)


class FakeClient:
    def __init__(self, result=None): self.result = result or {}; self.calls = []
    async def request(self, *args, **kwargs): self.calls.append((args, kwargs)); return self.result


class FakeResponse:
    def __init__(self, status, payload=None, headers=None): self.status=status; self.payload=payload or {}; self.headers=headers or {}
    async def __aenter__(self): return self
    async def __aexit__(self, *args): return False
    async def json(self, **kwargs): return self.payload


class FakeSession:
    def __init__(self, factory): self.factory=factory
    async def __aenter__(self): return self
    async def __aexit__(self, *args): return False
    def request(self, *args, **kwargs):
        item=self.factory.items.pop(0)
        if isinstance(item, BaseException): raise item
        return item


class SessionFactory:
    def __init__(self, items): self.items=list(items)
    def __call__(self, **kwargs): return FakeSession(self)


async def no_sleep(_): pass


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_services_and_redacted_config(self):
        self.assertFalse(SpotifyService({}).ready)
        self.assertFalse(SteamService({}).ready)
        self.assertFalse(LetterboxdService.ready)
        self.assertNotIn("secret", repr(IntegrationConfig({"token": "secret"})))

    async def test_github_allowlist_confirmation_and_mock_success(self):
        fake = FakeClient({"number": 5, "html_url": "https://example.test/issues/5"})
        service = GitHubIssueService({"token": "secret", "allowed_repositories": ["owner/repo"], "dry_run": False}, fake)
        dry = await service.create_issue("owner/repo", "Title", "Body", confirmed=False)
        self.assertTrue(dry["dry_run"])
        self.assertEqual(fake.calls, [])
        live = await service.create_issue("owner/repo", "Title", "Body", confirmed=True)
        self.assertEqual(live["number"], 5)
        with self.assertRaises(PermissionError):
            await service.create_issue("other/repo", "Title", "Body", confirmed=True)

    async def test_calendar_timezone_and_destructive_boundary(self):
        fake = FakeClient({})
        service = GoogleCalendarService({"access_token":"token", "expires_at":99999999999}, fake)
        with self.assertRaises(ValueError):
            await service.create_event("Event", datetime.now(), datetime.now())
        with self.assertRaises(PermissionError):
            await service.update_bot_event("id", {}, {"summary":"changed"})
        aware = datetime.now(timezone.utc)
        preview = await service.create_event("Event", aware, aware + timedelta(hours=1))
        self.assertTrue(preview["dry_run"])
        self.assertEqual(fake.calls, [])
        await service.create_event("Event", aware, aware + timedelta(hours=1), confirmed=True)

    async def test_google_account_separation_tasks_and_sheet_allowlist(self):
        config = IntegrationConfig({"google":{"accounts":{"7":{"access_token":"a"},"8":{"access_token":"b"}}}})
        self.assertEqual(config.google_account(7)["access_token"], "a")
        self.assertEqual(config.google_account(8)["access_token"], "b")
        self.assertEqual(config.google_account(9), {})
        tasks = GoogleTasksService({"access_token":"a","expires_at":99999999999}, FakeClient({}))
        with self.assertRaises(ValueError):
            await tasks.update_task("id", {"due":"2026-09-26T12:00:00"})
        sheets = GoogleSheetsService({"access_token":"a","expires_at":99999999999,"allowed_spreadsheets":["approved"]}, FakeClient({}))
        with self.assertRaises(PermissionError):
            await sheets.append_score_rows("other", [["score"]])
        await sheets.append_score_rows("approved", [["score"]])

    async def test_http_auth_timeout_and_rate_limit_retry(self):
        auth = AsyncJSONClient(session_factory=SessionFactory([FakeResponse(401)]), sleep=no_sleep)
        with self.assertRaises(IntegrationAuthError):
            await auth.request("GET", "https://example.test")
        timeout = AsyncJSONClient(session_factory=SessionFactory([asyncio.TimeoutError(), asyncio.TimeoutError()]), sleep=no_sleep)
        with self.assertRaisesRegex(IntegrationError, "timed out"):
            await timeout.request("GET", "https://example.test")
        rate = AsyncJSONClient(session_factory=SessionFactory([FakeResponse(429, headers={"Retry-After":"0"}), FakeResponse(200, {"ok":True})]), sleep=no_sleep)
        self.assertEqual(await rate.request("GET", "https://example.test"), {"ok":True})


if __name__ == "__main__":
    unittest.main()
