from fastapi import APIRouter, Depends, HTTPException
from app.models.Page import Page
from app.config.database import book_pages_collection
from app.auth import get_current_user_authorization

router = APIRouter(
    prefix="/book_page",
    tags=["book pages"],
    responses={404: {"description": "Not found"}},
)


@router.post("/save_book_page", response_model=Page)
async def save_book_page(
    book_page: Page, current_user: dict = Depends(get_current_user_authorization)
):
    # Associate the book page with the user who is currently logged in
    book_page.username = current_user.get("username")

    # Insert the book page into the MongoDB collection
    await book_pages_collection.insert_one(book_page.dict())
    return {"message": "Book page saved successfully"}


@router.get("/get_book_page/{book_id}/{page_number}", response_model=Page)
async def get_book_page(book_id: str, page_number: int):
    # Retrieve the book page by book_id and page_number from the MongoDB collection
    book_page = await book_pages_collection.find_one(
        {"_id": book_id, "page_number": page_number}
    )
    if book_page is None:
        raise HTTPException(status_code=404, detail="Page not found")
    return book_page
