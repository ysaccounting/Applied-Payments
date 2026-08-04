"""
QuickBooks write-back.

This is the only code in the project that changes a real ledger, so it is
deliberately conservative:

  - **Idempotent.** Every charge records the QBO transaction id it created.
    A charge that already has one is skipped, so a re-run, a double-click or a
    retry after a timeout cannot post twice. This matters more than anything
    else here: a duplicated payment is far harder to notice and unwind than a
    failed one.
  - **Per-record.** One bad charge fails alone and is reported; the rest still
    post. An all-or-nothing batch would strand 200 good rows behind one bad one.
  - **Gated.** Both DRY_RUN=false and QBO_WRITE_ENABLED=true are required, plus
    a mapped QBO account for the card. Anything missing is a refusal, not a
    guess.
  - **Audited.** Before/after state, the QBO id, and who approved it are written
    to the audit log for every post.

Two shapes get written:

  matched -> BillPayment      pays down a specific bill from the card account
  coded   -> Purchase         an expense against a category account and vendor
"""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP

import httpx
from sqlalchemy.orm import Session

from ..config import settings
from ..models_db import (AuditRow, BillRow, CardRow, ChargeRow, QboTokenRow,
                         SourceAccountRow)
from . import qbo_oauth, qbo_tokens

log = logging.getLogger(__name__)

MINOR_VERSION = "75"


class WriteBlocked(RuntimeError):
    """Raised when the safety gates or configuration forbid a write."""


class WriteFailed(RuntimeError):
    pass


def assert_writes_allowed() -> None:
    if settings.dry_run:
        raise WriteBlocked("DRY_RUN is true — set it to false to post for real")
    if not settings.qbo_write_enabled:
        raise WriteBlocked("QBO_WRITE_ENABLED is false — set it to true to post")


def plain_fault(body: str, entity: str, status: int) -> str:
    """The sentence QuickBooks buried, rather than the whole JSON envelope.

    An Intuit fault is a nest of Fault/Error/Detail with the useful sentence at
    the bottom. Showing the raw payload makes an ordinary validation message --
    "subaccounts must match their parent's type" -- look like a crash.
    """
    import json as _json
    import re as _re
    try:
        err = (_json.loads(body).get("Fault", {}).get("Error") or [{}])[0]
        detail = (err.get("Detail") or "").strip()
        message = (err.get("Message") or "").strip()
        text = detail or message
        # Intuit prefixes the detail with its own label and appends a
        # timestamp; neither helps the reader.
        text = _re.sub(r"^[A-Za-z ]*Error:\s*", "", text).strip()
        text = _re.sub(r"\s*\d{4}-\d{2}-\d{2}T[\d:.\-+]+$", "", text).strip()
        text = text.rstrip(".")
        if text:
            return text
    except Exception:                       # noqa: BLE001
        pass
    return f"{entity} write failed ({status})"


def _post(db: Session, realm_id: str, entity: str, payload: dict) -> dict:
    qbo_tokens.assert_realm_allowed(realm_id)
    token = qbo_tokens.get_access_token(db, realm_id)
    url = f"{qbo_oauth.api_base()}/v3/company/{realm_id}/{entity}"
    r = httpx.post(
        url,
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/json",
                 "Content-Type": "application/json"},
        params={"minorversion": MINOR_VERSION},
        json=payload,
        timeout=60,
    )
    if r.status_code == 401:
        raise WriteFailed("401 from QuickBooks — token rejected; reconnect")
    if r.status_code >= 400:
        # Same reasoning as correct_bill_amount: a concurrency fault is not a
        # system failure, it's someone getting there first.
        if "5010" in r.text or "Stale Object" in r.text:
            raise WriteFailed("Bill already matched")
        raise WriteFailed(plain_fault(r.text, entity, r.status_code))
    return r.json()


def delete_transaction(db: Session, realm_id: str, entity: str,
                       txn_id: str, sync_token: str) -> bool:
    """Delete a transaction from QuickBooks.

    Used by undo. QuickBooks needs both the Id and the current SyncToken --
    the token is a concurrency guard, so a stale one means someone else edited
    the transaction and the delete is refused rather than clobbering them.
    """
    assert_writes_allowed()
    qbo_tokens.assert_realm_allowed(realm_id)
    token = qbo_tokens.get_access_token(db, realm_id)
    url = f"{qbo_oauth.api_base()}/v3/company/{realm_id}/{entity}"
    r = httpx.post(
        url,
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/json",
                 "Content-Type": "application/json"},
        params={"operation": "delete", "minorversion": MINOR_VERSION},
        json={"Id": txn_id, "SyncToken": sync_token or "0"},
        timeout=60,
    )
    if r.status_code >= 400:
        body = r.text or ""
        # Already deleted in QuickBooks -- by someone working directly in QBO,
        # or a retry after a partial failure. The goal of undo is "not in the
        # ledger", and that's already true, so treat it as done rather than
        # stranding the charge in Categorized with no way back.
        if "Object Not Found" in body or '"code":"610"' in body:
            log.info("%s %s already absent from QuickBooks — treating as deleted",
                     entity, txn_id)
            return True
        raise WriteFailed(
            f"could not delete {entity} {txn_id} ({r.status_code}): {body[:300]}")
    return True


def undo_post(db: Session, charge: ChargeRow, actor: str) -> dict:
    """Remove a charge's transaction from QuickBooks so it can be reworked.

    Deleting in QBO first, and only clearing local state if that succeeds, is
    what stops the app's view drifting from the ledger. A failed delete leaves
    everything as it was rather than pretending the transaction is gone.
    """
    if not charge.qbo_txn_id:
        return {"status": "nothing_posted"}

    entity = {"BillPayment": "billpayment", "Deposit": "deposit"}.get(
        charge.qbo_txn_type, "purchase")
    realm = _realm_for(db)
    try:
        delete_transaction(db, realm, entity, charge.qbo_txn_id,
                           charge.qbo_sync_token)
    except (WriteBlocked, WriteFailed) as e:
        return {"status": "failed", "error": str(e)[:300]}

    db.add(AuditRow(
        company=charge.company, action="qbo_deleted",
        charge_id=charge.charge_id,
        detail=f"deleted {charge.qbo_txn_type} {charge.qbo_txn_id} by {actor}",
        dry_run=False, actor=actor))
    # The payment is gone from the ledger, so the bill is owed again. Without
    # this it would stay invisible until the next sync -- unmatchable for as
    # long as that takes, which is exactly when the reviewer is trying to redo it.
    if charge.qbo_txn_type == "BillPayment" and charge.matched_bill_id:
        paid_bill = db.get(BillRow, charge.matched_bill_id)
        if paid_bill is not None:
            before_bal = paid_bill.balance or Decimal("0")
            paid_bill.balance = min(paid_bill.amount or (before_bal + charge.amount),
                                    before_bal + charge.amount)
            db.add(AuditRow(
                company=charge.company, action="bill_balance_restored",
                charge_id=charge.charge_id, bill_id=paid_bill.bill_id,
                detail=f"balance {before_bal} -> {paid_bill.balance} "
                       f"after undoing payment of ${charge.amount}",
                dry_run=False, actor=actor))
    charge.qbo_txn_id = ""
    charge.qbo_txn_type = ""
    charge.qbo_sync_token = ""
    charge.posted_at = None
    db.commit()
    return {"status": "deleted"}


def account_type(db: Session, qbo_account_id: str) -> str:
    """'Bank' or 'Credit Card' for a mapped account, from the cached chart.

    Determines which QuickBooks object a transaction becomes, so it can't be
    guessed: money returned to a bank account is a Deposit, money returned to a
    credit card is a Credit Card Credit, and they are not interchangeable.
    """
    from ..models_db import QboRefRow
    row = db.query(QboRefRow).filter(
        QboRefRow.kind == "account",
        QboRefRow.qbo_id == qbo_account_id).first()
    return (row.account_type if row else "") or "Credit Card"


def resolve_card_account(db: Session, charge: ChargeRow) -> tuple[str, str]:
    """Which QBO account this charge's card settles to.

    Per-card override first, then the card program default. A charge whose card
    isn't mapped is refused rather than posted to a guessed account.
    """
    override = db.get(CardRow, f"{charge.source}:{charge.card_last4}")
    if override is not None and override.qbo_account_id:
        return override.qbo_account_id, override.qbo_account_name

    prog = db.get(SourceAccountRow, f"{charge.company}:{charge.source}")
    if prog is not None and prog.qbo_account_id:
        return prog.qbo_account_id, prog.qbo_account_name

    raise WriteBlocked(
        f"card program '{charge.source}' has no QuickBooks account mapped — "
        f"set it under Cards before posting")


def _realm_for(db: Session) -> str:
    rows = db.query(QboTokenRow).all()
    if not rows:
        raise WriteBlocked("QuickBooks is not connected")
    if len(rows) > 1:
        # Multi-company will need the charge's company mapped to a realm; until
        # then, refuse rather than pick one arbitrarily.
        raise WriteBlocked(
            "more than one QuickBooks company is connected — "
            "per-company realm mapping is required before posting")
    return rows[0].realm_id


# The largest gap that can be a TicketVault rounding artifact. Beyond this it
# is a real discrepancy and must not be silently conformed to the charge --
# without a ceiling, "correct the loading artifact" becomes "make the books
# agree with whatever hit the card".
MAX_ROUNDING_GAP = Decimal("0.25")


def _rounding_gap_is_explained(charge_amount: Decimal, bill: BillRow) -> dict:
    """Is the charge-vs-bill difference a TicketVault rounding artifact?

    TicketVault builds a bill as round(round(total/qty) * qty), which loses a
    penny or two when the total doesn't divide evenly -- $100.00 over 3 tickets
    becomes $99.99. The charge is the true amount; the bill is the artifact.

    Proven case: the bill is EXACTLY what that arithmetic would produce from
    this charge. Plausible case: quantity is unknown but the gap is within the
    ceiling. Anything larger is a real discrepancy.
    """
    gap = (charge_amount - bill.amount).quantize(Decimal("0.01"))
    out = {"gap": gap, "explained": False, "proven": False, "reason": ""}

    if gap == 0:
        out["reason"] = "amounts already equal"
        return out

    if abs(gap) > MAX_ROUNDING_GAP:
        out["reason"] = (f"gap {gap} exceeds the {MAX_ROUNDING_GAP} rounding "
                         f"ceiling — a real discrepancy, not rounding")
        return out

    if bill.line_count != 1:
        out["reason"] = (f"bill has {bill.line_count} lines — rounding "
                         f"correction only applies to single-line bills")
        return out

    qty = bill.quantity
    if qty and qty > 0:
        unit = (charge_amount / Decimal(qty)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP)
        reconstructed = (unit * Decimal(qty)).quantize(Decimal("0.01"))
        if reconstructed == bill.amount:
            out.update(explained=True, proven=True,
                       reason=(f"bill = round({charge_amount}/{qty}) x {qty} "
                               f"= {bill.amount}; TicketVault rounding"))
            return out
        out["reason"] = (f"gap {gap} does not match TicketVault arithmetic "
                         f"for {qty} tickets (would be {reconstructed})")
        return out

    out.update(explained=True, proven=False,
               reason=f"gap {gap} within rounding ceiling; quantity unknown")
    return out


def match_is_postable(charge_amount: Decimal, bill: BillRow) -> dict:
    """Would a BillPayment for this charge/bill pair actually be accepted?

    The review queue used to offer candidates the write path then refused, so a
    reviewer could click Match, watch the row move to Categorized, and only then
    be told the gap was a real discrepancy. The verdict now lives in one place
    and both sides ask it.

    Note gap == 0 returns explained=False (there is nothing to explain), so it
    has to be admitted separately.
    """
    # A partly-paid bill can still look like a perfect amount match -- the gap
    # is measured against the bill's ORIGINAL amount, not what's left owed. The
    # write path refuses to overpay, so the queue must not offer it either.
    balance = bill.balance if bill.balance is not None else bill.amount
    if balance is not None and charge_amount > balance:
        return {"ok": False, "gap": charge_amount - (bill.amount or 0),
                "reason": f"only {balance} still owed — paying {charge_amount} "
                          f"would overpay the bill"}

    v = _rounding_gap_is_explained(charge_amount, bill)
    return {"ok": v["gap"] == 0 or v["explained"],
            "gap": v["gap"], "reason": v["reason"]}


def correct_bill_amount(db: Session, realm_id: str, bill: BillRow,
                        new_amount: Decimal, actor: str) -> dict:
    """Rewrite a single-line bill's amount to the true charge amount.

    A sparse update against the bill's CURRENT SyncToken -- re-read first, since
    the TicketVault import may have touched the bill since it was synced, and a
    stale token would either fail or clobber that change.
    """
    assert_writes_allowed()
    qbo_tokens.assert_realm_allowed(realm_id)
    token = qbo_tokens.get_access_token(db, realm_id)
    base = f"{qbo_oauth.api_base()}/v3/company/{realm_id}"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json",
               "Content-Type": "application/json"}

    r = httpx.get(f"{base}/bill/{bill.bill_id}", headers=headers,
                  params={"minorversion": MINOR_VERSION}, timeout=60)
    if r.status_code >= 400:
        raise WriteFailed(f"could not read bill {bill.bill_id} "
                          f"({r.status_code}): {r.text[:200]}")
    current = r.json().get("Bill", {})
    lines = current.get("Line", [])
    if len(lines) != 1:
        raise WriteFailed(f"bill {bill.bill_id} has {len(lines)} lines in "
                          f"QuickBooks — refusing to rewrite the amount")

    old_amount = Decimal(str(current.get("TotalAmt", bill.amount)))
    line = dict(lines[0])
    line["Amount"] = float(new_amount)
    for detail_key in ("AccountBasedExpenseLineDetail", "ItemBasedExpenseLineDetail"):
        if detail_key in line and isinstance(line[detail_key], dict):
            d = dict(line[detail_key])
            # keep unit price consistent where the line carries one
            if "Qty" in d and d.get("Qty"):
                try:
                    d["UnitPrice"] = float(
                        (new_amount / Decimal(str(d["Qty"]))).quantize(
                            Decimal("0.000001")))
                except Exception:
                    pass
            line[detail_key] = d

    # A sparse update still has to carry the entity's REQUIRED fields, and for
    # a Bill that means VendorRef. Leaving it out returns a 2020 validation
    # fault ("Required parameter VendorRef is missing"), which reads like a bug
    # in our payload because it is one. We already re-read the bill above, so
    # carry its own refs straight back.
    vendor_ref = current.get("VendorRef")
    if not vendor_ref:
        raise WriteFailed(
            f"bill {bill.bill_id} came back from QuickBooks without a VendorRef "
            f"— cannot safely rewrite its amount")
    payload = {
        "Id": current["Id"],
        "SyncToken": current["SyncToken"],
        "sparse": True,
        "VendorRef": vendor_ref,
        "Line": [line],
        "TotalAmt": float(new_amount),
    }
    # Optional, but a bill in a non-home currency needs these preserved or the
    # update is rejected on the same "required field" grounds.
    for k in ("CurrencyRef", "APAccountRef", "TxnDate"):
        if current.get(k):
            payload[k] = current[k]
    u = httpx.post(f"{base}/bill", headers=headers,
                   params={"minorversion": MINOR_VERSION}, json=payload, timeout=60)
    if u.status_code >= 400:
        # 5010 is Intuit's Stale Object Error: the bill changed between our read
        # and our write, which in practice means someone matched it first. The
        # raw fault names both people and reads like a system failure, so say
        # what actually happened.
        if "5010" in u.text or "Stale Object" in u.text:
            raise WriteFailed("Bill already matched")
        raise WriteFailed(f"could not correct bill {bill.bill_id} "
                          f"({u.status_code}): {u.text[:300]}")

    updated = u.json().get("Bill", {})
    bill.amount = new_amount
    bill.balance = new_amount
    db.add(AuditRow(
        company=bill.company, action="bill_amount_corrected",
        bill_id=bill.bill_id,
        detail=f"{old_amount} -> {new_amount} (TicketVault rounding) by {actor}",
        dry_run=False, actor=actor))
    db.commit()
    log.info("corrected bill %s: %s -> %s", bill.bill_id, old_amount, new_amount)
    return {"bill_id": bill.bill_id, "from": str(old_amount),
            "to": str(new_amount), "sync_token": updated.get("SyncToken")}


def _read_bill_live(db: Session, realm_id: str, bill_id: str) -> dict:
    """Fetch one bill from QuickBooks as it stands right now."""
    token = qbo_tokens.get_access_token(db, realm_id)
    base = f"{qbo_oauth.api_base()}/v3/company/{realm_id}"
    r = httpx.get(f"{base}/bill/{bill_id}",
                  headers={"Authorization": f"Bearer {token}",
                           "Accept": "application/json"},
                  params={"minorversion": MINOR_VERSION}, timeout=60)
    if r.status_code >= 400:
        raise WriteFailed(f"could not read bill {bill_id} from QuickBooks "
                          f"({r.status_code}): {r.text[:200]}")
    return r.json().get("Bill", {})


def _refresh_bill_from_live(db: Session, bill: BillRow, live: dict) -> None:
    """Bring the local mirror in step with what QuickBooks just told us."""
    try:
        bill.amount = Decimal(str(live.get("TotalAmt", bill.amount)))
        bill.balance = Decimal(str(live.get("Balance", bill.balance)))
    except Exception:
        return


def post_bill_payment(db: Session, charge: ChargeRow, actor: str) -> dict:
    """Pay down a matched bill from the card's account.

    If the bill is a penny or two under the charge because of TicketVault's
    rounding, the BILL is corrected first and then paid in full. Paying the
    charge amount against an uncorrected bill would leave it overpaid, and
    thousands of those would litter A/P with small credit balances.
    """
    assert_writes_allowed()
    realm = _realm_for(db)
    acct_id, acct_name = resolve_card_account(db, charge)

    if charge.is_credit:
        raise WriteFailed(
            "this is a refund — money came back, so it can't pay down a bill. "
            "Code it as an expense/refund instead.")

    bill = db.get(BillRow, charge.matched_bill_id)
    if bill is None:
        raise WriteFailed(f"bill {charge.matched_bill_id} is not in the local pool")

    # Check the LIVE bill before doing anything to it. The local mirror is
    # refreshed on a cron, so it can be minutes stale -- and during rollout,
    # while people are still paying bills directly in QuickBooks, minutes is
    # long enough for someone to match a charge to a bill that is already
    # settled. Paying it again would create an overpayment sitting in A/P as a
    # vendor credit, which is far harder to spot and unwind than a refusal.
    #
    # One extra GET per payment. Payments happen at human speed, so the cost is
    # irrelevant next to what it prevents.
    # Checked BEFORE asking QuickBooks, because QuickBooks can lag: two tabs
    # matching different charges to the same bill seconds apart could both read
    # a positive balance and both post, leaving an overpayment nobody notices.
    # Our own draw-down is immediate, so it closes that window.
    other = db.query(ChargeRow).filter(
        ChargeRow.company == charge.company,
        ChargeRow.matched_bill_id == bill.bill_id,
        ChargeRow.charge_id != charge.charge_id,
        ChargeRow.qbo_txn_id != "").first()
    if other is not None:
        # Matched inside the app -- by anyone, in any tab.
        raise WriteFailed("Bill already matched")
    if (bill.balance or 0) <= 0:
        # Balance is zero but nothing here paid it, so it was settled directly
        # in QuickBooks. Saying "already matched" would send someone looking
        # for a match in this app that doesn't exist.
        raise WriteFailed("Bill already paid")

    live = _read_bill_live(db, realm, bill.bill_id)
    live_balance = Decimal(str(live.get("Balance", "0")))
    _refresh_bill_from_live(db, bill, live)

    if live_balance <= 0:
        # Self-healing: the mirror now says what QuickBooks says, so the bill
        # drops out of the candidate pool and the Bills Available tab straight
        # away rather than waiting for the next sync to prune it.
        db.add(AuditRow(
            company=charge.company, action="bill_already_paid",
            charge_id=charge.charge_id, bill_id=bill.bill_id,
            detail=f"bill {bill.doc_number or bill.bill_id} had balance "
                   f"{live_balance} in QuickBooks at post time; refused",
            dry_run=False, actor=actor))
        db.commit()
        # Deliberately terse. The specifics -- which bill, what balance QBO
        # reported, when -- are in the audit entry written just above.
        raise WriteFailed("Bill already paid")

    if charge.amount > live_balance:
        db.commit()
        raise WriteFailed(
            f"charge ${charge.amount} is more than the ${live_balance} still "
            f"owed on bill {bill.doc_number or bill.bill_id} in QuickBooks — "
            f"paying it would overpay the bill. Nothing was posted.")

    correction = None
    # Computed against the LIVE amount, so a bill edited in QuickBooks since
    # the last sync isn't "corrected" against a stale figure.
    finding = _rounding_gap_is_explained(charge.amount, bill)
    if finding["gap"] != 0:
        if not finding["explained"]:
            raise WriteFailed(
                f"charge ${charge.amount} vs bill ${bill.amount}: "
                f"{finding['reason']}. Not posted — review this pair.")
        correction = correct_bill_amount(db, realm, bill, charge.amount, actor)
        correction["reason"] = finding["reason"]
        correction["proven"] = finding["proven"]

    payload = {
        "VendorRef": {"value": bill.vendor_id or ""} if getattr(bill, "vendor_id", "")
                     else {"name": bill.vendor or ""},
        "TxnDate": charge.txn_date.isoformat(),
        "PayType": "CreditCard",
        "CreditCardPayment": {"CCAccountRef": {"value": acct_id, "name": acct_name}},
        "TotalAmt": float(charge.amount),
        "Line": [{
            "Amount": float(charge.amount),
            "LinkedTxn": [{"TxnId": bill.bill_id, "TxnType": "Bill"}],
        }],
        # Just the source description. Provenance lives in the audit log; the
        # ledger memo should read the way the team writes it.
        "PrivateNote": charge.memo or charge.merchant or "",
    }
    body = _post(db, realm, "billpayment", payload)
    created = body.get("BillPayment", {})
    if correction:
        created["_bill_correction"] = correction
    return created


def post_expense(db: Session, charge: ChargeRow, actor: str) -> dict:
    """Record a coded charge in QuickBooks."""
    entity, payload = _expense_payload(db, charge)
    realm = _realm_for(db)
    body = _post(db, realm, entity, payload)
    return body.get("Deposit" if entity == "deposit" else "Purchase", {})


def _expense_payload(db: Session, charge: ChargeRow) -> tuple[str, dict]:
    """The QuickBooks object for a coded charge.

    Shared by the initial post and by later edits, so a corrected category
    lands the same way it would have if it had been right the first time.

    Four combinations, and they map to different QuickBooks objects:

        credit card + spend   Purchase (PaymentType CreditCard) -> Expense
        credit card + refund  Purchase with Credit=true -> Credit Card Credit
        bank + spend          Purchase (PaymentType Cash) -> Expense
        bank + refund         Deposit

    PaymentType Cash rather than Check for bank spending: QuickBooks renders a
    Check-type Purchase as a cheque (expecting a cheque number), while Cash
    renders as an Expense, which is what these card charges actually are.

    Posting a bank refund as a credit-card credit would put it on the wrong kind
    of account and wouldn't reconcile, so the account type decides the shape.
    """
    assert_writes_allowed()
    realm = _realm_for(db)
    acct_id, acct_name = resolve_card_account(db, charge)

    if not charge.coded_category:
        raise WriteFailed("no category set on this charge")

    is_bank = account_type(db, acct_id).lower().startswith("bank")

    # --- money coming back into a bank account: a Deposit ---
    if charge.is_credit and is_bank:
        line: dict = {
            "Amount": float(charge.amount),
            "DetailType": "DepositLineDetail",
            "DepositLineDetail": {
                "AccountRef": ({"value": charge.coded_category_id}
                               if charge.coded_category_id
                               else {"name": charge.coded_category}),
            },
            "Description": charge.memo or charge.merchant or "",
        }
        if charge.coded_vendor_id:
            line["DepositLineDetail"]["Entity"] = {
                "value": charge.coded_vendor_id, "type": "Vendor"}
        payload = {
            "DepositToAccountRef": {"value": acct_id, "name": acct_name},
            "TxnDate": charge.txn_date.isoformat(),
            "TotalAmt": float(charge.amount),
            "Line": [line],
            "PrivateNote": charge.memo or charge.merchant or "",
        }
        return "deposit", payload

    # Prefer the id: QuickBooks resolves by `value` when present, so a category
    # renamed since coding still posts correctly. Name is a fallback for
    # charges coded before ids were captured.
    account_ref = ({"value": charge.coded_category_id}
                   if charge.coded_category_id
                   else {"name": charge.coded_category})
    line: dict = {
        "Amount": float(charge.amount),
        "DetailType": "AccountBasedExpenseLineDetail",
        "AccountBasedExpenseLineDetail": {
            "AccountRef": account_ref,
        },
        "Description": charge.memo or charge.merchant or "",
    }
    payload = {
        # Cash -> QuickBooks shows an Expense; Check would show a cheque.
        "PaymentType": "Cash" if is_bank else "CreditCard",
        "AccountRef": {"value": acct_id, "name": acct_name},
        "TxnDate": charge.txn_date.isoformat(),
        "TotalAmt": float(charge.amount),
        "Line": [line],
        "PrivateNote": charge.memo or charge.merchant or "",
    }
    if charge.coded_vendor_id:
        payload["EntityRef"] = {"value": charge.coded_vendor_id, "type": "Vendor"}
    elif charge.coded_vendor:
        payload["EntityRef"] = {"name": charge.coded_vendor, "type": "Vendor"}
    if charge.is_credit:
        # Credit card only -- the bank case returned a Deposit above.
        payload["Credit"] = True

    return "purchase", payload


def update_posted_charge(db: Session, charge: ChargeRow, actor: str) -> dict:
    """Push an edited charge to the transaction QuickBooks already holds.

    A correction made here has to reach the ledger, or the app quietly becomes
    a second set of books that disagrees with the first.

    Sparse update, so only what we send changes -- but QuickBooks still
    requires an entity's mandatory fields in a sparse write, which is why the
    whole payload is rebuilt from _expense_payload rather than patched. The
    SyncToken is re-read first: a stale one is refused, and ours can be stale
    if anyone touched the transaction in QuickBooks since we wrote it.
    """
    assert_writes_allowed()
    if not charge.qbo_txn_id:
        return {"status": "skipped", "reason": "not in QuickBooks"}

    realm = _realm_for(db)
    entity = {"Purchase": "purchase", "Deposit": "deposit",
              "BillPayment": "billpayment"}.get(charge.qbo_txn_type or "")
    if not entity:
        return {"status": "skipped", "reason": f"cannot edit {charge.qbo_txn_type}"}

    token = qbo_tokens.get_access_token(db, realm)
    base = f"{qbo_oauth.api_base()}/v3/company/{realm}"
    r = httpx.get(f"{base}/{entity}/{charge.qbo_txn_id}",
                  headers={"Authorization": f"Bearer {token}",
                           "Accept": "application/json"},
                  params={"minorversion": MINOR_VERSION}, timeout=60)
    if r.status_code >= 400:
        raise WriteFailed(f"could not read the QuickBooks transaction "
                          f"({r.status_code}): {r.text[:200]}")
    key = {"purchase": "Purchase", "deposit": "Deposit",
           "billpayment": "BillPayment"}[entity]
    current = r.json().get(key, {})
    sync = str(current.get("SyncToken", charge.qbo_sync_token or "0"))

    if entity == "billpayment":
        # A bill payment's amount and linked bill are not ours to rewrite --
        # only the note that explains it.
        payload = {"Id": charge.qbo_txn_id, "SyncToken": sync, "sparse": True,
                   "PrivateNote": charge.memo or charge.merchant or "",
                   "VendorRef": current.get("VendorRef"),
                   "TotalAmt": current.get("TotalAmt")}
    else:
        # A FULL update, built by overlaying our changes onto the document
        # QuickBooks just gave us.
        #
        # Sparse looked right and wasn't: a sparse Purchase update applies the
        # fields it recognises but leaves the payee as it was, so editing the
        # vendor changed our copy and nothing in the ledger. A full update
        # replaces the object, so the payee actually moves -- and echoing the
        # current document back means nothing we don't model (class, department,
        # attachments, doc number) is lost in the process.
        _, built = _expense_payload(db, charge)
        payload = {k: v for k, v in current.items()
                   if k not in ("MetaData", "domain", "sparse")}
        payload["PrivateNote"] = built.get("PrivateNote", "")

        # The payee. Removing it entirely is meaningful too, so an absent
        # EntityRef clears rather than silently keeping the old one.
        if "EntityRef" in built:
            payload["EntityRef"] = built["EntityRef"]
        else:
            payload.pop("EntityRef", None)

        # Keep the existing line's Id: without it a full update deletes the
        # original line and adds a new one, which reshuffles the transaction
        # for no reason.
        new_line = (built.get("Line") or [{}])[0]
        old_lines = payload.get("Line") or []
        if old_lines:
            merged = dict(old_lines[0])
            # Merge the detail object rather than replacing it. A flat update
            # would swap out AccountBasedExpenseLineDetail wholesale and take
            # ClassRef, CustomerRef and BillableStatus with it -- fields the app
            # never sets but the ledger relies on.
            for k, v in new_line.items():
                if (isinstance(v, dict) and isinstance(merged.get(k), dict)):
                    sub = dict(merged[k])
                    sub.update(v)
                    merged[k] = sub
                else:
                    merged[k] = v
            payload["Line"] = [merged] + old_lines[1:]
        else:
            payload["Line"] = built.get("Line", [])

        payload.update({"Id": charge.qbo_txn_id, "SyncToken": sync})

    body = _post(db, realm, entity, payload)
    updated = body.get(key, {})
    charge.qbo_sync_token = str(updated.get("SyncToken", sync))
    db.add(AuditRow(company=charge.company, action="qbo_updated",
                    charge_id=charge.charge_id,
                    detail=f"updated {charge.qbo_txn_type} {charge.qbo_txn_id} "
                           f"in QuickBooks by {actor}",
                    dry_run=False, actor=actor))
    db.commit()
    return {"status": "updated", "id": charge.qbo_txn_id}


def push_charge(db: Session, charge: ChargeRow, actor: str) -> dict:
    """Post one charge. Returns a result dict; never raises for a normal failure."""
    if charge.qbo_txn_id:
        return {"charge_id": charge.charge_id, "status": "skipped",
                "reason": "already posted", "qbo_txn_id": charge.qbo_txn_id}
    if charge.status != "categorized":
        return {"charge_id": charge.charge_id, "status": "skipped",
                "reason": f"status is {charge.status}, not categorized"}

    try:
        if charge.resolution == "matched":
            created = post_bill_payment(db, charge, actor)
            kind = "BillPayment"
        elif charge.resolution == "coded":
            created = post_expense(db, charge, actor)
            # A bank refund comes back as a Deposit, which needs the right
            # entity name for a later undo to find it.
            kind = "Deposit" if (charge.is_credit and
                                 account_type(db, resolve_card_account(db, charge)[0])
                                 .lower().startswith("bank")) else "Purchase"
        else:
            return {"charge_id": charge.charge_id, "status": "skipped",
                    "reason": "no resolution recorded"}

        txn_id = created.get("Id", "")
        # A paid bill has to leave the candidate pool NOW, not at the next
        # sync. Otherwise it keeps being suggested for up to an hour after it
        # was settled, and two reviewers can match two different charges to the
        # same bill. Drawing the balance down (rather than deleting the row)
        # keeps the record around so Undo can put it back.
        if kind == "BillPayment" and charge.matched_bill_id:
            paid_bill = db.get(BillRow, charge.matched_bill_id)
            if paid_bill is not None:
                before_bal = paid_bill.balance
                paid_bill.balance = max(Decimal("0"),
                                        (before_bal or Decimal("0")) - charge.amount)
                db.add(AuditRow(
                    company=charge.company, action="bill_balance_drawn_down",
                    charge_id=charge.charge_id, bill_id=paid_bill.bill_id,
                    detail=f"balance {before_bal} -> {paid_bill.balance} "
                           f"after payment of ${charge.amount}",
                    dry_run=False, actor=actor))
        charge.qbo_txn_id = txn_id
        charge.qbo_txn_type = kind
        # needed to delete it again if the reviewer undoes
        charge.qbo_sync_token = str(created.get("SyncToken", "0"))
        charge.posted_at = datetime.utcnow()
        db.add(AuditRow(
            company=charge.company, action=f"qbo_post_{kind.lower()}",
            charge_id=charge.charge_id, bill_id=charge.matched_bill_id or None,
            detail=f"created {kind} {txn_id} for ${charge.amount} by {actor}",
            dry_run=False, actor=actor))
        db.commit()
        return {"charge_id": charge.charge_id, "status": "posted",
                "type": kind, "qbo_txn_id": txn_id}

    except (WriteBlocked, WriteFailed) as e:
        db.rollback()
        db.add(AuditRow(
            company=charge.company, action="qbo_post_failed",
            charge_id=charge.charge_id, detail=str(e)[:1500],
            dry_run=False, actor=actor))
        db.commit()
        return {"charge_id": charge.charge_id, "status": "failed",
                "error": str(e)[:300]}
    except Exception as e:                      # unexpected: log loudly, keep going
        db.rollback()
        log.exception("unexpected error posting %s", charge.charge_id)
        return {"charge_id": charge.charge_id, "status": "failed",
                "error": f"{type(e).__name__}: {e}"[:300]}


def push_batch(db: Session, company: str, actor: str,
               charge_ids: list[str] | None = None, limit: int = 50) -> dict:
    """Post categorized charges. Bounded by `limit` on purpose.

    Defaulting to a small batch means the first live run is small enough to
    check by hand in QuickBooks before doing more.
    """
    assert_writes_allowed()

    q = db.query(ChargeRow).filter(
        ChargeRow.company == company,
        ChargeRow.status == "categorized",
        ChargeRow.qbo_txn_id == "")
    if charge_ids:
        q = q.filter(ChargeRow.charge_id.in_(charge_ids))
    charges = q.limit(limit).all()

    results = [push_charge(db, c, actor) for c in charges]
    return {
        "company": company,
        "attempted": len(results),
        "posted": sum(1 for r in results if r["status"] == "posted"),
        "failed": sum(1 for r in results if r["status"] == "failed"),
        "skipped": sum(1 for r in results if r["status"] == "skipped"),
        "results": results,
    }


# --- creating reference records -----------------------------------------
#
# Coding a charge needs a vendor and a category that already exist in
# QuickBooks. When one doesn't, the old answer was "go and make it in QBO, then
# come back and refresh" -- a dead end in the middle of a task. These create it
# in place.
#
# All three follow the same shape: refuse a name that already exists (QuickBooks
# would either error or make a confusing near-duplicate), create, then hand back
# the id so the caller can cache it and use it immediately.

def _existing_name(db: Session, realm: str, entity: str, name: str) -> dict | None:
    """The record with this name, if QuickBooks already has one."""
    from .qbo_bills import _query
    safe = (name or "").replace("'", "\\'")
    field = "DisplayName" if entity == "Vendor" else "Name"
    resp = _query(db, realm,
                  f"SELECT * FROM {entity} WHERE {field} = '{safe}' MAXRESULTS 5")
    rows = resp.get(entity, [])
    return rows[0] if rows else None


def create_vendor(db: Session, name: str, actor: str) -> dict:
    """Add a vendor to QuickBooks and return {id, name}."""
    assert_writes_allowed()
    name = (name or "").strip()
    if not name:
        raise WriteFailed("A vendor name is required")
    realm = _realm_for(db)

    existing = _existing_name(db, realm, "Vendor", name)
    if existing:
        # Not an error worth stopping for: the person wanted this vendor to
        # exist, and it does. Hand back the one already there.
        return {"id": existing.get("Id"), "name": existing.get("DisplayName"),
                "created": False}

    body = _post(db, realm, "vendor", {"DisplayName": name})
    v = body.get("Vendor", {})
    db.add(AuditRow(company="", action="qbo_vendor_created",
                    detail=f"created vendor '{name}' ({v.get('Id')})",
                    dry_run=False, actor=actor))
    db.commit()
    return {"id": v.get("Id"), "name": v.get("DisplayName"), "created": True}


def create_category(db: Session, name: str, actor: str,
                    account_type: str = "Expense",
                    parent_id: str = "") -> dict:
    """Add an expense account -- a 'category' -- to QuickBooks.

    `name` may be a colon path ("Ticket Credits:MLB:Blue Jays"), which is how
    the team writes categories. QuickBooks stores the leaf name plus a parent
    link, so the path is split and each missing level is created in turn.
    """
    assert_writes_allowed()
    name = (name or "").strip().strip(":")
    if not name:
        raise WriteFailed("A category name is required")
    # Every type QuickBooks accepts. A sub-account must match its parent's type,
    # so this cannot be limited to the expense kinds.
    allowed = {"Expense", "Cost of Goods Sold", "Other Expense", "Income",
               "Other Income", "Bank", "Accounts Receivable",
               "Other Current Asset", "Fixed Asset", "Other Asset",
               "Accounts Payable", "Credit Card", "Other Current Liability",
               "Long Term Liability", "Equity"}
    if account_type not in allowed:
        raise WriteFailed(f"'{account_type}' isn't a QuickBooks account type")
    realm = _realm_for(db)

    parent = parent_id
    created_any = False
    leaf = None
    path = []
    for part in [p.strip() for p in name.split(":") if p.strip()]:
        path.append(part)
        full = ":".join(path)
        existing = _existing_name(db, realm, "Account", full) \
            or _existing_name(db, realm, "Account", part)
        if existing and (existing.get("FullyQualifiedName") or "") == full:
            parent = existing.get("Id")
            leaf = existing
            continue

        payload = {"Name": part, "AccountType": account_type}
        if parent:
            payload["SubAccount"] = True
            payload["ParentRef"] = {"value": parent}
        body = _post(db, realm, "account", payload)
        leaf = body.get("Account", {})
        parent = leaf.get("Id")
        created_any = True

    if leaf is None:
        raise WriteFailed(f"could not create '{name}'")
    if created_any:
        db.add(AuditRow(company="", action="qbo_category_created",
                        detail=f"created category '{name}' ({leaf.get('Id')})",
                        dry_run=False, actor=actor))
        db.commit()
    return {"id": leaf.get("Id"),
            "name": leaf.get("FullyQualifiedName") or leaf.get("Name"),
            "created": created_any}


def create_bank_account(db: Session, name: str, actor: str,
                        account_type: str = "Credit Card") -> dict:
    """Add a bank or credit-card account, the kind a card program settles to."""
    assert_writes_allowed()
    name = (name or "").strip()
    if not name:
        raise WriteFailed("An account name is required")
    if account_type not in ("Bank", "Credit Card"):
        raise WriteFailed("Account type must be Bank or Credit Card")
    realm = _realm_for(db)

    existing = _existing_name(db, realm, "Account", name)
    if existing:
        return {"id": existing.get("Id"),
                "name": existing.get("FullyQualifiedName") or existing.get("Name"),
                "type": existing.get("AccountType"), "created": False}

    payload = {"Name": name, "AccountType": account_type}
    if account_type == "Bank":
        payload["AccountSubType"] = "Checking"
    body = _post(db, realm, "account", payload)
    a = body.get("Account", {})
    db.add(AuditRow(company="", action="qbo_account_created",
                    detail=f"created {account_type} account '{name}' ({a.get('Id')})",
                    dry_run=False, actor=actor))
    db.commit()
    return {"id": a.get("Id"),
            "name": a.get("FullyQualifiedName") or a.get("Name"),
            "type": a.get("AccountType"), "created": True}


# --- editing bills -------------------------------------------------------

def edit_bill(db: Session, bill: BillRow, actor: str,
              vendor_name: str | None = None,
              new_amount: Decimal | None = None) -> dict:
    """Change a bill's vendor and/or amount, in QuickBooks and in our mirror.

    Reads the bill live first. The SyncToken is a concurrency guard and ours
    goes stale the moment anyone touches the bill in QuickBooks or the
    TicketVault import runs, so a cached one would either be rejected or
    overwrite someone else's change.

    Refuses two things outright:

      * more than one line -- splitting a new total across lines is an
        accounting decision, not one to guess at;
      * an amount below what has already been paid against the bill, which
        QuickBooks would leave over-applied.
    """
    assert_writes_allowed()
    realm = _realm_for(db)
    qbo_tokens.assert_realm_allowed(realm)
    token = qbo_tokens.get_access_token(db, realm)
    base = f"{qbo_oauth.api_base()}/v3/company/{realm}"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json",
               "Content-Type": "application/json"}

    r = httpx.get(f"{base}/bill/{bill.bill_id}", headers=headers,
                  params={"minorversion": MINOR_VERSION}, timeout=60)
    if r.status_code >= 400:
        raise WriteFailed(f"could not read bill {bill.doc_number or bill.bill_id} "
                          f"({r.status_code}): {r.text[:160]}")
    current = r.json().get("Bill", {})
    lines = current.get("Line", [])

    total = Decimal(str(current.get("TotalAmt", bill.amount or 0)))
    balance = Decimal(str(current.get("Balance", bill.balance or 0)))
    paid = total - balance

    payload = {k: v for k, v in current.items()
               if k not in ("MetaData", "domain", "sparse")}
    payload["Id"] = bill.bill_id
    payload["SyncToken"] = str(current.get("SyncToken", "0"))
    changed = []

    if vendor_name is not None and vendor_name.strip():
        from ..models_db import QboRefRow
        ref = db.query(QboRefRow).filter(
            QboRefRow.kind == "vendor", QboRefRow.name == vendor_name).first()
        if ref is None:
            raise WriteFailed(f"'{vendor_name}' isn't a vendor in QuickBooks")
        was = (current.get("VendorRef") or {}).get("name") or bill.vendor
        payload["VendorRef"] = {"value": ref.qbo_id}
        changed.append(f"vendor '{was}' -> '{vendor_name}'")

    if new_amount is not None:
        new_amount = Decimal(new_amount).quantize(Decimal("0.01"))
        if new_amount <= 0:
            raise WriteFailed("A bill's amount must be more than zero")
        if len(lines) != 1:
            raise WriteFailed(
                f"bill {bill.doc_number or bill.bill_id} has {len(lines)} lines "
                f"in QuickBooks — change the amount there instead")
        if new_amount < paid:
            raise WriteFailed(
                f"bill {bill.doc_number or bill.bill_id} already has "
                f"${paid} paid against it — the amount can't go below that")
        line = dict(lines[0])
        line["Amount"] = float(new_amount)
        # The detail block carries its own copy of the amount on some line
        # types; leaving it behind makes QuickBooks reject the update.
        for key in ("AccountBasedExpenseLineDetail", "ItemBasedExpenseLineDetail"):
            if isinstance(line.get(key), dict) and "Amount" in line[key]:
                line[key] = {**line[key], "Amount": float(new_amount)}
        payload["Line"] = [line]
        payload["TotalAmt"] = float(new_amount)
        changed.append(f"amount ${total} -> ${new_amount}")

    if not changed:
        return {"status": "unchanged", "changed": []}

    body = _post(db, realm, "bill", payload)
    updated = body.get("Bill", {})

    # Mirror what QuickBooks now holds rather than what we asked for.
    if "TotalAmt" in updated:
        newtotal = Decimal(str(updated["TotalAmt"]))
        bill.balance = newtotal - paid
        bill.amount = newtotal
    if payload.get("VendorRef"):
        bill.vendor = vendor_name
    db.add(AuditRow(company=bill.company, action="bill_edited",
                    bill_id=bill.bill_id, detail="; ".join(changed),
                    dry_run=False, actor=actor))
    db.commit()
    return {"status": "updated", "changed": changed}
