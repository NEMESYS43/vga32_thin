"""Built-in desktop apps."""
import math
import os
import platform
import queue
import subprocess
import threading
import time
from datetime import datetime

from PIL import Image, ImageDraw

from .palette import C, PALETTE
from .ui import BIG, CH, CW, FONT, App, parse_color, text_size


class Terminal(App):
    title = "Terminal"
    size = (480, 300)
    icon = "terminal"
    MAX_LINES = 2000

    def __init__(self, desk, command=None, cwd=None, **kw):
        super().__init__(desk)
        self.cwd = cwd if cwd and os.path.isdir(cwd) else os.path.expanduser("~")
        self.lines = [f"{platform.system()} shell on {platform.node()}", "Type a command. 'cls' clears.", ""]
        self.dropped = 0            # lines trimmed off the top, so line numbers stay absolute
        self.inp, self.scroll, self.busy = "", 0, False
        self.last_exit = None
        self.out = queue.Queue()
        self.proc = None
        if command:
            self.run(command)

    @property
    def line_count(self):
        return self.dropped + len(self.lines)

    def lines_since(self, n):
        return self.lines[max(0, n - self.dropped):]

    def _append(self, ln):
        self.lines.append(ln)
        extra = len(self.lines) - self.MAX_LINES
        if extra > 0:
            del self.lines[:extra]
            self.dropped += extra

    def prompt(self):
        return f"{self.cwd}> "

    def run(self, cmd):
        self._append(self.prompt() + cmd)
        self.scroll = 0
        s = cmd.strip()
        self.last_exit = None
        if not s:
            return
        if s in ("cls", "clear"):
            self.dropped += len(self.lines)
            self.lines = []
            return
        if s == "cd" or s.startswith("cd ") or (len(s) == 2 and s[1] == ":"):
            target = s[3:].strip().strip('"') if s.startswith("cd") else s
            target = os.path.expanduser(target or "~")
            path = os.path.normpath(os.path.join(self.cwd, target))
            if os.path.isdir(path):
                self.cwd = path
                self.last_exit = 0
            else:
                self._append(f"no such directory: {target}")
                self.last_exit = 1
            return
        self.busy = True
        threading.Thread(target=self._exec, args=(s,), daemon=True).start()

    def _exec(self, cmd):
        code = -1
        try:
            self.proc = subprocess.Popen(cmd, shell=True, cwd=self.cwd, stdout=subprocess.PIPE,
                                         stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
            for raw in self.proc.stdout:
                self.out.put(raw.decode(errors="replace").rstrip("\r\n").expandtabs(4))
            code = self.proc.wait()
        except Exception as e:
            self.out.put(f"error: {e}")
        self.proc = None
        self.out.put(("exit", code))

    def kill(self):
        if self.proc:
            self.proc.kill()
            self._append("^C")

    def tick(self):
        changed = False
        while True:
            try:
                ln = self.out.get_nowait()
            except queue.Empty:
                return changed
            changed = True
            if isinstance(ln, tuple):
                self.busy, self.last_exit = False, ln[1]
            else:
                self._append(ln)

    def key(self, k):
        if not k.down:
            return
        if k.ctrl and k.name in ("c", "C"):
            if self.proc:
                self.kill()
            else:
                self.inp = ""
        elif self.busy:
            return
        elif k.name == "RETURN":
            cmd, self.inp = self.inp, ""
            self.run(cmd)
        elif k.name == "BACKSPACE":
            self.inp = self.inp[:-1]
        elif k.name == "PAGEUP":
            self.scroll += 10
        elif k.name == "PAGEDOWN":
            self.scroll = max(0, self.scroll - 10)
        elif k.char:
            self.inp += k.char
            self.scroll = 0

    def mouse(self, x, y, buttons, pressed, released, wheel):
        if wheel:
            self.scroll = max(0, self.scroll - wheel * 3)

    def copy(self):
        return "\n".join(self.lines[-200:])

    def paste(self, text):
        if not self.busy:
            self.inp += text.replace("\r", "").split("\n")[0]

    def context_menu(self, x, y):
        return [{"id": "copy", "label": "Copy Output"},
                {"id": "paste", "label": "Paste", "enabled": bool(self.desk.clipboard) and not self.busy},
                {"separator": True},
                {"id": "kill", "label": "Stop Command", "shortcut": "Ctrl+C", "enabled": self.busy},
                {"id": "cls", "label": "Clear"}]

    def on_menu(self, item_id):
        if item_id == "copy":
            self.desk.set_clipboard(self.copy(), "terminal")
        elif item_id == "paste":
            self.paste(self.desk.clipboard)
        elif item_id == "kill":
            self.kill()
        elif item_id == "cls" and not self.busy:
            self.run("cls")

    def draw(self, d, x, y, w, h):
        d.rectangle([x, y, x + w - 1, y + h - 1], fill=C.black)
        cols, rows = max(1, (w - 8) // CW), max(1, (h - 4) // CH)
        wrapped = []
        tail = [] if self.busy else [self.prompt() + self.inp + "_"]
        # Only wrap what can be shown unless the user scrolled back.
        source = self.lines if self.scroll else self.lines[-rows * 4:]
        for ln in source + tail:
            wrapped += [ln[i:i + cols] for i in range(0, max(len(ln), 1), cols)]
        self.scroll = min(self.scroll, max(0, len(wrapped) - rows))
        end = len(wrapped) - self.scroll
        for i, ln in enumerate(wrapped[max(0, end - rows):end]):
            d.text((x + 4, y + 2 + i * CH), ln, font=FONT, fill=C.lime)

    def state(self):
        return {"cwd": self.cwd, "busy": self.busy, "last_exit": self.last_exit, "input": self.inp,
                "tail": self.lines[-15:]}


class Notes(App):
    title = "Notes"
    size = (420, 300)
    icon = "notes"
    path = os.path.join(os.path.expanduser("~"), "vga32_notes.txt")

    def __init__(self, desk, text=None, path=None, **kw):
        super().__init__(desk)
        if path:
            self.path = os.path.abspath(os.path.expanduser(path))
            self.title = os.path.basename(self.path)
        try:
            with open(self.path, encoding="utf-8") as f:
                self.lines = f.read().split("\n")
        except OSError:
            self.lines = [""] if path else ["Notes are saved to " + self.path, "Ctrl+S saves.", ""]
        self.r, self.c, self.top, self.status = 0, 0, 0, ""
        if text is not None:
            self.set_text(text)

    @property
    def text(self):
        return "\n".join(self.lines)

    def set_text(self, text, append=False, save=True):
        if append:
            text = self.text + ("" if self.text.endswith("\n") or not self.text else "\n") + text
        self.lines = text.replace("\r\n", "\n").split("\n") or [""]
        self.r = len(self.lines) - 1
        self.c = len(self.lines[self.r])
        if save:
            self.save()

    def save(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(self.text)
        self.status = "saved " + datetime.now().strftime("%H:%M:%S")

    def close(self):
        self.save()

    def key(self, k):
        if not k.down:
            return
        L, r, c = self.lines, self.r, self.c
        self.status = ""
        if k.ctrl and k.name in ("s", "S"):
            self.save()
        elif k.name == "LEFT":
            if c:
                c -= 1
            elif r:
                r -= 1; c = len(L[r])
        elif k.name == "RIGHT":
            if c < len(L[r]):
                c += 1
            elif r < len(L) - 1:
                r += 1; c = 0
        elif k.name == "UP":
            r = max(0, r - 1)
        elif k.name == "DOWN":
            r = min(len(L) - 1, r + 1)
        elif k.name == "PAGEUP":
            r = max(0, r - 15)
        elif k.name == "PAGEDOWN":
            r = min(len(L) - 1, r + 15)
        elif k.name == "HOME":
            c = 0
        elif k.name == "END":
            c = len(L[r])
        elif k.name == "RETURN":
            L[r:r + 1] = [L[r][:c], L[r][c:]]
            r += 1; c = 0
        elif k.name == "BACKSPACE":
            if c:
                L[r] = L[r][:c - 1] + L[r][c:]; c -= 1
            elif r:
                c = len(L[r - 1]); L[r - 1] += L.pop(r); r -= 1
        elif k.name == "DELETE":
            if c < len(L[r]):
                L[r] = L[r][:c] + L[r][c + 1:]
            elif r < len(L) - 1:
                L[r] += L.pop(r + 1)
        elif k.name == "TAB":
            L[r] = L[r][:c] + "    " + L[r][c:]; c += 4
        elif k.char:
            L[r] = L[r][:c] + k.char + L[r][c:]; c += 1
        self.r, self.c = r, min(c, len(L[r]))

    def copy(self):
        return self.text

    def paste(self, text):
        L, r, c = self.lines, self.r, self.c
        parts = text.replace("\r\n", "\n").split("\n")
        tail = L[r][c:]
        L[r] = L[r][:c] + parts[0]
        L[r + 1:r + 1] = parts[1:]
        self.r = r + len(parts) - 1
        self.c = len(L[self.r])
        L[self.r] += tail

    def context_menu(self, x, y):
        return [{"id": "save", "label": "Save", "shortcut": "Ctrl+S"},
                {"id": "copy", "label": "Copy All"},
                {"id": "paste", "label": "Paste", "enabled": bool(self.desk.clipboard)},
                {"separator": True},
                {"id": "clear", "label": "Clear"}]

    def on_menu(self, item_id):
        if item_id == "save":
            self.save()
        elif item_id == "copy":
            self.desk.set_clipboard(self.text, "notes")
        elif item_id == "paste":
            self.paste(self.desk.clipboard)
        elif item_id == "clear":
            self.set_text("", save=False)

    def mouse(self, x, y, buttons, pressed, released, wheel):
        if wheel:
            self.top = max(0, min(len(self.lines) - 1, self.top - wheel * 3))
        if pressed & 1:
            self.r = min(len(self.lines) - 1, self.top + max(0, (y - 2) // CH))
            self.c = min(len(self.lines[self.r]), max(0, (x - 4 + CW // 2) // CW))

    def draw(self, d, x, y, w, h):
        d.rectangle([x, y, x + w - 1, y + h - 1], fill=C.white)
        rows = max(1, (h - 4 - CH) // CH)
        if self.r < self.top:
            self.top = self.r
        elif self.r >= self.top + rows:
            self.top = self.r - rows + 1
        for i, ln in enumerate(self.lines[self.top:self.top + rows]):
            d.text((x + 4, y + 2 + i * CH), ln, font=FONT, fill=C.black)
        cx, cy = x + 4 + self.c * CW, y + 2 + (self.r - self.top) * CH
        d.rectangle([cx, cy, cx + 1, cy + CH - 2], fill=C.blue)
        sy = y + h - CH - 1
        d.rectangle([x, sy, x + w - 1, y + h - 1], fill=C.gray)
        d.text((x + 4, sy + 1), f"Ln {self.r + 1}  Col {self.c + 1}   {self.status}", font=FONT, fill=C.black)

    def state(self):
        return {"path": self.path, "lines": len(self.lines), "cursor": [self.r + 1, self.c + 1],
                "preview": self.text[:400]}


class Paint(App):
    icon = "paint"
    title = "Paint"
    size = (420, 320)

    def __init__(self, desk, **kw):
        super().__init__(desk)
        self.canvas = Image.new("RGB", (640, 480), C.white)
        self.cd = ImageDraw.Draw(self.canvas)
        self.cd.fontmode = "1"
        self.color, self.brush, self.last = C.black, 2, None

    def clear(self, color=None):
        self.cd.rectangle([0, 0, 639, 479], fill=parse_color(color, C.white))

    def draw_elements(self, elements):
        from .panel import render_elements
        w, h = self.desk.client_size(self)
        render_elements(self.canvas, self.cd, elements, w, h - 18)

    def mouse(self, x, y, buttons, pressed, released, wheel):
        w, h = self.desk.client_size(self)
        sw = max(1, w // 16)
        if y >= h - 18:
            if pressed & 1 and x // sw < 16:
                self.color = PALETTE[x // sw]
            self.last = None
            return
        if wheel:
            self.brush = max(1, min(12, self.brush + wheel))
        if buttons & 3:
            col = self.color if buttons & 1 else C.white
            p = (x, y)
            self.cd.line([self.last or p, p], fill=col, width=self.brush * 2)
            r = self.brush
            self.cd.ellipse([x - r, y - r, x + r, y + r], fill=col)
            self.last = p
        else:
            self.last = None

    def key(self, k):
        if k.down and k.char.isdigit() and k.char != "0":
            self.brush = int(k.char)
        elif k.down and k.name in ("c", "C") and not k.ctrl:
            self.clear()

    def draw(self, d, x, y, w, h):
        self.desk.target.paste(self.canvas.crop((0, 0, w, h - 18)), (x, y))
        sw = max(1, w // 16)
        for i, rgb in enumerate(PALETTE):
            d.rectangle([x + i * sw, y + h - 18, x + i * sw + sw - 1, y + h - 1], fill=rgb)
            if rgb == self.color:
                inv = C.white if sum(rgb) < 380 else C.black
                d.rectangle([x + i * sw + 2, y + h - 16, x + i * sw + sw - 3, y + h - 3], outline=inv)

    def state(self):
        return {"brush": self.brush, "color": list(self.color), "canvas_size": list(self.desk.client_size(self))}


class Clock(App):
    icon = "clock"
    title = "Clock"
    size = (220, 250)

    def __init__(self, desk, **kw):
        super().__init__(desk)
        self.shown = None

    def tick(self):
        now = int(time.time())
        if now != self.shown:
            self.shown = now
            return True
        return False

    def draw(self, d, x, y, w, h):
        d.rectangle([x, y, x + w - 1, y + h - 1], fill=C.navy)
        now = datetime.now()
        r = max(10, min(w, h - 50) // 2 - 8)
        cx, cy = x + w // 2, y + 8 + r
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=C.white, outline=C.sky, width=3)
        for i in range(12):
            a = math.radians(i * 30)
            d.line([cx + (r - 8) * math.sin(a), cy - (r - 8) * math.cos(a),
                    cx + (r - 3) * math.sin(a), cy - (r - 3) * math.cos(a)], fill=C.black, width=2)
        for frac, length, col, wd in ((((now.hour % 12) + now.minute / 60) / 12, 0.5, C.black, 4),
                                      ((now.minute + now.second / 60) / 60, 0.75, C.black, 3),
                                      (now.second / 60, 0.85, C.red, 1)):
            a = frac * 2 * math.pi
            d.line([cx, cy, cx + r * length * math.sin(a), cy - r * length * math.cos(a)], fill=col, width=wd)
        s = now.strftime("%H:%M:%S")
        tw, _ = text_size(s, BIG)
        d.text((cx - tw // 2, cy + r + 6), s, font=BIG, fill=C.yellow)


class System(App):
    icon = "system"
    title = "System"
    size = (360, 230)

    def __init__(self, desk, **kw):
        super().__init__(desk)
        try:
            import psutil
            self.ps = psutil
            psutil.cpu_percent()
        except ImportError:
            self.ps = None
        self.cpu, self.next = [], 0

    def tick(self):
        if time.monotonic() < self.next:
            return False
        self.next = time.monotonic() + 1
        if self.ps:
            self.cpu = (self.cpu + [self.ps.cpu_percent()])[-120:]
        return True

    def draw(self, d, x, y, w, h):
        d.rectangle([x, y, x + w - 1, y + h - 1], fill=C.black)
        dev = self.desk.dev
        info = [f"Host   {platform.node()}", f"OS     {platform.system()} {platform.release()}",
                f"Python {platform.python_version()}",
                f"Board  {dev.info.name} @ {dev.host}  {self.desk.W}x{self.desk.H}",
                f"Sent   {self.desk.screen.bytes_sent // 1024} KB"]
        if not self.ps:
            info.append("pip install psutil for CPU / RAM")
        for i, s in enumerate(info):
            d.text((x + 6, y + 4 + i * CH), s, font=FONT, fill=C.white)
        if not self.ps:
            return
        mem = self.ps.virtual_memory()
        gy = y + 8 + len(info) * CH
        d.text((x + 6, gy), f"RAM {mem.percent:4.1f}%", font=FONT, fill=C.sky)
        bx = x + 6 + CW * 10
        d.rectangle([bx, gy + 2, x + w - 8, gy + CH - 2], outline=C.darkgray)
        d.rectangle([bx, gy + 2, bx + (x + w - 8 - bx) * mem.percent / 100, gy + CH - 2], fill=C.sky)
        gy += CH + 4
        cpu = self.cpu[-1] if self.cpu else 0
        d.text((x + 6, gy), f"CPU {cpu:4.1f}%", font=FONT, fill=C.lime)
        top, bot = gy + CH, y + h - 6
        if bot - top < 8:
            return
        d.rectangle([x + 6, top, x + w - 7, bot], outline=C.darkgray)
        gw = x + w - 8 - (x + 7)
        pts = self.cpu[-max(1, gw // 3):]
        for i, v in enumerate(pts):
            px = x + w - 8 - (len(pts) - 1 - i) * 3
            d.line([px, bot - 1, px, bot - 1 - (bot - top - 2) * v / 100], fill=C.lime, width=2)

    def state(self):
        if not self.ps:
            return {}
        return {"cpu_percent": self.cpu[-1] if self.cpu else None, "ram_percent": self.ps.virtual_memory().percent}


class About(App):
    icon = "about"
    title = "About"
    size = (320, 176)

    def draw(self, d, x, y, w, h):
        d.rectangle([x, y, x + w - 1, y + h - 1], fill=C.gray)
        for i, s in enumerate(["VGA32 Thin Client", "", "Apps run on this PC; the board",
                               "is a 16-color screen, PS/2 keyboard", "and mouse over WiFi.", "",
                               "Ctrl+Esc / Win: menu   Alt+F4: close", "Alt+Tab: switch   drag corner: resize",
                               "Right-click: context menus", "Settings: theme, wallpaper, apps"]):
            d.text((x + 8, y + 4 + i * CH), s, font=FONT, fill=C.black)
