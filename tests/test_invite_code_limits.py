"""
INVITE-004 — a cohort code has a ceiling and a last day (ADR-077, FR-020).

Pins:

  1. Today's entries parse as before and carry no limit.
  2. `cohort:code:max:YYYY-MM-DD`, each limit optional, with the two fallbacks.
  3. A limit that cannot be read closes the code; it never opens it.
  4. The last day is inclusive, in UTC.
  5. The ceiling refuses at the count, the count is asked only when there is a
     ceiling and no further than it, and a failed count is a 503, not an entry.
  6. The gate and the public check answer a full or expired code exactly as an
     unknown one; the check names no cohort.
  7. The start-up lines never contain a code.
"""
import sys
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock

from tests._stubs import fake_request, stub_if_missing

stub_if_missing("langfuse", "langfuse.langchain")
sys.modules.setdefault("app.models.agent_models", MagicMock())

import pytest
from fastapi import HTTPException

from app.config.beta import (
    BETA_INVITE_REQUIRED_CODE,
    INVITE_CHECK_UNAVAILABLE_CODE,
    CodeRule,
    describe_code_rules,
    gate_new_account,
    is_past,
    open_code_rule,
    parse_code_rules,
    parse_invite_codes,
)
from app.models.Beta import InviteCheckRequest
from app.routers import beta as beta_router

NOW = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)
ENV = ("BETA_ACTIVE", "BETA_INVITE_REQUIRED", "BETA_UPGRADES_OPEN", "BETA_INVITE_CODES", "BETA_CODE_MAX_USES", "BETA_CODE_EXPIRES")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ENV:
        monkeypatch.delenv(name, raising=False)


def counting(uses: int) -> AsyncMock:
    return AsyncMock(return_value=uses)


# ── 1, 2. Parsing ────────────────────────────────────────────────────────────


def test_entries_without_limits_parse_as_before():
    rules = parse_code_rules("friends:c0-7F3A, students:c1-9b2e ,,letmein")
    assert rules == {
        "c0-7f3a": CodeRule(cohort="friends"),
        "c1-9b2e": CodeRule(cohort="students"),
        "letmein": CodeRule(cohort=""),
    }
    assert parse_invite_codes("friends:c0-7F3A,letmein") == {"c0-7f3a": "friends", "letmein": ""}


@pytest.mark.parametrize(
    "entry, expected",
    [
        ("friends:c0:25:2026-12-15", CodeRule("friends", 25, date(2026, 12, 15))),
        ("friends:c0:25", CodeRule("friends", 25, None)),
        ("friends:c0::2026-12-15", CodeRule("friends", None, date(2026, 12, 15))),
        ("friends:c0: 25 : 2026-12-15 ", CodeRule("friends", 25, date(2026, 12, 15))),
        ("friends:c0:0", CodeRule("friends", 0, None)),
    ],
)
def test_an_entry_may_carry_a_ceiling_and_a_last_day(entry, expected):
    assert parse_code_rules(entry) == {"c0": expected}


def test_the_fallbacks_cover_only_what_an_entry_leaves_out():
    rules = parse_code_rules("a:one,b:two:5,c:three::2026-11-30,d:four:7:2026-11-20", "50", "2026-12-31")
    assert rules["one"] == CodeRule("a", 50, date(2026, 12, 31))
    assert rules["two"] == CodeRule("b", 5, date(2026, 12, 31))
    assert rules["three"] == CodeRule("c", 50, date(2026, 11, 30))
    assert rules["four"] == CodeRule("d", 7, date(2026, 11, 20))


# ── 3. An unreadable limit closes the code ───────────────────────────────────


@pytest.mark.parametrize("entry", ["friends:c0:many", "friends:c0:-1", "friends:c0:5:soon", "friends:c0:5:2026-13-40", "friends:c0:5:2026-12-15:extra", "friends:c0:2.5"])
def test_an_unreadable_limit_makes_the_code_unusable(entry):
    assert parse_code_rules(entry)["c0"].usable is False


def test_an_unreadable_fallback_closes_the_codes_that_rely_on_it_and_no_other():
    rules = parse_code_rules("a:one,b:two:5:2026-12-01", "lots", None)
    assert rules["one"].usable is False
    assert rules["two"] == CodeRule("b", 5, date(2026, 12, 1))


@pytest.mark.asyncio
async def test_an_unusable_code_is_refused_without_counting(monkeypatch):
    monkeypatch.setenv("BETA_INVITE_CODES", "friends:c0:many")
    count = counting(0)
    assert await open_code_rule("c0", count, NOW) is None
    count.assert_not_awaited()


# ── 4. The last day ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "now, past",
    [
        (datetime(2026, 12, 15, 0, 0, tzinfo=timezone.utc), False),
        (datetime(2026, 12, 15, 23, 59, 59, tzinfo=timezone.utc), False),
        (datetime(2026, 12, 16, 0, 0, tzinfo=timezone.utc), True),
        (datetime(2026, 12, 14, 12, 0, tzinfo=timezone.utc), False),
    ],
)
def test_the_last_day_is_inclusive_in_utc(now, past):
    assert is_past(CodeRule("friends", None, date(2026, 12, 15)), now) is past


def test_a_rule_with_no_last_day_is_never_past():
    assert is_past(CodeRule("friends"), datetime(2099, 1, 1, tzinfo=timezone.utc)) is False


# ── 5. The ceiling ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("uses, opens", [(0, True), (24, True), (25, False), (26, False)])
async def test_the_ceiling_refuses_at_the_count(monkeypatch, uses, opens):
    monkeypatch.setenv("BETA_INVITE_CODES", "friends:C0-7f3a:25")
    count = counting(uses)
    rule = await open_code_rule(" c0-7F3A ", count, NOW)
    assert (rule is not None) is opens
    count.assert_awaited_once_with("c0-7f3a", 25)


@pytest.mark.asyncio
async def test_a_code_without_a_ceiling_is_not_counted(monkeypatch):
    monkeypatch.setenv("BETA_INVITE_CODES", "friends:c0::2026-12-15")
    count = counting(10_000)
    assert await open_code_rule("c0", count, NOW) == CodeRule("friends", None, date(2026, 12, 15))
    count.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_ceiling_of_zero_closes_the_code_without_counting(monkeypatch):
    monkeypatch.setenv("BETA_INVITE_CODES", "friends:c0:0")
    count = counting(0)
    assert await open_code_rule("c0", count, NOW) is None
    count.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_count_that_fails_refuses_with_503_and_writes_nothing(monkeypatch):
    monkeypatch.setenv("BETA_INVITE_REQUIRED", "true")
    monkeypatch.setenv("BETA_INVITE_CODES", "friends:c0:25")
    with pytest.raises(HTTPException) as refused:
        await gate_new_account("c0", AsyncMock(side_effect=RuntimeError("database unreachable")), NOW)
    assert refused.value.status_code == 503
    assert refused.value.detail == {"code": INVITE_CHECK_UNAVAILABLE_CODE}


@pytest.mark.asyncio
async def test_the_fallback_ceiling_applies_to_a_plain_entry(monkeypatch):
    monkeypatch.setenv("BETA_INVITE_CODES", "friends:c0")
    monkeypatch.setenv("BETA_CODE_MAX_USES", "3")
    assert await open_code_rule("c0", counting(2), NOW) is not None
    assert await open_code_rule("c0", counting(3), NOW) is None


# ── 6. Full, expired and unknown are one answer ──────────────────────────────


async def _gate_refusal(header, count) -> HTTPException:
    with pytest.raises(HTTPException) as refused:
        await gate_new_account(header, count, NOW)
    return refused.value


@pytest.mark.asyncio
async def test_the_gate_refuses_full_expired_and_unknown_identically(monkeypatch):
    monkeypatch.setenv("BETA_INVITE_REQUIRED", "true")
    monkeypatch.setenv("BETA_INVITE_CODES", "full:c-full:5,old:c-old::2026-11-11,bad:c-bad:x")
    refusals = [
        await _gate_refusal("c-full", counting(5)),
        await _gate_refusal("c-old", counting(0)),
        await _gate_refusal("c-bad", counting(0)),
        await _gate_refusal("never-issued", counting(0)),
        await _gate_refusal(None, counting(0)),
    ]
    assert {(r.status_code, str(r.detail)) for r in refusals} == {(403, str({"code": BETA_INVITE_REQUIRED_CODE}))}


@pytest.mark.asyncio
async def test_the_gate_stamps_a_code_with_room(monkeypatch):
    monkeypatch.setenv("BETA_INVITE_REQUIRED", "true")
    monkeypatch.setenv("BETA_INVITE_CODES", "friends:c0:25:2026-12-15")
    assert await gate_new_account("C0", counting(24), NOW) == {"cohort": "friends", "invite_code": "c0", "joined_at": NOW}


@pytest.mark.asyncio
async def test_the_public_check_gives_one_answer_and_names_no_cohort(monkeypatch):
    monkeypatch.setenv("BETA_INVITE_CODES", "full:c-full:5,old:c-old::2026-01-01,friends:c-open:5")
    monkeypatch.setattr(beta_router, "count_code_uses", AsyncMock(side_effect=lambda code, ceiling: 5 if code == "c-full" else 0))

    async def check(code):
        answer = await beta_router.check_invite(fake_request(), InviteCheckRequest(code=code))
        return answer.model_dump()

    closed = [await check(code) for code in ("c-full", "c-old", "never-issued")]
    assert closed == [{"valid": False, "cohort": None}] * 3
    assert await check("c-open") == {"valid": True, "cohort": None}


# ── 7. The start-up lines ────────────────────────────────────────────────────


def test_the_start_up_lines_name_cohorts_and_limits_and_never_a_code(monkeypatch):
    monkeypatch.setenv("BETA_INVITE_CODES", "friends:s3cret-one:25:2026-12-15,students:s3cret-two:x,s3cret-three")
    lines = describe_code_rules()
    assert lines[0] == "cohort 'friends': max 25, last day 2026-12-15"
    assert "UNREADABLE" in lines[1] and "students" in lines[1]
    assert lines[2].startswith("cohort '(unnamed)': max none, last day none")
    assert "s3cret" not in " ".join(lines)
