"""TTS support services: language segmentation and audio stitching.

Both modules in this package (`segmentation.py`, `audio_stitching.py`) are
pure — no FastAPI, Mongo, or HTTP imports — so they are unit-testable in
total isolation and safely importable from any runtime context (a router,
a script, a background job) without pulling in web-framework state.
"""
from __future__ import annotations
