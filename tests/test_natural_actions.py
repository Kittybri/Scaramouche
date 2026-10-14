"""Offline Phase 3 intent and confirmation tests."""
import asyncio
import time
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
import natural_actions as na


def msg(text,user=10,channel=20,mid=30,parent=0,guild=False):
    result=NS(id=mid,content=text,author=NS(id=user,bot=False),
              channel=NS(id=channel),guild=object() if guild else None,
              mentions=[NS(id=99)],reference=NS(message_id=parent))
    result.reply=AsyncMock(return_value=NS(id=41))
    return result


class ActionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        na._pending.clear()

    async def test_parse_reminder(self):
        self.assertEqual(na.parse_reminder("Wanderer, remind me to call home in 2 hours"),("call home",7200))
        self.assertEqual(na.parse_reminder("Scara, remind me in 5 minutes to drink water"),("drink water",300))
        self.assertIsNone(na.parse_reminder("remind me in 0 hours to run"))
        self.assertIsNone(na.parse_reminder("remind me in 8 days to run"))

    async def test_confirmation_requires_exact_reply_and_author(self):
        m=NS(add_reminder=AsyncMock())
        start=msg("remind me to stretch in 5 minutes")
        self.assertTrue(await na.maybe_handle_actions(start,"wanderer",99,m,AsyncMock()))
        self.assertEqual(m.add_reminder.await_count,0)
        self.assertFalse(await na.confirm_if_reply(msg("confirm",parent=99),"wanderer",m))
        self.assertFalse(await na.confirm_if_reply(msg("confirm",user=11,parent=41),"wanderer",m))
        self.assertFalse(await na.confirm_if_reply(msg("confirm",channel=21,parent=41),"wanderer",m))
        ok=msg("confirm",parent=41)
        self.assertTrue(await na.confirm_if_reply(ok,"wanderer",m))
        self.assertEqual(m.add_reminder.await_count,1)
        self.assertFalse(await na.confirm_if_reply(ok,"wanderer",m))

    async def test_expired_request(self):
        m=NS(add_reminder=AsyncMock())
        await na.maybe_handle_actions(msg("remind me to stretch in 5 minutes"),"wanderer",99,m,AsyncMock())
        key=next(iter(na._pending))
        na._pending[key]["expires"]=time.monotonic()-1
        self.assertFalse(await na.confirm_if_reply(msg("confirm",parent=41),"wanderer",m))
        self.assertEqual(m.add_reminder.await_count,0)

    async def test_owner_doc_not_read_in_guild_or_for_other_user(self):
        reader=AsyncMock()
        summary=AsyncMock(return_value="Summary")
        link="https://docs.google.com/document/d/abc1234567890/edit"
        outsider=msg("summarize "+link,user=11)
        self.assertTrue(await na.maybe_handle_actions(outsider,"wanderer",99,NS(),summary,owner_id=10,google_doc_reader=reader))
        reader.assert_not_awaited()
        guildmsg=msg("summarize "+link,guild=True)
        self.assertTrue(await na.maybe_handle_actions(guildmsg,"wanderer",99,NS(),summary,owner_id=10,google_doc_reader=reader))
        reader.assert_not_awaited()

    async def test_plan_is_draft(self):
        summarize=AsyncMock(return_value="1. Sketch\n2. Test")
        m=msg("Wanderer, make a plan for finishing my homework")
        self.assertTrue(await na.maybe_handle_actions(m,"wanderer",99,NS(),summarize))
        self.assertIn("Draft plan only",m.reply.call_args.args[0])


if __name__=="__main__":
    unittest.main()
