from pydantic import BaseModel, Field
from typing import List, Literal, Optional
from datetime import datetime

#: How bad the problem is.
BugSeverity = Literal["low", "medium", "high", "critical"]
#: Where the problem is: the web form's four areas and the phone's (BUG-001).
BugCategory = Literal["ui", "functionality", "performance", "study", "books", "planning", "focus", "other"]
#: Which client sent the report.
BugPlatform = Literal["web", "android"]


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
    status: str = "open"  # open, in-progress, resolved, closed
    priority: Optional[str] = None  # For future admin use
    tags: List[str] = []
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    resolved_at: Optional[datetime] = None

    class Config:
        populate_by_name = True
        json_encoders = {datetime: lambda v: v.isoformat()}


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


class BugStatusUpdate(BaseModel):
    """Schema for updating bug status (Dev only)"""

    status: str  # open, in-progress, resolved, closed
    priority: Optional[str] = None  # low, medium, high, urgent
    notes: Optional[str] = None  # Developer notes
