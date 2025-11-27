import os
import re
import time
from datetime import datetime
from imapclient import IMAPClient, SEEN
import pyzmail
from email.utils import parsedate_to_datetime
from pathlib import Path
from .config import (
    IMAP_HOST, IMAP_PORT, IMAP_USER, IMAP_PASS, IMAP_FOLDER,
    IMAP_SENDER_FILTER, IMAP_SUBJECT_FILTER, IMAP_DATE_FROM,
    IMAP_LIMIT, IMAP_ONLY_WITH_ATTACHMENTS, PDF_SAVE_DIR,
    MOVE_PROCESSED_TO_FOLDER, MARK_AS_SEEN
)
from .db import is_uid_processed, mark_uid_processed
from tqdm import tqdm
from pymongo import MongoClient
from .config import MONGO_URI, MONGO_DB, MONGO_EMAIL_SETUP_COLLECTION
import logging

logger = logging.getLogger(__name__)

client = MongoClient(MONGO_URI)
db = client[MONGO_DB]
email_setup_col = db[MONGO_EMAIL_SETUP_COLLECTION]
imap_config_col = db["imap_config"]

# === Helpers para email setups ===
def get_email_setups():
    """
    Devuelve todos los setups guardados en la BD.
    """
    return list(email_setup_col.find({}, {"_id": 0}))

def get_email_setup_by_sender(sender: str):
    """
    Retorna el setup cuyo bank_sender coincide con sender
    """
    return email_setup_col.find_one({"bank_sender": sender})

# === Helpers para imap config ===
def get_imap_config():
    data = imap_config_col.find_one({}, {"_id": 0})
    return data or {}


PDF_EXT_RE = re.compile(r"\.pdf$", re.IGNORECASE)

def _build_search_criteria(date_from: str = None, date_to: str = None):
    """
    Build IMAP search criteria dict for imapclient.search()
    
    imapclient usa un formato especial para los criterios.
    Args:
        date_from: Fecha inicial (YYYY-MM-DD). Si es None, usa IMAP_DATE_FROM del config
        date_to: Fecha final (YYYY-MM-DD). Si es None, no filtra hasta hoy
    """
    criteria = []
    
    # Date filter: SINCE <date>
    if date_from:
        try:
            d = datetime.fromisoformat(date_from)
            criteria.extend(['SINCE', d.strftime('%d-%b-%Y')])
            logger.info(f"✅ SINCE filter: {d.strftime('%d-%b-%Y')}")
        except Exception as e:
            logger.warning(f"⚠️ Invalid date_from format: {e}")
    elif IMAP_DATE_FROM:
        # Fallback a config
        try:
            d = datetime.fromisoformat(IMAP_DATE_FROM)
            criteria.extend(['SINCE', d.strftime('%d-%b-%Y')])
            logger.info(f"✅ SINCE filter (from config): {d.strftime('%d-%b-%Y')}")
        except Exception:
            pass
    
    # Date filter: BEFORE <date>
    if date_to:
        try:
            d = datetime.fromisoformat(date_to)
            # Sumar 1 día para incluir todo el día especificado
            from datetime import timedelta
            d_next = d + timedelta(days=1)
            criteria.extend(['BEFORE', d_next.strftime('%d-%b-%Y')])
            logger.info(f"✅ BEFORE filter: {d_next.strftime('%d-%b-%Y')}")
        except Exception as e:
            logger.warning(f"⚠️ Invalid date_to format: {e}")
    
    if not criteria:
        criteria = ['ALL']
        logger.info("ℹ️ No date filters, using ALL")
    
    logger.info(f"📅 Final IMAP search criteria: {criteria}")
    return criteria

def _ensure_bytes(s):
    return s if isinstance(s, bytes) else s.encode("utf-8", errors="ignore")

def _extract_text_html(msg):
    """
    Extract text/plain and text/html bodies from a pyzmail.PyzMessage.
    Returns (text_body, html_body)
    """
    # Extract TEXT
    text_body = None
    if msg.text_part:
        try:
            payload = msg.text_part.get_payload()
            if payload:
                if isinstance(payload, bytes):
                    text_body = payload.decode(msg.text_part.charset or "utf-8", errors="ignore")
                else:
                    text_body = str(payload)
        except Exception as e:
            logger.warning(f"⚠️ Error extracting text body: {e}")
            text_body = None

    # Extract HTML
    html_body = None
    if msg.html_part:
        try:
            payload = msg.html_part.get_payload()
            if payload:
                if isinstance(payload, bytes):
                    html_body = payload.decode(msg.html_part.charset or "utf-8", errors="ignore")
                else:
                    html_body = str(payload)
        except Exception as e:
            logger.warning(f"⚠️ Error extracting html body: {e}")
            html_body = None

    return text_body, html_body


def _save_attachment(uid, filename, part):
    """
    Save attachment bytes to disk. part.get_payload() may return bytes already for pyzmail parts.
    """
    safe_name = filename.replace("/", "_").replace("\\", "_")
    timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    fname = f"{uid}_{timestamp}_{safe_name}"
    path = Path(PDF_SAVE_DIR) / fname
    payload = part.get_payload()  # bytes
    # Ensure bytes
    if isinstance(payload, str):
        payload = payload.encode("utf-8", errors="ignore")
    with open(path, "wb") as f:
        f.write(payload)
    return str(path.resolve())

def _extract_pdfs_from_pyzmessage(msg, uid):
    """
    Given a pyzmail.PyzMessage, iterate through its parts and collect all pdf attachments.
    Returns list of paths saved.
    """
    saved = []
    # pyzmail exposes mailparts
    for part in msg.mailparts:
        # part.filename may be None; part.type contains MIME type
        filename = part.filename
        if filename and PDF_EXT_RE.search(filename):
            path = _save_attachment(uid, filename, part)
            saved.append({"filename": filename, "path": path, "mime": part.type})
    return saved

def _create_imap_client():
    """Crea y autentica una conexión IMAP con reintentos"""
    max_retries = 3
    for attempt in range(max_retries):
        try:
            client = IMAPClient(IMAP_HOST, port=IMAP_PORT, use_uid=True, ssl=True, timeout=60)
            imap_config = get_imap_config()
            if imap_config:
                client.login(imap_config.get("user"), imap_config.get("password"))
            else:
                client.login(IMAP_USER, IMAP_PASS)
            logger.info("✅ IMAP connected successfully")
            return client
        except Exception as e:
            logger.error(f"❌ IMAP connection attempt {attempt + 1}/{max_retries} failed: {e}")
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)  # Exponential backoff: 1s, 2s, 4s
            else:
                raise

def _fetch_with_retry(client, batch, fetch_attrs, max_retries=3):
    """Intenta hacer fetch con reintentos y reconexión"""
    for attempt in range(max_retries):
        try:
            return client.fetch(batch, fetch_attrs)
        except Exception as e:
            logger.warning(f"⚠️ Fetch attempt {attempt + 1}/{max_retries} failed: {e}")
            if attempt < max_retries - 1:
                try:
                    logger.info("🔄 Reconnecting to IMAP...")
                    client.logout()
                except:
                    pass
                time.sleep(2 ** attempt)
                client = _create_imap_client()
            else:
                raise
    return {}

def connect_and_download_pdfs(
    limit: int = None,
    folder: str = None,
    mark_processed: bool = True,
    verbose: bool = False,
    force: bool = False,
    date_from: str = None,
    date_to: str = None
):
    """
    Connects to IMAP server, searches messages according to config, downloads PDF attachments.
    
    Args:
        limit: Límite de emails a procesar
        folder: Carpeta IMAP
        mark_processed: Marcar como procesado en BD
        verbose: Mostrar logs detallados
        force: Procesar mensajes aunque ya estén en BD
        date_from: Fecha inicial (YYYY-MM-DD). Ej: "2024-01-15"
        date_to: Fecha final (YYYY-MM-DD). Ej: "2024-12-31"
        
    If force=True -> procesa mensajes aunque ya estén marcados en la BD.
    """
    results = []
    folder = folder or IMAP_FOLDER
    limit = limit if (limit is not None) else (IMAP_LIMIT or 0)
    
    client = None
    try:
        client = _create_imap_client()
        client.select_folder(folder, readonly=False)
        criteria = _build_search_criteria(date_from=date_from, date_to=date_to)
        
        if verbose:
            logger.info(f"📅 Criterios de búsqueda: {criteria}")
            if date_from:
                logger.info(f"   Desde: {date_from}")
            if date_to:
                logger.info(f"   Hasta: {date_to}")
        
        # initial search
        uids = client.search(criteria)
        if not uids:
            if verbose:
                logger.info("❌ No messages found for criteria: " + str(criteria))
            return results

        uids.sort()
        if limit and limit > 0:
            uids = uids[-limit:]
            if verbose:
                logger.info(f"📧 Encontrados {len(uids)} emails (limitados a {limit})")
        else:
            if verbose:
                logger.info(f"📧 Encontrados {len(uids)} emails")

        fetch_attrs = ['RFC822', 'BODYSTRUCTURE', 'ENVELOPE']
        chunk_size = 50  # Reducido de 100 a 50 para evitar problemas de conexión
        to_iter = uids
        for i in range(0, len(to_iter), chunk_size):
            batch = to_iter[i:i+chunk_size]
            logger.info(f"📬 Procesando batch {i//chunk_size + 1} ({len(batch)} emails)...")
            
            # Fetch con reintentos
            resp = _fetch_with_retry(client, batch, fetch_attrs, max_retries=3)
            
            if not resp:
                logger.warning(f"⚠️ Batch vacío, continuando...")
                continue
            for uid, data in resp.items():
                # <-- aquí está la modificación clave:
                if not force and is_uid_processed(uid, folder=folder):
                    if verbose:
                        logger.info(f"⏭️  Skipping already processed UID {uid}")
                    continue

                raw = data.get(b'RFC822')
                if not raw:
                    if verbose:
                        logger.info(f"❌ No RFC822 body for UID {uid}; skipping")
                    continue

                try:
                    msg = pyzmail.PyzMessage.factory(raw)
                except Exception as e:
                    if verbose:
                        logger.error(f"❌ Failed parsing message UID {uid}: {e}")
                    continue

                # extrae cuerpos, metadatos, PDFs (igual que antes)
                text_body, html_body = _extract_text_html(msg)
                envelope = data.get(b'ENVELOPE')
                subject = msg.get_subject() or ""
                from_ = msg.get_addresses('from')  # list of tuples (name, email)
                from_str = ", ".join([f"{n} <{e}>" if n else e for n,e in from_]) if from_ else ""
                message_id = msg.get_decoded_header("message-id") or "unknown"
                try:
                    date_header = msg.get('date')
                    if date_header:
                        date_dt = parsedate_to_datetime(date_header)
                    else:
                        date_dt = None
                except Exception:
                    date_dt = None

                # ===== FILTRO ADICIONAL POR FECHA EN PYTHON =====
                # Para asegurar que REALMENTE está dentro del rango
                if date_from or date_to:
                    if not date_dt:
                        logger.warning(f"⚠️ UID {uid} sin fecha, saltando")
                        continue
                    
                    email_date = date_dt.date()
                    
                    if date_from:
                        date_from_obj = datetime.fromisoformat(date_from).date()
                        if email_date < date_from_obj:
                            logger.warning(f"⚠️ UID {uid} anterior a {date_from} ({email_date}), saltando")
                            continue
                    
                    if date_to:
                        date_to_obj = datetime.fromisoformat(date_to).date()
                        if email_date > date_to_obj:
                            logger.warning(f"⚠️ UID {uid} posterior a {date_to} ({email_date}), saltando")
                            continue
                    
                    logger.info(f"✅ UID {uid} está dentro del rango de fechas ({email_date})")

                ALLOWED_SUBJECT_TERMS = [
                    "yape", "comprobante", "transferen", "consumo", 
                    "retiro", "devolución", "cargo", "abono", "movimiento", "operación"
                ]

                setups = get_email_setups()  # retorna lista de dicts
                if setups:
                    senders = [s["bank_sender"].strip().lower() for s in setups if s.get("bank_sender")]
                else:
                    # fallback a env
                    senders = [s.strip().lower() for s in IMAP_SENDER_FILTER.split(",") if s.strip()]

                # Filtrar emails por sender
                match_sender = False
                for _, email_addr in from_:
                    if any(s in email_addr.lower() for s in senders):
                        match_sender = True
                        break

                if not match_sender:
                    if verbose:
                        logger.info(f"❌ UID {uid} sender {from_str} filtered out")
                    continue
                
                if IMAP_SUBJECT_FILTER:
                    subs = [x.strip().lower() for x in IMAP_SUBJECT_FILTER.split(",") if x.strip()]
                    if not any(sub in subject.lower() for sub in subs):
                        if verbose:
                            logger.info(f"❌ UID {uid} subject '{subject}' filtered out")
                        continue
                
                # Dentro del loop de mensajes
                subject_lower = subject.lower()

                # Filtrar solo si el asunto contiene alguno de los términos permitidos
                if not any(term in subject_lower for term in ALLOWED_SUBJECT_TERMS):
                    if verbose:
                        logger.info(f"❌ UID {uid} subject '{subject}' filtered out (not a payment/movement)")
                    continue
                
                pdfs = _extract_pdfs_from_pyzmessage(msg, uid)
                if IMAP_ONLY_WITH_ATTACHMENTS and not pdfs:
                    if verbose:
                        logger.info(f"❌ UID {uid} has no PDF attachments; skipping due to IMAP_ONLY_WITH_ATTACHMENTS")
                    continue

                # ===== VALIDAR QUE TENEMOS DATOS MÍNIMOS =====
                # No queremos guardar emails completamente vacíos
                if not any([subject, text_body, html_body, from_str, message_id]):
                    logger.error(f"❌ UID {uid} completamente vacío, NO PROCESANDO")
                    continue
                
                if not text_body and not html_body:
                    logger.warning(f"⚠️ UID {uid} sin cuerpo (text_body ni html_body), saltando")
                    continue

                metadata = {
                    "folder": folder,
                    "message_id": message_id,
                    "subject": subject,
                    "from": from_str,
                    "date": date_dt.isoformat() if date_dt else None,
                    "fetched_at": datetime.utcnow().isoformat(),
                    "pdfs": [p["path"] for p in pdfs],
                    "text_body": text_body,
                    "html_body": html_body,
                }

                logger.info(f"✅ UID {uid} validado correctamente")
                logger.debug(f"   Subject: {subject[:50] if subject else 'N/A'}")
                logger.debug(f"   From: {from_str[:50] if from_str else 'N/A'}")
                logger.debug(f"   Has text: {bool(text_body)}, Has HTML: {bool(html_body)}")

                # flags y move (igual)
                try:
                    if MARK_AS_SEEN:
                        client.add_flags(uid, [SEEN])
                except Exception:
                    pass
                if MOVE_PROCESSED_TO_FOLDER:
                    try:
                        if MOVE_PROCESSED_TO_FOLDER not in client.list_folders():
                            client.create_folder(MOVE_PROCESSED_TO_FOLDER)
                        client.move(uid, MOVE_PROCESSED_TO_FOLDER)
                    except Exception:
                        pass

                results.append({"uid": uid, "metadata": metadata})
            
            # Pausa entre batches para evitar sobrecargar la conexión
            if i + chunk_size < len(to_iter):
                time.sleep(1)
                
    finally:
        if client:
            try:
                client.logout()
                logger.info("✅ IMAP disconnected")
            except Exception as e:
                logger.warning(f"⚠️ Error closing IMAP connection: {e}")

    logger.info(f"✅ Descargados {len(results)} emails válidos")
    return results