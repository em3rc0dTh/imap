from fastapi import FastAPI, WebSocket
from .db import find

app = FastAPI(title="Finanzas API")

@app.get("/movimientos")
def get_movimientos(limit: int = 50):
    return find(limit)

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    await ws.send_text("Conectado a Finanzas Automate Backend")
    while True:
        data = await ws.receive_text()
        await ws.send_text(f"Echo: {data}")
