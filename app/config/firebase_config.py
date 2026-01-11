"""
Firebase Authentication Configuration
Handles Firebase Admin SDK setup for token validation
"""

import os
import firebase_admin
from firebase_admin import credentials, auth as firebase_auth
from dotenv import load_dotenv

load_dotenv()

# Initialize Firebase Admin SDK
def initialize_firebase():
    """Initialize Firebase Admin SDK with service account or default credentials"""
    try:
        # Check if already initialized
        firebase_admin.get_app()
        print("✅ Firebase Admin SDK already initialized")
    except ValueError:
        # Not initialized yet
        
        # Priority 1: Check for JSON content in ENV (Base64 or Raw String)
        # This is best for production (Heroku, Vercel, Docker, etc.)
        firebase_service_json = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON")
        firebase_service_path = os.getenv("FIREBASE_SERVICE_ACCOUNT_PATH")

        if firebase_service_json:
            import json
            import base64
            
            try:
                # Try decoding base64 first
                try:
                    decoded_json = base64.b64decode(firebase_service_json).decode('utf-8')
                    service_account_info = json.loads(decoded_json)
                except Exception:
                    # If not base64, try loading as raw JSON string
                    service_account_info = json.loads(firebase_service_json)
                
                cred = credentials.Certificate(service_account_info)
                firebase_admin.initialize_app(cred)
                print("✅ Firebase Admin SDK initialized with ENV JSON content")
                return
            except Exception as e:
                print(f"⚠️ Failed to load FIREBASE_SERVICE_ACCOUNT_JSON: {e}")

        # Priority 2: Check for file path
        if firebase_service_path and os.path.exists(firebase_service_path):
            cred = credentials.Certificate(firebase_service_path)
            firebase_admin.initialize_app(cred)
            print(f"✅ Firebase Admin SDK initialized with service account file: {firebase_service_path}")
        else:
            # Priority 3: Default Credentials (useful for local dev with gcloud CLI)
            firebase_admin.initialize_app()
            print("⚠️  Firebase Admin SDK initialized with default credentials")

# Initialize on module import
initialize_firebase()

def verify_firebase_token(id_token: str) -> dict:
    """
    Verify Firebase ID token and return decoded claims
    
    Args:
        id_token: Firebase ID token from client
        
    Returns:
        dict: Decoded token with user claims (uid, email, etc.)
        
    Raises:
        firebase_admin.auth.InvalidIdTokenError: If token is invalid
        firebase_admin.auth.ExpiredIdTokenError: If token is expired
    """
    try:
        decoded_token = firebase_auth.verify_id_token(id_token)
        return decoded_token
    except Exception as e:
        print(f"❌ Firebase token verification failed: {e}")
        raise
