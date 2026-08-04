"""
Orchestration.

Ties the pieces together for one company's daily run:

    candidates -> score -> pick best -> resolve conflicts -> tier by confidence
    -> decide bill overwrite -> flag missing buy-ins.

Thresholds here are the business dial the proposal flags as an open question.
They are set high on purpose: better to send a borderline match to review than
to auto-post a wrong one. Tune against real match rates in discovery.
"""

from __future__ import annotations

from typing import List

from .models import Bill, Charge, Decision, MatchResult
from .scoring import candidates, score


# Confidence tiers. AUTO must clear a high bar because it posts without a human.
AUTO_THRESHOLD = 0.85
REVIEW_THRESHOLD = 0.55
# Below REVIEW_THRESHOLD with a candidate -> exception; with no candidate at
# all -> missing buy-in (paid but never recorded).


def _decide_overwrite(result: MatchResult) -> None:
    """Apply the bill-overwrite rule for the TicketVault rounding case.

    Auto-overwrite ONLY when:
      - single-line bill (99.99% of them), and
      - the gap is an explained rounding artifact, and
      - we're confident enough to be auto/review (not exception).
    Anything else: leave the bill alone, let a human look.
    """
    r = result.rounding
    if result.bill is None or not r.get("explained"):
        return
    if result.bill.line_count != 1:
        result.reasons.append("multi-line bill — overwrite withheld, sent to review")
        return
    if r.get("correct_to") is not None and r["gap"] != 0:
        result.overwrite_bill_amount = r["correct_to"]
        result.rounding_explained = True
        strength = "proven" if r.get("exact") else "plausible"
        result.reasons.append(
            f"overwrite bill {result.bill.amount} -> {r['correct_to']} "
            f"({strength} TicketVault rounding)"
        )


def match_charge(charge: Charge, bills: List[Bill]) -> MatchResult:
    cands = candidates(charge, bills)

    if not cands:
        # nothing to match against at all -> paid but not recorded
        return MatchResult(
            charge=charge, bill=None, score=0.0,
            decision=Decision.MISSING_BUYIN,
            reasons=["no open bill matches this charge — possible missing buy-in"],
        )

    scored = sorted(
        ((score(charge, b), b) for b in cands),
        key=lambda t: t[0]["score"], reverse=True,
    )
    best, best_bill = scored[0]

    # ambiguity guard: if the top two are close, don't trust the winner
    ambiguous = len(scored) > 1 and (best["score"] - scored[1][0]["score"]) < 0.10

    if best["score"] >= AUTO_THRESHOLD and not ambiguous:
        decision = Decision.AUTO_MATCH
    elif best["score"] >= REVIEW_THRESHOLD:
        decision = Decision.REVIEW
    else:
        decision = Decision.EXCEPTION

    result = MatchResult(
        charge=charge, bill=best_bill, score=best["score"], decision=decision,
        signals=best["signals"], reasons=list(best["reasons"]),
    )
    result.rounding = best["rounding"]           # attach for overwrite decision
    if ambiguous:
        result.reasons.append(
            f"ambiguous: {len(scored)} candidates within 0.10 — routed to exception"
        )
        result.decision = Decision.EXCEPTION

    _decide_overwrite(result)
    return result


def reconcile(charges: List[Charge], bills: List[Bill]) -> List[MatchResult]:
    """Run a full company batch and resolve bill contention.

    A single bill must not be auto-claimed by two charges. If it is, keep the
    stronger and demote the other to review.
    """
    results = [match_charge(c, bills) for c in charges]

    claimed = {}
    for r in results:
        if r.decision == Decision.AUTO_MATCH and r.bill is not None:
            prior = claimed.get(r.bill.bill_id)
            if prior is None or r.score > prior.score:
                if prior is not None:
                    prior.decision = Decision.REVIEW
                    prior.reasons.append("bill also claimed by a stronger charge — review")
                claimed[r.bill.bill_id] = r
            else:
                r.decision = Decision.REVIEW
                r.reasons.append("bill already claimed by a stronger charge — review")
    return results
