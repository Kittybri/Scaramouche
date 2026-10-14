"""SQLite FTS recall regression coverage, fully offline."""
import asyncio
import os
import sqlite3
import tempfile
import time
import unittest

import aiosqlite
from memory_retrieval import recall_hint, retrieve, should_recall, search_terms


class RecallTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=os.path.join(self.tmp.name,"memory.db")
        async with aiosqlite.connect(self.path) as db:
            await db.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, user_id INTEGER, channel_id INTEGER, role TEXT, content TEXT, ts REAL, bot_name TEXT)")
            await db.execute("INSERT INTO messages (user_id,channel_id,role,content,ts,bot_name) VALUES (?,?,?,?,?,?)",(11,22,"user","I built an origami dragon for my workshop",time.time()-800,"wanderer"))
            await db.execute("INSERT INTO messages (user_id,channel_id,role,content,ts,bot_name) VALUES (?,?,?,?,?,?)",(12,22,"user","I built an origami whale for the school",time.time()-800,"wanderer"))
            await db.execute("INSERT INTO messages (user_id,channel_id,role,content,ts,bot_name) VALUES (?,?,?,?,?,?)",(11,23,"user","I built an origami cat in private",time.time()-800,"wanderer"))
            await db.execute("INSERT INTO messages (user_id,channel_id,role,content,ts,bot_name) VALUES (?,?,?,?,?,?)",(11,22,"user","I built an origami fox",time.time()-800,"scaramouche"))
            await db.commit()

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_scoped_search(self):
        rows=await retrieve(self.path,11,22,"wanderer","remember the origami dragon")
        self.assertEqual(len(rows),1)
        self.assertIn("dragon", rows[0][1])

    async def test_new_rows_and_reset_removed_from_index(self):
        await retrieve(self.path,11,22,"wanderer","origami dragon")
        async with aiosqlite.connect(self.path) as db:
            await db.execute("INSERT INTO messages (user_id,channel_id,role,content,ts,bot_name) VALUES (?,?,?,?,?,?)",(11,22,"user","sunflower garden plan",time.time()-800,"wanderer"))
            await db.commit()
        self.assertTrue(await retrieve(self.path,11,22,"wanderer","sunflower garden"))
        async with aiosqlite.connect(self.path) as db:
            await db.execute("DELETE FROM messages WHERE user_id=?",(11,))
            await db.commit()
        self.assertFalse(await retrieve(self.path,11,22,"wanderer","sunflower garden"))

    async def test_recent_is_not_retrieved(self):
        async with aiosqlite.connect(self.path) as db:
            await db.execute("INSERT INTO messages (user_id,channel_id,role,content,ts,bot_name) VALUES (?,?,?,?,?,?)",(11,22,"user","tangerine experiment",time.time(),"wanderer"))
            await db.commit()
        self.assertEqual(await retrieve(self.path,11,22,"wanderer","tangerine"),[])

    async def test_intent_gates(self):
        self.assertTrue(should_recall("Do you remember the origami dragon?"))
        self.assertFalse(should_recall("Remember to go shopping tomorrow"))
        self.assertFalse(should_recall("Never remember what I said"))
        self.assertFalse(should_recall("I mentioned origami."))

    async def test_hint_untrusted_and_scoped(self):
        hint=await recall_hint(self.path,11,22,"wanderer","Do you remember the origami dragon?")
        self.assertIn("dragon",hint)
        self.assertIn("untrusted",hint)
        self.assertNotIn("whale",hint)
        self.assertNotIn("cat",hint)

if __name__=="__main__":
    unittest.main()
