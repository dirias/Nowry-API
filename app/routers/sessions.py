from fastapi import APIRouter, Depends, HTTPException, Form
import jwt
from app.config.database import users_collection
import secrets

SECRET_KEY = secrets.token_hex(32)

router = APIRouter(
    prefix="/session",
    tags=["sessions"],
    responses={404: {"description": "Not found"}},
)


@router.post("/login")
async def login(email: str = Form(...), password: str = Form(...)):
    # Check if the user exists and the password is correct
    user = await users_collection.find_one({"email": email, "password": password})
    if user is None:
        raise HTTPException(status_code=400, detail="Invalid credentials")
    token = jwt.encode({"user_id": str(user["_id"])}, SECRET_KEY, algorithm="HS256")
    return {
        "message": "Login successful",
        "username": user.get("username"),
        "token": token,
    }


@router.post("/reset_password")
async def reset_password(email: str = Form(...), new_password: str = Form(...)):
    # Check if the user exists and update the password
    user = await users_collection.find_one({"email": email})
    if user is None:
        raise HTTPException(status_code=400, detail="User not found")

    await users_collection.update_one(
        {"email": email}, {"$set": {"password": new_password}}
    )

    return {"message": "Password reset successful"}
