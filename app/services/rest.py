"""Rest — content out of play, at three grains, materialized on the card (SR-001, ADR-043).

A card rests if it is rested itself, or carries a rested tag, or sits in a rested
deck. Each grain writes a *source* onto the card — ``card``, ``tag:<name>``,
``deck`` — with its own "until", and the card carries one derived ``rest_until``:
``None`` when awake, :data:`INDEFINITE` when resting until woken, otherwise the
latest of its live sources. Every count and the session selection read that one
field through :func:`awake_clause`, so six due computations each gain one clause
instead of a join across tags and decks (PRD ``prd-rest.md`` D2, E4).

Rest writes exactly its own fields — ``rests``, ``rest_until``, ``rested_at``,
``updated_at`` — and nothing the scheduler owns. The narrowness is the
guarantee (ADR-010 applied): ``tests/test_rest.py`` pins the ``$set`` keys.

A dated rest ends by itself: nothing wakes it, the field simply compares past
``now`` (D3). An expired entry is dropped the next time the card's rests are
rewritten, which is why every writer re-filters the array.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from pymongo.collection import Collection

#: "Until woken": a far date, so one field answers both "is it resting" and
#: "until when", and a dated rest and an open one sort the same way.
INDEFINITE = datetime(9999, 12, 31)

SOURCE_CARD = "card"
SOURCE_DECK = "deck"
TAG_PREFIX = "tag:"

#: The only keys a rest or wake may write (ADR-043 point 1).
REST_FIELDS = ("rests", "rest_until", "rested_at", "updated_at")


def tag_source(tag: str) -> str:
    return f"{TAG_PREFIX}{tag}"


def now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def until_or_indefinite(until: Optional[datetime]) -> datetime:
    """A request's ``until``: a naive UTC date, or the far date for "until woken"."""
    if until is None:
        return INDEFINITE
    if until.tzinfo is not None:
        until = until.astimezone(timezone.utc).replace(tzinfo=None)
    return until


def awake_clause(now: datetime) -> dict:
    """The one clause every count and selection carries: not resting at ``now``.

    ``$not: {$gt: now}`` matches a missing field, a null, and a date at or
    before ``now`` — so a card written before the field existed is awake, and a
    dated rest ends by itself (D3). One key, so it composes with ``**`` into any
    match that already has an ``$or``.
    """
    return {"rest_until": {"$not": {"$gt": now}}}


def resting_clause(now: datetime) -> dict:
    return {"rest_until": {"$gt": now}}


def resting_expr(now: datetime) -> dict:
    """The aggregation-expression twin of :func:`resting_clause`, for ``$cond``."""
    return {"$gt": ["$rest_until", now]}


def derive_rest_until(rests: Optional[list], now: datetime) -> Optional[datetime]:
    """The card's one field from its sources: the latest live ``until``, or None.

    Pure, and the reference the pipeline below must agree with: a source whose
    ``until`` is at or before ``now`` is spent and does not count.
    """
    live = [entry.get("until") for entry in (rests or []) if entry.get("until") is not None and entry["until"] > now]
    return max(live) if live else None


def _live_other_sources(now: datetime, drop_source: Optional[str] = None, drop_prefix: Optional[str] = None) -> dict:
    """``$filter`` keeping the entries that are still live and not the one being rewritten."""
    conditions = [{"$gt": ["$$this.until", now]}]
    if drop_source is not None:
        conditions.append({"$ne": ["$$this.source", drop_source]})
    if drop_prefix is not None:
        conditions.append({"$not": [{"$regexMatch": {"input": "$$this.source", "regex": f"^{drop_prefix}"}}]})
    return {"$filter": {"input": {"$ifNull": ["$rests", []]}, "cond": {"$and": conditions}}}


def rest_update(source: str, until: datetime, now: datetime) -> list:
    """A pipeline update that adds (or refreshes) ``source`` and re-derives ``rest_until``.

    Only :data:`REST_FIELDS` are set. ``rested_at`` is kept once set, so a card
    rested, woken and rested again remembers its first rest.
    """
    return [
        {"$set": {"rests": {"$concatArrays": [_live_other_sources(now, drop_source=source), [{"source": source, "until": until, "since": now}]]}}},
        {"$set": {"rest_until": {"$max": "$rests.until"}, "rested_at": {"$ifNull": ["$rested_at", now]}, "updated_at": now}},
    ]


def wake_update(now: datetime, source: Optional[str] = None, source_prefix: Optional[str] = None) -> list:
    """A pipeline update that removes ``source`` (or every source with the prefix) and re-derives.

    ``$max`` over an empty array is null, which is exactly "awake".
    """
    return [
        {"$set": {"rests": _live_other_sources(now, drop_source=source, drop_prefix=source_prefix)}},
        {"$set": {"rest_until": {"$max": "$rests.until"}, "updated_at": now}},
    ]


async def rest_many(collection: Collection, match: dict, source: str, until: Optional[datetime], now: datetime) -> int:
    result = await collection.update_many(match, rest_update(source, until_or_indefinite(until), now))
    return int(result.matched_count)


async def wake_many(collection: Collection, match: dict, now: datetime, source: Optional[str] = None, source_prefix: Optional[str] = None) -> int:
    result = await collection.update_many(match, wake_update(now, source=source, source_prefix=source_prefix))
    return int(result.matched_count)


async def resting_tag_settings(tag_settings: Collection, user_id: str, now: datetime) -> list:
    """The user's tags that rest at ``now``: ``[{tag, rest_until}]``, at most 500."""
    return await tag_settings.find(
        {"user_id": user_id, **resting_clause(now)}, {"tag": 1, "rest_until": 1}
    ).to_list(length=500)


async def sync_tag_rests(collection: Collection, tag_settings: Collection, user_id: str, card_match: dict, now: datetime) -> None:
    """Re-apply every resting tag of the user to the cards in ``card_match`` that carry it (D9).

    Called after anything that can give a card a tag — create, PATCH, bulk tag,
    rename — so a card that joins a resting tag rests with it.
    """
    for setting in await resting_tag_settings(tag_settings, user_id, now):
        await rest_many(collection, {**card_match, "tags": setting["tag"]}, tag_source(setting["tag"]), setting.get("rest_until"), now)


def deck_is_resting(deck_doc: Optional[dict], now: datetime) -> bool:
    until = (deck_doc or {}).get("rest_until")
    return until is not None and until > now


async def sync_deck_rest(collection: Collection, target_deck: Optional[dict], card_match: dict, now: datetime, left_resting_deck: bool = False) -> None:
    """Make the cards in ``card_match`` carry their new deck's rest (D9).

    A card moved into a resting deck rests. A card that LEFT a resting deck
    drops that deck's hold; a card that left an awake deck carried none, so
    nothing is written for it — writes happen only when a hold changes.
    """
    if deck_is_resting(target_deck, now):
        await rest_many(collection, card_match, SOURCE_DECK, target_deck["rest_until"], now)
    elif left_resting_deck:
        await wake_many(collection, card_match, now, source=SOURCE_DECK)
