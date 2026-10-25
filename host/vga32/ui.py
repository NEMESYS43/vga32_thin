"""Shared drawing helpers and the App base class."""
import math

import numpy as np
from PIL import ImageFont

from .palette import C, NAMES, PALETTE, rgb_to_index

TITLE_H = 18      # classic theme metrics; the live values come from theme.current()
TASKBAR_H = 24
SEMANTIC = {}     # theme color names (face, text, accent, select...) -> RGB, filled by theme.activate()


def load_font(size=13):
    for name in ("consola.ttf", "DejaVuSansMono.ttf", "LiberationMono-Regular.ttf", "Menlo.ttc", "cour.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            pass
    return ImageFont.load_default()


FONT = load_font(13)
MEDIUM = load_font(18)
BIG = load_font(28)
FONTS = {"small": FONT, "normal": FONT, "medium": MEDIUM, "big": BIG}


def text_size(s, font=FONT):
    l, t, r, b = font.getbbox(s or " ")
    return int(font.getlength(s)), b


CW = max(1, int(FONT.getlength("M")))   # monospace cell
CH = text_size("Mg")[1] + 3


def bevel(d, x, y, w, h, raised=True, face=C.gray):
    d.rectangle([x, y, x + w - 1, y + h - 1], fill=face)
    hi, lo = (C.white, C.darkgray) if raised else (C.darkgray, C.white)
    d.line([x, y + h - 1, x, y, x + w - 1, y], fill=hi)
    d.line([x + w - 1, y, x + w - 1, y + h - 1, x, y + h - 1], fill=lo)


def parse_color(v, default=C.black):
    """Palette name ("red"), index (0-15), "#rrggbb" or [r, g, b] -> nearest palette RGB."""
    if v is None:
        return default
    if isinstance(v, int) and 0 <= v < 16:
        return PALETTE[v]
    if isinstance(v, str):
        s = v.strip().lower()
        if s in NAMES:
            return PALETTE[NAMES.index(s)]
        if s in SEMANTIC:
            return SEMANTIC[s]
        if s.startswith("#") and len(s) in (4, 7):
            if len(s) == 4:
                s = "#" + "".join(ch * 2 for ch in s[1:])
            v = [int(s[i:i + 2], 16) for i in (1, 3, 5)]
        else:
            raise ValueError(f"unknown color {v!r}; use one of {', '.join(NAMES)}, a theme color "
                             f"({', '.join(SEMANTIC)}) or #rrggbb")
    if isinstance(v, (list, tuple)) and len(v) == 3:
        return PALETTE[int(rgb_to_index(np.array([[v]], np.uint8))[0, 0])]
    raise ValueError(f"bad color {v!r}")


GLYPHS = ["app", "terminal", "web", "browser", "image", "notes", "paint", "clock", "system", "about", "panel",
          "settings", "files", "folder", "file", "assistant", "chat", "window", "cloud", "star", "gear", "doc"]


def draw_glyph(d, x, y, name, s=32):
    """A small palette icon in an s x s box. Unknown names draw a generic app icon."""
    k = s / 32
    P = lambda *pts: [(x + px * k, y + py * k) for px, py in zip(pts[::2], pts[1::2])]
    R = lambda x0, y0, x1, y1: [x + x0 * k, y + y0 * k, x + x1 * k - 1, y + y1 * k - 1]
    if name in ("terminal",):
        d.rectangle(R(2, 4, 30, 28), fill=C.black, outline=C.gray)
        d.line(P(7, 11, 12, 15, 7, 19), fill=C.lime, width=max(1, int(2 * k)))
        d.line(P(14, 21, 22, 21), fill=C.lime, width=max(1, int(2 * k)))
    elif name in ("web", "browser", "cloud"):
        if name == "cloud":
            d.ellipse(R(4, 12, 18, 26), fill=C.white, outline=C.darkgray)
            d.ellipse(R(12, 6, 28, 24), fill=C.white, outline=C.darkgray)
            d.rectangle(R(10, 18, 24, 26), fill=C.white)
            d.line(P(10, 25, 24, 25), fill=C.darkgray)
        else:
            d.ellipse(R(3, 3, 29, 29), fill=C.sky if name == "web" else C.cyan, outline=C.navy)
            d.ellipse(R(10, 3, 22, 29), outline=C.navy)
            d.line(P(3, 16, 29, 16), fill=C.navy)
            d.line(P(6, 9, 26, 9), fill=C.navy)
            d.line(P(6, 23, 26, 23), fill=C.navy)
    elif name == "image":
        d.rectangle(R(3, 5, 29, 27), fill=C.sky, outline=C.black)
        d.polygon(P(4, 26, 13, 13, 20, 22, 23, 18, 28, 26), fill=C.green)
        d.ellipse(R(20, 8, 26, 14), fill=C.yellow)
    elif name in ("notes", "doc", "file"):
        d.polygon(P(6, 2, 21, 2, 27, 8, 27, 30, 6, 30), fill=C.white, outline=C.black)
        d.polygon(P(21, 2, 21, 8, 27, 8), fill=C.gray, outline=C.black)
        if name != "file":
            for ly in (12, 16, 20, 24):
                d.line(P(9, ly, 24, ly), fill=C.blue)
    elif name == "paint":
        d.ellipse(R(2, 6, 30, 28), fill=C.white, outline=C.black)
        for (cx, cy), col in zip(((9, 13), (16, 10), (23, 13), (10, 21)), (C.red, C.yellow, C.blue, C.lime)):
            d.ellipse(R(cx - 3, cy - 3, cx + 3, cy + 3), fill=col)
    elif name == "clock":
        d.ellipse(R(3, 3, 29, 29), fill=C.white, outline=C.black, width=max(1, int(2 * k)))
        d.line(P(16, 16, 16, 8), fill=C.black, width=max(1, int(2 * k)))
        d.line(P(16, 16, 22, 19), fill=C.red, width=max(1, int(2 * k)))
    elif name == "system":
        d.rectangle(R(3, 4, 29, 28), fill=C.black, outline=C.gray)
        d.line(P(5, 22, 10, 18, 14, 21, 19, 10, 23, 15, 27, 8), fill=C.lime, width=max(1, int(2 * k)))
    elif name in ("about", "star"):
        d.polygon(P(16, 2, 20, 12, 30, 12, 22, 19, 25, 30, 16, 23, 7, 30, 10, 19, 2, 12, 12, 12),
                  fill=C.yellow, outline=C.black)
    elif name in ("settings", "gear"):
        for a in range(8):
            t = a * math.pi / 4
            d.line(P(16, 16, 16 + 13 * math.cos(t), 16 + 13 * math.sin(t)), fill=C.darkgray, width=max(2, int(5 * k)))
        d.ellipse(R(6, 6, 26, 26), fill=C.gray, outline=C.black)
        d.ellipse(R(12, 12, 20, 20), fill=C.white, outline=C.black)
    elif name in ("files", "folder"):
        d.polygon(P(2, 8, 12, 8, 15, 11, 30, 11, 30, 28, 2, 28), fill=C.yellow, outline=C.black)
        d.line(P(2, 14, 30, 14), fill=C.orange)
    elif name in ("assistant", "chat"):
        d.rounded_rectangle(R(2, 4, 30, 23), radius=max(1, int(5 * k)), fill=C.white, outline=C.black)
        d.polygon(P(8, 22, 8, 30, 15, 22), fill=C.white, outline=C.black)
        d.line(P(9, 22, 14, 22), fill=C.white)
        for cx in (10, 16, 22):
            d.ellipse(R(cx - 2, 12, cx + 2, 16), fill=C.purple if name == "assistant" else C.black)
    elif name == "window":
        d.rectangle(R(2, 4, 30, 28), fill=C.white, outline=C.black)
        d.rectangle(R(2, 4, 30, 10), fill=C.blue, outline=C.black)
    elif name == "panel":
        d.rectangle(R(2, 4, 30, 28), fill=C.navy, outline=C.black)
        d.rectangle(R(6, 9, 26, 13), fill=C.lime)
        d.rectangle(R(6, 17, 18, 21), fill=C.orange)
    else:
        d.rectangle(R(4, 4, 28, 28), fill=C.gray, outline=C.black)
        d.rectangle(R(4, 4, 28, 10), fill=C.blue, outline=C.black)
        d.rectangle(R(9, 14, 23, 24), fill=C.white, outline=C.darkgray)


class App:
    title = "App"
    size = (320, 200)      # client area
    icon = "app"           # a draw_glyph name
    category = "Accessories"
    app_id = None          # set by the registry

    def __init__(self, desk, **kw):
        self.desk = desk

    def menus(self):
        """Extra menu-bar menus for this app: [(title, [item, ...])]. Items as in menu.Menu."""
        return []

    def context_menu(self, x, y):
        """Items for a right-click at client (x, y), or None for no menu."""
        return None

    def on_menu(self, item_id):
        """A menu item from menus() or context_menu() was picked."""
        pass

    def copy(self):
        """Text for Edit > Copy, or None."""
        return None

    def paste(self, text):
        """Edit > Paste. Default: type the text in."""
        from .client import Key
        for ch in text:
            if ch == "\n":
                self.key(Key("RETURN", "", True, 0))
            elif ch >= " ":
                self.key(Key(ch if ch.isalnum() and ch.isascii() else "NONE", ch, True, 0))

    def draw(self, d, x, y, w, h):
        pass

    def key(self, k):
        pass

    def mouse(self, x, y, buttons, pressed, released, wheel):
        pass

    def tick(self):
        """Called a few times a second; return True to request a redraw."""
        return False

    def state(self):
        """App-specific details for the agent API."""
        return {}

    def close(self):
        pass
