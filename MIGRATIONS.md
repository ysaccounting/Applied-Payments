# Database migrations

Alembic owns the schema. Migrations run automatically on startup, so a normal
deploy needs nothing extra.

## Changing the schema

1. Edit the model in `app/models_db.py`
2. Generate a migration:
   ```bash
   alembic revision --autogenerate -m "what changed"
   ```
3. **Read the generated file** in `migrations/versions/`. Autogenerate is a
   draft, not an authority — it misreads renames as drop+add, which loses data.
4. Commit it. The next deploy applies it on startup.

To apply locally: `alembic upgrade head`. To inspect: `alembic current`,
`alembic history`.

## How startup handles an existing database

`app/alembic_boot.py` covers three cases:

| Situation | What happens |
|---|---|
| Fresh database | migrations run from zero |
| Already stamped | `upgrade head` — the normal case |
| Existing tables, no stamp | reconcile columns in place, then `stamp head` |

That third case is the deployed database as it stood before Alembic existed:
tables built by `create_all()`, some columns added by hand. Running the baseline
there would try to CREATE tables that already exist and fail, so it adopts the
database as-is instead of replaying history over live data.

If Alembic fails for any reason, startup falls back to `create_all()` plus the
column reconciler in `app/migrate.py`, so the app boots rather than 500ing.

## The non-nullable column trap

Autogenerate emits new columns as `nullable=False` **with no default**. Postgres
rejects that on a table that already has rows, and the migration rolls back —
sometimes quietly, if you only read the "Running upgrade" line.

`migrations/env.py` fixes this automatically: a `process_revision_directives`
hook copies the model's Python default into a `server_default` so existing rows
get a real value. If a column has no usable default, it's made nullable rather
than inventing one.

You still get the trap if you hand-write a migration. When adding a
non-nullable column to a populated table, always give it a `server_default`.

## Local SQLite quirk

SQLite can't `ALTER` most things, so `env.py` turns on batch mode (which
rewrites the table) for SQLite only. DDL there isn't fully transactional, so a
partly-applied migration is possible locally. Postgres — what runs in
production — applies DDL transactionally, so a failed migration rolls back
cleanly.
