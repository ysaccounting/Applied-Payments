"""
Zone 2 intake — read a FILLED Zone-1 review workbook (.xlsx), apply the
answer-driven rules to the yellow (flagged) rows, and return a DataFrame in
the exact schema the existing processor.process() already expects.

Rules applied to YELLOW rows only (negative Amount or blank Company):
  1. Misc Company filled        -> Company := Misc Company (V/W/X ignored)
  2. Chargeback = Cancelled Event-> no change
  3. Chargeback = Discount       -> Company := Company + "-Fee"
  4. Chargeback = TradeDesk Fees -> Company := "Other Fees"
  5. Chargeback = Problem Order + Already Paid? = Yes
                                 -> SPLIT into two negative lines:
                                    line1 = -|Payout|, original Company
                                    line2 = Amount - line1, Company + "-Fee"
  6. Chargeback = Problem Order + Already Paid? = No
                                 -> Company := Company + "-Fee"
  7. Cancelled Out? = Yes        -> confirmation only (no effect)

Non-yellow rows pass through untouched; any answers on them are ignored.
Cancellation Reason (col T) becomes the processor's Reason field (col S).

Blocks (raise ValueError) when:
  - a yellow row has ALL answer columns (U-X) blank
  - a Problem Order row has no Already Paid? answer
"""
import os
import re
import pandas as pd
from openpyxl import load_workbook

from zone1 import RAW_COLS, ANSWER_HEADERS   # keep both sides in agreement

PROC_COLS = RAW_COLS + ['Reason']            # what processor.process() expects (19 cols, Reason at S)


def _to_amt(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace('$', '').replace(',', '').replace('(', '-').replace(')', '').strip())
    except Exception:
        return None


def _s(v):
    return '' if v is None else str(v).strip()


def _data_sheet(wb):
    for s in wb.worksheets:
        if s.title != 'Rules':
            return s
    return wb.worksheets[0]


def _locate_columns(ws):
    """Return dict of 1-based column indexes for raw + answer fields, by header name
    with a fallback to the fixed Zone-1 layout (A-R raw, T-X answers)."""
    header = {}
    for c in range(1, ws.max_column + 1):
        name = _s(ws.cell(row=1, column=c).value)
        if name:
            header.setdefault(name, c)
    cols = {}
    # raw cols A-R
    for i, name in enumerate(RAW_COLS, start=1):
        cols[name] = header.get(name, i)
    # answers T-X (fixed positions 20-24 in the Zone-1 layout)
    fixed = {'Cancellation Reason': 20, 'Misc Company': 21, 'Chargeback Type': 22,
             'Already Paid?': 23, 'Cancelled Out?': 24}
    for name, pos in fixed.items():
        cols[name] = header.get(name, pos)
    return cols


def read_filled_zone1(xlsx_path, original_filename):
    """
    -> (raw_df, processor_filename, zone1_values)
       raw_df            : DataFrame in PROC_COLS schema, rules applied.
       processor_filename: filename to hand to process() (the _zone1 suffix removed).
       zone1_values      : [header_row, *data_rows] (A-R + T-X) as submitted, for the tab.
    Raises ValueError on the block conditions above.
    """
    wb = load_workbook(xlsx_path, data_only=True)
    ws = _data_sheet(wb)
    col = _locate_columns(ws)

    def cell(r, name):
        return ws.cell(row=r, column=col[name]).value

    out_rows = []          # processor-input dicts
    snapshot = []          # for the Zone 1 Output tab
    snap_header = list(RAW_COLS) + list(ANSWER_HEADERS)
    snapshot.append(snap_header)

    errors = []
    confirm_errors = []
    for r in range(2, ws.max_row + 1):
        company = _s(cell(r, 'Company'))
        amount = _to_amt(cell(r, 'Amount'))
        order = _s(cell(r, 'Order#'))
        if amount is None and company == '' and order == '':
            continue  # truly empty trailing row

        reason = _s(cell(r, 'Cancellation Reason'))
        misc = _s(cell(r, 'Misc Company'))
        ctype = _s(cell(r, 'Chargeback Type'))
        paid = _s(cell(r, 'Already Paid?'))
        cancelled = _s(cell(r, 'Cancelled Out?'))

        # snapshot of exactly what was submitted (raw A-R + answers T-X)
        snapshot.append([_s(cell(r, n)) if n != 'Amount' else (amount if amount is not None else _s(cell(r, n)))
                         for n in RAW_COLS] + [reason, misc, ctype, paid, cancelled])

        flagged = (amount is not None and amount < 0) or (company == '')

        # base record carried into the processor (raw fields + Reason)
        base = {n: _s(cell(r, n)) for n in RAW_COLS}
        base['Amount'] = amount if amount is not None else _s(cell(r, 'Amount'))
        base['Reason'] = reason

        if not flagged:
            out_rows.append(base)            # ignore any answers on non-yellow rows
            continue

        # ----- yellow row: validate + apply rules -----
        if not any([misc, ctype, paid, cancelled]):
            errors.append(f"row {r} (Order# {order or 'blank'}, Amount {amount})")
            continue

        # Problem Order / Cancelled Event must be confirmed cancelled out in TV (X = Yes).
        # Skipped when Misc Company overrides the row (that ignores the V/W/X answers).
        if not misc and ctype in ('Problem Order', 'Cancelled Event') and cancelled != 'Yes':
            confirm_errors.append(f"row {r} (Order# {order or 'blank'}, {ctype})")

        if misc:                              # Rule 1 — Misc Company overrides Company
            base['Company'] = misc
            out_rows.append(base)
        elif ctype == 'Cancelled Event':      # Rule 2 — no change
            out_rows.append(base)
        elif ctype == 'Discount':             # Rule 3 — append -Fee
            base['Company'] = company + '-Fee'
            out_rows.append(base)
        elif ctype == 'TradeDesk Fees':       # Rule 4 — Other Fees
            base['Company'] = 'Other Fees'
            out_rows.append(base)
        elif ctype == 'Problem Order':
            if paid == 'Yes':                 # Rule 5 — split into two negatives
                payout = abs(_to_amt(cell(r, 'Payout')) or 0.0)
                line1 = dict(base); line1['Amount'] = -payout              # original company
                line2 = dict(base); line2['Amount'] = (amount or 0.0) + payout
                line2['Company'] = company + '-Fee'                       # remainder
                out_rows.append(line1)
                out_rows.append(line2)
            elif paid == 'No':                # Rule 6 — append -Fee
                base['Company'] = company + '-Fee'
                out_rows.append(base)
            else:
                errors.append(f"row {r} (Order# {order or 'blank'}): Problem Order needs an 'Already Paid?' answer")
                continue
        else:
            # only Cancelled Out? (or some non-transforming answer) was set — pass through unchanged
            out_rows.append(base)

    problems = []
    if errors:
        problems.append("These yellow rows still need answers "
                        "(fill the Misc Company / Chargeback Type / Already Paid? / Cancelled Out? columns):\n  • "
                        + "\n  • ".join(errors))
    if confirm_errors:
        problems.append("These rows are Problem Order or Cancelled Event but don't have 'Cancelled Out?' = Yes. "
                        "Confirm the order is cancelled out in TV, then set column X to Yes:\n  • "
                        + "\n  • ".join(confirm_errors))
    if problems:
        raise ValueError("Can't process —\n\n" + "\n\n".join(problems))

    raw_df = pd.DataFrame(out_rows, columns=PROC_COLS)

    # processor filename: drop the _zone1 suffix, treat as the original report name
    fn = re.sub(r'_zone1', '', original_filename, flags=re.I)
    processor_filename = os.path.splitext(fn)[0] + '.csv'

    return raw_df, processor_filename, snapshot


def looks_like_zone1(xlsx_path):
    """True if the workbook carries the Zone-1 answer columns."""
    try:
        wb = load_workbook(xlsx_path, read_only=True, data_only=True)
        ws = _data_sheet(wb)
        hdr = {_s(ws.cell(row=1, column=c).value) for c in range(1, (ws.max_column or 0) + 1)}
        wb.close()
        return 'Chargeback Type' in hdr and 'Misc Company' in hdr
    except Exception:
        return False


def add_zone1_output_tab(wb, snapshot, title='Zone 1 Output'):
    """Append the as-submitted Zone-1 data (header + rows) as a tab in an output workbook."""
    from openpyxl.styles import Font, PatternFill, Alignment
    ws = wb.create_sheet(title)
    HFONT = Font(name='Arial', size=10, bold=True)
    FONT = Font(name='Arial', size=10)
    HEAD = PatternFill('solid', fgColor='D9E1F2')
    FLAG = PatternFill('solid', fgColor='FFF2CC')
    header = snapshot[0]
    for ci, name in enumerate(header, 1):
        c = ws.cell(row=1, column=ci, value=name); c.font = HFONT; c.fill = HEAD
        c.alignment = Alignment(horizontal='center', vertical='center')
    for ri, row in enumerate(snapshot[1:], start=2):
        amt = row[2] if isinstance(row[2], (int, float)) else _to_amt(row[2])
        company = _s(row[0])
        flagged = (amt is not None and amt < 0) or company == ''
        for ci, val in enumerate(row, 1):
            c = ws.cell(row=ri, column=ci, value=val); c.font = FONT
            if ci == 3 and isinstance(val, (int, float)):
                c.number_format = '#,##0.00'; c.alignment = Alignment(horizontal='center')
            if flagged:
                c.fill = FLAG
    widths = {'A': 16, 'B': 14, 'C': 12, 'D': 14, 'E': 8, 'F': 10, 'G': 32, 'H': 18, 'I': 18, 'J': 20,
              'K': 14, 'L': 8, 'M': 6, 'N': 8, 'O': 5, 'P': 20, 'Q': 18, 'R': 26, 'S': 22, 'T': 18,
              'U': 17, 'V': 14, 'W': 15}
    for col, w in widths.items():
        ws.column_dimensions[col].width = w
    ws.freeze_panes = 'I2'   # keep columns A-H pinned while scrolling right
    return ws
