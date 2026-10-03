import os
import requests
from typing import Optional

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8856348501:AAFsLNtALhE8Tjpmx4plzr19LO11TQhlN5g")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

_cached_chat_id: Optional[str] = TELEGRAM_CHAT_ID if TELEGRAM_CHAT_ID else None

def get_latest_chat_id() -> Optional[str]:
    """Retrieves the latest chat ID who sent /start or any message to the bot."""
    global _cached_chat_id
    if _cached_chat_id:
        return _cached_chat_id

    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
        r = requests.get(url, timeout=6)
        if r.status_code == 200:
            data = r.json()
            for update in reversed(data.get("result", [])):
                msg = update.get("message") or update.get("channel_post") or {}
                chat = msg.get("chat", {})
                cid = chat.get("id")
                if cid:
                    _cached_chat_id = str(cid)
                    return _cached_chat_id
    except Exception as e:
        print(f"[TelegramNotifier] Discovery error: {e}")
    return None

def send_telegram_update(message: str) -> bool:
    """Sends a progress update to Telegram. Fails silently if user hasn't messaged bot yet."""
    cid = get_latest_chat_id()
    if not cid:
        print(f"[TelegramNotifier] (Awaiting user /start at t.me/Submittermebot) Log: {message}")
        return False

    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": cid,
            "text": message,
            "parse_mode": "HTML"
        }
        r = requests.post(url, json=payload, timeout=8)
        if r.status_code == 200:
            print(f"[TelegramNotifier] Sent: {message}")
            return True
        else:
            print(f"[TelegramNotifier] Telegram API error: {r.status_code} {r.text}")
    except Exception as e:
        print(f"[TelegramNotifier] Send error: {e}")
    return False

def send_telegram_video(video_path: str, caption: str = "") -> bool:
    """Sends an MP4 video directly through Telegram."""
    cid = get_latest_chat_id()
    if not cid:
        return False

    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendVideo"
        with open(video_path, "rb") as vf:
            files = {"video": vf}
            data = {"chat_id": cid, "caption": caption}
            r = requests.post(url, data=data, files=files, timeout=120)
            return r.status_code == 200
    except Exception as e:
        print(f"[TelegramNotifier] Video upload error: {e}")
        return False
