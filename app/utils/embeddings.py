"""
Text chunking and embedding utilities for book RAG.

Calls the Google Generative Language REST API directly with httpx —
bypasses the google-generativeai SDK's embed_content routing issues.
Uses text-embedding-004 (v1) which produces 768-dim vectors.
"""
from __future__ import annotations

import json
import os
import re
from typing import List

import httpx

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_GEMINI_KEY_ENV = "GEMINI_API_KEY"
_EMBED_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-embedding-001:embedContent"
)
_EMBED_DIM = 3072


def _api_key() -> str:
    key = os.environ.get(_GEMINI_KEY_ENV, "")
    if not key:
        raise RuntimeError(f"{_GEMINI_KEY_ENV} environment variable is not set")
    return key


# ---------------------------------------------------------------------------
# Plain-text extraction from Lexical JSON
# ---------------------------------------------------------------------------

def extract_plain_text(raw_content: str) -> str:
    """Extract plain text from a Lexical JSON string, or strip HTML as fallback."""
    if not raw_content:
        return ""
    try:
        parsed = json.loads(raw_content)
        parts: List[str] = []

        def walk(node: dict) -> None:
            if isinstance(node.get("text"), str) and node["text"].strip():
                parts.append(node["text"].strip())
            for child in node.get("children", []):
                walk(child)

        walk(parsed.get("root", parsed))
        return " ".join(parts)
    except (json.JSONDecodeError, AttributeError):
        return re.sub(r"<[^>]+>", " ", raw_content).strip()


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

def chunk_text(text: str, chunk_size: int = 1200, overlap: int = 150) -> List[str]:
    """
    Split plain text into overlapping chunks at paragraph/sentence boundaries.
    chunk_size and overlap are in characters (~300 and ~37 tokens at 4 chars/token).
    """
    if not text:
        return []

    paragraphs = [p.strip() for p in re.split(r"\n{2,}", text) if p.strip()]
    chunks: List[str] = []
    current = ""

    for para in paragraphs:
        if len(current) + len(para) + 1 <= chunk_size:
            current = (current + " " + para).strip() if current else para
        else:
            if current:
                chunks.append(current)
            if len(para) > chunk_size:
                for sent in re.split(r"(?<=[.!?])\s+", para):
                    if len(current) + len(sent) + 1 <= chunk_size:
                        current = (current + " " + sent).strip() if current else sent
                    else:
                        if current:
                            chunks.append(current)
                        current = sent
            else:
                current = para

    if current:
        chunks.append(current)

    # Apply character-level overlap
    if overlap > 0 and len(chunks) > 1:
        overlapped: List[str] = [chunks[0]]
        for i in range(1, len(chunks)):
            tail = chunks[i - 1][-overlap:]
            overlapped.append((tail + " " + chunks[i]).strip())
        return overlapped

    return chunks


# ---------------------------------------------------------------------------
# Embedding — direct REST calls, no SDK
# ---------------------------------------------------------------------------

def _task_type_header(task_type: str) -> str:
    """Convert snake_case task type to SCREAMING_SNAKE_CASE for the REST API."""
    return task_type.upper().replace("-", "_")


def _embed_single(text: str, task_type: str) -> List[float]:
    """
    Call the Google Embedding REST API for one text string.
    Returns a 768-dim float list. Raises on HTTP or parse errors.
    """
    payload = {
        "model": "models/gemini-embedding-001",
        "content": {"parts": [{"text": text}]},
        "taskType": _task_type_header(task_type),
    }
    resp = httpx.post(
        _EMBED_URL,
        params={"key": _api_key()},
        json=payload,
        timeout=30.0,
    )
    resp.raise_for_status()
    return resp.json()["embedding"]["values"]


def embed_texts(texts: List[str], task_type: str = "retrieval_document") -> List[List[float]]:
    """Embed each text string individually. Returns list of 768-dim vectors."""
    return [_embed_single(t, task_type) for t in texts]


def embed_query(query: str) -> List[float]:
    """Embed a single query string for vector search retrieval."""
    return _embed_single(query, "retrieval_query")
