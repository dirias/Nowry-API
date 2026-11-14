"""
The bug report's limits (ADMIN-008a, docs/prd-admin.md FR-008).

Pure checks the `POST /bugs` route applies in order: text lengths, then the
screenshots, then the account's reports of the UTC day. The 7 MB body cap is
`app/core/body_limit.py`, which runs before the body is parsed.
"""
from __future__ import annotations

import base64
import binascii
from datetime import datetime, timedelta
from typing import Dict, Iterable, Optional, Tuple

#: The most characters each text field keeps; a longer one is refused by name.
TEXT_LIMITS: Dict[str, int] = {
    "title": 140,
    "description": 4000,
    "steps_to_reproduce": 4000,
    "expected_behavior": 4000,
    "actual_behavior": 4000,
}
MAX_SCREENSHOTS: int = 3
#: The most bytes one screenshot holds once decoded.
MAX_SCREENSHOT_BYTES: int = 2 * 1024 * 1024
#: The most bytes a whole report request may carry.
MAX_BODY_BYTES: int = 7 * 1024 * 1024
#: Reports one account may send per UTC day.
DAILY_REPORTS: int = 10

#: First bytes of the image types a screenshot may be.
_PNG = b"\x89PNG\r\n\x1a\n"
_JPEG = b"\xff\xd8\xff"


def too_large(field: str) -> Dict[str, str]:
    """The 413 detail naming the field that broke its limit."""
    return {"code": "too_large", "field": field}


def oversized_text(texts: Dict[str, Optional[str]]) -> Optional[str]:
    """The first text field longer than its limit, or None."""
    for field, limit in TEXT_LIMITS.items():
        if len(texts.get(field) or "") > limit:
            return field
    return None


def decode_screenshot(data: str) -> Optional[bytes]:
    """The image bytes of a data URL or bare base64 string, or None when it is not base64.

    Cheap guard first: base64 grows 4/3, so a string far over the cap is refused unread.
    """
    encoded = data.split(",", 1)[1] if data.startswith("data:") and "," in data else data
    if len(encoded) > (MAX_SCREENSHOT_BYTES // 3 + 1) * 4 + 8:
        return None
    try:
        return base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        return None


def is_allowed_image(raw: bytes) -> bool:
    """PNG, JPEG or WebP by the file's first bytes."""
    return raw.startswith(_PNG) or raw.startswith(_JPEG) or (raw[:4] == b"RIFF" and raw[8:12] == b"WEBP")


def screenshots_ok(datas: Iterable[str]) -> bool:
    """At most three screenshots, each base64, at most 2 MB decoded, and an allowed image."""
    datas = list(datas)
    if len(datas) > MAX_SCREENSHOTS:
        return False
    for data in datas:
        raw = decode_screenshot(data)
        if raw is None or len(raw) > MAX_SCREENSHOT_BYTES or not is_allowed_image(raw):
            return False
    return True


def utc_day_bounds(now: datetime) -> Tuple[datetime, datetime]:
    """The UTC midnight that started `now`'s day and the one that ends it."""
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


def daily_limit(retry_at: datetime) -> Dict[str, str]:
    """The 429 detail: when the account may report again."""
    return {"code": "daily_limit", "retry_at": retry_at.isoformat()}
