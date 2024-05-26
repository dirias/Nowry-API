from typing import List
from fastapi import APIRouter, Depends, HTTPException
from pymongo.collection import Collection
from app.models.Book import Book
from app.config.database import books_collection

router = APIRouter(
    prefix="/book",
    tags=["books"],
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

@router.put("/edit/{book_id}", summary="Edit a book by ID", response_model=Book)
async def edit_book(
    book_id: str, book_data: Book, books_collection: Collection = Depends(get_books_collection)
):
    # Check if the book exists
    existing_book = await books_collection.find_one({"_id": book_id})
    if existing_book is None:
        raise HTTPException(status_code=404, detail="Book not found")

    # Update the book data
    await books_collection.update_one({"_id": book_id}, {"$set": book_data.dict()})

    return {"message": "Book updated successfully"}

@router.delete("/delete/{book_id}", summary="Delete a book by ID")
async def delete_book(book_id: str, books_collection: Collection = Depends(get_books_collection)):
    # Check if the book exists
    existing_book = await books_collection.find_one({"_id": book_id})
    if existing_book is None:
        raise HTTPException(status_code=404, detail="Book not found")

    # Delete the book
    await books_collection.delete_one({"_id": book_id})

    return {"message": "Book deleted successfully"}

@router.get("/search", summary="Search books by title", response_model=List[Book])
async def search_books(title: str, books_collection: Collection = Depends(get_books_collection)):
    # Search books by title (case-insensitive)
    cursor = books_collection.find({"title": {"$regex": title, "$options": "i"}})
    books = await cursor.to_list(length=100)  # Limit to 100 books for safety
    return books

@router.get("/all", summary="Get all books", response_model=List[Book])
async def get_all_books(books_collection: Collection = Depends(get_books_collection)):
    # Retrieve all books from the MongoDB collection
    cursor = books_collection.find({})
    books = await cursor.to_list(length=100)  # Limit to 100 books for safety
    return books