"""IMPORT-016 / ADR-063: the library answers a page at a time.

Run over the in-memory MongoDB so the filters, the order and the offsets are
real. mongomock has no collation, so the title order is asked without it here
and the collation itself is asserted on the function that applies it.
"""
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from bson import ObjectId

from app.services import book_library
from tests._fake_world import fake_world

T0 = datetime(2026, 11, 1, 12, 0, 0)


@pytest.fixture
def world():
    with fake_world() as w, patch.object(book_library, "apply_title_collation", lambda cursor: cursor):
        uid = w.new_user("reader")
        w.act_as(uid)
        yield w


def _book(world, title, minutes, **fields):
    doc = {"title": title, "user_id": world.current["user"]["user_id"], "deleted_at": None,
           "updated_at": T0 + timedelta(minutes=minutes), "created_at": T0, "word_count": 10, "section_count": 1,
           "full_content": "x" * 50, **fields}
    return str(world.run(world.raw["books"].insert_one(doc)).inserted_id)


def _library(world, **params):
    response = world.http.get("/book/library", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def _seed(world, count=130):
    return [_book(world, f"doc {n:03d}", n, source="imported" if n % 3 == 0 else "written",
                  tags=["bio"] if n % 10 == 0 else []) for n in range(count)]


def test_the_newest_document_is_on_the_first_page_however_many_there_are(world):
    """The fault: past a hundred documents, a new one could not be seen."""
    ids = _seed(world)
    page = _library(world)
    assert page["total"] == 130 and page["limit"] == 24 and len(page["items"]) == 24
    assert page["items"][0]["_id"] == ids[-1] and page["items"][0]["title"] == "doc 129"
    assert page["continue_book"]["_id"] == ids[-1]
    assert page["counts"] == {"all": 130, "written": 86, "imported": 44}
    assert "full_content" not in page["items"][0]


def test_every_document_is_reached_exactly_once_by_paging(world):
    ids = set(_seed(world))
    seen, offset = [], 0
    while True:
        page = _library(world, offset=offset, limit=50)
        seen += [item["_id"] for item in page["items"]]
        offset += 50
        if offset >= page["total"]:
            break
    assert len(seen) == 130 and set(seen) == ids


def test_search_kind_and_tags_are_answered_by_the_server_over_the_whole_library(world):
    _seed(world)
    found = _library(world, q="DOC 00")
    assert found["total"] == 10 and {item["title"] for item in found["items"]} == {f"doc 00{n}" for n in range(10)}
    assert found["counts"]["all"] == 130  # the counts describe the library, not the answer
    imported = _library(world, kind="imported", limit=100)
    assert imported["total"] == 44 and all(item["source"] == "imported" for item in imported["items"])
    written = _library(world, kind="written", limit=1)
    assert written["total"] == 86
    tagged = _library(world, tags=["bio"])
    assert tagged["total"] == 13 and tagged["tags"] == [{"tag": "bio", "count": 13}]


def test_an_import_is_also_found_by_its_author_and_a_regex_is_only_text(world):
    _book(world, "Deep Work", 1, source="imported", author="Cal Newport")
    _book(world, "Notes (v2)", 2, author="Cal Newport")
    assert [i["title"] for i in _library(world, q="newport")["items"]] == ["Deep Work"]
    assert [i["title"] for i in _library(world, q="(v2)")["items"]] == ["Notes (v2)"]
    assert _library(world, q=".*")["total"] == 0


def test_title_order_and_cards_order(world):
    a = _book(world, "banana", 1)
    b = _book(world, "apple", 2)
    c = _book(world, "cherry", 3)
    assert [i["title"] for i in _library(world, sort="title")["items"]] == ["apple", "banana", "cherry"]
    uid = world.current["user"]["user_id"]
    world.run(world.raw["cards"].insert_many(
        [{"user_id": uid, "deleted_at": None, "source_book_id": a} for _ in range(3)]
        + [{"user_id": uid, "deleted_at": None, "source_book_id": b}]))
    by_cards = _library(world, sort="cards")
    assert [i["_id"] for i in by_cards["items"]] == [a, b, c]
    assert [i["cards"] for i in by_cards["items"]] == [3, 1, 0]
    assert [i["_id"] for i in _library(world, sort="cards", offset=1, limit=1)["items"]] == [b]
    assert [i["_id"] for i in _library(world, sort="cards", offset=2, limit=5)["items"]] == [c]


def test_the_title_order_asks_for_the_readers_alphabet():
    cursor = MagicMock()
    book_library.apply_title_collation(cursor)
    (collation,), _ = cursor.collation.call_args
    assert collation.document["locale"] == "en" and collation.document["strength"] == 2


def test_another_users_and_deleted_documents_are_never_in_the_answer(world):
    mine = _book(world, "mine", 1)
    _book(world, "gone", 2, deleted_at=T0)
    world.run(world.raw["books"].insert_one({"title": "theirs", "user_id": str(ObjectId()), "deleted_at": None, "updated_at": T0}))
    page = _library(world)
    assert [i["_id"] for i in page["items"]] == [mine] and page["total"] == 1 and page["counts"]["all"] == 1


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 101}, {"offset": -1}, {"kind": "other"}, {"sort": "random"}])
def test_a_question_outside_the_bounds_is_refused(world, params):
    assert world.http.get("/book/library", params=params).status_code == 422


def test_an_empty_library_answers_with_nothing_and_zeroes(world):
    page = _library(world)
    assert page == {"items": [], "total": 0, "offset": 0, "limit": 24,
                    "counts": {"all": 0, "written": 0, "imported": 0}, "tags": [], "continue_book": None}


def test_the_old_list_returns_the_most_recent_hundred_newest_first(world):
    ids = _seed(world)
    books = world.http.get("/book/all").json()
    assert len(books) == 100 and books[0]["_id"] == ids[-1] and ids[0] not in {b["_id"] for b in books}
