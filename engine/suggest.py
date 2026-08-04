"""
Suggestion engine — ranked match candidates for human review.

This is NOT auto-matching. For each charge it returns the top-N most likely
bills, each with a score and plain-English reasons, so a reviewer picks from a
short list instead of searching. The bar is "is the right bill in the top 3-5",
not "did we get it right unattended".

Signals available on real Y&S data (measured, not assumed):
  - date proximity      charge date vs bill date
  - amount closeness    exact, rounding-tolerance, or near
  - text overlap        charge bank detail (merchant + cardholder) vs bill
                        vendor name + memo

Deliberately NOT used: order number (only 12% of bills) and card last-4 (absent
from bills entirely). The engine leans on what the data actually carries.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from difflib import SequenceMatcher

# Words that appear in nearly every merchant/vendor string and carry no
# discriminating power. Stripped before text comparison.
STOPWORDS = {
    "tickets", "ticket", "tm", "the", "inc", "llc", "co", "com", "arena",
    "stadium", "center", "centre", "field", "park", "sports", "entertainment",
    "ent", "operations", "mlb", "nba", "nhl", "nfl", "us", "usa", "i",
}

DATE_WINDOW_DAYS = 10       # generous: suggestions, not auto-match
MAX_SUGGESTIONS = 5


def tokens(text: str) -> set[str]:
    """Normalize free text to comparable word tokens."""
    if not text:
        return set()
    words = re.findall(r"[a-z0-9]+", str(text).lower())
    return {w for w in words if w not in STOPWORDS and len(w) > 2}


# Marketplace prefixes a bank feed puts in front of a purchase. The bill's
# VENDOR names the marketplace ("Ticketmaster", "Gotickets"), so this is the
# most reliable bridge between the two sides -- far more so than the performer,
# which frequently doesn't reach the bank detail at all.
#
# "TM *" is Ticketmaster. Bare "TM" is NOT: a charge reading "TM CHICAGO BULLS"
# is the team billing directly, and matching it to a Ticketmaster bill would be
# wrong. The asterisk is the whole distinction, so it is matched literally.
MARKETPLACE_PREFIXES = [
    (re.compile(r"\bTM\s*\*", re.I), "ticketmaster"),
    (re.compile(r"\bTICKETMASTER\b", re.I), "ticketmaster"),
    (re.compile(r"\bSEATGEEK\b", re.I), "seatgeek"),
    (re.compile(r"\bSTUBHUB\b", re.I), "stubhub"),
    (re.compile(r"\bVIVID\s*SEATS\b", re.I), "vivid seats"),
    (re.compile(r"\bGOTICKETS\b", re.I), "gotickets"),
    (re.compile(r"\bTICKPICK\b", re.I), "tickpick"),
    (re.compile(r"\bAXS\b", re.I), "axs"),
    (re.compile(r"\bLIVE\s*NATION\b", re.I), "live nation"),
]


def marketplace_of(bank_detail: str) -> str:
    """Which ticketing marketplace this bank detail names, if any."""
    text = bank_detail or ""
    for pattern, name in MARKETPLACE_PREFIXES:
        if pattern.search(text):
            return name
    return ""


def vendor_match(bank_detail: str, bill_vendor: str) -> float:
    """1.0 when the bank detail names the same marketplace as the bill's vendor.

    Deliberately strict. The point of this signal is that "TM *" identifies
    Ticketmaster with near-certainty; loosening it to a fuzzy comparison would
    let "TM CHICAGO BULLS" -- a team charge -- match Ticketmaster bills, which
    is exactly the confusion it exists to prevent.
    """
    mk = marketplace_of(bank_detail)
    if not mk or not bill_vendor:
        return 0.0
    return 1.0 if mk in bill_vendor.strip().lower() else 0.0


def text_overlap(charge_text: str, bill_text: str) -> float:
    """How much of the charge's bank detail appears in the bill's vendor/memo.

    Uses token overlap first (robust to word order and extra noise), then falls
    back to fuzzy string similarity for near-misses like
    'BROADWAYACROSSAMERICA' vs 'Broadway Across America'.
    """
    ct, bt = tokens(charge_text), tokens(bill_text)
    if not ct or not bt:
        return 0.0

    shared = ct & bt
    if shared:
        return len(shared) / min(len(ct), len(bt))

    # No shared tokens: merchant strings are often run together with no spaces,
    # so compare the squashed forms fuzzily.
    a = "".join(sorted(ct))
    b = "".join(sorted(bt))
    ratio = SequenceMatcher(None, a, b).ratio()
    return ratio if ratio >= 0.6 else 0.0


@dataclass
class Suggestion:
    bill_id: str
    score: float
    reasons: list[str] = field(default_factory=list)
    date_gap_days: int = 0
    amount_gap: Decimal = Decimal("0.00")


# Weights: amount and text carry the most information; date narrows the field.
W_AMOUNT = 0.38
W_TEXT = 0.27
W_DATE = 0.15
W_NAME_EMAIL = 0.20     # cardholder name found inside the bill's buyer email
# An EXACT email match between the charge's cardholder and the bill's buyer.
# Weighted above the fuzzy name-in-email signal because it is a different kind
# of evidence: "kaylsbentley@gmail.com == kaylsbentley@gmail.com" identifies the
# person, where "surname appears in the address" only suggests it. Most charges
# could never produce this -- only Divvy's export carries an email -- until the
# address book supplied one for every cardholder.
W_EMAIL = 0.32
# The bank detail names the same marketplace as the bill's vendor. Separate from
# W_TEXT because it is a different question: text overlap asks "do these
# describe the same event", this asks "did the money go to the same place".
W_VENDOR = 0.22


# The weights above are defaults. They can be overridden at runtime from the
# Match Weights table, so tuning does not need a deploy.
DEFAULT_WEIGHTS = {
    "amount": W_AMOUNT, "text": W_TEXT, "date": W_DATE,
    "name_email": W_NAME_EMAIL, "email": W_EMAIL, "vendor": W_VENDOR,
}

_W = dict(DEFAULT_WEIGHTS)


def set_weights(overrides: dict) -> None:
    """Replace the live weights. Unknown keys and bad values are ignored."""
    global _W
    merged = dict(DEFAULT_WEIGHTS)
    for k, v in (overrides or {}).items():
        if k in merged:
            try:
                f = float(v)
            except (TypeError, ValueError):
                continue
            if 0 <= f <= 1:
                merged[k] = f
    _W = merged


def current_weights() -> dict:
    return dict(_W)


def name_in_email(cardholder: str, email: str) -> float:
    """How strongly the cardholder's name appears in the bill's buyer email.

    Y&S bill memos carry the buyer's email, and it very often encodes their
    name -- "Shannon Tanner" -> shannontanner@outlook.com. That's the closest
    thing to a shared identifier between the card side (which knows names) and
    the ledger side (which knows emails).

    It is NOT always true, so this is a strong positive signal when present and
    costs nothing when absent -- never a penalty, or legitimate matches on
    cards bought for someone else would be pushed down.

    Returns 1.0 for both names present, 0.6 for surname only, 0.0 for no match.
    """
    if not cardholder or not email:
        return 0.0
    local = email.split("@")[0].lower()
    local_alpha = re.sub(r"[^a-z]", "", local)
    if not local_alpha:
        return 0.0

    parts = [p for p in re.split(r"[^a-zA-Z]+", cardholder.lower()) if len(p) > 1]
    if not parts:
        return 0.0

    hits = [p for p in parts if p in local_alpha]
    if len(hits) >= 2:
        return 1.0
    if hits:
        # a single hit counts for more if it's the surname (usually distinctive)
        return 0.6 if hits[0] == parts[-1] else 0.4
    return 0.0


def score_pair(charge, bill) -> Suggestion | None:
    """Score one charge against one bill. Returns None if implausible."""
    gap_days = abs((bill.txn_date - charge.txn_date).days)
    if gap_days > DATE_WINDOW_DAYS:
        return None

    reasons: list[str] = []
    score = 0.0

    # --- amount ---
    amount_gap = charge.amount - bill.amount
    abs_gap = abs(amount_gap)
    if abs_gap == 0:
        score += _W["amount"]
        reasons.append("amount matches exactly")
    elif abs_gap <= Decimal("0.05"):
        score += _W["amount"] * 0.95
        reasons.append(f"amount off by {amount_gap:+} (rounding)")
    elif abs_gap <= Decimal("1.00"):
        score += _W["amount"] * 0.7
        reasons.append(f"amount off by {amount_gap:+}")
    else:
        # proportional falloff: a $5 gap on $2000 is closer than on $50
        rel = float(abs_gap) / max(float(bill.amount), 1.0)
        if rel <= 0.02:
            score += _W["amount"] * 0.4
            reasons.append(f"amount within 2% ({amount_gap:+})")
        else:
            return None      # too far apart to be worth suggesting

    # --- text: charge bank detail vs bill vendor + memo ---
    charge_text = f"{charge.merchant or ''} {charge.cardholder_name or ''}"
    # The marketplace first. "TM *JACKSON BROWNE" tells you the money went to
    # Ticketmaster even though the bill's memo names Jackson Browne and the bank
    # detail never mentions Ticketmaster by name -- and the performer often
    # doesn't reach the bank detail at all, so the event comparison alone misses
    # these entirely.
    mk = vendor_match(charge.merchant or "", bill.vendor or "")
    if mk:
        score += _W["vendor"] * mk
        reasons.append(f"bank detail names {marketplace_of(charge.merchant or '')}")

    # Then the event/memo comparison, which catches the cases where the
    # performer or team IS in the bank detail.
    event = (bill.memo or "").split("/")[0].strip()
    overlap = max(
        text_overlap(charge_text, event),
        text_overlap(charge_text, f"{bill.vendor or ''} {bill.memo or ''}"),
    )
    if overlap > 0:
        score += _W["text"] * overlap
        if overlap >= 0.5:
            reasons.append("merchant matches bill event/vendor")
        else:
            reasons.append("partial merchant/vendor match")

    # --- buyer identity ---
    #
    # An exact email match supersedes the fuzzy one rather than stacking with
    # it: they are two readings of the same evidence, and counting both would
    # let one signal contribute half the score.
    # Membership, not equality: a profile buys under several addresses and the
    # bill names whichever was used, so any one of them identifies the buyer.
    b_email = (bill.email or "").strip().lower()
    known = {e.strip().lower() for e in (getattr(charge, "alt_emails", None) or set())}
    if charge.email:
        known.add(charge.email.strip().lower())
    if b_email and b_email in known:
        score += _W["email"]
        reasons.append("same buyer email")
    else:
        ne = name_in_email(charge.cardholder_name or "", bill.email or "")
        if ne > 0:
            score += _W["name_email"] * ne
            reasons.append("cardholder name matches buyer email"
                           if ne >= 1.0 else "cardholder surname matches buyer email")

    # --- date proximity ---
    closeness = 1 - (gap_days / (DATE_WINDOW_DAYS + 1))
    score += _W["date"] * closeness
    if gap_days == 0:
        reasons.append("same date")
    else:
        reasons.append(f"{gap_days}d apart")

    return Suggestion(
        bill_id=bill.bill_id,
        score=round(min(score, 1.0), 4),
        reasons=reasons,
        date_gap_days=gap_days,
        amount_gap=amount_gap,
    )


def suggest(charge, bills, limit: int = MAX_SUGGESTIONS) -> list[Suggestion]:
    """Return the top-N candidate bills for a charge, best first."""
    out = []
    for bill in bills:
        if bill.company != charge.company or not bill.is_open:
            continue
        s = score_pair(charge, bill)
        if s is not None:
            out.append(s)
    out.sort(key=lambda s: s.score, reverse=True)
    return out[:limit]
