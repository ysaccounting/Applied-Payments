"""
Core data shapes.

Everything flowing through the engine is one of three things: a Charge (a
credit-card transaction, from any source), a Bill (a recorded purchase read
from QuickBooks), or a MatchResult (the engine's decision about a charge).

The whole point of normalizing every source into `Charge` is that once a
transaction is in this shape, the matcher does not care whether it came from
Slash, WEX, Divvy, Amex or Chase. Only the ingestion adapter differs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Optional


# Money is Decimal, never float. Financial rounding on floats silently lies
# ($0.1 + 0.2 != 0.3), and this whole engine turns on getting pennies right.
def money(value) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"))


@dataclass
class Charge:
    """A credit-card charge, normalized from any source."""
    charge_id: str
    company: str
    source: str                       # "slash", "wex", "divvy", "amex", ...
    amount: Decimal
    txn_date: date
    card_last4: Optional[str] = None
    cardholder_name: Optional[str] = None
    email: Optional[str] = None
    # Every address known for this cardholder. A profile buys under several,
    # and a bill can name any of them, so the match is membership rather than
    # equality. Defaults to empty, which behaves exactly as before.
    alt_emails: set = field(default_factory=set)
    order_number: Optional[str] = None
    raw_description: str = ""          # original memo/description text
    merchant: str = ""                 # bank detail: merchant/description
    is_credit: bool = False            # positive-amount (money back) row

    def __post_init__(self):
        self.amount = money(self.amount)


@dataclass
class Bill:
    """A recorded purchase read from QuickBooks."""
    bill_id: str                      # QuickBooks internal Id
    company: str
    amount: Decimal
    txn_date: date
    balance: Decimal                  # from QBO; > 0 means open in A/P aging
    line_count: int = 1               # 99.99% are single-line
    quantity: Optional[int] = None    # ticket count, when known (drives rounding logic)
    vendor: Optional[str] = None
    # QuickBooks "Bill no." -- what people actually cite, as opposed to the
    # internal Id used for API calls.
    doc_number: str = ""
    # matchable identifiers, mostly parsed out of the bill memo:
    name: Optional[str] = None
    email: Optional[str] = None
    order_number: Optional[str] = None
    card_last4: Optional[str] = None
    memo: str = ""

    def __post_init__(self):
        self.amount = money(self.amount)
        self.balance = money(self.balance)

    @property
    def is_open(self) -> bool:
        """Only open bills are candidates. Balance > 0 == open in A/P aging."""
        return self.balance > Decimal("0")


class Decision(str, Enum):
    AUTO_MATCH = "auto_match"          # high confidence, post without review
    REVIEW = "review"                  # medium: human confirms in the queue
    EXCEPTION = "exception"            # low/ambiguous: work the exception queue
    MISSING_BUYIN = "missing_buyin"    # charge with no bill at all -> ops


@dataclass
class MatchResult:
    charge: Charge
    bill: Optional[Bill]
    score: float
    decision: Decision
    signals: dict = field(default_factory=dict)   # which evidence agreed
    reasons: list = field(default_factory=list)   # human-readable explanation
    # bill-overwrite handling for the TicketVault rounding case:
    overwrite_bill_amount: Optional[Decimal] = None
    rounding_explained: bool = False
    rounding: dict = field(default_factory=dict)   # gap analysis from rounding.explain_gap
