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
DANGER_RADIUS_KM = float(os.environ.get("DANGER_RADIUS_KM", "1.0"))  # fallback

# ---------- Radius per event (km) - "Israel practical" defaults ----------
SMOKE_RADIUS_KM = float(os.environ.get("SMOKE_RADIUS_KM", "0.2"))               # 200m לבית/בניין
QUAKE_LIGHT_RADIUS_KM = float(os.environ.get("QUAKE_LIGHT_RADIUS_KM", "35"))    # רעידה קלה
QUAKE_STRONG_RADIUS_KM = float(os.environ.get("QUAKE_STRONG_RADIUS_KM", "120")) # רעידה חזקה
TERROR_RADIUS_KM = float(os.environ.get("TERROR_RADIUS_KM", "10"))              # אופציונלי

# ✅ חדש: Near home threshold (km)
HOME_NEAR_RADIUS_KM = float(os.environ.get("HOME_NEAR_RADIUS_KM", "0.2"))       # 200m

# ✅ קישור לאתר
SERVER_PUBLIC_URL = "https://esp32-alert-server.onrender.com"

# ---------- In-memory current event ----------
LAST_EVENT = {
    "active": False,
    "type": None,   # smoke / quake / normal / unknown / terror
    "level": None,  # light / strong / None / reported
    "lat": None,
    "lon": None,
    "ts": None,
    "device_id": None,
    "raw": {},

    # ✅ מי פתח אירוע
    "reported_by": None,        # chat_id
    "reported_by_name": None,   # שם בטלגרם
    "reported_ts": None,        # זמן פתיחה

    # ✅ תיאור אירוע
    "description": None
}

PENDING_DESC = set()

EVENT_TEXT = {
    ("smoke", "strong"): "🔥 עשן / שריפה (חזק)",
    ("smoke", "light"):  "🔥 עשן / שריפה (קל)",
    ("quake", "strong"): "🌎 רעידת אדמה (חזקה)",
    ("quake", "light"):  "🌎 רעידת אדמה (קלה)",
    ("normal", None):    "✅ חזרה לשגרה",
    ("terror", "reported"): "🚨 אירוע חריג (פח״ע)",
}

# ✅ הנחיות LED (לפי המערכת שלך)
LED_GUIDANCE_OUTSIDE_TEXT = (
    "💡 הנחיות ניווט באמצעות לדים (עשן/רעידת אדמה):\n"
    "• לדים ירוקים מסמנים את הכיוון הבטוח ליציאה החוצה.\n"
    "• לדים אדומים מסמנים אזור מסוכן (להתרחק ממנו).\n"
    "• במקרה של אירוע — עקבו אחרי *האור הירוק* עד היציאה/נקודה בטוחה."
)

LED_GUIDANCE_MAMAD_TEXT = (
    "💡 הנחיות ניווט באמצעות לדים (פח״ע):\n"
    "• יש ללכת לכיוון הממ״ד לפי *האור הירוק* בכניסה לממ״ד.\n"
    "• לדים אדומים ליד הדלת מסמנים אזור מסוכן — להתרחק.\n"
    "• עקבו אחרי *האור הירוק* עד הכניסה לממ״ד."
)

# ✅ הנחיות בטיחות — יישלחו רק למי שבתוך הרדיוס
QUAKE_SAFETY_TEXT = (
    "📢 הנחיות בעת רעידת אדמה:\n"
    "• יש לצאת למרחב פתוח ולהתרחק ממבנים.\n"
    "• אם אתם בתוך מבנה – יש להתפנות בזהירות."
)

SMOKE_SAFETY_TEXT = (
    "🔥 הנחיות בעשן/שריפה:\n"
    "• התקשר/י מיד לכבאות והצלה: 102\n"
    "• התפנה/י מהבית/בניין בהקדם.\n"
    "• אל תשתמש/י במעלית.\n"
    "• אם יש עשן – התכופף/י נמוך והתרחק/י מהעשן."
)

HAZARD_TYPES = {"smoke", "quake", "terror"}  # רק אלה מצדיקים בקשת מיקום

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

def reset_event():
    LAST_EVENT["active"] = False
    LAST_EVENT["type"] = None
    LAST_EVENT["level"] = None
    LAST_EVENT["lat"] = None
    LAST_EVENT["lon"] = None
    LAST_EVENT["ts"] = None
    LAST_EVENT["device_id"] = None
    LAST_EVENT["raw"] = {}
    LAST_EVENT["reported_by"] = None
    LAST_EVENT["reported_by_name"] = None
    LAST_EVENT["reported_ts"] = None
    LAST_EVENT["description"] = None

def current_radius_km() -> float:
    if not LAST_EVENT.get("active"):
        return 0.0

    t = LAST_EVENT.get("type")
    lvl = LAST_EVENT.get("level")

    if t == "smoke":
        return SMOKE_RADIUS_KM

    if t == "quake":
        if lvl == "strong":
            return QUAKE_STRONG_RADIUS_KM
        return QUAKE_LIGHT_RADIUS_KM

    if t == "terror":
        return TERROR_RADIUS_KM

    return DANGER_RADIUS_KM

def normalize_command(text: str) -> str:
    t = (text or "").strip()
    tl = t.lower()
    if tl in ("/start", "start") or t == "🚀 Start":
        return "/start"
    if tl in ("/help", "help") or t == "❓ Help":
        return "/help"
    return t

# ---------- Telegram Helpers ----------
def main_menu_keyboard():
    return {
        "keyboard": [
            [{"text": "🚀 Start"}, {"text": "❓ Help"}],
            [{"text": "📍 שלח מיקום", "request_location": True}],
            [{"text": "🚨 אירוע חריג"}, {"text": "📝 תיאור אירוע"}],
            [{"text": "🔚 סיום אירוע"}],
        ],
        "resize_keyboard": True
    }

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

def telegram_broadcast(text: str, reply_markup=None):
    conn = db()
    users = conn.execute("SELECT chat_id FROM users").fetchall()
    conn.close()
    for u in users:
        telegram_send(u["chat_id"], text, reply_markup=reply_markup)

def telegram_broadcast_request_location(event_text: str):
    conn = db()
    users = conn.execute("SELECT chat_id FROM users").fetchall()
    conn.close()

    reply_markup = {
        "keyboard": [[{"text": "📍 שלח מיקום", "request_location": True}]],
        "resize_keyboard": True,
        "one_time_keyboard": True
    }

    msg = (
        f"⚠️ יש אירוע: {event_text}\n\n"
        "📍 בבקשה שלח מיקום כדי לבדוק אם אתה באזור סכנה.\n\n"
        f"🌐 אתר המערכת:\n{SERVER_PUBLIC_URL}"
    )

    for u in users:
        telegram_send(u["chat_id"], msg, reply_markup=reply_markup)

# ---------- Web ----------
@app.get("/")
def home():
    conn = db()
    users = conn.execute("SELECT * FROM users").fetchall()
    conn.close()

    danger, safe, pending, near_home = [], [], [], []
    radius_km = current_radius_km()

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

        # ✅ חדש: ברעידה בלבד, מי שבמרחק <= 200m מסומן כ"Near the Home"
        if LAST_EVENT.get("type") == "quake" and dist <= HOME_NEAR_RADIUS_KM:
            near_home.append((u, dist))
        else:
            (danger if dist <= radius_km else safe).append((u, dist))

    def row(u, dist):
        dist_str = "N/A" if dist is None else f"{dist:.2f} km"
        last_ts = u["last_loc_ts"] or "N/A"
        return f"""
        <div class="row">
          <div class="name">{u['name']}</div>
          <div class="meta">{dist_str} • last: {last_ts}</div>
        </div>
        """

    event_label = current_event_label()
    badge_class = "ok"
    if LAST_EVENT.get("active"):
        badge_class = "warn"
        if (LAST_EVENT.get("type") in ("smoke", "quake") and LAST_EVENT.get("level") == "strong") or LAST_EVENT.get("type") == "terror":
            badge_class = "danger"

    desc = LAST_EVENT.get("description") or "—"
    rep_name = LAST_EVENT.get("reported_by_name") or "—"
    rep_ts = LAST_EVENT.get("reported_ts") or "—"

    show_near_home = (LAST_EVENT.get("active") and LAST_EVENT.get("type") == "quake")

    html = f"""
    <!doctype html>
    <html lang="he" dir="rtl">
    <head>
      <meta charset="utf-8">
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <title>GreenEye</title>
      <style>
        :root {{
          --bg: #0b1220;
          --card: #111a2e;
          --text: #eaf0ff;
          --muted: rgba(234,240,255,.72);
          --line: rgba(234,240,255,.14);
          --ok: #2dd4bf;
          --warn: #fbbf24;
          --danger: #fb7185;
        }}
        body {{
          margin: 0;
          font-family: Arial, sans-serif;
          background: radial-gradient(1200px 600px at 20% 0%, rgba(45,212,191,.18), transparent 60%),
                      radial-gradient(900px 500px at 90% 10%, rgba(251,113,133,.14), transparent 55%),
                      var(--bg);
          color: var(--text);
        }}
        .wrap {{
          max-width: 980px;
          margin: 0 auto;
          padding: 22px 16px 60px;
        }}
        .header {{
          display: flex;
          align-items: baseline;
          justify-content: space-between;
          gap: 12px;
          margin-bottom: 14px;
        }}
        h1 {{
          margin: 0;
          font-size: 34px;
          letter-spacing: .4px;
        }}
        .pill {{
          display: inline-flex;
          align-items: center;
          gap: 8px;
          padding: 8px 12px;
          border-radius: 999px;
          font-weight: 700;
          border: 1px solid var(--line);
          background: rgba(17,26,46,.65);
        }}
        .pill.ok {{ color: var(--ok); }}
        .pill.warn {{ color: var(--warn); }}
        .pill.danger {{ color: var(--danger); }}
        .grid {{
          display: grid;
          grid-template-columns: 1.1fr .9fr;
          gap: 14px;
          margin-top: 14px;
        }}
        @media (max-width: 860px) {{
          .grid {{ grid-template-columns: 1fr; }}
        }}
        .card {{
          background: rgba(17,26,46,.78);
          border: 1px solid var(--line);
          border-radius: 18px;
          padding: 16px;
          box-shadow: 0 12px 35px rgba(0,0,0,.35);
        }}
        .card h2 {{
          margin: 0 0 10px;
          font-size: 18px;
        }}
        .kv {{
          display: grid;
          grid-template-columns: 160px 1fr;
          gap: 8px 12px;
          font-size: 14px;
          color: var(--muted);
        }}
        .kv b {{ color: var(--text); font-weight: 700; }}
        .section {{
          margin-top: 14px;
          display: grid;
          gap: 14px;
        }}
        .list {{
          display: grid;
          gap: 10px;
        }}
        .row {{
          padding: 12px;
          border-radius: 14px;
          border: 1px solid var(--line);
          background: rgba(255,255,255,.04);
        }}
        .name {{
          font-weight: 800;
          margin-bottom: 3px;
        }}
        .meta {{
          font-size: 13px;
          color: var(--muted);
          word-break: break-word;
        }}
        a {{
          color: var(--ok);
          text-decoration: none;
        }}
        a:hover {{
          text-decoration: underline;
        }}
        .footer {{
          margin-top: 14px;
          color: var(--muted);
          font-size: 13px;
          text-align: center;
        }}
      </style>
    </head>
    <body>
      <div class="wrap">
        <div class="header">
          <h1>GreenEye</h1>
          <div class="pill {badge_class}">Status: {event_label}</div>
        </div>

        <div class="grid">
          <div class="card">
            <h2>📡 מידע מערכת</h2>
            <div class="kv">
              <div><b>Active</b></div><div>{LAST_EVENT["active"]}</div>
              <div><b>Device</b></div><div>{LAST_EVENT["device_id"]}</div>
              <div><b>Event lat/lon</b></div><div>{LAST_EVENT["lat"]}, {LAST_EVENT["lon"]}</div>
              <div><b>Radius (km)</b></div><div>{radius_km}</div>
              <div><b>Near-Home (km)</b></div><div>{HOME_NEAR_RADIUS_KM}</div>
              <div><b>Time (UTC)</b></div><div>{LAST_EVENT["ts"]}</div>
              <div><b>Reported by</b></div><div>{rep_name}<br><small>{rep_ts}</small></div>
              <div><b>Description</b></div><div>{desc}</div>
              <div><b>Server</b></div><div><a href="{SERVER_PUBLIC_URL}">{SERVER_PUBLIC_URL}</a></div>
            </div>
          </div>

          <div class="card">
            <h2>🧭 מקרא</h2>
            <div class="list">
              {"<div class='row'><div class='name'>🏠 Near the Home</div><div class='meta'>עד 200 מטר מהבית (ברעידה)</div></div>" if show_near_home else ""}
              <div class="row"><div class="name">🚨 In danger</div><div class="meta">בתוך הרדיוס</div></div>
              <div class="row"><div class="name">✅ Safe</div><div class="meta">מחוץ לרדיוס / חסר מידע</div></div>
              <div class="row"><div class="name">⏳ No response</div><div class="meta">ממתין למיקום</div></div>
            </div>
          </div>
        </div>

        <div class="section">

          {""
          if not show_near_home else f"""
          <div class="card">
            <h2>🏠 Near the Home ({len(near_home)})</h2>
            <div class="list">
              {("".join(row(u, d) for u, d in near_home) if near_home else '<div class="meta">אין משתמשים קרובים לבית כרגע.</div>')}
            </div>
          </div>
          """}

          <div class="card">
            <h2>🚨 In danger ({len(danger)})</h2>
            <div class="list">
              {("".join(row(u, d) for u, d in danger) if danger else '<div class="meta">אין משתמשים באזור סכנה כרגע.</div>')}
            </div>
          </div>

          <div class="card">
            <h2>✅ Safe  ({len(safe)})</h2>
            <div class="list">
              {("".join(row(u, d) for u, d in safe) if safe else '<div class="meta">אין רשומות כרגע.</div>')}
            </div>
          </div>

          <div class="card">
            <h2>⏳ No response ({len(pending)})</h2>
            <div class="list">
              {("".join(f'<div class="row"><div class="name">{u["name"]}</div><div class="meta">ממתין למיקום…</div></div>' for u in pending) if pending else '<div class="meta">כולם ענו 👍</div>')}
            </div>
          </div>
        </div>

        <div class="footer">
          GreenEye • simple dashboard
        </div>
      </div>
    </body>
    </html>
    """
    return html

# ✅ ESP32 pulls current event
@app.get("/current_event")
def current_event():
    return jsonify({
        "active": bool(LAST_EVENT.get("active", False)),
        "type": LAST_EVENT.get("type"),
        "level": LAST_EVENT.get("level"),
        "ts": LAST_EVENT.get("ts"),
        "device_id": LAST_EVENT.get("device_id"),
        "lat": LAST_EVENT.get("lat"),
        "lon": LAST_EVENT.get("lon"),
        "description": LAST_EVENT.get("description"),
        "radius_km": current_radius_km(),
        "near_home_km": HOME_NEAR_RADIUS_KM,
    })

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

    # ---------- normal = חזרה לשגרה ----------
    if event_type == "normal":
        reset_event()
        set_all_pending(0)

        telegram_broadcast(
            "✅ יש אירוע: חזרה לשגרה\nהאירוע הסתיים.\n\n"
            f"🌐 אתר המערכת:\n{SERVER_PUBLIC_URL}",
            reply_markup=main_menu_keyboard()
        )
        return jsonify({"ok": True, "status": "cleared"})

    # ---------- אירוע מסוכן ----------
    LAST_EVENT["active"] = True
    LAST_EVENT["type"] = event_type
    LAST_EVENT["level"] = level
    LAST_EVENT["lat"] = data.get("event_lat")
    LAST_EVENT["lon"] = data.get("event_lon")
    LAST_EVENT["device_id"] = data.get("device_id") or data.get("device") or "esp32"
    LAST_EVENT["ts"] = now_iso()
    LAST_EVENT["raw"] = data

    LAST_EVENT["reported_by"] = "ESP32"
    LAST_EVENT["reported_by_name"] = "ESP32"
    LAST_EVENT["reported_ts"] = now_iso()

    if event_type in HAZARD_TYPES:
        set_all_pending(1)
        telegram_broadcast_request_location(current_event_label())
    else:
        set_all_pending(0)
        telegram_broadcast(
            f"ℹ️ עדכון מערכת: {current_event_label()}\n\n🌐 {SERVER_PUBLIC_URL}",
            reply_markup=main_menu_keyboard()
        )

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

        text_raw = msg.get("text") or ""
        text = normalize_command(text_raw)

        if text:
            upsert_user(chat_id, name)

        if chat_id in PENDING_DESC and text:
            LAST_EVENT["description"] = text.strip()
            PENDING_DESC.discard(chat_id)
            telegram_send(chat_id, "📝 התיאור נשמר באתר ✅", reply_markup=main_menu_keyboard())
            return jsonify({"ok": True})

        if text == "/start":
            first_time = not user_exists(chat_id)
            upsert_user(chat_id, name)

            hello = (
                f"שלום {name} 👋\n"
                "נרשמת לראשונה למערכת GreenEye ✅\n\n"
                f"🌐 אתר המערכת:\n{SERVER_PUBLIC_URL}\n\n"
                f"סטטוס נוכחי: {current_event_label()}\n\n"
                "אפשר ללחוץ על 📍 כדי לשלוח מיקום."
            ) if first_time else (
                f"היי {name} 🙂\n"
                "אתה כבר רשום במערכת ✅\n\n"
                f"🌐 אתר המערכת:\n{SERVER_PUBLIC_URL}\n\n"
                f"סטטוס נוכחי: {current_event_label()}"
            )

            telegram_send(chat_id, hello, reply_markup=main_menu_keyboard())
            return jsonify({"ok": True})

        if text == "/help":
            help_msg = (
                "❓ עזרה:\n"
                "• Start – הרשמה והודעת פתיחה\n"
                "• Help – תפריט זה\n"
                "• 📍 שלח מיקום – שולח את המיקום שלך\n"
                "• 🚨 אירוע חריג – פתיחת אירוע ע״י משתמש (מבקש מיקום של המדווח)\n"
                "• 📝 תיאור אירוע – כותבים תיאור שיופיע באתר\n"
                "• 🔚 סיום אירוע – מחזיר לשגרה\n\n"
                f"🌐 אתר המערכת:\n{SERVER_PUBLIC_URL}"
            )
            telegram_send(chat_id, help_msg, reply_markup=main_menu_keyboard())
            return jsonify({"ok": True})

        if text == "🔚 סיום אירוע":
            reset_event()
            set_all_pending(0)
            telegram_broadcast("🔔 עדכון מערכת:\nהאירוע סומן כנסגר ע״י משתמש.", reply_markup=main_menu_keyboard())
            telegram_broadcast("✅ חזרה לשגרה.\n\n" f"🌐 אתר המערכת:\n{SERVER_PUBLIC_URL}", reply_markup=main_menu_keyboard())
            return jsonify({"ok": True})

        if text == "📝 תיאור אירוע":
            PENDING_DESC.add(chat_id)
            telegram_send(chat_id, "כתוב עכשיו את תיאור האירוע (הודעה אחת) והוא יופיע באתר.", reply_markup=main_menu_keyboard())
            return jsonify({"ok": True})

        if text == "🚨 אירוע חריג":
            LAST_EVENT["active"] = True
            LAST_EVENT["type"] = "terror"
            LAST_EVENT["level"] = "reported"
            LAST_EVENT["lat"] = None
            LAST_EVENT["lon"] = None
            LAST_EVENT["ts"] = now_iso()
            LAST_EVENT["device_id"] = "telegram"
            LAST_EVENT["raw"] = {"source": "telegram_button"}

            LAST_EVENT["reported_by"] = chat_id
            LAST_EVENT["reported_by_name"] = name
            LAST_EVENT["reported_ts"] = now_iso()
            LAST_EVENT["description"] = "—"

            set_all_pending(1)

            telegram_send(
                chat_id,
                "🚨 הדיווח התקבל.\n"
                "אנא שלח עכשיו מיקום כדי לסמן את *מיקום האירוע*.\n\n"
                f"🌐 {SERVER_PUBLIC_URL}",
                reply_markup={
                    "keyboard": [[{"text": "📍 שלח מיקום", "request_location": True}]],
                    "resize_keyboard": True,
                    "one_time_keyboard": True
                }
            )
            return jsonify({"ok": True})

        if text == "📍 שלח מיקום" and not msg.get("location"):
            telegram_send(
                chat_id,
                "כדי לשלוח מיקום צריך ללחוץ על כפתור 📍 ולאשר הרשאת Location.\n"
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

            # אירוע פח״ע: מיקום האירוע נקבע לפי המדווח הראשון
            if (
                LAST_EVENT.get("active")
                and LAST_EVENT.get("type") == "terror"
                and LAST_EVENT.get("lat") is None
                and chat_id == LAST_EVENT.get("reported_by")
            ):
                LAST_EVENT["lat"] = lat
                LAST_EVENT["lon"] = lon
                telegram_broadcast_request_location(current_event_label())
                telegram_send(chat_id, "✅ קיבלתי את מיקום האירוע.\nשלחתי עכשיו לכל המשתמשים בקשה למיקום כדי לבדוק מי באזור.", reply_markup=main_menu_keyboard())
                return jsonify({"ok": True})

            if not LAST_EVENT.get("active"):
                telegram_send(chat_id, f"✅ קיבלתי מיקום. כרגע אין אירוע פעיל.\n\n🌐 {SERVER_PUBLIC_URL}", reply_markup=main_menu_keyboard())
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
            radius_km = current_radius_km()

            is_near_home = (LAST_EVENT.get("type") == "quake" and dist <= HOME_NEAR_RADIUS_KM)

            if dist <= radius_km:
                extra = ""
                near_home_line = "\n🏠 אתה בקרבת הבית (עד 200 מטר)." if is_near_home else ""

                if LAST_EVENT.get("type") == "quake":
                    extra = "\n\n" + QUAKE_SAFETY_TEXT + "\n\n" + LED_GUIDANCE_OUTSIDE_TEXT
                elif LAST_EVENT.get("type") == "smoke":
                    extra = "\n\n" + SMOKE_SAFETY_TEXT + "\n\n" + LED_GUIDANCE_OUTSIDE_TEXT
                elif LAST_EVENT.get("type") == "terror":
                    extra = "\n\n" + LED_GUIDANCE_MAMAD_TEXT

                telegram_send(
                    chat_id,
                    f"⚠️ אתה בתוך אזור הסכנה! ({dist:.2f} ק״מ)\n"
                    f"רדיוס נוכחי: {radius_km} ק״מ\n"
                    f"אירוע: {current_event_label()}"
                    f"{near_home_line}"
                    f"{extra}\n\n"
                    f"🌐 אתר המערכת:\n{SERVER_PUBLIC_URL}",
                    reply_markup=main_menu_keyboard()
                )
            else:
                telegram_send(
                    chat_id,
                    f"✅ אתה מחוץ לאזור הסכנה. ({dist:.2f} ק״מ)\n"
                    f"רדיוס נוכחי: {radius_km} ק״מ\n"
                    f"אירוע: {current_event_label()}\n\n"
                    f"🌐 אתר המערכת:\n{SERVER_PUBLIC_URL}",
                    reply_markup=main_menu_keyboard()
                )
            return jsonify({"ok": True})

        if text:
            telegram_send(chat_id, "לא זיהיתי. לחץ Help או כתוב /help", reply_markup=main_menu_keyboard())
        return jsonify({"ok": True})

    except Exception as e:
        print("ERROR in /telegram:", repr(e))
        return jsonify({"ok": False, "error": str(e)}), 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
