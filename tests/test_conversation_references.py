"""Offline regression checks for Phase 1 temporary conversation references."""
import unittest
from types import SimpleNamespace as NS
from conversation_references import (
    remember_direct_message, resolve_followup, conversation_hint,
    reset_sessions, is_direct_message,
)

def msg(content, user=11, channel=22, bot=99, mid=1, guild=True, parent=None, mentions=None):
    return NS(id=mid,content=content,author=NS(id=user,bot=False),
              channel=NS(id=channel),guild=object() if guild else None,
              mentions=[NS(id=x) for x in (mentions if mentions is not None else [bot])],
              reference=NS(resolved=parent) if parent else None)

class ReferenceTests(unittest.TestCase):
    def setUp(self):
        reset_sessions()

    def test_second_source_and_comparison(self):
        a=msg("Wanderer, read https://example.org/one",mid=1)
        b=msg("Wanderer, read https://example.org/two",mid=2)
        remember_direct_message(a,"wanderer",99)
        remember_direct_message(b,"wanderer",99)
        self.assertEqual(resolve_followup(msg("Explain the second one",mid=3),"wanderer",99),("web","https://example.org/two"))
        self.assertEqual(len(resolve_followup(msg("Compare those two",mid=4),"wanderer",99)[1]),2)

    def test_user_and_channel_isolation(self):
        remember_direct_message(msg("Look at https://example.org/a"),"wanderer",99)
        self.assertIsNone(resolve_followup(msg("Explain that",user=12),"wanderer",99))
        self.assertIsNone(resolve_followup(msg("Explain that",channel=23),"wanderer",99))
        self.assertIsNone(resolve_followup(msg("Explain that"),"scaramouche",99))

    def test_ambient_and_bot_identity(self):
        ambient=msg("Wanderer was talking about https://example.org/x",mentions=[])
        self.assertFalse(is_direct_message(ambient,"wanderer",99))
        remember_direct_message(ambient,"wanderer",99)
        self.assertIsNone(resolve_followup(msg("Read that"),"wanderer",99))

    def test_explicit_no_browsing(self):
        remember_direct_message(msg("Scara, look at https://example.org/a"),"scaramouche",99)
        self.assertIsNone(resolve_followup(msg("Don't read that"),"scaramouche",99))

    def test_reply_context(self):
        parent=NS(author=NS(id=99),content="Earlier example: test",id=20)
        hint=conversation_hint(msg("What did you mean?",parent=parent),"wanderer",99)
        self.assertIn("Earlier example",hint)

    def test_not_retrieve_untrusted_private_addresses(self):
        remember_direct_message(msg("Look at https://127.0.0.1/secrets"),"wanderer",99)
        self.assertIsNone(resolve_followup(msg("Explain that"),"wanderer",99))

    def test_expiration(self):
        from conversation_references import _sessions, TTL
        remember_direct_message(msg("Read https://example.org/a"),"wanderer",99)
        key=next(iter(_sessions))
        _sessions[key].touched-=TTL+1
        self.assertIsNone(resolve_followup(msg("Explain it"),"wanderer",99))

if __name__=="__main__":
    unittest.main()
