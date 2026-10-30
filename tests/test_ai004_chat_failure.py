"""AI-004 — chat answers 503 ai_unavailable, and a failed reply is not charged (ADR-054)."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.utils.agent_llm import AllProvidersFailed


def _body():
    from app.routers.agent import ChatRequest
    return ChatRequest(message="Give me examples", history=[], language="en", context=None)


async def _send(chat_outcome):
    """Run the route with the model call replaced; return (response or error, users mock)."""
    import app.routers.agent as agent_module

    user = {"_id": "u1", "subscription": {"tier": "free"}, "agent": {"xp": 0}, "preferences": {}}
    with patch.object(agent_module, "users_collection") as users, \
            patch.object(agent_module, "get_langfuse_client", return_value=None), \
            patch.object(agent_module, "_append_to_persistent_history", new_callable=AsyncMock), \
            patch.object(agent_module, "_load_persistent_history", new_callable=AsyncMock, return_value=[]), \
            patch.object(agent_module.agent_llm, "chat", new_callable=AsyncMock) as chat, \
            patch.object(agent_module, "grant_xp", new_callable=AsyncMock,
                         return_value={"level_up": False, "new_level": 1, "new_stage": 1}), \
            patch.object(agent_module, "ObjectId", side_effect=lambda x: x):
        users.find_one = AsyncMock(return_value=user)
        users.update_one = AsyncMock(return_value=MagicMock(matched_count=1))
        if isinstance(chat_outcome, BaseException):
            chat.side_effect = chat_outcome
        else:
            chat.return_value = chat_outcome
        # Past the rate limiter, which wants a real Starlette request.
        route = getattr(agent_module.chat, "__wrapped__", agent_module.chat)
        try:
            result = await route(request=MagicMock(), body=_body(), current_user={"user_id": "u1"})
        except HTTPException as error:
            result = error
    return result, users


def _increments(users) -> list[int]:
    """The `$inc` applied to the month's count by each write, in order."""
    increments = []
    for call in users.update_one.await_args_list:
        update = call.args[1] if len(call.args) > 1 else call.kwargs.get("update", {})
        counted = update.get("$inc", {}) if isinstance(update, dict) else {}
        if "agent.messages_used_this_month" in counted:
            increments.append(counted["agent.messages_used_this_month"])
    return increments


@pytest.mark.asyncio
async def test_no_provider_is_a_503_the_clients_can_read() -> None:
    error, _ = await _send(AllProvidersFailed(["groq"]))
    assert error.status_code == 503
    assert error.detail == "ai_unavailable"
    assert error.headers["Retry-After"] == "60"


@pytest.mark.asyncio
async def test_a_failed_reply_gives_the_message_back() -> None:
    _, users = await _send(AllProvidersFailed(["groq"]))
    assert _increments(users) == [1, -1]


@pytest.mark.asyncio
async def test_an_unexpected_failure_stays_502_and_is_refunded() -> None:
    error, users = await _send(RuntimeError("boom"))
    assert error.status_code == 502
    assert _increments(users) == [1, -1]


@pytest.mark.asyncio
async def test_a_reply_still_counts_one_message() -> None:
    response, users = await _send("Here are three examples.")
    assert response.reply == "Here are three examples."
    assert _increments(users) == [1]
