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
    "pause_enabled": True, "pause_duration": PAUSE_DURATION,
    "voice_output": True, "voice_input": True,
    "voice_name": VOICE_NAME, "alert_delay": ALERT_DELAY,
    "max_retry_time": 300,
}

# ============ State ============
owner_last_seen = {}
chat_names_cache = {}
custom_pause_durations = {}
global_mute_until = 0
last_bot_send_time = 0
dm_senders = []          # فقط DM بدون bot
all_senders = []         # همه چت‌ها (گروه، کانال، bot)
bot_last_replies = {}    # {chat_id: {"text": ..., "ts": ...}} - پاسخ‌های ربات
recent_actions = deque(maxlen=15)
recent_logs = deque(maxlen=100)
started_at = time.time()
dm_dirty = False
all_dirty = False

def log(message):
    ts = time.strftime("%H:%M:%S")
    recent_logs.append({"time": ts, "text": message})
    print(f"{ts} {message}")

def track_action(action_type, target, extra=""):
    recent_actions.append({"time": time.strftime("%H:%M:%S"), "type": action_type, "target": target, "extra": extra})

ai = genai.Client(api_key=GEMINI_API_KEY)

MODELS = ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash",
          "gemini-3-flash", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]
STT_MODELS = ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash",
              "gemini-3.5-flash", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]
RETRY_DELAYS = [0.5, 1, 2, 3, 5, 8, 13, 20, 30, 30, 30]

OWNER_INFO_BASE = """
Name: Poryia Rahmani
Nickname: Poryia
Age group: Teenager / student
Location: Iran
Status: Student
Interests: Bodybuilding, computers, AI, coding
Current projects: AI assistant, VPN app
Personality: Young, ambitious, hardworking
Notes: Student, busy during day
"""

# ============ Helper functions ============
async def cache_chat_name(chat_id):
    try:
        entity = await client.get_entity(chat_id)
        name = getattr(entity, "first_name", None) or getattr(entity, "title", None) or str(chat_id)
        chat_names_cache[chat_id] = name
    except:
        chat_names_cache[chat_id] = str(chat_id)

def get_pause_duration(chat_id):
    return custom_pause_durations.get(chat_id, runtime_settings["pause_duration"])

def mark_owner_active(chat_id):
    owner_last_seen[chat_id] = time.time()
    if chat_id not in chat_names_cache:
        try: asyncio.create_task(cache_chat_name(chat_id))
        except: pass

def is_owner_paused(chat_id):
    if not runtime_settings["pause_enabled"]: return False
    last = owner_last_seen.get(chat_id, 0)
    if last == 0: return False
    return (time.time() - last) < get_pause_duration(chat_id)

def pause_remaining(chat_id):
    last = owner_last_seen.get(chat_id, 0)
    if last == 0: return 0
    return max(0, int(get_pause_duration(chat_id) - (time.time() - last)))

def is_globally_muted():
    return time.time() < global_mute_until

def track_sender(chat_id, name, is_bot=False, is_group=False):
    global dm_senders, all_senders, dm_dirty, all_dirty
    entry = {"chat_id": chat_id, "name": name, "timestamp": time.time(),
             "is_bot": is_bot, "is_group": is_group}
    all_senders = [x for x in all_senders if x["chat_id"] != chat_id]
    all_senders.insert(0, entry)
    all_senders = all_senders[:50]
    all_dirty = True
    if not is_bot and not is_group:
        dm_senders = [x for x in dm_senders if x["chat_id"] != chat_id]
        dm_senders.insert(0, entry)
        dm_senders = dm_senders[:30]
        dm_dirty = True

async def get_last_incoming_text(chat_id):
    """متن آخرین پیام دریافتی"""
    try:
        messages = await client.get_messages(chat_id, limit=10)
        for m in messages:
            if not m.out:
                return m.text or "[رسانه/ویس/عکس]"
        return None
    except:
        return None

async def get_last_outgoing_text(chat_id):
    """متن آخرین پاسخ ربات (یا خودت)"""
    try:
        messages = await client.get_messages(chat_id, limit=10)
        for m in messages:
            if m.out:
                return m.text or "[رسانه/ویس/عکس]"
        # fallback به cache
        if chat_id in bot_last_replies:
            return bot_last_replies[chat_id]["text"]
        return None
    except:
        return None

async def get_last_messages_text(chat_id, count=5):
    """متن N پیام آخر"""
    try:
        messages = await client.get_messages(chat_id, limit=count)
        if not messages: return "هیچ پیامی نیست"
        lines = []
        for m in reversed(messages):
            who = "🤖 ربات" if m.out else "👤 کاربر"
            text = m.text or "[رسانه]"
            if len(text) > 200: text = text[:200] + "..."
            lines.append(f"{who}: {text}")
        return "\n".join(lines)
    except Exception as e:
        return f"خطا: {str(e)[:100]}"

async def populate_senders_from_telegram(limit=15, include_all=False):
    global dm_senders, all_senders, dm_dirty, all_dirty
    try:
        results = []
        async for dialog in client.iter_dialogs(limit=100):
            if dialog.id == OWNER_ID: continue
            entity = dialog.entity
            if entity is None: continue
            is_bot = getattr(entity, "bot", False)
            is_group = not dialog.is_user
            if not include_all and (is_bot or is_group): continue
            try:
                msgs = await client.get_messages(dialog.id, limit=10)
            except: continue
            last_incoming = None
            for m in msgs:
                if not m.out:
                    last_incoming = m; break
            if not last_incoming: continue
            name = (getattr(entity, "first_name", None) or getattr(entity, "title", None)
                    or getattr(entity, "username", None) or str(dialog.id))
            chat_names_cache[dialog.id] = name
            ts = last_incoming.date.timestamp() if last_incoming.date else time.time()
            results.append({"chat_id": dialog.id, "name": name, "timestamp": ts,
                            "is_bot": is_bot, "is_group": is_group})
        results.sort(key=lambda x: x["timestamp"], reverse=True)
        if include_all:
            all_senders = results[:limit]; all_dirty = True
            log(f"📥 ALL: {len(all_senders)}")
        else:
            dm_senders = results[:limit]; dm_dirty = True
            log(f"📥 DM: {len(dm_senders)}")
        return len(results[:limit])
    except Exception as e:
        log(f"⚠️ Populate: {str(e)[:100]}")
        return 0

async def resolve_chat_id(target_str):
    target_str = str(target_str).strip()
    if target_str.lower() in ["last", "اخرین", "آخرین", "اخری", "آخری"]:
        if dm_senders: return dm_senders[0]["chat_id"]
        if await populate_senders_from_telegram(include_all=False) > 0:
            return dm_senders[0]["chat_id"]
        return None
    try: return int(target_str)
    except: pass
    tl = target_str.lower()
    for cid, name in chat_names_cache.items():
        if name and (tl in str(name).lower() or str(name).lower() in tl):
            return cid
    for item in dm_senders + all_senders:
        if tl in item["name"].lower() or item["name"].lower() in tl:
            return item["chat_id"]
    try:
        async for dialog in client.iter_dialogs(limit=60):
            entity = dialog.entity
            if entity is None: continue
            en = getattr(entity, "first_name", "") or getattr(entity, "title", "") or ""
            if en and (tl in en.lower() or en.lower() in tl):
                chat_names_cache[dialog.id] = en
                return dialog.id
    except: pass
    return None

def build_paused_section():
    if not owner_last_seen: return "=== PAUSED ===\nهیچ"
    lines = ["=== PAUSED ==="]
    found = False
    for cid in list(owner_last_seen.keys()):
        rem = pause_remaining(cid)
        if rem > 0:
            found = True
            name = chat_names_cache.get(cid, "?")
            lines.append(f"- {name} (id:{cid}) باقی: {rem//60}د {rem%60}ث")
    if not found: return "=== PAUSED ===\nهیچ"
    return "\n".join(lines)

def build_dm_senders_section():
    if not dm_senders:
        return "=== DM SENDERS ===\n❌ خالی. اگه پرسید، [POPULATE_DM_SENDERS]"
    lines = ["=== DM SENDERS (فقط خصوصی — برای سوال «بهت پیام داد») ==="]
    for i, it in enumerate(dm_senders[:10], 1):
        ago = int(time.time() - it["timestamp"])
        lines.append(f"{i}. {it['name']} (id:{it['chat_id']}) — {ago//60}د {ago%60}ث")
    return "\n".join(lines)

def build_all_senders_section():
    if not all_senders:
        return "=== ALL CHATS ===\n❌ خالی. اگه پرسید، [POPULATE_ALL_SENDERS]"
    lines = ["=== ALL CHATS (همه — برای سوال «کلی») ==="]
    for i, it in enumerate(all_senders[:10], 1):
        ago = int(time.time() - it["timestamp"])
        kind = "bot" if it.get("is_bot") else ("group" if it.get("is_group") else "DM")
        lines.append(f"{i}. {it['name']} (id:{it['chat_id']}, {kind}) — {ago//60}د {ago%60}ث")
    return "\n".join(lines)

def build_recent_actions_section():
    if not recent_actions: return ""
    lines = ["=== RECENT ACTIONS ==="]
    for a in list(recent_actions)[-6:]:
        e = f" {a['extra']}" if a['extra'] else ""
        lines.append(f"- [{a['time']}] {a['type']} '{a['target']}'{e}")
    return "\n".join(lines)

def build_custom_rules_section():
    rules = memory.get("custom_rules", [])
    return "=== RULES ===\n" + "\n".join(f"- {r}" for r in rules) if rules else ""

def build_personality_section():
    p = memory.get("personality_override", "")
    return f"=== PERSONALITY ===\n{p}" if p else ""

def build_global_mute_section():
    if is_globally_muted():
        r = int(global_mute_until - time.time())
        return f"=== MUTED ===\n{r//60}د {r%60}ث"
    return ""

# ============ Forward ============
async def forward_last_messages(chat_id, count=1):
    try:
        messages = await client.get_messages(chat_id, limit=count * 3)
        if not messages: return 0, "هیچ پیامی نیست"
        user_msgs = [m for m in messages if not m.out][:count]
        if not user_msgs: user_msgs = messages[:count]
        await client.forward_messages("me", user_msgs)
        return len(user_msgs), None
    except Exception as e:
        return 0, str(e)

# ============ GitHub Memory ============
def github_headers():
    return {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}

def fetch_memory():
    empty = {"learned_facts": {}, "owner_notes": [], "voice_preferences": {},
             "custom_rules": [], "personality_override": "",
             "dm_senders": [], "all_senders": []}
    if not GITHUB_TOKEN: return empty, None
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{MEMORY_PATH}"
    try:
        r = requests.get(url, headers=github_headers(), params={"ref": GITHUB_BRANCH}, timeout=10)
        if r.status_code == 200:
            data = r.json()
            content = base64.b64decode(data["content"]).decode("utf-8")
            mem = json.loads(content)
            for k, v in empty.items(): mem.setdefault(k, v)
            mem.pop("conversations", None); mem.pop("last_senders", None)
            return mem, data["sha"]
        elif r.status_code == 404:
            return empty, None
    except Exception as e:
        log(f"⚠️ Fetch: {str(e)[:100]}")
    return empty, None

def push_memory(mem):
    if not GITHUB_TOKEN: return
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{MEMORY_PATH}"
    _, sha = fetch_memory()
    to_save = {
        "learned_facts": mem.get("learned_facts", {}),
        "owner_notes": mem.get("owner_notes", []),
        "voice_preferences": mem.get("voice_preferences", {}),
        "custom_rules": mem.get("custom_rules", []),
        "personality_override": mem.get("personality_override", ""),
        "dm_senders": mem.get("dm_senders", [])[:30],
        "all_senders": mem.get("all_senders", [])[:30],
    }
    content = base64.b64encode(json.dumps(to_save, ensure_ascii=False, indent=2).encode("utf-8")).decode("utf-8")
    payload = {"message": "Update AI memory", "content": content, "branch": GITHUB_BRANCH}
    if sha: payload["sha"] = sha
    try:
        r = requests.put(url, headers=github_headers(), json=payload, timeout=15)
        if r.status_code in (200, 201): log("💾 Saved")
    except Exception as e:
        log(f"⚠️ Push: {str(e)[:100]}")

def save_memory_async():
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, push_memory, memory)

def save_senders_if_dirty():
    global dm_dirty, all_dirty
    if dm_dirty or all_dirty:
        memory["dm_senders"] = list(dm_senders)[:30]
        memory["all_senders"] = list(all_senders)[:30]
        save_memory_async()
        dm_dirty = False
        all_dirty = False

memory, _sha = fetch_memory()

if memory.get("dm_senders"):
    dm_senders = list(memory["dm_senders"])
    log(f"📥 Loaded {len(dm_senders)} DM")
if memory.get("all_senders"):
    all_senders = list(memory["all_senders"])
    log(f"📥 Loaded {len(all_senders)} ALL")

histories = {}
MAX_HISTORY = 12

def get_voice_pref(key):
    return memory.get("voice_preferences", {}).get(str(key))

def set_voice_pref(key, value):
    key = str(key)
    if memory.get("voice_preferences", {}).get(key) == value: return
    memory.setdefault("voice_preferences", {})[key] = value
    save_memory_async()

def build_owner_info():
    info = OWNER_INFO_BASE
    facts = memory.get("learned_facts", {})
    if facts:
        info += "\n\n=== Facts ===\n"
        for k, v in facts.items(): info += f"- {k}: {v}\n"
    return info

def voice_status_block(pref):
    if pref is None: return "=== VOICE ===\nOffer ONCE"
    if pref is True: return "=== VOICE ===\nON"
    return "=== VOICE ===\nOFF"

def build_prompt(user_id, new_message, is_owner=False):
    hist = histories.setdefault(str(user_id), [])
    hist.append(f"User: {new_message}")
    del hist[:-MAX_HISTORY]

    if is_owner:
        vpref = get_voice_pref("owner")
        vblock = voice_status_block(vpref)
        prompt = f"""You are {OWNER_NAME}'s personal AI assistant. Persian, warm, casual.

=== About {OWNER_NAME} ===
{build_owner_info()}

{build_paused_section()}

{build_dm_senders_section()}

{build_all_senders_section()}

{build_global_mute_section()}

{build_recent_actions_section()}

{build_custom_rules_section()}

{build_personality_section()}

=== TAGS ===

[LEARN: k | v] · [LEARN_RAW: t] · [FORGET: k] · [SHOW_MEMORY]
[PAUSE: target | min] · [PAUSE_FOREVER: target] · [UNPAUSE: target] · [UNPAUSE_ALL]
[FORWARD: target | n] · [FORWARD_LAST | n]
[CONTEXT: target]
[POPULATE_DM_SENDERS] · [WHO_LAST_DM]
[POPULATE_ALL_SENDERS] · [WHO_LAST_ALL]
[SHOW_LAST_INCOMING: target] — نمایش متن آخرین پیام دریافتی
[SHOW_LAST_OUTGOING: target] — نمایش متن آخرین پاسخ ربات
[SHOW_MSG: target | n] — نمایش n پیام آخر
[GLOBAL_MUTE: min] · [GLOBAL_UNMUTE]
[RULE_ADD: t] · [RULE_REMOVE: i] · [RULE_LIST] · [RULE_CLEAR]
[PERSONALITY_SET: t] · [PERSONALITY_CLEAR] · [PERSONALITY_SHOW]

=== ⚠️⚠️⚠️ CRITICAL DECISION RULES ⚠️⚠️⚠️ ===

**1. DISTINGUISH DM vs ALL:**
- "آخرین نفری که **بهت** پیام داد" / "آخرین DM" / "کی بهت پیام داد" → **DM only** (dm_senders section)
- "**کلی**" / "توی **همه** چت‌هام" / "همه جا" / "آخرین پیام **دریافت شده**" → **ALL chats** (all_senders section)

⚠️ When in doubt → ASK: "منظورت DM هست یا کلی (گروه/کانال/bot هم)؟"

**2. If user asks "چی بوده" / "چی گفت" / "محتواش چیه":**
→ You MUST output [SHOW_LAST_INCOMING: target] to fetch actual text
→ DO NOT just say "می‌خوای بیارم؟" — JUST SHOW IT

**3. If user asks "تو چی جواب دادی" / "جواب تو چی بود":**
→ Output [SHOW_LAST_OUTGOING: target]
→ NEVER say "من جواب ندادم" without checking first

**4. If user asks "بیشتر" / "چند تا" / "نشونم بده":**
→ Use [SHOW_MSG: target | 5] to show multiple messages

**5. NEVER promise without action:**
- ❌ "الان می‌فرستم" (بدون tag)
- ✅ "باشه 📨\n[FORWARD_LAST | 1]"

**6. When user says "اره" / "باشه" / "بله" after you OFFERED something:**
→ DO IT NOW with the tag. Don't ask again.

**7. Only save memory when "یادت باشه..." / "فراموش کن..."**

**8. If user asks a general QUESTION → JUST ANSWER, no tag.**
Examples:
- "چطوری؟" → answer, NO tag
- "مگه من سازنده تو نیستم؟" → answer, NO tag
- "چه چیزایی بلدی؟" → list, NO tag

**9. DON'T repeat actions from RECENT ACTIONS.**

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
                es = str(e)
                if "429" in es or "RESOURCE_EXHAUSTED" in es: log(f"⏸️ {model_name}: quota")
                elif "503" in es: log(f"⏳ {model_name}: busy")
                elif "404" in es: log(f"❌ {model_name}: not found")
                else: log(f"❌ {model_name}: {es[:80]}")
                continue
        if time.time() - started > runtime_settings.get("max_retry_time", 300):
            return None, None
        delay = RETRY_DELAYS[min(round_num - 1, len(RETRY_DELAYS) - 1)]
        if chat_id:
            try: await client.action(chat_id, "typing")
            except: pass
        await asyncio.sleep(delay)

async def send_alert(text):
    if not NTFY_TOPIC: return
    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=text.encode('utf-8'),
            headers={"Title": "Important", "Priority": "urgent", "Tags": "rotating_light,warning"},
            timeout=10
        ))
    except: pass

async def transcribe_voice(voice_path):
    started = time.time()
    rn = 0
    while True:
        rn += 1
        try:
            with open(voice_path, "rb") as f: audio = f.read()
            if len(audio) < 100: return None
        except: return None
        for mn in STT_MODELS:
            try:
                resp = ai.models.generate_content(model=mn, contents=[types.Content(parts=[
                    types.Part.from_bytes(data=audio, mime_type="audio/ogg"),
                    types.Part.from_text(text="این فایل صوتی رو دقیقاً به همون زبونی که صحبت شده، رونویسی کن. فقط متن رو بده.")
                ])])
                t = (resp.text or "").strip()
                if t: return t
            except: continue
        if time.time() - started > 300: return None
        await asyncio.sleep(RETRY_DELAYS[min(rn-1, len(RETRY_DELAYS)-1)])

async def handle_voice_input(event):
    try: await event.reply("🎤 دارم گوش می‌دم...")
    except: pass
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp: vp = tmp.name
    try:
        await event.download_media(file=vp)
        return await transcribe_voice(vp)
    finally:
        try: os.unlink(vp)
        except: pass

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
        if not clean or len(clean) < 2: return None
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp: mp3 = tmp.name
        await edge_tts.Communicate(clean, voice).save(mp3)
        ogg = mp3.replace(".mp3", ".ogg")
        res = subprocess.run(["ffmpeg", "-y", "-i", mp3, "-c:a", "libopus", "-b:a", "48k",
                              "-vbr", "on", "-compression_level", "10", "-frame_duration", "60",
                              "-application", "voip", ogg], capture_output=True, timeout=30)
        try: os.unlink(mp3)
        except: pass
        return ogg if res.returncode == 0 else None
    except: return None

async def send_voice_reply(chat_id, reply_to_id, text):
    if not runtime_settings.get("voice_output", True): return
    vp = await text_to_voice(text)
    if not vp: return
    try:
        await client.send_file(chat_id, vp, voice_note=True, reply_to=reply_to_id)
    except: pass
    finally:
        try: os.unlink(vp)
        except: pass


client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)


# ============ REST API ============
CORS = {"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "X-API-Key, Content-Type", "Access-Control-Allow-Methods": "GET, POST, OPTIONS"}

def auth(req): return (req.headers.get("X-API-Key") or req.query.get("api_key")) == API_KEY
def jr(data, status=200): return web.json_response(data, status=status, headers=CORS)

async def opts(req): return web.Response(headers=CORS)
async def api_ping(req): return jr({"pong": True, "time": time.time()})

async def api_status(req):
    if not auth(req): return jr({"error": "Unauthorized"}, 401)
    try:
        me = await client.get_me()
        return jr({"online": True, "user_id": me.id, "username": me.username, "first_name": me.first_name,
                   "uptime_seconds": int(time.time() - started_at),
                   "facts_count": len(memory.get("learned_facts", {})),
                   "paused_chats_count": sum(1 for cid in owner_last_seen if is_owner_paused(cid)),
                   "globally_muted": is_globally_muted(),
                   "custom_rules_count": len(memory.get("custom_rules", [])),
                   "personality_override": memory.get("personality_override", ""),
                   "dm_senders": dm_senders[:10], "all_senders": all_senders[:10],
                   "settings": runtime_settings})
    except Exception as e: return jr({"error": str(e)}, 500)

async def api_settings_get(req):
    if not auth(req): return jr({"error": "Unauthorized"}, 401)
    return jr({"settings": runtime_settings})

async def api_settings_post(req):
    if not auth(req): return jr({"error": "Unauthorized"}, 401)
    try: data = await req.json()
    except: return jr({"error": "Invalid"}, 400)
    allowed = {"pause_enabled", "pause_duration", "voice_output", "voice_input", "voice_name", "alert_delay", "max_retry_time"}
    upd = {k: v for k, v in data.items() if k in allowed}
    runtime_settings.update(upd)
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
            out.append({"chat_id": cid, "name": name, "remaining_seconds": rem})
    out.sort(key=lambda x: x["remaining_seconds"])
    return jr({"paused": out})

async def api_pause(req):
    if not auth(req): return jr({"error": "Unauthorized"}, 401)
    try:
        data = await req.json()
        cid = int(data["chat_id"]); dur = data.get("duration")
    except: return jr({"error": "chat_id required"}, 400)
    owner_last_seen[cid] = time.time()
    if dur: custom_pause_durations[cid] = int(dur)
    return jr({"ok": True})

async def api_resume(req):
    if not auth(req): return jr({"error": "Unauthorized"}, 401)
    try:
        data = await req.json()
        cid = int(data["chat_id"])
    except: return jr({"error": "chat_id required"}, 400)
    owner_last_seen.pop(cid, None); custom_pause_durations.pop(cid, None)
    return jr({"ok": True})

async def api_resume_all(req):
    if not auth(req): return jr({"error": "Unauthorized"}, 401)
    c = len(owner_last_seen)
    owner_last_seen.clear(); custom_pause_durations.clear()
    return jr({"ok": True, "cleared": c})

async def api_logs(req):
    if not auth(req): return jr({"error": "Unauthorized"}, 401)
    return jr({"logs": list(recent_logs)})

async def api_facts(req):
    if not auth(req): return jr({"error": "Unauthorized"}, 401)
    return jr({"facts": memory.get("learned_facts", {}), "notes": memory.get("owner_notes", []),
               "rules": memory.get("custom_rules", []), "personality": memory.get("personality_override", "")})

def create_app():
    app = web.Application()
    app.router.add_route("OPTIONS", "/{tail:.*}", opts)
    app.router.add_get("/api/ping", api_ping)
    app.router.add_get("/api/status", api_status)
    app.router.add_get("/api/settings", api_settings_get)
    app.router.add_post("/api/settings", api_settings_post)
    app.router.add_get("/api/paused", api_paused)
    app.router.add_post("/api/pause", api_pause)
    app.router.add_post("/api/resume", api_resume)
    app.router.add_post("/api/resume_all", api_resume_all)
    app.router.add_get("/api/logs", api_logs)
    app.router.add_get("/api/facts", api_facts)
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
            await event.reply("🚫 ویس غیرفعال"); return
        text = await handle_voice_input(event) or ""
        if not text:
            await event.reply("نتونستم ویس رو بفهمم 😅"); return
        await event.reply(f"🎤 «{text}»")

    if not text.strip(): return
    log(f"🔍 Owner: {text[:50]!r}")

    prompt = build_prompt("owner", text, is_owner=True)
    raw, _ = await ask_gemini(prompt, chat_id=event.chat_id)
    if raw is None:
        await event.reply("الان نشد، دوباره بفرست 🙏"); return

    # ===== POPULATE_DM_SENDERS =====
    if "[POPULATE_DM_SENDERS]" in raw:
        await event.reply("⏳ دارم DM ها رو می‌خونم...")
        count = await populate_senders_from_telegram(include_all=False)
        save_senders_if_dirty()
        if count == 0:
            await event.reply("❓ DM با پیام دریافتی پیدا نکردم")
        else:
            prompt2 = build_prompt("owner", text + " [DM لود شد]", is_owner=True)
            raw2, _ = await ask_gemini(prompt2, chat_id=event.chat_id)
            if raw2:
                r = re.sub(r"\[(POPULATE_DM_SENDERS|WHO_LAST_DM|IMPORTANT|NORMAL|ENABLE_VOICE|DISABLE_VOICE)\]", "", raw2).strip()
                if r:
                    last_bot_send_time = time.time()
                    await event.reply(r); return
            if dm_senders:
                last = dm_senders[0]
                ago = int(time.time() - last["timestamp"])
                last_bot_send_time = time.time()
                await event.reply(f"📨 آخرین DM: **{last['name']}** — {ago//60}د {ago%60}ث پیش")
        return

    # ===== POPULATE_ALL_SENDERS =====
    if "[POPULATE_ALL_SENDERS]" in raw:
        await event.reply("⏳ دارم همه چت‌ها رو می‌خونم...")
        count = await populate_senders_from_telegram(include_all=True)
        save_senders_if_dirty()
        if count == 0:
            await event.reply("❓ پیام دریافتی پیدا نکردم")
        else:
            prompt2 = build_prompt("owner", text + " [ALL لود شد]", is_owner=True)
            raw2, _ = await ask_gemini(prompt2, chat_id=event.chat_id)
            if raw2:
                r = re.sub(r"\[(POPULATE_ALL_SENDERS|WHO_LAST_ALL|IMPORTANT|NORMAL|ENABLE_VOICE|DISABLE_VOICE)\]", "", raw2).strip()
                if r:
                    last_bot_send_time = time.time()
                    await event.reply(r); return
            if all_senders:
                last = all_senders[0]
                ago = int(time.time() - last["timestamp"])
                kind = "bot" if last.get("is_bot") else ("group" if last.get("is_group") else "DM")
                last_bot_send_time = time.time()
                await event.reply(f"🌐 کلی، آخرین پیام از **{last['name']}** ({kind}) — {ago//60}د {ago%60}ث پیش")
        return

    # ===== WHO_LAST_DM =====
    if "[WHO_LAST_DM]" in raw:
        if not dm_senders:
            await populate_senders_from_telegram(include_all=False)
        if not dm_senders:
            await event.reply("❓ DM پیدا نشد"); return
        last = dm_senders[0]
        ago = int(time.time() - last["timestamp"])
        await event.reply(f"📨 **{last['name']}** — {ago//60}د {ago%60}ث پیش")
        return

    # ===== WHO_LAST_ALL =====
    if "[WHO_LAST_ALL]" in raw:
        if not all_senders:
            await populate_senders_from_telegram(include_all=True)
        if not all_senders:
            await event.reply("❓ پیام پیدا نشد"); return
        last = all_senders[0]
        ago = int(time.time() - last["timestamp"])
        kind = "bot" if last.get("is_bot") else ("group" if last.get("is_group") else "DM")
        await event.reply(f"🌐 **{last['name']}** ({kind}) — {ago//60}د {ago%60}ث پیش")
        return

    # ===== SHOW_LAST_INCOMING =====
    m = re.search(r"\[SHOW_LAST_INCOMING:\s*([^\]]+)\]", raw)
    if m:
        target = m.group(1).strip()
        cid = await resolve_chat_id(target)
        if cid is None:
            await event.reply(f"❓ «{target}» پیدا نشد"); return
        name = chat_names_cache.get(cid, str(cid))
        content = await get_last_incoming_text(cid)
        if not content:
            await event.reply(f"❓ پیام دریافتی از «{name}» پیدا نشد"); return
        await event.reply(f"📩 آخرین پیام از **{name}**:\n\n{content}")
        return

    # ===== SHOW_LAST_OUTGOING =====
    m = re.search(r"\[SHOW_LAST_OUTGOING:\s*([^\]]+)\]", raw)
    if m:
        target = m.group(1).strip()
        cid = await resolve_chat_id(target)
        if cid is None:
            await event.reply(f"❓ «{target}» پیدا نشد"); return
        name = chat_names_cache.get(cid, str(cid))
        content = await get_last_outgoing_text(cid)
        if not content:
            await event.reply(f"ℹ️ هیچ پاسخ ارسالی به «{name}» پیدا نشد"); return
        await event.reply(f"🤖 آخرین پاسخ به **{name}**:\n\n{content}")
        return

    # ===== SHOW_MSG =====
    m = re.search(r"\[SHOW_MSG:\s*([^|\]]+)(?:\|\s*(\d+))?\]", raw)
    if m:
        target = m.group(1).strip()
        count = min(int(m.group(2)) if m.group(2) else 5, 20)
        cid = await resolve_chat_id(target)
        if cid is None:
            await event.reply(f"❓ «{target}» پیدا نشد"); return
        name = chat_names_cache.get(cid, str(cid))
        content = await get_last_messages_text(cid, count)
        await event.reply(f"📜 **{name}** — {count} پیام آخر:\n\n{content}")
        return

    # ===== UNPAUSE_ALL =====
    if "[UNPAUSE_ALL]" in raw:
        c = len(owner_last_seen)
        owner_last_seen.clear(); custom_pause_durations.clear()
        track_action("UNPAUSE_ALL", c)
        r = re.sub(r"\[(UNPAUSE_ALL|IMPORTANT|NORMAL|ENABLE_VOICE|DISABLE_VOICE)\]", "", raw).strip()
        if not r: r = f"✅ همه {c} چت آزاد شدن"
        last_bot_send_time = time.time()
        await event.reply(r); return

    # ===== UNPAUSE =====
    m = re.search(r"\[UNPAUSE:\s*([^\]]+)\]", raw)
    if m:
        target = m.group(1).strip()
        cid = await resolve_chat_id(target)
        if cid is None:
            await event.reply(f"❓ «{target}» پیدا نشد"); return
        name = chat_names_cache.get(cid, str(cid))
        if cid in owner_last_seen:
            del owner_last_seen[cid]; custom_pause_durations.pop(cid, None)
            track_action("UNPAUSE", cid, name)
            r = re.sub(r"\[(UNPAUSE:[^\]]+|IMPORTANT|NORMAL|ENABLE_VOICE|DISABLE_VOICE)\]", "", raw).strip()
            if not r: r = f"✅ «{name}» آزاد شد"
        else:
            r = f"ℹ️ «{name}» از قبل آزاده"
        last_bot_send_time = time.time()
        await event.reply(r); return

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
            if not r: r = f"🛑 «{name}» دائمی"
            last_bot_send_time = time.time()
            await event.reply(r)
        else: await event.reply(f"❓ «{target}» پیدا نشد")
        return

    # ===== PAUSE =====
    m = re.search(r"\[PAUSE:\s*([^|\]]+)(?:\|\s*(\d+))?\]", raw)
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
            if not r: r = f"🛑 «{name}» {mins}د"
            last_bot_send_time = time.time()
            await event.reply(r)
        else: await event.reply(f"❓ «{target}» پیدا نشد")
        return

    # ===== FORWARD_LAST =====
    m = re.search(r"\[FORWARD_LAST(?:\s*\|\s*(\d+))?\]", raw)
    if m:
        count = min(int(m.group(1)) if m.group(1) else 1, 20)
        if not dm_senders:
            await populate_senders_from_telegram(include_all=False)
        if not dm_senders:
            await event.reply("❓ DM نبود"); return
        last = dm_senders[0]
        fwd, err = await forward_last_messages(last["chat_id"], count)
        if err: await event.reply(f"❌ {err}")
        else:
            track_action("FORWARD_LAST", last["chat_id"], last["name"])
            r = re.sub(r"\[(FORWARD_LAST[^\]]*|IMPORTANT|NORMAL|ENABLE_VOICE|DISABLE_VOICE)\]", "", raw).strip()
            if not r: r = f"📨 {fwd} از «{last['name']}»"
            last_bot_send_time = time.time()
            await event.reply(r)
        return

    # ===== FORWARD =====
    m = re.search(r"\[FORWARD:\s*([^|\]]+)(?:\|\s*(\d+))?\]", raw)
    if m:
        target = m.group(1).strip()
        count = min(int(m.group(2)) if m.group(2) else 1, 20)
        cid = await resolve_chat_id(target)
        if cid:
            fwd, err = await forward_last_messages(cid, count)
            name = chat_names_cache.get(cid, str(cid))
            if err: await event.reply(f"❌ {err}")
            else:
                track_action("FORWARD", cid, f"{name} {fwd}")
                r = re.sub(r"\[(FORWARD:[^\]]+|IMPORTANT|NORMAL|ENABLE_VOICE|DISABLE_VOICE)\]", "", raw).strip()
                if not r: r = f"📨 {fwd} از «{name}»"
                last_bot_send_time = time.time()
                await event.reply(r)
        else: await event.reply(f"❓ «{target}» پیدا نشد")
        return

    # ===== CONTEXT =====
    m = re.search(r"\[CONTEXT:\s*([^\]]+)\]", raw)
    if m:
        target = m.group(1).strip()
        cid = await resolve_chat_id(target)
        if cid:
            ctx = await get_last_messages_text(cid, 5)
            name = chat_names_cache.get(cid, str(cid))
            await event.reply(f"👁️ {name}:\n\n{ctx}")
        else: await event.reply(f"❓ «{target}» پیدا نشد")
        return

    # ===== GLOBAL_MUTE =====
    m = re.search(r"\[GLOBAL_MUTE:\s*(\d+)\]", raw)
    if m:
        mins = int(m.group(1))
        global_mute_until = time.time() + mins * 60
        track_action("GLOBAL_MUTE", f"{mins}m")
        r = re.sub(r"\[(GLOBAL_MUTE:[^\]]+|IMPORTANT|NORMAL|ENABLE_VOICE|DISABLE_VOICE)\]", "", raw).strip()
        if not r: r = f"🔇 {mins}د"
        last_bot_send_time = time.time()
        await event.reply(r); return

    # ===== GLOBAL_UNMUTE =====
    if "[GLOBAL_UNMUTE]" in raw:
        global_mute_until = 0
        r = re.sub(r"\[(GLOBAL_UNMUTE|IMPORTANT|NORMAL|ENABLE_VOICE|DISABLE_VOICE)\]", "", raw).strip()
        if not r: r = "🔊 آنمیوت"
        last_bot_send_time = time.time()
        await event.reply(r); return

    # ===== Rules/Personality =====
    m = re.search(r"\[RULE_ADD:\s*([^\]]+)\]", raw)
    if m:
        rule = m.group(1).strip()
        memory.setdefault("custom_rules", [])
        if rule not in memory["custom_rules"]:
            memory["custom_rules"].append(rule)
            save_memory_async(); track_action("RULE_ADD", rule[:40])
        r = re.sub(r"\[(RULE_ADD:[^\]]+|IMPORTANT|NORMAL|ENABLE_VOICE|DISABLE_VOICE)\]", "", raw).strip()
        if not r: r = f"✅ #{len(memory['custom_rules'])}\n📏 {rule}"
        last_bot_send_time = time.time()
        await event.reply(r); return

    m = re.search(r"\[RULE_REMOVE:\s*(\d+)\]", raw)
    if m:
        idx = int(m.group(1)) - 1
        rules = memory.get("custom_rules", [])
        if 0 <= idx < len(rules):
            removed = rules.pop(idx)
            save_memory_async()
            r = re.sub(r"\[(RULE_REMOVE:\d+|IMPORTANT|NORMAL|ENABLE_VOICE|DISABLE_VOICE)\]", "", raw).strip()
            if not r: r = f"🗑️ {removed}"
            last_bot_send_time = time.time()
            await event.reply(r)
        else: await event.reply(f"❓ #{idx+1} نیست")
        return

    if "[RULE_LIST]" in raw:
        rules = memory.get("custom_rules", [])
        if not rules: await event.reply("📏 هیچ"); return
        await event.reply("📏 **قوانین:**\n" + "\n".join(f"{i}. {r}" for i, r in enumerate(rules, 1)))
        return

    if "[RULE_CLEAR]" in raw:
        c = len(memory.get("custom_rules", []))
        memory["custom_rules"] = []
        save_memory_async()
        r = re.sub(r"\[(RULE_CLEAR|IMPORTANT|NORMAL|ENABLE_VOICE|DISABLE_VOICE)\]", "", raw).strip()
        if not r: r = f"🧹 {c} پاک شد"
        last_bot_send_time = time.time()
        await event.reply(r); return

    m = re.search(r"\[PERSONALITY_SET:\s*([^\]]+)\]", raw)
    if m:
        p = m.group(1).strip()
        memory["personality_override"] = p
        save_memory_async()
        r = re.sub(r"\[(PERSONALITY_SET:[^\]]+|IMPORTANT|NORMAL|ENABLE_VOICE|DISABLE_VOICE)\]", "", raw).strip()
        if not r: r = f"🎭 {p}"
        last_bot_send_time = time.time()
        await event.reply(r); return

    if "[PERSONALITY_CLEAR]" in raw:
        memory["personality_override"] = ""
        save_memory_async()
        r = re.sub(r"\[(PERSONALITY_CLEAR|IMPORTANT|NORMAL|ENABLE_VOICE|DISABLE_VOICE)\]", "", raw).strip()
        if not r: r = "🎭 پیش‌فرض"
        last_bot_send_time = time.time()
        await event.reply(r); return

    # ===== Memory =====
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
                await event.reply(f"✅ {k.strip()} → {v.strip()}")
                return
        except: pass

    if "[LEARN_RAW:" in raw:
        try:
            inner = raw.split("[LEARN_RAW:", 1)[1].split("]", 1)[0].strip()
            fid = f"fact_{len(memory.get('learned_facts', {})) + 1}"
            memory.setdefault("learned_facts", {})[fid] = inner
            save_memory_async()
            last_bot_send_time = time.time()
            await event.reply(f"✅ {inner}")
            return
        except: pass

    if "[FORGET:" in raw:
        try:
            k = raw.split("[FORGET:", 1)[1].split("]", 1)[0].strip()
            facts = memory.get("learned_facts", {})
            if k in facts:
                del facts[k]; save_memory_async()
                last_bot_send_time = time.time()
                await event.reply(f"🗑️ {k}")
            else:
                last_bot_send_time = time.time()
                await event.reply(f"❓ {k}")
            return
        except: pass

    if "[SHOW_MEMORY]" in raw:
        facts = memory.get("learned_facts", {})
        r = "🧠 **حافظه:**\n\n"
        if facts:
            for k, v in facts.items(): r += f"• {k}: {v}\n"
        else: r += "خالی\n"
        last_bot_send_time = time.time()
        await event.reply(r); return

    if enable_voice: set_voice_pref("owner", True)
    elif disable_voice: set_voice_pref("owner", False)

    reply = re.sub(r"\[(IMPORTANT|NORMAL|ENABLE_VOICE|DISABLE_VOICE)\]", "", raw).strip()
    if not reply: return

    histories.setdefault("owner", []).append(f"You: {reply}")
    last_bot_send_time = time.time()
    await event.reply(reply)

    if get_voice_pref("owner") is True and not disable_voice:
        await send_voice_reply(event.chat_id, event.message.id, reply)


# ============ Incoming handler (tracks all + replies to DM) ============
@client.on(events.NewMessage(incoming=True))
async def on_incoming(event):
    try: sender = await event.get_sender()
    except: sender = None
    if sender is None or sender.id == OWNER_ID: return

    # Track ALL chats
    if event.is_private:
        sname = getattr(sender, "first_name", "") or getattr(sender, "username", "") or str(sender.id)
        track_sender(sender.id, sname, is_bot=getattr(sender, "bot", False), is_group=False)
        # Don't reply to bots
        if getattr(sender, "bot", False): return
    else:
        title = getattr(event.chat, "title", None) or str(event.chat_id)
        track_sender(event.chat_id, title, is_bot=False, is_group=True)
        return  # skip groups for replies

    if is_globally_muted(): return
    if is_owner_paused(event.chat_id): return

    text = event.message.text or ""

    if not text.strip() and event.message.voice:
        if not runtime_settings.get("voice_input", True): return
        text = await handle_voice_input(event) or ""
        if not text:
            await event.reply("نتونستم ویس رو بفهمم 😅"); return

    if not text.strip(): return

    user_key = str(sender.id)
    log(f"🔍 User {user_key}: {text[:50]!r}")

    prompt = build_prompt(sender.id, text, is_owner=False)
    raw, _ = await ask_gemini(prompt, chat_id=event.chat_id)
    if raw is None: return

    important = "[IMPORTANT]" in raw
    enable_voice = "[ENABLE_VOICE]" in raw
    disable_voice = "[DISABLE_VOICE]" in raw

    if enable_voice: set_voice_pref(user_key, True)
    elif disable_voice: set_voice_pref(user_key, False)

    reply = re.sub(r"\[(IMPORTANT|NORMAL|ENABLE_VOICE|DISABLE_VOICE)\]", "", raw).strip()
    if not reply: return

    histories.setdefault(user_key, []).append(f"You: {reply}")

    # Store bot reply for later "what did you say" queries
    bot_last_replies[event.chat_id] = {"text": reply, "ts": time.time()}

    await event.reply(reply)

    if get_voice_pref(user_key) is True and not disable_voice:
        await send_voice_reply(event.chat_id, event.message.id, reply)

    if important:
        sender_name = getattr(sender, "first_name", "") or getattr(sender, "username", "") or str(sender.id)
        uname = f"@{sender.username}" if sender.username else "—"
        alert = (f"⚠️ پیام مهم\n\n👤 {sender_name} ({uname})\n🆔 `{sender.id}`\n\n"
                 f"📩 {text}\n\n🤖 {reply}")
        await client.send_message("me", alert, silent=True)
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
    await client.start()
    me = await client.get_me()
    log(f"✅ {me.first_name} (id={me.id})")
    await start_api()
    log("🚀 Running...")
    log(f"📥 DM: {len(dm_senders)} | ALL: {len(all_senders)}")
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
