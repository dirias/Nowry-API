"""Boards (ADR-047, docs/prd-boards.md D2, D5, D10).

A board document is `schema: 2`: `nodes` of four kinds, `links`, a `viewport`
and a revision `rev`. A board saved before ADR-047 (no `schema`) has React Flow
`nodes` and `edges`; it is migrated on its first GET (`app/services/boards.py`).
The collection keeps its name, `blackboards`, and so does the route prefix.
"""
from datetime import datetime
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

NodeKind = Literal["note", "text", "frame", "item"]
NoteColor = Literal["yellow", "green", "blue", "pink", "grey"]
TextSize = Literal["md", "lg", "xl"]
RefKind = Literal["task", "milestone", "goal"]

#: Field bounds. Generous for a learner's board, tight enough that one body
#: cannot store megabytes in a single node.
MAX_ID_LENGTH = 100
MAX_TEXT_LENGTH = 20000
MAX_TITLE_LENGTH = 200
MAX_LABEL_LENGTH = 200
MAX_CARD_IDS = 500


class Viewport(BaseModel):
    x: float = 0
    y: float = 0
    zoom: float = 1


class BoardRef(BaseModel):
    """What an item tile reads: a live task, milestone or goal (ADR-046)."""

    kind: RefKind
    id: str = Field(..., min_length=1, max_length=MAX_ID_LENGTH)
    goal_id: Optional[str] = Field(None, max_length=MAX_ID_LENGTH)


class BoardNode(BaseModel):
    """One thing on a board. Which fields mean something depends on `kind`:
    a note has text and colour, a text has text and size, a frame a title and
    a size, an item a ref. The rest stay null."""

    id: str = Field(..., min_length=1, max_length=MAX_ID_LENGTH)
    kind: NodeKind
    x: float
    y: float
    w: Optional[float] = None
    h: Optional[float] = None
    z: int = 0
    text: Optional[str] = Field(None, max_length=MAX_TEXT_LENGTH)
    color: Optional[NoteColor] = None
    size: Optional[TextSize] = None
    title: Optional[str] = Field(None, max_length=MAX_TITLE_LENGTH)
    ref: Optional[BoardRef] = None
    card_ids: List[str] = Field(default_factory=list, max_length=MAX_CARD_IDS)


class BoardLink(BaseModel):
    """`{id, from, to, label}` — `from` is a Python keyword, hence the alias."""

    model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)

    id: str = Field(..., min_length=1, max_length=MAX_ID_LENGTH)
    from_id: str = Field(..., alias="from", min_length=1, max_length=MAX_ID_LENGTH)
    to: str = Field(..., min_length=1, max_length=MAX_ID_LENGTH)
    label: str = Field("", max_length=MAX_LABEL_LENGTH)


class BoardResponse(BaseModel):
    """GET /blackboards/{id}, POST /blackboards, POST /{id}/duplicate."""

    model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)

    id: str
    name: str
    owner_user_id: Optional[str] = None
    collaborators: List[str] = []
    schema_version: int = Field(2, alias="schema")
    rev: int
    nodes: List[BoardNode] = []
    links: List[BoardLink] = []
    viewport: Viewport = Field(default_factory=Viewport)
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class BoardPatch(BaseModel):
    """PATCH /blackboards/{id}: the changes since `rev` (D5). The server merges
    by id whatever `rev` says; `rev` is the base the client saw."""

    model_config = ConfigDict(extra="forbid")

    rev: int = Field(..., ge=0)
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    viewport: Optional[Viewport] = None
    upsert_nodes: List[BoardNode] = []
    delete_nodes: List[str] = []
    upsert_links: List[BoardLink] = []
    delete_links: List[str] = []


class BoardPatchResponse(BaseModel):
    rev: int
    updated_at: datetime


class BoardCounts(BaseModel):
    notes: int = 0
    links: int = 0
    items: int = 0
    cards: int = 0


class OutlineRect(BaseModel):
    """One rectangle of a tile's thumbnail."""

    x: float
    y: float
    w: float
    h: float
    kind: NodeKind
    color: Optional[NoteColor] = None


class BoardListItem(BaseModel):
    """One row of GET /blackboards: no nodes, counts and an outline instead."""

    id: str
    name: str
    owner_user_id: Optional[str] = None
    owner_name: str = ""
    collaborators: List[str] = []
    is_owner: bool
    updated_at: Optional[datetime] = None
    counts: BoardCounts
    outline: List[OutlineRect] = []


class CreateBoardRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)


class InviteCollaboratorRequest(BaseModel):
    invitee_email: str = Field(..., min_length=1)


class BoardToCardRequest(BaseModel):
    """The selection to turn into cards. `node_texts` is what the client read;
    when it is empty the texts are read from the stored board by `node_ids`."""

    node_ids: List[str]
    node_texts: List[str] = []


class BoardToCardResponse(BaseModel):
    cards: List[Dict[str, str]]   # each: {"front": str, "back": str}
    node_count: int
    nodes_truncated: bool
