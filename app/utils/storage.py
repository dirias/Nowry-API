"""
Storage abstraction layer for image uploads.
Supports Cloudinary with easy migration to S3.
"""
from abc import ABC, abstractmethod
from typing import Optional, Dict, Any
import os


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
        """Upload to Cloudinary"""
        result = self.cloudinary.uploader.upload(
            file_content,
            folder=folder,
            resource_type="image",
            **options
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
        result = self.cloudinary.uploader.destroy(public_id)
        return result.get('result') == 'ok'
    
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
