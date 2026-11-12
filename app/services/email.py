"""
Nowry writes to one person (MAIL-004, docs/prd-road-to-market.md FR-021, ADR-078).

`send_email(kind, to, locale, values)` renders the two parts on the shared
layout, checks the day's budget for the kind's class, posts to Resend with the
kind's sender, and records the attempt. It answers with an outcome and never
raises; anything but "sent" tells the caller to fall back (for account email,
to Firebase's own).

    RESEND_API_KEY        the key from resend.com
    EMAIL_FROM_ACCOUNT    e.g. "Nowry <account@nowry.app>"
    EMAIL_REPLY_TO        e.g. support@nowry.app
    EMAIL_FROM_PERSONAL   e.g. "Didier from Nowry <hello@nowry.app>"
    EMAIL_POSTAL_ADDRESS  a footer line; absent while empty
    EMAIL_DAILY_BUDGET    sends a day for this environment (default 90)
    EMAIL_ALLOWLIST       development: the only addresses or @domains written to
    FRONTEND_URL          where links point
    EMAIL_MARK_URL        the mark image; a public PNG (default: production's)
"""
from __future__ import annotations

import os
from typing import Mapping, Optional

import httpx

from app.emails.budget import may_send, parse_budget
from email.utils import formataddr, parseaddr

from app.emails.render import KINDS, RenderedEmail, load_strings, render_email, resolve_locale
from app.services import email_ledger, email_suppressions
from app.utils.logger import get_logger

logger = get_logger(__name__)

RESEND_ENDPOINT = "https://api.resend.com/emails"
RESEND_COUNT_HEADER = "x-resend-daily-quota"

SENT = "sent"
UNCONFIGURED = "unconfigured"
NOT_ALLOWED = "not_allowed"
OVER_BUDGET = "over_budget"
SUPPRESSED = "suppressed"
FAILED = "failed"


def _env(name: str) -> str:
    return (os.getenv(name) or "").strip()


def frontend_url() -> str:
    return (_env("FRONTEND_URL") or "http://localhost:3000").rstrip("/")


# The mark is fetched by mail providers' own servers (Gmail's image proxy), not
# by the reader, so it must be public. A preview site behind a login, such as
# development's, answers them with a redirect and the image shows broken. So
# every environment uses production's file unless told otherwise.
DEFAULT_MARK_URL = "https://www.nowry.app/logo192.png"


def mark_url() -> str:
    return _env("EMAIL_MARK_URL") or DEFAULT_MARK_URL


def personal_sender(setting: str, locale: Optional[str]) -> str:
    """The personal From with its name in the reader's language (ADR-078 point 2, ADR-080 point 7).

    The setting gives the address; the name before it comes from the locale file.
    """
    address = parseaddr(setting)[1]
    if not address:
        return ""
    return formataddr((load_strings(resolve_locale(locale))["common"]["sender_personal"], address))


def sender_config(kind: str, locale: Optional[str] = None) -> Optional[dict]:
    """The key, From and Reply-To for a kind, or None when it cannot be sent from here."""
    key = _env("RESEND_API_KEY")
    if KINDS[kind]["sender"] == "account":
        sender, reply_to = _env("EMAIL_FROM_ACCOUNT"), _env("EMAIL_REPLY_TO")
    else:
        sender, reply_to = personal_sender(_env("EMAIL_FROM_PERSONAL"), locale), ""
    if not (key and sender):
        return None
    return {"key": key, "from": sender, "reply_to": reply_to}


def without_tag(address: str) -> str:
    """`ana+test@example.com` reads as `ana@example.com`: a "+tag" names the same mailbox."""
    local, at, domain = address.strip().lower().rpartition("@")
    return f"{local.split('+', 1)[0]}{at}{domain}"


def allowed_recipient(to: str, allowlist: str) -> bool:
    """With no list, anyone. With one, only its addresses and `@domain` entries.

    Whatever follows a "+" in the local part is ignored on both sides, so one
    entry covers every alias of that mailbox.
    """
    entries = [entry.strip().lower() for entry in allowlist.split(",") if entry.strip()]
    if not entries:
        return True
    address = without_tag(to)
    return any(address.endswith(entry) if entry.startswith("@") else address == without_tag(entry) for entry in entries)


def resend_payload(config: dict, to: str, rendered: RenderedEmail, headers: Optional[Mapping[str, str]] = None) -> dict:
    payload = {"from": config["from"], "to": [to], "subject": rendered.subject[:200], "text": rendered.text, "html": rendered.html}
    if config["reply_to"]:
        payload["reply_to"] = config["reply_to"]
    if headers:
        payload["headers"] = dict(headers)
    return payload


async def _post(config: dict, payload: dict) -> bool:
    """True when Resend accepted it. Never raises."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                RESEND_ENDPOINT, json=payload,
                headers={"Authorization": f"Bearer {config['key']}", "Content-Type": "application/json"},
            )
    except Exception as exc:  # noqa: BLE001 - a failed email must never fail the request
        logger.warning("send_email: could not reach Resend: %s", exc)
        return False
    if response.status_code >= 300:
        logger.warning("send_email: Resend answered %s: %s", response.status_code, response.text[:200])
        return False
    await email_ledger.note_resend_count(response.headers.get(RESEND_COUNT_HEADER))
    return True


async def send_email(
    kind: str, to: str, locale: Optional[str], values: Mapping[str, str], headers: Optional[Mapping[str, str]] = None
) -> str:
    """Send one email of a known kind. Returns the outcome; only `SENT` means it left.

    `headers` are extra message headers (a visitor kind's `List-Unsubscribe`).
    A letter to a visitor is not sent to an address that bounced or complained
    (ADR-080 point 5); account email is not held back by that mark.
    """
    config = sender_config(kind, locale)
    if config is None:
        return UNCONFIGURED  # tests, a fresh checkout: nothing is sent and nothing is recorded
    email_class = KINDS[kind]["class"]
    # "Questions:" names an address alone, never "Name <address>".
    filled = {"email": to, "support": config["reply_to"] or parseaddr(config["from"])[1], **values}
    rendered = render_email(kind, locale, filled, logo_url=mark_url(), postal=_env("EMAIL_POSTAL_ADDRESS"))
    if not allowed_recipient(to, _env("EMAIL_ALLOWLIST")):
        outcome = NOT_ALLOWED
    elif KINDS[kind]["sender"] == "personal" and await email_suppressions.is_suppressed(to):
        outcome = SUPPRESSED
    elif not may_send(email_class, await email_ledger.sent_today(), parse_budget(os.getenv("EMAIL_DAILY_BUDGET"))):
        outcome = OVER_BUDGET
    else:
        outcome = SENT if await _post(config, resend_payload(config, to, rendered, headers)) else FAILED
    if outcome != SENT:
        logger.warning("send_email: %s not sent (%s)", kind, outcome)
    await email_ledger.record(kind, email_class, outcome, rendered.locale, to)
    return outcome
