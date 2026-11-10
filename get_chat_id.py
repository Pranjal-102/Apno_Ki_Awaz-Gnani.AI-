import os

import requests
from dotenv import load_dotenv

load_dotenv()
token = os.getenv("TELEGRAM_BOT_TOKEN")
if not token:
    raise SystemExit("Add TELEGRAM_BOT_TOKEN to .env first")

data = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=15).json()
chats = {}
for u in data.get("result", []):
    chat = (u.get("message") or {}).get("chat")
    if chat:
        chats[chat["id"]] = chat.get("first_name") or chat.get("title")

if not chats:
    print("No messages found. Open your bot in Telegram, press Start, send 'hi', then run this again.")
else:
    for cid, name in chats.items():
        print("chat id:", cid, "|", name)
    print("Add this line to .env:  TELEGRAM_CHAT_ID=<the number>")