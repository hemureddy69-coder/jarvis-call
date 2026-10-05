"""
Habit streaks.

  "Track a new habit: gym" / "Add habit read 20 pages"
  "I went to the gym" / "Done with reading today"
  "How are my habits?" / "What's my gym streak?"
  "Remove the habit meditation"

The evening call reminds you which habits are still open and which streaks
are about to break; the Sunday call gives the weekly completion rate.
"""
from __future__ import annotations

from datetime import date, timedelta

from plugins import _college_store as college
from plugins import _store as st

NAME = "habits"
DEFAULT = {"habits": [], "log": {}}

PLUGIN = {
    "name": "habits",
    "description": (
        "Daily habit tracker with streaks. Use when the user wants to start tracking a "
        "habit, says they did a habit today (gym, reading, coding practice, water, "
        "meditation, walk...), wants to undo that, asks about habits or streaks, or "
        "removes a habit."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "add, done, undo, status, remove"},
            "habit": {"type": "STRING", "description": "Habit name, e.g. 'gym'"},
            "date": {"type": "STRING", "description": "Day it was done (default today)"},
        },
        "required": ["action"],
    },
}


def run(parameters: dict, player=None, session_memory=None) -> str:
    try:
        a = (parameters.get("action") or "status").lower().strip()
        name = (parameters.get("habit") or "").strip()
        day = college.parse_date(parameters.get("date", "")) or date.today()
        if a == "add":
            return _add(name)
        if a == "remove":
            return _remove(name)
        if a in ("done", "undo"):
            return _set(name, day, a == "done")
        return _status(name)
    except Exception as e:
        return f"Sir, the habit tracker hit an error: {e}"


def _find(data: dict, name: str) -> dict | None:
    q = name.lower()
    if not q:
        return None
    for h in data["habits"]:
        if h["name"].lower() == q:
            return h
    for h in data["habits"]:
        if q in h["name"].lower() or h["name"].lower() in q:
            return h
    return None


def _add(name: str) -> str:
    if not name:
        return "Sir, what habit should I track?"
    with st.lock():
        data = st.load(NAME, DEFAULT)
        if _find(data, name):
            return f"I'm already tracking {name}, sir."
        data["habits"].append({"id": college.new_id(), "name": name, "created": date.today().isoformat()})
        st.save(NAME, data)
    return f"Tracking {name} from today. Tell me each day you do it and I'll keep your streak."


def _remove(name: str) -> str:
    with st.lock():
        data = st.load(NAME, DEFAULT)
        h = _find(data, name)
        if not h:
            return "Sir, I'm not tracking that habit."
        data["habits"] = [x for x in data["habits"] if x["id"] != h["id"]]
        for k in list(data["log"]):
            data["log"][k] = [i for i in data["log"][k] if i != h["id"]]
        st.save(NAME, data)
    return f"Stopped tracking {h['name']}, sir."


def _set(name: str, day: date, done: bool) -> str:
    with st.lock():
        data = st.load(NAME, DEFAULT)
        h = _find(data, name)
        if not h:
            if not done or not name:
                return "Sir, I'm not tracking that habit."
            h = {"id": college.new_id(), "name": name, "created": day.isoformat()}
            data["habits"].append(h)
        ids = data["log"].setdefault(day.isoformat(), [])
        if done and h["id"] not in ids:
            ids.append(h["id"])
        if not done and h["id"] in ids:
            ids.remove(h["id"])
        st.save(NAME, data)
    s = streak(h["id"])
    if not done:
        return f"Unmarked {h['name']}, sir."
    return f"{h['name']} done. " + (f"That's a {s}-day streak!" if s > 1 else "Streak started.")


def streak(hid: str) -> int:
    log = st.load(NAME, DEFAULT)["log"]
    d = date.today()
    if hid not in log.get(d.isoformat(), []):
        d -= timedelta(days=1)          # today isn't over yet — count up to yesterday
    n = 0
    while hid in log.get(d.isoformat(), []):
        n += 1
        d -= timedelta(days=1)
    return n


def week_rate(hid: str) -> int:
    log = st.load(NAME, DEFAULT)["log"]
    days = [(date.today() - timedelta(days=i)).isoformat() for i in range(7)]
    return sum(hid in log.get(d, []) for d in days)


def _status(name: str) -> str:
    data = st.load(NAME, DEFAULT)
    if not data["habits"]:
        return "You aren't tracking any habits yet, sir. Say 'track a new habit' to start."
    today = data["log"].get(date.today().isoformat(), [])
    habits = [_find(data, name)] if name else data["habits"]
    habits = [h for h in habits if h]
    if not habits:
        return "Sir, I'm not tracking that habit."
    parts = []
    for h in habits:
        mark = "done today" if h["id"] in today else "not yet today"
        parts.append(f"{h['name']}: {mark}, streak {streak(h['id'])}, {week_rate(h['id'])} of last 7 days")
    return "; ".join(parts) + "."


def call_facts(kind: str) -> list[str]:
    data = st.load(NAME, DEFAULT)
    if not data["habits"]:
        return []
    today = data["log"].get(date.today().isoformat(), [])
    if kind == "weekly":
        return ["Habits this week: " + "; ".join(
            f"{h['name']} {week_rate(h['id'])}/7 (streak {streak(h['id'])})" for h in data["habits"])]
    if kind == "recap":
        open_ = [h for h in data["habits"] if h["id"] not in today]
        out = []
        if open_:
            out.append("Habits not done yet today: " + ", ".join(
                f"{h['name']}" + (f" (streak of {streak(h['id'])} at risk)" if streak(h["id"]) >= 2 else "")
                for h in open_))
        done = [h for h in data["habits"] if h["id"] in today]
        if done:
            out.append("Habits done today: " + ", ".join(h["name"] for h in done))
        return out
    return ["Habits to do today: " + ", ".join(h["name"] for h in data["habits"])]
