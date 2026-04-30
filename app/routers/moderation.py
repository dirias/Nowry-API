"""
Content Moderation API Router
Handles reporting inappropriate/problematic public content
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from typing import Optional, Literal
from pydantic import BaseModel
from bson import ObjectId
from datetime import datetime, timezone

from app.config.database import db
from app.models.PublicContent import ContentReport
from app.auth.firebase_auth import get_current_user
from app.auth.dependencies import require_admin

router = APIRouter(prefix="/moderation", tags=["Content Moderation"])


# ========== Request Models ==========

class ReportContentRequest(BaseModel):
    reason: Literal["copyright", "inappropriate", "spam", "misinformation", "other"]
    description: Optional[str] = None


class ReviewReportRequest(BaseModel):
    action_taken: str  # "removed", "kept_public", "warned_creator"
    resolution_notes: Optional[str] = None


# ========== Reporting Endpoints (Auth Required) ==========

@router.post("/report/{content_type}/{content_id}")
async def report_content(
    content_type: Literal["book", "deck", "card"],
    content_id: str,
    report_data: ReportContentRequest,
    current_user: dict = Depends(get_current_user)
):
    """
    Report inappropriate or problematic public content.
    
    Args:
        content_type: Type of content ("book", "deck", "card")
        content_id: ID of the content to report
        report_data: Report details
        
    Reasons:
        - copyright: Copyright infringement
        - inappropriate: Offensive/NSFW content
        - spam: Spam or low-quality
        - misinformation: Factually incorrect
        - other: Other issues
    """
    # Verify content exists and is public
    collection = db[f"{content_type}s"]
    content = await collection.find_one({
        "_id": ObjectId(content_id),
        "is_public": True
    })
    
    if not content:
        raise HTTPException(status_code=404, detail="Public content not found")
    
    # Check if user already reported this content
    existing_report = await db["content_reports"].find_one({
        "content_type": content_type,
        "content_id": content_id,
        "reporter_user_id": current_user["uid"]
    })
    
    if existing_report:
        raise HTTPException(status_code=400, detail="You have already reported this content")
    
    # Create report
    title_field = "title" if content_type == "book" else "name"
    content_title = content.get(title_field, "Untitled")
    
    report = ContentReport(
        content_type=content_type,
        content_id=content_id,
        content_title=content_title,
        reporter_user_id=current_user["uid"],
        reporter_email=current_user.get("email"),
        reason=report_data.reason,
        description=report_data.description,
        status="pending"
    )
    
    result = await db["content_reports"].insert_one(report.model_dump(by_alias=True))
    
    # TODO: Send notification to moderators/admin
    # await notify_moderators(report_id=str(result.inserted_id))
    
    return {
        "message": "Report submitted successfully. Our team will review it shortly.",
        "report_id": str(result.inserted_id)
    }


@router.get("/reports")
async def get_my_reports(
    status: Optional[Literal["pending", "under_review", "resolved", "dismissed"]] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: dict = Depends(get_current_user)
):
    """
    Get reports I've submitted.
    
    Query Parameters:
        - status: Filter by report status
        - page: Page number
        - page_size: Items per page
    """
    query = {"reporter_user_id": current_user["uid"]}
    
    if status:
        query["status"] = status
    
    total = await db["content_reports"].count_documents(query)
    skip = (page - 1) * page_size
    
    reports = await db["content_reports"].find(query).sort("created_at", -1).skip(skip).limit(page_size).to_list(page_size)
    
    return {
        "items": reports,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": (total + page_size - 1) // page_size
    }


# ========== Admin/Moderator Endpoints (Admin Auth Required) ==========
# TODO: Add admin authorization middleware

@router.get("/admin/reports")
async def get_all_reports(
    status: Optional[Literal["pending", "under_review", "resolved", "dismissed"]] = None,
    reason: Optional[Literal["copyright", "inappropriate", "spam", "misinformation", "other"]] = None,
    content_type: Optional[Literal["book", "deck", "card"]] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: dict = Depends(require_admin)
):
    """
    Get all content reports (Admin only).
    """
    
    query = {}
    
    if status:
        query["status"] = status
    
    if reason:
        query["reason"] = reason
    
    if content_type:
        query["content_type"] = content_type
    
    total = await db["content_reports"].count_documents(query)
    skip = (page - 1) * page_size
    
    reports = await db["content_reports"].find(query).sort("created_at", -1).skip(skip).limit(page_size).to_list(page_size)
    
    return {
        "items": reports,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": (total + page_size - 1) // page_size
    }


@router.put("/admin/reports/{report_id}/review")
async def review_report(
    report_id: str,
    review_data: ReviewReportRequest,
    current_user: dict = Depends(require_admin)
):
    """
    Review and resolve a content report (Admin only).
    """
    
    # Get report
    report = await db["content_reports"].find_one({"_id": ObjectId(report_id)})
    
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    
    # Update report
    update_data = {
        "status": "resolved",
        "reviewed_by": current_user["uid"],
        "reviewed_at": datetime.now(timezone.utc),
        "action_taken": review_data.action_taken,
        "resolution_notes": review_data.resolution_notes,
        "updated_at": datetime.now(timezone.utc)
    }
    
    await db["content_reports"].update_one(
        {"_id": ObjectId(report_id)},
        {"$set": update_data}
    )
    
    # If action is "removed", unpublish the content
    if review_data.action_taken == "removed":
        collection = db[f"{report['content_type']}s"]
        await collection.update_one(
            {"_id": ObjectId(report["content_id"])},
            {
                "$set": {
                    "is_public": False,
                    "updated_at": datetime.now(timezone.utc)
                }
            }
        )
    
    # TODO: Send notification to reporter and content creator
    
    return {
        "message": "Report reviewed successfully",
        "report_id": report_id,
        "action_taken": review_data.action_taken
    }


@router.delete("/admin/content/{content_type}/{content_id}")
async def remove_public_content(
    content_type: Literal["book", "deck"],
    content_id: str,
    reason: str,
    current_user: dict = Depends(require_admin)
):
    """
    Remove content from public access (Admin only).
    Makes content private and notifies creator.
    """
    
    collection = db[f"{content_type}s"]
    
    content = await collection.find_one({
        "_id": ObjectId(content_id),
        "is_public": True
    })
    
    if not content:
        raise HTTPException(status_code=404, detail="Public content not found")
    
    # Unpublish content
    await collection.update_one(
        {"_id": ObjectId(content_id)},
        {
            "$set": {
                "is_public": False,
                "updated_at": datetime.now(timezone.utc)
            }
        }
    )
    
    # TODO: Send notification to content creator
    # await notify_creator(
    #     user_id=content["user_id"],
    #     content_type=content_type,
    #     content_id=content_id,
    #     reason=reason
    # )
    
    return {
        "message": "Content removed from public access",
        "content_id": content_id,
        "reason": reason
    }
