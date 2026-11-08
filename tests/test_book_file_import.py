"""QA-019: book import on fixtures built in the test, with a size cap.

``app/utils/file_import.py`` parses untrusted PDF, docx and txt uploads and had
never run under test. The fixtures are generated here (PyMuPDF writes the PDF,
``zipfile`` the docx), so nothing binary is checked in, and the route is walked
in preview and save mode, then with an empty file, a malformed PDF, an unknown
extension and a file past the cap, which is refused before any parser runs.
"""
import io
import zipfile
from unittest.mock import patch

import pytest

pytest.importorskip("mongomock_motor")

from app.utils.file_import import process_uploaded_file  # noqa: E402
from tests._fake_world import fake_world  # noqa: E402

PARAGRAPH = "The inner membrane folds into cristae, which increase the surface available for the electron transport chain."


def pdf_bytes(pages: int = 2) -> bytes:
    import fitz
    doc = fitz.open()
    for n in range(pages):
        page = doc.new_page()
        page.insert_text((72, 72), f"Page {n + 1}. {PARAGRAPH} " * 3, fontsize=11)
    return doc.tobytes()


def docx_bytes(paragraphs=(PARAGRAPH, "Each turn of the rotor yields three ATP.")) -> bytes:
    body = "".join(f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>" for text in paragraphs)
    document = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                f'<w:body>{body}</w:body></w:document>')
    types = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
             '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
             '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
             '<Default Extension="xml" ContentType="application/xml"/>'
             '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
             '</Types>')
    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
            '</Relationships>')
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", types)
        archive.writestr("_rels/.rels", rels)
        archive.writestr("word/document.xml", document)
    return buffer.getvalue()


def txt_bytes() -> bytes:
    return ("\n\n".join([PARAGRAPH] * 40)).encode()


# ── the parser ──────────────────────────────────────────────────────────────

def test_a_pdf_yields_one_page_per_page_with_metadata():
    pages = process_uploaded_file("mito.pdf", pdf_bytes(2))
    assert [p["page_number"] for p in pages] == [1, 2]
    assert "cristae" in pages[0]["content"] and pages[0]["format"] == "html"
    meta = pages[0]["extraction_metadata"]
    assert meta["total_words"] > 30 and meta["total_pages"] == 2


def test_a_docx_yields_html_pages():
    pages = process_uploaded_file("mito.docx", docx_bytes())
    assert pages and "cristae" in pages[0]["content"] and "<p>" in pages[0]["content"]


def test_a_txt_is_split_into_pages():
    pages = process_uploaded_file("mito.txt", txt_bytes())
    assert len(pages) >= 1 and "cristae" in pages[0]["content"]


@pytest.mark.parametrize("name, content", [
    ("empty.pdf", b""),
    ("broken.pdf", b"%PDF-1.4 this is not a pdf"),
    ("empty.docx", b""),
    ("broken.docx", b"PK\x03\x04 not a docx"),
    ("photo.png", b"\x89PNG"),
])
def test_what_cannot_be_read_yields_no_pages(name, content):
    assert process_uploaded_file(name, content) == []


# ── the route ───────────────────────────────────────────────────────────────

@pytest.fixture
def world():
    with fake_world() as w:
        uid = w.new_user("reader")
        w.act_as(uid)
        yield w


def _upload(world, name, content, **form):
    return world.http.post("/book/import", files={"file": (name, content, "application/octet-stream")}, data=form)


def test_preview_describes_the_file_without_saving(world):
    response = _upload(world, "mito.pdf", pdf_bytes(2), preview="true")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["preview"] is True and body["title"] == "mito" and body["total_pages"] == 2
    assert body["file_info"] == {"original_filename": "mito.pdf", "type": "PDF", "size": len(pdf_bytes(2))}
    assert len(body["sample_pages"]) == 2 and "cristae" in body["sample_pages"][0]["content_preview"]
    assert world.run(world.raw["books"].count_documents({})) == 0


def test_save_creates_the_book_with_its_pages(world):
    response = _upload(world, "mito.docx", docx_bytes(), title="Mitochondria", username="Ana")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["title"] == "Mitochondria" and body["author"] == "Ana" and body["page_count"] >= 1
    stored = world.run(world.raw["books"].find_one({}))
    assert stored["title"] == "Mitochondria" and stored["source"] == "imported"
    assert "cristae" in stored["full_content"] and stored["user_id"] == world.current["user"]["user_id"]


@pytest.mark.parametrize("name, content", [("empty.txt", b""), ("broken.pdf", b"%PDF-1.4 nope"), ("photo.png", b"\x89PNG")])
def test_an_unreadable_upload_is_400(world, name, content):
    response = _upload(world, name, content)
    assert response.status_code == 400
    assert world.run(world.raw["books"].count_documents({})) == 0


def test_an_oversize_upload_is_refused_before_parsing(world):
    with patch("app.routers.books.BOOK_IMPORT_MAX_BYTES", 1024), \
            patch("app.utils.file_import.process_uploaded_file") as parse:
        response = _upload(world, "big.txt", b"x" * 4096)
    assert response.status_code == 413
    parse.assert_not_called()
