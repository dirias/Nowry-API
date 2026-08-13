from pydantic import BaseModel, Field
from typing import Optional, List, Any, Dict
from datetime import datetime


class BlackboardNode(BaseModel):
    id: str
    type: str = "stickyNote"
    position: Dict[str, float]
    data: Dict[str, Any]


class BlackboardEdge(BaseModel):
    id: str
    source: str
    target: str
    type: Optional[str] = "smoothstep"
    animated: Optional[bool] = False
    data: Optional[Dict[str, Any]] = {}


class Blackboard(BaseModel):
    id: Optional[str] = None
    user_id: str                              # Legacy single-board field — kept for backward compat
    owner_user_id: Optional[str] = None      # Phase 7: explicit owner for multi-board
    collaborators: List[str] = []            # Phase 7: user_ids with write access
    name: str = "My Blackboard"
    nodes: List[Dict[str, Any]] = []
    edges: List[Dict[str, Any]] = []
    viewport: Optional[Dict[str, float]] = {"x": 0, "y": 0, "zoom": 1}
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    # Soft delete (30-day retention, purged by the `soft_delete_ttl` index)
    deleted_at: Optional[datetime] = None
    deleted_by: Optional[str] = None


class BlackboardUpdate(BaseModel):
    name: Optional[str] = None
    nodes: Optional[List[Dict[str, Any]]] = None
    edges: Optional[List[Dict[str, Any]]] = None
    viewport: Optional[Dict[str, float]] = None


# ── Phase 7: New request/response models ──────────────────────────────────────

class CreateBoardRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)


class InviteCollaboratorRequest(BaseModel):
    invitee_email: str = Field(..., min_length=1)


class BoardToCardRequest(BaseModel):
    node_ids: List[str]
    node_texts: List[str]


class BoardToCardResponse(BaseModel):
    cards: List[Dict[str, str]]   # each: {"front": str, "back": str}
    node_count: int
    nodes_truncated: bool
