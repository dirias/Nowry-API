"""
GTM-004 — companion chat reaches the model each tier is sold (v1.0 Phase 4).

`AgentLLM._get_provider` used to prefer Groq whenever its key was set, so a
Plus or Pro chat never reached Gemini. The tier decides now; a missing key
falls back with a warning instead of failing the chat.
"""
import sys
from unittest.mock import MagicMock, patch

from tests._stubs import stub_if_missing

stub_if_missing("langfuse", "langfuse.langchain", "groq", "google.generativeai")

import pytest


def _llm(groq=None, gemini=None):
    env = {}
    if groq:
        env["GROQ_API_KEY"] = groq
    if gemini:
        env["GEMINI_API_KEY"] = gemini
    with patch.dict("os.environ", env, clear=False):
        for name in ("GROQ_API_KEY", "GEMINI_API_KEY"):
            if name not in env:
                import os
                os.environ.pop(name, None)
        from app.utils import agent_llm as module
        with patch.object(module, "Groq", MagicMock()), patch.object(module.genai, "configure", MagicMock()):
            return module.AgentLLM()


@pytest.mark.parametrize("tier, expected", [("free", "groq"), ("plus", "gemini"), ("pro", "gemini"), (None, "groq")])
def test_each_tier_reaches_the_provider_it_is_sold(tier, expected):
    llm = _llm(groq="g", gemini="m")
    assert llm._get_provider(tier) == expected


def test_a_missing_gemini_key_falls_back_to_groq_for_paid_tiers():
    llm = _llm(groq="g")
    assert llm._get_provider("pro") == "groq"
    assert llm._get_provider("free") == "groq"


def test_a_missing_groq_key_falls_back_to_gemini_for_free():
    llm = _llm(gemini="m")
    assert llm._get_provider("free") == "gemini"
    assert llm._get_provider("plus") == "gemini"


def test_no_keys_is_an_error():
    llm = _llm()
    with pytest.raises(ValueError):
        llm._get_provider("free")


def test_chat_passes_the_tier_to_the_provider_choice():
    import inspect
    from app.utils.agent_llm import AgentLLM

    source = inspect.getsource(AgentLLM.chat)
    assert "self._get_provider(tier)" in source
