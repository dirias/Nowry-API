"""What a child process does with an uploaded file (ADR-070).

Imported by the child alone, so it pulls in the readers and nothing of the
application: no database, no settings. The parent is `services/import_parser`.
"""
from __future__ import annotations

import io
import json
import zipfile
from typing import Any, Dict, List, Optional

#: What one part of a Word archive may expand to, and the archive as a whole.
#: A .docx is a zip: 150 MB of spaces is 150 KB on the wire, and the reader
#: builds a tree of all of it. Text at the word ceiling is a few megabytes of
#: XML; pictures are bounded by the 25 MB upload and do not compress.
ARCHIVE_PART_MAX_BYTES: int = 40 * 1024 * 1024
ARCHIVE_TOTAL_MAX_BYTES: int = 200 * 1024 * 1024
ARCHIVE_MAX_ENTRIES: int = 5000


def archive_too_large(file_content: bytes) -> bool:
    """Whether a zip declares more than a document can honestly hold. Not a zip: False, the reader says why."""
    try:
        with zipfile.ZipFile(io.BytesIO(file_content)) as archive:
            entries = archive.infolist()
    except (zipfile.BadZipFile, OSError, ValueError):
        return False
    if len(entries) > ARCHIVE_MAX_ENTRIES:
        return True
    xml_too_large = any(e.file_size > ARCHIVE_PART_MAX_BYTES for e in entries if e.filename.lower().endswith((".xml", ".rels")))
    return xml_too_large or sum(e.file_size for e in entries) > ARCHIVE_TOTAL_MAX_BYTES


def _measure(pages: List[Dict[str, Any]]) -> Dict[str, int]:
    """The words and the stored size the import would have, counted the way a save counts them."""
    from app.services.book_sections import document_stats, parse_lexical
    from app.utils.file_import import strip_media_markers
    from app.utils.html_to_lexical import html_to_lexical_json, simple_html_to_lexical

    html_content = strip_media_markers("\n".join(p.get("content", "") for p in pages))
    try:
        content = json.dumps(html_to_lexical_json(html_content))
    except Exception:
        content = json.dumps(simple_html_to_lexical(html_content))
    words = int(document_stats(parse_lexical(content), content)["word_count"])
    return {"words": words, "content_bytes": len(content.encode("utf-8"))}


def read_file(filename: str, file_content: bytes) -> Dict[str, Any]:
    """Pages, pictures, word count and stored size of one uploaded file."""
    from app.utils.file_import import (
        IMPORT_UNREADABLE, process_uploaded_file_with_media, refusal_code, wears_the_wrong_extension,
    )

    if wears_the_wrong_extension(filename, file_content):
        return {"pages": [], "pictures": [], "words": 0, "content_bytes": 0, "refusal": IMPORT_UNREADABLE}
    if filename.lower().endswith(".docx") and archive_too_large(file_content):
        return {"too_complex": True}
    pages, pictures = process_uploaded_file_with_media(filename, file_content)
    if not pages:
        # Why, decided here: finding out opens the file again, and a locked PDF takes seconds to say so.
        return {"pages": [], "pictures": [], "words": 0, "content_bytes": 0, "refusal": refusal_code(filename, file_content)}
    return {"pages": pages, "pictures": pictures, **_measure(pages)}


def to_lexical(html_content: str) -> str:
    """The document's stored JSON from its HTML."""
    from app.utils.html_to_lexical import html_to_lexical_json, simple_html_to_lexical

    try:
        return json.dumps(html_to_lexical_json(html_content))
    except Exception:
        return json.dumps(simple_html_to_lexical(html_content))


def run(connection: Any, memory_bytes: Optional[int], filename: str, file_content: bytes) -> None:
    """The child's whole life: bound itself, read the file, send the answer."""
    if memory_bytes:
        try:
            import resource
            resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
        except Exception:
            pass  # not every platform enforces it; the time bound still holds
    try:
        connection.send(read_file(filename, file_content))
    except MemoryError:
        connection.send({"too_complex": True})
    except Exception as error:  # the readers catch their own; this is the unexpected
        connection.send({"error": repr(error)[:300]})
    finally:
        connection.close()
