"""
ONB-003 — idempotent deck-fork claim and replay.

Pins the fork state machine from ADR-005 and `docs/architecture-onboarding.md`:
the durable `(content type, source, user)` key, the claim taken before any copy,
replay of a completed deck fork as `200 created=false`, recoverable
`fork_in_progress`, stale-record recreation after the user deletes their fork,
cleanup of a partial copy, and unchanged behaviour for book forks and for deck
forks that supply no idempotency header.

`FakeForkCollection` enforces the unique key the way MongoDB does, so the race
tests exercise the real losing path (`DuplicateKeyError`) rather than a mock
that agrees with the code under test.
"""
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

# Stub imports that may be missing from the local dev env, before any app
# import. Mirrors the module-level stub block in test_public_curation.py.
for mod in ["app.models.agent_models", "langfuse", "langfuse.langchain"]:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()

import pytest
from bson import ObjectId
from fastapi import HTTPException
from pymongo.errors import DuplicateKeyError

SOURCE_OID = ObjectId("70b8d295f1d2c17f4e4b5678")
FORKED_OID = ObjectId("70b8d295f1d2c17f4e4b9999")
NEW_OID = ObjectId("70b8d295f1d2c17f4e4baaaa")
OWNER_ID = "507f1f77bcf86cd799439099"
FORKER_ID = "507f1f77bcf86cd799439011"
IDEMPOTENCY_KEY = "0b6f6a2e-4a6c-4c4e-9f77-4a0a2f7c1d55"

KEY_FIELDS = ("original_content_type", "original_content_id", "forked_by_user_id")


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

def source_deck(**overrides) -> dict:
    deck = {
        "_id": SOURCE_OID,
        "user_id": OWNER_ID,
        "name": "Foundations of Biology",
        "is_public": True,
        "deleted_at": None,
        "public_metadata": {"category": "science"},
    }
    deck.update(overrides)
    return deck


def forked_deck(oid=FORKED_OID, **overrides) -> dict:
    deck = {
        "_id": oid,
        "user_id": FORKER_ID,
        "name": "Foundations of Biology (Forked)",
        "is_public": False,
        "deleted_at": None,
        "forked_from": {"id": str(SOURCE_OID)},
    }
    deck.update(overrides)
    return deck


def fork_record(**overrides) -> dict:
    record = {
        "_id": ObjectId(),
        "original_content_type": "deck",
        "original_content_id": str(SOURCE_OID),
        "original_creator_id": OWNER_ID,
        "forked_by_user_id": FORKER_ID,
        "forked_content_id": str(FORKED_OID),
        "status": "completed",
        "idempotency_key": None,
        "failure_code": None,
        "forked_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }
    record.update(overrides)
    return record


def _matches(document: dict, query: dict) -> bool:
    """Equality matching, with a missing field matching a queried `None`."""
    return all(document.get(field) == value for field, value in query.items())


class FakeForkCollection:
    """In-memory `content_forks` that enforces the ADR-005 unique key."""

    def __init__(self, documents=None):
        self.documents = [dict(d) for d in (documents or [])]
        self.inserts = 0
        self.deletes = 0

    def _key_of(self, document: dict) -> tuple:
        return tuple(document.get(field) for field in KEY_FIELDS)

    async def find_one(self, query: dict, *args, **kwargs):
        for document in self.documents:
            if _matches(document, query):
                return dict(document)
        return None

    async def insert_one(self, document: dict):
        if any(self._key_of(d) == self._key_of(document) for d in self.documents):
            raise DuplicateKeyError("content_forks_unique_source_user")
        self.documents.append(dict(document))
        self.inserts += 1
        return MagicMock(inserted_id=document.get("_id"))

    async def update_one(self, query: dict, update: dict):
        for document in self.documents:
            if _matches(document, query):
                document.update(update.get("$set", {}))
                return MagicMock(matched_count=1, modified_count=1)
        return MagicMock(matched_count=0, modified_count=0)

    async def find_one_and_update(self, query: dict, update: dict, **kwargs):
        for document in self.documents:
            if _matches(document, query):
                document.update(update.get("$set", {}))
                return dict(document)
        return None

    async def delete_one(self, query: dict):
        for index, document in enumerate(self.documents):
            if _matches(document, query):
                self.documents.pop(index)
                self.deletes += 1
                return MagicMock(deleted_count=1)
        return MagicMock(deleted_count=0)

    def find(self, query: dict, *args, **kwargs):
        matched = [dict(d) for d in self.documents if _matches(d, query)]
        return FakeCursor(matched)


class FakeCursor:
    def __init__(self, documents):
        self.documents = documents

    def sort(self, *args, **kwargs):
        return self

    def skip(self, *args, **kwargs):
        return self

    def limit(self, *args, **kwargs):
        return self

    async def to_list(self, length=None):
        return self.documents[:length] if length else list(self.documents)


class FakeContentCollection:
    """Minimal decks/books collection: find_one, insert_one, update, delete."""

    def __init__(self, documents=None, insert_id=NEW_OID):
        self.documents = [dict(d) for d in (documents or [])]
        self.insert_id = insert_id
        self.inserts = 0
        self.deletes = 0
        self.increments = 0

    async def find_one(self, query: dict, *args, **kwargs):
        for document in self.documents:
            if _matches(document, query):
                return dict(document)
        return None

    async def insert_one(self, document: dict):
        stored = dict(document)
        stored["_id"] = self.insert_id
        self.documents.append(stored)
        self.inserts += 1
        return MagicMock(inserted_id=self.insert_id)

    async def update_one(self, query: dict, update: dict):
        if "$inc" in update:
            self.increments += 1
        for document in self.documents:
            if _matches(document, query):
                document.update(update.get("$set", {}))
                return MagicMock(matched_count=1)
        return MagicMock(matched_count=0)

    async def delete_one(self, query: dict):
        for index, document in enumerate(self.documents):
            if _matches(document, query):
                self.documents.pop(index)
                self.deletes += 1
                return MagicMock(deleted_count=1)
        return MagicMock(deleted_count=0)

    def find(self, query: dict, *args, **kwargs):
        return FakeCursor([dict(d) for d in self.documents if _matches(d, query)])


class FakeCardsCollection:
    def __init__(self, cards=None, fail_on_insert=False):
        self.cards = [dict(c) for c in (cards or [])]
        self.fail_on_insert = fail_on_insert
        self.deleted = 0

    def find(self, query: dict, *args, **kwargs):
        return FakeCursor([dict(c) for c in self.cards])

    async def insert_many(self, documents):
        if self.fail_on_insert:
            raise RuntimeError("cards backend unavailable")
        return MagicMock(inserted_ids=[ObjectId() for _ in documents])

    async def delete_many(self, query: dict):
        self.deleted += 1
        return MagicMock(deleted_count=len(self.cards))


def make_service(content=None, forks=None, cards=None, content_type="deck"):
    from app.services.public_content_service import PublicContentService

    content = content if content is not None else FakeContentCollection([source_deck()])
    forks = forks if forks is not None else FakeForkCollection()
    cards = cards if cards is not None else FakeCardsCollection()
    users = MagicMock()
    users.find_one = AsyncMock(return_value={"full_name": "Nowry"})

    database = {
        f"{content_type}s": content,
        "content_forks": forks,
        "cards": cards,
        "users": users,
    }
    return PublicContentService(database), content, forks, cards


async def fork(service, content_type="deck", **kwargs):
    return await service.fork_content(
        content_type=content_type,
        original_content_id=str(SOURCE_OID),
        forking_user_id=FORKER_ID,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# First fork — the claim is taken before any content exists
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_first_fork_creates_content_and_completes_the_claim():
    service, content, forks, _ = make_service()

    outcome = await fork(service)

    assert outcome.created is True
    assert outcome.content["_id"] == str(NEW_OID)
    assert len(forks.documents) == 1
    record = forks.documents[0]
    assert record["status"] == "completed"
    assert record["forked_content_id"] == str(NEW_OID)
    assert record["original_content_type"] == "deck"


@pytest.mark.asyncio
async def test_claim_is_inserted_before_the_copy():
    """The pending record must exist before the deck does, or the race is open."""
    service, content, forks, _ = make_service()
    observed = {}

    original_insert = content.insert_one

    async def spy(document):
        observed["records"] = [dict(d) for d in forks.documents]
        return await original_insert(document)

    content.insert_one = spy
    await fork(service)

    assert len(observed["records"]) == 1
    assert observed["records"][0]["status"] == "pending"
    assert observed["records"][0]["forked_content_id"] is None


@pytest.mark.asyncio
async def test_fork_counter_increments_only_on_creation():
    service, content, forks, _ = make_service()

    await fork(service)
    assert content.increments == 1

    await fork(service)  # replay
    assert content.increments == 1


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_completed_replay_returns_the_same_deck_and_created_false():
    content = FakeContentCollection([source_deck(), forked_deck()])
    service, content, forks, _ = make_service(
        content=content, forks=FakeForkCollection([fork_record()])
    )

    outcome = await fork(service)

    assert outcome.created is False
    assert outcome.content["_id"] == str(FORKED_OID)
    assert content.inserts == 0
    assert len(forks.documents) == 1


@pytest.mark.asyncio
async def test_replay_does_not_raise_already_forked_for_decks():
    content = FakeContentCollection([source_deck(), forked_deck()])
    service, _, _, _ = make_service(
        content=content, forks=FakeForkCollection([fork_record()])
    )

    outcome = await fork(service)  # would previously have been 409 already_forked

    assert outcome.created is False


@pytest.mark.asyncio
async def test_legacy_record_without_status_replays_as_completed():
    record = fork_record()
    record.pop("status")
    record.pop("updated_at")
    content = FakeContentCollection([source_deck(), forked_deck()])
    service, content, forks, _ = make_service(
        content=content, forks=FakeForkCollection([record])
    )

    outcome = await fork(service)

    assert outcome.created is False
    assert outcome.content["_id"] == str(FORKED_OID)
    assert content.inserts == 0


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_loser_of_the_insert_race_gets_recoverable_fork_in_progress():
    """The winner holds a fresh pending claim; the loser must not copy anything."""
    forks = FakeForkCollection()
    service, content, forks, _ = make_service(forks=forks)

    original_insert = forks.insert_one

    async def winner_claims_first(document):
        # Simulate the winner's claim landing between our read and our insert.
        forks.documents.append(fork_record(status="pending", forked_content_id=None))
        return await original_insert(document)

    forks.insert_one = winner_claims_first

    with pytest.raises(HTTPException) as error:
        await fork(service)

    assert error.value.status_code == 409
    assert error.value.detail["code"] == "fork_in_progress"
    assert content.inserts == 0
    assert len(forks.documents) == 1


@pytest.mark.asyncio
async def test_fresh_pending_claim_blocks_a_second_request():
    forks = FakeForkCollection([fork_record(status="pending", forked_content_id=None)])
    service, content, _, _ = make_service(forks=forks)

    with pytest.raises(HTTPException) as error:
        await fork(service)

    assert error.value.status_code == 409
    assert error.value.detail["code"] == "fork_in_progress"
    assert content.inserts == 0


@pytest.mark.asyncio
async def test_loser_replays_when_the_winner_finished_first():
    """Same race, winner already completed: the loser gets the winner's deck."""
    content = FakeContentCollection([source_deck(), forked_deck()])
    forks = FakeForkCollection()
    service, content, forks, _ = make_service(content=content, forks=forks)

    original_insert = forks.insert_one

    async def winner_completes_first(document):
        forks.documents.append(fork_record())
        return await original_insert(document)

    forks.insert_one = winner_completes_first

    outcome = await fork(service)

    assert outcome.created is False
    assert outcome.content["_id"] == str(FORKED_OID)
    assert content.inserts == 0


@pytest.mark.asyncio
async def test_two_requests_produce_one_record_and_one_deck():
    service, content, forks, _ = make_service()

    first = await fork(service)
    with pytest.raises(HTTPException):
        # The completed fork's deck is live, so the second request replays; make
        # the first deck unreachable only to prove no second claim is possible.
        forks.documents[0]["status"] = "pending"
        forks.documents[0]["updated_at"] = datetime.now(timezone.utc)
        await fork(service)

    assert first.created is True
    assert content.inserts == 1
    assert len(forks.documents) == 1


# ---------------------------------------------------------------------------
# Stale, failed and abandoned records
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_deleted_prior_fork_is_recreated_on_the_same_record():
    """The user deleted their forked deck, then forks the same source again."""
    deleted = forked_deck(deleted_at=datetime.now(timezone.utc))
    content = FakeContentCollection([source_deck(), deleted])
    forks = FakeForkCollection([fork_record()])
    service, content, forks, _ = make_service(content=content, forks=forks)

    outcome = await fork(service)

    assert outcome.created is True
    assert outcome.content["_id"] == str(NEW_OID)
    assert len(forks.documents) == 1
    assert forks.documents[0]["status"] == "completed"
    assert forks.documents[0]["forked_content_id"] == str(NEW_OID)
    assert forks.inserts == 0  # reclaimed, not re-inserted


@pytest.mark.asyncio
async def test_soft_deleted_prior_fork_is_not_deleted_by_recreation():
    """A deck the user deleted stays in its retention window untouched."""
    deleted = forked_deck(deleted_at=datetime.now(timezone.utc))
    content = FakeContentCollection([source_deck(), deleted])
    service, content, _, cards = make_service(
        content=content, forks=FakeForkCollection([fork_record()])
    )

    await fork(service)

    assert content.deletes == 0
    assert cards.deleted == 0


@pytest.mark.asyncio
async def test_failed_record_discards_its_partial_copy_before_recreating():
    partial = forked_deck()
    content = FakeContentCollection([source_deck(), partial])
    forks = FakeForkCollection([fork_record(status="failed", failure_code="RuntimeError")])
    service, content, forks, cards = make_service(content=content, forks=forks)

    outcome = await fork(service)

    assert outcome.created is True
    assert content.deletes == 1  # the abandoned partial copy
    assert cards.deleted == 1
    assert await content.find_one({"_id": FORKED_OID}) is None
    assert forks.documents[0]["forked_content_id"] == str(NEW_OID)
    assert forks.documents[0]["failure_code"] is None


@pytest.mark.asyncio
async def test_partial_copy_of_another_source_is_never_deleted():
    """The ownership/attribution guard is what makes the cleanup safe."""
    unrelated = forked_deck(forked_from={"id": str(ObjectId())})
    content = FakeContentCollection([source_deck(), unrelated])
    service, content, _, _ = make_service(
        content=content, forks=FakeForkCollection([fork_record(status="failed")])
    )

    await fork(service)

    assert content.deletes == 0


@pytest.mark.asyncio
async def test_abandoned_pending_claim_is_taken_over_after_the_window():
    from app.services.public_content_service import FORK_PENDING_TAKEOVER_SECONDS

    abandoned = fork_record(
        status="pending",
        forked_content_id=None,
        updated_at=datetime.now(timezone.utc)
        - timedelta(seconds=FORK_PENDING_TAKEOVER_SECONDS + 1),
    )
    service, content, forks, _ = make_service(forks=FakeForkCollection([abandoned]))

    outcome = await fork(service)

    assert outcome.created is True
    assert len(forks.documents) == 1
    assert forks.documents[0]["status"] == "completed"


@pytest.mark.asyncio
async def test_pending_claim_with_unreadable_timestamps_is_taken_over():
    stuck = fork_record(status="pending", forked_content_id=None, updated_at=None, forked_at=None)
    service, _, forks, _ = make_service(forks=FakeForkCollection([stuck]))

    outcome = await fork(service)

    assert outcome.created is True
    assert forks.documents[0]["status"] == "completed"


@pytest.mark.asyncio
async def test_losing_the_reclaim_race_returns_fork_in_progress():
    """Two requests reclaiming one stale record: the compare-and-set picks one."""
    content = FakeContentCollection([source_deck()])
    forks = FakeForkCollection([fork_record(status="failed")])
    forks.find_one_and_update = AsyncMock(return_value=None)
    service, content, _, _ = make_service(content=content, forks=forks)

    with pytest.raises(HTTPException) as error:
        await fork(service)

    assert error.value.status_code == 409
    assert error.value.detail["code"] == "fork_in_progress"
    assert content.inserts == 0


# ---------------------------------------------------------------------------
# Failure during copy
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_card_copy_failure_marks_the_claim_failed_and_returns_500():
    cards = FakeCardsCollection(cards=[{"_id": ObjectId(), "front": "a"}], fail_on_insert=True)
    service, content, forks, _ = make_service(cards=cards)

    with pytest.raises(HTTPException) as error:
        await fork(service)

    assert error.value.status_code == 500
    assert error.value.detail["code"] == "fork_failed"
    record = forks.documents[0]
    assert record["status"] == "failed"
    assert record["failure_code"] == "RuntimeError"
    # The partial deck id is retained so the retry can clean it up.
    assert record["forked_content_id"] == str(NEW_OID)


@pytest.mark.asyncio
async def test_a_failed_copy_is_never_replayed_as_a_completed_fork():
    cards = FakeCardsCollection(cards=[{"_id": ObjectId(), "front": "a"}], fail_on_insert=True)
    service, content, forks, _ = make_service(cards=cards)

    with pytest.raises(HTTPException):
        await fork(service)

    cards.fail_on_insert = False
    content.insert_id = ObjectId("70b8d295f1d2c17f4e4bbbbb")
    outcome = await fork(service)

    assert outcome.created is True
    assert outcome.content["_id"] == str(content.insert_id)
    assert content.deletes == 1  # exactly one visible deck remains


# ---------------------------------------------------------------------------
# Backward compatibility
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_book_fork_without_header_behaves_as_before():
    book = {
        "_id": SOURCE_OID,
        "user_id": OWNER_ID,
        "title": "A Public Book",
        "is_public": True,
        "deleted_at": None,
    }
    content = FakeContentCollection([book])
    service, content, forks, cards = make_service(content=content, content_type="book")

    outcome = await service.fork_content(
        content_type="book",
        original_content_id=str(SOURCE_OID),
        forking_user_id=FORKER_ID,
    )

    assert outcome.created is True
    assert outcome.content["title"] == "A Public Book (Forked)"
    assert forks.documents[0]["original_content_type"] == "book"


@pytest.mark.asyncio
async def test_duplicate_book_fork_replays_the_same_book():
    """ONB-014: books replay like decks — `created=False`, same book, no copy.

    This asserted `409 already_forked` until ONB-014. ADR-005's rationale — a
    retrying client cannot tell a lost response from an unwanted duplicate —
    never depended on the content type, and the split left one Fork button in
    `PublicView` behaving two different ways.
    """
    book = {
        "_id": SOURCE_OID, "user_id": OWNER_ID, "title": "A Public Book",
        "is_public": True, "deleted_at": None,
    }
    forked = {
        "_id": FORKED_OID, "user_id": FORKER_ID, "title": "A Public Book (Forked)",
        "deleted_at": None, "forked_from": {"id": str(SOURCE_OID)},
    }
    content = FakeContentCollection([book, forked])
    forks = FakeForkCollection([fork_record(original_content_type="book")])
    service, content, _, _ = make_service(
        content=content, forks=forks, content_type="book"
    )

    outcome = await service.fork_content(
        content_type="book",
        original_content_id=str(SOURCE_OID),
        forking_user_id=FORKER_ID,
    )

    assert outcome.created is False
    assert str(outcome.content["_id"]) == str(FORKED_OID)
    assert content.inserts == 0


@pytest.mark.asyncio
async def test_deck_fork_without_an_idempotency_key_records_none():
    service, _, forks, _ = make_service()

    await fork(service)

    assert forks.documents[0]["idempotency_key"] is None


@pytest.mark.asyncio
async def test_supplied_idempotency_key_is_recorded_for_correlation():
    service, _, forks, _ = make_service()

    await fork(service, idempotency_key=IDEMPOTENCY_KEY)

    assert forks.documents[0]["idempotency_key"] == IDEMPOTENCY_KEY


@pytest.mark.asyncio
async def test_a_changed_idempotency_key_cannot_create_a_second_fork():
    """Uniqueness rests on (deck, user), never on the correlation header."""
    service, content, forks, _ = make_service()

    await fork(service, idempotency_key=IDEMPOTENCY_KEY)
    outcome = await fork(service, idempotency_key="11111111-2222-3333-4444-555555555555")

    assert outcome.created is False
    assert content.inserts == 1
    assert len(forks.documents) == 1


@pytest.mark.asyncio
async def test_self_fork_and_missing_content_are_unchanged():
    service, _, _, _ = make_service(content=FakeContentCollection([source_deck(user_id=FORKER_ID)]))
    with pytest.raises(HTTPException) as self_fork:
        await fork(service)
    assert self_fork.value.status_code == 400
    assert self_fork.value.detail == "cannot_fork_own_content"

    empty, _, _, _ = make_service(content=FakeContentCollection([]))
    with pytest.raises(HTTPException) as missing:
        await fork(empty)
    assert missing.value.status_code == 404


@pytest.mark.asyncio
async def test_private_source_is_not_forkable():
    service, _, _, _ = make_service(content=FakeContentCollection([source_deck(is_public=False)]))

    with pytest.raises(HTTPException) as error:
        await fork(service)

    assert error.value.status_code == 404


# ---------------------------------------------------------------------------
# Idempotency-Key validation
# ---------------------------------------------------------------------------

def test_absent_idempotency_key_is_accepted():
    from app.services.public_content_service import validated_idempotency_key

    assert validated_idempotency_key(None) is None


def test_uuid_idempotency_key_is_normalised():
    from app.services.public_content_service import validated_idempotency_key

    assert validated_idempotency_key(f"  {IDEMPOTENCY_KEY.upper()}  ") == IDEMPOTENCY_KEY


@pytest.mark.parametrize("raw", ["", "not-a-uuid", "12345"])
def test_malformed_idempotency_key_is_rejected(raw):
    from app.services.public_content_service import validated_idempotency_key

    with pytest.raises(HTTPException) as error:
        validated_idempotency_key(raw)

    assert error.value.status_code == 400
    assert error.value.detail["code"] == "malformed_idempotency_key"


# ---------------------------------------------------------------------------
# The durable key and its index
# ---------------------------------------------------------------------------

def test_fork_key_names_content_type_source_and_user():
    from app.models.PublicContent import fork_key

    assert fork_key("deck", SOURCE_OID, FORKER_ID) == {
        "original_content_type": "deck",
        "original_content_id": str(SOURCE_OID),
        "forked_by_user_id": FORKER_ID,
    }


def test_fork_status_defaults_for_legacy_records():
    from app.models.PublicContent import fork_status

    assert fork_status({"forked_content_id": "abc"}) == "completed"
    assert fork_status({}) == "failed"
    assert fork_status({"status": "pending"}) == "pending"


def load_database_module():
    """Return the real `app.config.database` module.

    Several test modules install a bare MagicMock at
    `sys.modules["app.config.database"]`, so under full-suite ordering a plain
    import here yields mocks instead of the index helpers.
    """
    import importlib.util
    import inspect

    module = sys.modules.get("app.config.database")
    if inspect.iscoroutinefunction(getattr(module, "create_fork_indexes", None)):
        return module

    source = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "app", "config", "database.py",
    )
    spec = importlib.util.spec_from_file_location("_real_app_config_database", source)
    real = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(real)
    return real


@pytest.mark.asyncio
async def test_unique_fork_index_is_created_and_verified():
    database = load_database_module()
    collection = MagicMock()
    collection.create_index = AsyncMock()
    collection.index_information = AsyncMock(
        return_value={database.FORK_UNIQUE_INDEX: {}}
    )

    missing = await database.create_fork_indexes(collection)

    assert missing == []
    call = collection.create_index.await_args
    assert call.args[0] == [
        ("original_content_type", 1),
        ("original_content_id", 1),
        ("forked_by_user_id", 1),
    ]
    assert call.kwargs["unique"] is True
    assert call.kwargs["name"] == database.FORK_UNIQUE_INDEX


@pytest.mark.asyncio
async def test_index_failure_is_reported_not_raised():
    """Existing duplicates must not take the API down at startup."""
    database = load_database_module()
    collection = MagicMock()
    collection.create_index = AsyncMock(side_effect=DuplicateKeyError("duplicates"))
    collection.index_information = AsyncMock(return_value={})

    missing = await database.create_fork_indexes(collection)

    assert missing == [database.FORK_UNIQUE_INDEX]
