"""
Authentication and roles.

Two roles, deliberately simple:

    admin     — everything: manage users, connect QuickBooks, change settings,
                push to the ledger.
    reviewer  — the daily job: see queues, confirm matches, work exceptions.
                Cannot push, cannot connect accounts, cannot manage users.

Design notes that matter for a financial app:

  - Passwords are bcrypt-hashed. Plaintext or fast hashes (md5/sha) are not an
    option here.
  - Tokens are short-lived JWTs. There is no server-side session store, so
    logout is client-side; for revocation you'd add a token blocklist.
  - JWT_SECRET has no usable default. If it's unset the app refuses to issue
    tokens rather than signing with something guessable.
  - Every consequential action records WHO did it (see audit_log.actor).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
import bcrypt
from jose import JWTError, jwt
from sqlalchemy.orm import Session

from .config import settings
from .db import get_session
from .models_db import UserRow

log = logging.getLogger(__name__)

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login", auto_error=False)

ALGORITHM = "HS256"

# Four tiers, narrowing by what each can change:
#
#   owner       the QuickBooks connection and the company registry. These
#               decide which ledger the app writes to, so they stay with one
#               person -- everything else is delegated.
#   admin       everything operational, plus inviting users.
#   user        everything operational, but cannot invite users.
#   view_only   can see everything a user sees and change nothing.
#
ROLE_OWNER = "owner"
ROLE_ADMIN = "admin"
ROLE_USER = "user"
ROLE_VIEW = "view_only"
ROLE_REVIEWER = ROLE_USER          # old name, kept so existing rows still work
ROLES = {ROLE_ADMIN, ROLE_USER, ROLE_VIEW}    # assignable roles
ALL_ROLES = {ROLE_OWNER} | ROLES


# bcrypt operates on bytes and silently ignores anything past 72 bytes, so
# long passwords are truncated explicitly rather than raising at runtime.
_BCRYPT_MAX = 72


def _pw_bytes(plain: str) -> bytes:
    return plain.encode("utf-8")[:_BCRYPT_MAX]


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(_pw_bytes(plain), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(_pw_bytes(plain), hashed.encode())
    except (ValueError, TypeError):
        return False


def _secret() -> str:
    if not settings.jwt_secret:
        raise HTTPException(
            500, "JWT_SECRET is not configured — refusing to issue tokens")
    return settings.jwt_secret


def create_token(username: str, role: str) -> str:
    """Short-lived token, refreshed on every authenticated request.

    Expiry is the idle window, not the session length: an active person gets a
    fresh token on each call, while a browser left alone simply expires.
    """
    expire = datetime.utcnow() + timedelta(minutes=settings.session_idle_minutes)
    payload = {"sub": username, "role": role, "exp": expire,
               "iat": datetime.utcnow()}
    return jwt.encode(payload, _secret(), algorithm=ALGORITHM)


def hash_token(raw: str) -> str:
    """Reset links and 2FA codes are stored hashed, never in the clear."""
    import hashlib
    return hashlib.sha256(raw.encode()).hexdigest()


def new_code(digits: int = 6) -> str:
    import secrets as _s
    return "".join(_s.choice("0123456789") for _ in range(digits))


def valid_email(value: str) -> bool:
    import re as _re
    return bool(_re.match(r"^[^@\s]+@[^@\s]+\.[a-zA-Z]{2,}$", (value or "").strip()))


def authenticate(db: Session, username: str, password: str) -> UserRow | None:
    """Sign in by username, or by email if that's what was typed."""
    key = username.lower().strip()
    user = db.get(UserRow, key)
    if user is None:
        user = db.query(UserRow).filter(UserRow.email == key).first()
    if user is None or not user.is_active:
        return None
    if not verify_password(password, user.password_hash):
        return None
    user.last_login = datetime.utcnow()
    db.commit()
    return user


def current_user(token: str = Depends(oauth2_scheme),
                 db: Session = Depends(get_session)) -> UserRow:
    creds_error = HTTPException(
        status.HTTP_401_UNAUTHORIZED, "not authenticated",
        headers={"WWW-Authenticate": "Bearer"})
    if not token:
        raise creds_error
    try:
        payload = jwt.decode(token, _secret(), algorithms=[ALGORITHM])
        username = payload.get("sub")
    except JWTError:
        raise creds_error
    if not username:
        raise creds_error
    user = db.get(UserRow, username)
    if user is None or not user.is_active:
        raise creds_error
    return user


def refreshed_token(user: UserRow) -> str:
    """A new token extending the idle window from now."""
    return create_token(user.username, user.role)


def require_role(*roles: str):
    """Route dependency: reject anyone without one of these roles."""
    def _dep(user: UserRow = Depends(current_user)) -> UserRow:
        if user.role not in roles:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"requires role: {' or '.join(roles)} (you are '{user.role}')")
        return user
    return _dep


# Owner only: the QuickBooks connection and the company registry.
require_owner = require_role(ROLE_OWNER)

# Admin: everything operational plus user management.
require_admin = require_role(ROLE_OWNER, ROLE_ADMIN)

# Anyone who may change data -- view_only is deliberately excluded.
require_write = require_role(ROLE_OWNER, ROLE_ADMIN, ROLE_USER)

# Anyone signed in, including view_only.
require_reviewer = require_role(ROLE_OWNER, ROLE_ADMIN, ROLE_USER, ROLE_VIEW)
require_read = require_reviewer


def ensure_seed_admin(db: Session) -> None:
    """Create the first admin from env vars if no users exist at all."""
    if db.query(UserRow).count() > 0:
        return
    if not (settings.seed_admin_user and settings.seed_admin_password):
        log.warning("no users and no SEED_ADMIN_* configured — "
                    "nobody can log in yet")
        return
    # The first account is the owner.
    seed_name = settings.seed_admin_user.lower().strip()
    db.add(UserRow(
        username=seed_name,
        email=seed_name if valid_email(seed_name) else "",
        password_hash=hash_password(settings.seed_admin_password),
        role=ROLE_OWNER,
    ))
    db.commit()
    log.info("seeded owner account '%s'", settings.seed_admin_user)
