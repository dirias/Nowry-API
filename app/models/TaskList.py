"""The task list — a named, owned home for tasks (PLAN-021).

A task's `category` holds the id of its list. Lists used to live only in the
web browser's localStorage under generated ids like "list_1788662138438"; the
import route carries those ids over as `legacy_id` and remaps the tasks.
"""
from __future__ import annotations

from datetime import datetime
from typing import List

from pydantic import BaseModel, ConfigDict, Field, field_validator

NAME_MAX = 60
LEGACY_ID_MAX = 80
IMPORT_MAX = 50


def clean_name(value: str) -> str:
    """Trim a list name and hold it to 1..NAME_MAX characters."""
    stripped = value.strip()
    if not stripped:
        raise ValueError("name must not be blank")
    if len(stripped) > NAME_MAX:
        raise ValueError(f"name must be at most {NAME_MAX} characters")
    return stripped


class TaskListCreate(BaseModel):
    """The body of POST /task-lists/."""

    model_config = ConfigDict(extra="forbid")

    name: str

    _name = field_validator("name")(clean_name)


class TaskListRename(BaseModel):
    """The body of PATCH /task-lists/{id}."""

    model_config = ConfigDict(extra="forbid")

    name: str

    _name = field_validator("name")(clean_name)


class TaskListImportItem(BaseModel):
    """One browser-held list. A blank name is accepted here and skipped by the
    route, so one empty legacy list never fails the whole import; a long one is
    cut to NAME_MAX rather than refused, for the same reason."""

    legacy_id: str = Field(min_length=1, max_length=LEGACY_ID_MAX)
    name: str

    @field_validator("name")
    @classmethod
    def _trim_name(cls, value: str) -> str:
        return value.strip()[:NAME_MAX].strip()


class TaskListImport(BaseModel):
    """The body of POST /task-lists/import."""

    lists: List[TaskListImportItem] = Field(max_length=IMPORT_MAX)


class TaskListOut(BaseModel):
    """A list as the client sees it."""

    id: str
    name: str
    created_at: datetime
    updated_at: datetime
