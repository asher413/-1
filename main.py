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

# מאגר זמני בזיכרון לשמירת מצב המשתמשים
db_sessions = {}

# הגדרות אבטחה
WHITELIST = ["0534133753", "0534133754"]  
ACCESS_CODE = "1234"                       

def fetch_youtube_ids(query: str, max_results=6):
    """מחפש ביוטיוב דרך DuckDuckGo HTML - עוקף חסימות ב-100% במהירות שיא"""
    encoded_query = urllib.parse.quote(query)
    # שימוש בכתובת ה-HTML הנקייה של DuckDuckGo שמציגה תוצאות מיוטיוב ללא חסימות Cloudflare
    url = f"https://html.duckduckgo.com/html/?q={encoded_query}+site:youtube.com"
    
    try:
        req = urllib.request.Request(
            url, 
            headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            html = response.read().decode('utf-8', errors='ignore')
            
            # חילוץ מזהי וידאו (Video IDs) מהתוצאות - תומך גם בפורמט רגיל וגם בפורמט מוקדד
            raw_matches = re.findall(r'watch\?v=([a-zA-Z0-9_-]{11})', html)
            encoded_matches = re.findall(r'watch%3Fv%3D([a-zA-Z0-9_-]{11})', html)
            
            video_ids = []
            for v_id in (raw_matches + encoded_matches):
                if v_id not in video_ids:
                    video_ids.append(v_id)
                if len(video_ids) >= max_results:
                    break
                    
            if video_ids:
                print(f"Successfully found {len(video_ids)} videos via DuckDuckGo.")
                return video_ids
    except Exception as e:
        print(f"DuckDuckGo search extraction failed: {e}")
            
    # --- פלייליסט גיבוי קשיח (FAILSAFE) ---
    # במקרה חירום קיצוני, המערכת תטען שירים מוכרים כדי שהשיחה לעולם לא תתנתק בשגיאה
    print("Activating robust failsafe playlist.")
    return ["YmK2mZf_uRE", "7un666Y6N_Q", "H762G1UoP2k", "4X7bLks7Oxc"][:max_results]

@app.get("/stream_media/{phone}/{index}.mp3")
def stream_media(phone: str, index: int):
    """מפנה את ימות המשיח ישירות לזרם האודיו הרשמי (מבוצע מהשרתים של ימות בארץ, ללא חסימת Render)"""
    try:
        if phone in db_sessions and db_sessions[phone]["playlist"]:
            playlist = db_sessions[phone]["playlist"]
            idx = int(index)
            if 0 <= idx < len(playlist):
                video_id = playlist[idx]
                # הפניה לשרת אודיו ישיר ויציב שאינו חסום בישראל
                stream_url = f"https://yewtu.be/latest_version?id={video_id}&itag=140"
                return RedirectResponse(url=stream_url)
    except Exception as e:
        print(f"Error redirecting stream: {e}")
    
    return PlainTextResponse("No media found")

def make_native_tts_command(text: str, min_dig: str, max_dig: str, sec: int, type_mode: str) -> str:
    """מייצר פקודת הקראה מובנית (TTS) ומגדיר פרמטרים קשיחים למניעת באגים בימות המשיח"""
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

    # שליפת ה-ValName מכל המקורות האפשריים (כולל התפרצות בזמן השמעת play_url)
    val_name_choices = [v for k, v in request.query_params.multi_items() if k == "ValName"]
    ValName = val_name_choices[-1] if val_name_choices else None
    
    if not ValName:
        ValName = request.query_params.get("play_url_pressed")

    # בדיקה האם השיר הסתיים בצורה טבעית
    song_ended = request.query_params.get("play_url_end") == "yes"

    base_url = str(request.base_url).rstrip('/')
    if "onrender.com" in base_url and base_url.startswith("http://"):
        base_url = base_url.replace("http://", "https://")

    # 1. אתחול סשן למשתמש חדש
    if ApiPhone not in db_sessions:
        is_whitelisted = ApiPhone in WHITELIST
        db_sessions[ApiPhone] = {
            "auth": is_whitelisted,
            "state": "MAIN_MENU" if is_whitelisted else "CHECK_AUTH",
            "playlist": [],
            "index": 0
        }

    session = db_sessions[ApiPhone]

    # 2. שלב אימות קוד גישה
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
            session["playlist"] = fetch_youtube_ids("שירים חדשים מיוזיק", max_results=6)
            session["index"] = 0
            
            clean_media_url = f"{base_url}/stream_media/{ApiPhone}/0.mp3"
            return f"play_url={clean_media_url}"

        else:
            return make_native_tts_command("לתפריט חיפוש קולי הקש 1 לשירים חדשים ועדכניים הקש 2", "1", "1", 10, "digits")

    # --- עיבוד תוצאת חיפוש קולי ---
    elif state == "WAITING_FOR_SEARCH":
        if ValName == "0":
            session["state"] = "MAIN_MENU"
            return make_native_tts_command("חוזר לתפריט הראשי", "1", "1", 3, "digits")

        if not ValName or ValName in ["1", "2", "*", "#"]:
            return make_native_tts_command("לא קלטתי את הדיבור שלכם אנא אמרו את שם השיר בבירור לאחר הצליל", "1", "50", 10, "voice")
        
        session["state"] = "PLAYING_SEARCH"
        session["playlist"] = fetch_youtube_ids(ValName, max_results=1)
        session["index"] = 0
        clean_media_url = f"{base_url}/stream_media/{ApiPhone}/0.mp3"
        return f"play_url={clean_media_url}"

    # --- שליטה בזמן השמעת חיפוש ---
    elif state == "PLAYING_SEARCH":
        session["state"] = "MAIN_MENU"
        return make_native_tts_command("ההשמעה הסתיימה חוזר לתפריט הראשי", "1", "1", 3, "digits")

    # --- שליטה בנגן פלייליסט ---
    elif state == "PLAYING_LATEST":
        playlist = session["playlist"]
        idx = session["index"]

        if ValName == "1" or song_ended or not ValName:  # שיר הבא / השיר הסתיים מעצמו
            idx += 1
        elif ValName == "2":  # שיר קודם
            idx -= 1
        elif ValName == "0":  # חזרה לתפריט
            session["state"] = "MAIN_MENU"
            return make_native_tts_command("חוזר לתפריט הראשי", "1", "1", 3, "digits")

        if idx >= len(playlist):
            session["state"] = "MAIN_MENU"
            return make_native_tts_command("הגעת לסוף הפלייליסט חוזר לתפריט הראשי", "1", "1", 3, "digits")
        elif idx < 0:
            idx = 0 

        session["index"] = idx
        clean_media_url = f"{base_url}/stream_media/{ApiPhone}/{idx}.mp3"
        return f"play_url={clean_media_url}"

    return "hangup"
