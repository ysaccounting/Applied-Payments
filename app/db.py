"""
Database wiring. SQLite for local dev, Postgres in production — the same code,
switched only by DATABASE_URL. That's why nothing above hardcodes a driver.
"""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from .config import settings
from .models_db import Base

def _normalize(url: str) -> str:
    """Railway (and Heroku) inject DATABASE_URL as `postgres://`, a legacy
    scheme SQLAlchemy 2.x refuses. Rewrite it to the driver-qualified form or
    the app dies on boot with an opaque error."""
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+psycopg2://", 1)
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg2://", 1)
    return url


DATABASE_URL = _normalize(settings.database_url)

_connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
_kwargs = {} if DATABASE_URL.startswith("sqlite") else {
    # Managed Postgres drops idle connections; recycle before that happens.
    "pool_pre_ping": True, "pool_recycle": 300,
}
engine = create_engine(DATABASE_URL, connect_args=_connect_args,
                       future=True, **_kwargs)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def init_db() -> None:
    """Bring the schema to head.

    Alembic owns the schema now. The bootstrap handles the three cases: a fresh
    database, one already under Alembic, and the deployed one that predates it.
    If Alembic can't run for any reason, fall back to create_all + the column
    reconciler so the app still boots rather than 500ing on every request.
    """
    try:
        from .alembic_boot import run as _alembic_run
        _alembic_run()
    except Exception:
        import logging
        logging.getLogger(__name__).exception(
            "alembic bootstrap failed — falling back to create_all")
        Base.metadata.create_all(engine)
        from .migrate import migrate
        migrate()


def get_session():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
