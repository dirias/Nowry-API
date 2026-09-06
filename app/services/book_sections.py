"""Sections of a document, for the book→cards link (docs/prd-book-cards.md, D2/D3/D5).

A section is an H1 or H2 heading and the text down to the next heading of the same or
higher level; H3s belong to their H2. A section with fewer than MIN_SECTION_WORDS words
of body is not a section for generation or counting. A document with no headings is one
section named after the document. Identity is heading text plus ordinal (D3); `hash`
is what "changed since its cards" compares (D5).
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, asdict
from typing import Any, Iterable, Optional

MIN_SECTION_WORDS: int = 40
# The 20-card cap per run applies per section on Plus (docs/prd-book-cards.md D10).
PLUS_CARDS_PER_SECTION: int = 20
SECTION_HEADING_TAGS: tuple[str, ...] = ("h1", "h2")
HASH_LENGTH: int = 12


@dataclass
class Section:
    index: int
    heading: str
    level: str          # "h1" | "h2" | "doc"
    text: str
    words: int
    hash: str

    def stamp(self) -> dict:
        """The value a generated card carries as `source_section`."""
        return {"heading": self.heading, "index": self.index, "hash": self.hash}

    def public(self) -> dict:
        d = asdict(self)
        d.pop("text")
        return d


def _node_text(node: Any) -> str:
    """All text under a Lexical node, joined with spaces."""
    parts: list[str] = []

    def walk(n: Any) -> None:
        if isinstance(n, list):
            for child in n:
                walk(child)
        elif isinstance(n, dict):
            if n.get("type") == "text" and "text" in n:
                parts.append(str(n["text"]))
            for key in ("children", "root"):
                if key in n:
                    walk(n[key])

    walk(node)
    return " ".join(p for p in parts if p)


def _word_count(text: str) -> int:
    return len([w for w in re.split(r"\s+", text.strip()) if w])


def section_hash(text: str) -> str:
    """A change-detection fingerprint (D5), not a security hash."""
    normalised = re.sub(r"\s+", " ", text).strip().lower()
    digest = hashlib.sha1(normalised.encode("utf-8"), usedforsecurity=False)
    return digest.hexdigest()[:HASH_LENGTH]


def _top_level_blocks(lexical_state: Any) -> list[dict]:
    root = lexical_state.get("root") if isinstance(lexical_state, dict) else None
    children = root.get("children") if isinstance(root, dict) else None
    return [c for c in (children or []) if isinstance(c, dict)]


def parse_lexical(raw_content: Optional[str]) -> Optional[dict]:
    """The Lexical state, or None when the content is legacy HTML / plain text."""
    if not raw_content:
        return None
    try:
        state = json.loads(raw_content)
    except (json.JSONDecodeError, TypeError):
        return None
    return state if isinstance(state, dict) and "root" in state else None


def split_sections(lexical_state: Optional[dict], document_title: str = "") -> list[Section]:
    """Split a Lexical state into sections per D2. Short sections are dropped, so the
    returned indexes are ordinals among the sections that exist, in document order."""
    blocks = _top_level_blocks(lexical_state) if lexical_state else []
    raw: list[tuple[str, str, list[str]]] = []  # (heading, level, text parts)
    current: Optional[tuple[str, str, list[str]]] = None
    preamble: list[str] = []

    for block in blocks:
        tag = str(block.get("tag") or "").lower()
        if block.get("type") == "heading" and tag in SECTION_HEADING_TAGS:
            if current is not None:
                raw.append(current)
            current = (_node_text(block).strip() or document_title or "", tag, [])
            continue
        text = _node_text(block).strip()
        if not text:
            continue
        if current is None:
            preamble.append(text)
        else:
            current[2].append(text)
    if current is not None:
        raw.append(current)

    sections: list[Section] = []
    if not raw:
        # No headings: the whole document is one section named after it.
        text = " ".join(preamble)
        if _word_count(text) > 0:
            sections.append(Section(0, document_title or "", "doc", text, _word_count(text), section_hash(text)))
        return sections

    pre_text = " ".join(preamble)
    if _word_count(pre_text) >= MIN_SECTION_WORDS:
        sections.append(Section(0, document_title or "", "doc", pre_text, _word_count(pre_text), section_hash(pre_text)))
    for heading, level, parts in raw:
        text = " ".join(parts)
        words = _word_count(text)
        if words < MIN_SECTION_WORDS:
            continue
        sections.append(Section(len(sections), heading, level, text, words, section_hash(text)))
    return sections


def document_stats(lexical_state: Optional[dict], raw_content: Optional[str] = None) -> dict:
    """`word_count` and `section_count` for a book document, computed on save (FR-004)."""
    if lexical_state:
        text = _node_text(lexical_state)
        sections = split_sections(lexical_state)
        return {"word_count": _word_count(text), "section_count": len(sections)}
    text = re.sub(r"<[^>]+>", " ", raw_content or "")
    return {"word_count": _word_count(text), "section_count": 1 if _word_count(text) else 0}


def annotate_with_cards(sections: Iterable[Section], cards_by_heading: dict[str, list[dict]]) -> list[dict]:
    """Attach `cards` and `changed` to each section from the cards that carry its heading (D3, D5)."""
    out: list[dict] = []
    for section in sections:
        cards = cards_by_heading.get(section.heading, [])
        changed = any((c.get("source_section") or {}).get("hash") not in (None, section.hash) for c in cards)
        row = section.public()
        row["cards"] = len(cards)
        row["changed"] = bool(cards) and changed
        out.append(row)
    return out
