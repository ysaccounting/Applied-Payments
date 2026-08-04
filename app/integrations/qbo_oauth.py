"""
QuickBooks Online OAuth 2.0.

Intuit uses three-legged OAuth: the user authorizes in a browser, Intuit
redirects back with a short-lived code, and the app exchanges that for tokens.

What matters operationally:
  - Access tokens last ~1 hour. Refresh tokens last ~100 days and ROTATE on
    every refresh, so the new one must be persisted or the connection dies.
  - Tokens are per *realm* (per QBO company file). A realm is the unit of
    authorization, so tokens are stored keyed by realm_id and every API call
    names its realm explicitly. This is what stops a call intended for the test
    company from ever reaching a production file.
  - `state` is a CSRF guard: generated at authorize time, verified at callback.
"""

from __future__ import annotations

import base64
import logging
import secrets
from datetime import datetime, timedelta
from urllib.parse import urlencode

import httpx

from ..config import settings

log = logging.getLogger(__name__)

AUTH_URL = "https://appcenter.intuit.com/connect/oauth2"
TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
REVOKE_URL = "https://developer.api.intuit.com/v2/oauth2/tokens/revoke"

# Accounting scope covers reading bills and writing bill payments.
SCOPE = "com.intuit.quickbooks.accounting"

API_BASE = {
    "sandbox": "https://sandbox-quickbooks.api.intuit.com",
    "production": "https://quickbooks.api.intuit.com",
}


class OAuthError(RuntimeError):
    pass


def api_base() -> str:
    return API_BASE.get(settings.qbo_environment, API_BASE["sandbox"])


def _basic_auth_header() -> str:
    raw = f"{settings.qbo_client_id}:{settings.qbo_client_secret}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def build_authorize_url(state: str | None = None) -> tuple[str, str]:
    """Return (url, state). Send the user to `url`; keep `state` to verify."""
    if not settings.qbo_client_id:
        raise OAuthError("QBO_CLIENT_ID is not configured")
    state = state or secrets.token_urlsafe(24)
    params = {
        "client_id": settings.qbo_client_id,
        "response_type": "code",
        "scope": SCOPE,
        "redirect_uri": settings.qbo_redirect_uri,
        "state": state,
    }
    return f"{AUTH_URL}?{urlencode(params)}", state


def exchange_code(code: str, realm_id: str) -> dict:
    """Swap the authorization code for access + refresh tokens."""
    r = httpx.post(
        TOKEN_URL,
        headers={"Authorization": _basic_auth_header(),
                 "Accept": "application/json",
                 "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "authorization_code",
              "code": code,
              "redirect_uri": settings.qbo_redirect_uri},
        timeout=30,
    )
    if r.status_code != 200:
        raise OAuthError(f"token exchange failed ({r.status_code}): {r.text[:300]}")
    return _shape(r.json(), realm_id)


def refresh(refresh_token: str, realm_id: str) -> dict:
    """Refresh an expired access token.

    Intuit ROTATES the refresh token here -- the response carries a new one and
    the old becomes invalid. Failing to persist it silently breaks the
    connection about an hour later, which is a nasty way to find out.
    """
    r = httpx.post(
        TOKEN_URL,
        headers={"Authorization": _basic_auth_header(),
                 "Accept": "application/json",
                 "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "refresh_token", "refresh_token": refresh_token},
        timeout=30,
    )
    if r.status_code != 200:
        raise OAuthError(f"token refresh failed ({r.status_code}): {r.text[:300]}")
    return _shape(r.json(), realm_id)


def revoke(token: str) -> bool:
    r = httpx.post(
        REVOKE_URL,
        headers={"Authorization": _basic_auth_header(),
                 "Content-Type": "application/json"},
        json={"token": token},
        timeout=30,
    )
    return r.status_code == 200


def _shape(payload: dict, realm_id: str) -> dict:
    now = datetime.utcnow()
    return {
        "realm_id": realm_id,
        "access_token": payload["access_token"],
        "refresh_token": payload["refresh_token"],
        # Refresh a little early so a long batch can't expire mid-run.
        "access_expires_at": now + timedelta(seconds=int(payload.get("expires_in", 3600)) - 120),
        "refresh_expires_at": now + timedelta(
            seconds=int(payload.get("x_refresh_token_expires_in", 8726400))),
    }
