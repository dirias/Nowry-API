from datetime import datetime
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from pymongo.collection import Collection
from bson import ObjectId
from app.models.Book import Book, BookSummary
from app.config.database import books_collection
from app.auth.firebase_auth import get_firebase_user

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
    
    print(f"[DEBUG CREATE] Attempting to insert book: {book.title}")
    # Exclude _id to let MongoDB generate it as ObjectId
    book_dict = book.dict(by_alias=True, exclude={'id'})
    print(f"[DEBUG CREATE] Book dict keys: {book_dict.keys()}")
    
    new_book = await books_collection.insert_one(book_dict)
    book_id = str(new_book.inserted_id)
    print(f"[DEBUG CREATE] Book inserted with ID: {book_id}")
    print(f"[DEBUG CREATE] Insert result acknowledged: {new_book.acknowledged}")
    
    print(f"[DEBUG CREATE] Insert result acknowledged: {new_book.acknowledged}")
    
    # We no longer create a "first page" as the book uses full_content now.
    
    # Fetch and return the created book
    print(f"[DEBUG CREATE] Fetching created book from database...")
    created_book = await books_collection.find_one({"_id": new_book.inserted_id})
    print(f"[DEBUG CREATE] Created book found in DB: {created_book is not None}")
    
    if created_book:
        created_book["_id"] = str(created_book["_id"])
        print(f"[DEBUG CREATE] Returning book: {created_book['_id']}")
        return created_book
    
    print(f"[DEBUG CREATE] ERROR: Book was inserted but not found in database!")
    raise HTTPException(status_code=500, detail="Failed to create book")


@router.put("/edit/{book_id}", summary="Edit a book by ID", response_model=Book)
async def edit_book(
    book_id: str,
    book_data: Book,
    books_collection: Collection = Depends(get_books_collection),
):
    # Check if the book exists (Try both ObjectId and String ID)
    query = {"_id": ObjectId(book_id)}
    existing_book = await books_collection.find_one(query)
    
    if existing_book is None:
        # Fallback to string ID
        query = {"_id": book_id}
        existing_book = await books_collection.find_one(query)
        
    if existing_book is None:
        raise HTTPException(status_code=404, detail="Book not found")

    # Update the book data using partial update (exclude_unset=True)
    update_data = book_data.dict(exclude_unset=True)
    
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
        query, # Use the query that successfully found the book
        {"$set": update_data},
    )

    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="Book not found")

    # Fetch and return the updated book
    # Fetch and return the updated book
    updated_book = await books_collection.find_one(query)
    if updated_book:
        updated_book["_id"] = str(updated_book["_id"])
        return updated_book
    
    raise HTTPException(status_code=500, detail="Error fetching updated book")


@router.delete("/delete/{book_id}", summary="Soft delete a book by ID")
async def delete_book(
    book_id: str,
    books_collection: Collection = Depends(get_books_collection),
    current_user: dict = Depends(get_firebase_user),
):
    """
    Soft delete a book (sets deleted_at timestamp).
    Also auto-unpublishes if the book was public.
    """
    from datetime import datetime
    
    user_id = current_user.get("user_id")
    
    # Try ObjectId first
    try:
        obj_id = ObjectId(book_id)
        
        # Check ownership and existence
        book = await books_collection.find_one({
            "_id": obj_id,
            "user_id": user_id
        })
        
        if not book:
            raise HTTPException(status_code=404, detail="Book not found or not authorized")
        
        # Soft delete + auto-unpublish
        result = await books_collection.update_one(
            {"_id": obj_id},
            {
                "$set": {
                    "deleted_at": datetime.utcnow(),
                    "deleted_by": user_id,
                    "is_public": False,  # Auto-unpublish
                    "updated_at": datetime.utcnow()
                }
            }
        )
        
        if result.modified_count > 0:
            print(f"[DEBUG DELETE] Book soft-deleted successfully: {book_id}")
            return {"message": "Book deleted successfully"}
        
    except Exception as e:
        print(f"[DEBUG DELETE] Error: {e}")
        raise HTTPException(status_code=500, detail="Error deleting book")
    
    raise HTTPException(status_code=404, detail="Book not found")


@router.get("/search", summary="Search books by title", response_model=List[BookSummary])
async def search_books(
    title: str, books_collection: Collection = Depends(get_books_collection)
):
    # Search books by title (case-insensitive), excluding full_content for performance
    cursor = books_collection.find(
        {"title": {"$regex": title, "$options": "i"}},
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
    print(f"[DEBUG GET_ALL] Total books for user {user_id}: {total_count}")
    
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
    
    print(f"[DEBUG GET_ALL] Non-deleted books: {len(books)}")
    for book in books:
        print(f"[DEBUG GET_ALL] - Book: {book.get('_id')} | Title: {book.get('title')} | deleted_at: {book.get('deleted_at')}")
        book["_id"] = str(book["_id"])
    
    return books


@router.get("/{book_id}", response_model=Book)
async def get_book_by_id(
    book_id: str,
    books_collection: Collection = Depends(get_books_collection),
):
    # Find the book by its ID in the MongoDB collection
    # Find the book by its ID in the MongoDB collection
    print(f"[DEBUG] Searching for book with ID: {book_id} (Code Version: Fallback-Enabled)")
    object_id = None
    try:
        object_id = ObjectId(book_id)
        print(f"[DEBUG] Converted to ObjectId: {object_id}")
    except Exception as e:
        print(f"[DEBUG] '{book_id}' is not a valid ObjectId: {e}")
    
    book = None
    if object_id:
        book = await books_collection.find_one({"_id": object_id})
    
    if not book:
        print(f"[DEBUG] Book not found by ObjectId. Trying String ID: {book_id}")
        book = await books_collection.find_one({"_id": book_id})

    print(f"[DEBUG] Book found: {book is not None}")
    
    if not book:
        # DB Dump for debugging
        print(f"[DEBUG] --- START DB DUMP (First 20) ---")
        try:
            all_books_cursor = books_collection.find({}, {"_id": 1, "title": 1})
            all_books = await all_books_cursor.to_list(length=20)
            for b in all_books:
                # Print repr to see types clearly (ObjectId(...) vs 'string')
                print(f" - ID: {repr(b['_id'])} | Title: {b.get('title', 'No Title')}")
        except Exception as ex:
            print(f"[DEBUG] Error dumping DB: {ex}")
        print(f"[DEBUG] --- END DB DUMP ---")
        
        raise HTTPException(status_code=404, detail="Book not found")

    if book:
        book["_id"] = str(book["_id"])
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
        print(f"✓ Converted import to JSON format: {len(lexical_json['root']['children'])} blocks")
    except Exception as e:
        print(f"⚠ HTML conversion failed, using simple fallback: {e}")
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
    book_dict = new_book.dict(by_alias=True, exclude={'id'})
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
