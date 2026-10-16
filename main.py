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
OWNER_NAME = os.environ.get("OWNER_NAME", "the owner")

ai = genai.Client(api_key=GEMINI_API_KEY)

ASSISTANT_PROMPT = f"""You are the personal AI assistant of {OWNER_NAME}.
You answer Telegram messages on their behalf while they are away.

Rules:
- Always respond in the same language as the incoming message (default: Persian).
- If asked who you are, say you're "{OWNER_NAME}'s AI assistant".
- Be polite, concise, and helpful.
- Never pretend to be {OWNER_NAME} themselves.
- If the message is important or needs {OWNER_NAME}'s personal attention, tell the user you'll notify them.
- At the very end of your response, on a new line, add exactly one tag:
  [IMPORTANT] if the message needs {OWNER_NAME}'s attention,
  [NORMAL] otherwise.
"""

histories = {}
MAX_HISTORY = 10

client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)


def build_prompt(user_id, new_message):
    hist = histories.setdefault(user_id, [])
    hist.append(f"User: {new_message}")
    del hist[:-MAX_HISTORY]
    return (
        ASSISTANT_PROMPT
        + "\n\nConversation so far:\n"
        + "\n".join(hist)
        + "\nAssistant:"
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

    try:
        result = ai.models.generate_content(
            model="gemini-3.8-flash",
            contents=prompt,
        )
        raw = (result.text or "").strip()
    except Exception as e:
        print("Gemini error:", e)
        return

    important = "[IMPORTANT]" in raw
    reply = raw.replace("[IMPORTANT]", "").replace("[NORMAL]", "").strip()
    if not reply:
        return

    histories[sender.id].append(f"Assistant: {reply}")

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
