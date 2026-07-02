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
# ⚙️ קונפיגורציה — הכל מ-ENV, שום סוד קשיח
# ==========================================
RAPIDAPI_KEY = os.environ.get("RAPIDAPI_KEY", "")  # ריק => המסלול הזה ידולג בשקט
RAPIDAPI_HOST = os.environ.get(
    "RAPIDAPI_HOST", "youtube-mp3-audio-video-downloader.p.rapidapi.com"
)
DB_PATH = os.environ.get("IVR_DB_PATH", "ivr_production.db")

_whitelist_env = os.environ.get("IVR_WHITELIST_PHONES", "")
DEFAULT_WHITELIST = [p.strip() for p in _whitelist_env.split(",") if p.strip()]

# קוד גישה קבוע (לא מתחלף בכל ריסטארט!) — חובה להגדיר ב-ENV בפרודקשן.
# אם לא הוגדר, מייצרים קוד רנדומלי חד-פעמי ומדפיסים ללוג כדי שלא "ייעלם" בלי תיעוד.
DEFAULT_ACCESS_CODE = os.environ.get("IVR_DEFAULT_ACCESS_CODE")
if not DEFAULT_ACCESS_CODE:
    DEFAULT_ACCESS_CODE = secrets.token_hex(2)
    logger.warning(
        "IVR_DEFAULT_ACCESS_CODE not set! Generated a random one-time code: %s "
        "— set this env var explicitly in production so it doesn't change on restart.",
        DEFAULT_ACCESS_CODE,
    )

if not DEFAULT_WHITELIST:
    logger.warning(
        "IVR_WHITELIST_PHONES not set — no phone numbers are pre-authorized. "
        "Set it via env var, comma separated, e.g. '0534133753,0534133754'."
    )

# בסיס URL ציבורי אופציונלי — אם מוגדר, נשתמש בו במקום לבנות URL מה-headers
# (headers כמו Host/X-Forwarded-Host יכולים להיות מזויפים ע"י מי ששולח את הבקשה).
PUBLIC_BASE_URL = os.environ.get("IVR_PUBLIC_BASE_URL", "").rstrip("/")

RATE_LIMIT_PER_MINUTE = int(os.environ.get("IVR_RATE_LIMIT_PER_MINUTE", "20"))
SESSION_TTL_HOURS = int(os.environ.get("IVR_SESSION_TTL_HOURS", "4"))
MAX_PLAYLIST_SIZE = int(os.environ.get("IVR_MAX_PLAYLIST_SIZE", "15"))
SEARCH_RECURSION_DEPTH_LIMIT = 40

PHONE_RE = re.compile(r"^\d{9,15}$")
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

search_cache: TTLCache = TTLCache(maxsize=1000, ttl=900)
stream_url_cache: TTLCache = TTLCache(maxsize=500, ttl=600)

# מנעולים פר-טלפון כדי למנוע מרוץ מצבים בין שתי בקשות מקבילות לאותו מספר.
_phone_locks: dict[str, asyncio.Lock] = {}


def get_phone_lock(phone: str) -> asyncio.Lock:
    lock = _phone_locks.get(phone)
    if lock is None:
        lock = asyncio.Lock()
        _phone_locks[phone] = lock
    return lock


# ==========================================
# 🎵 פלייליסט חירום — תמיד עותק עמוק, אף פעם לא האובייקט הגלובלי עצמו
# ==========================================
_EMERGENCY_PLAYLIST_SOURCE = [
    {"id": "4NzIOLEeJZM", "title": "נחמן פילמר שמחה פורצת גבולות 15", "duration": "1:26:07", "author": "נחמן פילמר"},
    {"id": "WSMFtm3ZqcY", "title": "סט להיטים דתי חרדי קיץ פול ווליום", "duration": "1:26:18", "author": "פול ווליום"},
    {"id": "3QDfxHZaUik", "title": "סט להיטים דתי מקפיץ בטירוף רמיקסים", "duration": "1:45:25", "author": "מדע והשכל"},
    {"id": "kP1jrKkSZfE", "title": "שמחת היום 1 סט חסידי קצבי אש", "duration": "2:20:53", "author": "פול ווליום"},
]


def get_emergency_playlist() -> List[dict]:
    """עותק עמוק תמיד — כי random.shuffle() ופעולות אחרות עלולות לשבש
    מצב גלובלי משותף בין כל המתקשרים אם נחזיר רפרנס לאותה רשימה."""
    return copy.deepcopy(_EMERGENCY_PLAYLIST_SOURCE)


# ==========================================
# 🌐 HTTP Client משותף (connection pooling אמיתי)
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
    cleanup_task = asyncio.create_task(active_session_cleanup())
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
        logger.info("🛑 IVR Engine stopped")


app = FastAPI(title="Bulletproof IVR YouTube Engine 2026", lifespan=lifespan)

# CORS: allow_origins=["*"] + allow_credentials=True זו קומבינציה לא חוקית/לא בטוחה.
# זה שרת IVR שלא נצרך מדפדפן עם קרדנצ'לס, אז משביתים credentials.
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
        # WAL משפר קונקורנטיות (קריאה תוך כדי כתיבה), busy_timeout מצמצם "database is locked".
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
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_rate_limits_phone_ts ON rate_limits(phone, timestamp)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_sessions_last_active ON sessions(last_active)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_favorites_phone ON favorites(phone)")

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


async def run_db_query(
    query: str, params: tuple = (), fetchall: bool = False, commit: bool = False
):
    def _execute():
        conn = sqlite3.connect(DB_PATH, timeout=5.0)
        try:
            conn.execute("PRAGMA busy_timeout=5000")
            cursor = conn.cursor()
            cursor.execute(query, params)
            result = cursor.fetchall() if fetchall else cursor.fetchone()
            if commit:
                conn.commit()
            return result
        except Exception:
            if commit:
                conn.rollback()
            raise
        finally:
            conn.close()

    try:
        return await asyncio.get_running_loop().run_in_executor(None, _execute)
    except Exception as e:
        logger.error("DB query failed: %s | query=%s", e, query)
        return [] if fetchall else None


# ==========================================
# 🚦 Rate Limiting
# ==========================================
async def is_rate_limited(phone: str) -> bool:
    if not phone or not PHONE_RE.match(phone):
        return True
    try:
        now = utcnow()
        one_minute_ago = (now - timedelta(minutes=1)).isoformat()
        await run_db_query("DELETE FROM rate_limits WHERE timestamp < ?", (one_minute_ago,), commit=True)

        count_row = await run_db_query(
            "SELECT COUNT(*) FROM rate_limits WHERE phone = ? AND timestamp > ?",
            (phone, one_minute_ago),
        )
        count = count_row[0] if count_row else 0
        if count >= RATE_LIMIT_PER_MINUTE:
            return True

        await run_db_query(
            "INSERT INTO rate_limits (phone, timestamp) VALUES (?, ?)",
            (phone, now.isoformat()),
            commit=True,
        )
        return False
    except Exception as e:
        logger.error("Rate limit check failed: %s", e)
        return True  # בספק — עדיף לחסום מאשר לפתוח פרצה


# ==========================================
# 🔎 חיפוש — InnerTube + Invidious fallback
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
            return  # הגנת עומק/גודל — לא נתלה על JSON ענק/עוין

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


async def search_invidious_fallback(query: str) -> List[dict]:
    instances = [
        "https://invidious.projectsegfau.lt",
        "https://vid.puffyan.us",
        "https://invidious.nerdvpn.de",
        "https://inv.tux.digital",
    ]
    assert http_client is not None
    for inst in instances:
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


async def search_youtube_innertube(query: str, filter_newest: bool = False) -> List[dict]:
    query = (query or "").strip()[:150]
    if not query:
        return get_emergency_playlist()

    cache_key = f"{'newest:' if filter_newest else ''}{query}"
    if cache_key in search_cache:
        return search_cache[cache_key]

    url = "https://www.youtube.com/youtubei/v1/search"
    payload = {
        "context": {
            "client": {
                "clientName": "WEB",
                "clientVersion": "2.20260601.01.00",
                "hl": "he",
                "gl": "IL",
            }
        },
        "query": query,
    }
    if filter_newest:
        payload["params"] = "EgQIARAB"  # מיון לפי תאריך העלאה

    headers = {
        "Content-Type": "application/json",
        "Origin": "https://www.youtube.com",
        "Referer": "https://www.youtube.com/",
    }

    tracks: List[dict] = []
    try:
        assert http_client is not None
        resp = await http_client.post(url, json=payload, headers=headers, timeout=7.0)
        logger.info("InnerTube status: %s for query: %s", resp.status_code, query)
        if resp.status_code == 200:
            raw_data = resp.json()
            tracks = extract_tracks_from_innertube(raw_data)
            if tracks:
                logger.info("✅ InnerTube parsed successfully: %d tracks", len(tracks))
                search_cache[cache_key] = tracks
                return tracks
            logger.warning("InnerTube returned 200 but no tracks parsed for query=%r", query)
    except (httpx.HTTPError, asyncio.TimeoutError, json.JSONDecodeError) as e:
        logger.error("InnerTube request failed: %s", e)

    logger.info("InnerTube parsing failed → trying Invidious fallback")
    fallback_tracks = await search_invidious_fallback(query)
    if fallback_tracks:
        return fallback_tracks

    logger.warning("All search backends failed → Emergency playlist")
    return get_emergency_playlist()


# ==========================================
# 🎧 Streaming — עם pre-flight validation לפני שמתחייבים ללקוח
# ==========================================
async def _candidate_stream_urls(video_id: str) -> List[str]:
    """מחזיר רשימת URL-ים מועמדים לפי סדר עדיפות, בלי לבצע עדיין בקשה בפועל."""
    candidates: List[str] = []
    watch_url = f"https://www.youtube.com/watch?v={video_id}"

    # Cobalt (רק את ה-URL, את הבקשה בפועל נעשה בעת ה-preflight)
    for inst in ["https://api.cobalt.tools/api/json", "https://cobalt.api.v0.wtf/api/json"]:
        candidates.append(f"cobalt::{inst}::{watch_url}")

    if RAPIDAPI_KEY:
        candidates.append(f"rapidapi::{video_id}")

    candidates.append(f"invidious::https://invidious.projectsegfau.lt/latest_version?id={video_id}&itag=140")
    return candidates


async def _resolve_candidate(candidate: str) -> Optional[str]:
    """ממיר מועמד ל-URL אמיתי של קובץ מדיה (מבצע בקשת רשת אם צריך)."""
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


async def fetch_stream_url(video_id: str) -> Optional[str]:
    """שומר תאימות לאחור: מחזיר את ה-URL הראשון שמצליח להיפתר (ללא preflight מלא)."""
    if video_id in stream_url_cache:
        return stream_url_cache[video_id]

    for candidate in await _candidate_stream_urls(video_id):
        resolved = await _resolve_candidate(candidate)
        if resolved:
            stream_url_cache[video_id] = resolved
            return resolved
    return None


@app.get("/stream/{video_id}.mp3")
async def proxy_mp3_stream(video_id: str):
    if not VIDEO_ID_RE.match(video_id):
        raise HTTPException(400, "Invalid video ID")

    assert http_client is not None
    candidates = await _candidate_stream_urls(video_id)

    # Pre-flight: לנסות כל מועמד עד שמוצאים אחד שבאמת מחזיר 200,
    # ורק אז להתחייב ללקוח עם StreamingResponse. כך לא "נתקע" על מקור מת.
    for candidate in candidates:
        target_url = await _resolve_candidate(candidate)
        if not target_url or not target_url.startswith("https://"):
            continue
        try:
            req = http_client.build_request("GET", target_url)
            resp = await http_client.send(req, stream=True)
        except (httpx.HTTPError, asyncio.TimeoutError) as e:
            logger.warning("Stream preflight failed for %s: %s", candidate[:40], e)
            continue

        if resp.status_code != 200:
            await resp.aclose()
            continue

        stream_url_cache[video_id] = target_url

        async def chunk_generator(response: httpx.Response):
            try:
                async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                    yield chunk
            except (httpx.HTTPError, asyncio.TimeoutError) as e:
                logger.error("Streaming error mid-stream for %s: %s", video_id, e)
            finally:
                await response.aclose()

        return StreamingResponse(chunk_generator(resp), media_type="audio/mpeg")

    logger.error("All stream sources exhausted for video_id=%s", video_id)
    raise HTTPException(502, "No available audio source for this track")


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


def get_final_play_command(video_id: str, request: Request) -> str:
    if PUBLIC_BASE_URL:
        base = PUBLIC_BASE_URL
    else:
        host = (request.headers.get("x-forwarded-host") or request.headers.get("host", "")).split(":")[0]
        protocol = request.headers.get("x-forwarded-proto") or ("http" if "localhost" in host else "https")
        port = ":10000" if "localhost" in host else ""
        base = f"{protocol}://{host}{port}"
    return f"read={base}/stream/{video_id}.mp3=ValName,no,1,0,2,digits,no"


# ==========================================
# 🧹 Background cleanup
# ==========================================
async def active_session_cleanup():
    while True:
        try:
            cutoff = (utcnow() - timedelta(hours=SESSION_TTL_HOURS)).isoformat()
            await run_db_query("DELETE FROM sessions WHERE last_active < ?", (cutoff,), commit=True)
            # גם ננקה מנעולים ישנים כדי לא לדלוף זיכרון עם הרבה מספרים לאורך זמן
            for ph in list(_phone_locks.keys()):
                lock = _phone_locks.get(ph)
                if lock and not lock.locked():
                    _phone_locks.pop(ph, None)
        except Exception as e:
            logger.error("Cleanup failed: %s", e)
        await asyncio.sleep(1800)


# ==========================================
# 🎛️ States
# ==========================================
class State(str, Enum):
    CHECK_AUTH = "CHECK_AUTH"
    MAIN_MENU = "MAIN_MENU"
    WAITING_FOR_SEARCH = "WAITING_FOR_SEARCH"
    PLAYING_TRACKS = "PLAYING_TRACKS"


ERROR_FALLBACK_CMD = None  # מוגדר בהמשך אחרי הפונקציה כדי להימנע מ-forward ref


def _generic_error_command() -> str:
    return make_ivr_read_command("משהו השתבש אנא נסו שוב מאוחר יותר", "1", "1", 5, "digits")


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

    ApiPhone = ApiPhone.strip()
    if not PHONE_RE.match(ApiPhone):
        logger.warning("Rejected malformed phone: %r", ApiPhone)
        return "OK"

    val_params = [v for k, v in request.query_params.multi_items() if k == "ValName"]
    ValName = (val_params[-1] if val_params else None)
    if ValName is not None:
        ValName = ValName.strip()[:150]

    logger.info("📞 Phone: %s | ValName: %r", ApiPhone, ValName)

    try:
        if await is_rate_limited(ApiPhone):
            return make_ivr_read_command("בוצעו יותר מדי פעולות אנא המתן מעט", "1", "1", 5, "digits")

        async with get_phone_lock(ApiPhone):
            return await _handle_ivr_locked(request, ApiPhone, ValName)
    except Exception as e:
        logger.exception("Unhandled error in IVR handler for phone=%s: %s", ApiPhone, e)
        return _generic_error_command()


async def _handle_ivr_locked(request: Request, ApiPhone: str, ValName: Optional[str]) -> str:
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
            await run_db_query(
                "INSERT OR REPLACE INTO users (phone, authorized, access_code) VALUES (?, 1, ?)",
                (ApiPhone, stored_access_code),
                commit=True,
            )
            await run_db_query(
                "UPDATE sessions SET state = ? WHERE phone = ?", (State.MAIN_MENU.value, ApiPhone), commit=True
            )
            state = State.MAIN_MENU.value
            ValName = None
        else:
            msg = "קוד שגוי אנא נסה שנית" if ValName else "אנא הקש את קוד הגישה"
            return make_ivr_read_command(msg, "4", "4", 10, "digits")

    # ---------- MAIN MENU ----------
    if state == State.MAIN_MENU.value:
        if ValName == "1":
            await run_db_query(
                "UPDATE sessions SET state = ? WHERE phone = ?", (State.WAITING_FOR_SEARCH.value, ApiPhone), commit=True
            )
            return make_ivr_read_command("אנא אמרו את שם השיר לאחר הצליל", "1", "50", 10, "voice")

        elif ValName == "2":
            tracks = await search_youtube_innertube("שירים חסידיים חדשים", filter_newest=True)
            if not tracks:
                tracks = get_emergency_playlist()
            await run_db_query(
                "UPDATE sessions SET state = ?, playlist_json = ?, current_index = 0 WHERE phone = ?",
                (State.PLAYING_TRACKS.value, json.dumps(tracks, ensure_ascii=False), ApiPhone),
                commit=True,
            )
            return get_final_play_command(tracks[0]["id"], request)

        elif ValName == "3":
            favs = await run_db_query(
                "SELECT video_id, title FROM favorites WHERE phone = ? ORDER BY created_at DESC",
                (ApiPhone,), fetchall=True,
            )
            if not favs:
                return make_ivr_read_command("רשימת המועדפים ריקה", "1", "1", 4, "digits")
            tracks = [{"id": f[0], "title": f[1], "duration": "00:00", "author": ""} for f in favs]
            await run_db_query(
                "UPDATE sessions SET state = ?, playlist_json = ?, current_index = 0 WHERE phone = ?",
                (State.PLAYING_TRACKS.value, json.dumps(tracks, ensure_ascii=False), ApiPhone),
                commit=True,
            )
            return get_final_play_command(tracks[0]["id"], request)

        else:
            return make_ivr_read_command("לחיפוש קולי 1 • שירים חדשים 2 • מועדפים 3", "1", "1", 10, "digits")

    # ---------- SEARCH ----------
    elif state == State.WAITING_FOR_SEARCH.value:
        if not ValName or len(ValName) < 2 or ValName in ("1", "2", "*", "#"):
            return make_ivr_read_command("לא קלטתי בבירור, אנא אמרו שוב", "1", "50", 10, "voice")

        tracks = await search_youtube_innertube(ValName)
        if not tracks:
            tracks = get_emergency_playlist()

        await run_db_query(
            "UPDATE sessions SET state = ?, playlist_json = ?, current_index = 0 WHERE phone = ?",
            (State.PLAYING_TRACKS.value, json.dumps(tracks, ensure_ascii=False), ApiPhone),
            commit=True,
        )
        return get_final_play_command(tracks[0]["id"], request)

    # ---------- PLAYING ----------
    elif state == State.PLAYING_TRACKS.value:
        if not playlist:
            playlist = get_emergency_playlist()
            await run_db_query(
                "UPDATE sessions SET playlist_json = ? WHERE phone = ?",
                (json.dumps(playlist, ensure_ascii=False), ApiPhone), commit=True,
            )

        total = len(playlist)
        index = (index % total) if total > 0 else 0

        if ValName == "2":
            index = (index - 1) % total
        elif ValName == "3":
            return make_ivr_read_command("הושהה • להמשך 4 • תפריט 0", "1", "1", 20, "digits")
        elif ValName == "5":
            random.shuffle(playlist)
            index = 0
            await run_db_query(
                "UPDATE sessions SET playlist_json = ? WHERE phone = ?",
                (json.dumps(playlist, ensure_ascii=False), ApiPhone), commit=True,
            )
        elif ValName == "6":
            curr = playlist[index]
            await run_db_query(
                "INSERT OR IGNORE INTO favorites (phone, video_id, title, created_at) VALUES (?, ?, ?, ?)",
                (ApiPhone, curr["id"], curr["title"], utcnow().isoformat()),
                commit=True,
            )
            return make_ivr_read_command("נוסף למועדפים • ממשיך...", "1", "1", 3, "digits")
        elif ValName == "0":
            await run_db_query(
                "UPDATE sessions SET state = ?, playlist_json = '[]', current_index = 0 WHERE phone = ?",
                (State.MAIN_MENU.value, ApiPhone), commit=True,
            )
            return make_ivr_read_command("חוזר לתפריט הראשי", "1", "1", 3, "digits")
        else:
            # ValName == "1" (הבא) או "" (ללא קלט / טיימאאוט) => מעבר לשיר הבא כברירת מחדל
            index = (index + 1) % total

        await run_db_query(
            "UPDATE sessions SET current_index = ? WHERE phone = ?", (index, ApiPhone), commit=True
        )
        return get_final_play_command(playlist[index]["id"], request)

    # מצב לא מוכר — נאפס בבטחה חזרה לתפריט במקום לתקוע את השיחה
    logger.warning("Unknown session state %r for phone=%s — resetting", state, ApiPhone)
    await run_db_query(
        "UPDATE sessions SET state = ?, playlist_json = '[]', current_index = 0 WHERE phone = ?",
        (State.MAIN_MENU.value, ApiPhone), commit=True,
    )
    return make_ivr_read_command("לחיפוש קולי 1 • שירים חדשים 2 • מועדפים 3", "1", "1", 10, "digits")


# ==========================================
# ❤️ Health check
# ==========================================
@app.get("/health")
async def health():
    db_ok = True
    try:
        await run_db_query("SELECT 1")
    except Exception:
        db_ok = False
    return {"status": "ok" if db_ok else "degraded", "db": db_ok, "time": utcnow().isoformat()}

