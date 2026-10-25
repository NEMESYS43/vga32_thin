"""The 16-entry desktop palette and RGB -> palette-index mapping.

The VGA32 DAC is RGB222 (each channel 0, 85, 170 or 255), so every palette entry is
one of those 64 colors. Index 0 must stay black and 15 white: the board's built-in
arrow cursor uses them.
"""
import numpy as np
from PIL import Image

PALETTE = [
    (0, 0, 0),        # 0  black
    (85, 85, 85),     # 1  dark gray
    (170, 170, 170),  # 2  gray
    (0, 0, 85),       # 3  navy
    (0, 85, 170),     # 4  blue
    (85, 170, 255),   # 5  sky
    (0, 85, 85),      # 6  teal
    (0, 170, 170),    # 7  cyan
    (0, 170, 85),     # 8  green
    (85, 255, 85),    # 9  lime
    (170, 0, 0),      # 10 maroon
    (255, 85, 85),    # 11 red
    (255, 170, 0),    # 12 orange
    (255, 255, 85),   # 13 yellow
    (170, 85, 255),   # 14 purple
    (255, 255, 255),  # 15 white
]
NAMES = ["black", "darkgray", "gray", "navy", "blue", "sky", "teal", "cyan",
         "green", "lime", "maroon", "red", "orange", "yellow", "purple", "white"]

# Named colors for app code, e.g. C.white -> (255, 255, 255) usable directly in ImageDraw.
C = type("Colors", (), {n: PALETTE[i] for i, n in enumerate(NAMES)})

# What each index actually looks like on the monitor. Apps always draw with the logical PALETTE;
# a theme may remap entries here (sent to the board with OP_PALETTE), e.g. sky -> lavender.
DISPLAY = list(PALETTE)


def display_flat():
    """DISPLAY as a flat [r, g, b, ...] list for PIL "P" images (web mirror, screenshots)."""
    return [c for rgb in DISPLAY for c in rgb]


RGB222 = [((i >> 4) & 3) * 85 for i in range(64)], [((i >> 2) & 3) * 85 for i in range(64)], [(i & 3) * 85 for i in range(64)]
CANDIDATES = np.stack(RGB222, 1).astype(np.uint8)          # every color the board's DAC can show
_BAYER4 = np.array([[0, 8, 2, 10], [12, 4, 14, 6], [3, 11, 1, 9], [15, 7, 13, 5]], np.uint8)


def _build_lut32(palette):
    """32x32x32 table: color (5 bits per channel) -> nearest palette index."""
    pal = np.array(palette, dtype=np.float32)
    g = np.arange(32, dtype=np.float32) * (255 / 31)
    r, gg, b = np.meshgrid(g, g, g, indexing="ij")
    cols = np.stack([r, gg, b], -1).reshape(-1, 3)
    return ((cols[:, None, :] - pal[None]) ** 2).sum(-1).argmin(1).astype(np.uint8)


def _build_plan(palette=PALETTE, spread_penalty=0.02):
    """Pattern-dither plan (Yliluoma's algorithm 2): for each of 32^3 input colors, 16 palette
    indices whose average in linear light matches it, sorted by brightness. A pixel then takes
    plan[color][bayer position]. The penalty keeps far-off colors out of the mix (less speckle)."""
    lin = lambda c: (c / 255.0) ** 2.2 * 255
    pal = lin(np.array(palette, np.float32))
    g = np.arange(32, dtype=np.float32) * (255 / 31)
    r, gg, b = np.meshgrid(g, g, g, indexing="ij")
    cols = lin(np.stack([r, gg, b], -1).reshape(-1, 3))
    cand = ((pal[None] - cols[:, None, :]) ** 2).sum(-1) * spread_penalty
    plan = np.zeros((cols.shape[0], 16), np.uint8)
    acc = np.zeros_like(cols)
    for c in range(16):
        err = ((((acc[:, None, :] + pal[None]) / (c + 1)) - cols[:, None, :]) ** 2).sum(-1) + cand
        k = err.argmin(1)
        plan[:, c] = k
        acc += pal[k]
    luma = (np.array(palette, np.float32) * [0.299, 0.587, 0.114]).sum(1)
    return np.take_along_axis(plan, np.argsort(luma[plan], axis=1, kind="stable"), 1)


class PaletteState:
    """Everything needed to map and dither onto one 16-color palette. Building the dither plan
    takes ~0.3 s, so new states are prepared off the desktop thread and swapped in whole."""

    def __init__(self, palette, plan=False):
        self.palette = [tuple(int(v) for v in c) for c in palette]
        self.array = np.array(self.palette, np.uint8)
        self.lut = _build_lut32(self.palette)
        self._plan = _build_plan(self.palette) if plan else None
        self._pal_image = None

    @property
    def plan(self):
        if self._plan is None:
            self._plan = _build_plan(self.palette)
        return self._plan

    def pal_image(self):
        if self._pal_image is None:
            im = Image.new("P", (1, 1))
            flat = [c for rgb in self.palette for c in rgb]
            im.putpalette(flat + flat[:3] * (256 - len(self.palette)))
            self._pal_image = im
        return self._pal_image


DEFAULT = PaletteState(PALETTE)
_active = DEFAULT


def active():
    """The palette the screen currently maps to: the logical PALETTE, or an adaptive one."""
    return _active


def activate(state):
    global _active
    _active = state or DEFAULT


def rgb_to_index(rgb):
    """HxWx3 uint8 array -> HxW indices of the nearest color in the active palette."""
    q = rgb.astype(np.uint16) >> 3
    return _active.lut[(q[..., 0] << 10) | (q[..., 1] << 5) | q[..., 2]]


def ordered_dither(img):
    """Pattern-dither an RGB image onto the active palette (RGB out, exact palette colors).

    Unlike Floyd-Steinberg the output depends only on each pixel's color and position, so
    unchanged parts of consecutive video frames stay identical and only real changes get
    sent to the board.
    """
    st = _active
    a = np.asarray(img.convert("RGB"), dtype=np.uint16)
    h, w = a.shape[:2]
    q = (a * 31 + 127) // 255
    t = np.tile(_BAYER4, (h // 4 + 1, w // 4 + 1))[:h, :w]
    return Image.fromarray(st.array[st.plan[(q[..., 0] << 10) | (q[..., 1] << 5) | q[..., 2], t]])


def dither(img):
    """Floyd-Steinberg dither an RGB image onto the active palette; returns RGB with exact palette colors."""
    return img.convert("RGB").quantize(palette=_active.pal_image(), dither=Image.Dither.FLOYDSTEINBERG).convert("RGB")


# ----- adaptive palettes -----
def palette_error(sample, palette):
    """Mean squared distance from each sample pixel to its nearest palette color."""
    px = sample.reshape(-1, 3).astype(np.float32)
    pal = np.asarray(palette, np.float32)
    return float(((px[:, None, :] - pal[None]) ** 2).sum(-1).min(1).mean())


def choose_palette(sample, fixed=((0, 0, 0), (255, 255, 255))):
    """The 16 board colors (of the DAC's 64) that best cover an image sample (HxWx3 uint8).
    Black stays at index 0 and white at 15 (the board's cursor uses them); the rest are picked
    greedily to minimize each pixel's distance to its nearest color, then sorted by brightness."""
    px = sample.reshape(-1, 3).astype(np.float32)
    cand = CANDIDATES.astype(np.float32)
    d = ((px[:, None, :] - cand[None]) ** 2).sum(-1)              # pixels x 64
    chosen = [int(np.where((CANDIDATES == f).all(1))[0][0]) for f in fixed]
    best = d[:, chosen].min(1)
    while len(chosen) < 16:
        gain = np.maximum(best[:, None] - d, 0).sum(0)
        gain[chosen] = -1
        k = int(gain.argmax())
        if gain[k] <= 0:            # the image needs fewer colors: fill with the most used remaining grays/hues
            k = next(i for i in np.argsort(d.mean(0)) if i not in chosen)
        chosen.append(k)
        best = np.minimum(best, d[:, k])
    middle = sorted(chosen[2:], key=lambda i: (CANDIDATES[i] * [0.299, 0.587, 0.114]).sum())
    cols = [tuple(int(v) for v in CANDIDATES[i]) for i in middle]
    return [tuple(fixed[0])] + cols + [tuple(fixed[1])]
