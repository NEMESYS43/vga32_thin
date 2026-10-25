"""Settings (Control Panels): theme, desktop background and wallpaper, clock, apps, agents.

Everything writes through Desktop.apply_settings, so changes persist to ~/.vga32/settings.json
and agents see them as settings.changed events.
"""
from . import theme as themes
from .palette import C, NAMES, PALETTE
from .ui import CH, FONT, App, bevel, draw_glyph, text_size

PAGES = [("appearance", "Appearance", "settings"), ("desktop", "Desktop", "image"),
         ("apps", "Apps", "window"), ("agents", "Agents", "assistant")]
SIDEBAR_W = 104
MODES = ["fill", "fit", "center", "tile"]


class SettingsApp(App):
    title = "Settings"
    size = (500, 320)
    icon = "settings"
    category = "System"

    def __init__(self, desk, page="appearance", **kw):
        super().__init__(desk)
        self.page = page if page in dict((p, 1) for p, _, _ in PAGES) else "appearance"
        self.hits = []             # (rect, callable)
        self.edit = None           # (settings key, buffer) of the focused text field
        self.scroll = 0

    # ----- helpers -----
    def _set(self, dotted, value):
        top, *rest = dotted.split(".")
        val = value
        for part in reversed(rest):
            val = {part: val}
        self.desk.apply_settings({top: val})

    def _hit(self, r, fn):
        self.hits.append((r, fn))

    def _button(self, d, x, y, label, fn, pressed=False, w=None):
        w = w or text_size(label)[0] + 16
        bevel(d, x, y, w, 20, raised=not pressed, face=C.white if pressed else C.gray)
        d.text((x + 8, y + 3), label, font=FONT, fill=C.black)
        self._hit((x, y, w, 20), fn)
        return x + w + 4

    def _check(self, d, x, y, label, on, fn):
        d.rectangle([x, y + 1, x + 12, y + 13], fill=C.white, outline=C.black)
        if on:
            d.line([x + 2, y + 7, x + 5, y + 11, x + 11, y + 3], fill=C.black, width=2)
        d.text((x + 18, y + 1), label, font=FONT, fill=C.black)
        self._hit((x, y, 18 + text_size(label)[0], 16), fn)

    def _field(self, d, x, y, w, label, key, value, secret=False):
        d.text((x, y + 3), label, font=FONT, fill=C.black)
        fx = x + 90
        editing = self.edit is not None and self.edit[0] == key
        shown = self.edit[1] if editing else ("*" * len(value) if secret and value else str(value or ""))
        d.rectangle([fx, y, x + w - 1, y + 19], fill=C.white, outline=C.blue if editing else C.darkgray)
        while shown and FONT.getlength(shown) > w - 100:
            shown = shown[1:]
        d.text((fx + 3, y + 3), shown, font=FONT, fill=C.black)
        if editing:
            cx = fx + 4 + int(FONT.getlength(shown))
            d.line([cx, y + 3, cx, y + 16], fill=C.black)
        self._hit((fx, y, w - 90, 20), lambda: setattr(self, "edit", (key, "" if secret else str(value or ""))))

    def _commit(self):
        key, buf = self.edit
        self.edit = None
        if key == "webhooks":
            self._set("webhooks", [u.strip() for u in buf.split(",") if u.strip()])
        else:
            self._set(key, buf)

    # ----- input -----
    def key(self, k):
        if not k.down or self.edit is None:
            return
        key, buf = self.edit
        if k.name == "RETURN":
            self._commit()
        elif k.name == "ESCAPE":
            self.edit = None
        elif k.name == "BACKSPACE":
            self.edit = (key, buf[:-1])
        elif k.char and not k.ctrl:
            self.edit = (key, buf + k.char)

    def paste(self, text):
        if self.edit is not None:
            self.edit = (self.edit[0], self.edit[1] + text.replace("\n", ""))

    def mouse(self, x, y, buttons, pressed, released, wheel):
        if wheel and self.page == "apps":
            self.scroll = max(0, self.scroll - wheel)
        if not pressed & 1:
            return
        for (hx, hy, hw, hh), fn in reversed(self.hits):
            if hx <= x < hx + hw and hy <= y < hy + hh:
                if self.edit is not None:
                    self._commit()           # clicking elsewhere saves the field being edited
                fn()
                return
        if self.edit is not None:
            self._commit()

    # ----- drawing -----
    def draw(self, d, x, y, w, h):
        self.hits = []
        d.rectangle([x, y, x + w - 1, y + h - 1], fill=C.gray)
        d.rectangle([x, y, x + SIDEBAR_W - 1, y + h - 1], fill=C.white)
        d.line([x + SIDEBAR_W, y, x + SIDEBAR_W, y + h - 1], fill=C.darkgray)
        for i, (pid, label, glyph) in enumerate(PAGES):
            py = y + 6 + i * 44
            if pid == self.page:
                d.rectangle([x + 2, py - 2, x + SIDEBAR_W - 3, py + 40], fill=self.desk.theme.colors["select"])
            draw_glyph(d, x + (SIDEBAR_W - 24) // 2, py, glyph, 24)
            tw = text_size(label)[0]
            d.text((x + (SIDEBAR_W - tw) // 2, py + 25), label, font=FONT,
                   fill=self.desk.theme.colors["select_text"] if pid == self.page else C.black)
            self._hit((x, py - 2, SIDEBAR_W, 44), lambda p=pid: (setattr(self, "page", p), setattr(self, "edit", None)))
        cx, cw = x + SIDEBAR_W + 12, w - SIDEBAR_W - 24
        getattr(self, "_page_" + self.page)(d, cx, y + 8, cw, h - 16)

    def _page_appearance(self, d, x, y, w, h):
        s = self.desk.settings
        d.text((x, y), "Theme", font=FONT, fill=C.black)
        y += CH + 4
        for name, cls in themes.THEMES.items():
            on = self.desk.theme.name == name
            r = (x, y, w, 38)
            bevel(d, *r, raised=not on, face=C.white if on else C.gray)
            self._preview(d, x + 6, y + 5, cls)
            d.text((x + 70, y + 4), cls.label, font=FONT, fill=C.black)
            d.text((x + 70, y + 4 + CH), "menu bar on top" if cls.bar_top else "taskbar and Start menu",
                   font=FONT, fill=C.darkgray)
            self._hit(r, lambda n=name: self.desk.set_theme(n))
            y += 44
        y += 6
        self._check(d, x, y, "24-hour clock", s["clock_24h"], lambda: self._set("clock_24h", not s["clock_24h"]))
        y += 22
        d.text((x, y), "Fonts: drop a .ttf into ~/.vga32/fonts for", font=FONT, fill=C.darkgray)
        d.text((x, y + CH), "Platinum title bars and menus.", font=FONT, fill=C.darkgray)

    def _preview(self, d, x, y, cls):
        d.rectangle([x, y, x + 55, y + 27], fill=C.teal if not cls.bar_top else C.blue)
        if cls.bar_top:
            d.rectangle([x, y, x + 55, y + 4], fill=C.gray)
            d.rectangle([x + 10, y + 8, x + 45, y + 25], fill=C.gray, outline=C.black)
            for sy in range(y + 9, y + 13, 2):
                d.line([x + 11, sy, x + 44, sy], fill=C.white)
        else:
            d.rectangle([x, y + 23, x + 55, y + 27], fill=C.gray)
            d.rectangle([x + 10, y + 4, x + 45, y + 20], fill=C.gray, outline=C.black)
            d.rectangle([x + 11, y + 5, x + 44, y + 8], fill=C.blue)

    def _page_desktop(self, d, x, y, w, h):
        st = self.desk.style
        d.text((x, y), "Background color", font=FONT, fill=C.black)
        y += CH + 2
        cur = st["background"]
        for i, name in enumerate(NAMES):
            sx, sy = x + (i % 8) * 26, y + (i // 8) * 22
            d.rectangle([sx, sy, sx + 21, sy + 17], fill=PALETTE[i], outline=C.black)
            if name == cur:
                d.rectangle([sx - 2, sy - 2, sx + 23, sy + 19], outline=C.white, width=2)
            self._hit((sx, sy, 22, 18), lambda n=name: self._set("desktop.background", n))
        y += 50
        self._check(d, x, y, "Pattern", st["pattern"], lambda: self._set("desktop.pattern", not st["pattern"]))
        self._check(d, x + 100, y, "Desktop icons", st["icons"], lambda: self._set("desktop.icons", not st["icons"]))
        y += 24
        self._field(d, x, y, w, "Caption", "desktop.caption", st["caption"])
        y += 26
        self._field(d, x, y, w, "Wallpaper", "desktop.wallpaper", st["wallpaper"])
        y += 26
        bx = x + 90
        for m in MODES:
            bx = self._button(d, bx, y, m.title(), lambda m=m: self._set("desktop.wallpaper_mode", m),
                              pressed=st["wallpaper_mode"] == m)
        y += 26
        self._button(d, x + 90, y, "No wallpaper", lambda: self._set("desktop.wallpaper", ""))
        d.text((x, y + 26), "Wallpaper: an image path on this PC or a URL.", font=FONT, fill=C.darkgray)

    def _page_apps(self, d, x, y, w, h):
        s = self.desk.settings
        hidden, pinned = s["hidden_apps"], s["icons"]
        d.text((x, y), "App", font=FONT, fill=C.darkgray)
        d.text((x + w - 150, y), "Menu", font=FONT, fill=C.darkgray)
        d.text((x + w - 90, y), "Desktop", font=FONT, fill=C.darkgray)
        y += CH + 2
        entries = sorted((e for e in self.desk.registry.entries.values() if not e.get("hidden")),
                         key=lambda e: (e.get("category", ""), e["title"].lower()))
        rows = max(1, (h - CH - 30) // 22)
        self.scroll = min(self.scroll, max(0, len(entries) - rows))
        for e in entries[self.scroll:self.scroll + rows]:
            draw_glyph(d, x, y, e.get("icon", "app"), 16)
            d.text((x + 22, y + 1), f"{e['title']}"[:30], font=FONT, fill=C.black)
            d.text((x + 22 + text_size(e["title"][:30])[0] + 6, y + 1), e.get("category", ""), font=FONT,
                   fill=C.darkgray)
            shown = e["id"] not in hidden
            on_desk = shown and (e.get("desktop") or e["id"] in pinned)
            self._check(d, x + w - 145, y, "", shown, lambda e=e, v=shown: self._toggle_hidden(e["id"], v))
            self._check(d, x + w - 80, y, "", on_desk, lambda e=e, v=on_desk: self._toggle_desk(e, v))
            y += 22
        if len(entries) > rows:
            d.text((x, y + 2), f"{self.scroll + 1}-{min(len(entries), self.scroll + rows)} of {len(entries)} "
                               "(scroll for more)", font=FONT, fill=C.darkgray)
        if self.desk.registry.errors:
            d.text((x, y + 2 + CH), f"{len(self.desk.registry.errors)} plugin(s) failed to load; see get_state",
                   font=FONT, fill=C.maroon)

    def _toggle_hidden(self, app_id, shown):
        hidden = [a for a in self.desk.settings["hidden_apps"] if a != app_id]
        if shown:
            hidden.append(app_id)
        self.desk.settings.set("hidden_apps", hidden)
        self.desk.dirty = True

    def _toggle_desk(self, e, on):
        if on:
            self.desk.pin_icon(e["id"], False)
        else:
            if e["id"] in self.desk.settings["hidden_apps"]:
                self._toggle_hidden(e["id"], False)
            self.desk.pin_icon(e["id"], True)

    def _page_agents(self, d, x, y, w, h):
        s = self.desk.settings
        a = s["assistant"]
        d.text((x, y), "Assistant (local AI, OpenAI-compatible API)", font=FONT, fill=C.black)
        y += CH + 4
        self._field(d, x, y, w, "Endpoint", "assistant.url", a.get("url", ""))
        y += 24
        self._field(d, x, y, w, "Model", "assistant.model", a.get("model", ""))
        y += 24
        self._field(d, x, y, w, "API key", "assistant.api_key", a.get("api_key", ""), secret=True)
        y += 32
        d.text((x, y), "External agents", font=FONT, fill=C.black)
        y += CH + 4
        self._field(d, x, y, w, "Webhooks", "webhooks", ", ".join(s["webhooks"]))
        y += 26
        toks = s["tokens"]
        d.text((x, y), f"Tokens: {len(toks)}" + (" - " + ", ".join(
            f"{t.get('name')} [{' '.join(t.get('scopes', []))}]" for t in toks) if toks else
            " (agents use the HOST_KEY)"), font=FONT, fill=C.darkgray)
        y += CH + 2
        d.text((x, y), "Add tokens with the settings tool or settings.json.", font=FONT, fill=C.darkgray)
        y += CH + 2
        d.text((x, y), f"Events so far: {self.desk.bus.last_seq()}", font=FONT, fill=C.darkgray)

    def state(self):
        return {"page": self.page, "editing": self.edit[0] if self.edit else None}
