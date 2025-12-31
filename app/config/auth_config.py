# app/config/auth_config.py
import os
from dotenv import load_dotenv
from fastapi import HTTPException, Header
import jwt

load_dotenv()

# Use a fixed secret key from environment or a consistent fallback for development
SECRET_KEY = os.getenv("SECRET_KEY", "nowry_super_secret_key_12345")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30

# Secure cookie is required for production (HTTPS), but disabled for dev (HTTP)
# Secure cookie is required for production (HTTPS), but disabled for dev (HTTP)
SECURE_COOKIE = os.getenv("ENV") == "production"
SAMESITE_COOKIE = "none" if SECURE_COOKIE else "lax"


def get_current_user_authorization(authorization: str = Header(None)):
    """
    Extract and validate JWT token from Authorization header.
    Returns the decoded token payload with user_id.
    """
    if not authorization:
        raise HTTPException(status_code=401, detail="Authorization header missing")

    # Check if it's a Bearer token
    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(
            status_code=401, detail="Invalid authorization header format"
        )

    token = parts[1]

    try:
        # Decode the JWT token
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = payload.get("user_id")

        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid token payload")

        return {"user_id": user_id}
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token has expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")
