import os
from motor.motor_asyncio import AsyncIOMotorClient

# --- Load MongoDB URI and DB name from environment ---
MONGO_USER = os.getenv("MONGO_USER", "root")
MONGO_PASS = os.getenv("MONGO_PASS", "example")
MONGO_HOST = os.getenv("MONGO_HOST", "mongodb")
MONGO_PORT = os.getenv("MONGO_PORT", "27017")
MONGO_DB = os.getenv("MONGO_DB", "mydb")

MONGO_URI = f"mongodb://{MONGO_USER}:{MONGO_PASS}@{MONGO_HOST}:{MONGO_PORT}/"

# --- Create client ---
mongo_client = AsyncIOMotorClient(MONGO_URI)
db = mongo_client[MONGO_DB]

# --- Collections ---
users_collection = db["users"]
books_collection = db["books"]
book_pages_collection = db["book_pages"]
decks_collection = db["decks"]
cards_collection = db["cards"]
study_cards_collection = db["cards"]  # Alias for cards collection
tasks_collection = db["tasks"]
bugs_collection = db["bugs"]


async def create_indexes():
    # User indexes
    await users_collection.create_index("email", unique=True)
    await users_collection.create_index("username", unique=True)

    # Data indexes for performance
    await books_collection.create_index("user_id")
    await book_pages_collection.create_index("book_id")
    await decks_collection.create_index("user_id")
    await cards_collection.create_index("deck_id")
    await tasks_collection.create_index("user_id")
    await tasks_collection.create_index("status")

    print("Database indexes created successfully.")
