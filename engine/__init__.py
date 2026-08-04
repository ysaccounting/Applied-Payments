"""YS reconciliation matching engine — prototype core."""
from .models import Charge, Bill, MatchResult, Decision, money
from .matcher import reconcile, match_charge
from .normalize import normalize, extract_order_number
from .rounding import explain_gap
from .suggest import suggest, Suggestion
from .classify import classify, ChargeClass, set_hal_index
from .hal import HalRecord, HalIndex, validate, TC, DEP

__all__ = [
    "Charge", "Bill", "MatchResult", "Decision", "money",
    "reconcile", "match_charge", "normalize", "extract_order_number", "explain_gap",
    "suggest", "Suggestion", "classify", "ChargeClass", "set_hal_index",
    "HalRecord", "HalIndex", "validate", "TC", "DEP",
]
