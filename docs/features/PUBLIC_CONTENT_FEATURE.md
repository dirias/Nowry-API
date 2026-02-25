# Public Content Sharing - Feature Documentation

## Overview

The Public Content Sharing feature allows users to share their Books and Decks with the Nowry community. Other users can discover, view, like, and fork (clone) public content to their own libraries.

**Status:** ✅ MVP Complete  
**Version:** 1.0.0  
**Release Date:** January 2025

---

## Table of Contents

1. [Features](#features)
2. [Architecture](#architecture)
3. [Data Models](#data-models)
4. [API Endpoints](#api-endpoints)
5. [User Workflows](#user-workflows)
6. [Security & Privacy](#security--privacy)
7. [Content Moderation](#content-moderation)
8. [Database Indexes](#database-indexes)
9. [Frontend Integration](#frontend-integration)
10. [Future Enhancements](#future-enhancements)

---

## Features

### Core Features (MVP)

✅ **Public Publishing**
- Make Books and Decks public with one click
- Add metadata (category, tags, difficulty, language)
- Choose license type (All Rights Reserved, CC-BY, CC-BY-SA, CC0)
- Unpublish anytime

✅ **Discovery & Browse**
- Browse public Books and Decks (no login required)
- Filter by category, tags, language, difficulty
- Search by title, description, tags
- Sort by recent, popular, or top-rated

✅ **Engagement**
- Like/unlike public content (requires login)
- View tracking for analytics
- View count displayed on content

✅ **Forking (Cloning)**
- Clone public content to your library
- Creates private copy with attribution
- Tracks fork count

✅ **Content Reporting**
- Report inappropriate content
- Categories: copyright, inappropriate, spam, misinformation, other
- Moderation queue for admins

### Coming Soon (Phase 2+)

🔜 **Comments & Reviews**
- Leave comments on public content
- Rate content (1-5 stars)
- Reply to comments

🔜 **Creator Profiles**
- View creator's public content
- Follow creators
- Creator statistics

🔜 **Collections**
- Group related content
- Featured collections
- Curated lists

🔜 **Monetization**
- Premium content (paid)
- Creator revenue sharing
- Sponsored content

---

## Architecture

### High-Level Flow

```
User Creates Content (Book/Deck)
    ↓
User Clicks "Publish"
    ↓
Content becomes public with metadata
    ↓
Other users discover content
    ↓
Engagement (views, likes, forks)
    ↓
Analytics tracked
```

### Components

1. **Models** (`app/models/PublicContent.py`)
   - `PublicMetadata` - Engagement metrics and discovery info
   - `ContentReport` - User reports for moderation
   - `ContentFork` - Tracking forks/clones
   - `ContentLike` - User likes/favorites
   - `ContentView` - View tracking (optional)

2. **Service Layer** (`app/services/public_content_service.py`)
   - `PublicContentService` - Business logic for publishing, browsing, forking

3. **API Routers**
   - `app/routers/public_content.py` - Public browsing, publishing, engagement
   - `app/routers/moderation.py` - Content reporting and moderation

4. **Updated Models**
   - `Book` - Added `is_public`, `published_at`, `public_metadata`
   - `Deck` - Added `is_public`, `published_at`, `public_metadata`

---

## Data Models

### Book & Deck (Updated)

```python
class Book(BaseModel, SoftDeleteMixin):
    # ... existing fields ...
    
    # NEW: Public Sharing
    is_public: bool = False
    published_at: Optional[datetime] = None
    public_metadata: Optional[PublicMetadata] = None
```

### PublicMetadata

```python
class PublicMetadata(BaseModel):
    # Engagement
    views: int = 0
    likes: int = 0
    forks: int = 0
    downloads: int = 0
    
    # Discovery
    category: Optional[str] = None  # "Science", "Math", "Languages"
    tags: List[str] = []
    language: str = "en"
    difficulty_level: Optional[str] = None  # "beginner", "intermediate", "advanced"
    
    # Quality
    average_rating: float = 0.0
    rating_count: int = 0
    
    # Legal
    license_type: str = "all_rights_reserved"
    is_original_content: bool = True
    original_source: Optional[str] = None
    attribution: Optional[str] = None
```

### ContentReport

```python
class ContentReport(BaseModel):
    content_type: str  # "book" or "deck"
    content_id: str
    reporter_user_id: str
    reason: str  # "copyright", "inappropriate", "spam", "misinformation", "other"
    description: Optional[str]
    status: str  # "pending", "under_review", "resolved", "dismissed"
    reviewed_by: Optional[str]
    action_taken: Optional[str]
```

---

## API Endpoints

### Public Browse (No Auth Required)

#### GET `/public/books`
Browse public books with filters.

**Query Parameters:**
- `category` (optional) - Filter by category
- `tags` (optional) - Comma-separated tags
- `language` (optional) - Language code (en, es, fr, de, ja)
- `difficulty` (optional) - beginner, intermediate, advanced
- `search` (optional) - Search query
- `sort_by` (optional) - recent (default), popular, top_rated
- `page` (default: 1)
- `page_size` (default: 20, max: 100)

**Response:**
```json
{
  "items": [...],
  "total": 150,
  "page": 1,
  "page_size": 20,
  "total_pages": 8
}
```

#### GET `/public/decks`
Same as `/public/books` but for decks.

#### GET `/public/books/{book_id}`
Get a single public book. Tracks view.

#### GET `/public/decks/{deck_id}`
Get a single public deck. Tracks view.

### Publishing (Auth Required)

#### POST `/public/books/{book_id}/publish`
Publish a book.

**Request Body:**
```json
{
  "category": "Science",
  "tags": ["physics", "quantum"],
  "language": "en",
  "difficulty_level": "intermediate",
  "license_type": "CC-BY",
  "is_original_content": true,
  "attribution": null
}
```

**Response:**
```json
{
  "message": "Book published successfully",
  "book": {...}
}
```

#### POST `/public/decks/{deck_id}/publish`
Same as book publish but for decks.

#### POST `/public/books/{book_id}/unpublish`
Make a book private again.

#### POST `/public/decks/{deck_id}/unpublish`
Make a deck private again.

### Engagement (Auth Required)

#### POST `/public/books/{book_id}/like`
Like a public book.

#### DELETE `/public/books/{book_id}/like`
Unlike a book.

#### POST `/public/decks/{deck_id}/like`
Like a public deck.

#### DELETE `/public/decks/{deck_id}/like`
Unlike a deck.

#### GET `/public/me/liked`
Get content I've liked.

**Query Parameters:**
- `content_type` (optional) - "book" or "deck"
- `page`, `page_size`

### Forking (Auth Required)

#### POST `/public/books/{book_id}/fork`
Clone a public book to your library.

**Response:**
```json
{
  "message": "Book forked successfully",
  "forked_book": {
    "_id": "new_id",
    "title": "Original Title (Forked)",
    "is_public": false,
    "user_id": "your_user_id"
  }
}
```

#### POST `/public/decks/{deck_id}/fork`
Clone a public deck to your library.

### Moderation

#### POST `/moderation/report/{content_type}/{content_id}`
Report inappropriate content.

**Request Body:**
```json
{
  "reason": "copyright",
  "description": "This contains copyrighted material from XYZ textbook"
}
```

#### GET `/moderation/reports`
Get my submitted reports.

#### GET `/moderation/admin/reports` (Admin Only)
Get all reports for moderation.

#### PUT `/moderation/admin/reports/{report_id}/review` (Admin Only)
Review and resolve a report.

---

## User Workflows

### Workflow 1: Publishing Content

```
1. User creates a Book or Deck
2. User clicks "Publish" button
3. Modal opens with publishing options:
   - Category selection
   - Tags input
   - Language selector
   - Difficulty level
   - License type
   - Original content checkbox
4. User fills form and confirms
5. API call: POST /public/books/{id}/publish
6. Content becomes public
7. Success message shown
```

### Workflow 2: Discovering Content

```
1. User (or anonymous visitor) visits Public Library page
2. Filters/searches for content:
   - Category dropdown
   - Tag chips
   - Search bar
   - Sort selector
3. Browse results in grid/list view
4. Click on item to view details
5. View page shows:
   - Content preview
   - Metadata (views, likes, forks)
   - Creator info
   - Action buttons (Like, Fork)
```

### Workflow 3: Forking Content

```
1. User finds interesting public content
2. User clicks "Fork" button
3. Confirmation dialog:
   "This will create a private copy in your library. Continue?"
4. API call: POST /public/books/{id}/fork
5. Copy created in user's library
6. Redirect to edit page of forked content
7. Toast notification: "Forked successfully! This is now your private copy."
```

### Workflow 4: Reporting Content

```
1. User views public content
2. User clicks "Report" button
3. Modal opens with report form:
   - Reason dropdown
   - Description textarea
4. User submits report
5. API call: POST /moderation/report/book/{id}
6. Confirmation: "Thank you. We'll review this report."
7. Admin receives notification
```

---

## Security & Privacy

### Access Control

**Public Browse:**
- ✅ No authentication required
- ✅ Only shows `is_public: true` content
- ✅ Respects soft delete (`deleted_at: null`)

**Publishing:**
- ✅ Requires authentication
- ✅ User must own the content
- ✅ Cannot publish deleted content

**Forking:**
- ✅ Requires authentication
- ✅ Creates private copy (not public by default)
- ✅ Original creator credited

**Moderation:**
- ✅ User reports tracked by user_id
- ✅ Admin endpoints (TODO: add role check)
- ✅ Report status tracked

### Privacy Considerations

**What's Public:**
- Content title/name
- Content body (full_content)
- Creator username (user_id)
- Metadata (category, tags, etc.)
- Engagement metrics (views, likes, forks)
- Published date

**What's Private:**
- User email (not exposed)
- Creation date (only published_at shown)
- Edit history
- Private notes/annotations

### Rate Limiting

**Recommended:**
```python
# Browse endpoints (no auth)
- 100 requests/minute per IP

# Publish/Unpublish
- 10 requests/minute per user

# Fork
- 20 requests/minute per user

# Like
- 50 requests/minute per user

# Report
- 5 requests/minute per user
```

---

## Content Moderation

### Report Reasons

1. **Copyright** - Copyright infringement
   - Action: DMCA takedown process
   - Priority: HIGH

2. **Inappropriate** - Offensive/NSFW content
   - Action: Immediate unpublish
   - Priority: HIGH

3. **Spam** - Low-quality or spam
   - Action: Unpublish + warning
   - Priority: MEDIUM

4. **Misinformation** - Factually incorrect
   - Action: Review + possible unpublish
   - Priority: MEDIUM

5. **Other** - Other issues
   - Action: Case-by-case review
   - Priority: LOW

### Moderation Workflow

```
Report Submitted
    ↓
Status: "pending"
    ↓
Admin reviews → Status: "under_review"
    ↓
Admin takes action:
    - "removed" → Content unpublished
    - "kept_public" → No action
    - "warned_creator" → Warning sent
    ↓
Status: "resolved"
    ↓
Notifications sent to reporter & creator
```

### Admin Actions

```python
# Unpublish content
POST /moderation/admin/content/book/{id}

# Review report
PUT /moderation/admin/reports/{report_id}/review

# View all reports
GET /moderation/admin/reports
```

---

## Database Indexes

### Required Indexes

```python
# Books
await db["books"].create_index([("is_public", 1)])
await db["books"].create_index([("published_at", -1)])
await db["books"].create_index([("public_metadata.category", 1)])
await db["books"].create_index([("public_metadata.tags", 1)])
await db["books"].create_index([("public_metadata.views", -1)])
await db["books"].create_index([("public_metadata.average_rating", -1)])

# Decks (same indexes)
await db["decks"].create_index([("is_public", 1)])
await db["decks"].create_index([("published_at", -1)])
# ... (same as books)

# Compound indexes for efficient querying
await db["books"].create_index([
    ("is_public", 1),
    ("deleted_at", 1),
    ("published_at", -1)
])

await db["books"].create_index([
    ("is_public", 1),
    ("public_metadata.category", 1),
    ("published_at", -1)
])

# Content Likes
await db["content_likes"].create_index([("user_id", 1), ("created_at", -1)])
await db["content_likes"].create_index([("content_id", 1), ("content_type", 1)])
await db["content_likes"].create_index([
    ("content_id", 1),
    ("content_type", 1),
    ("user_id", 1)
], unique=True)

# Content Forks
await db["content_forks"].create_index([("original_content_id", 1)])
await db["content_forks"].create_index([("forked_by_user_id", 1)])

# Content Reports
await db["content_reports"].create_index([("status", 1), ("created_at", -1)])
await db["content_reports"].create_index([("content_id", 1), ("content_type", 1)])

# Content Views (if implemented)
await db["content_views"].create_index([("content_id", 1), ("viewed_at", -1)])
await db["content_views"].create_index([("viewed_at", -1)])  # For cleanup
```

---

## Frontend Integration

### Components Needed

**1. PublishModal**
```tsx
<PublishModal
  contentType="book"
  contentId="123"
  onPublish={(metadata) => publishBook(metadata)}
/>
```

**2. PublicBrowsePage**
```tsx
<PublicBrowse
  contentType="book"
  filters={filters}
  onFilterChange={setFilters}
/>
```

**3. PublicContentCard**
```tsx
<PublicContentCard
  content={book}
  onLike={handleLike}
  onFork={handleFork}
  onReport={handleReport}
/>
```

**4. ReportModal**
```tsx
<ReportModal
  contentType="book"
  contentId="123"
  onReport={(data) => reportContent(data)}
/>
```

### Sample Frontend Code

```typescript
// Publish a book
const publishBook = async (bookId: string, metadata: PublishMetadata) => {
  const response = await fetch(`/api/public/books/${bookId}/publish`, {
    method: 'POST',
    headers: {
      'Authorization': `Bearer ${token}`,
      'Content-Type': 'application/json'
    },
    body: JSON.stringify(metadata)
  });
  
  if (!response.ok) throw new Error('Failed to publish');
  return response.json();
};

// Browse public books
const browseBooks = async (filters: BrowseFilters) => {
  const params = new URLSearchParams({
    category: filters.category || '',
    tags: filters.tags.join(','),
    search: filters.search || '',
    sort_by: filters.sortBy,
    page: filters.page.toString(),
    page_size: '20'
  });
  
  const response = await fetch(`/api/public/books?${params}`);
  return response.json();
};

// Fork a book
const forkBook = async (bookId: string) => {
  const response = await fetch(`/api/public/books/${bookId}/fork`, {
    method: 'POST',
    headers: {
      'Authorization': `Bearer ${token}`
    }
  });
  
  if (!response.ok) throw new Error('Failed to fork');
  return response.json();
};
```

---

## Future Enhancements

### Phase 2: Community Features
- [ ] Comments on public content
- [ ] Ratings & reviews (1-5 stars)
- [ ] Creator profiles
- [ ] Follow creators
- [ ] Activity feed

### Phase 3: Advanced Discovery
- [ ] Recommended content (AI-powered)
- [ ] Trending content
- [ ] Collections & playlists
- [ ] Featured content (curated)

### Phase 4: Monetization
- [ ] Premium content (paid)
- [ ] Creator subscriptions
- [ ] Revenue sharing (70/30 split)
- [ ] Sponsored content
- [ ] Affiliate links

### Phase 5: Collaboration
- [ ] Co-authoring
- [ ] Version control (fork → improve → submit PR)
- [ ] Content merging
- [ ] Contributor credits

### Phase 6: Analytics
- [ ] Creator dashboard
- [ ] Engagement metrics
- [ ] Audience insights
- [ ] A/B testing for content

---

## Testing Checklist

### Unit Tests
- [ ] PublicContentService methods
- [ ] Model validations
- [ ] Authorization checks

### Integration Tests
- [ ] Publish workflow (authenticated)
- [ ] Browse workflow (anonymous)
- [ ] Fork workflow
- [ ] Report workflow
- [ ] Like/unlike workflow

### E2E Tests
- [ ] User publishes book
- [ ] Anonymous user browses and views
- [ ] Authenticated user forks content
- [ ] User reports inappropriate content
- [ ] Admin reviews and resolves report

---

## Deployment Checklist

### Before Launch
- [ ] Add database indexes
- [ ] Set up rate limiting
- [ ] Configure CORS for public endpoints
- [ ] Add admin role checking
- [ ] Set up monitoring (views, forks, reports)
- [ ] Legal review (Terms of Service, DMCA)
- [ ] Privacy policy update

### Launch Day
- [ ] Enable feature flag
- [ ] Monitor error rates
- [ ] Watch moderation queue
- [ ] Track engagement metrics

### Post-Launch
- [ ] Gather user feedback
- [ ] Monitor performance
- [ ] Iterate on discovery algorithm
- [ ] Plan Phase 2 features

---

## Support & Troubleshooting

### Common Issues

**Issue:** Content not appearing in browse
- **Check:** `is_public: true` and `deleted_at: null`
- **Check:** Indexes created

**Issue:** Fork button not working
- **Check:** User is authenticated
- **Check:** Original content is public
- **Check:** User has permission to create content

**Issue:** Reports not showing up
- **Check:** Admin role configured
- **Check:** Moderation endpoints working

---

## Metrics to Track

### Key Metrics
- Public content count (books & decks)
- Views per content item
- Fork rate (forks / views)
- Like rate (likes / views)
- Reports per 1000 views
- Time to first fork
- Creator adoption rate

### Success Criteria
- **Month 1:** 100+ public items
- **Month 3:** 1000+ public items, 10% fork rate
- **Month 6:** 5000+ public items, <1% report rate

---

**Last Updated:** January 24, 2025  
**Version:** 1.0.0  
**Status:** ✅ MVP Complete
