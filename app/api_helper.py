import logging
from datetime import datetime

logger = logging.getLogger(__name__)


def match_and_update_accounts(tv: dict, accounts_col) -> dict:
    """
    Busca cuentas en la BD que coincidan con los últimos 3 dígitos
    de originAccount o destinationAccount y actualiza el número completo.
    """
    if not tv:
        return tv

    # Campos a verificar
    fields_to_check = ["originAccount", "destinationAccount"]

    # Obtener todas las cuentas (o filtrar optimizadamente si son muchas)
    # Por ahora traemos todas para iterar en memoria o hacemos queries.
    # Dado que es "last 3 digits", regex es buena opción en query.

    for field in fields_to_check:
        account_val = tv.get(field)

        if account_val and isinstance(account_val, str) and len(account_val) >= 3:
            last_3 = account_val[-3:]

            # Buscar cuenta que termine en estos 3 dígitos
            # Regex: ".*123$"
            matched_account = accounts_col.find_one(
                {"account_number": {"$regex": f".*{last_3}$"}}
            )

            if matched_account:
                full_number = matched_account.get("account_number")
                if full_number:
                    logger.info(
                        f"✅ Account MATCH: {account_val} -> {full_number} (Field: {field})"
                    )
                    tv[field] = full_number

    return tv


def parse_amount(value):
    """Parsea monto a float removiendo símbolos de moneda y comas"""
    if not value:
        return 0.0

    if isinstance(value, (int, float)):
        return float(value)

    try:
        # Remover S/, PEN, espacios
        val = (
            str(value)
            .upper()
            .replace("S/", "")
            .replace("PEN", "")
            .replace("USD", "")
            .replace("$", "")
            .strip()
        )
        # Remover comas (asumiendo que son separadores de miles si hay punto decimal, o decimales si no hay punto)
        # Caso simple: 1,200.50 -> 1200.50
        val = val.replace(",", "")
        return float(val)
    except Exception:
        return 0.0


def parse_date(value):
    """Intenta parsear fecha de string a datetime"""
    if not value:
        return None

    try:
        # Intentar ISO format primero
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except:
        pass

    # Formatos comunes
    formats = [
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y %H:%M",
        "%d/%m/%Y",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%d %b %Y %H:%M",  # 19 Feb 2024 14:30
    ]

    for fmt in formats:
        try:
            return datetime.strptime(str(value), fmt)
        except:
            continue

    return None
