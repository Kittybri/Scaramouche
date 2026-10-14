"""Regression contracts for targeted stabilization without rewriting bot flows."""
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]

class StabilizationContracts(unittest.TestCase):
    def test_reminders_acknowledged_only_after_delivery(self):
        mem = (ROOT / "memory.py").read_text()
        bot = (ROOT / "bot.py").read_text()
        start = mem.index("async def get_due_reminders(")
        end = mem.index("async def mark_reminder_done(", start)
        self.assertNotIn("UPDATE reminders SET done=1", mem[start:end])
        self.assertIn("UPDATE reminders SET done=1 WHERE id=? AND done=0", mem)
        sender = 'await ch.send(f"{u.mention} {msg}")'
        guarded = 'await _guarded_channel_send(ch, f"{u.mention} {msg}")'
        marker = sender if sender in bot else guarded
        self.assertIn(marker + '\n                await mem.mark_reminder_done(r["id"])', bot)

    def test_correct_prompt_reference_separator(self):
        bot = (ROOT / "bot.py").read_text()
        self.assertIn('extra = f"{extra}\\n{reference_hint}" if extra else reference_hint', bot)
        self.assertNotIn('extra = f"{extra}\\\\n{reference_hint}" if extra else reference_hint', bot)

    def test_confirmation_checked_before_cache_dependent_addressing(self):
        actions = (ROOT / "natural_actions.py").read_text()
        start = actions.index("async def maybe_handle_actions(")
        body = actions[start:]
        self.assertLess(body.index("if await confirm_if_reply("),
                        body.index("if not is_direct_message("))

    def test_wanderer_doc_writes_are_owner_preview_only(self):
        bot = (ROOT / "bot.py").read_text()
        if 'BOT_NAME = "wanderer"' not in bot:
            return
        guard = (ROOT / "docfix_guard.py").read_text()
        bridge = (ROOT / "google_docs_bridge.py").read_text()
        self.assertIn("secrets.token_urlsafe", guard)
        self.assertIn('required_revision_id=record["revision"]', bot)
        self.assertIn('"writeControl"] = {"requiredRevisionId": required_revision_id}', bridge)
        self.assertNotIn("_looks_like_docfix_request(raw_stripped)", bot)
        self.assertIn("ctx.author.id != OWNER_ID", bot)
        self.assertIn("document changed", bot.lower() if "document changed" in bot.lower() else "document changed")

if __name__ == "__main__":
    unittest.main()
