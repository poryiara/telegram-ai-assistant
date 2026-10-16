import os
import json
import asyncio
from pathlib import Path
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from google import genai

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
SESSION_STRING = os.environ["SESSION_STRING"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
OWNER_ID = int(os.environ["OWNER_ID"])
OWNER_NAME = os.environ.get("OWNER_NAME", "Poryia")

ai = genai.Client(api_key=GEMINI_API_KEY)

MODELS = [
    "gemini-3.8-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash-lite",
]

# ============ مسیر فایل حافظه ============
MEMORY_FILE = Path("memory.json")

# ============ اطلاعات پایه مالک ============
OWNER_INFO_BASE = """
Name: Poryia Rahmani
Nickname: Poryia
Age group: Teenager / student
Location: Iran
Status: Student — still in school, no full-time job yet
Interests: Bodybuilding & gym 💪, computers & tech, artificial intelligence, coding
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

# ============ توابع حافظه ============
def load_memory():
    if MEMORY_FILE.exists():
        try:
            with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"⚠️ Memory load error: {e}")
    return {"conversations": {}, "learned_facts": {}, "owner_notes": []}

def save_memory(memory):
    try:
        with open(MEMORY_FILE, "w", encoding="utf-8") as f:
            json.dump(memory, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"⚠️ Memory save error: {e}")

# بارگذاری حافظه در شروع
memory = load_memory()
histories = memory.get("conversations", {})
MAX_HISTORY = 12

def build_owner_info():
    """اطلاعات مالک + چیزایی که بهش یاد دادیم"""
    info = OWNER_INFO_BASE
    facts = memory.get("learned_facts", {})
    notes = memory.get("owner_notes", [])
    
    if facts:
        info += "\n\n=== Learned facts about Poryia ===\n"
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
        # پرامپت مخصوص ادمین (Saved Messages)
        prompt = f"""You are the personal AI assistant of {OWNER_NAME}.
You are currently talking to {OWNER_NAME} THEMSELVES in their Saved Messages (admin panel).

This is your boss. Be warm, friendly, and casual with them. You can be yourself.
If they tell you to learn something, acknowledge it warmly.

=== About your boss ===
{build_owner_info()}

=== Current admin conversation ===
{chr(10).join(hist)}
You:"""
    else:
        # پرامپت مخصوص کاربران عادی
        prompt = f"""You are the personal AI assistant of {OWNER_NAME}.
You answer Telegram messages on their behalf while they are away.

You're not a stiff corporate bot. You're like a warm, funny, loyal friend.
Use emojis naturally (1-3 per message). Match the user's energy.
If someone's stressed, acknowledge their feeling first.
Sound native in Persian — use casual, everyday Persian.

=== About your boss ===
{build_owner_info()}

=== Rules ===
- Always respond in the same language as the incoming message (default: Persian).
- If asked who you are, say you're "{OWNER_NAME}'s AI assistant" warmly.
- Never pretend to be {OWNER_NAME} themselves.
- If important, tell the user you'll notify {OWNER_NAME}.
- Keep replies short and natural (1-3 sentences usually).
- At the very end, on a new line, add exactly one tag:
  [IMPORTANT] or [NORMAL]

=== Current conversation ===
{chr(10).join(hist)}
You:"""
    
    return prompt

async def ask_gemini(prompt):
    raw = None
    last_error = None
    for model_name in MODELS:
        try:
            result = ai.models.generate_content(
                model=model_name,
                contents=prompt,
            )
            raw = (result.text or "").strip()
            print(f"✅ Response from {model_name}")
            break
        except Exception as e:
            last_error = e
            print(f"❌ Gemini error with {model_name}:", e)
            continue
    return raw, last_error

# ============ کلاینت ============
client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)


# ============ هندلر ادمین (Saved Messages) ============
@client.on(events.NewMessage(outgoing=True, func=lambda e: e.is_private and e.chat_id == OWNER_ID))
async def on_owner_message(event):
    """وقتی خودت توی Saved Messages پیام می‌فرستی"""
    text = event.message.text or ""
    if not text.strip():
        return
    
    # دستورات ویژه
    if text.startswith("/learn "):
        content = text[7:].strip()
        if "=" in content:
            key, value = content.split("=", 1)
            memory.setdefault("learned_facts", {})[key.strip()] = value.strip()
            save_memory(memory)
            await event.reply(f"✅ یاد گرفتم: **{key.strip()}** = {value.strip()}")
        else:
            # اگه = نبود، به عنوان یه فکت کلی ذخیره کن
            fact_id = f"fact_{len(memory.get('learned_facts', {})) + 1}"
            memory.setdefault("learned_facts", {})[fact_id] = content
            save_memory(memory)
            await event.reply(f"✅ یاد گرفتم: {content}")
        return
    
    if text == "/memory":
        facts = memory.get("learned_facts", {})
        notes = memory.get("owner_notes", [])
        reply = "📚 **حافظه‌ی من:**\n\n"
        if facts:
            reply += "**فکت‌های یادگرفته:**\n"
            for k, v in facts.items():
                reply += f"• {k}: {v}\n"
        else:
            reply += "هنوز چیزی یاد نگرفتم.\n"
        if notes:
            reply += "\n**یادداشت‌ها:**\n"
            for n in notes:
                reply += f"• {n}\n"
        await event.reply(reply)
        return
    
    if text.startswith("/forget "):
        key = text[8:].strip()
        facts = memory.get("learned_facts", {})
        if key in facts:
            del facts[key]
            save_memory(memory)
            await event.reply(f"🗑️ فراموش کردم: {key}")
        else:
            await event.reply(f"❌ چنین چیزی توی حافظه‌ام نیست: {key}")
        return
    
    if text.startswith("/note "):
        note = text[6:].strip()
        memory.setdefault("owner_notes", []).append(note)
        save_memory(memory)
        await event.reply(f"📝 یادداشت اضافه شد: {note}")
        return
    
    if text == "/help":
        await event.reply(
            "🤖 **دستورات ادمین:**\n\n"
            "/learn کلید = مقدار — یاد گرفتن یه فکت\n"
            "/learn یه متن — یاد گرفتن یه فکت کلی\n"
            "/memory — دیدن حافظه\n"
            "/forget کلید — فراموش کردن یه فکت\n"
            "/note متن — اضافه کردن یادداشت\n"
            "/help — همین راهنما\n\n"
            "💬 **گفتگوی آزاد:**\n"
            "هر پیام دیگه‌ای بفرستی، به‌عنوان چت با AI جواب داده می‌شه."
        )
        return
    
    # اگه دستور نبود، به‌عنوان چت با AI جواب بده
    prompt = build_prompt("owner", text, is_owner=True)
    raw, last_error = await ask_gemini(prompt)
    
    if raw is None:
        await event.reply(f"❌ خطا: {last_error}")
        return
    
    # پاکسازی تگ‌ها
    reply = raw.replace("[IMPORTANT]", "").replace("[NORMAL]", "").strip()
    if not reply:
        return
    
    # ذخیره توی تاریخچه
    histories.setdefault("owner", []).append(f"You: {reply}")
    save_memory(memory)
    
    await event.reply(reply)


# ============ هندلر کاربران عادی ============
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
        return

    important = "[IMPORTANT]" in raw
    reply = raw.replace("[IMPORTANT]", "").replace("[NORMAL]", "").strip()
    if not reply:
        return

    # ذخیره توی تاریخچه و حافظه
    histories.setdefault(str(sender.id), []).append(f"You: {reply}")
    memory["conversations"] = histories
    save_memory(memory)

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
        await client.send_message("me", alert)


async def main():
    await client.start()
    me = await client.get_me()
    print(f"✅ Logged in as {me.first_name} (id={me.id})")
    print("🚀 Userbot is running...")
    print(f"📚 Loaded {len(memory.get('learned_facts', {}))} learned facts")
    print(f"💬 Loaded {len(memory.get('conversations', {}))} conversations")
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
