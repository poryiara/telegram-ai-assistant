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
senders_dirty = False  # برای اینکه بدونیم last_senders تغییر کرده و باید ذخیره بشه

recent_logs = deque(maxlen=100)
started_at = time.time()

def log(message):
    ts = time.strftime("%H:%M:%S")
    entry = {"time": ts, "text": message}
    recent_logs.append(entry)
    print(f"{ts} {message}")

def track_action(action_type, target, extra=""):
    ts = time.strftime("%H:%M:%S")
    recent_actions.append({"time": ts, "type": action_type, "target": target, "extra": extra})

# ============ Gemini ============
ai = genai.Client(api_key=GEMINI_API_KEY)

MODELS = ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash",
          "gemini-3.5-flash", "gemini-3-flash", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]
STT_MODELS = ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash",
              "gemini-3.5-flash", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]
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
    """ذخیره آخرین فرستنده — با پرچم dirty برای ذخیره بعدی"""
    global last_sender_cache, senders_dirty
    last_sender_cache = [x for x in last_sender_cache if x["chat_id"] != chat_id]
    last_sender_cache.insert(0, {"chat_id": chat_id, "name": name, "timestamp": time.time()})
    last_sender_cache = last_sender_cache[:30]
    senders_dirty = True

async def populate_senders_from_telegram(limit=15):
    """پر کردن cache از تلگرام — با آخرین پیام دریافتی، بدون bot"""
    global last_sender_cache, senders_dirty
    try:
        results = []
        scanned = 0
        async for dialog in client.iter_dialogs(limit=60):
            if not dialog.is_user:
                continue
            if dialog.id == OWNER_ID:
                continue
            entity = dialog.entity
            # skip bots
            if getattr(entity, "bot", False):
                continue
            # skip deleted
            if getattr(entity, "deleted", False):
                continue
            scanned += 1
            
            try:
                msgs = await client.get_messages(dialog.id, limit=15)
            except:
                continue
            
            # آخرین پیام دریافتی
            last_incoming = None
            for m in msgs:
                if not m.out:
                    last_incoming = m
                    break
            
            if not last_incoming:
                continue
            
            sender_name = getattr(entity, "first_name", "") or getattr(entity, "username", "") or str(dialog.id)
            chat_names_cache[dialog.id] = sender_name
            
            try:
                ts = last_incoming.date.timestamp()
            except:
                ts = time.time()
            
            results.append({
                "chat_id": dialog.id,
                "name": sender_name,
                "timestamp": ts,
            })
        
        results.sort(key=lambda x: x["timestamp"], reverse=True)
        last_sender_cache.clear()
        last_sender_cache.extend(results[:limit])
        senders_dirty = True
        log(f"📥 Populated {len(results)} senders from {scanned} real users")
        return len(last_sender_cache)
    except Exception as e:
        log(f"⚠️ Populate error: {str(e)[:100]}")
        return 0

async def resolve_chat_id(target_str):
    target_str = str(target_str).strip()
    if target_str.lower() in ["last", "اخرین", "آخرین", "اخری", "آخری"]:
        if last_sender_cache:
            return last_sender_cache[0]["chat_id"]
        if await populate_senders_from_telegram() > 0:
            return last_sender_cache[0]["chat_id"]
        return None
    
    try:
        return int(target_str)
    except:
        pass
    
    target_lower = target_str.lower()
    
    for cid, name in chat_names_cache.items():
        if name and (target_lower in str(name).lower() or str(name).lower() in target_lower):
            return cid
    
    for item in last_sender_cache:
        if target_lower in item["name"].lower() or item["name"].lower() in target_lower:
            return item["chat_id"]
    
    try:
        async for dialog in client.iter_dialogs(limit=60):
            if not dialog.is_user:
                continue
            entity = dialog.entity
            if getattr(entity, "bot", False):
                continue
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
    lines = ["=== PAUSED CHATS ==="]
    found = False
    for chat_id in list(owner_last_seen.keys()):
        remaining = pause_remaining(chat_id)
        if remaining > 0:
            found = True
            name = chat_names_cache.get(chat_id, "نامشخص")
            dm = get_pause_duration(chat_id) // 60
            lines.append(f"- {name} (id:{chat_id}) | {dm}د | باقی: {remaining//60}د {remaining%60}ث")
    if not found:
        return "=== PAUSED CHATS ===\nهیچ چتی pause نیست."
    return "\n".join(lines)

def build_last_senders_section():
    if not last_sender_cache:
        return "=== RECENT SENDERS ===\nخالی. اگه پرسید، [POPULATE_SENDERS] بزن."
    lines = ["=== RECENT SENDERS ==="]
    for i, item in enumerate(last_sender_cache[:10], 1):
        ago = int(time.time() - item["timestamp"])
        lines.append(f"{i}. {item['name']} (id:{item['chat_id']}) — {ago//60}د {ago%60}ث پیش")
    return "\n".join(lines)

def build_recent_actions_section():
    if not recent_actions:
        return ""
    lines = ["=== RECENT ACTIONS (کارهایی که همین الان انجام دادی — تکرارشون نکن) ==="]
    for a in list(recent_actions)[-6:]:
        extra = f" {a['extra']}" if a['extra'] else ""
        lines.append(f"- [{a['time']}] {a['type']} '{a['target']}'{extra}")
    return "\n".join(lines)

def build_custom_rules_section():
    rules = memory.get("custom_rules", [])
    if not rules:
        return ""
    return "=== CUSTOM RULES ===\n" + "\n".join(f"- {r}" for r in rules)

def build_personality_section():
    p = memory.get("personality_override", "")
    return f"=== PERSONALITY ===\n{p}" if p else ""

def build_global_mute_section():
    if is_globally_muted():
        r = int(global_mute_until - time.time())
        return f"=== GLOBAL MUTE ===\n{r//60}د {r%60}ث باقی."
    return ""

# ============ Forward / Context ============
async def forward_last_messages(chat_id, count=1):
    try:
        messages = await client.get_messages(chat_id, limit=count * 3)
        if not messages:
            return 0, "هیچ پیامی نیست"
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
            text = msg.text or "[رسانه]"
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
    empty = {"learned_facts": {}, "owner_notes": [], "voice_preferences": {},
             "custom_rules": [], "personality_override": "", "last_senders": []}
    if not GITHUB_TOKEN:
        return empty, None
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{MEMORY_PATH}"
    try:
        r = requests.get(url, headers=github_headers(), params={"ref": GITHUB_BRANCH}, timeout=10)
        if r.status_code == 200:
            data = r.json()
            content = base64.b64decode(data["content"]).decode("utf-8")
            mem = json.loads(content)
            for k, v in empty.items():
                mem.setdefault(k, v)
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
        "last_senders": mem.get("last_senders", [])[:30],
    }
    content = base64.b64encode(json.dumps(to_save, ensure_ascii=False, indent=2).encode("utf-8")).decode("utf-8")
    payload = {"message": "Update AI memory", "content": content, "branch": GITHUB_BRANCH}
    if sha:
        payload["sha"] = sha
    try:
        r = requests.put(url, headers=github_headers(), json=payload, timeout=15)
        if r.status_code in (200, 201):
            log("💾 Memory saved")
    except Exception as e:
        log(f"⚠️ Push exception: {str(e)[:100]}")

def save_memory_async():
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, push_memory, memory)

def save_senders_if_dirty():
    """اگه last_senders تغییر کرده، ذخیره کن"""
    global senders_dirty
    if senders_dirty:
        memory["last_senders"] = list(last_sender_cache)[:30]
        save_memory_async()
        senders_dirty = False

memory, _sha = fetch_memory()

# بارگذاری last_senders از memory
if memory.get("last_senders"):
    last_sender_cache.extend(memory["last_senders"])
    log(f"📥 Loaded {len(last_sender_cache)} last_senders from memory")

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
        for k, v in facts.items():
            info += f"- {k}: {v}\n"
    if notes:
        info += "\n\n=== Notes ===\n"
        for n in notes:
            info += f"- {n}\n"
    return info

def voice_status_block(pref):
    if pref is None:
        return "=== VOICE ===\nOffer ONCE. yes→[ENABLE_VOICE] no→[DISABLE_VOICE]"
    if pref is True:
        return "=== VOICE ===\nON. Don't mention. user says no→[DISABLE_VOICE]"
    return "=== VOICE ===\nOFF. user asks→[ENABLE_VOICE]"

def build_prompt(user_id, new_message, is_owner=False):
    hist = histories.setdefault(str(user_id), [])
    hist.append(f"User: {new_message}")
    del hist[:-MAX_HISTORY]

    if is_owner:
        vpref = get_voice_pref("owner")
        vblock = voice_status_block(vpref)
        prompt = f"""You are {OWNER_NAME}'s personal AI assistant. Talking to {OWNER_NAME} THEMSELVES.
Persian, warm, casual.

=== About your boss ===
{build_owner_info()}

{build_paused_section()}

{build_last_senders_section()}

{build_global_mute_section()}

{build_recent_actions_section()}

{build_custom_rules_section()}

{build_personality_section()}

=== COMMANDS (only when user gives a CLEAR ORDER) ===

Memory: [LEARN: key | value] · [LEARN_RAW: text] · [FORGET: key] · [SHOW_MEMORY]
Pause: [PAUSE: target | minutes] · [PAUSE_FOREVER: target] · [UNPAUSE: target] · [UNPAUSE_ALL]
Retrieval: [FORWARD: target | count] · [FORWARD_LAST | count] · [CONTEXT: target]
Senders: [WHO_LAST] · [LIST_SENDERS] · [POPULATE_SENDERS]
Mute: [GLOBAL_MUTE: minutes] · [GLOBAL_UNMUTE]
Rules: [RULE_ADD: text] · [RULE_REMOVE: index] · [RULE_LIST] · [RULE_CLEAR]
Personality: [PERSONALITY_SET: text] · [PERSONALITY_CLEAR] · [PERSONALITY_SHOW]

=== ⚠️ CRITICAL RULES ===

1. **If user asks a QUESTION → JUST ANSWER, no tag.**
   - "مگه من سازنده تو نیستم؟" → answer warmly, NO tag
   - "چطوری؟" → answer, NO tag
   - "چه چیزایی بلدی؟" → list capabilities, NO tag

2. **If user asks "آخرین نفر کی پیام داد" / "کی بهت پیام داد" / "last sender":**
   - Look at RECENT SENDERS section above
   - If it has data → answer directly from that data, NO tag
   - If it says "خالی" → output [POPULATE_SENDERS] on its own line, system will fetch and re-ask
   - ⚠️ NEVER make up a name. NEVER say "storebat" unless it's in RECENT SENDERS.
   - ⚠️ The MOST RECENT (index 1) is the answer, not just any name.

3. **DON'T REPEAT ACTIONS** — check RECENT ACTIONS above.
   - Just unpaused X → don't unpause again
   - Just paused X → don't pause again
   - Chat NOT in PAUSED CHATS → already unpaused, don't touch

4. **"مگه من سازنده تو نیستم؟" → این یه سواله، جواب بده، هیچ tag نزن.**

5. **Tag on its own line. One action per message.**

6. **Memory: only when "یادت باشه..." / "به خاطر بسپار..." / "فراموش کن..."**

{vblock}

=== CONVERSATION ===
{chr(10).join(hist)}
You:"""
    else:
        vpref = get_voice_pref(user_id)
        vblock = voice_status_block(vpref)
        prompt = f"""You are {OWNER_NAME}'s AI assistant, answering DMs.
Persian, casual, warm, 1-3 sentences.

=== About {OWNER_NAME} ===
{build_owner_info()}

{build_personality_section()}

=== Rules ===
- Same language
- "{OWNER_NAME}'s AI assistant"
- Never pretend to be them
- End with [IMPORTANT] or [NORMAL]

=== TEACHING ===
Only {OWNER_NAME} can teach. Voice request ≠ teaching.

{vblock}

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
                    log(f"✅ {model_name} (r{round_num}, {int(time.time()-started)}s)")
                    return raw, None
            except Exception as e:
                last_error = e
                es = str(e)
                if "429" in es or "RESOURCE_EXHAUSTED" in es:
                    log(f"⏸️ {model_name}: quota")
                elif "503" in es or "UNAVAILABLE" in es:
                    log(f"⏳ {model_name}: busy")
                elif "404" in es:
                    log(f"❌ {model_name}: not found")
                else:
                    log(f"❌ {model_name}: {es[:80]}")
                continue

        if time.time() - started > max_time:
            log(f"⛔ Gave up")
            return None, last_error

        delay = RETRY_DELAYS[min(round_num - 1, len(RETRY_DELAYS) - 1)]
        if chat_id:
            try:
                await client.action(chat_id, "typing")
            except:
                pass
        await asyncio.sleep(delay)

async def send_alert(text):
    if not NTFY_TOPIC:
        return
    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=text.encode('utf-8'),
            headers={"Title": "Important", "Priority": "urgent", "Tags": "rotating_light,warning"},
            timeout=10
        ))
        log("🔔 Alert")
    except Exception as e:
        log(f"⚠️ ntfy: {str(e)[:80]}")

async def transcribe_voice(voice_path):
    started = time.time()
    rn = 0
    while True:
        rn += 1
        try:
            with open(voice_path, "rb") as f:
                audio = f.read()
            if len(audio) < 100:
                return None
        except:
            return None
        for mn in STT_MODELS:
            try:
                resp = ai.models.generate_content(model=mn, contents=[types.Content(parts=[
                    types.Part.from_bytes(data=audio, mime_type="audio/ogg"),
                    types.Part.from_text(text="این فایل صوتی رو دقیقاً به همون زبونی که صحبت شده، رونویسی کن. فقط متن رو بده.")
                ])])
                t = (resp.text or "").strip()
                if t:
                    log(f"🎤 STT: {mn}")
                    return t
            except:
                continue
        if time.time() - started > 300:
            return None
        await asyncio.sleep(RETRY_DELAYS[min(rn-1, len(RETRY_DELAYS)-1)])

async def handle_voice_input(event):
    try:
        await event.reply("🎤 دارم گوش می‌دم...")
    except:
        pass
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        vp = tmp.name
    try:
        await event.download_media(file=vp)
        return await transcribe_voice(vp)
    finally:
        try:
            os.unlink(vp)
        except:
            pass

def clean_text_for_voice(text):
    pat = re.compile("[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF"
                     "\U0001F1E0-\U0001F1FF\U00002702-\U000027B0\U000024C2-\U0001F251"
                     "\U0001f926-\U0001f937\U00010000-\U0010ffff\u2640-\u2642"
                     "\u2600-\u2B55\u200d\u23cf\u23e9\u231a\ufe0f\u3030]+", flags=re.UNICODE)
    t = pat.sub("", text).replace("*", "").replace("_", "").replace("`", "")
    return re.sub(r"\s+", " ", t).strip()

async def text_to_voice(text, voice=None):
    try:
        voice = voice or runtime_settings.get("voice_name", VOICE_NAME)
        clean = clean_text_for_voice(text)
        if not clean or len(clean) < 2:
            return None
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
            mp3 = tmp.name
        await edge_tts.Communicate(clean, voice).save(mp3)
        ogg = mp3.replace(".mp3", ".ogg")
        res = subprocess.run(["ffmpeg", "-y", "-i", mp3, "-c:a", "libopus", "-b:a", "48k",
                              "-vbr", "on", "-compression_level", "10", "-frame_duration", "60",
                              "-application", "voip", ogg], capture_output=True, timeout=30)
        try:
            os.unlink(mp3)
        except:
            pass
        return ogg if res.returncode == 0 else None
    except Exception as e:
        log(f"⚠️ voice: {str(e)[:80]}")
        return None

async def send_voice_reply(chat_id, reply_to_id, text):
    if not runtime_settings.get("voice_output", True):
        return
    vp = await text_to_voice(text)
    if not vp:
        return
    try:
        await client.send_file(chat_id, vp, voice_note=True, reply_to=reply_to_id)
        log("🎤 Voice sent")
    except Exception as e:
        log(f"⚠️ voice send: {str(e)[:80]}")
    finally:
        try:
            os.unlink(vp)
        except:
            pass


client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)


# ============ REST API ============
CORS = {"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "X-API-Key, Content-Type", "Access-Control-Allow-Methods": "GET, POST, OPTIONS"}

def auth(req):
    return (req.headers.get("X-API-Key") or req.query.get("api_key")) == API_KEY

def jr(data, status=200):
    return web.json_response(data, status=status, headers=CORS)

async def opts(req):
    return web.Response(headers=CORS)

async def api_ping(req):
    return jr({"pong": True, "time": time.time()})

async def api_status(req):
    if not auth(req): return jr({"error": "Unauthorized"}, 401)
    try:
        me = await client.get_me()
        return jr({
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
            "last_senders": last_sender_cache[:10],
            "settings": runtime_settings,
        })
    except Exception as e:
        return jr({"error": str(e)}, 500)

async def api_get_settings(req):
    if not auth(req): return jr({"error": "Unauthorized"}, 401)
    return jr({"settings": runtime_settings})

async def api_update_settings(req):
    if not auth(req): return jr({"error": "Unauthorized"}, 401)
    try: data = await req.json()
    except: return jr({"error": "Invalid"}, 400)
    allowed = {"pause_enabled", "pause_duration", "voice_output", "voice_input", "voice_name", "alert_delay", "max_retry_time"}
    upd = {k: v for k, v in data.items() if k in allowed}
    runtime_settings.update(upd)
    log(f"⚙️ {upd}")
    return jr({"ok": True, "updated": upd, "settings": runtime_settings})

async def api_paused(req):
    if not auth(req): return jr({"error": "Unauthorized"}, 401)
    out = []
    for cid in list(owner_last_seen.keys()):
        rem = pause_remaining(cid)
        if rem > 0:
            name = chat_names_cache.get(cid)
            if not name:
                try:
                    e = await client.get_entity(cid)
                    name = getattr(e, "first_name", None) or getattr(e, "title", None)
                    chat_names_cache[cid] = name
                except: pass
            out.append({"chat_id": cid, "name": name, "remaining_seconds": rem, "custom_duration": custom_pause_durations.get(cid)})
    out.sort(key=lambda x: x["remaining_seconds"])
    return jr({"paused": out})

async def api_pause(req):
    if not auth(req): return jr({"error": "Unauthorized"}, 401)
    try:
        data = await req.json()
        cid = int(data["chat_id"])
        dur = data.get("duration")
    except: return jr({"error": "chat_id required"}, 400)
    owner_last_seen[cid] = time.time()
    if dur: custom_pause_durations[cid] = int(dur)
    if cid not in chat_names_cache:
        try: asyncio.create_task(cache_chat_name(cid))
        except: pass
    return jr({"ok": True})

async def api_resume(req):
    if not auth(req): return jr({"error": "Unauthorized"}, 401)
    try:
        data = await req.json()
        cid = int(data["chat_id"])
    except: return jr({"error": "chat_id required"}, 400)
    owner_last_seen.pop(cid, None)
    custom_pause_durations.pop(cid, None)
    return jr({"ok": True})

async def api_resume_all(req):
    if not auth(req): return jr({"error": "Unauthorized"}, 401)
    c = len(owner_last_seen)
    owner_last_seen.clear()
    custom_pause_durations.clear()
    return jr({"ok": True, "cleared": c})

async def api_logs(req):
    if not auth(req): return jr({"error": "Unauthorized"}, 401)
    return jr({"logs": list(recent_logs)})

async def api_facts(req):
    if not auth(req): return jr({"error": "Unauthorized"}, 401)
    return jr({"facts": memory.get("learned_facts", {}), "notes": memory.get("owner_notes", []),
               "rules": memory.get("custom_rules", []), "personality": memory.get("personality_override", "")})

async def api_fact_add(req):
    if not auth(req): return jr({"error": "Unauthorized"}, 401)
    try:
        d = await req.json()
        k, v = d.get("key", "").strip(), d.get("value", "").strip()
        if not k or not v: return jr({"error": "required"}, 400)
        memory.setdefault("learned_facts", {})[k] = v
        save_memory_async()
        return jr({"ok": True})
    except Exception as e: return jr({"error": str(e)}, 500)

async def api_fact_delete(req):
    if not auth(req): return jr({"error": "Unauthorized"}, 401)
    try:
        d = await req.json()
        k = d.get("key", "").strip()
        if k in memory.get("learned_facts", {}):
            del memory["learned_facts"][k]
            save_memory_async()
            return jr({"ok": True})
        return jr({"error": "not found"}, 404)
    except Exception as e: return jr({"error": str(e)}, 500)

def create_app():
    app = web.Application()
    app.router.add_route("OPTIONS", "/{tail:.*}", opts)
    app.router.add_get("/api/ping", api_ping)
    app.router.add_get("/api/status", api_status)
    app.router.add_get("/api/settings", api_get_settings)
    app.router.add_post("/api/settings", api_update_settings)
    app.router.add_get("/api/paused", api_paused)
    app.router.add_post("/api/pause", api_pause)
    app.router.add_post("/api/resume", api_resume)
    app.router.add_post("/api/resume_all", api_resume_all)
    app.router.add_get("/api/logs", api_logs)
    app.router.add_get("/api/facts", api_facts)
    app.router.add_post("/api/facts/add", api_fact_add)
    app.router.add_post("/api/facts/delete", api_fact_delete)
    return app


# ============ Owner activity ============
@client.on(events.NewMessage(outgoing=True))
async def on_owner_activity(event):
    if not event.is_private: return
    try: me = await client.get_me()
    except: return
    if event.chat_id == me.id: return
    if getattr(event.message, 'forward', None): return
    mark_owner_active(event.chat_id)
    rem = pause_remaining(event.chat_id)
    log(f"🛑 Paused {event.chat_id} {rem//60}m")


# ============ Owner handler ============
@client.on(events.NewMessage(outgoing=True))
async def on_owner_message(event):
    global global_mute_until, last_bot_send_time

    try: me = await client.get_me()
    except: return
    if event.chat_id != me.id or event.sender_id != me.id: return
    if time.time() - last_bot_send_time < 1.5: return
    if getattr(event.message, 'forward', None): return

    text = event.message.text or ""

    if not text.strip() and event.message.voice:
        if not runtime_settings.get("voice_input", True):
            await event.reply("🚫 ویس غیرفعال")
            return
        text = await handle_voice_input(event) or ""
        if not text:
            await event.reply("نتونستم ویس رو بفهمم 😅")
            return
        await event.reply(f"🎤 «{text}»")

    if not text.strip(): return
    log(f"🔍 Owner: {text[:50]!r}")

    prompt = build_prompt("owner", text, is_owner=True)
    raw, _ = await ask_gemini(prompt, chat_id=event.chat_id)
    if raw is None:
        await event.reply("الان نشد، دوباره بفرست 🙏")
        return

    # ===== POPULATE_SENDERS =====
    if "[POPULATE_SENDERS]" in raw:
        await event.reply("⏳ دارم چت‌های اخیرت رو از تلگرام می‌خونم...")
        count = await populate_senders_from_telegram()
        if count == 0:
            await event.reply("❓ چت خصوصی با پیام دریافتی پیدا نکردم")
        else:
            # Save to GitHub so it survives restarts
            save_senders_if_dirty()
            # Re-ask AI with new data
            prompt2 = build_prompt("owner", text + " [داده لود شد]", is_owner=True)
            raw2, _ = await ask_gemini(prompt2, chat_id=event.chat_id)
            if raw2:
                reply = re.sub(r"\[(POPULATE_SENDERS|WHO_LAST|LIST_SENDERS|IMPORTANT|NORMAL|ENABLE_VOICE|DISABLE_VOICE)\]", "", raw2).strip()
                if reply:
                    last_bot_send_time = time.time()
                    await event.reply(reply)
                    return
            # Fallback: list directly
            if last_sender_cache:
                last = last_sender_cache[0]
                ago = int(time.time() - last["timestamp"])
                last_bot_send_time = time.time()
                await event.reply(f"آخرین نفر: **{last['name']}** (`{last['chat_id']}`) — {ago//60}د {ago%60}ث پیش")
        return

    # ===== UNPAUSE_ALL =====
    if "[UNPAUSE_ALL]" in raw:
        c = len(owner_last_seen)
        owner_last_seen.clear()
        custom_pause_durations.clear()
        track_action("UNPAUSE_ALL", c)
        log(f"▶️ Unpaused all ({c})")
        r = re.sub(r"\[(UNPAUSE_ALL|IMPORTANT|NORMAL|ENABLE_VOICE|DISABLE_VOICE)\]", "", raw).strip()
        if not r: r = f"✅ همه {c} چت آزاد شدن"
        last_bot_send_time = time.time()
        await event.reply(r)
        return

    # ===== UNPAUSE =====
    m = re.search(r"\[UNPAUSE:\s*([^\]]+)\]", raw)
    if m:
        target = m.group(1).strip()
        cid = await resolve_chat_id(target)
        if cid is None:
            await event.reply(f"❓ «{target}» رو نمی‌شناسم")
            return
        name = chat_names_cache.get(cid, str(cid))
        if cid in owner_last_seen:
            del owner_last_seen[cid]
            custom_pause_durations.pop(cid, None)
            track_action("UNPAUSE", cid, name)
            r = re.sub(r"\[(UNPAUSE:[^\]]+|IMPORTANT|NORMAL|ENABLE_VOICE|DISABLE_VOICE)\]", "", raw).strip()
            if not r: r = f"✅ «{name}» آزاد شد"
        else:
            r = f"ℹ️ «{name}» از قبل آزاده"
        last_bot_send_time = time.time()
        await event.reply(r)
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
            r = re.sub(r"\[(PAUSE_FOREVER:[^\]]+|IMPORTANT|NORMAL|ENABLE_VOICE|DISABLE_VOICE)\]", "", raw).strip()
            if not r: r = f"🛑 «{name}» دائمی pause شد"
            last_bot_send_time = time.time()
            await event.reply(r)
        else:
            await event.reply(f"❓ «{target}» رو پیدا نکردم")
        return

    # ===== PAUSE =====
    m = re.search(r"\[PAUSE:\s*([^|\]]+)(?:\|\s*(\d+)\s*)?\]", raw)
    if m:
        target = m.group(1).strip()
        mins = int(m.group(2)) if m.group(2) else (runtime_settings["pause_duration"] // 60)
        cid = await resolve_chat_id(target)
        if cid:
            owner_last_seen[cid] = time.time()
            custom_pause_durations[cid] = mins * 60
            await cache_chat_name(cid)
            name = chat_names_cache.get(cid, str(cid))
            track_action("PAUSE", cid, f"{name} {mins}m")
            r = re.sub(r"\[(PAUSE:[^\]]+|IMPORTANT|NORMAL|ENABLE_VOICE|DISABLE_VOICE)\]", "", raw).strip()
            if not r: r = f"🛑 «{name}» {mins}د pause شد"
            last_bot_send_time = time.time()
            await event.reply(r)
        else:
            await event.reply(f"❓ «{target}» رو پیدا نکردم")
        return

    # ===== FORWARD_LAST =====
    m = re.search(r"\[FORWARD_LAST(?:\s*\|\s*(\d+))?\]", raw)
    if m:
        count = min(int(m.group(1)) if m.group(1) else 1, 20)
        if not last_sender_cache:
            await populate_senders_from_telegram()
        if not last_sender_cache:
            await event.reply("❓ پیامی نیومده")
            return
        last = last_sender_cache[0]
        fwd, err = await forward_last_messages(last["chat_id"], count)
        if err:
            await event.reply(f"❌ {err}")
        else:
            track_action("FORWARD_LAST", last["chat_id"], last["name"])
            r = re.sub(r"\[(FORWARD_LAST[^\]]*|IMPORTANT|NORMAL|ENABLE_VOICE|DISABLE_VOICE)\]", "", raw).strip()
            if not r: r = f"📨 {fwd} پیام از «{last['name']}»"
            last_bot_send_time = time.time()
            await event.reply(r)
        return

    # ===== FORWARD =====
    m = re.search(r"\[FORWARD:\s*([^|\]]+)(?:\|\s*(\d+)\s*)?\]", raw)
    if m:
        target = m.group(1).strip()
        count = min(int(m.group(2)) if m.group(2) else 1, 20)
        cid = await resolve_chat_id(target)
        if cid:
            fwd, err = await forward_last_messages(cid, count)
            name = chat_names_cache.get(cid, str(cid))
            if err:
                await event.reply(f"❌ {err}")
            else:
                track_action("FORWARD", cid, f"{name} {fwd}")
                r = re.sub(r"\[(FORWARD:[^\]]+|IMPORTANT|NORMAL|ENABLE_VOICE|DISABLE_VOICE)\]", "", raw).strip()
                if not r: r = f"📨 {fwd} پیام از «{name}»"
                last_bot_send_time = time.time()
                await event.reply(r)
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
            r = re.sub(r"\[(CONTEXT:[^\]]+|IMPORTANT|NORMAL|ENABLE_VOICE|DISABLE_VOICE)\]", "", raw).strip()
            msg = f"👁️ {name}:\n\n{ctx}"
            if r: msg = r + "\n\n" + msg
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
            await event.reply("❓ پیامی پیدا نکردم")
            return
        lines = []
        if "[WHO_LAST]" in raw:
            last = last_sender_cache[0]
            ago = int(time.time() - last["timestamp"])
            lines.append(f"آخرین نفر: **{last['name']}** (`{last['chat_id']}`) — {ago//60}د {ago%60}ث پیش")
        if "[LIST_SENDERS]" in raw:
            lines.append("\n**لیست:**")
            for i, it in enumerate(last_sender_cache[:10], 1):
                ago = int(time.time() - it["timestamp"])
                lines.append(f"{i}. {it['name']} (`{it['chat_id']}`) — {ago//60}د پیش")
        r = re.sub(r"\[(WHO_LAST|LIST_SENDERS|IMPORTANT|NORMAL|ENABLE_VOICE|DISABLE_VOICE)\]", "", raw).strip()
        msg = "\n".join(lines)
        if r: msg = r + "\n\n" + msg
        last_bot_send_time = time.time()
        await event.reply(msg)
        return

    # ===== GLOBAL_MUTE =====
    m = re.search(r"\[GLOBAL_MUTE:\s*(\d+)\]", raw)
    if m:
        mins = int(m.group(1))
        global_mute_until = time.time() + mins * 60
        track_action("GLOBAL_MUTE", f"{mins}m")
        r = re.sub(r"\[(GLOBAL_MUTE:[^\]]+|IMPORTANT|NORMAL|ENABLE_VOICE|DISABLE_VOICE)\]", "", raw).strip()
        if not r: r = f"🔇 {mins}د ساکت"
        last_bot_send_time = time.time()
        await event.reply(r)
        return

    # ===== GLOBAL_UNMUTE =====
    if "[GLOBAL_UNMUTE]" in raw:
        global_mute_until = 0
        r = re.sub(r"\[(GLOBAL_UNMUTE|IMPORTANT|NORMAL|ENABLE_VOICE|DISABLE_VOICE)\]", "", raw).strip()
        if not r: r = "🔊 آنمیوت شد"
        last_bot_send_time = time.time()
        await event.reply(r)
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
        r = re.sub(r"\[(RULE_ADD:[^\]]+|IMPORTANT|NORMAL|ENABLE_VOICE|DISABLE_VOICE)\]", "", raw).strip()
        if not r: r = f"✅ قانون #{len(memory['custom_rules'])}\n📏 {rule}"
        last_bot_send_time = time.time()
        await event.reply(r)
        return

    # ===== RULE_REMOVE =====
    m = re.search(r"\[RULE_REMOVE:\s*(\d+)\]", raw)
    if m:
        idx = int(m.group(1)) - 1
        rules = memory.get("custom_rules", [])
        if 0 <= idx < len(rules):
            removed = rules.pop(idx)
            save_memory_async()
            r = re.sub(r"\[(RULE_REMOVE:\d+|IMPORTANT|NORMAL|ENABLE_VOICE|DISABLE_VOICE)\]", "", raw).strip()
            if not r: r = f"🗑️ حذف: {removed}"
            last_bot_send_time = time.time()
            await event.reply(r)
        else:
            await event.reply(f"❓ #{idx+1} نیست")
        return

    # ===== RULE_LIST =====
    if "[RULE_LIST]" in raw:
        rules = memory.get("custom_rules", [])
        if not rules:
            await event.reply("📏 قانونی نیست")
            return
        lines = ["📏 **قوانین:**\n"] + [f"{i}. {r}" for i, r in enumerate(rules, 1)]
        r = re.sub(r"\[(RULE_LIST|IMPORTANT|NORMAL|ENABLE_VOICE|DISABLE_VOICE)\]", "", raw).strip()
        msg = "\n".join(lines)
        if r: msg = r + "\n\n" + msg
        last_bot_send_time = time.time()
        await event.reply(msg)
        return

    # ===== RULE_CLEAR =====
    if "[RULE_CLEAR]" in raw:
        c = len(memory.get("custom_rules", []))
        memory["custom_rules"] = []
        save_memory_async()
        r = re.sub(r"\[(RULE_CLEAR|IMPORTANT|NORMAL|ENABLE_VOICE|DISABLE_VOICE)\]", "", raw).strip()
        if not r: r = f"🧹 {c} قانون پاک شد"
        last_bot_send_time = time.time()
        await event.reply(r)
        return

    # ===== PERSONALITY_SET =====
    m = re.search(r"\[PERSONALITY_SET:\s*([^\]]+)\]", raw)
    if m:
        p = m.group(1).strip()
        memory["personality_override"] = p
        save_memory_async()
        track_action("PERSONALITY_SET", p[:40])
        r = re.sub(r"\[(PERSONALITY_SET:[^\]]+|IMPORTANT|NORMAL|ENABLE_VOICE|DISABLE_VOICE)\]", "", raw).strip()
        if not r: r = f"🎭 شخصیت:\n{p}"
        last_bot_send_time = time.time()
        await event.reply(r)
        return

    # ===== PERSONALITY_CLEAR =====
    if "[PERSONALITY_CLEAR]" in raw:
        memory["personality_override"] = ""
        save_memory_async()
        r = re.sub(r"\[(PERSONALITY_CLEAR|IMPORTANT|NORMAL|ENABLE_VOICE|DISABLE_VOICE)\]", "", raw).strip()
        if not r: r = "🎭 پیش‌فرض"
        last_bot_send_time = time.time()
        await event.reply(r)
        return

    # ===== PERSONALITY_SHOW =====
    if "[PERSONALITY_SHOW]" in raw:
        p = memory.get("personality_override", "")
        if not p:
            await event.reply("🎭 override نیست")
            return
        await event.reply(f"🎭 {p}")
        return

    # ===== Memory tags =====
    enable_voice = "[ENABLE_VOICE]" in raw
    disable_voice = "[DISABLE_VOICE]" in raw

    if "[LEARN:" in raw:
        try:
            inner = raw.split("[LEARN:", 1)[1].split("]", 1)[0].strip()
            if "|" in inner:
                k, v = inner.split("|", 1)
                memory.setdefault("learned_facts", {})[k.strip()] = v.strip()
                save_memory_async()
                last_bot_send_time = time.time()
                await event.reply(f"✅ گرفتم: {k.strip()} → {v.strip()}")
                return
        except: pass

    if "[LEARN_RAW:" in raw:
        try:
            inner = raw.split("[LEARN_RAW:", 1)[1].split("]", 1)[0].strip()
            fid = f"fact_{len(memory.get('learned_facts', {})) + 1}"
            memory.setdefault("learned_facts", {})[fid] = inner
            save_memory_async()
            last_bot_send_time = time.time()
            await event.reply(f"✅ گرفتم: {inner}")
            return
        except: pass

    if "[FORGET:" in raw:
        try:
            k = raw.split("[FORGET:", 1)[1].split("]", 1)[0].strip()
            facts = memory.get("learned_facts", {})
            if k in facts:
                del facts[k]
                save_memory_async()
                last_bot_send_time = time.time()
                await event.reply(f"🗑️ فراموش: {k}")
            else:
                last_bot_send_time = time.time()
                await event.reply(f"❓ نبود: {k}")
            return
        except: pass

    if "[SHOW_MEMORY]" in raw:
        facts = memory.get("learned_facts", {})
        notes = memory.get("owner_notes", [])
        r = "🧠 **حافظه:**\n\n"
        if facts:
            for k, v in facts.items():
                r += f"• {k}: {v}\n"
        else:
            r += "خالی\n"
        if notes:
            r += "\n**یادداشت:**\n"
            for n in notes:
                r += f"• {n}\n"
        last_bot_send_time = time.time()
        await event.reply(r)
        return

    if enable_voice: set_voice_pref("owner", True)
    elif disable_voice: set_voice_pref("owner", False)

    reply = re.sub(r"\[(IMPORTANT|NORMAL|ENABLE_VOICE|DISABLE_VOICE)\]", "", raw).strip()
    if not reply: return

    histories.setdefault("owner", []).append(f"You: {reply}")
    last_bot_send_time = time.time()
    await event.reply(reply)

    if get_voice_pref("owner") is True and not disable_voice:
        await send_voice_reply(event.chat_id, event.message.id, reply)


# ============ DM handler ============
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
        log(f"🔇 Muted — skip {sender.id}")
        return

    if is_owner_paused(event.chat_id):
        log(f"🛑 Paused {event.chat_id}")
        return

    text = event.message.text or ""

    if not text.strip() and event.message.voice:
        if not runtime_settings.get("voice_input", True):
            return
        text = await handle_voice_input(event) or ""
        if not text:
            await event.reply("نتونستم ویس رو بفهمم 😅")
            return

    if not text.strip():
        return

    user_key = str(sender.id)
    log(f"🔍 User {user_key}: {text[:50]!r}")

    prompt = build_prompt(sender.id, text, is_owner=False)
    raw, _ = await ask_gemini(prompt, chat_id=event.chat_id)
    if raw is None:
        return

    important = "[IMPORTANT]" in raw
    enable_voice = "[ENABLE_VOICE]" in raw
    disable_voice = "[DISABLE_VOICE]" in raw

    if enable_voice: set_voice_pref(user_key, True)
    elif disable_voice: set_voice_pref(user_key, False)

    reply = re.sub(r"\[(IMPORTANT|NORMAL|ENABLE_VOICE|DISABLE_VOICE)\]", "", raw).strip()
    if not reply: return

    histories.setdefault(user_key, []).append(f"You: {reply}")
    await event.reply(reply)

    if get_voice_pref(user_key) is True and not disable_voice:
        await send_voice_reply(event.chat_id, event.message.id, reply)

    if important:
        uname = f"@{sender.username}" if sender.username else "—"
        alert = (f"⚠️ پیام مهم\n\n👤 {sender_name} ({uname})\n🆔 `{sender.id}`\n\n"
                 f"📩 {text}\n\n🤖 {reply}")
        await client.send_message("me", alert, silent=True)
        log(f"📩 Alert. {runtime_settings['alert_delay']}s...")
        await asyncio.sleep(runtime_settings["alert_delay"])
        await send_alert(alert)


async def start_api():
    app = create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log(f"🚀 API :{PORT}")


async def main():
    global memory
    await client.start()
    me = await client.get_me()
    log(f"✅ {me.first_name} (id={me.id})")

    await start_api()

    # Persist last_senders on startup if loaded from Telegram earlier
    if not last_sender_cache:
        log("📥 No last_senders in memory, will populate on demand")

    log("🚀 Running...")
    log(f"📚 {len(memory.get('learned_facts', {}))} facts | 📏 {len(memory.get('custom_rules', []))} rules")
    log(f"📥 {len(last_sender_cache)} senders cached")
    log(f"⏸️ Pause: {'on' if runtime_settings['pause_enabled'] else 'off'}")

    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
