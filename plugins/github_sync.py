"""
GitHub sync — Jarvis keeps working (and CALLING you) while your PC is off.

  * Your data (timetable, deadlines, expenses, habits, journal…) lives in a
    PRIVATE GitHub repo, so the PC, the phone call app and the GitHub runner
    all see the same thing.
  * A free GitHub Actions job runs every ~5 minutes while the PC is off: it
    rings your phone (ntfy) for scheduled calls, answers Telegram, and sends
    reminders. While the PC is on, the PC does all of that itself.
  * The call app is a free GitHub Pages site; it talks to Gemini Live straight
    from your phone's browser — a real two-way conversation.

Set up once with:  python setup_github.py   (see GITHUB_SETUP.md)
"""
from __future__ import annotations

import time
from datetime import datetime

from plugins import _store as st

NS = "github"
CONTEXT = "context"
CONTEXT_EVERY_S = 3600


def _test(values: dict):
    import requests
    repo = str((values or {}).get("data_repo") or "").strip()
    tok = str((values or {}).get("token") or "").strip()
    if not repo or not tok:
        return False, "Fill in the data repo (you/jarvis-data) and the token — or run setup_github.py."
    try:
        r = requests.get(f"https://api.github.com/repos/{repo}",
                         headers={"Authorization": f"Bearer {tok}"}, timeout=10)
    except Exception as e:
        return False, f"Can't reach GitHub: {e}"
    if r.status_code != 200:
        return False, f"GitHub said {r.status_code} — check the repo name and token."
    return True, f"Connected to {repo} ✓ (private: {r.json().get('private')})"


PLUGIN_SETTINGS = {
    "namespace": NS,
    "title": "GITHUB SYNC (PC-OFF MODE)",
    "fields": [
        {"key": "data_repo", "label": "Private data repo (you/jarvis-data)", "type": "text"},
        {"key": "token", "label": "GitHub token", "type": "password"},
        {"key": "pages_url", "label": "Call app address (https://you.github.io/jarvis-call)", "type": "text"},
        {"key": "enabled", "label": "Use GitHub sync", "type": "toggle", "default": True},
    ],
    "action": {"label": "TEST", "run": _test},
}

PLUGIN = {
    "name": "github_sync",
    "description": ("Reports whether Jarvis's PC-off mode (GitHub sync + call app) is set up, "
                    "and gives the link to the Jarvis call app."),
    "parameters": {"type": "OBJECT", "properties": {
        "action": {"type": "STRING", "description": "status or link"}}, "required": []},
}


def run(parameters: dict, player=None, session_memory=None) -> str:
    if not st.is_linked():
        return "PC-off mode isn't set up yet, sir. Run setup_github.py once."
    from plugins import app_call
    if (parameters.get("action") or "") == "link":
        return f"Your call app is at {app_call.pages_url()}"
    pc = st.load("pc_status", {}).get("last", 0)
    return ("PC-off mode is on: your data is on GitHub, and when the PC is off GitHub rings "
            f"you and answers Telegram. Last PC check-in {int((time.time() - pc) / 60)} minutes ago.")


def refresh_context(force: bool = False) -> None:
    """Weather + headlines for the phone call app (it can't fetch these itself)."""
    if not st.is_linked():
        return
    ctx = st.load(CONTEXT, {"updated": 0})
    if not force and time.time() - float(ctx.get("updated", 0)) < CONTEXT_EVERY_S:
        return
    try:
        from plugins import phone_call
        cfg = phone_call._cfg()
        city = cfg.get("city") or "Chennai"
        st.save(CONTEXT, {
            "updated": time.time(),
            "updated_at": datetime.now().isoformat(timespec="minutes"),
            "weather_today": phone_call._weather(city, tomorrow=False),
            "weather_tomorrow": phone_call._weather(city, tomorrow=True),
            "headlines": phone_call._headlines(cfg.get("news_topic") or "")[:1500],
            "user_name": phone_call._user_name(),
            "assistant_name": phone_call._assistant_name(),
            "morning_time": cfg.get("morning_time"), "evening_time": cfg.get("evening_time"),
        })
    except Exception as e:
        print(f"[GitHub] context refresh failed: {e}")


SHARED_NS = ("phone_call", "voice_calls", "app_call", "attendance", "birthdays", "expenses")
SECRET_KEYS = ("account_sid", "auth_token", "from_number", "to_number", "token", "key")


def publish_settings() -> None:
    """PC → GitHub: your call times and toggles (never keys), so the GitHub
    runner follows the same schedule while the PC is off."""
    if st.is_lite() or not st.is_linked():
        return
    try:
        from memory.config_manager import get_plugin_config
        shared = {ns: {k: v for k, v in (get_plugin_config(ns) or {}).items() if k not in SECRET_KEYS}
                  for ns in SHARED_NS}
        if shared != st.load("pc_settings", {}):
            st.save("pc_settings", shared)
    except Exception as e:
        print(f"[GitHub] settings sync failed: {e}")


def _pc_loop():
    publish_settings()
    refresh_context()


st.start_loop("github-sync", _pc_loop, every_s=600, first_delay_s=60)
