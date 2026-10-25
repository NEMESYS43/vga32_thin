"""Dialog boxes for agents (the ask_user tool) and the desktop: message, confirm, input, choice.

When the user answers (or closes the window), a dialog.result event is published with
{dialog_id (= window id), button, value} and the window closes.
"""
from .palette import C
from .panel import _wrap
from .ui import CH, FONT, App, bevel, draw_glyph, text_size

KINDS = ("message", "confirm", "input", "choice")
ICONS = {"message": "about", "confirm": "about", "input": "notes", "choice": "window"}


class Dialog(App):
    title = "Dialog"
    size = (340, 140)
    icon = "window"
    category = "System"

    def __init__(self, desk, kind="message", text="", title=None, buttons=None, value="", options=None,
                 icon=None, **kw):
        super().__init__(desk)
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {', '.join(KINDS)}")
        self.kind = kind
        self.text = str(text)
        self.title = title or {"message": "Message", "confirm": "Confirm", "input": "Input",
                               "choice": "Choose"}[kind]
        self.buttons = [str(b) for b in (buttons or (["OK"] if kind == "message" else ["OK", "Cancel"]))][:4]
        self.value = str(value or "")
        self.options = [str(o) for o in (options or [])][:12]
        self.sel = 0 if self.options else -1
        self.glyph = icon or ICONS[kind]
        self.answered = None
        self.boxes = []
        lines = _wrap(self.text, FONT, 280)
        h = 24 + len(lines) * CH + 40
        if kind == "input":
            h += 28
        if kind == "choice":
            h += len(self.options) * (CH + 2) + 8
        self.size = (max(260, min(440, 64 + max([text_size(ln)[0] for ln in lines] + [180]))), h)

    def answer(self, button):
        if self.answered is not None:
            return
        self.answered = button
        value = self.value if self.kind == "input" else \
            (self.options[self.sel] if self.kind == "choice" and self.sel >= 0 else None)
        win = next((w for w in self.desk.windows if w.app is self), None)
        wid = win.id if win else None
        self.desk.bus.publish("dialog.result", dialog_id=wid, window_id=wid, button=button, value=value,
                              app_id="dialog")
        if win is not None:
            self.desk.close(win)

    def close(self):
        if self.answered is None:          # closed with the X: counts as the last (cancel) button
            self.answered = ""
            win = next((w for w in self.desk.windows if w.app is self), None)
            wid = win.id if win else None
            self.desk.bus.publish("dialog.result", dialog_id=wid, window_id=wid, button=None, value=None,
                                  app_id="dialog", closed=True)

    def key(self, k):
        if not k.down:
            return
        if k.name == "RETURN":
            self.answer(self.buttons[0])
        elif k.name == "ESCAPE":
            self.answer(self.buttons[-1])
        elif self.kind == "choice" and k.name in ("UP", "DOWN") and self.options:
            self.sel = (self.sel + (1 if k.name == "DOWN" else -1)) % len(self.options)
        elif self.kind == "input":
            if k.name == "BACKSPACE":
                self.value = self.value[:-1]
            elif k.char and not k.ctrl:
                self.value += k.char

    def paste(self, text):
        if self.kind == "input":
            self.value += text.replace("\n", " ")

    def mouse(self, x, y, buttons, pressed, released, wheel):
        if not pressed & 1:
            return
        for kind, data, (bx, by, bw, bh) in self.boxes:
            if bx <= x < bx + bw and by <= y < by + bh:
                if kind == "button":
                    self.answer(data)
                elif kind == "option":
                    self.sel = data

    def draw(self, d, x, y, w, h):
        face = self.desk.theme.colors["face"]
        d.rectangle([x, y, x + w - 1, y + h - 1], fill=face)
        draw_glyph(d, x + 12, y + 12, self.glyph, 32)
        ty = y + 14
        for ln in _wrap(self.text, FONT, w - 70):
            d.text((x + 56, ty), ln, font=FONT, fill=C.black)
            ty += CH
        self.boxes = []
        ty += 8
        if self.kind == "input":
            d.rectangle([x + 56, ty, x + w - 12, ty + 20], fill=C.white, outline=C.black)
            shown = self.value
            while shown and FONT.getlength(shown) > w - 80:
                shown = shown[1:]
            d.text((x + 60, ty + 3), shown, font=FONT, fill=C.black)
            cx = x + 61 + int(FONT.getlength(shown))
            d.line([cx, ty + 3, cx, ty + 16], fill=C.black)
            ty += 28
        if self.kind == "choice":
            for i, opt in enumerate(self.options):
                r = (x + 56, ty, w - 68, CH + 1)
                if i == self.sel:
                    d.rectangle([r[0], r[1], r[0] + r[2] - 1, r[1] + r[3] - 1], fill=self.desk.theme.colors["select"])
                d.text((r[0] + 4, ty + 1), opt, font=FONT,
                       fill=self.desk.theme.colors["select_text"] if i == self.sel else C.black)
                self.boxes.append(("option", i, r))
                ty += CH + 2
        bx = x + w - 12
        by = y + h - 32
        for i, label in enumerate(reversed(self.buttons)):
            bw = max(70, text_size(label)[0] + 20)
            bx -= bw
            bevel(d, bx, by, bw, 22)
            if label == self.buttons[0]:
                d.rectangle([bx - 2, by - 2, bx + bw + 1, by + 23], outline=C.black)
            tw = text_size(label)[0]
            d.text((bx + (bw - tw) // 2, by + 4), label, font=FONT, fill=C.black)
            self.boxes.append(("button", label, (bx, by, bw, 22)))
            bx -= 10

    def state(self):
        return {"kind": self.kind, "text": self.text, "buttons": self.buttons, "value": self.value,
                "options": self.options, "selected": self.sel}
