"""
PUB-001 — provenance travels with the content, and the library refuses what
arrived from a file (ADR-037, `docs/prd-publish-provenance.md`).

Pins three things:

  1. `POST /import/apkg/confirm` stamps the deck it creates with
     `source: "imported"`.
  2. `publish_content` refuses an imported deck, an imported book, and a deck
     any of whose live cards came from an imported book — with the stable code
     and the matching reason, after ownership and before any write.
  3. Content written in Nowry publishes exactly as before, and a deck document
     that predates the field reads as created.

Direct-call style like `test_public_curation.py`: the service is built on a
dict of mocked Motor collections, the handler is awaited with its `Depends()`
parameters passed as plain kwargs.
"""
import sys
from unittest.mock import AsyncMock, MagicMock, patch

from tests._stubs import stub_if_missing

stub_if_missing("langfuse", "langfuse.langchain")
sys.modules.setdefault("app.models.agent_models", MagicMock())

import pytest
from bson import ObjectId
from fastapi import HTTPException

from app.services.public_content_service import (
    PROVENANCE_READ_LIMIT,
    SOURCE_NOT_PUBLISHABLE,
    PublicContentService,
    direct_publish_block_reason,
)

OWNER = "507f1f77bcf86cd799439011"
DECK_OID = ObjectId("70b8d295f1d2c17f4e4b5678")
BOOK_OID = ObjectId("60b8d295f1d2c17f4e4b1234")
LISTING = {"category": "science", "tags": ["cells"], "language": "en"}


def _cursor(docs):
    cursor = MagicMock()
    cursor.to_list = AsyncMock(return_value=list(docs))
    return cursor


def _collection(found=None, find_docs=()):
    """A Motor collection mock: find_one() → `found`, find().to_list() → `find_docs`."""
    collection = MagicMock()
    collection.find_one = AsyncMock(return_value=found)
    collection.find = MagicMock(return_value=_cursor(find_docs))
    collection.update_one = AsyncMock(return_value=MagicMock(matched_count=1))
    return collection


def _service(*, decks=None, books=None, cards=None):
    return PublicContentService(
        {
            "decks": decks if decks is not None else _collection(),
            "books": books if books is not None else _collection(),
            "cards": cards if cards is not None else _collection(),
        }
    )


def _deck(**overrides):
    deck = {"_id": DECK_OID, "user_id": OWNER, "is_public": False, "deleted_at": None, "name": "Pharm"}
    deck.update(overrides)
    return deck


def _book(**overrides):
    book = {"_id": BOOK_OID, "user_id": OWNER, "is_public": False, "deleted_at": None, "title": "Notes"}
    book.update(overrides)
    return book


async def _refusal(service, content_type, content_id):
    with pytest.raises(HTTPException) as raised:
        await service.publish_content(
            content_type=content_type, content_id=content_id, user_id=OWNER, public_metadata=dict(LISTING)
        )
    return raised.value


# ── 1. The import stamps its origin ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_apkg_confirm_writes_an_imported_source():
    from app.routers import import_apkg

    decks = MagicMock()
    decks.insert_one = AsyncMock(return_value=MagicMock(inserted_id=DECK_OID))
    decks.update_one = AsyncMock()
    cards = MagicMock()
    cards.insert_many = AsyncMock(return_value=MagicMock(inserted_ids=[ObjectId()]))
    payload = import_apkg.ImportConfirmPayload(
        deck_name="Step 1", cards=[import_apkg.ParsedCard(front="Q", back="A", tags=[])]
    )

    with patch.object(import_apkg, "decks_collection", decks), patch.object(
        import_apkg, "cards_collection", cards
    ), patch.object(import_apkg, "_get_remaining_quota", AsyncMock(return_value=-1)):
        result = await import_apkg.confirm_import(payload=payload, user={"user_id": OWNER})

    assert result["deck_id"] == str(DECK_OID)
    assert decks.insert_one.call_args[0][0]["source"] == "imported"


# ── 2. The three refusals ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_imported_deck_is_refused_before_its_cards_are_read():
    decks = _collection(found=_deck(source="imported"))
    cards = _collection()
    error = await _refusal(_service(decks=decks, cards=cards), "deck", str(DECK_OID))

    assert error.status_code == 409
    assert error.detail == {"code": SOURCE_NOT_PUBLISHABLE, "reason": "imported_deck"}
    cards.find.assert_not_called()
    decks.update_one.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_imported_book_is_refused():
    books = _collection(found=_book(source="imported"))
    cards = _collection()
    error = await _refusal(_service(books=books, cards=cards), "book", str(BOOK_OID))

    assert error.status_code == 409
    assert error.detail == {"code": SOURCE_NOT_PUBLISHABLE, "reason": "imported_book"}
    cards.find.assert_not_called()
    books.update_one.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_deck_with_a_card_from_an_imported_book_is_refused():
    decks = _collection(found=_deck(source="created"))
    cards = _collection(find_docs=[{"source_book_id": str(BOOK_OID)}, {"source_book_id": "not-an-id"}])
    books = _collection(find_docs=[{"_id": BOOK_OID}])
    error = await _refusal(_service(decks=decks, books=books, cards=cards), "deck", str(DECK_OID))

    assert error.detail == {"code": SOURCE_NOT_PUBLISHABLE, "reason": "cards_from_imported_book"}
    decks.update_one.assert_not_awaited()

    # Both reads are bounded, and the cards read excludes deleted cards and
    # cards with no source link.
    cards.find.return_value.to_list.assert_awaited_once_with(length=PROVENANCE_READ_LIMIT)
    card_query = cards.find.call_args[0][0]
    assert card_query["deleted_at"] is None
    assert card_query["source_book_id"] == {"$nin": [None, ""]}
    assert DECK_OID in card_query["deck_id"]["$in"] and str(DECK_OID) in card_query["deck_id"]["$in"]
    books.find.return_value.to_list.assert_awaited_once_with(length=1)
    book_query = books.find.call_args[0][0]
    assert book_query["source"] == "imported"
    assert book_query["_id"]["$in"] == [BOOK_OID]


# ── 3. Written content is untouched ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_created_deck_whose_cards_come_from_written_books_publishes():
    decks = _collection(found=_deck(source="created"))
    cards = _collection(find_docs=[{"source_book_id": str(BOOK_OID)}])
    books = _collection(find_docs=[])  # the book exists but was written, not imported
    service = _service(decks=decks, books=books, cards=cards)

    await service.publish_content(content_type="deck", content_id=str(DECK_OID), user_id=OWNER, public_metadata=dict(LISTING))

    written = decks.update_one.call_args[0][1]["$set"]
    assert written["is_public"] is True
    assert written["public_metadata"]["category"] == "science"


@pytest.mark.asyncio
async def test_a_deck_that_predates_the_field_reads_as_created():
    decks = _collection(found=_deck())  # no `source` key at all
    cards = _collection(find_docs=[])
    service = _service(decks=decks, cards=cards)

    await service.publish_content(content_type="deck", content_id=str(DECK_OID), user_id=OWNER, public_metadata=dict(LISTING))

    decks.update_one.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_written_book_publishes_without_reading_cards():
    books = _collection(found=_book(source="written"))
    cards = _collection()
    service = _service(books=books, cards=cards)

    await service.publish_content(content_type="book", content_id=str(BOOK_OID), user_id=OWNER, public_metadata=dict(LISTING))

    cards.find.assert_not_called()
    books.update_one.assert_awaited_once()


@pytest.mark.asyncio
async def test_ownership_is_checked_before_provenance():
    decks = _collection(found=None)  # not this user's deck
    error = await _refusal(_service(decks=decks), "deck", str(DECK_OID))
    assert error.status_code == 404


def test_direct_reason_reads_only_the_source_field():
    assert direct_publish_block_reason("deck", {"source": "imported"}) == "imported_deck"
    assert direct_publish_block_reason("book", {"source": "imported"}) == "imported_book"
    assert direct_publish_block_reason("deck", {"source": "created"}) is None
    assert direct_publish_block_reason("book", {"source": "written"}) is None
    assert direct_publish_block_reason("deck", {}) is None
