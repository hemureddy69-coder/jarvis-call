"""
Focus mode — Pomodoro timer that keeps distractions off your screen.

  "Start focus mode for DBMS revision" (25 min work / 5 min break)
  "Focus for 50 minutes, 3 rounds"
  "Stop focus mode" / "How long have I focused today?"

While a work round runs, Jarvis checks the active window every few seconds.
If a blocked site (Instagram, YouTube…) is open in a browser, it is minimized
(or its tab closed — your choice in settings) and Jarvis tells you off. Blocked
apps (e.g. Discord, Steam) are closed. Breaks are free time.
Optionally Jarvis PHONES you after repeated distractions (uses Twilio credit).

Window watching works on Windows. On macOS/Linux the timer still works, the
blocking just doesn't.
"""
from __future__ import annotations

import platform
import threading
import time
from datetime import date, datetime

from plugins import _bus as bus
from plugins import _store as st

NAME = "focus_mode"
LOG = "focus_log"
DEFAULT_LOG = {"sessions": []}
IS_WIN = platform.system() == "Windows"
BROWSERS = {"chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe",
            "opera_gx.exe", "vivaldi.exe", "arc.exe", "iexplore.exe"}

PLUGIN_SETTINGS = {
    "namespace": NAME,
    "title": "FOCUS MODE",
    "fields": [
        {"key": "blocked_sites", "label": "Blocked sites (words in the tab title)", "type": "text",
         "default": "instagram, youtube, netflix, reddit, facebook, prime video, hotstar, snapchat"},
        {"key": "blocked_apps", "label": "Blocked apps (process names)", "type": "text",
         "default": "discord.exe, steam.exe, epicgameslauncher.exe"},
        {"key": "on_distraction", "label": "When a blocked site opens", "type": "choice",
         "options": ["minimize window", "close the tab", "warn only"]},
        {"key": "call_after", "label": "Phone me after this many distractions (0 = never)",
         "type": "text", "default": "0"},
    ],
}

PLUGIN = {
    "name": "focus_mode",
    "description": (
        "Pomodoro focus sessions that block distracting websites and apps. Use when "
        "the user wants to focus/study/work without distractions, start a pomodoro, "
        "stop or check focus mode, or asks how long they focused today/this week."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "start, stop, status, stats"},
            "minutes": {"type": "INTEGER", "description": "Work minutes per round (default 25)"},
            "break_minutes": {"type": "INTEGER", "description": "Break minutes (default 5)"},
            "rounds": {"type": "INTEGER", "description": "Number of rounds (default 1)"},
            "task": {"type": "STRING", "description": "What the user is working on"},
        },
        "required": ["action"],
    },
}

_S: dict = {}                 # current session; empty when idle
_LOCK = threading.RLock()


def run(parameters: dict, player=None, session_memory=None) -> str:
    try:
        a = (parameters.get("action") or "status").lower().strip()
        if a == "start":
            return _start(parameters)
        if a == "stop":
            return _stop(user=True)
        if a == "stats":
            return _stats_text()
        return _status()
    except Exception as e:
        return f"Sir, focus mode hit an error: {e}"


def _start(p: dict) -> str:
    with _LOCK:
        if _S:
            return "Focus mode is already running, sir. " + _status()
        work = max(1, min(180, st.to_int(p.get("minutes"), 25)))
        brk = max(0, min(60, st.to_int(p.get("break_minutes"), 5)))
        rounds = max(1, min(12, st.to_int(p.get("rounds"), 1)))
        _S.update(task=(p.get("task") or "").strip(), work=work, brk=brk, rounds=rounds,
                  round=1, phase="work", phase_end=time.time() + work * 60,
                  started=time.time(), focused_s=0.0, distractions=0, called=False,
                  last_tick=time.time())
        threading.Thread(target=_loop, name="focus-loop", daemon=True).start()
    extra = "" if IS_WIN else " (Site blocking only works on Windows; the timer still runs.)"
    return (f"Focus mode on: {st.plural(rounds, 'round')} of {work} minutes"
            + (f" for {_S['task']}" if _S["task"] else "") + ". Distractions are blocked. Go." + extra)


def _stop(user: bool = False) -> str:
    with _LOCK:
        if not _S:
            return "Focus mode isn't running, sir."
        mins = round(_S["focused_s"] / 60)
        entry = {"date": date.today().isoformat(), "start": datetime.fromtimestamp(_S["started"]).strftime("%H:%M"),
                 "minutes": mins, "task": _S["task"], "distractions": _S["distractions"]}
        _S.clear()
    if mins > 0:
        with st.lock():
            log = st.load(LOG, DEFAULT_LOG)
            log["sessions"].append(entry)
            log["sessions"] = log["sessions"][-500:]
            st.save(LOG, log)
    msg = f"Focus session ended: {st.plural(mins, 'minute')} focused, {st.plural(entry['distractions'], 'distraction')} blocked."
    if not user:
        bus.say(msg + f" Total today: {today_minutes()} minutes.")
    return msg


def _status() -> str:
    with _LOCK:
        if not _S:
            return f"Focus mode is off. You've focused {today_minutes()} minutes today, sir."
        left = max(0, int(_S["phase_end"] - time.time()))
        return (f"Round {_S['round']} of {_S['rounds']}, {_S['phase']} — "
                f"{left // 60} min {left % 60} s left. Distractions blocked: {_S['distractions']}.")


# ── the loop ─────────────────────────────────────────────────────────────────
def _loop():
    last_app_scan = 0.0
    while True:
        time.sleep(3)
        with _LOCK:
            if not _S:
                return
            now = time.time()
            if _S["phase"] == "work":
                _S["focused_s"] += now - _S["last_tick"]
            _S["last_tick"] = now
            phase, phase_end = _S["phase"], _S["phase_end"]

        if now >= phase_end:
            with _LOCK:
                if not _S:
                    return
                if phase == "work":
                    if _S["round"] >= _S["rounds"]:
                        pass
                    elif _S["brk"]:
                        _S.update(phase="break", phase_end=now + _S["brk"] * 60)
                        bus.say(f"Round {_S['round']} done. Take a {_S['brk']} minute break — stretch, drink water.")
                        bus.toast("Focus mode", f"Break: {_S['brk']} minutes")
                        continue
                    else:
                        _S.update(round=_S["round"] + 1, phase_end=now + _S["work"] * 60)
                        continue
                else:
                    _S.update(phase="work", round=_S["round"] + 1, phase_end=now + _S["work"] * 60)
                    bus.say(f"Break's over. Round {_S['round']} of {_S['rounds']} — back to work.")
                    bus.toast("Focus mode", f"Round {_S['round']} started")
                    continue
            _stop()                      # last work round finished
            return

        if phase != "work" or not IS_WIN or not st.plugin_enabled(NAME):
            continue
        try:
            _check_window()
            if now - last_app_scan > 10:
                last_app_scan = now
                _close_blocked_apps()
        except Exception as e:
            print(f"[Focus] watcher error: {e}")


def _words(key: str, default: str) -> list[str]:
    return [w.strip().lower() for w in str(st.setting(NAME, key, default)).split(",") if w.strip()]


def _active_window():
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    try:
        import psutil
        proc = psutil.Process(pid.value).name().lower()
    except Exception:
        proc = ""
    return hwnd, buf.value, proc


def _check_window():
    hwnd, title, proc = _active_window()
    if proc not in BROWSERS:
        return
    t = title.lower()
    hit = next((w for w in _words("blocked_sites", PLUGIN_SETTINGS["fields"][0]["default"]) if w in t), None)
    if not hit:
        return
    mode = str(st.setting(NAME, "on_distraction", "minimize window"))
    import ctypes
    user32 = ctypes.windll.user32
    if mode.startswith("close"):
        # Ctrl+W on the same window that is showing the blocked tab
        if user32.GetForegroundWindow() == hwnd:
            for vk, up in ((0x11, 0), (0x57, 0), (0x57, 2), (0x11, 2)):
                user32.keybd_event(vk, 0, up, 0)
    elif mode.startswith("minimize"):
        user32.ShowWindow(hwnd, 6)
    _distracted(hit.title())


def _close_blocked_apps():
    blocked = set(_words("blocked_apps", PLUGIN_SETTINGS["fields"][1]["default"]))
    if not blocked:
        return
    import psutil
    for p in psutil.process_iter(["name"]):
        name = (p.info.get("name") or "").lower()
        if name in blocked:
            try:
                p.terminate()
                _distracted(name.replace(".exe", "").title())
            except Exception:
                pass


def _distracted(what: str):
    with _LOCK:
        if not _S:
            return
        _S["distractions"] += 1
        n, called, task = _S["distractions"], _S["called"], _S["task"]
    bus.toast("Focus mode", f"{what} blocked — back to work")
    bus.say(f"The user opened {what} during focus mode{' while working on ' + task if task else ''}. "
            "Give them a short, witty nudge back to work.", dedupe_key="focus-nudge", min_gap_s=45)
    limit = st.to_int(st.setting(NAME, "call_after", 0), 0)
    if limit and n >= limit and not called:
        with _LOCK:
            if _S:
                _S["called"] = True
        try:
            from plugins import phone_call
            phone_call.run({"action": "call_now", "kind": "reminder",
                            "message": f"you've been distracted {n} times in this focus session. "
                                       "Put the phone down and get back to work."})
        except Exception as e:
            print(f"[Focus] couldn't place the call: {e}")


# ── stats ────────────────────────────────────────────────────────────────────
def _minutes_since(days: int) -> int:
    cutoff = date.fromordinal(date.today().toordinal() - days + 1).isoformat()
    return sum(s["minutes"] for s in st.load(LOG, DEFAULT_LOG)["sessions"] if s["date"] >= cutoff)


def today_minutes() -> int:
    live = round(_S.get("focused_s", 0) / 60) if _S else 0
    return _minutes_since(1) + live


def _stats_text() -> str:
    t, w = today_minutes(), _minutes_since(7)
    return f"Focused {t} minutes today and {w // 60} hours {w % 60} minutes in the last 7 days, sir."


def call_facts(kind: str) -> list[str]:
    if kind == "weekly":
        w = _minutes_since(7)
        return [f"Focus time this week: {w // 60} h {w % 60} min"] if w else []
    if kind == "recap":
        t = today_minutes()
        return [f"Focused {t} minutes today"] if t else ["No focus sessions today"]
    return []
