"""
ONB-004 — verified fork-owned onboarding activation.

Pins ADR-006 and the `Idempotent fork and activation` contract in
`docs/architecture-onboarding.md`: activation is a consequence of a verified,
completed copy of approved official content and of nothing else. What these
tests exist to prove is the ordering — the response is successful only after the
deck, its cards and the activation are all durable — and the repair path, where
a retry after a failed activation activates the user without copying a second
deck.

The fork fakes are reused from `test_fork_idempotency` so the state machine
under test here is the same one ONB-003 pinned, not a re-mocked stand-in.
`FakeUsersCollection` implements dotted-path matching the way MongoDB does,
including a missing field matching a queried `None`, because that equivalence
is exactly what makes the activation filter idempotent for legacy users.
"""
import copy
import os
import sys
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

# Stub imports that may be missing from the local dev env, before any app
# import. Mirrors the module-level stub block in test_fork_idempotency.py.
for mod in ["app.models.agent_models", "langfuse", "langfuse.langchain"]:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()

import pytest
from bson import ObjectId
from fastapi import HTTPException
from pydantic import ValidationError

from tests.test_fork_idempotency import (
    FORKED_OID,
    IDEMPOTENCY_KEY,
    NEW_OID,
    SOURCE_OID,
    FakeContentCollection,
    FakeCursor,
    FakeForkCollection,
    fork_record,
    forked_deck,
)

OFFICIAL_PUBLISHER_ID = "507f1f77bcf86cd799439099"
FORKER_ID = "507f1f77bcf86cd799439011"
COMMUNITY_PUBLISHER_ID = "507f1f77bcf86cd799439077"
FORKER_OID = ObjectId(FORKER_ID)

APPROVED_CURATION = {
    "status": "approved",
    "topic": "science",
    "learning_outcome": "Explain the core structures and processes of a cell.",
    "rank": 1,
    "reviewed_by": "507f1f77bcf86cd799439001",
    "reviewed_at": datetime(2026, 8, 1, tzinfo=timezone.utc),
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def official_source_deck(curation=None, **overrides) -> dict:
    """A public deck satisfying every clause of the official predicate."""
    deck = {
        "_id": SOURCE_OID,
        "user_id": OFFICIAL_PUBLISHER_ID,
        "name": "Foundations of Biology",
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


def community_deck(**overrides) -> dict:
    """Approved-looking curation, but published by an ordinary account.

    The realistic near-miss: a community deck cannot become official by
    carrying curation metadata, only by being owned by the configured
    publisher *and* reviewed.
    """
    return official_source_deck(user_id=COMMUNITY_PUBLISHER_ID, **overrides)


def incomplete_user(**overrides) -> dict:
    user = {
        "_id": FORKER_OID,
        "email": "learner@example.com",
        "wizard_completed": False,
        "onboarding": {
            "status": "incomplete",
            "last_meaningful_point": "first_deck",
            "postponed_at": None,
            "activated_at": None,
            "updated_at": datetime(2026, 8, 12, tzinfo=timezone.utc),
        },
    }
    user.update(overrides)
    return user


def _dotted_get(document: dict, path: str):
    node = document
    for part in path.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def _dotted_set(document: dict, path: str, value) -> None:
    node = document
    parts = path.split(".")
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[parts[-1]] = value


def _user_matches(document: dict, query: dict) -> bool:
    """Dotted-path equality; a missing field matches a queried `None`."""
    return all(_dotted_get(document, field) == value for field, value in query.items())


class FakeUsersCollection:
    """In-memory `users` supporting the dotted activation filter and `$set`."""

    def __init__(self, users=None, fail_on_update=False):
        self.users = [copy.deepcopy(u) for u in (users or [])]
        self.fail_on_update = fail_on_update
        self.applied_updates = []
        self.on_update = None

    async def find_one(self, query: dict, *args, **kwargs):
        for user in self.users:
            if _user_matches(user, query):
                return copy.deepcopy(user)
        return None

    async def find_one_and_update(self, query: dict, update: dict, **kwargs):
        if self.on_update is not None:
            await self.on_update()
        if self.fail_on_update:
            raise RuntimeError("users backend unavailable")

        for user in self.users:
            if _user_matches(user, query):
                set_doc = update.get("$set", {})
                self.applied_updates.append(dict(set_doc))
                for path, value in set_doc.items():
                    _dotted_set(user, path, value)
                return copy.deepcopy(user)
        return None


class RecordingCardsCollection:
    """Cards collection that reports how many copies are actually persisted."""

    def __init__(self, cards=None, fail_on_insert=False):
        self.cards = [dict(c) for c in (cards or [])]
        self.fail_on_insert = fail_on_insert
        self.inserted = 0
        self.deleted = 0

    def find(self, query: dict, *args, **kwargs):
        return FakeCursor([dict(c) for c in self.cards])

    async def insert_many(self, documents):
        if self.fail_on_insert:
            raise RuntimeError("cards backend unavailable")
        self.inserted += len(documents)
        return MagicMock(inserted_ids=[ObjectId() for _ in documents])

    async def delete_many(self, query: dict):
        self.deleted += 1
        return MagicMock(deleted_count=len(self.cards))


def source_cards(count: int = 2):
    return [
        {"_id": ObjectId(), "deck_id": SOURCE_OID, "front": f"q{i}", "back": f"a{i}"}
        for i in range(count)
    ]


def make_service(content=None, forks=None, cards=None, users=None):
    from app.services.public_content_service import PublicContentService

    if content is None:
        content = FakeContentCollection([official_source_deck()])
    forks = forks if forks is not None else FakeForkCollection()
    cards = cards if cards is not None else RecordingCardsCollection(source_cards())
    users = users if users is not None else FakeUsersCollection([incomplete_user()])

    database = {
        "decks": content,
        "content_forks": forks,
        "cards": cards,
        "users": users,
    }
    return PublicContentService(database), content, forks, cards, users


def with_official_publisher(user_id=OFFICIAL_PUBLISHER_ID):
    """Patch the official publisher configuration for one call."""
    return patch.dict(os.environ, {"OFFICIAL_PUBLISHER_USER_ID": user_id or ""})


async def onboarding_fork(service, **kwargs):
    return await service.fork_content(
        content_type="deck",
        original_content_id=str(SOURCE_OID),
        forking_user_id=FORKER_ID,
        onboarding_context=True,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Activation follows a completed copy — and only a completed copy
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_onboarding_fork_activates_and_returns_the_documented_body():
    service, content, forks, cards, users = make_service()

    with with_official_publisher():
        outcome = await onboarding_fork(service, idempotency_key=IDEMPOTENCY_KEY)

    assert outcome.created is True
    assert outcome.content["_id"] == str(NEW_OID)
    assert outcome.onboarding is not None
    assert outcome.onboarding.status == "activated"
    assert outcome.onboarding.activated_at.tzinfo is not None

    stored = users.users[0]
    assert stored["wizard_completed"] is True
    assert stored["onboarding"]["status"] == "activated"
    assert stored["onboarding"]["activated_at"] == outcome.onboarding.activated_at


@pytest.mark.asyncio
async def test_activation_is_written_only_after_deck_cards_and_claim_are_durable():
    """The ordering guarantee: nothing is activated on a half-finished copy."""
    service, content, forks, cards, users = make_service()
    observed = {}

    async def snapshot():
        observed["deck_inserts"] = content.inserts
        observed["cards_inserted"] = cards.inserted
        observed["fork_status"] = forks.documents[0]["status"]
        observed["forked_content_id"] = forks.documents[0]["forked_content_id"]

    users.on_update = snapshot

    with with_official_publisher():
        await onboarding_fork(service)

    assert observed["deck_inserts"] == 1
    assert observed["cards_inserted"] == 2
    assert observed["fork_status"] == "completed"
    assert observed["forked_content_id"] == str(NEW_OID)


@pytest.mark.asyncio
async def test_wizard_completed_and_onboarding_fields_land_in_one_update():
    """One document, one `$set` — no reader can see a half-activated user."""
    service, _, _, _, users = make_service()

    with with_official_publisher():
        await onboarding_fork(service)

    assert len(users.applied_updates) == 1
    set_doc = users.applied_updates[0]
    assert set_doc["wizard_completed"] is True
    assert set_doc["onboarding.status"] == "activated"
    assert set_doc["onboarding.activated_at"] == set_doc["onboarding.updated_at"]
    # Activation is not a screen: the resume point is left exactly as recorded.
    assert "onboarding.last_meaningful_point" not in set_doc


@pytest.mark.asyncio
async def test_a_failed_card_copy_activates_nothing():
    service, content, forks, cards, users = make_service(
        cards=RecordingCardsCollection(source_cards(), fail_on_insert=True)
    )

    with with_official_publisher():
        with pytest.raises(HTTPException) as error:
            await onboarding_fork(service)

    assert error.value.status_code == 500
    assert error.value.detail["code"] == "fork_failed"
    assert users.applied_updates == []
    assert users.users[0]["wizard_completed"] is False


@pytest.mark.asyncio
async def test_fork_in_progress_activates_nothing():
    """A concurrent claim is not a completed copy, so it cannot activate."""
    pending = fork_record(status="pending", forked_content_id=None)
    service, content, _, _, users = make_service(forks=FakeForkCollection([pending]))

    with with_official_publisher():
        with pytest.raises(HTTPException) as error:
            await onboarding_fork(service)

    assert error.value.status_code == 409
    assert error.value.detail["code"] == "fork_in_progress"
    assert content.inserts == 0
    assert users.applied_updates == []


# ---------------------------------------------------------------------------
# The source must be approved official content
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("deck", [
    official_source_deck(curation=dict(APPROVED_CURATION, status="draft")),
    official_source_deck(curation=dict(APPROVED_CURATION, status="in_review")),
    official_source_deck(curation=None, public_metadata={"category": "science"}),
    official_source_deck(user_id=COMMUNITY_PUBLISHER_ID),
])
async def test_unofficial_source_is_refused_and_never_activates(deck):
    service, content, forks, _, users = make_service(
        content=FakeContentCollection([deck])
    )

    with with_official_publisher():
        with pytest.raises(HTTPException) as error:
            await onboarding_fork(service)

    assert error.value.status_code == 409
    assert error.value.detail["code"] == "source_not_official"
    assert content.inserts == 0
    assert forks.documents == []
    assert users.applied_updates == []
    assert users.users[0]["wizard_completed"] is False


@pytest.mark.asyncio
async def test_topic_inconsistent_official_deck_is_refused():
    """ONB-002's topic/category clause, not a second definition of official."""
    deck = official_source_deck()
    deck["public_metadata"]["category"] = "history"
    service, content, _, _, users = make_service(content=FakeContentCollection([deck]))

    with with_official_publisher():
        with pytest.raises(HTTPException) as error:
            await onboarding_fork(service)

    assert error.value.detail["code"] == "source_not_official"
    assert content.inserts == 0
    assert users.applied_updates == []


@pytest.mark.asyncio
async def test_unconfigured_publisher_fails_closed():
    service, content, _, _, users = make_service()

    with with_official_publisher(user_id=""):
        with pytest.raises(HTTPException) as error:
            await onboarding_fork(service)

    assert error.value.detail["code"] == "source_not_official"
    assert content.inserts == 0
    assert users.applied_updates == []


@pytest.mark.asyncio
async def test_official_check_runs_before_the_claim_is_taken():
    """A source that cannot activate must not leave a fork record behind."""
    service, _, forks, _, _ = make_service(
        content=FakeContentCollection([community_deck()])
    )

    with with_official_publisher():
        with pytest.raises(HTTPException):
            await onboarding_fork(service)

    assert forks.documents == []


# ---------------------------------------------------------------------------
# Replay repairs a missed activation — the crux of ADR-006
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_replay_repairs_activation_without_copying_a_second_deck():
    content = FakeContentCollection([official_source_deck(), forked_deck()])
    service, content, forks, cards, users = make_service(
        content=content, forks=FakeForkCollection([fork_record()])
    )

    with with_official_publisher():
        outcome = await onboarding_fork(service)

    assert outcome.created is False
    assert outcome.content["_id"] == str(FORKED_OID)
    assert content.inserts == 0
    assert cards.inserted == 0
    assert len(forks.documents) == 1

    assert outcome.onboarding is not None
    assert outcome.onboarding.status == "activated"
    assert users.users[0]["wizard_completed"] is True


@pytest.mark.asyncio
async def test_second_replay_preserves_the_first_activation_time():
    """Activation is idempotent: the retry reports it, it does not move it."""
    content = FakeContentCollection([official_source_deck(), forked_deck()])
    service, content, _, _, users = make_service(
        content=content, forks=FakeForkCollection([fork_record()])
    )

    with with_official_publisher():
        first = await onboarding_fork(service)
        second = await onboarding_fork(service)

    assert second.created is False
    assert second.onboarding.activated_at == first.onboarding.activated_at
    assert len(users.applied_updates) == 1


@pytest.mark.asyncio
async def test_activated_legacy_user_without_the_subdocument_is_repaired():
    """`wizard_completed=True` alone carries no `activated_at` to report."""
    legacy = {"_id": FORKER_OID, "email": "legacy@example.com", "wizard_completed": True}
    service, _, _, _, users = make_service(users=FakeUsersCollection([legacy]))

    with with_official_publisher():
        outcome = await onboarding_fork(service)

    assert outcome.onboarding.activated_at is not None
    assert users.users[0]["onboarding"]["status"] == "activated"
    assert users.users[0]["onboarding"]["activated_at"] == outcome.onboarding.activated_at


@pytest.mark.asyncio
async def test_failed_activation_keeps_the_deck_and_reports_it_as_recoverable():
    service, content, forks, cards, users = make_service(
        users=FakeUsersCollection([incomplete_user()], fail_on_update=True)
    )

    with with_official_publisher():
        with pytest.raises(HTTPException) as error:
            await onboarding_fork(service)

    assert error.value.status_code == 500
    assert error.value.detail["code"] == "activation_failed"
    # The copy really happened, so the retry must replay it rather than redo it.
    assert content.inserts == 1
    assert cards.inserted == 2
    assert forks.documents[0]["status"] == "completed"


@pytest.mark.asyncio
async def test_the_retry_after_a_failed_activation_completes_the_journey():
    """End to end: activation dies, the identical request repairs everything."""
    users = FakeUsersCollection([incomplete_user()], fail_on_update=True)
    content = FakeContentCollection([official_source_deck()])
    service, content, forks, cards, users = make_service(content=content, users=users)

    with with_official_publisher():
        with pytest.raises(HTTPException):
            await onboarding_fork(service, idempotency_key=IDEMPOTENCY_KEY)

        # The deck the first attempt really created is the live replay target;
        # nothing is staged here, the retry has to find it on its own.
        users.fail_on_update = False
        outcome = await onboarding_fork(service, idempotency_key=IDEMPOTENCY_KEY)

    assert outcome.created is False
    assert content.inserts == 1
    assert cards.inserted == 2
    assert len(forks.documents) == 1
    assert outcome.onboarding.status == "activated"
    assert users.users[0]["wizard_completed"] is True


@pytest.mark.asyncio
async def test_a_missing_user_document_is_a_recoverable_activation_failure():
    service, _, _, _, _ = make_service(users=FakeUsersCollection([]))

    with with_official_publisher():
        with pytest.raises(HTTPException) as error:
            await onboarding_fork(service)

    assert error.value.status_code == 500
    assert error.value.detail["code"] == "activation_failed"


# ---------------------------------------------------------------------------
# Ordinary forks are untouched
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fork_without_onboarding_context_never_touches_the_user():
    service, content, _, _, users = make_service()

    outcome = await service.fork_content(
        content_type="deck",
        original_content_id=str(SOURCE_OID),
        forking_user_id=FORKER_ID,
    )

    assert outcome.created is True
    assert outcome.onboarding is None
    assert users.applied_updates == []
    assert users.users[0]["wizard_completed"] is False


@pytest.mark.asyncio
async def test_an_ordinary_fork_of_a_non_official_deck_still_succeeds():
    """The official predicate gates onboarding only, never normal library use."""
    service, content, _, _, users = make_service(
        content=FakeContentCollection([community_deck()])
    )

    outcome = await service.fork_content(
        content_type="deck",
        original_content_id=str(SOURCE_OID),
        forking_user_id=FORKER_ID,
    )

    assert outcome.created is True
    assert outcome.onboarding is None
    assert users.applied_updates == []


# ---------------------------------------------------------------------------
# Route contract
# ---------------------------------------------------------------------------

def test_fork_body_is_optional_so_existing_callers_are_unaffected():
    import inspect

    from app.routers.public_content import fork_deck

    parameter = inspect.signature(fork_deck).parameters["body"]
    assert parameter.default is None


def test_only_the_documented_context_value_is_accepted():
    from app.routers.public_content import ForkRequest

    assert ForkRequest().context is None
    assert ForkRequest(context="onboarding").context == "onboarding"
    with pytest.raises(ValidationError):
        ForkRequest(context="onboardng")


def fork_client(service):
    """A TestClient over the real route, so the wire format is what is asserted.

    The serialized body matters here rather than the handler's return value:
    whether the `onboarding` key is present at all is part of the contract
    ONB-005 builds its fixtures from.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.auth.firebase_auth import get_current_user
    from app.routers.public_content import get_public_service, router

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: {"user_id": FORKER_ID}
    app.dependency_overrides[get_public_service] = lambda: service
    return TestClient(app)


FORK_URL = f"/public/decks/{SOURCE_OID}/fork"


def test_onboarding_request_returns_the_documented_wire_body():
    service, _, _, _, _ = make_service()

    with with_official_publisher():
        response = fork_client(service).post(
            FORK_URL,
            json={"context": "onboarding"},
            headers={"Idempotency-Key": IDEMPOTENCY_KEY},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["message"] == "Deck forked successfully"
    assert body["created"] is True
    assert body["forked_deck"]["_id"] == str(NEW_OID)
    assert body["onboarding"]["status"] == "activated"
    assert body["onboarding"]["activated_at"].endswith("Z")


@pytest.mark.parametrize("payload", [None, {}])
def test_a_fork_without_onboarding_context_carries_no_onboarding_key(payload):
    service, _, _, _, users = make_service()

    response = fork_client(service).post(FORK_URL, json=payload)

    assert response.status_code == 200
    assert response.json()["created"] is True
    assert "onboarding" not in response.json()
    assert users.applied_updates == []


def test_existing_deck_fields_keep_their_nulls():
    """`response_model_exclude_none` must not reach inside `forked_deck`."""
    service, _, _, _, _ = make_service()

    body = fork_client(service).post(FORK_URL).json()

    assert body["forked_deck"]["public_metadata"] is None
    assert body["forked_deck"]["published_at"] is None


def test_unofficial_onboarding_request_returns_409_source_not_official():
    service, content, _, _, users = make_service(
        content=FakeContentCollection([community_deck()])
    )

    with with_official_publisher():
        response = fork_client(service).post(FORK_URL, json={"context": "onboarding"})

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "source_not_official"
    assert content.inserts == 0
    assert users.applied_updates == []


def test_the_response_schema_stays_documented():
    """The contract ONB-005 reads from OpenAPI must not degrade to `object`."""
    schema = fork_client(make_service()[0]).app.openapi()
    properties = schema["components"]["schemas"]["ForkDeckResponse"]["properties"]

    assert sorted(properties) == ["created", "forked_deck", "message", "onboarding"]


@pytest.mark.asyncio
async def test_route_passes_onboarding_context_through_to_the_service():
    from app.routers.public_content import ForkRequest, fork_deck

    service = MagicMock()
    service.fork_content = AsyncMock(
        return_value=MagicMock(created=True, content={"_id": "x"}, onboarding=None)
    )

    await fork_deck(
        deck_id=str(SOURCE_OID),
        body=ForkRequest(context="onboarding"),
        idempotency_key=None,
        current_user={"user_id": FORKER_ID},
        service=service,
    )

    assert service.fork_content.await_args.kwargs["onboarding_context"] is True


# ---------------------------------------------------------------------------
# Nothing else can activate
# ---------------------------------------------------------------------------

def test_journey_patch_can_never_write_activation():
    from app.models.User import OnboardingState
    from app.routers.users import _build_onboarding_update

    state = OnboardingState(updated_at=datetime.now(timezone.utc))
    now = datetime.now(timezone.utc)

    for action, point in (("record_point", "first_deck"), ("postpone", None)):
        set_doc = _build_onboarding_update(action, point, state, now)
        assert set_doc["onboarding.status"] == "incomplete"
        assert "wizard_completed" not in set_doc
        assert "onboarding.activated_at" not in set_doc


def test_activation_update_is_the_single_definition_of_the_transition():
    from app.models.User import onboarding_activation_update

    now = datetime.now(timezone.utc)

    assert onboarding_activation_update(now) == {
        "wizard_completed": True,
        "onboarding.status": "activated",
        "onboarding.activated_at": now,
        "onboarding.updated_at": now,
        "updated_at": now,
    }


def test_legacy_complete_wizard_route_remains_available():
    from app.routers.users import router

    route = next(
        r for r in router.routes
        if getattr(r, "path", "").endswith("/complete-wizard")
    )
    assert "POST" in route.methods


def test_only_the_fork_service_can_perform_activation():
    """FR-033 / ADR-006, enforced structurally rather than by inspection.

    Card generation, the journey PATCH and the legacy completion route must not
    grow an activation path later. Any new caller of the activation update fails
    this test and has to be justified against ADR-006 first.
    """
    import pathlib

    app_root = pathlib.Path(__file__).resolve().parent.parent / "app"
    callers = sorted(
        path.relative_to(app_root).as_posix()
        for path in app_root.rglob("*.py")
        if path.is_file()
        and path.name != "User.py"
        and "onboarding_activation_update" in path.read_text(encoding="utf-8")
    )

    assert callers == ["services/public_content_service.py"]
