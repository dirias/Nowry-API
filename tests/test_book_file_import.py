"""QA-019: book import on fixtures built in the test, with a size cap.

``app/utils/file_import.py`` parses untrusted PDF, docx and txt uploads and had
never run under test. The fixtures are generated here (PyMuPDF writes the PDF,
``zipfile`` the docx), so nothing binary is checked in, and the route is walked
in preview and save mode, then with an empty file, a malformed PDF, an unknown
extension and a file past the cap, which is refused before any parser runs.

MEDIA-006 (ADR-062 point 5): a PDF and a docx each carrying a picture yield one
image node whose source is the stored URL; no data URL enters the document.
"""
import io
import json
import zipfile
from unittest.mock import patch

import pytest

pytest.importorskip("mongomock_motor")

from app.utils.file_import import process_uploaded_file, process_uploaded_file_with_media  # noqa: E402
from app.utils.html_to_lexical import html_to_lexical_json  # noqa: E402
from tests._fake_world import fake_world  # noqa: E402

PARAGRAPH = "The inner membrane folds into cristae, which increase the surface available for the electron transport chain."
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def png_bytes() -> bytes:
    """A tiny grey PNG, drawn by PyMuPDF."""
    import fitz
    pixmap = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 8, 6), 0)
    pixmap.clear_with(200)
    return pixmap.tobytes("png")


def pdf_with_picture_bytes() -> bytes:
    import fitz
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), f"Before the picture. {PARAGRAPH}", fontsize=11)
    page.insert_image(fitz.Rect(72, 120, 272, 270), stream=png_bytes())
    page.insert_text((72, 300), "After the picture.", fontsize=11)
    return doc.tobytes()


_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
_PIC = "http://schemas.openxmlformats.org/drawingml/2006/picture"


def docx_with_picture_bytes(alt: str = "A membrane") -> bytes:
    """A docx with a paragraph, an inline picture (rId2 → word/media/image1.png) and a paragraph."""
    drawing = (f'<w:p><w:r><w:drawing><wp:inline xmlns:wp="{_WP}"><wp:extent cx="1905000" cy="1428750"/>'
               f'<wp:docPr id="1" name="Picture 1" descr="{alt}"/>'
               f'<a:graphic xmlns:a="{_A}"><a:graphicData uri="{_PIC}"><pic:pic xmlns:pic="{_PIC}">'
               '<pic:nvPicPr><pic:cNvPr id="1" name="image1.png"/><pic:cNvPicPr/></pic:nvPicPr>'
               f'<pic:blipFill><a:blip r:embed="rId2" xmlns:r="{_R}"/></pic:blipFill>'
               '<pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="1905000" cy="1428750"/></a:xfrm>'
               '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr>'
               '</pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>')
    body = (f"<w:p><w:r><w:t>Before the picture. {PARAGRAPH}</w:t></w:r></w:p>" + drawing
            + "<w:p><w:r><w:t>After the picture.</w:t></w:r></w:p>")
    document = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                f'<w:document xmlns:w="{_W}" xmlns:r="{_R}"><w:body>{body}</w:body></w:document>')
    types = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
             '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
             '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
             '<Default Extension="xml" ContentType="application/xml"/>'
             '<Default Extension="png" ContentType="image/png"/>'
             '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
             '</Types>')
    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
            '</Relationships>')
    document_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                     '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                     f'<Relationship Id="rId2" Type="{_R}/image" Target="media/image1.png"/>'
                     '</Relationships>')
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", types)
        archive.writestr("_rels/.rels", rels)
        archive.writestr("word/document.xml", document)
        archive.writestr("word/_rels/document.xml.rels", document_rels)
        archive.writestr("word/media/image1.png", png_bytes())
    return buffer.getvalue()


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


# ── MEDIA-006: imports keep their pictures ──────────────────────────────────

class FakeStorage:
    def __init__(self, fail: bool = False):
        self.uploads, self.fail = [], fail

    async def upload(self, file_content, filename, folder="", **options):
        if self.fail:
            raise RuntimeError("cloud down")
        self.uploads.append({"folder": folder, "filename": filename, "head": file_content[:8]})
        public_id = f"{folder}/img{len(self.uploads)}"
        return {"url": f"http://cdn.test/{public_id}", "secure_url": f"https://cdn.test/{public_id}.png",
                "public_id": public_id, "width": 320, "height": 240, "format": "png", "bytes": len(file_content)}

    def get_thumbnail_url(self, public_id, width=200, height=200):
        return f"https://cdn.test/thumb/{public_id}"


@pytest.mark.parametrize("name, content", [("mito.pdf", pdf_with_picture_bytes()), ("mito.docx", docx_with_picture_bytes())])
def test_a_picture_comes_out_as_bytes_beside_a_marker_never_inlined(name, content):
    pages, media = process_uploaded_file_with_media(name, content)
    html = "".join(p["content"] for p in pages)
    assert "Before the picture" in html and "After the picture" in html
    assert 'data-media="1"' in html and "data:" not in html
    assert len(media) == 1 and media[0]["index"] == 1 and media[0]["bytes"].startswith(PNG_MAGIC)
    assert media[0]["ext"] == "png" and pages[0]["extraction_metadata"]["pictures"] == 1
    assert process_uploaded_file(name, content)[0]["content"] == pages[0]["content"]


def test_the_docx_picture_keeps_its_alt_text():
    pages, media = process_uploaded_file_with_media("mito.docx", docx_with_picture_bytes(alt="A membrane"))
    assert media[0]["alt"] == "A membrane" and 'alt="A membrane"' in pages[0]["content"]


def test_the_converter_turns_an_img_into_a_root_level_image_node():
    lexical = html_to_lexical_json(
        '<p>Before <img src="https://cdn.test/a.png" width="320" height="240" alt="A membrane"> after</p>'
        '<p><img src="data:image/png;base64,AAAA"></p><img data-media="3" alt="never stored"><p>End</p>'
    )
    children = lexical["root"]["children"]
    assert [c["type"] for c in children] == ["paragraph", "image", "paragraph", "paragraph"]
    assert children[1] == {"type": "image", "src": "https://cdn.test/a.png", "altText": "A membrane",
                           "width": 320, "height": 240, "alignment": "center", "maxWidth": 600, "version": 1}
    assert children[0]["children"][0]["text"] == "Before" and children[2]["children"][0]["text"] == "after"
    unsized = html_to_lexical_json('<img src="https://cdn.test/b.png">')["root"]["children"][0]
    assert (unsized["width"], unsized["height"], unsized["altText"]) == (None, None, "")


@pytest.mark.parametrize("name, content", [("mito.pdf", pdf_with_picture_bytes()), ("mito.docx", docx_with_picture_bytes())])
def test_an_imported_picture_is_stored_under_the_new_book_and_becomes_an_image_node(world, name, content):
    fake = FakeStorage()
    with patch("app.routers.books.get_storage_backend", return_value=fake):
        response = _upload(world, name, content, title="Pictures")
    assert response.status_code == 200, response.text
    book_id = response.json()["_id"]
    user = world.current["user"]["user_id"]
    stored = world.run(world.raw["books"].find_one({}))
    assert str(stored["_id"]) == book_id and stored["title"] == "Pictures"
    assert "data:" not in stored["full_content"] and "data-media" not in stored["full_content"]
    images = [c for c in json.loads(stored["full_content"])["root"]["children"] if c["type"] == "image"]
    assert len(images) == 1
    assert images[0]["src"] == f"https://cdn.test/nowry/{user}/{book_id}/img1.png"
    assert (images[0]["width"], images[0]["height"]) == (320, 240)
    assert fake.uploads[0]["folder"] == f"nowry/{user}/{book_id}" and fake.uploads[0]["head"] == PNG_MAGIC
    rows = world.run(world.raw["media"].find({}).to_list(length=10))
    assert len(rows) == 1 and (rows[0]["user_id"], rows[0]["book_id"]) == (user, book_id)
    assert rows[0]["public_id"] == f"nowry/{user}/{book_id}/img1"


def test_a_picture_that_cannot_be_stored_is_dropped_and_the_import_goes_on(world):
    with patch("app.routers.books.get_storage_backend", return_value=FakeStorage(fail=True)):
        response = _upload(world, "mito.pdf", pdf_with_picture_bytes())
    assert response.status_code == 200, response.text
    stored = world.run(world.raw["books"].find_one({}))
    assert "data:" not in stored["full_content"] and "After the picture" in stored["full_content"]
    assert not [c for c in json.loads(stored["full_content"])["root"]["children"] if c["type"] == "image"]
    assert world.run(world.raw["media"].count_documents({})) == 0


def test_a_preview_stores_nothing_and_shows_no_marker(world):
    fake = FakeStorage()
    with patch("app.routers.books.get_storage_backend", return_value=fake):
        response = _upload(world, "mito.docx", docx_with_picture_bytes(), preview="true")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["metadata"]["pictures"] == 1
    assert "data-media" not in body["sample_pages"][0]["content_preview"]
    assert fake.uploads == [] and world.run(world.raw["media"].count_documents({})) == 0
