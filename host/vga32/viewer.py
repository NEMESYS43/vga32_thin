"""Image viewer: files, URLs and images dropped onto the web remote, dithered to the palette."""
import os
import queue
import threading
import urllib.parse

from . import images
from .palette import C
from .ui import CH, CW, FONT, App, bevel

TOOLBAR_H = 24


def default_folder():
    for d in ("~/Pictures", "~/OneDrive/Pictures", "~"):
        p = os.path.expanduser(d)
        if os.path.isdir(p):
            return p
    return os.getcwd()


def folder_images(folder):
    try:
        names = sorted(os.listdir(folder), key=str.lower)
    except OSError:
        return []
    return [os.path.join(folder, n) for n in names if n.lower().endswith(images.EXTENSIONS)]


class Viewer(App):
    title = "Images"
    icon = "image"
    size = (480, 340)

    def __init__(self, desk, url=None, image=None, **kw):
        super().__init__(desk)
        self.src, self.img, self.error = None, None, None
        self.loading = False
        self.dither = True
        self.actual = False            # False = fit to window, True = 1:1 with panning
        self.pan = [0, 0]
        self.drag = None
        self.cache = None              # ((w, h, dither, actual), palette image)
        self.inbox = queue.Queue()
        self.load_id = 0
        self.folder, self.index = [], -1
        self.editing, self.addr = False, ""
        if image is not None:
            self._set(url or "image", image)
        elif url:
            self.open(url)
        else:
            files = folder_images(default_folder())
            if files:
                self.open(files[0])
            else:
                self.error = "Ctrl+L or click the bar to open a file path or URL"

    # ---------- loading ----------
    def open(self, src):
        src = src.strip().strip('"')
        if images.is_local(src) or os.path.exists(src):
            src = images.local_path(src)
            if os.path.isdir(src):
                files = folder_images(src)
                if not files:
                    self.error = f"no images in {src}"
                    return
                src = files[0]
        self.loading, self.error = True, None
        self.load_id += 1
        lid = self.load_id

        def work():
            try:
                self.inbox.put((lid, src, images.load(src), None))
            except Exception as e:
                self.inbox.put((lid, src, None, str(e)[:160]))
        threading.Thread(target=work, daemon=True).start()

    def _set(self, src, img):
        self.src, self.img, self.error, self.loading = src, img, None, False
        self.cache, self.pan = None, [0, 0]
        if os.path.isfile(src):
            self.folder = folder_images(os.path.dirname(src))
            try:
                self.index = [os.path.normcase(p) for p in self.folder].index(os.path.normcase(src))
            except ValueError:
                self.index = -1
        else:
            self.folder, self.index = [], -1
        self.title = f"{self.name()} - Images"

    def name(self):
        if not self.src:
            return "Images"
        if self.src.startswith("data:"):
            return "dropped image"
        if os.path.isfile(self.src):
            return os.path.basename(self.src)
        return urllib.parse.unquote(urllib.parse.urlparse(self.src).path.rsplit("/", 1)[-1]) or self.src

    def step(self, n):
        if self.folder:
            self.index = (self.index + n) % len(self.folder)
            self.open(self.folder[self.index])

    def tick(self):
        changed = False
        while True:
            try:
                lid, src, img, err = self.inbox.get_nowait()
            except queue.Empty:
                return changed
            if lid != self.load_id:
                continue
            changed = True
            if img is None:
                self.loading, self.error = False, err
            else:
                self._set(src, img)

    # ---------- input ----------
    def key(self, k):
        if not k.down:
            return
        if self.editing:
            if k.name == "RETURN":
                self.editing = False
                if self.addr.strip():
                    self.open(self.addr)
            elif k.name == "ESCAPE":
                self.editing = False
            elif k.name == "BACKSPACE":
                self.addr = self.addr[:-1]
            elif k.char:
                self.addr += k.char
            return
        if (k.ctrl and k.name in ("l", "L")) or k.name == "F6":
            self.editing, self.addr = True, ""
        elif k.name in ("d", "D") and not k.ctrl:
            self.dither = not self.dither
            self.cache = None
        elif k.name in ("f", "F", "z", "Z", "1") and not k.ctrl:
            self.actual = not self.actual
            self.cache, self.pan = None, [0, 0]
        elif self.actual and k.name in ("LEFT", "RIGHT", "UP", "DOWN"):
            dx, dy = {"LEFT": (-40, 0), "RIGHT": (40, 0), "UP": (0, -40), "DOWN": (0, 40)}[k.name]
            self.pan[0] += dx
            self.pan[1] += dy
        elif k.name in ("RIGHT", "PAGEDOWN", "SPACE", "DOWN"):
            self.step(1)
        elif k.name in ("LEFT", "PAGEUP", "BACKSPACE", "UP"):
            self.step(-1)
        elif k.name == "HOME" and self.folder:
            self.step(-self.index)
        elif k.name == "END" and self.folder:
            self.step(len(self.folder) - 1 - self.index)

    def mouse(self, x, y, buttons, pressed, released, wheel):
        if wheel:
            self.step(-1 if wheel > 0 else 1)
        if pressed & 1 and y < TOOLBAR_H:
            w, _ = self.desk.client_size(self)
            if x < 30:
                self.step(-1)
            elif x < 56:
                self.step(1)
            else:
                self.editing, self.addr = True, ""
            return
        if pressed & 1:
            self.drag = (x, y, list(self.pan))
        if buttons & 1 and self.drag and self.actual:
            x0, y0, (px, py) = self.drag
            self.pan = [px - (x - x0), py - (y - y0)]
        if released & 1:
            self.drag = None

    # ---------- drawing ----------
    def draw(self, d, x, y, w, h):
        d.rectangle([x, y, x + w - 1, y + h - 1], fill=C.darkgray)
        d.rectangle([x, y, x + w - 1, y + TOOLBAR_H - 1], fill=C.gray)
        bevel(d, x + 4, y + 2, 24, TOOLBAR_H - 4)
        d.text((x + 12, y + 5), "<", font=FONT, fill=C.black)
        bevel(d, x + 30, y + 2, 24, TOOLBAR_H - 4)
        d.text((x + 38, y + 5), ">", font=FONT, fill=C.black)
        bevel(d, x + 58, y + 2, w - 62, TOOLBAR_H - 4, raised=False, face=C.white)
        maxc = max(1, (w - 72) // CW)
        label = (self.addr[-maxc + 1:] + "_") if self.editing else (self.src or "")[:maxc]
        if not self.editing and self.src and self.src.startswith("data:"):
            label = "dropped image"
        d.text((x + 62, y + 5), label, font=FONT, fill=C.black)

        ax, ay, aw, ah = x, y + TOOLBAR_H, w, h - TOOLBAR_H - CH - 2
        if self.img is not None and aw > 0 and ah > 0:
            key = (aw, ah, self.dither, self.actual)
            if not self.cache or self.cache[0] != key:
                src = self.img if self.actual else images.fit(self.img, aw - 4, ah - 4, upscale=True)
                self.cache = (key, images.to_palette(src, self.dither))
            im = self.cache[1]
            if self.actual:
                self.pan[0] = max(0, min(max(0, im.width - aw), self.pan[0]))
                self.pan[1] = max(0, min(max(0, im.height - ah), self.pan[1]))
                im = im.crop((self.pan[0], self.pan[1], self.pan[0] + min(aw, im.width),
                              self.pan[1] + min(ah, im.height)))
            self.desk.target.paste(im, (ax + (aw - im.width) // 2, ay + (ah - im.height) // 2))
        elif self.loading or self.error:
            msg = "Loading..." if self.loading else self.error
            d.text((ax + 10, ay + 10), msg[: max(1, (aw - 20) // CW)], font=FONT, fill=C.white)

        sy = y + h - CH - 2
        d.rectangle([x, sy, x + w - 1, y + h - 1], fill=C.gray)
        parts = []
        if self.error and self.img is not None:      # a newer image failed; keep showing the old one
            parts.append("can't open: " + self.error)
        if self.img is not None:
            parts.append(f"{self.img.width}x{self.img.height}")
        if self.folder:
            parts.append(f"{self.index + 1}/{len(self.folder)}")
        parts.append("1:1" if self.actual else "fit")
        parts.append("dither" if self.dither else "flat")
        parts.append("D dither  F fit  arrows next")
        d.text((x + 4, sy + 1), "  ".join(parts)[: max(1, (w - 8) // CW)], font=FONT, fill=C.black)

    def state(self):
        return {"src": None if not self.src else ("data:..." if self.src.startswith("data:") else self.src),
                "name": self.name(), "size": list(self.img.size) if self.img else None, "loading": self.loading,
                "error": self.error, "dither": self.dither, "mode": "actual" if self.actual else "fit",
                "folder_index": self.index if self.folder else None, "folder_count": len(self.folder)}
