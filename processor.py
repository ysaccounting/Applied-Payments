import pandas as pd
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter
import re
from datetime import datetime
import os

# ── Company mapping (static) ──────────────────────────────────────────────────
COMPANY_MAPPING = {
    "Bearhawk - Aaron": "Affiliates", "Bearhawk - Aaron-Fee": "Affiliates",
    "Bearhawk - Chris": "Affiliates", "Bearhawk - Chris-Fee": "Affiliates",
    "Bearhawk - Dylan": "Affiliates", "Bearhawk - Dylan-Fee": "Affiliates",
    "Bearhawk - Gray": "Affiliates", "Bearhawk - Gray-Fee": "Affiliates",
    "Bearhawk - Jason": "Affiliates", "Bearhawk - Jason-Fee": "Affiliates",
    "Bearhawk Group": "Affiliates", "Bearhawk Group-Fee": "Affiliates",
    "Best Tix": "Affiliates", "Best Tix-Fee": "Affiliates",
    "Damon and Crew": "Affiliates", "Damon and Crew-Fee": "Affiliates",
    "GK LLC": "Affiliates", "GK LLC-Fee": "Affiliates",
    "Jacks YS": "Affiliates", "Jacks YS-Fee": "Affiliates",
    "Levovitz": "Affiliates", "Levovitz-Fee": "Affiliates",
    "Needle Tickets LLC": "Affiliates", "Needle Tickets LLC-Fee": "Affiliates",
    "Pollak Tickets": "Affiliates", "Pollak Tickets-Fee": "Affiliates",
    "Ticketwonders LLC": "Affiliates", "Ticketwonders LLC-Fee": "Affiliates",
    "Yoni Levine": "Affiliates", "Yoni Levine-Fee": "Affiliates",
    "YourTickets": "Affiliates", "YourTickets-Fee": "Affiliates",
    "YS Katz": "Affiliates", "YS Katz-Fee": "Affiliates",
    "YS TL": "Affiliates", "YS TL-Fee": "Affiliates",
    "YSA": "Affiliates", "YSA-Fee": "Affiliates",
    "YSA 2": "Affiliates", "YSA 2-Fee": "Affiliates",
    "YSA 3": "Affiliates", "YSA 3-Fee": "Affiliates",
    "YSM Tickets": "Affiliates", "YSM Tickets-Fee": "Affiliates",
    "YSS Tickets": "Affiliates", "YSS Tickets-Fee": "Affiliates",
    "YSW": "Affiliates", "YSW-Fee": "Affiliates",
    "Due from/to Ticket Vault": "Other",
    "Due from/to TickPick": "Other",
    "Isaac Knopf": "Other",
    "JL": "Other",
    "Not Found": "Other",
    "Other Fees": "Other",
    "The Ticket Guy": "Other", "The Ticket Guy-Fee": "Other",
    "Upside LLC": "Other",
    "YS Tickets-Fee": "Y&S - Deposit",
    "YS Tickets Spec-Fee": "Y&S - Deposit",
    "YS-Seatgeek-Fee": "Y&S - Deposit",
    "YS-Seatgeek2-Fee": "Y&S - Deposit",
    "YS Tickets": "Y&S - RecPmt",
    "YS Tickets Spec": "Y&S - RecPmt",
    "YS-Seatgeek": "Y&S - RecPmt",
    "YS-Seatgeek2": "Y&S - RecPmt",
    "StubHub Loan": "Y&S - StubHub",
}

YSA_VARIANTS = {"YSA", "YSA 2", "YSA 3"}

HEADER_FILL = PatternFill("solid", start_color="375623", end_color="375623")
HEADER_FONT = Font(name="Arial", bold=True, color="FFFFFF", size=10)
SECTION_FONT = Font(name="Arial", bold=True, size=10)
DATA_FONT = Font(name="Arial", size=10)
ALIGN_CENTER = Alignment(horizontal="center", vertical="center")
ALIGN_LEFT = Alignment(horizontal="left", vertical="center")

DATA_COLS = [
    "Company", "Date", "Network", "Type", "Order#", "Amount",
    "Performer", "Venue", "EventDate", "Section", "Row", "Seat", "Qty", "Reason"
]

COL_WIDTHS = {
    "Company": 20, "Date": 12, "Network": 14, "Type": 18,
    "Order#": 14, "Amount": 12, "Performer": 25, "Venue": 30,
    "EventDate": 22, "Section": 10, "Row": 6, "Seat": 10, "Qty": 6, "Reason": 25,
}


def parse_filename(filename):
    """Extract network and remittance date from filename like YS_Stubhub_5-5-26.csv"""
    base = os.path.splitext(filename)[0]
    parts = base.split("_")
    network_raw = parts[1]
    date_parts = parts[2].split("-")
    remit_date = datetime(2000 + int(date_parts[2]), int(date_parts[0]), int(date_parts[1]))
    return network_raw, remit_date


def build_row(r, remit_date_str, network):
    company_raw = str(r["Company"]).strip() if pd.notna(r["Company"]) else ""
    is_fee = company_raw.endswith("-Fee")
    is_cancellation = company_raw == "Cancellation Fees"
    amount = r["Amount"]

    if is_fee or is_cancellation:
        type_val = "Cancellation Fees"
    elif amount >= 0:
        type_val = "Payment"
    else:
        type_val = "Recoup"

    company_out = re.sub(r"-Fee$", "", company_raw)
    if company_out in YSA_VARIANTS:
        company_out = "YSA"
    if company_out.startswith("Bearhawk"):
        company_out = "Bearhawk Group"

    category = COMPANY_MAPPING.get(company_raw, "Unknown")

    return {
        "Company": company_out,
        "Date": remit_date_str,
        "Network": network,
        "Type": type_val,
        "Order#": int(r["Order#"]) if pd.notna(r["Order#"]) else "",
        "Amount": amount,
        "Performer": r["Performer"] if pd.notna(r["Performer"]) else "",
        "Venue": r["Venue"] if pd.notna(r["Venue"]) else "",
        "EventDate": r["EventDate"] if pd.notna(r["EventDate"]) else "",
        "Section": r["Section"] if pd.notna(r["Section"]) else "",
        "Row": r["Row"] if pd.notna(r["Row"]) else "",
        "Seat": r["Seat"] if pd.notna(r["Seat"]) else "",
        "Qty": r["Qty"] if pd.notna(r["Qty"]) else "",
        "Reason": r["Reason"] if "Reason" in r.index and pd.notna(r["Reason"]) else "",
        "_category": category,
    }


def write_header_row(ws, row_num, headers):
    for col_idx, h in enumerate(headers, 1):
        cell = ws.cell(row=row_num, column=col_idx, value=h)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = ALIGN_CENTER


def write_data_cell(ws, row, col, value, fmt=None, align=ALIGN_LEFT):
    cell = ws.cell(row=row, column=col, value=value)
    cell.font = DATA_FONT
    cell.alignment = align
    if fmt:
        cell.number_format = fmt
    return cell


def style_data_tab(ws, df):
    for col_idx, col_name in enumerate(DATA_COLS, 1):
        cell = ws.cell(row=1, column=col_idx, value=col_name)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = ALIGN_CENTER

    for row_idx, row in df.iterrows():
        for col_idx, col_name in enumerate(DATA_COLS, 1):
            val = row[col_name]
            cell = ws.cell(row=row_idx + 2, column=col_idx, value=val)
            cell.font = DATA_FONT
            if col_name == "Amount":
                cell.number_format = "#,##0.00"
                cell.alignment = ALIGN_CENTER
            elif col_name in ("Date", "Network", "Type", "Order#", "EventDate", "Section", "Row", "Seat", "Qty"):
                cell.alignment = ALIGN_CENTER
            else:
                cell.alignment = ALIGN_LEFT

    for col_idx, col_name in enumerate(DATA_COLS, 1):
        ws.column_dimensions[get_column_letter(col_idx)].width = COL_WIDTHS.get(col_name, 15)

    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(DATA_COLS))}1"


def process(csv_path, filename):
    raw = pd.read_csv(csv_path)
    network_raw, remit_date = parse_filename(filename)
    remit_date_str = remit_date.strftime("%m/%d/%Y")
    network = f"{network_raw} (C)"
    memo = os.path.splitext(filename)[0]

    rows = [build_row(r, remit_date_str, network) for _, r in raw.iterrows()]
    df_out = pd.DataFrame(rows)

    ys_df = df_out[df_out["_category"].isin(["Y&S - Deposit", "Y&S - RecPmt"])].copy()
    affiliates_df = df_out[df_out["_category"] == "Affiliates"].copy()
    stubhub_df = df_out[df_out["_category"] == "Y&S - StubHub"].copy()
    other_df = df_out[df_out["_category"] == "Other"].copy()

    # ── Entry #1: Receive Payment ─────────────────────────────────────────────
    ys_payments = ys_df[ys_df["Type"] == "Payment"]["Amount"].sum()
    ys_recoups = ys_df[ys_df["Type"] == "Recoup"]["Amount"].sum()
    receive_payment_amt = round(ys_payments + ys_recoups, 2)

    # ── Entry #2: Bank Deposit ────────────────────────────────────────────────
    aff_grouped = affiliates_df.groupby("Company")["Amount"].sum().reset_index()
    aff_grouped.columns = ["Account", "Amount"]
    sh_grouped = stubhub_df.groupby("Company")["Amount"].sum().reset_index()
    sh_grouped.columns = ["Account", "Amount"]
    other_grouped = other_df.groupby("Company")["Amount"].sum().reset_index()
    other_grouped.columns = ["Account", "Amount"]
    ys_cancel_amt = round(ys_df[ys_df["Type"] == "Cancellation Fees"]["Amount"].sum(), 2)
    cancel_row = pd.DataFrame([{"Account": "Cancellation Fees", "Amount": ys_cancel_amt}])

    deposit_rows = pd.concat([aff_grouped, sh_grouped, other_grouped, cancel_row], ignore_index=True)
    deposit_rows["Amount"] = deposit_rows["Amount"].round(2)
    deposit_rows["Network"] = network
    deposit_rows["Date"] = remit_date_str
    deposit_rows["Deposit #"] = memo
    deposit_rows["Bank Account"] = "FFB Chkg"

    bank_deposit_total = round(deposit_rows["Amount"].sum(), 2)
    combined_total = round(receive_payment_amt + bank_deposit_total, 2)

    # ── Build Applied Payments workbook ───────────────────────────────────────
    wb1 = openpyxl.Workbook()
    wb1.remove(wb1.active)

    # Summary tab
    ws_sum = wb1.create_sheet("Summary")
    ws_sum.cell(row=1, column=1, value="Receive Payment").font = SECTION_FONT
    write_header_row(ws_sum, 2, ["Memo", "Amount"])
    write_data_cell(ws_sum, 3, 1, memo)
    write_data_cell(ws_sum, 3, 2, receive_payment_amt, fmt="#,##0.00", align=ALIGN_CENTER)
    ws_sum.cell(row=5, column=1, value="Bank Deposit").font = SECTION_FONT
    write_header_row(ws_sum, 6, ["Account", "Amount", "Network", "Date", "Deposit #", "Bank Account"])
    for i, row in deposit_rows.iterrows():
        r = 7 + i
        write_data_cell(ws_sum, r, 1, row["Account"])
        write_data_cell(ws_sum, r, 2, row["Amount"], fmt="#,##0.00", align=ALIGN_CENTER)
        write_data_cell(ws_sum, r, 3, row["Network"], align=ALIGN_CENTER)
        write_data_cell(ws_sum, r, 4, row["Date"], align=ALIGN_CENTER)
        write_data_cell(ws_sum, r, 5, row["Deposit #"])
        write_data_cell(ws_sum, r, 6, row["Bank Account"], align=ALIGN_CENTER)
    for col_idx, w in enumerate([28, 14, 14, 12, 28, 14], 1):
        ws_sum.column_dimensions[get_column_letter(col_idx)].width = w

    # Data tabs
    tab_data = {
        "Y&S": ys_df[DATA_COLS].reset_index(drop=True),
        "Affiliates": affiliates_df[DATA_COLS].reset_index(drop=True),
        "StubHub Loan": stubhub_df[DATA_COLS].reset_index(drop=True),
        "Other": other_df[DATA_COLS].reset_index(drop=True),
    }
    for tab_name, df in tab_data.items():
        ws = wb1.create_sheet(tab_name)
        style_data_tab(ws, df)

    # ── Build Bank Deposit workbook ───────────────────────────────────────────
    bd_cols = ["Account", "Amount", "Network", "Date", "Deposit #", "Bank Account"]
    bd_col_widths = [28, 14, 14, 12, 28, 14]

    wb2 = openpyxl.Workbook()
    ws_bd = wb2.active
    ws_bd.title = "Bank Deposit"
    write_header_row(ws_bd, 1, bd_cols)
    for i, row in deposit_rows.iterrows():
        r = 2 + i
        write_data_cell(ws_bd, r, 1, row["Account"])
        write_data_cell(ws_bd, r, 2, row["Amount"], fmt="#,##0.00", align=ALIGN_CENTER)
        write_data_cell(ws_bd, r, 3, row["Network"], align=ALIGN_CENTER)
        write_data_cell(ws_bd, r, 4, row["Date"], align=ALIGN_CENTER)
        write_data_cell(ws_bd, r, 5, row["Deposit #"])
        write_data_cell(ws_bd, r, 6, row["Bank Account"], align=ALIGN_CENTER)
    for col_idx, w in enumerate(bd_col_widths, 1):
        ws_bd.column_dimensions[get_column_letter(col_idx)].width = w
    ws_bd.freeze_panes = "A2"
    ws_bd.auto_filter.ref = f"A1:{get_column_letter(len(bd_cols))}1"

    return {
        "wb_applied": wb1,
        "wb_deposit": wb2,
        "memo": memo,
        "receive_payment_amt": receive_payment_amt,
        "bank_deposit_total": bank_deposit_total,
        "combined_total": combined_total,
    }
