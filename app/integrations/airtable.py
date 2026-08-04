"""
Airtable mirror — pulls the HAL season-ticket records into local storage.

Why mirror rather than query live: HAL is ~30,000 records and the rules run
against every charge in a daily batch. Hitting the API per charge would be slow
and rate-limited. Instead the worker pages the relevant slice into Postgres on a
schedule, and the classifier queries the local copy.

Field IDs are the real ones from the Y&S "Tickets" base. They're pinned here
because Airtable field *names* can be renamed by users while IDs are stable.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Iterator

import time

import httpx

from ..config import settings

log = logging.getLogger(__name__)


class AirtableAuthError(RuntimeError):
    pass


class AirtableQueryError(RuntimeError):
    pass

API = "https://api.airtable.com/v0"

BASE_TICKETS = "appOEQZDCBXBZRiUv"
TABLE_HAL = "tblHVnNLLKSnvjoKr"
TABLE_ADDRESS_BOOK = "tblSBLNnEWyhjrdHn"
TABLE_TEAMS = "tblHjlJG913h29EnU"
# The Emails table. Read directly rather than through the address book's E-Mail
# column, because over the REST API a linked-record field returns bare record
# ids -- ["rec7SLm..."] -- and never the address itself. This table carries both
# the address and the profile name as plain text.
TABLE_EMAILS = "tblDBBSFfsl92GIip"
F_EM_ADDRESS = "fldfS8zFymh4kIt1N"     # "pendent_dairies8x@icloud.com"
F_EM_PERSON = "fld6Ka3DqaL0osGqS"      # "Jane Santiago"

# Address Book fields used to build the person's name
F_AB_FIRSTNAME = "fldBl388T6D83cAOx"
F_AB_LASTNAME = "fldQ9ylAmLdvCCnc5"
F_AB_LOOKUP = "fldCAmTJkqgLD3hZ2"      # primary: "Akeya Eleni (CA) San Francisco 94132"
# The address book's "E-Mail" column, and the ONLY place an address is read
# from. It links to the Emails table and allows several links per profile
# (prefersSingleRecordLink: false), so it already holds every address a profile
# buys under -- there is nothing to supplement it with.
#
# Deliberately not used: the neighbouring lookup fields. They reach through a
# DIFFERENT link to pull recovery and forwarding addresses off related records,
# which are not addresses this person buys under, and matching on them would
# attach charges to the wrong buyer.
F_AB_EMAIL = "fldqXIrzfwuxEVgXs"

F_TEAM_NAME = "fldvPQkyhI69Pm0zp"      # Teams primary: "San Jose Sharks"

# --- HAL field IDs -----------------------------------------------------------
F_LOOKUP = "fld4YB0f3SzM97YAs"      # "LA Clippers matthewjude86@outlook.com"
F_ADDRESS_BOOK = "fldxpdk4QRYmskwDX"  # "Matthew Jude (CA) Los Angeles 90004"
F_TEAM = "fldpUJf4MQ61kfYsZ"
F_FULL_PARTIAL = "fldWKMM4HFjzr0i3t"  # plan type; carries Wait List / DEPOSIT
F_TOTAL = "fldKTWBlOVClzGvav"
F_CARD = "fldKJyhEeOvropPHO"          # card PROGRAM name, not a number
F_PAYMENT_PLAN = "fldCtfLz5E4kqlT23"  # checkbox

# Season status fields, keyed by season label.
# IDs are used for FIELD SELECTION (stable across renames); names are required
# for filterByFormula, which does not accept field IDs.
STATUS_FIELDS = {
    "24/25": "fldrme0zedWhb3mUE",
    "25/26": "fldixaT5UFHyDaFkd",
    "26/27": "fldFiTzd1eig0eg0B",
    "27/28": "fldFqi6LIWFfpCYUQ",
}

STATUS_FIELD_NAMES = {
    "24/25": "24/25 Status",
    "25/26": "25/26 Status",
    "26/27": "26/27 Status",
    "27/28": "27/28 Status",
}


def _cell(rec: dict, field_id: str):
    return rec.get("cellValuesByFieldId", rec.get("fields", {})).get(field_id)


def _select_name(value) -> str | None:
    """singleSelect comes back as {"id","name","color"}."""
    if isinstance(value, dict):
        return value.get("name")
    return value or None


def _multi_names(value) -> str:
    """multipleSelects comes back as a list of those objects."""
    if isinstance(value, list):
        return ",".join(v.get("name", "") for v in value if isinstance(v, dict))
    return str(value or "")


def _linked_name(value) -> str:
    """Linked records come back as [{"id","name"}]."""
    if isinstance(value, list) and value:
        first = value[0]
        return first.get("name", "") if isinstance(first, dict) else str(first)
    return ""


def fetch_hal(seasons: list[str] | None = None,
              page_size: int = 100,
              modified_since_minutes: int | None = None) -> Iterator[dict]:
    """Page the HAL table, yielding normalized dicts.

    Filters to records that have a status set for at least one of `seasons`, so
    the mirror stays to the live population rather than all ~30k historical rows.
    """
    # Include 25/26. Plenty of records that are live today still have only a
    # 25/26 status set -- filtering to 26/27+ dropped them from the mirror
    # entirely, so their cardholders looked like they had no HAL record at all.
    seasons = seasons or ["25/26", "26/27", "27/28"]
    fields = [F_LOOKUP, F_ADDRESS_BOOK, F_TEAM, F_FULL_PARTIAL, F_TOTAL,
              F_CARD, F_PAYMENT_PLAN] + [STATUS_FIELDS[s] for s in seasons
                                         if s in STATUS_FIELDS]

    # filterByFormula takes field NAMES. Records with any season status set.
    clauses = [f"{{{STATUS_FIELD_NAMES[s]}}} != ''"
               for s in seasons if s in STATUS_FIELD_NAMES]
    formula = f"OR({','.join(clauses)})" if clauses else ""

    if modified_since_minutes:
        # Incremental: only records touched recently. LAST_MODIFIED_TIME() with
        # no arguments is the record's own last-modified stamp, so this catches
        # any edit -- status, plan type, links -- not just one field.
        window = (f"IS_AFTER(LAST_MODIFIED_TIME(), "
                  f"DATEADD(NOW(), -{int(modified_since_minutes)}, 'minutes'))")
        formula = f"AND({formula},{window})" if formula else window

    headers = {"Authorization": f"Bearer {settings.airtable_token}"}
    params: list = [("pageSize", page_size), ("returnFieldsByFieldId", "true")]
    if formula:
        params.append(("filterByFormula", formula))
    params += [("fields[]", f) for f in fields]

    url = f"{API}/{BASE_TICKETS}/{TABLE_HAL}"
    offset = None
    pages = 0
    with httpx.Client(timeout=30) as client:
        while True:
            q = list(params)
            if offset:
                q.append(("offset", offset))
            r = client.get(url, headers=headers, params=q)
            if r.status_code in (401, 403):
                raise AirtableAuthError(
                    f"Airtable returned {r.status_code}. Check that the token "
                    f"exists, has scopes data.records:read and schema.bases:read, "
                    f"AND has been granted access to the Tickets base "
                    f"({BASE_TICKETS}). Adding scopes alone is not enough — the "
                    f"base must be added under the token's Access section. "
                    f"Airtable said: {r.text[:200]}")
            if r.status_code == 422:
                raise AirtableQueryError(
                    f"Airtable rejected the query (422). Usually a field name in "
                    f"filterByFormula that doesn't exist. Airtable said: {r.text[:300]}")
            if r.status_code == 429:
                time.sleep(2)          # backoff and retry this page
                continue
            r.raise_for_status()
            body = r.json()
            for rec in body.get("records", []):
                yield _map_record(rec, seasons)
            pages += 1
            offset = body.get("offset")
            if not offset:
                break
            time.sleep(0.25)   # Airtable allows 5 req/sec per base
    log.info("fetched HAL in %d pages", pages)


def _fetch_table(table_id: str, fields: list[str],
                 page_size: int = 100) -> Iterator[dict]:
    """Page any table, returning raw records."""
    headers = {"Authorization": f"Bearer {settings.airtable_token}"}
    base_params = [("pageSize", page_size), ("returnFieldsByFieldId", "true")]
    base_params += [("fields[]", f) for f in fields]
    url = f"{API}/{BASE_TICKETS}/{table_id}"
    offset = None
    with httpx.Client(timeout=30) as client:
        while True:
            q = list(base_params)
            if offset:
                q.append(("offset", offset))
            r = client.get(url, headers=headers, params=q)
            if r.status_code in (401, 403):
                raise AirtableAuthError(
                    f"Airtable returned {r.status_code} reading {table_id}. "
                    f"The token needs access to the Tickets base.")
            if r.status_code == 429:
                time.sleep(2)
                continue
            r.raise_for_status()
            body = r.json()
            for rec in body.get("records", []):
                yield rec
            offset = body.get("offset")
            if not offset:
                break
            time.sleep(0.25)


_EMAIL_IN_TEXT = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def _emails_from(value) -> set[str]:
    """Every address in a linked-record cell, whatever shape it arrives in."""
    if value is None:
        return set()
    if isinstance(value, list):
        text = " ".join(
            v.get("name", "") if isinstance(v, dict) else str(v) for v in value)
    elif isinstance(value, dict):
        # {"linkedRecordIds": [...], "valuesByLinkedRecordId": {...}}
        text = json.dumps(value)
    else:
        text = str(value)
    return {m.group(0).lower() for m in _EMAIL_IN_TEXT.finditer(text)}


def fetch_person_emails() -> dict[str, set[str]]:
    """Map a cardholder's name -> every email address on their profile.

    The name on a card and the email on a bill are the same person, but only
    Divvy's export states the email. This closes that gap for every other card
    program, which is what lets the email signal fire at all.

    Read from the Emails table, not the address book. The address book links to
    it, and over REST a linked-record field comes back as record ids with no
    text -- so reading it there yielded nothing at all. Here both the address
    and the person's name are plain fields.

    Addresses shared by more than one profile are dropped. A team mailbox
    appears on dozens of records, so matching on it would attach a charge to
    whichever bill happened to be closest -- worse than no signal, because it
    looks like certainty.
    """
    by_name: dict[str, set[str]] = {}
    seen = 0
    for rec in _fetch_table(TABLE_EMAILS, [F_EM_ADDRESS, F_EM_PERSON]):
        cells = rec.get("fields", {})
        seen += 1
        email = _first_text(cells.get(F_EM_ADDRESS)).strip().lower()
        name = _first_text(cells.get(F_EM_PERSON)).strip().lower()
        if name and email and "@" in email:
            by_name.setdefault(name, set()).add(email)

    owners: dict[str, int] = {}
    for emails in by_name.values():
        for e in emails:
            owners[e] = owners.get(e, 0) + 1
    shared = {e for e, n in owners.items() if n > 1}

    out = {k: (v - shared) for k, v in by_name.items()}
    out = {k: v for k, v in out.items() if v}
    log.info("address book: %d email rows -> %d cardholders, %d addresses "
             "(%d shared, dropped)", seen, len(out),
             sum(len(v) for v in out.values()), len(shared))
    return out


def _first_text(value) -> str:
    """A cell's text, whatever wrapper Airtable put it in."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        for v in value:
            if isinstance(v, str):
                return v
            if isinstance(v, dict) and v.get("name"):
                return v["name"]
        return ""
    if isinstance(value, dict):
        return value.get("name") or ""
    return str(value)


def fetch_person_names() -> dict:
    """Map Address Book record id -> person's name.

    Necessary because the REST API returns linked-record fields as bare record
    IDs (["recXXXX"]), NOT as {"id","name"} objects. Without this resolution,
    HAL's Address Book link is an opaque ID and no cardholder ever matches.
    """
    out = {}
    for rec in _fetch_table(TABLE_ADDRESS_BOOK,
                            [F_AB_FIRSTNAME, F_AB_LASTNAME, F_AB_LOOKUP]):
        cells = rec.get("fields", {})
        first = (cells.get(F_AB_FIRSTNAME) or "").strip()
        last = (cells.get(F_AB_LASTNAME) or "").strip()
        if first or last:
            name = f"{first} {last}".strip()
        else:
            # fall back to the primary field, "Name (ST) City 12345"
            raw = cells.get(F_AB_LOOKUP) or ""
            name = re.sub(r"\s*\(.*$", "", str(raw)).strip()
        if name:
            out[rec["id"]] = name
    log.info("resolved %d address-book names", len(out))
    return out


def fetch_team_names() -> dict:
    """Map Teams record id -> team name."""
    out = {}
    for rec in _fetch_table(TABLE_TEAMS, [F_TEAM_NAME]):
        name = (rec.get("fields", {}).get(F_TEAM_NAME) or "").strip()
        if name:
            out[rec["id"]] = name
    log.info("resolved %d team names", len(out))
    return out


def _link_ids(value) -> list:
    """Linked-record cell -> list of record ids (handles both shapes)."""
    if not isinstance(value, list):
        return []
    ids = []
    for v in value:
        if isinstance(v, dict):
            ids.append(v.get("id") or v.get("name") or "")
        else:
            ids.append(str(v))
    return [i for i in ids if i]


def _map_record(rec: dict, seasons: list[str]) -> dict:
    statuses = {}
    for s in seasons:
        fid = STATUS_FIELDS.get(s)
        if fid:
            name = _select_name(_cell(rec, fid))
            if name:
                statuses[s] = name
    return {
        "record_id": rec["id"],
        "lookup_name": _cell(rec, F_LOOKUP) or "",
        # raw ids; resolved to names by the worker using the lookup maps
        "address_book_ids": _link_ids(_cell(rec, F_ADDRESS_BOOK)),
        "team_ids": _link_ids(_cell(rec, F_TEAM)),
        "address_book": _linked_name(_cell(rec, F_ADDRESS_BOOK)),
        "team": _linked_name(_cell(rec, F_TEAM)),
        "plan_type": _multi_names(_cell(rec, F_FULL_PARTIAL)),
        "total": _cell(rec, F_TOTAL) or 0,
        "card_program": _cell(rec, F_CARD) or "",
        "has_payment_plan": bool(_cell(rec, F_PAYMENT_PLAN)),
        "statuses": statuses,
    }
