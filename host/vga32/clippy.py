import math
from vga32.ui import wrap_text, CH, FONT
import time
from vga32.palette import C, ordered_dither
import numpy as np
from PIL import Image, ImageOps

# Regenerate a clean transparent Clippy sprite from the source image every load.
# This avoids manually stripping out already-rendered eyes/face details.
src_img = Image.open('/root/.hermes/cache/scratch/clippy.jpg').convert('RGB')
mask = src_img.convert('L').point(lambda p: 0 if p > 220 else 255, mode='1')
bbox = mask.getbbox()
crop = src_img.crop(bbox)
_w = 44
_h = int(crop.height * _w / crop.width)
crop = crop.resize((_w, _h), Image.LANCZOS)
alpha = crop.convert('L').point(lambda p: 0 if p > 225 else 255, mode='1')
SPRITE_RGBA = Image.new('RGBA', crop.size)
SPRITE_RGBA.paste(crop)
SPRITE_RGBA.putalpha(alpha)
SPRITE_W, SPRITE_H = SPRITE_RGBA.size
SPRITE_MASK = SPRITE_RGBA.split()[3]
SPRITE_MASK_ARR = np.asarray(SPRITE_MASK)
SPRITE_RGB = SPRITE_RGBA.convert('RGB')
SPRITE_DITHERED = ordered_dither(SPRITE_RGB).convert('RGB')
SPRITE_DITHERED.putalpha(Image.fromarray(SPRITE_MASK_ARR))

GOOD_WORDS = ("great", "done", "success", "cool", "nice", "thanks", "thank", "ok", "yes", "ready", "fixed", "happy", "happiness")
QUESTION_WORDS = ("how", "why", "what", "when", "which", "where", "could", "should", "can")
BAD_WORDS = ("error", "fail", "blocked", "wrong", "sad", "issue", "problem", "sorry", "crash", "broken")
ALERT_WORDS = ("warning", "urgent", "critical", "caution", "stop", "attention")
ANGRY_WORDS = ("angry", "mad", "furious", "rage", "annoy", "frustrat", "hate", "stupid", "unacceptable")
SAD_WORDS = ("sad", "sorry", "grief", "mourn", "unhappy", "down")


def infer_mood(text, mood=None):
    if mood:
        return str(mood)
    t = (text or "").lower()
    if "?" in t or any(w in t.split() for w in QUESTION_WORDS):
        return "thinking"
    if any(w in t for w in ANGRY_WORDS):
        return "angry"
    if any(w in t for w in SAD_WORDS):
        return "sad"
    if any(w in t for w in BAD_WORDS):
        return "worried"
    if any(w in t for w in ALERT_WORDS):
        return "alert"
    if any(w in t for w in GOOD_WORDS):
        return "happy"
    if "!" in t:
        return "excited"
    if any(w in t for w in ("bye", "later", "leave", "move", "out")):
        return "wink"
    return "neutral"



MOOD_STYLES = {
    "neutral":   {"amp": 2.6, "hz": 1.20, "baseline": 0, "outline": C.black,   "marker": None},
    "happy":     {"amp": 5.2, "hz": 1.65, "baseline": -4, "outline": C.green,  "marker": "*"},
    "thinking":  {"amp": 0.7, "hz": 0.55, "baseline": 0, "outline": C.navy,   "marker": "?"},
    "worried":   {"amp": 1.1, "hz": 0.70, "baseline": 2, "outline": C.orange, "marker": "?"},
    "sad":       {"amp": 0.5, "hz": 0.40, "baseline": 6, "outline": C.navy,   "marker": None},
    "angry":     {"amp": 2.3, "hz": 5.30, "baseline": 3, "outline": C.red,    "marker": "!"},
    "alert":     {"amp": 4.4, "hz": 2.10, "baseline": -2, "outline": C.orange,"marker": "!"},
    "excited":   {"amp": 6.6, "hz": 2.35, "baseline": -5, "outline": C.yellow,"marker": "!"},
    "wink":      {"amp": 2.0, "hz": 1.05, "baseline": 0, "outline": C.purple, "marker": None},
}


def mood_style(mood):
    return MOOD_STYLES.get(mood, MOOD_STYLES["neutral"])



class ClippyOverlay:
    def __init__(self, desk):
        self.desk = desk
        self.visible = True
        self.text = "I am Hermes Clippy, your desktop overlay. Talk to me and I react."
        self.mood = "happy"
        self._default = "Hermes Clippy. Talk and I react; Alt+Arrow keys move me out of the way."
        self._last_text = None
        self._last_lines = None
        self.sprite_x = 48
        self.sprite_y = desk.H - SPRITE_H - 74
        self.target_x = self.sprite_x
        self.target_y = self.sprite_y
        self.start_x = self.sprite_x
        self.start_y = self.sprite_y
        self._move_until = None
        self._move_start = None
        self.dragging = False
        self._drag_dx = 0
        self._drag_dy = 0
        self._bob = 0.0
        self._blink = False
        self._baseline = 0
        self._last_interact = time.monotonic()
        self
    def regions(self):
        desk = self.desk
        work = desk.work_area()
        wx, wy, ww, wh = work
        right = wx + ww - SPRITE_W - 12
        bottom = wy + wh - SPRITE_H - 8
        top = wy + 6
        return {
            "top-left": [wx, top],
            "top-right": [right + 0, top + 10],
            "left": [wx, bottom],
            "right": [right, bottom],
            "center": [ww // 2 - SPRITE_W // 2 + wx, wy + wh // 2 - SPRITE_H // 2],
            "bottom-left": [wx, bottom],
            "bottom-right": [right, bottom],
            "bottom": [wx + ww // 2 - SPRITE_W // 2, bottom],
            "assistant": [right, bottom],
            "cursor": [max(wx, min(right, desk.mx - SPRITE_W // 2)),
                       max(wy, min(bottom, desk.my - SPRITE_H // 2))],
        }

    def _lines(self):
        txt = self.text or self._default
        if txt == self._last_text:
            return self._last_lines
        out = wrap_text(txt, FONT, 176)[:7]
        if not out:
            out = [self._default]
        self._last_text = txt
        self._last_lines = out
        return out

    def update(self, text=None, mood=None, x=None, y=None, region=None, animate=True,
               visible=None, duration=0.55, **kw):
        if visible is not None:
            self.visible = bool(visible)
        if text is not None:
            text = str(text).strip()
            self.text = text if text else self._default
        if mood is not None or text is not None:
            self.mood = infer_mood(self.text, mood)
        rx, ry = None, None
        regions = self.regions()
        if region is not None:
            key = str(region).lower().strip()
            if key in ("mouse", "cursor", "here", "map here"):
                key = "cursor"
            if key in regions:
                pos = regions.get(key)
                if pos:
                    rx, ry = pos
            else:
                for name, pos in regions.items():
                    if key in name:
                        if not pos:
                            continue
                        rx, ry = pos
                        break
        if x is not None:
            rx = int(x)
        if y is not None:
            ry = int(y)
        if rx is not None or ry is not None:
            nx = self.sprite_x if rx is None else int(rx)
            ny = self.sprite_y if ry is None else int(ry)
            if animate and duration and duration > 0:
                self.start_x, self.start_y = self.sprite_x, self.sprite_y
                self.target_x, self.target_y = nx, ny
                self._move_start = time.monotonic()
                self._move_until = self._move_start + max(0.05, float(duration))
            else:
                self.sprite_x, self.sprite_y = nx, ny
                self.start_x, self.start_y = nx, ny
                self.target_x, self.target_y = nx, ny
                self._move_until = None
        self._last_interact = time.monotonic()
        self.desk.dirty = True

    def say(self, text=None, mood=None, ttl=None, **kw):
        self.update(text=text, mood=mood, **kw)
        self._ttl = None
        self._last_interact = time.monotonic()
        self.desk.dirty = True
        return self.state()

    def move_to(self, x, y=None, animate=False):
        if y is None:
            y = self.sprite_y
        self.update(x=int(x), y=int(y), animate=animate, duration=0.0)
        self._last_interact = time.monotonic()
        self.desk.dirty = True

    def hit(self, px, py):
        ix = px - self.sprite_x
        iy = py - self.sprite_y
        if 0 <= ix < SPRITE_W and 0 <= iy < SPRITE_H:
            try:
                return int(SPRITE_MASK_ARR[iy, ix]) > 90
            except Exception:
                return False
        return False

    def tick(self):
        now = time.monotonic()
        style = mood_style(self.mood)
        self._bob = math.sin(now * style.get("hz", 1.2)) * style.get("amp", 3.0)
        self._baseline = style.get("baseline", 0)
        self._blink = bool((now % 4.2) < 0.16)
        changed = False
        if self._move_until:
            try:
                span = max(0.001, self._move_until - self._move_start)
                dt = min(1, (now - self._move_start) / span)
                if dt >= 1:
                    self.sprite_x, self.sprite_y = self.target_x, self.target_y
                    self._move_until = None
                else:
                    t = 1 - (1 - dt) ** 3
                    self.sprite_x = int(self.start_x + (self.target_x - self.start_x) * t)
                    self.sprite_y = int(self.start_y + (self.target_y - self.start_y) * t)
                changed = True
            except Exception as e:
                # Log only the failed interpolation attempt when debugging is enabled.
                self._move_until = None
                return False
        if now - self._last_interact > 120:
            if self.mood not in ("neutral",):
                self.mood = "neutral"
                self._last_interact = now
        self.desk.dirty = True
        return changed

    def draw(self, d):
        x = self.sprite_x
        style = mood_style(self.mood)
        y = self.sprite_y + int(self._bob) + int(self._baseline)
        if self.visible:
            self.desk.img.paste(SPRITE_DITHERED, (x, y), SPRITE_MASK)
        lines = self._lines()
        # Keep original facial pixels. Mood is expressed by loose visual markers, not facial expression changes.
        marker = mood_style(self.mood).get("marker")
        if marker == "?":
            d.text((x + SPRITE_W - 14, y + 72), "?", font=FONT, fill=style.get("outline", C.black))
        elif marker == "!":
            d.text((x + SPRITE_W - 14, y + 70), "!", font=FONT, fill=style.get("outline", C.black))
        elif marker == "*":
            d.text((x + SPRITE_W - 14, y + 72), "*", font=FONT, fill=style.get("outline", C.black))
        # always-on chat bubble (clean rounded box, stable position)
        try:
            pixel_lengths = [d.text_length(line, font=FONT) for line in lines]
        except Exception:
            pixel_lengths = [len(line) * 7 for line in lines]
        maxw = int(max(pixel_lengths)) if pixel_lengths else 70
        margin = 10
        bw = int(max(74, min(maxw + 14, self.desk.W - 8)))
        bh = int(max(14, len(lines) * CH + 12))
        side_gap = 14
        # Prefer right of Clippy if there is room, otherwise left.
        if x + SPRITE_W + side_gap + bw + margin <= self.desk.W:
            bx = x + SPRITE_W + side_gap
        else:
            bx = max(4, x - bw - side_gap)
        by = max(4, min(y - 4, self.desk.H - bh - 4))
        # Bubble body only, to avoid clipped-looking tail artifacts.
        d.rounded_rectangle((bx, by, bx + bw, by + bh), radius=6, fill=C.white,
                                outline=style.get("outline", C.black), width=1)
        for i, line in enumerate(lines):
            d.text((bx + 7, by + 6 + i * CH), line, font=FONT, fill=C.black)

    def state(self):
        regions = self.regions()
        nearest_region = min(
            regions.items(), key=lambda kv: abs(kv[1][0] - self.sprite_x) + abs(kv[1][1] - self.sprite_y)
        )[0] if regions else None
        return {"text": self.text, "mood": self.mood, "visible": self.visible,
                "x": self.sprite_x, "y": self.sprite_y,
                "target": [self.target_x, self.target_y],
                "dragging": self.dragging, "regions": regions, "nearest_region": nearest_region}


def auto_mood(text, mood=None):
    return infer_mood(text, mood)