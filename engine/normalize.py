"""
Ingestion / normalization.

Every source (Slash, WEX, Divvy, Amex, Chase, ...) arrives with its own columns
and quirks. Each gets a small adapter that lands it in the common `Charge`
shape. This is the ONLY place that knows about source-specific formats; the
matcher downstream is source-agnostic.

Adding a new card source = writing one adapter here. Nothing else changes.

For the real build these adapters take a CSV/export row. Here they take a dict
so the sample data can exercise the same code path your real files will.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Optional

from .models import Charge, money


# Order/PO numbers are usually buried in free-text memo/description, not a clean
# column. This is the extraction that makes order-number matching possible.
# Tune these patterns during discovery against real memos.
_ORDER_PATTERNS = [
    re.compile(r"\bORDER[:#\s-]*([A-Z0-9]{5,})", re.I),
    re.compile(r"\bPO[:#\s-]*([A-Z0-9]{5,})", re.I),
    re.compile(r"\bCONF(?:IRMATION)?[:#\s-]*([A-Z0-9]{5,})", re.I),
    re.compile(r"#\s*([0-9]{5,})"),
]


def extract_order_number(text: str) -> Optional[str]:
    if not text:
        return None
    for pat in _ORDER_PATTERNS:
        m = pat.search(text)
        if m:
            return m.group(1).upper()
    return None


def _last4(value) -> Optional[str]:
    if value is None:
        return None
    digits = re.sub(r"\D", "", str(value))
    return digits[-4:] if len(digits) >= 4 else None


def _parse_date(value) -> date:
    if isinstance(value, date):
        return value
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d-%b-%Y"):
        try:
            return datetime.strptime(str(value), fmt).date()
        except ValueError:
            continue
    raise ValueError(f"unrecognized date: {value!r}")


def _norm_email(value) -> Optional[str]:
    return str(value).strip().lower() if value else None


# --- Per-source adapters -----------------------------------------------------
# They differ only in which raw keys map to which normalized field. That's the
# whole job. Real versions read the actual export columns.

def from_slash(row: dict, company: str) -> Charge:
    desc = row.get("description", "")
    return Charge(
        charge_id=row["id"],
        company=company,
        source="slash",
        amount=money(row["amount"]),
        txn_date=_parse_date(row["date"]),
        card_last4=_last4(row.get("card")),
        cardholder_name=row.get("cardholder"),
        email=_norm_email(row.get("email")),
        order_number=extract_order_number(desc),
        raw_description=desc,
    )


def from_wex(row: dict, company: str) -> Charge:
    desc = row.get("memo", "")
    return Charge(
        charge_id=row["txn_id"],
        company=company,
        source="wex",
        amount=money(row["total"]),
        txn_date=_parse_date(row["post_date"]),
        card_last4=_last4(row.get("card_number")),
        cardholder_name=row.get("employee"),
        email=_norm_email(row.get("employee_email")),
        order_number=extract_order_number(desc),
        raw_description=desc,
    )


def from_divvy(row: dict, company: str) -> Charge:
    desc = row.get("note", "")
    return Charge(
        charge_id=row["transaction_id"],
        company=company,
        source="divvy",
        amount=money(row["amount"]),
        txn_date=_parse_date(row["cleared_date"]),
        card_last4=_last4(row.get("last_four")),
        cardholder_name=row.get("user"),
        email=_norm_email(row.get("user_email")),
        order_number=extract_order_number(desc),
        raw_description=desc,
    )


# Registry so ingestion can dispatch by source name.
ADAPTERS = {"slash": from_slash, "wex": from_wex, "divvy": from_divvy}


def normalize(source: str, row: dict, company: str) -> Charge:
    return ADAPTERS[source](row, company)
