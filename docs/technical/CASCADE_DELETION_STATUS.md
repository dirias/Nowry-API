# Cascade Deletion Status & Implementation

## ❌ Current Status: NOT Implemented

### What's Missing:

1. **Books/Decks** - NO cascade to cards
2. **Focus Areas** - NO cascade to goals/priorities (actually blocks deletion!)
3. **Goals** - NO cascade to activities
4. **User Account** - Uses HARD delete, not soft delete!

---

## 🔴 CRITICAL ISSUE: User Account Deletion

### Current Implementation (users.py, line 487-498):

```python
@router.delete("/account")
async def delete_account(current_user: dict = Depends(get_firebase_user)):
    """Delete user account and all associated data"""
    user_id = current_user["user_id"]
    
    # ❌ HARD DELETE - PERMANENT DATA LOSS!
    await books_collection.delete_many({"user_id": user_id})
    await study_cards_collection.delete_many({"user_id": user_id})
    await decks_collection.delete_many({"user_id": user_id})
    
    # ❌ HARD DELETE USER
    result = await users_collection.delete_one({"_id": ObjectId(user_id)})
```

**Problems:**
- ❌ Hard deletes all data (cannot be recovered)
- ❌ Violates soft delete pattern
- ❌ Not SOC 2 compliant (no audit trail)
- ❌ Not GDPR compliant (no grace period)
- ❌ Doesn't handle public content properly
- ❌ Doesn't cascade to all related data

---

## 🔴 CRITICAL ISSUE: Focus Area Deletion

### Current Implementation (annual_planning.py, lines 251-284):

```python
@router.delete("/focus-areas/{id}")
async def delete_focus_area(...):
    # Check if there are related goals
    goals_count = await goals_collection.count_documents({"focus_area_id": id})
    priorities_count = await priorities_collection.count_documents({"focus_area_id": id})
    
    # ❌ BLOCKS DELETION instead of cascading
    if goals_count > 0 or priorities_count > 0:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot delete focus area. It has {goals_count} goal(s)..."
        )
    
    # ❌ HARD DELETE
    await focus_areas_collection.delete_one({"_id": ObjectId(id)})
```

**Problems:**
- ❌ Blocks deletion instead of cascading
- ❌ Forces manual deletion of children first
- ❌ Uses hard delete, not soft delete
- ❌ Poor UX (user must delete goals first)

---

## ✅ RECOMMENDED IMPLEMENTATION

### 1. User Account Deletion (Soft Delete + Cascade)

```python
@router.delete("/account")
async def delete_account(current_user: dict = Depends(get_firebase_user)):
    """
    Soft delete user account and all associated data.
    Data can be recovered within 30 days before permanent deletion.
    """
    from datetime import datetime
    
    user_id = current_user["user_id"]
    now = datetime.utcnow()
    
    # 1. Soft delete user account
    await users_collection.update_one(
        {"_id": ObjectId(user_id)},
        {
            "$set": {
                "deleted_at": now,
                "deleted_by": user_id,
                "updated_at": now
            }
        }
    )
    
    # 2. Cascade soft delete to all user content
    soft_delete_update = {
        "$set": {
            "deleted_at": now,
            "deleted_by": user_id,
            "is_public": False,  # Auto-unpublish
            "updated_at": now
        }
    }
    
    # Books (and their public metadata)
    await books_collection.update_many(
        {"user_id": user_id, "deleted_at": None},
        soft_delete_update
    )
    
    # Decks (and auto-unpublish)
    await decks_collection.update_many(
        {"user_id": user_id, "deleted_at": None},
        soft_delete_update
    )
    
    # Study Cards
    await study_cards_collection.update_many(
        {"user_id": user_id, "deleted_at": None},
        soft_delete_update
    )
    
    # Annual Plans
    await annual_plans_collection.update_many(
        {"user_id": user_id, "deleted_at": None},
        soft_delete_update
    )
    
    # Focus Areas
    await focus_areas_collection.update_many(
        {"user_id": user_id, "deleted_at": None},
        soft_delete_update
    )
    
    # Goals
    await goals_collection.update_many(
        {"user_id": user_id, "deleted_at": None},
        soft_delete_update
    )
    
    # Priorities
    await priorities_collection.update_many(
        {"user_id": user_id, "deleted_at": None},
        soft_delete_update
    )
    
    # Daily Routines
    await daily_routines_collection.update_many(
        {"user_id": user_id, "deleted_at": None},
        soft_delete_update
    )
    
    return {
        "message": "Account deleted successfully. Data can be recovered within 30 days.",
        "recovery_deadline": (datetime.utcnow() + timedelta(days=30)).isoformat()
    }
```

### 2. Focus Area Deletion (Cascade to Children)

```python
@router.delete("/focus-areas/{id}")
async def delete_focus_area(
    id: str,
    current_user: dict = Depends(get_firebase_user),
):
    """
    Soft delete a focus area and cascade to related goals, activities, and priorities.
    """
    from datetime import datetime
    
    user_id = current_user["user_id"]
    now = datetime.utcnow()
    
    # 1. Verify ownership
    focus_area = await focus_areas_collection.find_one({
        "_id": ObjectId(id),
        "user_id": user_id,
        "deleted_at": None
    })
    
    if not focus_area:
        raise HTTPException(status_code=404, detail="Focus area not found")
    
    soft_delete_update = {
        "$set": {
            "deleted_at": now,
            "deleted_by": user_id,
            "updated_at": now
        }
    }
    
    # 2. Soft delete the focus area
    await focus_areas_collection.update_one(
        {"_id": ObjectId(id)},
        soft_delete_update
    )
    
    # 3. CASCADE: Soft delete all related goals
    await goals_collection.update_many(
        {"focus_area_id": id, "deleted_at": None},
        soft_delete_update
    )
    
    # 4. CASCADE: Soft delete all related priorities
    await priorities_collection.update_many(
        {"focus_area_id": id, "deleted_at": None},
        soft_delete_update
    )
    
    # 5. CASCADE: Soft delete activities (nested in goals)
    # This happens automatically when goals are queried with deleted_at filter
    
    return {"message": "Focus area and all related data deleted successfully"}
```

### 3. Goal Deletion (Cascade to Activities)

```python
@router.delete("/goals/{id}")
async def delete_goal(id: str, current_user: dict = Depends(get_firebase_user)):
    """
    Soft delete a goal and cascade to related activities.
    """
    from datetime import datetime
    
    user_id = current_user["user_id"]
    now = datetime.utcnow()
    
    # 1. Verify ownership
    goal = await goals_collection.find_one({
        "_id": ObjectId(id),
        "user_id": user_id,  # Add user check
        "deleted_at": None
    })
    
    if not goal:
        raise HTTPException(status_code=404, detail="Goal not found")
    
    soft_delete_update = {
        "$set": {
            "deleted_at": now,
            "deleted_by": user_id,
            "updated_at": now
        }
    }
    
    # 2. Soft delete the goal
    await goals_collection.update_one(
        {"_id": ObjectId(id)},
        soft_delete_update
    )
    
    # 3. CASCADE: Soft delete all related activities
    await activities_collection.update_many(
        {"goal_id": id, "deleted_at": None},
        soft_delete_update
    )
    
    return {"message": "Goal and all activities deleted successfully"}
```

### 4. Deck Deletion (Cascade to Cards)

```python
@router.delete("/{id}")
async def delete_deck(
    id: str,
    collection: Collection = Depends(get_decks_collection),
    user: dict = Depends(get_firebase_user),
):
    """
    Soft delete a deck and cascade to all its cards.
    """
    from bson import ObjectId
    from datetime import datetime
    
    user_id = user.get("user_id")
    now = datetime.utcnow()
    
    # Verify ownership
    try:
        existing_deck = await collection.find_one({"_id": ObjectId(id)})
    except Exception:
        existing_deck = await collection.find_one({"id": id})
    
    if not existing_deck:
        raise HTTPException(status_code=404, detail="Deck not found")
    
    if str(existing_deck.get("user_id")) != str(user_id):
        raise HTTPException(
            status_code=403, detail="Not authorized to delete this deck"
        )
    
    soft_delete_update = {
        "$set": {
            "deleted_at": now,
            "deleted_by": user_id,
            "is_public": False,  # Auto-unpublish
            "updated_at": now
        }
    }
    
    # 1. Soft delete the deck
    await collection.update_one(
        {"_id": existing_deck["_id"]},
        soft_delete_update
    )
    
    # 2. CASCADE: Soft delete all cards in this deck
    from app.config.database import cards_collection
    await cards_collection.update_many(
        {"deck_id": str(existing_deck["_id"]), "deleted_at": None},
        soft_delete_update
    )
    
    return None
```

---

## 📊 Cascade Relationship Table

| Parent | Children | Current Status | Should Cascade? |
|--------|----------|---------------|-----------------|
| **User Account** | All user data | ❌ Hard delete | ✅ YES (soft) |
| **Book** | Study cards from book | ❌ No cascade | ⚠️ Maybe* |
| **Deck** | Study cards in deck | ❌ No cascade | ✅ YES |
| **Focus Area** | Goals, Priorities | ❌ Blocks delete | ✅ YES |
| **Goal** | Activities | ❌ No cascade | ✅ YES |
| **Annual Plan** | Focus Areas, Priorities | ❌ No cascade | ✅ YES |

*Cards from books might be in multiple decks - need to decide on behavior

---

## 🎯 Implementation Priority

### CRITICAL (Security/Compliance)
1. ✅ **User Account Deletion** - Soft delete + cascade (GDPR, SOC 2)

### HIGH (Data Integrity)
2. ✅ **Deck Deletion** - Cascade to cards
3. ✅ **Focus Area Deletion** - Cascade to goals/priorities
4. ✅ **Goal Deletion** - Cascade to activities

### MEDIUM (UX Improvement)
5. ⚠️ **Annual Plan Deletion** - Cascade to focus areas/priorities

---

## 🔧 Testing Checklist

After implementation:

- [ ] Delete user account → all books/decks/cards soft deleted
- [ ] Delete user account → all annual plan data soft deleted
- [ ] Delete user account → public content unpublished
- [ ] Delete deck → all cards in deck soft deleted
- [ ] Delete focus area → all goals/priorities soft deleted
- [ ] Delete goal → all activities soft deleted
- [ ] Restore user → all data restored (optional feature)
- [ ] Permanent cleanup job → hard deletes after 30 days

---

## 🚨 IMMEDIATE ACTION REQUIRED

**User account deletion is CRITICAL:**
- Currently uses hard delete (permanent data loss)
- Not recoverable
- Not compliant with GDPR (no grace period)
- Not compliant with SOC 2 (no audit trail)

**Recommendation:** Implement soft delete for user accounts ASAP before production.

Would you like me to implement these cascade deletions now?
