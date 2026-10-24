"""Readiness for an uptime monitor (QA-004).

``GET /`` answers "ok" while the process is up, even when Mongo is gone and
every real request fails. ``GET /health`` pings the database and answers 503
when it cannot reach it, so a monitor sees the outage the users see.
"""
import asyncio
import logging
from typing import Literal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.config.database import mongo_client
from app.core.limiter import limiter

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])

#: Shorter than any monitor's own timeout, so a hung ping reads as down, not as no answer.
PING_TIMEOUT_SECONDS: float = 2.0


class HealthStatus(BaseModel):
    status: Literal["ok", "degraded"]
    database: Literal["ok", "unreachable"]


async def database_reachable() -> bool:
    try:
        await asyncio.wait_for(mongo_client.admin.command("ping"), timeout=PING_TIMEOUT_SECONDS)
        return True
    except Exception:
        logger.warning("Health check: MongoDB ping failed", exc_info=True)
        return False


@router.get("/health", response_model=HealthStatus, responses={503: {"model": HealthStatus}})
@limiter.exempt
async def health(request: Request) -> HealthStatus | JSONResponse:
    if await database_reachable():
        return HealthStatus(status="ok", database="ok")
    body = HealthStatus(status="degraded", database="unreachable")
    return JSONResponse(status_code=503, content=body.model_dump())
