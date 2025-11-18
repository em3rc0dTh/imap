import os
from dotenv import load_dotenv

load_dotenv()

IMAP_USER = os.getenv("IMAP_USER")
IMAP_PASS = os.getenv("IMAP_PASS")
IMAP_HOST = os.getenv("IMAP_HOST", "imap.gmail.com")
IMAP_FOLDER = os.getenv("IMAP_FOLDER", "INBOX")
IMAP_SENDER_FILTER = os.getenv("IMAP_SENDER_FILTER", "")

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "finanzas")
MONGO_COLLECTION = os.getenv("MONGO_COLLECTION", "movimientos")

OUTPUT_DIR = os.getenv("OUTPUT_DIR", "output")
PDF_SAVE_DIR = os.path.join(OUTPUT_DIR, "pdfs")
JSON_OUTPUT = os.path.join(OUTPUT_DIR, "movimientos.json")

TESSERACT_CMD = os.getenv("TESSERACT_CMD", "/usr/bin/tesseract")

OLLAMA_ENABLED = os.getenv("OLLAMA_ENABLED", "false").lower() == "true"
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1")

EMBED_MODEL = os.getenv("EMBED_MODEL", "all-MiniLM-L6-v2")
