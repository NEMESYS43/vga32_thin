"""Assistant: a chat window backed by a local model (Ollama, llama.cpp, LM Studio... any
OpenAI-compatible /chat/completions endpoint) that can drive this desktop with the agent tools.

The endpoint, model and key come from settings["assistant"]. The conversation runs on a worker
thread; tool calls go through Agent.run, which hops onto the desktop thread like any MCP call.
"""
import json
import threading
import urllib.request

from .palette import C
from .panel import _wrap
from .ui import CH, FONT, App

MAX_ROUNDS = 8
SYSTEM = ("You are the assistant built into a retro desktop shown on a 640x400, 16-color VGA monitor. "
          "You can see and control the desktop with the tools provided: open apps, run terminal commands on "
          "the host PC, browse the web, show panels, notify, ask the user with dialogs. Use get_state first "
          "when you need to know what's open. Keep replies short; the window is small.")
COLORS = {"user": C.navy, "assistant": C.black, "tool": C.green, "error": C.maroon, "info": C.darkgray}


def chat(cfg, messages, tools=None, timeout=180):
    """One /chat/completions call; returns the assistant message dict."""
    url = cfg.get("url", "").rstrip("/")
    if not url:
        raise RuntimeError("set the assistant endpoint in Settings > Agents")
    if not url.endswith("/chat/completions"):
        url += "/chat/completions"
    body = {"model": cfg.get("model") or "llama3.1", "messages": messages, "stream": False}
    if tools:
        body["tools"] = tools
    headers = {"Content-Type": "application/json"}
    if cfg.get("api_key"):
        headers["Authorization"] = "Bearer " + cfg["api_key"]
    req = urllib.request.Request(url, json.dumps(body).encode(), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read())
    return data["choices"][0]["message"]


def tool_specs(exclude=("screenshot",)):
    from .agent import TOOLS
    return [{"type": "function", "function": {"name": t["name"], "description": t["description"][:1000],
                                              "parameters": t["inputSchema"]}}
            for t in TOOLS if t["name"] not in exclude]


class Assistant(App):
    title = "Assistant"
    size = (420, 300)
    icon = "assistant"
    category = "Agents"

    def __init__(self, desk, text=None, **kw):
        super().__init__(desk)
        self.log = [("info", "Ask me to do things on this desktop. Enter sends.")]
        self.messages = []
        self.inp = ""
        self.busy = False
        self.status = ""
        self.scroll = 0
        self.stop = False
        self.lock = threading.Lock()
        self._changed = False
        if text:
            self.send(text)

    # ----- conversation -----
    def _add(self, role, text):
        with self.lock:
            self.log.append((role, str(text)))
            del self.log[:-300]
            self._changed = True

    def send(self, text):
        text = text.strip()
        if not text or self.busy:
            return
        self._add("user", text)
        self.busy, self.stop, self.scroll = True, False, 0
        threading.Thread(target=self._run, args=(text,), daemon=True).start()

    def _run(self, text):
        from .agent import Agent, ImageResult, ToolError
        cfg = dict(self.desk.settings["assistant"])
        agent = Agent(self.desk)
        if not self.messages:
            self.messages = [{"role": "system", "content": cfg.get("system") or SYSTEM}]
        self.messages.append({"role": "user", "content": text})
        tools = tool_specs()
        try:
            for _ in range(MAX_ROUNDS):
                self.status = "thinking..."
                self._changed = True
                try:
                    msg = chat(cfg, self.messages, tools)
                except urllib.error.HTTPError as e:
                    if e.code == 400 and tools:     # model without tool support: plain chat
                        tools = None
                        msg = chat(cfg, self.messages)
                    else:
                        raise
                self.messages.append({k: v for k, v in msg.items() if k in ("role", "content", "tool_calls")})
                calls = msg.get("tool_calls") or []
                if msg.get("content"):
                    self._add("assistant", msg["content"].strip())
                if not calls or self.stop:
                    break
                for c in calls:
                    fn = c.get("function", {})
                    name = fn.get("name", "")
                    try:
                        args = fn.get("arguments") or {}
                        args = json.loads(args) if isinstance(args, str) else args
                    except ValueError:
                        args = {}
                    self.status = f"running {name}..."
                    self._add("tool", f"> {name} {json.dumps(args)[:120]}")
                    try:
                        r = agent.run(name, args)
                        out = json.dumps(r.meta if isinstance(r, ImageResult) else r, default=str)
                    except ToolError as e:
                        out = json.dumps({"error": str(e)})
                    self.messages.append({"role": "tool", "tool_call_id": c.get("id", name), "name": name,
                                          "content": out[:6000]})
        except Exception as e:
            self._add("error", f"{type(e).__name__}: {e}")
        finally:
            self.busy, self.status = False, ""
            self._changed = True
            self.desk.bus.publish("assistant.reply", app_id="assistant",
                                  text=next((t for r, t in reversed(self.log) if r == "assistant"), ""))

    def clear(self):
        if not self.busy:
            self.messages, self.log = [], [("info", "New conversation.")]

    # ----- app -----
    def tick(self):
        if self._changed:
            self._changed = False
            return True
        return self.busy

    def key(self, k):
        if not k.down:
            return
        if k.name == "RETURN":
            text, self.inp = self.inp, ""
            self.send(text)
        elif k.name == "BACKSPACE":
            self.inp = self.inp[:-1]
        elif k.name == "ESCAPE" and self.busy:
            self.stop = True
        elif k.name in ("PAGEUP", "PAGEDOWN"):
            self.scroll = max(0, self.scroll + (8 if k.name == "PAGEUP" else -8))
        elif k.char and not k.ctrl:
            self.inp += k.char

    def mouse(self, x, y, buttons, pressed, released, wheel):
        if wheel:
            self.scroll = max(0, self.scroll + wheel * 3)

    def copy(self):
        return next((t for r, t in reversed(self.log) if r == "assistant"), None)

    def paste(self, text):
        self.inp += text.replace("\r", "").replace("\n", " ")

    def context_menu(self, x, y):
        return [{"id": "copy", "label": "Copy Last Answer", "enabled": self.copy() is not None},
                {"id": "paste", "label": "Paste", "enabled": bool(self.desk.clipboard)},
                {"separator": True},
                {"id": "stop", "label": "Stop", "shortcut": "Esc", "enabled": self.busy},
                {"id": "clear", "label": "New Conversation", "enabled": not self.busy},
                {"id": "settings", "label": "Assistant Settings..."}]

    def menus(self):
        return [("Chat", [{"id": "clear", "label": "New Conversation"}, {"id": "stop", "label": "Stop"},
                          {"id": "settings", "label": "Settings..."}])]

    def on_menu(self, item_id):
        if item_id == "copy" and self.copy():
            self.desk.set_clipboard(self.copy(), "assistant")
        elif item_id == "paste":
            self.paste(self.desk.clipboard)
        elif item_id == "stop":
            self.stop = True
        elif item_id == "clear":
            self.clear()
        elif item_id == "settings":
            self.desk.launch("settings", page="agents")

    def draw(self, d, x, y, w, h):
        d.rectangle([x, y, x + w - 1, y + h - 1], fill=C.white)
        ih = CH + 8
        with self.lock:
            log = list(self.log)
        lines = []
        for role, text in log:
            prefix = {"user": "You: ", "assistant": "", "tool": "", "error": "! ", "info": ""}[role]
            for ln in _wrap(prefix + text, FONT, w - 12):
                lines.append((role, ln))
            lines.append(("info", ""))
        rows = max(1, (h - ih - 4) // CH)
        self.scroll = min(self.scroll, max(0, len(lines) - rows))
        end = len(lines) - self.scroll
        for i, (role, ln) in enumerate(lines[max(0, end - rows):end]):
            d.text((x + 6, y + 2 + i * CH), ln, font=FONT, fill=COLORS[role])
        iy = y + h - ih
        d.rectangle([x, iy, x + w - 1, y + h - 1], fill=C.gray)
        d.rectangle([x + 3, iy + 3, x + w - 4, y + h - 4], fill=C.white, outline=C.darkgray)
        shown = self.status if self.busy else self.inp
        while shown and FONT.getlength(shown) > w - 16:
            shown = shown[1:]
        d.text((x + 6, iy + 4), shown, font=FONT, fill=C.darkgray if self.busy else C.black)
        if not self.busy:
            cx = x + 7 + int(FONT.getlength(shown))
            d.line([cx, iy + 5, cx, iy + CH + 2], fill=C.black)

    def state(self):
        return {"busy": self.busy, "status": self.status, "model": self.desk.settings["assistant"].get("model"),
                "last": [{"role": r, "text": t[:500]} for r, t in self.log[-6:]]}
