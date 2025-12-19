import os
from dotenv import load_dotenv
from pathlib import Path
load_dotenv()

IMAP_USER = os.getenv("IMAP_USER")
IMAP_PASS = os.getenv("IMAP_PASS")
IMAP_HOST = os.getenv("IMAP_HOST", "imap.gmail.com")
IMAP_PORT = int(os.getenv("IMAP_PORT", "993"))
IMAP_FOLDER = os.getenv("IMAP_FOLDER", "INBOX")
IMAP_SENDER_FILTER = os.getenv("IMAP_SENDER_FILTER", "")
# Comma-separated list of subjects substrings (optional)
IMAP_SUBJECT_FILTER = os.getenv("IMAP_SUBJECT_FILTER", "").strip()
# Date filter: format YYYY-MM-DD (optional)
IMAP_DATE_FROM = os.getenv("IMAP_DATE_FROM", "").strip()
# 0 or empty -> no limit (bring all)
IMAP_LIMIT = int(os.getenv("IMAP_LIMIT", "0") or 0)
# Only fetch messages that have attachments (useful to skip plain notifications)
IMAP_ONLY_WITH_ATTACHMENTS = os.getenv("IMAP_ONLY_WITH_ATTACHMENTS", "false").lower() in ("1","true","yes")

# Paths
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "output"))
PDF_SAVE_DIR = OUTPUT_DIR / "pdfs"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
PDF_SAVE_DIR.mkdir(parents=True, exist_ok=True)

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "finanzas")
MONGO_COLLECTION = os.getenv("MONGO_COLLECTION", "movimientos")
MONGO_EMAIL_SETUP_COLLECTION = os.getenv("MONGO_EMAIL_SETUP_COLLECTION", "email_setups")

OUTPUT_DIR = os.getenv("OUTPUT_DIR", "output")
PDF_SAVE_DIR = os.path.join(OUTPUT_DIR, "pdfs")
JSON_OUTPUT = os.path.join(OUTPUT_DIR, "movimientos.json")

TESSERACT_CMD = os.getenv("TESSERACT_CMD", "/usr/bin/tesseract")

OLLAMA_ENABLED = os.getenv("OLLAMA_ENABLED", "false").lower() == "true"
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1")

EMBED_MODEL = os.getenv("EMBED_MODEL", "all-MiniLM-L6-v2")

# Behavior
MOVE_PROCESSED_TO_FOLDER = os.getenv("MOVE_PROCESSED_TO_FOLDER", "").strip()  # e.g. "Processed"
MARK_AS_SEEN = os.getenv("MARK_AS_SEEN", "true").lower() in ("1","true","yes")

# ============================================================================
# 🆕 FASE 4 - IMAP LIFECYCLE CONFIGURATION
# ============================================================================

# Intervalo de polling en segundos (cada cuánto revisar emails nuevos)
IMAP_POLL_INTERVAL = int(os.getenv("IMAP_POLL_INTERVAL", "60"))  # 60 segundos por defecto

# Backoff para reconexión (segundos base para exponential backoff)
IMAP_RECONNECT_BACKOFF = int(os.getenv("IMAP_RECONNECT_BACKOFF", "5"))  # 5 segundos

# Máximo de reintentos antes de abortar
IMAP_MAX_RETRIES = int(os.getenv("IMAP_MAX_RETRIES", "5"))  # 5 reintentos

# Habilitar modo persistente (loop infinito)
IMAP_PERSISTENT_MODE = os.getenv("IMAP_PERSISTENT_MODE", "false").lower() in ("1", "true", "yes")