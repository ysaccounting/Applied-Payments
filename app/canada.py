"""Is this charge Canadian?

No export carries a currency or country field, so the only evidence is the bank
detail string. Three kinds of evidence, in descending order of certainty:

  1. an explicit marker  -- "CAD", "CANADA", a .ca domain
  2. a Canadian team or venue -- "TORONTO BLUE JAYS", "SCOTIABANK ARENA"
  3. a Canadian city NEXT TO its province code -- "TORONTO ON", "MONTREAL QC"

The third rule is why this is a module and not a regex. Several Canadian city
names are also US city names, and the US ones are more common in this data:

    ONTARIO CA      -> Ontario, California
    VANCOUVER WA    -> Vancouver, Washington
    LONDON KY       -> London, Kentucky
    HAMILTON OH     -> Hamilton, Ohio
    WINDSOR CT      -> Windsor, Connecticut

So a city alone never counts. It must be followed by the matching province, and
"CA" is deliberately NOT treated as a province code -- it is the US state
abbreviation for California far more often than it is short for Canada.
"""
from __future__ import annotations

import re

# Explicit and unambiguous.
_EXPLICIT = re.compile(
    r"(\bCAD\b|\bCANADA\b|\bCANADIAN\b|\.ca\b|\bTICKETMASTER\.CA\b)", re.I)

# Teams and venues that only exist in Canada. Kept as whole phrases: "Jets"
# alone is a New York football team, "Blue Jays" is unambiguous.
_CANADIAN_ENTITIES = [
    # NHL
    "toronto maple leafs", "maple leafs", "montreal canadiens", "canadiens",
    "ottawa senators", "winnipeg jets", "calgary flames", "edmonton oilers",
    "vancouver canucks",
    # MLB / NBA / MLS
    "toronto blue jays", "blue jays", "toronto raptors", "raptors",
    "toronto fc", "cf montreal", "cf montréal", "vancouver whitecaps",
    # CFL
    "toronto argonauts", "argonauts", "montreal alouettes", "alouettes",
    "hamilton tiger-cats", "tiger-cats", "ottawa redblacks", "redblacks",
    "winnipeg blue bombers", "blue bombers", "saskatchewan roughriders",
    "roughriders", "calgary stampeders", "stampeders", "edmonton elks",
    "bc lions",
    # venues
    "scotiabank arena", "rogers centre", "bell centre", "canadian tire centre",
    "rogers place", "scotiabank saddledome", "saddledome", "rogers arena",
    "canada life centre", "bmo field", "commonwealth stadium", "td place",
    "mosaic stadium", "princess auto stadium", "bc place", "tim hortons field",
    "place bell", "videotron centre", "centre videotron",
]

# City -> the province codes it may legitimately be paired with.
_CITY_PROVINCE = {
    "toronto": {"ON"}, "ottawa": {"ON"}, "hamilton": {"ON"},
    "mississauga": {"ON"}, "london": {"ON"}, "windsor": {"ON"},
    "kitchener": {"ON"}, "kingston": {"ON"}, "markham": {"ON"},
    "montreal": {"QC"}, "quebec": {"QC"}, "laval": {"QC"}, "gatineau": {"QC"},
    "vancouver": {"BC"}, "victoria": {"BC"}, "burnaby": {"BC"},
    "surrey": {"BC"}, "richmond": {"BC"},
    "calgary": {"AB"}, "edmonton": {"AB"},
    "winnipeg": {"MB"}, "regina": {"SK"}, "saskatoon": {"SK"},
    "halifax": {"NS"}, "moncton": {"NB"}, "st johns": {"NL"},
    "charlottetown": {"PE"},
}

# Every province EXCEPT CA -- see the module docstring.
_PROVINCES = r"ON|QC|BC|AB|MB|SK|NS|NB|NL|PE|YT|NT|NU"


def _norm(text: str) -> str:
    # Strip punctuation to spaces so "TORONTO, ON" and "TORONTO ON" read alike.
    return re.sub(r"[^A-Za-z0-9.]+", " ", text or "").strip()


# Rules loaded from the database, cached per process. The lists above are the
# fallback for a database that has none yet.
_RULES: tuple[list[str], dict[str, set[str]]] | None = None


def reset_rules() -> None:
    global _RULES
    _RULES = None


def load_rules(db=None):
    """(rows, cities) -- Item/Rule/Input rows plus the seeded city rules."""
    global _RULES
    if _RULES is not None:
        return _RULES
    rows, cities = [], dict(_CITY_PROVINCE)
    if db is not None:
        try:
            from .models_db import CanadaRuleRow
            all_rows = db.query(CanadaRuleRow).filter(
                CanadaRuleRow.active.is_(True)).all()
            if all_rows:
                rows = [r for r in all_rows if r.kind != "city_province"]
                cities = {r.phrase.strip().lower():
                          {p.strip().upper() for p in (r.provinces or "").split(",") if p.strip()}
                          for r in all_rows if r.kind == "city_province" and r.phrase}
        except Exception:
            pass
    _RULES = (rows, cities)
    return _RULES


def matches_rules(vendor: str, bank_detail: str, rules,
                  card_account: str = "") -> bool:
    """Does any Item/Rule/Input row match this charge?

    Item says WHICH field to look at, Rule says how to compare, Input is the
    text. Keeping the field explicit matters: 'Ducks' in a vendor name means
    Anaheim, while 'Ducks' in a bank detail could be anything.

    Shared by the Canada rules and the payment rules -- same shape, same
    comparison, different table.
    """
    fields = {"vendor": (vendor or "").strip().lower(),
              "bank_detail": (bank_detail or "").strip().lower(),
              "card_account": (card_account or "").strip().lower()}
    for r in rules:
        target = fields.get(r.item or "bank_detail", "")
        needle = (r.phrase or "").strip().lower()
        if not needle or not target:
            continue
        if r.rule == "equals":
            # Card Account carries two names at once ("wex Wex (Credit)"), so
            # Equals means "is one of them" rather than "is the whole string".
            if target == needle or needle in target.split():
                return True
            if (r.item or "") == "card_account" and needle in target:
                return True
        elif needle in target:
            return True
    return False


def looks_canadian(*parts: str, db=None, vendor: str = "") -> bool:
    """True when the charge's vendor or bank detail says it was made in Canada."""
    _vendor = vendor
    raw = " ".join(p or "" for p in parts)
    if not raw.strip():
        return False

    if _EXPLICIT.search(raw):
        return True

    rows, cities = load_rules(db)
    low = _norm(raw).lower()
    # Item/Rule/Input rows. `parts` arrives as (merchant, raw_description, memo)
    # -- the merchant IS the bank detail, and the vendor is passed separately
    # by callers that have one.
    if rows and matches_rules(_vendor or "", raw, rows):
        return True
    if not rows:
        for phrase in _CANADIAN_ENTITIES:
            if phrase in low:
                return True

    # City immediately followed by its own province code.
    for city, provinces in cities.items():
        for m in re.finditer(rf"\b{re.escape(city)}\b\s+({_PROVINCES})\b",
                             low, re.I):
            if m.group(1).upper() in provinces:
                return True
    return False
