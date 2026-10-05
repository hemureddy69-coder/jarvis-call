"""
A tiny bridge so background plugins can talk to you.

main.py calls attach(jarvis, ui) once at startup. After that any plugin can:
    _bus.say("Focus session over — take a 5 minute break.")   # Jarvis says it aloud
    _bus.log("Logged ₹120 for lunch")                         # activity log line
    _bus.toast("Focus mode", "Instagram closed")              # desktop notification

Every function is safe to call before attach() or when Jarvis is offline —
it simply prints instead. The leading underscore keeps the plugin loader from
treating this file as a plugin.
"""
from __future__ import annotations

import platform
import threading
import time

_JARVIS = None
_UI = None
_LAST_SAID: dict[str, float] = {}


def attach(jarvis, ui) -> None:
    global _JARVIS, _UI
    _JARVIS, _UI = jarvis, ui


def say(text: str, dedupe_key: str | None = None, min_gap_s: float = 0,
        phone_text: str | None = None) -> None:
    """Ask Jarvis to tell the user `text` in its own voice.

    On the cloud server there is no voice, so `phone_text` (or `text`) goes to
    the user's phone on Telegram instead."""
    if dedupe_key and min_gap_s:
        last = _LAST_SAID.get(dedupe_key, 0)
        if time.time() - last < min_gap_s:
            return
        _LAST_SAID[dedupe_key] = time.time()
    log(text)
    try:
        from plugins import _store
        if _store.is_cloud():
            phone(phone_text or text, force=True)
            return
    except Exception:
        pass
    if _JARVIS is None:
        return
    try:
        if hasattr(_JARVIS, "wake"):
            _JARVIS.wake(reason="notification")     # a notice should never be lost to sleep
    except Exception:
        pass
    try:
        _JARVIS.speak("[Automatic notice from a background feature — tell the user this "
                      "briefly and naturally, in their language, then carry on]: " + text)
    except Exception as e:
        print(f"[bus] speak failed: {e}")


def log(text: str) -> None:
    print(f"[JARVIS] {text}")
    if _UI is not None:
        try:
            _UI.write_log(f"JARVIS: {text}")
        except Exception:
            pass


def toast(title: str, message: str) -> None:
    """Desktop notification; never blocks, never raises."""
    def _do():
        try:
            if platform.system() == "Windows":
                from win10toast import ToastNotifier
                ToastNotifier().show_toast(title, message, duration=5, threaded=False)
            elif platform.system() == "Darwin":
                import subprocess
                subprocess.run(["osascript", "-e",
                                f'display notification "{message}" with title "{title}"'],
                               timeout=5)
            else:
                import subprocess
                subprocess.run(["notify-send", title, message], timeout=5)
        except Exception:
            pass
    threading.Thread(target=_do, daemon=True).start()


def phone(text: str, force: bool = False) -> None:
    """Push an alert to the phone over Telegram (if set up and allowed). Never blocks."""
    def _do():
        try:
            from plugins import _telegram
            _telegram.send(text) if force else _telegram.push(text)
        except Exception:
            pass
    threading.Thread(target=_do, daemon=True).start()
