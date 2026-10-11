"""Operator docs must keep OAuth and document tool boundaries clear."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_current_integration_notes_are_distinct_from_old_checkpoints():
    setup = (ROOT / "INTEGRATIONS_SETUP.md").read_text()
    connected = (ROOT / "CONNECTED_ACCOUNTS.md").read_text()
    assert "Oracle-hosted" in setup
    assert "user-owned" in setup
    assert "owner-only" in setup
    assert "Current October 2026 note" in connected
    assert "Google Production" in connected
    assert "Phase 2 remain incomplete" in connected
