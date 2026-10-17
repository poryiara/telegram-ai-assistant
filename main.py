import os
import json
import base64
import asyncio
import requests
import subprocess
import tempfile
import re
import time
from collections import deque
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from google import genai
from google.genai import types
from aiohttp import web
import edge_tts

# ============ تنظیمات محیطی ============
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
SESSION_STRING = os.environ["SESSION_STRING"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
OWNER_ID = int(os.environ["OWNER_ID"])
OWNER_NAME = os.environ.get("OWNER_NAME", "Poryia")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
API_KEY = os.environ.get("API_KEY", "changeme")

VOICE_NAME = os.environ.get("VOICE_NAME", "fa-IR-FaridNeural")
ALERT_DELAY = int(os.environ.get("ALERT_DELAY", "15"))
PAUSE_DURATION = int(os.environ.get("PAUSE_DURATION", "1800"))
PORT = int(os.environ.get("PORT", "8080"))

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "poryiara/telegram-ai-assistant")
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "main")
MEMORY_PATH = os.environ.get("MEMORY_PATH", "memory.json")

# ============ تنظیمات قابل تغییر از اپ ============
runtime_settings = {
    "pause_enabled": True,
    "pause_duration": PAUSE_DURATION,
    "voice_output": True,
    "voice_input": True,
    "voice_name": VOICE_NAME,
    "alert_delay": ALERT_DELAY,
    "max_retry_time": 300,
}

# ============ State ============
owner_last_seen = {}          # chat_id → timestamp آخرین پیام مالک
chat_names_cache = {}         # chat_id → name
custom_pause_durations = {}   # chat_id → seconds (override)
global_mute_until = 0         # timestamp تا کی همه چیز ساکت
last_bot_send_time = 0        # برای جلوگیری از loop

# ============ لاگ ============
recent_logs = deque(maxlen=100)
started_at = time.time()

def log(message):
    ts = time.strftime("%H:%M:%S")
    entry = {"time": ts, "text": message}
    recent_logs.append(entry)
    print(f"{ts} {message}")

# ============ Gemini ============
ai = genai.Client(api_key=GEMINI_API_KEY)

MODELS = [
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
]

STT_MODELS = [
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
]

RETRY_DELAYS = [0.5, 1, 2, 3, 5, 8, 13, 20, 30, 30, 30]

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

# ============ Pause helpers ============
async def cache_chat_name(chat_id):
    try:
        entity = await client.get_entity(chat_id)
        name = getattr(entity, "first_name", None) or getattr(entity, "title", None) or str(chat_id)
        chat_names_cache[chat_id] = name
    except Exception as e:
        chat_names_cache[chat_id] = str(chat_id)
        log(f"⚠️ Could not fetch name for {chat_id}: {str(e)[:60]}")

def get_pause_duration(chat_id):
    return custom_pause_durations.get(chat_id, runtime_settings["pause_duration"])

def mark_owner_active(chat_id):
    owner_last_seen[chat_id] = time.time()
    if chat_id not in chat_names_cache:
        try:
            asyncio.create_task(cache_chat_name(chat_id))
        except:
            pass

def is_owner_paused(chat_id):
    if not runtime_settings["pause_enabled"]:
        return False
    last = owner_last_seen.get(chat_id, 0)
    if last == 0:
        return False
    return (time.time() - last) < get_pause_duration(chat_id)

def pause_remaining(chat_id):
    last = owner_last_seen.get(chat_id, 0)
    if last == 0:
        return 0
    return max(0, int(get_pause_duration(chat_id) - (time.time() - last)))

def is_globally_muted():
    return time.time() < global_mute_until

async def resolve_chat_id(target_str):
    """تبدیل اسم یا ID به chat_id"""
    target_str = target_str.strip()
    try:
        return int(target_str)
    except:
        pass
    
    target_lower = target_str.lower()
    # اول از کش
    for cid, name in chat_names_cache.items():
        if name and (target_lower in name.lower() or name.lower() in target_lower):
            return cid
    
    # سرچ توی چت‌های اخیر
    try:
        count = 0
        async for dialog in client.iter_dialogs(limit=40):
            if not dialog.is_user:
                continue
            entity = dialog.entity
            entity_name = getattr(entity, "first_name", "") or getattr(entity, "title", "") or ""
            if entity_name and (target_lower in entity_name.lower() or entity_name.lower() in target_lower):
                chat_names_cache[dialog.id] = entity_name
                return dialog.id
            count += 1
    except Exception as e:
        log(f"⚠️ Search error: {str(e)[:80]}")
    return None

def build_paused_section():
    if not owner_last_seen:
        return "=== PAUSED CHATS ===\nهیچ چتی pause نیست."
    
    lines = ["=== PAUSED CHATS (current) ==="]
    found = False
    for chat_id in list(owner_last_seen.keys()):
        remaining = pause_remaining(chat_id)
        if remaining > 0:
            found = True
            name = chat_names_cache.get(chat_id, "نامشخص")
            duration_min = get_pause_duration(chat_id) // 60
            custom = " (custom)" if chat_id in custom_pause_durations else ""
            lines.append(f"- نام: {name} | chat_id: {chat_id} | مدت کل: {duration_min}دقیقه{custom} | باقی‌مونده: {remaining//60}د {remaining%60}ث")
    
    if not found:
        return "=== PAUSED CHATS ===\nهیچ چتی pause نیست."
    
    return "\n".join(lines)

def build_global_mute_section():
    if is_globally_muted():
        remaining = int(global_mute_until - time.time())
        return f"=== GLOBAL MUTE ===\n⚠️ ربات الان در حالت سکوت سراسری است. {remaining//60} دقیقه و {remaining%60} ثانیه باقی مانده."
    return ""

# ============ Forward / Context helpers ============
async def forward_last_messages(chat_id, count=1):
    """فوروارد آخرین N پیام به Saved Messages"""
    try:
        messages = await client.get_messages(chat_id, limit=count)
        if not messages:
            return 0, "هیچ پیامی پیدا نشد"
        await client.forward_messages("me", messages)
        return len(messages), None
    except Exception as e:
        return 0, str(e)

async def show_context(chat_id, count=5):
    """نمایش متن آخرین N پیام"""
    try:
        messages = await client.get_messages(chat_id, limit=count)
        if not messages:
            return "هیچ پیامی نیست"
        lines = []
        for msg in reversed(messages):
            sender = "من" if msg.out else "کاربر"
            text = msg.text or "[رسانه/ویس/عکس]"
            if len(text) > 150:
                text = text[:150] + "..."
            lines.append(f"{sender}: {text}")
        return "\n".join(lines)
    except Exception as e:
        return f"خطا: {str(e)[:100]}"

# ============ GitHub Memory ============
def github_headers():
    return {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }

def fetch_memory():
    empty = {"learned_facts": {}, "owner_notes": [], "voice_preferences": {}}
    if not GITHUB_TOKEN:
        log("⚠️ No GITHUB_TOKEN")
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
            return empty, None
        else:
            log(f"⚠️ GitHub fetch: {r.status_code}")
    except Exception as e:
        log(f"⚠️ GitHub fetch exception: {str(e)[:100]}")
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
            log("💾 Memory saved")
        else:
            log(f"⚠️ GitHub push: {r.status_code}")
    except Exception as e:
        log(f"⚠️ GitHub push exception: {str(e)[:100]}")

def save_memory_async():
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, push_memory, memory)

memory, _sha = fetch_memory()
histories = {}
MAX_HISTORY = 12

def get_voice_pref(key):
    return memory.get("voice_preferences", {}).get(str(key))

def set_voice_pref(key, value):
    key = str(key)
    current = memory.get("voice_preferences", {}).get(key)
    if current == value:
        return
    memory.setdefault("voice_preferences", {})[key] = value
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

⚠️ When a user asks "برام ویس بفرست", they mean "send ME voice replies from now on" — NOT to forward something to Poryia."""

    if pref is True:
        return """=== VOICE MESSAGES ===
Voice is ON for this user. A voice version of your reply will be sent automatically.
→ Do NOT mention voice.
→ If user says "دیگه ویس نمیخوام" / "متن بفرست" → output [DISABLE_VOICE]"""

    return """=== VOICE MESSAGES ===
Voice is OFF. Text only. Do NOT offer voice again.
→ If user asks for voice ("برام ویس بفرست", "send voice") → output [ENABLE_VOICE]"""

def build_prompt(user_id, new_message, is_owner=False):
    hist = histories.setdefault(str(user_id), [])
    hist.append(f"User: {new_message}")
    del hist[:-MAX_HISTORY]

    if is_owner:
        vpref = get_voice_pref("owner")
        vblock = voice_status_block(vpref, is_owner=True)
        paused_section = build_paused_section()
        mute_section = build_global_mute_section()
        prompt = f"""You are the personal AI assistant of {OWNER_NAME}.
You are talking to {OWNER_NAME} THEMSELVES in their Saved Messages.
This is your boss. Be warm, friendly, casual — like a close friend.

=== About your boss ===
{build_owner_info()}

{paused_section}

{mute_section}

=== SPECIAL POWERS (OWNER ONLY) ===

**Memory:**
[LEARN: key | value]  — remember a fact
[LEARN_RAW: text]     — remember a general fact
[FORGET: key]         — forget a fact
[SHOW_MEMORY]         — show everything

**Pause management:**
[PAUSE: target | minutes]    — pause a chat for X minutes (default = global pause_duration)
[PAUSE_FOREVER: target]      — pause a chat indefinitely (until manual unpause)
[UNPAUSE: target]            — unpause a specific chat
[UNPAUSE_ALL]                — unpause everything

**Message retrieval:**
[FORWARD: target | count]    — forward last N messages from a chat to Saved Messages
[CONTEXT: target]            — show last 5 messages from a chat as text (without forwarding)

**Global mute (silence everything):**
[GLOBAL_MUTE: minutes]       — mute ALL bot responses for X minutes
[GLOBAL_UNMUTE]              — unmute immediately

⚠️ TARGET: can be a numeric chat_id OR a name (Persian or English). System resolves names.
⚠️ After outputting a tag, you can add a short confirmation text BEFORE or AFTER. The tag MUST be on its own line.

=== EXAMPLES ===

Owner: علی رو 2 ساعت pause کن
You: باشه، علی رو ۲ ساعت pause کردم ✋
[PAUSE: علی | 120]

Owner: این چت رو دائمی pause کن
You: حله، تا وقتی خودت unpause نکنی ساکت می‌مونه 🛑
[PAUSE_FOREVER: 123456789]

Owner: پاز علی رو لغو کن
You: باشه آزادش کردم ✅
[UNPAUSE: علی]

Owner: همه رو آزاد کن
You: همه چت‌ها آزاد شدن ✅
[UNPAUSE_ALL]

Owner: پیام‌های آخر علی رو برام فوروارد کن
You: باشه، ۵ پیام آخرش رو می‌فرستم 📨
[FORWARD: علی | 5]

Owner: علی آخرین بار چی گفت؟
You: بذار ببینم...
[CONTEXT: علی]

Owner: پیام آیدی 123456789 رو برام فوروارد کن
You: باشه ✅
[FORWARD: 123456789 | 1]

Owner: 30 دقیقه همه رو ساکت کن (به کسی جواب نده)
You: باشه ۳۰ دقیقه ساکت می‌شم 🤐
[GLOBAL_MUTE: 30]

Owner: دیگه ساکت نباش
You: باشه دوباره فعال شدم ✨
[GLOBAL_UNMUTE]

Owner: کی pause هست؟
You: [list naturally from PAUSED CHATS section, NO tags]

Owner: یادت باشه اسم بهترین دوستم علیه
You: [LEARN: best_friend | Ali]

=== MEMORY RULES ===
DO save ONLY if owner says "یادت باشه..." / "به خاطر بسپار..." / "remember..." / "فراموش کن..."

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

async def ask_gemini(prompt, chat_id=None):
    started = time.time()
    round_num = 0
    last_error = None
    max_time = runtime_settings.get("max_retry_time", 300)

    while True:
        round_num += 1
        for model_name in MODELS:
            try:
                result = ai.models.generate_content(model=model_name, contents=prompt)
                raw = (result.text or "").strip()
                if raw:
                    elapsed = int(time.time() - started)
                    log(f"✅ {model_name} (round {round_num}, {elapsed}s)")
                    return raw, None
            except Exception as e:
                last_error = e
                err_str = str(e)
                if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                    log(f"⏸️ {model_name}: quota")
                elif "503" in err_str or "UNAVAILABLE" in err_str:
                    log(f"⏳ {model_name}: busy")
                elif "404" in err_str or "NOT_FOUND" in err_str:
                    log(f"❌ {model_name}: not found")
                else:
                    log(f"❌ {model_name}: {err_str[:80]}")
                continue

        elapsed = time.time() - started
        if elapsed > max_time:
            log(f"⛔ Gave up after {int(elapsed)}s")
            return None, last_error

        delay_idx = min(round_num - 1, len(RETRY_DELAYS) - 1)
        delay = RETRY_DELAYS[delay_idx]
        log(f"⏸️ Round {round_num} failed — retry in {delay}s...")

        if chat_id:
            try:
                await client.action(chat_id, "typing")
            except:
                pass

        await asyncio.sleep(delay)

async def send_alert(alert_text):
    if not NTFY_TOPIC:
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
        log("🔔 Alert sent")
    except Exception as e:
        log(f"⚠️ ntfy error: {str(e)[:80]}")

async def transcribe_voice(voice_path):
    started = time.time()
    round_num = 0
    max_time = runtime_settings.get("max_retry_time", 300)
    while True:
        round_num += 1
        try:
            with open(voice_path, "rb") as f:
                audio_data = f.read()
            if len(audio_data) < 100:
                return None
        except Exception as e:
            log(f"⚠️ File read error: {str(e)[:80]}")
            return None

        for model_name in STT_MODELS:
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
                    log(f"🎤 STT: {model_name} (round {round_num})")
                    return text
            except Exception as e:
                err_str = str(e)
                if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                    log(f"⏸️ STT {model_name}: quota")
                elif "503" in err_str or "UNAVAILABLE" in err_str:
                    log(f"⏳ STT {model_name}: busy")
                else:
                    log(f"❌ STT {model_name}: {err_str[:80]}")
                continue

        elapsed = time.time() - started
        if elapsed > max_time:
            log(f"⛔ STT gave up after {int(elapsed)}s")
            return None

        delay = RETRY_DELAYS[min(round_num - 1, len(RETRY_DELAYS) - 1)]
        log(f"⏸️ STT round {round_num} failed — retry in {delay}s...")
        await asyncio.sleep(delay)

async def handle_voice_input(event):
    try:
        await event.reply("🎤 دارم گوش می‌دم...")
    except:
        pass

    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        voice_path = tmp.name

    try:
        await event.download_media(file=voice_path)
        text = await transcribe_voice(voice_path)
    finally:
        try:
            os.unlink(voice_path)
        except:
            pass

    return text

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

async def text_to_voice(text, voice=None):
    try:
        voice = voice or runtime_settings.get("voice_name", VOICE_NAME)
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
                "-b:a", "48k", "-vbr", "on",
                "-compression_level", "10",
                "-frame_duration", "60",
                "-application", "voip",
                ogg_path
            ],
            capture_output=True, timeout=30
        )
        try:
            os.unlink(mp3_path)
        except:
            pass
        if result.returncode != 0:
            log(f"⚠️ ffmpeg error")
            return None
        return ogg_path
    except Exception as e:
        log(f"⚠️ Voice gen error: {str(e)[:80]}")
        return None

async def send_voice_reply(chat_id, reply_to_id, text):
    if not runtime_settings.get("voice_output", True):
        return
    voice_path = await text_to_voice(text)
    if not voice_path:
        return
    try:
        await client.send_file(chat_id, voice_path, voice_note=True, reply_to=reply_to_id)
        log("🎤 Voice sent")
    except Exception as e:
        log(f"⚠️ Voice send error: {str(e)[:80]}")
    finally:
        try:
            os.unlink(voice_path)
        except:
            pass


client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)


# ============================================================
#                       REST API
# ============================================================

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "X-API-Key, Content-Type",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
}

def api_auth(request):
    key = request.headers.get("X-API-Key") or request.query.get("api_key")
    return key == API_KEY

def json_response(data, status=200):
    return web.json_response(data, status=status, headers=CORS_HEADERS)

async def handle_options(request):
    return web.Response(headers=CORS_HEADERS)

async def api_ping(request):
    return json_response({"pong": True, "time": time.time()})

async def api_status(request):
    if not api_auth(request):
        return json_response({"error": "Unauthorized"}, 401)
    try:
        me = await client.get_me()
        uptime = int(time.time() - started_at)
        return json_response({
            "online": True,
            "user_id": me.id,
            "username": me.username,
            "first_name": me.first_name,
            "uptime_seconds": uptime,
            "facts_count": len(memory.get("learned_facts", {})),
            "notes_count": len(memory.get("owner_notes", [])),
            "voice_prefs_count": len(memory.get("voice_preferences", {})),
            "paused_chats_count": sum(1 for cid in owner_last_seen if is_owner_paused(cid)),
            "globally_muted": is_globally_muted(),
            "settings": runtime_settings,
        })
    except Exception as e:
        return json_response({"error": str(e)}, 500)

async def api_get_settings(request):
    if not api_auth(request):
        return json_response({"error": "Unauthorized"}, 401)
    return json_response({"settings": runtime_settings})

async def api_update_settings(request):
    if not api_auth(request):
        return json_response({"error": "Unauthorized"}, 401)
    try:
        data = await request.json()
    except:
        return json_response({"error": "Invalid JSON"}, 400)

    allowed = {"pause_enabled", "pause_duration", "voice_output", "voice_input", "voice_name", "alert_delay", "max_retry_time"}
    updated = {}
    for key, value in data.items():
        if key in allowed:
            runtime_settings[key] = value
            updated[key] = value

    log(f"⚙️ Settings updated: {updated}")
    return json_response({"ok": True, "updated": updated, "settings": runtime_settings})

async def api_paused_list(request):
    if not api_auth(request):
        return json_response({"error": "Unauthorized"}, 401)
    paused = []
    for chat_id in list(owner_last_seen.keys()):
        remaining = pause_remaining(chat_id)
        if remaining > 0:
            name = chat_names_cache.get(chat_id)
            if not name:
                try:
                    entity = await client.get_entity(chat_id)
                    name = getattr(entity, "first_name", None) or getattr(entity, "title", None)
                    chat_names_cache[chat_id] = name
                except:
                    pass
            paused.append({
                "chat_id": chat_id,
                "name": name,
                "remaining_seconds": remaining,
                "custom_duration": custom_pause_durations.get(chat_id),
            })
    paused.sort(key=lambda x: x["remaining_seconds"])
    return json_response({"paused": paused})

async def api_pause(request):
    if not api_auth(request):
        return json_response({"error": "Unauthorized"}, 401)
    try:
        data = await request.json()
        chat_id = int(data.get("chat_id"))
        duration = data.get("duration")  # optional, seconds
    except:
        return json_response({"error": "chat_id required"}, 400)
    owner_last_seen[chat_id] = time.time()
    if duration:
        custom_pause_durations[chat_id] = int(duration)
    if chat_id not in chat_names_cache:
        try:
            asyncio.create_task(cache_chat_name(chat_id))
        except:
            pass
    log(f"🛑 Paused {chat_id} for {get_pause_duration(chat_id)//60}m")
    return json_response({"ok": True, "chat_id": chat_id, "remaining": get_pause_duration(chat_id)})

async def api_resume(request):
    if not api_auth(request):
        return json_response({"error": "Unauthorized"}, 401)
    try:
        data = await request.json()
        chat_id = int(data.get("chat_id"))
    except:
        return json_response({"error": "chat_id required"}, 400)
    owner_last_seen.pop(chat_id, None)
    custom_pause_durations.pop(chat_id, None)
    log(f"▶️ Resumed {chat_id}")
    return json_response({"ok": True})

async def api_resume_all(request):
    if not api_auth(request):
        return json_response({"error": "Unauthorized"}, 401)
    count = len(owner_last_seen)
    owner_last_seen.clear()
    custom_pause_durations.clear()
    log(f"▶️ Resumed all ({count} chats)")
    return json_response({"ok": True, "cleared": count})

async def api_logs(request):
    if not api_auth(request):
        return json_response({"error": "Unauthorized"}, 401)
    return json_response({"logs": list(recent_logs)})

async def api_facts(request):
    if not api_auth(request):
        return json_response({"error": "Unauthorized"}, 401)
    return json_response({
        "facts": memory.get("learned_facts", {}),
        "notes": memory.get("owner_notes", []),
    })

async def api_fact_add(request):
    if not api_auth(request):
        return json_response({"error": "Unauthorized"}, 401)
    try:
        data = await request.json()
        key = data.get("key", "").strip()
        value = data.get("value", "").strip()
        if not key or not value:
            return json_response({"error": "key and value required"}, 400)
        memory.setdefault("learned_facts", {})[key] = value
        save_memory_async()
        log(f"📚 Fact added: {key}")
        return json_response({"ok": True})
    except Exception as e:
        return json_response({"error": str(e)}, 500)

async def api_fact_delete(request):
    if not api_auth(request):
        return json_response({"error": "Unauthorized"}, 401)
    try:
        data = await request.json()
        key = data.get("key", "").strip()
        if key in memory.get("learned_facts", {}):
            del memory["learned_facts"][key]
            save_memory_async()
            log(f"🗑️ Fact deleted: {key}")
            return json_response({"ok": True})
        return json_response({"error": "not found"}, 404)
    except Exception as e:
        return json_response({"error": str(e)}, 500)

def create_app():
    app = web.Application()
    app.router.add_route("OPTIONS", "/{tail:.*}", handle_options)
    app.router.add_get("/api/ping", api_ping)
    app.router.add_get("/api/status", api_status)
    app.router.add_get("/api/settings", api_get_settings)
    app.router.add_post("/api/settings", api_update_settings)
    app.router.add_get("/api/paused", api_paused_list)
    app.router.add_post("/api/pause", api_pause)
    app.router.add_post("/api/resume", api_resume)
    app.router.add_post("/api/resume_all", api_resume_all)
    app.router.add_get("/api/logs", api_logs)
    app.router.add_get("/api/facts", api_facts)
    app.router.add_post("/api/facts/add", api_fact_add)
    app.router.add_post("/api/facts/delete", api_fact_delete)
    return app


# ============ ردیاب فعالیت ============
@client.on(events.NewMessage(outgoing=True))
async def on_owner_activity(event):
    if not event.is_private:
        return
    try:
        me = await client.get_me()
    except:
        return
    if event.chat_id == me.id:
        return
    # اگه forwarded پیام باشه، نادیده بگیر
    if getattr(event.message, 'forward', None):
        return
    mark_owner_active(event.chat_id)
    remaining = pause_remaining(event.chat_id)
    log(f"🛑 Paused chat {event.chat_id} for {remaining//60}m")


# ============ هندلر ادمین ============
@client.on(events.NewMessage(outgoing=True))
async def on_owner_message(event):
    global global_mute_until, last_bot_send_time

    try:
        me = await client.get_me()
    except Exception as e:
        log(f"⚠️ Owner check error: {str(e)[:80]}")
        return

    if event.chat_id != me.id:
        return
    if event.sender_id != me.id:
        return

    # جلوگیری از loop
    if time.time() - last_bot_send_time < 1.5:
        return

    # پیام‌های forward شده رو نادیده بگیر
    if getattr(event.message, 'forward', None):
        return

    text = event.message.text or ""

    if not text.strip() and event.message.voice:
        if not runtime_settings.get("voice_input", True):
            await event.reply("🚫 خوندن ویس غیرفعاله")
            return
        text = await handle_voice_input(event) or ""
        if not text:
            await event.reply("متأسفم، نتونستم ویست رو بفهمم 😅")
            return
        await event.reply(f"🎤 شنیدم: «{text}»")

    if not text.strip():
        return

    log(f"🔍 Owner: {text[:50]!r}")

    prompt = build_prompt("owner", text, is_owner=True)
    raw, last_error = await ask_gemini(prompt, chat_id=event.chat_id)
    if raw is None:
        await event.reply("متأسفم، الان نتونستم جواب بدم. دوباره امتحان کن 🙏")
        return

    # ===== UNPAUSE_ALL =====
    if "[UNPAUSE_ALL]" in raw:
        count = len(owner_last_seen)
        owner_last_seen.clear()
        custom_pause_durations.clear()
        log(f"▶️ AI unpaused all ({count} chats)")
        reply = re.sub(r"\[UNPAUSE_ALL\]", "", raw)
        reply = re.sub(r"\[(IMPORTANT|NORMAL|ENABLE_VOICE|DISABLE_VOICE)\]", "", reply).strip()
        if not reply:
            reply = f"✅ همه {count} چت آزاد شدن"
        last_bot_send_time = time.time()
        await event.reply(reply)
        return

    # ===== UNPAUSE =====
    m = re.search(r"\[UNPAUSE:\s*([^\]]+)\]", raw)
    if m:
        target = m.group(1).strip()
        cid = await resolve_chat_id(target)
        if cid and cid in owner_last_seen:
            del owner_last_seen[cid]
            custom_pause_durations.pop(cid, None)
            name = chat_names_cache.get(cid, str(cid))
            log(f"▶️ AI unpaused {cid} ({name})")
            reply = re.sub(r"\[UNPAUSE:[^\]]+\]", "", raw)
            reply = re.sub(r"\[(IMPORTANT|NORMAL|ENABLE_VOICE|DISABLE_VOICE)\]", "", reply).strip()
            if not reply:
                reply = f"✅ چت «{name}» آزاد شد"
            last_bot_send_time = time.time()
            await event.reply(reply)
        else:
            await event.reply(f"❓ نتونستم چت «{target}» رو توی لیست pause پیدا کنم")
        return

    # ===== PAUSE_FOREVER =====
    m = re.search(r"\[PAUSE_FOREVER:\s*([^\]]+)\]", raw)
    if m:
        target = m.group(1).strip()
        cid = await resolve_chat_id(target)
        if cid:
            owner_last_seen[cid] = time.time()
            custom_pause_durations[cid] = 10 * 365 * 24 * 3600  # 10 سال
            await cache_chat_name(cid)
            name = chat_names_cache.get(cid, str(cid))
            log(f"🛑 AI paused FOREVER: {cid} ({name})")
            reply = re.sub(r"\[PAUSE_FOREVER:[^\]]+\]", "", raw)
            reply = re.sub(r"\[(IMPORTANT|NORMAL|ENABLE_VOICE|DISABLE_VOICE)\]", "", reply).strip()
            if not reply:
                reply = f"🛑 چت «{name}» برای همیشه pause شد"
            last_bot_send_time = time.time()
            await event.reply(reply)
        else:
            await event.reply(f"❓ نتونستم «{target}» رو پیدا کنم")
        return

    # ===== PAUSE with duration =====
    m = re.search(r"\[PAUSE:\s*([^|\]]+)(?:\|\s*(\d+)\s*)?\]", raw)
    if m:
        target = m.group(1).strip()
        minutes = int(m.group(2)) if m.group(2) else (runtime_settings["pause_duration"] // 60)
        cid = await resolve_chat_id(target)
        if cid:
            owner_last_seen[cid] = time.time()
            custom_pause_durations[cid] = minutes * 60
            await cache_chat_name(cid)
            name = chat_names_cache.get(cid, str(cid))
            log(f"🛑 AI paused {cid} ({name}) for {minutes}m")
            reply = re.sub(r"\[PAUSE:[^\]]+\]", "", raw)
            reply = re.sub(r"\[(IMPORTANT|NORMAL|ENABLE_VOICE|DISABLE_VOICE)\]", "", reply).strip()
            if not reply:
                reply = f"🛑 چت «{name}» برای {minutes} دقیقه pause شد"
            last_bot_send_time = time.time()
            await event.reply(reply)
        else:
            await event.reply(f"❓ نتونستم «{target}» رو پیدا کنم")
        return

    # ===== FORWARD =====
    m = re.search(r"\[FORWARD:\s*([^|\]]+)(?:\|\s*(\d+)\s*)?\]", raw)
    if m:
        target = m.group(1).strip()
        count = int(m.group(2)) if m.group(2) else 1
        count = min(count, 20)  # حداکثر ۲۰
        cid = await resolve_chat_id(target)
        if cid:
            forwarded, err = await forward_last_messages(cid, count)
            name = chat_names_cache.get(cid, str(cid))
            if err:
                await event.reply(f"❌ خطا در فوروارد: {err}")
            else:
                log(f"📨 Forwarded {forwarded} msgs from {cid} ({name})")
                reply = re.sub(r"\[FORWARD:[^\]]+\]", "", raw)
                reply = re.sub(r"\[(IMPORTANT|NORMAL|ENABLE_VOICE|DISABLE_VOICE)\]", "", reply).strip()
                if not reply:
                    reply = f"📨 {forwarded} پیام از «{name}» فوروارد شد"
                last_bot_send_time = time.time()
                await event.reply(reply)
        else:
            await event.reply(f"❓ نتونستم «{target}» رو پیدا کنم")
        return

    # ===== CONTEXT =====
    m = re.search(r"\[CONTEXT:\s*([^\]]+)\]", raw)
    if m:
        target = m.group(1).strip()
        cid = await resolve_chat_id(target)
        if cid:
            ctx = await show_context(cid, 5)
            name = chat_names_cache.get(cid, str(cid))
            log(f"👁️ Context from {cid} ({name})")
            reply = re.sub(r"\[CONTEXT:[^\]]+\]", "", raw)
            reply = re.sub(r"\[(IMPORTANT|NORMAL|ENABLE_VOICE|DISABLE_VOICE)\]", "", reply).strip()
            message = f"👁️ آخرین پیام‌های {name}:\n\n{ctx}"
            if reply:
                message = reply + "\n\n" + message
            last_bot_send_time = time.time()
            await event.reply(message)
        else:
            await event.reply(f"❓ نتونستم «{target}» رو پیدا کنم")
        return

    # ===== GLOBAL_MUTE =====
    m = re.search(r"\[GLOBAL_MUTE:\s*(\d+)\]", raw)
    if m:
        minutes = int(m.group(1))
        global_mute_until = time.time() + minutes * 60
        log(f"🔇 Global mute for {minutes}m")
        reply = re.sub(r"\[GLOBAL_MUTE:[^\]]+\]", "", raw)
        reply = re.sub(r"\[(IMPORTANT|NORMAL|ENABLE_VOICE|DISABLE_VOICE)\]", "", reply).strip()
        if not reply:
            reply = f"🔇 به مدت {minutes} دقیقه به هیچ‌کس جواب نمی‌دم"
        last_bot_send_time = time.time()
        await event.reply(reply)
        return

    # ===== GLOBAL_UNMUTE =====
    if "[GLOBAL_UNMUTE]" in raw:
        was_muted = is_globally_muted()
        global_mute_until = 0
        log("🔊 Global unmute")
        reply = re.sub(r"\[GLOBAL_UNMUTE\]", "", raw)
        reply = re.sub(r"\[(IMPORTANT|NORMAL|ENABLE_VOICE|DISABLE_VOICE)\]", "", reply).strip()
        if not reply:
            reply = "🔊 سکوت برداشته شد" if was_muted else "🔊 از اول هم ساکت نبودم"
        last_bot_send_time = time.time()
        await event.reply(reply)
        return

    # ===== Memory tags =====
    enable_voice = "[ENABLE_VOICE]" in raw
    disable_voice = "[DISABLE_VOICE]" in raw

    if "[LEARN:" in raw:
        try:
            inner = raw.split("[LEARN:", 1)[1].split("]", 1)[0].strip()
            if "|" in inner:
                key, value = inner.split("|", 1)
                memory.setdefault("learned_facts", {})[key.strip()] = value.strip()
                save_memory_async()
                last_bot_send_time = time.time()
                await event.reply(f"✅ گرفتم! یادم می‌مونه: {key.strip()} → {value.strip()}")
                return
        except Exception as e:
            log(f"LEARN parse error: {str(e)[:80]}")

    if "[LEARN_RAW:" in raw:
        try:
            inner = raw.split("[LEARN_RAW:", 1)[1].split("]", 1)[0].strip()
            fact_id = f"fact_{len(memory.get('learned_facts', {})) + 1}"
            memory.setdefault("learned_facts", {})[fact_id] = inner
            save_memory_async()
            last_bot_send_time = time.time()
            await event.reply(f"✅ گرفتم! یادم می‌مونه: {inner}")
            return
        except Exception as e:
            log(f"LEARN_RAW parse error: {str(e)[:80]}")

    if "[FORGET:" in raw:
        try:
            key = raw.split("[FORGET:", 1)[1].split("]", 1)[0].strip()
            facts = memory.get("learned_facts", {})
            if key in facts:
                del facts[key]
                save_memory_async()
                last_bot_send_time = time.time()
                await event.reply(f"🗑️ باشه، فراموش کردم: {key}")
            else:
                last_bot_send_time = time.time()
                await event.reply(f"❓ همچین چیزی توی حافظه‌ام نبود: {key}")
            return
        except Exception as e:
            log(f"FORGET parse error: {str(e)[:80]}")

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
        last_bot_send_time = time.time()
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

    last_bot_send_time = time.time()
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

    if is_globally_muted():
        remaining = int(global_mute_until - time.time())
        log(f"🔇 Global mute — skipping user {sender.id} ({remaining//60}m left)")
        return

    if is_owner_paused(event.chat_id):
        remaining = pause_remaining(event.chat_id)
        log(f"🛑 Chat {event.chat_id} paused — {remaining//60}m {remaining%60}s left")
        return

    text = event.message.text or ""

    if not text.strip() and event.message.voice:
        if not runtime_settings.get("voice_input", True):
            return
        text = await handle_voice_input(event) or ""
        if not text:
            await event.reply("متأسفم، نتونستم ویست رو بفهمم 😅 می‌تونی متنی بنویسی؟")
            return

    if not text.strip():
        return

    user_key = str(sender.id)
    log(f"🔍 User {user_key}: {text[:50]!r}")

    prompt = build_prompt(sender.id, text, is_owner=False)
    raw, last_error = await ask_gemini(prompt, chat_id=event.chat_id)
    if raw is None:
        log("All models failed")
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
        log(f"📩 Silent alert. Wait {runtime_settings['alert_delay']}s...")
        await asyncio.sleep(runtime_settings["alert_delay"])
        await send_alert(alert)


async def start_api_server():
    app = create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log(f"🚀 API server started on port {PORT}")


async def main():
    await client.start()
    me = await client.get_me()
    log(f"✅ Logged in as {me.first_name} (id={me.id})")

    await start_api_server()

    log("🚀 Userbot is running...")
    log(f"📚 {len(memory.get('learned_facts', {}))} facts")
    log(f"🎤 Voice input: {'on' if runtime_settings['voice_input'] else 'off'} | output: {'on' if runtime_settings['voice_output'] else 'off'}")
    log(f"⏸️ Pause: {'on' if runtime_settings['pause_enabled'] else 'off'} ({runtime_settings['pause_duration']//60}min default)")
    log(f"🔐 API Key: {'set' if API_KEY != 'changeme' else 'NOT SET'}")
    log("🧠 AI commands: PAUSE, PAUSE_FOREVER, UNPAUSE, UNPAUSE_ALL, FORWARD, CONTEXT, GLOBAL_MUTE, GLOBAL_UNMUTE")

    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
