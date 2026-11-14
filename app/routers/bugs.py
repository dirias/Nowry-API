from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import status as http_status
from typing import Any, Dict, List, Optional
from datetime import datetime, timezone
from bson import ObjectId

from app.models.Bug import (
    BugReport,
    BugReportCreate,
    BugReportResponse,
    BugStatusUpdate,
)
from app.models.common import BugStatsResponse, BugStatusUpdateResponse, MessageResponse
from app.config.database import bugs_collection, users_collection
from app.auth.firebase_auth import get_firebase_user
from app.auth.dependencies import require_admin
from app.services.admin_actions import record_admin_action

router = APIRouter(prefix="/bugs", tags=["bugs"])


@router.post("", response_model=BugReportResponse)
async def submit_bug_report(
    bug_data: BugReportCreate, current_user=Depends(get_firebase_user)
):
    """
    Submit a new bug report.

    Available to all users (especially beta testers).
    Captures bug details, screenshots, and browser information.
    """

    # Check if user has beta features (optional - for now allow all users)
    user_id = current_user.get("user_id")
    user = await users_collection.find_one({"_id": ObjectId(user_id)})

    # For beta, we can allow all users to report bugs
    # Later, we can restrict to beta testers only
    # if not user.get("beta_features", {}).get("bug_reporting", False):
    #     raise HTTPException(
    #         status_code=http_status.HTTP_403_FORBIDDEN,
    #         detail="Bug reporting is only available for beta testers"
    #     )

    # Validate screenshot count (max 3)
    if len(bug_data.screenshots) > 3:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail="Maximum 3 screenshots allowed per bug report",
        )

    # Create bug report document
    bug_dict = {
        "user_id": user_id,
        "title": bug_data.title,
        "description": bug_data.description,
        "steps_to_reproduce": bug_data.steps_to_reproduce,
        "expected_behavior": bug_data.expected_behavior,
        "actual_behavior": bug_data.actual_behavior,
        "severity": bug_data.severity,
        "category": bug_data.category,
        "url": bug_data.url,
        "browser_info": bug_data.browser_info.model_dump(),
        "screenshots": [s.model_dump() for s in bug_data.screenshots],
        "status": "open",
        "priority": None,
        "tags": [],
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
        "resolved_at": None,
    }

    # Insert into database
    result = await bugs_collection.insert_one(bug_dict)

    return BugReportResponse(
        bug_id=str(result.inserted_id),
        message="Bug report submitted successfully. Thank you for helping us improve!",
        status="open",
        created_at=bug_dict["created_at"],
    )


@router.get("/my-reports", response_model=List[BugReport])
async def get_my_bug_reports(current_user=Depends(get_firebase_user)):
    """
    Get all bug reports submitted by the current user.

    Returns bugs sorted by creation date (newest first).
    """

    user_id = current_user.get("user_id")
    bugs_cursor = bugs_collection.find({"user_id": user_id}).sort("created_at", -1)

    bugs = await bugs_cursor.to_list(length=100)

    # Convert ObjectId to string for response
    for bug in bugs:
        bug["_id"] = str(bug["_id"])

    return bugs


# ========== ADMIN-ONLY ENDPOINTS (ADMIN-001, ADR-086) ==========
# These must come BEFORE /{bug_id} routes to prevent "all" and "stats" being treated as bug IDs.
# `require_admin` reads `is_admin` from the account; `role` grants nothing.

#: The most reports one triage listing returns.
ALL_BUGS_LIMIT = 500
STATUS_COUNTS = {"open": "open", "in_progress": "in-progress", "resolved": "resolved"}
SEVERITIES = ("critical", "high", "medium", "low")


def bug_filter(bug_status: Optional[str], severity: Optional[str], category: Optional[str]) -> Dict[str, str]:
    """The listing's filter: only the fields the caller named."""
    named = {"status": bug_status, "severity": severity, "category": category}
    return {field: value for field, value in named.items() if value}


@router.get("/all", response_model=List[BugReport])
async def get_all_bugs(
    bug_status: Optional[str] = Query(default=None, alias="status", max_length=40),
    severity: Optional[str] = Query(default=None, max_length=40),
    category: Optional[str] = Query(default=None, max_length=40),
    current_user: dict = Depends(require_admin),
) -> List[dict]:
    """Every bug report, newest first, optionally filtered (admin only).

    - status: open, in-progress, resolved, closed
    - severity: low, medium, high, critical
    - category: ui, functionality, performance, other
    """
    bugs = await bugs_collection.find(bug_filter(bug_status, severity, category)).sort("created_at", -1).to_list(ALL_BUGS_LIMIT)
    for bug in bugs:
        bug["_id"] = str(bug["_id"])
    return bugs


@router.get("/stats", response_model=BugStatsResponse)
async def get_bug_stats(current_user: dict = Depends(require_admin)) -> BugStatsResponse:
    """Counts by status and severity (admin only)."""
    by_status = {key: await bugs_collection.count_documents({"status": value}) for key, value in STATUS_COUNTS.items()}
    by_severity = {name: await bugs_collection.count_documents({"severity": name}) for name in SEVERITIES}
    return BugStatsResponse(total=await bugs_collection.count_documents({}), by_status=by_status, by_severity=by_severity)


# ========== USER ENDPOINTS (with path parameters) ==========


@router.get("/{bug_id}", response_model=BugReport)
async def get_bug_by_id(
    bug_id: str, current_user=Depends(get_firebase_user)
):
    """
    Get a specific bug report by ID.

    Users can only view their own bug reports.
    """

    try:
        bug = await bugs_collection.find_one({"_id": ObjectId(bug_id)})
    except Exception:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST, detail="Invalid bug ID format"
        )

    if not bug:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail="Bug report not found"
        )

    # Check if user owns this bug report
    user_id = current_user.get("user_id")
    if bug["user_id"] != user_id:
        raise HTTPException(
            status_code=http_status.HTTP_403_FORBIDDEN,
            detail="You can only view your own bug reports",
        )

    bug["_id"] = str(bug["_id"])
    return bug


@router.delete("/{bug_id}", response_model=MessageResponse)
async def delete_bug_report(
    bug_id: str, current_user=Depends(get_firebase_user)
):
    """
    Delete a bug report.

    Users can only delete their own bug reports.
    Useful if submitted by mistake.
    """

    try:
        bug = await bugs_collection.find_one({"_id": ObjectId(bug_id)})
    except Exception:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST, detail="Invalid bug ID format"
        )

    if not bug:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail="Bug report not found"
        )

    # Check if user owns this bug report
    user_id = current_user.get("user_id")
    if bug["user_id"] != user_id:
        raise HTTPException(
            status_code=http_status.HTTP_403_FORBIDDEN,
            detail="You can only delete your own bug reports",
        )

    # Delete the bug report
    await bugs_collection.delete_one({"_id": ObjectId(bug_id)})

    return {"message": "Bug report deleted successfully"}


@router.patch("/{bug_id}/status", response_model=BugStatusUpdateResponse)
async def update_bug_status(
    bug_id: str,
    status_update: BugStatusUpdate,
    current_user: dict = Depends(require_admin),
) -> BugStatusUpdateResponse:
    """Change a report's status, priority and notes (admin only). Logged as `bug.status`."""
    if not ObjectId.is_valid(bug_id):
        raise HTTPException(status_code=http_status.HTTP_400_BAD_REQUEST, detail="Invalid bug ID format")
    bug = await bugs_collection.find_one({"_id": ObjectId(bug_id)})
    if not bug:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Bug not found")

    now = datetime.now(timezone.utc)
    update_data: Dict[str, Any] = {"status": status_update.status, "updated_at": now}
    if status_update.priority:
        update_data["priority"] = status_update.priority
    if status_update.notes:
        update_data["dev_notes"] = status_update.notes
    if status_update.status == "resolved":
        update_data["resolved_at"] = now
    await bugs_collection.update_one({"_id": ObjectId(bug_id)}, {"$set": update_data})
    await record_admin_action(
        current_user["user_id"], "bug.status", "bug", bug_id, bug.get("title"),
        details={"from": bug.get("status"), "to": status_update.status, "priority": status_update.priority},
    )
    return BugStatusUpdateResponse(message="Bug status updated successfully", bug_id=bug_id, status=status_update.status)
