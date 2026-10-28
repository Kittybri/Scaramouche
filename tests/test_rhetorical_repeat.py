from anti_repeat import (
    PatternHistory,
    PatternScopeSamples,
    analyze_repetition,
    build_pattern_scopes,
    build_prompt_guard,
    merge_recent_messages,
    response_signature,
    rhetorical_pattern,
    suppress_repeated_name_opener,
)


def test_same_bit_different_words_classifies_as_fake_praise():
    assert rhetorical_pattern("Astonishing. You failed again.") == "fake_praise"
    assert rhetorical_pattern("Impressive. Another failure.") == "fake_praise"


def test_pattern_frequency_rejects_fourth_use_not_first_use():
    draft = "Brilliant. You broke it again."
    once = PatternScopeSamples(user=("fake_praise",))
    stale = PatternScopeSamples(user=("fake_praise",) * 3)
    assert not analyze_repetition(draft, [], pattern_scopes=once).repetitive
    result = analyze_repetition(draft, [], pattern_scopes=stale)
    assert result.repetitive
    assert result.rejected_for_pattern_repeat
    assert result.pattern_frequency == 3


def test_user_scope_is_stronger_and_does_not_penalize_another_user():
    draft = "Impressive. Another failure."
    user_a = PatternScopeSamples(user=("fake_praise",) * 3)
    user_b = PatternScopeSamples(user=(), global_character=("fake_praise",) * 3)
    assert analyze_repetition(draft, [], pattern_scopes=user_a).repetitive
    assert not analyze_repetition(draft, [], pattern_scopes=user_b).repetitive


def test_channel_and_global_thresholds_are_bounded_by_strength():
    draft = "Astonishing. You failed again."
    assert analyze_repetition(
        draft, [], pattern_scopes=PatternScopeSamples(channel=("fake_praise",) * 4)
    ).repetitive
    assert not analyze_repetition(
        draft, [], pattern_scopes=PatternScopeSamples(global_character=("fake_praise",) * 7)
    ).repetitive
    assert analyze_repetition(
        draft, [], pattern_scopes=PatternScopeSamples(global_character=("fake_praise",) * 8)
    ).repetitive


def test_global_prompt_guard_uses_signatures_not_private_text():
    private = [
        "Astonishing. You failed at violet-private-detail again.",
        "Impressive. Another violet-private-detail failure.",
        "Brilliant. The violet-private-detail broke again.",
    ]
    scopes = build_pattern_scopes("scaramouche", global_messages=private)
    # Raise the deliberately weak global scope to its saturation threshold.
    scopes = PatternScopeSamples(global_character=scopes.global_character * 3)
    guard = build_prompt_guard("scaramouche", [], pattern_scopes=scopes)
    assert "fake praise" in guard
    assert "violet-private-detail" not in guard


def test_factual_and_serious_modes_allow_appropriate_structure():
    factual = "The answer is 42 because six multiplied by seven is 42."
    serious = "Please speak to someone you trust and stay somewhere safe."
    noisy = PatternScopeSamples(user=("answer_then_insult",) * 10)
    assert not analyze_repetition(
        factual, ["The result is 21 because three times seven is 21."],
        pattern_scopes=noisy, factual_mode=True,
    ).repetitive
    assert not analyze_repetition(
        serious, ["Please contact someone you trust and remain safe."],
        pattern_scopes=noisy, serious_mode=True,
    ).repetitive


def test_signature_tracks_rhetorical_and_positional_fields():
    signature = response_signature("Pathetic. The answer is seven, obviously.")
    assert signature.rhetorical_pattern == "dismissive_opening"
    assert signature.answer_position == "after_mockery"
    assert signature.mockery_position == "first"
    assert signature.sentence_count == 2


def test_runtime_pattern_history_is_bounded_and_global_has_no_raw_text():
    history = PatternHistory()
    for user_id in range(2100):
        history.remember(
            "scaramouche", f"Impressive. User {user_id} failed again.",
            user_id=user_id, channel_id=user_id,
        )
    assert len(history.users) == 2048
    assert len(history.channels) == 1024
    assert len(history.global_character["scaramouche"]) == 80
    assert set(history.global_character["scaramouche"]) == {"fake_praise"}
    history.forget_user("scaramouche", 2099)
    assert not history.samples("scaramouche", user_id=2099).user


def test_spoken_delivery_marker_does_not_hide_repeated_opening():
    recent = merge_recent_messages(["[voice spoken] Ah, Kittybri—listen."])
    assert recent == ["Ah, Kittybri—listen."]
    assert suppress_repeated_name_opener(
        "Ah, Kittybri—so you finally came back.", "Kittybri", recent,
    ) == "So you finally came back."


def test_name_opener_is_at_most_once_within_a_new_reply():
    assert suppress_repeated_name_opener(
        "Ah, Kittybri. Listen. Ah, Kittybri—answer me.", "Kittybri", [],
    ) == "Ah, Kittybri. Listen. Answer me."


def test_name_opener_guard_does_not_erase_other_names_or_quoted_words():
    assert suppress_repeated_name_opener(
        'I heard you say "Ah, Kittybri." Oh, Wanderer.',
        "Kittybri", ["Ah, Kittybri—listen."],
    ) == 'I heard you say "Ah, Kittybri." Oh, Wanderer.'
