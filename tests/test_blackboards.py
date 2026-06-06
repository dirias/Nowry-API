"""
Phase 7 — Blackboard multi-board tests.
Stubs written in Wave 0; implementations follow in Wave 2.
All tests fail at import until routers/blackboards.py is updated.
"""
import sys
from unittest.mock import MagicMock, AsyncMock

# Stub out imports that may not be present yet
for mod in ["app.models.agent_models"]:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()

import pytest


# --------------------------------------------------------------------------- #
# BB-01 — Free tier: single board cap
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_free_board_limit_returns_402(mock_blackboards_collection, mock_firebase_user):
    """Free user cannot create a second board — POST /blackboards returns 402."""
    pytest.skip("Wave 0 stub — implement in 07-02-PLAN.md")


# --------------------------------------------------------------------------- #
# BB-01/BB-02 — Tier-appropriate board list
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_list_boards_returns_owned_and_shared(mock_blackboards_collection, mock_firebase_user):
    """GET /blackboards returns boards where user is owner OR collaborator."""
    pytest.skip("Wave 0 stub — implement in 07-02-PLAN.md")


# --------------------------------------------------------------------------- #
# BB-02 — Collaboration: save guard
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_save_board_denied_when_not_owner_or_collaborator(mock_blackboards_collection, mock_firebase_user):
    """PUT /blackboards/{id} returns 403 when user is neither owner nor collaborator."""
    pytest.skip("Wave 0 stub — implement in 07-02-PLAN.md")


# --------------------------------------------------------------------------- #
# BB-02 — Collaboration: invite
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_invite_collaborator_success(mock_blackboards_collection, mock_firebase_user, mock_invitation):
    """PUT /blackboards/{id}/invite adds collaborator to board."""
    pytest.skip("Wave 0 stub — implement in 07-02-PLAN.md")


@pytest.mark.asyncio
async def test_invite_collaborator_requires_ownership(mock_blackboards_collection, mock_firebase_user):
    """PUT /blackboards/{id}/invite returns 403 when caller is not owner."""
    pytest.skip("Wave 0 stub — implement in 07-02-PLAN.md")


# --------------------------------------------------------------------------- #
# BB-04 — Board-to-card generation
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_generate_cards_from_board_success(mock_blackboards_collection, mock_firebase_user):
    """POST /blackboards/{id}/generate-cards returns cards list for valid node selection."""
    pytest.skip("Wave 0 stub — implement in 07-02-PLAN.md")


@pytest.mark.asyncio
async def test_generate_cards_empty_text_returns_422(mock_blackboards_collection, mock_firebase_user):
    """POST /blackboards/{id}/generate-cards returns 422 when all nodes have no extractable text."""
    pytest.skip("Wave 0 stub — implement in 07-02-PLAN.md")
