# main.py
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.routers import book_pages, books, users, sessions, cards

import secrets

SECRET_KEY = secrets.token_hex(32)

app = FastAPI()

origins = [
    "http://localhost:3000",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["*"],
)

app.include_router(book_pages.router)
app.include_router(books.router)
app.include_router(users.router)
app.include_router(sessions.router)
app.include_router(cards.router)

# Additional routes and middleware if needed
