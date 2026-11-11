"""
Storage abstraction layer for image uploads.
Supports Cloudinary with easy migration to S3.
"""
from abc import ABC, abstractmethod
from typing import Optional, Dict, Any, List
import os

#: The folder every user upload and the delete guard live under (MEDIA-001,
#: ADR-062 point 3). Read at call time so each environment — and each test —
#: gets its own root; the companion's generated assets keep `nowry/`.
DEFAULT_MEDIA_ROOT = "nowry"


def media_root() -> str:
    """The environment's root folder for user uploads, `nowry` unless `MEDIA_ROOT_FOLDER` says otherwise."""
    return os.getenv("MEDIA_ROOT_FOLDER", "").strip().strip("/") or DEFAULT_MEDIA_ROOT


class StorageBackend(ABC):
    """Abstract base class for storage backends"""
    
    @abstractmethod
    async def upload(
        self, 
        file_content: bytes, 
        filename: str,
        folder: str = "",
        **options
    ) -> Dict[str, Any]:
        """
        Upload file to storage
        
        Returns:
            {
                'url': str,           # Public URL
                'secure_url': str,    # HTTPS URL
                'public_id': str,     # Storage identifier
                'width': int,         # Image width
                'height': int,        # Image height
                'format': str,        # File format (jpg, png, etc)
                'bytes': int          # File size
            }
        """
        pass
    
    @abstractmethod
    async def delete(self, public_id: str) -> bool:
        """Delete file from storage"""
        pass

    async def delete_many(self, public_ids: List[str]) -> int:
        """Delete several files; returns how many storage confirmed gone.

        The default deletes one by one; a backend with a bulk call overrides it.
        """
        removed = 0
        for public_id in public_ids:
            if await self.delete(public_id):
                removed += 1
        return removed
    
    @abstractmethod
    def get_thumbnail_url(self, public_id: str, width: int = 200, height: int = 200) -> str:
        """Generate thumbnail URL"""
        pass


class CloudinaryStorage(StorageBackend):
    """Cloudinary storage implementation"""
    
    def __init__(self):
        import cloudinary
        import cloudinary.uploader
        
        cloudinary.config(
            cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
            api_key=os.getenv("CLOUDINARY_API_KEY"),
            api_secret=os.getenv("CLOUDINARY_API_SECRET")
        )
        self.cloudinary = cloudinary
    
    async def upload(
        self,
        file_content: bytes,
        filename: str,
        folder: str = "",
        **options
    ) -> Dict[str, Any]:
        """Upload to Cloudinary — runs the synchronous SDK call in a thread executor
        so it does not block the asyncio event loop."""
        import asyncio
        import functools

        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            functools.partial(
                self.cloudinary.uploader.upload,
                file_content,
                folder=folder,
                resource_type="image",
                **options,
            ),
        )

        return {
            'url': result.get('url'),
            'secure_url': result.get('secure_url'),
            'public_id': result.get('public_id'),
            'width': result.get('width'),
            'height': result.get('height'),
            'format': result.get('format'),
            'bytes': result.get('bytes')
        }

    async def upload_video(
        self,
        file_content: bytes,
        folder: str = "",
        **options
    ) -> Dict[str, Any]:
        """Upload a video to Cloudinary — runs in a thread executor."""
        import asyncio
        import functools

        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            functools.partial(
                self.cloudinary.uploader.upload,
                file_content,
                folder=folder,
                resource_type="video",
                **options,
            ),
        )

        return {
            'url': result.get('url'),
            'secure_url': result.get('secure_url'),
            'public_id': result.get('public_id'),
            'width': result.get('width'),
            'height': result.get('height'),
            'format': result.get('format'),
            'bytes': result.get('bytes')
        }

    async def delete(self, public_id: str) -> bool:
        """Delete from Cloudinary"""
        import asyncio
        import functools

        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            functools.partial(self.cloudinary.uploader.destroy, public_id),
        )
        return result.get('result') == 'ok'

    #: Cloudinary's Admin API takes at most 100 public ids per call.
    DELETE_BATCH = 100

    async def delete_many(self, public_ids: List[str]) -> int:
        """Delete in batches through the Admin API — in a thread executor, like the rest."""
        import asyncio
        import functools
        import cloudinary.api

        loop = asyncio.get_event_loop()
        removed = 0
        for start in range(0, len(public_ids), self.DELETE_BATCH):
            batch = public_ids[start:start + self.DELETE_BATCH]
            result = await loop.run_in_executor(
                None,
                # `invalidate`: a deleted picture leaves the CDN's cache too, not only the bucket.
                functools.partial(cloudinary.api.delete_resources, batch, resource_type="image", invalidate=True),
            )
            # "not_found" is gone as well: an earlier attempt removed it and lost the answer.
            removed += sum(1 for state in (result.get("deleted") or {}).values() if state in ("deleted", "not_found"))
        return removed
    
    def get_thumbnail_url(self, public_id: str, width: int = 200, height: int = 200) -> str:
        """Generate Cloudinary transformation URL"""
        from cloudinary import CloudinaryImage
        return CloudinaryImage(public_id).build_url(
            width=width,
            height=height,
            crop="fit",
            quality="auto",
            fetch_format="auto"
        )


class S3Storage(StorageBackend):
    """
    S3 storage implementation (future migration target)
    Currently not implemented - placeholder for migration
    """
    
    def __init__(self):
        raise NotImplementedError("S3 storage not yet implemented")
    
    async def upload(self, file_content: bytes, filename: str, folder: str = "", **options):
        raise NotImplementedError()
    
    async def delete(self, public_id: str) -> bool:
        raise NotImplementedError()
    
    def get_thumbnail_url(self, public_id: str, width: int = 200, height: int = 200) -> str:
        raise NotImplementedError()


# Factory function for easy backend switching
def get_storage_backend(backend: str = "cloudinary") -> StorageBackend:
    """
    Get storage backend instance
    
    Args:
        backend: "cloudinary" or "s3"
    
    Returns:
        StorageBackend instance
    """
    if backend == "cloudinary":
        return CloudinaryStorage()
    elif backend == "s3":
        return S3Storage()
    else:
        raise ValueError(f"Unknown storage backend: {backend}")
