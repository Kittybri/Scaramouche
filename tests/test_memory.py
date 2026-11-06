"""Offline checks for memory.py on a temporary database: migrations, privacy scoping, reset, reminders."""

import asyncio
import importlib.util
import os
import sqlite3
import tempfile
import time
import unittest
from contextlib import contextmanager

HAVE_AIOSQLITE = importlib.util.find_spec("aiosqlite") is not None


@contextmanager
def sql(path):
    """sqlite3 connection that is committed and really closed (``with sqlite3.connect`` does not close)."""
    conn = sqlite3.connect(path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


@unittest.skipUnless(HAVE_AIOSQLITE, "aiosqlite is not installed")
class MemoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import memory

        self.memory = memory
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.mem = memory.Memory("scaramouche", data_dir=self.tmp.name)
        await self.mem.init()
        memory.Memory._muted.clear()
        memory.Memory._muted_by.clear()

    async def test_init_is_idempotent_and_get_user_is_read_only(self):
        await self.mem.init()
        self.assertIsNone(await self.mem.get_user(9))
        await self.mem.upsert_user(1, "a#1", "Alice")
        await self.mem.get_user(1)
        with sql(self.mem.db_path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM user_preferences").fetchone()[0], 0)

    async def test_two_instances_do_not_share_a_database_path(self):
        other = self.memory.Memory("wanderer", data_dir=self.tmp.name)
        self.assertNotEqual(other.db_path, self.mem.db_path)
        self.assertEqual(other.shared_db_path, self.mem.shared_db_path)

    async def test_old_schema_is_migrated_without_losing_data(self):
        path = os.path.join(self.tmp.name, "old.db")
        with sql(path) as db:
            db.executescript(
                "CREATE TABLE users (user_id INTEGER PRIMARY KEY, username TEXT, display_name TEXT);"
                "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, channel_id INTEGER, role TEXT, content TEXT, ts REAL);"
                "CREATE TABLE reminders (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, channel_id INTEGER, reminder TEXT, due_ts REAL, done INTEGER DEFAULT 0);"
                "INSERT INTO users VALUES (1,'u','U'); INSERT INTO messages (user_id,channel_id,role,content,ts) VALUES (1,5,'user','old message',1);"
                "INSERT INTO reminders (user_id,channel_id,reminder,due_ts) VALUES (1,5,'old reminder',1);")
        old_dir = tempfile.mkdtemp(dir=self.tmp.name)
        os.replace(path, os.path.join(old_dir, "scaramouche.db"))
        mem = self.memory.Memory("scaramouche", data_dir=old_dir)
        await mem.init()
        user = await mem.get_user(1)
        self.assertFalse(user["adult_confirmed"])
        self.assertEqual((await mem.get_history(1, 5))[0]["content"], "old message")
        self.assertEqual((await mem.get_due_reminders())[0]["reminder"], "old reminder")

    async def test_topics_and_summaries_keep_their_dm_origin_apart(self):
        await self.mem.upsert_user(1, "a", "A")
        await self.mem.record_topic(1, "health", private=True)
        await self.mem.record_topic(1, "games")
        await self.mem.record_topic(1, "games", private=True)
        public = {t["topic"]: t["count"] for t in await self.mem.get_top_topics(1, 5, public_only=True)}
        everything = {t["topic"]: t["count"] for t in await self.mem.get_top_topics(1, 5)}
        self.assertEqual(public, {"games": 1})
        self.assertEqual(everything, {"health": 1, "games": 2})
        await self.mem.save_summary(1, "public summary")
        await self.mem.save_summary(1, "dm summary", private=True)
        user = await self.mem.get_user(1)
        self.assertEqual((user["memory_summary"], user["memory_summary_dm"]), ("public summary", "dm summary"))
        await self.mem.reset_user(1)
        user = await self.mem.get_user(1)
        self.assertEqual((user["memory_summary"], user["memory_summary_dm"]), (None, None))
        self.assertEqual(await self.mem.get_top_topics(1, 5), [])

    async def test_summaries_and_topics_from_before_the_split_count_as_private(self):
        old_dir = tempfile.mkdtemp(dir=self.tmp.name)
        with sql(os.path.join(old_dir, "scaramouche.db")) as db:
            db.executescript(
                "CREATE TABLE users (user_id INTEGER PRIMARY KEY, username TEXT, display_name TEXT, "
                "memory_summary TEXT, summary_msg_count INTEGER, message_count INTEGER);"
                "INSERT INTO users VALUES (1,'u','U','a summary nobody can place',400,520);"
                "CREATE TABLE user_topics (user_id INTEGER, topic TEXT, count INTEGER DEFAULT 0, last_seen REAL DEFAULT 0, PRIMARY KEY (user_id, topic));"
                "INSERT INTO user_topics VALUES (1,'health',4,1);")
        mem = self.memory.Memory("scaramouche", data_dir=old_dir)
        await mem.init()
        user = await mem.get_user(1)
        self.assertIsNone(user["memory_summary"])  # nothing unproven reaches a server channel
        self.assertEqual(user["memory_summary_dm"], "a summary nobody can place")
        self.assertTrue(await mem.needs_summary(1))  # and a proper (public) one gets built
        self.assertEqual(await mem.get_top_topics(1, 3, public_only=True), [])
        self.assertEqual([t["topic"] for t in await mem.get_top_topics(1, 3)], ["health"])
        await mem.save_summary(1, "fresh public summary")
        await mem.init()  # the migration runs once: a second start keeps what was written since
        user = await mem.get_user(1)
        self.assertEqual((user["memory_summary"], user["memory_summary_dm"]), ("fresh public summary", "a summary nobody can place"))

    async def test_simultaneous_first_messages_do_not_collide(self):
        # four handlers racing to register the same brand-new user used to raise IntegrityError in most of them
        for uid in range(1000, 1030):
            results = await asyncio.gather(*[self.mem.upsert_user(uid, f"u{uid}", f"name {i}") for i in range(4)], return_exceptions=True)
            self.assertEqual([r for r in results if isinstance(r, Exception)], [])
            self.assertEqual((await self.mem.get_user(uid))["username"], f"u{uid}")
        with sql(self.mem.db_path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM users WHERE user_id>=1000").fetchone()[0], 30)
        first = (await self.mem.get_user(1000))["first_seen"]
        await self.mem.upsert_user(1000, "renamed", "Renamed")
        user = await self.mem.get_user(1000)
        self.assertEqual((user["username"], user["first_seen"]), ("renamed", first))  # first contact is kept

    async def test_simultaneous_trivia_answers_all_count(self):
        results = await asyncio.gather(*[self.mem.update_trivia(5, i % 2 == 0) for i in range(8)], return_exceptions=True)
        self.assertEqual([r for r in results if isinstance(r, Exception)], [])
        with sql(self.mem.db_path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*), SUM(correct), SUM(wrong) FROM trivia WHERE user_id=5").fetchone(), (1, 4, 4))
        self.assertEqual({k: v for k, v in (await self.mem.get_trivia_stats(5)).items() if k in ("correct", "wrong")}, {"correct": 4, "wrong": 4})

    async def test_duplicate_trivia_rows_from_older_versions_are_merged_on_start(self):
        old_dir = tempfile.mkdtemp(dir=self.tmp.name)
        with sql(os.path.join(old_dir, "scaramouche.db")) as db:
            db.executescript(
                "CREATE TABLE trivia (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, correct INTEGER DEFAULT 0, wrong INTEGER DEFAULT 0);"
                "INSERT INTO trivia (user_id,correct,wrong) VALUES (1,2,1),(1,3,0),(1,0,4),(2,5,5);")
        mem = self.memory.Memory("scaramouche", data_dir=old_dir)
        await mem.init()
        with sql(mem.db_path) as db:
            rows = db.execute("SELECT user_id, correct, wrong FROM trivia ORDER BY user_id").fetchall()
        self.assertEqual(rows, [(1, 5, 5), (2, 5, 5)])
        await mem.update_trivia(1, True)
        await mem.init()  # idempotent
        with sql(mem.db_path) as db:
            self.assertEqual(db.execute("SELECT correct, wrong FROM trivia WHERE user_id=1").fetchone(), (6, 5))

    async def test_dm_content_is_kept_out_of_public_queries(self):
        await self.mem.track_channel(100, 7)
        await self.mem.track_channel(200, 8)
        await self.mem.add_message(1, 100, "user", "said in guild seven")
        await self.mem.add_message(1, 200, "user", "said in guild eight")
        await self.mem.add_message(1, 1, "user", "private dm text")
        self.assertEqual(set(await self.mem.get_recent_messages(1, 10)), {"said in guild seven", "said in guild eight", "private dm text"})
        self.assertEqual(set(await self.mem.get_recent_messages(1, 10, public_only=True)), {"said in guild seven", "said in guild eight"})
        self.assertEqual(await self.mem.get_recent_messages(1, 10, guild_id=7), ["said in guild seven"])
        with sql(self.mem.db_path) as db:
            db.execute("UPDATE messages SET ts=ts-999999")
        seen = {await self.mem.get_random_old_message(1, public_only=True) for _ in range(25)}
        self.assertNotIn("private dm text", seen)

    async def test_private_flags_hide_dm_memories_from_public_use(self):
        await self.mem.upsert_user(1, "a", "A")
        await self.mem.add_memory_event(1, "confession", "secret from a dm", 5, private=True)
        await self.mem.add_memory_event(1, "promise", "said in public", 3)
        public = [e["memory"] for e in await self.mem.get_memory_bank_entries(1, include_private=False)]
        self.assertEqual(public, ["said in public"])
        picks = {(await self.mem.get_weighted_memory_event(1, include_private=False))["memory"] for _ in range(15)}
        self.assertEqual(picks, {"said in public"})
        await self.mem.add_memory_event(1, "confession", "secret from a dm", 5, private=False)  # later said openly
        self.assertEqual(len(await self.mem.get_memory_bank_entries(1, include_private=False)), 2)

        await self.mem.set_callback_memory(1, "cb", private=True)
        await self.mem.update_last_statement(1, "st", private=True)
        await self.mem.open_conflict(1, "co", private=True)
        user = await self.mem.get_user(1)
        self.assertEqual((user["callback_private"], user["last_statement_private"], user["conflict_private"]), (True, True, True))
        await self.mem.set_callback_memory(1, "public cb")
        user = await self.mem.get_user(1)
        self.assertEqual((user["callback_private"], user["last_statement_private"], user["conflict_private"]), (False, True, True))
        await self.mem.resolve_conflict(1)
        self.assertFalse((await self.mem.get_user(1))["conflict_private"])

        await self.mem.add_inside_joke(1, "dm joke", private=True)
        await self.mem.add_inside_joke(1, "public joke")
        await self.mem.add_shared_inside_joke(1, "shared dm joke", private=True)
        self.assertEqual(await self.mem.list_inside_jokes(1, include_private=False), ["public joke"])
        self.assertIsNone(await self.mem.get_random_shared_inside_joke(1, include_private=False))

    async def test_reset_removes_everything_remembered_but_only_for_that_user(self):
        for uid in (5, 50):
            await self.mem.upsert_user(uid, f"u{uid}", f"User{uid}")
            await self.mem.add_message(uid, uid, "user", "dm")
            await self.mem.add_reminder(uid, uid, "r", time.time() + 999)
            await self.mem.update_trivia(uid, True)
            await self.mem.set_dm_sent(uid)
            await self.mem.set_hostage(uid, "d")
            await self.mem.consume_phrase(f"scaramouche:user:{uid}", "k", 100)
            await self.mem.add_milestone(f"scaramouche:user:{uid}", "m", "n")
        await self.mem.reset_user(5)
        with sql(self.mem.db_path) as db:
            for table in ("messages", "reminders", "trivia", "dm_cooldown", "hostages"):
                self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {table} WHERE user_id=5").fetchone()[0], 0, table)
                self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {table} WHERE user_id=50").fetchone()[0], 1, table)
            self.assertEqual([r[0] for r in db.execute("SELECT scope FROM phrase_cooldowns")], ["scaramouche:user:50"])
            self.assertEqual([r[0] for r in db.execute("SELECT scope FROM relationship_milestones")], ["scaramouche:user:50"])

    async def test_reminders_survive_failed_delivery(self):
        await self.mem.add_reminder(1, 100, "r1", time.time() - 1)
        await self.mem.add_reminder(2, 200, "r2", time.time() - 1, dm=True)
        due = await self.mem.get_due_reminders()
        self.assertEqual([(r["reminder"], r["dm"]) for r in due], [("r1", False), ("r2", True)])
        self.assertEqual(len(await self.mem.get_due_reminders()), 2)  # fetching does not consume them
        await self.mem.complete_reminder(due[0]["id"])
        gave_up = [await self.mem.retry_reminder_later(due[1]["id"], delay_seconds=0, max_attempts=3) for _ in range(3)]
        self.assertEqual(gave_up, [False, False, True])
        self.assertEqual(await self.mem.get_due_reminders(), [])

    async def test_forget_treats_wildcards_literally(self):
        await self.mem.add_memory_event(7, "promise", "alpha beta", 3)
        await self.mem.add_memory_event(7, "fight", "gamma 100% sure", 3)
        self.assertEqual((await self.mem.forget_memory_matches(7, "%"))["memories"], 1)
        self.assertEqual((await self.mem.forget_memory_matches(7, "_"))["memories"], 0)

    async def test_deltas_are_clamped(self):
        await self.mem.upsert_user(1, "a", "A")
        await self.mem.apply_deltas(1, mood=50, affection=500, trust=-500, drift=7)
        user = await self.mem.get_user(1)
        self.assertEqual((user["mood"], user["affection"], user["trust"], user["drift_score"]), (10, 100, 0, 7))

    async def test_active_channels_and_greeting_day(self):
        await self.mem.track_channel(300, 9)
        self.assertIn(300, [c[0] for c in await self.mem.get_active_channels()])
        await self.mem.upsert_user(3, "c", "C")
        self.assertTrue(await self.mem.should_greet(3, 20261105))
        await self.mem.mark_greeted(3, 20261105)
        self.assertFalse(await self.mem.should_greet(3, 20261105))
        self.assertTrue(await self.mem.should_greet(3, 20261106))

    async def test_dm_eligibility_needs_proactive_and_adult_confirmation_for_nsfw(self):
        await self.mem.upsert_user(3, "c", "C")
        await self.mem.add_message(3, 3, "user", "hi")
        await self.mem.set_mode(3, "nsfw_mode", True)
        self.assertEqual([(u["user_id"], u["nsfw_mode"]) for u in await self.mem.get_dm_eligible_users()], [(3, False)])
        await self.mem.set_adult_confirmed(3, True)
        self.assertEqual([(u["user_id"], u["nsfw_mode"]) for u in await self.mem.get_dm_eligible_users()], [(3, True)])
        await self.mem.set_mode(3, "proactive", False)
        self.assertEqual(await self.mem.get_dm_eligible_users(), [])

    async def test_history_respects_the_size_budget(self):
        for i in range(50):
            await self.mem.add_message(1, 9, "user", f"{i:02d} " + "x" * 100)
        trimmed = await self.mem.get_history(1, 9, limit=50, max_chars=1000)
        self.assertLess(sum(len(m["content"]) for m in trimmed), 1300)
        self.assertTrue(trimmed[-1]["content"].startswith("49"))  # newest kept, oldest dropped

    async def test_mute_remembers_who_muted(self):
        self.mem.mute_user(1, 60, by=2)
        self.assertTrue(self.mem.is_muted(1))
        self.assertEqual(self.mem.muted_by(1), 2)
        self.mem.unmute_user(1)
        self.assertFalse(self.mem.is_muted(1))

    async def test_pruning_never_deletes_conversation_history(self):
        await self.mem.add_message(1, 1, "user", "old")
        with sql(self.mem.db_path) as db:
            db.execute("UPDATE messages SET ts=ts-100*86400")
        await self.mem.prune_old_data()
        self.assertEqual(len(await self.mem.get_history(1, 1)), 1)


if __name__ == "__main__":
    unittest.main()
