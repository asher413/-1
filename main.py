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
import time
import random
import secrets
import sqlite3
import logging
import asyncio
import subprocess
import tempfile
from enum import Enum
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Tuple

import httpx
from fastapi import FastAPI, Query, Request, HTTPException
from fastapi.responses import PlainTextResponse, StreamingResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from cachetools import TTLCache

# ffmpeg מובא כבינארי ארוז דרך pip (imageio-ffmpeg) — בלי צורך בהתקנת מערכת
# (apt-get) שלא בטוח שתומכת בה תוכנית Render החינמית. נדרש כדי להמיר את
# האודיו לפורמט הטלפוני המדויק (WAV PCM, 8000Hz, מונו) שימות המשיח דורשת —
# גילינו אמפירית ש-convertAudio לא באמת ממיר בצד ימות (ר' הערה ב-Yemot upload).
try:
    import imageio_ffmpeg
    FFMPEG_BIN = imageio_ffmpeg.get_ffmpeg_exe()
except ImportError:
    imageio_ffmpeg = None
    FFMPEG_BIN = None

# ==========================================
# 📋 לוגר
# ==========================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("IVR_Production_Engine")

# חשוב לאבטחה: httpx מדפיס ברירת מחדל את ה-URL המלא של כל בקשה ברמת INFO —
# כולל query params. אצל ימות המשיח זה חושף את הסיסמה בטקסט גלוי בלוגים
# (ראינו את זה בפועל: "Login?username=...&password=..."). מנמיכים ל-WARNING
# כדי שרק שגיאות אמיתיות של httpx יופיעו, לא כל בקשה עם הסודות שבתוכה.
logging.getLogger("httpx").setLevel(logging.WARNING)

if FFMPEG_BIN is None:
    logger.warning(
        "imageio_ffmpeg not installed — add 'imageio-ffmpeg' to requirements.txt! "
        "Without it, audio uploaded to Yemot won't be converted to the required "
        "telephony WAV format (PCM, 8000Hz, mono) and playback will likely fail/sound wrong. "
        "Confirmed empirically: Yemot's own convertAudio parameter does NOT do this conversion."
    )


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

PLAY_COMMAND_TEMPLATE = os.environ.get(
    "IVR_PLAY_COMMAND_TEMPLATE",
    "read={base}/stream/{video_id}.mp3=ValName,no,1,0,2,digits,no",
)

YEMOT_PLAY_TEMPLATE = os.environ.get(
    "IVR_YEMOT_PLAY_TEMPLATE",
    "read={yemot_path}=ValName,no,1,0,2,digits,no",
)

RATE_LIMIT_PER_MINUTE = int(os.environ.get("IVR_RATE_LIMIT_PER_MINUTE", "20"))
SESSION_TTL_HOURS = int(os.environ.get("IVR_SESSION_TTL_HOURS", "4"))
MAX_PLAYLIST_SIZE = int(os.environ.get("IVR_MAX_PLAYLIST_SIZE", "15"))
SEARCH_RECURSION_DEPTH_LIMIT = 40
DB_READ_POOL_SIZE = max(1, int(os.environ.get("IVR_DB_READ_POOL_SIZE", "8")))

STREAM_MODE = os.environ.get("IVR_STREAM_MODE", "buffered").lower()
MAX_STREAM_BYTES = int(os.environ.get("IVR_MAX_STREAM_BYTES", str(30 * 1024 * 1024)))  # 30MB ~ מספיק לשיר ארוך מאוד

ANONYMOUS_PHONE_VALUES = {"0", "", "anonymous", "unknown", "withheld", "unavailable"}

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

YEMOT_SYSTEM_NUMBER = os.environ.get("YEMOT_SYSTEM_NUMBER", "")
YEMOT_PASSWORD = os.environ.get("YEMOT_PASSWORD", "")
YEMOT_API_KEY = os.environ.get("YEMOT_API_KEY", "")
YEMOT_ENABLED = bool(YEMOT_API_KEY or (YEMOT_SYSTEM_NUMBER and YEMOT_PASSWORD))
YEMOT_API_BASE = os.environ.get("YEMOT_API_BASE", "https://www.call2all.co.il/ym/api")
YEMOT_UPLOAD_FOLDER = os.environ.get("YEMOT_UPLOAD_FOLDER", "90").strip().strip("/")
if YEMOT_UPLOAD_FOLDER and not YEMOT_UPLOAD_FOLDER.lower().startswith("ivr2:"):
    YEMOT_UPLOAD_FOLDER = f"ivr2:/{YEMOT_UPLOAD_FOLDER}"

YEMOT_AUTO_DELETE_AFTER_PLAY = os.environ.get("YEMOT_AUTO_DELETE_AFTER_PLAY", "true").lower() == "true"
if not YEMOT_ENABLED:
    logger.info(
        "YEMOT_SYSTEM_NUMBER/YEMOT_PASSWORD (or YEMOT_API_KEY) not set — playback will use the "
        "direct-URL method, which Yemot Hamashiach is confirmed NOT to support. "
        "Set these env vars to enable the upload-first flow that actually works on Yemot."
    )
elif YEMOT_API_KEY:
    logger.info("Yemot auth: using YEMOT_API_KEY directly (bypasses username:password Login entirely)")
else:
    logger.info(
        "Yemot auth: using YEMOT_SYSTEM_NUMBER/YEMOT_PASSWORD (Login-based session token)."
    )
YEMOT_RECORDINGS_FOLDER = os.environ.get("YEMOT_RECORDINGS_FOLDER", "ivr2:/ai_recordings").rstrip("/")

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_STT_MODEL = os.environ.get("GROQ_STT_MODEL", "whisper-large-v3-turbo")
STT_ENABLED = bool(GROQ_API_KEY and YEMOT_ENABLED)
if GROQ_API_KEY and not YEMOT_ENABLED:
    logger.warning(
        "GROQ_API_KEY מוגדר אך YEMOT_SYSTEM_NUMBER/YEMOT_PASSWORD חסרים — "
        "בלי זה אי אפשר להוריד את קובץ ההקלטה מימות כדי לתמלל אותו, "
        "כך שזיהוי הדיבור החינמי מנוטרל וממשיכים עם voice הרגיל (בתשלום)."
    )
elif not GROQ_API_KEY:
    logger.info(
        "GROQ_API_KEY not set — voice search will use Yemot's built-in (paid) "
        "recognition. Set GROQ_API_KEY (free tier at console.groq.com) to use "
        "free transcription instead — no cost per search."
    )

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

INNERTUBE_KEY = os.environ.get("IVR_INNERTUBE_KEY", "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8")

YOUTUBE_DATA_API_KEY = os.environ.get("YOUTUBE_DATA_API_KEY", "")
YOUTUBE_DATA_API_ENABLED = bool(YOUTUBE_DATA_API_KEY)
if not YOUTUBE_DATA_API_ENABLED:
    logger.info(
        "YOUTUBE_DATA_API_KEY not set — search will rely entirely on scraping "
        "(InnerTube/Invidious/Piped), which is inherently less reliable. "
        "Setting a free Data API v3 key significantly improves search uptime."
    )

YOUTUBE_PROXY_BASE = os.environ.get("IVR_YOUTUBE_PROXY_BASE", "").rstrip("/")
YOUTUBE_PROXY_SECRET = os.environ.get("IVR_YOUTUBE_PROXY_SECRET", "")

_default_invidious = "https://invidious.projectsegfau.lt,https://yewtu.be,https://invidious.fdn.fr,https://iv.ggtyler.dev"
_default_piped = "https://pipedapi.kavin.rocks,https://api-piped.mha.fi,https://piped-api.lunar.icu"
INVIDIOUS_INSTANCES = [i.strip() for i in os.environ.get("IVR_INVIDIOUS_INSTANCES", _default_invidious).split(",") if i.strip()]
PIPED_INSTANCES = [i.strip() for i in os.environ.get("IVR_PIPED_INSTANCES", _default_piped).split(",") if i.strip()]

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
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS yemot_uploads (
                video_id TEXT PRIMARY KEY,
                yemot_path TEXT NOT NULL,
                uploaded_at TEXT NOT NULL
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS session_uploads (
                session_key TEXT,
                video_id TEXT,
                PRIMARY KEY (session_key, video_id)
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS yemot_file_counter (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                next_num INTEGER NOT NULL DEFAULT 1
            )
        """)
        cursor.execute("INSERT OR IGNORE INTO yemot_file_counter (id, next_num) VALUES (1, 1)")
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
# 🚦 Rate Limiting
# ==========================================
async def is_rate_limited(session_key: str) -> bool:
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
        return True


# ==========================================
# 🔎 חיפוש
# ==========================================
def _parse_iso8601_duration(iso: str) -> str:
    m = re.match(r"^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$", iso or "")
    if not m:
        return "00:00"
    h, mi, se = (int(x) if x else 0 for x in m.groups())
    if h:
        return f"{h}:{mi:02d}:{se:02d}"
    return f"{mi}:{se:02d}"


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
    ids = _VIDEO_ID_SCAN_RE.findall(raw_text or "")
    tracks = [{"id": vid, "title": "שיר ללא שם", "duration": "00:00", "author": "אמן"} for vid in ids]
    return _dedupe_and_trim(tracks)


async def search_youtube_data_api(query: str, filter_newest: bool = False) -> List[dict]:
    if not YOUTUBE_DATA_API_ENABLED:
        return []
    assert http_client is not None
    try:
        search_resp = await http_client.get(
            "https://www.googleapis.com/youtube/v3/search",
            params={
                "part": "snippet",
                "q": query,
                "type": "video",
                "maxResults": min(MAX_PLAYLIST_SIZE, 15),
                "regionCode": "IL",
                "relevanceLanguage": "he",
                "order": "date" if filter_newest else "relevance",
                "key": YOUTUBE_DATA_API_KEY,
            },
            timeout=7.0,
        )
        if search_resp.status_code != 200:
            try:
                err = search_resp.json().get("error", {})
                reason = (err.get("errors") or [{}])[0].get("reason", "")
            except (json.JSONDecodeError, KeyError, IndexError):
                reason = ""
            if reason == "quotaExceeded":
                logger.warning(
                    "YouTube Data API v3 quota exceeded for today — falling back "
                    "to scraping tiers until the quota resets (daily, Pacific time)."
                )
            else:
                logger.warning("YouTube Data API v3 search failed: %s %s", search_resp.status_code, reason)
            return []

        items = search_resp.json().get("items", [])
        prelim = []
        for item in items:
            vid = (item.get("id") or {}).get("videoId")
            snippet = item.get("snippet") or {}
            if not vid:
                continue
            prelim.append({
                "id": vid,
                "title": snippet.get("title", "שיר ללא שם"),
                "author": snippet.get("channelTitle", "אמן"),
                "duration": "00:00",
            })
        prelim = _dedupe_and_trim(prelim)
        if not prelim:
            return []

        try:
            ids_param = ",".join(t["id"] for t in prelim)
            details_resp = await http_client.get(
                "https://www.googleapis.com/youtube/v3/videos",
                params={"part": "contentDetails", "id": ids_param, "key": YOUTUBE_DATA_API_KEY},
                timeout=6.0,
            )
            if details_resp.status_code == 200:
                duration_by_id = {
                    d["id"]: _parse_iso8601_duration((d.get("contentDetails") or {}).get("duration", ""))
                    for d in details_resp.json().get("items", [])
                }
                for t in prelim:
                    t["duration"] = duration_by_id.get(t["id"], "00:00")
        except (httpx.HTTPError, asyncio.TimeoutError, json.JSONDecodeError) as e:
            logger.warning("YouTube Data API duration lookup failed (non-fatal): %s", e)

        logger.info("✅ YouTube Data API v3 success: %d tracks", len(prelim))
        return prelim

    except (httpx.HTTPError, asyncio.TimeoutError, json.JSONDecodeError) as e:
        logger.warning("YouTube Data API v3 request failed: %s", e)
        return []


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
                url_path = item.get("url", "")
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


async def _search_via_innertube_scrape(query: str, filter_newest: bool) -> List[dict]:
    api_base = YOUTUBE_PROXY_BASE or "https://www.youtube.com"
    url = f"{api_base}/youtubei/v1/search?key={INNERTUBE_KEY}&prettyPrint=false"
    payload = {
        "context": {
            "client": {
                "clientName": "WEB",
                "clientVersion": "2.20260601.01.00",
                "hl": "he",
                "gl": "IL",
                "platform": "DESKTOP",
                "clientFormFactor": "UNKNOWN_FORM_FACTOR",
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
        "X-YouTube-Client-Name": "1",
        "X-YouTube-Client-Version": "2.20260601.01.00",
    }
    if YOUTUBE_PROXY_BASE and YOUTUBE_PROXY_SECRET:
        headers["X-Proxy-Auth"] = YOUTUBE_PROXY_SECRET

    try:
        assert http_client is not None
        resp = await http_client.post(url, json=payload, headers=headers, timeout=7.0)
        logger.info("InnerTube status: %s for query: %s (via %s)", resp.status_code, query,
                    "proxy" if YOUTUBE_PROXY_BASE else "direct")
        if resp.status_code != 200:
            logger.warning("InnerTube non-200 body preview: %s", resp.text[:300].replace("\n", " "))
            return []

        try:
            raw_data = resp.json()
            tracks = extract_tracks_from_innertube(raw_data)
        except json.JSONDecodeError:
            raw_data = None
            tracks = []

        if not tracks:
            logger.warning("Structured parse got 0 tracks for query=%r — trying regex fallback", query)
            tracks = extract_tracks_regex_fallback(resp.text)

        if tracks:
            logger.info("✅ InnerTube parsed successfully: %d tracks", len(tracks))
            return tracks

        if raw_data is not None and isinstance(raw_data, dict):
            top_keys = list(raw_data.keys())
            estimated = raw_data.get("estimatedResults")
            logger.warning(
                "InnerTube returned 200 but 0 tracks for query=%r. top_level_keys=%s estimatedResults=%s.",
                query, top_keys, estimated,
            )
        else:
            logger.warning(
                "InnerTube returned 200 but non-JSON/undecodable body for query=%r. Preview: %s",
                query, resp.text[:300].replace("\n", " "),
            )
        return []
    except (httpx.HTTPError, asyncio.TimeoutError) as e:
        logger.error("InnerTube request failed: %s", e)
        return []


# ==========================================
# 🆕 yt-dlp — מנוע החיפוש/הורדה החדש (מחליף cobalt/rapidapi/invidious-stream)
# ==========================================
# הסיבה שהמערכת לא עבדה בפועל (ר' לוגים): כל שרשרת הורדת האודיו הישנה
# (cobalt.tools, RapidAPI, Invidious /latest_version) מתה או לא אמינה —
# ראינו בלוגים DNS שנכשל ל-cobalt, RapidAPI ללא תוצאה, ו-Invidious שמחזיר
# 22 בייטים של הודעת שגיאה טקסטואלית במקום קובץ אודיו אמיתי. yt-dlp הוא
# הכלי הפעיל-ביותר-מתוחזק (עדכונים כמעט יומיים) נגד שינויי ההגנות של
# יוטיוב, ומטפל בעצמו בפענוח החתימות/הצפנה שמקורות ה-scraping הידניים לא
# מסוגלים לעמוד בהם יותר. משתמשים בו גם לחיפוש (ytsearch) וגם להורדה.
try:
    import yt_dlp
    YTDLP_AVAILABLE = True
except ImportError:
    yt_dlp = None
    YTDLP_AVAILABLE = False
    logger.warning(
        "yt-dlp not installed — add 'yt-dlp' to requirements.txt! "
        "Without it, search and audio download will NOT work — this is now the primary engine."
    )

# עוגיות אופציונליות (קובץ בפורמט Netscape) — עוזר במקרים של הגבלת גיל/אזור
# או חסימת בוט אגרסיבית יותר מהרגיל על ה-IP של Render. לא חובה.
YTDLP_COOKIES_FILE = os.environ.get("YTDLP_COOKIES_FILE", "")
# פרוקסי אופציונלי (http/socks5) אם ה-IP של הפלטפורמה שלכם חסום ע"י יוטיוב.
YTDLP_PROXY = os.environ.get("YTDLP_PROXY", "")
# "player client" — לפעמים client=android/ios עוקף חסימות טוב יותר מ-web.
YTDLP_PLAYER_CLIENT = os.environ.get("YTDLP_PLAYER_CLIENT", "").strip()  # ריק = ברירת מחדל של yt-dlp
YTDLP_SEARCH_TIMEOUT = float(os.environ.get("YTDLP_SEARCH_TIMEOUT_SEC", "12"))
YTDLP_DOWNLOAD_TIMEOUT = float(os.environ.get("YTDLP_DOWNLOAD_TIMEOUT_SEC", "45"))

if not YOUTUBE_DATA_API_ENABLED:
    logger.info("Search engine order: yt-dlp (primary) → emergency playlist (no YOUTUBE_DATA_API_KEY set).")


def _ytdlp_base_opts() -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "socket_timeout": 10,
        "extractor_retries": 1,
        "ignoreerrors": True,
        "geo_bypass_country": "IL",
    }
    if YTDLP_COOKIES_FILE:
        opts["cookiefile"] = YTDLP_COOKIES_FILE
    if YTDLP_PROXY:
        opts["proxy"] = YTDLP_PROXY
    if YTDLP_PLAYER_CLIENT:
        opts["extractor_args"] = {"youtube": {"player_client": [YTDLP_PLAYER_CLIENT]}}
    if FFMPEG_BIN:
        opts["ffmpeg_location"] = os.path.dirname(FFMPEG_BIN)
    return opts


def _ytdlp_search_sync(query: str, limit: int, filter_newest: bool) -> List[dict]:
    """סינכרוני בכוונה — yt-dlp חוסם (I/O+CPU), חובה להריץ ב-executor."""
    if not YTDLP_AVAILABLE:
        return []
    opts = _ytdlp_base_opts()
    opts["extract_flat"] = "in_playlist"  # בקשה אחת, בלי לפתוח כל וידאו בנפרד => מהיר

    if filter_newest:
        import urllib.parse
        q = urllib.parse.quote(query)
        # sp=CAI%3D == מיון לפי תאריך העלאה בחיפוש הרגיל של יוטיוב
        target = f"https://www.youtube.com/results?search_query={q}&sp=CAI%3D"
    else:
        target = f"ytsearch{max(1, min(limit, 20))}:{query}"

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(target, download=False)
    except Exception as e:
        logger.warning("yt-dlp search raised: %s: %s", type(e).__name__, e)
        return []

    if not info:
        return []
    entries = info.get("entries") if isinstance(info, dict) else None
    if entries is None:
        entries = [info]

    tracks = []
    for e in entries:
        if not e:
            continue
        vid = e.get("id")
        if not vid or not VIDEO_ID_RE.match(vid):
            continue
        duration = e.get("duration")
        if isinstance(duration, (int, float)) and duration >= 0:
            duration = int(duration)
            duration_str = f"{duration // 60}:{duration % 60:02d}"
        else:
            duration_str = "00:00"
        tracks.append({
            "id": vid,
            "title": e.get("title") or "שיר ללא שם",
            "author": e.get("channel") or e.get("uploader") or "אמן",
            "duration": duration_str,
        })
        if len(tracks) >= limit:
            break
    return tracks


async def search_youtube_ytdlp(query: str, filter_newest: bool = False) -> List[dict]:
    if not YTDLP_AVAILABLE:
        return []
    loop = asyncio.get_running_loop()
    try:
        tracks = await asyncio.wait_for(
            loop.run_in_executor(None, _ytdlp_search_sync, query, MAX_PLAYLIST_SIZE, filter_newest),
            timeout=YTDLP_SEARCH_TIMEOUT,
        )
    except asyncio.TimeoutError:
        logger.warning("yt-dlp search timed out (query=%r)", query)
        return []
    tracks = _dedupe_and_trim(tracks)
    if tracks:
        logger.info("✅ yt-dlp search success: %d tracks", len(tracks))
    return tracks


def _ytdlp_download_audio_sync(video_id: str) -> Optional[bytes]:
    """מוריד את ערוץ האודיו הטוב ביותר של הסרטון לקובץ זמני ומחזיר את
    הבייטים הגולמיים. בכוונה *לא* מבקשים מ-yt-dlp להמיר ל-mp3/wav (אין
    postprocessor) — ההמרה לפורמט הטלפוני המדויק (PCM 8000Hz מונו) נעשית
    בנפרד דרך convert_to_telephony_wav, כדי לשמור שכבת אחריות אחת ברורה."""
    if not YTDLP_AVAILABLE:
        return None
    url = f"https://www.youtube.com/watch?v={video_id}"
    with tempfile.TemporaryDirectory(prefix="ytdlp_") as tmpdir:
        outtmpl = os.path.join(tmpdir, "%(id)s.%(ext)s")
        opts = _ytdlp_base_opts()
        opts.update({
            "format": "bestaudio/best",
            "outtmpl": outtmpl,
            "skip_download": False,
            "noprogress": True,
        })
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                if info is None:
                    logger.warning("yt-dlp returned no info for %s (video unavailable/blocked?)", video_id)
                    return None
                filename = ydl.prepare_filename(info)
        except Exception as e:
            logger.warning("yt-dlp download failed for %s: %s: %s", video_id, type(e).__name__, e)
            return None

        if not os.path.exists(filename):
            # לפעמים הסיומת בפועל שונה ממה ש-prepare_filename ניחש (תלוי קודק) —
            # ניקח כל קובץ שכן נוצר בתיקייה הזמנית.
            candidates = [f for f in os.listdir(tmpdir) if not f.startswith(".")]
            if not candidates:
                logger.error("yt-dlp reported success but no output file exists for %s", video_id)
                return None
            filename = os.path.join(tmpdir, candidates[0])

        try:
            with open(filename, "rb") as f:
                data = f.read()
        except OSError as e:
            logger.error("Failed reading yt-dlp output file for %s: %s", video_id, e)
            return None

        if len(data) < 1000:
            logger.error("yt-dlp output suspiciously small (%d bytes) for %s", len(data), video_id)
            return None

        logger.info("✅ yt-dlp download succeeded for %s: %d bytes", video_id, len(data))
        return data


async def download_audio_via_ytdlp(video_id: str) -> Optional[bytes]:
    if not YTDLP_AVAILABLE:
        logger.error("yt-dlp not installed — cannot download audio for %s", video_id)
        return None
    loop = asyncio.get_running_loop()
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(None, _ytdlp_download_audio_sync, video_id),
            timeout=YTDLP_DOWNLOAD_TIMEOUT,
        )
    except asyncio.TimeoutError:
        logger.warning("yt-dlp download timed out for %s", video_id)
        return None


async def search_youtube_innertube(query: str, filter_newest: bool = False) -> List[dict]:
    """מנוע החיפוש הראשי — סדר עדכני (2026):
    0) YouTube Data API v3 הרשמי (אם מוגדר מפתח) — הכי מהיר וזול, לא נשבר,
       אבל מוגבל למכסה יומית (~100 חיפושים/יום בחינם).
    1) yt-dlp (ytsearch) — לא צריך מפתח API, מתוחזק באופן פעיל נגד שינויי
       יוטיוב, ומחליף את כל שכבות ה-scraping הידניות הישנות (InnerTube גולמי,
       Invidious, Piped) שהפסיקו לעבוד באופן אמין.
    2) InnerTube/Invidious/Piped — נשארו כרשת ביטחון אחרונה-לפני-חירום, אבל
       לא מהימנות יותר; ר' לוגים שהראו שהן מתות/חוסמות.
    3) פלייליסט חירום — כדי שלעולם לא תיתקע שיחה בלי שום שיר.
    כל שכבה שמצליחה נשמרת בקאש (Redis אם מוגדר, אחרת מקומי) ל-15 דקות."""
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

    if YOUTUBE_DATA_API_ENABLED:
        tracks = await search_youtube_data_api(query, filter_newest)
        if tracks:
            await cache_set(search_cache, "search", cache_key, json.dumps(tracks, ensure_ascii=False), 900)
            return tracks
        logger.info("Data API v3 unavailable/empty → trying yt-dlp search")

    tracks = await search_youtube_ytdlp(query, filter_newest)
    if tracks:
        await cache_set(search_cache, "search", cache_key, json.dumps(tracks, ensure_ascii=False), 900)
        return tracks

    logger.info("yt-dlp search failed/empty → trying legacy InnerTube scrape")
    tracks = await _search_via_innertube_scrape(query, filter_newest)
    if tracks:
        await cache_set(search_cache, "search", cache_key, json.dumps(tracks, ensure_ascii=False), 900)
        return tracks

    logger.info("InnerTube scrape failed → trying Invidious fallback")
    fallback_tracks = await search_invidious_fallback(query)
    if fallback_tracks:
        await cache_set(search_cache, "search", cache_key, json.dumps(fallback_tracks, ensure_ascii=False), 900)
        return fallback_tracks

    logger.info("Invidious failed → trying Piped fallback")
    fallback_tracks = await search_piped_fallback(query)
    if fallback_tracks:
        await cache_set(search_cache, "search", cache_key, json.dumps(fallback_tracks, ensure_ascii=False), 900)
        return fallback_tracks

    logger.error(
        "All search backends failed (query=%r) → serving Emergency playlist. "
        "אם גם yt-dlp נכשל, בדקו קודם כל שהחבילה yt-dlp מותקנת ומעודכנת "
        "(pip install -U yt-dlp) — יוטיוב משנה הגנות לעיתים קרובות והגרסה "
        "חייבת להיות עדכנית; שקלו גם YTDLP_PROXY אם ה-IP של Render חסום.",
        query,
    )
    return get_emergency_playlist()


# ==========================================
# 🎧 Streaming — מקור האודיו היחיד כעת הוא yt-dlp (ר' הערה למעלה)
# ==========================================
def _parse_range_header(range_header: Optional[str], total_len: int) -> Optional[Tuple[int, int]]:
    """מפרש 'Range: bytes=START-END' בסיסי. מחזיר (start, end) כולל, או None אם
    אין/לא תקין (ואז שולחים את הקובץ המלא — זה תמיד תקין, גם אם הלקוח ביקש range)."""
    if not range_header or not range_header.startswith("bytes="):
        return None
    try:
        spec = range_header.split("=", 1)[1].split(",")[0].strip()
        start_s, _, end_s = spec.partition("-")
        start = int(start_s) if start_s else 0
        end = int(end_s) if end_s else total_len - 1
        start = max(0, start)
        end = min(total_len - 1, end)
        if start > end or start >= total_len:
            return None
        return start, end
    except (ValueError, IndexError):
        return None


@app.api_route("/stream/{video_id}.mp3", methods=["GET", "HEAD"])
async def proxy_mp3_stream(video_id: str, request: Request):
    # לוג בולט: אם השורה הזו לעולם לא מופיעה בלוג אחרי שפקודת
    # "read=.../stream/..." הוחזרה למרכזיה, ההסבר הוא שימות המשיח מעולם לא
    # תמכה בניגון מ-URL חיצוני (מאומת מול הפורום הרשמי) — לא באג כאן.
    # /stream משמש היום רק כ-fallback לפלטפורמות אחרות שכן תומכות ב-URL חיצוני.
    client_ip = request.headers.get("x-forwarded-for", request.client.host if request.client else "unknown")
    logger.info("🔊 /stream request RECEIVED (%s) for video_id=%s from %s", request.method, video_id, client_ip)

    if not VIDEO_ID_RE.match(video_id):
        logger.warning("🔊 /stream rejected: invalid video_id=%r", video_id)
        raise HTTPException(400, "Invalid video ID")

    audio_bytes = await download_audio_via_ytdlp(video_id)
    if not audio_bytes:
        logger.error("All stream sources exhausted for video_id=%s", video_id)
        raise HTTPException(502, "No available audio source for this track")

    total_len = len(audio_bytes)
    base_headers = {
        "Content-Type": "audio/mpeg",
        "Accept-Ranges": "bytes",
        "Cache-Control": "no-store",
    }

    if request.method == "HEAD":
        headers = {**base_headers, "Content-Length": str(total_len)}
        return Response(status_code=200, headers=headers)

    range_header = request.headers.get("range")
    parsed_range = _parse_range_header(range_header, total_len)
    if parsed_range is not None:
        start, end = parsed_range
        chunk = audio_bytes[start:end + 1]
        headers = {
            **base_headers,
            "Content-Length": str(len(chunk)),
            "Content-Range": f"bytes {start}-{end}/{total_len}",
        }
        logger.info("🔊 /stream SUCCESS (206 partial %d-%d/%d) for %s via yt-dlp", start, end, total_len, video_id)
        return Response(content=chunk, status_code=206, media_type="audio/mpeg", headers=headers)

    headers = {**base_headers, "Content-Length": str(total_len)}
    logger.info("🔊 /stream SUCCESS (200, %d bytes) for %s via yt-dlp", total_len, video_id)
    return Response(content=audio_bytes, media_type="audio/mpeg", headers=headers)


async def download_audio_bytes(video_id: str) -> Optional[bytes]:
    """נקודת כניסה יחידה להורדת אודיו גולמי (לא ממומר) — כעת דרך yt-dlp בלבד."""
    return await download_audio_via_ytdlp(video_id)


async def download_and_convert_telephony_wav(video_id: str) -> Optional[bytes]:
    """מוריד עם yt-dlp וממיר ל-WAV הטלפוני הנדרש לימות המשיח (PCM 16-bit,
    8000Hz, מונו). ממשיך להיכשל בבירור (None) אם ההורדה או ההמרה נכשלו,
    כדי שהקורא (ensure_uploaded_to_yemot) ידע לדווח שגיאה נכונה ולא ינגן
    שקט/שיבוש בשיחה חיה."""
    raw = await download_audio_via_ytdlp(video_id)
    if not raw:
        return None
    converted = await convert_to_telephony_wav(raw)
    if not converted:
        logger.warning("Conversion failed for yt-dlp audio (video=%s)", video_id)
        return None
    return converted


def _convert_to_telephony_wav_sync(input_bytes: bytes) -> Optional[bytes]:
    """ממיר אודיו כלשהו (מכל קודק שיוטיוב סיפק — webm/opus/m4a/מה שלא יהיה)
    ל-WAV PCM 16-bit, 8000Hz, מונו — פורמט הטלפוניה המדויק שימות המשיח דורשת.
    פונקציה חוסמת (subprocess) — יש להריץ דרך run_in_executor.

    קריטי (מתועד רשמית ע"י ימות): פרמטר convertAudio="1" של UploadFile *כן*
    קיים וקביל לפי הדוגמה הרשמית שלהם, אבל אנחנו ממירים כבר בצד שלנו כדי
    לשלוט באופן מדויק בפורמט (8000Hz מונו) ולא להסתמך על מה שהמרה בצד-שרת
    עושה בפועל — זה עדיין הכי אמין."""
    if FFMPEG_BIN is None:
        logger.error("Cannot convert audio to WAV — imageio_ffmpeg not installed")
        return None
    if not input_bytes or len(input_bytes) < 100:
        logger.error("Input audio is suspiciously small (%d bytes) — likely not real audio, skipping conversion",
                     len(input_bytes) if input_bytes else 0)
        return None
    with tempfile.NamedTemporaryFile(suffix=".in", delete=False) as fin:
        fin.write(input_bytes)
        in_path = fin.name
    out_path = in_path + ".wav"
    try:
        result = subprocess.run(
            [FFMPEG_BIN, "-y", "-i", in_path, "-ar", "8000", "-ac", "1", "-acodec", "pcm_s16le", out_path],
            capture_output=True, timeout=30,
        )
        if result.returncode != 0:
            stderr_text = result.stderr.decode(errors="replace")
            logger.error(
                "ffmpeg conversion failed (input=%d bytes, returncode=%d). Last part of stderr: ...%s",
                len(input_bytes), result.returncode, stderr_text[-1500:],
            )
            return None
        with open(out_path, "rb") as f:
            converted = f.read()
        if len(converted) < 100:
            logger.error("ffmpeg produced suspiciously small output (%d bytes) despite returncode=0", len(converted))
            return None
        return converted
    except (subprocess.TimeoutExpired, OSError) as e:
        logger.error("ffmpeg conversion error (input=%d bytes): %s", len(input_bytes), e)
        return None
    finally:
        for p in (in_path, out_path):
            try:
                if os.path.exists(p):
                    os.remove(p)
            except OSError:
                pass


async def convert_to_telephony_wav(input_bytes: bytes) -> Optional[bytes]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _convert_to_telephony_wav_sync, input_bytes)


# ==========================================
# 📤 ימות המשיח: Login + UploadFile (upload-first playback)
# ==========================================
# מאומת מול הפורום הרשמי + התיעוד הרשמי של ימות: ניגון ישירות מ-URL חיצוני
# *לא נתמך* — יש להעלות כל קובץ מראש עם UploadFile ואז לנגן לפי נתיב פנימי.
# הנתיב שכן הוכח כעובד בלוגים שלכם בפועל: 'ivr2:/<שלוחה>/<קובץ>.wav'
# (ר' "🗑️ Yemot file deleted: ivr2:/90/1.wav" בלוג — זה מוכיח שהעלאה קודמת
# הצליחה ואז נמחקה בהצלחה). כלומר: שכבת ה-Yemot upload כבר עובדת נכון; מה
# שהיה שבור זה אך ורק הורדת האודיו המקורי מיוטיוב (עכשיו מתוקן ע"י yt-dlp).
_yemot_token: Optional[str] = None
_yemot_token_expires_at: Optional[datetime] = None
_yemot_login_lock = asyncio.Lock()


async def _yemot_login(force: bool = False) -> Optional[str]:
    global _yemot_token, _yemot_token_expires_at

    if YEMOT_API_KEY:
        return YEMOT_API_KEY

    if not YEMOT_ENABLED:
        return None

    async with _yemot_login_lock:
        if not force and _yemot_token and _yemot_token_expires_at and utcnow() < _yemot_token_expires_at:
            return _yemot_token
        try:
            assert http_client is not None
            resp = await http_client.post(
                f"{YEMOT_API_BASE}/Login",
                data={"username": YEMOT_SYSTEM_NUMBER, "password": YEMOT_PASSWORD},
                timeout=10.0,
            )
            data = resp.json()
            if data.get("responseStatus") != "OK" or not data.get("token"):
                logger.error(
                    "Yemot Login failed: %s — אם זה FORBIDDEN/EXCEPTION, שקלו להגדיר YEMOT_API_KEY במקום.",
                    _safe_json_snippet_early(data),
                )
                return None
            _yemot_token = data["token"]
            _yemot_token_expires_at = utcnow() + timedelta(minutes=25)
            logger.info("Yemot Login OK — token cached for ~25 minutes")
            return _yemot_token
        except (httpx.HTTPError, asyncio.TimeoutError, json.JSONDecodeError) as e:
            logger.error("Yemot Login request failed: %s", e)
            return None


def _safe_json_snippet_early(data, limit: int = 500) -> str:
    try:
        s = json.dumps(data, ensure_ascii=False)
    except (TypeError, ValueError):
        s = str(data)
    return s[:limit]


_yemot_dirs_ensured: set = set()


async def _yemot_write_ext_ini(folder_path: str, contents: str) -> bool:
    token = await _yemot_login()
    if not token:
        return False
    try:
        assert http_client is not None
        resp = await http_client.post(
            f"{YEMOT_API_BASE}/UploadTextFile",
            data={"token": token, "what": f"{folder_path}/ext.ini", "contents": contents},
            timeout=10.0,
        )
        data = resp.json()
        ok = data.get("responseStatus") == "OK"
        logger.info("Yemot UploadTextFile(ext.ini @ %s/ext.ini): %s", folder_path, _safe_json_snippet_early(data))
        return ok
    except (httpx.HTTPError, asyncio.TimeoutError, json.JSONDecodeError) as e:
        logger.warning("Yemot UploadTextFile(ext.ini) failed for %s: %s", folder_path, e)
        return False


async def _yemot_ensure_dir(path: str) -> bool:
    if path in _yemot_dirs_ensured:
        return True
    token = await _yemot_login()
    if not token:
        return False

    ok_update_extension = False
    try:
        assert http_client is not None
        resp = await http_client.post(
            f"{YEMOT_API_BASE}/UpdateExtension",
            data={"token": token, "path": path, "type": "playfile"},
            timeout=10.0,
        )
        data = resp.json()
        ok_update_extension = data.get("responseStatus") == "OK"
        logger.info("Yemot UpdateExtension(%s, type=playfile): %s", path, _safe_json_snippet_early(data))
    except (httpx.HTTPError, asyncio.TimeoutError, json.JSONDecodeError) as e:
        logger.warning("Yemot UpdateExtension failed for %s: %s", path, e)

    ok_ext_ini = await _yemot_write_ext_ini(path, "type=playfile\n")

    ok = ok_update_extension or ok_ext_ini
    if ok:
        _yemot_dirs_ensured.add(path)
    return ok


async def _next_yemot_file_number() -> int:
    row = await run_db_query("SELECT next_num FROM yemot_file_counter WHERE id = 1")
    num = row[0] if row else 1
    await run_db_query(
        "INSERT INTO yemot_file_counter (id, next_num) VALUES (1, ?) "
        "ON CONFLICT(id) DO UPDATE SET next_num = next_num + 1",
        (num + 1,), commit=True,
    )
    return num


def _bare_yemot_extension() -> str:
    folder = YEMOT_UPLOAD_FOLDER
    if folder.lower().startswith("ivr2:"):
        folder = folder[5:]
    return folder.strip("/")


def _yemot_path_candidates(dest_filename: str) -> List[str]:
    """נתיב ראשון ברשימה הוא הפורמט המאומת-עובד בפועל (מוכח מהלוגים שלכם:
    'ivr2:/90/1.wav' הועלה ונמחק בהצלחה) — גם תואם לדוגמת הקוד הרשמית של
    ימות (UploadFile: path=`ivr2:/${path}.wav`). האחרים הם רשת ביטחון בלבד."""
    ext_num = _bare_yemot_extension()
    return [
        f"{YEMOT_UPLOAD_FOLDER}/{dest_filename}",
        f"ivr/{ext_num}/{dest_filename}",
        f"ivr/{ext_num}/1/{dest_filename}",
        f"ivr/1/{ext_num}/{dest_filename}",
    ]


async def _yemot_upload_file(video_id: str, converted_wav_bytes: bytes) -> Optional[str]:
    converted = converted_wav_bytes
    file_num = await _next_yemot_file_number()
    dest_filename = f"{file_num}.wav"
    is_new_folder = YEMOT_UPLOAD_FOLDER not in _yemot_dirs_ensured
    await _yemot_ensure_dir(YEMOT_UPLOAD_FOLDER)

    candidates = _yemot_path_candidates(dest_filename)

    async def _do_upload(token: str, path: str) -> httpx.Response:
        assert http_client is not None
        return await http_client.post(
            f"{YEMOT_API_BASE}/UploadFile",
            data={"token": token, "path": path},
            files={"file": (dest_filename, converted, "audio/wav")},
            timeout=30.0,
        )

    token = await _yemot_login()
    if not token:
        return None

    last_error_data = None
    for i, candidate_path in enumerate(candidates):
        try:
            resp = await _do_upload(token, candidate_path)
            data = resp.json()
        except (httpx.HTTPError, asyncio.TimeoutError, json.JSONDecodeError) as e:
            logger.error("Yemot UploadFile request failed for %s (path=%r): %s", video_id, candidate_path, e)
            continue

        if data.get("responseStatus") == "OK":
            canonical_path = data.get("path") or candidate_path
            logger.info("✅ Yemot UploadFile success for %s → %s (scheme %d/%d)",
                        video_id, canonical_path, i + 1, len(candidates))
            return canonical_path

        last_error_data = data
        logger.warning("Yemot UploadFile scheme %d/%d failed for %s (path=%r): %s",
                        i + 1, len(candidates), video_id, candidate_path, _safe_json_snippet_early(data))

    logger.warning("Yemot UploadFile: all %d path schemes failed for %s. Last error: %s",
                    len(candidates), video_id, _safe_json_snippet_early(last_error_data))

    if is_new_folder:
        logger.info("🕒 Starting background warm-up retry for new Yemot folder %s (up to ~2 min)",
                    YEMOT_UPLOAD_FOLDER)
        asyncio.create_task(_yemot_upload_warmup(video_id, converted, dest_filename))

    return None


async def _yemot_upload_warmup(video_id: str, converted_wav_bytes: bytes, dest_filename: str) -> None:
    candidates = _yemot_path_candidates(dest_filename)
    for delay in (10, 20, 30, 45):
        await asyncio.sleep(delay)
        token = await _yemot_login()
        if not token:
            continue

        for candidate_path in candidates:
            try:
                assert http_client is not None
                resp = await http_client.post(
                    f"{YEMOT_API_BASE}/UploadFile",
                    data={"token": token, "path": candidate_path},
                    files={"file": (dest_filename, converted_wav_bytes, "audio/wav")},
                    timeout=30.0,
                )
                data = resp.json()
            except (httpx.HTTPError, asyncio.TimeoutError, json.JSONDecodeError) as e:
                logger.warning("Yemot warm-up upload attempt failed (path=%r): %s", candidate_path, e)
                continue

            if data.get("responseStatus") == "OK":
                canonical_path = data.get("path") or candidate_path
                logger.info("✅ Yemot warm-up upload succeeded for %s → %s", video_id, canonical_path)
                await run_db_query(
                    "INSERT OR REPLACE INTO yemot_uploads (video_id, yemot_path, uploaded_at) VALUES (?, ?, ?)",
                    (video_id, canonical_path, utcnow().isoformat()),
                    commit=True,
                )
                return
            logger.warning("Yemot warm-up still failing (path=%r): %s",
                            candidate_path, _safe_json_snippet_early(data))

    logger.error(
        "Yemot warm-up gave up after ~105s for folder %s (tried %d path schemes each round).",
        YEMOT_UPLOAD_FOLDER, len(candidates),
    )


async def _yemot_delete_file(path: str) -> bool:
    token = await _yemot_login()
    if not token:
        return False
    try:
        assert http_client is not None
        resp = await http_client.post(
            f"{YEMOT_API_BASE}/FileAction",
            data={"token": token, "action": "delete", "what": path},
            timeout=10.0,
        )
        data = resp.json()
        ok = data.get("responseStatus") == "OK"
        if ok:
            logger.info("🗑️ Yemot file deleted: %s", path)
        else:
            logger.warning("Yemot delete failed for %s: %s", path, _safe_json_snippet_early(data))
        return ok
    except (httpx.HTTPError, asyncio.TimeoutError, json.JSONDecodeError) as e:
        logger.warning("Yemot FileAction(delete) request failed for %s: %s", path, e)
        return False


async def _track_session_upload(session_key: str, video_id: str) -> None:
    await run_db_query(
        "INSERT OR IGNORE INTO session_uploads (session_key, video_id) VALUES (?, ?)",
        (session_key, video_id), commit=True,
    )


async def cleanup_session_uploads(session_key: str) -> None:
    if not YEMOT_ENABLED or not YEMOT_AUTO_DELETE_AFTER_PLAY:
        return
    rows = await run_db_query(
        "SELECT video_id FROM session_uploads WHERE session_key = ?", (session_key,), fetchall=True
    )
    if not rows:
        return
    for (video_id,) in rows:
        cached = await run_db_query("SELECT yemot_path FROM yemot_uploads WHERE video_id = ?", (video_id,))
        if cached:
            await _yemot_delete_file(cached[0])
            await run_db_query("DELETE FROM yemot_uploads WHERE video_id = ?", (video_id,), commit=True)
    await run_db_query("DELETE FROM session_uploads WHERE session_key = ?", (session_key,), commit=True)
    logger.info("🧹 Cleaned up %d uploaded track(s) for session=%s after hangup", len(rows), session_key)


async def ensure_uploaded_to_yemot(video_id: str, session_key: Optional[str] = None) -> Optional[str]:
    if not YEMOT_ENABLED:
        return None

    cached = await run_db_query("SELECT yemot_path FROM yemot_uploads WHERE video_id = ?", (video_id,))
    if cached:
        if session_key and YEMOT_AUTO_DELETE_AFTER_PLAY:
            await _track_session_upload(session_key, video_id)
        return cached[0]

    converted_wav = await download_and_convert_telephony_wav(video_id)
    if not converted_wav:
        logger.error("Could not download+convert audio for %s — cannot upload to Yemot", video_id)
        return None

    yemot_path = await _yemot_upload_file(video_id, converted_wav)
    if not yemot_path:
        return None

    await run_db_query(
        "INSERT OR REPLACE INTO yemot_uploads (video_id, yemot_path, uploaded_at) VALUES (?, ?, ?)",
        (video_id, yemot_path, utcnow().isoformat()),
        commit=True,
    )
    if session_key and YEMOT_AUTO_DELETE_AFTER_PLAY:
        await _track_session_upload(session_key, video_id)
    return yemot_path


async def yemot_download_file(path: str) -> Optional[bytes]:
    if not YEMOT_ENABLED:
        return None
    token = await _yemot_login()
    if not token:
        return None
    try:
        assert http_client is not None
        resp = await http_client.post(
            f"{YEMOT_API_BASE}/DownloadFile",
            data={"token": token, "path": path},
            timeout=15.0,
        )
        if resp.status_code != 200 or len(resp.content) == 0:
            logger.warning("Yemot DownloadFile failed for path=%r: status=%s", path, resp.status_code)
            return None
        return resp.content
    except (httpx.HTTPError, asyncio.TimeoutError) as e:
        logger.warning("Yemot DownloadFile request failed for path=%r: %s", path, e)
        return None


def _looks_like_yemot_path(value: str) -> bool:
    if not value:
        return False
    v = value.strip().lower()
    return (
        v.startswith("ivr2:") or v.startswith("/") or
        v.endswith(".wav") or v.endswith(".mp3") or v.endswith(".ogg")
    )


async def transcribe_audio_bytes(audio_bytes: bytes, filename: str = "recording.wav") -> Optional[str]:
    if not GROQ_API_KEY:
        return None
    try:
        assert http_client is not None
        resp = await http_client.post(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            data={"model": GROQ_STT_MODEL, "language": "he", "response_format": "json"},
            files={"file": (filename, audio_bytes, "audio/wav")},
            timeout=20.0,
        )
        if resp.status_code != 200:
            logger.warning("Groq transcription failed: %s %s", resp.status_code, resp.text[:200])
            return None
        text = (resp.json() or {}).get("text", "").strip()
        return text or None
    except (httpx.HTTPError, asyncio.TimeoutError, json.JSONDecodeError) as e:
        logger.warning("Groq transcription request failed: %s", e)
        return None


async def resolve_search_query_from_valname(val_name: str) -> Optional[str]:
    if STT_ENABLED and _looks_like_yemot_path(val_name):
        audio_bytes = await yemot_download_file(val_name.strip())
        if not audio_bytes:
            logger.error("Could not download recording from Yemot at path=%r for transcription", val_name)
            return None
        text = await transcribe_audio_bytes(audio_bytes)
        if text:
            logger.info("🎙️ Free transcription result: %r (from recording %s)", text, val_name)
        else:
            logger.warning("Transcription returned empty/failed for recording %s", val_name)
        return text

    return val_name


# ==========================================
# 💳 סליקת תשלומים (אופציונלי)
# ==========================================
def _safe_json_snippet(data, limit: int = 2000) -> str:
    try:
        full = json.dumps(data, ensure_ascii=False)
    except (TypeError, ValueError):
        full = str(data)
    if len(full) <= limit:
        return full
    return json.dumps({"_truncated": True, "raw_prefix": full[: max(0, limit - 60)]}, ensure_ascii=False)


async def charge_customer(phone: str, amount_ils: float) -> Tuple[bool, str, str]:
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


def make_ivr_record_command(text: str, max_seconds: int) -> str:
    clean = clean_text_for_ivr(text)
    return f"read=t-{clean}=ValName,no,,,{max_seconds},record,no"


def _get_url_based_play_command(video_id: str, request: Request) -> Optional[str]:
    if PUBLIC_BASE_URL:
        base = PUBLIC_BASE_URL
    else:
        raw_host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
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


async def _play_command_or_error(video_id: str, request: Request, session_key: Optional[str] = None) -> str:
    if YEMOT_ENABLED:
        yemot_path = await ensure_uploaded_to_yemot(video_id, session_key)
        if yemot_path:
            return YEMOT_PLAY_TEMPLATE.format(yemot_path=yemot_path, video_id=video_id)
        logger.warning(
            "Yemot upload failed/unavailable for %s — falling back to URL-based play command "
            "(won't work on real Yemot, but keeps the call from dying silently)", video_id,
        )

    cmd = _get_url_based_play_command(video_id, request)
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
    backoff = 5
    while True:
        try:
            await active_session_cleanup()
            return
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
@app.api_route("/youtube", methods=["GET", "POST"], response_class=PlainTextResponse)
async def handle_ivr(request: Request):
    all_items = list(request.query_params.multi_items())
    if request.method == "POST":
        try:
            content_type = request.headers.get("content-type", "")
            if "form" in content_type:
                form = await request.form()
                all_items.extend(form.multi_items())
        except Exception as e:
            logger.warning("Failed to parse POST body for /youtube: %s", e)

    def _get_last(key: str) -> Optional[str]:
        values = [v for k, v in all_items if k == key]
        return values[-1] if values else None

    ApiPhone = _get_last("ApiPhone")
    hangup = _get_last("hangup")

    if not ApiPhone:
        return "OK"

    raw_phone = ApiPhone.strip()
    is_anonymous = raw_phone.lower() in ANONYMOUS_PHONE_VALUES

    if is_anonymous:
        call_id = (_get_last("ApiCallId") or _get_last("ApiYFCallId") or "").strip()
        if not call_id:
            logger.warning("Anonymous caller with no call id — rejecting")
            return "OK"
        session_key = f"anon:{call_id[:64]}"
    else:
        if not PHONE_RE.match(raw_phone):
            logger.warning("Rejected malformed phone: %r", raw_phone)
            return "OK"
        session_key = raw_phone

    if hangup == "yes":
        if YEMOT_AUTO_DELETE_AFTER_PLAY:
            asyncio.create_task(cleanup_session_uploads(session_key))
        return "OK"

    ValName = _get_last("ValName")
    if ValName is not None:
        ValName = ValName.strip()[:150]

    logger.info("📞 Phone: %s | Session: %s | Method: %s | ValName: %r",
                raw_phone, session_key, request.method, ValName)

    try:
        if await is_rate_limited(session_key):
            result = make_ivr_read_command("בוצעו יותר מדי פעולות אנא המתן מעט", "1", "1", 5, "digits")
        else:
            async with get_phone_lock(session_key):
                result = await _handle_ivr_locked(request, session_key, ValName, is_anonymous)
    except Exception as e:
        logger.exception("Unhandled error in IVR handler for session=%s: %s", session_key, e)
        result = _generic_error_command()

    logger.info("📤 Returning to IVR (session=%s): %s", session_key, result)
    return result


async def _handle_ivr_locked(
    request: Request, ApiPhone: str, ValName: Optional[str], is_anonymous: bool = False
) -> str:
    if is_anonymous:
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

    if state == State.MAIN_MENU.value:
        if ValName == "1":
            await _save_session(ApiPhone, State.WAITING_FOR_SEARCH.value, [], 0)
            if STT_ENABLED:
                return make_ivr_record_command("אנא אמרו את שם השיר לאחר הצליל", max_seconds=10)
            return make_ivr_read_command("אנא אמרו את שם השיר לאחר הצליל", "1", "50", 10, "voice")

        elif ValName == "2":
            tracks = await search_youtube_innertube("שירים חסידיים חדשים", filter_newest=True)
            if not tracks:
                tracks = get_emergency_playlist()
            await _save_session(ApiPhone, State.PLAYING_TRACKS.value, tracks, 0)
            return await _play_command_or_error(tracks[0]["id"], request, ApiPhone)

        elif ValName == "3":
            favs = await run_db_query(
                "SELECT video_id, title FROM favorites WHERE phone = ? ORDER BY created_at DESC",
                (ApiPhone,), fetchall=True,
            )
            if not favs:
                return make_ivr_read_command("רשימת המועדפים ריקה", "1", "1", 4, "digits")
            tracks = [{"id": f[0], "title": f[1], "duration": "00:00", "author": ""} for f in favs]
            await _save_session(ApiPhone, State.PLAYING_TRACKS.value, tracks, 0)
            return await _play_command_or_error(tracks[0]["id"], request, ApiPhone)

        elif ValName == "9" and CLEARING_ENABLED:
            await _save_session(ApiPhone, State.WAITING_FOR_DONATION_AMOUNT.value, [], 0)
            return make_ivr_read_command(
                f"הקישו סכום לתרומה בשקלים בין {int(DONATION_MIN_ILS)} ל {int(DONATION_MAX_ILS)} ואז סולמית",
                "1", "6", 12, "digits",
            )

        else:
            return make_ivr_read_command(MAIN_MENU_TEXT, "1", "1", 10, "digits")

    elif state == State.WAITING_FOR_SEARCH.value:
        if not ValName or len(ValName) < 2 or ValName in ("1", "2", "*", "#"):
            retry_cmd = (
                make_ivr_record_command("לא קלטתי בבירור, אנא אמרו שוב", max_seconds=10)
                if STT_ENABLED else
                make_ivr_read_command("לא קלטתי בבירור, אנא אמרו שוב", "1", "50", 10, "voice")
            )
            return retry_cmd

        query = await resolve_search_query_from_valname(ValName)
        if not query:
            retry_cmd = (
                make_ivr_record_command("לא הצלחתי להבין, אנא נסו שוב", max_seconds=10)
                if STT_ENABLED else
                make_ivr_read_command("לא הצלחתי להבין, אנא נסו שוב", "1", "50", 10, "voice")
            )
            return retry_cmd

        tracks = await search_youtube_innertube(query)
        if not tracks:
            tracks = get_emergency_playlist()

        await _save_session(ApiPhone, State.PLAYING_TRACKS.value, tracks, 0)
        return await _play_command_or_error(tracks[0]["id"], request, ApiPhone)

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

    elif state == State.PLAYING_TRACKS.value:
        if not playlist:
            playlist = get_emergency_playlist()
            index = 0

        total = len(playlist)
        index = (index % total) if total > 0 else 0

        if ValName == "2":
            index = (index - 1) % total
        elif ValName == "3":
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
            return make_ivr_read_command("נוסף למועדפים • ממשיך...", "1", "1", 3, "digits")
        elif ValName == "0":
            await _save_session(ApiPhone, State.MAIN_MENU.value, [], 0)
            return make_ivr_read_command("חוזר לתפריט הראשי", "1", "1", 3, "digits")
        else:
            index = (index + 1) % total

        await _save_session(ApiPhone, State.PLAYING_TRACKS.value, playlist, index)
        return await _play_command_or_error(playlist[index]["id"], request, ApiPhone)

    logger.warning("Unknown session state %r for phone=%s — resetting", state, ApiPhone)
    await _save_session(ApiPhone, State.MAIN_MENU.value, [], 0)
    return make_ivr_read_command(MAIN_MENU_TEXT, "1", "1", 10, "digits")


# ==========================================
# ❤️ Debug + Health
# ==========================================
@app.get("/debug/search")
async def debug_search(q: str = Query(...), token: str = Query(None), verbose: int = Query(0)):
    """כלי אבחון מהיר: בודק מה מנוע החיפוש בפועל מחזיר בלי לחכות לשיחת טלפון.
    מנוטרל לגמרי (404) אם IVR_DEBUG_TOKEN לא הוגדר.
    verbose=1 בודק כל שכבה בנפרד (מועיל לאבחן איזו שכבה בדיוק נכשלת)."""
    if not DEBUG_TOKEN:
        raise HTTPException(404)
    if token != DEBUG_TOKEN:
        raise HTTPException(403, "Invalid token")

    if verbose:
        result = {}
        if YOUTUBE_DATA_API_ENABLED:
            t = await search_youtube_data_api(q)
            result["data_api_v3"] = {"count": len(t), "sample": t[:2]}
        else:
            result["data_api_v3"] = "disabled (no YOUTUBE_DATA_API_KEY)"
        result["ytdlp_available"] = YTDLP_AVAILABLE
        if YTDLP_AVAILABLE:
            t = await search_youtube_ytdlp(q)
            result["ytdlp"] = {"count": len(t), "sample": t[:2]}
        t = await _search_via_innertube_scrape(q, False)
        result["innertube_scrape_legacy"] = {"count": len(t), "sample": t[:2]}
        t = await search_invidious_fallback(q)
        result["invidious_legacy"] = {"count": len(t), "sample": t[:2]}
        t = await search_piped_fallback(q)
        result["piped_legacy"] = {"count": len(t), "sample": t[:2]}
        return result

    tracks = await search_youtube_innertube(q)
    return {"query": q, "count": len(tracks), "tracks": tracks}


@app.get("/debug/ytdlp")
async def debug_ytdlp(
    token: str = Query(None),
    video_id: str = Query(None, description="11-char YouTube video id to test-download"),
):
    """בודק את שרשרת yt-dlp החדשה בבידוד: הורדת אודיו + המרה ל-WAV טלפוני,
    בלי לחכות לשיחת טלפון ובלי להעלות לימות. מנוטרל (404) בלי IVR_DEBUG_TOKEN."""
    if not DEBUG_TOKEN:
        raise HTTPException(404)
    if token != DEBUG_TOKEN:
        raise HTTPException(403, "Invalid token")

    report: dict = {"ytdlp_available": YTDLP_AVAILABLE, "ffmpeg_available": FFMPEG_BIN is not None}
    if not YTDLP_AVAILABLE:
        report["conclusion"] = "yt-dlp אינו מותקן — הוסיפו 'yt-dlp' ל-requirements.txt ועשו deploy מחדש."
        return report

    if not video_id:
        report["note"] = "ספקו target ?video_id=XXXXXXXXXXX (11 תווים) כדי לבדוק הורדה אמיתית."
        return report

    if not VIDEO_ID_RE.match(video_id):
        raise HTTPException(400, "Invalid video ID")

    t0 = time.monotonic()
    raw = await download_audio_via_ytdlp(video_id)
    report["download"] = {
        "ok": raw is not None,
        "bytes": len(raw) if raw else 0,
        "seconds": round(time.monotonic() - t0, 2),
    }
    if not raw:
        report["conclusion"] = (
            "ההורדה נכשלה. בדקו: (1) שהחבילה yt-dlp מעודכנת (pip install -U yt-dlp — "
            "יוטיוב משנה הגנות לעיתים קרובות), (2) שה-IP של Render לא חסום ע\"י יוטיוב "
            "(שקלו YTDLP_PROXY), (3) לוגים מפורטים מעל להודעה הזו."
        )
        return report

    t1 = time.monotonic()
    converted = await convert_to_telephony_wav(raw)
    report["conversion"] = {
        "ok": converted is not None,
        "bytes": len(converted) if converted else 0,
        "seconds": round(time.monotonic() - t1, 2),
    }
    report["conclusion"] = "✅ הורדה + המרה הצליחו — הבעיה (אם עדיין קיימת) היא בשכבת ה-Yemot upload, לא כאן."
    return report


@app.get("/debug/yemot")
async def debug_yemot(token: str = Query(None), target_ext: str = Query(None)):
    """כלי אבחון לימות המשיח: Login + ניסיון UpdateExtension/UploadFile אמיתי
    עם קובץ דמה קטן, על הנתיב שכבר הוכח כעובד ('ivr2:/<שלוחה>/<קובץ>.wav')
    וגם על כמה חלופות, למקרה שהחשבון שלכם מוגדר אחרת. מנוטרל (404) בלי טוקן."""
    if not DEBUG_TOKEN:
        raise HTTPException(404)
    if token != DEBUG_TOKEN:
        raise HTTPException(403, "Invalid token")
    if not YEMOT_ENABLED:
        raise HTTPException(400, "YEMOT_SYSTEM_NUMBER/YEMOT_PASSWORD not configured")

    assert http_client is not None
    report: dict = {}

    yemot_token = await _yemot_login(force=True)
    report["login"] = {"ok": bool(yemot_token), "token_preview": (yemot_token[:6] + "...") if yemot_token else None}
    if not yemot_token:
        report["conclusion"] = "Login עצמו נכשל — בדקו YEMOT_SYSTEM_NUMBER/YEMOT_PASSWORD או YEMOT_API_KEY"
        return report

    ext_to_test = (target_ext or _bare_yemot_extension()).strip().strip("/")
    folder_path = f"ivr2:/{ext_to_test}"

    resp = await http_client.post(
        f"{YEMOT_API_BASE}/UpdateExtension",
        data={"token": yemot_token, "path": folder_path, "type": "playfile"},
        timeout=10.0,
    )
    report["update_extension"] = {"path_tried": folder_path, "raw_response": resp.json()}

    dummy_wav = b"RIFF" + b"\x00" * 200  # לא WAV תקין אמיתי, רק לבדוק שה-endpoint מקבל את הבקשה
    debug_file_num = await _next_yemot_file_number()
    upload_path = f"{folder_path}/{debug_file_num}.wav"
    resp2 = await http_client.post(
        f"{YEMOT_API_BASE}/UploadFile",
        data={"token": yemot_token, "path": upload_path},
        files={"file": (f"{debug_file_num}.wav", dummy_wav, "audio/wav")},
        timeout=15.0,
    )
    data2 = resp2.json()
    report["upload_attempt"] = {"path_tried": upload_path, "raw_response": data2}

    if data2.get("responseStatus") == "OK":
        await _yemot_delete_file(data2.get("path") or upload_path)
        report["conclusion"] = f"✅ העלאה לימות עובדת עם הנתיב {upload_path!r}. YEMOT_UPLOAD_FOLDER תקין."
    else:
        report["conclusion"] = (
            "❌ ההעלאה נכשלה על הנתיב הזה. ודאו שיצרתם ידנית שלוחה בשם הזה בפאנל הניהול "
            "של ימות, או נסו target_ext אחר. שימו לב: אם /debug/ytdlp כן עובד, הבעיה "
            "ממוקדת בצד ימות ולא בהורדת האודיו."
        )

    return report


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
        "youtube_data_api_enabled": YOUTUBE_DATA_API_ENABLED,
        "ytdlp_available": YTDLP_AVAILABLE,
        "ffmpeg_available": FFMPEG_BIN is not None,
        "yemot_upload_enabled": YEMOT_ENABLED,
        "yemot_auto_delete_after_play": YEMOT_AUTO_DELETE_AFTER_PLAY if YEMOT_ENABLED else None,
        "free_stt_enabled": STT_ENABLED,
        "time": utcnow().isoformat(),
    }
