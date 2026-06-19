"""
Zone 1 — turn a RAW applied-payments report (CSV) into an enriched review
workbook (.xlsx) with the answer columns + dropdowns + Rules tab.

Public API:
    generate_review_workbook(input_path) -> (openpyxl Workbook, data_tab_name)

The workbook is identical to the standalone prototype; this module only
parameterizes the input/output so app.py can call it.
"""
import os
import csv
import io
import re
import processor as P
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side, Protection
from openpyxl.worksheet.protection import SheetProtection
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.formatting.rule import FormulaRule
from openpyxl.cell.rich_text import CellRichText, TextBlock
from openpyxl.cell.text import InlineFont

RAW_COLS = ['Company', 'Order#', 'Amount', 'Status', 'TV Fee', 'Payout', 'EventName', 'Performer',
            'Opponent', 'Venue', 'EventDate', 'Section', 'Row', 'Seat', 'Qty', 'CancellationReason',
            'InternalFulfillmentStatus', 'Notes']

# The answer columns that Zone 2 reads back (kept here so both sides agree).
ANSWER_HEADERS = ['Cancellation Reason', 'Misc Company', 'Chargeback Type', 'Already Paid?', 'Cancelled Out?']


def _realign(fields):
    """Same comma-shift handling the processor uses, returning canonical 18 cols."""
    f = list(fields)
    at = lambda i: f[i] if i < len(f) else ''
    if   P._is_valid_date(at(10)): shift = 0
    elif P._is_valid_date(at(11)): shift = 1
    elif P._is_valid_date(at(12)): shift = 2
    else:                          shift = 0
    eventname = ','.join(f[6:7 + shift])
    tail = f[7 + shift:7 + shift + 11]; tail += [''] * (11 - len(tail))
    return [str(x).strip() for x in (f[0:6] + [eventname] + tail)]


def _to_amt(v):
    try:
        return float(str(v).replace('$', '').replace(',', '').replace('(', '-').replace(')', '').strip())
    except Exception:
        return None


_DATE_RE = re.compile(r'^\s*(\d{1,2}/\d{1,2}/\d{2,4})')


def _strip_time(s):
    """EventDate display: keep the date, drop any '5:00:00 PM' timestamp."""
    s = str(s).strip()
    if not s:
        return ''
    m = _DATE_RE.match(s)
    return m.group(1) if m else s


def _is_flagged(canon):
    a = _to_amt(canon[2])
    return (a is not None and a < 0) or (canon[0].strip() == '')


def _read_rows(input_path):
    """Read the raw report resiliently (handles stray CP1252 bytes / BOM)."""
    with open(input_path, 'rb') as fh:
        data = fh.read()
    if data[:3] == b'\xef\xbb\xbf':
        data = data[3:]
    text = data.decode('utf-8', errors='utf8_cp1252')
    return list(csv.reader(io.StringIO(text)))


def _tab_name(input_path):
    try:
        net = P.parse_filename(os.path.basename(input_path))[0]
        name = re.sub(r'[^A-Za-z0-9 ]', '', str(net)).strip()[:31]
        return name or 'Report'
    except Exception:
        return 'Report'


def generate_review_workbook(input_path):
    """Build and return (Workbook, data_tab_name) for the given raw report CSV."""
    all_rows = _read_rows(input_path)
    rows = [_realign(r) for r in all_rows[1:]]
    # flagged (negative amount OR blank company) to the top, then by amount ascending
    rows.sort(key=lambda c: (0 if _is_flagged(c) else 1, _to_amt(c[2]) if _to_amt(c[2]) is not None else 0))

    wb = Workbook(); ws = wb.active; ws.title = _tab_name(input_path)
    FONT = Font(name='Arial', size=10)
    HFONT = Font(name='Arial', size=10, bold=True)
    HEAD_FILL = PatternFill('solid', fgColor='D9E1F2')
    FLAG_FILL = PatternFill('solid', fgColor='FFF2CC')
    GRAY = PatternFill('solid', fgColor='D9D9D9')
    thin = Side(style='thin', color='BFBFBF'); border = Border(thin, thin, thin, thin)

    # A-R raw | S sep | T Reason | U Misc Company | V Chargeback Type | W Already Paid? | X Cancelled Out?
    headers = RAW_COLS + [''] + ANSWER_HEADERS
    for ci, name in enumerate(headers, 1):
        c = ws.cell(row=1, column=ci, value=name); c.font = HFONT
        c.alignment = Alignment(horizontal='center', vertical='center')
        c.fill = GRAY if ci == 19 else HEAD_FILL
        c.border = border

    UNLOCKED = Protection(locked=False)
    LOCKED = Protection(locked=True)
    for ri, canon in enumerate(rows, start=2):
        flag = _is_flagged(canon)
        for ci in range(1, 19):                       # A-R
            val = canon[ci - 1]
            if ci == 3:
                a = _to_amt(val); val = a if a is not None else val
            if ci == 11:
                val = _strip_time(val)
            cell = ws.cell(row=ri, column=ci, value=val); cell.font = FONT
            if ci == 3:
                cell.number_format = '#,##0.00'; cell.alignment = Alignment(horizontal='center')
            if flag:
                cell.fill = FLAG_FILL
            cell.protection = UNLOCKED
        sep = ws.cell(row=ri, column=19, value=' '); sep.fill = GRAY; sep.protection = UNLOCKED
        rc = ws.cell(row=ri, column=20, value=canon[15]); rc.font = FONT; rc.protection = UNLOCKED
        if flag:
            rc.fill = FLAG_FILL
        for ci in (21, 22, 23, 24):                   # U,V,W,X input cols
            cell = ws.cell(row=ri, column=ci); cell.font = FONT
            if flag:
                cell.fill = FLAG_FILL
            cell.protection = UNLOCKED if flag else LOCKED   # block answers on non-yellow rows

    last = len(rows) + 1

    def dv(formula):
        d = DataValidation(type='list', formula1=formula, allow_blank=True,
                           showErrorMessage=True, errorStyle='stop')
        d.error = 'Pick a value from the list.'; d.errorTitle = 'Invalid entry'
        ws.add_data_validation(d); return d
    dv('"Due from/to TickPick,Not Found,StubHub Loan"').add(f'U2:U{last}')
    dv('"Cancelled Event,Discount,Problem Order,TradeDesk Fees"').add(f'V2:V{last}')
    dv('"Yes,No"').add(f'W2:W{last}')
    dv('"Yes"').add(f'X2:X{last}')

    widths = {'A': 16, 'B': 14, 'C': 12, 'D': 14, 'E': 8, 'F': 10, 'G': 32, 'H': 20, 'I': 20, 'J': 22,
              'K': 18, 'L': 8, 'M': 6, 'N': 8, 'O': 5, 'P': 22, 'Q': 20, 'R': 30, 'S': 3, 'T': 34,
              'U': 22, 'V': 17, 'W': 15, 'X': 16}
    for col, w in widths.items():
        ws.column_dimensions[col].width = w

    ws.freeze_panes = 'I2'   # keep columns A-H (Company..Performer) pinned while scrolling right
    ws.auto_filter.ref = 'A1:R1'
    ws.protection = SheetProtection(sheet=True,
        selectLockedCells=False, selectUnlockedCells=False,
        sort=False, autoFilter=False,
        formatCells=False, formatColumns=False, formatRows=False)

    red = PatternFill('solid', fgColor='FFC7CE')
    redfont = Font(name='Arial', size=10, color='9C0006')
    ws.conditional_formatting.add(f'V2:X{last}',
        FormulaRule(formula=['$U2<>""'], fill=red, font=redfont, stopIfTrue=False))

    _build_rules_tab(wb)
    return wb, ws.title


def _build_rules_tab(wb):
    rs = wb.create_sheet('Rules')
    FONT = Font(name='Arial', size=10)
    TITLE = Font(name='Arial', size=13, bold=True)
    SUB = Font(name='Arial', size=10, bold=True, color='1F4E78')
    RH = Font(name='Arial', size=10, bold=True, color='FFFFFF')
    RHEAD = PatternFill('solid', fgColor='4472C4')
    wrap = Alignment(wrap_text=True, vertical='top')
    wrapc = Alignment(wrap_text=True, vertical='center', horizontal='left')

    rs.cell(row=1, column=1, value='How Zone 2 reads your answers').font = TITLE
    rs.cell(row=2, column=1,
            value='Yellow rows on the first tab need review — every row with a negative Amount or a blank Company. '
                  'Fill in the columns on the right for each yellow row; Zone 2 then handles the adjustments and row splitting accordingly.').font = FONT
    rs.cell(row=2, column=1).alignment = Alignment(wrap_text=False, vertical='center', horizontal='left')

    r = 4
    hA = rs.cell(row=r, column=1, value='Your answer'); hA.font = RH; hA.fill = RHEAD
    hA.alignment = Alignment(horizontal='center', vertical='center')
    hB = rs.cell(row=r, column=2, value='What Zone 2 does to that row'); hB.font = RH; hB.fill = RHEAD
    hB.alignment = Alignment(horizontal='left', vertical='center')

    MAIN_F = InlineFont(rFont='Arial', sz=10, b=True, color='1F4E78')
    NOTE_F = InlineFont(rFont='Arial', sz=10, color='000000')
    PLAIN = InlineFont(rFont='Arial', sz=10)
    BOLD = InlineFont(rFont='Arial', sz=10, b=True)
    acenter = Alignment(wrap_text=True, horizontal='center', vertical='center')

    discount_b = CellRichText([
        TextBlock(PLAIN, '"-Fee" is added to the Company name so that it\'s treated as a cancellation fee, but '),
        TextBlock(BOLD, 'do not'),
        TextBlock(PLAIN, ' cancel out the order from TV.'),
    ])

    rules = [
        ('Cancellation Reason', None,
         'Notes in Column P which came from TV carry over automatically to this column. '
         'Manually fill in the blank rows as needed.'),
        ('Misc Company is filled in',
         "(if this column is filled in, don't fill in the other columns)",
         'This value replaces the Company.'),
        ('Chargeback Type = Cancelled Event',
         '(the full chargeback amount is a payout recoup with no cancellation fee)',
         'Nothing changes on the row.'),
        ('Chargeback Type = Discount',
         '(the full chargeback amount is a cancellation fee)',
         discount_b),
        ('Chargeback Type = Problem Order  +  Already Paid? = Yes',
         '(the chargeback amount is a payout recoup + cancellation fee)',
         'The row is split into two negative lines:\n'
         '   • Line 1 = the Payout amount, as a negative, with the original Company.\n'
         '   • Line 2 = the remainder, under the Company with "-Fee" added so that it\'s treated as a cancellation fee.\n'
         'These two lines add back to the original Amount.'),
        ('Chargeback Type = Problem Order  +  Already Paid? = No',
         '(the full chargeback amount is a cancellation fee)',
         '"-Fee" is added to the Company name so that it\'s treated as a cancellation fee.'),
        ('Chargeback Type = TradeDesk Fees',
         '(the full chargeback amount is Other Fees)',
         'Company is set to Other Fees. Do not fill in Not Found in the Misc Company column.'),
        ('Cancelled Out?', None,
         'Confirm the order is cancelled out from TV if necessary and put Yes.'),
    ]
    r = 5
    for main, note, does in rules:
        a = rs.cell(row=r, column=1)
        if note:
            a.value = CellRichText([TextBlock(MAIN_F, main + '\n'), TextBlock(NOTE_F, note)])
        else:
            a.value = main; a.font = SUB
        a.alignment = acenter
        b = rs.cell(row=r, column=2)
        if isinstance(does, CellRichText):
            b.value = does
        else:
            b.value = does; b.font = FONT
        b.alignment = wrapc
        if 'Already Paid? = Yes' in main:
            rs.row_dimensions[r].height = 62
        elif note:
            rs.row_dimensions[r].height = 30
        r += 1

    r += 1
    rs.cell(row=r, column=1, value='What blocks Zone 2 processing').font = Font(name='Arial', size=11, bold=True, color='9C0006')
    r += 1
    blocks = [
        'A yellow (flagged) row left with all answer columns blank.',
        "A Problem Order row with no 'Already Paid?' answer.",
        "A Problem Order or Cancelled Event row that doesn't have 'Cancelled Out?' = Yes.",
        'A Cancelled Event, Problem Order, or Discount row with a blank Cancellation Reason.',
    ]
    for b in blocks:
        c = rs.cell(row=r, column=1, value='•  ' + b)
        c.font = FONT; c.alignment = wrap
        rs.merge_cells(start_row=r, start_column=1, end_row=r, end_column=2)
        r += 1
    c = rs.cell(row=r, column=1, value='In each case Zone 2 stops and lists the exact rows to fix.')
    c.font = FONT; c.alignment = wrap
    rs.merge_cells(start_row=r, start_column=1, end_row=r, end_column=2)

    rs.column_dimensions['A'].width = 73
    rs.column_dimensions['B'].width = 124
    return rs
