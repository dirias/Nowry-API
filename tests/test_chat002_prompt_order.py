"""CHAT-002 — the subject goes last; one identity; the phone gets only what it can use (ADR-055 §3)."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.routers.agent import ChatRequest, ScreenContextPayload, _build_system_prompt

USER = {
    "username": "rikydier",
    "preferences": {"general": {"primary_topic": "Japanese", "interests": ["technology"], "study_goal": "language"}},
}
CARD = ScreenContextPayload(page="study_session", deckId="d1", deckName="JPN N3", cardId="c1", front="効果", isFlipped=False)


def _prompt(client: str = "phone", **kwargs) -> str:
    return _build_system_prompt(USER, True, screen_context=CARD, pet_name="Luna", client=client, **kwargs)


def test_the_subject_is_the_last_section() -> None:
    prompt = _prompt()
    subject_at = prompt.rfind("THE SUBJECT OF THIS CONVERSATION")
    assert subject_at > prompt.find("DEFAULT LANGUAGE")
    assert "Front: 効果" in prompt[subject_at:]


def test_one_identity_and_no_handle() -> None:
    prompt = _prompt()
    assert "You are Luna" in prompt
    assert "Study Buddy" not in prompt
    assert "rikydier" not in prompt


def test_an_unnamed_companion_is_nowry() -> None:
    assert "You are Nowry" in _build_system_prompt(USER, False, client="phone")


def test_the_phone_gets_no_quiz_contract_and_stays_short() -> None:
    phone = _prompt("phone")
    assert "start_quiz" not in phone.split("THE SUBJECT OF THIS CONVERSATION")[0].split("SMALL TALK")[0]
    assert "QUIZ MODE" not in phone
    assert len(phone.split()) < 1000


def test_the_web_keeps_its_quiz_contract() -> None:
    assert "QUIZ MODE" in _prompt("web")


def test_interests_yield_to_a_subject() -> None:
    assert "Only when the subject gives no example of its own" in _prompt()
    assert "Only when the subject gives no example" not in _build_system_prompt(USER, False, client="phone")


def test_language_rules_only_for_a_language_subject() -> None:
    science_user = {"username": "x", "preferences": {"general": {"study_goal": "academic"}}}
    assert "LANGUAGE LEARNING SPECIFICS" in _prompt()
    assert "LANGUAGE LEARNING SPECIFICS" not in _build_system_prompt(science_user, False, client="phone")


def test_a_request_without_client_is_the_web() -> None:
    assert ChatRequest(message="hi").client == "web"
    assert ChatRequest(message="hi", client="phone").client == "phone"


@pytest.mark.asyncio
@pytest.mark.parametrize("client, quiz_attached", [("phone", False), ("web", True)])
async def test_the_quiz_tool_is_only_attached_for_the_web(client: str, quiz_attached: bool) -> None:
    import app.routers.agent as agent_module

    user = {"_id": "u1", "subscription": {"tier": "free"}, "agent": {"xp": 0}, "preferences": {}}
    with patch.object(agent_module, "users_collection") as users, \
            patch.object(agent_module, "get_langfuse_client", return_value=None), \
            patch.object(agent_module.agent_llm, "chat", new_callable=AsyncMock, return_value="ok") as chat, \
            patch.object(agent_module, "grant_xp", new_callable=AsyncMock,
                         return_value={"level_up": False, "new_level": 1, "new_stage": 1}), \
            patch.object(agent_module, "ObjectId", side_effect=lambda x: x):
        users.find_one = AsyncMock(return_value=user)
        users.update_one = AsyncMock(return_value=MagicMock(matched_count=1))
        route = getattr(agent_module.chat, "__wrapped__", agent_module.chat)
        await route(request=MagicMock(), body=ChatRequest(message="hi", client=client), current_user={"user_id": "u1"})

    tools = chat.await_args.kwargs["tools"]
    assert any(tool is quiz for tool in tools for quiz in agent_module.QUIZ_TOOLS) is quiz_attached


def test_examples_are_asked_for_as_tagged_lines() -> None:
    # CHAT-004: both clients draw [[EX|…]] lines as rows.
    for client in ("phone", "web"):
        assert "[[EX|sentence|reading|translation]]" in _prompt(client)
