"""Scaramouche owner-only admin and backup command contracts."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_scara_private_server_inventory_requires_configured_owner():
    source = (ROOT / "restored_admin.py").read_text()
    assert "if not owner_id or ctx.author.id!=owner_id:" in source
    assert "await ctx.author.send(page" in source
    assert "await ctx.send(page" not in source


def test_self_model_admin_backup_also_fails_closed():
    bot = (ROOT / "bot.py").read_text()
    assert "def _owner_only(ctx) -> bool:" in bot
    at = bot.index("async def selfbackup_cmd(ctx):")
    assert "if not _owner_only(ctx):" in bot[at:at + 100]
    assert "return is_owner_user(ctx.author.id)" in bot
    assert "return bool(OWNER_ID and int(user_id) == OWNER_ID)" in bot
