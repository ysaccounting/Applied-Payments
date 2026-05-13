import os
import uuid
import threading
import time
from flask import Flask, request, jsonify, send_file, render_template
from processor import process

app = Flask(__name__)

# Use Railway Volume at /data if available, otherwise fall back to local folders
DATA_DIR = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "/data")
UPLOAD_FOLDER = os.path.join(DATA_DIR, "uploads")
OUTPUT_FOLDER = os.path.join(DATA_DIR, "outputs")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

# In-memory session index (maps session_id -> file paths + names)
# Sessions are rebuilt from disk on startup so downloads survive restarts
sessions = {}


def index_existing_files():
    """On startup, re-index any output files already on the volume."""
    for fname in os.listdir(OUTPUT_FOLDER):
        if not fname.endswith(".xlsx"):
            continue
        # filename format: <session_id>_<memo> Applied Payments.xlsx
        #                  <session_id>_<memo> Bank Deposit.xlsx
        parts = fname.split("_", 1)
        if len(parts) < 2:
            continue
        sid = parts[0]
        if sid not in sessions:
            sessions[sid] = {}
        fpath = os.path.join(OUTPUT_FOLDER, fname)
        if "Applied Payments" in fname:
            sessions[sid]["applied_path"] = fpath
            sessions[sid]["applied_name"] = "_".join(parts[1:])
        elif "Bank Deposit" in fname:
            sessions[sid]["deposit_path"] = fpath
            sessions[sid]["deposit_name"] = "_".join(parts[1:])


index_existing_files()


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
                    # Remove from session index too
                    for sid, s in list(sessions.items()):
                        if fpath in s.values():
                            sessions.pop(sid, None)


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

    try:
        result = process(csv_path, f.filename)
    except Exception as e:
        if os.path.exists(csv_path):
            os.remove(csv_path)
        return jsonify({"error": str(e)}), 500

    memo = result["memo"]
    applied_name = f"{memo} Applied Payments.xlsx"
    deposit_name = f"{memo} Bank Deposit.xlsx"
    applied_path = os.path.join(OUTPUT_FOLDER, f"{session_id}_{applied_name}")
    deposit_path = os.path.join(OUTPUT_FOLDER, f"{session_id}_{deposit_name}")

    result["wb_applied"].save(applied_path)
    result["wb_deposit"].save(deposit_path)

    if os.path.exists(csv_path):
        os.remove(csv_path)

    sessions[session_id] = {
        "applied_path": applied_path,
        "deposit_path": deposit_path,
        "applied_name": applied_name,
        "deposit_name": deposit_name,
    }

    return jsonify({
        "session_id": session_id,
        "memo": memo,
        "receive_payment_amt": result["receive_payment_amt"],
        "bank_deposit_total": result["bank_deposit_total"],
        "combined_total": result["combined_total"],
    })


@app.route("/download/<session_id>/<file_type>")
def download(session_id, file_type):
    s = sessions.get(session_id)
    if not s:
        return "Session not found — please re-process your file.", 404
    if file_type == "applied":
        path = s.get("applied_path")
        name = s.get("applied_name")
    elif file_type == "deposit":
        path = s.get("deposit_path")
        name = s.get("deposit_name")
    else:
        return "Invalid file type", 400

    if not path or not os.path.exists(path):
        return "File not found on disk — please re-process your file.", 404

    return send_file(path, as_attachment=True, download_name=name)


@app.route("/reset/<session_id>", methods=["POST"])
def reset(session_id):
    s = sessions.pop(session_id, None)
    if s:
        for path in [s.get("applied_path"), s.get("deposit_path")]:
            if path and os.path.exists(path):
                os.remove(path)
    return jsonify({"ok": True})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
