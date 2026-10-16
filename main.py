import os
import json
import base64
import asyncio
import requests
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from google import genai

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
SESSION_STRING = os.environ["SESSION_STRING"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
OWNER_ID = int(os.environ["OWNER_ID"])
OWNER_NAME = os.environ.get("OWNER_NAME", "Poryia")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")

# GitHub memory
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
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
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
    if not GITHUB_TOKEN:
        print("⚠️ No GITHUB_TOKEN — using empty memory")
        return {"conversations": {}, "learned_facts": {}, "owner_notes": []}, None
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{MEMORY_PATH}"
    try:
        r = requests.get(url, headers=github_headers(), params={"ref": GITHUB_BRANCH}, timeout=10)
        if r.status_code == 200:
            data = r.json()
            content = base64.b64decode(data["content"]).decode("utf-8")
            return json.loads(content), data["sha"]
        elif r.status_code == 404:
            print("ℹ️ memory.json not found on GitHub — starting fresh")
            return {"conversations": {}, "learned_facts": {}, "owner_notes": []}, None
        else:
            print(f"⚠️ GitHub fetch error: {r.status_code} {r.text[:200]}")
    except Exception as e:
        print(f"⚠️ GitHub fetch exception: {e}")
    return {"conversations": {}, "learned_facts": {}, "owner_notes": []}, None

def push_memory(mem):
    if not GITHUB_TOKEN:
        return
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{MEMORY_PATH}"
    _, sha = fetch_memory()
    content = base64.b64encode(json.dumps(mem, ensure_ascii=False, indent=2).encode("utf-8")).decode("utf-8")
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
histories = memory.get("conversations", {})
MAX_HISTORY = 12

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

def build_prompt(user_id, new_message, is_owner=False):
    hist = histories.setdefault(str(user_id), [])
    hist.append(f"User: {new_message}")
    del hist[:-MAX_HISTORY]
    if is_owner:
        prompt = f"""You are the personal AI assistant of {OWNER_NAME}.
You are talking to {OWNER_NAME} THEMSELVES in their Saved Messages.
This is your boss. Be warm, friendly, casual — like a close friend.

=== About your boss ===
{build_owner_info()}

=== SPECIAL POWERS (VERY IMPORTANT) ===
You can secretly modify your own memory. When you want to do that,
output ONLY the tag on a single line, nothing else.

Tags you can use:
[LEARN: key | value]      — remember a specific fact
[LEARN_RAW: text]         — remember a general fact without a key
[FORGET: key]             — forget a fact
[SHOW_MEMORY]             — show the owner everything you remember

⚠️ CRITICAL: Be VERY conservative about saving. Only save when the owner
EXPLICITLY and CLEARLY asks you to remember something.

DO save if the owner says things like:
- "یادت باشه..." / "به خاطر بسپار..." / "اینو یادت بمونه..."
- "remember that..." / "keep in mind..."
- "فراموش کن..." / "پاکش کن..." (for FORGET)
- "چی یادت هست؟" / "چی از من میدونی؟" (for SHOW_MEMORY)

DO NOT save in these cases (just chat normally):
- Normal conversation, greetings, questions, jokes
- The owner asking YOU questions (don't save their questions as facts)
- The owner telling you about their day in passing
- The owner asking for advice or opinions
- Small talk, casual chat, or emotional conversations
- Anything that isn't explicitly a "remember this" request

When in doubt, DO NOT save. Just reply as a friend.

Examples:
Owner: سلام، خوبی؟
You: سلام خودم! 😄 خوبم، تو چطوری؟ چه خبر از باشگاه؟
(NO tags — just chat)

Owner: امروز رفتم باشگاه، خیلی خسته‌ام
You: آفرین 💪 خستگی بعد از تمرین بهترین حس دنیاست. استراحت کن.
(NO tags — just chat)

Owner: چه چیزایی میخوای درباره من بدونی؟
You: هر چی دوست داری بهم بگی 😊 مثلاً علاقه‌مندی‌هات، دوستات، یا هر چیز دیگه‌ای که فکر می‌کنی به بهتر جواب دادن کمک می‌کنه.
(NO tags — this is a question, not a save request)

Owner: یادت باشه اسم بهترین دوستم علیه
You: [LEARN: best_friend | Ali]

Owner: راستی یادت باشه من یه پروژه VPN دارم
You: [LEARN_RAW: Poryia has a personal VPN project]

Owner: اون چیز درباره بهترین دوستمو فراموش کن
You: [FORGET: best_friend]

Owner: چی یادت هست درباره من؟
You: [SHOW_MEMORY]

=== Current conversation ===
{chr(10).join(hist)}
You:"""
    else:
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
                print(f"✅ Response from {model_name}")
                return raw, None
            except Exception as e:
                last_error = e
                err_str = str(e)
                if "503" in err_str or "UNAVAILABLE" in err_str:
                    print(f"⏳ {model_name} busy, trying next...")
                else:
                    print(f"❌ Gemini error with {model_name}:", err_str[:150])
                continue
        if attempt == 0:
            print("⏸️ All models busy — waiting 3s before retry...")
            await asyncio.sleep(3)
    return None, last_error

async def send_alert(alert_text):
    """ارسال آلارم فوری از طریق ntfy.sh"""
    if not NTFY_TOPIC:
        print("⚠️ NTFY_TOPIC تنظیم نشده — آلارم فرستاده نشد")
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
        print("🔔 آلارم از طریق ntfy.sh فرستاده شد")
    except Exception as e:
        print(f"⚠️ خطای ntfy: {e}")

client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)

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
    print(f"🔍 Owner message: {text[:50]!r}")
    if not text.strip():
        return
    prompt = build_prompt("owner", text, is_owner=True)
    raw, last_error = await ask_gemini(prompt)
    if raw is None:
        await event.reply("اوه، الان سرورای گوگل شلوغن 😅 یه دقیقه دیگه دوباره بفرست.")
        return

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

    reply = raw.replace("[IMPORTANT]", "").replace("[NORMAL]", "").strip()
    if not reply:
        return
    histories.setdefault("owner", []).append(f"You: {reply}")
    memory["conversations"] = histories
    save_memory_async()
    await event.reply(reply)

@client.on(events.NewMessage(incoming=True, func=lambda e: e.is_private))
async def on_dm(event):
    sender = await event.get_sender()
    if sender.bot or sender.id == OWNER_ID:
        return
    text = event.message.text or ""
    if not text.strip():
        return
    prompt = build_prompt(sender.id, text, is_owner=False)
    raw, last_error = await ask_gemini(prompt)
    if raw is None:
        print("All models failed. Last error:", last_error)
        await event.reply("الان یه کم شلوغم، چند لحظه دیگه دوباره پیام بده 🙏")
        return

    important = "[IMPORTANT]" in raw
    reply = raw.replace("[IMPORTANT]", "").replace("[NORMAL]", "").strip()
    if not reply:
        return
    histories.setdefault(str(sender.id), []).append(f"You: {reply}")
    memory["conversations"] = histories
    save_memory_async()
    await event.reply(reply)

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
        await send_alert(alert)
        await client.send_message("me", alert)

async def main():
    await client.start()
    me = await client.get_me()
    print(f"✅ Logged in as {me.first_name} (id={me.id})")
    print("🚀 Userbot is running...")
    print(f"📚 Loaded {len(memory.get('learned_facts', {}))} facts from GitHub")
    print(f"💬 Loaded {len(memory.get('conversations', {}))} conversations")
    if NTFY_TOPIC:
        print(f"🔔 ntfy alerts enabled for topic: {NTFY_TOPIC}")
    await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
