import uvicorn
import logging

if __name__ == "__main__":
    # Configurar logging
    logging.basicConfig(
        level=logging.INFO,  # Nivel de log: INFO y superior
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logger = logging.getLogger("uvicorn")

    uvicorn.run(
        "app.api:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="info",  # asegura que uvicorn muestre logs INFO
    )
