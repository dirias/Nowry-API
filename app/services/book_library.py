"""The library, a page at a time (ADR-063).

`GET /book/all` returned the first hundred documents the database happened to
hold, unsorted, and the clients filtered, searched and sorted that list
themselves — so the hundred-and-first document could not be seen, searched for
or opened from the library, though it had been saved (IMPORT-016).

The library is now asked a question and answers one page of it: which kind,
which words, which tags, in which order, from which offset. Everything the
screen draws that is not a row — how many documents there are of each kind,
which tags exist, which document to continue — comes back beside the page, so
no client ever needs the whole list.

Every query here is scoped to one user and bounded by a limit.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from pymongo.collation import Collation

from app.services import book_search

KINDS = ("all", "written", "imported")
SORTS = ("edited", "title", "cards")
PAGE_DEFAULT: int = 24
PAGE_MAX: int = 100
TAGS_MAX: int = 100
#: The most documents with cards that the `cards` order ranks; the rest follow by last edit.
CARD_RANK_MAX: int = 2000

#: Titles sort as a reader expects: "álgebra" beside "algebra", "Zoo" after both.
TITLE_COLLATION = Collation(locale="en", strength=2)
#: A row for the list: neither the document's content nor its search terms travel.
SUMMARY_PROJECTION = {"full_content": 0, "search_terms": 0}


def owned(user_id: str) -> dict:
    """The filter every library query starts from: this user's live documents."""
    return {"user_id": user_id, "deleted_at": None}


def library_filter(user_id: str, kind: str = "all", search: str = "", tags: Optional[list[str]] = None) -> dict:
    """The Mongo filter for one question asked of the library."""
    query: dict[str, Any] = owned(user_id)
    if kind == "imported":
        query["source"] = "imported"
    elif kind == "written":
        query["source"] = {"$ne": "imported"}
    if tags:
        query["tags"] = {"$in": tags}
    needle = (search or "").strip()
    if needle:
        query["$or"] = _search_clauses(needle)
    return query


def _search_clauses(needle: str) -> list[dict]:
    """Which documents the typed words find (IMPORT-036, ADR-069).

    Each folded word must begin one of the document's stored search terms —
    the suffixes of its title's words, and of an import's author — so the
    match ignores case, accents, width and kana, and is answered by the index
    on `search_terms`. A document written before the terms existed has none;
    it is matched on its title as before, until its title is next saved.
    """
    literal = {"$regex": re.escape(needle), "$options": "i"}
    terms = book_search.query_terms(needle)
    if not terms:
        # Nothing but punctuation was typed: only a title that contains exactly that can match.
        return [{"title": literal}]
    by_terms = {"$and": [{"search_terms": {"$regex": "^" + re.escape(term)}} for term in terms]}
    without_terms = {"search_terms": {"$exists": False},
                     "$or": [{"title": literal}, {"source": "imported", "author": literal}]}
    return [by_terms, without_terms]


def apply_title_collation(cursor: Any) -> Any:
    """Order titles by the reader's alphabet, not by code point."""
    return cursor.collation(TITLE_COLLATION)


async def _page_by_field(books: Any, query: dict, sort: str, offset: int, limit: int) -> list[dict]:
    if sort == "title":
        cursor = apply_title_collation(books.find(query, SUMMARY_PROJECTION).sort([("title", 1), ("_id", 1)]))
    else:
        cursor = books.find(query, SUMMARY_PROJECTION).sort([("updated_at", -1), ("_id", -1)])
    return await cursor.skip(offset).limit(limit).to_list(length=limit)


async def _ranked_by_cards(cards: Any, user_id: str) -> list[str]:
    """This user's document ids that have cards, most cards first."""
    rows = await cards.aggregate([
        {"$match": {"user_id": user_id, "deleted_at": None, "source_book_id": {"$ne": None}}},
        {"$group": {"_id": "$source_book_id", "cards": {"$sum": 1}}},
        {"$sort": {"cards": -1, "_id": 1}},
        {"$limit": CARD_RANK_MAX},
    ]).to_list(length=CARD_RANK_MAX)
    return [str(row["_id"]) for row in rows if row.get("_id")]


def _object_ids(ids: list[str]) -> list:
    from bson import ObjectId
    return [ObjectId(i) if ObjectId.is_valid(i) else i for i in ids]


async def _page_by_cards(books: Any, cards: Any, user_id: str, query: dict, offset: int, limit: int) -> list[dict]:
    """Documents with the most cards first, then the rest by last edit.

    The count lives on the cards, not on the document, so the order is made of
    two bounded reads: the ranked ids that match the question, then the others.
    """
    ranked_ids = _object_ids(await _ranked_by_cards(cards, user_id))
    with_cards = await books.find({**query, "_id": {"$in": ranked_ids}}, SUMMARY_PROJECTION).to_list(length=CARD_RANK_MAX)
    position = {str(oid): index for index, oid in enumerate(ranked_ids)}
    with_cards.sort(key=lambda book: position.get(str(book["_id"]), len(position)))
    page = with_cards[offset:offset + limit]
    missing = limit - len(page)
    if missing > 0:
        rest_offset = max(0, offset - len(with_cards))
        rest = books.find({**query, "_id": {"$nin": ranked_ids}}, SUMMARY_PROJECTION).sort([("updated_at", -1), ("_id", -1)])
        page += await rest.skip(rest_offset).limit(missing).to_list(length=missing)
    return page


async def library_page(books: Any, cards: Any, user_id: str, *, kind: str, search: str,
                       tags: list[str], sort: str, offset: int, limit: int) -> tuple[list[dict], int]:
    """One page of the answer and how many documents the whole answer has."""
    query = library_filter(user_id, kind, search, tags)
    total = await books.count_documents(query)
    if sort == "cards":
        items = await _page_by_cards(books, cards, user_id, query, offset, limit)
    else:
        items = await _page_by_field(books, query, sort, offset, limit)
    return items, total


async def library_summary(books: Any, user_id: str) -> dict:
    """What the screen draws beside the rows: counts by kind, the tags, the document to continue."""
    base = owned(user_id)
    rows = await books.aggregate([
        {"$match": base},
        {"$facet": {
            "kinds": [{"$group": {"_id": {"$cond": [{"$eq": ["$source", "imported"]}, "imported", "written"]},
                                  "count": {"$sum": 1}}}],
            "tags": [{"$unwind": "$tags"}, {"$group": {"_id": "$tags", "count": {"$sum": 1}}},
                     {"$sort": {"count": -1, "_id": 1}}, {"$limit": TAGS_MAX}],
        }},
    ]).to_list(length=1)
    facet = rows[0] if rows else {"kinds": [], "tags": []}
    counts = {"all": 0, "written": 0, "imported": 0}
    for row in facet.get("kinds", []):
        counts[row["_id"]] = row["count"]
        counts["all"] += row["count"]
    tags = [{"tag": row["_id"], "count": row["count"]} for row in facet.get("tags", []) if row.get("_id")]
    latest = await books.find(base, SUMMARY_PROJECTION).sort([("updated_at", -1), ("_id", -1)]).limit(1).to_list(length=1)
    return {"counts": counts, "tags": tags, "continue_book": latest[0] if latest else None}
