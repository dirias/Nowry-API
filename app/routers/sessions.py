from fastapi import APIRouter, Depends, HTTPException, Form, Request
from fastapi.responses import JSONResponse
import jwt
import bcrypt
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.config.database import users_collection
from app.config.auth_config import SECRET_KEY, SECURE_COOKIE

router = APIRouter(
    prefix="/session",
    tags=["sessions"],
    responses={404: {"description": "Not found"}},
)


from pydantic import BaseModel


class LoginRequest(BaseModel):
    email: str
    password: str


@router.post("/login")
@Limiter(key_func=get_remote_address).limit("5/minute")
async def login(request: Request, credentials: LoginRequest):
    email = credentials.email
    password = credentials.password
    # Check if the user exists
    user = await users_collection.find_one({"email": email})

    if not user:
        raise HTTPException(status_code=400, detail="Invalid credentials")

    # Verify password
    if not bcrypt.checkpw(password.encode("utf-8"), user["password"].encode("utf-8")):
        raise HTTPException(status_code=400, detail="Invalid credentials")

    token = jwt.encode({"user_id": str(user["_id"])}, SECRET_KEY, algorithm="HS256")

    response = JSONResponse(
        content={
            "message": "Login successful",
            "username": user.get("username"),
            "role": user.get("role", "user"),
            # We don't send token in body anymore for security, or we can send it but client ignores it
        }
    )

    response.set_cookie(
        key="access_token",
        value=token,
        httponly=True,
        secure=SECURE_COOKIE,  # True in prod (HTTPS), False in dev (HTTP)
        samesite="lax",
        path="/",  # Explicitly set path to root
        max_age=60 * 60 * 24 * 7,  # 7 days
    )

    return response


@router.post("/logout")
async def logout():
    response = JSONResponse(content={"message": "Logout successful"})
    response.delete_cookie(
        key="access_token", httponly=True, secure=SECURE_COOKIE, samesite="lax"
    )
    return response


@router.post("/reset_password")
async def reset_password(email: str = Form(...), new_password: str = Form(...)):
    # Check if the user exists and update the password
    user = await users_collection.find_one({"email": email})
    if user is None:
        raise HTTPException(status_code=400, detail="User not found")

    # Hash new password
    hashed_password = bcrypt.hashpw(
        new_password.encode("utf-8"), bcrypt.gensalt()
    ).decode("utf-8")

    await users_collection.update_one(
        {"email": email}, {"$set": {"password": hashed_password}}
    )

    return {"message": "Password reset successful"}
