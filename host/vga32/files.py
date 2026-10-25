"""Files: browse the PC's folders and open things in the right app.

Double-click (or Enter) opens: folders navigate, images go to the viewer, text files to
Notes, .html/.htm to Web. Right-click for more (terminal here, copy path, set as wallpaper).
"""
import os
import string
import time

from .images import EXTENSIONS as IMAGE_EXT
from .palette import C
from .ui import CH, FONT, App, bevel, draw_glyph, text_size

TEXT_EXT = (".txt", ".md", ".py", ".json", ".yaml", ".yml", ".ini", ".cfg", ".toml", ".log", ".csv", ".ino",
            ".c", ".h", ".cpp", ".js", ".ts", ".css", ".sh", ".bat", ".ps1", ".xml")
ROW_H = CH + 3
BAR_H = 24


def _size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


class Files(App):
    title = "Files"
    size = (460, 300)
    icon = "files"
    category = "Accessories"

    def __init__(self, desk, path=None, url=None, **kw):
        super().__init__(desk)
        self.entries, self.sel, self.top, self.error = [], 0, 0, ""
        self.cwd = ""
        self.last_click = (-1, 0.0)
        self.go(path or url or os.path.expanduser("~"))

    # ----- navigation -----
    def go(self, path):
        path = os.path.abspath(os.path.expanduser(path)) if path != "" else ""
        try:
            if path == "" or (os.name == "nt" and path in ("\\", "/")):
                names = [f"{d}:\\" for d in string.ascii_uppercase if os.path.exists(f"{d}:\\")]
                self.entries = [(n, True, 0, 0) for n in names]
                path = ""
            else:
                ents = []
                with os.scandir(path) as it:
                    for e in it:
                        if e.name.startswith("."):
                            continue
                        try:
                            st = e.stat()
                            ents.append((e.name, e.is_dir(), st.st_size, st.st_mtime))
                        except OSError:
                            pass
                ents.sort(key=lambda t: (not t[1], t[0].lower()))
                self.entries = [("..", True, 0, 0)] + ents
            self.cwd, self.sel, self.top, self.error = path, 0, 0, ""
            self.title = os.path.basename(path.rstrip("\\/")) or path or "Computer"
        except OSError as e:
            self.error = str(e)

    def path_of(self, i):
        name = self.entries[i][0]
        if self.cwd == "":
            return name
        if name == "..":
            parent = os.path.dirname(self.cwd.rstrip("\\/")) or self.cwd
            return "" if os.name == "nt" and parent == self.cwd else parent
        return os.path.join(self.cwd, name)

    def open(self, i):
        if not 0 <= i < len(self.entries):
            return
        p = self.path_of(i)
        is_dir = self.entries[i][1]
        low = p.lower()
        if is_dir:
            self.go(p)
        elif low.endswith(IMAGE_EXT):
            self.desk.launch("viewer", url=p)
        elif low.endswith((".bsp", ".obj")):
            self.desk.launch("viewer3d", path=p)
        elif low.endswith((".html", ".htm")):
            self.desk.launch("web", url="file:///" + p.replace("\\", "/").lstrip("/"))
        elif low.endswith(TEXT_EXT) or self.entries[i][2] < 256 * 1024:
            self.desk.launch("notes", path=p)
        else:
            self.desk.notify(f"No app opens {os.path.basename(p)}")

    # ----- input -----
    def key(self, k):
        if not k.down or not self.entries:
            return
        if k.name in ("UP", "DOWN"):
            self.sel = max(0, min(len(self.entries) - 1, self.sel + (1 if k.name == "DOWN" else -1)))
        elif k.name in ("PAGEUP", "PAGEDOWN"):
            self.sel = max(0, min(len(self.entries) - 1, self.sel + (10 if k.name == "PAGEDOWN" else -10)))
        elif k.name == "RETURN":
            self.open(self.sel)
        elif k.name == "BACKSPACE" and self.cwd:
            self.open(0)
        elif k.name in ("F5",):
            self.go(self.cwd)
        elif k.char and not k.ctrl:
            ch = k.char.lower()
            for j in list(range(self.sel + 1, len(self.entries))) + list(range(0, self.sel + 1)):
                if self.entries[j][0].lower().startswith(ch):
                    self.sel = j
                    break

    def _row_at(self, y):
        i = self.top + (y - BAR_H - 2) // ROW_H
        return i if y > BAR_H and 0 <= i < len(self.entries) else -1

    def mouse(self, x, y, buttons, pressed, released, wheel):
        if wheel:
            self.top = max(0, min(max(0, len(self.entries) - 3), self.top - wheel * 3))
        if pressed & 1:
            if y < BAR_H and x < 30 and self.cwd:
                self.open(0)
                return
            i = self._row_at(y)
            if i < 0:
                return
            last, t = self.last_click
            now = time.monotonic()
            self.sel = i
            if last == i and now - t < 0.4:
                self.last_click = (-1, 0.0)
                self.open(i)
            else:
                self.last_click = (i, now)

    def context_menu(self, x, y):
        i = self._row_at(y)
        if i >= 0:
            self.sel = i
        p = self.path_of(self.sel) if self.entries else self.cwd
        is_img = p.lower().endswith(IMAGE_EXT)
        return [{"id": "open", "label": "Open", "enabled": bool(self.entries)},
                {"id": "copy_path", "label": "Copy Path"},
                {"id": "wallpaper", "label": "Use as Wallpaper", "enabled": is_img},
                {"separator": True},
                {"id": "terminal", "label": "Open Terminal Here", "enabled": bool(self.cwd)},
                {"id": "refresh", "label": "Refresh", "shortcut": "F5"},
                {"id": "home", "label": "Home Folder"}]

    def on_menu(self, item_id):
        p = self.path_of(self.sel) if self.entries else self.cwd
        if item_id == "open":
            self.open(self.sel)
        elif item_id == "copy_path":
            self.desk.set_clipboard(p, "files")
        elif item_id == "wallpaper":
            self.desk.apply_settings({"desktop": {"wallpaper": p}})
        elif item_id == "terminal":
            self.desk.launch("terminal", cwd=self.cwd)
        elif item_id == "refresh":
            self.go(self.cwd)
        elif item_id == "home":
            self.go("~")

    def copy(self):
        return self.path_of(self.sel) if self.entries else self.cwd

    def paste(self, text):
        text = text.strip().strip('"')
        if os.path.isdir(os.path.expanduser(text)):
            self.go(text)

    # ----- drawing -----
    def draw(self, d, x, y, w, h):
        d.rectangle([x, y, x + w - 1, y + h - 1], fill=C.white)
        bevel(d, x, y, w, BAR_H, face=C.gray)
        bevel(d, x + 3, y + 3, 22, 18)
        d.polygon([(x + 8, y + 12), (x + 14, y + 6), (x + 20, y + 12)], fill=C.black if self.cwd else C.darkgray)
        d.rectangle([x + 12, y + 12, x + 16, y + 17], fill=C.black if self.cwd else C.darkgray)
        label = self.error or self.cwd or "Computer"
        while label and FONT.getlength(label) > w - 40:
            label = label[1:]
        d.text((x + 32, y + 5), label, font=FONT, fill=C.maroon if self.error else C.black)
        rows = max(1, (h - BAR_H - 4) // ROW_H)
        if self.sel < self.top:
            self.top = self.sel
        elif self.sel >= self.top + rows:
            self.top = self.sel - rows + 1
        for j, (name, is_dir, size, mtime) in enumerate(self.entries[self.top:self.top + rows]):
            i = self.top + j
            ry = y + BAR_H + 2 + j * ROW_H
            sel = i == self.sel
            if sel:
                d.rectangle([x, ry, x + w - 1, ry + ROW_H - 1], fill=self.desk.theme.colors["select"])
            col = self.desk.theme.colors["select_text"] if sel else C.black
            low = name.lower()
            glyph = "folder" if is_dir else "image" if low.endswith(IMAGE_EXT) else "star" if low.endswith(
                (".bsp", ".obj")) else \
                "web" if low.endswith((".html", ".htm")) else "doc" if low.endswith(TEXT_EXT) else "file"
            draw_glyph(d, x + 4, ry + 1, glyph, ROW_H - 2)
            d.text((x + ROW_H + 8, ry + 1), name[: max(1, (w - 190) // 7)], font=FONT, fill=col)
            if not is_dir:
                s = _size(size)
                d.text((x + w - 170 - text_size(s)[0] + 60, ry + 1), s, font=FONT, fill=col)
            if mtime:
                d.text((x + w - 100, ry + 1), time.strftime("%Y-%m-%d", time.localtime(mtime)), font=FONT, fill=col)

    def state(self):
        return {"cwd": self.cwd, "selected": self.path_of(self.sel) if self.entries else None,
                "entries": [{"name": n, "dir": dr, "size": s} for n, dr, s, _ in self.entries[:200]],
                "error": self.error}
