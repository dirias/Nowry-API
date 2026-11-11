"""ADR-065 / IMPORT-019: an image's removal is decided durably and retried until storage has answered."""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from bson import ObjectId

from app.utils import media_ledger
from tests._fake_world import fake_world


class Storage:
    """A storage that can refuse: `fail` calls raise, then it deletes."""

    def __init__(self, fail: int = 0):
        self.fail, self.calls, self.deleted = fail, 0, []

    async def delete_many(self, public_ids):
        self.calls += 1
        if self.calls <= self.fail:
            raise RuntimeError("Error 420 - Too many concurrent admin_api_resource_destroy operations")
        self.deleted += public_ids
        return len(public_ids)


@pytest.fixture
def world():
    with fake_world() as w:
        w.act_as(w.new_user("writer"))
        yield w


def _rows(world, book_id, count=3, **extra):
    uid = world.current["user"]["user_id"]
    docs = [{"user_id": uid, "book_id": book_id, "public_id": f"root/{uid}/{book_id}/img{n}", "url": "u",
             "created_at": datetime.now(timezone.utc), **extra} for n in range(count)]
    world.run(world.raw["media"].insert_many(docs))


def _media(world, query=None):
    return world.run(world.raw["media"].find(query or {}).to_list(length=500))


def test_a_sweep_that_storage_refuses_keeps_its_rows_marked_and_the_next_pass_removes_them(world):
    """The fault: a rate-limited sweep left the images stored for good — nothing tried again."""
    book = str(ObjectId())
    _rows(world, book)
    storage = Storage(fail=1)
    with patch.object(media_ledger, "_storage", lambda: storage):
        assert world.run(media_ledger.sweep_book(book)) == 0
        rows = _media(world)
        assert len(rows) == 3 and all(r["delete_attempts"] == 1 and "delete_lease_owner" not in r for r in rows)
        assert all(r["delete_after"].replace(tzinfo=timezone.utc) > datetime.now(timezone.utc) for r in rows)
        assert world.run(media_ledger.collect()) == 0  # not due yet: the delay is real
        world.run(world.raw["media"].update_many({}, {"$set": {"delete_after": datetime.now(timezone.utc) - timedelta(seconds=1)}}))
        assert world.run(media_ledger.reap_once()) == 3
    assert _media(world) == [] and len(storage.deleted) == 3 and storage.calls == 2


def test_the_decision_is_written_by_the_request_before_any_storage_call(world):
    book = str(ObjectId())
    _rows(world, book)
    assert world.run(media_ledger.condemn({"book_id": book})) == 3
    assert world.run(media_ledger.condemn({"book_id": book})) == 0  # idempotent
    assert all("delete_after" in r for r in _media(world))


def test_only_condemned_rows_are_removed(world):
    keep, drop = str(ObjectId()), str(ObjectId())
    _rows(world, keep, 2)
    _rows(world, drop, 2)
    storage = Storage()
    with patch.object(media_ledger, "_storage", lambda: storage):
        world.run(media_ledger.condemn({"book_id": drop}))
        assert world.run(media_ledger.collect()) == 2
    assert {r["book_id"] for r in _media(world)} == {keep} and all(keep not in p for p in storage.deleted)


def test_a_batch_leased_by_another_worker_is_left_alone_until_the_lease_ends(world):
    book = str(ObjectId())
    lease = datetime.now(timezone.utc) + timedelta(seconds=120)
    _rows(world, book, 2, delete_after=datetime.now(timezone.utc) - timedelta(seconds=5), delete_lease_until=lease,
          delete_lease_owner="other")
    storage = Storage()
    with patch.object(media_ledger, "_storage", lambda: storage):
        assert world.run(media_ledger.collect()) == 0 and storage.calls == 0
        world.run(world.raw["media"].update_many({}, {"$set": {"delete_lease_until": datetime.now(timezone.utc) - timedelta(seconds=1)}}))
        assert world.run(media_ledger.collect()) == 2


def test_more_rows_than_a_batch_are_removed_in_rounds(world):
    book = str(ObjectId())
    _rows(world, book, 250)
    storage = Storage()
    with patch.object(media_ledger, "_storage", lambda: storage):
        world.run(media_ledger.condemn({"book_id": book}))
        assert world.run(media_ledger.collect()) == 250
    assert storage.calls == 3 and _media(world) == []


def test_the_retry_delay_doubles_and_is_capped():
    assert [media_ledger.retry_delay(n) for n in range(4)] == [60, 120, 240, 480]
    assert media_ledger.retry_delay(40) == media_ledger.RETRY_CAP_SECONDS


def test_images_of_a_book_already_deleted_are_found_and_condemned(world):
    """Rows from before removal was durable: the reaper finds them by their deleted book."""
    uid = world.current["user"]["user_id"]
    gone = world.run(world.raw["books"].insert_one({"user_id": uid, "title": "gone", "deleted_at": datetime.now(timezone.utc)})).inserted_id
    live = world.run(world.raw["books"].insert_one({"user_id": uid, "title": "live", "deleted_at": None})).inserted_id
    _rows(world, str(gone), 2)
    _rows(world, str(live), 2)
    storage = Storage()
    with patch.object(media_ledger, "_storage", lambda: storage), patch.object(media_ledger, "_orphan_cursor", None):
        assert world.run(media_ledger.reap_once(world.raw["books"])) == 2
    assert {r["book_id"] for r in _media(world)} == {str(live)}


def test_a_storage_id_written_again_is_no_longer_condemned(world):
    uid = world.current["user"]["user_id"]
    world.run(world.raw["media"].insert_one({"user_id": uid, "book_id": None, "public_id": "root/u/avatar", "delete_after": datetime.now(timezone.utc)}))
    world.run(media_ledger.record(user_id=uid, book_id=None, public_id="root/u/avatar", url="u", bytes_=1, format_="png", width=1, height=1))
    (row,) = _media(world)
    assert "delete_after" not in row


def test_deleting_a_book_condemns_its_images_even_when_the_first_attempt_fails(world):
    uid = world.current["user"]["user_id"]
    book = world.run(world.raw["books"].insert_one({"user_id": uid, "title": "notes", "deleted_at": None})).inserted_id
    _rows(world, str(book), 2)
    storage = Storage(fail=1)
    with patch.object(media_ledger, "_storage", lambda: storage):
        assert world.http.delete(f"/book/delete/{book}").status_code == 204
    rows = _media(world)
    assert len(rows) == 2 and all(r["delete_attempts"] == 1 for r in rows)
