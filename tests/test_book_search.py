"""ADR-069: the library's search — folded terms written with the title, matched by prefix on an index."""
from datetime import datetime
from unittest.mock import patch

import pytest
from app.services import book_library, book_search
from app.services.book_search import fold, query_terms, search_terms
from tests._fake_world import fake_world

T0 = datetime(2026, 11, 1, 12, 0, 0)


@pytest.fixture
def world():
    with fake_world() as w, patch.object(book_library, "apply_title_collation", lambda cursor: cursor):
        w.act_as(w.new_user("reader"))
        yield w


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
    assert len(search_terms("x" * 5000 + " " + " ".join(f"word{n}" for n in range(500)))) <= book_search.MAX_TERMS
    assert query_terms("  Deep-Work (v2) ") == ["deep", "work", "v2"] and query_terms(".*") == []


# ── the library's search ────────────────────────────────────────────────────

def _book(world, title, **extra):
    return _insert(world, title=title, word_count=5, section_count=1,
                   **book_search.search_fields(title, extra.get("author"), extra.get("source")), **extra)


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


def test_a_document_without_terms_is_still_found_by_its_title_and_gets_terms_when_renamed(world):
    book_id = _insert(world, title="Old Notes on Cells", word_count=1, section_count=1)
    assert _titles(world, q="notes") == ["Old Notes on Cells"]
    assert world.http.put(f"/book/edit/{book_id}", json={"title": "Células"}).status_code == 200
    assert _titles(world, q="celulas") == ["Células"]


def test_the_query_is_a_prefix_match_on_the_indexed_terms():
    clauses = book_library.library_filter("u1", search="Álgebra II")["$or"]
    assert clauses[0] == {"$and": [{"search_terms": {"$regex": "^algebra"}}, {"search_terms": {"$regex": "^ii"}}]}
    assert clauses[1]["search_terms"] == {"$exists": False}


def test_a_rename_is_searchable_and_the_terms_never_leave_the_server(world):
    created = world.http.post("/book/create", json={"title": "Borrador"})
    assert created.status_code == 200, created.text
    book_id = created.json()["_id"]
    assert world.http.put(f"/book/edit/{book_id}", json={"title": "Química orgánica"}).status_code == 200
    assert _titles(world, q="quimica") == ["Química orgánica"] and _titles(world, q="borrador") == []
    row = world.http.get("/book/library").json()["items"][0]
    assert "search_terms" not in row and "search_terms" not in world.http.get(f"/book/{book_id}").json()
