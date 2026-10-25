"""Every native widget, on tabs; switch themes to see them restyle.
Copy into ~/.vga32/apps/ or install with install_app."""
from vga32.widgets import (Button, Checkbox, ColorPicker, Column, Dropdown, Group, Label, ListBox, MenuButton,
                           NumberField, ProgressBar, Radio, Row, ScrollView, Slider, Switch, Table, TabView,
                           TextArea, TextField, TreeView, UIApp)

PLANETS = [["Mercury", 0.39, 0], ["Venus", 0.72, 0], ["Earth", 1.0, 1], ["Mars", 1.52, 2],
           ["Jupiter", 5.2, 95], ["Saturn", 9.5, 146], ["Uranus", 19.2, 28], ["Neptune", 30.1, 16]]


class WidgetGallery(UIApp):
    app_id = "demo.widgets"
    title = "Widget Gallery"
    icon = "panel"
    category = "Demos"
    size = (460, 340)

    def build(self):
        controls = Column([
            Row([TextField(id="name", placeholder="Your name", flex=1),
                 Dropdown(["Classic", "Platinum", "Aqua"], "Aqua", id="theme")]),
            Row([Group("Options", Column([Checkbox("Sound", True, id="sound"), Switch("Music", id="music")],
                                         gap=4, padding=2), flex=1),
                 Group("Speed", Radio(["Slow", "Normal", "Fast"], "Normal", id="speed"), flex=1)]),
            Slider(0, 100, 40, label="Volume", id="volume"),
            ProgressBar(40, id="progress"),
            Row([Button("Apply", id="apply", default=True), Button("Reset", id="reset"),
                 MenuButton("More", [{"id": "open", "label": "Open File..."}, {"id": "save", "label": "Save As..."},
                                     {"separator": True}, {"id": "about", "label": "About"}], id="more")]),
        ], gap=8, padding=4)
        data = Column([
            Table(["Planet", "AU", "Moons"], PLANETS, widths=[150, 80, 80], visible_rows=5, id="planets"),
            TreeView([{"label": "Inner", "open": True, "children": [{"label": p[0]} for p in PLANETS[:4]]},
                      {"label": "Outer", "children": [{"label": p[0]} for p in PLANETS[4:]]}],
                     visible_rows=4, id="tree"),
        ], gap=6, padding=4)
        text = Column([
            TextArea("Multi-line text.\nArrows, Enter, Backspace,\nwheel and scrollbar all work.", rows=4,
                     id="notes"),
            Row([NumberField(3, 0, 10, label="Copies", id="copies"),
                 NumberField(1.5, 0, 5, 0.25, label="Zoom", id="zoom")]),
            Row([Label("Color"), ColorPicker("blue", id="color")]),
        ], gap=6, padding=4)
        many = Column([Checkbox(f"Option {i + 1}", i % 3 == 0, id=f"opt{i}") for i in range(14)], gap=3, padding=4)
        return Column([TabView([("Controls", controls), ("Data", data), ("Text", text),
                                ("Scroll", ScrollView(many, h=200))], id="tabs", flex=1)], padding=6)

    def on_change(self, w):
        if w.id == "volume":
            self.find("progress").value = w.value
        elif w.id == "theme":
            self.desk.set_theme(w.value.lower())

    def on_click(self, w):
        if w.id == "apply":
            self.desk.notify(f"Hello {self.find('name').value or 'there'}")
        elif w.id == "reset":
            self.rebuild(keep_values=False)
        elif w.id == "more" and w.picked == "open":
            self.desk.file_dialog("open", lambda p: self.desk.notify(f"Picked {p}"))
        elif w.id == "more" and w.picked == "save":
            self.desk.file_dialog("save", lambda p: self.desk.notify(f"Would save to {p}"), name="untitled.txt")
        elif w.id == "more" and w.picked == "about":
            self.desk.dialog(kind="message", text="Widget Gallery: every native widget.")
