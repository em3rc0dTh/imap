# from pymongo import MongoClient
# from .config import MONGO_URI, MONGO_DB, MONGO_COLLECTION

# client = MongoClient(MONGO_URI)
# db = client[MONGO_DB]
# collection = db[MONGO_COLLECTION]

# def insert_many(records):
#     if not records:
#         return 0
#     res = collection.insert_many(records)
#     return len(res.inserted_ids)

# def clear_collection():
#     collection.delete_many({})

# def find(limit=100):
#     return list(collection.find({}, {"_id": 0}).limit(limit))

from pymongo import MongoClient
from .config import MONGO_URI, MONGO_DB, MONGO_COLLECTION

client = MongoClient(MONGO_URI)
db = client[MONGO_DB]
processed_col = db[MONGO_COLLECTION]
email_setup_col = db["email_setups"]

# processed_col documents structure:
# { "_id": <uid (int)>, "folder": "<folder>", "message_id": "<Message-ID>", "fetched_at": <datetime>, "subject": "...", "from": "...", "pdfs": [ "path1", ... ] }

def is_uid_processed(uid, folder=None):
    query = {"_id": uid}
    if folder:
        query["folder"] = folder
    return processed_col.find_one(query) is not None

def mark_uid_processed(uid, metadata: dict):
    # store uid as _id to ensure uniqueness
    doc = {"_id": uid}
    doc.update(metadata)
    # upsert
    processed_col.replace_one({"_id": uid}, doc, upsert=True)
