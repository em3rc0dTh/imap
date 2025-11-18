from pymongo import MongoClient
from .config import MONGO_URI, MONGO_DB, MONGO_COLLECTION

client = MongoClient(MONGO_URI)
db = client[MONGO_DB]
collection = db[MONGO_COLLECTION]

def insert_many(records):
    if not records:
        return 0
    res = collection.insert_many(records)
    return len(res.inserted_ids)

def clear_collection():
    collection.delete_many({})

def find(limit=100):
    return list(collection.find({}, {"_id": 0}).limit(limit))
