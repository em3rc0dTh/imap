from pydantic import BaseModel
from .db import email_setup_col, imap_config_col, raw_emails_col, processed_emails_col, get_tenant_db
from fastapi import FastAPI, Query, Header
from fastapi.middleware.cors import CORSMiddleware
from .ingest_email import connect_and_download_pdfs
from datetime import datetime, timedelta
import re
import logging
from bson import ObjectId
from fastapi import HTTPException
from typing import Optional
from pymongo import MongoClient
from .config import MONGO_URI, MONGO_DB
from bs4 import BeautifulSoup
import requests
from datetime import datetime
import dotenv
import os
dotenv.load_dotenv()
IA_EXTRACT_URL = os.getenv("IA_EXTRACT_URL") or "http://localhost:8080/extract"
IA_TIMEOUT = 10


app = FastAPI()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# MongoDB client global
mongo_client = MongoClient(MONGO_URI)

# Colecciones globales (fallback - solo usar si no hay tenant específico)
db = mongo_client[MONGO_DB]
email_setup_col = db["Email_Setup_IMAP"]
imap_config_col = db["IMAP_Config"]
raw_emails_col = db["Transaction_Raw_IMAP"]
processed_emails_col = db["Transaction_Processed_IMAP"]

# === CORS ===
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============================================================================
# 🆕 HELPER: Get tenant-specific collections
# ============================================================================

def get_tenant_collections(db_name: str):
    """
    Retorna las colecciones específicas de un tenant.
    
    Args:
        db_name: Nombre de la base de datos del tenant
    
    Returns:
        dict con las colecciones: email_setup_col, imap_config_col, raw_emails_col, processed_emails_col
    """
    tenant_db = mongo_client[db_name]
    
    return {
        "email_setup_col": tenant_db["email_setups"],
        "imap_config_col": tenant_db["imap_config"],
        "raw_emails_col": tenant_db["Transaction_Raw_IMAP"],
        "processed_emails_col": tenant_db["Transaction_Processed_IMAP"]
    }

# ============================================================================
# Normalize functions (sin cambios)
# ============================================================================

def normalize(email_item):
    return {
        "_id": str(email_item.get("_id")),
        "uid": email_item.get("uid"),
        "message_id": email_item.get("message_id"),
        "from": email_item.get("from"),
        "subject": email_item.get("subject"),
        "date": email_item.get("date"),
        "attachments": email_item.get("pdfs", []),
        "html_body": email_item.get("html_body") or "",
        "body": email_item.get("html_body") or email_item.get("text_body") or "",
        "text_body": email_item.get("text_body") or "",
        "source": email_item.get("source"),
        "transactionVariables": email_item.get("transactionVariables"),
        "transactionType": email_item.get("transactionType"),
    }

def normalize_raw(email_data):
    return {
        "uid": email_data.get("uid"),
        "message_id": email_data.get("message_id"),
        "from": email_data.get("from"),
        "subject": email_data.get("subject"),
        "date": email_data.get("date"),
        "html_body": email_data.get("html_body"),
        "text_body": email_data.get("text_body"),
        "body": email_data.get("html_body") or email_data.get("text_body") or "",
        "pdfs": email_data.get("pdfs", []),
        "source": email_data.get("source"),  
        "fetched_at": email_data.get("fetched_at", datetime.utcnow().isoformat())
    }

# ============================================================================
# Parser functions (sin cambios - mantengo solo las firmas)
# ============================================================================

def extract(body, patterns):
    """Extrae valores usando lista de patrones regex"""
    for p in patterns:
        m = re.search(p, body, re.IGNORECASE)
        if m and m.group(1):
            return m.group(1).strip()
    return "-"

def parse_email_text(body):
    """Parser para emails en texto plano - SIEMPRE retorna dict"""
    if not body:
        return {
            "monto": "-",
            "yapero": "-",
            "origen": "-",
            "fecha": "-",
            "nombreBenef": "-",
            "cuentaBenef": "-",
            "nroOperacion": "-",
            "celularBenef": "-"
        }
    
    try:
        body = body.replace("\r", "").replace("\u00a0", " ").strip()
        
        monto = extract(body, [
            r"Monto(?: Total)?:?\s*S\/\s*([\d,.]+)",
            r"Total del consumo:?\s*S\/\s*([\d,.]+)",
            r"S\/\s*([\d,.]+)\s*(?:PEN)?",
        ])
        
        nroOperacion = extract(body, [
            r"N(?:ú|u)mero de operación:?\s*(\d+)",
            r"N° de operación:?\s*(\d+)",
            r"Nº de operación:?\s*(\d+)",
            r"Código de operación:?\s*(\d+)",
            r"\bOperación[: ]+(\d{5,})",
        ])
        
        fecha = extract(body, [
            r"(\d{1,2}\s+(?:enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|setiembre|octubre|noviembre|diciembre)\s+\d{4}\s*-\s*\d{1,2}:\d{2}\s*(?:a\.?m\.?|p\.?m\.?))",
            r"\bFecha(?: y hora)?:?\s*(.+)",
            r"(\d{1,2}/\d{1,2}/\d{4}\s+\d{1,2}:\d{2}\s*(?:AM|PM))",
            r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})",
        ])
        
        yapero = extract(body, [
            r"Hola[, ]+([A-Za-zÁÉÍÓÚÑáéíóúñ ]+)",
            r"De: ([A-Za-zÁÉÍÓÚÑáéíóúñ ]+)",
            r"Titular:?\s*([A-Za-zÁÉÍÓÚÑáéíóúñ ]+)",
        ])
        
        origen = extract(body, [
            r"Cuenta cargo:?\s*([\d ]+)",
            r"Desde el número:?\s*(\d{6,})",
            r"Tu número de celular:?\s*(\d{6,})",
            r"Cuenta origen:?\s*([\d ]{6,})",
        ])
        
        nombreBenef = extract(body, [
            r"Nombre del Beneficiario:?\s*(.+)",
            r"Enviado a:?\s*(.+)",
            r"Beneficiario:?\s*(.+)",
            r"Para:?\s*([A-Za-zÁÉÍÓÚÑáéíóúñ ]+)",
        ])
        
        cuentaBenef = extract(body, [
            r"Cuenta destino:?\s*([\d ]+)",
            r"Celular del Beneficiario:?\s*(\d{6,})",
            r"Nro destino:?\s*(\d{6,})",
        ])
        
        celularBenef = extract(body, [
            r"celular del beneficiario[:\s]*([x\d]{6,})",
            r"celular[:\s]*([x\d]{6,})",
            r"destinatario[:\s]*([x\d]{6,})",
            r"cuenta destino[:\s]*([x\d]{6,})",
        ])
        
        return {
            "monto": monto,
            "yapero": yapero,
            "origen": origen,
            "fecha": fecha,
            "nombreBenef": nombreBenef,
            "cuentaBenef": cuentaBenef,
            "nroOperacion": nroOperacion,
            "celularBenef": celularBenef,
        }
    
    except Exception as e:
        logger.error(f"Error in parse_email_text: {e}")
        return {
            "monto": "-",
            "yapero": "-",
            "origen": "-",
            "fecha": "-",
            "nombreBenef": "-",
            "cuentaBenef": "-",
            "nroOperacion": "-",
            "celularBenef": "-"
        }


def parse_email_html(body):
    """Parser para emails HTML - SIEMPRE retorna dict"""
    if not body:
        return {
            "monto": "-",
            "yapero": "-",
            "origen": "-",
            "fecha": "-",
            "nombreBenef": "-",
            "cuentaBenef": "-",
            "nroOperacion": "-",
            "celularBenef": "-"
        }
    
    try:
        # TODO: Implementar parseo HTML real
        # Por ahora usar parser de texto como fallback
        logger.warning("parse_email_html not fully implemented, using text parser")
        
        # Intentar extraer texto del HTML
        soup = BeautifulSoup(body, 'html.parser')
        text = soup.get_text(separator='\n', strip=True)
        
        return parse_email_text(text)
    
    except Exception as e:
        logger.error(f"Error in parse_email_html: {e}")
        return {
            "monto": "-",
            "yapero": "-",
            "origen": "-",
            "fecha": "-",
            "nombreBenef": "-",
            "cuentaBenef": "-",
            "nroOperacion": "-",
            "celularBenef": "-"
        }


def parse_interbank(text_body):
    """Parser específico para Interbank - SIEMPRE retorna dict"""
    if not text_body:
        return {
            "monto": "-",
            "yapero": "-",
            "origen": "-",
            "fecha": "-",
            "nombreBenef": "-",
            "cuentaBenef": "-",
            "nroOperacion": "-",
            "tipoOperacion": "-",
            "comision": "-",
            "celularBenef": "-"
        }
    
    try:
        monto = extract(text_body, [r"Monto Total:\s*S\/\s*([\d,.]+)"])
        yapero = extract(text_body, [r"Hola\s+([^\n,]+)"])
        origen = extract(text_body, [r"Cuenta cargo:\s*Cuenta Simple Soles\s*([\d\s]+)"]).replace(" ", "")
        fecha = extract(text_body, [r"(\d{2}\s\w{3}\s\d{4}\s\d{2}:\d{2}\s[AP]M)"])
        nombreBenef = extract(text_body, [r"Cuenta destino:\s*([^\n]+)"])
        cuentaBenef = extract(text_body, [r"Cuenta destino:[^\n]+\n([\d\s]+)"]).replace(" ", "")
        nroOperacion = extract(text_body, [r"Código de operación:\s*(\d+)"])
        tipoOperacion = extract(text_body, [r"Tipo de operación:\s*([^\n]+)"])
        comision = extract(text_body, [r"Comisión:\s*S\/\s*([\d,.]+)"])
        
        return {
            "monto": monto,
            "yapero": yapero,
            "origen": origen,
            "fecha": fecha,
            "nombreBenef": nombreBenef,
            "cuentaBenef": cuentaBenef,
            "nroOperacion": nroOperacion,
            "tipoOperacion": tipoOperacion,
            "comision": comision,
            "celularBenef": "-"
        }
    
    except Exception as e:
        logger.error(f"Error in parse_interbank: {e}")
        return {
            "monto": "-",
            "yapero": "-",
            "origen": "-",
            "fecha": "-",
            "nombreBenef": "-",
            "cuentaBenef": "-",
            "nroOperacion": "-",
            "tipoOperacion": "-",
            "comision": "-",
            "celularBenef": "-"
        }

# ============================================================================
# 🆕 EMAIL SETUP ENDPOINTS - Multi-tenant aware
# ============================================================================

class EmailSetup(BaseModel):
    alias: str | None = None
    bank_name: str
    service_type: str
    bank_sender: str
    tenant_id: Optional[str] = None
    tenant_detail_id: Optional[str] = None
    account_id: Optional[str] = None
    db_name: Optional[str] = None

@app.post("/email/setup")
def create_email_setup(
    setup: EmailSetup,
    x_database_name: str = Header(..., description="Nombre de la base de datos del tenant")
):
    """
    Crea un email setup en la base de datos específica del tenant.
    
    Headers requeridos:
        X-Database-Name: Nombre de la BD del tenant (ej: "finanzas_personal_12345678")
    
    Body:
    {
      "bank_name": "BCP",
      "bank_sender": "noreply@bcp.com.pe",
      "service_type": "email",
      "alias": "BCP Personal",
      "tenant_id": "507f1f77bcf86cd799439011",
      "tenant_detail_id": "507f1f77bcf86cd799439012",
      "account_id": "acc_123",
      "db_name": "finanzas_personal_12345678"
    }
    """
    # Validar contexto multi-tenant completo
    if any([setup.tenant_id, setup.tenant_detail_id, setup.account_id]):
        if not all([setup.tenant_id, setup.tenant_detail_id, setup.account_id, setup.db_name]):
            raise HTTPException(
                status_code=400, 
                detail="If providing tenant context, all fields (tenant_id, tenant_detail_id, account_id, db_name) are required"
            )
    
    # Obtener colecciones del tenant
    cols = get_tenant_collections(x_database_name)
    
    doc = setup.dict()
    doc["created_at"] = datetime.utcnow()
    doc["updated_at"] = datetime.utcnow()
    
    result = cols["email_setup_col"].insert_one(doc)
    
    logger.info(f"✅ Email setup created in DB: {x_database_name}")
    
    return {"status": "success", "id": str(result.inserted_id), "database": x_database_name}

@app.get("/email/setup")
def read_email_setups(
    x_database_name: str = Header(..., description="Nombre de la base de datos del tenant"),
    tenant_id: Optional[str] = Query(None),
    tenant_detail_id: Optional[str] = Query(None)
):
    """
    Lista email setups de un tenant específico.
    
    Headers requeridos:
        X-Database-Name: Nombre de la BD del tenant
    """
    cols = get_tenant_collections(x_database_name)
    
    query = {}
    if tenant_id:
        query["tenant_id"] = tenant_id
    if tenant_detail_id:
        query["tenant_detail_id"] = tenant_detail_id
    
    setups = list(cols["email_setup_col"].find(query))
    for s in setups:
        s["id"] = str(s["_id"])
        del s["_id"]
    
    return setups

@app.get("/email/setup/{setup_id}")
def read_email_setup(
    setup_id: str,
    x_database_name: str = Header(..., description="Nombre de la base de datos del tenant")
):
    cols = get_tenant_collections(x_database_name)
    
    setup = cols["email_setup_col"].find_one({"_id": ObjectId(setup_id)})
    if not setup:
        raise HTTPException(status_code=404, detail="Setup not found")
    
    setup["id"] = str(setup["_id"])
    del setup["_id"]
    return setup

@app.put("/email/setup/{setup_id}")
def update_email_setup(
    setup_id: str,
    setup: EmailSetup,
    x_database_name: str = Header(..., description="Nombre de la base de datos del tenant")
):
    cols = get_tenant_collections(x_database_name)
    
    result = cols["email_setup_col"].update_one(
        {"_id": ObjectId(setup_id)},
        {"$set": {**setup.dict(), "updated_at": datetime.utcnow()}}
    )
    
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Setup not found")
    
    return {"status": "success"}

@app.delete("/email/setup/{setup_id}")
def delete_email_setup(
    setup_id: str,
    x_database_name: str = Header(..., description="Nombre de la base de datos del tenant")
):
    cols = get_tenant_collections(x_database_name)
    
    result = cols["email_setup_col"].delete_one({"_id": ObjectId(setup_id)})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Setup not found")
    
    return {"status": "success"}

# ============================================================================
# 🆕 IMAP CONFIG ENDPOINTS - Multi-tenant aware
# ============================================================================

class ImapConfig(BaseModel):
    user: str
    password: str

@app.post("/imap/config")
def create_imap_config(
    config: ImapConfig,
    x_database_name: str = Header(..., description="Nombre de la base de datos del tenant")
):
    """
    Crea configuración IMAP en la base de datos del tenant.
    
    Headers requeridos:
        X-Database-Name: Nombre de la BD del tenant
    """
    cols = get_tenant_collections(x_database_name)
    
    # Desactivar configuraciones anteriores
    cols["imap_config_col"].update_many({}, {"$set": {"active": False}})
    
    doc = config.dict()
    doc["active"] = True
    doc["created_at"] = datetime.utcnow()
    doc["updated_at"] = datetime.utcnow()
    
    cols["imap_config_col"].insert_one(doc)
    
    logger.info(f"✅ IMAP config created in DB: {x_database_name}")
    
    return {"status": "success", "database": x_database_name}

@app.get("/imap/config")
def get_active_imap_config(
    x_database_name: str = Header(..., description="Nombre de la base de datos del tenant")
):
    """
    Obtiene la configuración IMAP activa del tenant.
    
    Headers requeridos:
        X-Database-Name: Nombre de la BD del tenant
    """
    cols = get_tenant_collections(x_database_name)
    
    config = cols["imap_config_col"].find_one({"active": True}, {"_id": 0})
    return config or {}

@app.get("/imap/config/history")
def get_imap_config_history(
    x_database_name: str = Header(..., description="Nombre de la base de datos del tenant")
):
    cols = get_tenant_collections(x_database_name)
    
    configs = list(cols["imap_config_col"].find({}))
    for c in configs:
        c["id"] = str(c["_id"])
        del c["_id"]
    return configs

@app.put("/imap/config/{config_id}")
def update_imap_config(
    config_id: str,
    config: ImapConfig,
    x_database_name: str = Header(..., description="Nombre de la base de datos del tenant")
):
    cols = get_tenant_collections(x_database_name)
    
    result = cols["imap_config_col"].update_one(
        {"_id": ObjectId(config_id)},
        {"$set": {**config.dict(), "updated_at": datetime.utcnow()}}
    )
    
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Config not found")
    
    return {"status": "success"}

@app.delete("/imap/config/{config_id}")
def delete_imap_config(
    config_id: str,
    x_database_name: str = Header(..., description="Nombre de la base de datos del tenant")
):
    cols = get_tenant_collections(x_database_name)
    
    result = cols["imap_config_col"].delete_one({"_id": ObjectId(config_id)})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Config not found")
    
    return {"status": "success"}

# ============================================================================
# 🆕 INGEST ENDPOINT - Multi-tenant aware
# ============================================================================

@app.get("/ingest")
def ingest(
    x_database_name: str = Header(..., description="Nombre de la base de datos del tenant"),
    limit: int | None = Query(default=None),
    force: bool = Query(default=False),
    date_from: str | None = Query(default=None, description="Fecha inicial (YYYY-MM-DD)"),
    date_to: str | None = Query(default=None, description="Fecha final (YYYY-MM-DD)")
):
    """
    Ingesta emails y los guarda en la base de datos del tenant especificado.
    
    Headers requeridos:
        X-Database-Name: Nombre de la BD del tenant (ej: "finanzas_personal_12345678")
    
    Params:
        - limit: Número máximo de emails a procesar
        - force: Si es true, reprocesa emails ya guardados
        - date_from: Fecha inicial (YYYY-MM-DD)
        - date_to: Fecha final (YYYY-MM-DD)
    """
    logger.info(f"🔄 Starting ingest for database: {x_database_name}")
    
    # Obtener colecciones del tenant
    cols = get_tenant_collections(x_database_name)
    
    # Download emails (ya viene con deduplicación de is_uid_processed)
    raw_emails = connect_and_download_pdfs(
        limit=limit, 
        force=force,
        date_from=date_from,
        date_to=date_to,
        verbose=True,
        db_name=x_database_name
    )
    
    logger.info(f"✅ Downloaded {len(raw_emails)} emails from IMAP")
    
    # === 🔥 OPTIMIZACIÓN: Pre-cargar UIDs y Message-IDs ya procesados ===
    processed_uids = set()
    processed_message_ids = set()
    
    if not force:
        # Cargar UIDs ya guardados en raw_emails
        existing_raw = cols["raw_emails_col"].find(
            {"uid": {"$exists": True}},
            {"uid": 1, "message_id": 1, "_id": 0}
        )
        
        for doc in existing_raw:
            if doc.get("uid"):
                processed_uids.add(doc["uid"])
            if doc.get("message_id"):
                processed_message_ids.add(doc["message_id"])
        
        logger.info(f"📊 Found {len(processed_uids)} UIDs already in raw_emails")
        logger.info(f"📊 Found {len(processed_message_ids)} Message-IDs already in raw_emails")
    
    results = []
    skipped_already_processed = 0
    skipped_parse_error = 0
    skipped_refund = 0
    
    for email_item in raw_emails:
        uid = email_item.get("uid")
        
        try:
            metadata = email_item.get("metadata", {})
            logger.info(
                "📩 IMAP METADATA UID %s → keys=%s",
                uid,
                list(metadata.keys())
            )

            if not metadata:
                logger.warning(f"⚠️ Email UID {uid} missing metadata, skipping...")
                continue
            
            subject = metadata.get("subject", "")
            text_body = metadata.get("text_body")
            html_body = metadata.get("html_body")
            from_addr = metadata.get("from", "")
            message_id = metadata.get("message_id", "unknown")
            
            # === 🔥 DEDUPLICACIÓN ROBUSTA ===
            if not force:
                # Estrategia 1: Verificar UID
                if uid in processed_uids:
                    logger.info(f"⏭️  UID {uid} already processed (found in raw_emails), skipping")
                    skipped_already_processed += 1
                    continue
                
                # Estrategia 2: Verificar Message-ID (más confiable)
                if message_id and message_id != "unknown" and message_id in processed_message_ids:
                    logger.info(f"⏭️  Message-ID {message_id} already processed (UID {uid}), skipping")
                    skipped_already_processed += 1
                    continue
                
                # Estrategia 3: Verificar en BD si no estaba en cache
                # (por si otro proceso lo guardó entre medio)
                exists_in_db = cols["raw_emails_col"].find_one({
                    "$or": [
                        {"uid": uid},
                        {"message_id": message_id} if message_id != "unknown" else {}
                    ]
                }, {"_id": 1})
                
                if exists_in_db:
                    logger.info(f"⏭️  UID {uid} found in DB during processing, skipping")
                    # Agregar a cache para futuras iteraciones
                    processed_uids.add(uid)
                    if message_id != "unknown":
                        processed_message_ids.add(message_id)
                    skipped_already_processed += 1
                    continue
            
            # Validación mínima
            if not any([subject, text_body, html_body, from_addr, message_id]):
                logger.error(f"❌ UID {uid} completely empty, NOT SAVING")
                continue
            
            # Detectar devoluciones
            is_refund = "devolución" in subject.lower() or "devolucion" in subject.lower()
            
            if is_refund:
                logger.info(f"⚠️ UID {uid} is a REFUND → Skipping (not implemented yet)")
                skipped_refund += 1
                continue
            
            # === GUARDAR RAW EMAIL en BD del tenant ===
            raw_data = normalize_raw({"uid": uid, **metadata})
            raw_data["source"] = "imap"
            ai_payload = extract_transaction_via_ai(html_body)

            if ai_payload:
                raw_data["transactionVariables"] = normalize_transaction_variables(
                    ai_payload.get("transactionVariables")
                )
                raw_data["transactionType"] = ai_payload.get("transactionType")
                raw_data["transactionConfidence"] = ai_payload.get("confidence")

            
            try:
                raw_result = cols["raw_emails_col"].insert_one(raw_data)
                raw_id = raw_result.inserted_id
                logger.info(f"✅ Raw email saved: {raw_id} (UID: {uid})")
                
                # Agregar a cache inmediatamente
                processed_uids.add(uid)
                if message_id != "unknown":
                    processed_message_ids.add(message_id)
                
            except Exception as insert_error:
                # Si falla el insert (por duplicate key, etc.)
                logger.warning(f"⚠️ Could not insert UID {uid}: {insert_error}")
                skipped_already_processed += 1
                continue
            
            # === PARSEAR EMAIL ===
            processed_data = None
            
            try:
                if "interbank" in subject.lower() or not html_body:
                    processed_data = parse_interbank(text_body)
                elif html_body:
                    processed_data = parse_email_html(html_body)
                else:
                    processed_data = parse_email_text(text_body)
            except Exception as parse_error:
                logger.error(f"❌ Parser exception for UID {uid}: {parse_error}")
                processed_data = None
            
            # 🔥 VALIDACIÓN: Parser debe retornar dict
            if not isinstance(processed_data, dict):
                logger.warning(f"⚠️ UID {uid} parser returned {type(processed_data)}, skipping processed save")
                skipped_parse_error += 1
                continue
            
            # Agregar metadata
            processed_data["message_id"] = message_id
            processed_data["from"] = from_addr
            processed_data["subject"] = subject
            processed_data["date"] = metadata.get("date")
            processed_data["uid"] = uid
            processed_data["raw_email_id"] = raw_id
            processed_data["processed_at"] = datetime.utcnow()
            processed_data["type"] = "consumo"
            processed_data["source"] = "imap"
            
            # === GUARDAR PROCESSED EMAIL en BD del tenant ===
            try:
                processed_result = cols["processed_emails_col"].insert_one(processed_data)
                logger.info(f"✅ Processed email saved: {processed_result.inserted_id}")
                
                results.append({
                    "uid": uid,
                    "raw_id": str(raw_id),
                    "processed_id": str(processed_result.inserted_id),
                    "subject": subject,
                    "monto": processed_data.get("monto", "-"),
                    "nroOperacion": processed_data.get("nroOperacion", "-"),
                    "type": "consumo",
                    "source": "imap"
                })
            except Exception as proc_insert_error:
                logger.error(f"❌ Could not insert processed email for UID {uid}: {proc_insert_error}")
                continue
        
        except Exception as e:
            logger.error(f"❌ Error processing UID {uid}: {e}", exc_info=True)
            continue
    
    # === RESUMEN FINAL ===
    logger.info("=" * 70)
    logger.info(f"✅ Ingest completed for {x_database_name}")
    logger.info(f"   📊 Downloaded from IMAP: {len(raw_emails)}")
    logger.info(f"   ✅ Successfully processed: {len(results)}")
    logger.info(f"   ⏭️  Skipped (already processed): {skipped_already_processed}")
    logger.info(f"   ⚠️  Skipped (parse error): {skipped_parse_error}")
    logger.info(f"   🔄 Skipped (refunds): {skipped_refund}")
    logger.info("=" * 70)
    
    return {
        "database": x_database_name,
        "success": True,
        "summary": {
            "downloaded": len(raw_emails),
            "processed": len(results),
            "skipped_already_processed": skipped_already_processed,
            "skipped_parse_error": skipped_parse_error,
            "skipped_refund": skipped_refund
        },
        "emails": results
    }

def extract_transaction_via_ai(html: str) -> dict | None:
    if not html or len(html.strip()) < 50:
        return None

    try:
        resp = requests.post(
            IA_EXTRACT_URL,
            json={
                "html": html,
                "formato": "dict",
                "detalles": True
            },
            timeout=IA_TIMEOUT
        )

        if resp.status_code != 200:
            return None

        return resp.json()

    except requests.RequestException as e:
        logger.warning(f"⚠️ IA service unreachable: {e}")
        return None

from datetime import datetime, timedelta

def normalize_transaction_variables(tv: dict | None) -> dict | None:
    if not tv:
        return None

    tv_norm = tv.copy()
    op_date = tv_norm.get("operationDate")

    if not op_date:
        return tv_norm

    try:
        if isinstance(op_date, str):
            dt = datetime.fromisoformat(op_date.replace("Z", "+00:00"))
        elif isinstance(op_date, datetime):
            dt = op_date
        else:
            raise TypeError

        # 🔴 FORZAR corrección: viene como UTC pero es -05
        dt = dt + timedelta(hours=5)

        tv_norm["operationDate"] = dt

    except Exception:
        tv_norm["operationDate"] = None

    return tv_norm

# ============================================================================
# EMAIL LISTING ENDPOINTS - Multi-tenant aware
# ============================================================================

@app.get("/emails")
def get_emails(
    x_database_name: str = Header(..., description="Nombre de la base de datos del tenant")
):
    """Retorna emails raw del tenant"""
    cols = get_tenant_collections(x_database_name)
    
    emails = list(cols["raw_emails_col"].find().sort("date", -1))
    return [normalize(e) for e in emails]

@app.get("/emails/raw")
def get_raw_emails(
    x_database_name: str = Header(..., description="Nombre de la base de datos del tenant")
):
    """Retorna emails raw del tenant"""
    cols = get_tenant_collections(x_database_name)
    
    emails = list(cols["raw_emails_col"].find({}, {"_id": 0}))
    return [normalize(e) for e in emails]

@app.get("/emails/raw/{raw_id}")
def get_raw_email(
    raw_id: str,
    x_database_name: str = Header(..., description="Nombre de la base de datos del tenant")
):
    """Obtiene email raw por ID"""
    cols = get_tenant_collections(x_database_name)
    
    try:
        email = cols["raw_emails_col"].find_one({"_id": ObjectId(raw_id)}, {"_id": 0})
        if not email:
            return {"error": "Email not found"}
        return email
    except Exception as e:
        logger.error(f"Error fetching raw email: {e}")
        return {"error": "Invalid ID format"}