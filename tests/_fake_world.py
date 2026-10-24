"""The real app over an in-memory MongoDB, for tests that need real query semantics.

A ``MagicMock`` collection returns whatever the test told it to, whatever the
filter, so it cannot prove an ownership filter or a lookup. ``fake_world()``
swaps every Motor collection, database and client the app imported for
mongomock-motor, records every document a write's filter matches (a write the
fake turns into a no-op still counts), and lets a test act as any user it
creates (ADR-049 point 3, amended for QA-007).
"""
import asyncio
import contextlib
import sys
from dataclasses import dataclass, field

from bson import ObjectId

WRITES = ("update_one", "update_many", "replace_one", "delete_one", "delete_many",
          "find_one_and_update", "find_one_and_replace", "find_one_and_delete")


class _Recording:
    """A collection that notes every document a write's filter matches."""

    def __init__(self, inner, touched):
        self._inner, self._touched = inner, touched

    def __getattr__(self, name):
        attr = getattr(self._inner, name)
        if name not in WRITES:
            return attr

        async def write(filter, *args, **kwargs):
            for doc in await self._inner.find(filter, {"_id": 1}).to_list(length=None):
                self._touched.append((self._inner.name, doc["_id"]))
            return await attr(filter, *args, **kwargs)

        return write

    def __getitem__(self, name):
        return self._inner[name]


class _RecordingDb:
    def __init__(self, inner, touched):
        self._inner, self._touched = inner, touched

    def __getitem__(self, name):
        return _Recording(self._inner[name], self._touched)

    def get_collection(self, name, *args, **kwargs):
        return self[name]

    def __getattr__(self, name):
        attr = getattr(self._inner, name)
        return _Recording(attr, self._touched) if type(attr).__name__.endswith("Collection") else attr


@dataclass
class World:
    app: object
    http: object
    raw: object
    loop: asyncio.AbstractEventLoop
    touched: list = field(default_factory=list)
    users: dict = field(default_factory=dict)
    current: dict = field(default_factory=dict)

    def run(self, coroutine):
        return self.loop.run_until_complete(coroutine)

    def new_user(self, label: str, **fields) -> ObjectId:
        uid = ObjectId()
        doc = {"_id": uid, "firebase_uid": f"fb-{label}-{uid}", "email": f"{label}@example.test",
               "username": f"{label}{str(uid)[-6:]}", "subscription": {"tier": "pro", "status": "active"}}
        doc.update(fields)
        self.run(self.raw["users"].insert_one(doc))
        self.users[uid] = {"user_id": str(uid), "firebase_uid": doc["firebase_uid"],
                           "email": doc["email"], "email_verified": True}
        return uid

    def act_as(self, uid: ObjectId) -> None:
        self.current["user"] = self.users[uid]


@contextlib.contextmanager
def fake_world():
    import mongomock_motor
    from fastapi.testclient import TestClient
    from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorCollection, AsyncIOMotorDatabase

    from app.main import app
    import app.auth.firebase_auth as firebase_auth

    client = mongomock_motor.AsyncMongoMockClient()
    raw = client["nowry-test"]
    touched: list = []
    db = _RecordingDb(raw, touched)

    swapped = []
    for name, module in list(sys.modules.items()):
        if module is None or not (name == "app" or name.startswith("app.")):
            continue
        for attr, value in list(vars(module).items()):
            if isinstance(value, AsyncIOMotorCollection):
                replacement = db[value.name]
            elif isinstance(value, AsyncIOMotorDatabase):
                replacement = db
            elif isinstance(value, AsyncIOMotorClient):
                replacement = client
            else:
                continue
            swapped.append((module, attr, value))
            setattr(module, attr, replacement)
    assert len(swapped) > 50, "the app's Motor objects were not found; nothing would be tested"

    world = World(app=app, http=None, raw=raw, loop=asyncio.new_event_loop(), touched=touched)

    async def as_current():
        return world.current["user"]

    previous_overrides = dict(app.dependency_overrides)
    app.dependency_overrides[firebase_auth.get_firebase_user] = as_current
    app.dependency_overrides[firebase_auth.get_current_user] = as_current
    limiter = app.state.limiter
    was_enabled = getattr(limiter, "enabled", True)
    limiter.enabled = False
    world.http = TestClient(app, raise_server_exceptions=False)
    try:
        yield world
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous_overrides)
        limiter.enabled = was_enabled
        for module, attr, value in swapped:
            setattr(module, attr, value)
        world.loop.close()
