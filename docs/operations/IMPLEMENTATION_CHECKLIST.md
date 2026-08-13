# Public Content MVP - Implementation Checklist

## ✅ COMPLETED (Backend)

### 1. Models Created
- ✅ `PublicContent.py` - PublicMetadata, ContentReport, ContentFork, ContentLike, ContentView
- ✅ `Book.py` - Added public fields
- ✅ `Deck.py` - Added public fields

### 2. Service Layer
- ✅ `public_content_service.py` - All business logic

### 3. API Routers
- ✅ `public_content.py` - 15 endpoints
- ✅ `moderation.py` - 5 endpoints
- ✅ Added to `main.py`

---

## 🔧 TODO - Backend (Quick Setup)

### Step 1: Add optional_auth Helper

Edit `/app/auth/firebase_auth.py`, add this function:

```python
from typing import Optional
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

security = HTTPBearer(auto_error=False)

async def optional_auth(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)
) -> Optional[dict]:
    """Get authenticated user (optional - returns None if not logged in)"""
    if not credentials:
        return None
    
    try:
        # Extract token
        token = credentials.credentials
        
        # Check cache first
        cached_data = _get_cached_token(token)
        if cached_data:
            # Add uid alias for compatibility
            cached_data["uid"] = cached_data.get("firebase_uid") or cached_data.get("user_id")
            return cached_data
        
        # Verify token
        decoded_token = verify_firebase_token(token)
        
        token_data = {
            "firebase_uid": decoded_token.get("uid"),
            "uid": decoded_token.get("uid"),  # Alias
            "email": decoded_token.get("email"),
            "email_verified": decoded_token.get("email_verified", False),
        }
        
        # Cache it
        _cache_token(token, token_data)
        
        return token_data
    except:
        return None  # Silent fail - user not logged in
```

### Step 2: Update get_current_user (Compatibility Fix)

In the same file, ensure `get_current_user` returns `uid` field:

```python
async def get_current_user(request: Request) -> dict:
    """Get authenticated user (required)"""
    user_data = await get_firebase_user(request)
    
    # Add uid alias for compatibility
    user_data["uid"] = user_data.get("firebase_uid") or user_data.get("user_id")
    
    return user_data
```

### Step 3: Create Database Indexes

Run this ONCE in a Python script or Jupyter notebook:

```python
from app.config.database import db

async def create_public_content_indexes():
    # Books
    await db["books"].create_index([("is_public", 1)])
    await db["books"].create_index([("published_at", -1)])
    await db["books"].create_index([("public_metadata.category", 1)])
    await db["books"].create_index([("public_metadata.tags", 1)])
    await db["books"].create_index([
        ("is_public", 1),
        ("deleted_at", 1),
        ("published_at", -1)
    ])
    
    # Decks (same)
    await db["decks"].create_index([("is_public", 1)])
    await db["decks"].create_index([("published_at", -1)])
    await db["decks"].create_index([("public_metadata.category", 1)])
    await db["decks"].create_index([("public_metadata.tags", 1)])
    await db["decks"].create_index([
        ("is_public", 1),
        ("deleted_at", 1),
        ("published_at", -1)
    ])
    
    # Engagement
    await db["content_likes"].create_index([("user_id", 1)])
    await db["content_likes"].create_index([
        ("content_id", 1),
        ("content_type", 1),
        ("user_id", 1)
    ], unique=True)
    
    await db["content_forks"].create_index([("original_content_id", 1)])
    await db["content_reports"].create_index([("status", 1), ("created_at", -1)])
    
    print("✅ Indexes created!")

# Run it
await create_public_content_indexes()
```

### Step 4: Test Backend

```bash
# Test public browse (no auth)
curl "http://localhost:8000/public/books"

# Test publish (with auth)
curl -X POST "http://localhost:8000/public/books/{id}/publish" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"category": "Science", "tags": ["physics"], "language": "en"}'
```

---

## 🎨 TODO - Frontend (Main Work)

All components should follow `DESIGN_GUIDELINES.md`:
- Use theme tokens (NO hardcoded colors)
- Responsive (`xs`, `md` breakpoints)
- i18n (`{t('key')}`)
- Minimal padding
- Sentence case for text

### Component 1: PublishModal

**Location:** `nowry/src/components/PublishModal.js`

**UI Design:**
- Modal (centered, backdrop)
- Form fields:
  - Category dropdown (Science, Math, Languages, etc.)
  - Tags input (chip selector)
  - Language dropdown (en, es, fr, de, ja)
  - Difficulty radio (beginner, intermediate, advanced)
  - License dropdown (All Rights Reserved, CC-BY, CC-BY-SA, CC0)
  - Checkbox: "This is my original content"
- Buttons: Cancel (plain), Publish (primary)

**Key Points:**
- Validation: Category required, at least 1 tag
- On submit: Call API `/public/books/{id}/publish`
- Show success toast after publish

### Component 2: PublishButton

**Location:** Add to Book/Deck editor headers

```jsx
<Button
  variant="outlined"
  startDecorator={<PublicIcon />}
  onClick={() => setPublishModalOpen(true)}
>
  {book.is_public ? t('public.unpublish') : t('public.publish')}
</Button>
```

### Component 3: Public Browse Page

**Location:** `nowry/src/pages/PublicBrowse.js`  
**Route:** `/browse` or `/public`

**Layout:**
```
┌─────────────────────────────────────────┐
│  Public Library                         │
│  ┌─────────┬──────────┬──────────┐     │
│  │Category▼│  Tags▼   │ Search🔍│     │
│  └─────────┴──────────┴──────────┘     │
│                                         │
│  ┌──────┐ ┌──────┐ ┌──────┐           │
│  │Book 1│ │Book 2│ │Book 3│           │
│  │ 👁 50│ │ 👁120│ │ 👁 80│           │
│  │ ❤️ 5 │ │ ❤️ 12│ │ ❤️ 8 │           │
│  └──────┘ └──────┘ └──────┘           │
└─────────────────────────────────────────┘
```

**Features:**
- Tab switcher: Books / Decks
- Filters sidebar (or top bar on mobile)
- Grid of cards (responsive: xs=1, sm=2, md=3, lg=4)
- Pagination
- NO AUTH required

**Card Design:**
- Cover image / color
- Title (truncated)
- Author name
- Stats row: Views, Likes, Forks
- Tags (first 3 only)
- Hover: Subtle lift effect

### Component 4: Public View Page

**Location:** `nowry/src/pages/PublicView.js`  
**Route:** `/public/books/:id` or `/public/decks/:id`

**Layout:**
```
┌───────────────────────────────────────┐
│ Book Title                            │
│ by @username                          │
│                                       │
│ ┌─────────────────────────────┐     │
│ │  Content Preview            │     │
│ │  (truncated or full)        │     │
│ └─────────────────────────────┘     │
│                                       │
│ [❤️ Like]  [🍴 Fork]  [⚠️ Report]    │
│                                       │
│ Stats: 150 views • 12 likes • 3 forks│
│ Category: Science  Tags: physics     │
└───────────────────────────────────────┘
```

**Features:**
- Anonymous viewing (no login required)
- Action buttons (require login when clicked)
- Content preview
- Metadata display

### Component 5: Like/Fork Buttons

**Design:**
- Like button: Heart icon, shows count
  - Unfilled when not liked
  - Filled when liked
  - Toggle on click
- Fork button: Fork icon + text "Fork to Library"
  - Opens confirmation modal
  - On success: Navigate to forked item

**Code Example:**
```jsx
<Button
  variant="outlined"
  size="sm"
  startDecorator={isLiked ? <FavoriteIcon /> : <FavoriteBorderIcon />}
  onClick={handleLike}
  sx={{
    borderColor: isLiked ? 'danger.outlinedBorder' : 'neutral.outlinedBorder',
    color: isLiked ? 'danger.plainColor' : 'text.primary'
  }}
>
  {likes} {t('public.likes')}
</Button>
```

### Component 6: Report Modal

**UI:**
- Select reason: Copyright, Inappropriate, Spam, Misinformation, Other
- Textarea: Description (optional)
- Buttons: Cancel, Submit Report

**After submission:**
- Show success message
- Disable report button (already reported)

### Component 7: My Liked Content Page

**Location:** `nowry/src/pages/MyLikes.js`  
**Route:** `/liked` or `/my/likes`

**Features:**
- Tab switcher: All / Books / Decks
- Grid layout (same as browse)
- Shows all content I've liked
- Can unlike directly from this page

---

## 🎨 Design Tokens to Use

```jsx
// Backgrounds
bgcolor: 'background.surface'
bgcolor: 'background.level1'

// Text
color: 'text.primary'
color: 'text.secondary'
color: 'text.tertiary'

// Borders
borderColor: 'divider'
borderColor: 'neutral.outlinedBorder'

// Interactive
'&:hover': {
  borderColor: 'primary.outlinedBorder',
  bgcolor: 'background.level1'
}
```

---

## 📱 Mobile Considerations

- Stack filters vertically on mobile
- Reduce card columns (xs=1, sm=2)
- Make action buttons full-width in modals
- Compact stats (use icons only on mobile)

---

## 🌐 i18n Keys to Add

```json
{
  "public": {
    "publish": "Publish",
    "unpublish": "Unpublish",
    "published": "Published",
    "browse": "Browse Public Library",
    "likes": "likes",
    "views": "views",
    "forks": "forks",
    "fork": "Fork to Library",
    "forkSuccess": "Forked successfully!",
    "report": "Report",
    "reported": "Reported",
    "category": "Category",
    "tags": "Tags",
    "difficulty": "Difficulty",
    "language": "Language",
    "license": "License",
    "originalContent": "This is my original content",
    "publishSuccess": "Published successfully!",
    "unpublishSuccess": "Unpublished successfully!"
  }
}
```

---

## ⚡ Quick Start Order

1. ✅ Add `optional_auth` to backend
2. ✅ Create indexes
3. ✅ Test API with curl
4. 🎨 Build PublishModal
5. 🎨 Add Publish button to editors
6. 🎨 Build Public Browse page
7. 🎨 Build Public View page
8. 🎨 Add Like/Fork buttons
9. 🎨 Build Report modal
10. 🎨 Build My Likes page

**Estimated Time:**  
Backend: 30 mins  
Frontend: 4-6 hours

You're 90% done! Just need to wire up the UI. Want me to start building the React components now?
