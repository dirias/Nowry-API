"""Tests for Phase 10 Model Config — MC-02, MC-03.

Wave 0 stubs: these tests import app.core.model_config which does not yet
exist. The first three tests fail with ImportError (correct RED state) until
Wave 2 delivers the module. test_no_duplicate_helpers will also fail until
Wave 4 removes the per-router helper functions.

Test isolation strategy mirrors test_langfuse_client.py:
- sys.modules.setdefault() guard prevents Groq/Gemini SDK import errors on 3.9 runner
- importlib.reload() resets module-level state between tests
- monkeypatch for env vars; patch() for LLM client constructors
"""

import sys
from unittest.mock import MagicMock

# Prevent SDK import errors on Python 3.9 test runner
sys.modules.setdefault("groq", MagicMock())
sys.modules.setdefault("google.generativeai", MagicMock())
sys.modules.setdefault("langfuse", MagicMock())

import importlib
import pytest
import re
from pathlib import Path
from unittest.mock import patch, MagicMock


def test_free_tier_returns_groq(monkeypatch):
    """MC-02: get_client_for_tier('free') returns the Groq singleton."""
    monkeypatch.setenv("GROQ_API_KEY", "test-groq-key")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    fake_groq = MagicMock()
    with patch("app.core.model_config.Groq_client", return_value=fake_groq):
        import app.core.model_config as mc
        importlib.reload(mc)
        result = mc.get_client_for_tier("free")
    assert result is fake_groq


def test_plus_tier_returns_gemini_flash(monkeypatch):
    """MC-02: get_client_for_tier('plus') returns the Gemini Flash singleton."""
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    fake_flash = MagicMock()
    fake_pro = MagicMock()

    def fake_gemini(model_name):
        return fake_flash if "flash" in model_name else fake_pro

    with patch("app.core.model_config.Gemini_client", side_effect=fake_gemini):
        import app.core.model_config as mc
        importlib.reload(mc)
        result = mc.get_client_for_tier("plus")
    assert result is fake_flash


def test_pro_tier_returns_gemini_pro(monkeypatch):
    """MC-02: get_client_for_tier('pro') returns the Gemini Pro singleton."""
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    fake_flash = MagicMock()
    fake_pro = MagicMock()

    def fake_gemini(model_name):
        return fake_flash if "flash" in model_name else fake_pro

    with patch("app.core.model_config.Gemini_client", side_effect=fake_gemini):
        import app.core.model_config as mc
        importlib.reload(mc)
        result = mc.get_client_for_tier("pro")
    assert result is fake_pro


def test_no_duplicate_helpers():
    """MC-03: _get_llm_client_for_tier (any variant) must not appear in books.py, quiz_ai.py, or cards.py."""
    api_dir = Path(__file__).parent.parent / "app" / "routers"
    for filename in ["books.py", "quiz_ai.py", "cards.py"]:
        filepath = api_dir / filename
        content = filepath.read_text()
        # Strip comment lines before checking
        non_comment_lines = [
            line for line in content.splitlines()
            if not line.strip().startswith("#")
        ]
        non_comment_text = "\n".join(non_comment_lines)
        assert "_get_llm_client_for_tier" not in non_comment_text, (
            f"MC-03 VIOLATION: _get_llm_client_for_tier found in {filename}. "
            "All duplicated helpers must be removed and replaced with model_config.get_client_for_tier()."
        )
        assert "_get_groq_client" not in non_comment_text, (
            f"MC-03 VIOLATION: _get_groq_client helper found in {filename}. "
            "Must be removed — Groq client is centralized in model_config.py."
        )
