"""
Phase 34 Plan 03 — account-deletion cascade tests (F7).

`blackboards` was the only content collection absent from `delete_account`'s
cascade: a deleted user's own boards persisted forever, and their stale user_id
stayed in every other board's `collaborators` array. These tests pin both
cascade operations.

Tests call `delete_account` directly rather than going through TestClient/HTTP,
passing the `Depends()` parameter as a plain kwarg — the same pattern used by
`test_annual_planning_be.py` and `test_blackboards.py`.
"""
import sys
from unittest.mock import MagicMock, AsyncMock, patch

# Stub out imports that may not be present in the local dev env, before any
# app import. Mirrors the module-level stub block in test_blackboards.py.
for mod in ["app.models.agent_models", "langfuse", "langfuse.langchain"]:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()

import pytest


def make_collection(matched_count: int = 1, docs=None) -> MagicMock:
    """Mock a Motor collection covering every op `delete_account` performs.

    `count_documents` returns 0 so the public-content guard passes, and `find`
    yields a cursor whose bounded `.to_list()` resolves to `docs` (default empty,
    which short-circuits the annual-plan sub-cascades).
    """
    collection = MagicMock()
    result = MagicMock()
    result.matched_count = matched_count
    collection.update_one = AsyncMock(return_value=result)
    collection.update_many = AsyncMock(return_value=result)
    collection.count_documents = AsyncMock(return_value=0)
    cursor = MagicMock()
    cursor.to_list = AsyncMock(return_value=list(docs or []))
    collection.find = MagicMock(return_value=cursor)
    return collection


@pytest.mark.asyncio
async def test_delete_account_cascades_blackboards(mock_firebase_user):
    """DELETE /users/account soft-deletes owned boards and strips stale collaborators."""
    from app.routers.users import delete_account

    user_id = mock_firebase_user["user_id"]
    mock_blackboards = make_collection()

    # Record cascade-vs-revocation ordering to pin D-08 (all MongoDB soft-deletes
    # commit BEFORE Firebase credentials are revoked).
    order: list = []
    mock_blackboards.update_many.side_effect = lambda *a, **kw: order.append("cascade")

    # firebase_admin is imported inside delete_account's try block; stubbing it in
    # sys.modules keeps the test hermetic (no credential resolution / network).
    mock_fb_auth = MagicMock()
    mock_fb_auth.revoke_refresh_tokens.side_effect = lambda uid: order.append("revoke")

    # priorities/activities/daily_routines are imported INSIDE delete_account, so
    # they resolve from app.config.database at call time and must be patched there.
    # patch.object on the module object is deliberate: other test modules install a
    # bare MagicMock at sys.modules["app.config.database"], which leaves app.config
    # without a `database` attribute and makes patch()'s dotted lookup fail under
    # full-suite ordering.
    db_module = sys.modules["app.config.database"]

    with patch.dict(sys.modules, {"firebase_admin": MagicMock(auth=mock_fb_auth)}), \
         patch.object(db_module, "priorities_collection", make_collection()), \
         patch.object(db_module, "activities_collection", make_collection()), \
         patch.object(db_module, "daily_routines_collection", make_collection()), \
         patch("app.routers.users.users_collection", make_collection()), \
         patch("app.routers.users.decks_collection", make_collection()), \
         patch("app.routers.users.books_collection", make_collection()), \
         patch("app.routers.users.study_cards_collection", make_collection()), \
         patch("app.routers.users.study_sessions_collection", make_collection()), \
         patch("app.routers.users.annual_plans_collection", make_collection()), \
         patch("app.routers.users.focus_areas_collection", make_collection()), \
         patch("app.routers.users.goals_collection", make_collection()), \
         patch("app.routers.users.tasks_collection", make_collection()), \
         patch("app.routers.users.blackboards_collection", mock_blackboards):

        await delete_account(current_user=mock_firebase_user)

    calls = mock_blackboards.update_many.call_args_list
    assert len(calls) == 2, "expected exactly the owned-board and collaborator cascades"

    # 1. Owned boards are soft-deleted, scoped to this user, skipping already-deleted docs.
    owned_filter, owned_update = calls[0].args
    assert owned_filter["owner_user_id"] == user_id
    assert owned_filter["deleted_at"] is None
    assert owned_update["$set"]["deleted_at"] is not None
    assert owned_update["$set"]["deleted_by"] == user_id
    assert owned_update["$set"]["updated_at"] is not None
    # Blackboard has no is_public field — soft_delete_update must not be reused.
    assert "is_public" not in owned_update["$set"]

    # 2. The deleted user is pulled from every OTHER board's collaborators array.
    collab_filter, collab_update = calls[1].args
    assert collab_filter == {"collaborators": user_id}
    assert collab_update["$pull"] == {"collaborators": user_id}
    # A pull must never also soft-delete somebody else's board.
    assert "deleted_at" not in collab_update.get("$set", {})

    # D-08: both cascades commit before Firebase credentials are revoked.
    assert order == ["cascade", "cascade", "revoke"]
