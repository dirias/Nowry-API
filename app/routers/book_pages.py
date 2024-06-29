from fastapi import APIRouter, Depends, HTTPException
from app.models.Page import Page
from bson import ObjectId
from app.config.database import book_pages_collection, books_collection
from pymongo.collection import Collection
from app.auth import get_current_user_authorization

router = APIRouter(
    prefix="/book_page",
    tags=["book pages"],
    responses={404: {"description": "Not found"}},
)


def get_page_collection() -> Collection:
    # Assuming books_collection is defined in your MongoDB configuration
    return book_pages_collection


def get_book_collection() -> Collection:
    # Assuming books_collection is defined in your MongoDB configuration
    return books_collection


@router.post("/save_book_page", response_model=Page)
async def save_book_page(
    book_page: Page,
    book_pages_collection: Collection = Depends(get_page_collection),
    books_collection: Collection = Depends(get_book_collection),
):
    # Insert the book page into the MongoDB collection
    result = await book_pages_collection.insert_one(book_page.dict(by_alias=True))

    await books_collection.update_one(
        {"_id": ObjectId(book_page.book_id)}, {"$push": {"pages": result.inserted_id}}
    )

    return book_page


@router.get("/get_book_page/{book_id}/{page_number}", response_model=Page)
async def get_book_page(book_id: str, page_number: int):
    # Retrieve the book page by book_id and page_number from the MongoDB collection
    book_page = await book_pages_collection.find_one(
        {"_id": book_id, "page_number": page_number}
    )
    if book_page is None:
        raise HTTPException(status_code=404, detail="Page not found")
    return book_page
