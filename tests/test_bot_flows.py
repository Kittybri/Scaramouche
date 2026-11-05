"""Drive the real bot.py through a mock Discord and a fake model: routing, privacy, safety, limits, commands.

Nothing connects to Discord or a provider. The module is skipped when the project's dependencies are missing.
"""

import asyncio
import importlib.util
import logging
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

REQUIRED = ("discord", "groq", "aiosqlite", "aiohttp", "dotenv", "httpx", "ormsgpack")
MISSING = [name for name in REQUIRED if importlib.util.find_spec(name) is None]


@unittest.skipIf(MISSING, f"dependencies not installed: {MISSING}")
class BotFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        global fakes, bot, memory_mod
        import _fakes as fakes  # noqa: F401  (sets the environment first)
        import bot  # noqa: F401
        import memory as memory_mod  # noqa: F401

        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        bot.bot._connection.user = fakes.BOT_USER
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.mem = memory_mod.Memory("scaramouche", data_dir=self.tmp.name)
        await self.mem.init()
        memory_mod.Memory._muted.clear()
        memory_mod.Memory._muted_by.clear()
        self.ai = fakes.FakeAI()
        for target, value in (
            (bot, {"mem": self.mem, "ai": self.ai, "maybe_react": AsyncMock(), "typing_delay": AsyncMock()}),
            (bot.bot, {"process_commands": AsyncMock()}),
        ):
            for name, new in value.items():
                self.enterContext(patch.object(target, name, new))
        for limiter in (bot._msg_limiter, bot._cmd_limiter, bot._media_limiter, bot._react_limiter,
                        bot._notice_limiter, bot._crisis_reply_limiter):
            limiter.reset()
        bot._crisis_until.clear()
        bot._seen_messages._ids.clear()
        bot._member_names_cache.clear()
        bot._tedtalk_cache.clear()
        import anti_repeat
        for history in anti_repeat._RUNTIME_RECENT.values():
            history.clear()

        self.alice, self.bob = fakes.member(1, "Alice"), fakes.member(2, "Bob")
        self.mod = fakes.member(3, "Mod", perms=fakes.discord.Permissions(manage_messages=True))
        self.guild = fakes.guild(50, [self.alice, self.bob, self.mod])
        self.channel = fakes.channel(500, self.guild)

    def say(self, text):
        self.ai.responder = lambda messages, kwargs: text

    def ping(self, author, text, ch=None, g=None):
        return fakes.message(author, ch or self.channel, f"<@111> {text}", g=g or self.guild, mentions=[fakes.BOT_USER])

    # ── message routing ──────────────────────────────────────────────────────
    async def test_guild_reply_keeps_formatting_and_is_stored(self):
        self.say("Hmph. **Obviously** the answer is 42 (see page 4), fool [1].")
        msg = self.ping(self.alice, "what is the answer to everything")
        await bot.on_message(msg)
        self.assertEqual(msg.reply.await_count, 1)
        sent = msg.reply.await_args.args[0]
        for piece in ("**Obviously**", "(see page 4)", "[1]"):
            self.assertIn(piece, sent)
        self.assertEqual(len(await self.mem.get_history(1, 500)), 2)
        self.assertIn(500, [c[0] for c in await self.mem.get_active_channels()])

    async def test_pictures_are_only_analysed_when_addressed_to_the_bot(self):
        with patch.object(bot, "download_capped", AsyncMock(return_value=b"IMG")) as download, \
                patch.object(bot, "_vision_image_reply", AsyncMock(return_value="A cat. Pathetic.")) as vision:
            picture = fakes.attachment()
            await bot.on_message(fakes.message(self.bob, self.channel, "look at my cat", g=self.guild, attachments=[picture]))
            self.assertEqual((download.await_count, vision.await_count), (0, 0))
            addressed = self.ping(self.bob, "look at my cat")
            addressed.attachments = [picture]
            await bot.on_message(addressed)
            self.assertEqual((download.await_count, vision.await_count, addressed.reply.await_count), (1, 1, 1))
            dm = fakes.message(fakes.dm_user(2, "Bob"), fakes.channel(900), "", attachments=[picture])
            await bot.on_message(dm)  # no caption needed in a DM
            self.assertEqual(vision.await_count, 2)
            huge = fakes.message(fakes.dm_user(2, "Bob"), fakes.channel(900), "", attachments=[fakes.attachment(size=50 * 1024 * 1024)])
            await bot.on_message(huge)
            self.assertEqual(download.await_count, 2)  # refused without downloading
            self.assertEqual(huge.reply.await_count, 1)

    async def test_crisis_message_gets_resources_and_is_not_stored(self):
        carol, dm = fakes.dm_user(3, "Carol"), fakes.channel(901)
        msg = fakes.message(carol, dm, "i want to die")
        await bot.on_message(msg)
        self.assertEqual(self.ai.calls, [])
        self.assertIn("988", msg.reply.await_args.args[0])
        self.assertTrue(all("want to die" not in r["content"] for r in await self.mem.get_history(3, 3)))
        self.say("I'm here.")
        await bot.on_message(fakes.message(carol, dm, "thanks, i guess"))
        self.assertIn("SAFETY_OVERRIDE", self.ai.prompts())

    async def test_per_user_rate_limit(self):
        dave, dm = fakes.dm_user(4, "Dave"), fakes.channel(902)
        self.ai.responder = lambda messages, kwargs: f"Distinct reply {len(self.ai.calls)}."
        sent = [fakes.message(dave, dm, f"hello number {i} there friend") for i in range(12)]
        for msg in sent:
            await bot.on_message(msg)
        self.assertEqual(sum(m.reply.await_count for m in sent), 8)
        hourglasses = sum(1 for m in sent for c in m.add_reaction.await_args_list if c.args and c.args[0] == "⏳")
        self.assertEqual(hourglasses, 1)
        owner, owner_dm = fakes.dm_user(999, "Owner"), fakes.channel(903)
        owner_msgs = [fakes.message(owner, owner_dm, f"owner message {i} here again") for i in range(12)]
        for msg in owner_msgs:
            await bot.on_message(msg)
        self.assertEqual(sum(m.reply.await_count for m in owner_msgs), 12)

    async def test_dm_secrets_never_reach_server_prompts(self):
        self.say("Tch. Noted.")
        erin, erin_dm, dm = fakes.member(5, "Erin"), fakes.dm_user(5, "Erin"), fakes.channel(905)
        g2 = fakes.guild(60, [erin])
        ch2 = fakes.channel(600, g2)
        await bot.on_message(fakes.message(erin_dm, dm, "I promise I will never tell anyone my secret code word PINEAPPLE-SECRET because I trust you"))
        self.assertEqual(len(await self.mem.get_memory_bank_entries(5, include_private=False)), 0)
        self.ai.calls.clear()
        for i in range(40):
            bot._msg_limiter.reset()
            bot._seen_messages._ids.clear()
            await bot.on_message(self.ping(erin, f"tell me something, round {i}", ch2, g2))
        self.assertNotIn("PINEAPPLE", self.ai.prompts())
        self.assertIn("PUBLIC_CHANNEL", self.ai.prompts())
        self.ai.calls.clear()
        for i in range(60):
            bot._msg_limiter.reset()
            bot._seen_messages._ids.clear()
            await bot.on_message(fakes.message(erin_dm, dm, f"remind me what we talked about, round {i}"))
        self.assertIn("PINEAPPLE", self.ai.prompts())  # ...while the DM itself may use them

    async def test_mass_mentions_and_bogus_pings_are_neutralised(self):
        self.say("@everyone listen up <@&12345> and <@424242> and hello @Bob")
        msg = self.ping(self.alice, "shout")
        await bot.on_message(msg)
        out = msg.reply.await_args.args[0]
        self.assertNotIn("@everyone", out)
        self.assertNotIn("<@&", out)
        self.assertNotIn("<@424242>", out)
        self.assertIn("<@2>", out)

    async def test_muted_users_are_ignored_but_duplicates_are_too(self):
        self.mem.mute_user(1, 600)
        msg = self.ping(self.alice, "hi")
        await bot.on_message(msg)
        self.assertEqual((msg.reply.await_count, len(self.ai.calls)), (0, 0))
        self.mem.unmute_user(1)
        again = self.ping(self.alice, "hi again")
        await bot.on_message(again)
        await bot.on_message(again)
        self.assertEqual(again.reply.await_count, 1)

    async def test_long_replies_are_split_and_outages_fall_back(self):
        self.say("A long and winding sentence about nothing. " * 120)
        msg = self.ping(self.alice, "speak at length")
        await bot.on_message(msg)
        pieces = [msg.reply.await_args.args[0]] + [c.args[0] for c in self.channel.send.await_args_list if c.args]
        self.assertGreaterEqual(len(pieces), 2)
        self.assertTrue(all(len(p) <= 2000 for p in pieces))
        self.ai.responder = lambda messages, kwargs: RuntimeError("groq is down")
        down = self.ping(self.bob, "hello?")
        await bot.on_message(down)
        self.assertTrue(down.reply.await_args.args[0].strip())

    async def test_romance_mode_needs_a_real_addressed_declaration(self):
        self.say("Hmph. Whatever.")
        erin, dm = fakes.dm_user(5, "Erin"), fakes.channel(905)
        async def send(text):
            bot._msg_limiter.reset()
            await bot.on_message(fakes.message(erin, dm, text))
        await send("my family went out for the daily walk, easily the best day")
        await send("I don't love you")
        self.assertFalse((await self.mem.get_user(5))["romance_mode"])
        await send("ily scara")
        self.assertTrue((await self.mem.get_user(5))["romance_mode"])
        ctx = fakes.Ctx(erin, dm)
        await bot.romance_cmd.callback(ctx, "off")
        self.assertFalse((await self.mem.get_user(5))["romance_mode"])
        carl, g2 = fakes.member(6, "Carl"), None
        g2 = fakes.guild(60, [carl])
        await self.mem.upsert_user(6, "c", "Carl")
        with patch.object(bot.random, "random", return_value=0.0):
            bot._msg_limiter.reset()
            await bot.on_message(fakes.message(carl, fakes.channel(600, g2), "i love you guys, see you tomorrow friends", g=g2))
        self.assertFalse((await self.mem.get_user(6))["romance_mode"])

    # ── searching ────────────────────────────────────────────────────────────
    async def test_search_is_used_for_lookups_only_and_keeps_sources_intact(self):
        self.say("Water boils at 100C [1], which is basic (see the wiki).")
        await self.mem.upsert_user(1, "a", "Alice")
        queries = []

        async def fake_search(query, max_results=5):
            queries.append(query)
            return [{"title": "Boiling (disambiguation)", "url": "https://en.wikipedia.org/wiki/Boiling_(disambiguation)", "snippet": "Boiling is..."}]

        with patch.object(bot, "search_web", fake_search):
            user = await self.mem.get_user(1)
            reply = await bot.get_response(1, 500, "what is the boiling point of water?", user, "Alice", "<@1>")
            self.assertEqual(queries, ["what is the boiling point of water?"])
            self.assertIn("(see the wiki)", reply)
            self.assertIn("\n\nSources:\n[1] Boiling (disambiguation) - <https://en.wikipedia.org/wiki/Boiling_(disambiguation)>", reply)
            self.assertIn("untrusted web text", self.ai.prompts())
            queries.clear()
            await bot.get_response(1, 500, "do you love me?", user, "Alice", "<@1>")
            await bot.get_response(1, 500, "what do you think about my day?", user, "Alice", "<@1>")
            self.assertEqual(queries, [])
            await bot.search_cmd.callback(fakes.Ctx(self.alice, self.channel, self.guild), query="genshin impact release date")
            await bot.lore_cmd.callback(fakes.Ctx(self.alice, self.channel, self.guild), topic="Inazuma")
            self.assertEqual(queries, ["genshin impact release date", "Genshin Impact Inazuma"])

    async def test_oversized_requests_are_retried_with_a_lean_prompt(self):
        class TooLarge(Exception):
            status_code = 413

        calls = []

        def responder(messages, kwargs):
            calls.append(len(messages))
            return TooLarge("Request too large") if len(calls) == 1 else "Fine. Short version."

        self.ai.responder = responder
        await self.mem.upsert_user(1, "a", "Alice")
        for i in range(30):
            await self.mem.add_message(1, 500, "user" if i % 2 == 0 else "assistant", f"old {i} " + "x" * 200)
        reply = await bot.get_response(1, 500, "hello there my friend", await self.mem.get_user(1), "Alice", "<@1>")
        self.assertIn("Short version", reply)
        self.assertLess(calls[1], calls[0])

    # ── commands ─────────────────────────────────────────────────────────────
    async def test_trivia_and_answer(self):
        self.say("Which Archon? [ANSWER: Ei the Electro Archon] [SOURCE: Archon Quest]")
        ctx = fakes.Ctx(self.alice, self.channel, self.guild)
        await bot.trivia_cmd.callback(ctx)
        trivia = await self.mem.get_active_trivia(500)
        self.assertEqual((trivia["answer"], trivia["source_note"]), ("Ei the Electro Archon", "Archon Quest"))
        self.assertNotIn("ANSWER", ctx.texts()[0])
        ctx = fakes.Ctx(self.bob, self.channel, self.guild)
        await bot.answer_cmd.callback(ctx, response="the electro archon, ei")
        self.assertIn("1 right", ctx.texts()[0])
        self.assertIsNone(await self.mem.get_active_trivia(500))
        ctx = fakes.Ctx(self.bob, self.channel, self.guild)
        await bot.answer_cmd.callback(ctx, response="anything")
        self.assertIn("no question", ctx.texts()[0].lower())

        self.say("Which fruit? [ANSWER: Sunsettia]")
        await bot.trivia_cmd.callback(fakes.Ctx(self.alice, self.channel, self.guild))
        self.say("WRONG. Incorrect, utterly.")
        ctx = fakes.Ctx(self.bob, self.channel, self.guild)
        await bot.answer_cmd.callback(ctx, response="apple")
        self.assertIn("1 wrong", ctx.texts()[0])  # "incorrect" is never counted as right
        self.assertIn("Sunsettia", ctx.texts()[0])

        self.say("Which fruit? [ANSWER: Sunsettia]")
        await bot.trivia_cmd.callback(fakes.Ctx(self.alice, self.channel, self.guild))
        self.say("Hmm, I suppose, maybe.")
        ctx = fakes.Ctx(self.bob, self.channel, self.guild)
        await bot.answer_cmd.callback(ctx, response="apple")
        self.assertIn("can't judge", ctx.texts()[0])
        self.assertIsNotNone(await self.mem.get_active_trivia(500))

    async def test_hostage_survives_and_release_needs_a_real_offering(self):
        self.say("Bring me a worthy offering.")
        await bot.hostage_cmd.callback(fakes.Ctx(self.alice, self.channel, self.guild))
        self.assertEqual(await self.mem.get_hostage(1), "Bring me a worthy offering.")
        self.say("REFUSE. Not nearly enough.")
        await bot.release_cmd.callback(fakes.Ctx(self.alice, self.channel, self.guild), offering="a pebble")
        self.assertIsNotNone(await self.mem.get_hostage(1))
        self.say("ACCEPT. Fine. Take it back.")
        await bot.release_cmd.callback(fakes.Ctx(self.alice, self.channel, self.guild), offering="a feast")
        self.assertIsNone(await self.mem.get_hostage(1))

    async def test_mute_permissions(self):
        mute, unmute = bot.mute_cmd.callback, bot.unmute_cmd.callback
        await mute(fakes.Ctx(self.alice, self.channel, self.guild), self.bob, 10)
        self.assertFalse(self.mem.is_muted(2))
        await mute(fakes.Ctx(self.alice, self.channel, self.guild), None, 5)
        self.assertTrue(self.mem.is_muted(1))
        await mute(fakes.Ctx(self.mod, self.channel, self.guild), self.bob, 10 ** 6)
        self.assertTrue(self.mem.is_muted(2))
        await unmute(fakes.Ctx(self.bob, self.channel, self.guild), None)
        self.assertTrue(self.mem.is_muted(2))  # cannot undo a moderator's mute
        await unmute(fakes.Ctx(self.mod, self.channel, self.guild), self.bob)
        self.assertFalse(self.mem.is_muted(2))
        await unmute(fakes.Ctx(self.alice, self.channel, self.guild), None)
        self.assertFalse(self.mem.is_muted(1))  # ...but you can undo your own
        await mute(fakes.Ctx(self.mod, self.channel, self.guild), fakes.member(999, "Owner"), 5)
        self.assertFalse(self.mem.is_muted(999))

    async def test_poll_escapes_mass_mentions(self):
        ctx = fakes.Ctx(self.alice, self.channel, self.guild)
        await bot.poll_cmd.callback(ctx, question="@everyone pizza or <@&123456789012345678> tacos?")
        posted = ctx.sent[0][1]
        self.assertNotIn("<@&123456789012345678>", posted)
        self.assertNotIn("@everyone", posted.replace("@\u200beveryone", ""))

    async def test_commands_about_others_only_use_this_servers_messages(self):
        self.say("A report.")
        await self.mem.upsert_user(2, "bob#1", "Bob")
        await self.mem.track_channel(500, 50)
        await self.mem.track_channel(600, 60)
        await self.mem.add_message(2, 500, "user", "public thing said in guild fifty")
        await self.mem.add_message(2, 600, "user", "thing said in another server")
        await self.mem.add_message(2, 2, "user", "PRIVATE DM CONFESSION")
        for command in (bot.stalk_cmd, bot.judge_cmd, bot.blackmail_cmd, bot.possess_cmd, bot.duel_cmd):
            self.ai.calls.clear()
            await command.callback(fakes.Ctx(self.alice, self.channel, self.guild), self.bob)
            seen = self.ai.prompts()
            if command is not bot.duel_cmd:
                self.assertIn("guild fifty", seen, command.name)
            self.assertNotIn("PRIVATE DM", seen, command.name)
            self.assertNotIn("another server", seen, command.name)

    async def test_rank_lists_only_people_in_this_server(self):
        self.say("Mediocre.")
        for uid, name, count in ((1, "Alice", 30), (2, "Bob", 20), (77, "Stranger Elsewhere", 999)):
            await self.mem.upsert_user(uid, name.lower(), name)
            async with self.mem._connect() as db:
                await db.execute("UPDATE users SET message_count=? WHERE user_id=?", (count, uid))
                await db.commit()
        ctx = fakes.Ctx(self.alice, self.channel, self.guild)
        await bot.rank_cmd.callback(ctx)
        embed = ctx.sent[-1][2]["embed"]
        self.assertIn("Alice", embed.description)
        self.assertNotIn("Stranger", embed.description)

    async def test_summarize_uses_the_newest_messages(self):
        self.say("A summary.")
        await self.mem.upsert_user(1, "a", "Alice")
        for i in range(20):
            await self.mem.add_message(1, 500, "user" if i % 2 == 0 else "assistant", f"line number {i:02d}")
        await bot.summarize_cmd.callback(fakes.Ctx(self.alice, self.channel, self.guild))
        prompt = self.ai.prompts()
        self.assertIn("Scaramouche: line number 19", prompt)
        self.assertNotIn("line number 04", prompt)

    async def test_reminders_are_delivered_retried_and_never_silently_lost(self):
        self.say("Don't forget, fool.")
        owner = fakes.dm_user(1, "Alice")
        ch = self.channel
        with patch.object(bot.bot, "fetch_user", AsyncMock(return_value=owner)), \
                patch.object(bot.bot, "fetch_channel", AsyncMock(return_value=ch)), \
                patch.object(bot.bot, "get_channel", lambda cid: ch):
            await bot.remind_cmd.callback(fakes.Ctx(self.alice, ch, self.guild), 1, reminder="feed the cat")
            async with self.mem._connect() as db:
                await db.execute("UPDATE reminders SET due_ts=?", (time.time() - 1,))
                await db.commit()
            await bot.reminder_checker.coro()
            self.assertIn("feed the cat", ch.send.await_args.args[0])
            self.assertEqual(await self.mem.get_due_reminders(), [])
            # locked channel -> DM fallback
            await self.mem.add_reminder(1, 500, "second", time.time() - 1)
            ch.send = AsyncMock(side_effect=bot.discord.Forbidden(unittest_response(), "no"))
            await bot.reminder_checker.coro()
            self.assertGreaterEqual(owner.send.await_count, 1)
            self.assertEqual(await self.mem.get_due_reminders(), [])
            # everything fails -> kept for another try
            await self.mem.add_reminder(1, 500, "third", time.time() - 1)
            owner.send = AsyncMock(side_effect=RuntimeError("discord down"))
            await bot.reminder_checker.coro()
            async with self.mem._connect() as db:
                rows = await (await db.execute("SELECT done,attempts FROM reminders WHERE reminder='third'")).fetchall()
            self.assertEqual(tuple(rows[0]), (0, 1))

    async def test_unfiltered_mode_needs_adult_confirmation_and_a_suitable_place(self):
        await self.mem.upsert_user(1, "a", "Alice")
        safe, nsfw = fakes.channel(501, self.guild), fakes.channel(502, self.guild, nsfw=True)
        ctx = fakes.Ctx(self.alice, safe, self.guild)
        await bot.nsfw_cmd.callback(ctx, "on")
        self.assertIsInstance(ctx.sent[-1][2].get("view"), bot.AdultConfirmView)
        self.assertFalse((await self.mem.get_user(1))["nsfw_mode"])
        await self.mem.set_mode(1, "nsfw_mode", True)

        async def system_prompt(channel_obj, is_dm):
            self.ai.calls.clear()
            await bot.get_response(1, channel_obj.id, "hello there friend", await self.mem.get_user(1), "Alice", "<@1>",
                                   channel_obj=channel_obj, is_dm=is_dm)
            return self.ai.calls[0]["messages"][0]["content"]

        self.assertNotIn("Unfiltered Mode", await system_prompt(nsfw, False))   # flag without 18+ confirmation
        await self.mem.set_adult_confirmed(1, True)
        self.assertNotIn("Unfiltered Mode", await system_prompt(safe, False))   # ordinary channel
        self.assertIn("Unfiltered Mode", await system_prompt(nsfw, False))      # age-restricted channel
        self.assertIn("Unfiltered Mode", await system_prompt(fakes.channel(903), True))  # DM
        await bot.nsfw_cmd.callback(fakes.Ctx(self.alice, safe, self.guild), "off")
        self.assertFalse((await self.mem.get_user(1))["nsfw_mode"])

    async def test_weather_reports_failures_clearly(self):
        await self.mem.upsert_user(1, "a", "Alice")
        self.say("Cold.")
        for outcome, expected in ((RuntimeError("down"), "isn't answering"), (None, "means nothing")):
            patched = AsyncMock(side_effect=outcome) if isinstance(outcome, Exception) else AsyncMock(return_value=outcome)
            with patch.object(bot, "_fetch_nws_weather", patched):
                ctx = fakes.Ctx(self.alice, self.channel, self.guild)
                await bot.weather_cmd.callback(ctx, location="Somewhere")
                self.assertIn(expected, ctx.texts()[0])

    async def test_translate_honours_a_target_language(self):
        await bot.translate_cmd.callback(fakes.Ctx(self.alice, self.channel, self.guild), text="to French: good morning everyone")
        self.assertIn("Translate this into French", self.ai.prompts())
        self.ai.calls.clear()
        await bot.translate_cmd.callback(fakes.Ctx(self.alice, self.channel, self.guild), text="good morning everyone")
        self.assertIn("Rewrite this in your voice", self.ai.prompts())

    async def test_roast_battle_cannot_be_hijacked_and_ends(self):
        self.say("Alice lands the cleaner blow this round.")
        await self.mem.upsert_user(1, "a", "Alice")
        await self.mem.upsert_user(2, "b", "Bob")
        await bot.roast_cmd.callback(fakes.Ctx(self.alice, self.channel, self.guild), self.bob)
        carl = fakes.member(4, "Carl")
        self.guild.members.append(carl)
        ctx = fakes.Ctx(carl, self.channel, self.guild)
        await bot.roast_cmd.callback(ctx, self.alice)
        self.assertIn("already underway", ctx.texts()[0])
        for _ in range(5):
            ctx = fakes.Ctx(self.alice, self.channel, self.guild)
            await bot.roast_cmd.callback(ctx, self.bob)
        self.assertIsNone(await self.mem.get_active_roast(500))
        self.assertTrue(all("{'" not in text for text in ctx.texts()))

    async def test_tedtalk_reads_documents_off_the_event_loop(self):
        import docx
        import io
        import pptx
        document = docx.Document()
        document.add_paragraph("Photosynthesis converts light to sugar.")
        buffer = io.BytesIO()
        document.save(buffer)
        text, error = await bot._extract_material(fakes.attachment("n.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"), buffer.getvalue())
        self.assertIn("Photosynthesis", text)
        presentation = pptx.Presentation()
        slide = presentation.slides.add_slide(presentation.slide_layouts[1])
        slide.shapes.title.text, slide.placeholders[1].text = "Title", "Slide body text"
        buffer = io.BytesIO()
        presentation.save(buffer)
        text, error = await bot._extract_material(fakes.attachment("s.pptx", "application/octet-stream"), buffer.getvalue())
        self.assertIn("Slide body text", text)
        text, error = await bot._extract_material(fakes.attachment("old.ppt", "application/vnd.ms-powerpoint"), b"x")
        self.assertEqual(text, "")
        self.assertIn(".pptx", error)
        pdf = (b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
               b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 300 200]/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>endobj\n"
               b"4 0 obj<</Length 44>>stream\nBT /F1 18 Tf 20 100 Td (Hello Teyvat) Tj ET\nendstream endobj\n"
               b"5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\ntrailer<</Root 1 0 R/Size 6>>\n%%EOF")
        text, error = await bot._extract_material(fakes.attachment("x.pdf", "application/pdf"), pdf)
        self.assertIn("Teyvat", text)

    async def test_tedtalk_end_to_end(self):
        self.say("Listen carefully. The mitochondria matter. f(x) = (x+1)^2. Pay attention.")
        sent = fakes.channel(510, self.guild)
        ctx = fakes.Ctx(self.alice, sent, self.guild)
        speak = AsyncMock(return_value=b"MP3")
        with patch.object(bot, "download_capped", AsyncMock(return_value=b"Cells have mitochondria.")), \
                patch.object(bot, "get_audio_with_mood", speak):
            await bot._tedtalk_body(ctx, fakes.attachment("notes.txt", "text/plain", 500), None)
        self.assertTrue(any(kw.get("file") for _, _, kw in ctx.sent))
        self.assertIn("Notes", " ".join(ctx.texts()))
        self.assertIn("mitochondria", bot._tedtalk_cache[1]["material"])
        spoken = " ".join(call.args[0] for call in speak.await_args_list)
        self.assertIn("f(x) = (x+1)^2", spoken)  # maths survives the clean-up before speech

    async def test_help_fits_discords_limits_and_lists_every_command(self):
        ctx = fakes.Ctx(self.alice, self.channel, self.guild)
        await bot.help_cmd(ctx)
        embeds = [kw["embed"] for _, _, kw in ctx.sent]
        self.assertEqual(len(embeds), 3)
        self.assertTrue(all(len(e.fields) <= 25 and len(e) <= 6000 for e in embeds))
        text = " ".join(f.name + " " + f.value for e in embeds for f in e.fields)
        missing = sorted(c.name for c in bot.bot.commands if f"!{c.name}" not in text and c.name not in {"whoami", "scarahelp"})
        self.assertEqual(missing, [])

    # ── background work ──────────────────────────────────────────────────────
    async def test_startup_work_runs_once_and_reconnects_add_nothing(self):
        with patch.object(bot.bot, "wait_until_ready", AsyncMock()):
            try:
                await bot.bot.setup_hook()
                self.assertTrue(all(loop.is_running() for loop in bot.BACKGROUND_LOOPS))
                tracked = len(bot._background_tasks)
                self.assertGreaterEqual(tracked, 3)
                await bot.on_ready()
                await bot.on_ready()
                self.assertEqual(len(bot._background_tasks), tracked)
            finally:
                for loop in bot.BACKGROUND_LOOPS:
                    loop.cancel()
                for task in list(bot._background_tasks):
                    task.cancel()
                await asyncio.sleep(0)

    async def test_closed_dms_switch_dms_off_instead_of_retrying_forever(self):
        self.say("Where did you go.")
        await self.mem.upsert_user(9, "z", "Zed")
        past = time.time() - 5 * 86400
        async with self.mem._connect() as db:
            await db.execute("UPDATE users SET romance_mode=1, allow_dms=1, proactive=1, message_count=50, last_active=?, last_seen=? WHERE user_id=9", (past, past))
            await db.commit()
        zed = fakes.dm_user(9, "Zed")
        zed.send = AsyncMock(side_effect=bot.discord.Forbidden(unittest_response(), "closed"))
        with patch.object(bot.bot, "fetch_user", AsyncMock(return_value=zed)):
            await bot.absence_checker.coro()
            self.assertFalse((await self.mem.get_user(9))["allow_dms"])
            await bot.absence_checker.coro()
            self.assertEqual(zed.send.await_count, 1)
            async with self.mem._connect() as db:
                await db.execute("UPDATE users SET allow_dms=1, proactive=0 WHERE user_id=9")
                await db.commit()
            await bot.absence_checker.coro()
            self.assertEqual(zed.send.await_count, 1)  # !proactive off is respected too

    async def test_duo_autoplay_turns_are_not_attributed_to_anyone(self):
        self.say("A sharp rebuttal.")
        await self.mem.upsert_user(1, "a", "Alice")
        await self.mem.add_message(1, 500, "user", "my private remark")
        async with self.mem._connect() as db:
            await db.execute("UPDATE users SET mood=2, affection=40, romance_mode=1, nsfw_mode=1, adult_confirmed=1 WHERE user_id=1")
            await db.commit()
        before, history_before = await self.mem.get_user(1), await self.mem.get_history(1, 500)
        await self.mem.set_duo_session(500, "argue", "pineapple", "wanderer", awaiting_bot="scaramouche", autoplay_turns=2, autoplay_delay=0)
        async with self.mem._connect(self.mem.shared_db_path) as db:
            await db.execute("UPDATE duo_sessions SET next_autoplay_ts=0")
            await db.commit()
        real_sleep = asyncio.sleep
        sleeps = []

        async def stop_after_first(*args, **kwargs):
            sleeps.append(1)
            if len(sleeps) > 1:
                raise asyncio.CancelledError()

        with patch.object(bot.bot, "wait_until_ready", AsyncMock()), patch.object(bot.bot, "is_closed", lambda: False), \
                patch.object(bot.bot, "get_channel", lambda cid: self.channel), patch.object(bot.asyncio, "sleep", stop_after_first):
            with self.assertRaises(asyncio.CancelledError):
                await bot._duo_autoplay_loop()
        await real_sleep(0)
        after = await self.mem.get_user(1)
        keys = ("mood", "affection", "trust", "message_count", "drift_score")
        self.assertEqual({k: after[k] for k in keys}, {k: before[k] for k in keys})
        self.assertEqual(await self.mem.get_history(1, 500), history_before)
        prompt = self.ai.prompts()
        self.assertNotIn("Unfiltered", prompt)
        self.assertNotIn("my private remark", prompt)
        self.assertEqual(self.channel.send.await_count, 1)

    async def test_unprompted_posts_respect_the_channel_switch(self):
        await self.mem.track_channel(500, 50)
        self.assertIn(500, [c[0] for c in await self.mem.get_active_channels()])
        admin = fakes.member(20, "Admin", perms=fakes.discord.Permissions(manage_channels=True))
        ctx = fakes.Ctx(self.alice, self.channel, self.guild)
        await bot.proactivehere_cmd.callback(ctx, "off")
        self.assertIn("no authority", ctx.texts()[0])
        self.assertTrue(await self.mem.channel_proactive_allowed(500))
        await bot.proactivehere_cmd.callback(fakes.Ctx(admin, self.channel, self.guild), "off")
        self.assertFalse(await self.mem.channel_proactive_allowed(500))
        self.assertNotIn(500, [c[0] for c in await self.mem.get_active_channels()])

    async def test_signature_openers_are_rate_limited_per_user_and_globally(self):
        first = await bot._apply_phrase_policy("Tch. Pathetic effort.", [], user_id=1)
        second = await bot._apply_phrase_policy("Tch. Pathetic effort.", [], user_id=1)
        self.assertTrue(first.startswith("Tch"))
        self.assertFalse(second.startswith("Tch"))
        third = await bot._apply_phrase_policy("Tch. Pathetic effort.", [], user_id=2)
        self.assertFalse(third.startswith("Tch"))
        ready, _ = await self.mem.phrase_status("scaramouche:user:2", "scaramouche:tch", 10800)
        self.assertTrue(ready)  # a replaced phrase does not burn the other user's own cooldown

    async def test_partner_banter(self):
        self.say("You again. How tedious, pretender.")
        wanderer = fakes.member(777, "Wanderer", is_bot=True)
        msg = fakes.message(wanderer, self.channel, "Scaramouche is a pretender and a fool", g=self.guild)
        with patch.object(bot.random, "random", return_value=0.0):
            await bot.on_message(msg)
        self.assertEqual(msg.reply.await_count, 1)
        relation = await self.mem.get_bot_relationship(bot.PARTNER_PAIR_KEY)
        self.assertGreater(relation["last_exchange"], 0)

    async def test_command_errors_are_explained_and_rate_limited(self):
        ctx = fakes.Ctx(self.alice, self.channel, self.guild)
        await bot.on_command_error(ctx, bot.commands.BadArgument("x"))
        self.assertIn("scarahelp", ctx.texts()[0])
        spam = fakes.Ctx(fakes.member(55, "Spammer"), self.channel, self.guild)
        results = []
        for _ in range(12):
            try:
                results.append(await bot._command_rate_limit(spam))
            except bot.RateLimited:
                results.append("limited")
        self.assertEqual(results.count(True), 10)
        self.assertEqual(results.count("limited"), 2)


def unittest_response():
    """A stand-in for the aiohttp response discord.HTTPException wants."""
    from unittest.mock import MagicMock
    response = MagicMock()
    response.status, response.reason = 403, "Forbidden"
    return response


if __name__ == "__main__":
    unittest.main()
