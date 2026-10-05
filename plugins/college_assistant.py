"""
College assistant — timetable and assignment / exam deadlines, by voice.

  "Add DSA class on Monday at 9 in room A101"
  "What classes do I have tomorrow?"
  "What's my next class?"
  "I have an OS assignment due Friday at 11pm"
  "What's due this week?"
  "Mark the OS assignment as done"

Data: memory/college.json (shared with phone_call.py, which uses it to call you
before classes and deadlines and to build the morning / evening calls).
Needs plugins/_college_store.py in the same folder.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from plugins import _college_store as store

PLUGIN = {
    "name": "college_assistant",
    "description": (
        "Manages the user's college TIMETABLE and assignment/exam/project DEADLINES. "
        "Use for: adding or removing a class from the weekly timetable, asking what "
        "classes are today/tomorrow/on a day or this week, asking for the next class, "
        "adding a deadline (assignment, lab record, exam, project, quiz), listing "
        "what's due, and marking a deadline done. Do NOT use reminder tools for "
        "classes or assignments — use this. Pass dates as YYYY-MM-DD when you know "
        "them; words like 'tomorrow' or 'friday' are also accepted."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": (
                    "One of: add_class, remove_class, list_classes, next_class, "
                    "add_deadline, list_deadlines, complete_deadline, remove_deadline"
                ),
            },
            "subject": {"type": "STRING", "description": "Course/subject name, e.g. 'DSA'"},
            "day": {
                "type": "STRING",
                "description": "For classes: weekday (monday..sunday), 'today', 'tomorrow', "
                               "or 'week' for list_classes of the whole week",
            },
            "start": {"type": "STRING", "description": "Class start time, e.g. '09:00' or '2pm'"},
            "end": {"type": "STRING", "description": "Class end time (optional)"},
            "room": {"type": "STRING", "description": "Room or lab (optional)"},
            "title": {"type": "STRING", "description": "Deadline title, e.g. 'OS assignment 3'"},
            "due_date": {"type": "STRING", "description": "Deadline date, YYYY-MM-DD or 'friday'"},
            "due_time": {"type": "STRING", "description": "Deadline time, default 23:59"},
            "days_ahead": {"type": "INTEGER", "description": "For list_deadlines: window in days (default 7)"},
        },
        "required": ["action"],
    },
}


def run(parameters: dict, player=None, session_memory=None) -> str:
    try:
        action = (parameters.get("action") or "").strip().lower()
        handler = _ACTIONS.get(action)
        if not handler:
            return ("Sir, I can add or remove classes, list your timetable, tell you the "
                    "next class, and add, list or complete deadlines.")
        msg = handler(parameters)
    except Exception as e:
        msg = f"Sir, the college assistant hit an error: {e}"
    if player:
        try:
            player.write_log(f"JARVIS: {msg}")
        except Exception:
            pass
    return msg


# ── classes ──────────────────────────────────────────────────────────────────
def _add_class(p: dict) -> str:
    subject = (p.get("subject") or "").strip()
    day = store.parse_day(p.get("day", ""))
    start = store.parse_time(p.get("start", ""))
    if not (subject and day and start):
        return "Sir, I need the subject, the weekday and the start time for that class."
    end = store.parse_time(p.get("end", "")) or ""
    data = store.load()
    for c in data["classes"]:
        if c["day"] == day and c["start"] == start:
            c.update(subject=subject, end=end or c.get("end", ""), room=p.get("room") or c.get("room", ""))
            store.save(data)
            return f"Updated your {day.title()} {start} slot to {subject}, sir."
    data["classes"].append({"id": store.new_id(), "subject": subject, "day": day,
                            "start": start, "end": end, "room": (p.get("room") or "").strip()})
    store.save(data)
    return f"Added {subject} on {day.title()}s at {store._spoken_time(start)}, sir."


def _remove_class(p: dict) -> str:
    subject = (p.get("subject") or "").strip().lower()
    day = store.parse_day(p.get("day", ""))
    start = store.parse_time(p.get("start", ""))
    data = store.load()
    keep, gone = [], []
    for c in data["classes"]:
        match = ((not subject or subject in c["subject"].lower())
                 and (not day or c["day"] == day)
                 and (not start or c["start"] == start))
        (gone if match and (subject or day or start) else keep).append(c)
    if not gone:
        return "Sir, I couldn't find that class in your timetable."
    data["classes"] = keep
    store.save(data)
    return f"Removed {len(gone)} class{'es' if len(gone) > 1 else ''} from your timetable, sir."


def _list_classes(p: dict) -> str:
    raw = (p.get("day") or "today").strip().lower()
    if raw in ("week", "this week", "all"):
        parts = []
        for i, d in enumerate(store.DAYS):
            cls = [c for c in store.load()["classes"] if c["day"] == d]
            if cls:
                cls.sort(key=lambda c: c["start"])
                parts.append(f"{d.title()}: " + ", ".join(store.fmt_class(c) for c in cls))
        return ("Your week, sir. " + ". ".join(parts) + ".") if parts else \
            "Your timetable is empty, sir. Tell me your classes and I'll remember them."
    day = store.parse_day(raw) or store.DAYS[date.today().weekday()]
    target = date.today() + timedelta(days=(store.DAYS.index(day) - date.today().weekday()) % 7)
    cls = store.classes_on(target)
    label = "today" if target == date.today() else ("tomorrow" if target == date.today() + timedelta(days=1) else f"on {day.title()}")
    if not cls:
        return f"No classes {label}, sir."
    return f"You have {len(cls)} class{'es' if len(cls) > 1 else ''} {label}: " + \
        "; ".join(store.fmt_class(c) for c in cls) + "."


def _next_class(p: dict) -> str:
    now = datetime.now()
    for offset in range(8):
        d = date.today() + timedelta(days=offset)
        for c in store.classes_on(d):
            start = datetime.combine(d, datetime.strptime(c["start"], "%H:%M").time())
            if start > now:
                mins = int((start - now).total_seconds() // 60)
                when = (f"in {mins} minutes" if mins < 90 else
                        f"at {store._spoken_time(c['start'])}" + ("" if offset == 0 else
                        (" tomorrow" if offset == 1 else f" on {d.strftime('%A')}")))
                room = f" in {c['room']}" if c.get("room") else ""
                return f"Your next class is {c['subject']}{room}, {when}, sir."
    return "You have no upcoming classes in your timetable, sir."


# ── deadlines ────────────────────────────────────────────────────────────────
def _add_deadline(p: dict) -> str:
    title = (p.get("title") or p.get("subject") or "").strip()
    due_d = store.parse_date(p.get("due_date", ""))
    if not (title and due_d):
        return "Sir, I need what the deadline is and the date it's due."
    due_t = store.parse_time(p.get("due_time", ""), "23:59")
    due = datetime.combine(due_d, datetime.strptime(due_t, "%H:%M").time())
    data = store.load()
    data["deadlines"].append({"id": store.new_id(), "title": title,
                              "subject": (p.get("subject") or "").strip() if p.get("title") else "",
                              "due": due.isoformat(timespec="minutes"), "done": False})
    store.save(data)
    return f"Noted, sir: {store.fmt_deadline(data['deadlines'][-1])}. I'll call you before it's due."


def _list_deadlines(p: dict) -> str:
    try:
        days = int(p.get("days_ahead") or 7)
    except (TypeError, ValueError):
        days = 7
    items = store.open_deadlines(within_days=days)
    if not items:
        return f"Nothing due in the next {days} days, sir."
    return f"{len(items)} pending, sir: " + "; ".join(store.fmt_deadline(d) for d in items) + "."


def _find_deadlines(p: dict, data: dict) -> list[dict]:
    q = (p.get("title") or p.get("subject") or "").strip().lower()
    if not q:
        return []
    words = [w for w in q.split() if len(w) > 1]
    hits = []
    for d in data["deadlines"]:
        hay = f"{d.get('title', '')} {d.get('subject', '')}".lower()
        if q in hay or (words and all(w in hay for w in words)):
            hits.append(d)
    return hits


def _complete_deadline(p: dict) -> str:
    data = store.load()
    hits = [d for d in _find_deadlines(p, data) if not d.get("done")]
    if not hits:
        return "Sir, I couldn't find an open deadline matching that."
    for d in hits:
        d["done"] = True
        d["done_at"] = datetime.now().isoformat(timespec="minutes")
    store.save(data)
    return f"Marked {', '.join(d['title'] for d in hits)} as done. Well done, sir."


def _remove_deadline(p: dict) -> str:
    data = store.load()
    hits = _find_deadlines(p, data)
    if not hits:
        return "Sir, I couldn't find that deadline."
    ids = {d["id"] for d in hits}
    data["deadlines"] = [d for d in data["deadlines"] if d["id"] not in ids]
    store.save(data)
    return f"Removed {len(hits)} deadline{'s' if len(hits) > 1 else ''}, sir."


_ACTIONS = {
    "add_class": _add_class,
    "remove_class": _remove_class,
    "list_classes": _list_classes,
    "next_class": _next_class,
    "add_deadline": _add_deadline,
    "list_deadlines": _list_deadlines,
    "complete_deadline": _complete_deadline,
    "remove_deadline": _remove_deadline,
}
