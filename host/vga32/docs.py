"""Developer docs for agents, generated from the code so they never go stale (dev_docs tool and
vga32://docs/<topic> resources)."""
import inspect
import importlib

GUIDE = """# Building apps for the VGA32 desktop

Apps are Python classes installed with install_app(name, source[, assets]). They run inside the
desktop on the host PC, are listed in the menus, persist across restarts and can be called by you.
Screen: 640x400 (usually), 16 colors on screen at a time chosen from 64 (RGB 0/85/170/255 per
channel); apps draw in full color and the desktop maps or dithers. Check with screenshot / get_state.

## Pick a base class
- vga32.widgets.UIApp - forms, tools, editors: return a widget tree from build(). Widgets include a
  live 3D view (Viewport3D). Set fps = 30 + update(dt) for animation.            -> topic "widgets"
- vga32.game.GameApp - games and real-time views: update(dt) / render(g) at fps, held keys, mouse
  look (mouse_look = True), canvas that follows the window (pixel = 2).           -> topic "game"
- vga32.ui.App - raw: draw(d, x, y, w, h) with PIL, key(k), mouse(...), tick().   -> topic "app"
Every app class needs app_id (e.g. "me.myapp"), title, and may set icon, category, size.

## 3D
- vga32.gl3d - GPU engine: scene graph, meshes (triangles/lines/points, dynamic), materials,
  custom GLSL shaders, raw-GL draw hooks, lights, fog, skyboxes, transparency, GPU picking, rays,
  orthographic/orbit cameras.                                                     -> topic "gl3d"
- vga32.mesh.EditableMesh - polygon modeling: extrude, inset, subdivide, smooth, mirror, raycast,
  undo, OBJ in/out.                                                               -> topic "mesh"
- vga32.formats.load_model - glTF/GLB and OBJ+MTL files into the scene.          -> topic "formats"
- vga32.render3d - cameras (Camera, OrbitCamera) and CPU fallbacks (Scene, Raycaster). -> "render3d"
- vga32.quake - Quake 1 .bsp maps with collision.                                 -> topic "quake"
Check vga32.gl3d.available() and show gl3d.unavailable_reason() if it's False.

## Talk to your app while it runs
- app_call(command, args): calls the app's cmd_<command>(**args); "commands" lists them, "state"
  returns state(). Give your app cmd_ methods so you (and other agents) can drive it.
- app_eval(code): run Python inside the app (app, desk in scope) to inspect or patch it live.
- app_logs: self.log(...) lines plus any crash tracebacks from draw / input / tick.
- self.emit("thing", ...) publishes "app.thing" events -> wait_events(types=["app."]).
- self.storage (dict) + self.save_storage() persist data per app_id; app_data reads/writes it.
- Assets (textures, models) ship with install_app(assets={...}); find them with
  vga32.registry.assets_dir(__file__).

## Minimal 3D tool
    from vga32.widgets import UIApp, Column, Row, Button, Label, Viewport3D
    from vga32.gl3d import MeshData, Material

    class Spinner(UIApp):
        app_id = "me.spinner"; title = "Spinner"; size = (360, 280); fps = 30
        def build(self):
            self.vp = Viewport3D(flex=1, on_pick=self.picked)
            r = self.vp.renderer
            r.add(MeshData.grid(4, 0.5))
            self.cube = r.add(MeshData.box(1, 1, 1), Material(color="orange"), pos=(0, 0.5, 0), name="cube")
            return Column([self.vp, Row([Button("Reset", on_click=lambda b: self.cmd_reset()), Label("", id="info", flex=1)])])
        def update(self, dt):
            self.cube.rot[1] += dt
        def picked(self, pick, x, y):
            self.find("info").text = f"clicked {pick.node.name}" if pick else "nothing"
        def cmd_reset(self):
            "Stop the spin."
            self.cube.rot[:] = 0
            return "ok"
"""

TOPICS = {
    "overview": None,
    "app": ("vga32.ui", ["App", "draw_glyph", "parse_color"]),
    "game": ("vga32.game", ["GameApp", "Canvas"]),
    "widgets": ("vga32.widgets", None),
    "gl3d": ("vga32.gl3d", ["Renderer", "Node", "MeshData", "Material", "Shader", "Pick", "checker", "available"]),
    "mesh": ("vga32.mesh", ["EditableMesh"]),
    "formats": ("vga32.formats", ["load_model", "load_obj", "GLTF"]),
    "render3d": ("vga32.render3d", ["Camera", "OrbitCamera", "Mesh", "Scene", "Raycaster"]),
    "quake": ("vga32.quake", ["Level", "Player", "load"]),
    "panel": ("vga32.panel", ["render_elements"]),
    "events": ("vga32.events", ["EventBus"]),
    "palette": ("vga32.palette", ["C", "NAMES", "ordered_dither", "dither", "choose_palette"]),
}


def _sig(obj):
    try:
        return str(inspect.signature(obj))
    except (TypeError, ValueError):
        return "(...)"


def _doc(obj, full=False):
    d = inspect.getdoc(obj) or ""
    return d if full else d.split("\n\n")[0]


def describe(topic):
    if topic in (None, "", "overview"):
        return GUIDE + "\nTopics: " + ", ".join(TOPICS)
    if topic not in TOPICS:
        raise ValueError(f"unknown topic {topic!r}; topics: {', '.join(TOPICS)}")
    modname, names = TOPICS[topic]
    mod = importlib.import_module(modname)
    out = [f"# {modname}", "", _doc(mod, full=True), ""]
    if names is None:
        names = [n for n, o in vars(mod).items() if inspect.isclass(o) and o.__module__ == modname
                 and not n.startswith("_")]
    for name in names:
        obj = getattr(mod, name, None)
        if obj is None:
            continue
        if inspect.isclass(obj):
            bases = [b.__name__ for b in obj.__bases__ if b is not object]
            out.append(f"## class {name}{_sig(obj)}" + (f"  ({', '.join(bases)})" if bases else ""))
            if _doc(obj):
                out.append(_doc(obj, full=True))
            attrs = [f"{k} = {v!r}" for k, v in vars(obj).items() if not k.startswith("_") and
                     isinstance(v, (int, float, str, bool, tuple, type(None))) and len(repr(v)) < 60]
            if attrs:
                out.append("Class attributes: " + ", ".join(attrs))
            for mname, m in vars(obj).items():
                if mname.startswith("_") and mname != "__init__":
                    continue
                target = m.fget if isinstance(m, property) else getattr(m, "__func__", m)
                if not callable(target) and not isinstance(m, property):
                    continue
                kind = "property " if isinstance(m, property) else ("static " if isinstance(m, staticmethod) else "")
                sig = "" if isinstance(m, property) else _sig(target)
                line = f"- {kind}{mname}{sig}"
                if _doc(target):
                    line += f": {_doc(target)}"
                out.append(line)
            out.append("")
        elif callable(obj):
            out.append(f"## {name}{_sig(obj)}\n{_doc(obj, full=True)}\n")
        else:
            out.append(f"## {name} = {obj!r}"[:400])
    return "\n".join(out)
