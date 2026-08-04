"""
TicketVault rounding logic — the heart of the confidence check.

The situation (confirmed against how TicketVault builds bills):

    A bill amount is computed as round(unit_price * quantity, 2), where
    unit_price was itself derived by dividing the true total by quantity and
    rounding. When the total doesn't divide evenly, a penny or two is lost.

    Example: true cost $100.00 for 3 tickets.
        unit = round(100.00 / 3, 2)      = 33.33
        bill = round(33.33 * 3, 2)       = 99.99
        gap  = charge(100.00) - bill(99.99) = 0.01

So a small gap between charge and bill is NOT noise to be tolerated blindly.
It is a *predictable artifact* we can reconstruct and confirm. That lets us do
something stronger than "within two cents": we check whether the bill is
exactly what TicketVault would have produced from this charge. If it is, the
difference is explained, and we can auto-correct the bill with confidence.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from .models import money


# Max rounding error TicketVault can introduce is < half a cent per ticket.
# We give it a hair of slack and an absolute floor for tiny ticket counts.
def _rounding_ceiling(quantity: Optional[int]) -> Decimal:
    if quantity and quantity > 0:
        # half a cent per ticket, rounded up to the cent, min one cent
        cents = (Decimal(quantity) * Decimal("0.005")).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        return max(cents, Decimal("0.01"))
    return Decimal("0.02")  # conservative fallback when qty is unknown


def reconstruct_ticketvault_bill(true_total: Decimal, quantity: int) -> Decimal:
    """Reproduce TicketVault's arithmetic: round(round(total/qty) * qty)."""
    unit = (true_total / Decimal(quantity)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    return money(unit * Decimal(quantity))


def explain_gap(charge_amount: Decimal, bill: "Bill") -> dict:
    """
    Decide whether the charge-vs-bill gap is an explainable TicketVault artifact.

    Returns a dict describing the finding:
        gap:        signed charge - bill
        explained:  True if the gap is a genuine TicketVault rounding artifact
        exact:      True if the bill is EXACTLY reconstruct(charge, qty)  (strongest)
        within_ceiling: True if the gap is small enough to plausibly be rounding
        correct_to: the amount the bill should be overwritten to (the charge), or None
    """
    charge_amount = money(charge_amount)
    gap = charge_amount - bill.amount
    ceiling = _rounding_ceiling(bill.quantity)

    finding = {
        "gap": gap,
        "ceiling": ceiling,
        "exact": False,
        "within_ceiling": abs(gap) <= ceiling,
        "explained": False,
        "correct_to": None,
        "note": "",
    }

    if gap == 0:
        finding["note"] = "exact amount match"
        return finding

    # Strongest signal: if we know the ticket count, is the bill EXACTLY what
    # TicketVault would compute from this charge? Then the gap is fully explained.
    if bill.quantity and bill.quantity > 0:
        if reconstruct_ticketvault_bill(charge_amount, bill.quantity) == bill.amount:
            finding["exact"] = True
            finding["explained"] = True
            finding["correct_to"] = charge_amount
            finding["note"] = (
                f"bill = round({charge_amount}/{bill.quantity}) * {bill.quantity} "
                f"= {bill.amount}; TicketVault rounding artifact"
            )
            return finding

    # Weaker fallback: no quantity (or reconstruction didn't line up), but the
    # gap is within the size rounding could produce. Plausibly rounding, but not
    # proven — treat as explainable-but-soft.
    if finding["within_ceiling"]:
        finding["explained"] = True
        finding["correct_to"] = charge_amount
        finding["note"] = (
            f"gap {gap} within rounding ceiling {ceiling}; "
            f"plausible rounding (qty unknown/unconfirmed)"
        )
        return finding

    # Gap too large to be TicketVault rounding. This is a REAL discrepancy.
    # Do not auto-correct; a human should see it.
    finding["note"] = f"gap {gap} exceeds rounding ceiling {ceiling}; not a rounding artifact"
    return finding
