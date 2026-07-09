"""
Bulletproof IVR YouTube Engine 2026 — Hardened Edition
=======================================================
מנוע IVR חסין: הפעלת שירים מיוטיוב דרך מרכזיה טלפונית (IVR), עם תמיכה
בסליקת תשלומים (תרומה/מנוי) דרך ספק סליקה חיצוני.

איך להריץ (Render / כל שרת אחר):
  משתני סביבה חשובים שכדאי להגדיר:
    IVR_WHITELIST_PHONES=0534133753,0534133754     # טלפונים מורשים אוטומטית
    IVR_DEFAULT_ACCESS_CODE=1234                    # קוד גישה לכל השאר
    IVR_PUBLIC_BASE_URL=https://your-app.onrender.com
    IVR_REQUIRE_PUBLIC_BASE_URL=true
    RAPIDAPI_KEY=...                                 # אופציונלי
    CLEARING_API_URL / CLEARING_TERMINAL / CLEARING_API_KEY   # לסליקה, אופציונלי
    REDIS_URL=...                                    # אופציונלי, קאש משותף בין instances

⚠️ חשוב לגבי הרשימה הלבנה: אם IVR_WHITELIST_PHONES לא מוגדר, שום מספר אינו
מאושר מראש — כל מתקשר יתבקש להקיש קוד גישה (IVR_DEFAULT_ACCESS_CODE, ברירת
מחדל "1234" אם לא הוגדר, עם אזהרה בלוג). זו לא תקלה — זו התנהגות מכוונת כדי
שלא יהיה קוד/מספר "קסום" קשיח בקוד המקור. יש שתי דרכים לפתור את זה:
  1. הגדירו IVR_WHITELIST_PHONES עם המספרים שרוצים שיעברו בלי קוד.
  2. או פשוט הקישו את קוד הגישה כשהמערכת מבקשת אותו.
"""

import os
import re
import json
import copy
import random
import secrets
import sqlite3
import logging
import asyncio
from enum import Enum
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Tuple

import httpx
from fastapi import FastAPI, Query, Request, HTTPException
from fastapi.responses import PlainTextResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from cachetools import TTLCache

# ==========================================
# 📋 לוגר
# ==========================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("IVR_Production_Engine")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ==========================================
# ⚙️ קונפיגורציה — הכל מ-ENV, שום סוד קשיח בקוד
# ==========================================
RAPIDAPI_KEY = os.environ.get("RAPIDAPI_KEY", "")  # ריק => המסלול הזה ידולג בשקט
RAPIDAPI_HOST = os.environ.get(
    "RAPIDAPI_HOST", "youtube-mp3-audio-video-downloader.p.rapidapi.com"
)
DB_PATH = os.environ.get("IVR_DB_PATH", "ivr_production.db")

_whitelist_env = os.environ.get("IVR_WHITELIST_PHONES", "")
DEFAULT_WHITELIST = [p.strip() for p in _whitelist_env.split(",") if p.strip()]

# קוד גישה לכל מי שלא ברשימה הלבנה. ברירת המחדל "1234" היא לנוחות התחלתית
# בלבד (כדי שהמערכת תהיה שמישה מהרגע הראשון) — יש להחליף אותה ב-production
# ע"י הגדרת IVR_DEFAULT_ACCESS_CODE, אחרת כל מי שמנחש "1234" ייכנס.
DEFAULT_ACCESS_CODE = os.environ.get("IVR_DEFAULT_ACCESS_CODE", "1234")
if os.environ.get("IVR_DEFAULT_ACCESS_CODE") is None:
    logger.warning(
        "IVR_DEFAULT_ACCESS_CODE not set! Using default access code: %s "
        "— set this env var explicitly in production so it's not guessable.",
        DEFAULT_ACCESS_CODE,
    )

if not DEFAULT_WHITELIST:
    logger.warning(
        "IVR_WHITELIST_PHONES not set — no phone numbers are pre-authorized. "
        "Every caller will be asked for the access code (%s). "
        "Set IVR_WHITELIST_PHONES via env var, comma separated, "
        "e.g. '0534133753,0534133754' to skip the code for specific numbers.",
        DEFAULT_ACCESS_CODE,
    )

# בסיס URL ציבורי — קריטי לבטיחות. אם לא מוגדר, נופלים חזרה על ה-Host header
# של הבקשה, שניתן לזיוף ע"י מי ששולח את הבקשה. ב-production מומלץ להגדיר גם
# IVR_PUBLIC_BASE_URL וגם IVR_REQUIRE_PUBLIC_BASE_URL=true.
PUBLIC_BASE_URL = os.environ.get("IVR_PUBLIC_BASE_URL", "").rstrip("/")
REQUIRE_PUBLIC_BASE_URL = os.environ.get("IVR_REQUIRE_PUBLIC_BASE_URL", "false").lower() == "true"

_trusted_hosts_env = os.environ.get("IVR_TRUSTED_HOSTS", "")
TRUSTED_HOSTS = {h.strip().lower() for h in _trusted_hosts_env.split(",") if h.strip()}

if REQUIRE_PUBLIC_BASE_URL and not PUBLIC_BASE_URL:
    raise RuntimeError(
        "IVR_REQUIRE_PUBLIC_BASE_URL=true אך IVR_PUBLIC_BASE_URL לא הוגדר. "
        "מסרבים לעלות: בניית כתובת ה-callback מ-Host header ניתנת לזיוף."
    )
if not PUBLIC_BASE_URL:
    logger.warning(
        "IVR_PUBLIC_BASE_URL לא הוגדר — ניפול חזרה על Host header של הבקשה, שניתן לזיוף. "
        "מומלץ מאוד להגדיר IVR_PUBLIC_BASE_URL (וגם IVR_REQUIRE_PUBLIC_BASE_URL=true) בפרודקשן."
    )

# תבנית פקודת ה-IVR להפעלת שיר. ההנחה: המרכזיה מבצעת HTTP GET לכתובת שמוחזרת
# בתוך פקודת read=<url>=... (הקראת קובץ mp3 מרוחק). אם בלוגים אין בקשות ל-
# /stream/... אחרי שהפקודה חוזרת, סימן שהמרכזיה שלכם דורשת פורמט אחר —
# אפשר לשנות רק דרך IVR_PLAY_COMMAND_TEMPLATE בלי לגעת בקוד.
PLAY_COMMAND_TEMPLATE = os.environ.get(
    "IVR_PLAY_COMMAND_TEMPLATE",
    "read={base}/stream/{video_id}.mp3=ValName,no,1,0,2,digits,no",
)

RATE_LIMIT_PER_MINUTE = int(os.environ.get("IVR_RATE_LIMIT_PER_MINUTE", "20"))
SESSION_TTL_HOURS = int(os.environ.get("IVR_SESSION_TTL_HOURS", "4"))
MAX_PLAYLIST_SIZE = int(os.environ.get("IVR_MAX_PLAYLIST_SIZE", "15"))
SEARCH_RECURSION_DEPTH_LIMIT = 40
DB_READ_POOL_SIZE = max(1, int(os.environ.get("IVR_DB_READ_POOL_SIZE", "8")))

# מספרים "חסויים" ששולחות מרכזיות שונות כשהמתקשר חסם הצגת מספר.
ANONYMOUS_PHONE_VALUES = {"0", "", "anonymous", "unknown", "withheld", "unavailable"}

# --- סליקת תשלומים (אופציונלי) -----------------------------------------
# אדפטר גנרי ל-REST endpoint של ספק סליקה (Cardcom / Tranzila / Yaad Sarig /
# PayMe וכו'). כל ספק דורש פורמט קצת שונה — מה שכאן הוא שלד עבודה מלא
# (רישום עסקה, retry-safe, לוגים, שמירת תוצאה) עם נקודת הרחבה יחידה
# (charge_customer) שיש להתאים לפי מסמכי ה-API של הספק שבחרתם בפועל.
CLEARING_API_URL = os.environ.get("CLEARING_API_URL", "")
CLEARING_TERMINAL = os.environ.get("CLEARING_TERMINAL", "")
CLEARING_API_KEY = os.environ.get("CLEARING_API_KEY", "")
CLEARING_ENABLED = bool(CLEARING_API_URL and CLEARING_API_KEY)
DONATION_MIN_ILS = float(os.environ.get("IVR_DONATION_MIN_ILS", "5"))
DONATION_MAX_ILS = float(os.environ.get("IVR_DONATION_MAX_ILS", "1000"))
if not CLEARING_ENABLED:
    logger.info(
        "Payment clearing not configured (CLEARING_API_URL/CLEARING_API_KEY missing) — "
        "donation menu option will be disabled."
    )

# Redis אופציונלי לקאש משותף בין כמה instances. בלי REDIS_URL — TTLCache מקומי.
REDIS_URL = os.environ.get("REDIS_URL", "")
_redis = None
if REDIS_URL:
    try:
        import redis.asyncio as _aioredis  # type: ignore
        _redis = _aioredis.from_url(REDIS_URL, decode_responses=True, socket_timeout=2.0)
        logger.info("Redis cache enabled (shared across instances).")
    except ImportError:
        logger.warning(
            "REDIS_URL מוגדר אך חבילת redis לא מותקנת (pip install redis) — "
            "נופלים חזרה על TTLCache מקומי שאינו משותף בין instances."
        )
        _redis = None

PHONE_RE = re.compile(r"^\d{9,15}$")
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
AMOUNT_RE = re.compile(r"^\d{1,6}$")

# מפתח ה-API הציבורי שקליינט הווב של יוטיוב שולח בפועל. בלעדיו InnerTube
# נוטה להחזיר תשובת 200 "ריקה" (בלי תוצאות) במקום שגיאה — בדיוק התופעה
# שראינו בלוגים. ניתן לדרוס אם יוטיוב ישנה אותו (מפתח ציבורי, לא סוד).
INNERTUBE_KEY = os.environ.get("IVR_INNERTUBE_KEY", "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8")

# רשימות שרתי Invidious/Piped ניתנות לעדכון דרך ENV בלי לגעת בקוד — רשימות
# כאלה "מתות" ומתחלפות כל הזמן, אז חשוב שלא יהיו קשיחות בקוד באופן בלעדי.
_default_invidious = "https://invidious.projectsegfau.lt,https://yewtu.be,https://invidious.fdn.fr,https://iv.ggtyler.dev"
_default_piped = "https://pipedapi.kavin.rocks,https://api-piped.mha.fi,https://piped-api.lunar.icu"
INVIDIOUS_INSTANCES = [i.strip() for i in os.environ.get("IVR_INVIDIOUS_INSTANCES", _default_invidious).split(",") if i.strip()]
PIPED_INSTANCES = [i.strip() for i in os.environ.get("IVR_PIPED_INSTANCES", _default_piped).split(",") if i.strip()]

# טוקן אופציונלי לחשוף endpoint אבחון לבדיקת חיפוש בלי לחכות לשיחת טלפון.
# בלי IVR_DEBUG_TOKEN, ה-endpoint מנוטרל לגמרי (404).
DEBUG_TOKEN = os.environ.get("IVR_DEBUG_TOKEN", "")

search_cache: TTLCache = TTLCache(maxsize=1000, ttl=900)
stream_url_cache: TTLCache = TTLCache(maxsize=500, ttl=600)


async def cache_get(local_cache: TTLCache, namespace: str, key: str) -> Optional[str]:
    if _redis is not None:
        try:
            val = await _redis.get(f"{namespace}:{key}")
            if val is not None:
                return val
        except Exception as e:
            logger.warning("Redis GET failed (%s) — נופל ל-cache מקומי: %s", namespace, e)
    return local_cache.get(key)


async def cache_set(local_cache: TTLCache, namespace: str, key: str, value: str, ttl: int) -> None:
    local_cache[key] = value
    if _redis is not None:
        try:
            await _redis.set(f"{namespace}:{key}", value, ex=ttl)
        except Exception as e:
            logger.warning("Redis SET failed (%s) — הערך נשמר רק מקומית: %s", namespace, e)


# מנעולים פר-טלפון: מונעים מרוץ מצבים בין שתי בקשות מקבילות לאותו מספר.
# כל הלוגיקה של PLAYING_TRACKS (כולל random.shuffle) רצה בתוך המנעול הזה,
# כך ששתי בקשות מקבילות לאותו טלפון תמיד מתבצעות בסדר, אף פעם בו-זמנית.
_phone_locks: dict[str, asyncio.Lock] = {}


def get_phone_lock(phone: str) -> asyncio.Lock:
    lock = _phone_locks.get(phone)
    if lock is None:
        lock = asyncio.Lock()
        _phone_locks[phone] = lock
    return lock


# ==========================================
# 🎵 פלייליסט חירום — תמיד עותק עמוק
# ==========================================
_EMERGENCY_PLAYLIST_SOURCE = [
    {"id": "4NzIOLEeJZM", "title": "נחמן פילמר שמחה פורצת גבולות 15", "duration": "1:26:07", "author": "נחמן פילמר"},
    {"id": "WSMFtm3ZqcY", "title": "סט להיטים דתי חרדי קיץ פול ווליום", "duration": "1:26:18", "author": "פול ווליום"},
    {"id": "3QDfxHZaUik", "title": "סט להיטים דתי מקפיץ בטירוף רמיקסים", "duration": "1:45:25", "author": "מדע והשכל"},
    {"id": "kP1jrKkSZfE", "title": "שמחת היום 1 סט חסידי קצבי אש", "duration": "2:20:53", "author": "פול ווליום"},
]


def get_emergency_playlist() -> List[dict]:
    return copy.deepcopy(_EMERGENCY_PLAYLIST_SOURCE)


# ==========================================
# 🌐 HTTP Client משותף
# ==========================================
http_client: Optional[httpx.AsyncClient] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global http_client
    http_client = httpx.AsyncClient(
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
        timeout=httpx.Timeout(8.0),
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"},
    )
    init_db()
    _init_db_pools()
    cleanup_task = asyncio.create_task(_cleanup_supervisor())
    logger.info("🚀 IVR Engine started")
    try:
        yield
    finally:
        cleanup_task.cancel()
        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass
        await http_client.aclose()
        _close_db_pools()
        if _redis is not None:
            try:
                await _redis.aclose()
            except Exception:
                pass
        logger.info("🛑 IVR Engine stopped")


app = FastAPI(title="Bulletproof IVR YouTube Engine 2026", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ==========================================
# 🗄️ בסיס נתונים
# ==========================================
def init_db() -> None:
    conn = sqlite3.connect(DB_PATH)
    try:
        cursor = conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA foreign_keys=ON")

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                phone TEXT PRIMARY KEY,
                authorized INTEGER DEFAULT 0,
                access_code TEXT DEFAULT '0000'
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                phone TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                playlist_json TEXT DEFAULT '[]',
                current_index INTEGER DEFAULT 0,
                last_active TEXT NOT NULL
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS favorites (
                phone TEXT,
                video_id TEXT,
                title TEXT,
                created_at TEXT,
                PRIMARY KEY(phone, video_id)
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS rate_limits (
                phone TEXT,
                timestamp TEXT
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS payments (
                tx_id TEXT PRIMARY KEY,
                phone TEXT,
                amount REAL,
                status TEXT,
                provider_response TEXT,
                created_at TEXT
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_rate_limits_phone_ts ON rate_limits(phone, timestamp)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_sessions_last_active ON sessions(last_active)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_favorites_phone ON favorites(phone)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_payments_phone ON payments(phone)")

        for ph in DEFAULT_WHITELIST:
            if PHONE_RE.match(ph):
                cursor.execute(
                    "INSERT OR IGNORE INTO users (phone, authorized, access_code) VALUES (?, 1, ?)",
                    (ph, DEFAULT_ACCESS_CODE),
                )
            else:
                logger.warning("Skipping invalid whitelist phone entry: %r", ph)

        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Connection pool: כתיבות מסתדרות דרך חיבור יחיד + מנעול אחד (SQLite ממילא
# מרשה כותב יחיד בו-זמנית). קריאות (SELECT) מקבלות פול קטן של חיבורים
# נפרדים לקונקורנטיות אמיתית, וגם חוסכות פתיחה/סגירה של הקובץ בכל בקשה.
# להיקף גדול משמעותית (מאות שיחות מקבילות) מומלץ לעבור בעתיד ל-Postgres.
# --------------------------------------------------------------------------
_write_conn: Optional[sqlite3.Connection] = None
_write_lock = asyncio.Lock()
_read_conns: List[sqlite3.Connection] = []
_read_locks: List[asyncio.Lock] = []
_read_rr = 0


def _open_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=5.0, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _init_db_pools() -> None:
    global _write_conn, _read_conns, _read_locks
    _write_conn = _open_conn()
    _read_conns = [_open_conn() for _ in range(DB_READ_POOL_SIZE)]
    _read_locks = [asyncio.Lock() for _ in range(DB_READ_POOL_SIZE)]
    logger.info("DB pools ready: 1 write connection + %d read connections", DB_READ_POOL_SIZE)


def _close_db_pools() -> None:
    global _write_conn, _read_conns
    try:
        if _write_conn:
            _write_conn.close()
    except Exception:
        pass
    for c in _read_conns:
        try:
            c.close()
        except Exception:
            pass
    _write_conn = None
    _read_conns = []


async def run_db_query(
    query: str, params: tuple = (), fetchall: bool = False, commit: bool = False
):
    global _read_rr

    def _execute(conn: sqlite3.Connection):
        cursor = conn.cursor()
        cursor.execute(query, params)
        result = cursor.fetchall() if fetchall else cursor.fetchone()
        if commit:
            conn.commit()
        return result

    if _write_conn is None or not _read_conns:
        # פול לא אותחל עדיין (למשל שימוש לפני עליית lifespan) — fallback בטוח.
        def _execute_standalone():
            conn = _open_conn()
            try:
                return _execute(conn)
            finally:
                conn.close()
        try:
            return await asyncio.get_running_loop().run_in_executor(None, _execute_standalone)
        except Exception as e:
            logger.error("DB query failed (standalone): %s | query=%s", e, query)
            return [] if fetchall else None

    try:
        if commit:
            async with _write_lock:
                return await asyncio.get_running_loop().run_in_executor(None, _execute, _write_conn)
        else:
            idx = _read_rr % len(_read_conns)
            _read_rr += 1
            async with _read_locks[idx]:
                return await asyncio.get_running_loop().run_in_executor(None, _execute, _read_conns[idx])
    except Exception as e:
        logger.error("DB query failed: %s | query=%s", e, query)
        if commit and _write_conn is not None:
            try:
                _write_conn.rollback()
            except Exception:
                pass
        return [] if fetchall else None


# ==========================================
# 🚦 Rate Limiting (בלי DELETE בכל בקשה — רק ספירה; הניקוי בטאסק הרקע)
# ==========================================
async def is_rate_limited(session_key: str) -> bool:
    # הוולידציה של phone/anon כבר בוצעה ב-handle_ivr לפני שהגענו לכאן — כאן רק
    # מוודאים שיש מפתח לא-ריק (יכול להיות "0501234567" או "anon:<call_id>").
    if not session_key:
        return True
    try:
        now = utcnow()
        one_minute_ago = (now - timedelta(minutes=1)).isoformat()

        count_row = await run_db_query(
            "SELECT COUNT(*) FROM rate_limits WHERE phone = ? AND timestamp > ?",
            (session_key, one_minute_ago),
        )
        count = count_row[0] if count_row else 0
        if count >= RATE_LIMIT_PER_MINUTE:
            return True

        await run_db_query(
            "INSERT INTO rate_limits (phone, timestamp) VALUES (?, ?)",
            (session_key, now.isoformat()),
            commit=True,
        )
        return False
    except Exception as e:
        logger.error("Rate limit check failed: %s", e)
        return True  # בספק — עדיף לחסום מאשר לפתוח פרצה


# ==========================================
# 🔎 חיפוש — InnerTube + regex fallback + Invidious fallback
# ==========================================
def _dedupe_and_trim(tracks: List[dict], limit: int = MAX_PLAYLIST_SIZE) -> List[dict]:
    seen = set()
    out = []
    for t in tracks:
        vid = t.get("id")
        if not vid or not VIDEO_ID_RE.match(vid) or vid in seen:
            continue
        seen.add(vid)
        t["title"] = (t.get("title") or "שיר ללא שם")[:120]
        t["author"] = (t.get("author") or "אמן")[:80]
        t["duration"] = t.get("duration") or "00:00"
        out.append(t)
        if len(out) >= limit:
            break
    return out


def extract_tracks_from_innertube(data: dict) -> List[dict]:
    tracks: List[dict] = []

    def recursive_extract(node, depth: int = 0):
        if depth > SEARCH_RECURSION_DEPTH_LIMIT or len(tracks) >= MAX_PLAYLIST_SIZE * 3:
            return

        if isinstance(node, dict):
            renderer = None
            if "videoRenderer" in node:
                renderer = node["videoRenderer"]
            elif "compactVideoRenderer" in node:
                renderer = node["compactVideoRenderer"]
            elif "richItemRenderer" in node and "content" in node["richItemRenderer"]:
                recursive_extract(node["richItemRenderer"]["content"], depth + 1)
                return
            elif "itemSectionRenderer" in node:
                recursive_extract(node["itemSectionRenderer"].get("contents", []), depth + 1)
                return

            if renderer and renderer.get("videoId"):
                video_id = renderer.get("videoId")
                title_runs = renderer.get("title", {}).get("runs")
                if title_runs:
                    title = title_runs[0].get("text", "שיר ללא שם")
                else:
                    title = renderer.get("title", {}).get("simpleText", "שיר ללא שם")

                byline_runs = renderer.get("longBylineText", {}).get("runs", [{}])
                author = byline_runs[0].get("text", "אמן") if byline_runs else "אמן"

                tracks.append({
                    "id": video_id,
                    "title": title,
                    "duration": renderer.get("lengthText", {}).get("simpleText", "00:00"),
                    "author": author,
                })
                return

            for value in node.values():
                recursive_extract(value, depth + 1)

        elif isinstance(node, list):
            for item in node:
                recursive_extract(item, depth + 1)

    recursive_extract(data)
    return _dedupe_and_trim(tracks)


_VIDEO_ID_SCAN_RE = re.compile(r'"videoId"\s*:\s*"([A-Za-z0-9_-]{11})"')


def extract_tracks_regex_fallback(raw_text: str) -> List[dict]:
    """קו הגנה אחרון אם מבנה ה-JSON של InnerTube משתנה ופענוח מובנה נכשל:
    סריקת regex גולמית לזיהוי videoId בטקסט הגולמי. פחות מדויק (בלי כותרות
    אמיתיות) אבל עדיף על נפילה מיידית ל-Invidious/פלייליסט חירום."""
    ids = _VIDEO_ID_SCAN_RE.findall(raw_text or "")
    tracks = [{"id": vid, "title": "שיר ללא שם", "duration": "00:00", "author": "אמן"} for vid in ids]
    return _dedupe_and_trim(tracks)


async def search_invidious_fallback(query: str) -> List[dict]:
    assert http_client is not None
    for inst in INVIDIOUS_INSTANCES:
        try:
            resp = await http_client.get(
                f"{inst}/api/v1/search", params={"q": query, "type": "video"}, timeout=6.0
            )
            if resp.status_code != 200:
                continue
            try:
                data = resp.json()
            except json.JSONDecodeError:
                logger.warning("Invidious %s returned non-JSON", inst)
                continue

            tracks = []
            for item in data:
                if isinstance(item, dict) and item.get("videoId"):
                    tracks.append({
                        "id": item["videoId"],
                        "title": item.get("title", "שיר ללא שם"),
                        "duration": str(item.get("lengthSeconds", "00:00")),
                        "author": item.get("author", "אמן"),
                    })

            tracks = _dedupe_and_trim(tracks, limit=12)
            if tracks:
                logger.info("✅ Invidious success via %s: %d tracks", inst, len(tracks))
                return tracks
        except (httpx.HTTPError, asyncio.TimeoutError) as e:
            logger.warning("Invidious %s failed: %s", inst, e)
            continue
    return []


async def search_piped_fallback(query: str) -> List[dict]:
    """שכבת fallback נוספת (Piped) — פורמט ותוכן instances שונים מ-Invidious,
    כך ששני השירותים לא נופלים יחד באותו סוג תקלה (שרת מסוים חסום/מת)."""
    assert http_client is not None
    for inst in PIPED_INSTANCES:
        try:
            resp = await http_client.get(
                f"{inst}/search", params={"q": query, "filter": "videos"}, timeout=6.0
            )
            if resp.status_code != 200:
                continue
            try:
                data = resp.json()
            except json.JSONDecodeError:
                logger.warning("Piped %s returned non-JSON", inst)
                continue

            items = data.get("items", []) if isinstance(data, dict) else []
            tracks = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                url_path = item.get("url", "")  # e.g. "/watch?v=XXXXXXXXXXX"
                m = re.search(r"[?&]v=([A-Za-z0-9_-]{11})", url_path)
                if not m:
                    continue
                secs = item.get("duration")
                duration = f"{secs // 60}:{secs % 60:02d}" if isinstance(secs, int) and secs >= 0 else "00:00"
                tracks.append({
                    "id": m.group(1),
                    "title": item.get("title", "שיר ללא שם"),
                    "duration": duration,
                    "author": item.get("uploaderName", "אמן"),
                })

            tracks = _dedupe_and_trim(tracks, limit=12)
            if tracks:
                logger.info("✅ Piped success via %s: %d tracks", inst, len(tracks))
                return tracks
        except (httpx.HTTPError, asyncio.TimeoutError) as e:
            logger.warning("Piped %s failed: %s", inst, e)
            continue
    return []


async def search_youtube_innertube(query: str, filter_newest: bool = False) -> List[dict]:
    query = (query or "").strip()[:150]
    if not query:
        return get_emergency_playlist()

    cache_key = f"{'newest:' if filter_newest else ''}{query}"
    cached = await cache_get(search_cache, "search", cache_key)
    if cached:
        try:
            return json.loads(cached)
        except json.JSONDecodeError:
            pass

    # הגדרת הכתובת של הפרוקסי שלך
    proxy_url = "https://shiny-union-ba59.a41337sh.workers.dev"
    url = f"{proxy_url}/youtubei/v1/search?key={INNERTUBE_KEY}&prettyPrint=false"
    
    # שינוי ל-ANDROID כדי לעקוף את החסימה של יוטיוב
    payload = {
        "context": {
            "client": {
                "clientName": "ANDROID",
                "clientVersion": "17.20.39",
                "androidSdkVersion": 31,
                "hl": "he",
                "gl": "IL",
                "clientFormFactor": "SMALL_FORM_FACTOR",
            }
        },
        "query": query,
    }
    if filter_newest:
        payload["params"] = "EgQIARAB"

    headers = {
        "Content-Type": "application/json",
        "Origin": "https://www.youtube.com",
        "Referer": "https://www.youtube.com/",
        "User-Agent": "com.google.android.youtube/17.20.39 (Linux; U; Android 11; il)",
    }

    tracks: List[dict] = []
    try:
        assert http_client is not None
        resp = await http_client.post(url, json=payload, headers=headers, timeout=10.0)
        logger.info("InnerTube status: %s for query: %s", resp.status_code, query)
        
        if resp.status_code == 200:
            try:
                raw_data = resp.json()
                tracks = extract_tracks_from_innertube(raw_data)
            except json.JSONDecodeError:
                tracks = []

            if not tracks:
                logger.warning("Structured parse got 0 tracks — trying regex fallback")
                tracks = extract_tracks_regex_fallback(resp.text)

            if tracks:
                logger.info("✅ InnerTube parsed successfully: %d tracks", len(tracks))
                await cache_set(search_cache, "search", cache_key, json.dumps(tracks, ensure_ascii=False), 900)
                return tracks

            logger.warning("InnerTube returned 200 but 0 tracks for query=%r.", query)
    except (httpx.HTTPError, asyncio.TimeoutError) as e:
        logger.error("InnerTube request failed: %s", e)

    # Fallbacks
    logger.info("InnerTube parsing failed → trying Invidious fallback")
    fallback_tracks = await search_invidious_fallback(query)
    if fallback_tracks:
        await cache_set(search_cache, "search", cache_key, json.dumps(fallback_tracks, ensure_ascii=False), 900)
        return fallback_tracks

    logger.info("Invidious failed → trying Piped fallback")
    fallback_tracks = await search_piped_fallback(query)
    if fallback_tracks:
        await cache_set(search_cache, "search", cache_key, json.dumps(fallback_tracks, ensure_ascii=False), 900)
        return fallback_tracks

    logger.warning("All search backends failed → Emergency playlist")
    return get_emergency_playlist()


# ==========================================
# 🎧 Streaming — עם pre-flight validation לפני שמתחייבים ללקוח
# ==========================================
async def _candidate_stream_urls(video_id: str) -> List[str]:
    candidates: List[str] = []
    watch_url = f"https://www.youtube.com/watch?v={video_id}"

    for inst in ["https://api.cobalt.tools/api/json", "https://cobalt.api.v0.wtf/api/json"]:
        candidates.append(f"cobalt::{inst}::{watch_url}")

    if RAPIDAPI_KEY:
        candidates.append(f"rapidapi::{video_id}")

    candidates.append(f"invidious::https://invidious.projectsegfau.lt/latest_version?id={video_id}&itag=140")
    return candidates


async def _resolve_candidate(candidate: str) -> Optional[str]:
    assert http_client is not None
    try:
        if candidate.startswith("cobalt::"):
            _, inst, watch_url = candidate.split("::", 2)
            r = await http_client.post(
                inst,
                json={"url": watch_url, "downloadMode": "audio", "audioFormat": "mp3"},
                timeout=4.0,
            )
            if r.status_code == 200:
                return r.json().get("url")

        elif candidate.startswith("rapidapi::"):
            video_id = candidate.split("::", 1)[1]
            r = await http_client.get(
                f"https://{RAPIDAPI_HOST}/get_mp3_download_link/{video_id}",
                headers={"x-rapidapi-key": RAPIDAPI_KEY, "x-rapidapi-host": RAPIDAPI_HOST},
                timeout=5.0,
            )
            if r.status_code == 200:
                js = r.json()
                return js.get("file") or js.get("link") or js.get("url")

        elif candidate.startswith("invidious::"):
            return candidate.split("::", 1)[1]

    except (httpx.HTTPError, asyncio.TimeoutError, json.JSONDecodeError, ValueError) as e:
        logger.warning("Candidate resolve failed (%s): %s", candidate[:40], e)
    return None


@app.get("/stream/{video_id}.mp3")
async def proxy_mp3_stream(video_id: str):
    if not VIDEO_ID_RE.match(video_id):
        raise HTTPException(400, "Invalid video ID")

    assert http_client is not None
    cached_url = await cache_get(stream_url_cache, "stream", video_id)
    candidates = ([f"invidious::{cached_url}"] if cached_url else []) + await _candidate_stream_urls(video_id)

    for candidate in candidates:
        target_url = await _resolve_candidate(candidate)
        if not target_url or not target_url.startswith("https://"):
            continue
        try:
            req = http_client.build_request(
                "GET", target_url,
                # read=None: זו הזרמת אודיו ארוכת-טווח שהמרכזיה עשויה "להשהות" בפועל
                # (buffer איטי מצידה) — טיימאאוט קריאה קצוב היה מנתק שירים תקינים
                # באמצע. connect/write נשארים קצובים כדי לא להיתקע על מקור מת.
                timeout=httpx.Timeout(connect=5.0, read=None, write=10.0, pool=5.0),
            )
            resp = await http_client.send(req, stream=True)
        except (httpx.HTTPError, asyncio.TimeoutError) as e:
            logger.warning("Stream preflight failed for %s: %s", candidate[:40], e)
            continue

        if resp.status_code != 200:
            await resp.aclose()
            continue

        await cache_set(stream_url_cache, "stream", video_id, target_url, 600)

        async def chunk_generator(response: httpx.Response):
            try:
                async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                    yield chunk
            except (httpx.HTTPError, asyncio.TimeoutError) as e:
                # לא ניתן "לתקן" סטרים שכבר החל להישלח ללקוח (headers כבר נשלחו) —
                # מה שאפשר זה לוודא ניקוי משאבים נקי ולתעד לצורך מעקב/דשבורד.
                logger.error("Streaming error mid-stream for %s: %s", video_id, e)
            finally:
                await response.aclose()

        return StreamingResponse(chunk_generator(resp), media_type="audio/mpeg")

    logger.error("All stream sources exhausted for video_id=%s", video_id)
    raise HTTPException(502, "No available audio source for this track")


# ==========================================
# 💳 סליקת תשלומים (אופציונלי)
# ==========================================
def _safe_json_snippet(data, limit: int = 2000) -> str:
    """שומר תמיד JSON תקין (אף פעם לא נחתך באמצע), גם כשהתשובה מהספק ענקית —
    כדי שקריאה עתידית עם json.loads() לא תיפול."""
    try:
        full = json.dumps(data, ensure_ascii=False)
    except (TypeError, ValueError):
        full = str(data)
    if len(full) <= limit:
        return full
    return json.dumps({"_truncated": True, "raw_prefix": full[: max(0, limit - 60)]}, ensure_ascii=False)


async def charge_customer(phone: str, amount_ils: float) -> Tuple[bool, str, str]:
    """
    מבצע חיוב מול ספק הסליקה שהוגדר ב-ENV. זהו אדפטר גנרי ל-REST endpoint —
    התאימו את שדות הבקשה/תשובה למסמכי ה-API של הספק שלכם בפועל
    (Cardcom / Tranzila / Yaad Sarig / PayMe...). כל מה שסביב (רישום עסקה,
    לוגים, טיפול בשגיאות) כבר מוכן ולא צריך לגעת בו.
    מחזיר: (success, message_for_caller, tx_id)
    """
    tx_id = secrets.token_hex(8)
    if not CLEARING_ENABLED:
        return False, "שירות התשלומים אינו זמין כרגע", tx_id

    await run_db_query(
        "INSERT INTO payments (tx_id, phone, amount, status, created_at) VALUES (?, ?, ?, 'pending', ?)",
        (tx_id, phone, amount_ils, utcnow().isoformat()),
        commit=True,
    )
    try:
        assert http_client is not None
        resp = await http_client.post(
            CLEARING_API_URL,
            json={
                "terminal": CLEARING_TERMINAL,
                "api_key": CLEARING_API_KEY,
                "amount": amount_ils,
                "currency": "ILS",
                "phone": phone,
                "reference": tx_id,
            },
            timeout=10.0,
        )
        try:
            data = resp.json()
        except json.JSONDecodeError:
            data = {"raw": resp.text[:500]}

        success = resp.status_code == 200 and bool(
            data.get("success") or str(data.get("status", "")).lower() == "approved"
        )
        status = "approved" if success else "declined"
        await run_db_query(
            "UPDATE payments SET status = ?, provider_response = ? WHERE tx_id = ?",
            (status, _safe_json_snippet(data), tx_id),
            commit=True,
        )
        msg = "התשלום אושר, תודה רבה" if success else "התשלום נדחה, אנא נסו כרטיס אחר"
        return success, msg, tx_id
    except (httpx.HTTPError, asyncio.TimeoutError) as e:
        logger.error("Clearing charge failed for tx=%s: %s", tx_id, e)
        await run_db_query(
            "UPDATE payments SET status = 'error', provider_response = ? WHERE tx_id = ?",
            (str(e)[:500], tx_id),
            commit=True,
        )
        return False, "שגיאה בביצוע התשלום, אנא נסו שוב מאוחר יותר", tx_id


@app.post("/payment/webhook")
async def payment_webhook(request: Request):
    """נקודת קצה אופציונלית לאישורי תשלום א-סינכרוניים מספק הסליקה (אם הוא
    תומך ב-webhook/IPN). מגן בסיסי: דורש שיתאים tx_id קיים בטבלה. הוסיפו כאן
    אימות חתימה/סוד משותף לפי מסמכי הספק שלכם לפני production אמיתי."""
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(400, "Invalid JSON")

    tx_id = payload.get("reference") or payload.get("tx_id")
    if not tx_id:
        raise HTTPException(400, "Missing reference/tx_id")

    row = await run_db_query("SELECT tx_id FROM payments WHERE tx_id = ?", (tx_id,))
    if not row:
        raise HTTPException(404, "Unknown transaction")

    status = "approved" if payload.get("success") else "declined"
    await run_db_query(
        "UPDATE payments SET status = ?, provider_response = ? WHERE tx_id = ?",
        (status, _safe_json_snippet(payload), tx_id),
        commit=True,
    )
    return {"ok": True}


# ==========================================
# 📟 IVR Helpers
# ==========================================
def clean_text_for_ivr(text: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9\sא-ת]", " ", text or "")
    return re.sub(r"\s+", " ", cleaned).strip()[:200]


def make_ivr_read_command(text: str, min_dig: str, max_dig: str, sec: int, mode: str) -> str:
    clean = clean_text_for_ivr(text)
    if mode.lower() == "voice":
        return f"read=t-{clean}=ValName,no,50,1,{sec},voice,no"
    return f"read=t-{clean}=ValName,no,{max_dig},{min_dig},{sec},{mode.lower()},no"


def get_final_play_command(video_id: str, request: Request) -> Optional[str]:
    if PUBLIC_BASE_URL:
        base = PUBLIC_BASE_URL
    else:
        raw_host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
        # מאחורי כמה פרוקסים בשרשרת (למשל Cloudflare + Render) הכותרת עלולה
        # להכיל כמה ערכים מופרדים בפסיק ("host1, host2") — הראשון הוא המקורי.
        host = raw_host.split(",")[0].strip().split(":")[0].lower()
        if TRUSTED_HOSTS and host not in TRUSTED_HOSTS:
            logger.error("Rejected untrusted Host header for play command: %r", host)
            return None
        protocol = request.headers.get("x-forwarded-proto") or ("http" if "localhost" in host else "https")
        port = ":10000" if "localhost" in host else ""
        base = f"{protocol}://{host}{port}"
    return PLAY_COMMAND_TEMPLATE.format(base=base, video_id=video_id)


def _generic_error_command() -> str:
    return make_ivr_read_command("משהו השתבש אנא נסו שוב מאוחר יותר", "1", "1", 5, "digits")


def _play_command_or_error(video_id: str, request: Request) -> str:
    cmd = get_final_play_command(video_id, request)
    return cmd if cmd is not None else _generic_error_command()


# ==========================================
# 🧹 Background cleanup + watchdog
# ==========================================
async def _cleanup_once() -> None:
    cutoff = (utcnow() - timedelta(hours=SESSION_TTL_HOURS)).isoformat()
    await run_db_query("DELETE FROM sessions WHERE last_active < ?", (cutoff,), commit=True)

    one_hour_ago = (utcnow() - timedelta(hours=1)).isoformat()
    await run_db_query("DELETE FROM rate_limits WHERE timestamp < ?", (one_hour_ago,), commit=True)

    for ph in list(_phone_locks.keys()):
        lock = _phone_locks.get(ph)
        if lock and not lock.locked():
            _phone_locks.pop(ph, None)


async def active_session_cleanup():
    while True:
        try:
            await _cleanup_once()
        except Exception as e:
            logger.error("Cleanup iteration failed: %s", e)
        await asyncio.sleep(1800)


async def _cleanup_supervisor():
    """עוטף את משימת הניקוי ומרים אותה מחדש אם היא קורסת מסיבה בלתי צפויה,
    כדי שלעולם לא "נשכח" עם sessions/rate_limits שהולכים ותופחים."""
    backoff = 5
    while True:
        try:
            await active_session_cleanup()
            return  # לא אמור לקרות (לולאה אינסופית), אבל ליתר ביטחון
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("Cleanup task crashed, restarting in %ss: %s", backoff, e)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 300)


# ==========================================
# 🎛️ States
# ==========================================
class State(str, Enum):
    CHECK_AUTH = "CHECK_AUTH"
    MAIN_MENU = "MAIN_MENU"
    WAITING_FOR_SEARCH = "WAITING_FOR_SEARCH"
    PLAYING_TRACKS = "PLAYING_TRACKS"
    WAITING_FOR_DONATION_AMOUNT = "WAITING_FOR_DONATION_AMOUNT"


MAIN_MENU_TEXT = (
    "לחיפוש קולי 1 • שירים חדשים 2 • מועדפים 3"
    + (" • תרומה 9" if CLEARING_ENABLED else "")
)


async def _save_session(phone: str, state: str, playlist: List[dict], index: int) -> None:
    """כותב state + playlist_json + current_index יחד, תמיד באותה קריאה —
    כדי שה-DB וה-RAM לעולם לא יתפצלו לגרסאות לא-מסונכרנות (state ישן מול
    פלייליסט חדש וכד')."""
    await run_db_query(
        "UPDATE sessions SET state = ?, playlist_json = ?, current_index = ? WHERE phone = ?",
        (state, json.dumps(playlist, ensure_ascii=False), index, phone),
        commit=True,
    )


async def _load_or_create_session(phone: str, is_whitelisted: bool) -> Tuple[str, List[dict], int]:
    session_data = await run_db_query(
        "SELECT state, playlist_json, current_index FROM sessions WHERE phone = ?", (phone,)
    )
    if not session_data:
        state = State.MAIN_MENU.value if is_whitelisted else State.CHECK_AUTH.value
        await run_db_query(
            "INSERT INTO sessions (phone, state, playlist_json, current_index, last_active) VALUES (?, ?, ?, ?, ?)",
            (phone, state, "[]", 0, utcnow().isoformat()),
            commit=True,
        )
        return state, [], 0

    state, playlist_json, index = session_data
    try:
        playlist = json.loads(playlist_json) if playlist_json else []
    except json.JSONDecodeError:
        playlist = []
    return state, playlist, (index or 0)


# ==========================================
# 📞 Main IVR Endpoint
# ==========================================
@app.get("/youtube", response_class=PlainTextResponse)
async def handle_ivr(request: Request, ApiPhone: str = Query(None), hangup: str = Query(None)):
    if hangup == "yes" or not ApiPhone:
        return "OK"

    raw_phone = ApiPhone.strip()
    is_anonymous = raw_phone.lower() in ANONYMOUS_PHONE_VALUES

    if is_anonymous:
        # מתקשר עם מספר חסום. אי אפשר לזהות אותו לאורך זמן (וגם לא כדאי —
        # אין למה "לזכור"), אבל חובה לתת לו session_key ייחודי לשיחה הנוכחית
        # (ApiCallId/ApiYFCallId) ולא "0" קבוע — אחרת כל המתקשרים החסויים
        # היו חולקים session אחד וקופצים על הפלייליסט/מצב אחד של השני.
        call_id = (
            request.query_params.get("ApiCallId")
            or request.query_params.get("ApiYFCallId")
            or ""
        ).strip()
        if not call_id:
            logger.warning("Anonymous caller with no call id — rejecting")
            return "OK"
        session_key = f"anon:{call_id[:64]}"
    else:
        if not PHONE_RE.match(raw_phone):
            logger.warning("Rejected malformed phone: %r", raw_phone)
            return "OK"
        session_key = raw_phone

    val_params = [v for k, v in request.query_params.multi_items() if k == "ValName"]
    ValName = (val_params[-1] if val_params else None)
    if ValName is not None:
        ValName = ValName.strip()[:150]

    logger.info("📞 Phone: %s | Session: %s | ValName: %r", raw_phone, session_key, ValName)

    try:
        if await is_rate_limited(session_key):
            return make_ivr_read_command("בוצעו יותר מדי פעולות אנא המתן מעט", "1", "1", 5, "digits")

        async with get_phone_lock(session_key):
            return await _handle_ivr_locked(request, session_key, ValName, is_anonymous)
    except Exception as e:
        logger.exception("Unhandled error in IVR handler for session=%s: %s", session_key, e)
        return _generic_error_command()


async def _handle_ivr_locked(
    request: Request, ApiPhone: str, ValName: Optional[str], is_anonymous: bool = False
) -> str:
    if is_anonymous:
        # אין ל-caller חסוי זהות יציבה שאפשר לשמור/לאשר לצמיתות — תמיד דורשים
        # קוד גישה, ולעולם לא כותבים אותו לטבלת users (שם היא לא בעלת משמעות).
        is_whitelisted = False
        stored_access_code = DEFAULT_ACCESS_CODE
    else:
        user_data = await run_db_query("SELECT authorized, access_code FROM users WHERE phone = ?", (ApiPhone,))
        is_whitelisted = bool(user_data and user_data[0] == 1)
        stored_access_code = user_data[1] if user_data else DEFAULT_ACCESS_CODE

    state, playlist, index = await _load_or_create_session(ApiPhone, is_whitelisted)

    await run_db_query(
        "UPDATE sessions SET last_active = ? WHERE phone = ?", (utcnow().isoformat(), ApiPhone), commit=True
    )

    # ---------- Auth flow ----------
    if not is_whitelisted and state == State.CHECK_AUTH.value:
        if ValName and ValName == stored_access_code:
            if not is_anonymous:
                await run_db_query(
                    "INSERT OR REPLACE INTO users (phone, authorized, access_code) VALUES (?, 1, ?)",
                    (ApiPhone, stored_access_code),
                    commit=True,
                )
            state = State.MAIN_MENU.value
            await _save_session(ApiPhone, state, [], 0)
            ValName = None
        else:
            msg = "קוד שגוי אנא נסה שנית" if ValName else "אנא הקש את קוד הגישה"
            return make_ivr_read_command(msg, "4", "4", 10, "digits")

    # ---------- MAIN MENU ----------
    if state == State.MAIN_MENU.value:
        if ValName == "1":
            await _save_session(ApiPhone, State.WAITING_FOR_SEARCH.value, [], 0)
            return make_ivr_read_command("אנא אמרו את שם השיר לאחר הצליל", "1", "50", 10, "voice")

        elif ValName == "2":
            tracks = await search_youtube_innertube("שירים חסידיים חדשים", filter_newest=True)
            if not tracks:
                tracks = get_emergency_playlist()
            await _save_session(ApiPhone, State.PLAYING_TRACKS.value, tracks, 0)
            return _play_command_or_error(tracks[0]["id"], request)

        elif ValName == "3":
            favs = await run_db_query(
                "SELECT video_id, title FROM favorites WHERE phone = ? ORDER BY created_at DESC",
                (ApiPhone,), fetchall=True,
            )
            if not favs:
                return make_ivr_read_command("רשימת המועדפים ריקה", "1", "1", 4, "digits")
            tracks = [{"id": f[0], "title": f[1], "duration": "00:00", "author": ""} for f in favs]
            await _save_session(ApiPhone, State.PLAYING_TRACKS.value, tracks, 0)
            return _play_command_or_error(tracks[0]["id"], request)

        elif ValName == "9" and CLEARING_ENABLED:
            await _save_session(ApiPhone, State.WAITING_FOR_DONATION_AMOUNT.value, [], 0)
            return make_ivr_read_command(
                f"הקישו סכום לתרומה בשקלים בין {int(DONATION_MIN_ILS)} ל {int(DONATION_MAX_ILS)} ואז סולמית",
                "1", "6", 12, "digits",
            )

        else:
            return make_ivr_read_command(MAIN_MENU_TEXT, "1", "1", 10, "digits")

    # ---------- SEARCH ----------
    elif state == State.WAITING_FOR_SEARCH.value:
        if not ValName or len(ValName) < 2 or ValName in ("1", "2", "*", "#"):
            return make_ivr_read_command("לא קלטתי בבירור, אנא אמרו שוב", "1", "50", 10, "voice")

        tracks = await search_youtube_innertube(ValName)
        if not tracks:
            tracks = get_emergency_playlist()

        await _save_session(ApiPhone, State.PLAYING_TRACKS.value, tracks, 0)
        return _play_command_or_error(tracks[0]["id"], request)

    # ---------- DONATION AMOUNT ----------
    elif state == State.WAITING_FOR_DONATION_AMOUNT.value:
        if not ValName or not AMOUNT_RE.match(ValName):
            return make_ivr_read_command("סכום לא תקין, אנא הקישו שוב מספר בשקלים", "1", "6", 10, "digits")

        amount = float(ValName)
        if amount < DONATION_MIN_ILS or amount > DONATION_MAX_ILS:
            return make_ivr_read_command(
                f"הסכום חייב להיות בין {int(DONATION_MIN_ILS)} ל {int(DONATION_MAX_ILS)} שקלים",
                "1", "6", 10, "digits",
            )

        success, message, tx_id = await charge_customer(ApiPhone, amount)
        logger.info("Payment attempt tx=%s phone=%s amount=%s success=%s", tx_id, ApiPhone, amount, success)
        await _save_session(ApiPhone, State.MAIN_MENU.value, [], 0)
        return make_ivr_read_command(message, "1", "1", 6, "digits")

    # ---------- PLAYING ----------
    elif state == State.PLAYING_TRACKS.value:
        if not playlist:
            playlist = get_emergency_playlist()
            index = 0

        total = len(playlist)
        index = (index % total) if total > 0 else 0

        if ValName == "2":
            index = (index - 1) % total
        elif ValName == "3":
            # משהים: לא כותבים ל-DB (אין שינוי state/playlist/index אמיתי)
            return make_ivr_read_command("הושהה • להמשך 4 • תפריט 0", "1", "1", 20, "digits")
        elif ValName == "5":
            random.shuffle(playlist)
            index = 0
        elif ValName == "6":
            curr = playlist[index]
            await run_db_query(
                "INSERT OR IGNORE INTO favorites (phone, video_id, title, created_at) VALUES (?, ?, ?, ?)",
                (ApiPhone, curr["id"], curr["title"], utcnow().isoformat()),
                commit=True,
            )
            # אין שינוי לפלייליסט/אינדקס — רק פידבק, וממשיכים לנגן את אותו שיר.
            return make_ivr_read_command("נוסף למועדפים • ממשיך...", "1", "1", 3, "digits")
        elif ValName == "0":
            await _save_session(ApiPhone, State.MAIN_MENU.value, [], 0)
            return make_ivr_read_command("חוזר לתפריט הראשי", "1", "1", 3, "digits")
        else:
            # ValName == "1" (הבא) או "" (ללא קלט / טיימאאוט) => מעבר לשיר הבא כברירת מחדל
            index = (index + 1) % total

        # כתיבה אטומית יחידה: state + playlist_json + current_index תמיד יחד,
        # כך ש-DB וה-RAM לעולם לא מתפצלים לגרסאות לא-מסונכרנות.
        await _save_session(ApiPhone, State.PLAYING_TRACKS.value, playlist, index)
        return _play_command_or_error(playlist[index]["id"], request)

    # מצב לא מוכר — נאפס בבטחה חזרה לתפריט במקום לתקוע את השיחה
    logger.warning("Unknown session state %r for phone=%s — resetting", state, ApiPhone)
    await _save_session(ApiPhone, State.MAIN_MENU.value, [], 0)
    return make_ivr_read_command(MAIN_MENU_TEXT, "1", "1", 10, "digits")


# ==========================================
# ❤️ Health check
# ==========================================
@app.get("/debug/search")
async def debug_search(q: str = Query(...), token: str = Query(None)):
    """כלי אבחון מהיר: בודק מה מנוע החיפוש בפועל מחזיר בלי לחכות לשיחת טלפון.
    מנוטרל לגמרי (404) אם IVR_DEBUG_TOKEN לא הוגדר — לא נחשף בטעות בפרודקשן."""
    if not DEBUG_TOKEN:
        raise HTTPException(404)
    if token != DEBUG_TOKEN:
        raise HTTPException(403, "Invalid token")
    tracks = await search_youtube_innertube(q)
    return {"query": q, "count": len(tracks), "tracks": tracks}


@app.get("/health")
async def health():
    db_ok = True
    try:
        await run_db_query("SELECT 1")
    except Exception:
        db_ok = False
    return {
        "status": "ok" if db_ok else "degraded",
        "db": db_ok,
        "clearing_enabled": CLEARING_ENABLED,
        "redis_enabled": _redis is not None,
        "whitelist_count": len(DEFAULT_WHITELIST),
        "time": utcnow().isoformat(),
    }
