from pydantic import BaseModel
from .mixins import SoftDeleteMixin
from typing import Optional
from datetime import datetime


class User(BaseModel, SoftDeleteMixin):
    firebase_uid: str  # Reference to Firebase Auth user
    username: str
    email: str
    role: Optional[str] = "user"  # user, dev, admin
    is_beta: Optional[bool] = False  # Beta tester flag
    # password: str  # REMOVED - Firebase handles authentication
