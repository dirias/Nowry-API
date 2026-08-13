# Test User - Quick Reference 🚀

## Run Seed Script

```bash
cd Nowry-API
./seed.sh
```

## Test User

```
📧 dev@nowry.com
🎭 Role: dev
🧪 Beta: true
🔑 Firebase UID: dev-test-user-123456
```

## What Gets Created

| Category | Count | Details |
|----------|-------|---------|
| 📚 **Books** | ~49 | All `.md` docs, published |
| 📅 **Annual Plan** | 1 | 2026 plan |
| 🎯 **Focus Areas** | 3 | Product, UX, Growth |
| ⭐ **Priorities** | 5 | Across focus areas |
| 🎖️ **Goals** | 5 | Quarterly with progress |
| 📋 **Activities** | 5 | Daily/weekly routines |
| 🎴 **Decks** | 3 | Python, Design, JS |
| 💳 **Cards** | 11 | Flashcards & quizzes |
| ⏰ **Routines** | 2 | Morning & work blocks |
| ✅ **Tasks** | 6 | Mixed statuses |

## Features

✅ Idempotent (safe to re-run)  
✅ Realistic data & metrics  
✅ All published (dev-restricted)  
✅ Auto-categorized  
✅ Full coverage of features

## Quick Test

```bash
# 1. Seed data
./seed.sh

# 2. Start API
uvicorn app.main:app --reload

# 3. Start frontend
cd ../nowry && npm start

# 4. Login
# Email: dev@nowry.com
# (Create Firebase user manually)
```

## Cleanup

```javascript
// MongoDB shell
db.users.deleteOne({ email: "dev@nowry.com" })
```

Or just delete account in app (cascade delete).

## Files

- `seed_test_user.py` - Main script
- `seed.sh` - Helper runner
- `SEED_TEST_USER_README.md` - Full docs
- `SEED_COMPLETE.md` - Summary

## Troubleshooting

| Issue | Solution |
|-------|----------|
| Module not found | `cd Nowry-API` |
| DB connection | `docker-compose up -d mongodb` |
| Permission denied | `chmod +x seed.sh` |
| Firebase auth | Create user manually in Firebase Console |

---

**Need help?** See `SEED_TEST_USER_README.md` for full documentation.
