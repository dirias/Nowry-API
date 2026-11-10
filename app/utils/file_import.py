"""
Enhanced file import utilities for processing PDFs, DOCX, and TXT files
Preserves formatting by converting to HTML which can be imported into Lexical editor
"""

import fitz  # PyMuPDF
import mammoth
from typing import Dict, List, Optional, Tuple
import codecs
import logging
import re
import html

logger = logging.getLogger(__name__)

# ── pictures ────────────────────────────────────────────────────────────────
#
# A picture an importer finds is handed back as bytes beside the HTML, never
# inlined (ADR-062 point 5): the HTML carries a marker, `<img data-media="n">`,
# and the route stores the bytes and puts the stored URL where the marker was.
# A marker nothing replaced is dropped by `place_media`, so a data URL can
# never enter a document through an import.

_MARKER = re.compile(r'<img\b[^>]*\bdata-media="(\d+)"[^>]*/?>', re.IGNORECASE)
_ALT = re.compile(r'\balt="([^"]*)"')
DEFAULT_ALT = "Imported image"


def media_marker(index: int, alt: str = DEFAULT_ALT) -> str:
    """The HTML placeholder for picture `index` of the import."""
    return f'<img data-media="{index}" alt="{html.escape(alt or DEFAULT_ALT, quote=True)}">'


def _picture(media: List[Dict], data: bytes, ext: str, width: Optional[int], height: Optional[int], alt: str) -> int:
    """Add a picture to the import's media list and return its marker index."""
    media.append({"index": len(media) + 1, "bytes": data, "ext": (ext or "png").lower().lstrip("."),
                  "width": width, "height": height, "alt": alt or DEFAULT_ALT})
    return media[-1]["index"]


def place_media(html_content: str, placements: Dict[int, Dict]) -> str:
    """Replace each marker with its stored image, or drop it when nothing was stored.

    `placements` maps a marker index to `{"url", "width", "height"}`.
    """
    def replace(match: "re.Match") -> str:
        placed = placements.get(int(match.group(1)))
        if not placed or not placed.get("url"):
            return ""
        alt_match = _ALT.search(match.group(0))
        alt = alt_match.group(1) if alt_match else html.escape(DEFAULT_ALT, quote=True)
        size = "".join(f' {name}="{int(placed[name])}"' for name in ("width", "height") if placed.get(name))
        return f'<img src="{html.escape(placed["url"], quote=True)}"{size} alt="{alt}">'

    return _MARKER.sub(replace, html_content)


def strip_media_markers(html_content: str) -> str:
    """The HTML without its picture markers — for a preview, which stores nothing."""
    return place_media(html_content, {})


# ── reading a PDF's lines ───────────────────────────────────────────────────
#
# A PDF has no paragraphs, lists or tables, only lines of positioned text. What
# the importer can recover it recovers here (IMPORT-011): a line that starts
# with a bullet or a number is a list row, a word hyphenated at a line end is
# one word, Japanese lines join without a space, a line repeated in the margin
# of the pages is a running header or footer, and ruled cells are a table.

_BULLET = re.compile(r"^\s*([•·▪◦●○■□‣⁃∙➢➤✓✔\-–—\*])\s+(?=\S)")
_NUMBERED = re.compile(r"^\s*(\d{1,2})[.)]\s+(?=\S)")
_DIGITS = re.compile(r"\d+")
_MARGIN_BAND = 0.07  # the top and bottom share of a page where running lines live (59 pt of A4:
                     # above a one-inch margin's first line, so body text is never a candidate)
_RUNNING_MAX_LENGTH = 120  # a header or footer is a line, not a paragraph


def _is_cjk(char: str) -> bool:
    code = ord(char)
    return (0x3040 <= code <= 0x30FF or 0x3400 <= code <= 0x4DBF or 0x4E00 <= code <= 0x9FFF
            or 0xF900 <= code <= 0xFAFF or 0xFF00 <= code <= 0xFFEF or 0x3000 <= code <= 0x303F
            or 0xAC00 <= code <= 0xD7AF)


def _joint(previous: str, following: str) -> Tuple[str, str]:
    """How two lines of one paragraph meet: (`previous` as it should end, the glue)."""
    if not previous or not following:
        return previous, ""
    if previous.endswith("­"):
        return previous[:-1], ""
    if previous.endswith("-") and len(previous) > 1 and previous[-2].isalpha() and following[0].islower():
        return previous[:-1], ""  # a word cut at the line end
    if _is_cjk(previous[-1]) and _is_cjk(following[0]):
        return previous, ""  # no spaces between lines of Japanese, Chinese or Korean
    return previous, " "


def _pdf_line(line: Dict) -> Optional[Dict]:
    """One line as `{"segments": [[text, flags]], "text", "size", "bbox"}`, or None when it is blank."""
    segments: List[List] = []
    sizes: Dict[float, int] = {}
    for span in line.get("spans", []):
        text = span.get("text", "")
        if not text:
            continue
        flags = span.get("flags", 0) & (16 | 2)
        if segments and segments[-1][1] == flags:
            segments[-1][0] += text
        else:
            segments.append([text, flags])
        size = round(span.get("size", 12), 1)
        sizes[size] = sizes.get(size, 0) + len(text.strip())
    text = "".join(segment[0] for segment in segments)
    if not text.strip():
        return None
    return {"segments": segments, "text": text.strip(), "bbox": line.get("bbox", [0, 0, 0, 0]),
            "size": max(sizes, key=sizes.get) if sizes else 12}


def _strip_marker(entry: Dict, length: int) -> None:
    """Remove the first `length` characters (a list marker and its space) from the line."""
    remaining = length
    for segment in entry["segments"]:
        cut = min(remaining, len(segment[0]))
        segment[0] = segment[0][cut:]
        remaining -= cut
        if not remaining:
            break
    entry["segments"] = [segment for segment in entry["segments"] if segment[0]]
    entry["text"] = "".join(segment[0] for segment in entry["segments"]).strip()


def _append_line(parts: List[List], entry: Dict) -> None:
    """Add a line's segments to a paragraph being built, joined the way `_joint` says."""
    segments = [list(segment) for segment in entry["segments"]]
    if not segments:
        return
    segments[0][0] = segments[0][0].lstrip()
    segments[-1][0] = segments[-1][0].rstrip()
    if parts:
        parts[-1][0] = parts[-1][0].rstrip()
        parts[-1][0], glue = _joint(parts[-1][0], segments[0][0])
        if glue:
            parts[-1][0] += glue
    for text, flags in segments:
        if not text:
            continue
        if parts and parts[-1][1] == flags:
            parts[-1][0] += text
        else:
            parts.append([text, flags])


def _render(parts: List[List]) -> str:
    out = []
    for text, flags in parts:
        piece = html.escape(text)
        if flags & 16:
            piece = f"<strong>{piece}</strong>"
        if flags & 2:
            piece = f"<em>{piece}</em>"
        out.append(piece)
    return "".join(out)


def _heading_tag(size: float) -> Optional[str]:
    if size > 24:
        return "h1"
    if size > 18:
        return "h2"
    if size > 14:
        return "h3"
    return None


def _running_key(page_height: float, entry: Dict) -> Optional[Tuple[str, str]]:
    """The identity of a line in a page's top or bottom margin, digits aside; None elsewhere."""
    if not page_height or len(entry["text"]) > _RUNNING_MAX_LENGTH:
        return None
    top, bottom = entry["bbox"][1], entry["bbox"][3]
    if bottom <= page_height * _MARGIN_BAND:
        band = "top"
    elif top >= page_height * (1 - _MARGIN_BAND):
        band = "bottom"
    else:
        return None
    return band, _DIGITS.sub("#", entry["text"])


def _running_lines(doc) -> set:
    """The margin lines that repeat on at least half the pages (and at least two): headers, footers, page numbers."""
    if doc.page_count < 2:
        return set()
    seen: Dict[Tuple[str, str], int] = {}
    for page in doc:
        keys = set()
        for block in page.get_text("dict", flags=0)["blocks"]:
            for line in block.get("lines", []):
                entry = _pdf_line(line)
                key = _running_key(page.rect.height, entry) if entry else None
                if key:
                    keys.add(key)
        for key in keys:
            seen[key] = seen.get(key, 0) + 1
    needed = max(2, (doc.page_count + 1) // 2)
    return {key for key, count in seen.items() if count >= needed}


def _page_tables(page) -> List[Dict]:
    """The page's ruled tables as pseudo-blocks carrying their HTML, positioned by their box."""
    try:
        # A ruled table is drawn with lines. A page with too few of them has none,
        # and finding that out the long way costs ~50 ms a page.
        if len(page.get_cdrawings()) < 4:
            return []
        found = page.find_tables(strategy="lines")
    except Exception:  # an optional reading; a page without it is still imported
        return []
    tables = []
    for table in getattr(found, "tables", []):
        try:
            rows = table.extract()
        except Exception:
            continue
        rows = [[(cell or "").replace("\n", " ").strip() for cell in row] for row in rows or []]
        rows = [row for row in rows if any(row)]
        cells = [cell for row in rows for cell in row]
        if len(rows) < 2 or max(len(row) for row in rows) < 2 or sum(1 for c in cells if c) < len(cells) * 0.5:
            continue  # a box around some text, not a table
        body = "".join(
            "<tr>" + "".join(f"<td><p>{html.escape(cell)}</p></td>" for cell in row) + "</tr>" for row in rows
        )
        tables.append({"type": "table", "bbox": list(table.bbox), "custom_html": f"<table>{body}</table>"})
    return tables


def _inside(bbox, container) -> bool:
    """True when the centre of `bbox` lies inside `container`."""
    x, y = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
    return container[0] <= x <= container[2] and container[1] <= y <= container[3]


def _pdf_blocks_to_html(blocks_list: List[Dict], page_height: float = 0, running: Optional[set] = None) -> str:
    """Text blocks as paragraphs, headings and lists; pictures and tables where they sit."""
    pieces: List[Tuple[Optional[str], str]] = []  # (list kind or None, html)

    def flush(kind: Optional[str], parts: List[List], tag: Optional[str]) -> None:
        body = _render(parts)
        if not body.strip():
            return
        if kind:
            pieces.append((kind, f"<li>{body}</li>"))
        else:
            pieces.append((None, f"<{tag or 'p'}>{body}</{tag or 'p'}>"))

    for block in blocks_list:
        if block.get("custom_html"):
            pieces.append((None, block["custom_html"]))
            continue
        kind, tag, parts = None, None, []
        for line in block.get("lines", []):
            entry = _pdf_line(line)
            if entry is None:
                continue
            if running and _running_key(page_height, entry) in running:
                continue
            heading = _heading_tag(entry["size"])
            marker = None if heading else (_BULLET.match(entry["text"]) or _NUMBERED.match(entry["text"]))
            if marker:
                flush(kind, parts, tag)
                leading = len(entry["segments"][0][0]) - len(entry["segments"][0][0].lstrip()) if entry["segments"] else 0
                number = marker.re is _NUMBERED
                _strip_marker(entry, leading + len(marker.group(0).lstrip()))
                kind, tag, parts = ("ol" if number else "ul"), None, []
            elif heading:
                if kind or tag != heading:  # a heading starts; the same level on the next line is its wrap
                    flush(kind, parts, tag)
                    kind, tag, parts = None, heading, []
            elif tag:
                # plain text after a heading is a paragraph of its own
                flush(kind, parts, tag)
                kind, tag, parts = None, None, []
            # otherwise the line continues the paragraph or the list row above it
            _append_line(parts, entry)
        flush(kind, parts, tag)

    out: List[str] = []
    open_kind: Optional[str] = None
    for kind, piece in pieces:
        if kind != open_kind:
            if open_kind:
                out.append(f"</{open_kind}>")
            if kind:
                out.append(f"<{kind}>")
            open_kind = kind
        out.append(piece)
    if open_kind:
        out.append(f"</{open_kind}>")
    return "".join(out)


def extract_formatted_text_from_pdf(file_content: bytes, media: Optional[List[Dict]] = None) -> List[Dict[str, str]]:
    """
    Extract text from PDF preserving formatting AND multi-column layouts
    Detects academic paper 2-column formats and preserves them
    Returns HTML content per page with quality metrics

    Pictures are appended to `media` as bytes and marked in the HTML (see
    `media_marker`); a caller that passes no list gets the markers alone.
    """
    if media is None:
        media = []
    try:
        import io

        pdf_file = io.BytesIO(file_content)
        doc = fitz.open(stream=pdf_file, filetype="pdf")

        pages = []
        total_chars_extracted = 0
        running = _running_lines(doc)

        for page_num, page in enumerate(doc):
            # Get page dimensions to detect columns
            page_width = page.rect.width
            page_height = page.rect.height
            mid_point = page_width / 2

            # Extract text blocks with position info
            blocks = page.get_text("dict")["blocks"]

            # Ruled tables are read as tables; the text they cover is theirs.
            tables = _page_tables(page)
            if tables:
                blocks = [b for b in blocks if b.get("type") != 0
                          or not any(_inside(b.get("bbox", [0, 0, 0, 0]), t["bbox"]) for t in tables)] + tables

            # Separate blocks into left and right columns
            left_column = []
            right_column = []
            full_width_blocks = []

            for block in blocks:
                # Common block properties
                bbox = block.get("bbox", [0, 0, 0, 0])
                block_left = bbox[0]
                block_right = bbox[2]
                block_top = bbox[1]

                # Attach position for sorting
                block["_y_pos"] = block_top

                if block.get("type") in (0, "table"):  # Text block, or a table found above
                    # Determine column (logic handled below)
                    pass
                elif block.get("type") == 1:  # Image block — the dict carries the bytes
                    image_bytes = block.get("image")
                    if not image_bytes:
                        continue
                    index = _picture(media, image_bytes, block.get("ext", "png"),
                                     block.get("width"), block.get("height"), DEFAULT_ALT)
                    block["custom_html"] = media_marker(index)
                else:
                    continue  # Skip other types

                # Determine if block is in left column, right column, or full width
                if block_right < mid_point + 10:  # Left column (tighter margin)
                    left_column.append(block)
                elif block_left > mid_point - 10:  # Right column
                    right_column.append(block)
                else:  # Spanning / Center
                    full_width_blocks.append(block)

            # --- INTELLIGENT LAYOUT DETECTION ---
            # Check if this is actually a 2-column page or just a single column with some short lines

            # Count text length in each section
            def get_text_len(block_list):
                length = 0
                for b in block_list:
                    if b.get("type") == 0:  # Only count text blocks
                        for line in b.get("lines", []):
                            for span in line.get("spans", []):
                                length += len(span.get("text", ""))
                return length

            left_len = get_text_len(left_column)
            right_len = get_text_len(right_column)
            spanning_len = get_text_len(full_width_blocks)

            total_len = left_len + right_len + spanning_len

            # Heuristics for 2-Column Mode:
            # 1. Must have substantial content in BOTH columns (to avoid sidebars/margin notes being treated as 2-col)
            # 2. Spanning content (titles) shouldn't dominate (> 40% of page usually means generic single col)
            is_two_column = False

            if total_len > 0:
                has_two_distinct_cols = (left_len > total_len * 0.1) and (
                    right_len > total_len * 0.1
                )
                not_dominated_by_center = spanning_len < total_len * 0.6

                if has_two_distinct_cols and not_dominated_by_center:
                    is_two_column = True

            # If NOT 2-column, merge everything back to linear layout (Single Column)
            if not is_two_column:
                full_width_blocks = full_width_blocks + left_column + right_column
                # Sort by vertical position (top to bottom)
                full_width_blocks.sort(key=lambda b: b.get("bbox", [0, 0, 0, 0])[1])
                left_column = []
                right_column = []

            # Sort blocks by vertical position within each column
            left_column.sort(key=lambda b: b.get("_y_pos", 0))
            right_column.sort(key=lambda b: b.get("_y_pos", 0))
            full_width_blocks.sort(key=lambda b: b.get("_y_pos", 0))

            # Format blocks to HTML
            def format_blocks_to_html(blocks_list, column_class=""):
                return _pdf_blocks_to_html(blocks_list, page_height, running)

            # Build page HTML preserving column structure
            page_html = ""

            # Full-width content first (titles, abstracts)
            if full_width_blocks:
                page_html += format_blocks_to_html(full_width_blocks)

            # If we have two columns, preserve them
            if left_column and right_column:
                page_html += '<div style="display: grid; grid-template-columns: 1fr 1fr; gap: 20px;">'
                page_html += f'<div class="column-left">{format_blocks_to_html(left_column)}</div>'
                page_html += f'<div class="column-right">{format_blocks_to_html(right_column)}</div>'
                page_html += "</div>"
            elif left_column:
                # Only left column (or single column)
                page_html += format_blocks_to_html(left_column)
            elif right_column:
                # Only right column
                page_html += format_blocks_to_html(right_column)

            # Extract plain text for quality metrics
            plain_text = page.get_text()
            char_count = len(plain_text)
            word_count = len(plain_text.split())
            total_chars_extracted += char_count

            if page_html.strip():
                pages.append(
                    {
                        "title": f"Página {page_num + 1}",
                        "content": page_html,
                        "page_number": page_num + 1,
                        "format": "html",
                        "has_columns": bool(left_column and right_column),
                        "word_count": word_count,
                        "char_count": char_count,
                        "quality_score": min(
                            100, int((char_count / 2000) * 100)
                        ),  # Rough quality estimate
                    }
                )

        doc.close()

        # Add validation metadata
        if pages:
            pages[0]["extraction_metadata"] = {
                "total_pages": len(pages),
                "total_chars": total_chars_extracted,
                "total_words": sum(p.get("word_count", 0) for p in pages),
                "multi_column_pages": sum(
                    1 for p in pages if p.get("has_columns", False)
                ),
            }

        return pages
    except Exception as e:
        logger.warning("PDF import: formatted extraction failed (%s); trying the plain fallback", e)
        return extract_text_from_pdf_fallback(file_content)


def extract_text_from_pdf_fallback(file_content: bytes) -> List[Dict[str, str]]:
    """
    Fallback plain text extraction from PDF
    """
    try:
        from PyPDF2 import PdfReader
        import io

        pdf_file = io.BytesIO(file_content)
        reader = PdfReader(pdf_file)

        pages = []
        for i, page in enumerate(reader.pages):
            text = page.extract_text()
            if text.strip():
                # Convert to simple HTML paragraphs
                paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
                html_content = (
                    "<p>" + "</p><p>".join(html.escape(p) for p in paragraphs) + "</p>"
                )

                pages.append(
                    {
                        "title": f"Página {i + 1}",
                        "content": html_content,
                        "page_number": i + 1,
                        "format": "html",
                    }
                )

        return pages
    except Exception as e:
        logger.warning("PDF import: the plain fallback failed too (%s)", e)
        return []


def _docx_picture_handler(media: List[Dict]):
    """A mammoth image converter that keeps the bytes and leaves a marker in the HTML."""
    def handle(image) -> Dict[str, str]:
        with image.open() as handle_:
            data = handle_.read()
        ext = (image.content_type or "image/png").split("/")[-1].replace("jpeg", "jpg")
        index = _picture(media, data, ext, None, None, image.alt_text or DEFAULT_ALT)
        return {"data-media": str(index), "alt": image.alt_text or DEFAULT_ALT}

    return mammoth.images.img_element(handle)


def extract_formatted_text_from_docx(file_content: bytes, media: Optional[List[Dict]] = None) -> List[Dict[str, str]]:
    """
    Extract text from DOCX preserving formatting using mammoth
    Converts to HTML which preserves bold, italic, headings, lists, etc.

    Pictures are appended to `media` as bytes and marked in the HTML (see
    `media_marker`); a caller that passes no list gets the markers alone.
    """
    if media is None:
        media = []
    try:
        import io

        docx_file = io.BytesIO(file_content)

        # Convert DOCX to HTML using mammoth (preserves formatting); pictures
        # come out as bytes beside it, never as data URIs (ADR-062 point 5).
        result = mammoth.convert_to_html(docx_file, convert_image=_docx_picture_handler(media))
        html_content = result.value

        # Split into pages (by page breaks or content length)
        pages = []

        # Try to split by page break markers
        if "<hr" in html_content.lower() or "---" in html_content:
            page_splits = re.split(
                r"<hr[^>]*>|<p>-{3,}</p>", html_content, flags=re.IGNORECASE
            )
        else:
            # Split into chunks of reasonable size for pages
            # Parse HTML and split by approximate character count
            page_splits = split_html_into_pages(html_content, max_chars=3000)

        for i, page_html in enumerate(page_splits):
            cleaned_html = page_html.strip()
            if cleaned_html and cleaned_html != "<p></p>":
                pages.append(
                    {
                        "title": f"Página {i + 1}",
                        "content": cleaned_html,
                        "page_number": i + 1,
                        "format": "html",
                    }
                )

        return pages
    except Exception as e:
        logger.warning("DOCX import: extraction failed (%s)", e)
        return []


_TAG = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9]*)\b[^>]*?(/?)>")
_VOID_TAGS = frozenset({"img", "br", "hr", "input", "meta", "link", "col", "wbr", "area", "base", "embed", "source", "track"})


def _top_level_blocks(html_content: str) -> List[str]:
    """The HTML cut at the end of each top-level element, losing nothing.

    Every character of the input is in exactly one block: text between two
    elements goes with the element that follows it, and what is left after the
    last element is a block of its own. Joining the blocks gives the input back.
    """
    blocks: List[str] = []
    depth = 0
    start = 0
    for match in _TAG.finditer(html_content):
        closing, name, self_closing = match.group(1), match.group(2).lower(), match.group(3)
        if closing:
            depth = max(0, depth - 1)
        elif not self_closing and name not in _VOID_TAGS:
            depth += 1
            continue
        if depth == 0:
            blocks.append(html_content[start:match.end()])
            start = match.end()
    if html_content[start:].strip():
        blocks.append(html_content[start:])
    return blocks


def split_html_into_pages(html_content: str, max_chars: int = 3000) -> List[str]:
    """
    Split HTML content into page-sized chunks while preserving structure.

    The cut falls only between top-level elements, so a page never ends inside a
    paragraph, a list or a table, and no text is dropped (IMPORT-002: the regex
    this replaces kept only `<tag>…</tag>` matches, so the plain text between
    two inline runs of a paragraph was lost, and so was a list's closing tag).
    """
    pages = []
    current_page = []
    current_length = 0

    for block in _top_level_blocks(html_content):
        block_length = len(re.sub(r"<[^>]+>", "", block))  # Text length without tags

        if current_length + block_length > max_chars and current_page:
            pages.append("".join(current_page))
            current_page = [block]
            current_length = block_length
        else:
            current_page.append(block)
            current_length += block_length

    if current_page:
        pages.append("".join(current_page))

    return pages if pages else [html_content]


# ── text encodings ──────────────────────────────────────────────────────────
#
# A text file says nothing about its encoding. Reading UTF-8 only refused every
# file Notepad or Word saved as "plain text" on Windows (IMPORT-006), which is
# where a student's accents and kanji most often come from.

_BOMS = (
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF32_LE, "utf-32"),
    (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)

#: Encodings the detector is trusted to tell apart: their byte patterns are
#: distinctive. Between the single-byte code pages it guesses, and guesses wrong
#: (Windows-1252 French read as Windows-1250), so those are not asked of it.
_DETECTABLE = frozenset({
    "utf_16", "utf_16_le", "utf_16_be", "utf_32", "utf_32_le", "utf_32_be",
    "cp932", "shift_jis", "shift_jis_2004", "shift_jisx0213", "euc_jp", "euc_jis_2004", "euc_jisx0213", "iso2022_jp",
    "gb18030", "gbk", "gb2312", "big5", "big5hkscs", "cp950", "cp949", "euc_kr", "johab",
})


def _detected_encoding(data: bytes) -> Optional[str]:
    """The detector's best guess, or None when it has none or is not installed."""
    try:
        from charset_normalizer import from_bytes
    except ImportError:  # pragma: no cover - listed in requirements.txt
        return None
    best = from_bytes(data).best()
    return best.encoding if best else None


def _looks_like_text(text: str) -> bool:
    """False for what decoded without error but is not writing: control characters throughout."""
    if not text:
        return True
    controls = sum(1 for char in text if ord(char) < 32 and char not in "\t\n\r\f")
    return controls / len(text) < 0.02


def decode_text(data: bytes) -> Optional[str]:
    """A text file's bytes as text, or None when they are not text.

    In order: a byte-order mark says what it is; valid UTF-8 is UTF-8; a
    multi-byte encoding the detector recognises (Shift-JIS, GB18030, EUC-KR,
    UTF-16 without a mark) is that; anything else is read as Windows-1252, the
    code page Western-language "plain text" comes in. A byte-order mark is
    never part of the text.
    """
    for mark, encoding in _BOMS:
        if data.startswith(mark):
            try:
                return data.decode(encoding).lstrip("\ufeff")
            except UnicodeDecodeError:
                break
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    guess = _detected_encoding(data)
    if guess in _DETECTABLE:
        try:
            return data.decode(guess)
        except (UnicodeDecodeError, LookupError):
            pass
    if b"\x00" in data:
        return None  # binary: no single-byte text carries NUL bytes
    for encoding in ("cp1252", "latin-1"):
        try:
            text = data.decode(encoding)
        except UnicodeDecodeError:
            continue
        return text if _looks_like_text(text) else None
    return None


# ── reading a text file's lines ─────────────────────────────────────────────
#
# In a text file the writer's line breaks are the only structure there is
# (IMPORT-012). A blank line ends a paragraph; inside one, each line is kept as
# a line unless the paragraph is plainly hard-wrapped prose; a line that starts
# with a dash, a bullet or a number is a list row, and one that starts with
# `# ` is a heading — the marks people type in notes whether or not they call
# it Markdown.

_TXT_HEADING = re.compile(r"^(#{1,6})\s+(\S.*?)\s*#*\s*$")
_TXT_BULLET = re.compile(r"^\s*[-*•·▪◦‣]\s+(\S.*)$")
_TXT_NUMBERED = re.compile(r"^\s*\d{1,3}[.)]\s+(\S.*)$")
_PARAGRAPH_BREAK = re.compile(r"\n[ \t]*\n+")
_WRAP_MIN_LENGTH = 45   # hard-wrapped prose has long lines…
_WRAP_MAX_SPREAD = 40   # …of nearly the same length


def _is_hard_wrapped(lines: List[str]) -> bool:
    """True when the lines are prose broken at a fixed width, not lines the writer meant."""
    if len(lines) < 3:
        return False
    body = [len(line) for line in lines[:-1]]
    return min(body) >= _WRAP_MIN_LENGTH and max(body) - min(body) <= _WRAP_MAX_SPREAD


def _unwrap(lines: List[str]) -> str:
    text = ""
    for line in lines:
        text, glue = _joint(text, line)
        text += glue + line
    return text


def _text_paragraph_to_html(paragraph: str) -> str:
    """One blank-line-delimited paragraph as headings, lists and lines."""
    out: List[str] = []
    plain: List[str] = []
    open_list: Optional[str] = None

    def close_plain() -> None:
        if not plain:
            return
        if _is_hard_wrapped(plain):
            out.append(f"<p>{html.escape(_unwrap(plain))}</p>")
        else:
            out.append("<p>" + "<br>".join(html.escape(line) for line in plain) + "</p>")
        plain.clear()

    def close_list() -> None:
        nonlocal open_list
        if open_list:
            out.append(f"</{open_list}>")
            open_list = None

    for raw in paragraph.split("\n"):
        line = raw.rstrip()
        if not line.strip():
            continue
        heading = _TXT_HEADING.match(line)
        bullet = _TXT_BULLET.match(line)
        numbered = _TXT_NUMBERED.match(line)
        if heading:
            close_plain()
            close_list()
            level = len(heading.group(1))
            out.append(f"<h{level}>{html.escape(heading.group(2))}</h{level}>")
        elif bullet or numbered:
            close_plain()
            kind = "ul" if bullet else "ol"
            if open_list != kind:
                close_list()
                out.append(f"<{kind}>")
                open_list = kind
            out.append(f"<li>{html.escape((bullet or numbered).group(1))}</li>")
        elif open_list and raw[:1] in (" ", "\t") and out and out[-1].endswith("</li>"):
            # an indented line under a list row continues that row
            out[-1] = out[-1][:-5] + " " + html.escape(line.strip()) + "</li>"
        else:
            close_list()
            plain.append(line.strip())
    close_plain()
    close_list()
    return "".join(out)


def extract_formatted_text_from_txt(file_content: bytes) -> List[Dict[str, str]]:
    """
    Extract text from TXT file and convert to HTML: paragraphs, line breaks,
    lists and `#` headings, in pages of about 3,000 characters.
    """
    try:
        text = decode_text(file_content)
        if text is None:
            return []
        text = text.replace("\r\n", "\n").replace("\r", "\n")

        paragraphs = [p.strip("\n") for p in _PARAGRAPH_BREAK.split(text) if p.strip()]

        pages: List[Dict[str, str]] = []
        current: List[str] = []
        char_count = 0
        max_chars_per_page = 3000

        def close_page() -> None:
            pages.append({
                "title": f"Página {len(pages) + 1}",
                "content": "".join(current),
                "page_number": len(pages) + 1,
                "format": "html",
            })

        for paragraph in paragraphs:
            rendered = _text_paragraph_to_html(paragraph)
            if not rendered:
                continue
            if char_count > max_chars_per_page and current:
                close_page()
                current, char_count = [], 0
            current.append(rendered)
            char_count += len(paragraph)
        if current:
            close_page()
        return pages
    except Exception as e:
        logger.warning("TXT import: extraction failed (%s)", e)
        return []


def process_uploaded_file_with_media(filename: str, file_content: bytes) -> Tuple[List[Dict[str, str]], List[Dict]]:
    """
    Process uploaded file: the pages with preserved formatting, and the pictures.

    Each picture is `{"index", "bytes", "ext", "width", "height", "alt"}`; its
    place in the pages' HTML is the marker `media_marker(index)`.
    """
    file_ext = filename.lower().split(".")[-1]
    media: List[Dict] = []

    if file_ext == "pdf":
        pages = extract_formatted_text_from_pdf(file_content, media)
    elif file_ext == "docx":
        # Not "doc": the legacy binary format is not a docx and mammoth cannot
        # read it; `refusal_code` says so in words the writer can act on.
        pages = extract_formatted_text_from_docx(file_content, media)
    elif file_ext == "txt":
        pages = extract_formatted_text_from_txt(file_content)
    else:
        pages = []

    if pages:
        _count_pages(pages)
        metadata = pages[0].setdefault("extraction_metadata", {})
        metadata.setdefault("total_pages", len(pages))
        metadata.setdefault("total_words", sum(p["word_count"] for p in pages))
        metadata.setdefault("total_chars", sum(p["char_count"] for p in pages))
        metadata.setdefault("multi_column_pages", 0)
        metadata["pictures"] = len(media)
    return pages, media


_ANY_TAG = re.compile(r"<[^>]+>")


def _count_pages(pages: List[Dict]) -> None:
    """Give every page its word and character count.

    Only the PDF reader counted, so a preview of a text or Word file said
    "0 words" of a document it had just read (IMPORT-013).
    """
    for page in pages:
        if "word_count" in page and "char_count" in page:
            continue
        text = html.unescape(_ANY_TAG.sub(" ", page.get("content", "")))
        page.setdefault("word_count", len(text.split()))
        page.setdefault("char_count", len(" ".join(text.split())))


def process_uploaded_file(filename: str, file_content: bytes) -> List[Dict[str, str]]:
    """
    Process uploaded file and extract pages with preserved formatting
    Returns list of pages with HTML content (pictures as markers, see above)
    """
    return process_uploaded_file_with_media(filename, file_content)[0]


# ── why a file was refused ──────────────────────────────────────────────────
#
# An import that yields no pages used to be one sentence, "Failed to extract
# content from file", whatever the cause (IMPORT-007). The causes ask different
# things of the writer, so each has a code the clients translate.

IMPORT_UNSUPPORTED_TYPE = "import_unsupported_type"
IMPORT_LEGACY_DOC = "import_legacy_doc"
IMPORT_EMPTY = "import_empty"
IMPORT_PASSWORD_PROTECTED = "import_password_protected"
IMPORT_UNREADABLE = "import_unreadable"
IMPORT_TOO_LARGE = "import_too_large"

SUPPORTED_EXTENSIONS = ("pdf", "docx", "txt")


def _pdf_refusal(file_content: bytes) -> str:
    import io
    try:
        doc = fitz.open(stream=io.BytesIO(file_content), filetype="pdf")
    except Exception:
        return IMPORT_UNREADABLE
    try:
        if doc.needs_pass:
            return IMPORT_PASSWORD_PROTECTED
        return IMPORT_EMPTY if doc.page_count else IMPORT_UNREADABLE
    finally:
        doc.close()


def _docx_refusal(file_content: bytes) -> str:
    import io
    import zipfile
    try:
        with zipfile.ZipFile(io.BytesIO(file_content)) as archive:
            return IMPORT_EMPTY if "word/document.xml" in archive.namelist() else IMPORT_UNREADABLE
    except (zipfile.BadZipFile, OSError):
        return IMPORT_UNREADABLE


def refusal_code(filename: str, file_content: bytes) -> str:
    """Why `process_uploaded_file_with_media` gave no pages for this file, as a stable code."""
    extension = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    if extension == "doc":
        return IMPORT_LEGACY_DOC
    if extension not in SUPPORTED_EXTENSIONS:
        return IMPORT_UNSUPPORTED_TYPE
    if not file_content.strip():
        return IMPORT_EMPTY
    if extension == "pdf":
        return _pdf_refusal(file_content)
    if extension == "docx":
        return _docx_refusal(file_content)
    return IMPORT_EMPTY if decode_text(file_content) is not None else IMPORT_UNREADABLE
