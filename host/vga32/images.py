"""Loading images from URLs, files and data URIs, and fitting them onto the 16-color palette."""
import base64
import io
import os
import urllib.parse
import urllib.request

from PIL import Image, ImageOps

from .palette import dither

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) VGA32TextBrowser/1.0"
MAX_BYTES = 12 << 20
MAX_SIDE = 1280          # decoded images are shrunk to this; the screen is 640 wide anyway
EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff", ".ico")


def is_local(src):
    return os.path.isabs(src) or src.startswith(("file:", "~", "./", "../"))


def local_path(src):
    if src.startswith("file:"):
        src = urllib.parse.unquote(urllib.parse.urlparse(src).path)
        if os.name == "nt" and src.startswith("/") and src[2:3] == ":":
            src = src[1:]
    return os.path.abspath(os.path.expanduser(src))


def read_bytes(src, referer=None, timeout=15):
    """Raw bytes of an http(s) URL, data: URI, local path or bare base64."""
    if src.startswith(("http://", "https://")):
        headers = {"User-Agent": UA, "Accept": "image/avif,image/webp,image/png,image/*;q=0.8,*/*;q=0.5"}
        if referer:
            headers["Referer"] = referer
        with urllib.request.urlopen(urllib.request.Request(src, headers=headers), timeout=timeout) as r:
            return r.read(MAX_BYTES)
    if src.startswith("data:"):
        meta, _, payload = src.partition(",")
        return base64.b64decode(payload) if ";base64" in meta else urllib.parse.unquote_to_bytes(payload)
    if is_local(src) or os.path.exists(src):
        with open(local_path(src), "rb") as f:
            return f.read(MAX_BYTES)
    return base64.b64decode(src)


def decode(data):
    """Bytes -> RGB image (first frame, EXIF-rotated, transparency on white, at most MAX_SIDE)."""
    im = Image.open(io.BytesIO(data))
    if im.format == "JPEG":
        im.draft("RGB", (MAX_SIDE, MAX_SIDE))
    im.seek(0)
    im = ImageOps.exif_transpose(im)
    if im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info):
        im = im.convert("RGBA")
        bg = Image.new("RGB", im.size, (255, 255, 255))
        bg.paste(im, mask=im.getchannel("A"))
        im = bg
    else:
        im = im.convert("RGB")
    if max(im.size) > MAX_SIDE:
        im.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)
    return im


def load(src, referer=None, timeout=15):
    return decode(read_bytes(src, referer, timeout))


def fit(im, w, h, upscale=False):
    """Scale to fit inside w x h, keeping the aspect ratio."""
    s = min(w / im.width, h / im.height)
    if not upscale:
        s = min(1.0, s)
    size = (max(1, round(im.width * s)), max(1, round(im.height * s)))
    return im if size == im.size else im.resize(size, Image.LANCZOS)


def to_palette(im, use_dither=True):
    """RGB image -> RGB image that uses only palette colors."""
    if use_dither:
        return dither(im)
    from .palette import PALETTE
    pal = Image.new("P", (1, 1))
    flat = [c for rgb in PALETTE for c in rgb]
    pal.putpalette(flat + flat[:3] * (256 - len(PALETTE)))
    return im.quantize(palette=pal, dither=Image.Dither.NONE).convert("RGB")
