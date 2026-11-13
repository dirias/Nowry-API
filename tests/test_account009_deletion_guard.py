"""ACCOUNT-009..012 (ADR-089, FR-038..041): deletion needs a fresh sign-in and
sends one notice; a re-registration within 30 days is told once, after the
address is verified.
"""
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("mongomock_motor")

from tests._fake_world import fake_world  # noqa: E402

FIREBASE = ("firebase_admin.auth.revoke_refresh_tokens", "firebase_admin.auth.delete_user")


@pytest.fixture
def world():
    with fake_world() as w:
        uid = w.new_user("ana", email="ana@example.com", email_verified=True, preferences={"general": {"language": "fr"}})
        w.act_as(uid)
        w.uid = uid
        yield w


def _delete(world, path="/users/account", sender=None):
    sender = sender or AsyncMock(return_value="sent")
    with patch(FIREBASE[0]), patch(FIREBASE[1]), patch("app.services.account_deletion.send_email", sender):
        response = world.http.delete(path)
    return response, sender


def _doc(world, uid):
    return world.run(world.raw["users"].find_one({"_id": uid}))


# ── FR-038: a fresh sign-in ─────────────────────────────────────────────────

def test_a_sign_in_older_than_five_minutes_is_refused_and_nothing_is_deleted(world):
    world.current["user"]["auth_time"] = int(time.time()) - 301
    response, sender = _delete(world)
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "reauth_required"
    assert _doc(world, world.uid).get("deleted_at") is None
    sender.assert_not_awaited()


def test_a_token_without_a_sign_in_time_is_refused(world):
    world.current["user"].pop("auth_time")
    response, _ = _delete(world)
    assert response.status_code == 401


def test_after_signing_in_again_the_deletion_succeeds(world):
    world.current["user"]["auth_time"] = int(time.time()) - 600
    assert _delete(world)[0].status_code == 401
    world.current["user"]["auth_time"] = int(time.time())  # the re-authentication
    response, _ = _delete(world)
    assert response.status_code == 200, response.text
    assert _doc(world, world.uid)["deleted_at"] is not None


# ── FR-039: the notice email ────────────────────────────────────────────────

def test_the_notice_goes_once_to_the_verified_address_in_the_clients_language(world):
    response, sender = _delete(world, "/users/account?locale=es")
    assert response.status_code == 200
    sender.assert_awaited_once()
    kind, to, locale, _values = sender.await_args.args
    assert (kind, to, locale) == ("account.deleted", "ana@example.com", "es")
    assert _doc(world, world.uid)["deletion_notice"]["outcome"] == "sent"
    again, sender_again = _delete(world)
    assert again.status_code in (401, 404)  # the account is gone; nothing is sent again
    sender_again.assert_not_awaited()


def test_without_a_client_language_the_stored_one_is_used(world):
    _, sender = _delete(world)
    assert sender.await_args.args[2] == "fr"


def test_an_unverified_address_is_sent_nothing(world):
    world.run(world.raw["users"].update_one({"_id": world.uid}, {"$set": {"email_verified": False}}))
    response, sender = _delete(world)
    assert response.status_code == 200
    sender.assert_not_awaited()
    assert _doc(world, world.uid)["deletion_notice"]["outcome"] == "unverified"


def test_the_notice_renders_in_five_languages_with_the_support_address():
    from app.emails.render import KINDS, render_email

    assert KINDS["account.deleted"] == {"class": "account", "sender": "account", "button": "none"}
    for locale in ("en", "es", "fr", "de", "ja"):
        rendered = render_email("account.deleted", locale, {"email": "a@example.com", "support": "support@nowry.app"}, logo_url="https://x/l.png")
        assert "support@nowry.app" in rendered.text and "support@nowry.app" in rendered.html
        assert rendered.locale == locale and rendered.subject


# ── FR-041: the re-registration notice ──────────────────────────────────────

def _reregister(world, deleted_days_ago: float, verified: bool = True):
    """Ana deletes, then the same address makes a new account."""
    deleted_at = datetime.now(timezone.utc) - timedelta(days=deleted_days_ago)
    _delete(world)
    world.run(world.raw["users"].update_one({"_id": world.uid}, {"$set": {"deleted_at": deleted_at}}))
    new = world.new_user("ana2", email="Ana@Example.com", email_verified=verified, created_at=datetime.now(timezone.utc))
    world.act_as(new)
    return new, deleted_at


def test_a_verified_new_account_within_30_days_is_told(world):
    _, deleted_at = _reregister(world, 3)
    body = world.http.get("/users/me").json()
    previous = body["previous_account"]
    assert previous["deleted_at"][:10] == deleted_at.date().isoformat()
    assert previous["erased_at"][:10] == (deleted_at + timedelta(days=30)).date().isoformat()


def test_after_30_days_nothing_is_said(world):
    _reregister(world, 30.5)
    assert world.http.get("/users/me").json().get("previous_account") is None


def test_before_the_address_is_verified_nothing_is_said(world):
    _reregister(world, 3, verified=False)
    assert world.http.get("/users/me").json().get("previous_account") is None


def test_once_dismissed_it_is_not_said_again(world):
    _reregister(world, 3)
    assert world.http.post("/users/me/notices/previous-account/dismiss").status_code == 204
    assert world.http.get("/users/me").json().get("previous_account") is None


def test_an_account_that_existed_before_the_deletion_is_not_told(world):
    other = world.new_user("older", email="ana@example.com", email_verified=True,
                           created_at=datetime.now(timezone.utc) - timedelta(days=40))
    _delete(world)
    world.act_as(other)
    assert world.http.get("/users/me").json().get("previous_account") is None
