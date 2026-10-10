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

from datetime import datetime, timedelta, timezone
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


# ---------------------------------------------------------------------------
# SR-002 — the soft landing (PRD prd-rest.md D5, ADR-043 point 5).
#
# A set of cards whose next_review is in the past would all land today. They
# are spread forward instead: most-overdue first, at most `per_day` a day, at
# most MAX_SPREAD_DAYS out. Only next_review moves, only forward, only for
# cards that are already late. Ease, interval, repetitions and last_reviewed
# are never touched, so the next grade schedules normally.
# ---------------------------------------------------------------------------

MAX_SPREAD_DAYS = 14

#: The only keys a spread may write.
SPREAD_FIELDS = ("next_review", "updated_at")


def spread_plan(cards: list, per_day: int, today_start: datetime, max_days: int = MAX_SPREAD_DAYS) -> list:
    """Which day each overdue card lands on: ``[(id, next_review)]``, most-overdue first.

    ``per_day`` is the deck's review cap. When the pile needs more than
    ``max_days`` at that pace, the pace rises so the last card still lands
    within ``max_days`` — the cap on days is the promise ("within two weeks"),
    the cap on cards a preference. A card already due today is left where it is.
    Pure, and deterministic for a given order.
    """
    overdue = sorted(
        (c for c in cards if c.get("next_review") is not None and c["next_review"] < today_start),
        key=lambda c: (c["next_review"], str(c.get("_id"))),
    )
    if not overdue:
        return []
    per_day = max(1, int(per_day))
    days_needed = -(-len(overdue) // per_day)  # ceil
    if days_needed > max_days:
        per_day = -(-len(overdue) // max_days)
    plan = []
    for index, card in enumerate(overdue):
        day = min(index // per_day, max_days - 1)
        plan.append((card["_id"], today_start + timedelta(days=day)))
    return plan


def spread_days(plan: list) -> int:
    """How many days a plan spans, for the sentence the Today object says."""
    if not plan:
        return 0
    return len({when for _, when in plan})


async def spread_cards(collection: Collection, match: dict, per_day: int, now: datetime, include_today: bool = False) -> dict:
    """Spread the overdue cards in ``match`` forward and report ``{cards, days}``.

    ``include_today`` is "Lighten today": cards due today count as late too,
    so the overflow moves to tomorrow onward. Writes SPREAD_FIELDS only, one
    update per landing day.
    """
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    cutoff = now if include_today else today_start
    late = await collection.find(
        {**match, **awake_clause(now), "last_reviewed": {"$ne": None}, "next_review": {"$lt": cutoff}},
        {"_id": 1, "next_review": 1},
    ).to_list(length=5000)
    # "Late" for the plan is relative to the cutoff: under Lighten today, a card
    # due this morning is late too.
    plan = spread_plan(late, per_day, today_start) if not include_today else spread_plan(
        [{**c, "next_review": min(c["next_review"], today_start - timedelta(seconds=1))} for c in late], per_day, today_start
    )
    by_day: dict = {}
    for card_id, when in plan:
        by_day.setdefault(when, []).append(card_id)
    for when, ids in by_day.items():
        if when <= today_start and not include_today:
            # Day 0 lands today, where they already are: nothing to write.
            continue
        await collection.update_many({"_id": {"$in": ids}}, {"$set": {"next_review": when, "updated_at": now}})
    return {"cards": len(plan), "days": spread_days(plan)}
