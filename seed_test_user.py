"""
Test User Seed Script
Creates a development test user with comprehensive mock data

This script:
1. Creates/updates a test Firebase-authenticated user
2. Creates books from all documentation files
3. Publishes books (dev-restricted visibility)
4. Creates mock annual planning data (goals, priorities, focus areas, activities)
5. Creates mock flashcard decks and cards
6. Creates mock daily routines
7. Creates mock tasks

User Profile:
- Email: dev@nowry.com
- Role: dev
- Beta: true
- All features enabled
"""

import asyncio
import os
import sys
import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from bson import ObjectId
import random

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent))

from app.config.database import (
    users_collection,
    books_collection,
    decks_collection,
    cards_collection,
    annual_plans_collection,
    focus_areas_collection,
    goals_collection,
    priorities_collection,
    activities_collection,
    daily_routines_collection,
    tasks_collection,
)
from app.utils.markdown_to_lexical import markdown_to_lexical
from app.config.firebase_config import initialize_firebase

# Initialize Firebase Admin SDK
initialize_firebase()


# Test user configuration
TEST_USER_EMAIL = "dev@nowry.com"
TEST_USER_PASSWORD = "Nowry2026%"  # Change this if you want
TEST_USER_USERNAME = "NowryDev"

TEST_USER_DATA = {
    "username": TEST_USER_USERNAME,
    "email": TEST_USER_EMAIL,
    "role": "dev",  # Developer role
    "is_beta": True,  # Beta user
    "subscription": {
        "tier": "pro",  # Use 'tier' not 'plan' to match frontend expectations
        "status": "active",
        "features": {
            "max_books": -1,  # Unlimited
            "max_pages_per_book": -1,
            "max_decks": -1,
            "max_cards": -1,
            "ai_generation": True,
            "public_sharing": True,
            "advanced_analytics": True,
        }
    },
    "preferences": {
        "interests": ["Technology", "Science", "Design", "Engineering"],
        "theme_color": "#0b6bcb",
        "language": "en",
        "pomodoro_work_minutes": 25,
        "pomodoro_short_break_minutes": 5,
        "pomodoro_long_break_minutes": 15,
        "pomodoro_auto_start": False,
        "pomodoro_enabled": True,
    },
    "wizard_completed": True,
    "created_at": datetime.utcnow(),
    "updated_at": datetime.utcnow(),
}


async def create_or_update_test_user():
    """Create or update the test user in Firebase and database"""
    from firebase_admin import auth as firebase_auth
    
    print("🔧 Creating/updating test user...")
    
    # Step 1: Create or get Firebase Auth user
    try:
        print(f"   → Checking Firebase Auth for {TEST_USER_EMAIL}...")
        firebase_user = firebase_auth.get_user_by_email(TEST_USER_EMAIL)
        print(f"   ✓ Firebase user already exists (UID: {firebase_user.uid})")
        firebase_uid = firebase_user.uid
        
        # Update password if needed
        try:
            firebase_auth.update_user(
                firebase_user.uid,
                password=TEST_USER_PASSWORD,
                display_name=TEST_USER_USERNAME
            )
            print(f"   ✓ Updated Firebase user password")
        except Exception as e:
            print(f"   ⚠️  Could not update password: {e}")
        
    except firebase_auth.UserNotFoundError:
        print(f"   → Creating new Firebase Auth user...")
        try:
            firebase_user = firebase_auth.create_user(
                email=TEST_USER_EMAIL,
                password=TEST_USER_PASSWORD,
                display_name=TEST_USER_USERNAME,
                email_verified=True  # Auto-verify for dev user
            )
            firebase_uid = firebase_user.uid
            print(f"   ✓ Created Firebase user (UID: {firebase_uid})")
        except Exception as e:
            print(f"   ❌ Failed to create Firebase user: {e}")
            print(f"   ℹ️  Using fallback UID for database only")
            firebase_uid = "dev-test-user-fallback"
    except Exception as e:
        print(f"   ⚠️  Firebase Admin SDK not configured: {e}")
        print(f"   ℹ️  Using fallback UID - you'll need to create Firebase user manually")
        firebase_uid = "dev-test-user-fallback"
    
    # Step 2: Create or update database user
    existing_user = await users_collection.find_one({"email": TEST_USER_EMAIL})
    
    user_data = {
        **TEST_USER_DATA,
        "firebase_uid": firebase_uid,
        "updated_at": datetime.utcnow()
    }
    
    if existing_user:
        print(f"   ✓ Database user already exists (ID: {existing_user['_id']})")
        user_id = str(existing_user["_id"])
        # Update the user with latest config
        await users_collection.update_one(
            {"_id": existing_user["_id"]},
            {"$set": user_data}
        )
        print("   ✓ Updated database user configuration")
    else:
        result = await users_collection.insert_one(user_data)
        user_id = str(result.inserted_id)
        print(f"   ✓ Created database user (ID: {user_id})")
    
    return user_id, firebase_uid


async def create_books_from_docs(user_id: str):
    """Create books from all .md documentation files"""
    print("\n📚 Creating books from documentation files...")
    
    # Find all .md files in docs directories only
    script_dir = Path(__file__).parent  # /app (when in Docker) or Nowry-API/ (when on host)
    md_files = []
    
    # Look for docs in multiple possible locations
    possible_docs_paths = [
        script_dir / "docs",  # /app/docs (Docker) or Nowry-API/docs (host)
        script_dir.parent / "Nowry-API" / "docs",  # For when running from parent dir
        script_dir.parent / "nowry" / "docs",  # UI docs from parent dir
        Path("/app/docs"),  # Absolute path in Docker
        Path("/app/nowry/docs"),  # UI docs mounted in Docker (if available)
    ]
    
    for docs_path in possible_docs_paths:
        if docs_path.exists() and docs_path.is_dir():
            found_files = list(docs_path.rglob("*.md"))
            if found_files:
                print(f"   → Found {len(found_files)} files in {docs_path}")
                md_files.extend(found_files)
    
    print(f"   📄 Total: {len(md_files)} documentation files")
    
    created_count = 0
    for md_file in md_files:
        # Skip node_modules and hidden directories
        if any(part.startswith('.') or part == 'node_modules' for part in md_file.parts):
            continue
        
        # Read file content
        try:
            with open(md_file, 'r', encoding='utf-8') as f:
                content = f.read()
        except:
            continue
        
        # Parse title (first # heading or filename)
        title = md_file.stem.replace('_', ' ').replace('-', ' ').title()
        for line in content.split('\n'):
            if line.startswith('# '):
                title = line[2:].strip()
                break
        
        # Determine category based on path
        category = "Documentation"
        if 'design' in str(md_file):
            category = "Design & UX"
        elif 'technical' in str(md_file):
            category = "Technical"
        elif 'features' in str(md_file):
            category = "Features"
        elif 'planning' in str(md_file):
            category = "Planning"
        elif 'deploy' in str(md_file):
            category = "Deployment"
        
        # Extract tags from path
        tags = [
            part.lower() for part in md_file.parts 
            if part not in ['nowry', 'Nowry-API', 'docs', '/', '\\', '.', '..'] 
            and not part.endswith('.md')
            and len(part) > 1  # Skip single character tags
            and not part.startswith('.')  # Skip hidden folders
        ][:5]  # Limit to 5 tags
        
        # Create summary (first 200 chars, clean text only)
        summary_lines = [
            line for line in content.split('\n') 
            if line.strip() 
            and not line.startswith('#')
            and not line.strip().startswith('```')
            and not line.strip().startswith('>')
            and not line.strip().startswith('-')
            and not line.strip().startswith('*')
            and not line.strip().startswith('|')
            and not line.strip().startswith('<')  # Skip HTML lines
        ]
        # Clean any remaining markdown syntax
        summary_text = ' '.join(summary_lines[:3])
        summary_text = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', summary_text)  # Remove links
        summary_text = re.sub(r'\*\*([^\*]+)\*\*', r'\1', summary_text)  # Remove bold
        summary_text = re.sub(r'\*([^\*]+)\*', r'\1', summary_text)  # Remove italic
        summary_text = re.sub(r'`([^`]+)`', r'\1', summary_text)  # Remove code
        summary_text = re.sub(r'<[^>]+>', '', summary_text)  # Remove any HTML tags
        summary = summary_text[:200].strip() + "..." if summary_text else ""
        
        # Convert markdown to Lexical JSON format (before checking for existing)
        try:
            lexical_json = markdown_to_lexical(content)
            full_content = json.dumps(lexical_json)
        except Exception as e:
            print(f"   ⚠️  Failed to convert {title}: {e}")
            # Fallback to simple format
            full_content = json.dumps({
                "root": {
                    "children": [{
                        "type": "paragraph",
                        "children": [{"type": "text", "text": content[:500], "format": 0}]
                    }],
                    "direction": "ltr",
                    "format": "",
                    "indent": 0,
                    "type": "root",
                    "version": 1
                }
            })
        
        # Check if book already exists
        existing = await books_collection.find_one({
            "user_id": user_id,
            "title": title,
            "deleted_at": None
        })
        
        if existing:
            # Update existing book with new summary and content
            await books_collection.update_one(
                {"_id": existing["_id"]},
                {"$set": {
                    "summary": summary,
                    "full_content": full_content,
                    "tags": tags,  # Update tags too
                    "public_metadata.tags": tags,  # Update public metadata tags
                    "updated_at": datetime.utcnow()
                }}
            )
            print(f"   ↻ Updated: {title[:50]}")
            continue
        
        # Create book
        book_data = {
            "title": title,
            "author": "Nowry Team",
            "user_id": user_id,
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
            "page_limit": 50,
            "tags": tags,
            "summary": summary,
            "cover_image": "",
            "cover_color": random.choice([
                "#0b6bcb", "#097b45", "#9c27b0", "#d32f2f", 
                "#f57c00", "#0288d1", "#7b1fa2", "#c62828"
            ]),
            "page_size": "a4",
            "full_content": full_content,  # Store as Lexical JSON
            "auto_save_enabled": False,
            # Public sharing (dev-restricted)
            "is_public": True,
            "published_at": datetime.utcnow(),
            "public_metadata": {
                "views": random.randint(10, 500),
                "likes": random.randint(5, 100),
                "forks": random.randint(0, 20),
                "downloads": random.randint(0, 50),
                "category": category,
                "tags": tags,
                "language": "en",
                "difficulty_level": "intermediate",
                "average_rating": round(random.uniform(4.0, 5.0), 1),
                "rating_count": random.randint(5, 50),
                "license_type": "all_rights_reserved",
                "is_original_content": True,
                "restricted_to": "dev"  # ONLY DEV USERS CAN SEE THESE BOOKS
            }
        }
        
        await books_collection.insert_one(book_data)
        created_count += 1
    
    print(f"   ✓ Created {created_count} books from documentation")
    return created_count


async def create_annual_planning_data(user_id: str):
    """Create mock annual planning data"""
    print("\n📅 Creating annual planning data...")
    
    current_year = datetime.utcnow().year
    
    # Check if annual plan already exists
    existing_plan = await annual_plans_collection.find_one({
        "user_id": user_id,
        "year": current_year
    })
    
    if existing_plan:
        print(f"   ↻ Annual plan for {current_year} already exists (skipping)")
        return
    
    # Create Annual Plan
    annual_plan = {
        "user_id": user_id,
        "year": current_year,
        "title": "Growth & Innovation Year",
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow(),
    }
    
    plan_result = await annual_plans_collection.insert_one(annual_plan)
    plan_id = str(plan_result.inserted_id)
    print(f"   ✓ Created annual plan for {current_year}")
    
    # Create Focus Areas
    focus_areas_data = [
        {
            "name": "Product Development",
            "description": "Build and enhance core features",
            "color": "#0b6bcb",
            "icon": "🚀"
        },
        {
            "name": "User Experience",
            "description": "Improve design and usability",
            "color": "#9c27b0",
            "icon": "🎨"
        },
        {
            "name": "Growth & Marketing",
            "description": "Expand user base and engagement",
            "color": "#097b45",
            "icon": "📈"
        }
    ]
    
    focus_area_ids = []
    for idx, fa_data in enumerate(focus_areas_data):
        fa = {
            **fa_data,
            "annual_plan_id": plan_id,
            "order": idx + 1,  # FocusArea requires order field (1, 2, 3)
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
        }
        result = await focus_areas_collection.insert_one(fa)
        focus_area_ids.append(str(result.inserted_id))
    
    print(f"   ✓ Created {len(focus_area_ids)} focus areas")
    
    # Create Priorities
    priorities_data = [
        {"title": "Launch Public Content Sharing", "focus_area_idx": 0, "description": "Enable users to share and discover content"},
        {"title": "Implement Cascade Deletion", "focus_area_idx": 0, "description": "Ensure data integrity with soft deletes"},
        {"title": "Redesign Book Editor", "focus_area_idx": 1, "description": "Modern, intuitive editing experience"},
        {"title": "Improve Mobile Experience", "focus_area_idx": 1, "description": "Responsive design for all devices"},
        {"title": "Increase User Retention by 20%", "focus_area_idx": 2, "description": "Engage users with better features"},
    ]
    
    for idx, priority_data in enumerate(priorities_data):
        priority = {
            "title": priority_data["title"],
            "description": priority_data["description"],
            "annual_plan_id": plan_id,
            "focus_area_id": focus_area_ids[priority_data["focus_area_idx"]],
            "order": idx,  # Priority requires order field
            "is_completed": False,
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
        }
        await priorities_collection.insert_one(priority)
    
    print(f"   ✓ Created {len(priorities_data)} priorities")
    
    # Create Goals
    goals_data = [
        {
            "title": "Ship Public Content MVP",
            "description": "Complete public sharing feature with browse, publish, and moderation",
            "focus_area_idx": 0,
            "quarter": 1,
            "progress": 85,
            "status": "in_progress"
        },
        {
            "title": "Implement Advanced Search",
            "description": "Add full-text search with filters and sorting",
            "focus_area_idx": 0,
            "quarter": 2,
            "progress": 30,
            "status": "in_progress"
        },
        {
            "title": "Launch Dark Mode",
            "description": "Complete dark theme support across all pages",
            "focus_area_idx": 1,
            "quarter": 1,
            "progress": 100,
            "status": "completed"
        },
        {
            "title": "Mobile App Prototype",
            "description": "Build React Native prototype for iOS and Android",
            "focus_area_idx": 1,
            "quarter": 3,
            "progress": 0,
            "status": "not_started"
        },
        {
            "title": "Reach 10K Users",
            "description": "Grow user base to 10,000 active users",
            "focus_area_idx": 2,
            "quarter": 2,
            "progress": 45,
            "status": "in_progress"
        }
    ]
    
    goal_ids = []
    for goal_data in goals_data:
        goal = {
            "title": goal_data["title"],
            "description": goal_data["description"],
            "focus_area_id": focus_area_ids[goal_data["focus_area_idx"]],
            "quarter": goal_data["quarter"],
            "year": current_year,
            "type": "quarterly",
            "progress": goal_data["progress"],
            "status": goal_data["status"],
            "milestones": [],
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
            "completed_at": datetime.utcnow() if goal_data["status"] == "completed" else None,
        }
        result = await goals_collection.insert_one(goal)
        goal_ids.append(str(result.inserted_id))
    
    print(f"   ✓ Created {len(goal_ids)} goals")
    
    # Create Activities
    activities_data = [
        {"goal_idx": 0, "title": "Code Review Sessions", "frequency": "daily"},
        {"goal_idx": 0, "title": "User Testing", "frequency": "weekly"},
        {"goal_idx": 1, "title": "Research Competitors", "frequency": "weekly"},
        {"goal_idx": 2, "title": "Design Iterations", "frequency": "daily"},
        {"goal_idx": 4, "title": "Social Media Posts", "frequency": "daily"},
    ]
    
    for activity_data in activities_data:
        activity = {
            "goal_id": goal_ids[activity_data["goal_idx"]],
            "title": activity_data["title"],
            "description": "",
            "frequency": activity_data["frequency"],
            "days_of_week": [0, 1, 2, 3, 4] if activity_data["frequency"] == "daily" else [1, 3],
            "time_of_day": "anytime",
            "duration_minutes": 30,
            "is_active": True,
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
        }
        await activities_collection.insert_one(activity)
    
    print(f"   ✓ Created {len(activities_data)} activities")


async def create_flashcard_decks(user_id: str):
    """Create mock flashcard decks with cards"""
    print("\n🎴 Creating flashcard decks and cards...")
    
    decks_data = [
        {
            "name": "Python Programming Basics",
            "description": "Essential Python concepts and syntax",
            "deck_type": "flashcard",
            "color": "#0b6bcb",
            "cards": [
                {"front": "What is a list in Python?", "back": "A mutable, ordered collection of items"},
                {"front": "What is a dictionary?", "back": "A collection of key-value pairs"},
                {"front": "What does 'def' keyword do?", "back": "Defines a function"},
                {"front": "What is a lambda function?", "back": "An anonymous, single-expression function"},
                {"front": "What is list comprehension?", "back": "A concise way to create lists using a single line"}
            ],
            "is_public": True,
            "category": "Programming"
        },
        {
            "name": "Web Design Principles",
            "description": "Fundamental concepts in modern web design",
            "deck_type": "flashcard",
            "color": "#9c27b0",
            "cards": [
                {"front": "What is responsive design?", "back": "Design that adapts to different screen sizes"},
                {"front": "What is the 60-30-10 rule?", "back": "Color distribution: 60% dominant, 30% secondary, 10% accent"},
                {"front": "What is whitespace?", "back": "Empty space around elements that improves readability"},
                {"front": "What is visual hierarchy?", "back": "Organizing elements by importance to guide user attention"}
            ],
            "is_public": True,
            "category": "Design"
        },
        {
            "name": "JavaScript ES6+ Features",
            "description": "Modern JavaScript features and syntax",
            "deck_type": "quiz",
            "color": "#f57c00",
            "cards": [
                {
                    "question": "Which keyword declares a constant?",
                    "options": ["var", "let", "const", "static"],
                    "correct": "const"
                },
                {
                    "question": "What is destructuring?",
                    "options": [
                        "Deleting objects",
                        "Extracting values from arrays/objects",
                        "Breaking loops",
                        "Error handling"
                    ],
                    "correct": "Extracting values from arrays/objects"
                }
            ],
            "is_public": False,
            "category": "Programming"
        }
    ]
    
    total_cards = 0
    for deck_data in decks_data:
        # Create deck
        deck = {
            "name": deck_data["name"],
            "description": deck_data["description"],
            "user_id": user_id,  # Use string, not ObjectId
            "deck_type": deck_data["deck_type"],
            "status": "new",
            "tags": [deck_data["category"].lower()],
            "total_cards": len(deck_data["cards"]),
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
            "is_public": deck_data["is_public"],
            "published_at": datetime.utcnow() if deck_data["is_public"] else None,
        }
        
        if deck_data["is_public"]:
            deck["public_metadata"] = {
                "views": random.randint(50, 500),
                "likes": random.randint(10, 100),
                "forks": random.randint(1, 30),
                "downloads": random.randint(5, 50),
                "category": deck_data["category"],
                "tags": [deck_data["category"].lower(), "flashcards"],
                "language": "en",
                "difficulty_level": "beginner",
                "average_rating": round(random.uniform(4.0, 5.0), 1),
                "rating_count": random.randint(10, 100),
                "license_type": "cc-by",
                "is_original_content": True,
            }
        
        deck_result = await decks_collection.insert_one(deck)
        deck_id = deck_result.inserted_id
        
        # Create cards
        for card_data in deck_data["cards"]:
            card = {
                "user_id": user_id,  # Use string, not ObjectId
                "deck_id": deck_id,
                "created_at": datetime.utcnow(),
                "updated_at": datetime.utcnow(),
                "card_type": deck_data["deck_type"],
                "tags": [deck_data["category"].lower()],
            }
            
            if deck_data["deck_type"] == "flashcard":
                card.update({
                    "title": card_data["front"],
                    "content": card_data["back"],
                })
            else:  # quiz
                card.update({
                    "title": card_data["question"],
                    "content": card_data["question"],
                    "options": card_data["options"],
                    "correct_answer": card_data["correct"],
                })
            
            await cards_collection.insert_one(card)
            total_cards += 1
    
    print(f"   ✓ Created {len(decks_data)} decks with {total_cards} cards")


async def create_daily_routines(user_id: str):
    """Create mock daily routine templates"""
    print("\n⏰ Creating daily routine templates...")
    
    # DailyRoutineTemplate model has: morning_routine, afternoon_routine, evening_routine (lists of dicts)
    routine_data = {
        "user_id": user_id,
        "name": "Default Daily Routine",
        "morning_routine": [
            {"time": "06:00", "activity": "Wake up & hydrate", "duration": 5, "completed": False},
            {"time": "06:05", "activity": "Exercise", "duration": 30, "completed": False},
            {"time": "06:35", "activity": "Shower & get ready", "duration": 25, "completed": False},
            {"time": "07:00", "activity": "Breakfast", "duration": 20, "completed": False},
            {"time": "07:20", "activity": "Review daily goals", "duration": 10, "completed": False},
        ],
        "afternoon_routine": [
            {"time": "12:00", "activity": "Lunch break", "duration": 30, "completed": False},
            {"time": "14:00", "activity": "Deep work session", "duration": 90, "completed": False},
            {"time": "15:30", "activity": "Team sync", "duration": 30, "completed": False},
            {"time": "16:00", "activity": "Email & messages", "duration": 30, "completed": False},
        ],
        "evening_routine": [
            {"time": "18:00", "activity": "Dinner", "duration": 45, "completed": False},
            {"time": "19:00", "activity": "Personal time", "duration": 60, "completed": False},
            {"time": "20:00", "activity": "Reading/Learning", "duration": 30, "completed": False},
            {"time": "21:30", "activity": "Prepare for bed", "duration": 30, "completed": False},
        ],
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow(),
    }
    
    await daily_routines_collection.insert_one(routine_data)
    
    print(f"   ✓ Created daily routine template")


async def create_tasks(user_id: str):
    """Create mock tasks"""
    print("\n✅ Creating tasks...")
    
    tasks_data = [
        {"title": "Review pull requests", "priority": "high", "is_completed": False, "category": "work"},
        {"title": "Update documentation", "priority": "medium", "is_completed": False, "category": "work"},
        {"title": "Fix mobile layout bug", "priority": "high", "is_completed": False, "category": "work"},
        {"title": "Schedule team meeting", "priority": "medium", "is_completed": True, "category": "work"},
        {"title": "Research new UI patterns", "priority": "low", "is_completed": False, "category": "study"},
        {"title": "Write unit tests", "priority": "high", "is_completed": False, "category": "work"},
    ]
    
    for task_data in tasks_data:
        task = {
            "title": task_data["title"],
            "description": "",
            "user_id": user_id,
            "priority": task_data["priority"],
            "is_completed": task_data["is_completed"],
            "category": task_data["category"],
            "tags": [task_data["category"]],
            "deadline": datetime.utcnow() + timedelta(days=random.randint(1, 7)),
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
        }
        await tasks_collection.insert_one(task)
    
    print(f"   ✓ Created {len(tasks_data)} tasks")


async def main():
    """Main execution"""
    print("\n" + "="*60)
    print("🚀 NOWRY TEST USER SEED SCRIPT")
    print("="*60)
    
    try:
        # Create test user (Firebase + Database)
        user_id, firebase_uid = await create_or_update_test_user()
        
        # Create all test data
        await create_books_from_docs(user_id)
        await create_annual_planning_data(user_id)
        await create_flashcard_decks(user_id)
        await create_daily_routines(user_id)
        await create_tasks(user_id)
        
        print("\n" + "="*60)
        print("✅ TEST USER SEED COMPLETE!")
        print("="*60)
        print(f"\n📧 Email: {TEST_USER_EMAIL}")
        print(f"🔑 Password: {TEST_USER_PASSWORD}")
        print(f"👤 Username: {TEST_USER_USERNAME}")
        print(f"🆔 Firebase UID: {firebase_uid}")
        print(f"🎭 Role: dev")
        print(f"🧪 Beta: true")
        print(f"\n💡 You can now login with these credentials!")
        print(f"   1. Start the API: uvicorn app.main:app --reload")
        print(f"   2. Start the frontend: cd ../nowry && npm start")
        print(f"   3. Login with email: {TEST_USER_EMAIL}")
        print(f"   4. Password: {TEST_USER_PASSWORD}")
        print("="*60 + "\n")
        
    except Exception as e:
        print(f"\n❌ ERROR: {str(e)}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
