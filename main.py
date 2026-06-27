import io
from fastapi import FastAPI, Query, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
import yt_dlp

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

def fetch_youtube_urls(query: str, max_results=5):
    """מחפש ביוטיוב תוך התחזות למכשיר אנדרואיד כדי לעקוף חסימות בוטים של חוות שרתים"""
    ydl_opts = {
        'format': 'bestaudio/best',
        'default_search': f'ytsearch{max_results}',
        'quiet': True,
        'no_warnings': True,
        # מעקף חסימת בוטים - התחזות ללקוח אנדרואיד
        'extractor_args': {
            'youtube': {
                'player_client': ['android']
            }
        }
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(query, download=False)
            urls = []
            if 'entries' in info:
                for entry in info['entries']:
                    if entry and 'url' in entry:
                        urls.append(entry['url'])
            return urls
        except Exception as e:
            print(f"Error fetching YouTube: {e}")
            return []

@app.get("/stream_media/{phone}/{index}.mp3")
def stream_media(phone: str, index: int):
    """מפנה את ימות המשיח ישירות לשרתי גוגל/יוטיוב בצורה אופטימלית ויציבה"""
    try:
        if phone in db_sessions and db_sessions[phone]["playlist"]:
            playlist = db_sessions[phone]["playlist"]
            idx = int(index)
            if 0 <= idx < len(playlist):
                return RedirectResponse(url=playlist[idx])
    except Exception as e:
        print(f"Error redirecting stream: {e}")
    
    return PlainTextResponse("No media found")

def make_native_tts_command(text: str, min_dig: str, max_dig: str, sec: int, type_mode: str) -> str:
    """מייצר פקודת הקראה מובנית (TTS) ומטפל בצורה נכונה במצב קולי ללא הגבלת ספרות"""
    clean_text = text.replace("=", "").replace(",", "").replace("-", "")
    
    # חוק ברזל בימות המשיח: במצב קולי (voice) אסור לשלוח מינימום ומקסימום ספרות!
    if type_mode.lower() == "voice":
        return f"read=t-{clean_text}=ValName,no,,,{sec},voice,no"
    
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

    # פתרון גאוני לבאג השרשור של ימות המשיח: שליפת ה-ValName האחרון בלבד מתוך ה-URL
    val_name_choices = [v for k, v in request.query_params.multi_items() if k == "ValName"]
    ValName = val_name_choices[-1] if val_name_choices else None

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
            return make_native_tts_command("אנא אמרו בצורה ברורה את שם השיר או השיעור המבוקש", "", "", 10, "voice")
        
        elif ValName == "2":
            session["state"] = "PLAYING_LATEST"
            session["playlist"] = fetch_youtube_urls("שירים חדשים", max_results=7)
            session["index"] = 0
            if not session["playlist"]:
                session["state"] = "MAIN_MENU"
                return make_native_tts_command("יוטיוב חסם זמנית את הבקשה חוזר לתפריט הראשי", "1", "1", 3, "digits")
            
            clean_media_url = f"{base_url}/stream_media/{ApiPhone}/0.mp3"
            return f"read=f-{clean_media_url}=ValName,no,1,1,3,digits,no"

        else:
            return make_native_tts_command("לתפריט חיפוש קולי הקש 1 לשירים חדשים ועדכניים הקש 2", "1", "1", 10, "digits")

    # --- עיבוד תוצאת חיפוש קולי ---
    elif state == "WAITING_FOR_SEARCH":
        if not ValName:
            session["state"] = "MAIN_MENU"
            return make_native_tts_command("לא התקבל קלט קולי חוזר לתפריט הראשי", "1", "1", 3, "digits")
        
        urls = fetch_youtube_urls(ValName, max_results=1)
        if urls:
            session["state"] = "PLAYING_SEARCH"
            session["playlist"] = urls
            session["index"] = 0
            clean_media_url = f"{base_url}/stream_media/{ApiPhone}/0.mp3"
            return f"read=f-{clean_media_url}=ValName,no,1,1,3,digits,no"
        else:
            session["state"] = "MAIN_MENU"
            return make_native_tts_command("לא נמצאו תוצאות ביוטיוב חוזר לתפריט הראשי", "1", "1", 3, "digits")

    # --- שליטה בזמן השמעת חיפוש ---
    elif state == "PLAYING_SEARCH":
        session["state"] = "MAIN_MENU"
        return make_native_tts_command("ההשמעה הסתיימה חוזר לתפריט הראשי", "1", "1", 3, "digits")

    # --- שליטה בנגן פלייליסט ---
    elif state == "PLAYING_LATEST":
        playlist = session["playlist"]
        idx = session["index"]

        if ValName == "1":  # שיר הבא
            idx += 1
        elif ValName == "2":  # שיר קודם
            idx -= 1
        elif ValName == "0":  # חזרה לתפריט
            session["state"] = "MAIN_MENU"
            return make_native_tts_command("חוזר לתפריט הראשי", "1", "1", 3, "digits")
        elif not ValName:  
            idx += 1

        if idx >= len(playlist):
            session["state"] = "MAIN_MENU"
            return make_native_tts_command("הגעת לסוף הפלייליסט חוזר לתפריט הראשי", "1", "1", 3, "digits")
        elif idx < 0:
            idx = 0 

        session["index"] = idx
        clean_media_url = f"{base_url}/stream_media/{ApiPhone}/{idx}.mp3"
        return f"read=f-{clean_media_url}=ValName,no,1,1,3,digits,no"

    return "hangup"
