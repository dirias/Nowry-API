"""
TTS access control on public books + per-user rate limit.

Covers POST /book/{book_id}/tts after the authorization predicate was widened
from "owner only" to "owner OR is_public", and the 60/hour per-user cap that
bounds Google Cloud TTS spend now that non-owned content is reachable.

The access predicate lives in the Mongo query, not in Python, so a plain
AsyncMock returning a fixed document would assert nothing about it. These tests
use a fake collection that actually evaluates the filter against a small corpus
of books — that is what makes "private book owned by someone else -> 404" a
real assertion rather than a restatement of the mock's return value.
"""
from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock, patch

from bson import ObjectId


# ---------------------------------------------------------------------------
# The google-* and firebase-admin packages are real installed dependencies, so
# unlike test_tracing.py this module must NOT pre-stub `google` — registering a
# bare MagicMock under "google" turns it into a non-package and breaks
# `from google.auth.credentials import ...` inside firebase_admin.
#
# Other test modules do register MagicMock stand-ins for app.routers.tts. Drop
# such a stub (only if it is one) so this file imports the real router.
# ---------------------------------------------------------------------------
if isinstance(sys.modules.get("app.routers.tts"), MagicMock):
    del sys.modules["app.routers.tts"]

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

import app.routers.tts as tts_module  # noqa: E402
from app.models.tts import TTSRequest  # noqa: E402


OWNER_ID = "507f1f77bcf86cd799439011"
STRANGER_ID = "507f1f77bcf86cd799439022"

PUBLIC_BOOK_ID = ObjectId("60b8d295f1d2c17f4e4b1111")
PRIVATE_BOOK_ID = ObjectId("60b8d295f1d2c17f4e4b2222")
DELETED_PUBLIC_BOOK_ID = ObjectId("60b8d295f1d2c17f4e4b3333")
MISSING_BOOK_ID = ObjectId("60b8d295f1d2c17f4e4b9999")


# ---------------------------------------------------------------------------
# Fake books collection that evaluates the query filter
# ---------------------------------------------------------------------------
class FakeBooksCollection:
    """Minimal Motor stand-in supporting only the operators tts.py uses.

    Handles top-level field equality, `deleted_at: None`, `$or` over
    single-key equality clauses, and include-style projections.
    """

    def __init__(self, docs: list) -> None:
        self.docs = docs
        self.last_filter: dict = {}
        self.last_projection = None

    @staticmethod
    def _matches_clause(doc: dict, field: str, expected) -> bool:
        return doc.get(field) == expected

    def _matches(self, doc: dict, query: dict) -> bool:
        for field, expected in query.items():
            if field == "$or":
                if not any(
                    all(self._matches_clause(doc, f, v) for f, v in clause.items())
                    for clause in expected
                ):
                    return False
            elif not self._matches_clause(doc, field, expected):
                return False
        return True

    @staticmethod
    def _project(doc: dict, projection) -> dict:
        if not projection:
            return dict(doc)
        included = {f for f, flag in projection.items() if flag}
        result = {f: v for f, v in doc.items() if f in included}
        if projection.get("_id", 1):
            result["_id"] = doc["_id"]
        return result

    async def find_one(self, query: dict, projection=None):
        self.last_filter = query
        self.last_projection = projection
        for doc in self.docs:
            if self._matches(doc, query):
                return self._project(doc, projection)
        return None


@pytest.fixture
def fake_books() -> FakeBooksCollection:
    return FakeBooksCollection(
        [
            {
                "_id": PUBLIC_BOOK_ID,
                "user_id": OWNER_ID,
                "title": "A Public Book",
                "full_content": "x" * 5000,
                "is_public": True,
                "deleted_at": None,
            },
            {
                "_id": PRIVATE_BOOK_ID,
                "user_id": OWNER_ID,
                "title": "A Private Book",
                "full_content": "y" * 5000,
                "is_public": False,
                "deleted_at": None,
            },
            {
                "_id": DELETED_PUBLIC_BOOK_ID,
                "user_id": OWNER_ID,
                "title": "A Deleted Public Book",
                "is_public": True,
                "deleted_at": "2026-01-01T00:00:00Z",
            },
        ]
    )


@pytest.fixture
def fake_tts_client() -> MagicMock:
    client = MagicMock()
    client.synthesize_speech.return_value = MagicMock(audio_content=b"fake-mp3-bytes")
    return client


def _patched_router(fake_books: FakeBooksCollection, fake_tts_client: MagicMock,
                    rate_limit=None):
    """Patch tts.py's module-level collaborators.

    Langfuse is disabled (get_langfuse_client -> None) so these tests exercise
    the access path only; tracing has its own coverage in test_tracing.py.
    """
    rate_limit = rate_limit or AsyncMock(return_value=1)
    return (
        patch.object(tts_module, "books_collection", fake_books),
        patch.object(tts_module, "get_tts_client", return_value=fake_tts_client),
        patch.object(tts_module, "get_langfuse_client", return_value=None),
        patch.object(tts_module, "enforce_user_rate_limit", new=rate_limit),
    )


async def _call(book_id, user_id: str, tier: str = "plus"):
    return await tts_module.generate_tts(
        book_id=str(book_id),
        body=TTSRequest(text="Hello world", language_code="en-US"),
        tier=tier,
        current_user={"user_id": user_id},
    )


# ---------------------------------------------------------------------------
# Acceptance 1 — public book, non-owner -> 200
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_public_book_non_owner_gets_audio(fake_books, fake_tts_client):
    p1, p2, p3, p4 = _patched_router(fake_books, fake_tts_client)
    with p1, p2, p3, p4:
        response = await _call(PUBLIC_BOOK_ID, STRANGER_ID)

    assert response.status_code == 200
    assert response.media_type == "audio/mpeg"
    assert response.body == b"fake-mp3-bytes"
    assert fake_tts_client.synthesize_speech.call_count == 1


@pytest.mark.asyncio
async def test_public_book_query_keeps_soft_delete_filter(fake_books, fake_tts_client):
    """The widened predicate must not relax deleted_at, and must widen via $or."""
    p1, p2, p3, p4 = _patched_router(fake_books, fake_tts_client)
    with p1, p2, p3, p4:
        await _call(PUBLIC_BOOK_ID, STRANGER_ID)

    assert fake_books.last_filter["deleted_at"] is None
    assert {"user_id": STRANGER_ID} in fake_books.last_filter["$or"]
    assert {"is_public": True} in fake_books.last_filter["$or"]


@pytest.mark.asyncio
async def test_public_book_read_is_projected(fake_books, fake_tts_client):
    """full_content is the whole book body and is never needed here."""
    p1, p2, p3, p4 = _patched_router(fake_books, fake_tts_client)
    with p1, p2, p3, p4:
        await _call(PUBLIC_BOOK_ID, STRANGER_ID)

    assert fake_books.last_projection == {"user_id": 1, "is_public": 1}


# ---------------------------------------------------------------------------
# Acceptance 2 — private book, non-owner -> 404 (NOT 403)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_private_book_non_owner_gets_404_not_403(fake_books, fake_tts_client):
    p1, p2, p3, p4 = _patched_router(fake_books, fake_tts_client)
    with p1, p2, p3, p4:
        with pytest.raises(HTTPException) as exc_info:
            await _call(PRIVATE_BOOK_ID, STRANGER_ID)

    # 403 would confirm the book exists to a stranger. This must stay 404.
    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Book not found."
    assert fake_tts_client.synthesize_speech.call_count == 0


@pytest.mark.asyncio
async def test_private_book_404_is_indistinguishable_from_missing(
    fake_books, fake_tts_client
):
    p1, p2, p3, p4 = _patched_router(fake_books, fake_tts_client)
    with p1, p2, p3, p4:
        with pytest.raises(HTTPException) as private_exc:
            await _call(PRIVATE_BOOK_ID, STRANGER_ID)
        with pytest.raises(HTTPException) as missing_exc:
            await _call(MISSING_BOOK_ID, STRANGER_ID)

    assert private_exc.value.status_code == missing_exc.value.status_code
    assert private_exc.value.detail == missing_exc.value.detail


# ---------------------------------------------------------------------------
# Acceptance 4 — owner path unchanged
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_owner_private_book_still_works(fake_books, fake_tts_client):
    p1, p2, p3, p4 = _patched_router(fake_books, fake_tts_client)
    with p1, p2, p3, p4:
        response = await _call(PRIVATE_BOOK_ID, OWNER_ID)

    assert response.status_code == 200
    assert response.body == b"fake-mp3-bytes"


@pytest.mark.asyncio
async def test_owner_public_book_still_works(fake_books, fake_tts_client):
    p1, p2, p3, p4 = _patched_router(fake_books, fake_tts_client)
    with p1, p2, p3, p4:
        response = await _call(PUBLIC_BOOK_ID, OWNER_ID)

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_pro_owner_keeps_language_override(fake_books, fake_tts_client):
    """Pro language selection and the Plus en-US clamp are untouched.

    Asserts against a patched `texttospeech` module rather than reading
    `.language_code` off the constructed voice: test_advanced_ai.py registers a
    MagicMock for google.cloud.texttospeech before app.routers.tts is first
    imported, so whether the real proto class is in play depends on test order.
    """
    mock_texttospeech = MagicMock()
    p1, p2, p3, p4 = _patched_router(fake_books, fake_tts_client)
    with p1, p2, p3, p4, patch.object(tts_module, "texttospeech", mock_texttospeech):
        await tts_module.generate_tts(
            book_id=str(PRIVATE_BOOK_ID),
            body=TTSRequest(text="Bonjour", language_code="fr-FR"),
            tier="pro",
            current_user={"user_id": OWNER_ID},
        )
        pro_kwargs = mock_texttospeech.VoiceSelectionParams.call_args.kwargs

        await tts_module.generate_tts(
            book_id=str(PRIVATE_BOOK_ID),
            body=TTSRequest(text="Bonjour", language_code="fr-FR"),
            tier="plus",
            current_user={"user_id": OWNER_ID},
        )
        plus_kwargs = mock_texttospeech.VoiceSelectionParams.call_args.kwargs

    assert pro_kwargs["language_code"] == "fr-FR"
    assert plus_kwargs["language_code"] == "en-US"


# ---------------------------------------------------------------------------
# Acceptance 3 — free tier still 403
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_free_tier_still_403_on_public_book(fake_books, fake_tts_client):
    """Widening ownership must not widen the tier gate."""
    rate_limit = AsyncMock(return_value=1)
    p1, p2, p3, p4 = _patched_router(fake_books, fake_tts_client, rate_limit)
    with p1, p2, p3, p4:
        with pytest.raises(HTTPException) as exc_info:
            await _call(PUBLIC_BOOK_ID, STRANGER_ID, tier="free")

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "TTS requires a Plus or Pro subscription."
    assert fake_tts_client.synthesize_speech.call_count == 0
    rate_limit.assert_not_awaited()  # a 403 must not burn quota


# ---------------------------------------------------------------------------
# Acceptance 6 — soft-deleted public book -> 404
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_soft_deleted_public_book_is_404(fake_books, fake_tts_client):
    p1, p2, p3, p4 = _patched_router(fake_books, fake_tts_client)
    with p1, p2, p3, p4:
        with pytest.raises(HTTPException) as exc_info:
            await _call(DELETED_PUBLIC_BOOK_ID, STRANGER_ID)

    assert exc_info.value.status_code == 404
    assert fake_tts_client.synthesize_speech.call_count == 0


@pytest.mark.asyncio
async def test_soft_deleted_public_book_is_404_for_owner_too(
    fake_books, fake_tts_client
):
    p1, p2, p3, p4 = _patched_router(fake_books, fake_tts_client)
    with p1, p2, p3, p4:
        with pytest.raises(HTTPException) as exc_info:
            await _call(DELETED_PUBLIC_BOOK_ID, OWNER_ID)

    assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# Invalid ObjectId -> 400 (unchanged)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_invalid_book_id_is_400(fake_books, fake_tts_client):
    p1, p2, p3, p4 = _patched_router(fake_books, fake_tts_client)
    with p1, p2, p3, p4:
        with pytest.raises(HTTPException) as exc_info:
            await _call("not-an-object-id", STRANGER_ID)

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Invalid book ID."


# ---------------------------------------------------------------------------
# Change 3 — observability
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_trace_metadata_attributes_non_owner_usage(fake_books, fake_tts_client):
    langfuse_client = MagicMock()
    p1, p2, _, p4 = _patched_router(fake_books, fake_tts_client)
    with p1, p2, p4, \
         patch.object(tts_module, "get_langfuse_client", return_value=langfuse_client), \
         patch.object(tts_module, "propagate_attributes") as mock_propagate:
        await _call(PUBLIC_BOOK_ID, STRANGER_ID)

    metadata = mock_propagate.call_args.kwargs["metadata"]
    assert metadata["is_public"] is True
    assert metadata["owner_id"] == OWNER_ID
    assert metadata["is_owner"] is False
    assert metadata["user_id"] == STRANGER_ID


@pytest.mark.asyncio
async def test_trace_metadata_marks_owner_usage(fake_books, fake_tts_client):
    langfuse_client = MagicMock()
    p1, p2, _, p4 = _patched_router(fake_books, fake_tts_client)
    with p1, p2, p4, \
         patch.object(tts_module, "get_langfuse_client", return_value=langfuse_client), \
         patch.object(tts_module, "propagate_attributes") as mock_propagate:
        await _call(PRIVATE_BOOK_ID, OWNER_ID)

    metadata = mock_propagate.call_args.kwargs["metadata"]
    assert metadata["is_public"] is False
    assert metadata["is_owner"] is True


# ---------------------------------------------------------------------------
# Acceptance 5 — rate limit
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_router_surfaces_429(fake_books, fake_tts_client):
    """A 429 from the limiter must abort before the billed provider call."""
    rate_limit = AsyncMock(
        side_effect=HTTPException(
            status_code=429, detail="Too many audio requests. Please wait a moment."
        )
    )
    p1, p2, p3, p4 = _patched_router(fake_books, fake_tts_client, rate_limit)
    with p1, p2, p3, p4:
        with pytest.raises(HTTPException) as exc_info:
            await _call(PUBLIC_BOOK_ID, STRANGER_ID)

    assert exc_info.value.status_code == 429
    assert exc_info.value.detail == "Too many audio requests. Please wait a moment."
    assert fake_tts_client.synthesize_speech.call_count == 0


@pytest.mark.asyncio
async def test_route_is_configured_for_60_per_hour():
    assert tts_module._TTS_RATE_LIMIT_MAX_REQUESTS == 60
    assert tts_module._TTS_RATE_LIMIT_WINDOW_SECONDS == 3600
    assert (
        tts_module._TTS_RATE_LIMIT_DETAIL
        == "Too many audio requests. Please wait a moment."
    )


class FakeRateLimitCollection:
    """In-memory stand-in implementing the $inc/$setOnInsert upsert semantics."""

    def __init__(self) -> None:
        self.store: dict = {}

    async def find_one_and_update(
        self, query: dict, update: dict, upsert: bool = False, return_document=None
    ) -> dict:
        key = query["_id"]
        if key not in self.store:
            if not upsert:
                return None
            self.store[key] = {"_id": key, "count": 0}
            self.store[key].update(update.get("$setOnInsert", {}))
        for field, delta in update.get("$inc", {}).items():
            self.store[key][field] = self.store[key].get(field, 0) + delta
        return dict(self.store[key])


@pytest.mark.asyncio
async def test_limiter_allows_60_then_rejects_61st():
    from app.utils import rate_limit as rate_limit_module

    fake = FakeRateLimitCollection()
    with patch.object(rate_limit_module, "rate_limits_collection", fake):
        for expected_count in range(1, 61):
            count = await rate_limit_module.enforce_user_rate_limit(
                user_id=STRANGER_ID,
                feature="tts_amagic",
                limit=60,
                window_seconds=3600,
                detail="Too many audio requests. Please wait a moment.",
            )
            assert count == expected_count

        with pytest.raises(HTTPException) as exc_info:
            await rate_limit_module.enforce_user_rate_limit(
                user_id=STRANGER_ID,
                feature="tts_amagic",
                limit=60,
                window_seconds=3600,
                detail="Too many audio requests. Please wait a moment.",
            )

    assert exc_info.value.status_code == 429
    assert exc_info.value.detail == "Too many audio requests. Please wait a moment."
    assert "Retry-After" in exc_info.value.headers


@pytest.mark.asyncio
async def test_limiter_is_per_user_not_global():
    """One user exhausting the quota must not lock out everyone else."""
    from app.utils import rate_limit as rate_limit_module

    fake = FakeRateLimitCollection()
    with patch.object(rate_limit_module, "rate_limits_collection", fake):
        for _ in range(60):
            await rate_limit_module.enforce_user_rate_limit(
                user_id=STRANGER_ID, feature="tts_amagic", limit=60,
                window_seconds=3600, detail="nope",
            )
        # A different uid gets its own bucket.
        count = await rate_limit_module.enforce_user_rate_limit(
            user_id=OWNER_ID, feature="tts_amagic", limit=60,
            window_seconds=3600, detail="nope",
        )

    assert count == 1


@pytest.mark.asyncio
async def test_limiter_buckets_are_namespaced_by_feature():
    from app.utils import rate_limit as rate_limit_module

    fake = FakeRateLimitCollection()
    with patch.object(rate_limit_module, "rate_limits_collection", fake):
        await rate_limit_module.enforce_user_rate_limit(
            user_id=STRANGER_ID, feature="tts_amagic", limit=60,
            window_seconds=3600, detail="nope",
        )
        count = await rate_limit_module.enforce_user_rate_limit(
            user_id=STRANGER_ID, feature="other_feature", limit=60,
            window_seconds=3600, detail="nope",
        )

    assert count == 1


@pytest.mark.asyncio
async def test_limiter_sets_ttl_expiry_on_new_bucket():
    from datetime import datetime

    from app.utils import rate_limit as rate_limit_module

    fake = FakeRateLimitCollection()
    with patch.object(rate_limit_module, "rate_limits_collection", fake):
        await rate_limit_module.enforce_user_rate_limit(
            user_id=STRANGER_ID, feature="tts_amagic", limit=60,
            window_seconds=3600, detail="nope",
        )

    bucket = next(iter(fake.store.values()))
    assert isinstance(bucket["expires_at"], datetime)
    # Must outlive the window it meters, or the TTL monitor could drop a live bucket.
    assert bucket["expires_at"].timestamp() >= bucket["window_start"] + 3600


@pytest.mark.asyncio
async def test_limiter_fails_closed_when_counter_unavailable():
    """A broken meter must not silently grant unmetered access to a billed API."""
    from app.utils import rate_limit as rate_limit_module

    broken = MagicMock()
    broken.find_one_and_update = AsyncMock(side_effect=RuntimeError("mongo down"))
    with patch.object(rate_limit_module, "rate_limits_collection", broken):
        with pytest.raises(HTTPException) as exc_info:
            await rate_limit_module.enforce_user_rate_limit(
                user_id=STRANGER_ID, feature="tts_amagic", limit=60,
                window_seconds=3600, detail="nope",
            )

    assert exc_info.value.status_code == 500
