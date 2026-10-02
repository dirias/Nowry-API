"""
Someone hears a user who writes in (GTM-007, docs/prd-road-to-market.md FR-011).

One function, `notify(subject, text)`, posts a plain-text email to the
operator's mailbox through Resend's REST API. It is guarded on three
environment variables and is a silent no-op without them, so development and
the test suite send nothing. It never raises: a notification that fails is
logged and the request that triggered it still succeeds, because the message
or report was already stored.

    RESEND_API_KEY     the key from resend.com
    NOTIFY_TO_EMAIL    where notifications land (the mailbox someone reads)
    NOTIFY_FROM_EMAIL  a sender on a domain verified in Resend, e.g. "Nowry <notify@nowry.app>"
"""
from __future__ import annotations

import os
from typing import Optional

import httpx

from app.utils.logger import get_logger

logger = get_logger(__name__)

RESEND_ENDPOINT = "https://api.resend.com/emails"


def notification_config() -> Optional[dict]:
    """The three settings, or None when any is missing (then nothing is sent)."""
    key = (os.getenv("RESEND_API_KEY") or "").strip()
    to = (os.getenv("NOTIFY_TO_EMAIL") or "").strip()
    sender = (os.getenv("NOTIFY_FROM_EMAIL") or "").strip()
    if not (key and to and sender):
        return None
    return {"key": key, "to": to, "from": sender}


async def notify(subject: str, text: str) -> bool:
    """Send one plain-text notification. True when Resend accepted it, False otherwise. Never raises."""
    config = notification_config()
    if config is None:
        return False
    payload = {"from": config["from"], "to": [config["to"]], "subject": subject[:200], "text": text[:20000]}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                RESEND_ENDPOINT,
                json=payload,
                headers={"Authorization": f"Bearer {config['key']}", "Content-Type": "application/json"},
            )
        if response.status_code >= 300:
            logger.warning("notify: Resend answered %s: %s", response.status_code, response.text[:200])
            return False
        return True
    except Exception as exc:  # noqa: BLE001 - a failed notification must never fail the request
        logger.warning("notify: could not send '%s': %s", subject[:60], exc)
        return False


def contact_notification(doc: dict) -> tuple[str, str]:
    """Subject and body for a contact message."""
    return (
        f"[Nowry] Contact from {doc.get('name', '')}",
        f"From: {doc.get('name', '')} <{doc.get('email', '')}>\nLocale: {doc.get('locale')}\nPage: {doc.get('page')}\n\n{doc.get('message', '')}",
    )


def copyright_notification(doc: dict) -> tuple[str, str]:
    """Subject and body for a copyright notice."""
    return (
        f"[Nowry] Copyright notice from {doc.get('name', '')}",
        f"From: {doc.get('name', '')} <{doc.get('email', '')}>\nSigned: {doc.get('signature', '')}\nLocale: {doc.get('locale')}\n\nWork:\n{doc.get('work', '')}\n\nLocation in Nowry:\n{doc.get('location', '')}",
    )


def report_notification(report: dict, report_id: str) -> tuple[str, str]:
    """Subject and body for a content report."""
    return (
        f"[Nowry] Content report: {report.get('reason', '')} on {report.get('content_type', '')} '{report.get('content_title', '')}'",
        f"Report {report_id}\nContent: {report.get('content_type', '')} {report.get('content_id', '')} — {report.get('content_title', '')}\nReason: {report.get('reason', '')}\nReporter: {report.get('reporter_email', '')} ({report.get('reporter_user_id', '')})\n\n{report.get('description') or ''}",
    )
