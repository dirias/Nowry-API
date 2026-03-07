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
    user_id: str
    name: str = "My Blackboard"
    nodes: List[Dict[str, Any]] = []
    edges: List[Dict[str, Any]] = []
    viewport: Optional[Dict[str, float]] = {"x": 0, "y": 0, "zoom": 1}
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class BlackboardUpdate(BaseModel):
    name: Optional[str] = None
    nodes: Optional[List[Dict[str, Any]]] = None
    edges: Optional[List[Dict[str, Any]]] = None
    viewport: Optional[Dict[str, float]] = None
