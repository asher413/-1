import os
import json
import re
import httpx
import asyncio
import sqlite3
import logging
import random
from datetime import datetime, timedelta
from typing import List

from fastapi import FastAPI, Query, Request, HTTPException
from fastapi.responses import PlainTextResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from cachetools import TTLCache

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("IVR_Production_Engine")

app = FastAPI(title="Bulletproof IVR YouTube Engine 2026")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

DB_PATH = "ivr_production.db"
search_cache = TTLCache(maxsize=1000, ttl=900)
stream_url_cache = TTLCache(maxsize=600, ttl=720)

EMERGENCY_PLAYLIST = [
    {"id": "4NzIOLEeJZM", "title": "נחמן פילמר שמחה פורצת גבולות", "duration": "1:26:07", "author": "נחמן פילמר"},
    {"id": "WSMFtm3ZqcY", "title": "סט להיטים דתי חרדי", "duration": "1:26:18", "author": "פול ווליום"},
    {"id": "3QDfxHZaUik", "title": "סט להיטים דתי מקפיץ", "duration": "1:45:25", "author": "מדע והשכל"}
]

# ========================================== DB ==========================================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS users (phone TEXT PRIMARY KEY, authorized INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS sessions (phone TEXT PRIMARY KEY, state TEXT, playlist_json TEXT, current_index INTEGER DEFAULT 0, last_active TEXT);
        CREATE TABLE IF NOT EXISTS favorites (phone TEXT, video_id TEXT, title TEXT, PRIMARY KEY(phone, video_id));
        CREATE TABLE IF NOT EXISTS rate_limits (phone TEXT, timestamp TEXT);
    """)
    for ph in ["0534133753", "0534133754"]:
        cursor.execute("INSERT OR IGNORE INTO users (phone, authorized) VALUES (?, 1)", (ph,))
    conn.commit()
    conn.close()

init_db()

async def run_db_query(query: str, params: tuple = (), fetchall=False, commit=False):
    def _exec():
        conn = sqlite3.connect(DB_PATH, timeout=15)
        try:
            cur = conn.cursor()
            cur.execute(query, params)
            res = cur.fetchall() if fetchall else cur.fetchone()
            if commit: conn.commit()
            return res
        finally:
            conn.close()
    return await asyncio.get_running_loop().run_in_executor(None, _exec)

async def is_rate_limited(phone: str, limit=25) -> bool:
    if not phone: return True
    now = datetime.utcnow()
    await run_db_query("DELETE FROM rate_limits WHERE timestamp < ?", ((now - timedelta(minutes=1)).isoformat(),), commit=True)
    count_row = await run_db_query("SELECT COUNT(*) FROM rate_limits WHERE phone=? AND timestamp>?", (phone, (now-timedelta(minutes=1)).isoformat()))
    count = count_row[0] if count_row else 0
    if count >= limit: return True
    await run_db_query("INSERT INTO rate_limits VALUES (?,?)", (phone, now.isoformat()), commit=True)
    return False

# ========================================== Parsing ==========================================
def seconds_to_mmss(sec):
    try:
        s = int(sec)
        return f"{s//60:02d}:{s%60:02d}"
    except:
        return "00:00"

def extract_tracks_robust(data) -> List[dict]:
    tracks = []
    def deep_search(node):
        if isinstance(node, dict):
            # Direct renderers
            for rkey in ["videoRenderer", "compactVideoRenderer", "richItemRenderer"]:
                renderer = node.get(rkey)
                if renderer and isinstance(renderer, dict):
                    vid = renderer.get("videoId") or renderer.get("content", {}).get("videoRenderer", {}).get("videoId")
                    if vid:
                        title_obj = renderer.get("title", {})
                        title = title_obj.get("simpleText") or (title_obj.get("runs", [{}])[0].get("text") if isinstance(title_obj.get("runs"), list) else "שיר")
                        tracks.append({
                            "id": vid,
                            "title": title,
                            "duration": seconds_to_mmss(renderer.get("lengthSeconds") or renderer.get("lengthText", {}).get("simpleText", 0)),
                            "author": renderer.get("longBylineText", {}).get("runs", [{}])[0].get("text", "אמן")
                        })
                        if len(tracks) >= 15: return
            # Go deeper
            for v in node.values():
                deep_search(v)
        elif isinstance(node, list):
            for item in node:
                deep_search(item)
    deep_search(data)
    return tracks

async def search_youtube_innertube(query: str, filter_newest: bool = False) -> List[dict]:
    if not filter_newest and query in search_cache:
        return search_cache[query]

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://www.youtube.com/youtubei/v1/search",
                json={
                    "context": {"client": {"clientName": "WEB", "clientVersion": "2.20260701.01.00", "hl": "he", "gl": "IL"}},
                    "query": query,
                    "params": "EgQIARAB" if filter_newest else None
                },
                headers={"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"},
                timeout=8.0
            )
            logger.info(f"InnerTube status: {resp.status_code}")
            if resp.status_code == 200:
                tracks = extract_tracks_robust(resp.json())
                if tracks:
                    logger.info(f"✅ InnerTube parsed {len(tracks)} tracks")
                    if not filter_newest:
                        search_cache[query] = tracks
                    return tracks[:15]
                else:
                    logger.warning("InnerTube 200 but no tracks. Keys: " + str(list(resp.json().keys())[:15]))
    except Exception as e:
        logger.error(f"InnerTube failed: {e}")

    logger.info("→ Trying Invidious fallback")
    return await search_invidious_fallback(query) or EMERGENCY_PLAYLIST

async def search_invidious_fallback(query: str) -> List[dict]:
    instances = ["https://invidious.projectsegfau.lt", "https://vid.puffyan.us", "https://invidious.nerdvpn.de"]
    for base in instances:
        try:
            async with httpx.AsyncClient() as client:
                r = await client.get(f"{base}/api/v1/search?q={query}&type=video", timeout=7.0)
                if r.status_code == 200:
                    try:
                        data = r.json()
                        tracks = [{
                            "id": item["videoId"],
                            "title": item.get("title", "שיר"),
                            "duration": seconds_to_mmss(item.get("lengthSeconds")),
                            "author": item.get("author", "")
                        } for item in data if item.get("videoId")][:12]
                        if tracks:
                            logger.info(f"✅ Invidious success via {base}")
                            return tracks
                    except:
                        logger.warning(f"Invidious {base} non-JSON")
        except Exception as e:
            logger.warning(f"Invidious {base} failed: {e}")
    return []

# ========================================== Streaming ==========================================
async def get_stream_url(video_id: str) -> str:
    if video_id in stream_url_cache: return stream_url_cache[video_id]
    async with httpx.AsyncClient() as client:
        for url in ["https://api.cobalt.tools/api/json", "https://cobalt.api.v0.wtf/api/json"]:
            try:
                r = await client.post(url, json={"url": f"https://youtube.com/watch?v={video_id}", "downloadMode": "audio"}, timeout=5.0)
                if r.status_code == 200 and (link := r.json().get("url")):
                    stream_url_cache[video_id] = link
                    return link
            except: continue
    fallback = f"https://invidious.projectsegfau.lt/latest_version?id={video_id}&itag=140"
    stream_url_cache[video_id] = fallback
    return fallback

@app.get("/stream/{video_id}.mp3")
async def proxy_mp3_stream(video_id: str):
    if not video_id or len(video_id) != 11:
        raise HTTPException(400, "Invalid ID")
    if await is_rate_limited(f"stream_{video_id[-6:]}", 30):
        raise HTTPException(429)

    target = await get_stream_url(video_id)
    async def generator():
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
                async with client.stream("GET", target) as r:
                    if r.status_code != 200:
                        logger.error(f"Stream failed {video_id} status {r.status_code}")
                        return
                    async for chunk in r.aiter_bytes(64*1024):
                        yield chunk
        except Exception as e:
            logger.error(f"Stream error {video_id}: {e}")

    return StreamingResponse(generator(), media_type="audio/mpeg")

# ========================================== IVR ==========================================
def clean_text(text: str) -> str:
    return re.sub(r'\s+', ' ', re.sub(r'[^a-zA-Z0-9\sא-ת]', ' ', text or "")).strip()[:180]

def make_read(text: str, min_d="1", max_d="1", sec=10, mode="digits") -> str:
    return f"read=t-{clean_text(text)}=ValName,no,{max_d},{min_d},{sec},{mode.lower()},no"

def get_play_cmd(video_id: str, request: Request) -> str:
    host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or "localhost").split(":")[0]
    proto = request.headers.get("x-forwarded-proto") or ("https" if "localhost" not in host else "http")
    port = ":10000" if "localhost" in host else ""
    return f"read={proto}://{host}{port}/stream/{video_id}.mp3=ValName,no,1,0,2,digits,no"

@app.get("/youtube", response_class=PlainTextResponse)
async def handle_ivr(request: Request, ApiPhone: str = Query(None), hangup: str = Query(None)):
    if hangup == "yes" or not ApiPhone: return "OK"

    val_params = [v for k, v in request.query_params.multi_items() if k.lower() == "valname"]
    ValName = val_params[-1] if val_params else None

    logger.info(f"📞 {ApiPhone} | ValName: {ValName}")

    if await is_rate_limited(ApiPhone):
        return make_read("יותר מדי פעולות, המתן", "1", "1", 6)

    # Load session
    row = await run_db_query("SELECT state, playlist_json, current_index FROM sessions WHERE phone=?", (ApiPhone,))
    if row:
        state, pjson, idx = row[0] if isinstance(row, list) else row
        playlist = json.loads(pjson) if pjson else []
        index = idx or 0
    else:
        auth = await run_db_query("SELECT authorized FROM users WHERE phone=?", (ApiPhone,))
        state = "MAIN_MENU" if auth and auth[0] else "CHECK_AUTH"
        playlist, index = [], 0
        await run_db_query("INSERT INTO sessions (phone,state,playlist_json,current_index,last_active) VALUES (?,?,?,?,?)",
                          (ApiPhone, state, "[]", 0, datetime.utcnow().isoformat()), commit=True)

    await run_db_query("UPDATE sessions SET last_active=? WHERE phone=?", (datetime.utcnow().isoformat(), ApiPhone), commit=True)

    if state == "CHECK_AUTH":
        if ValName == "1234":
            await run_db_query("UPDATE users SET authorized=1 WHERE phone=?", (ApiPhone,), commit=True)
            state = "MAIN_MENU"
        else:
            return make_read("הקש קוד גישה" if not ValName else "קוד שגוי", "4", "4", 12)

    if state == "MAIN_MENU":
        if ValName == "1":
            await run_db_query("UPDATE sessions SET state='WAITING_FOR_SEARCH' WHERE phone=?", (ApiPhone,), commit=True)
            return make_read("אמרו את שם השיר", "1", "50", 12, "voice")
        if ValName == "2":
            tracks = await search_youtube_innertube("שירים חסידיים חדשים", True)
            await run_db_query("UPDATE sessions SET state='PLAYING_TRACKS', playlist_json=?, current_index=0 WHERE phone=?", (json.dumps(tracks), ApiPhone), commit=True)
            return get_play_cmd(tracks[0]["id"], request) if tracks else make_read("אין תוצאות", "1", "1", 5)
        return make_read("1=חיפוש • 2=חדשים • 3=מועדפים", "1", "1", 10)

    if state == "WAITING_FOR_SEARCH":
        if not ValName or len(ValName.strip()) < 2:
            return make_read("לא הבנתי, אמרו שוב", "1", "50", 10, "voice")
        tracks = await search_youtube_innertube(ValName.strip())
        await run_db_query("UPDATE sessions SET state='PLAYING_TRACKS', playlist_json=?, current_index=0 WHERE phone=?", (json.dumps(tracks), ApiPhone), commit=True)
        return get_play_cmd(tracks[0]["id"], request) if tracks else make_read("אין תוצאות", "1", "1", 5)

    if state == "PLAYING_TRACKS":
        if not playlist: playlist = EMERGENCY_PLAYLIST
        total = len(playlist)
        index = max(0, index % total) if total > 0 else 0

        if ValName == "1": index = (index + 1) % total
        elif ValName == "2": index = (index - 1) % total
        elif ValName == "0":
            await run_db_query("UPDATE sessions SET state='MAIN_MENU', playlist_json='[]' WHERE phone=?", (ApiPhone,), commit=True)
            return make_read("חוזר לתפריט", "1", "1", 4)

        await run_db_query("UPDATE sessions SET current_index=? WHERE phone=?", (index, ApiPhone), commit=True)
        return get_play_cmd(playlist[index]["id"], request)

    return "hangup"
