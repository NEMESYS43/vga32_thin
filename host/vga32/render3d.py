"""Small 3D engines for GameApp: a polygon renderer and a grid raycaster, in numpy + PIL.

Both draw full-color into a Canvas; call g.dither() afterwards to spread the shading over the
16-color palette (ordered dither, stable from frame to frame), or skip it for flat, cheaper frames.

Polygon scenes (meshes, lighting, fog, sky) - good up to a few hundred faces at 160x100-320x200:

    from vga32.render3d import Scene, Mesh, Camera

    def start(self):
        self.scene = Scene(sky=("navy", "sky"), ground="green", fog=("sky", 8, 30))
        self.cube = self.scene.add(Mesh.cube(1, "red"), pos=(0, 0.5, 4))
        self.scene.add(Mesh.grid(20, 20, 10, "green", "lime"))
        self.cam = Camera(pos=(0, 1.2, 0))

    def update(self, dt):
        self.cube.rot[1] += dt                       # spin (radians)
        self.cam.look(*self.mouse_delta())           # mouse-look (set mouse_look = True)
        self.cam.walk(self.axis("w", "s") * 3 * dt, self.axis("d", "a") * 3 * dt)

    def render(self, g):
        self.scene.render(g, self.cam)
        g.dither()

Axes: x right, y up, z forward; yaw turns left/right around y, pitch looks up/down (radians).
Meshes: Mesh.cube, box, plane, grid (checkerboard), pyramid, sphere, cylinder, Mesh.load_obj(path),
or Mesh(vertices, faces, colors). Colors: palette names, "#rrggbb" (kept exact until dithering)
or (r, g, b).

Grid raycaster (Wolfenstein/Doom-style corridors; fast at any size):

    from vga32.render3d import Raycaster
    self.world = Raycaster(["#########",
                            "#.......#",
                            "#..R....#",
                            "#########"], colors={"#": "gray", "R": "red"})
    self.cam = Camera(pos=(1.5, 0, 1.5))
    ...
    self.world.move(self.cam, forward, strafe)       # walks with wall collision
    self.world.render(g, self.cam, sprites=[(4.5, 2.5, "yellow", 0.5)])
"""
import math

import numpy as np
from PIL import ImageDraw

from .palette import NAMES, PALETTE


def to_rgb(c, default=(255, 255, 255)):
    """Palette name / index / '#rrggbb' / (r, g, b) -> exact RGB (not snapped to the palette)."""
    if c is None:
        return default
    if isinstance(c, (tuple, list)) and len(c) == 3:
        return tuple(int(v) for v in c)
    if isinstance(c, int) and 0 <= c < 16:
        return PALETTE[c]
    s = str(c).strip().lower()
    if s in NAMES:
        return PALETTE[NAMES.index(s)]
    if s.startswith("#") and len(s) in (4, 7):
        if len(s) == 4:
            s = "#" + "".join(ch * 2 for ch in s[1:])
        return tuple(int(s[i:i + 2], 16) for i in (1, 3, 5))
    raise ValueError(f"bad color {c!r}")


def rotation(rx, ry, rz):
    """Rotation matrix for pitch (x), yaw (y), roll (z) in radians, applied z, then x, then y."""
    cx, sx, cy, sy, cz, sz = math.cos(rx), math.sin(rx), math.cos(ry), math.sin(ry), math.cos(rz), math.sin(rz)
    Rx = np.array([[1, 0, 0], [0, cx, -sx], [0, sx, cx]])
    Ry = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]])
    Rz = np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]])
    return Ry @ Rx @ Rz


class Camera:
    """pos, yaw (radians, 0 looks along +z), pitch (radians, + looks up), fov (horizontal degrees).
    ortho: None for perspective, or the visible height in world units for an orthographic view."""

    def __init__(self, pos=(0.0, 1.0, 0.0), yaw=0.0, pitch=0.0, fov=70.0, ortho=None):
        self.pos = np.array(pos, dtype=np.float64)
        self.yaw, self.pitch, self.fov = float(yaw), float(pitch), float(fov)
        self.ortho = ortho

    def look_at(self, target):
        """Turn to face a world point."""
        d = np.asarray(target, dtype=np.float64) - self.pos
        n = np.linalg.norm(d)
        if n > 1e-9:
            self.yaw = math.atan2(d[0], d[2])
            self.pitch = math.asin(max(-1.0, min(1.0, d[1] / n)))
        return self

    def forward(self, flat=False):
        cp = 1.0 if flat else math.cos(self.pitch)
        return np.array([math.sin(self.yaw) * cp, 0.0 if flat else math.sin(self.pitch), math.cos(self.yaw) * cp])

    def right(self):
        return np.array([math.cos(self.yaw), 0.0, -math.sin(self.yaw)])

    def look(self, dx, dy, sensitivity=0.005):
        """Mouse-look: dx/dy in pixels (e.g. from GameApp.mouse_delta())."""
        self.yaw += dx * sensitivity
        self.pitch = max(-1.4, min(1.4, self.pitch - dy * sensitivity))

    def walk(self, forward=0.0, strafe=0.0, up=0.0):
        """Move on the ground plane (FPS style)."""
        self.pos += self.forward(flat=True) * forward + self.right() * strafe + np.array([0, up, 0])

    def fly(self, forward=0.0, strafe=0.0, up=0.0):
        """Move where the camera points (free-fly)."""
        self.pos += self.forward() * forward + self.right() * strafe + np.array([0, up, 0])

    def basis(self):
        f = self.forward()
        r = self.right()
        u = np.cross(f, r)
        return r, u, f


class OrbitCamera(Camera):
    """Editor-style camera circling a target: orbit (rotate), pan and zoom. Call update() after
    changing target / distance / yaw / pitch directly (the methods below do it for you)."""

    def __init__(self, target=(0, 0, 0), distance=6.0, yaw=0.6, pitch=-0.45, fov=60.0, ortho=None):
        super().__init__(fov=fov, ortho=ortho)
        self.target = np.array(target, dtype=np.float64)
        self.distance = float(distance)
        self.yaw, self.pitch = float(yaw), float(pitch)
        self.update()

    def update(self):
        self.pitch = max(-1.55, min(1.55, self.pitch))
        self.pos = self.target - self.forward() * self.distance
        if self.ortho is not None:
            self.ortho = max(0.01, self.distance * math.tan(math.radians(self.fov) / 2) * 1.2)
        return self

    def orbit(self, dx, dy, sensitivity=0.01):
        self.yaw += dx * sensitivity
        self.pitch -= dy * sensitivity
        return self.update()

    def pan(self, dx, dy, sensitivity=None):
        s = sensitivity if sensitivity is not None else self.distance * 0.0025
        r, u, _ = self.basis()
        self.target += (-r * dx + u * dy) * s
        return self.update()

    def zoom(self, factor):
        """factor < 1 moves closer."""
        self.distance = max(0.05, min(1e5, self.distance * factor))
        return self.update()

    def frame(self, lo, hi):
        """Aim at and fit a bounding box."""
        lo, hi = np.asarray(lo, float), np.asarray(hi, float)
        self.target = (lo + hi) / 2
        self.distance = max(0.1, float(np.linalg.norm(hi - lo)) * 1.1 / (2 * math.tan(math.radians(self.fov) / 2)))
        return self.update()


class Mesh:
    def __init__(self, vertices, faces, colors="white", double_sided=False):
        self.vertices = np.asarray(vertices, dtype=np.float64).reshape(-1, 3)
        self.faces = [tuple(int(i) for i in f) for f in faces]
        cols = colors if isinstance(colors, list) else [colors] * len(self.faces)
        self.colors = [to_rgb(c) for c in cols]
        self.double_sided = double_sided

    @staticmethod
    def box(w=1.0, h=1.0, d=1.0, color="white"):
        x, y, z = w / 2, h / 2, d / 2
        v = [(-x, -y, -z), (x, -y, -z), (x, y, -z), (-x, y, -z), (-x, -y, z), (x, -y, z), (x, y, z), (-x, y, z)]
        f = [(0, 3, 2, 1), (4, 5, 6, 7), (0, 4, 7, 3), (1, 2, 6, 5), (3, 7, 6, 2), (0, 1, 5, 4)]
        cols = color if isinstance(color, list) else [color] * 6
        return Mesh(v, f, cols)

    @staticmethod
    def cube(size=1.0, color="white"):
        return Mesh.box(size, size, size, color)

    @staticmethod
    def plane(w=10.0, d=10.0, color="green", double_sided=True):
        x, z = w / 2, d / 2
        return Mesh([(-x, 0, -z), (x, 0, -z), (x, 0, z), (-x, 0, z)], [(0, 3, 2, 1)], color, double_sided)

    @staticmethod
    def grid(w=20.0, d=20.0, n=10, color="green", color2="lime"):
        """A checkerboard floor of n x n tiles (more tiles also means better depth sorting)."""
        v, f, c = [], [], []
        for i in range(n):
            for j in range(n):
                x0, z0 = -w / 2 + w * i / n, -d / 2 + d * j / n
                x1, z1 = x0 + w / n, z0 + d / n
                k = len(v)
                v += [(x0, 0, z0), (x1, 0, z0), (x1, 0, z1), (x0, 0, z1)]
                f.append((k, k + 3, k + 2, k + 1))
                c.append(color if (i + j) % 2 == 0 else color2)
        return Mesh(v, f, c, double_sided=True)

    @staticmethod
    def pyramid(size=1.0, color="yellow"):
        s = size / 2
        v = [(-s, 0, -s), (s, 0, -s), (s, 0, s), (-s, 0, s), (0, size, 0)]
        f = [(0, 1, 2, 3)[::-1], (0, 4, 1), (1, 4, 2), (2, 4, 3), (3, 4, 0)]
        return Mesh(v, f, color)

    @staticmethod
    def sphere(r=0.5, color="white", segments=12, rings=7):
        v = [(0, r, 0)]
        for i in range(1, rings):
            phi = math.pi * i / rings
            for j in range(segments):
                th = 2 * math.pi * j / segments
                v.append((r * math.sin(phi) * math.cos(th), r * math.cos(phi), r * math.sin(phi) * math.sin(th)))
        v.append((0, -r, 0))
        bottom = len(v) - 1
        f = []
        ring = lambda i, j: 1 + (i - 1) * segments + j % segments
        for j in range(segments):
            f.append((0, ring(1, j + 1), ring(1, j)))
            f.append((bottom, ring(rings - 1, j), ring(rings - 1, j + 1)))
        for i in range(1, rings - 1):
            for j in range(segments):
                f.append((ring(i, j), ring(i, j + 1), ring(i + 1, j + 1), ring(i + 1, j)))
        return Mesh(v, f, color)

    @staticmethod
    def cylinder(r=0.5, h=1.0, color="white", segments=12):
        v = []
        for j in range(segments):
            th = 2 * math.pi * j / segments
            v += [(r * math.cos(th), 0, r * math.sin(th)), (r * math.cos(th), h, r * math.sin(th))]
        f = [(2 * j, 2 * j + 1, (2 * j + 3) % (2 * segments), (2 * j + 2) % (2 * segments)) for j in range(segments)]
        f.append(tuple(2 * j + 1 for j in range(segments)))
        f.append(tuple(2 * j for j in reversed(range(segments))))
        return Mesh(v, f, color)

    @staticmethod
    def load_obj(path_or_text, color="white", scale=1.0):
        """Wavefront .obj (v and f lines; materials ignored)."""
        text = path_or_text
        if "\n" not in path_or_text:
            with open(path_or_text, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        v, f = [], []
        for line in text.splitlines():
            p = line.split()
            if not p:
                continue
            if p[0] == "v":
                v.append(tuple(float(x) * scale for x in p[1:4]))
            elif p[0] == "f":
                idx = [int(t.split("/")[0]) for t in p[1:]]
                f.append(tuple(i - 1 if i > 0 else len(v) + i for i in idx))
        return Mesh(v, f, color)


class Object3D:
    def __init__(self, mesh, pos=(0, 0, 0), rot=(0, 0, 0), scale=1.0, visible=True):
        self.mesh = mesh
        self.pos = np.array(pos, dtype=np.float64)
        self.rot = np.array(rot, dtype=np.float64)        # pitch, yaw, roll
        self.scale = np.array([scale] * 3 if np.isscalar(scale) else scale, dtype=np.float64)
        self.visible = visible

    def world_vertices(self):
        R = rotation(*self.rot)
        return (self.mesh.vertices * self.scale) @ R.T + self.pos


class Scene:
    def __init__(self, sky=("navy", "sky"), ground=None, fog=None, light=(-0.4, -1.0, 0.6), ambient=0.35, near=0.05):
        self.objects = []
        self.sky = (to_rgb(sky[0]), to_rgb(sky[1])) if isinstance(sky, (tuple, list)) and len(sky) == 2 and \
            not isinstance(sky[0], (int, float)) else (to_rgb(sky), to_rgb(sky))
        self.ground = to_rgb(ground) if ground is not None else None
        self.fog = (to_rgb(fog[0]), float(fog[1]), float(fog[2])) if fog else None
        L = np.array(light, dtype=np.float64)
        self.light = L / (np.linalg.norm(L) or 1)
        self.ambient = float(ambient)
        self.near = float(near)
        self.stats = {}

    def add(self, mesh, pos=(0, 0, 0), rot=(0, 0, 0), scale=1.0):
        obj = Object3D(mesh, pos, rot, scale)
        self.objects.append(obj)
        return obj

    def remove(self, obj):
        self.objects.remove(obj)

    def _background(self, g, cam, fpx):
        W, H = g.w, g.h
        horizon = H / 2 + math.tan(cam.pitch) * fpx
        rows = np.arange(H, dtype=np.float64)
        top, bot = np.array(self.sky[0], float), np.array(self.sky[1], float)
        t = np.clip(rows / max(1.0, horizon), 0, 1)[:, None]
        col = top * (1 - t) + bot * t
        if self.ground is not None:
            below = rows >= horizon
            far = self.fog[0] if self.fog else self.ground
            k = np.clip((rows - horizon) / max(1.0, H - horizon), 0, 1)[:, None]
            gcol = np.array(far, float) * (1 - k) + np.array(self.ground, float) * k
            col = np.where(below[:, None], gcol, col)
        arr = np.repeat(col[:, None, :], W, axis=1).astype(np.uint8)
        from PIL import Image
        g.img.paste(Image.fromarray(arr))

    def render(self, g, cam):
        """Draw the scene into Canvas g from camera cam (full color; follow with g.dither())."""
        W, H = g.w, g.h
        fpx = (W / 2) / math.tan(math.radians(cam.fov) / 2)
        self._background(g, cam, fpx)
        r, u, f = cam.basis()
        polys = []
        drawn = culled = 0
        for obj in self.objects:
            if not obj.visible:
                continue
            wv = obj.world_vertices()
            rel = wv - cam.pos
            cs = np.stack([rel @ r, rel @ u, rel @ f], axis=1)         # camera space
            mesh = obj.mesh
            for face, base in zip(mesh.faces, mesh.colors):
                pc = cs[list(face)]
                if (pc[:, 2] <= self.near).all():
                    culled += 1
                    continue
                pw = wv[list(face)]
                n = np.cross(pw[1] - pw[0], pw[2] - pw[0])
                nl = np.linalg.norm(n)
                if nl == 0:
                    continue
                n /= nl
                to_cam = cam.pos - pw.mean(axis=0)
                facing = float(n @ to_cam)
                if facing < 0:
                    if not mesh.double_sided:
                        culled += 1
                        continue
                    n = -n
                if (pc[:, 2] <= self.near).any():
                    pc = self._clip(pc)
                    if len(pc) < 3:
                        continue
                sx = W / 2 + pc[:, 0] / pc[:, 2] * fpx
                sy = H / 2 - pc[:, 1] / pc[:, 2] * fpx
                if sx.max() < 0 or sx.min() >= W or sy.max() < 0 or sy.min() >= H:
                    culled += 1
                    continue
                light = self.ambient + (1 - self.ambient) * max(0.0, float(-n @ self.light))
                col = np.array(base, float) * light
                depth = float(pc[:, 2].mean())
                if self.fog:
                    fc, f0, f1 = self.fog
                    k = min(1.0, max(0.0, (depth - f0) / max(1e-6, f1 - f0)))
                    col = col * (1 - k) + np.array(fc, float) * k
                polys.append((depth, list(zip(sx.tolist(), sy.tolist())), tuple(int(c) for c in np.clip(col, 0, 255))))
        polys.sort(key=lambda p: -p[0])
        d = ImageDraw.Draw(g.img)
        for _, pts, col in polys:
            d.polygon(pts, fill=col)
            drawn += 1
        self.stats = {"faces_drawn": drawn, "culled": culled}

    def _clip(self, pc):
        """Clip a camera-space polygon against the near plane (Sutherland-Hodgman)."""
        out = []
        n = len(pc)
        for i in range(n):
            a, b = pc[i], pc[(i + 1) % n]
            ain, bin_ = a[2] > self.near, b[2] > self.near
            if ain:
                out.append(a)
            if ain != bin_:
                t = (self.near - a[2]) / (b[2] - a[2])
                out.append(a + (b - a) * t)
        return np.array(out)


class Raycaster:
    """Grid-world raycaster. map: list of strings; '.', ' ' and '0' are empty, any other character
    is a wall drawn in colors[char] (default gray). Camera.pos[0] is the column, pos[2] the row."""

    def __init__(self, grid, colors=None, ceiling=("navy", "teal"), floor=("darkgray", "gray"), fog=None,
                 max_dist=24.0):
        self.grid = [str(r) for r in grid]
        self.h = len(self.grid)
        self.w = max(len(r) for r in self.grid)
        chars = sorted({ch for r in self.grid for ch in r if ch not in ". 0"})
        self.chars = chars
        self.cells = np.zeros((self.h, self.w), np.int16)
        for j, row in enumerate(self.grid):
            for i, ch in enumerate(row):
                if ch not in ". 0":
                    self.cells[j, i] = chars.index(ch) + 1
        colors = colors or {}
        self.palette = np.array([(0, 0, 0)] + [to_rgb(colors.get(ch, "gray")) for ch in chars], np.float64)
        self.ceiling = (np.array(to_rgb(ceiling[0]), float), np.array(to_rgb(ceiling[1]), float))
        self.floor = (np.array(to_rgb(floor[0]), float), np.array(to_rgb(floor[1]), float))
        self.fog = (np.array(to_rgb(fog[0]), float), float(fog[1]), float(fog[2])) if fog else None
        self.max_dist = max_dist
        self.zbuffer = None

    def cell(self, x, z):
        i, j = int(math.floor(x)), int(math.floor(z))
        if 0 <= j < self.h and 0 <= i < self.w:
            return self.grid[j][i] if i < len(self.grid[j]) else "."
        return "#"

    def is_wall(self, x, z):
        return self.cell(x, z) not in ". 0"

    def move(self, cam, forward=0.0, strafe=0.0, radius=0.2):
        """Walk with wall collision (slides along walls)."""
        dx = math.sin(cam.yaw) * forward + math.cos(cam.yaw) * strafe
        dz = math.cos(cam.yaw) * forward - math.sin(cam.yaw) * strafe
        x, z = cam.pos[0], cam.pos[2]
        nx = x + dx
        if not self.is_wall(nx + math.copysign(radius, dx), z):
            x = nx
        nz = z + dz
        if not self.is_wall(x, nz + math.copysign(radius, dz)):
            z = nz
        cam.pos[0], cam.pos[2] = x, z

    def render(self, g, cam, sprites=()):
        """sprites: (x, z, color, size[, height]) billboards, drawn with depth against the walls."""
        W, H = g.w, g.h
        fov = math.radians(cam.fov)
        plane = math.tan(fov / 2)
        dir_x, dir_z = math.sin(cam.yaw), math.cos(cam.yaw)
        pl_x, pl_z = math.cos(cam.yaw) * plane, -math.sin(cam.yaw) * plane
        camx = 2 * (np.arange(W) + 0.5) / W - 1
        rdx = dir_x + pl_x * camx
        rdz = dir_z + pl_z * camx
        px, pz = cam.pos[0], cam.pos[2]
        mx = np.full(W, int(math.floor(px)))
        mz = np.full(W, int(math.floor(pz)))
        with np.errstate(divide="ignore"):
            ddx = np.where(rdx == 0, 1e30, np.abs(1 / rdx))
            ddz = np.where(rdz == 0, 1e30, np.abs(1 / rdz))
        sx = np.where(rdx < 0, -1, 1)
        sz = np.where(rdz < 0, -1, 1)
        side_x = np.where(rdx < 0, (px - mx) * ddx, (mx + 1 - px) * ddx)
        side_z = np.where(rdz < 0, (pz - mz) * ddz, (mz + 1 - pz) * ddz)
        hit = np.zeros(W, np.int16)
        side = np.zeros(W, np.int8)
        active = np.ones(W, bool)
        for _ in range(int(self.max_dist * 2) + 4):
            stepx = active & (side_x < side_z)
            stepz = active & ~stepx
            side_x = np.where(stepx, side_x + ddx, side_x)
            mx = np.where(stepx, mx + sx, mx)
            side_z = np.where(stepz, side_z + ddz, side_z)
            mz = np.where(stepz, mz + sz, mz)
            side = np.where(stepx, 0, np.where(stepz, 1, side))
            inside = (mx >= 0) & (mx < self.w) & (mz >= 0) & (mz < self.h)
            cellv = np.where(inside, self.cells[np.clip(mz, 0, self.h - 1), np.clip(mx, 0, self.w - 1)], 1)
            newhit = active & (cellv > 0)
            hit = np.where(newhit, cellv, hit)
            active &= ~newhit
            if not active.any():
                break
        dist = np.where(side == 0, side_x - ddx, side_z - ddz)
        dist = np.maximum(dist, 1e-3)
        self.zbuffer = dist
        horizon = H / 2 + math.tan(cam.pitch) * (W / 2) / plane
        line_h = (W / 2 / plane) / dist
        top = horizon - line_h / 2
        bot = horizon + line_h / 2
        # wall color: tile color, darker on z-facing sides, dark seams at tile edges, fog by distance
        wall_x = np.where(side == 0, pz + dist * rdz, px + dist * rdx)
        wall_x -= np.floor(wall_x)
        col = self.palette[hit] * np.where(side == 1, 0.72, 1.0)[:, None]
        col *= np.where((wall_x < 0.04) | (wall_x > 0.96), 0.55, 1.0)[:, None]
        if self.fog is not None:
            fc, f0, f1 = self.fog
            k = np.clip((dist - f0) / max(1e-6, f1 - f0), 0, 1)[:, None]
            col = col * (1 - k) + fc * k
        rows = np.arange(H, dtype=np.float64)[:, None]
        t_ceil = np.clip(rows / max(1.0, horizon), 0, 1)
        t_floor = np.clip((rows - horizon) / max(1.0, H - horizon), 0, 1)
        ceil = self.ceiling[0] * (1 - t_ceil) + self.ceiling[1] * t_ceil
        flo = self.floor[0] * (1 - t_floor) + self.floor[1] * t_floor
        bg = np.where(rows < horizon, ceil, flo)                    # (H, 3)
        img = np.repeat(bg[:, None, :], W, axis=1)
        wall = (rows >= top[None, :]) & (rows < bot[None, :]) & (hit > 0)[None, :]
        img = np.where(wall[:, :, None], col[None, :, :], img)
        # billboards, far to near
        inv = 1.0 / (pl_x * dir_z - dir_x * pl_z)
        for s in sorted(sprites, key=lambda s: -((s[0] - px) ** 2 + (s[1] - pz) ** 2)):
            sxw, szw, scol, size = s[0], s[1], to_rgb(s[2]), s[3] if len(s) > 3 else 0.5
            height = s[4] if len(s) > 4 else 0.0
            rx, rz = sxw - px, szw - pz
            tx = inv * (dir_z * rx - dir_x * rz)
            tz = inv * (-pl_z * rx + pl_x * rz)
            if tz <= 0.1:
                continue
            scx = (W / 2) * (1 + tx / tz)
            ph = (W / 2 / plane) / tz
            sw = ph * size
            x0, x1 = int(max(0, scx - sw / 2)), int(min(W, scx + sw / 2))
            cy = horizon + ph / 2 - ph * height - sw / 2       # resting on the floor, raised by height
            y0, y1 = int(max(0, cy - sw / 2)), int(min(H, cy + sw / 2))
            if x0 >= x1 or y0 >= y1:
                continue
            vis = dist[x0:x1] > tz
            if not vis.any():
                continue
            c = np.array(scol, float)
            if self.fog is not None:
                fc, f0, f1 = self.fog
                k = min(1.0, max(0.0, (tz - f0) / max(1e-6, f1 - f0)))
                c = c * (1 - k) + fc * k
            yy, xx = np.mgrid[y0:y1, x0:x1]
            round_ = ((xx - scx) ** 2 + (yy - cy) ** 2) <= (sw / 2) ** 2
            mask = round_ & vis[None, :]
            region = img[y0:y1, x0:x1]
            region[mask] = c
        from PIL import Image
        g.img.paste(Image.fromarray(np.clip(img, 0, 255).astype(np.uint8)))
