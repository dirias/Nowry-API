"""
The two account emails a person asks for (MAIL-006, MAIL-007, ADR-078).

    POST /auth/email/verification    signed in to Firebase; an account document is not needed
    POST /auth/email/password-reset  public; one answer whatever the address is
"""
from typing import Literal, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field

from app.config.firebase_config import verify_firebase_token
from app.core.limiter import client_address
from app.services import account_email, dev_outbox
from app.utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/auth/email", tags=["authentication"])

RESET_ACK = "If this address has an account, a link is on its way."


class VerificationEmailRequest(BaseModel):
    language: Optional[str] = Field(default=None, max_length=12)


class VerificationEmailResponse(BaseModel):
    """`sent`: ours went out. `fallback`: ask Firebase. `verified`: nothing to do. `wait`: try again in `retry_after` seconds."""
    status: Literal["sent", "fallback", "verified", "wait"]
    retry_after: Optional[int] = None


class PasswordResetRequest(BaseModel):
    email: EmailStr
    language: Optional[str] = Field(default=None, max_length=12)


class PasswordResetResponse(BaseModel):
    message: str


async def firebase_identity(request: Request) -> dict:
    """Who Firebase says is calling. No account document is needed or made.

    The usual dependency creates the account and runs the invite gate; a person
    confirming the address of a personal invitation has no account yet (ADR-077),
    and they are exactly who must be able to ask for this email.
    """
    header = request.headers.get("Authorization") or ""
    token = header[7:] if header.startswith("Bearer ") else request.cookies.get("firebase_token")
    if not token:
        raise HTTPException(status_code=401, detail="Authentication required. No Firebase token found.")
    try:
        return verify_firebase_token(token)
    except Exception as exc:  # noqa: BLE001 - the reason is logged, not returned (QA-006)
        logger.info("firebase_identity: token rejected: %s", type(exc).__name__)
        raise HTTPException(status_code=401, detail="Invalid Firebase token.")


@router.post("/verification", response_model=VerificationEmailResponse)
async def send_verification_email(
    request: Request, payload: VerificationEmailRequest, identity: dict = Depends(firebase_identity),
) -> VerificationEmailResponse:
    """Send the caller their verification email, in their language, on Nowry's layout."""
    email = identity.get("email")
    if not email:
        raise HTTPException(status_code=400, detail="This sign-in has no email address.")
    if identity.get("email_verified"):
        return VerificationEmailResponse(status="verified")
    # E2E-007: on development, the suite's own requests skip the per-network check only.
    exempt = dev_outbox.suite_request(request.headers.get("X-Dev-Outbox-Key"), email)
    wait = await account_email.verification_wait(identity.get("uid") or email, client_address(request), exempt_network=exempt)
    if wait:
        return VerificationEmailResponse(status="wait", retry_after=wait)
    status = await account_email.send_verification(email, payload.language)
    return VerificationEmailResponse(status=status)


@router.post("/password-reset", response_model=PasswordResetResponse, status_code=202)
async def send_password_reset_email(
    request: Request, payload: PasswordResetRequest, background: BackgroundTasks,
) -> PasswordResetResponse:
    """Always the same answer, given before anything is looked up (ADR-078 point 7)."""
    if await account_email.reset_wait(payload.email, client_address(request)) == 0:
        background.add_task(account_email.send_reset, payload.email, payload.language)
    return PasswordResetResponse(message=RESET_ACK)
