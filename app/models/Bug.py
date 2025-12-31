from pydantic import BaseModel, Field
from typing import List, Optional
from datetime import datetime


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
    url: str  # Page URL where bug occurred
    browser_info: BrowserInfo
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
    """Schema for creating a bug report"""

    title: str
    description: str
    steps_to_reproduce: Optional[str] = ""
    expected_behavior: Optional[str] = ""
    actual_behavior: Optional[str] = ""
    severity: str = "medium"
    category: str = "functionality"
    url: str
    browser_info: BrowserInfo
    screenshots: List[Screenshot] = []


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
