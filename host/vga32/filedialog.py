"""Open / Save file dialogs built from native widgets. Apps call desktop.file_dialog():

    self.desk.file_dialog("open", self.load, title="Open level", filter=(".bsp", ".obj"))
    self.desk.file_dialog("save", self.save_as, name="untitled.txt")

The callback gets the chosen path (nothing is called on Cancel). The choice is also published as a
dialog.result event {button: "Open" | "Save" | "Cancel", value: path}.
"""
import os
import string
import time

from .widgets import Button, Column, Label, Row, Table, TextField, UIApp


def _size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


class FileDialogApp(UIApp):
    title = "Open"
    icon = "files"
    category = "System"
    size = (440, 300)

    def __init__(self, desk, mode="open", callback=None, path=None, filter=None, name="", title=None, **kw):
        self.mode = mode if mode in ("open", "save", "folder") else "open"
        self.callback = callback
        self.filter = tuple(f.lower() for f in (filter or ()))
        self.cwd = os.path.abspath(os.path.expanduser(path or "~"))
        if not os.path.isdir(self.cwd):
            self.cwd = os.path.dirname(self.cwd) if os.path.isdir(os.path.dirname(self.cwd)) else os.path.expanduser("~")
        self.default_name = name
        self.entries = []
        self.answered = False
        super().__init__(desk)
        self.title = title or {"open": "Open", "save": "Save As", "folder": "Choose Folder"}[self.mode]
        self.go(self.cwd)

    def build(self):
        verb = {"open": "Open", "save": "Save", "folder": "Choose"}[self.mode]
        return Column([
            Row([Button("Up", id="up", on_click=lambda b: self.go(os.path.dirname(self.cwd) or self.cwd)),
                 Button("Home", id="home", on_click=lambda b: self.go("~")),
                 Label("", id="where", wrap=False, flex=1)]),
            Table(["Name", "Size", "Modified"], [], widths=[220, 70, 90], visible_rows=8, id="files",
                  on_activate=lambda t: self.activate()),
            TextField(self.default_name, "file name" if self.mode == "save" else "path or name", id="name",
                      label="Name:", on_submit=lambda f: self.confirm()),
            Row([Label(", ".join(self.filter) if self.filter else "", id="filter", flex=1),
                 Button("Cancel", id="cancel", on_click=lambda b: self.cancel()),
                 Button(verb, id="ok", default=True, on_click=lambda b: self.confirm())]),
        ], gap=6)

    def go(self, path):
        path = os.path.abspath(os.path.expanduser(path))
        rows = []
        try:
            ents = []
            with os.scandir(path) as it:
                for e in it:
                    if e.name.startswith("."):
                        continue
                    try:
                        st = e.stat()
                    except OSError:
                        continue
                    is_dir = e.is_dir()
                    if not is_dir and (self.mode == "folder" or
                                       (self.filter and not e.name.lower().endswith(self.filter))):
                        continue
                    ents.append((e.name, is_dir, st.st_size, st.st_mtime))
            ents.sort(key=lambda t: (not t[1], t[0].lower()))
            self.entries = ents
            for name, is_dir, size, mtime in ents:
                rows.append([name + ("/" if is_dir else ""), "" if is_dir else _size(size),
                             time.strftime("%Y-%m-%d", time.localtime(mtime))])
            self.cwd = path
        except OSError as e:
            self.desk.notify(str(e))
            return
        t = self.find("files")
        t.set_rows(rows)
        t.value, t.top = None, 0
        self.find("where").text = self.cwd

    def on_change(self, w):
        if w.id == "files" and w.value is not None:
            name, is_dir = self.entries[w.value][:2]
            if not is_dir or self.mode == "folder":
                self.find("name").set_value(name)

    def activate(self):
        t = self.find("files")
        if t.value is None:
            return
        name, is_dir = self.entries[t.value][:2]
        if is_dir:
            self.go(os.path.join(self.cwd, name))
        else:
            self.confirm()

    def confirm(self):
        name = self.find("name").value.strip()
        t = self.find("files")
        if not name and t.value is not None:
            name = self.entries[t.value][0]
        if self.mode == "folder" and not name:
            return self._done(self.cwd)
        if not name:
            return
        path = os.path.abspath(os.path.join(self.cwd, os.path.expanduser(name)))
        if os.path.isdir(path) and self.mode != "folder":
            self.find("name").set_value("")
            return self.go(path)
        if self.mode == "open" and not os.path.exists(path):
            self.desk.notify(f"Not found: {name}")
            return
        if self.mode == "save" and self.filter and not path.lower().endswith(self.filter):
            path += self.filter[0]
        self._done(path)

    def cancel(self):
        self._finish("Cancel", None)

    def _done(self, path):
        self._finish({"open": "Open", "save": "Save", "folder": "Choose"}[self.mode], path)
        if self.callback:
            self.callback(path)

    def _finish(self, button, value):
        if self.answered:
            return
        self.answered = True
        win = self.window()
        self.desk.bus.publish("dialog.result", dialog_id=win.id if win else None, window_id=win.id if win else None,
                              button=button, value=value, app_id="filedialog")
        if win is not None:
            self.desk.close(win)

    def close(self):
        if not self.answered:
            self.answered = True
            win = self.window()
            self.desk.bus.publish("dialog.result", dialog_id=win.id if win else None,
                                  window_id=win.id if win else None, button=None, value=None, app_id="filedialog",
                                  closed=True)

    def key(self, k):
        if k.down and k.name == "ESCAPE":
            return self.cancel()
        if k.down and k.name == "BACKSPACE" and self.focus_widget is self.find("files"):
            return self.go(os.path.dirname(self.cwd) or self.cwd)
        super().key(k)
