from fastapi import HTTPException, Header, Request
import jwt
import secrets

SECRET_KEY = secrets.token_hex(32)


def get_current_user_authorization(request: Request):
    if request.headers.get("Authorization") is None:
        raise HTTPException(status_code=401, detail="Token is missing")
    try:
        payload = jwt.decode(
            request.headers.get("Authorization"), SECRET_KEY, algorithms=["HS256"]
        )
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token has expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")
