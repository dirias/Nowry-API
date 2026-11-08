"""The editor's image routes over a fake storage backend (MEDIA-001/003, ADR-062).

Nothing here reaches Cloudinary: the backend is faked at ``get_storage_backend``
where each module imports it, and the fake records every upload and deletion
so a test can read what would have been stored and where.
"""
import os
from unittest.mock import patch

import pytest

pytest.importorskip("mongomock_motor")

from tests._fake_world import fake_world  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


class FakeStorage:
    """Records what the routes ask of storage and answers like Cloudinary would."""

    def __init__(self):
        self.uploads: list = []
        self.deleted: list = []

    async def upload(self, file_content: bytes, filename: str, folder: str = "", **options) -> dict:
        public_id = f"{folder}/img{len(self.uploads) + 1}"
        self.uploads.append({"folder": folder, "filename": filename, "bytes": len(file_content), "options": options})
        return {"url": f"http://cdn.test/{public_id}.png", "secure_url": f"https://cdn.test/{public_id}.png",
                "public_id": public_id, "width": 640, "height": 480, "format": "png", "bytes": len(file_content)}

    async def delete(self, public_id: str) -> bool:
        self.deleted.append(public_id)
        return True

    async def delete_many(self, public_ids: list) -> int:
        self.deleted.extend(public_ids)
        return len(public_ids)

    def get_thumbnail_url(self, public_id: str, width: int = 200, height: int = 200) -> str:
        return f"https://cdn.test/thumb/{public_id}"


@pytest.fixture
def world():
    with fake_world() as w:
        uid = w.new_user("ana")
        w.act_as(uid)
        w.uid = uid
        yield w


@pytest.fixture
def storage():
    fake = FakeStorage()
    with patch("app.routers.image_upload.get_storage_backend", return_value=fake), \
            patch("app.utils.media_ledger.get_storage_backend", return_value=fake):
        yield fake


@pytest.fixture
def book(world):
    return world.http.post("/book/create", json={"title": "Notes", "content": "p"}).json()["_id"]


def _upload(world, book_id, content=PNG, name="pic.png", content_type="image/png"):
    form = {"book_id": book_id} if book_id is not None else {}
    return world.http.post("/image/upload", files={"file": (name, content, content_type)}, data=form)


def _ledger(world, **query):
    return world.run(world.raw["media"].find(query).to_list(length=50))


# ── MEDIA-001: the root folder is the environment's ─────────────────────────

def test_uploads_and_the_delete_guard_use_the_environment_root(world, storage, book):
    user = world.current["user"]["user_id"]
    with patch.dict(os.environ, {"MEDIA_ROOT_FOLDER": "nowry-test"}):
        response = _upload(world, book)
        assert response.status_code == 200, response.text
        assert storage.uploads[0]["folder"] == f"nowry-test/{user}/{book}"
        assert world.http.delete(f"/image/delete/nowry/{user}/{book}/img1").status_code == 403
        assert world.http.delete(f"/image/delete/nowry-test/{user}/{book}/img1").status_code == 200
    assert storage.deleted == [f"nowry-test/{user}/{book}/img1"]


def test_without_the_variable_the_root_is_nowry(world, storage, book):
    user = world.current["user"]["user_id"]
    with patch.dict(os.environ, {"MEDIA_ROOT_FOLDER": ""}):
        response = _upload(world, book)
        assert response.status_code == 200, response.text
        assert storage.uploads[0]["folder"] == f"nowry/{user}/{book}"
        assert world.http.delete(f"/image/delete/nowry/{user}/{book}/img1").status_code == 200
        assert world.http.delete(f"/image/delete/nowry/{world.new_user('bob')}/{book}/img1").status_code == 403


# ── MEDIA-003: a document owns its images ───────────────────────────────────

def test_an_upload_is_stored_under_the_book_and_written_to_the_ledger(world, storage, book):
    user = world.current["user"]["user_id"]
    response = _upload(world, book)
    assert response.status_code == 200, response.text
    body = response.json()
    public_id = f"nowry/{user}/{book}/img1"
    assert body["url"] == f"https://cdn.test/{public_id}.png" and body["public_id"] == public_id
    assert (body["width"], body["height"], body["size"], body["format"]) == (640, 480, len(PNG), "png")
    assert storage.uploads[0]["options"] == {"quality": "auto", "fetch_format": "auto"}
    rows = _ledger(world)
    assert len(rows) == 1
    row = rows[0]
    assert (row["user_id"], row["book_id"], row["public_id"], row["url"]) == (user, book, public_id, body["url"])
    assert (row["bytes"], row["format"], row["width"], row["height"]) == (len(PNG), "png", 640, 480)
    assert row["created_at"] is not None


def test_an_upload_without_a_book_is_refused_and_stores_nothing(world, storage):
    assert _upload(world, None).status_code == 422
    assert _upload(world, "not-an-id").status_code == 404
    assert _upload(world, "507f1f77bcf86cd799439011").status_code == 404
    assert storage.uploads == [] and _ledger(world) == []


def test_an_upload_into_another_users_book_or_a_deleted_one_is_404(world, storage, book):
    bob = world.new_user("bob")
    world.act_as(bob)
    assert _upload(world, book).status_code == 404
    world.act_as(world.uid)
    with patch("app.utils.media_ledger.sweep_book"):
        assert world.http.delete(f"/book/delete/{book}").status_code == 204
    assert _upload(world, book).status_code == 404
    assert storage.uploads == [] and _ledger(world) == []


def test_deleting_an_image_removes_its_ledger_row(world, storage, book):
    public_id = _upload(world, book).json()["public_id"]
    _upload(world, book)
    assert len(_ledger(world)) == 2
    assert world.http.delete(f"/image/delete/{public_id}").status_code == 200
    assert storage.deleted == [public_id]
    assert [r["public_id"] for r in _ledger(world)] == [f"{public_id[:-1]}2"]


def test_deleting_a_book_sweeps_its_images_from_storage_and_the_ledger(world, storage, book):
    other = world.http.post("/book/create", json={"title": "Other", "content": "p"}).json()["_id"]
    ids = [_upload(world, book).json()["public_id"] for _ in range(2)]
    kept = _upload(world, other).json()["public_id"]
    assert world.http.delete(f"/book/delete/{book}").status_code == 204
    assert sorted(storage.deleted) == sorted(ids)
    assert [r["public_id"] for r in _ledger(world)] == [kept]


def test_deleting_the_account_sweeps_every_image_it_owns_orphans_included(world, storage, book):
    mine = [_upload(world, book).json()["public_id"] for _ in range(2)]
    user = world.current["user"]["user_id"]
    world.run(world.raw["media"].insert_one(
        {"user_id": user, "book_id": "gone-long-ago", "public_id": f"nowry/{user}/orphan"}))
    bob = world.new_user("bob")
    world.act_as(bob)
    theirs = world.http.post("/book/create", json={"title": "Bob", "content": "p"}).json()["_id"]
    their_image = _upload(world, theirs).json()["public_id"]
    world.act_as(world.uid)
    with patch("firebase_admin.auth.revoke_refresh_tokens"), patch("firebase_admin.auth.delete_user"):
        assert world.http.delete("/users/account").status_code == 200
    assert sorted(storage.deleted) == sorted(mine + [f"nowry/{user}/orphan"])
    assert [r["public_id"] for r in _ledger(world)] == [their_image]


# ── MEDIA-004: bounded read, honest statuses, a monthly limit ───────────────

def test_an_oversize_upload_is_413_before_it_is_stored(world, storage, book):
    with patch("app.routers.image_upload.MAX_FILE_SIZE", 1024):
        response = _upload(world, book, content=PNG + b"\x00" * 4096)
    assert response.status_code == 413
    assert storage.uploads == [] and _ledger(world) == []


@pytest.mark.parametrize("name, content, content_type", [
    ("notes.txt", b"hello there", "text/plain"),
    ("notes.txt", PNG, "image/png"),
    ("pic.png", b"<svg xmlns='http://www.w3.org/2000/svg'/>" + b" " * 32, "image/png"),
    ("pic.jpg", b"\x89PNX" + b"\x00" * 32, "image/jpeg"),
])
def test_what_is_not_an_image_by_type_or_bytes_is_415(world, storage, book, name, content, content_type):
    response = _upload(world, book, content=content, name=name, content_type=content_type)
    assert response.status_code == 415, response.text
    assert storage.uploads == [] and _ledger(world) == []


@pytest.mark.parametrize("name, content, content_type", [
    ("pic.jpg", b"\xff\xd8\xff\xe0" + b"\x00" * 32, "image/jpeg"),
    ("pic.gif", b"GIF89a" + b"\x00" * 32, "image/gif"),
    ("pic.webp", b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 32, "image/webp"),
])
def test_every_accepted_format_is_recognised_by_its_bytes(world, storage, book, name, content, content_type):
    assert _upload(world, book, content=content, name=name, content_type=content_type).status_code == 200


def test_the_plans_monthly_count_is_enforced_from_the_ledger(world, storage, book):
    from datetime import datetime, timedelta, timezone
    user = world.current["user"]["user_id"]
    world.run(world.raw["users"].update_one({"_id": world.uid}, {"$set": {"subscription.tier": "free"}}))
    now = datetime.now(timezone.utc)
    rows = [{"user_id": user, "book_id": book, "public_id": f"old/{n}", "created_at": now - timedelta(minutes=1)}
            for n in range(199)]
    rows += [{"user_id": user, "book_id": book, "public_id": f"last-month/{n}", "created_at": now - timedelta(days=40)}
             for n in range(5)]
    rows += [{"user_id": "someone-else", "book_id": "x", "public_id": f"theirs/{n}", "created_at": now} for n in range(5)]
    world.run(world.raw["media"].insert_many(rows))

    assert _upload(world, book).status_code == 200
    refused = _upload(world, book)
    assert refused.status_code == 402 and refused.json()["detail"] == "image_limit_reached"
    assert len(storage.uploads) == 1

    world.run(world.raw["users"].update_one({"_id": world.uid}, {"$set": {"subscription.tier": "pro"}}))
    assert _upload(world, book).status_code == 200


def test_a_storage_failure_is_502_without_the_backends_message(world, storage, book):
    with patch.object(storage, "upload", side_effect=RuntimeError("cloudinary says: quota exceeded for cloud dxyz")):
        response = _upload(world, book)
    assert response.status_code == 502
    assert response.json() == {"detail": "image_storage_unavailable"}
    assert _ledger(world) == []

    public_id = _upload(world, book).json()["public_id"]
    with patch.object(storage, "delete", side_effect=RuntimeError("cloudinary says: 500")):
        response = world.http.delete(f"/image/delete/{public_id}")
    assert response.status_code == 502 and response.json() == {"detail": "image_storage_unavailable"}
    assert len(_ledger(world)) == 1


def test_a_storage_failure_during_a_sweep_keeps_the_rows_and_the_deletion_succeeds(world, storage, book):
    _upload(world, book)
    with patch.object(storage, "delete_many", side_effect=RuntimeError("cloud down")):
        assert world.http.delete(f"/book/delete/{book}").status_code == 204
    assert len(_ledger(world)) == 1
    assert world.run(world.raw["books"].find_one({})).get("deleted_at") is not None
