"""A small in-memory stand-in for a Motor collection, for the invitation tests.

It understands the filters those tests use (equality, `$or`, `$gt`, `$lte`) and
applies `$set`, so the atomic claim is exercised as a query, not as a mock that
returns whatever the test told it to.
"""
from __future__ import annotations

import copy
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from bson import ObjectId


def _matches(row: Dict[str, Any], query: Dict[str, Any]) -> bool:
    for key, wanted in query.items():
        if key == "$or":
            if not any(_matches(row, branch) for branch in wanted):
                return False
        elif isinstance(wanted, dict) and any(name.startswith("$") for name in wanted):
            value = row.get(key)
            if "$gt" in wanted and not (value is not None and value > wanted["$gt"]):
                return False
            if "$lte" in wanted and not (value is not None and value <= wanted["$lte"]):
                return False
        elif row.get(key) != wanted:
            return False
    return True


class _Cursor:
    def __init__(self, rows: List[Dict[str, Any]]):
        self.rows = rows

    def sort(self, key: str, direction: int) -> "_Cursor":
        self.rows = sorted(self.rows, key=lambda row: row[key], reverse=direction < 0)
        return self

    def limit(self, count: int) -> "_Cursor":
        self.rows = self.rows[:count]
        return self

    async def to_list(self, length: int) -> List[Dict[str, Any]]:
        return [copy.deepcopy(row) for row in self.rows[:length]]


class FakeCollection:
    def __init__(self) -> None:
        self.rows: List[Dict[str, Any]] = []

    async def insert_one(self, document: Dict[str, Any]) -> SimpleNamespace:
        stored = copy.deepcopy(document)
        stored.setdefault("_id", ObjectId())
        self.rows.append(stored)
        return SimpleNamespace(inserted_id=stored["_id"])

    async def find_one(self, query: Dict[str, Any], projection: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        return next((copy.deepcopy(row) for row in self.rows if _matches(row, query)), None)

    def find(self, query: Dict[str, Any]) -> _Cursor:
        return _Cursor([row for row in self.rows if _matches(row, query)])

    async def update_many(self, query: Dict[str, Any], update: Dict[str, Any]) -> SimpleNamespace:
        hits = [row for row in self.rows if _matches(row, query)]
        for row in hits:
            row.update(update["$set"])
        return SimpleNamespace(modified_count=len(hits))

    async def update_one(self, query: Dict[str, Any], update: Dict[str, Any]) -> SimpleNamespace:
        row = next((row for row in self.rows if _matches(row, query)), None)
        if row is not None:
            row.update(update["$set"])
        return SimpleNamespace(modified_count=int(row is not None))

    async def find_one_and_update(self, query: Dict[str, Any], update: Dict[str, Any], return_document: Any = None, **_: Any) -> Optional[Dict[str, Any]]:
        row = next((row for row in self.rows if _matches(row, query)), None)
        if row is None:
            return None
        row.update(update["$set"])
        return copy.deepcopy(row)
