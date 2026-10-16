import asyncio
from telethon.sync import TelegramClient
from telethon.sessions import StringSession

API_ID = int(input("API_ID: "))
API_HASH = input("API_HASH: ")

loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

with TelegramClient(StringSession(), API_ID, API_HASH) as client:
    session_string = client.session.save()
    print("\n=== SESSION STRING (این رو کپی کن) ===")
    print(session_string)
    print("=== END ===")