"""
HAL validation — resolves a charge's cardholder to a season-ticket record and
decides the expense category.

Data source (Airtable, base "Tickets" appOEQZDCBXBZRiUv):
    HAL            tblHVnNLLKSnvjoKr   season-ticket records
      25/26 Status fldixaT5UFHyDaFkd   singleSelect
      26/27 Status fldFiTzd1eig0eg0B   singleSelect
      24/25 Status fldrme0zedWhb3mUE   singleSelect
      Full/Partial fldWKMM4HFjzr0i3t   plan type (multipleSelects)
      Year         fldEW82npcMjMRfqm   season starting year
      Team / Address Book / Default card / Payment Plans  (linked records)
    Cards          tblYVHB8AqUthH6ST   has Last 4 (formula), links to HAL
    Payment Plans  tblkko9IoGyAs6S49   per-instalment Date + Amount

The category is driven by the HAL season status, per the accounting rule:

    TC   Team charge — a live season-ticket holder paying into their plan.
    DEP  One-time deposit to get on a waiting list.

Refunds follow the same categorisation as the charge they reverse: a refund on
a waitlist deposit codes DEP, a refund on a plan payment codes TC.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Optional


# --- category constants ------------------------------------------------------
TC = "TC"
DEP = "DEP"


# --- HAL season status buckets ----------------------------------------------
# Exact option names read from the Airtable field config.

STATUS_ACTIVE = {"Active"}

# NOTE (corrected against real data): the TC/DEP split is NOT driven by status.
# It is driven by the Full/Partial field, which carries "Wait List" and
# "DEPOSIT" as options alongside the real plan types. Status answers "is this
# record live for the season"; Full/Partial answers "is this a plan or a
# waitlist deposit". The two are independent -- a record can be Active AND
# Wait List (e.g. San Diego Padres / willieoherty1).
DEP_PLAN_TYPES = {"wait list", "waitlist", "deposit", "partial deposit",
                  "flex deposit", "playoff deposit", "add-on deposit",
                  "ticket bank deposit", "partial plan deposit", "half deposit"}

# No live record for the season — a team charge here is an anomaly worth review.
STATUS_DEAD = {
    "Canceled", "Declined", "Not Renewed", "DO NOT RENEW", "Opted Out",
    "Emailed/Opted Out", "Forfeited", "To Be Refunded", "No Invoice",
    "Locked Out", "Disabled Gmail",
}

# In-progress admin states: validate, but surface the status so a reviewer can
# see the account wasn't settled.
STATUS_IN_PROGRESS = {
    "Pending", "Under Review", "Relocated", "Credit Card", "Form Sent",
    "Check Back", "Contact AM", "Phone Call", "Emailed", "Password",
}


def season_for(txn_date: date, start_month: int = 7) -> str:
    """Which season a charge belongs to.

    Seasons are labelled by starting year, e.g. '26/27'. Charges from the
    start month onward belong to the upcoming season -- a July 2026 charge is
    paying into 26/27. `start_month` is overridable per team from the Teams
    table's Start Month, so leagues that straddle differently can be driven
    from data rather than hardcoded.
    """
    y = txn_date.year if txn_date.month >= start_month else txn_date.year - 1
    return f"{str(y)[2:]}/{str(y + 1)[2:]}"


STATUS_FIELD_BY_SEASON = {
    "25/26": "25/26 Status",
    "26/27": "26/27 Status",
    "27/28": "27/28 Status",
}

# Seasons the mirror carries, newest last. A charge is checked against its own
# season first, then the others -- renewals mean a record can be live for the
# upcoming season while the current one is still blank, or vice versa.
# Newest first: a charge is checked against its own season, then these in turn.
# 25/26 is included because records that are live today often have only that
# status filled in, and treating them as "no record" is worse than validating
# against a slightly stale season and saying so.
TRACKED_SEASONS = ["26/27", "27/28", "25/26"]


def set_tracked_seasons(seasons: list[str]) -> None:
    """Point the validator at whatever the admin chose to mirror."""
    global TRACKED_SEASONS
    if seasons:
        TRACKED_SEASONS = list(seasons)


@dataclass
class HalRecord:
    """A season-ticket record, as loaded from Airtable."""
    record_id: str
    holder_name: str                      # from Address Book
    team: str
    statuses: dict                        # {"26/27": "Active", ...}
    card_last4: Optional[str] = None
    plan_type: str = ""                   # Full/Partial
    has_payment_plan: bool = False
    plan_instalments: list = field(default_factory=list)   # [(date, amount)]

    def status_for(self, season: str) -> Optional[str]:
        return self.statuses.get(season)


@dataclass
class HalValidation:
    matched: bool
    category: Optional[str]               # TC | DEP | None
    season: str
    status: Optional[str] = None
    holder: Optional[str] = None
    team: Optional[str] = None
    needs_review: bool = False
    reasons: list = field(default_factory=list)


# HAL's Address Book values look like "Matthew Jude (CA) Los Angeles 90004".
# The person's name is everything before the state parenthetical -- and that
# name is what shows up as the cardholder on the credit-card charge.
_AB_NAME = re.compile(r"^(.*?)\s*\(")


def person_name(address_book_value: str) -> str:
    m = _AB_NAME.match(str(address_book_value or ""))
    return (m.group(1) if m else str(address_book_value or "")).strip()


def _norm_name(name: str) -> str:
    return " ".join(sorted(str(name or "").lower().replace(",", " ").split()))


def _category_for(rec: "HalRecord") -> str:
    """TC or DEP, decided by the Full/Partial plan type."""
    types = [t.strip().lower() for t in str(rec.plan_type or "").split(",")]
    return DEP if any(t in DEP_PLAN_TYPES for t in types) else TC


def _squash(name: str) -> str:
    """Letters only, lowercased -- so "Akeya Eleni" == "akeyaeleni"."""
    return re.sub(r"[^a-z]", "", str(name or "").lower())


class HalIndex:
    """In-memory mirror of the HAL records, keyed for fast lookup.

    Production: refreshed on a schedule from Airtable so rules run against a
    local copy rather than hitting the API per charge.
    """

    def __init__(self, records: list[HalRecord]):
        self.by_name: dict[str, list[HalRecord]] = {}
        self.by_squash: dict[str, list[HalRecord]] = {}
        self.by_last4: dict[str, list[HalRecord]] = {}
        for r in records:
            self.by_name.setdefault(_norm_name(r.holder_name), []).append(r)
            # Squashed key catches holders whose name came from an email local
            # part ("akeyaeleni"), which has no space to tokenise on.
            sq = _squash(r.holder_name)
            if sq:
                self.by_squash.setdefault(sq, []).append(r)
            if r.card_last4:
                self.by_last4.setdefault(r.card_last4, []).append(r)

    def lookup(self, cardholder_name: str, card_last4: Optional[str]) -> list[HalRecord]:
        """Resolve a cardholder to their HAL records."""
        if card_last4 and card_last4 in self.by_last4:
            return self.by_last4[card_last4]
        hit = self.by_name.get(_norm_name(cardholder_name), [])
        if hit:
            return hit
        # "Akeya Eleni" -> "akeyaeleni", matching an email-derived holder name
        return self.by_squash.get(_squash(cardholder_name), [])


# City names are shared across teams ("San Francisco Giants" vs "San Francisco
# 49ers"), so overlapping on those alone is not a match -- the distinguishing
# word is the nickname.
_TEAM_STOPWORDS = {
    "san", "new", "los", "las", "fort", "saint", "city", "north", "south",
    "east", "west", "football", "baseball", "basketball", "hockey", "club",
    "the", "and", "tickets", "ticket",
}


def _team_score(charge_merchant: str, team: str) -> float:
    """How strongly a charge's merchant refers to a team. 0 means no match.

    Scored rather than boolean because one person holds records for several
    teams in the same city, and picking the first overlap gets the wrong one.
    """
    m, t = _squash(charge_merchant), _squash(team)
    if not m or not t:
        return 0.0
    if m == t:
        return 1.0
    if t in m or m in t:
        return 0.9

    def words(x):
        return {w for w in re.findall(r"[a-z0-9]+", str(x).lower())
                if len(w) > 2 and w not in _TEAM_STOPWORDS}

    mw, tw = words(charge_merchant), words(team)
    if not mw or not tw:
        return 0.0
    shared = mw & tw
    if not shared:
        return 0.0
    return 0.5 * (len(shared) / min(len(mw), len(tw)))


def validate(charge, hal: HalIndex, start_month: int = 7) -> HalValidation:
    """Resolve a team charge to a HAL record and decide TC vs DEP.

    A person typically holds records for SEVERAL teams -- five is common. So
    matching on the cardholder alone picks an arbitrary one, and a charge to
    San Jose Sharks can end up validated against a San Diego FC record that
    says "Opted Out". The team has to be part of the match.
    """
    season = season_for(charge.txn_date, start_month)
    candidates = hal.lookup(charge.cardholder_name or "", charge.card_last4)

    # Narrow to the team the charge was actually paid to. Keep only the best
    # scoring team(s) -- not everything that shares a city name.
    #
    # If NOTHING matches, this isn't a team charge for this person: a theater
    # ticket on the card of someone who happens to hold Blue Jays seats must not
    # be validated against the Blue Jays record. Return no match and let it be
    # treated as an ordinary purchase.
    merchant = getattr(charge, "merchant", "") or ""
    if candidates and merchant:
        scored = [(_team_score(merchant, r.team), r) for r in candidates]
        best = max((s for s, _ in scored), default=0.0)
        if best <= 0:
            return HalValidation(
                matched=False, category=None, season=season, needs_review=False,
                reasons=[f"'{charge.cardholder_name}' holds season tickets, but "
                         f"none for '{merchant}' — not a team charge"],
            )
        candidates = [r for s, r in scored if s == best]

    if not candidates:
        return HalValidation(
            matched=False, category=None, season=season, needs_review=True,
            reasons=[f"'{charge.cardholder_name}' has no HAL record — cannot "
                     f"confirm season-ticket holder"],
        )

    # Prefer a record with a status for the charge's own season; if none has
    # one, accept another tracked season. Mid-renewal, a holder can be live for
    # 27/28 while 26/27 is still blank (or the reverse).
    rec = next((r for r in candidates if r.status_for(season)), None)
    used_season = season
    if rec is None:
        for alt in TRACKED_SEASONS:
            rec = next((r for r in candidates if r.status_for(alt)), None)
            if rec is not None:
                used_season = alt
                break
    if rec is None:
        rec = candidates[0]
    status = rec.status_for(used_season)
    stale = used_season != season
    season = used_season

    v = HalValidation(matched=True, category=None, season=season, status=status,
                      holder=rec.holder_name, team=rec.team)

    if status is None:
        v.needs_review = True
        v.reasons.append(f"HAL record found but no {season} status set")
        return v

    if status in STATUS_ACTIVE:
        v.category = _category_for(rec)
        v.reasons.append(f"active HAL record for {season} — code as {v.category}"
                         + (" (no status set for the charge's own season)"
                            if stale else ""))

    elif status in STATUS_IN_PROGRESS:
        v.category = _category_for(rec)
        v.reasons.append(f"HAL status '{status}' (in progress) — code as "
                         f"{v.category}, account not yet settled")

    elif status in STATUS_DEAD:
        v.needs_review = True
        v.reasons.append(f"HAL status '{status}' for {season} — no live record; "
                         f"team charge on this card is an anomaly")

    else:
        v.needs_review = True
        v.reasons.append(f"unrecognised HAL status '{status}' — review")

    if rec.plan_type:
        v.reasons.append(f"plan: {rec.plan_type}")
    return v
