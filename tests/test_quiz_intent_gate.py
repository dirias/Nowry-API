"""
Regression tests for the deterministic server-side start_quiz intent gate.

Bug: Llama/gpt-oss on Groq pattern-match study-flavored content requests
("Give me an example using this card") into start_quiz with self-reported
confidence="high", ignoring the prompt's never-call rules. The gate in
app.routers.agent._dispatch_tool_call_inner re-validates intent with a tiny
secondary LLM call and vetoes the tool via ToolCallRejectedError, which the
provider loop turns into a re-run of the turn without the tool.
"""
import sys
from unittest.mock import AsyncMock, MagicMock

# Python 3.9 compatibility stubs (mirrors test_smart_pet.py)
for mod in ["google.generativeai", "google.generativeai.types",
            "google.generativeai.protos", "google.api_core.exceptions"]:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()

for mod in ["slowapi", "slowapi.util", "langfuse"]:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()

if "app.core.limiter" not in sys.modules:
    _mock_limiter_mod = MagicMock()
    _mock_limiter_instance = MagicMock()
    _mock_limiter_instance.limit = lambda *a, **k: (lambda fn: fn)
    _mock_limiter_mod.limiter = _mock_limiter_instance
    sys.modules["app.core.limiter"] = _mock_limiter_mod

if "app.auth.firebase_auth" not in sys.modules:
    _mock_firebase_auth_mod = MagicMock()
    _mock_firebase_auth_mod.get_firebase_user = MagicMock()
    sys.modules["app.auth.firebase_auth"] = _mock_firebase_auth_mod

# Full-suite ordering guard (mirrors test_smart_pet.py): ensure app.models.quiz
# exposes real Pydantic classes even if an earlier test registered a MagicMock.
_quiz_mod = sys.modules.get("app.models.quiz")
if _quiz_mod is None or not isinstance(getattr(_quiz_mod, "QuizConfig", None), type):
    from typing import Optional as _Optional
    from pydantic import BaseModel as _BM

    class _QuizConfig(_BM):
        mode: str
        topic: _Optional[str] = None
        question_count: int = 10
        deck_id: _Optional[str] = None

    class _QuizOffer(_BM):
        topic: str
        mode: str = "ai"
        question_count: int = 10

    if _quiz_mod is None:
        _quiz_mod = MagicMock()
        sys.modules["app.models.quiz"] = _quiz_mod
    _quiz_mod.QuizConfig = _QuizConfig
    _quiz_mod.QuizOffer = _QuizOffer

import importlib as _importlib

sys.modules.pop("app.config.subscription_plans", None)
sys.modules["app.config.subscription_plans"] = _importlib.import_module(
    "app.config.subscription_plans"
)
sys.modules.pop("app.models.agent_models", None)
sys.modules["app.models.agent_models"] = _importlib.import_module(
    "app.models.agent_models"
)

import json

import pytest

from app.routers import agent as agent_module
from app.utils.agent_llm import ToolCallRejectedError

_HIGH_CONF_ARGS = {"mode": "ai", "confidence": "high", "topic": "行動する"}


def _patch_intent(monkeypatch: pytest.MonkeyPatch, verdict) -> AsyncMock:
    mock = AsyncMock(return_value=verdict)
    monkeypatch.setattr(agent_module.agent_llm, "intent_yes_no", mock)
    return mock


@pytest.mark.asyncio
async def test_gate_rejects_content_request(monkeypatch):
    """Classifier says NO → ToolCallRejectedError, quiz never configured."""
    _patch_intent(monkeypatch, False)
    holder: dict = {}
    with pytest.raises(ToolCallRejectedError):
        await agent_module._dispatch_tool_call_inner(
            "start_quiz", dict(_HIGH_CONF_ARGS), "u1",
            quiz_result_holder=holder,
            user_message="Give me an example using this card",
        )
    assert holder == {}, "a rejected call must never populate quiz_config"


@pytest.mark.asyncio
async def test_gate_allows_explicit_quiz_request(monkeypatch):
    """Classifier says YES → quiz launches exactly as before."""
    _patch_intent(monkeypatch, True)
    holder: dict = {}
    result = json.loads(await agent_module._dispatch_tool_call_inner(
        "start_quiz", dict(_HIGH_CONF_ARGS), "u1",
        quiz_result_holder=holder,
        user_message="quiz me on 行動する",
    ))
    assert result["status"] == "launching"
    assert holder["quiz_config"]["topic"] == "行動する"


@pytest.mark.asyncio
async def test_gate_fails_open_when_classifier_unavailable(monkeypatch):
    """Classifier verdict None (unavailable/unparseable) → fail-open: a
    transient classifier failure must never block a legitimate quiz."""
    _patch_intent(monkeypatch, None)
    holder: dict = {}
    result = json.loads(await agent_module._dispatch_tool_call_inner(
        "start_quiz", dict(_HIGH_CONF_ARGS), "u1",
        quiz_result_holder=holder,
        user_message="quiz me",
    ))
    assert result["status"] == "launching"
    assert "quiz_config" in holder


@pytest.mark.asyncio
async def test_gate_skipped_for_non_high_confidence(monkeypatch):
    """confidence != high keeps the existing soft rejection (error string to
    the model) and never spends a classifier call."""
    mock = _patch_intent(monkeypatch, False)
    holder: dict = {}
    result = json.loads(await agent_module._dispatch_tool_call_inner(
        "start_quiz", {"mode": "ai", "confidence": "medium"}, "u1",
        quiz_result_holder=holder,
        user_message="help me study X",
    ))
    assert "error" in result
    assert holder == {}
    mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_gate_fails_open_without_user_message(monkeypatch):
    """No threaded user message (e.g. legacy caller) → gate cannot judge →
    fail-open, preserving pre-gate behavior."""
    mock = _patch_intent(monkeypatch, False)
    holder: dict = {}
    result = json.loads(await agent_module._dispatch_tool_call_inner(
        "start_quiz", dict(_HIGH_CONF_ARGS), "u1",
        quiz_result_holder=holder,
    ))
    assert result["status"] == "launching"
    mock.assert_not_awaited()
