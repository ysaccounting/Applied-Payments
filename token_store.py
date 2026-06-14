"""
Single shared QuickBooks connection for the whole app.

QBO allows exactly one connection (one access/refresh token pair) per company.
Instead of storing tokens per browser session — which makes every user re-authorize
and invalidate the previous person's tokens — we persist ONE connection here,
on the Railway volume, and every user shares it. An admin connects once.

A file lock (plus an in-process lock) serializes token refreshes so concurrent
users can't refresh at the same time and clobber each other's token (minting a new
token invalidates the old one).
"""
import os
import json
import time
import fcntl
import threading
import contextlib

from qbo_auth import refresh_token

DATA_DIR = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "/data")
TOKEN_PATH = os.path.join(DATA_DIR, "qbo_token.json")
LOCK_PATH = os.path.join(DATA_DIR, "qbo_token.lock")

_thread_lock = threading.Lock()


def _ensure_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


@contextlib.contextmanager
def _exclusive_lock():
    """In-process + cross-process exclusive lock for read-modify-write of the token."""
    _ensure_dir()
    with _thread_lock:
        f = open(LOCK_PATH, "w")
        try:
            fcntl.flock(f, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
            f.close()


def load_connection():
    """Return {'token': {...}, 'realm_id': '...'} or None if nobody has connected."""
    try:
        with open(TOKEN_PATH) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    if data.get("token") and data.get("realm_id"):
        return data
    return None


def save_connection(token_data, realm_id):
    """Persist the shared connection atomically (admin connect, or after a refresh)."""
    _ensure_dir()
    payload = {"token": token_data, "realm_id": realm_id, "updated_at": time.time()}
    tmp = TOKEN_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f)
    os.replace(tmp, TOKEN_PATH)


def clear_connection():
    """Remove the shared connection (disconnects QBO for everyone)."""
    try:
        os.remove(TOKEN_PATH)
    except FileNotFoundError:
        pass


def is_connected():
    return load_connection() is not None


def _needs_refresh(token_data):
    return time.time() >= token_data.get("expires_at", 0) - 60


def get_active_connection():
    """
    Return (token_data, realm_id) for the shared connection, refreshing and
    persisting the access token first if it's near expiry. Returns (None, None)
    if no one has connected yet.
    """
    conn = load_connection()
    if not conn:
        return None, None

    token_data = conn["token"]
    realm_id = conn["realm_id"]

    if _needs_refresh(token_data):
        with _exclusive_lock():
            # Re-read inside the lock: another worker may have just refreshed.
            conn = load_connection()
            if not conn:
                return None, None
            token_data = conn["token"]
            realm_id = conn["realm_id"]
            if _needs_refresh(token_data):
                token_data = refresh_token(token_data)
                save_connection(token_data, realm_id)

    return token_data, realm_id
