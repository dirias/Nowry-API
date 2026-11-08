"""Reading an upload without trusting it (QA-019, MEDIA-004).

No route reads an upload unbounded: the body is read in chunks and refused
with 413 the moment it passes the cap, so a file past the limit never sits in
memory whole. The sniffer reads an image's format off its first bytes, so a
declared `image/png` that is not a PNG is refused as what it is.
"""
from typing import Optional

from fastapi import HTTPException, UploadFile

READ_CHUNK: int = 1024 * 1024

#: The formats the editor accepts, by the bytes a file of that format starts with.
_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpg"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
)


async def read_bounded_upload(file: UploadFile, max_bytes: int) -> bytes:
    """The upload's bytes, or 413 as soon as they pass `max_bytes`."""
    chunks: list = []
    total = 0
    while True:
        chunk = await file.read(READ_CHUNK)
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"File exceeds the {max_bytes // (1024 * 1024)} MB limit.",
            )
        chunks.append(chunk)


def sniff_image_format(head: bytes) -> Optional[str]:
    """The image format the first bytes declare — png, jpg, gif or webp — or None."""
    for magic, name in _MAGIC:
        if head.startswith(magic):
            return name
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return None
