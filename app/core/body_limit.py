"""
A request-body cap for one router's routes (ADMIN-008a, FR-008).

FastAPI reads and parses the body before any dependency runs, so the cap sits
in the route class: a declared `Content-Length` over the cap is refused before
reading anything, and a body without one is read in chunks and refused as soon
as it passes the cap. The read body is kept on the request, so parsing reads it
from memory. The refusal is `413 {"detail": {"code": "too_large", "field": "body"}}`.
"""
from __future__ import annotations

from typing import Any, Callable, Coroutine, Type

from fastapi.routing import APIRoute
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

Handler = Callable[[Request], Coroutine[Any, Any, Response]]
TOO_LARGE_BODY = {"detail": {"code": "too_large", "field": "body"}}


class BodyTooLarge(Exception):
    """The body passed the cap."""


def declared_over(request: Request, max_bytes: int) -> bool:
    declared = request.headers.get("content-length", "")
    return declared.isdigit() and int(declared) > max_bytes


async def read_capped(request: Request, max_bytes: int) -> None:
    """Read the body into the request, raising BodyTooLarge once it passes `max_bytes`."""
    chunks = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > max_bytes:
            raise BodyTooLarge()
        chunks.append(chunk)
    request._body = b"".join(chunks)  # what Request.body() returns from now on


def body_capped_route(max_bytes: int) -> Type[APIRoute]:
    """An APIRoute class whose routes refuse bodies over `max_bytes`."""

    class BodyCappedRoute(APIRoute):
        def get_route_handler(self) -> Handler:
            handler = super().get_route_handler()

            async def capped(request: Request) -> Response:
                if declared_over(request, max_bytes):
                    return JSONResponse(TOO_LARGE_BODY, status_code=413)
                try:
                    await read_capped(request, max_bytes)
                except BodyTooLarge:
                    return JSONResponse(TOO_LARGE_BODY, status_code=413)
                return await handler(request)

            return capped

    return BodyCappedRoute
