"""
What the beta gate reads from the database (ADR-077).

The rules live in `app/config/beta.py` and take these as arguments, so they are
tested without Mongo.
"""
from __future__ import annotations

from app.config.database import users_collection


async def count_code_uses(code: str, ceiling: int) -> int:
    """How many accounts carry this cohort code, counted no further than `ceiling`.

    Deleted accounts count: the code made them. The limit bounds the read, and
    the sparse index on `beta.invite_code` serves it.
    """
    return await users_collection.count_documents({"beta.invite_code": code}, limit=max(ceiling, 1))
