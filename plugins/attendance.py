"""
Attendance tracker — keeps you above your college's minimum (default 75%).

  "I attended DSA today" / "I missed OS" / "OS was cancelled"
  "I attended all my classes today"
  "What's my attendance?" / "How many DSA classes can I skip?"

After each class in your timetable ends, Jarvis asks whether you went (once).
Attendance warnings are also read out on the morning / evening / weekly calls.
Needs _college_store.py, _store.py and _bus.py in plugins/.
"""
from __future__ import annotations

import math
import re
from datetime import date, datetime, timedelta

from plugins import _bus as bus
from plugins import _college_store as college
from plugins import _store as st

NAME = "attendance"
DEFAULT = {"records": [], "asked": {}}

PLUGIN_SETTINGS = {
    "namespace": NAME,
    "title": "ATTENDANCE",
    "fields": [
        {"key": "threshold", "label": "Minimum attendance %", "type": "text", "default": "75"},
        {"key": "ask_after_class", "label": "Ask me after each class", "type": "toggle", "default": True},
    ],
}

PLUGIN = {
    "name": "attendance",
    "description": (
        "Tracks the user's class ATTENDANCE. Use when the user says they attended, "
        "missed/bunked/skipped, or had a cancelled class, says they attended all classes "
        "today, or asks for their attendance percentage, how many classes they can skip, "
        "or how many they must attend to reach the minimum. Not for the timetable itself "
        "(use college_assistant for that)."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "mark, mark_all_today, status, undo_last"},
            "subject": {"type": "STRING", "description": "Subject name, e.g. 'DSA'. Empty = all subjects for status"},
            "status": {"type": "STRING", "description": "present, absent or cancelled"},
            "date": {"type": "STRING", "description": "Date of the class (default today)"},
        },
        "required": ["action"],
    },
}


def run(parameters: dict, player=None, session_memory=None) -> str:
    try:
        a = (parameters.get("action") or "status").lower().strip()
        if a == "mark":
            return _mark(parameters)
        if a == "mark_all_today":
            return _mark_all(parameters)
        if a == "undo_last":
            return _undo()
        return _status(parameters.get("subject", ""))
    except Exception as e:
        return f"Sir, the attendance tracker hit an error: {e}"


# ── recording ────────────────────────────────────────────────────────────────
def _norm_status(s: str) -> str | None:
    s = (s or "").lower()
    has = lambda *ws: any(re.search(rf"\b{w}", s) for w in ws)
    if has("cancel", "holiday", "no class", "off"):
        return "cancelled"
    if has("absent", "miss", "bunk", "skip", "didn't", "did not", "no$", "not"):
        return "absent"
    if has("present", "attend", "went", "yes", "there", "go"):
        return "present"
    return None


def _subject_match(name: str) -> str:
    """Use the timetable's spelling of a subject when the user says it loosely."""
    q = (name or "").strip().lower()
    names = {c["subject"] for c in college.load()["classes"]}
    for n in names:
        if n.lower() == q:
            return n
    for n in names:
        if q and (q in n.lower() or n.lower() in q):
            return n
    for n in names:                       # "OS" -> "Operating Systems"
        initials = "".join(w[0] for w in n.lower().split() if w)
        if q and len(initials) > 1 and q.replace(".", "").replace(" ", "") == initials:
            return n
    # reuse the spelling already used in past records
    for r in st.load(NAME, DEFAULT)["records"]:
        if r["subject"].lower() == q:
            return r["subject"]
    return name.strip()


def _record(subject: str, status: str, day: date) -> None:
    with st.lock():
        data = st.load(NAME, DEFAULT)
        data["records"] = [r for r in data["records"]
                           if not (r["subject"].lower() == subject.lower() and r["date"] == day.isoformat())]
        data["records"].append({"date": day.isoformat(), "subject": subject, "status": status})
        st.save(NAME, data)


def _mark(p: dict) -> str:
    subject = _subject_match(p.get("subject", ""))
    status = _norm_status(p.get("status", ""))
    if not subject or not status:
        return "Sir, tell me the subject and whether you were present, absent, or it was cancelled."
    day = college.parse_date(p.get("date", "")) or date.today()
    _record(subject, status, day)
    if status == "cancelled":
        return f"Noted, {subject} was cancelled. It won't count against you, sir."
    return f"Marked {status} for {subject}. " + _one_line(subject)


def _mark_all(p: dict) -> str:
    status = _norm_status(p.get("status", "present")) or "present"
    day = college.parse_date(p.get("date", "")) or date.today()
    classes = college.classes_on(day)
    if not classes:
        return "Sir, there are no classes in your timetable for that day."
    for c in classes:
        _record(c["subject"], status, day)
    return f"Marked {status} for {st.plural(len(classes), 'class', 'classes')}, sir."


def _undo() -> str:
    with st.lock():
        data = st.load(NAME, DEFAULT)
        if not data["records"]:
            return "Nothing to undo, sir."
        r = data["records"].pop()
        st.save(NAME, data)
    return f"Removed the {r['status']} entry for {r['subject']} on {r['date']}, sir."


# ── maths ────────────────────────────────────────────────────────────────────
def _threshold() -> float:
    return max(1, min(100, st.to_int(st.setting(NAME, "threshold", 75), 75))) / 100


def stats() -> dict[str, dict]:
    out: dict[str, dict] = {}
    spelling: dict[str, str] = {}
    for r in st.load(NAME, DEFAULT)["records"]:
        key = spelling.setdefault(r["subject"].lower(), r["subject"])
        s = out.setdefault(key, {"present": 0, "absent": 0})
        if r["status"] in ("present", "absent"):
            s[r["status"]] += 1
    t = _threshold()
    for s in out.values():
        total = s["present"] + s["absent"]
        s["total"] = total
        s["pct"] = 100.0 * s["present"] / total if total else 100.0
        s["can_skip"] = max(0, math.floor(s["present"] / t - total)) if total else 0
        need = math.ceil((t * total - s["present"]) / (1 - t)) if t < 1 else 0
        s["must_attend"] = max(0, need)
    return out


def _one_line(subject: str) -> str:
    s = next((v for k, v in stats().items() if k.lower() == subject.lower()), None)
    if not s or not s["total"]:
        return ""
    line = f"{subject} is at {s['pct']:.0f} percent"
    if s["must_attend"]:
        return line + f" — below {int(_threshold() * 100)}. Attend the next {s['must_attend']} in a row to recover."
    if s["can_skip"]:
        return line + f". You could miss {st.plural(s['can_skip'], 'more class', 'more classes')} and stay safe."
    return line + ". No room to skip right now."


def _status(subject: str) -> str:
    all_stats = stats()
    if not all_stats:
        return "No attendance recorded yet, sir. Tell me after each class whether you went."
    if subject:
        name = _subject_match(subject)
        return _one_line(name) or f"Sir, I have no attendance for {name} yet."
    parts = [_one_line(n) for n in sorted(all_stats)]
    present = sum(s["present"] for s in all_stats.values())
    total = sum(s["total"] for s in all_stats.values())
    overall = f"Overall {100 * present / total:.0f} percent. " if total else ""
    return overall + " ".join(p for p in parts if p)


# ── ask after each class ─────────────────────────────────────────────────────
def _tick():
    if st.is_cloud() and st.desktop_online():
        return          # the PC is on — it asks out loud instead of via Telegram
    if not st.plugin_enabled(NAME) or not st.truthy(st.setting(NAME, "ask_after_class", True)):
        return
    now = datetime.now()
    today = date.today()
    data = st.load(NAME, DEFAULT)
    recorded = {r["subject"].lower() for r in data["records"] if r["date"] == today.isoformat()}
    for c in college.classes_on(today):
        try:
            start = datetime.combine(today, datetime.strptime(c["start"], "%H:%M").time())
            end = (datetime.combine(today, datetime.strptime(c["end"], "%H:%M").time())
                   if c.get("end") else start + timedelta(minutes=50))
        except Exception:
            continue
        key = f"{today.isoformat()}:{c['id']}"
        if c["subject"].lower() in recorded or key in data["asked"]:
            continue
        if end + timedelta(minutes=5) <= now <= end + timedelta(hours=3):
            with st.lock():
                data = st.load(NAME, DEFAULT)
                data["asked"][key] = True
                if len(data["asked"]) > 300:
                    data["asked"] = dict(list(data["asked"].items())[-150:])
                st.save(NAME, data)
            bus.say(f"Ask the user whether they attended {c['subject']} just now, so "
                    "attendance can be recorded (present, absent or cancelled).",
                    phone_text=f"✅ Did you attend {c['subject']}? Reply e.g. "
                               f"\"attended {c['subject']}\", \"missed {c['subject']}\" "
                               f"or \"{c['subject']} was cancelled\".")
            break       # one question at a time


# ── facts for the phone calls ────────────────────────────────────────────────
def call_facts(kind: str) -> list[str]:
    s = stats()
    if not s:
        return []
    low = [f"{n} {v['pct']:.0f}% (must attend next {v['must_attend']})"
           for n, v in sorted(s.items()) if v["must_attend"]]
    if kind == "weekly":
        return ["Attendance by subject: " + "; ".join(f"{n} {v['pct']:.0f}%" for n, v in sorted(s.items()))]
    if kind == "recap":
        today = date.today().isoformat()
        done = {r["subject"].lower() for r in st.load(NAME, DEFAULT)["records"] if r["date"] == today}
        missing = [c["subject"] for c in college.classes_on(date.today()) if c["subject"].lower() not in done]
        out = []
        if missing:
            out.append("Attendance not yet recorded today for: " + ", ".join(missing) + " (ask them to tell you)")
        if low:
            out.append(f"Below {int(_threshold() * 100)}% attendance: " + "; ".join(low))
        return out
    return [f"Attendance warning (below {int(_threshold() * 100)}%): " + "; ".join(low)] if low else []


st.start_loop(NAME, _tick, every_s=60)
