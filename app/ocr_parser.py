import os, re, json
from pdf2image import convert_from_path
import pytesseract
from .config import TESSERACT_CMD, JSON_OUTPUT

pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD

LINE_RE = re.compile(
    r"^(\d{2}\w{3})\s+(\d{2}\w{3})\s+(.+?)\s+([\d,]+\.\d{2})?\s*([\d,]+\.\d{2})?$",
    re.IGNORECASE
)

def to_float(v):
    return float(v.replace(",", "")) if v else None

def ocr_pdf_to_json(pdf_paths):
    data = []
    for pdf in pdf_paths:
        pages = convert_from_path(pdf, dpi=300)
        for page in pages:
            text = pytesseract.image_to_string(page, lang="spa")
            for line in text.splitlines():
                match = LINE_RE.match(line.strip())
                if match:
                    data.append({
                        "FECHA_PROC": match.group(1),
                        "FECHA_VALOR": match.group(2),
                        "DESCRIPCION": match.group(3).strip(),
                        "CARGOS_DEBE": to_float(match.group(4)),
                        "ABONOS_HABER": to_float(match.group(5))
                    })
    os.makedirs(os.path.dirname(JSON_OUTPUT), exist_ok=True)
    with open(JSON_OUTPUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)
    return data
