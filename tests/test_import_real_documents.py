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


# ── IMPORT-046: a right-to-left line is rebuilt from where its glyphs sit ────

def _raw_line(stored: list) -> dict:
    """A rawdict line from `(char, x)` pairs in the order the file stores them; each glyph is 6 points wide."""
    return {"spans": [{"size": 11, "chars": [{"c": c, "bbox": [x, 0, x + 6, 11]} for c, x in stored]}]}


def _drawn(visual: str, stored_order=None) -> dict:
    """`visual` is the line as it looks on the page, left to right; storage order defaults to right-to-left."""
    placed = [(char, index * 6) for index, char in enumerate(visual)]
    return _raw_line(stored_order(placed) if stored_order else list(reversed(placed)))


def test_latin_words_and_numbers_inside_hebrew_read_in_their_place():
    # On the page, left to right: "Apple גתומ לש"  — read right to left: "של מותג Apple".
    assert file_import._logical_rtl_text(_drawn("Apple גתומ לש")) == "של מותג Apple"
    # Mostly a Latin name: read from the right because the page is.
    assert file_import._logical_rtl_text(_drawn("macOS Tahoe 26 רובע"), base_rtl=True) == "עבור macOS Tahoe 26"
    assert file_import._logical_rtl_text(_drawn("macOS Tahoe 26 רובע")) is None


def test_the_storage_order_does_not_matter_only_the_positions_do():
    """Apple's licence stores the Latin word first although it is read last."""
    def latin_first(placed):
        latin = [p for p in placed if p[0].isascii() and p[0].isalpha()]
        return latin + [p for p in reversed(placed) if p not in latin]
    assert file_import._logical_rtl_text(_drawn("Apple גתומ לש", latin_first)) == "של מותג Apple"


def test_punctuation_and_brackets_come_out_as_typed():
    # Visual: ".(ןימי) הנושאר הדומע" — the brackets are stored as drawn, mirrored.
    assert file_import._logical_rtl_text(_drawn(".(ןימי) הנושאר הדומע")) == "עמודה ראשונה (ימין)."
    assert file_import._logical_rtl_text(_drawn(",APPLE גתומ לש")) == "של מותג APPLE,"
    assert file_import._logical_rtl_text(_drawn("(ok) דואמ בוט"), base_rtl=True) == "טוב מאוד (ok)"


def test_a_line_drawn_left_to_right_or_with_no_rtl_letters_is_left_as_stored():
    assert file_import._logical_rtl_text(_raw_line([(c, i * 6) for i, c in enumerate("עמודה ראשונה")])) is None
    assert file_import._logical_rtl_text(_drawn("plain English")) is None
    assert file_import._logical_rtl_text({"spans": []}) is None


def test_a_gap_with_no_stored_space_is_a_space():
    stored = [("ב", 60), ("א", 54), ("ד", 30), ("ג", 24)]   # two words, 18 points apart, no space glyph
    assert file_import._logical_rtl_text(_raw_line(stored)) == "בא דג"


# ── IMPORT-047: equations and text boxes are text the writer typed ──────────

_NS = ('xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
       'xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math" '
       'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
       'xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape"')


def _docx(body: str, prolog: str = "") -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                         '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                         '<Default Extension="xml" ContentType="application/xml"/>'
                         '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        archive.writestr("_rels/.rels", '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                         '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
        archive.writestr("word/document.xml", f'<?xml version="1.0"?>{prolog}<w:document {_NS}><w:body>{body}</w:body></w:document>')
    return out.getvalue()


def _p(text: str) -> str:
    return f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"


def _mr(text: str) -> str:
    return f"<m:r><m:t>{text}</m:t></m:r>"


def test_an_equation_is_imported_as_its_linear_text():
    fraction = f"<m:f><m:fPr/><m:num>{_mr('a+b')}</m:num><m:den>{_mr('2')}</m:den></m:f>"
    power = f"<m:sSup><m:e>{_mr('c')}</m:e><m:sup>{_mr('2')}</m:sup></m:sSup>"
    root = f"<m:rad><m:radPr/><m:deg/><m:e>{_mr('x')}</m:e></m:rad>"
    body = (_p("Energy:") + f"<w:p><w:r><w:t xml:space=\"preserve\">so </w:t></w:r><m:oMath>{_mr('E=m')}{power}</m:oMath></w:p>"
            + f"<w:p><m:oMathPara><m:oMath>{fraction}{_mr('+')}{root}</m:oMath></m:oMathPara></w:p>")
    html = _html(process_uploaded_file("physics.docx", _docx(body)))
    assert "so E=mc^2" in html and "(a+b)/2+√(x)" in html and "Energy:" in html


def test_a_text_box_without_fallback_markup_follows_its_paragraph():
    box = ("<w:p><w:r><w:t>Anchor paragraph.</w:t></w:r><w:r><mc:AlternateContent><mc:Choice Requires=\"wps\"><wps:wsp><wps:txbx>"
           f"<w:txbxContent>{_p('Inside the box.')}{_p('Second line in the box.')}</w:txbxContent></wps:txbx></wps:wsp></mc:Choice>"
           "</mc:AlternateContent></w:r></w:p>")
    html = _html(process_uploaded_file("boxed.docx", _docx(box + _p("After."))))
    order = [html.index(text) for text in ("Anchor paragraph.", "Inside the box.", "Second line in the box.", "After.")]
    assert order == sorted(order)


def test_a_document_with_neither_is_not_rewritten_and_one_with_a_doctype_is_left_alone():
    plain = _docx(_p("Nothing special."))
    assert file_import._with_unread_text(plain) is plain
    declared = _docx(f"<w:p><m:oMath>{_mr('x')}</m:oMath></w:p>", prolog='<!DOCTYPE d [<!ENTITY a "b">]>')
    assert file_import._with_unread_text(declared) is declared
    assert file_import._with_unread_text(b"not a zip") == b"not a zip"


# ── IMPORT-048: the reason given is the reason; an extension is not trusted ──

def test_a_word_file_the_reader_cannot_read_is_unreadable_not_empty():
    from app.utils.file_import import refusal_code
    nested = "".join("<w:tbl><w:tr><w:tc>" for _ in range(3000)) + _p("deep") + "".join("</w:tc></w:tr></w:tbl>" for _ in range(3000))
    assert refusal_code("deep.docx", _docx(nested)) == "import_unreadable"
    assert refusal_code("blank.docx", _docx("")) == "import_empty"


def test_a_file_that_is_not_what_its_name_says_is_refused_as_unreadable():
    import fitz
    from app.utils.import_worker import read_file

    png = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 8, 8), 0).tobytes("png")
    assert read_file("photo.pdf", png)["refusal"] == "import_unreadable"
    assert read_file("notes.docx", b"%PDF-1.7 not a word file")["refusal"] == "import_unreadable"
    assert file_import.wears_the_wrong_extension("ok.pdf", b"junk before\n%PDF-1.4 ...") is False
    assert file_import.wears_the_wrong_extension("empty.pdf", b"") is False        # empty has its own sentence
    assert file_import.wears_the_wrong_extension("notes.txt", b"\x89PNG") is False  # text is judged by its bytes elsewhere


def test_the_reason_for_an_empty_answer_comes_back_with_it():
    from app.utils.import_worker import read_file
    assert read_file("empty.txt", b"")["refusal"] == "import_empty"
    assert read_file("old.doc", b"\xd0\xcf\x11\xe0")["refusal"] == "import_legacy_doc"
    assert "refusal" not in read_file("a.txt", b"some words here")


# ── IMPORT-049: titles and authors are one bounded, printable line ──────────

from tests._fake_world import fake_world  # noqa: E402


@pytest.fixture
def world():
    with fake_world() as w:
        w.act_as(w.new_user("reader"))
        yield w


def _import(world, name, content=b"some words to import here", **form):
    return world.http.post("/book/import", files={"file": (name, content, "application/octet-stream")}, data=form)


def _stored(world):
    return world.run(world.raw["books"].find_one({}, sort=[("_id", -1)]))


def test_clean_label_keeps_one_printable_line():
    from app.utils.labels import clean_label, import_title
    assert clean_label("  a\x00b\n c\t ", 50) == "a b c"
    assert clean_label("report\u202etxt.exe", 50) == "report txt.exe"     # no override left to reverse the name
    assert clean_label("x" * 500, 200) == "x" * 200 and clean_label(None, 10) == ""
    assert clean_label("日本語のノート 📚", 50) == "日本語のノート 📚"
    assert import_title("  ", "notes.final.txt") == "notes.final"
    assert import_title(None, ".txt") == ".txt" and import_title("Mine", "x.txt") == "Mine"


def test_an_import_never_has_an_empty_or_endless_title(world):
    assert _import(world, ".txt").status_code == 200 and _stored(world)["title"] == ".txt"
    assert _import(world, "   .txt", b"other words to import now").status_code == 200 and _stored(world)["title"] == ".txt"
    assert _import(world, "n" * 300 + ".txt", b"third file of words").status_code == 200 and len(_stored(world)["title"]) == 200
    assert _import(world, "a.txt", b"fourth file of words", title="T" * 5000).status_code == 200 and len(_stored(world)["title"]) == 200
    assert _import(world, "a\x00b.txt", b"fifth file of words").status_code == 200 and "\x00" not in _stored(world)["title"]


def test_the_author_of_an_import_is_the_account_not_the_form(world):
    account = world.current["user"].get("username")
    assert _import(world, "a.txt", username="<script>x</script>" + "U" * 5000).status_code == 200
    stored = _stored(world)
    if account:
        assert stored["author"] == account
    assert len(stored["author"]) <= 100 and "U" * 101 not in stored["author"]


def test_created_and_edited_titles_are_cleaned_and_bounded(world):
    created = world.http.post("/book/create", json={"title": "  My\u202e notes\n ", "author": "A" * 400}).json()
    assert created["title"] == "My notes" and len(created["author"]) == 100
    edited = world.http.put(f"/book/edit/{created['_id']}", json={"title": "Z" * 900}).json()
    assert len(edited["title"]) == 200
