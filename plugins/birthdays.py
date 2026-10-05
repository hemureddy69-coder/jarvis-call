"""
Birthday reminders.

  "Remember Rahul's birthday is on 14 March"
  "Whose birthday is coming up?"
  "Forget Rahul's birthday"

On the day, the morning call mentions it with a suggested wish, and Jarvis
reminds you on the PC (once) — with a 3-day heads-up for gifts.
"""
from __future__ import annotations

from datetime import date, datetime

from plugins import _bus as bus
from plugins import _college_store as college
from plugins import _store as st

NAME = "birthdays"
DEFAULT = {"people": [], "told": {}}

PLUGIN = {
    "name": "birthdays",
    "description": (
        "Saves friends' and family birthdays and reminds the user. Use when the user "
        "tells you someone's birthday, asks whose birthday is today or coming up, or "
        "asks you to forget a birthday."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "add, upcoming, remove"},
            "name": {"type": "STRING", "description": "Person's name"},
            "date": {"type": "STRING", "description": "Birthday, e.g. '14 March' or '2003-03-14'"},
            "relation": {"type": "STRING", "description": "e.g. friend, sister, roommate (optional)"},
        },
        "required": ["action"],
    },
}


def run(parameters: dict, player=None, session_memory=None) -> str:
    try:
        a = (parameters.get("action") or "upcoming").lower().strip()
        if a == "add":
            return _add(parameters)
        if a == "remove":
            return _remove(parameters.get("name", ""))
        return _upcoming_text()
    except Exception as e:
        return f"Sir, the birthday reminder hit an error: {e}"


def _parse_md(text: str) -> tuple[int, int] | None:
    t = (text or "").strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d %B %Y", "%d %b %Y"):
        try:
            d = datetime.strptime(t, fmt)
            return d.month, d.day
        except ValueError:
            pass
    d = college.parse_date(t)
    return (d.month, d.day) if d else None


def _add(p: dict) -> str:
    name = (p.get("name") or "").strip()
    md = _parse_md(p.get("date", ""))
    if not name or not md:
        return "Sir, I need the name and the date."
    with st.lock():
        data = st.load(NAME, DEFAULT)
        data["people"] = [x for x in data["people"] if x["name"].lower() != name.lower()]
        data["people"].append({"name": name, "month": md[0], "day": md[1],
                               "relation": (p.get("relation") or "").strip()})
        st.save(NAME, data)
    d = date(2000, md[0], md[1])
    return f"Saved: {name}'s birthday on {d.strftime('%d %B').lstrip('0')}. I'll remind you, sir."


def _remove(name: str) -> str:
    with st.lock():
        data = st.load(NAME, DEFAULT)
        before = len(data["people"])
        data["people"] = [x for x in data["people"] if name.lower() not in x["name"].lower()]
        st.save(NAME, data)
    return "Removed, sir." if len(data["people"]) < before else "Sir, I don't have that birthday saved."


def _days_until(p: dict) -> int:
    t = date.today()
    for y in (t.year, t.year + 1):
        try:
            d = date(y, p["month"], p["day"])
        except ValueError:                   # 29 Feb in a non-leap year
            d = date(y, 3, 1)
        if d >= t:
            return (d - t).days
    return 365


def upcoming(within: int = 30) -> list[tuple[dict, int]]:
    out = [(p, _days_until(p)) for p in st.load(NAME, DEFAULT)["people"]]
    return sorted([x for x in out if x[1] <= within], key=lambda x: x[1])


def _who(p: dict) -> str:
    return f"{p['name']} ({p['relation']})" if p.get("relation") else p["name"]


def _upcoming_text() -> str:
    up = upcoming(30)
    if not up:
        return "No birthdays in the next 30 days, sir."
    return "; ".join(f"{_who(p)} " + ("today!" if d == 0 else "tomorrow" if d == 1 else f"in {d} days")
                     for p, d in up) + "."


def _tick():
    if st.is_cloud() and st.desktop_online():
        return          # the PC is on — it asks out loud instead of via Telegram
    now = datetime.now()
    if now.hour < 9 or now.hour >= 22 or not st.plugin_enabled(NAME):
        return
    key = date.today().isoformat()
    for p, d in upcoming(3):
        if d not in (0, 3):
            continue
        k = f"{key}:{p['name']}:{d}"
        with st.lock():
            data = st.load(NAME, DEFAULT)
            if k in data["told"]:
                continue
            data["told"][k] = True
            data["told"] = dict(list(data["told"].items())[-200:])
            st.save(NAME, data)
        if not st.is_cloud():
            bus.phone(f"🎂 {_who(p)}'s birthday is " + ("today!" if d == 0 else "in 3 days."))
        if d == 0:
            bus.say(f"Today is {_who(p)}'s birthday. Remind the user to wish them and offer a short wish they could send.",
                    phone_text=f"🎂 Today is {_who(p)}'s birthday — don't forget to wish them!")
        else:
            bus.say(f"{_who(p)}'s birthday is in 3 days — mention it in case the user wants to plan a gift.",
                    phone_text=f"🎁 {_who(p)}'s birthday is in 3 days.")
        return


def call_facts(kind: str) -> list[str]:
    if kind != "briefing":
        return []
    up = upcoming(3)
    today = [_who(p) for p, d in up if d == 0]
    soon = [f"{_who(p)} in {d} days" for p, d in up if d > 0]
    out = []
    if today:
        out.append("BIRTHDAY TODAY: " + ", ".join(today) + " (remind them to wish, suggest a one-line wish)")
    if soon:
        out.append("Birthdays soon: " + ", ".join(soon))
    return out


st.start_loop(NAME, _tick, every_s=300, first_delay_s=60)
