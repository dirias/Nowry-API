"""AI-003 — a paid tier's generation falls back from Gemini to Groq (ADR-054)."""
import importlib
from unittest.mock import MagicMock, patch

import pytest

from app.ai_orchestrator.llm_clients.gemini_client import GeminiQuotaError
from app.core import llm_health


@pytest.fixture(autouse=True)
def _clean_breaker():
    llm_health.reset()
    yield
    llm_health.reset()


def _config(monkeypatch, groq: bool = True):
    """model_config reloaded with fake clients, and the fakes to drive."""
    monkeypatch.setenv("GEMINI_API_KEY", "m")
    if groq:
        monkeypatch.setenv("GROQ_API_KEY", "g")
    else:
        monkeypatch.delenv("GROQ_API_KEY", raising=False)
    fake_groq, fake_flash, fake_pro = MagicMock(), MagicMock(), MagicMock()
    with patch("app.core.model_config.Groq_client", return_value=fake_groq), \
            patch("app.core.model_config.Gemini_client", side_effect=lambda m: fake_flash if "flash" in m else fake_pro):
        import app.core.model_config as mc
        importlib.reload(mc)
    return mc, fake_groq, fake_flash, fake_pro


def test_plus_generation_is_answered_by_groq_when_gemini_is_out_of_credit(monkeypatch) -> None:
    mc, fake_groq, fake_flash, _ = _config(monkeypatch)
    fake_flash.request.side_effect = GeminiQuotaError("quota")
    fake_groq.request.return_value = "groq completion"

    assert mc.get_client_for_tier("plus").request("make cards") == "groq completion"
    fake_groq.request.assert_called_once_with("make cards")


def test_a_down_gemini_is_skipped_on_the_next_request(monkeypatch) -> None:
    mc, fake_groq, _, fake_pro = _config(monkeypatch)
    fake_pro.request.side_effect = GeminiQuotaError("quota")
    fake_groq.request.return_value = "groq completion"

    client = mc.get_client_for_tier("pro")
    client.request("one")
    client.request("two")

    assert fake_pro.request.call_count == 1
    assert fake_groq.request.call_count == 2


def test_both_failing_raises_groqs_error(monkeypatch) -> None:
    mc, fake_groq, fake_flash, _ = _config(monkeypatch)
    fake_flash.request.side_effect = GeminiQuotaError("quota")
    fake_groq.request.side_effect = TimeoutError()

    with pytest.raises(TimeoutError):
        mc.get_client_for_tier("plus").request("make cards")


def test_a_request_error_is_raised_without_trying_groq(monkeypatch) -> None:
    mc, fake_groq, fake_flash, _ = _config(monkeypatch)
    fake_flash.request.side_effect = ValueError("bad prompt")

    with pytest.raises(ValueError):
        mc.get_client_for_tier("plus").request("make cards")
    fake_groq.request.assert_not_called()


def test_free_is_the_bare_groq_client(monkeypatch) -> None:
    mc, fake_groq, _, _ = _config(monkeypatch)
    assert mc.get_client_for_tier("free") is fake_groq


def test_without_groq_plus_is_the_bare_gemini_client(monkeypatch) -> None:
    mc, _, fake_flash, _ = _config(monkeypatch, groq=False)
    assert mc.get_client_for_tier("plus") is fake_flash
