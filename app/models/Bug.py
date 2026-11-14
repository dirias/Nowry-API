from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from typing import Any, Dict, List, Literal, Optional, get_args
from datetime import datetime

#: How bad the problem is.
BugSeverity = Literal["low", "medium", "high", "critical"]
#: Where the problem is: the web form's four areas and the phone's (BUG-001).
BugCategory = Literal["ui", "functionality", "performance", "study", "books", "planning", "focus", "other"]
#: Which client sent the report.
BugPlatform = Literal["web", "android"]
#: The fixed triage states (ADMIN-007b). Only these are written.
BugStatus = Literal["new", "in_progress", "fixed", "wont_fix"]
BUG_STATUSES: tuple = get_args(BugStatus)
#: Stored values from before ADMIN-007b and what they read as.
LEGACY_STATUSES: Dict[str, str] = {"open": "new", "in-progress": "in_progress", "resolved": "fixed", "closed": "wont_fix"}
SEVERITIES: tuple = get_args(BugSeverity)
NOTE_MAX_LENGTH: int = 2000


def current_status(stored: Optional[str]) -> str:
    """A stored status as the fixed list names it; anything unknown reads as `new`."""
    status = LEGACY_STATUSES.get(stored or "", stored)
    return status if status in BUG_STATUSES else "new"


def stored_statuses(status: str) -> List[str]:
    """Every stored value that reads as `status`, for filters and counts."""
    return [status, *(old for old, new in LEGACY_STATUSES.items() if new == status)]


class Screenshot(BaseModel):
    """Screenshot attachment for bug report"""

    filename: str
    data: str  # Base64 encoded image
    uploaded_at: datetime = Field(default_factory=datetime.utcnow)


class BrowserInfo(BaseModel):
    """Browser and system information"""

    name: str
    version: str
    os: str
    screen_resolution: str


class BugReport(BaseModel):
    """Bug report model"""

    id: Optional[str] = Field(alias="_id", default=None)
    user_id: str
    title: str
    description: str
    steps_to_reproduce: Optional[str] = ""
    expected_behavior: Optional[str] = ""
    actual_behavior: Optional[str] = ""
    severity: str = "medium"  # low, medium, high, critical
    category: str = "functionality"  # ui, functionality, performance, other
    url: Optional[str] = ""  # Page URL where bug occurred (none from the phone)
    browser_info: Optional[BrowserInfo] = None
    platform: str = "web"
    app_version: Optional[str] = None
    device: Optional[str] = None
    screenshots: List[Screenshot] = []
    status: str = "new"  # new, in_progress, fixed, wont_fix (old values mapped on read)
    priority: Optional[str] = None  # For future admin use
    tags: List[str] = []
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    resolved_at: Optional[datetime] = None

    class Config:
        populate_by_name = True
        json_encoders = {datetime: lambda v: v.isoformat()}


    @field_validator("status", mode="before")
    @classmethod
    def _read_status(cls, value: Optional[str]) -> str:
        return current_status(value)


class BugReportCreate(BaseModel):
    """A report from the web form or the phone (BUG-001, FR-007).

    The phone sends `platform: "android"`, its device and app version, and no
    url, browser info or screenshots. Text lengths are checked by the route so
    a long field answers 413 with its name (ADMIN-008a), not 422.
    """

    title: str
    description: str
    steps_to_reproduce: Optional[str] = ""
    expected_behavior: Optional[str] = ""
    actual_behavior: Optional[str] = ""
    severity: BugSeverity = "medium"
    category: BugCategory = "functionality"
    url: Optional[str] = ""
    browser_info: Optional[BrowserInfo] = None
    screenshots: List[Screenshot] = []
    platform: BugPlatform = "web"
    app_version: Optional[str] = Field(default=None, max_length=40)
    device: Optional[str] = Field(default=None, max_length=120)


class BugReportResponse(BaseModel):
    """Response after submitting a bug report"""

    bug_id: str
    message: str
    status: str
    created_at: datetime


class BugNote(BaseModel):
    """One entry of a report's note history: what was written, by which admin, when.

    A note from before the history (`dev_notes`) reads as the first entry, with no author or time.
    """

    text: str
    by: Optional[str] = None
    at: Optional[datetime] = None


class BugScreenshotRow(BaseModel):
    """A screenshot as stored today (a base64 data URL); other stored keys pass through."""

    model_config = ConfigDict(extra="allow")

    filename: str = ""
    data: Optional[str] = None
    uploaded_at: Optional[datetime] = None


class BugRow(BaseModel):
    """One report as the Bug reports tab reads it (ADMIN-007b). The reporter by username only."""

    id: str
    title: str
    description: str = ""
    steps_to_reproduce: Optional[str] = ""
    expected_behavior: Optional[str] = ""
    actual_behavior: Optional[str] = ""
    severity: str = "medium"
    category: str = "functionality"
    status: BugStatus = "new"
    priority: Optional[str] = None
    url: Optional[str] = ""
    platform: str = "web"
    app_version: Optional[str] = None
    device: Optional[str] = None
    browser_info: Optional[Dict[str, Any]] = None
    screen_size: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    resolved_at: Optional[datetime] = None
    reporter_username: str = ""
    screenshots: List[BugScreenshotRow] = []
    notes: List[BugNote] = []


class BugStatusUpdate(BaseModel):
    """An admin's change to a report: a status, a priority, a note to append; at least one."""

    status: Optional[BugStatus] = None
    priority: Optional[str] = Field(default=None, max_length=40)
    note: Optional[str] = Field(default=None, max_length=NOTE_MAX_LENGTH)

    @field_validator("note")
    @classmethod
    def _blank_note_is_none(cls, value: Optional[str]) -> Optional[str]:
        return value.strip() or None if value is not None else None

    @model_validator(mode="after")
    def _something_to_change(self) -> "BugStatusUpdate":
        if self.status is None and not self.priority and self.note is None:
            raise ValueError("give a status, a priority or a note")
        return self


class BugStatusChanged(BaseModel):
    """What `PATCH /bugs/{id}/status` answers: the report's state and note history after the change."""

    message: str
    bug_id: str
    status: BugStatus
    priority: Optional[str] = None
    notes: List[BugNote] = []
