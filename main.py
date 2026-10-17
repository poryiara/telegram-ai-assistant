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
owner_last_seen = {}
chat_names_cache = {}
custom_pause_durations = {}
global_mute_until = 0
last_bot_send_time = 0
last_sender_cache = []
recent_actions = deque(maxlen=15)

recent_logs = deque(maxlen=100)
started_at = time.time()

def log(message):
    ts = time.strftime("%H:%M:%S")
    entry = {"time": ts, "text": message}
    recent_logs.append(entry)
    print(f"{ts} {message}")

def track_action(action_type, target, extra=""):
    ts = time.strftime("%H:%M:%S")
    recent_actions.append({
        "time": ts,
        "type": action_type,
        "target": target,
        "extra": extra,
    })

# ============ Gemini ============
ai = genai.Client(api_key=GEMINI_API_KEY)

MODELS = [
    "gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash",
    "gemini-3.5-flash", "gemini-3-flash",
    "gemini-3.5-flash-lite", "gemini-3.1-flash-lite",
]

STT_MODELS = [
    "gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash",
    "gemini-3.5-flash", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite",
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

# ============ Chat helpers ============
async def cache_chat_name(chat_id):
    try:
        entity = await client.get_entity(chat_id)
        name = getattr(entity, "first_name", None) or getattr(entity, "title", None) or str(chat_id)
        chat_names_cache[chat_id] = name
    except Exception as e:
        chat_names_cache[chat_id] = str(chat_id)
        log(f"⚠️ Cache name error {chat_id}: {str(e)[:60]}")

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

def track_sender(chat_id, name):
    global last_sender_cache
    last_sender_cache = [x for x in last_sender_cache if x["chat_id"] != chat_id]
    last_sender_cache.insert(0, {
        "chat_id": chat_id,
        "name": name,
        "timestamp": time.time(),
    })
    last_sender_cache = last_sender_cache[:30]

async def populate_senders_from_telegram(limit=10):
    """اگه cache خالی بود، از تلگرام چت‌های اخیر رو بگیر"""
    try:
        found = 0
        async for dialog in client.iter_dialogs(limit=25):
            if not dialog.is_user:
                continue
            if dialog.id == OWNER_ID:
                continue
            # آخرین پیام رو بگیر
            try:
                msgs = await client.get_messages(dialog.id, limit=1)
                if not msgs:
                    continue
                last = msgs[0]
                if last.out:
                    continue  # اگه آخرین پیام از خودت بوده، رد کن
            except:
                continue
            sender_name = getattr(dialog.entity, "first_name", "") or getattr(dialog.entity, "username", "") or str(dialog.id)
            chat_names_cache[dialog.id] = sender_name
            # timestamp واقعی پیام
            msg_time = last.date.timestamp() if last.date else time.time()
            last_sender_cache.append({
                "chat_id": dialog.id,
                "name": sender_name,
                "timestamp": msg_time,
            })
            found += 1
            if found >= limit:
                break
        # مرتب‌سازی بر اساس زمان
        last_sender_cache.sort(key=lambda x: x["timestamp"], reverse=True)
        last_sender_cache[:] = last_sender_cache[:30]
        log(f"📥 Populated {found} senders from Telegram")
        return found
    except Exception as e:
        log(f"⚠️ Populate error: {str(e)[:100]}")
        return 0

async def resolve_chat_id(target_str):
    target_str = str(target_str).strip()
    # اگه "last" یا مشابه باشه، آخرین فرستنده
    if target_str.lower() in ["last", "اخرین", "آخرین", "اخری", "آخری"]:
        if last_sender_cache:
            return last_sender_cache[0]["chat_id"]
        # اگه cache خالی، از تلگرام پر کن
        if await populate_senders_from_telegram() > 0:
            return last_sender_cache[0]["chat_id"]
        return None
    
    # اگه عددی بود، مستقیم برگردون
    try:
        return int(target_str)
    except:
        pass
    
    target_lower = target_str.lower()
    
    # جستجو توی chat_names_cache
    for cid, name in chat_names_cache.items():
        if name and (target_lower in str(name).lower() or str(name).lower() in target_lower):
            return cid
    
    # جستجو توی last_sender_cache
    for item in last_sender_cache:
        if target_lower in item["name"].lower() or item["name"].lower() in target_lower:
            return item["chat_id"]
    
    # جستجو توی دیالوگ‌های تلگرام
    try:
        async for dialog in client.iter_dialogs(limit=50):
            if not dialog.is_user:
                continue
            entity = dialog.entity
            entity_name = getattr(entity, "first_name", "") or getattr(entity, "title", "") or ""
            if entity_name and (target_lower in entity_name.lower() or entity_name.lower() in target_lower):
                chat_names_cache[dialog.id] = entity_name
                return dialog.id
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
            lines.append(f"- نام: {name} | chat_id: {chat_id} | مدت: {duration_min}د{custom} | باقی: {remaining//60}د {remaining%60}ث")
    if not found:
        return "=== PAUSED CHATS ===\nهیچ چتی pause نیست."
    return "\n".join(lines)

def build_last_senders_section():
    if not last_sender_cache:
        return "=== RECENT SENDERS ===\n⚠️ Cache خالیه. اگه owner پرسید، بگو [POPULATE_SENDERS] بزنه."
    lines = ["=== RECENT SENDERS (last 10) ==="]
    for i, item in enumerate(last_sender_cache[:10], 1):
        ago = int(time.time() - item["timestamp"])
        lines.append(f"{i}. نام: {item['name']} | chat_id: {item['chat_id']} | {ago//60}د {ago%60}ث پیش")
    return "\n".join(lines)

def build_recent_actions_section():
    if not recent_actions:
        return ""
    lines = ["=== RECENT ACTIONS (what you just did — DO NOT REPEAT) ==="]
    for a in list(recent_actions)[-6:]:
        extra = f" {a['extra']}" if a['extra'] else ""
        lines.append(f"- [{a['time']}] {a['type']} '{a['target']}'{extra}")
    lines.append("\n⚠️ If you already did something, do NOT do it again. The state is already changed.")
    lines.append("⚠️ If the user asks a general question, JUST ANSWER. Don't take action.")
    return "\n".join(lines)

def build_custom_rules_section():
    rules = memory.get("custom_rules", [])
    if not rules:
        return ""
    lines = ["=== CUSTOM RULES (OBEY) ==="]
    for rule in rules:
        lines.append(f"- {rule}")
    return "\n".join(lines)

def build_personality_section():
    p = memory.get("personality_override", "")
    if not p:
        return ""
    return f"=== PERSONALITY OVERRIDE ===\n{p}"

def build_global_mute_section():
    if is_globally_muted():
        remaining = int(global_mute_until - time.time())
        return f"=== GLOBAL MUTE ===\n⚠️ سکوت سراسری فعال — {remaining//60}د {remaining%60}ث باقی."
    return ""

# ============ Forward / Context ============
async def forward_last_messages(chat_id, count=1):
    try:
        messages = await client.get_messages(chat_id, limit=count * 2)
        if not messages:
            return 0, "هیچ پیامی پیدا نشد"
        user_msgs = [m for m in messages if not m.out][:count]
        if not user_msgs:
            user_msgs = messages[:count]
        await client.forward_messages("me", user_msgs)
        return len(user_msgs), None
    except Exception as e:
        return 0, str(e)

async def show_context(chat_id, count=5):
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
    return {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}

def fetch_memory():
    empty = {"learned_facts": {}, "owner_notes": [], "voice_preferences": {}, "custom_rules": [], "personality_override": ""}
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
            mem.setdefault("custom_rules", [])
            mem.setdefault("personality_override", "")
            mem.pop("conversations", None)
            return mem, data["sha"]
        elif r.status_code == 404:
            return empty, None
    except Exception as e:
        log(f"⚠️ Fetch error: {str(e)[:100]}")
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
        "custom_rules": mem.get("custom_rules", []),
        "personality_override": mem.get("personality_override", ""),
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
            log(f"⚠️ Push error: {r.status_code}")
    except Exception as e:
        log(f"⚠️ Push exception: {str(e)[:100]}")

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
    if memory.get("voice_preferences", {}).get(key) == value:
        return
    memory.setdefault("voice_preferences", {})[key] = value
    save_memory_async()

def build_owner_info():
    info = OWNER_INFO_BASE
    facts = memory.get("learned_facts", {})
    notes = memory.get("owner_notes", [])
    if facts:
        info += "\n\n=== Learned facts ===\n"
        for key, value in facts.items():
            info += f"- {key}: {value}\n"
    if notes:
        info += "\n\n=== Notes ===\n"
        for note in notes:
            info += f"- {note}\n"
    return info

def voice_status_block(pref):
    if pref is None:
        return """=== VOICE ===
Offer voice ONCE. If user says yes → [ENABLE_VOICE], no → [DISABLE_VOICE]."""
    if pref is True:
        return """=== VOICE ===
Voice is ON. Auto-sent with reply. Don't mention it.
If user says "دیگه ویس نمیخوام" → [DISABLE_VOICE]"""
    return """=== VOICE ===
Voice is OFF. Text only.
If user asks for voice → [ENABLE_VOICE]"""

def build_prompt(user_id, new_message, is_owner=False):
    hist = histories.setdefault(str(user_id), [])
    hist.append(f"User: {new_message}")
    del hist[:-MAX_HISTORY]

    if is_owner:
        vpref = get_voice_pref("owner")
        vblock = voice_status_block(vpref)
        paused_section = build_paused_section()
        senders_section = build_last_senders_section()
        mute_section = build_global_mute_section()
        rules_section = build_custom_rules_section()
        personality_section = build_personality_section()
        actions_section = build_recent_actions_section()
        
        prompt = f"""You are {OWNER_NAME}'s personal AI assistant. You are talking to {OWNER_NAME} THEMSELVES in Saved Messages.
Be warm, friendly, casual — like a close friend. Persian.

=== About your boss ===
{build_owner_info()}

{paused_section}

{senders_section}

{mute_section}

{actions_section}

{rules_section}

{personality_section}

=== COMMANDS (only when user gives a clear order) ===

Memory: [LEARN: key | value] · [LEARN_RAW: text] · [FORGET: key] · [SHOW_MEMORY]
Pause: [PAUSE: target | minutes] · [PAUSE_FOREVER: target] · [UNPAUSE: target] · [UNPAUSE_ALL]
Retrieval: [FORWARD: target | count] · [FORWARD_LAST | count] · [CONTEXT: target]
Senders: [WHO_LAST] · [LIST_SENDERS] · [POPULATE_SENDERS]
Mute: [GLOBAL_MUTE: minutes] · [GLOBAL_UNMUTE]
Rules: [RULE_ADD: text] · [RULE_REMOVE: index] · [RULE_LIST] · [RULE_CLEAR]
Personality: [PERSONALITY_SET: text] · [PERSONALITY_CLEAR] · [PERSONALITY_SHOW]

⚠️ TARGET: chat_id, name, or "آخرین/last"

=== ⚠️ CRITICAL RULES — READ CAREFULLY ===

1. **If user asks a QUESTION, JUST ANSWER.** Don't output any tag.
   Example: "مگه من سازنده تو نیستم؟" → Just answer warmly, NO tags.
   Example: "چطوری؟" → Just answer, NO tags.
   Example: "الان چه چیزایی بلدی؟" → List capabilities, NO tags.

2. **DON'T REPEAT ACTIONS.** Check RECENT ACTIONS above.
   - If you just unpaused X, don't unpause again.
   - If you just paused X, don't pause again.
   - If a chat is NOT in PAUSED CHATS, it's already unpaused. Don't try.

3. **Empty RECENT SENDERS → use [POPULATE_SENDERS]** to fetch from Telegram.
   Then AFTER the tool fetches, answer in a follow-up turn.

4. **Only output action tags when user EXPLICITLY gives a command.**
   NOT when:
   - They ask a question
   - They greet you
   - They ask about your capabilities
   - They ask "who was the last person?"
     → Answer from RECENT SENDERS data directly (no tag needed if data exists)
     → If data is empty, output [POPULATE_SENDERS] and wait

5. **Tag on its own line.** One action per message. Combine text + tag.

6. **MEMORY: only save when owner says "یادت باشه..." / "به خاطر بسپار..." / "فراموش کن..."**

{vblock}

Voice tags AFTER [IMPORTANT]/[NORMAL], each on own line.

=== CONVERSATION ===
{chr(10).join(hist)}
You:"""
    else:
        vpref = get_voice_pref(user_id)
        vblock = voice_status_block(vpref)
        personality_section = build_personality_section()
        prompt = f"""You are {OWNER_NAME}'s personal AI assistant, answering their DMs.
Warm, funny, real — like a close friend. Persian, casual.
Use emojis naturally. Match energy.

=== About {OWNER_NAME} ===
{build_owner_info()}

{personality_section}

=== Rules ===
- Same language as incoming (default Persian)
- If asked who you are, say "{OWNER_NAME}'s AI assistant"
- Never pretend to be {OWNER_NAME}
- Keep short (1-3 sentences)
- End with exactly one tag: [IMPORTANT] or [NORMAL]

=== TEACHING RULE ===
⚠️ ONLY {OWNER_NAME} can teach you. Regular users → politely decline.
EXCEPTION: Voice requests are NOT teaching.

{vblock}

Voice tags after [IMPORTANT]/[NORMAL], on own lines.

=== CONVERSATION ===
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

        delay = RETRY_DELAYS[min(round_num - 1, len(RETRY_DELAYS) - 1)]
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
    headers = {"Title": "Important Alert", "Priority": "urgent", "Tags": "rotating_light,warning"}
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
            return None

        for model_name in STT_MODELS:
            try:
                response = ai.models.generate_content(
                    model=model_name,
                    contents=[types.Content(parts=[
                        types.Part.from_bytes(data=audio_data, mime_type="audio/ogg"),
                        types.Part.from_text(text="این فایل صوتی رو دقیقاً به همون زبونی که صحبت شده، رونویسی کن. فقط متن رو بده."),
                    ])]
                )
                text = (response.text or "").strip()
                if text:
                    log(f"🎤 STT: {model_name}")
                    return text
            except Exception as e:
                continue

        elapsed = time.time() - started
        if elapsed > max_time:
            return None
        await asyncio.sleep(RETRY_DELAYS[min(round_num - 1, len(RETRY_DELAYS) - 1)])

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
        "[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF"
        "\U0001F1E0-\U0001F1FF\U00002702-\U000027B0\U000024C2-\U0001F251"
        "\U0001f926-\U0001f937\U00010000-\U0010ffff\u2640-\u2642"
        "\u2600-\u2B55\u200d\u23cf\u23e9\u231a\ufe0f\u3030]+",
        flags=re.UNICODE
    )
    text = emoji_pattern.sub("", text)
    text = text.replace("*", "").replace("_", "").replace("`", "")
    return re.sub(r"\s+", " ", text).strip()

async def text_to_voice(text, voice=None):
    try:
        voice = voice or runtime_settings.get("voice_name", VOICE_NAME)
        clean = clean_text_for_voice(text)
        if not clean or len(clean) < 2:
            return None
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp_mp3:
            mp3_path = tmp_mp3.name
        await edge_tts.Communicate(clean, voice).save(mp3_path)
        ogg_path = mp3_path.replace(".mp3", ".ogg")
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", mp3_path, "-c:a", "libopus", "-b:a", "48k",
             "-vbr", "on", "-compression_level", "10", "-frame_duration", "60",
             "-application", "voip", ogg_path],
            capture_output=True, timeout=30
        )
        try:
            os.unlink(mp3_path)
        except:
            pass
        return ogg_path if result.returncode == 0 else None
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
        log(f"⚠️ Voice send: {str(e)[:80]}")
    finally:
        try:
            os.unlink(voice_path)
        except:
            pass


client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)


# ============ REST API ============
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
        return json_response({
            "online": True, "user_id": me.id, "username": me.username,
            "first_name": me.first_name,
            "uptime_seconds": int(time.time() - started_at),
            "facts_count": len(memory.get("learned_facts", {})),
            "notes_count": len(memory.get("owner_notes", [])),
            "voice_prefs_count": len(memory.get("voice_preferences", {})),
            "paused_chats_count": sum(1 for cid in owner_last_seen if is_owner_paused(cid)),
            "globally_muted": is_globally_muted(),
            "custom_rules_count": len(memory.get("custom_rules", [])),
            "personality_override": memory.get("personality_override", ""),
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
    log(f"⚙️ Settings: {updated}")
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
            paused.append({"chat_id": chat_id, "name": name, "remaining_seconds": remaining,
                          "custom_duration": custom_pause_durations.get(chat_id)})
    paused.sort(key=lambda x: x["remaining_seconds"])
    return json_response({"paused": paused})

async def api_pause(request):
    if not api_auth(request):
        return json_response({"error": "Unauthorized"}, 401)
    try:
        data = await request.json()
        chat_id = int(data.get("chat_id"))
        duration = data.get("duration")
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
    track_action("PAUSE", chat_id, f"{get_pause_duration(chat_id)//60}m")
    return json_response({"ok": True})

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
    track_action("UNPAUSE", chat_id)
    return json_response({"ok": True})

async def api_resume_all(request):
    if not api_auth(request):
        return json_response({"error": "Unauthorized"}, 401)
    count = len(owner_last_seen)
    owner_last_seen.clear()
    custom_pause_durations.clear()
    track_action("UNPAUSE_ALL", count)
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
        "rules": memory.get("custom_rules", []),
        "personality": memory.get("personality_override", ""),
    })

async def api_fact_add(request):
    if not api_auth(request):
        return json_response({"error": "Unauthorized"}, 401)
    try:
        data = await request.json()
        key = data.get("key", "").strip()
        value = data.get("value", "").strip()
        if not key or not value:
            return json_response({"error": "required"}, 400)
        memory.setdefault("learned_facts", {})[key] = value
        save_memory_async()
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


# ============ Owner activity tracker ============
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
    if getattr(event.message, 'forward', None):
        return
    mark_owner_active(event.chat_id)
    remaining = pause_remaining(event.chat_id)
    log(f"🛑 Paused {event.chat_id} for {remaining//60}m")


# ============ Owner message handler ============
@client.on(events.NewMessage(outgoing=True))
async def on_owner_message(event):
    global global_mute_until, last_bot_send_time

    try:
        me = await client.get_me()
    except Exception as e:
        return

    if event.chat_id != me.id or event.sender_id != me.id:
        return

    if time.time() - last_bot_send_time < 1.5:
        return

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

    # ===== POPULATE_SENDERS =====
    if "[POPULATE_SENDERS]" in raw:
        await event.reply("⏳ دارم چت‌های اخیرت رو از تلگرام می‌خونم...")
        count = await populate_senders_from_telegram()
        if count == 0:
            await event.reply("❓ چت خصوصی با پیام دریافتی پیدا نکردم")
        else:
            # حالا دوباره از AI بخواه با داده‌ی جدید جواب بده
            prompt2 = build_prompt("owner", text + " [داده‌ها لود شد، الان جواب بده]", is_owner=True)
            raw2, _ = await ask_gemini(prompt2, chat_id=event.chat_id)
            if raw2:
                reply = re.sub(r"\[(POPULATE_SENDERS|WHO_LAST|LIST_SENDERS)\]", "", raw2)
                reply = re.sub(r"\[(IMPORTANT|NORMAL|ENABLE_VOICE|DISABLE_VOICE)\]", "", reply).strip()
                if reply:
                    last_bot_send_time = time.time()
                    await event.reply(reply)
                    return
            await event.reply(f"✅ {count} چت اخیر لود شد. دوباره بپرس.")
        return

    # ===== UNPAUSE_ALL =====
    if "[UNPAUSE_ALL]" in raw:
        count = len(owner_last_seen)
        owner_last_seen.clear()
        custom_pause_durations.clear()
        track_action("UNPAUSE_ALL", count)
        log(f"▶️ Unpaused all ({count})")
        reply = re.sub(r"\[(UNPAUSE_ALL|IMPORTANT|NORMAL|ENABLE_VOICE|DISABLE_VOICE)\]", "", raw).strip()
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
        if cid is None:
            await event.reply(f"❓ «{target}» رو نمی‌شناسم. اسم دقیق‌تر یا chat_id بده.")
            return
        name = chat_names_cache.get(cid, str(cid))
        if cid in owner_last_seen:
            del owner_last_seen[cid]
            custom_pause_durations.pop(cid, None)
            track_action("UNPAUSE", cid, name)
            log(f"▶️ Unpaused {cid} ({name})")
            reply = re.sub(r"\[(UNPAUSE:[^\]]+|IMPORTANT|NORMAL|ENABLE_VOICE|DISABLE_VOICE)\]", "", raw).strip()
            if not reply:
                reply = f"✅ «{name}» آزاد شد"
        else:
            reply = f"ℹ️ «{name}» از قبل آزاده (توی لیست pause نبود)"
        last_bot_send_time = time.time()
        await event.reply(reply)
        return

    # ===== PAUSE_FOREVER =====
    m = re.search(r"\[PAUSE_FOREVER:\s*([^\]]+)\]", raw)
    if m:
        target = m.group(1).strip()
        cid = await resolve_chat_id(target)
        if cid:
            owner_last_seen[cid] = time.time()
            custom_pause_durations[cid] = 10 * 365 * 24 * 3600
            await cache_chat_name(cid)
            name = chat_names_cache.get(cid, str(cid))
            track_action("PAUSE_FOREVER", cid, name)
            log(f"🛑 Paused FOREVER {cid} ({name})")
            reply = re.sub(r"\[(PAUSE_FOREVER:[^\]]+|IMPORTANT|NORMAL|ENABLE_VOICE|DISABLE_VOICE)\]", "", raw).strip()
            if not reply:
                reply = f"🛑 «{name}» برای همیشه pause شد"
            last_bot_send_time = time.time()
            await event.reply(reply)
        else:
            await event.reply(f"❓ «{target}» رو پیدا نکردم")
        return

    # ===== PAUSE =====
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
            track_action("PAUSE", cid, f"{name} {minutes}m")
            log(f"🛑 Paused {cid} ({name}) {minutes}m")
            reply = re.sub(r"\[(PAUSE:[^\]]+|IMPORTANT|NORMAL|ENABLE_VOICE|DISABLE_VOICE)\]", "", raw).strip()
            if not reply:
                reply = f"🛑 «{name}» برای {minutes} دقیقه pause شد"
            last_bot_send_time = time.time()
            await event.reply(reply)
        else:
            await event.reply(f"❓ «{target}» رو پیدا نکردم")
        return

    # ===== FORWARD_LAST =====
    m = re.search(r"\[FORWARD_LAST(?:\s*\|\s*(\d+))?\]", raw)
    if m:
        count = int(m.group(1)) if m.group(1) else 1
        count = min(count, 20)
        if not last_sender_cache:
            await populate_senders_from_telegram()
        if not last_sender_cache:
            await event.reply("❓ هیچ پیامی نیومده")
            return
        last = last_sender_cache[0]
        forwarded, err = await forward_last_messages(last["chat_id"], count)
        if err:
            await event.reply(f"❌ خطا: {err}")
        else:
            track_action("FORWARD_LAST", last["chat_id"], last["name"])
            log(f"📨 Forwarded {forwarded} from last ({last['name']})")
            reply = re.sub(r"\[(FORWARD_LAST[^\]]*|IMPORTANT|NORMAL|ENABLE_VOICE|DISABLE_VOICE)\]", "", raw).strip()
            if not reply:
                reply = f"📨 {forwarded} پیام از «{last['name']}»"
            last_bot_send_time = time.time()
            await event.reply(reply)
        return

    # ===== FORWARD =====
    m = re.search(r"\[FORWARD:\s*([^|\]]+)(?:\|\s*(\d+)\s*)?\]", raw)
    if m:
        target = m.group(1).strip()
        count = int(m.group(2)) if m.group(2) else 1
        count = min(count, 20)
        cid = await resolve_chat_id(target)
        if cid:
            forwarded, err = await forward_last_messages(cid, count)
            name = chat_names_cache.get(cid, str(cid))
            if err:
                await event.reply(f"❌ خطا: {err}")
            else:
                track_action("FORWARD", cid, f"{name} {forwarded}")
                log(f"📨 Forwarded {forwarded} from {cid}")
                reply = re.sub(r"\[(FORWARD:[^\]]+|IMPORTANT|NORMAL|ENABLE_VOICE|DISABLE_VOICE)\]", "", raw).strip()
                if not reply:
                    reply = f"📨 {forwarded} پیام از «{name}»"
                last_bot_send_time = time.time()
                await event.reply(reply)
        else:
            await event.reply(f"❓ «{target}» رو پیدا نکردم")
        return

    # ===== CONTEXT =====
    m = re.search(r"\[CONTEXT:\s*([^\]]+)\]", raw)
    if m:
        target = m.group(1).strip()
        cid = await resolve_chat_id(target)
        if cid:
            ctx = await show_context(cid, 5)
            name = chat_names_cache.get(cid, str(cid))
            log(f"👁️ Context from {cid}")
            reply = re.sub(r"\[(CONTEXT:[^\]]+|IMPORTANT|NORMAL|ENABLE_VOICE|DISABLE_VOICE)\]", "", raw).strip()
            msg = f"👁️ {name}:\n\n{ctx}"
            if reply:
                msg = reply + "\n\n" + msg
            last_bot_send_time = time.time()
            await event.reply(msg)
        else:
            await event.reply(f"❓ «{target}» رو پیدا نکردم")
        return

    # ===== WHO_LAST / LIST_SENDERS =====
    if "[WHO_LAST]" in raw or "[LIST_SENDERS]" in raw:
        if not last_sender_cache:
            await populate_senders_from_telegram()
        if not last_sender_cache:
            await event.reply("❓ از زمان ری‌استارت هنوز پیامی نیومده و توی تلگرام هم پیدا نکردم")
            return
        lines = []
        if "[WHO_LAST]" in raw:
            last = last_sender_cache[0]
            ago = int(time.time() - last["timestamp"])
            lines.append(f"آخرین نفر: **{last['name']}** (`{last['chat_id']}`) — {ago//60}د {ago%60}ث پیش")
        if "[LIST_SENDERS]" in raw:
            lines.append("\n**آخرین فرستنده‌ها:**")
            for i, item in enumerate(last_sender_cache[:10], 1):
                ago = int(time.time() - item["timestamp"])
                lines.append(f"{i}. {item['name']} (`{item['chat_id']}`) — {ago//60}د پیش")
        reply = re.sub(r"\[(WHO_LAST|LIST_SENDERS|IMPORTANT|NORMAL|ENABLE_VOICE|DISABLE_VOICE)\]", "", raw).strip()
        msg = "\n".join(lines)
        if reply:
            msg = reply + "\n\n" + msg
        last_bot_send_time = time.time()
        await event.reply(msg)
        return

    # ===== GLOBAL_MUTE =====
    m = re.search(r"\[GLOBAL_MUTE:\s*(\d+)\]", raw)
    if m:
        minutes = int(m.group(1))
        global_mute_until = time.time() + minutes * 60
        track_action("GLOBAL_MUTE", f"{minutes}m")
        log(f"🔇 Global mute {minutes}m")
        reply = re.sub(r"\[(GLOBAL_MUTE:[^\]]+|IMPORTANT|NORMAL|ENABLE_VOICE|DISABLE_VOICE)\]", "", raw).strip()
        if not reply:
            reply = f"🔇 {minutes} دقیقه ساکت"
        last_bot_send_time = time.time()
        await event.reply(reply)
        return

    # ===== GLOBAL_UNMUTE =====
    if "[GLOBAL_UNMUTE]" in raw:
        global_mute_until = 0
        track_action("GLOBAL_UNMUTE", "")
        reply = re.sub(r"\[(GLOBAL_UNMUTE|IMPORTANT|NORMAL|ENABLE_VOICE|DISABLE_VOICE)\]", "", raw).strip()
        if not reply:
            reply = "🔊 سکوت برداشته شد"
        last_bot_send_time = time.time()
        await event.reply(reply)
        return

    # ===== RULE_ADD =====
    m = re.search(r"\[RULE_ADD:\s*([^\]]+)\]", raw)
    if m:
        rule = m.group(1).strip()
        memory.setdefault("custom_rules", [])
        if rule not in memory["custom_rules"]:
            memory["custom_rules"].append(rule)
            save_memory_async()
            track_action("RULE_ADD", rule[:40])
            log(f"📏 Rule added")
        reply = re.sub(r"\[(RULE_ADD:[^\]]+|IMPORTANT|NORMAL|ENABLE_VOICE|DISABLE_VOICE)\]", "", raw).strip()
        if not reply:
            reply = f"✅ قانون اضافه شد (#{len(memory['custom_rules'])})\n📏 {rule}"
        last_bot_send_time = time.time()
        await event.reply(reply)
        return

    # ===== RULE_REMOVE =====
    m = re.search(r"\[RULE_REMOVE:\s*(\d+)\]", raw)
    if m:
        idx = int(m.group(1)) - 1
        rules = memory.get("custom_rules", [])
        if 0 <= idx < len(rules):
            removed = rules.pop(idx)
            save_memory_async()
            track_action("RULE_REMOVE", removed[:40])
            reply = re.sub(r"\[(RULE_REMOVE:\d+|IMPORTANT|NORMAL|ENABLE_VOICE|DISABLE_VOICE)\]", "", raw).strip()
            if not reply:
                reply = f"🗑️ حذف شد: {removed}"
            last_bot_send_time = time.time()
            await event.reply(reply)
        else:
            await event.reply(f"❓ قانون #{idx+1} نیست")
        return

    # ===== RULE_LIST =====
    if "[RULE_LIST]" in raw:
        rules = memory.get("custom_rules", [])
        if not rules:
            await event.reply("📏 قانونی نیست")
            return
        lines = ["📏 **قوانین:**\n"] + [f"{i}. {r}" for i, r in enumerate(rules, 1)]
        reply = re.sub(r"\[(RULE_LIST|IMPORTANT|NORMAL|ENABLE_VOICE|DISABLE_VOICE)\]", "", raw).strip()
        msg = "\n".join(lines)
        if reply:
            msg = reply + "\n\n" + msg
        last_bot_send_time = time.time()
        await event.reply(msg)
        return

    # ===== RULE_CLEAR =====
    if "[RULE_CLEAR]" in raw:
        count = len(memory.get("custom_rules", []))
        memory["custom_rules"] = []
        save_memory_async()
        reply = re.sub(r"\[(RULE_CLEAR|IMPORTANT|NORMAL|ENABLE_VOICE|DISABLE_VOICE)\]", "", raw).strip()
        if not reply:
            reply = f"🧹 {count} قانون پاک شد"
        last_bot_send_time = time.time()
        await event.reply(reply)
        return

    # ===== PERSONALITY_SET =====
    m = re.search(r"\[PERSONALITY_SET:\s*([^\]]+)\]", raw)
    if m:
        p = m.group(1).strip()
        memory["personality_override"] = p
        save_memory_async()
        track_action("PERSONALITY_SET", p[:40])
        log(f"🎭 Personality set")
        reply = re.sub(r"\[(PERSONALITY_SET:[^\]]+|IMPORTANT|NORMAL|ENABLE_VOICE|DISABLE_VOICE)\]", "", raw).strip()
        if not reply:
            reply = f"🎭 شخصیت تغییر کرد:\n{p}"
        last_bot_send_time = time.time()
        await event.reply(reply)
        return

    # ===== PERSONALITY_CLEAR =====
    if "[PERSONALITY_CLEAR]" in raw:
        memory["personality_override"] = ""
        save_memory_async()
        reply = re.sub(r"\[(PERSONALITY_CLEAR|IMPORTANT|NORMAL|ENABLE_VOICE|DISABLE_VOICE)\]", "", raw).strip()
        if not reply:
            reply = "🎭 پیش‌فرض"
        last_bot_send_time = time.time()
        await event.reply(reply)
        return

    # ===== PERSONALITY_SHOW =====
    if "[PERSONALITY_SHOW]" in raw:
        p = memory.get("personality_override", "")
        if not p:
            await event.reply("🎭 هیچ override فعال نیست")
            return
        await event.reply(f"🎭 **شخصیت:**\n{p}")
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
                track_action("LEARN", key.strip())
                last_bot_send_time = time.time()
                await event.reply(f"✅ گرفتم! یادم می‌مونه: {key.strip()} → {value.strip()}")
                return
        except Exception as e:
            log(f"LEARN error: {str(e)[:80]}")

    if "[LEARN_RAW:" in raw:
        try:
            inner = raw.split("[LEARN_RAW:", 1)[1].split("]", 1)[0].strip()
            fact_id = f"fact_{len(memory.get('learned_facts', {})) + 1}"
            memory.setdefault("learned_facts", {})[fact_id] = inner
            save_memory_async()
            track_action("LEARN_RAW", inner[:40])
            last_bot_send_time = time.time()
            await event.reply(f"✅ گرفتم! یادم می‌مونه: {inner}")
            return
        except Exception as e:
            log(f"LEARN_RAW error: {str(e)[:80]}")

    if "[FORGET:" in raw:
        try:
            key = raw.split("[FORGET:", 1)[1].split("]", 1)[0].strip()
            facts = memory.get("learned_facts", {})
            if key in facts:
                del facts[key]
                save_memory_async()
                last_bot_send_time = time.time()
                await event.reply(f"🗑️ فراموش کردم: {key}")
            else:
                last_bot_send_time = time.time()
                await event.reply(f"❓ نبود: {key}")
            return
        except Exception as e:
            log(f"FORGET error: {str(e)[:80]}")

    if "[SHOW_MEMORY]" in raw:
        facts = memory.get("learned_facts", {})
        notes = memory.get("owner_notes", [])
        reply = "🧠 **حافظه:**\n\n"
        if facts:
            reply += "**فکت‌ها:**\n"
            for k, v in facts.items():
                reply += f"• {k}: {v}\n"
        else:
            reply += "خالی\n"
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

    reply = re.sub(r"\[(IMPORTANT|NORMAL|ENABLE_VOICE|DISABLE_VOICE)\]", "", raw).strip()
    if not reply:
        return

    histories.setdefault("owner", []).append(f"You: {reply}")
    last_bot_send_time = time.time()
    await event.reply(reply)

    should_voice = get_voice_pref("owner") is True
    if should_voice and not disable_voice:
        await send_voice_reply(event.chat_id, event.message.id, reply)


# ============ Regular user handler ============
@client.on(events.NewMessage(incoming=True, func=lambda e: e.is_private))
async def on_dm(event):
    sender = await event.get_sender()
    if sender.bot or sender.id == OWNER_ID:
        return

    sender_name = getattr(sender, "first_name", "") or getattr(sender, "username", "") or str(sender.id)
    track_sender(sender.id, sender_name)
    if sender.id not in chat_names_cache:
        chat_names_cache[sender.id] = sender_name

    if is_globally_muted():
        remaining = int(global_mute_until - time.time())
        log(f"🔇 Muted — skip {sender.id}")
        return

    if is_owner_paused(event.chat_id):
        remaining = pause_remaining(event.chat_id)
        log(f"🛑 Paused {event.chat_id} — {remaining//60}m left")
        return

    text = event.message.text or ""

    if not text.strip() and event.message.voice:
        if not runtime_settings.get("voice_input", True):
            return
        text = await handle_voice_input(event) or ""
        if not text:
            await event.reply("متأسفم، نتونستم ویست رو بفهمم 😅")
            return

    if not text.strip():
        return

    user_key = str(sender.id)
    log(f"🔍 User {user_key}: {text[:50]!r}")

    prompt = build_prompt(sender.id, text, is_owner=False)
    raw, last_error = await ask_gemini(prompt, chat_id=event.chat_id)
    if raw is None:
        return

    important = "[IMPORTANT]" in raw
    enable_voice = "[ENABLE_VOICE]" in raw
    disable_voice = "[DISABLE_VOICE]" in raw

    if enable_voice:
        set_voice_pref(user_key, True)
    elif disable_voice:
        set_voice_pref(user_key, False)

    reply = re.sub(r"\[(IMPORTANT|NORMAL|ENABLE_VOICE|DISABLE_VOICE)\]", "", raw).strip()
    if not reply:
        return

    histories.setdefault(user_key, []).append(f"You: {reply}")
    await event.reply(reply)

    should_voice = get_voice_pref(user_key) is True
    if should_voice and not disable_voice:
        await send_voice_reply(event.chat_id, event.message.id, reply)

    if important:
        username = f"@{sender.username}" if sender.username else "—"
        alert = (
            f"⚠️ پیام مهم\n\n"
            f"👤 از: {sender_name} ({username})\n"
            f"🆔 `{sender.id}`\n\n"
            f"📩 {text}\n\n"
            f"🤖 {reply}"
        )
        await client.send_message("me", alert, silent=True)
        log(f"📩 Alert. Wait {runtime_settings['alert_delay']}s...")
        await asyncio.sleep(runtime_settings["alert_delay"])
        await send_alert(alert)


async def start_api_server():
    app = create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log(f"🚀 API on port {PORT}")


async def main():
    await client.start()
    me = await client.get_me()
    log(f"✅ Logged in as {me.first_name} (id={me.id})")

    await start_api_server()

    log("🚀 Userbot running...")
    log(f"📚 {len(memory.get('learned_facts', {}))} facts | 📏 {len(memory.get('custom_rules', []))} rules")
    log(f"🎤 Voice: in={'on' if runtime_settings['voice_input'] else 'off'} out={'on' if runtime_settings['voice_output'] else 'off'}")
    log(f"⏸️ Pause: {'on' if runtime_settings['pause_enabled'] else 'off'} ({runtime_settings['pause_duration']//60}min)")
    log("🧠 Senders cache: will populate on demand from Telegram")

    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
