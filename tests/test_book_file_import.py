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


# ── IMPORT-001..004: an import loses nothing and can be saved again ─────────

def _walk(node):
    yield node
    for child in node.get("children", []):
        yield from _walk(child)


def _text(node) -> str:
    return " ".join(n["text"] for n in _walk(node) if n.get("type") == "text")


def test_every_node_the_converter_emits_carries_the_fields_the_editor_reads():
    """IMPORT-001: a list item without `indent` made the editor stop parsing at the first list."""
    root = html_to_lexical_json("<h2>Organelles</h2><p>Intro <b>bold</b></p><ul><li>Nucleus</li><li>Ribosome</li></ul>")["root"]
    for node in _walk(root):
        assert node.get("version") == 1, node
        if node["type"] in ("paragraph", "heading", "list", "listitem"):
            assert isinstance(node["indent"], int) and node["format"] == "" and "direction" in node, node
        if node["type"] == "listitem":
            assert isinstance(node["value"], int) and node["value"] >= 1
        if node["type"] == "list":
            assert node["tag"] in ("ul", "ol") and node["start"] == 1
    assert [n["value"] for n in root["children"][2]["children"]] == [1, 2]


def test_an_item_with_a_nested_list_keeps_its_own_text_and_wraps_the_list():
    """IMPORT-001: 'Mitochondrion' used to vanish, and the nested list sat bare inside its parent."""
    root = html_to_lexical_json(
        "<ul><li>Nucleus</li><li>Mitochondrion<ul><li>Cristae</li><li>Matrix</li></ul></li><li>Ribosome</li></ul>"
    )["root"]
    (outer,) = root["children"]
    rows = outer["children"]
    assert [_text(row) for row in rows] == ["Nucleus", "Mitochondrion", "Cristae Matrix", "Ribosome"]
    wrapper = rows[2]
    assert [child["type"] for child in wrapper["children"]] == ["list"]
    assert all(child["type"] == "listitem" for child in outer["children"])
    assert [item["indent"] for item in wrapper["children"][0]["children"]] == [1, 1]
    assert [row["value"] for row in rows if row is not wrapper] == [1, 2, 3]


def test_a_paragraph_inside_a_list_item_is_the_items_text():
    """IMPORT-001: a footnote's `<li><p>…</p></li>` lost its text."""
    root = html_to_lexical_json('<ol><li id="footnote-1"><p>The note itself.</p></li></ol><p>After.</p>')["root"]
    assert _text(root["children"][0]) == "The note itself."
    assert root["children"][1]["type"] == "paragraph"


def test_the_page_splitter_returns_every_character_it_was_given():
    """IMPORT-002: text between two inline runs of a paragraph, and a list's closing tag, were dropped."""
    from app.utils.file_import import split_html_into_pages
    html_content = ("<p>The cell is the <strong>basic unit</strong> of <em>life</em>. Truly.</p>"
                    "<ul><li>Nucleus<ul><li>Envelope</li></ul></li></ul>"
                    '<p><img data-media="1" alt="x" /></p><table><tr><td><p>cell</p></td></tr></table>')
    for max_chars in (5, 40, 3000):
        assert "".join(split_html_into_pages(html_content, max_chars)) == html_content
    assert len(split_html_into_pages(html_content, 5)) == 3  # the picture has no text, so it rides with the table


def _docx_with_runs() -> bytes:
    body = ('<w:p><w:r><w:t xml:space="preserve">The cell is the </w:t></w:r>'
            '<w:r><w:rPr><w:b/></w:rPr><w:t>basic unit</w:t></w:r>'
            '<w:r><w:t xml:space="preserve"> of </w:t></w:r>'
            '<w:r><w:rPr><w:i/></w:rPr><w:t>life</w:t></w:r>'
            '<w:r><w:t>, everywhere.</w:t></w:r></w:p>')
    return _replace_document_body(docx_bytes(("x",)), body)


def _replace_document_body(archive_bytes: bytes, body: str) -> bytes:
    source = zipfile.ZipFile(io.BytesIO(archive_bytes))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in source.namelist():
            data = source.read(name)
            if name == "word/document.xml":
                data = data.decode().replace("<w:p><w:r><w:t>x</w:t></w:r></w:p>", body).encode()
            archive.writestr(name, data)
    return buffer.getvalue()


def test_a_docx_paragraph_keeps_the_plain_text_between_its_formatted_runs(world):
    """IMPORT-002, end to end: the words outside bold and italic arrive in the saved document."""
    response = _upload(world, "cell.docx", _docx_with_runs())
    assert response.status_code == 200, response.text
    stored = world.run(world.raw["books"].find_one({}))
    text = _text(json.loads(stored["full_content"])["root"])
    for piece in ("The cell is the", "basic unit", "of", "life", ", everywhere."):
        assert piece in text, text


@pytest.mark.parametrize("form", [{"preview": "true"}, {}])
def test_an_import_past_the_word_ceiling_is_refused_with_the_code(world, form):
    """IMPORT-003 / ADR-041: accepted, the document could never be saved again."""
    storage = FakeStorage()
    with patch("app.routers.books.SUBSCRIPTION_PLANS", {"free": {"limits": {"document_words": 50}}}), \
            patch("app.routers.books.SubscriptionTier") as tier, \
            patch("app.routers.books.get_storage_backend", return_value=storage):
        tier.FREE = "free"
        response = _upload(world, "long.txt", txt_bytes(), **form)
    assert response.status_code == 413, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "document_too_long" and detail["limit"] == 50 and detail["words"] > 50
    assert world.run(world.raw["books"].count_documents({})) == 0 and storage.uploads == []


def test_an_import_under_the_ceiling_is_saved_with_its_word_count(world):
    response = _upload(world, "mito.txt", txt_bytes())
    assert response.status_code == 200, response.text
    assert world.run(world.raw["books"].find_one({}))["word_count"] > 100


def test_import_pictures_are_stored_several_at_a_time_and_each_keeps_its_place(world):
    """IMPORT-004: one after another, two dozen pictures outlasted the client's timeout."""
    import asyncio
    from app.routers import books as books_router

    class SlowStorage(FakeStorage):
        running = peak = 0

        async def upload(self, file_content, filename, folder="", **options):
            SlowStorage.running += 1
            SlowStorage.peak = max(SlowStorage.peak, SlowStorage.running)
            await asyncio.sleep(0.01)
            SlowStorage.running -= 1
            index = int(filename.split("-")[1].split(".")[0])
            return {"secure_url": f"https://cdn.test/{folder}/{index}.png", "public_id": f"{folder}/{index}",
                    "width": 10, "height": 10, "format": "png", "bytes": len(file_content)}

    pictures = [{"index": n, "bytes": png_bytes(), "ext": "png", "width": 1, "height": 1} for n in range(1, 13)]
    with patch("app.routers.books.get_storage_backend", return_value=SlowStorage()):
        placed = world.run(books_router._store_import_pictures(pictures, world.current["user"]["user_id"], "b" * 24))
    assert sorted(placed) == list(range(1, 13))
    assert all(placed[n]["url"].endswith(f"/{n}.png") for n in placed)
    assert 1 < SlowStorage.peak <= books_router.IMPORT_PICTURE_CONCURRENCY
    assert world.run(world.raw["media"].count_documents({})) == 12


# ── IMPORT-006/007: any common encoding is read, and a refusal says why ─────

SPANISH = "Tema 3: La célula\n\nAño, niño, pingüino, ¿qué? ¡ánimo! Señor Núñez."
JAPANESE = "日本語のノート\n\n細胞は生命の基本単位です。漢字、ひらがな、カタカナ。"


@pytest.mark.parametrize("label, data, expected", [
    ("utf-8", SPANISH.encode("utf-8"), SPANISH),
    ("utf-8 with a mark", b"\xef\xbb\xbf" + SPANISH.encode("utf-8"), SPANISH),
    ("windows-1252", SPANISH.encode("cp1252"), SPANISH),
    ("windows-1252 curly quotes", "He said “hello” — it’s fine. Café, naïve.".encode("cp1252"),
     "He said “hello” — it’s fine. Café, naïve."),
    ("utf-16 with a mark", SPANISH.encode("utf-16"), SPANISH),
    ("utf-16 without a mark", SPANISH.encode("utf-16-le"), SPANISH),
    ("shift-jis", JAPANESE.encode("shift_jis"), JAPANESE),
])
def test_a_text_file_is_read_in_the_encoding_it_was_saved_in(label, data, expected):
    from app.utils.file_import import decode_text
    assert decode_text(data) == expected, label


@pytest.mark.parametrize("data", [b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x01\x40", bytes(range(256)) * 8])
def test_bytes_that_are_not_text_are_not_decoded_as_text(data):
    from app.utils.file_import import decode_text
    assert decode_text(data) is None


def test_a_windows_text_file_is_imported_with_its_accents(world):
    response = _upload(world, "apuntes.txt", SPANISH.encode("cp1252"))
    assert response.status_code == 200, response.text
    stored = world.run(world.raw["books"].find_one({}))
    assert "pingüino" in stored["full_content"] or "ping\\u00fcino" in stored["full_content"]
    assert "﻿" not in json.loads(stored["full_content"])["root"]["children"][0]["children"][0]["text"]


def _encrypted_pdf() -> bytes:
    import fitz
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), PARAGRAPH, fontsize=11)
    return doc.tobytes(encryption=fitz.PDF_ENCRYPT_AES_256, user_pw="secret", owner_pw="owner")


def _blank_pdf() -> bytes:
    import fitz
    doc = fitz.open()
    doc.new_page()
    return doc.tobytes()


@pytest.mark.parametrize("name, content, code", [
    ("notes.md", b"# Title\n\ntext", "import_unsupported_type"),
    ("book.epub", b"PK\x03\x04", "import_unsupported_type"),
    ("no-extension", b"some text", "import_unsupported_type"),
    ("old.doc", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64, "import_legacy_doc"),
    ("empty.txt", b"", "import_empty"),
    ("spaces.txt", b"  \n\n \n", "import_empty"),
    ("blank.pdf", _blank_pdf(), "import_empty"),
    ("locked.pdf", _encrypted_pdf(), "import_password_protected"),
    ("broken.pdf", b"%PDF-1.4 nope", "import_unreadable"),
    ("broken.docx", b"PK\x03\x04 not a docx", "import_unreadable"),
    ("photo.txt", b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR", "import_unreadable"),
])
def test_a_refused_import_says_which_reason_it_was(world, name, content, code):
    """IMPORT-007: every cause used to be one English sentence."""
    for form in ({"preview": "true"}, {}):
        response = _upload(world, name, content, **form)
        assert response.status_code == 400, response.text
        assert response.json()["detail"] == {"code": code}
    assert world.run(world.raw["books"].count_documents({})) == 0


def test_an_oversize_import_is_refused_with_a_code_and_the_limit(world):
    with patch("app.routers.books.BOOK_IMPORT_MAX_BYTES", 2 * 1024 * 1024):
        response = _upload(world, "big.txt", b"x" * (3 * 1024 * 1024))
    assert response.status_code == 413
    assert response.json()["detail"] == {"code": "import_too_large", "limit_mb": 2}


# ── IMPORT-009..012: formatting, structure and lines survive the import ─────

def _first(root, node_type):
    return next(node for node in _walk(root) if node.get("type") == node_type)


def test_formatting_is_lexicals_bitmask_not_a_list_of_names():
    """IMPORT-009: `["bold"]` read as no formatting at all in the editor and on the phone."""
    root = html_to_lexical_json(
        "<p><strong>b</strong> <em>i</em> <u>u</u> <s>s</s> <code>c</code> <sub>x</sub> <sup>y</sup> "
        "<strong><em>bi</em></strong></p>"
    )["root"]
    formats = {node["text"].strip(): node["format"] for node in _walk(root) if node.get("type") == "text" and node["text"].strip()}
    assert formats == {"b": 1, "i": 2, "u": 8, "s": 4, "c": 16, "x": 32, "y": 64, "bi": 3}


def test_the_space_around_a_formatted_run_is_kept_and_only_the_ends_are_trimmed():
    """IMPORT-010: every text node was stripped, so 'is the **unit** of' read 'is theunitof'."""
    root = html_to_lexical_json("<p>  The cell is the <strong>basic unit</strong> of   <em>life</em> .\n</p>")["root"]
    (paragraph,) = root["children"]
    assert "".join(node["text"] for node in paragraph["children"]) == "The cell is the basic unit of life ."
    root = html_to_lexical_json("<p><strong>Bold words</strong> <em>and italics</em></p>")["root"]
    assert "".join(node["text"] for node in root["children"][0]["children"]) == "Bold words and italics"


def test_a_table_becomes_a_table_with_rows_cells_and_headers():
    root = html_to_lexical_json(
        "<table><thead><tr><th><p>Organelle</p></th><th>Role</th></tr></thead>"
        "<tbody><tr><td><p>Nucleus</p></td><td></td></tr></tbody></table><p>After</p>"
    )["root"]
    table, after = root["children"]
    assert table["type"] == "table" and after["type"] == "paragraph"
    assert [[cell["headerState"] for cell in row["children"]] for row in table["children"]] == [[1, 1], [0, 0]]
    assert [[_text(cell) for cell in row["children"]] for row in table["children"]] == [["Organelle", "Role"], ["Nucleus", ""]]
    for row in table["children"]:
        assert row["type"] == "tablerow"
        for cell in row["children"]:
            assert cell["type"] == "tablecell" and cell["colSpan"] == 1 and cell["rowSpan"] == 1
            assert cell["children"] and cell["children"][0]["type"] == "paragraph"  # never an empty cell


def test_a_link_keeps_its_address_and_a_bookmark_is_only_its_text():
    root = html_to_lexical_json(
        '<p>See <a href="https://example.com/a">the page</a>, note<sup><a href="#fn1">[1]</a></sup> '
        '<a href="javascript:alert(1)">x</a><a id="anchor"></a>.</p>'
    )["root"]
    links = [node for node in _walk(root) if node.get("type") == "link"]
    assert [(link["url"], _text(link)) for link in links] == [("https://example.com/a", "the page")]
    assert "[1]" in _text(root) and "javascript" not in json.dumps(root)


def test_a_quote_and_a_code_block_keep_their_kind():
    root = html_to_lexical_json("<blockquote><p>A quotation.</p></blockquote><pre>a = 1\n  b = 2</pre><p>x</p>")["root"]
    quote, code, paragraph = root["children"]
    assert quote["type"] == "quote" and _text(quote) == "A quotation."
    assert code["type"] == "code"
    assert [(n["type"], n.get("text")) for n in code["children"]] == [("text", "a = 1"), ("linebreak", None), ("text", "  b = 2")]
    assert paragraph["type"] == "paragraph"


def _pdf_of(draw) -> bytes:
    import fitz
    doc = fitz.open()
    draw(doc)
    return doc.tobytes()


def _html_of_pdf(draw) -> str:
    return "\n".join(page["content"] for page in process_uploaded_file("notes.pdf", _pdf_of(draw)))


def test_pdf_lines_that_start_with_a_bullet_or_a_number_are_lists():
    """IMPORT-011: they were one paragraph, '· A · B · C'."""
    def draw(doc):
        page = doc.new_page()
        page.insert_text((72, 100), "Organelles of the cell:", fontsize=11)
        for index, item in enumerate(["- Nucleus: holds the DNA", "- Mitochondrion: makes ATP", "- Ribosome"]):
            page.insert_text((90, 130 + 15 * index), item, fontsize=11)
        for index, item in enumerate(["First step", "Second step"]):
            page.insert_text((90, 200 + 15 * index), f"{index + 1}. {item}", fontsize=11)
        page.insert_text((72, 260), "1. A numbered heading", fontsize=19)
    content = _html_of_pdf(draw)
    assert "<ul><li>Nucleus: holds the DNA</li><li>Mitochondrion: makes ATP</li><li>Ribosome</li></ul>" in content
    assert "<ol><li>First step</li><li>Second step</li></ol>" in content
    assert "<h2>1. A numbered heading</h2>" in content
    root = html_to_lexical_json(content)["root"]
    assert [node["listType"] for node in root["children"] if node["type"] == "list"] == ["bullet", "number"]


def test_pdf_lines_of_one_paragraph_join_as_a_reader_would_join_them():
    """IMPORT-011: a word cut at the line end is one word; Japanese lines join without a space."""
    def draw(doc):
        page = doc.new_page()
        page.insert_text((72, 100), "A word cut by a hyphen at the end of the li-\nne joins, and a plain line\nbreak is a space.", fontsize=11)
        page.insert_text((72, 200), "細胞は生命の基本単位です。\n二行目のテキスト。", fontsize=11, fontname="japan")
    content = _html_of_pdf(draw)
    assert "at the end of the line joins, and a plain line break is a space." in content
    assert "細胞は生命の基本単位です。二行目のテキスト。" in content


def test_pdf_running_headers_and_footers_are_left_out_but_body_text_is_not():
    def draw(doc):
        for number in range(1, 5):
            page = doc.new_page()
            page.insert_text((72, 40), "Biology notes — chapter 3", fontsize=8)
            page.insert_text((72, 100), f"Body of page {number}. " + PARAGRAPH, fontsize=11)
            page.insert_text((72, 815), f"Page {number} of 4", fontsize=8)
    pages = process_uploaded_file("notes.pdf", _pdf_of(draw))
    content = "\n".join(page["content"] for page in pages)
    assert "Biology notes" not in content and "of 4" not in content
    assert all(f"Body of page {n}." in content for n in range(1, 5))
    single = process_uploaded_file("one.pdf", _pdf_of(lambda doc: doc.new_page().insert_text((72, 40), "A title in the margin", fontsize=8)))
    assert "A title in the margin" in single[0]["content"]  # one page: nothing repeats, nothing is dropped


def test_a_ruled_pdf_table_is_a_table():
    def draw(doc):
        import fitz
        page = doc.new_page()
        page.insert_text((72, 80), "Before the table.", fontsize=11)
        rows = [("Organelle", "Role", "Size"), ("Nucleus", "DNA", "6 um"), ("Ribosome", "Proteins", "25 nm")]
        for r, row in enumerate(rows):
            for c, cell in enumerate(row):
                page.insert_text((76 + c * 150, 120 + r * 17), cell, fontsize=11)
                page.draw_rect(fitz.Rect(72 + c * 150, 108 + r * 17, 222 + c * 150, 125 + r * 17), width=0.5)
        page.insert_text((72, 200), "After the table.", fontsize=11)
    content = _html_of_pdf(draw)
    root = html_to_lexical_json(content)["root"]
    table = _first(root, "table")
    assert [[_text(cell) for cell in row["children"]] for row in table["children"]] == [
        ["Organelle", "Role", "Size"], ["Nucleus", "DNA", "6 um"], ["Ribosome", "Proteins", "25 nm"]]
    kinds = [node["type"] for node in root["children"]]
    assert kinds == ["paragraph", "table", "paragraph"] and content.count("Nucleus") == 1


def _txt_html(text: str) -> str:
    return "".join(page["content"] for page in process_uploaded_file("notes.txt", text.encode()))


def test_a_text_file_with_windows_line_endings_keeps_its_paragraphs():
    """IMPORT-012: `\\r\\n\\r\\n` was not a paragraph break, so the whole file was one paragraph."""
    assert _txt_html("First paragraph.\r\n\r\nSecond paragraph.\r\n") == "<p>First paragraph.</p><p>Second paragraph.</p>"
    assert _txt_html("Old Mac.\r\rSecond.") == "<p>Old Mac.</p><p>Second.</p>"


def test_text_lines_are_lines_and_marked_lines_are_lists_and_headings():
    content = _txt_html(
        "# Topic 3\n\nOrganelles:\n- Nucleus\n- Ribosome\n  makes proteins\n\n1. First\n2) Second\n\n"
        "Line one\nline two\n\n#hashtag is not a heading\n\n<b>literal</b> & more"
    )
    assert content == (
        "<h1>Topic 3</h1><p>Organelles:</p><ul><li>Nucleus</li><li>Ribosome makes proteins</li></ul>"
        "<ol><li>First</li><li>Second</li></ol><p>Line one<br>line two</p><p>#hashtag is not a heading</p>"
        "<p>&lt;b&gt;literal&lt;/b&gt; &amp; more</p>"
    )
    root = html_to_lexical_json(content)["root"]
    assert [node["type"] for node in root["children"]] == ["heading", "paragraph", "list", "list", "paragraph", "paragraph", "paragraph"]
    line_pair = root["children"][4]["children"]
    assert [node["type"] for node in line_pair] == ["text", "linebreak", "text"] and line_pair[2]["text"] == "line two"
    assert _text(root["children"][6]) == "<b>literal</b> & more"


def test_hard_wrapped_prose_is_joined_back_into_a_paragraph():
    wrapped = ("The mitochondrion is the organelle that produces most of the cell's supply of\n"
               "adenosine triphosphate, which is then used as a source of chemical energy by nearly\n"
               "every process in the cell, including growth and move-\nment of the cell itself.")
    assert _txt_html(wrapped) == ("<p>The mitochondrion is the organelle that produces most of the cell&#x27;s supply of "
                                  "adenosine triphosphate, which is then used as a source of chemical energy by nearly "
                                  "every process in the cell, including growth and movement of the cell itself.</p>")


# ── IMPORT-013: the preview says facts ──────────────────────────────────────

@pytest.mark.parametrize("name, content", [("mito.txt", txt_bytes()), ("mito.docx", docx_bytes()), ("mito.pdf", pdf_bytes(2))])
def test_a_preview_counts_the_words_of_every_kind_of_file_and_warns_of_nothing(world, name, content):
    """Only the PDF reader counted, so a text or Word preview said 0 words and 'low word count'."""
    body = _upload(world, name, content, preview="true").json()
    assert body["facts"]["words"] > 10 and body["facts"]["pages"] == body["total_pages"]
    assert body["facts"]["pictures"] == 0 and body["facts"]["scanned"] is False
    assert body["quality_summary"]["total_words"] == body["facts"]["words"]
    assert all(page["word_count"] > 0 for page in body["sample_pages"])
    assert body["warnings"] == [] and body["info"] is None


def test_a_preview_of_a_scan_says_it_is_one(world):
    import fitz
    doc = fitz.open()
    pixmap = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 40, 30), False)
    pixmap.set_rect(pixmap.irect, (200, 40, 40))
    for _ in range(2):
        page = doc.new_page()
        page.insert_image(page.rect, pixmap=pixmap)
    facts = _upload(world, "scan.pdf", doc.tobytes(), preview="true").json()["facts"]
    assert facts == {"pages": 2, "words": 0, "pictures": 2, "scanned": True, "damaged": False}


def test_a_preview_carries_the_first_pages_only_and_still_says_how_many_there_are(world):
    with patch("app.routers.books.PREVIEW_PAGE_LIMIT", 3):
        body = _upload(world, "long.pdf", pdf_bytes(7), preview="true").json()
    assert body["total_pages"] == 7 and body["facts"]["pages"] == 7
    assert [page["page_number"] for page in body["sample_pages"]] == [1, 2, 3]


# ── IMPORT-018: a page is read down its columns, however many it has ────────

def _columns_pdf(columns: int, title: bool = True) -> bytes:
    import fitz
    doc = fitz.open()
    page = doc.new_page()
    if title:
        page.insert_text((50, 60), "A title across the whole page width of this article", fontsize=20)
    width = 480 / columns
    for c in range(columns):
        left = 50 + c * (width + 12)
        page.insert_textbox(fitz.Rect(left, 90, left + width, 420), (f"Column{c + 1} text here. " * (72 // columns)), fontsize=9)
        page.insert_textbox(fitz.Rect(left, 440, left + width, 760), (f"Column{c + 1} lower part. " * (60 // columns)), fontsize=9)
    return doc.tobytes()


@pytest.mark.parametrize("columns", [2, 3, 4])
def test_columns_are_read_left_to_right_each_from_top_to_bottom(columns):
    """The fault: three columns came out 2, 1, 3 — the page was split at its midpoint."""
    import re
    (page,) = process_uploaded_file("article.pdf", _columns_pdf(columns))
    order = []
    for name in re.findall(r"Column(\d) (?:text|lower)", page["content"]):
        if not order or order[-1] != name:
            order.append(name)
    assert order == [str(n) for n in range(1, columns + 1)]
    assert page["has_columns"] is True
    assert page["content"].index("A title across") < page["content"].index("Column1")
    upper, lower = page["content"].index("Column1 text"), page["content"].index("Column1 lower")
    assert upper < lower < page["content"].index("Column2 text")
    assert "<div" not in page["content"]


def test_one_column_with_a_page_number_beside_it_is_one_column():
    import fitz
    doc = fitz.open()
    page = doc.new_page()
    page.insert_textbox(fitz.Rect(72, 80, 330, 700), "Body text of a narrow single column. " * 60, fontsize=10)
    page.insert_text((500, 400), "12", fontsize=10)
    (result,) = process_uploaded_file("narrow.pdf", doc.tobytes())
    assert result["has_columns"] is False and "Body text" in result["content"]


# ── IMPORT-020 / ADR-066: a file already imported is recognised, and the reader is asked ──

def test_the_preview_says_when_the_same_file_is_already_in_the_library(world):
    first = _upload(world, "mito.txt", txt_bytes()).json()
    body = _upload(world, "renamed.txt", txt_bytes(), preview="true").json()
    assert body["duplicate_of"]["_id"] == first["_id"] and body["duplicate_of"]["title"] == "mito"
    assert _upload(world, "other.txt", b"something else entirely", preview="true").json()["duplicate_of"] is None


def test_a_save_that_asks_is_refused_with_the_document_it_would_copy_and_copy_imports_it(world):
    first = _upload(world, "mito.txt", txt_bytes(), on_duplicate="ask")
    assert first.status_code == 200, first.text
    again = _upload(world, "mito.txt", txt_bytes(), on_duplicate="ask")
    assert again.status_code == 409
    detail = again.json()["detail"]
    assert detail["code"] == "import_duplicate" and detail["existing"]["_id"] == first.json()["_id"]
    assert world.run(world.raw["books"].count_documents({})) == 1
    assert _upload(world, "mito.txt", txt_bytes(), on_duplicate="copy").status_code == 200
    assert world.run(world.raw["books"].count_documents({})) == 2


def test_a_client_that_says_nothing_keeps_importing_as_it_always_did(world):
    assert _upload(world, "mito.txt", txt_bytes()).status_code == 200
    assert _upload(world, "mito.txt", txt_bytes()).status_code == 200


def test_the_import_records_what_it_was_made_from_and_an_edit_cannot_change_it(world):
    import hashlib
    book_id = _upload(world, "mito.txt", txt_bytes()).json()["_id"]
    stored = world.run(world.raw["books"].find_one({}))
    assert stored["source_hash"] == hashlib.sha256(txt_bytes()).hexdigest() and stored["source_filename"] == "mito.txt"
    response = world.http.put(f"/book/edit/{book_id}", json={"title": "renamed", "source_hash": "forged"})
    assert response.status_code == 200, response.text
    assert world.run(world.raw["books"].find_one({}))["source_hash"] == stored["source_hash"]


def test_a_deleted_import_and_another_readers_import_are_not_duplicates(world):
    book_id = _upload(world, "mito.txt", txt_bytes()).json()["_id"]
    assert world.http.delete(f"/book/delete/{book_id}").status_code == 204
    assert _upload(world, "mito.txt", txt_bytes(), on_duplicate="ask").status_code == 200
    world.act_as(world.new_user("someone else"))
    assert _upload(world, "mito.txt", txt_bytes(), on_duplicate="ask").status_code == 200


def test_an_unknown_answer_to_the_duplicate_question_is_refused(world):
    assert _upload(world, "mito.txt", txt_bytes(), on_duplicate="replace").status_code == 422


# ── IMPORT-021/022: a damaged PDF says so; East Asian text is read in the encoding it has ──

CHINESE = "细胞是生命的基本单位。线粒体产生能量,细胞核含有遗传物质。\n\n第二段:核糖体合成蛋白质。"
KOREAN = "세포는 생명의 기본 단위입니다. 미토콘드리아는 에너지를 만듭니다.\n\n핵에는 유전 물질이 있습니다."
JAPANESE_LONG = "第3章 細胞の構造\n\n核にはDNAが含まれています。ミトコンドリアはATPを産生します。\n\n・核:DNAを含む\n1. 最初の手順\n"


@pytest.mark.parametrize("label, data, expected", [
    ("utf-16-le, Japanese, no mark", JAPANESE_LONG.encode("utf-16-le"), JAPANESE_LONG),
    ("utf-16-be, Japanese, no mark", JAPANESE_LONG.encode("utf-16-be"), JAPANESE_LONG),
    ("utf-16-le, Chinese, no mark", CHINESE.encode("utf-16-le"), CHINESE),
    ("utf-16-be, Spanish, no mark", SPANISH.encode("utf-16-be"), SPANISH),
    ("euc-jp", JAPANESE_LONG.encode("euc_jp"), JAPANESE_LONG),
    ("euc-jp, short", JAPANESE.encode("euc_jp"), JAPANESE),
    ("iso-2022-jp", JAPANESE_LONG.encode("iso2022_jp"), JAPANESE_LONG),
    ("shift-jis, long", JAPANESE_LONG.encode("shift_jis"), JAPANESE_LONG),
    ("gbk", CHINESE.encode("gbk"), CHINESE),
    ("euc-kr", KOREAN.encode("euc_kr"), KOREAN),
])
def test_east_asian_text_is_read_in_the_encoding_it_was_saved_in(label, data, expected):
    """IMPORT-022. EUC-JP became mojibake and ISO-2022-JP its own escape codes; UTF-16 without a mark was refused."""
    from app.utils.file_import import decode_text
    assert decode_text(data) == expected, label


@pytest.mark.parametrize("label, data, expected", [
    ("windows-1252 Spanish", SPANISH.encode("cp1252"), SPANISH),
    ("windows-1252 short", "Señor Núñez dijo que sí, mañana.".encode("cp1252"), "Señor Núñez dijo que sí, mañana."),
    ("windows-1252 French", "L’unité de base. Très bien, où ça ? Cœur, façade, être.".encode("cp1252"),
     "L’unité de base. Très bien, où ça ? Cœur, façade, être."),
    ("utf-8 with NUL bytes", b"Texto antes\x00\x00 despues.\n\nSegundo.", "Texto antes\x00\x00 despues.\n\nSegundo."),
])
def test_western_text_is_not_mistaken_for_an_east_asian_or_utf16_file(label, data, expected):
    from app.utils.file_import import decode_text
    assert decode_text(data) == expected, label


def _truncated_pdf(keep: float) -> bytes:
    import fitz
    doc = fitz.open()
    for n in range(6):
        doc.new_page().insert_text((72, 100), f"Page {n + 1}. {PARAGRAPH}", fontsize=11)
    data = doc.tobytes()
    return data[:int(len(data) * keep)]


def test_a_damaged_pdf_that_yields_nothing_is_called_damaged_not_empty(world):
    """IMPORT-021: 'has no text to import' sent the writer looking for the wrong problem."""
    from app.utils.file_import import refusal_code
    data = _truncated_pdf(0.5)
    if process_uploaded_file("cut.pdf", data):
        pytest.skip("this cut still yields pages; the next test covers that case")
    assert refusal_code("cut.pdf", data) == "import_unreadable"
    assert _upload(world, "cut.pdf", data, preview="true").json()["detail"] == {"code": "import_unreadable"}


def test_a_damaged_pdf_that_still_yields_pages_is_previewed_as_damaged(world):
    import fitz
    doc = fitz.open()
    doc.new_page().insert_text((72, 100), PARAGRAPH, fontsize=11)
    whole = doc.tobytes()
    assert _upload(world, "whole.pdf", whole, preview="true").json()["facts"]["damaged"] is False
    cut = whole[:-40]  # the cross-reference table's end is gone; the reader rebuilds it
    body = _upload(world, "cut.pdf", cut, preview="true").json()
    assert body["facts"]["damaged"] is True and body["facts"]["words"] > 10


# ── IMPORT-024: Arabic and Hebrew letters from a PDF are letters ────────────

def test_presentation_forms_from_a_pdf_become_the_letters_they_stand_for():
    from app.utils.file_import import _fold_presentation_forms, _pdf_line
    shaped = "ﺍﻟﺨﻠﻴﺔ ﻫﻲ ﺍﻟﻮﺣﺪﺓ"  # الخلية هي الوحدة, as glyph forms
    assert _fold_presentation_forms(shaped) == "الخلية هي الوحدة"
    assert _fold_presentation_forms("x² + H₂O ㎝ ｶﾀｶﾅ") == "x² + H₂O ㎝ ｶﾀｶﾅ"  # nothing else is normalised
    line = _pdf_line({"spans": [{"text": shaped, "size": 11, "flags": 0}], "bbox": [0, 0, 1, 1]})
    assert line["text"] == "الخلية هي الوحدة"
    assert _fold_presentation_forms("שׁוֹ") == "שׁוֹ"  # Hebrew shin with dot, vav with holam


# ── IMPORT-025: what a text file's writer aligned or marked is kept ─────────

NBSP = " "


def test_east_asian_bullets_are_list_marks_with_or_without_a_space():
    content = _txt_html("主な細胞小器官:\n・核:DNAを含む\n・ミトコンドリア\n● リボソーム")
    assert content == "<p>主な細胞小器官:</p><ul><li>核:DNAを含む</li><li>ミトコンドリア</li><li>リボソーム</li></ul>"
    assert _txt_html("*bold* and -5 degrees") == "<p>*bold* and -5 degrees</p>"  # a mark needs its space


def test_indentation_and_tabs_survive_as_no_break_spaces_through_to_the_document():
    content = _txt_html("def f(x):\n    return x + 1\n\tif x:")
    assert content == f"<p>def f(x):<br>{NBSP * 4}return x + 1<br>{NBSP * 4}if x:</p>"
    (paragraph,) = html_to_lexical_json(content)["root"]["children"]
    texts = [node["text"] for node in paragraph["children"] if node["type"] == "text"]
    assert texts == ["def f(x):", f"{NBSP * 4}return x + 1", f"{NBSP * 4}if x:"]


def test_tab_separated_lines_are_a_table_and_ragged_ones_are_not():
    table = _txt_html("Organelle\tRole\tSize\nNucleus\tDNA\t6 um")
    root = html_to_lexical_json(table)["root"]
    assert [[_text(c) for c in row["children"]] for row in _first(root, "table")["children"]] == [
        ["Organelle", "Role", "Size"], ["Nucleus", "DNA", "6 um"]]
    assert "<table>" not in _txt_html("a\tb\tc\nonly two\tcells")
    assert "<table>" not in _txt_html("one\tline")


def test_control_characters_are_not_text_and_a_form_feed_breaks_the_paragraph():
    assert _txt_html("Before\x00\x00 after.\x0cNext page") == "<p>Before after.</p><p>Next page</p>"


def test_a_pdf_line_starting_with_a_japanese_bullet_is_a_list_row():
    def draw(doc):
        page = doc.new_page()
        for index, item in enumerate(["・核:DNAを含む", "・ミトコンドリア"]):
            page.insert_text((90, 130 + 18 * index), item, fontsize=11, fontname="japan")
    assert "<ul><li>核:DNAを含む</li><li>ミトコンドリア</li></ul>" in _html_of_pdf(draw)
