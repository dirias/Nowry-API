"""
Update Book Summaries - Fix HTML/Markdown in Summaries
Re-processes existing books to generate clean text summaries
"""

import asyncio
import sys
import re
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent))

from app.config.database import books_collection


async def clean_summary_text(raw_summary: str) -> str:
    """Clean markdown/HTML from summary text"""
    if not raw_summary:
        return ""
    
    # Remove markdown syntax
    clean_text = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', raw_summary)  # Links
    clean_text = re.sub(r'\*\*([^\*]+)\*\*', r'\1', clean_text)  # Bold
    clean_text = re.sub(r'\*([^\*]+)\*', r'\1', clean_text)  # Italic
    clean_text = re.sub(r'`([^`]+)`', r'\1', clean_text)  # Code
    clean_text = re.sub(r'<[^>]+>', '', clean_text)  # HTML tags
    clean_text = re.sub(r'<!--.*?-->', '', clean_text)  # HTML comments
    
    # Clean up whitespace
    clean_text = ' '.join(clean_text.split())
    
    return clean_text[:200].strip() + "..." if len(clean_text) > 200 else clean_text.strip()


async def update_book_summaries():
    """Update summaries for all books"""
    print("\n" + "="*60)
    print("🔧 UPDATING BOOK SUMMARIES")
    print("="*60)
    
    # Find all books with potentially problematic summaries
    books = await books_collection.find({
        "deleted_at": None,
        "summary": {"$exists": True, "$ne": ""}
    }).to_list(length=None)
    
    print(f"\n📚 Found {len(books)} books to check")
    
    updated_count = 0
    skipped_count = 0
    
    for book in books:
        original_summary = book.get("summary", "")
        
        # Check if summary contains HTML or markdown
        if any(marker in original_summary for marker in ['<', '>', '```', '<!--', '**', '__']):
            clean_summary = await clean_summary_text(original_summary)
            
            # Update the book
            await books_collection.update_one(
                {"_id": book["_id"]},
                {"$set": {
                    "summary": clean_summary,
                    "updated_at": datetime.utcnow()
                }}
            )
            
            print(f"   ✓ Updated: {book['title'][:50]}")
            print(f"      Before: {original_summary[:80]}...")
            print(f"      After:  {clean_summary[:80]}...")
            updated_count += 1
        else:
            skipped_count += 1
    
    print("\n" + "="*60)
    print(f"✅ COMPLETE!")
    print(f"   Updated: {updated_count} books")
    print(f"   Skipped: {skipped_count} books (already clean)")
    print("="*60 + "\n")


if __name__ == "__main__":
    asyncio.run(update_book_summaries())
