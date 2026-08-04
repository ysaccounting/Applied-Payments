# Making it real: HAL sync + QuickBooks

Both are now driven from the UI. Open your app, sign in as admin, click
**Setup**.

## 1. HAL sync (Airtable)

**Prerequisite** — set in Railway Variables:

```
AIRTABLE_TOKEN=<personal access token>
HAL_SEASONS=25/26,26/27
```

Create the token at airtable.com/create/tokens with scopes
`data.records:read` and `schema.bases:read`, granted on the **Tickets** base.

Then: **Setup → Sync HAL now**.

It runs in the background — roughly 18,000 records over paged requests, a few
minutes. Reopen Setup to see the record count climb. When it's done the
"HAL not synced" banner disappears and expense categories become verified
against real season-ticket records instead of the merchant registry.

## 2. QuickBooks

**Prerequisites** — in Railway Variables:

```
QBO_CLIENT_ID=<Intuit production key>
QBO_CLIENT_SECRET=<Intuit production key>
QBO_ENVIRONMENT=production
QBO_ALLOWED_REALMS=<your company's realm id>
QBO_REDIRECT_URI=https://<your-domain>/qbo/callback
```

And in the Intuit developer portal, register that exact redirect URI under the
**Production** keys.

Then: **Setup → Connect QuickBooks** → approve at Intuit → you land back on
the callback with tokens stored.

### 3. Pull the open bills

Connecting doesn't fetch anything by itself. In **Setup**, click
**Pull open bills**.

That runs `SELECT * FROM Bill WHERE Balance > '0'` against every connected
company file, over the last 120 days, and fills the candidate pool. It runs in
the background; reopen Setup to see the count.

**Bills are never uploaded.** They come only from QuickBooks — that's the whole
reason the QBO connection exists. Charges come from card exports (upload or
scheduled pull); bills come from the ledger. The engine matches between them.

Only bills with a balance are pulled. Already-paid bills are excluded, and a
bill that gets paid later drops out of the pool on the next sync — otherwise
settled bills would keep being suggested as matches.

## Order matters

1. Upload charges → the queue populates
2. Sync HAL → expense categories become verified (TC/DEP)
3. Connect QBO + sync bills → bill-payment suggestions appear

Each step makes the previous one more useful. Nothing writes to your ledger at
any point — `DRY_RUN` and `QBO_WRITE_ENABLED` are still off, and the write
methods are unimplemented.

## What to look at once HAL is synced

The number this has been building toward: in Expenses/Refunds, how many charges
resolve to a live season-ticket holder versus falling to review as
`hal_no_active_record`. A high review count after a **full** sync is a real
finding worth investigating. (An earlier sample run showed a high count, but
that was mirror coverage — 192 of 13,666 records — not a real signal.)
