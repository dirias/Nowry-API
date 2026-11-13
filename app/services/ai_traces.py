"""
Langfuse traces that belong to an account, and deleting them with it (ADR-081 amendment).

Langfuse can delete traces by ID but has no "delete this user" call, and on the
Hobby plan its API can only find the last 30 days; older traces stay stored with
no end date. So the API remembers, as each span is exported, which trace belongs
to which internal account id, and deletes every remembered trace when the
account is deleted, together with any the observations API still finds for it.

    should_export_span(span)       Langfuse's export hook: records, then defers to the default filter
    flush_loop()                   writes recorded pairs to `ai_traces`; retries pending deletions
    delete_user_traces(user_id)    at account deletion; never raises

Nothing but the two ids and a time is stored here: no prompt, no answer.
"""
from __future__ import annotations

import asyncio
import os
import queue
from datetime import datetime, timedelta, timezone
from typing import Iterable, List, Optional, Set, Tuple

import httpx

from app.utils.logger import get_logger

logger = get_logger(__name__)

#: How often recorded pairs are written, and pending deletions retried.
FLUSH_SECONDS = 10
RETRY_SECONDS = 15 * 60
#: Langfuse takes up to 1,000 trace IDs per delete call and returns up to 1,000 rows per page.
BATCH = 1000
#: What the Hobby plan's API can still find, in days.
FINDABLE_DAYS = 30
#: A bound on the pages read for one account, so a deletion never loops unbounded.
MAX_PAGES = 50
MAX_ATTEMPTS = 20
TIMEOUT = httpx.Timeout(20.0)

_recorded: "queue.SimpleQueue[Tuple[str, str]]" = queue.SimpleQueue()


def should_export_span(span) -> bool:
    """Langfuse's `should_export_span`: remember (account, trace) and keep the default decision."""
    from langfuse._client.span_filter import is_default_export_span

    export = is_default_export_span(span)
    if export:
        try:
            user_id = (span.attributes or {}).get("user.id")
            if user_id:
                _recorded.put((str(user_id), format(span.context.trace_id, "032x")))
        except Exception:  # noqa: BLE001 - recording must never stop a trace from being sent
            pass
    return export


def _drain(limit: int = 5000) -> List[Tuple[str, str]]:
    pairs: List[Tuple[str, str]] = []
    while len(pairs) < limit:
        try:
            pairs.append(_recorded.get_nowait())
        except queue.Empty:
            break
    return pairs


async def flush_recorded() -> int:
    """Write what was recorded since the last flush. Returns how many distinct traces were written."""
    from pymongo import UpdateOne

    from app.config.database import ai_traces_collection

    pairs = _drain()
    if not pairs:
        return 0
    now = datetime.now(timezone.utc)
    by_trace = {trace_id: user_id for user_id, trace_id in pairs}
    ops = [
        UpdateOne({"trace_id": trace_id}, {"$setOnInsert": {"trace_id": trace_id, "user_id": user_id, "created_at": now}}, upsert=True)
        for trace_id, user_id in by_trace.items()
    ]
    await ai_traces_collection.bulk_write(ops, ordered=False)
    return len(ops)


def _credentials() -> Optional[Tuple[str, str, str]]:
    public, secret = os.getenv("LANGFUSE_PUBLIC_KEY"), os.getenv("LANGFUSE_SECRET_KEY")
    if not public or not secret:
        return None
    base = os.getenv("LANGFUSE_BASE_URL", "https://cloud.langfuse.com")
    return (base if base.startswith("https://") else "https://cloud.langfuse.com").rstrip("/"), public, secret


async def _find_recent(client: httpx.AsyncClient, base: str, user_id: str, now: datetime) -> Set[str]:
    """Trace IDs the observations API still finds for the account (the last 30 days on Hobby)."""
    found: Set[str] = set()
    params = {
        "userId": user_id,
        "fromStartTime": (now - timedelta(days=FINDABLE_DAYS)).isoformat(),
        "toStartTime": (now + timedelta(minutes=5)).isoformat(),
        "fields": "core",
        "limit": BATCH,
    }
    for _ in range(MAX_PAGES):
        response = await client.get(f"{base}/api/public/v2/observations", params=params)
        response.raise_for_status()
        body = response.json()
        found.update(row["traceId"] for row in body.get("data", []) if row.get("traceId"))
        cursor = (body.get("meta") or {}).get("cursor")
        if not cursor:
            break
        params["cursor"] = cursor
    return found


async def _delete(client: httpx.AsyncClient, base: str, trace_ids: Iterable[str]) -> None:
    ids = sorted(set(trace_ids))
    for start in range(0, len(ids), BATCH):
        response = await client.request("DELETE", f"{base}/api/public/traces", json={"traceIds": ids[start : start + BATCH]})
        response.raise_for_status()


async def _remembered(user_id: str) -> List[str]:
    from app.config.database import ai_traces_collection

    rows = await ai_traces_collection.find({"user_id": user_id}, {"trace_id": 1}).to_list(length=100_000)
    return [row["trace_id"] for row in rows]


async def _erase(user_id: str) -> int:
    """Delete the account's traces at Langfuse, then its ledger rows. Raises on a Langfuse failure."""
    from app.config.database import ai_traces_collection

    credentials = _credentials()
    if credentials is None:
        return 0
    base, public, secret = credentials
    await flush_recorded()  # a trace made a moment ago is in the queue, not yet in the ledger
    async with httpx.AsyncClient(auth=(public, secret), timeout=TIMEOUT) as client:
        trace_ids = set(await _remembered(user_id)) | await _find_recent(client, base, user_id, datetime.now(timezone.utc))
        await _delete(client, base, trace_ids)
    await ai_traces_collection.delete_many({"user_id": user_id})
    return len(trace_ids)


async def delete_user_traces(user_id: str) -> bool:
    """At account deletion: delete the account's Langfuse traces. On failure, queue a retry. Never raises."""
    from app.config.database import ai_trace_deletions_collection

    try:
        count = await _erase(user_id)
        logger.info("account deletion: %d Langfuse traces deleted", count)
        return True
    except Exception as exc:  # noqa: BLE001 - Langfuse must never undo a deletion
        logger.error("account deletion: Langfuse traces not deleted yet, will retry: %s", type(exc).__name__)
        try:
            await ai_trace_deletions_collection.update_one(
                {"user_id": user_id},
                {"$setOnInsert": {"user_id": user_id, "requested_at": datetime.now(timezone.utc), "attempts": 0}},
                upsert=True,
            )
        except Exception as store_exc:  # noqa: BLE001
            logger.error("account deletion: the Langfuse retry could not be queued: %s", type(store_exc).__name__)
        return False


async def retry_pending() -> None:
    """Retry deletions that failed. A row leaves when it succeeds or after MAX_ATTEMPTS (logged)."""
    from app.config.database import ai_trace_deletions_collection

    pending = await ai_trace_deletions_collection.find({"attempts": {"$lt": MAX_ATTEMPTS}}).to_list(length=200)
    for row in pending:
        try:
            await _erase(row["user_id"])
            await ai_trace_deletions_collection.delete_one({"_id": row["_id"]})
        except Exception as exc:  # noqa: BLE001
            attempts = row.get("attempts", 0) + 1
            await ai_trace_deletions_collection.update_one({"_id": row["_id"]}, {"$set": {"attempts": attempts}})
            if attempts >= MAX_ATTEMPTS:
                logger.error("Langfuse traces of a deleted account could not be deleted after %d attempts: %s", attempts, type(exc).__name__)


async def flush_loop() -> None:
    """Runs for the life of the app: writes recorded pairs, and retries pending deletions."""
    since_retry = RETRY_SECONDS  # retry once soon after a start
    while True:
        try:
            await flush_recorded()
            if since_retry >= RETRY_SECONDS:
                since_retry = 0
                await retry_pending()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the loop must outlive a database hiccup
            logger.warning("ai_traces: flush failed: %s", type(exc).__name__)
        await asyncio.sleep(FLUSH_SECONDS)
        since_retry += FLUSH_SECONDS
