"""
Awake mode — stops Jarvis from dozing off all the time.

What changed (fork):
  * Jarvis now STARTS AWAKE, even with the "Hey Jarvis" wake word switched on.
  * It only naps after a long quiet spell (default 20 min, 0 = never), not 2 min.
  * Typing a command, a Telegram/phone notice, or a reminder wakes it up.
  * "Hey Jarvis" is easier to trigger (sensitivity: normal / high / very high).
  * Reconnecting now shows CONNECTING instead of SLEEPING.
  * "Only answer when I'm talking to Jarvis" (Gemini's proactive audio) is OFF by
    default — when on, Jarvis may stay silent if it thinks you weren't talking
    to it, which looks like it's asleep.

Voice: "stay awake", "go to sleep", "sleep after 30 minutes of silence",
       "make the wake word more sensitive", "are you awake?"
"""
from __future__ import annotations

from plugins import _bus as bus
from plugins import _store as st

NS = "awake"

PLUGIN_SETTINGS = {
    "namespace": NS,
    "title": "AWAKE MODE",
    "fields": [
        {"key": "auto_sleep_minutes", "label": "Nap after this many quiet minutes (0 = never sleep)",
         "type": "text", "default": "20"},
        {"key": "wake_sensitivity", "label": "'Hey Jarvis' sensitivity", "type": "choice",
         "options": ["high", "very high", "normal"]},
        {"key": "only_when_addressed", "label": "Only answer when I'm talking to Jarvis (can feel unresponsive)",
         "type": "toggle", "default": False},
    ],
}

PLUGIN = {
    "name": "awake_mode",
    "description": (
        "Controls whether the assistant stays awake. Use when the user says 'stay awake', "
        "'don't sleep', 'go to sleep', 'sleep after N minutes', 'make the wake word more/less "
        "sensitive', or asks if you are awake / why you keep sleeping."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "stay_awake, sleep_now, set_auto_sleep, set_sensitivity, status"},
            "minutes": {"type": "INTEGER", "description": "For set_auto_sleep: minutes of silence (0 = never)"},
            "sensitivity": {"type": "STRING", "description": "normal, high or very high"},
        },
        "required": ["action"],
    },
}


def _save(**values):
    from memory.config_manager import save_plugin_config
    save_plugin_config(NS, values)


def run(parameters: dict, player=None, session_memory=None) -> str:
    a = (parameters.get("action") or "status").lower().strip()
    j = bus._JARVIS
    if a == "stay_awake":
        _save(auto_sleep_minutes="0")
        if j is not None:
            j.wake(reason="you asked me to stay awake")
        return "Done — I'll stay awake until you tell me to sleep, sir."
    if a == "sleep_now":
        if j is not None and getattr(j, "_wake_enabled", False):
            j.sleep(reason="you asked")
            return "Going quiet. Say 'Hey Jarvis' when you need me."
        return ("I can only sleep when the 'Hey Jarvis' wake word is switched on "
                "(⚙ → CONTROLS → WAKE WORD), otherwise nothing could wake me up.")
    if a == "set_auto_sleep":
        mins = max(0, st.to_int(parameters.get("minutes"), 20))
        _save(auto_sleep_minutes=str(mins))
        if j is not None:
            j._last_user_speech = __import__("time").monotonic()
        return ("I'll never fall asleep on my own now." if mins == 0
                else f"I'll nap after {mins} quiet minutes, sir.")
    if a == "set_sensitivity":
        level = str(parameters.get("sensitivity") or "high").lower()
        if level not in ("normal", "high", "very high"):
            level = "high"
        _save(wake_sensitivity=level)
        if j is not None and getattr(j, "_wake_detector", None) is not None:
            try:
                j._wake_detector._threshold = {"normal": 0.5, "high": 0.35, "very high": 0.25}[level]
            except Exception:
                pass
        return f"Wake word sensitivity set to {level}."
    mins = st.to_int(st.setting(NS, "auto_sleep_minutes", 20), 20)
    awake = getattr(j, "_awake", True) if j is not None else True
    ww = getattr(j, "_wake_enabled", False) if j is not None else False
    return (f"I'm {'awake' if awake else 'asleep'}. Wake word is {'on' if ww else 'off'}; "
            + ("I never auto-sleep." if not ww or mins == 0 else f"I nap after {mins} quiet minutes."))
