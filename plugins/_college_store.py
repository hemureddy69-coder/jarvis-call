"""
Shared storage for the college assistant and the phone-call plugin.

The leading underscore keeps the plugin loader from treating this file as a
plugin of its own (see core/plugin_loader.py). Both college_assistant.py and
phone_call.py import it, so it must sit in plugins/ next to them.

Everything lives in memory/college.json on this machine (git-ignored).
"""
from __future__ import annotations

import re
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

BASE_DIR  = Path(__file__).resolve().parent.parent

DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


# ── load / save (through _store so it syncs with the cloud when linked) ──────
def load() -> dict:
    from plugins import _store
    return _store.load("college", {"classes": [], "deadlines": []})


def save(data: dict) -> None:
    from plugins import _store
    _store.save("college", data)


def new_id() -> str:
    return uuid.uuid4().hex[:6]


# ── parsing helpers (the model may send "tomorrow", "fri", "5pm" ...) ────────
def parse_day(text: str) -> str | None:
    t = (text or "").strip().lower()
    if t in ("today",):
        return DAYS[date.today().weekday()]
    if t in ("tomorrow",):
        return DAYS[(date.today().weekday() + 1) % 7]
    for d in DAYS:
        if t == d or (len(t) >= 3 and d.startswith(t)):
            return d
    return None


def parse_time(text: str, default: str | None = None) -> str | None:
    """'9', '9:30', '09:30', '2pm', '2:15 PM', '14:15' -> 'HH:MM'."""
    t = (text or "").strip().lower().replace(".", ":")
    if not t:
        return default
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", t)
    if not m:
        return default
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ap == "pm" and h < 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    if not (0 <= h < 24 and 0 <= mi < 60):
        return default
    return f"{h:02d}:{mi:02d}"


def parse_date(text: str) -> date | None:
    """'2026-10-05', 'today', 'tomorrow', 'friday', 'in 3 days', '5 oct', '05/10'."""
    t = (text or "").strip().lower()
    today = date.today()
    if not t:
        return None
    if t == "today":
        return today
    if t == "tomorrow":
        return today + timedelta(days=1)
    if t == "yesterday":
        return today - timedelta(days=1)
    m = re.fullmatch(r"in (\d+) days?", t)
    if m:
        return today + timedelta(days=int(m.group(1)))
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d/%m", "%d %b", "%d %B",
                "%b %d", "%B %d", "%d %b %Y", "%d %B %Y"):
        try:
            d = datetime.strptime(t, fmt).date()
            if "%Y" not in fmt:
                d = d.replace(year=today.year)
                if d < today:
                    d = d.replace(year=today.year + 1)
            return d
        except ValueError:
            pass
    t = t.replace("next ", "")
    day = parse_day(t)
    if day:
        ahead = (DAYS.index(day) - today.weekday()) % 7
        return today + timedelta(days=ahead or 7)
    return None


# ── queries used by both plugins ─────────────────────────────────────────────
def classes_on(day: date) -> list[dict]:
    name = DAYS[day.weekday()]
    return sorted((c for c in load()["classes"] if c.get("day") == name),
                  key=lambda c: c.get("start", ""))


def due_dt(d: dict) -> datetime | None:
    try:
        return datetime.fromisoformat(d["due"])
    except Exception:
        return None


def open_deadlines(within_days: int | None = None) -> list[dict]:
    now = datetime.now()
    out = []
    for d in load()["deadlines"]:
        if d.get("done"):
            continue
        due = due_dt(d)
        if due is None:
            continue
        if within_days is not None and due > now + timedelta(days=within_days):
            continue
        out.append(d)
    return sorted(out, key=lambda d: d["due"])


def fmt_class(c: dict) -> str:
    room = f" in {c['room']}" if c.get("room") else ""
    return f"{c.get('subject', 'class')} at {_spoken_time(c.get('start', ''))}{room}"


def fmt_deadline(d: dict) -> str:
    due = due_dt(d)
    subj = f" for {d['subject']}" if d.get("subject") else ""
    if due is None:
        return f"{d.get('title', 'task')}{subj}"
    now = datetime.now()
    if due < now:
        when = f"overdue since {_spoken_day(due.date())}"
    else:
        when = f"due {_spoken_day(due.date())} at {_spoken_time(due.strftime('%H:%M'))}"
    return f"{d.get('title', 'task')}{subj}, {when}"


def _spoken_time(hhmm: str) -> str:
    try:
        t = datetime.strptime(hhmm, "%H:%M")
        return t.strftime("%I:%M %p").lstrip("0").replace(":00 ", " ")
    except Exception:
        return hhmm


def _spoken_day(d: date) -> str:
    diff = (d - date.today()).days
    if diff == 0:
        return "today"
    if diff == 1:
        return "tomorrow"
    if diff == -1:
        return "yesterday"
    if 1 < diff < 7:
        return d.strftime("%A")
    return d.strftime("%d %B").lstrip("0")
