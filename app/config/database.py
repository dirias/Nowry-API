from motor.motor_asyncio import AsyncIOMotorClient

# MongoDB configuration
MONGO_URI = "mongodb://mongodb:27017"
mongo_client = AsyncIOMotorClient(MONGO_URI)
db = mongo_client["mydb"]
users_collection = db["users"]
books_collection = db["books"]
book_pages_collection = db["book_pages"]
