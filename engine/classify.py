"""
Charge classification and expense-coding rules.

Every charge takes one of two paths, and getting this split right matters more
than any matching refinement — they are different accounting treatments:

  TICKET_CREDIT  -> payments to a team/venue funding a season-ticket-holder
                    account. There is NO individual bill to pay down. These are
                    coded as EXPENSES by rule (vendor, account, class).

  PURCHASE       -> a one-off ticket buy that should pay down a specific bill.
                    These go to the suggestion engine for reviewer matching.

The classifier is rule-driven and explainable — every routing decision names
the rule that fired, so a reviewer can see why a charge was treated as a season
credit rather than a purchase.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum

from .hal import TC, DEP, validate as hal_validate


class ChargeClass(str, Enum):
    EXPENSE = "expense"                 # -> expense/refund coding, no bill
    BILL_PAYMENT = "bill_payment"       # -> suggest bills to pay down
    FEE = "fee"                         # card/FX/program fees -> expense
    TRANSFER = "transfer"               # loans, wires, internal -> not ours
    UNCLASSIFIED = "unclassified"       # -> review queue


@dataclass
class Classification:
    charge_class: ChargeClass
    confidence: float
    rule: str
    coding: dict = field(default_factory=dict)   # account/vendor/class if expense
    reasons: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Season-ticket account registry.
#
# In production this is backed by the Airtable "HAL" database plus the team
# payment-plan rules: which teams/venues we hold season accounts with, which
# cards fund them, and the expected instalment cadence. Seeded here from teams
# observed in the real July data.
# ---------------------------------------------------------------------------
SEASON_ACCOUNT_TEAMS = {
    "state farm arena", "arena operations", "madison square garden",
    "boston bruins", "buffalo sabres", "san jose sharks", "anaheim ducks",
    "chicago blackhawks", "washington capitals", "colorado avalanche",
    "philadelphia flyers", "pittsburgh penguins", "vegas golden knights",
    "atlanta hawks", "phoenix suns", "philadelphia 76ers", "detroit tigers",
    "cincinnati bengals", "miami dolphins", "indianapolis colts",
    "spurs sports", "kse tickets", "new cardinals stadium",
}

# Cards known to be dedicated to season-ticket funding (from the registry).
SEASON_FUNDING_CARDS: set[str] = set()

# Set at startup from the Airtable mirror. When None, team charges fall back to
# the merchant registry and the category is assumed TC (unverified).
HAL_INDEX = None


def set_hal_index(index):
    global HAL_INDEX
    HAL_INDEX = index

FEE_PATTERNS = [
    re.compile(r"\bfee\b", re.I),
    re.compile(r"foreign transaction", re.I),
    re.compile(r"physical card", re.I),
]

TRANSFER_PATTERNS = [
    re.compile(r"loan", re.I),
    re.compile(r"wire transfer", re.I),
    re.compile(r"between .* accounts", re.I),
]

SEASON_MEMO_PATTERNS = [
    re.compile(r"season", re.I),
    re.compile(r"\bSTH\b", re.I),          # season ticket holder
    re.compile(r"payment plan", re.I),
    re.compile(r"instal?lment", re.I),
]


def _norm(text) -> str:
    return re.sub(r"[^a-z0-9 ]", " ", str(text or "").lower())


def _matches_season_team(merchant: str) -> str | None:
    m = _norm(merchant)
    for team in SEASON_ACCOUNT_TEAMS:
        if team in m:
            return team
        # merchant strings are often squashed: "STATEFARMARENA"
        if team.replace(" ", "") in m.replace(" ", ""):
            return team
    return None


def classify(charge) -> Classification:
    """Route a charge to expense coding or bill-payment suggestions."""
    desc = f"{charge.merchant or ''} {charge.raw_description or ''}"

    # --- refunds / credits: money coming BACK. Never a bill payment. ---
    if getattr(charge, "is_credit", False):
        return Classification(
            ChargeClass.EXPENSE, 0.95, "refund_credit",
            coding={"account": "Inventory Asset", "split": "Clearing Account",
                    "vendor": charge.merchant or "", "category": TC},
            reasons=["money returned (refund/credit) — codes to the same category "
                     "as the charge it reverses"],
        )

    # --- fees and transfers first: never bill payments ---
    for pat in TRANSFER_PATTERNS:
        if pat.search(desc):
            return Classification(
                ChargeClass.TRANSFER, 0.95, "transfer_pattern",
                reasons=["internal transfer / loan / wire — not a ticket transaction"],
            )
    for pat in FEE_PATTERNS:
        if pat.search(desc):
            return Classification(
                ChargeClass.FEE, 0.95, "fee_pattern",
                coding={"account": "Bank & Card Fees"},
                reasons=["card program or FX fee — code as expense"],
            )

    # --- explicit season-ticket language ---
    for pat in SEASON_MEMO_PATTERNS:
        if pat.search(desc):
            team = _matches_season_team(charge.merchant) or (charge.merchant or "")
            return Classification(
                ChargeClass.EXPENSE, 0.95, "season_memo",
                coding={"account": "Inventory Asset", "split": "Clearing Account",
                        "vendor": team},
                reasons=["memo names a season-ticket plan — code as expense, no bill payment"],
            )

    # --- HAL first, driven by the data rather than a hardcoded team list ---
    #
    # The old order asked "is this merchant one of the teams we know about?"
    # using a list written by hand -- so a charge to a team missing from that
    # list never reached HAL at all, and looked like an ordinary purchase.
    #
    # Now HAL decides: if the cardholder has a season-ticket record for a team
    # matching this merchant, it's a team charge. HAL holds 18,000 records
    # across every team you deal with, which is a far better authority than any
    # list maintained in code.
    if HAL_INDEX is not None and charge.cardholder_name:
        v = hal_validate(charge, HAL_INDEX)
        if v.matched and v.team:
            if v.needs_review:
                return Classification(
                    ChargeClass.EXPENSE, 0.4, "hal_no_active_record",
                    coding={"vendor": v.team, "category": None,
                            "season": v.season, "hal_status": v.status,
                            "holder": v.holder},
                    reasons=v.reasons,
                )
            return Classification(
                ChargeClass.EXPENSE, 0.95, "hal_validated",
                coding={"vendor": v.team, "category": v.category,
                        "season": v.season, "hal_status": v.status,
                        "holder": v.holder},
                reasons=v.reasons,
            )

    # --- fallback: the merchant looks like a team we know, but HAL had nothing ---
    team = _matches_season_team(charge.merchant)
    if team:
        # A charge paid DIRECTLY to a team we hold a season account with is a
        # ticket credit funding that account, not a marketplace purchase.
        conf = 0.85
        reasons = [f"paid directly to '{team}' — a season-ticket account team"]
        if charge.card_last4 and charge.card_last4 in SEASON_FUNDING_CARDS:
            conf = 0.95
            reasons.append("card is dedicated to season-ticket funding")
        return Classification(
            ChargeClass.EXPENSE, conf, "season_account_team",
            coding={"account": "Inventory Asset", "split": "Clearing Account",
                    "vendor": team, "category": TC},
            reasons=reasons + ["HAL not connected — category assumed TC, unverified"],
        )

    # --- otherwise: a purchase to be matched against a bill ---
    return Classification(
        ChargeClass.BILL_PAYMENT, 0.7, "default_bill_payment",
        reasons=[("no HAL record for this cardholder and team — "
                  "treating as a purchase, suggesting bills")
                 if HAL_INDEX is not None else
                 "HAL not connected — treating as a purchase"],
    )
