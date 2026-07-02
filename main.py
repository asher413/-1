import os
import json
import re
import asyncio
import sqlite3
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, List
from fastapi import FastAPI, Query, Request, HTTPException
from fastapi.responses import PlainTextResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from cachetools import TTLCache

# ==========================================
# 📋 19. הגדרת לוגר מקצועי ומסודר
# ==========================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("IVR_Production_Engine")

app = FastAPI(title="Advanced IVR YouTube Engine 2026")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ==========================================
# 🔑 20. הגדרות אבטחה ומשתני סביבה
# ==========================================
RAPIDAPI_KEY = os.environ.get("RAPIDAPI_KEY", "b356e0c424msh95c209990ea7472p1fe240jsn2a029b5480bf")
RAPIDAPI_HOST = "youtube-mp3-audio-video-downloader.p.rapidapi.com"
DB_PATH = "ivr_production.db"

# 6. שכבת Cache פנימית לתוצאות חיפוש וכתובות שמע (ל-15 דקות)
search_cache = TTLCache(maxsize=1000, ttl=900)
stream_url_cache = TTLCache(maxsize=500, ttl=600)

# 9. הגבלת כמות הזרמות סימולטניות למניעת קריסת השרת ב-Render
STREAM_SEMAPHORE = asyncio.Semaphore(25)

# ==========================================
# 💾 22. בסיס נתונים קבוע (SQLite)
# ==========================================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    # טבלת משתמשים והרשאות (21)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            phone TEXT PRIMARY KEY,
            authorized INTEGER DEFAULT 0,
            access_code TEXT DEFAULT '1234'
        )
    """)
    # טבלת סשנים קבועה (7, 13)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            phone TEXT PRIMARY KEY,
            state TEXT,
            playlist_json TEXT,
            current_index INTEGER DEFAULT 0,
            last_active TEXT
        )
    """)
    # טבלת מועדפים (25)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS favorites (
            phone TEXT,
            video_id TEXT,
            title TEXT,
            PRIMARY KEY(phone, video_id)
        )
    """)
    # טבלת הגבלת קצב (23)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS rate_limits (
            phone TEXT,
            timestamp TEXT
        )
    """)
    
    # הזנת מספרי Whitelist ראשוניים למערכת
    default_whitelist = ["0534133753", "0534133754"]
    for ph in default_whitelist:
        cursor.execute("INSERT OR IGNORE INTO users (phone, authorized) VALUES (?, 1)", (ph,))
        
    conn.commit()
    conn.close()

init_db()

# עטיפת פעולות ה-DB בריצה אסינכרונית כדי לא לחסום את ה-Event Loop (16)
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

# ==========================================
# 🛡️ 23. מנגנון מגבלת קצב (Rate Limiting)
# ==========================================
async def is_rate_limited(phone: str) -> bool:
    now = datetime.utcnow()
    one_minute_ago = (now - timedelta(minutes=1)).isoformat()
    await run_db_query("DELETE FROM rate_limits WHERE timestamp < ?", (one_minute_ago,), commit=True)
    
    recent_requests = await run_db_query(
        "SELECT COUNT(*) FROM rate_limits WHERE phone = ? AND timestamp > ?", 
        (phone, one_minute_ago), fetchall=False
    )
    if recent_requests and recent_requests[0] >= 20: # מקסימום 20 בקשות בדקה
        return True
        
    await run_db_query("INSERT INTO rate_limits (phone, timestamp) VALUES (?, ?)", (phone, now.isoformat()), commit=True)
    return False

# ==========================================
# 🔍 1. מנוע חיפוש חסין דרך YouTube InnerTube API
# ==========================================
async def search_youtube_innertube(query: str, filter_newest: bool = False) -> List[dict]:
    """
    פנייה ישירה ל-API הפנימי של יוטיוב. מחזיר כותרת, מזהה, אורך ויוצר (2, 14).
    חסין לחלוטין משינויי HTML, עוקף קאפצ'ות ודפי הסכמה (Consent).
    """
    current_year = datetime.now().year # 10. דינמי לחלוטין
    if filter_newest:
        query += f" חדש {current_year}"

    if query in search_cache:
        logger.info(f"Search Cache Hit for query: {query}")
        return search_cache[query]

    url = "https://www.youtube.com/youtubei/v1/search"
    payload = {
        "context": {
            "client": {
                "clientName": "WEB",
                "clientVersion": "2.20240228.00.00",
                "hl": "he",
                "gl": "IL"
            }
        },
        "query": query
    }
    
    # 17. מנגנון Retry אסינכרוני עם Exponential Backoff
    async with httpx.AsyncClient() as client:
        for attempt in range(3):
            try:
                response = await client.post(url, json=payload, timeout=4.0)
                if response.status_code == 200:
                    data = response.json()
                    tracks = []
                    
                    # סריקת עץ ה-JSON היציב של יוטיוב
                    contents = data.get("contents", {}).get("twoColumnSearchResultsRenderer", {}).get("primaryContents", {}).get("sectionListRenderer", {}).get("contents", [])
                    if not contents:
                        continue
                        
                    item_section = contents[0].get("itemSectionRenderer", {}).get("contents", [])
                    for item in item_section:
                        if "videoRenderer" in item:
                            vr = item["videoRenderer"]
                            video_id = vr.get("videoId")
                            title = vr.get("title", {}).get("runs", [{}])[0].get("text", "שיר ללא שם")
                            duration = vr.get("lengthText", {}).get("simpleText", "00:00")
                            author = vr.get("longBylineText", {}).get("runs", [{}])[0].get("text", "אמן לא ידוע")
                            
                            tracks.append({
                                "id": video_id,
                                "title": title,
                                "duration": duration,
                                "author": author
                            })
                            if len(tracks) >= 15: # הגבלה ל-15 תוצאות איכותיות
                                break
                                
                    if tracks:
                        search_cache[query] = tracks
                        return tracks
            except Exception as e:
                logger.warning(f"Attempt {attempt+1} failed searching YouTube: {e}")
                await asyncio.sleep(0.5 * (attempt + 1))
                
    return []

# ==========================================
# 🗜️ 3, 4. מנועי שליית קישורי המרה עם הגנות חסימה
# ==========================================
async def fetch_cobalt_link(video_id: str, client: httpx.AsyncClient) -> Optional[str]:
    instances = ["https://api.cobalt.tools/api/json", "https://cobalt.api.v0.wtf/api/json"]
    payload = {"url": f"https://www.youtube.com/watch?v={video_id}", "downloadMode": "audio", "audioFormat": "mp3"}
    for inst in instances:
        try:
            res = await client.post(inst, json=payload, headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"}, timeout=3.5)
            if res.status_code == 200:
                return res.json().get("url")
        except Exception:
            continue
    return None

async def fetch_rapidapi_link(video_id: str, client: httpx.AsyncClient) -> Optional[str]:
    url = f"https://{RAPIDAPI_HOST}/get_mp3_download_link/{video_id}"
    try:
        res = await client.get(url, headers={"x-rapidapi-key": RAPIDAPI_KEY, "x-rapidapi-host": RAPIDAPI_HOST}, timeout=4.0)
        if res.status_code == 200:
            js = res.json()
            # 4. מניעת קריסה במקרה של מצב עיבוד (processing)
            if "processing" not in str(js.get("comment", "")).lower():
                return js.get("file") or js.get("link") or js.get("url")
    except Exception:
        pass
    return None

# ==========================================
# 🎵 5, 18. מזרים אסינכרוני מבוקר (Streaming Proxy)
# ==========================================
@app.get("/stream/{video_id}.mp3")
async def proxy_mp3_stream(video_id: str):
    """מזרים מידע אסינכרוני מבוקר עם Semaphore ו-Timeout הגנתי"""
    if video_id in stream_url_cache:
        target_url = stream_url_cache[video_id]
    else:
        async with httpx.AsyncClient() as client:
            target_url = await fetch_cobalt_link(video_id, client) or await fetch_rapidapi_link(video_id, client)
            if not target_url:
                # Fallback אחרון - אינווידיוס
                target_url = f"https://invidious.projectsegfau.lt/latest_version?id={video_id}&itag=140"
            stream_url_cache[video_id] = target_url

    async def chunk_generator():
        # 9. שימוש ב-Semaphore למניעת הצפת השרת
        async with STREAM_SEMAPHORE:
            try:
                # 5. שימוש ב-httpx.stream אסינכרוני מנוהל עם Timeout קשיח
                async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, read=30.0)) as client:
                    async with client.stream("GET", target_url, headers={"User-Agent": "Mozilla/5.0"}) as response:
                        if response.status_code not in [200, 206]:
                            return
                        async_iterator = response.aiter_bytes(chunk_size=64 * 1024)
                        while True:
                            try:
                                chunk = await asyncio.wait_for(async_iterator.__anext__(), timeout=10.0)
                                yield chunk
                            except StopAsyncIteration:
                                break
                            except asyncio.TimeoutError:
                                logger.error("Stream read timeout reached.")
                                break
            except Exception as e:
                logger.error(f"Streaming anomaly detected: {e}")

    return StreamingResponse(chunk_generator(), media_type="audio/mpeg")

# ==========================================
# 📞 מחולל פקודות מערכת מותאם לימות המשיח
# ==========================================
def make_ivr_read_command(text: str, min_dig: str, max_dig: str, sec: int, mode: str) -> str:
    clean = text.replace("=", "").replace(",", "").replace("-", "")
    if mode.lower() == "voice":
        return f"read=t-{clean}=ValName,no,50,1,{sec},voice,no&"
    confirm = "yes" if (max_dig and int(max_dig) > 1) else "no"
    return f"read=t-{clean}=ValName,no,{max_dig},{min_dig},{sec},{mode.lower()},{confirm}&"

def get_final_play_command(video_id: str, title: str, request: Request) -> str:
    host = request.headers.get("host", "").split(":")[0]
    protocol = "https" if "localhost" not in host and "127.0.0.1" not in host else "http"
    port_suffix = ":10000" if "localhost" in host else ""
    stream_url = f"{protocol}://{host}{port_suffix}/stream/{video_id}.mp3"
    
    # 24, 25. הקראת כותרת השיר ומעבר חופשי לניהול הנגן (1=הבא, 2=הקודם, 3=עצור, 4=המשך, 5=ערבוב, 6=מועדפים, 0=תפריט)
    announcement = f"מנגן כעת את, {title}. לשיר הבא הקש 1, לקודם 2, לעצירה 3, למועדפים 6, לתפריט 0."
    return f"read=t-{announcement}=ValName,no,1,0,3,digits,no&" \
           f"read={stream_url}=ValName,no,1,0,1,digits,no&"

# ==========================================
# 🗃️ 8. מנקה Sessions רדומים אוטומטי (Memory Leak Protection)
# ==========================================
async def active_session_cleanup():
    while True:
        try:
            limit_time = (datetime.utcnow() - timedelta(hours=2)).isoformat()
            await run_db_query("DELETE FROM sessions WHERE last_active < ?", (limit_time,), commit=True)
        except Exception as e:
            logger.error(f"Session cleaner error: {e}")
        await asyncio.sleep(1800) # ריצה כל חצי שעה

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(active_session_cleanup())

# ==========================================
# 🎛️ הליבה המרכזית: ניהול פרוטוקול ה-IVR האסינכרוני
# ==========================================
@app.get("/youtube", response_class=PlainTextResponse)
async def handle_ivr(request: Request, ApiPhone: str = Query(None), hangup: str = Query(None)):
    if hangup == "yes" or not ApiPhone:
        return "OK"

    # הגנת Rate Limit (23)
    if await is_rate_limited(ApiPhone):
        return make_ivr_read_command("בוצעו יותר מדי פעולות בדקה אנא המתן מעט", "1", "1", 5, "digits")

    val_params = [v for k, v in request.query_params.multi_items() if k == "ValName"]
    ValName = val_params[-1] if val_params else None

    # 7, 21, 22. שליפת סשן קבוע וסטטוס אבטחה מה-DB (מונע מחיקה באתחול שרת)
    user_data = await run_db_query("SELECT authorized FROM users WHERE phone = ?", (ApiPhone,))
    is_whitelisted = user_data and user_data[0] == 1
    
    session_data = await run_db_query("SELECT state, playlist_json, current_index FROM sessions WHERE phone = ?", (ApiPhone,))
    
    if not session_data:
        state = "MAIN_MENU" if is_whitelisted else "CHECK_AUTH"
        playlist = []
        index = 0
        await run_db_query(
            "INSERT INTO sessions (phone, state, playlist_json, current_index, last_active) VALUES (?, ?, ?, ?, ?)",
            (ApiPhone, state, "[]", 0, datetime.utcnow().isoformat()), commit=True
        )
    else:
        state, playlist_json, index = session_data
        playlist = json.loads(playlist_json)

    # עדכון זמן פעילות אחרון
    await run_db_query("UPDATE sessions SET last_active = ? WHERE phone = ?", (datetime.utcnow().isoformat(), ApiPhone), commit=True)

    # --- אימות קוד גישה ---
    if not is_whitelisted and state == "CHECK_AUTH":
        if ValName == "1234": # 20. קוד גישה
            await run_db_query("INSERT OR REPLACE INTO users (phone, authorized) VALUES (?, 1)", (ApiPhone,), commit=True)
            state = "MAIN_MENU"
            ValName = None
        else:
            if ValName:
                return make_ivr_read_command("קוד שגוי, אנא נסה שנית", "4", "4", 10, "digits")
            return make_ivr_read_command("אנא הקש את קוד הגישה", "4", "4", 10, "digits")

    # --- 12, 25. ניהול מצבי הנגן והתפריטים ---
    if state == "MAIN_MENU":
        if ValName == "1":
            await run_db_query("UPDATE sessions SET state = 'WAITING_FOR_SEARCH' WHERE phone = ?", (ApiPhone,), commit=True)
            return make_ivr_read_command("אנא אמרו את שם השיר המבוקש לאחר הצליל", "1", "50", 10, "voice")
        elif ValName == "2":
            tracks = await search_youtube_innertube("שירים חסידיים", filter_newest=True)
            if not tracks:
                return make_ivr_read_command("לא נמצאו שירים כרגע", "1", "1", 3, "digits")
            await run_db_query("UPDATE sessions SET state = 'PLAYING_TRACKS', playlist_json = ?, current_index = 0 WHERE phone = ?", (json.dumps(tracks), ApiPhone), commit=True)
            return get_final_play_command(tracks[0]["id"], tracks[0]["title"], request)
        elif ValName == "3":
            # שלוחת מועדפים (25)
            favs = await run_db_query("SELECT video_id, title FROM favorites WHERE phone = ?", (ApiPhone,), fetchall=True)
            if not favs:
                return make_ivr_read_command("רשימת המועדפים שלך ריקה, חוזר לתפריט", "1", "1", 4, "digits")
            tracks = [{"id": f[0], "title": f[1], "duration": "00:00", "author": ""} for f in favs]
            await run_db_query("UPDATE sessions SET state = 'PLAYING_TRACKS', playlist_json = ?, current_index = 0 WHERE phone = ?", (json.dumps(tracks), ApiPhone), commit=True)
            return get_final_play_command(tracks[0]["id"], tracks[0]["title"], request)
        else:
            return make_ivr_read_command("לחיפוש קולי הקש 1, לשירים חדשים הקש 2, למועדפים הקש 3", "1", "1", 10, "digits")

    elif state == "WAITING_FOR_SEARCH":
        if not ValName or ValName in ["1", "2", "*", "#"]:
            return make_ivr_read_command("לא קלטתי, אנא אמרו את שם השיר בבירור", "1", "50", 10, "voice")
        
        # 2. שליית מטא-דאטה מסודרת
        tracks = await search_youtube_innertube(ValName, filter_newest=False)
        if not tracks:
            await run_db_query("UPDATE sessions SET state = 'MAIN_MENU' WHERE phone = ?", (ApiPhone,), commit=True)
            return make_ivr_read_command("לא נמצאו תוצאות, חוזר לתפריט", "1", "1", 4, "digits")
        
        # 12. שמירה על רשימת הנגן גם לאחר חיפוש יחיד
        await run_db_query(
            "UPDATE sessions SET state = 'PLAYING_TRACKS', playlist_json = ?, current_index = 0 WHERE phone = ?", 
            (json.dumps(tracks), ApiPhone), commit=True
        )
        return get_final_play_command(tracks[0]["id"], tracks[0]["title"], request)

    elif state == "PLAYING_TRACKS":
        if not playlist:
            # 11. פתרון סמנטי במקום קריסה או צליל לא קשור
            await run_db_query("UPDATE sessions SET state = 'MAIN_MENU' WHERE phone = ?", (ApiPhone,), commit=True)
            return make_ivr_read_command("לא ניתן להשמיע קובץ זה כרגע, חוזר לתפריט", "1", "1", 4, "digits")

        # 25. מערכת שליטה מתקדמת במקשי הנגן
        if ValName == "1" or ValName == "": # הבא או שיר שהסתיים אוטומטית
            index += 1
        elif ValName == "2": # הקודם
            index -= 1
        elif ValName == "3": # השהיה / עצירה
            return make_ivr_read_command("השמעה מושהית, להמשך הקש 4, לחזרה לתפריט הקש 0", "1", "1", 20, "digits")
        elif ValName == "4": # המשך מאותה נקודה
            pass 
        elif ValName == "5": # 25. ערבוב (Shuffle)
            import random
            random.shuffle(playlist)
            index = 0
            await run_db_query("UPDATE sessions SET playlist_json = ? WHERE phone = ?", (json.dumps(playlist), ApiPhone), commit=True)
        elif ValName == "6": # 25. הוספה למועדפים
            curr_track = playlist[index % len(playlist)]
            await run_db_query("INSERT OR IGNORE INTO favorites (phone, video_id, title) VALUES (?, ?, ?)", (ApiPhone, curr_track["id"], curr_track["title"]), commit=True)
            return make_ivr_read_command("השיר נוסף למועדפים שלך, ממשיך בנגינה", "1", "1", 3, "digits")
        elif ValName == "0":
            await run_db_query("UPDATE sessions SET state = 'MAIN_MENU' WHERE phone = ?", (ApiPhone,), commit=True)
            return make_ivr_read_command("חוזר לתפריט הראשי", "1", "1", 3, "digits")

        index = index % len(playlist)
        await run_db_query("UPDATE sessions SET current_index = ? WHERE phone = ?", (index, ApiPhone), commit=True)
        
        target_track = playlist[index]
        return get_final_play_command(target_track["id"], target_track["title"], request)

    return "hangup"
