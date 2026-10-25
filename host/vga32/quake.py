"""Quake 1 maps: load a .bsp (version 29, BSP2 and 2PSB too) for the GPU renderer, with textures,
lightmaps (colored from a .lit file next to the map), the sky, entities and player collision.

    from vga32 import quake
    level = quake.load("maps/e1m1.bsp")                  # also finds the .lit and the palette
    level.add_to(renderer)                               # gl3d.Renderer
    cam = level.spawn_camera()                           # at info_player_start
    player = level.player(cam)                           # Quake-style walking: player.update(dt, fwd, side, jump)

Textures in a .bsp are 8-bit and need Quake's palette (gfx/palette.lmp). It is looked up next to
the map (../gfx/palette.lmp), in id1/ and in pak0.pak/pak1.pak of a Quake install; pass
palette="path/to/palette.lmp" or a .pak to choose. Without one, textures are shown in grey
(lighting and colored .lit light still apply). Skyboxes come from gfx/env/<sky>_rt.tga etc.

Quake units are kept (a player is 56 units tall). World axes: Quake's x east, y north, z up become
our x right, z forward, y up, i.e. (qx, qy, qz) -> (qx, qz, qy).
"""
import math
import os
import re
import struct

import numpy as np
from PIL import Image

LUMPS = ("entities", "planes", "textures", "vertexes", "visibility", "nodes", "texinfo", "faces", "lighting",
         "clipnodes", "leafs", "marksurfaces", "edges", "surfedges", "models")
CONTENTS_EMPTY, CONTENTS_SOLID, CONTENTS_WATER, CONTENTS_SLIME, CONTENTS_LAVA, CONTENTS_SKY = -1, -2, -3, -4, -5, -6
SKIP_TEXTURES = ("trigger", "clip", "skip", "hint", "nodraw")


def q2gl(v):
    """Quake (x, y, z) -> our (x, y, z) for numpy arrays (..., 3)."""
    v = np.asarray(v, dtype=np.float64)
    return v[..., [0, 2, 1]]


def gl2q(v):
    return np.asarray(v, dtype=np.float64)[..., [0, 2, 1]]


# ----- palette -----
def read_pak(path):
    """{name: bytes} of a Quake .pak archive."""
    out = {}
    with open(path, "rb") as f:
        magic, ofs, size = struct.unpack("<4sii", f.read(12))
        if magic != b"PACK":
            raise ValueError(f"{path} is not a .pak")
        f.seek(ofs)
        entries = [struct.unpack("<56sii", f.read(64)) for _ in range(size // 64)]
        for name, o, n in entries:
            f.seek(o)
            out[name.split(b"\0")[0].decode("latin-1")] = f.read(n)
    return out


def find_palette(bsp_path, hint=None):
    """768 bytes of RGB, or None."""
    cands = []
    if hint:
        cands.append(hint)
    d = os.path.dirname(os.path.abspath(bsp_path))
    for base in (d, os.path.dirname(d), os.path.dirname(os.path.dirname(d))):
        cands += [os.path.join(base, "gfx", "palette.lmp"), os.path.join(base, "palette.lmp"),
                  os.path.join(base, "id1", "gfx", "palette.lmp"), os.path.join(base, "id1", "pak0.pak"),
                  os.path.join(base, "pak0.pak"), os.path.join(base, "id1", "pak1.pak")]
    env = os.environ.get("QUAKE_DIR")
    if env:
        cands += [os.path.join(env, "id1", "gfx", "palette.lmp"), os.path.join(env, "id1", "pak0.pak")]
    for c in cands:
        try:
            if c.lower().endswith(".pak") and os.path.exists(c):
                data = read_pak(c).get("gfx/palette.lmp")
                if data and len(data) >= 768:
                    return data[:768]
            elif os.path.exists(c):
                with open(c, "rb") as f:
                    data = f.read()
                if len(data) >= 768:
                    return data[:768]
        except (OSError, ValueError):
            pass
    return None


def grey_palette():
    """Stand-in when Quake's palette isn't available: brightness ramps, so texture detail still shows.
    Quake's palette is 16 ramps of 16; the first 8 run dark to light, the next 6 light to dark."""
    pal = []
    for i in range(256):
        row, k = divmod(i, 16)
        t = k / 15 if row < 8 or row >= 14 else 1 - k / 15
        g = int(30 + 200 * t)
        pal += [g, g, g]
    return bytes(pal)


# ----- the map -----
class Level:
    def __init__(self, path, palette=None, lit=None):
        self.path = path
        with open(path, "rb") as f:
            self.data = f.read()
        ver = self.data[:4]
        if ver == b"BSP2":
            self.fmt = "bsp2"
        elif ver == b"2PSB":
            self.fmt = "2psb"
        else:
            v = struct.unpack("<i", ver)[0]
            if v != 29:
                raise ValueError(f"{os.path.basename(path)}: BSP version {v}; only Quake 1 maps (29, BSP2) "
                                 "are supported")
            self.fmt = "bsp29"
        self.lumps = {name: struct.unpack_from("<ii", self.data, 4 + i * 8) for i, name in enumerate(LUMPS)}
        pal = find_palette(path, palette)
        self.has_palette = pal is not None
        self.palette = np.frombuffer(pal or grey_palette(), np.uint8).reshape(256, 3)
        self.entities = self._entities()
        self.worldspawn = next((e for e in self.entities if e.get("classname") == "worldspawn"), {})
        self._read_geometry()
        self.lit = self._load_lit(lit)
        self.textures = self._textures()
        self._batches = None

    def lump(self, name):
        ofs, size = self.lumps[name]
        return self.data[ofs:ofs + size]

    # entities
    def _entities(self):
        text = self.lump("entities").split(b"\0")[0].decode("latin-1")
        out = []
        for block in re.findall(r"\{([^}]*)\}", text):
            out.append(dict(re.findall(r'"([^"]*)"\s*"([^"]*)"', block)))
        return out

    def find(self, classname):
        return [e for e in self.entities if e.get("classname", "").startswith(classname)]

    # geometry
    def _read_geometry(self):
        big = self.fmt != "bsp29"
        self.vertices = np.frombuffer(self.lump("vertexes"), np.float32).reshape(-1, 3).astype(np.float64)
        p = self.lump("planes")
        n = len(p) // 20
        self.planes = [struct.unpack_from("<4fi", p, i * 20) for i in range(n)]
        self.edges = np.frombuffer(self.lump("edges"), np.uint32 if big else np.uint16).reshape(-1, 2)
        self.surfedges = np.frombuffer(self.lump("surfedges"), np.int32)
        t = self.lump("texinfo")
        self.texinfo = [struct.unpack_from("<8f2i", t, i * 40) for i in range(len(t) // 40)]
        f = self.lump("faces")
        if big:   # planenum i, side i, firstedge i, numedges i, texinfo i, styles 4B, lightofs i
            self.faces = [struct.unpack_from("<5i4Bi", f, i * 28) for i in range(len(f) // 28)]
        else:     # planenum h, side h, firstedge i, numedges h, texinfo h, styles 4B, lightofs i
            self.faces = [struct.unpack_from("<hhihh4Bi", f, i * 20) for i in range(len(f) // 20)]
        m = self.lump("models")
        self.models = [struct.unpack_from("<9f4i3i", m, i * 64) for i in range(len(m) // 64)]
        c = self.lump("clipnodes")
        if big:
            self.clipnodes = [struct.unpack_from("<3i", c, i * 12) for i in range(len(c) // 12)]
        else:
            self.clipnodes = [struct.unpack_from("<ihh", c, i * 8) for i in range(len(c) // 8)]
        self.lighting = self.lump("lighting")

    def _load_lit(self, lit):
        path = lit or os.path.splitext(self.path)[0] + ".lit"
        try:
            with open(path, "rb") as f:
                d = f.read()
            if d[:4] == b"QLIT":
                return d[8:]
        except OSError:
            pass
        return None

    def _textures(self):
        d = self.lump("textures")
        out = []
        if len(d) < 4:
            return out
        n = struct.unpack_from("<i", d, 0)[0]
        offs = struct.unpack_from(f"<{n}i", d, 4)
        for o in offs:
            if o < 0:
                out.append({"name": "missing", "w": 64, "h": 64, "image": None})
                continue
            name = d[o:o + 16].split(b"\0")[0].decode("latin-1").lower()
            w, h = struct.unpack_from("<II", d, o + 16)
            mip0 = struct.unpack_from("<I", d, o + 24)[0]
            img = None
            if mip0 and w and h and o + mip0 + w * h <= len(d):
                idx = np.frombuffer(d, np.uint8, w * h, o + mip0).reshape(h, w)
                rgb = self.palette[idx]
                alpha = np.where((idx == 255) & name.startswith("{"), 0, 255).astype(np.uint8)
                img = Image.fromarray(np.dstack([rgb, alpha]), "RGBA")
            out.append({"name": name, "w": w or 64, "h": h or 64, "image": img})
        return out

    # ----- building GPU batches -----
    def skip_models(self):
        """Brush models that are triggers or invisible helpers."""
        skip = set()
        for e in self.entities:
            mdl = e.get("model", "")
            if mdl.startswith("*") and e.get("classname", "").startswith(("trigger_", "func_illusionary_hidden")):
                skip.add(int(mdl[1:]))
        return skip

    def batches(self):
        """{texture index: dict(positions, uv, uv2)} plus the lightmap atlas image. Cached."""
        if self._batches is not None:
            return self._batches
        skip = self.skip_models()
        face_model = {}
        for mi, m in enumerate(self.models):
            if mi in skip:
                continue
            first, num = m[14], m[15]          # mins(3) maxs(3) origin(3) headnodes(4) visleafs firstface numfaces
            for fi in range(first, first + num):
                face_model[fi] = mi
        polys = []          # (tex, quake verts (n,3), st (n,2), lightmap rect info)
        lm_blocks = []
        for fi, face in enumerate(self.faces):
            if fi not in face_model:
                continue
            firstedge, numedges, ti, styles, lightofs = face[2], face[3], face[4], face[5:9], face[9]
            tinfo = self.texinfo[ti]
            tex = tinfo[8]
            tname = self.textures[tex]["name"] if 0 <= tex < len(self.textures) else ""
            if tname.startswith(SKIP_TEXTURES):
                continue
            se = self.surfedges[firstedge:firstedge + numedges]
            idx = np.where(se >= 0, self.edges[np.abs(se), 0], self.edges[np.abs(se), 1])
            v = self.vertices[idx]
            s = v @ np.array(tinfo[0:3]) + tinfo[3]
            t = v @ np.array(tinfo[4:7]) + tinfo[7]
            lm = None
            if lightofs >= 0 and styles[0] != 255 and not tname.startswith(("sky", "*")):
                smin, smax = math.floor(s.min() / 16), math.ceil(s.max() / 16)
                tmin, tmax = math.floor(t.min() / 16), math.ceil(t.max() / 16)
                lw, lh = smax - smin + 1, tmax - tmin + 1
                if lw <= 64 and lh <= 64:
                    lm = (lightofs, lw, lh, smin, tmin)
                    lm_blocks.append((len(polys), lw, lh))
            polys.append((tex, v, s, t, lm, tname))
        atlas, places = self._pack_lightmaps(polys, lm_blocks)
        out = {}
        for pi, (tex, v, s, t, lm, tname) in enumerate(polys):
            tw, th = (self.textures[tex]["w"], self.textures[tex]["h"]) if 0 <= tex < len(self.textures) else (64, 64)
            n = len(v)
            tri = np.array([[0, i, i + 1] for i in range(1, n - 1)]).ravel()
            b = out.setdefault(tex, {"positions": [], "uv": [], "uv2": [], "name": tname})
            b["positions"].append(q2gl(v[tri]))
            b["uv"].append(np.stack([s[tri] / tw, -t[tri] / th], 1))      # textures are flipped for GL
            if lm is not None and pi in places:
                ax, ay = places[pi]
                _, lw, lh, smin, tmin = lm
                u2 = (ax + s[tri] / 16 - smin + 0.5) / atlas.width
                v2 = 1 - (ay + t[tri] / 16 - tmin + 0.5) / atlas.height
                b["uv2"].append(np.stack([u2, v2], 1))
            else:
                b["uv2"].append(np.tile([[0.5 / atlas.width, 1 - 0.5 / atlas.height]], (len(tri), 1)))   # white texel
        for b in out.values():
            for k in ("positions", "uv", "uv2"):
                b[k] = np.concatenate(b[k]).astype(np.float32)
        self._batches = (out, atlas)
        return self._batches

    def _pack_lightmaps(self, polys, blocks, width=1024):
        """Shelf-pack every face's lightmap into one RGB atlas. Texel (0, 0) is left white for unlit faces."""
        blocks = sorted(blocks, key=lambda b: -b[2])
        x, y, shelf = 2, 0, 1
        places = {}
        for pi, lw, lh in blocks:
            if x + lw + 1 > width:
                x, y, shelf = 0, y + shelf + 1, 0
            places[pi] = (x, y)
            x += lw + 1
            shelf = max(shelf, lh)
        height = 1 << max(4, math.ceil(math.log2(y + shelf + 2)))
        atlas = np.zeros((height, width, 3), np.uint8)
        atlas[0, 0] = 255
        light = self.lit if self.lit is not None else self.lighting
        mult = 3 if self.lit is not None else 1
        for pi, (ax, ay) in places.items():
            ofs, lw, lh, _, _ = polys[pi][4]
            n = lw * lh * mult
            raw = np.frombuffer(light, np.uint8, n, ofs * mult) if ofs * mult + n <= len(light) else None
            if raw is None:
                atlas[ay:ay + lh, ax:ax + lw] = 255
                continue
            block = raw.reshape(lh, lw, 3) if mult == 3 else np.repeat(raw.reshape(lh, lw, 1), 3, axis=2)
            atlas[ay:ay + lh, ax:ax + lw] = block
        return Image.fromarray(atlas), places

    def add_to(self, renderer, lightmap_scale=2.0):
        """Upload the map into a gl3d.Renderer; returns the nodes. Sets the sky from worldspawn."""
        from .gl3d import Material, MeshData
        batches, atlas = self.batches()
        nodes = []
        for tex, b in batches.items():
            t = self.textures[tex] if 0 <= tex < len(self.textures) else {"name": "", "image": None}
            name = t["name"]
            mesh = MeshData(b["positions"], uv=b["uv"], uv2=b["uv2"])
            if name.startswith("sky"):
                mat = Material(sky=True)
            elif name.startswith("*"):
                mat = Material(texture=t["image"], warp=True, unlit=True)
            else:
                mat = Material(texture=t["image"], lightmap=atlas, alpha_test=name.startswith("{"),
                               lightmap_scale=lightmap_scale)
            nodes.append(renderer.add(mesh, mat))
        self.apply_sky(renderer)
        return nodes

    def apply_sky(self, renderer):
        name = self.worldspawn.get("sky") or self.worldspawn.get("skyname")
        if name:
            base = os.path.dirname(os.path.dirname(os.path.abspath(self.path)))
            faces = []
            # Quake skybox suffixes for the cube faces +x, -x, +y (up), -y (down), +z, -z in our axes.
            # Found by fitting the seams of a real skybox; every face is mirrored (Quake is right-handed).
            for suffix in ("bk", "ft", "up", "dn", "rt", "lf"):
                p = next((os.path.join(base, "gfx", "env", f"{name}{suffix}.{ext}") for ext in ("tga", "png", "jpg")
                          if os.path.exists(os.path.join(base, "gfx", "env", f"{name}{suffix}.{ext}"))), None)
                p = p or next((os.path.join(base, "gfx", "env", f"{name}_{suffix}.{ext}") for ext in ("tga", "png", "jpg")
                               if os.path.exists(os.path.join(base, "gfx", "env", f"{name}_{suffix}.{ext}"))), None)
                faces.append(p)
            if all(faces):
                renderer.skybox([Image.open(p).convert("RGB").transpose(Image.FLIP_LEFT_RIGHT) for p in faces])
                return
        renderer.sky_gradient("#202040", "#806050")

    # ----- spawning and collision -----
    def spawn(self):
        """(position in our axes at eye height, yaw radians) of info_player_start."""
        e = next(iter(self.find("info_player_start") or self.find("info_player_deathmatch") or
                      self.find("info_player")), None)
        if e is None:
            mins, maxs = np.array(self.models[0][0:3]), np.array(self.models[0][3:6])
            return q2gl((mins + maxs) / 2), 0.0
        o = np.array([float(x) for x in e.get("origin", "0 0 0").split()])
        ang = float(e.get("angle", 0))
        return q2gl(o + [0, 0, 22]), math.radians(90 - ang)

    def spawn_camera(self, fov=90.0):
        from .render3d import Camera
        pos, yaw = self.spawn()
        return Camera(pos=pos, yaw=yaw, pitch=0.0, fov=fov)

    def contents(self, q, hull=1):
        """Contents at Quake point q in a collision hull (0 = point, 1 = player, 2 = large)."""
        if hull == 0:
            return self._point_contents0(q)
        node = self.models[0][9 + hull]
        cn, planes = self.clipnodes, self.planes
        while node >= 0:
            pn, front, back = cn[node]
            nx, ny, nz, dist, ptype = planes[pn]
            d = (q[ptype] if ptype < 3 else nx * q[0] + ny * q[1] + nz * q[2]) - dist
            node = front if d >= 0 else back
        return node

    def _point_contents0(self, q):
        if not hasattr(self, "_nodes"):
            n = self.lump("nodes")
            big = self.fmt != "bsp29"
            size = 44 if self.fmt == "bsp2" else (32 if self.fmt == "2psb" else 24)
            fmt = "<3i" if big else "<i2h"
            self._nodes = [struct.unpack_from(fmt, n, i * size) for i in range(len(n) // size)]
            lf = self.lump("leafs")
            lsize = 44 if self.fmt == "bsp2" else (32 if self.fmt == "2psb" else 28)
            self._leaf_contents = [struct.unpack_from("<i", lf, i * lsize)[0] for i in range(len(lf) // lsize)]
        node = self.models[0][9]
        while node >= 0:
            pn, front, back = self._nodes[node]
            nx, ny, nz, dist, ptype = self.planes[pn]
            d = (q[ptype] if ptype < 3 else nx * q[0] + ny * q[1] + nz * q[2]) - dist
            node = front if d >= 0 else back
        return self._leaf_contents[-node - 1]

    def player(self, cam):
        return Player(self, cam)

    def info(self):
        batches, atlas = self.batches()
        return {"file": os.path.basename(self.path), "format": self.fmt, "faces": len(self.faces),
                "triangles": sum(len(b["positions"]) // 3 for b in batches.values()), "textures": len(self.textures),
                "entities": len(self.entities), "palette": self.has_palette, "colored_light": self.lit is not None,
                "lightmap_atlas": list(atlas.size), "message": self.worldspawn.get("message", ""),
                "sky": self.worldspawn.get("sky", "")}


class Player:
    """Quake-style movement against the player hull: walking with gravity, stairs and sliding,
    or noclip flying. Positions are the camera's (eye) position in our axes."""
    EYE = 22.0
    STEP = 18.0

    def __init__(self, level, cam):
        self.level, self.cam = level, cam
        self.vel = np.zeros(3)            # Quake axes
        self.on_ground = False
        self.noclip = False
        self.speed, self.gravity, self.jump_speed = 320.0, 800.0, 270.0

    @property
    def origin(self):
        return gl2q(self.cam.pos) - [0, 0, self.EYE]

    def _solid(self, q):
        return self.level.contents(q, 1) == CONTENTS_SOLID

    def update(self, dt, forward=0.0, side=0.0, jump=False, up=0.0):
        """forward / side / up in -1..1 (keys or analog)."""
        yaw = self.cam.yaw
        fq = np.array([math.sin(yaw), math.cos(yaw), 0.0])      # our forward (x, z) -> Quake (x, y)
        rq = np.array([math.cos(yaw), -math.sin(yaw), 0.0])
        if self.noclip:
            look = gl2q(self.cam.forward())
            move = (look * forward + rq * side + np.array([0, 0, up])) * self.speed * 1.5 * dt
            self.cam.pos = self.cam.pos + q2gl(move)
            return
        wish = (fq * forward + rq * side)
        nrm = np.linalg.norm(wish)
        if nrm > 1:
            wish /= nrm
        o = self.origin
        if self._solid(o):                 # stuck (e.g. spawned in a wall): float free
            self.cam.pos = self.cam.pos + np.array([0, 1.0, 0])
            return
        self.vel[0:2] = wish[0:2] * self.speed
        self.on_ground = self._solid(o - [0, 0, 1.0])
        if self.on_ground:
            self.vel[2] = max(self.vel[2], 0) if not jump else self.jump_speed
        else:
            self.vel[2] -= self.gravity * dt
        o = self._move(o, self.vel * dt)
        self.cam.pos = q2gl(o + [0, 0, self.EYE])

    def _move(self, o, d):
        # vertical first, then horizontal with step-up and per-axis sliding
        steps = max(1, int(np.abs(d).max() // 8) + 1)
        d = d / steps
        for _ in range(steps):
            nz = o + [0, 0, d[2]]
            if not self._solid(nz):
                o = nz
            else:
                self.vel[2] = 0
            h = o + [d[0], d[1], 0]
            if not self._solid(h):
                o = h
                continue
            up = o + [0, 0, self.STEP]
            if self.on_ground and not self._solid(up) and not self._solid(up + [d[0], d[1], 0]):
                o = up + [d[0], d[1], 0]
                for _ in range(int(self.STEP)):          # settle back down onto the step
                    if self._solid(o - [0, 0, 1]):
                        break
                    o = o - [0, 0, 1]
                continue
            for axis in (0, 1):
                t = o.copy()
                t[axis] += d[axis]
                if not self._solid(t):
                    o = t
        return o


def load(path, palette=None):
    return Level(path, palette)
