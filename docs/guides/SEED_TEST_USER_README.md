# Test User Seed Script

## Overview

This script creates a comprehensive test user with mock data for development and testing purposes.

## What It Creates

### 1. Test User Profile
- **Email**: `dev@nowry.com`
- **Username**: `DevTestUser`
- **Firebase UID**: `dev-test-user-123456` (fake for dev)
- **Role**: `dev` (developer role with full access)
- **Beta**: `true` (beta tester with early access to features)
- **Subscription**: Premium (unlimited everything)
- **Preferences**: All settings configured

### 2. Documentation Books
- Creates books from **all `.md` documentation files**
- Automatically categorizes by directory:
  - Design & UX
  - Technical
  - Features
  - Planning
  - Deployment
  - General Documentation
- All books are **published** (with dev-restricted visibility)
- Includes realistic engagement metrics (views, likes, forks)

### 3. Annual Planning Data
- **Annual Plan**: 2026 plan with theme and vision
- **Focus Areas**: 3 areas (Product, UX, Growth)
- **Priorities**: 5 priorities across focus areas
- **Goals**: 5 quarterly goals with progress tracking
- **Activities**: 5 recurring activities linked to goals

### 4. Flashcard Decks & Cards
- **3 decks** with different types:
  - Python Programming (5 flashcards)
  - Web Design Principles (4 flashcards)
  - JavaScript ES6+ (2 quiz cards)
- Mix of **public** and **private** decks
- Realistic engagement metrics

### 5. Daily Routines
- **Morning Routine** template
- **Work Focus Block** template
- Complete time slots with activities

### 6. Tasks
- **6 tasks** with different statuses:
  - 2 in progress
  - 3 todo
  - 1 completed
- Various priority levels (high, medium, low)

---

## Usage

### Prerequisites

1. **MongoDB running** (Docker or local)
2. **Python environment** with dependencies installed
3. **Correct database configuration** in `app/config/database.py`

### Running the Script

```bash
# From the Nowry-API directory
cd Nowry-API

# Run the seed script
python seed_test_user.py
```

### Output

```
============================================================
🚀 NOWRY TEST USER SEED SCRIPT
============================================================
🔧 Creating/updating test user...
   ✓ Created new test user (ID: 67...)

📚 Creating books from documentation files...
   Found 49 documentation files
   ✓ Created 49 books from documentation

📅 Creating annual planning data...
   ✓ Created annual plan for 2026
   ✓ Created 3 focus areas
   ✓ Created 5 priorities
   ✓ Created 5 goals
   ✓ Created 5 activities

🎴 Creating flashcard decks and cards...
   ✓ Created 3 decks with 11 cards

⏰ Creating daily routine templates...
   ✓ Created 2 daily routine templates

✅ Creating tasks...
   ✓ Created 6 tasks

============================================================
✅ TEST USER SEED COMPLETE!
============================================================

📧 Email: dev@nowry.com
👤 Username: DevTestUser
🔑 Firebase UID: dev-test-user-123456
🎭 Role: dev
🧪 Beta: true

💡 This user has full access to all features and dev-restricted content
============================================================
```

---

## Features

### ✅ Idempotent
- Safe to run multiple times
- Updates existing user instead of creating duplicates
- Skips existing books to avoid duplicates

### 📊 Realistic Data
- Random engagement metrics (views, likes, forks)
- Varied statuses (in progress, completed, todo)
- Different difficulty levels
- Multiple categories

### 🔒 Dev-Restricted Content
- All books published with dev-restricted visibility
- Only dev/admin users can see this content
- Perfect for testing without cluttering public content

### 🎨 Comprehensive Coverage
- Tests all major features:
  - Books with public sharing
  - Flashcard decks (flashcards + quizzes)
  - Annual planning system
  - Daily routines
  - Tasks
  - User preferences

---

## Customization

### Change Test User Email

```python
TEST_USER = {
    "email": "your-email@example.com",  # Change this
    # ...
}
```

### Change User Role

```python
TEST_USER = {
    "role": "admin",  # user, dev, admin
    # ...
}
```

### Disable Beta Access

```python
TEST_USER = {
    "is_beta": False,
    # ...
}
```

### Add More Books

Add `.md` files anywhere in the project:
- Script automatically finds all `.md` files
- Categorizes by directory structure
- Extracts title from first `#` heading

### Customize Annual Planning

Edit the data in `create_annual_planning_data()`:
```python
focus_areas_data = [
    {
        "name": "Your Focus Area",
        "description": "Description",
        "color": "#0b6bcb",
        "icon": "🚀"
    },
    # Add more...
]
```

---

## Firebase Authentication

### Important Notes

1. **This creates a database user ONLY**
   - The script does NOT create a Firebase Auth user
   - Firebase UID is fake: `dev-test-user-123456`
   
2. **For Full Authentication:**
   - Create a real Firebase user via Firebase Console:
     - Email: `dev@nowry.com`
     - Password: (your choice)
   - Update the `firebase_uid` in the script with the real UID
   - Or use Firebase Admin SDK to create programmatically

3. **For Local Testing:**
   - Use Firebase Emulator Suite
   - Mock Firebase authentication
   - Or bypass auth middleware temporarily

---

## Troubleshooting

### Error: "Module not found"
```bash
# Make sure you're in the Nowry-API directory
cd Nowry-API
python seed_test_user.py
```

### Error: "Cannot connect to database"
```bash
# Start MongoDB (Docker example)
docker-compose up -d mongodb

# Or check if MongoDB is running locally
mongo --eval "db.adminCommand('ping')"
```

### Error: "Permission denied" reading files
```bash
# Check file permissions
ls -la /path/to/Nowry/

# Fix if needed
chmod -R u+r /path/to/Nowry/
```

### Books not appearing
- Check `deleted_at` field is `null`
- Check `user_id` matches test user ID
- Check `is_public` is set correctly

---

## Cleanup

### Remove Test User and All Data

```javascript
// In MongoDB shell or Compass
db.users.deleteOne({ email: "dev@nowry.com" })
db.books.deleteMany({ user_id: "<user_id>" })
db.decks.deleteMany({ user_id: "<user_id>" })
db.cards.deleteMany({ user_id: "<user_id>" })
db.annual_plans.deleteMany({ user_id: "<user_id>" })
db.focus_areas.deleteMany({ user_id: "<user_id>" })
db.goals.deleteMany({ user_id: "<user_id>" })
db.priorities.deleteMany({ user_id: "<user_id>" })
db.activities.deleteMany({ user_id: "<user_id>" })
db.daily_routines.deleteMany({ user_id: "<user_id>" })
db.tasks.deleteMany({ user_id: "<user_id>" })
```

### Or Use Soft Delete
```bash
# In the app, just delete the user account
# All data will be soft-deleted via cascade
```

---

## Development Workflow

### 1. Fresh Database
```bash
# Start fresh MongoDB
docker-compose down -v
docker-compose up -d mongodb

# Run seed script
python seed_test_user.py
```

### 2. Testing Public Content
- Login as dev user (dev@nowry.com)
- Browse to `/browse` page
- See all documentation books published
- Test like, fork, report features

### 3. Testing Annual Planning
- Navigate to Annual Planning page
- See pre-populated goals, focus areas, priorities
- Test editing, updating progress, etc.

### 4. Testing Flashcards
- Navigate to Cards/Decks page
- See pre-created decks
- Test studying, adding cards, etc.

---

## Future Enhancements

- [ ] Add more diverse book content
- [ ] Create mock user interactions (likes, forks)
- [ ] Add mock bug reports
- [ ] Create mock visualizer diagrams
- [ ] Add mock news articles
- [ ] Generate realistic study history
- [ ] Add command-line arguments for customization
- [ ] Support multiple test users
- [ ] Add cleanup command

---

## Related Documentation

- `/Nowry-API/CASCADE_DELETION_COMPLETE.md` - Soft delete behavior
- `/Nowry-API/PUBLIC_CONTENT_FEATURE.md` - Public sharing details
- `/Nowry-API/SOFT_DELETE_GUIDE.md` - Soft delete implementation
- `/nowry/docs/design/DESIGN_GUIDELINES.md` - UI/UX standards

---

## License

Internal development tool - not for production use.
