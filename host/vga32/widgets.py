"""Native UI widgets: buttons, checkboxes, radios, sliders, text fields, lists, dropdowns,
progress bars, labels, groups and layout containers. They draw through the current theme, so
the same app looks right in Classic, Platinum and Aqua.

Build an app by subclassing UIApp and returning a widget tree from build():

    from vga32.widgets import UIApp, Column, Row, Label, Button, Slider, TextField, Checkbox

    class Mixer(UIApp):
        app_id = "demo.mixer"
        title = "Mixer"
        size = (300, 180)

        def build(self):
            return Column([
                Label("Volume", size="medium"),
                Slider(id="vol", min=0, max=100, value=40),
                Checkbox(id="mute", label="Mute"),
                Row([TextField(id="name", placeholder="Preset name", flex=1),
                     Button("Save", id="save", on_click=self.save)]),
            ])

        def save(self, button):
            self.desk.notify(f"saved {self.values()}")

        def on_change(self, widget):          # any value changed
            ...

Widgets: Column, Row, Group, TabView, ScrollView, Spacer, Separator (layout); Label, ProgressBar,
ImageView, CanvasView (display / custom drawing), Viewport3D (a live GPU 3D view with orbit / pick); Button, MenuButton, Checkbox, Switch, Radio, Slider,
NumberField, TextField, TextArea, ListBox, Table, TreeView, Dropdown, ColorPicker (input).
Dialogs: self.desk.file_dialog("open" | "save", callback, filter=(".txt",)) and
self.desk.dialog(kind="confirm", text=..., on_result=fn(button, value)).

Widgets are found by id with self.find("vol"); every value is in self.values() and in
get_state, and agents can read/set/click them with the ui tool. Panels made by agents use
the same widgets for their button, input, checkbox, radio, slider, select, list and progress
elements.
"""
import time

from . import theme as themes
from .palette import C
from .ui import FONTS, App, parse_color, text_size
from .ui import wrap_text as _wrap

ROW_H = 22


def _font(size="normal"):
    if size in (None, "normal", "small"):
        return themes.current().menu_font
    return FONTS.get(size, themes.current().menu_font)


class Widget:
    focusable = False
    stretch = False          # in a Row: take the row's full height instead of the preferred height

    def __init__(self, id=None, w=None, h=None, flex=0, enabled=True, visible=True, on_change=None, stretch=None):
        if stretch is not None:
            self.stretch = stretch
        self.id = id
        self.fixed_w, self.fixed_h, self.flex = w, h, flex
        self.enabled, self.visible = enabled, visible
        self.on_change = on_change
        self.rect = (0, 0, 0, 0)
        self.clip = None             # set by a ScrollView: only this part of the widget can be hit
        self.tooltip = None
        self.app = None              # the UIApp / Panel hosting it, for change notifications

    # layout
    def pref_w(self):
        return self.fixed_w or 80

    def pref_h(self, width):
        return self.fixed_h or ROW_H

    def layout(self, x, y, w, h):
        self.rect = (x, y, w, h)

    def children(self):
        return []

    def walk(self, hidden=False):
        """This widget and its descendants; hidden=True includes invisible ones (e.g. other tabs)."""
        if not self.visible and not hidden:
            return
        yield self
        for c in self.children():
            yield from c.walk(hidden)

    def contains(self, px, py):
        x, y, w, h = self.rect
        if self.clip is not None:
            cx, cy, cw, ch = self.clip
            if not (cx <= px < cx + cw and cy <= py < cy + ch):
                return False
        return self.visible and x <= px < x + w and y <= py < y + h

    # drawing and input
    def draw(self, d, ctx):
        pass

    def mouse(self, x, y, buttons, pressed, released, wheel, ctx):
        return False

    def key(self, k, ctx):
        return False

    # values
    value = None

    def set_value(self, v):
        self.value = v

    def changed(self, ctx=None, **extra):
        if self.on_change:
            self.on_change(self)
        if self.app is not None:
            self.app.widget_changed(self, **extra)


class Ctx:
    """What widgets need while drawing / handling input."""
    def __init__(self, host, img=None, ink=None):
        self.host = host
        self.theme = themes.current()
        self.text = ink or self.theme.colors["text"]       # label color (panels pick one to suit their background)
        self.img = img
        self.focus = getattr(host, "focus_widget", None)
        self.desk = getattr(host, "desk", None)

    def focused(self, w):
        return self.focus is w


# ----- containers -----
class Column(Widget):
    def __init__(self, items, gap=6, padding=8, **kw):
        super().__init__(**kw)
        self.items, self.gap, self.padding = [i for i in items if i is not None], gap, padding

    def children(self):
        return self.items

    def pref_h(self, width):
        vis = [c for c in self.items if c.visible]
        inner = width - 2 * self.padding
        return 2 * self.padding + sum(c.pref_h(inner) for c in vis) + self.gap * max(0, len(vis) - 1)

    def layout(self, x, y, w, h):
        super().layout(x, y, w, h)
        p = self.padding
        vis = [c for c in self.items if c.visible]
        inner = w - 2 * p
        fixed = sum(c.pref_h(inner) for c in vis if not c.flex) + self.gap * max(0, len(vis) - 1)
        flex_total = sum(c.flex for c in vis)
        spare = max(0, h - 2 * p - fixed)
        cy = y + p
        for c in vis:
            ch = int(spare * c.flex / flex_total) if c.flex and flex_total else c.pref_h(inner)
            c.layout(x + p, cy, inner if c.fixed_w is None else min(inner, c.fixed_w), ch)
            cy += ch + self.gap

    def draw(self, d, ctx):
        for c in self.items:
            if c.visible:
                c.draw(d, ctx)


class Row(Widget):
    def __init__(self, items, gap=6, **kw):
        super().__init__(**kw)
        self.items, self.gap = [i for i in items if i is not None], gap

    def children(self):
        return self.items

    def _widths(self, width):
        vis = [c for c in self.items if c.visible]
        fixed = sum(c.pref_w() for c in vis if not c.flex) + self.gap * max(0, len(vis) - 1)
        flex_total = sum(c.flex for c in vis)
        spare = max(0, width - fixed)
        return vis, [int(spare * c.flex / flex_total) if c.flex and flex_total else c.pref_w() for c in vis]

    def pref_h(self, width):
        vis, ws = self._widths(width)
        return self.fixed_h or max([c.pref_h(cw) for c, cw in zip(vis, ws)] + [0])

    def pref_w(self):
        vis = [c for c in self.items if c.visible]
        return self.fixed_w or sum(c.pref_w() for c in vis) + self.gap * max(0, len(vis) - 1)

    def layout(self, x, y, w, h):
        super().layout(x, y, w, h)
        vis, ws = self._widths(w)
        cx = x
        for c, cw in zip(vis, ws):
            ch = h if c.stretch and not c.fixed_h else (min(h, c.pref_h(cw)) if not c.fixed_h else c.fixed_h)
            c.layout(cx, y + (h - ch) // 2, cw, ch)
            cx += cw + self.gap

    def draw(self, d, ctx):
        for c in self.items:
            if c.visible:
                c.draw(d, ctx)


class Group(Widget):
    """A titled frame around one child."""
    def __init__(self, title, child, **kw):
        super().__init__(**kw)
        self.title, self.child = title, child

    def children(self):
        return [self.child]

    def pref_h(self, width):
        return self.child.pref_h(width - 16) + 22

    def layout(self, x, y, w, h):
        super().layout(x, y, w, h)
        self.child.layout(x + 8, y + 16, w - 16, h - 22)

    def draw(self, d, ctx):
        ctx.theme.draw_group(d, self.rect, self.title)
        self.child.draw(d, ctx)


class Spacer(Widget):
    def __init__(self, h=8, **kw):
        super().__init__(h=h, **kw)


class Separator(Widget):
    def pref_h(self, width):
        return 8

    def draw(self, d, ctx):
        x, y, w, h = self.rect
        d.line([x, y + h // 2, x + w - 1, y + h // 2], fill=C.gray)


# ----- static -----
class Label(Widget):
    def __init__(self, text="", size="normal", color=None, align="left", bold=False, wrap=True, **kw):
        super().__init__(**kw)
        self.text, self.size, self.color, self.align, self.bold, self.wrap = str(text), size, color, align, bold, wrap

    def _lines(self, width):
        font = _font(self.size)
        return _wrap(self.text, font, width) if self.wrap else [self.text], font

    def pref_w(self):
        return self.fixed_w or int(_font(self.size).getlength(self.text)) + 4

    def pref_h(self, width):
        lines, font = self._lines(width)
        return self.fixed_h or (text_size("Mg", font)[1] + 3) * len(lines)

    def draw(self, d, ctx):
        x, y, w, h = self.rect
        lines, font = self._lines(w)
        col = parse_color(self.color, ctx.text) if self.color else ctx.text
        lh = text_size("Mg", font)[1] + 3
        for i, ln in enumerate(lines):
            tw = int(font.getlength(ln))
            lx = x + {"center": (w - tw) // 2, "right": w - tw}.get(self.align, 0)
            d.text((lx, y + i * lh), ln, font=font, fill=col)
            if self.bold:
                d.text((lx + 1, y + i * lh), ln, font=font, fill=col)


class ProgressBar(Widget):
    def __init__(self, value=0, max=100, **kw):
        super().__init__(**kw)
        self.value, self.max = value, max

    def pref_h(self, width):
        return self.fixed_h or 14

    def draw(self, d, ctx):
        ctx.theme.draw_progress(d, self.rect, float(self.value) / (float(self.max) or 1), time.monotonic())


class ImageView(Widget):
    """Shows a PIL image (already on the palette, or it snaps to it)."""
    def __init__(self, image=None, **kw):
        super().__init__(**kw)
        self.image = image

    def pref_h(self, width):
        return self.fixed_h or (self.image.height if self.image is not None else 20)

    def draw(self, d, ctx):
        if self.image is not None and ctx.img is not None:
            x, y, w, h = self.rect
            ctx.img.paste(self.image, (x + (w - self.image.width) // 2, y))


class CanvasView(Widget):
    """A custom-drawn area: draw(d, x, y, w, h); optional mouse(x, y, buttons, pressed, released, wheel)."""
    focusable = True
    wants_wheel = True
    stretch = True

    def __init__(self, draw=None, mouse=None, key=None, **kw):
        super().__init__(**kw)
        self.draw_fn, self.mouse_fn, self.key_fn = draw, mouse, key

    def pref_h(self, width):
        return self.fixed_h or 100

    def draw(self, d, ctx):
        if self.draw_fn:
            self.draw_fn(d, *self.rect)

    def mouse(self, x, y, buttons, pressed, released, wheel, ctx):
        if self.mouse_fn:
            self.mouse_fn(x - self.rect[0], y - self.rect[1], buttons, pressed, released, wheel)
            return True
        return False

    def key(self, k, ctx):
        return bool(self.key_fn and self.key_fn(k))


# ----- interactive -----
class Button(Widget):
    focusable = True

    def __init__(self, label="OK", on_click=None, default=False, **kw):
        super().__init__(**kw)
        self.label, self.on_click, self.default = str(label), on_click, default
        self.down = False

    def pref_w(self):
        return self.fixed_w or max(64, int(_font().getlength(self.label)) + 28)

    def pref_h(self, width):
        return self.fixed_h or 22

    def draw(self, d, ctx):
        x, y, w, h = self.rect
        ctx.theme.draw_button(d, (x, y, min(w, self.pref_w()) if self.fixed_w is None and not self.flex else w, h),
                              self.label, self.down, self.default, self.enabled, ctx.focused(self))

    def click(self):
        if not self.enabled:
            return
        if self.on_click:
            self.on_click(self)
        if self.app is not None:
            self.app.widget_clicked(self)

    def mouse(self, x, y, buttons, pressed, released, wheel, ctx):
        if pressed & 1:
            self.down = True
        if released & 1:
            if self.down and self.contains(x, y):
                self.click()
            self.down = False
        return True

    def key(self, k, ctx):
        if k.down and k.name in ("SPACE", "RETURN"):
            self.click()
            return True
        return False


class Checkbox(Widget):
    focusable = True

    def __init__(self, label="", checked=False, **kw):
        super().__init__(**kw)
        self.label, self.value = str(label), bool(checked)

    def pref_w(self):
        return self.fixed_w or int(_font().getlength(self.label)) + 24

    def pref_h(self, width):
        return self.fixed_h or 18

    def draw(self, d, ctx):
        x, y, w, h = self.rect
        cy = y + (h - 13) // 2
        ctx.theme.draw_check(d, x, cy, self.value, False, self.enabled)
        d.text((x + 19, cy - 1), self.label, font=_font(), fill=ctx.text if self.enabled else ctx.theme.colors["disabled"])
        if ctx.focused(self):
            tw = int(_font().getlength(self.label))
            d.rectangle([x + 17, cy - 2, x + 21 + tw, cy + 14], outline=C.darkgray)

    def toggle(self):
        if self.enabled:
            self.value = not self.value
            self.changed()

    def mouse(self, x, y, buttons, pressed, released, wheel, ctx):
        if pressed & 1:
            self.toggle()
        return True

    def key(self, k, ctx):
        if k.down and k.name == "SPACE":
            self.toggle()
            return True
        return False

    def set_value(self, v):
        self.value = bool(v)


class Radio(Widget):
    """One choice from options; value is the chosen option."""
    focusable = True

    def __init__(self, options=(), value=None, horizontal=False, **kw):
        super().__init__(**kw)
        self.options = [str(o) for o in options]
        self.value = value if value in self.options else (self.options[0] if self.options else None)
        self.horizontal = horizontal

    def _boxes(self):
        x, y, w, h = self.rect
        out, cx = [], x
        for i, o in enumerate(self.options):
            ow = int(_font().getlength(o)) + 30
            out.append((o, (cx, y, ow, 18) if self.horizontal else (x, y + i * 20, w, 18)))
            cx += ow
        return out

    def pref_w(self):
        return self.fixed_w or (sum(int(_font().getlength(o)) + 30 for o in self.options) if self.horizontal
                                else max([int(_font().getlength(o)) + 24 for o in self.options] + [40]))

    def pref_h(self, width):
        return self.fixed_h or (18 if self.horizontal else 20 * max(1, len(self.options)))

    def draw(self, d, ctx):
        for o, (bx, by, bw, bh) in self._boxes():
            ctx.theme.draw_check(d, bx, by + 2, o == self.value, True, self.enabled)
            d.text((bx + 19, by + 1), o, font=_font(), fill=ctx.text)
            if ctx.focused(self) and o == self.value:
                tw = int(_font().getlength(o))
                d.rectangle([bx + 17, by, bx + 21 + tw, by + 16], outline=C.darkgray)

    def mouse(self, x, y, buttons, pressed, released, wheel, ctx):
        if pressed & 1 and self.enabled:
            for o, (bx, by, bw, bh) in self._boxes():
                if bx <= x < bx + bw and by <= y < by + bh and o != self.value:
                    self.value = o
                    self.changed()
        return True

    def key(self, k, ctx):
        if k.down and k.name in ("UP", "DOWN", "LEFT", "RIGHT") and self.options:
            i = self.options.index(self.value) if self.value in self.options else 0
            i = (i + (1 if k.name in ("DOWN", "RIGHT") else -1)) % len(self.options)
            self.value = self.options[i]
            self.changed()
            return True
        return False


class Slider(Widget):
    wants_wheel = True
    focusable = True

    def __init__(self, min=0, max=100, value=0, step=1, label=None, show_value=True, **kw):
        super().__init__(**kw)
        self.min, self.max, self.step = float(min), float(max), float(step or 0)
        self.value = self._snap(float(value))
        self.label, self.show_value = label, show_value
        self.dragging = False

    def _snap(self, v):
        v = max(self.min, min(self.max, v))
        if self.step:
            v = self.min + round((v - self.min) / self.step) * self.step
        return int(v) if self.step and float(self.step).is_integer() and float(self.min).is_integer() else round(v, 4)

    def _track(self):
        x, y, w, h = self.rect
        lw = int(_font().getlength(self.label)) + 8 if self.label else 0
        vw = int(_font().getlength(f"{self.max:g}")) + 10 if self.show_value else 0
        return x + lw, y, max(20, w - lw - vw), h

    def pref_w(self):
        return self.fixed_w or 160

    def pref_h(self, width):
        return self.fixed_h or 20

    def draw(self, d, ctx):
        x, y, w, h = self.rect
        col = ctx.text
        if self.label:
            d.text((x, y + (h - 14) // 2), self.label, font=_font(), fill=col)
        tr = self._track()
        frac = (self.value - self.min) / ((self.max - self.min) or 1)
        ctx.theme.draw_slider(d, tr, frac, ctx.focused(self), self.enabled)
        if self.show_value:
            s = f"{self.value:g}"
            d.text((tr[0] + tr[2] + 8, y + (h - 14) // 2), s, font=_font(), fill=col)

    def _set_from_x(self, x):
        tx, _, tw, _ = self._track()
        v = self._snap(self.min + (self.max - self.min) * (x - tx - 5) / max(1, tw - 10))
        if v != self.value:
            self.value = v
            self.changed()

    def mouse(self, x, y, buttons, pressed, released, wheel, ctx):
        if not self.enabled:
            return True
        if pressed & 1:
            self.dragging = True
        if self.dragging and buttons & 1:
            self._set_from_x(x)
        if released & 1:
            self.dragging = False
        if wheel:
            self.nudge(wheel)
        return True

    def nudge(self, n):
        step = self.step or (self.max - self.min) / 20
        v = self._snap(self.value + n * step)
        if v != self.value:
            self.value = v
            self.changed()

    def key(self, k, ctx):
        if k.down and k.name in ("LEFT", "RIGHT", "UP", "DOWN", "PAGEUP", "PAGEDOWN", "HOME", "END"):
            if k.name == "HOME":
                self.value = self.min
                self.changed()
            elif k.name == "END":
                self.value = self.max
                self.changed()
            else:
                self.nudge({"LEFT": -1, "DOWN": -1, "RIGHT": 1, "UP": 1, "PAGEUP": 10, "PAGEDOWN": -10}[k.name])
            return True
        return False

    def set_value(self, v):
        self.value = self._snap(float(v))


class TextField(Widget):
    focusable = True

    def __init__(self, value="", placeholder="", password=False, on_submit=None, label=None, **kw):
        super().__init__(**kw)
        self.value, self.placeholder, self.password = str(value), placeholder, password
        self.on_submit, self.label = on_submit, label
        self.cursor = len(self.value)

    def pref_w(self):
        return self.fixed_w or 160

    def pref_h(self, width):
        return self.fixed_h or 22

    def draw(self, d, ctx):
        x, y, w, h = self.rect
        font = _font()
        if self.label:
            d.text((x, y + (h - 14) // 2), self.label, font=font, fill=ctx.text)
            lw = int(font.getlength(self.label)) + 8
            x, w = x + lw, w - lw
        focused = ctx.focused(self)
        ix, iy, iw, ih = ctx.theme.draw_field(d, (x, y, w, h), focused, self.enabled)
        shown = ("*" * len(self.value)) if self.password else self.value
        before = shown[:self.cursor]
        start = 0
        while start < len(before) and font.getlength(before[start:]) > iw - 6:
            start += 1
        vis = shown[start:]
        while vis and font.getlength(vis) > iw - 2:
            vis = vis[:-1]
        if not shown and not focused:
            d.text((ix + 2, iy + (ih - 14) // 2), self.placeholder, font=font, fill=C.gray)
        d.text((ix + 2, iy + (ih - 14) // 2), vis, font=font, fill=C.black)
        if focused:
            cx = ix + 2 + int(font.getlength(before[start:]))
            d.line([cx, iy + 2, cx, iy + ih - 3], fill=C.black)

    def mouse(self, x, y, buttons, pressed, released, wheel, ctx):
        if pressed & 1:
            font = _font()
            rel = x - self.rect[0] - 5 - (int(font.getlength(self.label)) + 8 if self.label else 0)
            shown = ("*" * len(self.value)) if self.password else self.value
            self.cursor = next((i for i in range(len(shown) + 1) if font.getlength(shown[:i]) >= rel), len(shown))
        return True

    def key(self, k, ctx):
        if not k.down or not self.enabled:
            return False
        v, c = self.value, self.cursor
        if k.name == "RETURN":
            if self.on_submit:
                self.on_submit(self)
            if self.app is not None:
                self.app.widget_submitted(self)
            return True
        if k.name == "BACKSPACE":
            if c:
                self.value, self.cursor = v[:c - 1] + v[c:], c - 1
        elif k.name == "DELETE":
            self.value = v[:c] + v[c + 1:]
        elif k.name == "LEFT":
            self.cursor = max(0, c - 1)
            return True
        elif k.name == "RIGHT":
            self.cursor = min(len(v), c + 1)
            return True
        elif k.name == "HOME":
            self.cursor = 0
            return True
        elif k.name == "END":
            self.cursor = len(v)
            return True
        elif k.char and not k.ctrl:
            self.value, self.cursor = v[:c] + k.char + v[c:], c + 1
        else:
            return False
        self.changed()
        return True

    def paste(self, text):
        t = text.replace("\r", "").replace("\n", " ")
        self.value = self.value[:self.cursor] + t + self.value[self.cursor:]
        self.cursor += len(t)
        self.changed()

    def set_value(self, v):
        self.value = str(v)
        self.cursor = len(self.value)


class ListBox(Widget):
    wants_wheel = True
    """A scrolling list; value is the selected item (or None). Double-click / Enter activates."""
    focusable = True

    def __init__(self, items=(), selected=None, rows=6, on_activate=None, **kw):
        super().__init__(**kw)
        self.items = [str(i) for i in items]
        self.value = selected if selected in self.items else None
        self.rows, self.top = rows, 0
        self.on_activate = on_activate
        self._last = (None, 0.0)

    def pref_h(self, width):
        return self.fixed_h or self.rows * 18 + 4

    def set_items(self, items):
        self.items = [str(i) for i in items]
        if self.value not in self.items:
            self.value = None

    def _index(self):
        return self.items.index(self.value) if self.value in self.items else -1

    def draw(self, d, ctx):
        x, y, w, h = self.rect
        overflow = len(self.items) * 18 > h - 4
        ix, iy, iw, ih = ctx.theme.draw_list(d, (x, y, w - 14, h) if overflow else self.rect)
        rows = max(1, ih // 18)
        if overflow:
            n = len(self.items)
            ctx.theme.draw_scrollbar(d, (x + w - 13, y, 13, h), self.top / n, min(1.0, (self.top + rows) / n))
        i = self._index()
        if i >= 0:
            self.top = min(max(self.top, i - rows + 1), i) if not (self.top <= i < self.top + rows) else self.top
        self.top = max(0, min(self.top, max(0, len(self.items) - rows)))
        for j, item in enumerate(self.items[self.top:self.top + rows]):
            ry = iy + j * 18
            sel = self.top + j == i
            if sel:
                d.rectangle([ix, ry, ix + iw - 1, ry + 17], fill=ctx.theme.colors["select"])
            s = item
            while s and _font().getlength(s) > iw - 8:
                s = s[:-1]
            d.text((ix + 4, ry + 1), s, font=_font(),
                   fill=ctx.theme.colors["select_text"] if sel else C.black)
        if ctx.focused(self) and i < 0:
            d.rectangle([ix, iy, ix + iw - 1, iy + ih - 1], outline=C.darkgray)

    def _select(self, i):
        if 0 <= i < len(self.items) and self.items[i] != self.value:
            self.value = self.items[i]
            self.changed()

    def activate(self):
        if self.on_activate:
            self.on_activate(self)
        if self.app is not None:
            self.app.widget_submitted(self)

    def mouse(self, x, y, buttons, pressed, released, wheel, ctx):
        if wheel:
            self.top = max(0, self.top - wheel)
        if pressed & 1:
            i = self.top + (y - self.rect[1] - 2) // 18
            self._select(i)
            last, t = self._last
            now = time.monotonic()
            if last == i and now - t < 0.4:
                self.activate()
                self._last = (None, 0.0)
            else:
                self._last = (i, now)
        return True

    def key(self, k, ctx):
        if not k.down or not self.items:
            return False
        i = self._index()
        if k.name in ("UP", "DOWN", "PAGEUP", "PAGEDOWN", "HOME", "END"):
            n = {"UP": -1, "DOWN": 1, "PAGEUP": -self.rows, "PAGEDOWN": self.rows}.get(k.name)
            self._select(0 if k.name == "HOME" else len(self.items) - 1 if k.name == "END" else
                         max(0, min(len(self.items) - 1, (i if i >= 0 else 0) + n)))
            return True
        if k.name == "RETURN" and i >= 0:
            self.activate()
            return True
        return False

    def set_value(self, v):
        self.value = v if v in self.items else None


class Dropdown(Widget):
    """A popup button: clicking opens the options as a menu."""
    focusable = True

    def __init__(self, options=(), value=None, label=None, **kw):
        super().__init__(**kw)
        self.options = [str(o) for o in options]
        self.value = value if value in self.options else (self.options[0] if self.options else None)
        self.label = label

    def pref_w(self):
        lw = int(_font().getlength(self.label)) + 8 if self.label else 0
        return self.fixed_w or lw + max([int(_font().getlength(o)) for o in self.options] + [40]) + 40

    def pref_h(self, width):
        return self.fixed_h or 22

    def draw(self, d, ctx):
        x, y, w, h = self.rect
        if self.label:
            d.text((x, y + (h - 14) // 2), self.label, font=_font(), fill=ctx.text)
            lw = int(_font().getlength(self.label)) + 8
            x, w = x + lw, w - lw
        ctx.theme.draw_button(d, (x, y, w, h), "", False, False, self.enabled, ctx.focused(self))
        d.text((x + 10, y + (h - 14) // 2), str(self.value or ""), font=_font(), fill=C.black)
        ax, ay = x + w - 14, y + h // 2
        d.polygon([(ax - 4, ay - 2), (ax + 4, ay - 2), (ax, ay + 3)], fill=C.black)

    def open(self, ctx):
        desk = ctx.desk
        if desk is None or not self.enabled:
            return
        x, y, w, h = self.rect
        ox, oy = getattr(ctx.host, "screen_origin", lambda: (0, 0))()
        items = [{"label": o, "checked": o == self.value, "action": (lambda o=o: self._pick(o))} for o in self.options]
        desk.open_menu(items, ox + x + (int(_font().getlength(self.label)) + 8 if self.label else 0), oy + y + h,
                       source="dropdown")

    def _pick(self, o):
        if o != self.value:
            self.value = o
            self.changed()

    def mouse(self, x, y, buttons, pressed, released, wheel, ctx):
        if pressed & 1:
            self.open(ctx)
        return True

    def key(self, k, ctx):
        if k.down and k.name in ("SPACE", "RETURN"):
            self.open(ctx)
            return True
        if k.down and k.name in ("UP", "DOWN") and self.options:
            i = self.options.index(self.value) if self.value in self.options else 0
            self._pick(self.options[max(0, min(len(self.options) - 1, i + (1 if k.name == "DOWN" else -1)))])
            return True
        return False


# ----- more containers -----
class TabView(Widget):
    """Tabs across the top; only the chosen page is shown. value is the selected tab index."""
    focusable = True

    def __init__(self, tabs, value=0, **kw):
        super().__init__(**kw)
        self.tabs = [(str(t), w) for t, w in tabs]
        self.value = int(value)
        self._sync()

    def _sync(self):
        for i, (_, w) in enumerate(self.tabs):
            w.visible = i == self.value

    def children(self):
        return [w for _, w in self.tabs]

    def pref_h(self, width):
        return self.fixed_h or 24 + max([w.pref_h(width - 12) for _, w in self.tabs] + [40]) + 8

    def _heads(self):
        x, y, w, h = self.rect
        out, cx = [], x + 6
        for i, (t, _) in enumerate(self.tabs):
            tw = int(_font().getlength(t)) + 20
            out.append((i, (cx, y, tw, 22)))
            cx += tw + 1
        return out

    def layout(self, x, y, w, h):
        super().layout(x, y, w, h)
        for _, c in self.tabs:
            c.layout(x + 6, y + 28, w - 12, h - 34)

    def draw(self, d, ctx):
        x, y, w, h = self.rect
        ctx.theme.draw_tab_page(d, (x, y + 21, w, h - 21))
        for i, r in self._heads():
            ctx.theme.draw_tab(d, r, self.tabs[i][0], i == self.value, ctx.focused(self) and i == self.value)
        self._sync()
        page = self.tabs[self.value][1] if self.tabs else None
        if page is not None:
            page.draw(d, ctx)

    def select(self, i):
        if 0 <= i < len(self.tabs) and i != self.value:
            self.value = i
            self._sync()
            self.changed()

    def mouse(self, x, y, buttons, pressed, released, wheel, ctx):
        if pressed & 1:
            for i, (hx, hy, hw, hh) in self._heads():
                if hx <= x < hx + hw and hy <= y < hy + hh:
                    self.select(i)
        return True

    def key(self, k, ctx):
        if k.down and k.name in ("LEFT", "RIGHT"):
            self.select(self.value + (1 if k.name == "RIGHT" else -1))
            return True
        return False

    def contains(self, px, py):        # only the tab strip belongs to the TabView itself
        x, y, w, h = self.rect
        return self.visible and x <= px < x + w and y <= py < y + 22

    def set_value(self, v):
        names = [t for t, _ in self.tabs]
        self.value = names.index(v) if v in names else int(v)
        self._sync()


class ScrollView(Widget):
    """Scrolls a taller child inside a fixed height (wheel or scrollbar drag). h=None: as tall as the
    layout gives it (flex in a Column, the full height in a Row)."""
    wants_wheel = True
    interactive = True
    stretch = True

    def __init__(self, child, h=150, **kw):
        super().__init__(h=h, **kw)
        self.child = child
        self.offset = 0
        self._drag = None

    def children(self):
        return [self.child]

    def layout(self, x, y, w, h):
        super().layout(x, y, w, h)
        inner = w - 14
        ch = self.child.pref_h(inner)
        self.content_h = ch
        self.offset = max(0, min(self.offset, ch - h))
        self.child.layout(x, y - self.offset, inner, ch)
        for c in self.child.walk(hidden=True):
            c.clip = (x, y, inner, h)

    def draw(self, d, ctx):
        from PIL import Image, ImageDraw
        x, y, w, h = self.rect
        if ctx.img is None:
            self.child.draw(d, ctx)
            return
        # draw the child onto a scratch copy, then keep only the viewport
        scratch = ctx.img.copy()
        sd = ImageDraw.Draw(scratch)
        sd.fontmode = "1"
        self.child.draw(sd, Ctx(ctx.host, scratch, ctx.text))
        box = (max(0, x), max(0, y), max(0, x) + max(1, w - 14), max(0, y) + max(1, h))
        ctx.img.paste(scratch.crop(box), box[:2])
        frac0 = self.offset / max(1, self.content_h)
        frac1 = min(1.0, (self.offset + h) / max(1, self.content_h))
        ctx.theme.draw_scrollbar(d, (x + w - 13, y, 13, h), frac0, frac1)

    def contains(self, px, py):
        x, y, w, h = self.rect
        return self.visible and x + w - 14 <= px < x + w and y <= py < y + h      # the scrollbar

    def scroll(self, dy):
        self.offset = max(0, min(self.content_h - self.rect[3], self.offset + dy))

    def mouse(self, x, y, buttons, pressed, released, wheel, ctx):
        _, ry, _, h = self.rect
        if wheel:
            self.scroll(-wheel * 20)
        if pressed & 1:
            self._drag = (y, self.offset)
        if self._drag and buttons & 1:
            y0, o0 = self._drag
            self.scroll(o0 + (y - y0) * self.content_h / max(1, h) - self.offset)
        if released & 1:
            self._drag = None
        return True


# ----- more controls -----
class Switch(Widget):
    """An on/off toggle."""
    focusable = True

    def __init__(self, label="", value=False, **kw):
        super().__init__(**kw)
        self.label, self.value = str(label), bool(value)

    def pref_w(self):
        return self.fixed_w or int(_font().getlength(self.label)) + 44

    def pref_h(self, width):
        return self.fixed_h or 20

    def draw(self, d, ctx):
        x, y, w, h = self.rect
        ctx.theme.draw_switch(d, (x, y + (h - 16) // 2, 32, 16), self.value, self.enabled, ctx.focused(self))
        d.text((x + 38, y + (h - 14) // 2), self.label, font=_font(), fill=ctx.text)

    def mouse(self, x, y, buttons, pressed, released, wheel, ctx):
        if pressed & 1 and self.enabled:
            self.value = not self.value
            self.changed()
        return True

    def key(self, k, ctx):
        if k.down and k.name in ("SPACE", "RETURN"):
            self.value = not self.value
            self.changed()
            return True
        return False

    def set_value(self, v):
        self.value = bool(v)


class NumberField(Widget):
    wants_wheel = True
    """A number with up/down buttons; type digits, arrows or the wheel to change it."""
    focusable = True

    def __init__(self, value=0, min=None, max=None, step=1, label=None, decimals=None, **kw):
        super().__init__(**kw)
        self.min, self.max, self.step, self.label = min, max, step, label
        self.decimals = decimals if decimals is not None else (0 if float(step).is_integer() else 2)
        self.value = self._clamp(value)
        self.text = None             # being typed

    def _clamp(self, v):
        v = float(v)
        if self.min is not None:
            v = max(float(self.min), v)
        if self.max is not None:
            v = min(float(self.max), v)
        return int(round(v)) if self.decimals == 0 else round(v, self.decimals)

    def pref_w(self):
        lw = int(_font().getlength(self.label)) + 8 if self.label else 0
        return self.fixed_w or lw + 90

    def pref_h(self, width):
        return self.fixed_h or 22

    def _parts(self):
        x, y, w, h = self.rect
        lw = int(_font().getlength(self.label)) + 8 if self.label else 0
        return (x + lw, y, w - lw - 16, h), (x + w - 15, y, 15, h // 2), (x + w - 15, y + h // 2, 15, h - h // 2)

    def draw(self, d, ctx):
        field, up, down = self._parts()
        if self.label:
            d.text((self.rect[0], self.rect[1] + (self.rect[3] - 14) // 2), self.label, font=_font(), fill=ctx.text)
        ix, iy, iw, ih = ctx.theme.draw_field(d, field, ctx.focused(self), self.enabled)
        s = self.text if self.text is not None else f"{self.value:.{self.decimals}f}"
        d.text((ix + iw - 2 - int(_font().getlength(s)), iy + (ih - 14) // 2), s, font=_font(), fill=C.black)
        for r, pts in ((up, "up"), (down, "down")):
            ctx.theme.draw_button(d, r, "", False, False, self.enabled)
            cx, cy = r[0] + r[2] // 2, r[1] + r[3] // 2
            dy = -2 if pts == "up" else 2
            d.polygon([(cx - 3, cy - dy // 2), (cx + 3, cy - dy // 2), (cx, cy + dy)], fill=C.black)

    def nudge(self, n):
        v = self._clamp(self.value + n * self.step)
        if v != self.value:
            self.value = v
            self.changed()

    def _commit(self):
        if self.text is not None:
            try:
                v = self._clamp(float(self.text))
                if v != self.value:
                    self.value = v
                    self.changed()
            except ValueError:
                pass
            self.text = None

    def mouse(self, x, y, buttons, pressed, released, wheel, ctx):
        _, up, down = self._parts()
        if wheel:
            self.nudge(wheel)
        if pressed & 1:
            for r, n in ((up, 1), (down, -1)):
                if r[0] <= x < r[0] + r[2] and r[1] <= y < r[1] + r[3]:
                    self._commit()
                    self.nudge(n)
        return True

    def key(self, k, ctx):
        if not k.down:
            return False
        if k.name in ("UP", "DOWN"):
            self._commit()
            self.nudge(1 if k.name == "UP" else -1)
        elif k.name in ("RETURN", "TAB"):
            self._commit()
            return k.name == "RETURN"
        elif k.name == "BACKSPACE":
            self.text = (self.text if self.text is not None else "")[:-1]
        elif k.name == "ESCAPE":
            self.text = None
        elif k.char and (k.char.isdigit() or k.char in ".-"):
            self.text = (self.text or "") + k.char
        else:
            return False
        return True

    def set_value(self, v):
        self.value = self._clamp(v)
        self.text = None


class TextArea(Widget):
    wants_wheel = True
    """Multi-line text editing with a scrollbar. value is the whole text."""
    focusable = True

    def __init__(self, value="", rows=6, readonly=False, **kw):
        super().__init__(**kw)
        self.lines = str(value).split("\n") or [""]
        self.rows, self.readonly = rows, readonly
        self.r, self.c, self.top = 0, 0, 0
        self._drag = None

    @property
    def value(self):
        return "\n".join(self.lines)

    def set_value(self, v):
        self.lines = str(v).replace("\r\n", "\n").split("\n") or [""]
        self.r = min(self.r, len(self.lines) - 1)
        self.c = min(self.c, len(self.lines[self.r]))

    def pref_h(self, width):
        return self.fixed_h or self.rows * 16 + 6

    def _vis_rows(self):
        return max(1, (self.rect[3] - 6) // 16)

    def draw(self, d, ctx):
        x, y, w, h = self.rect
        ix, iy, iw, ih = ctx.theme.draw_field(d, (x, y, w - 14, h), ctx.focused(self), self.enabled)
        rows = self._vis_rows()
        if self.r < self.top:
            self.top = self.r
        elif self.r >= self.top + rows:
            self.top = self.r - rows + 1
        font = _font()
        for i, ln in enumerate(self.lines[self.top:self.top + rows]):
            s = ln
            while s and font.getlength(s) > iw - 4:
                s = s[:-1]
            d.text((ix + 2, iy + 1 + i * 16), s, font=font, fill=C.black)
        if ctx.focused(self) and not self.readonly:
            cx = ix + 2 + int(font.getlength(self.lines[self.r][:self.c]))
            cy = iy + 1 + (self.r - self.top) * 16
            d.line([cx, cy + 1, cx, cy + 14], fill=C.black)
        n = max(1, len(self.lines))
        ctx.theme.draw_scrollbar(d, (x + w - 13, y, 13, h), self.top / n, min(1.0, (self.top + rows) / n))

    def mouse(self, x, y, buttons, pressed, released, wheel, ctx):
        if wheel:
            self.top = max(0, min(len(self.lines) - 1, self.top - wheel * 3))
            self.r = max(self.top, min(self.r, self.top + self._vis_rows() - 1))
        if pressed & 1:
            rx, ry, rw, rh = self.rect
            if x >= rx + rw - 14:
                self._drag = True
            else:
                self.r = max(0, min(len(self.lines) - 1, self.top + (y - ry - 3) // 16))
                rel = x - rx - 5
                font = _font()
                ln = self.lines[self.r]
                self.c = next((i for i in range(len(ln) + 1) if font.getlength(ln[:i]) >= rel), len(ln))
        if self._drag and buttons & 1:
            rx, ry, rw, rh = self.rect
            self.top = max(0, min(len(self.lines) - 1, int((y - ry) / max(1, rh) * len(self.lines))))
            self.r = self.top
        if released & 1:
            self._drag = None
        return True

    def key(self, k, ctx):
        if not k.down:
            return False
        L, r, c = self.lines, self.r, self.c
        nav = {"UP": (max(0, r - 1), c), "DOWN": (min(len(L) - 1, r + 1), c), "HOME": (r, 0), "END": (r, len(L[r])),
               "PAGEUP": (max(0, r - self._vis_rows()), c), "PAGEDOWN": (min(len(L) - 1, r + self._vis_rows()), c)}
        if k.name in nav:
            self.r, c = nav[k.name]
            self.c = min(c, len(L[self.r]))
            return True
        if k.name == "LEFT":
            if c:
                self.c -= 1
            elif r:
                self.r, self.c = r - 1, len(L[r - 1])
            return True
        if k.name == "RIGHT":
            if c < len(L[r]):
                self.c += 1
            elif r < len(L) - 1:
                self.r, self.c = r + 1, 0
            return True
        if self.readonly or k.ctrl:
            return False
        if k.name == "RETURN":
            L[r:r + 1] = [L[r][:c], L[r][c:]]
            self.r, self.c = r + 1, 0
        elif k.name == "BACKSPACE":
            if c:
                L[r] = L[r][:c - 1] + L[r][c:]
                self.c -= 1
            elif r:
                self.c = len(L[r - 1])
                L[r - 1] += L.pop(r)
                self.r -= 1
        elif k.name == "DELETE":
            if c < len(L[r]):
                L[r] = L[r][:c] + L[r][c + 1:]
            elif r < len(L) - 1:
                L[r] += L.pop(r + 1)
        elif k.name == "TAB":
            L[r] = L[r][:c] + "    " + L[r][c:]
            self.c += 4
        elif k.char:
            L[r] = L[r][:c] + k.char + L[r][c:]
            self.c += 1
        else:
            return False
        self.changed()
        return True

    def paste(self, text):
        if self.readonly:
            return
        parts = text.replace("\r\n", "\n").split("\n")
        L, r, c = self.lines, self.r, self.c
        tail = L[r][c:]
        L[r] = L[r][:c] + parts[0]
        L[r + 1:r + 1] = parts[1:]
        self.r = r + len(parts) - 1
        self.c = len(L[self.r])
        L[self.r] += tail
        self.changed()


class Table(Widget):
    wants_wheel = True
    """Rows and columns with a header; click a header to sort, a row to select (value = row index in
    rows as given), double-click / Enter to activate."""
    focusable = True

    def __init__(self, columns, rows=(), widths=None, selected=None, visible_rows=8, on_activate=None, **kw):
        super().__init__(**kw)
        self.columns = [str(c) for c in columns]
        self.rows = [list(r) for r in rows]
        self.widths = list(widths) if widths else None
        self.value = selected
        self.visible_rows = visible_rows
        self.sort = None            # (column, descending)
        self.top = 0
        self.on_activate = on_activate
        self._last = (None, 0.0)

    def set_rows(self, rows):
        self.rows = [list(r) for r in rows]
        if self.value is not None and self.value >= len(self.rows):
            self.value = None

    def _order(self):
        idx = list(range(len(self.rows)))
        if self.sort is not None:
            col, desc = self.sort

            def key(i):
                v = self.rows[i][col] if col < len(self.rows[i]) else ""
                return (0, float(v)) if isinstance(v, (int, float)) else (1, str(v).lower())
            idx.sort(key=key, reverse=desc)
        return idx

    def _widths(self, w):
        if self.widths:
            return self.widths
        n = max(1, len(self.columns))
        return [w // n] * n

    def pref_h(self, width):
        return self.fixed_h or 20 + self.visible_rows * 17 + 4

    def draw(self, d, ctx):
        x, y, w, h = self.rect
        ix, iy, iw, ih = ctx.theme.draw_list(d, (x, y, w - 14, h))
        ws = self._widths(iw)
        cx = ix
        for ci, (name, cw) in enumerate(zip(self.columns, ws)):
            arrow = "" if not self.sort or self.sort[0] != ci else (" v" if self.sort[1] else " ^")
            ctx.theme.draw_header(d, (cx, iy, cw, 18), name + arrow)
            cx += cw
        rows = max(1, (ih - 20) // 17)
        order = self._order()
        sel_pos = order.index(self.value) if self.value in order else -1
        if sel_pos >= 0 and not (self.top <= sel_pos < self.top + rows):
            self.top = max(0, sel_pos - rows + 1) if sel_pos >= self.top + rows else sel_pos
        self.top = max(0, min(self.top, max(0, len(order) - rows)))
        font = _font()
        for j, ri in enumerate(order[self.top:self.top + rows]):
            ry = iy + 20 + j * 17
            sel = ri == self.value
            if sel:
                d.rectangle([ix, ry, ix + iw - 1, ry + 16], fill=ctx.theme.colors["select"])
            cx = ix
            for ci, cw in enumerate(ws):
                v = self.rows[ri][ci] if ci < len(self.rows[ri]) else ""
                s = f"{v:g}" if isinstance(v, float) else str(v)
                while s and font.getlength(s) > cw - 8:
                    s = s[:-1]
                d.text((cx + 4, ry + 1), s, font=font, fill=ctx.theme.colors["select_text"] if sel else C.black)
                cx += cw
        n = max(1, len(order))
        ctx.theme.draw_scrollbar(d, (x + w - 13, y, 13, h), self.top / n, min(1.0, (self.top + rows) / n))

    def activate(self):
        if self.on_activate:
            self.on_activate(self)
        if self.app is not None:
            self.app.widget_submitted(self)

    def mouse(self, x, y, buttons, pressed, released, wheel, ctx):
        rx, ry, rw, rh = self.rect
        if wheel:
            self.top = max(0, self.top - wheel)
        if not pressed & 1:
            return True
        if x >= rx + rw - 14:
            n = len(self.rows)
            self.top = int((y - ry) / max(1, rh) * n)
            return True
        if y < ry + 22:
            cx = rx + 2
            for ci, cw in enumerate(self._widths(rw - 18)):
                if cx <= x < cx + cw:
                    self.sort = (ci, not self.sort[1]) if self.sort and self.sort[0] == ci else (ci, False)
                cx += cw
            return True
        order = self._order()
        j = self.top + (y - ry - 22) // 17
        if 0 <= j < len(order):
            ri = order[j]
            if ri != self.value:
                self.value = ri
                self.changed()
            last, t = self._last
            now = time.monotonic()
            if last == ri and now - t < 0.4:
                self.activate()
                self._last = (None, 0.0)
            else:
                self._last = (ri, now)
        return True

    def key(self, k, ctx):
        if not k.down or not self.rows:
            return False
        order = self._order()
        pos = order.index(self.value) if self.value in order else -1
        if k.name in ("UP", "DOWN", "PAGEUP", "PAGEDOWN", "HOME", "END"):
            n = {"UP": -1, "DOWN": 1, "PAGEUP": -self.visible_rows, "PAGEDOWN": self.visible_rows}.get(k.name, 0)
            pos = 0 if k.name == "HOME" else len(order) - 1 if k.name == "END" else max(0, min(len(order) - 1, pos + n))
            if order[pos] != self.value:
                self.value = order[pos]
                self.changed()
            return True
        if k.name == "RETURN" and self.value is not None:
            self.activate()
            return True
        return False

    def selected_row(self):
        return self.rows[self.value] if self.value is not None and self.value < len(self.rows) else None


class TreeView(Widget):
    wants_wheel = True
    """Nested items: [{"label": ..., "id": ..., "children": [...], "open": False}]. value = selected id
    (or label). Click the triangle or press Left/Right to fold."""
    focusable = True

    def __init__(self, nodes, selected=None, visible_rows=8, on_activate=None, **kw):
        super().__init__(**kw)
        self.nodes = nodes
        self.value = selected
        self.visible_rows = visible_rows
        self.top = 0
        self.on_activate = on_activate
        self._last = (None, 0.0)

    @staticmethod
    def _id(n):
        return n.get("id", n.get("label"))

    def _flat(self):
        out = []

        def walk(nodes, depth):
            for n in nodes:
                out.append((n, depth))
                if n.get("open") and n.get("children"):
                    walk(n["children"], depth + 1)
        walk(self.nodes, 0)
        return out

    def pref_h(self, width):
        return self.fixed_h or self.visible_rows * 17 + 4

    def draw(self, d, ctx):
        x, y, w, h = self.rect
        ix, iy, iw, ih = ctx.theme.draw_list(d, (x, y, w - 14, h))
        flat = self._flat()
        rows = max(1, ih // 17)
        ids = [self._id(n) for n, _ in flat]
        if self.value in ids:
            p = ids.index(self.value)
            if not (self.top <= p < self.top + rows):
                self.top = p if p < self.top else p - rows + 1
        self.top = max(0, min(self.top, max(0, len(flat) - rows)))
        for j, (n, depth) in enumerate(flat[self.top:self.top + rows]):
            ry = iy + j * 17
            sel = self._id(n) == self.value
            if sel:
                d.rectangle([ix, ry, ix + iw - 1, ry + 16], fill=ctx.theme.colors["select"])
            col = ctx.theme.colors["select_text"] if sel else C.black
            tx = ix + 4 + depth * 14
            if n.get("children"):
                if n.get("open"):
                    d.polygon([(tx, ry + 6), (tx + 8, ry + 6), (tx + 4, ry + 11)], fill=col)
                else:
                    d.polygon([(tx + 2, ry + 4), (tx + 7, ry + 8), (tx + 2, ry + 12)], fill=col)
            d.text((tx + 12, ry + 1), str(n.get("label", "")), font=_font(), fill=col)
        n = max(1, len(flat))
        ctx.theme.draw_scrollbar(d, (x + w - 13, y, 13, h), self.top / n, min(1.0, (self.top + rows) / n))

    def _select(self, n):
        if self._id(n) != self.value:
            self.value = self._id(n)
            self.changed()

    def mouse(self, x, y, buttons, pressed, released, wheel, ctx):
        if wheel:
            self.top = max(0, self.top - wheel)
        if not pressed & 1:
            return True
        flat = self._flat()
        j = self.top + (y - self.rect[1] - 2) // 17
        if 0 <= j < len(flat):
            n, depth = flat[j]
            tx = self.rect[0] + 6 + depth * 14
            if n.get("children") and x < tx + 12:
                n["open"] = not n.get("open")
                return True
            self._select(n)
            last, t = self._last
            now = time.monotonic()
            if last is n and now - t < 0.4:
                if n.get("children"):
                    n["open"] = not n.get("open")
                elif self.on_activate:
                    self.on_activate(self)
                if self.app is not None:
                    self.app.widget_submitted(self)
                self._last = (None, 0.0)
            else:
                self._last = (n, now)
        return True

    def key(self, k, ctx):
        if not k.down:
            return False
        flat = self._flat()
        ids = [self._id(n) for n, _ in flat]
        p = ids.index(self.value) if self.value in ids else -1
        if k.name in ("UP", "DOWN") and flat:
            p = max(0, min(len(flat) - 1, p + (1 if k.name == "DOWN" else -1)))
            self._select(flat[p][0])
            return True
        if k.name in ("LEFT", "RIGHT") and p >= 0:
            n = flat[p][0]
            if n.get("children"):
                n["open"] = k.name == "RIGHT"
            return True
        if k.name == "RETURN" and p >= 0 and self.app is not None:
            self.app.widget_submitted(self)
            return True
        return False


class ColorPicker(Widget):
    """The 16 palette colors as swatches; value is the color name."""
    focusable = True

    def __init__(self, value="black", **kw):
        super().__init__(**kw)
        from .palette import NAMES
        self.names = NAMES
        self.value = value if value in NAMES else "black"

    def pref_w(self):
        return self.fixed_w or 8 * 20

    def _cell(self):
        return max(8, min(20, (self.rect[2] or self.pref_w()) // 8))

    def pref_h(self, width):
        return self.fixed_h or 2 * max(8, min(20, width // 8))

    def draw(self, d, ctx):
        from .palette import PALETTE
        x, y, w, h = self.rect
        c = self._cell()
        for i, name in enumerate(self.names):
            sx, sy = x + (i % 8) * c, y + (i // 8) * c
            d.rectangle([sx + 1, sy + 1, sx + c - 3, sy + c - 3], fill=PALETTE[i], outline=C.darkgray)
            if name == self.value:
                d.rectangle([sx - 1, sy - 1, sx + c - 1, sy + c - 1], outline=C.black if i > 2 else C.white, width=2)
                if ctx.focused(self):
                    d.rectangle([sx - 3, sy - 3, sx + c + 1, sy + c + 1], outline=ctx.theme.colors["select"])

    def mouse(self, x, y, buttons, pressed, released, wheel, ctx):
        if pressed & 1:
            c = self._cell()
            i = (y - self.rect[1]) // c * 8 + (x - self.rect[0]) // c
            if 0 <= i < 16 and self.names[i] != self.value:
                self.value = self.names[i]
                self.changed()
        return True

    def key(self, k, ctx):
        if k.down and k.name in ("LEFT", "RIGHT", "UP", "DOWN"):
            i = self.names.index(self.value)
            i = (i + {"LEFT": -1, "RIGHT": 1, "UP": -8, "DOWN": 8}[k.name]) % 16
            self.value = self.names[i]
            self.changed()
            return True
        return False


class MenuButton(Widget):
    """A button that pops up a menu; on_select(item_id) or the host's on_click with widget.picked."""
    focusable = True

    def __init__(self, label, items, on_select=None, **kw):
        super().__init__(**kw)
        self.label, self.items, self.on_select = str(label), items, on_select
        self.picked = None

    def pref_w(self):
        return self.fixed_w or int(_font().getlength(self.label)) + 36

    def pref_h(self, width):
        return self.fixed_h or 22

    def draw(self, d, ctx):
        x, y, w, h = self.rect
        ctx.theme.draw_button(d, (x, y, w, h), "", False, False, self.enabled, ctx.focused(self))
        d.text((x + 10, y + (h - 14) // 2), self.label, font=_font(), fill=C.black)
        ax, ay = x + w - 12, y + h // 2
        d.polygon([(ax - 4, ay - 2), (ax + 4, ay - 2), (ax, ay + 3)], fill=C.black)

    def _open(self, ctx):
        desk = ctx.desk
        if desk is None:
            return
        ox, oy = getattr(ctx.host, "screen_origin", lambda: (0, 0))()
        x, y, w, h = self.rect

        def pick(it):
            self.picked = it.get("id", it.get("label"))
            if self.on_select:
                self.on_select(self.picked)
            if self.app is not None:
                self.app.widget_clicked(self)
        desk.open_menu(self.items, ox + x, oy + y + h, source="menubutton", on_pick=pick)

    def mouse(self, x, y, buttons, pressed, released, wheel, ctx):
        if pressed & 1 and self.enabled:
            self._open(ctx)
        return True

    def key(self, k, ctx):
        if k.down and k.name in ("SPACE", "RETURN"):
            self._open(ctx)
            return True
        return False



class Viewport3D(Widget):
    """A live 3D view (gl3d GPU renderer) inside a widget layout.

        vp = Viewport3D(h=220, flex=1, on_pick=self.picked, on_drag=self.dragged)
        vp.renderer.add(MeshData.box(1, 1, 1), Material(color="orange"))

    Mouse: right-drag orbits, middle-drag (or right-drag with the wheel button) pans, the wheel zooms;
    left click calls on_pick(pick, x, y) with a gl3d.Pick (or None) and left-drag calls
    on_drag(dx, dy, x, y, pick_at_press) - for moving things with the mouse. on_frame(viewport) runs
    before each render (animate here). camera is an OrbitCamera unless you pass another;
    controls=None turns the built-in orbit/pan/zoom off. pixel=2 renders at half resolution (faster,
    chunkier); dither "ordered" | "floyd" | "none". Requires gl3d.available()."""
    focusable = True
    wants_wheel = True
    interactive = True
    stretch = True

    def __init__(self, camera=None, on_pick=None, on_drag=None, on_frame=None, on_key=None, controls="orbit",
                 pixel=1, dither="ordered", background="#202830", continuous=False, **kw):
        super().__init__(**kw)
        from . import gl3d
        from .render3d import OrbitCamera
        self.camera = camera or OrbitCamera()
        self.on_pick, self.on_drag, self.on_frame, self.on_key = on_pick, on_drag, on_frame, on_key
        self.controls, self.pixel, self.dither = controls, max(1, int(pixel)), dither
        self.continuous = continuous
        self.error = None
        self.renderer = None
        if gl3d.available():
            self.renderer = gl3d.Renderer(64, 64)
            self.renderer.background(background)
        else:
            self.error = gl3d.unavailable_reason()
        self._drag = None             # (button, x, y, moved, pick)
        self.last_image = None

    @property
    def animated(self):
        return self.continuous

    def pref_h(self, width):
        return self.fixed_h or 200

    def _size(self):
        _, _, w, h = self.rect
        return max(8, w // self.pixel), max(8, h // self.pixel)

    def local(self, x, y):
        """Screen-of-window coordinates -> renderer pixel coordinates."""
        return (x - self.rect[0]) / self.pixel, (y - self.rect[1]) / self.pixel

    def pick_at(self, x, y):
        if self.renderer is None:
            return None
        rx, ry = self.local(x, y)
        self.renderer.resize(*self._size())
        return self.renderer.pick(self.camera, rx, ry)

    def ray_at(self, x, y):
        rx, ry = self.local(x, y)
        self.renderer.resize(*self._size())
        return self.renderer.ray(self.camera, rx, ry)

    def draw(self, d, ctx):
        from PIL import Image
        x, y, w, h = self.rect
        if self.renderer is None:
            d.rectangle([x, y, x + w - 1, y + h - 1], fill=C.black)
            d.text((x + 6, y + 6), "3D needs the GPU renderer:", font=_font(), fill=C.white)
            d.text((x + 6, y + 22), str(self.error)[: max(1, w // 7)], font=_font(), fill=C.yellow)
            return
        if self.on_frame:
            self.on_frame(self)
        img = self.renderer.render(self.camera, *self._size())
        if ctx.host is not None and hasattr(ctx.host, "palette_sample"):
            import numpy as np
            ctx.host.palette_sample = np.asarray(img.resize((64, 40), Image.BOX))
        if self.dither != "none":
            from .palette import dither as fs, ordered_dither
            img = ordered_dither(img) if self.dither == "ordered" else fs(img)
        if self.pixel > 1:
            img = img.resize((img.width * self.pixel, img.height * self.pixel), Image.NEAREST)
        self.last_image = img
        if ctx.img is not None:
            ctx.img.paste(img.crop((0, 0, w, h)), (x, y))
        if ctx.focused(self):
            d.rectangle([x, y, x + w - 1, y + h - 1], outline=ctx.theme.colors["select"])

    def mouse(self, x, y, buttons, pressed, released, wheel, ctx):
        if self.renderer is None:
            return True
        cam = self.camera
        if wheel and self.controls:
            if hasattr(cam, "zoom"):
                cam.zoom(0.85 ** wheel)
            else:
                cam.fly(wheel * 0.5)
        if pressed:
            b = 1 if pressed & 1 else 2 if pressed & 2 else 4
            self._drag = [b, x, y, False, self.pick_at(x, y) if b == 1 else None]
        if self._drag and buttons:
            b, lx, ly, moved, pk = self._drag
            dx, dy = x - lx, y - ly
            if dx or dy:
                self._drag[1:4] = [x, y, True]
                if b == 1 and self.on_drag:
                    self.on_drag(dx, dy, x, y, pk)
                elif b == 2 and self.controls and hasattr(cam, "orbit"):
                    cam.orbit(dx, dy)
                elif b == 4 and self.controls and hasattr(cam, "pan"):
                    cam.pan(dx, dy)
        if released and self._drag:
            b, _, _, moved, pk = self._drag
            if b == 1 and not moved and self.on_pick:
                rx, ry = self.local(x, y)
                self.on_pick(pk, rx, ry)
            self._drag = None
        return True

    def key(self, k, ctx):
        if self.on_key and self.on_key(k):
            return True
        cam = self.camera
        if k.down and self.controls and hasattr(cam, "orbit") and k.name in ("LEFT", "RIGHT", "UP", "DOWN"):
            cam.orbit({"LEFT": -20, "RIGHT": 20}.get(k.name, 0), {"UP": -20, "DOWN": 20}.get(k.name, 0))
            return True
        if k.down and self.controls and hasattr(cam, "zoom") and k.name in ("PAGEUP", "PAGEDOWN"):
            cam.zoom(0.8 if k.name == "PAGEUP" else 1.25)
            return True
        return False

# ----- hosting -----
class WidgetHost:
    """Input routing and focus for a widget tree. Mixed into UIApp and Panel."""
    focus_widget = None
    _capture = None

    def widget_list(self):
        return []

    def focusables(self):
        return [w for w in self.widget_list() if w.focusable and w.visible and w.enabled]

    def find(self, wid):
        return next((w for w in self.all_widgets() if w.id == wid), None)

    def all_widgets(self):
        return self.widget_list()

    def values(self):
        return {w.id: w.value for w in self.all_widgets()
                if w.id is not None and not isinstance(w, (Button, Label)) and w.value is not None}

    def screen_origin(self):
        win = next((w for w in self.desk.windows if w.app is self), None)
        return win.client()[:2] if win else (0, 0)

    def route_mouse(self, x, y, buttons, pressed, released, wheel):
        ctx = Ctx(self)
        if wheel and not buttons and self._capture is None:
            # the wheel scrolls the innermost scrollable widget under the pointer
            for w in reversed(self.widget_list()):
                if getattr(w, "wants_wheel", False) and Widget.contains(w, x, y):
                    w.mouse(x, y, buttons, 0, 0, wheel, ctx)
                    return True
        if self._capture is not None:
            self._capture.mouse(x, y, buttons, pressed, released, wheel, ctx)
            if not buttons:
                self._capture = None
            return True
        hit = None
        for w in self.widget_list():
            if w.visible and w.contains(x, y) and (w.focusable or getattr(w, "interactive", False)):
                hit = w                      # deepest (last in walk order) wins
        if hit is None:
            if pressed:
                self.focus_widget = None
            return False
        if pressed and hit.focusable and hit.enabled:
            self.focus_widget = hit
        if hit.enabled and hit.mouse(x, y, buttons, pressed, released, wheel, Ctx(self)) and pressed:
            self._capture = hit
        return True

    def route_key(self, k):
        if k.down and k.name == "TAB" and not k.ctrl:
            f = self.focusables()
            if f:
                i = f.index(self.focus_widget) if self.focus_widget in f else -1
                self.focus_widget = f[(i + (-1 if k.shift else 1)) % len(f)]
            return True
        if self.focus_widget is not None and self.focus_widget.key(k, Ctx(self)):
            return True
        if k.down and k.name == "RETURN":
            default = next((w for w in self.widget_list() if isinstance(w, Button) and w.default and w.enabled), None)
            if default is not None:
                default.click()
                return True
        return False

    # notifications from widgets; hosts override
    def widget_changed(self, w, **extra):
        pass

    def widget_clicked(self, w):
        pass

    def widget_submitted(self, w):
        pass


class UIApp(WidgetHost, App):
    """An app whose window is a widget tree from build(). Override on_change / on_click / on_submit
    (they get the widget) or pass callbacks to the widgets themselves. fps > 0 redraws continuously
    (animation); update(dt) is then called each frame."""
    background = None           # None = the theme's face color
    fps = 0

    def update(self, dt):
        pass

    def __init__(self, desk, **kw):
        super().__init__(desk)
        self.root = self.build()
        for w in self.root.walk(hidden=True):
            w.app = self
        f = self.focusables()
        self.focus_widget = next((w for w in f if isinstance(w, (TextField, ListBox))), None)

    def build(self):
        return Column([Label("Override build()")])

    def rebuild(self, keep_values=True):
        """Build the tree again: after changing its structure, or with keep_values=False to reset it."""
        old = self.values() if keep_values else {}
        self.root = self.build()
        self.focus_widget = None
        for w in self.root.walk(hidden=True):
            w.app = self
            if w.id in old:
                w.set_value(old[w.id])

    def widget_list(self):
        return list(self.root.walk())

    def all_widgets(self):
        return list(self.root.walk(hidden=True))

    def on_change(self, w):
        pass

    def on_click(self, w):
        pass

    def on_submit(self, w):
        pass

    def widget_changed(self, w, **extra):
        self.on_change(w)
        self.desk.bus.publish("ui.change", app_id=self.app_id, window_id=self._wid(), id=w.id, value=w.value)

    def widget_clicked(self, w):
        self.on_click(w)
        self.desk.bus.publish("ui.click", app_id=self.app_id, window_id=self._wid(), id=w.id)

    def widget_submitted(self, w):
        self.on_submit(w)
        self.desk.bus.publish("ui.submit", app_id=self.app_id, window_id=self._wid(), id=w.id, value=w.value)

    def _wid(self):
        return next((x.id for x in self.desk.windows if x.app is self), None)

    def draw(self, d, x, y, w, h):
        th = themes.current()
        d.rectangle([x, y, x + w - 1, y + h - 1],
                    fill=parse_color(self.background, th.colors["face"]) if self.background else th.colors["face"])
        self.root.layout(x, y, w, h)
        self.root.draw(d, Ctx(self, self.desk.target))

    def mouse(self, x, y, buttons, pressed, released, wheel):
        self.route_mouse(x, y, buttons, pressed, released, wheel)

    def key(self, k):
        self.route_key(k)

    def paste(self, text):
        if isinstance(self.focus_widget, TextField):
            self.focus_widget.paste(text)

    def tick(self):
        now = time.monotonic()
        dt = min(0.25, now - getattr(self, "_last_tick", now))
        self._last_tick = now
        if self.fps:
            self.update(dt)
            return True
        return any(isinstance(w, ProgressBar) or getattr(w, "animated", False) for w in self.widget_list())

    def state(self):
        return {"values": self.values(), "focus": getattr(self.focus_widget, "id", None),
                "widgets": [{"id": w.id, "type": type(w).__name__.lower(), "enabled": w.enabled,
                             **({"value": w.value} if w.value is not None else {}),
                             **({"label": w.label} if hasattr(w, "label") and w.label else {}),
                             **({"options": w.options} if hasattr(w, "options") else {})}
                            for w in self.all_widgets() if w.id is not None]}
