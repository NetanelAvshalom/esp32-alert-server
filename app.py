import os
from datetime import datetime, timezone
from flask import Flask, request, jsonify

app = Flask(__name__)

# סטטוס "אחרון" בזיכרון (ב-Render Free זה יחזיק כל עוד האינסטנס חי)
LAST_STATUS = {
    "status": "idle",           # idle / smoke / quake_light / quake_strong / normal
    "message": "אין אירועים עדיין",
    "ts": None,
    "device_id": None,
    "data": {}
}

# סוד קטן כדי שלא כל אחד בעולם יוכל לשלוח לך התראות
SHARED_SECRET = os.environ.get("SHARED_SECRET", "")

def now_iso():
    return datetime.now(timezone.utc).isoformat()

@app.get("/")
def home():
    ts = LAST_STATUS["ts"] or "N/A"
    return f"""
    <h2>ESP32 Alert Server ✅</h2>
    <p><b>Status:</b> {LAST_STATUS["status"]}</p>
    <p><b>Message:</b> {LAST_STATUS["message"]}</p>
    <p><b>Time (UTC):</b> {ts}</p>
    <p><b>Device:</b> {LAST_STATUS["device_id"]}</p>
    <pre>{LAST_STATUS["data"]}</pre>
    """

@app.post("/alert")
def alert():
    # אימות בסיסי עם סוד
    if SHARED_SECRET:
        secret = request.headers.get("X-SECRET", "")
        if secret != SHARED_SECRET:
            return jsonify({"ok": False, "error": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}

    # מצפים למשהו כזה:
    # { "device_id":"esp32-1", "status":"smoke", "message":"...", "data":{...} }
    LAST_STATUS["device_id"] = data.get("device_id")
    LAST_STATUS["status"] = data.get("status", "unknown")
    LAST_STATUS["message"] = data.get("message", "")
    LAST_STATUS["data"] = data.get("data", {})
    LAST_STATUS["ts"] = now_iso()

    print("Received alert:", LAST_STATUS)
    return jsonify({"ok": True, "saved": LAST_STATUS})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
