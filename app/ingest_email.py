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
    MOVE_PROCESSED_TO_FOLDER, MARK_AS_SEEN,
    MONGO_URI, MONGO_DB, MONGO_EMAIL_SETUP_COLLECTION,
    MONGO_COLLECTION
)
from .db import is_uid_processed, mark_uid_processed
from pymongo import MongoClient
import logging

logger = logging.getLogger(__name__)

# Clientes MongoDB (fallback)
client = MongoClient(MONGO_URI)
db = client[MONGO_DB]
email_setup_col = db[MONGO_EMAIL_SETUP_COLLECTION]
raw_emails_col = db[MONGO_COLLECTION]
imap_config_col = db["imap_config"]

PDF_EXT_RE = re.compile(r"\.pdf$", re.IGNORECASE)


def resolve_imap_folder(imap, folder_name):
    """Resuelve el nombre de la carpeta IMAP (ej: All Mail en Gmail)"""
    if folder_name.upper() == "INBOX":
        return "INBOX"

    try:
        folders = imap.list_folders()
    except Exception as e:
        logger.error(f"❌ Error al listar carpetas IMAP: {e}")
        return "INBOX"

    possible_names = [
        "All Mail", "[Gmail]/All Mail", "[Google Mail]/All Mail",
        "Todos", "[Gmail]/Todos", "Todos los mensajes",
        "[Gmail]/Todos los mensajes", "Archive", "[Gmail]/Archive",
    ]

    for flags, delimiter, mailbox in folders:
        mailbox_str = mailbox
        for name in possible_names:
            if name.lower() in mailbox_str.lower():
                logger.info(f"✔ IMAP folder detectado: {mailbox_str}")
                return mailbox_str

    logger.warning("⚠ No se encontró ALL MAIL/TODOS, usando INBOX")
    return "INBOX"

def _build_imap_search_criteria(
    date_from: str = None,
    date_to: str = None,
    senders: list = None,
    subject_keywords: list = None
):
    """
    Construye criterios de búsqueda IMAP (RFC 3501).
    
    IMPORTANTE: SUBJECT en IMAP busca palabras completas, no subcadenas.
    Para "yape" → encuentra "Yapeo exitoso" ✅
    Para "transferen" → NO encuentra "transferencia" ❌
    
    SOLUCIÓN: Si hay keywords problemáticos, hacer búsqueda solo por FROM
    y filtrar por SUBJECT en el lado del cliente.
    """
    criteria = []
    
    # === FILTROS DE FECHA ===
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
    
    # === FILTRO DE REMITENTES (OR) ===
    valid_senders = [s.strip() for s in (senders or []) if s and s.strip()]
    if valid_senders:
        if len(valid_senders) == 1:
            criteria.extend(['FROM', valid_senders[0]])
            logger.info(f"✅ FROM filter: {valid_senders[0]}")
        else:
            or_clause = None
            for sender in reversed(valid_senders):
                if or_clause is None:
                    or_clause = ['FROM', sender]
                else:
                    or_clause = ['OR', 'FROM', sender, or_clause]
            
            if or_clause:
                criteria.append(or_clause)
                logger.info(f"✅ FROM filter (OR): {valid_senders}")
    
    # === FILTRO DE SUBJECT (FLEXIBLE) ===
    # 🔥 CAMBIO: No filtrar por SUBJECT en servidor, hacerlo en cliente
    # Razón: IMAP SUBJECT busca palabras completas, no subcadenas
    # "transferen" no encuentra "transferencia"
    # "devolucion" no encuentra "devolución" (por tilde)
    
    valid_keywords = [k.strip() for k in (subject_keywords or []) if k and k.strip()]
    if valid_keywords:
        logger.info(f"⚠️ SUBJECT keywords will be filtered CLIENT-SIDE: {valid_keywords}")
        logger.info(f"   Reason: IMAP SUBJECT only matches whole words, not substrings")
        # NO agregar criterios SUBJECT al servidor
    
    if not criteria:
        criteria = ['ALL']
        logger.info("ℹ️ No filters applied, using ALL")
    
    logger.info(f"📋 Final IMAP criteria: {criteria}")
    return criteria

def _extract_text_html(msg):
    """Extrae text/plain y text/html de un mensaje pyzmail"""
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

    return text_body, html_body


def _save_attachment(uid, filename, part):
    """Guarda un attachment en disco"""
    safe_name = filename.replace("/", "_").replace("\\", "_")
    timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    fname = f"{uid}_{timestamp}_{safe_name}"
    path = Path(PDF_SAVE_DIR) / fname
    payload = part.get_payload()
    
    if isinstance(payload, str):
        payload = payload.encode("utf-8", errors="ignore")
    
    with open(path, "wb") as f:
        f.write(payload)
    
    return str(path.resolve())


def _extract_pdfs_from_pyzmessage(msg, uid):
    """Extrae PDFs de un mensaje pyzmail"""
    saved = []
    for part in msg.mailparts:
        filename = part.filename
        if filename and PDF_EXT_RE.search(filename):
            path = _save_attachment(uid, filename, part)
            saved.append({"filename": filename, "path": path, "mime": part.type})
    return saved


def _create_imap_client(db_name: str = None):
    """
    Crea y autentica una conexión IMAP con reintentos.
    Soporte multi-tenant: usa config de la BD del tenant si se proporciona.
    """
    max_retries = 3
    
    # Obtener configuración IMAP (de tenant o global)
    if db_name:
        from .db import get_tenant_collections
        cols = get_tenant_collections(db_name)
        imap_config = cols["imap_config_col"].find_one({"active": True}, {"_id": 0})
        logger.info(f"📦 Loading IMAP config from tenant DB: {db_name}")
    else:
        imap_config = imap_config_col.find_one({"active": True}, {"_id": 0})
        logger.info("📦 Loading IMAP config from default DB")
    
    for attempt in range(max_retries):
        try:
            client = IMAPClient(IMAP_HOST, port=IMAP_PORT, use_uid=True, ssl=True, timeout=60)
            
            if imap_config:
                client.login(imap_config.get("user"), imap_config.get("password"))
                logger.info(f"✅ IMAP connected with DB config")
            else:
                client.login(IMAP_USER, IMAP_PASS)
                logger.info("✅ IMAP connected with environment config")
            
            return client
            
        except Exception as e:
            logger.error(f"❌ IMAP connection attempt {attempt + 1}/{max_retries} failed: {e}")
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
            else:
                raise


def _fetch_with_retry(client, batch, fetch_attrs, max_retries=3):
    """Intenta hacer fetch con reintentos"""
    for attempt in range(max_retries):
        try:
            return client.fetch(batch, fetch_attrs)
        except Exception as e:
            logger.warning(f"⚠️ Fetch attempt {attempt + 1}/{max_retries} failed: {e}")
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
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
    date_to: str = None,
    db_name: str = None
):
    """
    Conecta a servidor IMAP, busca emails con filtrado HÍBRIDO.
    
    ESTRATEGIA:
    - Filtrado SERVER-SIDE: fecha + remitente (eficiente)
    - Filtrado CLIENT-SIDE: subject keywords (flexible, soporta subcadenas)
    """
    results = []
    
    # === CONFIGURACIÓN MULTI-TENANT ===
    if db_name:
        from .db import get_tenant_collections
        cols = get_tenant_collections(db_name)
        email_setup_col_target = cols["email_setup_col"]
        logger.info(f"📦 Using tenant database: {db_name}")
    else:
        email_setup_col_target = email_setup_col
        logger.info(f"📦 Using default database: {MONGO_DB}")
    
    client = None
    try:
        # === CONECTAR A IMAP ===
        client = _create_imap_client(db_name)
        folder = resolve_imap_folder(client, folder or IMAP_FOLDER)
        client.select_folder(folder, readonly=False)
        limit = limit if (limit is not None) else (IMAP_LIMIT or 0)
        
        # === OBTENER FILTROS DESDE BD ===
        setups = list(email_setup_col_target.find({}))
        senders = [s["bank_sender"].strip() for s in setups if s.get("bank_sender")]
        
        if verbose and senders:
            logger.info(f"📧 Found {len(senders)} email setups: {senders}")
        
        # Subject keywords (para filtrado CLIENT-SIDE)
        subject_keywords = []
        if IMAP_SUBJECT_FILTER:
            subject_keywords = [x.strip() for x in IMAP_SUBJECT_FILTER.split(",") if x.strip()]
        else:
            subject_keywords = [
                "yape", "comprobante", "transferen", "consumo",
                "retiro", "devolucion", "cargo", "abono", "movimiento", "operacion"
            ]
        
        # === CONSTRUIR CRITERIOS IMAP (SIN SUBJECT) ===
        criteria = _build_imap_search_criteria(
            date_from=date_from,
            date_to=date_to,
            senders=senders,
            subject_keywords=None  # 🔥 No filtrar SUBJECT en servidor
        )
        
        if verbose:
            logger.info(f"🔍 Server-side criteria: {criteria}")
            logger.info(f"🔍 Client-side SUBJECT filter: {subject_keywords}")
        
        # === BÚSQUEDA IMAP (SERVER-SIDE: fecha + remitente) ===
        uids = client.search(criteria, charset="UTF-8")
        
        if not uids:
            logger.info("✅ No emails matching server-side criteria")
            return results
        
        logger.info(f"🎯 Server returned {len(uids)} UIDs (filtered by date + sender)")
        
        # Ordenar y aplicar limit
        uids.sort()
        if limit and limit > 0:
            uids = uids[-limit:]
            logger.info(f"📧 Limited to {limit} most recent")
        
        # === FETCH EN BATCHES ===
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
                # Skip already processed
                if not force:
                    if db_name:
                        already_processed = is_uid_processed(uid, folder=folder, db_name=db_name)
                    else:
                        already_processed = is_uid_processed(uid, folder=folder)
                    
                    if already_processed:
                        if verbose:
                            logger.info(f"⏭️  UID {uid} already processed, skipping")
                        continue
                
                # Extract email data
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
                
                # === FILTRADO CLIENT-SIDE: SUBJECT KEYWORDS ===
                # 🔥 NUEVO: Verificar si el subject contiene ALGUNA keyword
                subject_lower = subject.lower()
                
                if subject_keywords:
                    matches_keyword = any(kw.lower() in subject_lower for kw in subject_keywords)
                    
                    if not matches_keyword:
                        if verbose:
                            logger.info(f"⏭️  UID {uid} subject doesn't match keywords: '{subject[:50]}'")
                        continue
                    else:
                        if verbose:
                            logger.info(f"✅ UID {uid} matches keyword in subject: '{subject[:50]}'")
                
                # Validación mínima
                if not any([subject, text_body, html_body, from_str, message_id]):
                    logger.error(f"❌ UID {uid} completely empty, skipping")
                    continue
                
                if not text_body and not html_body:
                    logger.warning(f"⚠️ UID {uid} has no body content, skipping")
                    continue
                
                # Extract PDFs
                pdfs = _extract_pdfs_from_pyzmessage(msg, uid)
                
                # Check attachment requirement
                if IMAP_ONLY_WITH_ATTACHMENTS and not pdfs:
                    if verbose:
                        logger.info(f"⏭️  UID {uid} has no PDF attachments")
                    continue
                
                # Build metadata
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
                
                # Mark as seen / move
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
    
    logger.info(f"✅ Downloaded {len(results)} valid emails (after client-side filtering)")
    return results

# ============================================================================
# 🔬 FUNCIÓN DE DIAGNÓSTICO
# ============================================================================

def diagnose_imap_search(db_name: str = None):
    """
    Diagnostica por qué la búsqueda IMAP no encuentra emails.
    Ejecuta múltiples tests para identificar el problema exacto.
    """
    client = None
    results = {
        "total_emails": 0,
        "from_yape_exact": 0,
        "from_yape_partial": 0,
        "with_any_subject": 0,
        "combined": 0,
        "sample_emails": [],
        "issue_detected": None,
        "alternative_senders": []
    }
    
    try:
        # Obtener colecciones del tenant
        if db_name:
            from .db import get_tenant_collections
            cols = get_tenant_collections(db_name)
            email_setup_col_target = cols["email_setup_col"]
        else:
            email_setup_col_target = email_setup_col
        
        # Crear conexión IMAP
        client = _create_imap_client(db_name)
        folder = resolve_imap_folder(client, IMAP_FOLDER)
        client.select_folder(folder, readonly=True)
        
        logger.info("=" * 70)
        logger.info("🔬 IMAP DIAGNOSTIC MODE")
        logger.info(f"   Database: {db_name or 'default'}")
        logger.info(f"   Folder: {folder}")
        logger.info("=" * 70)
        
        # TEST 1: Total emails
        logger.info("\n📧 TEST 1: Total emails in folder")
        all_uids = client.search(['ALL'])
        results["total_emails"] = len(all_uids)
        logger.info(f"   ✅ Found {len(all_uids)} total emails")
        
        if len(all_uids) == 0:
            results["issue_detected"] = "NO_EMAILS_IN_FOLDER"
            return results
        
        # TEST 2: FROM exact
        logger.info("\n📧 TEST 2: FROM notificaciones@yape.pe (exact)")
        try:
            from_exact = client.search(['FROM', 'notificaciones@yape.pe'])
            results["from_yape_exact"] = len(from_exact)
            logger.info(f"   Result: {len(from_exact)} emails")
        except Exception as e:
            logger.error(f"   ❌ Search failed: {e}")
        
        # TEST 2b: FROM partial
        logger.info("\n📧 TEST 2b: FROM yape.pe (partial)")
        try:
            from_partial = client.search(['FROM', 'yape.pe'])
            results["from_yape_partial"] = len(from_partial)
            logger.info(f"   Result: {len(from_partial)} emails")
        except Exception as e:
            logger.error(f"   ❌ Search failed: {e}")
        
        # TEST 2c: Alternatives
        logger.info("\n📧 TEST 2c: Alternative senders")
        alternatives = ['Yape', 'noreply@yape.pe', 'no-reply@yape.pe', 'notificacion@yape.pe', 'info@yape.pe']
        
        for alt in alternatives:
            try:
                alt_uids = client.search(['FROM', alt])
                if len(alt_uids) > 0:
                    logger.info(f"   ✅ FROM '{alt}': {len(alt_uids)} emails")
                    results["alternative_senders"].append({"sender": alt, "count": len(alt_uids)})
            except:
                pass
        
        # TEST 3: SUBJECT keywords
        logger.info("\n📧 TEST 3: SUBJECT keywords")
        keywords_test = ['yape', 'comprobante', 'transferencia', 'pago', 'yapeo']
        subject_results = {}
        
        for kw in keywords_test:
            try:
                kw_uids = client.search(['SUBJECT', kw])
                subject_results[kw] = len(kw_uids)
                if len(kw_uids) > 0:
                    logger.info(f"   ✅ SUBJECT '{kw}': {len(kw_uids)} emails")
            except Exception as e:
                logger.warning(f"   ⚠️ SUBJECT '{kw}' failed: {e}")
        
        results["with_any_subject"] = max(subject_results.values()) if subject_results else 0
        
        # TEST 4: Combined
        logger.info("\n📧 TEST 4: Combined FROM + SUBJECT")
        try:
            combined = client.search(['FROM', 'notificaciones@yape.pe', 'SUBJECT', 'yape'])
            results["combined"] = len(combined)
            logger.info(f"   Result: {len(combined)} emails")
        except Exception as e:
            logger.error(f"   ❌ Combined search failed: {e}")
        
        # TEST 5: Sample emails
        logger.info("\n📧 TEST 5: Inspecting recent emails")
        sample_uids = all_uids[-10:] if len(all_uids) >= 10 else all_uids
        
        for uid in sample_uids:
            try:
                resp = client.fetch([uid], ['ENVELOPE'])
                if uid not in resp:
                    continue
                
                env = resp[uid].get(b'ENVELOPE')
                if not env:
                    continue
                
                from_str = "Unknown"
                if env.from_ and len(env.from_) > 0:
                    mailbox = env.from_[0].mailbox.decode('utf-8', errors='ignore') if env.from_[0].mailbox else ''
                    host = env.from_[0].host.decode('utf-8', errors='ignore') if env.from_[0].host else ''
                    from_str = f"{mailbox}@{host}"
                
                subject = "No subject"
                if env.subject:
                    subject = env.subject.decode('utf-8', errors='ignore')
                
                date_str = "No date"
                if env.date:
                    date_str = env.date.decode('utf-8', errors='ignore')
                
                email_info = {
                    "uid": uid,
                    "from": from_str,
                    "subject": subject[:80],
                    "date": date_str
                }
                
                results["sample_emails"].append(email_info)
                
                logger.info(f"\n   📬 UID {uid}:")
                logger.info(f"      From: {from_str}")
                logger.info(f"      Subject: {subject[:60]}...")
                
                if 'yape' in from_str.lower() or 'yape' in subject.lower():
                    logger.info(f"      🎯 CONTAINS 'yape'")
            
            except Exception as e:
                logger.warning(f"   ⚠️ Could not inspect UID {uid}: {e}")
        
        # Diagnostic summary
        logger.info("\n" + "=" * 70)
        logger.info("📊 DIAGNOSTIC SUMMARY")
        logger.info("=" * 70)
        
        if results["from_yape_exact"] == 0 and results["from_yape_partial"] == 0:
            results["issue_detected"] = "NO_EMAILS_FROM_YAPE"
            logger.error("❌ ISSUE: No emails from Yape found")
        elif results["combined"] > 0:
            results["issue_detected"] = None
            logger.info(f"✅ SUCCESS: Found {results['combined']} matching emails")
        else:
            results["issue_detected"] = "UNKNOWN"
        
        logger.info("=" * 70)
        
    except Exception as e:
        logger.error(f"❌ Diagnostic failed: {e}", exc_info=True)
        results["issue_detected"] = f"ERROR: {str(e)}"
    
    finally:
        if client:
            try:
                client.logout()
            except:
                pass
    
    return results