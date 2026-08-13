"""
Book RAG: index book content into MongoDB and retrieve relevant chunks at query time.

MongoDB Atlas Vector Search index required (create once in Atlas UI):
  Collection : book_chunks
  Index name : book_chunks_vector_index
  Field      : embedding  (vector, 3072 dims, cosine similarity)
  Filters    : book_id (string), user_id (string)

Index JSON definition for Atlas UI:
{
  "fields": [
    {
      "type": "vector",
      "path": "embedding",
      "numDimensions": 3072,
      "similarity": "cosine"
    },
    { "type": "filter", "path": "book_id" },
    { "type": "filter", "path": "user_id" }
  ]
}
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from app.config.database import book_chunks_collection
from app.utils.embeddings import chunk_text, embed_query, embed_texts, extract_plain_text

logger = logging.getLogger(__name__)


async def index_book(book_id: str, user_id: str, raw_content: str) -> None:
    """
    Chunk + embed a book's content and upsert into book_chunks collection.
    Called in the background after every book save — non-blocking.
    All exceptions are caught internally so the background task never raises.
    """
    try:
        plain = extract_plain_text(raw_content)
        if not plain.strip():
            logger.info("[book_rag] Book %s has no text — skipping index", book_id)
            return

        chunks = chunk_text(plain)
        if not chunks:
            return

        embeddings = embed_texts(chunks, task_type="retrieval_document")
        if len(embeddings) != len(chunks):
            logger.error(
                "[book_rag] Embedding count mismatch for book %s: got %d embeddings for %d chunks",
                book_id,
                len(embeddings),
                len(chunks),
            )
            return

        now = datetime.now(timezone.utc)

        # Delete existing chunks for this book before re-inserting
        await book_chunks_collection.delete_many({"book_id": book_id, "user_id": user_id})

        docs = [
            {
                "book_id": book_id,
                "user_id": user_id,
                "chunk_index": i,
                "text": chunks[i],
                "embedding": embeddings[i],
                "indexed_at": now,
            }
            for i in range(len(chunks))
        ]
        await book_chunks_collection.insert_many(docs)
        logger.info("[book_rag] Indexed %d chunks for book %s", len(docs), book_id)

    except Exception as exc:
        logger.error("[book_rag] Indexing failed for book %s: %s", book_id, exc)
        # Non-fatal — book save already succeeded; RAG will not have latest content


async def retrieve_book_context(
    book_id: str,
    user_id: str,
    query: str,
    top_k: int = 4,
) -> Optional[str]:
    """
    Embed the query and retrieve the top-k most relevant chunks from the book.

    Returns a formatted string ready to inject into the system prompt, or None if
    no chunks are indexed yet or Atlas Vector Search is unavailable.
    All exceptions are caught so the chat endpoint always works even when RAG fails.
    """
    try:
        query_vector = embed_query(query)
        if not query_vector:
            return None

        pipeline = [
            {
                "$vectorSearch": {
                    "index": "book_chunks_vector_index",
                    "path": "embedding",
                    "queryVector": query_vector,
                    "numCandidates": top_k * 10,
                    "limit": top_k,
                    "filter": {"book_id": book_id, "user_id": user_id},
                }
            },
            {
                "$project": {
                    "text": 1,
                    "chunk_index": 1,
                    "_id": 0,
                }
            },
        ]

        cursor = book_chunks_collection.aggregate(pipeline)
        chunks = await cursor.to_list(length=top_k)

        if not chunks:
            return None

        # Sort by chunk_index to preserve reading order
        chunks.sort(key=lambda c: c.get("chunk_index", 0))
        combined = "\n\n---\n\n".join(c["text"] for c in chunks)
        return combined

    except Exception as exc:
        logger.warning("[book_rag] Retrieval failed for book %s: %s", book_id, exc)
        return None
