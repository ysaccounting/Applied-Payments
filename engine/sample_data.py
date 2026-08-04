"""
Synthetic sample data that mirrors what you described, so the engine runs
end-to-end today. Every case here is one we talked through:

  1. clean match       — order# + last4 + exact amount        -> AUTO
  2. TicketVault round  — $100 charge vs $99.99 bill, qty 3    -> AUTO + overwrite (proven)
  3. rounding, qty 7    — $250 charge vs $249.97 bill          -> AUTO + overwrite (proven)
  4. no order#          — last4 + amount + date only           -> REVIEW
  5. ambiguous          — two open bills both plausible        -> EXCEPTION
  6. missing buy-in     — charge with no bill at all           -> MISSING_BUYIN
  7. real discrepancy   — $1.00 off, NOT rounding              -> REVIEW, no overwrite
  8. paid bill ignored  — matching bill exists but balance = 0 -> not offered

Bills carry the fields that in reality are parsed out of the QBO memo
(name, email, order#, last4) plus quantity from TicketVault.
"""

from datetime import date

from .models import Bill
from .normalize import normalize

COMPANY = "Front Row Brokers LLC"

# --- raw card exports (as each portal would hand them to us) ------------------
RAW_CHARGES = [
    ("slash", {
        "id": "SLASH-1001", "amount": "1204.00", "date": "2026-07-21",
        "card": "**** 0087", "cardholder": "Aaron Cohen",
        "email": "aaron@frontrow.co",
        "description": "TICKETVAULT ORDER: 55120 balcony x4",
    }),
    ("slash", {
        "id": "SLASH-1002", "amount": "100.00", "date": "2026-07-21",
        "card": "4410", "cardholder": "Miriam Ford",
        "email": "miriam@frontrow.co",
        "description": "ORDER 55121 GA lawn x3",
    }),
    ("wex", {
        "txn_id": "WEX-1003", "total": "250.00", "post_date": "07/21/2026",
        "card_number": "9977 88", "employee": "David Reyes",
        "employee_email": "david@frontrow.co",
        "memo": "PO#55122 upper x7",
    }),
    ("divvy", {
        "transaction_id": "DIVVY-1004", "amount": "540.00",
        "cleared_date": "2026-07-20", "last_four": "0091", "user": "S. Klein",
        "user_email": "", "note": "ticket purchase, no order ref",
    }),
    ("slash", {
        "id": "SLASH-1005", "amount": "860.50", "date": "2026-07-21",
        "card": "4471", "cardholder": "Dana Levy", "email": "",
        "description": "ticket buy",     # no order# -> two bills look plausible
    }),
    ("wex", {
        "txn_id": "WEX-1006", "total": "960.00", "post_date": "07/21/2026",
        "card_number": "553388", "employee": "Ops Card",
        "employee_email": "", "memo": "purchase, no bill loaded yet",
    }),
    ("divvy", {
        "transaction_id": "DIVVY-1007", "amount": "300.00",
        "cleared_date": "2026-07-21", "last_four": "0087", "user": "Aaron Cohen",
        "user_email": "aaron@frontrow.co", "note": "ORDER 55130 x2",
    }),
]

CHARGES = [normalize(src, row, COMPANY) for src, row in RAW_CHARGES]

# --- open bills read from QuickBooks (memo already parsed into fields) --------
BILLS = [
    # 1. clean exact match for SLASH-1001
    Bill("QBO-8801", COMPANY, "1204.00", date(2026, 7, 21), balance="1204.00",
         quantity=4, name="Cohen, Aaron", email="aaron@frontrow.co",
         order_number="55120", card_last4="0087", memo="ORDER 55120"),

    # 2. TicketVault rounding: true $100, bill 33.33*3 = 99.99, qty 3
    Bill("QBO-8802", COMPANY, "99.99", date(2026, 7, 21), balance="99.99",
         quantity=3, name="Ford, Miriam", email="miriam@frontrow.co",
         order_number="55121", card_last4="4410", memo="ORDER 55121"),

    # 3. TicketVault rounding: true $250, 35.71*7 = 249.97, qty 7
    Bill("QBO-8803", COMPANY, "249.97", date(2026, 7, 21), balance="249.97",
         quantity=7, name="Reyes, David", email="david@frontrow.co",
         order_number="55122", card_last4="7788", memo="PO 55122"),

    # 4. no order# on the charge; matches on last4 + amount + date
    Bill("QBO-8804", COMPANY, "540.00", date(2026, 7, 20), balance="540.00",
         quantity=6, name="Klein, S", order_number=None, card_last4="0091",
         memo="lawn seats"),

    # 5a & 5b. two plausible bills for SLASH-1005 (same last4, same amount)
    Bill("QBO-8805", COMPANY, "860.50", date(2026, 7, 21), balance="860.50",
         quantity=5, name="Levy, Dana", card_last4="4471", memo="ticket buy A"),
    Bill("QBO-8806", COMPANY, "860.50", date(2026, 7, 20), balance="860.50",
         quantity=5, name="Levy, Daniel", card_last4="4471", memo="ticket buy B"),

    # 7. real discrepancy: bill 959.00 vs charge... (we point DIVVY nowhere; use
    #    a bill $1 off from a charge to show it is NOT auto-overwritten)
    Bill("QBO-8807", COMPANY, "299.00", date(2026, 7, 21), balance="299.00",
         quantity=2, name="Cohen, Aaron", email="aaron@frontrow.co",
         order_number="55130", card_last4="0087", memo="ORDER 55130"),

    # 8. a matching bill for SLASH-1001's twin, but already PAID (balance 0) ->
    #    must be ignored as a candidate
    Bill("QBO-8800", COMPANY, "1204.00", date(2026, 7, 21), balance="0.00",
         quantity=4, name="Cohen, Aaron", order_number="55120",
         card_last4="0087", memo="already paid duplicate"),
]
