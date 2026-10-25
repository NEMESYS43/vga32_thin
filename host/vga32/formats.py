"""3D model files -> gl3d nodes: glTF 2.0 (.gltf / .glb) and Wavefront OBJ (+ MTL).

    from vga32.formats import load_model
    root = load_model(renderer, "robot.glb", parent=None, name="robot")   # a Node with the model's tree
    root.scale = 0.5

glTF: meshes (triangles, indexed or not), POSITION / NORMAL / TEXCOORD_0 / COLOR_0, base color
factors and textures (embedded, data URIs or files), and the node hierarchy with TRS or matrix
transforms. Animations, skins and morph targets are ignored. glTF is right-handed, so the model
is mirrored on z into our left-handed axes.
OBJ: v / vt / vn / f (any polygon size), usemtl with Kd colors and map_Kd textures.
"""
import base64
import json
import math
import os
import struct

import numpy as np
from PIL import Image

from .gl3d import Material, MeshData, Node

COMPONENTS = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}
DTYPES = {5120: np.int8, 5121: np.uint8, 5122: np.int16, 5123: np.uint16, 5125: np.uint32, 5126: np.float32}
FLIP_Z = np.diag([1.0, 1.0, -1.0, 1.0])


def _quat_matrix(q):
    x, y, z, w = q
    return np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                     [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                     [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])


class GLTF:
    def __init__(self, path):
        self.path = path
        self.base = os.path.dirname(os.path.abspath(path))
        with open(path, "rb") as f:
            data = f.read()
        self.bin = None
        if data[:4] == b"glTF":
            length = struct.unpack_from("<I", data, 8)[0]
            ofs = 12
            while ofs < length:
                clen, ctype = struct.unpack_from("<I4s", data, ofs)
                chunk = data[ofs + 8:ofs + 8 + clen]
                if ctype == b"JSON":
                    self.doc = json.loads(chunk)
                elif ctype == b"BIN\0":
                    self.bin = chunk
                ofs += 8 + clen
        else:
            self.doc = json.loads(data)
        self._buffers = {}
        self._images = {}

    def buffer(self, i):
        if i not in self._buffers:
            b = self.doc["buffers"][i]
            uri = b.get("uri")
            if uri is None:
                self._buffers[i] = self.bin
            elif uri.startswith("data:"):
                self._buffers[i] = base64.b64decode(uri.split(",", 1)[1])
            else:
                with open(os.path.join(self.base, uri), "rb") as f:
                    self._buffers[i] = f.read()
        return self._buffers[i]

    def view(self, i):
        bv = self.doc["bufferViews"][i]
        buf = self.buffer(bv["buffer"])
        o = bv.get("byteOffset", 0)
        return buf[o:o + bv["byteLength"]], bv.get("byteStride")

    def accessor(self, i):
        a = self.doc["accessors"][i]
        n, comps = a["count"], COMPONENTS[a["type"]]
        dt = np.dtype(DTYPES[a["componentType"]])
        if "bufferView" not in a:
            out = np.zeros((n, comps), dt)
        else:
            raw, stride = self.view(a["bufferView"])
            o = a.get("byteOffset", 0)
            item = dt.itemsize * comps
            if stride and stride != item:
                rows = np.frombuffer(raw, np.uint8, count=stride * (n - 1) + item, offset=o)
                out = np.lib.stride_tricks.as_strided(rows, (n, item), (stride, 1)).copy().view(dt).reshape(n, comps)
            else:
                out = np.frombuffer(raw, dt, count=n * comps, offset=o).reshape(n, comps)
        if a.get("normalized") and dt.kind in "ui":
            out = out.astype(np.float32) / np.iinfo(dt).max
        return out

    def image(self, i):
        if i not in self._images:
            im = self.doc["images"][i]
            if "bufferView" in im:
                raw, _ = self.view(im["bufferView"])
                import io
                img = Image.open(io.BytesIO(raw))
            elif im.get("uri", "").startswith("data:"):
                import io
                img = Image.open(io.BytesIO(base64.b64decode(im["uri"].split(",", 1)[1])))
            else:
                img = Image.open(os.path.join(self.base, im["uri"]))
            self._images[i] = img.convert("RGBA")
        return self._images[i]

    def material(self, i):
        if i is None:
            return Material()
        m = self.doc["materials"][i]
        pbr = m.get("pbrMetallicRoughness", {})
        f = pbr.get("baseColorFactor", [1, 1, 1, 1])
        tex = None
        if "baseColorTexture" in pbr:
            t = self.doc["textures"][pbr["baseColorTexture"]["index"]]
            if "source" in t:
                tex = self.image(t["source"])
        return Material(color=tuple(int(c * 255) for c in f[:3]), texture=tex, opacity=f[3] if len(f) > 3 else 1.0,
                        alpha_test=m.get("alphaMode") == "MASK", filter="linear",
                        cull="none" if m.get("doubleSided") else "back", emissive=0.0)

    def mesh_parts(self, mi):
        out = []
        for prim in self.doc["meshes"][mi]["primitives"]:
            if prim.get("mode", 4) != 4:
                continue
            at = prim["attributes"]
            pos = self.accessor(at["POSITION"])
            nrm = self.accessor(at["NORMAL"]) if "NORMAL" in at else None
            uv = self.accessor(at["TEXCOORD_0"]).astype(np.float32) if "TEXCOORD_0" in at else None
            if uv is not None:
                uv[:, 1] = 1 - uv[:, 1]        # glTF's v runs down the image; ours runs up
            col = self.accessor(at["COLOR_0"])[:, :3] if "COLOR_0" in at else None
            idx = self.accessor(prim["indices"]).ravel() if "indices" in prim else None
            md = MeshData(pos, normals=nrm, uv=uv, colors=col, indices=idx)
            out.append((md, self.material(prim.get("material"))))
        return out

    def node_matrix(self, n):
        if "matrix" in n:
            return np.array(n["matrix"], float).reshape(4, 4).T
        m = np.eye(4)
        if "rotation" in n:
            m[:3, :3] = _quat_matrix(n["rotation"])
        if "scale" in n:
            m[:3, :3] = m[:3, :3] * np.array(n["scale"], float)
        if "translation" in n:
            m[:3, 3] = n["translation"]
        return m

    def build(self, renderer, parent=None, name=None):
        root = renderer.group(name or os.path.basename(self.path), parent=parent)
        holder = renderer.group("gltf-axes", parent=root)
        holder.matrix = FLIP_Z
        scene = self.doc.get("scene", 0)
        roots = self.doc["scenes"][scene]["nodes"] if self.doc.get("scenes") else list(range(len(self.doc.get("nodes", []))))
        cache = {}

        def make(ni, par):
            n = self.doc["nodes"][ni]
            node = renderer.group(n.get("name"), parent=par)
            node.matrix = self.node_matrix(n)
            if "mesh" in n:
                if n["mesh"] not in cache:
                    cache[n["mesh"]] = self.mesh_parts(n["mesh"])
                for md, mat in cache[n["mesh"]]:
                    renderer.add(md, mat, parent=node, name=self.doc["meshes"][n["mesh"]].get("name"))
            for c in n.get("children", []):
                make(c, node)
        for ni in roots:
            make(ni, holder)
        return root


def load_obj(renderer, path, parent=None, name=None, color="#c8c8c8"):
    """OBJ with MTL colors / textures, uv and normals. Returns a group Node, one child per material."""
    base = os.path.dirname(os.path.abspath(path))
    v, vt, vn = [], [], []
    mats = {}
    groups = {}
    cur = None
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = f.read().splitlines()
    for line in lines:
        p = line.split()
        if not p:
            continue
        if p[0] == "v":
            v.append([float(x) for x in p[1:4]])
        elif p[0] == "vt":
            vt.append([float(x) for x in p[1:3]])
        elif p[0] == "vn":
            vn.append([float(x) for x in p[1:4]])
        elif p[0] == "mtllib":
            mats.update(_read_mtl(os.path.join(base, " ".join(p[1:]))))
        elif p[0] == "usemtl":
            cur = " ".join(p[1:])
        elif p[0] == "f":
            refs = []
            for t in p[1:]:
                s = (t.split("/") + ["", ""])[:3]
                fix = lambda x, n: (int(x) - 1 if int(x) > 0 else n + int(x)) if x else None
                refs.append((fix(s[0], len(v)), fix(s[1], len(vt)), fix(s[2], len(vn))))
            g = groups.setdefault(cur, [])
            for i in range(1, len(refs) - 1):
                g.append((refs[0], refs[i], refs[i + 1]))
    root = renderer.group(name or os.path.basename(path), parent=parent)
    V, VT, VN = np.array(v or [[0, 0, 0]], float), np.array(vt or [[0, 0]], float), np.array(vn or [[0, 0, 1]], float)
    for mname, tris in groups.items():
        flat = [r for t in tris for r in t]
        pos = V[[r[0] for r in flat]]
        has_uv = all(r[1] is not None for r in flat) and vt
        has_n = all(r[2] is not None for r in flat) and vn
        uv = VT[[r[1] for r in flat]] if has_uv else None
        nrm = VN[[r[2] for r in flat]] if has_n else None
        md = MeshData(pos, normals=nrm, uv=uv)
        m = mats.get(mname, {})
        tex = None
        if m.get("map_Kd") and os.path.exists(m["map_Kd"]):
            tex = Image.open(m["map_Kd"]).convert("RGBA")
        mat = Material(color=m.get("Kd", color), texture=tex, opacity=m.get("d", 1.0), filter="linear")
        renderer.add(md, mat, parent=root, name=mname)
    return root


def _read_mtl(path):
    out, cur = {}, None
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                p = line.split()
                if not p:
                    continue
                if p[0] == "newmtl":
                    cur = out.setdefault(" ".join(p[1:]), {})
                elif cur is None:
                    continue
                elif p[0] == "Kd":
                    cur["Kd"] = tuple(int(float(x) * 255) for x in p[1:4])
                elif p[0] == "d":
                    cur["d"] = float(p[1])
                elif p[0] == "map_Kd":
                    cur["map_Kd"] = os.path.join(os.path.dirname(path), p[-1])
    except OSError:
        pass
    return out


def load_model(renderer, path, parent=None, name=None):
    """.gltf / .glb / .obj into the renderer; returns the model's root Node."""
    low = path.lower()
    if low.endswith((".gltf", ".glb")):
        return GLTF(path).build(renderer, parent, name)
    if low.endswith(".obj"):
        return load_obj(renderer, path, parent, name)
    raise ValueError(f"unsupported model format: {os.path.basename(path)} (use .gltf, .glb or .obj)")
