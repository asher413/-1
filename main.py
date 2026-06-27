import io
from fastapi import FastAPI, Query, Request
from fastapi.responses import PlainTextResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
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

# הגדרות אבטחה (נשמר בדיוק מהמקור שלך)
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

@app.get("/tts/{hex_text}")
def text_to_speech(hex_text: str):
    """נתיב פנימי שמייצר קובץ אודיו מהטקסט המוצפן ומחזיר אותו לימות המשיח כנגן"""
    try:
        text = bytes.fromhex(hex_text).decode('utf-8')
    except Exception:
        text = "שגיאה"
    
    tts = gTTS(text=text, lang='he')
    fp = io.BytesIO()
    tts.write_to_fp(fp)
    fp.seek(0)
    return StreamingResponse(fp, media_type="audio/mpeg")

def make_tts_command(request: Request, text: str, min_dig: int, max_dig: int, sec: int, type_mode: str) -> str:
    """פונקציית עזר שמייצרת פקודת קריאה תואמת לימות המשיח ללא שימוש ב-TTS שלהם"""
    base_url = str(request.base_url).rstrip('/')
    hex_text = text.encode('utf-8').hex()
    audio_url = f"{base_url}/tts/{hex_text}"
    return f"read=t-{audio_url}=ValName={min_dig}={max_dig}={sec}={type_mode}"

@app.get("/youtube", response_class=PlainTextResponse)
def handle_ivr(
    request: Request,
    ApiPhone: str = Query(None),
    ValName: str = Query(None),  # הקלט של המשתמש
    hangup: str = Query(None)    # מונע קריסות וניתוקים מיידיים בסגירת שיחה
):
    # טיפול בניתוק שיחה או חוסר במספר טלפון
    if hangup == "yes" or not ApiPhone:
        return "OK"

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

    # 2. שלב אימות קוד גישה (למי שלא ברשימה הלבנה)
    if not session["auth"]:
        if session["state"] == "CHECK_AUTH":
            if ValName == ACCESS_CODE:
                session["auth"] = True
                session["state"] = "MAIN_MENU"
                # קוד נכון -> ממשיך ישירות ללוגיקת התפריט הראשי בהמשך הריצה
            else:
                if ValName is not None: # אם הקיש קוד שגוי
                    return make_tts_command(request, "קוד שגוי. אנא נסה שנית", 4, 4, 10, "digits")
                # בקשת קוד ראשונית
                return make_tts_command(request, "אנא הקש את קוד הגישה בן ארבע הספרות", 4, 4, 10, "digits")

    # 3. ניהול המצבים (מכונת מצבים)
    state = session["state"]

    # --- תפריט ראשי ---
    if state == "MAIN_MENU":
        if ValName == "1":
            session["state"] = "WAITING_FOR_SEARCH"
            return make_tts_command(request, "אנא אמרו את שם השיר או השיעור המבוקש", 1, 1, 10, "voice")
        
        elif ValName == "2":
            session["state"] = "PLAYING_LATEST"
            session["playlist"] = fetch_youtube_urls("שירים חדשים 2026", max_results=7)
            session["index"] = 0
            if not session["playlist"]:
                session["state"] = "MAIN_MENU"
                return make_tts_command(request, "שגיאה בטעינת השירים. חוזר לתפריט הראשי", 0, 0, 3, "digits")
            
            # השמעת השיר הראשון בפלייליסט
            current_url = session["playlist"][0]
            return f"read=t-{current_url}=ValName=1=1=3=digits"

        else:
            # השמעת תפריט ראשי
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
            return f"read=t-{urls[0]}=ValName=1=1=3=digits"
        else:
            session["state"] = "MAIN_MENU"
            return make_tts_command(request, "לא נמצאו תוצאות. חוזר לתפריט הראשי", 0, 0, 3, "digits")

    # --- שליטה בזמן השמעת חיפוש ---
    elif state == "PLAYING_SEARCH":
        session["state"] = "MAIN_MENU"
        return make_tts_command(request, "חוזר לתפריט הראשי", 0, 0, 3, "digits")

    # --- שליטה בנגן פלייליסט (שירים חדשים) ---
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
        current_url = playlist[idx]
        return f"read=t-{current_url}=ValName=1=1=3=digits"

    return "hangup"
