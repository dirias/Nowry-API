import os
from motor.motor_asyncio import AsyncIOMotorClient

# --- Load MongoDB URI and DB name from environment ---
MONGO_DB = os.getenv("MONGO_DB", "mydb")

# Use single URI for both Local (Docker) and Production
MONGO_URI = os.getenv("MONGO_URI", "mongodb://root:example@mongodb:27017/")

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
