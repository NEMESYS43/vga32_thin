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
  button   id, x, y, w, h, label, color, bg   -> clicks show up in panel events
  image    x, y, w, h, src (http(s) URL, file path, data URI or base64), dither (bool), fit (bool)
  input    id, x, y, w, label, value, placeholder   -> a text field; Enter sends panel.submit
  checkbox id, x, y, label, checked                 -> toggling sends panel.change

A spec may also declare menus:
  "menu": [{"id": "refresh", "label": "Refresh"}, ...]                 right-click menu
  "menubar": [{"title": "View", "items": [{"id": ..., "label": ...}]}] menu-bar menus (Platinum)
Picks arrive as menu.select events {window_id, app_id, id}.
"""
import time

from PIL import Image

from . import images
from .palette import C
from .ui import CH, CW, FONT, FONTS, App, bevel, parse_color, text_size

GAP = 4


def prepare(elements):
    """Fetch/decode images ahead of time (off the desktop thread) so drawing never blocks."""
    for e in elements:
        if e.get("type") == "image" and "_img" not in e and e.get("src"):
            e["_img"] = images.load(e["src"])
    return elements


def _wrap(text, font, width):
    out = []
    for para in str(text).split("\n"):
        words, line = para.split(" "), ""
        for w in words:
            t = w if not line else line + " " + w
            if width and font.getlength(t) > width and line:
                out.append(line)
                line = w
            else:
                line = t
        out.append(line)
    return out


def render_elements(img, d, elements, W, H, background=C.white, state=None, fields=None):
    """Draw onto (img, d). Returns [(id, x, y, w, h)] for buttons. Input and checkbox elements
    read their live values from state {"values", "focus"} and are listed in fields."""
    buttons, cy = [], GAP
    fields = [] if fields is None else fields
    # Default text color contrasts with the background, so a dark panel doesn't hide unstyled text.
    ink = C.white if sum(background) < 300 else C.black
    for e in elements:
        t = e.get("type", "text")
        x = int(e.get("x", 8))
        y = int(e["y"]) if "y" in e else cy
        w = int(e.get("w", W - x - 8))
        h = int(e.get("h", 0))
        if t == "text":
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
        elif t == "button":
            w = int(e.get("w", max(60, text_size(e.get("label", ""))[0] + 20)))
            h = h or 22
            bevel(d, x, y, w, h, face=parse_color(e.get("bg"), C.gray))
            label = str(e.get("label", e.get("id", "")))
            tw, th = text_size(label)
            d.text((x + (w - tw) / 2, y + (h - th) / 2 - 1), label, font=FONT, fill=parse_color(e.get("color"), C.black))
            buttons.append((str(e.get("id", label)), x, y, w, h))
        elif t == "input":
            h = h or 20
            fid = str(e.get("id", "input"))
            label = e.get("label")
            fx = x
            if label:
                d.text((x, y + 3), str(label), font=FONT, fill=ink)
                fx = x + int(FONT.getlength(str(label))) + 6
            fw = max(20, w - (fx - x))
            focused = state is not None and state.get("focus") == fid
            d.rectangle([fx, y, fx + fw - 1, y + h - 1], fill=C.white, outline=C.blue if focused else C.darkgray)
            val = state["values"].get(fid, e.get("value", "")) if state is not None else e.get("value", "")
            shown = str(val) or ("" if focused else str(e.get("placeholder", "")))
            while shown and FONT.getlength(shown) > fw - 8:
                shown = shown[1:]
            d.text((fx + 3, y + 3), shown, font=FONT, fill=C.black if val else C.darkgray)
            if focused:
                cx = fx + 4 + int(FONT.getlength(shown))
                d.line([cx, y + 3, cx, y + h - 4], fill=C.black)
            fields.append((fid, "input", fx, y, fw, h))
        elif t == "checkbox":
            h = h or 16
            fid = str(e.get("id", "checkbox"))
            on = state["values"].get(fid, bool(e.get("checked"))) if state is not None else bool(e.get("checked"))
            d.rectangle([x, y + 1, x + 12, y + 13], fill=C.white, outline=C.black)
            if on:
                d.line([x + 2, y + 7, x + 5, y + 11, x + 11, y + 3], fill=C.black, width=2)
            label = str(e.get("label", ""))
            d.text((x + 18, y + 1), label, font=FONT, fill=ink)
            fields.append((fid, "checkbox", x, y, 18 + int(FONT.getlength(label)), h))
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
    return buttons


class Panel(App):
    title = "Panel"
    icon = "panel"
    size = (320, 200)
    MAX_EVENTS = 200

    def __init__(self, desk, spec=None, title=None, **kw):
        super().__init__(desk)
        self.spec = {}
        self.events = []
        self.buttons = []
        self.pressed = None
        self.fields = []           # (id, kind, x, y, w, h) of inputs and checkboxes
        self.form = {"values": {}, "focus": None}
        self.set_spec(spec or {"elements": [{"type": "text", "text": "Empty panel"}]}, title)

    def set_spec(self, spec, title=None):
        self.spec = spec
        self.title = title or spec.get("title") or self.title
        for e in spec.get("elements", []):
            if e.get("type") in ("input", "checkbox") and "id" in e and e.get("reset"):
                self.form["values"].pop(str(e["id"]), None)

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

    def values(self):
        out = {}
        for e in self.spec.get("elements", []):
            if e.get("type") == "input" and "id" in e:
                out[str(e["id"])] = self.form["values"].get(str(e["id"]), e.get("value", ""))
            elif e.get("type") == "checkbox" and "id" in e:
                out[str(e["id"])] = self.form["values"].get(str(e["id"]), bool(e.get("checked")))
        return out

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
        hit = next((b for b in self.buttons if b[1] <= x < b[1] + b[3] and b[2] <= y < b[2] + b[4]), None)
        if pressed & 1:
            self.pressed = hit
            f = next((f for f in self.fields if f[2] <= x < f[2] + f[4] and f[3] <= y < f[3] + f[5]), None)
            self.form["focus"] = f[0] if f and f[1] == "input" else None
            if f and f[1] == "checkbox":
                v = not self.values().get(f[0], False)
                self.form["values"][f[0]] = v
                self._event(type="change", id=f[0], value=v, values=self.values())
        if released & 1:
            if hit and self.pressed and hit[0] == self.pressed[0]:
                self._event(type="click", id=hit[0])
            self.pressed = None

    def key(self, k):
        if not k.down:
            return
        fid = self.form["focus"]
        if fid is not None:
            val = str(self.values().get(fid, ""))
            if k.name == "RETURN":
                self._event(type="submit", id=fid, value=val, values=self.values())
            elif k.name == "BACKSPACE":
                self.form["values"][fid] = val[:-1]
            elif k.name == "ESCAPE":
                self.form["focus"] = None
            elif k.name == "TAB":
                ids = [f[0] for f in self.fields if f[1] == "input"]
                self.form["focus"] = ids[(ids.index(fid) + 1) % len(ids)] if fid in ids else None
            elif k.char and not k.ctrl:
                self.form["values"][fid] = val + k.char
            return
        self._event(type="key", name=k.name, char=k.char, mods=k.mods)

    def copy(self):
        return "\n".join(str(e.get("text", "")) for e in self.spec.get("elements", []) if e.get("type") == "text")

    def paste(self, text):
        if self.form["focus"] is not None:
            fid = self.form["focus"]
            self.form["values"][fid] = str(self.values().get(fid, "")) + text.replace("\n", " ")

    def draw(self, d, x, y, w, h):
        bg = parse_color(self.spec.get("background"), C.white)
        d.rectangle([x, y, x + w - 1, y + h - 1], fill=bg)
        self.fields = []
        self.buttons = render_elements(self.desk.target, d, self.spec.get("elements", []), w, h, bg,
                                       self.form, self.fields)
        if self.pressed:
            _, bx, by, bw, bh = self.pressed
            d.rectangle([bx, by, bx + bw - 1, by + bh - 1], outline=C.blue, width=2)

    def state(self):
        return {"elements": len(self.spec.get("elements", [])), "pending_events": len(self.events),
                "buttons": [b[0] for b in self.buttons], "values": self.values(),
                "focus": self.form["focus"]}
