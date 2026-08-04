"""
Database tables.

The audit_log table is not optional decoration — for a system that writes to
financial records, every consequential action (a match approved, a bill
overwritten, a payment pushed) has to be reconstructable after the fact. It is
what makes "review completed matches without opening each record" possible, and
what an auditor asks for first.
"""

from __future__ import annotations

from datetime import datetime, date
from decimal import Decimal

from sqlalchemy import String, Numeric, Date, DateTime, Integer, Boolean, Float, Text, Index
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class CompanyRow(Base):
    """A serviced company, linked to its QuickBooks file.

    Admin-managed on purpose. The company name is the key every charge, bill,
    card mapping and learned rule is filed under, so letting anyone type it
    means two spellings become two companies and a queue silently shows
    nothing. Set once when the company is onboarded; everyone else picks from
    the list.

    realm_id is the durable identity -- a QuickBooks company can be renamed,
    its realm cannot.
    """
    __tablename__ = "companies"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, unique=True)
    realm_id: Mapped[str] = mapped_column(String, default="", index=True)
    qbo_company_name: Mapped[str] = mapped_column(String, default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ChargeRow(Base):
    __tablename__ = "charges"
    # Every queue read filters on (company, status) and orders by date. Three
    # separate single-column indexes make the planner pick one and scan the
    # rest; one composite index covers the whole access pattern.
    __table_args__ = (
        Index("ix_charges_company_status_date", "company", "status", "txn_date"),
    )
    # charge_id is "{source}:{source_txn_id}" -- stable across re-uploads, so
    # re-running a file or a pull upserts instead of duplicating.
    charge_id: Mapped[str] = mapped_column(String, primary_key=True)
    source_txn_id: Mapped[str] = mapped_column(String, default="", index=True)
    company: Mapped[str] = mapped_column(String, index=True)
    source: Mapped[str] = mapped_column(String)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    txn_date: Mapped[date] = mapped_column(Date)
    card_last4: Mapped[str | None] = mapped_column(String, nullable=True)
    cardholder_name: Mapped[str | None] = mapped_column(String, nullable=True)
    email: Mapped[str | None] = mapped_column(String, nullable=True)
    order_number: Mapped[str | None] = mapped_column(String, nullable=True)
    merchant: Mapped[str] = mapped_column(String, default="")
    # Free-text memo from the upload, carried through to the QuickBooks memo
    # so the ledger keeps whatever context the source file had.
    memo: Mapped[str] = mapped_column(Text, default="")
    # Review state, mirroring the QuickBooks bank feed:
    #   for_review  -> untouched, waiting on a person
    #   categorized -> resolved (matched to a bill, or coded as an expense)
    #   excluded    -> deliberately set aside, not going to the ledger
    status: Mapped[str] = mapped_column(String, default="for_review", index=True)
    resolution: Mapped[str] = mapped_column(String, default="")   # matched|coded
    matched_bill_id: Mapped[str] = mapped_column(String, default="")
    # Both the name (for display) and the QuickBooks id (for posting). The id
    # is what survives a rename in QuickBooks -- posting by name alone breaks
    # the moment someone renames an account, and does so silently until the
    # next write fails.
    coded_category: Mapped[str] = mapped_column(String, default="")
    coded_category_id: Mapped[str] = mapped_column(String, default="")
    coded_vendor: Mapped[str] = mapped_column(String, default="")
    # Snapshot of the bill this charge paid, taken at match time.
    #
    # It cannot be looked up later: sync_bills prunes any bill QuickBooks no
    # longer returns as open, and paying a bill is exactly what makes it stop
    # being open. So within one sync of a successful match the BillRow is gone,
    # and Categorized would show blanks for its own history.
    # Match strength, stored rather than recomputed per request.
    #
    # It used to be derived on the fly, which meant the database couldn't filter
    # or sort by it -- the browser could only narrow the page it already had, so
    # "Strong" over 375 charges showed whatever was strong in the first 200.
    # Refreshed whenever bills or charges change; see review.refresh_scores.
    tier: Mapped[str] = mapped_column(String, default="none", index=True)
    score: Mapped[int] = mapped_column(Integer, default=0)
    # The engine's proposed coding, stored for the same reason as tier: the
    # Vendor and Category columns show it on For Review, and a column you can
    # see but not filter or sort by is half a column.
    suggested_vendor: Mapped[str] = mapped_column(String, default="")
    suggested_category: Mapped[str] = mapped_column(String, default="")
    # Money coming back that is a payment to the card account rather than a
    # merchant refund. Different things to an accountant, and the only signal
    # is in the description, so it's decided once at ingest/scoring time.
    is_card_payment: Mapped[bool] = mapped_column(Boolean, default=False)
    # Charged in Canada. There is no currency or country field in any of the
    # exports, so this is read out of the bank detail and stored once.
    is_canadian: Mapped[bool] = mapped_column(Boolean, default=False)
    # Someone cleared the engine's proposed coding on purpose. Without this the
    # next scoring run simply proposed it again, so Clear appeared to work and
    # then silently undid itself.
    # Cleared per field, not per charge. One flag meant clearing the vendor also
    # blanked the category, and picking either one back brought both back.
    suggestion_cleared: Mapped[bool] = mapped_column(Boolean, default=False)
    vendor_cleared: Mapped[bool] = mapped_column(Boolean, default=False)
    category_cleared: Mapped[bool] = mapped_column(Boolean, default=False)
    matched_bill_no: Mapped[str] = mapped_column(String, default="")
    matched_bill_vendor: Mapped[str] = mapped_column(String, default="")
    matched_bill_date: Mapped[str] = mapped_column(String, default="")
    matched_bill_memo: Mapped[str] = mapped_column(Text, default="")
    coded_vendor_id: Mapped[str] = mapped_column(String, default="")
    resolved_by: Mapped[str] = mapped_column(String, default="")
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Set once the charge has been written to QuickBooks. Its presence is what
    # makes posting idempotent -- a charge with an id here is never posted again.
    qbo_txn_id: Mapped[str] = mapped_column(String, default="", index=True)
    qbo_txn_type: Mapped[str] = mapped_column(String, default="")
    qbo_sync_token: Mapped[str] = mapped_column(String, default="")
    post_error: Mapped[str] = mapped_column(Text, default="")
    posted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    raw_description: Mapped[str] = mapped_column(Text, default="")
    is_credit: Mapped[bool] = mapped_column(Boolean, default=False)
    ingested_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class BillRow(Base):
    __tablename__ = "bills"
    bill_id: Mapped[str] = mapped_column(String, primary_key=True)
    company: Mapped[str] = mapped_column(String, index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    txn_date: Mapped[date] = mapped_column(Date)
    balance: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    line_count: Mapped[int] = mapped_column(Integer, default=1)
    quantity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    vendor: Mapped[str | None] = mapped_column(String, nullable=True)
    vendor_id: Mapped[str] = mapped_column(String, default="")
    doc_number: Mapped[str] = mapped_column(String, default="")   # QBO's Bill no.
    name: Mapped[str | None] = mapped_column(String, nullable=True)
    email: Mapped[str | None] = mapped_column(String, nullable=True)
    order_number: Mapped[str | None] = mapped_column(String, nullable=True)
    card_last4: Mapped[str | None] = mapped_column(String, nullable=True)
    memo: Mapped[str] = mapped_column(Text, default="")


class MatchRow(Base):
    __tablename__ = "match_results"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    company: Mapped[str] = mapped_column(String, index=True)
    charge_id: Mapped[str] = mapped_column(String, index=True)
    bill_id: Mapped[str | None] = mapped_column(String, nullable=True)
    score: Mapped[float] = mapped_column(Float)
    decision: Mapped[str] = mapped_column(String)
    reasons: Mapped[str] = mapped_column(Text, default="")
    overwrite_bill_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    posted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class HalRow(Base):
    """Local mirror of an Airtable HAL season-ticket record.

    Refreshed by the scheduled worker; the classifier reads only from here so a
    daily batch never hits the Airtable API per charge.
    """
    __tablename__ = "hal_records"
    record_id: Mapped[str] = mapped_column(String, primary_key=True)
    lookup_name: Mapped[str] = mapped_column(String, default="")
    holder_name: Mapped[str] = mapped_column(String, index=True)   # parsed person
    address_book: Mapped[str] = mapped_column(String, default="")
    team: Mapped[str] = mapped_column(String, default="")
    plan_type: Mapped[str] = mapped_column(String, default="")
    card_program: Mapped[str] = mapped_column(String, default="")
    has_payment_plan: Mapped[bool] = mapped_column(Boolean, default=False)
    statuses_json: Mapped[str] = mapped_column(Text, default="{}")
    synced_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class CanadaRuleRow(Base):
    """A phrase that marks a charge as Canadian.

    Started as a hardcoded list of teams, venues and city/province pairs. It is
    a table because the list is never finished -- a new venue or a renamed
    merchant shouldn't need a deploy.
    """
    __tablename__ = "canada_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    phrase: Mapped[str] = mapped_column(String, index=True)     # the Input
    # Which field to read: vendor | bank_detail.
    item: Mapped[str] = mapped_column(String, default="bank_detail")
    # How to compare: equals | contains.
    rule: Mapped[str] = mapped_column(String, default="contains")
    # Retained for the rules seeded from the original code, which matched on a
    # city plus its province code.
    kind: Mapped[str] = mapped_column(String, default="phrase")
    provinces: Mapped[str] = mapped_column(String, default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str] = mapped_column(String, default="")


class MatchWeightRow(Base):
    """One tunable weight in the bill-matching score.

    A table rather than constants so the scorecard can be tuned against real
    outcomes without a deploy. Absent keys fall back to the defaults in
    engine/suggest.py, so an empty table behaves exactly as the code does.
    """
    __tablename__ = "match_weights"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    weight: Mapped[float] = mapped_column(Float, default=0.0)


class PaymentRuleRow(Base):
    """A rule that marks a credit as a PAYMENT rather than a refund.

    Same Item/Rule/Input shape as the Canada rules. It exists because the text
    is not always there to read: a WEX payment arrives with nothing but a card
    number in the bank detail, so the only thing that identifies it is which
    card account it came from.

    Only ever consulted for credits. A charge going out is never a payment onto
    the card, whatever the rule says.
    """
    __tablename__ = "payment_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    phrase: Mapped[str] = mapped_column(String, index=True)      # the Input
    # vendor | bank_detail | card_account
    item: Mapped[str] = mapped_column(String, default="bank_detail")
    rule: Mapped[str] = mapped_column(String, default="contains")  # equals | contains
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str] = mapped_column(String, default="")


class ActionRow(Base):
    """One undoable click.

    A batch is ONE row, not one per charge: you pressed Add once over forty
    charges, so one Undo puts all forty back. The `state` column holds what each
    charge looked like BEFORE the click, which is everything needed to reverse
    it without re-deriving anything.

    Deliberately not recorded, because they cannot be reversed: permanent
    deletes from Excluded (the rows are gone) and the bill-payment to expense
    conversion (which already deleted one QuickBooks transaction and created
    another).
    """
    __tablename__ = "actions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    company: Mapped[str] = mapped_column(String, index=True)
    actor: Mapped[str] = mapped_column(String, index=True)
    action: Mapped[str] = mapped_column(String)          # match | code | exclude | edit
    summary: Mapped[str] = mapped_column(String, default="")
    # JSON: [{charge_id, status, resolution, coded_category, ..., qbo_txn_id}]
    state: Mapped[str] = mapped_column(Text, default="[]")
    at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    undone_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ProfileEmailRow(Base):
    """Cardholder name -> email, mirrored from the Airtable address book.

    Only Divvy's export includes an email. Slash and WEX name the cardholder but
    not their address, so the email signal -- worth 3% on its own, but decisive
    when amount and date are ambiguous -- could never fire for two of the three
    card programs. This table supplies it.
    """
    __tablename__ = "profile_emails"

    # One row per (name, address): a profile routinely buys under several, and
    # a bill can name any of them.
    name_key: Mapped[str] = mapped_column(String, primary_key=True)
    email: Mapped[str] = mapped_column(String, primary_key=True)
    display_name: Mapped[str] = mapped_column(String, default="")
    synced_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class SourceAccountRow(Base):
    """Card program -> QuickBooks account.

    This is the primary mapping. Slash, Divvy and WEX each issue effectively
    unlimited virtual cards, but every card in a program settles to ONE parent
    account in QuickBooks. So the mapping belongs at the program level: set it
    once and every card -- including cards that don't exist yet -- resolves
    automatically. Per-card overrides (CardRow) are the rare exception.
    """
    __tablename__ = "source_accounts"
    id: Mapped[str] = mapped_column(String, primary_key=True)   # "{company}:{source}"
    company: Mapped[str] = mapped_column(String, index=True)
    source: Mapped[str] = mapped_column(String)
    nickname: Mapped[str] = mapped_column(String, default="")
    # Which CSV layout this program exports.
    #
    # This used to be inferred from `source`, which meant the source key had to
    # literally be "slash" / "divvy" / "wex" -- so it couldn't just be a name
    # someone chose, and a fourth card account was impossible without a code
    # change. Naming the format separately frees the key to be arbitrary.
    # Empty means "same as source", which is what the original three are.
    file_format: Mapped[str] = mapped_column(String, default="")
    # A virtual-card program: Slash and Divvy issue a fresh number per purchase,
    # so a "card" here is one of thousands rather than a physical card someone
    # carries. Worth flagging in the picker, because it changes what a CC Last 4
    # actually identifies.
    is_virtual: Mapped[bool] = mapped_column(Boolean, default=False)
    qbo_account_id: Mapped[str] = mapped_column(String, default="")
    qbo_account_name: Mapped[str] = mapped_column(String, default="")
    realm_id: Mapped[str] = mapped_column(String, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class CardRow(Base):
    """A credit card, named by your team and mapped to a QBO account.

    This is what tells the engine which QuickBooks bank/credit-card account a
    charge belongs to. Without it there is no way to post a transaction to the
    right ledger account, so write-back depends on this being filled in.

    Keyed on last-4 + source because the same last-4 could theoretically appear
    on two different card programs.
    """
    __tablename__ = "cards"
    id: Mapped[str] = mapped_column(String, primary_key=True)   # "{source}:{last4}"
    last4: Mapped[str] = mapped_column(String, index=True)
    source: Mapped[str] = mapped_column(String, default="")     # wex|divvy|slash
    nickname: Mapped[str] = mapped_column(String, default="")   # your own name for it
    holder: Mapped[str] = mapped_column(String, default="")     # seen on charges
    company: Mapped[str] = mapped_column(String, default="")
    qbo_account_id: Mapped[str] = mapped_column(String, default="")
    qbo_account_name: Mapped[str] = mapped_column(String, default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class UserRow(Base):
    """A person who can sign in.

    `username` is the email address -- one identity, and password reset has
    somewhere to send to without a second field that can drift out of sync.
    """
    __tablename__ = "users"
    username: Mapped[str] = mapped_column(String, primary_key=True)
    # Separate from the username on purpose: people sign in with the handle
    # they already know, while codes and reset links need somewhere real to go.
    # Required on every account, so nobody ends up unable to reset a password.
    email: Mapped[str] = mapped_column(String, default="", index=True)
    full_name: Mapped[str] = mapped_column(String, default="")
    twofa_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    password_hash: Mapped[str] = mapped_column(String)
    role: Mapped[str] = mapped_column(String, default="reviewer")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_login: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class UploadRow(Base):
    """Record of an ingested file or scheduled pull, for traceability."""
    __tablename__ = "uploads"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String)          # wex | divvy | slash
    method: Mapped[str] = mapped_column(String)          # upload | pull
    filename: Mapped[str] = mapped_column(String, default="")
    company: Mapped[str] = mapped_column(String)
    rows_seen: Mapped[int] = mapped_column(Integer, default=0)
    rows_new: Mapped[int] = mapped_column(Integer, default=0)
    rows_duplicate: Mapped[int] = mapped_column(Integer, default=0)
    actor: Mapped[str] = mapped_column(String, default="system")
    at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class QboTokenRow(Base):
    """OAuth tokens, one row per QBO company file (realm).

    Realm-keyed on purpose: every API call names its realm, so a call meant for
    the test company cannot reach a production file.
    """
    __tablename__ = "qbo_tokens"
    realm_id: Mapped[str] = mapped_column(String, primary_key=True)
    access_token: Mapped[str] = mapped_column(Text)
    refresh_token: Mapped[str] = mapped_column(Text)
    access_expires_at: Mapped[datetime] = mapped_column(DateTime)
    refresh_expires_at: Mapped[datetime] = mapped_column(DateTime)
    label: Mapped[str] = mapped_column(String, default="")
    company_name: Mapped[str] = mapped_column(String, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class OAuthStateRow(Base):
    """Short-lived CSRF state values issued at authorize time."""
    __tablename__ = "oauth_states"
    state: Mapped[str] = mapped_column(String, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class QboRefRow(Base):
    """Cached QuickBooks reference data: accounts and vendors.

    Cached rather than fetched live because the coding dropdowns need them on
    every page load, and a vendor list can run to thousands of rows. Refreshed
    on a schedule and on demand.
    """
    __tablename__ = "qbo_reference"
    id: Mapped[str] = mapped_column(String, primary_key=True)   # "{kind}:{realm}:{qbo_id}"
    kind: Mapped[str] = mapped_column(String, index=True)       # account|vendor
    realm_id: Mapped[str] = mapped_column(String, default="")
    qbo_id: Mapped[str] = mapped_column(String, default="")
    name: Mapped[str] = mapped_column(String, default="")
    account_type: Mapped[str] = mapped_column(String, default="")
    subtype: Mapped[str] = mapped_column(String, default="")
    usable_for: Mapped[str] = mapped_column(String, default="")  # bank|category
    synced_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class LearnedRuleRow(Base):
    """A coding rule learned from what reviewers actually chose.

    Keyed on a normalized merchant so card descriptors that differ only by
    order number collapse to one rule.
    """
    __tablename__ = "learned_rules"
    id: Mapped[str] = mapped_column(String, primary_key=True)   # "{company}|{key}"
    company: Mapped[str] = mapped_column(String, index=True)
    merchant_key: Mapped[str] = mapped_column(String, index=True)
    sample_merchant: Mapped[str] = mapped_column(String, default="")
    category: Mapped[str] = mapped_column(String, default="")
    vendor: Mapped[str] = mapped_column(String, default="")
    confirmations: Mapped[int] = mapped_column(Integer, default=0)
    disagreements: Mapped[int] = mapped_column(Integer, default=0)
    auto_apply: Mapped[bool] = mapped_column(Boolean, default=False)
    last_actor: Mapped[str] = mapped_column(String, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AppSettingRow(Base):
    """Runtime settings an admin can change without a redeploy."""
    __tablename__ = "app_settings"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_by: Mapped[str] = mapped_column(String, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AuthTokenRow(Base):
    """Short-lived, single-use tokens for password reset and 2FA.

    Stored hashed: a leaked database shouldn't hand over working reset links.
    """
    __tablename__ = "auth_tokens"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String, index=True)
    kind: Mapped[str] = mapped_column(String)            # reset | twofa
    token_hash: Mapped[str] = mapped_column(String, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class TrustedDeviceRow(Base):
    """A browser that has passed 2FA recently, so it isn't asked again."""
    __tablename__ = "trusted_devices"
    id: Mapped[str] = mapped_column(String, primary_key=True)   # hashed cookie
    username: Mapped[str] = mapped_column(String, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class SyncStatusRow(Base):
    """Outcome of the last background sync, so failures are visible.

    A background task that dies silently is worse than one that never ran --
    the UI would show 0 records forever with no explanation.
    """
    __tablename__ = "sync_status"
    name: Mapped[str] = mapped_column(String, primary_key=True)   # "hal" | "bills"
    state: Mapped[str] = mapped_column(String, default="idle")    # running|ok|error
    detail: Mapped[str] = mapped_column(Text, default="")
    records: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class UserPrefRow(Base):
    """Per-user UI preferences — column order, visibility, widths.

    Keyed by username rather than stored in the browser: someone signing in
    from a second machine should see the layout they set up, and a cleared
    cache shouldn't silently reset it.
    """
    __tablename__ = "user_prefs"
    __table_args__ = (
        Index("ix_user_prefs_user_key", "username", "key", unique=True),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String, index=True)
    key: Mapped[str] = mapped_column(String)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AuditRow(Base):
    __tablename__ = "audit_log"
    # The log is always read newest-first for one company. Without this the
    # ORDER BY at DESC sorts every row for that company on each read, which
    # matters because the audit table grows faster than the charges do --
    # several entries per resolved charge.
    __table_args__ = (
        Index("ix_audit_company_at", "company", "at"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    company: Mapped[str] = mapped_column(String, index=True)
    action: Mapped[str] = mapped_column(String)          # e.g. "bill_overwrite", "push"
    charge_id: Mapped[str | None] = mapped_column(String, nullable=True)
    bill_id: Mapped[str | None] = mapped_column(String, nullable=True)
    detail: Mapped[str] = mapped_column(Text, default="")
    dry_run: Mapped[bool] = mapped_column(Boolean, default=True)
    actor: Mapped[str] = mapped_column(String, default="system")
    at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
