import os
import json
import re
import httpx
import asyncio
import sqlite3
import logging
import random
from datetime import datetime, timedelta
from typing import Optional, List

from fastapi import FastAPI, Query, Request, HTTPException
from fastapi.responses import PlainTextResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from cachetools import TTLCache

# ==========================================
# לוגר + FastAPI
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
stream_url_cache = TTLCache(maxsize=500, ttl=720)  # 12 דקות

EMERGENCY_PLAYLIST = [
    {"id": "4NzIOLEeJZM", "title": "נחמן פילמר שמחה פורצת גבולות 15", "duration": "1:26:07", "author": "נחמן פילמר"},
    {"id": "WSMFtm3ZqcY", "title": "סט להיטים דתי חרדי קיץ פול ווליום", "duration": "1:26:18", "author": "פול ווליום"},
    {"id": "3QDfxHZaUik", "title": "סט להיטים דתי מקפיץ בטירוף רמיקסים", "duration": "1:45:25", "author": "מדע והשכל"},
    {"id": "kP1jrKkSZfE", "title": "שמחת היום 1 סט חסידי קצבי אש", "duration": "2:20:53", "author": "פול ווליום"}
]

# ==========================================
# DB + Rate Limit
# ==========================================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    for table in [
        """CREATE TABLE IF NOT EXISTS users (phone TEXT PRIMARY KEY, authorized INTEGER DEFAULT 0, access_code TEXT DEFAULT '1234')""",
        """CREATE TABLE IF NOT EXISTS sessions (phone TEXT PRIMARY KEY, state TEXT, playlist_json TEXT, current_index INTEGER DEFAULT 0, last_active TEXT)""",
        """CREATE TABLE IF NOT EXISTS favorites (phone TEXT, video_id TEXT, title TEXT, PRIMARY KEY(phone, video_id))""",
        """CREATE TABLE IF NOT EXISTS rate_limits (phone TEXT, timestamp TEXT)"""
    ]:
        cursor.execute(table)
    
    for ph in ["0534133753", "0534133754"]:
        cursor.execute("INSERT OR IGNORE INTO users (phone, authorized) VALUES (?, 1)", (ph,))
    conn.commit()
    conn.close()

init_db()

async def run_db_query(query: str, params: tuple = (), fetchall: bool = False, commit: bool = False):
    def _execute():
        conn = sqlite3.connect(DB_PATH, timeout=10)
        try:
            cursor = conn.cursor()
            cursor.execute(query, params)
            result = cursor.fetchall() if fetchall else cursor.fetchone()
            if commit:
                conn.commit()
            return result
        except Exception as e:
            logger.error(f"DB Error: {e} | Query: {query[:100]}")
            if commit and conn:
                conn.rollback()
            raise
        finally:
            conn.close()

    try:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _execute)
    except Exception as e:
        logger.error(f"run_db_query failed: {e}")
        return None if not fetchall else []

async def is_rate_limited(phone: str) -> bool:
    if not phone or len(phone) < 9:
        return True
    try:
        now = datetime.utcnow()
        one_min_ago = (now - timedelta(minutes=1)).isoformat()
        await run_db_query("DELETE FROM rate_limits WHERE timestamp < ?", (one_min_ago,), commit=True)
        
        count = await run_db_query(
            "SELECT COUNT(*) FROM rate_limits WHERE phone = ? AND timestamp > ?",
            (phone, one_min_ago)
        )
        count = count[0] if count else 0
        
        if count >= 25:
            return True
        await run_db_query("INSERT INTO rate_limits (phone, timestamp) VALUES (?, ?)", 
                          (phone, now.isoformat()), commit=True)
        return False
    except:
        return True

# ==========================================
# חיפוש משופר
# ==========================================
def extract_tracks_from_innertube(data: dict) -> List[dict]:
    tracks = []
    def recursive(node):
        if isinstance(node, dict):
            for key in ["videoRenderer", "compactVideoRenderer"]:
                if key in node:
                    r = node[key]
                    if r.get("videoId"):
                        title_runs = r.get("title", {}).get("runs", [{}])
                        title = title_runs[0].get("text") or r.get("title", {}).get("simpleText", "שיר ללא שם")
                        tracks.append({
                            "id": r["videoId"],
                            "title": title,
                            "duration": r.get("lengthText", {}).get("simpleText", "00:00"),
                            "author": r.get("longBylineText", {}).get("runs", [{}])[0].get("text", "אמן")
                        })
                        return
            for v in node.values():
                recursive(v)
        elif isinstance(node, list):
            for item in node:
                recursive(item)
    recursive(data)
    return tracks

async def search_youtube_innertube(query: str, filter_newest: bool = False) -> List[dict]:
    if not filter_newest and query in search_cache:
        return search_cache[query]

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://www.youtube.com/youtubei/v1/search",
                json={
                    "context": {"client": {"clientName": "WEB", "clientVersion": "2.20260601.01.00", "hl": "he", "gl": "IL"}},
                    "query": query,
                    "params": "EgQIARAB" if filter_newest else None
                },
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Content-Type": "application/json",
                    "Origin": "https://www.youtube.com"
                },
                timeout=7.0
            )
            if resp.status_code == 200:
                tracks = extract_tracks_from_innertube(resp.json())
                if tracks:
                    if not filter_newest:
                        search_cache[query] = tracks
                    logger.info(f"✅ InnerTube success: {len(tracks)} tracks")
                    return tracks[:15]
    except Exception as e:
        logger.error(f"InnerTube failed: {e}")

    # Fallback
    fallback = await search_invidious_fallback(query)
    return fallback[:15] if fallback else EMERGENCY_PLAYLIST

async def search_invidious_fallback(query: str) -> List[dict]:
    instances = ["https://invidious.projectsegfau.lt", "https://vid.puffyan.us", "https://invidious.nerdvpn.de"]
    async with httpx.AsyncClient() as client:
        for inst in instances:
            try:
                r = await client.get(f"{inst}/api/v1/search", params={"q": query, "type": "video"}, timeout=6.0)
                if r.status_code == 200:
                    data = r.json()
                    tracks = [{
                        "id": item["videoId"],
                        "title": item.get("title", "שיר ללא שם"),
                        "duration": str(item.get("lengthSeconds", "00:00")),
                        "author": item.get("author", "אמן")
                    } for item in data if item.get("videoId")][:15]
                    if tracks:
                        logger.info(f"✅ Invidious success via {inst}")
                        return tracks
            except Exception as e:
                logger.warning(f"Invidious {inst} failed: {e}")
    return []

# ==========================================
# Streaming משופר
# ==========================================
async def get_stream_url(video_id: str) -> str:
    if video_id in stream_url_cache:
        return stream_url_cache[video_id]

    async with httpx.AsyncClient() as client:
        for inst in ["https://api.cobalt.tools/api/json", "https://cobalt.api.v0.wtf/api/json"]:
            try:
                r = await client.post(inst, json={
                    "url": f"https://www.youtube.com/watch?v={video_id}",
                    "downloadMode": "audio",
                    "audioFormat": "mp3"
                }, timeout=5.0)
                if r.status_code == 200 and (url := r.json().get("url")):
                    stream_url_cache[video_id] = url
                    return url
            except:
                continue

        # Fallbacks
        url = f"https://invidious.projectsegfau.lt/latest_version?id={video_id}&itag=140"
        stream_url_cache[video_id] = url
        return url

@app.get("/stream/{video_id}.mp3")
async def proxy_mp3_stream(video_id: str, request: Request):
    if not video_id or len(video_id) != 11:
        raise HTTPException(400, "Invalid ID")
    
    if await is_rate_limited("stream_" + video_id[-6:]):  # הגנה בסיסית
        raise HTTPException(429, "Too many requests")

    target_url = await get_stream_url(video_id)

    async def generator():
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(25.0)) as client:  # timeout ארוך יותר
                async with client.stream("GET", target_url, headers={"User-Agent": "Mozilla/5.0"}) as resp:
                    if resp.status_code != 200:
                        logger.error(f"Stream {video_id} failed with {resp.status_code}")
                        yield b""  # שקט במקום קריסה
                        return
                    async for chunk in resp.aiter_bytes(64 * 1024):
                        yield chunk
        except Exception as e:
            logger.error(f"Streaming error {video_id}: {e}")

    return StreamingResponse(generator(), media_type="audio/mpeg")

# ==========================================
# IVR Helpers
# ==========================================
def clean_text_for_ivr(text: str) -> str:
    return re.sub(r'\s+', ' ', re.sub(r'[^a-zA-Z0-9\sא-ת]', ' ', text or "")).strip()[:180]

def make_ivr_read_command(text: str, min_dig="1", max_dig="1", sec=8, mode="digits") -> str:
    clean = clean_text_for_ivr(text)
    return f"read=t-{clean}=ValName,no,{max_dig},{min_dig},{sec},{mode.lower()},no"

def get_final_play_command(video_id: str, request: Request) -> str:
    host = (request.headers.get("x-forwarded-host") or 
            request.headers.get("host") or 
            "localhost").split(":")[0]
    protocol = request.headers.get("x-forwarded-proto") or ("https" if "localhost" not in host else "http")
    port = ":10000" if "localhost" in host else ""
    return f"read={protocol}://{host}{port}/stream/{video_id}.mp3=ValName,no,1,0,2,digits,no"

# ==========================================
# Background
# ==========================================
async def active_session_cleanup():
    while True:
        await run_db_query("DELETE FROM sessions WHERE last_active < ?", 
                          ((datetime.utcnow() - timedelta(hours=4)).isoformat(),), commit=True)
        await asyncio.sleep(1800)

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(active_session_cleanup())

# ==========================================
# Main Endpoint
# ==========================================
@app.get("/youtube", response_class=PlainTextResponse)
async def handle_ivr(request: Request, ApiPhone: str = Query(None), hangup: str = Query(None)):
    if hangup == "yes" or not ApiPhone:
        return "OK"

    val_params = [v for k, v in request.query_params.multi_items() if k.lower() == "valname"]
    ValName = val_params[-1] if val_params else None

    logger.info(f"📞 Phone: {ApiPhone} | ValName: {ValName}")

    if await is_rate_limited(ApiPhone):
        return make_ivr_read_command("יותר מדי פעולות, המתן מעט", "1", "1", 6)

    # Load session safely
    session_data = await run_db_query("SELECT state, playlist_json, current_index FROM sessions WHERE phone = ?", (ApiPhone,))
    if session_data:
        state, playlist_json, index = session_data
        playlist = json.loads(playlist_json) if playlist_json else []
        index = index or 0
    else:
        is_whitelisted = bool(await run_db_query("SELECT authorized FROM users WHERE phone = ?", (ApiPhone,)))
        state = "MAIN_MENU" if is_whitelisted else "CHECK_AUTH"
        playlist = []
        index = 0
        await run_db_query(
            "INSERT INTO sessions (phone, state, playlist_json, current_index, last_active) VALUES (?, ?, ?, ?, ?)",
            (ApiPhone, state, "[]", 0, datetime.utcnow().isoformat()), commit=True
        )

    await run_db_query("UPDATE sessions SET last_active = ? WHERE phone = ?", 
                      (datetime.utcnow().isoformat(), ApiPhone), commit=True)

    # Auth
    if state == "CHECK_AUTH":
        if ValName == "1234":
            await run_db_query("INSERT OR REPLACE INTO users (phone, authorized) VALUES (?, 1)", (ApiPhone,), commit=True)
            state = "MAIN_MENU"
        else:
            return make_ivr_read_command("קוד שגוי או הקש קוד" if ValName else "הקש קוד גישה", "4", "4", 10)

    # ==================== MAIN MENU ====================
    if state == "MAIN_MENU":
        if ValName == "1":
            await run_db_query("UPDATE sessions SET state = 'WAITING_FOR_SEARCH' WHERE phone = ?", (ApiPhone,), commit=True)
            return make_ivr_read_command("אמרו את שם השיר אחרי הצליל", "1", "50", 12, "voice")
        if ValName == "2":
            tracks = await search_youtube_innertube("שירים חסידיים חדשים", filter_newest=True)
            await run_db_query("UPDATE sessions SET state='PLAYING_TRACKS', playlist_json=?, current_index=0 WHERE phone=?", 
                              (json.dumps(tracks), ApiPhone), commit=True)
            return get_final_play_command(tracks[0]["id"], request) if tracks else make_ivr_read_command("אין תוצאות", "1", "1", 5)
        if ValName == "3":
            favs = await run_db_query("SELECT video_id, title FROM favorites WHERE phone = ?", (ApiPhone,), fetchall=True)
            if not favs:
                return make_ivr_read_command("אין מועדפים", "1", "1", 5)
            tracks = [{"id": f[0], "title": f[1], "duration": "00:00", "author": ""} for f in favs]
            await run_db_query("UPDATE sessions SET state='PLAYING_TRACKS', playlist_json=?, current_index=0 WHERE phone=?", 
                              (json.dumps(tracks), ApiPhone), commit=True)
            return get_final_play_command(tracks[0]["id"], request)
        return make_ivr_read_command("1=חיפוש 2=חדשים 3=מועדפים", "1", "1", 10)

    # ==================== SEARCH ====================
    if state == "WAITING_FOR_SEARCH":
        if not ValName or len(ValName.strip()) < 2:
            return make_ivr_read_command("לא הבנתי, אמרו שוב", "1", "50", 10, "voice")
        tracks = await search_youtube_innertube(ValName.strip())
        await run_db_query("UPDATE sessions SET state='PLAYING_TRACKS', playlist_json=?, current_index=0 WHERE phone=?", 
                          (json.dumps(tracks), ApiPhone), commit=True)
        return get_final_play_command(tracks[0]["id"], request) if tracks else make_ivr_read_command("אין תוצאות", "1", "1", 5)

    # ==================== PLAYING_TRACKS ====================
    if state == "PLAYING_TRACKS":
        if not playlist:
            playlist = EMERGENCY_PLAYLIST
            await run_db_query("UPDATE sessions SET playlist_json=? WHERE phone=?", (json.dumps(playlist), ApiPhone), commit=True)

        total = len(playlist)
        index = max(0, index % total) if total > 0 else 0

        if ValName == "1":
            index = (index + 1) % total
        elif ValName == "2":
            index = (index - 1) % total
        elif ValName == "3":
            return make_ivr_read_command("מושהה • 4=המשך • 0=תפריט", "1", "1", 20)
        elif ValName == "5":
            random.shuffle(playlist)
            index = 0
            await run_db_query("UPDATE sessions SET playlist_json=? WHERE phone=?", (json.dumps(playlist), ApiPhone), commit=True)
        elif ValName == "6":
            curr = playlist[index]
            await run_db_query("INSERT OR IGNORE INTO favorites VALUES (?,?,?)", 
                              (ApiPhone, curr["id"], curr["title"]), commit=True)
            return make_ivr_read_command("נוסף למועדפים", "1", "1", 3)
        elif ValName == "0":
            await run_db_query("UPDATE sessions SET state='MAIN_MENU', playlist_json='[]', current_index=0 WHERE phone=?", (ApiPhone,), commit=True)
            return make_ivr_read_command("חוזר לתפריט", "1", "1", 4)

        # Save index
        await run_db_query("UPDATE sessions SET current_index=? WHERE phone=?", (index, ApiPhone), commit=True)
        return get_final_play_command(playlist[index]["id"], request)

    return "hangup"
