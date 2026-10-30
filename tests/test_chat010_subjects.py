"""CHAT-010 — subjects beyond the card: their keys, their prompt text, and the recent threads."""
from datetime import datetime

from app.routers import agent
from app.routers.agent import ScreenContextPayload, _build_context_injection, _recent_threads, _subject_of


def test_each_screen_is_its_own_thread() -> None:
    assert _subject_of(ScreenContextPayload(page="deck", deckId="d1", deckName="JPN N3")) == ("deck:d1", "JPN N3")
    assert _subject_of(ScreenContextPayload(page="board", boardId="b1", boardTitle="Cells")) == ("board:b1", "Cells")
    assert _subject_of(ScreenContextPayload(page="today")) == ("today", "")
    assert _subject_of(ScreenContextPayload(page="plans", goalTitle="Pass N3")) == ("plan", "Pass N3")
    assert _subject_of(ScreenContextPayload(page="annual_planning"))[0] == "plan"


def test_the_prompt_names_each_subject() -> None:
    deck = _build_context_injection(ScreenContextPayload(page="deck", deckId="d1", deckName="JPN N3", dueCards=24, newCards=5))
    assert "deck 'JPN N3' (24 due, 5 new)" in deck and "Active deck ID: d1" in deck
    board = _build_context_injection(ScreenContextPayload(page="board", boardTitle="Cells", noteText="ATP is made in mitochondria"))
    assert "board 'Cells'" in board and "ATP is made in mitochondria" in board
    today = _build_context_injection(ScreenContextPayload(page="today", reviewsDue=24, tasksLeft=3))
    assert "24 reviews due, 3 tasks left" in today
    plan = _build_context_injection(ScreenContextPayload(page="plans", goalTitle="Pass N3"))
    assert "Goal on screen: 'Pass N3'" in plan


def test_recent_threads_are_newest_first_one_each_without_general() -> None:
    history = [
        {"role": "user", "content": "a", "subject": "card:c1", "subject_title": "効果", "ts": datetime(2026, 10, 30)},
        {"role": "user", "content": "b"},
        {"role": "user", "content": "c", "subject": "book:b1", "subject_title": "Biology", "ts": datetime(2026, 10, 31)},
        {"role": "model", "content": "d", "subject": "card:c1", "subject_title": "効果", "ts": datetime(2026, 10, 31, 9)},
    ]
    threads = _recent_threads(history)
    assert [(thread.subject, thread.title) for thread in threads] == [("card:c1", "効果"), ("book:b1", "Biology")]


def test_recent_threads_are_capped() -> None:
    history = [{"subject": f"card:{i}", "subject_title": str(i)} for i in range(12)]
    assert len(_recent_threads(history)) == agent.RECENT_THREADS
