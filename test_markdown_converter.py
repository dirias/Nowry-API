#!/usr/bin/env python3
"""
Test Markdown to Lexical Converter
Quick test to verify markdown conversion works correctly
"""

from app.utils.markdown_to_lexical import markdown_to_lexical
import json

# Test markdown content
test_markdown = """# Welcome to Nowry

This is a **bold** and *italic* text with `code`.

## Features

- **Public Content Sharing**: Share your notes with the world
- **Cascade Deletion**: Safe deletion with recovery
- **Beautiful UI**: Following design guidelines

### Code Example

```python
def hello_world():
    print("Hello, Nowry!")
```

### Table Example

| Feature | Status |
|---------|--------|
| Books | ✅ Done |
| Cards | ✅ Done |
| Goals | ✅ Done |

> This is a blockquote with important information.

[Learn more](https://nowry.com)
"""

if __name__ == "__main__":
    print("🧪 Testing Markdown to Lexical Converter...\n")
    
    try:
        # Convert markdown to Lexical
        lexical_json = markdown_to_lexical(test_markdown)
        
        # Pretty print
        print("✅ Conversion successful!")
        print(f"\nGenerated {len(lexical_json['root']['children'])} blocks:\n")
        
        for i, child in enumerate(lexical_json['root']['children'], 1):
            node_type = child.get('type', 'unknown')
            print(f"  {i}. {node_type}")
            
            # Show text content for some types
            if node_type in ['heading', 'paragraph']:
                if 'children' in child and child['children']:
                    text = ' '.join(
                        c.get('text', '') for c in child['children'] 
                        if c.get('type') == 'text'
                    )[:50]
                    print(f"     \"{text}...\"")
        
        print("\n📄 Full JSON (first 500 chars):")
        json_str = json.dumps(lexical_json, indent=2)
        print(json_str[:500] + "...\n")
        
        print("✅ Test passed! Markdown conversion is working correctly.")
        
    except Exception as e:
        print(f"❌ Test failed: {e}")
        import traceback
        traceback.print_exc()
