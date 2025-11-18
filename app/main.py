from .ingest_email import connect_and_download_pdfs
from .ocr_parser import ocr_pdf_to_json
from .classifier import HybridClassifier
from .db import insert_many

def run_pipeline():
    print("📥 Descargando PDFs desde Gmail...")
    pdfs = connect_and_download_pdfs()
    print(f"Encontrados {len(pdfs)} PDFs")

    print("🔍 Aplicando OCR...")
    movimientos = ocr_pdf_to_json(pdfs)
    print(f"Extraídos {len(movimientos)} movimientos")

    print("🧠 Clasificando...")
    clf = HybridClassifier()
    for m in movimientos:
        m["CATEGORIA"] = clf.classify(m["DESCRIPCION"])

    print("💾 Guardando en MongoDB...")
    inserted = insert_many(movimientos)
    print(f"Insertados {inserted} registros en MongoDB")

if __name__ == "__main__":
    run_pipeline()
