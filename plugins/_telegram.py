"""
Minimal Telegram Bot API client (plain `requests`, no extra package).

Shared by telegram_bot.py, laptop_guard.py and anything that wants to push a
message to your phone. Leading underscore = not a plugin itself.

Settings live under plugin_config["telegram"]: bot_token, chat_id.
Only the paired chat_id is ever answered — everyone else is ignored.
"""
from __future__ import annotations

import time

from plugins import _store as st

NS = "telegram"
API = "https://api.telegram.org"
MAX_LEN = 4000


def token() -> str:
    return str(st.setting(NS, "bot_token", "") or "").strip()


PAIR_NS = "telegram_pair"     # not shown in the settings form, so SAVE can't wipe it


def chat_id() -> str:
    return str(st.setting(PAIR_NS, "chat_id", "") or st.setting(NS, "chat_id", "") or "").strip()


def save_chat_id(chat: str) -> None:
    from memory.config_manager import save_plugin_config
    save_plugin_config(PAIR_NS, {"chat_id": str(chat)})


def configured() -> bool:
    return bool(token() and chat_id())


def api(method: str, http_timeout: float = 20, files=None, tok: str | None = None, **params):
    """Call a Bot API method. Returns the `result` or raises RuntimeError."""
    import requests
    tok = tok or token()
    if not tok:
        raise RuntimeError("no Telegram bot token set")
    url = f"{API}/bot{tok}/{method}"
    if files:
        r = requests.post(url, data=params, files=files, timeout=http_timeout)
    else:
        r = requests.post(url, json=params, timeout=http_timeout)
    try:
        j = r.json()
    except Exception:
        raise RuntimeError(f"Telegram HTTP {r.status_code}")
    if not j.get("ok"):
        raise RuntimeError(j.get("description") or f"Telegram error {r.status_code}")
    return j.get("result")


def send(text: str, to: str | None = None) -> bool:
    """Send text to the paired phone. Splits long messages. Never raises."""
    to = to or chat_id()
    if not (token() and to and text):
        return False
    try:
        for i in range(0, len(text), MAX_LEN):
            api("sendMessage", chat_id=to, text=text[i:i + MAX_LEN],
                disable_web_page_preview=True)
            time.sleep(0.05)
        return True
    except Exception as e:
        print(f"[Telegram] send failed: {e}")
        return False


def send_photo(data: bytes, caption: str = "", filename: str = "photo.jpg", to: str | None = None) -> bool:
    to = to or chat_id()
    if not (token() and to and data):
        return False
    try:
        api("sendPhoto", http_timeout=60, files={"photo": (filename, data)},
            chat_id=to, caption=caption[:1000])
        return True
    except Exception as e:
        print(f"[Telegram] photo failed: {e}")
        return False


def send_document(path, caption: str = "", to: str | None = None) -> bool:
    to = to or chat_id()
    if not (token() and to):
        return False
    try:
        with open(path, "rb") as f:
            api("sendDocument", http_timeout=60, files={"document": (path.name, f)},
                chat_id=to, caption=caption[:1000])
        return True
    except Exception as e:
        print(f"[Telegram] document failed: {e}")
        return False


def push(text: str) -> bool:
    """Alert pushed to the phone if the user enabled 'push_alerts'."""
    if not configured() or not st.truthy(st.setting(NS, "push_alerts", True)):
        return False
    return send(text)


def download(file_id: str) -> bytes:
    import requests
    info = api("getFile", file_id=file_id)
    r = requests.get(f"{API}/file/bot{token()}/{info['file_path']}", timeout=60)
    r.raise_for_status()
    return r.content
