"""
Phase 7 — Blackboard multi-board tests (implemented in Phase 34 Plan 01).

Tests call the router functions in `app.routers.blackboards` directly rather than
going through TestClient/HTTP, passing the `Depends()` parameters as plain kwargs.
This mirrors the pattern established in `test_annual_planning_be.py`.

`blackboards.py` does `from app.config.database import db` and then calls
`db.blackboards.<op>` / `db.users.<op>`, so every test patches the single module
attribute `app.routers.blackboards.db` — NOT an individually-imported collection.
"""
import sys
from unittest.mock import MagicMock, AsyncMock, patch

# Stub out imports that may not be present yet.
# `langfuse.langchain` is required because app.routers.blackboards imports the
# AI orchestrator, whose module-level `from langfuse.langchain import
# CallbackHandler` fails against conftest's bare `langfuse` MagicMock
# ("'langfuse' is not a package"). Same stub as tests/test_tracing.py L21.
for mod in ["app.models.agent_models", "langfuse", "langfuse.langchain"]:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()

import pytest
from bson import ObjectId
from fastapi import HTTPException


# --------------------------------------------------------------------------- #
# Shared test data helpers
# --------------------------------------------------------------------------- #
OTHER_USER_ID = "507f1f77bcf86cd799439099"


def make_board(owner_user_id: str, collaborators=None, name: str = "Test Board") -> dict:
    """Build a Phase 7 multi-board document owned by `owner_user_id`."""
    return {
        "_id": ObjectId(),
        "owner_user_id": owner_user_id,
        "user_id": owner_user_id,
        "collaborators": list(collaborators or []),
        "name": name,
        "nodes": [],
        "edges": [],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }


def make_cursor(docs: list) -> MagicMock:
    """Mock a Motor cursor whose bounded `.to_list(length=N)` resolves to `docs`."""
    cursor = MagicMock()
    cursor.to_list = AsyncMock(return_value=docs)
    return cursor


# --------------------------------------------------------------------------- #
# BB-01 — Free tier: single board cap
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_free_board_limit_returns_402(mock_blackboards_collection, mock_firebase_user):
    """Free user cannot create a second board — POST /blackboards returns 402."""
    from app.routers.blackboards import create_board
    from app.models.Blackboard import CreateBoardRequest

    with patch("app.routers.blackboards.db") as mock_db:
        # Free tier caps at 1 board; the user already owns one.
        mock_db.blackboards.count_documents = AsyncMock(return_value=1)
        mock_db.blackboards.insert_one = AsyncMock()

        with pytest.raises(HTTPException) as exc_info:
            await create_board(
                body=CreateBoardRequest(name="Second Board"),
                current_user=mock_firebase_user,
                tier="free",
            )

    assert exc_info.value.status_code == 402
    assert exc_info.value.detail == "board_limit_reached"
    # The cap is enforced before any write.
    mock_db.blackboards.insert_one.assert_not_called()
    mock_db.blackboards.count_documents.assert_awaited_once_with(
        {"owner_user_id": mock_firebase_user["user_id"]}
    )


# --------------------------------------------------------------------------- #
# BB-01/BB-02 — Tier-appropriate board list
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_list_boards_returns_owned_and_shared(mock_blackboards_collection, mock_firebase_user):
    """GET /blackboards returns boards where user is owner OR collaborator."""
    from app.routers.blackboards import list_boards

    user_id = mock_firebase_user["user_id"]
    owned_board = make_board(owner_user_id=user_id, name="My Board")
    shared_board = make_board(
        owner_user_id=OTHER_USER_ID,
        collaborators=[user_id],
        name="Shared With Me",
    )
    owned_id, shared_id = str(owned_board["_id"]), str(shared_board["_id"])

    with patch("app.routers.blackboards.db") as mock_db:
        # list_boards() calls find() twice: owned filter first, then shared filter.
        mock_db.blackboards.find = MagicMock(
            side_effect=[make_cursor([owned_board]), make_cursor([shared_board])]
        )

        result = await list_boards(current_user=mock_firebase_user)

    assert len(result) == 2
    by_id = {doc["id"]: doc for doc in result}
    assert by_id[owned_id]["is_owner"] is True
    assert by_id[shared_id]["is_owner"] is False

    # Both queries are bounded and use the expected filters.
    owned_call, shared_call = mock_db.blackboards.find.call_args_list
    assert owned_call.args[0] == {"owner_user_id": user_id}
    assert shared_call.args[0] == {"collaborators": user_id}


# --------------------------------------------------------------------------- #
# BB-02 — Collaboration: save guard
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_save_board_denied_when_not_owner_or_collaborator(mock_blackboards_collection, mock_firebase_user):
    """PUT /blackboards/{id} returns 403 when user is neither owner nor collaborator."""
    from app.routers.blackboards import save_blackboard
    from app.models.Blackboard import BlackboardUpdate

    # Board belongs to someone else and the caller is not a collaborator.
    foreign_board = make_board(owner_user_id=OTHER_USER_ID, collaborators=[])

    with patch("app.routers.blackboards.db") as mock_db:
        mock_db.blackboards.find_one = AsyncMock(return_value=foreign_board)
        mock_db.blackboards.find_one_and_update = AsyncMock()

        with pytest.raises(HTTPException) as exc_info:
            await save_blackboard(
                board_id=str(foreign_board["_id"]),
                update=BlackboardUpdate(name="x"),
                current_user=mock_firebase_user,
            )

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "board_access_denied"
    # The guard rejects before persisting anything.
    mock_db.blackboards.find_one_and_update.assert_not_called()


# --------------------------------------------------------------------------- #
# BB-02 — Collaboration: invite
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_invite_collaborator_success(mock_blackboards_collection, mock_firebase_user, mock_invitation):
    """PUT /blackboards/{id}/invite adds collaborator to board."""
    from app.routers.blackboards import invite_collaborator
    from app.models.Blackboard import InviteCollaboratorRequest

    board = make_board(owner_user_id=mock_firebase_user["user_id"])
    invitee_id = ObjectId()
    invitee_doc = {"_id": invitee_id, "email": mock_invitation["invitee_email"]}

    with patch("app.routers.blackboards.db") as mock_db:
        mock_db.blackboards.find_one = AsyncMock(return_value=board)
        mock_db.users.find_one = AsyncMock(return_value=invitee_doc)
        mock_db.blackboards.update_one = AsyncMock()

        result = await invite_collaborator(
            board_id=str(board["_id"]),
            body=InviteCollaboratorRequest(**mock_invitation),
            current_user=mock_firebase_user,
        )

    assert result == {"ok": True}
    # Invitee is resolved by email, never trusted from the request body as an id.
    mock_db.users.find_one.assert_awaited_once_with(
        {"email": mock_invitation["invitee_email"]}
    )
    filter_arg, update_arg = mock_db.blackboards.update_one.await_args.args
    assert filter_arg == {"_id": board["_id"]}
    assert update_arg == {"$addToSet": {"collaborators": str(invitee_id)}}


@pytest.mark.asyncio
async def test_invite_collaborator_requires_ownership(mock_blackboards_collection, mock_firebase_user):
    """PUT /blackboards/{id}/invite returns 403 when caller is not owner."""
    from app.routers.blackboards import invite_collaborator
    from app.models.Blackboard import InviteCollaboratorRequest

    # Caller is a collaborator on someone else's board — still may not invite.
    foreign_board = make_board(
        owner_user_id=OTHER_USER_ID,
        collaborators=[mock_firebase_user["user_id"]],
    )

    with patch("app.routers.blackboards.db") as mock_db:
        mock_db.blackboards.find_one = AsyncMock(return_value=foreign_board)
        mock_db.users.find_one = AsyncMock()
        mock_db.blackboards.update_one = AsyncMock()

        with pytest.raises(HTTPException) as exc_info:
            await invite_collaborator(
                board_id=str(foreign_board["_id"]),
                body=InviteCollaboratorRequest(invitee_email="someone@example.com"),
                current_user=mock_firebase_user,
            )

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "only_owner_can_invite"
    # Ownership is checked before the invitee lookup and before any write.
    mock_db.users.find_one.assert_not_called()
    mock_db.blackboards.update_one.assert_not_called()


# --------------------------------------------------------------------------- #
# BB-04 — Board-to-card generation
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_generate_cards_from_board_success(mock_blackboards_collection, mock_firebase_user):
    """POST /blackboards/{id}/generate-cards returns cards list for valid node selection."""
    from app.routers.blackboards import generate_cards_from_board
    from app.models.Blackboard import BoardToCardRequest

    board = make_board(owner_user_id=mock_firebase_user["user_id"])
    generated = [{"front": "Q1", "back": "A1"}]

    # orchestrator.invoke is a sync callable — it runs inside asyncio.to_thread.
    fake_invoke = MagicMock(return_value={"generated_cards": generated})

    with patch("app.routers.blackboards.db") as mock_db, \
         patch("app.routers.blackboards.orchestrator.invoke", fake_invoke):
        mock_db.blackboards.find_one = AsyncMock(return_value=board)

        result = await generate_cards_from_board(
            board_id=str(board["_id"]),
            body=BoardToCardRequest(node_ids=["n1"], node_texts=["Some node text"]),
            current_user=mock_firebase_user,
            tier="pro",
        )

    assert result.cards == generated
    assert result.node_count == 1
    assert result.nodes_truncated is False

    # The "rag" graph is invoked with the cleaned node text in the state payload.
    graph_name, state = fake_invoke.call_args.args
    assert graph_name == "rag"
    assert "Some node text" in state["sampleText"]
    assert state["tier"] == "pro"


@pytest.mark.asyncio
async def test_generate_cards_empty_text_returns_422(mock_blackboards_collection, mock_firebase_user):
    """POST /blackboards/{id}/generate-cards returns 422 when all nodes have no extractable text."""
    from app.routers.blackboards import generate_cards_from_board
    from app.models.Blackboard import BoardToCardRequest

    board = make_board(owner_user_id=mock_firebase_user["user_id"])
    fake_invoke = MagicMock()

    with patch("app.routers.blackboards.db") as mock_db, \
         patch("app.routers.blackboards.orchestrator.invoke", fake_invoke):
        mock_db.blackboards.find_one = AsyncMock(return_value=board)

        with pytest.raises(HTTPException) as exc_info:
            await generate_cards_from_board(
                board_id=str(board["_id"]),
                body=BoardToCardRequest(node_ids=["n1", "n2"], node_texts=["   ", ""]),
                current_user=mock_firebase_user,
                tier="pro",
            )

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail == "no_extractable_text"
    # No LLM spend on an empty selection.
    fake_invoke.assert_not_called()
