# app/config/auth_config.py
import logging
import os
from dotenv import load_dotenv
from fastapi import HTTPException, Header
import jwt

from app.config.environment import is_production

load_dotenv()

logger = logging.getLogger(__name__)

# Obviously-a-dev-value fallback — never used in production (see below).
_DEV_ONLY_SECRET_KEY = "DEV-ONLY-INSECURE-nowry-secret-key-do-not-use-in-prod"

_env_secret_key = os.getenv("SECRET_KEY", "").strip()

if is_production():
    if not _env_secret_key:
        raise RuntimeError(
            "SECRET_KEY environment variable is not set. Refusing to start "
            "in production with no signing key. Generate one with: "
            "openssl rand -hex 32"
        )
    SECRET_KEY = _env_secret_key
else:
    if _env_secret_key:
        SECRET_KEY = _env_secret_key
    else:
        logger.warning(
            "SECRET_KEY is not set — falling back to an insecure DEV-ONLY "
            "signing key. This is only safe for local development. Set "
            "SECRET_KEY before deploying (e.g. `openssl rand -hex 32`)."
        )
        SECRET_KEY = _DEV_ONLY_SECRET_KEY

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30

# Secure cookie is required for production (HTTPS), but disabled for dev (HTTP)
SECURE_COOKIE = is_production()
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
