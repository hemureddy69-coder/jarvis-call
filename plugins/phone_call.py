"""
Phone calls — JARVIS rings your real phone number (through Twilio) and talks.

  * Morning briefing  — weather, today's classes, deadlines, a few headlines
  * Class alerts      — a short call N minutes before each class
  * Deadline alerts   — a call N hours before an assignment / exam is due
  * Evening recap     — tomorrow's classes, what's still pending, tomorrow's weather
  * On demand         — "call me in 20 minutes", "call me at 6 and remind me to …"

The call is one-way: JARVIS speaks, you listen. (Talking back on a phone call
needs a public web server for Twilio to reach — a possible later upgrade.)

COST: Twilio charges per call minute. The trial gives free credit, but trial
calls can only reach numbers you have verified in the Twilio console and play
a short "trial account" notice first. "Max calls per day" below caps spending.

Setup: ⚙ → plugin settings → PHONE CALLS. Fill in the Twilio fields, press
TEST CALL. Credentials are stored in config/api_keys.json (git-ignored).

Needs plugins/_college_store.py in the same folder. Uses `requests` (already in
requirements.txt) — no Twilio SDK required.
"""
from __future__ import annotations

import json
import re
import threading
import time
import traceback
from datetime import date, datetime, timedelta
from pathlib import Path
from xml.sax.saxutils import escape

from plugins import _college_store as college

NS = "phone_call"
BASE_DIR   = Path(__file__).resolve().parent.parent
STATE_FILE = BASE_DIR / "memory" / "phone_call_state.json"

# Automatic class / deadline calls are never placed in this window (the morning
# call already covers anything that falls in it). On-demand calls ignore it.
QUIET_START, QUIET_END = "23:00", "06:30"
CATCH_UP_MINUTES = 60          # PC switched on late? still call if within this
RETRY_AFTER_MINUTES = 5
WAKE_RETRY_MINUTES = 3

# ── settings shown in the app (⚙ → plugin settings) ──────────────────────────
DEFAULTS = {
    "call_method": "free app (ntfy)",
    "account_sid": "", "auth_token": "", "from_number": "", "to_number": "",
    "voice": "Polly.Raveena", "city": "Chennai", "news_topic": "India",
    "morning_enabled": True, "morning_time": "07:30",
    "evening_enabled": True, "evening_time": "21:30",
    "class_alerts": True, "class_lead_minutes": "15",
    "deadline_alerts": True, "deadline_lead_hours": "3",
    "retry_unanswered": True, "max_calls_per_day": "6",
    "wake_up_mode": False, "wake_up_retries": "3", "weekly_report": True,
}


def _test_call(values: dict):
    cfg = {**DEFAULTS, **(values or {})}
    if _method(cfg) != "twilio" and _app_ready():
        from plugins import app_call
        ok, info = app_call.ring("reminder", "This is a test call. If you can hear me, calls work!",
                                 source="manual")
        return ok, ("Ringing your phone through the ntfy app — tap the alert to answer."
                    if ok else info)
    problem = _twilio_problem(cfg)
    if problem:
        return False, problem
    name = _user_name()
    ok, info = _place_call(cfg, f"Hello {name}. This is {_assistant_name()}. "
                                "Your phone calls are set up and working. Goodbye for now.",
                           count_it=False)
    return ok, (f"Calling {cfg['to_number']} — pick up! ({info})" if ok else info)


PLUGIN_SETTINGS = {
    "namespace": NS,
    "title": "PHONE CALLS (TWILIO)",
    "fields": [
        {"key": "call_method", "label": "How to call me", "type": "choice",
         "options": ["free app (ntfy)", "twilio", "app, Twilio if app offline"]},
        {"key": "account_sid", "label": "Twilio Account SID (only for Twilio)", "type": "text", "placeholder": "ACxxxxxxxx…"},
        {"key": "auth_token",  "label": "Twilio Auth Token",  "type": "password"},
        {"key": "from_number", "label": "Twilio phone number (from)", "type": "text", "placeholder": "+1xxxxxxxxxx"},
        {"key": "to_number",   "label": "Your phone number (to)", "type": "text", "placeholder": "+91xxxxxxxxxx"},
        {"key": "voice", "label": "Call voice", "type": "choice",
         "options": ["Polly.Raveena", "Polly.Aditi", "Polly.Kajal-Neural", "Polly.Brian", "Polly.Matthew-Neural"]},
        {"key": "city", "label": "City for weather", "type": "text", "default": "Chennai"},
        {"key": "news_topic", "label": "News topic", "type": "text", "default": "India"},
        {"key": "morning_enabled", "label": "Morning briefing call", "type": "toggle", "default": True},
        {"key": "morning_time", "label": "Morning call time (HH:MM)", "type": "text", "default": "07:30"},
        {"key": "evening_enabled", "label": "Evening recap call", "type": "toggle", "default": True},
        {"key": "evening_time", "label": "Evening call time (HH:MM)", "type": "text", "default": "21:30"},
        {"key": "class_alerts", "label": "Call before classes", "type": "toggle", "default": True},
        {"key": "class_lead_minutes", "label": "Minutes before class", "type": "text", "default": "15"},
        {"key": "deadline_alerts", "label": "Call before deadlines", "type": "toggle", "default": True},
        {"key": "deadline_lead_hours", "label": "Hours before deadline", "type": "text", "default": "3"},
        {"key": "wake_up_mode", "label": "Wake-up mode: keep calling until I answer the morning call",
         "type": "toggle", "default": False},
        {"key": "wake_up_retries", "label": "Wake-up: extra attempts (3 min apart)", "type": "text", "default": "3"},
        {"key": "weekly_report", "label": "Sunday evening = weekly report card", "type": "toggle", "default": True},
        {"key": "retry_unanswered", "label": "Retry once if unanswered", "type": "toggle", "default": True},
        {"key": "max_calls_per_day", "label": "Max calls per day (cost cap)", "type": "text", "default": "6"},
    ],
    "action": {"label": "TEST CALL", "run": _test_call},
}

PLUGIN = {
    "name": "phone_call",
    "description": (
        "Places a REAL phone call to the user's mobile, where the assistant speaks. "
        "Use when the user says 'call me', 'call me in 20 minutes', 'call me at 6 pm "
        "and remind me to …', 'give me my briefing on the phone', 'cancel the call', "
        "or asks which calls are scheduled. Morning briefing, evening recap and "
        "class/deadline calls also happen automatically on schedule. Do NOT use "
        "send_message for this — that sends text, this rings the phone."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "call_now, schedule, list_scheduled, cancel_scheduled"},
            "kind": {"type": "STRING",
                     "description": "briefing (morning-style update), recap (evening-style), "
                                    "or reminder (speak the message). Default reminder if a "
                                    "message is given, otherwise briefing."},
            "message": {"type": "STRING", "description": "What to say / remind about (for reminder calls)"},
            "delay_minutes": {"type": "INTEGER", "description": "For schedule: call this many minutes from now"},
            "at_time": {"type": "STRING", "description": "For schedule: clock time HH:MM (24h) or '6pm'"},
        },
        "required": ["action"],
    },
}

_PLAYER = None
_STATE_LOCK = threading.RLock()


# ═════════════════════════════════════════════════════════════════════════════
#  Tool entry point (voice commands)
# ═════════════════════════════════════════════════════════════════════════════
def run(parameters: dict, player=None, session_memory=None) -> str:
    global _PLAYER
    if player is not None:
        _PLAYER = player
    try:
        action = (parameters.get("action") or "call_now").strip().lower()
        message = (parameters.get("message") or "").strip()
        kind = (parameters.get("kind") or ("reminder" if message else "briefing")).strip().lower()
        if kind not in ("briefing", "recap", "reminder"):
            kind = "reminder" if message else "briefing"
        cfg = _cfg()

        if action in ("list_scheduled", "list", "status"):
            q = _state()["queue"]
            if not q:
                return "No extra calls are scheduled, sir. Your automatic calls are " + _auto_summary(cfg) + "."
            items = "; ".join(f"{_hhmm(datetime.fromisoformat(i['at']))} — "
                              f"{i.get('message') or i['kind']}" for i in q)
            return f"Scheduled calls, sir: {items}."

        if action in ("cancel_scheduled", "cancel"):
            with _STATE_LOCK:
                st = _state()
                n = len(st["queue"])
                st["queue"] = []
                _save_state(st)
            return f"Cancelled {n} scheduled call{'s' if n != 1 else ''}, sir." if n else \
                "There were no scheduled calls to cancel, sir."

        problem = _config_problem(cfg)
        if problem:
            return f"Sir, I can't place calls yet — {problem}"

        if action == "schedule":
            at = _resolve_when(parameters)
            if at is None:
                return "Sir, when should I call? Give me minutes from now or a clock time."
            with _STATE_LOCK:
                st = _state()
                st["queue"].append({"at": at.isoformat(timespec="seconds"),
                                    "kind": kind, "message": message, "attempt": 1})
                _save_state(st)
            return f"I'll call you at {_hhmm(at)}, sir."

        # call_now
        threading.Thread(target=_fire, args=(kind, message, "manual", 1, None),
                         daemon=True).start()
        return "Calling your phone now, sir."
    except Exception as e:
        traceback.print_exc()
        return f"Sir, the phone call plugin failed: {e}"


def schedule_call(minutes: float, script: str, kind: str = "reminder") -> tuple[bool, str]:
    """Public helper for other plugins (e.g. the Telegram /rescue command):
    ring the phone `minutes` from now and say exactly `script`."""
    cfg = _cfg()
    problem = _config_problem(cfg)
    if problem:
        return False, problem
    at = datetime.now() + timedelta(minutes=max(0.0, float(minutes)))
    with _STATE_LOCK:
        st = _state()
        st["queue"].append({"at": at.isoformat(timespec="seconds"), "kind": kind, "message": "",
                            "attempt": 1, "script": script, "source": "scheduled"})
        _save_state(st)
    return True, _hhmm(at)


# ═════════════════════════════════════════════════════════════════════════════
#  Scheduler — runs in the background from launch
# ═════════════════════════════════════════════════════════════════════════════
def _scheduler_loop():
    time.sleep(20)                       # let the app finish booting
    while True:
        try:
            _tick()
        except Exception:
            traceback.print_exc()
        time.sleep(20)


def _tick():
    try:
        from memory.config_manager import get_plugin_enabled
        if not get_plugin_enabled(NS):
            return
    except Exception:
        pass
    from plugins import _store
    if not _store.runs_schedulers():
        return                          # the linked cloud server makes the calls
    cfg = _cfg()
    if _config_problem(cfg):
        return
    now = datetime.now()
    today = date.today()
    tkey = today.isoformat()

    # one-off calls the user asked for (and retries)
    due_items = []
    with _STATE_LOCK:
        st = _state()
        keep = []
        for item in st["queue"]:
            try:
                (due_items if datetime.fromisoformat(item["at"]) <= now else keep).append(item)
            except Exception:
                pass
        if due_items:
            st["queue"] = keep
            _save_state(st)
    for item in due_items:
        _spawn(item["kind"], item.get("message", ""), item.get("source", "scheduled"),
               item.get("attempt", 1), item.get("script"))

    # Everything due in this tick goes into ONE call — three calls in a minute
    # would be annoying and cost three times as much.
    main_kind = None
    for kind, flag, tfield in (("briefing", "morning_enabled", "morning_time"),
                               ("recap", "evening_enabled", "evening_time")):
        if not _truthy(cfg.get(flag)):
            continue
        hhmm = college.parse_time(str(cfg.get(tfield, "")))
        if not hhmm:
            continue
        at = datetime.combine(today, datetime.strptime(hhmm, "%H:%M").time())
        if at <= now < at + timedelta(minutes=CATCH_UP_MINUTES) and _claim(f"{kind}:{tkey}"):
            main_kind = kind

    quiet = _in_quiet(now)
    alerts: list[str] = []

    # before each class
    if _truthy(cfg.get("class_alerts")) and not quiet:
        lead = _int(cfg.get("class_lead_minutes"), 15)
        for c in college.classes_on(today):
            try:
                start = datetime.combine(today, datetime.strptime(c["start"], "%H:%M").time())
            except Exception:
                continue
            if start - timedelta(minutes=lead) <= now < start and _claim(f"class:{tkey}:{c['id']}"):
                mins = max(1, int((start - now).total_seconds() // 60))
                room = f" in {c['room']}" if c.get("room") else ""
                alerts.append(f"{c['subject']} starts in {mins} minutes{room}. Time to head out.")

    # before each deadline
    if _truthy(cfg.get("deadline_alerts")) and not quiet:
        lead = _int(cfg.get("deadline_lead_hours"), 3)
        for d in college.open_deadlines():
            due = college.due_dt(d)
            if due and due - timedelta(hours=lead) <= now < due and _claim(f"deadline:{d['id']}"):
                hrs = (due - now).total_seconds() / 3600
                left = (f"{int(hrs * 60)} minutes" if hrs < 1 else
                        f"about {round(hrs)} hour{'s' if round(hrs) != 1 else ''}")
                subj = f" for {d['subject']}" if d.get("subject") else ""
                alerts.append(f"Heads-up: {d['title']}{subj} is due in {left}. "
                              "If it's already done, tell me to mark it complete.")

    if main_kind:
        _spawn(main_kind, " ".join(alerts), "auto", 1, None)
    elif alerts:
        _spawn("reminder", "", "auto", 1,
               f"{_greet()} {_user_name()}, it's {_assistant_name()}. " + " ".join(alerts))


def _spawn(kind, message, source, attempt, script):
    threading.Thread(target=_fire, args=(kind, message, source, attempt, script),
                     daemon=True).start()


def _fire(kind: str, message: str, source: str, attempt: int, script: str | None):
    """Build the words, ring the phone, watch the result, retry once if missed."""
    cfg = _cfg()
    m = _method(cfg)
    if m == "app" or (m == "app_or_twilio" and _app_ready()):
        from plugins import app_call
        wake = kind == "briefing" and source != "manual" and _truthy(cfg.get("wake_up_mode"))
        extra = None
        if kind == "checkin_followup":
            try:
                from plugins import voice_calls
                extra = voice_calls._last_checkin()
            except Exception:
                pass
        ok, info = app_call.ring(kind, message, source, script=script, wake=wake, extra=extra)
        _log(f"📲 {'Ringing' if ok else 'Ring failed'} ({kind}) in the Jarvis app" + ("" if ok else f": {info}"))
        if not ok:
            try:
                from plugins import _bus
                _bus.phone(f"📞 Couldn't ring you for your {kind} call: {info}")
            except Exception:
                pass
        return
    twiml = None
    if script is None:
        try:                                   # two-way call if the line is up
            from plugins import voice_calls
            twiml = voice_calls.twiml_for(kind, message)
        except ModuleNotFoundError:
            pass
        except Exception as e:
            print(f"[PhoneCall] two-way call unavailable: {e}")
        if twiml is None:
            if kind in ("checkin", "checkin_followup", "journal", "quiz"):
                script = (f"{_greet()} {_user_name()}. This was meant to be your {kind.replace('_', ' ')} "
                          "call, but my two-way line isn't up right now, so I can't hear you. "
                          "Message me on Telegram instead. Talk soon.")
            else:
                script = _build_script(kind, message, cfg)
    ok, info = _place_call(cfg, script or "", twiml=twiml)
    _log(f"📞 {('Calling' if ok else 'Call failed')} ({kind}): {info}")
    if not ok:
        try:
            from plugins import _bus
            _bus.phone(f"📞 Couldn't place your {kind} call: {info}\n\n" + (script or "")[:3500])
        except Exception:
            pass
    wake = kind == "briefing" and source != "manual" and _truthy(cfg.get("wake_up_mode"))
    max_attempts = (1 + _int(cfg.get("wake_up_retries"), 3)) if wake else \
        (2 if _truthy(cfg.get("retry_unanswered")) else 1)
    if not ok or attempt >= max_attempts:
        return
    status, duration = _wait_for_outcome(cfg, info)
    # In wake-up mode a call you hang up on within 15 s doesn't count as answered.
    missed = status in ("no-answer", "busy", "failed", "canceled") or \
        (wake and status == "completed" and duration < 15)
    if missed:
        at = datetime.now() + timedelta(minutes=WAKE_RETRY_MINUTES if wake else RETRY_AFTER_MINUTES)
        with _STATE_LOCK:
            st = _state()
            st["queue"].append({"at": at.isoformat(timespec="seconds"), "kind": kind,
                                "message": message, "attempt": attempt + 1, "script": script,
                                "source": source})
            _save_state(st)
        _log(f"📞 Call was {status} — trying again at {_hhmm(at)}.")


# ═════════════════════════════════════════════════════════════════════════════
#  What JARVIS says
# ═════════════════════════════════════════════════════════════════════════════
def gather_facts(kind: str, message: str, cfg: dict) -> tuple[list[str], bool]:
    """Everything a briefing / recap should mention. Also used by two-way calls."""
    today = date.today()
    facts = [f"Now: {datetime.now().strftime('%A %d %B %Y, %I:%M %p')}"]
    if message:
        facts.append("URGENT, say this first: " + message)
    if kind == "briefing":
        cls = college.classes_on(today)
        facts.append("Classes today: " + ("; ".join(college.fmt_class(c) for c in cls) if cls else "none"))
        w = _weather(cfg.get("city") or "Chennai", tomorrow=False)
        if w:
            facts.append("Weather today: " + w)
        news = _headlines(cfg.get("news_topic") or "")
        if news:
            facts.append("Headlines (pick the 3 most important, one short line each):\n" + news)
    else:
        tmr = today + timedelta(days=1)
        cls = college.classes_on(tmr)
        facts.append("Classes tomorrow: " + ("; ".join(college.fmt_class(c) for c in cls) if cls else "none"))
        w = _weather(cfg.get("city") or "Chennai", tomorrow=True)
        if w:
            facts.append("Weather tomorrow: " + w)
    dl = college.open_deadlines(within_days=4)
    facts.append("Pending deadlines: " + ("; ".join(college.fmt_deadline(d) for d in dl) if dl else "none"))

    # Sunday evening = weekly report card
    weekly = kind == "recap" and today.weekday() == 6 and _truthy(cfg.get("weekly_report"))
    if weekly:
        facts += _deadline_week_facts()
    facts += _plugin_facts("weekly" if weekly else kind)
    return facts, weekly


def _build_script(kind: str, message: str, cfg: dict) -> str:
    name = _user_name()
    if kind == "reminder":
        return f"{_greet()} {name}. This is {_assistant_name()} with your reminder: {message or 'you asked me to call you'}."

    facts, weekly = gather_facts(kind, message, cfg)
    prompt = (
        f"You are {_assistant_name()}, {name}'s personal assistant, speaking on a PHONE CALL. "
        "Write exactly what you will say for the "
        + ("morning briefing" if kind == "briefing" else
           "Sunday WEEKLY REPORT CARD (give an honest grade for the week and one goal for next week)"
           if weekly else "evening recap") + ". "
        "Rules: plain spoken English for text-to-speech, no markdown, no lists, no emojis, no "
        f"URLs; {'120 to 200' if weekly else '80 to 160'} words; warm and brisk like a capable "
        f"assistant; start with a greeting using the name {name}; anything marked URGENT comes "
        "first; mention every class, deadline and exam topic; say times naturally; say rupee "
        "amounts as 'rupees'; if the chance of rain is 50% or more, tell them to carry an umbrella; "
        "skip anything that says none; "
        + ("end by wishing a good day." if kind == "briefing" else
           "end by suggesting one thing to finish tonight if anything is pending, then good night.")
        + "\n\nFacts:\n" + "\n".join(facts)
    )
    text = ""
    try:
        from core import gemini
        text = gemini.text(prompt, default="", timeout_ms=25000)
    except Exception as e:
        print(f"[PhoneCall] Gemini unavailable, using the plain script: {e}")
    text = re.sub(r"[*#_`>\[\]]", "", text or "").strip()
    if len(text) > 40:
        return text
    # plain fallback — still useful when Gemini is out of quota
    lines = [f"{_greet()} {name}, it's {_assistant_name()}."]
    if message:
        lines.append(message)
    for f in facts[1:]:
        if f.startswith(("Headlines", "URGENT")) or f.endswith(": none"):
            continue
        line = re.sub(r"\s*\([^)]*\)$", "", f.split("\n")[0]).rstrip(".")
        lines.append(line + ".")
    lines.append("Have a great day." if kind == "briefing" else "Good night.")
    return " ".join(lines)


_FACT_PLUGINS = ("attendance", "exam_planner", "habits", "focus_mode", "expenses", "birthdays",
                 "voice_calls")


def _plugin_facts(kind: str) -> list[str]:
    """Each feature plugin may expose call_facts(kind) -> list[str]."""
    import importlib
    out = []
    for name in _FACT_PLUGINS:
        try:
            from memory.config_manager import get_plugin_enabled
            if not get_plugin_enabled(name):
                continue
        except Exception:
            pass
        try:
            mod = importlib.import_module(f"plugins.{name}")
            out += [f for f in (mod.call_facts(kind) or []) if f]
        except ModuleNotFoundError:
            continue
        except Exception as e:
            print(f"[PhoneCall] {name} facts failed: {e}")
    return out


def _deadline_week_facts() -> list[str]:
    now = datetime.now()
    week_ago = now - timedelta(days=7)
    done, missed = [], []
    for d in college.load()["deadlines"]:
        due = college.due_dt(d)
        if d.get("done"):
            try:
                if datetime.fromisoformat(d.get("done_at", "")) >= week_ago:
                    done.append(d["title"])
            except ValueError:
                pass
        elif due and week_ago <= due < now:
            missed.append(d["title"])
    return [f"Deadlines finished this week: {', '.join(done) or 'none'}",
            f"Deadlines missed this week: {', '.join(missed) or 'none'}"]


def _weather(city: str, tomorrow: bool) -> str:
    """wttr.in — free, no key."""
    try:
        import requests
        r = requests.get(f"https://wttr.in/{city}", params={"format": "j1"}, timeout=8,
                         headers={"User-Agent": "curl/8"})
        j = r.json()
        day = j["weather"][1 if tomorrow else 0]
        desc = day["hourly"][4]["weatherDesc"][0]["value"]
        rain = max(int(h.get("chanceofrain", 0)) for h in day["hourly"])
        out = f"{desc}, {day['mintempC']} to {day['maxtempC']}°C, {rain}% chance of rain"
        if not tomorrow:
            out = f"now {j['current_condition'][0]['temp_C']}°C; " + out
        return f"{city}: {out}"
    except Exception as e:
        print(f"[PhoneCall] weather failed: {e}")
        return ""


def _headlines(topic: str) -> str:
    try:
        from actions.web_search import _news
        txt = _news(topic) or ""
        return "" if txt.startswith("No news") else txt[:1500]
    except Exception as e:
        print(f"[PhoneCall] news failed: {e}")
        return ""


# ═════════════════════════════════════════════════════════════════════════════
#  Twilio (plain REST through `requests`)
# ═════════════════════════════════════════════════════════════════════════════
_TWILIO_HINTS = {
    21219: "your number isn't verified — on a trial account add it under Phone Numbers → Verified Caller IDs.",
    21215: "calls to this country are blocked — enable India under Voice → Settings → Geo permissions.",
    21212: "the 'from' number isn't valid — use the Twilio number from your console, with +country code.",
    21211: "the 'to' number isn't valid — use +91 followed by your 10-digit number.",
    20003: "the Account SID or Auth Token is wrong.",
    21210: "the 'from' number isn't a verified Twilio number on this account.",
}


def _place_call(cfg: dict, script: str, count_it: bool = True, twiml: str | None = None) -> tuple[bool, str]:
    if count_it and not _within_daily_cap(cfg):
        return False, "daily call limit reached — skipped to save credit."
    try:
        import requests
    except ImportError:
        return False, "the 'requests' package is missing (pip install requests)."
    if not twiml:
        voice = cfg.get("voice") or "Polly.Raveena"
        paras = [p.strip() for p in re.split(r"\n\s*\n", script.strip()) if p.strip()][:12]
        body = "".join(f'<Say voice="{escape(voice)}">{escape(p)}</Say><Pause length="1"/>' for p in paras)
        twiml = f'<Response><Pause length="1"/>{body}</Response>'
        if len(twiml) > 3900:                   # Twilio's inline TwiML limit is 4000 chars
            short = escape(script[:3000])
            twiml = f'<Response><Pause length="1"/><Say voice="{escape(voice)}">{short}</Say></Response>'
    sid = cfg["account_sid"].strip()
    try:
        r = requests.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Calls.json",
            auth=(sid, cfg["auth_token"].strip()),
            data={"To": cfg["to_number"].strip(), "From": cfg["from_number"].strip(),
                  "Twiml": twiml, "Timeout": 30},
            timeout=20,
        )
        j = r.json() if r.content else {}
    except Exception as e:
        return False, f"couldn't reach Twilio: {e}"
    if r.status_code >= 300:
        code = j.get("code")
        return False, f"Twilio error {code}: " + _TWILIO_HINTS.get(code, j.get("message", r.text[:160]))
    if count_it:
        _count_call()
    return True, j.get("sid", "call queued")


def _wait_for_outcome(cfg: dict, call_sid: str, max_wait: int = 420) -> tuple[str, int]:
    """Poll Twilio until the call ends. Returns (status, seconds_talked)."""
    try:
        import requests
        sid = cfg["account_sid"].strip()
        deadline = time.time() + max_wait
        status = ""
        while time.time() < deadline:
            time.sleep(10)
            r = requests.get(f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Calls/{call_sid}.json",
                             auth=(sid, cfg["auth_token"].strip()), timeout=15)
            j = r.json()
            status = j.get("status", "")
            if status in ("completed", "busy", "no-answer", "failed", "canceled"):
                return status, _int(j.get("duration"), 0)
        return status, 0
    except Exception:
        return "", 0


# ═════════════════════════════════════════════════════════════════════════════
#  Helpers
# ═════════════════════════════════════════════════════════════════════════════
def _cfg() -> dict:
    try:
        from memory.config_manager import get_plugin_config
        stored = get_plugin_config(NS) or {}
    except Exception:
        stored = {}
    return {**DEFAULTS, **{k: v for k, v in stored.items() if v not in (None, "")}}


def _method(cfg: dict) -> str:
    m = str(cfg.get("call_method") or "").lower()
    if m.startswith("twilio"):
        return "twilio"
    if "twilio if" in m:
        return "app_or_twilio"
    return "app"


def _app_ready() -> bool:
    try:
        from plugins import app_call
        return app_call.ready()
    except Exception:
        return False


def _config_problem(cfg: dict) -> str:
    """'' when a call can be placed right now with the chosen method."""
    m = _method(cfg)
    if m == "twilio":
        return _twilio_problem(cfg)
    if _app_ready():
        return ""
    if m == "app_or_twilio" and not _twilio_problem(cfg):
        return ""
    return "the free call server isn't online yet (see CLOUD_SETUP.md)."


def _twilio_problem(cfg: dict) -> str:
    if not cfg.get("account_sid", "").strip().startswith("AC"):
        return "add your Twilio Account SID (starts with AC) in the phone-call settings."
    if not cfg.get("auth_token", "").strip():
        return "add your Twilio Auth Token in the phone-call settings."
    for key, label in (("from_number", "Twilio number"), ("to_number", "your phone number")):
        if not re.fullmatch(r"\+\d{8,15}", cfg.get(key, "").replace(" ", "")):
            return f"set {label} with the country code, like +91XXXXXXXXXX."
    return ""


def _state() -> dict:
    from plugins import _store
    with _STATE_LOCK:
        st = _store.load("phone_call_state", {})      # shared with the GitHub runner
        st.setdefault("fired", {})
        st.setdefault("queue", [])
        st.setdefault("count", {"date": "", "n": 0})
        return st


def _save_state(st: dict) -> None:
    with _STATE_LOCK:
        cutoff = (date.today() - timedelta(days=14)).isoformat()
        st["fired"] = {k: v for k, v in st["fired"].items() if v >= cutoff}
        from plugins import _store
        _store.save("phone_call_state", st)


def _claim(key: str) -> bool:
    """True exactly once per key, across restarts."""
    with _STATE_LOCK:
        st = _state()
        if key in st["fired"]:
            return False
        st["fired"][key] = date.today().isoformat()
        _save_state(st)
        return True


def _within_daily_cap(cfg: dict) -> bool:
    st = _state()
    c = st["count"]
    return c.get("date") != date.today().isoformat() or c.get("n", 0) < _int(cfg.get("max_calls_per_day"), 6)


def _count_call() -> None:
    with _STATE_LOCK:
        st = _state()
        today = date.today().isoformat()
        if st["count"].get("date") != today:
            st["count"] = {"date": today, "n": 0}
        st["count"]["n"] += 1
        _save_state(st)


def _resolve_when(p: dict) -> datetime | None:
    now = datetime.now()
    try:
        mins = int(p.get("delay_minutes") or 0)
    except (TypeError, ValueError):
        mins = 0
    if mins > 0:
        return now + timedelta(minutes=mins)
    hhmm = college.parse_time(str(p.get("at_time") or ""))
    if hhmm:
        at = datetime.combine(date.today(), datetime.strptime(hhmm, "%H:%M").time())
        return at if at > now else at + timedelta(days=1)
    return None


def _in_quiet(now: datetime) -> bool:
    t = now.strftime("%H:%M")
    return t >= QUIET_START or t < QUIET_END


def _auto_summary(cfg: dict) -> str:
    parts = []
    if _truthy(cfg.get("morning_enabled")):
        parts.append(f"morning at {cfg.get('morning_time')}")
    if _truthy(cfg.get("evening_enabled")):
        parts.append(f"evening at {cfg.get('evening_time')}")
    if _truthy(cfg.get("class_alerts")):
        parts.append(f"{cfg.get('class_lead_minutes')} minutes before classes")
    if _truthy(cfg.get("deadline_alerts")):
        parts.append(f"{cfg.get('deadline_lead_hours')} hours before deadlines")
    return ", ".join(parts) or "all switched off"


def _greet() -> str:
    h = datetime.now().hour
    return "Good morning" if h < 12 else ("Good afternoon" if h < 17 else "Good evening")


def _user_name() -> str:
    try:
        from memory.config_manager import get_user_name
        return get_user_name() or "sir"
    except Exception:
        return "sir"


def _assistant_name() -> str:
    try:
        from memory.config_manager import get_assistant_name
        return get_assistant_name() or "Jarvis"
    except Exception:
        return "Jarvis"


def _hhmm(dt: datetime) -> str:
    return dt.strftime("%I:%M %p").lstrip("0")


def _int(v, default: int) -> int:
    try:
        return max(0, int(str(v).strip()))
    except (TypeError, ValueError):
        return default


def _truthy(v) -> bool:
    return v is True or str(v).strip().lower() in ("true", "1", "yes", "on")


def _log(msg: str) -> None:
    print(f"[PhoneCall] {msg}")
    if _PLAYER is not None:
        try:
            _PLAYER.write_log(f"JARVIS: {msg}")
        except Exception:
            pass


# Start the scheduler once, when the plugin loader imports this file.
import os as _os
if not globals().get("_SCHEDULER_STARTED") and _os.environ.get("JARVIS_ROLE") != "lite":
    _SCHEDULER_STARTED = True
    threading.Thread(target=_scheduler_loop, name="phone-call-scheduler", daemon=True).start()
