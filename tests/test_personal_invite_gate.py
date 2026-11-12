"""
INVITE-003 — the gate spends a personal invitation, for its verified address only
(ADR-077, docs/prd-road-to-market.md FR-019).

The real ``get_firebase_user`` runs over the in-memory database, with
Firebase's answer for the token supplied, so the claim is a real query.

Pins:

  1. Unverified: one answer (`invite_verify_email`) whatever the token, and
     nothing is written or spent.
  2. Verified, the invited address: the account exists, stamped with the
     invitation's cohort and id, with no token or code on it; the invitation is
     used and tied to the account.
  3. Verified, anything else (second use, another address, expired, revoked,
     unknown): the refusal a missing code gets, identical in every case.
  4. The two requests of one sign-up do not refuse each other.
  5. An account that could not be written gives the invitation back.
  6. Cohort codes still work; an existing account is never asked.
  7. The public check says usable or not, with no cohort and no reason.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from starlette.requests import Request

pytest.importorskip("mongomock_motor")

from app.config import invites as rules  # noqa: E402
from app.config.beta import BETA_INVITE_REQUIRED_CODE  # noqa: E402
from app.models.Beta import InviteCheckRequest  # noqa: E402
from app.services import beta_invites  # noqa: E402
from app.services.beta_invites import INVITE_VERIFY_EMAIL_CODE  # noqa: E402
from tests._fake_world import fake_world  # noqa: E402
from tests._stubs import fake_request  # noqa: E402

ADMIN = "65f0a1b2c3d4e5f6a7b8c9d0"
ANA = "ana@example.com"


@pytest.fixture
def world(monkeypatch):
    for name in ("BETA_ACTIVE", "BETA_UPGRADES_OPEN", "BETA_CODE_MAX_USES", "BETA_CODE_EXPIRES", "BETA_INVITE_TTL_DAYS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BETA_INVITE_REQUIRED", "true")
    monkeypatch.setenv("BETA_INVITE_CODES", "friends:c0-7f3a")
    with fake_world() as w:
        yield w


def decoded(uid="fb-ana", email=ANA, verified=True, provider="password"):
    return {"uid": uid, "email": email, "email_verified": verified, "auth_time": 100, "firebase": {"sign_in_provider": provider}}


def sign_in(world, token_claims, invite=None):
    import app.auth.firebase_auth as firebase_auth

    headers = [(b"authorization", b"Bearer t")]
    if invite is not None:
        headers.append((b"x-invite-code", invite.encode()))
    request = Request({"type": "http", "headers": headers, "query_string": b""})
    with patch.object(firebase_auth, "verify_firebase_token", return_value=token_claims), \
         patch.object(firebase_auth, "_get_cached_token", return_value=None), \
         patch.object(firebase_auth, "_cache_token"), \
         patch.object(firebase_auth._stripe.Customer, "create", side_effect=RuntimeError("no Stripe in tests")):
        return world.run(firebase_auth.get_firebase_user(request))


def refusal(world, token_claims, invite=None) -> HTTPException:
    with pytest.raises(HTTPException) as refused:
        sign_in(world, token_claims, invite)
    return refused.value


def invite(world, email=ANA, cohort="c1-waitlist", **changes) -> str:
    row, token = world.run(beta_invites.create_invite(email, cohort, ADMIN))
    if changes:
        world.run(world.raw["invites"].update_one({"_id": row["_id"]}, {"$set": changes}))
    return token


def invites(world) -> list:
    return world.run(world.raw["invites"].find({}).to_list(length=50))


def accounts(world) -> list:
    return world.run(world.raw["users"].find({}).to_list(length=50))


# ── 1. Unverified ────────────────────────────────────────────────────────────


def test_an_unverified_address_gets_one_answer_whatever_the_token(world):
    good = invite(world)
    used = invite(world, email="bo@example.com", status="used", used_by_uid="fb-bo")
    old = invite(world, email="cy@example.com", expires_at=datetime.now(timezone.utc) - timedelta(days=1))
    before = invites(world)

    answers = [refusal(world, decoded(verified=False), token) for token in (good, used, old, rules.make_token())]
    answers.append(refusal(world, decoded(email="eve@example.com", uid="fb-eve", verified=False), good))

    assert {(a.status_code, str(a.detail)) for a in answers} == {(403, str({"code": INVITE_VERIFY_EMAIL_CODE}))}
    assert accounts(world) == []
    assert invites(world) == before


# ── 2. The invited, verified address ─────────────────────────────────────────


def test_the_verified_invited_address_gets_an_account_stamped_with_the_cohort(world):
    token = invite(world, cohort="c1-waitlist")
    resolved = sign_in(world, decoded(), token)

    [account] = accounts(world)
    [row] = invites(world)
    assert str(account["_id"]) == resolved["user_id"]
    assert account["beta"]["cohort"] == "c1-waitlist"
    assert account["beta"]["invite_id"] == str(row["_id"])
    assert set(account["beta"]) == {"cohort", "invite_id", "joined_at"}
    assert token not in repr(account) and row["token_hash"] not in repr(account)
    assert row["status"] == "used" and row["used_by"] == resolved["user_id"] and row["used_by_uid"] == "fb-ana"


def test_a_google_sign_in_with_the_invited_address_goes_straight_through(world):
    token = invite(world)
    sign_in(world, decoded(provider="google.com"), token)
    assert [a["signup_provider"] for a in accounts(world)] == ["google.com"]


def test_the_address_compares_without_case(world):
    token = invite(world, email="Ana@Example.com")
    sign_in(world, decoded(email="ANA@example.COM"), token)
    assert len(accounts(world)) == 1


def test_confirming_the_email_turns_the_refusal_into_an_account(world):
    token = invite(world)
    assert refusal(world, decoded(verified=False), token).detail == {"code": INVITE_VERIFY_EMAIL_CODE}
    sign_in(world, decoded(verified=True), token)
    assert len(accounts(world)) == 1 and invites(world)[0]["status"] == "used"


# ── 3. Everything else, verified ─────────────────────────────────────────────


def test_every_failure_is_the_refusal_a_missing_code_gets(world):
    # Spent by another Firebase identity of the same address (the account it made is gone).
    spent = invite(world, email="bo@example.com", status="used", used_by_uid="fb-bo")
    for_ana = invite(world)
    old = invite(world, email="cy@example.com", expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    revoked = invite(world, email="di@example.com", status="revoked")
    rows_before, accounts_before = invites(world), accounts(world)

    answers = [
        refusal(world, decoded(uid="fb-bo2", email="bo@example.com"), spent),
        refusal(world, decoded(uid="fb-eve", email="eve@example.com"), for_ana),
        refusal(world, decoded(uid="fb-cy", email="cy@example.com"), old),
        refusal(world, decoded(uid="fb-di", email="di@example.com"), revoked),
        refusal(world, decoded(uid="fb-x", email="x@example.com"), rules.make_token()),
        refusal(world, decoded(uid="fb-x", email="x@example.com"), None),
        refusal(world, decoded(uid="fb-x", email="x@example.com"), "not-a-code"),
    ]

    assert {(a.status_code, str(a.detail)) for a in answers} == {(403, str({"code": BETA_INVITE_REQUIRED_CODE}))}
    assert accounts(world) == accounts_before
    assert invites(world) == rows_before


def test_a_token_works_once(world):
    token = invite(world)
    sign_in(world, decoded(), token)
    world.run(world.raw["users"].delete_many({}))
    assert refusal(world, decoded(uid="fb-ana-again"), token).detail == {"code": BETA_INVITE_REQUIRED_CODE}


def test_a_new_invitation_closes_the_earlier_link(world):
    first = invite(world)
    second = invite(world)
    assert refusal(world, decoded(), first).detail == {"code": BETA_INVITE_REQUIRED_CODE}
    sign_in(world, decoded(), second)
    assert len(accounts(world)) == 1


# ── 4. One sign-up, two requests ─────────────────────────────────────────────


def test_the_second_request_of_the_same_sign_up_is_not_refused(world):
    token = invite(world)
    first = sign_in(world, decoded(), token)
    second = sign_in(world, decoded(), token)
    assert first["user_id"] == second["user_id"]
    assert len(accounts(world)) == 1


def test_a_claim_made_before_the_account_exists_is_honoured_for_the_same_uid(world):
    """Request A has claimed and not yet written; request B, same uid, arrives."""
    token = invite(world)
    world.run(beta_invites.spend_personal_invite(token, {"firebase_uid": "fb-ana", "email": ANA, "email_verified": True}))
    assert invites(world)[0]["status"] == "used" and accounts(world) == []
    sign_in(world, decoded(), token)
    assert len(accounts(world)) == 1 and invites(world)[0]["used_by"] is not None


# ── 5. A failed write gives the invitation back ──────────────────────────────


def test_an_account_that_could_not_be_written_releases_the_invitation(world):
    import app.auth.firebase_auth as firebase_auth
    from app.config import database

    token = invite(world)
    with patch.object(database.users_collection._inner, "insert_one", AsyncMock(side_effect=RuntimeError("write failed"))):
        failed = refusal(world, decoded(), token)
    assert failed.status_code == 500
    [row] = invites(world)
    assert row["status"] == "open" and row["used_by_uid"] is None and row["used_at"] is None

    sign_in(world, decoded(), token)
    assert len(accounts(world)) == 1 and invites(world)[0]["status"] == "used"
    assert firebase_auth is not None


def test_releasing_never_raises(world):
    class Broken:
        async def update_one(self, *args, **kwargs):
            raise RuntimeError("database unreachable")

    world.run(beta_invites.release_personal_invite({"invite_id": "65f0a1b2c3d4e5f6a7b8c9d0"}, "fb-ana", collection=Broken()))
    world.run(beta_invites.release_personal_invite(None, "fb-ana", collection=Broken()))
    world.run(beta_invites.release_personal_invite({"cohort": "friends", "invite_code": "c0"}, "fb-ana", collection=Broken()))


# ── 6. What did not change ───────────────────────────────────────────────────


def test_a_cohort_code_still_makes_an_account_verified_or_not(world):
    sign_in(world, decoded(verified=False), "C0-7F3A")
    [account] = accounts(world)
    assert account["beta"]["cohort"] == "friends" and account["beta"]["invite_code"] == "c0-7f3a"
    assert invites(world) == []


def test_an_existing_account_is_never_asked(world):
    uid = world.new_user("old")
    account = world.run(world.raw["users"].find_one({"_id": uid}))
    resolved = sign_in(world, decoded(uid=account["firebase_uid"], email=account["email"], verified=False))
    assert resolved["user_id"] == str(uid)


def test_with_the_gate_off_a_token_is_neither_needed_nor_spent(world, monkeypatch):
    monkeypatch.setenv("BETA_INVITE_REQUIRED", "false")
    token = invite(world)
    sign_in(world, decoded(verified=False), token)
    assert accounts(world)[0]["beta"] is None
    assert invites(world)[0]["status"] == "open"


# ── 7. The public check ──────────────────────────────────────────────────────


def test_the_check_says_usable_or_not_and_nothing_else(world):
    from app.routers import beta as beta_router

    def check(code):
        return world.run(beta_router.check_invite(fake_request(), InviteCheckRequest(code=code))).model_dump()

    good = invite(world)
    spent = invite(world, email="bo@example.com", status="used")
    old = invite(world, email="cy@example.com", expires_at=datetime.now(timezone.utc) - timedelta(days=1))
    revoked = invite(world, email="di@example.com", status="revoked")

    assert check(good) == {"valid": True, "cohort": None}
    closed = [check(token) for token in (spent, old, revoked, rules.make_token())]
    assert closed == [{"valid": False, "cohort": None}] * 4
    assert invites(world)[0]["status"] == "open", "checking spends nothing"
