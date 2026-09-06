"""BOOK-001 — sections of a document (docs/prd-book-cards.md D2, D3, D5).

Pins the splitting rules the counts and the per-section generation depend on.
"""
from __future__ import annotations

from app.services.book_sections import (
    MIN_SECTION_WORDS,
    annotate_with_cards,
    document_stats,
    parse_lexical,
    section_hash,
    split_sections,
)

WORDS = " ".join(f"w{i}" for i in range(MIN_SECTION_WORDS))  # exactly the floor


def text(s: str) -> dict:
    return {"type": "text", "text": s}


def para(s: str) -> dict:
    return {"type": "paragraph", "children": [text(s)]}


def heading(tag: str, s: str) -> dict:
    return {"type": "heading", "tag": tag, "children": [text(s)]}


def doc(*blocks) -> dict:
    return {"root": {"children": list(blocks)}}


def test_h1_and_h2_start_sections_and_h3_folds_into_its_h2():
    state = doc(
        heading("h1", "Grammar"), para(WORDS),
        heading("h2", "Particles"), para(WORDS), heading("h3", "は vs が"), para("short note " * 25),
        heading("h2", "Verbs"), para(WORDS),
    )
    sections = split_sections(state, "N3")
    assert [s.heading for s in sections] == ["Grammar", "Particles", "Verbs"]
    assert [s.index for s in sections] == [0, 1, 2]
    assert "は vs が" in sections[1].text and sections[1].words > MIN_SECTION_WORDS, "the H3 body belongs to Particles"


def test_a_short_section_is_not_a_section_and_indexes_stay_ordinal():
    state = doc(heading("h2", "Intro"), para("ten words only " * 3), heading("h2", "Body"), para(WORDS))
    sections = split_sections(state, "Doc")
    assert [(s.heading, s.index) for s in sections] == [("Body", 0)]


def test_no_headings_is_one_section_named_after_the_document():
    sections = split_sections(doc(para("just prose"), para("more prose")), "Photosynthesis notes")
    assert len(sections) == 1
    assert sections[0].heading == "Photosynthesis notes" and sections[0].level == "doc"


def test_the_preamble_is_a_section_only_when_it_has_forty_words():
    long_pre = split_sections(doc(para(WORDS), heading("h2", "A"), para(WORDS)), "Doc")
    short_pre = split_sections(doc(para("a few words"), heading("h2", "A"), para(WORDS)), "Doc")
    assert [s.heading for s in long_pre] == ["Doc", "A"]
    assert [s.heading for s in short_pre] == ["A"]


def test_hash_ignores_whitespace_and_case_but_not_words():
    assert section_hash("Particles  mark\n roles") == section_hash("particles mark roles")
    assert section_hash("particles mark roles") != section_hash("particles mark rolls")


def test_annotate_counts_cards_by_heading_and_flags_a_changed_section():
    state = doc(heading("h2", "Particles"), para(WORDS), heading("h2", "Verbs"), para(WORDS))
    sections = split_sections(state, "Doc")
    same = sections[0].hash
    rows = annotate_with_cards(sections, {
        "Particles": [{"source_section": {"heading": "Particles", "index": 0, "hash": same}}, {"source_section": {"heading": "Particles", "index": 0, "hash": "stale0000000"}}],
    })
    assert rows[0]["cards"] == 2 and rows[0]["changed"] is True
    assert rows[1]["cards"] == 0 and rows[1]["changed"] is False, "no cards means nothing to be out of date"
    assert "text" not in rows[0], "the section body never leaves the server"


def test_document_stats_and_legacy_html():
    state = doc(heading("h2", "A"), para(WORDS), heading("h2", "B"), para("tiny"))
    assert document_stats(state) == {"word_count": MIN_SECTION_WORDS + 3, "section_count": 1}
    assert document_stats(None, "<p>three words here</p>") == {"word_count": 3, "section_count": 1}
    assert parse_lexical("<p>legacy</p>") is None and parse_lexical(None) is None
