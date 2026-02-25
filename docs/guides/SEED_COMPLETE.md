# Test User Seed Script - Complete! ✅

## 🎉 What Was Created

### 1. Seed Script (`seed_test_user.py`)
Comprehensive Python script that creates a full test user with realistic data.

**Creates:**
- ✅ Test user with dev role and beta access
- ✅ Books from all `.md` documentation files (49 files)
- ✅ Annual planning data (plan, focus areas, goals, priorities, activities)
- ✅ Flashcard decks (3 decks with 11 cards)
- ✅ Daily routine templates (2 routines)
- ✅ Tasks (6 tasks with various statuses)

### 2. Helper Script (`seed.sh`)
Bash script to make running the seed easy:
```bash
./seed.sh
```

### 3. Documentation (`SEED_TEST_USER_README.md`)
Complete guide with:
- Usage instructions
- Customization options
- Troubleshooting
- Cleanup procedures

### 4. User Model Update
Added role and beta fields to User model:
```python
class User(BaseModel, SoftDeleteMixin):
    role: Optional[str] = "user"  # user, dev, admin
    is_beta: Optional[bool] = False  # Beta tester flag
```

---

## 🚀 Quick Start

### Run the Seed Script

```bash
cd Nowry-API
./seed.sh
```

Or directly:
```bash
cd Nowry-API
python3 seed_test_user.py
```

### Test User Credentials

```
📧 Email: dev@nowry.com
👤 Username: DevTestUser
🔑 Firebase UID: dev-test-user-123456
🎭 Role: dev
🧪 Beta: true
```

---

## 📊 What Gets Seeded

### User Profile
- **Role**: `dev` (full access to all features)
- **Beta**: `true` (early access features)
- **Subscription**: Premium (unlimited everything)
- **Preferences**: All configured with sensible defaults

### Documentation Books (~49 books)
All your `.md` documentation files become books:

| Category | Examples |
|----------|----------|
| **Design & UX** | DESIGN_GUIDELINES.md, COLOR_SYSTEM.md |
| **Technical** | API_REFACTOR_COMPLETE.md, CONTENT_FIRST_ARCHITECTURE.md |
| **Features** | BUG_REPORTING_SYSTEM.md, STUDY_SYSTEM_IMPLEMENTATION.md |
| **Planning** | SUBSCRIPTION_SYSTEM_PLAN.md, AUTH_ONBOARDING_ENHANCEMENT.md |
| **Deployment** | DEPLOYMENT_GUIDE.md, INFRASTRUCTURE.md |
| **Documentation** | CASCADE_DELETION_COMPLETE.md, SOFT_DELETE_GUIDE.md |

**All books are published** with:
- ✅ Realistic engagement metrics (views, likes, forks)
- ✅ Proper categorization
- ✅ Tags from file path
- ✅ Auto-generated summaries

### Annual Planning (2026)
- **1 Annual Plan**: "Growth & Innovation"
- **3 Focus Areas**: 
  - 🚀 Product Development
  - 🎨 User Experience  
  - 📈 Growth & Marketing
- **5 Priorities**: Distributed across focus areas
- **5 Goals**: Quarterly goals with progress (0-100%)
- **5 Activities**: Daily/weekly activities linked to goals

### Flashcards
- **3 Decks**:
  - Python Programming Basics (5 flashcards)
  - Web Design Principles (4 flashcards)
  - JavaScript ES6+ Features (2 quiz cards)
- Mix of public and private decks
- Realistic engagement metrics

### Daily Routines
- **Morning Routine**: 5 time slots
- **Work Focus Block**: Deep work sessions with breaks

### Tasks
- **6 Tasks** with varied:
  - Status: todo, in_progress, completed
  - Priority: low, medium, high
  - Due dates: Next 7 days

---

## 🎯 Use Cases

### 1. Testing Public Content Sharing
```
Login as dev@nowry.com → Browse → See 49 published books
```

### 2. Testing Annual Planning
```
Navigate to Annual Planning → See pre-populated goals & focus areas
```

### 3. Testing Flashcards
```
Navigate to Cards → See 3 pre-created decks → Study mode
```

### 4. Testing Book Editor
```
Go to Books → Open any documentation book → Edit, Save, Publish
```

### 5. UI/UX Testing
- Test with real content (not Lorem Ipsum)
- Every feature has data
- Realistic engagement metrics

---

## 🔧 Features

### ✅ Idempotent
- Safe to run multiple times
- Updates existing user
- Skips duplicate books

### 🎲 Realistic Data
- Random engagement metrics
- Varied statuses and priorities
- Different categories and tags
- Natural content distribution

### 🔒 Dev-Restricted Content
All published books are dev-restricted (visible only to dev/admin users).

**Future Enhancement**: Add `is_dev_content` flag to public_metadata to filter by user role.

### 📝 Auto-Categorization
Books are automatically categorized based on their directory:
```
nowry/docs/design/ → "Design & UX"
nowry/docs/technical/ → "Technical"
nowry/docs/features/ → "Features"
```

---

## 🐛 Troubleshooting

### "Module not found"
```bash
cd Nowry-API  # Make sure you're in the right directory
```

### "Cannot connect to database"
```bash
# Start MongoDB
docker-compose up -d mongodb

# Or check connection
mongo --eval "db.adminCommand('ping')"
```

### "Permission denied" on seed.sh
```bash
chmod +x seed.sh
```

### Firebase Authentication
The script creates a **database user only**. For full auth:
1. Create Firebase user manually (Firebase Console)
2. Update `firebase_uid` in script
3. Or use Firebase Emulator

---

## 🧹 Cleanup

### Remove all test data
```javascript
// MongoDB shell
db.users.deleteOne({ email: "dev@nowry.com" })
db.books.deleteMany({ user_id: "<user_id>" })
db.decks.deleteMany({ user_id: "<user_id>" })
// ... etc
```

### Or use soft delete
Just delete the user account in the app - cascade deletion handles everything!

---

## 🔮 Future Enhancements

- [ ] Dev-restricted visibility flag on public_metadata
- [ ] Support multiple test users
- [ ] Command-line arguments (--clean, --skip-books, etc.)
- [ ] Mock user interactions (comments, ratings)
- [ ] Generate study history
- [ ] Add mock visualizer diagrams
- [ ] Create mock news articles
- [ ] Bulk import from CSV/JSON

---

## 📝 Files Created

1. `/Nowry-API/seed_test_user.py` - Main seed script
2. `/Nowry-API/seed.sh` - Helper bash script
3. `/Nowry-API/SEED_TEST_USER_README.md` - Full documentation
4. `/Nowry-API/SEED_COMPLETE.md` - This summary
5. `/Nowry-API/app/models/User.py` - Updated with role & beta fields

---

## ✅ Ready to Use!

Run the script and get instant test data:

```bash
cd Nowry-API
./seed.sh
```

**That's it!** You now have a fully populated test user ready for development and testing. 🎉

---

## 🔗 Related Documentation

- `SEED_TEST_USER_README.md` - Full usage guide
- `CASCADE_DELETION_COMPLETE.md` - Soft delete behavior
- `PUBLIC_CONTENT_FEATURE.md` - Public sharing details
- `SOFT_DELETE_GUIDE.md` - Soft delete implementation
