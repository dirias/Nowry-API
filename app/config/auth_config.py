# app/config/auth_config.py
import os
from dotenv import load_dotenv

load_dotenv()

# Use a fixed secret key from environment or a consistent fallback for development
SECRET_KEY = os.getenv("SECRET_KEY", "nowry_super_secret_key_12345")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30
