"""QA-020: the account routes at the route level.

The users router has 28 tests and they stop at sign-up and ``/me``. Settings
are where data loss shows: a profile patch that drops a field, a password
change that accepts the wrong current one, a deletion that leaves public
content behind. Each route runs here over the fake world against the
documents the API itself writes.
"""
from unittest.mock import patch

import bcrypt
import pytest
from bson import ObjectId

pytest.importorskip("mongomock_motor")

from tests._fake_world import fake_world  # noqa: E402


@pytest.fixture
def world():
    with fake_world() as w:
        uid = w.new_user("ana", bio="old bio", username="ana_old")
        w.act_as(uid)
        w.uid = uid
        yield w


def _user(world, uid=None):
    return world.run(world.raw["users"].find_one({"_id": uid or world.uid}))


# ── profile ─────────────────────────────────────────────────────────────────

def test_put_profile_writes_the_bio_and_nothing_else(world):
    response = world.http.put("/users/profile", json={"bio": "I read at night"})
    assert response.status_code == 200, response.text
    user = _user(world)
    assert user["bio"] == "I read at night" and user["username"] == "ana_old"
    assert world.http.put("/users/profile", json={}).json() == {"message": "No changes requested"}


def test_patch_profile_renames_and_refuses_a_taken_name(world):
    other = world.new_user("bob", username="taken_name")
    assert world.http.patch("/users/profile", json={}).status_code == 400
    assert world.http.patch("/users/profile", json={"username": "taken_name"}).status_code == 409
    assert world.http.patch("/users/profile", json={"username": "not valid!"}).status_code == 422
    response = world.http.patch("/users/profile", json={"username": "ana_new", "bio": "short"})
    assert response.status_code == 200, response.text
    assert response.json()["username"] == "ana_new"
    assert (_user(world)["username"], _user(world)["bio"]) == ("ana_new", "short")
    assert _user(world, other)["username"] == "taken_name"


# ── password ────────────────────────────────────────────────────────────────

def test_a_provider_managed_account_cannot_change_a_password_here(world):
    response = world.http.put("/users/password", json={"current_password": "x", "new_password": "longenough1"})
    assert response.status_code == 400
    assert "authentication provider" in response.json()["detail"]


def test_a_local_password_changes_only_with_the_right_current_one(world):
    hashed = bcrypt.hashpw(b"old-secret", bcrypt.gensalt(4)).decode()
    world.run(world.raw["users"].update_one({"_id": world.uid}, {"$set": {"password": hashed}}))

    wrong = world.http.put("/users/password", json={"current_password": "nope", "new_password": "new-secret-1"})
    assert wrong.status_code == 400 and _user(world)["password"] == hashed
    short = world.http.put("/users/password", json={"current_password": "old-secret", "new_password": "short"})
    assert short.status_code == 422
    right = world.http.put("/users/password", json={"current_password": "old-secret", "new_password": "new-secret-1"})
    assert right.status_code == 200, right.text
    assert bcrypt.checkpw(b"new-secret-1", _user(world)["password"].encode())


# ── notifications ───────────────────────────────────────────────────────────

def test_notification_preferences_are_written_field_by_field(world):
    assert world.http.put("/users/notifications", json={}).status_code == 400
    assert world.http.put("/users/notifications", json={"email_digest": False, "marketing": True}).status_code == 200
    prefs = _user(world)["notification_preferences"]
    assert prefs == {"email_digest": False, "marketing": True}
    world.http.put("/users/notifications", json={"study_reminders": False})
    assert _user(world)["notification_preferences"] == {"email_digest": False, "marketing": True, "study_reminders": False}


# ── avatar ──────────────────────────────────────────────────────────────────

class _FakeStorage:
    """Answers like Cloudinary and records what was asked (MEDIA-006: the avatar is a file)."""

    def __init__(self):
        self.uploads, self.deleted = [], []

    async def upload(self, file_content, filename, folder="", **options):
        self.uploads.append({"folder": folder, "options": options, "bytes": len(file_content)})
        public_id = f"{folder}/{options.get('public_id', 'img')}"
        return {"url": f"http://cdn.test/{public_id}", "secure_url": f"https://cdn.test/{public_id}?v={len(self.uploads)}",
                "public_id": public_id, "width": 256, "height": 256, "format": "png", "bytes": len(file_content)}

    async def delete_many(self, public_ids):
        self.deleted.extend(public_ids)
        return len(public_ids)

    def get_thumbnail_url(self, public_id, width=200, height=200):
        return f"https://cdn.test/thumb/{public_id}"


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def test_the_avatar_must_be_a_small_image(world):
    fake = _FakeStorage()
    with patch("app.routers.users.get_storage_backend", return_value=fake):
        assert world.http.post("/users/avatar", files={"file": ("a.txt", b"hello", "text/plain")}).status_code == 415
        assert world.http.post("/users/avatar", files={"file": ("a.png", b"hello" * 8, "image/png")}).status_code == 415
        too_big = world.http.post("/users/avatar", files={"file": ("a.png", PNG + b"0" * (2 * 1024 * 1024), "image/png")})
        assert too_big.status_code == 413
    assert fake.uploads == [] and _user(world).get("avatar_url") is None


def test_the_avatar_is_stored_as_one_file_per_account_and_goes_with_it(world):
    fake = _FakeStorage()
    user = world.current["user"]["user_id"]
    with patch("app.routers.users.get_storage_backend", return_value=fake), \
            patch("app.utils.media_ledger.get_storage_backend", return_value=fake):
        ok = world.http.post("/users/avatar", files={"file": ("a.png", PNG, "image/png")})
        assert ok.status_code == 200, ok.text
        url = ok.json()["avatar_url"]
        assert url == f"https://cdn.test/nowry/{user}/avatar?v=1" and _user(world)["avatar_url"] == url
        assert fake.uploads[0]["folder"] == f"nowry/{user}"
        assert fake.uploads[0]["options"]["public_id"] == "avatar" and fake.uploads[0]["options"]["overwrite"] is True

        again = world.http.post("/users/avatar", files={"file": ("b.png", PNG, "image/png")})
        assert again.status_code == 200 and _user(world)["avatar_url"] == again.json()["avatar_url"]
        rows = world.run(world.raw["media"].find({"user_id": user}).to_list(length=10))
        assert len(rows) == 1 and rows[0]["book_id"] is None and rows[0]["public_id"] == f"nowry/{user}/avatar"

        with patch.object(fake, "upload", side_effect=RuntimeError("cloud says no")):
            down = world.http.post("/users/avatar", files={"file": ("c.png", PNG, "image/png")})
        assert down.status_code == 502 and down.json() == {"detail": "image_storage_unavailable"}

        with patch("firebase_admin.auth.revoke_refresh_tokens"), patch("firebase_admin.auth.delete_user"):
            assert world.http.delete("/users/account").status_code == 200
    assert fake.deleted == [f"nowry/{user}/avatar"]
    assert world.run(world.raw["media"].count_documents({"user_id": user})) == 0


# ── delete ──────────────────────────────────────────────────────────────────

def test_deletion_is_refused_while_anything_is_public(world):
    deck = world.http.post("/decks", json={"name": "Shared"}).json()["_id"]
    world.http.patch(f"/decks/{deck}/settings", json={"is_public": True})
    response = world.http.delete("/users/account")
    assert response.status_code == 409
    assert "1 public deck" in response.json()["detail"]
    assert _user(world).get("deleted_at") is None


def test_deletion_soft_deletes_the_account_and_everything_it_owns(world):
    deck = world.http.post("/decks", json={"name": "Private"}).json()["_id"]
    card = world.http.post("/study-cards", json={"title": "q", "content": "a", "deck_id": deck}).json()
    book = world.http.post("/book/create", json={"title": "Notes", "content": "p"}).json()
    plan = world.http.post("/annual-plan", json={"year": 2026, "title": "Plan"}).json()
    bystander = world.new_user("bob")
    world.act_as(bystander)
    their_deck = world.http.post("/decks", json={"name": "Theirs"}).json()["_id"]
    world.act_as(world.uid)

    with patch("firebase_admin.auth.revoke_refresh_tokens") as revoke, patch("firebase_admin.auth.delete_user") as drop:
        response = world.http.delete("/users/account")
    assert response.status_code == 200, response.text
    assert _user(world)["deleted_at"] is not None
    revoke.assert_called_once_with(world.current["user"]["firebase_uid"])
    drop.assert_called_once_with(world.current["user"]["firebase_uid"])
    for name, ident in [("decks", deck), ("cards", card.get("_id") or card.get("id")),
                        ("books", book.get("_id") or book.get("id")), ("annual_plans", plan.get("_id") or plan.get("id"))]:
        doc = world.run(world.raw[name].find_one({"_id": ObjectId(ident)}))
        assert doc["deleted_at"] is not None, name
    assert world.run(world.raw["decks"].find_one({"_id": ObjectId(their_deck)})).get("deleted_at") is None
