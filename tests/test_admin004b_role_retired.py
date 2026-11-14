"""
ADMIN-004b — `role` retired, `/users/me` says `is_admin` (ADR-086 points 1–2, docs/prd-admin.md FR-009).

Pins:

  1. `role` is gone from the user model, the `/auth` answer model, new account documents
     (both sign-up paths) and `/users/me`.
  2. `/users/me` carries `is_admin: true` for an admin and omits the key for everyone else,
     while its other null fields stay on the wire.
  3. `scripts/retire_role.py`: counts per role value, stops on any live role but "user",
     writes only with `--apply`.
  4. The guard: (a) no code under app/ reads `role` to decide anything — the only `role`
     reads left are chat-message dicts for the language models; (b) every route whose
     path holds `/admin`, and the three bug-triage routes, have `require_admin` in their
     dependency tree.
"""
import ast
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from bson import ObjectId
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mongomock_motor import AsyncMongoMockClient

APP = Path(__file__).resolve().parent.parent / "app"


# ── 1. Gone from the model, the answers and new documents ────────────────────


def test_role_is_not_a_field_of_the_user_or_the_auth_answer():
    from app.models.common import UserAuthResponse
    from app.models.User import User
    from app.routers.users import UserMeResponse

    for model in (User, UserAuthResponse, UserMeResponse):
        assert "role" not in model.model_fields, model.__name__


@pytest.mark.parametrize("path", ["routers/auth.py", "auth/firebase_auth.py"])
def test_no_new_account_document_is_written_with_a_role(path):
    tree = ast.parse((APP / path).read_text(encoding="utf-8"))
    keys = [key.value for node in ast.walk(tree) if isinstance(node, ast.Dict) for key in node.keys if isinstance(key, ast.Constant)]
    assert "firebase_uid" in keys  # the account document is a literal here
    assert "role" not in keys


# ── 2. /users/me ─────────────────────────────────────────────────────────────


def me_client(monkeypatch, account: dict) -> TestClient:
    from app.auth.firebase_auth import get_firebase_user
    from app.routers import users as users_router
    from app.services import account_deletion

    collection = AsyncMongoMockClient()["nowry-test"]["users"]
    asyncio.new_event_loop().run_until_complete(collection.insert_one(account))
    monkeypatch.setattr(users_router, "users_collection", collection)
    monkeypatch.setattr(account_deletion, "previous_account", AsyncMock(return_value=None))
    api = FastAPI()
    api.include_router(users_router.router)
    api.dependency_overrides[get_firebase_user] = lambda: {"firebase_uid": "uid-1", "user_id": str(account["_id"])}
    return TestClient(api)


def account(**fields) -> dict:
    return {"_id": ObjectId(), "firebase_uid": "uid-1", "username": "ana", "email": "ana@example.com", "deleted_at": None, **fields}


def test_an_admin_reads_is_admin_true_and_no_role(monkeypatch):
    body = me_client(monkeypatch, account(is_admin=True, role="dev")).get("/users/me").json()
    assert body["is_admin"] is True
    assert "role" not in body


@pytest.mark.parametrize("flag", [{}, {"is_admin": False}, {"is_admin": "true"}, {"is_admin": 1}])
def test_anyone_else_reads_no_is_admin_key_and_keeps_the_other_nulls(monkeypatch, flag):
    body = me_client(monkeypatch, account(**flag)).get("/users/me").json()
    assert "is_admin" not in body and "role" not in body
    for kept in ("avatar_url", "photo_url", "previous_account", "created_at"):
        assert kept in body and body[kept] is None


# ── 3. retire_role.py ────────────────────────────────────────────────────────


def run_retire(capsys, rows, *argv):
    from scripts import retire_role

    users = AsyncMongoMockClient()["nowry-test"]["users"]
    loop = asyncio.new_event_loop()
    if rows:
        loop.run_until_complete(users.insert_many(rows))
    code = loop.run_until_complete(retire_role.run(retire_role.parse_args(list(argv)), users=users))
    left = loop.run_until_complete(users.count_documents({"role": {"$exists": True}}))
    return code, capsys.readouterr().out, left


def test_a_dry_run_counts_each_role_and_writes_nothing(capsys):
    rows = [account(role="user"), account(role="user"), account(), account(role="dev", deleted_at="2026-01-01")]
    code, out, left = run_retire(capsys, rows)
    assert code == 0
    assert "live accounts by role: (none): 1, user: 2" in out
    assert "all accounts by role: (none): 1, dev: 1, user: 2" in out
    assert "dry run, nothing written" in out and left == 3


def test_apply_removes_role_from_every_account(capsys):
    rows = [account(role="user"), account(role="dev", deleted_at="2026-01-01")]
    code, out, left = run_retire(capsys, rows, "--apply")
    assert code == 0 and "role removed from 2 account(s) (written)" in out and left == 0


@pytest.mark.parametrize("role", ["dev", "beta", "admin"])
def test_a_live_account_with_another_role_stops_it(capsys, role):
    code, out, left = run_retire(capsys, [account(role="user"), account(role=role)], "--apply")
    assert code == 1 and "stopped: 1 live account(s)" in out and left == 2


def test_it_prints_no_address():
    from scripts import retire_role

    assert "email" not in Path(retire_role.__file__).read_text(encoding="utf-8").split('"""', 2)[2]


# ── 4. The guard ─────────────────────────────────────────────────────────────

#: The only `role` reads allowed under app/: a chat message's role for the language models.
CHAT_ROLE_READS = {
    ("routers/agent.py", "entry['role']"),
    ("utils/agent_llm.py", "msg['role']"),
}


def _role_reads(tree: ast.AST):
    """Every `x["role"]` read and `x.get("role", …)` call, as source text."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load):
            if isinstance(node.slice, ast.Constant) and node.slice.value == "role":
                yield ast.unparse(node)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get":
            if node.args and isinstance(node.args[0], ast.Constant) and node.args[0].value == "role":
                yield ast.unparse(node)


def test_no_code_under_app_decides_anything_from_role():
    found = set()
    for path in APP.rglob("*.py"):
        if not path.is_file():
            continue
        relative = path.relative_to(APP).as_posix()
        for read in _role_reads(ast.parse(path.read_text(encoding="utf-8"))):
            found.add((relative, read))
    assert found - CHAT_ROLE_READS == set()


def _calls(dependant) -> set:
    calls = {dependant.call}
    for sub in dependant.dependencies:
        calls |= _calls(sub)
    return calls


BUG_TRIAGE = {("GET", "/bugs/all"), ("GET", "/bugs/stats"), ("PATCH", "/bugs/{bug_id}/status")}


def _app_routes(app):
    """(methods, path, dependant) for every API route, through included routers on any FastAPI version."""
    from fastapi.routing import APIRoute

    for route in app.routes:
        if isinstance(route, APIRoute):
            yield route.methods, route.path, route.dependant
        elif hasattr(route, "effective_route_contexts"):
            for context in route.effective_route_contexts():
                yield context.methods, context.path, context.dependant


def test_every_admin_route_and_the_bug_triage_routes_need_require_admin():
    from app.auth.dependencies import require_admin
    from app.main import app

    checked = set()
    for methods, path, dependant in _app_routes(app):
        keys = {(method, path) for method in methods}
        if "/admin" in path or keys & BUG_TRIAGE:
            assert require_admin in _calls(dependant), f"{sorted(methods)} {path}"
            checked |= keys
    assert BUG_TRIAGE <= checked
    assert any("/beta/admin/" in path for _, path in checked) and any("/moderation/admin/" in path for _, path in checked)
