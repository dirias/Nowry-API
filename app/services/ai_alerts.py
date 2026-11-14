"""
The operator hears when an AI provider stops answering (AI-007, ADR-091).

ADR-054 moves a request from Gemini to Groq when Gemini fails, silently. This
module raises the alarm: a quota or credit refusal from Gemini, or any provider
failure of the last provider in a chain (nothing is left behind it, so the
companion and card generation stop). One email to `NOTIFY_TO_EMAIL` on the
account layout and one Sentry warning, at most once per provider in any 6 hours,
claimed in the shared store so two workers do not both send. No prompt, reply,
account or address of a user is in either.

    should_alert(provider, exc, last)   whether this failure is one to report
    report_soon(provider, exc, last, fallback)   from anywhere, never blocks or raises
    alert(...)                          the coroutine that claims, warns and sends
"""
from __future__ import annotations

import asyncio
import os
from concurrent.futures import Future
from datetime import datetime, timezone
from typing import Optional, Set

from app.core import llm_health
from app.services.email import send_email
from app.utils.keyed_rate_limit import claim_rolling
from app.utils.logger import get_logger

logger = get_logger(__name__)

WINDOW_SECONDS = 6 * 3600
NAMES = {"gemini": "Gemini", "groq": "Groq"}

_loop: Optional[asyncio.AbstractEventLoop] = None
_pending: Set[object] = set()


def remember_loop(loop: asyncio.AbstractEventLoop) -> None:
    """The app's event loop, so an alert raised in a worker thread can be run on it."""
    global _loop
    _loop = loop


def should_alert(provider: str, exc: BaseException, last: bool) -> bool:
    """Gemini refusing for quota or credit; or the last provider failing for any provider reason."""
    if last:
        return llm_health.is_provider_failure(exc)
    return provider == "gemini" and llm_health.is_quota_failure(exc)


def sentry_warning(message: str, tags: dict) -> None:
    """A warning in Sentry, when Sentry is set up. Never raises."""
    try:
        import sentry_sdk

        with sentry_sdk.new_scope() as scope:
            for key, value in tags.items():
                scope.set_tag(key, value)
            sentry_sdk.capture_message(message, level="warning")
    except Exception:  # noqa: BLE001 - reporting must not fail anything
        pass


async def alert(provider: str, exc: BaseException, last: bool, fallback: Optional[str]) -> str:
    """Claim the provider's 6 hours, warn Sentry and email the operator. Returns the outcome; never raises."""
    urgent = last
    try:
        if not await claim_rolling("ai_alert", f"{provider}:{'urgent' if urgent else 'down'}", WINDOW_SECONDS):
            return "limited"
        name = NAMES.get(provider, provider.title())
        reason = type(exc).__name__
        if urgent:
            sentry_warning(f"AI provider {name} failed and no provider is left ({reason})", {"provider": provider, "urgent": "yes"})
        else:
            sentry_warning(f"AI provider {name} refused for quota or credit; traffic went to {NAMES.get(fallback or '', fallback)} ({reason})",
                           {"provider": provider, "urgent": "no"})
        to = (os.getenv("NOTIFY_TO_EMAIL") or "").strip()
        if not to:
            return "unconfigured"
        values = {
            "provider": name,
            "fallback": NAMES.get(fallback or "", fallback or ""),
            "reason": reason,
            "time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
        }
        return await send_email("ops.ai_urgent" if urgent else "ops.ai_provider", to, "en", values)
    except Exception as error:  # noqa: BLE001 - an alert never fails the request
        logger.warning("ai alert: could not raise the alert for %s: %s", provider, type(error).__name__)
        return "failed"


def report_soon(provider: str, exc: BaseException, last: bool, fallback: Optional[str]) -> None:
    """Report a provider failure without waiting for it, from the event loop or a worker thread. Never raises."""
    try:
        if not should_alert(provider, exc, last):
            return
        coroutine = alert(provider, exc, last, fallback)
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is not None:
            task = running.create_task(coroutine)
        elif _loop is not None and _loop.is_running():
            task = asyncio.run_coroutine_threadsafe(coroutine, _loop)
        else:
            coroutine.close()
            logger.warning("ai alert: no event loop to report %s on", provider)
            return
        _pending.add(task)
        done = (lambda finished: _pending.discard(finished))
        if isinstance(task, Future):
            task.add_done_callback(done)
        else:
            task.add_done_callback(done)
    except Exception as error:  # noqa: BLE001
        logger.warning("ai alert: could not schedule the alert: %s", type(error).__name__)
