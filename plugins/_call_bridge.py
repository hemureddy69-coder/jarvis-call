"""
Two-way phone calls: Twilio <-> your PC <-> Gemini Live.

    Your phone ──Twilio──▶ https://<random>.trycloudflare.com ──cloudflared──▶
    this PC :8765 (/twilio/voice, /twilio/stream) ──▶ Gemini Live (voice in/out)

* A tiny FastAPI server (already a Jarvis dependency) runs on 127.0.0.1:8765.
* cloudflared (free, no account) gives it a public https address, so Twilio can
  stream call audio to it. It's downloaded into tools/ the first time.
* Each call opens its own Gemini Live session. Twilio sends 8 kHz μ-law audio,
  Gemini wants 16 kHz PCM and speaks 24 kHz PCM — converted here with numpy.
* Security: every stream must carry this run's secret token; the incoming-call
  webhook checks Twilio's signature AND that the caller is your own number.

voice_calls.py registers a "mode provider" that decides what Jarvis says and
which extra tools exist in each kind of call (briefing, check-in, journal, quiz,
incoming). Leading underscore = not a plugin itself.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import platform
import re
import secrets
import shutil
import subprocess
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

import numpy as np

from plugins import _store as st

BASE_DIR = st.BASE_DIR
TOOLS_DIR = BASE_DIR / "tools"
CALL_LOG_DIR = BASE_DIR / "notes" / "calls"
SECRET = secrets.token_urlsafe(16)          # new every launch
DEFAULT_PORT = 8765

# ═════════════════════════════════════════════════════════════════════════════
#  Audio: μ-law 8 kHz  <->  PCM16 16/24 kHz
# ═════════════════════════════════════════════════════════════════════════════
def _build_ulaw_decode() -> np.ndarray:
    u = ~np.arange(256, dtype=np.int32) & 0xFF
    sign = u & 0x80
    exponent = (u >> 4) & 0x07
    mantissa = u & 0x0F
    sample = (((mantissa << 3) + 0x84) << exponent) - 0x84
    return np.where(sign != 0, -sample, sample).astype(np.int16)


def _build_ulaw_encode() -> np.ndarray:
    x = np.arange(-32768, 32768, dtype=np.int32)
    sign = np.where(x < 0, 0x80, 0)
    mag = np.minimum(np.abs(x), 32635) + 0x84
    exponent = np.floor(np.log2(np.maximum(mag, 1))).astype(np.int32) - 7
    exponent = np.clip(exponent, 0, 7)
    mantissa = (mag >> (exponent + 3)) & 0x0F
    return (~(sign | (exponent << 4) | mantissa) & 0xFF).astype(np.uint8)


_ULAW_DEC = _build_ulaw_decode()
_ULAW_ENC = _build_ulaw_encode()          # index = int16 + 32768


def ulaw_to_pcm16k(data: bytes) -> bytes:
    """Twilio frame (8 kHz μ-law) -> 16 kHz little-endian PCM16 for Gemini."""
    s = _ULAW_DEC[np.frombuffer(data, dtype=np.uint8)].astype(np.float32)
    if s.size == 0:
        return b""
    up = np.empty(s.size * 2, dtype=np.float32)
    up[0::2] = s
    up[1::2] = np.append((s[:-1] + s[1:]) / 2, s[-1])     # linear interpolation
    return np.clip(up, -32768, 32767).astype("<i2").tobytes()


class Downsampler:
    """24 kHz PCM16 from Gemini -> 8 kHz μ-law for Twilio (keeps leftovers)."""

    def __init__(self):
        self._rest = np.zeros(0, dtype=np.int16)

    def __call__(self, pcm24: bytes) -> bytes:
        s = np.concatenate([self._rest, np.frombuffer(pcm24, dtype="<i2")])
        n = (s.size // 3) * 3
        self._rest = s[n:]
        if n == 0:
            return b""
        avg = s[:n].reshape(-1, 3).astype(np.int32).mean(axis=1)   # cheap low-pass
        return _ULAW_ENC[np.clip(avg, -32768, 32767).astype(np.int32) + 32768].tobytes()

    def reset(self):
        self._rest = np.zeros(0, dtype=np.int16)


# ═════════════════════════════════════════════════════════════════════════════
#  Call contexts (what each call is about)
# ═════════════════════════════════════════════════════════════════════════════
_CONTEXTS: dict[str, dict] = {}
_CTX_LOCK = threading.Lock()
MODE_PROVIDER = None        # set by voice_calls.py: fn(ctx) -> dict


def new_context(mode: str, **data) -> str:
    cid = secrets.token_urlsafe(8)
    with _CTX_LOCK:
        now = time.time()
        for k in [k for k, v in _CONTEXTS.items() if now - v["created"] > 6 * 3600]:
            _CONTEXTS.pop(k, None)
        _CONTEXTS[cid] = {"mode": mode, "created": now, **data}
    return cid


def get_context(cid: str) -> dict | None:
    with _CTX_LOCK:
        return dict(_CONTEXTS[cid]) if cid in _CONTEXTS else None


def update_context(cid: str, **data) -> None:
    with _CTX_LOCK:
        if cid in _CONTEXTS:
            _CONTEXTS[cid].update(data)


def stream_twiml(cid: str) -> str:
    """TwiML that connects the call audio to our websocket."""
    url = public_url()
    if not url:
        raise RuntimeError("the public tunnel is not up")
    ws = url.replace("https://", "wss://") + "/twilio/stream"
    return ('<Response><Connect>'
            f'<Stream url="{escape(ws)}">'
            f'<Parameter name="ctx" value="{escape(cid)}"/>'
            f'<Parameter name="secret" value="{escape(SECRET)}"/>'
            '</Stream></Connect></Response>')


# ═════════════════════════════════════════════════════════════════════════════
#  Tunnel (cloudflared quick tunnel — free, no account)
# ═════════════════════════════════════════════════════════════════════════════
class Tunnel:
    def __init__(self):
        self.url = ""
        self.error = ""
        self.proc: subprocess.Popen | None = None
        self.port = DEFAULT_PORT
        self._lock = threading.Lock()
        self._listeners: list = []

    def on_url(self, fn):
        self._listeners.append(fn)

    def start(self, port: int):
        with self._lock:
            if self.proc and self.proc.poll() is None:
                return
            self.port = port
            threading.Thread(target=self._run, name="cloudflared", daemon=True).start()

    def stop(self):
        with self._lock:
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
            self.proc, self.url = None, ""

    def _run(self):
        backoff = 5
        while True:
            exe = find_cloudflared(download=True)
            if not exe:
                self.error = ("cloudflared isn't installed and couldn't be downloaded — "
                              "see VOICE_CALLS_SETUP.md")
                print(f"[Calls] {self.error}")
                return
            flags = 0x08000000 if platform.system() == "Windows" else 0     # CREATE_NO_WINDOW
            try:
                proc = subprocess.Popen(
                    [exe, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{self.port}"],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                    encoding="utf-8", errors="replace", creationflags=flags)
            except Exception as e:
                self.error = f"couldn't start cloudflared: {e}"
                print(f"[Calls] {self.error}")
                return
            self.proc = proc
            for line in proc.stdout:
                m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
                if m and m.group(0) != self.url:
                    self.url, self.error = m.group(0), ""
                    print(f"[Calls] 🌐 Public address: {self.url}")
                    for fn in self._listeners:
                        threading.Thread(target=fn, args=(self.url,), daemon=True).start()
            proc.wait()
            if self.proc is not proc:      # stopped on purpose
                return
            self.url = ""
            self.error = "tunnel dropped — restarting"
            print(f"[Calls] tunnel exited; restarting in {backoff}s")
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)


TUNNEL = Tunnel()


FIXED_URL = ""        # set when the server has its own domain (cloud + Caddy)


def public_url() -> str:
    return FIXED_URL or TUNNEL.url


def find_cloudflared(download: bool = False) -> str | None:
    exe_name = "cloudflared.exe" if platform.system() == "Windows" else "cloudflared"
    local = TOOLS_DIR / exe_name
    if local.exists():
        return str(local)
    found = shutil.which("cloudflared")
    if found:
        return found
    return _download_cloudflared(local) if download else None


def _download_cloudflared(dest: Path) -> str | None:
    system, machine = platform.system(), platform.machine().lower()
    arch = "arm64" if machine in ("arm64", "aarch64") else "amd64"
    base = "https://github.com/cloudflare/cloudflared/releases/latest/download/"
    if system == "Windows":
        name = f"cloudflared-windows-{arch}.exe"
    elif system == "Linux":
        name = f"cloudflared-linux-{arch}"
    else:
        print("[Calls] On macOS install cloudflared with:  brew install cloudflared")
        return None
    try:
        import requests
        print(f"[Calls] Downloading {name} (one time, ~40 MB)…")
        TOOLS_DIR.mkdir(exist_ok=True)
        tmp = dest.with_suffix(".part")
        with requests.get(base + name, stream=True, timeout=120) as r:
            r.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(1 << 16):
                    f.write(chunk)
        tmp.replace(dest)
        if system != "Windows":
            dest.chmod(0o755)
        print("[Calls] cloudflared ready.")
        return str(dest)
    except Exception as e:
        print(f"[Calls] cloudflared download failed: {e}")
        return None


# ═════════════════════════════════════════════════════════════════════════════
#  Twilio helpers
# ═════════════════════════════════════════════════════════════════════════════
def twilio_signature_ok(auth_token: str, url: str, params: dict, signature: str) -> bool:
    payload = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    digest = hmac.new(auth_token.encode(), payload.encode(), hashlib.sha1).digest()
    return hmac.compare_digest(base64.b64encode(digest).decode(), signature or "")


def point_number_at(url: str, cfg: dict) -> str:
    """Set the Twilio number's 'A call comes in' webhook to this run's address."""
    import requests
    sid, tok = cfg["account_sid"].strip(), cfg["auth_token"].strip()
    num = cfg["from_number"].strip()
    base = f"https://api.twilio.com/2010-04-01/Accounts/{sid}"
    r = requests.get(f"{base}/IncomingPhoneNumbers.json", params={"PhoneNumber": num},
                     auth=(sid, tok), timeout=20)
    nums = r.json().get("incoming_phone_numbers", [])
    if not nums:
        return f"couldn't find {num} on the Twilio account"
    r = requests.post(f"{base}/IncomingPhoneNumbers/{nums[0]['sid']}.json",
                      data={"VoiceUrl": url + "/twilio/voice", "VoiceMethod": "POST"},
                      auth=(sid, tok), timeout=20)
    return "" if r.status_code < 300 else f"Twilio refused the webhook update ({r.status_code})"


def _phone_cfg() -> dict:
    try:
        from plugins import phone_call
        return phone_call._cfg()
    except Exception:
        return {}


# ═════════════════════════════════════════════════════════════════════════════
#  Web server
# ═════════════════════════════════════════════════════════════════════════════
_SERVER_STARTED = False
ACTIVE_CALLS: dict[str, "CallSession"] = {}
ROUTE_HOOKS: list = []      # fn(app) — other modules add their own pages / APIs


def start_server(port: int = DEFAULT_PORT) -> str:
    """Start the local server once. Returns '' or an error message."""
    global _SERVER_STARTED
    if _SERVER_STARTED:
        return ""
    try:
        import uvicorn
        app = _build_app()
    except ImportError as e:
        return f"missing package: {e.name} (pip install fastapi \"uvicorn[standard]\")"
    for hook in ROUTE_HOOKS:
        try:
            hook(app)
        except Exception:
            traceback.print_exc()
    import os
    config = uvicorn.Config(app, host=os.environ.get("HOST", "127.0.0.1"), port=port, log_level="warning",
                            ws_ping_interval=None)
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, name="call-server", daemon=True).start()
    _SERVER_STARTED = True
    return ""


def _build_app():
    from fastapi import FastAPI, Request, WebSocket
    from fastapi.responses import Response

    app = FastAPI()

    @app.get("/health")
    async def health():
        return {"ok": True, "calls": len(ACTIVE_CALLS)}

    @app.post("/twilio/voice")
    async def incoming(request: Request):
        form = dict(await request.form())
        cfg = _phone_cfg()
        url = public_url() + "/twilio/voice"
        if not twilio_signature_ok(cfg.get("auth_token", ""), url, form,
                                   request.headers.get("X-Twilio-Signature", "")):
            print("[Calls] rejected a webhook with a bad Twilio signature")
            return Response("<Response><Reject/></Response>", media_type="application/xml")
        caller = re.sub(r"\s", "", str(form.get("From", "")))
        mine = re.sub(r"\s", "", str(cfg.get("to_number", "")))
        if not mine or caller != mine:
            print(f"[Calls] rejected incoming call from {caller}")
            return Response("<Response><Reject/></Response>", media_type="application/xml")
        cid = new_context("inbound")
        return Response(stream_twiml(cid), media_type="application/xml")

    @app.websocket("/twilio/stream")
    async def stream(ws: WebSocket):
        await ws.accept()
        session = CallSession(ws)
        try:
            await session.run()
        except Exception:
            traceback.print_exc()
        finally:
            try:
                await ws.close()
            except Exception:
                pass

    return app


# ═════════════════════════════════════════════════════════════════════════════
#  One live phone conversation
# ═════════════════════════════════════════════════════════════════════════════
_PLUGIN_TOOLS = ("college_assistant", "attendance", "exam_planner", "habits", "expenses",
                 "birthdays", "focus_mode", "phone_call")


def plugin_tool_decls() -> list[dict]:
    import importlib
    out = []
    for name in _PLUGIN_TOOLS:
        try:
            if not st.plugin_enabled(name):
                continue
            p = importlib.import_module(f"plugins.{name}").PLUGIN
            out.append({"name": p["name"], "description": p["description"],
                        "parameters": p["parameters"]})
        except Exception:
            continue
    return out


END_CALL_DECL = {
    "name": "end_call",
    "description": "Hang up the phone call. Say a short goodbye FIRST, then call this.",
    "parameters": {"type": "OBJECT", "properties": {}},
}


class CallSession:
    def __init__(self, ws, gemini_connect=None):
        self.ws = ws
        self.stream_sid = ""
        self.call_sid = ""
        self.ctx: dict = {}
        self.spec: dict = {}
        self.session = None
        self.down = Downsampler()
        self.inbuf = bytearray()
        self.transcript: list[str] = []
        self._in_words: list[str] = []
        self._out_words: list[str] = []
        self.ending = False
        self.closed = asyncio.Event()
        self.started = time.time()
        self.state: dict = {}           # mode handlers keep their data here
        self._connect = gemini_connect or _gemini_connect

    # ── entry point ──────────────────────────────────────────────────────────
    async def run(self):
        # wait for Twilio's "start" event
        while True:
            msg = json.loads(await self.ws.receive_text())
            if msg.get("event") == "start":
                break
            if msg.get("event") == "stop":
                return
        start = msg["start"]
        self.stream_sid = start.get("streamSid", "")
        self.call_sid = start.get("callSid", "")
        params = start.get("customParameters") or {}
        if not hmac.compare_digest(str(params.get("secret", "")), SECRET):
            print("[Calls] stream rejected: wrong secret")
            return
        self.ctx = get_context(str(params.get("ctx", ""))) or {"mode": "inbound"}
        if MODE_PROVIDER is None:
            print("[Calls] voice_calls plugin not loaded")
            return
        self.spec = MODE_PROVIDER(self.ctx)
        ACTIVE_CALLS[self.call_sid] = self
        print(f"[Calls] ☎ {self.ctx['mode']} call connected")
        try:
            async with self._connect(self.spec) as session:
                self.session = session
                await session.send_client_content(
                    turns={"role": "user", "parts": [{"text": self.spec["opening"]}]},
                    turn_complete=True)
                tasks = [asyncio.create_task(self._from_twilio()),
                         asyncio.create_task(self._from_gemini()),
                         asyncio.create_task(self._clock())]
                await self.closed.wait()
                for t in tasks:
                    t.cancel()
        finally:
            ACTIVE_CALLS.pop(self.call_sid, None)
            self._flush_words()
            self._finish()

    # ── Twilio -> Gemini ─────────────────────────────────────────────────────
    async def _from_twilio(self):
        try:
            while True:
                msg = json.loads(await self.ws.receive_text())
                ev = msg.get("event")
                if ev == "media":
                    self.inbuf += base64.b64decode(msg["media"]["payload"])
                    if len(self.inbuf) >= 800:           # 100 ms batches
                        pcm = ulaw_to_pcm16k(bytes(self.inbuf))
                        self.inbuf.clear()
                        await self._send_audio(pcm)
                elif ev == "mark" and msg.get("mark", {}).get("name") == "end":
                    self.closed.set()
                    return
                elif ev == "stop":
                    self.closed.set()
                    return
        except Exception:
            self.closed.set()

    async def _send_audio(self, pcm: bytes):
        from google.genai import types
        await self.session.send_realtime_input(
            audio=types.Blob(data=pcm, mime_type="audio/pcm;rate=16000"))

    # ── Gemini -> Twilio ─────────────────────────────────────────────────────
    async def _from_gemini(self):
        try:
            while not self.closed.is_set():
                got = False
                async for r in self.session.receive():
                    got = True
                    if r.data:
                        await self._play(r.data)
                    sc = r.server_content
                    if sc:
                        if getattr(sc, "interrupted", False):
                            self.down.reset()
                            await self._send({"event": "clear", "streamSid": self.stream_sid})
                        if sc.input_transcription and sc.input_transcription.text:
                            self._words("in", sc.input_transcription.text)
                        if sc.output_transcription and sc.output_transcription.text:
                            self._words("out", sc.output_transcription.text)
                        if sc.turn_complete:
                            self._flush_words()
                    if r.tool_call:
                        await self._tools(r.tool_call.function_calls)
                if not got:
                    await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[Calls] Gemini stream ended: {e}")
            self.closed.set()

    async def _play(self, pcm24: bytes):
        ulaw = self.down(pcm24)
        for i in range(0, len(ulaw), 1600):                  # 200 ms per message
            await self._send({"event": "media", "streamSid": self.stream_sid,
                              "media": {"payload": base64.b64encode(ulaw[i:i + 1600]).decode()}})

    async def _send(self, obj: dict):
        try:
            await self.ws.send_text(json.dumps(obj))
        except Exception:
            self.closed.set()

    async def _tools(self, calls):
        from google.genai import types
        self._flush_words()
        responses = []
        for fc in calls:
            name, args = fc.name, dict(fc.args or {})
            print(f"[Calls] 🔧 {name} {args}")
            if name == "end_call":
                result = "Ending the call."
                asyncio.create_task(self._hang_up())
            else:
                try:
                    result = await asyncio.to_thread(self._run_tool, name, args)
                except Exception as e:
                    result = f"Error: {e}"
            self.transcript.append(f"[{name}] {result}")
            responses.append(types.FunctionResponse(id=fc.id, name=name, response={"result": str(result)}))
        await self.session.send_tool_response(function_responses=responses)

    def _run_tool(self, name: str, args: dict) -> str:
        handler = self.spec.get("handle_tool")
        if handler:
            out = handler(name, args, self)
            if out is not None:
                return str(out)
        if name in _PLUGIN_TOOLS:
            import importlib
            return str(importlib.import_module(f"plugins.{name}").run(args) or "Done.")
        return f"Unknown tool {name}"

    async def _hang_up(self):
        if self.ending:
            return
        self.ending = True
        await asyncio.sleep(0.5)
        # Twilio echoes this mark once everything sent before it has played.
        await self._send({"event": "mark", "streamSid": self.stream_sid, "mark": {"name": "end"}})
        try:
            await asyncio.wait_for(self.closed.wait(), timeout=20)
        except asyncio.TimeoutError:
            self.closed.set()

    async def _clock(self):
        limit = max(2, int(self.spec.get("max_minutes", 10))) * 60
        await asyncio.sleep(limit - 60)
        if not self.closed.is_set() and not self.ending:
            await self.session.send_client_content(
                turns={"role": "user", "parts": [{"text":
                       "(System: one minute left on this call. Wrap up now, say goodbye and call end_call.)"}]},
                turn_complete=True)
        await asyncio.sleep(70)
        self.closed.set()

    # ── transcript ───────────────────────────────────────────────────────────
    def _words(self, who: str, text: str):
        if who == "in":
            if self._out_words:
                self._flush_words()
            self._in_words.append(text)
        else:
            if self._in_words:
                self._flush_words()
            self._out_words.append(text)

    def _flush_words(self):
        if self._in_words:
            self.transcript.append("You: " + "".join(self._in_words).strip())
            self._in_words = []
        if self._out_words:
            self.transcript.append("Jarvis: " + "".join(self._out_words).strip())
            self._out_words = []

    def _finish(self):
        mins = (time.time() - self.started) / 60
        print(f"[Calls] ☎ {self.ctx.get('mode')} call ended after {mins:.1f} min")
        if self.transcript:
            try:
                CALL_LOG_DIR.mkdir(parents=True, exist_ok=True)
                path = CALL_LOG_DIR / f"{datetime.now():%Y-%m-%d_%H%M}_{self.ctx.get('mode', 'call')}.txt"
                path.write_text("\n".join(self.transcript), encoding="utf-8")
            except Exception as e:
                print(f"[Calls] couldn't save transcript: {e}")
        on_end = self.spec.get("on_end")
        if on_end:
            try:
                on_end(self)
            except Exception:
                traceback.print_exc()


def _gemini_connect(spec: dict):
    """Async context manager for a Gemini Live session configured for a phone call."""
    from google import genai
    from google.genai import types
    from core import gemini
    try:
        from memory.config_manager import get_voice
        voice = get_voice()
    except Exception:
        voice = "Charon"
    config = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        system_instruction=spec["system"],
        tools=[{"function_declarations": spec["tools"] + [END_CALL_DECL]}],
        input_audio_transcription={},
        output_audio_transcription={},
        speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(
            prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice))),
    )
    client = genai.Client(api_key=gemini.api_key(), http_options={"api_version": "v1beta"})
    return client.aio.live.connect(model=gemini.live_model(), config=config)
