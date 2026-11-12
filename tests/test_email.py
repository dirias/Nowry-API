"""MAIL-004 — one layout, one sending function, a ledger (docs/prd-road-to-market.md FR-021, ADR-078)."""
import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.emails import budget
from app.emails.render import KINDS, LOCALES, render_email, resolve_locale
from app.services import email as email_service
from app.services import email_ledger, notifications

LOCALE_DIR = Path(__file__).resolve().parents[1] / "app" / "emails" / "locales"
VALUES = {"email": "ana@example.com", "link": "https://nowry.app/auth/action?mode=verifyEmail&oobCode=abc", "support": "support@nowry.app"}
ENV = {"RESEND_API_KEY": "re_test", "EMAIL_FROM_ACCOUNT": "Nowry <account@nowry.app>", "EMAIL_REPLY_TO": "support@nowry.app", "FRONTEND_URL": "https://nowry.app"}
ALL_ENV = (*ENV, "EMAIL_MARK_URL", "EMAIL_FROM_PERSONAL", "EMAIL_POSTAL_ADDRESS", "EMAIL_DAILY_BUDGET", "EMAIL_ALLOWLIST", "NOTIFY_TO_EMAIL", "NOTIFY_FROM_EMAIL")


def _shape(node, path=""):
    """Every key path in a locale file, with the length of each list."""
    if isinstance(node, dict):
        return {entry for key, value in node.items() for entry in _shape(value, f"{path}.{key}")}
    return {f"{path}[{len(node)}]" if isinstance(node, list) else path}


def _resend(status=200, headers=None):
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.post = AsyncMock(return_value=MagicMock(status_code=status, text="x", headers=headers or {}))
    return client


@pytest.fixture
def ledger(monkeypatch):
    """Settings for an account email, and a ledger that touches no database."""
    for name in ALL_ENV:
        monkeypatch.delenv(name, raising=False)
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)
    fake = MagicMock(sent_today=AsyncMock(return_value=0), record=AsyncMock(), note_resend_count=AsyncMock())
    monkeypatch.setattr(email_service.email_ledger, "sent_today", fake.sent_today)
    monkeypatch.setattr(email_service.email_ledger, "record", fake.record)
    monkeypatch.setattr(email_service.email_ledger, "note_resend_count", fake.note_resend_count)
    return fake


# --- the words ---------------------------------------------------------------

def test_every_locale_has_every_key_english_has():
    english = _shape(json.loads((LOCALE_DIR / "en.json").read_text(encoding="utf-8")))
    for locale in LOCALES:
        assert _shape(json.loads((LOCALE_DIR / f"{locale}.json").read_text(encoding="utf-8"))) == english, locale
    assert sorted(path.stem for path in LOCALE_DIR.glob("*.json")) == sorted(LOCALES)


def test_every_kind_has_its_words():
    english = json.loads((LOCALE_DIR / "en.json").read_text(encoding="utf-8"))
    for kind in KINDS:
        group, name = kind.split(".")
        assert {"subject", "before", "button", "after", "signature", "footer"} <= set(english[group][name])


@pytest.mark.parametrize("locale", LOCALES)
@pytest.mark.parametrize("kind", sorted(KINDS))
def test_both_parts_render_in_every_language_with_nothing_left_unfilled(kind, locale):
    rendered = render_email(kind, locale, VALUES, logo_url="https://nowry.app/logo192.png")
    assert rendered.locale == locale and rendered.subject
    for part in (rendered.subject, rendered.text, rendered.html):
        assert "$" not in part
    assert VALUES["link"] in rendered.text.splitlines()  # the link alone on its line
    assert 'href="https://nowry.app/auth/action?mode=verifyEmail&amp;oobCode=abc"' in rendered.html
    assert f'<html lang="{locale}">' in rendered.html and "ana@example.com" in rendered.html
    assert rendered.html.count("<img ") == 1 and 'alt="Nowry"' in rendered.html


def test_an_unknown_language_reads_as_english_and_a_region_is_trimmed():
    assert [resolve_locale(code) for code in ("es-MX", "ES", "ja_JP", "pt", "", None)] == ["es", "es", "ja", "en", "en", "en"]
    assert render_email("account.reset", "pt-BR", VALUES, logo_url="x").subject == "Reset your Nowry password"


def test_values_are_escaped_in_the_html_and_left_alone_in_the_text():
    values = {**VALUES, "email": 'a"<b>@example.com'}
    rendered = render_email("account.verify", "en", values, logo_url="x")
    assert "<b>@example.com" not in rendered.html and "a&quot;&lt;b&gt;@example.com" in rendered.html
    assert 'a"<b>@example.com' in rendered.text


def test_the_postal_line_is_absent_until_it_is_set():
    without = render_email("account.verify", "en", VALUES, logo_url="x")
    with_address = render_email("account.verify", "en", VALUES, logo_url="x", postal="1 Example St, City")
    assert "1 Example St" not in without.html + without.text
    assert "1 Example St, City" in with_address.html and with_address.text.rstrip().endswith("1 Example St, City")


def test_an_unknown_kind_or_a_missing_value_is_a_fault():
    with pytest.raises(KeyError):
        render_email("account.nope", "en", VALUES, logo_url="x")
    with pytest.raises(KeyError):
        render_email("account.verify", "en", {"link": "x", "support": "y"}, logo_url="x")


# --- the budget --------------------------------------------------------------

def test_lower_classes_stop_early_so_account_email_keeps_a_fifth_of_the_day():
    assert [budget.ceiling(name, 100) for name in ("account", "invitation", "notice", "confirmation")] == [100, 80, 80, 60]
    assert budget.may_send("confirmation", 59, 100) and not budget.may_send("confirmation", 60, 100)
    assert budget.may_send("notice", 79, 100) and not budget.may_send("notice", 80, 100)
    assert budget.may_send("account", 99, 100) and not budget.may_send("account", 100, 100)
    assert budget.ceiling("something-new", 100) == 60


def test_the_budget_setting_is_a_number_or_the_default():
    assert [budget.parse_budget(raw) for raw in ("10", " 100 ", "", None, "many", "-5")] == [10, 100, 90, 90, 90, 90]


# --- sending -----------------------------------------------------------------

def test_an_allow_list_names_addresses_and_domains_and_none_means_anyone():
    assert email_service.allowed_recipient("ana@example.com", "")
    assert email_service.allowed_recipient("Ana@Example.com", "ana@example.com, @nowry.app")
    assert email_service.allowed_recipient("didier@nowry.app", "ana@example.com, @nowry.app")
    assert not email_service.allowed_recipient("someone@else.org", "ana@example.com, @nowry.app")


def test_an_entry_covers_every_plus_alias_of_its_mailbox_and_no_other():
    listed = "ana@example.com, bo+team@example.com"
    assert email_service.allowed_recipient("ana+anything@example.com", listed)
    assert email_service.allowed_recipient("Ana+a+b@Example.com", listed)
    assert email_service.allowed_recipient("bo@example.com", listed) and email_service.allowed_recipient("bo+other@example.com", listed)
    assert not email_service.allowed_recipient("anab@example.com", listed)
    assert not email_service.allowed_recipient("ana+x@other.org", listed)
    assert not email_service.allowed_recipient("x+ana@example.com", listed)
    assert email_service.allowed_recipient("me+tag@nowry.app", "@nowry.app")


async def test_without_the_settings_nothing_is_sent_and_nothing_is_recorded(ledger, monkeypatch):
    monkeypatch.delenv("EMAIL_FROM_ACCOUNT")
    with patch.object(email_service.httpx, "AsyncClient") as client:
        assert await email_service.send_email("account.verify", "ana@example.com", "es", {"link": "L"}) == email_service.UNCONFIGURED
    client.assert_not_called()
    ledger.record.assert_not_awaited()


async def test_an_account_email_goes_out_with_both_parts_its_sender_and_reply_to(ledger):
    client = _resend(headers={"x-resend-daily-quota": "7"})
    with patch.object(email_service.httpx, "AsyncClient", return_value=client):
        assert await email_service.send_email("account.verify", "ana@example.com", "es-MX", {"link": VALUES["link"]}) == email_service.SENT
    args, kwargs = client.post.call_args
    payload = kwargs["json"]
    assert args[0] == email_service.RESEND_ENDPOINT and kwargs["headers"]["Authorization"] == "Bearer re_test"
    assert (payload["from"], payload["to"], payload["reply_to"]) == ("Nowry <account@nowry.app>", ["ana@example.com"], "support@nowry.app")
    assert payload["subject"] == "Confirma tu correo para Nowry"
    assert VALUES["link"] in payload["text"] and "support@nowry.app" in payload["text"]
    # The mark is production's public file whatever FRONTEND_URL is: a preview site behind a login breaks it in Gmail.
    assert 'src="https://www.nowry.app/logo192.png"' in payload["html"]
    assert set(payload) == {"from", "to", "subject", "text", "html", "reply_to"}  # no tracking, no tags
    ledger.record.assert_awaited_once_with("account.verify", "account", "sent", "es", "ana@example.com")
    ledger.note_resend_count.assert_awaited_once_with("7")


async def test_a_refusal_from_resend_or_no_network_is_failed_and_recorded(ledger):
    with patch.object(email_service.httpx, "AsyncClient", return_value=_resend(status=429)):
        assert await email_service.send_email("account.reset", "ana@example.com", "en", {"link": "L"}) == email_service.FAILED
    with patch.object(email_service.httpx, "AsyncClient", side_effect=RuntimeError("down")):
        assert await email_service.send_email("account.reset", "ana@example.com", "en", {"link": "L"}) == email_service.FAILED
    assert [call.args[2] for call in ledger.record.await_args_list] == ["failed", "failed"]


async def test_a_spent_day_and_an_address_off_the_list_send_nothing(ledger, monkeypatch):
    monkeypatch.setenv("EMAIL_DAILY_BUDGET", "10")
    ledger.sent_today.return_value = 10
    with patch.object(email_service.httpx, "AsyncClient") as client:
        assert await email_service.send_email("account.verify", "ana@example.com", "en", {"link": "L"}) == email_service.OVER_BUDGET
        monkeypatch.setenv("EMAIL_ALLOWLIST", "@nowry.app")
        ledger.sent_today.return_value = 0
        assert await email_service.send_email("account.verify", "ana@example.com", "en", {"link": "L"}) == email_service.NOT_ALLOWED
    client.assert_not_called()
    assert [call.args[2] for call in ledger.record.await_args_list] == ["over_budget", "not_allowed"]


# --- the ledger --------------------------------------------------------------

def test_a_ledger_row_holds_no_address():
    now = datetime(2026, 11, 12, 23, 59, tzinfo=timezone.utc)
    row = email_ledger.ledger_row("account.verify", "account", "sent", "es", " Ana@Example.com ", now)
    assert row == {"kind": "account.verify", "class": "account", "outcome": "sent", "locale": "es",
                   "recipient_hash": email_ledger.recipient_hash("ana@example.com"), "day": "2026-11-12", "created_at": now}
    assert "ana" not in json.dumps(row, default=str).lower()


async def test_the_count_is_ours_or_resends_when_that_is_higher_and_zero_when_unreadable(monkeypatch):
    collection = MagicMock(count_documents=AsyncMock(return_value=4), find_one=AsyncMock(return_value={"resend_count": 9}))
    monkeypatch.setattr(email_ledger, "email_sends_collection", collection)
    assert await email_ledger.sent_today() == 9
    collection.find_one.return_value = None
    assert await email_ledger.sent_today() == 4
    collection.count_documents.side_effect = RuntimeError("no database")
    assert await email_ledger.sent_today() == 0


async def test_recording_never_raises(monkeypatch):
    collection = MagicMock(insert_one=AsyncMock(side_effect=RuntimeError("no database")), update_one=AsyncMock(side_effect=RuntimeError("no")))
    monkeypatch.setattr(email_ledger, "email_sends_collection", collection)
    await email_ledger.record("account.verify", "account", "sent", "en", "ana@example.com")
    await email_ledger.note_resend_count("12")
    await email_ledger.note_resend_count(None)
    assert collection.update_one.await_count == 1


# --- notices share the day ---------------------------------------------------

async def test_a_notice_past_its_share_of_the_day_is_not_sent(monkeypatch):
    for name, value in {"RESEND_API_KEY": "re_test", "NOTIFY_TO_EMAIL": "ops@example.com", "NOTIFY_FROM_EMAIL": "Nowry <notify@nowry.app>", "EMAIL_DAILY_BUDGET": "100"}.items():
        monkeypatch.setenv(name, value)
    record = AsyncMock()
    monkeypatch.setattr(notifications.email_ledger, "record", record)
    monkeypatch.setattr(notifications.email_ledger, "note_resend_count", AsyncMock())
    monkeypatch.setattr(notifications.email_ledger, "sent_today", AsyncMock(return_value=80))
    with patch.object(notifications.httpx, "AsyncClient") as client:
        assert await notifications.notify("s", "t") is False
    client.assert_not_called()
    record.assert_awaited_once_with("notice", "notice", "over_budget", "en", "ops@example.com")
    monkeypatch.setattr(notifications.email_ledger, "sent_today", AsyncMock(return_value=79))
    with patch.object(notifications.httpx, "AsyncClient", return_value=_resend()):
        assert await notifications.notify("s", "t") is True
    assert record.await_args.args[2] == "sent"


def test_the_mark_is_a_public_png_that_does_not_follow_the_site_address(monkeypatch):
    monkeypatch.delenv("EMAIL_MARK_URL", raising=False)
    monkeypatch.setenv("FRONTEND_URL", "https://www.dev.nowry.app")
    assert email_service.mark_url() == "https://www.nowry.app/logo192.png"
    assert email_service.mark_url().startswith("https://") and email_service.mark_url().endswith(".png")
    monkeypatch.setenv("EMAIL_MARK_URL", "https://cdn.example.com/mark.png")
    assert email_service.mark_url() == "https://cdn.example.com/mark.png"
