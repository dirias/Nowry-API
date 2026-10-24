"""QA-008: sign-up, sign-in, billing, import and export at the route level.

Each runs the real handler over the in-memory database (``tests/_fake_world``),
so the lookups, the ownership and the shapes are the ones production runs.
"""
import io
import json
import os
import sqlite3
import tempfile
import zipfile
from unittest.mock import MagicMock, patch

import pytest
from bson import ObjectId

pytest.importorskip("mongomock_motor")

from tests._fake_world import fake_world  # noqa: E402


@pytest.fixture
def world():
    with fake_world() as w:
        yield w


def _token(world, uid, **overrides):
    """What get_firebase_user hands a handler for that user."""
    token = dict(world.users[uid])
    token.update(overrides)
    world.current["user"] = token
    return token


# ── /auth/register and /auth/login ──────────────────────────────────────────


def test_register_creates_a_free_account_with_the_pet_asleep(world):
    world.current["user"] = {"firebase_uid": "fb-new", "email": "new@example.com", "email_verified": True}
    response = world.http.post("/auth/register", json={"firebase_uid": "fb-new", "email": "new@example.com", "username": "newcomer"})
    assert response.status_code == 200, response.text
    doc = world.run(world.raw["users"].find_one({"firebase_uid": "fb-new"}))
    assert doc["subscription"]["tier"] == "free"
    assert doc["preferences"]["pet"] == {"pet_active": False, "pet_revealed": False}
    assert response.json()["user_id"] == str(doc["_id"])


def test_register_refuses_a_uid_that_is_not_the_tokens(world):
    world.current["user"] = {"firebase_uid": "fb-mine", "email": "me@example.com"}
    response = world.http.post("/auth/register", json={"firebase_uid": "fb-someone-else", "email": "me@example.com", "username": "mine"})
    assert response.status_code == 400


def test_register_cannot_claim_another_accounts_email(world):
    """The body's email is the caller's word; the token's is Firebase's."""
    victim = world.new_user("victim", username="victim_name")
    world.current["user"] = {"firebase_uid": "fb-attacker", "email": "attacker@example.com", "email_verified": True}
    response = world.http.post("/auth/register", json={
        "firebase_uid": "fb-attacker", "email": "victim@example.com", "username": "pwned",
    })
    assert response.status_code == 400
    assert str(victim) not in response.text and "victim" not in response.text
    assert world.run(world.raw["users"].find_one({"_id": victim}))["username"] == "victim_name"


def test_register_again_keeps_the_account_and_takes_the_typed_username(world):
    uid = world.new_user("returning", username="auto_from_token")
    token = _token(world, uid)
    response = world.http.post("/auth/register", json={
        "firebase_uid": token["firebase_uid"], "email": token["email"], "username": "typed_name",
    })
    assert response.status_code == 200
    assert response.json()["user_id"] == str(uid)
    assert world.run(world.raw["users"].find_one({"_id": uid}))["username"] == "typed_name"


def test_login_returns_the_tokens_account_and_404s_an_unknown_one(world):
    uid = world.new_user("member")
    token = _token(world, uid)
    ok = world.http.post("/auth/login", json={"firebase_uid": token["firebase_uid"], "email": token["email"]})
    assert ok.status_code == 200 and ok.json()["user_id"] == str(uid)

    world.current["user"] = {"firebase_uid": "fb-nobody", "email": "nobody@example.com"}
    missing = world.http.post("/auth/login", json={"firebase_uid": "fb-nobody", "email": "nobody@example.com"})
    assert missing.status_code == 404

    mismatch = world.http.post("/auth/login", json={"firebase_uid": token["firebase_uid"], "email": token["email"]})
    assert mismatch.status_code == 400


def test_an_unverified_email_never_resolves_to_someone_elses_account(world):
    """The email fallback exists for a Google account re-issued a new uid; Google emails are verified."""
    from starlette.requests import Request

    import app.auth.firebase_auth as firebase_auth

    victim = world.new_user("victim")
    request = Request({"type": "http", "headers": [(b"authorization", b"Bearer unverified-token")], "query_string": b""})
    decoded = {"uid": "fb-squatter", "email": "victim@example.com", "email_verified": False}
    with patch.object(firebase_auth, "verify_firebase_token", return_value=decoded), \
         patch.object(firebase_auth, "_get_cached_token", return_value=None), \
         patch.object(firebase_auth, "_cache_token"):
        try:
            resolved = world.run(firebase_auth.get_firebase_user(request))
        except Exception:
            resolved = {}
    assert resolved.get("user_id") != str(victim)


def test_an_unverified_email_cannot_take_an_account_over_on_a_duplicate_key(world):
    """Production has a unique email index: the insert collides, and recovery used to adopt the account."""
    from starlette.requests import Request

    import app.auth.firebase_auth as firebase_auth

    world.run(world.raw["users"].create_index("email", unique=True))
    victim = world.new_user("victim")
    request = Request({"type": "http", "headers": [(b"authorization", b"Bearer unverified-token")], "query_string": b""})
    decoded = {"uid": "fb-squatter", "email": "victim@example.com", "email_verified": False}
    with patch.object(firebase_auth, "verify_firebase_token", return_value=decoded), \
         patch.object(firebase_auth, "_get_cached_token", return_value=None), \
         patch.object(firebase_auth, "_cache_token"):
        with pytest.raises(Exception) as refused:
            world.run(firebase_auth.get_firebase_user(request))
    assert getattr(refused.value, "status_code", None) == 409
    assert world.run(world.raw["users"].find_one({"_id": victim}))["firebase_uid"].startswith("fb-victim")


# ── billing ─────────────────────────────────────────────────────────────────


def test_the_portal_opens_for_the_callers_own_customer_only(world):
    payer = world.new_user("payer", stripe_customer_id="cus_payer")
    world.new_user("other", stripe_customer_id="cus_other")
    _token(world, payer)
    with patch("app.routers.subscriptions.stripe") as stripe:
        stripe.billing_portal.Session.create.return_value = MagicMock(url="https://billing.example/session")
        response = world.http.post("/stripe/create-portal-session")
    assert response.status_code == 200 and response.json()["url"] == "https://billing.example/session"
    assert stripe.billing_portal.Session.create.call_args.kwargs["customer"] == "cus_payer"


def test_the_portal_says_so_when_there_is_no_billing_account(world):
    _token(world, world.new_user("never_paid"))
    with patch("app.routers.subscriptions.stripe") as stripe:
        response = world.http.post("/stripe/create-portal-session")
    assert response.status_code == 400
    stripe.billing_portal.Session.create.assert_not_called()


def test_subscription_status_reads_the_callers_plan(world):
    uid = world.new_user("plus", subscription={"tier": "plus", "status": "past_due", "ai_usage_count": 7})
    world.new_user("pro")
    _token(world, uid)
    body = world.http.get("/stripe/subscription-status").json()
    assert body["tier"] == "plus" and body["status"] == "past_due" and body["ai_usage_count"] == 7


# ── /import/apkg ─────────────────────────────────────────────────────────────


def _apkg(notes=(("前", "まえ\x1fbefore"),), decks=None, db_name="collection.anki2") -> bytes:
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, db_name)
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE col (decks TEXT)")
        conn.execute("INSERT INTO col VALUES (?)", (json.dumps(decks or {"1": {"name": "Default"}, "2": {"name": "JLPT::N3"}}),))
        conn.execute("CREATE TABLE notes (flds TEXT, tags TEXT)")
        conn.executemany("INSERT INTO notes VALUES (?, ?)", [(front + "\x1f" + back, " n3 ") for front, back in notes])
        conn.commit()
        conn.close()
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.write(path, db_name)
        return buffer.getvalue()


def _upload(world, data: bytes, name="deck.apkg"):
    return world.http.post("/import/apkg", files={"file": (name, data, "application/octet-stream")})


def test_a_real_package_previews_its_cards_and_deck_name(world):
    _token(world, world.new_user("importer"))
    response = _upload(world, _apkg())
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["suggested_name"] == "N3" and body["card_count"] == 1
    assert body["preview_cards"][0]["front"] == "前" and body["preview_cards"][0]["tags"] == ["n3"]


@pytest.mark.parametrize("data, name, status", [
    (b"anything", "deck.txt", 400),
    (b"this is not a zip archive", "deck.apkg", 422),
    (b"PK\x03\x04" + b"\x00" * 64, "deck.apkg", 422),  # a zip header and then nothing
])
def test_what_is_not_a_package_is_refused_with_a_reason(world, data, name, status):
    _token(world, world.new_user("importer"))
    response = _upload(world, data, name)
    assert response.status_code == status, response.text


def test_a_zip_without_an_anki_collection_is_refused(world):
    _token(world, world.new_user("importer"))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("readme.txt", "hello")
    assert _upload(world, buffer.getvalue()).status_code == 422


def test_an_oversized_upload_is_refused_before_it_is_read_whole(world):
    import app.routers.import_apkg as import_apkg

    _token(world, world.new_user("importer"))
    from starlette.datastructures import UploadFile

    reads = []
    real_read = UploadFile.read

    async def counting_read(self, size=-1):
        reads.append(size)
        return await real_read(self, size)

    with patch.object(import_apkg, "MAX_FILE_SIZE_BYTES", 4096), \
         patch.object(UploadFile, "read", counting_read):
        response = _upload(world, b"\x00" * 100_000)
    assert response.status_code == 413
    assert -1 not in reads, "the whole body was read before the size was known"


def test_a_package_whose_database_claims_to_be_huge_is_refused(world):
    import app.routers.import_apkg as import_apkg

    _token(world, world.new_user("importer"))
    with patch.object(import_apkg, "MAX_DATABASE_BYTES", 1024):
        response = _upload(world, _apkg(notes=[("x" * 2000, "y")]))
    assert response.status_code == 422


# ── /users/export ────────────────────────────────────────────────────────────


def test_the_export_holds_everything_the_caller_made_and_nothing_else(world):
    other = world.new_user("other")
    _token(world, other)
    world.http.post("/decks", json={"name": "OTHER-DECK"})

    me = world.new_user("me")
    _token(world, me)
    deck = world.http.post("/decks", json={"name": "MY-DECK"}).json()
    card = world.http.post("/study-cards", json={"title": "MY-CARD", "content": "a", "deck_id": deck["_id"]}).json()
    world.http.post(f"/study-cards/{card['_id']}/review", json={"quality": 4})
    world.http.post("/book/create", json={"title": "MY-BOOK", "content": "pages"})
    world.http.post("/tasks/", json={"title": "MY-TASK"})
    plan = world.http.post("/annual-plan", json={"year": 2026, "title": "MY-PLAN"}).json()
    area = world.http.post("/annual-plan/focus-areas", json={"annual_plan_id": plan["_id"], "name": "MY-AREA", "order": 0}).json()
    world.http.post("/annual-plan/goals", json={"focus_area_id": area["_id"], "title": "MY-GOAL"})

    response = world.http.get("/users/export")
    assert response.status_code == 200, response.text[:300]
    assert "attachment" in response.headers["content-disposition"]
    body = response.text
    for mine in ("MY-DECK", "MY-CARD", "MY-BOOK", "MY-TASK", "MY-PLAN", "MY-GOAL"):
        assert mine in body, mine
    assert "OTHER-DECK" not in body
