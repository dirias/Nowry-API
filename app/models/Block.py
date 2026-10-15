"""The Block — the product's one object for time (ADR-045, `docs/prd-blocks.md`).

A block is time set aside: a start and a length on the learner's own clock, a
weekday rule or a single date, what it is for, a reminder, and — in `block_log`
— one row per local date saying whether it was kept.

Times and dates are the learner's local wall clock as strings. The server never
converts them (ADR-045 §3): the weekday of a date string is zone-free, and
"07:30" means 07:30 wherever the learner wakes up.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SOURCE_TYPES = ("routine", "study", "focus", "goal", "task", "free")
LOG_STATUSES = ("kept", "skipped", "partial")
REMINDER_MINUTES = (0, 5, 15, 30)

TIME_PATTERN = r"^([01]\d|2[0-3]):[0-5]\d$"
DATE_PATTERN = r"^\d{4}-\d{2}-\d{2}$"

MIN_LENGTH = 5
MAX_LENGTH = 480
STEP = 5


class BlockSource(BaseModel):
    """What the block is for. `id` names the goal or task when there is one."""

    type: Literal["routine", "study", "focus", "goal", "task", "free"] = "free"
    id: Optional[str] = None


def _check_length(value: int) -> int:
    if value % STEP != 0:
        raise ValueError(f"length_min must be a multiple of {STEP}")
    return value


def _check_days(days: list[int]) -> list[int]:
    if any(d < 0 or d > 6 for d in days):
        raise ValueError("days_of_week entries must be 0 (Monday) to 6 (Sunday)")
    return sorted(set(days))


def _check_reminder(value: Optional[int]) -> Optional[int]:
    if value is not None and value not in REMINDER_MINUTES:
        raise ValueError(f"reminder_min must be one of {REMINDER_MINUTES} or null")
    return value


class BlockCreate(BaseModel):
    """The body of POST /blocks: everything a block is, nothing it is given."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=120)
    source: BlockSource = Field(default_factory=BlockSource)
    start: str = Field(pattern=TIME_PATTERN)
    length_min: int = Field(ge=MIN_LENGTH, le=MAX_LENGTH)
    days_of_week: list[int] = Field(default_factory=list)
    date: Optional[str] = Field(default=None, pattern=DATE_PATTERN)
    reminder_min: Optional[int] = None
    after_block_id: Optional[str] = None
    area_id: Optional[str] = None

    @field_validator("title")
    @classmethod
    def _strip_title(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("title must not be blank")
        return stripped

    _length = field_validator("length_min")(_check_length)
    _days = field_validator("days_of_week")(_check_days)
    _reminder = field_validator("reminder_min")(_check_reminder)

    @model_validator(mode="after")
    def _one_rule(self) -> "BlockCreate":
        if bool(self.days_of_week) == bool(self.date):
            raise ValueError("a block repeats on days_of_week or happens once on a date, never both or neither")
        return self


class BlockUpdate(BaseModel):
    """The body of PATCH /blocks/{id}: only the fields sent are written.

    `extra="forbid"` is what refuses `user_id`, `migrated_from` and anything
    else a client has no business writing.
    """

    model_config = ConfigDict(extra="forbid")

    title: Optional[str] = Field(default=None, min_length=1, max_length=120)
    source: Optional[BlockSource] = None
    start: Optional[str] = Field(default=None, pattern=TIME_PATTERN)
    length_min: Optional[int] = Field(default=None, ge=MIN_LENGTH, le=MAX_LENGTH)
    days_of_week: Optional[list[int]] = None
    date: Optional[str] = Field(default=None, pattern=DATE_PATTERN)
    reminder_min: Optional[int] = None
    after_block_id: Optional[str] = None
    area_id: Optional[str] = None

    @field_validator("length_min")
    @classmethod
    def _length(cls, value: Optional[int]) -> Optional[int]:
        return value if value is None else _check_length(value)

    @field_validator("days_of_week")
    @classmethod
    def _days(cls, value: Optional[list[int]]) -> Optional[list[int]]:
        return value if value is None else _check_days(value)

    _reminder = field_validator("reminder_min")(_check_reminder)


class Block(BlockCreate):
    """A stored block, as the API returns it."""

    model_config = ConfigDict(extra="ignore")

    id: str
    user_id: str
    migrated_from: Optional[dict] = None
    created_at: datetime
    updated_at: datetime


class BlockRecord(BaseModel):
    """What a kept block produced, when it produced something countable."""

    study_session_id: Optional[str] = None
    cards: Optional[int] = Field(default=None, ge=0)


class BlockLogUpsert(BaseModel):
    """The body of PUT /blocks/{id}/log/{date}."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["kept", "skipped", "partial"]
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    minutes: int = Field(default=0, ge=0, le=24 * 60)
    record: Optional[BlockRecord] = None


class BlockLog(BlockLogUpsert):
    model_config = ConfigDict(extra="ignore")

    id: str
    user_id: str
    block_id: str
    date: str
    created_at: datetime
    updated_at: datetime


class BlockOccurrence(BaseModel):
    """One day's instance of a block, with its log row when there is one."""

    date: str
    block_id: str
    start: str
    length_min: int
    # The block's title rides on the row so a client can draw a day without a
    # second lookup; the response model used to drop it and every row read blank.
    title: str = ""
    log: Optional[BlockLog] = None


class MigrationReport(BaseModel):
    blocks: int
    logs: int


class BlocksResponse(BaseModel):
    blocks: list[Block]
    occurrences: list[BlockOccurrence]
    migrated: Optional[MigrationReport] = None


class BlockSummary(BaseModel):
    planned: int
    kept: int
    skipped: int
    partial: int
    minutes: int
    by_source: dict[str, int]
    by_area: dict[str, int]
