"""
Charge ingestion — two paths into the same place.

    upload   a person drops the CSV the portal gives them today
    pull     the worker fetches on a schedule, where the source has an API

Both normalize through the same adapters and upsert on a stable key, so
re-uploading a file or re-running a pull is safe. That idempotency is the
important part: overlapping date ranges are normal (a Monday pull and a
Tuesday pull both cover the weekend), and double-counting a charge would
corrupt reconciliation silently.

Every row in an uploaded file is imported. Nothing is skipped for having been
seen before -- if a file is uploaded twice, its charges appear twice.

That is a deliberate choice: a skipped upload that reports "0 new" is confusing
and hard to recover from, whereas a duplicate is visible in the queue and can be
excluded. The tradeoff is that duplicates CAN be posted to QuickBooks if nobody
notices them, so uploading the same file twice is worth avoiding.

Charge ids stay unique per import so the two copies are separate records:
`{company}:{source}:{txn_id}` plus a suffix when that id already exists.

Portal transaction IDs:
    Slash  -> "Id"              (agg_tx_2wku97sary82g)
    Divvy  -> "Transaction ID"  (base64 blob)
    WEX    -> no ID in the export; a content hash is used instead.
"""

from __future__ import annotations

import csv
import hashlib
import io
import logging
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy.orm import Session

from .models_db import ChargeRow, UploadRow

log = logging.getLogger(__name__)

SOURCES = ("wex", "divvy", "slash")

# Sources with an API the worker can pull from. WEX has no usable one for this
# export, so it stays upload-only.
PULLABLE = {"slash", "divvy"}


def _money(v) -> Decimal | None:
    if v is None:
        return None
    s = re.sub(r"[$,\s]", "", str(v))
    if not s or s in {"-", "nan"}:
        return None
    try:
        return abs(Decimal(s)).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None


def _date(v) -> date | None:
    s = str(v or "").strip()
    if not s:
        return None
    s = s.split("T")[0].split(" ")[0]
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d-%b-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _last4(v) -> str:
    digits = re.sub(r"\D", "", str(v or ""))
    return digits[-4:] if len(digits) >= 4 else ""


def _row_hash(row: dict, occurrence: int = 0) -> str:
    """Content hash for sources with no transaction ID (WEX).

    `occurrence` disambiguates genuinely identical rows -- WEX exports really do
    contain repeated (card, amount, date, merchant) tuples, which are separate
    real charges. Counting occurrences within the file keeps them distinct while
    staying idempotent: the same file re-uploaded produces the same sequence,
    so the same IDs, so nothing double-counts.
    """
    blob = "|".join(f"{k}={row.get(k, '')}" for k in sorted(row))
    if occurrence:
        blob += f"|#{occurrence}"
    return hashlib.sha256(blob.encode()).hexdigest()[:20]


# --- per-source row mappers --------------------------------------------------

def _map_slash(row: dict) -> dict | None:
    if row.get("Type") not in ("card_settlement", "card_refund"):
        return None
    amt = _money(row.get("Amount"))
    d = _date(row.get("Date (UTC)"))
    if amt is None or d is None:
        return None
    raw = row.get("Amount") or "0"
    return {
        "source_txn_id": row.get("Id") or _row_hash(row),
        "amount": amt, "txn_date": d,
        "card_last4": _last4(row.get("Last 4")),
        "cardholder_name": (row.get("Card Name") or "").strip(),
        "merchant": (row.get("Description") or "").strip(),
        "order_number": (row.get("Order Id") or "").strip() or None,
        "raw_description": row.get("Type", ""),
        "is_credit": not str(raw).strip().startswith("-"),
    }


def _map_divvy(row: dict) -> dict | None:
    if (row.get("Status") or "").lower() != "complete":
        return None
    amt = _money(row.get("Amount"))
    d = _date(row.get("Date (UTC)"))
    if amt is None or d is None:
        return None
    raw = str(row.get("Amount") or "")
    return {
        "source_txn_id": row.get("Transaction ID") or _row_hash(row),
        "amount": amt, "txn_date": d,
        "card_last4": _last4(row.get("Card Last 4")),
        "cardholder_name": (row.get("Card Name") or "").strip(),
        "merchant": (row.get("Clean Merchant Name") or "").strip(),
        "email": (row.get("Card Holder Email") or "").strip().lower() or None,
        "raw_description": row.get("Merchant", ""),
        "is_credit": "-" not in raw,
    }


def _map_wex(row: dict, occurrence: int = 0) -> dict | None:
    amt = _money(row.get("Transaction.Transaction Amount"))
    d = _date(row.get("Transaction.Transaction Dt"))
    if amt is None or d is None:
        return None
    return {
        # WEX's export carries no transaction ID, so identity is a content hash
        # of the row. Re-uploading the same file is still idempotent; an edited
        # row would look like a new charge.
        "source_txn_id": _row_hash(row, occurrence),
        "amount": amt, "txn_date": d,
        "card_last4": _last4(row.get("Card Number.Card No")),
        "cardholder_name": (row.get("Purchase Card Log.Name") or "").strip(),
        "merchant": (row.get("Transaction.Merchant Name") or "").strip(),
        "raw_description": row.get("Purchase Card Log.Event", "") or "",
        "is_credit": str(row.get("Transaction.Transaction Amount", "")).strip().startswith("-"),
    }


# --- pre-formatted export --------------------------------------------------
# A separate in-house tool normalizes all three card portals into one shape:
#
#     Date,Description,Amount
#     7/27/2026,Blue Jays Baseball - Ian McCormick - 7766,-1424.22
#
# The description packs merchant, cardholder and card last-4 into one field,
# which is everything the matcher needs. Rows that aren't purchases carry no
# cardholder or last-4 ("Slash fee: Foreign transaction fee for 07.26.26") and
# are kept as-is rather than force-parsed.

_SIMPLE_COLS = {"Date", "Description", "Amount"}
# Optional column names the in-house formatter may add.
_MEMO_COLS = ("Memo", "memo", "Note", "Notes")


def _pick_memo(row: dict) -> str:
    for k in _MEMO_COLS:
        if row.get(k):
            return str(row[k]).strip()
    return ""
_TRAILING_LAST4 = re.compile(r"^\d{4}$")


def compose_memo(merchant: str, cardholder: str, last4: str,
                 tail: str = "") -> str:
    """Build "Merchant - Cardholder - 1234", optionally keeping a tail.

    `tail` is whatever someone typed AFTER the card number. Bank detail and
    profile each own their own segment of the memo and nothing more, so
    correcting either rewrites its part and leaves an added note alone.
    """
    parts = [str(merchant or "").strip(), str(cardholder or "").strip(),
             str(last4 or "").strip()]
    head = " - ".join(p for p in parts if p)
    tail = str(tail or "").strip()
    return f"{head} - {tail}" if tail else head


def memo_tail(memo: str, last4: str) -> tuple[bool, str]:
    """Split a memo into its standard head and whatever follows the card number.

    Returns (recognised, tail). `recognised` is False when the memo has been
    replaced wholesale and no longer contains the card number -- in that case
    there is no head to rewrite, and the safe move is to leave the whole thing
    alone rather than guess which part was the note.

    The card number is searched for from the RIGHT: a merchant or a note could
    contain four digits, and the last occurrence is the real boundary.
    """
    last4 = str(last4 or "").strip()
    parts = [p.strip() for p in str(memo or "").split(" - ")]
    if not last4 or last4 not in parts:
        return False, ""
    i = len(parts) - 1 - parts[::-1].index(last4)
    return True, " - ".join(parts[i + 1:]).strip()


def parse_simple_description(desc: str) -> dict:
    """Split "Merchant - Cardholder - 1234" into its parts.

    Split from the RIGHT: merchant names contain hyphens ("TM- BLUE JAYS")
    while the cardholder and last-4 are always the final two segments.
    """
    parts = [p.strip() for p in str(desc or "").split(" - ")]
    out = {"merchant": desc or "", "cardholder": "", "last4": ""}
    if len(parts) >= 3 and _TRAILING_LAST4.match(parts[-1]):
        out["last4"] = parts[-1]
        out["cardholder"] = parts[-2]
        out["merchant"] = " - ".join(parts[:-2]).strip()
    elif len(parts) == 2 and _TRAILING_LAST4.match(parts[-1]):
        out["last4"] = parts[-1]
        out["merchant"] = parts[0]
    return out


def _map_simple(row: dict, occurrence: int = 0) -> dict | None:
    raw = str(row.get("Amount", "")).strip()
    amt = _money(raw)
    d = _date(row.get("Date"))
    if amt is None or d is None:
        return None
    parsed = parse_simple_description(row.get("Description", ""))
    return {
        # no transaction id in this format, so identity is a content hash plus
        # an occurrence counter -- same approach as the raw WEX export
        "source_txn_id": _row_hash(row, occurrence),
        "amount": amt, "txn_date": d,
        "card_last4": parsed["last4"],
        "cardholder_name": parsed["cardholder"],
        "merchant": parsed["merchant"],
        "raw_description": row.get("Description", ""),
        # The Description column is what the team wants carried into the
        # QuickBooks memo, so it seeds the memo unless an explicit Memo column
        # is present. Editable per charge afterwards.
        "memo": _pick_memo(row) or str(row.get("Description", "") or "").strip(),
        # negative is a charge; positive is money coming back
        "is_credit": not raw.startswith("-"),
    }


MAPPERS = {"slash": _map_slash, "divvy": _map_divvy, "wex": _map_wex}


# Header signatures unique to each portal's export, used to catch a file being
# uploaded under the wrong card account -- picking "slash" and choosing the
# Divvy export would otherwise import garbage silently.
SOURCE_SIGNATURES = {
    "slash": {"Type", "Order Id", "Last 4"},
    "divvy": {"Clean Merchant Name", "Card Last 4", "Card Holder Email"},
    "wex": {"Transaction.Transaction Amount", "Card Number.Card No"},
}


def is_simple_format(headers: set[str]) -> bool:
    """The in-house pre-formatted export, which is identical for every card."""
    return _SIMPLE_COLS.issubset(headers)


def detect_source(headers: set[str]) -> str | None:
    """Which portal produced this CSV, judged by its column names."""
    best, score = None, 0
    for src, sig in SOURCE_SIGNATURES.items():
        hits = len(sig & headers)
        if hits > score:
            best, score = src, hits
    return best if score >= 2 else None


def filename_suggests_source(filename: str) -> str | None:
    """Which card program a file name points at, if any.

    The pre-formatted export has identical columns for every program, so the
    columns can't identify it. The file name usually can, and getting this wrong
    silently loads a whole month of charges under the wrong card account.
    """
    n = (filename or "").lower()
    hits = [s for s in SOURCES if s in n]
    return hits[0] if len(hits) == 1 else None


def ingest_csv(db: Session, source: str, content: bytes, company: str,
               filename: str = "", actor: str = "system",
               method: str = "upload", override_name_check: bool = False,
               preflight: bool = False, file_format: str = "") -> dict:
    """Parse a portal CSV and import charges. Returns a summary.

    With `preflight=True` nothing is written -- the file is parsed and counted
    so the person can be shown what a duplicate upload would do BEFORE it
    happens, rather than being told afterwards.
    """
    source = source.lower()
    # The source is just a name now. What has to be recognised is the FORMAT,
    # which is checked below once the headers have been read -- a pre-formatted
    # file is valid under any card account, whatever it's called.
    if file_format and file_format.lower() not in MAPPERS:
        raise ValueError(
            f"unknown file format '{file_format}' "
            f"(expected one of {tuple(sorted(MAPPERS))})")

    text = content.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))

    # Guard against the wrong file being uploaded under a card account. Without
    # this the mapper finds none of its columns, imports zero rows, and reports
    # "0 new" as though the file were simply a duplicate.
    headers = set(reader.fieldnames or [])

    # The pre-formatted export looks the same for every card program, so it
    # can't be auto-detected -- trust the card account the person picked.
    simple = is_simple_format(headers)
    detected = None if simple else detect_source(headers)

    # The file name is a hint, not evidence -- files get renamed. It's surfaced
    # as a warning before the upload and never blocks it.
    name_hint = filename_suggests_source(filename)
    name_mismatch = bool(name_hint and name_hint != source)
    # Which layout to parse with is read from the file's own columns. Asking
    # anyone to configure it was solving a problem the headers already answer,
    # and it meant a card account could be set up in a way its files contradict.
    fmt = detected or (file_format or source or "").lower()
    if not simple and fmt not in MAPPERS:
        raise ValueError(
            "this file doesn't match any export layout we recognise "
            f"({', '.join(sorted(MAPPERS))}), and isn't the pre-formatted "
            "export either. Check it's the file the portal produced.")

    mapper = _map_simple if simple else MAPPERS[fmt]

    seen = new = dup = skipped = dup_excluded = 0
    no_match_sample: list[str] = []
    occurrences: dict[str, int] = {}   # content-hash -> times seen in this file
    batch_ids: set[str] = set()        # guards duplicates within one file
    for row in reader:
        seen += 1
        if simple or fmt == "wex":
            # neither format carries a transaction id, so identical rows are
            # distinguished by how many times they've appeared in this file
            base = _row_hash(row)
            n_prev = occurrences.get(base, 0)
            occurrences[base] = n_prev + 1
            mapped = mapper(row, n_prev)
        else:
            mapped = mapper(row)
        if mapped is None:
            skipped += 1
            continue
        # Import every row. Where the id already exists -- a re-upload, or the
        # same transaction twice in one file -- add a suffix so both are kept
        # as distinct records rather than one silently replacing the other.
        base_id = f"{company}:{source}:{mapped['source_txn_id']}"
        charge_id = base_id
        n = 1
        # Collect the status of EVERY copy already held, not just the first.
        # Checking only the base id meant one excluded copy silenced the warning
        # for good: re-upload once after excluding (correctly quiet), and every
        # later re-upload stayed quiet too, even with live copies sitting in
        # For Review.
        existing: list[str] = []
        while True:
            if charge_id in batch_ids:
                charge_id = f"{base_id}#{n}"
                n += 1
                continue
            prior = db.get(ChargeRow, charge_id)
            if prior is None:
                break
            existing.append(prior.status)
            charge_id = f"{base_id}#{n}"
            n += 1
        # An excluded charge will never be posted, so it isn't a duplicate worth
        # warning about -- but it IS counted separately, or the totals look
        # wrong: "134 of 174" with no explanation of the other 40 reads like a
        # miscount rather than a deliberate omission.
        if any(st != "excluded" for st in existing):
            dup += 1                      # counted, but still imported
        elif existing:
            dup_excluded += 1
        elif preflight and len(no_match_sample) < 5:
            # Nothing here matched this row at all. Keeping a few examples turns
            # "why 134 of 174?" into a question the numbers can answer.
            no_match_sample.append(base_id)
        batch_ids.add(charge_id)
        new += 1
        if preflight:
            continue                      # counting only; write nothing
        db.add(ChargeRow(
            charge_id=charge_id,
            source_txn_id=mapped["source_txn_id"],
            company=company,
            source=source,
            amount=mapped["amount"],
            txn_date=mapped["txn_date"],
            card_last4=mapped.get("card_last4"),
            cardholder_name=mapped.get("cardholder_name"),
            email=mapped.get("email"),
            order_number=mapped.get("order_number"),
            merchant=mapped.get("merchant", ""),
            memo=mapped.get("memo", ""),
            raw_description=mapped.get("raw_description", ""),
            is_credit=mapped.get("is_credit", False),
        ))
        if new % 200 == 0:
            db.commit()

    if preflight:
        # Counted only -- discard anything staged so the queue is untouched.
        db.rollback()
        return {"source": source, "company": company, "filename": filename,
                "rows_seen": seen, "new": new, "duplicates": dup,
                "duplicates_excluded": dup_excluded,
                "no_match": new - dup - dup_excluded,
                "no_match_sample": no_match_sample,
                "skipped": skipped, "preflight": True,
                "name_mismatch": name_mismatch, "name_hint": name_hint or ""}

    db.commit()

    db.add(UploadRow(source=source, method=method, filename=filename,
                     company=company, rows_seen=seen, rows_new=new,
                     rows_duplicate=dup, actor=actor))
    db.commit()

    summary = {"source": source, "company": company, "filename": filename,
               "rows_seen": seen, "new": new,
               # `duplicates` now means "imported, but an identical charge was
               # already present" -- a warning, not a count of skipped rows.
               "duplicates": dup, "skipped": skipped}
    log.info("ingest %s: %s", source, summary)
    return summary


# --- scheduled pulls ---------------------------------------------------------

def pull_source(db: Session, source: str, company: str,
                since: date | None = None) -> dict:
    """Fetch charges from a source's API.

    Not yet wired to live APIs — Slash and Divvy both expose one, and each
    needs its own credential and endpoint. The shape is here so the worker can
    call it uniformly; each adapter returns CSV-equivalent rows that go through
    the same mappers and the same dedup.
    """
    if source not in PULLABLE:
        raise ValueError(
            f"'{source}' has no API pull — upload its export instead "
            f"(pullable: {sorted(PULLABLE)})")
    raise NotImplementedError(
        f"live {source} API pull is not wired yet; use manual upload")
