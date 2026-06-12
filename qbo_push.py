"""
QBO push logic — Receive Payment and Bank Deposit entries.
"""
from datetime import datetime
from collections import defaultdict
from qbo_auth import api_get, api_post, get_valid_token


def _parse_date(date_str: str) -> str:
    """Convert mm/dd/yyyy to yyyy-mm-dd for QBO API."""
    try:
        dt = datetime.strptime(date_str, "%m/%d/%Y")
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return date_str


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
            if customer:
                line["DepositLineDetail"]["Entity"] = {
                    "EntityRef": {"value": customer["Id"], "name": customer["DisplayName"]},
                    "Type": "Customer",
                }
            lines.append(line)

        if not lines:
            continue

        bank_acct = search_account(token_data, realm_id, rows[0]["bank_account"])
        if not bank_acct:
            results.append({"status": "error", "deposit_num": dep_num,
                             "error": f"Bank account not found in QBO: {rows[0]['bank_account']}"})
            continue

        payload = {
            "TxnDate": _parse_date(date),
            "PrivateNote": dep_num,
            "DocNumber": dep_num,
            "DepositToAccountRef": {"value": bank_acct["Id"], "name": bank_acct["Name"]},
            "Line": lines,
        }
        try:
            result = api_post(token_data, realm_id, "deposit?minorversion=65", payload)
            results.append({"status": "ok", "deposit_num": dep_num,
                             "id": result.get("Deposit", {}).get("Id")})
        except Exception as e:
            results.append({"status": "error", "deposit_num": dep_num, "error": str(e)})

    return results
