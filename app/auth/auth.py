from fastapi import HTTPException, Header, Request
import jwt
from app.config.auth_config import SECRET_KEY


def get_current_user_authorization(request: Request):
    # Try to get token from cookie first (HttpOnly)
    token = request.cookies.get("access_token")

    # Fallback to Authorization header if cookie not present
    if not token:
        auth_header = request.headers.get("Authorization")
        if auth_header:
            token = auth_header.replace("Bearer ", "")

    if token is None:
        raise HTTPException(status_code=401, detail="Token is missing")

    # Clean up token if needed (though cookie shouldn't have Bearer prefix)
    if token.startswith("Bearer "):
        token = token.replace("Bearer ", "")

    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token has expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")
