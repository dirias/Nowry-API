from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from app.auth.firebase_auth import get_firebase_user
from app.utils.storage import get_storage_backend
from typing import Dict, Any
import os

router = APIRouter(
    prefix="/image",
    tags=["images"],
    responses={404: {"description": "Not found"}},
)

# Allowed file types
ALLOWED_EXTENSIONS = {'jpg', 'jpeg', 'png', 'gif', 'webp'}
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB

def validate_image_file(file: UploadFile) -> None:
    """Validate image file type and size"""
    # Check file extension
    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename provided")
    
    ext = file.filename.rsplit('.', 1)[-1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid file type. Allowed: {', '.join(ALLOWED_EXTENSIONS)}"
        )


@router.post("/upload", summary="Upload an image")
async def upload_image(
    file: UploadFile = File(...),
    book_id: str = None,
    current_user: dict = Depends(get_firebase_user),
) -> Dict[str, Any]:
    """
    Upload an image to cloud storage
    
    Returns:
        {
            "url": "https://...",
            "thumbnail_url": "https://...",
            "width": 1920,
            "height": 1080,
            "size": 245678
        }
    """
    # Validate file
    validate_image_file(file)
    
    # Read file content
    file_content = await file.read()
    
    # Check file size
    if len(file_content) > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=400,
            detail=f"File too large. Maximum size: {MAX_FILE_SIZE / 1024 / 1024}MB"
        )
    
    # TODO: Check subscription limits
    # user_id = current_user.get("user_id")
    # await check_image_upload_limit(user_id)
    
    try:
        # Get storage backend
        storage = get_storage_backend(os.getenv("STORAGE_BACKEND", "cloudinary"))
        
        # Organize in folders: nowry/{user_id}/{book_id}/
        user_id = current_user.get("user_id")
        folder = f"nowry/{user_id}"
        if book_id:
            folder += f"/{book_id}"
        
        # Upload to storage
        result = await storage.upload(
            file_content=file_content,
            filename=file.filename,
            folder=folder,
            quality="auto",
            fetch_format="auto"  # Auto WebP conversion
        )
        
        # Generate thumbnail URL
        thumbnail_url = storage.get_thumbnail_url(
            public_id=result['public_id'],
            width=200,
            height=200
        )
        
        return {
            "url": result['secure_url'],
            "thumbnail_url": thumbnail_url,
            "width": result['width'],
            "height": result['height'],
            "size": result['bytes'],
            "format": result['format']
        }
        
    except Exception as e:
        print(f"Error uploading image: {e}")
        raise HTTPException(status_code=500, detail=f"Upload failed: {str(e)}")


@router.delete("/delete/{public_id:path}", summary="Delete an image")
async def delete_image(
    public_id: str,
    current_user: dict = Depends(get_firebase_user),
) -> Dict[str, str]:
    """Delete an image from storage"""
    try:
        storage = get_storage_backend(os.getenv("STORAGE_BACKEND", "cloudinary"))
        success = await storage.delete(public_id)
        
        if success:
            return {"message": "Image deleted successfully"}
        else:
            raise HTTPException(status_code=404, detail="Image not found")
            
    except Exception as e:
        print(f"Error deleting image: {e}")
        raise HTTPException(status_code=500, detail=f"Delete failed: {str(e)}")
