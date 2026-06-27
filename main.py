import io
from fastapi import FastAPI, Query, Request
from fastapi.responses import PlainTextResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
import requests
import yt_dlp
from gtts import gTTS

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
    """מחפש ביוטיוב ומחזיר רשימה של קישורי שמע ישירים"""
    ydl_opts = {
        'format': 'bestaudio/best',
        'default_search': f'ytsearch{max_results}',
        'quiet': True,
        'no_warnings': True,
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

@app.get("/tts/{hex_text}.mp3")
def text_to_speech(hex_text: str):
    """מייצר קובץ אודיו מהטקסט ומחזיר אותו עם סיומת mp3 מדומה שהמערכת אוהבת"""
    try:
        text = bytes.fromhex(hex_text).decode('utf-8')
    except Exception:
        text = "שגיאה"
    
    tts = gTTS(text=text, lang='he')
    fp = io.BytesIO()
    tts.write_to_fp(fp)
    fp.seek(0)
    return StreamingResponse(fp, media_type="audio/mpeg")

@app.get("/stream_media/{phone}/{index}.mp3")
def stream_media(phone: str, index: int):
    """מזרים את האודיו של יוטיוב ישירות לימות המשיח כקובץ mp3 נקי ללא הפניות"""
    try:
        if phone in db_sessions and db_sessions[phone]["playlist"]:
            playlist = db_sessions[phone]["playlist"]
            idx = int(index)
            if 0 <= idx < len(playlist):
                video_url = playlist[idx]
                
                # פונקציה פנימית שמזרימה את השיר בחלקים קטנים כדי לא להעמיס על זיכרון השרת
                def generate_chunks():
                    with requests.get(video_url, stream=True) as r:
                        r.raise_for_status()
                        for chunk in r.iter_content(chunk_size=65536):
                            if chunk:
                                yield chunk
                                
                return StreamingResponse(generate_chunks(), media_type="audio/mpeg")
    except Exception as e:
        print(f"Error streaming media: {e}")
    
    return PlainTextResponse("No media found")

def make_tts_command(request: Request, text: str, min_dig: int, max_dig: int, sec: int, type_mode: str) -> str:
    """מייצר פקודת קריאה תואמת לימות המשיח עם תחילית f- וסיומת mp3 קבועה"""
    base_url = str(request.base_url).rstrip('/')
    # תיקון פרוטוקול ל-https בשרתי Render
    if "onrender.com" in base_url and base_url.startswith("http://"):
        base_url = base_url.replace("http://", "https://")
        
    hex_text = text.encode('utf-8').hex()
    audio_url = f"{base_url}/tts/{hex_text}.mp3"
    return f"read=f-{audio_url}=ValName={min_dig}={max_dig}={sec}={type_mode.lower()}"

@app.get("/youtube", response_class=PlainTextResponse)
def handle_ivr(
    request: Request,
    ApiPhone: str = Query(None),
    ValName: str = Query(None),
    hangup: str = Query(None)
):
    if hangup == "yes" or not ApiPhone:
        return "OK"

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
                if ValName is not None: 
                    return make_tts_command(request, "קוד שגוי. אנא נסה שנית", 4, 4, 10, "digits")
                return make_tts_command(request, "אנא הקש את קוד הגישה בן ארבע הספרות", 4, 4, 10, "digits")

    state = session["state"]

    # --- תפריט ראשי ---
    if state == "MAIN_MENU":
        if ValName == "1":
            session["state"] = "WAITING_FOR_SEARCH"
            return make_tts_command(request, "אנא אמרו את שם השיר או השיעור המבוקש ונחפש אותו ביוטיוב", 1, 1, 10, "voice")
        
        elif ValName == "2":
            session["state"] = "PLAYING_LATEST"
            session["playlist"] = fetch_youtube_urls("שירים חדשים", max_results=7)
            session["index"] = 0
            if not session["playlist"]:
                session["state"] = "MAIN_MENU"
                return make_tts_command(request, "שגיאה בטעינת השירים. חוזר לתפריט הראשי", 0, 0, 3, "digits")
            
            clean_media_url = f"{base_url}/stream_media/{ApiPhone}/0.mp3"
            return f"read=f-{clean_media_url}=ValName=1=1=3=digits"

        else:
            return make_tts_command(request, "לתפריט חיפוש קולי הקש 1. לשירים חדשים ועדכניים הקש 2.", 1, 1, 10, "digits")

    # --- עיבוד תוצאת חיפוש קולי ---
    elif state == "WAITING_FOR_SEARCH":
        if not ValName:
            session["state"] = "MAIN_MENU"
            return make_tts_command(request, "לא התקבל קלט. חוזר לתפריט הראשי", 0, 0, 3, "digits")
        
        urls = fetch_youtube_urls(ValName, max_results=1)
        if urls:
            session["state"] = "PLAYING_SEARCH"
            session["playlist"] = urls
            session["index"] = 0
            clean_media_url = f"{base_url}/stream_media/{ApiPhone}/0.mp3"
            return f"read=f-{clean_media_url}=ValName=1=1=3=digits"
        else:
            session["state"] = "MAIN_MENU"
            return make_tts_command(request, "לא נמצאו תוצאות. חוזר לתפריט הראשי", 0, 0, 3, "digits")

    # --- שליטה בזמן השמעת חיפוש ---
    elif state == "PLAYING_SEARCH":
        session["state"] = "MAIN_MENU"
        return make_tts_command(request, "ההשמעה הסתיימה. חוזר לתפריט הראשי", 0, 0, 3, "digits")

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
            return make_tts_command(request, "חוזר לתפריט הראשי", 0, 0, 3, "digits")

        if idx >= len(playlist):
            session["state"] = "MAIN_MENU"
            return make_tts_command(request, "הגעת לסוף הפלייליסט. חוזר לתפריט הראשי", 0, 0, 3, "digits")
        elif idx < 0:
            idx = 0 

        session["index"] = idx
        clean_media_url = f"{base_url}/stream_media/{ApiPhone}/{idx}.mp3"
        return f"read=f-{clean_media_url}=ValName=1=1=3=digits"

    return "hangup"
