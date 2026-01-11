"""
Authentication module for Firebase-based authentication.
All authentication now uses Firebase tokens.
"""
from .firebase_auth import get_firebase_user

__all__ = ['get_firebase_user']
