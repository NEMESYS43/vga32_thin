"""Popup menus: the Start/system menu, menu-bar menus, right-click menus and their submenus.

An item is a dict:
  {"id": "copy", "label": "Copy", "shortcut": "Ctrl+C", "enabled": True, "checked": False}
  {"label": "Arrange", "items": [...]}          a submenu
  {"separator": True}
  {"label": "...", "action": callable}          host-side items may carry their own action

The desktop keeps a stack of open menus (root first). Picking an item runs its action, or
else the root's on_pick(item).
"""
SEP_H = 7


class Menu:
    def __init__(self, items, x, y, theme, screen, on_pick=None, source=None, up=False, parent=None):
        self.items = [it for it in items if it is not None]
        self.theme = theme
        self.on_pick = on_pick
        self.source = source            # what opened it ("start", "system", ("app", title), "context"...)
        self.parent = parent
        self.on_close = None            # called if the menu closes without a pick
        self.picked = False
        self.hover = -1
        font = theme.menu_font
        labels = [font.getlength(it.get("label", "")) + font.getlength(it.get("shortcut", "")) +
                  (24 if it.get("shortcut") else 0) for it in self.items if not it.get("separator")]
        w = int(max(labels, default=60)) + 40
        h = sum(SEP_H if it.get("separator") else theme.item_h for it in self.items) + 6
        sw, sh = screen
        w = min(w, sw)
        if up:
            y -= h
        x = max(0, min(sw - w, x))
        y = max(0, min(sh - h, y))
        self.rect = (x, y, w, h)

    def rows(self):
        x, y, w, _ = self.rect
        cy = y + 3
        for i, it in enumerate(self.items):
            ih = SEP_H if it.get("separator") else self.theme.item_h
            yield i, it, (x + 3, cy, w - 6, ih)
            cy += ih

    def contains(self, px, py):
        x, y, w, h = self.rect
        return x <= px < x + w and y <= py < y + h

    def item_at(self, px, py):
        for i, it, (x, y, w, h) in self.rows():
            if x <= px < x + w and y <= py < y + h:
                return i
        return -1

    def row_rect(self, i):
        for j, _, r in self.rows():
            if j == i:
                return r
        return None

    def selectable(self, i):
        it = self.items[i]
        return not it.get("separator") and it.get("enabled", True)

    def step(self, d):
        n = len(self.items)
        i = self.hover
        for _ in range(n):
            i = (i + d) % n if i >= 0 else (0 if d > 0 else n - 1)
            if self.selectable(i):
                self.hover = i
                return

    def root(self):
        m = self
        while m.parent is not None:
            m = m.parent
        return m

    def public(self):
        """For the agent API: the items without host callables."""
        def clean(it):
            out = {k: v for k, v in it.items() if k != "action" and not k.startswith("_")}
            if "items" in out:
                out["items"] = [clean(s) for s in out["items"]]
            return out
        return {"source": self.source if isinstance(self.source, str) else list(self.source or []),
                "rect": list(self.rect), "items": [clean(it) for it in self.items]}
