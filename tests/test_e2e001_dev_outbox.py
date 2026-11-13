"""E2E-001 (ADR-090): the dev-only outbox.

Every email the API renders is kept, off production only, and read only with
the key. Production never keeps or serves anything.
"""
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("mongomock_motor")

from tests._fake_world import fake_world  # noqa: E402

KEY = "k" * 32


@pytest.mark.parametrize(
    "env, flag, expected",
    [("development", "1", True), ("development", "", False), ("production", "1", False), ("prod", "true", False), ("", "1", True)],
)
def test_the_outbox_is_on_only_when_asked_and_never_in_production(monkeypatch, env, flag, expected):
    from app.services import dev_outbox

    monkeypatch.setenv("ENV", env)
    monkeypatch.setenv("DEV_OUTBOX", flag)
    assert dev_outbox.enabled() is expected


def _send(world, monkeypatch, env="development"):
    monkeypatch.setenv("ENV", env)
    monkeypatch.setenv("DEV_OUTBOX", "1")
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    monkeypatch.setenv("EMAIL_FROM_ACCOUNT", "Nowry <account@nowry.app>")
    monkeypatch.setenv("EMAIL_REPLY_TO", "support@nowry.app")
    from app.services import email

    with patch.object(email, "_post", AsyncMock(return_value=True)):
        return world.run(email.send_email("account.deleted", "rikydier+e2e@example.com", "es", {}))


def test_a_sent_email_is_kept_with_its_text_off_production(monkeypatch):
    with fake_world() as world:
        assert _send(world, monkeypatch) == "sent"
        row = world.run(world.raw["dev_outbox"].find_one({}))
        assert row["to"] == "rikydier+e2e@example.com" and row["kind"] == "account.deleted"
        assert "support@nowry.app" in row["text"] and row["outcome"] == "sent" and row["locale"] == "es"


def test_production_keeps_nothing(monkeypatch):
    with fake_world() as world:
        _send(world, monkeypatch, env="production")
        assert world.run(world.raw["dev_outbox"].count_documents({})) == 0


def test_the_outbox_is_read_only_with_the_key(monkeypatch):
    monkeypatch.setenv("DEV_OUTBOX_KEY", KEY)
    with fake_world() as world:
        _send(world, monkeypatch)
        from app.routers import dev_outbox as route
        from app.main import app

        if not any(getattr(r, "path", "") == "/dev/outbox" for r in app.routes):
            app.include_router(route.router)
        assert world.http.get("/dev/outbox").status_code == 404
        assert world.http.get("/dev/outbox", headers={"X-Dev-Outbox-Key": "wrong"}).status_code == 404
        response = world.http.get("/dev/outbox", params={"to": "RIKYDIER+e2e@example.com"}, headers={"X-Dev-Outbox-Key": KEY})
        assert response.status_code == 200, response.text
        items = response.json()["items"]
        assert len(items) == 1 and items[0]["kind"] == "account.deleted"
        monkeypatch.setenv("ENV", "production")
        assert world.http.get("/dev/outbox", headers={"X-Dev-Outbox-Key": KEY}).status_code == 404


def test_production_does_not_mount_the_route(monkeypatch):
    from app.services import dev_outbox

    monkeypatch.setenv("ENV", "production")
    monkeypatch.setenv("DEV_OUTBOX", "1")
    assert dev_outbox.should_mount() is False
