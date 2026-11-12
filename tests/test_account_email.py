"""MAIL-006 and MAIL-007 — verification and reset written by Nowry (FR-023, FR-024, ADR-078)."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers import account_email as routes
from app.services import account_email as service
from app.utils import keyed_rate_limit

FIREBASE_LINK = "https://nowry-prod.firebaseapp.com/__/auth/action?mode=verifyEmail&oobCode=AB%2Bc_1&apiKey=k&lang=en"
OURS = "https://nowry.app/auth/action?mode=verifyEmail&oobCode=AB%2Bc_1"


def _user(*providers, disabled=False):
    return SimpleNamespace(disabled=disabled, provider_data=[SimpleNamespace(provider_id=name) for name in providers])


@pytest.fixture
def quiet(monkeypatch):
    """No notice, no stored counter, a known site address."""
    monkeypatch.setenv("FRONTEND_URL", "https://nowry.app/")
    notice = AsyncMock()
    monkeypatch.setattr(service, "notify", notice)
    monkeypatch.setattr(service, "seconds_to_wait", AsyncMock(return_value=0))
    return notice


@pytest.fixture
def client():
    api = FastAPI()
    api.include_router(routes.router)
    return TestClient(api)


# --- the link ----------------------------------------------------------------

def test_the_code_moves_onto_nowrys_page_and_nothing_else_comes_with_it():
    assert service.action_link(FIREBASE_LINK, "verifyEmail", "https://nowry.app") == OURS
    assert "apiKey" not in OURS and "firebaseapp" not in OURS
    with pytest.raises(ValueError):
        service.action_link("https://x.firebaseapp.com/__/auth/action?mode=verifyEmail", "verifyEmail", "https://nowry.app")


def test_a_limit_is_counted_on_a_hash_of_the_address():
    key = service.address_key(" Ana@Example.com ")
    assert key == service.address_key("ana@example.com") and "ana" not in key and len(key) == 32


# --- the stored counter ------------------------------------------------------

async def test_the_counter_lets_the_limit_through_then_says_how_long_to_wait(monkeypatch):
    collection = MagicMock(find_one_and_update=AsyncMock(return_value={"count": 1}))
    monkeypatch.setattr(keyed_rate_limit, "rate_limits_collection", collection)
    assert await keyed_rate_limit.seconds_to_wait("f", "k", 1, 60) == 0
    collection.find_one_and_update.return_value = {"count": 2}
    assert 1 <= await keyed_rate_limit.seconds_to_wait("f", "k", 1, 60) <= 60
    query = collection.find_one_and_update.await_args.args[0]
    assert query["_id"].startswith("f:k:")
    collection.find_one_and_update.side_effect = RuntimeError("no database")
    assert await keyed_rate_limit.seconds_to_wait("f", "k", 1, 60) == 60


async def test_the_longest_of_several_limits_is_the_wait(monkeypatch):
    waits = iter([0, 40, 7])
    monkeypatch.setattr(keyed_rate_limit, "seconds_to_wait", AsyncMock(side_effect=lambda *a: next(waits)))
    assert await keyed_rate_limit.longest_wait([("a", "k", 1, 60), ("b", "k", 5, 86400), ("c", "ip", 10, 3600)]) == 40


async def test_verification_and_reset_each_take_three_limits(monkeypatch):
    seen = AsyncMock(return_value=0)
    monkeypatch.setattr(service, "longest_wait", seen)
    await service.verification_wait("uid1", "1.2.3.4")
    await service.reset_wait("Ana@Example.com", "1.2.3.4")
    verify, reset = (call.args[0] for call in seen.await_args_list)
    assert [(f, k, n, w) for f, k, n, w in verify] == [("email_verify_minute", "uid1", 1, 60), ("email_verify_day", "uid1", 5, 86400), ("email_verify_ip", "1.2.3.4", 10, 3600)]
    key = service.address_key("ana@example.com")
    assert reset == [("email_reset_hour", key, 3, 3600), ("email_reset_day", key, 5, 86400), ("email_reset_ip", "1.2.3.4", 5, 3600)]


# --- verification ------------------------------------------------------------

async def test_verification_goes_out_on_the_layout_with_a_nowry_link(quiet):
    send = AsyncMock(return_value="sent")
    with patch.object(service.firebase_admin_auth, "generate_email_verification_link", return_value=FIREBASE_LINK) as make, \
            patch.object(service.email_service, "send_email", send):
        assert await service.send_verification("ana@example.com", "es") == "sent"
    make.assert_called_once_with("ana@example.com")
    send.assert_awaited_once_with("account.verify", "ana@example.com", "es", {"link": OURS})
    quiet.assert_not_awaited()


@pytest.mark.parametrize("outcome", ["failed", "over_budget", "not_allowed"])
async def test_when_ours_cannot_go_the_client_is_told_to_ask_firebase_and_the_operator_hears(quiet, outcome):
    with patch.object(service.firebase_admin_auth, "generate_email_verification_link", return_value=FIREBASE_LINK), \
            patch.object(service.email_service, "send_email", AsyncMock(return_value=outcome)):
        assert await service.send_verification("ana@example.com", "en") == "fallback"
    quiet.assert_awaited_once()
    assert "ana@example.com" not in str(quiet.await_args)


async def test_no_link_from_firebase_is_a_fallback_and_nothing_set_up_is_not_news(quiet):
    with patch.object(service.firebase_admin_auth, "generate_email_verification_link", side_effect=RuntimeError("quota")), \
            patch.object(service.email_service, "send_email", AsyncMock()) as send:
        assert await service.send_verification("ana@example.com", "en") == "fallback"
    send.assert_not_awaited()
    assert quiet.await_count == 1
    with patch.object(service.firebase_admin_auth, "generate_email_verification_link", return_value=FIREBASE_LINK), \
            patch.object(service.email_service, "send_email", AsyncMock(return_value="unconfigured")):
        assert await service.send_verification("ana@example.com", "en") == "fallback"
    assert quiet.await_count == 1


async def test_the_operator_hears_of_a_fallback_once_an_hour(quiet, monkeypatch):
    monkeypatch.setattr(service, "seconds_to_wait", AsyncMock(return_value=1800))
    await service._note_fallback("account.reset", "failed")
    quiet.assert_not_awaited()


# --- reset -------------------------------------------------------------------

async def test_only_an_address_with_a_password_is_sent_a_reset(quiet):
    send = AsyncMock(return_value="sent")
    make = MagicMock(return_value=FIREBASE_LINK.replace("verifyEmail", "resetPassword"))
    cases = {
        "nobody": service.firebase_admin_auth.UserNotFoundError("none"),
        "google only": _user("google.com"),
        "disabled": _user("password", disabled=True),
        "lookup down": RuntimeError("network"),
    }
    for name, answer in cases.items():
        lookup = MagicMock(side_effect=answer) if isinstance(answer, Exception) else MagicMock(return_value=answer)
        with patch.object(service.firebase_admin_auth, "get_user_by_email", lookup), \
                patch.object(service.firebase_admin_auth, "generate_password_reset_link", make), \
                patch.object(service.email_service, "send_email", send):
            await service.send_reset("ana@example.com", "es")
        assert not make.called and not send.await_count, name
    with patch.object(service.firebase_admin_auth, "get_user_by_email", MagicMock(return_value=_user("google.com", "password"))), \
            patch.object(service.firebase_admin_auth, "generate_password_reset_link", make), \
            patch.object(service.email_service, "send_email", send):
        await service.send_reset("ana@example.com", "es")
    send.assert_awaited_once_with("account.reset", "ana@example.com", "es", {"link": OURS.replace("verifyEmail", "resetPassword")})


@pytest.mark.parametrize("fault", ["send", "link"])
async def test_a_reset_we_cannot_send_is_sent_by_firebase_from_here(quiet, fault):
    make = MagicMock(side_effect=RuntimeError("quota")) if fault == "link" else MagicMock(return_value=FIREBASE_LINK)
    firebase = AsyncMock(return_value=True)
    with patch.object(service.firebase_admin_auth, "get_user_by_email", MagicMock(return_value=_user("password"))), \
            patch.object(service.firebase_admin_auth, "generate_password_reset_link", make), \
            patch.object(service.email_service, "send_email", AsyncMock(return_value="failed")), \
            patch.object(service, "firebase_sends_reset", firebase):
        await service.send_reset("ana@example.com", "ja-JP")
    firebase.assert_awaited_once_with("ana@example.com", "ja-JP")
    quiet.assert_awaited_once()


async def test_firebases_own_reset_needs_the_web_key_and_carries_the_language(monkeypatch):
    monkeypatch.delenv("FIREBASE_WEB_API_KEY", raising=False)
    with patch.object(service.httpx, "AsyncClient") as never:
        assert await service.firebase_sends_reset("ana@example.com", "es") is False
    never.assert_not_called()
    monkeypatch.setenv("FIREBASE_WEB_API_KEY", "web-key")
    http = MagicMock()
    http.__aenter__ = AsyncMock(return_value=http)
    http.__aexit__ = AsyncMock(return_value=False)
    http.post = AsyncMock(return_value=MagicMock(status_code=200))
    with patch.object(service.httpx, "AsyncClient", return_value=http):
        assert await service.firebase_sends_reset("ana@example.com", "es-MX") is True
    args, kwargs = http.post.call_args
    assert args[0] == service.FIREBASE_SEND_CODE and kwargs["params"] == {"key": "web-key"}
    assert kwargs["json"] == {"requestType": "PASSWORD_RESET", "email": "ana@example.com"}
    assert kwargs["headers"] == {"X-Firebase-Locale": "es"}


# --- the routes --------------------------------------------------------------

def test_the_verification_route_needs_firebases_word_and_no_account(client, monkeypatch):
    assert client.post("/auth/email/verification", json={}).status_code == 401
    monkeypatch.setattr(routes, "verify_firebase_token", MagicMock(side_effect=ValueError("bad")))
    assert client.post("/auth/email/verification", json={}, headers={"Authorization": "Bearer x"}).status_code == 401


def test_the_verification_route_answers_verified_wait_sent_or_fallback(client, monkeypatch):
    headers = {"Authorization": "Bearer t", "X-Forwarded-For": "9.9.9.9"}
    token = {"uid": "u1", "email": "ana@example.com", "email_verified": True}
    monkeypatch.setattr(routes, "verify_firebase_token", lambda _: token)
    wait, send = AsyncMock(return_value=0), AsyncMock(return_value="sent")
    monkeypatch.setattr(routes.account_email, "verification_wait", wait)
    monkeypatch.setattr(routes.account_email, "send_verification", send)
    assert client.post("/auth/email/verification", json={}, headers=headers).json() == {"status": "verified", "retry_after": None}
    send.assert_not_awaited()
    token["email_verified"] = False
    wait.return_value = 42
    assert client.post("/auth/email/verification", json={}, headers=headers).json() == {"status": "wait", "retry_after": 42}
    send.assert_not_awaited()
    wait.return_value = 0
    assert client.post("/auth/email/verification", json={"language": "es"}, headers=headers).json()["status"] == "sent"
    send.assert_awaited_once_with("ana@example.com", "es")
    assert wait.await_args.args == ("u1", "9.9.9.9")
    send.return_value = "fallback"
    assert client.post("/auth/email/verification", json={}, headers=headers).json()["status"] == "fallback"


def test_a_reset_is_answered_the_same_whatever_the_address_and_past_the_limit(client, monkeypatch):
    wait, send = AsyncMock(return_value=0), AsyncMock()
    monkeypatch.setattr(routes.account_email, "reset_wait", wait)
    monkeypatch.setattr(routes.account_email, "send_reset", send)
    first = client.post("/auth/email/password-reset", json={"email": "ana@example.com", "language": "es"})
    send.assert_awaited_once_with("ana@example.com", "es")
    wait.return_value = 3000  # past the limit: nothing is looked up or sent
    limited = client.post("/auth/email/password-reset", json={"email": "nobody@example.com"})
    assert send.await_count == 1
    for answer in (first, limited):
        assert (answer.status_code, answer.json()) == (202, {"message": routes.RESET_ACK})
    assert client.post("/auth/email/password-reset", json={"email": "not-an-address"}).status_code == 422
