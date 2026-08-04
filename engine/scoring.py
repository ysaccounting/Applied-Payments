"""
Candidate generation (blocking) + scoring.

Two steps, deliberately separate:

1. candidates(): cheaply narrow the open bills to a handful worth scoring, so we
   never score every charge against every bill. This is the disciplined version
   of QuickBooks' crude "amount within a date range".

2. score(): for each surviving (charge, bill) pair, weigh the evidence and
   produce a confidence score PLUS the per-signal breakdown that tells a
   reviewer *why*. The score is an explicit, explainable scorecard — no black
   box — which is what you want on day one with no labeled data and an auditor
   who may ask how a match was decided.
"""

from __future__ import annotations

from datetime import timedelta
from difflib import SequenceMatcher
from typing import List

from .models import Bill, Charge
from .rounding import explain_gap


DATE_WINDOW = timedelta(days=5)   # charge should post near the bill date


def _name_similarity(a: str, b: str) -> float:
    """Token-sorted fuzzy match so 'John Smith' ~ 'Smith, John'."""
    if not a or not b:
        return 0.0
    ta = " ".join(sorted(a.lower().replace(",", " ").split()))
    tb = " ".join(sorted(b.lower().replace(",", " ").split()))
    return SequenceMatcher(None, ta, tb).ratio()


def candidates(charge: Charge, bills: List[Bill]) -> List[Bill]:
    """Cheap pre-filter: same company, open, near in date, and sharing at least
    one strong identifier OR a plausible amount."""
    out = []
    for b in bills:
        if b.company != charge.company or not b.is_open:
            continue
        if abs((b.txn_date - charge.txn_date).days) > DATE_WINDOW.days:
            continue
        # keep it if any identifier lines up, or the amount is in the ballpark
        # (ballpark = within a dollar, to catch rounding cases before scoring)
        if (
            (charge.order_number and charge.order_number == b.order_number)
            or (charge.card_last4 and charge.card_last4 == b.card_last4)
            or abs(charge.amount - b.amount) <= 1
        ):
            out.append(b)
    return out


# Signal weights. These are a starting scorecard, tuned in discovery against
# real match rates. Order number is near-decisive; amount+last4+date together
# are strong; name/email corroborate.
WEIGHTS = {
    "order_number": 0.40,
    "card_last4": 0.20,
    "amount": 0.25,
    "date": 0.05,
    "name": 0.07,
    "email": 0.03,
}


def score(charge: Charge, bill: Bill) -> dict:
    """Return {'score': float 0..1, 'signals': {...}, 'reasons': [...],
    'rounding': <finding>}."""
    signals = {}
    reasons = []
    s = 0.0

    # order number — the disambiguator when present
    if charge.order_number and bill.order_number:
        if charge.order_number == bill.order_number:
            signals["order_number"] = "match"
            s += WEIGHTS["order_number"]
            reasons.append(f"order # {charge.order_number} matches")
        else:
            signals["order_number"] = "mismatch"
            reasons.append("order # differs")
    else:
        signals["order_number"] = "absent"

    # card last-4
    if charge.card_last4 and bill.card_last4:
        if charge.card_last4 == bill.card_last4:
            signals["card_last4"] = "match"
            s += WEIGHTS["card_last4"]
            reasons.append(f"last-4 {charge.card_last4} matches")
        else:
            signals["card_last4"] = "mismatch"
    else:
        signals["card_last4"] = "absent"

    # amount — with the rounding-aware logic built in
    rounding = explain_gap(charge.amount, bill)
    if rounding["gap"] == 0:
        signals["amount"] = "exact"
        s += WEIGHTS["amount"]
        reasons.append("amount exact")
    elif rounding["exact"]:
        # bill is exactly TicketVault's reconstruction of the charge:
        # treat as a full amount match, it's a proven rounding artifact
        signals["amount"] = "rounding_exact"
        s += WEIGHTS["amount"]
        reasons.append(f"amount off by {rounding['gap']} — TicketVault rounding (explained)")
    elif rounding["explained"]:
        signals["amount"] = "rounding_soft"
        s += WEIGHTS["amount"] * 0.9      # small penalty: plausible but unproven
        reasons.append(f"amount off by {rounding['gap']} — within rounding tolerance")
    else:
        signals["amount"] = "mismatch"
        reasons.append(f"amount off by {rounding['gap']} — NOT rounding")

    # date proximity
    days = abs((bill.txn_date - charge.txn_date).days)
    if days <= DATE_WINDOW.days:
        signals["date"] = f"within {days}d"
        s += WEIGHTS["date"] * (1 - days / (DATE_WINDOW.days + 1))
    else:
        signals["date"] = "outside window"

    # name
    if charge.cardholder_name and bill.name:
        sim = _name_similarity(charge.cardholder_name, bill.name)
        if sim >= 0.8:
            signals["name"] = f"match ({sim:.0%})"
            s += WEIGHTS["name"] * sim
            reasons.append(f"name matches ({sim:.0%})")
        else:
            signals["name"] = f"weak ({sim:.0%})"
    else:
        signals["name"] = "absent"

    # email
    if charge.email and bill.email and charge.email == bill.email:
        signals["email"] = "match"
        s += WEIGHTS["email"]
        reasons.append("email matches")
    else:
        signals["email"] = "absent" if not (charge.email and bill.email) else "mismatch"

    return {
        "score": round(min(s, 1.0), 4),
        "signals": signals,
        "reasons": reasons,
        "rounding": rounding,
    }
