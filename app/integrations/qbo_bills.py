"""
QuickBooks bills reader.

Replaces the spreadsheet snapshot with a live query. The important part is the
filter: only bills with Balance > 0 are candidates, because a bill that's
already been paid should never be offered as a match. QBO has no "paid" boolean
-- Balance is how you derive it.

The other half of the job is parsing the memo. Y&S bill memos look like:

    Philadelphia Union / arthurmitzel1@outlook.com / Leagues Cups (YS-Seatgeek2)
    AC/DC / danielsgregory874@gmail.com /  47-38086/PHI (YS-Seatgeek2)

so the event, buyer email, descriptor and source account all have to be pulled
out of one free-text field. Measured on 1,939 real bills: email present on 94%,
an order-like number on 12%, source account on 98%.
"""

from __future__ import annotations

import logging
import re
from datetime import date, timedelta
from decimal import Decimal
from typing import Iterator

import httpx
from sqlalchemy.orm import Session

from ..config import settings
from . import qbo_oauth, qbo_tokens

log = logging.getLogger(__name__)

MINOR_VERSION = "75"

EMAIL_RE = re.compile(r"[\w.\-]+@[\w.\-]+\.\w+")
ORDER_RE = re.compile(r"\b(\d{2,}-\d{2,}[A-Z0-9/\-]*)")
PAREN_RE = re.compile(r"\(([^)]*)\)\s*$")


class QboReadError(RuntimeError):
    pass


def _query(db: Session, realm_id: str, sql: str) -> dict:
    """Run a QBO SQL-like query, refreshing the token if needed."""
    qbo_tokens.assert_realm_allowed(realm_id)
    token = qbo_tokens.get_access_token(db, realm_id)
    url = f"{qbo_oauth.api_base()}/v3/company/{realm_id}/query"
    r = httpx.get(
        url,
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/json"},
        params={"query": sql, "minorversion": MINOR_VERSION},
        timeout=60,
    )
    if r.status_code == 401:
        raise QboReadError("401 from QBO — token rejected; try reconnecting")
    if r.status_code != 200:
        raise QboReadError(f"query failed ({r.status_code}): {r.text[:300]}")
    return r.json().get("QueryResponse", {})


def parse_memo(memo: str) -> dict:
    """Pull the matchable identifiers out of a bill memo."""
    memo = memo or ""
    # Split on " / " (with spaces), not bare "/" -- otherwise event names that
    # contain a slash ("AC/DC") get truncated to "AC".
    parts = [p.strip() for p in re.split(r"\s+/\s+", memo)] if " / " in memo \
        else [p.strip() for p in memo.split("/")]
    em = EMAIL_RE.search(memo)
    order = ORDER_RE.search(memo)
    paren = PAREN_RE.search(memo)
    tail = paren.group(1).strip() if paren else None
    return {
        "event": parts[0] if parts else "",
        "email": em.group(0).lower() if em else None,
        "order_number": order.group(1) if order else None,
        # The trailing parenthetical is either the source account
        # ("YS-Seatgeek2") or a PO date note -- they're not the same thing.
        "source_account": tail if tail and not tail.lower().startswith("po created") else None,
        "po_created": tail if tail and tail.lower().startswith("po created") else None,
    }


def fetch_open_bills(db: Session, realm_id: str,
                     since: date | None = None,
                     page_size: int = 500) -> Iterator[dict]:
    """Yield open bills (Balance > 0), newest first, paging until exhausted.

    `since` bounds the window -- there's no value in offering a reviewer a bill
    from three years ago as a candidate for today's charge.
    """
    since = since or (date.today() - timedelta(days=120))
    start = 1
    total = 0
    while True:
        sql = (
            "SELECT * FROM Bill "
            f"WHERE Balance > '0' AND TxnDate >= '{since.isoformat()}' "
            f"ORDERBY TxnDate DESC "
            f"STARTPOSITION {start} MAXRESULTS {page_size}"
        )
        resp = _query(db, realm_id, sql)
        bills = resp.get("Bill", [])
        if not bills:
            break
        for b in bills:
            yield _map_bill(b)
        total += len(bills)
        if len(bills) < page_size:
            break
        start += page_size
    log.info("fetched %d open bills for realm %s", total, realm_id)


def _map_bill(b: dict) -> dict:
    memo = b.get("PrivateNote") or ""
    lines = b.get("Line", []) or []
    parsed = parse_memo(memo)

    # Line-level description sometimes carries the detail when PrivateNote is
    # thin; fall back to it for parsing.
    if not parsed["email"] and lines:
        desc = " ".join(str(l.get("Description") or "") for l in lines)
        parsed = parse_memo(memo + " " + desc) if desc.strip() else parsed

    # Ticket count, where the line carries one -- needed to prove a rounding
    # gap is TicketVault's round(unit x qty) artifact rather than a real
    # discrepancy.
    qty = None
    if len(lines) == 1:
        for key in ("AccountBasedExpenseLineDetail", "ItemBasedExpenseLineDetail"):
            detail = lines[0].get(key) or {}
            if detail.get("Qty"):
                try:
                    qty = int(float(detail["Qty"]))
                except (TypeError, ValueError):
                    qty = None
                break

    return {
        "bill_id": b.get("Id"),
        "quantity": qty,
        "doc_number": b.get("DocNumber") or "",
        "vendor": (b.get("VendorRef") or {}).get("name", ""),
        "vendor_id": (b.get("VendorRef") or {}).get("value", ""),
        "txn_date": b.get("TxnDate"),
        "due_date": b.get("DueDate"),
        "total": Decimal(str(b.get("TotalAmt", 0))),
        "balance": Decimal(str(b.get("Balance", 0))),
        "line_count": len(lines),
        "memo": memo,
        "sync_token": b.get("SyncToken"),   # required for any later update
        **parsed,
    }


def count_open_bills(db: Session, realm_id: str) -> int:
    resp = _query(db, realm_id, "SELECT COUNT(*) FROM Bill WHERE Balance > '0'")
    return int(resp.get("totalCount", 0))


def list_bank_accounts(db: Session, realm_id: str) -> list[dict]:
    """Bank and Credit Card accounts — the ones a card charge can post to."""
    resp = _query(
        db, realm_id,
        "SELECT * FROM Account WHERE AccountType IN "
        "('Bank','Credit Card') MAXRESULTS 200")
    out = []
    for a in resp.get("Account", []):
        out.append({
            "id": a.get("Id"),
            "name": a.get("Name"),
            "type": a.get("AccountType"),
            "subtype": a.get("AccountSubType", ""),
            "active": a.get("Active", True),
            "realm_id": realm_id,
        })
    return sorted(out, key=lambda x: (x["type"], x["name"] or ""))


def list_expense_accounts(db: Session, realm_id: str) -> list[dict]:
    """Accounts a charge can be coded to — the 'Category' in QuickBooks terms."""
    resp = _query(
        db, realm_id,
        "SELECT * FROM Account WHERE AccountType IN "
        "('Expense','Other Expense','Cost of Goods Sold','Other Current Asset',"
        "'Fixed Asset','Other Current Liability') MAXRESULTS 500")
    out = [{
        "id": a.get("Id"), "name": a.get("Name"),
        "type": a.get("AccountType"), "subtype": a.get("AccountSubType", ""),
        "fully_qualified": a.get("FullyQualifiedName", a.get("Name")),
    } for a in resp.get("Account", []) if a.get("Active", True)]
    return sorted(out, key=lambda x: (x["type"], x["name"] or ""))


def list_vendors(db: Session, realm_id: str) -> list[dict]:
    """Active vendors from the QBO file."""
    out = []
    start = 1
    while True:
        resp = _query(db, realm_id,
                      f"SELECT * FROM Vendor WHERE Active = true "
                      f"STARTPOSITION {start} MAXRESULTS 1000")
        vendors = resp.get("Vendor", [])
        if not vendors:
            break
        out += [{"id": v.get("Id"),
                 "name": v.get("DisplayName") or v.get("CompanyName") or ""}
                for v in vendors]
        if len(vendors) < 1000:
            break
        start += 1000
    return sorted(out, key=lambda x: (x["name"] or "").lower())


def company_name(db: Session, realm_id: str) -> str:
    """Read CompanyInfo — the cheapest call to confirm a connection works."""
    resp = _query(db, realm_id, "SELECT * FROM CompanyInfo")
    info = (resp.get("CompanyInfo") or [{}])[0]
    return info.get("CompanyName", "")
