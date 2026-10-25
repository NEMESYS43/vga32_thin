"""3D Viewer: walk through Quake maps (.bsp) and look at models (.obj) with the GPU renderer.

Controls: WASD / arrows move, mouse looks (click to capture, Esc releases), Space jumps (flies up
in noclip), C goes down, F toggles walking / noclip flying, P toggles colors adapted to the scene, R respawns, Tab jumps to the next spawn
point, [ and ] change brightness, - and = change the field of view, Ctrl+O opens a file.
Agents drive it with the view3d tool.
"""
import math
import os
import threading
import time

import numpy as np

from . import gl3d
from .game import GameApp
from .render3d import Camera, Mesh


class Viewer3D(GameApp):
    title = "3D Viewer"
    icon = "star"
    category = "Graphics"
    width, height = 200, 130
    pixel = 2
    fps = 30
    mouse_look = True
    adaptive_palette = True        # while focused, the 16 screen colors are picked for the scene
    background = "black"

    def __init__(self, desk, path=None, url=None, **kw):
        self.path = path or url
        super().__init__(desk)

    def start(self):
        self.renderer = None
        self.level = None
        self.player = None
        self.model_nodes = []
        self.cam = Camera(pos=(0, 2, -6), fov=90)
        self.status = "Open a .bsp (Quake map) or .obj model: Ctrl+O, or from Files."
        self.loading = False
        self.load_error = None
        self.pending = None           # loaded data waiting for the GPU upload (desktop thread)
        self.spawns, self.spawn_i = [], 0
        self.dither_mode = "ordered"
        self.hud = True
        self._fps_t, self._fps_n, self.real_fps = time.monotonic(), 0, 0.0
        if not gl3d.available():
            self.status = "GPU renderer unavailable: " + (gl3d.unavailable_reason() or "?")
        elif self.path:
            self.open(self.path)

    # ----- loading -----
    def open(self, path):
        path = os.path.abspath(os.path.expanduser(str(path)))
        if not os.path.exists(path):
            self.status = f"Not found: {path}"
            return
        self.path, self.loading, self.load_error = path, True, None
        self.title = f"3D Viewer - {os.path.basename(path)}"
        self.status = f"Loading {os.path.basename(path)}..."

        def work():
            try:
                if path.lower().endswith(".bsp"):
                    from . import quake
                    lv = quake.load(path)
                    lv.batches()                           # the slow part, off the desktop thread
                    self.pending = ("bsp", lv)
                elif path.lower().endswith(".obj"):
                    self.pending = ("obj", Mesh.load_obj(path))
                else:
                    raise ValueError("open a .bsp (Quake 1 map) or .obj model")
            except Exception as e:
                self.load_error = f"{type(e).__name__}: {e}"
                self.loading = False
        threading.Thread(target=work, daemon=True).start()

    def _upload(self):
        kind, data = self.pending
        self.pending = None
        if self.renderer is None:
            self.renderer = gl3d.Renderer(self.width, self.height, near=1.0, far=20000.0)
        r = self.renderer
        r.clear()
        r.lights.clear()
        r.fog(None)
        if kind == "bsp":
            self.level = data
            data.add_to(r)
            r.gamma, r.auto_exposure = 1.5, 0.36
            r.near, r.far = 4.0, 16384.0
            self.spawns = [e for e in data.entities if e.get("classname", "").startswith("info_player")] or [{}]
            self.spawn_i = 0
            self.cam = data.spawn_camera(fov=self.cam.fov)
            self.player = data.player(self.cam)
            info = data.info()
            self.status = (info["message"] or info["file"]) + ("" if info["palette"] else
                                                               "  (grey: no Quake palette found)")
        else:
            self.level, self.player = None, None
            mesh = gl3d.MeshData.from_mesh(data)
            lo, hi = mesh.bounds()
            size = float(np.max(hi - lo)) or 1.0
            center = (lo + hi) / 2
            r.add(mesh, gl3d.Material(color="#d0d0d0"), pos=-center * (2 / size), scale=2 / size)
            r.add(gl3d.MeshData.plane(20, 20, uv_scale=10, y=-1.05),
                  gl3d.Material(texture=gl3d.checker(64, "#506070", "#303840")))
            r.sky_gradient("#102040", "#6080b0")
            r.sun((-0.5, -1, 0.4), "#e0d8c8", "#404858")
            r.gamma, r.auto_exposure, r.exposure = 1.2, None, 1.0
            r.near, r.far = 0.05, 200.0
            self.cam = Camera(pos=(0, 0.5, -4), fov=self.cam.fov)
            self.status = f"{os.path.basename(self.path)}: {mesh.triangles} triangles"
        self.loading = False
        self.desk.bus.publish("view3d.loaded", app_id=self.app_id, path=self.path, status=self.status)

    # ----- controls -----
    def respawn(self, i=None):
        if self.level is None:
            return
        if i is not None and self.spawns:
            self.spawn_i = i % len(self.spawns)
        e = self.spawns[self.spawn_i] if self.spawns else {}
        if e.get("origin"):
            from .quake import q2gl
            o = np.array([float(v) for v in e["origin"].split()])
            self.cam.pos = q2gl(o + [0, 0, 22])
            self.cam.yaw = math.radians(90 - float(e.get("angle", 0)))
            self.cam.pitch = 0
        else:
            self.cam.pos, self.cam.yaw = self.level.spawn()
        self.player.vel[:] = 0

    def update(self, dt):
        if self.pending is not None:
            self._upload()
        if self.load_error:
            self.status, self.load_error = self.load_error, None
        dx, dy = self.mouse_delta()
        self.cam.look(dx, dy, 0.004)
        self.cam.yaw += self.axis("right", "left") * 2.0 * dt
        fwd = self.axis("w", "s") + self.axis("up", "down")
        side = self.axis("d", "a")
        if self.pressed("f") and self.player is not None:
            self.player.noclip = not self.player.noclip
        if self.pressed("r"):
            self.respawn()
        if self.pressed("tab"):
            self.respawn(self.spawn_i + 1)
        if self.pressed("leftbracket", "rightbracket") and self.renderer is not None:
            r = self.renderer
            r.auto_exposure = None
            r.exposure = max(0.2, min(8, r.exposure * (1.25 if self.pressed("rightbracket") else 0.8)))
        if self.pressed("minus", "equals"):
            self.cam.fov = max(40, min(130, self.cam.fov + (10 if self.pressed("equals") else -10)))
        if self.pressed("h"):
            self.hud = not self.hud
        if self.pressed("p"):
            self.adaptive_palette = not self.adaptive_palette
        if self.pressed("g"):
            self.dither_mode = {"ordered": "floyd", "floyd": "none", "none": "ordered"}[self.dither_mode]
        if self.pressed("o") and (self.held("lctrl") or self.held("rctrl")):
            self.desk.file_dialog("open", self.open, title="Open 3D file", filter=(".bsp", ".obj"),
                                  path=os.path.dirname(self.path) if self.path else None)
        if self.player is not None:
            self.player.update(dt, fwd, side, jump=self.held("space"), up=self.axis("space", "c"))
        else:
            self.cam.fly(fwd * 3 * dt, side * 3 * dt, self.axis("space", "c") * 3 * dt)
        self._fps_n += 1
        now = time.monotonic()
        if now - self._fps_t >= 1:
            self.real_fps, self._fps_n, self._fps_t = self._fps_n / (now - self._fps_t), 0, now

    def resized(self):
        if self.renderer is not None:
            self.renderer.resize(self.width, self.height)

    def render(self, g):
        if self.pending is not None:          # also while paused (unfocused), so loading always finishes
            self._upload()
        if self.load_error:
            self.status, self.load_error = self.load_error, None
        if self.renderer is None or self.loading:
            g.clear("navy")
            msg = self.status
            g.text(self.width // 2, self.height // 2 - 8, msg[:self.width // 7], "white", center=True)
            if self.loading:
                n = int(time.monotonic() * 4) % 4
                g.text(self.width // 2, self.height // 2 + 8, "." * (n + 1), "white", center=True)
            return
        g.blit(self.renderer.render(self.cam, self.width, self.height))
        if self.dither_mode != "none":
            g.dither(self.dither_mode)

    def overlay(self, g):
        if not self.hud or self.renderer is None or self.loading:
            return
        mode = "" if self.player is None else ("noclip" if self.player.noclip else "walk")
        line = f"{self.real_fps:.0f} fps  {self.renderer.stats.get('triangles', 0)} tris  {mode}"
        for text, y in ((line, 3), (self.status, 18)):
            text = text[: max(1, (g.w - 8) // 7)]
            g.rect(3, y, g.text_width(text) + 6, 14, "black")
            g.text(6, y, text, "yellow" if y == 3 else "white")

    def context_menu(self, x, y):
        items = [{"id": "open", "label": "Open..."},
                 {"id": "noclip", "label": "Fly (noclip)", "checked": bool(self.player and self.player.noclip),
                  "enabled": self.player is not None},
                 {"id": "respawn", "label": "Back to Start", "enabled": self.level is not None},
                 {"id": "hud", "label": "Show HUD", "checked": self.hud},
                 {"id": "palette", "label": "Adapt Colors to Scene", "checked": self.adaptive_palette},
                 {"separator": True}]
        return items + (super().context_menu(x, y) or [])

    def on_menu(self, item_id):
        if item_id == "open":
            self.desk.file_dialog("open", self.open, title="Open 3D file", filter=(".bsp", ".obj"))
        elif item_id == "noclip" and self.player is not None:
            self.player.noclip = not self.player.noclip
        elif item_id == "respawn":
            self.respawn()
        elif item_id == "hud":
            self.hud = not self.hud
        elif item_id == "palette":
            self.adaptive_palette = not self.adaptive_palette
        else:
            super().on_menu(item_id)

    def game_state(self):
        s = {"file": self.path, "status": self.status, "loading": self.loading,
             "camera": {"pos": [round(float(v), 1) for v in self.cam.pos], "yaw_deg": round(math.degrees(self.cam.yaw), 1),
                        "pitch_deg": round(math.degrees(self.cam.pitch), 1), "fov": self.cam.fov},
             "fps": round(self.real_fps, 1), "gpu": gl3d.renderer_name(),
             "adaptive_palette": self.adaptive_palette, "screen_palette": [list(c) for c in self.desk.palettes.current]
             if self.desk.palettes.current else None}
        if self.renderer is not None:
            s.update(triangles=self.renderer.stats.get("triangles"), exposure=round(self.renderer.exposure, 2))
        if self.level is not None:
            s["map"] = self.level.info()
            s["mode"] = "noclip" if self.player.noclip else "walk"
            s["spawn_points"] = len(self.spawns)
        return s

    def close(self):
        if self.renderer is not None:
            self.renderer.release()
            self.renderer = None
