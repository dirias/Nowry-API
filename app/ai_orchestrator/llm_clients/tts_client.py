"""
Google Cloud Text-to-Speech client factory.

Authentication priority:
1. GOOGLE_TTS_API_KEY env var (simple API-key auth via ClientOptions — no
   service-account file/JSON needed; requires an "unrestricted" or
   texttospeech-scoped API key from Google Cloud Console)
2. GOOGLE_TTS_CREDENTIALS_JSON env var (inline service account JSON string)
3. GOOGLE_APPLICATION_CREDENTIALS env var (path to service account JSON file)
   — NOTE: this only works if that path exists on disk at runtime. Railway's
   filesystem is ephemeral and nothing in this repo's entrypoint.sh/Procfile
   writes a credentials file at boot (contrast with FIREBASE_SERVICE_ACCOUNT_JSON
   in app/config/firebase_config.py, which reads inline JSON directly — no file
   needed). If GOOGLE_APPLICATION_CREDENTIALS is set to a path that doesn't
   exist in the deployed container, this step — and the ADC fallback below —
   will fail with a credentials error.
4. Application Default Credentials (gcloud CLI or GCE metadata server)
"""
from __future__ import annotations

import json
import os

from google.api_core.client_options import ClientOptions
from google.cloud import texttospeech
from google.oauth2 import service_account


def get_tts_client() -> texttospeech.TextToSpeechClient:
    """Return a TextToSpeechClient authenticated via API key, service account, or ADC."""
    api_key = os.getenv("GOOGLE_TTS_API_KEY")
    if api_key:
        return texttospeech.TextToSpeechClient(
            client_options=ClientOptions(api_key=api_key)
        )

    creds_json = os.getenv("GOOGLE_TTS_CREDENTIALS_JSON")
    if creds_json:
        info = json.loads(creds_json)
        creds = service_account.Credentials.from_service_account_info(
            info,
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
        return texttospeech.TextToSpeechClient(credentials=creds)

    # Falls back to GOOGLE_APPLICATION_CREDENTIALS env var (file path — only
    # works if the file actually exists in this environment) or ADC.
    return texttospeech.TextToSpeechClient()
