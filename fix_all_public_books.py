"""
Fix All Public Books
Scans all public books and fixes:
1. Corrupted summaries (containing JSON/HTML)
2. Missing summaries
3. Ensures all summaries are clean plain text
"""

import asyncio
import json
import re
from datetime import datetime
from app.config.database import books_collection


def extract_clean_summary_from_lexical(content_str, max_length=200):
    """Extract clean text summary from Lexical JSON content"""
    try:
        # Parse the Lexical JSON
        lexical = json.loads(content_str) if isinstance(content_str, str) else content_str
        
        if not lexical.get('root', {}).get('children'):
            return ""
        
        # Extract text from all nodes
        texts = []
        
        def extract_text(node):
            if isinstance(node, dict):
                # Text node
                if node.get('type') == 'text':
                    text = node.get('text', '')
                    if text.strip():
                        texts.append(text.strip())
                
                # Recursively process children
                if 'children' in node and isinstance(node['children'], list):
                    for child in node['children']:
                        extract_text(child)
        
        # Process all root children
        for child in lexical['root']['children']:
            extract_text(child)
            # Stop after collecting enough text
            if len(' '.join(texts)) > max_length * 2:
                break
        
        # Join and clean the text
        summary = ' '.join(texts)
        
        # Remove extra whitespace
        summary = re.sub(r'\s+', ' ', summary)
        
        # Truncate to max length
        if len(summary) > max_length:
            summary = summary[:max_length].rsplit(' ', 1)[0] + '...'
        
        return summary.strip()
        
    except Exception as e:
        print(f"   ⚠️  Error extracting from Lexical: {e}")
        return ""


def is_summary_corrupted(summary):
    """Check if summary is corrupted (contains JSON, HTML, markdown, or is too long)"""
    if not summary or len(summary.strip()) == 0:
        return True
    
    # Check for JSON
    if summary.strip().startswith('{') or summary.strip().startswith('['):
        return True
    
    # Check for HTML tags
    if '<html' in summary.lower() or '<div' in summary.lower() or '<p' in summary.lower():
        return True
    
    # Check for excessive markdown (code blocks, etc.)
    if '```' in summary:
        return True
    
    # Check for raw markdown syntax that should have been cleaned
    markdown_indicators = ['##', '**', '__', '```', '<!--']
    if any(indicator in summary for indicator in markdown_indicators):
        return True
    
    # Check for directory/file structures
    structure_indicators = ['├──', '└──', '│', 'src/', 'app/', '/api/', '/components/']
    if any(indicator in summary for indicator in structure_indicators):
        return True
    
    # Check if summary is too long (should be around 200 chars max)
    if len(summary) > 250:
        return True
    
    return False


async def fix_all_public_books():
    """Fix all public books with corrupted or missing summaries"""
    print("\n" + "="*70)
    print("🔧 FIXING ALL PUBLIC BOOKS")
    print("="*70)
    
    # Find all public books
    books = await books_collection.find({
        "is_public": True,
        "deleted_at": None
    }).to_list(length=None)
    
    print(f"\n📚 Found {len(books)} public books to check")
    
    fixed_count = 0
    skipped_count = 0
    failed_count = 0
    
    for book in books:
        book_id = book['_id']
        title = book.get('title', 'Untitled')
        current_summary = book.get('summary', '')
        full_content = book.get('full_content', '')
        
        # Check if summary needs fixing
        if is_summary_corrupted(current_summary):
            print(f"\n🔧 Fixing: {title[:60]}")
            print(f"   Current summary preview: {str(current_summary)[:80]}...")
            
            # Try to extract clean summary from full_content
            new_summary = ""
            
            if full_content:
                new_summary = extract_clean_summary_from_lexical(full_content)
            
            # Fallback to title-based summary if extraction failed
            if not new_summary or len(new_summary) < 20:
                new_summary = f"This is a comprehensive guide about {title.lower()}. Click to read the full content and explore this topic in detail."
            
            # Update the book
            try:
                await books_collection.update_one(
                    {"_id": book_id},
                    {"$set": {
                        "summary": new_summary,
                        "updated_at": datetime.utcnow()
                    }}
                )
                print(f"   ✅ New summary: {new_summary[:80]}...")
                fixed_count += 1
            except Exception as e:
                print(f"   ❌ Failed to update: {e}")
                failed_count += 1
        else:
            skipped_count += 1
    
    print("\n" + "="*70)
    print("✅ COMPLETE!")
    print(f"   Fixed:   {fixed_count} books")
    print(f"   Skipped: {skipped_count} books (already clean)")
    print(f"   Failed:  {failed_count} books")
    print("="*70 + "\n")


if __name__ == "__main__":
    asyncio.run(fix_all_public_books())
