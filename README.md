# jarvis-call

The free, always-on half of my Jarvis assistant (a fork of
[MARK LV by FatihMakes](https://github.com/FatihMakes/Mark-LV), CC BY-NC 4.0):

* **docs/** — the Jarvis call app (GitHub Pages). It talks to Gemini Live directly from the
  phone; keys are stored only in the phone's browser.
* **.github/workflows/jarvis.yml** — runs `jarvis_lite.py` every ~5 minutes while my PC is
  off: rings my phone (ntfy) for scheduled calls, answers Telegram, sends reminders.

Personal data lives in a separate private repo. No keys are stored in this repo.
