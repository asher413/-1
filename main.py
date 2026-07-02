import os
import json
import re
import httpx
import asyncio
import sqlite3
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, List
from fastapi import FastAPI, Query, Request
from fastapi.responses import PlainTextResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from cachetools import TTLCache

# ==========================================
# 📋 הגדרת לוגר ייצור ממוקד
# ==========================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("IVR_Production_Engine")

app = FastAPI(title="Bulletproof IVR YouTube Engine 2026")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

RAPIDAPI_KEY = os.environ.get("RAPIDAPI_KEY", "b356e0c424msh95c209990ea7472p1fe240jsn2a029b5480bf")
RAPIDAPI_HOST = "youtube-mp3-audio-video-downloader.p.rapidapi.com"
DB_PATH = "ivr_production.db"

search_cache = TTLCache(maxsize=1000, ttl=900)
stream_url_cache = TTLCache(maxsize=500, ttl=600)

# פלייליסט חירום קשיח - מבטיח שלעולם לא נחזיר 0 תוצאות ב-IVR
EMERGENCY_PLAYLIST = [
    {"id": "4NzIOLEeJZM", "title": "נחמן פילמר שמחה פורצת גבולות 15", "duration": "1:26:07", "author": "נחמן פילמר"},
    {"id": "WSMFtm3ZqcY", "title": "סט להיטים דתי חרדי קיץ פול ווליום", "duration": "1:26:18", "author": "פול ווליום"},
    {"id": "3QDfxHZaUik", "title": "סט להיטים דתי מקפיץ בטירוף רמיקסים", "duration": "1:45:25", "author": "מדע והשכל"},
    {"id": "kP1jrKkSZfE", "title": "שמחת היום 1 סט חסידי קצבי אש", "duration": "2:20:53", "author": "פול ווליום"}
]

# ==========================================
# 🩺 נתיב הבריאות עבור Render
# ==========================================
@app.get("/")
async def render_health_check():
    return {"status": "healthy", "engine": "IVR Fallback Core 2026"}

# ==========================================
# 💾 בסיס נתונים קבוע (SQLite)
# ==========================================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            phone TEXT PRIMARY KEY,
            authorized INTEGER DEFAULT 0,
            access_code TEXT DEFAULT '1234'
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            phone TEXT PRIMARY KEY,
            state TEXT,
            playlist_json TEXT,
            current_index INTEGER DEFAULT 0,
            last_active TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS favorites (
            phone TEXT,
            video_id TEXT,
            title TEXT,
            PRIMARY KEY(phone, video_id)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS rate_limits (
            phone TEXT,
            timestamp TEXT
        )
    """)
    default_whitelist = ["0534133753", "0534133754"]
    for ph in default_whitelist:
        cursor.execute("INSERT OR IGNORE INTO users (phone, authorized) VALUES (?, 1)", (ph,))
    conn.commit()
    conn.close()

init_db()

async def run_db_query(query: str, params: tuple = (), fetchall=False, commit=False):
    def _execute():
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(query, params)
        res = cursor.fetchall() if fetchall else cursor.fetchone()
        if commit:
            conn.commit()
        conn.close()
        return res
    return await asyncio.get_running_loop().run_in_executor(None, _execute)

async def is_rate_limited(phone: str) -> bool:
    now = datetime.utcnow()
    one_minute_ago = (now - timedelta(minutes=1)).isoformat()
    await run_db_query("DELETE FROM rate_limits WHERE timestamp < ?", (one_minute_ago,), commit=True)
    recent_requests = await run_db_query("SELECT COUNT(*) FROM rate_limits WHERE phone = ? AND timestamp > ?", (phone, one_minute_ago), fetchall=False)
    if recent_requests and recent_requests[0] >= 20:
        return True
    await run_db_query("INSERT INTO rate_limits (phone, timestamp) VALUES (?, ?)", (phone, now.isoformat()), commit=True)
    return False

def recursive_find_video_renderers(data) -> list:
    renderers = []
    if isinstance(data, dict):
        if "videoRenderer" in data:
            renderers.append(data["videoRenderer"])
        for value in data.values():
            renderers.extend(recursive_find_video_renderers(value))
    elif isinstance(data, list):
        for item in data:
            renderers.extend(recursive_find_video_renderers(item))
    return renderers

# ==========================================
# 🔄 מנוע הגיבוי המבוזר: Invidious API
# ==========================================
async def search_invidious_fallback(query: str) -> List[dict]:
    logger.info(f"⚡ Launching Invidious Fallback Engine for: '{query}'")
    instances = [
        "https://inv.tux.digital",
        "https://invidious.nerdvpn.de",
        "https://vid.puffyan.us",
        "https://invidious.projectsegfau.lt"
    ]
    async with httpx.AsyncClient() as client:
        for inst in instances:
            try:
                url = f"{inst}/api/v1/search"
                response = await client.get(url, params={"q": query, "type": "video"}, timeout=4.0)
                if response.status_code == 200:
                    data = response.json()
                    tracks = []
                    for item in data:
                        if item.get("type") == "video" and item.get("videoId"):
                            tracks.append({
                                "id": item["videoId"],
                                "title": item.get("title", "שיר ללא שם"),
                                "duration": str(item.get("lengthSeconds", "00:00")),
                                "author": item.get("author", "אמן לא ידוע")
                            })
                        if len(tracks) >= 15:
                            break
                    if tracks:
                        logger.info(f"✅ Invidious Fallback Hit! Successfully parsed {len(tracks)} tracks via {inst}")
                        return tracks
            except Exception as e:
                logger.warning(f"Invidious instance {inst} failed or timed out: {e}")
                continue
    return []

# ==========================================
# 🔍 מנוע חיפוש משולב (InnerTube + Invidious)
# ==========================================
async def search_youtube_innertube(query: str, filter_newest: bool = False) -> List[dict]:
    if query in search_cache and not filter_newest:
        return search_cache[query]

    url = "https://www.youtube.com/youtubei/v1/search"
    payload = {
        "context": {"client": {"clientName": "WEB", "clientVersion": "2.20260301.00.00", "hl": "he", "gl": "IL"}},
        "query": query
    }
    if filter_newest:
        payload["params"] = "EgQIARAB"

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Content-Type": "application/json",
        "Origin": "https://www.youtube.com"
    }
    
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(url, json=payload, headers=headers, timeout=4.5)
            if response.status_code == 200:
                raw_data = response.json()
                
                # טקטיקת ה-Debug שלך: הדפסת תחילת ה-JSON לזיהוי חסימות IP
                logger.info("🔍 RAW RESPONSE SNIPPET (First 800 chars): %s", json.dumps(raw_data)[:800])
                
                video_nodes = recursive_find_video_renderers(raw_data)
                
                if video_nodes:
                    tracks = []
                    for vr in video_nodes:
                        video_id = vr.get("videoId")
                        if not video_id:
                            continue
                        title_runs = vr.get("title", {}).get("runs", [{}])
                        title = title_runs[0].get("text", "שיר ללא שם") if title_runs else "שיר ללא שם"
                        tracks.append({
                            "id": video_id,
                            "title": title,
                            "duration": vr.get("lengthText", {}).get("simpleText", "00:00"),
                            "author": vr.get("longBylineText", {}).get("runs", [{}])[0].get("text", "אמן")
                        })
                        if len(tracks) >= 15:
                            break
                    if tracks:
                        if not filter_newest:
                            search_cache[query] = tracks
                        return tracks
        except Exception as e:
            logger.error(f"InnerTube core request exploded: {e}")

    # 🚨 שכבת הגנה 2: אם הגענו לכאן, השרת נחסם או חזר עם 0 תוצאות. נעבור מיד לפרוקסי מבוזר!
    fallback_tracks = await search_invidious_fallback(query)
    if fallback_tracks:
        return fallback_tracks

    # 🚨 שכבת הגנה 3: קטסטרופה מוחלטת - הכל חסום, נחזיר את פלייליסט החירום
    logger.critical("🚨 ALL SEARCH ENGINES FAILED/BLOCKED. Engaging Emergency Playlist to prevent IVR crash.")
    return EMERGENCY_PLAYLIST

# ==========================================
# 🎵 מזרים מדיה אסינכרוני (Streaming Proxy)
# ==========================================
async def fetch_cobalt_link(video_id: str, client: httpx.AsyncClient) -> Optional[str]:
    instances = ["https://api.cobalt.tools/api/json", "https://cobalt.api.v0.wtf/api/json"]
    for inst in instances:
        try:
            res = await client.post(inst, json={"url": f"https://www.youtube.com/watch?v={video_id}", "downloadMode": "audio", "audioFormat": "mp3"}, headers={"Accept": "application/json"}, timeout=3.5)
            if res.status_code == 200:
                return res.json().get("url")
        except Exception:
            continue
    return None

async def fetch_rapidapi_link(video_id: str, client: httpx.AsyncClient) -> Optional[str]:
    try:
        res = await client.get(f"https://{RAPIDAPI_HOST}/get_mp3_download_link/{video_id}", headers={"x-rapidapi-key": RAPIDAPI_KEY, "x-rapidapi-host": RAPIDAPI_HOST}, timeout=4.0)
        if res.status_code == 200:
            js = res.json()
            return js.get("file") or js.get("link") or js.get("url")
    except Exception:
        pass
    return None

@app.get("/stream/{video_id}.mp3")
async def proxy_mp3_stream(video_id: str):
    logger.info(f"🎵 STREAM REQUEST - Extracting Audio for Video ID: {video_id}")
    if video_id in stream_url_cache:
        target_url = stream_url_cache[video_id]
    else:
        async with httpx.AsyncClient() as client:
            target_url = await fetch_cobalt_link(video_id, client) or await fetch_rapidapi_link(video_id, client)
            if not target_url:
                target_url = f"https://invidious.projectsegfau.lt/latest_version?id={video_id}&itag=140"
            stream_url_cache[video_id] = target_url

    async def chunk_generator():
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
                async with client.stream("GET", target_url, headers={"User-Agent": "Mozilla/5.0"}) as response:
                    async_iterator = response.aiter_bytes(chunk_size=64 * 1024)
                    while True:
                        try:
                            chunk = await asyncio.wait_for(async_iterator.__anext__(), timeout=12.0)
                            yield chunk
                        except StopAsyncIteration:
                            break
        except Exception as e:
            logger.error(f"Stream interrupted: {e}")

    return StreamingResponse(chunk_generator(), media_type="audio/mpeg")

# ==========================================
# 🧼 סינטקס IVR נקי
# ==========================================
def clean_text_for_ivr(text: str) -> str:
    cleaned = re.sub(r'[^a-zA-Z0-9\sא-ת]', ' ', text)
    return re.sub(r'\s+', ' ', cleaned).strip()

def make_ivr_read_command(text: str, min_dig: str, max_dig: str, sec: int, mode: str) -> str:
    clean = clean_text_for_ivr(text)
    if mode.lower() == "voice":
        return f"read=t-{clean}=ValName,no,50,1,{sec},voice,no"
    return f"read=t-{clean}=ValName,no,{max_dig},{min_dig},{sec},{mode.lower()},no"

def get_final_play_command(video_id: str, request: Request) -> str:
    host = (request.headers.get("x-forwarded-host") or request.headers.get("host", "")).split(":")[0]
    protocol = request.headers.get("x-forwarded-proto") or ("https" if "localhost" not in host else "http")
    port = ":10000" if "localhost" in host else ""
    return f"read={protocol}://{host}{port}/stream/{video_id}.mp3=ValName,no,1,0,2,digits,no"

async def active_session_cleanup():
    while True:
        try:
            await run_db_query("DELETE FROM sessions WHERE last_active < ?", ((datetime.utcnow() - timedelta(hours=2)).isoformat(),), commit=True)
        except Exception:
            pass
        await asyncio.sleep(1800)

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(active_session_cleanup())

# ==========================================
# 🎛️ הליבה המרכזית: ניהול פרוטוקול ה-IVR
# ==========================================
@app.get("/youtube", response_class=PlainTextResponse)
async def handle_ivr(request: Request, ApiPhone: str = Query(None), hangup: str = Query(None)):
    if hangup == "yes" or not ApiPhone:
        return "OK"

    val_params = [v for k, v in request.query_params.multi_items() if k == "ValName"]
    ValName = val_params[-1] if val_params else None

    logger.info(f"📞 INCOMING REQUEST - Phone: {ApiPhone} | Extracted ValName: {ValName}")

    if await is_rate_limited(ApiPhone):
        cmd = make_ivr_read_command("בוצעו יותר מדי פעולות בדקה אנא המתן מעט", "1", "1", 5, "digits")
        logger.info("IVR RESPONSE SENT: %s", cmd)
        return cmd

    user_data = await run_db_query("SELECT authorized FROM users WHERE phone = ?", (ApiPhone,))
    is_whitelisted = user_data and user_data[0] == 1
    
    session_data = await run_db_query("SELECT state, playlist_json, current_index FROM sessions WHERE phone = ?", (ApiPhone,))
    
    if not session_data:
        state = "MAIN_MENU" if is_whitelisted else "CHECK_AUTH"
        playlist, index = [], 0
        await run_db_query("INSERT INTO sessions (phone, state, playlist_json, current_index, last_active) VALUES (?, ?, ?, ?, ?)", (ApiPhone, state, "[]", 0, datetime.utcnow().isoformat()), commit=True)
    else:
        state, playlist_json, index = session_data
        playlist = json.loads(playlist_json)

    logger.info(f"🚦 STATE ENGINE - Active State: {state} | Tracks Loaded: {len(playlist)}")
    await run_db_query("UPDATE sessions SET last_active = ? WHERE phone = ?", (datetime.utcnow().isoformat(), ApiPhone), commit=True)

    if not is_whitelisted and state == "CHECK_AUTH":
        if ValName == "1234":
            await run_db_query("INSERT OR REPLACE INTO users (phone, authorized) VALUES (?, 1)", (ApiPhone,), commit=True)
            state = "MAIN_MENU"
            ValName = None
        else:
            cmd = make_ivr_read_command("קוד שגוי אנא נסה שנית" if ValName else "אנא הקש את קוד הגישה", "4", "4", 10, "digits")
            logger.info("IVR RESPONSE SENT: %s", cmd)
            return cmd

    if state == "MAIN_MENU":
        if ValName == "1":
            await run_db_query("UPDATE sessions SET state = 'WAITING_FOR_SEARCH' WHERE phone = ?", (ApiPhone,), commit=True)
            cmd = make_ivr_read_command("אנא אמרו את שם השיר המבוקש לאחר הצליל", "1", "50", 10, "voice")
            logger.info("IVR RESPONSE SENT: %s", cmd)
            return cmd
        elif ValName == "2":
            tracks = await search_youtube_innertube("שירים חסידיים חדשים", filter_newest=True)
            # עדכון בסיס הנתונים לסטטוס ניגון ומניעת איבוד המצב (התגברות על הבאג המשני)
            await run_db_query("UPDATE sessions SET state = 'PLAYING_TRACKS', playlist_json = ?, current_index = 0 WHERE phone = ?", (json.dumps(tracks), ApiPhone), commit=True)
            cmd = get_final_play_command(tracks[0]["id"], request)
            logger.info("IVR RESPONSE SENT: %s", cmd)
            return cmd
        elif ValName == "3":
            favs = await run_db_query("SELECT video_id, title FROM favorites WHERE phone = ?", (ApiPhone,), fetchall=True)
            if not favs:
                cmd = make_ivr_read_command("רשימת המועדפים ריקה חוזר לתפריט", "1", "1", 4, "digits")
                logger.info("IVR RESPONSE SENT: %s", cmd)
                return cmd
            tracks = [{"id": f[0], "title": f[1], "duration": "00:00", "author": ""} for f in favs]
            await run_db_query("UPDATE sessions SET state = 'PLAYING_TRACKS', playlist_json = ?, current_index = 0 WHERE phone = ?", (json.dumps(tracks), ApiPhone), commit=True)
            cmd = get_final_play_command(tracks[0]["id"], request)
            logger.info("IVR RESPONSE SENT: %s", cmd)
            return cmd
        else:
            cmd = make_ivr_read_command("לחיפוש קולי הקש 1 לשירים חדשים הקש 2 למועדפים הקש 3", "1", "1", 10, "digits")
            logger.info("IVR RESPONSE SENT: %s", cmd)
            return cmd

    elif state == "WAITING_FOR_SEARCH":
        if not ValName or ValName in ["1", "2", "*", "#"]:
            cmd = make_ivr_read_command("לא קלטתי אנא אמרו את שם השיר בבירור", "1", "50", 10, "voice")
            logger.info("IVR RESPONSE SENT: %s", cmd)
            return cmd
        
        tracks = await search_youtube_innertube(ValName, filter_newest=False)
        await run_db_query("UPDATE sessions SET state = 'PLAYING_TRACKS', playlist_json = ?, current_index = 0 WHERE phone = ?", (json.dumps(tracks), ApiPhone), commit=True)
        cmd = get_final_play_command(tracks[0]["id"], request)
        logger.info("IVR RESPONSE SENT: %s", cmd)
        return cmd

    elif state == "PLAYING_TRACKS":
        if ValName == "1" or ValName == "": 
            index += 1
        elif ValName == "2": 
            index -= 1
        elif ValName == "3": 
            cmd = make_ivr_read_command("השמעה מושהית להמשך הקש 4 לחזרה לתפריט הקש 0", "1", "1", 20, "digits")
            logger.info("IVR RESPONSE SENT: %s", cmd)
            return cmd
        elif ValName == "5": 
            import random
            random.shuffle(playlist)
            index = 0
            await run_db_query("UPDATE sessions SET playlist_json = ? WHERE phone = ?", (json.dumps(playlist), ApiPhone), commit=True)
        elif ValName == "6": 
            curr_track = playlist[index % len(playlist)]
            await run_db_query("INSERT OR IGNORE INTO favorites (phone, video_id, title) VALUES (?, ?, ?)", (ApiPhone, curr_track["id"], curr_track["title"]), commit=True)
            cmd = make_ivr_read_command("השיר נוסף למועדפים שלך ממשיך בנגינה", "1", "1", 3, "digits")
            logger.info("IVR RESPONSE SENT: %s", cmd)
            return cmd
        elif ValName == "0":
            await run_db_query("UPDATE sessions SET state = 'MAIN_MENU', playlist_json = '[]', current_index = 0 WHERE phone = ?", (ApiPhone,), commit=True)
            cmd = make_ivr_read_command("חוזר לתפריט הראשי", "1", "1", 3, "digits")
            logger.info("IVR RESPONSE SENT: %s", cmd)
            return cmd

        index = index % len(playlist)
        await run_db_query("UPDATE sessions SET current_index = ? WHERE phone = ?", (index, ApiPhone), commit=True)
        cmd = get_final_play_command(playlist[index]["id"], request)
        logger.info("IVR RESPONSE SENT: %s", cmd)
        return cmd

    return "hangup"
