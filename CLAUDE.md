# Nowry API — Claude Code Configuration

> **Parent context:** See `/Nowry/CLAUDE.md` for full project overview and architecture.

## This Package

FastAPI backend serving the Nowry React frontend. Python + MongoDB (Motor async driver).

```bash
uvicorn app.main:app --reload   # dev server — http://localhost:8000
```

## Stack

- **FastAPI** + **Pydantic v2** (strict validation)
- **Motor** (async MongoDB driver)
- **Firebase Admin SDK** — verifies ID tokens for auth
- **Python 3.11+**

## Rules

1. **Pydantic v2 models everywhere** — request AND response models are mandatory
2. **Firebase auth injection** — every protected route must dependency-inject the verified Firebase user
3. **Bounded queries** — always `.find().to_list(length=N)` — never unbounded
4. **PEP8** — explicit type hints on every function signature
5. **No placeholder code** — no `# TODO: implement`, write functional code
6. **STRICT ISOLATION** — never reference `/nowry/` frontend paths

## Folder Structure

```
app/
├── main.py           # FastAPI app entrypoint
├── routers/          # Route handlers (cards, decks, books, users, etc.)
├── models/           # Pydantic v2 models (request + response)
├── services/         # Business logic
├── db/               # MongoDB connection + collection helpers
└── dependencies/     # Auth injection, shared dependencies
```

## Auth Pattern

```python
from app.dependencies.auth import get_current_user

@router.get("/resource")
async def get_resource(user = Depends(get_current_user)):
    # user.uid is the Firebase UID
    ...
```

## MongoDB Query Pattern

```python
# ✅ Always bounded
docs = await collection.find({"user_id": user.uid}).to_list(length=100)

# ❌ Never unbounded
docs = await collection.find({"user_id": user.uid}).to_list(None)
```

## Response Model Pattern

```python
class DeckResponse(BaseModel):
    id: str
    name: str
    created_at: datetime

@router.get("/decks/{deck_id}", response_model=DeckResponse)
async def get_deck(deck_id: str, user=Depends(get_current_user)) -> DeckResponse:
    ...
```
