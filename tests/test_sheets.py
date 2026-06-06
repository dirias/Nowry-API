"""
Phase 8 — Micro Sheets tests. Stubs written in Wave 0; implementations follow in Wave 1.
All tests are skipped until routers/sheets.py exists.
"""
from __future__ import annotations

import sys
from unittest.mock import MagicMock, AsyncMock

import pytest


# ─────────────────────────────────────────────────────────────────────────────
# Module-level sys.modules stubs — installed before any app import
# (mirrors the approach used in test_ai_magic.py and test_stripe_webhooks.py)
# ─────────────────────────────────────────────────────────────────────────────

if "groq" not in sys.modules:
    sys.modules["groq"] = MagicMock()
if "google" not in sys.modules:
    sys.modules["google"] = MagicMock()
if "google.generativeai" not in sys.modules:
    sys.modules["google.generativeai"] = MagicMock()

mock_firebase = MagicMock()
mock_firebase.get_firebase_user = AsyncMock(return_value={"user_id": "507f1f77bcf86cd799439011"})
sys.modules.setdefault("app.auth.firebase_auth", mock_firebase)

mock_deps = MagicMock()
mock_deps.get_subscription_tier = AsyncMock(return_value="free")
mock_deps.track_ai_usage = AsyncMock(return_value={"user_id": "507f1f77bcf86cd799439011"})
sys.modules.setdefault("app.auth.dependencies", mock_deps)

_mock_db = MagicMock()
_mock_sheets_col = MagicMock()
_mock_sheets_col.insert_one = AsyncMock()
_mock_sheets_col.find_one = AsyncMock()
_mock_sheets_col.update_one = AsyncMock()
_mock_db.sheets_collection = _mock_sheets_col
sys.modules.setdefault("app.config.database", _mock_db)

if "app.ai_orchestrator.llm_clients.gemini_client" not in sys.modules:
    sys.modules["app.ai_orchestrator.llm_clients.gemini_client"] = MagicMock()


# ─────────────────────────────────────────────────────────────────────────────
# SHEET-01: POST /sheets creates sheet, GET /sheets returns it
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.skip(reason="Implementing in 08-02-PLAN")
@pytest.mark.asyncio
async def test_create_and_list_sheets(mock_firebase_user):
    """SHEET-01: POST /sheets creates a sheet; GET /sheets returns it in the list."""
    from unittest.mock import patch
    from app.routers.sheets import create_sheet, list_sheets

    sheet_doc = {
        "_id": "617f1f77bcf86cd799439abc",
        "user_id": "507f1f77bcf86cd799439011",
        "title": "Test Sheet",
        "data": [],
        "column_labels": [],
        "deleted_at": None,
    }

    with patch("app.routers.sheets.sheets_collection") as mock_col:
        mock_col.insert_one = AsyncMock(return_value=MagicMock(inserted_id="617f1f77bcf86cd799439abc"))
        mock_col.find_one = AsyncMock(return_value=sheet_doc)
        mock_cursor = MagicMock()
        mock_cursor.sort = MagicMock(return_value=mock_cursor)
        mock_cursor.to_list = AsyncMock(return_value=[sheet_doc])
        mock_col.find = MagicMock(return_value=mock_cursor)

        sheets = await list_sheets(current_user=mock_firebase_user)

    assert len(sheets) > 0


# ─────────────────────────────────────────────────────────────────────────────
# T-IDOR: GET /sheets/{id} with wrong user_id returns 404
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.skip(reason="Implementing in 08-02-PLAN")
@pytest.mark.asyncio
async def test_sheet_idor_isolation(mock_firebase_user):
    """T-IDOR: GET /sheets/{id} with a different user's sheet returns 404 (not the sheet)."""
    from fastapi import HTTPException
    from unittest.mock import patch
    from app.routers.sheets import get_sheet

    # find_one returns None → simulates IDOR filter (user_id mismatch → no doc)
    with patch("app.routers.sheets.sheets_collection") as mock_col:
        mock_col.find_one = AsyncMock(return_value=None)

        with pytest.raises(HTTPException) as exc_info:
            await get_sheet(
                sheet_id="617f1f77bcf86cd799439abc",
                current_user=mock_firebase_user,
            )

    assert exc_info.value.status_code == 404


# ─────────────────────────────────────────────────────────────────────────────
# T-ID: user_id on created sheet must equal token user_id (never from body)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.skip(reason="Implementing in 08-02-PLAN")
@pytest.mark.asyncio
async def test_create_sheet_user_id_from_token(mock_firebase_user):
    """T-ID: user_id on the created sheet document equals the token user_id, never from the request body."""
    from unittest.mock import patch, call
    from app.routers.sheets import create_sheet
    from app.models.Sheet import Sheet

    attacker_user_id = "attacker-user-id-000"
    token_user_id = mock_firebase_user.get("user_id")

    # Provide a sheet body that contains an attacker user_id — router must override it
    sheet_body = Sheet(title="Test Sheet")

    inserted_id = "617f1f77bcf86cd799439abc"

    with patch("app.routers.sheets.sheets_collection") as mock_col:
        mock_col.insert_one = AsyncMock(return_value=MagicMock(inserted_id=inserted_id))
        mock_col.find_one = AsyncMock(return_value={
            "_id": inserted_id,
            "user_id": token_user_id,
            "title": "Test Sheet",
            "data": [],
            "column_labels": [],
            "deleted_at": None,
        })

        result = await create_sheet(
            sheet=sheet_body,
            current_user=mock_firebase_user,
        )

        # Verify the inserted doc carries token user_id, not any body-provided value
        insert_call_arg = mock_col.insert_one.call_args[0][0]
        assert insert_call_arg.get("user_id") == token_user_id
        assert insert_call_arg.get("user_id") != attacker_user_id


# ─────────────────────────────────────────────────────────────────────────────
# T-DoS: list_sheets must use .to_list(length=100) — never unbounded
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.skip(reason="Implementing in 08-02-PLAN")
@pytest.mark.asyncio
async def test_list_sheets_bounded(mock_firebase_user):
    """T-DoS: GET /sheets calls .to_list(length=100) — never .to_list(None)."""
    from unittest.mock import patch, call
    from app.routers.sheets import list_sheets

    with patch("app.routers.sheets.sheets_collection") as mock_col:
        mock_cursor = MagicMock()
        mock_cursor.sort = MagicMock(return_value=mock_cursor)
        mock_cursor.to_list = AsyncMock(return_value=[])
        mock_col.find = MagicMock(return_value=mock_cursor)

        await list_sheets(current_user=mock_firebase_user)

        mock_cursor.to_list.assert_called_once_with(length=100)


# ─────────────────────────────────────────────────────────────────────────────
# MAX_CELLS guard: PUT /sheets/{id} with oversized data raises 400
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.skip(reason="Implementing in 08-02-PLAN")
@pytest.mark.asyncio
async def test_update_sheet_max_cells(mock_firebase_user):
    """MAX_CELLS: PUT /sheets/{id} with data exceeding 1000×50 cells raises HTTPException(400)."""
    from fastapi import HTTPException
    from unittest.mock import patch
    from app.routers.sheets import update_sheet

    # 1001 rows × 50 columns = 50,050 cells — exceeds 1000×50 = 50,000 limit
    oversized_data = [[{"value": "x"} for _ in range(50)] for _ in range(1001)]

    with patch("app.routers.sheets.sheets_collection"):
        with pytest.raises(HTTPException) as exc_info:
            await update_sheet(
                sheet_id="617f1f77bcf86cd799439abc",
                update={"data": oversized_data},
                current_user=mock_firebase_user,
            )

    assert exc_info.value.status_code == 400


# ─────────────────────────────────────────────────────────────────────────────
# RET-01: DELETE /sheets/{id} sets deleted_at (soft delete — not physical delete)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.skip(reason="Implementing in 08-02-PLAN")
@pytest.mark.asyncio
async def test_soft_delete_sheet(mock_firebase_user):
    """RET-01: DELETE /sheets/{id} performs a soft delete — sets deleted_at, does not remove the document."""
    from unittest.mock import patch
    from app.routers.sheets import delete_sheet

    with patch("app.routers.sheets.sheets_collection") as mock_col:
        mock_col.update_one = AsyncMock(return_value=MagicMock(matched_count=1))

        await delete_sheet(
            sheet_id="617f1f77bcf86cd799439abc",
            current_user=mock_firebase_user,
        )

        # Verify update_one was called (soft delete), not delete_one (physical delete)
        mock_col.update_one.assert_called_once()
        update_filter, update_op = mock_col.update_one.call_args[0]
        assert "$set" in update_op
        assert "deleted_at" in update_op["$set"]
        # Confirm delete_one was NOT called
        mock_col.delete_one = MagicMock()
        mock_col.delete_one.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# deleted_at filter: GET /sheets/{id} for soft-deleted sheet returns 404
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.skip(reason="Implementing in 08-02-PLAN")
@pytest.mark.asyncio
async def test_get_deleted_sheet_returns_404(mock_firebase_user):
    """GET /sheets/{id} for a soft-deleted sheet returns 404 (deleted_at: None filter)."""
    from fastapi import HTTPException
    from unittest.mock import patch
    from app.routers.sheets import get_sheet

    # Returning None simulates the query filter {"deleted_at": None} excluding the soft-deleted doc
    with patch("app.routers.sheets.sheets_collection") as mock_col:
        mock_col.find_one = AsyncMock(return_value=None)

        with pytest.raises(HTTPException) as exc_info:
            await get_sheet(
                sheet_id="617f1f77bcf86cd799439abc",
                current_user=mock_firebase_user,
            )

    assert exc_info.value.status_code == 404
