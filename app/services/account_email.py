"""
Verification and password reset, written by Nowry (MAIL-006, MAIL-007,
docs/prd-road-to-market.md FR-023 and FR-024, ADR-078 points 5 to 8).

Firebase keeps the accounts and the one-time codes. This module asks the Admin
SDK for a link, keeps only its code, puts it on a Nowry address, and sends the
email on the shared layout. When that cannot be done, Firebase's own email is
the fallback: the signed-in client sends its own verification, and for a reset
this module asks Firebase's public endpoint, so the caller learns nothing.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
from typing import Optional
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
from firebase_admin import auth as firebase_admin_auth

from app.emails.render import resolve_locale
from app.services import email as email_service
from app.services.notifications import notify
from app.utils.keyed_rate_limit import longest_wait, seconds_to_wait
from app.utils.logger import get_logger

logger = get_logger(__name__)

FIREBASE_SEND_CODE = "https://identitytoolkit.googleapis.com/v1/accounts:sendOobCode"
MINUTE, HOUR, DAY = 60, 3600, 86400
SENT, FALLBACK = "sent", "fallback"


def address_key(address: str) -> str:
    """A limit is counted on a hash, so the counter never holds an address."""
    return hashlib.sha256(address.strip().lower().encode("utf-8")).hexdigest()[:32]


def action_link(firebase_link: str, mode: str, frontend_url: str) -> str:
    """Firebase's link carries a one-time code; move it onto Nowry's own page. Raises ValueError without one."""
    codes = parse_qs(urlparse(firebase_link).query).get("oobCode") or []
    if not codes or not codes[0]:
        raise ValueError("no oobCode in the link")
    return f"{frontend_url}/auth/action?{urlencode({'mode': mode, 'oobCode': codes[0]})}"


async def verification_wait(firebase_uid: str, caller: str) -> int:
    """Seconds before this account may be sent another verification email; 0 when it may now."""
    return await longest_wait([
        ("email_verify_minute", firebase_uid, 1, MINUTE),
        ("email_verify_day", firebase_uid, 5, DAY),
        ("email_verify_ip", caller, 10, HOUR),
    ])


async def reset_wait(address: str, caller: str) -> int:
    key = address_key(address)
    return await longest_wait([
        ("email_reset_hour", key, 3, HOUR),
        ("email_reset_day", key, 5, DAY),
        ("email_reset_ip", caller, 5, HOUR),
    ])


async def _note_fallback(kind: str, reason: str) -> None:
    """Tell the operator, at most once an hour, that Firebase's email went out in place of ours."""
    logger.warning("account email: %s fell back to Firebase (%s)", kind, reason)
    if reason == email_service.UNCONFIGURED:
        return  # nothing is set up here; that is not news
    if await seconds_to_wait("email_fallback_notice", "all", 1, HOUR) == 0:
        await notify("[Nowry] Email: Resend refused, Firebase used", f"Kind: {kind}\nReason: {reason}\n\nOne notice an hour at most.")


async def send_verification(email: str, language: Optional[str]) -> str:
    """Send the verification email on the layout. `SENT`, or `FALLBACK` when the client should ask Firebase."""
    try:
        firebase_link = await asyncio.to_thread(firebase_admin_auth.generate_email_verification_link, email)
        link = action_link(firebase_link, "verifyEmail", email_service.frontend_url())
    except Exception as exc:  # noqa: BLE001 - any failure here has one answer: Firebase's own email
        await _note_fallback("account.verify", f"no link: {type(exc).__name__}")
        return FALLBACK
    outcome = await email_service.send_email("account.verify", email, language, {"link": link})
    if not email_service.delivered(outcome):
        await _note_fallback("account.verify", outcome)
        return FALLBACK
    return SENT


def _providers(user) -> set:
    return {getattr(provider, "provider_id", "") for provider in (getattr(user, "provider_data", None) or [])}


def _has_password(user) -> bool:
    return not getattr(user, "disabled", False) and "password" in _providers(user)


def _google_only(user) -> bool:
    """Signs in with Google and has no password (ADR-083): told so, not sent a reset."""
    return not getattr(user, "disabled", False) and _providers(user) == {"google.com"}


async def send_google_notice(email: str, language: Optional[str]) -> None:
    """`account.google`: this account signs in with Google. No Firebase fallback: Firebase has nothing to send."""
    try:
        link = f"{email_service.frontend_url()}/login"
        outcome = await email_service.send_email("account.google", email, language, {"link": link})
    except Exception as exc:  # noqa: BLE001 - runs after the answer; nothing to raise to
        outcome = f"failed: {type(exc).__name__}"
    if not email_service.delivered(outcome):
        logger.warning("account email: the Google sign-in notice was not sent (%s)", outcome)


async def firebase_sends_reset(email: str, language: Optional[str]) -> bool:
    """The fallback: Firebase's stock reset email, asked for from here so the caller learns nothing."""
    key = (os.getenv("FIREBASE_WEB_API_KEY") or "").strip()
    if not key:
        logger.warning("account email: no FIREBASE_WEB_API_KEY, so a reset had no fallback")
        return False
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                FIREBASE_SEND_CODE, params={"key": key}, json={"requestType": "PASSWORD_RESET", "email": email},
                headers={"X-Firebase-Locale": resolve_locale(language)},
            )
        return response.status_code < 300
    except Exception as exc:  # noqa: BLE001
        logger.warning("account email: Firebase's reset fallback failed: %s", type(exc).__name__)
        return False


async def send_reset(email: str, language: Optional[str]) -> None:
    """Runs after the answer. A password account gets the reset; a Google-only one the notice (ADR-083). Never raises."""
    try:
        user = await asyncio.to_thread(firebase_admin_auth.get_user_by_email, email)
    except firebase_admin_auth.UserNotFoundError:
        return
    except Exception as exc:  # noqa: BLE001
        logger.warning("account email: could not look the address up: %s", type(exc).__name__)
        return
    if not _has_password(user):
        if _google_only(user):
            await send_google_notice(email, language)
        return
    try:
        firebase_link = await asyncio.to_thread(firebase_admin_auth.generate_password_reset_link, email)
        link = action_link(firebase_link, "resetPassword", email_service.frontend_url())
        outcome = await email_service.send_email("account.reset", email, language, {"link": link})
    except Exception as exc:  # noqa: BLE001
        outcome = f"no link: {type(exc).__name__}"
    if not email_service.delivered(outcome):
        await _note_fallback("account.reset", outcome)
        await firebase_sends_reset(email, language)
