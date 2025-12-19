from pymongo import MongoClient
from .config import MONGO_URI, MONGO_DB, MONGO_COLLECTION
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

# Cliente MongoDB global
client = MongoClient(MONGO_URI)

# Base de datos y colecciones por defecto (fallback)
db = client[MONGO_DB]
raw_emails_col = db[MONGO_COLLECTION]
processed_emails_col = db["Transaction_Raw_IMAP"]
email_setup_col = db["email_setups"]
imap_config_col = db["imap_config"]

# ============================================================================
# 🆕 MULTI-TENANT DATABASE ACCESS
# ============================================================================

def get_tenant_db(db_name: str):
    """
    Obtiene la base de datos específica de un tenant.
    
    Args:
        db_name: Nombre de la base de datos (ej: "finanzas_personal_12345678")
    
    Returns:
        Database object
    """
    return client[db_name]

def get_tenant_collections(db_name: str):
    """
    Retorna las colecciones de un tenant específico.
    
    Args:
        db_name: Nombre de la base de datos del tenant
    
    Returns:
        Dict con las colecciones: email_setup_col, imap_config_col, raw_emails_col, processed_emails_col
    """
    tenant_db = get_tenant_db(db_name)
    
    return {
        "email_setup_col": tenant_db["email_setups"],
        "imap_config_col": tenant_db["imap_config"],
        "raw_emails_col": tenant_db["Transaction_Raw_IMAP"],
        "processed_emails_col": tenant_db["Transaction_Processed_IMAP"]
    }

# ============================================================================
# LEGACY FUNCTIONS (mantener para compatibilidad)
# ============================================================================

def is_uid_processed(uid, folder=None, db_name: str = None):
    """
    Verifica si un UID ya fue procesado.
    
    Args:
        uid: UID del email
        folder: Carpeta IMAP (opcional)
        db_name: Nombre de la BD del tenant (si no se provee, usa la BD por defecto)
    """
    if db_name:
        cols = get_tenant_collections(db_name)
        raw_col = cols["raw_emails_col"]
    else:
        raw_col = raw_emails_col
    
    query = {"_id": uid}
    if folder:
        query["folder"] = folder
    
    return raw_col.find_one(query) is not None

def mark_uid_processed(uid, metadata: dict, db_name: str = None):
    """
    Marca un UID como procesado.
    
    Args:
        uid: UID del email
        metadata: Metadata del email
        db_name: Nombre de la BD del tenant (opcional)
    """
    if db_name:
        tenant_db = get_tenant_db(db_name)
        processed_col = tenant_db["processed_uids"]
    else:
        processed_col = db["processed_uids"]
    
    processed_col.update_one(
        {"_id": uid},
        {"$set": {"processed_at": datetime.utcnow()}},
        upsert=True
    )

# ============================================================================
# CONTEXT RESOLUTION
# ============================================================================

class EmailContext:
    """
    Representa el contexto completo necesario para procesar un email.
    """
    def __init__(
        self,
        workspace_id: str = None,
        entity_id: str = None,
        account_id: str = None,
        database_name: str = None,
        bank_name: str = None,
        bank_sender: str = None
    ):
        self.workspace_id = workspace_id
        self.entity_id = entity_id
        self.account_id = account_id
        self.database_name = database_name
        self.bank_name = bank_name
        self.bank_sender = bank_sender
    
    def is_valid(self) -> bool:
        """Verifica si el contexto tiene todos los datos necesarios"""
        required_fields = [
            self.workspace_id,
            self.entity_id,
            self.account_id,
            self.database_name,
            self.bank_sender
        ]
        
        is_complete = all(required_fields)
        
        if not is_complete:
            logger.warning(f"⚠️ Incomplete context: {self.to_dict()}")
        
        return is_complete
    
    def to_dict(self) -> dict:
        """Convierte el contexto a diccionario"""
        return {
            "workspace_id": self.workspace_id,
            "entity_id": self.entity_id,
            "account_id": self.account_id,
            "database_name": self.database_name,
            "bank_name": self.bank_name,
            "bank_sender": self.bank_sender
        }

def resolve_context(email_metadata: dict, db_name: str = None) -> EmailContext:
    """
    Resuelve el contexto completo de un email.
    
    Args:
        email_metadata: Dict con from, subject, etc.
        db_name: Nombre de la BD donde buscar el setup (si no se provee, usa la por defecto)
    
    Returns:
        EmailContext con los datos resueltos
    """
    from_addr = email_metadata.get("from", "")
    
    # Extraer email del formato "Name <email@domain.com>"
    if "<" in from_addr and ">" in from_addr:
        from_addr = from_addr.split("<")[1].split(">")[0].strip()
    
    logger.info(f"🔍 Resolving context for email from: {from_addr}")
    
    # Buscar setup en la BD del tenant (o BD por defecto)
    if db_name:
        cols = get_tenant_collections(db_name)
        setup_col = cols["email_setup_col"]
    else:
        setup_col = email_setup_col
    
    setup = setup_col.find_one({"bank_sender": from_addr})
    
    if not setup:
        logger.warning(f"⚠️ No setup found for sender: {from_addr}")
        return EmailContext(bank_sender=from_addr)
    
    context = EmailContext(
        workspace_id=setup.get("tenant_id"),
        entity_id=setup.get("tenant_detail_id"),
        account_id=setup.get("account_id"),
        database_name=setup.get("db_name", MONGO_DB),
        bank_name=setup.get("bank_name"),
        bank_sender=setup.get("bank_sender")
    )
    
    logger.info(f"✅ Context resolved: {context.to_dict()}")
    return context