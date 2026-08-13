# Content Deletion & Fork Behavior - Design Decision

## Current Implementation ✅

### What Works Now:
1. **Browse Filtering**: Deleted content is automatically hidden from public browse
   - Query filters: `"is_public": True, "deleted_at": None`
   - Soft-deleted content won't appear in search results

2. **Fork Independence**: Forked content is independent
   - When user forks content, a complete copy is created
   - Forked content has its own `_id` and `user_id`
   - Original and fork are separate database documents

## What Needs Implementation 🔧

### 1. Auto-Unpublish on Delete

When a user soft-deletes published content, it should be automatically unpublished.

**Why?**
- Prevents orphaned public listings
- Clear user expectation: deleted = not public
- Maintains data integrity

**Implementation:**
Add to Book/Deck delete endpoints:

```python
# When soft-deleting
await collection.update_one(
    {"_id": ObjectId(content_id)},
    {
        "$set": {
            "deleted_at": datetime.utcnow(),
            "deleted_by": user_id,
            "is_public": False,  # ← Auto-unpublish
            "updated_at": datetime.utcnow()
        }
    }
)
```

### 2. Fork Tracking & Notifications

**Current Behavior:**
- Original deleted → Forks remain intact ✅
- Forks are independent copies in user's library
- No link breaks because forks are full copies

**Optional Enhancement (Future):**
Track relationship for transparency:

```python
# Add to ContentFork model
class ContentFork(BaseModel):
    original_deleted: bool = False
    original_deleted_at: Optional[datetime] = None
```

**User Notification (Future):**
- "Original content by @username is no longer available"
- Doesn't affect functionality, just informational

### 3. Public View Behavior

**Current:**
- Direct link to deleted content: Returns 404 ✅ (checked at line 211)
- Query filters for `deleted_at: None`

**Edge Case:**
What if user has a direct link open when content is deleted?

**Solution:**
Already handled! The `get_public_content_by_id` query includes:
```python
query = {
    "_id": ObjectId(content_id),
    "is_public": True,
    "deleted_at": None  # ← This prevents access
}
```

## Recommended Immediate Changes

### Priority 1: Auto-Unpublish on Delete

Update the delete endpoints in `books.py` and `decks.py`:

**Before:**
```python
await collection.update_one(
    {"_id": ObjectId(book_id)},
    {"$set": {"deleted_at": datetime.utcnow(), "deleted_by": user_id}}
)
```

**After:**
```python
await collection.update_one(
    {"_id": ObjectId(book_id)},
    {
        "$set": {
            "deleted_at": datetime.utcnow(),
            "deleted_by": user_id,
            "is_public": False,  # Auto-unpublish
            "updated_at": datetime.utcnow()
        }
    }
)
```

### Priority 2: Cascade Delete for Related Records

When content is deleted, clean up engagement records:

```python
# Delete likes
await db.content_likes.delete_many({
    "content_id": content_id,
    "content_type": content_type
})

# Delete views (optional - might want to keep for analytics)
# await db.content_views.delete_many(...)

# Keep forks - they're independent content
# Keep reports - needed for moderation history
```

## Summary: What Happens When...

### Scenario 1: User Deletes Published Book
1. ✅ Book is soft-deleted (`deleted_at` set)
2. 🔧 Book should be auto-unpublished (`is_public = False`) - **NEEDS IMPLEMENTATION**
3. ✅ Disappears from public browse immediately (filter works)
4. ✅ Direct links return 404
5. ✅ Forks remain intact in other users' libraries
6. 🔧 Likes/views should be cleaned up - **OPTIONAL**

### Scenario 2: User Views Fork After Original is Deleted
1. ✅ Fork works perfectly (independent copy)
2. ✅ Fork can still be published by its owner
3. 💡 Could show "Forked from deleted content" badge - **FUTURE**

### Scenario 3: User Restores Deleted Content
1. ✅ Content is restored (`deleted_at = None`)
2. ❌ Content stays unpublished (user must re-publish) - **BY DESIGN**
3. ✅ This is correct behavior - gives user control

## Implementation Checklist

- [ ] Update `books.py` delete endpoint to auto-unpublish
- [ ] Update `decks.py` delete endpoint to auto-unpublish
- [ ] Add cascade delete for likes (optional)
- [ ] Add cascade delete for views (optional)
- [ ] Test: Delete published book → verify not in browse
- [ ] Test: Delete published book → verify direct link 404
- [ ] Test: Fork still accessible after original deleted
- [ ] Test: Restore deleted book → verify stays unpublished

## Code Changes Needed

I'll create the necessary updates to the delete endpoints.
