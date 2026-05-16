"""
Google Cloud Text-to-Speech client factory.

Authentication priority:
1. GOOGLE_TTS_CREDENTIALS_JSON env var (inline service account JSON string)
2. GOOGLE_APPLICATION_CREDENTIALS env var (path to service account JSON file)
3. Application Default Credentials (gcloud CLI or GCE metadata server)
"""
from __future__ import annotations

import json
import os

from google.cloud import texttospeech
from google.oauth2 import service_account


def get_tts_client() -> texttospeech.TextToSpeechClient:
    """Return a TextToSpeechClient authenticated via service account or ADC."""
    creds_json = os.getenv("GOOGLE_TTS_CREDENTIALS_JSON")
    if creds_json:
        info = json.loads(creds_json)
        creds = service_account.Credentials.from_service_account_info(
            info,
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
        return texttospeech.TextToSpeechClient(credentials=creds)
    # Falls back to GOOGLE_APPLICATION_CREDENTIALS env var or ADC
    return texttospeech.TextToSpeechClient()
