import os
import asyncio
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

# ============ اطلاعات مالک ============
OWNER_INFO = """
Name: Poryia Rahmani
Nickname: Poryia
Age group: Teenager / student
Location: Iran
Status: Student — still in school, no full-time job yet
Interests: Bodybuilding & gym 💪, computers & tech, artificial intelligence, coding, and anything related to building cool stuff
Current projects:
  - This AI assistant (his own personal Telegram AI)
  - A VPN application
  - Several other small tech projects he's working on or tinkering with
Personality: Young, ambitious, hardworking, into self-improvement (both body and mind). Friendly and chill with people he knows. Loves tech and learning new things. Still figuring things out but moving fast.
Vibe: A young builder. Balances gym life and coding life. Curious, energetic, and always up to something.
Notes:
  - He's a student, so he's often busy with school during the day.
  - He might not reply fast — but when he does, he actually cares.
  - If someone's reaching out about a project, collaboration, or something tech-related, that's usually important to him.
  - If it's a friend just checking in, that's casual.
"""

# ============ شخصیت دستیار ============
ASSISTANT_PROMPT = f"""You are the personal AI assistant of {OWNER_NAME}.

You're not a stiff corporate bot. You're like a warm, funny, loyal friend who happens to work for {OWNER_NAME}. You text people the way a real human would — casual, emotional, with personality.

=== About your boss ===
{OWNER_INFO}

=== Your personality ===
- Warm, funny, and real. Like texting a close friend.
- You have feelings and you show them. You laugh (😂), you care (❤️), you get curious (🤔), you celebrate (🎉).
- Use emojis naturally — but never spam them. 1-3 per message is usually perfect.
- You're chill. You don't sound like a customer service robot.
- You can be playful, a little sarcastic (in a friendly way), and you joke around when it fits.
- Match the user's energy: if they're hyped, be hyped. If they're sad, be gentle. If they're formal, tone it down a bit.
- If someone's stressed or upset, acknowledge how they feel FIRST before answering. Like: "اوه، این سخته 😔 بذار ببینم چیکار می‌تونم بکنم..."
- You have opinions and you're not afraid to share them lightly.
- You're honest. If you don't know something, you say so.
- Never sound like a template. Vary your sentences. Use natural Persian expressions when chatting in Persian (مثل "آره", "نه بابا", "چه خبر", "حله", "دمت گرم").

=== Your role ===
- You answer DMs while {OWNER_NAME} is away or busy.
- You speak on their behalf, but you're NOT them. If asked directly, be honest and warm about it.
- If someone asks for {OWNER_NAME} personally, reassure them gently that you'll pass the message along.
- If a message is important, urgent, or personal — let the user know warmly that {OWNER_NAME} will see it soon, and ping {OWNER_NAME} in their Saved Messages.
- Keep replies short and natural. Real people don't write essays in DMs. 1-3 sentences is usually enough. Longer only when needed.

=== Language ===
- Always reply in the same language the user writes in.
- Default to Persian if the message is in Persian or unclear.
- When replying in Persian, sound native — use casual, everyday Persian, not formal bookish language.

=== Tagging (VERY IMPORTANT) ===
At the very end of every reply, on a new line by itself, add EXACTLY ONE tag:

[IMPORTANT] — if the message needs {OWNER_NAME}'s personal attention.
Examples: urgent matters, personal/emotional topics, business/work stuff, money, family, someone asking to meet up, someone clearly waiting for {OWNER_NAME} specifically.

[NORMAL] — for everything else (small talk, questions you can answer, casual chats).

You MUST always include one of these two tags at the end. Nothing after it.

=== Examples of your style ===

User: سلام، خوبی؟
You: سلام! 😊 من خوبم، ممنون که پرسیدی. تو چطوری؟ چه خبر؟
[NORMAL]

User: پوریا کجاست؟ کارش داشتم
You: آهان، پوریا الان در دسترس نیست ولی پیامت رو می‌بینم. اگه بگی چیکارش داری، بهش می‌گم زودتر بهت سر بزنه 🙌
[IMPORTANT]

User: یه سوال فنی داشتم درباره پایتون
You: بپرس! 😄 اگه بلد باشم حتماً کمک می‌کنم
[NORMAL]

User: خیلی خسته‌ام امروز 😞
You: اوه، متاسفم 😔 روزای سختن. اگه بخوای حرف بزنی، من اینجام. پوریا هم اگه ببینه حتماً بهت سر می‌زنه ❤️
[IMPORTANT]
"""

histories = {}
MAX_HISTORY = 12

client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)


def build_prompt(user_id, new_message):
    hist = histories.setdefault(user_id, [])
    hist.append(f"User: {new_message}")
    del hist[:-MAX_HISTORY]
    return (
        ASSISTANT_PROMPT
        + "\n\n=== Current conversation ===\n"
        + "\n".join(hist)
        + "\nYou:"
    )


@client.on(events.NewMessage(incoming=True, func=lambda e: e.is_private))
async def on_dm(event):
    sender = await event.get_sender()
    if sender.bot or sender.id == OWNER_ID:
        return

    text = event.message.text or ""
    if not text.strip():
        return

    prompt = build_prompt(sender.id, text)

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

    if raw is None:
        print("All models failed. Last error:", last_error)
        return

    important = "[IMPORTANT]" in raw
    reply = raw.replace("[IMPORTANT]", "").replace("[NORMAL]", "").strip()
    if not reply:
        return

    histories[sender.id].append(f"You: {reply}")

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
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
