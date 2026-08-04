"""
Review queue — what the frontend renders.

Takes the charges and bills already in the database, runs each charge through
the classifier, and for bill-payment charges ranks candidate bills. This is the
read model behind the review screens: nothing here writes.
"""

from __future__ import annotations

import json
import re
from collections import Counter

from sqlalchemy import select, func, case, literal
from sqlalchemy.orm import Session

from engine import Bill, Charge, ChargeClass, classify, suggest, set_hal_index
from engine.hal import HalIndex, HalRecord
from .models_db import BillRow, ChargeRow, HalRow

MAX_SUGGESTIONS = 10   # UI lets the reviewer show top 3 / 5 / 10


# name -> email, from the address-book mirror. Loaded once per request batch:
# a few hundred rows, consulted for every charge on the page.
_EMAIL_BY_NAME: dict[str, set[str]] | None = None


def _emails_for(db: Session, name: str) -> set[str]:
    """Every address on file for a cardholder."""
    global _EMAIL_BY_NAME
    if _EMAIL_BY_NAME is None:
        from .models_db import ProfileEmailRow
        _EMAIL_BY_NAME = {}
        for r in db.query(ProfileEmailRow).all():
            if r.email:
                _EMAIL_BY_NAME.setdefault(r.name_key, set()).add(r.email)
    return _EMAIL_BY_NAME.get((name or "").strip().lower(), set())


def reset_email_cache() -> None:
    global _EMAIL_BY_NAME
    _EMAIL_BY_NAME = None


def _to_charge(r: ChargeRow, db: Session | None = None) -> Charge:
    # The charge's own email wins; the address book fills the gap. Slash and WEX
    # exports name the cardholder but never their address, so without this the
    # email signal is dead for two of the three card programs.
    #
    # A profile usually has more than one address and a bill may name any of
    # them, so all are carried and any one counts as a match.
    known = _emails_for(db, r.cardholder_name) if db is not None else set()
    email = r.email or (sorted(known)[0] if known else "")
    if r.email:
        known = known | {r.email.strip().lower()}
    return Charge(
        charge_id=r.charge_id, company=r.company, source=r.source,
        amount=r.amount, txn_date=r.txn_date, card_last4=r.card_last4,
        cardholder_name=r.cardholder_name, email=email or None,
        alt_emails=known,
        order_number=r.order_number, raw_description=r.raw_description or "",
        merchant=(r.merchant or r.raw_description or "").strip(),
        is_credit=bool(r.is_credit),
    )


def _to_bill(r: BillRow) -> Bill:
    return Bill(
        bill_id=r.bill_id, company=r.company, amount=r.amount,
        txn_date=r.txn_date, balance=r.balance, line_count=r.line_count,
        vendor=r.vendor, doc_number=r.doc_number or "",
        name=r.name, email=r.email,
        order_number=r.order_number, memo=r.memo or "",
    )


def load_hal_index(db: Session) -> HalIndex | None:
    """Cached — see app/hal_cache.py. Rebuilding ~18k records per request was
    a second or two of latency on every click."""
    from .hal_cache import get_index
    return get_index(db)


from datetime import date as _dt_date

_NULL_DATE = _dt_date.min


def _order_by(sort: str, direction: str, nick: dict, db, company: str):
    """SQL ORDER BY clauses matching what the Python sort used to produce.

    Two things need care:

    * **Nulls.** The old code keyed a NULL date as date.min and a NULL amount as
      0, so nulls sorted first ascending. COALESCE reproduces that on both
      SQLite and Postgres, whose native NULL ordering differs from each other's.

    * **Card.** That column shows the account NICKNAME, which lives in
      source_accounts, not on the charge. Ordering by ChargeRow.source instead
      would be wrong whenever a nickname doesn't sort like its source key, so we
      rank the sources by nickname and order by that rank explicitly.

    A charge_id tiebreaker is appended to every ordering. Without a fully
    deterministic sort, OFFSET paging can show the same row twice and skip
    another -- invisible until someone resolves a charge they saw twice.
    """
    desc_ = direction != "asc"

    def d(col):
        return col.desc() if desc_ else col.asc()

    if sort == "account":
        sources = [r for (r,) in db.query(ChargeRow.source)
                   .filter(ChargeRow.company == company).distinct().all()]
        for k in nick:
            if k not in sources:
                sources.append(k)
        ranked = sorted(sources, key=lambda x: (nick.get(x, x) or "").lower())
        if ranked:
            rank = case({src: i for i, src in enumerate(ranked)},
                        value=ChargeRow.source, else_=len(ranked))
            return [d(rank), ChargeRow.charge_id.asc()]
        return [d(ChargeRow.source), ChargeRow.charge_id.asc()]

    if sort == "category":
        shown = case((ChargeRow.resolution == "matched", literal("Bill Payment")),
                     else_=ChargeRow.coded_category)
        return [d(func.lower(func.coalesce(shown, ""))), ChargeRow.charge_id.asc()]

    if sort == "cvendor":
        # The column falls back to the bill's vendor for a payment, so the sort
        # has to use the same value the eye sees.
        shown = case((ChargeRow.resolution == "matched", ChargeRow.matched_bill_vendor),
                     else_=ChargeRow.coded_vendor)
        return [d(func.lower(func.coalesce(shown, ""))), ChargeRow.charge_id.asc()]

    if sort == "status":
        # Strong first by default -- the STORED score, so the ordering covers
        # every matching charge rather than the page in front of you.
        return [d(ChargeRow.score), ChargeRow.charge_id.asc()]

    if sort == "user":
        return [d(func.lower(func.coalesce(ChargeRow.resolved_by, ""))),
                ChargeRow.charge_id.asc()]

    if sort == "ts":
        return [d(ChargeRow.resolved_at), ChargeRow.charge_id.asc()]

    if sort == "kind":
        # Bill payment, then expense, then refund -- the order they appear in
        # the filter, so the column and the control agree.
        rank = case(
            (ChargeRow.resolution == "matched", 0),
            (ChargeRow.is_credit.is_(True), 2),
            else_=1)
        return [d(rank), ChargeRow.charge_id.asc()]

    if sort == "details":
        # Sort on whatever the column leads with: the bill number for a
        # payment, the category for a coding.
        lead = case((ChargeRow.resolution == "matched", ChargeRow.matched_bill_no),
                    else_=ChargeRow.coded_category)
        return [d(func.lower(func.coalesce(lead, ""))), ChargeRow.charge_id.asc()]

    zero = literal(0)
    cols = {
        "amount": [func.coalesce(ChargeRow.amount, zero)],
        "vendor": [func.lower(func.coalesce(ChargeRow.merchant, ""))],
        "profile": [func.lower(func.coalesce(ChargeRow.cardholder_name, ""))],
        "card": [func.coalesce(ChargeRow.card_last4, "")],
        "memo": [func.lower(func.coalesce(ChargeRow.memo, ""))],
        "date": [func.coalesce(ChargeRow.txn_date, _NULL_DATE),
                 func.coalesce(ChargeRow.amount, zero)],
    }.get(sort, [func.coalesce(ChargeRow.txn_date, _NULL_DATE),
                 func.coalesce(ChargeRow.amount, zero)])
    return [d(c) for c in cols] + [ChargeRow.charge_id.asc()]


def _candidate_postable(charge_amount, bill_row) -> bool:
    """Would matching this charge to this bill survive the write path?

    Asks the same question qbo_write asks at post time, so the queue never
    offers a Match it would then refuse.
    """
    if bill_row is None:
        return False
    from .integrations.qbo_write import match_is_postable
    try:
        return bool(match_is_postable(charge_amount, bill_row)["ok"])
    except Exception:
        # A verdict we can't compute shouldn't hide the candidate outright --
        # the reviewer still sees it, and the write path remains the backstop.
        return True


CHARGE_FILTER_KEYS = ("status", "card", "source", "date_from", "date_to",
                      "amt_min", "amt_max", "vendor", "profile", "last4",
                      "memo", "details", "cvendor", "category", "user",
                      "ts_from", "ts_to", "currency", "txn_type", "kinds",
                      "tier", "matchable", "q")


def apply_charge_filters(qy, f: dict):
    """Every charge filter, in one place.

    The grid, the tab counts and the per-card counts all have to narrow the
    same way. When this logic was duplicated, the header said "Divvy (172)"
    above a grid showing one row, because only the grid knew about the date
    filter.
    """
    from sqlalchemy import or_ as _or, and_ as _and, false as _false

    status = f.get("status")
    if status:
        qy = qy.where(ChargeRow.status == status)
    # `card` is one or more card ACCOUNTS (programs like slash/divvy/wex),
    # comma-separated -- not individual virtual cards, of which there are
    # hundreds and nobody filters by one.
    if f.get("card"):
        wanted = [c.strip() for c in str(f["card"]).split(",") if c.strip()]
        if wanted:
            qy = qy.where(ChargeRow.source.in_(wanted))
    if f.get("source"):
        qy = qy.where(ChargeRow.source == f["source"])
    if f.get("date_from"):
        qy = qy.where(ChargeRow.txn_date >= f["date_from"])
    if f.get("date_to"):
        qy = qy.where(ChargeRow.txn_date <= f["date_to"])
    if f.get("amt_min") is not None:
        qy = qy.where(ChargeRow.amount >= f["amt_min"])
    if f.get("amt_max") is not None:
        qy = qy.where(ChargeRow.amount <= f["amt_max"])
    # Separate fields rather than one search box: reviewers look for a specific
    # vendor OR a specific person, and a combined search matches neither well.
    if f.get("vendor"):
        qy = qy.where(ChargeRow.merchant.ilike(f"%{f['vendor']}%"))
    if f.get("profile"):
        qy = qy.where(ChargeRow.cardholder_name.ilike(f"%{f['profile']}%"))
    if f.get("last4"):
        qy = qy.where(ChargeRow.card_last4.ilike(f"%{f['last4']}%"))
    if f.get("memo"):
        qy = qy.where(ChargeRow.memo.ilike(f"%{f['memo']}%"))
    # The Vendor column shows whichever applies: the coded vendor once resolved,
    # the bill's vendor for a payment, the engine's suggestion while still in
    # For Review. The filter looks at all three or it misses what's on screen.
    if f.get("cvendor"):
        _cv = f"%{f['cvendor']}%"
        qy = qy.where(_or(ChargeRow.coded_vendor.ilike(_cv),
                          ChargeRow.matched_bill_vendor.ilike(_cv),
                          ChargeRow.suggested_vendor.ilike(_cv)))
    # Same for Category. "Bill Payment" is not stored anywhere -- it is what the
    # column prints for a matched charge -- so it is matched explicitly.
    if f.get("category"):
        _ct = f"%{f['category']}%"
        _cl = [ChargeRow.coded_category.ilike(_ct),
               ChargeRow.suggested_category.ilike(_ct)]
        if "bill payment".startswith(str(f["category"]).strip().lower()[:12]):
            _cl.append(ChargeRow.resolution == "matched")
        qy = qy.where(_or(*_cl))
    # No export carries a currency field, so CAD means "tagged Canadian" and
    # USD means everything else. Named Currency because that is the question
    # being asked, even though the evidence is the bank detail.
    if f.get("currency") == "CAD":
        qy = qy.where(ChargeRow.is_canadian.is_(True))
    elif f.get("currency") == "USD":
        qy = qy.where(ChargeRow.is_canadian.is_(False))
    if f.get("user"):
        qy = qy.where(ChargeRow.resolved_by.ilike(f"%{f['user']}%"))
    if f.get("ts_from"):
        qy = qy.where(ChargeRow.resolved_at >= f["ts_from"])
    if f.get("ts_to"):
        qy = qy.where(ChargeRow.resolved_at <= f["ts_to"])
    if f.get("details"):
        _d = f"%{f['details']}%"
        qy = qy.where(_or(ChargeRow.matched_bill_no.ilike(_d),
                          ChargeRow.matched_bill_vendor.ilike(_d),
                          ChargeRow.matched_bill_date.ilike(_d),
                          ChargeRow.matched_bill_memo.ilike(_d),
                          ChargeRow.coded_vendor.ilike(_d),
                          ChargeRow.coded_category.ilike(_d)))
    # Amounts are stored absolute with is_credit carrying the sign, so an amount
    # range can't express "refunds only" -- this is the only way to ask.
    # Multi-select: a comma list. Selecting everything means the same as
    # selecting nothing, and the UI sends neither.
    if f.get("txn_type"):
        if str(f["txn_type"]) == "\u0000none":
            return qy.where(_false())
        _want = {t.strip() for t in str(f["txn_type"]).split(",") if t.strip()}
        _tc = []
        if "charges" in _want:
            _tc.append(ChargeRow.is_credit.is_(False))
        if "refunds" in _want:
            _tc.append(_and(ChargeRow.is_credit.is_(True),
                            ChargeRow.is_card_payment.is_(False)))
        if "payments" in _want:
            _tc.append(_and(ChargeRow.is_credit.is_(True),
                            ChargeRow.is_card_payment.is_(True)))
        qy = qy.where(_or(*_tc) if _tc else _false())
    # On Categorized the question is what the charge was resolved AS, which is
    # a different axis: a bill payment, an expense, or money coming back.
    if f.get("kinds"):
        if str(f["kinds"]) == "\u0000none":
            return qy.where(_false())
        want = {k.strip() for k in f["kinds"].split(",") if k.strip()}
        clauses = []
        if "bill_payment" in want:
            clauses.append(ChargeRow.resolution == "matched")
        if "expense" in want:
            clauses.append(_and(ChargeRow.resolution == "coded",
                                ChargeRow.is_credit.is_(False)))
        # Money coming back splits two ways, as it does on For Review: a refund
        # reverses a purchase, a payment settles the card.
        if "refund" in want:
            clauses.append(_and(ChargeRow.resolution == "coded",
                                ChargeRow.is_credit.is_(True),
                                ChargeRow.is_card_payment.is_(False)))
        if "payment" in want:
            clauses.append(_and(ChargeRow.resolution == "coded",
                                ChargeRow.is_credit.is_(True),
                                ChargeRow.is_card_payment.is_(True)))
        qy = qy.where(_or(*clauses) if clauses else _false())
    # "Match" means a bill candidate exists; "Add" means there isn't one, so
    # coding is the only route. Derived from the stored tier: "none" is exactly
    # the no-candidate case.
    if f.get("matchable") == "match":
        qy = qy.where(ChargeRow.tier != "none")
    elif f.get("matchable") == "add":
        qy = qy.where(ChargeRow.tier == "none")
    if str(f.get("tier") or "") == "\u0000none":
        return qy.where(_false())
    if str(f.get("matchable") or "") == "\u0000none":
        return qy.where(_false())
    if f.get("tier"):
        wanted = [t.strip() for t in str(f["tier"]).split(",") if t.strip()]
        if wanted:
            qy = qy.where(ChargeRow.tier.in_(wanted))
    if f.get("q"):
        like = f"%{f['q']}%"
        qy = qy.where(_or(ChargeRow.merchant.ilike(like),
                          ChargeRow.cardholder_name.ilike(like),
                          ChargeRow.card_last4.ilike(like),
                          ChargeRow.matched_bill_id.ilike(like)))
    return qy


_ACCOUNT_NAMES: set[str] | None = None


_VENDOR_NAMES: set[str] | None = None


def _known_vendor(db: Session, name: str) -> str:
    """The name back if QuickBooks has that vendor, otherwise blank.

    Same reasoning as categories: the engine composes vendor names from the
    event and team, and not every one it can build exists. A suggestion the
    ledger will reject looks decided, so it gets committed and fails at post
    time -- blank at least asks the question.
    """
    global _VENDOR_NAMES
    if not name:
        return ""
    if _VENDOR_NAMES is None:
        from .models_db import QboRefRow
        _VENDOR_NAMES = {r.name for r in db.query(QboRefRow.name).filter(
            QboRefRow.kind == "vendor").all() if r.name}
    if not _VENDOR_NAMES:      # nothing synced yet -- don't blank everything
        return name
    return name if name in _VENDOR_NAMES else ""


def _known_account(db: Session, name: str) -> str:
    """The name back if QuickBooks has that account, otherwise blank.

    Cached per request-batch: the chart of accounts is a few hundred rows and
    this runs once per charge on the page.
    """
    global _ACCOUNT_NAMES
    if not name:
        return ""
    if _ACCOUNT_NAMES is None:
        from .models_db import QboRefRow
        _ACCOUNT_NAMES = {r.name for r in db.query(QboRefRow.name).filter(
            QboRefRow.kind == "account").all() if r.name}
    # No accounts synced yet: don't suppress every suggestion on a fresh setup.
    if not _ACCOUNT_NAMES:
        return name
    return name if name in _ACCOUNT_NAMES else ""


def reset_account_cache() -> None:
    """Called after a QuickBooks refresh, so new records are picked up."""
    global _ACCOUNT_NAMES, _VENDOR_NAMES
    _ACCOUNT_NAMES = None
    _VENDOR_NAMES = None


def _fmt_central(dt):
    from .export_xlsx import fmt_central
    return fmt_central(dt)


# A credit that's a payment onto the card, not a refund from a merchant.
# Statement wording is remarkably consistent about this.
# Money moved onto the card, or between our own accounts -- not a merchant
# returning a purchase. Wires and ACH belong here: they're funding movements,
# and calling them refunds puts them in the wrong bucket for coding.
_CARD_PAYMENT_RE = re.compile(
    r"\b(payment\s+(thank\s+you|received|-\s*thank)|autopay|auto\s*pay|"
    r"online\s+payment|electronic\s+payment|pmt\s+thank|"
    r"cardmember\s+payment|direct\s+debit|"
    r"wire(\s+transfer)?|ach(\s+(transfer|credit|debit|payment))?|"
    r"bank\s+transfer|funds\s+transfer|transfer\s+from|eft)\b", re.I)


def looks_like_card_payment(*parts: str) -> bool:
    return bool(_CARD_PAYMENT_RE.search(" ".join(p or "" for p in parts)))


_PAY_RULES = None
_SOURCE_NAMES = None


def reset_payment_rules() -> None:
    global _PAY_RULES, _SOURCE_NAMES
    _PAY_RULES = None
    _SOURCE_NAMES = None


def is_payment(row, db=None) -> bool:
    """Is this credit a payment onto the card rather than a merchant refund?

    Wording first -- "WIRE TRANSFER", "AUTOPAY" -- then the rules table, which
    exists for the programs that send no wording at all. A WEX payment arrives
    with a bare card number, so the only thing left to match on is which card
    account it came from.
    """
    global _PAY_RULES
    if not row.is_credit:
        return False        # money going out is never a payment onto the card
    if looks_like_card_payment(row.merchant, row.raw_description, row.memo):
        return True
    if db is None:
        return False
    if _PAY_RULES is None:
        from .models_db import PaymentRuleRow
        _PAY_RULES = db.query(PaymentRuleRow).filter(
            PaymentRuleRow.active.is_(True)).all()
    if not _PAY_RULES:
        return False
    from .canada import matches_rules
    # Card Account is matched against BOTH the internal key ("wex") and the
    # name on the Cards screen ("Wex (Credit)"). Someone writing a rule reads
    # the second and has no reason to know the first exists.
    global _SOURCE_NAMES
    if _SOURCE_NAMES is None:
        from .models_db import SourceAccountRow
        _SOURCE_NAMES = {r.source: (r.nickname or r.source)
                         for r in db.query(SourceAccountRow).all()}
    account = f"{row.source or ''} {_SOURCE_NAMES.get(row.source, '')}".strip()
    return matches_rules(
        row.coded_vendor or row.matched_bill_vendor or row.suggested_vendor or "",
        row.merchant or row.raw_description or "",
        _PAY_RULES, card_account=account)


def tier_for(top_score: float, has_candidates: bool) -> str:
    """The one place the score-to-tier thresholds live."""
    if not has_candidates:
        return "none"
    return ("strong" if top_score >= 0.60
            else "moderate" if top_score >= 0.45 else "weak")


def load_match_weights(db: Session) -> None:
    """Push any tuned weights into the scorer before it runs."""
    from .models_db import MatchWeightRow
    from engine.suggest import set_weights
    try:
        set_weights({r.key: r.weight for r in db.query(MatchWeightRow).all()})
    except Exception:                       # noqa: BLE001
        pass                                # defaults stand


def refresh_flags(db: Session, company: str) -> int:
    """Recompute the descriptive flags on every charge in a company.

    Cheap -- string tests, no bill scan -- so it can run over the whole table
    rather than only what is still in the queue.
    """
    from .canada import looks_canadian, reset_rules
    reset_rules()
    n = 0
    for row in db.scalars(select(ChargeRow).where(ChargeRow.company == company)):
        pay = is_payment(row, db)
        cad = looks_canadian(row.merchant, row.raw_description, row.memo, db=db,
            vendor=row.coded_vendor or row.matched_bill_vendor
                   or row.suggested_vendor or "")
        if bool(row.is_card_payment) != pay or bool(row.is_canadian) != cad:
            row.is_card_payment, row.is_canadian = pay, cad
            n += 1
    if n:
        db.commit()
    return n


def refresh_scores(db: Session, company: str) -> int:
    """Recompute stored match strength for every unresolved charge.

    Runs in the worker rather than on a page load: it is the full
    charges x bills scan, which is exactly the cost we keep off the request
    path. Roughly a second for a few hundred charges against a few thousand
    bills, and it only has to run when bills or charges actually change.
    """
    idx = load_hal_index(db)
    set_hal_index(idx)
    load_match_weights(db)

    bill_rows = list(db.scalars(select(BillRow).where(BillRow.company == company)))
    bills = [_to_bill(b) for b in bill_rows]
    by_id = {b.bill_id: b for b in bill_rows}

    # Flags first, over EVERY charge -- not just the unresolved ones.
    # is_card_payment and is_canadian describe what a charge IS, which doesn't
    # stop being true once it's coded. Computing them only in the For Review
    # loop meant an already-categorised wire transfer kept showing as a refund.
    refresh_flags(db, company)

    rows = list(db.scalars(select(ChargeRow).where(
        ChargeRow.company == company, ChargeRow.status == "for_review")))
    n = 0
    for row in rows:
        if row.is_credit or not bills:
            tier, score = "none", 0
        else:
            sg = [x for x in suggest(_to_charge(row, db), bills)
                  if _candidate_postable(row.amount, by_id.get(x.bill_id))]
            top = sg[0].score if sg else 0
            tier, score = tier_for(top, bool(sg)), int(round(top * 100))
        # The proposed coding, stored alongside the strength so both are
        # filterable. Computed from the same classification the queue shows.
        try:
            k = classify(_to_charge(row, db))
            sv = _known_vendor(db, k.coding.get("vendor") or "")[:200]
            sc = _known_account(db, k.coding.get("category") or "")[:200]
        except Exception:
            sv = sc = ""
        # Cleared on purpose, per field. Proposing it again would overwrite a
        # decision -- but only for the field that was actually cleared.
        if row.vendor_cleared:
            sv = ""
        if row.category_cleared:
            sc = ""
        pay = is_payment(row, db)
        from .canada import looks_canadian
        cad = looks_canadian(row.merchant, row.raw_description, row.memo, db=db,
            vendor=row.coded_vendor or row.matched_bill_vendor
                   or row.suggested_vendor or "")

        if (row.tier != tier or row.score != score
                or row.suggested_vendor != sv or row.suggested_category != sc
                or bool(row.is_card_payment) != pay
                or bool(row.is_canadian) != cad):
            row.tier, row.score = tier, score
            row.suggested_vendor, row.suggested_category = sv, sc
            row.is_card_payment = pay
            row.is_canadian = cad
            n += 1
    db.commit()
    return n


def build_queue(db: Session, company: str, status: str = "for_review",
                card: str | None = None, source: str | None = None,
                limit: int = 200, offset: int = 0,
                date_from=None, date_to=None,
                amt_min=None, amt_max=None,
                vendor: str | None = None, profile: str | None = None,
                last4: str | None = None,
                memo: str | None = None,
                details: str | None = None,
                txn_type: str | None = None,
                kinds: str | None = None,
                cvendor: str | None = None,
                category: str | None = None,
                user: str | None = None,
                currency: str | None = None,
                ts_from=None, ts_to=None,
                tier: str | None = None,
                matchable: str | None = None,
                q: str | None = None,
                sort: str = "date", direction: str = "desc") -> dict:
    """One unified queue, mirroring the QuickBooks bank feed.

    Every charge appears once and can be resolved either way -- matched to a
    bill, or coded as an expense/refund. The engine offers both: ranked bill
    candidates AND a suggested category. The reviewer picks; nothing forces a
    charge into one lane before a human has looked at it.
    """
    idx = load_hal_index(db)
    set_hal_index(idx)

    # keep the validator's season list in step with what was actually mirrored
    from .models_db import AppSettingRow
    from engine.hal import set_tracked_seasons
    _s = db.get(AppSettingRow, "hal_seasons")
    if _s and _s.value:
        set_tracked_seasons([x.strip() for x in _s.value.split(",") if x.strip()])

    _f = dict(status=status, card=card, source=source,
              date_from=date_from, date_to=date_to,
              amt_min=amt_min, amt_max=amt_max, vendor=vendor, profile=profile,
              last4=last4, memo=memo, details=details, cvendor=cvendor,
              category=category, user=user, currency=currency,
              ts_from=ts_from, ts_to=ts_to,
              txn_type=txn_type, kinds=kinds, tier=tier,
              matchable=matchable, q=q)
    qy = apply_charge_filters(
        select(ChargeRow).where(ChargeRow.company == company), _f)

    # nickname per program, so rows show the account name your team chose
    from .models_db import SourceAccountRow
    nick = {s_.source: (s_.nickname or s_.source) for s_ in db.query(SourceAccountRow)
            .filter(SourceAccountRow.company == company).all()}

    # Count and page in the DATABASE. This used to load every matching row into
    # Python, sort there and slice afterwards, so asking for 200 rows out of
    # 110,000 still materialised all 110,000 -- Categorized grew without bound
    # and took the page load with it.
    total = db.scalar(select(func.count()).select_from(qy.subquery())) or 0
    qy = qy.order_by(*_order_by(sort, direction, nick, db, company))
    if limit:
        qy = qy.offset(offset).limit(limit)
    elif offset:
        qy = qy.offset(offset)
    page = list(db.scalars(qy))

    # Candidate bills are only ever shown for an unresolved charge, so the
    # Categorized and Excluded tabs were paying for a scan whose result was
    # thrown away. Load the pool only when something on this page needs it.
    needs_suggestions = any(r.status == "for_review" and not r.is_credit
                            for r in page)
    if needs_suggestions:
        # Keep the ORM rows as well as the engine objects: the postability
        # check needs line_count and quantity, which live on the row.
        bill_rows = list(db.scalars(
            select(BillRow).where(BillRow.company == company)))
        bills = [_to_bill(b) for b in bill_rows]
    else:
        bill_rows, bills = [], []
    bill_by_id = {b.bill_id: b for b in bills}
    bill_row_by_id = {b.bill_id: b for b in bill_rows}

    from .learning import suggestion_for

    load_match_weights(db)  # tunable in the UI, so read per request
    reset_account_cache()   # accounts can change between requests
    reset_email_cache()
    from .canada import reset_rules
    reset_rules()           # both rule tables are editable in the UI
    reset_payment_rules()
    items, _drifted = [], []
    _flagged = 0
    for row in page:
        # Self-heal the descriptive flags for the rows actually on screen.
        # They were only recomputed during a bill sync, so a wire transfer kept
        # showing as a refund until the next one -- and on an already-resolved
        # charge, never. This is a regex over 200 rows; the cost is nothing and
        # the grid is right the moment you look at it.
        from .canada import looks_canadian as _lc
        _pay = is_payment(row, db)
        _cad = _lc(row.merchant, row.raw_description, row.memo, db=db,
            vendor=row.coded_vendor or row.matched_bill_vendor
                   or row.suggested_vendor or "")
        if bool(row.is_card_payment) != _pay or bool(row.is_canadian) != _cad:
            row.is_card_payment, row.is_canadian = _pay, _cad
            _flagged += 1

        c = _to_charge(row, db)
        k = classify(c)
        # A learned rule beats the engine's default: it reflects what this team
        # actually chose for this merchant, not a guess from a hardcoded list.
        learned = suggestion_for(db, company, c.merchant)
        # A refund is money returned; it can't pay down a bill, so don't
        # offer candidates that would only lead to a rejected post.
        sg = ([] if (row.is_credit or row.status != "for_review" or not bills)
              else suggest(c, bills))
        # Drop anything the write path would refuse. Offering a candidate that
        # cannot be posted wastes a click and teaches reviewers to distrust the
        # suggestions; a charge whose only candidate is unpostable is honestly
        # "no candidate" and belongs in coding instead.
        sg = [s_ for s_ in sg
              if _candidate_postable(c.amount, bill_row_by_id.get(s_.bill_id))
              ][:MAX_SUGGESTIONS]
        top = sg[0].score if sg else 0
        if row.status == "for_review" and not row.is_credit and bills:
            tier, score_pct = tier_for(top, bool(sg)), int(round(top * 100))
            # Self-healing: if the stored value drifted since the last refresh,
            # correct it now rather than letting the filter and the badge argue.
            if row.tier != tier or row.score != score_pct:
                row.tier, row.score = tier, score_pct
                _drifted.append(row)
        else:
            tier = row.tier or "none"

        items.append({
            "id": c.charge_id, "src": c.source,
            "account": nick.get(c.source, c.source),
            "amt": str(c.amount or 0), "date": str(c.txn_date or ""),
            "merchant": c.merchant, "memo": row.memo or "",
            # Amounts are stored absolute, so without this a refund is
            # indistinguishable from a charge in the grid.
            "is_credit": bool(row.is_credit),
            # A payment onto the card, not a refund from a merchant -- the
            # Amount column colours and badges them differently.
            "is_card_payment": bool(row.is_card_payment),
            "is_canadian": bool(row.is_canadian),
            "card": c.card_last4 or "", "holder": c.cardholder_name or "",
            "status": row.status, "resolution": row.resolution,
            "matched_bill_id": row.matched_bill_id,
            "coded_category": row.coded_category,
            "coded_vendor": row.coded_vendor,
            # Snapshot taken at match time; the bill itself may since have been
            # pruned, so these are the only record of what it was.
            "matched_bill_no": row.matched_bill_no or "",
            "matched_bill_vendor": row.matched_bill_vendor or "",
            "matched_bill_date": row.matched_bill_date or "",
            "matched_bill_memo": row.matched_bill_memo or "",
            "resolved_by": row.resolved_by,
            # Chicago wall time: the team is there, and a UTC stamp on a
            # spreadsheet gets misread as local every time.
            # UTC, ISO 8601 with a Z. The browser renders it in whatever zone
            # the person's machine is set to -- a fixed zone is wrong for
            # anyone outside it, and the server has no way to know.
            "resolved_at_utc": (row.resolved_at.isoformat() + "Z"
                                if row.resolved_at else ""),
            "qbo_txn_id": row.qbo_txn_id, "qbo_txn_type": row.qbo_txn_type,
            "post_error": row.post_error,
            "tier": tier,
            # Resolved rows aren't rescored on view, so fall back to what was
            # stored when they were still in the queue.
            "score": (round(top * 100) if sg else 0) if bills else (row.score or 0),
            # The engine composes category names from the season and team
            # ("Ticket Credits:CFL:Calgary Stampeders (TC)"), and not every
            # combination it can build exists in the chart of accounts. A
            # suggestion QuickBooks will reject is worse than none: it looks
            # decided, so it gets committed, and fails at post time.
            # Cleared on purpose means cleared. The engine still HAS an opinion
            # here -- it recomputes one on every request -- and returning it
            # anyway is what made Clear look like it had done nothing.
            # Stored first, computed second. The stored value is what a clear
            # writes to and what the filters search, so preferring the live
            # computation meant the grid could show something the database did
            # not agree with -- which is how a cleared vendor came back.
            "suggested_category": "" if row.category_cleared else (
                row.suggested_category or _known_account(
                    db, learned["category"] if learned
                    else k.coding.get("category") or "")),
            "suggested_vendor": "" if row.vendor_cleared else (
                row.suggested_vendor or _known_vendor(
                    db, learned["vendor"] if learned
                    else k.coding.get("vendor", ""))),
            "coding_rule": "learned" if learned else k.rule,
            "coding_why": (
                f"learned from {learned['confirmations']} previous coding(s)"
                + (" — applies automatically" if learned["auto_apply"] else "")
                if learned else "; ".join(k.reasons)),
            "learned": bool(learned),
            "learned_confirmations": learned["confirmations"] if learned else 0,
            "hal_status": k.coding.get("hal_status", ""),
            "season": k.coding.get("season", ""),
            "is_expense_like": k.charge_class != ChargeClass.BILL_PAYMENT,
            # ranked bill candidates, always offered
            "candidates": [{
                "bill": (bill_by_id[s.bill_id].doc_number
                         or bill_by_id[s.bill_id].bill_id),
                "bill_id": s.bill_id,
                "amt": str(bill_by_id[s.bill_id].amount),
                "date": str(bill_by_id[s.bill_id].txn_date),
                "vendor": bill_by_id[s.bill_id].vendor or "",
                "memo": (bill_by_id[s.bill_id].memo or "")[:90],
                "email": bill_by_id[s.bill_id].email or "",
                "score": round(s.score, 2),
                "why": "; ".join(s.reasons),
            } for s in sg],
        })

    if _flagged:
        db.commit()
    if _drifted:
        db.commit()      # stored strength corrected while rendering the page

    return {"company": company, "status": status,
            "hal_connected": idx is not None,
            "bills_loaded": len(bills), "total": total,
            "offset": offset, "limit": limit,
            "sort": sort, "direction": direction, "items": items}


def queue_counts(db: Session, company: str, card: str | None = None,
                 filters: dict | None = None) -> dict:
    """Counts per state, and per card, under the SAME filters as the grid.

    Two different questions, so two different uses of `card`:
      * the tab counts respect the card selection, because that's what the
        grid is showing;
      * the per-card counts deliberately ignore it, or an unticked card would
        report zero and you could never tell what ticking it would give you.
    """
    from sqlalchemy import func
    f = dict(filters or {})
    status = f.get("status") or "for_review"

    base = apply_charge_filters(
        db.query(ChargeRow.status, func.count(ChargeRow.charge_id)).filter(
            ChargeRow.company == company),
        {**f, "status": None, "card": card})
    counts = dict(base.group_by(ChargeRow.status).all())

    from .models_db import SourceAccountRow
    nick = {s.source: (s.nickname or s.source) for s in db.query(SourceAccountRow)
            .filter(SourceAccountRow.company == company).all()}

    per_card = dict(apply_charge_filters(
        db.query(ChargeRow.source, func.count(ChargeRow.charge_id)).filter(
            ChargeRow.company == company),
        {**f, "status": status, "card": None}).group_by(ChargeRow.source).all())

    # Every card account the company has, not just the ones with matching rows.
    # GROUP BY drops a source with no matches, so under a Refunds filter a card
    # with no refunds vanished from the picker -- the list got shorter than the
    # selection, "all selected" stopped being true, and the button flipped from
    # "All Cards" to "3 card accounts" while nothing had been deselected.
    all_sources = [r for (r,) in db.query(ChargeRow.source).filter(
        ChargeRow.company == company).distinct().all() if r]
    for src in nick:
        if src not in all_sources:
            all_sources.append(src)

    from .models_db import SourceAccountRow as _SA
    virtual = {r.source for r in db.query(_SA).filter(
        _SA.company == company, _SA.is_virtual.is_(True)).all()}
    per_source = [{"source": src, "name": nick.get(src, src),
                   "virtual": src in virtual,
                   "for_review": per_card.get(src, 0)}
                  for src in all_sources]
    per_source.sort(key=lambda s: -s["for_review"])

    from .models_db import BillRow as _B
    # Open bills only -- the same set the Bills Available tab lists and the
    # matcher scores against.
    bills_n = db.query(_B).filter(_B.company == company, _B.balance > 0).count()

    return {
        "bills": bills_n,
        "for_review": counts.get("for_review", 0),
        "categorized": counts.get("categorized", 0),
        "excluded": counts.get("excluded", 0),
        "sources": per_source,
    }


def build_review(db: Session, company: str, limit: int = 200,
                 tier: str | None = None, source: str | None = None) -> dict:
    """Legacy two-bucket view, kept for the older endpoint."""
    idx = load_hal_index(db)
    set_hal_index(idx)          # None is fine: classifier falls back gracefully

    charges = [_to_charge(r, db) for r in db.scalars(
        select(ChargeRow).where(ChargeRow.company == company))]
    bills = [_to_bill(r) for r in db.scalars(
        select(BillRow).where(BillRow.company == company))]
    bill_by_id = {b.bill_id: b for b in bills}

    from .learning import suggestion_for
    payments, expenses = [], []
    tiers = Counter()

    for c in charges:
        if source and c.source != source:
            continue
        k = classify(c)

        if k.charge_class == ChargeClass.BILL_PAYMENT:
            sg = suggest(c, bills)[:MAX_SUGGESTIONS]
            top = sg[0].score if sg else 0
            t = ("strong" if top >= 0.60 else "moderate" if top >= 0.45
                 else "weak" if sg else "none")
            tiers[t] += 1
            if tier and t != tier:
                continue
            payments.append({
                "id": c.charge_id, "src": c.source, "amt": str(c.amount),
                "date": str(c.txn_date), "merchant": c.merchant,
                "card": c.card_last4 or "", "holder": c.cardholder_name or "",
                "tier": t,
                "candidates": [{
                    "bill": (bill_by_id[s.bill_id].doc_number
                         or bill_by_id[s.bill_id].bill_id),
                "bill_id": s.bill_id,
                    "amt": str(bill_by_id[s.bill_id].amount),
                    "date": str(bill_by_id[s.bill_id].txn_date),
                    "vendor": bill_by_id[s.bill_id].vendor or "",
                    "memo": (bill_by_id[s.bill_id].memo or "")[:90],
                    "score": round(s.score, 2),
                    "why": "; ".join(s.reasons),
                } for s in sg],
            })
        else:
            expenses.append({
                "id": c.charge_id, "src": c.source, "amt": str(c.amount),
                "date": str(c.txn_date), "merchant": c.merchant,
                "card": c.card_last4 or "", "holder": c.cardholder_name or "",
                "category": k.coding.get("category") or "—",
                "vendor": k.coding.get("vendor", ""),
                "season": k.coding.get("season", ""),
                "hal": k.coding.get("hal_status", ""),
                "rule": k.rule,
                "why": "; ".join(k.reasons),
            })

    order = {"strong": 0, "moderate": 1, "weak": 2, "none": 3}
    payments.sort(key=lambda p: (order[p["tier"]], -float(p["amt"])))
    expenses.sort(key=lambda e: -float(e["amt"]))

    return {
        "company": company,
        "hal_connected": idx is not None,
        "stats": {
            "charges": len(charges), "bills": len(bills),
            "bill_payments": sum(tiers.values()), "expenses": len(expenses),
            "strong": tiers["strong"], "moderate": tiers["moderate"],
            "weak": tiers["weak"], "none": tiers["none"],
        },
        "bill_payments": payments[:limit],
        "expenses": expenses[:limit],
    }


def list_companies(db: Session) -> list[str]:
    rows = db.execute(select(ChargeRow.company).distinct()).scalars().all()
    return sorted(r for r in rows if r)
