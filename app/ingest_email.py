import os
import re
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

def _build_search_criteria():
    """
    Build IMAP search criteria list for imapclient.search()
    """
    criteria = ['ALL']
    # Date filter: SINCE <date>
    if IMAP_DATE_FROM:
        # Expect YYYY-MM-DD
        try:
            d = datetime.fromisoformat(IMAP_DATE_FROM)
            criteria = ['SINCE', d.strftime('%d-%b-%Y')]
        except Exception:
            # ignore invalid date
            pass

    # If user wants only messages with attachments, some servers support 'HAS', but it's not standardized.
    # We'll still use HAS if available; otherwise filter later.
    # Sender filter: allow comma-separated list
    if IMAP_SENDER_FILTER:
        senders = [s.strip() for s in IMAP_SENDER_FILTER.split(",") if s.strip()]
        if len(senders) == 1:
            criteria += ['FROM', senders[0]]
        elif len(senders) > 1:
            # Combine with ORs: (FROM a OR FROM b OR FROM c)
            # imapclient expects a flat list for complex queries; build manually using OR nesting.
            # Simpler approach: search ALL and filter in Python.
            pass

    # Subject filter: will filter in Python
    return criteria

def _ensure_bytes(s):
    return s if isinstance(s, bytes) else s.encode("utf-8", errors="ignore")

def _extract_text_html(msg):
    """
    Extract text/plain and text/html bodies from a pyzmail.PyzMessage.
    Returns (text_body, html_body)
    """
    # Extract TEXT
    if msg.text_part:
        try:
            text_body = msg.text_part.get_payload().decode(msg.text_part.charset or "utf-8", errors="ignore")
        except Exception:
            text_body = None
    else:
        text_body = None

    # Extract HTML
    if msg.html_part:
        try:
            html_body = msg.html_part.get_payload().decode(msg.html_part.charset or "utf-8", errors="ignore")
        except Exception:
            html_body = None
    else:
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

def connect_and_download_pdfs(
    limit: int = None,
    folder: str = None,
    mark_processed: bool = True,
    verbose: bool = False,
    force: bool = False
):
    """
    Connects to IMAP server, searches messages according to config, downloads PDF attachments.
    If force=True -> procesa mensajes aunque ya estén marcados en la BD.
    """
    results = []
    folder = folder or IMAP_FOLDER
    limit = limit if (limit is not None) else (IMAP_LIMIT or 0)
    client = IMAPClient(IMAP_HOST, port=IMAP_PORT, use_uid=True, ssl=True, timeout=30)
    imap_config = get_imap_config()
    if imap_config:
        client.login(imap_config.get("user"), imap_config.get("password"))
    else:
        client.login(IMAP_USER, IMAP_PASS)
    try:
        client.select_folder(folder, readonly=False)
        criteria = _build_search_criteria()
        # initial search
        uids = client.search(criteria)
        if not uids:
            if verbose:
                print("No messages found for criteria:", criteria)
            return results

        uids.sort()
        if limit and limit > 0:
            uids = uids[-limit:]

        fetch_attrs = ['RFC822', 'BODYSTRUCTURE', 'ENVELOPE']
        chunk_size = 100
        to_iter = uids
        for i in range(0, len(to_iter), chunk_size):
            batch = to_iter[i:i+chunk_size]
            resp = client.fetch(batch, fetch_attrs)
            for uid, data in resp.items():
                # <-- aquí está la modificación clave:
                if not force and is_uid_processed(uid, folder=folder):
                    if verbose:
                        print(f"Skipping already processed UID {uid}")
                    continue

                raw = data.get(b'RFC822')
                if not raw:
                    if verbose:
                        print(f"No RFC822 body for UID {uid}; skipping")
                    continue

                try:
                    msg = pyzmail.PyzMessage.factory(raw)
                except Exception as e:
                    if verbose:
                        print(f"Failed parsing message UID {uid}: {e}")
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

                # filtros en Python (igual que ahora)
                # if IMAP_SENDER_FILTER:
                #     senders = [s.strip().lower() for s in IMAP_SENDER_FILTER.split(",") if s.strip()]
                #     match_sender = False
                #     for _, email_addr in from_:
                #         if any(s in email_addr.lower() for s in senders):
                #             match_sender = True
                #             break
                #     if not match_sender:
                #         if verbose:
                #             print(f"UID {uid} sender {from_str} filtered out")
                #         continue

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
                        print(f"UID {uid} sender {from_str} filtered out")
                    continue
                if IMAP_SUBJECT_FILTER:
                    subs = [x.strip().lower() for x in IMAP_SUBJECT_FILTER.split(",") if x.strip()]
                    if not any(sub in subject.lower() for sub in subs):
                        if verbose:
                            print(f"UID {uid} subject '{subject}' filtered out")
                        continue

                pdfs = _extract_pdfs_from_pyzmessage(msg, uid)
                if IMAP_ONLY_WITH_ATTACHMENTS and not pdfs:
                    if verbose:
                        print(f"UID {uid} has no PDF attachments; skipping due to IMAP_ONLY_WITH_ATTACHMENTS")
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

                # marcar procesado sólo si mark_processed=True
                if mark_processed:
                    mark_uid_processed(uid, metadata)

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
    finally:
        try:
            client.logout()
        except Exception:
            pass

    return results
