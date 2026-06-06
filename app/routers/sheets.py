"""
Micro Sheets router — full CRUD for user-owned spreadsheet sheets.

Endpoints:
    GET    /sheets            — list all (non-deleted) sheets for authenticated user
    POST   /sheets            — create a new sheet
    GET    /sheets/{sheet_id} — retrieve a single sheet (IDOR-protected)
    PUT    /sheets/{sheet_id} — update title / data / column_labels (MAX_CELLS guard)
    DELETE /sheets/{sheet_id} — soft-delete (sets deleted_at, does not remove)

Security (threat model 08-02):
    T-08-02-01  IDOR: every query includes {"user_id": user_id} — cross-user returns 404
    T-08-02-02  user_id set from Firebase token only — never from request body
    T-08-02-03  MAX_CELLS guard: raises HTTP 400 before any DB write when rows×cols > 50,000
    T-08-02-04  Bounded list: .to_list(length=100) — never unbounded
    T-08-02-05  Depends(get_firebase_user) on every endpoint — unauthenticated → 401
"""
from datetime import datetime, timezone
from typing import List

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException

from app.auth.firebase_auth import get_firebase_user
from app.config.database import sheets_collection
from app.models.common import MessageResponse
from app.models.sheets import Sheet, SheetCreate, SheetSummary, SheetUpdate

router = APIRouter(prefix="/sheets", tags=["sheets"])

MAX_CELLS = 50_000  # 1000 rows × 50 columns


def _check_max_cells(data: list) -> None:
    """Raise HTTP 400 if sheet data exceeds MAX_CELLS (T-08-02-03)."""
    if not data:
        return
    rows = len(data)
    cols = max((len(row) for row in data), default=0)
    if rows * cols > MAX_CELLS:
        raise HTTPException(
            status_code=400,
            detail=f"Sheet exceeds maximum cell limit ({MAX_CELLS} cells).",
        )


@router.get("", response_model=List[SheetSummary])
async def list_sheets(
    current_user: dict = Depends(get_firebase_user),
) -> List[SheetSummary]:
    """Return all non-deleted sheets for the authenticated user, sorted by updated_at desc."""
    user_id: str = current_user.get("user_id")
    sheets = (
        await sheets_collection.find(
            {"user_id": user_id, "deleted_at": None}
        )
        .sort("updated_at", -1)
        .to_list(length=100)
    )
    return sheets


@router.post("", response_model=Sheet, status_code=201)
async def create_sheet(
    sheet: SheetCreate,
    current_user: dict = Depends(get_firebase_user),
) -> Sheet:
    """Create a new sheet. user_id is sourced from the Firebase token — never from the body."""
    user_id: str = current_user.get("user_id")
    if sheet.data:
        _check_max_cells(sheet.data)
    now = datetime.now(timezone.utc)
    doc = {
        "user_id": user_id,
        "title": sheet.title,
        "data": sheet.data,
        "column_labels": sheet.column_labels,
        "created_at": now,
        "updated_at": now,
        "deleted_at": None,
        "deleted_by": None,
    }
    result = await sheets_collection.insert_one(doc)
    created = await sheets_collection.find_one({"_id": result.inserted_id})
    return created


@router.get("/{sheet_id}", response_model=Sheet)
async def get_sheet(
    sheet_id: str,
    current_user: dict = Depends(get_firebase_user),
) -> Sheet:
    """Retrieve a sheet by ID. Returns 404 if not found, wrong user, or soft-deleted."""
    user_id: str = current_user.get("user_id")
    sheet = await sheets_collection.find_one(
        {"_id": ObjectId(sheet_id), "user_id": user_id, "deleted_at": None}
    )
    if not sheet:
        raise HTTPException(status_code=404, detail="Sheet not found.")
    return sheet


@router.put("/{sheet_id}", response_model=Sheet)
async def update_sheet(
    sheet_id: str,
    update: SheetUpdate,
    current_user: dict = Depends(get_firebase_user),
) -> Sheet:
    """Update a sheet's title, data, and/or column_labels. MAX_CELLS guard applied to data."""
    user_id: str = current_user.get("user_id")
    updates: dict = {"updated_at": datetime.now(timezone.utc)}
    if update.title is not None:
        updates["title"] = update.title
    if update.data is not None:
        _check_max_cells(update.data)
        updates["data"] = update.data
    if update.column_labels is not None:
        updates["column_labels"] = update.column_labels

    result = await sheets_collection.update_one(
        {"_id": ObjectId(sheet_id), "user_id": user_id, "deleted_at": None},
        {"$set": updates},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Sheet not found.")
    return await sheets_collection.find_one({"_id": ObjectId(sheet_id)})


@router.delete("/{sheet_id}", response_model=MessageResponse)
async def delete_sheet(
    sheet_id: str,
    current_user: dict = Depends(get_firebase_user),
) -> MessageResponse:
    """Soft-delete a sheet by setting deleted_at. Does not physically remove the document."""
    user_id: str = current_user.get("user_id")
    now = datetime.now(timezone.utc)
    result = await sheets_collection.update_one(
        {"_id": ObjectId(sheet_id), "user_id": user_id, "deleted_at": None},
        {"$set": {"deleted_at": now, "deleted_by": user_id, "updated_at": now}},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Sheet not found.")
    return {"message": "Sheet deleted."}
