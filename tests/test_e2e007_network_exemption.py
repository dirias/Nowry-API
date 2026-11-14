"""E2E-007 (ADR-090 amended, FR-042): on development, a verification request that
carries the valid outbox key for a suite alias is exempt from the per-network
limit. The per-account limits stay for everyone; a non-suite address is limited
as before, key or not; production has no outbox, so the exemption cannot apply.
"""
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.services import account_email, dev_outbox

KEY = "k" * 32
SUITE = "rikydier+e2e-abc123-verify@gmail.com"


@pytest.fixture
def development(monkeypatch):
    monkeypatch.setenv("ENV", "development")
    monkeypatch.setenv("DEV_OUTBOX", "1")
    monkeypatch.setenv("DEV_OUTBOX_KEY", KEY)


@pytest.mark.parametrize(
    "key, address, expected",
    [
        (KEY, SUITE, True),
        (KEY, "rikydier+manual@gmail.com", False),
        (KEY, "ana@example.com", False),
        ("wrong", SUITE, False),
        (None, SUITE, False),
    ],
)
def test_only_a_suite_alias_with_the_key_is_exempt(development, key, address, expected):
    assert dev_outbox.suite_request(key, address) is expected


def test_production_never_exempts(monkeypatch):
    monkeypatch.setenv("ENV", "production")
    monkeypatch.setenv("DEV_OUTBOX", "1")
    monkeypatch.setenv("DEV_OUTBOX_KEY", KEY)
    assert dev_outbox.suite_request(KEY, SUITE) is False


def test_a_short_or_missing_server_key_exempts_nothing(monkeypatch):
    monkeypatch.setenv("ENV", "development")
    monkeypatch.setenv("DEV_OUTBOX", "1")
    monkeypatch.setenv("DEV_OUTBOX_KEY", "short")
    assert dev_outbox.suite_request("short", SUITE) is False


@pytest.mark.asyncio
async def test_the_exemption_drops_only_the_network_check():
    seen = []

    async def longest(checks):
        seen.append([name for name, *_ in checks])
        return 0

    with patch.object(account_email, "longest_wait", longest):
        await account_email.verification_wait("uid-1", "203.0.113.9")
        await account_email.verification_wait("uid-1", "203.0.113.9", exempt_network=True)
    assert seen[0] == ["email_verify_minute", "email_verify_day", "email_verify_ip"]
    assert seen[1] == ["email_verify_minute", "email_verify_day"]


def _client(identity):
    from app.main import app
    from app.routers import account_email as route

    app.dependency_overrides[route.firebase_identity] = lambda: identity
    return app, TestClient(app)


@pytest.mark.parametrize(
    "email, header, exempt",
    [(SUITE, KEY, True), (SUITE, None, False), ("rikydier+manual@gmail.com", KEY, False)],
)
def test_the_route_asks_for_the_exemption_only_when_it_applies(development, email, header, exempt):
    from app.routers import account_email as route

    app, client = _client({"uid": "u1", "email": email, "email_verified": False})
    wait = AsyncMock(return_value=0)
    try:
        with patch.object(route.account_email, "verification_wait", wait), \
             patch.object(route.account_email, "send_verification", AsyncMock(return_value="sent")):
            headers = {"X-Dev-Outbox-Key": header} if header else {}
            response = client.post("/auth/email/verification", json={"language": "en"}, headers=headers)
    finally:
        app.dependency_overrides.pop(route.firebase_identity, None)
    assert response.status_code == 200, response.text
    assert wait.await_args.kwargs.get("exempt_network", False) is exempt
