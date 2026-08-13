"""
Mixins for model functionality
"""
from typing import Optional
from datetime import datetime, timezone
from pydantic import Field


class SoftDeleteMixin:
    """
    Mixin to add soft delete functionality to models.
    
    Usage:
        class MyModel(BaseModel, SoftDeleteMixin):
            # ... other fields
            pass
    
    Query filtering:
        # Only active (non-deleted) records
        {"deleted_at": None}
        
        # Include deleted records
        {} or {"deleted_at": {"$ne": None}}
    """
    deleted_at: Optional[datetime] = None
    deleted_by: Optional[str] = None  # User ID who performed the deletion
    
    @property
    def is_deleted(self) -> bool:
        """Check if the record is soft-deleted"""
        return self.deleted_at is not None
    
    def soft_delete(self, user_id: Optional[str] = None) -> dict:
        """
        Soft delete the record.
        
        Args:
            user_id: Optional ID of the user performing the deletion
            
        Returns:
            dict: Update query for MongoDB
        """
        return {
            "$set": {
                "deleted_at": datetime.now(timezone.utc),
                "deleted_by": user_id,
                "updated_at": datetime.now(timezone.utc)
            }
        }
    
    def restore(self) -> dict:
        """
        Restore a soft-deleted record.
        
        Returns:
            dict: Update query for MongoDB
        """
        return {
            "$set": {
                "deleted_at": None,
                "deleted_by": None,
                "updated_at": datetime.now(timezone.utc)
            }
        }
    
    @staticmethod
    def active_filter() -> dict:
        """
        Returns a MongoDB filter for active (non-deleted) records.
        
        Returns:
            dict: MongoDB filter
        """
        return {"deleted_at": None}
    
    @staticmethod
    def deleted_filter() -> dict:
        """
        Returns a MongoDB filter for deleted records only.
        
        Returns:
            dict: MongoDB filter
        """
        return {"deleted_at": {"$ne": None}}
