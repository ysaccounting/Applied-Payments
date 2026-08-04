"""
Token lifecycle: store, refresh-on-demand, and the realm allowlist.

`get_access_token()` is the single entry point every QBO API call goes through.
It refreshes transparently when the access token is near expiry and persists the
rotated refresh token in the same transaction.
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy.orm import Session

from ..config import settings
from ..models_db import QboTokenRow
from . import qbo_oauth

log = logging.getLogger(__name__)


class RealmNotAllowed(RuntimeError):
    pass


class NotConnected(RuntimeError):
    pass


def allowed_realms() -> set[str]:
    return {r.strip() for r in settings.qbo_allowed_realms.split(",") if r.strip()}


def assert_realm_allowed(realm_id: str) -> None:
    """Hard guard against touching an unintended company file.

    An empty allowlist means nothing is authorized -- deliberately fail closed
    rather than defaulting to 'any realm'.
    """
    allowed = allowed_realms()
    if not allowed:
        raise RealmNotAllowed(
            "QBO_ALLOWED_REALMS is empty — no company file is authorized. "
            "Set it to your test company's realm ID.")
    if realm_id not in allowed:
        raise RealmNotAllowed(
            f"realm {realm_id} is not in QBO_ALLOWED_REALMS — refusing to proceed")


def save_tokens(db: Session, tokens: dict, label: str = "") -> QboTokenRow:
    assert_realm_allowed(tokens["realm_id"])
    row = QboTokenRow(
        realm_id=tokens["realm_id"],
        access_token=tokens["access_token"],
        refresh_token=tokens["refresh_token"],
        access_expires_at=tokens["access_expires_at"],
        refresh_expires_at=tokens["refresh_expires_at"],
        label=label,
        updated_at=datetime.utcnow(),
    )
    db.merge(row)
    db.commit()
    log.info("stored QBO tokens for realm %s", tokens["realm_id"])
    return row


def get_access_token(db: Session, realm_id: str) -> str:
    """Return a valid access token, refreshing if needed."""
    assert_realm_allowed(realm_id)
    row = db.get(QboTokenRow, realm_id)
    if row is None:
        raise NotConnected(f"realm {realm_id} is not connected — run the OAuth flow")

    now = datetime.utcnow()
    if row.refresh_expires_at <= now:
        raise NotConnected(
            f"refresh token for realm {realm_id} expired on "
            f"{row.refresh_expires_at:%Y-%m-%d} — reauthorize")

    if row.access_expires_at > now:
        return row.access_token

    log.info("access token expired for realm %s — refreshing", realm_id)
    fresh = qbo_oauth.refresh(row.refresh_token, realm_id)
    # The rotated refresh token MUST be persisted or the connection breaks.
    row.access_token = fresh["access_token"]
    row.refresh_token = fresh["refresh_token"]
    row.access_expires_at = fresh["access_expires_at"]
    row.refresh_expires_at = fresh["refresh_expires_at"]
    row.updated_at = now
    db.commit()
    return row.access_token


def connection_status(db: Session) -> list[dict]:
    out = []
    now = datetime.utcnow()
    for row in db.query(QboTokenRow).all():
        out.append({
            "realm_id": row.realm_id,
            "label": row.label,
            "access_valid": row.access_expires_at > now,
            "refresh_expires": row.refresh_expires_at.isoformat(),
            "days_until_reauth": (row.refresh_expires_at - now).days,
        })
    return out
