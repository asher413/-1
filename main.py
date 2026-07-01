import io
import json
import re
import urllib.request
import urllib.parse
from fastapi import FastAPI, Query, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
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
# 🔑 מפתח ה-API וההגדרות המעודכנות מהתמונה שלך:
# ====================================================================
RAPIDAPI_KEY = "b356e0c424msh95c209990ea7472p1fe240jsn2a029b5480bf"
RAPIDAPI_HOST = "youtube-mp3-audio-video-downloader.p.rapidapi.com"
# ====================================================================

db_sessions = {}
WHITELIST = ["0534133753", "0534133754"]  
ACCESS_CODE = "1234"                       

def fetch_youtube_ids(query: str, max_results=30, filter_newest=False):
    """שולף מזהי וידאו מיוטיוב בצורה מהירה במיוחד דרך שרתי API חלופיים כדי למנוע חסימות ועיכובים"""
    if filter_newest:
        query += " חדש 2026"
    encoded_query = urllib.parse.quote(query)
    
    # שימוש בשרתים מבוזרים כדי להחזיר תשובה מיידית (פחות מ-500ms) ולמנוע ניתוקים בימות המשיח
    piped_instances = [
        f"https://pipedapi.kavin.rocks/search?q={encoded_query}&filter=videos",
        f"https://api.piped.yt/search?q={encoded_query}&filter=videos",
        f"https://pipedapi.moomoo.me/search?q={encoded_query}&filter=videos"
    ]
    
    for url in piped_instances:
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=3) as response:
                data = json.loads(response.read().decode('utf-8'))
                video_ids = []
                items = data.get("items", [])
                for item in items:
                    if item.get("type") == "stream":
                        video_id = item.get("id")
                        if video_id and len(video_id) == 11 and video_id not in video_ids:
                            video_ids.append(video_id)
                    if len(video_ids) >= max_results:
                        break
                if video_ids:
                    print(f"Successfully fetched {len(video_ids)} IDs from Piped API.")
                    return video_ids
        except Exception as e:
            print(f"Piped instance failed ({url.split('/')[2]}): {e}")
            continue
            
    # פלייליסט גיבוי מהיר למקרה שכל הרשתות עמוסות
    return ["YmK2mZf_uRE", "7un666Y6N_Q", "H762G1UoP2k", "4X7bLks7Oxc"][:max_results]

@app.get("/stream_media/{phone}/{index}.mp3")
def stream_media(phone: str, index: int):
    """פונה ל-RapidAPI החדש מהתמונה באמצעות הנתיב המדויק ומחזיר קישור הזרמה ישיר"""
    try:
        if phone in db_sessions and db_sessions[phone]["playlist"]:
            playlist = db_sessions[phone]["playlist"]
            idx = int(index)
            if 0 <= idx < len(playlist):
                video_id = playlist[idx]
                
                # התאמה לנתיב הרשמי של ה-API הספציפי שפתחת בתמונה (get-mp3-download-link)
                api_url = f"https://{RAPIDAPI_HOST}/get-mp3-download-link/{video_id}"
                req = urllib.request.Request(api_url)
                req.add_header("x-rapidapi-key", RAPIDAPI_KEY)
                req.add_header("x-rapidapi-host", RAPIDAPI_HOST)
                
                try:
                    with urllib.request.urlopen(req, timeout=5) as response:
                        res_data = json.loads(response.read().decode('utf-8'))
                        mp3_link = res_data.get("download_url") or res_data.get("link") or res_data.get("url")
                        if mp3_link:
                            return RedirectResponse(url=mp3_link)
                except Exception as e1:
                    print(f"First endpoint variant failed, trying underscore: {e1}")
                    # גיבוי בפורמט קו תחתון ליתר ביטחון
                    api_url = f"https://{RAPIDAPI_HOST}/get_mp3_download_link/{video_id}"
                    req = urllib.request.Request(api_url)
                    req.add_header("x-rapidapi-key", RAPIDAPI_KEY)
                    req.add_header("x-rapidapi-host", RAPIDAPI_HOST)
                    with urllib.request.urlopen(req, timeout=5) as response:
                        res_data = json.loads(response.read().decode('utf-8'))
                        mp3_link = res_data.get("download_url") or res_data.get("link") or res_data.get("url")
                        if mp3_link:
                            return RedirectResponse(url=mp3_link)
    except Exception as e:
        print(f"RapidAPI streaming link error: {e}")
    
    return RedirectResponse(url="https://www.soundhelix.com/examples/mp3/SoundHelix-Song-1.mp3")

def make_native_tts_command(text: str, min_dig: str, max_dig: str, sec: int, type_mode: str) -> str:
    clean_text = text.replace("=", "").replace(",", "").replace("-", "")
    if type_mode.lower() == "voice":
        return f"read=t-{clean_text}=ValName,no,50,1,{sec},voice,no"
    confirm_hash = "yes" if (max_dig and int(max_dig) > 1) else "no"
    return f"read=t-{clean_text}=ValName,no,{max_dig},{min_dig},{sec},{type_mode.lower()},{confirm_hash}"

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
    
    if not ValName:
        ValName = request.query_params.get("play_url_pressed")

    song_ended = request.query_params.get("play_url_end") == "yes"

    host = request.headers.get("x-forwarded-host", request.url.netloc)
    proto = request.headers.get("x-forwarded-proto", "https")
    base_url = f"{proto}://{host}"

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
            session["playlist"] = fetch_youtube_ids("שירים חדשים 2026 מוזיקה חסידית", max_results=30, filter_newest=True)
            session["index"] = 0
            clean_media_url = f"{base_url}/stream_media/{ApiPhone}/0.mp3"
            return f"play_url={clean_media_url}"
            
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
        clean_media_url = f"{base_url}/stream_media/{ApiPhone}/0.mp3"
        return f"play_url={clean_media_url}"

    elif state == "PLAYING_SEARCH":
        session["state"] = "MAIN_MENU"
        return make_native_tts_command("ההשמעה הסתיימה חוזר לתפריט הראשי", "1", "1", 3, "digits")

    # --- שלוחה 3: תפריט חיפוש מהיר מובנה ---
    elif state == "PREDEFINED_ARTISTS":
        if ValName == "0":
            session["state"] = "MAIN_MENU"
            return make_native_tts_command("חוזר לתפריט הראשי", "1", "1", 3, "digits")
            
        artist_queries = {
            "1": "עומר אדם כללי",
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
                clean_media_url = f"{base_url}/stream_media/{ApiPhone}/{session['index']}.mp3"
                return f"play_url={clean_media_url}"
            else:
                return make_native_tts_command(f"מספר מחוץ לטווח. נא הקש מספר בין 1 ל-{len(playlist)}", "1", "2", 10, "digits")
        except ValueError:
            return make_native_tts_command("קלט לא תקין. אנא הקש מספר שיר תקין", "1", "2", 10, "digits")

    # --- נגן פלייליסט שירים חדשים / מובנים ---
    elif state == "PLAYING_LATEST":
        playlist = session["playlist"]
        idx = session["index"]

        if ValName == "1" or song_ended or not ValName:
            idx += 1
        elif ValName == "2":
            idx -= 1
        elif ValName == "0":
            session["state"] = "MAIN_MENU"
            return make_native_tts_command("חוזר לתפריט הראשי", "1", "1", 3, "digits")

        if idx >= len(playlist):
            idx = 0 
        elif idx < 0:
            idx = len(playlist) - 1 

        session["index"] = idx
        clean_media_url = f"{base_url}/stream_media/{ApiPhone}/{idx}.mp3"
        return f"play_url={clean_media_url}"

    return "hangup"
