"""
Exam countdown + revision planner.

  "My DSA exam is on 20 November — topics: arrays, linked lists, trees, graphs, DP"
  "Add topics hashing and heaps to DSA"
  "What should I study today?"
  "I finished trees" / "Done with linked lists in DSA"
  "How many days to my exams?"

It spreads the topics you haven't covered over the days left, keeps the last day
before each exam for full revision, and reads today's topics on the morning call.
"""
from __future__ import annotations

import math
from datetime import date, timedelta

from plugins import _college_store as college
from plugins import _store as st

NAME = "exams"
DEFAULT = {"exams": []}
MAX_TOPICS_PER_DAY = 6

PLUGIN = {
    "name": "exam_planner",
    "description": (
        "Exam countdown and daily revision planner. Use when the user tells you an exam "
        "date and/or its syllabus topics, adds topics to an exam, asks what to study "
        "today, says they finished/covered a topic, asks how many days are left until "
        "exams, or removes an exam. Assignment deadlines go to college_assistant instead."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "add_exam, add_topics, today_plan, topic_done, list_exams, remove_exam"},
            "subject": {"type": "STRING", "description": "Exam subject, e.g. 'DSA'"},
            "date": {"type": "STRING", "description": "Exam date, YYYY-MM-DD or words like '20 nov'"},
            "topics": {"type": "STRING", "description": "Comma-separated topics"},
        },
        "required": ["action"],
    },
}


def run(parameters: dict, player=None, session_memory=None) -> str:
    try:
        a = (parameters.get("action") or "today_plan").lower().strip()
        fn = {"add_exam": _add_exam, "add_topics": _add_topics, "topic_done": _topic_done,
              "list_exams": _list, "remove_exam": _remove}.get(a)
        return fn(parameters) if fn else _today_text()
    except Exception as e:
        return f"Sir, the exam planner hit an error: {e}"


def _split(topics: str) -> list[str]:
    raw = (topics or "").replace(" and ", ",").replace(";", ",")
    return [t.strip() for t in raw.split(",") if t.strip()]


def _find(data: dict, subject: str) -> dict | None:
    q = (subject or "").strip().lower()
    if not q:
        return None
    upcoming = [e for e in data["exams"] if e["date"] >= date.today().isoformat()]
    for pool in (upcoming, data["exams"]):
        for e in pool:
            if e["subject"].lower() == q:
                return e
        for e in pool:
            if q in e["subject"].lower() or e["subject"].lower() in q:
                return e
    return None


def _add_exam(p: dict) -> str:
    subject = (p.get("subject") or "").strip()
    d = college.parse_date(p.get("date", ""))
    if not subject or not d:
        return "Sir, I need the subject and the exam date."
    with st.lock():
        data = st.load(NAME, DEFAULT)
        e = _find(data, subject)
        if e and e["date"] >= date.today().isoformat():
            e["date"] = d.isoformat()
        else:
            e = {"id": college.new_id(), "subject": subject, "date": d.isoformat(), "topics": []}
            data["exams"].append(e)
        known = {t["name"].lower() for t in e["topics"]}
        e["topics"] += [{"name": t, "done": False} for t in _split(p.get("topics", "")) if t.lower() not in known]
        st.save(NAME, data)
    days = (d - date.today()).days
    msg = f"{subject} exam set for {d.strftime('%d %B').lstrip('0')}, {st.plural(days, 'day')} away."
    if not e["topics"]:
        msg += " Tell me its topics and I'll plan your revision."
    else:
        msg += f" {st.plural(len(e['topics']), 'topic')} to cover — I'll spread them out for you."
    return msg


def _add_topics(p: dict) -> str:
    with st.lock():
        data = st.load(NAME, DEFAULT)
        e = _find(data, p.get("subject", ""))
        if not e:
            return "Sir, add the exam with its date first."
        known = {t["name"].lower() for t in e["topics"]}
        new = [t for t in _split(p.get("topics", "")) if t.lower() not in known]
        e["topics"] += [{"name": t, "done": False} for t in new]
        st.save(NAME, data)
    return f"Added {st.plural(len(new), 'topic')} to {e['subject']}, sir."


def _topic_done(p: dict) -> str:
    q = (p.get("topics") or "").strip().lower()
    if not q:
        return "Sir, which topic did you finish?"
    with st.lock():
        data = st.load(NAME, DEFAULT)
        pools = [_find(data, p.get("subject", ""))] if p.get("subject") else data["exams"]
        hits = []
        for e in pools:
            if not e:
                continue
            for t in e["topics"]:
                if not t["done"] and any(x and (x in t["name"].lower() or t["name"].lower() in x)
                                         for x in (s.lower() for s in _split(q))):
                    t["done"] = True
                    hits.append((e, t["name"]))
        st.save(NAME, data)
    if not hits:
        return "Sir, I couldn't find that topic in your exam plans."
    e = hits[-1][0]
    left = sum(not t["done"] for t in e["topics"])
    return (f"Nice — ticked off {', '.join(h[1] for h in hits)}. "
            f"{st.plural(left, 'topic')} left for {e['subject']}.")


def _list(p: dict) -> str:
    ex = upcoming()
    if not ex:
        return "No upcoming exams saved, sir."
    return "; ".join(
        f"{e['subject']} in {st.plural(_days(e), 'day')}, "
        f"{sum(t['done'] for t in e['topics'])} of {len(e['topics'])} topics done" for e in ex) + "."


def _remove(p: dict) -> str:
    with st.lock():
        data = st.load(NAME, DEFAULT)
        e = _find(data, p.get("subject", ""))
        if not e:
            return "Sir, I couldn't find that exam."
        data["exams"] = [x for x in data["exams"] if x["id"] != e["id"]]
        st.save(NAME, data)
    return f"Removed the {e['subject']} exam, sir."


# ── planning ─────────────────────────────────────────────────────────────────
def _days(e: dict) -> int:
    return (date.fromisoformat(e["date"]) - date.today()).days


def upcoming(within: int = 60) -> list[dict]:
    return sorted((e for e in st.load(NAME, DEFAULT)["exams"] if 0 <= _days(e) <= within),
                  key=lambda e: e["date"])


def today_plan() -> list[tuple[str, list[str], str]]:
    """[(subject, topics_for_today, note)] — nearest exam first."""
    plan, budget = [], MAX_TOPICS_PER_DAY
    for e in upcoming():
        days = _days(e)
        left = [t["name"] for t in e["topics"] if not t["done"]]
        if days <= 1:
            plan.append((e["subject"], [], "exam is " + ("today" if days == 0 else "tomorrow") +
                         " — full revision of everything"))
            continue
        if not left:
            plan.append((e["subject"], [], "all topics covered — light revision"))
            continue
        study_days = max(1, days - 1)           # keep the last day for revision
        per_day = min(budget, math.ceil(len(left) / study_days))
        if per_day <= 0:
            break
        plan.append((e["subject"], left[:per_day], f"exam in {days} days"))
        budget -= per_day
    return plan


def _today_text() -> str:
    plan = today_plan()
    if not plan:
        return "No exams coming up, sir. Tell me your exam dates and topics and I'll plan your revision."
    parts = []
    for subject, topics, note in plan:
        parts.append(f"{subject} ({note}): {', '.join(topics)}" if topics else f"{subject}: {note}")
    return "Today's revision plan, sir. " + ". ".join(parts) + "."


def call_facts(kind: str) -> list[str]:
    plan = today_plan()
    if not plan:
        return []
    if kind == "briefing":
        return ["Today's revision plan: " + "; ".join(
            f"{s} ({n}): {', '.join(t)}" if t else f"{s}: {n}" for s, t, n in plan)]
    soon = [e for e in upcoming(14)]
    if soon:
        return ["Exams coming up: " + "; ".join(
            f"{e['subject']} in {st.plural(_days(e), 'day')}, "
            f"{st.plural(sum(not t['done'] for t in e['topics']), 'topic')} left"
            for e in soon)]
    return []
