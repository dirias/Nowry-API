"""Boards v2: the v1 migration, the change merge, the limits, the list summary
(BOARD-001, ADR-047, docs/prd-boards.md D2, D5, D8, D10).

Everything here is pure: it takes plain board documents and dicts and returns
new ones. The only I/O a migration needs, the retired priorities an entity
pointed at, is resolved by the router beforehand with one bounded query and
handed in as `priority_refs` ({priority id: ref or None}).
"""
from __future__ import annotations

from typing import Any, Iterable, Optional

from fastapi import HTTPException

from app.models.Blackboard import MAX_TEXT_LENGTH, BoardPatch

SCHEMA = 2
MAX_NODES = 500
MAX_LINKS = 1000
MAX_CHANGES = 300
MAX_OUTLINE = 60
FRAME_Z = -1

#: The size a node is drawn at when it has none of its own.
DEFAULT_SIZES: dict[str, tuple[float, float]] = {
    "note": (220.0, 100.0),
    "text": (240.0, 40.0),
    "frame": (300.0, 300.0),
    "item": (256.0, 56.0),
}

#: Eight v1 sticky colours fold to five (D8); anything else is yellow.
COLOR_MAP = {
    "yellow": "yellow",
    "green": "green",
    "teal": "green",
    "blue": "blue",
    "purple": "blue",
    "pink": "pink",
    "red": "pink",
    "slate": "grey",
}

DEFAULT_VIEWPORT = {"x": 0.0, "y": 0.0, "zoom": 1.0}


# --- reading a document -------------------------------------------------------


def is_v2(doc: dict) -> bool:
    return doc.get("schema") == SCHEMA


def make_node(node_id: Any, kind: str, x: Any, y: Any, **fields: Any) -> dict:
    """A stored node in its full shape: every key present, unused ones null."""
    node = {
        "id": str(node_id), "kind": kind, "x": _num(x), "y": _num(y),
        "w": None, "h": None, "z": 0, "text": None, "color": None,
        "size": None, "title": None, "ref": None, "card_ids": [],
    }
    node.update(fields)
    return node


def make_link(link_id: Any, from_id: Any, to_id: Any, label: Any = "") -> dict:
    return {"id": str(link_id), "from": str(from_id), "to": str(to_id), "label": str(label or "")}


def viewport_of(doc: dict) -> dict:
    """The stored viewport as {x, y, zoom}, defaults filling what is missing."""
    raw = doc.get("viewport") or {}
    return {key: _num(raw.get(key), default) for key, default in DEFAULT_VIEWPORT.items()}


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# --- the v1 → v2 migration (D8) -----------------------------------------------


def v1_size(node: dict) -> tuple[Optional[float], Optional[float]]:
    """A v1 node's size: width/height, else measured, else style, else none."""
    for source in (node, node.get("measured") or {}, node.get("style") or {}):
        width, height = source.get("width"), source.get("height")
        if isinstance(width, (int, float)) and isinstance(height, (int, float)):
            return float(width), float(height)
    return None, None


def sticky_text(data: dict) -> str:
    """A sticky's title and body become one text; the first line is the title."""
    title = str(data.get("title") or "").strip()
    body = str(data.get("body") or "").strip()
    return clip((title + ("\n" + body if body else "")).strip())


def clip(text: Any) -> str:
    """A v1 string, stripped and cut to what a v2 node holds."""
    return str(text or "").strip()[:MAX_TEXT_LENGTH]


def priority_ref(priority: dict) -> Optional[dict]:
    """Where a retired priority went (PLAN-013's `migrated_to`), as an item ref.

    A task is enough on its own; a milestone needs its goal, which is the
    priority's `linked_entity_id` when it was linked to a goal. Anything that
    cannot be resolved is None, and the entity becomes a note.
    """
    target = priority.get("migrated_to") or {}
    if target.get("type") == "task" and target.get("id"):
        return {"kind": "task", "id": str(target["id"]), "goal_id": None}
    goal_id = priority.get("linked_entity_id") if priority.get("linked_entity_type") == "goal" else None
    if target.get("type") == "milestone" and target.get("id") and goal_id:
        return {"kind": "milestone", "id": str(target["id"]), "goal_id": str(goal_id)}
    return None


def copy_name(name: str, limit: int = 100) -> str:
    """A duplicate's name: the original's, cut so " (copy)" always fits."""
    suffix = " (copy)"
    return (name or "")[: limit - len(suffix)] + suffix


def priority_ids(docs: Iterable[dict]) -> list[str]:
    """Every priority a v1 board's entity nodes point at, once each, in order."""
    seen: dict[str, None] = {}
    for doc in docs:
        if is_v2(doc):
            continue
        for node in doc.get("nodes") or []:
            data = node.get("data") or {}
            if node.get("type") == "entity" and data.get("entityType") == "priority" and data.get("entityId"):
                seen.setdefault(str(data["entityId"]), None)
    return list(seen)


def _entity_fields(data: dict, priority_refs: dict[str, Optional[dict]]) -> dict:
    """An entity's v2 fields: an item when its target resolves, else a note."""
    entity_type, entity_id = data.get("entityType"), data.get("entityId")
    ref = None
    if entity_type in ("task", "goal") and entity_id:
        ref = {"kind": entity_type, "id": str(entity_id), "goal_id": None}
    elif entity_type == "priority" and entity_id:
        ref = priority_refs.get(str(entity_id))
    if ref is not None:
        return {"kind": "item", "ref": ref}
    return {"kind": "note", "text": clip(data.get("title")), "color": "yellow"}


def migrate_node(node: dict, priority_refs: dict[str, Optional[dict]]) -> dict:
    """One v1 React Flow node as a v2 node."""
    data = node.get("data") or {}
    position = node.get("position") or {}
    w, h = v1_size(node)
    z = int(_num(node.get("zIndex"), 0))
    v1_type = node.get("type")
    if v1_type == "titleNode":
        fields = {"kind": "text", "text": clip(data.get("label")), "size": "lg"}
    elif v1_type == "squareNode":
        fields = {"kind": "frame", "title": "", "w": w or 300.0, "h": h or 300.0, "z": FRAME_Z}
        w = h = None
    elif v1_type == "entity":
        fields = _entity_fields(data, priority_refs)
    else:
        color = COLOR_MAP.get(str(data.get("color") or "").lower(), "yellow")
        fields = {"kind": "note", "text": sticky_text(data), "color": color}
    sized = {"w": w, "h": h} if w is not None else {}
    return make_node(node.get("id"), x=position.get("x"), y=position.get("y"), **{"z": z, **sized, **fields})


def migrate_edges(edges: Iterable[dict], node_ids: set[str]) -> list[dict]:
    """v1 edges as links; an edge whose end is missing is dropped."""
    links = []
    for edge in edges:
        source, target = str(edge.get("source")), str(edge.get("target"))
        if source in node_ids and target in node_ids and edge.get("id") is not None:
            label = edge.get("label") or (edge.get("data") or {}).get("label") or ""
            links.append(make_link(edge["id"], source, target, label))
    return links


def migrate_board(doc: dict, priority_refs: dict[str, Optional[dict]]) -> tuple[list[dict], list[dict]]:
    """A v1 board's nodes and edges as v2 nodes and links."""
    nodes = [migrate_node(node, priority_refs) for node in doc.get("nodes") or [] if node.get("id") is not None]
    links = migrate_edges(doc.get("edges") or [], {node["id"] for node in nodes})
    return nodes, links


def board_content(doc: dict, priority_refs: dict[str, Optional[dict]]) -> tuple[list[dict], list[dict]]:
    """A board's nodes and links in v2, whichever schema it is stored in."""
    if is_v2(doc):
        return list(doc.get("nodes") or []), list(doc.get("links") or [])
    return migrate_board(doc, priority_refs)


# --- saving by change (D5, D10) -------------------------------------------------


def count_changes(patch: BoardPatch) -> int:
    return len(patch.upsert_nodes) + len(patch.delete_nodes) + len(patch.upsert_links) + len(patch.delete_links)


def _upsert(rows: list[dict], updates: Iterable[dict]) -> list[dict]:
    """Replace the row with the same id in place, or append it."""
    position = {row["id"]: index for index, row in enumerate(rows)}
    for update in updates:
        if update["id"] in position:
            rows[position[update["id"]]] = update
        else:
            position[update["id"]] = len(rows)
            rows.append(update)
    return rows


def merge_changes(nodes: list[dict], links: list[dict], patch: BoardPatch) -> tuple[list[dict], list[dict]]:
    """Apply a PATCH's changes by id. Deletes win over upserts in the same body;
    a link survives only while both its ends are nodes."""
    node_rows = _upsert(list(nodes), (node.model_dump() for node in patch.upsert_nodes))
    gone_nodes = set(patch.delete_nodes)
    node_rows = [node for node in node_rows if node["id"] not in gone_nodes]
    link_rows = _upsert(list(links), (link.model_dump(by_alias=True) for link in patch.upsert_links))
    gone_links = set(patch.delete_links)
    present = {node["id"] for node in node_rows}
    link_rows = [
        link for link in link_rows
        if link["id"] not in gone_links and link["from"] in present and link["to"] in present
    ]
    return node_rows, link_rows


def check_change_count(patch: BoardPatch) -> None:
    if count_changes(patch) > MAX_CHANGES:
        raise HTTPException(status_code=422, detail="too_many_changes")


def check_size(before: tuple[list, list], after: tuple[list, list]) -> None:
    """Over 500 nodes or 1,000 links is refused, unless the change did not grow
    that list: a board migrated over the limit can still be edited down."""
    for limit, old, new in ((MAX_NODES, before[0], after[0]), (MAX_LINKS, before[1], after[1])):
        if len(new) > limit and len(new) > len(old):
            raise HTTPException(status_code=422, detail="board_too_large")


# --- the list summary -------------------------------------------------------------


def counts(nodes: list[dict], links: list[dict]) -> dict:
    return {
        "notes": sum(1 for node in nodes if node.get("kind") == "note"),
        "links": len(links),
        "items": sum(1 for node in nodes if node.get("kind") == "item"),
        "cards": sum(len(node.get("card_ids") or []) for node in nodes),
    }


def outline(nodes: list[dict]) -> list[dict]:
    """Up to 60 rectangles for a tile's thumbnail: frames first, then the rest,
    each by z then insertion; a node without a size takes its kind's default."""
    ordered = sorted(
        enumerate(nodes),
        key=lambda pair: (0 if pair[1].get("kind") == "frame" else 1, pair[1].get("z") or 0, pair[0]),
    )
    rects = []
    for _, node in ordered[:MAX_OUTLINE]:
        default_w, default_h = DEFAULT_SIZES.get(node.get("kind"), DEFAULT_SIZES["note"])
        rects.append({
            "x": _num(node.get("x")), "y": _num(node.get("y")),
            "w": _num(node.get("w"), default_w), "h": _num(node.get("h"), default_h),
            "kind": node.get("kind"), "color": node.get("color"),
        })
    return rects


# --- card generation --------------------------------------------------------------


def node_card_text(node: dict) -> str:
    """What a node gives the card generator: a note's or text's text, a frame's
    title; an item gives nothing (it is a live reference, not a thought)."""
    if node.get("kind") in ("note", "text"):
        return str(node.get("text") or "")
    if node.get("kind") == "frame":
        return str(node.get("title") or "")
    return ""


def selection_texts(nodes: list[dict], node_ids: list[str]) -> list[str]:
    """The card texts of the selected nodes, in the selection's order."""
    by_id = {node["id"]: node for node in nodes}
    return [node_card_text(by_id[node_id]) for node_id in node_ids if node_id in by_id]
