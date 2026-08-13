"""
ONB-002 — trusted official curation and bounded curated browse.

Pins the curated browse contract from `docs/architecture-onboarding.md` and the
official predicate from ADR-004: server-derived `is_official`, deterministic
rank ordering that never consults popularity, an empty page for uncovered
topics, the documented 400s, and the guarantee that ordinary publishing can
never write approval, reviewer, review time or rank.

Handlers and the service are called directly with `Depends()` parameters passed
as plain kwargs — the same pattern as `test_onboarding_state.py` and
`test_users.py`.
"""
import os
import sys
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

# Stub imports that may be missing from the local dev env, before any app
# import. Mirrors the module-level stub block in test_onboarding_state.py.
for mod in ["app.models.agent_models", "langfuse", "langfuse.langchain"]:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()

import pytest
from bson import ObjectId
from fastapi import HTTPException
from pymongo.errors import DuplicateKeyError

OFFICIAL_USER_ID = "507f1f77bcf86cd799439099"
OTHER_USER_ID = "507f1f77bcf86cd799439011"
DECK_OID = ObjectId("70b8d295f1d2c17f4e4b5678")

APPROVED_CURATION = {
    "status": "approved",
    "topic": "science",
    "learning_outcome": "Explain the core structures and processes of a cell.",
    "rank": 1,
    "reviewed_by": "507f1f77bcf86cd799439001",
    "reviewed_at": datetime(2026, 8, 1, tzinfo=timezone.utc),
}


def official_deck(curation=None, **overrides) -> dict:
    """A deck that satisfies every clause of the official predicate."""
    deck = {
        "_id": DECK_OID,
        "user_id": OFFICIAL_USER_ID,
        "name": "Foundations of Biology",
        "description": "Core concepts for a first biology review.",
        "total_cards": 24,
        "is_public": True,
        "deleted_at": None,
        "public_metadata": {
            "category": "science",
            "language": "en",
            "curation": dict(APPROVED_CURATION if curation is None else curation),
        },
    }
    deck.update(overrides)
    return deck


def mock_decks_collection(docs=None, total=None):
    """Motor deck-collection mock exposing find().sort().skip().limit().to_list()."""
    collection = MagicMock()
    cursor = MagicMock()
    cursor.sort = MagicMock(return_value=cursor)
    cursor.skip = MagicMock(return_value=cursor)
    cursor.limit = MagicMock(return_value=cursor)
    cursor.to_list = AsyncMock(return_value=list(docs or []))
    collection.find = MagicMock(return_value=cursor)
    collection.count_documents = AsyncMock(
        return_value=total if total is not None else len(docs or [])
    )
    collection.find_one = AsyncMock(return_value=None)
    collection.update_one = AsyncMock(return_value=MagicMock(matched_count=1))
    return collection, cursor


def make_service(collection):
    from app.services.public_content_service import PublicContentService

    return PublicContentService({"decks": collection, "books": collection})


def load_database_module():
    """Return the real `app.config.database` module.

    Several test modules install a bare MagicMock at
    `sys.modules["app.config.database"]`, so under full-suite ordering a plain
    import here yields mocks instead of the index helpers. Loading the module
    from source under a private name gives the real functions without mutating
    `sys.modules` for anyone else.
    """
    import importlib.util
    import inspect

    module = sys.modules.get("app.config.database")
    if inspect.iscoroutinefunction(getattr(module, "create_curation_indexes", None)):
        return module

    source = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "app", "config", "database.py",
    )
    spec = importlib.util.spec_from_file_location("_real_app_config_database", source)
    real = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(real)
    return real


def with_official_publisher(user_id=OFFICIAL_USER_ID, name=None):
    """Patch the official publisher configuration for one call."""
    env = {"OFFICIAL_PUBLISHER_USER_ID": user_id or ""}
    if name is not None:
        env["OFFICIAL_PUBLISHER_NAME"] = name
    return patch.dict(os.environ, env)


async def curated_browse(collection, **kwargs):
    service = make_service(collection)
    return await service.browse_public_content(
        content_type="deck",
        category=kwargs.pop("category", "science"),
        sort_by=kwargs.pop("sort_by", "curated"),
        official=kwargs.pop("official", True),
        page=kwargs.pop("page", 1),
        page_size=kwargs.pop("page_size", 3),
        **kwargs,
    )


def applied_query(collection) -> dict:
    return collection.find.call_args[0][0]


# ---------------------------------------------------------------------------
# The official predicate — server-derived, never stored
# ---------------------------------------------------------------------------

def test_predicate_accepts_a_fully_qualified_official_deck():
    from app.models.PublicContent import is_official_deck

    assert is_official_deck(official_deck(), OFFICIAL_USER_ID) is True


@pytest.mark.parametrize(
    "overrides,curation,reason",
    [
        ({}, {**APPROVED_CURATION, "status": "in_review"}, "unapproved"),
        ({}, {**APPROVED_CURATION, "status": "draft"}, "draft"),
        ({}, {**APPROVED_CURATION, "status": "rejected"}, "rejected"),
        ({"is_public": False}, None, "non-public"),
        ({"deleted_at": datetime(2026, 8, 1, tzinfo=timezone.utc)}, None, "deleted"),
        ({"user_id": OTHER_USER_ID}, None, "wrong owner"),
    ],
)
def test_predicate_excludes_disqualified_decks(overrides, curation, reason):
    from app.models.PublicContent import is_official_deck

    deck = official_deck(curation=curation, **overrides)
    assert is_official_deck(deck, OFFICIAL_USER_ID) is False, reason


def test_predicate_excludes_topic_category_mismatch():
    """An approval filed under a different topic than the browse category."""
    from app.models.PublicContent import is_official_deck

    deck = official_deck(curation={**APPROVED_CURATION, "topic": "history"})
    assert is_official_deck(deck, OFFICIAL_USER_ID) is False


def test_predicate_excludes_deck_without_curation():
    from app.models.PublicContent import is_official_deck

    deck = official_deck()
    deck["public_metadata"].pop("curation")
    assert is_official_deck(deck, OFFICIAL_USER_ID) is False


def test_predicate_fails_closed_without_configured_publisher():
    """No configuration means nothing can ever be official."""
    from app.models.PublicContent import is_official_deck

    assert is_official_deck(official_deck(), None) is False


def test_predicate_ignores_a_client_supplied_official_flag():
    """A stored `is_official` key carries no authority whatsoever."""
    from app.models.PublicContent import is_official_deck

    deck = official_deck(user_id=OTHER_USER_ID, is_official=True)
    assert is_official_deck(deck, OFFICIAL_USER_ID) is False


def test_unreadable_curation_is_not_an_approval():
    """A malformed stored subdocument must not be treated as approved."""
    from app.models.PublicContent import is_official_deck, parse_deck_curation

    deck = official_deck(curation={"status": "approved", "topic": "science", "rank": 0})
    assert parse_deck_curation(deck) is None
    assert is_official_deck(deck, OFFICIAL_USER_ID) is False


# ---------------------------------------------------------------------------
# Curated browse — query, ordering, bounds
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_curated_browse_filters_on_the_official_predicate():
    collection, _ = mock_decks_collection([official_deck()])

    with with_official_publisher():
        await curated_browse(collection)

    query = applied_query(collection)
    assert query["is_public"] is True
    assert query["deleted_at"] is None
    assert query["user_id"] == OFFICIAL_USER_ID
    assert query["public_metadata.curation.status"] == "approved"
    assert query["public_metadata.curation.topic"] == "science"
    assert query["public_metadata.category"] == "science"


@pytest.mark.asyncio
async def test_curated_browse_orders_by_rank_then_stable_id():
    collection, cursor = mock_decks_collection([official_deck()])

    with with_official_publisher():
        await curated_browse(collection)

    sort_spec = cursor.sort.call_args[0][0]
    assert sort_spec == [("public_metadata.curation.rank", 1), ("_id", 1)]


@pytest.mark.asyncio
async def test_curated_browse_never_consults_popularity():
    collection, cursor = mock_decks_collection([official_deck()])

    with with_official_publisher():
        await curated_browse(collection)

    sorted_fields = {field for field, _ in cursor.sort.call_args[0][0]}
    assert not any("views" in f or "rating" in f for f in sorted_fields)


@pytest.mark.asyncio
async def test_curated_browse_returns_at_most_three_for_onboarding():
    collection, cursor = mock_decks_collection([official_deck()], total=7)

    with with_official_publisher():
        result = await curated_browse(collection, page_size=3)

    cursor.limit.assert_called_once_with(3)
    cursor.to_list.assert_awaited_once_with(3)
    assert result["page_size"] == 3


@pytest.mark.asyncio
async def test_browse_page_size_is_clamped_to_one_hundred():
    """Bounded read even if a caller bypasses the router's Query bound."""
    collection, cursor = mock_decks_collection([])

    with with_official_publisher():
        result = await curated_browse(collection, page_size=5000)

    cursor.limit.assert_called_once_with(100)
    cursor.to_list.assert_awaited_once_with(100)
    assert result["page_size"] == 100


@pytest.mark.asyncio
async def test_curated_browse_compares_topic_and_category_without_a_category():
    collection, _ = mock_decks_collection([])

    with with_official_publisher():
        await curated_browse(collection, category=None)

    query = applied_query(collection)
    assert query["$expr"] == {
        "$eq": ["$public_metadata.curation.topic", "$public_metadata.category"]
    }


@pytest.mark.asyncio
async def test_uncovered_topic_returns_a_successful_empty_page():
    """FR-059: missing coverage is an expected outcome, not an error."""
    collection, _ = mock_decks_collection([], total=0)

    with with_official_publisher():
        result = await curated_browse(collection, category="philosophy")

    assert result == {
        "items": [],
        "total": 0,
        "page": 1,
        "page_size": 3,
        "total_pages": 0,
    }


@pytest.mark.asyncio
async def test_unconfigured_publisher_returns_an_empty_page_without_querying():
    collection, _ = mock_decks_collection([official_deck()])

    with with_official_publisher(user_id=""):
        result = await curated_browse(collection)

    assert result["items"] == []
    assert result["total"] == 0
    collection.find.assert_not_called()
    collection.count_documents.assert_not_awaited()


# ---------------------------------------------------------------------------
# Curated browse — item projection
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_official_item_carries_typed_curation_publisher_and_flag():
    collection, _ = mock_decks_collection([official_deck()])

    with with_official_publisher():
        result = await curated_browse(collection)

    item = result["items"][0]
    assert item["is_official"] is True
    assert item["curation"] == {
        "topic": "science",
        "learning_outcome": "Explain the core structures and processes of a cell.",
        "rank": 1,
    }
    assert item["publisher"] == {"name": "Nowry"}
    assert item["_id"] == str(DECK_OID)
    assert item["total_cards"] == 24


@pytest.mark.asyncio
async def test_official_item_never_exposes_reviewer_identity():
    collection, _ = mock_decks_collection([official_deck()])

    with with_official_publisher():
        result = await curated_browse(collection)

    assert set(result["items"][0]["curation"]) == {"topic", "learning_outcome", "rank"}


@pytest.mark.asyncio
async def test_raw_curation_is_stripped_from_echoed_public_metadata():
    """`public_metadata` is echoed verbatim to anonymous browsers, so the stored
    subdocument (which carries reviewer identity and review time) must not ride
    along inside it."""
    collection, _ = mock_decks_collection([official_deck()])

    with with_official_publisher():
        result = await curated_browse(collection)

    metadata = result["items"][0]["public_metadata"]
    assert "curation" not in metadata
    assert metadata["category"] == "science"
    assert "editor" not in str(result["items"][0])
    assert "reviewed_by" not in str(result["items"][0])


@pytest.mark.asyncio
async def test_single_deck_read_does_not_leak_reviewer_identity():
    """The detail route echoes public_metadata too."""
    collection, _ = mock_decks_collection([])
    collection.find_one = AsyncMock(return_value=official_deck())
    service = make_service(collection)

    with with_official_publisher():
        result = await service.get_public_content_by_id(
            content_type="deck", content_id=str(DECK_OID), track_view=False
        )

    assert "curation" not in result["public_metadata"]
    assert "reviewed_by" not in str(result)


@pytest.mark.asyncio
async def test_unapproved_curation_is_also_stripped_from_metadata():
    deck = official_deck(curation={**APPROVED_CURATION, "status": "in_review"})
    collection, _ = mock_decks_collection([deck])

    with with_official_publisher():
        result = await curated_browse(collection, official=False, sort_by="recent")

    assert "curation" not in result["items"][0]["public_metadata"]


@pytest.mark.asyncio
async def test_publisher_display_name_is_configurable():
    collection, _ = mock_decks_collection([official_deck()])

    with with_official_publisher(name="Nowry Editorial"):
        result = await curated_browse(collection)

    assert result["items"][0]["publisher"] == {"name": "Nowry Editorial"}


@pytest.mark.asyncio
async def test_non_official_item_is_flagged_false_and_hides_curation():
    """Editorial state of an unapproved candidate stays private."""
    deck = official_deck(curation={**APPROVED_CURATION, "status": "in_review"})
    collection, _ = mock_decks_collection([deck])

    with with_official_publisher():
        result = await curated_browse(collection, official=False, sort_by="recent")

    item = result["items"][0]
    assert item["is_official"] is False
    assert item["curation"] is None


@pytest.mark.asyncio
async def test_forged_official_flag_on_a_stored_document_is_overwritten():
    deck = official_deck(user_id=OTHER_USER_ID, is_official=True)
    deck["public_metadata"]["curation"]["reviewed_by"] = "attacker"
    collection, _ = mock_decks_collection([deck])

    with with_official_publisher():
        result = await curated_browse(collection, official=False, sort_by="recent")

    assert result["items"][0]["is_official"] is False
    assert result["items"][0]["publisher"] is None


# ---------------------------------------------------------------------------
# Ordinary browse is unchanged
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ordinary_browse_query_and_sort_are_untouched():
    collection, cursor = mock_decks_collection([])
    service = make_service(collection)

    await service.browse_public_content(content_type="deck", page=1, page_size=20)

    query = applied_query(collection)
    assert "user_id" not in query
    assert not [key for key in query if "curation" in key]
    assert "$expr" not in query
    assert cursor.sort.call_args[0][0] == [("published_at", -1)]


@pytest.mark.asyncio
async def test_ordinary_browse_keeps_its_access_restriction_filter():
    collection, _ = mock_decks_collection([])
    service = make_service(collection)

    await service.browse_public_content(
        content_type="deck", viewer_role="user", viewer_is_beta=False
    )

    restrictions = applied_query(collection)["$or"]
    assert {"public_metadata.restricted_to": None} in restrictions
    assert {"public_metadata.restricted_to": "dev"} not in restrictions


@pytest.mark.asyncio
async def test_official_browse_still_applies_access_restrictions():
    collection, _ = mock_decks_collection([])

    with with_official_publisher():
        await curated_browse(collection)

    assert "$or" in applied_query(collection)


# ---------------------------------------------------------------------------
# Router — documented 400s
# ---------------------------------------------------------------------------

def test_curated_sort_requires_official():
    from app.routers.public_content import _validate_curated_browse

    with pytest.raises(HTTPException) as exc:
        _validate_curated_browse(official=False, sort_by="curated", category="science")

    assert exc.value.status_code == 400
    assert exc.value.detail["code"] == "curated_sort_requires_official"


def test_official_browse_rejects_a_non_taxonomy_category():
    from app.routers.public_content import _validate_curated_browse

    with pytest.raises(HTTPException) as exc:
        _validate_curated_browse(official=True, sort_by="curated", category="Science")

    assert exc.value.status_code == 400
    assert exc.value.detail["code"] == "invalid_official_category"


def test_valid_official_combinations_pass_validation():
    from app.routers.public_content import _validate_curated_browse

    _validate_curated_browse(official=True, sort_by="curated", category="science")
    _validate_curated_browse(official=True, sort_by="curated", category=None)
    _validate_curated_browse(official=False, sort_by="recent", category="anything")


# ---------------------------------------------------------------------------
# Ordinary publishing cannot reach curation — the security core
# ---------------------------------------------------------------------------

def test_publish_request_has_no_curation_surface():
    from app.routers.public_content import PublishRequest

    forbidden = {"curation", "status", "rank", "reviewed_by", "reviewed_at", "is_official"}
    assert forbidden.isdisjoint(PublishRequest.model_fields)


def test_publish_request_silently_drops_a_curation_key():
    from app.routers.public_content import PublishRequest

    request = PublishRequest(category="science", curation=dict(APPROVED_CURATION))
    assert "curation" not in request.model_dump()


@pytest.mark.asyncio
async def test_publish_cannot_write_curation_even_when_injected():
    """Defense in depth: the service strips curation before validating."""
    collection, _ = mock_decks_collection([])
    collection.find_one = AsyncMock(
        return_value={"_id": DECK_OID, "user_id": OTHER_USER_ID, "is_public": False}
    )
    service = make_service(collection)

    await service.publish_content(
        content_type="deck",
        content_id=str(DECK_OID),
        user_id=OTHER_USER_ID,
        public_metadata={"category": "science", "curation": dict(APPROVED_CURATION)},
    )

    written = collection.update_one.call_args[0][1]["$set"]["public_metadata"]
    assert written["curation"] is None


@pytest.mark.asyncio
async def test_republishing_preserves_a_completed_editorial_review():
    """Unpublishing is an availability action, not an editorial one."""
    collection, _ = mock_decks_collection([])
    collection.find_one = AsyncMock(
        return_value={
            "_id": DECK_OID,
            "user_id": OFFICIAL_USER_ID,
            "is_public": False,
            "public_metadata": {
                "category": "science",
                "curation": dict(APPROVED_CURATION),
            },
        }
    )
    service = make_service(collection)

    await service.publish_content(
        content_type="deck",
        content_id=str(DECK_OID),
        user_id=OFFICIAL_USER_ID,
        public_metadata={"category": "science"},
    )

    written = collection.update_one.call_args[0][1]["$set"]["public_metadata"]
    assert written["curation"]["status"] == "approved"


def test_curation_request_rejects_server_owned_fields():
    from pydantic import ValidationError

    from app.routers.public_content import DeckCurationRequest

    for forged in ("reviewed_by", "reviewed_at", "is_official"):
        with pytest.raises(ValidationError):
            DeckCurationRequest(
                status="approved",
                topic="science",
                learning_outcome="Explain a cell.",
                rank=1,
                **{forged: "forged"},
            )


def test_curation_route_is_admin_gated():
    from app.auth.dependencies import require_admin
    from app.routers.public_content import router

    route = next(
        r for r in router.routes if r.path.endswith("/curation") and "PUT" in r.methods
    )
    assert require_admin in [d.call for d in route.dependant.dependencies]


# ---------------------------------------------------------------------------
# The trusted curation operation
# ---------------------------------------------------------------------------

async def call_set_curation(collection, curation, deck_id=None, reviewer="reviewer-1"):
    service = make_service(collection)
    return await service.set_deck_curation(
        deck_id=deck_id or str(DECK_OID),
        curation=dict(curation),
        reviewer_id=reviewer,
    )


APPROVAL_BODY = {
    "status": "approved",
    "topic": "science",
    "learning_outcome": "Explain the core structures and processes of a cell.",
    "rank": 1,
}


@pytest.mark.asyncio
async def test_curation_write_stamps_server_reviewer_and_time():
    deck = official_deck()
    deck["public_metadata"].pop("curation")
    collection, _ = mock_decks_collection([])
    collection.find_one = AsyncMock(return_value=deck)

    with with_official_publisher():
        result = await call_set_curation(collection, APPROVAL_BODY, reviewer="editor-7")

    written = collection.update_one.call_args[0][1]["$set"]["public_metadata.curation"]
    assert written["reviewed_by"] == "editor-7"
    assert isinstance(written["reviewed_at"], datetime)
    assert written["status"] == "approved"
    assert result["deck_id"] == str(DECK_OID)


@pytest.mark.asyncio
async def test_curation_write_ignores_a_forged_reviewer_in_the_payload():
    deck = official_deck()
    collection, _ = mock_decks_collection([])
    collection.find_one = AsyncMock(return_value=deck)

    with with_official_publisher():
        await call_set_curation(
            collection,
            {**APPROVAL_BODY, "reviewed_by": "attacker", "reviewed_at": "2000-01-01"},
            reviewer="editor-7",
        )

    written = collection.update_one.call_args[0][1]["$set"]["public_metadata.curation"]
    assert written["reviewed_by"] == "editor-7"
    assert written["reviewed_at"].year >= 2026


@pytest.mark.asyncio
async def test_curation_rejects_a_deck_outside_the_official_account():
    collection, _ = mock_decks_collection([])
    collection.find_one = AsyncMock(return_value=official_deck(user_id=OTHER_USER_ID))

    with with_official_publisher():
        with pytest.raises(HTTPException) as exc:
            await call_set_curation(collection, APPROVAL_BODY)

    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "not_official_publisher"


@pytest.mark.asyncio
async def test_curation_requires_a_configured_publisher():
    collection, _ = mock_decks_collection([])

    with with_official_publisher(user_id=""):
        with pytest.raises(HTTPException) as exc:
            await call_set_curation(collection, APPROVAL_BODY)

    assert exc.value.status_code == 503
    assert exc.value.detail["code"] == "official_publisher_not_configured"


@pytest.mark.asyncio
async def test_curation_rejects_a_missing_deck():
    collection, _ = mock_decks_collection([])
    collection.find_one = AsyncMock(return_value=None)

    with with_official_publisher():
        with pytest.raises(HTTPException) as exc:
            await call_set_curation(collection, APPROVAL_BODY)

    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_approval_rejects_a_topic_that_does_not_match_the_category():
    collection, _ = mock_decks_collection([])
    collection.find_one = AsyncMock(return_value=official_deck())

    with with_official_publisher():
        with pytest.raises(HTTPException) as exc:
            await call_set_curation(collection, {**APPROVAL_BODY, "topic": "history"})

    assert exc.value.status_code == 400
    assert exc.value.detail["code"] == "curation_topic_category_mismatch"


@pytest.mark.asyncio
async def test_approval_rejects_an_unpublished_deck():
    collection, _ = mock_decks_collection([])
    collection.find_one = AsyncMock(return_value=official_deck(is_public=False))

    with with_official_publisher():
        with pytest.raises(HTTPException) as exc:
            await call_set_curation(collection, APPROVAL_BODY)

    assert exc.value.status_code == 400
    assert exc.value.detail["code"] == "deck_not_public"


@pytest.mark.asyncio
async def test_approval_rejects_an_empty_deck():
    collection, _ = mock_decks_collection([])
    collection.find_one = AsyncMock(return_value=official_deck(total_cards=0))

    with with_official_publisher():
        with pytest.raises(HTTPException) as exc:
            await call_set_curation(collection, APPROVAL_BODY)

    assert exc.value.status_code == 400
    assert exc.value.detail["code"] == "deck_has_no_cards"


@pytest.mark.asyncio
async def test_duplicate_approved_rank_is_a_conflict():
    collection, _ = mock_decks_collection([])
    collection.find_one = AsyncMock(return_value=official_deck())
    collection.update_one = AsyncMock(side_effect=DuplicateKeyError("duplicate"))

    with with_official_publisher():
        with pytest.raises(HTTPException) as exc:
            await call_set_curation(collection, APPROVAL_BODY)

    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "curated_rank_taken"


@pytest.mark.asyncio
async def test_non_approved_statuses_skip_the_approval_gates():
    """A candidate may be filed under any topic while still in review."""
    in_review = {**APPROVED_CURATION, "status": "in_review", "topic": "history"}
    collection, _ = mock_decks_collection([])
    # First read loads the deck for validation; the second is the post-write
    # re-read that `is_official` is derived from.
    collection.find_one = AsyncMock(
        side_effect=[
            official_deck(total_cards=0),
            official_deck(curation=in_review, total_cards=0),
        ]
    )

    with with_official_publisher():
        result = await call_set_curation(
            collection, {**APPROVAL_BODY, "status": "in_review", "topic": "history"}
        )

    assert result["status"] == "in_review"
    assert result["is_official"] is False


@pytest.mark.asyncio
async def test_approval_reports_official_status_from_the_stored_result():
    """`is_official` in the response is re-derived, not assumed from the write."""
    collection, _ = mock_decks_collection([])
    collection.find_one = AsyncMock(
        side_effect=[official_deck(), official_deck()]
    )

    with with_official_publisher():
        result = await call_set_curation(collection, APPROVAL_BODY)

    assert result["is_official"] is True


# ---------------------------------------------------------------------------
# Indexes
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_curated_indexes_are_created_and_verified():
    database = load_database_module()
    CURATED_BROWSE_INDEX = database.CURATED_BROWSE_INDEX
    CURATED_APPROVED_RANK_INDEX = database.CURATED_APPROVED_RANK_INDEX
    create_curation_indexes = database.create_curation_indexes

    collection = MagicMock()
    collection.create_index = AsyncMock()
    collection.index_information = AsyncMock(
        return_value={CURATED_BROWSE_INDEX: {}, CURATED_APPROVED_RANK_INDEX: {}}
    )

    missing = await create_curation_indexes(collection)

    assert missing == []
    browse_call, unique_call = collection.create_index.await_args_list
    assert browse_call.kwargs["name"] == CURATED_BROWSE_INDEX
    assert browse_call.args[0] == [
        ("is_public", 1),
        ("deleted_at", 1),
        ("public_metadata.curation.status", 1),
        ("public_metadata.curation.topic", 1),
        ("public_metadata.curation.rank", 1),
        ("_id", 1),
    ]
    assert unique_call.kwargs["unique"] is True
    assert unique_call.kwargs["partialFilterExpression"] == {
        "public_metadata.curation.status": "approved"
    }


@pytest.mark.asyncio
async def test_missing_curated_index_is_reported_not_swallowed():
    database = load_database_module()
    CURATED_BROWSE_INDEX = database.CURATED_BROWSE_INDEX
    create_curation_indexes = database.create_curation_indexes

    collection = MagicMock()
    collection.create_index = AsyncMock(side_effect=[None, Exception("duplicate ranks")])
    collection.index_information = AsyncMock(return_value={CURATED_BROWSE_INDEX: {}})

    missing = await create_curation_indexes(collection)

    assert missing == ["decks_curation_approved_topic_rank"]
