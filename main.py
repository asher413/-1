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
    """מחפש ביוטיוב דרך שרתים מבוזרים ופתוחים עם גיבוי פלייליסט קשיח שלא יכול להיכשל"""
    encoded_query = urllib.parse.quote(query)
    
    # תערובת מגוונת של שרתי Piped ו-Invidious ללא הגנות חוסמות
    urls = [
        f"https://pipedapi.kavin.rocks/search?q={encoded_query}&filter=videos",
        f"https://inv.nadeko.net/api/v1/search?q={encoded_query}&type=video",
        f"https://pipedapi.tokhmi.xyz/search?q={encoded_query}&filter=videos",
        f"https://invidious.flokinet.to/api/v1/search?q={encoded_query}&type=video",
        f"https://iv.melmac.space/api/v1/search?q={encoded_query}&type=video"
    ]
    
    for url in urls:
        try:
            req = urllib.request.Request(
                url, 
                headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
            )
            with urllib.request.urlopen(req, timeout=5) as response:
                res_body = response.read().decode('utf-8', errors='ignore')
                data = json.loads(res_body)
                video_ids = []
                
                # תמיכה במבנה של שרתי Invidious (מערך שטוח)
                if isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict) and 'videoId' in item:
                            video_ids.append(item['videoId'])
                
                # תמיכה במבנה של שרתי Piped (מילון עם אובייקט items)
                elif isinstance(data, dict) and 'items' in data:
                    for item in data['items']:
                        if isinstance(item, dict):
                            if 'videoId' in item:
                                video_ids.append(item['videoId'])
                            elif 'url' in item and '/watch?v=' in item['url']:
                                v_id = item['url'].split('/watch?v=')[-1].split('&')[0]
                                video_ids.append(v_id)
                                
                if video_ids:
                    # ניקוי כפילויות ושמירה על הסדר המקורי
                    seen = set()
                    unique_ids = [x for x in video_ids if not (x in seen or seen.add(x))]
                    return unique_ids[:max_results]
        except Exception as e:
            print(f"Failed instance {url.split('/')[2]}: {e}")
            continue
            
    # --- פלייליסט גיבוי קשיח (FAILSAFE) ---
    # אם כל השרתים נכשלו, המערכת תטען את השירים האלו כדי שלעולם לא תהיה קריסה בשיחה
    print("All search instances failed or blocked. Activating robust failsafe playlist.")
    failsafe_playlist = [
        "rcOwvZ26KFQ",  # שיר 1 מהלוגים המקוריים שלך
        "eaqW5eQXTdM",  # שיר 2 מהלוגים המקוריים שלך
        "YmK2mZf_uRE",  # ישי ריבו - סיבת הסיבות
        "7un666Y6N_Q",  # חנן בן ארי - חנניה
        "H762G1UoP2k",  # מרדכי שפירא
        "4X7bLks7Oxc"   # יעקב שוואקי
    ]
    return failsafe_playlist[:max_results]

@app.get("/stream_media/{phone}/{index}.mp3")
def stream_media(phone: str, index: int):
    """מפנה את ימות המשיח ישירות להזרמת המדיה (ההפניה מתבצעת במכשיר הקצה ולא חסומה)"""
    try:
        if phone in db_sessions and db_sessions[phone]["playlist"]:
            playlist = db_sessions[phone]["playlist"]
            idx = int(index)
            if 0 <= idx < len(playlist):
                video_id = playlist[idx]
                # שרת הפצה יציב להזרמת סאונד ישירות לטלפון
                stream_url = f"https://inv.nadeko.net/latest_version?id={video_id}&itag=140"
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
            session["playlist"] = fetch_youtube_ids("שירים חדשים מיוזיק", max_results=6)
            session["index"] = 0
            
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
        
        session["state"] = "PLAYING_SEARCH"
        session["playlist"] = fetch_youtube_ids(ValName, max_results=1)
        session["index"] = 0
        clean_media_url = f"{base_url}/stream_media/{ApiPhone}/0.mp3"
        return f"read=f-{clean_media_url}=ValName,no,1,1,3,digits,no"

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
