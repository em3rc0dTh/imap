from pymongo import MongoClient
from .config import MONGO_URI, MONGO_DB, MONGO_COLLECTION

client = MongoClient(MONGO_URI)
db = client[MONGO_DB]
raw_emails_col = db[MONGO_COLLECTION]
processed_emails_col = db["processed_emails"]
email_setup_col = db["email_setups"]
imap_config_col = db["imap_config"]

# raw_emails_col documents structure:
# { "_id": <uid (int)>, "folder": "<folder>", "message_id": "<Message-ID>", "fetched_at": <datetime>, "subject": "...", "from": "...", "pdfs": [ "path1", ... ] }

def is_uid_processed(uid, folder=None):
    """Verifica si un UID ya fue procesado"""
    query = {"_id": uid}
    if folder:
        query["folder"] = folder
    return raw_emails_col.find_one(query) is not None

def mark_uid_processed(uid, metadata: dict):
    """
    Marca un UID como procesado.
    IMPORTANTE: Solo lo marca, NO lo guarda en raw_emails_col
    (raw_emails_col ya debe tener el documento guardado antes)
    """
    # Aquí solo actualizamos un contador o flag en otra colección si es necesario
    # Ya no hacemos replace_one porque eso puede sobrescribir con datos vacíos
    
    # Opcionalmente, podemos guardar en una colección separada de "processed_uids"
    processed_col = db["processed_uids"]
    processed_col.update_one(
        {"_id": uid},
        {"$set": {"processed_at": __import__("datetime").datetime.utcnow()}},
        upsert=True
    )