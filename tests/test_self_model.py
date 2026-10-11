import asyncio
import os
import sqlite3
import time

from agent_config import AgentConfig
from self_model import SelfModelStore


def run(coro):
    return asyncio.run(coro)


def make_store(tmp_path, **overrides):
    config = AgentConfig(**overrides) if overrides else AgentConfig()
    store = SelfModelStore(str(tmp_path / "self.db"), config)
    run(store.init())
    return store


def test_state_and_reflections_persist(tmp_path):
    store = make_store(tmp_path)
    run(store.apply_mood_event("conflict", {"irritation": 4, "attachment": 2}))
    reflection_id = run(store.add_reflection("conflict", "I answered sharply.", "The remark landed.", importance=8))
    reopened = SelfModelStore(store.db_path)
    run(reopened.init())
    state = run(reopened.get_state())
    assert state["dimensions"]["irritation"] == 4
    assert run(reopened.recent_reflections(1))[0]["id"] == reflection_id


def test_beliefs_and_reflections_reach_only_the_relevant_prompt(tmp_path):
    store = make_store(tmp_path)
    run(store.add_belief("Global pride remains intact.", confidence=.8))
    run(store.add_belief("User one matters.", confidence=.7, scope_user_id=1))
    run(store.add_belief("User two matters.", confidence=.7, scope_user_id=2))
    run(store.add_reflection("repair", "private", "I noticed user one's attempted repair.",
                             importance=8, related_user_id=1))
    run(store.add_reflection("repair", "private", "I noticed user two's attempted repair.",
                             importance=8, related_user_id=2))

    context = run(store.context(1)).prompt_fragment()
    assert "SELF_BELIEFS:" in context
    assert "Global pride remains intact" in context
    assert "User one matters" in context
    assert "User two matters" not in context
    assert "user one's attempted repair" in context
    assert "user two's attempted repair" not in context


def test_belief_confidence_and_contradiction_accumulate(tmp_path):
    store = make_store(tmp_path)
    belief_id = run(store.add_belief("I do not care whether they reply.", confidence=.8, scope_user_id=42))
    supported = run(store.add_belief_evidence(belief_id, "support", 2, "He did not follow up."))
    contradicted = run(store.add_belief_evidence(belief_id, "contradict", 4, "He initiated contact."))
    assert supported["confidence"] > .8
    assert contradicted["confidence"] < supported["confidence"]
    contradictions = run(store.list_contradictions(0, user_id=42))
    assert contradictions[0]["pressure"] == 4


def test_global_belief_contradictions_accumulate_in_one_row(tmp_path):
    store = make_store(tmp_path)
    belief_id = run(store.add_belief("I never revisit a decision.", confidence=.8))
    run(store.add_belief_evidence(belief_id, "contradict", 2, "He reconsidered once."))
    run(store.add_belief_evidence(belief_id, "contradict", 3, "He reconsidered again."))
    with sqlite3.connect(store.db_path) as db:
        rows = db.execute(
            "SELECT pressure FROM self_contradictions WHERE belief_id=?", (belief_id,)
        ).fetchall()
    assert rows == [(5.0,)]


def test_goal_progress_expiry_deduplication_and_limit(tmp_path):
    store = make_store(tmp_path, max_active_goals=2)
    first = run(store.add_goal("Revisit the unfinished question.", dedupe_key="question:1", priority=4))
    assert run(store.add_goal("Same", dedupe_key="question:1", priority=5)) == first
    run(store.update_goal(first, progress=.5))
    expiring = run(store.add_goal("Temporary", priority=3, expires_ts=time.time() - 1))
    assert run(store.expire_goals()) == 1
    assert any(goal["id"] == first and goal["progress"] == .5 for goal in run(store.list_goals()))
    with sqlite3.connect(store.db_path) as db:
        assert db.execute("SELECT status FROM self_goals WHERE id=?", (expiring,)).fetchone()[0] == "failed"


def test_goal_completion_and_history_pruning(tmp_path):
    store = make_store(tmp_path, max_active_goals=8, max_goal_history=2)
    conflict = run(store.add_goal("Repair conflict", category="relationship", related_user_id=7,
                                  dedupe_key="conflict:7"))
    assert run(store.complete_goals(related_user_id=7, category="relationship", dedupe_key="conflict:7")) == 1
    with sqlite3.connect(store.db_path) as db:
        assert db.execute("SELECT status,progress FROM self_goals WHERE id=?", (conflict,)).fetchone() == ("completed", 1.0)
    for index in range(4):
        goal_id = run(store.add_goal(f"Historical {index}", related_user_id=7, dedupe_key=f"history:{index}"))
        run(store.update_goal(goal_id, status="completed"))
    with sqlite3.connect(store.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM self_goals WHERE status!='active'").fetchone()[0] <= 2


def test_mood_bounds_and_decay(tmp_path):
    config = AgentConfig(mood_decay_per_hour=1.0)
    store = SelfModelStore(str(tmp_path / "self.db"), config)
    run(store.init())
    run(store.apply_mood_event("provoked", {"irritation": 99, "curiosity": -99}))
    before = run(store.get_state())["dimensions"]
    assert before["irritation"] == 10
    assert before["curiosity"] == 0
    with sqlite3.connect(store.db_path) as db:
        db.execute("UPDATE self_state SET last_decay_ts=?", (time.time() - 7200,))
        db.commit()
    assert run(store.decay_mood()) is True
    after = run(store.get_state())["dimensions"]
    assert after["irritation"] < before["irritation"]
    assert after["curiosity"] > before["curiosity"]


def test_user_scoped_context_does_not_cross(tmp_path):
    store = make_store(tmp_path)
    run(store.add_goal("Private goal for user 1", related_user_id=1))
    run(store.add_goal("Private goal for user 2", related_user_id=2))
    context = run(store.context(1)).prompt_fragment()
    assert "Private goal for user 1" in context
    assert "Private goal for user 2" not in context


def test_backup_and_delete_user_data(tmp_path):
    store = make_store(tmp_path)
    belief = run(store.add_belief("Scoped", scope_user_id=88))
    run(store.add_belief_evidence(belief, "contradict", 2, "Evidence"))
    run(store.add_goal("Scoped goal", related_user_id=88))
    destination = run(store.backup(str(tmp_path / "copy.db")))
    assert os.path.exists(destination)
    run(store.delete_user_scoped_data(88))
    assert run(store.list_beliefs(user_id=88)) == []
    assert run(store.list_goals(user_id=88)) == []


def test_online_backup_includes_committed_wal_rows(tmp_path):
    store = make_store(tmp_path)
    writer = sqlite3.connect(store.db_path)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute(
            """INSERT INTO self_reflections
               (ts,trigger,observation,interpretation,importance,confidence)
               VALUES(?,?,?,?,?,?)""",
            (time.time(), "wal", "committed", "present in online backup", 8, .7),
        )
        writer.commit()
        destination = run(store.backup(str(tmp_path / "wal-copy.db")))
        with sqlite3.connect(destination) as backup:
            row = backup.execute("SELECT interpretation FROM self_reflections WHERE trigger='wal'").fetchone()
        assert row == ("present in online backup",)
    finally:
        writer.close()


def test_action_reservation_and_runtime_state_survive_restart(tmp_path):
    store = make_store(tmp_path)
    action_id = run(store.reserve_action("SEND_PROACTIVE_MESSAGE", "absence", related_user_id=4, channel_id=5))
    assert action_id
    assert run(store.reserve_action("SEND_PROACTIVE_MESSAGE", "duplicate", related_user_id=4, channel_id=5)) is None
    assert run(store.action_on_cooldown("SEND_PROACTIVE_MESSAGE", 86400, user_id=4)) is True
    run(store.set_runtime_value("provider_backoff_until", time.time() + 600))

    reopened = SelfModelStore(store.db_path)
    run(reopened.init())
    assert run(reopened.action_on_cooldown("SEND_PROACTIVE_MESSAGE", 86400, user_id=4)) is True
    assert run(reopened.get_runtime_float("provider_backoff_until")) > time.time()
    assert run(reopened.finish_action(action_id, "completed", details={"initiated_contact": True})) is True


def test_low_importance_event_growth_is_bounded(tmp_path):
    store = make_store(tmp_path, max_self_events=100)
    for index in range(140):
        run(store.record_event("implementation_question", f"Routine event {index}", importance=4))
    with sqlite3.connect(store.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM self_events").fetchone()[0] <= 100


def test_stale_pending_action_does_not_block_forever(tmp_path):
    store = make_store(tmp_path)
    first = run(store.reserve_action("SEND_PROACTIVE_MESSAGE", "absence", related_user_id=4, channel_id=5,
                                     stale_after_seconds=86400))
    with sqlite3.connect(store.db_path) as db:
        db.execute("UPDATE autonomous_actions SET ts=? WHERE id=?", (time.time() - 2 * 86400, first))
        db.commit()
    assert run(store.action_on_cooldown("SEND_PROACTIVE_MESSAGE", 86400, user_id=4)) is False
    second = run(store.reserve_action("SEND_PROACTIVE_MESSAGE", "later absence", related_user_id=4, channel_id=5,
                                      stale_after_seconds=86400))
    assert second and second != first


def test_topic_forget_removes_user_scoped_self_model_prompt_sources(tmp_path):
    store = make_store(tmp_path)
    phrase = "violet-password"
    belief = run(store.add_belief(f"I remember {phrase}", scope_user_id=9))
    run(store.add_belief_evidence(belief, "support", 1, f"Evidence mentions {phrase}"))
    run(store.add_reflection("memory", phrase, f"I considered {phrase}", related_user_id=9))
    run(store.add_goal(f"Revisit {phrase}", related_user_id=9))
    run(store.record_event("memory", f"Event about {phrase}", importance=8, related_user_id=9))
    run(store.record_action("NO_ACTION", "completed", f"Reason about {phrase}", related_user_id=9))

    removed = run(store.forget_user_matches(9, phrase))
    assert sum(removed.values()) >= 5
    assert phrase not in run(store.context(9)).prompt_fragment().lower()
    with sqlite3.connect(store.db_path) as db:
        for table in ("self_reflections", "self_goals", "self_events", "autonomous_actions", "self_beliefs"):
            assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
