import logging

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
