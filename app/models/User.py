from pydantic import BaseModel
from typing import Optional


class User(BaseModel):
    firebase_uid: str  # Reference to Firebase Auth user
    username: str
    email: str
    # password: str  # REMOVED - Firebase handles authentication
