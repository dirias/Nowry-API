import asyncio
import hashlib
import os
from datetime import datetime, timezone
from typing import List, Optional
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, UploadFile, File, Form
from pymongo.collection import Collection
from bson import ObjectId
from app.models.Book import Book, BookLibraryPage, BookSummary
from app.services import book_library, book_search, import_parser
from pymongo.errors import DuplicateKeyError
from app.models.ai_expand import AIExpandRequest, AIExpandResponse
from app.config.database import books_collection, cards_collection
from app.models.CardGenerationRequest import compute_effective_cap
from app.services.book_sections import PLUS_CARDS_PER_SECTION, annotate_with_cards, document_stats, parse_lexical, split_sections
from app.config.subscription_plans import SUBSCRIPTION_PLANS, SubscriptionTier

DOCUMENT_TOO_LONG_CODE = "document_too_long"


DOCUMENT_TOO_LARGE_CODE = "document_too_large"
#: What one document may weigh as stored JSON (ADR-071). The database refuses a
#: record past 16 MB; the ceiling sits under it so a document that was saved can
#: still be edited and saved again. Words do not bound this: 190,000 long ones
#: (a hex dump, minified code) are under the word ceiling and over this.
DOCUMENT_MAX_BYTES: int = int(os.getenv("DOCUMENT_MAX_BYTES", str(12 * 1024 * 1024)))


def ensure_document_within_ceiling(word_count: int) -> None:
    """ADR-041: one size ceiling per document, the same on every tier (413 with a code)."""
    limit = int(SUBSCRIPTION_PLANS[SubscriptionTier.FREE]["limits"]["document_words"])
    if limit != -1 and word_count > limit:
        raise HTTPException(status_code=413, detail={"code": DOCUMENT_TOO_LONG_CODE, "limit": limit, "words": word_count})


def ensure_document_within_bytes(content_bytes: int) -> None:
    """ADR-071: a document the database could not hold is refused with a code, never met as a failed write."""
    if content_bytes > DOCUMENT_MAX_BYTES:
        raise HTTPException(status_code=413, detail={
            "code": DOCUMENT_TOO_LARGE_CODE, "limit": DOCUMENT_MAX_BYTES // (1024 * 1024),
            "megabytes": round(content_bytes / (1024 * 1024), 1)})
from app.auth.firebase_auth import get_firebase_user
from app.auth.dependencies import require_ownership, track_ai_usage
from app.utils import media_ledger
from app.utils.logger import get_logger
from app.utils.storage import get_storage_backend, media_root
from app.utils.uploads import read_bounded_upload
from app.utils.word_count import count_words
from app.core.model_config import get_client_for_tier, TIER_MODEL_NAMES
from app.core.langfuse_client import get_langfuse_client
from langfuse import propagate_attributes
from app.core import prompt_manager

logger = get_logger(__name__)

_GROQ_MODEL: str = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")


router = APIRouter(
    prefix="/book",
    tags=["books"],
    dependencies=[Depends(get_firebase_user)],
    responses={404: {"description": "Not found"}},
)


# Define a dependency to access the books collection from MongoDB
def get_books_collection() -> Collection:
    # Assuming books_collection is defined in your MongoDB configuration
    return books_collection




@router.post("/create", summary="Create a new book", response_model=Book)
async def create_book(
    book: Book,
    books_collection: Collection = Depends(get_books_collection),
    current_user: dict = Depends(get_firebase_user),
):
    # Set user_id from token
    user_id = current_user.get("user_id")
    book.user_id = user_id

    # ADR-041: books are unlimited on every tier. The one ceiling is per document.
    logger.info(f"Creating book: {book.title}")
    # Exclude _id to let MongoDB generate it as ObjectId
    book_dict = book.model_dump(by_alias=True, exclude={'id'})
    if book_dict.get("full_content"):
        stats = document_stats(parse_lexical(book_dict["full_content"]), book_dict["full_content"])
        ensure_document_within_ceiling(stats["word_count"])
        ensure_document_within_bytes(len(book_dict["full_content"].encode("utf-8")))
        book_dict.update(stats)
    # Searchable from its first write (ADR-069).
    book_dict.update(book_search.search_fields(book_dict.get("title"), book_dict.get("author"), book_dict.get("source")))

    new_book = await books_collection.insert_one(book_dict)
    book_id = str(new_book.inserted_id)
    logger.info(f"Book inserted with ID: {book_id}")

    # We no longer create a "first page" as the book uses full_content now.

    # Fetch and return the created book
    created_book = await books_collection.find_one({"_id": new_book.inserted_id})

    if created_book:
        created_book["_id"] = str(created_book["_id"])
        return created_book

    logger.error(f"Book was inserted (ID: {book_id}) but not found in database after insert")
    raise HTTPException(status_code=500, detail="Failed to create book")


POSITION_FIELDS = {"last_section", "reading_position"}


@router.put("/edit/{book_id}", summary="Edit a book by ID", response_model=Book)
async def edit_book(
    book_id: str,
    book_data: Book,
    background_tasks: BackgroundTasks,
    books_collection: Collection = Depends(get_books_collection),
    existing_book: dict = Depends(require_ownership(get_books_collection, "book_id")),
    current_user: dict = Depends(get_firebase_user),
):
    # Update the book data using partial update (exclude_unset=True)
    update_data = book_data.model_dump(exclude_unset=True)
    
    # Remove immutable/system fields that shouldn't be updated by user.
    # forked_from is permanently set at fork time and must never be overwritten.
    fields_to_remove = ["id", "_id", "user_id", "created_at", "forked_from", "source", "page_count",
                        "source_hash", "source_filename", "search_terms"]
    for field in fields_to_remove:
        update_data.pop(field, None)

    # docs/prd-book-cards.md E5 / prd-books-library.md D6: words and sections are computed on
    # save, so the library never parses content to draw a row.
    if update_data.get("full_content"):
        content = update_data["full_content"]
        stats = document_stats(parse_lexical(content), content)
        ensure_document_within_ceiling(stats["word_count"])
        ensure_document_within_bytes(len(content.encode("utf-8")))
        update_data.update(stats)

    if not update_data:
        raise HTTPException(status_code=400, detail="No data provided for update")

    # A renamed document is found by its new name (ADR-069).
    if "title" in update_data or "author" in update_data:
        update_data.update(book_search.search_fields(
            update_data.get("title", existing_book.get("title")),
            update_data.get("author", existing_book.get("author")),
            existing_book.get("source"),
        ))

    # docs/prd-books-library.md FR-001: a scroll is not an edit. The pointer fields update on
    # their own without bumping updated_at, so "edited Yesterday" keeps meaning edited.
    if not set(update_data) <= POSITION_FIELDS:
        update_data["updated_at"] = datetime.now()

    res = await books_collection.update_one(
        {"_id": ObjectId(existing_book["_id"]) if len(existing_book["_id"]) == 24 else existing_book["_id"]},
        {"$set": update_data},
    )

    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="Book not found")

    # Fetch and return the updated book
    updated_book = await books_collection.find_one({"_id": ObjectId(existing_book["_id"]) if len(existing_book["_id"]) == 24 else existing_book["_id"]})
    if updated_book:
        updated_book["_id"] = str(updated_book["_id"])

        # Trigger background RAG indexing if content changed
        if "full_content" in update_data and update_data["full_content"]:
            from app.utils.book_rag import index_book
            # GTM-005: chat retrieval filters chunks on the Mongo user id
            # (`agent.py` passes current_user["user_id"]); indexing used to store
            # the Firebase uid, so no chunk ever matched. Same id on both sides.
            user_id: str = current_user["user_id"]
            background_tasks.add_task(
                index_book,
                book_id=str(existing_book["_id"]),
                user_id=user_id,
                raw_content=update_data["full_content"],
            )

        return updated_book
    
    raise HTTPException(status_code=500, detail="Error fetching updated book")


@router.delete("/delete/{book_id}", summary="Soft delete a book by ID", status_code=204)
async def delete_book(
    book_id: str,
    background_tasks: BackgroundTasks,
    books_collection: Collection = Depends(get_books_collection),
    book: dict = Depends(require_ownership(get_books_collection, "book_id")),
):
    """
    Soft delete a book (sets deleted_at timestamp).
    Also auto-unpublishes if the book was public.

    A book has no restore route, so its soft delete is final for the writer and
    its images go with it (ADR-062 point 2): the sweep runs after the response,
    best effort and logged, and never fails the deletion itself.
    """
    now = datetime.now(timezone.utc)

    try:
        # Soft delete + auto-unpublish
        result = await books_collection.update_one(
            {"_id": ObjectId(book["_id"]) if len(book["_id"]) == 24 else book["_id"]},
            {
                "$set": {
                    "deleted_at": now,
                    "deleted_by": book.get("user_id"),
                    "is_public": False,  # Auto-unpublish
                    "updated_at": now
                }
            }
        )

        if result.modified_count > 0:
            logger.info(f"Book soft-deleted successfully: {book_id}")
            # The decision is written before the response (ADR-065): whatever
            # happens to the first attempt below, the reaper finishes the job.
            await media_ledger.condemn_for({"book_id": book_id}, f"book {book_id}")
            background_tasks.add_task(media_ledger.sweep_book, book_id)
            return None

    except Exception as e:
        logger.error(f"Error soft-deleting book {book_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Error deleting book")

    raise HTTPException(status_code=404, detail="Book not found")


@router.get("/search", summary="Search books by title", response_model=List[BookSummary])
async def search_books(
    title: str,
    books_collection: Collection = Depends(get_books_collection),
    current_user: dict = Depends(get_firebase_user),
):
    user_id = current_user.get("user_id")
    import re
    safe_title = re.escape(title)
    
    # Search books by title (case-insensitive), excluding full_content for performance, and locked to the user
    cursor = books_collection.find(
        {"title": {"$regex": safe_title, "$options": "i"}, "user_id": user_id, "deleted_at": None},
        {"full_content": 0}
    ).sort([("updated_at", -1), ("_id", -1)]).limit(100)
    books = await cursor.to_list(length=100)  # the hundred most recent matches
    return books


@router.get("/all", summary="Get all books", response_model=List[BookSummary])
async def get_all_books(
    books_collection: Collection = Depends(get_books_collection),
    current_user: dict = Depends(get_firebase_user),
):
    user_id = current_user.get("user_id")
    
    # Debug: Check total books for this user (including deleted)
    total_count = await books_collection.count_documents({"user_id": user_id})
    logger.debug(f"Total books for user {user_id}: {total_count}")

    # Retrieve all books for the current user that are NOT soft-deleted
    # Exclude full_content significantly improves performance for large documents
    # The hundred most recently edited, newest first. This route is kept for the
    # surfaces that want "the recent documents" and for clients that predate the
    # paged library (ADR-063); unsorted, it returned the OLDEST hundred, so a new
    # document past the hundredth was invisible (IMPORT-016). The library itself
    # reads `GET /book/library`.
    cursor = books_collection.find(
        {
            "user_id": user_id,
            "deleted_at": None  # Only books where deleted_at is None
        },
        {"full_content": 0}
    ).sort([("updated_at", -1), ("_id", -1)]).limit(100)
    books = await cursor.to_list(length=100)

    return await _as_library_rows(books_collection, user_id, books)


async def _as_library_rows(books_collection: Collection, user_id: str, books: list[dict]) -> list[dict]:
    """Rows as the library draws them: string ids, stats present, and each row's cards."""
    for book in books:
        book["_id"] = str(book["_id"])

    await _backfill_document_stats(books_collection, books)

    # docs/prd-book-cards.md D6 / FR-004: cards · due · sections_with_cards per book from
    # ONE aggregation over this user's linked cards, so a row never costs a request.
    counts = await _card_counts_for_books(user_id, [b["_id"] for b in books])
    for book in books:
        row = counts.get(book["_id"], {})
        book["cards"] = row.get("cards", 0)
        book["due"] = row.get("due", 0)
        book["sections_with_cards"] = row.get("sections_with_cards", 0)
        book.setdefault("source", "written")

    return books


@router.get("/library", summary="One page of the library, with its counts (ADR-063)", response_model=BookLibraryPage)
async def get_library(
    kind: str = Query("all", pattern="^(all|written|imported)$"),
    q: str = Query("", max_length=200),
    tags: Optional[List[str]] = Query(None, max_length=20),
    sort: str = Query("edited", pattern="^(edited|title|cards)$"),
    offset: int = Query(0, ge=0, le=100_000),
    limit: int = Query(book_library.PAGE_DEFAULT, ge=1, le=book_library.PAGE_MAX),
    books_collection: Collection = Depends(get_books_collection),
    current_user: dict = Depends(get_firebase_user),
) -> dict:
    """The library answers a question a page at a time; the screen never holds the whole list.

    `total` counts the documents that match the question; `counts`, `tags` and
    `continue_book` describe the whole library, whatever was asked.
    """
    user_id = current_user.get("user_id")
    items, total = await book_library.library_page(
        books_collection, cards_collection, user_id,
        kind=kind, search=q, tags=[t for t in (tags or []) if t][:20], sort=sort, offset=offset, limit=limit,
    )
    summary = await book_library.library_summary(books_collection, user_id)
    continue_book = summary["continue_book"]
    decorated = await _as_library_rows(books_collection, user_id, items + ([continue_book] if continue_book else []))
    return {
        "items": decorated[:len(items)],
        "total": total,
        "offset": offset,
        "limit": limit,
        "counts": summary["counts"],
        "tags": summary["tags"],
        "continue_book": decorated[len(items)] if continue_book else None,
    }


BACKFILL_PER_REQUEST = 20


async def _backfill_document_stats(books_collection: Collection, books: list[dict]) -> None:
    """Documents saved before the stats existed (BOOK-001/005) have no word or section count, and
    the library would draw them thin until their next save. Compute and store the stats for up to
    BACKFILL_PER_REQUEST such documents per list request — once each, then the row is true."""
    missing = [b for b in books if b.get("word_count") is None][:BACKFILL_PER_REQUEST]
    for book in missing:
        oid = ObjectId(book["_id"]) if ObjectId.is_valid(book["_id"]) else book["_id"]
        full = await books_collection.find_one({"_id": oid}, {"full_content": 1})
        content = (full or {}).get("full_content") or ""
        stats = document_stats(parse_lexical(content), content)
        book.update(stats)
        await books_collection.update_one({"_id": oid}, {"$set": stats})


async def _card_counts_for_books(user_id: str, book_ids: list[str]) -> dict:
    """{book_id: {cards, due, sections_with_cards}} for the cards that carry a source link.
    due = reviewed before and next_review <= now, the same rule as the study centre's groups."""
    if not book_ids:
        return {}
    now_dt = datetime.now(timezone.utc).replace(tzinfo=None)
    rows = await cards_collection.aggregate([
        {"$match": {"user_id": user_id, "deleted_at": None, "source_book_id": {"$in": book_ids}}},
        {"$group": {
            "_id": "$source_book_id",
            "cards": {"$sum": 1},
            "due": {"$sum": {"$cond": [
                {"$and": [{"$ne": ["$last_reviewed", None]}, {"$lte": ["$next_review", now_dt]}]}, 1, 0]}},
            "headings": {"$addToSet": "$source_section.heading"},
        }},
        {"$limit": 100},
    ]).to_list(length=100)
    return {
        str(row["_id"]): {
            "cards": row.get("cards", 0),
            "due": row.get("due", 0),
            "sections_with_cards": len([h for h in row.get("headings", []) if h]),
        }
        for row in rows
    }


@router.get("/{book_id}/sections", summary="The document's sections with their card counts (BOOK-001)")
async def get_book_sections(
    book_id: str,
    existing_book: dict = Depends(require_ownership(get_books_collection, "book_id")),
    current_user: dict = Depends(get_firebase_user),
) -> dict:
    """docs/prd-book-cards.md FR-002: index · heading · words · cards · changed per section (D2, D3, D5).
    The section text never leaves the server; a legacy HTML document is one section."""
    user_id = current_user.get("user_id")
    title = existing_book.get("title") or ""
    raw = existing_book.get("full_content") or ""
    state = parse_lexical(raw)
    sections = split_sections(state, title)
    if not sections and not state and raw.strip():
        from app.services.book_sections import Section, section_hash
        import re as _re
        text = _re.sub(r"<[^>]+>", " ", raw)
        words = count_words(text)
        if words:
            sections = [Section(0, title, "doc", text, words, section_hash(text))]
    linked = await cards_collection.find(
        {"user_id": user_id, "deleted_at": None, "source_book_id": str(existing_book["_id"])},
        {"source_section": 1},
    ).to_list(length=2000)
    by_heading: dict = {}
    for card in linked:
        heading = (card.get("source_section") or {}).get("heading")
        if heading is not None:
            by_heading.setdefault(heading, []).append(card)
    rows = annotate_with_cards(sections, by_heading)
    # `estimate`: the cards one run would make for the section, from the same adaptive cap the
    # generation applies, so the sheet's budget line (D10) is the server's number, not a guess.
    for section, row in zip(sections, rows):
        row["estimate"] = min(compute_effective_cap(section.text, None), PLUS_CARDS_PER_SECTION)
    return {"book_id": str(existing_book["_id"]), "title": title, "sections": rows}


@router.get("/{book_id}", response_model=Book)
async def get_book_by_id(
    book: dict = Depends(require_ownership(get_books_collection, "book_id")),
):
    return book


BOOK_IMPORT_MAX_BYTES: int = int(os.getenv("BOOK_IMPORT_MAX_BYTES", str(25 * 1024 * 1024)))


#: How many pages a preview carries.
PREVIEW_PAGE_LIMIT: int = 20

#: How many of an import's pictures are stored at once.
IMPORT_PICTURE_CONCURRENCY: int = int(os.getenv("IMPORT_PICTURE_CONCURRENCY", "4"))


async def _read_bounded_upload(file: UploadFile) -> bytes:
    """The upload's bytes, or 413 as soon as they pass `BOOK_IMPORT_MAX_BYTES`."""
    return await read_bounded_upload(file, BOOK_IMPORT_MAX_BYTES)


async def _store_import_pictures(pictures: list, user_id: str, book_id: str) -> dict:
    """Store an import's pictures under the new book and write their ledger rows.

    Returns marker index → `{"url", "width", "height"}` for `place_media`. A
    picture that cannot be stored is dropped with a log line, never inlined
    (ADR-062 point 5); the import itself goes on.
    """
    if not pictures:
        return {}
    try:
        storage = get_storage_backend(os.getenv("STORAGE_BACKEND", "cloudinary"))
    except Exception:
        logger.error("Import pictures for book %s dropped: no storage backend", book_id, exc_info=True)
        return {}
    folder = f"{media_root()}/{user_id}/{book_id}"
    gate = asyncio.Semaphore(IMPORT_PICTURE_CONCURRENCY)

    async def store(picture: dict) -> Optional[tuple]:
        """One picture stored and ledgered: (marker index, placement), or None when it was dropped."""
        async with gate:
            try:
                result = await storage.upload(
                    file_content=picture["bytes"],
                    filename=f"import-{picture['index']}.{picture['ext']}",
                    folder=folder,
                    quality="auto",
                    fetch_format="auto",
                )
            except Exception:
                logger.warning("Import picture %s for book %s could not be stored; dropped",
                               picture["index"], book_id, exc_info=True)
                return None
            width = result.get("width") or picture.get("width")
            height = result.get("height") or picture.get("height")
            await media_ledger.record(
                user_id=user_id, book_id=book_id, public_id=result["public_id"], url=result["secure_url"],
                bytes_=result.get("bytes"), format_=result.get("format"), width=width, height=height,
                origin=media_ledger.ORIGIN_IMPORT,
            )
            return picture["index"], {"url": result["secure_url"], "width": width, "height": height}

    # Stored a few at a time, not one after another: a document with two dozen
    # pictures used to outlast the client's patience and be imported twice (IMPORT-004).
    stored = await asyncio.gather(*(store(picture) for picture in pictures))
    return dict(entry for entry in stored if entry)


IMPORT_FAILED_CODE = "import_failed"
IMPORT_PICTURES_NOT_STORED_CODE = "import_pictures_not_stored"
IMPORT_IN_PROGRESS_CODE = "import_in_progress"
#: What one stored picture adds to a document's JSON, for the size check made before any is stored.
PICTURE_NODE_BYTES: int = 400
IMPORT_LOCK_WAIT_SECONDS: float = float(os.getenv("IMPORT_LOCK_WAIT_SECONDS", "45"))


class _import_lock:
    """One reader's save of one file at a time (ADR-071 point 4).

    The lock is a row keyed by reader and file hash; its unique `_id` is what
    makes taking it atomic. A second save of the same file waits for the first
    and then finds the document it made. A row left by a crashed process is
    removed by its TTL index.
    """

    def __init__(self, books_collection: Collection, user_id: str, source_hash: str) -> None:
        self._locks = books_collection.database["import_locks"]
        self._key = f"{user_id}:{source_hash}"

    async def __aenter__(self) -> None:
        waited = 0.0
        while True:
            try:
                await self._locks.insert_one({"_id": self._key, "created_at": datetime.now(timezone.utc)})
                return
            except DuplicateKeyError:
                if waited >= IMPORT_LOCK_WAIT_SECONDS:
                    raise HTTPException(status_code=409, detail={"code": IMPORT_IN_PROGRESS_CODE})
                await asyncio.sleep(0.25)
                waited += 0.25

    async def __aexit__(self, *_: object) -> None:
        try:
            await self._locks.delete_one({"_id": self._key})
        except Exception:
            logger.warning("Import lock %s not released; its TTL will", self._key, exc_info=True)


async def _save_import(books_collection: Collection, current_user: dict, extracted_pages: list, pictures: list, *,
                       title: str, author: str, filename: str, source_hash: str) -> dict:
    """Store the pictures, build the document and insert it — or leave nothing behind (ADR-071).

    Pictures are stored before the document exists, under the id minted for
    it (ADR-062 point 2). Whatever stops the save after that — a conversion
    error, a ceiling, the database — marks those pictures for removal, and the
    answer is a coded refusal rather than a bare 500.
    """
    from app.utils.file_import import place_media

    user_id = current_user.get("user_id")
    book_oid = ObjectId()
    try:
        placements = await _store_import_pictures(pictures, user_id, str(book_oid))
        dropped = len(pictures) - len(placements)
        combined_html = place_media("\n".join(p.get("content", "") for p in extracted_pages), placements)
        full_content = await import_parser.to_lexical(combined_html)
        stats = document_stats(parse_lexical(full_content), full_content)
        if dropped and not placements and not stats["word_count"]:
            # A scan whose pages could not be stored is an empty document: refuse it, do not save it (IMPORT-044).
            raise HTTPException(status_code=422, detail={"code": IMPORT_PICTURES_NOT_STORED_CODE})
        ensure_document_within_ceiling(stats["word_count"])
        ensure_document_within_bytes(len(full_content.encode("utf-8")))
        new_book = Book(
            id=book_oid, title=title, author=author, user_id=user_id,
            # ADR-067: facts, not prose. That it was imported, the file it came from
            # and its page count are fields each client words in its own language.
            summary="", created_at=datetime.now(), updated_at=datetime.now(), full_content=full_content,
            source="imported", source_hash=source_hash, source_filename=filename[:255] or None,
            page_count=len(extracted_pages), **stats,
        )
        book_dict = new_book.model_dump(by_alias=True)
        book_dict["_id"] = book_oid
        book_dict.update(book_search.search_fields(title, author, "imported"))
        await books_collection.insert_one(book_dict)
    except HTTPException:
        await media_ledger.condemn_for({"book_id": str(book_oid)}, f"refused import {book_oid}")
        raise
    except Exception:
        logger.error("Import of %r failed while saving; its pictures are marked for removal", filename, exc_info=True)
        await media_ledger.condemn_for({"book_id": str(book_oid)}, f"failed import {book_oid}")
        raise HTTPException(status_code=500, detail={"code": IMPORT_FAILED_CODE})
    return {"book_id": str(book_oid), "pictures_dropped": dropped}


IMPORT_DUPLICATE_CODE = "import_duplicate"

async def _existing_import(books_collection: Collection, user_id: str, source_hash: str) -> Optional[dict]:
    """The reader's live document made from the same file, newest first, or None."""
    rows = await books_collection.find(
        {"user_id": user_id, "deleted_at": None, "source_hash": source_hash},
        {"title": 1, "created_at": 1},
    ).sort([("created_at", -1), ("_id", -1)]).limit(1).to_list(length=1)
    if not rows:
        return None
    created = rows[0].get("created_at")
    return {"_id": str(rows[0]["_id"]), "title": rows[0].get("title") or "",
            "created_at": created.isoformat() if hasattr(created, "isoformat") else None}


@router.post("/import", summary="Import a book from file (PDF, DOCX, TXT)")
async def import_book_from_file(
    file: UploadFile = File(...),
    username: str = Form("Unknown"),
    preview: bool = Form(False),  # Preview mode for validation
    title: Optional[str] = Form(None), # Optional title override
    # ADR-066: "ask" refuses a file already in the library with 409 so the client
    # can ask the reader; "copy" imports it again. A client that says nothing is
    # one built before the question existed, and keeps its old behaviour.
    on_duplicate: str = Form("copy", pattern="^(ask|copy)$"),
    books_collection: Collection = Depends(get_books_collection),
    current_user: dict = Depends(get_firebase_user),
):
    """
    Import a book from an uploaded file.

    - **preview=true**: Returns extraction preview with quality metrics for validation
    - **preview=false**: Creates and saves the book with all pages

    Preserves formatting, multi-column layouts, and provides quality metrics.
    """
    from app.utils.file_import import (
        IMPORT_PICTURES_PER_DOCUMENT, IMPORT_TOO_LARGE, refusal_code, strip_media_markers,
    )

    # The upload's bytes, bounded (QA-019): a file past the cap is refused
    # before a parser sees it, as the .apkg import does (QA-008). The refusal
    # carries a code and the limit, so the client says it in its own language.
    try:
        file_content = await _read_bounded_upload(file)
    except HTTPException as refused:
        if refused.status_code != 413:
            raise
        raise HTTPException(
            status_code=413,
            detail={"code": IMPORT_TOO_LARGE, "limit_mb": BOOK_IMPORT_MAX_BYTES // (1024 * 1024)},
        )
    filename = file.filename or ""

    # Read in a child process with a time limit (ADR-070): a hostile or merely
    # huge file costs that child, never the server. Its pictures come back as
    # bytes beside the HTML (ADR-062 point 5).
    try:
        parsed = await import_parser.parse(filename, file_content)
    except import_parser.ImportTooComplex:
        raise HTTPException(status_code=422, detail={"code": import_parser.IMPORT_TOO_COMPLEX})
    extracted_pages = parsed.pages
    # ADR-067: an import keeps the first IMPORT_PICTURES_PER_DOCUMENT pictures. The
    # text is imported whole; the markers of the pictures left out are dropped
    # with them, and the preview says how many will be kept.
    pictures_found = len(parsed.pictures)
    pictures = parsed.pictures[:IMPORT_PICTURES_PER_DOCUMENT]

    if not extracted_pages:
        # Which of the reasons it was (IMPORT-007): wrong type, old .doc, empty,
        # password-protected, or unreadable.
        raise HTTPException(status_code=400, detail={"code": refusal_code(filename, file_content)})

    # ADR-041, ADR-071: the ceilings are per document, however the document
    # arrives. An import past either is refused here, in the preview and in the
    # save, before a picture is stored: accepted, it could never be saved again
    # (IMPORT-003) — or could not be written at all (IMPORT-042).
    import_words = parsed.words
    ensure_document_within_ceiling(import_words)
    ensure_document_within_bytes(parsed.content_bytes + len(pictures) * PICTURE_NODE_BYTES)

    # ADR-066: the same bytes already imported by this reader? The preview says
    # so; a save that asked to be told is refused with the document it would copy.
    source_hash = hashlib.sha256(file_content).hexdigest()
    user_id = current_user.get("user_id")
    duplicate_of = await _existing_import(books_collection, user_id, source_hash)
    if duplicate_of and not preview and on_duplicate == "ask":
        raise HTTPException(status_code=409, detail={"code": IMPORT_DUPLICATE_CODE, "existing": duplicate_of})

    # Create book title from filename or use provided title
    book_title = title if title and title.strip() else filename.rsplit(".", 1)[0]

    # Get extraction metadata
    metadata = extracted_pages[0].get("extraction_metadata", {})

    # What the file holds, as facts the client words itself (IMPORT-013). The
    # old "low word count" warning fired on every text and Word file, because
    # only the PDF reader counted words, and on any slide deck; "N pages have
    # 2-column layout (preserved)" promised a layout the document does not keep.
    # Both are gone: `warnings` and `info` stay in the answer, empty, for a
    # client that still reads them.
    warnings: list = []
    info_msg = None
    # The count of the document as it will be saved, not of the page as it was
    # extracted: list marks and running headers are gone by then, and the preview
    # used to promise more words than the library then showed (IMPORT-027).
    total_words = import_words
    facts = {
        "pages": len(extracted_pages),
        "words": total_words,
        "pictures": pictures_found,
        "pictures_kept": len(pictures),
        "picture_limit": IMPORT_PICTURES_PER_DOCUMENT,
        # Pages that are pictures and no text: a scan. It imports, but there is
        # nothing in it to make cards from, and the reader should hear that first.
        "scanned": total_words == 0 and pictures_found > 0,
        # The file was broken and only what could be rebuilt was read (IMPORT-021).
        "damaged": bool(metadata.get("damaged")),
    }
    avg_words_per_page = total_words / len(extracted_pages) if extracted_pages else 0

    # PREVIEW MODE: Return extraction data for validation
    if preview:
        return {
            "preview": True,
            "title": book_title,
            "total_pages": len(extracted_pages),
            "metadata": metadata,
            "facts": facts,
            "duplicate_of": duplicate_of,
            "info": info_msg,
            "warnings": warnings,
            "file_info": {
                "original_filename": filename,
                "type": (
                    filename.rsplit(".", 1)[-1].upper()
                    if "." in filename
                    else "Unknown"
                ),
                "size": len(file_content),  # File size in bytes
            },
            "sample_pages": [
                {
                    "page_number": p.get("page_number"),
                    "title": p.get("title"),
                    "content_preview": (
                        strip_media_markers(p.get("content", ""))[:5000] + "..."
                        if len(p.get("content", "")) > 5000
                        else strip_media_markers(p.get("content", ""))
                    ),  # Show full page; a preview stores no picture
                    "word_count": p.get("word_count"),
                    "has_columns": p.get("has_columns", False),
                    "quality_score": p.get("quality_score", 0),
                }
                # The first pages only: a preview is a look, and every page of
                # a 500-page file was megabytes the reader never paged through.
                for p in extracted_pages[:PREVIEW_PAGE_LIMIT]
            ],
            "quality_summary": {
                "total_words": total_words,
                "total_chars": metadata.get("total_chars", 0),
                "avg_words_per_page": int(avg_words_per_page),
                "has_multi_column": metadata.get("multi_column_pages", 0) > 0,
            },
        }

    # SAVE MODE. A save that asked about duplicates holds the file's lock while
    # it works, so two of them at once cannot both find the library empty of it
    # (ADR-071 point 4).
    if on_duplicate == "ask":
        async with _import_lock(books_collection, user_id, source_hash):
            existing = await _existing_import(books_collection, user_id, source_hash)
            if existing:
                raise HTTPException(status_code=409, detail={"code": IMPORT_DUPLICATE_CODE, "existing": existing})
            saved = await _save_import(books_collection, current_user, extracted_pages, pictures,
                                       title=book_title, author=username, filename=filename, source_hash=source_hash)
    else:
        saved = await _save_import(books_collection, current_user, extracted_pages, pictures,
                                   title=book_title, author=username, filename=filename, source_hash=source_hash)
    book_id = saved["book_id"]

    # Return the book info
    return {
        "_id": book_id,
        "title": book_title,
        "author": username,
        "page_count": len(extracted_pages), # Keep count for meta info
        # ADR-071 point 3: pictures the storage would not keep; the client says so.
        "pictures_dropped": saved["pictures_dropped"],
        "extraction_quality": {
            "total_words": metadata.get("total_words", 0),
            "multi_column_pages": metadata.get("multi_column_pages", 0),
            "warnings": warnings,
        },
    }


@router.post("/{book_id}/ai-expand", response_model=AIExpandResponse)
async def ai_expand_text(
    book_id: str,
    body: AIExpandRequest,
    current_user: dict = Depends(track_ai_usage),
) -> AIExpandResponse:
    """Expand selected text using tier-appropriate LLM. All tiers have access; model quality differs."""
    user_id: str = current_user.get("user_id", "")
    tier: str = current_user.get("subscription", {}).get("tier", "free")
    if tier not in ("free", "plus", "pro"):
        tier = "free"

    # Ownership check
    try:
        book = await books_collection.find_one({"_id": ObjectId(book_id), "deleted_at": None})
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid book ID.")
    if not book or book.get("user_id") != user_id:
        raise HTTPException(status_code=404, detail="Book not found.")

    # Character limits: Free=500, Plus=2000, Pro=unlimited
    char_limits: dict = {"free": 500, "plus": 2000}
    limit = char_limits.get(tier)
    if limit and len(body.selected_text) > limit:
        raise HTTPException(
            status_code=400,
            detail=f"Selected text exceeds the {limit}-character limit for your plan.",
        )

    llm_client = get_client_for_tier(tier)
    if llm_client is None:
        raise HTTPException(status_code=503, detail="AI service unavailable. API key not configured.")

    system_prompt = prompt_manager.get_prompt("nowry-book-expand")
    user_prompt = (
        f"Instruction: {body.instruction}\n\n"
        f"Text to expand:\n{body.selected_text}"
    )

    client = get_langfuse_client()
    model_name = TIER_MODEL_NAMES.get(tier, TIER_MODEL_NAMES["free"])
    trace_metadata = {"feature": "book_expand", "tier": tier, "user_id": user_id, "model": model_name}

    raw_text: str = ""
    last_exc = None
    for attempt in range(1, 3):
        try:
            if client:
                try:
                    with propagate_attributes(
                        user_id=user_id,
                        trace_name="book_expand",
                        metadata=trace_metadata,
                        tags=["book_expand", tier],
                    ):
                        with client.start_as_current_observation(
                            name="book_expand",
                            as_type="generation",
                            model=model_name,
                            input=[
                                {"role": "system", "content": system_prompt},
                                {"role": "user", "content": user_prompt},
                            ],
                            model_parameters={"temperature": 0.7, "max_tokens": 1500},
                        ) as generation:
                            if tier == "free":
                                completion = await asyncio.to_thread(llm_client.chat.completions.create,
                                    model=_GROQ_MODEL,
                                    max_tokens=1500,
                                    temperature=0.7,
                                    messages=[
                                        {"role": "system", "content": system_prompt},
                                        {"role": "user", "content": user_prompt},
                                    ],
                                )
                                raw_text = (completion.choices[0].message.content or "").strip()
                                usage = getattr(completion, "usage", None)
                                usage_details = (
                                    {
                                        "input": getattr(usage, "prompt_tokens", 0),
                                        "output": getattr(usage, "completion_tokens", 0),
                                        "total": getattr(usage, "total_tokens", 0),
                                    }
                                    if usage
                                    else None
                                )
                            else:
                                combined_prompt = f"{system_prompt}\n\n{user_prompt}"
                                completion = await asyncio.to_thread(llm_client.request, combined_prompt)
                                raw_text = (completion.choices[0].message.content or "").strip()
                                usage_details = None

                            # D-13: full output, no truncation
                            generation.update(output=raw_text, usage_details=usage_details)
                except Exception as langfuse_exc:
                    logger.warning(
                        f"[ai_expand] Langfuse tracing failed, continuing without trace: {langfuse_exc}"
                    )
                    if tier == "free":
                        completion = await asyncio.to_thread(llm_client.chat.completions.create,
                            model=_GROQ_MODEL,
                            max_tokens=1500,
                            temperature=0.7,
                            messages=[
                                {"role": "system", "content": system_prompt},
                                {"role": "user", "content": user_prompt},
                            ],
                        )
                        raw_text = (completion.choices[0].message.content or "").strip()
                    else:
                        combined_prompt = f"{system_prompt}\n\n{user_prompt}"
                        completion = await asyncio.to_thread(llm_client.request, combined_prompt)
                        raw_text = (completion.choices[0].message.content or "").strip()
            else:
                if tier == "free":
                    completion = await asyncio.to_thread(llm_client.chat.completions.create,
                        model=_GROQ_MODEL,
                        max_tokens=1500,
                        temperature=0.7,
                        messages=[
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_prompt},
                        ],
                    )
                    raw_text = (completion.choices[0].message.content or "").strip()
                else:
                    combined_prompt = f"{system_prompt}\n\n{user_prompt}"
                    completion = await asyncio.to_thread(llm_client.request, combined_prompt)
                    raw_text = (completion.choices[0].message.content or "").strip()

            if raw_text:
                break
            logger.warning(f"[ai_expand] LLM returned empty on attempt {attempt} (tier={tier}).")
        except Exception as exc:
            last_exc = exc
            logger.warning(f"[ai_expand] LLM API error on attempt {attempt} (tier={tier}): {exc}")

    if not raw_text:
        logger.error(f"[ai_expand] Failed after retries. last_exc={last_exc}")
        raise HTTPException(status_code=502, detail="AI service error. Please try again.")

    return AIExpandResponse(expanded_text=raw_text)
