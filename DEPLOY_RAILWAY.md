# Deploying to Railway

Deploying puts this on the public internet. Read the security note at the
bottom before you invite anyone else in.

---

## 1. Push to a Git repo

Railway deploys from Git. From the `ys_reconciliation/` folder:

```bash
git init
git add .
git commit -m "reconciliation service"
git remote add origin <your repo>
git push -u origin main
```

**Make sure `.env` is not committed.** Only `.env.example` belongs in Git.
Add a `.gitignore` with at least:

```
.env
*.db
__pycache__/
*.pyc
```

## 2. Create the Railway project

1. railway.app → **New Project** → **Deploy from GitHub repo**
2. Pick the repo. Railway reads `railway.toml` and builds from the `Dockerfile`.
3. First build will fail to boot until env vars are set — expected.

## 3. Add Postgres

**+ New** → **Database** → **PostgreSQL**. Railway injects `DATABASE_URL` into
your service automatically. The app rewrites the legacy `postgres://` scheme
(which SQLAlchemy 2.x rejects) on boot, so nothing else is needed.

Tables are created on startup. There are no migrations yet — a schema change
later means either a manual `ALTER` or adding Alembic.

## 4. Set environment variables

Service → **Variables**:

```
JWT_SECRET=<python -c "import secrets;print(secrets.token_urlsafe(48))">
SEED_ADMIN_USER=josh
SEED_ADMIN_PASSWORD=<long, and change it after first login>
APP_ENV=production

AIRTABLE_TOKEN=<your Airtable PAT>
HAL_SEASONS=25/26,26/27

QBO_CLIENT_ID=<from Intuit>
QBO_CLIENT_SECRET=<from Intuit>
QBO_ENVIRONMENT=production
QBO_ALLOWED_REALMS=<your test company realm id>
QBO_REDIRECT_URI=https://<your-app>.up.railway.app/qbo/callback

DRY_RUN=true
QBO_WRITE_ENABLED=false
```

Leave `DATABASE_URL` alone — Railway sets it.

### The redirect URI will bite you

`QBO_REDIRECT_URI` must **exactly** match what's registered in the Intuit
developer portal — scheme, host, path, no trailing slash. Your Railway domain
isn't `localhost`, so:

1. Generate a domain: service → **Settings** → **Networking** → **Generate Domain**
2. Register `https://<that-domain>/qbo/callback` in the Intuit portal
3. Set `QBO_REDIRECT_URI` to the same string

Mismatch produces an unhelpful Intuit error. It's the most common failure here.

## 5. Verify

```bash
curl https://<your-app>.up.railway.app/health
```

Expect `{"status":"ok", "dry_run":true, "qbo_write_enabled":false, "safe":true}`.

`/health` is deliberately unauthenticated so Railway's healthcheck can reach it.
Everything else returns 401 without a token — verify that:

```bash
curl -i https://<your-app>.up.railway.app/results/test    # expect 401
```

Then log in and change the seed password.

## 6. Add the scheduled jobs

Two cron services in the same project, pointed at the same repo:

| Schedule | Start command | Does |
|---|---|---|
| `0 */4 * * *` | `python -m app.worker --sync-hal` | refresh the HAL mirror |
| `0 9 * * *` | `python -m app.worker` | daily reconciliation run |

HAL gets its own cadence because your team edits it through the day, and a
stale mirror causes false "no live record" flags.

Each needs the same `DATABASE_URL` and `AIRTABLE_TOKEN`. In Railway, reference
the shared variables rather than re-typing them.

## 7. Connect QuickBooks

Open `https://<your-app>.up.railway.app/qbo/connect` in a browser while logged
in as admin. After approving, check `/qbo/status`.

---

## Before other people use this

Deploying makes it internet-facing. What's in place: every endpoint requires a
login, passwords are bcrypt-hashed, roles are enforced, the realm allowlist
fails closed, and the ledger write path is gated **and** unimplemented. Railway
terminates TLS, so traffic is encrypted.

What isn't:

- **No token revocation.** A JWT stays valid until it expires (default 8 hours).
  If someone leaves or a token leaks, deactivating the user stops new logins but
  doesn't kill the existing token.
- **No password reset flow.** An admin has to create a replacement user.
- **No rate limiting** on `/auth/login` — brute-force is unthrottled.
- **No audit of reads.** Writes are audited; who *looked* at what is not.

None of that blocks a pilot with a few trusted people on your own team. All of
it matters before this holds live financial data for 15 entities. This is the
layer I'd want reviewed by an experienced engineer before it goes wide — not
because the approach is wrong, but because auth over other people's financial
records is where quiet mistakes are expensive.

**Keep `DRY_RUN=true` and `QBO_WRITE_ENABLED=false` until you have watched a
dry-run push plan do exactly what you expect, against the test company.**
