import uvicorn
import logging
import threading

if __name__ == "__main__":
    # Configurar logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logger = logging.getLogger("uvicorn")
    
    # ============================================================================
    # 🆕 FASE 4 - Iniciar cliente IMAP en background (si está habilitado)
    # ============================================================================
    from app.config import IMAP_PERSISTENT_MODE
    
    if IMAP_PERSISTENT_MODE:
        logger.info("🔄 IMAP Persistent Mode ENABLED - Starting background client...")
        
        def start_imap_client():
            """Inicia el cliente IMAP en thread separado"""
            try:
                from app.ingest_email import run_imap_client
                run_imap_client()
            except Exception as e:
                logger.error(f"❌ IMAP client crashed: {e}", exc_info=True)
        
        # Iniciar IMAP en thread daemon (se detiene con el proceso principal)
        imap_thread = threading.Thread(
            target=start_imap_client,
            name="IMAPClientThread",
            daemon=True
        )
        imap_thread.start()
        logger.info("✅ IMAP client thread started successfully")
    else:
        logger.info("⏸️ IMAP Persistent Mode DISABLED - Use /ingest endpoint to process emails")
    
    # ============================================================================
    # Iniciar servidor FastAPI
    # ============================================================================
    uvicorn.run(
        "app.api:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="info",
    )