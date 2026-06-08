import os
from datetime import datetime, timezone
from typing import List, Optional
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, UploadFile, File, Form
from pymongo.collection import Collection
from bson import ObjectId
from app.models.Book import Book, BookSummary
from app.models.ai_expand import AIExpandRequest, AIExpandResponse
from app.config.database import books_collection
from app.auth.firebase_auth import get_firebase_user
from app.auth.dependencies import require_ownership, track_ai_usage
from app.utils.logger import get_logger
from app.core.model_config import get_client_for_tier
from app.core import prompt_manager

logger = get_logger(__name__)

_GROQ_MODEL: str = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")


router = APIRouter(
    prefix="/book",
    tags=["books"],
    dependencies=[Depends(get_firebase_user)],
    responses={404: {"description": "Not found"}},
)


# Define a dependency to access the books collection from MongoDB
def get_books_collection() -> Collection:
    # Assuming books_collection is defined in your MongoDB configuration
    return books_collection




@router.post("/create", summary="Create a new book", response_model=Book)
async def create_book(
    book: Book,
    books_collection: Collection = Depends(get_books_collection),
    current_user: dict = Depends(get_firebase_user),
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
        current_book_count = await books_collection.count_documents({
            "user_id": user_id,
            "deleted_at": None  # Only count non-deleted books
        })
        if current_book_count >= book_limit:
            raise HTTPException(
                status_code=403,
                detail=f"Book limit reached for {plan['name']} plan. Upgrade to create more books.",
            )
    # --------------------------------
    
    logger.info(f"Creating book: {book.title}")
    # Exclude _id to let MongoDB generate it as ObjectId
    book_dict = book.model_dump(by_alias=True, exclude={'id'})

    new_book = await books_collection.insert_one(book_dict)
    book_id = str(new_book.inserted_id)
    logger.info(f"Book inserted with ID: {book_id}")

    # We no longer create a "first page" as the book uses full_content now.

    # Fetch and return the created book
    created_book = await books_collection.find_one({"_id": new_book.inserted_id})

    if created_book:
        created_book["_id"] = str(created_book["_id"])
        return created_book

    logger.error(f"Book was inserted (ID: {book_id}) but not found in database after insert")
    raise HTTPException(status_code=500, detail="Failed to create book")


@router.put("/edit/{book_id}", summary="Edit a book by ID", response_model=Book)
async def edit_book(
    book_id: str,
    book_data: Book,
    background_tasks: BackgroundTasks,
    books_collection: Collection = Depends(get_books_collection),
    existing_book: dict = Depends(require_ownership(get_books_collection, "book_id")),
    current_user: dict = Depends(get_firebase_user),
):
    # Update the book data using partial update (exclude_unset=True)
    update_data = book_data.model_dump(exclude_unset=True)
    
    # Remove immutable/system fields that shouldn't be updated by user.
    # forked_from is permanently set at fork time and must never be overwritten.
    fields_to_remove = ["id", "_id", "user_id", "created_at", "forked_from"]
    for field in fields_to_remove:
        update_data.pop(field, None)
    
    if not update_data:
        raise HTTPException(status_code=400, detail="No data provided for update")

    # Add updated_at timestamp
    update_data["updated_at"] = datetime.now()

    res = await books_collection.update_one(
        {"_id": ObjectId(existing_book["_id"]) if len(existing_book["_id"]) == 24 else existing_book["_id"]},
        {"$set": update_data},
    )

    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="Book not found")

    # Fetch and return the updated book
    updated_book = await books_collection.find_one({"_id": ObjectId(existing_book["_id"]) if len(existing_book["_id"]) == 24 else existing_book["_id"]})
    if updated_book:
        updated_book["_id"] = str(updated_book["_id"])

        # Trigger background RAG indexing if content changed
        if "full_content" in update_data and update_data["full_content"]:
            from app.utils.book_rag import index_book
            user_id: str = current_user["uid"]
            background_tasks.add_task(
                index_book,
                book_id=str(existing_book["_id"]),
                user_id=user_id,
                raw_content=update_data["full_content"],
            )

        return updated_book
    
    raise HTTPException(status_code=500, detail="Error fetching updated book")


@router.delete("/delete/{book_id}", summary="Soft delete a book by ID", status_code=204)
async def delete_book(
    book_id: str,
    books_collection: Collection = Depends(get_books_collection),
    book: dict = Depends(require_ownership(get_books_collection, "book_id")),
):
    """
    Soft delete a book (sets deleted_at timestamp).
    Also auto-unpublishes if the book was public.
    """
    now = datetime.now(timezone.utc)

    try:
        # Soft delete + auto-unpublish
        result = await books_collection.update_one(
            {"_id": ObjectId(book["_id"]) if len(book["_id"]) == 24 else book["_id"]},
            {
                "$set": {
                    "deleted_at": now,
                    "deleted_by": book.get("user_id"),
                    "is_public": False,  # Auto-unpublish
                    "updated_at": now
                }
            }
        )

        if result.modified_count > 0:
            logger.info(f"Book soft-deleted successfully: {book_id}")
            return None

    except Exception as e:
        logger.error(f"Error soft-deleting book {book_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Error deleting book")

    raise HTTPException(status_code=404, detail="Book not found")


@router.get("/search", summary="Search books by title", response_model=List[BookSummary])
async def search_books(
    title: str,
    books_collection: Collection = Depends(get_books_collection),
    current_user: dict = Depends(get_firebase_user),
):
    user_id = current_user.get("user_id")
    import re
    safe_title = re.escape(title)
    
    # Search books by title (case-insensitive), excluding full_content for performance, and locked to the user
    cursor = books_collection.find(
        {"title": {"$regex": safe_title, "$options": "i"}, "user_id": user_id, "deleted_at": None},
        {"full_content": 0}
    )
    books = await cursor.to_list(length=100)  # Limit to 100 books for safety
    return books


@router.get("/all", summary="Get all books", response_model=List[BookSummary])
async def get_all_books(
    books_collection: Collection = Depends(get_books_collection),
    current_user: dict = Depends(get_firebase_user),
):
    user_id = current_user.get("user_id")
    
    # Debug: Check total books for this user (including deleted)
    total_count = await books_collection.count_documents({"user_id": user_id})
    logger.debug(f"Total books for user {user_id}: {total_count}")

    # Retrieve all books for the current user that are NOT soft-deleted
    # Exclude full_content significantly improves performance for large documents
    cursor = books_collection.find(
        {
            "user_id": user_id,
            "deleted_at": None  # Only books where deleted_at is None
        },
        {"full_content": 0}
    )
    books = await cursor.to_list(length=100)  # Limit to 100 books for safety

    for book in books:
        book["_id"] = str(book["_id"])
    
    return books


@router.get("/{book_id}", response_model=Book)
async def get_book_by_id(
    book: dict = Depends(require_ownership(get_books_collection, "book_id")),
):
    return book


@router.post("/import", summary="Import a book from file (PDF, DOCX, TXT)")
async def import_book_from_file(
    file: UploadFile = File(...),
    username: str = Form("Unknown"),
    preview: bool = Form(False),  # Preview mode for validation
    title: Optional[str] = Form(None), # Optional title override
    books_collection: Collection = Depends(get_books_collection),
    current_user: dict = Depends(get_firebase_user),
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

    # Create book title from filename or use provided title
    book_title = title if title and title.strip() else filename.rsplit(".", 1)[0]

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

    # SAVE MODE: Create the book with full_content
    # Convert extracted pages to clean JSON format (Content-First)
    from app.utils.html_to_lexical import html_to_lexical_json
    import json
    
    # Combine all pages into single HTML
    combined_html = "\n".join([p.get("content", "") for p in extracted_pages])
    
    # Convert HTML to Lexical JSON
    try:
        lexical_json = html_to_lexical_json(combined_html)
        full_content = json.dumps(lexical_json)
        logger.info(f"Converted import to JSON format: {len(lexical_json['root']['children'])} blocks")
    except Exception as e:
        logger.warning(f"HTML conversion failed, using simple fallback: {e}")
        from app.utils.html_to_lexical import simple_html_to_lexical
        lexical_json = simple_html_to_lexical(combined_html)
        full_content = json.dumps(lexical_json)

    new_book = Book(
        title=book_title,
        author=username,
        user_id=current_user.get("user_id"),
        isbn="Importado",
        summary=f"Imported from {filename} - {len(extracted_pages)} pages",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        full_content=full_content,
        cover_color="#4A90E2",
    )

    # Exclude 'id' so MongoDB generates a proper ObjectId, identifying this as a new document
    book_dict = new_book.model_dump(by_alias=True, exclude={'id'})
    result = await books_collection.insert_one(book_dict)
    book_id = str(result.inserted_id)

    # Return the book info
    return {
        "_id": book_id,
        "title": book_title,
        "author": username,
        "page_count": len(extracted_pages), # Keep count for meta info
        "extraction_quality": {
            "total_words": metadata.get("total_words", 0),
            "multi_column_pages": metadata.get("multi_column_pages", 0),
            "warnings": warnings,
        },
    }


@router.post("/{book_id}/ai-expand", response_model=AIExpandResponse)
async def ai_expand_text(
    book_id: str,
    body: AIExpandRequest,
    current_user: dict = Depends(track_ai_usage),
) -> AIExpandResponse:
    """Expand selected text using tier-appropriate LLM. All tiers have access; model quality differs."""
    user_id: str = current_user.get("user_id", "")
    tier: str = current_user.get("subscription", {}).get("tier", "free")
    if tier not in ("free", "plus", "pro"):
        tier = "free"

    # Ownership check
    try:
        book = await books_collection.find_one({"_id": ObjectId(book_id), "deleted_at": None})
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid book ID.")
    if not book or book.get("user_id") != user_id:
        raise HTTPException(status_code=404, detail="Book not found.")

    # Character limits: Free=500, Plus=2000, Pro=unlimited
    char_limits: dict = {"free": 500, "plus": 2000}
    limit = char_limits.get(tier)
    if limit and len(body.selected_text) > limit:
        raise HTTPException(
            status_code=400,
            detail=f"Selected text exceeds the {limit}-character limit for your plan.",
        )

    llm_client = get_client_for_tier(tier)
    if llm_client is None:
        raise HTTPException(status_code=503, detail="AI service unavailable. API key not configured.")

    system_prompt = prompt_manager.get_prompt("nowry-book-expand")
    user_prompt = (
        f"Instruction: {body.instruction}\n\n"
        f"Text to expand:\n{body.selected_text}"
    )

    raw_text: str = ""
    last_exc = None
    for attempt in range(1, 3):
        try:
            if tier == "free":
                completion = llm_client.chat.completions.create(
                    model=_GROQ_MODEL,
                    max_tokens=1500,
                    temperature=0.7,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                )
                raw_text = (completion.choices[0].message.content or "").strip()
            else:
                combined_prompt = f"{system_prompt}\n\n{user_prompt}"
                completion = llm_client.request(combined_prompt)
                raw_text = (completion.choices[0].message.content or "").strip()

            if raw_text:
                break
            logger.warning(f"[ai_expand] LLM returned empty on attempt {attempt} (tier={tier}).")
        except Exception as exc:
            last_exc = exc
            logger.warning(f"[ai_expand] LLM API error on attempt {attempt} (tier={tier}): {exc}")

    if not raw_text:
        logger.error(f"[ai_expand] Failed after retries. last_exc={last_exc}")
        raise HTTPException(status_code=502, detail="AI service error. Please try again.")

    return AIExpandResponse(expanded_text=raw_text)
