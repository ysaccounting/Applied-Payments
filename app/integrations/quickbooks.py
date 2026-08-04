"""
QuickBooks boundary.

This is the one place that talks to a live ledger, so it's the one place with
the strictest guard. Reads are safe. Writes are gated behind TWO switches
(DRY_RUN off AND QBO_WRITE_ENABLED on) and, until the real build wires the
Intuit client, refuse to do anything real regardless.

Everything a write *would* do is recorded to the audit log, so you can run the
whole pipeline in dry-run and inspect exactly what it intends to post before a
single real write is ever turned on.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy.orm import Session

from ..config import settings
from ..models_db import AuditRow, MatchRow, BillRow


class WriteBlocked(RuntimeError):
    pass


class QuickBooksClient:
    """Interface to QBO. Read methods are where the Intuit API calls go in the
    real build; write methods are gated. Left intentionally un-wired to a live
    account so nothing here can touch real books yet."""

    # --- reads (safe) --------------------------------------------------------
    def __init__(self, db=None, realm_id: str = ""):
        self.db = db
        self.realm_id = realm_id

    def read_open_bills(self, since=None):
        """Live: SELECT * FROM Bill WHERE Balance > '0', memo parsed."""
        from .qbo_bills import fetch_open_bills
        return list(fetch_open_bills(self.db, self.realm_id, since=since))

    def company_name(self) -> str:
        from .qbo_bills import company_name
        return company_name(self.db, self.realm_id)

    # --- writes (gated) ------------------------------------------------------
    def _guard(self):
        if settings.dry_run or not settings.qbo_write_enabled:
            raise WriteBlocked(
                "write-back is gated: set dry_run=false AND qbo_write_enabled=true"
            )

    def post_bill_payment(self, company: str, bill_id: str, amount: Decimal):
        self._guard()
        raise NotImplementedError("live QBO write is wired during the M8 build")

    def overwrite_bill_amount(self, company: str, bill_id: str, amount: Decimal):
        self._guard()
        raise NotImplementedError("live QBO write is wired during the M8 build")


def push_company(db: Session, company: str) -> dict:
    """Push approved matches for a company. In dry-run (the default) this posts
    nothing — it records to the audit log exactly what it *would* do, and returns
    that plan. This is how you validate the write path safely."""
    approved = [
        m for m in db.query(MatchRow).filter(
            MatchRow.company == company,
            MatchRow.decision == "auto_match",
            MatchRow.posted.is_(False),
        ).all()
    ]

    planned = []
    for m in approved:
        # record the intended payment
        db.add(AuditRow(
            company=company, action="push_bill_payment",
            charge_id=m.charge_id, bill_id=m.bill_id,
            detail=f"pay bill {m.bill_id} from charge {m.charge_id}",
            dry_run=settings.dry_run,
        ))
        # and the overwrite, if this match carried one
        if m.overwrite_bill_amount is not None:
            db.add(AuditRow(
                company=company, action="push_bill_overwrite",
                charge_id=m.charge_id, bill_id=m.bill_id,
                detail=f"overwrite bill {m.bill_id} -> {m.overwrite_bill_amount}",
                dry_run=settings.dry_run,
            ))
        planned.append({
            "charge_id": m.charge_id, "bill_id": m.bill_id,
            "overwrite_to": str(m.overwrite_bill_amount) if m.overwrite_bill_amount else None,
        })

        if not settings.dry_run and settings.qbo_write_enabled:
            # real path (not reachable until both switches are flipped AND the
            # client is wired); left here to show where it goes
            client = QuickBooksClient()
            client.post_bill_payment(company, m.bill_id, Decimal("0"))
            m.posted = True

    db.commit()
    return {
        "company": company,
        "dry_run": settings.dry_run,
        "write_enabled": settings.qbo_write_enabled,
        "planned_count": len(planned),
        "planned": planned,
        "note": "dry-run: nothing was posted to QuickBooks" if settings.dry_run
                else "live write path",
    }
