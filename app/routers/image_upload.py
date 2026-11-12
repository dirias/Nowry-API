"""The editor's image routes (ADR-062).

A picture belongs to a document: the upload names a book the caller owns, is
stored under `{root}/{user}/{book}/` and written to the media ledger, which is
how the book's deletion and the account's deletion later find it.

The statuses say what happened (ADR-062 point 7): 404 when the book is not the
caller's, 402 `image_limit_reached` when the plan's month is spent, 413 past
the cap — refused before the body is read whole — 415 for anything that is not
an image by its declared type or its first bytes, and 502 when storage fails,
without the backend's own message.
"""
import os
from typing import Optional

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from app.auth.dependencies import get_access
from app.auth.firebase_auth import get_firebase_user
from app.config.database import books_collection
from app.config.entitlements import Access, access_limit
from app.models.common import MessageResponse
from app.utils import media_ledger
from app.utils.logger import get_logger
from app.utils.storage import get_storage_backend, media_root
from app.utils.uploads import read_bounded_upload, sniff_image_format

logger = get_logger(__name__)

router = APIRouter(
    prefix="/image",
    tags=["images"],
    responses={404: {"description": "Not found"}},
)

# Allowed file types
ALLOWED_EXTENSIONS = {'jpg', 'jpeg', 'png', 'gif', 'webp'}
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB

#: The one detail a storage failure reports; the backend's message stays in the log.
STORAGE_UNAVAILABLE = "image_storage_unavailable"
#: The plan refusal's code, translated by the clients.
LIMIT_REACHED = "image_limit_reached"


class ImageUploadResponse(BaseModel):
    """What the editor keeps of a stored image."""
    url: str
    thumbnail_url: str
    public_id: str
    width: Optional[int] = None
    height: Optional[int] = None
    size: Optional[int] = None
    format: Optional[str] = None


def validate_image_file(file: UploadFile) -> None:
    """415 unless the upload declares itself an image of an accepted kind."""
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(status_code=415, detail="Invalid MIME type. Must be an image.")
    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename provided")
    ext = file.filename.rsplit('.', 1)[-1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=415,
            detail=f"Invalid file type. Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}"
        )


def require_image_bytes(content: bytes) -> str:
    """The format the bytes really are, or 415: a declared type is a claim, the bytes are the fact."""
    sniffed = sniff_image_format(content[:16])
    if sniffed is None:
        raise HTTPException(status_code=415, detail="The file is not an image.")
    return sniffed


async def require_own_book(book_id: str, user_id: str) -> dict:
    """The caller's live book by id, or 404 — a missing book and another user's look the same."""
    try:
        oid = ObjectId(book_id)
    except (InvalidId, TypeError):
        raise HTTPException(status_code=404, detail="Book not found")
    book = await books_collection.find_one(
        {"_id": oid, "user_id": user_id, "deleted_at": None}, {"_id": 1}
    )
    if not book:
        raise HTTPException(status_code=404, detail="Book not found")
    return book


async def require_image_quota(user_id: str, access: Access) -> None:
    """402 when the account has stored as many images this month as its plan allows."""
    limit = access_limit(access, "images_per_month")
    if limit < 0:
        return
    if await media_ledger.count_this_month(user_id) >= limit:
        raise HTTPException(status_code=402, detail=LIMIT_REACHED)


def _storage():
    return get_storage_backend(os.getenv("STORAGE_BACKEND", "cloudinary"))


async def _store(file_content: bytes, filename: str, folder: str) -> tuple:
    """Upload through the backend; a failure is logged and reported as 502 with a fixed detail."""
    try:
        storage = _storage()
        result = await storage.upload(
            file_content=file_content,
            filename=filename,
            folder=folder,
            quality="auto",
            fetch_format="auto",  # Auto WebP conversion
        )
        thumbnail_url = storage.get_thumbnail_url(public_id=result['public_id'], width=200, height=200)
    except Exception:
        logger.error("Image storage failed for folder %s", folder, exc_info=True)
        raise HTTPException(status_code=502, detail=STORAGE_UNAVAILABLE)
    return result, thumbnail_url


@router.post("/upload", summary="Upload an image into a document", response_model=ImageUploadResponse)
async def upload_image(
    file: UploadFile = File(...),
    book_id: str = Form(...),
    current_user: dict = Depends(get_firebase_user),
    access: Access = Depends(get_access),
) -> ImageUploadResponse:
    """Store an image under the caller's book and record it in the media ledger."""
    user_id = current_user.get("user_id")
    validate_image_file(file)
    await require_own_book(book_id, user_id)
    await require_image_quota(user_id, access)
    file_content = await read_bounded_upload(file, MAX_FILE_SIZE)
    require_image_bytes(file_content)

    # The document is the folder (ADR-062 point 2): {root}/{user_id}/{book_id}/
    result, thumbnail_url = await _store(file_content, file.filename, f"{media_root()}/{user_id}/{book_id}")

    await media_ledger.record(
        user_id=user_id,
        book_id=book_id,
        public_id=result['public_id'],
        url=result['secure_url'],
        bytes_=result.get('bytes'),
        format_=result.get('format'),
        width=result.get('width'),
        height=result.get('height'),
    )
    return ImageUploadResponse(
        url=result['secure_url'],
        thumbnail_url=thumbnail_url,
        public_id=result['public_id'],
        width=result.get('width'),
        height=result.get('height'),
        size=result.get('bytes'),
        format=result.get('format'),
    )


@router.delete("/delete/{public_id:path}", summary="Delete an image", response_model=MessageResponse)
async def delete_image(
    public_id: str,
    current_user: dict = Depends(get_firebase_user),
) -> MessageResponse:
    """Delete an image from storage and drop its ledger row."""
    user_id = current_user.get("user_id")

    # Security Fix: Prevent IDOR by asserting the image being deleted belongs solely to the mapped User ID folder
    if not public_id.startswith(f"{media_root()}/{user_id}/"):
        raise HTTPException(
            status_code=403, detail="Not authorized to delete this image."
        )

    try:
        success = await _storage().delete(public_id)
    except Exception:
        logger.error("Image deletion failed for %s", public_id, exc_info=True)
        raise HTTPException(status_code=502, detail=STORAGE_UNAVAILABLE)

    row = await media_ledger.row_for_public_id(user_id, public_id)
    if row:
        await media_ledger.remove_rows([row["_id"]])
    if not success:
        raise HTTPException(status_code=404, detail="Image not found")
    return MessageResponse(message="Image deleted successfully")
