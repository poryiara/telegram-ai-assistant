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

# ============ Config ============
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

IGNORED_USER_IDS = {777000, 42777}
PIPE_CHAR = chr(124)  # کاراکتر خط عمودی بدون هیچ مشکل کپی‌پیست

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
bot_sending_to_chat = {}
dm_senders = []
all_senders = []
bot_last_replies = {}
recent_actions = deque(maxlen=15)
recent_logs = deque(maxlen=100)
started_at = time.time()
dm_dirty = False
all_dirty = False
last_memory_save_ts = 0

ALERT_PREFIX = "⚠️ پیام مهم"

def log(message):
    ts = time.strftime("%H:%M:%S")
    recent_logs.append({"time": ts, "text": message})
    print(f"{ts} {message}")

def track_action(action_type, target, extra=""):
    recent_actions.append({
        "time": time.strftime("%H:%M:%S"),
        "type": action_type,
        "target": target,
        "extra": extra
    })

ai = genai.Client(api_key=GEMINI_API_KEY)

MODELS = [
    "gemini-3-flash-preview",
    "gemini-3-flash",
    "gemini-3.1-flash-lite-preview",
    "gemini-3.1-flash-lite",
]
STT_MODELS = [
    "gemini-3-flash-preview",
    "gemini-3-flash",
    "gemini-3.1-flash-lite-preview",
    "gemini-3.1-flash-lite",
]
RETRY_DELAYS = [0.5, 1, 2, 3, 5, 8, 13, 20, 30, 30, 30]

# ============ Safe Tag Helpers (No Regex Escaping Needed) ============
def extract_tag_content(text, tag_prefix):
    """جدا کردن محتوای داخل تگ مثل [TAG: content] بدون نیاز به Regex"""
    marker = "[" + tag_prefix
    idx = text.find(marker)
    if idx == -1:
        return None
    start = idx + len(marker)
    end = text.find("]", start)
    if end == -1:
        return None
    return text[start:end].strip()

def strip_all_system_tags(text):
    """پاک کردن تمام تگ‌های سیستمی از متن نهایی پیام"""
    if not text:
        return ""
    known_prefixes = (
        "POPULATE_DM_SENDERS", "WHO_LAST_DM", "POPULATE_ALL_SENDERS", "WHO_LAST_ALL",
        "SHOW_LAST_INCOMING", "SHOW_LAST_OUTGOING", "SHOW_MSG", "UNPAUSE_ALL", "UNPAUSE",
        "PAUSE_FOREVER", "PAUSE", "FORWARD_LAST", "FORWARD", "CONTEXT", "GLOBAL_MUTE",
        "GLOBAL_UNMUTE", "RULE_ADD", "RULE_REMOVE", "RULE_LIST", "RULE_CLEAR",
        "PERSONALITY_SET", "PERSONALITY_CLEAR", "PERSONALITY_SHOW", "LEARN_RAW", "LEARN",
        "FORGET", "SHOW_MEMORY", "IMPORTANT", "NORMAL", "ENABLE_VOICE", "DISABLE_VOICE"
    )
    out = text
    for p in known_prefixes:
        while True:
            idx = out.find("[" + p)
            if idx == -1:
                break
            end = out.find("]", idx)
            if end == -1:
                break
            out = out[:idx] + out[end + 1:]
    return out.strip()

# ============ ProgressReporter ============
class ProgressReporter:
    MESSAGES = [
        "⌛ یه کم طولانی شد، هنوز دارم روش کار می‌کنم...",
        "💪 دارم تلاش می‌کنم، یه لحظه صبر...",
        "🙏 عذر می‌خوام طول کشید، هنوز مشغولم...",
        "😅 خیلی طول کشید ولی دارم ادامه می‌دم...",
        "⏳ هنوز در حال پردازش...",
        "🔍 دارم دنبال جواب می‌گردم...",
        "⚙️ پردازش در جریانه، صبر کن...",
        "🔄 دارم دوباره تلاش می‌کنم...",
        "💭 تقریباً تمومه، فقط چند لحظه...",
        "🎯 دارم نتیجه رو آماده می‌کنم...",
        "📝 دارم جواب رو می‌نویسم...",
        "✨ آخرین تلاش...",
    ]
    
    def __init__(self, chat_id, initial_msg=None, interval=10):
        self.chat_id = chat_id
        self.interval = interval
        self.initial_msg = initial_msg
        self.task = None
        self.done = asyncio.Event()
        self.step = 0
    
    async def _run(self):
        global last_bot_send_time
        if self.initial_msg:
            try:
                last_bot_send_time = time.time()
                bot_sending_to_chat[self.chat_id] = time.time()
                await client.send_message(self.chat_id, self.initial_msg)
            except:
                pass
        while not self.done.is_set():
            try:
                await asyncio.wait_for(self.done.wait(), timeout=self.interval)
                return
            except asyncio.TimeoutError:
                pass
            if self.step < len(self.MESSAGES):
                msg = self.MESSAGES[self.step]
                elapsed = (self.step + 1) * self.interval
                if self.step >= 3:
                    msg = msg + f" ({elapsed}s)"
                self.step += 1
                try:
                    last_bot_send_time = time.time()
                    bot_sending_to_chat[self.chat_id] = time.time()
                    await client.send_message(self.chat_id, msg)
                    await client.action(self.chat_id, "typing")
                except:
                    pass
    
    def start(self):
        self.task = asyncio.create_task(self._run())
    
    async def stop(self):
        self.done.set()
        if self.task:
            try:
                await asyncio.wait_for(self.task, timeout=2)
            except:
                pass

# ============ Helpers ============
def format_ago(seconds):
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} ثانیه پیش"
    mins = seconds // 60
    secs = seconds % 60
    if mins < 60:
        return f"{mins} دقیقه و {secs} ثانیه پیش"
    hours = mins // 60
    rem_mins = mins % 60
    return f"{hours} ساعت و {rem_mins} دقیقه پیش"

async def cache_chat_name(chat_id, force=False):
    if not force and chat_id in chat_names_cache and not str(chat_names_cache[chat_id]).isdigit():
        return chat_names_cache[chat_id]
    try:
        entity = await client.get_entity(chat_id)
        name = getattr(entity, "first_name", None) or getattr(entity, "title", None) or str(chat_id)
        chat_names_cache[chat_id] = name
        return name
    except:
        if chat_id not in chat_names_cache:
            chat_names_cache[chat_id] = str(chat_id)
        return chat_names_cache[chat_id]

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

def track_sender(chat_id, name, is_bot=False, is_group=False, ts=None):
    global dm_senders, all_senders, dm_dirty, all_dirty
    if chat_id in IGNORED_USER_IDS or chat_id == OWNER_ID:
        return
    now_ts = ts if ts is not None else time.time()
    if now_ts > time.time() + 60:
        now_ts = time.time()

    chat_names_cache[chat_id] = name
    entry = {
        "chat_id": chat_id,
        "name": name,
        "timestamp": now_ts,
        "is_bot": is_bot,
        "is_group": is_group
    }
    all_senders = [x for x in all_senders if x["chat_id"] != chat_id and x["chat_id"] not in IGNORED_USER_IDS]
    all_senders.insert(0, entry)
    all_senders.sort(key=lambda x: x.get("timestamp", 0), reverse=True)
    all_senders = all_senders[:50]
    all_dirty = True

    if not is_bot and not is_group:
        dm_senders = [x for x in dm_senders if x["chat_id"] != chat_id and x["chat_id"] not in IGNORED_USER_IDS]
        dm_senders.insert(0, entry)
        dm_senders.sort(key=lambda x: x.get("timestamp", 0), reverse=True)
        dm_senders = dm_senders[:30]
        dm_dirty = True

async def forward_last_incoming(chat_id):
    global last_bot_send_time
    try:
        messages = await client.get_messages(chat_id, limit=20)
        for m in messages:
            if not m.out:
                last_bot_send_time = time.time()
                await client.forward_messages("me", m)
                return True
        return False
    except Exception as e:
        log(f"⚠️ Fwd incoming: {str(e)[:80]}")
        return False

async def forward_last_outgoing(chat_id):
    global last_bot_send_time
    try:
        messages = await client.get_messages(chat_id, limit=20)
        for m in messages:
            if m.out:
                last_bot_send_time = time.time()
                await client.forward_messages("me", m)
                return True
        return False
    except Exception as e:
        log(f"⚠️ Fwd outgoing: {str(e)[:80]}")
        return False

async def get_last_messages_text(chat_id, count=5):
    try:
        messages = await client.get_messages(chat_id, limit=count)
        if not messages:
            return "هیچ پیامی نیست"
        lines = []
        for m in reversed(messages):
            who = "🤖 ربات/پوریا" if m.out else "👤 کاربر"
            text = m.text or "[رسانه/ویس]"
            if len(text) > 200:
                text = text[:200] + "..."
            lines.append(f"{who}: {text}")
        return "\n".join(lines)
    except Exception as e:
        return f"خطا: {str(e)[:100]}"

async def populate_senders_from_telegram(limit=20, include_all=False):
    global dm_senders, all_senders, dm_dirty, all_dirty
    try:
        dm_results = []
        all_results = []
        async for dialog in client.iter_dialogs(limit=100):
            if dialog.id == OWNER_ID or dialog.id in IGNORED_USER_IDS:
                continue
            entity = dialog.entity
            if entity is None:
                continue
            is_bot = getattr(entity, "bot", False)
            is_group = not dialog.is_user
            if not include_all and (is_bot or is_group):
                continue
            try:
                msgs = await client.get_messages(dialog.id, limit=15)
            except:
                continue
            last_incoming = None
            for m in msgs:
                if not m.out:
                    last_incoming = m
                    break
            if not last_incoming:
                continue
            name = (getattr(entity, "first_name", None) or getattr(entity, "title", None)
                    or getattr(entity, "username", None) or str(dialog.id))
            chat_names_cache[dialog.id] = name
            ts = last_incoming.date.timestamp() if last_incoming.date else time.time()
            item = {
                "chat_id": dialog.id,
                "name": name,
                "timestamp": ts,
                "is_bot": is_bot,
                "is_group": is_group
            }
            all_results.append(item)
            if not is_bot and not is_group:
                dm_results.append(item)

        if include_all:
            all_results.sort(key=lambda x: x["timestamp"], reverse=True)
            all_senders = all_results[:limit]
            all_dirty = True
            if dm_results:
                dm_results.sort(key=lambda x: x["timestamp"], reverse=True)
                dm_senders = dm_results[:limit]
                dm_dirty = True
            log(f"📥 Populated {len(all_senders)} ALL & {len(dm_senders)} DM from Telegram")
            return len(all_senders)
        else:
            dm_results.sort(key=lambda x: x["timestamp"], reverse=True)
            dm_senders = dm_results[:limit]
            dm_dirty = True
            log(f"📥 Populated {len(dm_senders)} DM from Telegram")
            return len(dm_senders)
    except Exception as e:
        log(f"⚠️ Populate: {str(e)[:100]}")
        return 0

async def resolve_chat_id(target_str):
    target_str = str(target_str).strip()
    if target_str.lower() in ["last", "اخرین", "آخرین", "اخری", "آخری"]:
        if dm_senders:
            return dm_senders[0]["chat_id"]
        if await populate_senders_from_telegram(include_all=False) > 0:
            return dm_senders[0]["chat_id"]
        return None
    try:
        return int(target_str)
    except:
        pass
    tl = target_str.lower()
    for cid, name in chat_names_cache.items():
        if name and (tl in str(name).lower() or str(name).lower() in tl):
            return cid
    for item in dm_senders + all_senders:
        if tl in item["name"].lower() or item["name"].lower() in tl:
            return item["chat_id"]
    try:
        async for dialog in client.iter_dialogs(limit=80):
            entity = dialog.entity
            if entity is None:
                continue
            en = getattr(entity, "first_name", "") or getattr(entity, "title", "") or ""
            un = getattr(entity, "username", "") or ""
            if (en and (tl in en.lower() or en.lower() in tl)) or (un and tl == un.lower()):
                chat_names_cache[dialog.id] = en or un
                return dialog.id
    except:
        pass
    return None

def build_paused_section():
    if not owner_last_seen:
        return "=== PAUSED CHATS ===\nهیچ چتی پاز نیست"
    lines = ["=== PAUSED CHATS ==="]
    found = False
    for cid in list(owner_last_seen.keys()):
        rem = pause_remaining(cid)
        if rem > 0:
            found = True
            name = chat_names_cache.get(cid, "?")
            lines.append(f"- {name} (id:{cid}) — زمان باقی‌مانده پاز: {rem//60} دقیقه و {rem%60} ثانیه")
    if not found:
        return "=== PAUSED CHATS ===\nهیچ چتی پاز نیست"
    return "\n".join(lines)

def build_dm_senders_section():
    if not dm_senders:
        return "=== DM SENDERS (فقط چت‌های خصوصی افراد واقعی) ===\n❌ خالی. اگه پرسید کی پیام داده، حتماً تگ [POPULATE_DM_SENDERS] رو بذار."
    sorted_dms = sorted(
        [x for x in dm_senders if x["chat_id"] not in IGNORED_USER_IDS],
        key=lambda x: x.get("timestamp", 0),
        reverse=True
    )
    lines = ["=== DM SENDERS (لیست آخرین کسانی که در پی‌وی/خصوصی پیام دادند به ترتیب از جدیدترین به قدیمی‌ترین) ==="]
    for i, it in enumerate(sorted_dms[:10], 1):
        ago_str = format_ago(time.time() - it["timestamp"])
        lines.append(f"{i}. {it['name']} (id:{it['chat_id']}) — {ago_str}")
    return "\n".join(lines)

def build_bot_replies_section():
    if not bot_last_replies:
        return "=== BOT RECENT REPLIES (آخرین چت‌هایی که خودت/دستیار در این نشست بهشون جواب دادی) ===\nهنوز در این نشست به کسی جواب ندادم."
    sorted_replies = sorted(bot_last_replies.items(), key=lambda x: x[1]["ts"], reverse=True)
    lines = ["=== BOT RECENT REPLIES (آخرین کسانی که تو/دستیار بهشون پاسخ دادی — از جدیدترین) ==="]
    for i, (cid, info) in enumerate(sorted_replies[:10], 1):
        ago_str = format_ago(time.time() - info["ts"])
        name = info.get("name") or chat_names_cache.get(cid, str(cid))
        u_txt = info.get("user_text", "")[:40]
        b_txt = info.get("text", "")[:40]
        lines.append(f"{i}. {name} (id:{cid}) — {ago_str} - پیام کاربر: «{u_txt}» - جواب دستیار: «{b_txt}»")
    return "\n".join(lines)

def build_all_senders_section():
    if not all_senders:
        return "=== ALL CHATS ===\n❌ خالی. اگه پرسید، [POPULATE_ALL_SENDERS]"
    sorted_all = sorted(
        [x for x in all_senders if x["chat_id"] not in IGNORED_USER_IDS],
        key=lambda x: x.get("timestamp", 0),
        reverse=True
    )
    lines = ["=== ALL CHATS (همه چت‌ها شامل گروه‌ها و بات‌ها — فقط برای وقتی که کلمه «کلی» یا «همه چت‌ها» رو گفت) ==="]
    for i, it in enumerate(sorted_all[:10], 1):
        ago_str = format_ago(time.time() - it["timestamp"])
        kind = "bot" if it.get("is_bot") else ("group" if it.get("is_group") else "DM")
        lines.append(f"{i}. {it['name']} (id:{it['chat_id']}, {kind}) — {ago_str}")
    return "\n".join(lines)

def build_recent_actions_section():
    if not recent_actions:
        return ""
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

async def forward_last_messages(chat_id, count=1):
    global last_bot_send_time
    try:
        messages = await client.get_messages(chat_id, limit=count * 3)
        if not messages:
            return 0, "هیچ پیامی نیست"
        user_msgs = [m for m in messages if not m.out][:count]
        if not user_msgs:
            user_msgs = messages[:count]
        last_bot_send_time = time.time()
        await client.forward_messages("me", user_msgs)
        return len(user_msgs), None
    except Exception as e:
        return 0, str(e)

# ============ GitHub Memory ============
def github_headers():
    return {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}

def fetch_memory():
    empty = {
        "learned_facts": {},
        "owner_notes": [],
        "voice_preferences": {},
        "custom_rules": [],
        "personality_override": "",
        "dm_senders": [],
        "all_senders": []
    }
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
            mem.pop("last_senders", None)
            return mem, data["sha"]
        elif r.status_code == 404:
            return empty, None
    except Exception as e:
        log(f"⚠️ Fetch: {str(e)[:100]}")
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
        "dm_senders": list(dm_senders)[:30],
        "all_senders": list(all_senders)[:30],
    }
    content = base64.b64encode(json.dumps(to_save, ensure_ascii=False, indent=2).encode("utf-8")).decode("utf-8")
    payload = {"message": "Update AI memory", "content": content, "branch": GITHUB_BRANCH}
    if sha:
        payload["sha"] = sha
    try:
        r = requests.put(url, headers=github_headers(), json=payload, timeout=15)
        if r.status_code in (200, 201):
            log("💾 Saved memory to GitHub")
    except Exception as e:
        log(f"⚠️ Push: {str(e)[:100]}")

def save_memory_async():
    memory["dm_senders"] = list(dm_senders)[:30]
    memory["all_senders"] = list(all_senders)[:30]
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, push_memory, memory)

def save_senders_if_dirty(force=False):
    global dm_dirty, all_dirty, last_memory_save_ts
    now = time.time()
    if dm_dirty or (all_dirty and (force or now - last_memory_save_ts > 60)):
        last_memory_save_ts = now
        dm_dirty = False
        all_dirty = False
        save_memory_async()

memory, _sha = fetch_memory()
now_init = time.time()
if memory.get("dm_senders"):
    cleaned_dm = []
    for x in memory["dm_senders"]:
        if x.get("chat_id") in IGNORED_USER_IDS:
            continue
        if x.get("timestamp", 0) > now_init:
            x["timestamp"] = now_init - 3600
        cleaned_dm.append(x)
    dm_senders = cleaned_dm
    log(f"📥 Loaded {len(dm_senders)} DM from memory")

if memory.get("all_senders"):
    cleaned_all = []
    for x in memory["all_senders"]:
        if x.get("chat_id") in IGNORED_USER_IDS:
            continue
        if x.get("timestamp", 0) > now_init:
            x["timestamp"] = now_init - 3600
        cleaned_all.append(x)
    all_senders = cleaned_all
    log(f"📥 Loaded {len(all_senders)} ALL from memory")

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
    info = """
Name: Poryia Rahmani
Nickname: Poryia
Age group: Teenager / student
Location: Iran
Status: Student
Interests: Bodybuilding, computers, AI, coding
Current projects: AI assistant, VPN app
Personality: Young, ambitious, hardworking
"""
    facts = memory.get("learned_facts", {})
    if facts:
        info += "\n=== Facts ===\n"
        for k, v in facts.items():
            info += f"- {k}: {v}\n"
    return info

def voice_status_block(pref):
    if pref is None:
        return "=== VOICE ===\nOffer ONCE"
    if pref is True:
        return "=== VOICE ===\nON"
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

{build_bot_replies_section()}

{build_all_senders_section()}

{build_global_mute_section()}

{build_recent_actions_section()}

{build_custom_rules_section()}

{build_personality_section()}

=== TAGS ===
[LEARN: k {PIPE_CHAR} v] · [LEARN_RAW: t] · [FORGET: k] · [SHOW_MEMORY]
[PAUSE: target {PIPE_CHAR} min] · [PAUSE_FOREVER: target] · [UNPAUSE: target] · [UNPAUSE_ALL]
[FORWARD: target {PIPE_CHAR} n] · [FORWARD_LAST {PIPE_CHAR} n] · [CONTEXT: target]
[POPULATE_DM_SENDERS] · [WHO_LAST_DM]
[POPULATE_ALL_SENDERS] · [WHO_LAST_ALL]
[SHOW_LAST_INCOMING: target] — forward آخرین پیام کاربر
[SHOW_LAST_OUTGOING: target] — forward آخرین پاسخ ربات
[SHOW_MSG: target {PIPE_CHAR} n]
[GLOBAL_MUTE: min] · [GLOBAL_UNMUTE]
[RULE_ADD: t] · [RULE_REMOVE: i] · [RULE_LIST] · [RULE_CLEAR]
[PERSONALITY_SET: t] · [PERSONALITY_CLEAR] · [PERSONALITY_SHOW]

=== ⚠️ CRITICAL — WHO MESSAGED LAST & BOT REPLIES ===
1. Look closely at `=== DM SENDERS ===` and `=== BOT RECENT REPLIES ===` above!
2. Item #1 in `=== DM SENDERS ===` is the absolute latest person who sent a private message (DM).
3. Item #1 in `=== BOT RECENT REPLIES ===` is the latest person YOU (the bot) replied to.
4. If {OWNER_NAME} says someone else messaged more recently or says your list is outdated/wrong (e.g. "نه، سهیل آخرین نفر بود" or "دوباره چک کن"), DO NOT argue with him! Immediately output `[POPULATE_DM_SENDERS]` so the system refreshes the real-time messages from Telegram!

=== ⚠️ CRITICAL — INCOMING vs OUTGOING ===

📥 **INCOMING** = پیام کاربر (طرف مقابل)
Trigger words:
- "چی **نوشته**" (has ه at end — third person)
- "چی **گفت**"
- "**پیامش** چی بود"
- "**پیام کاربر** چی بود"
- "**چه پیامی** داد"
→ Use [SHOW_LAST_INCOMING: target]

📤 **OUTGOING** = پاسخ ربات (خودت)
Trigger words:
- "**تو** چی **نوشتی**" (has ی at end — second person)
- "**تو** چی **گفتی**"
- "**جواب تو** چی بود"
- "**پاسخ تو** چی بود"
- "**دستیار** چی گفت"
→ Use [SHOW_LAST_OUTGOING: target]

🚨 One-letter difference:
- "نوشتـه" / "گفـت" = THEM = INCOMING
- "نوشتـی" / "گفتـی" = YOU = OUTGOING

Example:
- "چی نوشته؟" → [SHOW_LAST_INCOMING: علی]
- "چی نوشتی؟" → [SHOW_LAST_OUTGOING: علی]
- "چی نوشته چی نوشتی؟" → BOTH tags on separate lines

⚠️ These tags FORWARD the actual message. Don't say "متنش اینه".

=== OTHER RULES ===

**DM vs ALL:**
- "آخرین نفری که **بهت** پیام داد" → DM (dm_senders)
- "**کلی**" / "همه چت‌هام" → ALL (all_senders)

**"بیشتر":** [SHOW_MSG: target {PIPE_CHAR} 5]

**NEVER promise without action:**
- ❌ "الان می‌فرستم" (بدون tag)
- ✅ "باشه 📨\n[FORWARD_LAST {PIPE_CHAR} 1]"

**User says "اره"/"باشه" after offer → DO IT with tag**

**Only save memory when "یادت باشه..." / "فراموش کن..."**

**General question → JUST ANSWER, no tag.**

**DON'T repeat actions from RECENT ACTIONS**

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

{build_custom_rules_section()}

=== Rules ===
- Same language as user
- Introduce as "{OWNER_NAME}'s AI assistant"
- Never pretend to be them
- Keep short (1-3 sentences)

=== ⚠️⚠️⚠️ MANDATORY — TAG AT END OF EVERY REPLY ⚠️⚠️⚠️ ===

EVERY SINGLE REPLY must end with EXACTLY ONE tag on its own line.
This is NOT optional. The system depends on it.
NEVER skip this tag.

[IMPORTANT] — use when ANY of:
- User is stressed, sad, angry, emotional
- User asks to meet/call/see {OWNER_NAME}
- Contains "فوری" / "اورژانس" / "مهم" / "ضروری" / "زود"
- Business, money, family, health, school issues
- User wants {OWNER_NAME} specifically
- Waiting for response / urgent
- Negative feedback or complaint

[NORMAL] — everything else:
- Casual chat, greetings, jokes
- General knowledge questions
- Information that doesn't need {OWNER_NAME}

Examples:
- "سلام، خوبی؟" → "سلام! 😊 خوبم..." + [NORMAL]
- "پوریا کجاست؟" → "الان نیست..." + [IMPORTANT]
- "فوری زنگ بزن" → "باشه بهش می‌گم..." + [IMPORTANT]
- "پایتخت فرانسه؟" → "پاریس 🇫🇷" + [NORMAL]

=== TEACHING ===
Only {OWNER_NAME} can teach. Voice request ≠ teaching.

{vblock}

=== CONVERSATION ===
{chr(10).join(hist)}
You:"""
    return prompt

async def ask_gemini(prompt, chat_id=None, with_progress=False, progress_initial=None):
    progress = None
    if with_progress and chat_id:
        progress = ProgressReporter(chat_id, progress_initial, interval=12)
        progress.start()
    try:
        started = time.time()
        round_num = 0
        while True:
            round_num += 1
            for model_name in MODELS:
                try:
                    result = await ai.aio.models.generate_content(model=model_name, contents=prompt)
                    raw = (result.text or "").strip()
                    if raw:
                        log(f"✅ {model_name} (r{round_num}, {int(time.time()-started)}s)")
                        return raw, None
                except Exception as e:
                    es = str(e)
                    if "429" in es or "RESOURCE_EXHAUSTED" in es:
                        log(f"⏸️ {model_name}: quota")
                    elif "503" in es:
                        log(f"⏳ {model_name}: busy")
                    elif "404" in es:
                        log(f"❌ {model_name}: not found")
                    else:
                        log(f"❌ {model_name}: {es[:80]}")
                    continue
            if time.time() - started > runtime_settings.get("max_retry_time", 300):
                return None, None
            delay = RETRY_DELAYS[min(round_num - 1, len(RETRY_DELAYS) - 1)]
            if chat_id:
                try:
                    await client.action(chat_id, "typing")
                except:
                    pass
            await asyncio.sleep(delay)
    finally:
        if progress:
            await progress.stop()

async def send_alert(text):
    if not NTFY_TOPIC:
        log("⚠️ send_alert: NTFY_TOPIC not set — SKIPPING!")
        return
    try:
        loop = asyncio.get_event_loop()
        r = await loop.run_in_executor(None, lambda: requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=text.encode('utf-8'),
            headers={"Title": "Important", "Priority": "urgent", "Tags": "rotating_light,warning"},
            timeout=10
        ))
        log(f"🔔 ntfy sent (status: {r.status_code})")
    except Exception as e:
        log(f"⚠️ ntfy error: {str(e)[:100]}")

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
                resp = await ai.aio.models.generate_content(
                    model=mn,
                    contents=[types.Content(parts=[
                        types.Part.from_bytes(data=audio, mime_type="audio/ogg"),
                        types.Part.from_text(text="این فایل صوتی رو دقیقاً به همون زبونی که صحبت شده، رونویسی کن. فقط متن رو بده.")
                    ])]
                )
                t = (resp.text or "").strip()
                if t:
                    return t
            except:
                continue
        if time.time() - started > 300:
            return None
        await asyncio.sleep(RETRY_DELAYS[min(rn-1, len(RETRY_DELAYS)-1)])

async def handle_voice_input(event):
    global last_bot_send_time
    try:
        last_bot_send_time = time.time()
        bot_sending_to_chat[event.chat_id] = time.time()
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
        res = subprocess.run(
            ["ffmpeg", "-y", "-i", mp3, "-c:a", "libopus", "-b:a", "48k",
             "-vbr", "on", "-compression_level", "10", "-frame_duration", "60",
             "-application", "voip", ogg],
            capture_output=True, timeout=30
        )
        try:
            os.unlink(mp3)
        except:
            pass
        return ogg if res.returncode == 0 else None
    except:
        return None

async def send_voice_reply(chat_id, reply_to_id, text):
    global last_bot_send_time
    if not runtime_settings.get("voice_output", True):
        return
    vp = await text_to_voice(text)
    if not vp:
        return
    try:
        last_bot_send_time = time.time()
        bot_sending_to_chat[chat_id] = time.time()
        await client.send_file(chat_id, vp, voice_note=True, reply_to=reply_to_id)
    except:
        pass
    finally:
        try:
            os.unlink(vp)
        except:
            pass

async def safe_reply(event, text):
    global last_bot_send_time
    last_bot_send_time = time.time()
    bot_sending_to_chat[event.chat_id] = time.time()
    return await event.reply(text)


client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)

# ============ REST API ============
CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "X-API-Key, Content-Type",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS"
}

def auth(req):
    return (req.headers.get("X-API-Key") or req.query.get("api_key")) == API_KEY

def jr(data, status=200):
    return web.json_response(data, status=status, headers=CORS)

async def opts(req):
    return web.Response(headers=CORS)

async def api_ping(req):
    return jr({"pong": True, "time": time.time()})

async def api_status(req):
    if not auth(req):
        return jr({"error": "Unauthorized"}, 401)
    try:
        me = await client.get_me()
        return jr({
            "online": True,
            "user_id": me.id,
            "username": me.username,
            "first_name": me.first_name,
            "uptime_seconds": int(time.time() - started_at),
            "facts_count": len(memory.get("learned_facts", {})),
            "paused_chats_count": sum(1 for cid in owner_last_seen if is_owner_paused(cid)),
            "globally_muted": is_globally_muted(),
            "custom_rules_count": len(memory.get("custom_rules", [])),
            "personality_override": memory.get("personality_override", ""),
            "dm_senders": dm_senders[:10],
            "all_senders": all_senders[:10],
            "settings": runtime_settings
        })
    except Exception as e:
        return jr({"error": str(e)}, 500)

async def api_settings_get(req):
    if not auth(req):
        return jr({"error": "Unauthorized"}, 401)
    return jr({"settings": runtime_settings})

async def api_settings_post(req):
    if not auth(req):
        return jr({"error": "Unauthorized"}, 401)
    try:
        data = await req.json()
    except:
        return jr({"error": "Invalid"}, 400)
    allowed = {"pause_enabled", "pause_duration", "voice_output", "voice_input", "voice_name", "alert_delay", "max_retry_time"}
    upd = {k: v for k, v in data.items() if k in allowed}
    runtime_settings.update(upd)
    return jr({"ok": True, "updated": upd, "settings": runtime_settings})

async def api_paused(req):
    if not auth(req):
        return jr({"error": "Unauthorized"}, 401)
    out = []
    for cid in list(owner_last_seen.keys()):
        rem = pause_remaining(cid)
        if rem > 0:
            name = chat_names_cache.get(cid)
            if not name or str(name).isdigit():
                try:
                    e = await client.get_entity(cid)
                    name = getattr(e, "first_name", None) or getattr(e, "title", None) or str(cid)
                    chat_names_cache[cid] = name
                except:
                    pass
            out.append({"chat_id": cid, "name": name, "remaining_seconds": rem})
    out.sort(key=lambda x: x["remaining_seconds"])
    return jr({"paused": out})

async def api_pause(req):
    if not auth(req):
        return jr({"error": "Unauthorized"}, 401)
    try:
        data = await req.json()
        cid = int(data["chat_id"])
        dur = data.get("duration")
    except:
        return jr({"error": "chat_id required"}, 400)
    owner_last_seen[cid] = time.time()
    if dur:
        custom_pause_durations[cid] = int(dur)
    return jr({"ok": True})

async def api_resume(req):
    if not auth(req):
        return jr({"error": "Unauthorized"}, 401)
    try:
        data = await req.json()
        cid = int(data["chat_id"])
    except:
        return jr({"error": "chat_id required"}, 400)
    owner_last_seen.pop(cid, None)
    custom_pause_durations.pop(cid, None)
    return jr({"ok": True})

async def api_resume_all(req):
    if not auth(req):
        return jr({"error": "Unauthorized"}, 401)
    c = len(owner_last_seen)
    owner_last_seen.clear()
    custom_pause_durations.clear()
    return jr({"ok": True, "cleared": c})

async def api_logs(req):
    if not auth(req):
        return jr({"error": "Unauthorized"}, 401)
    return jr({"logs": list(recent_logs)})

async def api_facts(req):
    if not auth(req):
        return jr({"error": "Unauthorized"}, 401)
    return jr({
        "facts": memory.get("learned_facts", {}),
        "notes": memory.get("owner_notes", []),
        "rules": memory.get("custom_rules", []),
        "personality": memory.get("personality_override", "")
    })

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

# ============ Owner handlers ============
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
    if time.time() - bot_sending_to_chat.get(event.chat_id, 0) < 5.0:
        return

    mark_owner_active(event.chat_id)
    rem = pause_remaining(event.chat_id)
    log(f"🛑 Owner manual activity -> Paused {event.chat_id} for {rem//60}m")


@client.on(events.NewMessage(outgoing=True))
async def on_owner_message(event):
    global global_mute_until, last_bot_send_time

    try:
        me = await client.get_me()
    except:
        return
    if event.chat_id != me.id or event.sender_id != me.id:
        return
    if time.time() - last_bot_send_time < 1.5:
        return
    if getattr(event.message, 'forward', None):
        return

    text = event.message.text or ""

    if text and ALERT_PREFIX in text:
        log(f"⏭️ Skipping alert message (not processing with AI)")
        return

    if not text.strip() and event.message.voice:
        if not runtime_settings.get("voice_input", True):
            await safe_reply(event, "🚫 ویس غیرفعال")
            return
        text = await handle_voice_input(event) or ""
        if not text:
            await safe_reply(event, "نتونستم ویس رو بفهمم 😅")
            return
        await safe_reply(event, f"🎤 «{text}»")

    if not text.strip():
        return
    log(f"🔍 Owner: {text[:50]!r}")

    prompt = build_prompt("owner", text, is_owner=True)
    raw, _ = await ask_gemini(prompt, chat_id=event.chat_id, with_progress=True)
    if raw is None:
        await safe_reply(event, "❌ متأسفم، الان نشد. دوباره بفرست 🙏")
        return

    # ===== SHOW_LAST_INCOMING + SHOW_LAST_OUTGOING =====
    in_target = extract_tag_content(raw, "SHOW_LAST_INCOMING:")
    out_target = extract_tag_content(raw, "SHOW_LAST_OUTGOING:")
    if in_target is not None or out_target is not None:
        target = in_target if in_target is not None else out_target
        cid = await resolve_chat_id(target)
        if cid is None:
            await safe_reply(event, f"❓ «{target}» پیدا نشد")
            return
        
        name = await cache_chat_name(cid, force=True)
        progress = ProgressReporter(event.chat_id, f"⏳ دارم پیام‌های {name} رو فوروارد می‌کنم...", interval=10)
        progress.start()
        forwarded_parts = []
        try:
            if in_target is not None:
                ok = await forward_last_incoming(cid)
                if ok:
                    forwarded_parts.append("📩 پیام کاربر")
            if out_target is not None:
                ok = await forward_last_outgoing(cid)
                if ok:
                    forwarded_parts.append("🤖 پاسخ ربات")
        finally:
            await progress.stop()
        
        if forwarded_parts:
            await safe_reply(event, f"⬆️ فوروارد شد از **{name}**:\n" + "\n".join(forwarded_parts))
        else:
            await safe_reply(event, f"❓ چیزی برای فوروارد پیدا نشد")
        return

    # ===== POPULATE_DM_SENDERS =====
    if "[POPULATE_DM_SENDERS]" in raw:
        progress = ProgressReporter(event.chat_id, "⏳ دارم چت‌های خصوصیت رو زنده از تلگرام می‌خونم...", interval=10)
        progress.start()
        try:
            count = await populate_senders_from_telegram(include_all=False)
            save_senders_if_dirty(force=True)
        finally:
            await progress.stop()
        if count == 0:
            await safe_reply(event, "❓ DM با پیام دریافتی پیدا نکردم")
        else:
            prompt2 = build_prompt("owner", text + " [لیست DM ها از تلگرام آپدیت شد، حالا دقیق جواب بده]", is_owner=True)
            raw2, _ = await ask_gemini(prompt2, chat_id=event.chat_id, with_progress=True)
            if raw2:
                r = strip_all_system_tags(raw2)
                if r:
                    histories.setdefault("owner", []).append(f"You: {r}")
                    await safe_reply(event, r)
                    return
            if dm_senders:
                last = dm_senders[0]
                ago_str = format_ago(time.time() - last["timestamp"])
                await safe_reply(event, f"📨 آخرین DM: **{last['name']}** — {ago_str}")
        return

    # ===== POPULATE_ALL_SENDERS =====
    if "[POPULATE_ALL_SENDERS]" in raw:
        progress = ProgressReporter(event.chat_id, "⏳ دارم همه چت‌ها رو از تلگرام می‌خونم...", interval=10)
        progress.start()
        try:
            count = await populate_senders_from_telegram(include_all=True)
            save_senders_if_dirty(force=True)
        finally:
            await progress.stop()
        if count == 0:
            await safe_reply(event, "❓ پیام دریافتی پیدا نکردم")
        else:
            prompt2 = build_prompt("owner", text + " [لیست همه چت‌ها از تلگرام آپدیت شد، حالا دقیق جواب بده]", is_owner=True)
            raw2, _ = await ask_gemini(prompt2, chat_id=event.chat_id, with_progress=True)
            if raw2:
                r = strip_all_system_tags(raw2)
                if r:
                    histories.setdefault("owner", []).append(f"You: {r}")
                    await safe_reply(event, r)
                    return
            if all_senders:
                last = all_senders[0]
                ago_str = format_ago(time.time() - last["timestamp"])
                kind = "bot" if last.get("is_bot") else ("group" if last.get("is_group") else "DM")
                await safe_reply(event, f"🌐 کلی، آخرین پیام از **{last['name']}** ({kind}) — {ago_str}")
        return

    # ===== WHO_LAST_DM =====
    if "[WHO_LAST_DM]" in raw:
        if not dm_senders:
            progress = ProgressReporter(event.chat_id, "⏳ دارم DM ها رو چک می‌کنم...", interval=10)
            progress.start()
            try:
                await populate_senders_from_telegram(include_all=False)
                save_senders_if_dirty(force=True)
            finally:
                await progress.stop()
        if not dm_senders:
            await safe_reply(event, "❓ DM پیدا نشد")
            return
        r = strip_all_system_tags(raw)
        if r:
            histories.setdefault("owner", []).append(f"You: {r}")
            await safe_reply(event, r)
        else:
            last = dm_senders[0]
            ago_str = format_ago(time.time() - last["timestamp"])
            await safe_reply(event, f"📨 **{last['name']}** — {ago_str}")
        return

    # ===== WHO_LAST_ALL =====
    if "[WHO_LAST_ALL]" in raw:
        if not all_senders:
            progress = ProgressReporter(event.chat_id, "⏳ دارم همه چت‌ها رو چک می‌کنم...", interval=10)
            progress.start()
            try:
                await populate_senders_from_telegram(include_all=True)
                save_senders_if_dirty(force=True)
            finally:
                await progress.stop()
        if not all_senders:
            await safe_reply(event, "❓ پیام پیدا نشد")
            return
        r = strip_all_system_tags(raw)
        if r:
            histories.setdefault("owner", []).append(f"You: {r}")
            await safe_reply(event, r)
        else:
            last = all_senders[0]
            ago_str = format_ago(time.time() - last["timestamp"])
            kind = "bot" if last.get("is_bot") else ("group" if last.get("is_group") else "DM")
            await safe_reply(event, f"🌐 **{last['name']}** ({kind}) — {ago_str}")
        return

    # ===== SHOW_MSG =====
    show_msg_inner = extract_tag_content(raw, "SHOW_MSG:")
    if show_msg_inner is not None:
        parts = show_msg_inner.split(PIPE_CHAR)
        target = parts[0].strip()
        count = 5
        if len(parts) > 1 and parts[1].strip().isdigit():
            count = min(int(parts[1].strip()), 20)
        cid = await resolve_chat_id(target)
        if cid is None:
            await safe_reply(event, f"❓ «{target}» پیدا نشد")
            return
        name = await cache_chat_name(cid, force=True)
        progress = ProgressReporter(event.chat_id, f"⏳ دارم {count} پیام آخر {name} رو می‌گیرم...", interval=10)
        progress.start()
        try:
            content = await get_last_messages_text(cid, count)
        finally:
            await progress.stop()
        await safe_reply(event, f"📜 **{name}** — {count} پیام آخر:\n\n{content}")
        return

    # ===== UNPAUSE_ALL =====
    if "[UNPAUSE_ALL]" in raw:
        c = len(owner_last_seen)
        owner_last_seen.clear()
        custom_pause_durations.clear()
        track_action("UNPAUSE_ALL", c)
        r = strip_all_system_tags(raw)
        if not r:
            r = f"✅ همه {c} چت آزاد شدن"
        await safe_reply(event, r)
        return

    # ===== UNPAUSE =====
    unpause_target = extract_tag_content(raw, "UNPAUSE:")
    if unpause_target is not None:
        cid = await resolve_chat_id(unpause_target)
        if cid is None:
            await safe_reply(event, f"❓ «{unpause_target}» پیدا نشد")
            return
        name = await cache_chat_name(cid, force=True)
        if cid in owner_last_seen:
            del owner_last_seen[cid]
            custom_pause_durations.pop(cid, None)
            track_action("UNPAUSE", cid, name)
            r = strip_all_system_tags(raw)
            if not r:
                r = f"✅ «{name}» آزاد شد"
        else:
            r = f"ℹ️ «{name}» از قبل آزاده"
        await safe_reply(event, r)
        return

    # ===== PAUSE_FOREVER =====
    pause_forever_target = extract_tag_content(raw, "PAUSE_FOREVER:")
    if pause_forever_target is not None:
        cid = await resolve_chat_id(pause_forever_target)
        if cid:
            owner_last_seen[cid] = time.time()
            custom_pause_durations[cid] = 10 * 365 * 24 * 3600
            name = await cache_chat_name(cid, force=True)
            track_action("PAUSE_FOREVER", cid, name)
            r = strip_all_system_tags(raw)
            if not r:
                r = f"🛑 «{name}» دائمی"
            await safe_reply(event, r)
        else:
            await safe_reply(event, f"❓ «{pause_forever_target}» پیدا نشد")
        return

    # ===== PAUSE =====
    pause_inner = extract_tag_content(raw, "PAUSE:")
    if pause_inner is not None:
        parts = pause_inner.split(PIPE_CHAR)
        target = parts[0].strip()
        mins = runtime_settings["pause_duration"] // 60
        if len(parts) > 1 and parts[1].strip().isdigit():
            mins = int(parts[1].strip())
        cid = await resolve_chat_id(target)
        if cid:
            owner_last_seen[cid] = time.time()
            custom_pause_durations[cid] = mins * 60
            name = await cache_chat_name(cid, force=True)
            track_action("PAUSE", cid, f"{name} {mins}m")
            r = strip_all_system_tags(raw)
            if not r:
                r = f"🛑 «{name}» {mins}د"
            await safe_reply(event, r)
        else:
            await safe_reply(event, f"❓ «{target}» پیدا نشد")
        return

    # ===== FORWARD_LAST =====
    fwd_last_inner = extract_tag_content(raw, "FORWARD_LAST")
    if fwd_last_inner is not None:
        count = 1
        cleaned_num = fwd_last_inner.replace(PIPE_CHAR, "").replace(":", "").strip()
        if cleaned_num.isdigit():
            count = min(int(cleaned_num), 20)
        if not dm_senders:
            progress = ProgressReporter(event.chat_id, "⏳ دارم دنبال آخرین DM می‌گردم...", interval=10)
            progress.start()
            try:
                await populate_senders_from_telegram(include_all=False)
                save_senders_if_dirty(force=True)
            finally:
                await progress.stop()
        if not dm_senders:
            await safe_reply(event, "❓ DM نبود")
            return
        last = dm_senders[0]
        progress = ProgressReporter(event.chat_id, f"⏳ دارم {count} پیام از {last['name']} فوروارد می‌کنم...", interval=10)
        progress.start()
        try:
            fwd, err = await forward_last_messages(last["chat_id"], count)
        finally:
            await progress.stop()
        if err:
            await safe_reply(event, f"❌ {err}")
        else:
            track_action("FORWARD_LAST", last["chat_id"], last["name"])
            r = strip_all_system_tags(raw)
            if not r:
                r = f"📨 {fwd} پیام از «{last['name']}» فوروارد شد"
            await safe_reply(event, r)
        return

    # ===== FORWARD =====
    fwd_inner = extract_tag_content(raw, "FORWARD:")
    if fwd_inner is not None:
        parts = fwd_inner.split(PIPE_CHAR)
        target = parts[0].strip()
        count = 1
        if len(parts) > 1 and parts[1].strip().isdigit():
            count = min(int(parts[1].strip()), 20)
        cid = await resolve_chat_id(target)
        if cid:
            name = await cache_chat_name(cid, force=True)
            progress = ProgressReporter(event.chat_id, f"⏳ دارم {count} پیام از {name} فوروارد می‌کنم...", interval=10)
            progress.start()
            try:
                fwd, err = await forward_last_messages(cid, count)
            finally:
                await progress.stop()
            if err:
                await safe_reply(event, f"❌ {err}")
            else:
                track_action("FORWARD", cid, f"{name} {fwd}")
                r = strip_all_system_tags(raw)
                if not r:
                    r = f"📨 {fwd} پیام از «{name}» فوروارد شد"
                await safe_reply(event, r)
        else:
            await safe_reply(event, f"❓ «{target}» پیدا نشد")
        return

    # ===== CONTEXT =====
    ctx_target = extract_tag_content(raw, "CONTEXT:")
    if ctx_target is not None:
        cid = await resolve_chat_id(ctx_target)
        if cid:
            name = await cache_chat_name(cid, force=True)
            progress = ProgressReporter(event.chat_id, f"⏳ دارم پیام‌های {name} رو می‌گیرم...", interval=10)
            progress.start()
            try:
                ctx = await get_last_messages_text(cid, 5)
            finally:
                await progress.stop()
            await safe_reply(event, f"👁️ {name}:\n\n{ctx}")
        else:
            await safe_reply(event, f"❓ «{ctx_target}» پیدا نشد")
        return

    # ===== GLOBAL_MUTE =====
    mute_inner = extract_tag_content(raw, "GLOBAL_MUTE:")
    if mute_inner is not None and mute_inner.isdigit():
        mins = int(mute_inner)
        global_mute_until = time.time() + mins * 60
        track_action("GLOBAL_MUTE", f"{mins}m")
        r = strip_all_system_tags(raw)
        if not r:
            r = f"🔇 {mins} دقیقه ساکت شدم"
        await safe_reply(event, r)
        return

    # ===== GLOBAL_UNMUTE =====
    if "[GLOBAL_UNMUTE]" in raw:
        global_mute_until = 0
        r = strip_all_system_tags(raw)
        if not r:
            r = "🔊 آنمیوت شدم"
        await safe_reply(event, r)
        return

    # ===== Rules =====
    rule_add_inner = extract_tag_content(raw, "RULE_ADD:")
    if rule_add_inner is not None:
        memory.setdefault("custom_rules", [])
        if rule_add_inner not in memory["custom_rules"]:
            memory["custom_rules"].append(rule_add_inner)
            save_memory_async()
            track_action("RULE_ADD", rule_add_inner[:40])
        r = strip_all_system_tags(raw)
        if not r:
            r = f"✅ قانون #{len(memory['custom_rules'])}\n📏 {rule_add_inner}"
        await safe_reply(event, r)
        return

    rule_rem_inner = extract_tag_content(raw, "RULE_REMOVE:")
    if rule_rem_inner is not None and rule_rem_inner.isdigit():
        idx = int(rule_rem_inner) - 1
        rules = memory.get("custom_rules", [])
        if 0 <= idx < len(rules):
            removed = rules.pop(idx)
            save_memory_async()
            r = strip_all_system_tags(raw)
            if not r:
                r = f"🗑️ {removed}"
            await safe_reply(event, r)
        else:
            await safe_reply(event, f"❓ #{idx+1} نیست")
        return

    if "[RULE_LIST]" in raw:
        rules = memory.get("custom_rules", [])
        if not rules:
            await safe_reply(event, "📏 هیچ قانونی نیست")
            return
        await safe_reply(event, "📏 **قوانین:**\n" + "\n".join(f"{i}. {r}" for i, r in enumerate(rules, 1)))
        return

    if "[RULE_CLEAR]" in raw:
        c = len(memory.get("custom_rules", []))
        memory["custom_rules"] = []
        save_memory_async()
        r = strip_all_system_tags(raw)
        if not r:
            r = f"🧹 {c} قانون پاک شد"
        await safe_reply(event, r)
        return

    pers_inner = extract_tag_content(raw, "PERSONALITY_SET:")
    if pers_inner is not None:
        memory["personality_override"] = pers_inner
        save_memory_async()
        r = strip_all_system_tags(raw)
        if not r:
            r = f"🎭 شخصیت تغییر کرد:\n{pers_inner}"
        await safe_reply(event, r)
        return

    if "[PERSONALITY_CLEAR]" in raw:
        memory["personality_override"] = ""
        save_memory_async()
        r = strip_all_system_tags(raw)
        if not r:
            r = "🎭 پیش‌فرض"
        await safe_reply(event, r)
        return

    # ===== Memory =====
    enable_voice = "[ENABLE_VOICE]" in raw
    disable_voice = "[DISABLE_VOICE]" in raw

    learn_inner = extract_tag_content(raw, "LEARN:")
    if learn_inner is not None and PIPE_CHAR in learn_inner:
        k, v = learn_inner.split(PIPE_CHAR, 1)
        memory.setdefault("learned_facts", {})[k.strip()] = v.strip()
        save_memory_async()
        await safe_reply(event, f"✅ گرفتم: {k.strip()} → {v.strip()}")
        return

    learn_raw_inner = extract_tag_content(raw, "LEARN_RAW:")
    if learn_raw_inner is not None:
        fid = f"fact_{len(memory.get('learned_facts', {})) + 1}"
        memory.setdefault("learned_facts", {})[fid] = learn_raw_inner
        save_memory_async()
        await safe_reply(event, f"✅ گرفتم: {learn_raw_inner}")
        return

    forget_inner = extract_tag_content(raw, "FORGET:")
    if forget_inner is not None:
        facts = memory.get("learned_facts", {})
        if forget_inner in facts:
            del facts[forget_inner]
            save_memory_async()
            await safe_reply(event, f"🗑️ فراموش: {forget_inner}")
        else:
            await safe_reply(event, f"❓ {forget_inner} نبود")
        return

    if "[SHOW_MEMORY]" in raw:
        facts = memory.get("learned_facts", {})
        r = "🧠 **حافظه:**\n\n"
        if facts:
            for k, v in facts.items():
                r += f"• {k}: {v}\n"
        else:
            r += "خالی\n"
        await safe_reply(event, r)
        return

    if enable_voice:
        set_voice_pref("owner", True)
    elif disable_voice:
        set_voice_pref("owner", False)

    reply = strip_all_system_tags(raw)
    if not reply:
        return

    histories.setdefault("owner", []).append(f"You: {reply}")
    await safe_reply(event, reply)

    if get_voice_pref("owner") is True and not disable_voice:
        await send_voice_reply(event.chat_id, event.message.id, reply)


# ============ Incoming handler ============
@client.on(events.NewMessage(incoming=True))
async def on_incoming(event):
    try:
        sender = await event.get_sender()
    except:
        sender = None
    if sender is None or sender.id == OWNER_ID or sender.id in IGNORED_USER_IDS:
        return

    msg_ts = event.message.date.timestamp() if getattr(event.message, "date", None) else time.time()

    if event.is_private:
        is_bot = getattr(sender, "bot", False)
        sname = getattr(sender, "first_name", "") or getattr(sender, "username", "") or str(sender.id)
        track_sender(sender.id, sname, is_bot=is_bot, is_group=False, ts=msg_ts)
        save_senders_if_dirty()
        if is_bot:
            return
    else:
        title = getattr(event.chat, "title", None) or str(event.chat_id)
        track_sender(event.chat_id, title, is_bot=False, is_group=True, ts=msg_ts)
        save_senders_if_dirty()
        return

    if is_globally_muted():
        return
    if is_owner_paused(event.chat_id):
        log(f"⏸️ Chat {event.chat_id} ({sname}) is paused, skipping AI reply")
        return

    text = event.message.text or ""

    if not text.strip() and event.message.voice:
        if not runtime_settings.get("voice_input", True):
            return
        text = await handle_voice_input(event) or ""
        if not text:
            await safe_reply(event, "نتونستم ویس رو بفهمم 😅")
            return

    if not text.strip():
        return

    user_key = str(sender.id)
    log(f"🔍 User {user_key} ({sname}): {text[:50]!r}")

    prompt = build_prompt(sender.id, text, is_owner=False)
    raw, _ = await ask_gemini(prompt, chat_id=event.chat_id)
    if raw is None:
        return

    important = "[IMPORTANT]" in raw
    enable_voice = "[ENABLE_VOICE]" in raw
    disable_voice = "[DISABLE_VOICE]" in raw

    if not important and "[NORMAL]" not in raw:
        urgent_kw = ["فوری", "اورژانس", "زنگ بزن", "مهم", "ضروری", "خودت بیا", "خودت جواب", "تماس بگیر"]
        if any(kw in text for kw in urgent_kw):
            important = True
            log(f"⚠️ No tag but urgent keyword → treating as IMPORTANT")

    if enable_voice:
        set_voice_pref(user_key, True)
    elif disable_voice:
        set_voice_pref(user_key, False)

    reply = strip_all_system_tags(raw)

    if important:
        try:
            sender_name = getattr(sender, "first_name", "") or getattr(sender, "username", "") or str(sender.id)
            uname = f"@{sender.username}" if sender.username else "—"
            alert_text = (f"{ALERT_PREFIX}\n\n"
                          f"👤 {sender_name} ({uname})\n"
                          f"🆔 `{sender.id}`\n\n"
                          f"📩 {text}\n\n"
                          f"🤖 {reply or '(بدون جواب)'}")
            log(f"🚨 IMPORTANT from {sender.id} — sending silent alert...")
            await client.send_message("me", alert_text, silent=True)
            log(f"📩 Silent alert sent to Saved Messages")
            
            async def delayed_ntfy():
                await asyncio.sleep(runtime_settings["alert_delay"])
                await send_alert(alert_text)
            asyncio.create_task(delayed_ntfy())
            log(f"⏰ ntfy alert scheduled in {runtime_settings['alert_delay']}s")
        except Exception as e:
            log(f"⚠️ Alert error: {str(e)[:150]}")

    if not reply:
        return

    histories.setdefault(user_key, []).append(f"You: {reply}")
    bot_last_replies[event.chat_id] = {
        "name": sname,
        "text": reply,
        "user_text": text,
        "ts": time.time()
    }

    await safe_reply(event, reply)

    if get_voice_pref(user_key) is True and not disable_voice:
        await send_voice_reply(event.chat_id, event.message.id, reply)


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
    log("🔄 Syncing recent senders from Telegram on startup...")
    await populate_senders_from_telegram(limit=20, include_all=True)
    save_senders_if_dirty(force=True)
    log("🚀 Running...")
    log(f"📥 DM: {len(dm_senders)} | ALL: {len(all_senders)}")
    log(f"🚨 Alert system: prefix='{ALERT_PREFIX}', delay={runtime_settings['alert_delay']}s")
    if not NTFY_TOPIC:
        log("⚠️⚠️⚠️ NTFY_TOPIC NOT SET — alerts won't be sent to phone!")
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
