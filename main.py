import io
import json
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

def fetch_youtube_ids(query: str, max_results=5):
    """מחפש ביוטיוב דרך שרתי API פתוחים ומבוזרים - עוקף חסימות ב-100% ללא yt-dlp"""
    encoded_query = urllib.parse.quote(query)
    
    # רשימת שרתי פרוקסי ציבוריים יציבים לגיבוי הדדי
    instances = [
        f"https://yewtu.be/api/v1/search?q={encoded_query}&type=video",
        f"https://vid.puffyan.us/api/v1/search?q={encoded_query}&type=video",
        f"https://invidious.nerdvpn.de/api/v1/search?q={encoded_query}&type=video"
    ]
    
    for url in instances:
        try:
            req = urllib.request.Request(
                url, 
                headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
            )
            with urllib.request.urlopen(req, timeout=4) as response:
                data = json.loads(response.read().decode())
                video_ids = []
                for item in data:
                    if isinstance(item, dict) and 'videoId' in item:
                        video_ids.append(item['videoId'])
                    if len(video_ids) >= max_results:
                        break
                if video_ids:
                    return video_ids
        except Exception as e:
            print(f"Failed searching on instance {url}: {e}")
            continue # מעבר לשרת הבא ברשימה במקרה של שגיאה
            
    return []

@app.get("/stream_media/{phone}/{index}.mp3")
def stream_media(phone: str, index: int):
    """מפנה את ימות המשיח ישירות לזרם האודיו של הפרוקסי הציבורי בפורמט קל ונתמך"""
    try:
        if phone in db_sessions and db_sessions[phone]["playlist"]:
            playlist = db_sessions[phone]["playlist"]
            idx = int(index)
            if 0 <= idx < len(playlist):
                video_id = playlist[idx]
                # itag=140 מייצג קובץ אודיו בלבד מסוג M4A/AAC שמתנגן מעולה בטלפון
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

    # שליפת ה-ValName האחרון בלבד מתוך ה-URL למניעת באג שרשור המקשים
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
            return make_native_tts_command("אנא אמרו את שם השיר או השיעור המבוקש לאחר הצליל", "1", "50", 10, "voice")
        
        elif ValName == "2":
            session["state"] = "PLAYING_LATEST"
            session["playlist"] = fetch_youtube_ids("שירים חדשים מיוזיק", max_results=7)
            session["index"] = 0
            if not session["playlist"]:
                session["state"] = "MAIN_MENU"
                return make_native_tts_command("לא נמצאו שירים אנא נסו שנית מאוחר יותר חוזר לתפריט הראשי", "1", "1", 3, "digits")
            
            clean_media_url = f"{base_url}/stream_media/{ApiPhone}/0.mp3"
            return f"read=f-{clean_media_url}=ValName,no,1,1,3,digits,no"

        else:
            return make_native_tts_command("לתפריט חיפוש קולי הקש 1 לשירים חדשים ועדכניים הקש 2", "1", "1", 10, "digits")

    # --- עיבוד תוצאת חיפוש קולי ---
    elif state == "WAITING_FOR_SEARCH":
        if ValName == "0":
            session["state"] = "MAIN_MENU"
            return make_native_tts_command("חוזר לתפריט הראשי", "1", "1", 3, "digits")

        if not ValName or ValName in ["1", "2", "*", "#"]:
            return make_native_tts_command("לא קלטתי את הדיבור שלכם אנא אמרו את שם השיר בבירור לאחר הצליל", "1", "50", 10, "voice")
        
        urls = fetch_youtube_ids(ValName, max_results=1)
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
