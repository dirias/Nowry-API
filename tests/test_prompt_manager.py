"""Tests for Phase 10 Prompt Manager — PM-01, PM-02, PM-03, MC-01.

Wave 0 stubs: these tests import app.core.prompt_manager which does not yet
exist. Each test fails with ImportError (correct RED state) until Wave 1
delivers the module.

Test isolation strategy mirrors test_langfuse_client.py:
- sys.modules.setdefault() guard prevents langfuse SDK import errors on 3.9 runner
- importlib.reload() resets module-level state between tests
- monkeypatch for env vars; patch() for external clients
- asyncio_mode = auto (from pytest.ini) governs @pytest.mark.asyncio behavior
"""

import sys
from unittest.mock import MagicMock

# Prevent langfuse from failing on Python 3.9 type syntax during collection
sys.modules.setdefault("langfuse", MagicMock())

import builtins
import importlib
import json
import logging
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock


def test_get_prompt_uses_langfuse(monkeypatch):
    """PM-01: get_prompt() returns Langfuse-hosted prompt when client is available."""
    import app.core.prompt_manager as pm
    importlib.reload(pm)
    fake_client = MagicMock()
    fake_prompt = MagicMock()
    fake_prompt.compile.return_value = "compiled-from-langfuse"
    fake_client.get_prompt.return_value = fake_prompt
    with patch("app.core.prompt_manager._prompt_cache", {}):
        with patch("app.core.langfuse_client.get_langfuse_client", return_value=fake_client):
            result = pm.get_prompt("nowry-cards-magic", prompt="test", sample_text="ctx", sample_number=5)
    assert result == "compiled-from-langfuse"


def test_get_prompt_fallback(monkeypatch):
    """PM-02: get_prompt() returns hardcoded constant when Langfuse is unavailable."""
    import app.core.prompt_manager as pm
    importlib.reload(pm)
    with patch("app.core.prompt_manager._prompt_cache", {}):
        with patch("app.core.langfuse_client.get_langfuse_client", return_value=None):
            result = pm.get_prompt("nowry-cards-magic", prompt="q", sample_text="s", sample_number=3)
    assert "q" in result or len(result) > 0  # fallback string returned, not empty


def test_get_prompt_fallback_logs_warning(monkeypatch, caplog):
    """PM-02: WARNING is logged when falling back to hardcoded constant."""
    import app.core.prompt_manager as pm
    importlib.reload(pm)
    with caplog.at_level(logging.WARNING, logger="app.core.prompt_manager"):
        with patch("app.core.prompt_manager._prompt_cache", {}):
            with patch("app.core.langfuse_client.get_langfuse_client", return_value=None):
                pm.get_prompt("nowry-cards-magic")
    assert any("[prompt_manager] Langfuse unavailable" in r.message for r in caplog.records)


def test_prompt_name_convention():
    """PM-03: All 8 keys in _FALLBACKS follow the nowry-* kebab-case convention (no / chars)."""
    import app.core.prompt_manager as pm
    importlib.reload(pm)
    assert len(pm._FALLBACKS) == 8
    for name in pm._FALLBACKS:
        assert name.startswith("nowry-"), f"Prompt name '{name}' must start with 'nowry-'"
        assert "/" not in name, f"Prompt name '{name}' must not contain '/'"


@pytest.mark.asyncio
async def test_prewarm_writes_prompts(tmp_path):
    """MC-01: prewarm() writes all 8 prompt templates to langfuse_cache.json['prompts']."""
    import app.core.prompt_manager as pm
    importlib.reload(pm)
    # Prepare a tmp cache file matching the real structure
    cache_file = tmp_path / "langfuse_cache.json"
    cache_file.write_text(json.dumps({"version": 1, "updated_at": None, "prompts": {}, "model_config": {}}))
    with patch("app.core.langfuse_client.get_langfuse_client", return_value=None):
        with patch("app.core.prompt_manager._prompt_cache", {}):
            real_open = builtins.open

            def patched_open(path, *args, **kwargs):
                if "langfuse_cache.json" in str(path):
                    return real_open(str(cache_file), *args, **kwargs)
                return real_open(path, *args, **kwargs)

            with patch("builtins.open", side_effect=patched_open):
                await pm.prewarm()
    data = json.loads(cache_file.read_text())
    assert "prompts" in data
    assert len(data["prompts"]) == 8
    for name in pm._FALLBACKS:
        assert name in data["prompts"], f"prewarm() must write prompt '{name}' to cache"
