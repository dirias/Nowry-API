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

from tests._stubs import stub_if_missing
from unittest.mock import MagicMock, AsyncMock, patch

# Stub out imports that may not be present in the local dev env, before any
# app import. Mirrors the module-level stub block in test_blackboards.py.
stub_if_missing("langfuse", "langfuse.langchain")
sys.modules.setdefault("app.models.agent_models", MagicMock())

from fastapi import BackgroundTasks
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
    # The push registry is deleted from rather than soft-deleted (MOB-027).
    collection.delete_many = AsyncMock(return_value=result)
    collection.count_documents = AsyncMock(return_value=0)
    cursor = MagicMock()
    cursor.to_list = AsyncMock(return_value=list(docs or []))
    collection.find = MagicMock(return_value=cursor)
    return collection
