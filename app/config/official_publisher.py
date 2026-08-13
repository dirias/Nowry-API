# app/config/official_publisher.py
"""Configuration for the trusted official Nowry publisher account (ADR-004).

Official seed decks are published by one designated account. That account's
identity is deployment configuration, never request data — which is what makes
`is_official` a server-derived projection rather than a badge a client can
claim.

`OFFICIAL_PUBLISHER_USER_ID` holds the value stored in `decks.user_id` for that
account (the Mongo user `_id` string, which is what `current_user["user_id"]`
carries throughout this codebase).

Values are read per call rather than captured at import time so a deployment
can rotate the account without a code change, and so tests can set the variable
around a single call.

When the variable is unset the official predicate is unsatisfiable: curated
browse returns an ordinary empty page and the curation operation refuses to
write. An unconfigured deployment therefore fails closed — it can never mark
anything official.
"""
import os
from typing import Optional

OFFICIAL_PUBLISHER_USER_ID_VAR = "OFFICIAL_PUBLISHER_USER_ID"
OFFICIAL_PUBLISHER_NAME_VAR = "OFFICIAL_PUBLISHER_NAME"

#: Display identity shown on the official mark when no override is configured.
DEFAULT_OFFICIAL_PUBLISHER_NAME = "Nowry"


def get_official_publisher_user_id() -> Optional[str]:
    """Return the configured official publisher user id, or None when unset."""
    return os.getenv(OFFICIAL_PUBLISHER_USER_ID_VAR, "").strip() or None


def get_official_publisher_name() -> str:
    """Return the publisher display name used by the official mark."""
    return (
        os.getenv(OFFICIAL_PUBLISHER_NAME_VAR, "").strip()
        or DEFAULT_OFFICIAL_PUBLISHER_NAME
    )


def is_official_publisher(user_id: object) -> bool:
    """Return True when `user_id` is the configured official publisher."""
    configured = get_official_publisher_user_id()
    return bool(configured) and str(user_id) == configured
