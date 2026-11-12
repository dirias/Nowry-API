"""
ACCOUNT-001..005 — deleting an account erases all of it after 30 days and
releases its identity at once (docs/prd-road-to-market.md FR-029, ADR-081).

  1. The list: every collection that holds an account id is marked, expired,
     or handled otherwise on purpose.
  2. Deletion marks every row the account owns, in every collection, and
     nobody else's; it removes tokens, board seats and the Stripe customer.
  3. The identity is released: the same address signs up again the same day
     as a new account that sees nothing old, and a deleted record is never read.
  4. The expiry index exists on every listed collection, the account included.
"""
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from bson import ObjectId
from starlette.requests import Request

pytest.importorskip("mongomock_motor")

from app.services import account_deletion  # noqa: E402
from tests._fake_world import fake_world  # noqa: E402

APP = Path(__file__).resolve().parents[1] / "app"
NOW = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def world():
    with fake_world() as w:
        uid = w.new_user("ana", stripe_customer_id="cus_ana")
        w.act_as(uid)
        w.uid = uid
        yield w


def _seed(world, uid: ObjectId) -> dict:
    """One row in every owned collection, and a plan with its children."""
    user_id = str(uid)
    ids = {}
    for name, owner in account_deletion.OWNED:
        if name == "annual_plans":
            continue
        ids[name] = world.run(world.raw[name].insert_one({owner: user_id, "deleted_at": None})).inserted_id
    plan = world.run(world.raw["annual_plans"].insert_one({"user_id": user_id, "deleted_at": None})).inserted_id
    area = world.run(world.raw["focus_areas"].insert_one({"annual_plan_id": str(plan)})).inserted_id
    goal = world.run(world.raw["goals"].insert_one({"focus_area_id": str(area), "deleted_at": None})).inserted_id
    ids["annual_plans"] = plan
    ids["focus_areas"] = area
    ids["goals"] = goal
    ids["priorities"] = world.run(world.raw["priorities"].insert_one({"annual_plan_id": str(plan), "focus_area_id": str(area), "deleted_at": None})).inserted_id
    ids["activities"] = world.run(world.raw["activities"].insert_one({"goal_id": str(goal), "deleted_at": None})).inserted_id
    return ids


def _delete(world, stripe_ok: bool = True, firebase=None):
    """DELETE /users/account with Firebase's admin module stubbed where the route imports it from."""
    auth = firebase or MagicMock()
    with patch.dict(sys.modules, {"firebase_admin": MagicMock(auth=auth)}), \
         patch.object(account_deletion, "delete_stripe_customer", return_value=stripe_ok) as stripe:
        response = world.http.delete("/users/account")
    return response, auth.revoke_refresh_tokens, auth.delete_user, stripe


def _resolve(world, decoded):
    import app.auth.firebase_auth as firebase_auth

    request = Request({"type": "http", "headers": [(b"authorization", b"Bearer t")], "query_string": b""})
    with patch.object(firebase_auth, "verify_firebase_token", return_value=decoded), \
         patch.object(firebase_auth, "_get_cached_token", return_value=None), \
         patch.object(firebase_auth, "_cache_token"), \
         patch.object(firebase_auth, "gate_account_creation", return_value=None), \
         patch("stripe.Customer.create", side_effect=RuntimeError("no network")):
        return world.run(firebase_auth.get_firebase_user(request))


# ── 1. The list ──────────────────────────────────────────────────────────────


def test_every_collection_that_holds_an_account_id_is_placed_on_purpose():
    """A new collection with a user_id must be added to OWNED or HANDLED_OTHERWISE."""
    source = (APP / "config" / "database.py").read_text()
    collections = set(re.findall(r'db\["([a-z_]+)"\]', source))
    owned = {name for name, _ in account_deletion.OWNED} | set(account_deletion.PLAN_CHILDREN) | {"users"}
    placed = owned | set(account_deletion.HANDLED_OTHERWISE)
    # Collections that never hold an account's own data.
    not_personal = {
        "public_content", "rate_limits", "email_sends", "email_suppressions", "stripe_processed_events",
        "waitlist", "contact_messages", "copyright_notices", "content_reports", "import_locks",
        "prompt_cache", "langfuse_cache", "official_content",
    }
    unplaced = sorted(collections - placed - not_personal)
    assert unplaced == [], f"place these in account_deletion.OWNED or HANDLED_OTHERWISE: {unplaced}"


def test_everything_marked_also_expires():
    assert set(account_deletion.EXPIRING) == {n for n, _ in account_deletion.OWNED} | set(account_deletion.PLAN_CHILDREN) | {"users"}
    assert account_deletion.RETENTION_DAYS == 30


# ── 2. Deletion ──────────────────────────────────────────────────────────────


def test_deletion_marks_every_row_the_account_owns_and_nobody_elses(world):
    mine = _seed(world, world.uid)
    bob = world.new_user("bob")
    theirs = _seed(world, bob)

    response, revoke, drop, stripe = _delete(world)

    assert response.status_code == 200, response.text
    assert response.json()["message"] == "Your account is deleted. Everything in it is erased after 30 days."
    for name, ident in mine.items():
        row = world.run(world.raw[name].find_one({"_id": ident}))
        assert row["deleted_at"] is not None and row["deleted_by"] == str(world.uid), name
    for name, ident in theirs.items():
        assert world.run(world.raw[name].find_one({"_id": ident})).get("deleted_at") is None, name
    revoke.assert_called_once()
    drop.assert_called_once()
    stripe.assert_awaited_once_with("cus_ana")


def test_comments_are_marked_to_be_erased_not_kept(world):
    """A comment is a private note; it goes with the account (ADR-081 point 3)."""
    note = world.run(world.raw["comments"].insert_one({"user_id": str(world.uid), "body": "mine", "deleted_at": None})).inserted_id
    _delete(world)
    row = world.run(world.raw["comments"].find_one({"_id": note}))
    assert row["deleted_at"] is not None and row["body"] == "mine"  # erased by the expiry, not rewritten


def test_tokens_and_board_seats_go_at_once(world):
    user_id = str(world.uid)
    world.run(world.raw["device_tokens"].insert_one({"user_id": user_id, "token": "ExponentPushToken[x]"}))
    bob = world.new_user("bob")
    board = world.run(world.raw["blackboards"].insert_one({"owner_user_id": str(bob), "collaborators": [user_id], "deleted_at": None})).inserted_id
    _delete(world)
    assert world.run(world.raw["device_tokens"].count_documents({"user_id": user_id})) == 0
    shared = world.run(world.raw["blackboards"].find_one({"_id": board}))
    assert shared["collaborators"] == [] and shared["deleted_at"] is None  # Bob's board stays


def test_every_database_write_is_committed_before_firebase_is_told(world):
    """D-08: the account record is released, last of the database writes, before Firebase is told."""
    order = []
    release = account_deletion.release_identity

    async def release_and_note(*args, **kwargs):
        await release(*args, **kwargs)
        order.append("database")

    auth = MagicMock()
    auth.revoke_refresh_tokens.side_effect = lambda uid: order.append("firebase")
    auth.delete_user.side_effect = RuntimeError("firebase down")
    with patch.object(account_deletion, "release_identity", release_and_note):
        response, *_ = _delete(world, firebase=auth)
    assert response.status_code == 200
    assert order == ["database", "firebase"]
    assert world.run(world.raw["users"].find_one({"_id": world.uid}))["deleted_at"] is not None  # a Firebase failure undid nothing


def test_a_stripe_refusal_never_undoes_the_deletion(world):
    response, *_ = _delete(world, stripe_ok=False)
    assert response.status_code == 200
    assert world.run(world.raw["users"].find_one({"_id": world.uid}))["deleted_at"] is not None


def test_deletion_is_still_refused_while_anything_is_public(world):
    world.run(world.raw["decks"].insert_one({"user_id": str(world.uid), "is_public": True, "deleted_at": None}))
    response, *_ = _delete(world)
    assert response.status_code == 409
    assert world.run(world.raw["users"].find_one({"_id": world.uid})).get("deleted_at") is None


@pytest.mark.asyncio
async def test_the_stripe_customer_is_deleted_and_a_failure_is_only_logged(monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    with patch("stripe.Customer.delete") as delete:
        assert await account_deletion.delete_stripe_customer("cus_1") is True
    delete.assert_called_once_with("cus_1")
    with patch("stripe.Customer.delete", side_effect=RuntimeError("stripe down")):
        assert await account_deletion.delete_stripe_customer("cus_1") is False
    assert await account_deletion.delete_stripe_customer(None) is False
    monkeypatch.delenv("STRIPE_SECRET_KEY")
    assert await account_deletion.delete_stripe_customer("cus_1") is False


# ── 3. The identity ──────────────────────────────────────────────────────────


def test_the_identity_is_released_and_kept_aside_for_support(world):
    before = world.run(world.raw["users"].find_one({"_id": world.uid}))
    _delete(world)
    after = world.run(world.raw["users"].find_one({"_id": world.uid}))
    assert after["email"] == f"deleted-{world.uid}@deleted.nowry.invalid"
    assert after["username"] == f"deleted-{world.uid}" and after["firebase_uid"] == f"deleted-{world.uid}"
    assert after["deleted_identity"] == {
        "email": before["email"], "username": before["username"],
        "firebase_uid": before["firebase_uid"], "stripe_customer_id": "cus_ana",
    }
    assert after["stripe_customer_id"] is None


def test_the_same_address_signs_up_again_the_same_day_as_a_new_account_that_sees_nothing_old(world):
    old_id = str(world.uid)
    old_deck = world.run(world.raw["decks"].insert_one({"user_id": old_id, "name": "old", "deleted_at": None})).inserted_id
    from app.config import database

    world.run(database.create_indexes())  # with the real unique indexes, a clash would fail the sign-up
    _delete(world)

    # Firebase gives the same address a new sign-in id; Google has verified it.
    token = _resolve(world, {"uid": "fb-new", "email": "ana@example.com", "email_verified": True,
                             "auth_time": 10, "firebase": {"sign_in_provider": "google.com"}})

    assert token["user_id"] != old_id
    new = world.run(world.raw["users"].find_one({"_id": ObjectId(token["user_id"])}))
    assert new["email"] == "ana@example.com" and new.get("deleted_at") is None
    assert world.run(world.raw["users"].count_documents({"email": "ana@example.com"})) == 1
    # Nothing old carries the new id, and the old deck stays marked under the old one.
    assert world.run(world.raw["decks"].count_documents({"user_id": token["user_id"]})) == 0
    assert world.run(world.raw["decks"].find_one({"_id": old_deck}))["deleted_at"] is not None
    # The old record is still deleted: signing in did not bring it back.
    assert world.run(world.raw["users"].find_one({"_id": ObjectId(old_id)}))["deleted_at"] is not None


def test_a_deleted_record_is_never_read_by_sign_in_even_with_its_old_values(world):
    """A record deleted before ADR-081 kept its real email and sign-in id; it is still not found."""
    from app.config import database

    world.run(database.create_indexes())  # the unique indexes are what a stale record collides with
    world.run(world.raw["users"].update_one({"_id": world.uid}, {"$set": {"deleted_at": NOW}}))
    old = world.run(world.raw["users"].find_one({"_id": world.uid}))
    import app.auth.firebase_auth as firebase_auth

    request = Request({"type": "http", "headers": [(b"authorization", b"Bearer t")], "query_string": b""})
    with patch.object(firebase_auth, "verify_firebase_token", return_value={"uid": old["firebase_uid"], "email": old["email"], "email_verified": True, "firebase": {"sign_in_provider": "google.com"}}), \
         patch.object(firebase_auth, "_get_cached_token", return_value=None), \
         patch.object(firebase_auth, "_cache_token"), \
         patch.object(firebase_auth, "gate_account_creation", return_value=None), \
         patch("stripe.Customer.create", side_effect=RuntimeError("no network")):
        token = world.run(firebase_auth.get_firebase_user(request))
    # A new account is made; the old record gives up its identity and stays deleted.
    assert token["user_id"] != str(world.uid)
    stale = world.run(world.raw["users"].find_one({"_id": world.uid}))
    assert stale["deleted_at"].replace(tzinfo=timezone.utc) == NOW and stale["email"] == f"deleted-{world.uid}@deleted.nowry.invalid"
    assert stale["deleted_identity"]["email"] == old["email"]
    assert world.run(world.raw["users"].count_documents({"email": old["email"]})) == 1


def test_register_does_not_treat_a_deleted_account_as_existing(world):
    _delete(world)
    world.users[world.uid] = {"user_id": "pending", "firebase_uid": "fb-new2", "email": "ana@example.com", "email_verified": True}
    world.act_as(world.uid)
    response = world.http.post("/auth/register", json={"firebase_uid": "fb-new2", "email": "ana@example.com", "username": "ana_again"})
    assert response.status_code in (200, 201), response.text
    assert response.json()["message"] == "User registered successfully"


# ── 4. The expiry ────────────────────────────────────────────────────────────


def test_the_expiry_index_exists_on_every_listed_collection(world):
    from app.config import database

    world.run(database.create_indexes())
    for name in account_deletion.EXPIRING:
        indexes = world.run(world.raw[name].index_information())
        ttl = indexes.get("soft_delete_ttl")
        assert ttl and ttl.get("expireAfterSeconds") == 30 * 24 * 3600, name
