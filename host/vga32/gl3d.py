"""GPU 3D engine (OpenGL 3.3 through moderngl), rendered offscreen and dithered onto the screen palette.

For anything 3D an app wants: games, viewers, editors, visualisations. Needs `pip install moderngl`
and an OpenGL 3.3 driver (on Linux it runs headless through EGL). Check gl3d.available() first.

    from vga32.gl3d import Renderer, Material, MeshData, checker
    from vga32.render3d import Camera, OrbitCamera

    r = Renderer(320, 200)
    r.sky_gradient("navy", "#80b0ff"); r.sun((-0.4, -1, 0.6), "#fff0d0", ambient="#404050")
    floor = r.add(MeshData.plane(40, 40, uv_scale=20), Material(texture=checker()))
    crate = r.add(MeshData.box(1, 1, 1), Material(color="#c08040"), pos=(0, 0.5, 4), name="crate")
    lid = r.add(MeshData.box(1.1, 0.1, 1.1), Material(color="red"), pos=(0, 0.55, 0), parent=crate)
    img = r.render(Camera(pos=(0, 1.6, 0)))          # PIL RGB image; in a GameApp: g.blit(img); g.dither()
    hit = r.pick(cam, x, y)                          # node / triangle / world point under a pixel

Scene graph: Renderer.add() returns a Node (pos, rot = (pitch, yaw, roll) radians, scale, or .matrix;
parent / children; name; visible; pickable; data dict). r.root is the top; r.find(name); r.walk().

MeshData: vertex arrays (positions, normals, uv, uv2, colors) drawn as triangles, lines or points,
optionally with an index buffer. Change it any time with mesh.update(...) - it is re-uploaded.
Builders: plane, box, sphere, cylinder, from_mesh (render3d.Mesh), from_indexed (vertices + faces),
lines (segment pairs), points, grid, axes, wireframe(mesh).

Material: color, opacity (blended, sorted), texture, lightmap, unlit, alpha_test, warp, sky, scroll,
filter, cull ("none" | "back" | "front"), wireframe, depth_test, depth_write, depth_offset (push faces
back so wire overlays drawn on them don't flicker), point_size, emissive,
shader=Shader(vertex, fragment) with uniforms={...} for fully custom looks.
Node.on_draw = fn(renderer, node, view, proj) draws with raw moderngl (renderer.ctx) for anything else.

Cameras come from render3d: Camera (look_at, ortho), OrbitCamera (orbit, pan, zoom, frame).
Coordinates: x right, y up, z forward (left-handed).
"""
import math
import os
import sys
import time

import numpy as np
from PIL import Image

from .render3d import Camera, Mesh, OrbitCamera, rotation, to_rgb  # noqa: F401  (re-exported)

_ctx = None
_error = None


def available():
    """True if a GPU context can be created (moderngl installed and a driver present)."""
    return context() is not None


def unavailable_reason():
    context()
    return _error


def context():
    global _ctx, _error
    if _ctx is not None or _error is not None:
        return _ctx
    try:
        import moderngl
    except ImportError:
        _error = "the GPU renderer needs moderngl: pip install moderngl"
        return None
    attempts = [{"backend": "egl"}, {}] if sys.platform.startswith("linux") else [{}]
    errs = []
    for kw in attempts:
        try:
            _ctx = moderngl.create_standalone_context(require=330, **kw)
            return _ctx
        except Exception as e:      # no EGL / no display / old driver
            errs.append(f"{kw.get('backend', 'default')}: {e}")
    _error = "no OpenGL 3.3 context (" + "; ".join(errs) + ")"
    return None


def renderer_name():
    c = context()
    return c.info.get("GL_RENDERER", "?") if c else None


# ----- math -----
def perspective(fov_deg, aspect, near, far):
    """Projection for a +z-forward (left-handed) view, OpenGL clip space. fov is vertical."""
    f = 1.0 / math.tan(math.radians(fov_deg) / 2)
    return np.array([[f / aspect, 0, 0, 0],
                     [0, f, 0, 0],
                     [0, 0, (far + near) / (far - near), -2 * far * near / (far - near)],
                     [0, 0, 1, 0]], dtype=np.float64)


def orthographic(height, aspect, near, far):
    w = height * aspect
    return np.array([[2 / w, 0, 0, 0],
                     [0, 2 / height, 0, 0],
                     [0, 0, 2 / (far - near), -(far + near) / (far - near)],
                     [0, 0, 0, 1]], dtype=np.float64)


def view_matrix(cam):
    r, u, f = cam.basis()
    p = cam.pos
    return np.array([[*r, -r @ p], [*u, -u @ p], [*f, -f @ p], [0, 0, 0, 1]], dtype=np.float64)


def model_matrix(pos=(0, 0, 0), rot=(0, 0, 0), scale=1.0):
    m = np.eye(4)
    s = np.array([scale] * 3 if np.isscalar(scale) else scale, dtype=np.float64)
    m[:3, :3] = rotation(*rot) * s
    m[:3, 3] = pos
    return m


def _mat(m):
    return np.asarray(m, dtype="f4").T.tobytes()       # moderngl wants column-major


def _set(prog, name, value):
    """Set a uniform if the program has it (the GLSL compiler drops unused ones), skipping
    values that are already set. The cache lives on the program: {name: [uniform, last value]}."""
    table = getattr(prog, "_vga_uniforms", None)
    if table is None:
        table = prog._vga_uniforms = {k: [prog[k], None] for k in prog}
    entry = table.get(name)
    if entry is None:
        return
    if isinstance(value, np.ndarray) and value.shape == (4, 4):
        value = _mat(value)
    if entry[1] == value:
        return
    entry[1] = value
    if isinstance(value, (bytes, bytearray)):
        entry[0].write(value)
    else:
        entry[0].value = value


# ----- geometry -----
class MeshData:
    """Vertex arrays for the GPU. mode: "triangles" (every 3 vertices, or 3 indices, make a triangle),
    "lines" (pairs) or "points". Edit the arrays and call update() - or pass new arrays to update()."""

    def __init__(self, positions, normals=None, uv=None, uv2=None, colors=None, indices=None, mode="triangles"):
        self.mode = mode
        self.indices = None
        self.version = 0
        self._gpu = {}
        self._set(positions, normals, uv, uv2, colors, indices)

    def _set(self, positions, normals=None, uv=None, uv2=None, colors=None, indices=None):
        self.positions = np.asarray(positions, np.float32).reshape(-1, 3)
        n = len(self.positions)
        self.indices = None if indices is None else np.asarray(indices, np.uint32).ravel()
        if normals is not None:
            self.normals = np.asarray(normals, np.float32).reshape(-1, 3)
        elif self.mode == "triangles":
            self.normals = self._smooth_normals() if self.indices is not None else self._flat_normals()
        else:
            self.normals = np.zeros((n, 3), np.float32)
        self.uv = np.asarray(uv, np.float32).reshape(-1, 2) if uv is not None else np.zeros((n, 2), np.float32)
        self.uv2 = np.asarray(uv2, np.float32).reshape(-1, 2) if uv2 is not None else np.zeros((n, 2), np.float32)
        if colors is None:
            self.colors = np.ones((n, 3), np.float32)
        else:
            c = np.asarray(colors, np.float32)
            if c.ndim == 1:
                c = np.tile(c, (n, 1))
            self.colors = (c / 255 if c.max() > 1.0 else c).reshape(-1, 3)

    def update(self, positions=None, normals=None, uv=None, uv2=None, colors=None, indices=None):
        """Replace any arrays (or, with no arguments, re-upload arrays you edited in place).
        New positions keep the other arrays when the vertex count is unchanged; normals are
        recomputed unless given."""
        if positions is not None:
            n = len(np.asarray(positions).reshape(-1, 3))
            same = n == len(self.positions)
            self._set(positions, normals,
                      uv if uv is not None else (self.uv if same else None),
                      uv2 if uv2 is not None else (self.uv2 if same else None),
                      colors if colors is not None else (self.colors if same else None),
                      indices if indices is not None else (self.indices if same else None))
        else:
            for name, val in (("normals", normals), ("uv", uv), ("uv2", uv2), ("colors", colors)):
                if val is not None:
                    setattr(self, name, np.asarray(val, np.float32).reshape(len(self.positions), -1))
            if indices is not None:
                self.indices = np.asarray(indices, np.uint32).ravel()
        self.version += 1
        return self

    def _tris(self):
        p = self.positions
        return p[self.indices].reshape(-1, 3, 3) if self.indices is not None else p[:len(p) // 3 * 3].reshape(-1, 3, 3)

    def _flat_normals(self):
        p = self._tris()
        n = np.cross(p[:, 1] - p[:, 0], p[:, 2] - p[:, 0])
        n /= np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-9)
        out = np.repeat(n, 3, axis=0).astype(np.float32)
        if len(out) < len(self.positions):
            out = np.vstack([out, np.zeros((len(self.positions) - len(out), 3), np.float32)])
        return out

    def _smooth_normals(self):
        p = self.positions
        tri = self.indices.reshape(-1, 3)
        fn = np.cross(p[tri[:, 1]] - p[tri[:, 0]], p[tri[:, 2]] - p[tri[:, 0]])
        n = np.zeros_like(p)
        for k in range(3):
            np.add.at(n, tri[:, k], fn)
        return (n / np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-9)).astype(np.float32)

    @property
    def triangles(self):
        if self.mode != "triangles":
            return 0
        return (len(self.indices) if self.indices is not None else len(self.positions)) // 3

    @property
    def count(self):
        return len(self.indices) if self.indices is not None else len(self.positions)

    def bounds(self):
        if len(self.positions) == 0:
            return np.zeros(3), np.zeros(3)
        return self.positions.min(axis=0), self.positions.max(axis=0)

    def raycast(self, origin, direction):
        """Nearest hit of a ray with the triangles (in the mesh's own space): (t, triangle index) or None."""
        if self.mode != "triangles" or self.triangles == 0:
            return None
        tri = self._tris().astype(np.float64)
        o, d = np.asarray(origin, float), np.asarray(direction, float)
        e1, e2 = tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0]
        pv = np.cross(d, e2)
        det = (e1 * pv).sum(1)
        ok = np.abs(det) > 1e-12
        inv = np.where(ok, 1 / np.where(ok, det, 1), 0)
        tv = o - tri[:, 0]
        u = (tv * pv).sum(1) * inv
        qv = np.cross(tv, e1)
        v = (d * qv).sum(1) * inv
        t = (e2 * qv).sum(1) * inv
        hit = ok & (u >= 0) & (v >= 0) & (u + v <= 1) & (t > 1e-9)
        if not hit.any():
            return None
        t = np.where(hit, t, np.inf)
        i = int(t.argmin())
        return float(t[i]), i

    # ----- builders -----
    @staticmethod
    def from_indexed(vertices, faces, colors=None, smooth=False, uv=None):
        """Polygons (lists of vertex indices, any size) -> triangles. colors: one per face or per vertex.
        smooth shares normals across faces; flat gives each face its own."""
        v = np.asarray(vertices, np.float64).reshape(-1, 3)
        tris, fidx = [], []
        for fi, f in enumerate(faces):
            for i in range(1, len(f) - 1):
                tris.append((f[0], f[i], f[i + 1]))
                fidx.append(fi)
        tris = np.asarray(tris, np.int64).reshape(-1, 3)
        fidx = np.asarray(fidx, np.int64)
        c = None
        if colors is not None:
            c = np.array([x if isinstance(x, np.ndarray) else to_rgb(x) for x in colors], np.float32)
        per_face = c is not None and len(c) == len(faces) and len(faces) != len(v)
        if smooth and not per_face:
            # shared vertices: indexed, normals averaged across faces
            m = MeshData(v, colors=c, indices=tris.ravel(), uv=uv)
        else:
            flat = tris.ravel()
            col = None if c is None else (c[fidx].repeat(3, 0) if per_face else c[flat])
            m = MeshData(v[flat], colors=col, uv=None if uv is None else np.asarray(uv)[flat])
            if smooth:
                m.normals = MeshData(v, indices=flat).normals[flat]
        m.face_of_triangle = fidx          # triangle index -> polygon index (for picking)
        return m

    @staticmethod
    def from_mesh(mesh, uv_mode="box"):
        """From a render3d.Mesh (fan-triangulated). Face colors become vertex colors; uv is projected
        per face (box mapping) so textures tile one per unit."""
        pos, col, uv = [], [], []
        for face, c in zip(mesh.faces, mesh.colors):
            v = mesh.vertices[list(face)]
            nrm = np.cross(v[1] - v[0], v[2] - v[0])
            ax = np.argmax(np.abs(nrm))
            dims = [i for i in range(3) if i != ax]
            for i in range(1, len(face) - 1):
                for p in (v[0], v[i], v[i + 1]):
                    pos.append(p)
                    col.append(np.array(c, float) / 255)
                    uv.append((p[dims[0]], -p[dims[1]]))
        return MeshData(pos, uv=uv, colors=col)

    @staticmethod
    def plane(w=10.0, d=10.0, uv_scale=1.0, y=0.0):
        x, z = w / 2, d / 2
        p = [(-x, y, -z), (-x, y, z), (x, y, z), (-x, y, -z), (x, y, z), (x, y, -z)]
        uv = [(0, 0), (0, uv_scale), (uv_scale, uv_scale), (0, 0), (uv_scale, uv_scale), (uv_scale, 0)]
        return MeshData(p, normals=[(0, 1, 0)] * 6, uv=uv)

    @staticmethod
    def box(w=1.0, h=1.0, d=1.0):
        return MeshData.from_mesh(Mesh.box(w, h, d))

    @staticmethod
    def sphere(r=0.5, segments=24, rings=16):
        pos, nrm, uv = [], [], []

        def pt(i, j):
            phi, th = math.pi * i / rings, 2 * math.pi * j / segments
            n = (math.sin(phi) * math.cos(th), math.cos(phi), math.sin(phi) * math.sin(th))
            return [r * c for c in n], n, (j / segments, i / rings)
        for i in range(rings):
            for j in range(segments):
                a, b, c, d = pt(i, j), pt(i, j + 1), pt(i + 1, j + 1), pt(i + 1, j)
                for q in (a, c, b, a, d, c):
                    pos.append(q[0]); nrm.append(q[1]); uv.append(q[2])
        return MeshData(pos, normals=nrm, uv=uv)

    @staticmethod
    def cylinder(r=0.5, h=1.0, segments=24):
        return MeshData.from_mesh(Mesh.cylinder(r, h, "white", segments))

    @staticmethod
    def lines(segments, colors=None):
        """segments: [(a, b), ...] or an (N, 2, 3) array; colors: one, per segment or per vertex."""
        p = np.asarray(segments, np.float32).reshape(-1, 3)
        c = None
        if colors is not None:
            if isinstance(colors, (str, tuple)) and (isinstance(colors, str) or len(colors) == 3):
                c = np.tile(np.array(to_rgb(colors), np.float32), (len(p), 1))
            else:
                cl = np.array([to_rgb(x) for x in colors], np.float32)
                c = cl.repeat(2, 0) if len(cl) == len(p) // 2 else cl
        return MeshData(p, colors=c, mode="lines")

    @staticmethod
    def points(points, colors=None):
        p = np.asarray(points, np.float32).reshape(-1, 3)
        c = None if colors is None else (np.tile(np.array(to_rgb(colors), np.float32), (len(p), 1))
                                         if isinstance(colors, str) else np.array([to_rgb(x) for x in colors], np.float32))
        return MeshData(p, colors=c, mode="points")

    @staticmethod
    def grid(size=10.0, step=1.0, color="#606060", axis_colors=("#c04040", "#4040c0"), y=0.0):
        """Lines on the ground plane, with the x and z axes highlighted."""
        n = int(size / step)
        segs, cols = [], []
        for i in range(-n, n + 1):
            v = i * step
            segs += [((v, y, -size), (v, y, size)), ((-size, y, v), (size, y, v))]
            cols += [axis_colors[1] if i == 0 else color, axis_colors[0] if i == 0 else color]
        return MeshData.lines(segs, cols)

    @staticmethod
    def axes(length=1.0):
        """x red, y green, z blue."""
        o = (0, 0, 0)
        return MeshData.lines([(o, (length, 0, 0)), (o, (0, length, 0)), (o, (0, 0, length))], ["red", "lime", "blue"])

    @staticmethod
    def wireframe(mesh, color="black"):
        """The unique edges of a triangle mesh as lines (shared edges once)."""
        tri = mesh._tris()
        e = np.concatenate([tri[:, [0, 1]], tri[:, [1, 2]], tri[:, [2, 0]]])
        key = np.round(np.sort(e.reshape(-1, 6).reshape(-1, 2, 3), axis=1).reshape(-1, 6), 5)
        _, keep = np.unique(key, axis=0, return_index=True)
        return MeshData.lines(e[np.sort(keep)], color)

    @staticmethod
    def bbox(lo, hi, color="yellow"):
        lo, hi = np.asarray(lo, float), np.asarray(hi, float)
        c = [np.array([x, y, z]) for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2])]
        pairs = [(0, 1), (2, 3), (4, 5), (6, 7), (0, 2), (1, 3), (4, 6), (5, 7), (0, 4), (1, 5), (2, 6), (3, 7)]
        return MeshData.lines([(c[a], c[b]) for a, b in pairs], color)


class Shader:
    """Custom GLSL for a Material. Attributes available to the vertex shader: in_pos, in_normal,
    in_uv, in_uv2, in_color. Uniforms set for you when declared: u_model, u_view, u_proj, u_time,
    u_cam, u_color, u_opacity, u_tex (sampler2D, unit 0), u_has_tex, u_resolution, plus the
    material's uniforms dict. Write the fragment color to an `out vec4`."""

    def __init__(self, vertex, fragment):
        self.vertex, self.fragment = vertex, fragment


class Material:
    def __init__(self, color="white", texture=None, lightmap=None, unlit=False, alpha_test=False, warp=False,
                 sky=False, scroll=(0.0, 0.0), filter="nearest", double_sided=True, emissive=0.0, lightmap_scale=2.0,
                 opacity=1.0, cull=None, wireframe=False, depth_test=True, depth_write=None, point_size=3.0,
                 line_width=1.0, shader=None, uniforms=None, vertex_colors=True, depth_offset=0.0):
        self.color = np.array(to_rgb(color), np.float32) / 255
        self.texture = texture
        self.lightmap = lightmap
        self.unlit, self.alpha_test, self.warp, self.sky = unlit, alpha_test, warp, sky
        self.scroll = scroll
        self.filter = filter
        self.cull = cull if cull is not None else ("none" if double_sided else "back")
        self.emissive = emissive
        self.lightmap_scale = lightmap_scale
        self.opacity = float(opacity)
        self.wireframe = wireframe
        self.depth_test = depth_test
        self.depth_write = depth_write
        self.point_size, self.line_width = point_size, line_width
        self.shader = shader
        self.uniforms = dict(uniforms or {})
        self.vertex_colors = vertex_colors
        self.depth_offset = depth_offset     # > 0 pushes filled faces back so edges / decals drawn on them win

    @property
    def double_sided(self):
        return self.cull == "none"

    @property
    def blended(self):
        return self.opacity < 1.0

    def set_color(self, color):
        self.color = np.array(to_rgb(color), np.float32) / 255


def checker(size=64, a="#c0c0c0", b="#606060", cells=8):
    """A checkerboard texture (PIL image), handy as a placeholder."""
    ca, cb = to_rgb(a), to_rgb(b)
    yy, xx = np.mgrid[0:size, 0:size] * cells // size
    arr = np.where(((xx + yy) % 2 == 0)[:, :, None], ca, cb).astype(np.uint8)
    return Image.fromarray(arr)


class Node:
    """A scene-graph node: an optional mesh + material, a local transform and children."""
    _ids = 0

    def __init__(self, mesh=None, material=None, pos=(0, 0, 0), rot=(0, 0, 0), scale=1.0, name=None):
        Node._ids += 1
        self.id = Node._ids
        self.mesh, self.material = mesh, material
        self.pos = np.array(pos, np.float64)
        self.rot = np.array(rot, np.float64)
        self.scale = scale
        self.matrix = None                  # set to override pos/rot/scale with your own 4x4
        self.name = name
        self.visible = True
        self.pickable = True
        self.parent = None
        self.children = []
        self.data = {}
        self.on_draw = None                 # fn(renderer, node, view, proj): custom moderngl drawing
        self.order = 0                      # draw order among blended / overlay nodes
        self._key = self._local = None

    def local(self):
        if self.matrix is not None:
            return self.matrix
        key = (*np.ravel(self.pos).tolist(), *np.ravel(self.rot).tolist(), *np.ravel(self.scale).tolist())
        if key != self._key:            # rebuilt only when pos / rot / scale change
            self._key, self._local = key, model_matrix(self.pos, self.rot, self.scale)
        return self._local

    def world(self):
        """The node's transform in world space (parents applied)."""
        m = self.local()
        return self.parent.world() @ m if self.parent is not None else m

    model = world        # older name

    def add(self, child):
        if child.parent is not None:
            child.parent.children.remove(child)
        child.parent = self
        self.children.append(child)
        return child

    def remove(self, child):
        self.children.remove(child)
        child.parent = None

    def detach(self):
        if self.parent is not None:
            self.parent.remove(self)

    def walk(self, visible_only=False):
        if visible_only and not self.visible:
            return
        yield self
        for c in list(self.children):
            yield from c.walk(visible_only)

    def world_position(self):
        return self.world()[:3, 3].copy()

    def world_bounds(self):
        """Axis-aligned bounds of this node's mesh and its children in world space, or None."""
        pts = []
        for n in self.walk():
            if n.mesh is not None and len(n.mesh.positions):
                lo, hi = n.mesh.bounds()
                corners = np.array([[x, y, z, 1] for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2])])
                pts.append((corners @ n.world().T)[:, :3])
        if not pts:
            return None
        p = np.vstack(pts)
        return p.min(0), p.max(0)


class Pick:
    def __init__(self, node, triangle, position, distance):
        self.node, self.triangle, self.position, self.distance = node, triangle, position, distance

    def __repr__(self):
        return f"Pick(node={self.node.name or self.node.id}, triangle={self.triangle}, position={self.position})"


VERT = """
#version 330
uniform mat4 u_model, u_view, u_proj; uniform float u_point_size;
in vec3 in_pos; in vec3 in_normal; in vec2 in_uv; in vec2 in_uv2; in vec3 in_color;
out vec3 v_world; out vec3 v_normal; out vec2 v_uv; out vec2 v_uv2; out vec3 v_color;
void main() {
    vec4 w = u_model * vec4(in_pos, 1.0);
    v_world = w.xyz;
    v_normal = mat3(u_model) * in_normal;
    v_uv = in_uv; v_uv2 = in_uv2; v_color = in_color;
    gl_Position = u_proj * u_view * w;
    gl_PointSize = u_point_size;
}
"""

FRAG = """
#version 330
uniform vec3 u_color; uniform float u_time; uniform vec2 u_scroll; uniform float u_opacity;
uniform bool u_has_tex, u_has_lm, u_unlit, u_alpha_test, u_warp, u_sky, u_vertex_colors;
uniform sampler2D u_tex; uniform sampler2D u_lm; uniform float u_lm_scale; uniform float u_emissive;
uniform vec3 u_cam; uniform vec3 u_ambient; uniform vec3 u_sun_dir; uniform vec3 u_sun_color;
uniform int u_nlights; uniform vec4 u_light_pos[8]; uniform vec3 u_light_color[8];
uniform bool u_fog; uniform vec3 u_fog_color; uniform vec2 u_fog_range;
uniform int u_sky_mode; uniform vec3 u_sky_top; uniform vec3 u_sky_bottom; uniform samplerCube u_skybox;
uniform float u_exposure; uniform float u_gamma;
in vec3 v_world; in vec3 v_normal; in vec2 v_uv; in vec2 v_uv2; in vec3 v_color;
out vec4 frag;

vec3 sky(vec3 d) {
    if (u_sky_mode == 2) return texture(u_skybox, d).rgb;
    float t = clamp(d.y * 0.5 + 0.5, 0.0, 1.0);
    return mix(u_sky_bottom, u_sky_top, t);
}

void main() {
    if (u_sky) { frag = vec4(pow(sky(normalize(v_world - u_cam)) * u_exposure, vec3(1.0 / u_gamma)), 1.0); return; }
    vec2 uv = v_uv + u_scroll * u_time;
    if (u_warp) uv += 0.08 * sin(uv.yx * 6.2831 * 0.5 + u_time * 1.5);
    vec4 base = vec4(u_color * (u_vertex_colors ? v_color : vec3(1.0)), u_opacity);
    if (u_has_tex) base *= texture(u_tex, uv);
    if (u_alpha_test && base.a < 0.5) discard;
    vec3 light;
    if (u_unlit || u_warp) light = vec3(1.0);
    else if (u_has_lm) light = texture(u_lm, v_uv2).rgb * u_lm_scale;
    else {
        vec3 n = normalize(v_normal);
        if (dot(n, u_cam - v_world) < 0.0) n = -n;      // light whichever side we are looking at
        light = u_ambient + u_sun_color * max(dot(n, -u_sun_dir), 0.0);
        for (int i = 0; i < u_nlights; i++) {
            vec3 L = u_light_pos[i].xyz - v_world;
            float dist = length(L);
            float att = clamp(1.0 - dist / u_light_pos[i].w, 0.0, 1.0);
            light += u_light_color[i] * max(dot(n, L / max(dist, 1e-4)), 0.0) * att * att;
        }
    }
    vec3 c = base.rgb * light + base.rgb * u_emissive;
    if (u_fog) {
        float k = clamp((length(v_world - u_cam) - u_fog_range.x) / max(u_fog_range.y - u_fog_range.x, 1e-4), 0.0, 1.0);
        c = mix(c, u_fog_color, k);
    }
    frag = vec4(pow(max(c * u_exposure, 0.0), vec3(1.0 / u_gamma)), base.a);
}
"""

PICK_FRAG = """
#version 330
uniform uint u_id;
out uvec2 frag;
void main() { frag = uvec2(u_id, uint(gl_PrimitiveID) + 1u); }
"""

BG_VERT = """
#version 330
in vec2 in_pos; out vec2 v_ndc;
void main() { v_ndc = in_pos; gl_Position = vec4(in_pos, 0.9999, 1.0); }
"""
BG_FRAG = """
#version 330
uniform mat4 u_inv; uniform vec3 u_cam; uniform bool u_ortho; uniform vec3 u_fwd;
uniform int u_sky_mode; uniform vec3 u_sky_top; uniform vec3 u_sky_bottom; uniform samplerCube u_skybox;
uniform float u_exposure; uniform float u_gamma;
in vec2 v_ndc; out vec4 frag;
void main() {
    vec4 p = u_inv * vec4(v_ndc, 1.0, 1.0);
    vec3 d = u_ortho ? u_fwd : normalize(p.xyz / p.w - u_cam);
    vec3 c = u_sky_mode == 2 ? texture(u_skybox, d).rgb : mix(u_sky_bottom, u_sky_top, clamp(d.y * 0.5 + 0.5, 0.0, 1.0));
    if (u_sky_mode == 0) c = u_sky_bottom;
    frag = vec4(pow(c * u_exposure, vec3(1.0 / u_gamma)), 1.0);
}
"""


class Renderer:
    def __init__(self, width, height, near=0.05, far=1000.0):
        ctx = context()
        if ctx is None:
            raise RuntimeError(unavailable_reason())
        import moderngl
        self.mgl = moderngl
        self.ctx = ctx
        self.width, self.height = int(width), int(height)
        self.near, self.far = near, far
        self.root = Node(name="root")
        self._ambient = np.array([0.35, 0.35, 0.4], np.float32)
        self._sun_dir = np.array([-0.4, -1.0, 0.6], np.float32)
        self._sun_dir /= np.linalg.norm(self._sun_dir)
        self._sun_color = np.array([0.8, 0.78, 0.7], np.float32)
        self.lights = []                       # [pos(3), color(3), radius]
        self._fog = None
        self._sky_mode, self._sky = 1, (np.array([0.1, 0.15, 0.4], np.float32), np.array([0.6, 0.75, 1.0], np.float32))
        self._skybox = None
        self.prog = ctx.program(vertex_shader=VERT, fragment_shader=FRAG)
        self.pick_prog = ctx.program(vertex_shader=VERT, fragment_shader=PICK_FRAG)
        self.bg = ctx.program(vertex_shader=BG_VERT, fragment_shader=BG_FRAG)
        # Each sampler gets its own unit: a 2D and a cube sampler sharing unit 0 is GL_INVALID_OPERATION.
        for prog in (self.prog, self.bg):
            for name, unit in (("u_tex", 0), ("u_lm", 1), ("u_skybox", 2)):
                _set(prog, name, unit)
        quad = np.array([-1, -1, 3, -1, -1, 3], np.float32)
        self._bg_vao = ctx.vertex_array(self.bg, [(ctx.buffer(quad.tobytes()), "2f", "in_pos")])
        self._textures = {}
        self._programs = {}
        self.stats = {}
        self.exposure = 1.0          # brightness multiplier
        self.gamma = 1.0             # > 1 lifts dark tones (16 colors have few dark shades)
        self.auto_exposure = None    # e.g. 0.4: adapt exposure so the frame's mean brightness heads there
        self.t0 = time.monotonic()
        self.last_view = self.last_proj = None
        self._resize(self.width, self.height)

    def _resize(self, w, h):
        self.width, self.height = w, h
        self.color_tex = self.ctx.texture((w, h), 4)
        self.depth = self.ctx.depth_renderbuffer((w, h))
        self.fbo = self.ctx.framebuffer([self.color_tex], self.depth)
        self._pick_fbo = None

    def resize(self, w, h):
        w, h = max(1, int(w)), max(1, int(h))
        if (w, h) != (self.width, self.height):
            self.fbo.release()
            if self._pick_fbo is not None:
                self._pick_fbo.release()
            self._resize(w, h)

    # ----- scene -----
    def add(self, mesh=None, material=None, pos=(0, 0, 0), rot=(0, 0, 0), scale=1.0, parent=None, name=None):
        """Add a node (with an optional mesh) under parent (default the root). Returns the Node."""
        if isinstance(mesh, Mesh):
            mesh = MeshData.from_mesh(mesh)
        if mesh is not None and material is None:
            material = Material(unlit=mesh.mode != "triangles")
        node = Node(mesh, material, pos, rot, scale, name)
        (parent or self.root).add(node)
        return node

    def group(self, name=None, pos=(0, 0, 0), rot=(0, 0, 0), scale=1.0, parent=None):
        """An empty node to hang others from."""
        return self.add(None, None, pos, rot, scale, parent, name)

    def remove(self, node):
        node.detach()

    def clear(self):
        self.root.children = []

    @property
    def nodes(self):
        return [n for n in self.root.walk() if n is not self.root]

    def walk(self):
        return self.nodes

    def find(self, name):
        return next((n for n in self.root.walk() if n.name == name), None)

    def find_id(self, node_id):
        return next((n for n in self.root.walk() if n.id == node_id), None)

    def sun(self, direction=(-0.4, -1, 0.6), color="#d0c8b0", ambient=None):
        d = np.array(direction, np.float32)
        self._sun_dir = d / (np.linalg.norm(d) or 1)
        self._sun_color = np.array(to_rgb(color), np.float32) / 255
        if ambient is not None:
            self._ambient = np.array(to_rgb(ambient), np.float32) / 255

    def ambient(self, color):
        self._ambient = np.array(to_rgb(color), np.float32) / 255

    def point_light(self, pos, color="white", radius=10.0, intensity=1.0):
        """Up to 8 point lights; returns the light (a list you can edit: [pos, color, radius])."""
        light = [np.array(pos, np.float32), np.array(to_rgb(color), np.float32) / 255 * intensity, float(radius)]
        self.lights.append(light)
        del self.lights[:-8]
        return light

    def fog(self, color=None, start=10.0, end=50.0):
        self._fog = None if color is None else (np.array(to_rgb(color), np.float32) / 255, float(start), float(end))

    def sky_gradient(self, top="navy", bottom="sky"):
        self._sky_mode = 1
        self._sky = (np.array(to_rgb(top), np.float32) / 255, np.array(to_rgb(bottom), np.float32) / 255)

    def background(self, color):
        """A plain background color instead of a sky."""
        self._sky_mode = 0
        c = np.array(to_rgb(color), np.float32) / 255
        self._sky = (c, c)

    def skybox(self, faces):
        """faces: six PIL images or paths for +x, -x, +y, -y, +z, -z (right, left, up, down, front, back)."""
        imgs = [self._load_img(f).convert("RGB") for f in faces]
        size = min(min(i.size) for i in imgs)
        imgs = [i.resize((size, size)) for i in imgs]
        cube = self.ctx.texture_cube((size, size), 3, b"".join(i.tobytes() for i in imgs))
        cube.filter = (self.mgl.LINEAR, self.mgl.LINEAR)
        self._skybox = cube
        self._sky_mode = 2

    @staticmethod
    def _load_img(src):
        if isinstance(src, Image.Image):
            return src
        from . import images
        return images.decode(images.read_bytes(str(src))) if not os.path.exists(str(src)) else Image.open(src)

    def texture(self, src, filt="nearest"):
        """A GPU texture from a PIL image or path, cached. filt: nearest (crisp retro texels),
        linear (smooth, mipmapped) or lightmap (smooth, clamped at the edges). After drawing into
        a PIL image you use as a texture, call update_texture(image)."""
        key = (id(src), filt)
        hit = self._textures.get(key)
        if hit is not None:
            return hit[0]
        im = self._load_img(src).convert("RGBA").transpose(Image.FLIP_TOP_BOTTOM)
        tex = self.ctx.texture(im.size, 4, im.tobytes())
        if filt == "linear":
            tex.build_mipmaps()
            tex.filter = (self.mgl.LINEAR_MIPMAP_LINEAR, self.mgl.LINEAR)
        elif filt == "lightmap":
            tex.filter = (self.mgl.LINEAR, self.mgl.LINEAR)
            tex.repeat_x = tex.repeat_y = False
        else:
            tex.filter = (self.mgl.NEAREST, self.mgl.NEAREST)
        self._textures[key] = (tex, src)       # keeping src alive keeps its id() unique
        return tex

    def update_texture(self, src):
        """Forget the GPU copy of a texture so the next frame uploads the image again."""
        for key in [k for k in self._textures if k[0] == id(src)]:
            self._textures.pop(key)[0].release()

    def _program(self, shader):
        key = id(shader)
        hit = self._programs.get(key)
        if hit is None:
            prog = self.ctx.program(vertex_shader=shader.vertex, fragment_shader=shader.fragment)
            _set(prog, "u_tex", 0)
            hit = self._programs[key] = (prog, shader, {})
        return hit

    def _vao(self, mesh, prog, cache):
        key = (id(prog),)
        entry = mesh._gpu.get(key)
        if entry is not None and entry[0] == mesh.version:
            return entry[1]
        if entry is not None:
            for obj in entry[2]:
                obj.release()
        data = np.hstack([mesh.positions, mesh.normals, mesh.uv, mesh.uv2, mesh.colors]).astype("f4")
        vbo = self.ctx.buffer(data.tobytes())
        attrs = ["in_pos", "in_normal", "in_uv", "in_uv2", "in_color"]
        parts = ["3f", "3f", "2f", "2f", "3f"]
        used = [a in prog for a in attrs]
        # attributes a program doesn't use (custom shaders, the pick pass) become padding
        fmt = " ".join(p if u else f"{int(p[0]) * 4}x" for p, u in zip(parts, used))
        content = [(vbo, fmt, *[a for a, u in zip(attrs, used) if u])]
        ibo = self.ctx.buffer(mesh.indices.astype("u4").tobytes()) if mesh.indices is not None else None
        vao = self.ctx.vertex_array(prog, content, index_buffer=ibo, index_element_size=4)
        mesh._gpu[key] = (mesh.version, vao, [vao, vbo] + ([ibo] if ibo is not None else []))
        return vao

    # ----- cameras and coordinates -----
    def _vfov(self, cam):
        """Camera.fov is horizontal (as in render3d); OpenGL wants the vertical angle."""
        return math.degrees(2 * math.atan(math.tan(math.radians(cam.fov) / 2) * self.height / self.width))

    def projection(self, cam):
        near = getattr(cam, "near", None) or self.near
        far = getattr(cam, "far", None) or self.far
        if getattr(cam, "ortho", None):
            return orthographic(cam.ortho, self.width / self.height, -far, far)
        return perspective(self._vfov(cam), self.width / self.height, near, far)

    def ray(self, cam, x, y):
        """World-space ray (origin, unit direction) through pixel (x, y) of a width x height render."""
        proj, view = self.projection(cam), view_matrix(cam)
        inv = np.linalg.inv(proj @ view)
        nx, ny = (x + 0.5) / self.width * 2 - 1, 1 - (y + 0.5) / self.height * 2
        a = inv @ [nx, ny, -1, 1]
        b = inv @ [nx, ny, 1, 1]
        a, b = a[:3] / a[3], b[:3] / b[3]
        d = b - a
        return a, d / np.linalg.norm(d)

    def project(self, cam, point):
        """World point -> (x, y, depth) in pixels of a width x height render (depth < 0: behind)."""
        p = self.projection(cam) @ view_matrix(cam) @ np.append(np.asarray(point, float), 1.0)
        if abs(p[3]) < 1e-12:
            return None
        n = p[:3] / p[3]
        return (n[0] + 1) / 2 * self.width, (1 - n[1]) / 2 * self.height, p[3]

    # ----- drawing -----
    def _common(self, p, cam, view, proj, t):
        _set(p, "u_view", view)
        _set(p, "u_proj", proj)
        _set(p, "u_time", t)
        _set(p, "u_cam", tuple(float(v) for v in cam.pos))
        _set(p, "u_resolution", (float(self.width), float(self.height)))
        _set(p, "u_ambient", tuple(self._ambient))
        _set(p, "u_sun_dir", tuple(self._sun_dir))
        _set(p, "u_sun_color", tuple(self._sun_color))
        _set(p, "u_exposure", self.exposure)
        _set(p, "u_gamma", self.gamma)

    def _draw_node(self, node, view, proj, t):
        ctx, mgl = self.ctx, self.mgl
        m = node.material or Material()
        mesh = node.mesh
        if m.shader is not None:
            p, _, _ = self._program(m.shader)
            self._common(p, self._cam, view, proj, t)
        else:
            p = self.prog
        world = node.world()
        _set(p, "u_model", world)
        _set(p, "u_color", tuple(m.color))
        _set(p, "u_opacity", m.opacity)
        _set(p, "u_has_tex", m.texture is not None)
        if m.texture is not None:
            self.texture(m.texture, m.filter).use(0)
        _set(p, "u_has_lm", m.lightmap is not None)
        if m.lightmap is not None:
            self.texture(m.lightmap, "lightmap").use(1)
        _set(p, "u_lm_scale", m.lightmap_scale)
        _set(p, "u_unlit", m.unlit or mesh.mode != "triangles")
        _set(p, "u_alpha_test", m.alpha_test)
        _set(p, "u_warp", m.warp)
        _set(p, "u_sky", m.sky)
        _set(p, "u_scroll", tuple(m.scroll))
        _set(p, "u_emissive", m.emissive)
        _set(p, "u_vertex_colors", m.vertex_colors)
        _set(p, "u_point_size", float(m.point_size))
        for k, v in m.uniforms.items():
            _set(p, k, v)
        cull = "none" if m.cull == "none" or mesh.mode != "triangles" else m.cull
        if cull != "none" and np.linalg.det(world[:3, :3]) < 0:     # a mirrored transform flips winding
            cull = "front" if cull == "back" else "back"
        # only touch GL state that changed since the last node
        state = (cull, bool(m.depth_test), m.depth_write if m.depth_write is not None else not m.blended,
                 bool(m.wireframe), m.line_width if mesh.mode == "lines" else None, m.depth_offset)
        if state != self._state:
            if cull == "none":
                ctx.disable(mgl.CULL_FACE)
            else:
                ctx.enable(mgl.CULL_FACE)
                # our left-handed view flips screen winding: "back" culls GL's front faces
                ctx.cull_face = "front" if cull == "back" else "back"
            (ctx.enable if m.depth_test else ctx.disable)(mgl.DEPTH_TEST)
            ctx.depth_mask = state[2]
            ctx.wireframe = state[3]
            if state[4] is not None:
                ctx.line_width = state[4]
            ctx.polygon_offset = (state[5], state[5])
            self._state = state
        mode = {"triangles": mgl.TRIANGLES, "lines": mgl.LINES, "points": mgl.POINTS}[mesh.mode]
        self._vao(mesh, p, None).render(mode)
        return mesh.triangles

    def render(self, cam, width=None, height=None):
        """Render from camera; returns a PIL RGB image (width x height, default the renderer size)."""
        mgl = self.mgl
        if width and height:
            self.resize(width, height)
        ctx = self.ctx
        self._cam = cam
        self.fbo.use()
        ctx.viewport = (0, 0, self.width, self.height)
        ctx.clear(0, 0, 0, 1, depth=1.0)
        ctx.enable(mgl.PROGRAM_POINT_SIZE)
        proj, view = self.projection(cam), view_matrix(cam)
        self.last_view, self.last_proj = view, proj
        t = time.monotonic() - self.t0
        # sky / background
        ctx.disable(mgl.DEPTH_TEST)
        ctx.disable(mgl.BLEND)
        _set(self.bg, "u_inv", np.linalg.inv(proj @ view))
        _set(self.bg, "u_ortho", bool(getattr(cam, "ortho", None)))
        _set(self.bg, "u_fwd", tuple(float(v) for v in cam.forward()))
        self._sky_uniforms(self.bg, cam)
        self._bg_vao.render(mgl.TRIANGLES)
        ctx.enable(mgl.DEPTH_TEST)
        p = self.prog
        self._common(p, cam, view, proj, t)
        lp = np.zeros((8, 4), np.float32)
        lc = np.zeros((8, 3), np.float32)
        for i, (pos, col, rad) in enumerate(self.lights[:8]):
            lp[i, :3], lp[i, 3], lc[i] = pos, rad, col
        _set(p, "u_nlights", len(self.lights[:8]))
        _set(p, "u_light_pos", lp.tobytes())
        _set(p, "u_light_color", lc.tobytes())
        _set(p, "u_fog", self._fog is not None)
        if self._fog is not None:
            _set(p, "u_fog_color", tuple(self._fog[0]))
            _set(p, "u_fog_range", (self._fog[1], self._fog[2]))
        self._sky_uniforms(p, cam)
        opaque, blended, custom = [], [], []
        for node in self.root.walk(visible_only=True):
            if node.on_draw is not None:
                custom.append(node)
            if node.mesh is None or node.mesh.count == 0:
                continue
            (blended if node.material is not None and (node.material.blended or not node.material.depth_test)
             else opaque).append(node)
        tris = 0
        self._state = None
        for node in opaque:
            tris += self._draw_node(node, view, proj, t)
        if blended:
            ctx.enable(mgl.BLEND)
            ctx.blend_func = mgl.SRC_ALPHA, mgl.ONE_MINUS_SRC_ALPHA
            cp = np.asarray(cam.pos, float)
            blended.sort(key=lambda n: (n.order, -np.linalg.norm(n.world()[:3, 3] - cp)))
            for node in blended:
                tris += self._draw_node(node, view, proj, t)
            ctx.disable(mgl.BLEND)
        ctx.wireframe = False
        ctx.depth_mask = True
        ctx.polygon_offset = (0.0, 0.0)
        for node in custom:
            node.on_draw(self, node, view, proj)
        ctx.disable(mgl.CULL_FACE)
        self.stats = {"triangles": tris, "nodes": len(opaque) + len(blended), "custom": len(custom)}
        data = self.fbo.read(components=3)
        img = Image.frombytes("RGB", (self.width, self.height), data).transpose(Image.FLIP_TOP_BOTTOM)
        if self.auto_exposure:
            self._adapt(img)
        return img

    def pick(self, cam, x, y):
        """What is under pixel (x, y) (in a width x height render): a Pick(node, triangle, position,
        distance) for the nearest pickable triangle-mesh node, or None. Uses a GPU id buffer."""
        mgl, ctx = self.mgl, self.ctx
        if self._pick_fbo is None:
            self._pick_tex = ctx.texture((self.width, self.height), 2, dtype="u4")
            self._pick_tex.filter = (mgl.NEAREST, mgl.NEAREST)
            self._pick_depth = ctx.depth_texture((self.width, self.height))
            self._pick_fbo = ctx.framebuffer([self._pick_tex], self._pick_depth)
        xi, yi = int(x), int(y)
        if not (0 <= xi < self.width and 0 <= yi < self.height):
            return None
        fbo = self._pick_fbo
        fbo.use()
        ctx.viewport = (0, 0, self.width, self.height)
        fbo.clear(0, 0, 0, 0, depth=1.0)
        ctx.enable(mgl.DEPTH_TEST)
        ctx.disable(mgl.BLEND)
        ctx.disable(mgl.CULL_FACE)
        proj, view = self.projection(cam), view_matrix(cam)
        p = self.pick_prog
        _set(p, "u_view", view)
        _set(p, "u_proj", proj)
        ids = {}
        for node in self.root.walk(visible_only=True):
            if node.mesh is None or node.mesh.mode != "triangles" or not node.pickable or node.mesh.count == 0:
                continue
            ids[node.id] = node
            _set(p, "u_model", node.world())
            p["u_id"].value = node.id
            self._vao(node.mesh, p, None).render(mgl.TRIANGLES)
        gy = self.height - 1 - yi
        raw = fbo.read(viewport=(xi, gy, 1, 1), components=2, dtype="u4")
        nid, prim = np.frombuffer(raw, np.uint32)[:2]
        if nid == 0 or nid not in ids:
            return None
        depth = np.frombuffer(self._pick_depth.read(), np.float32).reshape(self.height, self.width)[gy, xi]
        inv = np.linalg.inv(proj @ view)
        nx, ny = (xi + 0.5) / self.width * 2 - 1, 1 - (yi + 0.5) / self.height * 2
        w = inv @ [nx, ny, depth * 2 - 1, 1]
        pos = w[:3] / w[3]
        return Pick(ids[int(nid)], int(prim) - 1, pos, float(np.linalg.norm(pos - np.asarray(cam.pos))))

    def _adapt(self, img):
        a = np.asarray(img.resize((32, 20)), np.float32) / 255
        mean = float((a @ np.array([0.299, 0.587, 0.114], np.float32)).mean())
        target = float(self.auto_exposure)
        if mean > 1e-3:
            want = self.exposure * (target / mean) ** self.gamma
            self.exposure = float(np.clip(self.exposure + (want - self.exposure) * 0.15, 0.25, 8.0))

    def _sky_uniforms(self, p, cam):
        _set(p, "u_exposure", self.exposure)
        _set(p, "u_gamma", self.gamma)
        _set(p, "u_cam", tuple(float(v) for v in cam.pos))
        _set(p, "u_sky_mode", self._sky_mode)
        _set(p, "u_sky_top", tuple(self._sky[0]))
        _set(p, "u_sky_bottom", tuple(self._sky[1]))
        if self._skybox is not None:
            self._skybox.use(2)

    def release(self):
        for tex, _ in self._textures.values():
            tex.release()
        self._textures.clear()
        for n in self.root.walk():
            if n.mesh is not None:
                for entry in n.mesh._gpu.values():
                    for obj in entry[2]:
                        obj.release()
                n.mesh._gpu.clear()
        self.fbo.release()
