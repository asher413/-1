import io
import json
import re
import urllib.request
import urllib.parse
from fastapi import FastAPI, Query, Request
from fastapi.responses import PlainTextResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ====================================================================
# 🔑 הגדרות מפתח ה-API שלך:
# ====================================================================
RAPIDAPI_KEY = "b356e0c424msh95c209990ea7472p1fe240jsn2a029b5480bf"
RAPIDAPI_HOST = "youtube-mp3-audio-video-downloader.p.rapidapi.com"
# ====================================================================

db_sessions = {}
WHITELIST = ["0534133753", "0534133754"]  
ACCESS_CODE = "1234"                       

def fetch_youtube_ids(query: str, max_results=30, filter_newest=False):
    """שולף מזהי וידאו מיוטיוב בתוך פחות מ-400 מילישניות"""
    if filter_newest:
        query += " חדש 2026"
    
    encoded_query = urllib.parse.quote(query)
    url = f"https://www.youtube.com/results?search_query={encoded_query}"
    
    try:
        req = urllib.request.Request(
            url, 
            headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept-Language': 'he-IL,he;q=0.9,en-US;q=0.8,en;q=0.7'
            }
        )
        with urllib.request.urlopen(req, timeout=2) as response:
            html = response.read().decode('utf-8', errors='ignore')
            found_ids = re.findall(r'"videoId":"([a-zA-Z0-9_-]{11})"', html)
            
            video_ids = []
            for v_id in found_ids:
                if v_id not in video_ids:
                    video_ids.append(v_id)
                if len(video_ids) >= max_results:
                    break
                    
            if video_ids:
                print(f"🎯 Ultra-fast search success! Found {len(video_ids)} videos.")
                return video_ids
    except Exception as e:
        print(f"Direct YouTube search failed: {e}")
            
    return ["YmK2mZf_uRE", "7un666Y6N_Q", "H762G1UoP2k", "4X7bLks7Oxc"][:max_results]


def get_cobalt_mp3_url(video_id: str) -> str:
    """מנוע קובלט מעודכן עם פיילוד מתוקן ב-100% למניעת שגיאות 400"""
    instances = [
        "https://api.cobalt.tools/api/json",
        "https://cobalt.api.v0.wtf/api/json",
        "https://cobalt.moe/api/json"
    ]
    payload = {
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "downloadMode": "audio",  # חובה לקבוע מצב שמע!
        "audioFormat": "mp3"      # חובה לבקש פורמט MP3!
    }
    for instance in instances:
        try:
            encoded_payload = json.dumps(payload).encode('utf-8')
            req = urllib.request.Request(
                instance,
                data=encoded_payload,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "User-Agent": "Mozilla/5.0"
                },
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=4) as response:
                res_data = json.loads(response.read().decode('utf-8'))
                if "url" in res_data:
                    print(f"🚀 Cobalt Engine MP3 Direct Hit via {instance}!")
                    return res_data["url"]
        except Exception as e:
            print(f"Cobalt instance {instance} failed: {e}")
            continue
    return None


def get_rapidapi_mp3_url(video_id: str) -> str:
    """שולף קובץ MP3 מתוך ה-RapidAPI שברשותך"""
    url = f"https://{RAPIDAPI_HOST}/get_mp3_download_link/{video_id}"
    try:
        req = urllib.request.Request(url)
        req.add_header("x-rapidapi-key", RAPIDAPI_KEY)
        req.add_header("x-rapidapi-host", RAPIDAPI_HOST)
        req.add_header("User-Agent", "Mozilla/5.0")
        
        with urllib.request.urlopen(req, timeout=5) as response:
            res_data = json.loads(response.read().decode('utf-8'))
            mp3_link = res_data.get("file") or res_data.get("link") or res_data.get("url")
            if mp3_link and "processing" not in str(res_data.get("comment", "")).lower():
                print("✅ RapidAPI MP3 Cache Hit!")
                return mp3_link
    except Exception as e:
        print(f"RapidAPI MP3 check skipped/timeout: {e}")
    return None


@app.get("/stream/{video_id}.mp3")
def proxy_mp3_stream(video_id: str):
    """מזרים את השמע לימות המשיח בזמן אמת מתוך שרת ההמרה המוצלח ביותר"""
    print(f"🎵 Active Proxy Stream Connection initiated for video: {video_id}")
    
    # 1. ניסיון בשרת קובלט המתוקן
    stream_target = get_cobalt_mp3_url(video_id)
    
    # 2. ניסיון ב-RapidAPI המשולם שלך
    if not stream_target:
        stream_target = get_rapidapi_mp3_url(video_id)
        
    # 3. גיבוי חירום אמין (שרת אינווידיוס ציבורי)
    if not stream_target:
        instances = ["invidious.projectsegfau.lt", "yewtu.be", "invidious.privacydev.net"]
        for instance in instances:
            test_url = f"https://{instance}/latest_version?id={video_id}&itag=140"
            try:
                req = urllib.request.Request(test_url, method="HEAD")
                with urllib.request.urlopen(req, timeout=2) as res:
                    if res.status in [200, 301, 302]:
                        stream_target = test_url
                        break
            except Exception:
                continue
                
    if not stream_target:
        stream_target = "https://www.soundhelix.com/examples/mp3/SoundHelix-Song-1.mp3"

    def stream_generator():
        try:
            req = urllib.request.Request(stream_target, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as response:
                while True:
                    chunk = response.read(32 * 1024)
                    if not chunk:
                        break
                    yield chunk
        except Exception as e:
            print(f"Stream generation stopped: {e}")
            return

    return StreamingResponse(stream_generator(), media_type="audio/mpeg")


def get_final_play_command(video_id: str, request: Request) -> str:
    """מייצר פקודת השמעה רשמית ואמינה מסוג read עם דומיין HTTPS ציבורי ומדויק"""
    host = request.headers.get("host", "").split(":")[0] # שולף את הדומיין האמיתי של Render
    
    if "localhost" not in host and "127.0.0.1" not in host:
        stream_url = f"https://{host}/stream/{video_id}.mp3"
    else:
        stream_url = f"http://localhost:10000/stream/{video_id}.mp3"
        
    # משתמשים בפורמט read הרשמי של ימות המשיח: מנגן את הסטרים, 
    # מאפשר הקשה של ספרה אחת (0 לחזרה, 1 לקדימה, 2 לאחורה) 
    # ואם השיר מסתיים, המערכת מחכה שנייה אחת ומחזירה ערך ריק שמקפיץ לשיר הבא אוטומטית!
    return f"read={stream_url}=ValName,no,1,0,1,digits,no&"


def make_native_tts_command(text: str, min_dig: str, max_dig: str, sec: int, type_mode: str) -> str:
    clean_text = text.replace("=", "").replace(",", "").replace("-", "")
    if type_mode.lower() == "voice":
        return f"read=t-{clean_text}=ValName,no,50,1,{sec},voice,no&"
    confirm_hash = "yes" if (max_dig and int(max_dig) > 1) else "no"
    return f"read=t-{clean_text}=ValName,no,{max_dig},{min_dig},{sec},{type_mode.lower()},{confirm_hash}&"


@app.get("/youtube", response_class=PlainTextResponse)
def handle_ivr(
    request: Request,
    ApiPhone: str = Query(None),
    hangup: str = Query(None)
):
    if hangup == "yes" or not ApiPhone:
        return "OK"

    val_name_choices = [v for k, v in request.query_params.multi_items() if k == "ValName"]
    ValName = val_name_choices[-1] if val_name_choices else None

    if ApiPhone not in db_sessions:
        is_whitelisted = ApiPhone in WHITELIST
        db_sessions[ApiPhone] = {
            "auth": is_whitelisted,
            "state": "MAIN_MENU" if is_whitelisted else "CHECK_AUTH",
            "playlist": [],
            "index": 0
        }

    session = db_sessions[ApiPhone]

    if not session["auth"]:
        if session["state"] == "CHECK_AUTH":
            if ValName == ACCESS_CODE:
                session["auth"] = True
                session["state"] = "MAIN_MENU"
                ValName = None  
            else:
                if ValName is not None and ValName != "": 
                    return make_native_tts_command("קוד שגוי אנא נסה שנית", "4", "4", 10, "digits")
                return make_native_tts_command("אנא הקש את קוד הגישה בן ארבע הספרות", "4", "4", 10, "digits")

    state = session["state"]

    # --- תפריט ראשי ---
    if state == "MAIN_MENU":
        if ValName == "1":
            session["state"] = "WAITING_FOR_SEARCH"
            return make_native_tts_command("אנא אמרו את שם השיר או השיעור המבוקש לאחר הצליל", "1", "50", 10, "voice")
        
        elif ValName == "2":
            session["state"] = "PLAYING_LATEST"
            session["playlist"] = fetch_youtube_ids("שירים חדשים מוזיקה חסידית", max_results=30, filter_newest=True)
            session["index"] = 0
            if session["playlist"]:
                video_id = session["playlist"][0]
                return get_final_play_command(video_id, request)
            return make_native_tts_command("לא נמצאו שירים חוזר לתפריט הראשי", "1", "1", 3, "digits")
            
        elif ValName == "3":
            session["state"] = "PREDEFINED_ARTISTS"
            return make_native_tts_command("לתפריט החיפוש המוכן מראש: לעומר אדם הקש 1, ליעקב שוואקי הקש 2, לחנן בן ארי הקש 3, לישי ריבו הקש 4. לחזרה הקש 0", "1", "1", 10, "digits")
            
        else:
            return make_native_tts_command("לתפריט חיפוש קולי הקש 1, לשירים חדשים ועדכניים הקש 2, לחיפוש מהיר מובנה הקש 3", "1", "1", 10, "digits")

    # --- חיפוש קולי ---
    elif state == "WAITING_FOR_SEARCH":
        if ValName == "0":
            session["state"] = "MAIN_MENU"
            return make_native_tts_command("חוזר לתפריט הראשי", "1", "1", 3, "digits")
        if not ValName or ValName in ["1", "2", "*", "#"]:
            return make_native_tts_command("לא קלטתי את הדיבור שלכם אנא אמרו את שם השיר בבירור לאחר הצליל", "1", "50", 10, "voice")
        
        session["state"] = "PLAYING_SEARCH"
        session["playlist"] = fetch_youtube_ids(ValName, max_results=1, filter_newest=False)
        session["index"] = 0
        if session["playlist"]:
            video_id = session["playlist"][0]
            return get_final_play_command(video_id, request)
        else:
            session["state"] = "MAIN_MENU"
            return make_native_tts_command("לא נמצאו תוצאות חוזר לתפריט הראשי", "1", "1", 3, "digits")

    elif state == "PLAYING_SEARCH":
        session["state"] = "MAIN_MENU"
        return make_native_tts_command("ההשמעה הסתיימה חוזר לתפריט הראשי", "1", "1", 3, "digits")

    # --- שלוחה 3: תפריט חיפוש מהיר מובנה ---
    elif state == "PREDEFINED_ARTISTS":
        if ValName == "0":
            session["state"] = "MAIN_MENU"
            return make_native_tts_command("חוזר לתפריט הראשי", "1", "1", 3, "digits")
            
        artist_queries = {
            "1": "עומר אדם",
            "2": "יעקב שוואקי",
            "3": "חנן בן ארי",
            "4": "ישי ריבו"
        }
        
        if ValName in artist_queries:
            query_text = artist_queries[ValName]
            results = fetch_youtube_ids(query_text, max_results=30, filter_newest=False)
            session["playlist"] = results
            session["state"] = "CHOOSE_TRACK_NUMBER"
            
            total_found = len(results)
            return make_native_tts_command(f"נמצאו {total_found} תוצאות. אנא הקש את מספר השיר המבוקש מאחד עד {total_found} ולאחריו סולמית.", "1", "2", 15, "digits")
        else:
            return make_native_tts_command("בחירה שגויה. לעומר אדם הקש 1, ליעקב שוואקי הקש 2, לחנן בן ארי הקש 3, לישי ריבו הקש 4.", "1", "1", 10, "digits")

    # --- בחירת מספר שיר ספציפי מתוך רשימת התוצאות ---
    elif state == "CHOOSE_TRACK_NUMBER":
        if ValName == "0":
            session["state"] = "MAIN_MENU"
            return make_native_tts_command("חוזר לתפריט הראשי", "1", "1", 3, "digits")
            
        playlist = session["playlist"]
        try:
            chosen_number = int(ValName)
            if 1 <= chosen_number <= len(playlist):
                session["index"] = chosen_number - 1
                session["state"] = "PLAYING_LATEST" 
                video_id = playlist[session["index"]]
                return get_final_play_command(video_id, request)
            else:
                return make_native_tts_command(f"מספר מחוץ לטווח. נא הקש מספר בין 1 ל-{len(playlist)}", "1", "2", 10, "digits")
        except ValueError:
            return make_native_tts_command("קלט לא תקין. אנא הקש מספר שיר תקין", "1", "2", 10, "digits")

    # --- נגן פלייליסט שירים חדשים / מובנים ---
    elif state == "PLAYING_LATEST":
        playlist = session["playlist"]
        idx = session["index"]

        # ניהול המקשים בשידור חי בזמן השמעת השיר!
        if ValName == "1":    # מקש 1 עובר קדימה
            idx += 1
        elif ValName == "2":  # מקש 2 חוזר אחורה
            idx -= 1
        elif ValName == "0":  # מקש 0 חוזר לתפריט הראשי
            session["state"] = "MAIN_MENU"
            return make_native_tts_command("חוזר לתפריט הראשי", "1", "1", 3, "digits")
        elif ValName == "" or ValName is None:  # השיר הסתיים לבד! עובר אוטומטית לשיר הבא
            idx += 1

        if idx >= len(playlist):
            idx = 0 
        elif idx < 0:
            idx = len(playlist) - 1 

        session["index"] = idx
        if playlist:
            video_id = playlist[idx]
            return get_final_play_command(video_id, request)
        return make_native_tts_command("חוזר לתפריט הראשי", "1", "1", 3, "digits")

    return "hangup"
