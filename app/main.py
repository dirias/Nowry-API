from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.routers import (
    book_pages,
    books,
    users,
    sessions,
    cards,
    study_cards,
    tasks,
    decks,
    quizzes,
    visualizer,
)

load_dotenv()

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
    allow_headers=["*"],
)

app.include_router(book_pages.router)
app.include_router(books.router)
app.include_router(users.router)
app.include_router(sessions.router)
app.include_router(cards.router)
app.include_router(study_cards.router)
app.include_router(decks.router)
app.include_router(tasks.router)
app.include_router(quizzes.router)
app.include_router(visualizer.router)
