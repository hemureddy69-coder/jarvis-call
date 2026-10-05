"""
Laptop guard — catches anyone using your laptop while you're away.

  Voice:    "Guard my laptop"  (arms after 30 seconds so you can walk away)
  Telegram: /guard on · /guard off · /guard status

While armed, the first keyboard or mouse touch takes a webcam photo and a
screenshot, sends both to your phone on Telegram, and (optionally) locks
Windows. It stays armed — every new attempt (at most once a minute) is reported
until you disarm it from your phone or by voice.

Windows only for detecting input; the photo helpers are used by the Telegram
bot on any OS. Needs Telegram set up (telegram_bot plugin).
"""
from __future__ import annotations

import platform
import threading
import time
from datetime import datetime

from plugins import _bus as bus
from plugins import _store as st
from plugins import _telegram as tg

NAME = "laptop_guard"
IS_WIN = platform.system() == "Windows"
ARM_DELAY_S = 30
COOLDOWN_S = 60

PLUGIN_SETTINGS = {
    "namespace": NAME,
    "title": "LAPTOP GUARD",
    "fields": [
        {"key": "lock_on_intrusion", "label": "Lock Windows when someone touches it", "type": "toggle", "default": True},
        {"key": "webcam_index", "label": "Webcam number (0 = built-in)", "type": "text", "default": "0"},
    ],
}

PLUGIN = {
    "name": "laptop_guard",
    "description": (
        "Laptop guard: when armed, anyone touching the keyboard/mouse gets photographed "
        "with the webcam and a screenshot is sent to the user's phone on Telegram. Use "
        "when the user says 'guard my laptop', 'watch my laptop while I'm away', 'disarm "
        "the guard', or asks if the guard is on."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {"action": {"type": "STRING", "description": "arm, disarm, status"}},
        "required": ["action"],
    },
}

_G = {"armed": False, "since": 0.0, "baseline": 0, "last_alert": 0.0, "alerts": 0}
_LOCK = threading.RLock()


def run(parameters: dict, player=None, session_memory=None) -> str:
    a = (parameters.get("action") or "status").lower().strip()
    if a == "arm":
        return arm(source="voice")
    if a == "disarm":
        return disarm(source="voice")
    return status()


def arm(source: str = "voice", delay: int = ARM_DELAY_S) -> str:
    if not IS_WIN:
        return "Sir, the laptop guard can only detect intruders on Windows."
    if not tg.configured():
        return "Sir, set up the Telegram bot first so I have somewhere to send the photos."
    with _LOCK:
        _G.update(armed=True, since=time.time() + delay, baseline=0, alerts=0, last_alert=0.0)
    threading.Thread(target=_loop, name="guard-loop", daemon=True).start()
    tg.send(f"🛡 Laptop guard armed ({source}). Watching from {datetime.now().strftime('%I:%M %p')}"
            f"{f' (+{delay}s)' if delay else ''}. Send /guard off to disarm.")
    return (f"Laptop guard armed. You have {delay} seconds to step away — "
            "don't touch the keyboard after that." if delay else "Laptop guard armed.")


def disarm(source: str = "voice") -> str:
    with _LOCK:
        was, n = _G["armed"], _G["alerts"]
        _G["armed"] = False
    if was:
        tg.send(f"🛡 Laptop guard disarmed ({source}) at {datetime.now().strftime('%I:%M %p')}. "
                f"Intrusions caught: {n}.")
        return f"Laptop guard off. {st.plural(n, 'intrusion')} caught while you were away."
    return "The laptop guard wasn't on, sir."


def status() -> str:
    with _LOCK:
        if not _G["armed"]:
            return "Laptop guard is off."
        return f"Laptop guard is armed. Intrusions caught so far: {_G['alerts']}."


# ── watching ─────────────────────────────────────────────────────────────────
def _last_input_tick() -> int:
    import ctypes
    from ctypes import wintypes

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

    lii = LASTINPUTINFO()
    lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
    ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii))
    return lii.dwTime


def _loop():
    while True:
        time.sleep(1)
        with _LOCK:
            if not _G["armed"]:
                return
            if time.time() < _G["since"]:
                continue
            tick = _last_input_tick()
            if not _G["baseline"]:
                _G["baseline"] = tick
                continue
            touched = tick != _G["baseline"]
            cooling = time.time() - _G["last_alert"] < COOLDOWN_S
            if touched:
                _G["baseline"] = tick
            if not touched or cooling:
                continue
            _G["last_alert"] = time.time()
            _G["alerts"] += 1
        _intrusion()


def _intrusion():
    when = datetime.now().strftime("%I:%M:%S %p")
    bus.log(f"Laptop guard: intrusion at {when} — alerting your phone.")
    tg.send(f"🚨 Someone is using your laptop! ({when})")
    photo = webcam_jpeg()
    if photo:
        tg.send_photo(photo, f"Webcam · {when}", "webcam.jpg")
    shot = screenshot_png()
    if shot:
        tg.send_photo(shot, f"Screen · {when}", "screen.png")
    if st.truthy(st.setting(NAME, "lock_on_intrusion", True)):
        lock_pc()
        tg.send("🔒 Locked the laptop. Send /guard off when you're back.")


# ── capture helpers (also used by /photo and /screenshot) ────────────────────
def webcam_jpeg() -> bytes | None:
    try:
        import cv2
        idx = st.to_int(st.setting(NAME, "webcam_index", 0), 0)
        cam = cv2.VideoCapture(idx, cv2.CAP_DSHOW) if IS_WIN else cv2.VideoCapture(idx)
        try:
            frame = None
            for _ in range(12):               # let auto-exposure settle
                ok, f = cam.read()
                if ok:
                    frame = f
                time.sleep(0.05)
        finally:
            cam.release()
        if frame is None:
            return None
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
        return buf.tobytes() if ok else None
    except Exception as e:
        print(f"[Guard] webcam failed: {e}")
        return None


def screenshot_png() -> bytes | None:
    try:
        import mss
        import mss.tools
        with mss.mss() as s:
            img = s.grab(s.monitors[0])
            return mss.tools.to_png(img.rgb, img.size)
    except Exception as e:
        print(f"[Guard] screenshot failed: {e}")
        return None


def lock_pc() -> bool:
    try:
        if IS_WIN:
            import ctypes
            return bool(ctypes.windll.user32.LockWorkStation())
        import subprocess
        cmd = (["pmset", "displaysleepnow"] if platform.system() == "Darwin"
               else ["loginctl", "lock-session"])
        subprocess.run(cmd, timeout=5)
        return True
    except Exception:
        return False
