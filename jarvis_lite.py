"""
Jarvis "lite" — one pass of everything that must keep happening while the PC
is OFF. GitHub Actions runs this every ~5 minutes (.github/workflows/jarvis.yml).

  1. if the PC checked in recently, do nothing (the PC is handling it)
  2. answer Telegram messages
  3. ring the phone for due calls (morning/evening, class & deadline alerts,
     check-in / journal / quiz, "call me at 6") and re-ring unanswered ones
  4. attendance questions, birthday reminders
  5. refresh weather + headlines for the call app

Config comes from GitHub secrets (see setup_github.py). Part of the Jarvis fork
of MARK LV by FatihMakes (CC BY-NC 4.0).
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import traceback
from pathlib import Path

os.environ["JARVIS_ROLE"] = "lite"
BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from memory import config_manager as cfgm  # noqa: E402

PC_ONLY = ("focus_mode", "laptop_guard", "awake_mode")


def bootstrap() -> None:
    env = os.environ.get
    cfgm.save_api_keys(env("GEMINI_API_KEY", ""))
    cfgm.save_assistant_config(env("ASSISTANT_NAME") or "Jarvis", env("USER_NAME", ""))
    cfgm.save_plugin_config("telegram", {"bot_token": env("TELEGRAM_BOT_TOKEN", ""), "push_alerts": True})
    if env("TELEGRAM_CHAT_ID"):
        cfgm.save_plugin_config("telegram_pair", {"chat_id": env("TELEGRAM_CHAT_ID")})
    cfgm.save_plugin_config("app_call", {"ntfy_topic": env("NTFY_TOPIC", "")})
    cfgm.save_plugin_config("github", {"pages_url": env("PAGES_URL", "")})
    pc = {"city": env("CITY") or "Chennai", "call_method": "free app (ntfy)"}
    cfgm.save_plugin_config("phone_call", pc)
    data = cfgm.load_api_keys()
    data["plugins_enabled"] = {**(data.get("plugins_enabled") or {}), **{p: False for p in PC_ONLY}}
    cfgm.CONFIG_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def step(name, fn):
    try:
        fn()
    except Exception:
        print(f"[lite] {name} failed:")
        traceback.print_exc()


def main():
    bootstrap()
    from plugins import _store as st
    if not st.is_linked():
        print("[lite] DATA_REPO / DATA_TOKEN missing — nothing to do.")
        return
    if st.desktop_online():
        print("[lite] PC is on — it's handling everything. Bye.")
        return

    # apply the phone_call settings the PC saved (times, toggles) on top of defaults
    saved = st.load("pc_settings", {})
    for ns, values in saved.items():
        if isinstance(values, dict) and ns not in ("telegram", "telegram_pair", "github"):
            cfgm.save_plugin_config(ns, values)

    from core.plugin_loader import discover_plugins
    discover_plugins(BASE / "plugins", set(), logger=lambda m: None)
    from plugins import (app_call, attendance, birthdays, github_sync, phone_call,
                         telegram_bot, voice_calls)

    step("telegram", lambda: telegram_bot.poll_once(wait_s=0))
    step("calls", phone_call._tick)
    step("daily calls", voice_calls._tick)
    step("ring retries", app_call.pages_retry_tick)
    step("attendance", attendance._tick)
    step("birthdays", birthdays._tick)
    step("context", github_sync.refresh_context)

    # let background work (photo notes, ringing) finish before the job ends
    for t in threading.enumerate():
        if t is not threading.current_thread() and t.is_alive():
            t.join(timeout=90)
    st.flush_all()
    print("[lite] done", time.strftime("%H:%M:%S"))


if __name__ == "__main__":
    main()
