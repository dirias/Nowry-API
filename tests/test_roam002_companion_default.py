"""ROAM-002 (FR-037): the study buddy is on by default and stays in one place.

A new account, by either sign-up path, is stored with the companion on and
revealed, and answered with roaming off (FR-036). A person's own choice is kept.
The migration switches on only accounts that never had the companion.
"""
from unittest.mock import patch

import pytest
from bson import ObjectId
from starlette.requests import Request

pytest.importorskip("mongomock_motor")

from tests._fake_world import fake_world  # noqa: E402


@pytest.fixture
def world():
    with fake_world() as w:
        yield w


def _pet(world, **query):
    doc = world.run(world.raw["users"].find_one(query))
    return doc["preferences"]["pet"], doc


def test_a_registered_account_has_the_study_buddy_on_and_roaming_off(world):
    world.current["user"] = {"firebase_uid": "fb-new", "email": "new@example.com", "email_verified": True}
    response = world.http.post("/auth/register", json={"firebase_uid": "fb-new", "email": "new@example.com", "username": "newcomer"})
    assert response.status_code == 200, response.text
    pet, doc = _pet(world, firebase_uid="fb-new")
    assert pet == {"pet_active": True, "pet_revealed": True}

    world.current["user"] = {"firebase_uid": "fb-new", "email": "new@example.com", "user_id": str(doc["_id"])}
    state = world.http.get("/agent/me")
    assert state.status_code == 200, state.text
    body = state.json()
    assert body["pet_active"] is True
    assert body["pet_revealed"] is True
    assert body["agent_roaming_enabled"] is False


def test_an_account_made_by_the_sign_in_itself_has_the_study_buddy_on(world):
    import app.auth.firebase_auth as firebase_auth

    decoded = {"uid": "fb-auto", "email": "auto@example.com", "email_verified": True, "auth_time": 1, "firebase": {"sign_in_provider": "google.com"}}
    request = Request({"type": "http", "headers": [(b"authorization", b"Bearer t")], "query_string": b""})
    with patch.object(firebase_auth, "verify_firebase_token", return_value=decoded), \
         patch.object(firebase_auth, "_get_cached_token", return_value=None), \
         patch.object(firebase_auth, "_cache_token"):
        world.run(firebase_auth.get_firebase_user(request))
    pet, _ = _pet(world, firebase_uid="fb-auto")
    assert pet["pet_active"] is True and pet["pet_revealed"] is True


def test_a_person_who_turned_the_study_buddy_off_keeps_it_off(world):
    uid = world.run(world.raw["users"].insert_one({
        "firebase_uid": "fb-off", "email": "off@example.com", "username": "off", "deleted_at": None,
        "preferences": {"pet": {"pet_active": False, "pet_revealed": True}},
    })).inserted_id
    world.current["user"] = {"firebase_uid": "fb-off", "email": "off@example.com", "user_id": str(uid)}
    body = world.http.get("/agent/me").json()
    assert body["pet_active"] is False


# ── the migration ───────────────────────────────────────────────────────────

def _seed(world):
    users = world.raw["users"]
    rows = {
        "never": {"pet": {"pet_active": False, "pet_revealed": False}},     # made off, never revealed → on
        "half": {"pet": {"pet_active": True, "pet_revealed": False}},       # on but unrevealed → revealed
        "chose_off": {"pet": {"pet_active": False, "pet_revealed": True}},  # revealed, then turned off → kept
        "on": {"pet": {"pet_active": True, "pet_revealed": True}},          # already on → untouched
        "missing": {},                                                      # no fields → reads as on, untouched
    }
    ids = {}
    for name, prefs in rows.items():
        ids[name] = world.run(users.insert_one({"username": name, "deleted_at": None, "preferences": prefs})).inserted_id
    return ids


def test_the_migration_is_a_dry_run_by_default(world):
    from app.migrations.companion_on_by_default import companion_on_by_default

    ids = _seed(world)
    result = world.run(companion_on_by_default(collection=world.raw["users"]))
    assert result == {"found": 2, "changed": 0}
    pet, _ = _pet(world, _id=ids["never"])
    assert pet == {"pet_active": False, "pet_revealed": False}


def test_the_migration_switches_on_only_accounts_that_never_had_it(world):
    from app.migrations.companion_on_by_default import companion_on_by_default

    ids = _seed(world)
    result = world.run(companion_on_by_default(apply_changes=True, collection=world.raw["users"]))
    assert result == {"found": 2, "changed": 2}
    assert _pet(world, _id=ids["never"])[0] == {"pet_active": True, "pet_revealed": True}
    assert _pet(world, _id=ids["half"])[0] == {"pet_active": True, "pet_revealed": True}
    assert _pet(world, _id=ids["chose_off"])[0] == {"pet_active": False, "pet_revealed": True}
    assert world.run(world.raw["users"].find_one({"_id": ids["missing"]}))["preferences"] == {}
    again = world.run(companion_on_by_default(apply_changes=True, collection=world.raw["users"]))
    assert again == {"found": 0, "changed": 0}
    assert isinstance(ids["never"], ObjectId)
