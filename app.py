import os
import sqlite3
from datetime import datetime, timezone
from math import radians, sin, cos, sqrt, atan2

import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# ---------- ENV ----------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
SHARED_SECRET = os.environ.get("SHARED_SECRET", "")
DB_PATH = os.environ.get("DB_PATH", "data.db")
DANGER_RADIUS_KM = float(os.environ.get("DANGER_RADIUS_KM", "1.0"))

# ✅ קישור לאתר (כמו שביקשת)
SERVER_PUBLIC_URL = "https://esp32-alert-server.onrender.com"

# ---------- In-memory current event ----------
LAST_EVENT = {
    "active": False,
    "type": None,   # smoke / quake / normal / unknown
    "level": None,  # light / strong / None
    "lat": None,
    "lon": None,
    "ts": None,
    "device_id": None,
    "raw": {}
}

EVENT_TEXT = {
    ("smoke", "strong"): "🔥 עשן / שריפה (חזק)",
    ("smoke", "light"):  "🔥 עשן / שריפה (קל)",
    ("quake", "strong"): "🌎 רעידת אדמה (חזקה)",
    ("quake", "light"):  "🌎 רעידת אדמה (קלה)",
    ("normal", None):    "✅ חזרה לשגרה",
}

# ---------- DB ----------
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

def user_exists(chat_id: str) -> bool:
    conn = db()
    row = conn.execute("SELECT 1 FROM users WHERE chat_id=? LIMIT 1", (chat_id,)).fetchone()
    conn.close()
    return row is not None

def upsert_user(chat_id: str, name: str):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO users(chat_id, name) VALUES(?, ?)
    ON CONFLICT(chat_id) DO UPDATE SET name=excluded.name
    """, (chat_id, name))
    conn.commit()
    conn.close()

def set_all_pending(pending: int):
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE users SET pending_loc=?", (pending,))
    conn.commit()
    conn.close()

def update_location(chat_id: str, lat: float, lon: float):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    UPDATE users
    SET last_lat=?, last_lon=?, last_loc_ts=?, pending_loc=0
    WHERE chat_id=?
    """, (lat, lon, now_iso(), chat_id))
    conn.commit()
    conn.close()

# ---------- Utils ----------
def now_iso():
    return datetime.now(timezone.utc).isoformat()

def current_event_label():
    if not LAST_EVENT.get("active"):
        return "אין אירוע פעיל"
    t = LAST_EVENT.get("type")
    lvl = LAST_EVENT.get("level")
    return EVENT_TEXT.get((t, lvl), f"⚠️ אירוע: {t} | רמה: {lvl}")

def haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    c = 2 * atan2(sqrt(a), sqrt(1 - a))
    return R * c

# ---------- Telegram Helpers ----------
def main_menu_keyboard():
    # ✅ תפריט קבוע כולל כפתור מיקום (request_location עובד רק בכפתור keyboard)
    return {
        "keyboard": [
            [{"text": "🚀 Start"}, {"text": "❓ Help"}],
            [{"text": "📍 שלח מיקום", "request_location": True}],
        ],
        "resize_keyboard": True
    }

def normalize_command(text: str) -> str:
    t = (text or "").strip()
    tl = t.lower()
    if tl in ("/start", "start") or t == "🚀 Start":
        return "/start"
    if tl in ("/help", "help") or t == "❓ Help":
        return "/help"
    return t

# ---------- Telegram ----------
def telegram_send(chat_id: str, text: str, reply_markup=None):
    if not BOT_TOKEN:
        return False, "BOT_TOKEN missing"
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    r = requests.post(url, json=payload, timeout=15)
    return r.ok, r.text

def telegram_request_location(chat_id: str, event_text: str):
    # בקשה "חד פעמית" למיקום בזמן אירוע
    reply_markup = {
        "keyboard": [[{"text": "📍 שלח מיקום", "request_location": True}]],
        "resize_keyboard": True,
        "one_time_keyboard": True
    }
    msg = (
        f"⚠️ יש אירוע: {event_text}\n\n"
        "בבקשה שלח מיקום כדי לבדוק אם אתה באזור סכנה.\n\n"
        f"🌐 אתר המערכת:\n{SERVER_PUBLIC_URL}"
    )
    return telegram_send(chat_id, msg, reply_markup)

# ---------- Web ----------
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

        if (u["last_lat"] is None or u["last_lon"] is None or
                not LAST_EVENT["active"] or
                LAST_EVENT["lat"] is None or LAST_EVENT["lon"] is None):
            safe.append((u, None))
            continue

        dist = haversine_km(
            float(u["last_lat"]), float(u["last_lon"]),
            float(LAST_EVENT["lat"]), float(LAST_EVENT["lon"])
        )
        (danger if dist <= DANGER_RADIUS_KM else safe).append((u, dist))

    def row(u, dist):
        dist_str = "N/A" if dist is None else f"{dist:.2f} km"
        last_ts = u["last_loc_ts"] or "N/A"
        return f"<li>{u['name']} — {dist_str} — last={last_ts}</li>"

    html = f"""
    <h2>GreenEye</h2>
    <p><b>Event:</b> {current_event_label()}</p>
    <p><b>Active:</b> {LAST_EVENT["active"]}</p>
    <p><b>Device:</b> {LAST_EVENT["device_id"]}</p>
    <p><b>Event lat/lon:</b> {LAST_EVENT["lat"]}, {LAST_EVENT["lon"]}</p>
    <p><b>Radius (km):</b> {DANGER_RADIUS_KM}</p>
    <p><b>Time (UTC):</b> {LAST_EVENT["ts"]}</p>
    <p><b>Server:</b> {SERVER_PUBLIC_URL}</p>
    <hr/>
    """
    html += "<h3>🚨 In danger</h3><ul>" + "".join(row(u, d) for u, d in danger) + "</ul>"
    html += "<h3>✅ Safe / Unknown</h3><ul>" + "".join(row(u, d) for u, d in safe) + "</ul>"
    html += "<h3>⏳ No response</h3><ul>" + "".join(f"<li>{u['name']}</li>" for u in pending) + "</ul>"
    return html

# ---------- ESP32 -> Server ----------
@app.post("/alert")
def alert():
    if SHARED_SECRET:
        secret = request.headers.get("X-SECRET", "")
        if secret != SHARED_SECRET:
            return jsonify({"ok": False, "error": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}

    event_type = data.get("type")
    level = data.get("level")

    # fallback לפורמט הישן (status/message)
    if not event_type:
        status = data.get("status")
        msg = data.get("message")

        if status in ("smoke", "quake", "normal"):
            event_type = status
        else:
            event_type = "unknown"

        if msg in ("light", "strong"):
            level = msg
        else:
            level = None

    LAST_EVENT["active"] = True
    LAST_EVENT["type"] = event_type
    LAST_EVENT["level"] = level
    LAST_EVENT["lat"] = data.get("event_lat")
    LAST_EVENT["lon"] = data.get("event_lon")
    LAST_EVENT["device_id"] = data.get("device_id") or data.get("device") or "esp32"
    LAST_EVENT["ts"] = now_iso()
    LAST_EVENT["raw"] = data

    # כולם צריכים מיקום עכשיו
    set_all_pending(1)

    label = current_event_label()
    conn = db()
    users = conn.execute("SELECT chat_id FROM users").fetchall()
    conn.close()

    for u in users:
        telegram_request_location(u["chat_id"], label)

    print("Received alert:", LAST_EVENT)
    return jsonify({"ok": True, "saved": LAST_EVENT})

# ---------- Telegram -> Server (Webhook) ----------
@app.post("/telegram")
def telegram_webhook():
    try:
        update = request.get_json(silent=True) or {}
        msg = update.get("message") or update.get("edited_message")
        if not msg:
            return jsonify({"ok": True})

        chat = msg.get("chat", {})
        chat_id = str(chat.get("id"))

        first = (chat.get("first_name") or "").strip()
        last = (chat.get("last_name") or "").strip()
        name = (first + (" " + last if last else "")).strip() or "Unknown"

        text = normalize_command(msg.get("text") or "")

        # ---------- START ----------
        if text == "/start":
            first_time = not user_exists(chat_id)
            upsert_user(chat_id, name)

            if first_time:
                hello = (
                    f"שלום {name} 👋\n"
                    "נרשמת לראשונה למערכת לניטור סכנות מבוססת ESP32 ✅\n\n"
                    "מה אני עושה?\n"
                    "• מקבל התראות מה-ESP32 (עשן / רעידת אדמה)\n"
                    "• בזמן אירוע מבקש ממך מיקום\n"
                    "• מציג באתר מי באזור סכנה ומי לא ענה\n\n"
                    f"🌐 אתר המערכת:\n{SERVER_PUBLIC_URL}\n\n"
                    f"סטטוס נוכחי: {current_event_label()}\n\n"
                    "אפשר ללחוץ על 📍 כדי לשלוח מיקום."
                )
            else:
                hello = (
                    f"היי {name} 🙂\n"
                    "אתה כבר רשום במערכת ✅\n\n"
                    f"🌐 אתר המערכת:\n{SERVER_PUBLIC_URL}\n\n"
                    f"סטטוס נוכחי: {current_event_label()}\n\n"
                    "אפשר ללחוץ על 📍 כדי לשלוח מיקום."
                )

            telegram_send(chat_id, hello, reply_markup=main_menu_keyboard())
            return jsonify({"ok": True})

        # עדכון משתמש בכל הודעה
        upsert_user(chat_id, name)

        # ---------- HELP ----------
        if text == "/help":
            help_msg = (
                "❓ עזרה:\n"
                "• Start – הרשמה והודעת פתיחה\n"
                "• Help – תפריט זה\n"
                "• 📍 שלח מיקום – שולח את המיקום שלך\n\n"
                f"🌐 אתר המערכת:\n{SERVER_PUBLIC_URL}\n\n"
                "בזמן אירוע המערכת תבדוק אם אתה באזור סכנה."
            )
            telegram_send(chat_id, help_msg, reply_markup=main_menu_keyboard())
            return jsonify({"ok": True})

        # אם המשתמש הקליד/לחץ טקסט אבל לא שלח Location בפועל
        if text == "📍 שלח מיקום":
            telegram_send(
                chat_id,
                "כדי לשלוח מיקום צריך ללחוץ על כפתור 📍 ולאשר הרשאת מיקום.\n"
                "אם לא קופץ חלון הרשאה – בדוק בהגדרות טלגרם שהרשאת Location פתוחה.",
                reply_markup=main_menu_keyboard()
            )
            return jsonify({"ok": True})

        # ---------- LOCATION ----------
        loc = msg.get("location")
        if loc:
            lat = float(loc["latitude"])
            lon = float(loc["longitude"])
            update_location(chat_id, lat, lon)

            if not LAST_EVENT.get("active"):
                telegram_send(
                    chat_id,
                    f"✅ קיבלתי מיקום. כרגע אין אירוע פעיל.\n\n🌐 {SERVER_PUBLIC_URL}",
                    reply_markup=main_menu_keyboard()
                )
                return jsonify({"ok": True})

            if LAST_EVENT.get("lat") is None or LAST_EVENT.get("lon") is None:
                telegram_send(
                    chat_id,
                    "✅ קיבלתי מיקום.\n"
                    f"יש אירוע פעיל: {current_event_label()}\n"
                    "עדיין אין לי מיקום של האירוע עצמו, אז לא ניתן לחשב מרחק.\n\n"
                    f"🌐 אתר המערכת:\n{SERVER_PUBLIC_URL}",
                    reply_markup=main_menu_keyboard()
                )
                return jsonify({"ok": True})

            dist = haversine_km(lat, lon, float(LAST_EVENT["lat"]), float(LAST_EVENT["lon"]))
            if dist <= DANGER_RADIUS_KM:
                telegram_send(
                    chat_id,
                    f"⚠️ אתה בתוך אזור הסכנה! ({dist:.2f} ק״מ)\n"
                    f"אירוע: {current_event_label()}\n\n"
                    f"🌐 אתר המערכת:\n{SERVER_PUBLIC_URL}",
                    reply_markup=main_menu_keyboard()
                )
            else:
                telegram_send(
                    chat_id,
                    f"✅ אתה מחוץ לאזור הסכנה. ({dist:.2f} ק״מ)\n"
                    f"אירוע: {current_event_label()}\n\n"
                    f"🌐 אתר המערכת:\n{SERVER_PUBLIC_URL}",
                    reply_markup=main_menu_keyboard()
                )
            return jsonify({"ok": True})

        # ---------- OTHER TEXT ----------
        if text:
            telegram_send(chat_id, "לא זיהיתי. לחץ Help או כתוב /help", reply_markup=main_menu_keyboard())
        return jsonify({"ok": True})

    except Exception as e:
        print("ERROR in /telegram:", repr(e))
        return jsonify({"ok": False, "error": str(e)}), 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
