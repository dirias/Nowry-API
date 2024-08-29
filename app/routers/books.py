from datetime import datetime
from typing import List
from fastapi import APIRouter, Depends, HTTPException
from pymongo.collection import Collection
from bson import ObjectId
from app.models.Book import Book
from app.models.Page import Page
from app.config.database import books_collection, book_pages_collection

from ..routers.book_pages import save_book_page

router = APIRouter(
    prefix="/book",
    tags=["books"],
    responses={404: {"description": "Not found"}},
)


# Define a dependency to access the books collection from MongoDB
def get_books_collection() -> Collection:
    # Assuming books_collection is defined in your MongoDB configuration
    return books_collection


def get_page_collection() -> Collection:
    # Assuming books_collection is defined in your MongoDB configuration
    return book_pages_collection


@router.post("/create", summary="Create a new book", response_model=Book)
async def create_book(
    book: Book, books_collection: Collection = Depends(get_books_collection)
):
    new_book = await books_collection.insert_one(book.dict(by_alias=True))
    if new_book:
        await save_book_page(
            Page(book_id=str(book.id)), book_pages_collection, books_collection
        )
        # raise error in case
    return book


@router.put("/edit/{book_id}", summary="Edit a book by ID", response_model=Book)
async def edit_book(
    book_id: str,
    book_data: Book,
    books_collection: Collection = Depends(get_books_collection),
):
    # Check if the book exists
    existing_book = await books_collection.find_one({"_id": ObjectId(book_id)})
    if existing_book is None:
        raise HTTPException(status_code=404, detail="Book not found")

    # Update the book data
    res = await books_collection.update_one(
        {"_id": ObjectId(book_id)},
        {
            "$set": {
                "title": book_data.title,
                "updated_at": datetime.now(),
                "summary": book_data.summary,
                "cover_color": book_data.cover_color,
                "tags": book_data.tags,
            }
        },
    )

    if res.modified_count == 0:
        raise HTTPException(
            status_code=404, detail="Book update failed or no changes were made"
        )

    return {"message": "Book updated successfully"}


@router.delete("/delete/{book_id}", summary="Delete a book by ID")
async def delete_book(
    book_id: str,
    books_collection: Collection = Depends(get_books_collection),
    pages_collection: Collection = Depends(get_page_collection),
):
    # Check if the book exists
    existing_book = await books_collection.find_one({"_id": ObjectId(book_id)})
    if existing_book is None:
        raise HTTPException(status_code=404, detail="Book not found")

    # Delete all pages associated with the book
    deleted_pages_result = await pages_collection.delete_many({"book_id": book_id})
    # Delete the book
    deleted_book_result = await books_collection.delete_one({"_id": ObjectId(book_id)})

    if deleted_book_result.deleted_count == 1:
        return {
            "message": "Book and associated pages deleted successfully",
            "deleted_pages_count": deleted_pages_result.deleted_count,
        }
    else:
        raise HTTPException(status_code=500, detail="Error deleting the book")


@router.get("/search", summary="Search books by title", response_model=List[Book])
async def search_books(
    title: str, books_collection: Collection = Depends(get_books_collection)
):
    # Search books by title (case-insensitive)
    cursor = books_collection.find({"title": {"$regex": title, "$options": "i"}})
    books = await cursor.to_list(length=100)  # Limit to 100 books for safety
    return books


@router.get("/all", summary="Get all books", response_model=List[Book])
async def get_all_books(books_collection: Collection = Depends(get_books_collection)):
    # Retrieve all books from the MongoDB collection
    cursor = books_collection.find({})
    books = await cursor.to_list(length=100)  # Limit to 100 books for safety
    for book in books:
        book["_id"] = str(book["_id"])  # TODO: Improve to avoid this loop
        book["pages"] = [str(page) for page in book["pages"]]
    return books


@router.get("/{book_id}")
async def get_book_by_id(
    book_id: str,
    books_collection: Collection = Depends(get_books_collection),
    book_pages_collection: Collection = Depends(get_page_collection),
):
    # Find the book by its ID in the MongoDB collection
    print("testing")
    book = await books_collection.find_one({"_id": ObjectId(book_id)})
    if book:
        book["_id"] = str(book["_id"])

        # Retrieve all pages related to this book
        cursor = book_pages_collection.find({"book_id": book_id})
        pages = await cursor.to_list(length=50)  # Limit to 100 pages for safety

        # Convert ObjectId to str for _id in pages
        for page in pages:
            page["_id"] = str(page["_id"])

        # Add pages to the book dictionary
        book["pages"] = pages
        return book
    else:
        raise HTTPException(status_code=404, detail="Book not found")
