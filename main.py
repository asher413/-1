"""
Bulletproof IVR YouTube Engine 2026 — Hardened Edition
=======================================================
מנוע IVR חסין: הפעלת שירים מיוטיוב דרך מרכזיה טלפונית (IVR), עם תמיכה
בסליקת תשלומים (תרומה/מנוי) דרך ספק סליקה חיצוני.

איך להריץ (Render / כל שרת אחר):
  משתני סביבה חשובים שכדאי להגדיר:
    IVR_WHITELIST_PHONES=0534133753,0534133754     # טלפונים מורשים אוטומטית
    IVR_DEFAULT_ACCESS_CODE=1234                    # קוד גישה לכל השאר
    IVR_PUBLIC_BASE_URL=https://your-app.onrender.com
    IVR_REQUIRE_PUBLIC_BASE_URL=true
    RAPIDAPI_KEY=...                                 # אופציונלי
    CLEARING_API_URL / CLEARING_TERMINAL / CLEARING_API_KEY   # לסליקה, אופציונלי
    REDIS_URL=...                                    # אופציונלי, קאש משותף בין instances

⚠️ חשוב לגבי הרשימה הלבנה: אם IVR_WHITELIST_PHONES לא מוגדר, שום מספר אינו
מאושר מראש — כל מתקשר יתבקש להקיש קוד גישה (IVR_DEFAULT_ACCESS_CODE, ברירת
מחדל "1234" אם לא הוגדר, עם אזהרה בלוג). זו לא תקלה — זו התנהגות מכוונת כדי
שלא יהיה קוד/מספר "קסום" קשיח בקוד המקור. יש שתי דרכים לפתור את זה:
  1. הגדירו IVR_WHITELIST_PHONES עם המספרים שרוצים שיעברו בלי קוד.
  2. או פשוט הקישו את קוד הגישה כשהמערכת מבקשת אותו.
"""

import os
import re
import json
import copy
import time
import random
import secrets
import sqlite3
import logging
import asyncio
from enum import Enum
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Tuple

import httpx
from fastapi import FastAPI, Query, Request, HTTPException
from fastapi.responses import PlainTextResponse, StreamingResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from cachetools import TTLCache

# ==========================================
# 📋 לוגר
# ==========================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("IVR_Production_Engine")

# חשוב לאבטחה: httpx מדפיס ברירת מחדל את ה-URL המלא של כל בקשה ברמת INFO —
# כולל query params. אצל ימות המשיח זה חושף את הסיסמה בטקסט גלוי בלוגים
# (ראינו את זה בפועל: "Login?username=...&password=..."). מנמיכים ל-WARNING
# כדי שרק שגיאות אמיתיות של httpx יופיעו, לא כל בקשה עם הסודות שבתוכה.
logging.getLogger("httpx").setLevel(logging.WARNING)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ==========================================
# ⚙️ קונפיגורציה — הכל מ-ENV, שום סוד קשיח בקוד
# ==========================================
RAPIDAPI_KEY = os.environ.get("RAPIDAPI_KEY", "")  # ריק => המסלול הזה ידולג בשקט
RAPIDAPI_HOST = os.environ.get(
    "RAPIDAPI_HOST", "youtube-mp3-audio-video-downloader.p.rapidapi.com"
)
DB_PATH = os.environ.get("IVR_DB_PATH", "ivr_production.db")

_whitelist_env = os.environ.get("IVR_WHITELIST_PHONES", "")
DEFAULT_WHITELIST = [p.strip() for p in _whitelist_env.split(",") if p.strip()]

# קוד גישה לכל מי שלא ברשימה הלבנה. ברירת המחדל "1234" היא לנוחות התחלתית
# בלבד (כדי שהמערכת תהיה שמישה מהרגע הראשון) — יש להחליף אותה ב-production
# ע"י הגדרת IVR_DEFAULT_ACCESS_CODE, אחרת כל מי שמנחש "1234" ייכנס.
DEFAULT_ACCESS_CODE = os.environ.get("IVR_DEFAULT_ACCESS_CODE", "1234")
if os.environ.get("IVR_DEFAULT_ACCESS_CODE") is None:
    logger.warning(
        "IVR_DEFAULT_ACCESS_CODE not set! Using default access code: %s "
        "— set this env var explicitly in production so it's not guessable.",
        DEFAULT_ACCESS_CODE,
    )

if not DEFAULT_WHITELIST:
    logger.warning(
        "IVR_WHITELIST_PHONES not set — no phone numbers are pre-authorized. "
        "Every caller will be asked for the access code (%s). "
        "Set IVR_WHITELIST_PHONES via env var, comma separated, "
        "e.g. '0534133753,0534133754' to skip the code for specific numbers.",
        DEFAULT_ACCESS_CODE,
    )

# בסיס URL ציבורי — קריטי לבטיחות. אם לא מוגדר, נופלים חזרה על ה-Host header
# של הבקשה, שניתן לזיוף ע"י מי ששולח את הבקשה. ב-production מומלץ להגדיר גם
# IVR_PUBLIC_BASE_URL וגם IVR_REQUIRE_PUBLIC_BASE_URL=true.
PUBLIC_BASE_URL = os.environ.get("IVR_PUBLIC_BASE_URL", "").rstrip("/")
REQUIRE_PUBLIC_BASE_URL = os.environ.get("IVR_REQUIRE_PUBLIC_BASE_URL", "false").lower() == "true"

_trusted_hosts_env = os.environ.get("IVR_TRUSTED_HOSTS", "")
TRUSTED_HOSTS = {h.strip().lower() for h in _trusted_hosts_env.split(",") if h.strip()}

if REQUIRE_PUBLIC_BASE_URL and not PUBLIC_BASE_URL:
    raise RuntimeError(
        "IVR_REQUIRE_PUBLIC_BASE_URL=true אך IVR_PUBLIC_BASE_URL לא הוגדר. "
        "מסרבים לעלות: בניית כתובת ה-callback מ-Host header ניתנת לזיוף."
    )
if not PUBLIC_BASE_URL:
    logger.warning(
        "IVR_PUBLIC_BASE_URL לא הוגדר — ניפול חזרה על Host header של הבקשה, שניתן לזיוף. "
        "מומלץ מאוד להגדיר IVR_PUBLIC_BASE_URL (וגם IVR_REQUIRE_PUBLIC_BASE_URL=true) בפרודקשן."
    )

# תבנית פקודת ה-IVR להפעלת שיר בשיטה הישנה (URL חיצוני) — משמשת רק כ-fallback
# אם YEMOT_ENABLED=False או אם העלאה לימות נכשלה, למקרה שאתם על פלטפורמה
# אחרת שכן תומכת ב-URL חיצוני. אם בלוגים אין בקשות ל-/stream/... אחרי שהפקודה
# חוזרת, זה בגלל שימות המשיח לא תומכת בשיטה הזו כלל (מאומת מול הפורום שלהם) —
# הפתרון הוא YEMOT_SYSTEM_NUMBER+YEMOT_PASSWORD, לא שינוי בתבנית הזו.
PLAY_COMMAND_TEMPLATE = os.environ.get(
    "IVR_PLAY_COMMAND_TEMPLATE",
    "read={base}/stream/{video_id}.mp3=ValName,no,1,0,2,digits,no",
)

# תבנית פקודת ניגון עבור קובץ שכבר הועלה לימות המשיח (נתיב פנימי, לא URL).
# זו ה"השערה הכי סבירה" לפי תיעוד/דוגמאות קוד מהפורום הרשמי — לא אושרה
# ב-100% ודאות כי אין תיעוד רשמי חד-משמעי לתחביר הניגון בדיוק (רק להעלאה).
# מומלץ לבדוק פעם אחת ידנית ולהתאים דרך IVR_YEMOT_PLAY_TEMPLATE אם צריך.
YEMOT_PLAY_TEMPLATE = os.environ.get(
    "IVR_YEMOT_PLAY_TEMPLATE",
    "read={yemot_path}=ValName,no,1,0,2,digits,no",
)

RATE_LIMIT_PER_MINUTE = int(os.environ.get("IVR_RATE_LIMIT_PER_MINUTE", "20"))
SESSION_TTL_HOURS = int(os.environ.get("IVR_SESSION_TTL_HOURS", "4"))
MAX_PLAYLIST_SIZE = int(os.environ.get("IVR_MAX_PLAYLIST_SIZE", "15"))
SEARCH_RECURSION_DEPTH_LIMIT = 40
DB_READ_POOL_SIZE = max(1, int(os.environ.get("IVR_DB_READ_POOL_SIZE", "8")))

# הזרמת שמע: ברירת המחדל היא "buffered" — מורידים את כל קובץ ה-mp3 לזיכרון
# ואז שולחים תשובה רגילה (לא chunked) עם Content-Length מדויק. הרבה מערכות
# IVR/טלפוניה ישנות (כולל כאלה שמצפות לדעת מראש את גודל הקובץ) לא מפעילות
# בכלל נגן שמע כשהתשובה מגיעה ב-chunked transfer encoding בלי Content-Length —
# וזה בדיוק התסמין שראינו: /stream אף פעם לא נקרא בכלל אחרי שהפקודה הוחזרה.
# STREAM_MODE=passthrough מחזיר לסגנון הישן (הזרמה live, בלי buffer) אם תרצו.
STREAM_MODE = os.environ.get("IVR_STREAM_MODE", "buffered").lower()
MAX_STREAM_BYTES = int(os.environ.get("IVR_MAX_STREAM_BYTES", str(30 * 1024 * 1024)))  # 30MB ~ מספיק לשיר ארוך מאוד

# מספרים "חסויים" ששולחות מרכזיות שונות כשהמתקשר חסם הצגת מספר.
ANONYMOUS_PHONE_VALUES = {"0", "", "anonymous", "unknown", "withheld", "unavailable"}

# --- סליקת תשלומים (אופציונלי) -----------------------------------------
# אדפטר גנרי ל-REST endpoint של ספק סליקה (Cardcom / Tranzila / Yaad Sarig /
# PayMe וכו'). כל ספק דורש פורמט קצת שונה — מה שכאן הוא שלד עבודה מלא
# (רישום עסקה, retry-safe, לוגים, שמירת תוצאה) עם נקודת הרחבה יחידה
# (charge_customer) שיש להתאים לפי מסמכי ה-API של הספק שבחרתם בפועל.
CLEARING_API_URL = os.environ.get("CLEARING_API_URL", "")
CLEARING_TERMINAL = os.environ.get("CLEARING_TERMINAL", "")
CLEARING_API_KEY = os.environ.get("CLEARING_API_KEY", "")
CLEARING_ENABLED = bool(CLEARING_API_URL and CLEARING_API_KEY)
DONATION_MIN_ILS = float(os.environ.get("IVR_DONATION_MIN_ILS", "5"))
DONATION_MAX_ILS = float(os.environ.get("IVR_DONATION_MAX_ILS", "1000"))
if not CLEARING_ENABLED:
    logger.info(
        "Payment clearing not configured (CLEARING_API_URL/CLEARING_API_KEY missing) — "
        "donation menu option will be disabled."
    )

# --- ימות המשיח: העלאת קובץ מוקדמת (upload-first) --------------------------
# אושר במפורש בפורום המפתחים הרשמי של ימות המשיח: ניגון ישירות מ-URL חיצוני
# *אינו נתמך בכלל* — "השמעה בלי להעלות לא אפשרית". חובה להעלות את הקובץ
# למערכת ימות עם UploadFile ואז לנגן אותו לפי נתיב פנימי. זו הסיבה המדויקת
# ש-/stream/ מעולם לא נקרא בלוגים שלכם — לא היה שום באג בקוד, הפרוטוקול הזה
# פשוט לא קיים אצל ימות. בלי YEMOT_SYSTEM_NUMBER+YEMOT_PASSWORD, המערכת
# ממשיכה לנסות את שיטת ה-URL הישנה (למקרה שאתם על פלטפורמה אחרת בעתיד).
YEMOT_SYSTEM_NUMBER = os.environ.get("YEMOT_SYSTEM_NUMBER", "")
YEMOT_PASSWORD = os.environ.get("YEMOT_PASSWORD", "")
# API_KEY ייעודי (נוצר בפאנל הניהול של ימות, עם הרשאה מפורשת לשירות
# UploadFile) — אם מוגדר, משמש ישירות כ-token ומדלג לגמרי על Login עם
# username:password. קריטי לפי תיעוד: החל מנובמבר 2025 ימות עשויה לחסום
# את שיטת ה-Login המסורתית לפעולות מסוימות (כמו UploadFile) אלא אם הוגדר
# IP מאושר או נעשה שימוש ב-API_KEY. זה מסביר במדויק תסמין שראינו בפועל:
# Login ו-UpdateExtension הצליחו, אבל UploadFile ספציפית נכשל שוב ושוב.
YEMOT_API_KEY = os.environ.get("YEMOT_API_KEY", "")
YEMOT_ENABLED = bool(YEMOT_API_KEY or (YEMOT_SYSTEM_NUMBER and YEMOT_PASSWORD))
YEMOT_API_BASE = os.environ.get("YEMOT_API_BASE", "https://www.call2all.co.il/ym/api")
# נתיב ivr2 להעלאת שירים. לפי תיעוד רשמי של ימות: התחביר הנכון הוא
# ivr2:<תיקייה>/<קובץ> — בלי לוכסן אחרי הנקודתיים! (ivr2:5/000.wav, לא
# ivr2:/5/000.wav). בנוסף, תיקיות ב-ivr2 הן בפועל שלוחות (extensions) שצריך
# ליצור פעם אחת ידנית בפאנל הניהול של ימות (לא ניתן ליצור "תיקייה חדשה בשם
# חופשי" מה-API בלי שלוחה קיימת מתחתיה) — לכן ברירת המחדל כאן היא מספר
# שלוחה לדוגמה בלבד; יש להחליף ב-YEMOT_UPLOAD_FOLDER למספר שלוחה אמיתי
# שיצרתם מראש (למשל "90" אם פתחתם שלוחה 90 ל-mp3 של השירים).
YEMOT_UPLOAD_FOLDER = os.environ.get("YEMOT_UPLOAD_FOLDER", "90").strip().strip("/")
if YEMOT_UPLOAD_FOLDER and not YEMOT_UPLOAD_FOLDER.lower().startswith("ivr2:"):
    # קריטי: הדוגמה האמיתית והמאושרת ('עובד פצצה') מהפורום היא בדיוק
    # "ivr2:/5/ext.ini" — עם לוכסן מיד אחרי הנקודתיים. מנרמלים לאותו פורמט
    # בדיוק כדי שלא תצטרכו לזכור את זה בעצמכם.
    YEMOT_UPLOAD_FOLDER = f"ivr2:/{YEMOT_UPLOAD_FOLDER}"

# מחיקה אוטומטית של השיר מימות המשיח ברגע שהמתקשר יוצא מהקו (hangup) —
# חוסך מקום אחסון בחשבון ימות שלכם, במחיר איבוד קאש חוצה-משתמשים (אם שני
# מתקשרים שונים מבקשים את אותו שיר, הוא יורד+יועלה מחדש בכל פעם, במקום
# פעם אחת ולתמיד). ברירת מחדל: מופעל, לפי בקשה מפורשת. אפשר לכבות עם
# YEMOT_AUTO_DELETE_AFTER_PLAY=false אם עדיפה לכם החיסכון בזמן על פני החיסכון
# באחסון.
YEMOT_AUTO_DELETE_AFTER_PLAY = os.environ.get("YEMOT_AUTO_DELETE_AFTER_PLAY", "true").lower() == "true"
if not YEMOT_ENABLED:
    logger.info(
        "YEMOT_SYSTEM_NUMBER/YEMOT_PASSWORD (or YEMOT_API_KEY) not set — playback will use the "
        "direct-URL method, which Yemot Hamashiach is confirmed NOT to support. "
        "Set these env vars to enable the upload-first flow that actually works on Yemot."
    )
elif YEMOT_API_KEY:
    logger.info("Yemot auth: using YEMOT_API_KEY directly (bypasses username:password Login entirely)")
else:
    logger.info(
        "Yemot auth: using YEMOT_SYSTEM_NUMBER/YEMOT_PASSWORD (Login-based session token). "
        "If UploadFile keeps failing while Login/UpdateExtension succeed, this may be blocked "
        "by Yemot's Nov-2025 MFA policy — try setting YEMOT_API_KEY instead."
    )
YEMOT_RECORDINGS_FOLDER = os.environ.get("YEMOT_RECORDINGS_FOLDER", "ivr2:/ai_recordings").rstrip("/")

# --- זיהוי דיבור בחינם (בלי לשלם לימות המשיח) --------------------------------
# ימות המשיח גובה כסף על "voice" (זיהוי דיבור מובנה שלהם). האלטרנטיבה: מגדירים
# את השלוחה במצב "record" (הקלטה גולמית בלבד, ללא זיהוי - ולכן בחינם אצל ימות),
# מורידים את ההקלטה עם DownloadFile (כבר יש טוקן/Login ממודול ההעלאה למעלה),
# ומתמללים בעצמנו דרך Groq — יש להם free tier אמיתי ל-Whisper (מהיר, מדויק,
# בלי צורך במחשוב כבד על השרת שלכם, בניגוד להרצת Whisper מקומי על Render).
# הרשמה חינמית: https://console.groq.com → API Keys.
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_STT_MODEL = os.environ.get("GROQ_STT_MODEL", "whisper-large-v3-turbo")
STT_ENABLED = bool(GROQ_API_KEY and YEMOT_ENABLED)  # צריך גם Yemot כדי להוריד את ההקלטה עצמה
if GROQ_API_KEY and not YEMOT_ENABLED:
    logger.warning(
        "GROQ_API_KEY מוגדר אך YEMOT_SYSTEM_NUMBER/YEMOT_PASSWORD חסרים — "
        "בלי זה אי אפשר להוריד את קובץ ההקלטה מימות כדי לתמלל אותו, "
        "כך שזיהוי הדיבור החינמי מנוטרל וממשיכים עם voice הרגיל (בתשלום)."
    )
elif not GROQ_API_KEY:
    logger.info(
        "GROQ_API_KEY not set — voice search will use Yemot's built-in (paid) "
        "recognition. Set GROQ_API_KEY (free tier at console.groq.com) to use "
        "free transcription instead — no cost per search."
    )

# Redis אופציונלי לקאש משותף בין כמה instances. בלי REDIS_URL — TTLCache מקומי.
REDIS_URL = os.environ.get("REDIS_URL", "")
_redis = None
if REDIS_URL:
    try:
        import redis.asyncio as _aioredis  # type: ignore
        _redis = _aioredis.from_url(REDIS_URL, decode_responses=True, socket_timeout=2.0)
        logger.info("Redis cache enabled (shared across instances).")
    except ImportError:
        logger.warning(
            "REDIS_URL מוגדר אך חבילת redis לא מותקנת (pip install redis) — "
            "נופלים חזרה על TTLCache מקומי שאינו משותף בין instances."
        )
        _redis = None

PHONE_RE = re.compile(r"^\d{9,15}$")
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
AMOUNT_RE = re.compile(r"^\d{1,6}$")

# מפתח ה-API הציבורי שקליינט הווב של יוטיוב שולח בפועל. בלעדיו InnerTube
# נוטה להחזיר תשובת 200 "ריקה" (בלי תוצאות) במקום שגיאה — בדיוק התופעה
# שראינו בלוגים. ניתן לדרוס אם יוטיוב ישנה אותו (מפתח ציבורי, לא סוד).
INNERTUBE_KEY = os.environ.get("IVR_INNERTUBE_KEY", "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8")

# YouTube Data API v3 — הדרך ה*רשמית* לחפש (לא גירוד), הכי אמינה שיש. מכסה
# חינמית: 10,000 יחידות/יום, וחיפוש אחד עולה 100 יחידות => כ-100 חיפושים/יום
# בחינם. בלי מפתח, מדלגים לשכבות הגירוד (InnerTube/Invidious/Piped) כמו קודם.
# מפתח חינמי: Google Cloud Console → Enable "YouTube Data API v3" → Credentials.
YOUTUBE_DATA_API_KEY = os.environ.get("YOUTUBE_DATA_API_KEY", "")
YOUTUBE_DATA_API_ENABLED = bool(YOUTUBE_DATA_API_KEY)
if not YOUTUBE_DATA_API_ENABLED:
    logger.info(
        "YOUTUBE_DATA_API_KEY not set — search will rely entirely on scraping "
        "(InnerTube/Invidious/Piped), which is inherently less reliable. "
        "Setting a free Data API v3 key significantly improves search uptime."
    )

# פרוקסי אופציונלי (למשל Cloudflare Worker) בין השרת ליוטיוב — שימושי אם ה-IP
# של הפלטפורמה שלכם (Render וכו') חסום/מוגבל ע"י יוטיוב. בלי זה פונים ישירות
# ל-www.youtube.com. ה-secret נשלח כ-header ולא כחלק מה-URL כדי שלא ידלוף ללוגים.
YOUTUBE_PROXY_BASE = os.environ.get("IVR_YOUTUBE_PROXY_BASE", "").rstrip("/")
YOUTUBE_PROXY_SECRET = os.environ.get("IVR_YOUTUBE_PROXY_SECRET", "")

# רשימות שרתי Invidious/Piped ניתנות לעדכון דרך ENV בלי לגעת בקוד — רשימות
# כאלה "מתות" ומתחלפות כל הזמן, אז חשוב שלא יהיו קשיחות בקוד באופן בלעדי.
_default_invidious = "https://invidious.projectsegfau.lt,https://yewtu.be,https://invidious.fdn.fr,https://iv.ggtyler.dev"
_default_piped = "https://pipedapi.kavin.rocks,https://api-piped.mha.fi,https://piped-api.lunar.icu"
INVIDIOUS_INSTANCES = [i.strip() for i in os.environ.get("IVR_INVIDIOUS_INSTANCES", _default_invidious).split(",") if i.strip()]
PIPED_INSTANCES = [i.strip() for i in os.environ.get("IVR_PIPED_INSTANCES", _default_piped).split(",") if i.strip()]

# טוקן אופציונלי לחשוף endpoint אבחון לבדיקת חיפוש בלי לחכות לשיחת טלפון.
# בלי IVR_DEBUG_TOKEN, ה-endpoint מנוטרל לגמרי (404).
DEBUG_TOKEN = os.environ.get("IVR_DEBUG_TOKEN", "")

search_cache: TTLCache = TTLCache(maxsize=1000, ttl=900)
stream_url_cache: TTLCache = TTLCache(maxsize=500, ttl=600)


async def cache_get(local_cache: TTLCache, namespace: str, key: str) -> Optional[str]:
    if _redis is not None:
        try:
            val = await _redis.get(f"{namespace}:{key}")
            if val is not None:
                return val
        except Exception as e:
            logger.warning("Redis GET failed (%s) — נופל ל-cache מקומי: %s", namespace, e)
    return local_cache.get(key)


async def cache_set(local_cache: TTLCache, namespace: str, key: str, value: str, ttl: int) -> None:
    local_cache[key] = value
    if _redis is not None:
        try:
            await _redis.set(f"{namespace}:{key}", value, ex=ttl)
        except Exception as e:
            logger.warning("Redis SET failed (%s) — הערך נשמר רק מקומית: %s", namespace, e)


# מנעולים פר-טלפון: מונעים מרוץ מצבים בין שתי בקשות מקבילות לאותו מספר.
# כל הלוגיקה של PLAYING_TRACKS (כולל random.shuffle) רצה בתוך המנעול הזה,
# כך ששתי בקשות מקבילות לאותו טלפון תמיד מתבצעות בסדר, אף פעם בו-זמנית.
_phone_locks: dict[str, asyncio.Lock] = {}


def get_phone_lock(phone: str) -> asyncio.Lock:
    lock = _phone_locks.get(phone)
    if lock is None:
        lock = asyncio.Lock()
        _phone_locks[phone] = lock
    return lock


# ==========================================
# 🎵 פלייליסט חירום — תמיד עותק עמוק
# ==========================================
_EMERGENCY_PLAYLIST_SOURCE = [
    {"id": "4NzIOLEeJZM", "title": "נחמן פילמר שמחה פורצת גבולות 15", "duration": "1:26:07", "author": "נחמן פילמר"},
    {"id": "WSMFtm3ZqcY", "title": "סט להיטים דתי חרדי קיץ פול ווליום", "duration": "1:26:18", "author": "פול ווליום"},
    {"id": "3QDfxHZaUik", "title": "סט להיטים דתי מקפיץ בטירוף רמיקסים", "duration": "1:45:25", "author": "מדע והשכל"},
    {"id": "kP1jrKkSZfE", "title": "שמחת היום 1 סט חסידי קצבי אש", "duration": "2:20:53", "author": "פול ווליום"},
]


def get_emergency_playlist() -> List[dict]:
    return copy.deepcopy(_EMERGENCY_PLAYLIST_SOURCE)


# ==========================================
# 🌐 HTTP Client משותף
# ==========================================
http_client: Optional[httpx.AsyncClient] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global http_client
    http_client = httpx.AsyncClient(
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
        timeout=httpx.Timeout(8.0),
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"},
    )
    init_db()
    _init_db_pools()
    cleanup_task = asyncio.create_task(_cleanup_supervisor())
    logger.info("🚀 IVR Engine started")
    try:
        yield
    finally:
        cleanup_task.cancel()
        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass
        await http_client.aclose()
        _close_db_pools()
        if _redis is not None:
            try:
                await _redis.aclose()
            except Exception:
                pass
        logger.info("🛑 IVR Engine stopped")


app = FastAPI(title="Bulletproof IVR YouTube Engine 2026", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ==========================================
# 🗄️ בסיס נתונים
# ==========================================
def init_db() -> None:
    conn = sqlite3.connect(DB_PATH)
    try:
        cursor = conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA foreign_keys=ON")

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                phone TEXT PRIMARY KEY,
                authorized INTEGER DEFAULT 0,
                access_code TEXT DEFAULT '0000'
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                phone TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                playlist_json TEXT DEFAULT '[]',
                current_index INTEGER DEFAULT 0,
                last_active TEXT NOT NULL
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS favorites (
                phone TEXT,
                video_id TEXT,
                title TEXT,
                created_at TEXT,
                PRIMARY KEY(phone, video_id)
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS rate_limits (
                phone TEXT,
                timestamp TEXT
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS payments (
                tx_id TEXT PRIMARY KEY,
                phone TEXT,
                amount REAL,
                status TEXT,
                provider_response TEXT,
                created_at TEXT
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS yemot_uploads (
                video_id TEXT PRIMARY KEY,
                yemot_path TEXT NOT NULL,
                uploaded_at TEXT NOT NULL
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS session_uploads (
                session_key TEXT,
                video_id TEXT,
                PRIMARY KEY (session_key, video_id)
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS yemot_file_counter (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                next_num INTEGER NOT NULL DEFAULT 1
            )
        """)
        cursor.execute("INSERT OR IGNORE INTO yemot_file_counter (id, next_num) VALUES (1, 1)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_rate_limits_phone_ts ON rate_limits(phone, timestamp)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_sessions_last_active ON sessions(last_active)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_favorites_phone ON favorites(phone)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_payments_phone ON payments(phone)")

        for ph in DEFAULT_WHITELIST:
            if PHONE_RE.match(ph):
                cursor.execute(
                    "INSERT OR IGNORE INTO users (phone, authorized, access_code) VALUES (?, 1, ?)",
                    (ph, DEFAULT_ACCESS_CODE),
                )
            else:
                logger.warning("Skipping invalid whitelist phone entry: %r", ph)

        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Connection pool: כתיבות מסתדרות דרך חיבור יחיד + מנעול אחד (SQLite ממילא
# מרשה כותב יחיד בו-זמנית). קריאות (SELECT) מקבלות פול קטן של חיבורים
# נפרדים לקונקורנטיות אמיתית, וגם חוסכות פתיחה/סגירה של הקובץ בכל בקשה.
# להיקף גדול משמעותית (מאות שיחות מקבילות) מומלץ לעבור בעתיד ל-Postgres.
# --------------------------------------------------------------------------
_write_conn: Optional[sqlite3.Connection] = None
_write_lock = asyncio.Lock()
_read_conns: List[sqlite3.Connection] = []
_read_locks: List[asyncio.Lock] = []
_read_rr = 0


def _open_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=5.0, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _init_db_pools() -> None:
    global _write_conn, _read_conns, _read_locks
    _write_conn = _open_conn()
    _read_conns = [_open_conn() for _ in range(DB_READ_POOL_SIZE)]
    _read_locks = [asyncio.Lock() for _ in range(DB_READ_POOL_SIZE)]
    logger.info("DB pools ready: 1 write connection + %d read connections", DB_READ_POOL_SIZE)


def _close_db_pools() -> None:
    global _write_conn, _read_conns
    try:
        if _write_conn:
            _write_conn.close()
    except Exception:
        pass
    for c in _read_conns:
        try:
            c.close()
        except Exception:
            pass
    _write_conn = None
    _read_conns = []


async def run_db_query(
    query: str, params: tuple = (), fetchall: bool = False, commit: bool = False
):
    global _read_rr

    def _execute(conn: sqlite3.Connection):
        cursor = conn.cursor()
        cursor.execute(query, params)
        result = cursor.fetchall() if fetchall else cursor.fetchone()
        if commit:
            conn.commit()
        return result

    if _write_conn is None or not _read_conns:
        # פול לא אותחל עדיין (למשל שימוש לפני עליית lifespan) — fallback בטוח.
        def _execute_standalone():
            conn = _open_conn()
            try:
                return _execute(conn)
            finally:
                conn.close()
        try:
            return await asyncio.get_running_loop().run_in_executor(None, _execute_standalone)
        except Exception as e:
            logger.error("DB query failed (standalone): %s | query=%s", e, query)
            return [] if fetchall else None

    try:
        if commit:
            async with _write_lock:
                return await asyncio.get_running_loop().run_in_executor(None, _execute, _write_conn)
        else:
            idx = _read_rr % len(_read_conns)
            _read_rr += 1
            async with _read_locks[idx]:
                return await asyncio.get_running_loop().run_in_executor(None, _execute, _read_conns[idx])
    except Exception as e:
        logger.error("DB query failed: %s | query=%s", e, query)
        if commit and _write_conn is not None:
            try:
                _write_conn.rollback()
            except Exception:
                pass
        return [] if fetchall else None


# ==========================================
# 🚦 Rate Limiting (בלי DELETE בכל בקשה — רק ספירה; הניקוי בטאסק הרקע)
# ==========================================
async def is_rate_limited(session_key: str) -> bool:
    # הוולידציה של phone/anon כבר בוצעה ב-handle_ivr לפני שהגענו לכאן — כאן רק
    # מוודאים שיש מפתח לא-ריק (יכול להיות "0501234567" או "anon:<call_id>").
    if not session_key:
        return True
    try:
        now = utcnow()
        one_minute_ago = (now - timedelta(minutes=1)).isoformat()

        count_row = await run_db_query(
            "SELECT COUNT(*) FROM rate_limits WHERE phone = ? AND timestamp > ?",
            (session_key, one_minute_ago),
        )
        count = count_row[0] if count_row else 0
        if count >= RATE_LIMIT_PER_MINUTE:
            return True

        await run_db_query(
            "INSERT INTO rate_limits (phone, timestamp) VALUES (?, ?)",
            (session_key, now.isoformat()),
            commit=True,
        )
        return False
    except Exception as e:
        logger.error("Rate limit check failed: %s", e)
        return True  # בספק — עדיף לחסום מאשר לפתוח פרצה


# ==========================================
# 🔎 חיפוש — InnerTube + regex fallback + Invidious fallback
# ==========================================
def _parse_iso8601_duration(iso: str) -> str:
    """ממיר משך זמן בפורמט ISO8601 של YouTube ('PT4M13S') לפורמט קריא (4:13)."""
    m = re.match(r"^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$", iso or "")
    if not m:
        return "00:00"
    h, mi, se = (int(x) if x else 0 for x in m.groups())
    if h:
        return f"{h}:{mi:02d}:{se:02d}"
    return f"{mi}:{se:02d}"


def _dedupe_and_trim(tracks: List[dict], limit: int = MAX_PLAYLIST_SIZE) -> List[dict]:
    seen = set()
    out = []
    for t in tracks:
        vid = t.get("id")
        if not vid or not VIDEO_ID_RE.match(vid) or vid in seen:
            continue
        seen.add(vid)
        t["title"] = (t.get("title") or "שיר ללא שם")[:120]
        t["author"] = (t.get("author") or "אמן")[:80]
        t["duration"] = t.get("duration") or "00:00"
        out.append(t)
        if len(out) >= limit:
            break
    return out


def extract_tracks_from_innertube(data: dict) -> List[dict]:
    tracks: List[dict] = []

    def recursive_extract(node, depth: int = 0):
        if depth > SEARCH_RECURSION_DEPTH_LIMIT or len(tracks) >= MAX_PLAYLIST_SIZE * 3:
            return

        if isinstance(node, dict):
            renderer = None
            if "videoRenderer" in node:
                renderer = node["videoRenderer"]
            elif "compactVideoRenderer" in node:
                renderer = node["compactVideoRenderer"]
            elif "richItemRenderer" in node and "content" in node["richItemRenderer"]:
                recursive_extract(node["richItemRenderer"]["content"], depth + 1)
                return
            elif "itemSectionRenderer" in node:
                recursive_extract(node["itemSectionRenderer"].get("contents", []), depth + 1)
                return

            if renderer and renderer.get("videoId"):
                video_id = renderer.get("videoId")
                title_runs = renderer.get("title", {}).get("runs")
                if title_runs:
                    title = title_runs[0].get("text", "שיר ללא שם")
                else:
                    title = renderer.get("title", {}).get("simpleText", "שיר ללא שם")

                byline_runs = renderer.get("longBylineText", {}).get("runs", [{}])
                author = byline_runs[0].get("text", "אמן") if byline_runs else "אמן"

                tracks.append({
                    "id": video_id,
                    "title": title,
                    "duration": renderer.get("lengthText", {}).get("simpleText", "00:00"),
                    "author": author,
                })
                return

            for value in node.values():
                recursive_extract(value, depth + 1)

        elif isinstance(node, list):
            for item in node:
                recursive_extract(item, depth + 1)

    recursive_extract(data)
    return _dedupe_and_trim(tracks)


_VIDEO_ID_SCAN_RE = re.compile(r'"videoId"\s*:\s*"([A-Za-z0-9_-]{11})"')


def extract_tracks_regex_fallback(raw_text: str) -> List[dict]:
    """קו הגנה אחרון אם מבנה ה-JSON של InnerTube משתנה ופענוח מובנה נכשל:
    סריקת regex גולמית לזיהוי videoId בטקסט הגולמי. פחות מדויק (בלי כותרות
    אמיתיות) אבל עדיף על נפילה מיידית ל-Invidious/פלייליסט חירום."""
    ids = _VIDEO_ID_SCAN_RE.findall(raw_text or "")
    tracks = [{"id": vid, "title": "שיר ללא שם", "duration": "00:00", "author": "אמן"} for vid in ids]
    return _dedupe_and_trim(tracks)


async def search_youtube_data_api(query: str, filter_newest: bool = False) -> List[dict]:
    """שכבה 0: ה-API הרשמי של יוטיוב. לא גירוד — לא שובר כשיוטיוב משנה HTML/JSON
    פנימי, ולא נחסם לפי IP. המגבלה היחידה היא מכסה יומית (ר' הערה ב-config)."""
    if not YOUTUBE_DATA_API_ENABLED:
        return []
    assert http_client is not None
    try:
        search_resp = await http_client.get(
            "https://www.googleapis.com/youtube/v3/search",
            params={
                "part": "snippet",
                "q": query,
                "type": "video",
                "maxResults": min(MAX_PLAYLIST_SIZE, 15),
                "regionCode": "IL",
                "relevanceLanguage": "he",
                "order": "date" if filter_newest else "relevance",
                "key": YOUTUBE_DATA_API_KEY,
            },
            timeout=7.0,
        )
        if search_resp.status_code != 200:
            try:
                err = search_resp.json().get("error", {})
                reason = (err.get("errors") or [{}])[0].get("reason", "")
            except (json.JSONDecodeError, KeyError, IndexError):
                reason = ""
            if reason == "quotaExceeded":
                logger.warning(
                    "YouTube Data API v3 quota exceeded for today — falling back "
                    "to scraping tiers until the quota resets (daily, Pacific time)."
                )
            else:
                logger.warning("YouTube Data API v3 search failed: %s %s", search_resp.status_code, reason)
            return []

        items = search_resp.json().get("items", [])
        prelim = []
        for item in items:
            vid = (item.get("id") or {}).get("videoId")
            snippet = item.get("snippet") or {}
            if not vid:
                continue
            prelim.append({
                "id": vid,
                "title": snippet.get("title", "שיר ללא שם"),
                "author": snippet.get("channelTitle", "אמן"),
                "duration": "00:00",
            })
        prelim = _dedupe_and_trim(prelim)
        if not prelim:
            return []

        # קריאה שנייה (זולה — יחידת quota אחת) כדי לקבל משכי זמן אמיתיים.
        try:
            ids_param = ",".join(t["id"] for t in prelim)
            details_resp = await http_client.get(
                "https://www.googleapis.com/youtube/v3/videos",
                params={"part": "contentDetails", "id": ids_param, "key": YOUTUBE_DATA_API_KEY},
                timeout=6.0,
            )
            if details_resp.status_code == 200:
                duration_by_id = {
                    d["id"]: _parse_iso8601_duration((d.get("contentDetails") or {}).get("duration", ""))
                    for d in details_resp.json().get("items", [])
                }
                for t in prelim:
                    t["duration"] = duration_by_id.get(t["id"], "00:00")
        except (httpx.HTTPError, asyncio.TimeoutError, json.JSONDecodeError) as e:
            logger.warning("YouTube Data API duration lookup failed (non-fatal): %s", e)

        logger.info("✅ YouTube Data API v3 success: %d tracks", len(prelim))
        return prelim

    except (httpx.HTTPError, asyncio.TimeoutError, json.JSONDecodeError) as e:
        logger.warning("YouTube Data API v3 request failed: %s", e)
        return []


async def search_invidious_fallback(query: str) -> List[dict]:
    assert http_client is not None
    for inst in INVIDIOUS_INSTANCES:
        try:
            resp = await http_client.get(
                f"{inst}/api/v1/search", params={"q": query, "type": "video"}, timeout=6.0
            )
            if resp.status_code != 200:
                continue
            try:
                data = resp.json()
            except json.JSONDecodeError:
                logger.warning("Invidious %s returned non-JSON", inst)
                continue

            tracks = []
            for item in data:
                if isinstance(item, dict) and item.get("videoId"):
                    tracks.append({
                        "id": item["videoId"],
                        "title": item.get("title", "שיר ללא שם"),
                        "duration": str(item.get("lengthSeconds", "00:00")),
                        "author": item.get("author", "אמן"),
                    })

            tracks = _dedupe_and_trim(tracks, limit=12)
            if tracks:
                logger.info("✅ Invidious success via %s: %d tracks", inst, len(tracks))
                return tracks
        except (httpx.HTTPError, asyncio.TimeoutError) as e:
            logger.warning("Invidious %s failed: %s", inst, e)
            continue
    return []


async def search_piped_fallback(query: str) -> List[dict]:
    """שכבת fallback נוספת (Piped) — פורמט ותוכן instances שונים מ-Invidious,
    כך ששני השירותים לא נופלים יחד באותו סוג תקלה (שרת מסוים חסום/מת)."""
    assert http_client is not None
    for inst in PIPED_INSTANCES:
        try:
            resp = await http_client.get(
                f"{inst}/search", params={"q": query, "filter": "videos"}, timeout=6.0
            )
            if resp.status_code != 200:
                continue
            try:
                data = resp.json()
            except json.JSONDecodeError:
                logger.warning("Piped %s returned non-JSON", inst)
                continue

            items = data.get("items", []) if isinstance(data, dict) else []
            tracks = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                url_path = item.get("url", "")  # e.g. "/watch?v=XXXXXXXXXXX"
                m = re.search(r"[?&]v=([A-Za-z0-9_-]{11})", url_path)
                if not m:
                    continue
                secs = item.get("duration")
                duration = f"{secs // 60}:{secs % 60:02d}" if isinstance(secs, int) and secs >= 0 else "00:00"
                tracks.append({
                    "id": m.group(1),
                    "title": item.get("title", "שיר ללא שם"),
                    "duration": duration,
                    "author": item.get("uploaderName", "אמן"),
                })

            tracks = _dedupe_and_trim(tracks, limit=12)
            if tracks:
                logger.info("✅ Piped success via %s: %d tracks", inst, len(tracks))
                return tracks
        except (httpx.HTTPError, asyncio.TimeoutError) as e:
            logger.warning("Piped %s failed: %s", inst, e)
            continue
    return []


async def _search_via_innertube_scrape(query: str, filter_newest: bool) -> List[dict]:
    """שכבת גירוד (לא רשמית) של InnerTube — שכבה 1, רק אם ה-Data API הרשמי
    לא מוגדר/נכשל. עלולה להישבר כשיוטיוב משנה מבנה/מדיניות אנטי-בוט."""
    api_base = YOUTUBE_PROXY_BASE or "https://www.youtube.com"
    url = f"{api_base}/youtubei/v1/search?key={INNERTUBE_KEY}&prettyPrint=false"
    payload = {
        "context": {
            "client": {
                "clientName": "WEB",
                "clientVersion": "2.20260601.01.00",
                "hl": "he",
                "gl": "IL",
                "platform": "DESKTOP",
                "clientFormFactor": "UNKNOWN_FORM_FACTOR",
            }
        },
        "query": query,
    }
    if filter_newest:
        payload["params"] = "EgQIARAB"  # מיון לפי תאריך העלאה

    headers = {
        "Content-Type": "application/json",
        "Origin": "https://www.youtube.com",
        "Referer": "https://www.youtube.com/",
        "X-YouTube-Client-Name": "1",
        "X-YouTube-Client-Version": "2.20260601.01.00",
    }
    if YOUTUBE_PROXY_BASE and YOUTUBE_PROXY_SECRET:
        headers["X-Proxy-Auth"] = YOUTUBE_PROXY_SECRET

    try:
        assert http_client is not None
        resp = await http_client.post(url, json=payload, headers=headers, timeout=7.0)
        logger.info("InnerTube status: %s for query: %s (via %s)", resp.status_code, query,
                    "proxy" if YOUTUBE_PROXY_BASE else "direct")
        if resp.status_code != 200:
            # לוג אבחון מיידי — בלי זה קשה לדעת *למה* השרת דחה (400/403/...)
            # מבלי לשחזר את הבעיה שוב. 300 תווים מספיקים כדי לזהות סוג שגיאה.
            logger.warning("InnerTube non-200 body preview: %s", resp.text[:300].replace("\n", " "))
            return []

        try:
            raw_data = resp.json()
            tracks = extract_tracks_from_innertube(raw_data)
        except json.JSONDecodeError:
            raw_data = None
            tracks = []

        if not tracks:
            logger.warning("Structured parse got 0 tracks for query=%r — trying regex fallback", query)
            tracks = extract_tracks_regex_fallback(resp.text)

        if tracks:
            logger.info("✅ InnerTube parsed successfully: %d tracks", len(tracks))
            return tracks

        # אבחון מורחב: 200 תווים ראשונים כמעט תמיד זה רק "responseContext"/
        # "visitorData" — לא אינפורמטיבי. מה שכן אינפורמטיבי: אילו מפתחות
        # top-level קיימים (למשל absence של "contents" = כנראה נחסמנו/קיבלנו
        # תשובת "עזרה" ולא תוצאות אמיתיות), ו-estimatedResults אם קיים.
        if raw_data is not None and isinstance(raw_data, dict):
            top_keys = list(raw_data.keys())
            estimated = raw_data.get("estimatedResults")
            logger.warning(
                "InnerTube returned 200 but 0 tracks for query=%r. top_level_keys=%s estimatedResults=%s. "
                "אם 'contents' לא ברשימה — כנראה תשובת bot-block/consent ולא תוצאות אמיתיות; "
                "פתרון קבוע לכך הוא YOUTUBE_DATA_API_KEY (שכבה 0 הרשמית) ולא עוד תיקון בגירוד.",
                query, top_keys, estimated,
            )
        else:
            logger.warning(
                "InnerTube returned 200 but non-JSON/undecodable body for query=%r. Preview: %s",
                query, resp.text[:300].replace("\n", " "),
            )
        return []
    except (httpx.HTTPError, asyncio.TimeoutError) as e:
        logger.error("InnerTube request failed: %s", e)
        return []


async def search_youtube_innertube(query: str, filter_newest: bool = False) -> List[dict]:
    """מנוע החיפוש הראשי — עובר על כל שכבות ההגנה בסדר אמינות יורד:
    0) YouTube Data API v3 הרשמי (אם מוגדר מפתח) — הכי אמין, לא נשבר.
    1) גירוד InnerTube (ישיר או דרך פרוקסי) — לא רשמי, יכול להישבר.
    2) Invidious (כמה instances ציבוריים).
    3) Piped (כמה instances ציבוריים, סוג תקלה שונה מ-Invidious).
    4) פלייליסט חירום — כדי שלעולם לא תיתקע שיחה בלי שום שיר.
    כל שכבה שמצליחה נשמרת בקאש (Redis אם מוגדר, אחרת מקומי) ל-15 דקות."""
    query = (query or "").strip()[:150]
    if not query:
        return get_emergency_playlist()

    cache_key = f"{'newest:' if filter_newest else ''}{query}"
    cached = await cache_get(search_cache, "search", cache_key)
    if cached:
        try:
            return json.loads(cached)
        except json.JSONDecodeError:
            pass

    if YOUTUBE_DATA_API_ENABLED:
        tracks = await search_youtube_data_api(query, filter_newest)
        if tracks:
            await cache_set(search_cache, "search", cache_key, json.dumps(tracks, ensure_ascii=False), 900)
            return tracks
        logger.info("Data API v3 unavailable/empty → trying InnerTube scrape")

    tracks = await _search_via_innertube_scrape(query, filter_newest)
    if tracks:
        await cache_set(search_cache, "search", cache_key, json.dumps(tracks, ensure_ascii=False), 900)
        return tracks

    logger.info("InnerTube scrape failed → trying Invidious fallback")
    fallback_tracks = await search_invidious_fallback(query)
    if fallback_tracks:
        await cache_set(search_cache, "search", cache_key, json.dumps(fallback_tracks, ensure_ascii=False), 900)
        return fallback_tracks

    logger.info("Invidious failed → trying Piped fallback")
    fallback_tracks = await search_piped_fallback(query)
    if fallback_tracks:
        await cache_set(search_cache, "search", cache_key, json.dumps(fallback_tracks, ensure_ascii=False), 900)
        return fallback_tracks

    logger.error(
        "All search backends failed (query=%r) → serving Emergency playlist. "
        "אם YOUTUBE_DATA_API_KEY לא מוגדר — זו כנראה הסיבה המרכזית לכשל; "
        "כל שאר השכבות הן גירוד לא-רשמי שחשוף לחסימות בכל רגע.",
        query,
    )
    return get_emergency_playlist()


# ==========================================
# 🎧 Streaming — עם pre-flight validation לפני שמתחייבים ללקוח
# ==========================================
async def _candidate_stream_urls(video_id: str) -> List[str]:
    candidates: List[str] = []
    watch_url = f"https://www.youtube.com/watch?v={video_id}"

    for inst in ["https://api.cobalt.tools/api/json", "https://cobalt.api.v0.wtf/api/json"]:
        candidates.append(f"cobalt::{inst}::{watch_url}")

    if RAPIDAPI_KEY:
        candidates.append(f"rapidapi::{video_id}")

    candidates.append(f"invidious::https://invidious.projectsegfau.lt/latest_version?id={video_id}&itag=140")
    return candidates


async def _resolve_candidate(candidate: str) -> Optional[str]:
    assert http_client is not None
    try:
        if candidate.startswith("cobalt::"):
            _, inst, watch_url = candidate.split("::", 2)
            r = await http_client.post(
                inst,
                json={"url": watch_url, "downloadMode": "audio", "audioFormat": "mp3"},
                timeout=4.0,
            )
            if r.status_code == 200:
                return r.json().get("url")

        elif candidate.startswith("rapidapi::"):
            video_id = candidate.split("::", 1)[1]
            r = await http_client.get(
                f"https://{RAPIDAPI_HOST}/get_mp3_download_link/{video_id}",
                headers={"x-rapidapi-key": RAPIDAPI_KEY, "x-rapidapi-host": RAPIDAPI_HOST},
                timeout=5.0,
            )
            if r.status_code == 200:
                js = r.json()
                return js.get("file") or js.get("link") or js.get("url")

        elif candidate.startswith("invidious::"):
            return candidate.split("::", 1)[1]

    except (httpx.HTTPError, asyncio.TimeoutError, json.JSONDecodeError, ValueError) as e:
        logger.warning("Candidate resolve failed (%s): %s", candidate[:40], e)
    return None


def _parse_range_header(range_header: Optional[str], total_len: int) -> Optional[Tuple[int, int]]:
    """מפרש 'Range: bytes=START-END' בסיסי. מחזיר (start, end) כולל, או None אם
    אין/לא תקין (ואז שולחים את הקובץ המלא — זה תמיד תקין, גם אם הלקוח ביקש range)."""
    if not range_header or not range_header.startswith("bytes="):
        return None
    try:
        spec = range_header.split("=", 1)[1].split(",")[0].strip()
        start_s, _, end_s = spec.partition("-")
        start = int(start_s) if start_s else 0
        end = int(end_s) if end_s else total_len - 1
        start = max(0, start)
        end = min(total_len - 1, end)
        if start > end or start >= total_len:
            return None
        return start, end
    except (ValueError, IndexError):
        return None


@app.api_route("/stream/{video_id}.mp3", methods=["GET", "HEAD"])
async def proxy_mp3_stream(video_id: str, request: Request):
    # לוג בולט ובלתי-ניתן-לפספוס: אם השורה הזו לעולם לא מופיעה בלוג אחרי
    # שפקודת "read=.../stream/..." הוחזרה למרכזיה, זה מוכיח באופן חד-משמעי
    # שהמרכזיה בכלל לא ניסתה לפנות לכתובת — כלומר הבעיה היא בהגדרות הפלטפורמה
    # הטלפונית (חובה לאפשר ניגון קובץ מכתובת אינטרנט בהגדרות השלוחה/מס' מסלול),
    # ולא באג בקוד הזה.
    client_ip = request.headers.get("x-forwarded-for", request.client.host if request.client else "unknown")
    logger.info("🔊 /stream request RECEIVED (%s) for video_id=%s from %s", request.method, video_id, client_ip)

    if not VIDEO_ID_RE.match(video_id):
        logger.warning("🔊 /stream rejected: invalid video_id=%r", video_id)
        raise HTTPException(400, "Invalid video ID")

    assert http_client is not None
    cached_url = await cache_get(stream_url_cache, "stream", video_id)
    candidates = ([f"invidious::{cached_url}"] if cached_url else []) + await _candidate_stream_urls(video_id)

    for candidate in candidates:
        target_url = await _resolve_candidate(candidate)
        if not target_url or not target_url.startswith("https://"):
            continue

        if STREAM_MODE == "passthrough":
            result = await _try_passthrough_stream(candidate, target_url, video_id)
        else:
            result = await _try_buffered_stream(candidate, target_url, video_id, request)
        if result is not None:
            return result

    logger.error("All stream sources exhausted for video_id=%s", video_id)
    raise HTTPException(502, "No available audio source for this track")


async def download_audio_bytes(video_id: str) -> Optional[bytes]:
    """מוריד את קובץ ה-mp3 המלא לזיכרון, מנסה את כל המקורות לפי סדר עדיפות
    (כמו /stream), אך מחזיר bytes גולמיים במקום Response — משמש גם את /stream
    וגם את צינור ההעלאה לימות המשיח, כדי לא לשכפל את לוגיקת ה-fallback."""
    assert http_client is not None
    cached_url = await cache_get(stream_url_cache, "stream", video_id)
    candidates = ([f"invidious::{cached_url}"] if cached_url else []) + await _candidate_stream_urls(video_id)

    for candidate in candidates:
        target_url = await _resolve_candidate(candidate)
        if not target_url or not target_url.startswith("https://"):
            continue
        try:
            resp = await http_client.get(
                target_url,
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0),
                follow_redirects=True,
            )
        except (httpx.HTTPError, asyncio.TimeoutError) as e:
            logger.warning("Audio download failed for %s via %s: %s", video_id, candidate[:40], e)
            continue
        if resp.status_code != 200 or len(resp.content) == 0:
            continue
        if len(resp.content) > MAX_STREAM_BYTES:
            logger.warning("Track %s exceeds IVR_MAX_STREAM_BYTES via %s — trying next source",
                            video_id, candidate[:40])
            continue
        await cache_set(stream_url_cache, "stream", video_id, target_url, 600)
        return resp.content

    return None


async def _try_buffered_stream(candidate: str, target_url: str, video_id: str, request: Request):
    """מוריד את הקובץ המלא לזיכרון ומחזיר תשובה עם Content-Length מדויק —
    ברירת המחדל, כי זו הדרך הכי תואמת למגוון הרחב ביותר של פלטפורמות IVR."""
    assert http_client is not None
    try:
        resp = await http_client.get(
            target_url,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0),
            follow_redirects=True,
        )
    except (httpx.HTTPError, asyncio.TimeoutError) as e:
        logger.warning("Buffered fetch failed for %s: %s", candidate[:40], e)
        return None

    if resp.status_code != 200:
        return None

    body = resp.content
    if len(body) == 0:
        logger.warning("Source %s returned 0 bytes for %s — trying next source", candidate[:40], video_id)
        return None
    if len(body) > MAX_STREAM_BYTES:
        logger.warning(
            "Track %s exceeds IVR_MAX_STREAM_BYTES (%d > %d) — skipping this source",
            video_id, len(body), MAX_STREAM_BYTES,
        )
        return None

    await cache_set(stream_url_cache, "stream", video_id, target_url, 600)
    total_len = len(body)

    base_headers = {
        "Content-Type": "audio/mpeg",
        "Accept-Ranges": "bytes",
        "Cache-Control": "no-store",
    }

    range_header = request.headers.get("range")
    parsed_range = _parse_range_header(range_header, total_len)

    if request.method == "HEAD":
        headers = {**base_headers, "Content-Length": str(total_len)}
        logger.info("🔊 /stream HEAD served for %s: %d bytes total", video_id, total_len)
        return Response(status_code=200, headers=headers)

    if parsed_range is not None:
        start, end = parsed_range
        chunk = body[start:end + 1]
        headers = {
            **base_headers,
            "Content-Length": str(len(chunk)),
            "Content-Range": f"bytes {start}-{end}/{total_len}",
        }
        logger.info("🔊 /stream SUCCESS (206 partial %d-%d/%d): serving %s via %s",
                    start, end, total_len, video_id, candidate.split("::")[0])
        return Response(content=chunk, status_code=206, media_type="audio/mpeg", headers=headers)

    headers = {**base_headers, "Content-Length": str(total_len)}
    logger.info("🔊 /stream SUCCESS (200, %d bytes buffered): serving %s via %s",
                total_len, video_id, candidate.split("::")[0])
    return Response(content=body, media_type="audio/mpeg", headers=headers)


async def _try_passthrough_stream(candidate: str, target_url: str, video_id: str):
    """הסגנון הישן: הזרמה live בלי buffer מלא — פחות תואם לפלטפורמות IVR
    ישנות (אין Content-Length), אבל צורך פחות זיכרון וזמן-עד-תגובה-ראשונה
    קצר יותר. זמין דרך IVR_STREAM_MODE=passthrough למי שיודע שהמערכת שלו תומכת."""
    assert http_client is not None
    try:
        req = http_client.build_request(
            "GET", target_url,
            timeout=httpx.Timeout(connect=5.0, read=None, write=10.0, pool=5.0),
        )
        resp = await http_client.send(req, stream=True)
    except (httpx.HTTPError, asyncio.TimeoutError) as e:
        logger.warning("Passthrough stream failed for %s: %s", candidate[:40], e)
        return None

    if resp.status_code != 200:
        await resp.aclose()
        return None

    await cache_set(stream_url_cache, "stream", video_id, target_url, 600)
    logger.info("🔊 /stream SUCCESS (passthrough): serving %s via %s", video_id, candidate.split("::")[0])

    byte_counter = {"n": 0}

    async def chunk_generator(response: httpx.Response):
        try:
            async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                byte_counter["n"] += len(chunk)
                yield chunk
        except (httpx.HTTPError, asyncio.TimeoutError) as e:
            logger.error("Streaming error mid-stream for %s after %d bytes: %s", video_id, byte_counter["n"], e)
        finally:
            await response.aclose()
            logger.info("🔊 /stream ENDED for %s, total bytes sent: %d", video_id, byte_counter["n"])

    return StreamingResponse(chunk_generator(resp), media_type="audio/mpeg")


# ==========================================
# 📤 ימות המשיח: Login + UploadFile (upload-first playback)
# ==========================================
# מאומת מול הפורום הרשמי של מפתחי ימות המשיח: ניגון ישירות מ-URL חיצוני *לא
# נתמך בכלל* על ידי המערכת שלהם — יש להעלות כל קובץ מראש עם UploadFile ואז
# לנגן אותו לפי נתיב פנימי. זה מסביר באופן מוחלט למה /stream/ מעולם לא נקרא.
_yemot_token: Optional[str] = None
_yemot_token_expires_at: Optional[datetime] = None
_yemot_login_lock = asyncio.Lock()


async def _yemot_login(force: bool = False) -> Optional[str]:
    """מתחבר עם מספר מערכת+סיסמה ומקבל token זמני, עם קאש (ברירת מחדל: מרענן
    כל 25 דקות ליתר ביטחון, גם אם לא ידוע לנו במדויק כמה זמן token תקף).

    אם הוגדר YEMOT_API_KEY — משתמשים בו ישירות כ-token, בלי לקרוא ל-Login
    בכלל. זה קריטי כי לפי תיעוד: החל מנובמבר 2025, ימות עשויה לחסום
    התחברות מסורתית (username:password) לפעולות API מסוימות אלא אם כן
    הוגדר IP מאושר או שמשתמשים ב-API_KEY ייעודי. זה עשוי להסביר למה Login
    ו-UpdateExtension הצליחו (אולי לא מוגבלים) בעוד UploadFile ספציפית נכשל
    (ר' חשוב: ה-API_KEY חייב להיות מוגדר בפאנל הניהול עם הרשאה מפורשת
    לשירות UploadFile, אחרת גם הוא ידחה)."""
    global _yemot_token, _yemot_token_expires_at

    if YEMOT_API_KEY:
        return YEMOT_API_KEY

    if not YEMOT_ENABLED:
        return None

    async with _yemot_login_lock:
        if not force and _yemot_token and _yemot_token_expires_at and utcnow() < _yemot_token_expires_at:
            return _yemot_token
        try:
            assert http_client is not None
            # POST עם body במקום GET עם query params — Yemot תומכת בשני
            # השיטות (מתועד רשמית), אבל GET שם את הסיסמה בתוך ה-URL עצמו,
            # שמודפס במלואו ע"י httpx/Render/כל פרוקסי בדרך. עם POST הסיסמה
            # נמצאת ב-body ולא מודפסת בשום מקום.
            resp = await http_client.post(
                f"{YEMOT_API_BASE}/Login",
                data={"username": YEMOT_SYSTEM_NUMBER, "password": YEMOT_PASSWORD},
                timeout=10.0,
            )
            data = resp.json()
            if data.get("responseStatus") != "OK" or not data.get("token"):
                logger.error(
                    "Yemot Login failed: %s — אם זה FORBIDDEN/EXCEPTION, ייתכן שזו חסימת "
                    "ה-MFA של נובמבר 2025. שקלו להגדיר YEMOT_API_KEY במקום.",
                    _safe_json_snippet_early(data),
                )
                return None
            _yemot_token = data["token"]
            _yemot_token_expires_at = utcnow() + timedelta(minutes=25)
            logger.info("Yemot Login OK — token cached for ~25 minutes")
            return _yemot_token
        except (httpx.HTTPError, asyncio.TimeoutError, json.JSONDecodeError) as e:
            logger.error("Yemot Login request failed: %s", e)
            return None


def _safe_json_snippet_early(data, limit: int = 500) -> str:
    """גרסה מוקדמת של _safe_json_snippet (מוגדר שוב במלואו בהמשך הקובץ) —
    צריך כאן רק כדי לרשום שגיאת Login ללוג בלי לסמוך על סדר הגדרת פונקציות."""
    try:
        s = json.dumps(data, ensure_ascii=False)
    except (TypeError, ValueError):
        s = str(data)
    return s[:limit]


_yemot_dirs_ensured: set = set()  # קאש בזיכרון - אילו שלוחות ivr2 כבר נוצרו, כדי לא לקרוא UpdateExtension בכל העלאה


async def _yemot_write_ext_ini(folder_path: str, contents: str) -> bool:
    """כותב את קובץ ext.ini של השלוחה ישירות דרך UploadTextFile — מאומת מול
    דוגמה אמיתית ומאושרת כעובדת בפורום המפתחים ('עובד פצצה'):
      UploadTextFile?token=...&what=ivr2:/5/ext.ini&contents=type=...
    שימו לב לשמות הפרמטרים השונים מ-UploadFile: 'what' (לא 'path') ו-
    'contents' (לא 'file'). זו כנראה הדרך האמיתית להגדיר type= לשלוחה —
    ה-type שנשלח כפרמטר ל-UpdateExtension עצמו עלול פשוט להתעלם בשקט,
    מה שמסביר למה השלוחה 'הצליחה להיווצר' אבל עדיין דחתה העלאות קבצים."""
    token = await _yemot_login()
    if not token:
        return False
    try:
        assert http_client is not None
        resp = await http_client.post(
            f"{YEMOT_API_BASE}/UploadTextFile",
            data={"token": token, "what": f"{folder_path}/ext.ini", "contents": contents},
            timeout=10.0,
        )
        data = resp.json()
        ok = data.get("responseStatus") == "OK"
        logger.info("Yemot UploadTextFile(ext.ini @ %s/ext.ini): %s", folder_path, _safe_json_snippet_early(data))
        return ok
    except (httpx.HTTPError, asyncio.TimeoutError, json.JSONDecodeError) as e:
        logger.warning("Yemot UploadTextFile(ext.ini) failed for %s: %s", folder_path, e)
        return False


async def _yemot_ensure_dir(path: str) -> bool:
    """יוצר שלוחת ivr2 אם היא לא קיימת עדיין, ומגדיר אותה כ-'playfile'
    (השמעת קבצים) בשתי דרכים במקביל — כי לא ברור אילו מהן ימות "מכבד" בפועל:
      1. UpdateExtension עם type=playfile כפרמטר (יכול להתעלם בשקט).
      2. כתיבת ext.ini מפורשת עם 'type=playfile' כשורה ראשונה, דרך
         UploadTextFile — מאומת כעובד בדוגמה אמיתית מהפורום.
    מחזיר True אם לפחות אחת הדרכים הצליחה. לא בהכרח שהשלוחה כבר זמינה מיד —
    ר' ההערה על השהיית הפצה ב-_yemot_upload_file."""
    if path in _yemot_dirs_ensured:
        return True
    token = await _yemot_login()
    if not token:
        return False

    ok_update_extension = False
    try:
        assert http_client is not None
        resp = await http_client.post(
            f"{YEMOT_API_BASE}/UpdateExtension",
            data={"token": token, "path": path, "type": "playfile"},
            timeout=10.0,
        )
        data = resp.json()
        ok_update_extension = data.get("responseStatus") == "OK"
        logger.info("Yemot UpdateExtension(%s, type=playfile): %s", path, _safe_json_snippet_early(data))
    except (httpx.HTTPError, asyncio.TimeoutError, json.JSONDecodeError) as e:
        logger.warning("Yemot UpdateExtension failed for %s: %s", path, e)

    ok_ext_ini = await _yemot_write_ext_ini(path, "type=playfile\n")

    ok = ok_update_extension or ok_ext_ini
    if ok:
        _yemot_dirs_ensured.add(path)
    return ok


async def _next_yemot_file_number() -> int:
    """מספר רץ קצר (1, 2, 3...) לשמות קבצים בימות — לא timestamp. כל הדוגמאות
    האמיתיות שמצאנו בפורום המפתחים (1000.wav, 002.wav, M1101.wav) הן קצרות;
    יתכן שזה בדיוק מה שחסר גם אחרי שהפכנו לספרות בלבד — 13 ספרות של
    timestamp עדיין עלולות להידחות כ'לא תקין' אם יש טווח ערכים סביר מצופה.
    נשמר ב-DB (עולה תמיד, לעולם לא מתאפס) כדי שלא יהיה סיכוי להתנגשות שמות."""
    row = await run_db_query("SELECT next_num FROM yemot_file_counter WHERE id = 1")
    num = row[0] if row else 1
    await run_db_query(
        "INSERT INTO yemot_file_counter (id, next_num) VALUES (1, ?) "
        "ON CONFLICT(id) DO UPDATE SET next_num = next_num + 1",
        (num + 1,), commit=True,
    )
    return num


async def _yemot_upload_file(video_id: str, audio_bytes: bytes) -> Optional[str]:
    """מעלה קובץ mp3 לשלוחת ivr2 הייעודית בימות המשיח. מחזיר את הנתיב הפנימי
    שנקבע, או None אם ההעלאה נכשלה.

    קריטי #1: מאומת במפורש בפורום המפתחים — 'שם הקובץ צריך לכלול ספרות בלבד'.
    כל הדוגמאות האמיתיות שמצאנו (1000.wav, 002.wav, M1101.wav) הן שמות
    *קצרים* — לכן משתמשים כאן במספר רץ קצר (ר' _next_yemot_file_number).

    קריטי #2 (מקור: ניתוח תיעוד רשמי): פורמט הקבצים הטלפוני של ימות הוא
    WAV (PCM, 8000Hz, מונו) — ולכן ה-*נתיב היעד* (פרמטר path) חייב לציין
    סיומת .wav, גם כשמעלים בפועל קובץ mp3 עם convertAudio=1 (השרת ממיר
    אוטומטית לפורמט הטלפוני; ה-path רק אומר איך הקובץ *ייקרא* אחרי ההמרה).
    זו ככל הנראה הסיבה האמיתית לכל הכישלונות הקודמים — לא השלוחה, לא
    האותיות בשם, אלא הסיומת עצמה.

    חשוב גם לגבי תזמון: לפי דיווח אמיתי בפורום המפתחים, אחרי UpdateExtension
    לוקח עד כ-2 דקות עד שהשלוחה החדשה 'נתפסת' בפועל אצל ימות. מכיוון שזו
    שיחת טלפון חיה עם timeout אמיתי מצד המרכזיה — אסור לנו לחכות כל כך הרבה
    זמן בתוך הבקשה עצמה. לכן: בתוך השיחה מנסים רק פעמיים, מהר (0s, 3s). אם
    זו שלוחה חדשה וזה נכשל, מפעילים warm-up ברקע (לא חוסם את התשובה
    למרכזיה) שממשיך לנסות במשך עד כ-2 דקות."""
    file_num = await _next_yemot_file_number()
    dest_filename = f"{file_num}.wav"  # גם ב-path וגם בשם הקובץ שנשלח ב-multipart — זהים בכוונה עכשיו
    yemot_path = f"{YEMOT_UPLOAD_FOLDER}/{dest_filename}"
    is_new_folder = YEMOT_UPLOAD_FOLDER not in _yemot_dirs_ensured
    await _yemot_ensure_dir(YEMOT_UPLOAD_FOLDER)

    async def _do_upload(token: str) -> httpx.Response:
        assert http_client is not None
        # ניסוי: שם הקובץ ב-multipart זהה לחלוטין לזה שב-path (כולל סיומת
        # .wav), במקום לשלוח .mp3 בפועל עם .wav רק ביעד. אם ימות מזהה את
        # הקובץ בפועל בסניפינג בינארי (לא לפי שם) ו-convertAudio=1 עדיין
        # ממיר נכון, זה לא אמור לשבור כלום; אם אי-ההתאמה בין השמות היא
        # שגרמה ל-IllegalStateException, זה אמור לפתור.
        return await http_client.post(
            f"{YEMOT_API_BASE}/UploadFile",
            data={"token": token, "path": yemot_path, "convertAudio": "1"},
            files={"file": (dest_filename, audio_bytes, "audio/mpeg")},
            timeout=30.0,
        )

    token = await _yemot_login()
    if not token:
        return None

    # בתוך השיחה החיה: ניסיון מהיר בלבד, לא לחכות דקות על חשבון המתקשר.
    quick_delays = [0, 3]
    last_error_data = None
    for i, delay in enumerate(quick_delays):
        if delay:
            await asyncio.sleep(delay)
        try:
            resp = await _do_upload(token)
            data = resp.json()
        except (httpx.HTTPError, asyncio.TimeoutError, json.JSONDecodeError) as e:
            logger.error("Yemot UploadFile request failed for %s: %s", video_id, e)
            return None

        if data.get("responseStatus") == "OK":
            logger.info("✅ Yemot UploadFile success for %s → %s (attempt %d/%d)",
                        video_id, yemot_path, i + 1, len(quick_delays))
            return yemot_path

        last_error_data = data
        logger.warning("Yemot UploadFile attempt %d/%d failed for %s (path=%r): %s",
                        i + 1, len(quick_delays), video_id, yemot_path, _safe_json_snippet_early(data))
        token = await _yemot_login(force=True) or token

    logger.warning("Yemot UploadFile quick attempts exhausted for %s (path=%r): %s",
                    video_id, yemot_path, _safe_json_snippet_early(last_error_data))

    if is_new_folder:
        # שלוחה חדשה שכנראה עדיין לא הופצה — ממשיכים לנסות ברקע (בלי לעכב
        # את התשובה הנוכחית למרכזיה), כדי שהבקשה הבאה תעבוד חלק.
        logger.info("🕒 Starting background warm-up retry for new Yemot folder %s (up to ~2 min)",
                    YEMOT_UPLOAD_FOLDER)
        asyncio.create_task(_yemot_upload_warmup(video_id, audio_bytes, yemot_path))

    return None


async def _yemot_upload_warmup(video_id: str, audio_bytes: bytes, yemot_path: str) -> None:
    """ריצה ברקע בלבד (לא במסגרת שיחה חיה): ממשיכה לנסות להעלות עם השהיות
    גדלות עד שהשלוחה החדשה מופצת בימות (עד כ-2 דקות לפי דיווחים בפורום),
    ושומרת ל-DB אם וכשמצליחה — כדי שבקשות עתידיות ימצאו אותה בקאש מיד."""
    # שם הקובץ ב-multipart זהה עכשיו לזה שב-path (גם הוא .wav) — ר' הערה
    # מקבילה ב-_yemot_upload_file.
    dest_filename = yemot_path.rsplit("/", 1)[-1]
    for delay in (10, 20, 30, 45):
        await asyncio.sleep(delay)
        token = await _yemot_login()
        if not token:
            continue
        try:
            assert http_client is not None
            resp = await http_client.post(
                f"{YEMOT_API_BASE}/UploadFile",
                data={"token": token, "path": yemot_path, "convertAudio": "1"},
                files={"file": (dest_filename, audio_bytes, "audio/mpeg")},
                timeout=30.0,
            )
            data = resp.json()
        except (httpx.HTTPError, asyncio.TimeoutError, json.JSONDecodeError) as e:
            logger.warning("Yemot warm-up upload attempt failed: %s", e)
            continue

        if data.get("responseStatus") == "OK":
            logger.info("✅ Yemot warm-up upload succeeded for %s → %s — folder is ready for future requests",
                        video_id, yemot_path)
            await run_db_query(
                "INSERT OR REPLACE INTO yemot_uploads (video_id, yemot_path, uploaded_at) VALUES (?, ?, ?)",
                (video_id, yemot_path, utcnow().isoformat()),
                commit=True,
            )
            return
        logger.warning("Yemot warm-up upload still failing: %s", _safe_json_snippet_early(data))

    logger.error(
        "Yemot warm-up gave up after ~105s for folder %s — the path format is very likely still wrong, "
        "not just a propagation delay. Use /debug/yemot with target_ext to find the correct one.",
        YEMOT_UPLOAD_FOLDER,
    )


async def _yemot_delete_file(path: str) -> bool:
    """מוחק קובץ מהאחסון של ימות המשיח. מאומת מול הפורום הרשמי:
    FileAction?token=...&action=delete&what=<path>"""
    token = await _yemot_login()
    if not token:
        return False
    try:
        assert http_client is not None
        resp = await http_client.post(
            f"{YEMOT_API_BASE}/FileAction",
            data={"token": token, "action": "delete", "what": path},
            timeout=10.0,
        )
        data = resp.json()
        ok = data.get("responseStatus") == "OK"
        if ok:
            logger.info("🗑️ Yemot file deleted: %s", path)
        else:
            logger.warning("Yemot delete failed for %s: %s", path, _safe_json_snippet_early(data))
        return ok
    except (httpx.HTTPError, asyncio.TimeoutError, json.JSONDecodeError) as e:
        logger.warning("Yemot FileAction(delete) request failed for %s: %s", path, e)
        return False


async def _track_session_upload(session_key: str, video_id: str) -> None:
    """רושם ש-video_id הועלה לימות במהלך השיחה של session_key, כדי שנוכל
    למחוק אותו אוטומטית כשהמתקשר יתנתק (ר' cleanup_session_uploads)."""
    await run_db_query(
        "INSERT OR IGNORE INTO session_uploads (session_key, video_id) VALUES (?, ?)",
        (session_key, video_id), commit=True,
    )


async def cleanup_session_uploads(session_key: str) -> None:
    """נקרא ב-hangup: מוחק מימות את כל השירים שהועלו במהלך השיחה הזו, ומנקה
    גם את קאש ה-DB שלנו (yemot_uploads) כדי שהפעם הבאה תוריד ותעלה מחדש —
    זה בדיוק הטרייד-אוף שביקשתם: פחות אחסון תפוס, יותר קריאות API בכל ניגון."""
    if not YEMOT_ENABLED or not YEMOT_AUTO_DELETE_AFTER_PLAY:
        return
    rows = await run_db_query(
        "SELECT video_id FROM session_uploads WHERE session_key = ?", (session_key,), fetchall=True
    )
    if not rows:
        return
    for (video_id,) in rows:
        cached = await run_db_query("SELECT yemot_path FROM yemot_uploads WHERE video_id = ?", (video_id,))
        if cached:
            await _yemot_delete_file(cached[0])
            await run_db_query("DELETE FROM yemot_uploads WHERE video_id = ?", (video_id,), commit=True)
    await run_db_query("DELETE FROM session_uploads WHERE session_key = ?", (session_key,), commit=True)
    logger.info("🧹 Cleaned up %d uploaded track(s) for session=%s after hangup", len(rows), session_key)


async def ensure_uploaded_to_yemot(video_id: str, session_key: Optional[str] = None) -> Optional[str]:
    """נקודת הכניסה היחידה שצריך לקרוא לפני ניגון: מחזיר נתיב קובץ בימות
    (מהקאש/DB אם כבר הועלה בעבר, אחרת מוריד מיוטיוב ומעלה עכשיו). אם
    session_key סופק ומחיקה-אוטומטית מופעלת, רושמים אותו למחיקה בסוף השיחה."""
    if not YEMOT_ENABLED:
        return None

    cached = await run_db_query("SELECT yemot_path FROM yemot_uploads WHERE video_id = ?", (video_id,))
    if cached:
        if session_key and YEMOT_AUTO_DELETE_AFTER_PLAY:
            await _track_session_upload(session_key, video_id)
        return cached[0]

    audio_bytes = await download_audio_bytes(video_id)
    if not audio_bytes:
        logger.error("Could not download audio for %s — cannot upload to Yemot", video_id)
        return None

    yemot_path = await _yemot_upload_file(video_id, audio_bytes)
    if not yemot_path:
        return None

    await run_db_query(
        "INSERT OR REPLACE INTO yemot_uploads (video_id, yemot_path, uploaded_at) VALUES (?, ?, ?)",
        (video_id, yemot_path, utcnow().isoformat()),
        commit=True,
    )
    if session_key and YEMOT_AUTO_DELETE_AFTER_PLAY:
        await _track_session_upload(session_key, video_id)
    return yemot_path


async def yemot_download_file(path: str) -> Optional[bytes]:
    """מוריד קובץ (כמו הקלטה גולמית) מהאחסון של ימות המשיח, לפי הנתיב שחוזר
    ב-ValName אחרי read מסוג record. משתמש באותו token/login של ה-upload."""
    if not YEMOT_ENABLED:
        return None
    token = await _yemot_login()
    if not token:
        return None
    try:
        assert http_client is not None
        resp = await http_client.post(
            f"{YEMOT_API_BASE}/DownloadFile",
            data={"token": token, "path": path},
            timeout=15.0,
        )
        if resp.status_code != 200 or len(resp.content) == 0:
            logger.warning("Yemot DownloadFile failed for path=%r: status=%s", path, resp.status_code)
            return None
        return resp.content
    except (httpx.HTTPError, asyncio.TimeoutError) as e:
        logger.warning("Yemot DownloadFile request failed for path=%r: %s", path, e)
        return None


def _looks_like_yemot_path(value: str) -> bool:
    """היוריסטיקה לזיהוי אם ValName הוא נתיב קובץ בימות (אחרי הקלטה) ולא
    טקסט חופשי/ספרות: לפי תחיליות/סיומות טיפוסיות של מערכת הקבצים שלהם."""
    if not value:
        return False
    v = value.strip().lower()
    return (
        v.startswith("ivr2:") or v.startswith("/") or
        v.endswith(".wav") or v.endswith(".mp3") or v.endswith(".ogg")
    )


async def transcribe_audio_bytes(audio_bytes: bytes, filename: str = "recording.wav") -> Optional[str]:
    """מתמלל הקלטה קולית בעברית דרך Groq (Whisper) — יש להם free tier אמיתי,
    בלי לשלם על כל חיפוש כמו ב-voice המובנה של ימות. לא נבדק אמפירית מהסביבה
    הזו (groq.com לא בטווח הרשת של ה-sandbox), אבל עוקב אחרי ה-API המתועד
    שלהם (תואם-OpenAI: POST /openai/v1/audio/transcriptions, multipart)."""
    if not GROQ_API_KEY:
        return None
    try:
        assert http_client is not None
        resp = await http_client.post(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            data={"model": GROQ_STT_MODEL, "language": "he", "response_format": "json"},
            files={"file": (filename, audio_bytes, "audio/wav")},
            timeout=20.0,
        )
        if resp.status_code != 200:
            logger.warning("Groq transcription failed: %s %s", resp.status_code, resp.text[:200])
            return None
        text = (resp.json() or {}).get("text", "").strip()
        return text or None
    except (httpx.HTTPError, asyncio.TimeoutError, json.JSONDecodeError) as e:
        logger.warning("Groq transcription request failed: %s", e)
        return None


async def resolve_search_query_from_valname(val_name: str) -> Optional[str]:
    """נקודת הכניסה היחידה לפענוח מה שחזר מהשלוחה של 'אמרו שם שיר':
    - אם STT_ENABLED והערך נראה כמו נתיב קובץ בימות (מצב record חינמי) —
      מורידים את ההקלטה ומתמללים בעצמנו דרך Groq.
    - אחרת (או אם התמלול נכשל) — מניחים שזה כבר טקסט מזוהה (מצב voice
      המובנה בתשלום של ימות, או פלטפורמה אחרת), ומשתמשים בו כמות שהוא."""
    if STT_ENABLED and _looks_like_yemot_path(val_name):
        audio_bytes = await yemot_download_file(val_name.strip())
        if not audio_bytes:
            logger.error("Could not download recording from Yemot at path=%r for transcription", val_name)
            return None
        text = await transcribe_audio_bytes(audio_bytes)
        if text:
            logger.info("🎙️ Free transcription result: %r (from recording %s)", text, val_name)
        else:
            logger.warning("Transcription returned empty/failed for recording %s", val_name)
        return text

    return val_name


# ==========================================
# 💳 סליקת תשלומים (אופציונלי)
# ==========================================
def _safe_json_snippet(data, limit: int = 2000) -> str:
    """שומר תמיד JSON תקין (אף פעם לא נחתך באמצע), גם כשהתשובה מהספק ענקית —
    כדי שקריאה עתידית עם json.loads() לא תיפול."""
    try:
        full = json.dumps(data, ensure_ascii=False)
    except (TypeError, ValueError):
        full = str(data)
    if len(full) <= limit:
        return full
    return json.dumps({"_truncated": True, "raw_prefix": full[: max(0, limit - 60)]}, ensure_ascii=False)


async def charge_customer(phone: str, amount_ils: float) -> Tuple[bool, str, str]:
    """
    מבצע חיוב מול ספק הסליקה שהוגדר ב-ENV. זהו אדפטר גנרי ל-REST endpoint —
    התאימו את שדות הבקשה/תשובה למסמכי ה-API של הספק שלכם בפועל
    (Cardcom / Tranzila / Yaad Sarig / PayMe...). כל מה שסביב (רישום עסקה,
    לוגים, טיפול בשגיאות) כבר מוכן ולא צריך לגעת בו.
    מחזיר: (success, message_for_caller, tx_id)
    """
    tx_id = secrets.token_hex(8)
    if not CLEARING_ENABLED:
        return False, "שירות התשלומים אינו זמין כרגע", tx_id

    await run_db_query(
        "INSERT INTO payments (tx_id, phone, amount, status, created_at) VALUES (?, ?, ?, 'pending', ?)",
        (tx_id, phone, amount_ils, utcnow().isoformat()),
        commit=True,
    )
    try:
        assert http_client is not None
        resp = await http_client.post(
            CLEARING_API_URL,
            json={
                "terminal": CLEARING_TERMINAL,
                "api_key": CLEARING_API_KEY,
                "amount": amount_ils,
                "currency": "ILS",
                "phone": phone,
                "reference": tx_id,
            },
            timeout=10.0,
        )
        try:
            data = resp.json()
        except json.JSONDecodeError:
            data = {"raw": resp.text[:500]}

        success = resp.status_code == 200 and bool(
            data.get("success") or str(data.get("status", "")).lower() == "approved"
        )
        status = "approved" if success else "declined"
        await run_db_query(
            "UPDATE payments SET status = ?, provider_response = ? WHERE tx_id = ?",
            (status, _safe_json_snippet(data), tx_id),
            commit=True,
        )
        msg = "התשלום אושר, תודה רבה" if success else "התשלום נדחה, אנא נסו כרטיס אחר"
        return success, msg, tx_id
    except (httpx.HTTPError, asyncio.TimeoutError) as e:
        logger.error("Clearing charge failed for tx=%s: %s", tx_id, e)
        await run_db_query(
            "UPDATE payments SET status = 'error', provider_response = ? WHERE tx_id = ?",
            (str(e)[:500], tx_id),
            commit=True,
        )
        return False, "שגיאה בביצוע התשלום, אנא נסו שוב מאוחר יותר", tx_id


@app.post("/payment/webhook")
async def payment_webhook(request: Request):
    """נקודת קצה אופציונלית לאישורי תשלום א-סינכרוניים מספק הסליקה (אם הוא
    תומך ב-webhook/IPN). מגן בסיסי: דורש שיתאים tx_id קיים בטבלה. הוסיפו כאן
    אימות חתימה/סוד משותף לפי מסמכי הספק שלכם לפני production אמיתי."""
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(400, "Invalid JSON")

    tx_id = payload.get("reference") or payload.get("tx_id")
    if not tx_id:
        raise HTTPException(400, "Missing reference/tx_id")

    row = await run_db_query("SELECT tx_id FROM payments WHERE tx_id = ?", (tx_id,))
    if not row:
        raise HTTPException(404, "Unknown transaction")

    status = "approved" if payload.get("success") else "declined"
    await run_db_query(
        "UPDATE payments SET status = ?, provider_response = ? WHERE tx_id = ?",
        (status, _safe_json_snippet(payload), tx_id),
        commit=True,
    )
    return {"ok": True}


# ==========================================
# 📟 IVR Helpers
# ==========================================
def clean_text_for_ivr(text: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9\sא-ת]", " ", text or "")
    return re.sub(r"\s+", " ", cleaned).strip()[:200]


def make_ivr_read_command(text: str, min_dig: str, max_dig: str, sec: int, mode: str) -> str:
    clean = clean_text_for_ivr(text)
    if mode.lower() == "voice":
        return f"read=t-{clean}=ValName,no,50,1,{sec},voice,no"
    return f"read=t-{clean}=ValName,no,{max_dig},{min_dig},{sec},{mode.lower()},no"


def make_ivr_record_command(text: str, max_seconds: int) -> str:
    """מקליטה גולמית בלי זיהוי דיבור (record) — בחינם אצל ימות המשיח, בניגוד
    ל-voice (שגובה כסף). לפי אישור מפורש בפורום המפתחים של ימות: כשמסתיימת
    הקלטה, הנתיב שבו היא נשמרה חוזר ב-ValName בבקשה הבאה, ולא התמלול עצמו —
    אנחנו מורידים את הקובץ עם DownloadFile ומתמללים בעצמנו (ר' STT_ENABLED)."""
    clean = clean_text_for_ivr(text)
    return f"read=t-{clean}=ValName,no,,,{max_seconds},record,no"


def _get_url_based_play_command(video_id: str, request: Request) -> Optional[str]:
    """השיטה הישנה: מחזירים URL חיצוני שהמרכזיה אמורה להביא בעצמה. מאומת
    שימות המשיח *לא* תומכת בזה בכלל — זו רק שכבת fallback/תאימות לפלטפורמות
    אחרות, או למקרה ש-YEMOT_ENABLED=False."""
    if PUBLIC_BASE_URL:
        base = PUBLIC_BASE_URL
    else:
        raw_host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
        # מאחורי כמה פרוקסים בשרשרת (למשל Cloudflare + Render) הכותרת עלולה
        # להכיל כמה ערכים מופרדים בפסיק ("host1, host2") — הראשון הוא המקורי.
        host = raw_host.split(",")[0].strip().split(":")[0].lower()
        if TRUSTED_HOSTS and host not in TRUSTED_HOSTS:
            logger.error("Rejected untrusted Host header for play command: %r", host)
            return None
        protocol = request.headers.get("x-forwarded-proto") or ("http" if "localhost" in host else "https")
        port = ":10000" if "localhost" in host else ""
        base = f"{protocol}://{host}{port}"
    return PLAY_COMMAND_TEMPLATE.format(base=base, video_id=video_id)


def _generic_error_command() -> str:
    return make_ivr_read_command("משהו השתבש אנא נסו שוב מאוחר יותר", "1", "1", 5, "digits")


async def _play_command_or_error(video_id: str, request: Request, session_key: Optional[str] = None) -> str:
    """נקודת הכניסה היחידה לבניית פקודת ניגון: אם ימות מוגדר, מעלים קודם
    (או משתמשים בהעלאה קיימת) ומנגנים לפי נתיב פנימי — השיטה היחידה שבאמת
    עובדת בימות המשיח. אם ימות לא מוגדר, או שההעלאה נכשלה, נופלים חזרה
    לשיטת ה-URL הישנה (במקום פשוט להיכשל). session_key משמש למחיקה אוטומטית
    של השיר מימות ברגע שהמתקשר הזה מנתק את השיחה."""
    if YEMOT_ENABLED:
        yemot_path = await ensure_uploaded_to_yemot(video_id, session_key)
        if yemot_path:
            return YEMOT_PLAY_TEMPLATE.format(yemot_path=yemot_path, video_id=video_id)
        logger.warning(
            "Yemot upload failed/unavailable for %s — falling back to URL-based play command "
            "(won't work on real Yemot, but keeps the call from dying silently)", video_id,
        )

    cmd = _get_url_based_play_command(video_id, request)
    return cmd if cmd is not None else _generic_error_command()


# ==========================================
# 🧹 Background cleanup + watchdog
# ==========================================
async def _cleanup_once() -> None:
    cutoff = (utcnow() - timedelta(hours=SESSION_TTL_HOURS)).isoformat()
    await run_db_query("DELETE FROM sessions WHERE last_active < ?", (cutoff,), commit=True)

    one_hour_ago = (utcnow() - timedelta(hours=1)).isoformat()
    await run_db_query("DELETE FROM rate_limits WHERE timestamp < ?", (one_hour_ago,), commit=True)

    for ph in list(_phone_locks.keys()):
        lock = _phone_locks.get(ph)
        if lock and not lock.locked():
            _phone_locks.pop(ph, None)


async def active_session_cleanup():
    while True:
        try:
            await _cleanup_once()
        except Exception as e:
            logger.error("Cleanup iteration failed: %s", e)
        await asyncio.sleep(1800)


async def _cleanup_supervisor():
    """עוטף את משימת הניקוי ומרים אותה מחדש אם היא קורסת מסיבה בלתי צפויה,
    כדי שלעולם לא "נשכח" עם sessions/rate_limits שהולכים ותופחים."""
    backoff = 5
    while True:
        try:
            await active_session_cleanup()
            return  # לא אמור לקרות (לולאה אינסופית), אבל ליתר ביטחון
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("Cleanup task crashed, restarting in %ss: %s", backoff, e)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 300)


# ==========================================
# 🎛️ States
# ==========================================
class State(str, Enum):
    CHECK_AUTH = "CHECK_AUTH"
    MAIN_MENU = "MAIN_MENU"
    WAITING_FOR_SEARCH = "WAITING_FOR_SEARCH"
    PLAYING_TRACKS = "PLAYING_TRACKS"
    WAITING_FOR_DONATION_AMOUNT = "WAITING_FOR_DONATION_AMOUNT"


MAIN_MENU_TEXT = (
    "לחיפוש קולי 1 • שירים חדשים 2 • מועדפים 3"
    + (" • תרומה 9" if CLEARING_ENABLED else "")
)


async def _save_session(phone: str, state: str, playlist: List[dict], index: int) -> None:
    """כותב state + playlist_json + current_index יחד, תמיד באותה קריאה —
    כדי שה-DB וה-RAM לעולם לא יתפצלו לגרסאות לא-מסונכרנות (state ישן מול
    פלייליסט חדש וכד')."""
    await run_db_query(
        "UPDATE sessions SET state = ?, playlist_json = ?, current_index = ? WHERE phone = ?",
        (state, json.dumps(playlist, ensure_ascii=False), index, phone),
        commit=True,
    )


async def _load_or_create_session(phone: str, is_whitelisted: bool) -> Tuple[str, List[dict], int]:
    session_data = await run_db_query(
        "SELECT state, playlist_json, current_index FROM sessions WHERE phone = ?", (phone,)
    )
    if not session_data:
        state = State.MAIN_MENU.value if is_whitelisted else State.CHECK_AUTH.value
        await run_db_query(
            "INSERT INTO sessions (phone, state, playlist_json, current_index, last_active) VALUES (?, ?, ?, ?, ?)",
            (phone, state, "[]", 0, utcnow().isoformat()),
            commit=True,
        )
        return state, [], 0

    state, playlist_json, index = session_data
    try:
        playlist = json.loads(playlist_json) if playlist_json else []
    except json.JSONDecodeError:
        playlist = []
    return state, playlist, (index or 0)


# ==========================================
# 📞 Main IVR Endpoint
# ==========================================
@app.get("/youtube", response_class=PlainTextResponse)
async def handle_ivr(request: Request, ApiPhone: str = Query(None), hangup: str = Query(None)):
    if not ApiPhone:
        return "OK"

    raw_phone = ApiPhone.strip()
    is_anonymous = raw_phone.lower() in ANONYMOUS_PHONE_VALUES

    if is_anonymous:
        # מתקשר עם מספר חסום. אי אפשר לזהות אותו לאורך זמן (וגם לא כדאי —
        # אין למה "לזכור"), אבל חובה לתת לו session_key ייחודי לשיחה הנוכחית
        # (ApiCallId/ApiYFCallId) ולא "0" קבוע — אחרת כל המתקשרים החסויים
        # היו חולקים session אחד וקופצים על הפלייליסט/מצב אחד של השני.
        call_id = (
            request.query_params.get("ApiCallId")
            or request.query_params.get("ApiYFCallId")
            or ""
        ).strip()
        if not call_id:
            logger.warning("Anonymous caller with no call id — rejecting")
            return "OK"
        session_key = f"anon:{call_id[:64]}"
    else:
        if not PHONE_RE.match(raw_phone):
            logger.warning("Rejected malformed phone: %r", raw_phone)
            return "OK"
        session_key = raw_phone

    if hangup == "yes":
        # המתקשר יצא מהקו — אם הוגדר מחיקה אוטומטית אחרי השמעה, מוחקים עכשיו
        # מימות המשיח את כל השירים שהועלו במהלך השיחה הזו (שחרור מקום אחסון).
        if YEMOT_AUTO_DELETE_AFTER_PLAY:
            asyncio.create_task(cleanup_session_uploads(session_key))
        return "OK"

    val_params = [v for k, v in request.query_params.multi_items() if k == "ValName"]
    ValName = (val_params[-1] if val_params else None)
    if ValName is not None:
        ValName = ValName.strip()[:150]

    logger.info("📞 Phone: %s | Session: %s | ValName: %r", raw_phone, session_key, ValName)

    try:
        if await is_rate_limited(session_key):
            result = make_ivr_read_command("בוצעו יותר מדי פעולות אנא המתן מעט", "1", "1", 5, "digits")
        else:
            async with get_phone_lock(session_key):
                result = await _handle_ivr_locked(request, session_key, ValName, is_anonymous)
    except Exception as e:
        logger.exception("Unhandled error in IVR handler for session=%s: %s", session_key, e)
        result = _generic_error_command()

    # לוג מלא של הפקודה שמוחזרת למרכזיה — כך אפשר לראות בדיוק אילו תווים
    # קיבלה ימות המשיח, ולוודא שהפורמט תואם למה שאתם מריצים ידנית בבדיקות.
    logger.info("📤 Returning to IVR (session=%s): %s", session_key, result)
    return result


async def _handle_ivr_locked(
    request: Request, ApiPhone: str, ValName: Optional[str], is_anonymous: bool = False
) -> str:
    if is_anonymous:
        # אין ל-caller חסוי זהות יציבה שאפשר לשמור/לאשר לצמיתות — תמיד דורשים
        # קוד גישה, ולעולם לא כותבים אותו לטבלת users (שם היא לא בעלת משמעות).
        is_whitelisted = False
        stored_access_code = DEFAULT_ACCESS_CODE
    else:
        user_data = await run_db_query("SELECT authorized, access_code FROM users WHERE phone = ?", (ApiPhone,))
        is_whitelisted = bool(user_data and user_data[0] == 1)
        stored_access_code = user_data[1] if user_data else DEFAULT_ACCESS_CODE

    state, playlist, index = await _load_or_create_session(ApiPhone, is_whitelisted)

    await run_db_query(
        "UPDATE sessions SET last_active = ? WHERE phone = ?", (utcnow().isoformat(), ApiPhone), commit=True
    )

    # ---------- Auth flow ----------
    if not is_whitelisted and state == State.CHECK_AUTH.value:
        if ValName and ValName == stored_access_code:
            if not is_anonymous:
                await run_db_query(
                    "INSERT OR REPLACE INTO users (phone, authorized, access_code) VALUES (?, 1, ?)",
                    (ApiPhone, stored_access_code),
                    commit=True,
                )
            state = State.MAIN_MENU.value
            await _save_session(ApiPhone, state, [], 0)
            ValName = None
        else:
            msg = "קוד שגוי אנא נסה שנית" if ValName else "אנא הקש את קוד הגישה"
            return make_ivr_read_command(msg, "4", "4", 10, "digits")

    # ---------- MAIN MENU ----------
    if state == State.MAIN_MENU.value:
        if ValName == "1":
            await _save_session(ApiPhone, State.WAITING_FOR_SEARCH.value, [], 0)
            if STT_ENABLED:
                # מצב הקלטה חינמי (record) — בלי voice בתשלום של ימות. תמלול
                # עצמי דרך Groq קורה בשלב הבא (WAITING_FOR_SEARCH) לפי הנתיב
                # שיחזור ב-ValName.
                return make_ivr_record_command("אנא אמרו את שם השיר לאחר הצליל", max_seconds=10)
            return make_ivr_read_command("אנא אמרו את שם השיר לאחר הצליל", "1", "50", 10, "voice")

        elif ValName == "2":
            tracks = await search_youtube_innertube("שירים חסידיים חדשים", filter_newest=True)
            if not tracks:
                tracks = get_emergency_playlist()
            await _save_session(ApiPhone, State.PLAYING_TRACKS.value, tracks, 0)
            return await _play_command_or_error(tracks[0]["id"], request, ApiPhone)

        elif ValName == "3":
            favs = await run_db_query(
                "SELECT video_id, title FROM favorites WHERE phone = ? ORDER BY created_at DESC",
                (ApiPhone,), fetchall=True,
            )
            if not favs:
                return make_ivr_read_command("רשימת המועדפים ריקה", "1", "1", 4, "digits")
            tracks = [{"id": f[0], "title": f[1], "duration": "00:00", "author": ""} for f in favs]
            await _save_session(ApiPhone, State.PLAYING_TRACKS.value, tracks, 0)
            return await _play_command_or_error(tracks[0]["id"], request, ApiPhone)

        elif ValName == "9" and CLEARING_ENABLED:
            await _save_session(ApiPhone, State.WAITING_FOR_DONATION_AMOUNT.value, [], 0)
            return make_ivr_read_command(
                f"הקישו סכום לתרומה בשקלים בין {int(DONATION_MIN_ILS)} ל {int(DONATION_MAX_ILS)} ואז סולמית",
                "1", "6", 12, "digits",
            )

        else:
            return make_ivr_read_command(MAIN_MENU_TEXT, "1", "1", 10, "digits")

    # ---------- SEARCH ----------
    elif state == State.WAITING_FOR_SEARCH.value:
        if not ValName or len(ValName) < 2 or ValName in ("1", "2", "*", "#"):
            retry_cmd = (
                make_ivr_record_command("לא קלטתי בבירור, אנא אמרו שוב", max_seconds=10)
                if STT_ENABLED else
                make_ivr_read_command("לא קלטתי בבירור, אנא אמרו שוב", "1", "50", 10, "voice")
            )
            return retry_cmd

        query = await resolve_search_query_from_valname(ValName)
        if not query:
            # התמלול נכשל (למשל לא הצלחנו להוריד/לתמלל את ההקלטה) — לא נתקע
            # את המתקשר, פשוט מבקשים ניסיון נוסף באותה שיטה.
            retry_cmd = (
                make_ivr_record_command("לא הצלחתי להבין, אנא נסו שוב", max_seconds=10)
                if STT_ENABLED else
                make_ivr_read_command("לא הצלחתי להבין, אנא נסו שוב", "1", "50", 10, "voice")
            )
            return retry_cmd

        tracks = await search_youtube_innertube(query)
        if not tracks:
            tracks = get_emergency_playlist()

        await _save_session(ApiPhone, State.PLAYING_TRACKS.value, tracks, 0)
        return await _play_command_or_error(tracks[0]["id"], request, ApiPhone)

    # ---------- DONATION AMOUNT ----------
    elif state == State.WAITING_FOR_DONATION_AMOUNT.value:
        if not ValName or not AMOUNT_RE.match(ValName):
            return make_ivr_read_command("סכום לא תקין, אנא הקישו שוב מספר בשקלים", "1", "6", 10, "digits")

        amount = float(ValName)
        if amount < DONATION_MIN_ILS or amount > DONATION_MAX_ILS:
            return make_ivr_read_command(
                f"הסכום חייב להיות בין {int(DONATION_MIN_ILS)} ל {int(DONATION_MAX_ILS)} שקלים",
                "1", "6", 10, "digits",
            )

        success, message, tx_id = await charge_customer(ApiPhone, amount)
        logger.info("Payment attempt tx=%s phone=%s amount=%s success=%s", tx_id, ApiPhone, amount, success)
        await _save_session(ApiPhone, State.MAIN_MENU.value, [], 0)
        return make_ivr_read_command(message, "1", "1", 6, "digits")

    # ---------- PLAYING ----------
    elif state == State.PLAYING_TRACKS.value:
        if not playlist:
            playlist = get_emergency_playlist()
            index = 0

        total = len(playlist)
        index = (index % total) if total > 0 else 0

        if ValName == "2":
            index = (index - 1) % total
        elif ValName == "3":
            # משהים: לא כותבים ל-DB (אין שינוי state/playlist/index אמיתי)
            return make_ivr_read_command("הושהה • להמשך 4 • תפריט 0", "1", "1", 20, "digits")
        elif ValName == "5":
            random.shuffle(playlist)
            index = 0
        elif ValName == "6":
            curr = playlist[index]
            await run_db_query(
                "INSERT OR IGNORE INTO favorites (phone, video_id, title, created_at) VALUES (?, ?, ?, ?)",
                (ApiPhone, curr["id"], curr["title"], utcnow().isoformat()),
                commit=True,
            )
            # אין שינוי לפלייליסט/אינדקס — רק פידבק, וממשיכים לנגן את אותו שיר.
            return make_ivr_read_command("נוסף למועדפים • ממשיך...", "1", "1", 3, "digits")
        elif ValName == "0":
            await _save_session(ApiPhone, State.MAIN_MENU.value, [], 0)
            return make_ivr_read_command("חוזר לתפריט הראשי", "1", "1", 3, "digits")
        else:
            # ValName == "1" (הבא) או "" (ללא קלט / טיימאאוט) => מעבר לשיר הבא כברירת מחדל
            index = (index + 1) % total

        # כתיבה אטומית יחידה: state + playlist_json + current_index תמיד יחד,
        # כך ש-DB וה-RAM לעולם לא מתפצלים לגרסאות לא-מסונכרנות.
        await _save_session(ApiPhone, State.PLAYING_TRACKS.value, playlist, index)
        return await _play_command_or_error(playlist[index]["id"], request, ApiPhone)

    # מצב לא מוכר — נאפס בבטחה חזרה לתפריט במקום לתקוע את השיחה
    logger.warning("Unknown session state %r for phone=%s — resetting", state, ApiPhone)
    await _save_session(ApiPhone, State.MAIN_MENU.value, [], 0)
    return make_ivr_read_command(MAIN_MENU_TEXT, "1", "1", 10, "digits")


# ==========================================
# ❤️ Health check
# ==========================================
@app.get("/debug/search")
async def debug_search(q: str = Query(...), token: str = Query(None), verbose: int = Query(0)):
    """כלי אבחון מהיר: בודק מה מנוע החיפוש בפועל מחזיר בלי לחכות לשיחת טלפון.
    מנוטרל לגמרי (404) אם IVR_DEBUG_TOKEN לא הוגדר — לא נחשף בטעות בפרודקשן.
    verbose=1 בודק כל שכבה בנפרד (מועיל לאבחן איזו שכבה בדיוק נכשלת),
    אבל עלול לצרוך quota של Data API — להשתמש בזהירות."""
    if not DEBUG_TOKEN:
        raise HTTPException(404)
    if token != DEBUG_TOKEN:
        raise HTTPException(403, "Invalid token")

    if verbose:
        result = {}
        if YOUTUBE_DATA_API_ENABLED:
            t = await search_youtube_data_api(q)
            result["data_api_v3"] = {"count": len(t), "sample": t[:2]}
        else:
            result["data_api_v3"] = "disabled (no YOUTUBE_DATA_API_KEY)"
        t = await _search_via_innertube_scrape(q, False)
        result["innertube_scrape"] = {"count": len(t), "sample": t[:2]}
        t = await search_invidious_fallback(q)
        result["invidious"] = {"count": len(t), "sample": t[:2]}
        t = await search_piped_fallback(q)
        result["piped"] = {"count": len(t), "sample": t[:2]}
        return result

    tracks = await search_youtube_innertube(q)
    return {"query": q, "count": len(tracks), "tracks": tracks}


@app.get("/debug/yemot")
async def debug_yemot(token: str = Query(None), target_ext: str = Query(None)):
    """כלי אבחון מקיף לימות המשיח: מריץ Login, ואז מנסה כמה וריאציות שונות
    של פורמט הנתיב על CreateIVR2Dir ו-UploadFile (עם קובץ דמה קטן), ומחזיר
    את *כל* התגובות הגולמיות מהשרת בבת אחת. המטרה: לגלות באיזה פורמט נתיב
    ימות המשיח שלכם בפועל מצפה, בלי עוד סבב שלם של שיחת טלפון + דפלוי.

    target_ext (אופציונלי, מומלץ מאוד): מספר שלוחה אמיתי שיצרתם ידנית בפאנל
    הניהול של ימות (למשל '90' או '9/5') — בודק גם נתיבים שמצביעים עליה.
    זו ההשערה הכי סבירה כרגע: שחייבים שלוחה אמיתית קיימת, לא תיקייה חופשית.

    מנוטרל לגמרי (404) אם IVR_DEBUG_TOKEN לא הוגדר."""
    if not DEBUG_TOKEN:
        raise HTTPException(404)
    if token != DEBUG_TOKEN:
        raise HTTPException(403, "Invalid token")
    if not YEMOT_ENABLED:
        raise HTTPException(400, "YEMOT_SYSTEM_NUMBER/YEMOT_PASSWORD not configured")

    assert http_client is not None
    report: dict = {}

    yemot_token = await _yemot_login(force=True)
    report["login"] = {
        "ok": bool(yemot_token),
        "token_preview": (yemot_token[:6] + "...") if yemot_token else None,
    }
    if not yemot_token:
        report["conclusion"] = "Login עצמו נכשל — בדקו YEMOT_SYSTEM_NUMBER/YEMOT_PASSWORD"
        return report

    # --- UpdateExtension: זו הפקודה האמיתית ליצירת שלוחה (מאומתת מול המדריך
    # הרשמי למתחילים ב-API) — לא CreateIVR2Dir, שכשל בעקביות בכל בדיקה קודמת
    # וכנראה endpoint מת/שגוי. אם target_ext סופק, בודקים גם אותו במפורש.
    # --- UpdateExtension עם type=playfile: מאומת מול רשימת סוגי השלוחות
    # הרשמית של ימות ('playfile = השמעת קבצים') — זו כנראה הסיבה שהעלאות
    # נכשלו גם אחרי שהשלוחה 'קיימת': בלי type היא לא בהכרח מיועדת לקבצים.
    # לא CreateIVR2Dir (endpoint שגוי/לא פעיל שכשל בעקביות בכל בדיקה קודמת).
    dir_variants = ["ivr2:/ai_songs", "ivr2:ai_songs", "ai_songs", "/ai_songs"]
    if target_ext:
        clean_ext = target_ext.strip().strip("/")
        dir_variants = [f"ivr2:/{clean_ext}", f"ivr2:{clean_ext}"] + dir_variants
    report["update_extension_attempts"] = []
    created_paths = []
    for variant in dir_variants:
        try:
            resp = await http_client.post(
                f"{YEMOT_API_BASE}/UpdateExtension",
                data={"token": yemot_token, "path": variant, "type": "playfile"},
                timeout=10.0,
            )
            data = resp.json()
        except (httpx.HTTPError, asyncio.TimeoutError, json.JSONDecodeError) as e:
            data = {"error": str(e)}
        report["update_extension_attempts"].append({"path_tried": variant, "type": "playfile", "raw_response": data})
        if isinstance(data, dict) and data.get("responseStatus") == "OK":
            created_paths.append(variant)

    report["propagation_note"] = (
        "לפי דיווח בפורום המפתחים, UpdateExtension יכול לקחת עד כ-2 דקות עד "
        "שהשלוחה החדשה 'נתפסת' בפועל. אם UploadFile למטה נכשל על שלוחה "
        "שדווקא הצליחה כאן (responseStatus=OK) — נסו שוב את /debug/yemot "
        "הזה בעוד דקה-שתיים לפני שמסיקים שהפורמט שגוי."
    )

    # --- וריאציות פורמט להעלאת קובץ (UploadFile) — קובץ דמה זעיר, לא שיר אמיתי ---
    # אם סיפקתם target_ext (מספר שלוחה אמיתי שיצרתם ידנית בפאנל הניהול של
    # ימות), בודקים גם אותו — זו ההשערה החזקה ביותר כרגע: שהנתיב חייב להצביע
    # על שלוחה אמיתית שכבר קיימת בחשבון, לא תיקייה וירטואלית חופשית.
    if created_paths:
        # המתנה קצרה (לא 2 דקות מלאות — זו קריאת דיבוג אינטראקטיבית, לא שיחה
        # חיה) לתת סיכוי סביר להפצה לפני שבודקים העלאה.
        logger.info("Waiting 10s for possible Yemot extension propagation before testing upload...")
        await asyncio.sleep(10)

    dummy_audio = b"\x00" * 200
    debug_file_num = await _next_yemot_file_number()
    # בודקים גם .mp3 וגם .wav ליעד — לפי תיעוד: הפורמט הטלפוני של ימות הוא
    # WAV, ולכן ה-*יעד* (path) צריך להסתיים ב-.wav גם כשמעלים mp3 בפועל
    # (עם convertAudio=1). זו ההשערה החדשה שאנחנו בודקים כאן במפורש.
    base_names = [f"ivr2:/ai_songs/{debug_file_num}", f"ivr2:ai_songs/{debug_file_num}",
                  f"ivr2:/{debug_file_num}", f"ivr2:{debug_file_num}",
                  f"ivr2:/1/{debug_file_num}", f"1/{debug_file_num}", f"{debug_file_num}"]
    if target_ext:
        clean_ext = target_ext.strip().strip("/")
        base_names = [f"ivr2:/{clean_ext}/{debug_file_num}", f"ivr2:{clean_ext}/{debug_file_num}",
                      f"{clean_ext}/{debug_file_num}"] + base_names
    upload_variants = [f"{base}.{ext}" for base in base_names for ext in ("wav", "mp3")]
    report["upload_attempts"] = []
    successful_path = None
    for variant in upload_variants:
        upload_filename = "test." + variant.rsplit(".", 1)[-1]  # שם הקובץ הנשלח תמיד תואם לסיומת היעד כאן
        try:
            resp = await http_client.post(
                f"{YEMOT_API_BASE}/UploadFile",
                data={"token": yemot_token, "path": variant, "convertAudio": "1"},
                files={"file": (upload_filename, dummy_audio, "audio/mpeg")},
                timeout=15.0,
            )
            data = resp.json()
        except (httpx.HTTPError, asyncio.TimeoutError, json.JSONDecodeError) as e:
            data = {"error": str(e)}
        report["upload_attempts"].append({"path_tried": variant, "raw_response": data})
        if isinstance(data, dict) and data.get("responseStatus") == "OK" and successful_path is None:
            successful_path = variant

    if successful_path:
        report["conclusion"] = (
            f"✅ נמצא פורמט עובד: {successful_path!r} — עדכנו YEMOT_UPLOAD_FOLDER "
            f"בהתאם (בלי שם הקובץ) והריצו שוב."
        )
        # מנקים את קובץ הבדיקה כדי לא להשאיר זבל בחשבון
        await _yemot_delete_file(successful_path)
    else:
        report["conclusion"] = (
            "❌ אף וריאציה לא הצליחה. אנא שילחו את כל ה-JSON הזה חזרה — "
            "התגובות הגולמיות מכילות את הרמז המדויק (exceptionMessage/message) "
            "לכך שימות דוחה, גם אם הוא ריק במקרים מסוימים."
        )

    return report


@app.get("/health")
async def health():
    db_ok = True
    try:
        await run_db_query("SELECT 1")
    except Exception:
        db_ok = False
    return {
        "status": "ok" if db_ok else "degraded",
        "db": db_ok,
        "clearing_enabled": CLEARING_ENABLED,
        "redis_enabled": _redis is not None,
        "whitelist_count": len(DEFAULT_WHITELIST),
        "youtube_proxy_configured": bool(YOUTUBE_PROXY_BASE),
        "youtube_data_api_enabled": YOUTUBE_DATA_API_ENABLED,
        "yemot_upload_enabled": YEMOT_ENABLED,
        "yemot_auto_delete_after_play": YEMOT_AUTO_DELETE_AFTER_PLAY if YEMOT_ENABLED else None,
        "free_stt_enabled": STT_ENABLED,
        "time": utcnow().isoformat(),
    }
