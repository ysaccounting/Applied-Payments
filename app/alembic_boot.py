"""
Alembic bootstrap.

Three situations have to be handled, and getting this wrong on a live database
is expensive:

  1. **Fresh database** — no tables at all. Run migrations from zero.

  2. **Already under Alembic** — an alembic_version table exists. Upgrade to
     head, the normal case from here on.

  3. **Existing database, never seen Alembic** — this is the deployed one right
     now: it has tables created by create_all(), some missing columns added by
     the interim migrate.py, and no version stamp. Running the baseline
     migration here would try to CREATE tables that already exist and fail.

     So: reconcile the schema in place (add any missing columns), then STAMP it
     as being at head without running the migration. From the next change
     onward it's ordinary Alembic.

The stamp is the important bit. It says "this database already looks like the
baseline" rather than trying to replay history over live data.
"""

from __future__ import annotations

import logging
import os

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import inspect

from .db import engine

log = logging.getLogger(__name__)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALEMBIC_INI = os.path.join(ROOT, "alembic.ini")


def _config() -> Config:
    cfg = Config(ALEMBIC_INI)
    cfg.set_main_option("script_location", os.path.join(ROOT, "migrations"))
    return cfg


def current_revision() -> str | None:
    with engine.connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()


def has_any_tables() -> bool:
    names = set(inspect(engine).get_table_names()) - {"alembic_version"}
    return bool(names)


def _known_revisions(cfg) -> set[str]:
    """Every revision id the migration files on disk define."""
    from alembic.script import ScriptDirectory
    try:
        return {s.revision for s in ScriptDirectory.from_config(cfg).walk_revisions()}
    except Exception:                      # noqa: BLE001
        return set()


def _forget_version() -> None:
    """Drop the stamp so Alembic can be told where things stand again.

    Only used when the recorded revision names a migration that no longer
    exists on disk. Alembic then can't reason about the database at all: it
    can't upgrade (no path from an unknown id) and it can't even stamp (that
    resolves the current revision too), so every startup falls through to
    create_all -- which makes missing TABLES but never missing COLUMNS, and the
    app starts and then 500s on the first query that needs one.

    Dropping the row loses no schema. The reconcile that follows adds whatever
    columns the models expect, and the re-stamp records the real head.
    """
    from sqlalchemy import text
    with engine.begin() as conn:
        # Wait a few seconds for the lock, then give up.
        #
        # Without this the statement blocks indefinitely behind any open
        # transaction on the table -- an abandoned session in a SQL console is
        # enough -- and startup hangs with no error at all, which reads as a
        # healthcheck timeout and tells you nothing about the cause.
        try:
            conn.execute(text("SET LOCAL lock_timeout = '5s'"))
            conn.execute(text("SET LOCAL statement_timeout = '10s'"))
        except Exception:                   # noqa: BLE001
            pass                            # SQLite and friends have neither
        conn.execute(text("DELETE FROM alembic_version"))
    log.warning("alembic: cleared an unrecognised version stamp")


def _reconcile_and_stamp(cfg) -> None:
    """Make the live schema match the models, then declare it at head.

    Used when a migration can't be replayed -- typically because the column it
    adds is already there, left by the interim auto-migrator that predated
    Alembic. Replaying would fail on "column already exists" and take the whole
    upgrade with it, so instead the schema is reconciled directly and stamped.
    """
    from .models_db import Base
    from .migrate import migrate

    Base.metadata.create_all(engine)
    added = migrate()
    command.stamp(cfg, "head")
    log.info("alembic: reconciled schema (%d column(s) added) and stamped head",
             len(added))


def run() -> str:
    """Bring the database to head. Returns which path was taken."""
    cfg = _config()
    log.info("alembic: starting bootstrap")

    current = current_revision()

    # A stamp naming a migration that isn't on disk. Usually a deploy that went
    # backwards: the database ran a migration the running code no longer has.
    if current is not None and current not in _known_revisions(cfg):
        log.warning("alembic: database is stamped %s, which no migration "
                    "defines -- reconciling against the models instead", current)
        _forget_version()
        _reconcile_and_stamp(cfg)
        log.info("alembic: recovery complete")
        return "recovered"

    if current is not None:
        try:
            command.upgrade(cfg, "head")
            log.info("alembic: upgraded to head")
            return "upgraded"
        except Exception:
            # A half-applied schema shouldn't take the app down. Reconcile
            # against the models and re-stamp rather than leaving every
            # request 500ing on a missing or duplicated column.
            log.exception("alembic upgrade failed — reconciling schema instead")
            _reconcile_and_stamp(cfg)
            return "reconciled"

    if not has_any_tables():
        command.upgrade(cfg, "head")
        log.info("alembic: fresh database migrated to head")
        return "fresh"

    # Pre-existing database with no version stamp: adopt it as-is.
    _reconcile_and_stamp(cfg)
    return "adopted"
