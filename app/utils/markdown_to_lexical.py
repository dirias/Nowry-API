"""
Markdown to Lexical JSON Converter
Converts Markdown content to Lexical JSON format for book imports
"""

import re
import json
from typing import List, Dict, Any


def markdown_to_lexical(markdown_content: str) -> dict:
    """
    Convert Markdown to Lexical JSON format.
    
    Supports:
    - Headings (# ## ### #### ##### ######)
    - Bold (**text** or __text__)
    - Italic (*text* or _text_)
    - Code (`code`)
    - Lists (- or * or 1.)
    - Links ([text](url))
    - Code blocks (```code```)
    - Blockquotes (> text)
    - Horizontal rules (--- or ***)
    - Tables (| col | col |)
    
    Args:
        markdown_content: Raw markdown string
        
    Returns:
        Lexical JSON structure
    """
    lines = markdown_content.split('\n')
    children = []
    i = 0
    
    while i < len(lines):
        line = lines[i]
        
        # Skip empty lines (but preserve as paragraph breaks)
        if not line.strip():
            i += 1
            continue
        
        # Headings
        if line.startswith('#'):
            node = parse_heading(line)
            if node:
                children.append(node)
            i += 1
            continue
        
        # Code blocks
        if line.strip().startswith('```'):
            node, consumed = parse_code_block(lines, i)
            if node:
                children.append(node)
            i += consumed
            continue
        
        # Tables
        if '|' in line and i + 1 < len(lines) and '|' in lines[i + 1]:
            node, consumed = parse_table(lines, i)
            if node:
                children.append(node)
            i += consumed
            continue
        
        # Blockquote
        if line.strip().startswith('>'):
            node = parse_blockquote(line)
            if node:
                children.append(node)
            i += 1
            continue
        
        # Horizontal rule
        if re.match(r'^(\-{3,}|\*{3,}|_{3,})$', line.strip()):
            children.append({
                "type": "horizontalrule",
                "version": 1
            })
            i += 1
            continue
        
        # Unordered list
        if re.match(r'^\s*[\*\-\+]\s+', line):
            node, consumed = parse_list(lines, i, 'bullet')
            if node:
                children.append(node)
            i += consumed
            continue
        
        # Ordered list
        if re.match(r'^\s*\d+\.\s+', line):
            node, consumed = parse_list(lines, i, 'number')
            if node:
                children.append(node)
            i += consumed
            continue
        
        # Regular paragraph
        node = parse_paragraph(line)
        if node:
            children.append(node)
        i += 1
    
    # If no content, add empty paragraph
    if not children:
        children = [{
            "type": "paragraph",
            "children": [{"type": "text", "text": "", "format": 0}],
            "direction": "ltr",
            "format": "",
            "indent": 0,
            "version": 1
        }]
    
    return {
        "root": {
            "children": children,
            "direction": "ltr",
            "format": "",
            "indent": 0,
            "type": "root",
            "version": 1
        }
    }


def parse_heading(line: str) -> Dict[str, Any]:
    """Parse markdown heading to Lexical heading node"""
    match = re.match(r'^(#{1,6})\s+(.+)$', line)
    if not match:
        return None
    
    level = len(match.group(1))
    text = match.group(2).strip()
    
    return {
        "type": "heading",
        "tag": f"h{level}",
        "children": parse_inline_formatting(text),
        "direction": "ltr",
        "format": "",
        "indent": 0,
        "version": 1
    }


def parse_paragraph(line: str) -> Dict[str, Any]:
    """Parse markdown paragraph to Lexical paragraph node"""
    if not line.strip():
        return None
    
    return {
        "type": "paragraph",
        "children": parse_inline_formatting(line),
        "direction": "ltr",
        "format": "",
        "indent": 0,
        "version": 1
    }


def parse_blockquote(line: str) -> Dict[str, Any]:
    """Parse markdown blockquote to Lexical quote node"""
    text = re.sub(r'^>\s*', '', line).strip()
    
    return {
        "type": "quote",
        "children": [{
            "type": "paragraph",
            "children": parse_inline_formatting(text),
            "direction": "ltr",
            "format": "",
            "indent": 0,
            "version": 1
        }],
        "direction": "ltr",
        "format": "",
        "indent": 0,
        "version": 1
    }


def parse_code_block(lines: List[str], start_idx: int) -> tuple:
    """Parse markdown code block to Lexical code node"""
    # Get language if specified
    lang_match = re.match(r'^```(\w+)?', lines[start_idx])
    language = lang_match.group(1) if lang_match and lang_match.group(1) else ""
    
    # Find closing ```
    end_idx = start_idx + 1
    code_lines = []
    while end_idx < len(lines) and not lines[end_idx].strip().startswith('```'):
        code_lines.append(lines[end_idx])
        end_idx += 1
    
    code = '\n'.join(code_lines)
    
    node = {
        "type": "code",
        "language": language,
        "children": [{
            "type": "text",
            "text": code,
            "format": 0,
            "version": 1
        }],
        "direction": "ltr",
        "format": "",
        "indent": 0,
        "version": 1
    }
    
    return node, end_idx - start_idx + 2


def parse_list(lines: List[str], start_idx: int, list_type: str) -> tuple:
    """Parse markdown list to Lexical list node"""
    items = []
    i = start_idx
    
    # Determine indentation pattern
    if list_type == 'bullet':
        pattern = r'^\s*[\*\-\+]\s+'
    else:
        pattern = r'^\s*\d+\.\s+'
    
    while i < len(lines):
        line = lines[i]
        if not re.match(pattern, line):
            break
        
        # Extract list item text
        text = re.sub(pattern, '', line).strip()
        
        items.append({
            "type": "listitem",
            "children": [{
                "type": "paragraph",
                "children": parse_inline_formatting(text),
                "direction": "ltr",
                "format": "",
                "indent": 0,
                "version": 1
            }],
            "value": i - start_idx + 1,
            "version": 1
        })
        i += 1
    
    node = {
        "type": "list",
        "listType": list_type,
        "start": 1,
        "tag": "ul" if list_type == "bullet" else "ol",
        "children": items,
        "direction": "ltr",
        "format": "",
        "indent": 0,
        "version": 1
    }
    
    return node, i - start_idx


def parse_table(lines: List[str], start_idx: int) -> tuple:
    """Parse markdown table to Lexical table node"""
    # This is a simplified table parser
    # Full implementation would need to handle complex tables
    
    rows = []
    i = start_idx
    
    while i < len(lines) and '|' in lines[i]:
        line = lines[i].strip()
        
        # Skip separator line (|---|---|)
        if re.match(r'^\|[\s\-\:]+\|$', line):
            i += 1
            continue
        
        # Parse cells
        cells = [cell.strip() for cell in line.split('|')[1:-1]]
        
        row = {
            "type": "tablerow",
            "children": [
                {
                    "type": "tablecell",
                    "children": [{
                        "type": "paragraph",
                        "children": parse_inline_formatting(cell),
                        "direction": "ltr",
                        "format": "",
                        "indent": 0,
                        "version": 1
                    }],
                    "headerState": 0 if i > start_idx + 1 else 1,
                    "version": 1
                }
                for cell in cells
            ],
            "version": 1
        }
        rows.append(row)
        i += 1
    
    if not rows:
        return None, 1
    
    node = {
        "type": "table",
        "children": rows,
        "direction": "ltr",
        "format": "",
        "indent": 0,
        "version": 1
    }
    
    return node, i - start_idx


def parse_inline_formatting(text: str) -> List[Dict[str, Any]]:
    """
    Parse inline markdown formatting (bold, italic, code, links).
    Returns list of Lexical text nodes with formatting.
    """
    nodes = []
    current_pos = 0
    
    # Combined pattern for all inline elements
    # Order matters: links, code, bold, italic
    pattern = r'(\[([^\]]+)\]\(([^\)]+)\)|`([^`]+)`|\*\*([^\*]+)\*\*|__([^_]+)__|(\*|_)([^\*_]+)\7)'
    
    for match in re.finditer(pattern, text):
        # Add text before match
        if match.start() > current_pos:
            plain_text = text[current_pos:match.start()]
            if plain_text:
                nodes.append({
                    "type": "text",
                    "text": plain_text,
                    "format": 0,
                    "version": 1
                })
        
        # Handle link [text](url)
        if match.group(2) and match.group(3):
            nodes.append({
                "type": "link",
                "url": match.group(3),
                "children": [{
                    "type": "text",
                    "text": match.group(2),
                    "format": 0,
                    "version": 1
                }],
                "direction": "ltr",
                "format": "",
                "indent": 0,
                "version": 1
            })
        # Handle code `code`
        elif match.group(4):
            nodes.append({
                "type": "text",
                "text": match.group(4),
                "format": 16,  # Code format
                "version": 1
            })
        # Handle bold **text** or __text__
        elif match.group(5) or match.group(6):
            nodes.append({
                "type": "text",
                "text": match.group(5) or match.group(6),
                "format": 1,  # Bold format
                "version": 1
            })
        # Handle italic *text* or _text_
        elif match.group(8):
            nodes.append({
                "type": "text",
                "text": match.group(8),
                "format": 2,  # Italic format
                "version": 1
            })
        
        current_pos = match.end()
    
    # Add remaining text
    if current_pos < len(text):
        remaining = text[current_pos:]
        if remaining:
            nodes.append({
                "type": "text",
                "text": remaining,
                "format": 0,
                "version": 1
            })
    
    # If no nodes, add empty text node
    if not nodes:
        nodes = [{
            "type": "text",
            "text": text,
            "format": 0,
            "version": 1
        }]
    
    return nodes
