"""
Lightweight schema migration.

SQLAlchemy's create_all() creates missing *tables* but never alters existing
ones. So adding a column to a model works on a fresh database and breaks a
deployed one with an opaque "column does not exist" error at query time -- which
surfaces as a 500 on a page that used to work.

This compares each model against the live table and issues ALTER TABLE ADD
COLUMN for anything missing. That covers the only schema change this project
actually makes (adding nullable columns with defaults). It deliberately does
NOT drop, rename or retype anything: those are destructive and should be done
deliberately, with a backup, not automatically on boot.

If this project grows to need real migrations, replace it with Alembic.
"""

from __future__ import annotations

import logging

from sqlalchemy import inspect, text

from .db import engine
from .models_db import Base

log = logging.getLogger(__name__)

# SQLAlchemy type -> DDL, per dialect.
_TYPE_SQL = {
    "postgresql": {
        "VARCHAR": "VARCHAR", "TEXT": "TEXT", "INTEGER": "INTEGER",
        "BOOLEAN": "BOOLEAN", "DATETIME": "TIMESTAMP", "DATE": "DATE",
        "FLOAT": "DOUBLE PRECISION", "NUMERIC": "NUMERIC(12,2)",
    },
    "sqlite": {
        "VARCHAR": "TEXT", "TEXT": "TEXT", "INTEGER": "INTEGER",
        "BOOLEAN": "BOOLEAN", "DATETIME": "DATETIME", "DATE": "DATE",
        "FLOAT": "REAL", "NUMERIC": "NUMERIC",
    },
}


def _ddl_type(col, dialect: str) -> str:
    base = type(col.type).__name__.upper()
    mapping = _TYPE_SQL.get(dialect, _TYPE_SQL["sqlite"])
    for key, sql in mapping.items():
        if base.startswith(key):
            return sql
    return "TEXT"


def _default_clause(col) -> str:
    """A safe default so existing rows get a sensible value, not NULL."""
    d = col.default
    if d is None or d.is_callable:
        return ""
    val = d.arg
    if isinstance(val, bool):
        return f" DEFAULT {'TRUE' if val else 'FALSE'}"
    if isinstance(val, (int, float)):
        return f" DEFAULT {val}"
    if isinstance(val, str):
        escaped = val.replace("'", "''")
        return f" DEFAULT '{escaped}'"
    return ""


def migrate() -> list[str]:
    """Add any columns present in the models but missing from the database."""
    dialect = engine.dialect.name
    inspector = inspect(engine)
    applied: list[str] = []

    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not inspector.has_table(table.name):
                continue                      # create_all will handle it
            existing = {c["name"] for c in inspector.get_columns(table.name)}
            for col in table.columns:
                if col.name in existing:
                    continue
                ddl = (f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" '
                       f'{_ddl_type(col, dialect)}{_default_clause(col)}')
                try:
                    conn.execute(text(ddl))
                    applied.append(f"{table.name}.{col.name}")
                    log.info("migrated: added %s.%s", table.name, col.name)
                except Exception as e:
                    # "already exists" is fine -- another path added it first.
                    if "already exists" in str(e).lower() or "duplicate" in str(e).lower():
                        log.info("column %s.%s already present", table.name, col.name)
                    else:
                        log.error("migration failed for %s.%s: %s",
                                  table.name, col.name, e)

    if applied:
        log.info("schema migration added %d column(s): %s",
                 len(applied), ", ".join(applied))
    return applied
