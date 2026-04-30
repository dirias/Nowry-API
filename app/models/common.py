"""
Shared response models used across multiple routers.
"""
from pydantic import BaseModel
from typing import Any, Dict, List, Optional


class MessageResponse(BaseModel):
    """Generic response for operations that return a status message."""
    message: str


class OkResponse(BaseModel):
    """Generic response for lightweight acknowledgement endpoints."""
    ok: bool


class BugStatsResponse(BaseModel):
    """Response for GET /bugs/stats — counts by status and severity."""
    total: int
    by_status: Dict[str, int]
    by_severity: Dict[str, int]


class BugStatusUpdateResponse(BaseModel):
    """Response for PATCH /bugs/{bug_id}/status."""
    message: str
    bug_id: str
    status: str


class AvatarUploadResponse(BaseModel):
    """Response for POST /users/avatar."""
    message: str
    avatar_url: str


class TwoFactorEnableResponse(BaseModel):
    """Response for POST /users/2fa/enable."""
    message: str
    backup_codes: List[str]


class AccountDeleteResponse(BaseModel):
    """Response for DELETE /users/account."""
    message: str
    recovery_deadline: str


class UserAuthResponse(BaseModel):
    """Response returned by the /register and /login auth endpoints."""
    message: str
    user_id: str
    firebase_uid: str
    email: str
    username: Optional[str] = None
    photo_url: Optional[str] = None
    role: Optional[str] = None
    wizard_completed: bool = False
    subscription: Optional[Dict[str, Any]] = None


class BlackboardResponse(BaseModel):
    """Response for GET and PUT blackboard endpoints."""
    id: str
    board_id: Optional[str] = None
    user_id: str
    name: str
    nodes: List[Dict[str, Any]] = []
    edges: List[Dict[str, Any]] = []
    viewport: Optional[Dict[str, Any]] = None
    created_at: Optional[Any] = None
    updated_at: Optional[Any] = None


class FullAnnualPlanResponse(BaseModel):
    """
    Aggregated response for GET /annual-plan/full.
    Returns the plan together with all associated focus areas, priorities,
    goals, activities, and quarter reports in a single round-trip.
    """
    plan: Dict[str, Any]
    focus_areas: List[Dict[str, Any]]
    priorities: List[Dict[str, Any]]
    goals: List[Dict[str, Any]]
    activities: List[Dict[str, Any]]
    quarter_reports: List[Dict[str, Any]]
