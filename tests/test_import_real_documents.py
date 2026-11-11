"""Round four, findings 8 to 14 (IMPORT-045 … IMPORT-050): what real documents need from the importer."""
import io
import zipfile

import pytest

pytest.importorskip("mongomock_motor")

from app.utils import file_import  # noqa: E402
from app.utils.file_import import process_uploaded_file  # noqa: E402


def _html(pages) -> str:
    return "\n".join(page["content"] for page in pages)


# ── IMPORT-045: headings by the document's own type scale ───────────────────

def test_the_type_scale_ranks_the_sizes_above_the_body():
    sizes = {10.9: 275_000, 9.0: 30_000, 13.1: 2_000, 14.3: 1_400, 20.7: 300, 24.9: 21}
    assert file_import._type_scale(sizes) == {24.9: "h1", 20.7: "h2", 14.3: "h3", 13.1: "h3"}
    assert file_import._type_scale({12.0: 5000, 14.0: 80}) == {14.0: "h2"}       # one heading size: sections are cut at it
    assert file_import._type_scale({12.0: 5000, 26.0: 40}) == {26.0: "h1"}
    assert file_import._type_scale({12.0: 5000}) == {} and file_import._type_scale({}) == {}
    assert file_import._type_scale({12.0: 5000, 15.0: 4000}) == {}                # a second body face, not a heading


def _manual() -> bytes:
    """A 12-point manual whose chapters are set at 14 and whose title is set at 16: nothing above the old thresholds."""
    import fitz
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 60), "A Small Manual", fontsize=16)
    y = 100
    for chapter in ("Getting started", "Going further"):
        page.insert_text((72, y), chapter, fontsize=14)
        y += 24
        for _ in range(9):
            page.insert_text((72, y), "The quick brown fox jumps over the lazy dog and keeps running along the river.", fontsize=12)
            y += 16
        y += 14
    return doc.tobytes()


def test_a_manual_set_in_small_type_still_has_chapters_and_sections():
    from app.services.book_sections import document_stats, parse_lexical
    from app.utils.import_worker import to_lexical

    html = _html(process_uploaded_file("manual.pdf", _manual()))
    assert "<h1>A Small Manual</h1>" in html and "<h2>Getting started</h2>" in html and "<h2>Going further</h2>" in html
    content = to_lexical(html)
    assert document_stats(parse_lexical(content), content)["section_count"] == 2


def _paper() -> bytes:
    """A troff-style paper: section headings are bold, numbered and the same size as the text."""
    import fitz
    doc = fitz.open()
    page = doc.new_page()
    y = 72
    for heading, deeper in (("1. Introduction", None), ("2. What is a shell?", "2.1. Command Interpreter")):
        page.insert_text((72, y), heading, fontsize=10, fontname="hebo")
        y += 22
        for _ in range(3):
            page.insert_text((72, y), "A shell is a macro processor that executes commands for the person typing.", fontsize=10)
            y += 13
        y += 12
        if deeper:
            page.insert_text((72, y), deeper, fontsize=10, fontname="hebo")
            y += 22
            page.insert_text((72, y), "1. the first step of a real numbered list", fontsize=10)
            y += 13
            page.insert_text((72, y), "2. the second step of that list", fontsize=10)
            y += 25
    return doc.tobytes()


def test_a_bold_numbered_line_at_body_size_is_a_heading_and_a_numbered_list_is_still_a_list():
    html = _html(process_uploaded_file("paper.pdf", _paper()))
    assert "<h2>1. Introduction</h2>" in html and "<h2>2. What is a shell?</h2>" in html
    assert "<h3>2.1. Command Interpreter</h3>" in html
    assert "<ol><li>the first step of a real numbered list</li><li>the second step of that list</li></ol>" in html


def test_a_contents_line_loses_its_dot_leader_and_is_never_a_heading():
    import fitz
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "3 Basic Shell Features . . . . . . . . . . . . . . . . . . 5", fontsize=16)
    for n in range(8):
        page.insert_text((72, 110 + n * 16), "Body text that sets the size most of the document is written in, line after line.", fontsize=11)
    html = _html(process_uploaded_file("toc.pdf", doc.tobytes()))
    assert "<p>3 Basic Shell Features … 5</p>" in html and ". . ." not in html and "<h" not in html
