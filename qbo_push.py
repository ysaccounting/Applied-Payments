"""
QBO push logic — Receive Payment and Bank Deposit entries.
"""
from datetime import datetime
from collections import defaultdict
from qbo_auth import api_get, api_post, get_valid_token


# Network short codes for QBO DocNumber (21 char limit)
NETWORK_SHORT_CODES = {
    "stubhub": "SH",
    "vivid seats": "VS",
    "ticket evolution": "TEVO",
    "ticketsnow": "TNOW",
    "gotickets": "GOTIX",
    "seatgeek": "SG",
    "gametime": "GT",
    "ticketnetwork": "TND",
    "mercury": "MERC",
}


def _parse_date(date_str: str) -> str:
    """Convert mm/dd/yyyy to yyyy-mm-dd for QBO API."""
    try:
        dt = datetime.strptime(date_str, "%m/%d/%Y")
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return date_str


def _short_doc_number(deposit_num: str, network: str) -> str:
    """Build a QBO-safe DocNumber (max 21 chars) from deposit# and network.
    Format: <SHORT_CODE>_MM-DD-YY
    Falls back to truncating deposit_num if network not in map.
    """
    import re
    # Extract date portion from deposit_num (MM-DD-YY or MM-DD-YYYY)
    m = re.search(r"(\d{1,2}-\d{1,2}-\d{2,4})$", deposit_num)
    date_part = m.group(1) if m else ""
    # Normalize to MM-DD-YY
    if date_part:
        parts = date_part.split("-")
        if len(parts[2]) == 4:
            date_part = f"{parts[0]}-{parts[1]}-{parts[2][2:]}"

    # Get short code — strip (C), (CAD) etc from network name for lookup
    net_clean = re.sub(r"\s*\(.*?\)", "", network).strip().lower()
    short = NETWORK_SHORT_CODES.get(net_clean, "")
    if not short:
        # fallback: first 4 chars of network
        short = re.sub(r"[^A-Z0-9]", "", network.upper())[:4]

    doc = f"{short}_{date_part}" if date_part else deposit_num[:21]
    return doc[:21]


def search_account(token_data: dict, realm_id: str, name: str):
    """Find a QBO account by name."""
    q = name.replace("'", "\\'")
    result = api_get(token_data, realm_id, f"query?query=SELECT * FROM Account WHERE Name = '{q}'&minorversion=65")
    accounts = result.get("QueryResponse", {}).get("Account", [])
    return accounts[0] if accounts else None


def search_customer(token_data: dict, realm_id: str, name: str):
    """Find a QBO customer by DisplayName."""
    q = name.replace("'", "\\'")
    result = api_get(token_data, realm_id, f"query?query=SELECT * FROM Customer WHERE DisplayName = '{q}'&minorversion=65")
    customers = result.get("QueryResponse", {}).get("Customer", [])
    return customers[0] if customers else None


def get_company_info(token_data: dict, realm_id: str) -> dict:
    """Get basic company info to verify connection."""
    return api_get(token_data, realm_id, "companyinfo/" + realm_id)


def push_receive_payments(token_data: dict, realm_id: str, summary_data: dict) -> list:
    """
    Push Receive Payment entries to QBO.
    Each rp_row: {"memo": str, "amount": float, "date": str, "network": str, "bank_account": str}
    Network is used as the QBO Customer name.
    """
    results = []
    token_data = get_valid_token(token_data)

    for row in summary_data["rp_rows"]:
        # Look up customer using network name
        customer = search_customer(token_data, realm_id, row["network"])
        if not customer:
            results.append({"status": "error", "memo": row["memo"],
                             "error": f"Customer not found in QBO: {row['network']}"})
            continue

        # Look up deposit account (bank account)
        bank_acct = search_account(token_data, realm_id, row["bank_account"])
        if not bank_acct:
            results.append({"status": "error", "memo": row["memo"],
                             "error": f"Bank account not found in QBO: {row['bank_account']}"})
            continue

        payload = {
            "CustomerRef": {"value": customer["Id"], "name": customer["DisplayName"]},
            "TotalAmt": row["amount"],
            "TxnDate": _parse_date(row["date"]),
            "PrivateNote": row["memo"],
            "PaymentRefNum": _short_doc_number(row["deposit_num"], row.get("network", "")),
            "DepositToAccountRef": {"value": bank_acct["Id"], "name": bank_acct["Name"]},
        }
        try:
            result = api_post(token_data, realm_id, "payment?minorversion=65", payload)
            results.append({"status": "ok", "memo": row["memo"],
                             "id": result.get("Payment", {}).get("Id")})
        except Exception as e:
            results.append({"status": "error", "memo": row["memo"], "error": str(e)})

    return results


def push_bank_deposit(token_data: dict, realm_id: str, summary_data: dict) -> list:
    """
    Push Bank Deposit entries to QBO.
    Groups rows by deposit_num — one QBO Deposit per deposit#.
    """
    results = []
    token_data = get_valid_token(token_data)

    groups = defaultdict(list)
    for row in summary_data["deposit_rows"]:
        groups[row["deposit_num"]].append(row)

    for dep_num, rows in groups.items():
        date = rows[0]["date"]
        lines = []
        for row in rows:
            acct = search_account(token_data, realm_id, row["account"])
            if not acct:
                results.append({"status": "error", "deposit_num": dep_num,
                                 "error": f"Account not found in QBO: {row['account']}"})
                continue
            # Look up network as customer for Received From
            network_name = row.get("network", "")
            customer = search_customer(token_data, realm_id, network_name) if network_name else None

            line = {
                "Amount": row["amount"],
                "Description": dep_num,
                "DetailType": "DepositLineDetail",
                "DepositLineDetail": {
                    "AccountRef": {"value": acct["Id"], "name": acct["Name"]},
                },
            }
            lines.append(line)

        if not lines:
            continue

        bank_acct = search_account(token_data, realm_id, rows[0]["bank_account"])
        if not bank_acct:
            results.append({"status": "error", "deposit_num": dep_num,
                             "error": f"Bank account not found in QBO: {rows[0]['bank_account']}"})
            continue

        network_name = rows[0].get("network", "")

        # Look up network as customer for Received From (header level)
        received_from = search_customer(token_data, realm_id, network_name) if network_name else None

        doc_num = _short_doc_number(dep_num, network_name)
        payload = {
            "TxnDate": _parse_date(date),
            "PrivateNote": dep_num,
            "DocNumber": doc_num,
            "DepositToAccountRef": {"value": bank_acct["Id"], "name": bank_acct["Name"]},
            "Line": lines,
        }
        if received_from:
            payload["CustomerRef"] = {
                "value": received_from["Id"],
                "name": received_from["DisplayName"],
            }
        try:
            result = api_post(token_data, realm_id, "deposit?minorversion=65", payload)
            results.append({"status": "ok", "deposit_num": dep_num,
                             "id": result.get("Deposit", {}).get("Id")})
        except Exception as e:
            results.append({"status": "error", "deposit_num": dep_num, "error": str(e)})

    return results
