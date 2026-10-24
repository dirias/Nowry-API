"""QA-004: the API behind Railway's proxy — client address, CORS, readiness."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.core.limiter import client_address


def _request(forwarded: str | None = None, peer: str = "10.0.0.9") -> Request:
    headers = [(b"x-forwarded-for", forwarded.encode())] if forwarded is not None else []
    return Request({"type": "http", "headers": headers, "client": (peer, 1234)})


class TestClientAddress:
    def test_behind_one_proxy_the_entry_our_edge_wrote_is_the_client(self, monkeypatch):
        monkeypatch.delenv("TRUSTED_PROXY_HOPS", raising=False)
        assert client_address(_request("203.0.113.7")) == "203.0.113.7"

    def test_a_forged_entry_on_the_left_is_ignored(self, monkeypatch):
        monkeypatch.delenv("TRUSTED_PROXY_HOPS", raising=False)
        assert client_address(_request("1.1.1.1, 203.0.113.7")) == "203.0.113.7"

    def test_two_hops_reads_one_further_left(self, monkeypatch):
        monkeypatch.setenv("TRUSTED_PROXY_HOPS", "2")
        assert client_address(_request("1.1.1.1, 203.0.113.7, 198.51.100.2")) == "203.0.113.7"

    def test_no_header_falls_back_to_the_socket_peer(self, monkeypatch):
        monkeypatch.delenv("TRUSTED_PROXY_HOPS", raising=False)
        assert client_address(_request()) == "10.0.0.9"
        assert client_address(_request("")) == "10.0.0.9"

    def test_zero_hops_trusts_only_the_socket(self, monkeypatch):
        monkeypatch.setenv("TRUSTED_PROXY_HOPS", "0")
        assert client_address(_request("203.0.113.7")) == "10.0.0.9"

    def test_every_limiter_keys_on_it(self):
        # Several modules stub sys.modules["app.core.limiter"]; read the file.
        import importlib.util
        import os

        source = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app", "core", "limiter.py")
        spec = importlib.util.spec_from_file_location("_real_app_core_limiter", source)
        real = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(real)
        assert real.limiter._key_func is real.client_address

    def test_a_malformed_setting_reads_as_one_hop(self, monkeypatch):
        monkeypatch.setenv("TRUSTED_PROXY_HOPS", "railway")
        assert client_address(_request("1.1.1.1, 203.0.113.7")) == "203.0.113.7"


@pytest.fixture(scope="module")
def client() -> TestClient:
    from app.main import app

    return TestClient(app)


def _preflight(client: TestClient, origin: str):
    return client.options(
        "/health",
        headers={"Origin": origin, "Access-Control-Request-Method": "GET"},
    )


class TestCors:
    def test_nowrys_own_origins_are_allowed(self, client):
        for origin in ("https://nowry.app", "https://dev.nowry.app", "http://localhost:3000"):
            assert _preflight(client, origin).headers.get("access-control-allow-origin") == origin

    def test_another_vercel_project_is_not(self, client):
        response = _preflight(client, "https://attacker.vercel.app")
        assert "access-control-allow-origin" not in response.headers


class TestHealth:
    def test_ok_when_the_database_answers(self, client):
        with patch("app.routers.health.mongo_client") as mongo:
            mongo.admin.command = AsyncMock(return_value={"ok": 1})
            response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok", "database": "ok"}

    def test_503_when_it_does_not(self, client):
        with patch("app.routers.health.mongo_client") as mongo:
            mongo.admin.command = AsyncMock(side_effect=RuntimeError("no primary"))
            response = client.get("/health")
        assert response.status_code == 503
        assert response.json() == {"status": "degraded", "database": "unreachable"}

    def test_a_hung_ping_reads_as_down(self, client):
        import asyncio

        async def hang(*_args, **_kwargs):
            await asyncio.sleep(10)

        with patch("app.routers.health.mongo_client") as mongo, \
             patch("app.routers.health.PING_TIMEOUT_SECONDS", 0.05):
            mongo.admin.command = MagicMock(side_effect=hang)
            response = client.get("/health")
        assert response.status_code == 503

    def test_the_monitor_is_never_rate_limited(self, client):
        with patch("app.routers.health.mongo_client") as mongo:
            mongo.admin.command = AsyncMock(return_value={"ok": 1})
            statuses = {client.get("/health").status_code for _ in range(130)}
        assert statuses == {200}
