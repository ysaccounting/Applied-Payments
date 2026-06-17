"""
QBO push logic — Receive Payment and Bank Deposit entries.
"""
from datetime import datetime
from collections import defaultdict
from qbo_auth import api_get, api_post, get_valid_token, QBOError


def humanize_error(exc) -> str:
    """Turn any push exception into a short message a non-technical user can act on."""
    if isinstance(exc, QBOError):
        m = (exc.message or "").lower()
        if exc.status == 401 or "token" in m or "authenticationfailed" in m or "unauthorized" in m:
            return "Your QuickBooks connection expired. Please reconnect and try again."
        if "duplicate" in m and ("document" in m or "number" in m):
            return "QuickBooks already has an entry with this reference number."
        if "deposit account" in m or "deposittoaccount" in m:
            return "The bank account on this entry isn't a valid deposit account in QuickBooks."
        if "object not found" in m or "not found" in m:
            return "QuickBooks couldn't find one of the accounts or names on this entry."
        if "stale" in m or "out of date" in m:
            return "This entry was changed in QuickBooks since it loaded. Refresh and try again."
        # QBO's own validation messages are usually already readable
        if exc.message:
            return exc.message
        return "QuickBooks rejected this entry."
    return "Couldn't reach QuickBooks. Please try again."


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
                             "error": f"No customer named \"{row['network']}\" in QuickBooks."})
            continue

        # Look up deposit account (bank account)
        bank_acct = search_account(token_data, realm_id, row["bank_account"])
        if not bank_acct:
            results.append({"status": "error", "memo": row["memo"],
                             "error": f"No bank account named \"{row['bank_account']}\" in QuickBooks."})
            continue

        payload = {
            "CustomerRef": {"value": customer["Id"], "name": customer["DisplayName"]},
            "TotalAmt": row["amount"],
            "TxnDate": _parse_date(row["date"]),
            "PrivateNote": row["memo"],
            "PaymentRefNum": row["deposit_num"],
            "DepositToAccountRef": {"value": bank_acct["Id"], "name": bank_acct["Name"]},
        }
        try:
            result = api_post(token_data, realm_id, "payment?minorversion=65", payload)
            results.append({"status": "ok", "memo": row["memo"],
                             "id": result.get("Payment", {}).get("Id")})
        except Exception as e:
            results.append({"status": "error", "memo": row["memo"], "error": humanize_error(e)})

    return results


def push_bank_deposit(token_data: dict, realm_id: str, summary_data: dict) -> list:
    """
    Push Bank Deposit entries to QBO.
    Groups rows by deposit_num — one QBO Deposit per deposit#.

    All accounts are resolved BEFORE anything is posted: if any line's account
    (or a bank account) can't be found, nothing is sent to QuickBooks and a single
    clear error is returned. This prevents the case where a deposit posts anyway
    while the app reports failure — which led to duplicate deposits on retry.
    """
    results = []
    token_data = get_valid_token(token_data)

    groups = defaultdict(list)
    for row in summary_data["deposit_rows"]:
        groups[row["deposit_num"]].append(row)

    # ── Phase 1: resolve & validate every account up front (post nothing yet) ──
    prepared = []   # (dep_num, date, lines, bank_acct)
    problems = []   # human-readable issues; if any, we abort without posting
    for dep_num, rows in groups.items():
        date = rows[0]["date"]
        network_name = rows[0].get("network", "")
        received_from = search_customer(token_data, realm_id, network_name) if network_name else None

        lines = []
        for row in rows:
            acct_name = str(row.get("account", "")).strip()
            acct = search_account(token_data, realm_id, acct_name) if acct_name else None
            if not acct:
                if acct_name:
                    problems.append(f'No account named "{acct_name}" exists in QuickBooks.')
                else:
                    try:
                        amt_txt = f'${float(row.get("amount", 0)):,.2f}'
                    except Exception:
                        amt_txt = str(row.get("amount"))
                    problems.append(f'A Bank Deposit row has no Company assigned (amount {amt_txt}) — assign it and re-process.')
                continue

            deposit_detail = {
                "AccountRef": {"value": acct["Id"], "name": acct["Name"]},
                "CheckNum": dep_num,
            }
            if received_from:
                deposit_detail["Entity"] = {
                    "value": received_from["Id"],
                    "name": received_from["DisplayName"],
                    "type": "Customer",
                }
            lines.append({
                "Amount": row["amount"],
                "Description": dep_num,
                "DetailType": "DepositLineDetail",
                "DepositLineDetail": deposit_detail,
            })

        bank_name = str(rows[0].get("bank_account", "")).strip()
        bank_acct = search_account(token_data, realm_id, bank_name) if bank_name else None
        if not bank_acct:
            problems.append(f'No bank account named "{rows[0].get("bank_account", "")}" exists in QuickBooks.')

        prepared.append((dep_num, date, lines, bank_acct))

    if problems:
        # Nothing has been posted. Return one clear error so the button stays
        # active and a retry (after the data is fixed) won't create a duplicate.
        unique = list(dict.fromkeys(problems))
        msg = "Deposit not sent — nothing was posted to QuickBooks. " + " ".join(unique) + " Fix this and push again."
        return [{"status": "error", "deposit_num": "deposit", "error": msg}]

    # ── Phase 2: everything resolved — post all deposits, all-or-nothing ──
    # If any deposit fails at post time, roll back (delete) the ones already
    # created in this batch so the entire group either fully posts or not at all.
    posted = []  # (dep_num, id, synctoken)
    try:
        for dep_num, date, lines, bank_acct in prepared:
            if not lines:
                continue
            payload = {
                "TxnDate": _parse_date(date),
                "PrivateNote": dep_num,
                "DepositToAccountRef": {"value": bank_acct["Id"], "name": bank_acct["Name"]},
                "Line": lines,
            }
            result = api_post(token_data, realm_id, "deposit?minorversion=65", payload)
            dep = result.get("Deposit", {})
            posted.append((dep_num, dep.get("Id"), dep.get("SyncToken", "0")))
    except Exception as e:
        base = humanize_error(e)
        # Roll back anything that posted before the failure.
        failed_rollback = []
        for dn, dep_id, sync in posted:
            if not dep_id:
                continue
            try:
                api_post(token_data, realm_id, "deposit?operation=delete&minorversion=65",
                         {"Id": dep_id, "SyncToken": sync or "0"})
            except Exception:
                failed_rollback.append((dn, dep_id))
        if failed_rollback:
            remaining = ", ".join(f"{dn} (Id {i})" for dn, i in failed_rollback)
            msg = (f"{base} Some deposits posted before the error and could NOT be automatically "
                   f"removed: {remaining}. Please delete them in QuickBooks before pushing again.")
        else:
            msg = (f"{base} Nothing was left in QuickBooks — any deposits that posted before the "
                   f"error were rolled back. Fix the issue and push again.")
        return [{"status": "error", "deposit_num": "deposit", "error": msg}]

    # All deposits posted successfully.
    for dep_num, dep_id, sync in posted:
        results.append({"status": "ok", "deposit_num": dep_num, "id": dep_id})
    return results
