"""
ONB-003 — bounded reconciliation of historical duplicate fork records.

The unique fork key cannot be built while duplicates exist, so this operation
must run first. These tests pin the properties that make it safe to run against
production: dry run by default, one record kept per key, a live fork preferred
as canonical, user content never deleted, and a second pass finding nothing.
"""
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

for mod in ["app.models.agent_models", "langfuse", "langfuse.langchain"]:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()

import pytest
from bson import ObjectId

SOURCE_ID = str(ObjectId("70b8d295f1d2c17f4e4b5678"))
LIVE_DECK_OID = ObjectId("70b8d295f1d2c17f4e4b1111")
DEAD_DECK_OID = ObjectId("70b8d295f1d2c17f4e4b2222")
USER_ID = "507f1f77bcf86cd799439011"

KEY_FIELDS = ("original_content_type", "original_content_id", "forked_by_user_id")


def _matches(document: dict, query: dict) -> bool:
    return all(document.get(field) == value for field, value in query.items())


def record(forked_content_id, minutes_ago: int = 0, **overrides) -> dict:
    document = {
        "_id": ObjectId(),
        "original_content_type": "deck",
        "original_content_id": SOURCE_ID,
        "original_creator_id": "507f1f77bcf86cd799439099",
        "forked_by_user_id": USER_ID,
        "forked_content_id": str(forked_content_id) if forked_content_id else None,
        "status": "completed",
        "forked_at": datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
    }
    document.update(overrides)
    return document


class FakeCursor:
    def __init__(self, documents):
        self.documents = documents

    def sort(self, *args, **kwargs):
        return self

    async def to_list(self, length=None):
        return self.documents[:length] if length else list(self.documents)


class FakeForkCollection:
    """content_forks with just the operations the migration uses."""

    def __init__(self, documents):
        self.documents = [dict(d) for d in documents]
        self.deletes = 0

    def find(self, query, *args, **kwargs):
        matched = [dict(d) for d in self.documents if _matches(d, query)]
        matched.sort(key=lambda d: (d["forked_at"], str(d["_id"])))
        return FakeCursor(matched)

    async def delete_one(self, query):
        for index, document in enumerate(self.documents):
            if _matches(document, query):
                self.documents.pop(index)
                self.deletes += 1
                return MagicMock(deleted_count=1)
        return MagicMock(deleted_count=0)

    def aggregate(self, pipeline, **kwargs):
        """Group by the fork key, keep duplicates, then honour skip/limit."""
        groups = {}
        for document in self.documents:
            key = tuple(document.get(field) for field in KEY_FIELDS)
            groups[key] = groups.get(key, 0) + 1

        rows = [
            {"_id": dict(zip(KEY_FIELDS, key)), "count": count}
            for key, count in sorted(groups.items())
            if count > 1
        ]
        skip = next((stage["$skip"] for stage in pipeline if "$skip" in stage), 0)
        limit = next((stage["$limit"] for stage in pipeline if "$limit" in stage), None)
        return FakeCursor(rows[skip: skip + limit if limit else None])


class FakeContentCollection:
    def __init__(self, documents):
        self.documents = [dict(d) for d in documents]
        self.deletes = 0

    async def find_one(self, query, projection=None):
        for document in self.documents:
            if _matches(document, query):
                return dict(document)
        return None

    async def delete_one(self, query):
        self.deletes += 1
        return MagicMock(deleted_count=0)


def live_deck(oid=LIVE_DECK_OID) -> dict:
    return {"_id": oid, "user_id": USER_ID, "deleted_at": None}


def run(forks, decks=None):
    """Run the migration against fakes, returning (stats, forks, decks)."""
    import asyncio

    from app.migrations import reconcile_duplicate_forks as migration

    decks = decks if decks is not None else FakeContentCollection([live_deck()])

    async def _run(apply_changes):
        with patch.object(migration, "content_forks_collection", forks), \
             patch.object(migration, "db", {"decks": decks, "books": decks}):
            return await migration.reconcile_duplicate_forks(apply_changes=apply_changes)

    return _run


# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dry_run_reports_without_writing():
    forks = FakeForkCollection([record(LIVE_DECK_OID, 10), record(DEAD_DECK_OID, 5)])

    stats = await run(forks)(False)

    assert stats.duplicate_keys == 1
    assert stats.records_deleted == 1
    assert forks.deletes == 0
    assert len(forks.documents) == 2


@pytest.mark.asyncio
async def test_apply_leaves_exactly_one_record_per_key():
    forks = FakeForkCollection([record(LIVE_DECK_OID, 10), record(DEAD_DECK_OID, 5)])

    stats = await run(forks)(True)

    assert stats.records_deleted == 1
    assert len(forks.documents) == 1
    assert forks.documents[0]["forked_content_id"] == str(LIVE_DECK_OID)


@pytest.mark.asyncio
async def test_the_canonical_record_is_the_one_whose_content_is_live():
    """A later record pointing at a live deck beats an earlier dead one."""
    forks = FakeForkCollection([record(DEAD_DECK_OID, 60), record(LIVE_DECK_OID, 1)])

    await run(forks)(True)

    assert forks.documents[0]["forked_content_id"] == str(LIVE_DECK_OID)


@pytest.mark.asyncio
async def test_the_earliest_live_record_wins_among_several():
    second_live = ObjectId("70b8d295f1d2c17f4e4b3333")
    decks = FakeContentCollection([live_deck(), live_deck(second_live)])
    forks = FakeForkCollection([record(LIVE_DECK_OID, 30), record(second_live, 2)])

    stats = await run(forks, decks)(True)

    assert forks.documents[0]["forked_content_id"] == str(LIVE_DECK_OID)
    assert stats.keys_with_live_content == 1


@pytest.mark.asyncio
async def test_user_content_is_never_deleted():
    second_live = ObjectId("70b8d295f1d2c17f4e4b3333")
    decks = FakeContentCollection([live_deck(), live_deck(second_live)])
    forks = FakeForkCollection([record(LIVE_DECK_OID, 30), record(second_live, 2)])

    stats = await run(forks, decks)(True)

    assert decks.deletes == 0
    assert stats.orphaned_content == [f"deck:{second_live} (user {USER_ID})"]


@pytest.mark.asyncio
async def test_a_key_with_no_live_content_keeps_its_earliest_record():
    decks = FakeContentCollection([])
    forks = FakeForkCollection([record(DEAD_DECK_OID, 30), record(LIVE_DECK_OID, 1)])

    stats = await run(forks, decks)(True)

    assert stats.keys_without_live_content == 1
    assert len(forks.documents) == 1
    assert forks.documents[0]["forked_content_id"] == str(DEAD_DECK_OID)


@pytest.mark.asyncio
async def test_a_second_pass_finds_nothing():
    forks = FakeForkCollection([record(LIVE_DECK_OID, 10), record(DEAD_DECK_OID, 5)])

    await run(forks)(True)
    stats = await run(forks)(True)

    assert stats.duplicate_keys == 0
    assert stats.records_deleted == 0


@pytest.mark.asyncio
async def test_distinct_keys_are_not_treated_as_duplicates():
    """Same source and user, different content type — a book and a deck fork."""
    forks = FakeForkCollection([
        record(LIVE_DECK_OID, 10),
        record(LIVE_DECK_OID, 10, original_content_type="book"),
        record(LIVE_DECK_OID, 10, forked_by_user_id="507f1f77bcf86cd799439012"),
    ])

    stats = await run(forks)(True)

    assert stats.duplicate_keys == 0
    assert len(forks.documents) == 3


@pytest.mark.asyncio
async def test_records_with_no_content_id_are_reconciled():
    """Abandoned pending claims duplicated on one key still reduce to one."""
    forks = FakeForkCollection([
        record(None, 10, status="pending"),
        record(None, 5, status="failed"),
    ])

    stats = await run(forks, FakeContentCollection([]))(True)

    assert len(forks.documents) == 1
    assert stats.orphaned_content == []


@pytest.mark.asyncio
async def test_every_read_is_bounded():
    """No page in the operation may be fetched with to_list(None)."""
    from app.migrations import reconcile_duplicate_forks as migration

    lengths = []

    class RecordingCursor(FakeCursor):
        async def to_list(self, length=None):
            lengths.append(length)
            return await FakeCursor.to_list(self, length)

    forks = FakeForkCollection([record(LIVE_DECK_OID, 10), record(DEAD_DECK_OID, 5)])
    forks.find = lambda query, *a, **k: RecordingCursor(
        sorted(
            [dict(d) for d in forks.documents if _matches(d, query)],
            key=lambda d: (d["forked_at"], str(d["_id"])),
        )
    )
    original_aggregate = forks.aggregate
    forks.aggregate = lambda pipeline, **k: RecordingCursor(
        original_aggregate(pipeline, **k).documents
    )

    await run(forks)(True)

    assert lengths
    assert all(length is not None and length > 0 for length in lengths)
    assert max(lengths) <= max(migration.BATCH_SIZE, migration.MAX_RECORDS_PER_KEY)
