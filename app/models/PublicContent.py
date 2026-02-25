"""
Models for public content sharing and discovery
"""
from pydantic import BaseModel, Field
from typing import Optional, List, Literal
from datetime import datetime
from .types import PyObjectId


class PublicMetadata(BaseModel):
    """
    Metadata for publicly shared content.
    Tracks engagement metrics and discovery information.
    """
    views: int = 0
    likes: int = 0
    forks: int = 0  # Number of times content was cloned/forked
    downloads: int = 0
    
    # Discovery & Categorization
    category: Optional[str] = None  # "Science", "Math", "Languages", etc.
    tags: List[str] = Field(default_factory=list)
    language: str = "en"  # Content language (en, es, fr, de, ja)
    difficulty_level: Optional[Literal["beginner", "intermediate", "advanced"]] = None
    
    # Quality Indicators
    average_rating: float = 0.0  # 0-5 stars
    rating_count: int = 0
    
    # Legal & Attribution
    license_type: str = "all_rights_reserved"  # CC-BY, CC-BY-SA, CC0, all_rights_reserved
    is_original_content: bool = True
    original_source: Optional[str] = None  # If adapted from elsewhere
    attribution: Optional[str] = None
    
    # Access Control
    restricted_to: Optional[Literal["dev", "beta", "premium"]] = None  # Restrict visibility to specific user groups
    
    class Config:
        from_attributes = True


class ContentReport(BaseModel):
    """
    User reports for inappropriate or problematic public content.
    """
    id: PyObjectId = Field(default_factory=PyObjectId, alias="_id")
    
    # What's being reported
    content_type: Literal["book", "deck", "card"]
    content_id: str
    content_title: Optional[str] = None  # Cached for reference
    
    # Who reported it
    reporter_user_id: str
    reporter_email: Optional[str] = None
    
    # Report details
    reason: Literal[
        "copyright",  # Copyright infringement
        "inappropriate",  # Offensive/NSFW content
        "spam",  # Spam or low-quality
        "misinformation",  # Factually incorrect
        "other"
    ]
    description: Optional[str] = None
    
    # Moderation
    status: Literal["pending", "under_review", "resolved", "dismissed"] = "pending"
    reviewed_by: Optional[str] = None
    reviewed_at: Optional[datetime] = None
    resolution_notes: Optional[str] = None
    action_taken: Optional[str] = None  # "removed", "kept_public", "warned_creator", etc.
    
    # Timestamps
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    
    class Config:
        populate_by_name = True
        json_encoders = {PyObjectId: str}


class ContentFork(BaseModel):
    """
    Tracks when users fork/clone public content.
    Useful for analytics and showing popularity.
    """
    id: PyObjectId = Field(default_factory=PyObjectId, alias="_id")
    
    # Original content
    original_content_type: Literal["book", "deck"]
    original_content_id: str
    original_creator_id: str
    
    # Fork details
    forked_content_id: str
    forked_by_user_id: str
    
    # Timestamps
    forked_at: datetime = Field(default_factory=datetime.utcnow)
    
    class Config:
        populate_by_name = True
        json_encoders = {PyObjectId: str}


class ContentLike(BaseModel):
    """
    Tracks user likes/favorites for public content.
    """
    id: PyObjectId = Field(default_factory=PyObjectId, alias="_id")
    
    content_type: Literal["book", "deck"]
    content_id: str
    user_id: str
    
    created_at: datetime = Field(default_factory=datetime.utcnow)
    
    class Config:
        populate_by_name = True
        json_encoders = {PyObjectId: str}


class ContentView(BaseModel):
    """
    Track views for analytics (optional - can be heavyweight).
    Consider using Redis or time-series DB for better performance.
    """
    id: PyObjectId = Field(default_factory=PyObjectId, alias="_id")
    
    content_type: Literal["book", "deck"]
    content_id: str
    
    # Viewer info (optional for anonymous views)
    viewer_user_id: Optional[str] = None
    ip_address: Optional[str] = None
    user_agent: Optional[str] = None
    
    viewed_at: datetime = Field(default_factory=datetime.utcnow)
    
    class Config:
        populate_by_name = True
        json_encoders = {PyObjectId: str}
