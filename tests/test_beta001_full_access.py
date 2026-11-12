"""
BETA-001 / BETA-002 — a beta account is answered as Pro by a rule (ADR-079, FR-025).

  1. The rule: paid wins, a stamp counts whatever its cohort, no stamp is the
     stored plan, and the grant closes with its flag or after its last day.
  2. The caps: a tester's month, and Pro's limit for everything else.
  3. The AI ceiling counts a tester against 200, once per request, and an
     answer that came from the fallback does not count again.
  4. No router reads `subscription.tier` itself.
  5. What an account endpoint says about the plan.
"""
import importlib
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from tests._stubs import stub_if_missing

stub_if_missing("langfuse", "langfuse.langchain")
sys.modules.setdefault("app.models.agent_models", MagicMock())

import pytest
from bson import ObjectId
from fastapi import HTTPException

from app.ai_orchestrator.llm_clients.gemini_client import GeminiQuotaError
from app.config import beta
from app.config.entitlements import (
    ACCESS_PROJECTION,
    BETA_LIMITS,
    Access,
    access_fields,
    access_limit,
    access_limits,
    effective_tier,
    has_beta_stamp,
    resolve_access,
)
from app.config.subscription_plans import plan_limit
from app.core import llm_health

APP = Path(__file__).resolve().parents[1] / "app"
USER = "507f1f77bcf86cd799439011"
NOW = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)
OPEN_UNTIL = datetime(2099, 1, 1, tzinfo=timezone.utc)
STAMP = {"cohort": "friends", "invite_code": "c0-7f3a", "joined_at": NOW}


def _account(tier: str = "free", stamp=None) -> dict:
    return {"_id": ObjectId(USER), "subscription": {"tier": tier}, "beta": stamp}


@pytest.fixture
def grant_on(monkeypatch):
    monkeypatch.setenv("BETA_FULL_ACCESS", "true")
    monkeypatch.delenv("BETA_FULL_ACCESS_UNTIL", raising=False)


@pytest.fixture
def grant_off(monkeypatch):
    monkeypatch.delenv("BETA_FULL_ACCESS", raising=False)
    monkeypatch.delenv("BETA_FULL_ACCESS_UNTIL", raising=False)


# ── 1. The rule ──────────────────────────────────────────────────────────────


def test_a_stamped_free_account_is_answered_as_pro_through_the_beta(grant_on):
    assert resolve_access(_account("free", STAMP), NOW) == Access(tier="pro", source="beta", paid_tier="free")
    assert effective_tier(_account("free", STAMP), NOW) == "pro"


def test_a_stamp_with_an_empty_cohort_counts(grant_on):
    bare = {"cohort": "", "invite_code": "letmein", "joined_at": NOW}
    assert resolve_access(_account("free", bare), NOW).source == "beta"


def test_a_personal_invitation_stamp_counts(grant_on):
    personal = {"cohort": "c1-direct", "invite_id": "abc", "joined_at": NOW}
    assert resolve_access(_account("free", personal), NOW).tier == "pro"


@pytest.mark.parametrize("stamp", [None, {}, "friends", True, []])
def test_an_account_without_a_stamp_keeps_its_stored_plan(grant_on, stamp):
    assert not has_beta_stamp({"beta": stamp})
    assert resolve_access(_account("free", stamp), NOW) == Access(tier="free", source="free", paid_tier="free")


def test_the_older_is_beta_flag_grants_nothing(grant_on):
    account = {**_account("free"), "is_beta": True}
    assert resolve_access(account, NOW).tier == "free"


def test_a_paying_pro_account_is_paid_not_beta(grant_on):
    assert resolve_access(_account("pro", STAMP), NOW) == Access(tier="pro", source="paid", paid_tier="pro")


def test_a_paying_plus_tester_gets_pro_and_keeps_its_paid_plan_named(grant_on):
    assert resolve_access(_account("plus", STAMP), NOW) == Access(tier="pro", source="beta", paid_tier="plus")


def test_with_the_grant_off_every_tester_is_answered_by_the_stored_plan(grant_off):
    assert resolve_access(_account("free", STAMP), NOW) == Access(tier="free", source="free", paid_tier="free")
    assert resolve_access(_account("plus", STAMP), NOW) == Access(tier="plus", source="paid", paid_tier="plus")
    assert resolve_access(_account("pro", STAMP), NOW).source == "paid"


def test_an_unknown_stored_plan_reads_as_free(grant_off):
    assert resolve_access(_account("enterprise"), NOW).tier == "free"
    assert resolve_access(None, NOW).tier == "free"
    assert resolve_access({}, NOW).tier == "free"


def test_the_last_day_is_inclusive_in_utc(monkeypatch):
    monkeypatch.setenv("BETA_FULL_ACCESS", "true")
    monkeypatch.setenv("BETA_FULL_ACCESS_UNTIL", "2026-11-30")
    assert beta.full_access_open(datetime(2026, 11, 30, 23, 59, tzinfo=timezone.utc))
    assert not beta.full_access_open(datetime(2026, 12, 1, 0, 0, tzinfo=timezone.utc))
    assert resolve_access(_account("free", STAMP), datetime(2026, 12, 1, tzinfo=timezone.utc)).tier == "free"


def test_a_last_day_that_cannot_be_read_closes_the_grant(monkeypatch):
    monkeypatch.setenv("BETA_FULL_ACCESS", "true")
    monkeypatch.setenv("BETA_FULL_ACCESS_UNTIL", "end of January")
    assert not beta.full_access_open(NOW)
    assert "UNREADABLE" in beta.describe_full_access()


def test_the_start_up_line_says_what_is_set(monkeypatch, grant_off):
    assert beta.describe_full_access().endswith("off")
    monkeypatch.setenv("BETA_FULL_ACCESS", "true")
    assert beta.describe_full_access().endswith("on, last day none")
    monkeypatch.setenv("BETA_FULL_ACCESS_UNTIL", "2027-01-31")
    assert beta.describe_full_access().endswith("last day 2027-01-31")


# ── 2. The caps ──────────────────────────────────────────────────────────────


def test_a_testers_month():
    tester = Access(tier="pro", source="beta", paid_tier="free")
    assert BETA_LIMITS == {
        "ai_calls_per_month": 200,
        "tts_chars_per_month": 150_000,
        "agent_messages_per_month": 500,
        "images_per_month": 2000,
    }
    for key, cap in BETA_LIMITS.items():
        assert access_limit(tester, key) == cap


def test_everything_not_capped_is_pros():
    tester = Access(tier="pro", source="beta", paid_tier="free")
    for key in ("import_cards", "books", "flashcards", "document_words"):
        assert access_limit(tester, key) == plan_limit("pro", key)
    assert access_limits(tester)["ai_calls_per_month"] == 200
    assert access_limits(tester)["import_cards"] == -1


def test_a_paying_plus_tester_keeps_the_larger_of_the_two():
    plus_tester = Access(tier="pro", source="beta", paid_tier="plus")
    assert access_limit(plus_tester, "ai_calls_per_month") == 1000
    assert access_limit(plus_tester, "tts_chars_per_month") == 1_000_000
    assert access_limit(plus_tester, "agent_messages_per_month") == -1
    assert access_limit(plus_tester, "images_per_month") == 2000


def test_a_paying_pro_account_has_pros_own_limits():
    pro = Access(tier="pro", source="paid", paid_tier="pro")
    assert access_limit(pro, "ai_calls_per_month") == 1000
    assert access_limit(pro, "images_per_month") == -1


# ── 3. The AI ceiling, and the fallback ──────────────────────────────────────


def _users(account: dict, updated=None):
    users = MagicMock()
    users.find_one = AsyncMock(return_value=account)
    users.update_one = AsyncMock()
    users.find_one_and_update = AsyncMock(return_value=updated)
    return users


def _tester(count: int) -> dict:
    return {
        "_id": ObjectId(USER),
        "subscription": {"tier": "free", "ai_usage_count": count, "ai_usage_reset_date": OPEN_UNTIL},
        "beta": STAMP,
    }


@pytest.mark.asyncio
async def test_a_tester_is_counted_against_200(grant_on):
    from app.auth import dependencies

    users = _users(_tester(60), updated=_tester(61))
    with patch.object(dependencies, "users_collection", users):
        user = await dependencies.track_ai_usage({"user_id": USER})
    assert users.find_one.call_args[0][1] == ACCESS_PROJECTION
    assert users.find_one_and_update.call_args[0][0]["$or"][0] == {"subscription.ai_usage_count": {"$lt": 200}}
    # The returned document still carries the stamp, so the route resolves Pro from it.
    assert effective_tier(user) == "pro"


@pytest.mark.asyncio
async def test_a_tester_past_200_is_refused_with_the_cap_in_the_answer(grant_on):
    from app.auth import dependencies

    users = _users(_tester(200), updated=None)
    with patch.object(dependencies, "users_collection", users):
        with pytest.raises(HTTPException) as raised:
            await dependencies.track_ai_usage({"user_id": USER})
    assert raised.value.status_code == 429 and raised.value.detail["limit"] == 200


@pytest.mark.asyncio
async def test_with_the_grant_off_the_same_account_is_counted_against_50(grant_off):
    from app.auth import dependencies

    users = _users(_tester(60), updated=None)
    with patch.object(dependencies, "users_collection", users):
        with pytest.raises(HTTPException) as raised:
            await dependencies.track_ai_usage({"user_id": USER})
    assert raised.value.detail["limit"] == 50


@pytest.mark.asyncio
async def test_get_access_and_the_tier_dependency_answer_the_plan_in_effect(grant_on):
    from app.auth import dependencies

    users = _users(_account("free", STAMP))
    with patch.object(dependencies, "users_collection", users):
        assert await dependencies.get_subscription_tier({"user_id": USER}) == "pro"
        assert (await dependencies.get_access({"user_id": USER})).source == "beta"


@pytest.mark.asyncio
async def test_an_answer_from_the_fallback_counts_once(grant_on, monkeypatch):
    """The counter is taken by the dependency, before the model; the fallback never touches it."""
    from app.auth import dependencies

    llm_health.reset()
    monkeypatch.setenv("GEMINI_API_KEY", "m")
    monkeypatch.setenv("GROQ_API_KEY", "g")
    fake_groq, fake_flash, fake_pro = MagicMock(), MagicMock(), MagicMock()
    with patch("app.core.model_config.Groq_client", return_value=fake_groq), \
            patch("app.core.model_config.Gemini_client", side_effect=lambda m: fake_flash if "flash" in m else fake_pro):
        import app.core.model_config as mc
        importlib.reload(mc)
    fake_pro.request.side_effect = GeminiQuotaError("quota")
    fake_groq.request.return_value = "groq completion"

    users = _users(_tester(10), updated=_tester(11))
    try:
        with patch.object(dependencies, "users_collection", users):
            user = await dependencies.track_ai_usage({"user_id": USER})
            # The route asks for the client of the plan in effect: Pro's, with Groq behind it.
            answer = mc.get_client_for_tier(effective_tier(user)).request("make cards")
    finally:
        llm_health.reset()

    assert answer == "groq completion"
    fake_pro.request.assert_called_once()
    fake_groq.request.assert_called_once()
    assert users.find_one_and_update.await_count == 1
    assert users.find_one_and_update.call_args[0][1]["$inc"] == {"subscription.ai_usage_count": 1}


# ── 4. No router decides a plan by itself ────────────────────────────────────

#: A read of the stored plan: `.get("tier"`, `["tier"]`, or a projection of `subscription.tier`.
_DIRECT_READ = re.compile(r"""\.get\(\s*["']tier["']|\[\s*["']tier["']\s*\]|["']subscription\.tier["']\s*:\s*1""")

#: The Stripe webhook writes the stored plan; it never reads it to decide access.
_WRITERS = {"stripe_webhooks.py"}


def test_no_router_or_dependency_reads_the_stored_plan_itself():
    offenders = []
    for path in [*sorted((APP / "routers").glob("*.py")), APP / "auth" / "dependencies.py"]:
        if path.name in _WRITERS:
            continue
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            if _DIRECT_READ.search(line):
                offenders.append(f"{path.name}:{number}: {line.strip()}")
    assert offenders == [], "ask app.config.entitlements instead:\n" + "\n".join(offenders)


# ── 5. What the account endpoints say ────────────────────────────────────────


def test_the_fields_an_account_endpoint_adds(grant_on):
    assert access_fields(resolve_access(_account("free", STAMP), NOW)) == {"tier": "pro", "access": "beta", "paid_tier": "free"}
    assert access_fields(resolve_access(_account("free"), NOW)) == {"tier": "free", "access": "free", "paid_tier": "free"}
    assert access_fields(resolve_access(_account("plus"), NOW)) == {"tier": "plus", "access": "paid", "paid_tier": "plus"}


@pytest.mark.asyncio
async def test_subscription_status_names_the_plan_in_effect_and_the_paid_one(grant_on):
    from app.routers import subscriptions

    users = _users({**_account("free", STAMP), "subscription": {"tier": "free", "status": "active", "ai_usage_count": 7}})
    with patch.object(subscriptions, "users_collection", users):
        status = await subscriptions.get_subscription_status({"user_id": USER})
    assert (status.tier, status.access, status.paid_tier, status.ai_usage_count) == ("pro", "beta", "free", 7)
