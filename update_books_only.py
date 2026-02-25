"""
Quick Book Update Script
Updates only the books from markdown files (summaries and content)
"""

import asyncio
import sys
import json
import re
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from app.config.database import books_collection
from app.utils.markdown_to_lexical import markdown_to_lexical


async def update_books():
    """Update books from markdown documentation"""
    print("\n" + "="*60)
    print("📚 UPDATING BOOKS FROM MARKDOWN")
    print("="*60)
    
    # Find the dev user (adjust email if needed)
    from app.config.database import users_collection
    user = await users_collection.find_one({"email": "dev@nowry.com"})
    
    if not user:
        print("❌ User not found! Make sure to run seed_test_user.py first.")
        return
    
    user_id = str(user["_id"])
    print(f"\n✓ Found user: {user['email']} (ID: {user_id})")
    
    # Find all markdown files
    docs_paths = [
        Path("docs"),
        Path("nowry/docs")
    ]
    
    md_files = []
    for docs_path in docs_paths:
        if docs_path.exists():
            md_files.extend(docs_path.rglob("*.md"))
    
    print(f"\n📄 Found {len(md_files)} markdown files")
    
    updated_count = 0
    created_count = 0
    
    for md_file in md_files:
        try:
            content = md_file.read_text(encoding='utf-8')
            title = md_file.stem.replace('_', ' ').replace('-', ' ').title()
            
            # Extract category from path
            parts = md_file.parts
            if 'features' in parts:
                category = 'Features'
            elif 'deployment' in parts or 'operations' in parts:
                category = 'Deployment'
            elif 'technical' in parts:
                category = 'Technical'
            elif 'guides' in parts:
                category = 'Documentation'
            elif 'design' in parts:
                category = 'Design'
            else:
                category = 'Documentation'
            
            # Extract tags from path
            tags = [
                part.lower() for part in md_file.parts 
                if part not in ['nowry', 'Nowry-API', 'docs', '/', '\\', '.', '..'] 
                and not part.endswith('.md')
                and len(part) > 1
                and not part.startswith('.')
            ][:5]
            
            # Create clean summary
            summary_lines = [
                line for line in content.split('\n') 
                if line.strip() 
                and not line.startswith('#')
                and not line.strip().startswith('```')
                and not line.strip().startswith('>')
                and not line.strip().startswith('-')
                and not line.strip().startswith('*')
                and not line.strip().startswith('|')
                and not line.strip().startswith('<')
            ]
            summary_text = ' '.join(summary_lines[:3])
            summary_text = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', summary_text)
            summary_text = re.sub(r'\*\*([^\*]+)\*\*', r'\1', summary_text)
            summary_text = re.sub(r'\*([^\*]+)\*', r'\1', summary_text)
            summary_text = re.sub(r'`([^`]+)`', r'\1', summary_text)
            summary_text = re.sub(r'<[^>]+>', '', summary_text)
            summary = summary_text[:200].strip() + "..." if summary_text else ""
            
            # Convert to Lexical JSON
            try:
                lexical_json = markdown_to_lexical(content)
                full_content = json.dumps(lexical_json)
            except Exception as e:
                print(f"   ⚠️  Failed to convert {title}: {e}")
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
            
            # Check if exists
            existing = await books_collection.find_one({
                "user_id": user_id,
                "title": title,
                "deleted_at": None
            })
            
            if existing:
                # Update
                await books_collection.update_one(
                    {"_id": existing["_id"]},
                    {"$set": {
                        "summary": summary,
                        "full_content": full_content,
                        "tags": tags,
                        "public_metadata.tags": tags,
                        "updated_at": datetime.utcnow()
                    }}
                )
                print(f"   ↻ Updated: {title[:60]}")
                updated_count += 1
            else:
                # Create new
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
                    "cover_color": "#0b6bcb",
                    "page_size": "a4",
                    "full_content": full_content,
                    "auto_save_enabled": False,
                    "is_public": True,
                    "published_at": datetime.utcnow(),
                    "public_metadata": {
                        "views": 0,
                        "likes": 0,
                        "forks": 0,
                        "downloads": 0,
                        "category": category,
                        "tags": tags,
                        "language": "en",
                        "difficulty": "intermediate",
                        "estimated_read_time": len(content.split()) // 200
                    }
                }
                await books_collection.insert_one(book_data)
                print(f"   ✓ Created: {title[:60]}")
                created_count += 1
                
        except Exception as e:
            print(f"   ❌ Error processing {md_file.name}: {e}")
            continue
    
    print("\n" + "="*60)
    print("✅ COMPLETE!")
    print(f"   Updated: {updated_count} books")
    print(f"   Created: {created_count} books")
    print("="*60 + "\n")


if __name__ == "__main__":
    asyncio.run(update_books())
