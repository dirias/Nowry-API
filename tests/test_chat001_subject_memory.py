"""CHAT-001 — memory is kept per subject (ADR-055 §2)."""
from unittest.mock import AsyncMock, patch

import pytest

import app.routers.agent as agent
from app.routers.agent import ScreenContextPayload


def _card(card_id: str | None = "c1", front: str = "効果") -> ScreenContextPayload:
    return ScreenContextPayload(page="study_session", deckId="d1", deckName="JPN N3", cardId=card_id, front=front)


def test_a_card_chat_is_that_cards_thread() -> None:
    assert agent._subject_of(_card()) == ("card:c1", "効果")


def test_a_session_without_a_card_id_is_its_decks_thread() -> None:
    assert agent._subject_of(_card(card_id=None)) == ("deck:d1", "JPN N3")


def test_a_book_is_its_own_thread() -> None:
    ctx = ScreenContextPayload(page="book", bookId="b1", bookTitle="Biology Notes")
    assert agent._subject_of(ctx) == ("book:b1", "Biology Notes")


def test_nothing_declared_is_general() -> None:
    assert agent._subject_of(None) == ("general", "")
    assert agent._subject_of(ScreenContextPayload(page="dashboard")) == ("general", "")


@pytest.mark.asyncio
async def test_a_card_chat_replays_only_that_cards_turns() -> None:
    saved = {"agent": {"chat_history": [
        {"role": "user", "content": "plain verb forms?"},                       # before threads: General
        {"role": "model", "content": "Dictionary form, te-form…"},
        {"role": "user", "content": "examples of 効果", "subject": "card:c1"},
        {"role": "model", "content": "この薬は効果がある。", "subject": "card:c1"},
        {"role": "user", "content": "what is 結果", "subject": "card:c2"},
    ]}}
    with patch.object(agent, "users_collection") as users, patch.object(agent, "ObjectId", side_effect=lambda x: x):
        users.find_one = AsyncMock(return_value=saved)
        card = await agent._load_persistent_history("u1", subject="card:c1")
        general = await agent._load_persistent_history("u1", subject="general")

    assert [turn["content"] for turn in card] == ["examples of 効果", "この薬は効果がある。"]
    assert [turn["content"] for turn in general] == ["plain verb forms?", "Dictionary form, te-form…"]


@pytest.mark.asyncio
async def test_turns_are_saved_with_their_subject() -> None:
    with patch.object(agent, "users_collection") as users, patch.object(agent, "ObjectId", side_effect=lambda x: x):
        users.update_one = AsyncMock()
        await agent._append_to_persistent_history("u1", "q", "a", "card:c1", "効果")

    pushed = users.update_one.await_args.args[1]["$push"]["agent.chat_history"]
    assert [entry["subject"] for entry in pushed["$each"]] == ["card:c1", "card:c1"]
    assert pushed["$each"][0]["subject_title"] == "効果"
    assert pushed["$slice"] == -agent.CHAT_HISTORY_KEPT


def test_the_sessions_turns_are_sent_once() -> None:
    saved = [{"role": "user", "content": "old"}, {"role": "user", "content": "a"}, {"role": "model", "content": "b"}]
    session = [{"role": "user", "content": "a"}, {"role": "model", "content": "b"}]
    assert agent._merge_history(saved, session) == saved


def test_an_unsaved_session_is_appended() -> None:
    saved = [{"role": "user", "content": "old"}]
    session = [{"role": "user", "content": "a"}]
    assert agent._merge_history(saved, session) == saved + session
