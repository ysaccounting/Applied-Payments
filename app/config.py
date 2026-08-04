"""
Configuration — everything environment-driven. No secrets in code, ever.

The two settings that matter for safety:
  DRY_RUN            default TRUE  — write-back only logs what it *would* post.
  QBO_WRITE_ENABLED  default FALSE — a second, explicit switch before any real
                                     write to a live ledger is even possible.

Both must be deliberately flipped to write to QuickBooks. Belt and suspenders,
on purpose: the cost of an accidental write to real books is high, so the
default posture is "cannot touch the ledger".
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Local dev uses SQLite; Railway injects a Postgres DATABASE_URL.
    database_url: str = "sqlite:///./ys_local.db"

    # --- safety gates ---
    dry_run: bool = True
    qbo_write_enabled: bool = False

    # --- QuickBooks (per-company creds resolved at runtime; these are app-level) ---
    qbo_client_id: str = ""
    qbo_client_secret: str = ""
    qbo_environment: str = "sandbox"      # never default to production
    qbo_redirect_uri: str = "http://localhost:8000/qbo/callback"
    # Safety: only these realms may ever be connected or called. Empty means
    # "none authorized yet" -- set it explicitly to your TEST company realm.
    qbo_allowed_realms: str = ""

    # --- Airtable (HAL season-ticket mirror) ---
    airtable_token: str = ""
    hal_seasons: str = "25/26,26/27,27/28"     # comma-separated seasons to mirror

    # --- matching thresholds (the business dial) ---
    auto_threshold: float = 0.85
    review_threshold: float = 0.55

    # --- auth ---
    jwt_secret: str = ""                  # no default: unset means no tokens
    # Sessions end after this long with no activity. The token is reissued on
    # each request, so an active person is never interrupted.
    session_idle_minutes: int = 30
    jwt_expire_minutes: int = 30          # kept in step with the idle window
    reset_token_minutes: int = 30
    twofa_code_minutes: int = 10
    twofa_trust_hours: int = 12           # don't re-ask this browser for 12h
    require_twofa: bool = True
    app_base_url: str = ""                # for building reset links

    # --- email (password reset + 2FA) ---
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    # Display name on outgoing mail. Without it recipients see the bare address
    # (josh@ystickets.com), which looks like a person wrote it by hand.
    smtp_from_name: str = "The Engine"
    smtp_use_ssl: bool = False
    seed_admin_user: str = ""
    seed_admin_password: str = ""

    app_env: str = "development"


settings = Settings()
