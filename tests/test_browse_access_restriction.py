"""Browse access restriction — the dev/beta filter must survive every other filter.

`_build_browse_query` composes an access-restriction predicate (unrestricted
content, plus `dev`/`beta` content for a viewer holding that role) with the
ordinary browse filters. The restriction and the free-text search are
independent predicates that both have to hold, so neither may claim the single
top-level `$or` key on its own.

These tests evaluate the built query against real candidate documents rather
than asserting its shape, so they pin the *behaviour* (a restricted deck is not
returned) instead of the encoding. The fake collection follows the same
minimal-matcher pattern as `test_tts_public_access.py`.
"""
import sys
from unittest.mock import AsyncMock, MagicMock

# Stub imports that may be missing from the local dev env, before any app
# import. Mirrors the module-level stub block in test_public_curation.py.
for mod in ["app.models.agent_models", "langfuse", "langfuse.langchain"]:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()

import pytest
from bson import ObjectId

PUBLIC_DECK_OID = ObjectId("70b8d295f1d2c17f4e4b1111")
DEV_DECK_OID = ObjectId("70b8d295f1d2c17f4e4b2222")
BETA_DECK_OID = ObjectId("70b8d295f1d2c17f4e4b3333")

#: Every deck below carries this term in its name, so the search clause alone
#: matches all of them and only the access filter can separate them.
SEARCH_TERM = "biology"


def deck(oid: ObjectId, restricted_to=..., **overrides) -> dict:
    """A public, matching deck. `restricted_to=...` omits the key entirely."""
    metadata = {"category": "science", "language": "en", "tags": ["cells"]}
    if restricted_to is not ...:
        metadata["restricted_to"] = restricted_to

    document = {
        "_id": oid,
        "name": f"Foundations of {SEARCH_TERM.capitalize()}",
        "description": "Core concepts for a first review.",
        "is_public": True,
        "deleted_at": None,
        "public_metadata": metadata,
    }
    document.update(overrides)
    return document


UNRESTRICTED_DECK = deck(PUBLIC_DECK_OID)
DEV_DECK = deck(DEV_DECK_OID, restricted_to="dev")
BETA_DECK = deck(BETA_DECK_OID, restricted_to="beta")
ALL_DECKS = [UNRESTRICTED_DECK, DEV_DECK, BETA_DECK]


# ---------------------------------------------------------------------------
# Minimal query evaluator — only the operators _build_browse_query emits
# ---------------------------------------------------------------------------
def _resolve(doc: dict, path: str):
    """Read a dotted path, returning `_MISSING` when any segment is absent."""
    current = doc
    for segment in path.split("."):
        if not isinstance(current, dict) or segment not in current:
            return _MISSING
        current = current[segment]
    return current


_MISSING = object()


def _matches_operator(value, operator: str, expected) -> bool:
    if operator == "$exists":
        return (value is not _MISSING) is bool(expected)
    if operator == "$regex":
        import re

        candidates = value if isinstance(value, list) else [value]
        return any(
            isinstance(item, str) and re.search(expected, item, re.IGNORECASE)
            for item in candidates
        )
    if operator == "$options":  # consumed alongside $regex
        return True
    if operator == "$in":
        candidates = value if isinstance(value, list) else [value]
        return any(item in expected for item in candidates)
    raise AssertionError(f"unsupported operator in browse query: {operator}")


def _matches_field(doc: dict, field: str, expected) -> bool:
    value = _resolve(doc, field)
    if isinstance(expected, dict):
        return all(
            _matches_operator(value, operator, operand)
            for operator, operand in expected.items()
        )
    return value == expected


def matches(doc: dict, query: dict) -> bool:
    """True when `doc` satisfies `query`."""
    for field, expected in query.items():
        if field == "$and":
            if not all(matches(doc, clause) for clause in expected):
                return False
        elif field == "$or":
            if not any(matches(doc, clause) for clause in expected):
                return False
        elif not _matches_field(doc, field, expected):
            return False
    return True


class FakeDecksCollection:
    """Motor stand-in that actually evaluates the browse filter it is given."""

    def __init__(self, docs: list) -> None:
        self.docs = docs
        self.last_filter: dict = {}

    def _selected(self) -> list:
        return [doc for doc in self.docs if matches(doc, self.last_filter)]

    def find(self, query: dict):
        self.last_filter = query
        cursor = MagicMock()
        cursor.sort = MagicMock(return_value=cursor)
        cursor.skip = MagicMock(return_value=cursor)
        cursor.limit = MagicMock(return_value=cursor)
        cursor.to_list = AsyncMock(return_value=list(self._selected()))
        return cursor

    async def count_documents(self, query: dict) -> int:
        self.last_filter = query
        return len(self._selected())


async def browse(collection, **kwargs) -> dict:
    from app.services.public_content_service import PublicContentService

    service = PublicContentService({"decks": collection, "books": collection})
    return await service.browse_public_content(content_type="deck", **kwargs)


def returned_ids(result: dict) -> set:
    return {str(item["_id"]) for item in result["items"]}


# ---------------------------------------------------------------------------
# The regression: search must not dissolve the access restriction
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_search_does_not_leak_restricted_content_to_unentitled_viewer():
    """The bug: the search clause overwrote the access filter's `$or`.

    Every deck matches the search term, so a viewer holding neither role must
    still see only the unrestricted one. Before the fix the restriction was
    discarded and all three came back.
    """
    collection = FakeDecksCollection(ALL_DECKS)

    result = await browse(
        collection,
        search_query=SEARCH_TERM,
        viewer_role="user",
        viewer_is_beta=False,
    )

    assert returned_ids(result) == {str(PUBLIC_DECK_OID)}
    assert str(DEV_DECK_OID) not in returned_ids(result)
    assert str(BETA_DECK_OID) not in returned_ids(result)
    assert result["total"] == 1


@pytest.mark.asyncio
async def test_search_still_admits_content_the_viewer_is_entitled_to():
    """The fix must restrict, not over-restrict: a dev keeps seeing dev decks."""
    collection = FakeDecksCollection(ALL_DECKS)

    result = await browse(
        collection, search_query=SEARCH_TERM, viewer_role="dev", viewer_is_beta=False,
    )

    assert returned_ids(result) == {str(PUBLIC_DECK_OID), str(DEV_DECK_OID)}


@pytest.mark.asyncio
async def test_search_admits_beta_content_for_a_beta_viewer():
    collection = FakeDecksCollection(ALL_DECKS)

    result = await browse(
        collection, search_query=SEARCH_TERM, viewer_role="user", viewer_is_beta=True,
    )

    assert returned_ids(result) == {str(PUBLIC_DECK_OID), str(BETA_DECK_OID)}


@pytest.mark.asyncio
async def test_search_narrows_results_rather_than_widening_them():
    """Both predicates hold: a non-matching unrestricted deck stays out."""
    other = deck(ObjectId("70b8d295f1d2c17f4e4b4444"))
    other["name"] = "Introduction to Astronomy"
    other["description"] = "Stars and planets."
    other["public_metadata"]["tags"] = ["space"]
    collection = FakeDecksCollection([UNRESTRICTED_DECK, DEV_DECK, other])

    result = await browse(
        collection, search_query=SEARCH_TERM, viewer_role="user", viewer_is_beta=False,
    )

    assert returned_ids(result) == {str(PUBLIC_DECK_OID)}


@pytest.mark.asyncio
async def test_restriction_holds_when_search_combines_with_other_filters():
    """Category, language and tags must not displace the restriction either."""
    collection = FakeDecksCollection(ALL_DECKS)

    result = await browse(
        collection,
        category="science",
        tags=["cells"],
        language="en",
        difficulty=None,
        search_query=SEARCH_TERM,
        viewer_role="user",
        viewer_is_beta=False,
    )

    assert returned_ids(result) == {str(PUBLIC_DECK_OID)}


@pytest.mark.asyncio
async def test_restriction_without_search_is_unchanged():
    """The no-search path keeps its existing top-level `$or` encoding."""
    collection = FakeDecksCollection(ALL_DECKS)

    result = await browse(collection, viewer_role="user", viewer_is_beta=False)

    assert returned_ids(result) == {str(PUBLIC_DECK_OID)}
    assert {"public_metadata.restricted_to": None} in collection.last_filter["$or"]
    assert "$and" not in collection.last_filter


@pytest.mark.asyncio
async def test_browse_stays_bounded_under_search():
    """`page_size` is capped and the read is never unbounded."""
    from app.services.public_content_service import MAX_BROWSE_PAGE_SIZE

    collection = FakeDecksCollection(ALL_DECKS)
    cursors = []
    original_find = collection.find

    def recording_find(query):
        cursor = original_find(query)
        cursors.append(cursor)
        return cursor

    collection.find = recording_find

    result = await browse(
        collection,
        search_query=SEARCH_TERM,
        page_size=10_000,
        viewer_role="user",
        viewer_is_beta=False,
    )

    assert result["page_size"] == MAX_BROWSE_PAGE_SIZE
    assert cursors[0].limit.call_args[0][0] == MAX_BROWSE_PAGE_SIZE
    assert cursors[0].to_list.call_args[0][0] == MAX_BROWSE_PAGE_SIZE
