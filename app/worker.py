"""
Ingestion worker + seed.

`seed()` loads the sample charges and bills into the database so the whole
pipeline runs end-to-end today. In production this is replaced by real
ingestion: pull card/TicketVault exports and a QBO `Balance > 0` bill read,
normalize, route to company, upsert.

`run_daily()` is the scheduled entrypoint — on Railway this is a cron/scheduled
job that ingests, then reconciles each company. Kept deliberately thin.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

from engine.sample_data import CHARGES, BILLS, COMPANY
from engine.hal import person_name, HalRecord, HalIndex
from .config import settings
from .db import init_db, SessionLocal
from .models_db import ChargeRow, BillRow, CompanyRow, HalRow
from .persistence import run_reconciliation
from .integrations.airtable import fetch_hal, fetch_person_names, fetch_team_names

log = logging.getLogger(__name__)


def seed():
    init_db()
    db = SessionLocal()
    try:
        db.merge(CompanyRow(id="frontrow", name=COMPANY))
        for c in CHARGES:
            db.merge(ChargeRow(
                charge_id=c.charge_id, company=c.company, source=c.source,
                amount=c.amount, txn_date=c.txn_date, card_last4=c.card_last4,
                cardholder_name=c.cardholder_name, email=c.email,
                order_number=c.order_number, raw_description=c.raw_description,
            ))
        for b in BILLS:
            db.merge(BillRow(
                bill_id=b.bill_id, company=b.company, amount=b.amount,
                txn_date=b.txn_date, balance=b.balance, line_count=b.line_count,
                quantity=b.quantity, vendor=b.vendor, name=b.name, email=b.email,
                order_number=b.order_number, card_last4=b.card_last4, memo=b.memo,
            ))
        db.commit()
        print(f"seeded {len(CHARGES)} charges, {len(BILLS)} bills for {COMPANY}")
    finally:
        db.close()


def _configured_seasons() -> list[str]:
    """Seasons to mirror: the admin-set value if present, else the env var.

    Read at sync time rather than startup so a change in the UI applies to the
    very next run.
    """
    from .models_db import AppSettingRow
    db = SessionLocal()
    try:
        row = db.get(AppSettingRow, "hal_seasons")
        raw = row.value if row and row.value else settings.hal_seasons
    finally:
        db.close()
    return [s.strip() for s in raw.split(",") if s.strip()]


def _name_from_lookup(lookup_name: str) -> str:
    """Best-effort person name from HAL's 'Team email@host' primary field.

    Not a real name, but the email local part usually encodes one, which is
    enough for the cardholder-name matcher to work with.
    """
    import re as _re
    m = _re.search(r"([\w.\-]+)@", str(lookup_name or ""))
    return m.group(1) if m else ""


def _set_status(name: str, state: str, detail: str = "", records: int = 0,
                starting: bool = False):
    from .models_db import SyncStatusRow
    db = SessionLocal()
    try:
        row = db.get(SyncStatusRow, name) or SyncStatusRow(name=name)
        row.state = state
        row.detail = detail[:2000]
        row.records = records
        if starting:
            row.started_at = datetime.utcnow()
            row.finished_at = None
        else:
            row.finished_at = datetime.utcnow()
        db.merge(row)
        db.commit()
    finally:
        db.close()


def sync_profile_emails() -> int:
    """Mirror cardholder name -> email from the Airtable address book.

    Small and slow-changing, so it is replaced wholesale rather than diffed --
    a few hundred rows, and a stale address is worse than a missing one.
    """
    from .integrations.airtable import fetch_person_emails
    from .models_db import ProfileEmailRow

    try:
        mapping = fetch_person_emails()
    except Exception as e:                      # noqa: BLE001
        log.warning("address-book email sync failed: %s", e)
        return 0
    if not mapping:
        return 0

    db = SessionLocal()
    try:
        existing = {(r.name_key, r.email): r for r in db.query(ProfileEmailRow).all()}
        now = datetime.utcnow()
        wanted = {(name, email) for name, emails in mapping.items() for email in emails}

        for name, email in wanted - set(existing):
            db.add(ProfileEmailRow(name_key=name, email=email,
                                   display_name=name, synced_at=now))
        # Gone from the address book means gone here. An address removed there
        # -- or newly shared with another profile, and so dropped as
        # non-identifying -- must stop matching.
        for key in set(existing) - wanted:
            db.delete(existing[key])
        db.commit()
        log.info("mirrored %d addresses across %d cardholders",
                 len(wanted), len(mapping))
        return len(wanted)
    finally:
        db.close()


def sync_hal(incremental_minutes: int | None = None) -> int:
    """Refresh the local HAL mirror from Airtable.

    Upserts by record_id so re-running is idempotent. The person's name is
    parsed out of the Address Book value here (once, at sync time) rather than
    on every charge lookup.
    """
    if not settings.airtable_token:
        log.warning("AIRTABLE_TOKEN not set — skipping HAL sync")
        return 0

    seasons = _configured_seasons()
    mode = f"incremental({incremental_minutes}m)" if incremental_minutes else "full"
    _set_status("hal", "running", f"{mode} seasons={seasons}", starting=True)

    # Resolve linked-record IDs to names first. The REST API returns links as
    # bare record IDs, so without these maps the holder name would be "recXXXX"
    # and no cardholder would ever match.
    people = fetch_person_names()
    teams = fetch_team_names()
    log.info("resolved %d people, %d teams", len(people), len(teams))

    db = SessionLocal()
    n = 0
    unresolved = 0
    try:
        for rec in fetch_hal(seasons=seasons,
                             modified_since_minutes=incremental_minutes):
            ab_ids = rec.get("address_book_ids") or []
            holder = next((people[i] for i in ab_ids if i in people), "")
            if not holder:
                # last resort: the person's name is usually encoded in the
                # buyer email inside Lookup Name ("... akeyaeleni@outlook.com")
                holder = _name_from_lookup(rec.get("lookup_name", ""))
            if not holder:
                unresolved += 1

            team_ids = rec.get("team_ids") or []
            team = next((teams[i] for i in team_ids if i in teams), "")

            db.merge(HalRow(
                record_id=rec["record_id"],
                lookup_name=rec["lookup_name"],
                holder_name=holder,
                address_book=holder,
                team=team,
                plan_type=rec["plan_type"],
                card_program=rec["card_program"],
                has_payment_plan=rec["has_payment_plan"],
                statuses_json=json.dumps(rec["statuses"]),
            ))
            n += 1
            if n % 500 == 0:
                db.commit()
                log.info("synced %d HAL records", n)
        db.commit()
        log.info("HAL sync complete: %d records", n)
        from .models_db import HalRow as _H
        total = db.query(_H).count()
        try:
            from .hal_cache import invalidate as _inv
            _inv()
        except Exception:
            pass
        _set_status("hal", "ok",
                    f"{mode}: {n} record(s) updated, {unresolved} unresolved; "
                    f"{total} in mirror", records=total)
        return n
    except Exception as e:
        # Surface the real reason: bad token, wrong scope, no base access,
        # a filterByFormula error -- all of which otherwise look like "0 records".
        log.exception("HAL sync failed")
        _set_status("hal", "error", f"{type(e).__name__}: {e}", records=n)
        raise
    finally:
        db.close()


def load_hal_index(db) -> HalIndex:
    """Build the in-memory lookup index from the mirrored rows."""
    records = []
    for r in db.query(HalRow).all():
        records.append(HalRecord(
            record_id=r.record_id,
            holder_name=r.holder_name,
            team=r.team,
            statuses=json.loads(r.statuses_json or "{}"),
            plan_type=r.plan_type,
            has_payment_plan=r.has_payment_plan,
        ))
    return HalIndex(records)


def run_daily(companies: list[str] | None = None):
    db = SessionLocal()
    try:
        # Point the classifier at the mirrored HAL data before reconciling.
        from engine.classify import set_hal_index
        set_hal_index(load_hal_index(db))

        targets = companies or [c.name for c in db.query(CompanyRow).all()]
        for company in targets:
            rows = run_reconciliation(db, company)
            print(f"reconciled {company}: {len(rows)} charges")
    finally:
        db.close()


def sync_hal_incremental(minutes: int = 15) -> int:
    """Pick up HAL edits from the last `minutes`.

    Cheap enough to run every few minutes: a normal pass is a handful of
    requests rather than the ~180 a full sync needs. The hourly full sync stays
    as a safety net for anything this misses (deletes, clock skew, a failed run).
    """
    return sync_hal(incremental_minutes=minutes)


def sync_bills_all(days: int = 120) -> int:
    """Refresh open bills from every connected QBO company file."""
    from datetime import date as _d, timedelta as _td
    from .models_db import QboTokenRow, ChargeRow
    from .persistence import sync_bills_from_qbo
    from sqlalchemy import select as _select

    db = SessionLocal()
    total = 0
    try:
        realms = [r.realm_id for r in db.query(QboTokenRow).all()]
        if not realms:
            log.warning("no QBO connection — skipping bills sync")
            return 0
        # Registered companies, not names derived from charges. Deriving from
        # charges meant a company with none yet got no bills -- and a company
        # can't have matches until it has bills, so it never got out of that.
        from .models_db import CompanyRow as _C
        companies = [c.name for c in db.query(_C).filter(_C.active.is_(True)).all()]
        if not companies:
            log.warning("no companies registered — skipping bills sync")
            return 0
        from .review import refresh_scores
        for company in companies:
            for realm in realms:
                total += sync_bills_from_qbo(
                    db, company, realm, since=_d.today() - _td(days=days))
            # New or removed bills change what each unresolved charge could
            # match, so the stored strength is recomputed here -- in the worker,
            # where the full charges x bills scan belongs, rather than on a page
            # load. This is what lets Strength be a real database filter.
            try:
                n = refresh_scores(db, company)
                if n:
                    log.info("rescored %d charge(s) for %s", n, company)
            except Exception:
                log.exception("rescoring failed for %s", company)
        log.info("bills sync complete: %d", total)
        return total
    finally:
        db.close()


def sync_qbo_reference() -> dict:
    """Refresh cached QBO accounts (bank + category) and vendors."""
    from .models_db import QboTokenRow, QboRefRow
    from .integrations.qbo_bills import (list_bank_accounts,
                                         list_expense_accounts, list_vendors)
    db = SessionLocal()
    counts = {"bank": 0, "category": 0, "vendor": 0}
    try:
        realms = [r.realm_id for r in db.query(QboTokenRow).all()]
        if not realms:
            log.warning("no QBO connection — skipping reference sync")
            return counts

        for realm in realms:
            for a in list_bank_accounts(db, realm):
                db.merge(QboRefRow(
                    id=f"account:{realm}:{a['id']}", kind="account", realm_id=realm,
                    qbo_id=a["id"], name=a["name"] or "", account_type=a["type"],
                    subtype=a.get("subtype", ""), usable_for="bank",
                    synced_at=datetime.utcnow()))
                counts["bank"] += 1

            for a in list_expense_accounts(db, realm):
                db.merge(QboRefRow(
                    id=f"account:{realm}:{a['id']}", kind="account", realm_id=realm,
                    qbo_id=a["id"], name=a.get("fully_qualified") or a["name"],
                    account_type=a["type"], subtype=a.get("subtype", ""),
                    usable_for="category", synced_at=datetime.utcnow()))
                counts["category"] += 1

            for v in list_vendors(db, realm):
                db.merge(QboRefRow(
                    id=f"vendor:{realm}:{v['id']}", kind="vendor", realm_id=realm,
                    qbo_id=v["id"], name=v["name"], synced_at=datetime.utcnow()))
                counts["vendor"] += 1
            db.commit()
        # Push renamed accounts through to anything storing a name, so the UI
        # doesn't keep showing what an account used to be called.
        from .models_db import SourceAccountRow, ChargeRow, QboRefRow
        renamed = 0
        for sa in db.query(SourceAccountRow).all():
            if not sa.qbo_account_id:
                continue
            ref = db.query(QboRefRow).filter(
                QboRefRow.kind == "account",
                QboRefRow.qbo_id == sa.qbo_account_id).first()
            if ref and ref.name and ref.name != sa.qbo_account_name:
                log.info("card account renamed in QBO: %r -> %r",
                         sa.qbo_account_name, ref.name)
                sa.qbo_account_name = ref.name
                renamed += 1

        for ch in db.query(ChargeRow).filter(ChargeRow.coded_category_id != "").all():
            ref = db.query(QboRefRow).filter(
                QboRefRow.kind == "account",
                QboRefRow.qbo_id == ch.coded_category_id).first()
            if ref and ref.name and ref.name != ch.coded_category:
                ch.coded_category = ref.name
                renamed += 1
        db.commit()
        counts["renamed"] = renamed
        log.info("QBO reference synced: %s", counts)
        return counts
    finally:
        db.close()


def sync_qbo_all(days: int = 120) -> dict:
    """Everything from QuickBooks: bills, chart of accounts, vendors."""
    out = {"bills": 0, "reference": {}}
    _set_status("qbo", "running", "bills + accounts + vendors", starting=True)
    try:
        out["bills"] = sync_bills_all(days=days)
        out["reference"] = sync_qbo_reference()
        _set_status("qbo", "ok",
                    f"{out['bills']} open bills, "
                    f"{out['reference'].get('category',0)} categories, "
                    f"{out['reference'].get('vendor',0)} vendors",
                    records=out["bills"])
    except Exception as e:
        log.exception("QBO sync failed")
        _set_status("qbo", "error", f"{type(e).__name__}: {e}")
        raise
    return out


def hourly():
    """Everything that should stay fresh on the hour.

    Both syncs are idempotent, so a run that overlaps the previous one or
    repeats work is harmless.
    """
    init_db()
    try:
        sync_hal()                                    # full safety-net pass
    except Exception:
        log.exception("hourly: HAL sync failed")      # keep going to QBO
    try:
        # Hourly, not every five minutes. It used to run inside sync_hal, which
        # the incremental job also calls -- so a table that changes when someone
        # adds a profile was being re-read twelve times an hour. Startup fills it
        # when empty, and Setup has a button for "I need it now".
        sync_profile_emails()
    except Exception:
        log.exception("hourly: address-book email sync failed")
    try:
        sync_qbo_all()
    except Exception:
        log.exception("hourly: QBO sync failed")


if __name__ == "__main__":
    import sys
    init_db()
    if "--sync-hal" in sys.argv:
        sync_hal()
    elif "--sync-bills" in sys.argv:
        sync_bills_all()
    elif "--sync-qbo" in sys.argv:
        sync_qbo_all()
    elif "--hal-incremental" in sys.argv:
        sync_hal_incremental(15)
    elif "--hourly" in sys.argv:
        hourly()
    else:
        seed()
        sync_hal()
        run_daily()
