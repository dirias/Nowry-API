"""
HTML to Lexical JSON Converter
Converts HTML content to Lexical JSON format for book imports
"""

import re
import json
from html import unescape
from html.parser import HTMLParser


def _dimension(value) -> "int | None":
    """A width or height attribute as an int, or None when absent or not a number."""
    try:
        number = int(float(str(value).strip().rstrip("px")))
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _element(node_type: str, **fields) -> dict:
    """An element node with every field Lexical's `importJSON` reads.

    A node without `indent` is not a detail: `ListItemNode.setIndent` throws on
    it, the editor keeps only what it had parsed so far, and the next save
    writes that fragment over the document (IMPORT-001).
    """
    return {"type": node_type, **fields, "children": [], "direction": None, "format": "", "indent": 0, "version": 1}


def _paragraph() -> dict:
    return _element("paragraph", textFormat=0, textStyle="")


#: Lexical's text format bits. The format is this number, not a list of names:
#: a list reads as no formatting at all (IMPORT-009).
FORMAT_BITS = {
    "b": 1, "strong": 1,
    "i": 2, "em": 2,
    "s": 4, "strike": 4, "del": 4,
    "u": 8, "ins": 8,
    "code": 16, "tt": 16,
    "sub": 32,
    "sup": 64,
}

HEADING_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6")
# The whitespace HTML collapses. Not `\s`: that includes the no-break space,
# which is how an importer keeps an indent or a tab the writer meant.
_SPACE = re.compile(r"[ \t\n\r\f\v]+")
_SAFE_LINK = re.compile(r"^(https?://|mailto:)", re.IGNORECASE)


class HTMLToLexicalConverter(HTMLParser):
    """
    Converts HTML to Lexical JSON structure.

    Blocks: paragraphs, headings, lists (nested), tables, quotes, code blocks,
    images. Inline: bold, italic, underline, strikethrough, code, sub and
    superscript, links, line breaks. Whitespace is collapsed the way a browser
    collapses it, never stripped from the middle of a line.
    """

    def __init__(self):
        super().__init__()
        self.nodes = []
        self.sinks = [self.nodes]      # where a finished block goes: the root, or a table cell
        self.current_paragraph = None  # the open block that takes inline content
        self.implicit = False          # the open block was opened by loose text, not by a tag
        self.open_formats = []         # one bit per open formatting tag
        self.link = None               # the open link node, inside the open block
        self.list_stack = []
        self.saved_lists = []          # list stacks set aside while inside a table cell
        self.tables = []               # open tables: {"node", "row", "cell"}
        self.quote_depth = 0
        self.in_pre = False

    # ── blocks ───────────────────────────────────────────────────────────────

    @staticmethod
    def _trim(block: dict) -> None:
        """Drop the space a block ends with, and a link left empty by it."""
        children = block["children"]
        while children:
            last = children[-1]
            if last.get("type") == "link":
                HTMLToLexicalConverter._trim(last)
                if last["children"]:
                    return
            elif last.get("type") == "text":
                last["text"] = last["text"].rstrip(" ")
                if last["text"]:
                    return
            else:
                return
            children.pop()

    def _close_block(self) -> None:
        """Store the open paragraph, heading or quote if it holds anything."""
        block = self.current_paragraph
        self.current_paragraph, self.implicit, self.link = None, False, None
        if not block or block["type"] == "listitem":
            self.current_paragraph = block if block and block["type"] == "listitem" else None
            return
        if block["type"] != "code":
            self._trim(block)
        if block["children"]:
            self.sinks[-1].append(block)

    def _open_block(self, block: dict) -> None:
        self._close_block()
        if self.current_paragraph is None:  # not inside a list item
            self.current_paragraph = block

    # ── lists ────────────────────────────────────────────────────────────────

    def _append_item(self, item: dict) -> None:
        """Put a list item into the innermost open list, numbered and indented by its place."""
        items = self.list_stack[-1]["children"]
        # A row that only wraps a nested list has no number of its own.
        rows = [i for i in items if not (i["children"] and i["children"][0].get("type") == "list")]
        item["value"] = len(rows) + 1
        item["indent"] = len(self.list_stack) - 1
        items.append(item)

    def _close_item(self, reopen: bool = False) -> None:
        """Store the open list item if it holds anything; `reopen` leaves an empty one in its place."""
        item = self.current_paragraph
        self.link = None
        if item and item["type"] == "listitem":
            self._trim(item)
            if item["children"]:
                self._append_item(item)
        self.current_paragraph = _element("listitem", value=1) if reopen else None

    def _open_list(self, tag: str) -> None:
        if self.list_stack:
            # A list inside an item: what the item said so far is its own row, kept
            # above the nested list instead of being overwritten by it.
            if self.current_paragraph is not None:
                self._close_item()
        else:
            self._close_block()
        self.list_stack.append(_element(
            "list", listType="bullet" if tag == "ul" else "number", start=1, tag=tag,
        ))

    def _close_list(self) -> None:
        if not self.list_stack:
            return
        self._close_item()
        list_node = self.list_stack.pop()
        if not list_node["children"]:
            return
        if self.list_stack:
            # Lexical nests a list inside a list item of its own, never bare.
            wrapper = _element("listitem", value=1)
            wrapper["children"].append(list_node)
            self._append_item(wrapper)
            # Whatever the parent item says after the nested list is a new row.
            self.current_paragraph = _element("listitem", value=1)
        else:
            self.sinks[-1].append(list_node)

    # ── tables ───────────────────────────────────────────────────────────────

    def _open_cell(self, tag: str, attrs: dict) -> None:
        if not self.tables:
            return
        table = self.tables[-1]
        self._close_cell()
        if table["row"] is None:
            table["row"] = _element("tablerow")
        cell = _element(
            "tablecell", backgroundColor=None, headerState=1 if tag == "th" else 0,
            colSpan=_dimension(attrs.get("colspan")) or 1, rowSpan=_dimension(attrs.get("rowspan")) or 1, width=None,
        )
        table["cell"] = cell
        self.sinks.append(cell["children"])
        self.saved_lists.append(self.list_stack)
        self.list_stack = []

    def _close_cell(self) -> None:
        table = self.tables[-1] if self.tables else None
        if not table or table["cell"] is None:
            return
        while self.list_stack:
            self._close_list()
        self._close_block()
        cell, table["cell"] = table["cell"], None
        self.sinks.pop()
        self.list_stack = self.saved_lists.pop()
        if not cell["children"]:
            cell["children"].append(_paragraph())  # a cell always holds a block
        table["row"]["children"].append(cell)

    def _close_row(self) -> None:
        table = self.tables[-1] if self.tables else None
        if not table:
            return
        self._close_cell()
        row, table["row"] = table["row"], None
        if row and row["children"]:
            table["node"]["children"].append(row)

    def _close_table(self) -> None:
        if not self.tables:
            return
        self._close_row()
        table = self.tables.pop()["node"]
        if table["children"]:
            self.sinks[-1].append(table)

    # ── tags ─────────────────────────────────────────────────────────────────

    def handle_starttag(self, tag, attrs):
        """Handle opening HTML tags"""
        if tag in ("p", "div") or tag in HEADING_TAGS:
            if self.list_stack:
                # A paragraph inside a list item is that item's text (a footnote's
                # `<li><p>…</p></li>`), not a block that replaces it.
                if self.current_paragraph is None:
                    self.current_paragraph = _element("listitem", value=1)
            elif tag in HEADING_TAGS:
                self._open_block(_element("heading", tag=tag))
            elif self.quote_depth:
                self._open_block(_element("quote"))
            else:
                self._open_block(_paragraph())
        elif tag == "blockquote":
            self._close_block()
            self.quote_depth += 1
        elif tag == "pre":
            self._open_block(_element("code", language=None))
            self.in_pre = True
        elif tag in ("ul", "ol"):
            self._open_list(tag)
        elif tag == "li":
            if self.list_stack:
                self._close_item(reopen=True)
        elif tag == "table":
            if not self.list_stack:
                self._close_block()
                self.tables.append({"node": _element("table"), "row": None, "cell": None})
        elif tag == "tr":
            if self.tables:
                self._close_row()
                self.tables[-1]["row"] = _element("tablerow")
        elif tag in ("td", "th"):
            self._open_cell(tag, dict(attrs))
        elif tag in FORMAT_BITS:
            self.open_formats.append(FORMAT_BITS[tag])
        elif tag == "a":
            self._open_link(dict(attrs).get("href"))
        elif tag == "br":
            block = self.current_paragraph
            if block and block["children"]:
                self._inline_target().append({"type": "linebreak", "version": 1})
        elif tag == "img":
            self._add_image(dict(attrs))

    def handle_endtag(self, tag):
        """Handle closing HTML tags"""
        if tag in ("p", "div") or tag in HEADING_TAGS:
            if not self.list_stack:  # a list item closes with its own tag
                self._close_block()
        elif tag == "blockquote":
            self._close_block()
            self.quote_depth = max(0, self.quote_depth - 1)
        elif tag == "pre":
            self.in_pre = False
            self._close_block()
        elif tag in ("ul", "ol"):
            self._close_list()
        elif tag == "li":
            if self.list_stack:
                self._close_item()
        elif tag == "table":
            self._close_table()
        elif tag == "tr":
            self._close_row()
        elif tag in ("td", "th"):
            self._close_cell()
        elif tag in FORMAT_BITS:
            bit = FORMAT_BITS[tag]
            if bit in self.open_formats:
                self.open_formats.remove(bit)
        elif tag == "a":
            self.link = None

    # ── inline content ───────────────────────────────────────────────────────

    def _inline_target(self) -> list:
        """Where inline content goes: the open link, or the open block."""
        return self.link["children"] if self.link is not None else self.current_paragraph["children"]

    def _ensure_block(self) -> None:
        """Loose text opens a paragraph of its own (a row, inside a list)."""
        if self.current_paragraph is not None:
            return
        if self.list_stack:
            self.current_paragraph = _element("listitem", value=1)
        else:
            self.current_paragraph = _element("quote") if self.quote_depth else _paragraph()
            self.implicit = True

    def _open_link(self, href) -> None:
        """A link with somewhere to go becomes a link node; a bookmark or a bare anchor is just its text."""
        href = (href or "").strip()
        if not _SAFE_LINK.match(href) or self.in_pre:
            return
        self._ensure_block()
        self.link = _element("link", url=href, rel="noreferrer", target=None, title=None)
        self.current_paragraph["children"].append(self.link)

    def _add_image(self, attrs):
        """An `<img>` becomes a block-level image node (ADR-062 point 5).

        A marker nothing stored (no `src`) and an inlined picture (a data URL)
        are both dropped: a document holds stored URLs only.
        """
        src = (attrs.get('src') or '').strip()
        if not src or src.lower().startswith('data:'):
            return
        image = {
            "type": "image",
            "src": src,
            "altText": attrs.get('alt') or '',
            "width": _dimension(attrs.get('width')),
            "height": _dimension(attrs.get('height')),
            "alignment": "center",
            "maxWidth": 600,
            "version": 1,
        }
        block = self.current_paragraph
        if self.list_stack:
            # The picture leaves the list: the rows so far stay rows, and it sits after them at the root.
            self.sinks[-1].append(image)
            return
        # Close the open block, put the picture after it, and reopen one of the same kind.
        reopened = {key: value for key, value in block.items() if key != 'children'} if block else None
        self._close_block()
        self.sinks[-1].append(image)
        if reopened is not None:
            reopened['children'] = []
            self.current_paragraph = reopened

    def _last_text(self):
        children = self._inline_target()
        return children[-1] if children and children[-1].get("type") == "text" else None

    def handle_data(self, data):
        """Handle text content"""
        if self.in_pre and self.current_paragraph is not None:
            self._add_code(data)
            return
        text = _SPACE.sub(" ", data)
        if not text.strip(" ") and self.current_paragraph is None:
            return  # the line break between two blocks
        self._ensure_block()
        children = self._inline_target()
        starts_line = not self.current_paragraph["children"] or (
            children and children[-1].get("type") == "linebreak")
        previous = self._last_text()
        if starts_line or (previous and previous["text"].endswith(" ")) or (
                self.link is not None and not children and self._ends_with_space()):
            text = text.lstrip(" ")
        if not text:
            return
        mask = 0
        for bit in self.open_formats:
            mask |= bit
        if previous is not None and previous["format"] == mask:
            previous["text"] += text
            return
        children.append({
            "type": "text", "text": text, "format": mask,
            "detail": 0, "mode": "normal", "style": "", "version": 1,
        })

    def _ends_with_space(self) -> bool:
        """True when the block's text before the open link already ends in a space."""
        before = [child for child in self.current_paragraph["children"] if child is not self.link]
        return bool(before) and before[-1].get("type") == "text" and before[-1]["text"].endswith(" ")

    def _add_code(self, data: str) -> None:
        """Text inside `<pre>`: kept as written, one line break node per newline."""
        children = self.current_paragraph["children"]
        lines = data.replace("\r\n", "\n").split("\n")
        for index, line in enumerate(lines):
            if index:
                children.append({"type": "linebreak", "version": 1})
            if line:
                children.append({
                    "type": "text", "text": line, "format": 0,
                    "detail": 0, "mode": "normal", "style": "", "version": 1,
                })

    def close(self):
        """Close what the HTML left open, so an unclosed list or table is never lost."""
        super().close()
        while self.tables:
            self._close_table()
        while self.list_stack:
            self._close_list()
        self._close_block()

    def get_lexical_json(self):
        """Get final Lexical JSON structure"""
        # Ensure at least one node
        if not self.nodes:
            self.nodes = [_paragraph()]

        return {
            "root": {
                "children": self.nodes,
                "direction": None,
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
    converter.close()
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
