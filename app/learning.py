"""
Learned coding rules.

Every time a reviewer codes a charge, that decision is recorded against the
merchant. Once the same coding has been confirmed enough times consistently,
the rule starts applying itself.

The middle-ground policy, chosen deliberately:

    learn      always — every coding decision is recorded
    suggest    always — the queue pre-fills from what your team actually chose,
                        rather than from the engine's hardcoded guess
    auto-apply above a confirmation threshold, and only for rules that have
               been coded consistently
    post       never automatically — an auto-coded charge moves to Categorized,
               where a person still sees it before anything reaches QuickBooks

So automation removes clicks, not oversight. The last human checkpoint before
the ledger stays where it is.

Two guards worth knowing about:

  - **Disagreement resets trust.** If someone codes a merchant differently from
    the learned rule, the counter drops and auto-apply switches off. A rule that
    people keep overriding stops asserting itself.
  - **Rules are visible and reversible.** Every rule can be inspected, edited,
    demoted or deleted, with the count of confirmations behind it.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime

from sqlalchemy.orm import Session

from .models_db import ChargeRow, LearnedRuleRow

log = logging.getLogger(__name__)

# Confirmations needed before a rule may auto-apply. Deliberately not 2 or 3:
# a handful of coincidental repeats shouldn't be enough to stop asking.
AUTO_THRESHOLD = 8


def normalize_merchant(merchant: str) -> str:
    """Collapse a merchant string to a stable key.

    Card descriptors carry order numbers, store numbers and varying spacing --
    "FIFAUS - 61667627" and "FIFAUS - 61701234" are the same merchant. Digits
    and punctuation are stripped so they land on one rule instead of hundreds.
    """
    s = str(merchant or "").upper()
    s = re.sub(r"\b\d[\d\-]*\b", " ", s)          # order/store numbers
    s = re.sub(r"[^A-Z ]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:80]


def rule_key(company: str, merchant: str) -> str:
    return f"{company}|{normalize_merchant(merchant)}"


def record_decision(db: Session, charge: ChargeRow, category: str,
                    vendor: str, actor: str) -> LearnedRuleRow | None:
    """Learn from one coding decision."""
    key = normalize_merchant(charge.merchant)
    if not key or not category:
        return None

    rid = rule_key(charge.company, charge.merchant)
    rule = db.get(LearnedRuleRow, rid)
    if rule is None:
        # db.get() only looks at the database and rows already flushed, so
        # coding several charges from the SAME merchant in one batch created the
        # rule once per charge and the commit failed on the primary key --
        # taking the whole batch with it. Flushing makes the first insert
        # visible to the next lookup.
        db.flush()
        rule = db.get(LearnedRuleRow, rid)

    if rule is None:
        rule = LearnedRuleRow(
            id=rid, company=charge.company, merchant_key=key,
            sample_merchant=charge.merchant or "", category=category,
            vendor=vendor or "", confirmations=1, disagreements=0,
            auto_apply=False, last_actor=actor, updated_at=datetime.utcnow())
        db.add(rule)
        return rule

    same = (rule.category == category and (rule.vendor or "") == (vendor or ""))
    if same:
        rule.confirmations += 1
        # Consistent enough, and not being argued with -> allow auto-apply.
        if rule.confirmations >= AUTO_THRESHOLD and rule.disagreements == 0:
            if not rule.auto_apply:
                log.info("rule %s promoted to auto-apply after %d confirmations",
                         rid, rule.confirmations)
            rule.auto_apply = True
    else:
        # Someone coded this merchant differently. Trust the newer decision,
        # but stop auto-applying until it settles again.
        rule.disagreements += 1
        rule.category = category
        rule.vendor = vendor or ""
        rule.confirmations = 1
        rule.auto_apply = False
        log.info("rule %s changed by %s — auto-apply disabled", rid, actor)

    rule.last_actor = actor
    rule.updated_at = datetime.utcnow()
    return rule


def lookup(db: Session, company: str, merchant: str) -> LearnedRuleRow | None:
    key = normalize_merchant(merchant)
    if not key:
        return None
    return db.get(LearnedRuleRow, rule_key(company, merchant))


def suggestion_for(db: Session, company: str, merchant: str) -> dict | None:
    """What the learned rules say about this merchant, if anything."""
    rule = lookup(db, company, merchant)
    if rule is None:
        return None
    return {
        "category": rule.category,
        "vendor": rule.vendor,
        "confirmations": rule.confirmations,
        "auto_apply": rule.auto_apply,
        "source": "learned",
    }


def apply_auto_rules(db: Session, company: str, limit: int = 2000) -> dict:
    """Code charges whose merchant has a trusted rule.

    Auto-coded charges move to Categorized -- NOT posted. A person still sees
    them before anything reaches QuickBooks.
    """
    from .models_db import AuditRow

    # A rule missing a category or vendor can't be applied: coding requires
    # both, and an
    # auto-applied charge would otherwise reach Categorized in a state a person
    # is not allowed to create by hand. Older rules learned before the vendor
    # requirement can be in this state, so they're skipped rather than trusted.
    rules = {r.merchant_key: r for r in db.query(LearnedRuleRow).filter(
        LearnedRuleRow.company == company,
        LearnedRuleRow.auto_apply.is_(True)).all()
        if (r.vendor or "").strip() and (r.category or "").strip()}

    # A rule pointing at an account QuickBooks no longer has would post-fail
    # every time it fired, so it's skipped until someone recodes that merchant.
    from .models_db import QboRefRow
    known = {r.name for r in db.query(QboRefRow.name).filter(
        QboRefRow.kind == "account").all() if r.name}
    if known:
        rules = {k: r for k, r in rules.items() if r.category in known}
    if not rules:
        return {"applied": 0, "rules_active": 0}

    charges = db.query(ChargeRow).filter(
        ChargeRow.company == company,
        ChargeRow.status == "for_review").limit(limit).all()

    applied = 0
    for c in charges:
        rule = rules.get(normalize_merchant(c.merchant))
        if rule is None:
            continue
        c.status = "categorized"
        c.resolution = "coded"
        c.coded_category = rule.category
        c.coded_vendor = rule.vendor
        c.resolved_by = f"auto:{rule.id}"
        c.resolved_at = datetime.utcnow()
        db.add(AuditRow(
            company=company, action="auto_coded", charge_id=c.charge_id,
            detail=f"learned rule '{rule.merchant_key}' -> {rule.category}"
                   f"/{rule.vendor} ({rule.confirmations} confirmations)",
            dry_run=True, actor="system"))
        applied += 1
        if applied % 200 == 0:
            db.commit()
    db.commit()
    log.info("auto-coded %d charges for %s", applied, company)
    return {"applied": applied, "rules_active": len(rules)}
