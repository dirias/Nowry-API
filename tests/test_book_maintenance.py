"""ADR-069: derived fields stay true — folded search, versioned stats, an older import's facts — by a bounded pass."""
import json
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from bson import ObjectId

from app.services import book_library, book_maintenance
from app.services.book_maintenance import SEARCH_VERSION, STATS_VERSION, fold, query_terms, search_terms
from tests._fake_world import fake_world

T0 = datetime(2026, 11, 1, 12, 0, 0)
JAPANESE = "細胞は生命の基本単位です。ミトコンドリアはATPを産生します。核にはDNAが含まれています。" * 4


@pytest.fixture
def world():
    with fake_world() as w, patch.object(book_library, "apply_title_collation", lambda cursor: cursor):
        w.act_as(w.new_user("reader"))
        yield w


def _state(*blocks):
    children = [{"type": kind, **({"tag": tag} if tag else {}), "children": [{"type": "text", "text": text}]}
                for kind, text, tag in blocks]
    return json.dumps({"root": {"type": "root", "children": children}})


def _insert(world, **fields):
    doc = {"user_id": world.current["user"]["user_id"], "deleted_at": None, "updated_at": T0, "created_at": T0, **fields}
    return world.run(world.raw["books"].insert_one(doc)).inserted_id


def _titles(world, **params):
    response = world.http.get("/book/library", params=params)
    assert response.status_code == 200, response.text
    return sorted(item["title"] for item in response.json()["items"])


# ── folding ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("a, b", [
    ("Álgebra", "ALGEBRA"), ("École", "ecole"), ("über", "UBER"), ("Straße", "STRASSE"), ("cœur", "coeur"),
    ("ノート", "のーと"), ("ﾉｰﾄ", "ノート"), ("ＡＢＣ１２３", "abc123"), ("niño", "NINO"),
])
def test_what_folds_alike(a, b):
    assert fold(a) == fold(b)


def test_what_stays_different():
    assert fold("が") != fold("か")          # voicing is a different sound
    assert fold("한국어") == "한국어" and fold("中文") == "中文"


def test_terms_are_the_suffixes_of_the_words_and_are_bounded():
    assert search_terms("Álgebra II") == ["algebra", "lgebra", "gebra", "ebra", "bra", "ra", "a", "ii", "i"]
    assert "newport" in search_terms("Deep Work", "Cal Newport", "imported")
    assert "newport" not in search_terms("Deep Work", "Cal Newport", "written")  # an author finds only an import
    assert len(search_terms("x" * 5000 + " " + " ".join(f"word{n}" for n in range(500)))) <= book_maintenance.MAX_TERMS
    assert query_terms("  Deep-Work (v2) ") == ["deep", "work", "v2"] and query_terms(".*") == []


# ── the library's search ────────────────────────────────────────────────────

def _book(world, title, **extra):
    return _insert(world, title=title, word_count=5, section_count=1, stats_version=STATS_VERSION,
                   **book_maintenance.search_fields(title, extra.get("author"), extra.get("source")), **extra)


def test_search_ignores_accents_case_width_and_kana(world):
    """IMPORT-036: 'algebra' did not find 'Álgebra'."""
    for title in ["Álgebra lineal", "Algebra II", "École normale", "Straße und Brücke", "日本語ノート", "ひらがなのーと", "Biology"]:
        _book(world, title)
    assert _titles(world, q="algebra") == ["Algebra II", "Álgebra lineal"]
    assert _titles(world, q="ÁLGEBRA") == ["Algebra II", "Álgebra lineal"]
    assert _titles(world, q="ecole") == ["École normale"]
    assert _titles(world, q="strasse brucke") == ["Straße und Brücke"]
    assert _titles(world, q="のーと") == ["ひらがなのーと", "日本語ノート"]
    assert _titles(world, q="ﾉｰﾄ") == ["ひらがなのーと", "日本語ノート"]
    assert _titles(world, q="gebra") == ["Algebra II", "Álgebra lineal"]   # inside a word, still
    assert _titles(world, q="lineal algebra") == ["Álgebra lineal"]        # every word, any order
    assert _titles(world, q="zzz") == []


def test_an_import_is_found_by_its_author_and_punctuation_alone_only_literally(world):
    _book(world, "Deep Work", source="imported", author="Cal Newport")
    _book(world, "Notes (v2)", author="Cal Newport")
    assert _titles(world, q="newport") == ["Deep Work"]
    assert _titles(world, q="(v2)") == ["Notes (v2)"]
    assert _titles(world, q=".*") == []


def test_a_document_the_pass_has_not_reached_is_still_found_by_its_title(world):
    _insert(world, title="Old Notes on Cells", word_count=1, section_count=1)
    assert _titles(world, q="notes") == ["Old Notes on Cells"]
    world.run(book_maintenance.run_pass(world.raw["books"], pause=0))
    assert _titles(world, q="NOTES") == ["Old Notes on Cells"] and _titles(world, q="cells") == ["Old Notes on Cells"]


def test_the_query_is_a_prefix_match_on_the_indexed_terms():
    clauses = book_library.library_filter("u1", search="Álgebra II")["$or"]
    assert clauses[0] == {"$and": [{"search_terms": {"$regex": "^algebra"}}, {"search_terms": {"$regex": "^ii"}}]}
    assert clauses[1]["search_version"] == {"$exists": False}


def test_a_rename_is_searchable_and_the_terms_never_leave_the_server(world):
    created = world.http.post("/book/create", json={"title": "Borrador"})
    assert created.status_code == 200, created.text
    book_id = created.json()["_id"]
    assert world.http.put(f"/book/edit/{book_id}", json={"title": "Química orgánica"}).status_code == 200
    assert _titles(world, q="quimica") == ["Química orgánica"] and _titles(world, q="borrador") == []
    row = world.http.get("/book/library").json()["items"][0]
    assert "search_terms" not in row and "search_terms" not in world.http.get(f"/book/{book_id}").json()


# ── the pass: stale counts ──────────────────────────────────────────────────

def test_counts_stored_under_the_old_rule_are_recounted_by_the_pass(world):
    """IMPORT-034: a Japanese document kept '8 words, 0 sections' until someone saved it."""
    content = _state(("heading", "第3章", "h1"), ("paragraph", JAPANESE, None), ("heading", "第4章", "h1"), ("paragraph", JAPANESE, None))
    stale = _insert(world, title="ノート", full_content=content, word_count=8, section_count=0)
    current = _insert(world, title="fine", full_content=content, word_count=999, section_count=9,
                      stats_version=STATS_VERSION, search_version=SEARCH_VERSION, search_terms=["fine"])
    assert world.run(book_maintenance.run_pass(world.raw["books"], pause=0)) == 1
    fixed = world.run(world.raw["books"].find_one({"_id": stale}))
    assert fixed["section_count"] == 2 and fixed["word_count"] > 100
    assert fixed["stats_version"] == STATS_VERSION and fixed["search_version"] == SEARCH_VERSION and "のーと" in fixed["search_terms"]
    untouched = world.run(world.raw["books"].find_one({"_id": current}))
    assert untouched["word_count"] == 999                         # already current: left alone
    assert world.run(book_maintenance.run_pass(world.raw["books"], pause=0)) == 0   # idempotent


def test_the_pass_works_in_batches_and_resumes_where_it_stopped(world):
    for n in range(7):
        _insert(world, title=f"doc {n}", full_content=_state(("paragraph", "one two three", None)))
    assert world.run(book_maintenance.run_batch(world.raw["books"], batch=3)) == 3      # stopped after one batch…
    assert world.run(world.raw["books"].count_documents(book_maintenance.BEHIND)) == 4
    assert world.run(book_maintenance.run_pass(world.raw["books"], batch=3, pause=0)) == 4   # …and the next run finishes
    assert world.run(world.raw["books"].count_documents(book_maintenance.BEHIND)) == 0


def test_a_document_that_cannot_be_counted_is_stamped_so_the_pass_ends(world):
    _insert(world, title="odd", full_content="{not json")
    with patch.object(book_maintenance, "document_stats", side_effect=RuntimeError("boom")):
        assert world.run(book_maintenance.run_pass(world.raw["books"], pause=0)) == 1
    assert world.run(world.raw["books"].count_documents(book_maintenance.BEHIND)) == 0


def test_the_rows_on_screen_are_recounted_at_once(world):
    content = _state(("heading", "第3章", "h1"), ("paragraph", JAPANESE, None))
    _insert(world, title="ノート", full_content=content, word_count=3, section_count=0)
    row = world.http.get("/book/library").json()["items"][0]
    assert row["section_count"] == 1 and row["word_count"] > 50


# ── an older import's facts ─────────────────────────────────────────────────

def test_editing_an_older_import_keeps_where_it_came_from(world):
    """IMPORT-033: the edit form sends an empty summary, which erased the only place the file name lived."""
    book_id = _insert(world, title="apuntes", source="imported", summary="Imported from mis apuntes.pdf - 3 pages",
                      full_content=_state(("paragraph", "uno dos", None)))
    response = world.http.put(f"/book/edit/{book_id}", json={"title": "Apuntes de biología", "summary": ""})
    assert response.status_code == 200, response.text
    stored = world.run(world.raw["books"].find_one({"_id": book_id}))
    assert stored["source_filename"] == "mis apuntes.pdf" and stored["page_count"] == 3 and stored["summary"] == ""
    assert response.json()["source_filename"] == "mis apuntes.pdf"


def test_the_pass_also_writes_an_older_imports_facts_and_a_client_cannot_forge_them(world):
    book_id = _insert(world, title="old", source="imported", summary="Imported from a - b.docx - 12 pages", page_count=12,
                      full_content=_state(("paragraph", "uno", None)))
    world.run(book_maintenance.run_pass(world.raw["books"], pause=0))
    assert world.run(world.raw["books"].find_one({"_id": book_id}))["source_filename"] == "a - b.docx"
    world.http.put(f"/book/edit/{book_id}", json={"title": "x", "source_filename": "forged.pdf", "stats_version": 99})
    stored = world.run(world.raw["books"].find_one({"_id": book_id}))
    assert stored["source_filename"] == "a - b.docx" and stored["stats_version"] == STATS_VERSION


def test_a_written_document_with_that_sentence_typed_in_it_is_left_alone():
    assert book_maintenance.legacy_import_facts({"source": "written", "summary": "Imported from x - 2 pages"}) == {}
    assert book_maintenance.legacy_import_facts({"source": "imported", "summary": "My own summary"}) == {}
    assert book_maintenance.legacy_import_facts({"source": "imported", "source_filename": "kept.pdf",
                                                  "summary": "Imported from other.pdf - 2 pages"}) == {}


def test_the_loop_runs_a_pass_and_stops_when_told_to_run_once(world):
    _insert(world, title="behind", full_content=_state(("paragraph", "uno dos", None)))
    world.run(book_maintenance.maintenance_loop(world.raw["books"], interval=0))
    assert world.run(world.raw["books"].count_documents(book_maintenance.BEHIND)) == 0
