# Public Content Sharing - Quick Setup Guide

## 🚀 Getting Started

This guide will help you integrate the Public Content Sharing feature into your Nowry API.

---

## Step 1: Add Router to FastAPI App

In your `main.py` or app initialization file:

```python
from app.routers import public_content, moderation

# Add routers
app.include_router(public_content.router, prefix="/api")
app.include_router(moderation.router, prefix="/api")
```

---

## Step 2: Create Database Indexes

Run this once to create required indexes:

```python
from app.database import db

async def create_public_content_indexes():
    """Create indexes for public content feature"""
    
    # Books
    await db["books"].create_index([("is_public", 1)])
    await db["books"].create_index([("published_at", -1)])
    await db["books"].create_index([("public_metadata.category", 1)])
    await db["books"].create_index([("public_metadata.tags", 1)])
    await db["books"].create_index([("public_metadata.views", -1)])
    
    # Compound index for efficient querying
    await db["books"].create_index([
        ("is_public", 1),
        ("deleted_at", 1),
        ("published_at", -1)
    ])
    
    # Decks (same as books)
    await db["decks"].create_index([("is_public", 1)])
    await db["decks"].create_index([("published_at", -1)])
    await db["decks"].create_index([("public_metadata.category", 1)])
    await db["decks"].create_index([("public_metadata.tags", 1)])
    await db["decks"].create_index([("public_metadata.views", -1)])
    
    await db["decks"].create_index([
        ("is_public", 1),
        ("deleted_at", 1),
        ("published_at", -1)
    ])
    
    # Content Likes
    await db["content_likes"].create_index([("user_id", 1)])
    await db["content_likes"].create_index([("content_id", 1)])
    await db["content_likes"].create_index([
        ("content_id", 1),
        ("user_id", 1)
    ], unique=True)
    
    # Content Forks
    await db["content_forks"].create_index([("original_content_id", 1)])
    await db["content_forks"].create_index([("forked_by_user_id", 1)])
    
    # Content Reports
    await db["content_reports"].create_index([("status", 1), ("created_at", -1)])
    await db["content_reports"].create_index([("content_id", 1)])
    
    print("✅ All indexes created successfully!")

# Run once
# await create_public_content_indexes()
```

---

## Step 3: Add Optional Auth Helper

The public browse endpoints need to work without authentication, but should detect if a user is logged in.

Create `app/auth/firebase.py` (if not exists):

```python
from fastapi import Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from typing import Optional

security = HTTPBearer()

async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)) -> dict:
    """Get authenticated user (required)"""
    token = credentials.credentials
    # Your Firebase token verification here
    # ...
    return user

async def optional_auth(credentials: Optional[HTTPAuthorizationCredentials] = Depends(HTTPBearer(auto_error=False))) -> Optional[dict]:
    """Get authenticated user (optional - returns None if not logged in)"""
    if not credentials:
        return None
    
    try:
        token = credentials.credentials
        # Your Firebase token verification here
        # ...
        return user
    except:
        return None
```

---

## Step 4: Test the Endpoints

### Test 1: Publish a Book

```bash
curl -X POST "http://localhost:8000/api/public/books/{book_id}/publish" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "category": "Science",
    "tags": ["physics", "quantum"],
    "language": "en",
    "difficulty_level": "intermediate",
    "license_type": "CC-BY",
    "is_original_content": true
  }'
```

### Test 2: Browse Public Books (No Auth)

```bash
curl "http://localhost:8000/api/public/books?category=Science&sort_by=recent&page=1&page_size=20"
```

### Test 3: View Public Book

```bash
curl "http://localhost:8000/api/public/books/{book_id}"
```

### Test 4: Like a Book

```bash
curl -X POST "http://localhost:8000/api/public/books/{book_id}/like" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

### Test 5: Fork a Book

```bash
curl -X POST "http://localhost:8000/api/public/books/{book_id}/fork" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

---

## Step 5: Update Existing Book/Deck Endpoints

### Add Public Filter to List Endpoints

Your existing `/books` endpoint should now filter by user AND exclude public content browsing:

```python
@router.get("/books")
async def get_my_books(current_user: dict = Depends(get_current_user)):
    """Get MY books (private + my public ones)"""
    books = await db["books"].find({
        "user_id": current_user["uid"],
        "deleted_at": None
    }).to_list(None)
    
    return books

# Public books are accessed via /public/books (separate endpoint)
```

---

## Step 6: Frontend Integration

### Publish Button

Add a "Publish" button to your Book/Deck editor:

```typescript
const publishBook = async (bookId: string) => {
  const metadata = await openPublishModal(); // User fills form
  
  const response = await fetch(`/api/public/books/${bookId}/publish`, {
    method: 'POST',
    headers: {
      'Authorization': `Bearer ${token}`,
      'Content-Type': 'application/json'
    },
    body: JSON.stringify(metadata)
  });
  
  if (response.ok) {
    toast.success('Book published successfully!');
  }
};
```

### Public Browse Page

Create a new page `/public` or `/browse`:

```tsx
const PublicBrowsePage = () => {
  const [books, setBooks] = useState([]);
  const [filters, setFilters] = useState({
    category: '',
    search: '',
    sortBy: 'recent'
  });
  
  useEffect(() => {
    fetchPublicBooks();
  }, [filters]);
  
  const fetchPublicBooks = async () => {
    const params = new URLSearchParams({
      category: filters.category,
      search: filters.search,
      sort_by: filters.sortBy,
      page: '1',
      page_size: '20'
    });
    
    const response = await fetch(`/api/public/books?${params}`);
    const data = await response.json();
    setBooks(data.items);
  };
  
  return (
    <div>
      {/* Filters */}
      <Filters onChange={setFilters} />
      
      {/* Results */}
      <Grid>
        {books.map(book => (
          <BookCard key={book._id} book={book} />
        ))}
      </Grid>
    </div>
  );
};
```

---

## Step 7: Enable CORS for Public Endpoints

If your frontend is on a different domain:

```python
from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "https://yourdomain.com"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
```

---

## Step 8: Add Rate Limiting (Optional but Recommended)

Install slowapi:

```bash
pip install slowapi
```

Add to your app:

```python
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Apply to public endpoints
@router.get("/public/books")
@limiter.limit("100/minute")
async def browse_public_books(request: Request, ...):
    ...
```

---

## Step 9: Set Up Admin Role (TODO)

For moderation endpoints, add admin role checking:

```python
from fastapi import HTTPException

async def require_admin(current_user: dict = Depends(get_current_user)):
    """Require admin role"""
    # Check if user has admin role
    # This depends on your user model/auth system
    
    user_doc = await db["users"].find_one({"firebase_uid": current_user["uid"]})
    
    if not user_doc or not user_doc.get("is_admin"):
        raise HTTPException(status_code=403, detail="Admin access required")
    
    return current_user

# Update moderation endpoints
@router.get("/admin/reports")
async def get_all_reports(admin: dict = Depends(require_admin)):
    ...
```

---

## Step 10: Monitor & Iterate

### Metrics to Track

1. **Public Content Growth**
   - Daily new public books/decks
   - Total public content count

2. **Engagement**
   - Views per content
   - Fork rate
   - Like rate

3. **Quality**
   - Reports per 1000 views
   - Average time to fork

4. **SEO**
   - Google Search Console impressions
   - Organic traffic from public pages

### Set Up Analytics

```python
# Example: Track engagement
async def track_engagement_metrics():
    total_views = await db["content_views"].count_documents({})
    total_likes = await db["content_likes"].count_documents({})
    total_forks = await db["content_forks"].count_documents({})
    
    print(f"📊 Engagement Metrics:")
    print(f"  - Total Views: {total_views}")
    print(f"  - Total Likes: {total_likes}")
    print(f"  - Total Forks: {total_forks}")
```

---

## Troubleshooting

### Issue: Public content not showing

**Check:**
1. Content has `is_public: true`
2. Content has `deleted_at: null`
3. Indexes are created
4. No filters excluding the content

### Issue: Fork creates empty deck

**Note:** Deck forking doesn't copy cards yet. You need to implement card cloning:

```python
# In fork_content method
if content_type == "deck" and "cards" in original:
    # Get original cards
    original_cards = await db["study_cards"].find({
        "_id": {"$in": original["cards"]},
        "deleted_at": None
    }).to_list(None)
    
    # Clone each card
    new_card_ids = []
    for card in original_cards:
        new_card = dict(card)
        new_card.pop("_id")
        new_card["user_id"] = forking_user_id
        new_card["deck_id"] = result.inserted_id
        new_card["created_at"] = datetime.utcnow()
        
        card_result = await db["study_cards"].insert_one(new_card)
        new_card_ids.append(card_result.inserted_id)
    
    # Update forked deck with new card IDs
    await db["decks"].update_one(
        {"_id": result.inserted_id},
        {"$set": {"cards": new_card_ids, "total_cards": len(new_card_ids)}}
    )
```

---

## Next Steps

1. ✅ Test all endpoints with Postman/curl
2. ✅ Build frontend UI for publishing
3. ✅ Build public browse page
4. ✅ Add SEO meta tags to public pages
5. ✅ Set up monitoring
6. ✅ Launch to beta users
7. ✅ Gather feedback
8. ✅ Iterate!

---

## Need Help?

- 📖 See [PUBLIC_CONTENT_FEATURE.md](./PUBLIC_CONTENT_FEATURE.md) for full documentation
- 🐛 Check [Troubleshooting](#troubleshooting) section above
- 💬 Ask questions in your team Slack/Discord

---

**Last Updated:** January 24, 2025  
**Status:** ✅ Ready to Deploy
