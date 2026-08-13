"""
HTML to Lexical JSON Converter
Converts HTML content to Lexical JSON format for book imports
"""

import re
import json
from html import unescape
from html.parser import HTMLParser


class HTMLToLexicalConverter(HTMLParser):
    """
    Converts HTML to Lexical JSON structure
    Preserves formatting, headings, lists, and basic structure
    """
    
    def __init__(self):
        super().__init__()
        self.nodes = []
        self.current_paragraph = None
        self.text_format = []
        self.list_stack = []
        
    def handle_starttag(self, tag, attrs):
        """Handle opening HTML tags"""
        if tag in ['p', 'div']:
            self.current_paragraph = {
                "type": "paragraph",
                "children": []
            }
        elif tag in ['h1', 'h2', 'h3', 'h4', 'h5', 'h6']:
            self.current_paragraph = {
                "type": "heading",
                "tag": tag,
                "children": []
            }
        elif tag == 'ul' or tag == 'ol':
            list_node = {
                "type": "list",
                "listType": "bullet" if tag == 'ul' else "number",
                "children": []
            }
            self.list_stack.append(list_node)
        elif tag == 'li':
            if self.list_stack:
                self.current_paragraph = {
                    "type": "listitem",
                    "children": []
                }
        elif tag == 'strong' or tag == 'b':
            if 'bold' not in self.text_format:
                self.text_format.append('bold')
        elif tag == 'em' or tag == 'i':
            if 'italic' not in self.text_format:
                self.text_format.append('italic')
        elif tag == 'u':
            if 'underline' not in self.text_format:
                self.text_format.append('underline')
        elif tag == 'br':
            # Add line break
            if self.current_paragraph:
                self.current_paragraph['children'].append({
                    "type": "linebreak"
                })
    
    def handle_endtag(self, tag):
        """Handle closing HTML tags"""
        if tag in ['p', 'div'] or tag.startswith('h'):
            if self.current_paragraph and self.current_paragraph['children']:
                if self.list_stack:
                    self.list_stack[-1]['children'].append(self.current_paragraph)
                else:
                    self.nodes.append(self.current_paragraph)
            self.current_paragraph = None
        elif tag == 'ul' or tag == 'ol':
            if self.list_stack:
                list_node = self.list_stack.pop()
                if self.list_stack:
                    self.list_stack[-1]['children'].append(list_node)
                else:
                    self.nodes.append(list_node)
        elif tag == 'li':
            if self.current_paragraph:
                if self.list_stack:
                    self.list_stack[-1]['children'].append(self.current_paragraph)
                self.current_paragraph = None
        elif tag in ['strong', 'b']:
            if 'bold' in self.text_format:
                self.text_format.remove('bold')
        elif tag in ['em', 'i']:
            if 'italic' in self.text_format:
                self.text_format.remove('italic')
        elif tag == 'u':
            if 'underline' in self.text_format:
                self.text_format.remove('underline')
    
    def handle_data(self, data):
        """Handle text content"""
        text = data.strip()
        if not text:
            return
            
        # Create text node with current formatting
        text_node = {
            "type": "text",
            "text": text,
            "format": self.text_format.copy() if self.text_format else []
        }
        
        if self.current_paragraph:
            self.current_paragraph['children'].append(text_node)
        else:
            # Orphaned text - wrap in paragraph
            self.nodes.append({
                "type": "paragraph",
                "children": [text_node]
            })
    
    def get_lexical_json(self):
        """Get final Lexical JSON structure"""
        # Ensure at least one node
        if not self.nodes:
            self.nodes = [{
                "type": "paragraph",
                "children": [{"type": "text", "text": "", "format": []}]
            }]
        
        return {
            "root": {
                "children": self.nodes,
                "direction": "ltr",
                "format": "",
                "indent": 0,
                "type": "root",
                "version": 1
            }
        }


def html_to_lexical_json(html_content: str) -> dict:
    """
    Convert HTML content to Lexical JSON format
    
    Args:
        html_content: HTML string to convert
        
    Returns:
        dict: Lexical JSON structure
    """
    converter = HTMLToLexicalConverter()
    converter.feed(html_content)
    return converter.get_lexical_json()


def simple_html_to_lexical(html_content: str) -> dict:
    """
    Simple HTML to Lexical converter (fallback for complex HTML)
    Extracts text and preserves basic structure
    """
    # Remove script and style tags
    html_content = re.sub(r'<(script|style)[^>]*>.*?</\1>', '', html_content, flags=re.DOTALL)
    
    # Split by block elements
    blocks = re.split(r'</?(?:p|div|br|h[1-6]|li)[^>]*>', html_content)
    
    children = []
    for block in blocks:
        # Clean text
        text = re.sub(r'<[^>]+>', '', block)  # Remove tags
        text = unescape(text).strip()
        
        if text:
            children.append({
                "type": "paragraph",
                "children": [
                    {
                        "type": "text",
                        "text": text,
                        "format": []
                    }
                ]
            })
    
    if not children:
        children = [{
            "type": "paragraph",
            "children": [{"type": "text", "text": "", "format": []}]
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
