"""GameApp: a base class for real-time apps (games, animations, toys) that run on the desktop.

The game loop runs here on the host at `fps`, so keys and frames don't round-trip through an
agent. A game only writes update() and render():

    from vga32.game import GameApp

    class Snake(GameApp):
        app_id = "games.snake"
        title = "Snake"
        icon = "star"
        category = "Games"
        width, height = 160, 100          # logical canvas, scaled up to fit the window
        fps = 15

        def start(self):                  # called once, and again by restart()
            self.x, self.y = 80, 50

        def update(self, dt):             # dt = seconds since the last update
            if self.held("left"):  self.x -= 1
            if self.pressed("space"): self.restart()

        def render(self, g):              # g is a Canvas (see below)
            g.clear("black")
            g.rect(self.x, self.y, 4, 4, "lime")
            g.text(2, 2, f"score {self.score}", "white")

Keys are lowercase FabGL names: "left" "right" "up" "down" "space" "return" "escape" "a".."z"
"0".."9" "f1".., "lshift" "lctrl" "lalt". held(k) is true while down; pressed(k) / released(k) only
on the update where it changed. self.pointer = (x, y, buttons) in canvas coordinates, clicked(1).

Mouse-look (3D): set mouse_look = True. Clicking the game captures the mouse (cursor hidden, Esc
releases); self.mouse_delta() returns the (dx, dy) moved since the last update. axis("d", "a") gives
+1 / -1 / 0 for a pair of keys. See render3d.py for the 3D engines.

The game pauses while its window isn't focused (pause_unfocused = False to keep running).
Exceptions in start/update/render are caught: the window shows the traceback, state() reports
it and an app.error event is published, so an agent that wrote the game can fix it.
Put anything an agent should see (score, level, lives...) in game_state().
"""
import random
import time
import traceback

import numpy as np

from PIL import Image, ImageDraw

from .palette import C
from .ui import FONT, FONTS, MEDIUM, App, parse_color

_SPRITE_CACHE = {}


def _col(c, default=C.white):
    return parse_color(c, default) if c is not None else default


class Canvas:
    """Drawing helpers over a PIL image; colors are palette names, 0-15, #rrggbb or theme names."""

    def __init__(self, img):
        self.img = img
        self.sample = None
        self.d = ImageDraw.Draw(img)
        self.d.fontmode = "1"
        self.w, self.h = img.size

    def clear(self, color="black"):
        self.d.rectangle([0, 0, self.w - 1, self.h - 1], fill=_col(color, C.black))

    def rect(self, x, y, w, h, color="white", outline=None):
        if w <= 0 or h <= 0:
            return
        self.d.rectangle([x, y, x + w - 1, y + h - 1], fill=_col(color) if color is not None else None,
                         outline=_col(outline) if outline is not None else None)

    def frame(self, x, y, w, h, color="white"):
        self.rect(x, y, w, h, None, color)

    def circle(self, cx, cy, r, color="white", outline=None):
        self.d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=_col(color) if color is not None else None,
                       outline=_col(outline) if outline is not None else None)

    def line(self, x1, y1, x2, y2, color="white", width=1):
        self.d.line([x1, y1, x2, y2], fill=_col(color), width=width)

    def poly(self, points, color="white", outline=None):
        self.d.polygon([tuple(p) for p in points], fill=_col(color) if color is not None else None,
                       outline=_col(outline) if outline is not None else None)

    def pixel(self, x, y, color="white"):
        if 0 <= x < self.w and 0 <= y < self.h:
            self.d.point((x, y), fill=_col(color))

    def text(self, x, y, s, color="white", size="small", center=False):
        font = FONTS.get(size, FONT)
        s = str(s)
        if center:
            x -= int(font.getlength(s)) // 2
        self.d.text((x, y), s, font=font, fill=_col(color))

    def text_width(self, s, size="small"):
        return int(FONTS.get(size, FONT).getlength(str(s)))

    def dither(self, mode="ordered"):
        """Spread full-color drawing (3D shading, gradients) over the 16 colors. ordered = stable between
        frames (cheaper to send), floyd = smoother stills."""
        from .palette import dither as fs, ordered_dither
        # a small full-color copy, from which the desktop picks the best 16 colors (adaptive_palette)
        self.sample = np.asarray(self.img.resize((64, 40), Image.BOX))
        self.img.paste(ordered_dither(self.img) if mode == "ordered" else fs(self.img))

    def blit(self, image, x=0, y=0):
        """Paste a PIL image (e.g. one you rendered yourself)."""
        self.img.paste(image, (int(x), int(y)))

    def sprite(self, art, x, y, colors=None, scale=1, flip=False):
        """Draw ASCII art: a list of equal-length strings; '.' or ' ' is transparent, other chars map
        through colors {char: color} (default: palette initials, e.g. 'r' red, 'w' white, see SPRITE_COLORS)."""
        key = (tuple(art), tuple(sorted((colors or {}).items())), scale, flip)
        im = _SPRITE_CACHE.get(key)
        if im is None:
            cmap = dict(SPRITE_COLORS)
            cmap.update(colors or {})
            h, w = len(art), max(len(r) for r in art)
            im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            for j, row in enumerate(art):
                for i, ch in enumerate(row):
                    if ch not in ". ":
                        im.putpixel((w - 1 - i if flip else i, j), _col(cmap.get(ch, "white")) + (255,))
            if scale != 1:
                im = im.resize((w * scale, h * scale), Image.NEAREST)
            if len(_SPRITE_CACHE) > 512:
                _SPRITE_CACHE.clear()
            _SPRITE_CACHE[key] = im
        self.img.paste(im, (int(x), int(y)), im)


SPRITE_COLORS = {"k": "black", "K": "darkgray", "g": "gray", "n": "navy", "b": "blue", "s": "sky", "t": "teal",
                 "c": "cyan", "G": "green", "l": "lime", "m": "maroon", "r": "red", "o": "orange", "y": "yellow",
                 "p": "purple", "w": "white"}


class GameApp(App):
    title = "Game"
    icon = "star"
    category = "Games"
    width, height = 320, 200        # logical canvas size
    pixel = None                    # e.g. 2: the canvas follows the window size (window / pixel); resized() is called
    fps = 30
    pause_unfocused = True
    mouse_look = False              # click captures the mouse for 3D look; Esc releases
    background = "black"

    def __init__(self, desk, **kw):
        super().__init__(desk)
        if self.pixel:
            self.size = (self.width * self.pixel, self.height * self.pixel)
        else:
            self.size = (self.width * (2 if self.width <= 320 else 1), self.height * (2 if self.height <= 200 else 1))
        self.canvas = Image.new("RGB", (self.width, self.height), _col(self.background, C.black))
        self._held, self._pressed, self._released = set(), set(), set()
        self.pointer = (0, 0, 0)
        self._dx = self._dy = 0
        self._clicks = set()
        self._view = (0, 0, 1)        # offset x, y, scale of the canvas inside the window
        self.frame_no, self.time, self.score = 0, 0.0, 0
        self.paused = False
        self.error = None
        self._last = time.monotonic()
        self.rng = random.Random()
        self._guard(self.start)

    # ----- for games to override -----
    def start(self):
        pass

    def update(self, dt):
        pass

    def render(self, g):
        g.clear(self.background)

    def game_state(self):
        return {}

    def resized(self):
        """The canvas changed size (pixel mode); self.width / self.height are the new size."""
        pass

    def overlay(self, g):
        """Draw at full screen resolution over the scaled canvas (HUD text stays crisp).
        g is a Canvas over the window's client area."""
        pass

    # ----- helpers -----
    def held(self, *keys):
        return any(k in self._held for k in keys)

    def pressed(self, *keys):
        return any(k in self._pressed for k in keys)

    def released(self, *keys):
        return any(k in self._released for k in keys)

    def clicked(self, button=1):
        return button in self._clicks

    def mouse_delta(self):
        """(dx, dy) mouse movement since the last update while the mouse is captured."""
        return self._dx, self._dy

    def axis(self, plus, minus):
        return (1 if self.held(plus) else 0) - (1 if self.held(minus) else 0)

    def mouse_motion(self, dx, dy):
        self._dx += dx
        self._dy += dy

    def restart(self):
        self.frame_no, self.time, self.score = 0, 0.0, 0
        self._guard(self.start)

    def focused(self):
        return bool(self.desk.windows) and self.desk.windows[-1].app is self

    def _guard(self, fn, *a):
        if self.error:
            return
        try:
            fn(*a)
        except Exception:
            self.error = traceback.format_exc(limit=6)
            self.log(self.error)
            bus = getattr(self.desk, "bus", None)
            if bus is not None:
                bus.publish("app.error", app_id=self.app_id, title=self.title, error=self.error[-2000:])

    # ----- App plumbing -----
    def key(self, k):
        name = k.name.lower()
        if k.down:
            if name not in self._held:
                self._pressed.add(name)
            self._held.add(name)
            if self.error and name == "f5":       # F5 retries after an error
                self.error = None
                self.restart()
        else:
            self._held.discard(name)
            self._released.add(name)

    def mouse(self, x, y, buttons, pressed, released, wheel):
        if self.mouse_look and pressed & 1 and not self.mouse_grabbed and not self.error:
            self.grab_mouse()
            return                       # the capturing click doesn't count as a shot
        ox, oy, s = self._view
        self.pointer = ((x - ox) // s, (y - oy) // s, buttons)
        for b in (1, 2, 4):
            if pressed & b:
                self._clicks.add(b)

    def tick(self):
        now = time.monotonic()
        dt, self._last = min(0.1, now - self._last), now
        if self.error:
            return False
        if self.pause_unfocused and not self.focused():
            self._held.clear()
            if not self.paused:
                self.paused = True
                return True
            return False
        self.paused = False
        self._guard(self.update, dt)
        self.frame_no += 1
        self.time += dt
        self._pressed.clear()
        self._released.clear()
        self._clicks.clear()
        self._dx = self._dy = 0
        return True

    def draw(self, d, x, y, w, h):
        d.rectangle([x, y, x + w - 1, y + h - 1], fill=C.black)
        if self.error:
            d.text((x + 6, y + 4), "Game error (F5 retries):", font=FONT, fill=C.red)
            for i, ln in enumerate(self.error.strip().splitlines()[-((h - 30) // 14):]):
                d.text((x + 6, y + 22 + i * 14), ln[: max(1, w // 7)], font=FONT, fill=C.white)
            return
        if self.pixel:
            nw, nh = max(16, w // self.pixel), max(16, h // self.pixel)
            if (nw, nh) != (self.width, self.height):
                self.width, self.height = nw, nh
                self.canvas = Image.new("RGB", (nw, nh), _col(self.background, C.black))
                self._guard(self.resized)
        g = Canvas(self.canvas)
        self._guard(self.render, g)
        if g.sample is not None:
            self.palette_sample = g.sample
        s = max(1, min(w // self.width, h // self.height))
        img = self.canvas if s == 1 else self.canvas.resize((self.width * s, self.height * s), Image.NEAREST)
        ox, oy = x + (w - img.width) // 2, y + (h - img.height) // 2
        self._view = (ox - x, oy - y, s)
        self.desk.target.paste(img, (ox, oy))
        self._guard(self.overlay, Canvas(self.desk.target))
        if self.mouse_look and not self.paused and not self.mouse_grabbed:
            msg = "Click to look around - Esc releases"
            tw = int(FONT.getlength(msg))
            d.rectangle([x + (w - tw) // 2 - 6, y + h - 22, x + (w + tw) // 2 + 6, y + h - 6], fill=C.navy)
            d.text((x + (w - tw) // 2, y + h - 21), msg, font=FONT, fill=C.white)
        if self.paused:
            msg = "Paused - click to play"
            tw = int(MEDIUM.getlength(msg))
            d.rectangle([x + (w - tw) // 2 - 8, y + h // 2 - 14, x + (w + tw) // 2 + 8, y + h // 2 + 14], fill=C.navy)
            d.text((x + (w - tw) // 2, y + h // 2 - 10), msg, font=MEDIUM, fill=C.white)

    def context_menu(self, x, y):
        return [{"id": "restart", "label": "Restart"},
                {"id": "pause", "label": "Keep Running When Unfocused", "checked": not self.pause_unfocused}]

    def on_menu(self, item_id):
        if item_id == "restart":
            self.error = None
            self.restart()
        elif item_id == "pause":
            self.pause_unfocused = not self.pause_unfocused

    def state(self):
        try:
            extra = self.game_state()
        except Exception as e:
            extra = {"game_state_error": str(e)}
        return {"game": True, "canvas": [self.width, self.height], "fps": self.fps, "frame": self.frame_no,
                "mouse_grabbed": self.mouse_grabbed,
                "time": round(self.time, 2), "score": self.score, "paused": self.paused,
                "keys_held": sorted(self._held), "error": self.error, **extra}

