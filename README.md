# Remittance Processor

Processes TicketVault remittance CSV files (StubHub, etc.) into formatted Excel output files ready for QBO entry.

## What it does

1. User uploads a remittance CSV (e.g. `YS_Stubhub_5-5-26.csv`)
2. User enters the cash received amount
3. App processes the file and generates two output files:
   - **`[filename] Applied Payments.xlsx`** — Summary tab (Receive Payment + Bank Deposit entries) + Y&S, Affiliates, StubHub Loan, Other tabs
   - **`[filename] Bank Deposit.xlsx`** — QBO bank deposit import file
4. App verifies that cash received matches the combined total of all entries

## Running locally

```bash
pip install -r requirements.txt
python app.py
```

Visit `http://localhost:5000`

## Deploying to Railway

1. Push this repo to GitHub
2. Create a new Railway project → Deploy from GitHub repo
3. Railway auto-detects the Procfile and deploys

No environment variables required.

## Updating the company mapping

The company mapping is hardcoded in `processor.py` in the `COMPANY_MAPPING` dict. To update it, edit that dict and redeploy.

## File naming convention

Input files must follow the format: `YS_<Network>_<M>-<D>-<YY>.csv`

Example: `YS_Stubhub_5-5-26.csv`

- Network is parsed from the filename (e.g. `Stubhub` → `Stubhub (C)`)
- Remittance date is parsed from the filename (e.g. `5-5-26` → `05/05/2026`)
