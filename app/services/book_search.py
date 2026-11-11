"""What the library's search compares (ADR-069).

A document stores `search_terms`: every suffix of every folded word of its
title, and of an import's author. A query is folded the same way and each of
its words must *begin* one of the terms, so the match ignores case, Latin
accents, width and kana, and is answered by an index instead of by reading
every title. The terms are computed wherever a title is written.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Optional

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


def search_fields(title: Optional[str], author: Optional[str], source: Optional[str]) -> dict:
    """What a write stores so the document can be found."""
    return {"search_terms": search_terms(title, author, source)}
