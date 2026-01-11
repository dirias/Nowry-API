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
        firebase_service_account = os.getenv("FIREBASE_SERVICE_ACCOUNT_PATH")
        
        if firebase_service_account and os.path.exists(firebase_service_account):
            # Production: Use service account file
            cred = credentials.Certificate(firebase_service_account)
            firebase_admin.initialize_app(cred)
            print(f"✅ Firebase Admin SDK initialized with service account: {firebase_service_account}")
        else:
            # Development: Use default credentials or manual config
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
