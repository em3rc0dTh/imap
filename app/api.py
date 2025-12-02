from pydantic import BaseModel
from .db import email_setup_col, imap_config_col, raw_emails_col, processed_emails_col
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from .ingest_email import connect_and_download_pdfs
from datetime import datetime, timedelta
import re
import logging
from bson import ObjectId
from fastapi import HTTPException
from typing import Optional

app = FastAPI()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# === CORS ===
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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
    }

def normalize_raw(email_data):
    """Normaliza el email raw para guardar en BD"""
    return {
        "uid": email_data.get("uid"),
        "message_id": email_data.get("message_id"),
        "from": email_data.get("from"),
        "subject": email_data.get("subject"),
        "date": email_data.get("date"),
        "html_body": email_data.get("html_body"),
        "text_body": email_data.get("text_body"),
        "pdfs": email_data.get("pdfs", []),
        "fetched_at": email_data.get("fetched_at", datetime.utcnow().isoformat())
    }

def extract(body, patterns):
    """Extrae valores usando lista de patrones regex"""
    for p in patterns:
        m = re.search(p, body, re.IGNORECASE)
        if m and m.group(1):
            return m.group(1).strip()
    return "-"

def parse_email_text(body):
    """Parser para emails en texto plano (similar a tu parseEmailText)"""
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

def parse_email_html(body):

    def extract(html, patterns):
        for p in patterns:
            m = re.search(p, html, re.S | re.I)
            if m and m.group(1):
                return m.group(1).strip()
        return "-"

    # -------------------------------
    # MONEDA
    # -------------------------------
    moneda_raw = extract(body, [
        r"(S\/)\s*[\d,.]+",
        r"(USD)\s*[\d,.]+",
        r"(\$)\s*[\d,.]+",
    ])

    tipo_moneda = "-"
    if moneda_raw != "-":
        tipo_moneda = "PEN" if "S/" in moneda_raw else "USD"

    # -------------------------------
    # MONTO
    # -------------------------------
    monto = extract(body, [
        r'<td[^>]*class="soles-amount"[^>]*>\s*([\d.]+)\s*</td>',
        r'<strong>Monto de yapeo\*<\/strong>[\s\S]*?<td.*?font-size:50px[^"]*">([\d,.]+)<\/td>',
        r'Monto de Yapeo<\/td>\s*<td[^>]*>\s*S\/\s*([\d,.]+)',
        r'Total del consumo<\/td>.*?<b>S\/\s*([\d,.]+)<\/b>',
        r'Moneda y monto:<\/span>[\s\S]*?<span>S\/<\/span>\s*<span>([\d,.]+)<\/span>',
        r'Monto Total:<\/span>[\s\S]*?<span>S\/<\/span>\s*<span>([\d,.]+)<\/span>',
        r'Total del consumo\s*S\/\s*([\d,.]+)',
        r'S\/\s*([\d,.]+)<\/b>',
    ])

    # -------------------------------
    # YAPERO / TITULAR
    # -------------------------------
    yapero = extract(body, [
        r'Yapero\s*<\/td>\s*<td.*?>(.*?)<\/td>',
        r'Hola <b>(.*?)<\/b>',
        r'Hola\s*<span>([^<]+)<\/span>',
        r'Cuenta destino:<\/span>.*?<span>([^<]+)<\/span>',
    ])

    # -------------------------------
    # ORIGEN
    # -------------------------------
    origen = extract(body, [
        r'Tu número de celular\s*<\/td>\s*<td.*?>(.*?)<\/td>',
        r'Número de Tarjeta de Crédito<\/td>[\s\S]*?<b>(.*?)<\/b>',
        r'Cuenta cargo:<\/span>.*?<span>.*?<\/span><br.*?><span>(.*?)<\/span>',
        r'Cuenta cargo:<\/span>[\s\S]*?<span>(Cuenta Simple|Cuenta Corriente|Cuenta Interbank)<\/span>.*?<span>([\d\s]+)<\/span>',
    ])

    # -------------------------------
    # FECHA
    # -------------------------------
    fecha = extract(body, [
        r'Fecha y Hora de la operación.*?<td.*?>(.*?)<\/td>',
        r'Fecha y hora<\/td>[\s\S]*?<b><a.*?>(.*?)<\/a><\/b>',
        r'Date:<\/td>[\s\S]*?<b><a.*?>(.*?)<\/a><\/b>',
        r'Fecha y hora\s*[:\-]?\s*([\d]{1,2}.*?\d{4}.*?(AM|PM))',
    ])

    # -------------------------------
    # NOMBRE BENEFICIARIO
    # -------------------------------
    nombreBenef = extract(body, [
        r'Nombre del Beneficiario.*?<td.*?>(.*?)<\/td>',
        r'Empresa<\/td>[\s\S]*?<b>(.*?)<\/b>',
        r'Cuenta destino:<\/span>.*?<span>([^<]+)<\/span>',
    ])

    # -------------------------------
    # CUENTA BENEF
    # -------------------------------
    cuentaBenef = extract(body, [
        r'Celular del Beneficiario.*?<td.*?>(.*?)<\/td>',
        r'Cuenta destino:<\/span>.*?<span>.*?<\/span><br.*?><span>(.*?)<\/span>',
    ])

    # -------------------------------
    # NRO OPERACIÓN
    # -------------------------------
    nroOperacion = extract(body, [
        r'Nº de operación.*?<td.*?>(.*?)<\/td>',
        r'Número de operación<\/td>[\s\S]*?<b><a.*?>(.*?)<\/a><\/b>',
        r'Código de operación:<\/span>.*?<span>([\d]+)<\/span>',
        r'Código de operación:\s+(\d+)',
        r'N° de operación<\/td>[\s\S]*?<td.*?>\s*([\d]+)\s*<\/td>',
    ])

    # -------------------------------
    # CELULAR BENEF (fallback)
    # -------------------------------
    celularBenef = extract(body, [
        r'Celular del Beneficiario.*?<td.*?>(.*?)<\/td>',
        r'Cuenta destino:<\/span>.*?<span>(?:.*?)<\/span><br.*?><span>(.*?)<\/span>',
        r'Nombre del Beneficiario.*?<td.*?>(.*?)<\/td>',
        r'Empresa<\/td>[\s\S]*?<b>(.*?)<\/b>',
        r'Cuenta destino:<\/span>.*?<span>([^<]+)<\/span>',
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
        "tipo_moneda": tipo_moneda,
    }

def parse_interbank(text_body):
    """Parser específico para Interbank"""
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

def match_refund_to_consumption(refund, db):
    monto = refund["amount"]
    fecha_dev = refund["date"]

    # Buscar consumos con ese monto, últimos 3 días
    consumos = db.transactions.find({
        "type": "consumo",
        "amount": monto,
        "status": "pending"
    })

    if not consumos:
        return None

    # Elegir el más cercano en fecha
    mejor = None
    mejor_diff = 999

    for c in consumos:
        diff = abs((fecha_dev - c["date"]).days)
        if diff <= 2 and diff < mejor_diff:
            mejor = c
            mejor_diff = diff

    return mejor

def process_transaction(tx):
    if tx["type"] == "devolucion":
        consumo = match_refund_to_consumption(tx, db)

        if consumo:
            # enlazar
            db.transactions.update_one(
                {"id": consumo["id"]},
                {"$set": {"status": "refunded", "refund_id": tx["uid"]}}
            )
            logger.info(f"🔄 Devolución UID {tx['uid']} enlazada con consumo ID {consumo['id']}")
        else:
            logger.warning(f"⚠ No se encontró consumo para devolución UID {tx['uid']}")
        return

    if tx["type"] == "consumo":
        db.transactions.insert_one({
            "uid": tx["uid"],
            "type": "consumo",
            "amount": tx["amount"],
            "date": tx["date"],
            "status": "pending"
        })
        logger.info(f"💳 Consumo guardado UID {tx['uid']}")

def should_skip_consumption(subject, html_body, metadata, raw_emails_col):
    """
    Retorna True si el consumo debe saltarse porque tiene devolución matching (<2 días y mismo monto).
    """

    # 1. Detectar monto del consumo
    try:
        parsed = parse_email_html(html_body)
    except:
        parsed = {}

    amount_str = parsed.get("monto", "0").replace(",", "")
    try:
        amount = float(amount_str)
    except:
        amount = 0.0

    # 2. Obtener la fecha del email (metadata["date"])
    raw_date = metadata.get("date")
    if not raw_date:
        return False

    # 🔥 FIX: Convertir string → datetime (intenta varios formatos)
    email_date = None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M:%S", "%a, %d %b %Y %H:%M:%S %z", "%Y-%m-%d"):
        try:
            email_date = datetime.strptime(raw_date, fmt)
            break
        except:
            continue
    
    if email_date is None:
        # último intento usando parser flexible
        try:
            from dateutil import parser
            email_date = parser.parse(raw_date)
        except:
            return False
    
    # 3. Buscar devoluciones matching
    refund = raw_emails_col.find_one({
        "subject": {"$regex": "devoluci[oó]n", "$options": "i"},
        "monto": amount,
        "date": {
            # 🔥 Ahora seguro funciona porque email_date es datetime
            "$gte": email_date - timedelta(days=2),
            "$lte": email_date + timedelta(days=2),
        }
    })

    return refund is not None

class EmailSetup(BaseModel):
    alias: str | None = None
    bank_name: str
    service_type: str
    bank_sender: str

@app.post("/email/setup")
def create_email_setup(setup: EmailSetup):
    doc = setup.dict()
    doc["created_at"] = datetime.utcnow()
    doc["updated_at"] = datetime.utcnow()
    result = email_setup_col.insert_one(doc)
    return {"status": "success", "id": str(result.inserted_id)}

@app.get("/email/setup")
def read_email_setups():
    setups = list(email_setup_col.find({}))
    for s in setups:
        s["id"] = str(s["_id"])
        del s["_id"]
    return setups

@app.get("/email/setup/{setup_id}")
def read_email_setup(setup_id: str):
    setup = email_setup_col.find_one({"_id": ObjectId(setup_id)})
    if not setup:
        raise HTTPException(status_code=404, detail="Setup not found")
    setup["id"] = str(setup["_id"])
    del setup["_id"]
    return setup

@app.put("/email/setup/{setup_id}")
def update_email_setup(setup_id: str, setup: EmailSetup):
    result = email_setup_col.update_one(
        {"_id": ObjectId(setup_id)},
        {"$set": {**setup.dict(), "updated_at": datetime.utcnow()}}
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Setup not found")
    return {"status": "success"}

@app.delete("/email/setup/{setup_id}")
def delete_email_setup(setup_id: str):
    result = email_setup_col.delete_one({"_id": ObjectId(setup_id)})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Setup not found")
    return {"status": "success"}

class ImapConfig(BaseModel):
    user: str
    password: str

@app.post("/imap/config")
def create_imap_config(config: ImapConfig):
    imap_config_col.update_many({}, {"$set": {"active": False}})
    doc = config.dict()
    doc["active"] = True
    doc["created_at"] = datetime.utcnow()
    doc["updated_at"] = datetime.utcnow()
    imap_config_col.insert_one(doc)
    return {"status": "success"}

@app.get("/imap/config")
def get_active_imap_config():
    config = imap_config_col.find_one({"active": True}, {"_id": 0})
    return config or {}

@app.get("/imap/config/history")
def get_imap_config_history():
    configs = list(imap_config_col.find({}))
    for c in configs:
        c["id"] = str(c["_id"])
        del c["_id"]
    return configs

@app.put("/imap/config/{config_id}")
def update_imap_config(config_id: str, config: ImapConfig):
    result = imap_config_col.update_one(
        {"_id": ObjectId(config_id)},
        {"$set": {**config.dict(), "updated_at": datetime.utcnow()}}
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Config not found")
    return {"status": "success"}

@app.delete("/imap/config/{config_id}")
def delete_imap_config(config_id: str):
    result = imap_config_col.delete_one({"_id": ObjectId(config_id)})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Config not found")
    return {"status": "success"}

@app.get("/ingest")
def ingest(
    limit: int | None = Query(default=None),
    force: bool = Query(default=False),
    date_from: str | None = Query(default=None, description="Fecha inicial (YYYY-MM-DD)"),
    date_to: str | None = Query(default=None, description="Fecha final (YYYY-MM-DD)")
):
    """
    Ingesta emails desde IMAP, guarda raw + processed
    
    Params:
        - limit: Número máximo de emails a procesar
        - force: Si es true, reprocesa emails ya guardados
        - date_from: Fecha inicial en formato YYYY-MM-DD (ej: 2024-01-15)
        - date_to: Fecha final en formato YYYY-MM-DD (ej: 2024-12-31)
    """
    raw_emails = connect_and_download_pdfs(
        limit=limit, 
        force=force,
        date_from=date_from,
        date_to=date_to,
        verbose=True
    )
    logger.info(f"✅ Descargados {len(raw_emails)} emails")
    
    results = []
    
    for email_item in raw_emails:
        try:
            uid = email_item.get("uid")
            metadata = email_item.get("metadata", {})
            
            if not metadata:
                logger.warning(f"⚠️ Email UID {uid} sin metadata, saltando...")
                continue
            
            subject = metadata.get("subject")
            text_body = metadata.get("text_body")
            html_body = metadata.get("html_body")
            from_addr = metadata.get("from")
            message_id = metadata.get("message_id")
            
            # Si TODO está vacío, descartar
            if not any([subject, text_body, html_body, from_addr, message_id]):
                logger.error(f"❌ Email UID {uid} completamente vacío, NO GUARDANDO")
                logger.error(f"   Metadata completa: {metadata}")
                continue
            
            # Detectar devolución (ANTES DE GUARDAR RAW)
            is_refund = False
            amount = 0.0
            if subject:
                low = subject.lower()
                if "devolución" in low or "devolucion" in low:
                    is_refund = True
                    amount = parse_email_html(html_body).get("monto", "0").replace(",", "")
            
            if is_refund:
                logger.info(f"⚠️ UID {uid} es una devolución → NO se guarda RAW ni processed")

                # Seleccionar parser
                if "interbank" in subject.lower() or not html_body:
                    processed_data = parse_interbank(text_body)
                elif html_body:
                    processed_data = parse_email_html(html_body)
                else:
                    processed_data = parse_email_text(text_body)

                # Agregar metadata
                processed_data["message_id"] = metadata.get("message_id", "")
                processed_data["from"] = metadata.get("from", "")
                processed_data["subject"] = subject
                processed_data["date"] = metadata.get("date")
                processed_data["uid"] = uid
                processed_data["monto"] = amount

                logger.info(f"📊 Procesando DEVOLUCIÓN UID {uid}: {processed_data}")
                
                # Registrar en transacciones (devuelve / empareja)
                process_transaction(processed_data)

                continue  # No seguir con RAW/PROCESSED
            
            # --- DESCARTAR CONSUMO SI TIENE DEVOLUCIÓN MATCHING < 2 DÍAS ---
            if should_skip_consumption(subject, html_body, metadata, raw_emails_col):
                logger.info(f"⏩ UID {uid}: consumo saltado (tiene devolución matching <2 días). NO guardar RAW/PROCESSED.")
                continue


            if processed_data.get("monto") == amount:
                logger.info(f"✅ UID {uid} monto coincide con devolución registrada")
                continue
            
            # === Guardar RAW ===
            raw_data = normalize_raw({"uid": uid, **metadata})
            raw_result = raw_emails_col.insert_one(raw_data)
            raw_id = raw_result.inserted_id
            
            logger.info(f"✅ Raw email guardado con ID: {raw_id} (UID: {uid})")

            # Marcar UID procesado
            try:
                from .db import mark_uid_processed
                mark_uid_processed(uid, metadata)
                logger.info(f"✅ UID {uid} marcado como procesado")
            except Exception as e:
                logger.warning(f"⚠️ Error marcando como procesado: {e}")
            
            # === PROCESAR EMAIL ===
            # El subject/text/html ya estaban obtenidos arriba, no es necesario reobtenerlos
            
            if "interbank" in subject.lower() or not html_body:
                processed_data = parse_interbank(text_body)
            elif html_body:
                processed_data = parse_email_html(html_body)
            else:
                processed_data = parse_email_text(text_body)
            
            # Metadata adicional
            processed_data["message_id"] = metadata.get("message_id", "unknown")
            processed_data["from"] = metadata.get("from", "")
            processed_data["subject"] = subject
            processed_data["date"] = metadata.get("date")
            processed_data["uid"] = uid
            processed_data["raw_email_id"] = raw_id
            processed_data["processed_at"] = datetime.utcnow()

            logger.info(f"📊 Datos procesados UID {uid}: {processed_data}")
            
            # === Guardar processed ===
            processed_result = processed_emails_col.insert_one(processed_data)
            logger.info(f"✅ Processed email guardado con ID: {processed_result.inserted_id}")
            
            results.append({
                "uid": uid,
                "raw_id": str(raw_id),
                "processed_id": str(processed_result.inserted_id),
                "subject": subject,
                "monto": processed_data.get("monto", "-"),
                "nroOperacion": processed_data.get("nroOperacion", "-")
            })
        
        except Exception as e:
            logger.error(f"❌ Error procesando email UID {uid}: {e}", exc_info=True)
            continue

    logger.info(f"✅ Ingesta completada: {len(results)} emails procesados")
    
    return {
        "count": len(results),
        "emails": results
    }

@app.get("/emails")
def get_emails():
    """
    Retorna emails procesados
    """
    emails = list(
        raw_emails_col.find().sort("date", -1)   # ⬅️ Ordena por fecha (recientes primero)
    )
    return [normalize(e) for e in emails]

@app.get("/emails/raw")
def get_raw_emails():
    """
    Retorna emails raw
    """
    emails = list(raw_emails_col.find({}, {"_id": 0}))
    return [normalize(e) for e in emails]

@app.get("/emails/raw/invalid")
def get_invalid_raw_emails():
    """
    Retorna emails raw que están vacíos o sin datos
    """
    try:
        # Buscar emails donde subject, from, text_body y html_body sean todos null/vacíos
        invalid = list(raw_emails_col.find({
            "$and": [
                {"subject": {"$in": [None, ""]}},
                {"from": {"$in": [None, ""]}},
                {"text_body": {"$in": [None, ""]}},
                {"html_body": {"$in": [None, ""]}}
            ]
        }, {"_id": 0}).limit(50))
        
        logger.info(f"⚠️ Encontrados {len(invalid)} emails vacíos")
        return {
            "count": len(invalid),
            "invalid_emails": invalid
        }
    except Exception as e:
        logger.error(f"❌ Error: {e}")
        return {"error": str(e)}
def get_raw_email(raw_id: str):
    """
    Obtiene el email raw por su ID
    """
    try:
        email = raw_emails_col.find_one({"_id": ObjectId(raw_id)}, {"_id": 0})
        if not email:
            return {"error": "Email not found"}
        return email
    except Exception as e:
        logger.error(f"Error fetching raw email: {e}")
        return {"error": "Invalid ID format"}