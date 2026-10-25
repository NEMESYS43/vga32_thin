"""A small window manager rendered on the host and streamed to the board.

Everything is drawn into a PIL RGB image with palette colors (C.*), converted to
palette indices and diffed by Screen. Aliased text keeps every pixel on-palette.

The desktop outlives board connections: while no board is attached it draws into a
NullDevice, so the web UI and agent keep working and windows survive a board reboot.
All input (board, browser, agent calls) arrives on one queue, `inbox`, and is handled
on the desktop thread, so apps never need locks.

How things look (window chrome, the taskbar or menu bar, menus, palette, cursor) belongs to
the current theme (theme.py). What can be launched comes from the app registry (registry.py),
and everything that happens is published on the event bus (events.py).
"""
import collections
import concurrent.futures
import contextlib
import copy
import itertools
import math
import queue
import threading
import time

from PIL import Image, ImageDraw

from . import images
from . import theme as themes
from .apps import About, Clock, Notes, Paint, System, Terminal
from .assistant import Assistant
from .browser import Browser
from .client import Info, Key, Mouse
from .dialogs import Dialog
from .events import EventBus
from .files import Files
from .menu import Menu
from .palette import C
from .panel import Panel, prepare
from .registry import AppRegistry
from .screen import Screen
from .settings import Settings
from .settings_app import SettingsApp
from .ui import CH, CW, FONT, bevel, parse_color, text_size
from .viewer import Viewer
from .webview import WebView

# id, class, registry fields
BUILTINS = [
    ("files", Files, {"category": "Accessories", "desktop": True}),
    ("terminal", Terminal, {"category": "System", "desktop": True}),
    ("web", WebView, {"category": "Internet", "desktop": True}),
    ("browser", Browser, {"category": "Internet"}),
    ("viewer", Viewer, {"category": "Accessories"}),
    ("notes", Notes, {"category": "Accessories", "desktop": True}),
    ("paint", Paint, {"category": "Accessories"}),
    ("clock", Clock, {"category": "Accessories"}),
    ("assistant", Assistant, {"category": "Agents", "desktop": True}),
    ("system", System, {"category": "System"}),
    ("settings", SettingsApp, {"category": "System", "desktop": True}),
    ("about", About, {"category": "System"}),
    ("panel", Panel, {"category": "Agents", "hidden": True}),
    ("dialog", Dialog, {"category": "System", "hidden": True}),
]
ALIASES = {"images": "viewer", "image": "viewer", "control_panel": "settings", "chat": "assistant"}
LAYOUTS = ["cascade", "tile", "rows", "grid", "main-left", "main-top", "maximize"]
MIN_W = 140
DOUBLE_CLICK = 0.35       # seconds between clicks to count as a double-click
DOUBLE_CLICK_SLOP = 4     # max px the pointer may move between them
ECHO_WINDOW = 1.5         # seconds a board echo of a browser warp may lag behind
ICON_W, ICON_H = 48, 54


class NullDevice:
    """Stands in for the board while none is connected: every draw op is a no-op."""
    host = "(none)"
    closed = False

    def __init__(self, w, h):
        self.info = Info(1, w, h, 4, False, False, [], "(no board)")

    def wait_sync(self, *a, **k):
        return True

    def batch(self):
        return contextlib.nullcontext()

    def __getattr__(self, name):
        return lambda *a, **k: 0


class Call:
    """Run fn(desktop) on the desktop thread; the caller waits on .future."""
    def __init__(self, fn):
        self.fn = fn
        self.future = concurrent.futures.Future()


class Attach:
    def __init__(self, dev):
        self.dev = dev


class Window:
    _ids = itertools.count(1)

    def __init__(self, app, x, y, w, h):
        self.id = next(Window._ids)
        self.app = app
        self.x, self.y, self.w, self.h = x, y, w, h
        self.saved = None          # geometry before maximize
        self.shaded = False        # rolled up to just the title bar
        self.on_top = False

    @property
    def title(self):
        return self.app.title

    @property
    def visible_h(self):
        return themes.current().shaded_h() if self.shaded else self.h

    def contains(self, px, py):
        return self.x <= px < self.x + self.w and self.y <= py < self.y + self.visible_h

    def client(self):
        return themes.current().client(self)

    def info(self, focused):
        cx, cy, cw, ch = self.client()
        return {"id": self.id, "app": type(self.app).__name__.lower(), "app_id": self.app.app_id,
                "title": self.app.title, "x": self.x, "y": self.y, "w": self.w, "h": self.h,
                "client": {"x": cx, "y": cy, "w": cw, "h": ch}, "focused": focused,
                "maximized": self.saved is not None, "shaded": self.shaded, "on_top": self.on_top,
                "details": self.app.state()}


def _inside(r, px, py):
    x, y, w, h = r
    return x <= px < x + w and y <= py < y + h


def combo_name(name, mods):
    """Canonical hotkey text, e.g. "ctrl+alt+h", from a key name and modifier bits."""
    parts = [m for bit, m in ((1, "ctrl"), (6, "alt"), (8, "shift"), (16, "win")) if mods & bit]
    return "+".join(parts + [name.lower()])


class Desktop:
    def __init__(self, w=640, h=400, web=None, settings=None):
        self.W, self.H = w, h
        self.web = web
        self.settings = settings or Settings()
        try:
            self.theme = themes.activate(self.settings["theme"])
        except ValueError:
            self.theme = themes.activate("classic")
        self.registry = AppRegistry()
        for app_id, cls, kw in BUILTINS:
            self.registry.add_builtin(app_id, cls, **kw)
        self.registry.load_dir()
        self.bus = EventBus(self._webhooks_for)
        self.inbox = queue.Queue()
        self.dev = NullDevice(w, h)
        self.screen = Screen(self.dev)
        self.img = Image.new("RGB", (w, h))
        self.bg = None
        self.wall = None           # wallpaper already fitted and dithered to the work area
        self.wall_key = None
        self.target = None         # image the app being drawn renders into
        self.windows = []          # last = focused
        self.menus = []            # open popup menus, root first
        self.menu_opened = 0.0
        self.drag = None           # (window, dx, dy)
        self.resize = None         # (window, x0, y0, w0, h0)
        self.capture = None        # window receiving mouse while a button is held
        self.icon_drag = None      # (app_id, dx, dy)
        self.icon_sel = None
        self.mx, self.my, self.buttons = w // 2, h // 2, 0
        self.last_click = (None, 0, 0, 0)   # (thing, time, x, y) for double-clicks
        self.toasts = []           # (text, expires)
        self.clipboard = ""
        self.hotkeys = {}          # combo -> {"id", "combo", "owner"}
        self.dirty = True
        self.frames = 0
        self.clock = ""
        self.web_warps = collections.deque(maxlen=256)   # (x, y, time) of recent warps done for the browser
        self._load_wallpaper()
        self.launch("about")

    # ----- board -----
    @property
    def connected(self):
        return not isinstance(self.dev, NullDevice)

    def attach(self, dev):
        was = self.connected
        self.dev = dev
        w, h = dev.info.width, dev.info.height
        if (w, h) != (self.W, self.H):
            self.W, self.H = w, h
            self.img = Image.new("RGB", (w, h))
            self.bg = None
            self.wall_key = None
            self._load_wallpaper()
            for win in self.windows:
                self.set_geometry(win, win.x, win.y, win.w, win.h)
        self.screen = Screen(dev)
        self._apply_device()
        self.dirty = True
        if self.connected:
            self.bus.publish("board.attached", name=dev.info.name, host=dev.host)
        elif was:
            self.bus.publish("board.detached")

    def detach(self):
        self.attach(NullDevice(self.W, self.H))

    def _apply_device(self):
        """Send the theme's palette and cursor to the board."""
        self.dev.palette(self.theme.palette)
        cur = self.theme.cursor()
        if cur is None:
            self.dev.cursor_arrow()
        else:
            self.dev.cursor_image(*cur)

    def call(self, fn, timeout=15):
        """From any thread: run fn(desktop) on the desktop thread and return its result."""
        c = Call(fn)
        self.inbox.put(c)
        return c.future.result(timeout)

    def post(self, fn):
        """From any thread: run fn(desktop) on the desktop thread soon, without waiting."""
        self.inbox.put(Call(fn))

    # ----- apps -----
    def resolve(self, app_id):
        return ALIASES.get(str(app_id).lower(), app_id)

    def launch(self, app_id, x=None, y=None, w=None, h=None, **kw):
        """Open a registered app by id and return its window."""
        e = self.registry.get(self.resolve(app_id))
        if e.get("single_instance"):
            for win in reversed(self.windows):
                if win.app.app_id == e["id"]:
                    win.shaded = False
                    self.focus(win)
                    return win
        kind = e["kind"]
        size = e.get("size")
        if size and w is None and h is None:
            w, h = self.theme.frame_size(int(size[0]), int(size[1]))
        if kind in ("builtin", "python"):
            win = self.open(e["cls"], x, y, w, h, **kw)
        elif kind == "panel":
            spec = copy.deepcopy(e.get("spec") or {"elements": [{"type": "text", "text": "Starting..."}]})
            spec.setdefault("title", e["title"])
            if e.get("menu") and "menu" not in spec:
                spec["menu"] = e["menu"]
            win = self.open(Panel, x, y, w, h, spec=spec, title=kw.get("title") or e["title"])
            self._prepare_async(win, spec.get("elements", []))
        elif kind == "web":
            win = self.open(WebView, x, y, w, h, url=kw.get("url") or e["url"])
        elif kind == "browser":
            win = self.open(Browser, x, y, w, h, url=kw.get("url") or e["url"])
        elif kind == "command":
            win = self.open(Terminal, x, y, w, h, command=kw.get("command") or e["command"])
        else:
            raise ValueError(f"can't launch kind {kind!r}")
        win.app.app_id = e["id"]
        if kind not in ("builtin",):
            self.bus.publish("app.launched", app_id=e["id"], window_id=win.id, owner=e.get("owner"))
        return win

    def _prepare_async(self, win, elements):
        if not any(el.get("type") == "image" for el in elements):
            return

        def work():
            try:
                prepare(elements)
            except Exception:
                pass
            self.post(lambda d: setattr(d, "dirty", True))
        threading.Thread(target=work, daemon=True).start()

    def apps_menu_items(self, flat=False):
        hidden = self.settings["hidden_apps"]
        launch = lambda app_id: (lambda: self.launch(app_id))
        cats = self.registry.categories(hidden)
        if flat:
            return [{"label": e["title"], "action": launch(e["id"])}
                    for es in cats.values() for e in sorted(es, key=lambda e: e["title"].lower())]
        out = []
        for cat in sorted(cats, key=lambda c: (c == "System", c.lower())):
            es = sorted(cats[cat], key=lambda e: e["title"].lower())
            out.append({"label": cat, "items": [{"label": e["title"], "action": launch(e["id"])} for e in es]})
        return out

    # ----- window management -----
    def work_area(self):
        return self.theme.work_area(self.W, self.H)

    def open(self, cls, x=None, y=None, w=None, h=None, **kw):
        if isinstance(cls, str):
            return self.launch(cls, x, y, w, h, **kw)
        app = cls(self, **kw)
        ax, ay, aw, ah = self.work_area()
        n = len(self.windows)
        fw, fh = self.theme.frame_size(*app.size)
        w, h = w or fw, h or fh
        if x is None:      # cascade, but keep new windows fully on screen
            x = ax + (30 + n * 24) % max(1, aw // 2)
            x = max(ax, min(x, ax + aw - w))
        if y is None:
            y = ay + (20 + n * 20) % max(1, ah // 2)
            y = max(ay, min(y, ay + ah - h))
        win = Window(app, 0, 0, w, h)
        self.set_geometry(win, x, y, w, h)
        self.windows.append(win)
        self.close_menus()
        self.dirty = True
        self.bus.publish("window.opened", window_id=win.id, app_id=app.app_id or getattr(cls, "app_id", None),
                         title=app.title)
        return win

    def close(self, win):
        if win not in self.windows:
            return
        win.app.close()
        self.windows.remove(win)
        if self.capture is win:
            self.capture = None
        if self.drag and self.drag[0] is win:
            self.drag = None
        if self.resize and self.resize[0] is win:
            self.resize = None
        self.dirty = True
        self.bus.publish("window.closed", window_id=win.id, app_id=win.app.app_id, title=win.title)

    def focus(self, win):
        if self.windows[-1] is not win:
            self.windows.remove(win)
            self.windows.append(win)
            self.bus.publish("window.focused", window_id=win.id, app_id=win.app.app_id)
        self.dirty = True

    def stack(self):
        """Windows bottom to top as drawn: always-on-top windows last."""
        return [w for w in self.windows if not w.on_top] + [w for w in self.windows if w.on_top]

    def find(self, wid):
        for w in self.windows:
            if w.id == int(wid):
                return w
        raise KeyError(f"no window with id {wid}")

    def find_app(self, cls, wid=None, create=True, **kw):
        """The given window, else the topmost window of that app, else a new one."""
        if wid is not None:
            win = self.find(wid)
            if not isinstance(win.app, cls):
                raise ValueError(f"window {wid} is a {type(win.app).__name__}, not a {cls.__name__}")
            return win
        for win in reversed(self.windows):
            if isinstance(win.app, cls):
                return win
        if not create:
            raise KeyError(f"no {cls.__name__} window open")
        if getattr(cls, "app_id", None) in self.registry.entries:
            return self.launch(cls.app_id, **kw)
        return self.open(cls, **kw)

    def set_geometry(self, win, x, y, w, h):
        ax, ay, aw, ah = self.work_area()
        th = self.theme.title_h
        w = int(max(MIN_W, min(aw, w)))
        h = int(max(th + 40, min(ah, h)))
        win.x = int(max(ax - (w - 40), min(ax + aw - 40, x)))
        win.y = int(max(ay, min(ay + ah - th, y)))
        win.w, win.h = w, h
        if hasattr(win.app, "layout_cols"):
            win.app.layout_cols = None         # re-wrap text for the new width
        self.dirty = True

    def maximize(self, win, on=None):
        on = win.saved is None if on is None else on
        win.shaded = False
        if on and win.saved is None:
            win.saved = (win.x, win.y, win.w, win.h)
            self.set_geometry(win, *self.work_area())
        elif not on and win.saved is not None:
            self.set_geometry(win, *win.saved)
            win.saved = None

    def shade(self, win, on=None):
        win.shaded = not win.shaded if on is None else bool(on)
        if win.shaded and self.capture is win:
            self.capture = None
        self.dirty = True

    def layout(self, name, ids=None, gap=0):
        """Arrange windows: cascade, tile (columns), rows, grid, main-left, main-top, maximize."""
        wins = [self.find(i) for i in ids] if ids else list(self.windows)
        if not wins:
            return []
        for win in wins:
            win.saved = None
            win.shaded = False
        ax, ay, W, H = self.work_area()
        n = len(wins)
        g = int(gap)
        th = self.theme.title_h
        boxes = []
        if name == "cascade":
            for i, win in enumerate(wins):
                boxes.append((20 + i * 24, 10 + i * 22, min(W - 60, win.app.size[0] + 4),
                              min(H - 40, win.app.size[1] + th + 4)))
        elif name in ("tile", "columns"):
            cw = W // n
            boxes = [(i * cw + g, g, cw - 2 * g, H - 2 * g) for i in range(n)]
        elif name == "rows":
            rh = H // n
            boxes = [(g, i * rh + g, W - 2 * g, rh - 2 * g) for i in range(n)]
        elif name == "grid":
            cols = math.ceil(math.sqrt(n))
            rows = math.ceil(n / cols)
            cw, rh = W // cols, H // rows
            boxes = [((i % cols) * cw + g, (i // cols) * rh + g, cw - 2 * g, rh - 2 * g) for i in range(n)]
        elif name in ("main-left", "main-top"):
            if n == 1:
                boxes = [(0, 0, W, H)]
            elif name == "main-left":
                mw, rh = W * 3 // 5, H // (n - 1)
                boxes = [(g, g, mw - 2 * g, H - 2 * g)] + \
                        [(mw + g, i * rh + g, W - mw - 2 * g, rh - 2 * g) for i in range(n - 1)]
            else:
                mh, cw = H * 3 // 5, W // (n - 1)
                boxes = [(g, g, W - 2 * g, mh - 2 * g)] + \
                        [(i * cw + g, mh + g, cw - 2 * g, H - mh - 2 * g) for i in range(n - 1)]
        elif name == "maximize":
            boxes = [(0, 0, W, H)] * n
        else:
            raise ValueError("layout must be one of " + ", ".join(LAYOUTS))
        for win, (bx, by, bw, bh) in zip(wins, boxes):
            self.set_geometry(win, ax + bx, ay + by, bw, bh)
        # keep the first listed window on top for "main" layouts, otherwise preserve order
        if name.startswith("main") or name == "maximize":
            self.focus(wins[0])
        return [win.id for win in wins]

    def notify(self, text, seconds=4.0):
        self.toasts.append((str(text)[:200], time.monotonic() + float(seconds)))
        del self.toasts[:-4]
        self.dirty = True

    def client_size(self, app):
        for w in self.windows:
            if w.app is app:
                return w.client()[2:]
        return app.size

    def clock_text(self):
        return themes.clock_text(self.settings["clock_24h"])

    # ----- settings, theme, background -----
    def set_theme(self, name):
        old = self.theme.name
        self.theme = themes.activate(name)
        if self.settings["theme"] != name:
            self.settings.set("theme", name)
        for win in self.windows:
            if win.saved is not None:
                win.saved = None
                self.set_geometry(win, *self.work_area())
                win.saved = (win.x, win.y, win.w, win.h)
            else:
                self.set_geometry(win, win.x, win.y, win.w, win.h)
        self.close_menus()
        self.bg = None
        self.wall_key = None
        self._load_wallpaper()
        self._apply_device()
        self.dirty = True
        if old != name:
            self.bus.publish("theme.changed", theme=name)

    def apply_settings(self, values):
        """Merge settings (nested dict) and apply what changed."""
        before = copy.deepcopy(self.settings.data)
        self.settings.update(values)
        after = self.settings.data
        if after["theme"] != self.theme.name:
            self.set_theme(after["theme"])
        if after["desktop"] != before["desktop"]:
            parse_color(after["desktop"].get("background"), C.teal)    # validates
            self.bg = None
            self._load_wallpaper()
        self.dirty = True
        self.bus.publish("settings.changed", keys=sorted(values))
        return self.settings.public()

    def set_style(self, background=None, pattern=None, caption=None):
        """The old desktop_style call: a shortcut for the desktop settings."""
        d = {}
        if background is not None:
            parse_color(background)
            d["background"] = background
        if pattern is not None:
            d["pattern"] = bool(pattern)
        if caption is not None:
            d["caption"] = str(caption)
        if d:
            self.apply_settings({"desktop": d})

    @property
    def style(self):
        ds = self.settings["desktop"]
        return {"background": ds.get("background") or self.theme.desktop["background"],
                "pattern": ds.get("pattern", True), "caption": ds.get("caption", ""),
                "wallpaper": ds.get("wallpaper", ""), "wallpaper_mode": ds.get("wallpaper_mode", "fill"),
                "icons": ds.get("icons", True), "theme": self.theme.name}

    def _load_wallpaper(self):
        st = self.style
        src, mode = st["wallpaper"], st["wallpaper_mode"]
        area = self.work_area()
        key = (src, mode, area)
        if key == self.wall_key:
            return
        self.wall_key = key
        if not src:
            self.wall = None
            self.bg = None
            return

        def work():
            try:
                im = images.load(src)
                _, _, aw, ah = area
                if mode == "tile":
                    tile = images.to_palette(im if max(im.size) <= 256 else images.fit(im, 256, 256))
                    out = Image.new("RGB", (aw, ah))
                    for tx in range(0, aw, tile.width):
                        for ty in range(0, ah, tile.height):
                            out.paste(tile, (tx, ty))
                elif mode == "fill":
                    s = max(aw / im.width, ah / im.height)
                    big = im.resize((max(1, round(im.width * s)), max(1, round(im.height * s))), Image.LANCZOS)
                    l, t = (big.width - aw) // 2, (big.height - ah) // 2
                    out = images.to_palette(big.crop((l, t, l + aw, t + ah)))
                else:   # fit / center: on the background color
                    out = images.to_palette(images.fit(im, aw, ah, upscale=mode == "fit"))
            except Exception as e:
                out = None
                err = f"wallpaper: {e}"
                self.post(lambda d: d.notify(err))

            def done(d):
                if d.wall_key == key:
                    d.wall, d.bg = out, None
            self.post(done)
        threading.Thread(target=work, daemon=True).start()

    # ----- clipboard and hotkeys -----
    def set_clipboard(self, text, source="user"):
        self.clipboard = str(text)
        self.bus.publish("clipboard.changed", length=len(self.clipboard), source=source)

    def copy_from(self, win):
        text = win.app.copy()
        if text:
            self.set_clipboard(text)
            self.notify(f"Copied {len(text)} characters", 1.5)

    def paste_into(self, win):
        if self.clipboard:
            win.app.paste(self.clipboard)
            self.dirty = True

    # ----- menus -----
    def open_menu(self, items, x, y, source=None, on_pick=None, up=False):
        self.close_menus()
        self.menus = [Menu(items, x, y, self.theme, (self.W, self.H), on_pick, source, up)]
        self.menu_opened = time.monotonic()
        self.dirty = True
        return self.menus[0]

    def close_menus(self):
        if self.menus:
            root, self.menus = self.menus[0], []
            self.dirty = True
            if root.on_close and not root.picked:
                root.on_close()

    def menu_from(self, source):
        return bool(self.menus) and self.menus[0].source == source

    def _open_submenu(self, m, i):
        while self.menus and self.menus[-1] is not m:
            self.menus.pop()
        it = m.items[i]
        if it.get("items") is None or not m.selectable(i):
            return
        rx, ry, rw, rh = m.row_rect(i)
        sub = Menu(it["items"], rx + rw + 2, ry - 3, self.theme, (self.W, self.H), source=m.source, parent=m)
        if sub.rect[0] < rx + rw - 4:        # no room on the right: open to the left
            sub.rect = (max(0, m.rect[0] - sub.rect[2] + 2),) + sub.rect[1:]
        self.menus.append(sub)

    def _pick(self, m, i):
        it = m.items[i]
        if not m.selectable(i):
            return
        if it.get("items") is not None:
            self._open_submenu(m, i)
            m.hover = i
            self.dirty = True
            return
        root = m.root()
        root.picked = True
        self.close_menus()
        if callable(it.get("action")):
            it["action"]()
        elif root.on_pick:
            root.on_pick(it)
        self.dirty = True

    def start_menu_items(self):
        items = self.apps_menu_items()
        items += [{"separator": True},
                  {"label": "Settings...", "action": lambda: self.launch("settings")},
                  {"label": "Files", "action": lambda: self.launch("files")},
                  {"label": "Assistant", "action": lambda: self.launch("assistant")},
                  {"separator": True},
                  {"label": "Arrange", "items": self.arrange_items()},
                  {"label": "Theme", "items": self.theme_items()}]
        return items

    def system_menu_items(self):
        return [{"label": "About This Computer", "action": lambda: self.launch("about")},
                {"separator": True}] + self.apps_menu_items() + \
               [{"separator": True},
                {"label": "Control Panels", "items": [
                    {"label": "Settings...", "action": lambda: self.launch("settings")},
                    {"label": "Theme", "items": self.theme_items()}]},
                {"label": "Recent Windows", "items": self.window_list_items() or [
                    {"label": "(none)", "enabled": False}]}]

    def theme_items(self):
        return [{"label": cls.label, "checked": self.theme.name == name,
                 "action": (lambda n=name: self.set_theme(n))} for name, cls in themes.THEMES.items()]

    def arrange_items(self):
        return [{"label": name.replace("-", " ").title(), "action": (lambda n=name: self.layout(n))}
                for name in LAYOUTS]

    def window_list_items(self):
        top = self.windows[-1] if self.windows else None
        return [{"label": w.title, "checked": w is top,
                 "action": (lambda w=w: (setattr(w, "shaded", False), self.focus(w)))}
                for w in reversed(self.windows)]

    def window_menu_items(self, win):
        return [{"label": "Restore" if win.saved else "Maximize", "action": lambda: self.maximize(win)},
                {"label": "Roll Down" if win.shaded else "Roll Up", "action": lambda: self.shade(win)},
                {"label": "Always on Top", "checked": win.on_top,
                 "action": lambda: setattr(win, "on_top", not win.on_top)},
                {"separator": True},
                {"label": "Copy", "action": lambda: self.copy_from(win)},
                {"label": "Paste", "enabled": bool(self.clipboard), "action": lambda: self.paste_into(win)},
                {"separator": True},
                {"label": "Close", "shortcut": "Alt+F4", "action": lambda: self.close(win)}]

    def desktop_menu_items(self):
        icons = self.style["icons"]
        return [{"label": "New", "items": self.apps_menu_items(flat=True)},
                {"label": "Arrange Windows", "items": self.arrange_items()},
                {"label": "Clean Up Icons", "action": self.clean_up_icons, "enabled": icons},
                {"separator": True},
                {"label": "Paste as Note", "enabled": bool(self.clipboard),
                 "action": lambda: self.launch("notes").app.set_text(self.clipboard, append=True)},
                {"label": "Show Desktop Icons", "checked": icons,
                 "action": lambda: self.apply_settings({"desktop": {"icons": not icons}})},
                {"label": "Theme", "items": self.theme_items()},
                {"separator": True},
                {"label": "Desktop Settings...", "action": lambda: self.launch("settings", page="desktop")}]

    def app_menus(self, win):
        """Menu-bar menus for a window: File, Edit, the app's own menus, then Window."""
        app_id = win.app.app_id
        file_ = [{"label": "New Window", "enabled": bool(app_id),
                  "action": lambda: self.launch(app_id) if app_id else None},
                 {"label": "Close Window", "shortcut": "Alt+F4", "action": lambda: self.close(win)}]
        edit = [{"label": "Copy", "shortcut": "Ctrl+Shift+C", "action": lambda: self.copy_from(win)},
                {"label": "Paste", "shortcut": "Ctrl+Shift+V", "enabled": bool(self.clipboard),
                 "action": lambda: self.paste_into(win)}]
        out = [("File", file_), ("Edit", edit)]
        for title, items in win.app.menus() or []:
            out.append((title, [dict(it, action=(lambda i=it: win.app.on_menu(i.get("id"))))
                                if it.get("items") is None and not it.get("separator") and "action" not in it
                                else it for it in items]))
        window = self.window_list_items() + [{"separator": True}, {"label": "Arrange", "items": self.arrange_items()},
                                             {"label": "Roll Up/Down", "action": lambda: self.shade(win)}]
        out.append(("Window", window))
        return out

    def bar_menu_items(self, which):
        if which in ("start", "system"):
            return self.start_menu_items() if which == "start" else self.system_menu_items()
        if which == "switcher":
            return self.window_list_items() or [{"label": "(no windows)", "enabled": False}]
        if isinstance(which, tuple) and which[0] == "app" and self.windows:
            for title, items in self.app_menus(self.windows[-1]):
                if title == which[1]:
                    return items
        return []

    def open_bar_menu(self, it):
        x, y, w, h = it["rect"]
        which = "start" if it["kind"] == "start" else it["menu"]
        if self.menu_from(which):
            self.close_menus()
            return
        if self.theme.bar_top:
            self.open_menu(self.bar_menu_items(which), x, y + h, source=which)
        else:
            self.open_menu(self.bar_menu_items(which), x, y - 2, source=which, up=True)

    def toggle_main_menu(self):
        which = "system" if self.theme.bar_top else "start"
        if self.menu_from(which):
            self.close_menus()
            return
        for it in self.theme.bar_items(self):
            if it["kind"] == "start" or it.get("menu") == "system":
                self.open_bar_menu(it)
                return

    # ----- desktop icons -----
    def icon_entries(self):
        if not self.style["icons"]:
            return []
        hidden = self.settings["hidden_apps"]
        pinned = self.settings["icons"]
        return [e for e in self.registry.visible(hidden) if e.get("desktop") or e["id"] in pinned]

    def icon_slots(self):
        ax, ay, aw, ah = self.work_area()
        rows = max(1, (ah - 8) // (ICON_H + 10))
        pos = self.settings["icons"]
        out, used = [], 0
        for e in self.icon_entries():
            p = pos.get(e["id"])
            if p and isinstance(p, list) and len(p) == 2:
                x, y = int(p[0]), int(p[1])
            else:
                col, row = divmod(used, rows)
                used += 1
                x = ax + aw - 8 - ICON_W - col * (ICON_W + 20) if self.theme.bar_top else ax + 8 + col * (ICON_W + 20)
                y = ay + 8 + row * (ICON_H + 10)
            out.append((e, x, y))
        return out

    def icon_at(self, px, py):
        for e, x, y in reversed(self.icon_slots()):
            if x <= px < x + ICON_W and y <= py < y + ICON_H:
                return e, x, y
        return None

    def clean_up_icons(self):
        """Forget dragged positions so icons line up in the default grid again."""
        self.settings.set("icons", {k: None for k in self.settings["icons"]})
        self.dirty = True

    def pin_icon(self, app_id, on=True):
        icons = dict(self.settings["icons"])
        e = self.registry.get(app_id)
        if on:
            icons.setdefault(app_id, None)
        else:
            icons.pop(app_id, None)
            if e.get("desktop"):
                hidden = list(self.settings["hidden_apps"])
                if app_id not in hidden:
                    hidden.append(app_id)
                self.settings.set("hidden_apps", hidden, save=False)
        self.settings.set("icons", icons)
        self.dirty = True

    def icon_menu_items(self, e):
        return [{"label": "Open", "action": lambda: self.launch(e["id"])},
                {"separator": True},
                {"label": "Remove from Desktop", "action": lambda: self.pin_icon(e["id"], False)}]

    # ----- dialogs -----
    def dialog(self, **kw):
        """Open an always-on-top dialog window (see dialogs.Dialog) centered in the work area."""
        win = self.open(Dialog, **kw)
        ax, ay, aw, ah = self.work_area()
        self.set_geometry(win, ax + (aw - win.w) // 2, ay + (ah - win.h) // 3, win.w, win.h)
        win.on_top = True
        win.app.app_id = "dialog"
        self.focus(win)
        return win

    # ----- state -----
    def state(self):
        top = self.windows[-1] if self.windows else None
        bx, by, bw, bh = self.theme.bar_rect(self.W, self.H)
        return {"screen": {"w": self.W, "h": self.H, "work_area": list(self.work_area()),
                           "bar": {"x": bx, "y": by, "w": bw, "h": bh, "position": "top" if self.theme.bar_top
                                   else "bottom"}, "title_h": self.theme.title_h},
                "theme": self.theme.name,
                "board": {"connected": self.connected, "name": self.dev.info.name, "host": self.dev.host,
                          "frames_sent": self.frames, "bytes_sent": self.screen.bytes_sent},
                "mouse": {"x": self.mx, "y": self.my, "buttons": self.buttons},
                "menus": [m.public() for m in self.menus],
                "apps": sorted(self.registry.entries),
                "style": self.style,
                "clipboard_chars": len(self.clipboard),
                "hotkeys": sorted(self.hotkeys),
                "icons": [{"app_id": e["id"], "x": x, "y": y} for e, x, y in self.icon_slots()],
                "event_seq": self.bus.last_seq(),
                "plugin_errors": self.registry.errors,
                "windows": [w.info(w is top) for w in reversed(self.windows)]}   # focused first

    def _webhooks_for(self, ev):
        urls = list(self.settings["webhooks"])
        app_id = ev.get("app_id")
        if app_id:
            e = self.registry.entries.get(app_id)
            if e and e.get("webhook"):
                urls.append(e["webhook"])
        return urls

    # ----- input -----
    def on_key(self, k):
        if self.menus and k.down and self._menu_key(k):
            return
        if k.down and not k.name.startswith(("LCTRL", "RCTRL", "LALT", "RALT", "LSHIFT", "RSHIFT", "LGUI", "RGUI")):
            hk = self.hotkeys.get(combo_name(k.name, k.mods))
            if hk:
                self.bus.publish("hotkey", id=hk["id"], combo=hk["combo"], owner=hk.get("owner"))
                return
        if k.down and (k.name in ("LGUI", "RGUI") or (k.ctrl and k.name == "ESCAPE")):
            self.toggle_main_menu()
            return
        if k.down and k.alt and k.name == "F4":
            if self.windows:
                self.close(self.windows[-1])
            return
        if k.down and k.alt and k.name == "TAB":
            if len(self.windows) > 1:
                self.focus(self.windows[0])
            return
        if k.down and k.ctrl and k.shift and k.name.lower() in ("c", "v") and self.windows:
            (self.copy_from if k.name.lower() == "c" else self.paste_into)(self.windows[-1])
            return
        if self.windows and not self.windows[-1].shaded:
            self.windows[-1].app.key(k)
            self.dirty = True

    def _menu_key(self, k):
        m = self.menus[-1]
        if k.name == "ESCAPE":
            if len(self.menus) > 1:
                self.menus.pop()
            else:
                self.close_menus()
        elif k.name in ("UP", "DOWN"):
            m.step(-1 if k.name == "UP" else 1)
        elif k.name == "RIGHT" and 0 <= m.hover < len(m.items) and m.items[m.hover].get("items") is not None:
            self._open_submenu(m, m.hover)
            self.menus[-1].step(1)
        elif k.name == "LEFT" and len(self.menus) > 1:
            self.menus.pop()
        elif k.name in ("RETURN", "SPACE") and m.hover >= 0:
            self._pick(m, m.hover)
            if len(self.menus) and self.menus[-1] is not m:
                self.menus[-1].step(1)
        else:
            return False
        self.dirty = True
        return True

    def _menu_mouse(self, x, y, pressed, released):
        """Mouse while menus are open. Returns True if it was consumed."""
        hit = next((mm for mm in reversed(self.menus) if mm.contains(x, y)), None)
        if hit is not None:
            i = hit.item_at(x, y)
            if i != hit.hover:
                hit.hover = i
                if i >= 0 and hit.items[i].get("items") is not None:
                    self._open_submenu(hit, i)
                elif hit is not self.menus[-1]:
                    while self.menus[-1] is not hit:
                        self.menus.pop()
                self.dirty = True
            # click-click, or press on the bar then release on an item (hold-and-drag)
            if i >= 0 and (pressed & 3 or (released & 3 and time.monotonic() - self.menu_opened > 0.3)):
                if not (released and hit.items[i].get("items") is not None):
                    self._pick(hit, i)
            return True
        if pressed:
            for it in self.theme.bar_items(self):
                if it["kind"] in ("start", "menu") and _inside(it["rect"], x, y):
                    self.open_bar_menu(it)
                    return True
            self.close_menus()
            return True
        # hovering over another bar menu title while one is open switches to it (menu-bar feel)
        if self.theme.bar_top and isinstance(self.menus[0].source, (str, tuple)):
            for it in self.theme.bar_items(self):
                if it["kind"] == "menu" and _inside(it["rect"], x, y) and not self.menu_from(it["menu"]):
                    self.open_bar_menu(it)
                    self.menu_opened = 0.0
                    return True
        return bool(released)

    def on_mouse(self, m):
        x, y = m.x, m.y
        pressed, released = m.buttons & ~self.buttons, self.buttons & ~m.buttons
        self.mx, self.my, self.buttons = x, y, m.buttons

        if self.menus and not self.drag and not self.resize and \
                self._menu_mouse(x, y, pressed, released):
            return
        if self.drag:
            win, dx, dy = self.drag
            if m.buttons & 1:
                if (x - dx, y - dy) != (win.x, win.y):
                    win.saved = None
                    self.set_geometry(win, x - dx, y - dy, win.w, win.h)
                    self.last_click = (None, 0, 0, 0)   # a drag is never half a double-click
            else:
                self.drag = None
            return
        if self.resize:
            win, x0, y0, w0, h0 = self.resize
            if m.buttons & 1:
                win.saved = None
                self.set_geometry(win, win.x, win.y, w0 + x - x0, h0 + y - y0)
            else:
                self.resize = None
            return
        if self.icon_drag:
            app_id, dx, dy = self.icon_drag
            if m.buttons & 1:
                icons = self.settings["icons"]
                ax, ay, aw, ah = self.work_area()
                icons[app_id] = [max(ax, min(ax + aw - ICON_W, x - dx)), max(ay, min(ay + ah - ICON_H, y - dy))]
                self.dirty = True
            else:
                self.icon_drag = None
                self.settings.save()
            return

        if self.capture and (m.buttons or released):
            self._to_app(self.capture, x, y, m.buttons, pressed, released, m.wheel)
            if not m.buttons:
                self.capture = None
            return

        if pressed:
            self._press(x, y, m, pressed, released)
        elif m.wheel:
            for w in reversed(self.stack()):
                if w.contains(x, y):
                    if not w.shaded:
                        self._to_app(w, x, y, m.buttons, 0, 0, m.wheel)
                    break

    def _double(self, thing, x, y):
        """Record a click on `thing`; True if it completes a double-click."""
        last, t, lx, ly = self.last_click
        now = time.monotonic()
        if last is thing and now - t < DOUBLE_CLICK and abs(x - lx) <= DOUBLE_CLICK_SLOP \
                and abs(y - ly) <= DOUBLE_CLICK_SLOP:
            self.last_click = (None, 0, 0, 0)
            return True
        self.last_click = (thing, now, x, y)
        return False

    def _press(self, x, y, m, pressed, released):
        if _inside(self.theme.bar_rect(self.W, self.H), x, y):
            for it in self.theme.bar_items(self):
                if not _inside(it["rect"], x, y):
                    continue
                if it["kind"] == "task":
                    if pressed & 2:
                        self.open_menu(self.window_menu_items(it["win"]), x, y, source="window", up=True)
                    else:
                        it["win"].shaded = False
                        self.focus(it["win"])
                elif it["kind"] in ("start", "menu"):
                    self.open_bar_menu(it)
                    self.menu_opened = time.monotonic()
                self.dirty = True
                return
            return
        for w in reversed(self.stack()):
            if not w.contains(x, y):
                continue
            self.focus(w)
            boxes = self.theme.boxes(w)
            in_title = self.theme.in_title(w, x, y)
            if pressed & 2 and in_title:
                self.open_menu(self.window_menu_items(w), x, y, source="window")
            elif pressed & 1 and _inside(boxes["close"], x, y):
                self.close(w)
            elif pressed & 1 and _inside(boxes["zoom"], x, y):
                self.maximize(w)
            elif pressed & 1 and _inside(boxes["shade"], x, y):
                self.shade(w)
            elif pressed & 1 and self.theme.grip(w, x, y):
                self.resize = (w, x, y, w.w, w.h)
            elif in_title or w.shaded:
                if pressed & 1:
                    if self._double(w, x, y):
                        if self.theme.shade_on_double_click:
                            self.shade(w)
                        else:
                            self.maximize(w)
                    else:
                        self.drag = (w, x - w.x, y - w.y)
            else:
                cx, cy, _, _ = w.client()
                if pressed & 2:
                    items = w.app.context_menu(x - cx, y - cy)
                    if items:
                        self.open_menu(items, x, y, source="context",
                                       on_pick=lambda it, app=w.app: app.on_menu(it.get("id")))
                        return
                self.capture = w
                self._to_app(w, x, y, m.buttons, pressed, released, m.wheel)
            return
        # the desktop itself
        hit = self.icon_at(x, y)
        if hit:
            e, ix, iy = hit
            self.icon_sel = e["id"]
            if pressed & 2:
                self.open_menu(self.icon_menu_items(e), x, y, source="icon")
            elif self._double(e["id"], x, y):
                self.launch(e["id"])
            else:
                self.icon_drag = (e["id"], x - ix, y - iy)
        else:
            self.icon_sel = None
            if pressed & 2:
                self.open_menu(self.desktop_menu_items(), x, y, source="desktop")
        self.dirty = True

    def _to_app(self, w, x, y, buttons, pressed, released, wheel):
        cx, cy, _, _ = w.client()
        w.app.mouse(x - cx, y - cy, buttons, pressed, released, wheel)
        self.dirty = True

    def _accept_mouse(self, m):
        now = time.monotonic()
        if m.web:
            # Move the board's own cursor along so the VGA screen shows where the browser points.
            if (m.x, m.y) != (self.mx, self.my):
                self.dev.warp(m.x, m.y)
                self.web_warps.append((m.x, m.y, now))
            return True
        # After each warp the board reports that position back with its own (released) buttons.
        # During a fast drag several warps are in flight, so match against all recent ones;
        # letting an echo through reads as "button released" and ends the drag.
        while self.web_warps and now - self.web_warps[0][2] > ECHO_WINDOW:
            self.web_warps.popleft()
        return not any((m.x, m.y) == (wx, wy) for wx, wy, _ in self.web_warps)

    # ----- rendering -----
    def _background(self):
        st = self.style
        base = parse_color(st["background"], C.teal)
        bg = Image.new("RGB", (self.W, self.H), base)
        bd = ImageDraw.Draw(bg)
        bd.fontmode = "1"
        area = self.work_area()
        ax, ay, aw, ah = area
        text_col = self.theme.colors["desktop_text"]
        if self.wall is not None:
            bg.paste(self.wall, (ax + (aw - self.wall.width) // 2, ay + (ah - self.wall.height) // 2))
        elif st["pattern"]:
            text_col = self.theme.background_pattern(bd, self.W, self.H, base, area)
        if st["caption"]:
            bd.text((ax + 10, ay + ah - 22), st["caption"], font=FONT, fill=text_col)
        return bg

    def render(self):
        if self.bg is None:
            self.bg = self._background()
        self.img.paste(self.bg)
        d = ImageDraw.Draw(self.img)
        d.fontmode = "1"
        th = self.theme

        for e, x, y in self.icon_slots():
            th.draw_icon(d, x, y, e["title"], e.get("icon", "app"), e["id"] == self.icon_sel)

        top = self.windows[-1] if self.windows else None
        for w in self.stack():
            th.draw_window(d, w, w is top)
            if w.shaded:
                continue
            x, y, cw, ch = w.client()
            if cw > 0 and ch > 0:
                # Apps draw into their own image so they can't scribble outside the window.
                self.target = Image.new("RGB", (cw, ch), C.gray)
                sd = ImageDraw.Draw(self.target)
                sd.fontmode = "1"
                try:
                    w.app.draw(sd, 0, 0, cw, ch)
                except Exception as e:
                    sd.rectangle([0, 0, cw, CH + 4], fill=C.white)
                    sd.text((4, 2), f"error: {e}"[: max(1, cw // CW)], font=FONT, fill=C.red)
                self.img.paste(self.target, (x, y))
            th.draw_grip(d, w)

        self.clock = self.clock_text()
        th.draw_bar(self, d, th.bar_items(self))

        for m in self.menus:
            th.draw_menu(d, m)

        ty = self.work_area()[1] + 6
        for text, _ in self.toasts:
            tw = min(self.W - 20, text_size(text)[0] + 16)
            tx = self.W - tw - 8
            bevel(d, tx, ty, tw, CH + 8, face=C.yellow)
            d.text((tx + 8, ty + 4), text[:max(1, (tw - 16) // CW)], font=FONT, fill=C.black)
            ty += CH + 12

    def frame(self):
        """Render and push a frame now if anything changed."""
        if self.dirty:
            self.dirty = False
            self.render()
            self.screen.present_rgb(self.img)
            self.frames += 1
            if self.web:
                self.web.publish(self.screen.sent, self.connected)

    # ----- main loop -----
    def handle(self, ev):
        if ev is None:
            if self.dev.closed:               # the board went away
                self.detach()
        elif isinstance(ev, Attach):
            self.attach(ev.dev)
        elif isinstance(ev, Call):
            try:
                ev.future.set_result(ev.fn(self))
            except Exception as e:
                ev.future.set_exception(e)
            self.dirty = True
        elif isinstance(ev, Key):
            self.on_key(ev)
        elif isinstance(ev, Mouse) and self._accept_mouse(ev):
            if self.menus:
                self.dirty = True       # hover highlight
            self.on_mouse(ev)

    def run(self):
        last_tick = 0
        while True:
            try:
                ev = self.inbox.get(timeout=0.05)
            except queue.Empty:
                ev = False
            while ev is not False:
                self.handle(ev)
                try:
                    ev = self.inbox.get_nowait()
                except queue.Empty:
                    ev = False
            if time.monotonic() - last_tick > 0.2:
                last_tick = time.monotonic()
                for w in self.windows:
                    if w.app.tick():
                        self.dirty = True
                if self.clock_text() != self.clock:
                    self.dirty = True
                now = time.monotonic()
                if any(t < now for _, t in self.toasts):
                    self.toasts = [(s, t) for s, t in self.toasts if t >= now]
                    self.dirty = True
            try:
                self.frame()
            except Exception as e:     # a board dropping mid-frame shouldn't kill the desktop
                if self.dev.closed:
                    self.detach()
                else:
                    raise

    def shutdown(self):
        for w in self.windows:
            try:
                w.app.close()
            except Exception:
                pass
        from .webview import Engine
        Engine.shutdown()
