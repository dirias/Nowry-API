from datetime import datetime
from typing import List
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from pymongo.collection import Collection
from bson import ObjectId
from app.models.Book import Book
from app.models.Page import Page
from app.config.database import books_collection, book_pages_collection
from app.auth.auth import get_current_user_authorization

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
    book: Book,
    books_collection: Collection = Depends(get_books_collection),
    current_user: dict = Depends(get_current_user_authorization),
):
    # Set user_id from token
    user_id = current_user.get("user_id")
    book.user_id = user_id

    # --- Subscription Limit Check ---
    from app.config.database import users_collection
    from app.config.subscription_plans import SUBSCRIPTION_PLANS, SubscriptionTier

    # Get user subscription
    user = await users_collection.find_one({"_id": ObjectId(user_id)})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    subscription_data = user.get("subscription", {"tier": "free"})
    tier_key = subscription_data.get("tier", "free")
    plan = SUBSCRIPTION_PLANS.get(tier_key, SUBSCRIPTION_PLANS[SubscriptionTier.FREE])

    book_limit = plan["limits"]["books"]

    # Check limit if not unlimited (-1)
    if book_limit != -1:
        current_book_count = await books_collection.count_documents(
            {"user_id": user_id}
        )
        if current_book_count >= book_limit:
            raise HTTPException(
                status_code=403,
                detail=f"Book limit reached for {plan['name']} plan. Upgrade to create more books.",
            )
    # --------------------------------

    new_book = await books_collection.insert_one(book.dict(by_alias=True))
    if new_book:
        await save_book_page(
            Page(book_id=str(book.id)),
            book_pages_collection,
            books_collection,
            current_user=current_user,
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
                "cover_image": book_data.cover_image,
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
async def get_all_books(
    books_collection: Collection = Depends(get_books_collection),
    current_user: dict = Depends(get_current_user_authorization),
):
    user_id = current_user.get("user_id")
    # Retrieve all books for the current user
    cursor = books_collection.find({"user_id": user_id})
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


@router.post("/import", summary="Import a book from file (PDF, DOCX, TXT)")
async def import_book_from_file(
    file: UploadFile = File(...),
    username: str = Form("Unknown"),
    preview: bool = Form(False),  # Preview mode for validation
    books_collection: Collection = Depends(get_books_collection),
    pages_collection: Collection = Depends(get_page_collection),
    current_user: dict = Depends(get_current_user_authorization),
):
    """
    Import a book from an uploaded file.

    - **preview=true**: Returns extraction preview with quality metrics for validation
    - **preview=false**: Creates and saves the book with all pages

    Preserves formatting, multi-column layouts, and provides quality metrics.
    """
    from app.utils.file_import import process_uploaded_file

    # Read file content
    file_content = await file.read()
    filename = file.filename

    # Extract pages from file
    extracted_pages = process_uploaded_file(filename, file_content)

    if not extracted_pages:
        raise HTTPException(
            status_code=400, detail="Failed to extract content from file"
        )

    # Create book title from filename
    book_title = filename.rsplit(".", 1)[0]  # Remove extension

    # Get extraction metadata
    metadata = extracted_pages[0].get("extraction_metadata", {})

    # Calculate quality warnings
    warnings = []
    avg_words_per_page = (
        metadata.get("total_words", 0) / len(extracted_pages) if extracted_pages else 0
    )

    if avg_words_per_page < 100:
        warnings.append("Low word count detected - extraction may be incomplete")

    if metadata.get("multi_column_pages", 0) > 0:
        info_msg = (
            f"{metadata['multi_column_pages']} pages have 2-column layout (preserved)"
        )
    else:
        info_msg = "Single column layout detected"

    # PREVIEW MODE: Return extraction data for validation
    if preview:
        return {
            "preview": True,
            "title": book_title,
            "total_pages": len(extracted_pages),
            "metadata": metadata,
            "info": info_msg,
            "warnings": warnings,
            "file_info": {
                "original_filename": filename,
                "type": (
                    filename.rsplit(".", 1)[-1].upper()
                    if "." in filename
                    else "Unknown"
                ),
                "size": len(file_content),  # File size in bytes
            },
            "sample_pages": [
                {
                    "page_number": p.get("page_number"),
                    "title": p.get("title"),
                    "content_preview": (
                        p.get("content", "")[:5000] + "..."
                        if len(p.get("content", "")) > 5000
                        else p.get("content", "")
                    ),  # Show full page
                    "word_count": p.get("word_count"),
                    "has_columns": p.get("has_columns", False),
                    "quality_score": p.get("quality_score", 0),
                }
                for p in extracted_pages  # ALL pages for preview
            ],
            "quality_summary": {
                "total_words": metadata.get("total_words", 0),
                "total_chars": metadata.get("total_chars", 0),
                "avg_words_per_page": int(avg_words_per_page),
                "has_multi_column": metadata.get("multi_column_pages", 0) > 0,
            },
        }

    # SAVE MODE: Create the book and pages
    new_book = Book(
        title=book_title,
        author=username,
        user_id=current_user.get("user_id"),
        isbn="Importado",
        summary=f"Imported from {filename} - {len(extracted_pages)} pages",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        pages=[],
        cover_color="#4A90E2",
    )

    book_dict = new_book.dict(by_alias=True)
    result = await books_collection.insert_one(book_dict)
    book_id = str(result.inserted_id)

    # Create pages for the book
    created_pages = []
    for page_data in extracted_pages:
        page = Page(
            book_id=book_id,
            title=page_data["title"],
            content=page_data["content"],
            page_number=page_data.get("page_number", len(created_pages) + 1),
            created_at=datetime.now(),
            updated_at=datetime.now(),
        )

        page_dict = page.dict(by_alias=True, exclude={"id"})
        page_result = await pages_collection.insert_one(page_dict)
        page_dict["_id"] = str(page_result.inserted_id)
        created_pages.append(page_dict)

    # Return the book with pages and quality info
    return {
        "_id": book_id,
        "title": book_title,
        "author": username,
        "pages": created_pages,
        "page_count": len(created_pages),
        "extraction_quality": {
            "total_words": metadata.get("total_words", 0),
            "multi_column_pages": metadata.get("multi_column_pages", 0),
            "warnings": warnings,
        },
    }
