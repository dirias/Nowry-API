from fastapi import APIRouter, Depends, HTTPException
from pymongo.collection import Collection
from app.models.Book import Book
from app.config.database import books_collection

router = APIRouter(
    prefix="/book",
    tags=["items"],
    responses={404: {"description": "Not found"}},
)


# Define a dependency to access the books collection from MongoDB
def get_books_collection() -> Collection:
    # Assuming books_collection is defined in your MongoDB configuration
    return books_collection


@router.post("/create", summary="Create a new book", response_model=Book)
async def create_book(
    book: Book, books_collection: Collection = Depends(get_books_collection)
):
    result = await books_collection.insert_one(book.dict())
    inserted_id = str(result.inserted_id)
    return {"message": "Book created successfully", "book_id": inserted_id}
