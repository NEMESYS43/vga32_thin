"""Agent control surface: one tool set, exposed as REST (/api/<tool>) and MCP (/mcp).

Every tool runs its desktop work through Desktop.call(), i.e. on the desktop thread,
so agent actions interleave safely with keyboard, mouse and browser input.
"""
import base64
import io
import json
import os
import time

from PIL import Image

from .apps import Notes, Paint, Terminal
from .browser import Browser
from .client import Key, Mouse
from .keys import VK
from .palette import display_flat
from .panel import Panel, prepare
from .registry import AppRegistry
from .ui import GLYPHS

PROTOCOL_VERSIONS = ["2025-06-18", "2025-03-26", "2024-11-05"]
ALL_SCOPES = ("ui", "input", "terminal", "web", "settings", "admin")

INSTRUCTIONS = """Controls a VGA32 thin client: a 640x400 (typically), 16-color desktop shown on a VGA monitor
attached to an ESP32. Apps run on the host PC: terminal (real shell on the PC), web (full Chromium browser
with JavaScript and video, driven with the web tool), browser (lightweight text-mode browser with images),
viewer (images), files, notes, paint, clock, system, settings, assistant (local AI chat), about, plus
'panel' windows you design yourself, and any apps registered with register_app.

Workflow: call get_state for windows/geometry (and the theme's work area: the classic theme has a
taskbar at the bottom, platinum a menu bar at the top), screenshot to see the screen, then act with the
high-level tools and fall back to type_text / press_key / mouse. Passing window_id to mouse makes x/y
relative to that window's client area. Window geometry (x, y, w, h) is the outer frame incl. title bar.

Make your own app: register_app {id, title, icon, category, kind: "panel", menu: [...], persist: true}
puts it in the Start/system menu (and on the desktop with desktop: true). When the user opens it you
get an app.launched event with its window_id; draw it with panel (window_id) and react to the user
with wait_events (types ["panel.", "menu.select"], app_id). Panels support buttons, text inputs,
checkboxes, a right-click "menu" and "menubar" menus. ask_user shows a dialog and returns the answer.
Every event carries a seq; pass the last seq you saw as since so nothing is missed.
Persistent panel apps reopen with whatever you last drew (app.launched has restored: true).

Building apps: read dev_docs (topic overview first). Apps are Python classes installed with install_app;
drive them with app_call (their cmd_ methods), inspect them live with app_eval, read crashes with
app_logs, and keep data with self.storage / app_data.

Games and anything real-time: don't relay keys through yourself - write a Python app with install_app
(subclass vga32.game.GameApp). It runs on the desktop at its own fps with held/pressed keys and
mouse-look, can use the 3D engines in vga32.render3d, stays installed, and reports errors back so you
can fix and reinstall. There is a GPU 3D engine (vga32.gl3d) and a Quake 1 map loader (vga32.quake); the
built-in 3D Viewer (open_app viewer3d with path, or the view3d tool) walks through .bsp maps and shows
.obj models. Native UI apps subclass vga32.widgets.UIApp. Check them with screenshot /
get_state, and drive their widgets with the ui tool."""


READ_JS = """(max) => {
  const vis = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const t = s => (s || '').replace(/\\s+/g, ' ').trim();
  return {
    title: document.title, url: location.href,
    text: (document.body ? document.body.innerText : '').slice(0, max),
    links: [...document.querySelectorAll('a[href]')].filter(vis).slice(0, 200)
      .map((a, i) => ({text: t(a.innerText || a.title || a.ariaLabel).slice(0, 80), href: a.href})),
    controls: [...document.querySelectorAll('input,textarea,select,button,[role=button],[contenteditable=true]')]
      .filter(vis).slice(0, 100).map(e => ({tag: e.tagName.toLowerCase(), type: e.type || '', id: e.id || '',
        name: e.name || '', text: t(e.innerText || e.value || e.placeholder || e.ariaLabel || e.title).slice(0, 60)})),
    videos: [...document.querySelectorAll('video')].map(v => ({src: v.currentSrc, paused: v.paused,
      time: v.currentTime, duration: v.duration, size: [v.videoWidth, v.videoHeight]})),
    scroll: [scrollX, scrollY, document.documentElement.scrollHeight], viewport: [innerWidth, innerHeight]
  };
}"""


class ToolError(Exception):
    pass


class ImageResult:
    def __init__(self, png, meta):
        self.png, self.meta = png, meta


TOOLS = []


def tool(name, description, properties=None, required=()):
    def deco(fn):
        TOOLS.append({"name": name, "description": description, "fn": fn,
                      "inputSchema": {"type": "object", "properties": properties or {},
                                      "required": list(required), "additionalProperties": False}})
        return fn
    return deco


INT = {"type": "integer"}
NUM = {"type": "number"}
STR = {"type": "string"}
BOOL = {"type": "boolean"}
WID = {"type": "integer", "description": "Target window id (from get_state). Omit to use the topmost "
                                         "window of that app, opening one if needed."}
GEOM = {"x": INT, "y": INT, "w": {"type": "integer", "description": "outer width"},
        "h": {"type": "integer", "description": "outer height incl. title bar"}}
ELEMENTS = {"type": "array", "items": {"type": "object"}, "description":
            "Drawing elements, each {type, ...}. Coordinates are inside the window; omit y to stack "
            "below the previous element. Colors: black darkgray gray navy blue sky teal cyan green lime "
            "maroon red orange yellow purple white, 0-15, or #rrggbb (snapped to the palette). Types: "
            "text{text,x,y,color,size:small|medium|big,bold,align:left|center|right,w}; "
            "rect{x,y,w,h,fill,outline,width,radius}; ellipse{x,y,w,h,fill,outline}; "
            "line{points:[[x,y],...]|x1,y1,x2,y2,color,width}; bar{x,y,w,h,value,max,color,bg,label}; "
            "chart{x,y,w,h,values:[...],kind:line|bars,color,min,max}; list{x,y,items:[...],color}; "
            "table{x,y,rows:[[...]],header:bool,widths:[px]}; button{id,x,y,w,h,label,color,bg}; "
            "image{x,y,w,h,src:url|file path on the PC|data URI|base64,dither:bool,fit:bool}."}

MENU_ITEMS = {"type": "array", "items": {"type": "object"}, "description":
              "Menu items: {id, label, shortcut, enabled, checked}, {separator: true}, or {label, items: [...]} "
              "for a submenu."}

KEY_ALIASES = {"enter": "RETURN", "return": "RETURN", "esc": "ESCAPE", "escape": "ESCAPE", "tab": "TAB",
               "backspace": "BACKSPACE", "delete": "DELETE", "del": "DELETE", "insert": "INSERT",
               "home": "HOME", "end": "END", "pageup": "PAGEUP", "pgup": "PAGEUP", "pagedown": "PAGEDOWN",
               "pgdn": "PAGEDOWN", "up": "UP", "down": "DOWN", "left": "LEFT", "right": "RIGHT",
               "space": "SPACE", "win": "LGUI", "super": "LGUI", "meta": "LGUI", "gui": "LGUI", "menu": "LGUI"}
MOD_BITS = {"ctrl": 1, "control": 1, "alt": 2, "shift": 8, "win": 16, "super": 16, "meta": 16, "gui": 16}
PUNCT = {"`": "GRAVEACCENT", "'": "QUOTE", '"': "QUOTEDBL", "=": "EQUALS", "-": "MINUS", "+": "PLUS",
         "*": "ASTERISK", "\\": "BACKSLASH", "/": "SLASH", ".": "PERIOD", ":": "COLON", ",": "COMMA",
         ";": "SEMICOLON", "&": "AMPERSAND", "|": "VERTICALBAR", "#": "HASH", "@": "AT", "^": "CARET",
         "$": "DOLLAR", "%": "PERCENT", "!": "EXCLAIM", "?": "QUESTION", "{": "LEFTBRACE", "}": "RIGHTBRACE",
         "[": "LEFTBRACKET", "]": "RIGHTBRACKET", "(": "LEFTPAREN", ")": "RIGHTPAREN", "<": "LESS",
         ">": "GREATER", "_": "UNDERSCORE", "~": "TILDE", " ": "SPACE"}
BUTTONS = {"left": 1, "right": 2, "middle": 4}


def _char_key(ch):
    if ch == "\n":
        return Key("RETURN", "", True, 0)
    if ch == "\t":
        return Key("TAB", "", True, 0)
    name = ch if ch.isalnum() and ch.isascii() else PUNCT.get(ch, "NONE")
    return Key(name, ch if 32 <= ord(ch) < 127 else "", True, 0)


def _parse_combo(combo):
    parts = [p for p in combo.replace("-", "+").split("+") if p] if len(combo) > 1 else [combo]
    mods, key = 0, parts[-1]
    for p in parts[:-1]:
        if p.lower() not in MOD_BITS:
            raise ToolError(f"unknown modifier {p!r} in {combo!r}")
        mods |= MOD_BITS[p.lower()]
    name = KEY_ALIASES.get(key.lower())
    if name is None:
        if len(key) == 1:
            name = key if key.isalnum() else PUNCT.get(key, key)
        elif key.upper() in VK:
            name = key.upper()
        elif key in VK:
            name = key
        else:
            raise ToolError(f"unknown key {key!r}")
    if name in ("LGUI",) and not mods:
        mods = 16
    char = key if len(key) == 1 and not mods & 1 else ""
    return name, char, mods


TOOL_SCOPES = {"type_text": "input", "press_key": "input", "mouse": "input", "terminal": "terminal",
               "web": "web", "settings": "settings", "local_ai": "ui", "install_app": "admin",
               "app_source": "admin", "app_eval": "admin"}


class Agent:
    def __init__(self, desk, scopes=ALL_SCOPES, name="host"):
        self.desk = desk
        self.scopes = set(scopes)
        self.name = name

    # ---------- plumbing ----------
    def on(self, fn, timeout=15):
        return self.desk.call(fn, timeout)

    def inject(self, events):
        for e in events:
            self.desk.inbox.put(e)
        self.on(lambda d: None)          # returns once everything queued before it was handled

    def run(self, name, args):
        t = next((t for t in TOOLS if t["name"] == name), None)
        if t is None:
            raise ToolError(f"unknown tool {name!r}")
        allowed = t["inputSchema"]["properties"]
        unknown = set(args) - set(allowed)
        if unknown:
            raise ToolError(f"unknown argument(s) {', '.join(sorted(unknown))} for {name}")
        missing = [r for r in t["inputSchema"]["required"] if r not in args]
        if missing:
            raise ToolError(f"missing required argument(s) {', '.join(missing)} for {name}")
        scope = TOOL_SCOPES.get(name, "ui")
        if scope not in self.scopes:
            raise ToolError(f"this token lacks the {scope!r} scope needed for {name}")
        try:
            return t["fn"](self, **args)
        except (ToolError, KeyError, ValueError, TypeError) as e:
            raise ToolError(str(e).strip("'\"")) from e
        except TimeoutError as e:
            raise ToolError("desktop did not respond in time") from e

    @tool("batch", "Run several agent tools in one request. Each action is {\"tool\": name, \"args\": {...}}.\n"
          "Actions run in order against the desktop. Use this to combine clippy moves, app commands, or window\n"
          "actions without one HTTP round-trip per step.",
          {"actions": {"type": "array", "items": {"type": "object",
                         "properties": {"tool": STR, "args": {"type": "object"}},
                         "required": ["tool"]}}}, ["actions"])
    def batch(self, actions):
        if not isinstance(actions, list):
            raise ToolError("actions must be a list")
        if not actions:
            raise ToolError("actions must not be empty")
        if len(actions) > 24:
            raise ToolError("at most 24 actions per batch")
        results = []
        for index, item in enumerate(actions):
            if not isinstance(item, dict):
                raise ToolError(f"action {index} is not an object")
            name = item.get("tool")
            args = item.get("args", {})
            if not isinstance(name, str) or not name:
                raise ToolError(f"action {index} has no tool name")
            if args is None:
                args = {}
            if not isinstance(args, dict):
                raise ToolError(f"action {index} args must be an object")
            if name == "batch":
                raise ToolError("batch actions may not contain another batch")
            try:
                results.append({"index": index, "tool": name, "ok": True,
                                "result": self.run(name, args)})
            except ToolError as e:
                results.append({"index": index, "tool": name, "ok": False,
                                "error": str(e)})
                break
        return results

    def _wait(self, check, timeout, interval=0.15):
        end = time.monotonic() + timeout
        while True:
            r = self.on(check)
            if r is not None or time.monotonic() >= end:
                return r
            time.sleep(interval)

    # ---------- state & screen ----------
    @tool("get_state", "Desktop state: screen size, board connection, mouse, open windows (topmost first) "
          "with ids, geometry, client area and app-specific details, and the list of launchable apps.")
    def get_state(self):
        return self.on(lambda d: d.state())

    @tool("list_boards", "List discovered/remembered ESP32 VGA devices and the active board. "
          "Each record includes host, name, resolution, connection state and whether it is active.")
    def list_boards(self):
        return self.on(lambda d: d.list_boards())

    @tool("discover_boards", "Scan the LAN for ESP32 VGA beacons (UDP 7778) and remember every board found.",
          {"timeout": {"type": "number", "description": "seconds to listen, default 2"}})
    def discover_boards(self, timeout=2.0):
        return self.on(lambda d: d.discover_boards(timeout=timeout))

    @tool("connect_board", "Connect to (or switch the active desktop display to) an ESP32 VGA device "
          "given its host or host:port. Replaces the currently attached board.",
          {"host": STR, "key": STR, "port": INT, "timeout": NUM}, ["host"])
    def connect_board(self, host, key=None, port=None, timeout=5.0):
        return self.on(lambda d: d.connect_board(host=host, key=key, port=port, timeout=timeout))

    @tool("disconnect_board", "Detach the active ESP32 VGA board (the desktop keeps running into NullDevice).")
    def disconnect_board(self):
        return self.on(lambda d: d.disconnect_board())

    @tool("forget_board", "Remove a remembered board from multi-device management. Does not disconnect the active board.",
          {"host": STR}, ["host"])
    def forget_board(self, host):
        return self.on(lambda d: d.forget_board(host))

    @tool("screenshot", "PNG of exactly what the VGA monitor shows (the board's cursor is not included). "

          "Optionally crop to one window and upscale for easier reading.",
          {"window_id": INT, "scale": {"type": "integer", "minimum": 1, "maximum": 3,
                                       "description": "pixel upscale factor, default 1"}})
    def screenshot(self, window_id=None, scale=1):
        def grab(d):
            d.frame()
            idx = d.screen.sent.copy()
            box = None
            if window_id is not None:
                w = d.find(window_id)
                box = (max(0, w.x), max(0, w.y), min(d.W, w.x + w.w), min(d.H, w.y + w.h))
            return idx, box, d.W, d.H
        idx, box, W, H = self.on(grab)
        idx[idx == 255] = 0
        img = Image.fromarray(idx, "P")
        img.putpalette(display_flat())
        if box:
            img = img.crop(box)
        scale = max(1, min(3, int(scale)))
        if scale > 1:
            img = img.resize((img.width * scale, img.height * scale), Image.NEAREST)
        buf = io.BytesIO()
        img.save(buf, "PNG", optimize=True)
        meta = {"width": img.width, "height": img.height, "scale": scale, "screen": [W, H],
                "origin": list(box[:2]) if box else [0, 0]}
        return ImageResult(buf.getvalue(), meta)

    # ---------- windows ----------
    @tool("open_app", "Open an app by id and return its window. Built-ins: terminal, web (full browser), "
          "browser (text browser), viewer (images), files, notes, paint, clock, system, settings, assistant, "
          "about, panel; plus any registered app (see list_apps). Optional geometry, plus app options: url "
          "(browser/web page, image URL/path for viewer), path (folder for files, file for notes), text (notes: "
          "replaces content; assistant: first message), command (terminal, run immediately), page (settings: "
          "appearance|desktop|apps|agents), title/background/elements (panel).",
          {"app": STR, **GEOM, "url": STR, "path": STR, "text": STR, "command": STR, "page": STR, "title": STR,
           "background": STR, "elements": ELEMENTS}, ["app"])
    def open_app(self, app, x=None, y=None, w=None, h=None, title=None, background=None, elements=None, **kw):
        if app.lower() == "panel":
            kw["spec"] = {"title": title or "Panel", "background": background or "white",
                          "elements": prepare(elements or [])}
            kw["title"] = title
        return self.on(lambda d: d.launch(app, x, y, w, h, **kw).info(True))

    @tool("window", "Manage a window. action: focus, close, move (x,y), resize (w,h), set_geometry (x,y,w,h), "
          "maximize, restore, shade / unshade (roll up to the title bar), minimize / unminimize (to the "
          "taskbar or Dock), on_top / not_on_top (always on top).",
          {"window_id": INT, "action": {"type": "string", "enum": ["focus", "close", "move", "resize",
                                                                    "set_geometry", "maximize", "restore", "shade",
                                                                    "unshade", "minimize", "unminimize", "on_top",
                                                                    "not_on_top"]},
           **GEOM}, ["window_id", "action"])
    def window(self, window_id, action, x=None, y=None, w=None, h=None):
        def do(d):
            win = d.find(window_id)
            if action == "close":
                d.close(win)
                return {"closed": window_id}
            if action == "focus":
                d.focus(win)
            elif action in ("move", "resize", "set_geometry"):
                win.saved = None
                d.set_geometry(win, win.x if x is None else x, win.y if y is None else y,
                               win.w if w is None else w, win.h if h is None else h)
                d.focus(win)
            elif action == "maximize":
                d.maximize(win, True)
                d.focus(win)
            elif action == "restore":
                d.maximize(win, False)
            elif action in ("shade", "unshade"):
                d.shade(win, action == "shade")
            elif action in ("minimize", "unminimize"):
                d.minimize(win, action == "minimize")
            elif action in ("on_top", "not_on_top"):
                win.on_top = action == "on_top"
            return win.info(win is d.windows[-1])
        return self.on(do)

    @tool("layout", "Arrange windows. name: cascade, tile (side-by-side columns), rows, grid, main-left "
          "(first window large on the left, rest stacked right), main-top, maximize. window_ids picks and "
          "orders the windows (default: all, bottom to top). gap adds spacing in px.",
          {"name": {"type": "string", "enum": ["cascade", "tile", "rows", "grid", "main-left", "main-top",
                                               "maximize"]},
           "window_ids": {"type": "array", "items": INT}, "gap": INT}, ["name"])
    def layout(self, name, window_ids=None, gap=0):
        return self.on(lambda d: (d.layout(name, window_ids, gap), d.state()["windows"])[1])

    # ---------- raw input ----------
    @tool("type_text", "Type text into the focused window (or focus window_id first). \\n presses Enter.",
          {"text": STR, "window_id": INT}, ["text"])
    def type_text(self, text, window_id=None):
        if window_id is not None:
            self.on(lambda d: d.focus(d.find(window_id)))
        evs = []
        for ch in text:
            k = _char_key(ch)
            evs += [k, Key(k.name, k.char, False, 0)]
        self.inject(evs)
        return {"typed": len(text)}

    @tool("press_key", "Press key combos in order, e.g. [\"ctrl+s\"], [\"alt+tab\"], [\"enter\"], [\"win\"] "
          "(opens the app menu), [\"ctrl+l\", \"pageDown\"]. Names: enter esc tab backspace delete home end "
          "pageup pagedown up down left right space f1-f12 or a single character.",
          {"keys": {"type": "array", "items": STR}, "window_id": INT}, ["keys"])
    def press_key(self, keys, window_id=None):
        if isinstance(keys, str):
            keys = [keys]
        if window_id is not None:
            self.on(lambda d: d.focus(d.find(window_id)))
        evs = []
        for combo in keys:
            name, char, mods = _parse_combo(combo)
            evs += [Key(name, char, True, mods), Key(name, char, False, mods)]
        self.inject(evs)
        return {"pressed": keys}

    @tool("mouse", "Mouse action at (x, y): click, double_click, right_click, move, down, up, drag (to x2,y2), "
          "scroll (amount: + up / - down). With window_id, coordinates are relative to that window's client "
          "area (and the window is not raised unless you click it). look: relative motion by x, y pixels, "
          "as when an app has captured the mouse (mouse-look in 3D games).",
          {"action": {"type": "string", "enum": ["click", "double_click", "right_click", "move", "down", "up",
                                                 "drag", "scroll", "look"]},
           "x": INT, "y": INT, "x2": INT, "y2": INT, "button": {"type": "string", "enum": ["left", "right", "middle"]},
           "amount": INT, "window_id": INT}, ["action", "x", "y"])
    def mouse(self, action, x, y, x2=None, y2=None, button="left", amount=-3, window_id=None):
        if action == "look":
            self.inject([Mouse(int(x), int(y), 0, 0, web=True, rel=True)])
            return {"done": "look", "by": [int(x), int(y)], "grabbed": self.on(lambda d: d.grab is not None)}
        ox, oy, W, H = self.on(lambda d: ((d.find(window_id).client()[:2] if window_id is not None else (0, 0))
                                          + (d.W, d.H)))
        clamp = lambda px, py: (max(0, min(W - 1, ox + int(px))), max(0, min(H - 1, oy + int(py))))
        px, py = clamp(x, y)
        b = 2 if action == "right_click" else BUTTONS.get(button, 1)
        M = lambda X, Y, bt=0, wh=0: Mouse(X, Y, bt, wh, web=True)
        if action in ("click", "right_click"):
            evs = [M(px, py), M(px, py, b), M(px, py)]
        elif action == "double_click":
            evs = [M(px, py), M(px, py, b), M(px, py), M(px, py, b), M(px, py)]
        elif action == "move":
            evs = [M(px, py)]
        elif action == "down":
            evs = [M(px, py, b)]
        elif action == "up":
            evs = [M(px, py)]
        elif action == "drag":
            if x2 is None or y2 is None:
                raise ToolError("drag needs x2 and y2")
            qx, qy = clamp(x2, y2)
            steps = max(2, int(max(abs(qx - px), abs(qy - py)) / 12))
            evs = [M(px, py), M(px, py, b)]
            evs += [M(px + (qx - px) * i // steps, py + (qy - py) * i // steps, b) for i in range(1, steps + 1)]
            evs += [M(qx, qy)]
        elif action == "scroll":
            n = int(amount) or -3
            evs = [M(px, py)] + [M(px, py, 0, 1 if n > 0 else -1) for _ in range(abs(n))]
        else:
            raise ToolError(f"unknown mouse action {action!r}")
        self.inject(evs)
        return {"done": action, "at": [px, py]}

    # ---------- apps ----------
    @tool("terminal", "Use a terminal window (a real shell on the host PC). action run: run command and wait "
          "up to timeout seconds, returning its output and exit code (cd and cls work); read: last `lines` "
          "lines; kill: stop the running command. new_window opens a separate terminal.",
          {"action": {"type": "string", "enum": ["run", "read", "kill"]}, "command": STR, "timeout": NUM,
           "lines": INT, "window_id": WID, "new_window": BOOL}, ["action"])
    def terminal(self, action, command=None, timeout=60, lines=40, window_id=None, new_window=False):
        def target(d):
            if new_window:
                return d.open(Terminal)
            return d.find_app(Terminal, window_id)
        if action == "read":
            return self.on(lambda d: (lambda win: {"window_id": win.id, **win.app.state(),
                                                   "output": "\n".join(win.app.lines[-int(lines):])})(target(d)))
        if action == "kill":
            return self.on(lambda d: (lambda win: (win.app.kill(), {"window_id": win.id, "killed": True})[1])(
                target(d)))
        if not command:
            raise ToolError("run needs a command")

        def start(d):
            win = target(d)
            if win.app.busy:
                raise ToolError(f"terminal {win.id} is still running a command; kill it or use new_window")
            n = win.app.line_count
            win.app.run(command)
            d.focus(win)
            return win, n
        win, n = self.on(start)
        done = self._wait(lambda d: True if not win.app.busy else None, float(timeout))

        def result(d):
            out = win.app.lines_since(n)[1:]
            text = "\n".join(out)
            return {"window_id": win.id, "exit_code": win.app.last_exit, "cwd": win.app.cwd,
                    "timed_out": not done, "output": text[-20000:], "truncated": len(text) > 20000}
        return self.on(result)

    @tool("notes", "Read or change the Notes app text (saved to ~/vga32_notes.txt). action: read, write "
          "(replace), append.",
          {"action": {"type": "string", "enum": ["read", "write", "append"]}, "text": STR, "window_id": WID},
          ["action"])
    def notes(self, action, text=None, window_id=None):
        def do(d):
            win = d.find_app(Notes, window_id)
            if action in ("write", "append"):
                if text is None:
                    raise ToolError(f"{action} needs text")
                win.app.set_text(text, append=action == "append")
                d.focus(win)
            return {"window_id": win.id, "text": win.app.text}
        return self.on(do)

    @tool("browser", "Drive the text-mode web browser. actions: navigate (url, or plain words to search), back, "
          "forward, reload, read (whole page text + numbered links), read_visible (only what's on screen), "
          "click_link (index from read), scroll (lines, + down / - up), find (text; scrolls to it). Navigation "
          "waits for the page to load and returns its content.",
          {"action": {"type": "string", "enum": ["navigate", "back", "forward", "reload", "read", "read_visible",
                                                 "click_link", "scroll", "find"]},
           "url": STR, "index": INT, "lines": INT, "text": STR, "window_id": WID, "new_window": BOOL,
           "max_chars": INT, "include_links": BOOL}, ["action"])
    def browser(self, action, url=None, index=None, lines=10, text=None, window_id=None, new_window=False,
                max_chars=6000, include_links=True):
        def do(d):
            win = d.open(Browser) if new_window else d.find_app(Browser, window_id)
            b = win.app
            if action == "navigate":
                if not url:
                    raise ToolError("navigate needs url")
                b.navigate(url)
            elif action == "back":
                b.back()
            elif action == "forward":
                b.forward()
            elif action == "reload":
                b.reload()
            elif action == "click_link":
                if index is None or not 0 <= int(index) < len(b.page.links):
                    raise ToolError(f"index must be 0..{len(b.page.links) - 1}")
                b.follow(int(index))
            elif action == "scroll":
                b.scroll_by(int(lines))
            elif action == "find":
                if not text:
                    raise ToolError("find needs text")
                b.find(text)
            if action in ("navigate", "back", "forward", "reload", "click_link", "scroll", "find"):
                d.focus(win)
            return win
        win = self.on(do)
        if win.app.loading:
            self._wait(lambda d: True if not win.app.loading else None, 25)
        visible = action in ("read_visible", "scroll", "find")
        return self.on(lambda d: {"window_id": win.id,
                                  **win.app.read(int(max_chars), include_links and not visible, visible)})

    @tool("panel", "Create or update a panel: a window whose content you define with drawing elements, for "
          "dashboards, status boards, forms and mini-apps. Omit window_id to create one; pass the window_id of "
          "an app.launched event to draw your registered app. mode replace (default) or append elements. "
          "Buttons, inputs and checkboxes send panel.click / panel.submit / panel.change events (wait_events). "
          "menu: right-click items [{id,label,...}]; menubar: [{title, items:[...]}] for the menu bar. "
          "Picks arrive as menu.select.",
          {"window_id": INT, "title": STR, "background": STR, "elements": ELEMENTS,
           "mode": {"type": "string", "enum": ["replace", "append"]}, "menu": MENU_ITEMS,
           "menubar": {"type": "array", "items": {"type": "object"}}, **GEOM})
    def panel(self, window_id=None, title=None, background=None, elements=None, mode="replace",
              x=None, y=None, w=None, h=None, menu=None, menubar=None):
        given = elements is not None
        elements = prepare(elements or [])
        if window_id is None:
            r = self.open_app("panel", x, y, w, h, title=title, background=background, elements=elements)
            if menu is not None or menubar is not None:
                return self.panel(r["id"], menu=menu, menubar=menubar)
            return r

        def do(d):
            win = d.find_app(Panel, window_id)
            spec = dict(win.app.spec)
            if menu is not None:
                spec["menu"] = menu
            if menubar is not None:
                spec["menubar"] = menubar
            if background is not None:
                spec["background"] = background
            if given:
                spec["elements"] = (spec.get("elements", []) + elements) if mode == "append" else elements
            win.app.set_spec(spec, title)
            if any(v is not None for v in (x, y, w, h)):
                d.set_geometry(win, win.x if x is None else x, win.y if y is None else y,
                               win.w if w is None else w, win.h if h is None else h)
            e = d.registry.entries.get(win.app.app_id or "")
            if e and e.get("persist") and e.get("kind") == "panel":
                d.registry.save_content(e["id"], dict(spec, title=win.app.title))   # reopens like this
            return win.info(win is d.windows[-1])
        return self.on(do)

    @tool("panel_events", "Button clicks and key presses received by a panel since the last call. wait_seconds "
          "blocks until at least one event arrives (max 120).",
          {"window_id": INT, "clear": BOOL, "wait_seconds": NUM}, ["window_id"])
    def panel_events(self, window_id, clear=True, wait_seconds=0):
        def check(d):
            win = d.find_app(Panel, window_id, create=False)
            return win.app.take_events(clear) or None
        events = self._wait(check, min(120.0, float(wait_seconds)), 0.2) or []
        return {"window_id": window_id, "events": events}

    @tool("web", "Drive the full web browser (real Chromium: JavaScript, CSS, video), shown on the VGA screen. "
          "actions: navigate (url or search words; waits for DOMContentLoaded), back, forward, reload, "
          "read (title, url, visible text, links, form controls, videos, scroll position), "
          "eval (run JavaScript in the page; use 'return' for a value; e.g. control a video with "
          "document.querySelector('video').play()), click (CSS selector, or x/y in page pixels), "
          "type (text; into selector if given, else the focused element), press (Playwright key such as "
          "Enter, Control+A, ArrowDown), scroll (dy pixels), wait_for (selector to appear), "
          "screenshot (full-color PNG of the page, sharper than the 16-color screen), "
          "dither (mode: ordered = stable for video, floyd = best for stills, none = flat colors).",
          {"action": {"type": "string", "enum": ["navigate", "back", "forward", "reload", "read", "eval", "click",
                                                 "type", "press", "scroll", "wait_for", "screenshot", "dither"]},
           "url": STR, "script": STR, "selector": STR, "text": STR, "key": STR, "x": INT, "y": INT, "dy": INT,
           "mode": {"type": "string", "enum": ["ordered", "floyd", "none"]}, "timeout": NUM, "max_chars": INT,
           "window_id": WID, "new_window": BOOL}, ["action"])
    def web(self, action, url=None, script=None, selector=None, text=None, key=None, x=None, y=None, dy=None,
            mode=None, timeout=20, max_chars=6000, window_id=None, new_window=False):
        from .webview import HOME, WebView, to_url

        win = self.on(lambda d: d.open(WebView) if new_window else d.find_app(WebView, window_id))
        app = win.app
        if app.page is None:
            self._wait(lambda d: True if app.page is not None or app.error or app.engine.error else None, 60)
        if app.page is None:
            raise ToolError(app.error or app.engine.error or "the browser engine did not start")
        ms = int(float(timeout) * 1000)

        def page_info(p):
            return {"window_id": win.id, "url": p.url, "title": p.title()}

        def do():
            p = app.page
            if action == "navigate":
                if not url:
                    raise ToolError("navigate needs url")
                target = to_url(url)
                if target == "about:home":
                    p.set_content(HOME)
                else:
                    try:
                        p.goto(target, wait_until="domcontentloaded", timeout=ms)
                    except Exception as e:
                        if "Timeout" not in type(e).__name__:
                            raise ToolError(str(e).splitlines()[0])
                app._after_nav()
                return page_info(p)
            if action in ("back", "forward", "reload"):
                {"back": p.go_back, "forward": p.go_forward, "reload": p.reload}[action](
                    wait_until="domcontentloaded", timeout=ms)
                app._after_nav()
                return page_info(p)
            if action == "read":
                return {"window_id": win.id, **p.evaluate(READ_JS, int(max_chars))}
            if action == "eval":
                if not script:
                    raise ToolError("eval needs script")
                src = script.strip()
                if "return" in src and not src.startswith(("(", "async", "function")):
                    src = f"async () => {{ {src} }}"
                result = p.evaluate(src)
                s = json.dumps(result, default=str)
                return {"window_id": win.id, "result": json.loads(s) if len(s) < 20000 else s[:20000] + "..."}
            if action == "click":
                if selector:
                    p.click(selector, timeout=ms)
                elif x is not None and y is not None:
                    p.mouse.click(int(x), int(y))
                else:
                    raise ToolError("click needs selector or x and y")
                p.wait_for_timeout(150)
                return page_info(p)
            if action == "type":
                if text is None:
                    raise ToolError("type needs text")
                if selector:
                    p.fill(selector, text, timeout=ms)
                else:
                    p.keyboard.type(text)
                return page_info(p)
            if action == "press":
                if not key:
                    raise ToolError("press needs key")
                p.keyboard.press(key)
                p.wait_for_timeout(100)
                return page_info(p)
            if action == "scroll":
                p.mouse.wheel(0, int(dy if dy is not None else 400))
                p.wait_for_timeout(150)
                return {"window_id": win.id, "scroll": p.evaluate("[scrollX, scrollY, document.documentElement.scrollHeight]")}
            if action == "wait_for":
                if not selector:
                    raise ToolError("wait_for needs selector")
                p.wait_for_selector(selector, timeout=ms)
                return page_info(p)
            if action == "screenshot":
                return ImageResult(p.screenshot(type="png"), {"window_id": win.id, "url": p.url,
                                                               "viewport": list(app.viewport)})
            if action == "dither":
                app.set_dither(mode or "ordered")
                return {"window_id": win.id, "dither": app.dither}
            raise ToolError(f"unknown action {action}")

        try:
            result = app.engine.call(do, timeout=float(timeout) + 15)
        except ToolError:
            raise
        except Exception as e:
            raise ToolError(f"{type(e).__name__}: {str(e).splitlines()[0] if str(e) else ''}") from e
        if action not in ("read", "eval", "screenshot"):
            self.on(lambda d: d.focus(win))
        return result

    @tool("image","Show and control images in the image viewer. action open: src is an http(s) URL, a file "
          "path on the host PC, a folder (opens its first image) or a data:/base64 image; waits for it to load. "
          "next / prev step through the image's folder. Optional dither (true = Floyd-Steinberg, false = flat "
          "nearest colors) and fit (true = fit window, false = 1:1).",
          {"action": {"type": "string", "enum": ["open", "next", "prev", "set"]}, "src": STR, "window_id": WID,
           "new_window": BOOL, "dither": BOOL, "fit": BOOL, **GEOM}, ["action"])
    def image(self, action, src=None, window_id=None, new_window=False, dither=None, fit=None,
              x=None, y=None, w=None, h=None):
        from .viewer import Viewer

        def do(d):
            win = d.open(Viewer, x, y, w, h) if new_window else d.find_app(Viewer, window_id, x=x, y=y, w=w, h=h)
            v = win.app
            if action == "open":
                if not src:
                    raise ToolError("open needs src")
                v.open(src)
            elif action in ("next", "prev"):
                if not v.folder:
                    raise ToolError("the current image isn't from a folder on the host")
                v.step(1 if action == "next" else -1)
            if dither is not None:
                v.dither, v.cache = bool(dither), None
            if fit is not None:
                v.actual, v.cache, v.pan = not fit, None, [0, 0]
            d.focus(win)
            return win
        win = self.on(do)
        if win.app.loading:
            self._wait(lambda d: True if not win.app.loading else None, 30)
        return self.on(lambda d: win.info(win is d.windows[-1]))

    @tool("paint","Draw on a Paint window's canvas with the same elements as panel (text, shapes, charts, "
          "images...). clear wipes it first (optionally to a background color).",
          {"window_id": WID, "elements": ELEMENTS, "clear": BOOL, "background": STR})
    def paint(self, window_id=None, elements=None, clear=False, background=None):
        elements = prepare(elements or [])

        def do(d):
            win = d.find_app(Paint, window_id)
            if clear:
                win.app.clear(background)
            win.app.draw_elements(elements)
            d.focus(win)
            return win.info(True)
        return self.on(do)

    # ---------- desktop ----------
    @tool("notify", "Show a toast message in the top-right corner of the screen.",
          {"text": STR, "seconds": NUM}, ["text"])
    def notify(self, text, seconds=4):
        self.on(lambda d: d.notify(text, seconds))
        return {"shown": text}

    @tool("clippy_say", "Make the overlay Clippy character speak and change expression. Supports movement via x/y or region "
          "(top-left, top-right, bottom-left, bottom-right, center, assistant, cursor). "
          "Mood may be happy, neutral, thinking, sad, worried, alert, wink, excited.",
          {"text": STR, "mood": STR, "x": INT, "y": INT, "region": STR, "visible": BOOL,
           "animate": BOOL, "duration": NUM})
    def clippy_say(self, text=None, mood=None, x=None, y=None, region=None, visible=None,
                   animate=True, duration=0.55):
        def do(d):
            d.clippy.update(text=text, mood=mood, x=x, y=y, region=region, visible=visible,
                            animate=animate, duration=duration)
            return {"ok": True, "text": d.clippy.text, "mood": d.clippy.mood,
                    "visible": d.clippy.visible, "x": d.clippy.sprite_x, "y": d.clippy.sprite_y,
                    "nearest_region": d.clippy.state().get("nearest_region"),
                    "regions": list(d.clippy.state()["regions"].keys())}
        return self.on(do)

    @tool("clippy_move", "Move Clippy on the screen/map. target is a named map region (top-left, top-right, "
          "bottom-left, bottom-right, center, assistant, cursor) or provide x/y.",
          {"target": STR, "x": INT, "y": INT, "duration": NUM})
    def clippy_move(self, target=None, x=None, y=None, duration=0.55):
        def do(d):
            d.clippy.update(region=target if target else None, x=x, y=y, animate=True, duration=duration)
            return {"ok": True, "mood": d.clippy.mood, "x": d.clippy.sprite_x, "y": d.clippy.sprite_y,
                    "nearest_region": d.clippy.state().get("nearest_region")}
        return self.on(do)

    @tool("clippy_state", "Read Clippy's current text, mood, position, movement, and available map regions.",
          {})
    def clippy_state(self):
        def do(d):
            return d.clippy.state()
        return self.on(do)

    @tool("desktop_style", "Change the look: theme (classic = taskbar + Start menu, platinum = Mac OS 9-style "
          "menu bar and pinstriped windows, aqua = Mac OS X-style menu bar, traffic-light windows and a Dock), "
          "background color, pattern, caption, wallpaper (image path or URL, "
          "'' for none), wallpaper_mode (fill|fit|center|tile) and desktop icons on/off. Persists.",
          {"theme": {"type": "string", "enum": ["classic", "platinum", "aqua"]}, "background": STR, "pattern": BOOL,
           "caption": STR, "wallpaper": STR, "wallpaper_mode": {"type": "string", "enum": ["fill", "fit", "center",
                                                                                            "tile"]},
           "icons": BOOL})
    def desktop_style(self, theme=None, background=None, pattern=None, caption=None, wallpaper=None,
                      wallpaper_mode=None, icons=None):
        def do(d):
            if theme is not None:
                d.set_theme(theme)
            dd = {k: v for k, v in (("background", background), ("pattern", pattern), ("caption", caption),
                                    ("wallpaper", wallpaper), ("wallpaper_mode", wallpaper_mode),
                                    ("icons", icons)) if v is not None}
            if dd:
                d.apply_settings({"desktop": dd})
            return d.style
        return self.on(do)

    # ---------- apps & events ----------
    @tool("list_apps", "Every launchable app (built-in, plugin and registered): id, title, icon, category, kind, "
          "owner, whether it's on the desktop. Hidden ones (panel, dialog) are included with hidden: true.")
    def list_apps(self):
        return self.on(lambda d: [AppRegistry.public(e) for e in d.registry.entries.values()])

    @tool("register_app", "Declare an app so it shows in the Start/system menu (and on the desktop with "
          "desktop: true). kind panel (default): opening it shows `spec` and sends you app.launched {app_id, "
          "window_id}; draw it with panel and listen with wait_events. kind web/browser opens url; kind command "
          "runs command in a terminal. Fields: id (required, e.g. 'hermes.weather'), title, icon (" +
          ", ".join(GLYPHS) + "), category (menu folder, default "
          "Agents), size [w,h] (client area), single_instance, desktop, spec {background, elements, menu, "
          "menubar}, menu (right-click items), url, command, webhook (URL that gets this app's events), persist "
          "(keep across restarts in ~/.vga32/apps). Re-registering the same id updates it.",
          {"id": STR, "title": STR, "icon": STR, "category": STR,
           "kind": {"type": "string", "enum": ["panel", "web", "browser", "command"]},
           "size": {"type": "array", "items": INT}, "single_instance": BOOL, "desktop": BOOL,
           "spec": {"type": "object"}, "menu": MENU_ITEMS, "url": STR, "command": STR, "webhook": STR,
           "persist": BOOL, "description": STR, "open": {"type": "boolean", "description": "launch it now too"}},
          ["id"])
    def register_app(self, open=False, **manifest):
        manifest.setdefault("owner", self.name)

        def do(d):
            r = d.registry.register(manifest)
            d.dirty = True
            d.bus.publish("app.registered", app_id=r["id"], owner=r.get("owner"))
            if open:
                r["window"] = d.launch(r["id"]).info(True)
            r["event_seq"] = d.bus.last_seq()
            return r
        return self.on(do)

    @tool("install_app", "Install (or update) a Python app that runs ON the desktop - for games, animations and "
          "anything that needs real-time keyboard/mouse. It is saved to ~/.vga32/apps/<name>.py, loaded at once "
          "and stays installed across restarts. Subclass vga32.game.GameApp for games: set app_id, title, icon, "
          "category, width/height (logical canvas, scaled to the window), fps; write start(), update(dt) using "
          "self.held('left') / self.pressed('space') / self.released(k) / self.pointer / self.clicked(), and "
          "render(g) with g.clear, g.rect(x,y,w,h,color), g.frame, g.circle(cx,cy,r,color), g.line, g.poly, "
          "g.pixel, g.text(x,y,s,color,size=small|medium|big,center=False), g.sprite(['.rr.','rrrr'],x,y,"
          "colors={'r':'red'},scale=2,flip=False); put score etc. in game_state(). Colors are the 16 palette "
          "names. For 3D: set mouse_look = True (click captures the mouse; self.mouse_delta() each update) and "
          "use vga32.render3d - Scene/Mesh/Camera for polygon worlds (Mesh.cube/box/grid/sphere/cylinder/"
          "pyramid/load_obj, scene.render(g, cam) then g.dither()) or Raycaster(map_rows, colors) for "
          "corridors (world.move(cam, fwd, strafe), world.render(g, cam, sprites)). For forms and tools "
          "subclass vga32.widgets.UIApp and return a widget tree from build(): Column, Row, Group, Label, "
          "Button, Checkbox, Radio, Slider, TextField, ListBox, Dropdown, ProgressBar, CanvasView. "
          "For big, textured scenes use the GPU engine vga32.gl3d (Renderer, MeshData, Material: textures, "
          "lightmaps, lights, fog, skybox; r.render(cam) returns an image for g.blit) and vga32.quake to load "
          "Quake 1 maps (quake.load(path).add_to(renderer), level.spawn_camera(), level.player(cam) for "
          "collision). Set pixel = 2 to make the canvas follow the window size. "
          "For editors and tools mixing UI and 3D: vga32.widgets.Viewport3D (a live 3D view widget with "
          "orbit / pan / zoom and on_pick / on_drag), vga32.mesh.EditableMesh (extrude, inset, subdivide, "
          "smooth, undo, OBJ) and vga32.formats.load_model (glTF / GLB / OBJ). Give the app cmd_<name> methods "
          "so it can be driven with app_call. Full reference: dev_docs. "
          "Any other vga32.ui.App subclass with an app_id works too. Errors (syntax, load, or the first "
          "frames when open is true) come back in the result and as app.error events, so fix and reinstall. "
          "open (default true) restarts running copies and opens it. Needs the admin scope.",
          {"name": {"type": "string", "description": "file name, a Python identifier, e.g. snake_game"},
           "source": STR, "open": BOOL, "desktop": {"type": "boolean", "description": "add a desktop icon"},
           "assets": {"type": "object", "description": "files to put next to the app in ~/.vga32/apps/<name>/: "
                      "{\"textures/wall.png\": base64 | \"text:...\" | \"https://...\"}. In the app: "
                      "from vga32.registry import assets_dir; ASSETS = assets_dir(__file__)"}},
          ["name", "source"])
    def install_app(self, name, source, open=True, desktop=False, assets=None):
        if assets:      # downloads happen here, not on the desktop thread
            import re
            from .settings import APPS_DIR
            if not re.match(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$", name):
                raise ToolError("name must be a Python identifier (letters, digits, _), e.g. snake_game")
            try:
                self.desk.registry._write_assets(os.path.join(APPS_DIR, name), assets)
            except Exception as e:
                return {"installed": [], "error": f"assets: {type(e).__name__}: {e}"}

        def do(d):
            try:
                ids, err = d.registry.install(name, source)
            except SyntaxError as e:
                return {"installed": [], "error": f"SyntaxError line {e.lineno}: {e.msg}\n{(e.text or '').rstrip()}"}
            out = {"installed": ids, "error": err}
            if not ids:
                out["error"] = err or "no App subclass with an app_id found in the source"
                return out
            for app_id in ids:
                if desktop:
                    d.pin_icon(app_id, True)
                d.bus.publish("app.registered", app_id=app_id, owner=self.name)
            if open:
                old = [w for w in d.windows if w.app.app_id in ids]
                for w in old:
                    d.close(w)
                win = d.launch(ids[0])
                out["window_id"] = win.id
            d.dirty = True
            return out
        out = self.on(do)
        if open and out.get("window_id"):
            time.sleep(0.6)                      # let it run some frames, then report how it's doing

            def check(d):
                win = d.find(out["window_id"])
                d.focus(win)
                d.render()                       # draws now, so render() errors surface too
                return win.info(True)
            try:
                info = self.on(check)
                out["window"] = info
                if info["details"].get("error"):
                    out["error"] = info["details"]["error"]
            except KeyError:
                out["error"] = out.get("error") or "the window closed right after opening"
        return out

    @tool("ui", "Operate the native widgets of a window (UIApp windows and panels): get lists widgets with id, "
          "type, value, label, options; set changes a widget's value (as if the user did it, so the app reacts); "
          "click presses a button; focus moves keyboard focus.",
          {"window_id": INT, "action": {"type": "string", "enum": ["get", "set", "click", "focus"]}, "id": STR,
           "value": {}}, ["window_id", "action"])
    def ui(self, window_id, action, id=None, value=None):
        def do(d):
            win = d.find(window_id)
            app = win.app
            if not hasattr(app, "widget_list"):
                raise ToolError(f"window {window_id} has no widgets")
            if action != "get":
                w = app.find(id)
                if w is None:
                    raise ToolError(f"no widget {id!r}; ids: {[x.id for x in app.widget_list() if x.id]}")
                if action == "set":
                    w.set_value(value)
                    w.changed()
                elif action == "click":
                    if not hasattr(w, "click"):
                        raise ToolError(f"{id!r} is not a button")
                    w.click()
                elif action == "focus":
                    app.focus_widget = w
            items = [{"id": w.id, "type": type(w).__name__.lower(), "enabled": w.enabled,
                      **({"value": w.value} if w.value is not None else {}),
                      **({"label": w.label} if getattr(w, "label", None) else {}),
                      **({"options": w.options} if hasattr(w, "options") else {}),
                      **({"items": w.items} if hasattr(w, "items") else {})}
                     for w in app.widget_list() if w.id is not None]
            return {"window_id": window_id, "focus": getattr(app.focus_widget, "id", None), "widgets": items}
        return self.on(do)

    @tool("view3d", "Drive the 3D Viewer (GPU renderer for Quake 1 .bsp maps and .obj models). action open "
          "(path; waits for loading), state (file, map info, camera, fps, triangles), camera (set any of x, y, z "
          "in viewer units - Quake maps use Quake's x, y=up, z axes swapped: pass quake=true to give Quake "
          "coordinates - and yaw / pitch in degrees, fov), walk (forward / side in -1..1 for seconds, with "
          "collision and gravity unless noclip), mode (walk | noclip), spawn (index of info_player_* to jump to), "
          "exposure (number, or null for auto), screenshot (full-color PNG of the view, width / height optional).",
          {"action": {"type": "string", "enum": ["open", "state", "camera", "walk", "mode", "spawn", "exposure",
                                                 "screenshot"]},
           "path": STR, "window_id": WID, "x": NUM, "y": NUM, "z": NUM, "quake": BOOL, "yaw": NUM, "pitch": NUM,
           "fov": NUM, "forward": NUM, "side": NUM, "seconds": NUM, "mode": STR, "index": INT,
           "value": {"type": ["number", "null"]}, "width": INT, "height": INT}, ["action"])
    def view3d(self, action, path=None, window_id=None, x=None, y=None, z=None, quake=False, yaw=None, pitch=None,
               fov=None, forward=0.0, side=0.0, seconds=1.0, mode=None, index=None, value=None, width=None,
               height=None):
        import math
        from .viewer3d import Viewer3D

        def get(d):
            if action == "open" and window_id is None:
                return d.launch("viewer3d", path=path) if path else d.find_app(Viewer3D)
            return d.find_app(Viewer3D, window_id)
        win = self.on(get)
        v = win.app
        if action == "open":
            if not path:
                raise ToolError("open needs path")
            if os.path.abspath(os.path.expanduser(path)) != v.path:
                self.on(lambda d: v.open(path))
            self._wait(lambda d: True if (not v.loading and v.pending is None and v.renderer is not None)
                       or not v.loading and v.pending is None and v.renderer is None else None, 60)
        elif action == "camera":
            def do(d):
                cam = v.cam
                if any(c is not None for c in (x, y, z)):
                    p = list(cam.pos)
                    if quake:
                        from .quake import q2gl, gl2q
                        q = list(gl2q(cam.pos))
                        q = [q[0] if x is None else x, q[1] if y is None else y, q[2] if z is None else z]
                        p = list(q2gl(q))
                    else:
                        p = [p[0] if x is None else x, p[1] if y is None else y, p[2] if z is None else z]
                    cam.pos = __import__("numpy").array(p, float)
                if yaw is not None:
                    cam.yaw = math.radians(yaw)
                if pitch is not None:
                    cam.pitch = math.radians(pitch)
                if fov is not None:
                    cam.fov = float(fov)
            self.on(do)
        elif action == "walk":
            def step(d):
                if v.player is not None:
                    v.player.update(1 / 30, float(forward), float(side))
                else:
                    v.cam.fly(float(forward) * 0.1, float(side) * 0.1)
            for _ in range(max(1, int(float(seconds) * 30))):
                self.on(step)
        elif action == "mode":
            if v.player is None:
                raise ToolError("walk / noclip modes are for Quake maps")
            self.on(lambda d: setattr(v.player, "noclip", (mode or "noclip") == "noclip"))
        elif action == "spawn":
            self.on(lambda d: v.respawn(int(index or 0)))
        elif action == "exposure":
            def ex(d):
                if v.renderer is None:
                    raise ToolError("nothing loaded")
                if value is None:
                    v.renderer.auto_exposure = 0.36
                else:
                    v.renderer.auto_exposure, v.renderer.exposure = None, float(value)
            self.on(ex)
        elif action == "screenshot":
            def shot(d):
                if v.renderer is None:
                    raise ToolError("nothing loaded")
                w0, h0 = v.renderer.width, v.renderer.height
                img = v.renderer.render(v.cam, int(width or 640), int(height or 400))
                v.renderer.resize(w0, h0)
                return img
            img = self.on(shot)
            buf = io.BytesIO()
            img.save(buf, "PNG")
            return ImageResult(buf.getvalue(), {"window_id": win.id, **{k: v.game_state()[k] for k in ("file", "camera")}})
        return self.on(lambda d: {"window_id": win.id, **v.game_state()})

    # ---------- building and debugging apps ----------
    def _app_window(self, d, window_id=None, app_id=None):
        if window_id is not None:
            return d.find(window_id)
        if app_id:
            win = next((w for w in reversed(d.windows) if w.app.app_id == app_id), None)
            if win is None:
                raise ToolError(f"no open window of {app_id!r}; open_app it first")
            return win
        if not d.windows:
            raise ToolError("no windows open")
        return d.windows[-1]

    @tool("dev_docs", "Reference for building apps, generated from the code. topic: overview (start here: "
          "how apps work, base classes, a full example), app, game, widgets, gl3d (GPU 3D), mesh (editable "
          "polygon meshes), formats (glTF / OBJ loading), render3d (cameras, CPU 3D), quake, panel, events, "
          "palette.", {"topic": STR})
    def dev_docs(self, topic="overview"):
        from .docs import describe
        try:
            return {"topic": topic, "text": describe(topic)}
        except ValueError as e:
            raise ToolError(str(e))

    @tool("app_call", "Call a command on a running app: runs its cmd_<command>(**args) method and returns the "
          "result. command 'commands' lists what an app offers, 'state' returns its state. Target by "
          "window_id, or app_id (its topmost window), or the focused window.",
          {"command": STR, "args": {"type": "object"}, "window_id": INT, "app_id": STR}, ["command"])
    def app_call(self, command, args=None, window_id=None, app_id=None):
        def do(d):
            win = self._app_window(d, window_id, app_id)
            try:
                r = win.app.on_command(command, args or {})
            except ToolError:
                raise
            except Exception as e:
                import traceback
                d.app_log(win.app.app_id or "?", f"command {command} failed:\n" + traceback.format_exc(limit=6))
                raise ToolError(f"{type(e).__name__}: {e}") from e
            d.dirty = True
            return {"window_id": win.id, "result": json.loads(json.dumps(r, default=lambda o: o.tolist()
                                                                         if hasattr(o, "tolist") else str(o)))}
        return self.on(do, timeout=60)

    @tool("app_eval", "Run Python inside a running app to inspect or change it live (debugging, quick "
          "patches). In scope: app, desk, win, np, and every vga32 module as v (v.gl3d, v.mesh...). The value of "
          "a final expression is returned with anything printed. Runs on the desktop thread: keep it quick. "
          "Needs the admin scope.", {"code": STR, "window_id": INT, "app_id": STR}, ["code"])
    def app_eval(self, code, window_id=None, app_id=None):
        import ast
        import contextlib
        import importlib
        import numpy as np

        def do(d):
            win = self._app_window(d, window_id, app_id) if (window_id is not None or app_id or d.windows) else None
            v = type("vga32", (), {m: importlib.import_module("vga32." + m) for m in
                                   ("gl3d", "mesh", "formats", "render3d", "widgets", "game", "ui", "palette")})
            env = {"app": win.app if win else None, "win": win, "desk": d, "np": np, "v": v}
            tree = ast.parse(code)
            last = tree.body.pop() if tree.body and isinstance(tree.body[-1], ast.Expr) else None
            buf = io.StringIO()
            result = None
            try:
                with contextlib.redirect_stdout(buf):
                    exec(compile(tree, "<app_eval>", "exec"), env)
                    if last is not None:
                        result = eval(compile(ast.Expression(last.value), "<app_eval>", "eval"), env)
            except Exception as e:
                import traceback
                return {"error": traceback.format_exc(limit=4), "output": buf.getvalue()[-5000:]}
            d.dirty = True
            return {"result": repr(result)[:10000] if result is not None else None, "output": buf.getvalue()[-10000:]}
        return self.on(do, timeout=60)

    @tool("app_logs", "Log lines of an app: what it wrote with self.log(...) and tracebacks of crashes in its "
          "draw / input / tick / commands. Without app_id: which apps have logs.",
          {"app_id": STR, "limit": INT, "clear": BOOL})
    def app_logs(self, app_id=None, limit=100, clear=False):
        def do(d):
            if not app_id:
                return {"apps": {k: len(v) for k, v in d.app_logs.items()}}
            log = list(d.app_logs.get(app_id, []))[-int(limit):]
            if clear:
                d.app_logs.pop(app_id, None)
            return {"app_id": app_id, "lines": [{"time": t, "text": s} for t, s in log]}
        return self.on(do)

    @tool("app_data", "An app's persistent storage (self.storage, saved in ~/.vga32/apps/data/<app_id>.json): "
          "get, set (merge values), or replace.",
          {"app_id": STR, "action": {"type": "string", "enum": ["get", "set", "replace"]}, "values": {"type": "object"}},
          ["app_id", "action"])
    def app_data(self, app_id, action, values=None):
        def do(d):
            data = d.app_storage(app_id)
            if action in ("set", "replace"):
                data = dict(values or {}) if action == "replace" else {**data, **(values or {})}
                d.save_app_storage(app_id, data)
                for w in d.windows:          # open windows see the new data
                    if w.app.app_id == app_id:
                        w.app._storage = data
            return {"app_id": app_id, "data": data}
        return self.on(do)

    @tool("app_source", "Read the Python source of an installed app (to fix or extend it), by app id.",
          {"app_id": STR}, ["app_id"])
    def app_source(self, app_id):
        path, src = self.on(lambda d: d.registry.source(app_id))
        return {"app_id": app_id, "file": path, "source": src}

    @tool("unregister_app", "Remove an app added with register_app or install_app (deletes its saved manifest, "
          "plugin file and saved content). Open windows stay.",
          {"id": STR}, ["id"])
    def unregister_app(self, id):
        def do(d):
            d.registry.unregister(id)
            d.dirty = True
            d.bus.publish("app.unregistered", app_id=id)
            return {"removed": id}
        return self.on(do)

    @tool("wait_events", "Desktop events after `since` (a seq; default: only events from now on), waiting up to "
          "wait_seconds (max 300) for the first. Filter with types (prefixes like 'panel.' or exact "
          "'app.launched'), app_id, window_id. Returns {events, next_since}. Types: app.launched, "
          "app.registered, window.opened/closed/focused, panel.click/key/submit/change, menu.select, "
          "dialog.result, hotkey, clipboard.changed, settings.changed, theme.changed, board.attached/detached, "
          "assistant.reply.",
          {"since": INT, "wait_seconds": NUM, "types": {"type": "array", "items": STR}, "app_id": STR,
           "window_id": INT, "limit": INT})
    def wait_events(self, since=None, wait_seconds=30, types=None, app_id=None, window_id=None, limit=50):
        bus = self.desk.bus
        if since is None:
            since = bus.last_seq()
        evs = bus.wait(int(since), min(300.0, float(wait_seconds)), types=types, app_id=app_id,
                       window_id=window_id)[:max(1, int(limit))]
        return {"events": evs, "next_since": evs[-1]["seq"] if evs else int(since)}

    @tool("ask_user", "Show a dialog on the VGA screen and wait for the answer. kind: message (OK), confirm "
          "(buttons, default OK/Cancel), input (text field; value is the default), choice (pick one of "
          "options). Returns {button, value} - button is the clicked label, or null if the window was closed "
          "or wait_seconds (default 120, max 600) ran out. wait_seconds 0 returns the dialog_id immediately; "
          "the answer arrives later as a dialog.result event.",
          {"kind": {"type": "string", "enum": ["message", "confirm", "input", "choice"]}, "text": STR,
           "title": STR, "buttons": {"type": "array", "items": STR}, "value": STR,
           "options": {"type": "array", "items": STR}, "icon": STR, "wait_seconds": NUM}, ["text"])
    def ask_user(self, text, kind="message", title=None, buttons=None, value="", options=None, icon=None,
                 wait_seconds=120):
        seq = self.desk.bus.last_seq()
        win = self.on(lambda d: d.dialog(kind=kind, text=text, title=title, buttons=buttons, value=value,
                                          options=options, icon=icon))
        if not wait_seconds:
            return {"dialog_id": win.id, "since": seq}
        evs = self.desk.bus.wait(seq, min(600.0, float(wait_seconds)), types=["dialog.result"], window_id=win.id)
        if not evs:
            self.on(lambda d: d.close(win) if win in d.windows else None)
            return {"dialog_id": win.id, "button": None, "value": None, "timed_out": True}
        ev = evs[0]
        return {"dialog_id": win.id, "button": ev.get("button"), "value": ev.get("value")}

    @tool("show_menu", "Pop up a menu on the screen (at x,y, default the mouse) and wait for the user to pick. "
          "Returns {id} of the picked item, or {id: null} if they clicked away or wait_seconds (default 60) ran "
          "out.", {"items": MENU_ITEMS, "x": INT, "y": INT, "wait_seconds": NUM}, ["items"])
    def show_menu(self, items, x=None, y=None, wait_seconds=60):
        token = f"m{time.monotonic_ns()}"
        seq = self.desk.bus.last_seq()

        def do(d):
            m = d.open_menu(items, d.mx if x is None else int(x), d.my if y is None else int(y), source="agent",
                            on_pick=lambda it: d.bus.publish("menu.select", id=it.get("id"), menu=token,
                                                             label=it.get("label")))
            m.on_close = lambda: d.bus.publish("menu.select", id=None, menu=token)
        self.on(do)
        end = time.monotonic() + min(300.0, float(wait_seconds))
        while time.monotonic() < end:
            evs = self.desk.bus.wait(seq, end - time.monotonic(), types=["menu.select"])
            for ev in evs:
                seq = ev["seq"]
                if ev.get("menu") == token:
                    return {"id": ev.get("id"), "label": ev.get("label")}
        self.on(lambda d: d.close_menus() if d.menus and d.menus[0].source == "agent" else None)
        return {"id": None, "timed_out": True}

    @tool("clipboard", "The desktop clipboard shared by the apps (Edit menu, Ctrl+Shift+C/V, right-click "
          "Copy/Paste) and the web remote. action get or set (text).",
          {"action": {"type": "string", "enum": ["get", "set"]}, "text": STR}, ["action"])
    def clipboard(self, action, text=None):
        if action == "set":
            if text is None:
                raise ToolError("set needs text")
            self.on(lambda d: d.set_clipboard(text, self.name))
        return {"text": self.desk.clipboard}

    @tool("hotkey", "Register a global keyboard shortcut; presses arrive as hotkey events {id, combo} instead of "
          "going to the focused window. action register (combo like 'ctrl+alt+h', id), unregister (combo), "
          "list.",
          {"action": {"type": "string", "enum": ["register", "unregister", "list"]}, "combo": STR, "id": STR},
          ["action"])
    def hotkey(self, action, combo=None, id=None):
        def do(d):
            if action != "list":
                if not combo:
                    raise ToolError(f"{action} needs combo")
                name, _, mods = _parse_combo(combo)
                from .desktop import combo_name
                key = combo_name(name, mods)
                if action == "register":
                    d.hotkeys[key] = {"id": id or combo, "combo": key, "owner": self.name}
                else:
                    d.hotkeys.pop(key, None)
            return list(d.hotkeys.values())
        return self.on(do)

    @tool("settings", "Read or change persistent settings (~/.vga32/settings.json). action get returns them "
          "(secrets masked); set merges `values`, e.g. {\"theme\": \"platinum\", \"desktop\": {\"wallpaper\": "
          "\"C:/pics/bg.jpg\"}, \"clock_24h\": false, \"assistant\": {\"url\": \"http://127.0.0.1:11434/v1\", "
          "\"model\": \"qwen2.5\"}, \"hidden_apps\": [...], \"webhooks\": [...]}. tokens and webhooks need the "
          "admin scope.",
          {"action": {"type": "string", "enum": ["get", "set"]}, "values": {"type": "object"}}, ["action"])
    def settings(self, action, values=None):
        if action == "set":
            if not isinstance(values, dict) or not values:
                raise ToolError("set needs values (an object)")
            if {"tokens", "webhooks"} & set(values) and "admin" not in self.scopes:
                raise ToolError("changing tokens or webhooks needs the admin scope")
            return self.on(lambda d: d.apply_settings(values))
        return self.on(lambda d: d.settings.public())

    @tool("local_ai", "Ask the local model configured for the Assistant (Ollama, llama.cpp, LM Studio...) a "
          "question and get its reply as text. No tools, no window: a quick way to delegate summarising, "
          "drafting or classifying to the on-prem model.",
          {"prompt": STR, "system": STR, "model": STR, "timeout": NUM}, ["prompt"])
    def local_ai(self, prompt, system=None, model=None, timeout=120):
        from .assistant import chat
        cfg = dict(self.desk.settings["assistant"])
        if model:
            cfg["model"] = model
        msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
        try:
            msg = chat(cfg, msgs, timeout=float(timeout))
        except Exception as e:
            raise ToolError(f"local model at {cfg.get('url')}: {e}") from e
        return {"model": cfg.get("model"), "text": msg.get("content", "")}

    # ---------- MCP ----------
    def mcp(self, msg):
        """Handle one JSON-RPC message (or batch). Returns the response, or None for notifications."""
        if isinstance(msg, list):
            out = [r for r in (self.mcp(m) for m in msg) if r is not None]
            return out or None
        mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
        if mid is None:
            return None                                   # notification (e.g. notifications/initialized)
        try:
            if method == "initialize":
                v = params.get("protocolVersion")
                result = {"protocolVersion": v if v in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                          "capabilities": {"tools": {"listChanged": False},
                                           "resources": {"subscribe": False, "listChanged": False},
                                           "logging": {}},
                          "serverInfo": {"name": "vga32", "version": "1.0.0"},
                          "instructions": INSTRUCTIONS}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": [{k: t[k] for k in ("name", "description", "inputSchema")} for t in TOOLS]}
            elif method == "tools/call":
                result = self._mcp_call(params.get("name"), params.get("arguments") or {})
            elif method == "resources/list":
                result = {"resources": RESOURCES}
            elif method == "resources/templates/list":
                result = {"resourceTemplates": []}
            elif method == "resources/read":
                result = {"contents": [self.read_resource(params.get("uri", ""))]}
            elif method == "logging/setLevel":
                result = {}
            else:
                return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"no method {method}"}}
        except Exception as e:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(e)}}
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def _mcp_call(self, name, args):
        try:
            r = self.run(name, args)
        except ToolError as e:
            return {"content": [{"type": "text", "text": f"Error: {e}"}], "isError": True}
        if isinstance(r, ImageResult):
            return {"content": [{"type": "image", "data": base64.b64encode(r.png).decode(), "mimeType": "image/png"},
                                {"type": "text", "text": json.dumps(r.meta)}]}
        return {"content": [{"type": "text", "text": json.dumps(r, default=str)}]}

    def read_resource(self, uri):
        if uri == "vga32://screen":
            r = self.screenshot()
            return {"uri": uri, "mimeType": "image/png", "blob": base64.b64encode(r.png).decode()}
        if uri == "vga32://state":
            data = self.get_state()
        elif uri == "vga32://apps":
            data = self.list_apps()
        elif uri == "vga32://events":
            data = self.desk.bus.since(max(0, self.desk.bus.last_seq() - 100))
        elif uri == "vga32://settings":
            data = self.on(lambda d: d.settings.public())
        elif uri == "vga32://clipboard":
            return {"uri": uri, "mimeType": "text/plain", "text": self.desk.clipboard}
        elif uri.startswith("vga32://docs/"):
            from .docs import describe
            return {"uri": uri, "mimeType": "text/markdown", "text": describe(uri.rsplit("/", 1)[1])}
        elif uri == "vga32://notes":
            return {"uri": uri, "mimeType": "text/plain",
                    "text": self.on(lambda d: d.find_app(Notes, create=False).app.text
                                    if any(isinstance(w.app, Notes) for w in d.windows) else _read_notes())}
        else:
            raise ToolError(f"unknown resource {uri!r}")
        return {"uri": uri, "mimeType": "application/json", "text": json.dumps(data, default=str)}


def _read_notes():
    try:
        with open(Notes.path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


RESOURCES = [
    {"uri": "vga32://screen", "name": "screen", "mimeType": "image/png", "description": "What the monitor shows"},
    {"uri": "vga32://state", "name": "state", "mimeType": "application/json",
     "description": "Windows, theme, board, menus (same as get_state)"},
    {"uri": "vga32://apps", "name": "apps", "mimeType": "application/json", "description": "Launchable apps"},
    {"uri": "vga32://events", "name": "events", "mimeType": "application/json",
     "description": "The last 100 desktop events"},
    {"uri": "vga32://settings", "name": "settings", "mimeType": "application/json",
     "description": "Persistent settings (secrets masked)"},
    {"uri": "vga32://clipboard", "name": "clipboard", "mimeType": "text/plain", "description": "Clipboard text"},
    {"uri": "vga32://notes", "name": "notes", "mimeType": "text/plain", "description": "The Notes text"},
] + [{"uri": f"vga32://docs/{t}", "name": f"docs: {t}", "mimeType": "text/markdown",
      "description": f"App development reference: {t}"} for t in
     ("overview", "app", "game", "widgets", "gl3d", "mesh", "formats", "render3d", "quake", "panel", "events",
      "palette")]
