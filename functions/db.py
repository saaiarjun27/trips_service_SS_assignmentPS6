# In app/db.py
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel
import os

MONGO_URL = os.getenv("MONGO_URL", "mongodb://localhost:27017")
MONGO_DB_NAME = os.getenv("MONGO_DB_NAME", "trip_service_db")

class Database:
    client: AsyncIOMotorClient = None

db = Database()

async def connect_to_mongo():
    db.client = AsyncIOMotorClient(MONGO_URL)
    database = db.client[MONGO_DB_NAME]
    await database.command('ping')
    trips_collection = database["trips"]
    await trips_collection.create_index("trip_id", unique=True)
    return database

async def close_mongo_connection():
    if db.client:
        db.client.close()

def get_database() -> AsyncIOMotorClient:
    if db.client is None:
        raise Exception("Database client is not initialized.")
    return db.client[MONGO_DB_NAME]

def get_trip_collection():
    return get_database()["trips"]