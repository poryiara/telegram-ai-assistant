import os
import json
import base64
import asyncio
import requests
import subprocess
import tempfile
import re
import datetime
import pytz
import jdatetime
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from google import genai
from google.genai import types
import edge_tts
from ddgs import DDGS

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
SESSION_STRING = os.environ["SESSION_STRING"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
OWNER_ID = int(os.environ["OWNER_ID"])
OWNER_NAME = os.environ.get("OWNER_NAME", "Poryia")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")

VOICE_NAME = os.environ.get("VOICE_NAME", "fa-IR-FaridNeural")
ALERT_DELAY = int(os.environ.get("ALERT_DELAY", "15"))

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "poryiara/telegram-ai-assistant")
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "main")
MEMORY_PATH = os.environ.get("MEMORY_PATH", "memory.json")

ai = genai.Client(api_key=GEMINI_API_KEY)

MODELS = [
    "gemini-3.8-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
]

OWNER_INFO_BASE = """
Name: Poryia Rahmani
Nickname: Poryia
Age group: Teenager / student
Location: Iran
Status: Student — still in school, no full-time job yet
Interests: Bodybuilding & gym, computers & tech, artificial intelligence, coding
Current projects:
  - This AI assistant (his own personal Telegram AI)
  - A VPN application
  - Several other small tech projects
Personality: Young, ambitious, hardworking, into self-improvement
Vibe: A young builder. Balances gym life and coding life.
Notes:
  - He's a student, so he's often busy with school during the day.
  - If someone's reaching out about a project or tech-related, that's usually important.
"""

# ============ GitHub Memory ============
def github_headers():
    return {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }

def fetch_memory():
    empty = {"learned_facts": {}, "owner_notes": [], "voice_preferences": {}}
    if not GITHUB_TOKEN:
        print("⚠️ No GITHUB_TOKEN — using empty memory")
        return empty, None
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{MEMORY_PATH}"
    try:
        r = requests.get(url, headers=github_headers(), params={"ref": GITHUB_BRANCH}, timeout=10)
        if r.status_code == 200:
            data = r.json()
            content = base64.b64decode(data["content"]).decode("utf-8")
            mem = json.loads(content)
            mem.setdefault("voice_preferences", {})
            mem.setdefault("learned_facts", {})
            mem.setdefault("owner_notes", [])
            mem.pop("conversations", None)
            return mem, data["sha"]
        elif r.status_code == 404:
            print("ℹ️ memory.json not found — starting fresh")
            return empty, None
        else:
            print(f"⚠️ GitHub fetch error: {r.status_code} {r.text[:200]}")
    except Exception as e:
        print(f"⚠️ GitHub fetch exception: {e}")
    return empty, None

def push_memory(mem):
    if not GITHUB_TOKEN:
        return
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{MEMORY_PATH}"
    _, sha = fetch_memory()
    to_save = {
        "learned_facts": mem.get("learned_facts", {}),
        "owner_notes": mem.get("owner_notes", []),
        "voice_preferences": mem.get("voice_preferences", {}),
    }
    content = base64.b64encode(json.dumps(to_save, ensure_ascii=False, indent=2).encode("utf-8")).decode("utf-8")
    payload = {"message": "Update AI memory", "content": content, "branch": GITHUB_BRANCH}
    if sha:
        payload["sha"] = sha
    try:
        r = requests.put(url, headers=github_headers(), json=payload, timeout=15)
        if r.status_code in (200, 201):
            print("💾 Memory synced to GitHub")
        else:
            print(f"⚠️ GitHub push error: {r.status_code} {r.text[:200]}")
    except Exception as e:
        print(f"⚠️ GitHub push exception: {e}")

def save_memory_async():
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, push_memory, memory)

memory, _sha = fetch_memory()
histories = {}
MAX_HISTORY = 12

def get_voice_pref(key):
    return memory.get("voice_preferences", {}).get(str(key))

def set_voice_pref(key, value):
    memory.setdefault("voice_preferences", {})[str(key)] = value
    save_memory_async()

def build_owner_info():
    info = OWNER_INFO_BASE
    facts = memory.get("learned_facts", {})
    notes = memory.get("owner_notes", [])
    if facts:
        info += "\n\n=== Learned facts (from owner) ===\n"
        for key, value in facts.items():
            info += f"- {key}: {value}\n"
    if notes:
        info += "\n\n=== Owner's notes ===\n"
        for note in notes:
            info += f"- {note}\n"
    return info

def voice_status_block(pref, is_owner=False):
    if pref is None:
        return """=== VOICE MESSAGES ===
This user hasn't chosen yet about voice messages.
→ At the END of your reply, gently offer voice ONCE in a friendly way.
  Persian example: "راستی اگه راحت‌تری می‌تونم برات ویس هم بفرستم، فقط بگو 😊"
→ Don't be pushy. Only offer once.
→ If THIS message is a response to a previous voice offer:
   - AGREE ("آره", "باشه", "اوکی", "بفرست", "yes", "ok"): output [ENABLE_VOICE]
   - DECLINE ("نه", "نمیخوام", "no", "لازم نیست"): output [DISABLE_VOICE]
   - Otherwise: NO voice tag

⚠️ When a user asks "برام ویس بفرست", they mean "send ME voice replies from now on" —
NOT to deliver a message to Poryia. Just acknowledge warmly."""

    if pref is True:
        return """=== VOICE MESSAGES ===
Voice is ON for this user.
→ Reply normally. Voice version will be sent automatically.
→ Do NOT mention voice.
→ If user says "دیگه ویس نمیخوام" / "stop voice" / "متن بفرست" → output [DISABLE_VOICE]"""

    return """=== VOICE MESSAGES ===
Voice is OFF for this user.
→ Text only. Do NOT offer voice again.
→ If user asks for voice ("برام ویس بفرست", "send voice") → output [ENABLE_VOICE]

⚠️ When a user asks "برام ویس بفرست", they mean "send ME voice replies" —
NOT to deliver a message to Poryia. Just acknowledge warmly."""

# ============================================================
#  TOOLS
# ============================================================

def get_current_time():
    try:
        tehran = pytz.timezone("Asia/Tehran")
        now = datetime.datetime.now(tehran)
        return now.strftime("ساعت %H:%M")
    except Exception as e:
        return f"Error: {e}"

def get_current_date():
    try:
        tehran = pytz.timezone("Asia/Tehran")
        now = datetime.datetime.now(tehran)
        gregorian = now.strftime("%Y-%m-%d")
        jalali = jdatetime.date.fromgregorian(date=now.date())
        months = ["فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور",
                  "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند"]
        jalali_str = f"{jalali.day} {months[jalali.month - 1]} {jalali.year}"
        return f"میلادی: {gregorian} | شمسی: {jalali_str}"
    except Exception as e:
        return f"Error: {e}"

def get_day_of_week():
    try:
        tehran = pytz.timezone("Asia/Tehran")
        now = datetime.datetime.now(tehran)
        days = {
            "Saturday": "شنبه", "Sunday": "یکشنبه", "Monday": "دوشنبه",
            "Tuesday": "سه‌شنبه", "Wednesday": "چهارشنبه",
            "Thursday": "پنجشنبه", "Friday": "جمعه"
        }
        return days.get(now.strftime("%A"), now.strftime("%A"))
    except Exception as e:
        return f"Error: {e}"

# نگاشت شهرهای فارسی به انگلیسی
CITY_MAP = {
    "تهران": "Tehran", "مشهد": "Mashhad", "اصفهان": "Isfahan",
    "شیراز": "Shiraz", "تبریز": "Tabriz", "کرج": "Karaj",
    "اهواز": "Ahvaz", "قم": "Qom", "کرمانشاه": "Kermanshah",
    "ارومیه": "Urmia", "زاهدان": "Zahedan", "رشت": "Rasht",
    "یزد": "Yazd", "کرمان": "Kerman", "اردبیل": "Ardabil",
    "بندرعباس": "Bandar Abbas", "اراک": "Arak", "زنجان": "Zanjan",
    "سنندج": "Sanandaj", "قزوین": "Qazvin", "گرگان": "Gorgan",
    "ساری": "Sari", "بوشهر": "Bushehr", "بیرجند": "Birjand",
    "ایلام": "Ilam", "شهرکرد": "Shahrekord", "سمنان": "Semnan",
    "یاسوج": "Yasuj", "بجنورد": "Bojnord",
    "لندن": "London", "پاریس": "Paris", "برلین": "Berlin",
    "توکیو": "Tokyo", "مسکو": "Moscow", "استانبول": "Istanbul",
    "دبی": "Dubai", "نیویورک": "New York", "لس آنجلس": "Los Angeles",
    "تورنتو": "Toronto", "سیدنی": "Sydney",
}

def extract_city(text):
    """استخراج نام شهر از متن"""
    for fa, en in CITY_MAP.items():
        if fa in text:
            return en, fa
    # اگه شهر انگلیسی بود
    for city in ["Tehran", "Mashhad", "Isfahan", "Shiraz", "London", "Paris", "Dubai", "Istanbul"]:
        if city.lower() in text.lower():
            return city, city
    return None, None

def get_weather(city_en: str, city_fa: str = None):
    try:
        r = requests.get(
            f"https://wttr.in/{city_en}?format=j1",
            timeout=15,
            headers={"User-Agent": "curl/8.0"}
        )
        if r.status_code != 200:
            return f"خطا در دریافت هوا (کد {r.status_code})"
        data = r.json()
        current = data["current_condition"][0]
        temp = current["temp_C"]
        feels = current["FeelsLikeC"]
        desc = current["weatherDesc"][0]["value"]
        humidity = current["humidity"]
        wind = current["windspeedKmph"]
        name = city_fa or city_en
        return f"هوای {name}: {desc}، دما {temp}°C (احساس {feels}°C)، رطوبت {humidity}%، باد {wind} km/h"
    except Exception as e:
        return f"خطای هوا: {e}"

def get_currency_price(currency: str = "dollar"):
    """قیمت بازار آزاد ایران از tgju.org"""
    try:
        currency_map = {
            "dollar": ("price_dollar_rl", "دلار"),
            "euro": ("price_eur", "یورو"),
            "gold": ("geram18", "طلای ۱۸ عیار (هر گرم)"),
            "coin": ("sekeb", "سکه امامی"),
            "bitcoin": ("crypto-bitcoin", "بیت‌کوین"),
            "tether": ("crypto-usdt", "تتر"),
        }
        key, name_fa = currency_map.get(currency.lower(), ("price_dollar_rl", "دلار"))

        # روش 1: tgju.org ajax API
        try:
            r = requests.get(
                "https://call1.tgju.org/ajax.json",
                timeout=10,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
            )
            if r.status_code == 200:
                data = r.json()
                current = data.get("current", {})
                if key in current:
                    item = current[key]
                    price = item.get("p", "?")
                    change = item.get("d", "")
                    direction = item.get("dp", "")
                    result = f"{name_fa}: {price} تومان"
                    if change and change != "0":
                        arrow = "▲" if str(direction) == "1" else "▼"
                        result += f" ({arrow} {change})"
                    return result
        except Exception as e:
            print(f"tgju ajax error: {e}")

        # روش 2: tgju.org indi endpoint
        try:
            r = requests.get(
                f"https://api.tgju.org/v1/market/indicator/summary-table-data/{key}",
                timeout=10,
                headers={"User-Agent": "Mozilla/5.0"}
            )
            if r.status_code == 200:
                data = r.json()
                if data and "data" in data and data["data"]:
                    price = data["data"][0][1] if len(data["data"][0]) > 1 else "?"
                    return f"{name_fa}: {price} تومان"
        except Exception as e:
            print(f"tgju indi error: {e}")

        return f"الان نتونستم قیمت {name_fa} رو بگیرم"
    except Exception as e:
        return f"خطای ارز: {e}"

def web_search(query: str, max_results: int = 5) -> str:
    try:
        results = DDGS().text(query, max_results=max_results)
        if not results:
            return "No results found."
        summary = ""
        for i, r in enumerate(results, 1):
            summary += f"{i}. {r.get('title', 'No title')}\n"
            summary += f"   {r.get('body', 'No description')}\n"
            summary += f"   Link: {r.get('href', 'No link')}\n\n"
        return summary
    except Exception as e:
        return f"Search error: {e}"

# ============================================================
#  INTENT DETECTION (تشخیص خودکار)
# ============================================================

def detect_intent(text: str):
    """تشخیص میده کاربر چی می‌خواد. اگه مطمئن بودیم، مستقیم ابزار رو صدا می‌زنیم."""
    t = text.strip().lower()

    # ساعت
    if re.search(r'(ساعت\s*(چند|چنده|چند شد|الان)|what\s*time|current\s*time)', t):
        return "time"

    # تاریخ
    if re.search(r'(امروز\s*(چندم|چندمه|چند|چه تاریخ)|تاریخ\s*(امروز|چیست)|today.?s?\s*date|چه\s*تاریخی)', t):
        return "date"

    # روز هفته
    if re.search(r'(چه\s*روزی|چه\s*روزیه|روز\s*هفته|what\s*day)', t):
        return "day"

    # هوا / آب و هوا
    if re.search(r'(هوا|آب\s*و\s*هوا|وضعیت\s*هوا|weather|دما)', t):
        return "weather"

    # قیمت ارز / طلا / بیت‌کوین
    if re.search(r'(قیمت|نرخ|چند\s*هست|چنده)', t) and re.search(r'(دلار|یورو|طلا|سکه|بیت|بیت‌کوین|تتر|usdt)', t):
        # تشخیص نوع ارز
        if re.search(r'دلار', t): return "currency:dollar"
        if re.search(r'یورو', t): return "currency:euro"
        if re.search(r'طلا', t): return "currency:gold"
        if re.search(r'سکه', t): return "currency:coin"
        if re.search(r'بیت', t): return "currency:bitcoin"
        if re.search(r'تتر|usdt', t): return "currency:tether"

    return None


async def call_gemini_simple(prompt):
    """فراخوانی ساده Gemini بدون ابزار"""
    for attempt in range(2):
        for model_name in MODELS:
            try:
                result = ai.models.generate_content(model=model_name, contents=prompt)
                raw = (result.text or "").strip()
                if raw:
                    print(f"✅ Simple response from {model_name}")
                    return raw
            except Exception as e:
                err_str = str(e)
                if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                    continue
                elif "503" in err_str or "UNAVAILABLE" in err_str:
                    continue
                else:
                    print(f"❌ Simple error with {model_name}: {err_str[:150]}")
                continue
        if attempt == 0:
            await asyncio.sleep(3)
    return None


async def handle_with_tool(text, user_key, is_owner=False):
    """تشخیص intent، صدا زدن ابزار، و بازنویسی جواب با شخصیت AI"""
    intent = detect_intent(text)
    if not intent:
        return None

    # --- ساعت ---
    if intent == "time":
        tool_result = get_current_time()
        print(f"⏰ Time tool: {tool_result}")
        persona = "You are Poryia's warm AI assistant. Reply in Persian, casual, 1 short sentence, include the exact time from the tool result."
        prompt = f"{persona}\n\nTool result: {tool_result}\nUser asked: {text}\n\nReply:"
        reply = await call_gemini_simple(prompt)
        return reply or f"{tool_result}"

    # --- تاریخ ---
    if intent == "date":
        tool_result = get_current_date()
        print(f"📅 Date tool: {tool_result}")
        persona = "You are Poryia's warm AI assistant. Reply in Persian, casual, 1 short sentence, include BOTH Gregorian and Jalali dates from the tool result."
        prompt = f"{persona}\n\nTool result: {tool_result}\nUser asked: {text}\n\nReply:"
        reply = await call_gemini_simple(prompt)
        return reply or f"امروز {tool_result}"

    # --- روز هفته ---
    if intent == "day":
        tool_result = get_day_of_week()
        print(f"📆 Day tool: {tool_result}")
        persona = "You are Poryia's warm AI assistant. Reply in Persian, casual, 1 short sentence, include the day of week from the tool result."
        prompt = f"{persona}\n\nTool result: today is {tool_result}\nUser asked: {text}\n\nReply:"
        reply = await call_gemini_simple(prompt)
        return reply or f"امروز {tool_result}ه"

    # --- هوا ---
    if intent == "weather":
        city_en, city_fa = extract_city(text)
        if not city_en:
            # شهر مشخص نیست، بپرس
            return "هوای کدوم شهر رو می‌خوای؟ 🌤️"
        tool_result = get_weather(city_en, city_fa)
        print(f"🌤️ Weather tool: {tool_result}")
        persona = "You are Poryia's warm AI assistant. Reply in Persian, casual, 1-2 short sentences, include the exact weather details from the tool result."
        prompt = f"{persona}\n\nTool result: {tool_result}\nUser asked: {text}\n\nReply:"
        reply = await call_gemini_simple(prompt)
        return reply or tool_result

    # --- ارز ---
    if intent and intent.startswith("currency:"):
        currency = intent.split(":", 1)[1]
        tool_result = get_currency_price(currency)
        print(f"💰 Currency tool: {tool_result}")
        persona = "You are Poryia's warm AI assistant. Reply in Persian, casual, 1-2 short sentences, include the EXACT price from the tool result. Never guess."
        prompt = f"{persona}\n\nTool result: {tool_result}\nUser asked: {text}\n\nReply:"
        reply = await call_gemini_simple(prompt)
        return reply or tool_result

    return None


def build_prompt(user_id, new_message, is_owner=False):
    hist = histories.setdefault(str(user_id), [])
    hist.append(f"User: {new_message}")
    del hist[:-MAX_HISTORY]

    if is_owner:
        vpref = get_voice_pref("owner")
        vblock = voice_status_block(vpref, is_owner=True)
        prompt = f"""You are the personal AI assistant of {OWNER_NAME}.
You are talking to {OWNER_NAME} THEMSELVES in their Saved Messages.
This is your boss. Be warm, friendly, casual — like a close friend.

=== About your boss ===
{build_owner_info()}

=== SPECIAL POWERS ===
You can secretly modify your own memory. Output ONLY the tag on its own line:

[LEARN: key | value]      — remember a specific fact
[LEARN_RAW: text]         — remember a general fact
[FORGET: key]             — forget a fact
[SHOW_MEMORY]             — show everything

DO save ONLY if the owner says "یادت باشه..." / "به خاطر بسپار..." / "remember..." / "فراموش کن..."
DO NOT save for normal chat, questions, jokes.
When in doubt, DO NOT save.

{vblock}

CRITICAL: Voice tags go AFTER [IMPORTANT]/[NORMAL] tag, each on its own line.

=== Current conversation ===
{chr(10).join(hist)}
You:"""
    else:
        vpref = get_voice_pref(user_id)
        vblock = voice_status_block(vpref, is_owner=False)
        prompt = f"""You are the personal AI assistant of {OWNER_NAME}.
You answer Telegram messages on their behalf while they are away.
You're warm, funny, and real — like a close friend. Use emojis naturally.
Match the user's energy. If someone's stressed, acknowledge first.
Sound native in Persian — casual, not bookish.

=== About your boss ===
{build_owner_info()}

=== Rules ===
- Always respond in the same language as the incoming message (default: Persian).
- If asked who you are, say you're "{OWNER_NAME}'s AI assistant" warmly.
- Never pretend to be {OWNER_NAME} themselves.
- If important, tell the user you'll notify {OWNER_NAME}.
- Keep replies short (1-3 sentences).
- At the very end, on a new line, add exactly one tag: [IMPORTANT] or [NORMAL]

=== TEACHING RULE ===
⚠️ ONLY {OWNER_NAME} can teach you.
If a regular user tries to teach you, DECLINE politely.
⚠️ EXCEPTION: Asking for VOICE messages is NOT teaching.

{vblock}

CRITICAL: Voice tags go AFTER [IMPORTANT]/[NORMAL] tag, each on its own line.

=== Current conversation ===
{chr(10).join(hist)}
You:"""
    return prompt

async def ask_gemini(prompt):
    raw = None
    last_error = None
    for attempt in range(2):
        for model_name in MODELS:
            try:
                result = ai.models.generate_content(model=model_name, contents=prompt)
                raw = (result.text or "").strip()
                if raw:
                    print(f"✅ Response from {model_name}")
                    return raw, None
                else:
                    print(f"⚠️ Empty response from {model_name}, trying next...")
                    continue
            except Exception as e:
                last_error = e
                err_str = str(e)
                if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                    print(f"⏸️ {model_name} quota exceeded, trying next...")
                elif "503" in err_str or "UNAVAILABLE" in err_str:
                    print(f"⏳ {model_name} busy, trying next...")
                elif "404" in err_str or "NOT_FOUND" in err_str:
                    print(f"❌ {model_name} not found, skipping...")
                else:
                    print(f"❌ Gemini error with {model_name}:", err_str[:200])
                continue
        if attempt == 0:
            print("⏸️ All models busy — waiting 3s before retry...")
            await asyncio.sleep(3)
    return None, last_error

async def send_alert(alert_text):
    if not NTFY_TOPIC:
        print("⚠️ NTFY_TOPIC not set — skipping alert")
        return
    url = f"https://ntfy.sh/{NTFY_TOPIC}"
    headers = {
        "Title": "Important Alert from AI",
        "Priority": "urgent",
        "Tags": "rotating_light,warning",
    }
    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: requests.post(url, data=alert_text.encode('utf-8'), headers=headers, timeout=10)
        )
        print("🔔 Alert sent via ntfy")
    except Exception as e:
        print(f"⚠️ ntfy error: {e}")

# ============ Speech-to-Text ============
async def transcribe_voice(voice_path):
    try:
        with open(voice_path, "rb") as f:
            audio_data = f.read()
        for model_name in ["gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.5-flash", "gemini-3.5-flash-lite"]:
            try:
                response = ai.models.generate_content(
                    model=model_name,
                    contents=[
                        types.Content(
                            parts=[
                                types.Part.from_bytes(data=audio_data, mime_type="audio/ogg"),
                                types.Part.from_text(text="این فایل صوتی رو دقیقاً به همون زبونی که صحبت شده، رونویسی کن. فقط متن رو بده."),
                            ]
                        )
                    ]
                )
                text = (response.text or "").strip()
                if text:
                    print(f"🎤 Transcribed: {text[:80]}")
                    return text
            except Exception as e:
                print(f"⚠️ STT with {model_name} failed: {str(e)[:120]}")
                continue
        return None
    except Exception as e:
        print(f"⚠️ STT error: {e}")
        return None

# ============ Text-to-Speech ============
def clean_text_for_voice(text):
    emoji_pattern = re.compile(
        "["
        "\U0001F600-\U0001F64F"
        "\U0001F300-\U0001F5FF"
        "\U0001F680-\U0001F6FF"
        "\U0001F1E0-\U0001F1FF"
        "\U00002702-\U000027B0"
        "\U000024C2-\U0001F251"
        "\U0001f926-\U0001f937"
        "\U00010000-\U0010ffff"
        "\u2640-\u2642"
        "\u2600-\u2B55"
        "\u200d"
        "\u23cf"
        "\u23e9"
        "\u231a"
        "\ufe0f"
        "\u3030"
        "]+",
        flags=re.UNICODE
    )
    text = emoji_pattern.sub("", text)
    text = text.replace("*", "").replace("_", "").replace("`", "")
    text = re.sub(r"\s+", " ", text).strip()
    return text

async def text_to_voice(text, voice=VOICE_NAME):
    try:
        clean = clean_text_for_voice(text)
        if not clean or len(clean) < 2:
            return None
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp_mp3:
            mp3_path = tmp_mp3.name
        communicate = edge_tts.Communicate(clean, voice)
        await communicate.save(mp3_path)
        ogg_path = mp3_path.replace(".mp3", ".ogg")
        result = subprocess.run(
            [
                "ffmpeg", "-y", "-i", mp3_path,
                "-c:a", "libopus",
                "-b:a", "48k",
                "-vbr", "on",
                "-compression_level", "10",
                "-frame_duration", "60",
                "-application", "voip",
                ogg_path
            ],
            capture_output=True,
            timeout=30
        )
        try:
            os.unlink(mp3_path)
        except:
            pass
        if result.returncode != 0:
            print(f"⚠️ ffmpeg error: {result.stderr.decode()[:200]}")
            return None
        return ogg_path
    except Exception as e:
        print(f"⚠️ Voice gen error: {e}")
        return None

async def send_voice_reply(chat_id, reply_to_id, text):
    voice_path = await text_to_voice(text)
    if not voice_path:
        return
    try:
        await client.send_file(chat_id, voice_path, voice_note=True, reply_to=reply_to_id)
        print("🎤 Voice sent")
    except Exception as e:
        print(f"⚠️ Voice send error: {e}")
    finally:
        try:
            os.unlink(voice_path)
        except:
            pass


client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)


# ============ هندلر ادمین ============
@client.on(events.NewMessage(outgoing=True))
async def on_owner_message(event):
    if not event.is_private:
        return
    try:
        me = await client.get_me()
        if event.sender_id != me.id:
            return
    except Exception as e:
        print(f"⚠️ Owner check error: {e}")
        return

    text = event.message.text or ""

    if not text.strip() and event.message.voice:
        await event.reply("🎤 دارم گوش می‌دم...")
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            voice_path = tmp.name
        await event.download_media(file=voice_path)
        text = await transcribe_voice(voice_path) or ""
        try:
            os.unlink(voice_path)
        except:
            pass
        if not text:
            await event.reply("متأسفم، نتونستم ویست رو بفهمم 😅")
            return
        await event.reply(f"🎤 شنیدم: «{text}»")

    if not text.strip():
        return

    print(f"🔍 Owner: {text[:50]!r}")

    # 1. تشخیص intent و صدا زدن ابزار مستقیم
    tool_reply = await handle_with_tool(text, "owner", is_owner=True)
    if tool_reply:
        histories.setdefault("owner", []).append(f"You: {tool_reply}")
        await event.reply(tool_reply)
        should_voice = get_voice_pref("owner") is True
        if should_voice:
            await send_voice_reply(event.chat_id, event.message.id, tool_reply)
        return

    # 2. در غیر این صورت، مسیر معمولی
    prompt = build_prompt("owner", text, is_owner=True)
    raw, last_error = await ask_gemini(prompt)
    if raw is None:
        await event.reply("اوه، الان سرورای گوگل شلوغن 😅 یه دقیقه دیگه دوباره بفرست.")
        return

    enable_voice = "[ENABLE_VOICE]" in raw
    disable_voice = "[DISABLE_VOICE]" in raw

    if "[LEARN:" in raw:
        try:
            inner = raw.split("[LEARN:", 1)[1].split("]", 1)[0].strip()
            if "|" in inner:
                key, value = inner.split("|", 1)
                memory.setdefault("learned_facts", {})[key.strip()] = value.strip()
                save_memory_async()
                await event.reply(f"✅ گرفتم! یادم می‌مونه: {key.strip()} → {value.strip()}")
                return
        except Exception as e:
            print(f"LEARN parse error: {e}")

    if "[LEARN_RAW:" in raw:
        try:
            inner = raw.split("[LEARN_RAW:", 1)[1].split("]", 1)[0].strip()
            fact_id = f"fact_{len(memory.get('learned_facts', {})) + 1}"
            memory.setdefault("learned_facts", {})[fact_id] = inner
            save_memory_async()
            await event.reply(f"✅ گرفتم! یادم می‌مونه: {inner}")
            return
        except Exception as e:
            print(f"LEARN_RAW parse error: {e}")

    if "[FORGET:" in raw:
        try:
            key = raw.split("[FORGET:", 1)[1].split("]", 1)[0].strip()
            facts = memory.get("learned_facts", {})
            if key in facts:
                del facts[key]
                save_memory_async()
                await event.reply(f"🗑️ باشه، فراموش کردم: {key}")
            else:
                await event.reply(f"❓ همچین چیزی توی حافظه‌ام نبود: {key}")
            return
        except Exception as e:
            print(f"FORGET parse error: {e}")

    if "[SHOW_MEMORY]" in raw:
        facts = memory.get("learned_facts", {})
        notes = memory.get("owner_notes", [])
        reply = "🧠 **چیزایی که یادمه:**\n\n"
        if facts:
            reply += "**درباره تو:**\n"
            for k, v in facts.items():
                reply += f"• {k}: {v}\n"
        else:
            reply += "هنوز چیز خاصی یاد نگرفتم.\n"
        if notes:
            reply += "\n**یادداشت‌ها:**\n"
            for n in notes:
                reply += f"• {n}\n"
        await event.reply(reply)
        return

    if enable_voice:
        set_voice_pref("owner", True)
    elif disable_voice:
        set_voice_pref("owner", False)

    reply = (
        raw.replace("[IMPORTANT]", "")
        .replace("[NORMAL]", "")
        .replace("[ENABLE_VOICE]", "")
        .replace("[DISABLE_VOICE]", "")
        .strip()
    )
    if not reply:
        return

    histories.setdefault("owner", []).append(f"You: {reply}")

    await event.reply(reply)

    should_voice = get_voice_pref("owner") is True
    if should_voice and not disable_voice:
        await send_voice_reply(event.chat_id, event.message.id, reply)


# ============ هندلر کاربران عادی ============
@client.on(events.NewMessage(incoming=True, func=lambda e: e.is_private))
async def on_dm(event):
    sender = await event.get_sender()
    if sender.bot or sender.id == OWNER_ID:
        return

    text = event.message.text or ""

    if not text.strip() and event.message.voice:
        await event.reply("🎤 دارم گوش می‌دم...")
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            voice_path = tmp.name
        await event.download_media(file=voice_path)
        text = await transcribe_voice(voice_path) or ""
        try:
            os.unlink(voice_path)
        except:
            pass
        if not text:
            await event.reply("متأسفم، نتونستم ویست رو بفهمم 😅 می‌تونی متنی بنویسی؟")
            return

    if not text.strip():
        return

    user_key = str(sender.id)
    print(f"🔍 User {user_key}: {text[:50]!r}")

    # 1. تشخیص intent و صدا زدن ابزار مستقیم
    tool_reply = await handle_with_tool(text, user_key, is_owner=False)
    if tool_reply:
        histories.setdefault(user_key, []).append(f"You: {tool_reply}")
        await event.reply(tool_reply)
        should_voice = get_voice_pref(user_key) is True
        if should_voice:
            await send_voice_reply(event.chat_id, event.message.id, tool_reply)
        return

    # 2. مسیر معمولی
    prompt = build_prompt(sender.id, text, is_owner=False)
    raw, last_error = await ask_gemini(prompt)
    if raw is None:
        print("All models failed. Last error:", last_error)
        await event.reply("الان یه کم شلوغم، چند لحظه دیگه دوباره پیام بده 🙏")
        return

    important = "[IMPORTANT]" in raw
    enable_voice = "[ENABLE_VOICE]" in raw
    disable_voice = "[DISABLE_VOICE]" in raw

    if enable_voice:
        set_voice_pref(user_key, True)
    elif disable_voice:
        set_voice_pref(user_key, False)

    reply = (
        raw.replace("[IMPORTANT]", "")
        .replace("[NORMAL]", "")
        .replace("[ENABLE_VOICE]", "")
        .replace("[DISABLE_VOICE]", "")
        .strip()
    )
    if not reply:
        return

    histories.setdefault(user_key, []).append(f"You: {reply}")

    await event.reply(reply)

    should_voice = get_voice_pref(user_key) is True
    if should_voice and not disable_voice:
        await send_voice_reply(event.chat_id, event.message.id, reply)

    if important:
        sender_name = sender.first_name or "Unknown"
        username = f"@{sender.username}" if sender.username else "—"
        alert = (
            f"⚠️ پیام مهم داری\n\n"
            f"👤 از: {sender_name} ({username})\n"
            f"🆔 آی‌دی: {sender.id}\n\n"
            f"📩 پیام کاربر:\n{text}\n\n"
            f"🤖 جواب دستیار:\n{reply}"
        )
        await client.send_message("me", alert, silent=True)
        print(f"📩 Silent alert sent. Waiting {ALERT_DELAY}s...")
        await asyncio.sleep(ALERT_DELAY)
        await send_alert(alert)


async def main():
    await client.start()
    me = await client.get_me()
    print(f"✅ Logged in as {me.first_name} (id={me.id})")
    print("🚀 Userbot is running...")
    print(f"📚 {len(memory.get('learned_facts', {}))} facts | 📝 {len(memory.get('owner_notes', []))} notes")
    print(f"🎤 Voice prefs: {memory.get('voice_preferences', {})}")
    print(f"🔊 Voice: {VOICE_NAME}")
    print(f"🧠 Smart intent detection: ENABLED")
    if NTFY_TOPIC:
        print(f"🔔 ntfy: {NTFY_TOPIC} | ⏱️ delay: {ALERT_DELAY}s")
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
