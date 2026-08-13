# Soft Delete Implementation Guide

## Overview
Soft delete has been implemented for high-value models to prevent data loss and enable recovery. This document explains how to use it in your services.

## Models with Soft Delete

The following models now support soft delete via `SoftDeleteMixin`:

### User-Created Content (HIGH VALUE)
1. **Book** - User-created books/documents
2. **Deck** - Flashcard collections
3. **StudyCard** - Individual flashcards with learning progress

### Annual Planning Data (HISTORICAL VALUE)
5. **AnnualPlan** - Yearly planning container
6. **Goal** - Annual goals
7. **FocusArea** - Focus areas for the year
8. **Priority** - Top priorities

### User Configuration (PRODUCTIVITY VALUE)
9. **DailyRoutineTemplate** - Daily routine templates
10. **User** - Account data and compliance

### Models WITHOUT Soft Delete (Hard Delete)
- **Task** - Short-lived, low value (can archive completed tasks instead)
- **Activity** - Temporal logs (use retention policies)
- **CardGenerationRequest** - Temporary processing metadata
- **QuizGenerationRequest** - Temporary processing metadata
- **Bug** - Should use status changes, not deletion

## Basic Usage

### 1. Querying Active (Non-Deleted) Records

**IMPORTANT:** By default, MongoDB queries will return ALL records including soft-deleted ones. You MUST add the active filter to exclude deleted records.

```python
from app.models.Book import Book

# ❌ WRONG - Returns deleted records too
books = await db["books"].find({"user_id": user_id}).to_list(None)

# ✅ CORRECT - Only active records
books = await db["books"].find({
    "user_id": user_id,
    **Book.active_filter()  # {"deleted_at": None}
}).to_list(None)

# Alternative explicit syntax
books = await db["books"].find({
    "user_id": user_id,
    "deleted_at": None
}).to_list(None)
```

### 2. Soft Deleting a Record

```python
from datetime import datetime

# Using the mixin method
book = Book(**book_data)
update_query = book.soft_delete(user_id="user_123")

await db["books"].update_one(
    {"_id": ObjectId(book_id)},
    update_query
)

# Manual approach (equivalent)
await db["books"].update_one(
    {"_id": ObjectId(book_id)},
    {
        "$set": {
            "deleted_at": datetime.utcnow(),
            "deleted_by": user_id,
            "updated_at": datetime.utcnow()
        }
    }
)
```

### 3. Restoring a Soft-Deleted Record

```python
book = Book(**book_data)
restore_query = book.restore()

await db["books"].update_one(
    {"_id": ObjectId(book_id)},
    restore_query
)

# Manual approach (equivalent)
await db["books"].update_one(
    {"_id": ObjectId(book_id)},
    {
        "$set": {
            "deleted_at": None,
            "deleted_by": None,
            "updated_at": datetime.utcnow()
        }
    }
)
```

### 4. Querying Only Deleted Records

```python
from app.models.Book import Book

# Get all deleted books
deleted_books = await db["books"].find({
    "user_id": user_id,
    **Book.deleted_filter()  # {"deleted_at": {"$ne": None}}
}).to_list(None)
```

### 5. Checking if a Record is Deleted

```python
book = Book(**book_data)

if book.is_deleted:
    print("This book has been deleted")
else:
    print("This book is active")
```

## Service Implementation Examples

### GET Endpoint - Single Record
```python
@router.get("/books/{book_id}")
async def get_book(book_id: str, current_user: dict = Depends(get_current_user)):
    book = await db["books"].find_one({
        "_id": ObjectId(book_id),
        "user_id": current_user["uid"],
        "deleted_at": None  # Only active books
    })
    
    if not book:
        raise HTTPException(status_code=404, detail="Book not found")
    
    return book
```

### GET Endpoint - List
```python
@router.get("/books")
async def list_books(current_user: dict = Depends(get_current_user)):
    books = await db["books"].find({
        "user_id": current_user["uid"],
        **Book.active_filter()  # Only active books
    }).to_list(None)
    
    return books
```

### DELETE Endpoint - Soft Delete
```python
@router.delete("/books/{book_id}")
async def delete_book(book_id: str, current_user: dict = Depends(get_current_user)):
    # Verify ownership and not already deleted
    book = await db["books"].find_one({
        "_id": ObjectId(book_id),
        "user_id": current_user["uid"],
        "deleted_at": None
    })
    
    if not book:
        raise HTTPException(status_code=404, detail="Book not found")
    
    # Soft delete
    book_obj = Book(**book)
    await db["books"].update_one(
        {"_id": ObjectId(book_id)},
        book_obj.soft_delete(user_id=current_user["uid"])
    )
    
    return {"message": "Book deleted successfully"}
```

### POST Endpoint - Restore
```python
@router.post("/books/{book_id}/restore")
async def restore_book(book_id: str, current_user: dict = Depends(get_current_user)):
    # Find deleted book
    book = await db["books"].find_one({
        "_id": ObjectId(book_id),
        "user_id": current_user["uid"],
        "deleted_at": {"$ne": None}  # Only deleted books
    })
    
    if not book:
        raise HTTPException(status_code=404, detail="Deleted book not found")
    
    # Restore
    book_obj = Book(**book)
    await db["books"].update_one(
        {"_id": ObjectId(book_id)},
        book_obj.restore()
    )
    
    return {"message": "Book restored successfully"}
```

## Cascade Deletion

When deleting parent records, consider soft-deleting related records:

```python
# Example: Deleting a Deck should soft-delete all its cards
@router.delete("/decks/{deck_id}")
async def delete_deck(deck_id: str, current_user: dict = Depends(get_current_user)):
    deck = await db["decks"].find_one({
        "_id": ObjectId(deck_id),
        "user_id": ObjectId(current_user["uid"]),
        "deleted_at": None
    })
    
    if not deck:
        raise HTTPException(status_code=404, detail="Deck not found")
    
    # Soft delete the deck
    deck_obj = Deck(**deck)
    await db["decks"].update_one(
        {"_id": ObjectId(deck_id)},
        deck_obj.soft_delete(user_id=current_user["uid"])
    )
    
    # Cascade: Soft delete all cards in this deck
    card_obj = StudyCard()  # Just for accessing the method
    await db["study_cards"].update_many(
        {"deck_id": ObjectId(deck_id), "deleted_at": None},
        card_obj.soft_delete(user_id=current_user["uid"])
    )
    
    return {"message": "Deck and all cards deleted successfully"}
```

## Database Indexes

Add indexes for optimal query performance:

```python
# In your database initialization/migration
await db["books"].create_index([("deleted_at", 1)])
await db["decks"].create_index([("deleted_at", 1)])
await db["study_cards"].create_index([("deleted_at", 1)])
await db["users"].create_index([("deleted_at", 1)])
await db["annual_plans"].create_index([("deleted_at", 1)])
await db["goals"].create_index([("deleted_at", 1)])
await db["focus_areas"].create_index([("deleted_at", 1)])
await db["priorities"].create_index([("deleted_at", 1)])
await db["daily_routine_templates"].create_index([("deleted_at", 1)])

# Compound indexes for common queries
await db["books"].create_index([("user_id", 1), ("deleted_at", 1)])
await db["decks"].create_index([("user_id", 1), ("deleted_at", 1)])
await db["study_cards"].create_index([("deck_id", 1), ("deleted_at", 1)])
await db["annual_plans"].create_index([("user_id", 1), ("year", 1), ("deleted_at", 1)])
await db["goals"].create_index([("focus_area_id", 1), ("deleted_at", 1)])
await db["focus_areas"].create_index([("annual_plan_id", 1), ("deleted_at", 1)])
await db["priorities"].create_index([("annual_plan_id", 1), ("deleted_at", 1)])
await db["daily_routine_templates"].create_index([("user_id", 1), ("deleted_at", 1)])
```

## Cleanup Job (Optional)

Periodically hard-delete old soft-deleted records to save storage:

```python
from datetime import datetime, timedelta

async def cleanup_old_deleted_records():
    """
    Hard delete records that have been soft-deleted for more than 90 days.
    Run this as a scheduled job (e.g., daily).
    """
    cutoff_date = datetime.utcnow() - timedelta(days=90)
    
    collections = ["books", "decks", "study_cards", "annual_plans", "goals", "focus_areas", "priorities", "daily_routine_templates"]
    
    for collection_name in collections:
        result = await db[collection_name].delete_many({
            "deleted_at": {"$lt": cutoff_date, "$ne": None}
        })
        print(f"Cleaned up {result.deleted_count} records from {collection_name}")
```

## Migration Checklist

When updating existing services:

- [ ] Add `Book.active_filter()` or `{"deleted_at": None}` to all find queries
- [ ] Change delete endpoints to use soft delete instead of `delete_one()`
- [ ] Add restore endpoints where needed
- [ ] Implement cascade deletion for parent-child relationships
- [ ] Add database indexes for `deleted_at` field
- [ ] Test soft delete and restore functionality
- [ ] Update API documentation

## Testing

```python
# Test soft delete
book_id = "test_book_id"
await delete_book(book_id, current_user)

# Verify it's not in active queries
active_books = await db["books"].find({"deleted_at": None}).to_list(None)
assert book_id not in [str(b["_id"]) for b in active_books]

# Verify it exists in deleted queries
deleted_books = await db["books"].find({"deleted_at": {"$ne": None}}).to_list(None)
assert book_id in [str(b["_id"]) for b in deleted_books]

# Test restore
await restore_book(book_id, current_user)

# Verify it's back in active queries
active_books = await db["books"].find({"deleted_at": None}).to_list(None)
assert book_id in [str(b["_id"]) for b in active_books]
```

## Common Pitfalls

1. **Forgetting the active filter** - Always add `deleted_at: None` to queries
2. **Not handling cascades** - When deleting parents, soft-delete children too
3. **Missing indexes** - Index `deleted_at` for performance
4. **Not updating counts** - When showing "total books", exclude deleted ones
5. **Foreign key lookups** - Check `deleted_at` when looking up related records

## Additional Features in Mixin

The `SoftDeleteMixin` provides:
- `deleted_at` - Timestamp of deletion
- `deleted_by` - User ID who deleted the record
- `is_deleted` - Property to check deletion status
- `soft_delete(user_id)` - Method to generate soft delete update query
- `restore()` - Method to generate restore update query
- `active_filter()` - Static method returning `{"deleted_at": None}`
- `deleted_filter()` - Static method returning `{"deleted_at": {"$ne": None}}`
