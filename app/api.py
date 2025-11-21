from pydantic import BaseModel
from .db import email_setup_col, imap_config_col
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from .ingest_email import connect_and_download_pdfs

app = FastAPI()

# === CORS ===
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],        # Permitir cualquier frontend (localhost)
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Aquí guardaremos temporalmente los emails procesados
PROCESSED_EMAILS = []

def normalize(email_item):
    return {
        "uid": email_item.get("uid"),
        "from": email_item.get("from"),
        "subject": email_item.get("subject"),
        "date": email_item.get("date"),
        "attachments": email_item.get("pdfs", []),
        "body": email_item.get("html_body") or email_item.get("text_body") or "",
        "text_body": email_item.get("text_body") or "",
    }

class EmailSetup(BaseModel):
    alias: str | None = None
    bank_name: str
    service_type: str
    bank_sender: str

@app.post("/email/setup")
def save_email_setup(setup: EmailSetup):
    # Insertar en MongoDB
    result = email_setup_col.insert_one(setup.dict())
    return {"status": "success", "id": str(result.inserted_id)}

@app.get("/email/setup")
def get_email_setups():
    setups = list(email_setup_col.find({}, {"_id": 0}))  # Omitir _id si quieres
    return setups

class ImapConfig(BaseModel):
    user: str
    password: str
@app.post("/imap/config")
def save_imap_config(config: ImapConfig):
    imap_config_col.delete_many({})               # siempre borramos lo anterior
    imap_config_col.insert_one(config.dict())     # guardamos el único documento
    return {"status": "success"}

@app.get("/imap/config")
def get_imap_config():
    data = imap_config_col.find_one({}, {"_id": 0})
    return data or {}  
    

@app.get("/ingest")
def ingest(
    limit: int | None = Query(default=None),
    force: bool = Query(default=False)
):
    global PROCESSED_EMAILS

    raw = connect_and_download_pdfs(limit=limit, force=force)
    print(f"Procesados: {len(raw)} mensajes")
    PROCESSED_EMAILS = [normalize(e) for e in raw]

    return {
        "count": len(PROCESSED_EMAILS),
        "emails": PROCESSED_EMAILS
    }

from .db import processed_col

@app.get("/emails")
def get_emails():
    emails = list(processed_col.find())
    return [normalize(e) for e in emails]