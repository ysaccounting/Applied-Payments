"""
Append processed results to a shared Google Sheet, one tab per month.

Uses a Google service account (a single shared credential set via env vars), so no
user ever signs into Google — same model as the shared QBO connection. Each output
row is routed to a "Month YYYY" tab (created on demand) based on its own date.
"""
import os
import json
from datetime import datetime

# Env-driven config (set these in Railway; seal the JSON one):
#   GOOGLE_SERVICE_ACCOUNT_JSON -> the full service-account key JSON (as a string)
#   GOOGLE_SHEET_TABS           -> JSON map of source tab -> spreadsheet ID, e.g.
#                                  {"Y&S":"<idA>","StubHub Loan":"<idA>",
#                                   "Affiliates":"<idB>","Other":"<idB>"}
#   GOOGLE_SHEET_ID             -> a single spreadsheet ID used for any tab NOT in
#                                  the map (and the only var needed if you want all
#                                  tabs in one sheet, i.e. the old behavior).
SA_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "")


_SKIP_VALUES = {"", "skip", "none", "-", "false", "no", "exclude"}


def _parse_tabs():
    """Parse GOOGLE_SHEET_TABS into (routed, excluded).
    routed   = {tab_name: spreadsheet_id}
    excluded = {tab_name, ...} for tabs intentionally NOT sent (value is "skip"/null).
    """
    raw = os.environ.get("GOOGLE_SHEET_TABS", "")
    routed, excluded = {}, set()
    if raw:
        try:
            m = json.loads(raw)
        except Exception:
            m = {}
        for k, v in m.items():
            k = str(k)
            if v is None or v is False or (isinstance(v, str) and v.strip().lower() in _SKIP_VALUES):
                excluded.add(k)
            elif v:
                routed[k] = str(v)
    return routed, excluded


def _tab_map():
    return _parse_tabs()[0]


def _excluded_tabs():
    return _parse_tabs()[1]


def _sheet_for_tab(tab):
    """Which spreadsheet ID a given source tab routes to (map first, then default)."""
    return _tab_map().get(tab) or SHEET_ID or ""


def _fixed_tab_map():
    """Parse GOOGLE_SHEET_FIXED_TABS: source tab -> a fixed worksheet name.
    Rows from a source tab listed here always go to that one worksheet instead of a
    monthly tab. Example: {"StubHub Loan": "StubHub Loan Repay"}."""
    raw = os.environ.get("GOOGLE_SHEET_FIXED_TABS", "")
    if not raw:
        return {}
    try:
        m = json.loads(raw)
        return {str(k): str(v) for k, v in m.items() if v}
    except Exception:
        return {}


SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Columns written to each month tab — the four detail tabs combined, with a
# leading "Tab" column showing which Applied Payments tab each row came from.
DETAIL_HEADER = [
    "Tab", "Company", "Date", "Network", "Type", "Order#", "Amount",
    "Performer", "Venue", "EventDate", "Section", "Row", "Seat", "Qty", "Reason",
]


class SheetsNotConfigured(Exception):
    pass


def is_configured():
    """Configured if we have credentials AND at least one destination (map or default)."""
    return bool(SA_JSON and (_tab_map() or SHEET_ID))


def _service_account_email():
    try:
        return json.loads(SA_JSON).get("client_email", "")
    except Exception:
        return ""


def _client():
    """Authorize a gspread client from the service-account JSON (lazy imports)."""
    if not is_configured():
        raise SheetsNotConfigured(
            "Google Sheets isn't configured. Set GOOGLE_SERVICE_ACCOUNT_JSON and "
            "either GOOGLE_SHEET_TABS or GOOGLE_SHEET_ID."
        )
    import gspread
    from google.oauth2.service_account import Credentials
    info = json.loads(SA_JSON)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return gspread.authorize(creds)


def _month_tab_name(date_str):
    """'mm/dd/yyyy' -> 'May 2026'. Raises if the date can't be parsed."""
    dt = datetime.strptime(str(date_str).strip(), "%m/%d/%Y")
    return dt.strftime("%B %Y")


def _detail_row_to_list(rec):
    return [rec.get(col, "") for col in DETAIL_HEADER]


def _worksheet_for_row(rec):
    """Destination worksheet name for a row: a fixed override if its source tab has
    one, otherwise the 'Month YYYY' tab for its date."""
    fixed = _fixed_tab_map().get(rec.get("Tab", ""))
    if fixed:
        return fixed
    try:
        return _month_tab_name(rec.get("Date", ""))
    except Exception:
        return "Undated"


def _group_by_worksheet(recs):
    """Group rows by their destination worksheet name within one spreadsheet."""
    out = {}
    for rec in recs:
        out.setdefault(_worksheet_for_row(rec), []).append(_detail_row_to_list(rec))
    return out


def _append_rows_to_month_tab(ss, month, rows, existing):
    """Append rows into a single 'Month YYYY' worksheet of `ss`, creating it if needed."""
    ws = existing.get(month)
    if ws is None:
        ws = ss.add_worksheet(title=month, rows=max(500, len(rows) + 50), cols=len(DETAIL_HEADER))
        ws.append_row(DETAIL_HEADER, value_input_option="USER_ENTERED")
        try:
            ws.freeze(rows=1)
            ws.format("A1:O1", {"textFormat": {"bold": True}})
        except Exception:
            pass  # formatting is cosmetic; never fail the append over it
        existing[month] = ws
    elif not ws.row_values(1):
        ws.append_row(DETAIL_HEADER, value_input_option="USER_ENTERED")
    ws.append_rows(rows, value_input_option="USER_ENTERED")


def append_detail(detail_rows):
    """
    Route each detail row to the spreadsheet configured for its source tab
    (GOOGLE_SHEET_TABS, falling back to GOOGLE_SHEET_ID), then append it into the
    'Month YYYY' tab for its date. Returns:
        {
          "sheets": [{"url":..., "spreadsheet_id":..., "source_tabs":[...],
                      "tabs": {"May 2026": n}, "total": n}, ...],
          "total": <rows written>,
          "skipped": {"<tab>": n, ...}   # rows whose source tab has no destination
        }
    """
    # 1) Bucket rows by their target spreadsheet ID (resolved from the source Tab).
    excluded_tabs = _excluded_tabs()
    by_sheet = {}            # spreadsheet_id -> list[rec]
    by_sheet_tabs = {}       # spreadsheet_id -> set(source tab names)
    skipped = {}             # source tab -> count (no destination configured)
    excluded = {}            # source tab -> count (intentionally not sent)
    for rec in detail_rows:
        src_tab = rec.get("Tab", "")
        if src_tab in excluded_tabs:
            excluded[src_tab] = excluded.get(src_tab, 0) + 1
            continue
        target = _sheet_for_tab(src_tab)
        if not target:
            skipped[src_tab] = skipped.get(src_tab, 0) + 1
            continue
        by_sheet.setdefault(target, []).append(rec)
        by_sheet_tabs.setdefault(target, set()).add(src_tab)

    if not by_sheet:
        # Nothing routable. If everything was intentionally excluded, say so plainly.
        if excluded and not skipped:
            return {"sheets": [], "total": 0, "skipped": {}, "excluded": excluded}
        raise SheetsNotConfigured(
            "None of these tabs are mapped to a spreadsheet. Set GOOGLE_SHEET_TABS "
            "(or GOOGLE_SHEET_ID) so each tab has a destination."
        )

    # 2) For each target spreadsheet, append rows into their monthly tabs.
    client = _client()
    sheets_summary = []
    grand_total = 0
    for sheet_id, recs in by_sheet.items():
        ss = client.open_by_key(sheet_id)
        existing = {ws.title: ws for ws in ss.worksheets()}
        by_ws = _group_by_worksheet(recs)
        month_counts = {}
        for ws_name, rows in by_ws.items():
            _append_rows_to_month_tab(ss, ws_name, rows, existing)
            month_counts[ws_name] = len(rows)
        total = sum(month_counts.values())
        grand_total += total
        sheets_summary.append({
            "url": ss.url,
            "spreadsheet_id": sheet_id,
            "source_tabs": sorted(by_sheet_tabs[sheet_id]),
            "tabs": month_counts,
            "total": total,
        })

    return {"sheets": sheets_summary, "total": grand_total, "skipped": skipped, "excluded": excluded}


def humanize_sheets_error(exc) -> str:
    """Plain-language message for the UI."""
    if isinstance(exc, SheetsNotConfigured):
        return str(exc)
    import json as _json
    low = str(exc).lower()
    # Key/JSON can't be parsed (happens before any Google call) — usually a bad paste.
    if isinstance(exc, (_json.JSONDecodeError, ValueError)) and (
        "expecting value" in low or "json" in low or "deserialize" in low
        or "service account" in low or "key" in low or "padding" in low or "format" in low
    ):
        return ("The Google service-account key (GOOGLE_SERVICE_ACCOUNT_JSON) couldn't be read — "
                "it's likely incomplete or its line breaks got mangled. Re-paste the full key JSON.")
    if "deserialize" in low or "no key could be detected" in low or "expecting value" in low:
        return ("The Google service-account key (GOOGLE_SERVICE_ACCOUNT_JSON) couldn't be read — "
                "it's likely incomplete or its line breaks got mangled. Re-paste the full key JSON.")
    if "drive api has not been used" in low or "drive api is disabled" in low or "has not been used in project" in low:
        return "Enable the Google Drive API for the project, then try again."
    if "permission" in low or "403" in low or "does not have access" in low:
        email = _service_account_email() or "the service account"
        return f"The Google Sheet isn't shared with {email}. Share it as an Editor and try again."
    if "404" in low or "not found" in low or "unable to open" in low:
        return "Couldn't open a configured Google Sheet — check the IDs in GOOGLE_SHEET_TABS / GOOGLE_SHEET_ID."
    if "invalid_grant" in low or "credential" in low or "jwt" in low or "invalid_client" in low:
        return "The Google service-account credentials are invalid — check GOOGLE_SERVICE_ACCOUNT_JSON."
    return "Couldn't write to Google Sheets. Please try again."
