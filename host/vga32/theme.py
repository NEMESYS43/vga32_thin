"""Themes: everything about how the desktop chrome looks and where things sit.

A theme owns window frames (metrics, title bar, boxes), the bar (a bottom taskbar or a top
menu bar), popup menus, the display palette, the mouse cursor and the semantic colors apps
can use (theme.colors["face"], or "face" anywhere a color name is accepted).

Desktop logic only asks the theme for rectangles and hands it things to paint, so adding a
theme means subclassing Theme and registering it in THEMES.
"""
import glob
import os
from datetime import datetime

from PIL import ImageFont

from . import palette, ui
from .palette import C, PALETTE
from .settings import FONTS_DIR
from .ui import CH, CW, FONT, bevel, text_size


def _font(candidates, size):
    for name in candidates:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            pass
    return FONT


class Theme:
    name = "classic"
    label = "Classic"
    title_h = 18
    bar_h = 24
    bar_top = False             # True: a menu bar across the top instead of a taskbar
    title_double_click = "maximize"     # maximize | shade | minimize
    item_h = 20                 # popup menu row height
    palette = list(PALETTE)
    desktop = {"background": "teal", "pattern": "dots"}
    colors = {"face": C.gray, "text": C.black, "highlight": C.white, "shadow": C.darkgray,
              "accent": C.blue, "accent_text": C.white, "window": C.white, "window_text": C.black,
              "select": C.blue, "select_text": C.white, "disabled": C.darkgray, "desktop_text": C.white}

    def __init__(self):
        self.title_font = FONT
        self.menu_font = FONT
        self.mouse = (-1, -1)       # pointer position, set by the desktop before each frame

    # ----- geometry -----
    def work_area(self, W, H):
        """(x, y, w, h) that windows live in."""
        return (0, self.bar_h, W, H - self.bar_h) if self.bar_top else (0, 0, W, H - self.bar_h)

    def bar_rect(self, W, H):
        return (0, 0, W, self.bar_h) if self.bar_top else (0, H - self.bar_h, W, self.bar_h)

    def shaded_h(self):
        return self.title_h + 4

    def frame_size(self, cw, ch):
        """Outer window size for a client area of cw x ch."""
        return cw + 4, ch + self.title_h + 4

    def client(self, win):
        if win.shaded:
            return win.x + 2, win.y + self.title_h + 2, max(0, win.w - 4), 0
        return win.x + 2, win.y + self.title_h + 2, win.w - 4, win.h - self.title_h - 4

    def boxes(self, win):
        """Title bar buttons: name -> (x, y, w, h). Names: close, zoom, shade."""
        return {"close": (win.x + win.w - 18, win.y + 3, 14, 13),
                "zoom": (win.x + win.w - 34, win.y + 3, 14, 13),
                "shade": (win.x + win.w - 50, win.y + 3, 14, 13)}

    def in_title(self, win, px, py):
        return win.y <= py < win.y + self.title_h + 2

    def grip(self, win, px, py):
        return not win.shaded and px >= win.x + win.w - 10 and py >= win.y + win.h - 10

    # ----- windows -----
    def draw_window(self, d, win, active):
        h = win.visible_h
        bevel(d, win.x, win.y, win.w, h, face=self.colors["face"])
        d.rectangle([win.x + 2, win.y + 2, win.x + win.w - 3, win.y + self.title_h],
                    fill=C.blue if active else C.darkgray)
        maxc = max(1, (win.w - 62) // CW)
        d.text((win.x + 6, win.y + 4), win.title[:maxc], font=FONT, fill=C.white if active else C.gray)
        b = self.boxes(win)
        cx, cy, cw, ch = b["close"]
        bevel(d, cx, cy, cw, ch)
        d.line([cx + 4, cy + 3, cx + cw - 5, cy + ch - 4], fill=C.black, width=2)
        d.line([cx + cw - 5, cy + 3, cx + 4, cy + ch - 4], fill=C.black, width=2)
        mx, my, mw, mh = b["zoom"]
        bevel(d, mx, my, mw, mh)
        d.rectangle([mx + 3, my + 3, mx + mw - 4, my + mh - 4], outline=C.black)
        d.line([mx + 3, my + 4, mx + mw - 4, my + 4], fill=C.black)
        sx, sy, sw, sh = b["shade"]
        bevel(d, sx, sy, sw, sh)
        d.line([sx + 3, sy + sh - 4, sx + sw - 5, sy + sh - 4], fill=C.black, width=2)

    def draw_grip(self, d, win):
        gx, gy = win.x + win.w - 3, win.y + win.h - 3
        for k in (3, 6, 9):
            d.line([gx - k, gy, gx, gy - k], fill=C.darkgray)

    # ----- bar -----
    def bar_items(self, desk):
        """Clickable bar parts: dicts with rect, kind (start|task|menu|clock), label, and win or menu."""
        W, H = desk.W, desk.H
        ty = H - self.bar_h
        items = [{"kind": "start", "rect": (2, ty + 2, 58, self.bar_h - 4), "label": "Start"}]
        wins = desk.windows
        bw = 96 if not wins else min(110, (W - 64 - 70) // len(wins))
        x = 64
        for w in wins:
            items.append({"kind": "task", "rect": (x, ty + 2, bw, self.bar_h - 4), "label": w.title, "win": w})
            x += bw + 2
        items.append({"kind": "clock", "rect": (W - 62, ty + 2, 60, self.bar_h - 4), "label": desk.clock_text()})
        return items

    def draw_bar(self, desk, d, items):
        bx, by, bw, bh = self.bar_rect(desk.W, desk.H)
        bevel(d, bx, by, bw, bh, face=self.colors["face"])
        top = desk.windows[-1] if desk.windows else None
        for it in items:
            x, y, w, h = it["rect"]
            if it["kind"] == "start":
                bevel(d, x, y, w, h, raised=not desk.menu_from("start"))
                d.text((x + 8, y + 3), it["label"], font=FONT, fill=C.black)
            elif it["kind"] == "task":
                on = it["win"] is top
                bevel(d, x, y, w, h, raised=not on, face=C.white if on else C.gray)
                d.text((x + 5, y + 3), it["label"][:max(1, (w - 8) // CW)], font=FONT, fill=C.black)
            elif it["kind"] == "clock":
                bevel(d, x, y, w, h, raised=False)
                tw = text_size(it["label"])[0]
                d.text((x + (w - tw) // 2, y + 3), it["label"], font=FONT, fill=C.black)

    # ----- popup menus -----
    def draw_menu(self, d, m):
        x, y, w, h = m.rect
        bevel(d, x, y, w, h, face=self.colors["face"])
        for i, it, (ix, iy, iw, ih) in m.rows():
            if it.get("separator"):
                d.line([ix + 2, iy + ih // 2, ix + iw - 3, iy + ih // 2], fill=C.darkgray)
                d.line([ix + 2, iy + ih // 2 + 1, ix + iw - 3, iy + ih // 2 + 1], fill=C.white)
                continue
            self._menu_row(d, m, i, it, ix, iy, iw, ih)

    def _menu_row(self, d, m, i, it, ix, iy, iw, ih):
        enabled = it.get("enabled", True)
        hot = i == m.hover and enabled
        if hot:
            d.rectangle([ix, iy, ix + iw - 1, iy + ih - 1], fill=self.colors["select"])
        col = self.colors["select_text"] if hot else (self.colors["text"] if enabled else self.colors["disabled"])
        ty = iy + (ih - text_size("Mg", self.menu_font)[1]) // 2 - 1
        if it.get("checked"):
            d.text((ix + 4, ty), "✓" if self.menu_font is not FONT else "*", font=self.menu_font, fill=col)
        d.text((ix + 16, ty), it.get("label", ""), font=self.menu_font, fill=col)
        right = ix + iw - 8
        if it.get("items") is not None:
            cy = iy + ih // 2
            d.polygon([(right - 4, cy - 4), (right, cy), (right - 4, cy + 4)], fill=col)
        elif it.get("shortcut"):
            sw = int(self.menu_font.getlength(it["shortcut"]))
            d.text((right - sw, ty), it["shortcut"], font=self.menu_font, fill=col)

    # ----- desktop icons -----
    def draw_icon(self, d, x, y, label, glyph, selected):
        ui.draw_glyph(d, x + 8, y, glyph, 32)
        lw = min(72, text_size(label)[0])
        lx = x + 24 - lw // 2
        if selected:
            d.rectangle([lx - 2, y + 34, lx + lw + 1, y + 34 + CH], fill=self.colors["select"])
        d.text((lx, y + 35), label[:72 // CW], font=FONT,
               fill=self.colors["select_text"] if selected else self.colors["desktop_text"])

    def cursor(self):
        """None for the board's built-in arrow, or (rows of palette indices, hx, hy)."""
        return None

    # ----- Dock (only themes with one) -----
    def dock_zone(self, desk):
        """(x, y, w, h) the Dock owns, or None."""
        return None

    def dock_items(self, desk):
        return []

    def draw_dock(self, desk, d, items):
        pass

    # ----- widgets (widgets.py and panels draw through these, so they follow the theme) -----
    def draw_button(self, d, r, label, pressed=False, default=False, enabled=True, focused=False):
        x, y, w, h = r
        bevel(d, x, y, w, h, raised=not pressed, face=self.colors["face"])
        if default or focused:
            d.rectangle([x - 1, y - 1, x + w, y + h], outline=C.black)
        self._label(d, r, label, self.colors["text"] if enabled else self.colors["disabled"], pressed)

    def _label(self, d, r, label, col, pressed=False, font=None):
        x, y, w, h = r
        font = font or self.menu_font
        tw = int(font.getlength(label))
        th = text_size("Mg", font)[1]
        d.text((x + (w - tw) // 2 + pressed, y + (h - th) // 2 - 1 + pressed), label, font=font, fill=col)

    def draw_check(self, d, x, y, on, radio=False, enabled=True):
        """A 13x13 checkbox or radio button at (x, y)."""
        bg = C.white if enabled else C.gray
        if radio:
            d.ellipse([x, y, x + 12, y + 12], fill=bg, outline=C.darkgray)
            d.arc([x, y, x + 12, y + 12], 45, 225, fill=C.darkgray)
            if on:
                d.ellipse([x + 4, y + 4, x + 8, y + 8], fill=C.black)
            return
        bevel(d, x, y, 13, 13, raised=False, face=bg)
        if on:
            d.line([x + 3, y + 6, x + 5, y + 9, x + 10, y + 3], fill=C.black, width=2)

    def draw_field(self, d, r, focused=False, enabled=True):
        """A text field frame; returns the inner (x, y, w, h) to put text in."""
        x, y, w, h = r
        bevel(d, x, y, w, h, raised=False, face=C.white if enabled else C.gray)
        return x + 3, y + 2, w - 6, h - 4

    def draw_slider(self, d, r, frac, focused=False, enabled=True):
        x, y, w, h = r
        cy = y + h // 2
        bevel(d, x, cy - 2, w, 4, raised=False, face=C.white)
        kx = x + int((w - 10) * frac)
        bevel(d, kx, y + 1, 10, h - 2, face=self.colors["face"])
        if focused:
            d.rectangle([kx + 2, y + 3, kx + 7, y + h - 4], outline=C.black)

    def draw_progress(self, d, r, frac, phase=0):
        x, y, w, h = r
        bevel(d, x, y, w, h, raised=False, face=C.white)
        fw = int((w - 4) * max(0.0, min(1.0, frac)))
        for bx in range(x + 2, x + 2 + fw - 7, 9):     # Windows-style blocks
            d.rectangle([bx, y + 2, bx + 6, y + h - 3], fill=self.colors["select"])

    def draw_list(self, d, r):
        x, y, w, h = r
        bevel(d, x, y, w, h, raised=False, face=C.white)
        return x + 2, y + 2, w - 4, h - 4

    def draw_tab(self, d, r, label, active=False, focused=False):
        x, y, w, h = r
        if not active:
            y, h = y + 2, h - 2
        d.rectangle([x, y, x + w - 1, y + h + (1 if active else 0)], fill=self.colors["face"])
        d.line([x, y + h, x, y + 2, x + 2, y, x + w - 3, y], fill=C.white)
        d.line([x + w - 2, y + 1, x + w - 1, y + 2, x + w - 1, y + h], fill=C.black)
        d.line([x + w - 2, y + 2, x + w - 2, y + h], fill=C.darkgray)
        self._label(d, (x, y, w, h), label, self.colors["text"])
        if focused:
            d.rectangle([x + 4, y + 3, x + w - 5, y + h - 3], outline=C.darkgray)

    def draw_tab_page(self, d, r):
        x, y, w, h = r
        bevel(d, x, y, w, h, face=self.colors["face"])

    def draw_scrollbar(self, d, r, start, end):
        """Vertical scrollbar; start / end are the visible fraction of the content (0..1)."""
        x, y, w, h = r
        d.rectangle([x, y, x + w - 1, y + h - 1], fill=C.white)
        d.point([(px, py) for py in range(y, y + h) for px in range(x + py % 2, x + w, 2)], fill=C.gray)
        if end - start >= 0.999:
            return
        ty = y + int(h * start)
        th = max(10, int(h * (end - start)))
        bevel(d, x, min(ty, y + h - th), w, th, face=self.colors["face"])

    def draw_switch(self, d, r, on, enabled=True, focused=False):
        x, y, w, h = r
        bevel(d, x, y, w, h, raised=False, face=self.colors["select"] if on else C.white)
        kx = x + w - h if on else x
        bevel(d, kx, y, h, h, face=self.colors["face"])
        if focused:
            d.rectangle([x - 2, y - 2, x + w + 1, y + h + 1], outline=C.darkgray)

    def draw_header(self, d, r, label):
        x, y, w, h = r
        bevel(d, x, y, w, h, face=self.colors["face"])
        d.text((x + 4, y + (h - 14) // 2), label, font=self.menu_font, fill=self.colors["text"])

    def draw_group(self, d, r, title=""):
        x, y, w, h = r
        d.rectangle([x, y + 6, x + w - 2, y + h - 2], outline=C.darkgray)
        d.rectangle([x + 1, y + 7, x + w - 1, y + h - 1], outline=C.white)
        if title:
            tw = int(self.menu_font.getlength(title))
            d.rectangle([x + 6, y, x + 12 + tw, y + 12], fill=self.colors["face"])
            d.text((x + 9, y - 1), title, font=self.menu_font, fill=self.colors["text"])

    def background_pattern(self, bd, W, H, base, area, img=None):
        x0, y0, aw, ah = area
        accent = C.cyan if base != C.cyan else C.teal
        for i in range(x0, x0 + aw, 8):
            for j in range(y0, y0 + ah, 8):
                bd.point((i, j), fill=accent)
        return accent


# Platinum: grey, pinstriped title bars, a menu bar along the top, boxes on both ends.
STRIPE_A, STRIPE_B = C.white, C.gray


class Platinum(Theme):
    name = "platinum"
    label = "Platinum"
    title_h = 19
    bar_h = 20
    bar_top = True
    title_double_click = "shade"
    item_h = 18
    # RGB222 has no #DDDDDD, so the platinum face is the 170 grey; sky becomes the lavender
    # highlight and teal a desktop blue-violet. Index 0 and 15 stay black and white for the cursor.
    palette = list(PALETTE)
    palette[5] = (170, 170, 255)
    palette[6] = (85, 85, 170)
    desktop = {"background": "teal", "pattern": "checker"}
    colors = {"face": C.gray, "text": C.black, "highlight": C.white, "shadow": C.darkgray,
              "accent": C.sky, "accent_text": C.black, "window": C.white, "window_text": C.black,
              "select": C.navy, "select_text": C.white, "disabled": C.darkgray, "desktop_text": C.white}

    def __init__(self):
        super().__init__()
        own = sorted(glob.glob(os.path.join(FONTS_DIR, "*.ttf")) + glob.glob(os.path.join(FONTS_DIR, "*.otf")))
        bold = own + ["DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf", "arialbd.ttf", "Arial Bold.ttf",
                      "Helvetica.ttc"]
        self.title_font = _font(bold, 12)
        self.menu_font = _font(own + ["DejaVuSans.ttf", "LiberationSans-Regular.ttf", "arial.ttf",
                                      "Helvetica.ttc"], 12)
        self.bold_menu_font = _font(bold, 12)

    def shaded_h(self):
        return self.title_h + 2

    def frame_size(self, cw, ch):
        return cw + 10, ch + self.title_h + 5

    def client(self, win):
        if win.shaded:
            return win.x + 5, win.y + self.title_h, max(0, win.w - 10), 0
        return win.x + 5, win.y + self.title_h, win.w - 10, win.h - self.title_h - 5

    def boxes(self, win):
        y = win.y + 4
        return {"close": (win.x + 8, y, 11, 11),
                "zoom": (win.x + win.w - 36, y, 11, 11),
                "shade": (win.x + win.w - 20, y, 11, 11)}

    def grip(self, win, px, py):
        return not win.shaded and px >= win.x + win.w - 14 and py >= win.y + win.h - 14

    def _box(self, d, r, active):
        x, y, w, h = r
        if not active:
            return
        d.rectangle([x - 1, y - 1, x + w, y + h], fill=C.gray)
        d.rectangle([x, y, x + w - 1, y + h - 1], fill=C.gray, outline=C.black)
        d.line([x + 1, y + h - 2, x + 1, y + 1, x + w - 2, y + 1], fill=C.white)
        d.line([x + w - 2, y + 2, x + w - 2, y + h - 2, x + 2, y + h - 2], fill=C.darkgray)

    def draw_window(self, d, win, active):
        x, y, w, h = win.x, win.y, win.w, win.visible_h
        d.rectangle([x, y, x + w - 1, y + h - 1], fill=C.gray, outline=C.black)
        d.line([x + 1, y + h - 2, x + 1, y + 1, x + w - 2, y + 1], fill=C.white)
        d.line([x + w - 2, y + 2, x + w - 2, y + h - 2, x + 2, y + h - 2], fill=C.darkgray)
        th = self.title_h
        title = win.title
        while title and self.title_font.getlength(title) > w - 90:
            title = title[:-1]
        tw = int(self.title_font.getlength(title))
        tx = x + (w - tw) // 2
        if active:
            for sy in range(y + 4, y + th - 3, 2):     # pinstripes, broken around the title
                d.line([x + 4, sy, x + w - 5, sy], fill=STRIPE_A)
                d.line([x + 4, sy + 1, x + w - 5, sy + 1], fill=STRIPE_B)
            d.rectangle([tx - 6, y + 2, tx + tw + 5, y + th - 3], fill=C.gray)
        d.text((tx, y + 3), title, font=self.title_font, fill=C.black if active else C.darkgray)
        b = self.boxes(win)
        for name in ("close", "zoom", "shade"):
            self._box(d, b[name], active)
        if active:
            zx, zy, zw, zh = b["zoom"]
            d.rectangle([zx + 2, zy + 2, zx + 6, zy + 6], outline=C.black)
            sx, sy, sw, sh = b["shade"]
            d.line([sx + 2, sy + 4, sx + sw - 3, sy + 4], fill=C.black)
            d.line([sx + 2, sy + 6, sx + sw - 3, sy + 6], fill=C.black)
        if not win.shaded:
            cx, cy, cw, ch = self.client(win)
            d.rectangle([cx - 1, cy - 1, cx + cw, cy + ch], outline=C.darkgray)

    def draw_grip(self, d, win):
        x, y = win.x + win.w - 14, win.y + win.h - 14
        d.rectangle([x, y, x + 12, y + 12], fill=C.gray, outline=C.darkgray)
        d.rectangle([x + 3, y + 3, x + 8, y + 8], outline=C.black)
        d.rectangle([x + 5, y + 5, x + 11, y + 11], outline=C.black)

    def bar_items(self, desk):
        items = [{"kind": "menu", "menu": "system", "rect": (6, 0, 26, self.bar_h), "label": "◆"}]
        x = 36
        top = desk.windows[-1] if desk.windows else None
        if top is not None:
            for title, _ in desk.app_menus(top):
                tw = int(self.bold_menu_font.getlength(title)) + 16
                items.append({"kind": "menu", "menu": ("app", title), "rect": (x, 0, tw, self.bar_h),
                              "label": title, "bold": title == top.title})
                x += tw
        clock = desk.clock_text()
        cw = int(self.menu_font.getlength(clock)) + 16
        name = top.app.title if top is not None else "Finder"
        nw = min(150, int(self.bold_menu_font.getlength(name)) + 32)
        items.append({"kind": "menu", "menu": "switcher", "rect": (desk.W - nw - 4, 0, nw, self.bar_h),
                      "label": name, "bold": True})
        items.append({"kind": "clock", "rect": (desk.W - nw - cw - 8, 0, cw, self.bar_h), "label": clock})
        return items

    def draw_bar(self, desk, d, items):
        W = desk.W
        d.rectangle([0, 0, W - 1, self.bar_h - 1], fill=C.gray)
        d.line([0, 0, W - 1, 0], fill=C.white)
        d.line([0, self.bar_h - 1, W - 1, self.bar_h - 1], fill=C.black)
        for it in items:
            x, y, w, h = it["rect"]
            open_ = it["kind"] == "menu" and desk.menu_from(it["menu"])
            if open_:
                d.rectangle([x, 1, x + w - 1, h - 2], fill=self.colors["select"])
            col = self.colors["select_text"] if open_ else C.black
            font = self.bold_menu_font if it.get("bold") else self.menu_font
            if it.get("menu") == "system":
                cx, cy = x + w // 2, h // 2
                d.polygon([(cx, cy - 6), (cx + 6, cy), (cx, cy + 6), (cx - 6, cy)], fill=col)
                d.polygon([(cx, cy - 3), (cx + 3, cy), (cx, cy + 3), (cx - 3, cy)],
                          fill=C.sky if not open_ else C.white)
                continue
            label = it["label"]
            if it.get("menu") == "switcher":
                ui.draw_glyph(d, x + 4, (h - 12) // 2, "window", 12)
                lx = x + 20
                while label and font.getlength(label) > w - 26:
                    label = label[:-1]
            else:
                lx = x + 8
            d.text((lx, (h - text_size("Mg", font)[1]) // 2 - 1), label, font=font, fill=col)

    def draw_menu(self, d, m):
        x, y, w, h = m.rect
        d.rectangle([x + 2, y + 2, x + w + 1, y + h + 1], fill=C.darkgray)     # drop shadow
        d.rectangle([x, y, x + w - 1, y + h - 1], fill=C.gray, outline=C.black)
        d.line([x + 1, y + 1, x + w - 2, y + 1], fill=C.white)
        for i, it, (ix, iy, iw, ih) in m.rows():
            if it.get("separator"):
                d.line([ix, iy + ih // 2, ix + iw - 1, iy + ih // 2], fill=C.darkgray)
                d.line([ix, iy + ih // 2 + 1, ix + iw - 1, iy + ih // 2 + 1], fill=C.white)
                continue
            self._menu_row(d, m, i, it, ix, iy, iw, ih)

    def draw_icon(self, d, x, y, label, glyph, selected):
        ui.draw_glyph(d, x + 8, y, glyph, 32)
        lw = min(72, int(self.menu_font.getlength(label)))
        lx = x + 24 - lw // 2
        d.rectangle([lx - 2, y + 34, lx + lw + 1, y + 34 + 14],
                    fill=C.navy if selected else C.white)
        d.text((lx, y + 34), label, font=self.menu_font, fill=C.white if selected else C.black)

    def cursor(self):
        # Black arrow with a white outline: 0 black, 15 white, 255 transparent.
        art = ["X..........",
               "XX.........",
               "X#X........",
               "X##X.......",
               "X###X......",
               "X####X.....",
               "X#####X....",
               "X######X...",
               "X#######X..",
               "X########X.",
               "X#####XXXXX",
               "X##X##X....",
               "X#X.X##X...",
               "XX..X##X...",
               "X....X##X..",
               ".....X##X..",
               "......XX..."]
        m = {".": 255, "X": 15, "#": 0}
        return [[m[c] for c in row] for row in art], 0, 0

    def draw_button(self, d, r, label, pressed=False, default=False, enabled=True, focused=False):
        x, y, w, h = r
        if default:
            d.rounded_rectangle([x - 3, y - 3, x + w + 2, y + h + 2], radius=5, outline=C.black, width=2)
        d.rounded_rectangle([x, y, x + w - 1, y + h - 1], radius=3, fill=C.darkgray if pressed else C.gray,
                            outline=C.black)
        if not pressed:
            d.line([x + 2, y + h - 3, x + 2, y + 2, x + w - 3, y + 2], fill=C.white)
            d.line([x + w - 2, y + 3, x + w - 2, y + h - 2, x + 3, y + h - 2], fill=C.darkgray)
        self._label(d, r, label, (C.white if pressed else C.black) if enabled else C.darkgray, False)
        if focused and not default:
            d.rounded_rectangle([x - 2, y - 2, x + w + 1, y + h + 1], radius=4, outline=C.sky)

    def draw_slider(self, d, r, frac, focused=False, enabled=True):
        x, y, w, h = r
        cy = y + h // 2
        d.rectangle([x, cy - 2, x + w - 1, cy + 1], fill=C.darkgray, outline=C.black)
        kx = x + int((w - 11) * frac)
        d.polygon([(kx, y + 1), (kx + 10, y + 1), (kx + 10, y + h - 5), (kx + 5, y + h - 1), (kx, y + h - 5)],
                  fill=C.sky if focused else C.gray, outline=C.black)
        d.line([kx + 1, y + 2, kx + 9, y + 2], fill=C.white)

    def background_pattern(self, bd, W, H, base, area, img=None):
        x0, y0, aw, ah = area
        accent = C.navy if base != C.navy else C.blue
        for j in range(y0, y0 + ah, 2):
            for i in range(x0 + (j // 2) % 2 * 2, x0 + aw, 4):
                bd.point((i, j), fill=accent)
        return C.white


THEMES = {"classic": Theme, "platinum": Platinum}
from .theme_aqua import Aqua  # noqa: E402  (Aqua builds on the classes above)
THEMES["aqua"] = Aqua
_current = Theme()


def current():
    return _current


def activate(name):
    """Make `name` the current theme; returns it. Updates the display palette and semantic colors."""
    global _current
    cls = THEMES.get(name)
    if cls is None:
        raise ValueError(f"unknown theme {name!r}; choose from {', '.join(THEMES)}")
    _current = cls()
    palette.DISPLAY[:] = _current.palette
    ui.SEMANTIC.clear()
    ui.SEMANTIC.update(_current.colors)
    return _current


def clock_text(h24=True):
    if h24:
        return datetime.now().strftime("%H:%M")
    return datetime.now().strftime("%I:%M %p").lstrip("0")
