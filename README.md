# YS Reconciliation Engine — Deployable Service

The matching engine from the prototype, wrapped in the pieces a real deployment
needs: a database, an HTTP API, a scheduled ingestion worker, an audit trail,
and a **write-back boundary that cannot touch a live ledger** until two switches
are deliberately flipped.

It runs end-to-end today on SQLite with sample data. It is structured to deploy
to Railway (or any managed cloud) with Postgres, changing only configuration.

## What's here

```
app/
  config.py                env-driven settings + the two safety gates
  db.py                    SQLite locally / Postgres in prod (via DATABASE_URL)
  models_db.py             tables: companies, charges, bills, matches, audit_log
  persistence.py           bridge between DB rows and the engine's dataclasses
  api.py                   FastAPI: health, reconcile, results, audit, push
  worker.py                seed + scheduled daily ingestion/reconciliation
  integrations/
    quickbooks.py          the ONLY live-ledger boundary — reads, gated writes
engine/                    the matching core (unchanged from the prototype)
Dockerfile, railway.toml, requirements.txt, .env.example
```

## Run it locally

```bash
pip install -r requirements.txt
python -m app.worker                       # seed sample data + reconcile
uvicorn app.api:app --reload               # start the API on :8000
```

Then:

```bash
curl localhost:8000/health
curl -X POST "localhost:8000/reconcile/Front%20Row%20Brokers%20LLC"
curl "localhost:8000/results/Front%20Row%20Brokers%20LLC"
curl -X POST "localhost:8000/push/Front%20Row%20Brokers%20LLC"   # dry-run
curl "localhost:8000/audit/Front%20Row%20Brokers%20LLC"
```

## The safety gates (read this before anything touches real books)

Two independent switches, both defaulting to the safe position:

| Setting | Default | Effect |
|---|---|---|
| `DRY_RUN` | `true` | push records what it *would* do to the audit log, posts nothing |
| `QBO_WRITE_ENABLED` | `false` | a real write is impossible even with `DRY_RUN=false` |

A real write to QuickBooks requires `DRY_RUN=false` **and** `QBO_WRITE_ENABLED=true`
**and** the Intuit client wired in `integrations/quickbooks.py` (deliberately left
un-wired). `/health` reports `"safe": true` whenever a live write is impossible.

This is the boundary I flagged as needing an experienced review before it goes
live. The scaffolding is built so everything *around* that boundary can be
developed and tested in full safety first.

## Deploying to Railway

1. Push this repo; Railway builds from the `Dockerfile`.
2. Add a Postgres plugin — Railway injects `DATABASE_URL`, and the app uses it
   automatically (no code change).
3. Set env vars from `.env.example` (keep `DRY_RUN=true` until you're ready).
4. Add a second scheduled service for daily ingestion (see the commented cron in
   `railway.toml`): `python -m app.worker`.

## What's intentionally NOT wired yet

- **QuickBooks write-back.** `post_bill_payment()` / `overwrite_bill_amount()`
  raise `NotImplementedError` and sit behind two gates. Reads are live.
- **Scheduled API pulls** for Slash/Divvy — the hook exists (`pull_source()`),
  the live endpoints and credentials don't. Manual upload works today.
- **Token revocation and password reset.** A JWT stays valid until it expires.
- **The dashboard frontend.** It talks to this API; see the standalone
  `review.html` mockup for the intended shape.

## Connecting QuickBooks (OAuth)

**Before you connect**, set the fail-closed realm allowlist in `.env`:

```
QBO_CLIENT_ID=...            # from your Intuit app
QBO_CLIENT_SECRET=...
QBO_ENVIRONMENT=sandbox      # or production
QBO_REDIRECT_URI=http://localhost:8000/qbo/callback
QBO_ALLOWED_REALMS=<your TEST company realm id>
```

`QBO_ALLOWED_REALMS` is the guard that stops the app ever touching an
unintended company file. It **fails closed**: empty means nothing is
authorized. The realm ID must also be registered as a redirect URI in the
Intuit developer portal, exactly matching `QBO_REDIRECT_URI`.

Then:

1. `uvicorn app.api:app --reload`
2. Open `http://localhost:8000/qbo/connect` in a browser → Intuit login → approve
3. You land back on `/qbo/callback` and tokens are stored
4. Check `http://localhost:8000/qbo/status`

### Token lifecycle (handled for you)

- Access tokens last ~1 hour and refresh automatically, 2 minutes early, so a
  long batch can't expire mid-run.
- Refresh tokens last ~100 days and **rotate on every refresh** — the new one is
  persisted in the same transaction. `/qbo/status` shows `days_until_reauth`;
  when it hits zero someone must re-run `/qbo/connect`.
- `POST /qbo/disconnect/{realm_id}` revokes at Intuit and deletes locally.

Connecting grants **read** access only in practice — the write gates
(`DRY_RUN`, `QBO_WRITE_ENABLED`) are independent and still default to safe.

## Reading bills from QuickBooks

Once connected:

```
curl "localhost:8000/qbo/company/<realm_id>"                    # confirm the connection
curl -X POST "localhost:8000/qbo/sync-bills/Y%26S%20Tickets?realm_id=<realm_id>&days=120"
curl -X POST "localhost:8000/reconcile/Y%26S%20Tickets"
```

`sync-bills` runs `SELECT * FROM Bill WHERE Balance > '0'` (paged, bounded by
`days`) and replaces the local candidate pool. Bills that have since been paid
drop out automatically — otherwise settled bills keep getting suggested.

Each bill's `PrivateNote` is parsed into event / buyer email / order number /
source account. On 1,939 real Y&S bills: email present on 94%, order-like
number on 12%, source account on 98%. `SyncToken` is captured because any
later bill update (the TicketVault rounding overwrite) requires it.

## Users and roles

Two roles:

| Role | Can |
|------|-----|
| `reviewer` | see queues and results, work exceptions, upload charge files, view audit |
| `admin` | everything above, plus manage users, connect QuickBooks, sync bills, and push to the ledger |

**Every endpoint requires a login.** Unauthenticated requests get 401.

Set up the first admin in `.env`:

```
JWT_SECRET=<generate: python -c "import secrets;print(secrets.token_urlsafe(48))">
SEED_ADMIN_USER=josh
SEED_ADMIN_PASSWORD=<a long one>
```

The seed admin is created on startup **only if no users exist**. After that,
create people via the API:

```bash
TOKEN=$(curl -s -X POST localhost:8000/auth/login \
  -d "username=josh&password=..." | jq -r .access_token)

curl -X POST localhost:8000/auth/users -H "Authorization: Bearer $TOKEN" \
  -d "username=rivka&password=<12+ chars>&role=reviewer"
```

Passwords are bcrypt-hashed; minimum 12 characters. Tokens are JWTs valid for
`JWT_EXPIRE_MINUTES` (default one working day). There's no server-side session
store, so revoking a token before expiry would need a blocklist — worth adding
before this is public-facing.

## Getting charges in

Two paths, same destination, both idempotent.

**Manual upload** — what your team can use today:

```bash
curl -X POST localhost:8000/charges/upload -H "Authorization: Bearer $TOKEN" \
  -F "source=slash" -F "company=Y&S Tickets" -F "file=@slash_july.csv"
```

Verified on the real July exports: Slash 1,561 charges, Divvy 1,180, WEX 401.
Re-uploading the same file adds **zero** — charges are keyed on
`{source}:{transaction_id}` using each portal's own ID (Slash `Id`, Divvy
`Transaction ID`). WEX has no ID column, so a content hash plus an occurrence
counter is used — WEX exports genuinely contain identical repeated rows, and
those are separate real charges.

`GET /charges/uploads` shows the history: what was loaded, when, by whom, and
how much was new versus duplicate.

**Scheduled pulls** — Slash and Divvy both expose APIs; `pull_source()` in
`ingestion.py` is the hook, not yet wired to live endpoints (each needs its own
credential). WEX stays upload-only. `GET /charges/sources` reports which is
which.

## The reviewer UI

Served at the root of the app — just open `https://<your-domain>/` and log in.

It's a single-page frontend (`app/static/index.html`) that talks to this API:

- **Sign in** with the same credentials as the API; the token is held in
  `sessionStorage` and cleared on sign-out.
- **Bill Payments** tab — each charge with its ranked candidate bills, scored,
  with the reasons each candidate matched.
- **Expenses/Refunds** tab — TC / DEP coding with the rule that fired and the
  HAL status behind it.
- **Upload charges** button — drop a WEX/Divvy/Slash CSV straight in; the
  queue refreshes when it finishes.
- Filter by source and confidence, or search.

Two things it shows honestly rather than hiding:

- If **HAL isn't synced**, a banner says expense categories are unverified.
- If **no bills are loaded** (QuickBooks not connected yet), every charge shows
  as "no candidate" — because there is genuinely nothing to match against.

The action buttons (Approve, Match, Move to…) are not wired yet. They're the
next build step, and they're the point at which the UI starts writing rather
than reading.
