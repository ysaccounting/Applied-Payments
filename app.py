import os
import uuid
import threading
import time
from flask import Flask, request, jsonify, send_file, render_template
from processor import process

app = Flask(__name__)

UPLOAD_FOLDER = "uploads"
OUTPUT_FOLDER = "outputs"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

# Store output file paths keyed by session id
sessions = {}


def cleanup_old_files():
    """Delete files older than 1 hour."""
    while True:
        time.sleep(300)
        now = time.time()
        for folder in [UPLOAD_FOLDER, OUTPUT_FOLDER]:
            for fname in os.listdir(folder):
                fpath = os.path.join(folder, fname)
                if os.path.isfile(fpath) and now - os.path.getmtime(fpath) > 3600:
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

    try:
        result = process(csv_path, f.filename)
    except Exception as e:
        os.remove(csv_path)
        return jsonify({"error": str(e)}), 500

    memo = result["memo"]
    applied_path = os.path.join(OUTPUT_FOLDER, f"{session_id}_{memo} Applied Payments.xlsx")
    deposit_path = os.path.join(OUTPUT_FOLDER, f"{session_id}_{memo} Bank Deposit.xlsx")

    result["wb_applied"].save(applied_path)
    result["wb_deposit"].save(deposit_path)
    os.remove(csv_path)

    sessions[session_id] = {
        "applied_path": applied_path,
        "deposit_path": deposit_path,
        "applied_name": f"{memo} Applied Payments.xlsx",
        "deposit_name": f"{memo} Bank Deposit.xlsx",
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
    if session_id not in sessions:
        return "Session not found", 404
    s = sessions[session_id]
    if file_type == "applied":
        return send_file(s["applied_path"], as_attachment=True, download_name=s["applied_name"])
    elif file_type == "deposit":
        return send_file(s["deposit_path"], as_attachment=True, download_name=s["deposit_name"])
    return "Invalid file type", 400


@app.route("/reset/<session_id>", methods=["POST"])
def reset(session_id):
    if session_id in sessions:
        s = sessions.pop(session_id)
        for path in [s["applied_path"], s["deposit_path"]]:
            if os.path.exists(path):
                os.remove(path)
    return jsonify({"ok": True})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
