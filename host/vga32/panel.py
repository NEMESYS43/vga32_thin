"""Agent-defined windows: a JSON list of elements rendered with the palette.

Element types (coordinates are inside the panel; omit "y" to stack below the previous element):
  text     text, x, y, color, size small|medium|big, bold, align left|center|right, w (wrap width)
  rect     x, y, w, h, fill, outline, width, radius
  ellipse  x, y, w, h, fill, outline, width
  line     points [[x,y],...] or x1 y1 x2 y2, color, width
  bar      x, y, w, h, value, max (100), color, bg, label
  chart    x, y, w, h, values [...], kind line|bars, color, min, max
  list     x, y, items [...], color, bullet
  table    x, y, rows [[...]], header (bool), widths [px...], color
  image    x, y, w, h, src (http(s) URL, file path, data URI or base64), dither (bool), fit (bool)
Interactive elements are native widgets (widgets.py), drawn in the current theme's style:
  button   id, x, y, w, h, label, default        -> panel.click {id}
  input    id, x, y, w, label, value, placeholder, password  -> Enter sends panel.submit
  checkbox id, x, y, label, checked              -> panel.change {id, value}
  radio    id, x, y, options [...], value, horizontal
  slider   id, x, y, w, min, max, step, value, label, show_value
  select   id, x, y, w, options [...], value, label (a dropdown)
  listbox  id, x, y, w, h, items [...], selected, rows   (double-click / Enter -> panel.submit)
  progress x, y, w, h, value, max
  textarea id, x, y, w, rows, value, readonly     (multi-line; reported on change)
  number   id, x, y, w, value, min, max, step, label
  switch   id, x, y, label, value
  color    id, x, y, value (a palette color name)
Changes send panel.change {id, value, values}. The user's values persist across panel updates;
add "reset": true to an element to put its value back to what the element says. Tab moves focus.

A spec may also declare menus:
  "menu": [{"id": "refresh", "label": "Refresh"}, ...]                 right-click menu
  "menubar": [{"title": "View", "items": [{"id": ..., "label": ...}]}] menu-bar menus (Platinum)
Picks arrive as menu.select events {window_id, app_id, id}.
"""
import time

from PIL import Image

from . import images
from .widgets import WidgetHost
from .palette import C
from .ui import CH, CW, FONT, FONTS, App, bevel, parse_color, text_size, wrap_text

GAP = 4


def prepare(elements):
    """Fetch/decode images ahead of time (off the desktop thread) so drawing never blocks."""
    for e in elements:
        if e.get("type") == "image" and "_img" not in e and e.get("src"):
            e["_img"] = images.load(e["src"])
    return elements


_wrap = wrap_text      # older name, used by a few apps


WIDGET_TYPES = ("button", "input", "checkbox", "radio", "slider", "select", "listbox", "progress", "textarea",
                "number", "switch", "color")


def make_widget(e):
    from . import widgets as W_
    t, wid = e["type"], str(e.get("id", e["type"]))
    if t == "button":
        return W_.Button(str(e.get("label", wid)), id=wid, default=bool(e.get("default")))
    if t == "input":
        return W_.TextField(e.get("value", ""), e.get("placeholder", ""), bool(e.get("password")),
                            label=e.get("label"), id=wid)
    if t == "checkbox":
        return W_.Checkbox(e.get("label", ""), bool(e.get("checked")), id=wid)
    if t == "radio":
        return W_.Radio(e.get("options", []), e.get("value"), bool(e.get("horizontal")), id=wid)
    if t == "slider":
        return W_.Slider(e.get("min", 0), e.get("max", 100), e.get("value", 0), e.get("step", 1), e.get("label"),
                         e.get("show_value", True), id=wid)
    if t == "select":
        return W_.Dropdown(e.get("options", []), e.get("value"), e.get("label"), id=wid)
    if t == "listbox":
        return W_.ListBox(e.get("items", []), e.get("selected"), int(e.get("rows", 6)), id=wid)
    if t == "textarea":
        return W_.TextArea(e.get("value", ""), int(e.get("rows", 5)), bool(e.get("readonly")), id=wid)
    if t == "number":
        return W_.NumberField(e.get("value", 0), e.get("min"), e.get("max"), e.get("step", 1), e.get("label"), id=wid)
    if t == "switch":
        return W_.Switch(e.get("label", ""), bool(e.get("value", e.get("checked"))), id=wid)
    if t == "color":
        return W_.ColorPicker(e.get("value", "black"), id=wid)
    return W_.ProgressBar(e.get("value", 0), e.get("max", 100), id=wid)


def update_widget(w, e):
    """Refresh a cached widget from its element, keeping what the user entered unless reset."""
    if "label" in e and hasattr(w, "label"):
        w.label = None if e["label"] is None else str(e["label"])
    if "placeholder" in e and hasattr(w, "placeholder"):
        w.placeholder = str(e["placeholder"])
    if "options" in e and hasattr(w, "options"):
        w.options = [str(o) for o in e["options"]]
    if "items" in e and hasattr(w, "items"):
        w.items = [str(o) for o in e["items"]]
    if e["type"] == "slider":
        for k in ("min", "max", "step"):
            if k in e:
                setattr(w, k, float(e[k]))
    if e["type"] == "progress" and "max" in e:
        w.max = e["max"]
    if "default" in e and hasattr(w, "default"):
        w.default = bool(e["default"])
    w.enabled = e.get("enabled", True)
    if e.get("reset") or e["type"] == "progress":
        for k in ("value", "checked", "selected"):
            if k in e:
                w.set_value(e[k])


def render_elements(img, d, elements, W, H, background=C.white, host=None):
    """Draw onto (img, d). Returns [(id, x, y, w, h)] for buttons. Interactive elements become
    widgets; with a host (a Panel) they are kept between frames in host.widgets, and the ones
    drawn this frame are listed in host.drawn."""
    from .widgets import Ctx
    buttons, cy = [], GAP
    drawn = []
    # Default text color contrasts with the background, so a dark panel doesn't hide unstyled text.
    ink = C.white if sum(background) < 300 else C.black
    for e in elements:
        t = e.get("type", "text")
        x = int(e.get("x", 8))
        y = int(e["y"]) if "y" in e else cy
        w = int(e.get("w", W - x - 8))
        h = int(e.get("h", 0))
        if t in WIDGET_TYPES:
            wdg = host.widget_for(e) if host is not None else make_widget(e)
            if "w" in e:
                ww = w
            elif t in ("button", "checkbox", "radio", "select"):
                ww = min(W - x - 8, wdg.pref_w())
            else:
                ww = W - x - 8
            h = h or wdg.pref_h(ww)
            wdg.layout(x, y, ww, h)
            wdg.draw(d, Ctx(host, img, ink))
            drawn.append(wdg)
            if t == "button":
                buttons.append((wdg.id, x, y, ww, h))
        elif t == "text":
            font = FONTS.get(e.get("size", "small"), FONT)
            col = parse_color(e.get("color"), ink)
            lines = _wrap(e.get("text", ""), font, w if e.get("wrap", True) else 0)
            lh = text_size("Mg", font)[1] + 3
            for i, ln in enumerate(lines):
                lx = x
                if e.get("align") in ("center", "right"):
                    tw = font.getlength(ln)
                    lx = x + (w - tw) / 2 if e["align"] == "center" else x + w - tw
                d.text((lx, y + i * lh), ln, font=font, fill=col)
                if e.get("bold"):
                    d.text((lx + 1, y + i * lh), ln, font=font, fill=col)
            h = lh * len(lines)
        elif t in ("rect", "ellipse"):
            h = h or 20
            fill = parse_color(e["fill"]) if e.get("fill") is not None else None
            outline = parse_color(e["outline"]) if e.get("outline") is not None else None
            box = [x, y, x + w - 1, y + h - 1]
            if t == "ellipse":
                d.ellipse(box, fill=fill, outline=outline, width=int(e.get("width", 1)))
            elif e.get("radius"):
                d.rounded_rectangle(box, radius=int(e["radius"]), fill=fill, outline=outline, width=int(e.get("width", 1)))
            else:
                d.rectangle(box, fill=fill, outline=outline, width=int(e.get("width", 1)))
        elif t == "line":
            pts = e.get("points") or [[e.get("x1", x), e.get("y1", y)], [e.get("x2", x + w), e.get("y2", y)]]
            d.line([tuple(p) for p in pts], fill=parse_color(e.get("color"), ink), width=int(e.get("width", 1)))
            h = max(p[1] for p in pts) - y + 1 if "y" not in e else 1
        elif t == "bar":
            h = h or 16
            mx = float(e.get("max", 100)) or 1
            frac = max(0.0, min(1.0, float(e.get("value", 0)) / mx))
            d.rectangle([x, y, x + w - 1, y + h - 1], fill=parse_color(e.get("bg"), C.white), outline=C.darkgray)
            if frac > 0:
                d.rectangle([x + 1, y + 1, x + 1 + (w - 3) * frac, y + h - 2], fill=parse_color(e.get("color"), C.blue))
            label = e.get("label")
            if label is None:
                label = f"{float(e.get('value', 0)):g}/{mx:g}"
            if label:
                tw, th = text_size(label)
                d.text((x + (w - tw) / 2, y + (h - th) / 2 - 1), label, font=FONT,
                       fill=parse_color(e.get("label_color"), C.black))
        elif t == "chart":
            h = h or 60
            vals = [float(v) for v in e.get("values", [])]
            d.rectangle([x, y, x + w - 1, y + h - 1], fill=parse_color(e.get("bg"), C.white), outline=C.darkgray)
            if vals:
                lo = float(e.get("min", min(vals + [0])))
                hi = float(e.get("max", max(vals)))
                span = (hi - lo) or 1
                col = parse_color(e.get("color"), C.blue)
                n = len(vals)
                py = lambda v: y + h - 2 - (h - 4) * (min(max(v, lo), hi) - lo) / span
                if e.get("kind") == "bars":
                    bw = max(1, (w - 4) / n)
                    for i, v in enumerate(vals):
                        bx = x + 2 + i * bw
                        d.rectangle([bx, py(v), bx + max(1, bw - 2), y + h - 2], fill=col)
                else:
                    pts = [(x + 2 + (w - 4) * (i / max(1, n - 1)), py(v)) for i, v in enumerate(vals)]
                    d.line(pts if len(pts) > 1 else pts * 2, fill=col, width=int(e.get("width", 2)))
        elif t == "list":
            col = parse_color(e.get("color"), ink)
            bullet = e.get("bullet", "• ")
            lines = []
            for item in e.get("items", []):
                wrapped = _wrap(item, FONT, w - CW * 2)
                lines += [bullet + wrapped[0]] + ["  " + ln for ln in wrapped[1:]]
            for i, ln in enumerate(lines):
                d.text((x, y + i * CH), ln, font=FONT, fill=col)
            h = CH * len(lines)
        elif t == "table":
            rows = e.get("rows", [])
            ncol = max((len(r) for r in rows), default=0)
            widths = e.get("widths") or [w // max(1, ncol)] * ncol
            col = parse_color(e.get("color"), ink)
            for ri, row in enumerate(rows):
                ry = y + ri * CH
                if ri == 0 and e.get("header"):
                    d.rectangle([x, ry, x + sum(widths) - 1, ry + CH - 1], fill=parse_color(e.get("header_bg"), C.gray))
                cx = x
                for ci, cell in enumerate(row):
                    cw = widths[ci] if ci < len(widths) else widths[-1]
                    s = str(cell)
                    while s and FONT.getlength(s) > cw - 6:
                        s = s[:-1]
                    d.text((cx + 3, ry + 1), s, font=FONT, fill=col)
                    cx += cw
            h = CH * len(rows)
        elif t == "image":
            src = e.get("_img")
            if src is not None:
                w = int(e.get("w", min(src.width, W - x)))
                h = h or int(src.height * w / max(1, src.width))
                key = (w, h)
                if e.get("_cache", (None,))[0] != key:
                    # Scaling and dithering is slow; panels redraw every frame, so cache the result.
                    im = images.fit(src, w, h, upscale=True) if e.get("fit", True) else \
                        src.resize((max(1, w), max(1, h)), Image.LANCZOS)
                    e["_cache"] = (key, images.to_palette(im, e.get("dither", True)))
                im = e["_cache"][1]
                img.paste(im, (x + (w - im.width) // 2, y + (h - im.height) // 2))
            else:
                h = h or 20
                d.rectangle([x, y, x + w - 1, y + h - 1], outline=C.darkgray)
                d.text((x + 4, y + 3), "[image]", font=FONT, fill=C.darkgray)
        else:
            raise ValueError(f"unknown element type {t!r}")
        cy = max(cy, y + h + GAP)
    if host is not None:
        host.drawn = drawn
    return buttons


class Panel(WidgetHost, App):
    title = "Panel"
    icon = "panel"
    size = (320, 200)
    MAX_EVENTS = 200

    def __init__(self, desk, spec=None, title=None, **kw):
        super().__init__(desk)
        self.spec = {}
        self.events = []
        self.buttons = []
        self.widgets = {}          # element id -> widget, kept so typed values survive updates
        self.drawn = []            # widgets drawn in the last frame, in order
        self.set_spec(spec or {"elements": [{"type": "text", "text": "Empty panel"}]}, title)

    def set_spec(self, spec, title=None):
        self.spec = spec
        self.title = title or spec.get("title") or self.title
        ids = {str(e.get("id", e.get("type"))) for e in spec.get("elements", []) if e.get("type") in WIDGET_TYPES}
        self.widgets = {k: v for k, v in self.widgets.items() if k in ids}
        if self.focus_widget is not None and self.focus_widget not in self.widgets.values():
            self.focus_widget = None

    def widget_for(self, e):
        wid = str(e.get("id", e["type"]))
        w = self.widgets.get(wid)
        fresh = make_widget(dict(e, id=wid)) if w is None or getattr(w, "_etype", None) != e["type"] else None
        if fresh is not None:
            w = fresh
            w._etype = e["type"]
            w.app = self
            self.widgets[wid] = w
        else:
            update_widget(w, e)
        return w

    def widget_list(self):
        return list(self.drawn)

    def window_id(self):
        return next((w.id for w in self.desk.windows if w.app is self), None)

    def _event(self, **e):
        e["time"] = round(time.time(), 3)
        self.events.append(e)
        del self.events[:-self.MAX_EVENTS]
        bus = getattr(self.desk, "bus", None)
        if bus is not None:
            kind = e.get("type")
            bus.publish(kind if "." in kind else "panel." + kind, window_id=self.window_id(), app_id=self.app_id,
                        **{k: v for k, v in e.items() if k not in ("type", "time")})

    def widget_changed(self, w, **extra):
        if getattr(w, "_etype", None) in ("input", "textarea"):     # typing: only Enter / submit is reported
            return
        self._event(type="change", id=w.id, value=w.value, values=self.values())

    def widget_clicked(self, w):
        self._event(type="click", id=w.id)

    def widget_submitted(self, w):
        self._event(type="submit", id=w.id, value=w.value, values=self.values())

    def context_menu(self, x, y):
        return self.spec.get("menu") or None

    def menus(self):
        return [(m.get("title", "Menu"), m.get("items", [])) for m in self.spec.get("menubar", [])]

    def on_menu(self, item_id):
        self._event(type="menu.select", id=item_id)

    def take_events(self, clear=True):
        ev = list(self.events)
        if clear:
            self.events.clear()
        return ev

    def mouse(self, x, y, buttons, pressed, released, wheel):
        self.route_mouse(x, y, buttons, pressed, released, wheel)

    def key(self, k):
        if self.route_key(k):
            return
        if k.down and k.name == "ESCAPE" and self.focus_widget is not None:
            self.focus_widget = None
            return
        if k.down:
            self._event(type="key", name=k.name, char=k.char, mods=k.mods)

    def copy(self):
        return "\n".join(str(e.get("text", "")) for e in self.spec.get("elements", []) if e.get("type") == "text")

    def paste(self, text):
        if hasattr(self.focus_widget, "paste"):
            self.focus_widget.paste(text)

    def tick(self):
        return any(type(w).__name__ == "ProgressBar" for w in self.drawn)

    def draw(self, d, x, y, w, h):
        bg = parse_color(self.spec.get("background"), C.white)
        d.rectangle([x, y, x + w - 1, y + h - 1], fill=bg)
        self.buttons = render_elements(self.desk.target, d, self.spec.get("elements", []), w, h, bg, self)

    def state(self):
        return {"elements": len(self.spec.get("elements", [])), "pending_events": len(self.events),
                "buttons": [b[0] for b in self.buttons], "values": self.values(),
                "focus": getattr(self.focus_widget, "id", None)}
