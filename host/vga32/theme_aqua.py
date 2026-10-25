"""Aqua: the early Mac OS X look (10.0-10.3) on a 16-color, RGB222 screen.

White pinstriped menu bar and title bars, red/yellow/green "traffic light" window buttons on
the left (they show x - + under the pointer), centered titles, soft window and menu shadows,
rounded corners, blue gel controls, and a Dock along the bottom with magnification, running
indicators, labels and minimized windows.

RGB222 has no light greys between 170 and 255, so pinstripes are white with 170-grey lines;
the blue palette entry is remapped to a brighter Aqua blue. The default wallpaper is drawn
procedurally (a blue "swoosh") and ordered-dithered onto the palette.
"""
import math
from datetime import datetime

import numpy as np
from PIL import Image

from . import ui
from .palette import C, PALETTE, ordered_dither
from .theme import Platinum
from .ui import text_size

AQUA_BLUE = (0, 85, 255)


def _checker(x0, y0, x1, y1, phase=0):
    return [(x, y) for y in range(y0, y1) for x in range(x0 + (y + phase) % 2, x1, 2)]


def _gel(d, box, radius, base, light, outline=C.darkgray):
    """An Aqua gel: base color with a lighter upper half and a white glint."""
    x0, y0, x1, y1 = box
    d.rounded_rectangle(box, radius=radius, fill=base, outline=outline)
    mid = y0 + (y1 - y0) // 2
    if y1 - y0 > 6:
        d.rounded_rectangle([x0 + 2, y0 + 2, x1 - 2, mid], radius=max(1, radius - 2), fill=light)
        d.line([x0 + radius, y0 + 2, x1 - radius, y0 + 2], fill=C.white)


class Aqua(Platinum):
    name = "aqua"
    label = "Aqua"
    title_h = 22
    bar_h = 22
    bar_top = True
    title_double_click = "minimize"
    item_h = 18
    dock_h = 48
    palette = list(PALETTE)
    palette[4] = AQUA_BLUE
    desktop = {"background": "blue", "pattern": "swoosh"}
    colors = {"face": C.white, "text": C.black, "highlight": C.white, "shadow": C.darkgray,
              "accent": C.blue, "accent_text": C.white, "window": C.white, "window_text": C.black,
              "select": C.blue, "select_text": C.white, "disabled": C.gray, "desktop_text": C.white}

    def __init__(self):
        super().__init__()
        self.title_font = self.menu_font          # OS X titles are regular weight

    # ----- geometry -----
    def work_area(self, W, H):
        return 0, self.bar_h, W, H - self.bar_h - self.dock_h

    def shaded_h(self):
        return self.title_h

    def frame_size(self, cw, ch):
        return cw + 2, ch + self.title_h + 1

    def client(self, win):
        if win.shaded:
            return win.x + 1, win.y + self.title_h, max(0, win.w - 2), 0
        return win.x + 1, win.y + self.title_h, win.w - 2, win.h - self.title_h - 1

    def boxes(self, win):
        y = win.y + 5
        return {"close": (win.x + 8, y, 13, 13), "min": (win.x + 28, y, 13, 13), "zoom": (win.x + 48, y, 13, 13)}

    def grip(self, win, px, py):
        return not win.shaded and px >= win.x + win.w - 14 and py >= win.y + win.h - 14

    # ----- windows -----
    def _stripes(self, d, x0, y0, x1, y1, every=2, phase=0):
        for yy in range(y0 + phase, y1, every):
            d.line([x0, yy, x1, yy], fill=C.gray)

    def draw_window(self, d, win, active):
        x, y, w, h = win.x, win.y, win.w, win.visible_h
        # soft shadow: a dithered band below and to the right
        d.point(_checker(x + 4, y + h, x + w + 4, y + h + 4) + _checker(x + w, y + 6, x + w + 4, y + h), fill=C.darkgray)
        d.point(_checker(x + 2, y + h, x + w + 2, y + h + 2, 1), fill=C.black)
        d.rounded_rectangle([x, y, x + w - 1, y + h - 1], radius=6, fill=C.white, outline=C.darkgray)
        if not win.shaded:
            d.rectangle([x, y + h - 7, x + w - 1, y + h - 1], fill=C.white, outline=C.darkgray)
        th = self.title_h
        self._stripes(d, x + 2, y + 3, x + w - 3, y + th - 1, 2 if active else 4, 1)
        d.line([x + 1, y + 1, x + w - 2, y + 1], fill=C.white)
        if not win.shaded:
            d.line([x, y + th - 1, x + w - 1, y + th - 1], fill=C.darkgray)
        title = win.title
        while title and self.title_font.getlength(title) > w - 150:
            title = title[:-1]
        tw = int(self.title_font.getlength(title))
        tx = x + (w - tw) // 2
        d.rectangle([tx - 6, y + 4, tx + tw + 5, y + th - 4], fill=C.white)
        d.text((tx, y + 4), title, font=self.title_font, fill=C.black if active else C.darkgray)
        b = self.boxes(win)
        bx0 = b["close"][0]
        mx, my = self.mouse
        hover = active and bx0 - 2 <= mx < b["zoom"][0] + 15 and y + 3 <= my < y + 20
        for name, base, light, glyph in (("close", C.red, C.orange, "x"), ("min", C.orange, C.yellow, "-"),
                                         ("zoom", C.green, C.lime, "+")):
            bx, by, bw, bh = b[name]
            if active:
                _gel(d, [bx, by, bx + bw - 1, by + bh - 1], bw // 2, base, light, C.darkgray)
            else:
                d.ellipse([bx, by, bx + bw - 1, by + bh - 1], fill=C.white, outline=C.gray)
                d.ellipse([bx + 3, by + 3, bx + bw - 4, by + bh - 4], fill=C.gray)
            if hover:
                cx, cy = bx + bw // 2, by + bh // 2
                if glyph == "x":
                    d.line([cx - 2, cy - 2, cx + 2, cy + 2], fill=C.maroon)
                    d.line([cx - 2, cy + 2, cx + 2, cy - 2], fill=C.maroon)
                else:
                    d.line([cx - 3, cy, cx + 3, cy], fill=C.maroon)
                    if glyph == "+":
                        d.line([cx, cy - 3, cx, cy + 3], fill=C.maroon)

    def draw_grip(self, d, win):
        x, y = win.x + win.w - 3, win.y + win.h - 3
        for k in (3, 7, 11):
            d.line([x - k, y, x, y - k], fill=C.gray)
            d.line([x - k + 1, y, x, y - k + 1], fill=C.white)

    # ----- menu bar -----
    def bar_items(self, desk):
        top = desk.windows[-1] if desk.windows and not desk.windows[-1].minimized else None
        items = [{"kind": "menu", "menu": "system", "rect": (6, 0, 28, self.bar_h), "label": ""}]
        x = 36
        name = top.app.title if top is not None else "Finder"
        nw = int(self.bold_menu_font.getlength(name)) + 20
        items.append({"kind": "menu", "menu": "appname", "rect": (x, 0, nw, self.bar_h), "label": name, "bold": True})
        x += nw
        if top is not None:
            for title, _ in desk.app_menus(top):
                tw = int(self.menu_font.getlength(title)) + 18
                if x + tw > desk.W - 170:
                    break
                items.append({"kind": "menu", "menu": ("app", title), "rect": (x, 0, tw, self.bar_h), "label": title})
                x += tw
        items.append({"kind": "menu", "menu": "launcher", "rect": (desk.W - 26, 0, 22, self.bar_h), "label": ""})
        now = datetime.now()
        clock = now.strftime("%a ") + desk.clock_text()
        cw = int(self.menu_font.getlength(clock)) + 16
        items.append({"kind": "clock", "rect": (desk.W - 30 - cw, 0, cw, self.bar_h), "label": clock})
        return items

    def draw_bar(self, desk, d, items):
        W, bh = desk.W, self.bar_h
        d.rectangle([0, 0, W - 1, bh - 1], fill=C.white)
        self._stripes(d, 0, 1, W - 1, bh - 2, 4, 2)
        d.line([0, bh - 1, W - 1, bh - 1], fill=C.darkgray)
        d.point(_checker(0, bh, W, bh + 2), fill=C.darkgray)     # the bar's soft shadow
        for it in items:
            x, y, w, h = it["rect"]
            open_ = it["kind"] == "menu" and desk.menu_from(it["menu"])
            if open_:
                d.rectangle([x, 0, x + w - 1, h - 2], fill=C.blue)
            col = C.white if open_ else C.black
            if it.get("menu") == "system":       # a blue orb in place of a vendor logo
                cx, cy = x + w // 2, h // 2
                _gel(d, [cx - 7, cy - 7, cx + 7, cy + 7], 7, C.blue, C.sky, C.navy)
                continue
            if it.get("menu") == "launcher":     # a magnifier, like the search menu
                cx, cy = x + 9, h // 2 - 2
                d.ellipse([cx - 5, cy - 5, cx + 5, cy + 5], outline=col, width=2)
                d.line([cx + 4, cy + 4, cx + 9, cy + 9], fill=col, width=3)
                continue
            font = self.bold_menu_font if it.get("bold") else self.menu_font
            d.text((x + 9, (h - text_size("Mg", font)[1]) // 2 - 1), it["label"], font=font, fill=col)

    # ----- menus -----
    def draw_menu(self, d, m):
        x, y, w, h = m.rect
        d.point(_checker(x + 3, y + h, x + w + 3, y + h + 4) + _checker(x + w, y + 4, x + w + 3, y + h), fill=C.darkgray)
        d.rounded_rectangle([x, y, x + w - 1, y + h - 1], radius=5, fill=C.white, outline=C.gray)
        d.rectangle([x, y, x + w - 1, y + 6], fill=C.white, outline=None)
        d.line([x, y, x, y + 6], fill=C.gray)
        d.line([x + w - 1, y, x + w - 1, y + 6], fill=C.gray)
        for i, it, (ix, iy, iw, ih) in m.rows():
            if it.get("separator"):
                d.line([ix + 1, iy + ih // 2, ix + iw - 2, iy + ih // 2], fill=C.gray)
                continue
            self._menu_row(d, m, i, it, ix - 2, iy, iw + 4, ih)

    # ----- Dock -----
    def dock_zone(self, desk):
        return 0, desk.H - self.dock_h, desk.W, self.dock_h

    def dock_items(self, desk):
        base, big, gap = 32, 46, 6
        raw = [{"kind": "app", "entry": e, "windows": wins, "glyph": e.get("icon", "app"), "label": e["title"],
                "running": bool(wins)} for e, wins in desk.dock_apps()]
        mins = [w for w in reversed(desk.windows) if w.minimized]
        raw.append({"kind": "sep"})
        raw += [{"kind": "window", "win": w, "glyph": getattr(w.app, "icon", "window"), "label": w.title,
                 "running": False} for w in mins]
        raw.append({"kind": "launcher", "glyph": "folder", "label": "Applications", "running": False})
        # first pass at base size to find centers, then magnify around the pointer
        widths = [10 if it["kind"] == "sep" else base for it in raw]
        total = sum(widths) + gap * (len(raw) - 1)
        x = (desk.W - total) // 2
        centers = []
        for wdt in widths:
            centers.append(x + wdt / 2)
            x += wdt + gap
        hover = getattr(desk, "dock_hover", False)
        sizes = []
        for it, c in zip(raw, centers):
            if it["kind"] == "sep":
                sizes.append(10)
            elif hover:
                k = max(0.0, 1 - abs(desk.mx - c) / 70)
                sizes.append(int(base + (big - base) * (0.5 - 0.5 * math.cos(math.pi * k))))
            else:
                sizes.append(base)
        total = sum(sizes) + gap * (len(raw) - 1)
        x = (desk.W - total) // 2
        bottom = desk.H - 8
        for it, s in zip(raw, sizes):
            it["rect"] = (x, bottom - s, s, s) if it["kind"] != "sep" else (x, bottom - base, s, base)
            x += s + gap
        return raw

    def draw_dock(self, desk, d, items):
        if not items:
            return
        x0 = items[0]["rect"][0] - 8
        x1 = items[-1]["rect"][0] + items[-1]["rect"][2] + 8
        y0 = desk.H - 44
        d.point(_checker(x0 + 2, y0 + 2, x1 - 2, desk.H - 2), fill=C.white)       # frosted glass shelf
        d.rounded_rectangle([x0, y0, x1, desk.H - 1], radius=6, outline=C.gray)
        d.line([x0 + 4, y0 + 1, x1 - 4, y0 + 1], fill=C.white)
        hovered = None
        for it in items:
            x, y, s, _ = it["rect"]
            if it["kind"] == "sep":
                d.line([x + 5, y + 2, x + 5, desk.H - 4], fill=C.gray)
                continue
            ui.draw_glyph(d, x, y, it["glyph"], s)
            if it["kind"] == "window":           # a minimized window: small title bar over its icon
                d.rectangle([x, y + s - 8, x + s - 1, y + s - 1], fill=C.white, outline=C.darkgray)
                d.rectangle([x + 2, y + s - 6, x + 5, y + s - 3], fill=C.red)
            if it.get("running"):
                cx = x + s // 2
                d.polygon([(cx - 3, desk.H - 1), (cx + 3, desk.H - 1), (cx, desk.H - 4)], fill=C.black)
            if getattr(desk, "dock_hover", False) and x <= desk.mx < x + s and desk.H - 48 <= desk.my:
                hovered = it
        if hovered is not None and not desk.menus:
            x, y, s, _ = hovered["rect"]
            label = hovered["label"]
            tw = int(self.menu_font.getlength(label))
            lx = max(2, min(desk.W - tw - 18, x + s // 2 - tw // 2 - 8))
            ly = y - 22
            d.rounded_rectangle([lx, ly, lx + tw + 16, ly + 17], radius=8, fill=C.darkgray)
            d.text((lx + 8, ly + 1), label, font=self.menu_font, fill=C.white)

    # ----- widgets -----
    def draw_button(self, d, r, label, pressed=False, default=False, enabled=True, focused=False):
        x, y, w, h = r
        rad = h // 2
        if focused and not default:
            d.rounded_rectangle([x - 2, y - 2, x + w + 1, y + h + 1], radius=rad + 2, outline=C.sky, width=2)
        if not enabled:
            d.rounded_rectangle([x, y, x + w - 1, y + h - 1], radius=rad, fill=C.white, outline=C.gray)
        elif default or pressed:
            _gel(d, [x, y, x + w - 1, y + h - 1], rad, C.navy if pressed else C.blue, C.blue if pressed else C.sky,
                 C.navy)
        else:
            d.rounded_rectangle([x, y, x + w - 1, y + h - 1], radius=rad, fill=C.white, outline=C.darkgray)
            d.line([x + rad, y + h - 3, x + w - rad, y + h - 3], fill=C.gray)
        if label:
            self._label(d, r, label, C.black if enabled else C.gray)

    def draw_check(self, d, x, y, on, radio=False, enabled=True):
        box = [x, y, x + 12, y + 12]
        if radio:
            if on and enabled:
                _gel(d, box, 6, C.blue, C.sky, C.navy)
                d.ellipse([x + 4, y + 4, x + 8, y + 8], fill=C.black)
            else:
                d.ellipse(box, fill=C.white, outline=C.darkgray if enabled else C.gray)
                d.arc(box, 200, 340, fill=C.gray)
            return
        if on and enabled:
            _gel(d, box, 3, C.blue, C.sky, C.navy)
            d.line([x + 3, y + 6, x + 5, y + 9, x + 10, y + 2], fill=C.black, width=2)
        else:
            d.rounded_rectangle(box, radius=3, fill=C.white, outline=C.darkgray if enabled else C.gray)
            d.line([x + 2, y + 2, x + 10, y + 2], fill=C.gray)
            if on:
                d.line([x + 3, y + 6, x + 5, y + 9, x + 10, y + 2], fill=C.gray, width=2)

    def draw_field(self, d, r, focused=False, enabled=True):
        x, y, w, h = r
        if focused:
            d.rectangle([x - 2, y - 2, x + w + 1, y + h + 1], outline=C.sky, width=2)
        d.rectangle([x, y, x + w - 1, y + h - 1], fill=C.white, outline=C.darkgray)
        d.line([x + 1, y + 1, x + w - 2, y + 1], fill=C.gray)          # inset shadow along the top
        return x + 3, y + 2, w - 6, h - 4

    def draw_slider(self, d, r, frac, focused=False, enabled=True):
        x, y, w, h = r
        cy = y + h // 2
        kx = x + int((w - 14) * frac)
        d.rounded_rectangle([x, cy - 3, x + w - 1, cy + 2], radius=3, fill=C.white, outline=C.darkgray)
        if kx > x + 2 and enabled:
            d.rounded_rectangle([x + 1, cy - 2, kx + 7, cy + 1], radius=2, fill=C.sky)
        knob = [kx, cy - 7, kx + 13, cy + 6]
        if enabled:
            _gel(d, knob, 7, C.blue, C.sky, C.navy)
        else:
            d.ellipse(knob, fill=C.white, outline=C.gray)
        if focused:
            d.ellipse([kx - 2, cy - 9, kx + 15, cy + 8], outline=C.sky)

    def draw_progress(self, d, r, frac, phase=0):
        x, y, w, h = r
        rad = h // 2
        d.rounded_rectangle([x, y, x + w - 1, y + h - 1], radius=rad, fill=C.white, outline=C.darkgray)
        fw = int((w - 2) * max(0.0, min(1.0, frac)))
        if fw > 4:
            d.rounded_rectangle([x + 1, y + 1, x + fw, y + h - 2], radius=max(1, rad - 1), fill=C.blue)
            off = int(phase * 12) % 8                       # the moving barber-pole stripes
            for sx in range(x - h + off, x + fw - h - 1, 8):      # stripes stay inside the filled part
                d.polygon([(sx, y + h - 2), (sx + 4, y + h - 2), (sx + h + 1, y + 1), (sx + h - 3, y + 1)],
                          fill=C.sky)
            d.rounded_rectangle([x + 1, y + 1, x + fw, y + h - 2], radius=max(1, rad - 1), outline=C.navy)
            d.line([x + rad, y + 2, x + fw - rad, y + 2], fill=C.white)

    def draw_tab(self, d, r, label, active=False, focused=False):
        x, y, w, h = r
        if active:
            _gel(d, [x, y + 1, x + w - 1, y + h - 1], 6, C.blue, C.sky, C.navy)
        else:
            d.rounded_rectangle([x, y + 1, x + w - 1, y + h - 1], radius=6, fill=C.white, outline=C.darkgray)
        self._label(d, (x, y + 1, w, h - 1), label, C.black)
        if focused:
            d.rounded_rectangle([x - 2, y - 1, x + w + 1, y + h + 1], radius=8, outline=C.sky)

    def draw_tab_page(self, d, r):
        x, y, w, h = r
        d.rounded_rectangle([x, y + 10, x + w - 1, y + h - 1], radius=5, fill=C.white, outline=C.gray)
        self._stripes(d, x + 2, y + 13, x + w - 3, y + h - 3, 4, 1)

    def draw_scrollbar(self, d, r, start, end):
        x, y, w, h = r
        d.rectangle([x, y, x + w - 1, y + h - 1], fill=C.white, outline=C.gray)
        self._stripes(d, x + 1, y + 1, x + w - 2, y + h - 2, 2, 0)
        if end - start >= 0.999:
            return
        ty = y + int(h * start)
        th = max(14, int(h * (end - start)))
        ty = min(ty, y + h - th)
        _gel(d, [x + 1, ty, x + w - 2, ty + th - 1], (w - 2) // 2, C.blue, C.sky, C.navy)

    def draw_switch(self, d, r, on, enabled=True, focused=False):
        x, y, w, h = r
        if on and enabled:
            _gel(d, [x, y, x + w - 1, y + h - 1], h // 2, C.blue, C.sky, C.navy)
        else:
            d.rounded_rectangle([x, y, x + w - 1, y + h - 1], radius=h // 2, fill=C.white, outline=C.darkgray)
        kx = x + w - h if on else x
        d.ellipse([kx, y, kx + h - 1, y + h - 1], fill=C.white, outline=C.darkgray)
        if focused:
            d.rounded_rectangle([x - 2, y - 2, x + w + 1, y + h + 1], radius=h // 2 + 2, outline=C.sky)

    def draw_header(self, d, r, label):
        x, y, w, h = r
        d.rectangle([x, y, x + w - 1, y + h - 1], fill=C.white)
        d.line([x, y + h - 1, x + w - 1, y + h - 1], fill=C.gray)
        d.line([x + w - 1, y + 3, x + w - 1, y + h - 4], fill=C.gray)
        d.text((x + 5, y + (h - 14) // 2), label, font=self.menu_font, fill=C.black)

    def draw_list(self, d, r):
        x, y, w, h = r
        d.rectangle([x, y, x + w - 1, y + h - 1], fill=C.white, outline=C.darkgray)
        return x + 1, y + 1, w - 2, h - 2

    def draw_group(self, d, r, title=""):
        x, y, w, h = r
        d.rounded_rectangle([x, y + 7, x + w - 1, y + h - 1], radius=5, outline=C.gray)
        if title:
            tw = int(self.menu_font.getlength(title))
            d.rectangle([x + 8, y, x + 14 + tw, y + 14], fill=self.colors["face"])
            d.text((x + 11, y), title, font=self.menu_font, fill=C.black)

    # ----- desktop -----
    def draw_icon(self, d, x, y, label, glyph, selected):
        if selected:
            d.rounded_rectangle([x + 4, y - 3, x + 44, y + 36], radius=4, fill=C.darkgray)
        ui.draw_glyph(d, x + 8, y, glyph, 32)
        lw = min(80, int(self.menu_font.getlength(label)))
        lx = x + 24 - lw // 2
        if selected:
            d.rounded_rectangle([lx - 4, y + 34, lx + lw + 3, y + 49], radius=7, fill=C.blue)
        else:
            d.text((lx + 1, y + 36), label, font=self.menu_font, fill=C.black)     # drop shadow
        d.text((lx, y + 35), label, font=self.menu_font, fill=C.white)

    def background_pattern(self, bd, W, H, base, area, img=None):
        """The Aqua 'swoosh': a deep blue gradient with sweeping light bands, dithered."""
        if img is None:
            return super().background_pattern(bd, W, H, base, area)
        x0, y0, aw, ah = area
        ah += self.dock_h
        r, g, b = (np.array(base, np.float32) / 255.0)
        yy, xx = np.mgrid[0:ah, 0:aw].astype(np.float32)
        t = yy / max(1, ah)
        light = np.zeros_like(t)
        for amp, freq, phase, off, width, gain in ((0.18, 1.6, 0.4, 0.42, 0.05, 0.9), (0.22, 1.2, 2.0, 0.62, 0.09, 0.6),
                                                    (0.12, 2.3, 4.1, 0.30, 0.03, 0.7), (0.25, 0.9, 1.1, 0.80, 0.14, 0.45)):
            curve = off + amp * np.sin(xx / aw * freq * math.pi + phase)
            light += gain * np.exp(-((t - curve) / width) ** 2)
        shade = 0.35 + 0.55 * t
        rgb = np.stack([(r * 0.6 + 0.05) * shade + 0.55 * light,
                        (g * 0.6 + 0.20) * shade + 0.65 * light,
                        (b * 0.9 + 0.35) * shade + 0.75 * light], -1)
        im = Image.fromarray((np.clip(rgb, 0, 1) * 255).astype(np.uint8))
        img.paste(ordered_dither(im), (x0, y0))
        return C.white
