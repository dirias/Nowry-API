"""CHAT-015 — the nudge sends its facts and the learner's tone; the client writes the words (ADR-056 §6)."""
from app.routers.agent import _nudge_from


def test_the_busiest_deck_with_its_waiting_cards_and_tone() -> None:
    nudge = _nudge_from([{"deck_name": "JPN N3", "due_cards": 3, "new_cards": 2}], 9, "strict")
    assert (nudge.deck_name, nudge.count, nudge.tone, nudge.has_nudge) == ("JPN N3", 5, "strict", True)


def test_the_total_without_a_deck() -> None:
    nudge = _nudge_from([], 1, "friendly")
    assert nudge.deck_name is None and nudge.count == 1
    assert "1 card to review" in nudge.nudge


def test_no_handle_in_the_fallback_sentence() -> None:
    assert not _nudge_from([], 4, "friendly").nudge.startswith("Hey")
