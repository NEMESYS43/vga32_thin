"""Browser mirror, remote control and agent API for the desktop.

GET  /                 the remote-control page
GET  /frame?v=N        long-poll: PNG of the screen once its version differs from N
POST /input            JSON list of {t:"key"|"mouse", ...} events, injected as board input
GET  /api/state        desktop state (JSON)
GET  /api/screenshot   PNG (?window_id=&scale=)
POST /api/<tool>       run an agent tool with JSON arguments -> {"ok": true, "result": ...}
POST /mcp              MCP (Streamable HTTP, JSON responses) for agents such as Hermes
GET  /mcp              MCP server-to-client SSE stream: desktop events as notifications/message
GET  /api/events       SSE stream of desktop events (?since=&types=a,b&app_id=&window_id=)

Every request needs the HOST_KEY, since the desktop's Terminal runs shell commands:
?key=..., an "Authorization: Bearer ..." header, or an "X-VGA32-Key" header.
Agents can instead use a scoped token from settings["tokens"]: {"name", "token", "scopes"};
scopes are ui, input, terminal, web, settings, admin. The remote page needs the HOST_KEY.
"""
import hmac
import io
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from PIL import Image

from .agent import ALL_SCOPES, TOOLS, Agent, ImageResult, ToolError
from .client import Key, Mouse
from .palette import display_flat

SSE_PING = 15      # seconds between keep-alive comments on event streams


def _coerce(v):
    """Query-string values -> int/float/bool where they look like one."""
    if v in ("true", "false"):
        return v == "true"
    for t in (int, float):
        try:
            return t(v)
        except ValueError:
            pass
    return v


class WebUI:
    def __init__(self, key, port, desk, bind="0.0.0.0"):
        self.key = key
        self.port, self.bind = port, bind
        self.desk = desk
        self.agent = Agent(desk)
        self.png, self.size = None, (0, 0)
        self.version = 0
        self.connected = False
        self._cond = threading.Condition()

    def start(self):
        web = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _authed(self, q, host_only=False):
                """Checks the key; sets self.agent to an Agent with the caller's scopes."""
                auth = self.headers.get("Authorization", "")
                given = (q.get("key", [""])[0] or self.headers.get("X-VGA32-Key", "")
                         or (auth[7:] if auth.lower().startswith("bearer ") else ""))
                if hmac.compare_digest(given.encode(), web.key.encode()):
                    self.agent = web.agent
                    return True
                if not host_only:
                    for t in web.desk.settings["tokens"]:
                        tok = str(t.get("token", ""))
                        if tok and hmac.compare_digest(given.encode(), tok.encode()):
                            scopes = [s for s in t.get("scopes", ["ui"]) if s in ALL_SCOPES]
                            self.agent = Agent(web.desk, scopes, str(t.get("name", "token")))
                            return True
                self.close_connection = True       # any unread request body must not leak into the next request
                self._send(403, "text/plain", b"forbidden: pass the HOST_KEY as ?key=, a Bearer token or X-VGA32-Key",
                           [("Connection", "close")])
                return False

            def _send(self, code, ctype, body, headers=()):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                for k, v in headers:
                    self.send_header(k, v)
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def _json(self, code, obj):
                self._send(code, "application/json", json.dumps(obj, default=str).encode())

            def _body(self):
                n = int(self.headers.get("Content-Length", 0) or 0)
                raw = self.rfile.read(n) if n else b""
                return json.loads(raw) if raw.strip() else {}

            def _tool(self, name, args):
                try:
                    r = self.agent.run(name, args)
                except ToolError as e:
                    return self._json(400, {"ok": False, "error": str(e)})
                if isinstance(r, ImageResult):
                    return self._send(200, "image/png", r.png, [("X-Meta", json.dumps(r.meta))])
                self._json(200, {"ok": True, "result": r})

            def do_GET(self):
                u = urlparse(self.path)
                q = parse_qs(u.query)
                if not self._authed(q, host_only=u.path in ("/", "/frame")):
                    return
                if u.path == "/api/events" or (u.path == "/mcp" and "text/event-stream" in
                                               self.headers.get("Accept", "")):
                    return self._stream(q, mcp=u.path == "/mcp")
                if u.path == "/":
                    self._send(200, "text/html; charset=utf-8", PAGE.encode())
                elif u.path == "/frame":
                    try:
                        since = int(q.get("v", ["-1"])[0])
                    except ValueError:
                        since = -1
                    png, ver, size, conn = web.wait_frame(since, timeout=15)
                    if png is None:
                        self._send(204, "text/plain", b"", [("X-Version", str(ver)), ("X-Connected", str(int(conn)))])
                    else:
                        self._send(200, "image/png", png, [("X-Version", str(ver)), ("X-Connected", str(int(conn))),
                                                           ("X-Size", f"{size[0]}x{size[1]}")])
                elif u.path == "/api" or u.path == "/api/":
                    self._json(200, {"tools": [{k: t[k] for k in ("name", "description", "inputSchema")}
                                               for t in TOOLS]})
                elif u.path in ("/api/state", "/api/screenshot"):
                    args = {k: _coerce(v[0]) for k, v in q.items() if k != "key"}
                    self._tool("get_state" if u.path.endswith("state") else "screenshot", args)
                elif u.path == "/mcp":
                    self._send(405, "text/plain", b"use POST", [("Allow", "POST, DELETE")])
                else:
                    self._send(404, "text/plain", b"not found")

            def _stream(self, q, mcp=False):
                """Server-sent events until the client goes away."""
                arg = lambda k: q.get(k, [None])[0]
                since = int(arg("since")) if arg("since") not in (None, "") else web.desk.bus.last_seq()
                flt = {"types": [t for t in (arg("types") or "").split(",") if t] or None,
                       "app_id": arg("app_id"), "window_id": arg("window_id")}
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                try:
                    while True:
                        evs = web.desk.bus.wait(since, SSE_PING, **flt)
                        if not evs:
                            self.wfile.write(b": ping\n\n")
                        for ev in evs:
                            since = ev["seq"]
                            if mcp:
                                msg = {"jsonrpc": "2.0", "method": "notifications/message",
                                       "params": {"level": "info", "logger": "vga32", "data": ev}}
                                self.wfile.write(f"data: {json.dumps(msg, default=str)}\n\n".encode())
                            else:
                                self.wfile.write(f"id: {ev['seq']}\nevent: {ev['type']}\n"
                                                 f"data: {json.dumps(ev, default=str)}\n\n".encode())
                        self.wfile.flush()
                except (OSError, ValueError):
                    pass

            def do_DELETE(self):
                if self._authed(parse_qs(urlparse(self.path).query)):
                    self._send(200, "text/plain", b"")

            def do_POST(self):
                u = urlparse(self.path)
                if not self._authed(parse_qs(u.query), host_only=u.path == "/input"):
                    return
                try:
                    body = self._body()
                except ValueError as e:
                    return self._json(400, {"ok": False, "error": f"bad JSON: {e}"})
                if u.path == "/input":
                    try:
                        web.inject(body)
                    except (ValueError, TypeError, KeyError) as e:
                        return self._json(400, {"ok": False, "error": str(e)})
                    self._send(204, "text/plain", b"")
                elif u.path.startswith("/api/"):
                    if not isinstance(body, dict):
                        return self._json(400, {"ok": False, "error": "body must be a JSON object of arguments"})
                    self._tool(u.path[5:], body)
                elif u.path == "/mcp":
                    resp = self.agent.mcp(body)
                    if resp is None:
                        self._send(202, "text/plain", b"")
                    else:
                        self._json(200, resp)
                else:
                    self._send(404, "text/plain", b"not found")

        srv = ThreadingHTTPServer((self.bind, self.port), Handler)
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return self

    # ---------- desktop side ----------
    def publish(self, idx, connected):
        """idx: HxW palette indices, exactly what the board shows."""
        img = Image.fromarray(idx, "P")
        img.putpalette(display_flat())
        buf = io.BytesIO()
        img.save(buf, "PNG", compress_level=1)
        with self._cond:
            self.png, self.size = buf.getvalue(), (idx.shape[1], idx.shape[0])
            self.connected = connected
            self.version += 1
            self._cond.notify_all()

    def wait_frame(self, since, timeout):
        with self._cond:
            self._cond.wait_for(lambda: self.version != since, timeout)
            if self.version == since or self.png is None:
                return None, self.version, self.size, self.connected
            return self.png, self.version, self.size, self.connected

    def inject(self, events):
        q = self.desk.inbox
        for e in events:
            if e["t"] == "mouse":
                q.put(Mouse(int(e["x"]), int(e["y"]), int(e["b"]) & 7, int(e.get("w", 0)), web=True))
            elif e["t"] == "key":
                q.put(Key(str(e["name"]), str(e.get("char", ""))[:1], bool(e["down"]), int(e.get("mods", 0)) & 255))


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>VGA32 Remote</title>
<style>
:root{--bg:#1b1d22;--panel:#262931;--fg:#e6e6e6;--dim:#9aa0aa;--accent:#55aaff;--bad:#ff5555;--ok:#55ff55}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px system-ui,sans-serif;display:flex;flex-direction:column;align-items:center;min-height:100vh}
header{width:100%;display:flex;flex-wrap:wrap;gap:8px;align-items:center;padding:8px 16px;background:var(--panel)}
header h1{font-size:15px;margin:0 12px 0 0;font-weight:600}
button{background:#3a3e48;color:var(--fg);border:1px solid #4a4f5b;border-radius:6px;padding:5px 10px;font:inherit;cursor:pointer}
button:hover{border-color:var(--accent)}
#status{margin-left:auto;color:var(--dim);display:flex;align-items:center;gap:6px}
#dot{width:9px;height:9px;border-radius:50%;background:var(--bad)}
#dot.on{background:var(--ok)}
main{flex:1;display:flex;align-items:center;justify-content:center;width:100%;padding:16px}
#wrap{position:relative;max-width:100%}
canvas{display:block;width:100%;height:auto;image-rendering:pixelated;outline:2px solid #333;cursor:default;background:#000}
canvas:focus{outline-color:var(--accent)}
#hint{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;background:rgba(0,0,0,.55);color:#fff;font-size:16px;pointer-events:none}
#hint.hide{display:none}
#wrap.drop canvas{outline:3px dashed var(--accent)}
#file{display:none}
footer{color:var(--dim);font-size:12px;padding:0 16px 12px;text-align:center}
</style></head><body>
<header>
  <h1>VGA32 Remote</h1>
  <button data-combo="menu">Apps menu</button>
  <button data-combo="alttab">Alt+Tab</button>
  <button data-combo="altf4">Alt+F4</button>
  <button data-combo="esc">Esc</button>
  <button id="imgbtn" title="Show an image on the VGA screen (or drop / paste one onto the screen)">Image…</button>
  <button id="copybtn" title="Copy the desktop clipboard to this computer (paste text with Ctrl+V)">Copy</button>
  <input type="file" id="file" accept="image/*">
  <button id="scale">Scale: fit</button>
  <span id="status"><span id="dot"></span><span id="stxt">connecting…</span></span>
</header>
<main><div id="wrap"><canvas id="c" width="640" height="400" tabindex="0"></canvas>
<div id="hint">Click the screen to control it</div></div></main>
<footer>Mouse and keyboard go to the VGA32 desktop while the screen has focus. Win and Ctrl+Esc are taken by your OS, so use “Apps menu”. Right-click works. Drop or paste an image to show it on the VGA screen; pasted text goes into the focused window.</footer>
<script>
const KEY = new URLSearchParams(location.search).get("key") || "";
const cv = document.getElementById("c"), ctx = cv.getContext("2d");
const hint = document.getElementById("hint"), dot = document.getElementById("dot"), stxt = document.getElementById("stxt");
const wrap = document.getElementById("wrap");
let ver = -1, frames = 0, lastFps = performance.now(), fps = 0, connected = false;

function setStatus(){
  dot.classList.toggle("on", connected);
  stxt.textContent = connected ? `board connected · ${fps} fps` : "board disconnected";
}

async function pump(){
  for(;;){
    try{
      const r = await fetch(`/frame?v=${ver}&key=${encodeURIComponent(KEY)}`, {cache:"no-store"});
      connected = r.headers.get("X-Connected") === "1";
      if(r.status === 200){
        ver = +r.headers.get("X-Version");
        const [w,h] = r.headers.get("X-Size").split("x").map(Number);
        if(cv.width !== w || cv.height !== h){ cv.width = w; cv.height = h; applyScale(); }
        const bmp = await createImageBitmap(await r.blob());
        ctx.drawImage(bmp, 0, 0); bmp.close && bmp.close();
        frames++;
      }else if(r.status === 204){ ver = +(r.headers.get("X-Version") ?? ver); }
      else if(r.status === 403){ stxt.textContent = "wrong or missing ?key="; return; }
      else { await new Promise(r => setTimeout(r, 1000)); }
      const now = performance.now();
      if(now - lastFps > 1000){ fps = Math.round(frames * 1000 / (now - lastFps)); frames = 0; lastFps = now; }
      setStatus();
    }catch(e){ connected = false; setStatus(); await new Promise(r => setTimeout(r, 1000)); }
  }
}

// ---------- input ----------
let queue = [], sending = false, pendingMove = null;
function send(ev){ queue.push(ev); flush(); }
async function flush(){
  if(sending) return;
  if(pendingMove){ queue.push(pendingMove); pendingMove = null; }
  if(!queue.length) return;
  sending = true;
  const batch = queue; queue = [];
  try{ await fetch(`/input?key=${encodeURIComponent(KEY)}`, {method:"POST", body:JSON.stringify(batch)}); }catch(e){}
  sending = false;
  if(queue.length || pendingMove) flush();
}

let buttons = 0, mx = 0, my = 0;
function pos(e){
  const r = cv.getBoundingClientRect();
  mx = Math.max(0, Math.min(cv.width - 1, Math.floor((e.clientX - r.left) * cv.width / r.width)));
  my = Math.max(0, Math.min(cv.height - 1, Math.floor((e.clientY - r.top) * cv.height / r.height)));
}
const mouseEv = (w=0) => ({t:"mouse", x:mx, y:my, b:buttons, w});
cv.addEventListener("pointerdown", e => { cv.focus(); cv.setPointerCapture(e.pointerId); pos(e); buttons = e.buttons & 7; send(mouseEv()); e.preventDefault(); });
cv.addEventListener("pointerup", e => { pos(e); buttons = e.buttons & 7; send(mouseEv()); });
cv.addEventListener("pointermove", e => { pos(e); buttons = e.buttons & 7; pendingMove = mouseEv(); flush(); });
cv.addEventListener("wheel", e => { pos(e); send(mouseEv(e.deltaY < 0 ? 1 : -1)); e.preventDefault(); }, {passive:false});
cv.addEventListener("contextmenu", e => e.preventDefault());
cv.addEventListener("focus", () => hint.classList.add("hide"));
cv.addEventListener("blur", () => hint.classList.remove("hide"));

// Browser key -> FabGL VirtualKey name; modifier bits match the board (ctrl 1, lalt 2, shift 8, gui 16).
const NAMED = {Enter:"RETURN", Backspace:"BACKSPACE", Tab:"TAB", Escape:"ESCAPE", Delete:"DELETE", Insert:"INSERT",
  Home:"HOME", End:"END", PageUp:"PAGEUP", PageDown:"PAGEDOWN", ArrowUp:"UP", ArrowDown:"DOWN", ArrowLeft:"LEFT",
  ArrowRight:"RIGHT", Shift:"LSHIFT", Control:"LCTRL", Alt:"LALT", Meta:"LGUI", CapsLock:"CAPSLOCK", " ":"SPACE"};
const PUNCT = {"`":"GRAVEACCENT","'":"QUOTE",'"':"QUOTEDBL","=":"EQUALS","-":"MINUS","+":"PLUS","*":"ASTERISK",
  "\\":"BACKSLASH","/":"SLASH",".":"PERIOD",":":"COLON",",":"COMMA",";":"SEMICOLON","&":"AMPERSAND","|":"VERTICALBAR",
  "#":"HASH","@":"AT","^":"CARET","$":"DOLLAR","%":"PERCENT","!":"EXCLAIM","?":"QUESTION","{":"LEFTBRACE","}":"RIGHTBRACE",
  "[":"LEFTBRACKET","]":"RIGHTBRACKET","(":"LEFTPAREN",")":"RIGHTPAREN","<":"LESS",">":"GREATER","_":"UNDERSCORE","~":"TILDE"};
function vkName(k){
  if(NAMED[k]) return NAMED[k];
  if(/^F([1-9]|1[0-2])$/.test(k)) return k;
  if(k.length === 1 && /[A-Za-z0-9]/.test(k)) return k;
  return PUNCT[k] || "NONE";
}
const mods = e => (e.ctrlKey?1:0) | (e.altKey?2:0) | (e.shiftKey?8:0) | (e.metaKey?16:0);
function keyEv(e, down){
  const ch = e.key.length === 1 && e.key.charCodeAt(0) >= 32 && e.key.charCodeAt(0) < 127 && !e.ctrlKey ? e.key : "";
  return {t:"key", name:vkName(e.key), char:ch, down, mods:mods(e)};
}
// Keys the browser keeps: reload, devtools, and Ctrl+V so pasting an image works.
const passThrough = e => e.key === "F5" || e.key === "F12" || (e.ctrlKey && e.key.toLowerCase() === "v");
cv.addEventListener("keydown", e => {
  if(passThrough(e)) return;
  send(keyEv(e, true)); e.preventDefault();
});
cv.addEventListener("keyup", e => { if(!passThrough(e)){ send(keyEv(e, false)); e.preventDefault(); } });

// ---------- images: drop, paste or pick a file -> image viewer on the VGA screen ----------
async function shrink(file){
  // A 640x400 screen gains nothing from full-size photos; downscale before uploading.
  const bmp = await createImageBitmap(file);
  const s = Math.min(1, 1280 / Math.max(bmp.width, bmp.height));
  const c = document.createElement("canvas");
  c.width = Math.round(bmp.width * s); c.height = Math.round(bmp.height * s);
  const g = c.getContext("2d");
  g.fillStyle = "#fff"; g.fillRect(0, 0, c.width, c.height);
  g.drawImage(bmp, 0, 0, c.width, c.height);
  return c.toDataURL(file.type === "image/png" && c.width * c.height < 400000 ? "image/png" : "image/jpeg", 0.9);
}
async function showImage(file){
  if(!file || !file.type.startsWith("image/")) return;
  stxt.textContent = "sending image…";
  try{
    const src = await shrink(file);
    const r = await fetch(`/api/image?key=${encodeURIComponent(KEY)}`, {method:"POST",
      body: JSON.stringify({action:"open", src})});
    if(!r.ok) stxt.textContent = "image failed: " + ((await r.json()).error || r.status);
  }catch(e){ stxt.textContent = "image failed: " + e.message; }
  cv.focus();
}
wrap.addEventListener("dragover", e => { e.preventDefault(); wrap.classList.add("drop"); });
wrap.addEventListener("dragleave", () => wrap.classList.remove("drop"));
wrap.addEventListener("drop", e => {
  e.preventDefault(); wrap.classList.remove("drop");
  showImage([...e.dataTransfer.files].find(f => f.type.startsWith("image/")));
});
document.addEventListener("paste", async e => {
  const item = [...(e.clipboardData?.items || [])].find(i => i.type.startsWith("image/"));
  if(item){ e.preventDefault(); showImage(item.getAsFile()); return; }
  const text = e.clipboardData?.getData("text/plain");
  if(text){
    // Text goes to the desktop clipboard, then Ctrl+Shift+V pastes it into the focused window.
    e.preventDefault();
    await fetch(`/api/clipboard?key=${encodeURIComponent(KEY)}`, {method:"POST",
      body: JSON.stringify({action:"set", text})});
    send({t:"key", name:"v", char:"", down:true, mods:9}); send({t:"key", name:"v", char:"", down:false, mods:9});
  }
});
document.getElementById("copybtn").addEventListener("click", async () => {
  try{
    const r = await fetch(`/api/clipboard?key=${encodeURIComponent(KEY)}`, {method:"POST",
      body: JSON.stringify({action:"get"})});
    const text = (await r.json()).result.text;
    await navigator.clipboard.writeText(text);
    stxt.textContent = `copied ${text.length} characters`;
  }catch(e){ stxt.textContent = "copy failed: " + e.message; }
  cv.focus();
});
const fileInput = document.getElementById("file");
document.getElementById("imgbtn").addEventListener("click", () => fileInput.click());
fileInput.addEventListener("change", () => { showImage(fileInput.files[0]); fileInput.value = ""; });

const COMBOS = {
  menu:   [{name:"LGUI", mods:16}],
  alttab: [{name:"TAB", mods:2}],
  altf4:  [{name:"F4", mods:2}],
  esc:    [{name:"ESCAPE", mods:0}],
};
document.querySelectorAll("[data-combo]").forEach(b => b.addEventListener("click", () => {
  for(const k of COMBOS[b.dataset.combo]){
    send({t:"key", name:k.name, char:"", down:true, mods:k.mods});
    send({t:"key", name:k.name, char:"", down:false, mods:k.mods});
  }
  cv.focus();
}));

// ---------- scaling ----------
const SCALES = ["fit", "1x", "2x"];
let scale = 0;
try{ scale = Math.max(0, SCALES.indexOf(localStorage.getItem("vga32scale") || "fit")); }catch(e){}
function applyScale(){
  const s = SCALES[scale];
  document.getElementById("scale").textContent = "Scale: " + s;
  wrap.style.width = s === "fit" ? `min(100%, ${cv.width * 2}px, calc((100vh - 140px) * ${cv.width / cv.height}))`
                                 : `${cv.width * parseInt(s)}px`;
}
document.getElementById("scale").addEventListener("click", () => {
  scale = (scale + 1) % SCALES.length;
  try{ localStorage.setItem("vga32scale", SCALES[scale]); }catch(e){}
  applyScale();
});
applyScale();
pump();
</script></body></html>
"""
