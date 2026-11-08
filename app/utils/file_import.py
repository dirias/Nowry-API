"""
Enhanced file import utilities for processing PDFs, DOCX, and TXT files
Preserves formatting by converting to HTML which can be imported into Lexical editor
"""

import fitz  # PyMuPDF
import mammoth
from typing import Dict, List, Optional, Tuple
import re
import html

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

        for page_num, page in enumerate(doc):
            # Get page dimensions to detect columns
            page_width = page.rect.width
            page_height = page.rect.height
            mid_point = page_width / 2

            # Extract text blocks with position info
            blocks = page.get_text("dict")["blocks"]

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

                if block.get("type") == 0:  # Text block
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
                html_parts = []

                for block in blocks_list:
                    # Handle Image Blocks
                    if block.get("type") == 1 and block.get("custom_html"):
                        html_parts.append(block["custom_html"])
                        continue

                    # Handle Text Blocks
                    block_html = []
                    for line in block.get("lines", []):
                        line_html = []
                        for span in line.get("spans", []):
                            text = span.get("text", "").strip()
                            if not text:
                                continue

                            size = span.get("size", 12)
                            flags = span.get("flags", 0)
                            formatted_text = html.escape(text)

                            # Bold (flag 16)
                            if flags & 16:
                                formatted_text = f"<strong>{formatted_text}</strong>"

                            # Italic (flag 2)
                            if flags & 2:
                                formatted_text = f"<em>{formatted_text}</em>"

                            # Heading detection - Adjusted thresholds
                            if size > 24:
                                formatted_text = f"<h1>{formatted_text}</h1>"
                            elif size > 18:
                                formatted_text = f"<h2>{formatted_text}</h2>"
                            elif size > 14:
                                formatted_text = f"<h3>{formatted_text}</h3>"

                            line_html.append(formatted_text)

                        if line_html:
                            block_html.append(" ".join(line_html))

                    if block_html:
                        html_parts.append("<p>" + " ".join(block_html) + "</p>")

                return "".join(html_parts)

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
        print(f"Error extracting PDF with formatting: {str(e)}")
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
        print(f"Error in PDF fallback: {str(e)}")
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
        print(f"Error extracting DOCX with formatting: {str(e)}")
        return []


def split_html_into_pages(html_content: str, max_chars: int = 3000) -> List[str]:
    """
    Split HTML content into page-sized chunks while preserving structure
    """
    # Simple approach: split by paragraphs and group into pages
    paragraphs = re.findall(r"<[^>]+>.*?</[^>]+>|<[^/>]+/>", html_content, re.DOTALL)

    pages = []
    current_page = []
    current_length = 0

    for para in paragraphs:
        para_length = len(re.sub(r"<[^>]+>", "", para))  # Text length without tags

        if current_length + para_length > max_chars and current_page:
            pages.append("".join(current_page))
            current_page = [para]
            current_length = para_length
        else:
            current_page.append(para)
            current_length += para_length

    if current_page:
        pages.append("".join(current_page))

    return pages if pages else [html_content]


def extract_formatted_text_from_txt(file_content: bytes) -> List[Dict[str, str]]:
    """
    Extract text from TXT file and convert to HTML paragraphs
    """
    try:
        text = file_content.decode("utf-8")

        # Split by paragraphs
        paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]

        # Group into pages
        pages = []
        current_page_paras = []
        char_count = 0
        max_chars_per_page = 3000

        for para in paragraphs:
            if char_count > max_chars_per_page and current_page_paras:
                # Save current page
                html_content = (
                    "<p>"
                    + "</p><p>".join(html.escape(p) for p in current_page_paras)
                    + "</p>"
                )
                pages.append(
                    {
                        "title": f"Página {len(pages) + 1}",
                        "content": html_content,
                        "page_number": len(pages) + 1,
                        "format": "html",
                    }
                )
                current_page_paras = [para]
                char_count = len(para)
            else:
                current_page_paras.append(para)
                char_count += len(para)

        # Add last page
        if current_page_paras:
            html_content = (
                "<p>"
                + "</p><p>".join(html.escape(p) for p in current_page_paras)
                + "</p>"
            )
            pages.append(
                {
                    "title": f"Página {len(pages) + 1}",
                    "content": html_content,
                    "page_number": len(pages) + 1,
                    "format": "html",
                }
            )

        return pages
    except Exception as e:
        print(f"Error extracting TXT: {str(e)}")
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
    elif file_ext in ["docx", "doc"]:
        pages = extract_formatted_text_from_docx(file_content, media)
    elif file_ext == "txt":
        pages = extract_formatted_text_from_txt(file_content)
    else:
        pages = []

    if pages:
        pages[0].setdefault("extraction_metadata", {})["pictures"] = len(media)
    return pages, media


def process_uploaded_file(filename: str, file_content: bytes) -> List[Dict[str, str]]:
    """
    Process uploaded file and extract pages with preserved formatting
    Returns list of pages with HTML content (pictures as markers, see above)
    """
    return process_uploaded_file_with_media(filename, file_content)[0]
