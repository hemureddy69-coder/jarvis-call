"""
Telegram bot — control Jarvis from your phone, anywhere, for free.

No second SIM needed: you create a BOT from your own Telegram account with
@BotFather, paste its token into ⚙ → plugin settings → TELEGRAM, and pair it.

What you can do from the phone
  Plain messages   "spent 80 on chai", "what's due this week?", "I missed OS today"
                   — understood by Gemini and run on the same tools as voice
  Photo            of a whiteboard / notes page → summary, key points and
                   flashcards (saved in notes/ on the PC too)
  /rescue [min]    Jarvis phones you in 2 minutes (or [min]) with an "urgent" call
  /call [min]      briefing call now, or in [min] minutes
  /today /due /plan /habits /att /spent /budget   quick answers
  /guard on|off    laptop guard (webcam photo + screenshot if someone uses it)
  /photo /screenshot /pc /lock                    see and control the PC
  /focus [min] /stop                              focus mode
  /help

Voice: "send this to my phone: …", "pair Telegram".
Only the paired chat is answered; everyone else is ignored.
"""
from __future__ import annotations

import json
import random
import re
import threading
import time
from datetime import date, datetime
from pathlib import Path

from plugins import _bus as bus
from plugins import _store as st
from plugins import _telegram as tg

NAME = "telegram_bot"
STATE = "telegram_state"
NOTES_DIR = st.BASE_DIR / "notes"
_PAIR_CODE = f"{random.randint(1000, 9999)}"


def _test(values: dict):
    tok = str((values or {}).get("bot_token") or "").strip()
    if not tok:
        return False, "Paste the bot token from @BotFather first."
    try:
        me = tg.api("getMe", tok=tok)
    except Exception as e:
        return False, f"Token rejected: {e}"
    if not tg.chat_id():
        return True, f"Bot @{me.get('username')} OK. In Telegram, open it and send:  /start {_PAIR_CODE}"
    ok = tg.send("✅ Jarvis is connected. Send /help to see what I can do.")
    return ok, ("Sent a test message to your phone." if ok else "Couldn't message your chat — re-pair.")


PLUGIN_SETTINGS = {
    "namespace": tg.NS,
    "title": "TELEGRAM (PHONE CONTROL)",
    "fields": [
        {"key": "bot_token", "label": "Bot token from @BotFather", "type": "password"},
        {"key": "push_alerts", "label": "Also send budget / birthday / call alerts here",
         "type": "toggle", "default": True},
    ],
    "action": {"label": "TEST / PAIR", "run": _test},
}

PLUGIN = {
    "name": "telegram_bot",
    "description": (
        "Sends things to the user's phone through their Telegram bot, and handles "
        "pairing. Use when the user says 'send this to my phone', 'text me the list', "
        "'message me that link', or 'pair/connect Telegram'."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "send or pair"},
            "text": {"type": "STRING", "description": "What to send to the phone"},
        },
        "required": ["action"],
    },
}


def run(parameters: dict, player=None, session_memory=None) -> str:
    a = (parameters.get("action") or "send").lower().strip()
    if a == "pair" or not tg.chat_id():
        if not tg.token():
            return ("Sir, create a bot with @BotFather in Telegram, then paste its token in "
                    "the Telegram plugin settings.")
        return f"Open your bot in Telegram and send: /start {_PAIR_CODE}"
    text = (parameters.get("text") or "").strip()
    if not text:
        return "Sir, what should I send?"
    return "Sent to your phone, sir." if tg.send(text) else "Sir, I couldn't reach Telegram."


# ═════════════════════════════════════════════════════════════════════════════
#  Polling loop
# ═════════════════════════════════════════════════════════════════════════════
def poll_once(wait_s: int = 45) -> int:
    """Fetch and handle new messages once. Used by the PC loop and the GitHub runner."""
    state = st.load(STATE, {"offset": 0})
    updates = tg.api("getUpdates", http_timeout=wait_s + 15, offset=state["offset"],
                     allowed_updates=["message"], timeout=wait_s)
    for u in updates or []:
        state["offset"] = u["update_id"] + 1
        st.save(STATE, state)
        msg = u.get("message") or {}
        try:
            _handle(msg)
        except Exception as e:
            print(f"[Telegram] handler error: {e}")
            tg.send(f"⚠️ Something went wrong: {e}", to=str(msg.get("chat", {}).get("id", "")))
    return len(updates or [])


def _poll_loop():
    time.sleep(10)
    warned = False
    while True:
        # Only one copy of Jarvis polls Telegram: the PC while on, GitHub while off.
        if not tg.token() or not st.plugin_enabled(NAME) or not st.runs_schedulers():
            time.sleep(15)
            continue
        state = st.load(STATE, {"offset": 0})
        try:
            updates = tg.api("getUpdates", http_timeout=60, offset=state["offset"],
                             allowed_updates=["message"], timeout=45)
            warned = False
        except Exception as e:
            if not warned:
                print(f"[Telegram] polling error: {e}")
                warned = True
            time.sleep(10)
            continue
        for u in updates or []:
            state["offset"] = u["update_id"] + 1
            st.save(STATE, state)
            msg = u.get("message") or {}
            try:
                _handle(msg)
            except Exception as e:
                print(f"[Telegram] handler error: {e}")
                tg.send(f"⚠️ Something went wrong: {e}", to=str(msg.get("chat", {}).get("id", "")))


def _handle(msg: dict):
    chat = str(msg.get("chat", {}).get("id", ""))
    text = (msg.get("text") or msg.get("caption") or "").strip()
    paired = tg.chat_id()

    if not paired:                         # pairing
        if text.startswith("/start") and _PAIR_CODE in text:
            tg.save_chat_id(chat)            # stored separately — survives SAVE and restarts
            tg.send("✅ Paired! I'm Jarvis. Send /help to see what I can do.", to=chat)
            bus.say("Telegram is now paired with the user's phone.")
        else:
            tg.send("🔒 This Jarvis isn't paired yet. Press TEST / PAIR in the Jarvis "
                    "settings and send the /start code it shows.", to=chat)
            bus.log(f"Telegram pairing code: send  /start {_PAIR_CODE}  to your bot.")
        return
    if chat != paired:
        return                              # strangers are ignored silently

    if msg.get("photo"):
        threading.Thread(target=_photo_notes, args=(msg["photo"][-1]["file_id"], text),
                         daemon=True).start()
        tg.send("📸 Got it — reading the board, give me a few seconds…")
        return
    doc = msg.get("document") or {}
    if str(doc.get("mime_type", "")).startswith("image/"):
        threading.Thread(target=_photo_notes, args=(doc["file_id"], text, doc["mime_type"]),
                         daemon=True).start()
        tg.send("📸 Got it — reading it now…")
        return
    if not text:
        return
    bus.log(f"📱 Telegram: {text}")
    if text.startswith("/"):
        reply = _command(text)
    else:
        reply = _natural(text)
    if reply:
        tg.send(reply)


# ═════════════════════════════════════════════════════════════════════════════
#  Commands
# ═════════════════════════════════════════════════════════════════════════════
HELP = """🤖 Jarvis on Telegram

Just type normally: "spent 80 on chai", "what's due?", "I missed OS", "DSA exam on 20 Nov, topics trees, graphs", "remind me by call at 6pm to submit the lab".

📸 Send a photo of the board or notes to get a summary and flashcards.

/today: classes and deadlines today
/due: pending deadlines
/plan: today's revision plan
/habits · /att · /budget
/spent 120 lunch: log an expense
/call [min]: briefing call (now or later)
/app: your Jarvis call app link (call Jarvis any time)
/rescue [min]: an "urgent" call to get you out of somewhere
/quiz [topic] · /journal · /checkin: two-way calls
/line: is the two-way call line up?
/guard on|off|status: laptop guard
/photo · /screenshot · /pc · /lock
/focus [min] · /stop"""


def _plugin(name: str, **params) -> str:
    import importlib
    try:
        mod = importlib.import_module(f"plugins.{name}")
    except ModuleNotFoundError:
        return f"The {name} feature isn't installed."
    return str(mod.run(params) or "Done.")


def _command(text: str, local: bool = False) -> str:
    parts = text.split(maxsplit=1)
    cmd = parts[0].lower().split("@")[0]
    arg = parts[1].strip() if len(parts) > 1 else ""
    num = st.to_int(re.sub(r"[^\d]", "", arg) or None, 0)

    # Answered by the GitHub runner while the PC is off: PC-only commands can't run.
    if st.is_cloud() and not local and cmd in ("/photo", "/screenshot", "/lock", "/guard",
                                                 "/focus", "/stop", "/pc"):
        return ("💻 Your PC is off right now, so I can't do that. Everything else works — "
                "try /today, /due, or send /help.")
    if cmd == "/app":
        try:
            from plugins import app_call
            link = app_call.app_link()
        except Exception:
            link = ""
        return (f"📱 Your Jarvis app — tap to call Jarvis any time:\n{link}\n\n"
                "Tip: open it, then browser menu → Add to Home screen.") if link else \
            "The call server isn't online yet."

    if cmd in ("/start", "/help"):
        return HELP
    if cmd == "/today":
        return _plugin("college_assistant", action="list_classes", day="today") + "\n\n" + \
            _plugin("college_assistant", action="list_deadlines", days_ahead=2)
    if cmd == "/due":
        return _plugin("college_assistant", action="list_deadlines", days_ahead=num or 14)
    if cmd == "/plan":
        return _plugin("exam_planner", action="today_plan")
    if cmd == "/habits":
        return _plugin("habits", action="status")
    if cmd in ("/att", "/attendance"):
        return _plugin("attendance", action="status", subject=arg)
    if cmd == "/budget":
        return _plugin("expenses", action="summary", period="month")
    if cmd == "/spent":
        m = re.match(r"₹?\s*([\d.,]+)\s*(.*)", arg)
        if not m:
            return "Usage: /spent 120 lunch"
        return _plugin("expenses", action="add", amount=m.group(1), note=m.group(2))
    if cmd == "/call":
        if num:
            return _plugin("phone_call", action="schedule", kind="briefing", delay_minutes=num)
        return _plugin("phone_call", action="call_now", kind="briefing")
    if cmd == "/rescue":
        return _rescue(num or 2)
    if cmd == "/guard":
        from plugins import laptop_guard as lg
        a = arg.lower()
        if a in ("on", "arm"):
            return lg.arm(source="Telegram", delay=0)
        if a in ("off", "disarm"):
            return lg.disarm(source="Telegram")
        return lg.status()
    if cmd == "/photo":
        from plugins import laptop_guard as lg
        img = lg.webcam_jpeg()
        return "" if img and tg.send_photo(img, "Webcam", "webcam.jpg") else "Couldn't open the webcam."
    if cmd == "/screenshot":
        from plugins import laptop_guard as lg
        img = lg.screenshot_png()
        return "" if img and tg.send_photo(img, "Screen", "screen.png") else "Couldn't take a screenshot."
    if cmd == "/pc":
        return _pc_status()
    if cmd == "/lock":
        from plugins import laptop_guard as lg
        return "🔒 Locked." if lg.lock_pc() else "Couldn't lock the PC."
    if cmd in ("/quiz", "/journal", "/checkin"):
        return _plugin("voice_calls", action="call", mode=cmd[1:], topic=arg)
    if cmd == "/line":
        return _plugin("voice_calls", action="status")
    if cmd == "/focus":
        return _plugin("focus_mode", action="start", minutes=num or 25)
    if cmd == "/stop":
        return _plugin("focus_mode", action="stop")
    return "I don't know that command. /help"


def _rescue(minutes: int) -> str:
    try:
        from plugins import phone_call
    except Exception:
        return "The phone call feature isn't installed."
    script = ("Hello? Hey, it's me. Sorry to call out of the blue — something urgent has come up "
              "and I really need you right now. Can you come as soon as possible? "
              "I'll explain when you get here. Please hurry.")
    ok, info = phone_call.schedule_call(minutes, script)
    return (f"🆘 Rescue call set for {info}. Act surprised 😉" if ok else f"Can't call: {info}")


def _pc_status() -> str:
    try:
        import psutil
        b = psutil.sensors_battery()
        batt = (f"{b.percent:.0f}% {'charging' if b.power_plugged else 'on battery'}" if b else "n/a")
        up = time.time() - psutil.boot_time()
        lines = [
            "💻 PC status",
            f"Battery: {batt}",
            f"CPU: {psutil.cpu_percent(interval=0.5):.0f}%  ·  RAM: {psutil.virtual_memory().percent:.0f}%",
            f"Up for: {int(up // 3600)} h {int(up % 3600 // 60)} min",
        ]
        try:
            from plugins import focus_mode
            lines.append("Focus: " + focus_mode._status())
        except Exception:
            pass
        try:
            from plugins import laptop_guard
            lines.append("Guard: " + laptop_guard.status())
        except Exception:
            pass
        return "\n".join(lines)
    except Exception as e:
        return f"Couldn't read PC status: {e}"


# ═════════════════════════════════════════════════════════════════════════════
#  Plain-language messages → the same tools voice uses
# ═════════════════════════════════════════════════════════════════════════════
_TOOLS = ("college_assistant", "attendance", "exam_planner", "habits", "expenses",
          "birthdays", "focus_mode", "phone_call", "laptop_guard", "voice_calls")
_PC_ONLY = ("focus_mode", "laptop_guard")


def _tool_specs() -> list[dict]:
    import importlib
    specs = []
    for name in _TOOLS:
        try:
            if not st.plugin_enabled(name) or (st.is_cloud() and name in _PC_ONLY):
                continue
            p = importlib.import_module(f"plugins.{name}").PLUGIN
            specs.append({"name": p["name"], "description": p["description"],
                          "parameters": p["parameters"].get("properties", {})})
        except Exception:
            continue
    return specs


def _natural(text: str) -> str:
    try:
        from core import gemini
    except Exception:
        return "I can't understand free text right now — try /help."
    try:
        from memory.config_manager import get_user_name
        user = get_user_name() or "the user"
    except Exception:
        user = "the user"
    prompt = (
        f"You are Jarvis, {user}'s assistant, replying on Telegram. Today is "
        f"{datetime.now().strftime('%A %d %B %Y, %H:%M')}.\n"
        "Decide which tool calls (if any) handle this message. Tools:\n"
        + json.dumps(_tool_specs(), ensure_ascii=False)
        + "\n\nReturn ONLY JSON: {\"calls\": [{\"tool\": \"<name>\", \"args\": {...}}], "
          "\"reply\": \"<short reply if no tool fits, else empty>\"}. Use exact tool names "
          "and argument names. Dates as YYYY-MM-DD. Several calls are allowed.\n\n"
        f"Message: {text}"
    )
    plan = gemini.as_json(prompt, tier=gemini.FAST, default=None, timeout_ms=20000)
    if not isinstance(plan, dict):
        return "I couldn't reach Gemini just now. Slash commands still work — /help."
    results = []
    for c in (plan.get("calls") or [])[:5]:
        name = str(c.get("tool", ""))
        if name not in _TOOLS:
            continue
        args = c.get("args") or {}
        if name == "laptop_guard" and args.get("action") == "arm":
            from plugins import laptop_guard as lg
            results.append(lg.arm(source="Telegram", delay=0))
            continue
        results.append(_plugin(name, **args))
    if results:
        return "\n".join(r for r in results if r)
    return str(plan.get("reply") or "Sorry, I didn't get that. /help")


# ═════════════════════════════════════════════════════════════════════════════
#  Photo → notes + flashcards
# ═════════════════════════════════════════════════════════════════════════════
def _photo_notes(file_id: str, caption: str, mime: str = "image/jpeg"):
    try:
        data = tg.download(file_id)
    except Exception as e:
        tg.send(f"Couldn't download the photo: {e}")
        return
    try:
        from core import gemini
        from google.genai import types
        prompt = (
            "This is a photo of a classroom whiteboard, slide or handwritten notes"
            + (f" (context: {caption})" if caption else "") + ". Produce study notes in plain "
            "text (no markdown symbols like ** or #) with these sections:\n"
            "TITLE: one line\nSUMMARY: 2-3 sentences\nKEY POINTS: up to 8 lines starting with •\n"
            "FORMULAS / DEFINITIONS: if any\nFLASHCARDS: 5 lines 'Q: … | A: …'\n"
            "If text is unreadable, say what you can read and what is unclear."
        )
        notes = gemini.text([types.Part.from_bytes(data=data, mime_type=mime), prompt],
                            tier=gemini.SMART, default="", timeout_ms=60000)
    except Exception as e:
        notes = ""
        print(f"[Telegram] vision failed: {e}")
    if not notes:
        tg.send("I couldn't read that photo (Gemini didn't answer). Try again in a minute.")
        return
    notes = re.sub(r"[*#]{1,3}", "", notes).strip()
    NOTES_DIR.mkdir(exist_ok=True)
    title = re.search(r"TITLE:\s*(.+)", notes)
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", (title.group(1) if title else caption or "notes"))[:40].strip("_")
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    (NOTES_DIR / f"{stamp}_{slug}.txt").write_text(notes, encoding="utf-8")
    (NOTES_DIR / f"{stamp}_{slug}.jpg").write_bytes(data)
    tg.send("📝 " + notes + f"\n\n(Saved on your PC: notes/{stamp}_{slug}.txt)")
    bus.log(f"Saved notes from a photo: notes/{stamp}_{slug}.txt")


def _start():
    threading.Thread(target=_poll_loop, name="telegram-poll", daemon=True).start()


if not globals().get("_STARTED") and not st.is_lite():
    _STARTED = True
    _start()
