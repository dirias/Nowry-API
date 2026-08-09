"""Raw MP3 concatenation for multi-segment TTS output.

`concatenate_mp3_segments()` joins per-segment MP3 byte strings (each
produced by one `synthesize_speech()` call) into a single MP3 stream by raw
byte concatenation — no re-encode, no ffmpeg dependency, matching v1 scope
(`docs/architecture.md` Component Breakdown).

This is intentionally simple: MPEG audio frames are self-delimiting, and
Google Cloud TTS's `audio_content` output for `AudioEncoding.MP3` is a bare
frame stream (no ID3 container, no Xing/VBR header), so byte-level
concatenation of complete `synthesize_speech()` outputs produces a valid,
playable combined MP3 in the overwhelming majority of players. This module
does not parse or validate MP3 frame headers — if a future audio source
introduces container metadata (ID3 tags, etc.) into individual chunks, that
would need frame-aware stitching, which is out of scope for v1.
"""
from __future__ import annotations


def concatenate_mp3_segments(chunks: list[bytes]) -> bytes:
    """Concatenate MP3 byte chunks, in order, into one MP3 byte stream.

    Args:
        chunks: Ordered list of complete MP3 byte strings, one per segment.

    Returns:
        The concatenated MP3 bytes.

    Raises:
        ValueError: If `chunks` is empty.
    """
    if not chunks:
        raise ValueError("chunks must not be empty.")
    return b"".join(chunks)
