"""
Free "Jarvis calls" — no Twilio, no SIM, no per-minute cost.

How a call works
  1. At call time Jarvis sends a top-priority push through ntfy (free app) —
     your phone rings/alerts loudly. Telegram gets the same link as a backup.
  2. Tap it → a Jarvis call page opens in the browser → tap ANSWER.
  3. You talk two-way with Jarvis (Gemini Live), exactly like a phone call:
     briefing, recap, check-in, journal, quiz, reminders.
  4. Missed it? It rings again; if you still don't answer, the briefing /
     recap arrives as text on Telegram instead.

You can also call Jarvis any time from the Jarvis app page (bookmark it /
"Add to Home screen"): send /app on Telegram to get the link.

Runs on the always-on cloud server (see CLOUD_SETUP.md) or, when you have no
server, on this PC while it's on.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import secrets
import threading
import time
from datetime import datetime

from plugins import _bus as bus
from plugins import _call_bridge as bridge
from plugins import _store as st
from plugins import _telegram as tg

NAME = "app_call"
NTFY_DEFAULT = "https://ntfy.sh"


def server_key() -> str:
    """The secret that unlocks the app page and the data API (one per install)."""
    key = str(st.setting("cloud", "key", "") or "").strip()
    if not key:
        key = secrets.token_urlsafe(24)
        _save_setting("cloud", "key", key)
    return key


def topic() -> str:
    t = str(st.setting(NAME, "ntfy_topic", "") or "").strip()
    if not t:
        t = "jarvis-" + secrets.token_hex(6)
        _save_setting(NAME, "ntfy_topic", t)
    return t


def _save_setting(ns: str, key: str, value) -> None:
    try:
        from memory.config_manager import get_plugin_config, save_plugin_config
        cfg = get_plugin_config(ns) or {}
        cfg[key] = value
        save_plugin_config(ns, cfg)
    except Exception as e:
        print(f"[AppCall] couldn't save setting: {e}")


def pages_url() -> str:
    """The free GitHub Pages call app (works even when the PC is off)."""
    return str(st.setting("github", "pages_url", "") or __import__("os").environ.get("PAGES_URL", "")).strip().rstrip("/")


def app_link() -> str:
    if pages_url():
        return pages_url() + "/"
    url = bridge.public_url()
    return f"{url}/app?k={server_key()}" if url else ""


def _test_ring(values: dict):
    if pages_url() and st.is_linked():
        ok, info = ring("reminder", "This is a test call. If you can hear me, calls work!", "manual")
        return ok, ("Ringing — tap the ntfy alert to answer." if ok else info)
    if not bridge.public_url():
        return False, "The call server isn't online yet (no public address)."
    ok = _ntfy("Test call from Jarvis", "If your phone made a sound, ringing works. 🎉",
               app_link(), priority="5")
    return ok, (f"Sent. Subscribe to topic '{topic()}' in the ntfy app if nothing arrived."
                if ok else "Couldn't reach ntfy.")


PLUGIN_SETTINGS = {
    "namespace": NAME,
    "title": "FREE APP CALLS (NTFY)",
    "fields": [
        {"key": "ntfy_topic", "label": "ntfy topic (keep it secret; auto-made)", "type": "text"},
        {"key": "ntfy_server", "label": "ntfy server", "type": "text", "default": NTFY_DEFAULT},
        {"key": "rings", "label": "Ring attempts if unanswered", "type": "text", "default": "2"},
        {"key": "ring_gap", "label": "Seconds between rings", "type": "text", "default": "90"},
        {"key": "telegram_link", "label": "Also send the answer link on Telegram", "type": "toggle", "default": True},
    ],
    "action": {"label": "TEST RING", "run": _test_ring},
}

PLUGIN = {
    "name": "app_call",
    "description": (
        "The free Jarvis app call. Use when the user asks for the Jarvis app link, "
        "how to call Jarvis from the phone, or to test the call ringing."
    ),
    "parameters": {"type": "OBJECT", "properties": {
        "action": {"type": "STRING", "description": "link or test"}}, "required": ["action"]},
}


def run(parameters: dict, player=None, session_memory=None) -> str:
    if (parameters.get("action") or "link") == "test":
        return _test_ring({})[1]
    link = app_link()
    if not link:
        return "Sir, the call server isn't online yet."
    tg.send(f"📱 Your Jarvis app: {link}\nOpen it on your phone → browser menu → Add to Home screen.")
    return "I've sent the Jarvis app link to your Telegram, sir."


# ═════════════════════════════════════════════════════════════════════════════
#  Ringing
# ═════════════════════════════════════════════════════════════════════════════
def _ntfy(title: str, body: str, click: str, priority: str = "5") -> bool:
    try:
        import requests
        server = str(st.setting(NAME, "ntfy_server", NTFY_DEFAULT) or NTFY_DEFAULT).rstrip("/")
        headers = {"Title": title, "Priority": priority, "Tags": "telephone_receiver"}
        if click:
            headers["Click"] = click
            headers["Actions"] = f"view, Answer, {click}, clear=true"
        r = requests.post(f"{server}/{topic()}", data=body.encode("utf-8"), headers=headers, timeout=10)
        return r.status_code < 300
    except Exception as e:
        print(f"[AppCall] ntfy failed: {e}")
        return False


def ready() -> bool:
    return bool(bridge.public_url()) or bool(pages_url() and st.is_linked())


LABELS = {"briefing": "Morning briefing", "recap": "Evening recap", "checkin": "Study check-in",
          "checkin_followup": "Check-in follow-up", "journal": "Night journal", "quiz": "Quiz time",
          "reminder": "Reminder"}


def ring(kind: str, message: str = "", source: str = "auto", script: str | None = None,
         wake: bool = False, extra: dict | None = None) -> tuple[bool, str]:
    """Ring the phone for a call. Returns at once; ringing/retries run in a thread."""
    if not ready():
        return False, "call server offline"
    if st.is_lite() or not bridge.public_url():
        return _pages_ring(kind, message, script, wake, extra)
    cid = bridge.new_context(kind, message=message, script=script or "", answered=False,
                             **(extra or {}))
    threading.Thread(target=_ring_loop, args=(cid, kind, message, wake), daemon=True).start()
    return True, cid


def _ring_loop(cid: str, kind: str, message: str, wake: bool):
    url = f"{bridge.public_url()}/call/{cid}?k={server_key()}"
    label = LABELS.get(kind, "Call")
    tries = max(1, st.to_int(st.setting(NAME, "rings", 2), 2))
    if wake:
        tries = max(tries, 5)
    gap = max(30, st.to_int(st.setting(NAME, "ring_gap", 90), 90))
    for attempt in range(1, tries + 1):
        what = message if kind == "reminder" and message else label
        _ntfy("Jarvis is calling", f"📞 {what} — tap to answer", url)
        if attempt == 1 and st.truthy(st.setting(NAME, "telegram_link", True)):
            tg.send(f"📞 Jarvis is calling — {label}. Tap to answer:\n{url}")
        deadline = time.time() + gap
        while time.time() < deadline:
            time.sleep(2)
            ctx = bridge.get_context(cid) or {}
            if ctx.get("answered"):
                return
    bus.log(f"📞 {label} call wasn't answered.")
    _missed(kind, message)


# ── Ringing through the GitHub Pages app (PC off) ───────────────────────────
CALL_LOG = "call_log"


def _pages_link(rid: str, kind: str, message: str) -> str:
    from urllib.parse import quote
    return f"{pages_url()}/#mode={kind}&id={rid}" + (f"&msg={quote(message)}" if message else "")


def _pages_ring(kind, message, script, wake, extra) -> tuple[bool, str]:
    from datetime import datetime as _dt
    rid = secrets.token_hex(4)
    text = script or message
    with st.lock():
        log = st.load(CALL_LOG, {"rings": []})
        log["rings"].append({"id": rid, "kind": kind, "message": text, "wake": bool(wake),
                             "first": _dt.now().isoformat(timespec="seconds"), "last": time.time(),
                             "attempts": 1, "answered": False, "done": False})
        log["rings"] = log["rings"][-50:]
        st.save(CALL_LOG, log)
    _send_ring(rid, kind, text, first=True)
    return True, rid


def _send_ring(rid: str, kind: str, message: str, first: bool):
    url = _pages_link(rid, kind, message)
    label = LABELS.get(kind, "Call")
    what = message if kind == "reminder" and message else label
    _ntfy("Jarvis is calling", f"📞 {what[:120]} — tap to answer", url)
    if first and st.truthy(st.setting(NAME, "telegram_link", True)):
        tg.send(f"📞 Jarvis is calling — {label}. Tap to answer:\n{url}")


def pages_retry_tick():
    """Ring again if unanswered; after the last ring, fall back to text."""
    if not pages_url():
        return
    tries = max(1, st.to_int(st.setting(NAME, "rings", 2), 2))
    gap = max(60, st.to_int(st.setting(NAME, "ring_gap", 90), 90))
    changed, missed = False, []
    with st.lock():
        log = st.load(CALL_LOG, {"rings": []})
        now = time.time()
        for r in log["rings"]:
            if r.get("answered") or r.get("done") or now - r.get("last", 0) < gap:
                continue
            limit = max(tries, 5) if r.get("wake") else tries
            if r["attempts"] < limit:
                r["attempts"] += 1
                r["last"] = now
                _send_ring(r["id"], r["kind"], r.get("message", ""), first=False)
            else:
                r["done"] = True
                missed.append(r)
            changed = True
        if changed:
            st.save(CALL_LOG, log)
    for r in missed:
        bus.log(f"📞 {LABELS.get(r['kind'], 'Call')} call wasn't answered.")
        _missed(r["kind"], r.get("message", ""))


st.start_loop("pages-ring-retry", pages_retry_tick, every_s=30, first_delay_s=30)


def _missed(kind: str, message: str):
    try:
        if kind in ("briefing", "recap"):
            from plugins import phone_call
            text = phone_call._build_script(kind, message, phone_call._cfg())
            tg.send(f"📵 You missed the call, so here it is as text:\n\n{text}")
        elif kind == "reminder":
            tg.send(f"📵 Missed call — your reminder: {message}")
        else:
            tg.send(f"📵 Missed your {LABELS.get(kind, kind).lower()} call. Send /{kind.split('_')[0]} "
                    "when you're free and I'll call again.")
    except Exception as e:
        print(f"[AppCall] missed-call message failed: {e}")


# ═════════════════════════════════════════════════════════════════════════════
#  Web pages + the browser audio stream
# ═════════════════════════════════════════════════════════════════════════════
def _key_ok(k: str) -> bool:
    return bool(k) and hmac.compare_digest(str(k), server_key())


def register(app):
    from fastapi import Request, WebSocket
    from fastapi.responses import HTMLResponse, JSONResponse

    @app.get("/app")
    async def home(k: str = ""):
        if not _key_ok(k):
            return HTMLResponse("Not found", status_code=404)
        return HTMLResponse(_page(cid="", key=k))

    @app.get("/call/{cid}")
    async def call_page(cid: str, k: str = ""):
        if not _key_ok(k):
            return HTMLResponse("Not found", status_code=404)
        ctx = bridge.get_context(cid)
        if not ctx:
            return HTMLResponse(_page(cid="", key=k, note="That call has expired — you can call Jarvis below."))
        return HTMLResponse(_page(cid=cid, key=k, label=LABELS.get(ctx["mode"], "Jarvis")))

    @app.post("/api/app/new")
    async def new_call(request: Request):
        body = await request.json()
        if not _key_ok(body.get("k", "")):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        mode = body.get("mode") or "inbound"
        if mode not in ("inbound", "briefing", "recap", "checkin", "journal", "quiz"):
            mode = "inbound"
        cid = bridge.new_context(mode, message=body.get("topic", ""), answered=True)
        return {"cid": cid}

    @app.websocket("/app/stream")
    async def app_stream(ws: WebSocket):
        await ws.accept()
        try:
            await BrowserCallSession(ws).run()
        except Exception as e:
            print(f"[AppCall] call error: {e}")
        finally:
            try:
                await ws.close()
            except Exception:
                pass


bridge.ROUTE_HOOKS.append(register)


class BrowserCallSession(bridge.CallSession):
    """Same brain as a Twilio call; the phone's browser sends/plays raw PCM."""

    async def run(self):
        first = await self.ws.receive()
        try:
            msg = json.loads(first.get("text") or "{}")
        except Exception:
            return
        if msg.get("event") != "start" or not _key_ok(msg.get("key", "")):
            return
        cid = str(msg.get("ctx", ""))
        self.ctx = bridge.get_context(cid)
        if not self.ctx:
            await self._send({"event": "error", "text": "This call has expired."})
            return
        bridge.update_context(cid, answered=True)
        if bridge.MODE_PROVIDER is None:
            await self._send({"event": "error", "text": "Jarvis call brain isn't loaded."})
            return
        self.spec = bridge.MODE_PROVIDER(self.ctx)
        self.call_sid = cid
        bridge.ACTIVE_CALLS[cid] = self
        print(f"[AppCall] ☎ {self.ctx['mode']} call answered in the app")
        try:
            async with self._connect(self.spec) as session:
                self.session = session
                await self._send({"event": "connected"})
                await session.send_client_content(
                    turns={"role": "user", "parts": [{"text": self.spec["opening"]}]},
                    turn_complete=True)
                tasks = [asyncio.create_task(self._from_client()),
                         asyncio.create_task(self._from_gemini()),
                         asyncio.create_task(self._clock())]
                await self.closed.wait()
                for t in tasks:
                    t.cancel()
        finally:
            bridge.ACTIVE_CALLS.pop(cid, None)
            self._flush_words()
            self._finish()

    async def _from_client(self):
        try:
            while True:
                m = await self.ws.receive()
                if m.get("type") == "websocket.disconnect":
                    break
                if m.get("bytes"):
                    await self._send_audio(m["bytes"])
                elif m.get("text"):
                    ev = json.loads(m["text"]).get("event")
                    if ev in ("hangup", "ended"):
                        break
        except Exception:
            pass
        self.closed.set()

    async def _play(self, pcm24: bytes):
        try:
            await self.ws.send_bytes(pcm24)
        except Exception:
            self.closed.set()

    async def _send(self, obj: dict):
        if obj.get("event") == "clear":
            obj = {"event": "clear"}
        try:
            await self.ws.send_text(json.dumps(obj))
        except Exception:
            self.closed.set()

    async def _hang_up(self):
        if self.ending:
            return
        self.ending = True
        await asyncio.sleep(0.5)
        await self._send({"event": "end"})     # the page closes once playback finishes
        try:
            await asyncio.wait_for(self.closed.wait(), timeout=25)
        except asyncio.TimeoutError:
            self.closed.set()

    def _words(self, who: str, text: str):
        super()._words(who, text)
        asyncio.ensure_future(self._send({"event": "caption", "who": "you" if who == "in" else "jarvis",
                                          "text": text}))


# ═════════════════════════════════════════════════════════════════════════════
#  The page (one self-contained HTML file)
# ═════════════════════════════════════════════════════════════════════════════
def _names():
    try:
        from memory.config_manager import get_user_name, get_assistant_name
        return get_user_name() or "", get_assistant_name() or "Jarvis"
    except Exception:
        return "", "Jarvis"


def _page(cid: str, key: str, label: str = "", note: str = "") -> str:
    user, asst = _names()
    cfg = json.dumps({"cid": cid, "key": key, "label": label, "asst": asst, "note": note})
    return PAGE.replace("__CFG__", cfg).replace("__ASST__", asst)


PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#04121a"><meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes"><title>__ASST__</title>
<style>
:root{--bg:#04121a;--card:#0a2230;--pri:#2ee6d6;--dim:#7fa3ad;--red:#ff4d5e;--green:#22c55e;--text:#e8f6f8}
*{box-sizing:border-box}html,body{margin:0;height:100%;background:var(--bg);color:var(--text);
font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{min-height:100%;display:flex;flex-direction:column;align-items:center;padding:28px 16px calc(28px + env(safe-area-inset-bottom))}
h1{font-size:15px;letter-spacing:.25em;color:var(--dim);font-weight:600;margin:6px 0 0}
#who{font-size:30px;font-weight:700;margin:4px 0 2px}#status{color:var(--dim);font-size:15px;min-height:20px}
.orb{width:170px;height:170px;border-radius:50%;margin:34px 0 22px;position:relative;
background:radial-gradient(circle at 50% 45%,#5ff7ea 0,#16a99d 38%,#073c43 70%,transparent 72%);
box-shadow:0 0 60px rgba(46,230,214,.35);transition:transform .12s}
.orb.ring{animation:pulse 1.1s infinite}@keyframes pulse{50%{transform:scale(1.08);box-shadow:0 0 90px rgba(46,230,214,.6)}}
#caps{width:100%;max-width:520px;flex:1;overflow-y:auto;background:var(--card);border-radius:16px;padding:14px;
font-size:15px;line-height:1.45;min-height:120px}
#caps p{margin:0 0 8px}#caps .you{color:#9fd3ff}#caps .jarvis{color:var(--text)}#caps .sys{color:var(--dim);font-style:italic}
.row{display:flex;gap:22px;margin-top:22px;flex-wrap:wrap;justify-content:center}
button{border:0;border-radius:999px;font-size:16px;font-weight:700;color:#fff;padding:16px 26px;min-width:120px;cursor:pointer}
.go{background:var(--green)}.stop{background:var(--red)}.ghost{background:#163847;color:var(--text)}
.chips{display:flex;gap:8px;flex-wrap:wrap;justify-content:center;margin-top:14px}
.chips button{min-width:0;padding:10px 14px;font-size:14px;font-weight:600;background:#163847}
#timer{font-variant-numeric:tabular-nums;color:var(--dim);margin-top:6px}
.hide{display:none!important}
</style></head><body><main>
<h1>__ASST__</h1><div id="who">__ASST__</div><div id="status"></div><div id="timer"></div>
<div class="orb" id="orb"></div>
<div id="caps"></div>
<div class="row" id="idle"><button class="go" id="answer">Answer</button></div>
<div class="chips" id="modes"></div>
<div class="row hide" id="live"><button class="ghost" id="mute">Mute</button><button class="stop" id="hang">Hang up</button></div>
</main>
<script>
const C=__CFG__;const $=id=>document.getElementById(id);
let ws,inCtx,outCtx,stream,node,nextT=0,srcs=[],muted=false,ending=false,t0=0,tick,wake;
function cap(cls,txt){const p=document.createElement('p');p.className=cls;p.textContent=txt;$('caps').appendChild(p);$('caps').scrollTop=1e9;return p}
let lastWho='',lastP=null;
function caption(who,txt){if(who!==lastWho||!lastP){lastP=cap(who,'');lastWho=who}lastP.textContent+=txt;$('caps').scrollTop=1e9}
function status(s){$('status').textContent=s}
if(C.cid){$('who').textContent=C.label||C.asst;status('Incoming call…');$('orb').classList.add('ring')}
else{$('who').textContent=C.asst;status(C.note||'Tap to call '+C.asst);$('answer').textContent='Call '+C.asst;
 [['Briefing','briefing'],['Recap','recap'],['Quiz me','quiz'],['Journal','journal'],['Check-in','checkin']].forEach(([l,m])=>{
  const b=document.createElement('button');b.textContent=l;b.onclick=()=>start(m);$('modes').appendChild(b)})}
$('answer').onclick=()=>start(C.cid?null:'inbound');
$('hang').onclick=()=>{try{ws.send(JSON.stringify({event:'hangup'}))}catch(e){}finish('Call ended')};
$('mute').onclick=()=>{muted=!muted;$('mute').textContent=muted?'Unmute':'Mute'};
const WORKLET=`class Cap extends AudioWorkletProcessor{constructor(){super();this.r=sampleRate/16000;this.p=0;this.b=new Int16Array(1600);this.i=0}
process(inp){const ch=inp[0]&&inp[0][0];if(!ch)return true;
for(;this.p<ch.length;this.p+=this.r){const k=Math.floor(this.p),f=this.p-k,a=ch[k],c=k+1<ch.length?ch[k+1]:a;
let v=a+(c-a)*f;v=Math.max(-1,Math.min(1,v));this.b[this.i++]=v<0?v*32768:v*32767;
if(this.i===1600){this.port.postMessage(this.b.buffer.slice(0));this.i=0}}this.p-=ch.length;return true}}
registerProcessor('cap',Cap)`;
async function start(mode){
 $('idle').classList.add('hide');$('modes').classList.add('hide');$('orb').classList.remove('ring');status('Connecting…');
 try{
  stream=await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:true,noiseSuppression:true,autoGainControl:true,channelCount:1}});
 }catch(e){status('Microphone blocked — allow it in the browser and try again.');$('idle').classList.remove('hide');return}
 let cid=C.cid;
 if(!cid){const r=await fetch('/api/app/new',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({k:C.key,mode})});
  cid=(await r.json()).cid;if(!cid){status('Could not start the call.');return}}
 inCtx=new AudioContext();outCtx=new AudioContext();await inCtx.resume();await outCtx.resume();
 await inCtx.audioWorklet.addModule(URL.createObjectURL(new Blob([WORKLET],{type:'application/javascript'})));
 node=new AudioWorkletNode(inCtx,'cap');inCtx.createMediaStreamSource(stream).connect(node);
 node.port.onmessage=e=>{if(ws&&ws.readyState===1&&!muted)ws.send(e.data)};
 try{wake=await navigator.wakeLock.request('screen')}catch(e){}
 ws=new WebSocket((location.protocol==='https:'?'wss://':'ws://')+location.host+'/app/stream');ws.binaryType='arraybuffer';
 ws.onopen=()=>ws.send(JSON.stringify({event:'start',ctx:cid,key:C.key}));
 ws.onmessage=e=>{if(typeof e.data!=='string'){play(e.data);return}const m=JSON.parse(e.data);
  if(m.event==='connected'){status('Connected');$('live').classList.remove('hide');t0=Date.now();
    tick=setInterval(()=>{const s=Math.floor((Date.now()-t0)/1000);$('timer').textContent=Math.floor(s/60)+':'+String(s%60).padStart(2,'0')},500)}
  else if(m.event==='clear'){srcs.forEach(s=>{try{s.stop()}catch(e){}});srcs=[];nextT=0}
  else if(m.event==='caption'){caption(m.who,m.text)}
  else if(m.event==='end'){ending=true;const wait=Math.max(0,(nextT-outCtx.currentTime)*1000)+400;setTimeout(()=>{try{ws.send(JSON.stringify({event:'ended'}))}catch(e){}finish('Call ended')},wait)}
  else if(m.event==='error'){cap('sys',m.text);finish('Call failed')}};
 ws.onclose=()=>{if(!ending)finish('Call ended')};
}
function play(ab){const i16=new Int16Array(ab);if(!i16.length)return;const buf=outCtx.createBuffer(1,i16.length,24000);const ch=buf.getChannelData(0);
 for(let i=0;i<i16.length;i++)ch[i]=i16[i]/32768;const s=outCtx.createBufferSource();s.buffer=buf;s.connect(outCtx.destination);
 const at=Math.max(nextT,outCtx.currentTime+0.06);s.start(at);nextT=at+buf.duration;srcs.push(s);s.onended=()=>{srcs=srcs.filter(x=>x!==s)};
 $('orb').style.transform='scale(1.06)';setTimeout(()=>$('orb').style.transform='',120)}
function finish(msg){status(msg);clearInterval(tick);$('live').classList.add('hide');
 try{ws&&ws.close()}catch(e){}try{stream&&stream.getTracks().forEach(t=>t.stop())}catch(e){}try{inCtx&&inCtx.close()}catch(e){}
 try{wake&&wake.release()}catch(e){}
 if(!C.cid){setTimeout(()=>{$('idle').classList.remove('hide');$('modes').classList.remove('hide');status('Tap to call '+C.asst)},1500)}}
</script></body></html>"""
