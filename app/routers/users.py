from fastapi import APIRouter, Depends, HTTPException
from pymongo.collection import Collection
from app.models.User import User
from app.config.database import users_collection

router = APIRouter(
    prefix="/user",
    tags=["users"],
    responses={404: {"description": "Not found"}},
)


# Routes for user management
@router.post("/create_user", response_model=User)
async def create_user(user: User):
    # Check if the user already exists
    existing_user = await users_collection.find_one({"email": user.email})
    if existing_user:
        raise HTTPException(status_code=400, detail="User already exists")

    # Insert the new user into the database
    await users_collection.insert_one(user.dict())

    return user  # Return the UserCreate object directly
