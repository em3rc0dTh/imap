import os
import re
import time
from datetime import datetime, timedelta
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

def _build_imap_search_criteria(date_from: str = None, date_to: str = None, senders: list = None, subject_keywords: list = None):
    """
    Build IMAP search criteria for server-side filtering (RFC 3501).
    All filtering happens on IMAP server, returns only matching UIDs.
    
    Args:
        date_from: Start date (YYYY-MM-DD)
        date_to: End date (YYYY-MM-DD)
        senders: List of sender email addresses to match
        subject_keywords: List of keywords that must appear in subject
    
    Returns:
        List representing IMAP search criteria
    """
    criteria = []
    
    # === DATE RANGE FILTERS ===
    if date_from:
        try:
            d = datetime.fromisoformat(date_from)
            criteria.extend(['SINCE', d.strftime('%d-%b-%Y')])
            logger.info(f"✅ SINCE filter: {d.strftime('%d-%b-%Y')}")
        except Exception as e:
            logger.warning(f"⚠️ Invalid date_from: {e}")
    
    if date_to:
        try:
            d = datetime.fromisoformat(date_to)
            d_next = d + timedelta(days=1)
            criteria.extend(['BEFORE', d_next.strftime('%d-%b-%Y')])
            logger.info(f"✅ BEFORE filter: {d_next.strftime('%d-%b-%Y')}")
        except Exception as e:
            logger.warning(f"⚠️ Invalid date_to: {e}")
    
    # === SENDER FILTER (OR condition) ===
    # Only add if we have senders and they're not empty
    valid_senders = [s.strip() for s in (senders or []) if s and s.strip()]
    if valid_senders:
        if len(valid_senders) == 1:
            criteria.extend(['FROM', valid_senders[0]])
            logger.info(f"✅ FROM filter: {valid_senders[0]}")
        else:
            # Build nested OR: (FROM a OR FROM b OR FROM c)
            or_clause = None
            for sender in reversed(valid_senders):
                if or_clause is None:
                    or_clause = ['FROM', sender]
                else:
                    or_clause = ['OR', 'FROM', sender, or_clause]
            
            if or_clause:
                criteria.append(or_clause)
                logger.info(f"✅ FROM filter (OR): {valid_senders}")
    
    # === SUBJECT FILTER (OR condition) ===
    # At least ONE keyword must match
    valid_keywords = [k.strip() for k in (subject_keywords or []) if k and k.strip()]
    if valid_keywords:
        if len(valid_keywords) == 1:
            criteria.extend(['SUBJECT', valid_keywords[0]])
            logger.info(f"✅ SUBJECT filter: {valid_keywords[0]}")
        else:
            # Build nested OR: (SUBJECT a OR SUBJECT b OR SUBJECT c)
            or_clause = None
            for keyword in reversed(valid_keywords):
                if or_clause is None:
                    or_clause = ['SUBJECT', keyword]
                else:
                    or_clause = ['OR', 'SUBJECT', keyword, or_clause]
            
            if or_clause:
                criteria.append(or_clause)
                logger.info(f"✅ SUBJECT filter (OR): {valid_keywords}")
    
    # === EXCLUDE DEVOLUCIONES (optional - comment out if needed) ===
    # criteria.extend(['NOT', 'SUBJECT', 'devolución'])
    
    if not criteria:
        criteria = ['ALL']
        logger.info("ℹ️ No filters applied, using ALL")
    
    logger.info(f"📋 Final IMAP criteria: {criteria}")
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

def _build_imap_search_criteria(date_from: str = None, date_to: str = None, senders: list = None, subject_keywords: list = None):
    """
    Build IMAP search criteria for server-side filtering (RFC 3501).
    All filtering happens on IMAP server, returns only matching UIDs.
    
    Args:
        date_from: Start date (YYYY-MM-DD)
        date_to: End date (YYYY-MM-DD)
        senders: List of sender email addresses to match
        subject_keywords: List of keywords that must appear in subject
    
    Returns:
        List representing IMAP search criteria
    """
    criteria = []
    
    # === DATE RANGE FILTERS ===
    if date_from:
        try:
            d = datetime.fromisoformat(date_from)
            criteria.extend(['SINCE', d.strftime('%d-%b-%Y')])
            logger.info(f"✅ SINCE filter: {d.strftime('%d-%b-%Y')}")
        except Exception as e:
            logger.warning(f"⚠️ Invalid date_from: {e}")
    
    if date_to:
        try:
            d = datetime.fromisoformat(date_to)
            d_next = d + timedelta(days=1)
            criteria.extend(['BEFORE', d_next.strftime('%d-%b-%Y')])
            logger.info(f"✅ BEFORE filter: {d_next.strftime('%d-%b-%Y')}")
        except Exception as e:
            logger.warning(f"⚠️ Invalid date_to: {e}")
    
    # === SENDER FILTER (OR condition) ===
    # Only add if we have senders and they're not empty
    valid_senders = [s.strip() for s in (senders or []) if s and s.strip()]
    if valid_senders:
        if len(valid_senders) == 1:
            criteria.extend(['FROM', valid_senders[0]])
            logger.info(f"✅ FROM filter: {valid_senders[0]}")
        else:
            # Build nested OR: (FROM a OR FROM b OR FROM c)
            or_clause = None
            for sender in reversed(valid_senders):
                if or_clause is None:
                    or_clause = ['FROM', sender]
                else:
                    or_clause = ['OR', 'FROM', sender, or_clause]
            
            if or_clause:
                criteria.append(or_clause)
                logger.info(f"✅ FROM filter (OR): {valid_senders}")
    
    # === SUBJECT FILTER (OR condition) ===
    # At least ONE keyword must match
    valid_keywords = [k.strip() for k in (subject_keywords or []) if k and k.strip()]
    if valid_keywords:
        if len(valid_keywords) == 1:
            criteria.extend(['SUBJECT', valid_keywords[0]])
            logger.info(f"✅ SUBJECT filter: {valid_keywords[0]}")
        else:
            # Build nested OR: (SUBJECT a OR SUBJECT b OR SUBJECT c)
            or_clause = None
            for keyword in reversed(valid_keywords):
                if or_clause is None:
                    or_clause = ['SUBJECT', keyword]
                else:
                    or_clause = ['OR', 'SUBJECT', keyword, or_clause]
            
            if or_clause:
                criteria.append(or_clause)
                logger.info(f"✅ SUBJECT filter (OR): {valid_keywords}")
    
    # === EXCLUDE DEVOLUCIONES (optional - comment out if needed) ===
    # criteria.extend(['NOT', 'SUBJECT', 'devolución'])
    
    if not criteria:
        criteria = ['ALL']
        logger.info("ℹ️ No filters applied, using ALL")
    
    logger.info(f"📋 Final IMAP criteria: {criteria}")
    return criteria


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
    Connects to IMAP server, searches with SERVER-SIDE filtering, downloads PDFs.
    
    🚀 KEY OPTIMIZATION: All filtering (date, sender, subject) happens on IMAP server.
    Only matching UIDs are returned and fetched - no wasted bandwidth or processing.
    
    Args:
        limit: Máximo de emails a procesar (últimos N)
        folder: Carpeta IMAP
        mark_processed: Marcar como procesado en BD
        verbose: Mostrar logs detallados
        force: Procesar aunque ya estén en BD
        date_from: Fecha inicial (YYYY-MM-DD). Ej: "2024-01-15"
        date_to: Fecha final (YYYY-MM-DD). Ej: "2024-12-31"
    
    Returns:
        List of {uid, metadata} dicts
    """
    results = []
    folder = folder or IMAP_FOLDER
    limit = limit if (limit is not None) else (IMAP_LIMIT or 0)
    
    client = None
    try:
        client = _create_imap_client()
        client.select_folder(folder, readonly=False)
        
        # === GET FILTERS FROM CONFIG ===
        setups = get_email_setups()
        senders = [s["bank_sender"].strip() for s in setups if s.get("bank_sender")]
        
        # Subject keywords from config or defaults
        subject_keywords = []
        if IMAP_SUBJECT_FILTER:
            subject_keywords = [x.strip() for x in IMAP_SUBJECT_FILTER.split(",") if x.strip()]
        else:
            # Fallback to common payment/movement terms
            subject_keywords = [
                "yape", "comprobante", "transferen", "consumo",
                "retiro", "devolucion", "cargo", "abono", "movimiento", "operacion"
            ]
        
        # === BUILD SERVER-SIDE CRITERIA ===
        # ✅ Server filters by: date range + sender + subject keywords
        criteria = _build_imap_search_criteria(
            date_from=date_from,
            date_to=date_to,
            senders=senders,
            subject_keywords=subject_keywords
        )
        
        if verbose:
            logger.info(f"🔍 Server-side search criteria: {criteria}")
            if date_from:
                logger.info(f"   📅 From: {date_from}")
            if date_to:
                logger.info(f"   📅 To: {date_to}")
            if senders:
                logger.info(f"   👤 Senders: {', '.join(senders)}")
            logger.info(f"   🏷️  Keywords: {', '.join(subject_keywords)}")
        
        # === IMAP SEARCH (server-side filtering) ===
        # 🚀 FAST: Returns only matching UIDs, not all 100k emails
        uids = client.search(criteria, charset="UTF-8")
        
        if not uids:
            logger.info("✅ No emails matching server-side criteria")
            return results
        
        logger.info(f"🎯 Server returned {len(uids)} matching UIDs (pre-filtered)")
        
        # Sort and apply limit
        uids.sort()
        if limit and limit > 0:
            uids = uids[-limit:]
            logger.info(f"📧 Limited to {limit} most recent: {len(uids)} emails to process")
        
        # === FETCH IN BATCHES ===
        fetch_attrs = ['RFC822', 'BODYSTRUCTURE', 'ENVELOPE']
        chunk_size = 50
        
        for i in range(0, len(uids), chunk_size):
            batch = uids[i:i+chunk_size]
            logger.info(f"📬 Batch {i//chunk_size + 1}/{(len(uids)-1)//chunk_size + 1} ({len(batch)} emails)")
            
            resp = _fetch_with_retry(client, batch, fetch_attrs, max_retries=3)
            
            if not resp:
                logger.warning(f"⚠️ Batch empty, continuing...")
                continue
            
            for uid, data in resp.items():
                # === SKIP ALREADY PROCESSED ===
                if not force and is_uid_processed(uid, folder=folder):
                    if verbose:
                        logger.info(f"⏭️  UID {uid} already processed, skipping")
                    continue
                
                # === EXTRACT EMAIL DATA ===
                raw = data.get(b'RFC822')
                if not raw:
                    logger.warning(f"❌ UID {uid} has no RFC822 body")
                    continue
                
                try:
                    msg = pyzmail.PyzMessage.factory(raw)
                except Exception as e:
                    logger.error(f"❌ UID {uid} parse error: {e}")
                    continue
                
                # Extract components
                text_body, html_body = _extract_text_html(msg)
                subject = msg.get_subject() or ""
                from_ = msg.get_addresses('from') or []
                from_str = ", ".join([f"{n} <{e}>" if n else e for n, e in from_]) if from_ else ""
                message_id = msg.get_decoded_header("message-id") or "unknown"
                
                # Parse date
                try:
                    date_header = msg.get('date')
                    date_dt = parsedate_to_datetime(date_header) if date_header else None
                except Exception:
                    date_dt = None
                
                # === MINIMAL VALIDATION ===
                # We trust server-side filtering, but still validate we have content
                if not any([subject, text_body, html_body, from_str, message_id]):
                    logger.error(f"❌ UID {uid} completely empty, skipping")
                    continue
                
                if not text_body and not html_body:
                    logger.warning(f"⚠️ UID {uid} has no body content, skipping")
                    continue
                
                # Extract PDFs
                pdfs = _extract_pdfs_from_pyzmessage(msg, uid)
                
                # Check attachment requirement (only if configured)
                if IMAP_ONLY_WITH_ATTACHMENTS and not pdfs:
                    if verbose:
                        logger.info(f"⏭️  UID {uid} has no PDF attachments")
                    continue
                
                # === BUILD METADATA ===
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
                
                logger.info(f"✅ UID {uid} validated")
                if verbose:
                    logger.debug(f"   📄 Subject: {subject[:50]}")
                    logger.debug(f"   👤 From: {from_str[:50]}")
                    logger.debug(f"   📦 Text: {bool(text_body)}, HTML: {bool(html_body)}")
                
                # === MARK AS SEEN / MOVE (if configured) ===
                try:
                    if MARK_AS_SEEN:
                        client.add_flags(uid, [SEEN])
                except Exception as e:
                    logger.warning(f"⚠️ Could not mark as seen: {e}")
                
                if MOVE_PROCESSED_TO_FOLDER:
                    try:
                        client.move(uid, MOVE_PROCESSED_TO_FOLDER)
                    except Exception as e:
                        logger.warning(f"⚠️ Could not move email: {e}")
                
                results.append({"uid": uid, "metadata": metadata})
            
            # Pause between batches
            if i + chunk_size < len(uids):
                time.sleep(0.5)
    
    finally:
        if client:
            try:
                client.logout()
                logger.info("✅ IMAP disconnected")
            except Exception as e:
                logger.warning(f"⚠️ Error closing IMAP: {e}")
    
    logger.info(f"✅ Downloaded {len(results)} valid emails")
    return results