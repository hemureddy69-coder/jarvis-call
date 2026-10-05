"""
Small shared helpers for this fork's plugins: JSON files in memory/ and
background loops. Leading underscore = not a plugin itself.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import traceback
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
MEMORY_DIR = BASE_DIR / "memory"
_LOCK = threading.RLock()
_LOOPS: set[str] = set()

# ── Where this copy of Jarvis runs ───────────────────────────────────────────
#   desktop — the PC app (default).
#   lite    — the GitHub Actions runner (jarvis_lite.py), every ~5 minutes while
#             the PC is OFF: rings your phone, answers Telegram, sends reminders.
# Data can live in a PRIVATE GitHub repo (⚙ → PLUGIN SETTINGS → GITHUB SYNC), so
# the PC, the GitHub runner and the phone call page all share the same data.
ROLE = os.environ.get("JARVIS_ROLE", "desktop").strip().lower()
SYNCED = re.compile(r"^[a-z_]{1,40}$")          # memory/<name>.json files that sync
_DIRTY: set[str] = set()
_ETAG: dict[str, tuple[str, str, dict]] = {}     # name -> (etag, sha, data)
PC_STALE_S = 6 * 60                               # PC counts as off after this


def is_lite() -> bool:
    return ROLE == "lite"


def is_cloud() -> bool:
    """Running headless (no PC voice): notices go to Telegram instead."""
    return ROLE in ("cloud", "lite")


def github_link() -> tuple[str, str]:
    """(owner/repo, token) of the private data repo, or ('', '')."""
    repo = os.environ.get("DATA_REPO") or str(setting("github", "data_repo", "") or "")
    tok = os.environ.get("DATA_TOKEN") or str(setting("github", "token", "") or "")
    if repo.strip() and tok.strip() and (is_lite() or truthy(setting("github", "enabled", True))):
        return repo.strip(), tok.strip()
    return "", ""


def is_linked() -> bool:
    return bool(github_link()[0])


def runs_schedulers() -> bool:
    """Calls, schedules and Telegram polling run in ONE place: the PC while it is
    on, the GitHub runner while it is off."""
    if is_lite():
        return not desktop_online()
    return True


def _read_local(name: str) -> dict:
    try:
        data = json.loads((MEMORY_DIR / f"{name}.json").read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_local(name: str, data: dict) -> None:
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    path = MEMORY_DIR / f"{name}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _gh(method: str, name: str, body: dict | None = None, etag: str = ""):
    import requests
    repo, tok = github_link()
    headers = {"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28"}
    if etag:
        headers["If-None-Match"] = etag
    return requests.request(method, f"https://api.github.com/repos/{repo}/contents/memory/{name}.json",
                            json=body, headers=headers, timeout=10)


def _remote_get(name: str) -> dict | None:
    """Latest copy from GitHub. Conditional requests (304) don't use rate limit."""
    import base64
    cached = _ETAG.get(name)
    r = _gh("GET", name, etag=cached[0] if cached else "")
    if r.status_code == 304 and cached:
        return json.loads(json.dumps(cached[2]))
    if r.status_code == 404:
        _ETAG.pop(name, None)
        return {}
    r.raise_for_status()
    j = r.json()
    data = json.loads(base64.b64decode(j["content"]).decode("utf-8") or "{}")
    _ETAG[name] = (r.headers.get("ETag", ""), j["sha"], data)
    return json.loads(json.dumps(data))


def _remote_put(name: str, data: dict) -> None:
    import base64
    body = {"message": f"jarvis: {name}", "content": base64.b64encode(
        json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")).decode()}
    for _ in range(3):
        sha = _ETAG.get(name, ("", "", {}))[1]
        if not sha:
            try:
                _remote_get(name)
                sha = _ETAG.get(name, ("", "", {}))[1]
            except Exception:
                pass
        if sha:
            body["sha"] = sha
        r = _gh("PUT", name, body)
        if r.status_code in (409, 422):            # someone else saved first → retry
            _ETAG.pop(name, None)
            body.pop("sha", None)
            continue
        r.raise_for_status()
        j = r.json()
        _ETAG[name] = ("", j["content"]["sha"], json.loads(json.dumps(data)))
        return
    raise RuntimeError(f"couldn't save {name} to GitHub")


def load(name: str, default: dict) -> dict:
    """Read memory/<name>.json (from GitHub when linked), filling missing keys."""
    with _LOCK:
        data = None
        if is_linked() and SYNCED.match(name) and name not in _DIRTY:
            try:
                data = _remote_get(name)
                if isinstance(data, dict):
                    _write_local(name, data)        # offline cache
            except Exception:
                data = None
        if not isinstance(data, dict):
            data = _read_local(name)
        for k, v in default.items():
            data.setdefault(k, json.loads(json.dumps(v)))   # deep copy of default
        return data


def save(name: str, data: dict) -> None:
    with _LOCK:
        _write_local(name, data)
        if is_linked() and SYNCED.match(name):
            try:
                _remote_put(name, data)
                _DIRTY.discard(name)
            except Exception as e:
                print(f"[Sync] {name} not saved to GitHub yet ({e}) — will retry")
                _DIRTY.add(name)
                start_loop("store-sync", _flush_dirty, every_s=30, first_delay_s=30)


def _flush_dirty():
    for name in list(_DIRTY):
        with _LOCK:
            try:
                _remote_put(name, _read_local(name))
                _DIRTY.discard(name)
            except Exception:
                return


def flush_all() -> None:
    """Lite runner: push anything still pending before the job ends."""
    _flush_dirty()


def lock():
    return _LOCK


def _heartbeat():
    """PC side: tell the GitHub runner the PC is on (it then stays quiet)."""
    if not is_lite() and is_linked():
        try:
            import platform
            with _LOCK:
                _remote_put("pc_status", {"last": time.time(), "os": platform.system()})
        except Exception:
            pass


def desktop_online(within_s: float = PC_STALE_S) -> bool:
    """Is the PC app running right now? (Always True on the PC itself.)"""
    if not is_cloud():
        return True
    try:
        last = float((_remote_get("pc_status") if is_linked() else _read_local("pc_status")).get("last", 0))
    except Exception:
        last = 0
    return time.time() - last < within_s


def start_loop(name: str, fn, every_s: float, first_delay_s: float = 15) -> None:
    """Run fn() forever in a daemon thread. Started once per name per process.
    The GitHub runner (lite) calls each tick once itself, so no loops there."""
    if name in _LOOPS or is_lite():
        return
    _LOOPS.add(name)

    def _run():
        time.sleep(first_delay_s)
        while True:
            try:
                fn()
            except Exception:
                traceback.print_exc()
            time.sleep(every_s)

    threading.Thread(target=_run, name=f"{name}-loop", daemon=True).start()


def plugin_enabled(name: str) -> bool:
    try:
        from memory.config_manager import get_plugin_enabled
        return get_plugin_enabled(name)
    except Exception:
        return True


def setting(namespace: str, key: str, default=None):
    try:
        from memory.config_manager import get_plugin_setting
        v = get_plugin_setting(namespace, key, default)
        return default if v in (None, "") else v
    except Exception:
        return default


def truthy(v) -> bool:
    return v is True or str(v).strip().lower() in ("true", "1", "yes", "on")


def to_int(v, default: int) -> int:
    try:
        return int(float(str(v).strip()))
    except (TypeError, ValueError):
        return default


def to_float(v, default: float | None = None) -> float | None:
    try:
        return float(str(v).replace(",", "").replace("₹", "").replace("rs", "").strip())
    except (TypeError, ValueError):
        return default


def plural(n: int, word: str, plural_word: str | None = None) -> str:
    return f"{n} {word if n == 1 else (plural_word or word + 's')}"


# PC: heartbeat every 2 minutes once GitHub sync is set up
if not is_lite():
    start_loop("pc-heartbeat", _heartbeat, every_s=120, first_delay_s=20)
