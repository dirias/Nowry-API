from fastapi import FastAPI, HTTPException, Depends, Form, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from motor.motor_asyncio import AsyncIOMotorClient
import secrets
import jwt

SECRET_KEY = secrets.token_hex(32)
app = FastAPI()

origins = [
    "http://localhost:3000",  # Add the origin of your React application
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# MongoDB configuration
MONGO_URI = "mongodb://mongodb:27017"
mongo_client = AsyncIOMotorClient(MONGO_URI)
db = mongo_client["mydb"]
users_collection = db["users"]
book_pages_collection = db["book_pages"]

class UserCreate(BaseModel):
    username: str
    email: str
    password: str

class BookPage(BaseModel):
    title: str
    page_number: int
    content: str  # This will store the formatted content using Quill.js


# Routes for user management
@app.post("/create_user", response_model=UserCreate)
async def create_user(user: UserCreate):
    # Check if the user already exists
    existing_user = await users_collection.find_one({"email": user.email})
    if existing_user:
        raise HTTPException(status_code=400, detail="User already exists")

    # Insert the new user into the database
    await users_collection.insert_one(user.dict())

    return user  # Return the UserCreate object directly

@app.post("/login")
async def login(email: str = Form(...), password: str = Form(...)):
    # Check if the user exists and the password is correct
    user = await users_collection.find_one({"email": email, "password": password})
    if user is None:
        raise HTTPException(status_code=400, detail="Invalid credentials")
    token = jwt.encode({"user_id": str(user["_id"])}, SECRET_KEY, algorithm="HS256")
    return {"message": "Login successful", "username": user.get('username'), "token": token}

@app.post("/reset_password")    
async def reset_password(email: str = Form(...), new_password: str = Form(...)):
    # Check if the user exists and update the password
    user = await users_collection.find_one({"email": email})
    if user is None:
        raise HTTPException(status_code=400, detail="User not found")

    await users_collection.update_one({"email": email}, {"$set": {"password": new_password}})

    return {"message": "Password reset successful"}

def get_current_user_authorization(request: Request, token: str = Header(None)):
    if token is None:
        raise HTTPException(status_code=401, detail="Token is missing")
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token has expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")
    
@app.post("/save_book_page")
async def save_book_page(book_page: BookPage, current_user: dict = Depends(get_current_user_authorization)):
    # Associate the book page with the user who is currently logged in
    book_page.username = current_user.get('username')

    # Insert the book page into the MongoDB collection
    await book_pages_collection.insert_one(book_page.dict())
    return {"message": "Book page saved successfully"}

@app.get("/get_book_page/{book_id}/{page_number}")
async def get_book_page(book_id: str, page_number: int):
    # Retrieve the book page by book_id and page_number from the MongoDB collection
    book_page = await book_pages_collection.find_one({"_id": book_id, "page_number": page_number})
    if book_page is None:
        raise HTTPException(status_code=404, detail="Page not found")
    return book_page


