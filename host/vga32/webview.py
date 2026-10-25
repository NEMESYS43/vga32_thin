"""Full web browser: a real Chromium (Edge/Chrome via Playwright) renders the page with
JavaScript, CSS and video; its screencast frames are dithered onto the palette and the
desktop's mouse and keyboard are forwarded to the page.

Playwright's sync API is single-threaded, so one Engine thread owns the browser and
runs every page operation; windows talk to it through Engine.submit / Engine.call.
"""
import base64
import concurrent.futures
import io
import queue
import threading
import time

from PIL import Image

from .palette import C, dither, ordered_dither
from .ui import CH, CW, FONT, App, bevel

TOOLBAR_H = 24
STATUS_H = CH + 2
SEARCH = "https://duckduckgo.com/?q="
CHANNELS = ("msedge", "chrome", None)      # installed Edge, installed Chrome, Playwright's own Chromium
JPEG_QUALITY = 80

HOME = """<!doctype html><html><head><title>Web</title><style>
body{font:16px system-ui,sans-serif;margin:24px;background:#fff;color:#000}
h1{color:#00a;margin:0 0 8px} a{color:#00a} li{margin:6px 0} code{background:#eee;padding:1px 4px}
</style></head><body><h1>Web</h1>
<p>A real browser (JavaScript, CSS, video) streamed to the VGA screen in 16 colors.</p>
<p>Click the address bar or press <code>Ctrl+L</code> to type a URL or search.</p>
<ul><li><a href="https://www.youtube.com/">YouTube</a></li><li><a href="https://en.wikipedia.org/">Wikipedia</a></li>
<li><a href="https://news.ycombinator.com/">Hacker News</a></li><li><a href="https://www.openstreetmap.org/">OpenStreetMap</a></li>
<li><a href="https://play2048.co/">2048</a></li></ul>
<p>Keys: <code>Alt+Left/Right</code> back/forward, <code>F5</code> reload, <code>Ctrl+D</code> cycles dithering.</p>
</body></html>"""

KEYMAP = {"RETURN": "Enter", "KP_ENTER": "Enter", "BACKSPACE": "Backspace", "TAB": "Tab", "ESCAPE": "Escape",
          "DELETE": "Delete", "INSERT": "Insert", "HOME": "Home", "END": "End", "PAGEUP": "PageUp",
          "PAGEDOWN": "PageDown", "UP": "ArrowUp", "DOWN": "ArrowDown", "LEFT": "ArrowLeft", "RIGHT": "ArrowRight",
          "SPACE": " ", **{f"F{i}": f"F{i}" for i in range(1, 13)}}
MODIFIERS = {"LCTRL": "Control", "RCTRL": "Control", "LSHIFT": "Shift", "RSHIFT": "Shift",
             "LALT": "Alt", "RALT": "Alt"}
DITHERS = ("ordered", "floyd", "none")


def to_url(s):
    s = s.strip()
    if not s:
        return "about:home"
    if "://" in s or s.startswith(("about:", "data:", "file:")):
        return s
    if " " not in s and ("." in s or s.startswith("localhost")):
        return ("http://" if s.startswith(("localhost", "127.", "192.168.", "10.")) else "https://") + s
    from urllib.parse import quote_plus
    return SEARCH + quote_plus(s)


class Engine:
    _instance = None
    _lock = threading.Lock()

    @classmethod
    def get(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = Engine()
            return cls._instance

    def __init__(self):
        self.q = queue.Queue()
        self.ready = threading.Event()
        self.error = None
        self.browser = None
        self.channel = None
        self.user_agent = None
        threading.Thread(target=self._run, name="web-engine", daemon=True).start()

    def submit(self, fn):
        """Run fn() on the engine thread; returns a Future."""
        fut = concurrent.futures.Future()
        self.q.put((fn, fut))
        return fut

    def call(self, fn, timeout=30):
        return self.submit(fn).result(timeout)

    @classmethod
    def shutdown(cls, timeout=5):
        """Close the browser from its own thread (Playwright objects can't cross threads)."""
        eng = cls._instance
        if eng is None or eng.browser is None:
            return
        def stop():
            eng.browser.close()
            eng.pw.stop()
            eng.stopped = True
        try:
            eng.call(stop, timeout)
        except Exception:
            pass

    def _launch(self):
        errors = []
        for ch in CHANNELS:
            try:
                b = self.pw.chromium.launch(channel=ch, headless=True, args=[
                    "--autoplay-policy=no-user-gesture-required", "--mute-audio", "--hide-scrollbars"])
                self.channel = ch or "chromium"
                return b
            except Exception as e:
                errors.append(f"{ch or 'chromium'}: {str(e).splitlines()[0][:80]}")
        raise RuntimeError("no browser could be started (" + "; ".join(errors) + ")")

    def _run(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.error = "Playwright is not installed: pip install playwright"
            self.ready.set()
            return
        try:
            self.pw = sync_playwright().start()
            self.browser = self._launch()
            # A blank page whose wait_for_timeout lets Playwright deliver events (screencast frames).
            self.pump = self.browser.new_page()
            # Headless builds announce "HeadlessEdg"/"HeadlessChrome", which many sites block; look like
            # the normal browser instead.
            self.user_agent = self.pump.evaluate("navigator.userAgent").replace("Headless", "")
        except Exception as e:
            self.error = str(e)
            self.ready.set()
            return
        self.ready.set()
        self.stopped = False
        while not self.stopped:
            while True:
                try:
                    fn, fut = self.q.get_nowait()
                except queue.Empty:
                    break
                if fut.set_running_or_notify_cancel():
                    try:
                        fut.set_result(fn())
                    except Exception as e:
                        fut.set_exception(e)
            if self.stopped:
                break
            try:
                self.pump.wait_for_timeout(8)
            except Exception:
                time.sleep(0.008)


class WebView(App):
    title = "Web"
    icon = "web"
    size = (600, 360)

    def __init__(self, desk, url=None, **kw):
        super().__init__(desk)
        self.engine = Engine.get()
        self.page = self.ctx = self.cdp = None
        self.frame = None              # latest palette-converted frame
        self.frame_time = 0
        self.fps, self._fps_n, self._fps_t = 0.0, 0, time.monotonic()
        self.url, self.page_title, self.loading = "", "", True
        self.status = "starting browser..."
        self.error = None
        self.dither = "ordered"
        self.viewport = None
        self.editing, self.addr, self.addr_fresh = False, "", False
        self.move_pending = None
        self.closed = False
        self._woken = False
        self.buttons = 0
        w, h = self._viewport_size()
        self.engine.submit(lambda: self._create(w, h, url))

    # ---------- engine thread ----------
    def _create(self, w, h, url):
        if not self.engine.ready.wait(60) or self.engine.error:
            self.error = self.engine.error or "browser did not start"
            self._wake()
            return
        if self.closed:
            return
        self.ctx = self.engine.browser.new_context(viewport={"width": w, "height": h}, device_scale_factor=1,
                                                   user_agent=self.engine.user_agent, locale="en-US")
        self.page = self.ctx.new_page()
        self.viewport = (w, h)
        self.page.on("load", lambda: self._after_nav())
        self.page.on("framenavigated", lambda f: f == self.page.main_frame and self._after_nav(False))
        self.page.on("popup", self._popup)
        self.cdp = self.ctx.new_cdp_session(self.page)
        self.cdp.on("Page.screencastFrame", self._on_frame)
        self._start_cast()
        self._goto(url or "about:home")

    def _popup(self, popup):
        # Keep everything in this window: load the popup's URL here instead.
        url = popup.url
        try:
            popup.close()
        except Exception:
            pass
        if url and url != "about:blank":
            self._goto(url)

    def _start_cast(self):
        w, h = self.viewport
        self.cdp.send("Page.startScreencast", {"format": "jpeg", "quality": JPEG_QUALITY,
                                               "maxWidth": w, "maxHeight": h, "everyNthFrame": 1})

    def _on_frame(self, ev):
        try:
            self.cdp.send("Page.screencastFrameAck", {"sessionId": ev["sessionId"]})
        except Exception:
            return
        im = Image.open(io.BytesIO(base64.b64decode(ev["data"]))).convert("RGB")
        if self.viewport and im.size != self.viewport:
            im = im.resize(self.viewport, Image.BILINEAR)
        self.frame = self._convert(im)
        self.frame_time = time.monotonic()
        self._fps_n += 1
        now = time.monotonic()
        if now - self._fps_t >= 1:
            self.fps, self._fps_n, self._fps_t = self._fps_n / (now - self._fps_t), 0, now
        self._wake()

    def _convert(self, im):
        if self.dither == "ordered":
            return ordered_dither(im)
        if self.dither == "floyd":
            return dither(im)
        return im                       # nearest colors (the desktop maps it)

    def _wake(self):
        # Ask the desktop thread to redraw; coalesced so a fast video doesn't flood its queue.
        if not self._woken:
            self._woken = True
            from .desktop import Call
            self.desk.inbox.put(Call(lambda d: None))

    def _after_nav(self, loaded=True):
        try:
            self.url = self.page.url
            if loaded:
                self.loading = False
                self.page_title = self.page.title()
                self.title = (self.page_title or self.url)[:40] + " - Web"
                self.status = ""
        except Exception:
            pass
        self._wake()

    def _goto(self, url):
        url = to_url(url)
        self.loading, self.error, self.status = True, None, "loading " + url
        if url == "about:home":
            self.page.set_content(HOME)
            self.url, self.loading = "about:home", False
            self.page_title, self.title = "Web", "Web"
            return
        try:
            self.page.goto(url, wait_until="commit", timeout=30000)
        except Exception as e:
            self.loading, self.error = False, str(e).splitlines()[0][:160]
        self._wake()

    def _resize(self, w, h):
        if self.page and (w, h) != self.viewport:
            self.viewport = (w, h)
            self.page.set_viewport_size({"width": w, "height": h})
            self.cdp.send("Page.stopScreencast")
            self._start_cast()

    # ---------- helpers for any thread ----------
    def _viewport_size(self):
        w, h = self.desk.client_size(self)
        return max(64, w), max(48, h - TOOLBAR_H - STATUS_H)

    def run(self, fn):
        """Queue fn(page) on the engine thread (fire and forget)."""
        if self.page is not None:
            self.engine.submit(lambda: self.page and not self.closed and fn(self.page))

    def navigate(self, url):
        self.loading, self.status = True, "loading..."
        self.engine.submit(lambda: self.page and self._goto(url))

    def back(self):
        self.run(lambda p: p.go_back(wait_until="commit"))

    def forward(self):
        self.run(lambda p: p.go_forward(wait_until="commit"))

    def reload(self):
        self.run(lambda p: p.reload(wait_until="commit"))

    def set_dither(self, mode):
        if mode not in DITHERS:
            raise ValueError(f"dither must be one of {', '.join(DITHERS)}")
        self.dither = mode
        self.status = "dither: " + mode
        # Ask for a fresh frame so the change shows even on a static page.
        self.run(lambda p: (self.cdp.send("Page.stopScreencast"), self._start_cast()))

    def close(self):
        self.closed = True
        ctx = self.ctx
        if ctx:
            self.engine.submit(ctx.close)

    # ---------- input (desktop thread) ----------
    def key(self, k):
        if self.editing:
            if not k.down:
                return
            if k.name == "RETURN":
                self.editing = False
                self.navigate(self.addr)
            elif k.name == "ESCAPE":
                self.editing = False
            elif k.name == "BACKSPACE":
                self.addr = "" if self.addr_fresh else self.addr[:-1]
            elif k.char:
                self.addr = k.char if self.addr_fresh else self.addr + k.char
            self.addr_fresh = False
            return
        if k.down:
            if (k.ctrl and k.name in ("l", "L")) or k.name == "F6":
                self.editing, self.addr, self.addr_fresh = True, self.url, True
                return
            if k.ctrl and k.name in ("d", "D"):
                self.set_dither(DITHERS[(DITHERS.index(self.dither) + 1) % len(DITHERS)])
                return
            if k.name == "F5":
                self.reload()
                return
            if k.alt and k.name in ("LEFT", "RIGHT"):
                (self.back if k.name == "LEFT" else self.forward)()
                return
        if k.name in MODIFIERS:
            name = MODIFIERS[k.name]
            self.run(lambda p: (p.keyboard.down if k.down else p.keyboard.up)(name))
            return
        key = KEYMAP.get(k.name) or k.char or (k.name if len(k.name) == 1 else None)
        if not key:
            return
        if k.ctrl or k.alt:
            # Combos from the agent / web remote arrive as mods without separate modifier events.
            if k.down:
                combo = "+".join([m for m, on in (("Control", k.ctrl), ("Alt", k.alt),
                                                  ("Shift", k.shift and len(key) > 1)) if on] + [key])
                self.run(lambda p: p.keyboard.press(combo))
            return
        self.run(lambda p: (p.keyboard.down if k.down else p.keyboard.up)(key))

    def _buttons(self, w):
        return [("back", 4), ("fwd", 30), ("reload", 56), ("home", 82)], (110, w - 114)

    def mouse(self, x, y, buttons, pressed, released, wheel):
        w, h = self.desk.client_size(self)
        if y < TOOLBAR_H and not self.buttons:
            if pressed & 1:
                btns, (ax, aw) = self._buttons(w)
                for name, bx in btns:
                    if bx <= x < bx + 24:
                        {"back": self.back, "fwd": self.forward, "reload": self.reload,
                         "home": lambda: self.navigate("about:home")}[name]()
                        return
                if ax <= x:
                    self.editing, self.addr, self.addr_fresh = True, self.url, True
            return
        self.editing = False
        px, py = x, y - TOOLBAR_H
        # Coalesce moves: only the latest position is sent when the engine gets to it.
        first = self.move_pending is None
        self.move_pending = (px, py)
        if first:
            def move(p):
                pos, self.move_pending = self.move_pending, None
                if pos:
                    p.mouse.move(*pos)
            self.run(move)
        for bit, name in ((1, "left"), (2, "right"), (4, "middle")):
            if pressed & bit:
                self.run(lambda p, n=name, q=(px, py): (p.mouse.move(*q), p.mouse.down(button=n)))
            if released & bit:
                self.run(lambda p, n=name, q=(px, py): (p.mouse.move(*q), p.mouse.up(button=n)))
        self.buttons = buttons
        if wheel:
            self.run(lambda p, d=-wheel * 120: p.mouse.wheel(0, d))

    # ---------- drawing (desktop thread) ----------
    def draw(self, d, x, y, w, h):
        self._woken = False
        vw, vh = self._viewport_size()
        if self.page is not None and (vw, vh) != self.viewport:
            self.engine.submit(lambda: self._resize(vw, vh))
        d.rectangle([x, y, x + w - 1, y + h - 1], fill=C.white)
        # toolbar
        d.rectangle([x, y, x + w - 1, y + TOOLBAR_H - 1], fill=C.gray)
        btns, (ax, aw) = self._buttons(w)
        for (name, bx), g in zip(btns, "<>RH"):
            bevel(d, x + bx, y + 2, 24, TOOLBAR_H - 4)
            d.text((x + bx + 8, y + 5), g, font=FONT, fill=C.black)
        bevel(d, x + ax, y + 2, aw, TOOLBAR_H - 4, raised=False, face=C.white)
        maxc = max(1, (aw - 10) // CW)
        if self.editing:
            if self.addr_fresh:
                d.rectangle([x + ax + 3, y + 5, x + ax + 4 + min(len(self.addr), maxc) * CW, y + 5 + CH - 3],
                            fill=C.sky)
                shown = self.addr[:maxc]
            else:
                shown = self.addr[-maxc + 1:] + "_"
        else:
            shown = self.url[:maxc]
        d.text((x + ax + 4, y + 5), shown, font=FONT, fill=C.black)
        # page
        if self.frame is not None:
            self.desk.target.paste(self.frame, (x, y + TOOLBAR_H))
        else:
            msg = self.error or self.engine.error or self.status
            d.text((x + 10, y + TOOLBAR_H + 10), msg[:max(1, (w - 20) // CW)], font=FONT, fill=C.black)
        # status
        sy = y + h - STATUS_H
        d.rectangle([x, sy, x + w - 1, y + h - 1], fill=C.gray)
        left = self.error or self.status or ("loading..." if self.loading else self.page_title)
        right = f"{self.fps:4.1f} fps  {self.dither}" if time.monotonic() - self.frame_time < 2 else self.dither
        d.text((x + 4, sy + 1), left[:max(1, (w - 12) // CW - len(right) - 2)], font=FONT, fill=C.black)
        d.text((x + w - 6 - len(right) * CW, sy + 1), right, font=FONT, fill=C.black)

    def state(self):
        return {"url": self.url, "title": self.page_title, "loading": self.loading, "error": self.error,
                "viewport": list(self.viewport) if self.viewport else None, "fps": round(self.fps, 1),
                "dither": self.dither, "engine": self.engine.channel}
