"""Fields a document carries that are derived from the rest of it, and how they stay true (ADR-069).

Three things about a book are computed, stored, and read by queries that must
not parse content to answer:

- its **stats** (`word_count`, `section_count`), whose rule changed with
  ADR-064 — a count stored under the old rule is wrong until recomputed;
- its **search terms**, the folded words the library's search matches, which
  no document had before IMPORT-036;
- for an import made before ADR-067, **where it came from**, which lived only
  inside an English sentence in `summary`.

Each has a version stamp. A write computes them; a document whose stamp is
behind is brought up to date by `run_pass`, a background pass that works in
small batches, is safe to stop at any point and to run twice, and ends when no
document is behind. Nothing here reads more than one batch at a time.
"""
from __future__ import annotations

import asyncio
import os
import re
import unicodedata
from typing import Any, Optional

from app.services.book_sections import document_stats, parse_lexical
from app.utils.logger import get_logger

logger = get_logger(__name__)

#: The rule the stored counts were computed with. 2 = counted by script (ADR-064).
STATS_VERSION: int = 2
#: The rule the stored search terms were computed with.
SEARCH_VERSION: int = 1

BATCH: int = int(os.getenv("BOOK_MAINTENANCE_BATCH", "25"))
PAUSE_SECONDS: float = float(os.getenv("BOOK_MAINTENANCE_PAUSE_SECONDS", "0.5"))
MAX_TERMS: int = 400
MAX_WORD: int = 40

# ── search terms ─────────────────────────────────────────────────────────────

_LATIN_MARKS = re.compile("[̀-ͯ]")
_WORD = re.compile(r"[^\W_]+", re.UNICODE)
_LIGATURES = str.maketrans({"œ": "oe", "æ": "ae", "ø": "o", "đ": "d", "ł": "l"})
# Katakana to Hiragana: the two syllabaries spell the same sounds.
_KANA = {code: code - 0x60 for code in range(0x30A1, 0x30F7)}


def fold(text: str) -> str:
    """Text as the search compares it: no case, no Latin accents, one width, one kana.

    "Álgebra", "ALGEBRA" and "algebra" fold alike; so do "ﾉｰﾄ", "ノート" and
    "のーと", "ß" and "ss", "cœur" and "coeur". Voicing marks on kana are kept:
    が and か are different sounds.
    """
    decomposed = unicodedata.normalize("NFKD", text or "")
    stripped = _LATIN_MARKS.sub("", decomposed)
    return unicodedata.normalize("NFKC", stripped).casefold().translate(_LIGATURES).translate(_KANA)


def query_terms(needle: str) -> list[str]:
    """The folded words of what the reader typed."""
    return [word[:MAX_WORD] for word in _WORD.findall(fold(needle))]


def search_terms(title: Optional[str], author: Optional[str] = None, source: Optional[str] = None) -> list[str]:
    """Every suffix of every folded word of the title (and of an import's author).

    A search for "gebra" or "ノート" is then a *prefix* match against one of the
    terms, which an index answers; a "contains" match against the title itself
    could only be answered by reading every title.
    """
    words = _WORD.findall(fold(title or ""))
    if source == "imported":
        words += _WORD.findall(fold(author or ""))
    terms: list[str] = []
    seen: set[str] = set()
    for word in words:
        word = word[:MAX_WORD]
        for start in range(len(word)):
            term = word[start:]
            if term not in seen:
                seen.add(term)
                terms.append(term)
            if len(terms) >= MAX_TERMS:
                return terms
    return terms


# ── an older import's facts ─────────────────────────────────────────────────

_LEGACY_SUMMARY = re.compile(r"^Imported from (.+) - (\d+) pages$")


def legacy_import_facts(book: dict) -> dict:
    """`source_filename` and `page_count` read out of the sentence an older import stored.

    Empty when the document is not such an import or already carries the facts.
    """
    if book.get("source") != "imported" or book.get("source_filename"):
        return {}
    match = _LEGACY_SUMMARY.match(book.get("summary") or "")
    if not match:
        return {}
    facts: dict[str, Any] = {"source_filename": match.group(1)[:255]}
    if not book.get("page_count"):
        facts["page_count"] = int(match.group(2))
    return facts


# ── what a write stores ─────────────────────────────────────────────────────

def stats_fields(full_content: Optional[str]) -> dict:
    """The stats of this content, stamped with the rule that counted them."""
    content = full_content or ""
    return {**document_stats(parse_lexical(content), content), "stats_version": STATS_VERSION}


def search_fields(title: Optional[str], author: Optional[str], source: Optional[str]) -> dict:
    return {"search_terms": search_terms(title, author, source), "search_version": SEARCH_VERSION}


def is_behind(book: dict) -> bool:
    return book.get("stats_version") != STATS_VERSION or book.get("search_version") != SEARCH_VERSION


def upgrade_fields(book: dict) -> dict:
    """Everything a stored document needs set to be current. `book` carries `full_content` when stats are behind."""
    fields: dict[str, Any] = {}
    if book.get("stats_version") != STATS_VERSION:
        try:
            fields.update(stats_fields(book.get("full_content")))
        except Exception:
            # A document that cannot be counted is stamped all the same, or the pass would never end.
            logger.warning("Book %s: stats could not be recomputed", book.get("_id"), exc_info=True)
            fields["stats_version"] = STATS_VERSION
    if book.get("search_version") != SEARCH_VERSION:
        fields.update(search_fields(book.get("title"), book.get("author"), book.get("source")))
    fields.update(legacy_import_facts(book))
    return fields


# ── the pass ────────────────────────────────────────────────────────────────

BEHIND = {"$or": [{"stats_version": {"$ne": STATS_VERSION}}, {"search_version": {"$ne": SEARCH_VERSION}}]}


async def run_batch(books: Any, batch: int = BATCH) -> int:
    """Bring up to `batch` documents up to date. Returns how many it found behind."""
    rows = await books.find(BEHIND).limit(batch).to_list(length=batch)
    for book in rows:
        fields = upgrade_fields(book)
        # Stamps are always written, so a row can never be found behind twice by the same rule.
        fields.setdefault("stats_version", STATS_VERSION)
        fields.setdefault("search_version", SEARCH_VERSION)
        await books.update_one({"_id": book["_id"]}, {"$set": fields})
    return len(rows)


async def run_pass(books: Any, batch: int = BATCH, pause: float = PAUSE_SECONDS) -> int:
    """Run batches until no document is behind. Safe to cancel between any two; the next run resumes."""
    total = 0
    try:
        while True:
            done = await run_batch(books, batch)
            total += done
            if done < batch:
                break
            await asyncio.sleep(pause)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.error("Book maintenance pass stopped after %d documents", total, exc_info=True)
        return total
    if total:
        logger.info("Book maintenance brought %d documents up to date", total)
    return total


#: Seconds between two passes. A pass that finds nothing behind costs one indexed query.
INTERVAL_SECONDS: int = int(os.getenv("BOOK_MAINTENANCE_INTERVAL_SECONDS", "3600"))


async def maintenance_loop(books: Any, interval: Optional[int] = None) -> None:
    """Run the pass at startup and again every `interval` seconds, for the life of the process.

    Once is not enough: during a deploy an older API instance may still be
    writing documents without stamps after this one's first pass has finished.
    """
    interval = INTERVAL_SECONDS if interval is None else interval
    while True:
        await run_pass(books)
        if interval <= 0:
            return
        await asyncio.sleep(interval)
