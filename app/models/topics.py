"""
Canonical learning-topic taxonomy.

Single source of truth shared by:
  - app/routers/users.py            (request validation + response sanitisation)
  - app/migrations/                 (one-time normalisation of legacy documents)

Canonical form is **lowercase snake_case**. The onboarding wizard has always
written this form; the account-settings screen historically wrote Title Case,
which is why existing documents contain mixed-case duplicates such as
``['technology', 'Technology']``. Keeping the taxonomy in one module prevents
the router and the migration from drifting apart again.
"""
from __future__ import annotations

from typing import Literal, get_args

TopicValue = Literal[
    'artificial_intelligence',
    'technology',
    'science',
    'mathematics',
    'history',
    'languages',
    'literature',
    'art',
    'music',
    'business',
    'health',
    'philosophy',
    'design',
    'psychology',
]

#: Fast membership set derived from ``TopicValue`` so the two can never diverge.
TOPIC_TAXONOMY: frozenset[str] = frozenset(get_args(TopicValue))

#: Maximum number of interests a user may store.
MAX_INTERESTS: int = 5

#: Legacy free-text values that predate the taxonomy and map onto a canonical
#: value. Keys are already lowercased/underscored by ``normalize_topic``.
LEGACY_TOPIC_ALIASES: dict[str, str] = {
    'ai': 'artificial_intelligence',
    'a.i.': 'artificial_intelligence',
    'machine_learning': 'artificial_intelligence',
    'tech': 'technology',
    'maths': 'mathematics',
    'math': 'mathematics',
    'language': 'languages',
    'arts': 'art',
    'literature_&_writing': 'literature',
    'health_&_fitness': 'health',
}


def normalize_topic(raw: object) -> str | None:
    """
    Coerce a single stored topic value to its canonical taxonomy form.

    Returns ``None`` when the value is not a string, or does not resolve to one
    of the 14 canonical values. Callers decide whether an unresolvable value is
    dropped (interests) or nulled (primary_topic).
    """
    if not isinstance(raw, str):
        return None

    key = raw.strip().lower().replace(' ', '_').replace('-', '_')
    if not key:
        return None

    key = LEGACY_TOPIC_ALIASES.get(key, key)
    return key if key in TOPIC_TAXONOMY else None


def normalize_interests(raw: object) -> list[str]:
    """
    Coerce a stored ``interests`` array to the canonical taxonomy.

    Lowercases, resolves legacy aliases, drops values outside the taxonomy,
    de-duplicates **preserving first-seen order** (order is load-bearing —
    ``interests[0]`` becomes ``primary_topic``), and caps the result at
    ``MAX_INTERESTS``.

    Idempotent: ``normalize_interests(normalize_interests(x)) == normalize_interests(x)``.
    Never raises — a non-list input yields an empty list.
    """
    if not isinstance(raw, list):
        return []

    cleaned: list[str] = []
    for value in raw:
        topic = normalize_topic(value)
        if topic is None or topic in cleaned:
            continue
        cleaned.append(topic)
        if len(cleaned) == MAX_INTERESTS:
            break

    return cleaned


def derive_primary_topic(
    primary_topic: object,
    normalized_interests: list[str],
) -> str | None:
    """
    Resolve ``primary_topic`` against an already-normalized ``interests`` list.

    ``primary_topic`` is derived from ``interests[0]`` whenever the stored value
    is missing, outside the taxonomy, or absent from the user's interests. This
    guarantees the agent persona prompt never reads a primary topic the user did
    not actually select.
    """
    topic = normalize_topic(primary_topic)
    if topic is not None and topic in normalized_interests:
        return topic
    return normalized_interests[0] if normalized_interests else None
