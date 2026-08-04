"""
Persistence — the bridge between stored rows and the engine's dataclasses.

The engine (in engine/) knows nothing about the database. It takes Charge and
Bill objects and returns MatchResults. This module loads rows into those
objects, runs the engine, and writes the results and audit entries back. Keeping
that boundary clean is why the matching logic stayed testable in isolation.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from engine import Charge, Bill, reconcile, Decision
from .models_db import ChargeRow, BillRow, MatchRow, AuditRow


def _to_charge(r: ChargeRow) -> Charge:
    return Charge(
        charge_id=r.charge_id, company=r.company, source=r.source,
        amount=r.amount, txn_date=r.txn_date, card_last4=r.card_last4,
        cardholder_name=r.cardholder_name, email=r.email,
        order_number=r.order_number, raw_description=r.raw_description,
    )


def _to_bill(r: BillRow) -> Bill:
    return Bill(
        bill_id=r.bill_id, company=r.company, amount=r.amount, txn_date=r.txn_date,
        balance=r.balance, line_count=r.line_count, quantity=r.quantity,
        vendor=r.vendor, name=r.name, email=r.email, order_number=r.order_number,
        card_last4=r.card_last4, memo=r.memo,
    )


def sync_bills_from_qbo(db: Session, company: str, realm_id: str,
                        since=None) -> int:
    """Replace the local bill rows for a company with a live QBO read."""
    from .integrations.qbo_bills import fetch_open_bills
    from datetime import date as _date

    n = 0
    seen = set()
    for b in fetch_open_bills(db, realm_id, since=since):
        db.merge(BillRow(
            bill_id=b["bill_id"],
            company=company,
            amount=b["total"],
            balance=b["balance"],
            txn_date=_date.fromisoformat(b["txn_date"]),
            line_count=b["line_count"],
            quantity=b.get("quantity"),
            vendor=b["vendor"],
            vendor_id=b.get("vendor_id", ""),
            doc_number=b.get("doc_number", ""),
            email=b["email"],
            order_number=b["order_number"],
            memo=b["memo"],
        ))
        seen.add(b["bill_id"])
        n += 1
        if n % 200 == 0:
            db.commit()
    db.commit()

    # Bills that are no longer open (paid since last sync) drop out of the
    # candidate pool -- otherwise settled bills keep getting suggested.
    stale = db.query(BillRow).filter(BillRow.company == company).all()
    removed = 0
    for row in stale:
        if row.bill_id not in seen:
            db.delete(row)
            removed += 1
    db.commit()
    return n


def run_reconciliation(db: Session, company: str) -> list[MatchRow]:
    # Uses review's converter so the address-book email fallback applies to the
    # batch path too -- two converters would mean two different match rates.
    from .review import _to_charge as _to_charge_with_email
    charges = [_to_charge_with_email(r, db) for r in db.scalars(
        select(ChargeRow).where(ChargeRow.company == company))]
    bills = [_to_bill(r) for r in db.scalars(
        select(BillRow).where(BillRow.company == company))]

    results = reconcile(charges, bills)

    # clear any prior run for these charges so re-running is idempotent
    prior = db.scalars(select(MatchRow).where(MatchRow.company == company)).all()
    for p in prior:
        db.delete(p)

    rows = []
    for res in results:
        row = MatchRow(
            company=company,
            charge_id=res.charge.charge_id,
            bill_id=res.bill.bill_id if res.bill else None,
            score=res.score,
            decision=res.decision.value,
            reasons=" | ".join(res.reasons),
            overwrite_bill_amount=res.overwrite_bill_amount,
        )
        db.add(row)
        rows.append(row)

        # audit any bill overwrite the engine decided on (still just recorded,
        # not yet pushed — the push is a separate, gated action)
        if res.overwrite_bill_amount is not None and res.bill is not None:
            db.add(AuditRow(
                company=company, action="bill_overwrite_planned",
                charge_id=res.charge.charge_id, bill_id=res.bill.bill_id,
                detail=f"{res.bill.amount} -> {res.overwrite_bill_amount} "
                       f"(TicketVault rounding)",
                dry_run=True,
            ))

    db.commit()
    return rows
