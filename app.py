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

    try:
        result = process(csv_path, f.filename)
    except Exception as e:
        if os.path.exists(csv_path):
            os.remove(csv_path)
        return jsonify({"error": str(e)}), 500

    memo = result["memo"]
    applied_name = f"{memo} Applied Payments.xlsx"
    deposit_name = f"{memo} Bank Deposit.xlsx"
    applied_path = os.path.join(OUTPUT_FOLDER, f"{session_id}__applied__{applied_name}")
    deposit_path = os.path.join(OUTPUT_FOLDER, f"{session_id}__deposit__{deposit_name}")

    result["wb_applied"].save(applied_path)
    result["wb_deposit"].save(deposit_path)

    if os.path.exists(csv_path):
        os.remove(csv_path)

    return jsonify({
        "session_id": session_id,
        "memo": memo,
        "receive_payment_amt": result["receive_payment_amt"],
        "bank_deposit_total": result["bank_deposit_total"],
        "combined_total": result["combined_total"],
    })


@app.route("/download/<session_id>/<file_type>")
def download(session_id, file_type):
    # Find file directly on disk by session_id and type — no in-memory index needed
    marker = f"__applied__" if file_type == "applied" else f"__deposit__"
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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
