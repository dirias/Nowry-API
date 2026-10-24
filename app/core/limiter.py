"""
Shared SlowAPI rate limiter instance.

Defined here so routers can import it directly with:
    from app.core.limiter import limiter

main.py sets ``app.state.limiter = limiter`` so SlowAPIMiddleware picks it up.
"""

import os

from slowapi import Limiter
from slowapi.util import get_remote_address
from starlette.requests import Request


def _trusted_proxy_hops() -> int:
    """How many proxies sit in front of the API. Railway's edge is one."""
    try:
        return max(0, int(os.getenv("TRUSTED_PROXY_HOPS", "1")))
    except ValueError:
        return 1


def client_address(request: Request) -> str:
    """The caller's address, as the nearest trusted proxy saw it (QA-004).

    Behind Railway the socket peer is the proxy, so ``get_remote_address`` put
    every user in one bucket. Each proxy appends the address it received from
    to ``X-Forwarded-For``, so the entry ``hops`` from the right is the one our
    own edge wrote; anything left of it came from the client and can be forged.
    With no header (local development) the socket peer is the caller.
    """
    hops = _trusted_proxy_hops()
    forwarded = request.headers.get("x-forwarded-for", "")
    entries = [entry.strip() for entry in forwarded.split(",") if entry.strip()]
    if hops and len(entries) >= hops:
        return entries[-hops]
    return get_remote_address(request)


limiter = Limiter(key_func=client_address, default_limits=["120/minute"])
