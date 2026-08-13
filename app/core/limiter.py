"""
Shared SlowAPI rate limiter instance.

Defined here so routers can import it directly with:
    from app.core.limiter import limiter

main.py sets ``app.state.limiter = limiter`` so SlowAPIMiddleware picks it up.
"""

from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address, default_limits=["120/minute"])
