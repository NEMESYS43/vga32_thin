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


def _build_lut(palette):
    """64-entry table: packed RGB222 -> nearest palette index."""
    pal = np.array(palette, dtype=np.int32)
    lut = np.zeros(64, dtype=np.uint8)
    for i in range(64):
        rgb = np.array([(i >> 4) & 3, (i >> 2) & 3, i & 3]) * 85
        lut[i] = int(np.argmin(((pal - rgb) ** 2).sum(axis=1)))
    return lut


LUT = _build_lut(PALETTE)


def rgb_to_index(rgb):
    """HxWx3 uint8 array -> HxW palette indices (nearest RGB222 then nearest entry)."""
    q = (rgb.astype(np.uint16) + 42) // 85          # round each channel to 0..3
    packed = (q[..., 0] << 4) | (q[..., 1] << 2) | q[..., 2]
    return LUT[packed]


PAL_ARRAY = np.array(PALETTE, np.uint8)
_BAYER4 = np.array([[0, 8, 2, 10], [12, 4, 14, 6], [3, 11, 1, 9], [15, 7, 13, 5]], np.uint8)
_PLAN = None


def _build_plan(spread_penalty=0.02):
    """Pattern-dither plan (Yliluoma's algorithm 2): for each of 32^3 input colors, 16 palette
    indices whose average in linear light matches it, sorted by brightness. A pixel then takes
    plan[color][bayer position]. The penalty keeps far-off colors out of the mix (less speckle)."""
    lin = lambda c: (c / 255.0) ** 2.2 * 255
    pal = lin(np.array(PALETTE, np.float32))
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
    luma = (np.array(PALETTE, np.float32) * [0.299, 0.587, 0.114]).sum(1)
    return np.take_along_axis(plan, np.argsort(luma[plan], axis=1, kind="stable"), 1)


def ordered_dither(img):
    """Pattern-dither an RGB image onto the palette (RGB out, exact palette colors).

    Unlike Floyd-Steinberg the output depends only on each pixel's color and position, so
    unchanged parts of consecutive video frames stay identical and only real changes get
    sent to the board.
    """
    global _PLAN
    if _PLAN is None:
        _PLAN = _build_plan()
    a = np.asarray(img.convert("RGB"), dtype=np.uint16)
    h, w = a.shape[:2]
    q = (a * 31 + 127) // 255
    t = np.tile(_BAYER4, (h // 4 + 1, w // 4 + 1))[:h, :w]
    return Image.fromarray(PAL_ARRAY[_PLAN[(q[..., 0] << 10) | (q[..., 1] << 5) | q[..., 2], t]])


_PAL_IMAGE = None


def dither(img):
    """Floyd-Steinberg dither an RGB image onto the palette; returns RGB with exact palette colors."""
    global _PAL_IMAGE
    if _PAL_IMAGE is None:
        _PAL_IMAGE = Image.new("P", (1, 1))
        flat = [c for rgb in PALETTE for c in rgb]
        _PAL_IMAGE.putpalette(flat + flat[:3] * (256 - len(PALETTE)))
    return img.convert("RGB").quantize(palette=_PAL_IMAGE, dither=Image.Dither.FLOYDSTEINBERG).convert("RGB")
