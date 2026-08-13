# Page Model Deprecation & Migration Guide

## Overview

The **Page model has been deprecated and removed** as of January 2025. Books now use a single `full_content` field instead of separate Page documents in the database.

---

## What Changed

### Before (Deprecated Architecture)
```python
# OLD: Separate Page documents
class Book(BaseModel):
    id: PyObjectId
    title: str
    pages: List[PyObjectId]  # References to Page documents
    # ...

class Page(BaseModel):
    id: PyObjectId
    book_id: PyObjectId
    page_number: int
    content: str  # Individual page content
    # ...
```

**Problems with old architecture:**
- ❌ Complex queries (need to fetch book + all pages separately)
- ❌ Performance issues (N+1 query problem)
- ❌ Difficult to search across book content
- ❌ Complicated synchronization between book and pages
- ❌ More database documents = higher costs

### After (Current Architecture)
```python
# NEW: Single document with full content
class Book(BaseModel):
    id: PyObjectId
    title: str
    full_content: str  # All content in one field (JSON or HTML)
    # No pages field!
```

**Benefits of new architecture:**
- ✅ Single query to fetch entire book
- ✅ Better performance
- ✅ Easier full-text search
- ✅ Simpler data model
- ✅ Lower storage costs
- ✅ Easier backup/restore

---

## Migration Details

### Database Changes

**No database migration required** if you're starting fresh. If you have existing data with Pages:

1. **Legacy data handling:**
   - Old books with `pages` field will continue to work
   - Pages collection can be dropped after migration
   - No soft delete needed for Page model (doesn't exist anymore)

2. **Migration script example:**
```python
async def migrate_pages_to_full_content():
    """
    One-time migration: Combine all Page documents into Book.full_content
    """
    from app.database import db
    from bson import ObjectId
    
    # Find all books with pages field
    books_with_pages = await db["books"].find({"pages": {"$exists": True}}).to_list(None)
    
    for book in books_with_pages:
        if not book.get("pages"):
            continue
            
        # Fetch all pages for this book
        page_ids = [ObjectId(p) for p in book["pages"]]
        pages = await db["pages"].find({
            "_id": {"$in": page_ids}
        }).sort("page_number", 1).to_list(None)
        
        # Combine page content
        combined_content = "\n\n".join([
            page.get("content", "") for page in pages
        ])
        
        # Update book with full_content
        await db["books"].update_one(
            {"_id": book["_id"]},
            {
                "$set": {
                    "full_content": combined_content,
                    "updated_at": datetime.utcnow()
                },
                "$unset": {"pages": ""}  # Remove pages field
            }
        )
        
        print(f"Migrated book: {book.get('title')} ({len(pages)} pages)")
    
    print(f"Migration complete! {len(books_with_pages)} books migrated")

# Run migration (once only)
# await migrate_pages_to_full_content()

# After migration is verified, drop pages collection
# await db["pages"].drop()
```

---

## Code Updates Required

### 1. Remove Page Import Statements

**Before:**
```python
from app.models.Page import Page  # ❌ This will fail now
```

**After:**
```python
# No Page import needed! ✅
```

### 2. Update Book Queries

**Before:**
```python
# Fetch book
book = await db["books"].find_one({"_id": book_id})

# Fetch all pages separately
pages = await db["pages"].find({
    "_id": {"$in": book["pages"]}
}).sort("page_number", 1).to_list(None)

# Combine content
full_text = "\n".join([p["content"] for p in pages])
```

**After:**
```python
# Fetch book (content already included!)
book = await db["books"].find_one({"_id": book_id})

# Access content directly
full_text = book.get("full_content", "")
```

### 3. Update Book Creation

**Before:**
```python
# Create book
book = await db["books"].insert_one({
    "title": "My Book",
    "pages": []  # Empty list
})

# Create pages separately
for page_content in extracted_pages:
    page = await db["pages"].insert_one({
        "book_id": book.inserted_id,
        "page_number": i + 1,
        "content": page_content
    })
    # Add to book.pages array
    await db["books"].update_one(
        {"_id": book.inserted_id},
        {"$push": {"pages": page.inserted_id}}
    )
```

**After:**
```python
# Create book with content directly
combined_content = "\n\n".join(extracted_pages)

book = await db["books"].insert_one({
    "title": "My Book",
    "full_content": combined_content  # All content in one field
})
```

### 4. Update Book Updates

**Before:**
```python
# Update specific page
await db["pages"].update_one(
    {"_id": page_id},
    {"$set": {"content": new_content}}
)
```

**After:**
```python
# Update full_content
await db["books"].update_one(
    {"_id": book_id},
    {"$set": {"full_content": new_full_content}}
)
```

---

## Content Format

The `full_content` field supports two formats:

### 1. JSON Format (Lexical Editor State) - RECOMMENDED
```json
{
  "root": {
    "children": [
      {
        "type": "paragraph",
        "children": [
          {"type": "text", "text": "Hello world"}
        ]
      }
    ],
    "direction": "ltr",
    "format": "",
    "indent": 0,
    "type": "root",
    "version": 1
  }
}
```

### 2. HTML Format (Legacy)
```html
<h1>Chapter 1</h1>
<p>This is the first chapter...</p>
<h2>Section 1.1</h2>
<p>Content here...</p>
```

**Best Practice:** Use JSON (Lexical) format for new books, HTML is supported for legacy imports only.

---

## API Response Changes

### Book List Response

**Before:**
```json
{
  "id": "123",
  "title": "My Book",
  "pages": ["page1_id", "page2_id", "page3_id"],
  "page_count": 3
}
```

**After:**
```json
{
  "id": "123",
  "title": "My Book",
  "full_content": "...",
  "word_count": 1500
}
```

**Note:** `page_count` metadata may still appear in import responses, but it's just metadata, not actual Page documents.

---

## Testing Updates

### Update Test Cases

**Before:**
```python
@pytest.mark.asyncio
async def test_create_book_with_pages():
    # Create book
    book = await create_book({"title": "Test"})
    
    # Create pages
    page1 = await create_page(book.id, "Content 1")
    page2 = await create_page(book.id, "Content 2")
    
    # Verify
    assert len(book.pages) == 2
```

**After:**
```python
@pytest.mark.asyncio
async def test_create_book_with_content():
    # Create book with content directly
    book = await create_book({
        "title": "Test",
        "full_content": "Content 1\n\nContent 2"
    })
    
    # Verify
    assert book.full_content == "Content 1\n\nContent 2"
```

---

## Frequently Asked Questions

### Q: What happens to existing Page documents in my database?

**A:** They remain in the database but are not used. You can:
1. Run the migration script to move content to `full_content`
2. Drop the `pages` collection after verifying migration
3. Or leave them for historical purposes (they won't interfere)

### Q: Can I still use pagination in the UI?

**A:** Yes! Pagination is a UI concern, not a data model concern. Split `full_content` client-side:

```typescript
// Frontend: Split content for pagination
const contentPerPage = 3000; // characters
const pages = splitContentIntoPages(book.full_content, contentPerPage);
```

### Q: How do I search across book content now?

**A:** Much easier! Use MongoDB text search on the `full_content` field:

```python
# Create text index
await db["books"].create_index([("full_content", "text")])

# Search
results = await db["books"].find({
    "$text": {"$search": "quantum physics"}
}).to_list(None)
```

### Q: What about very large books?

**A:** MongoDB documents can be up to 16MB. For reference:
- 16MB ≈ 8 million characters
- Average book: 80,000 - 100,000 words ≈ 500KB - 600KB
- Large technical book: 200,000 words ≈ 1.2MB

If you somehow exceed 16MB (very rare), consider:
- Storing content in GridFS (MongoDB's file storage)
- Compressing content
- Splitting into multiple volumes

### Q: Does this affect soft delete?

**A:** No! Books still have soft delete. When a Book is soft-deleted, all its content (in `full_content`) is preserved and can be restored.

### Q: What about performance?

**A:** **Much better!** 
- Before: 1 query for book + N queries for pages = N+1 queries
- After: 1 query for everything = 1 query
- Network latency reduced
- Database load reduced

---

## Checklist for Migration

Use this checklist when migrating your code:

- [ ] Remove `Page` import statements
- [ ] Update book creation to use `full_content`
- [ ] Update book queries (no need to fetch pages separately)
- [ ] Update book update logic
- [ ] Remove page-related API endpoints (if any)
- [ ] Update frontend to handle `full_content` instead of pages array
- [ ] Run migration script on production database
- [ ] Verify all books have `full_content` populated
- [ ] Drop `pages` collection (after backup!)
- [ ] Update tests to not reference Page model
- [ ] Update API documentation
- [ ] Remove `Page` from soft delete documentation (already done!)

---

## Related Documentation

- [Soft Delete Implementation Guide](./SOFT_DELETE_GUIDE.md)
- [Book API Documentation](../docs/api/books.md)
- [Content Format Specification](../docs/content-format.md)

---

## Support

If you encounter issues during migration:

1. **Backup your database first!**
2. Run migration script with `dry_run=True` to preview changes
3. Test on staging environment before production
4. Keep `pages` collection for 30 days after migration (safety net)

---

## Changelog

| Date | Change | Author |
|------|--------|--------|
| 2025-01-24 | Page model removed, migration to full_content complete | System |
| 2024-XX-XX | Initial migration from Page to full_content | [Previous] |

---

**Last Updated:** 2025-01-24  
**Status:** ✅ Migration Complete - Page Model Removed
