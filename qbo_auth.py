import os
import time
import base64
import requests

# ── QBO OAuth 2.0 config ──────────────────────────────────────────────────────
CLIENT_ID     = os.environ.get("QBO_CLIENT_ID", "ABB2nisdNCJzxAcF8dba5q6NHMT3A3P0EF1ENEh8bV69y2177M")
CLIENT_SECRET = os.environ.get("QBO_CLIENT_SECRET", "G2TKvK5WxSPIsjiTtZ7ogVpg9PmvJD9odjLSWAmg")
REDIRECT_URI  = "https://applied-payments-production.up.railway.app/qbo/callback"
SCOPE         = "com.intuit.quickbooks.accounting"
AUTH_URL      = "https://appcenter.intuit.com/connect/oauth2"
TOKEN_URL     = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
REVOKE_URL    = "https://developer.api.intuit.com/v2/oauth2/tokens/revoke"
API_BASE      = "https://quickbooks.api.intuit.com"


class QBOError(Exception):
    """A QuickBooks API error carrying a readable message from the QBO response."""
    def __init__(self, message, detail="", code=None, status=None):
        super().__init__(message)
        self.message = message
        self.detail = detail
        self.code = code
        self.status = status


def _extract_qbo_error(resp):
    """Pull a readable (message, detail, code) out of a QBO error response."""
    try:
        body = resp.json()
    except Exception:
        return (resp.text[:300].strip() or f"HTTP {resp.status_code}", "", None)
    fault = body.get("Fault") or body.get("fault") or {}
    errors = fault.get("Error") or fault.get("error") or []
    if errors:
        err = errors[0]
        msg = (err.get("Message") or err.get("message") or "").strip()
        detail = (err.get("Detail") or err.get("detail") or "").strip()
        code = err.get("code") or err.get("Code")
        return (msg or "QuickBooks rejected the request", detail, code)
    return (resp.text[:300].strip() or f"HTTP {resp.status_code}", "", None)


def get_auth_url(state: str) -> str:
    """Build the Intuit OAuth authorization URL."""
    params = (
        f"?client_id={CLIENT_ID}"
        f"&response_type=code"
        f"&scope={SCOPE}"
        f"&redirect_uri={REDIRECT_URI}"
        f"&state={state}"
    )
    return AUTH_URL + params


def exchange_code(code: str) -> dict:
    """Exchange authorization code for tokens."""
    creds = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    resp = requests.post(TOKEN_URL, headers={
        "Authorization": f"Basic {creds}",
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    }, data={
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
    })
    resp.raise_for_status()
    data = resp.json()
    data["expires_at"] = time.time() + data.get("expires_in", 3600)
    return data


def refresh_token(token_data: dict) -> dict:
    """Refresh an expired access token."""
    creds = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    resp = requests.post(TOKEN_URL, headers={
        "Authorization": f"Basic {creds}",
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    }, data={
        "grant_type": "refresh_token",
        "refresh_token": token_data["refresh_token"],
    })
    resp.raise_for_status()
    data = resp.json()
    data["expires_at"] = time.time() + data.get("expires_in", 3600)
    return data


def get_valid_token(token_data: dict) -> dict:
    """Return a valid access token, refreshing if needed."""
    if time.time() >= token_data.get("expires_at", 0) - 60:
        return refresh_token(token_data)
    return token_data


def api_get(token_data: dict, realm_id: str, path: str) -> dict:
    token_data = get_valid_token(token_data)
    resp = requests.get(
        f"{API_BASE}/v3/company/{realm_id}/{path}",
        headers={
            "Authorization": f"Bearer {token_data['access_token']}",
            "Accept": "application/json",
        }
    )
    if not resp.ok:
        msg, detail, code = _extract_qbo_error(resp)
        raise QBOError(msg, detail=detail, code=code, status=resp.status_code)
    return resp.json()


def api_post(token_data: dict, realm_id: str, path: str, payload: dict) -> dict:
    token_data = get_valid_token(token_data)
    resp = requests.post(
        f"{API_BASE}/v3/company/{realm_id}/{path}",
        headers={
            "Authorization": f"Bearer {token_data['access_token']}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        json=payload
    )
    if not resp.ok:
        print(f"QBO API error {resp.status_code}: {resp.text}")
        print(f"Payload sent: {payload}")
        msg, detail, code = _extract_qbo_error(resp)
        raise QBOError(msg, detail=detail, code=code, status=resp.status_code)
    return resp.json()
