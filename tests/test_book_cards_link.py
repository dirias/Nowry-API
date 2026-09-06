"""BOOK-001 — the book→cards link at the API (docs/prd-book-cards.md FR-001..FR-004).

Direct-call style like test_ai_magic.py: the endpoint functions are awaited with
fakes for the collections and the model client, so the counting and stamping rules
are exercised without Mongo or Gemini.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_mock_agent_module = MagicMock()
_mock_agent_module.grant_xp = AsyncMock(return_value={})
sys.modules.setdefault("app.routers.agent", _mock_agent_module)

from app.services.book_sections import MIN_SECTION_WORDS, split_sections, parse_lexical  # noqa: E402

OWNER = "507f1f77bcf86cd799439011"
BOOK_ID = "60b8d295f1d2c17f4e4b1234"
WORDS = " ".join(f"w{i}" for i in range(MIN_SECTION_WORDS + 10))


def _text(s):
    return {"type": "text", "text": s}


def _doc(*blocks):
    return json.dumps({"root": {"children": list(blocks)}})


def _heading(tag, s):
    return {"type": "heading", "tag": tag, "children": [_text(s)]}


def _para(s):
    return {"type": "paragraph", "children": [_text(s)]}


BOOK = {
    "_id": BOOK_ID,
    "user_id": OWNER,
    "title": "N3 Grammar",
    "deleted_at": None,
    "full_content": _doc(_heading("h2", "Particles"), _para(WORDS), _heading("h2", "Verbs"), _para(WORDS), _heading("h2", "Tiny"), _para("too short")),
}


class FakeCursor:
    def __init__(self, rows):
        self.rows = rows

    async def to_list(self, length=None):
        return list(self.rows)


class FakeCollection:
    def __init__(self, find_rows=None, aggregate_rows=None):
        self.find_rows = find_rows or []
        self.aggregate_rows = aggregate_rows or []
        self.pipelines = []
        self.queries = []

    def find(self, query, projection=None):
        self.queries.append(query)
        return FakeCursor(self.find_rows)

    def aggregate(self, pipeline):
        self.pipelines.append(pipeline)
        return FakeCursor(self.aggregate_rows)


# ---------------------------------------------------------------------------
# FR-002 — sections with their card counts
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_sections_endpoint_reports_cards_and_changed_per_section(monkeypatch):
    from app.routers import books

    sections = split_sections(parse_lexical(BOOK["full_content"]), BOOK["title"])
    particles_hash = sections[0].hash
    cards = FakeCollection(find_rows=[
        {"source_section": {"heading": "Particles", "index": 0, "hash": particles_hash}},
        {"source_section": {"heading": "Particles", "index": 0, "hash": "0ld000000000"}},
    ])
    monkeypatch.setattr(books, "cards_collection", cards)

    result = await books.get_book_sections(book_id=BOOK_ID, existing_book=BOOK, current_user={"user_id": OWNER})

    assert [s["heading"] for s in result["sections"]] == ["Particles", "Verbs"], "the 2-word section is not a section"
    first = result["sections"][0]
    estimate = first.pop("estimate")
    assert 1 <= estimate <= 20
    assert first == {"index": 0, "heading": "Particles", "level": "h2", "words": MIN_SECTION_WORDS + 10, "hash": particles_hash, "cards": 2, "changed": True}
    assert result["sections"][1]["cards"] == 0 and result["sections"][1]["changed"] is False
    assert cards.queries[0]["source_book_id"] == BOOK_ID and cards.queries[0]["deleted_at"] is None


@pytest.mark.asyncio
async def test_sections_of_a_legacy_html_document_is_one_section(monkeypatch):
    from app.routers import books

    monkeypatch.setattr(books, "cards_collection", FakeCollection())
    legacy = {**BOOK, "full_content": "<p>" + WORDS + "</p>"}
    result = await books.get_book_sections(book_id=BOOK_ID, existing_book=legacy, current_user={"user_id": OWNER})
    assert [s["heading"] for s in result["sections"]] == ["N3 Grammar"]


# ---------------------------------------------------------------------------
# FR-003 — generation by section stamps every card and caps per section
# ---------------------------------------------------------------------------
def _gemini(cards_json: str):
    inst = MagicMock()
    shim = MagicMock()
    shim.choices = [MagicMock()]
    shim.choices[0].message.content = cards_json
    inst.request.return_value = shim
    return inst


@pytest.mark.asyncio
async def test_generate_by_section_stamps_cards_and_runs_one_generation_per_section():
    from app.models.book_generation import GenerateFromBookRequest
    from app.routers.cards import generate_cards_from_book

    gemini = _gemini(json.dumps([{"title": f"q{i}", "content": f"a{i}"} for i in range(30)]))
    with patch("app.routers.cards.books_collection") as col:
        col.find_one = AsyncMock(return_value=BOOK)
        with patch("app.routers.cards.get_client_for_tier", return_value=gemini):
            result = await generate_cards_from_book(
                body=GenerateFromBookRequest(book_id=BOOK_ID, sections=[1, 0, 1]),
                current_user={"user_id": OWNER, "subscription": {"tier": "plus"}},
                tier="plus",
            )

    assert gemini.request.call_count == 2, "one generation per distinct requested section"
    assert result.source_book_id == BOOK_ID and result.source_book_title == "N3 Grammar"
    headings = {c.source_section["heading"] for c in result.cards}
    assert headings == {"Particles", "Verbs"}
    assert all(c.source_section["hash"] for c in result.cards)
    # Adaptive cap: 50 words ≈ ceil(chars/250) → the floor of 3; Plus's 20 never binds here.
    per_section = {}
    for c in result.cards:
        per_section[c.source_section["heading"]] = per_section.get(c.source_section["heading"], 0) + 1
    assert all(n <= 20 for n in per_section.values())
    assert all(n <= 3 for n in per_section.values()), "a tiny section yields the adaptive floor, not thirty"


@pytest.mark.asyncio
async def test_generate_without_sections_keeps_the_whole_document_path_and_only_the_book_stamp():
    from app.models.book_generation import GenerateFromBookRequest
    from app.routers.cards import generate_cards_from_book

    gemini = _gemini(json.dumps([{"title": "q", "content": "a"}]))
    with patch("app.routers.cards.books_collection") as col:
        col.find_one = AsyncMock(return_value=BOOK)
        with patch("app.routers.cards.get_client_for_tier", return_value=gemini):
            result = await generate_cards_from_book(
                body=GenerateFromBookRequest(book_id=BOOK_ID),
                current_user={"user_id": OWNER, "subscription": {"tier": "plus"}},
                tier="plus",
            )
    assert gemini.request.call_count == 1
    assert result.cards[0].source_section is None and result.source_book_id == BOOK_ID


@pytest.mark.asyncio
async def test_generate_refuses_unknown_sections():
    from fastapi import HTTPException
    from app.models.book_generation import GenerateFromBookRequest
    from app.routers.cards import generate_cards_from_book

    with patch("app.routers.cards.books_collection") as col:
        col.find_one = AsyncMock(return_value=BOOK)
        with patch("app.routers.cards.get_client_for_tier", return_value=_gemini("[]")):
            with pytest.raises(HTTPException) as exc:
                await generate_cards_from_book(
                    body=GenerateFromBookRequest(book_id=BOOK_ID, sections=[9]),
                    current_user={"user_id": OWNER, "subscription": {"tier": "plus"}},
                    tier="plus",
                )
    assert exc.value.status_code == 400


# ---------------------------------------------------------------------------
# FR-004 — the list carries counts from one aggregation
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_list_counts_come_from_one_aggregation_over_linked_cards(monkeypatch):
    from app.routers import books

    cards = FakeCollection(aggregate_rows=[{"_id": BOOK_ID, "cards": 18, "due": 4, "headings": ["Particles", "Verbs", None]}])
    monkeypatch.setattr(books, "cards_collection", cards)
    books_col = FakeCollection(find_rows=[{"_id": BOOK_ID, "user_id": OWNER, "title": "N3 Grammar"}, {"_id": "60b8d295f1d2c17f4e4b9999", "user_id": OWNER, "title": "Empty"}])
    books_col.count_documents = AsyncMock(return_value=2)

    result = await books.get_all_books(books_collection=books_col, current_user={"user_id": OWNER})

    assert result[0]["cards"] == 18 and result[0]["due"] == 4 and result[0]["sections_with_cards"] == 2
    assert result[1]["cards"] == 0 and result[1]["source"] == "written"
    assert len(cards.pipelines) == 1, "one aggregation for the whole list"
    assert cards.pipelines[0][0]["$match"]["source_book_id"] == {"$in": [BOOK_ID, "60b8d295f1d2c17f4e4b9999"]}


def test_the_source_fields_are_not_editable_through_patch():
    from app.models.StudyCard import StudyCard, StudyCardUpdate

    assert {"source_book_id", "source_book_title", "source_section"} <= set(StudyCard.model_fields)
    assert not ({"source_book_id", "source_book_title", "source_section"} & set(StudyCardUpdate.model_fields))



# --- BOOK-004: the card outlives its document (D12, D13) ---------------------------------


@pytest.mark.asyncio
async def test_annotate_source_books_flags_deleted_and_missing_documents_and_follows_a_rename():
    from bson import ObjectId
    from app.routers import study_cards

    live_id, gone_id, missing_id = ObjectId(), ObjectId(), ObjectId()
    rows = [
        {"_id": live_id, "title": "N3 Grammar (2nd ed.)", "deleted_at": None},
        {"_id": gone_id, "title": "Old notes", "deleted_at": datetime(2026, 9, 1)},
    ]
    books = MagicMock()
    books.find.return_value.to_list = AsyncMock(return_value=rows)
    cards = [
        {"_id": "a", "source_book_id": str(live_id), "source_book_title": "N3 Grammar"},
        {"_id": "b", "source_book_id": str(gone_id), "source_book_title": "Old notes"},
        {"_id": "c", "source_book_id": str(missing_id), "source_book_title": "Vanished"},
        {"_id": "d"},
    ]
    with patch.object(study_cards, "books_collection", books):
        out = await study_cards._annotate_source_books(cards)

    assert [c.get("source_book_deleted") for c in out] == [False, True, True, None]
    assert out[0]["source_book_title"] == "N3 Grammar (2nd ed.)"
    assert out[1]["source_book_title"] == "Old notes"
    books.find.assert_called_once()


@pytest.mark.asyncio
async def test_annotate_source_books_skips_the_query_when_no_card_has_a_source():
    from app.routers import study_cards

    books = MagicMock()
    with patch.object(study_cards, "books_collection", books):
        await study_cards._annotate_source_books([{"_id": "a"}])
    books.find.assert_not_called()


def test_book_summary_keeps_the_computed_counts():
    from app.models.Book import BookSummary

    summary = BookSummary(title="N3", cards=18, due=4, sections_with_cards=3)
    dumped = summary.model_dump()
    assert (dumped["cards"], dumped["due"], dumped["sections_with_cards"]) == (18, 4, 3)
