"""
HTTP API. The dashboard (separate frontend) talks to these endpoints; the
scheduled worker calls the same reconciliation path directly.

Endpoints:
  GET  /health                  liveness + which safety gates are set
  POST /reconcile/{company}     run matching for a company, store results
  GET  /results/{company}       the day's results, grouped by decision
  GET  /audit/{company}         the audit trail
  POST /push/{company}          push approved matches (DRY-RUN by default)
"""

from __future__ import annotations

from collections import defaultdict

from datetime import datetime, timedelta

from fastapi import (BackgroundTasks, Depends, FastAPI, File, Form,
                     HTTPException, Query, UploadFile)
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .db import init_db, get_session
from .models_db import MatchRow, AuditRow, OAuthStateRow
from .persistence import run_reconciliation
from .models_db import UserRow, UploadRow
from . import auth as _auth
from .auth import (current_user, require_admin, require_reviewer, require_owner,
                   require_write, ROLES, ROLE_USER, ROLE_OWNER)
from .ingestion import ingest_csv, SOURCES, PULLABLE
from .integrations.quickbooks import push_company
from .integrations import qbo_oauth, qbo_tokens

import pathlib

import logging

log = logging.getLogger("ys.api")

app = FastAPI(title="YS Reconciliation Engine", version="0.1.0")

STATIC = pathlib.Path(__file__).parent / "static"


@app.get("/", include_in_schema=False)
def index():
    """The reviewer UI. Auth happens client-side against /auth/login."""
    return FileResponse(STATIC / "index.html")


def _seed_profile_emails(db) -> None:
    """Fill the cardholder-email mirror if it is empty -- in the BACKGROUND.

    It is what lets a card's PROFILE NAME identify the buyer on a bill, and it
    only populated during the hourly HAL sync, so between a deploy and the next
    sync the email signal was dead and every candidate scored the same.

    On a thread, because it is a paged HTTP fetch over the whole address book.
    Run inline it delayed the startup event, and the server does not answer
    /health until that returns -- so the healthcheck timed out and Railway
    marked a perfectly good deploy as failed. Nothing else waits on this: an
    empty mirror costs match quality, not correctness.
    """
    from .models_db import ProfileEmailRow
    if not settings.airtable_token:
        return
    try:
        if db.query(ProfileEmailRow).first() is not None:
            return
    except Exception:                           # noqa: BLE001
        return                                  # table not there yet; the hourly job will

    def _fill() -> None:
        try:
            from .worker import sync_profile_emails
            from .review import reset_email_cache
            n = sync_profile_emails()
            reset_email_cache()
            log.info("seeded %d cardholder addresses in the background", n)
        except Exception as e:                  # noqa: BLE001
            log.warning("could not seed cardholder emails: %s", e)

    import threading
    threading.Thread(target=_fill, name="seed-emails", daemon=True).start()


@app.on_event("startup")
def _startup():
    init_db()
    from .db import SessionLocal
    db = SessionLocal()
    try:
        _auth.ensure_seed_admin(db)
        _seed_profile_emails(db)
    finally:
        db.close()


# --- auth -------------------------------------------------------------------

def _issue_twofa(db: Session, user: UserRow) -> None:
    """Email a fresh sign-in code, invalidating any earlier one."""
    from .models_db import AuthTokenRow
    from . import mailer
    db.query(AuthTokenRow).filter(
        AuthTokenRow.username == user.username,
        AuthTokenRow.kind == "twofa",
        AuthTokenRow.used_at.is_(None)).delete(synchronize_session=False)
    code = _auth.new_code()
    db.add(AuthTokenRow(
        username=user.username, kind="twofa",
        token_hash=_auth.hash_token(code),
        expires_at=datetime.utcnow() + timedelta(minutes=settings.twofa_code_minutes)))
    db.commit()
    target = user.email or user.username
    mailer.send_twofa(target, code, settings.twofa_code_minutes)


def _device_trusted(db: Session, user: UserRow, device: str) -> bool:
    from .models_db import TrustedDeviceRow
    if not device:
        return False
    row = db.get(TrustedDeviceRow, _auth.hash_token(device))
    return bool(row and row.username == user.username
                and row.expires_at > datetime.utcnow())


@app.post("/auth/login")
def login(form: OAuth2PasswordRequestForm = Depends(),
          device: str = Form(""),
          db: Session = Depends(get_session)):
    """Password check, then 2FA unless this browser was trusted recently."""
    user = _auth.authenticate(db, form.username, form.password)
    if user is None:
        raise HTTPException(401, "incorrect username or password")

    needs_2fa = (settings.require_twofa and user.twofa_enabled
                 and not _device_trusted(db, user, device))
    if needs_2fa:
        _issue_twofa(db, user)
        from . import mailer
        return {"twofa_required": True, "username": user.username,
                "email_sent": mailer.is_configured(),
                "note": ("a code was emailed to you" if mailer.is_configured()
                         else "email isn't configured — the code is in the "
                              "server logs")}

    return {"access_token": _auth.create_token(user.username, user.role),
            "token_type": "bearer", "role": user.role,
            "idle_minutes": settings.session_idle_minutes}


@app.post("/auth/verify-2fa")
def verify_twofa(username: str = Form(...), code: str = Form(...),
                 trust_device: bool = Form(True),
                 db: Session = Depends(get_session)):
    """Exchange an emailed code for a session."""
    from .models_db import AuthTokenRow, TrustedDeviceRow
    import secrets as _s

    username = username.lower().strip()
    row = db.query(AuthTokenRow).filter(
        AuthTokenRow.username == username, AuthTokenRow.kind == "twofa",
        AuthTokenRow.used_at.is_(None)).order_by(
        AuthTokenRow.created_at.desc()).first()

    if row is None or row.expires_at <= datetime.utcnow():
        raise HTTPException(400, "that code has expired — sign in again")
    # Rate-limit guessing: six digits is only 10^6, so unlimited tries is weak.
    if row.attempts >= 5:
        raise HTTPException(429, "too many attempts — sign in again")
    row.attempts += 1
    db.commit()

    if row.token_hash != _auth.hash_token(code.strip()):
        raise HTTPException(400, "that code isn't right")

    row.used_at = datetime.utcnow()
    user = db.get(UserRow, username)
    if user is None or not user.is_active:
        raise HTTPException(401, "account unavailable")

    device_token = ""
    if trust_device:
        device_token = _s.token_urlsafe(32)
        db.add(TrustedDeviceRow(
            id=_auth.hash_token(device_token), username=username,
            expires_at=datetime.utcnow() + timedelta(hours=settings.twofa_trust_hours)))
    db.commit()

    return {"access_token": _auth.create_token(user.username, user.role),
            "token_type": "bearer", "role": user.role,
            "device": device_token,
            "trust_hours": settings.twofa_trust_hours,
            "idle_minutes": settings.session_idle_minutes}


@app.post("/auth/forgot")
def forgot_password(username: str = Form(...),
                    db: Session = Depends(get_session)):
    """Email a reset link.

    Always reports success. Saying "no such account" would let anyone probe
    which email addresses exist.
    """
    from .models_db import AuthTokenRow
    from . import mailer
    import secrets as _s

    username = username.lower().strip()
    user = db.get(UserRow, username)
    if user is None:
        user = db.query(UserRow).filter(UserRow.email == username).first()
    if user is not None and user.is_active:
        username = user.username
        token = _s.token_urlsafe(32)
        db.add(AuthTokenRow(
            username=username, kind="reset",
            token_hash=_auth.hash_token(token),
            expires_at=datetime.utcnow() + timedelta(minutes=settings.reset_token_minutes)))
        db.commit()
        base = settings.app_base_url.rstrip("/") or ""
        link = f"{base}/?reset={token}&u={username}"
        mailer.send_reset(user.email or user.username, link,
                          settings.reset_token_minutes)
    return {"sent": True,
            "note": "if that account exists, a reset link has been emailed"}


@app.post("/auth/reset")
def reset_password(username: str = Form(...), token: str = Form(...),
                   new_password: str = Form(...),
                   db: Session = Depends(get_session)):
    from .models_db import AuthTokenRow, TrustedDeviceRow
    username = username.lower().strip()
    if len(new_password) < 12:
        raise HTTPException(400, "password must be at least 12 characters")

    row = db.query(AuthTokenRow).filter(
        AuthTokenRow.username == username, AuthTokenRow.kind == "reset",
        AuthTokenRow.token_hash == _auth.hash_token(token),
        AuthTokenRow.used_at.is_(None)).first()
    if row is None or row.expires_at <= datetime.utcnow():
        raise HTTPException(400, "that reset link has expired or been used")

    user = db.get(UserRow, username)
    if user is None:
        raise HTTPException(404, "no such account")
    user.password_hash = _auth.hash_password(new_password)
    row.used_at = datetime.utcnow()
    # A reset usually means "someone else may have had access" -- drop trusted
    # devices so 2FA is required again everywhere.
    db.query(TrustedDeviceRow).filter(
        TrustedDeviceRow.username == username).delete(synchronize_session=False)
    db.commit()
    return {"reset": True}


@app.get("/auth/me")
def me(user: UserRow = Depends(current_user)):
    """Also refreshes the session — the client calls this periodically, which
    is what makes the 30-minute window slide with activity."""
    # Deliberately no email here: it's set by an admin when inviting and is
    # never shown to or edited by the person themselves.
    return {"username": user.username, "full_name": user.full_name,
            "role": user.role, "last_login": user.last_login,
            "access_token": _auth.refreshed_token(user),
            "idle_minutes": settings.session_idle_minutes}


@app.post("/auth/users")
def create_user(username: str = Form(...), email: str = Form(...),
                password: str = Form(""),
                full_name: str = Form(""), role: str = Form(ROLE_USER),
                send_invite: bool = Form(True),
                db: Session = Depends(get_session),
                admin: UserRow = Depends(require_admin)):
    """Add a user. The username IS their email address.

    With no password given, a random one is set and an invite emailed — so an
    admin never handles someone else's password.
    """
    from .models_db import AuthTokenRow
    from . import mailer
    import secrets as _s

    if role not in ROLES:
        raise HTTPException(400, f"role must be one of {sorted(ROLES)}")
    username = username.lower().strip()
    email = email.lower().strip()
    if not username:
        raise HTTPException(400, "username is required")
    if not _auth.valid_email(email):
        raise HTTPException(
            400, "a valid email address is required — it's where sign-in codes "
                 "and password resets are sent")
    if db.get(UserRow, username):
        raise HTTPException(409, "that username is taken")
    if db.query(UserRow).filter(UserRow.email == email).first():
        raise HTTPException(409, "that email is already on another account")
    if password and len(password) < 12:
        raise HTTPException(400, "password must be at least 12 characters")

    pw = password or _s.token_urlsafe(24)
    db.add(UserRow(username=username, email=email, full_name=full_name.strip(),
                   password_hash=_auth.hash_password(pw), role=role))
    db.commit()

    invited = False
    if send_invite:
        token = _s.token_urlsafe(32)
        db.add(AuthTokenRow(
            username=username, kind="reset",
            token_hash=_auth.hash_token(token),
            expires_at=datetime.utcnow() + timedelta(hours=48)))
        db.commit()
        base = settings.app_base_url.rstrip("/") or ""
        invited = mailer.send(
            email, "You've been added to Y&S Reconciliation",
            f"{admin.username} set up an account for you.\n\n"
            f"Choose your password here (valid 48 hours):\n"
            f"{base}/?reset={token}&u={username}\n")

    return {"created": username, "role": role, "by": admin.username,
            "invite_emailed": invited,
            "password_set_manually": bool(password)}


@app.get("/auth/users")
def list_users(db: Session = Depends(get_session),
               admin: UserRow = Depends(require_admin)):
    return [{"username": u.username, "email": u.email, "role": u.role,
             "active": u.is_active, "last_login": u.last_login}
            for u in db.query(UserRow).all()]


@app.post("/auth/users/{username}/deactivate")
def deactivate_user(username: str, db: Session = Depends(get_session),
                    admin: UserRow = Depends(require_admin)):
    u = db.get(UserRow, username.lower().strip())
    if u is None:
        raise HTTPException(404, "no such user")
    if u.username == admin.username:
        raise HTTPException(400, "cannot deactivate yourself")
    u.is_active = False
    db.commit()
    return {"deactivated": u.username}


# --- charge ingestion -------------------------------------------------------

@app.post("/charges/upload")
async def upload_charges(source: str = Form(...), company: str = Form(...),
                         file: UploadFile = File(...),
                         override_name_check: bool = Form(False),
                         preflight: bool = Form(False),
                         db: Session = Depends(get_session),
                         user: UserRow = Depends(require_write)):
    """Upload a card-portal CSV export. Idempotent: re-uploads are deduped."""
    # Registered card accounts, not a hardcoded three. The layout comes from
    # the account's file_format, so a program can be called anything.
    from .models_db import SourceAccountRow as _SA
    acct = db.query(_SA).filter(_SA.company == company,
                                _SA.source == source.lower()).first()
    if acct is None and source.lower() not in SOURCES:
        raise HTTPException(
            400, f"'{source}' is not a card account for {company}. "
                 f"Add it under Cards first.")
    # Only an explicitly configured format is passed through; otherwise the
    # layout is read from the file's own columns. Falling back to the source
    # name here is what made "amexcredit" look like a file format.
    _fmt = (acct.file_format if acct else "") or ""

    # The company must be one the admin registered. A typed name that differs
    # by so much as a space would file the charges where nothing can find them.
    from .models_db import CompanyRow
    known = db.query(CompanyRow).filter(CompanyRow.name == company).first()
    if known is None:
        registered = [c.name for c in db.query(CompanyRow).filter(
            CompanyRow.active.is_(True)).all()]
        raise HTTPException(
            400,
            f"'{company}' is not a registered company. "
            f"Pick one of: {registered or 'none set up yet — an admin must add one'}")

    content = await file.read()
    try:
        result = ingest_csv(db, source, content, company,
                            filename=file.filename or "", actor=user.username,
                            override_name_check=override_name_check,
                            preflight=preflight, file_format=_fmt)
    except ValueError as e:
        # Record rejected uploads too -- a file that was refused is exactly the
        # kind of thing someone asks about later.
        from .models_db import AuditRow
        db.add(AuditRow(
            company=company, action="upload_rejected",
            detail=f"{file.filename or '(no name)'} as {source}: {e}",
            dry_run=True, actor=user.username))
        db.commit()
        raise HTTPException(400, str(e))

    if preflight:
        return result           # nothing imported, nothing to record

    from .models_db import AuditRow
    db.add(AuditRow(
        company=company, action="charges_uploaded",
        detail=(f"{result['filename'] or '(no name)'} as {source}: "
                f"{result['new']} new, {result['duplicates']} already loaded, "
                f"{result['skipped']} skipped, of {result['rows_seen']} rows"),
        dry_run=True, actor=user.username))
    db.commit()
    return result


@app.get("/charges/uploads")
def upload_history(company: str | None = None,
                   db: Session = Depends(get_session),
                   user: UserRow = Depends(require_reviewer)):
    q = db.query(UploadRow).order_by(UploadRow.at.desc())
    if company:
        q = q.filter(UploadRow.company == company)
    return [{"at": u.at, "source": u.source, "method": u.method,
             "file": u.filename, "company": u.company, "seen": u.rows_seen,
             "rescored": _rescore_after_upload(db, company),
             "new": u.rows_new, "duplicate": u.rows_duplicate,
             "by": u.actor} for u in q.limit(50).all()]


@app.get("/charges/sources")
def charge_sources(user: UserRow = Depends(require_reviewer)):
    return {"sources": list(SOURCES), "api_pull_supported": sorted(PULLABLE),
            "upload_only": sorted(set(SOURCES) - PULLABLE)}


SLOW_REQUEST_MS = 1500


@app.middleware("http")
async def log_slow_requests(request, call_next):
    """Log anything slower than SLOW_REQUEST_MS, with a timing header always.

    The threshold that matters for this app is queue latency creeping past
    about 1.5s. Without this you only learn about it when somebody complains,
    by which point it has been getting worse for weeks.
    """
    import time as _time
    started = _time.perf_counter()
    response = await call_next(request)
    ms = (_time.perf_counter() - started) * 1000
    response.headers["X-Response-Time-ms"] = f"{ms:.0f}"
    if ms >= SLOW_REQUEST_MS:
        log.warning("SLOW %s %s -> %d in %.0fms", request.method,
                    request.url.path, response.status_code, ms)
    return response


@app.middleware("http")
async def sliding_session(request, call_next):
    """Extend the session on any authenticated request.

    Relying on a timer to call /auth/me meant a throttled background tab could
    miss its refresh and the token would expire mid-session -- which looked
    like being signed out at random while working.
    """
    response = await call_next(request)
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer ") and response.status_code < 400:
        from jose import JWTError, jwt as _jwt
        try:
            payload = _jwt.decode(auth[7:], _auth._secret(),
                                  algorithms=[_auth.ALGORITHM])
            fresh = _auth.create_token(payload["sub"], payload.get("role", ""))
            response.headers["X-Refresh-Token"] = fresh
        except (JWTError, KeyError, Exception):
            pass
    return response


@app.exception_handler(Exception)
async def unhandled(request, exc):
    """Return the actual error instead of a bare 500.

    A generic "Internal Server Error" in the UI means digging through deploy
    logs to learn anything. The type and message are enough to act on and
    contain no secrets; the traceback stays in the logs.
    """
    from fastapi.responses import JSONResponse
    import logging as _log
    _log.getLogger(__name__).exception("unhandled error on %s", request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": f"{type(exc).__name__}: {exc}"[:500],
                 "path": str(request.url.path)})


@app.get("/health")
def health():
    return {
        "status": "ok",
        "env": settings.app_env,
        "dry_run": settings.dry_run,
        "qbo_write_enabled": settings.qbo_write_enabled,
        "safe": settings.dry_run or not settings.qbo_write_enabled,
    }


# --- learned rules ----------------------------------------------------------

@app.get("/rules")
def list_rules(company: str = Query("Y&S Tickets"),
               db: Session = Depends(get_session),
               user: UserRow = Depends(require_reviewer)):
    """Everything the app has learned, and which rules act on their own."""
    from .models_db import LearnedRuleRow
    from .learning import AUTO_THRESHOLD
    rows = db.query(LearnedRuleRow).filter(
        LearnedRuleRow.company == company).order_by(
        LearnedRuleRow.confirmations.desc()).all()
    return {
        "company": company,
        "threshold": AUTO_THRESHOLD,
        "auto_count": sum(1 for r in rows if r.auto_apply),
        "rules": [{
            "id": r.id, "merchant": r.sample_merchant or r.merchant_key,
            "key": r.merchant_key, "category": r.category, "vendor": r.vendor,
            "confirmations": r.confirmations, "disagreements": r.disagreements,
            "auto_apply": r.auto_apply, "by": r.last_actor,
            "updated": r.updated_at,
        } for r in rows],
    }


@app.post("/rules/{rule_id}/auto")
def set_rule_auto(rule_id: str, auto: bool = Form(...),
                  db: Session = Depends(get_session),
                  user: UserRow = Depends(require_write)):
    """Turn auto-apply on or off for one rule."""
    from .models_db import LearnedRuleRow
    r = db.get(LearnedRuleRow, rule_id)
    if r is None:
        raise HTTPException(404, "no such rule")
    r.auto_apply = auto
    db.commit()
    return {"id": rule_id, "auto_apply": r.auto_apply}


@app.delete("/rules/{rule_id}")
def delete_rule(rule_id: str, db: Session = Depends(get_session),
                user: UserRow = Depends(require_write)):
    from .models_db import LearnedRuleRow
    r = db.get(LearnedRuleRow, rule_id)
    if r is None:
        raise HTTPException(404, "no such rule")
    db.delete(r); db.commit()
    return {"deleted": rule_id}


@app.post("/rules/apply")
def apply_rules(company: str = Form("Y&S Tickets"),
                db: Session = Depends(get_session),
                user: UserRow = Depends(require_write)):
    """Code everything matching a trusted rule.

    Charges move to Categorized for a human to glance at — nothing is posted.
    """
    from .learning import apply_auto_rules
    return apply_auto_rules(db, company)


# --- unified queue ----------------------------------------------------------

@app.get("/queue/{company}")
def queue(company: str, status: str = Query("for_review"),
          card: str | None = Query(None), source: str | None = Query(None),
          limit: int = Query(200), offset: int = Query(0),
          date_from: str = Query(""), date_to: str = Query(""),
          amt_min: str = Query(""), amt_max: str = Query(""),
          vendor: str = Query(""), profile: str = Query(""),
          last4: str = Query(""), memo: str = Query(""),
          details: str = Query(""), txn_type: str = Query(""),
          category: str = Query(""), user_f: str = Query("", alias="user"),
          ts_from: str = Query(""), ts_to: str = Query(""),
          currency: str = Query(""),
          kinds: str = Query(""), cvendor: str = Query(""),
          tier: str = Query(""), matchable: str = Query(""),
          q: str = Query(""), sort: str = Query("date"),
          direction: str = Query("desc"),
          db: Session = Depends(get_session),
          user: UserRow = Depends(require_reviewer)):
    from datetime import date as _date
    from decimal import Decimal as _D, InvalidOperation

    def _d(v):
        try:
            return _date.fromisoformat(v) if v else None
        except ValueError:
            return None

    def _m(v):
        try:
            return _D(v) if v not in ("", None) else None
        except (InvalidOperation, TypeError):
            return None

    from .review import build_queue
    return build_queue(db, company, status=status, card=card, source=source,
                       limit=limit, offset=offset,
                       date_from=_d(date_from), date_to=_d(date_to),
                       amt_min=_m(amt_min), amt_max=_m(amt_max),
                       vendor=vendor or None, profile=profile or None,
                       last4=last4 or None, memo=memo or None,
                       details=details or None, txn_type=txn_type or None,
                       category=category or None, user=user_f or None,
                       ts_from=_dt_from(ts_from), ts_to=_dt_to(ts_to),
                       currency=currency or None,
                       kinds=kinds or None, cvendor=cvendor or None,
                       tier=tier or None, matchable=matchable or None,
                       q=q or None, sort=sort, direction=direction)


@app.get("/bills/{company}")
def list_bills(company: str, limit: int = Query(200), offset: int = Query(0),
               q: str = Query(""), vendor: str = Query(""), memo: str = Query(""),
               date_from: str = Query(""), date_to: str = Query(""),
               amt_min: str = Query(""), amt_max: str = Query(""),
               bal_min: str = Query(""), bal_max: str = Query(""),
               sort: str = Query("date"),
               direction: str = Query("desc"),
               db: Session = Depends(get_session),
               user: UserRow = Depends(require_reviewer)):
    """Open bills available to match against, as rows.

    Same shape as the charge queue so the UI can render it with the same table.
    """
    from sqlalchemy import or_ as _or
    from .models_db import BillRow

    from datetime import date as _date
    from decimal import Decimal as _D, InvalidOperation

    def _d(v):
        try:
            return _date.fromisoformat(v) if v else None
        except ValueError:
            return None

    def _m(v):
        try:
            return _D(v) if v not in ("", None) else None
        except (InvalidOperation, TypeError):
            return None

    # Open bills only, matching what the matcher treats as a candidate. A bill
    # paid inside the app is drawn down immediately, so it leaves this tab at
    # the same moment it stops being suggested -- the count in the tab header
    # and the candidates in the queue can't disagree.
    qy = db.query(BillRow).filter(BillRow.company == company,
                                  BillRow.balance > 0)
    # `q` stays as the bill-number / catch-all box; the rest are the separate
    # fields reviewers actually think in -- a combined search matches none of
    # them well.
    if q:
        like = f"%{q}%"
        qy = qy.filter(_or(BillRow.vendor.ilike(like), BillRow.memo.ilike(like),
                           BillRow.doc_number.ilike(like),
                           BillRow.email.ilike(like)))
    if vendor:
        qy = qy.filter(BillRow.vendor.ilike(f"%{vendor}%"))
    if memo:
        qy = qy.filter(BillRow.memo.ilike(f"%{memo}%"))
    _df, _dt = _d(date_from), _d(date_to)
    if _df:
        qy = qy.filter(BillRow.txn_date >= _df)
    if _dt:
        qy = qy.filter(BillRow.txn_date <= _dt)
    _amin, _amax = _m(amt_min), _m(amt_max)
    if _amin is not None:
        qy = qy.filter(BillRow.amount >= _amin)
    if _amax is not None:
        qy = qy.filter(BillRow.amount <= _amax)
    # Balance is what's still owed, which is what makes a bill a candidate at
    # all -- worth filtering separately from the original amount.
    _bmin, _bmax = _m(bal_min), _m(bal_max)
    if _bmin is not None:
        qy = qy.filter(BillRow.balance >= _bmin)
    if _bmax is not None:
        qy = qy.filter(BillRow.balance <= _bmax)
    total = qy.count()

    col = {"date": BillRow.txn_date, "amount": BillRow.amount,
           "vendor": BillRow.vendor, "bill": BillRow.doc_number,
           "balance": BillRow.balance}.get(sort, BillRow.txn_date)
    qy = qy.order_by(col.desc() if direction != "asc" else col.asc())
    rows = qy.offset(offset).limit(limit).all() if limit else qy.offset(offset).all()

    return {"company": company, "total": total, "offset": offset, "limit": limit,
            "bills": [{
                "bill": b.doc_number or b.bill_id, "bill_id": b.bill_id,
                "amt": str(b.amount), "balance": str(b.balance),
                "date": str(b.txn_date), "vendor": b.vendor or "",
                "email": b.email or "", "memo": (b.memo or "")[:120],
                "quantity": b.quantity, "lines": b.line_count,
            } for b in rows]}


@app.get("/prefs/{key}")
def get_pref(key: str, db: Session = Depends(get_session),
             user: UserRow = Depends(require_reviewer)):
    """One preference blob for the signed-in user. Absent is not an error."""
    from .models_db import UserPrefRow
    row = db.query(UserPrefRow).filter(UserPrefRow.username == user.username,
                                       UserPrefRow.key == key).first()
    return {"key": key, "value": row.value if row else ""}


@app.post("/prefs/{key}")
def set_pref(key: str, value: str = Form(...),
             db: Session = Depends(get_session),
             user: UserRow = Depends(require_reviewer)):
    """Save a preference blob. Available to view-only users on purpose --
    changing which columns YOU see isn't a write to anyone's data."""
    from .models_db import UserPrefRow
    row = db.query(UserPrefRow).filter(UserPrefRow.username == user.username,
                                       UserPrefRow.key == key).first()
    if row is None:
        row = UserPrefRow(username=user.username, key=key)
        db.add(row)
    row.value = value[:20000]
    row.updated_at = datetime.utcnow()
    db.commit()
    return {"key": key, "saved": True}


@app.get("/queue/{company}/counts")
def queue_count(company: str, card: str | None = Query(None),
                status: str = Query("for_review"),
                txn_type: str = Query(""), kinds: str = Query(""),
                date_from: str = Query(""), date_to: str = Query(""),
                amt_min: str = Query(""), amt_max: str = Query(""),
                vendor: str = Query(""), profile: str = Query(""),
                last4: str = Query(""), memo: str = Query(""),
                cvendor: str = Query(""), q: str = Query(""),
                db: Session = Depends(get_session),
                user: UserRow = Depends(require_reviewer)):
    from datetime import date as _date
    from decimal import Decimal as _D, InvalidOperation

    def _d(v):
        try:
            return _date.fromisoformat(v) if v else None
        except ValueError:
            return None

    def _m(v):
        try:
            return _D(v) if v not in ("", None) else None
        except (InvalidOperation, TypeError):
            return None

    from .review import queue_counts
    return queue_counts(db, company, card=card, filters=dict(
        status=status or None, txn_type=txn_type or None, kinds=kinds or None,
        date_from=_d(date_from), date_to=_d(date_to),
        amt_min=_m(amt_min), amt_max=_m(amt_max),
        vendor=vendor or None, profile=profile or None, last4=last4 or None,
        memo=memo or None, cvendor=cvendor or None, q=q or None))


def _rescore_after_upload(db: Session, company: str) -> int:
    """New charges arrive with no strength; give them one before they're seen."""
    from .review import refresh_scores
    try:
        return refresh_scores(db, company)
    except Exception:
        return 0     # a scoring hiccup must not fail the upload itself


# --- restored: these four routes were removed by accident when the counts
# --- endpoint was rewritten, and the UI never stopped calling them.

@app.post("/queue/purge")
def purge_excluded(company: str = Form(...), ids: str = Form(""),
                   db: Session = Depends(get_session),
                   admin: UserRow = Depends(require_write)):
    """Permanently delete excluded charges. Admin only, irreversible.

    Excluded means the charge does not belong in the ledger, so by definition
    there is no QuickBooks transaction behind it -- excluding a posted charge is
    refused, and undoing one deletes its transaction first. Deleting from here
    therefore never touches QuickBooks and never asks about it.

    A handful of rows predate that rule and still carry a transaction id. They
    are deleted like any other, with the id recorded in the audit entry so the
    reference isn't lost silently.
    """
    from .models_db import ChargeRow, AuditRow

    q = db.query(ChargeRow).filter(ChargeRow.company == company,
                                   ChargeRow.status == "excluded")
    id_list = [i for i in ids.split(",") if i.strip()]
    if id_list:
        q = q.filter(ChargeRow.charge_id.in_(id_list))
    rows = q.all()

    # Counted for the audit note only. Nothing here calls QuickBooks: an
    # excluded charge has no transaction behind it, so there is nothing to
    # delete there and nothing to ask about.
    stale = [r for r in rows if r.qbo_txn_id]

    deleted = 0
    for r in rows:
        note = f"${r.amount} {r.txn_date} {r.merchant[:60]}"
        if r.qbo_txn_id:
            # Predates the rule that a posted charge can't be excluded. Noted
            # rather than blocked, so the id survives in the log.
            note += f" (carried a stale {r.qbo_txn_type} id {r.qbo_txn_id})"
        db.add(AuditRow(company=company, action="charge_purged",
                        charge_id=r.charge_id, detail=note,
                        dry_run=False, actor=admin.username))
        db.delete(r)
        deleted += 1
        if deleted % 300 == 0:
            db.commit()
    db.commit()
    return {"deleted": deleted, "company": company,
            "stale_refs": len(stale)}


@app.get("/audit/log/{company}")
def audit_log(company: str, limit: int = Query(200), offset: int = Query(0),
              actor: str = Query(""), action: str = Query(""),
              date_from: str = Query(""), date_to: str = Query(""),
              q: str = Query(""),
              db: Session = Depends(get_session),
              user: UserRow = Depends(require_reviewer)):
    """Every consequential action, newest first.

    Readable by anyone signed in on purpose: an audit trail only one person can
    see isn't much of a control.
    """
    from datetime import datetime as _dt
    from sqlalchemy import or_ as _or, func
    from .models_db import AuditRow

    qy = db.query(AuditRow).filter(AuditRow.company == company)
    if actor:
        qy = qy.filter(AuditRow.actor == actor)
    if action:
        qy = qy.filter(AuditRow.action.ilike(f"%{action}%"))
    if date_from:
        try:
            qy = qy.filter(AuditRow.at >= _dt.fromisoformat(date_from))
        except ValueError:
            pass
    if date_to:
        try:
            qy = qy.filter(AuditRow.at <= _dt.fromisoformat(date_to + "T23:59:59"))
        except ValueError:
            pass
    if q:
        like = f"%{q}%"
        qy = qy.filter(_or(AuditRow.detail.ilike(like),
                           AuditRow.charge_id.ilike(like),
                           AuditRow.bill_id.ilike(like)))

    total = qy.count()
    # limit=0 means "all of it" -- the export asks for the whole filtered log.
    _q = qy.order_by(AuditRow.at.desc()).offset(offset)
    rows = (_q.limit(limit).all() if limit else _q.all())

    actors = [r[0] for r in db.query(AuditRow.actor).filter(
        AuditRow.company == company).distinct().all() if r[0]]
    actions = [r[0] for r in db.query(AuditRow.action).filter(
        AuditRow.company == company).distinct().all() if r[0]]

    return {
        "company": company, "total": total, "offset": offset, "limit": limit,
        "actors": sorted(actors), "actions": sorted(actions),
        "entries": [{
            "at": r.at.isoformat(),
            "at_utc": r.at.isoformat() + "Z",
            "action": r.action, "actor": r.actor,
            "charge_id": r.charge_id or "", "bill_id": r.bill_id or "",
            "detail": r.detail or "", "dry_run": r.dry_run,
        } for r in rows],
    }


@app.post("/queue/memo")
def set_memo(charge_id: str = Form(...), memo: str = Form(""),
             db: Session = Depends(get_session),
             user: UserRow = Depends(require_write)):
    """Edit the memo that will be written to QuickBooks."""
    from .models_db import ChargeRow, AuditRow
    row = db.get(ChargeRow, charge_id)
    if row is None:
        raise HTTPException(404, "no such charge")
    if row.qbo_txn_id:
        raise HTTPException(
            409, "this charge is already posted — undo it first to change the memo")
    before = row.memo or ""
    row.memo = memo.strip()
    db.add(AuditRow(company=row.company, action="memo_edited",
                    charge_id=charge_id,
                    detail=f"{before!r} -> {row.memo!r}", dry_run=True,
                    actor=user.username))
    db.commit()
    return {"charge_id": charge_id, "memo": row.memo}


@app.get("/queue/{company}/version")
def queue_version(company: str, status: str = Query("for_review"),
                  db: Session = Depends(get_session),
                  user: UserRow = Depends(require_reviewer)):
    """Cheap poll: counts plus the latest resolution time.

    The UI hits this every few seconds. Returning a fingerprint rather than the
    rows keeps it light enough to poll while several people work at once.
    """
    from sqlalchemy import func
    from .models_db import ChargeRow
    counts = dict(db.query(ChargeRow.status, func.count(ChargeRow.charge_id))
                  .filter(ChargeRow.company == company)
                  .group_by(ChargeRow.status).all())
    latest = db.query(func.max(ChargeRow.resolved_at)).filter(
        ChargeRow.company == company).scalar()
    return {"for_review": counts.get("for_review", 0),
            "categorized": counts.get("categorized", 0),
            "excluded": counts.get("excluded", 0),
            "latest_change": latest.isoformat() if latest else ""}


def _dt_from(v):
    """A date string as the first instant of that day, in UTC."""
    from datetime import datetime as _dt, date as _d
    try:
        return _dt.combine(_d.fromisoformat(str(v)[:10]), _dt.min.time()) if v else None
    except ValueError:
        return None


def _dt_to(v):
    """...and as the last, so a single-day range includes the whole day."""
    from datetime import datetime as _dt, date as _d, timedelta as _td
    try:
        if not v:
            return None
        return _dt.combine(_d.fromisoformat(str(v)[:10]), _dt.min.time()) + _td(days=1)
    except ValueError:
        return None


def _as_naive(v):
    """ISO-with-Z to a naive datetime, so openpyxl writes a real timestamp."""
    from datetime import datetime as _dt
    try:
        return _dt.fromisoformat(str(v).replace("Z", "")) if v else None
    except ValueError:
        return None


def _as_date(v):
    """ISO string to a real date, so Excel formats and sorts it as one."""
    from datetime import date as _dd
    try:
        return _dd.fromisoformat(str(v)[:10]) if v else None
    except ValueError:
        return v


def _fmt_central_ts(dt):
    """UTC stamp as Chicago wall time — the team reads it as local either way."""
    from .export_xlsx import fmt_central
    return fmt_central(dt)


@app.get("/export/bills/{company}")
def export_bills(company: str, q: str = Query(""), vendor: str = Query(""),
                 memo: str = Query(""), date_from: str = Query(""),
                 date_to: str = Query(""), amt_min: str = Query(""),
                 amt_max: str = Query(""), bal_min: str = Query(""),
                 bal_max: str = Query(""), sort: str = Query("date"),
                 direction: str = Query("desc"),
                 db: Session = Depends(get_session),
                 user: UserRow = Depends(require_reviewer)):
    """Open bills matching the current filters, all of them."""
    from .export_xlsx import build_workbook, filename
    data = list_bills(company=company, limit=0, offset=0, q=q, vendor=vendor,
                      memo=memo, date_from=date_from, date_to=date_to,
                      amt_min=amt_min, amt_max=amt_max, bal_min=bal_min,
                      bal_max=bal_max, sort=sort, direction=direction,
                      db=db, user=user)
    headers = ["Bill No.", "Vendor", "Date", "Amount", "Balance", "QBO Memo"]
    rows = [[b["bill"], b["vendor"], _as_date(b["date"]), float(b["amt"] or 0),
             float(b["balance"] or 0), b["memo"]] for b in data["bills"]]
    applied = [("Company", company), ("Tab", "Bills Available"),
               ("Vendor", vendor or "—"), ("QBO memo", memo or "—"),
               ("Bill no. / search", q or "—"),
               ("Date from", date_from or "—"), ("Date to", date_to or "—"),
               ("Amount min", amt_min or "—"), ("Amount max", amt_max or "—"),
               ("Balance min", bal_min or "—"), ("Balance max", bal_max or "—"),
               ("Sorted by", f"{sort} {direction}"),
               ("Exported by", user.username)]
    blob = build_workbook("Bills", headers, rows, applied,
                          date_cols=[2], money_cols=[3, 4])
    return Response(
        content=blob,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition":
                 f'attachment; filename="{filename("bills", company)}"'})


@app.get("/export/audit/{company}")
def export_audit(company: str, action: str = Query(""), actor: str = Query(""),
                 date_from: str = Query(""), date_to: str = Query(""),
                 q: str = Query(""),
                 db: Session = Depends(get_session),
                 user: UserRow = Depends(require_reviewer)):
    """The audit log matching the current filters."""
    from .export_xlsx import build_workbook, filename, fmt_central
    data = audit_log(company=company, limit=0, offset=0, action=action,
                     actor=actor, date_from=date_from, date_to=date_to, q=q,
                     db=db, user=user)
    headers = ["Time Stamp", "User", "Action", "Charge", "Detail"]
    rows = [[_as_naive(e.get("at_utc") or e.get("at", "")), e.get("actor", ""),
             e.get("action", ""), e.get("charge_id", ""), e.get("detail", "")]
            for e in data["entries"]]
    applied = [("Company", company), ("Tab", "Audit log"),
               ("Action", action or "All actions"),
               ("User", actor or "All users"),
               ("Date from", date_from or "—"), ("Date to", date_to or "—"),
               ("Detail contains", q or "—"),
               ("Exported by", user.username)]
    blob = build_workbook("Audit log", headers, rows, applied, stamp_cols=[0])
    return Response(
        content=blob,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition":
                 f'attachment; filename="{filename("audit", company)}"'})


@app.get("/export/queue/{company}")
def export_queue(company: str, status: str = Query("for_review"),
                 card: str = Query(""), txn_type: str = Query(""),
                 kinds: str = Query(""), tier: str = Query(""),
                 date_from: str = Query(""), date_to: str = Query(""),
                 amt_min: str = Query(""), amt_max: str = Query(""),
                 vendor: str = Query(""), profile: str = Query(""),
                 last4: str = Query(""), memo: str = Query(""),
                 cvendor: str = Query(""), category: str = Query(""),
                 user_f: str = Query("", alias="user"),
                 ts_from: str = Query(""), ts_to: str = Query(""),
                 q: str = Query(""),
                 sort: str = Query("date"), direction: str = Query("desc"),
                 columns: str = Query(""),
                 db: Session = Depends(get_session),
                 user: UserRow = Depends(require_reviewer)):
    """The whole filtered grid as a spreadsheet, page size ignored.

    `columns` is the visible column keys in the order the person arranged them,
    so the export matches the screen rather than some canonical layout.
    """
    from .review import build_queue
    from .export_xlsx import build_workbook, filename, fmt_central
    from datetime import date as _date, datetime as _dt
    from decimal import Decimal as _D, InvalidOperation

    def _d(v):
        try:
            return _date.fromisoformat(v) if v else None
        except ValueError:
            return None

    def _m(v):
        try:
            return _D(v) if v not in ("", None) else None
        except (InvalidOperation, TypeError):
            return None

    # limit=0 means every matching row: the page size is a screen constraint,
    # not part of what was asked for.
    data = build_queue(db, company, status=status, limit=0, offset=0,
                       card=card or None, date_from=_d(date_from),
                       date_to=_d(date_to), amt_min=_m(amt_min),
                       amt_max=_m(amt_max), vendor=vendor or None,
                       profile=profile or None, last4=last4 or None,
                       memo=memo or None, cvendor=cvendor or None,
                       txn_type=txn_type or None, kinds=kinds or None,
                       tier=tier or None, category=category or None,
                       user=user_f or None,
                       ts_from=_dt_from(ts_from), ts_to=_dt_to(ts_to),
                       q=q or None,
                       sort=sort, direction=direction)

    LABELS = {"card": "Card", "cven": "Vendor", "date": "Date",
              "amt": "Amount", "ven": "Bank Detail", "prof": "Profile",
              "l4": "CC Last 4", "memo": "QBO Memo", "kind": "Transaction Type",
              "status": "Strength", "details": "Categorization Details",
              "user": "User", "ts": "Time Stamp"}

    def kind_of(it):
        if it["resolution"] == "matched":
            return "Bill Payment"
        return "Refund/Payment" if it["is_credit"] else "Expense"

    def details_of(it):
        if it["resolution"] == "matched":
            parts = [f"Bill {it['matched_bill_no']}" if it["matched_bill_no"] else "",
                     it["matched_bill_vendor"], it["matched_bill_date"],
                     it["matched_bill_memo"]]
        else:
            parts = [it["coded_category"]]
        return "  ·  ".join(p for p in parts if p)

    VALUES = {
        "card": lambda it: it["account"],
        "cven": lambda it: (it["matched_bill_vendor"] if it["resolution"] == "matched"
                            else it["coded_vendor"]),
        "date": lambda it: _as_date(it["date"]),
        # Signed, so a refund reads as money back rather than another charge.
        "amt": lambda it: -float(it["amt"] or 0) if it["is_credit"] else float(it["amt"] or 0),
        "ven": lambda it: it["merchant"],
        "prof": lambda it: it["holder"],
        "l4": lambda it: it["card"],
        "memo": lambda it: it["memo"],
        "kind": kind_of,
        "status": lambda it: f"{it['tier']}{' ' + str(it['score']) + '%' if it['score'] else ''}",
        "details": details_of,
        "user": lambda it: it.get("resolved_by", ""),
        # Exports carry a real datetime; Excel formats it in the reader's own
        # locale, which is the same principle as the grid.
        "ts": lambda it: _as_naive(it.get("resolved_at_utc", "")),
        "tier": lambda it: it.get("tier", ""),
    }

    keys = [k for k in (columns.split(",") if columns else []) if k in VALUES]
    if not keys:
        keys = ["card", "date", "amt", "ven", "prof", "l4", "memo"]
        if status == "categorized":
            keys = ["card", "cven", "date", "amt", "ven", "prof", "l4", "memo",
                    "kind", "details", "user", "ts"]

    headers = [LABELS[k] for k in keys]
    rows = [[VALUES[k](it) for k in keys] for it in data["items"]]

    TAB = {"for_review": "For Review", "categorized": "Categorized",
           "excluded": "Excluded"}
    applied = [("Company", company), ("Tab", TAB.get(status, status)),
               ("Card accounts", card or "All cards"),
               ("Transaction type", kinds or txn_type or "All"),
               ("Strength", tier or "All strengths"),
               ("Date from", date_from or "—"), ("Date to", date_to or "—"),
               ("Amount min", amt_min or "—"), ("Amount max", amt_max or "—"),
               ("Vendor", cvendor or "—"), ("Bank detail", vendor or "—"),
               ("Profile", profile or "—"), ("CC last 4", last4 or "—"),
               ("QBO memo", memo or "—"),
               ("Sorted by", f"{sort} {direction}"),
               ("Exported by", user.username)]

    date_cols = [i for i, k in enumerate(keys) if k == "date"]
    stamp_cols = [i for i, k in enumerate(keys) if k == "ts"]
    money_cols = [i for i, k in enumerate(keys) if k == "amt"]
    blob = build_workbook(TAB.get(status, "Export"), headers, rows, applied,
                          date_cols=date_cols, money_cols=money_cols,
                          stamp_cols=stamp_cols)
    return Response(
        content=blob,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition":
                 f'attachment; filename="{filename("transactions", company)}"'})


def _convert_payment_to_expense(db, row, category: str, actor: str) -> dict:
    """Turn a matched bill payment into a coded expense, ledger included.

    QuickBooks has no way to change a BillPayment into a Purchase, so the
    payment is deleted and an expense posted in its place. The order matters:
    the deletion goes first, and only once QuickBooks confirms it does the
    charge get rewritten -- otherwise a failure would leave the app calling it
    an expense while the payment still sits in the ledger.
    """
    from .models_db import QboRefRow, ChargeRow
    from .integrations.qbo_write import (undo_post, push_charge, WriteBlocked,
                                         WriteFailed)

    was_posted = bool(row.qbo_txn_id)
    acct = db.query(QboRefRow).filter(
        QboRefRow.kind == "account", QboRefRow.name == category).first()
    if acct is None and was_posted:
        raise HTTPException(
            400, f"'{category}' isn't in the QuickBooks chart of accounts. "
                 f"Pick an existing category, or refresh accounts first.")

    # The bill's vendor carries over as the expense's payee -- it's the best
    # answer available, and it's editable once the charge is an expense.
    vendor = (row.coded_vendor or row.matched_bill_vendor or "").strip()
    ven = db.query(QboRefRow).filter(
        QboRefRow.kind == "vendor", QboRefRow.name == vendor).first() if vendor else None

    if was_posted:
        try:
            res = undo_post(db, row, actor)
        except (WriteBlocked, WriteFailed) as e:
            raise HTTPException(400, f"Could not remove the bill payment from "
                                     f"QuickBooks: {e}")
        if res.get("status") not in ("deleted", "nothing_posted"):
            raise HTTPException(
                400, f"Could not remove the bill payment from QuickBooks: "
                     f"{res.get('error', 'refused')}. Nothing was changed.")

    was_bill = row.matched_bill_no or row.matched_bill_id
    row.resolution = "coded"
    row.status = "categorized"
    row.coded_category = category
    row.coded_category_id = acct.qbo_id if acct else ""
    row.coded_vendor = vendor
    row.coded_vendor_id = ven.qbo_id if ven else ""
    row.matched_bill_id = ""
    row.matched_bill_no = row.matched_bill_vendor = ""
    row.matched_bill_date = row.matched_bill_memo = ""
    row.resolved_by, row.resolved_at = actor, datetime.utcnow()

    changed = [f"bill payment on bill {was_bill} recategorised as "
               f"expense '{category}'"]
    db.add(AuditRow(company=row.company, action="payment_to_expense",
                    charge_id=row.charge_id, detail=changed[0],
                    dry_run=False, actor=actor))
    db.commit()

    qbo = "not in QuickBooks"
    if was_posted:
        result = push_charge(db, row, actor)
        if result["status"] == "failed":
            # The payment is gone and the expense didn't land. Say so plainly:
            # this charge now needs attention in QuickBooks.
            row.post_error = result.get("error", "")[:1000]
            db.commit()
            raise HTTPException(
                400, f"The bill payment was removed, but the expense could not "
                     f"be created: {result.get('error')}. The charge is in "
                     f"Categorized with nothing posted — fix and retry.")
        qbo = "replaced with an expense"
    return {"changed": changed, "qbo": qbo}


# --- undo ---------------------------------------------------------------

# Fields that together describe what a charge WAS. Restoring these is what an
# undo does; anything not listed here is either derived (tier, suggestions) or
# not something a click changes.
_UNDO_FIELDS = (
    "status", "resolution", "coded_category", "coded_category_id",
    "coded_vendor", "coded_vendor_id", "matched_bill_id", "matched_bill_no",
    "matched_bill_vendor", "matched_bill_date", "matched_bill_memo",
    "memo", "cardholder_name", "merchant",
    "qbo_txn_id", "qbo_txn_type", "qbo_sync_token", "post_error",
    "resolved_by",
)

# One step, not a history. Undo is for "that click was wrong", and a deeper
# stack invites walking back through work that QuickBooks has since accepted.
UNDO_DEPTH = 1


def _snapshot(rows) -> str:
    """What these charges look like right now, as JSON."""
    import json as _json
    out = []
    for r in rows:
        snap = {f: getattr(r, f, None) for f in _UNDO_FIELDS}
        snap["charge_id"] = r.charge_id
        snap["resolved_at"] = r.resolved_at.isoformat() if r.resolved_at else None
        out.append(snap)
    return _json.dumps(out)


def _record_action(db, company: str, actor: str, action: str,
                   summary: str, before: str) -> None:
    """Remember one click, and forget anything past the depth limit."""
    from .models_db import ActionRow
    db.add(ActionRow(company=company, actor=actor, action=action,
                     summary=summary, state=before))
    db.flush()
    # Only the last few are keepable. Older ones are deleted outright rather
    # than left to accumulate: an undo stack you cannot reach is just a table
    # holding stale copies of charges.
    keep = [a.id for a in db.query(ActionRow)
            .filter(ActionRow.company == company, ActionRow.actor == actor,
                    ActionRow.undone_at.is_(None))
            .order_by(ActionRow.at.desc()).limit(UNDO_DEPTH).all()]
    if keep:
        db.query(ActionRow).filter(
            ActionRow.company == company, ActionRow.actor == actor,
            ActionRow.undone_at.is_(None),
            ~ActionRow.id.in_(keep)).delete(synchronize_session=False)


@app.get("/actions/undoable/{company}")
def undoable(company: str, db: Session = Depends(get_session),
             user: UserRow = Depends(require_reviewer)):
    """What this person's Undo button would reverse, and how deep it goes."""
    from .models_db import ActionRow
    rows = (db.query(ActionRow)
            .filter(ActionRow.company == company, ActionRow.actor == user.username,
                    ActionRow.undone_at.is_(None))
            .order_by(ActionRow.at.desc()).limit(UNDO_DEPTH).all())
    return {"depth": len(rows),
            "next": ({"id": rows[0].id, "summary": rows[0].summary,
                      "at": rows[0].at.isoformat() + "Z"} if rows else None)}


@app.post("/actions/undo")
def undo_last(company: str = Form(...), db: Session = Depends(get_session),
              user: UserRow = Depends(require_write)):
    """Reverse this person's most recent click, QuickBooks included.

    Only their own: with several people working the queue, a shared stack means
    undoing a colleague's match you never saw.

    A charge that has been touched SINCE the action is left alone. Restoring an
    old snapshot over someone else's later work would discard it silently, so
    the honest answer is to say which ones and stop.
    """
    import json as _json
    from .models_db import ActionRow, ChargeRow
    from .integrations.qbo_write import undo_post, WriteBlocked, WriteFailed
    from datetime import datetime as _dt

    act = (db.query(ActionRow)
           .filter(ActionRow.company == company, ActionRow.actor == user.username,
                   ActionRow.undone_at.is_(None))
           .order_by(ActionRow.at.desc()).first())
    if act is None:
        raise HTTPException(400, "Nothing of yours left to undo.")

    snaps = _json.loads(act.state or "[]")
    restored, skipped, failed = 0, [], []

    # Bills take a different route: reversing one means writing to QuickBooks
    # again, so it goes back through the same edit path rather than restoring
    # fields locally and leaving the ledger out of step.
    if act.action == "bill_edit":
        from .models_db import BillRow
        from .integrations.qbo_write import edit_bill, WriteBlocked, WriteFailed
        from decimal import Decimal as _D
        for snap in snaps:
            row = db.get(BillRow, snap["bill_id"])
            if row is None:
                skipped.append(f"{snap['bill_id']}: no longer here")
                continue
            try:
                edit_bill(db, row, user.username,
                          vendor_name=snap.get("vendor") or None,
                          new_amount=_D(snap["amount"]))
                restored += 1
            except (WriteBlocked, WriteFailed) as e:
                failed.append(f"{row.doc_number or row.bill_id}: {e}")
        act.undone_at = _dt.utcnow()
        db.add(AuditRow(company=company, action="undo",
                        detail=f"undid '{act.summary}' — {restored} restored",
                        dry_run=False, actor=user.username))
        db.commit()
        return {"summary": act.summary, "restored": restored,
                "skipped": skipped, "failed": failed}

    for snap in snaps:
        row = db.get(ChargeRow, snap["charge_id"])
        if row is None:
            skipped.append(f"{snap['charge_id']}: no longer here")
            continue
        # Changed since? Whoever did that gets to keep it.
        now_resolved = row.resolved_at.isoformat() if row.resolved_at else None
        if row.resolved_by and row.resolved_by != user.username:
            skipped.append(f"{row.merchant or row.charge_id}: "
                           f"changed since by {row.resolved_by}")
            continue

        # The ledger first. If this action put something in QuickBooks and the
        # snapshot says there was nothing there before, take it back out --
        # otherwise the transaction is orphaned.
        if row.qbo_txn_id and not snap.get("qbo_txn_id"):
            try:
                res = undo_post(db, row, user.username)
                if res.get("status") not in ("deleted", "nothing_posted", "skipped"):
                    failed.append(f"{row.merchant or row.charge_id}: "
                                  f"{res.get('error', 'QuickBooks refused')}")
                    continue
            except (WriteBlocked, WriteFailed) as e:
                failed.append(f"{row.merchant or row.charge_id}: {e}")
                continue

        for f in _UNDO_FIELDS:
            setattr(row, f, snap.get(f))
        row.resolved_at = (_dt.fromisoformat(snap["resolved_at"])
                           if snap.get("resolved_at") else None)
        restored += 1

    act.undone_at = _dt.utcnow()
    db.add(AuditRow(company=company, action="undo",
                    detail=f"undid '{act.summary}' — {restored} restored",
                    dry_run=False, actor=user.username))
    db.commit()
    return {"summary": act.summary, "restored": restored,
            "skipped": skipped, "failed": failed}


WEIGHT_LABELS = {
    "amount": ("Amount", "How close the charge and bill amounts are."),
    "email": ("Buyer Email", "The bill's buyer email is one of this "
                             "cardholder's addresses. The strongest identifier."),
    "vendor": ("Marketplace", "The bank detail names the same marketplace as "
                              "the bill's vendor — 'TM *' means Ticketmaster."),
    "text": ("Text Overlap", "Words shared between the bank detail and the "
                             "bill's event or vendor."),
    "name_email": ("Name in Email", "The cardholder's name appears inside the "
                                    "bill's buyer email. Used only when there "
                                    "is no exact email match."),
    "date": ("Date Proximity", "How close the two dates are."),
}


@app.get("/match/weights")
def list_match_weights(db: Session = Depends(get_session),
                       user: UserRow = Depends(require_reviewer)):
    """The scorecard: each signal, its weight, and what it means."""
    from .models_db import MatchWeightRow
    from engine.suggest import DEFAULT_WEIGHTS
    saved = {r.key: r.weight for r in db.query(MatchWeightRow).all()}
    return {"weights": [
        {"key": k, "label": WEIGHT_LABELS[k][0], "help": WEIGHT_LABELS[k][1],
         "weight": saved.get(k, d), "default": d, "tuned": k in saved}
        for k, d in DEFAULT_WEIGHTS.items()]}


@app.post("/match/weights")
def save_match_weights(payload: str = Form(...),
                       db: Session = Depends(get_session),
                       user: UserRow = Depends(require_admin)):
    """Save tuned weights. An empty payload restores the defaults.

    Changing these re-ranks every suggestion, so the stored strengths are
    recomputed straight away rather than drifting until the next bill sync.
    """
    import json as _json
    from .models_db import MatchWeightRow
    from engine.suggest import DEFAULT_WEIGHTS

    try:
        vals = _json.loads(payload or "{}")
    except ValueError:
        raise HTTPException(400, "Could not read those weights")

    db.query(MatchWeightRow).delete()
    for k, v in vals.items():
        if k not in DEFAULT_WEIGHTS:
            continue
        try:
            f = round(float(v), 4)
        except (TypeError, ValueError):
            raise HTTPException(400, f"'{v}' isn't a number")
        if not 0 <= f <= 1:
            raise HTTPException(400, "Weights run from 0 to 1")
        db.add(MatchWeightRow(key=k, weight=f))
    db.add(AuditRow(company="", action="match_weights_saved",
                    detail=", ".join(f"{k}={v}" for k, v in sorted(vals.items())),
                    dry_run=False, actor=user.username))
    db.commit()

    from .review import load_match_weights
    load_match_weights(db)
    return {"saved": len(vals)}


@app.post("/match/weights/preview")
def preview_match_weights(company: str = Form(...), payload: str = Form(...),
                          db: Session = Depends(get_session),
                          user: UserRow = Depends(require_reviewer)):
    """What these weights would do, without saving them.

    Re-scores the queue under the proposed scorecard and reports how many
    charges change tier and how many top-ranked bills change. Tuning blind --
    save, then hunt for what moved -- is how a scorecard quietly gets worse.
    """
    import json as _json
    from engine.suggest import current_weights, set_weights
    from .review import build_queue, load_match_weights

    try:
        vals = _json.loads(payload or "{}")
    except ValueError:
        raise HTTPException(400, "Could not read those weights")

    load_match_weights(db)
    before = build_queue(db, company, status="for_review", limit=0)
    was = {i["id"]: (i["tier"], (i["candidates"] or [{}])[0].get("bill_id"))
           for i in before["items"]}

    saved = current_weights()
    try:
        set_weights({**saved, **vals})
        after = build_queue(db, company, status="for_review", limit=0)
    finally:
        set_weights(saved)          # never leak a preview into live scoring

    tier_moves, top_moves = 0, 0
    for i in after["items"]:
        old = was.get(i["id"])
        if not old:
            continue
        if old[0] != i["tier"]:
            tier_moves += 1
        if old[1] != (i["candidates"] or [{}])[0].get("bill_id"):
            top_moves += 1
    return {"charges": len(after["items"]), "tier_changes": tier_moves,
            "top_match_changes": top_moves}


@app.get("/payments/rules")
def list_payment_rules(db: Session = Depends(get_session),
                       user: UserRow = Depends(require_reviewer)):
    """The rules that mark a credit as a payment rather than a refund."""
    from .models_db import PaymentRuleRow
    rows = db.query(PaymentRuleRow).order_by(PaymentRuleRow.item,
                                             PaymentRuleRow.phrase).all()
    return {"rules": [{"id": r.id, "phrase": r.phrase, "item": r.item,
                       "rule": r.rule, "active": bool(r.active),
                       "note": r.note} for r in rows]}


@app.post("/payments/rules")
def save_payment_rule(phrase: str = Form(...), item: str = Form("bank_detail"),
                      rule: str = Form("contains"), rule_id: str = Form(""),
                      active: str = Form("true"), remove: str = Form(""),
                      db: Session = Depends(get_session),
                      user: UserRow = Depends(require_admin)):
    """Add, change or remove one payment rule."""
    from .models_db import PaymentRuleRow
    from .review import reset_payment_rules

    if remove and rule_id:
        db.query(PaymentRuleRow).filter(PaymentRuleRow.id == int(rule_id)).delete()
        db.commit()
        reset_payment_rules()
        return {"removed": rule_id}

    phrase = (phrase or "").strip().lower()
    if not phrase:
        raise HTTPException(400, "An input is required")
    if item not in ("vendor", "bank_detail", "card_account"):
        raise HTTPException(400, "Item must be Vendor, Bank Detail or Card Account")
    if rule not in ("equals", "contains"):
        raise HTTPException(400, "Rule must be Equals or Contains")

    row = (db.query(PaymentRuleRow).filter(PaymentRuleRow.id == int(rule_id)).first()
           if rule_id else None)
    if row is None:
        row = PaymentRuleRow(phrase=phrase)
        db.add(row)
    row.phrase, row.item, row.rule = phrase, item, rule
    row.active = active != "false"
    db.add(AuditRow(company="", action="payment_rule_saved",
                    detail=f"{item} {rule} '{phrase}'", dry_run=False,
                    actor=user.username))
    db.commit()
    reset_payment_rules()
    return {"saved": row.id}


@app.get("/canada/rules")
def list_canada_rules(db: Session = Depends(get_session),
                      user: UserRow = Depends(require_reviewer)):
    """The phrases that mark a charge as Canadian."""
    from .models_db import CanadaRuleRow
    rows = db.query(CanadaRuleRow).order_by(CanadaRuleRow.kind,
                                            CanadaRuleRow.phrase).all()
    return {"rules": [{"id": r.id, "phrase": r.phrase, "kind": r.kind,
                       "item": r.item, "rule": r.rule,
                       "provinces": r.provinces, "active": bool(r.active),
                       "note": r.note} for r in rows]}


@app.post("/canada/rules")
def save_canada_rule(phrase: str = Form(...), kind: str = Form("phrase"),
                     item: str = Form("bank_detail"), rule: str = Form("contains"),
                     provinces: str = Form(""), rule_id: str = Form(""),
                     active: str = Form("true"), remove: str = Form(""),
                     db: Session = Depends(get_session),
                     user: UserRow = Depends(require_admin)):
    """Add, change or remove one rule, then rescore.

    A city rule needs its provinces: several Canadian city names are more
    commonly US ones in this data, so a bare city would mislabel every
    'ONTARIO CA' and 'VANCOUVER WA' as Canadian.
    """
    from .models_db import CanadaRuleRow
    from .canada import reset_rules

    if remove and rule_id:
        db.query(CanadaRuleRow).filter(CanadaRuleRow.id == int(rule_id)).delete()
        db.commit()
        reset_rules()
        return {"removed": rule_id}

    phrase = (phrase or "").strip().lower()
    if not phrase:
        raise HTTPException(400, "A phrase is required")
    if kind not in ("phrase", "city_province"):
        raise HTTPException(400, "Kind must be phrase or city_province")
    if kind == "city_province" and not provinces.strip():
        raise HTTPException(
            400, "A city rule needs its province codes — 'Vancouver' alone "
                 "would also match Vancouver, Washington.")

    row = (db.query(CanadaRuleRow).filter(CanadaRuleRow.id == int(rule_id)).first()
           if rule_id else None)
    if row is None:
        row = CanadaRuleRow(phrase=phrase)
        db.add(row)
    if item not in ("vendor", "bank_detail"):
        raise HTTPException(400, "Item must be Vendor or Bank Detail")
    if rule not in ("equals", "contains"):
        raise HTTPException(400, "Rule must be Equals or Contains")
    row.phrase = phrase
    row.item = item
    row.rule = rule
    row.kind = kind
    row.provinces = provinces.strip().upper()
    row.active = active != "false"
    db.add(AuditRow(company="", action="canada_rule_saved",
                    detail=f"{item} {rule} '{phrase}'",
                    dry_run=False, actor=user.username))
    db.commit()
    reset_rules()
    return {"saved": row.id}


@app.post("/rules/rescore")
@app.post("/canada/rescore")
def rescore_canada(company: str = Form(...),
                   db: Session = Depends(get_session),
                   user: UserRow = Depends(require_write)):
    """Re-apply the rules to every charge, so a change takes effect now."""
    from .review import refresh_flags
    return {"changed": refresh_flags(db, company)}


@app.post("/bills/edit")
def edit_bills(bill_ids: str = Form(...), vendor: str = Form(""),
               amount_mode: str = Form(""), amount_value: str = Form(""),
               db: Session = Depends(get_session),
               user: UserRow = Depends(require_write)):
    """Edit one or many bills: vendor, amount, or both.

    Three ways to say what the amount becomes:

      set      the figure given
      delta    add the figure (negative to subtract)
      percent  add that percentage (negative to subtract)

    Percentages round to the cent -- a bill is a real invoice, and rounding to
    the dollar would silently invent a discrepancy the reconciler then has to
    explain.

    Each bill is read and written on its own, so a batch is a sequence of
    independent edits. One failure doesn't stop the rest; it's named in the
    result instead.
    """
    from .models_db import BillRow
    from .integrations.qbo_write import edit_bill, WriteBlocked, WriteFailed
    from decimal import Decimal as _D, InvalidOperation, ROUND_HALF_UP

    ids = [b.strip() for b in bill_ids.split(",") if b.strip()]
    if not ids:
        raise HTTPException(400, "No bills selected")
    if amount_mode and amount_mode not in ("set", "delta", "percent"):
        raise HTTPException(400, "Amount change must be set, delta or percent")

    value = None
    if amount_mode:
        try:
            value = _D(str(amount_value).strip())
        except (InvalidOperation, TypeError):
            raise HTTPException(400, "That amount isn't a number")
        if amount_mode == "set" and value <= 0:
            raise HTTPException(400, "A bill's amount must be more than zero")

    rows = db.query(BillRow).filter(BillRow.bill_id.in_(ids)).all()
    # What the bills looked like first, so this click can be undone like any
    # other. Bills are not charges, so they carry their own snapshot shape.
    import json as _json
    _before_bills = _json.dumps([
        {"bill_id": r.bill_id, "vendor": r.vendor or "",
         "amount": str(r.amount or 0), "balance": str(r.balance or 0)}
        for r in rows])
    found = {r.bill_id for r in rows}
    updated, failed = [], [{"bill": i, "error": "no longer in the bill list"}
                           for i in ids if i not in found]

    for row in rows:
        new_amount = None
        if amount_mode:
            base = _D(str(row.amount or 0))
            if amount_mode == "set":
                new_amount = value
            elif amount_mode == "delta":
                new_amount = base + value
            else:
                new_amount = base + (base * value / _D(100))
            # To the cent, never the dollar.
            new_amount = new_amount.quantize(_D("0.01"), rounding=ROUND_HALF_UP)
        try:
            res = edit_bill(db, row, user.username,
                            vendor_name=vendor or None, new_amount=new_amount)
            if res["status"] == "updated":
                updated.append({"bill": row.doc_number or row.bill_id,
                                "changed": res["changed"]})
        except WriteBlocked as e:
            failed.append({"bill": row.doc_number or row.bill_id,
                           "error": f"writing to QuickBooks is turned off ({e})"})
        except WriteFailed as e:
            failed.append({"bill": row.doc_number or row.bill_id, "error": str(e)})

    if updated:
        what = []
        if vendor:
            what.append("vendor")
        if amount_mode:
            what.append("amount")
        _record_action(db, rows[0].company if rows else "", user.username,
                       "bill_edit",
                       f"Updated {' and '.join(what)} on {len(updated)} bill(s)",
                       _before_bills)
        db.commit()

    return {"updated": len(updated), "failed": failed, "details": updated}


@app.post("/qbo/vendors/create")
def create_vendor_endpoint(name: str = Form(...),
                           db: Session = Depends(get_session),
                           user: UserRow = Depends(require_write)):
    """Create a vendor in QuickBooks and cache it for immediate use."""
    from .integrations.qbo_write import create_vendor, WriteBlocked, WriteFailed
    from .models_db import QboRefRow
    try:
        res = create_vendor(db, name, user.username)
    except WriteBlocked as e:
        raise HTTPException(400, f"Writing to QuickBooks is turned off ({e}).")
    except WriteFailed as e:
        raise HTTPException(400, str(e))
    # Cached now rather than waiting for the hourly reference sync -- the point
    # of creating it here is to use it in the next click.
    realm = _current_realm(db)
    db.merge(QboRefRow(id=f"vendor:{realm}:{res['id']}", kind="vendor",
                       realm_id=realm, qbo_id=res["id"], name=res["name"],
                       synced_at=datetime.utcnow()))
    db.commit()
    return res


@app.post("/qbo/categories/create")
def create_category_endpoint(name: str = Form(...),
                             account_type: str = Form("Expense"),
                             db: Session = Depends(get_session),
                             user: UserRow = Depends(require_write)):
    """Create an expense account -- a category -- in QuickBooks."""
    from .integrations.qbo_write import create_category, WriteBlocked, WriteFailed
    from .models_db import QboRefRow
    try:
        res = create_category(db, name, user.username, account_type=account_type)
    except WriteBlocked as e:
        raise HTTPException(400, f"Writing to QuickBooks is turned off ({e}).")
    except WriteFailed as e:
        raise HTTPException(400, str(e))
    realm = _current_realm(db)
    db.merge(QboRefRow(id=f"account:{realm}:{res['id']}", kind="account",
                       realm_id=realm, qbo_id=res["id"], name=res["name"],
                       account_type=account_type, usable_for="category",
                       synced_at=datetime.utcnow()))
    db.commit()
    from .review import reset_account_cache
    reset_account_cache()      # suggestions may now resolve to it
    return res


@app.post("/qbo/accounts/create")
def create_bank_account_endpoint(name: str = Form(...),
                                 account_type: str = Form("Credit Card"),
                                 db: Session = Depends(get_session),
                                 user: UserRow = Depends(require_admin)):
    """Create the bank or credit-card account a card program settles to."""
    from .integrations.qbo_write import (create_bank_account, WriteBlocked,
                                         WriteFailed)
    from .models_db import QboRefRow
    try:
        res = create_bank_account(db, name, user.username,
                                  account_type=account_type)
    except WriteBlocked as e:
        raise HTTPException(400, f"Writing to QuickBooks is turned off ({e}).")
    except WriteFailed as e:
        raise HTTPException(400, str(e))
    realm = _current_realm(db)
    db.merge(QboRefRow(id=f"account:{realm}:{res['id']}", kind="account",
                       realm_id=realm, qbo_id=res["id"], name=res["name"],
                       account_type=res.get("type") or account_type,
                       usable_for="bank", synced_at=datetime.utcnow()))
    db.commit()
    return res


def _current_realm(db: Session) -> str:
    from .models_db import QboTokenRow
    row = db.query(QboTokenRow).first()
    return row.realm_id if row else ""


@app.post("/queue/edit")
def edit_charge(charge_id: str = Form(...), profile: str = Form(None),
                merchant: str = Form(None), memo: str = Form(None),
                category: str = Form(None), vendor: str = Form(None),
                # An empty form field arrives as None, which is the same thing
                # FastAPI gives for "not sent at all" -- so clearing a field
                # could not be told apart from leaving it alone. The client
                # names what it means to blank.
                clear: str = Form(""),
                db: Session = Depends(get_session),
                user: UserRow = Depends(require_write)):
    """Correct a charge's details, and carry the correction into QuickBooks.

    Which fields are editable depends on what the charge is:

      * bank detail, profile and the QBO memo -- always. The memo can be set on
        its own, and is ALSO rebuilt from the other two whenever either changes
        ("Bank Detail - Profile - 1234"). Editing what a memo describes should
        move the memo; anything else leaves a stale description in the ledger.
      * category and vendor -- only for a coded expense or refund. A bill
        payment's category is the bill it paid, and rewriting that would mean
        unpicking the payment rather than editing it.

    Anything already in QuickBooks is updated there too. An edit that only
    changed our copy would make this app a second set of books.
    """
    from .models_db import ChargeRow
    row = db.get(ChargeRow, charge_id)
    if row is None:
        raise HTTPException(404, "charge not found")

    # Compare against what the COLUMN shows, not just the coded field: on For
    # Review the cell displays the engine's suggestion, so clearing it looked
    # like "no change" and did nothing.
    _before_edit = _snapshot([row])
    before = {"profile": row.cardholder_name or "", "memo": row.memo or "",
              "merchant": row.merchant or "",
              "category": row.coded_category or row.suggested_category or "",
              "vendor": row.coded_vendor or row.suggested_vendor or ""}
    changed, needs_push = [], False

    if memo is not None and memo != before["memo"]:
        row.memo = memo
        changed.append(f"QBO memo '{before['memo']}' -> '{memo}'")
        needs_push = True

    if profile is not None and profile != before["profile"]:
        row.cardholder_name = profile
        changed.append(f"profile '{before['profile']}' -> '{profile}'")
    if merchant is not None and merchant != before["merchant"]:
        row.merchant = merchant
        changed.append(f"bank detail '{before['merchant']}' -> '{merchant}'")

    # The memo follows from them. Rebuilding it here rather than asking for it
    # keeps the ledger's description and the row it came from in step, and it
    # is the memo -- not the profile -- that QuickBooks actually shows.
    if profile is not None or merchant is not None:
        from .ingestion import compose_memo, memo_tail
        # Bank detail and profile own their own segments and nothing else.
        # Anything typed after the card number is a note somebody added on
        # purpose, so it survives a correction to either of them.
        recognised, tail = memo_tail(before["memo"], row.card_last4 or "")
        new_memo = (compose_memo(row.merchant, row.cardholder_name,
                                 row.card_last4 or "", tail)
                    if recognised else (row.memo or ""))
        if new_memo != (row.memo or ""):
            was = row.memo or ""
            row.memo = new_memo
            changed.append(f"QBO memo '{was}' -> '{new_memo}'")
            needs_push = True

    # --- a bill payment recategorised becomes an expense ------------------
    #
    # There is no way to "edit" a BillPayment into a Purchase: they're
    # different objects. So the payment is deleted (which puts the bill back on
    # the books) and an expense is created in its place. Both sides move
    # together or neither does.
    if row.resolution == "matched" and category is not None \
            and category.strip() and category.strip().lower() != "bill payment":
        if vendor is not None and vendor.strip() != (row.matched_bill_vendor or "").strip():
            raise HTTPException(
                400, "Change the category first — a bill payment's vendor "
                     "comes from the bill it paid.")
        result = _convert_payment_to_expense(db, row, category.strip(),
                                             user.username)
        return {"charge_id": charge_id, "changed": result["changed"],
                "qbo": result["qbo"]}

    _clear = {c.strip() for c in (clear or "").split(",") if c.strip()}
    if _clear:
        # Applied straight to the row, not routed through the "did the value
        # change" comparison below. The value on screen can come from a live
        # computation that was never written to the row, so `before` reads as
        # blank and the comparison decides nothing changed -- which is how a
        # visible vendor refused to clear.
        if row.qbo_txn_id:
            raise HTTPException(
                400, "This charge is in QuickBooks, so it needs a vendor and "
                     "a category.")
        if "category" in _clear:
            row.coded_category = row.coded_category_id = ""
            row.suggested_category = ""
            row.category_cleared = True
            category = None
        if "vendor" in _clear:
            row.coded_vendor = row.coded_vendor_id = ""
            row.suggested_vendor = ""
            row.vendor_cleared = True
            vendor = None
        changed.append(f"{' and '.join(sorted(_clear))} cleared")
        needs_push = False

    if category is not None or vendor is not None:
        if row.resolution == "matched":
            # Vendor, or a no-op "Bill Payment" category: neither is editable
            # on a payment.
            raise HTTPException(
                400, "A bill payment's vendor comes from the bill it paid. "
                     "Change the category to recategorise it as an expense.")
        from .models_db import QboRefRow
        if category is not None and category != before["category"]:
            # Blank is allowed while a charge is still unresolved -- clearing a
            # wrong suggestion is a normal step, and Add already refuses to
            # commit without one. Only a POSTED charge needs a value, because
            # QuickBooks is holding it.
            if not category.strip():
                if row.qbo_txn_id:
                    raise HTTPException(
                        400, "This charge is in QuickBooks, so it needs a category.")
                row.coded_category = row.coded_category_id = ""
                row.suggested_category = ""
                row.suggestion_cleared = True
                changed.append(f"category '{before['category']}' cleared")
                category = None          # nothing further to resolve
            # One direction only. Going the other way means picking WHICH bill
            # this pays -- a decision with candidates, amounts and a rounding
            # check behind it, not a category you can type. Undo it back to For
            # Review and match it there.
            elif category.strip().lower() in ("bill payment", "billpayment"):
                raise HTTPException(
                    400, "To pay a bill with this charge, undo it back to For "
                         "Review and match it to the bill.")
            else:
                # Re-resolve to a QuickBooks id. The old id belongs to the old
                # category, and clearing it is not enough: QuickBooks accepts an
                # AccountRef by name when CREATING, but rejects an update with
                # "Required parameter ... AccountRef is missing" without a value.
                acct = db.query(QboRefRow).filter(
                    QboRefRow.kind == "account",
                    QboRefRow.name == category).first()
                if acct is None and row.qbo_txn_id:
                    raise HTTPException(
                        400, f"'{category}' isn't in the QuickBooks chart of "
                             f"accounts. Pick an existing category, or refresh "
                             f"accounts from QuickBooks first.")
                row.coded_category = category
                row.coded_category_id = acct.qbo_id if acct else ""
                row.category_cleared = False
                changed.append(f"category '{before['category']}' -> '{category}'")
                needs_push = True
        if vendor is not None and vendor != before["vendor"]:
            if not vendor.strip():
                # Blank is fine while the charge is unresolved -- clearing a
                # wrong suggestion is a normal step, and Add refuses to commit
                # without one anyway. A POSTED charge needs a value, because
                # QuickBooks is holding it.
                if row.qbo_txn_id:
                    raise HTTPException(
                        400, "This charge is in QuickBooks, so it needs a vendor.")
                row.coded_vendor = row.coded_vendor_id = row.suggested_vendor = ""
                row.suggestion_cleared = True
                changed.append(f"vendor '{before['vendor']}' cleared")
            else:
                ven = db.query(QboRefRow).filter(
                    QboRefRow.kind == "vendor", QboRefRow.name == vendor).first()
                if ven is None and row.qbo_txn_id:
                    raise HTTPException(
                        400, f"'{vendor}' isn't a vendor in QuickBooks. Pick an "
                             f"existing vendor, or refresh from QuickBooks first.")
                row.coded_vendor = vendor
                row.coded_vendor_id = ven.qbo_id if ven else ""
                row.vendor_cleared = False
                changed.append(f"vendor '{before['vendor']}' -> '{vendor}'")
                needs_push = True

    if not changed:
        return {"charge_id": charge_id, "changed": [], "qbo": "unchanged"}

    db.add(AuditRow(company=row.company, action="charge_edited",
                    charge_id=charge_id, detail="; ".join(changed),
                    dry_run=False, actor=user.username))
    _record_action(db, row.company, user.username, "edit",
                   changed[0] if len(changed) == 1
                   else f"Edited {len(changed)} fields", _before_edit)
    db.commit()

    qbo = "not in QuickBooks"
    if needs_push and row.qbo_txn_id:
        from .integrations.qbo_write import (update_posted_charge, WriteBlocked,
                                             WriteFailed)
        try:
            qbo = update_posted_charge(db, row, user.username)["status"]
        except WriteBlocked as e:
            qbo = f"not pushed ({e})"
        except WriteFailed as e:
            # The local edit stands; the ledger didn't take it. Say so rather
            # than rolling back an edit the person can see on screen.
            db.add(AuditRow(company=row.company, action="qbo_update_failed",
                            charge_id=charge_id, detail=str(e)[:400],
                            dry_run=False, actor=user.username))
            db.commit()
            raise HTTPException(
                400, f"Saved here, but QuickBooks refused the update: {e}")
    return {"charge_id": charge_id, "changed": changed, "qbo": qbo}


@app.post("/queue/select-all")
def select_all_ids(company: str = Form(...), status: str = Form("for_review"),
                   card: str = Form(""), source: str = Form(""),
                   vendor: str = Form(""), profile: str = Form(""),
                   last4: str = Form(""), memo: str = Form(""),
                   cvendor: str = Form(""), details: str = Form(""),
                   category: str = Form(""), user_f: str = Form("", alias="user"),
                   currency: str = Form(""),
                   ts_from: str = Form(""), ts_to: str = Form(""),
                   txn_type: str = Form(""), kinds: str = Form(""),
                   tier: str = Form(""), matchable: str = Form(""),
                   q: str = Form(""),
                   date_from: str = Form(""), date_to: str = Form(""),
                   amt_min: str = Form(""), amt_max: str = Form(""),
                   db: Session = Depends(get_session),
                   user: UserRow = Depends(require_write)):
    """Every charge id matching the current filters, not just the page shown.

    The queue renders a capped slice, so selecting the visible rows would miss
    most of them -- this returns the full set so a bulk action means what the
    person expects.

    Uses the same apply_charge_filters as the grid and the counts: "all" has to
    mean all of what's on screen, and three copies of the filter logic is how
    they drift apart.
    """
    from .models_db import ChargeRow
    from .review import apply_charge_filters
    from datetime import date as _dt
    from decimal import Decimal as _D, InvalidOperation as _IO

    def _d(v):
        try:
            return _dt.fromisoformat(v) if v else None
        except ValueError:
            return None

    def _m(v):
        try:
            return _D(v) if v else None
        except (_IO, TypeError):
            return None

    qy = apply_charge_filters(
        db.query(ChargeRow.charge_id).filter(ChargeRow.company == company),
        dict(status=status, card=card or None, source=source or None,
             vendor=vendor or None, profile=profile or None, last4=last4 or None,
             memo=memo or None, cvendor=cvendor or None, details=details or None,
             txn_type=txn_type or None, kinds=kinds or None, tier=tier or None,
             category=category or None, user=user_f or None,
             currency=currency or None,
             ts_from=_dt_from(ts_from), ts_to=_dt_to(ts_to),
             matchable=matchable or None,
             q=q or None, date_from=_d(date_from), date_to=_d(date_to),
             amt_min=_m(amt_min), amt_max=_m(amt_max)))
    ids = [r[0] for r in qy.all()]
    return {"ids": ids, "count": len(ids)}


@app.post("/queue/resolve")
def resolve(ids: str = Form(...), action: str = Form(...),
            bill_id: str = Form(""), category: str = Form(""),
            vendor: str = Form(""), db: Session = Depends(get_session),
            user: UserRow = Depends(require_write)):
    """Resolve one or many charges.

    action: match | code | exclude | undo

    A coding must carry a vendor. QuickBooks will accept a Purchase without an
    EntityRef, so nothing downstream would reject it -- it just lands in the
    ledger with a blank payee, and finding those again afterwards means going
    charge by charge. Cheaper to insist up front.

    Accepts a comma-separated list of ids so the UI can act on a whole
    selection at once. Every change is written to the audit log with the
    previous state, which is what makes undo possible and what an auditor
    would ask for.
    """
    from .models_db import ChargeRow, AuditRow
    id_list = [i for i in ids.split(",") if i.strip()]
    if not id_list:
        raise HTTPException(400, "no charge ids given")
    if action not in ("match", "code", "exclude", "undo"):
        raise HTTPException(400, "action must be match, code, exclude or undo")
    if action == "match" and not bill_id and len(id_list) > 1:
        raise HTTPException(400, "matching several charges needs one bill each")

    from .models_db import BillRow
    from .integrations.qbo_write import (push_charge, undo_post, WriteBlocked,
                                         assert_writes_allowed)
    try:
        assert_writes_allowed()
        live = True
    except WriteBlocked:
        live = False          # gates closed: resolve locally only

    changed = 0
    if action == "code":
        # Both are required by the write path anyway -- post_expense refuses a
        # charge with no category, and QuickBooks takes a Purchase with no
        # payee but leaves it blank in the ledger. Catching it here saves the
        # charge a pointless trip to Categorized and back.
        missing = [n for n, v in (("Category", category), ("Vendor", vendor))
                   if not (v or "").strip()]
        if missing:
            raise HTTPException(
                400, f"{' and '.join(missing)} {'are' if len(missing) > 1 else 'is'}"
                     f" required")

    # What these charges looked like BEFORE the click, captured while it is
    # still true. One snapshot for the whole batch: pressing Add over forty
    # charges is one action, so one Undo puts all forty back.
    _rows_before = [r for r in (db.get(ChargeRow, c) for c in id_list)
                    if r is not None]
    _before = _snapshot(_rows_before)
    # Every charge in a batch belongs to one company; take it from the rows
    # rather than asking the caller for something it already implies.
    _company = _rows_before[0].company if _rows_before else ""

    posted, post_errors, conflicts = [], [], []
    for cid in id_list:
        row = db.get(ChargeRow, cid)
        if row is None:
            continue

        # Someone else may have resolved this since the page was loaded. Silently
        # overwriting would leave the app disagreeing with QuickBooks -- their
        # transaction stays in the ledger while our record says something else.
        # Deliberately not "someone ELSE resolved it". One person with the app
        # open in two tabs hit this constantly: both tabs passed the guard, the
        # second resolve was a no-op because the write path is idempotent, and
        # nothing was reported -- so it looked like the click did nothing.
        # Already resolved is already resolved, whoever did it.
        if (action in ("match", "code", "exclude")
                and row.status != "for_review"
                and row.resolved_by
                and not row.resolved_by.startswith("auto:")):
            conflicts.append({
                "charge_id": cid,
                "resolved_by": row.resolved_by,
                "as": (f"bill {row.matched_bill_id}" if row.resolution == "matched"
                       else row.coded_category or row.status),
                # Lets the UI name the state rather than describing it.
                "status": row.status,
                "posted": bool(row.qbo_txn_id),
            })
            continue

        before = {"status": row.status, "resolution": row.resolution,
                  "bill": row.matched_bill_id, "category": row.coded_category}

        if action == "match":
            if row.is_credit:
                conflicts.append({
                    "charge_id": cid, "resolved_by": "—",
                    "as": "refund — code it as an expense/refund instead",
                    "posted": False})
                continue
            row.status, row.resolution = "categorized", "matched"
            row.matched_bill_id = bill_id
            row.coded_category = row.coded_vendor = ""
            # Snapshot the bill now -- see models_db: the BillRow is pruned once
            # QuickBooks stops returning it as open, which this match causes.
            _b = db.get(BillRow, bill_id) if bill_id else None
            if _b is not None:
                row.matched_bill_no = _b.doc_number or _b.bill_id or ""
                row.matched_bill_vendor = _b.vendor or ""
                row.matched_bill_date = str(_b.txn_date or "")
                row.matched_bill_memo = (_b.memo or "")[:500]
        elif action == "code":
            row.status, row.resolution = "categorized", "coded"
            row.coded_category, row.coded_vendor = category, vendor
            # Resolve to QuickBooks ids now, so a later rename in QBO doesn't
            # orphan the coding.
            from .models_db import QboRefRow
            acct = db.query(QboRefRow).filter(
                QboRefRow.kind == "account", QboRefRow.name == category).first()
            row.coded_category_id = acct.qbo_id if acct else ""
            ven = db.query(QboRefRow).filter(
                QboRefRow.kind == "vendor", QboRefRow.name == vendor).first() if vendor else None
            row.coded_vendor_id = ven.qbo_id if ven else ""
            row.matched_bill_id = ""
            # Learn from it: the next charge from this merchant pre-fills with
            # what was actually chosen, not the engine's hardcoded guess.
            from .learning import record_decision
            record_decision(db, row, category, vendor, user.username)
        elif action == "exclude":
            # Excluding is a statement that the charge does not belong in the
            # ledger. If it is already there, that has to be undone first --
            # otherwise the app records "excluded" while a live transaction
            # sits in QuickBooks with nothing pointing at it.
            if row.qbo_txn_id:
                raise HTTPException(
                    409, f"This charge is in QuickBooks as {row.qbo_txn_type} "
                         f"{row.qbo_txn_id}. Undo it first, then exclude it.")
            row.status, row.resolution = "excluded", ""
        elif action == "undo":
            if row.qbo_txn_id:
                # Remove it from the ledger first. Only if QuickBooks confirms
                # the delete do we clear local state -- otherwise the app would
                # claim the charge is unposted while the transaction lives on.
                res = undo_post(db, row, user.username)
                if res["status"] == "failed":
                    raise HTTPException(
                        409,
                        f"could not remove {row.qbo_txn_type} {row.qbo_txn_id} "
                        f"from QuickBooks: {res['error']}")
            row.status, row.resolution = "for_review", ""
            row.matched_bill_id = row.coded_category = row.coded_vendor = ""
            row.matched_bill_no = row.matched_bill_vendor = ""
            row.matched_bill_date = row.matched_bill_memo = ""
            row.post_error = ""

        row.resolved_by = "" if action == "undo" else user.username
        row.resolved_at = None if action == "undo" else datetime.utcnow()
        db.add(AuditRow(company=row.company, action=f"queue_{action}",
                        charge_id=cid, bill_id=bill_id or None,
                        detail=f"from {before} by {user.username}",
                        dry_run=not live, actor=user.username))
        changed += 1
    db.commit()

    # Post straight away rather than queueing: a reviewer's decision reaches
    # the ledger immediately, and Undo removes it again.
    if live and action in ("match", "code"):
        for cid in id_list:
            row = db.get(ChargeRow, cid)
            if row is None or row.qbo_txn_id:
                continue
            result = push_charge(db, row, user.username)
            if result["status"] == "posted":
                posted.append(result)
            elif result["status"] == "failed":
                # Nothing reached QuickBooks, so the charge has not been
                # resolved -- it goes back to For Review rather than sitting in
                # Categorized pretending to be done. Categorized is meant to
                # mean "this exists in the ledger"; a failed post that stayed
                # there made the tab lie, and left the reviewer to notice the
                # error and manually Undo before they could retry.
                row.post_error = result.get("error", "")[:1000]
                row.status, row.resolution = "for_review", ""
                row.matched_bill_id = ""
                row.coded_category = row.coded_vendor = ""
                row.coded_category_id = row.coded_vendor_id = ""
                row.matched_bill_no = row.matched_bill_vendor = ""
                row.matched_bill_date = row.matched_bill_memo = ""
                row.resolved_by, row.resolved_at = "", None
                db.add(AuditRow(
                    company=row.company, action="post_failed_reverted",
                    charge_id=cid,
                    detail=f"post failed, returned to For Review: "
                           f"{row.post_error[:300]}",
                    dry_run=not live, actor=user.username))
                db.commit()
                post_errors.append(result)

    # Recorded only if something actually changed -- an action that did nothing
    # is not worth a slot in a five-deep stack.
    if changed:
        verb = {"match": "Matched", "code": "Added", "exclude": "Excluded",
                "undo": "Undone"}.get(action, action.title())
        _record_action(db, _company, user.username, action,
                       f"{verb} {changed} transaction(s)", _before)
        db.commit()

    return {"action": action, "changed": changed, "live": live,
            "posted": len(posted), "post_failed": len(post_errors),
            "errors": post_errors[:5],
            "conflicts": len(conflicts), "conflict_detail": conflicts[:5]}


@app.get("/qbo/companies")
def qbo_companies(db: Session = Depends(get_session),
                  user: UserRow = Depends(require_reviewer)):
    """Connected QuickBooks files, by their own company names."""
    from .models_db import QboTokenRow
    from .integrations.qbo_bills import company_name
    out = []
    for t in db.query(QboTokenRow).all():
        name = t.company_name
        if not name:
            try:
                name = company_name(db, t.realm_id)
                t.company_name = name
                db.commit()
            except Exception:
                name = t.realm_id
        out.append({"realm_id": t.realm_id, "name": name or t.realm_id})
    return out


# --- card registry ----------------------------------------------------------

@app.get("/accounts/mapping")
def account_mapping(company: str = Query("Y&S Tickets"),
                    db: Session = Depends(get_session),
                    user: UserRow = Depends(require_reviewer)):
    """Card programs and their QBO accounts, plus any per-card overrides.

    Resolution order for a charge: per-card override, else the program default.
    """
    from .models_db import SourceAccountRow, CardRow, ChargeRow
    from sqlalchemy import func
    from .ingestion import SOURCE_SIGNATURES

    counts = dict(db.query(ChargeRow.source, func.count(ChargeRow.charge_id))
                  .filter(ChargeRow.company == company)
                  .group_by(ChargeRow.source).all())
    cards = dict(db.query(ChargeRow.source, func.count(func.distinct(ChargeRow.card_last4)))
                 .filter(ChargeRow.company == company)
                 .group_by(ChargeRow.source).all())

    mapped = {m.source: m for m in db.query(SourceAccountRow)
              .filter(SourceAccountRow.company == company).all()}

    # Always list every supported program, not only those with charges --
    # otherwise a new company shows an empty Cards screen and there's no way to
    # map an account before the first upload.
    from .ingestion import SOURCES as _SOURCES
    programs = []
    for src in sorted(set(list(counts) + list(mapped) + list(_SOURCES))):
        m = mapped.get(src)
        programs.append({
            "source": src,
            "nickname": m.nickname if m else "",
            "qbo_account_id": m.qbo_account_id if m else "",
            "qbo_account_name": m.qbo_account_name if m else "",
            "mapped": bool(m and m.qbo_account_id),
            # Blank means "same as source", which is true of the original three.
            "file_format": (m.file_format if m else "") or src,
            "is_virtual": bool(m.is_virtual) if m else False,
            "charges": counts.get(src, 0),
            "distinct_cards": cards.get(src, 0),
        })

    overrides = [{
        "id": c.id, "source": c.source, "last4": c.last4,
        "nickname": c.nickname, "qbo_account_name": c.qbo_account_name,
    } for c in db.query(CardRow).filter(
        CardRow.company == company, CardRow.qbo_account_id != "").all()]

    return {"company": company, "programs": programs, "overrides": overrides,
            "file_formats": sorted(SOURCE_SIGNATURES),
            "unmapped_programs": sum(1 for p in programs if not p["mapped"])}


def _assert_account_unique(db, company: str, source: str, nickname: str,
                           qbo_account_id: str) -> None:
    """Two card accounts must not share a name or a QuickBooks account.

    A duplicate name makes the upload picker ambiguous -- two identical entries
    and no way to tell which is which. Two programs pointing at one QuickBooks
    account is worse: every charge from either lands in the same place, so the
    mapping stops meaning anything and nothing downstream can tell them apart.
    """
    from .models_db import SourceAccountRow
    others = db.query(SourceAccountRow).filter(
        SourceAccountRow.company == company,
        SourceAccountRow.source != source).all()

    want = (nickname or "").strip().lower()
    if want:
        clash = next((o for o in others
                      if (o.nickname or o.source or "").strip().lower() == want), None)
        if clash:
            raise HTTPException(
                400, f"Another card account is already called "
                     f"'{clash.nickname or clash.source}'.")

    acct = (qbo_account_id or "").strip()
    if acct:
        clash = next((o for o in others if (o.qbo_account_id or "") == acct), None)
        if clash:
            raise HTTPException(
                400, f"'{clash.nickname or clash.source}' is already mapped to "
                     f"that QuickBooks account.")


@app.post("/accounts/mapping")
def save_account_mapping(source: str = Form(...), qbo_account_id: str = Form(...),
                         qbo_account_name: str = Form(...), nickname: str = Form(""),
                         company: str = Form("Y&S Tickets"),
                         db: Session = Depends(get_session),
                         user: UserRow = Depends(require_write)):
    """Point a whole card program at one QuickBooks account."""
    from .models_db import SourceAccountRow
    _assert_account_unique(db, company, source, nickname, qbo_account_id)
    db.merge(SourceAccountRow(
        id=f"{company}:{source}", company=company, source=source,
        nickname=nickname.strip(), qbo_account_id=qbo_account_id.strip(),
        qbo_account_name=qbo_account_name.strip(), updated_at=datetime.utcnow()))
    db.commit()
    return {"source": source, "account": qbo_account_name,
            "note": "all cards in this program now resolve to this account, "
                    "including ones not yet seen"}


@app.get("/cards")
def list_cards(company: str = Query("Y&S Tickets"),
               db: Session = Depends(get_session),
               user: UserRow = Depends(require_reviewer)):
    """Every card seen in the charge data, with its QBO mapping if set.

    Cards are discovered from the charges themselves -- upload an export and any
    new last-4 shows up here as unmapped, so nothing has to be entered by hand
    before it appears.
    """
    from .models_db import CardRow, ChargeRow
    from sqlalchemy import func

    seen = db.query(
        ChargeRow.source, ChargeRow.card_last4,
        func.count(ChargeRow.charge_id).label("n"),
        func.max(ChargeRow.cardholder_name).label("holder"),
    ).filter(ChargeRow.company == company,
             ChargeRow.card_last4 != "").group_by(
        ChargeRow.source, ChargeRow.card_last4).all()

    known = {c.id: c for c in db.query(CardRow).all()}
    out = []
    for src, last4, n, holder in seen:
        cid = f"{src}:{last4}"
        c = known.get(cid)
        out.append({
            "id": cid, "source": src, "last4": last4, "charges": n,
            "holder_seen": holder or "",
            "nickname": c.nickname if c else "",
            "qbo_account_id": c.qbo_account_id if c else "",
            "qbo_account_name": c.qbo_account_name if c else "",
            "mapped": bool(c and c.qbo_account_id),
            "active": c.active if c else True,
        })
    out.sort(key=lambda x: (x["mapped"], -x["charges"]))
    return {"company": company, "cards": out,
            "unmapped": sum(1 for c in out if not c["mapped"])}


@app.post("/cards")
def save_card(id: str = Form(...), nickname: str = Form(""),
              qbo_account_id: str = Form(""), qbo_account_name: str = Form(""),
              company: str = Form("Y&S Tickets"), active: bool = Form(True),
              db: Session = Depends(get_session),
              user: UserRow = Depends(require_write)):
    """Name a card and map it to a QBO account. Any signed-in user may do this."""
    from .models_db import CardRow
    src, _, last4 = id.partition(":")
    db.merge(CardRow(
        id=id, source=src, last4=last4, nickname=nickname.strip(),
        company=company, qbo_account_id=qbo_account_id.strip(),
        qbo_account_name=qbo_account_name.strip(), active=active,
        updated_at=datetime.utcnow(),
    ))
    db.commit()
    return {"saved": id, "nickname": nickname, "qbo_account": qbo_account_name}


@app.post("/accounts/source/virtual")
def set_source_virtual(company: str = Form(...), source: str = Form(...),
                       virtual: str = Form("true"),
                       db: Session = Depends(get_session),
                       user: UserRow = Depends(require_admin)):
    """Mark a card program as issuing virtual cards."""
    from .models_db import SourceAccountRow
    row = db.query(SourceAccountRow).filter(
        SourceAccountRow.company == company,
        SourceAccountRow.source == source).first()
    if row is None:
        row = SourceAccountRow(id=f"{company}:{source}", company=company,
                               source=source, nickname=source)
        db.add(row)
    row.is_virtual = virtual != "false"
    db.commit()
    return {"source": source, "is_virtual": row.is_virtual}


@app.post("/accounts/source/add")
def add_source_account(company: str = Form(""), source: str = Form(""),
                       nickname: str = Form(""), file_format: str = Form(""),
                       db: Session = Depends(get_session),
                       user: UserRow = Depends(require_admin)):
    """Register a card program before any of its charges have been uploaded.

    The list is otherwise derived from charges, which is a chicken-and-egg
    problem: a new program can't be mapped to a QuickBooks account until
    someone has already uploaded a file for it.
    """
    from .models_db import SourceAccountRow
    # Validated here rather than by FastAPI's required-field check, so an empty
    # box gets a sentence instead of a 422 the UI can only render as a blob.
    if not (company or "").strip():
        raise HTTPException(400, "Company is required")
    key = (source or "").strip().lower()
    if not key:
        raise HTTPException(400, "Card account key is required")
    if not key.replace("_", "").replace("-", "").isalnum():
        raise HTTPException(400, "Use letters, numbers, dashes or underscores only")
    existing = db.query(SourceAccountRow).filter(
        SourceAccountRow.company == company,
        SourceAccountRow.source == key).first()
    if existing:
        raise HTTPException(
            400, f"A card account called "
                 f"'{existing.nickname or existing.source}' already exists.")
    _assert_account_unique(db, company, key, nickname, "")
    # id is the composite "{company}:{source}", matching save_account_mapping.
    from .ingestion import SOURCE_SIGNATURES
    fmt = (file_format or "").strip().lower()
    if fmt and fmt not in SOURCE_SIGNATURES:
        raise HTTPException(
            400, f"File format must be one of {', '.join(sorted(SOURCE_SIGNATURES))}"
                 f", or blank for the pre-formatted export")
    db.add(SourceAccountRow(id=f"{company}:{key}", company=company, source=key,
                            nickname=(nickname or key).strip(),
                            file_format=fmt,
                            qbo_account_id="", qbo_account_name=""))
    db.add(AuditRow(company=company, action="card_account_added",
                    detail=f"{key} ({nickname or key})", dry_run=False,
                    actor=user.username))
    db.commit()
    return {"added": key}


@app.post("/accounts/source/delete")
def delete_source_account(company: str = Form(...), source: str = Form(...),
                          db: Session = Depends(get_session),
                          user: UserRow = Depends(require_admin)):
    """Remove a card program's mapping.

    Refuses while charges still reference it: deleting the mapping would leave
    those charges unpostable with no obvious cause, and the charges themselves
    are the record of what happened. Clear or exclude them first.
    """
    from .models_db import SourceAccountRow, ChargeRow, CardRow
    from sqlalchemy import func as _f
    by_status = dict(db.query(ChargeRow.status, _f.count(ChargeRow.charge_id))
                     .filter(ChargeRow.company == company,
                             ChargeRow.source == source)
                     .group_by(ChargeRow.status).all())
    total = sum(by_status.values())
    if total:
        # Any status blocks, including excluded and categorized: the charges are
        # the record of what happened, and removing the mapping they resolve
        # through would leave that record unexplainable later. Naming the split
        # at least says what to do about it.
        parts = ", ".join(f"{n} {st.replace('_', ' ')}"
                          for st, n in sorted(by_status.items()))
        raise HTTPException(
            400, f"{total} transaction(s) still use this card account "
                 f"({parts}). Delete them from Excluded, or leave the account "
                 f"in place.")
    db.query(SourceAccountRow).filter(
        SourceAccountRow.company == company,
        SourceAccountRow.source == source).delete()
    db.query(CardRow).filter(CardRow.company == company,
                             CardRow.source == source).delete()
    db.add(AuditRow(company=company, action="card_account_removed",
                    detail=source, dry_run=False, actor=user.username))
    db.commit()
    return {"removed": source}


@app.post("/cards/map-source")
def map_source(source: str = Form(...), qbo_account_id: str = Form(...),
               qbo_account_name: str = Form(...), company: str = Form("Y&S Tickets"),
               only_unmapped: bool = Form(True),
               db: Session = Depends(get_session),
               user: UserRow = Depends(require_write)):
    """Map every card from one program to a QBO account in one go.

    Virtual-card programs issue a card per buyer -- there are ~1,000 distinct
    last-4s in a single month -- but they all settle to the same QuickBooks
    account. Mapping them individually isn't realistic, so the default is to
    map by program and override the exceptions afterwards.
    """
    from .models_db import CardRow, ChargeRow
    from sqlalchemy import func

    seen = db.query(ChargeRow.card_last4, func.max(ChargeRow.cardholder_name)).filter(
        ChargeRow.company == company, ChargeRow.source == source,
        ChargeRow.card_last4 != "").group_by(ChargeRow.card_last4).all()

    n = 0
    for last4, holder in seen:
        cid = f"{source}:{last4}"
        existing = db.get(CardRow, cid)
        if only_unmapped and existing and existing.qbo_account_id:
            continue
        db.merge(CardRow(
            id=cid, source=source, last4=last4,
            nickname=existing.nickname if existing else "",
            holder=holder or "", company=company,
            qbo_account_id=qbo_account_id.strip(),
            qbo_account_name=qbo_account_name.strip(),
            active=True, updated_at=datetime.utcnow()))
        n += 1
        if n % 300 == 0:
            db.commit()
    db.commit()
    return {"source": source, "mapped": n, "account": qbo_account_name}


@app.get("/qbo/categories")
def qbo_categories(db: Session = Depends(get_session),
                   user: UserRow = Depends(require_reviewer)):
    """Cached expense/asset accounts. Refresh via /admin/sync-qbo."""
    from .models_db import QboRefRow
    rows = db.query(QboRefRow).filter(QboRefRow.kind == "account",
                                      QboRefRow.usable_for == "category").all()
    return {"categories": [{"id": r.qbo_id, "name": r.name,
                            "fully_qualified": r.name, "type": r.account_type,
                            "subtype": r.subtype}
                           for r in sorted(rows, key=lambda x: (x.account_type, x.name))]}


@app.get("/qbo/bank-accounts")
def qbo_bank_accounts(db: Session = Depends(get_session),
                      user: UserRow = Depends(require_reviewer)):
    """Bank and credit-card accounts -- what a card program settles to.

    Separate from /qbo/categories, which lists expense accounts. Offering the
    expense list here would let someone point a card program at an expense
    account, which QuickBooks accepts and then posts wrongly.
    """
    from .models_db import QboRefRow
    rows = db.query(QboRefRow).filter(QboRefRow.kind == "account",
                                      QboRefRow.usable_for == "bank").all()
    return {"accounts": [{"id": r.qbo_id, "name": r.name,
                          "type": r.account_type}
                         for r in sorted(rows, key=lambda x: (x.name or "").lower())]}


@app.get("/qbo/vendors")
def qbo_vendors(db: Session = Depends(get_session),
                user: UserRow = Depends(require_reviewer)):
    from .models_db import QboRefRow
    rows = db.query(QboRefRow).filter(QboRefRow.kind == "vendor").all()
    return {"vendors": [{"id": r.qbo_id, "name": r.name}
                        for r in sorted(rows, key=lambda x: (x.name or "").lower())]}


@app.get("/qbo/accounts")
def qbo_accounts(db: Session = Depends(get_session),
                 user: UserRow = Depends(require_reviewer)):
    """Cached bank / credit-card accounts. Refresh via /admin/sync-qbo."""
    from .models_db import QboRefRow
    rows = db.query(QboRefRow).filter(QboRefRow.kind == "account",
                                      QboRefRow.usable_for == "bank").all()
    return {"accounts": [{"id": r.qbo_id, "name": r.name, "type": r.account_type,
                          "subtype": r.subtype, "realm_id": r.realm_id}
                         for r in sorted(rows, key=lambda x: (x.account_type, x.name))]}


# --- admin actions ----------------------------------------------------------

@app.get("/admin/settings")
def get_settings(db: Session = Depends(get_session),
                 user: UserRow = Depends(require_reviewer)):
    """Runtime settings, with the env-var value as the fallback."""
    from .models_db import AppSettingRow
    row = db.get(AppSettingRow, "hal_seasons")
    return {
        "hal_seasons": (row.value if row else settings.hal_seasons),
        "hal_seasons_source": "database" if row else "environment",
        "available_seasons": ["24/25", "25/26", "26/27", "27/28", "28/29"],
        "updated_by": row.updated_by if row else "",
        "updated_at": row.updated_at if row else None,
    }


@app.post("/admin/settings")
def save_settings(hal_seasons: str = Form(...),
                  db: Session = Depends(get_session),
                  admin: UserRow = Depends(require_write)):
    """Change which HAL season columns are mirrored, without a redeploy."""
    from .models_db import AppSettingRow
    valid = {"24/25", "25/26", "26/27", "27/28", "28/29"}
    chosen = [s.strip() for s in hal_seasons.split(",") if s.strip()]
    bad = [s for s in chosen if s not in valid]
    if bad:
        raise HTTPException(400, f"unknown season(s): {bad}")
    if not chosen:
        raise HTTPException(400, "pick at least one season")

    db.merge(AppSettingRow(key="hal_seasons", value=",".join(chosen),
                           updated_by=admin.username, updated_at=datetime.utcnow()))
    db.commit()
    return {"hal_seasons": ",".join(chosen),
            "note": "takes effect on the next HAL sync"}


@app.post("/admin/sync-emails")
def admin_sync_emails(admin: UserRow = Depends(require_write)):
    """Mirror cardholder name -> email from the Airtable address book.

    Runs inline: it is one small table, unlike the HAL sync. Returns the counts
    so the result is verifiable rather than a hopeful "started".
    """
    if not settings.airtable_token:
        raise HTTPException(400, "AIRTABLE_TOKEN is not configured")
    from .worker import sync_profile_emails
    from .review import reset_email_cache
    n = sync_profile_emails()
    reset_email_cache()
    return {"addresses": n}


@app.get("/admin/email-status")
def email_status(db: Session = Depends(get_session),
                 user: UserRow = Depends(require_reviewer)):
    """How much of the address book is mirrored, and whether it is being used.

    'matched' is the number that actually helps: cardholder names on charges
    that resolve to an address. A big mirror and a small match count means the
    names on the cards are written differently from the names in Airtable.
    """
    from .models_db import ProfileEmailRow, ChargeRow
    from sqlalchemy import func as _f
    addresses = db.query(_f.count(ProfileEmailRow.email)).scalar() or 0
    people = db.query(_f.count(_f.distinct(ProfileEmailRow.name_key))).scalar() or 0
    known = {k for (k,) in db.query(ProfileEmailRow.name_key).distinct().all()}
    names = {(n or "").strip().lower()
             for (n,) in db.query(ChargeRow.cardholder_name).distinct().all() if n}
    matched = len(names & known)
    return {"addresses": addresses, "people": people,
            "cardholders": len(names), "matched": matched,
            "unmatched": sorted(names - known)[:12]}


@app.post("/admin/sync-hal")
def admin_sync_hal(background: BackgroundTasks,
                   admin: UserRow = Depends(require_write)):
    """Kick off the Airtable HAL mirror refresh.

    Runs in the background: ~18k records over paged requests takes minutes,
    far longer than an HTTP request should hang.
    """
    if not settings.airtable_token:
        raise HTTPException(400, "AIRTABLE_TOKEN is not configured")
    from .worker import sync_hal
    background.add_task(sync_hal)
    return {"started": True,
            "note": "syncing in the background; refresh the page in a few minutes"}


@app.post("/admin/sync-bills")
def admin_sync_bills(background: BackgroundTasks, company: str = Query(""),
                     days: int = Query(120),
                     db: Session = Depends(get_session),
                     admin: UserRow = Depends(require_write)):
    """Pull open bills (Balance > 0) from every connected QBO company file."""
    from .models_db import QboTokenRow, CompanyRow
    realms = [r.realm_id for r in db.query(QboTokenRow).all()]
    if not realms:
        raise HTTPException(400, "QuickBooks is not connected yet")
    if not company:
        reg = db.query(CompanyRow).filter(CompanyRow.active.is_(True)).first()
        if reg is None:
            raise HTTPException(400, "no company registered yet")
        company = reg.name

    from datetime import date as _d, timedelta as _td
    from .db import SessionLocal
    from .persistence import sync_bills_from_qbo

    def _run():
        s = SessionLocal()
        try:
            for realm in realms:
                sync_bills_from_qbo(s, company, realm,
                                    since=_d.today() - _td(days=days))
        finally:
            s.close()

    background.add_task(_run)
    return {"started": True, "realms": realms,
            "note": "pulling open bills in the background; reopen Setup shortly"}


@app.get("/admin/bills-status")
def bills_status(company: str = Query("Y&S Tickets"),
                 db: Session = Depends(get_session),
                 user: UserRow = Depends(require_reviewer)):
    from .models_db import BillRow
    return {"company": company,
            "bills": db.query(BillRow).filter(BillRow.company == company).count()}


@app.post("/admin/sync-qbo")
def admin_sync_qbo(background: BackgroundTasks, days: int = Query(120),
                   db: Session = Depends(get_session),
                   admin: UserRow = Depends(require_write)):
    """Refresh everything from QuickBooks: open bills, chart of accounts, vendors.

    This is the button to press after changing something in QBO and wanting the
    app to reflect it immediately, rather than waiting for the hourly run.
    """
    from .models_db import QboTokenRow
    if not db.query(QboTokenRow).count():
        raise HTTPException(400, "QuickBooks is not connected yet")
    from .worker import sync_qbo_all
    background.add_task(sync_qbo_all, days)
    return {"started": True,
            "note": "refreshing bills, accounts and vendors in the background"}


@app.get("/admin/qbo-status")
def qbo_sync_status(company: str = Query("Y&S Tickets"),
                    db: Session = Depends(get_session),
                    user: UserRow = Depends(require_reviewer)):
    from .models_db import BillRow, QboRefRow, SyncStatusRow
    st = db.get(SyncStatusRow, "qbo")
    return {
        "bills": db.query(BillRow).filter(BillRow.company == company).count(),
        "categories": db.query(QboRefRow).filter(
            QboRefRow.kind == "account", QboRefRow.usable_for == "category").count(),
        "bank_accounts": db.query(QboRefRow).filter(
            QboRefRow.kind == "account", QboRefRow.usable_for == "bank").count(),
        "vendors": db.query(QboRefRow).filter(QboRefRow.kind == "vendor").count(),
        "state": st.state if st else "never run",
        "detail": st.detail if st else "",
        "last_synced": st.finished_at if st else None,
    }


@app.get("/admin/schema")
def schema_status(admin: UserRow = Depends(require_admin)):
    """Which columns the database has, for diagnosing a bad deploy."""
    from sqlalchemy import inspect as _inspect
    from .db import engine as _e
    insp = _inspect(_e)
    from .alembic_boot import current_revision
    return {
        "alembic_revision": current_revision(),
        "tables": {t: sorted(c["name"] for c in insp.get_columns(t))
                   for t in sorted(insp.get_table_names())},
    }


@app.get("/admin/hal-status")
def hal_status(db: Session = Depends(get_session),
               user: UserRow = Depends(require_reviewer)):
    from .models_db import HalRow, SyncStatusRow
    n = db.query(HalRow).count()
    latest = db.query(HalRow).order_by(HalRow.synced_at.desc()).first()
    st = db.get(SyncStatusRow, "hal")
    from .hal_cache import stats as _cache_stats
    return {"records": n, "cache": _cache_stats(),
            "last_synced": latest.synced_at if latest else None,
            "configured": bool(settings.airtable_token),
            "state": st.state if st else "never run",
            "detail": st.detail if st else "",
            "finished_at": st.finished_at if st else None}


# --- QuickBooks OAuth -------------------------------------------------------

@app.get("/qbo/connect")
def qbo_connect(token: str | None = Query(None),
                db: Session = Depends(get_session)):
    """Start the OAuth flow.

    Browser navigation can't send an Authorization header, so this accepts the
    token as a query parameter instead. Admin is still required -- the token is
    verified the same way, just carried differently.
    """
    from jose import JWTError, jwt as _jwt
    if not token:
        raise HTTPException(401, "pass ?token=<your access token>")
    try:
        payload = _jwt.decode(token, _auth._secret(), algorithms=[_auth.ALGORITHM])
    except JWTError:
        raise HTTPException(401, "invalid or expired token")
    if payload.get("role") != "admin":
        raise HTTPException(403, "admin required")
    try:
        url, state = qbo_oauth.build_authorize_url()
    except qbo_oauth.OAuthError as e:
        raise HTTPException(400, str(e))
    db.add(OAuthStateRow(state=state))
    db.commit()
    return RedirectResponse(url)


@app.get("/qbo/callback")
def qbo_callback(code: str = Query(...), realmId: str = Query(...),
                 state: str = Query(...), db: Session = Depends(get_session)):
    """Intuit redirects here with the authorization code."""
    row = db.get(OAuthStateRow, state)
    if row is None:
        raise HTTPException(400, "unknown or reused state — restart the flow")
    if row.created_at < datetime.utcnow() - timedelta(minutes=15):
        db.delete(row); db.commit()
        raise HTTPException(400, "state expired — restart the flow")
    db.delete(row); db.commit()

    try:
        qbo_tokens.assert_realm_allowed(realmId)
        tokens = qbo_oauth.exchange_code(code, realmId)
        qbo_tokens.save_tokens(db, tokens)
        # store QBO's own company name so the picker matches what the team sees
        try:
            from .integrations.qbo_bills import company_name as _cn
            from .models_db import QboTokenRow as _Q
            row = db.get(_Q, realmId)
            if row is not None:
                row.company_name = _cn(db, realmId)
                db.commit()
        except Exception:
            pass
    except qbo_tokens.RealmNotAllowed as e:
        raise HTTPException(403, str(e))
    except qbo_oauth.OAuthError as e:
        raise HTTPException(400, str(e))

    return {"connected": True, "realm_id": realmId,
            "environment": settings.qbo_environment,
            "note": "read-only until the write gates are flipped"}


@app.get("/qbo/status")
def qbo_status(db: Session = Depends(get_session),
               admin: UserRow = Depends(require_owner)):
    return {
        "environment": settings.qbo_environment,
        "allowed_realms": sorted(qbo_tokens.allowed_realms()),
        "connections": qbo_tokens.connection_status(db),
        "dry_run": settings.dry_run,
        "write_enabled": settings.qbo_write_enabled,
    }


@app.post("/qbo/disconnect/{realm_id}")
def qbo_disconnect(realm_id: str, db: Session = Depends(get_session),
                   admin: UserRow = Depends(require_owner)):
    from .models_db import QboTokenRow
    row = db.get(QboTokenRow, realm_id)
    if row is None:
        raise HTTPException(404, "not connected")
    qbo_oauth.revoke(row.refresh_token)
    db.delete(row); db.commit()
    return {"disconnected": realm_id}


@app.get("/qbo/company/{realm_id}")
def qbo_company(realm_id: str, db: Session = Depends(get_session),
                admin: UserRow = Depends(require_admin)):
    """Cheapest possible check that a connection actually works."""
    from .integrations.qbo_bills import company_name, count_open_bills, QboReadError
    try:
        return {"realm_id": realm_id, "company_name": company_name(db, realm_id),
                "open_bills": count_open_bills(db, realm_id)}
    except Exception as e:
        raise HTTPException(400, str(e))


@app.post("/qbo/sync-bills/{company}")
def qbo_sync_bills(company: str, realm_id: str = Query(...),
                   days: int = Query(120),
                   db: Session = Depends(get_session),
                   admin: UserRow = Depends(require_write)):
    """Pull open bills from QBO into the local candidate pool."""
    from datetime import date as _date, timedelta as _td
    from .persistence import sync_bills_from_qbo
    try:
        # Bills changed, so every unresolved charge's match strength may have
        # too -- refresh it here rather than on the next page load.
        n = sync_bills_from_qbo(db, company, realm_id,
                                since=_date.today() - _td(days=days))
    except Exception as e:
        raise HTTPException(400, str(e))
    from .review import refresh_scores
    rescored = refresh_scores(db, company)
    return {"company": company, "realm_id": realm_id, "open_bills_synced": n,
            "rescored": rescored}


# --- review queue (read model for the UI) -----------------------------------

@app.get("/companies/registry")
def company_registry(db: Session = Depends(get_session),
                     user: UserRow = Depends(require_reviewer)):
    """The companies this engine services. Everyone reads; only admins write."""
    from .models_db import CompanyRow, ChargeRow, BillRow, QboTokenRow
    from sqlalchemy import func
    charges = dict(db.query(ChargeRow.company, func.count(ChargeRow.charge_id))
                   .group_by(ChargeRow.company).all())
    bills = dict(db.query(BillRow.company, func.count(BillRow.bill_id))
                 .group_by(BillRow.company).all())
    rows = db.query(CompanyRow).filter(CompanyRow.active.is_(True)).all()
    return {"companies": [{
        "id": c.id, "name": c.name, "realm_id": c.realm_id,
        "qbo_company_name": c.qbo_company_name,
        "charges": charges.get(c.name, 0), "bills": bills.get(c.name, 0),
    } for c in sorted(rows, key=lambda x: x.name)]}


@app.get("/companies/available-realms")
def available_realms(db: Session = Depends(get_session),
                     admin: UserRow = Depends(require_owner)):
    """Connected QuickBooks files, and whether each is already claimed."""
    from .models_db import QboTokenRow, CompanyRow
    taken = {c.realm_id: c.name for c in db.query(CompanyRow).all() if c.realm_id}
    out = []
    for t in db.query(QboTokenRow).all():
        name = t.company_name
        if not name:
            try:
                from .integrations.qbo_bills import company_name as _cn
                name = _cn(db, t.realm_id)
                t.company_name = name
                db.commit()
            except Exception:
                name = t.realm_id
        out.append({"realm_id": t.realm_id, "qbo_name": name,
                    "linked_to": taken.get(t.realm_id)})
    return {"realms": out}


@app.post("/companies")
def create_company(name: str = Form(...), realm_id: str = Form(...),
                   db: Session = Depends(get_session),
                   admin: UserRow = Depends(require_owner)):
    """Onboard a company and link it to a QuickBooks file. Admin only."""
    from .models_db import CompanyRow, QboTokenRow
    name = name.strip()
    if not name:
        raise HTTPException(400, "name is required")
    if db.get(QboTokenRow, realm_id) is None:
        raise HTTPException(400, f"realm {realm_id} is not connected")
    existing = db.query(CompanyRow).filter(CompanyRow.realm_id == realm_id).first()
    if existing is not None:
        raise HTTPException(409,
            f"that QuickBooks file is already linked to '{existing.name}'")
    if db.query(CompanyRow).filter(CompanyRow.name == name).first():
        raise HTTPException(409, f"a company named '{name}' already exists")

    tok = db.get(QboTokenRow, realm_id)
    db.add(CompanyRow(id=realm_id, name=name, realm_id=realm_id,
                      qbo_company_name=tok.company_name or "", active=True))
    db.commit()
    return {"created": name, "realm_id": realm_id}


@app.post("/companies/{company_id}/rename")
def rename_registered(company_id: str, new_name: str = Form(...),
                      db: Session = Depends(get_session),
                      admin: UserRow = Depends(require_owner)):
    """Rename a company and move all its data to the new name."""
    from .models_db import CompanyRow
    c = db.get(CompanyRow, company_id)
    if c is None:
        raise HTTPException(404, "no such company")
    old, new = c.name, new_name.strip()
    if not new or old == new:
        raise HTTPException(400, "give a different name")
    result = rename_company(old=old, new=new, db=db, admin=admin)
    c.name = new
    db.commit()
    return result


@app.get("/companies")
def companies(db: Session = Depends(get_session),
              user: UserRow = Depends(require_reviewer)):
    """Every company name that appears in the data, and where it came from.

    Charges carry whatever name was typed at upload; the QuickBooks file has
    its own name. If they differ, the queue filters on one and finds nothing --
    so surface both rather than silently showing an empty list.
    """
    from sqlalchemy import func, select as _sel
    from .models_db import ChargeRow, BillRow, QboTokenRow

    charge_counts = dict(db.query(ChargeRow.company, func.count(ChargeRow.charge_id))
                         .group_by(ChargeRow.company).all())
    bill_counts = dict(db.query(BillRow.company, func.count(BillRow.bill_id))
                       .group_by(BillRow.company).all())
    qbo = [{"realm_id": t.realm_id, "name": t.company_name or t.realm_id}
           for t in db.query(QboTokenRow).all()]

    names = sorted(set(charge_counts) | set(bill_counts) | {q["name"] for q in qbo})
    return {
        "companies": [{
            "name": n,
            "charges": charge_counts.get(n, 0),
            "bills": bill_counts.get(n, 0),
            "in_quickbooks": any(q["name"] == n for q in qbo),
        } for n in names],
        "quickbooks": qbo,
    }


@app.get("/companies/orphans")
def orphan_data(db: Session = Depends(get_session),
                admin: UserRow = Depends(require_owner)):
    """Data filed under a company name that isn't registered.

    Happens when charges were uploaded before the registry existed, or under a
    different spelling. Without surfacing it, the rows are invisible: the queue
    filters by the selected company and simply shows nothing.
    """
    from sqlalchemy import func
    from .models_db import ChargeRow, BillRow, CompanyRow

    registered = {c.name for c in db.query(CompanyRow).all()}
    charges = dict(db.query(ChargeRow.company, func.count(ChargeRow.charge_id))
                   .group_by(ChargeRow.company).all())
    bills = dict(db.query(BillRow.company, func.count(BillRow.bill_id))
                 .group_by(BillRow.company).all())

    orphans = []
    for name in sorted(set(charges) | set(bills)):
        if name in registered:
            continue
        orphans.append({"name": name, "charges": charges.get(name, 0),
                        "bills": bills.get(name, 0)})
    return {"orphans": orphans, "registered": sorted(registered)}


@app.post("/companies/adopt")
def adopt_orphans(orphan_name: str = Form(...), company: str = Form(...),
                  db: Session = Depends(get_session),
                  admin: UserRow = Depends(require_owner)):
    """Move data from an unregistered name onto a registered company."""
    from .models_db import CompanyRow
    if db.query(CompanyRow).filter(CompanyRow.name == company).first() is None:
        raise HTTPException(400, f"'{company}' is not a registered company")
    result = rename_company(old=orphan_name, new=company, db=db, admin=admin)

    # charge_ids are company-scoped, so rewrite the keys to match their new home
    from .models_db import ChargeRow
    moved = 0
    for row in db.query(ChargeRow).filter(ChargeRow.company == company).all():
        parts = row.charge_id.split(":", 2)
        if len(parts) == 3 and parts[0] != company:
            row.charge_id = f"{company}:{parts[1]}:{parts[2]}"
            moved += 1
        elif len(parts) == 2:                     # pre-scoping id
            row.charge_id = f"{company}:{parts[0]}:{parts[1]}"
            moved += 1
    db.commit()
    result["charge_ids_rekeyed"] = moved
    return result


@app.post("/companies/rename")
def rename_company(old: str = Form(...), new: str = Form(...),
                   db: Session = Depends(get_session),
                   admin: UserRow = Depends(require_owner)):
    """Move all data from one company name to another.

    For when charges were uploaded under a name that doesn't match the
    QuickBooks company, which leaves the queue looking empty.
    """
    from .models_db import (ChargeRow, BillRow, MatchRow, AuditRow, UploadRow,
                            SourceAccountRow, CardRow, LearnedRuleRow)
    old, new = old.strip(), new.strip()
    if not old or not new or old == new:
        raise HTTPException(400, "give two different names")

    moved = {}
    # CompanyRow keys on `name`, not `company`, and the caller updates it.
    for model in (ChargeRow, BillRow, MatchRow, AuditRow, UploadRow, CardRow):
        n = db.query(model).filter(model.company == old).update(
            {model.company: new}, synchronize_session=False)
        if n:
            moved[model.__tablename__] = n

    # these are keyed "{company}:..." / "{company}|...", so rebuild the ids
    for row in db.query(SourceAccountRow).filter(
            SourceAccountRow.company == old).all():
        db.delete(row)
        db.add(SourceAccountRow(
            id=f"{new}:{row.source}", company=new, source=row.source,
            nickname=row.nickname, qbo_account_id=row.qbo_account_id,
            qbo_account_name=row.qbo_account_name, realm_id=row.realm_id))
        moved["source_accounts"] = moved.get("source_accounts", 0) + 1

    for row in db.query(LearnedRuleRow).filter(
            LearnedRuleRow.company == old).all():
        db.delete(row)
        db.add(LearnedRuleRow(
            id=f"{new}|{row.merchant_key}", company=new,
            merchant_key=row.merchant_key, sample_merchant=row.sample_merchant,
            category=row.category, vendor=row.vendor,
            confirmations=row.confirmations, disagreements=row.disagreements,
            auto_apply=row.auto_apply, last_actor=row.last_actor))
        moved["learned_rules"] = moved.get("learned_rules", 0) + 1

    db.commit()
    return {"from": old, "to": new, "moved": moved}


@app.get("/review/companies")
def review_companies(db: Session = Depends(get_session),
                     user: UserRow = Depends(require_reviewer)):
    from .review import list_companies
    return list_companies(db)


@app.get("/review/{company}")
def review(company: str, limit: int = Query(200),
           db: Session = Depends(get_session),
           user: UserRow = Depends(require_reviewer)):
    from .review import build_review
    return build_review(db, company, limit=limit)


@app.post("/reconcile/{company}")
def reconcile_company(company: str, db: Session = Depends(get_session),
                      user: UserRow = Depends(require_write)):
    rows = run_reconciliation(db, company)
    counts: dict[str, int] = defaultdict(int)
    for r in rows:
        counts[r.decision] += 1
    return {"company": company, "charges": len(rows), "counts": dict(counts)}


@app.get("/results/{company}")
def results(company: str, db: Session = Depends(get_session),
            user: UserRow = Depends(require_reviewer)):
    rows = db.scalars(select(MatchRow).where(MatchRow.company == company)).all()
    grouped: dict[str, list] = defaultdict(list)
    for r in rows:
        grouped[r.decision].append({
            "charge_id": r.charge_id,
            "bill_id": r.bill_id,
            "score": round(r.score, 2),
            "overwrite_to": str(r.overwrite_bill_amount) if r.overwrite_bill_amount else None,
            "reasons": r.reasons.split(" | ") if r.reasons else [],
        })
    return {"company": company, "results": grouped}


@app.get("/audit/{company}")
def audit(company: str, db: Session = Depends(get_session),
          user: UserRow = Depends(require_reviewer)):
    rows = db.scalars(
        select(AuditRow).where(AuditRow.company == company).order_by(AuditRow.at)
    ).all()
    return {"company": company, "entries": [
        {"action": a.action, "charge_id": a.charge_id, "bill_id": a.bill_id,
         "detail": a.detail, "dry_run": a.dry_run, "at": a.at.isoformat()}
        for a in rows
    ]}


@app.post("/qbo/post/{company}")
def qbo_post(company: str, ids: str = Form(""), limit: int = Form(25),
             db: Session = Depends(get_session),
             admin: UserRow = Depends(require_write)):
    """Write categorized charges to QuickBooks. Admin only.

    Defaults to a small batch so the first live run can be checked by hand in
    QuickBooks before doing more. Already-posted charges are skipped, so a
    repeat call cannot duplicate anything.
    """
    from .integrations.qbo_write import push_batch, WriteBlocked
    id_list = [i for i in ids.split(",") if i.strip()] or None
    try:
        return push_batch(db, company, admin.username,
                          charge_ids=id_list, limit=limit)
    except WriteBlocked as e:
        raise HTTPException(400, str(e))


@app.get("/qbo/post-status/{company}")
def qbo_post_status(company: str, db: Session = Depends(get_session),
                    user: UserRow = Depends(require_reviewer)):
    """What is ready to post, and what already has been."""
    from .models_db import ChargeRow
    ready = db.query(ChargeRow).filter(
        ChargeRow.company == company, ChargeRow.status == "categorized",
        ChargeRow.qbo_txn_id == "").count()
    posted = db.query(ChargeRow).filter(
        ChargeRow.company == company, ChargeRow.qbo_txn_id != "").count()
    return {"ready_to_post": ready, "already_posted": posted,
            "dry_run": settings.dry_run,
            "write_enabled": settings.qbo_write_enabled,
            "writes_possible": (not settings.dry_run) and settings.qbo_write_enabled}


@app.post("/push/{company}")
def push(company: str, db: Session = Depends(get_session),
         admin: UserRow = Depends(require_write)):
    return push_company(db, company)
