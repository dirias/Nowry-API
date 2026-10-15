"""PLAN-009 — the companion's block moments are claimed once per account, and
the quarter report reads the blocks.

  1. A moment key is claimed on the account with one conditional update; a
     second claim answers already_seen; a malformed key is refused.
  2. The quarter's block summary is pure: planned from the rules (after each
     block's creation), kept rate with partials at half, minutes by source and
     area, study minutes against the plan, the most-kept rows first.
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bson import ObjectId
from fastapi import HTTPException

from tests._stubs import stub_if_missing

stub_if_missing("bcrypt", "langfuse", "langfuse.langchain")
if "app.auth.firebase_auth" not in sys.modules:
    _mock = MagicMock()
    _mock.get_firebase_user = MagicMock()
    sys.modules["app.auth.firebase_auth"] = _mock

from app.services import blocks as svc  # noqa: E402

USER_ID = "507f1f77bcf86cd799439011"


def _users(modified: int) -> MagicMock:
    users = MagicMock()
    users.update_one = AsyncMock(return_value=MagicMock(modified_count=modified))
    return users


@pytest.mark.asyncio
async def test_a_moment_is_claimed_once_and_the_second_claim_is_already_seen():
    from app.routers.agent import InterventionRequest, _claim_block_moment

    body = InterventionRequest(type="block_moment", block_reason="kept_after_skips", moment_key="kept_after_skips:b1:2026-10-15")
    users = _users(1)
    with patch("app.routers.agent.users_collection", users):
        assert await _claim_block_moment(body, USER_ID) is True
    query, update = users.update_one.call_args.args
    assert query == {"_id": ObjectId(USER_ID), "agent.seen_block_moments": {"$ne": "kept_after_skips:b1:2026-10-15"}}
    assert update["$push"]["agent.seen_block_moments"]["$each"] == ["kept_after_skips:b1:2026-10-15"]
    assert update["$push"]["agent.seen_block_moments"]["$slice"] == -200

    with patch("app.routers.agent.users_collection", _users(0)):
        assert await _claim_block_moment(body, USER_ID) is False


@pytest.mark.asyncio
async def test_a_malformed_key_is_refused():
    from app.routers.agent import InterventionRequest, _claim_block_moment

    body = InterventionRequest(type="block_moment", block_reason="week_kept", moment_key="week_kept:$ne")
    with patch("app.routers.agent.users_collection", _users(1)):
        with pytest.raises(HTTPException) as refused:
            await _claim_block_moment(body, USER_ID)
    assert refused.value.status_code == 422


def _block(title: str, source: str, days: list[int], length: int, created: datetime, area: str | None = None) -> dict:
    return {
        "_id": ObjectId(),
        "user_id": USER_ID,
        "title": title,
        "source": {"type": source, "id": None},
        "start": "07:00",
        "length_min": length,
        "days_of_week": days,
        "date": None,
        "area_id": area,
        "created_at": created,
        "deleted_at": None,
    }


def test_quarter_blocks_summary_reads_the_rules_and_the_log():
    created = datetime(2026, 10, 1, tzinfo=timezone.utc)
    review = _block("Morning review", "study", [0, 1, 2, 3, 4], 20, created)
    run = _block("Run", "goal", [0, 2, 4], 30, datetime(2026, 10, 8, tzinfo=timezone.utc), area="area-1")
    # Mon 5 Oct … Sun 11 Oct 2026: review on 5 weekdays; run created Thu 8th, so Fri 9th only.
    start, end = date(2026, 10, 5), date(2026, 10, 11)
    logs = [
        {"block_id": str(review["_id"]), "date": "2026-10-05", "status": "kept", "minutes": 18},
        {"block_id": str(review["_id"]), "date": "2026-10-06", "status": "partial", "minutes": 8},
        {"block_id": str(review["_id"]), "date": "2026-10-07", "status": "skipped", "minutes": 0},
        {"block_id": str(run["_id"]), "date": "2026-10-09", "status": "kept", "minutes": 30},
    ]

    out = svc.quarter_blocks_summary([review, run], logs, start, end)

    assert out["planned"] == 6 and out["kept"] == 2 and out["partial"] == 1 and out["skipped"] == 1
    assert out["kept_rate"] == round(2.5 / 6, 3)
    assert out["minutes"] == 56 and out["minutes_by_source"] == {"study": 26, "goal": 30} and out["minutes_by_area"] == {"area-1": 30}
    assert out["study_minutes_planned"] == 100 and out["study_minutes_kept"] == 26
    assert [r["title"] for r in out["rows"]] == ["Morning review", "Run"]
    assert out["rows"][0] == {"title": "Morning review", "planned": 5, "kept": 1}
