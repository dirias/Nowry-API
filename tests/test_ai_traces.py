"""Langfuse traces deleted with the account (ADR-081 amendment, ACCOUNT-007)."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.services import ai_traces


def _span(user_id="u1", trace_id=0xABC):
    return SimpleNamespace(attributes={"user.id": user_id} if user_id else {}, context=SimpleNamespace(trace_id=trace_id))


@pytest.fixture(autouse=True)
def _empty_queue():
    ai_traces._drain()
    yield
    ai_traces._drain()


def test_records_the_account_and_keeps_the_default_decision(monkeypatch):
    monkeypatch.setattr("langfuse._client.span_filter.is_default_export_span", lambda span: True)
    assert ai_traces.should_export_span(_span()) is True
    assert ai_traces._drain() == [("u1", format(0xABC, "032x"))]


def test_records_nothing_for_a_span_langfuse_would_not_send(monkeypatch):
    monkeypatch.setattr("langfuse._client.span_filter.is_default_export_span", lambda span: False)
    assert ai_traces.should_export_span(_span()) is False
    assert ai_traces._drain() == []


def test_records_nothing_without_an_account(monkeypatch):
    monkeypatch.setattr("langfuse._client.span_filter.is_default_export_span", lambda span: True)
    assert ai_traces.should_export_span(_span(user_id=None)) is True
    assert ai_traces._drain() == []


def test_a_broken_span_never_stops_the_export(monkeypatch):
    monkeypatch.setattr("langfuse._client.span_filter.is_default_export_span", lambda span: True)
    assert ai_traces.should_export_span(SimpleNamespace(attributes={"user.id": "u1"}, context=None)) is True


def _collections(monkeypatch, remembered):
    traces = MagicMock()
    traces.find.return_value.to_list = AsyncMock(return_value=[{"trace_id": t} for t in remembered])
    traces.delete_many = AsyncMock()
    traces.bulk_write = AsyncMock()
    deletions = MagicMock()
    deletions.update_one = AsyncMock()
    monkeypatch.setattr("app.config.database.ai_traces_collection", traces, raising=False)
    monkeypatch.setattr("app.config.database.ai_trace_deletions_collection", deletions, raising=False)
    return traces, deletions


def _langfuse(monkeypatch, handler):
    real = httpx.AsyncClient
    monkeypatch.setattr(ai_traces.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")


@pytest.mark.asyncio
async def test_deletes_remembered_and_recently_found_traces_then_forgets_them(monkeypatch):
    traces, deletions = _collections(monkeypatch, remembered=["old1", "old2"])
    calls = []

    def handler(request):
        calls.append(request)
        if request.method == "GET":
            assert request.url.params["userId"] == "u1"
            if "cursor" not in request.url.params:
                return httpx.Response(200, json={"data": [{"traceId": "new1"}, {"traceId": "old1"}], "meta": {"cursor": "c2"}})
            return httpx.Response(200, json={"data": [{"traceId": "new2"}], "meta": {"cursor": None}})
        return httpx.Response(200, json={"message": "ok"})

    _langfuse(monkeypatch, handler)
    assert await ai_traces.delete_user_traces("u1") is True
    deleted = [c for c in calls if c.method == "DELETE"]
    assert len(deleted) == 1
    import json

    assert sorted(json.loads(deleted[0].content)["traceIds"]) == ["new1", "new2", "old1", "old2"]
    traces.delete_many.assert_awaited_once_with({"user_id": "u1"})
    deletions.update_one.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_langfuse_failure_is_queued_and_never_raises(monkeypatch):
    traces, deletions = _collections(monkeypatch, remembered=["t1"])
    _langfuse(monkeypatch, lambda request: httpx.Response(503))
    assert await ai_traces.delete_user_traces("u1") is False
    deletions.update_one.assert_awaited_once()
    traces.delete_many.assert_not_awaited()


@pytest.mark.asyncio
async def test_without_langfuse_keys_nothing_is_called(monkeypatch):
    _collections(monkeypatch, remembered=["t1"])
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "")
    assert await ai_traces.delete_user_traces("u1") is True


@pytest.mark.asyncio
async def test_batches_deletions_by_a_thousand(monkeypatch):
    _collections(monkeypatch, remembered=[f"t{i}" for i in range(2500)])
    sizes = []

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"data": [], "meta": {}})
        import json

        sizes.append(len(json.loads(request.content)["traceIds"]))
        return httpx.Response(200, json={})

    _langfuse(monkeypatch, handler)
    assert await ai_traces.delete_user_traces("u1") is True
    assert sizes == [1000, 1000, 500]


@pytest.mark.asyncio
async def test_a_trace_langfuse_no_longer_has_counts_as_deleted(monkeypatch):
    traces, deletions = _collections(monkeypatch, remembered=["gone"])

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"data": [], "meta": {}})
        return httpx.Response(404, json={"message": "Trace not found"})

    _langfuse(monkeypatch, handler)
    assert await ai_traces.delete_user_traces("u1") is True
    traces.delete_many.assert_awaited_once_with({"user_id": "u1"})
    deletions.update_one.assert_not_awaited()


@pytest.mark.asyncio
async def test_shutdown_writes_every_pair_still_waiting(monkeypatch):
    traces, _ = _collections(monkeypatch, remembered=[])
    for i in range(7000):  # more than one flush takes at a time
        ai_traces._recorded.put(("u1", f"t{i}"))
    await ai_traces.flush_on_shutdown()
    assert ai_traces._recorded.empty()
    written = sum(len(call.args[0]) for call in traces.bulk_write.await_args_list)
    assert written == 7000


@pytest.mark.asyncio
async def test_a_failed_write_puts_the_pairs_back(monkeypatch):
    traces, _ = _collections(monkeypatch, remembered=[])
    traces.bulk_write = AsyncMock(side_effect=RuntimeError("database away"))
    ai_traces._recorded.put(("u1", "t1"))
    with pytest.raises(RuntimeError):
        await ai_traces.flush_recorded()
    assert ai_traces._drain() == [("u1", "t1")]


@pytest.mark.asyncio
async def test_shutdown_never_raises_when_the_database_is_away(monkeypatch):
    traces, _ = _collections(monkeypatch, remembered=[])
    traces.bulk_write = AsyncMock(side_effect=RuntimeError("database away"))
    ai_traces._recorded.put(("u1", "t1"))
    await ai_traces.flush_on_shutdown()
