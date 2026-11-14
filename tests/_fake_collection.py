"""A small in-memory stand-in for a Motor collection, for the invitation tests.

It understands the filters those tests use (equality, `$or`, `$gt`, `$lte`,
`$lt`, `$ne`, `$in`, `$exists`, `$regex`) and applies `$set`, `$unset` and, on an upsert,
`$setOnInsert`, so an atomic claim is exercised as a query, not as a mock that
returns whatever the test told it to.
"""
from __future__ import annotations

import copy
from types import SimpleNamespace
import re
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
            if "$lt" in wanted and not (value is not None and value < wanted["$lt"]):
                return False
            if "$ne" in wanted and value == wanted["$ne"]:
                return False
            if "$in" in wanted and value not in wanted["$in"]:
                return False
            if "$exists" in wanted and (key in row) != bool(wanted["$exists"]):
                return False
            if "$regex" in wanted:
                flags = re.IGNORECASE if "i" in wanted.get("$options", "") else 0
                if not (isinstance(value, str) and re.search(wanted["$regex"], value, flags)):
                    return False
        elif row.get(key) != wanted:
            return False
    return True


def _apply(row: Dict[str, Any], update: Dict[str, Any]) -> None:
    row.update(update.get("$set", {}))
    for key in update.get("$unset", {}):
        row.pop(key, None)


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

    def find(self, query: Dict[str, Any], projection: Optional[Dict[str, Any]] = None) -> _Cursor:
        return _Cursor([row for row in self.rows if _matches(row, query)])

    async def update_many(self, query: Dict[str, Any], update: Dict[str, Any]) -> SimpleNamespace:
        hits = [row for row in self.rows if _matches(row, query)]
        for row in hits:
            row.update(update["$set"])
        return SimpleNamespace(modified_count=len(hits))

    async def update_one(self, query: Dict[str, Any], update: Dict[str, Any], upsert: bool = False) -> SimpleNamespace:
        row = next((row for row in self.rows if _matches(row, query)), None)
        if row is None and upsert:
            fresh = {"_id": ObjectId(), **{k: v for k, v in query.items() if not k.startswith("$")}, **update.get("$setOnInsert", {})}
            _apply(fresh, update)
            self.rows.append(fresh)
            return SimpleNamespace(modified_count=0)
        if row is not None:
            _apply(row, update)
        return SimpleNamespace(modified_count=int(row is not None))

    async def find_one_and_update(
        self, query: Dict[str, Any], update: Dict[str, Any], return_document: Any = None, upsert: bool = False, **_: Any
    ) -> Optional[Dict[str, Any]]:
        """The row after the write; as it was before only when `return_document` is pymongo's BEFORE (False)."""
        row = next((row for row in self.rows if _matches(row, query)), None)
        if row is None:
            if upsert:
                fresh = {"_id": ObjectId(), **{k: v for k, v in query.items() if not k.startswith("$")}, **update.get("$setOnInsert", {})}
                _apply(fresh, update)
                self.rows.append(fresh)
            return None
        before = copy.deepcopy(row)
        _apply(row, update)
        return before if return_document is False else copy.deepcopy(row)
