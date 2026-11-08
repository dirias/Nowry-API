"""The editor's image routes (ADR-062).

A picture belongs to a document: the upload names a book the caller owns, is
stored under `{root}/{user}/{book}/` and written to the media ledger, which is
how the book's deletion and the account's deletion later find it.
"""
import os
from typing import Optional

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from app.auth.firebase_auth import get_firebase_user
from app.config.database import books_collection
from app.models.common import MessageResponse
from app.utils import media_ledger
from app.utils.logger import get_logger
from app.utils.storage import get_storage_backend, media_root

logger = get_logger(__name__)

router = APIRouter(
    prefix="/image",
    tags=["images"],
    responses={404: {"description": "Not found"}},
)

# Allowed file types
ALLOWED_EXTENSIONS = {'jpg', 'jpeg', 'png', 'gif', 'webp'}
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB


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
    """Validate image file type and size"""
    # Check MIME type
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Invalid MIME type. Must be an image.")

    # Check file extension
    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename provided")

    ext = file.filename.rsplit('.', 1)[-1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid file type. Allowed: {', '.join(ALLOWED_EXTENSIONS)}"
        )


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


def _storage():
    return get_storage_backend(os.getenv("STORAGE_BACKEND", "cloudinary"))


@router.post("/upload", summary="Upload an image into a document", response_model=ImageUploadResponse)
async def upload_image(
    file: UploadFile = File(...),
    book_id: str = Form(...),
    current_user: dict = Depends(get_firebase_user),
) -> ImageUploadResponse:
    """Store an image under the caller's book and record it in the media ledger."""
    user_id = current_user.get("user_id")
    validate_image_file(file)
    await require_own_book(book_id, user_id)

    # Read file content
    file_content = await file.read()

    # Check file size
    if len(file_content) > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=400,
            detail=f"File too large. Maximum size: {MAX_FILE_SIZE / 1024 / 1024}MB"
        )

    try:
        storage = _storage()
        # The document is the folder (ADR-062 point 2): {root}/{user_id}/{book_id}/
        result = await storage.upload(
            file_content=file_content,
            filename=file.filename,
            folder=f"{media_root()}/{user_id}/{book_id}",
            quality="auto",
            fetch_format="auto"  # Auto WebP conversion
        )
        thumbnail_url = storage.get_thumbnail_url(public_id=result['public_id'], width=200, height=200)
    except Exception as e:
        logger.error(f"Error uploading image: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Upload failed: {str(e)}")

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
    except Exception as e:
        logger.error(f"Error deleting image: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Delete failed: {str(e)}")

    row = await media_ledger.row_for_public_id(user_id, public_id)
    if row:
        await media_ledger.remove_rows([row["_id"]])
    if not success:
        raise HTTPException(status_code=404, detail="Image not found")
    return MessageResponse(message="Image deleted successfully")
