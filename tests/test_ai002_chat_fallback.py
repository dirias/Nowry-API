"""AI-002 — a paid tier's chat falls back from Gemini to Groq (ADR-054)."""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from google.api_core import exceptions as google_exc

from app.core import llm_health
from app.utils import agent_llm as module


@pytest.fixture(autouse=True)
def _clean_breaker():
    llm_health.reset()
    yield
    llm_health.reset()


def _llm() -> module.AgentLLM:
    with patch.dict("os.environ", {"GROQ_API_KEY": "g", "GEMINI_API_KEY": "m"}), \
            patch.object(module, "bounded_groq", MagicMock()), \
            patch.object(module.genai, "configure", MagicMock()):
        return module.AgentLLM()


def _chat(llm: module.AgentLLM, tier: str, meta: dict | None = None) -> str:
    return asyncio.run(llm.chat("hi", [], "system", tier=tier, meta=meta))


def test_plus_is_answered_by_groq_when_gemini_is_out_of_credit() -> None:
    llm = _llm()
    llm._chat_gemini = AsyncMock(side_effect=google_exc.ResourceExhausted("Your prepayment credits are depleted."))
    llm._chat_groq = AsyncMock(return_value="from groq")
    meta: dict = {}

    assert _chat(llm, "plus", meta) == "from groq"
    assert meta == {"provider": "groq", "fallback": True}


def test_the_next_turn_skips_gemini_without_calling_it() -> None:
    llm = _llm()
    llm._chat_gemini = AsyncMock(side_effect=google_exc.ResourceExhausted("depleted"))
    llm._chat_groq = AsyncMock(return_value="from groq")

    _chat(llm, "pro")
    _chat(llm, "pro")

    assert llm._chat_gemini.await_count == 1
    assert llm._chat_groq.await_count == 2


def test_gemini_answering_is_not_a_fallback() -> None:
    llm = _llm()
    llm._chat_gemini = AsyncMock(return_value="from gemini")
    llm._chat_groq = AsyncMock()
    meta: dict = {}

    assert _chat(llm, "plus", meta) == "from gemini"
    assert meta == {"provider": "gemini", "fallback": False}
    llm._chat_groq.assert_not_awaited()


def test_free_never_falls_back_to_gemini() -> None:
    llm = _llm()
    llm._chat_gemini = AsyncMock(return_value="from gemini")
    llm._chat_groq = AsyncMock(side_effect=google_exc.ServiceUnavailable("down"))

    with pytest.raises(module.AllProvidersFailed):
        _chat(llm, "free")
    llm._chat_gemini.assert_not_awaited()


def test_both_failing_raises_all_providers_failed_with_the_cause() -> None:
    llm = _llm()
    llm._chat_gemini = AsyncMock(side_effect=google_exc.ResourceExhausted("depleted"))
    llm._chat_groq = AsyncMock(side_effect=TimeoutError())

    with pytest.raises(module.AllProvidersFailed) as raised:
        _chat(llm, "plus")
    assert raised.value.providers == ["gemini", "groq"]
    assert isinstance(raised.value.__cause__, TimeoutError)


def test_a_request_error_is_raised_without_trying_groq() -> None:
    llm = _llm()
    llm._chat_gemini = AsyncMock(side_effect=google_exc.InvalidArgument("bad request"))
    llm._chat_groq = AsyncMock()

    with pytest.raises(google_exc.InvalidArgument):
        _chat(llm, "plus")
    llm._chat_groq.assert_not_awaited()
