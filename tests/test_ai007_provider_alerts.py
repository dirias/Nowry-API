"""AI-007 (ADR-091, prd-ai-resilience FR-006): the operator hears when an AI provider stops answering.

A Gemini quota or credit refusal sends one alert to NOTIFY_TO_EMAIL, at most once
per provider in any 6 hours; the last provider failing (no fallback left) sends the
urgent one. Each alert is a Sentry warning naming the provider only. An alert never
fails the request, from the event loop or from a worker thread.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from google.api_core import exceptions as google_exc

from app.core import llm_health
from app.emails.render import KINDS, render_email
from app.services import ai_alerts

QUOTA = google_exc.ResourceExhausted("Your prepayment credits are depleted")
OUTAGE = google_exc.ServiceUnavailable("unavailable")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    llm_health.reset()
    monkeypatch.setenv("NOTIFY_TO_EMAIL", "support@nowry.app")
    yield
    llm_health.reset()


def test_which_failures_raise_an_alert():
    assert ai_alerts.should_alert("gemini", QUOTA, last=False) is True
    assert ai_alerts.should_alert("gemini", OUTAGE, last=False) is False  # recovers in a minute
    assert ai_alerts.should_alert("groq", OUTAGE, last=True) is True  # nothing is left behind it
    assert ai_alerts.should_alert("groq", ValueError("bad request"), last=True) is False  # the request's fault


@pytest.mark.asyncio
async def test_a_gemini_credit_refusal_sends_one_email_and_one_warning():
    send = AsyncMock(return_value="sent")
    claim = AsyncMock(return_value=True)
    sentry = MagicMock()
    with patch.object(ai_alerts, "send_email", send), patch.object(ai_alerts, "claim_rolling", claim), \
         patch.object(ai_alerts, "sentry_warning", sentry):
        outcome = await ai_alerts.alert("gemini", QUOTA, last=False, fallback="groq")
    assert outcome == "sent"
    kind, to, locale, values = send.await_args.args
    assert (kind, to, locale) == ("ops.ai_provider", "support@nowry.app", "en")
    assert values["provider"] == "Gemini" and values["fallback"] == "Groq" and values["reason"] == "ResourceExhausted"
    assert claim.await_args.args[:3] == ("ai_alert", "gemini:down", 6 * 3600)
    sentry.assert_called_once()
    message, tags = sentry.call_args.args
    assert "Gemini" in message and tags == {"provider": "gemini", "urgent": "no"}


@pytest.mark.asyncio
async def test_within_6_hours_nothing_more_is_sent():
    send = AsyncMock(return_value="sent")
    sentry = MagicMock()
    with patch.object(ai_alerts, "send_email", send), patch.object(ai_alerts, "claim_rolling", AsyncMock(return_value=False)), \
         patch.object(ai_alerts, "sentry_warning", sentry):
        assert await ai_alerts.alert("gemini", QUOTA, last=False, fallback="groq") == "limited"
    send.assert_not_awaited()
    sentry.assert_not_called()


@pytest.mark.asyncio
async def test_the_last_provider_failing_is_urgent_with_its_own_key():
    send = AsyncMock(return_value="sent")
    claim = AsyncMock(return_value=True)
    sentry = MagicMock()
    with patch.object(ai_alerts, "send_email", send), patch.object(ai_alerts, "claim_rolling", claim), \
         patch.object(ai_alerts, "sentry_warning", sentry):
        await ai_alerts.alert("groq", OUTAGE, last=True, fallback=None)
    assert send.await_args.args[0] == "ops.ai_urgent"
    assert claim.await_args.args[1] == "groq:urgent"
    assert sentry.call_args.args[1] == {"provider": "groq", "urgent": "yes"}


@pytest.mark.asyncio
async def test_an_alert_that_fails_never_raises():
    with patch.object(ai_alerts, "claim_rolling", AsyncMock(side_effect=RuntimeError("store down"))), \
         patch.object(ai_alerts, "sentry_warning", MagicMock()):
        assert await ai_alerts.alert("gemini", QUOTA, last=False, fallback="groq") == "failed"


def test_the_emails_render_in_english_with_no_user_content():
    for kind in ("ops.ai_provider", "ops.ai_urgent"):
        assert KINDS[kind]["class"] == "notice" and KINDS[kind]["sender"] == "account" and KINDS[kind]["locale"] == "en"
        rendered = render_email(kind, "es", {"email": "support@nowry.app", "support": "support@nowry.app", "provider": "Gemini",
                                             "fallback": "Groq", "reason": "ResourceExhausted", "time": "2026-11-14 01:00"},
                                logo_url="https://www.nowry.app/logo192.png")
        assert rendered.locale == "en" and "Gemini" in rendered.subject
    urgent = render_email("ops.ai_urgent", "en", {"email": "a@b.c", "support": "a@b.c", "provider": "Groq", "fallback": "",
                                                  "reason": "ServiceUnavailable", "time": "t"}, logo_url="x")
    assert urgent.subject.startswith("[Nowry] URGENT")


# ── The two places a provider fails ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_chat_chain_reports_gemini_then_groq():
    from app.utils.agent_llm import AgentLLM, AllProvidersFailed

    llm = AgentLLM.__new__(AgentLLM)
    llm.groq_api_key = "k"
    reports = []
    with patch.object(AgentLLM, "_get_provider", lambda self, tier=None: "gemini"), \
         patch.object(AgentLLM, "_chat_gemini", AsyncMock(side_effect=QUOTA)), \
         patch.object(AgentLLM, "_chat_groq", AsyncMock(side_effect=OUTAGE)), \
         patch.object(ai_alerts, "report_soon", lambda provider, exc, last, fallback: reports.append((provider, last, fallback))):
        with pytest.raises(AllProvidersFailed):
            await llm.chat("hi", [], "system", tier="pro")
    assert reports == [("gemini", False, "groq"), ("groq", True, None)]


def test_the_generation_client_reports_from_its_worker_thread():
    from app.core.model_config import FallbackClient

    gemini = MagicMock(request=MagicMock(side_effect=QUOTA))
    groq = MagicMock(request=MagicMock(return_value="answer"))
    reports = []
    with patch.object(ai_alerts, "report_soon", lambda provider, exc, last, fallback: reports.append((provider, last, fallback))):
        assert FallbackClient(gemini, groq).request("prompt") == "answer"
    assert reports == [("gemini", False, "groq")]


@pytest.mark.asyncio
async def test_report_soon_hands_a_thread_s_alert_to_the_app_loop():
    ran = asyncio.Event()

    async def fake_alert(provider, exc, last, fallback):
        ran.set()
        return "sent"

    ai_alerts.remember_loop(asyncio.get_running_loop())
    with patch.object(ai_alerts, "alert", fake_alert):
        await asyncio.to_thread(ai_alerts.report_soon, "gemini", QUOTA, False, "groq")
        await asyncio.wait_for(ran.wait(), timeout=2)
    with patch.object(ai_alerts, "alert", fake_alert):
        ai_alerts.report_soon("groq", OUTAGE, True, None)  # also from the loop itself
