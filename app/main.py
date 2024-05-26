# main.py
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.routers import book_pages, books, users, sessions

import secrets

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

app.include_router(book_pages.router)
app.include_router(books.router)
app.include_router(users.router)
app.include_router(sessions.router)

# Additional routes and middleware if needed
