"""
User document models.

Also owns the typed ``users.onboarding`` subdocument and the pure normalization
helpers that back ``GET/PATCH /users/onboarding`` (ADR-003). The subdocument
records journey state only — status, last meaningful point, and postponement
time. Language, accent color, interests, primary topic, and study goal stay in
``preferences.general`` and are never duplicated here.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict

from .mixins import SoftDeleteMixin


class User(BaseModel, SoftDeleteMixin):
    firebase_uid: str  # Reference to Firebase Auth user
    username: str
    email: str
    role: Optional[str] = "user"  # user, dev, admin
    is_beta: Optional[bool] = False  # Beta tester flag
    # password: str  # REMOVED - Firebase handles authentication


# ---------------------------------------------------------------------------
# Onboarding journey state (ADR-003)
# ---------------------------------------------------------------------------

OnboardingStatus = Literal["incomplete", "activated"]
OnboardingPoint = Literal["welcome", "personalization", "first_deck"]

#: Journey progress is monotonic; ranks let the server keep the furthest point.
ONBOARDING_POINT_ORDER = {"welcome": 0, "personalization": 1, "first_deck": 2}

#: Welcome is the default entry point and is therefore not client-recordable.
RECORDABLE_ONBOARDING_POINTS = frozenset({"personalization", "first_deck"})

#: Postponement hides the Home re-entry point for this long (FR-042).
ONBOARDING_REENTRY_GRACE = timedelta(hours=24)


class OnboardingState(BaseModel):
    """Normalized journey state — the persisted shape plus legacy defaults."""

    model_config = ConfigDict(extra="ignore")

    status: OnboardingStatus = "incomplete"
    last_meaningful_point: OnboardingPoint = "welcome"
    postponed_at: Optional[datetime] = None
    activated_at: Optional[datetime] = None
    updated_at: datetime


def coerce_utc(value: object) -> Optional[datetime]:
    """Return ``value`` as an aware UTC datetime, or ``None`` when unusable.

    MongoDB hands back naive datetimes that are UTC by storage contract; the
    24-hour grace period must never compare a naive value against server time.
    """
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def onboarding_point_rank(point: object) -> int:
    """Rank an arbitrary stored value; unknown points fall back to Welcome."""
    return ONBOARDING_POINT_ORDER.get(point, 0) if isinstance(point, str) else 0


def later_onboarding_point(current: object, candidate: str) -> str:
    """Return whichever point is further along the journey."""
    if onboarding_point_rank(current) > onboarding_point_rank(candidate):
        return str(current)
    return candidate


def normalize_onboarding_state(
    user_doc: dict,
    now: Optional[datetime] = None,
) -> OnboardingState:
    """Resolve journey state for any user document, no migration required.

    Legacy users have no ``onboarding`` subdocument: ``wizard_completed=True``
    resolves to activated (routing compatibility, ADR-003), anything else
    resolves to an incomplete Welcome journey.
    """
    raw = user_doc.get("onboarding")
    raw = raw if isinstance(raw, dict) else {}

    status = raw.get("status")
    if user_doc.get("wizard_completed") is True:
        status = "activated"
    elif status not in ("incomplete", "activated"):
        status = "incomplete"

    point = raw.get("last_meaningful_point")
    if not isinstance(point, str) or point not in ONBOARDING_POINT_ORDER:
        point = "welcome"

    updated_at = (
        coerce_utc(raw.get("updated_at"))
        or coerce_utc(user_doc.get("updated_at"))
        or coerce_utc(user_doc.get("created_at"))
        or now
        or datetime.now(timezone.utc)
    )

    return OnboardingState(
        status=status,
        last_meaningful_point=point,
        postponed_at=coerce_utc(raw.get("postponed_at")),
        activated_at=coerce_utc(raw.get("activated_at")),
        updated_at=updated_at,
    )


def onboarding_show_reentry(
    state: OnboardingState,
    now: Optional[datetime] = None,
) -> bool:
    """Server-derived Home re-entry visibility (FR-043, FR-045, ADR-007).

    True only while the journey is incomplete and either nothing was postponed
    or at least 24 hours of server time have elapsed since the postponement.
    """
    if state.status != "incomplete":
        return False
    if state.postponed_at is None:
        return True
    reference = now or datetime.now(timezone.utc)
    return reference - state.postponed_at >= ONBOARDING_REENTRY_GRACE


def onboarding_resume_screen(state: OnboardingState) -> Optional[str]:
    """Server-normalized resume target; activated journeys resume nowhere."""
    if state.status == "activated":
        return None
    return state.last_meaningful_point


# ---------------------------------------------------------------------------
# Activation (ADR-006) — owned by the verified fork workflow, never by a client
# ---------------------------------------------------------------------------


class OnboardingActivation(BaseModel):
    """The activation facts a successful onboarding fork reports back.

    Deliberately narrower than :class:`OnboardingState`: the fork response
    confirms the transition it just performed, it is not a second journey-state
    contract. ``GET /users/onboarding`` remains the way to read full state.
    """

    status: Literal["activated"] = "activated"
    activated_at: datetime


def onboarding_activation_update(now: datetime) -> dict:
    """Build the ``$set`` document that activates a journey.

    Every field lands in one update on one document, so activation is atomic:
    no reader can ever observe ``wizard_completed`` without the matching
    ``onboarding`` fields, or the reverse. ``last_meaningful_point`` is
    deliberately not written here — activation is not a screen, and an
    activated journey resumes nowhere regardless of the point it stored.

    Only the fork workflow may call this (FR-006): activation must be the
    consequence of a verified official copy, never of reaching a screen.
    """
    return {
        "wizard_completed": True,
        "onboarding.status": "activated",
        "onboarding.activated_at": now,
        "onboarding.updated_at": now,
        "updated_at": now,
    }
