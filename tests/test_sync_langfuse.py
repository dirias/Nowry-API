"""Tests for Phase 11 Sync Script — SY-01, SY-02.

Wave 0 RED stubs: these tests import scripts.sync_langfuse, which does not yet
exist. They fail with ModuleNotFoundError for 'scripts.sync_langfuse' (correct
RED state) until Task 2 delivers the script. Task 2 then makes them pass GREEN.

Test isolation strategy mirrors test_model_config.py / test_langfuse_client.py:
- sys.modules.setdefault() guard prevents groq/google.generativeai/langfuse
  SDK import errors on the Python 3.9 test runner
- monkeypatch for env vars; patch() for the Langfuse constructor and the
  model_config singleton client attributes
- each test imports scripts.sync_langfuse inside the test body (not at module
  level) so the ImportError during RED is contained to each test individually,
  matching the test_prompt_manager.py pattern for not-yet-existing modules
"""

import sys
from unittest.mock import MagicMock

# Prevent SDK import errors on Python 3.9 test runner
sys.modules.setdefault("groq", MagicMock())
sys.modules.setdefault("google.generativeai", MagicMock())
sys.modules.setdefault("langfuse", MagicMock())

import importlib
import inspect
import pytest
from unittest.mock import patch, MagicMock

from app.core.prompt_manager import _FALLBACKS


def test_missing_credentials_exits_nonzero(monkeypatch):
    """11-01-01 / SY-01: missing LANGFUSE_SECRET_KEY/LANGFUSE_PUBLIC_KEY exits 1
    BEFORE any Langfuse client construction or API call (T-11-01 fail-fast)."""
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)

    from scripts import sync_langfuse

    fake_langfuse_ctor = MagicMock()
    with patch("scripts.sync_langfuse.Langfuse", fake_langfuse_ctor, create=True):
        with pytest.raises(SystemExit) as exc_info:
            sync_langfuse.main([])

    assert exc_info.value.code == 1
    assert fake_langfuse_ctor.call_count == 0


def test_compare_before_push_skips_unchanged(monkeypatch):
    """11-01-02 / SY-01, SY-02: identical content is detected as unchanged —
    zero create_prompt calls, returns 'unchanged' (D-05 compare-before-push)."""
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk_lf_test_key")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk_lf_test_key")

    from scripts import sync_langfuse

    local_content = _FALLBACKS["nowry-book-expand"]

    fake_client = MagicMock()
    fake_current = MagicMock()
    fake_current.prompt = local_content
    fake_client.get_prompt.return_value = fake_current

    result = sync_langfuse.compare_and_push_prompt(
        fake_client, "nowry-book-expand", local_content, dry_run=False
    )

    assert fake_client.create_prompt.call_count == 0
    assert result == "unchanged"


def test_compare_before_push_pushes_changed(monkeypatch):
    """11-01-03 / SY-01, SY-02: differing content triggers exactly one
    create_prompt call labeled 'production', returns 'pushed' (D-05)."""
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk_lf_test_key")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk_lf_test_key")

    from scripts import sync_langfuse

    local_content = _FALLBACKS["nowry-book-expand"]
    name = "nowry-book-expand"

    fake_client = MagicMock()
    fake_current = MagicMock()
    fake_current.prompt = "OLD CONTENT THAT DIFFERS"
    fake_client.get_prompt.return_value = fake_current

    result = sync_langfuse.compare_and_push_prompt(
        fake_client, name, local_content, dry_run=False
    )

    assert fake_client.create_prompt.call_count == 1
    _, kwargs = fake_client.create_prompt.call_args
    assert kwargs["labels"] == ["production"]
    assert kwargs["name"] == name
    assert kwargs["prompt"] == local_content
    assert result == "pushed"


def test_model_config_derived_from_live_wiring(monkeypatch):
    """11-02-01 / SY-02: nowry-model-config values are derived live from
    model_config.py's singleton client attributes — never hardcoded, never
    read from subscription_plans.AGENT_MODELS (D-07 / Pitfall 4 guard)."""
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk_lf_test_key")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk_lf_test_key")

    fake_groq = MagicMock()
    fake_groq.model = "llama-3.3-70b-versatile"
    fake_flash = MagicMock()
    fake_flash._model_id = "models/gemini-flash-latest"
    fake_pro = MagicMock()
    fake_pro._model_id = "models/gemini-pro-latest"

    import app.core.model_config as model_config

    with patch.object(model_config, "_groq_client", fake_groq), \
         patch.object(model_config, "_gemini_flash_client", fake_flash), \
         patch.object(model_config, "_gemini_pro_client", fake_pro):
        from scripts import sync_langfuse
        result = sync_langfuse.derive_model_config_dict()

    assert result == {
        "free": "llama-3.3-70b-versatile",
        "plus": "models/gemini-flash-latest",
        "pro": "models/gemini-pro-latest",
    }

    source = inspect.getsource(sync_langfuse.derive_model_config_dict)
    assert "subscription_plans" not in source
    assert "AGENT_MODELS" not in source
