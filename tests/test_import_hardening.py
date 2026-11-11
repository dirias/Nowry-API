"""Round four's edge cases (IMPORT-039 … IMPORT-044, ADR-070, ADR-071): a hostile file costs a child process,
a failed save leaves nothing behind, and every refusal is a code."""
import asyncio
import io
import zipfile
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from bson import ObjectId

pytest.importorskip("mongomock_motor")

from tests.test_book_file_import import (  # noqa: E402,F401  (world is a fixture)
    FakeStorage, _upload, docx_with_picture_bytes, pdf_with_picture_bytes, png_bytes, txt_bytes, world,
)
from app.services import import_parser  # noqa: E402
from app.utils import import_worker, media_ledger  # noqa: E402


def _router():
    """The books router, imported when a test runs: imported while tests are being collected, it is
    built before the suite's stubs are in place and the app then serves it with the real database."""
    from app.routers import books
    return books


def _docx(document_xml: bytes, extra: dict | None = None) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", document_xml)
        for name, data in (extra or {}).items():
            archive.writestr(name, data)
    return out.getvalue()


# ── IMPORT-039: what a Word archive expands to is bounded ───────────────────

def test_an_archive_that_expands_past_the_bound_is_recognised_without_reading_it():
    bomb = _docx(b" " * (import_worker.ARCHIVE_PART_MAX_BYTES + 1))
    assert len(bomb) < 200_000 and import_worker.archive_too_large(bomb)
    assert not import_worker.archive_too_large(docx_with_picture_bytes())
    assert not import_worker.archive_too_large(b"not a zip at all")


def test_too_many_entries_or_too_much_in_total_is_too_large():
    with patch.object(import_worker, "ARCHIVE_MAX_ENTRIES", 3):
        assert import_worker.archive_too_large(_docx(b"<x/>", {f"word/media/{n}.png": b"p" for n in range(5)}))
    with patch.object(import_worker, "ARCHIVE_TOTAL_MAX_BYTES", 1000):
        assert import_worker.archive_too_large(_docx(b"<x/>", {"word/media/a.png": b"p" * 2000}))


def test_a_zip_bomb_is_refused_with_its_own_code_and_nothing_is_saved(world):
    response = _upload(world, "bomb.docx", _docx(b" " * (import_worker.ARCHIVE_PART_MAX_BYTES + 1)), preview="true")
    assert response.status_code == 422 and response.json()["detail"] == {"code": "import_too_complex"}
    assert world.run(world.raw["books"].count_documents({})) == 0


# ── IMPORT-040: the file is read in a child with a time limit ───────────────

def test_the_child_reads_a_file_and_measures_it():
    with patch.object(import_parser, "ISOLATION", "process"):
        parsed = asyncio.run(import_parser.parse("mito.txt", txt_bytes()))
    assert len(parsed.pages) >= 1 and parsed.words > 100 and parsed.content_bytes > 1000


def test_the_child_hands_back_pictures_as_bytes():
    with patch.object(import_parser, "ISOLATION", "process"):
        parsed = asyncio.run(import_parser.parse("mito.pdf", pdf_with_picture_bytes()))
    assert len(parsed.pictures) == 1 and parsed.pictures[0]["bytes"][:4] == b"\x89PNG"


def test_a_file_that_is_not_read_in_time_is_stopped_and_refused():
    slow = _docx(b'<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                 b"<w:body>" + b"<w:p><w:r><w:t>a</w:t></w:r></w:p>" * 120_000 + b"</w:body></w:document>")
    with patch.object(import_parser, "ISOLATION", "process"), patch.object(import_parser, "TIMEOUT_SECONDS", 1.0):
        with pytest.raises(import_parser.ImportTooComplex):
            asyncio.run(import_parser.parse("slow.docx", slow))


def test_the_import_route_reads_through_the_parser_never_on_the_event_loop():
    import inspect
    source = inspect.getsource(_router())
    assert "await import_parser.parse(filename, file_content)" in source
    assert "process_uploaded_file_with_media(" not in source   # never on the event loop


# ── IMPORT-042: a document the database could not hold is refused, not met as a 500 ──

def test_an_import_past_the_byte_ceiling_is_refused_in_the_preview_and_in_the_save(world):
    storage = FakeStorage()
    with patch.object(_router(), "DOCUMENT_MAX_BYTES", 2000), \
            patch("app.routers.books.get_storage_backend", return_value=storage):
        for form in ({"preview": "true"}, {}):
            response = _upload(world, "long.txt", txt_bytes(), **form)
            assert response.status_code == 413, response.text
            detail = response.json()["detail"]
            assert detail["code"] == "document_too_large" and detail["limit"] == 0 and detail["megabytes"] >= 0
    assert world.run(world.raw["books"].count_documents({})) == 0 and storage.uploads == []


def test_an_edit_past_the_byte_ceiling_is_refused(world):
    created = world.http.post("/book/create", json={"title": "Notes"})
    content = '{"root":{"type":"root","children":[{"type":"paragraph","children":[{"type":"text","text":"' + "x" * 5000 + '"}]}]}}'
    with patch.object(_router(), "DOCUMENT_MAX_BYTES", 2000):
        response = world.http.put(f"/book/edit/{created.json()['_id']}", json={"full_content": content})
    assert response.status_code == 413 and response.json()["detail"]["code"] == "document_too_large"


# ── IMPORT-041: a save that fails leaves no picture behind and answers with a code ──

def test_a_save_that_fails_after_its_pictures_marks_them_for_removal_and_says_so(world):
    storage = FakeStorage()
    with patch("app.routers.books.get_storage_backend", return_value=storage), \
            patch.object(_router().book_search, "search_fields", side_effect=RuntimeError("the database said no")):
        response = _upload(world, "mito.pdf", pdf_with_picture_bytes())
    assert response.status_code == 500 and response.json()["detail"] == {"code": "import_failed"}
    assert world.run(world.raw["books"].count_documents({})) == 0
    rows = world.run(world.raw["media"].find({}).to_list(length=10))
    assert len(rows) == 1 and rows[0].get("delete_after") is not None


def test_a_ceiling_met_only_at_the_save_also_marks_the_pictures(world):
    storage = FakeStorage()
    calls = {"n": 0}

    def second_call_refuses(size: int) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise _router().HTTPException(status_code=413, detail={"code": "document_too_large", "limit": 12})

    with patch("app.routers.books.get_storage_backend", return_value=storage), \
            patch.object(_router(), "ensure_document_within_bytes", side_effect=second_call_refuses):
        response = _upload(world, "mito.pdf", pdf_with_picture_bytes())
    assert response.status_code == 413
    rows = world.run(world.raw["media"].find({}).to_list(length=10))
    assert len(rows) == 1 and rows[0].get("delete_after") is not None


def test_pictures_of_a_book_that_was_never_saved_are_orphans_after_the_grace_period(world):
    uid = world.current["user"]["user_id"]
    old, fresh = str(ObjectId()), str(ObjectId())
    live = str(world.run(world.raw["books"].insert_one({"user_id": uid, "deleted_at": None, "title": "kept"})).inserted_id)
    now = datetime.now(timezone.utc)
    world.run(world.raw["media"].insert_many([
        {"user_id": uid, "book_id": old, "public_id": "r/old", "url": "u", "created_at": now - timedelta(hours=3)},
        {"user_id": uid, "book_id": fresh, "public_id": "r/fresh", "url": "u", "created_at": now},   # an import under way
        {"user_id": uid, "book_id": live, "public_id": "r/live", "url": "u", "created_at": now - timedelta(hours=3)},
    ]))
    media_ledger._orphan_cursor = None
    assert world.run(media_ledger.condemn_orphans(world.raw["books"])) == 1
    marked = world.run(world.raw["media"].find({"delete_after": {"$exists": True}}).to_list(length=10))
    assert [row["public_id"] for row in marked] == ["r/old"]


# ── IMPORT-043: two saves of one file at once make one document ─────────────

def test_simultaneous_saves_that_ask_about_duplicates_make_exactly_one_document(world):
    from httpx import ASGITransport, AsyncClient

    async def race() -> list:
        async with AsyncClient(transport=ASGITransport(app=world.app), base_url="http://test") as client:
            async def one():
                return (await client.post("/book/import", files={"file": ("race.txt", txt_bytes(), "text/plain")},
                                          data={"on_duplicate": "ask"})).status_code
            return sorted(await asyncio.gather(*(one() for _ in range(6))))

    codes = world.run(race())
    assert codes == [200, 409, 409, 409, 409, 409]
    assert world.run(world.raw["books"].count_documents({})) == 1
    assert world.run(world.raw["import_locks"].count_documents({})) == 0   # released


def test_a_lock_that_is_never_released_refuses_instead_of_hanging(world):
    uid = world.current["user"]["user_id"]
    import hashlib
    key = f"{uid}:{hashlib.sha256(txt_bytes()).hexdigest()}"
    world.run(world.raw["import_locks"].insert_one({"_id": key, "created_at": datetime.now(timezone.utc)}))
    with patch.object(_router(), "IMPORT_LOCK_WAIT_SECONDS", 0.3):
        response = _upload(world, "held.txt", txt_bytes(), on_duplicate="ask")
    assert response.status_code == 409 and response.json()["detail"] == {"code": "import_in_progress"}
    assert _upload(world, "copy.txt", txt_bytes(), on_duplicate="copy").status_code == 200   # a copy was asked for: no lock


# ── IMPORT-044: a picture the storage would not keep is counted, and a scan made only of them is refused ──

class _SomeFail(FakeStorage):
    async def upload(self, file_content, filename, folder="", **options):
        if filename.startswith("import-1."):
            raise RuntimeError("file too large")
        return await super().upload(file_content, filename, folder, **options)


def test_the_answer_says_how_many_pictures_were_dropped(world):
    with patch("app.routers.books.get_storage_backend", return_value=_SomeFail()):
        response = _upload(world, "mito.pdf", pdf_with_picture_bytes())
    assert response.status_code == 200 and response.json()["pictures_dropped"] == 1
    with patch("app.routers.books.get_storage_backend", return_value=FakeStorage()):
        again = _upload(world, "mito2.docx", docx_with_picture_bytes())
    assert again.json()["pictures_dropped"] == 0


def test_a_scan_whose_pictures_could_not_be_stored_is_refused_not_saved_empty(world):
    import fitz
    doc = fitz.open()
    doc.new_page().insert_image(fitz.Rect(50, 50, 300, 300), stream=png_bytes())
    with patch("app.routers.books.get_storage_backend", return_value=FakeStorage(fail=True)):
        response = _upload(world, "scan.pdf", doc.tobytes())
    assert response.status_code == 422 and response.json()["detail"] == {"code": "import_pictures_not_stored"}
    assert world.run(world.raw["books"].count_documents({})) == 0
