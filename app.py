import os
import sqlite3
from datetime import datetime, timezone
from math import radians, sin, cos, sqrt, atan2

import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# ===== ENV =====
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
SHARED_SECRET = os.environ.get("SHARED_SECRET", "")
DB_PATH = os.environ.get("DB_PATH", "data.db")
DANGER_RADIUS_KM = float(os.environ.get("DANGER_RADIUS_KM", "1.0"))

# ===== LAST EVENT =====
LAST_EVENT = {
    "active": False,
    "type": None,     # smoke / quake / normal
    "level": None,    # light / strong / None
    "lat": None,
    "lon": None,
    "ts": None
}

EVENT_TEXT = {
    ("smoke", "strong"): "🔥 עשן / שריפה (חזק)",
    ("smoke", "light"):  "🔥 עשן / שריפה (קל)",
    ("quake", "strong"): "🌎 רעידת אדמה (חזקה)",
    ("quake", "light"):  "🌎 רעידת אדמה (קלה)",
    ("normal", None):    "✅ חזרה לשגרה",
}

# ===== DB =====
def db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        chat_id TEXT PRIMARY KEY,
        name TEXT,
        last_lat REAL,
        last_lon REAL,
        last_loc_ts TEXT,
        pending_loc INTEGER DEFAULT 0
    )
    """)
    conn.commit()
    conn.close()

init_db()

# ===== UTIL =====
def now_iso():
    return datetime.now(timezone.utc).isoformat()

def to_float(x):
    try:
        return float(x)
    except Exception:
        return None

def haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    c = 2 * atan2(sqrt(a), sqrt(1 - a))
    return R * c

def current_event_label():
    if not LAST_EVENT["active"]:
        return "אין אירוע פעיל"
    return EVENT_TEXT.get(
        (LAST_EVENT["type"], LAST_EVENT["level"]),
        f"⚠️ אירוע: {LAST_EVENT['type']} | רמה: {LAST_EVENT['level']}"
    )

def upsert_user(chat_id, name):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO users(chat_id, name) VALUES(?, ?)
    ON CONFLICT(chat_id) DO UPDATE SET name=excluded.name
    """, (chat_id, name))
    conn.commit()
    conn.close()

def set_all_pending(pending):
    conn = db()
    conn.execute("UPDATE users SET pending_loc=?", (pending,))
    conn.commit()
    conn.close()

def update_location(chat_id, lat, lon):
    conn = db()
    conn.execute("""
    UPDATE users
    SET last_lat=?, last_lon=?, last_loc_ts=?, pending_loc=0
    WHERE chat_id=?
    """, (lat, lon, now_iso(), chat_id))
    conn.commit()
    conn.close()

# ===== TELEGRAM =====
def telegram_send(chat_id, text, reply_markup=None):
    if not BOT_TOKEN:
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    requests.post(url, json=payload, timeout=15)

def telegram_request_location(chat_id):
    reply_markup = {
        "keyboard": [[{"text": "📍 שלח מיקום", "request_location": True}]],
        "resize_keyboard": True,
        "one_time_keyboard": True
    }
    telegram_send(
        chat_id,
        f"🚨 {current_event_label()}\n\nאנא שלח מיקום כדי לבדוק אם אתה באזור סכנה.",
        reply_markup
    )

# ===== WEB =====
@app.get("/")
def home():
    conn = db()
    users = conn.execute("SELECT * FROM users").fetchall()
    conn.close()

    danger, safe, pending = [], [], []

    for u in users:
        if u["pending_loc"] == 1:
            pending.append(u)
            continue

        if not LAST_EVENT["active"] or u["last_lat"] is None or u["last_lon"] is None:
            safe.append((u, None))
            continue

        if LAST_EVENT["lat"] is None or LAST_EVENT["lon"] is None:
            safe.append((u, None))
            continue

        dist = haversine_km(
            u["last_lat"], u["last_lon"],
            LAST_EVENT["lat"], LAST_EVENT["lon"]
        )
        (danger if dist <= DANGER_RADIUS_KM else safe).append((u, dist))

    def row(u, d):
        return f"<li>{u['name']} — {('N/A' if d is None else f'{d:.2f} km')}</li>"

    return f"""
    <h2>ESP32 Alert Server ✅</h2>
    <p><b>Event:</b> {current_event_label()}</p>
    <p><b>Time:</b> {LAST_EVENT['ts']}</p>
    <hr>
    <h3>🚨 In danger</h3><ul>{''.join(row(u,d) for u,d in danger)}</ul>
    <h3>✅ Safe / Unknown</h3><ul>{''.join(row(u,d) for u,d in safe)}</ul>
    <h3>⏳ No response</h3><ul>{''.join(f'<li>{u["name"]}</li>' for u in pending)}</ul>
    """

# ===== ESP32 -> SERVER =====
@app.post("/alert")
def alert():
    if SHARED_SECRET:
        if request.headers.get("X-SECRET", "") != SHARED_SECRET:
            return jsonify({"ok": False}), 401

    data = request.get_json(silent=True) or {}

    LAST_EVENT["active"] = True
    LAST_EVENT["type"] = data.get("type")
    LAST_EVENT["level"] = data.get("level")
    LAST_EVENT["lat"] = to_float(data.get("event_lat"))
    LAST_EVENT["lon"] = to_float(data.get("event_lon"))
    LAST_EVENT["ts"] = now_iso()

    set_all_pending(1)

    conn = db()
    users = conn.execute("SELECT chat_id FROM users").fetchall()
    conn.close()

    for u in users:
        telegram_request_location(u["chat_id"])

    return jsonify({"ok": True})

# ===== TELEGRAM WEBHOOK =====
@app.post("/telegram")
def telegram_webhook():
    update = request.get_json(silent=True) or {}
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return jsonify({"ok": True})

    chat = msg.get("chat", {})
    chat_id = str(chat.get("id"))
    name = (chat.get("first_name") or "").strip() or "User"

    upsert_user(chat_id, name)

    text = (msg.get("text") or "").strip()
    loc = msg.get("location")

    if text == "/start":
        telegram_send(
            chat_id,
            "שלום! 👋\nאני מערכת לניטור סכנות.\n"
            "בזמן אירוע אבקש ממך מיקום ואבדוק אם אתה בסכנה.\n\n"
            f"מצב נוכחי: {current_event_label()}"
        )
        return jsonify({"ok": True})

    if text == "/help":
        telegram_send(chat_id, "פקודות:\n/start\n/help\n📍 שליחת מיקום בעת אירוע")
        return jsonify({"ok": True})

    if loc:
        lat, lon = loc["latitude"], loc["longitude"]
        update_location(chat_id, lat, lon)

        if not LAST_EVENT["active"]:
            telegram_send(chat_id, "קיבלתי מיקום. כרגע אין אירוע פעיל.")
            return jsonify({"ok": True})

        if LAST_EVENT["lat"] is None:
            telegram_send(chat_id, f"קיבלתי מיקום.\nאירוע פעיל: {current_event_label()}")
            return jsonify({"ok": True})

        dist = haversine_km(lat, lon, LAST_EVENT["lat"], LAST_EVENT["lon"])
        telegram_send(
            chat_id,
            f"{'⚠️ בסכנה' if dist <= DANGER_RADIUS_KM else '✅ מחוץ לסכנה'}\n"
            f"מרחק: {dist:.2f} ק״מ\n{current_event_label()}"
        )
        return jsonify({"ok": True})

    return jsonify({"ok": True})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
