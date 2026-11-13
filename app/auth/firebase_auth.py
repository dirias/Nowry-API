"""
Firebase Authentication Module
Handles Firebase token validation and user authentication with caching
"""

from __future__ import annotations

from fastapi import HTTPException, Header, Request, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pymongo.errors import DuplicateKeyError
from app.auth.email_verification import session_is_cut, verification_update
from app.config.firebase_config import verify_firebase_token
from app.config.subscription_plans import SubscriptionTier
from app.config.beta import INVITE_HEADER
from app.services.beta_invites import gate_account_creation, release_personal_invite, settle_personal_invite
from app.models.User import USERNAME_MAX_LENGTH, sanitize_username
from app.services.account_lookup import active_account_by_email
from functools import lru_cache
from typing import Optional
from datetime import datetime, timezone
import asyncio
import logging
import os
import time
import stripe as _stripe

# Configure Stripe at module level — never inside a request handler (RESEARCH.md Pattern 1)
_stripe.api_key = os.getenv("STRIPE_SECRET_KEY")

logger = logging.getLogger(__name__)

def _first_of_next_month(dt: datetime) -> datetime:
    """Returns the first day of the next calendar month at 00:00:00 UTC."""
    if dt.month == 12:
        return dt.replace(year=dt.year + 1, month=1, day=1,
                          hour=0, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
    return dt.replace(month=dt.month + 1, day=1,
                      hour=0, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)


# Simple in-memory cache for validated tokens (UID -> {token_data, expiry})
_token_cache = {}
_CACHE_TTL = 300  # 5 minutes cache

# HTTP Bearer for optional auth
security = HTTPBearer(auto_error=False)


# uid -> sessions_valid_after, for accounts whose older sessions were cut in
# this process (ADR-076): a token cached before the cut is refused at once
# here, and within the cache's five minutes on another process.
_sessions_cut: dict = {}

def _refuse_a_cut_session(valid_after, token_data: dict) -> None:
    """401 when the token's sign-in is older than the account's cut (ADR-076)."""
    if session_is_cut(valid_after, token_data):
        raise HTTPException(status_code=401, detail="This session has ended. Sign in again.")


async def _record_verification(users_collection, user: dict, token_data: dict) -> None:
    """Store what Firebase says about the email, and cut older sessions when it calls for it (ADR-076)."""
    update = verification_update(user, token_data)
    if not update:
        return
    await users_collection.update_one({"_id": user["_id"]}, {"$set": update})
    user.update(update)
    if update.get("sessions_valid_after"):
        _sessions_cut[token_data["firebase_uid"]] = update["sessions_valid_after"]
        logger.warning("auth: a sign-in by %s verified an account made unverified; older sessions are cut", token_data.get("sign_in_provider"))


#: The 409 when two live Firebase identities claim one account (ADR-083).
ACCOUNT_CONFLICT_CODE: str = "account_conflict"


def _firebase_identity_exists(firebase_uid: str) -> Optional[bool]:
    """True or False from Firebase; None when Firebase could not be asked."""
    from firebase_admin import auth as firebase_admin_auth

    try:
        firebase_admin_auth.get_user(firebase_uid)
        return True
    except firebase_admin_auth.UserNotFoundError:
        return False
    except Exception as exc:  # noqa: BLE001 - an unanswered lookup changes nothing
        logger.warning("auth: could not ask Firebase about a stored sign-in id: %s", type(exc).__name__)
        return None


async def _alert_conflict() -> None:
    """Tell the operator, at most once an hour, that two live identities claim one account."""
    from app.services.notifications import notify
    from app.utils.keyed_rate_limit import seconds_to_wait

    logger.error("auth: two live Firebase identities claim one account; not merged (ADR-083)")
    try:
        if await seconds_to_wait("auth_conflict_notice", "all", 1, 3600) == 0:
            await notify(
                "[Nowry] Sign-in conflict: two identities, one account",
                "A verified sign-in matched an account whose own Firebase id is still alive. "
                "Nothing was merged; the person was told to write to support. Check the API logs.",
            )
    except Exception as exc:  # noqa: BLE001 - an alert never changes the answer
        logger.warning("auth: the conflict notice was not sent: %s", type(exc).__name__)


async def _relink_if_identity_gone(users_collection, user: dict, token_uid: str) -> None:
    """A verified email found this account under another Firebase id (ADR-083, FR-030).

    The stored id gone from Firebase: write the token's id on the account. Still
    alive: two identities claim one account, refused 409 and nothing written.
    Firebase unreachable: nothing written; the request is served by `user_id`.
    """
    stored_uid = user.get("firebase_uid")
    if not token_uid or stored_uid == token_uid:
        return
    exists = await asyncio.to_thread(_firebase_identity_exists, stored_uid) if stored_uid else False
    if exists is None:
        return
    if exists:
        await _alert_conflict()
        raise HTTPException(status_code=409, detail={"code": ACCOUNT_CONFLICT_CODE})
    await users_collection.update_one(
        {"_id": user["_id"], "firebase_uid": stored_uid},
        {"$set": {"firebase_uid": token_uid, "updated_at": datetime.now(timezone.utc)}},
    )
    user["firebase_uid"] = token_uid
    logger.info("auth: re-linked an account whose Firebase id no longer exists")


def _get_cached_token(token: str) -> dict | None:
    """Check if token is in cache and not expired"""
    if token in _token_cache:
        cached_data, expiry = _token_cache[token]
        if time.time() < expiry:
            return cached_data
        else:
            # Expired, remove from cache
            del _token_cache[token]
    return None


def _cache_token(token: str, token_data: dict):
    """Cache validated token data"""
    _token_cache[token] = (token_data, time.time() + _CACHE_TTL)
    
    # Simple cache cleanup - remove old entries if cache gets too large
    if len(_token_cache) > 1000:
        current_time = time.time()
        expired_keys = [k for k, (_, exp) in _token_cache.items() if current_time >= exp]
        for k in expired_keys:
            del _token_cache[k]


async def get_firebase_user(request: Request) -> dict:
    """
    Extract and validate Firebase ID token from Authorization header or cookie
    
    Args:
        request: FastAPI Request object
        
    Returns:
        dict: Decoded Firebase token with user claims
        
    Raises:
        HTTPException: If token is missing or invalid
    """
    # Try to get token from Authorization header first
    auth_header = request.headers.get("Authorization")
    token = None
    
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header.replace("Bearer ", "")
    
    # Fallback to cookie
    if not token:
        token = request.cookies.get("firebase_token")
    
    if not token:
        raise HTTPException(
            status_code=401,
            detail="Authentication required. No Firebase token found."
        )
    
    # Check cache first
    cached_data = _get_cached_token(token)
    if cached_data:
        _refuse_a_cut_session(_sessions_cut.get(cached_data.get("firebase_uid")), cached_data)
        return cached_data
    
    try:
        # Verify token with Firebase Admin SDK (network call - slow)
        decoded_token = verify_firebase_token(token)
    except Exception as e:
        # The reason (expired, revoked, wrong project) is logged, not returned:
        # it tells a caller probing tokens which check failed (QA-006).
        logger.info("get_firebase_user: token rejected: %s", e)
        raise HTTPException(
            status_code=401,
            detail="Invalid Firebase token."
        )

    # ADR-077: the stamp of a personal invitation spent by this request, so it
    # can be given back if the account is never written.
    beta_stamp: Optional[dict] = None
    token_data: dict = {}
    try:
        token_data = {
            "firebase_uid": decoded_token.get("uid"),
            "email": decoded_token.get("email"),
            "email_verified": decoded_token.get("email_verified", False),
            "name": decoded_token.get("name"),
            "picture": decoded_token.get("picture"),
            # ADR-076: who signed in and when, to tell an owner's sign-in from an earlier session.
            "sign_in_provider": (decoded_token.get("firebase") or {}).get("sign_in_provider"),
            "auth_time": decoded_token.get("auth_time"),
        }

        # --- Resolve MongoDB user_id ---
        # Many endpoints expect "user_id" (MongoDB _id) to be present.
        from app.config.database import users_collection

        # Search without filtering on deleted_at so soft-deleted docs are found too.
        # Try by firebase_uid first, then fall back to email (covers the case where
        # Firebase deleted the user and re-issued a new UID on re-registration with
        # the same Google account — same email, different UID).
        # ADR-081: a deleted account is never found here. Its identity was
        # released when it was deleted, and the filter says so as well, so a
        # sign-in with the same address is a new account that sees nothing old.
        user = await users_collection.find_one({"firebase_uid": token_data["firebase_uid"], "deleted_at": None})
        # Only a verified email may claim an existing account (QA-008): the case
        # this serves is a Google account, whose email Firebase has verified.
        # An unverified email is anyone's word, and would have resolved a new
        # Firebase account to whichever Nowry account holds that address.
        if not user and token_data.get("email"):
            user = await active_account_by_email(users_collection, token_data["email"])
            # The same uid is sign-up's own race (QA-010: /users/me and
            # /auth/register both provision), not a claim: use it.
            if user and user.get("firebase_uid") != token_data["firebase_uid"] and not token_data.get("email_verified"):
                # ADR-083 case 2: the address has an account, and this identity
                # has not proven it owns the address. Confirm first; the
                # verified sign-in that follows re-links. Before the invite
                # gate, so the answer is "confirm", not "private beta".
                raise HTTPException(status_code=409, detail={"code": "email_unverified"})
            if user:
                await _relink_if_identity_gone(users_collection, user, token_data["firebase_uid"])

        if user:
            # ADR-076: record the email's standing first, so the request that
            # verifies an account also sets the mark older sessions fail.
            await _record_verification(users_collection, user, token_data)
            _refuse_a_cut_session(user.get("sessions_valid_after"), token_data)

        if user:
            # Normal path: an active account. A deleted one is never read here
            # (ADR-081): nothing brings it back on its own.
            token_data["user_id"] = str(user["_id"])
        else:
            # Valid Firebase token but no MongoDB document — first-time sign-in
            # via Google before /auth/register is called.
            # Auto-create a minimal user document so the request succeeds and
            # the frontend can drive onboarding (wizard) normally.
            #
            # ADR-038: this is the one place a brand-new account comes into
            # being, for the email flow and the Google flow alike, so the beta
            # invite is enforced here. A refusal is raised before anything is
            # written; an existing account never reaches this branch.
            #
            # ADR-077: the header is a cohort code or a personal invitation.
            # A personal one is spent here, only by its own verified address.
            beta_stamp = await gate_account_creation(request.headers.get(INVITE_HEADER), token_data)
            display_name: str = token_data.get("name") or ""
            email: str = token_data.get("email") or ""
            raw_username: str = display_name or (email.split("@")[0] if email else "user")
            # This path never goes through RegisterRequest/ProfilePatchRequest
            # validation (there is no user-submitted form here), so the
            # derived value is sanitized against the identical username
            # format rule those models enforce — e.g. an email local-part
            # like "rikydier+nu1" would otherwise write an invalid "+".
            username: str = sanitize_username(raw_username, token_data["firebase_uid"][:6])
            now = datetime.now(timezone.utc)

            # Create Stripe customer — wrapped in try/except so a Stripe failure
            # never blocks user creation (T-03-02-02 mitigation)
            stripe_customer_id = None
            try:
                customer = await asyncio.to_thread(
                    _stripe.Customer.create,
                    email=email,
                    metadata={"firebase_uid": token_data["firebase_uid"]},
                )
                stripe_customer_id = customer.id
            except Exception as _stripe_err:
                logger.warning("Stripe customer creation failed for new user: %s", _stripe_err)

            new_user_doc = {
                "firebase_uid": token_data["firebase_uid"],
                "email": email,
                "username": username,
                "username_source": "derived",  # ADR-085: /auth/register may replace it once with the chosen name
                "photo_url": token_data.get("picture"),
                "role": "user",
                "stripe_customer_id": stripe_customer_id,  # None if Stripe call failed
                "subscription": {
                    "tier": SubscriptionTier.FREE.value,
                    "status": "active",
                    "ai_usage_count": 0,
                    "ai_usage_reset_date": _first_of_next_month(now),
                    "next_billing_date": None,
                    "stripe_subscription_id": None,
                    "billing_interval": None,
                    "subscription_status_updated_at": now,
                },
                "wizard_completed": False,
                # ADR-076: what Firebase says of the email today, and how the account was made.
                "email_verified": bool(token_data.get("email_verified")),
                "signup_provider": token_data.get("sign_in_provider"),
                "beta": beta_stamp,  # ADR-038: cohort and code when the gate let them in
                "preferences": {
                    "pet": {
                        # Mirror register_user's new-account defaults (auth.py):
                        # ROAM-002, FR-037: the study buddy is on and revealed
                        # from the first screen, resting in one place (roaming
                        # is off, FR-036), whichever request makes the account.
                        "pet_active": True,
                        "pet_revealed": True,
                    }
                },
                "created_at": now,
                "updated_at": now,
            }
            try:
                result = await users_collection.insert_one(new_user_doc)
                token_data["user_id"] = str(result.inserted_id)
            except DuplicateKeyError:
                # ADR-081: a record deleted before its identity was released at
                # deletion still holds this address or sign-in id. Release it
                # now and make the new account; the old one stays deleted.
                stale = await users_collection.find_one({
                    "$or": [{"email": email}, {"firebase_uid": token_data["firebase_uid"]}],
                    "deleted_at": {"$ne": None},
                })
                if stale:
                    from app.services.account_deletion import release_identity

                    await release_identity(stale, stale["deleted_at"], keep_existing=True)
                    result = await users_collection.insert_one(new_user_doc)
                    token_data["user_id"] = str(result.inserted_id)
                if "user_id" not in token_data:
                    # A unique index (firebase_uid/email/username) collided with an
                    # already-existing active user document. This happens when:
                    #   - two concurrent first-login requests race the insert, or
                    #   - the token's uid/email no longer matches the stored doc
                    #     (e.g. re-auth issued a new uid for the same email).
                    # Recover instead of 500ing: re-fetch the real existing doc by
                    # email (most reliable unique key here) and sync firebase_uid
                    # onto it so this and future logins resolve correctly.
                    #
                    # By email only when Firebase has verified it (QA-008): this
                    # branch re-points the account at the caller's uid, so an
                    # unverified address would hand someone else's account over.
                    #
                    # The same uid is the race above (sign-up's /users/me and
                    # /auth/register both provision), not a claim: a brand-new
                    # email account is always unverified, so refusing it broke
                    # every email sign-up (found by the QA-010 end-to-end run).
                    if email and not token_data.get("email_verified"):
                        holder = await users_collection.find_one({"email": email, "deleted_at": None}, {"firebase_uid": 1})
                        if holder and holder.get("firebase_uid") != token_data["firebase_uid"]:
                            raise HTTPException(
                                status_code=409,
                                detail={"code": "email_unverified"},
                            )
                    existing = await users_collection.find_one({"email": email, "deleted_at": None}) if email else None
                    if not existing:
                        existing = await users_collection.find_one(
                            {"firebase_uid": token_data["firebase_uid"], "deleted_at": None}
                        )
                    if existing:
                        if existing.get("firebase_uid") != token_data["firebase_uid"]:
                            await users_collection.update_one(
                                {"_id": existing["_id"]},
                                {
                                    "$set": {
                                        "firebase_uid": token_data["firebase_uid"],
                                        "updated_at": now,
                                    }
                                },
                            )
                        token_data["user_id"] = str(existing["_id"])
                    else:
                        # No matching doc by email/uid — this was a genuine
                        # username collision between two different accounts that
                        # happen to share a display name. Disambiguate and retry
                        # once rather than 500ing on a brand-new sign-up.
                        # Truncated: `username` is already sanitized to at most
                        # USERNAME_MAX_LENGTH chars, but appending "-{uid[:6]}"
                        # could otherwise push the result past that limit.
                        new_user_doc["username"] = (
                            f"{username}-{token_data['firebase_uid'][:6]}"[:USERNAME_MAX_LENGTH]
                        )
                        try:
                            result = await users_collection.insert_one(new_user_doc)
                            token_data["user_id"] = str(result.inserted_id)
                        except DuplicateKeyError:
                            # Still colliding (e.g. firebase_uid/email race resolved
                            # elsewhere in the meantime) — surface the failure.
                            raise

        # ADR-077: a personal invitation now belongs to the account it made.
        await settle_personal_invite(beta_stamp, token_data["user_id"], token_data["firebase_uid"])

        # uid is an alias for firebase_uid (Firebase authentication identifier only)
        # NEVER use uid for MongoDB storage — always use user_id (MongoDB ObjectId string)
        token_data["uid"] = token_data.get("firebase_uid")

        # Cache the validated token AFTER MongoDB data is fully joined
        _cache_token(token, token_data)

        return token_data
    except HTTPException:
        if "user_id" not in token_data:
            await release_personal_invite(beta_stamp, token_data.get("firebase_uid") or "")
        raise
    except Exception as e:
        if "user_id" not in token_data:
            await release_personal_invite(beta_stamp, token_data.get("firebase_uid") or "")
        # Log the full traceback server-side — the client only ever sees the
        # generic detail string below (no exception text: it can carry database
        # internals, QA-006), but without this, an unhandled error
        # here is invisible in the access logs (no traceback is ever printed
        # for a deliberately-raised HTTPException).
        logger.exception("get_firebase_user: failed to resolve user profile")
        raise HTTPException(
            status_code=500,
            detail="Failed to resolve user profile."
        )


async def get_current_user(request: Request) -> dict:
    """
    Get authenticated user (required).
    Alias for get_firebase_user with uid compatibility.
    """
    user_data = await get_firebase_user(request)
    return user_data


async def optional_auth(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)
) -> Optional[dict]:
    """
    Get authenticated user (optional - returns None if not logged in).
    Used for public endpoints that work for both authenticated and anonymous users.
    
    Args:
        request: FastAPI Request object
        credentials: Optional HTTP Bearer credentials
        
    Returns:
        dict | None: User data if authenticated, None otherwise
    """
    if not credentials:
        return None
    
    try:
        # Extract token
        token = credentials.credentials
        
        # Check cache first
        cached_data = _get_cached_token(token)
        if cached_data:
            return cached_data
        
        # Verify token
        decoded_token = verify_firebase_token(token)
        
        token_data = {
            "firebase_uid": decoded_token.get("uid"),
            "uid": decoded_token.get("uid"),  # Alias
            "email": decoded_token.get("email"),
            "email_verified": decoded_token.get("email_verified", False),
            "name": decoded_token.get("name"),
            "picture": decoded_token.get("picture"),
            "auth_time": decoded_token.get("auth_time"),
        }
        
        # Fetch MongoDB user ID
        from app.config.database import users_collection
        # ADR-081: a deleted account is never found here. Its identity was
        # released when it was deleted, and the filter says so as well, so a
        # sign-in with the same address is a new account that sees nothing old.
        user = await users_collection.find_one({"firebase_uid": token_data["firebase_uid"], "deleted_at": None})
        
        if user:
            # ADR-076: a session cut on this account is nobody here either.
            if session_is_cut(user.get("sessions_valid_after"), token_data):
                return None
            token_data["user_id"] = str(user["_id"])
        
        # Cache it
        _cache_token(token, token_data)
        
        return token_data
    except:
        # Silent fail - user not logged in or invalid token
        return None
