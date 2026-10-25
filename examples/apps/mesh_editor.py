"""Mesh Editor: a small polygon modeler built only from the public app APIs (UIApp, Viewport3D,
EditableMesh). Copy into ~/.vga32/apps/ or install it with install_app.

Mouse (in the 3D view): left click selects a face (or vertex), clicking a selected one deselects it;
left drag moves the selection; right drag orbits; middle drag pans; wheel zooms.
Keys: E extrude, I inset, S subdivide, M smooth, Del delete, A select all/none, Ctrl+Z / Ctrl+Y undo / redo.
Agents: app_call(command, args) with the cmd_ methods below ("commands" lists them).
"""
import math

import numpy as np

from vga32.gl3d import Material, MeshData
from vga32.mesh import EditableMesh
from vga32.render3d import OrbitCamera
from vga32.widgets import (Button, Checkbox, ColorPicker, Column, Label, NumberField, Radio, Row, ScrollView,
                           Separator, UIApp, Viewport3D)

SHAPES = {"cube": lambda s: EditableMesh.cube(s), "sphere": lambda s: EditableMesh.sphere(s / 2, 16, 10),
          "cylinder": lambda s: EditableMesh.cylinder(s / 2, s, 16), "plane": lambda s: EditableMesh.plane(s, s, 2)}


class MeshEditor(UIApp):
    app_id = "tools.mesh_editor"
    title = "Mesh Editor"
    icon = "star"
    category = "Graphics"
    size = (600, 360)
    adaptive_palette = True

    def build(self):
        self.mesh = EditableMesh.from_dict(self.storage["mesh"]) if self.storage.get("mesh") else EditableMesh.cube(1.5)
        self.sel_faces, self.sel_verts = set(), set()
        self.vp = Viewport3D(camera=OrbitCamera(target=(0, 0, 0), distance=6, yaw=0.7, pitch=-0.5),
                             on_pick=self.picked, on_drag=self.dragged, on_key=self.view_key, flex=1, pixel=1)
        r = self.vp.renderer
        if r is not None:
            r.background("#283040")
            r.sun((-0.5, -1, 0.3), "#f0e8d8", ambient="#505868")
            r.add(MeshData.grid(5, 0.5, "#506070"), name="grid")
            self.body = r.add(MeshData(np.zeros((0, 3))), Material(depth_offset=1.0), name="mesh")
            self.wire = r.add(MeshData(np.zeros((0, 3)), mode="lines"), Material(color="black", unlit=True), name="wire")
            self.wire.pickable = False
            self.dots = r.add(MeshData(np.zeros((0, 3)), mode="points"), Material(color="yellow", point_size=6,
                                                                                   depth_test=False), name="dots")
            self.dots.pickable = False
        side = Column([
            Label("Mesh Editor", bold=True),
            Radio(["Faces", "Vertices"], "Faces", horizontal=True, id="mode"),
            Row([Button("Cube", id="add_cube", w=64), Button("Sphere", id="add_sphere", w=64)], gap=4),
            Row([Button("Cylinder", id="add_cylinder", w=64), Button("Plane", id="add_plane", w=64)], gap=4),
            Separator(),
            Row([Button("Extrude", id="extrude", w=64), NumberField(0.5, -10, 10, 0.25, id="amount", w=64)], gap=4),
            Row([Button("Inset", id="inset", w=64), Button("Subdiv", id="subdivide", w=64)], gap=4),
            Row([Button("Smooth", id="smooth", w=64), Button("Delete", id="delete", w=64)], gap=4),
            Row([Button("Undo", id="undo", w=64), Button("Redo", id="redo", w=64)], gap=4),
            ColorPicker("gray", id="color", w=136),
            Row([Checkbox("Wire", True, id="wire"), Checkbox("Smooth", id="smooth_shading")], gap=6),
            Row([Button("Open", id="open", w=64), Button("Save", id="save", w=64)], gap=4),
            Label("", id="stats"),
        ], gap=5, padding=6)
        return Column([Row([self.vp, ScrollView(side, h=None, w=166)], gap=0, flex=1)], padding=0)

    def __init__(self, desk, **kw):
        super().__init__(desk, **kw)
        self.refresh(save=False)          # the widgets exist now

    # ----- display -----
    def refresh(self, save=True):
        if self.vp.renderer is None:
            return
        m = self.mesh
        smooth = bool(self.find("smooth_shading").value) if self.find("smooth_shading") else False
        self.body.mesh = m.to_meshdata(smooth=smooth, highlight=self.sel_faces)
        show_wire = self.find("wire").value if self.find("wire") else True
        self.wire.mesh = m.to_wire() if show_wire else MeshData(np.zeros((0, 3)), mode="lines")
        pts = m.vertices[sorted(self.sel_verts)] if self.sel_verts else np.zeros((0, 3))
        self.dots.mesh = MeshData.points(pts, "yellow") if len(pts) else MeshData(np.zeros((0, 3)), mode="points")
        st = m.stats()
        if self.find("stats") is not None:
            self.find("stats").text = (f"{st['vertices']} verts {st['faces']} faces\n"
                                       f"{len(self.sel_faces)} faces / {len(self.sel_verts)} verts selected")
        if save:
            self.storage["mesh"] = m.to_dict()
            self.save_storage()

    def selected_vertices(self):
        return sorted(self.sel_verts | set(self.mesh.face_vertices(self.sel_faces)))

    def edit(self):
        """Call before changing the mesh: keeps undo history."""
        self.mesh.snapshot()

    # ----- mouse and keys -----
    def picked(self, pick, x, y):
        if pick is None or pick.node is not self.body:
            self.sel_faces.clear()
            self.sel_verts.clear()
        elif self.find("mode").value == "Faces":
            f = int(self.body.mesh.face_of_triangle[pick.triangle])
            self.sel_faces ^= {f}
        else:
            v = self.mesh.nearest_vertex(pick.position)
            if v is not None:
                self.sel_verts ^= {v}
        self.refresh(save=False)
        self.emit("selection", faces=sorted(self.sel_faces), vertices=sorted(self.sel_verts))

    def dragged(self, dx, dy, x, y, pick):
        ids = self.selected_vertices()
        if not ids:
            return
        if not getattr(self, "_dragging", False):
            self.edit()
            self._dragging = True
        cam = self.vp.camera
        r, u, _ = cam.basis()
        s = cam.distance * 0.0035
        self.mesh.translate(ids, (r * dx - u * dy) * s)
        self.refresh(save=False)

    def mouse(self, x, y, buttons, pressed, released, wheel):
        super().mouse(x, y, buttons, pressed, released, wheel)
        if released and getattr(self, "_dragging", False):
            self._dragging = False
            self.refresh()

    def view_key(self, k):
        if not k.down:
            return False
        n = k.name.lower()
        if k.ctrl and n == "z":
            self.cmd_undo()
        elif k.ctrl and n == "y":
            self.cmd_redo()
        elif n == "e":
            self.cmd_extrude()
        elif n == "i":
            self.cmd_inset()
        elif n == "s":
            self.cmd_subdivide()
        elif n == "m":
            self.cmd_smooth()
        elif n == "delete":
            self.cmd_delete()
        elif n == "a":
            self.cmd_select(all=not (self.sel_faces or self.sel_verts))
        else:
            return False
        return True

    def on_click(self, w):
        wid = w.id or ""
        if wid.startswith("add_"):
            self.cmd_add(wid[4:])
        elif wid in ("extrude", "inset", "subdivide", "smooth", "delete", "undo", "redo"):
            getattr(self, "cmd_" + wid)()
        elif wid == "open":
            self.desk.file_dialog("open", lambda p: self.cmd_load(p), filter=(".obj",))
        elif wid == "save":
            self.desk.file_dialog("save", lambda p: self.cmd_save(p), name="mesh.obj", filter=(".obj",))

    def on_change(self, w):
        if w.id == "color" and self.sel_faces:
            self.cmd_color(w.value)
        elif w.id in ("wire", "smooth_shading"):
            self.refresh(save=False)

    # ----- commands (also what agents call with app_call) -----
    def cmd_add(self, shape="cube", size=1.0, at=(0, 0, 0)):
        "Add a primitive (cube | sphere | cylinder | plane) of a size at a position; selects its faces."
        if shape not in SHAPES:
            raise ValueError(f"shape must be one of {', '.join(SHAPES)}")
        self.edit()
        part = SHAPES[shape](float(size))
        part.colors = [tuple(c) for c in part.colors]
        self.sel_faces = set(self.mesh.add_mesh(part, at))
        self.sel_verts.clear()
        self.refresh()
        return self.mesh.stats()

    def cmd_select(self, faces=None, vertices=None, facing=None, all=False, none=False):
        "Select faces / vertices by index, faces facing a direction [x,y,z], all, or none."
        if none:
            self.sel_faces, self.sel_verts = set(), set()
        if all:
            self.sel_faces = set(range(len(self.mesh.faces)))
        if faces is not None:
            self.sel_faces = {int(f) for f in faces if 0 <= int(f) < len(self.mesh.faces)}
        if vertices is not None:
            self.sel_verts = {int(v) for v in vertices if 0 <= int(v) < len(self.mesh.vertices)}
        if facing is not None:
            self.sel_faces = set(self.mesh.faces_facing(facing))
        self.refresh(save=False)
        return {"faces": sorted(self.sel_faces), "vertices": sorted(self.sel_verts)}

    def cmd_extrude(self, distance=None):
        "Extrude the selected faces along their normal (distance, default the Extrude field)."
        if not self.sel_faces:
            return "select faces first"
        self.edit()
        d = float(distance if distance is not None else self.find("amount").value)
        self.sel_faces = set(self.mesh.extrude(sorted(self.sel_faces), d))
        self.refresh()
        return self.mesh.stats()

    def cmd_inset(self, amount=0.25):
        "Inset the selected faces (amount 0..1)."
        if not self.sel_faces:
            return "select faces first"
        self.edit()
        self.sel_faces = set(self.mesh.inset(sorted(self.sel_faces), float(amount)))
        self.refresh()
        return self.mesh.stats()

    def cmd_subdivide(self):
        "Subdivide the selected faces (or everything)."
        self.edit()
        self.mesh.subdivide(sorted(self.sel_faces) or None)
        self.sel_faces.clear()
        self.refresh()
        return self.mesh.stats()

    def cmd_smooth(self, iterations=1, factor=0.5):
        "Smooth the selected vertices (or everything)."
        self.edit()
        self.mesh.smooth(int(iterations), float(factor), self.selected_vertices() or None)
        self.refresh()
        return self.mesh.stats()

    def cmd_move(self, delta=(0, 0, 0)):
        "Move the selection by [dx, dy, dz]."
        self.edit()
        self.mesh.translate(self.selected_vertices(), delta)
        self.refresh()
        return "ok"

    def cmd_scale(self, factor=1.0):
        "Scale the selection about its center (number or [sx, sy, sz])."
        self.edit()
        self.mesh.scale(factor, self.selected_vertices() or None)
        self.refresh()
        return "ok"

    def cmd_rotate(self, axis="y", degrees=90.0):
        "Rotate the selection (or everything) about its center around x, y or z."
        from vga32.render3d import rotation
        a = math.radians(float(degrees))
        rot = {"x": (a, 0, 0), "y": (0, a, 0), "z": (0, 0, a)}[axis]
        ids = self.selected_vertices() or list(range(len(self.mesh.vertices)))
        c = self.mesh.vertices[ids].mean(0)
        self.edit()
        self.mesh.vertices[ids] = (self.mesh.vertices[ids] - c) @ rotation(*rot).T + c
        self.refresh()
        return "ok"

    def cmd_color(self, color="red"):
        "Color the selected faces (palette name or #rrggbb)."
        self.edit()
        self.mesh.set_color(self.sel_faces or range(len(self.mesh.faces)), color)
        self.refresh()
        return "ok"

    def cmd_delete(self):
        "Delete the selected faces and vertices."
        self.edit()
        if self.sel_faces:
            self.mesh.delete_faces(self.sel_faces)
        elif self.sel_verts:
            self.mesh.delete_vertices(self.sel_verts)
        self.sel_faces, self.sel_verts = set(), set()
        self.refresh()
        return self.mesh.stats()

    def cmd_clear(self):
        "Start with an empty mesh."
        self.edit()
        self.mesh = EditableMesh()
        self.sel_faces, self.sel_verts = set(), set()
        self.refresh()
        return "ok"

    def cmd_undo(self):
        "Undo the last edit."
        ok = self.mesh.undo()
        self.sel_faces, self.sel_verts = set(), set()
        self.refresh()
        return ok

    def cmd_redo(self):
        "Redo."
        ok = self.mesh.redo()
        self.refresh()
        return ok

    def cmd_set_mesh(self, vertices, faces, colors=None):
        "Replace the mesh with your own vertices [[x,y,z]...], faces [[i,j,k,...]...] and optional face colors."
        self.edit()
        self.mesh = EditableMesh(vertices, faces, colors)
        self.sel_faces, self.sel_verts = set(), set()
        self.refresh()
        return self.mesh.stats()

    def cmd_mesh(self):
        "The current mesh as {vertices, faces, colors}."
        return self.mesh.to_dict()

    def cmd_stats(self):
        "Counts and the selection."
        return {**self.mesh.stats(), "selected_faces": sorted(self.sel_faces), "selected_vertices": sorted(self.sel_verts),
                "bounds": [b.round(3).tolist() for b in self.mesh.bounds()]}

    def cmd_view(self, yaw=None, pitch=None, distance=None, frame=False):
        "Point the camera: yaw / pitch in degrees, distance, or frame=true to fit the mesh."
        cam = self.vp.camera
        if frame and len(self.mesh.vertices):
            cam.frame(*self.mesh.bounds())
        if yaw is not None:
            cam.yaw = math.radians(float(yaw))
        if pitch is not None:
            cam.pitch = math.radians(float(pitch))
        if distance is not None:
            cam.distance = float(distance)
        cam.update()
        return {"yaw": round(math.degrees(cam.yaw), 1), "pitch": round(math.degrees(cam.pitch), 1),
                "distance": round(cam.distance, 2)}

    def cmd_save(self, path):
        "Save as Wavefront OBJ (face colors go in path.colors.json)."
        self.mesh.save_obj(path)
        self.log("saved", path)
        return path

    def cmd_load(self, path):
        "Load an OBJ file."
        self.edit()
        self.mesh = EditableMesh.load_obj(path)
        self.sel_faces, self.sel_verts = set(), set()
        self.refresh()
        self.cmd_view(frame=True)
        return self.mesh.stats()

    def state(self):
        return {**super().state(), "mesh": self.mesh.stats(), "selected_faces": len(self.sel_faces),
                "selected_vertices": len(self.sel_verts)}
