"""
ONB-001 — resumable onboarding journey state.

Pins the `GET/PATCH /users/onboarding` contract from `docs/architecture-onboarding.md`:
legacy normalization without a migration, server-written UTC timestamps, the
monotonic meaningful-point rules, activation immutability, and the 24-hour
`show_reentry` boundary.

Handlers are called directly with the `Depends()` parameter passed as a plain
kwarg — the same pattern as `test_users.py` and `test_blackboards.py`. The 401
case goes through TestClient because the token check lives in the router-level
dependency, above the handler.
"""
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

# Stub imports that may be missing from the local dev env, before any app
# import. Mirrors the module-level stub block in test_users.py.
for mod in ["app.models.agent_models", "langfuse", "langfuse.langchain"]:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()

import pytest
from bson import ObjectId
from fastapi import HTTPException

USER_OID = ObjectId("507f1f77bcf86cd799439011")

# Every key the contract allows in the response — used to prove no preference
# field (language, theme_color, interests, primary_topic, study_goal) leaks in.
EXPECTED_RESPONSE_KEYS = {
    "status",
    "last_meaningful_point",
    "postponed_at",
    "activated_at",
    "updated_at",
    "show_reentry",
    "resume_screen",
}

PREFERENCE_KEYS = {
    "language",
    "theme_color",
    "interests",
    "primary_topic",
    "study_goal",
}


def user_doc(onboarding=None, wizard_completed=False, **extra) -> dict:
    """A user document carrying preferences the journey must never touch."""
    doc = {
        "_id": USER_OID,
        "firebase_uid": "test-firebase-uid-123",
        "email": "test@example.com",
        "wizard_completed": wizard_completed,
        "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 6, 1, tzinfo=timezone.utc),
        "preferences": {
            "general": {
                "language": "ja",
                "theme_color": "#2a6971",
                "interests": ["science", "mathematics"],
                "primary_topic": "science",
                "study_goal": "academic",
            }
        },
    }
    if onboarding is not None:
        doc["onboarding"] = onboarding
    doc.update(extra)
    return doc


def mock_users_collection(found=None, updated=None) -> MagicMock:
    collection = MagicMock()
    collection.find_one = AsyncMock(return_value=found)
    collection.find_one_and_update = AsyncMock(return_value=updated)
    return collection


async def call_get(collection, current_user):
    from app.routers.users import get_onboarding_state

    with patch("app.routers.users.users_collection", collection):
        return await get_onboarding_state(current_user=current_user)


async def call_patch(collection, current_user, body: dict):
    from app.routers.users import OnboardingStateUpdate, update_onboarding_state

    with patch("app.routers.users.users_collection", collection):
        return await update_onboarding_state(
            data=OnboardingStateUpdate(**body),
            current_user=current_user,
        )


def applied_set(collection) -> dict:
    """The `$set` document handed to MongoDB by the last PATCH."""
    _, update = collection.find_one_and_update.call_args[0]
    return update["$set"]


# ---------------------------------------------------------------------------
# GET — legacy normalization
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_legacy_user_without_subdocument_resolves_to_welcome(
    mock_firebase_user,
):
    """No `onboarding` subdocument and no wizard flag => incomplete at Welcome."""
    collection = mock_users_collection(found=user_doc())

    response = await call_get(collection, mock_firebase_user)

    assert response.status == "incomplete"
    assert response.last_meaningful_point == "welcome"
    assert response.resume_screen == "welcome"
    assert response.postponed_at is None
    assert response.activated_at is None
    # Never postponed => the re-entry point is immediately available.
    assert response.show_reentry is True
    # Read-only: normalization must not write, so no migration is required.
    collection.find_one_and_update.assert_not_called()


@pytest.mark.asyncio
async def test_get_legacy_wizard_completed_user_resolves_as_activated(
    mock_firebase_user,
):
    """`wizard_completed=True` maps to activated for routing compatibility."""
    collection = mock_users_collection(found=user_doc(wizard_completed=True))

    response = await call_get(collection, mock_firebase_user)

    assert response.status == "activated"
    assert response.resume_screen is None
    assert response.show_reentry is False


@pytest.mark.asyncio
async def test_get_returns_stored_subdocument(mock_firebase_user):
    postponed = datetime(2026, 8, 12, 1, 0, tzinfo=timezone.utc)
    updated = datetime(2026, 8, 13, 2, 0, tzinfo=timezone.utc)
    collection = mock_users_collection(
        found=user_doc(
            {
                "status": "incomplete",
                "last_meaningful_point": "personalization",
                "postponed_at": postponed,
                "activated_at": None,
                "updated_at": updated,
            }
        )
    )

    response = await call_get(collection, mock_firebase_user)

    assert response.status == "incomplete"
    assert response.last_meaningful_point == "personalization"
    assert response.resume_screen == "personalization"
    assert response.postponed_at == postponed
    assert response.updated_at == updated


@pytest.mark.asyncio
async def test_get_normalizes_corrupt_stored_point(mock_firebase_user):
    """An unrecognised stored point falls back to Welcome instead of erroring."""
    collection = mock_users_collection(
        found=user_doc({"status": "incomplete", "last_meaningful_point": "nonsense"})
    )

    response = await call_get(collection, mock_firebase_user)

    assert response.last_meaningful_point == "welcome"


@pytest.mark.asyncio
async def test_get_missing_user_returns_404(mock_firebase_user):
    collection = mock_users_collection(found=None)

    with pytest.raises(HTTPException) as exc:
        await call_get(collection, mock_firebase_user)

    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_get_carries_no_preference_fields(mock_firebase_user):
    """AC4: the journey response never duplicates general preferences."""
    collection = mock_users_collection(found=user_doc())

    payload = (await call_get(collection, mock_firebase_user)).model_dump()

    assert set(payload) == EXPECTED_RESPONSE_KEYS
    assert not PREFERENCE_KEYS & set(payload)


# ---------------------------------------------------------------------------
# show_reentry — the 24-hour boundary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "elapsed_hours, expected",
    [(0, False), (1, False), (23.99, False), (24, True), (25, True)],
)
def test_show_reentry_respects_grace_period(elapsed_hours, expected):
    from app.models.User import normalize_onboarding_state, onboarding_show_reentry

    now = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)
    state = normalize_onboarding_state(
        user_doc(
            {
                "status": "incomplete",
                "last_meaningful_point": "welcome",
                "postponed_at": now - timedelta(hours=elapsed_hours),
            }
        )
    )

    assert onboarding_show_reentry(state, now) is expected


def test_show_reentry_is_false_for_activated_user_regardless_of_postponement():
    from app.models.User import normalize_onboarding_state, onboarding_show_reentry

    now = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)
    state = normalize_onboarding_state(
        user_doc(
            {
                "status": "activated",
                "last_meaningful_point": "first_deck",
                "postponed_at": now - timedelta(days=30),
                "activated_at": now - timedelta(days=29),
            }
        )
    )

    assert onboarding_show_reentry(state, now) is False


def test_show_reentry_treats_naive_stored_timestamps_as_utc():
    """Motor returns naive datetimes; comparing them against server time must
    not raise and must not shift the boundary."""
    from app.models.User import normalize_onboarding_state, onboarding_show_reentry

    now = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)
    state = normalize_onboarding_state(
        user_doc({"status": "incomplete", "postponed_at": datetime(2026, 8, 12, 11, 0)})
    )

    assert state.postponed_at.tzinfo is not None
    assert onboarding_show_reentry(state, now) is True


# ---------------------------------------------------------------------------
# PATCH — record_point
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_patch_record_point_writes_server_timestamp(mock_firebase_user):
    before = datetime.now(timezone.utc)
    updated = user_doc(
        {"status": "incomplete", "last_meaningful_point": "personalization"}
    )
    collection = mock_users_collection(found=user_doc(), updated=updated)

    response = await call_patch(
        collection,
        mock_firebase_user,
        {"action": "record_point", "point": "personalization"},
    )

    set_doc = applied_set(collection)
    assert set_doc["onboarding.status"] == "incomplete"
    assert set_doc["onboarding.last_meaningful_point"] == "personalization"
    assert "onboarding.postponed_at" not in set_doc
    written = set_doc["onboarding.updated_at"]
    assert written.tzinfo == timezone.utc
    assert before <= written <= datetime.now(timezone.utc)
    assert response.last_meaningful_point == "personalization"
    assert response.status == "incomplete"


@pytest.mark.asyncio
async def test_patch_record_point_never_regresses_progress(mock_firebase_user):
    """Recording an earlier screen keeps the furthest point reached."""
    stored = user_doc({"status": "incomplete", "last_meaningful_point": "first_deck"})
    collection = mock_users_collection(found=stored, updated=stored)

    await call_patch(
        collection,
        mock_firebase_user,
        {"action": "record_point", "point": "personalization"},
    )

    assert applied_set(collection)["onboarding.last_meaningful_point"] == "first_deck"


@pytest.mark.asyncio
async def test_patch_record_point_creates_subdocument_for_legacy_user(
    mock_firebase_user,
):
    """A legacy user gains a complete subdocument on first write, no migration."""
    updated = user_doc({"status": "incomplete", "last_meaningful_point": "first_deck"})
    collection = mock_users_collection(found=user_doc(), updated=updated)

    await call_patch(
        collection,
        mock_firebase_user,
        {"action": "record_point", "point": "first_deck"},
    )

    set_doc = applied_set(collection)
    assert set(set_doc) == {
        "onboarding.status",
        "onboarding.last_meaningful_point",
        "onboarding.updated_at",
    }


# ---------------------------------------------------------------------------
# PATCH — postpone
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_patch_postpone_from_welcome_records_time_and_point(
    mock_firebase_user,
):
    before = datetime.now(timezone.utc)
    updated = user_doc(
        {
            "status": "incomplete",
            "last_meaningful_point": "welcome",
            "postponed_at": before,
        }
    )
    collection = mock_users_collection(found=user_doc(), updated=updated)

    response = await call_patch(collection, mock_firebase_user, {"action": "postpone"})

    set_doc = applied_set(collection)
    assert set_doc["onboarding.last_meaningful_point"] == "welcome"
    assert set_doc["onboarding.status"] == "incomplete"
    assert before <= set_doc["onboarding.postponed_at"] <= datetime.now(timezone.utc)
    assert set_doc["onboarding.postponed_at"].tzinfo == timezone.utc
    # Fresh postponement => the grace period is running.
    assert response.show_reentry is False


@pytest.mark.parametrize("stored_point", ["personalization", "first_deck"])
@pytest.mark.asyncio
async def test_patch_postpone_preserves_a_later_meaningful_point(
    mock_firebase_user, stored_point
):
    """A user who postpones after resuming further keeps the later resume point."""
    stored = user_doc({"status": "incomplete", "last_meaningful_point": stored_point})
    collection = mock_users_collection(found=stored, updated=stored)

    await call_patch(collection, mock_firebase_user, {"action": "postpone"})

    set_doc = applied_set(collection)
    assert set_doc["onboarding.last_meaningful_point"] == stored_point
    assert "onboarding.postponed_at" in set_doc


# ---------------------------------------------------------------------------
# PATCH — rejected mutations
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {"action": "record_point", "point": "welcome"},
        {"action": "record_point"},
        {"action": "record_point", "point": "nonsense"},
        {"action": "postpone", "point": "personalization"},
        {"action": "activate"},
        {"action": ""},
    ],
)
@pytest.mark.asyncio
async def test_patch_rejects_invalid_actions_with_400(mock_firebase_user, body):
    collection = mock_users_collection(found=user_doc(), updated=user_doc())

    with pytest.raises(HTTPException) as exc:
        await call_patch(collection, mock_firebase_user, body)

    assert exc.value.status_code == 400
    assert exc.value.detail == "invalid_action"
    collection.find_one_and_update.assert_not_called()


@pytest.mark.parametrize(
    "body",
    [{"action": "postpone"}, {"action": "record_point", "point": "personalization"}],
)
@pytest.mark.asyncio
async def test_patch_cannot_mutate_an_activated_journey(mock_firebase_user, body):
    """Activation is terminal: no action may move the journey back to incomplete."""
    activated = user_doc(
        {
            "status": "activated",
            "last_meaningful_point": "first_deck",
            "activated_at": datetime(2026, 8, 13, 3, 0, tzinfo=timezone.utc),
        },
        wizard_completed=True,
    )
    collection = mock_users_collection(found=activated, updated=activated)

    with pytest.raises(HTTPException) as exc:
        await call_patch(collection, mock_firebase_user, body)

    assert exc.value.status_code == 409
    assert exc.value.detail == "onboarding_already_activated"
    collection.find_one_and_update.assert_not_called()


@pytest.mark.asyncio
async def test_patch_missing_user_returns_404(mock_firebase_user):
    collection = mock_users_collection(found=None)

    with pytest.raises(HTTPException) as exc:
        await call_patch(collection, mock_firebase_user, {"action": "postpone"})

    assert exc.value.status_code == 404
    collection.find_one_and_update.assert_not_called()


@pytest.mark.asyncio
async def test_patch_writes_no_preference_fields(mock_firebase_user):
    """AC4: the journey write path touches only `onboarding.*`."""
    updated = user_doc({"status": "incomplete", "last_meaningful_point": "first_deck"})
    collection = mock_users_collection(found=user_doc(), updated=updated)

    response = await call_patch(
        collection,
        mock_firebase_user,
        {"action": "record_point", "point": "first_deck"},
    )

    assert all(key.startswith("onboarding.") for key in applied_set(collection))
    assert set(response.model_dump()) == EXPECTED_RESPONSE_KEYS


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


def test_onboarding_routes_require_a_firebase_token():
    """Both routes return the documented 401 when no token is presented."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.routers.users import router

    test_app = FastAPI()
    test_app.include_router(router)
    client = TestClient(test_app, raise_server_exceptions=False)

    assert client.get("/users/onboarding").status_code == 401
    assert client.patch(
        "/users/onboarding", json={"action": "postpone"}
    ).status_code == 401
