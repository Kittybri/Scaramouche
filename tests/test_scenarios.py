import asyncio

from agent_config import AgentConfig
from internal_state import perceive_message
from self_model import SelfModelStore


def run(coro):
    return asyncio.run(coro)


def test_multi_session_attachment_return_and_denial_scenario(tmp_path):
    config = AgentConfig(contradiction_threshold=3.0)
    store = SelfModelStore(str(tmp_path / "scenario.db"), config)
    run(store.init())

    # Scenario A: several meaningful sessions change modeled behavior gradually.
    for text in ("I missed you", "Thank you for remembering", "Are you okay?"):
        event = perceive_message(text)
        run(store.apply_mood_event(event.summary, event.mood_deltas))
        run(store.record_event(event.event_type, event.summary, importance=event.importance, related_user_id=55))
    assert run(store.get_state())["dimensions"]["attachment"] > 1

    # Scenario B: returning after an absence is important enough for reflection selection.
    returned = perceive_message("I'm back", returned_after_absence=True)
    assert returned.event_type == "user_return" and returned.should_reflect is True

    # Scenario C: repeated initiation undermines a denial without instantly rewriting it.
    belief_id = run(store.add_belief("I do not care whether this user replies.", confidence=.8, scope_user_id=55))
    for _ in range(3):
        run(store.add_belief_evidence(belief_id, "contradict", 1.2, "He initiated contact after noticing the absence."))
    belief = [item for item in run(store.list_beliefs(user_id=55)) if item["id"] == belief_id][0]
    context = run(store.context(55)).prompt_fragment()
    assert belief["confidence"] > .05  # denial weakens rather than being mechanically erased
    assert "Behavior conflicts with belief" in context
