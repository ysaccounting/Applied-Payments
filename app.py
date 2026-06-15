import os
import uuid
import threading
import time
import secrets
import hmac
from flask import Flask, request, jsonify, send_file, render_template, redirect, session, Response
from processor import process
from qbo_auth import get_auth_url, exchange_code, get_valid_token
from qbo_push import push_bank_deposit, push_receive_payments, get_company_info
import token_store

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", secrets.token_hex(32))

# ── Access control (configured via Railway environment variables) ─────────────
# APP_PASSWORD set        -> whole app requires Basic Auth (username defaults to "team")
# QBO_ADMIN_PASSWORD set  -> connecting/disconnecting QBO requires the admin password
# If a value is unset, that gate is simply off, so nothing breaks before you set them.
APP_USERNAME = os.environ.get("APP_USERNAME", "team")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
QBO_ADMIN_PASSWORD = os.environ.get("QBO_ADMIN_PASSWORD", "")


def _ct_eq(a, b):
    """Constant-time string compare."""
    return hmac.compare_digest(str(a), str(b))


def _admin_ok(provided):
    """True if the admin gate is off, or the supplied password matches."""
    if not QBO_ADMIN_PASSWORD:
        return True
    return bool(provided) and _ct_eq(provided, QBO_ADMIN_PASSWORD)


@app.before_request
def _require_app_login():
    """App-wide Basic Auth. Off until APP_PASSWORD is set in Railway."""
    if not APP_PASSWORD:
        return
    # Intuit redirects here after OAuth and can't send Basic Auth; it's protected
    # instead by the OAuth state/code, so exempt just this path.
    if request.path == "/qbo/callback":
        return
    auth = request.authorization
    if auth and _ct_eq(auth.username, APP_USERNAME) and _ct_eq(auth.password, APP_PASSWORD):
        return
    return Response("Authentication required.", 401,
                    {"WWW-Authenticate": 'Basic realm="Applied Payments"'})

# Use Railway Volume at /data if available, otherwise fall back to local folders
DATA_DIR = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "/data")
UPLOAD_FOLDER = os.path.join(DATA_DIR, "uploads")
OUTPUT_FOLDER = os.path.join(DATA_DIR, "outputs")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)


def cleanup_old_files():
    """Delete files older than 24 hours from the volume."""
    while True:
        time.sleep(3600)
        now = time.time()
        for folder in [UPLOAD_FOLDER, OUTPUT_FOLDER]:
            for fname in os.listdir(folder):
                fpath = os.path.join(folder, fname)
                if os.path.isfile(fpath) and now - os.path.getmtime(fpath) > 86400:
                    os.remove(fpath)


threading.Thread(target=cleanup_old_files, daemon=True).start()


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/process", methods=["POST"])
def process_file():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    f = request.files["file"]
    if not f.filename.endswith(".csv"):
        return jsonify({"error": "File must be a .csv"}), 400

    session_id = str(uuid.uuid4())
    csv_path = os.path.join(UPLOAD_FOLDER, f"{session_id}_{f.filename}")
    f.save(csv_path)

    # Handle optional EvoPay file for TicketEvolution uploads
    evopay_path = None
    evopay_file = request.files.get("evopay_file")
    if evopay_file and evopay_file.filename:
        evopay_ext = os.path.splitext(evopay_file.filename)[1].lower()
        evopay_path = os.path.join(UPLOAD_FOLDER, f"{session_id}_evopay{evopay_ext}")
        evopay_file.save(evopay_path)

    try:
        result = process(csv_path, f.filename, evopay_path=evopay_path)
    except Exception as e:
        if os.path.exists(csv_path):
            os.remove(csv_path)
        if evopay_path and os.path.exists(evopay_path):
            os.remove(evopay_path)
        return jsonify({"error": str(e)}), 500
    finally:
        if evopay_path and os.path.exists(evopay_path):
            os.remove(evopay_path)

    memo = result["memo"]
    date_range = result.get("date_range_str")
    if date_range:
        # Strip the date from the end of memo (e.g. YS_TicketEvolution_06-05-26 -> YS_TicketEvolution)
        import re as _re
        memo_base = _re.sub(r'_\d{1,2}-\d{1,2}-\d{2,4}.*$', '', memo).rstrip('_')
        file_prefix = f"{memo_base} {date_range}"
    else:
        file_prefix = memo

    applied_name = f"{file_prefix} Applied Payments.xlsx"
    deposit_name = f"{file_prefix} Bank Deposit.xlsx"
    applied_path = os.path.join(OUTPUT_FOLDER, f"{session_id}__applied__{applied_name}")
    deposit_path = os.path.join(OUTPUT_FOLDER, f"{session_id}__deposit__{deposit_name}")

    result["wb_applied"].save(applied_path)
    result["wb_deposit"].save(deposit_path)

    # Save Receive Payment file for TE
    receive_path = None
    receive_name = None
    if result.get("wb_receive"):
        receive_name = f"{file_prefix} Receive Payment.xlsx"
        receive_path = os.path.join(OUTPUT_FOLDER, f"{session_id}__receive__{receive_name}")
        result["wb_receive"].save(receive_path)

    if os.path.exists(csv_path):
        os.remove(csv_path)

    # Build summary data for QBO push
    deposit_rows_data = []
    bd_source = result.get("all_bd_rows_data", [])
    for row in bd_source:
        deposit_rows_data.append({
            "account": row.get("Account", ""),
            "amount": float(row.get("Amount", 0)),
            "date": row.get("Date", ""),
            "deposit_num": row.get("Deposit #", ""),
            "bank_account": row.get("Bank Account", ""),
            "network": row.get("Network", result.get("deposit_network_full", "")),
        })

    rp_rows_data = []
    for row in result.get("rp_rows_data", []):
        rp_rows_data.append({
            "memo": row.get("Deposit #", ""),
            "amount": float(row.get("Amount", 0)),
            "date": row.get("Date", ""),
            "deposit_num": row.get("Deposit #", ""),
            "network": result.get("deposit_network_full", ""),
            "bank_account": result.get("bank_account", "FFB Chkg"),
        })

    return jsonify({
        "session_id": session_id,
        "memo": memo,
        "file_prefix": file_prefix,
        "has_receive_payment": receive_path is not None,
        "receive_payment_amt": result["receive_payment_amt"],
        "bank_deposit_total": result["bank_deposit_total"],
        "combined_total": result["combined_total"],
        "deposit_rows": deposit_rows_data,
        "rp_rows": rp_rows_data,
    })


@app.route("/download/<session_id>/<file_type>")
def download(session_id, file_type):
    # Find file directly on disk by session_id and type — no in-memory index needed
    if file_type == "applied":
        marker = "__applied__"
    elif file_type == "deposit":
        marker = "__deposit__"
    elif file_type == "receive":
        marker = "__receive__"
    else:
        return "Invalid file type", 400
    matched_path = None
    matched_name = None

    for fname in os.listdir(OUTPUT_FOLDER):
        if fname.startswith(session_id) and marker in fname:
            matched_path = os.path.join(OUTPUT_FOLDER, fname)
            matched_name = fname.split(marker, 1)[1]
            break

    if not matched_path or not os.path.exists(matched_path):
        return "File not found — please re-process your file.", 404

    return send_file(matched_path, as_attachment=True, download_name=matched_name)


@app.route("/reset/<session_id>", methods=["POST"])
def reset(session_id):
    for fname in os.listdir(OUTPUT_FOLDER):
        if fname.startswith(session_id):
            fpath = os.path.join(OUTPUT_FOLDER, fname)
            if os.path.exists(fpath):
                os.remove(fpath)
    return jsonify({"ok": True})


# ── QBO OAuth routes ─────────────────────────────────────────────────────────

@app.route("/qbo/connect")
def qbo_connect():
    """Initiate QBO OAuth flow."""
    state = secrets.token_urlsafe(16)
    if not _admin_ok(request.args.get("admin_key", "")):
        return redirect("/?qbo_error=admin_required")
    session["qbo_state"] = state
    # Store session_id so we know which session to push after auth
    session["pending_session_id"] = request.args.get("session_id", "")
    session["push_type"] = request.args.get("push_type", "deposit")
    return redirect(get_auth_url(state))


@app.route("/qbo/callback")
def qbo_callback():
    """Handle QBO OAuth callback."""
    code = request.args.get("code")
    state = request.args.get("state")
    realm_id = request.args.get("realmId")
    error = request.args.get("error")

    if error or not code:
        return redirect("/?qbo_error=auth_failed")

    if state != session.get("qbo_state"):
        return redirect("/?qbo_error=invalid_state")

    try:
        from qbo_auth import exchange_code
        token_data = exchange_code(code)
        # Persist as the single shared connection for ALL users (admin connects once).
        token_store.save_connection(token_data, realm_id)
    except Exception as e:
        return redirect(f"/?qbo_error={str(e)}")

    return redirect("/?qbo_connected=1")


@app.route("/qbo/status")
def qbo_status():
    """Check if the shared QBO connection is live (same for every user)."""
    token_data, realm_id = token_store.get_active_connection()
    if not token_data:
        return jsonify({"connected": False, "admin_gate": bool(QBO_ADMIN_PASSWORD)})
    try:
        info = get_company_info(token_data, realm_id)
        company = info.get("CompanyInfo", {}).get("CompanyName", "Connected")
        return jsonify({"connected": True, "company": company, "admin_gate": bool(QBO_ADMIN_PASSWORD)})
    except Exception:
        return jsonify({"connected": False, "admin_gate": bool(QBO_ADMIN_PASSWORD)})


@app.route("/qbo/disconnect", methods=["POST"])
def qbo_disconnect():
    """Disconnect QBO. Clears the SHARED connection for every user — admin only."""
    data = request.get_json(silent=True) or {}
    provided = data.get("admin_key", "") or request.headers.get("X-Admin-Key", "")
    if not _admin_ok(provided):
        return jsonify({"ok": False, "error": "Admin password required to disconnect."}), 403
    token_store.clear_connection()
    return jsonify({"ok": True})


@app.route("/qbo/push/<session_id>/<push_type>", methods=["POST"])
def qbo_push(session_id, push_type):
    """Push entries to QBO using the shared company connection."""
    try:
        token_data, realm_id = token_store.get_active_connection()
    except Exception:
        token_data, realm_id = None, None

    if not token_data:
        return jsonify({"error": "QuickBooks isn't connected. Connect it first, then try again.",
                        "auth_required": True}), 401

    data = request.get_json()
    if not data:
        return jsonify({"error": "No data provided"}), 400

    try:
        if push_type == "deposit":
            results = push_bank_deposit(token_data, realm_id, data)
        elif push_type == "receive":
            results = push_receive_payments(token_data, realm_id, data)
        else:
            return jsonify({"error": "Invalid push type"}), 400

        errors = [r for r in results if r.get("status") == "error"]
        return jsonify({
            "ok": len(errors) == 0,
            "results": results,
            "errors": errors,
        })
    except Exception as e:
        from qbo_push import humanize_error
        msg = humanize_error(e)
        return jsonify({"ok": False, "results": [], "errors": [{"error": msg}], "error": msg}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
