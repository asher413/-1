from fastapi import FastAPI, Query
from fastapi.responses import PlainTextResponse
import yt_dlp

from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# מאגר זמני בזיכרון לשמירת מצב המשתמשים (בייצור מומלץ להחליף ב-Redis או DB)
# מבנה: { "phone_number": { "auth": Bool, "state": Str, "playlist": List, "index": Int } }
db_sessions = {}

# הגדרות אבטחה
WHITELIST = ["0534133753", "0534133754"]  # הכנס כאן מספרי טלפון מאושרים מראש
ACCESS_CODE = "1234"                       # קוד הגישה למי שלא ברשימה הלבנה

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

@app.get("/youtube", response_class=PlainTextResponse)
def handle_ivr(
    ApiPhone: str = Query(None),
    ValName: str = Query(None)  # הקלט של המשתמש (מקשים או דיבור)
):
    if not ApiPhone:
        return "hangup"

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
                # קוד נכון -> ממשיכים מיד לתפריט הראשי
            else:
                # בקשת קוד מהמשתמש (מינימום 4 ספרות, מקסימום 4 ספרות)
                return "read=s-אנא הקש את קוד הגישה בן ארבע הספרות=no=4=4=10=digits="
        
        if not session["auth"]: # אם עדיין לא עבר אימות אחרי ההקשה
            return "read=s-קוד שגוי. אנא נסה שנית=no=4=4=10=digits="

    # 3. ניהול המצבים (מכונת מצבים)
    state = session["state"]

    # --- תפריט ראשי ---
    if state == "MAIN_MENU":
        if ValName == "1":
            session["state"] = "WAITING_FOR_SEARCH"
            return "read=s-אנא אמרו את שם השיר או השיעור המבוקש=no=1=1=10=voice="
        
        elif ValName == "2":
            session["state"] = "PLAYING_LATEST"
            # מחפש את השירים הכי חדשים (ניתן לשנות את מונח החיפוש לפי הצורך)
            session["playlist"] = fetch_youtube_urls("שירים חדשים 2026", max_results=7)
            session["index"] = 0
            if not session["playlist"]:
                session["state"] = "MAIN_MENU"
                return "read=s-שגיאה בטעינת השירים. חוזר לתפריט הראשי=no=0=0=7=digits="
            
            # מעבר להשמעת השיר הראשון
            current_url = session["playlist"][0]
            return f"read=t-{current_url}=no=1=1=3=digits="

        else:
            # השמעת תפריט ראשי
            return "read=s-לתפריט חיפוש קולי הקש 1. לשירים חדשים ועדכניים הקש 2.=no=1=1=10=digits="

    # --- עיבוד תוצאת חיפוש קולי ---
    elif state == "WAITING_FOR_SEARCH":
        if not ValName:
            session["state"] = "MAIN_MENU"
            return "read=s-לא התקבל קלט. חוזר לתפריט הראשי=no=0=0=3=digits="
        
        urls = fetch_youtube_urls(ValName, max_results=1)
        if urls:
            session["state"] = "PLAYING_SEARCH"
            session["playlist"] = urls
            session["index"] = 0
            # משמיע את השיר שנמצא. הקשה על מקש כלשהו תחזיר לתפריט
            return f"read=t-{urls[0]}=no=1=1=3=digits="
        else:
            session["state"] = "MAIN_MENU"
            return "read=s-לא נמצאו תוצאות. חוזר לתפריט הראשי=no=0=0=3=digits="

    # --- שליטה בזמן השמעת חיפוש ---
    elif state == "PLAYING_SEARCH":
        # כל מקש שהוקש בזמן השיר או בסופו יחזיר לתפריט הראשי
        session["state"] = "MAIN_MENU"
        return "read=s-חוזר לתפריט הראשי=no=0=0=3=digits="

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
            return "read=s-חוזר לתפריט הראשי=no=0=0=3=digits="

        # בדיקת גבולות הפלייליסט
        if idx >= len(playlist):
            session["state"] = "MAIN_MENU"
            return "read=s-הגעת לסוף הפלייליסט. חוזר לתפריט הראשי=no=0=0=3=digits="
        elif idx < 0:
            idx = 0 # מניעת ירידה מתחת ל-0

        session["index"] = idx
        current_url = playlist[idx]
        
        # השמעת השיר הנוכחי ומעקב אחרי מקשים (1 הבא, 2 קודם, 0 תפריט)
        return f"read=t-{current_url}=no=1=1=3=digits="

    return "hangup"