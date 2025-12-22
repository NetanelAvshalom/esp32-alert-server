import os
import sqlite3
from datetime import datetime, timezone
from flask import Flask, request, jsonify
import requests
from math import radians, sin, cos, sqrt, atan2

app = Flask(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
SHARED_SECRET = os.environ.get("SHARED_SECRET", "")  # אותו סוד שיש ב-ESP32
DB_PATH = os.environ.get("DB_PATH", "data.db")

# אירוע הסכנה האחרון (אפשר גם לשמור ב-DB, כרגע גם וגם)
LAST_EVENT = {
    "active": False,
    "type": None,          # "smoke" / "quake"
    "level": None,         # "light" / "strong"
    "lat": None,
    "lon": None,
    "ts": None
}

DANGER_RADIUS_KM = float(os.environ.get("DANGER_RADIUS_KM", "1.0"))  # רדיוס סכנה לדוגמה


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


# ---------- Utilities ----------
def now_iso():
    return datetime.now(timezone.utc).isoformat()

def telegram_send(chat_id: str, text: str, reply_markup=None):
    if not BOT_TOKEN:
        return False, "BOT_TOKEN missing"
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    r = requests.post(url, json=payload, timeout=15)
    return r.ok, r.text

def telegram_request_location(chat_id: str):
    # כפתור שמבקש מיקום (Location request)
    reply_markup = {
        "keyboard": [[{"text": "📍 שלח מיקום", "request_location": True}]],
        "resize_keyboard": True,
        "one_time_keyboard": True
    }
    return telegram_send(chat_id, "יש אירוע! בבקשה שלח מיקום כדי לבדוק אם אתה באזור סכנה.", reply_markup)

def haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat/2)**2 + cos(radians(lat1))*cos(radians(lat2))*sin(dlon/2)**2
    c = 2 * atan2(sqrt(a), sqrt(1-a))
    return R * c

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


# ---------- Web Pages ----------
@app.get("/")
def home():
    conn = db()
    users = conn.execute("SELECT * FROM users").fetchall()
    conn.close()

    # חלוקה לפי סטטוס
    danger = []
    safe = []
    pending = []

    for u in users:
        if u["pending_loc"] == 1:
            pending.append(u)
            continue

        if u["last_lat"] is None or u["last_lon"] is None or not LAST_EVENT["active"]:
            safe.append((u, None))
            continue

        dist = haversine_km(u["last_lat"], u["last_lon"], LAST_EVENT["lat"], LAST_EVENT["lon"])
        if dist <= DANGER_RADIUS_KM:
            danger.append((u, dist))
        else:
            safe.append((u, dist))

    event_html = f"""
    <h2>ESP32 Alert Server ✅</h2>
    <p><b>Event active:</b> {LAST_EVENT["active"]}</p>
    <p><b>Type:</b> {LAST_EVENT["type"]} | <b>Level:</b> {LAST_EVENT["level"]}</p>
    <p><b>Event lat/lon:</b> {LAST_EVENT["lat"]}, {LAST_EVENT["lon"]}</p>
    <p><b>Radius (km):</b> {DANGER_RADIUS_KM}</p>
    <p><b>Time (UTC):</b> {LAST_EVENT["ts"]}</p>
    <hr/>
    """

    def row(u, dist):
        dist_str = "N/A" if dist is None else f"{dist:.2f} km"
        return f"<li>{u['name']} (chat_id={u['chat_id']}) — {dist_str} — last={u['last_loc_ts']}</li>"

    html = event_html
    html += "<h3>🚨 In danger</h3><ul>" + "".join(row(u, d) for u, d in danger) + "</ul>"
    html += "<h3>✅ Safe / Unknown</h3><ul>" + "".join(row(u, d) for u, d in safe) + "</ul>"
    html += "<h3>⏳ No response yet</h3><ul>" + "".join(f"<li>{u['name']} (chat_id={u['chat_id']})</li>" for u in pending) + "</ul>"

    return html


# ---------- ESP32 -> Server ----------
@app.post("/alert")
def alert():
    # אימות עם סוד (כמו שדיברנו)
    if SHARED_SECRET:
        secret = request.headers.get("X-SECRET", "")
        if secret != SHARED_SECRET:
            return jsonify({"ok": False, "error": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}

    # מה ESP32 צריך לשלוח:
    # {
    #   "type":"smoke" או "quake",
    #   "level":"light"/"strong",
    #   "event_lat": <lat>,
    #   "event_lon": <lon>
    # }
    LAST_EVENT["active"] = True
    LAST_EVENT["type"] = data.get("type")
    LAST_EVENT["level"] = data.get("level")
    LAST_EVENT["lat"] = data.get("event_lat")
    LAST_EVENT["lon"] = data.get("event_lon")
    LAST_EVENT["ts"] = now_iso()

    # מסמנים שכולם צריכים לשלוח מיקום
    set_all_pending(1)

    # שולחים בקשת מיקום לכל המשתמשים
    conn = db()
    users = conn.execute("SELECT chat_id FROM users").fetchall()
    conn.close()

    for u in users:
        telegram_request_location(u["chat_id"])

    return jsonify({"ok": True, "saved": LAST_EVENT})


# ---------- Telegram -> Server (Webhook) ----------
@app.post("/telegram")
# למעלה בקובץ (Globals)
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "")  # שים פה את ה-ID שלך ב-Render env
EVENT_TEXT = {
    ("smoke", "strong"): "🔥 עשן / שריפה (חזק)",
    ("smoke", "light"):  "🔥 עשן / שריפה (קל)",
    ("quake", "strong"): "🌎 רעידת אדמה (חזקה)",
    ("quake", "light"):  "🌎 רעידת אדמה (קלה)",
    ("normal", None):    "✅ חזרה לשגרה",
}

def is_admin(chat_id: str) -> bool:
    return ADMIN_CHAT_ID and chat_id == ADMIN_CHAT_ID

def current_event_label():
    if not LAST_EVENT.get("active"):
        return "אין אירוע פעיל"
    t = LAST_EVENT.get("type")
    lvl = LAST_EVENT.get("level")
    return EVENT_TEXT.get((t, lvl), f"⚠️ אירוע: {t} | רמה: {lvl}")

@app.post("/telegram")
def telegram_webhook():
    try:
        update = request.get_json(silent=True) or {}

        msg = update.get("message") or update.get("edited_message")
        if not msg:
            return jsonify({"ok": True})

        chat = msg.get("chat", {})
        chat_id = str(chat.get("id"))
        name = (chat.get("first_name") or "")
        if chat.get("last_name"):
            name += " " + chat.get("last_name")
        name = name.strip() or "Unknown"

        text = (msg.get("text") or "").strip()

        # רושם משתמש DB בכל הודעה
        upsert_user(chat_id, name)

        # 1) פקודות
        if text == "/start":
            hello = (
                "שלום! אני מערכת לניטור סכנות מבוססת ESP32 🛰️\n\n"
                "מה אני עושה?\n"
                "• מקבל התראות מה-ESP32 (עשן / רעידת אדמה)\n"
                "• מבקש מיקום מכל המשתמשים בזמן אירוע\n"
                "• מציג באתר מי באזור סכנה ומי לא ענה\n\n"
                f"סטטוס נוכחי: {current_event_label()}\n\n"
                "פקודות:\n"
                "/help – עזרה\n"
            )
            telegram_send(chat_id, hello)
            return jsonify({"ok": True})

        if text == "/help":
            help_msg = (
                "עזרה:\n"
                "/start – הרשמה והודעת פתיחה\n"
                "/help – תפריט זה\n"
                "📍 כשיהיה אירוע תקבל בקשה לשלוח מיקום.\n"
            )
            if is_admin(chat_id):
                help_msg += "\n(Admin)\n/status – מצב מערכת\n/clear – איפוס אירוע\n"
            telegram_send(chat_id, help_msg)
            return jsonify({"ok": True})

        if is_admin(chat_id) and text == "/status":
            telegram_send(chat_id, f"מצב מערכת: {current_event_label()}")
            return jsonify({"ok": True})

        if is_admin(chat_id) and text == "/clear":
            LAST_EVENT["active"] = False
            LAST_EVENT["type"] = None
            LAST_EVENT["level"] = None
            LAST_EVENT["lat"] = None
            LAST_EVENT["lon"] = None
            LAST_EVENT["ts"] = None
            set_all_pending(0)
            telegram_send(chat_id, "איפסתי אירוע. המערכת בשגרה ✅")
            return jsonify({"ok": True})

        # 2) מיקום
        loc = msg.get("location")
        if loc:
            lat = float(loc["latitude"])
            lon = float(loc["longitude"])
            update_location(chat_id, lat, lon)

            # אם אין אירוע פעיל – אל תגיד “אין סכנה” בטעות
            if not LAST_EVENT.get("active"):
                telegram_send(chat_id, "✅ קיבלתי מיקום. כרגע אין אירוע פעיל.")
                return jsonify({"ok": True})

            # יש אירוע פעיל אבל אולי אין לו lat/lon (כי ה-ESP32 לא שלח)
            if LAST_EVENT.get("lat") is None or LAST_EVENT.get("lon") is None:
                telegram_send(
                    chat_id,
                    "✅ קיבלתי מיקום.\n"
                    f"יש אירוע פעיל: {current_event_label()}\n"
                    "עדיין אין לי מיקום של האירוע עצמו, אז אני לא יכול לחשב מרחק.\n"
                    "ברגע שנוסיף מיקום לאירוע – האתר יציג מי בסכנה."
                )
                return jsonify({"ok": True})

            # יש גם מיקום אירוע וגם מיקום משתמש → מחשבים מרחק
            dist = haversine_km(lat, lon, float(LAST_EVENT["lat"]), float(LAST_EVENT["lon"]))
            if dist <= DANGER_RADIUS_KM:
                telegram_send(chat_id, f"⚠️ אתה בתוך אזור הסכנה! ({dist:.2f} ק״מ)\nאירוע: {current_event_label()}")
            else:
                telegram_send(chat_id, f"✅ אתה מחוץ לאזור הסכנה. ({dist:.2f} ק״מ)\nאירוע: {current_event_label()}")
            return jsonify({"ok": True})

        # 3) טקסט אחר
        if text:
            telegram_send(chat_id, "לא זיהיתי פקודה. נסה /help")
        return jsonify({"ok": True})

    except Exception as e:
        # קריטי כדי שלא תיפול כל האפליקציה בשקט
        print("ERROR in /telegram:", repr(e))
        return jsonify({"ok": False, "error": str(e)}), 200



if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
