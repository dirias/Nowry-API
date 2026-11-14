from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from fastapi import status as http_status
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, timezone
from bson import ObjectId

from app.models.Bug import (
    BUG_STATUSES,
    SEVERITIES,
    BugCategory,
    BugReport,
    BugReportCreate,
    BugReportResponse,
    BugRow,
    BugSeverity,
    BugStatus,
    BugStatusChanged,
    BugStatusUpdate,
    current_status,
    stored_statuses,
)
from app.models.common import BugStatsResponse, MessageResponse
from app.config.database import bugs_collection, users_collection
from app.auth.firebase_auth import get_firebase_user
from app.auth.dependencies import require_admin, require_recent_admin
from app.services.admin_actions import record_admin_action
from app.services.notifications import notify
from app.config.beta import beta_config
from app.core.body_limit import body_capped_route
from app.services.bug_limits import (
    DAILY_REPORTS,
    MAX_BODY_BYTES,
    daily_limit,
    oversized_text,
    screenshots_ok,
    too_large,
    utc_day_bounds,
)

router = APIRouter(prefix="/bugs", tags=["bugs"], route_class=body_capped_route(MAX_BODY_BYTES))


#: What `POST /bugs` answers when the beta is off (FR-007).
REPORTS_CLOSED: Dict[str, str] = {"code": "reports_closed"}


def ensure_reports_open() -> None:
    """Reports are taken only while the beta is on; otherwise 403 `reports_closed`."""
    if not beta_config()["active"]:
        raise HTTPException(status_code=http_status.HTTP_403_FORBIDDEN, detail=REPORTS_CLOSED)


def new_bug_document(bug_data: BugReportCreate, user_id: str, now: datetime) -> Dict[str, Any]:
    """The stored report: today's fields, the client's platform, and an empty note history."""
    return {
        "user_id": user_id,
        "title": bug_data.title,
        "description": bug_data.description,
        "steps_to_reproduce": bug_data.steps_to_reproduce,
        "expected_behavior": bug_data.expected_behavior,
        "actual_behavior": bug_data.actual_behavior,
        "severity": bug_data.severity,
        "category": bug_data.category,
        "url": bug_data.url or "",
        "browser_info": bug_data.browser_info.model_dump() if bug_data.browser_info else None,
        "screenshots": [s.model_dump() for s in bug_data.screenshots],
        "platform": bug_data.platform,
        "app_version": bug_data.app_version,
        "device": bug_data.device,
        "status": "new",
        "priority": None,
        "notes": [],
        "tags": [],
        "created_at": now,
        "updated_at": now,
        "resolved_at": None,
    }


def bug_notification(bug: Dict[str, Any], username: str) -> Tuple[str, str]:
    """Subject and body of the operator notice. The reporter by username, never by address."""
    lines = [
        f"Severity: {bug['severity']}",
        f"Category: {bug['category']}",
        f"Platform: {bug['platform']}",
        f"Device: {bug.get('device') or ''}",
        f"App version: {bug.get('app_version') or ''}",
        f"Page: {bug.get('url') or ''}",
        f"Reporter: {username}",
        "",
        bug["description"],
    ]
    return f"[Nowry] Bug: {bug['title']}", "\n".join(lines)


async def reporter_username(user_id: str) -> str:
    """The reporter's username, or "" when the account cannot be read."""
    if not ObjectId.is_valid(user_id):
        return ""
    account = await users_collection.find_one({"_id": ObjectId(user_id)}, {"username": 1})
    return (account or {}).get("username") or ""


async def notify_operator(bug: Dict[str, Any]) -> None:
    """Tell the operator about a new report. `notify` never raises."""
    subject, text = bug_notification(bug, await reporter_username(bug["user_id"]))
    await notify(subject, text)


def ensure_within_limits(bug_data: BugReportCreate) -> None:
    """413 `too_large` naming the first text or the screenshots that break a limit (ADMIN-008a)."""
    field = oversized_text(bug_data.model_dump(include={
        "title", "description", "steps_to_reproduce", "expected_behavior", "actual_behavior",
    }))
    if field is None and not screenshots_ok(shot.data for shot in bug_data.screenshots):
        field = "screenshots"
    if field is not None:
        raise HTTPException(status_code=http_status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail=too_large(field))


async def ensure_under_daily_limit(user_id: str, now: datetime) -> None:
    """429 `daily_limit` once the account has sent its reports for this UTC day."""
    start, end = utc_day_bounds(now)
    sent = await bugs_collection.count_documents(
        {"user_id": user_id, "created_at": {"$gte": start}}, limit=DAILY_REPORTS,
    )
    if sent >= DAILY_REPORTS:
        raise HTTPException(status_code=http_status.HTTP_429_TOO_MANY_REQUESTS, detail=daily_limit(end))


@router.post("", response_model=BugReportResponse)
async def submit_bug_report(
    bug_data: BugReportCreate,
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(get_firebase_user),
) -> BugReportResponse:
    """Store a tester's report while the beta is on, then tell the operator (BUG-001, FR-007)."""
    ensure_reports_open()
    ensure_within_limits(bug_data)
    now = datetime.now(timezone.utc)
    await ensure_under_daily_limit(current_user.get("user_id"), now)
    bug_dict = new_bug_document(bug_data, current_user.get("user_id"), now)
    result = await bugs_collection.insert_one(bug_dict)
    background_tasks.add_task(notify_operator, bug_dict)
    return BugReportResponse(
        bug_id=str(result.inserted_id),
        message="Bug report submitted successfully. Thank you for helping us improve!",
        status=bug_dict["status"],
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


# ========== ADMIN-ONLY ENDPOINTS (ADMIN-001, ADMIN-007b, ADR-086) ==========
# These must come BEFORE /{bug_id} routes to prevent "all" and "stats" being treated as bug IDs.
# `require_admin` reads `is_admin` from the account; `role` grants nothing.

#: The most reports one triage listing returns, and how many it returns unasked.
ALL_BUGS_LIMIT = 200
ALL_BUGS_DEFAULT = 100
#: What a malformed id answers on read, delete and change (ADMIN-007b).
BAD_ID: Dict[str, str] = {"code": "bad_id"}


def bug_oid(bug_id: str) -> ObjectId:
    """The path's report id, or `400 {"code": "bad_id"}` before anything is read."""
    if not ObjectId.is_valid(bug_id):
        raise HTTPException(status_code=http_status.HTTP_400_BAD_REQUEST, detail=BAD_ID)
    return ObjectId(bug_id)


async def find_bug(bug_id: str) -> Dict[str, Any]:
    """The stored report, or 400 for a malformed id and 404 for an unknown one."""
    bug = await bugs_collection.find_one({"_id": bug_oid(bug_id)})
    if not bug:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Bug report not found")
    return bug


def bug_filter(bug_status: Optional[str], severity: Optional[str], category: Optional[str]) -> Dict[str, Any]:
    """The listing's filter: only the fields the caller named; a status matches its old names too."""
    query: Dict[str, Any] = {field: value for field, value in (("severity", severity), ("category", category)) if value}
    if bug_status:
        query["status"] = {"$in": stored_statuses(bug_status)}
    return query


def note_history(bug: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The notes oldest first; a note from before the history (`dev_notes`) is the first entry."""
    legacy = [{"text": bug["dev_notes"], "by": None, "at": None}] if bug.get("dev_notes") else []
    return legacy + list(bug.get("notes") or [])


async def usernames(user_ids: List[str]) -> Dict[str, str]:
    """{user id: username} for the reporters of a page of reports, read in one bounded query."""
    oids = list({ObjectId(uid) for uid in user_ids if isinstance(uid, str) and ObjectId.is_valid(uid)})
    accounts = await users_collection.find({"_id": {"$in": oids}}, {"username": 1}).to_list(length=len(oids) or 1)
    return {str(account["_id"]): account.get("username") or "" for account in accounts}


def bug_row(bug: Dict[str, Any], username: str, with_notes: bool = True) -> BugRow:
    """One stored report as the tab reads it: status mapped, reporter by username, never the address."""
    browser = bug.get("browser_info") if isinstance(bug.get("browser_info"), dict) else None
    fields = {key: bug[key] for key in BugRow.model_fields if key in bug and bug[key] is not None}
    return BugRow(**{
        **fields,
        "id": str(bug["_id"]),
        "status": current_status(bug.get("status")),
        "browser_info": browser,
        "screen_size": (browser or {}).get("screen_resolution"),
        "reporter_username": username,
        "notes": note_history(bug) if with_notes else [],
    })


@router.get("/all", response_model=List[BugRow])
async def get_all_bugs(
    bug_status: Optional[BugStatus] = Query(default=None, alias="status"),
    severity: Optional[BugSeverity] = Query(default=None),
    category: Optional[BugCategory] = Query(default=None),
    limit: int = Query(default=ALL_BUGS_DEFAULT, ge=1, le=ALL_BUGS_LIMIT),
    current_user: dict = Depends(require_admin),
) -> List[BugRow]:
    """Reports newest first, optionally filtered by status, severity and category (admin only)."""
    bugs = await bugs_collection.find(bug_filter(bug_status, severity, category)).sort("created_at", -1).limit(limit).to_list(length=limit)
    names = await usernames([bug.get("user_id") for bug in bugs])
    return [bug_row(bug, names.get(bug.get("user_id"), "")) for bug in bugs]


@router.get("/stats", response_model=BugStatsResponse)
async def get_bug_stats(current_user: dict = Depends(require_admin)) -> BugStatsResponse:
    """Counts by status (old names counted under their new ones) and severity (admin only)."""
    by_status = {name: await bugs_collection.count_documents({"status": {"$in": stored_statuses(name)}}) for name in BUG_STATUSES}
    by_severity = {name: await bugs_collection.count_documents({"severity": name}) for name in SEVERITIES}
    return BugStatsResponse(total=await bugs_collection.count_documents({}), by_status=by_status, by_severity=by_severity)


# ========== ENDPOINTS WITH PATH PARAMETERS ==========


@router.get("/{bug_id}", response_model=BugRow)
async def get_bug_by_id(bug_id: str, current_user: dict = Depends(get_firebase_user)) -> BugRow:
    """One report. An admin reads any, with its note history; a reporter reads their own, without it."""
    bug = await find_bug(bug_id)
    if bug.get("user_id") != current_user.get("user_id"):
        await require_admin(current_user)  # 403 for anyone but an admin
        return bug_row(bug, (await usernames([bug.get("user_id")])).get(bug.get("user_id"), ""))
    is_admin = await caller_is_admin(current_user)
    return bug_row(bug, await reporter_username(bug["user_id"]), with_notes=is_admin)


async def caller_is_admin(current_user: dict) -> bool:
    """True when the caller's account holds `is_admin` (the same check as `require_admin`)."""
    try:
        await require_admin(current_user)
    except HTTPException:
        return False
    return True


@router.delete("/{bug_id}", response_model=MessageResponse)
async def delete_bug_report(bug_id: str, current_user: dict = Depends(get_firebase_user)) -> MessageResponse:
    """Delete one of the caller's own reports (sent by mistake)."""
    bug = await find_bug(bug_id)
    if bug["user_id"] != current_user.get("user_id"):
        raise HTTPException(
            status_code=http_status.HTTP_403_FORBIDDEN,
            detail="You can only delete your own bug reports",
        )
    await bugs_collection.delete_one({"_id": bug["_id"]})
    return MessageResponse(message="Bug report deleted successfully")


def triage_changes(bug: Dict[str, Any], change: BugStatusUpdate, admin_id: str, now: datetime) -> Dict[str, Any]:
    """The update: always the new name of the status, the priority if given, the note appended."""
    to_status = change.status or current_status(bug.get("status"))
    fields: Dict[str, Any] = {"status": to_status, "updated_at": now}
    if change.status == "fixed":
        fields["resolved_at"] = now
    if change.priority:
        fields["priority"] = change.priority
    update: Dict[str, Any] = {"$set": fields}
    if change.note:
        update["$push"] = {"notes": {"text": change.note, "by": admin_id, "at": now}}
    return update


@router.patch("/{bug_id}/status", response_model=BugStatusChanged)
async def update_bug_status(
    bug_id: str,
    status_update: BugStatusUpdate,
    current_user: dict = Depends(require_recent_admin),
) -> BugStatusChanged:
    """Change a report's status or priority and append a note (admin only). Logged as `bug.status`."""
    bug = await find_bug(bug_id)
    update = triage_changes(bug, status_update, current_user["user_id"], datetime.now(timezone.utc))
    await bugs_collection.update_one({"_id": bug["_id"]}, update)
    to_status = update["$set"]["status"]
    await record_admin_action(
        current_user["user_id"], "bug.status", "bug", bug_id, bug.get("title"),
        details={
            "from": current_status(bug.get("status")), "to": to_status,
            "note_added": status_update.note is not None, "priority": status_update.priority,
        },
    )
    changed = await bugs_collection.find_one({"_id": bug["_id"]}) or bug
    return BugStatusChanged(
        message="Bug status updated successfully", bug_id=bug_id, status=to_status,
        priority=changed.get("priority"), notes=note_history(changed),
    )
