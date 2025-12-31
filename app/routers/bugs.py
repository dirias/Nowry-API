from fastapi import APIRouter, Depends, HTTPException, status
from typing import List
from datetime import datetime
from bson import ObjectId

from app.models.Bug import (
    BugReport,
    BugReportCreate,
    BugReportResponse,
    BugStatusUpdate,
)
from app.config.database import bugs_collection, users_collection
from app.auth.auth import get_current_user_authorization

router = APIRouter(prefix="/bugs", tags=["bugs"])


@router.post("", response_model=BugReportResponse)
async def submit_bug_report(
    bug_data: BugReportCreate, current_user=Depends(get_current_user_authorization)
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
    #         status_code=status.HTTP_403_FORBIDDEN,
    #         detail="Bug reporting is only available for beta testers"
    #     )

    # Validate screenshot count (max 3)
    if len(bug_data.screenshots) > 3:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
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
        "browser_info": bug_data.browser_info.dict(),
        "screenshots": [s.dict() for s in bug_data.screenshots],
        "status": "open",
        "priority": None,
        "tags": [],
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow(),
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
async def get_my_bug_reports(current_user=Depends(get_current_user_authorization)):
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


# ========== DEVELOPER-ONLY ENDPOINTS ==========
# These must come BEFORE /{bug_id} routes to prevent "all" and "stats" being treated as bug IDs


@router.get("/all", response_model=List[BugReport])
async def get_all_bugs(
    status: str = None,
    severity: str = None,
    category: str = None,
    current_user=Depends(get_current_user_authorization),
):
    """
    Get all bug reports with optional filters (Dev only).

    Available filters:
    - status: open, in-progress, resolved, closed
    - severity: low, medium, high, critical
    - category: ui, functionality, performance, other
    """

    # Check if user is dev or beta
    user_id = current_user.get("user_id")
    user = await users_collection.find_one({"_id": ObjectId(user_id)})

    if not user or user.get("role") not in ["dev", "beta"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Developer or beta access required",
        )

    # Build query with filters
    query = {}
    if status:
        query["status"] = status
    if severity:
        query["severity"] = severity
    if category:
        query["category"] = category

    # Get bugs
    bugs = await bugs_collection.find(query).sort("created_at", -1).to_list(500)

    # Convert ObjectIds to strings
    for bug in bugs:
        bug["_id"] = str(bug["_id"])

    return bugs


@router.get("/stats", response_model=dict)
async def get_bug_stats(current_user=Depends(get_current_user_authorization)):
    """
    Get bug statistics (Dev only).

    Returns counts by status and severity.
    """

    # Check if user is dev or beta
    user_id = current_user.get("user_id")
    user = await users_collection.find_one({"_id": ObjectId(user_id)})

    if not user or user.get("role") not in ["dev", "beta"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Developer access required"
        )

    # Get statistics
    total = await bugs_collection.count_documents({})
    open_bugs = await bugs_collection.count_documents({"status": "open"})
    in_progress = await bugs_collection.count_documents({"status": "in-progress"})
    resolved = await bugs_collection.count_documents({"status": "resolved"})
    critical = await bugs_collection.count_documents({"severity": "critical"})
    high = await bugs_collection.count_documents({"severity": "high"})
    medium = await bugs_collection.count_documents({"severity": "medium"})
    low = await bugs_collection.count_documents({"severity": "low"})

    return {
        "total": total,
        "by_status": {
            "open": open_bugs,
            "in_progress": in_progress,
            "resolved": resolved,
        },
        "by_severity": {
            "critical": critical,
            "high": high,
            "medium": medium,
            "low": low,
        },
    }


# ========== USER ENDPOINTS (with path parameters) ==========


@router.get("/{bug_id}", response_model=BugReport)
async def get_bug_by_id(
    bug_id: str, current_user=Depends(get_current_user_authorization)
):
    """
    Get a specific bug report by ID.

    Users can only view their own bug reports.
    """

    try:
        bug = await bugs_collection.find_one({"_id": ObjectId(bug_id)})
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid bug ID format"
        )

    if not bug:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Bug report not found"
        )

    # Check if user owns this bug report
    user_id = current_user.get("user_id")
    if bug["user_id"] != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only view your own bug reports",
        )

    bug["_id"] = str(bug["_id"])
    return bug


@router.delete("/{bug_id}")
async def delete_bug_report(
    bug_id: str, current_user=Depends(get_current_user_authorization)
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
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid bug ID format"
        )

    if not bug:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Bug report not found"
        )

    # Check if user owns this bug report
    user_id = current_user.get("user_id")
    if bug["user_id"] != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only delete your own bug reports",
        )

    # Delete the bug report
    await bugs_collection.delete_one({"_id": ObjectId(bug_id)})

    return {"message": "Bug report deleted successfully"}


@router.patch("/{bug_id}/status", response_model=dict)
async def update_bug_status(
    bug_id: str,
    status_update: BugStatusUpdate,
    current_user=Depends(get_current_user_authorization),
):
    """
    Update bug status, priority, and notes (Dev only).

    Allows developers to manage bug lifecycle.
    """

    # Check if user is dev or beta
    user_id = current_user.get("user_id")
    user = await users_collection.find_one({"_id": ObjectId(user_id)})

    if not user or user.get("role") not in ["dev", "beta"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Developer access required"
        )

    # Find bug
    try:
        bug = await bugs_collection.find_one({"_id": ObjectId(bug_id)})
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid bug ID format"
        )

    if not bug:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Bug not found"
        )

    # Prepare update data
    update_data = {"status": status_update.status, "updated_at": datetime.utcnow()}

    if status_update.priority:
        update_data["priority"] = status_update.priority

    if status_update.notes:
        update_data["dev_notes"] = status_update.notes

    if status_update.status == "resolved":
        update_data["resolved_at"] = datetime.utcnow()

    # Update bug
    await bugs_collection.update_one({"_id": ObjectId(bug_id)}, {"$set": update_data})

    return {
        "message": "Bug status updated successfully",
        "bug_id": bug_id,
        "status": status_update.status,
    }
