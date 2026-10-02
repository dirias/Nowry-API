"""GTM-007 — someone hears a user who writes in (docs/prd-road-to-market.md FR-011)."""
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import notifications

APP = Path(__file__).resolve().parents[1] / "app"
ENV = {"RESEND_API_KEY": "re_test", "NOTIFY_TO_EMAIL": "ops@example.com", "NOTIFY_FROM_EMAIL": "Nowry <notify@nowry.app>"}


def _clear(monkeypatch):
    for name in ENV:
        monkeypatch.delenv(name, raising=False)


@pytest.mark.asyncio
async def test_without_the_three_settings_nothing_is_sent(monkeypatch):
    _clear(monkeypatch)
    with patch.object(notifications.httpx, "AsyncClient") as client:
        assert await notifications.notify("s", "t") is False
    client.assert_not_called()
    monkeypatch.setenv("RESEND_API_KEY", "re_test")  # one of three is not enough
    assert notifications.notification_config() is None


@pytest.mark.asyncio
async def test_with_the_settings_it_posts_to_resend(monkeypatch):
    _clear(monkeypatch)
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    response = MagicMock(status_code=200, text="ok")
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.post = AsyncMock(return_value=response)
    with patch.object(notifications.httpx, "AsyncClient", return_value=client):
        assert await notifications.notify("Subject", "Body") is True
    args, kwargs = client.post.call_args
    assert args[0] == notifications.RESEND_ENDPOINT
    assert kwargs["headers"]["Authorization"] == "Bearer re_test"
    assert kwargs["json"] == {"from": ENV["NOTIFY_FROM_EMAIL"], "to": [ENV["NOTIFY_TO_EMAIL"]], "subject": "Subject", "text": "Body"}


@pytest.mark.asyncio
async def test_a_failure_is_logged_and_never_raised(monkeypatch):
    _clear(monkeypatch)
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    with patch.object(notifications.httpx, "AsyncClient", side_effect=RuntimeError("down")):
        assert await notifications.notify("s", "t") is False
    bad = MagicMock(status_code=422, text="invalid from")
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.post = AsyncMock(return_value=bad)
    with patch.object(notifications.httpx, "AsyncClient", return_value=client):
        assert await notifications.notify("s", "t") is False


def test_each_entry_point_notifies_after_storing():
    contact = (APP / "routers" / "contact.py").read_text()
    copyright_ = (APP / "routers" / "copyright.py").read_text()
    moderation = (APP / "routers" / "moderation.py").read_text()
    assert "background_tasks.add_task(notify, *contact_notification(" in contact
    assert "background_tasks.add_task(notify, *copyright_notification(" in copyright_
    assert "background_tasks.add_task(notify, *report_notification(" in moderation
    assert "TODO: Send notification to moderators" not in moderation
    # stored first, notified second
    assert contact.index("await store_contact_message(") < contact.index("background_tasks.add_task(notify")
    assert moderation.index('insert_one(report.model_dump') < moderation.index("background_tasks.add_task(notify")


def test_the_three_messages_carry_what_a_reader_needs():
    subject, body = notifications.contact_notification({"name": "Ana", "email": "ana@x.io", "message": "Hola", "locale": "es", "page": "/contact"})
    assert "Ana" in subject and "ana@x.io" in body and "Hola" in body
    subject, body = notifications.copyright_notification({"name": "Ana", "email": "a@x", "work": "Book", "location": "https://nowry.app/public/deck/1", "signature": "Ana R", "locale": "en"})
    assert "Copyright" in subject and "Book" in body and "/public/deck/1" in body and "Ana R" in body
    subject, body = notifications.report_notification({"reason": "copyright", "content_type": "deck", "content_title": "Pharm", "content_id": "1", "reporter_email": "r@x", "reporter_user_id": "u", "description": "mine"}, "rid")
    assert "copyright" in subject and "Pharm" in subject and "rid" in body and "mine" in body
