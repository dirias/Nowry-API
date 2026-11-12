"""
INVITE-002 — an invitation for one address: the row, the rules, the operator's calls
(ADR-077, docs/prd-road-to-market.md FR-019).

Pins:

  1. A token has the prefix, is long and random, and only its hash is stored.
  2. A new row: lowercased address, cohort, open, 14 days unless told otherwise.
  3. State at a moment: open, expired (derived), used and revoked outlast the expiry.
  4. The claim query: this token, this address, open and in time, or already
     spent by this same uid.
  5. Creating revokes the address's open invitation; the answer carries the
     link once and the stored row does not carry the token.
  6. Listing is bounded and filtered; counts are per cohort and state.
  7. Revoking closes an open invitation and leaves a used one as it is.
  8. The three routes are admin-gated and their answers have no token or hash.
"""
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

from tests._stubs import stub_if_missing

stub_if_missing("langfuse", "langfuse.langchain")
sys.modules.setdefault("app.models.agent_models", MagicMock())

import pytest
from bson import ObjectId
from pydantic import ValidationError

from app.config import invites as rules
from app.models.Beta import InviteCreate, InviteCreated, InviteSummary
from app.services import beta_invites
from tests._fake_collection import FakeCollection

NOW = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)
ADMIN = "65f0a1b2c3d4e5f6a7b8c9d0"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv("BETA_INVITE_TTL_DAYS", raising=False)
    monkeypatch.delenv("FRONTEND_URL", raising=False)


# ── 1. The token ─────────────────────────────────────────────────────────────


def test_a_token_is_prefixed_long_and_never_repeats():
    tokens = {rules.make_token() for _ in range(200)}
    assert len(tokens) == 200
    assert all(token.startswith("nwi_") and len(token) >= 47 for token in tokens)


def test_a_personal_token_is_told_from_a_cohort_code():
    assert rules.is_personal_token(rules.make_token())
    assert rules.is_personal_token("  nwi_abc ")
    for other in (None, "", "c0-7f3a", "NWI_abc", "friends"):
        assert not rules.is_personal_token(other)


def test_the_hash_is_sha256_of_the_trimmed_token_and_not_the_token():
    token = rules.make_token()
    hashed = rules.hash_token(token)
    assert len(hashed) == 64 and token not in hashed
    assert rules.hash_token(f"  {token} ") == hashed
    assert rules.hash_token(token + "x") != hashed


# ── 2. A new row ─────────────────────────────────────────────────────────────


def test_a_new_row_holds_the_hash_and_not_the_token():
    token = rules.make_token()
    row = rules.new_invite_document(" Ana@Example.COM ", "c1-waitlist", token, ADMIN, NOW, note="from the list")
    assert row["email"] == "ana@example.com"
    assert row["cohort"] == "c1-waitlist"
    assert row["status"] == "open"
    assert row["token_hash"] == rules.hash_token(token)
    assert row["expires_at"] == NOW + timedelta(days=14)
    assert row["created_by"] == ADMIN and row["note"] == "from the list"
    assert token not in repr(row)


@pytest.mark.parametrize("env, requested, expected", [(None, None, 14), ("7", None, 7), ("7", 30, 30), ("nonsense", None, 14), ("0", None, 1), ("400", None, 90), (None, 400, 90)])
def test_the_life_of_an_invitation(monkeypatch, env, requested, expected):
    if env is not None:
        monkeypatch.setenv("BETA_INVITE_TTL_DAYS", env)
    assert rules.ttl_days(requested) == expected


# ── 3. State ─────────────────────────────────────────────────────────────────


def _row(**changes):
    row = rules.new_invite_document("ana@example.com", "friends", rules.make_token(), ADMIN, NOW)
    row.update(changes)
    return row


def test_state_at_a_moment():
    assert rules.invite_state(_row(), NOW) == "open"
    assert rules.invite_state(_row(), NOW + timedelta(days=14)) == "expired"
    assert rules.invite_state(_row(), NOW + timedelta(days=13, hours=23)) == "open"
    assert rules.invite_state(_row(status="used"), NOW + timedelta(days=99)) == "used"
    assert rules.invite_state(_row(status="revoked"), NOW + timedelta(days=99)) == "revoked"


def test_a_date_read_back_without_a_zone_is_utc():
    naive = _row(expires_at=(NOW + timedelta(days=1)).replace(tzinfo=None))
    assert rules.invite_state(naive, NOW) == "open"
    assert rules.invite_state(naive, NOW + timedelta(days=2)) == "expired"


def test_only_an_open_row_is_usable():
    assert rules.is_usable(_row(), NOW)
    assert not rules.is_usable(None, NOW)
    assert not rules.is_usable(_row(status="used"), NOW)
    assert not rules.is_usable(_row(status="revoked"), NOW)
    assert not rules.is_usable(_row(), NOW + timedelta(days=15))


# ── 4. The claim ─────────────────────────────────────────────────────────────


async def _stored(collection, email="ana@example.com", **changes):
    row, token = await beta_invites.create_invite(email, "friends", ADMIN, collection=collection, now=NOW)
    if changes:
        await collection.update_one({"_id": row["_id"]}, {"$set": changes})
    return row, token


async def _claim(collection, token, email, uid, now=NOW):
    return await collection.find_one_and_update(rules.claim_query(token, email, uid, now), rules.claim_update(uid, now))


@pytest.mark.asyncio
async def test_the_claim_spends_an_open_invitation_for_its_address():
    collection = FakeCollection()
    _, token = await _stored(collection)
    claimed = await _claim(collection, token, "Ana@Example.com", "uid-ana")
    assert claimed["status"] == "used" and claimed["used_by_uid"] == "uid-ana" and claimed["used_at"] == NOW


@pytest.mark.asyncio
async def test_the_claim_refuses_everything_else():
    collection = FakeCollection()
    _, token = await _stored(collection)
    assert await _claim(collection, token, "eve@example.com", "uid-eve") is None
    assert await _claim(collection, rules.make_token(), "ana@example.com", "uid-ana") is None
    assert await _claim(collection, token, "ana@example.com", "uid-ana", NOW + timedelta(days=14)) is None
    assert collection.rows[0]["status"] == "open"

    _, revoked = await _stored(collection, email="bo@example.com", status="revoked")
    assert await _claim(collection, revoked, "bo@example.com", "uid-bo") is None


@pytest.mark.asyncio
async def test_a_spent_invitation_answers_its_own_uid_again_and_no_other():
    collection = FakeCollection()
    _, token = await _stored(collection)
    assert await _claim(collection, token, "ana@example.com", "uid-ana") is not None
    assert await _claim(collection, token, "ana@example.com", "uid-ana", NOW + timedelta(days=30)) is not None
    assert await _claim(collection, token, "ana@example.com", "uid-other") is None


@pytest.mark.asyncio
async def test_a_claim_is_released_only_before_an_account_is_tied_to_it():
    collection = FakeCollection()
    row, token = await _stored(collection)
    await _claim(collection, token, "ana@example.com", "uid-ana")
    await collection.update_one(rules.release_query(row["_id"], "uid-ana"), rules.RELEASE_UPDATE)
    assert collection.rows[0]["status"] == "open" and collection.rows[0]["used_by_uid"] is None

    await _claim(collection, token, "ana@example.com", "uid-ana")
    await collection.update_one({"_id": row["_id"]}, {"$set": {"used_by": "account-1"}})
    await collection.update_one(rules.release_query(row["_id"], "uid-ana"), rules.RELEASE_UPDATE)
    assert collection.rows[0]["status"] == "used"


# ── 5. Creating ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_creating_stores_the_hash_and_hands_the_token_back_once():
    collection = FakeCollection()
    row, token = await beta_invites.create_invite("Ana@Example.com", "c1-waitlist", ADMIN, collection=collection, now=NOW)
    assert rules.is_personal_token(token)
    assert collection.rows[0]["token_hash"] == rules.hash_token(token)
    assert token not in repr(collection.rows)
    assert row["_id"] == collection.rows[0]["_id"]


@pytest.mark.asyncio
async def test_a_new_invitation_revokes_the_open_one_for_that_address_only():
    collection = FakeCollection()
    first, first_token = await _stored(collection)
    await _stored(collection, email="bo@example.com")
    second, second_token = await beta_invites.create_invite("ANA@example.com", "friends", ADMIN, collection=collection, now=NOW + timedelta(hours=1))

    by_id = {row["_id"]: row for row in collection.rows}
    assert by_id[first["_id"]]["status"] == "revoked" and by_id[first["_id"]]["revoked_by"] == ADMIN
    assert by_id[second["_id"]]["status"] == "open"
    assert [row["status"] for row in collection.rows if row["email"] == "bo@example.com"] == ["open"]
    assert await _claim(collection, first_token, "ana@example.com", "uid-ana") is None
    assert await _claim(collection, second_token, "ana@example.com", "uid-ana", NOW + timedelta(hours=2)) is not None


@pytest.mark.asyncio
async def test_a_used_invitation_is_not_revoked_by_a_new_one():
    collection = FakeCollection()
    first, token = await _stored(collection)
    await _claim(collection, token, "ana@example.com", "uid-ana")
    await beta_invites.create_invite("ana@example.com", "friends", ADMIN, collection=collection, now=NOW)
    assert collection.rows[0]["status"] == "used"


def test_the_link_carries_the_token_and_never_the_address(monkeypatch):
    monkeypatch.setenv("FRONTEND_URL", "https://nowry.app/")
    token = rules.make_token()
    assert rules.invite_link(token) == f"https://nowry.app/register?invite={token}"


@pytest.mark.parametrize("field, value", [("email", "not-an-address"), ("cohort", ""), ("cohort", "Has Spaces"), ("cohort", "x" * 41), ("expires_in_days", 0), ("expires_in_days", 91), ("note", "n" * 201)])
def test_the_create_request_refuses_out_of_bounds_fields(field, value):
    with pytest.raises(ValidationError):
        InviteCreate(**{"email": "ana@example.com", "cohort": "friends", field: value})


# ── 6. Listing ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_listing_is_newest_first_filtered_and_bounded():
    collection = FakeCollection()
    for index in range(5):
        await beta_invites.create_invite(f"p{index}@example.com", "friends" if index % 2 else "c1-waitlist", ADMIN, collection=collection, now=NOW + timedelta(minutes=index))
    await collection.update_one({"email": "p0@example.com"}, {"$set": {"status": "used"}})
    await collection.update_one({"email": "p1@example.com"}, {"$set": {"expires_at": NOW - timedelta(days=1)}})
    later = NOW + timedelta(hours=1)

    everything = await beta_invites.list_invites(collection=collection, now=later)
    assert [row["email"] for row in everything] == [f"p{index}@example.com" for index in (4, 3, 2, 1, 0)]
    assert [row["email"] for row in await beta_invites.list_invites("open", collection=collection, now=later)] == ["p4@example.com", "p3@example.com", "p2@example.com"]
    assert [row["email"] for row in await beta_invites.list_invites("expired", collection=collection, now=later)] == ["p1@example.com"]
    assert [row["email"] for row in await beta_invites.list_invites("used", collection=collection, now=later)] == ["p0@example.com"]
    assert [row["email"] for row in await beta_invites.list_invites(cohort="friends", collection=collection, now=later)] == ["p3@example.com", "p1@example.com"]
    assert len(await beta_invites.list_invites(limit=2, collection=collection, now=later)) == 2
    assert len(await beta_invites.list_invites(limit=10_000, collection=collection, now=later)) == 5
    assert beta_invites.LIST_MAX == 200


@pytest.mark.asyncio
async def test_counts_are_per_cohort_and_state_with_zeroes_filled_in():
    collection = MagicMock()
    cursor = MagicMock()
    cursor.to_list = AsyncMock(return_value=[
        {"_id": {"cohort": "friends", "state": "open"}, "n": 3},
        {"_id": {"cohort": "friends", "state": "used"}, "n": 2},
        {"_id": {"cohort": "c1-waitlist", "state": "expired"}, "n": 1},
    ])
    collection.aggregate = MagicMock(return_value=cursor)
    counts = await beta_invites.count_invites_by_cohort(collection=collection, now=NOW)
    assert counts == {
        "friends": {"open": 3, "used": 2, "revoked": 0, "expired": 0},
        "c1-waitlist": {"open": 0, "used": 0, "revoked": 0, "expired": 1},
    }
    assert cursor.to_list.await_args.kwargs["length"] > 0


# ── 7. Revoking ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_revoking_closes_an_open_invitation():
    collection = FakeCollection()
    row, token = await _stored(collection)
    revoked = await beta_invites.revoke_invite(str(row["_id"]), ADMIN, collection=collection, now=NOW)
    assert revoked["status"] == "revoked" and revoked["revoked_at"] == NOW and revoked["revoked_by"] == ADMIN
    assert await _claim(collection, token, "ana@example.com", "uid-ana") is None


@pytest.mark.asyncio
async def test_revoking_a_used_invitation_changes_nothing():
    collection = FakeCollection()
    row, token = await _stored(collection)
    await _claim(collection, token, "ana@example.com", "uid-ana")
    answered = await beta_invites.revoke_invite(str(row["_id"]), ADMIN, collection=collection, now=NOW)
    assert answered["status"] == "used" and answered["revoked_at"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("invite_id", ["not-an-id", str(ObjectId())])
async def test_revoking_what_does_not_exist_answers_none(invite_id):
    assert await beta_invites.revoke_invite(invite_id, ADMIN, collection=FakeCollection(), now=NOW) is None


# ── 8. The routes ────────────────────────────────────────────────────────────


def test_every_invitation_route_is_admin_gated():
    from app.auth.dependencies import require_admin
    from app.routers.beta_admin import router

    assert len(router.routes) == 3
    for route in router.routes:
        assert require_admin in [dependency.call for dependency in route.dependant.dependencies], route.path


def test_what_the_operator_reads_has_no_token_and_no_hash():
    from app.routers.beta_admin import summary_fields

    token = rules.make_token()
    row = {**rules.new_invite_document("ana@example.com", "friends", token, ADMIN, NOW), "_id": ObjectId()}
    fields = summary_fields(row, NOW)
    assert "token_hash" not in fields and row["token_hash"] not in repr(fields)
    assert InviteSummary(**fields).state == "open"
    created = InviteCreated(**fields, link=rules.invite_link(token))
    assert created.link.endswith(token) and "ana" not in created.link
    assert set(InviteSummary.model_fields) == {"id", "email", "cohort", "state", "note", "created_at", "expires_at", "used_at", "revoked_at"}


@pytest.mark.asyncio
async def test_the_create_route_answers_the_link_and_records_who_asked(monkeypatch):
    from app.routers import beta_admin

    collection = FakeCollection()
    monkeypatch.setattr(beta_invites, "invites_collection", collection)
    monkeypatch.setenv("FRONTEND_URL", "https://nowry.app")
    answer = await beta_admin.create_invite(InviteCreate(email="Ana@Example.com", cohort="c1-waitlist", expires_in_days=7), admin={"user_id": ADMIN})
    assert answer.email == "ana@example.com" and answer.cohort == "c1-waitlist" and answer.state == "open"
    assert answer.link.startswith("https://nowry.app/register?invite=nwi_")
    assert collection.rows[0]["created_by"] == ADMIN
    assert collection.rows[0]["token_hash"] == rules.hash_token(answer.link.split("invite=")[1])


@pytest.mark.asyncio
async def test_the_revoke_route_answers_404_for_no_such_invitation(monkeypatch):
    from fastapi import HTTPException
    from app.routers import beta_admin

    monkeypatch.setattr(beta_invites, "invites_collection", FakeCollection())
    with pytest.raises(HTTPException) as missing:
        await beta_admin.revoke_invite(str(ObjectId()), admin={"user_id": ADMIN})
    assert missing.value.status_code == 404
