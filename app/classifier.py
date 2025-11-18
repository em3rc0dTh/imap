from sentence_transformers import SentenceTransformer
from sklearn.neighbors import NearestNeighbors
import numpy as np
import requests
import os

# --- Configuración desde variables de entorno ---
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "none")
OLLAMA_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1:8b")

# --- Modelo local de embeddings ---
model = SentenceTransformer("all-MiniLM-L6-v2")

class HybridClassifier:
    def __init__(self):
        self.examples = [
            ("yape", "transferencia_personal"),
            ("plin", "transferencia_personal"),
            ("impuesto", "impuesto"),
            ("spotify", "servicio_consumo"),
            ("google", "servicio_consumo"),
            ("otros.pag", "pago_sueldo"),
        ]
        self.texts = [x[0] for x in self.examples]
        self.labels = [x[1] for x in self.examples]
        self.embeddings = model.encode(self.texts, convert_to_numpy=True)
        self.nn = NearestNeighbors(n_neighbors=1, metric="cosine").fit(self.embeddings)

    def classify(self, text):
        emb = model.encode([text], convert_to_numpy=True)
        _, idx = self.nn.kneighbors(emb)
        initial_category = self.labels[idx[0][0]]

        # Si Ollama está activo, pedir una segunda opinión
        if LLM_PROVIDER.lower() == "ollama":
            try:
                prompt = f"""Clasifica esta transacción en una categoría:
transferencia_personal, impuesto, retiro_efectivo, servicio_consumo, pago_sueldo u otros.

Transacción: "{text}"
Categoría sugerida: {initial_category}

Responde solo con una palabra de categoría."""
                r = requests.post(
                    f"{OLLAMA_URL}/api/generate",
                    json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False},
                    timeout=30,
                )
                if r.ok:
                    respuesta = r.json().get("response", "").strip().split()[0].lower()
                    return respuesta or initial_category
            except Exception as e:
                print(f"[Ollama] Error: {e}")
        return initial_category
