"""
Regression tests for Groq `tool_use_failed` graceful degradation.

Known Groq/Llama failure mode: the model emits a malformed tool call with the
arguments JSON embedded in the function NAME (e.g.
`<function=start_quiz {"confidence": "high"}>`), so Groq's server-side
validator rejects it with HTTP 400 code='tool_use_failed' ("attempted to call
tool ... which was not in request.tools") even though the tool WAS sent.

AgentLLM._chat_groq must detect this specific failure, retry the turn once
without tools (plain text answer), and still propagate every other error.
"""
import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.utils.agent_llm import AgentLLM


class FakeGroqToolUseFailed(Exception):
    """Mimics groq.BadRequestError shape for code='tool_use_failed'."""

    def __init__(self) -> None:
        self.status_code = 400
        self.body = {
            "error": {
                "message": (
                    "tool call validation failed: attempted to call tool "
                    "'start_quiz {\"confidence\": \"high\", \"mode\": \"ai\"}' "
                    "which was not in request.tools"
                ),
                "type": "invalid_request_error",
                "code": "tool_use_failed",
                "failed_generation": (
                    '<function=start_quiz {"confidence": "high"}></function>'
                ),
            }
        }
        super().__init__(self.body["error"]["message"])


def _fake_completion(content: str) -> MagicMock:
    msg = MagicMock()
    msg.content = content
    msg.tool_calls = None
    completion = MagicMock()
    completion.choices = [MagicMock(message=msg)]
    return completion


def _fake_gemini_tools() -> list:
    decl = MagicMock()
    decl.name = "start_quiz"
    decl.description = "quiz tool"
    decl.parameters = None
    tool = MagicMock()
    tool.function_declarations = [decl]
    return [tool]


def _make_llm(create_side_effect: Any) -> AgentLLM:
    llm = AgentLLM.__new__(AgentLLM)  # skip __init__ (no env keys needed)
    llm.groq_model = "llama-3.3-70b-versatile"
    client = MagicMock()
    client.chat.completions.create.side_effect = create_side_effect
    llm.groq_client = client
    return llm


def test_is_tool_use_failed_detects_groq_error_body() -> None:
    llm = AgentLLM.__new__(AgentLLM)
    assert llm._is_tool_use_failed(FakeGroqToolUseFailed()) is True


def test_is_tool_use_failed_detects_string_only() -> None:
    llm = AgentLLM.__new__(AgentLLM)
    assert llm._is_tool_use_failed(Exception("400 code: tool_use_failed")) is True


def test_is_tool_use_failed_ignores_other_errors() -> None:
    llm = AgentLLM.__new__(AgentLLM)
    assert llm._is_tool_use_failed(ValueError("rate limit exceeded")) is False
    assert llm._is_tool_use_failed(Exception("401 invalid api key")) is False


def test_tool_use_failed_retries_once_without_tools() -> None:
    calls: list[dict] = []

    def create(**kwargs: Any) -> MagicMock:
        calls.append(kwargs)
        if "tools" in kwargs:
            raise FakeGroqToolUseFailed()
        return _fake_completion("Here are more examples using における: ...")

    llm = _make_llm(create)
    reply = asyncio.new_event_loop().run_until_complete(
        llm._chat_groq(
            message="Give me more examples using における grammar",
            history=[],
            system_prompt="sys",
            tools=_fake_gemini_tools(),
            tool_dispatcher=None,
            user_id="u1",
        )
    )
    assert len(calls) == 2, "expected exactly one tool-less retry"
    assert "tools" in calls[0]
    assert "tools" not in calls[1] and "tool_choice" not in calls[1]
    assert "における" in reply


def test_genuine_groq_error_still_propagates() -> None:
    llm = _make_llm(ValueError("401 invalid api key"))
    with pytest.raises(ValueError, match="invalid api key"):
        asyncio.new_event_loop().run_until_complete(
            llm._chat_groq("hi", [], "sys", _fake_gemini_tools(), None, "u1")
        )
