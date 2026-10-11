import asyncio
import sqlite3
import time

from agent_config import AgentConfig
from anti_repeat import build_prompt_guard, looks_repetitive, repeated_shape, response_shape
from character_identity import IMPLEMENTATION_AWARENESS, attachment_guard, implementation_answer_hint
from memory import Memory
from provider_config import resolve_groq_model


def run(coro):
    return asyncio.run(coro)


def test_memory_paths_are_instance_local_and_existing_schema_survives(tmp_path):
    old_path = tmp_path / "old.db"
    with sqlite3.connect(old_path) as db:
        db.execute("CREATE TABLE users(user_id INTEGER PRIMARY KEY, username TEXT, display_name TEXT)")
        db.execute("CREATE TABLE messages(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER,channel_id INTEGER,role TEXT,content TEXT,ts REAL)")
        db.execute(
            """CREATE TABLE scene_state(
                   channel_id INTEGER PRIMARY KEY,location TEXT,situation TEXT,last_beat TEXT,
                   emotional_temp TEXT,objective TEXT,present TEXT,updated_ts REAL
               )"""
        )
        db.execute("INSERT INTO users(user_id,username,display_name) VALUES(1,'old','Old User')")
        db.execute("INSERT INTO messages(user_id,channel_id,role,content,ts) VALUES(1,2,'user','legacy',1)")
    first = Memory("one", db_path=str(old_path), shared_db_path=str(tmp_path / "shared1.db"))
    second = Memory("two", db_path=str(tmp_path / "two.db"), shared_db_path=str(tmp_path / "shared2.db"))
    run(first.init())
    run(second.init())
    run(second.upsert_user(2, "new", "New User"))
    assert run(first.get_user(1))["display_name"] == "Old User"
    assert run(first.get_user(2)) is None
    assert run(second.get_user(2))["display_name"] == "New User"
    run(first.init())  # migration rerun is idempotent
    with sqlite3.connect(old_path) as db:
        user_columns = {row[1] for row in db.execute("PRAGMA table_info(users)")}
        message_columns = {row[1] for row in db.execute("PRAGMA table_info(messages)")}
    assert {"last_active", "repair_count", "timezone_name"} <= user_columns
    assert "bot_name" in message_columns


def test_memory_backup(tmp_path):
    memory = Memory("test", db_path=str(tmp_path / "memory.db"), shared_db_path=str(tmp_path / "shared.db"))
    run(memory.init())
    destination = run(memory.backup(str(tmp_path / "memory-copy.db")))
    with sqlite3.connect(destination) as db:
        assert db.execute("PRAGMA quick_check").fetchone()[0] == "ok"


def test_mute_persists_restart_and_is_instance_scoped(tmp_path):
    path = str(tmp_path / "memory.db")
    shared = str(tmp_path / "shared.db")
    memory = Memory("test", db_path=path, shared_db_path=shared)
    run(memory.init())
    run(memory.mute_user(55, 600))
    assert run(memory.is_muted(55)) is True

    reopened = Memory("test", db_path=path, shared_db_path=shared)
    run(reopened.init())
    assert run(reopened.is_muted(55)) is True

    isolated = Memory("other", db_path=str(tmp_path / "other.db"), shared_db_path=str(tmp_path / "other-shared.db"))
    run(isolated.init())
    assert run(isolated.is_muted(55)) is False
    run(reopened.unmute_user(55))
    assert run(reopened.is_muted(55)) is False


def test_memory_backup_includes_wal_and_history_is_bounded(tmp_path):
    memory = Memory("test", db_path=str(tmp_path / "memory.db"), shared_db_path=str(tmp_path / "shared.db"))
    run(memory.init())
    run(memory.upsert_user(1, "one", "One"))
    for index in range(10):
        run(memory.add_message(1, 9, "user", f"{index}:" + "x" * 200))
    history = run(memory.get_history(1, 9, limit=10, max_chars_per_message=40, max_total_chars=120))
    assert len(history) == 3
    assert sum(len(item["content"]) for item in history) <= 120
    assert history[-1]["content"].startswith("9:")

    writer = sqlite3.connect(memory.db_path)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute(
            "INSERT INTO messages(user_id,channel_id,role,content,ts,bot_name) VALUES(?,?,?,?,?,?)",
            (1, 9, "assistant", "wal-visible", time.time(), "test"),
        )
        writer.commit()
        destination = run(memory.backup(str(tmp_path / "wal-memory-copy.db")))
        with sqlite3.connect(destination) as backup:
            assert backup.execute("SELECT COUNT(*) FROM messages WHERE content='wal-visible'").fetchone()[0] == 1
    finally:
        writer.close()


def test_upsert_returns_previous_activity_and_public_proactive_ignores_dm_opt_out(tmp_path):
    memory = Memory("test", db_path=str(tmp_path / "memory.db"), shared_db_path=str(tmp_path / "shared.db"))
    run(memory.init())
    assert run(memory.upsert_user(7, "seven", "Seven")) == 0
    old_activity = time.time() - 30 * 86400
    with sqlite3.connect(memory.db_path) as db:
        db.execute(
            "UPDATE users SET last_active=?,last_seen=?,message_count=20,affection=50,trust=40,proactive=1,allow_dms=0,callback_memory='private DM detail' WHERE user_id=7",
            (old_activity, old_activity),
        )
        db.execute("INSERT INTO channels(channel_id,guild_id,last_active) VALUES(99,123,?)", (old_activity,))
        db.execute(
            "INSERT INTO messages(user_id,channel_id,role,content,ts,bot_name) VALUES(?,?,?,?,?,?)",
            (7, 99, "user", "old message", old_activity, "test"),
        )
        db.execute(
            "INSERT INTO messages(user_id,channel_id,role,content,ts,bot_name) VALUES(?,?,?,?,?,?)",
            (7, 7, "user", "private DM detail", old_activity + 10, "test"),
        )
        db.commit()
    previous = run(memory.upsert_user(7, "seven", "Seven"))
    assert abs(previous - old_activity) < 1
    # Restore absence after verifying the atomic previous-timestamp return.
    with sqlite3.connect(memory.db_path) as db:
        db.execute("UPDATE users SET last_active=?,allow_dms=0 WHERE user_id=7", (old_activity,))
        db.commit()
    candidates = run(memory.get_proactive_candidates(absent_before=time.time() - 3 * 86400))
    assert candidates[0]["user_id"] == 7
    assert candidates[0]["channel_id"] == 99
    assert candidates[0]["context"] == "old message"


def test_forget_removes_matching_future_prompt_sources_and_full_reset(tmp_path):
    memory = Memory("test", db_path=str(tmp_path / "memory.db"), shared_db_path=str(tmp_path / "shared.db"))
    run(memory.init())
    run(memory.upsert_user(8, "eight", "Eight"))
    secret = "violet-password"
    with sqlite3.connect(memory.db_path) as db:
        db.execute(
            "UPDATE users SET callback_memory=?,memory_summary=?,last_statement=?,conflict_summary=?,conflict_open=1 WHERE user_id=8",
            (secret, secret, secret, secret),
        )
        db.execute(
            "INSERT INTO messages(user_id,channel_id,role,content,ts,bot_name) VALUES(?,?,?,?,?,?)",
            (8, 80, "user", f"remember {secret}", time.time(), "test"),
        )
        db.execute("INSERT INTO user_topics(user_id,topic,count,last_seen) VALUES(8,?,1,?)", (secret, time.time()))
        db.execute("INSERT INTO memory_bank(user_id,kind,memory,weight,ts) VALUES(8,'manual',?,5,?)", (secret, time.time()))
        db.execute("INSERT INTO reminders(user_id,channel_id,reminder,due_ts) VALUES(8,80,?,?)", (secret, time.time()))
        db.execute("INSERT INTO user_preferences(user_id,voice_enabled) VALUES(8,0)")
        db.commit()

    removed = run(memory.forget_memory_matches(8, secret))
    assert sum(removed.values()) >= 5
    assert run(memory.get_history(8, 80)) == []
    user = run(memory.get_user(8))
    assert user["callback_memory"] is None
    assert user["memory_summary"] is None
    assert user["last_statement"] is None
    assert user["conflict_summary"] is None
    assert user["conflict_open"] is False

    run(memory.reset_user(8))
    assert run(memory.get_user(8)) is None
    with sqlite3.connect(memory.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM user_preferences WHERE user_id=8").fetchone()[0] == 0
    with sqlite3.connect(memory.shared_db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM shared_users WHERE user_id=8").fetchone()[0] == 0


def test_repeated_opening_and_structural_pattern_detected():
    recent = ["Fine. That was predictable.", "Right. That was obvious.", "Enough. That was tedious."]
    assert response_shape(recent[0])["sentence_count"] == 2
    assert repeated_shape("Good. That was expected.", recent)
    assert "structural shape" in build_prompt_guard("scaramouche", recent)
    assert looks_repetitive("Distinct. The factual answer is precise.", recent, include_shape=False) is False
    private_recent = ["Kitty's private detail should stay private. One.",
                      "Kitty's private detail should stay private. Two."]
    assert "Kitty's private detail" not in build_prompt_guard("scaramouche", private_recent)


def test_character_prompt_invariants():
    assert "persistent character identity" in IMPLEMENTATION_AWARENESS
    assert "do not prove consciousness" in IMPLEMENTATION_AWARENESS
    assert implementation_answer_hint("How does your Python code work?")
    assert implementation_answer_hint("Are you actually an AI Discord bot?")
    assert implementation_answer_hint("Are you conscious?")
    assert implementation_answer_hint("Where are your memories stored?")
    assert implementation_answer_hint("Can you access your own source code?")
    assert implementation_answer_hint("What are you eating?") == ""
    high_attachment = attachment_guard(8, 90)
    assert "Do not become a generic sweet assistant" in high_attachment
    assert "pride remains" in high_attachment


def test_retired_provider_models_resolve_explicitly():
    assert resolve_groq_model("llama-3.3-70b-versatile") == "openai/gpt-oss-120b"
    assert resolve_groq_model("llama-3.2-90b-vision-preview") == "qwen/qwen3.8-27b"


def test_cost_sensitive_environment_overrides_are_hard_bounded(monkeypatch):
    monkeypatch.setenv("SELF_HISTORY_MESSAGES", "999999")
    monkeypatch.setenv("SELF_HISTORY_TOTAL_CHARS", "999999")
    monkeypatch.setenv("SELF_CHANNEL_CONTEXT_MESSAGES", "999999")
    monkeypatch.setenv("SELF_RESPONSE_ATTEMPTS", "999999")
    monkeypatch.setenv("GROQ_TIMEOUT_SECONDS", "999999")
    monkeypatch.setenv("GROQ_REASONING_EFFORT", "unbounded")
    config = AgentConfig.from_env()
    assert config.conversation_history_limit == 100
    assert config.history_total_chars == 30_000
    assert config.channel_context_limit == 50
    assert config.response_attempts == 3
    assert config.provider_timeout_seconds == 120
    assert config.provider_reasoning_effort == "low"
