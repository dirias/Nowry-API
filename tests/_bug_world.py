"""
A fake world for the bug-report routes (BUG-001, ADMIN-008a, ADMIN-007b).

One mongomock database behind `app.routers.bugs`, the admin check and the
admin log; a caller whose account can be switched; the operator notices kept
in a list instead of sent. Nothing reaches a real service.
"""
import asyncio
import base64
from datetime import datetime, timezone
from typing import Any, Dict, List

from bson import ObjectId
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mongomock_motor import AsyncMongoMockClient

from app.auth import dependencies
from app.auth.firebase_auth import get_firebase_user
from app.routers import bugs as bugs_router
from app.services import admin_actions

ADMIN = ObjectId()
TESTER = ObjectId()

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 64


def data_url(raw: bytes, kind: str = "png") -> str:
    return f"data:image/{kind};base64," + base64.b64encode(raw).decode()


def report(**fields: Any) -> Dict[str, Any]:
    """A web report body as the form sends it today."""
    return {
        "title": "The calendar lost a milestone",
        "description": "Gone after reload.",
        "steps_to_reproduce": "1. Add a milestone 2. Reload",
        "severity": "high",
        "category": "functionality",
        "url": "/calendar",
        "browser_info": {"name": "Chrome", "version": "130", "os": "macOS", "screen_resolution": "1440x900"},
        "screenshots": [],
        **fields,
    }


class World:
    def __init__(self, monkeypatch) -> None:
        self.database = AsyncMongoMockClient()["nowry-test"]
        self.loop = asyncio.new_event_loop()  # mongomock is loop-agnostic
        self.notices: List[tuple] = []
        monkeypatch.setenv("BETA_ACTIVE", "true")
        monkeypatch.setattr(dependencies, "users_collection", self.database["users"])
        monkeypatch.setattr(bugs_router, "bugs_collection", self.database["bugs"])
        monkeypatch.setattr(bugs_router, "users_collection", self.database["users"])
        monkeypatch.setattr(admin_actions, "admin_actions_collection", self.database["admin_actions"])

        async def fake_notify(subject: str, text: str, reply_to=None) -> bool:
            self.notices.append((subject, text))
            return True

        monkeypatch.setattr(bugs_router, "notify", fake_notify)
        api = FastAPI()
        api.include_router(bugs_router.router)
        self.caller: Dict[str, Any] = {"user_id": str(TESTER)}
        api.dependency_overrides[get_firebase_user] = lambda: {
            "auth_time": int(datetime.now(timezone.utc).timestamp()), **self.caller,
        }
        self.client = TestClient(api)
        self.run(self.database["users"].insert_many([
            {"_id": ADMIN, "email": "op@nowry.app", "username": "operator", "is_admin": True, "deleted_at": None},
            {"_id": TESTER, "email": "t@example.com", "username": "tester_one", "deleted_at": None},
        ]))

    def run(self, awaitable):
        return self.loop.run_until_complete(awaitable)

    def act_as(self, who: ObjectId) -> None:
        self.caller["user_id"] = str(who)

    def bugs(self) -> List[dict]:
        return self.run(self.database["bugs"].find({}).to_list(length=1000))

    def actions(self) -> List[dict]:
        return self.run(self.database["admin_actions"].find({}).to_list(length=1000))
