"""EditableMesh: a polygon mesh you can change - for modelers, level editors, procedural geometry.

    from vga32.mesh import EditableMesh
    m = EditableMesh.cube(2)
    top = m.faces_facing((0, 1, 0))                # face indices whose normal points up
    new = m.extrude(top, 1.0)                      # pull them up by 1; returns the new cap faces
    m.translate(m.face_vertices(new), (0, 0, 0.5))
    m.subdivide()                                  # each face -> quads around its center
    hit = m.raycast(origin, direction)             # (face, distance, point) or None
    data = m.to_meshdata(colors=...)               # for gl3d.Renderer.add(...)
    m.save_obj("thing.obj"); m2 = EditableMesh.load_obj("thing.obj")

Vertices are an (N, 3) float array; faces are lists of vertex indices of any polygon size. A face's
normal follows its vertex order, so keep neighbouring faces wound the same way (the primitives here
are). Each face has a color.
snapshot() / undo() / redo() give undo history.
"""
import json
import math

import numpy as np

from .render3d import to_rgb


class EditableMesh:
    def __init__(self, vertices=(), faces=(), colors=None):
        self.vertices = np.asarray(vertices, np.float64).reshape(-1, 3)
        self.faces = [list(map(int, f)) for f in faces]
        if colors is None:
            self.colors = [(200, 200, 200)] * len(self.faces)
        elif isinstance(colors, (str, tuple)) and (isinstance(colors, str) or len(colors) == 3 and
                                                    not isinstance(colors[0], (tuple, list))):
            self.colors = [to_rgb(colors)] * len(self.faces)
        else:
            self.colors = [to_rgb(c) for c in colors]
        self._undo, self._redo = [], []

    # ----- primitives -----
    @staticmethod
    def cube(size=1.0, color="#c8c8c8"):
        s = size / 2
        v = [(-s, -s, -s), (s, -s, -s), (s, s, -s), (-s, s, -s), (-s, -s, s), (s, -s, s), (s, s, s), (-s, s, s)]
        f = [(0, 3, 2, 1), (4, 5, 6, 7), (0, 4, 7, 3), (1, 2, 6, 5), (3, 7, 6, 2), (0, 1, 5, 4)]
        return EditableMesh(v, f, color)

    @staticmethod
    def plane(w=2.0, d=2.0, cells=1, color="#c8c8c8"):
        n = cells + 1
        v = [(-w / 2 + w * i / cells, 0, -d / 2 + d * j / cells) for j in range(n) for i in range(n)]
        f = [(j * n + i, (j + 1) * n + i, (j + 1) * n + i + 1, j * n + i + 1) for j in range(cells) for i in range(cells)]
        return EditableMesh(v, f, color)

    @staticmethod
    def cylinder(r=0.5, h=1.0, segments=16, color="#c8c8c8"):
        v = []
        for j in range(segments):
            a = 2 * math.pi * j / segments
            v += [(r * math.cos(a), -h / 2, r * math.sin(a)), (r * math.cos(a), h / 2, r * math.sin(a))]
        f = [(2 * j, 2 * j + 1, (2 * j + 3) % (2 * segments), (2 * j + 2) % (2 * segments)) for j in range(segments)]
        f.append([2 * j + 1 for j in range(segments)])
        f.append([2 * j for j in reversed(range(segments))])
        return EditableMesh(v, f, color)

    @staticmethod
    def sphere(r=0.5, segments=16, rings=10, color="#c8c8c8"):
        v = [(0, r, 0)]
        for i in range(1, rings):
            phi = math.pi * i / rings
            for j in range(segments):
                th = 2 * math.pi * j / segments
                v.append((r * math.sin(phi) * math.cos(th), r * math.cos(phi), r * math.sin(phi) * math.sin(th)))
        v.append((0, -r, 0))
        bottom = len(v) - 1
        ring = lambda i, j: 1 + (i - 1) * segments + j % segments
        f = []
        for j in range(segments):
            f.append((0, ring(1, j + 1), ring(1, j)))
            f.append((bottom, ring(rings - 1, j), ring(rings - 1, j + 1)))
        for i in range(1, rings - 1):
            for j in range(segments):
                f.append((ring(i, j), ring(i, j + 1), ring(i + 1, j + 1), ring(i + 1, j)))
        return EditableMesh(v, f, color)

    @staticmethod
    def from_meshdata(md):
        tri = md._tris()
        v = tri.reshape(-1, 3)
        return EditableMesh(v, [(3 * i, 3 * i + 1, 3 * i + 2) for i in range(len(tri))]).merge_by_distance()

    # ----- undo -----
    def snapshot(self):
        """Remember the current state (call before an edit you want to undo)."""
        self._undo.append((self.vertices.copy(), [list(f) for f in self.faces], list(self.colors)))
        del self._undo[:-100]
        self._redo.clear()

    def undo(self):
        if not self._undo:
            return False
        self._redo.append((self.vertices.copy(), [list(f) for f in self.faces], list(self.colors)))
        self.vertices, self.faces, self.colors = self._undo.pop()
        return True

    def redo(self):
        if not self._redo:
            return False
        self._undo.append((self.vertices.copy(), [list(f) for f in self.faces], list(self.colors)))
        self.vertices, self.faces, self.colors = self._redo.pop()
        return True

    def copy(self):
        return EditableMesh(self.vertices.copy(), [list(f) for f in self.faces], list(self.colors))

    # ----- queries -----
    def face_normal(self, fi):
        p = self.vertices[self.faces[fi]]
        n = np.zeros(3)
        for i in range(len(p)):                   # Newell's method: robust for any polygon
            a, b = p[i], p[(i + 1) % len(p)]
            n += np.array([(a[1] - b[1]) * (a[2] + b[2]), (a[2] - b[2]) * (a[0] + b[0]), (a[0] - b[0]) * (a[1] + b[1])])
        ln = np.linalg.norm(n)
        return n / ln if ln > 1e-12 else n

    def face_center(self, fi):
        return self.vertices[self.faces[fi]].mean(axis=0)

    def face_vertices(self, faces):
        return sorted({v for fi in faces for v in self.faces[fi]})

    def faces_facing(self, direction, threshold=0.9):
        d = np.asarray(direction, float)
        d /= np.linalg.norm(d)
        return [fi for fi in range(len(self.faces)) if self.face_normal(fi) @ d >= threshold]

    def edges(self):
        """Unique edges as sorted (a, b) vertex pairs."""
        out = set()
        for f in self.faces:
            for i in range(len(f)):
                a, b = f[i], f[(i + 1) % len(f)]
                out.add((min(a, b), max(a, b)))
        return sorted(out)

    def bounds(self):
        if len(self.vertices) == 0:
            return np.zeros(3), np.zeros(3)
        return self.vertices.min(0), self.vertices.max(0)

    def stats(self):
        return {"vertices": len(self.vertices), "faces": len(self.faces), "edges": len(self.edges()),
                "triangles": sum(len(f) - 2 for f in self.faces)}

    def raycast(self, origin, direction):
        """Nearest face hit: (face index, distance, point) or None."""
        o, d = np.asarray(origin, float), np.asarray(direction, float)
        tris, owner = [], []
        for fi, f in enumerate(self.faces):
            for i in range(1, len(f) - 1):
                tris.append((f[0], f[i], f[i + 1]))
                owner.append(fi)
        if not tris:
            return None
        t3 = self.vertices[np.array(tris)]
        e1, e2 = t3[:, 1] - t3[:, 0], t3[:, 2] - t3[:, 0]
        pv = np.cross(d, e2)
        det = (e1 * pv).sum(1)
        ok = np.abs(det) > 1e-12
        inv = np.where(ok, 1 / np.where(ok, det, 1), 0)
        tv = o - t3[:, 0]
        u = (tv * pv).sum(1) * inv
        qv = np.cross(tv, e1)
        v = (d * qv).sum(1) * inv
        t = (e2 * qv).sum(1) * inv
        hit = ok & (u >= 0) & (v >= 0) & (u + v <= 1) & (t > 1e-9)
        if not hit.any():
            return None
        t = np.where(hit, t, np.inf)
        i = int(t.argmin())
        return owner[i], float(t[i]), o + d * t[i]

    def nearest_vertex(self, point, max_dist=None):
        if len(self.vertices) == 0:
            return None
        dist = np.linalg.norm(self.vertices - np.asarray(point, float), axis=1)
        i = int(dist.argmin())
        return None if max_dist is not None and dist[i] > max_dist else i

    # ----- edits -----
    def add_vertex(self, p):
        self.vertices = np.vstack([self.vertices, np.asarray(p, float).reshape(1, 3)])
        return len(self.vertices) - 1

    def add_face(self, vertex_ids, color=None):
        self.faces.append(list(map(int, vertex_ids)))
        self.colors.append(to_rgb(color) if color is not None else (200, 200, 200))
        return len(self.faces) - 1

    def add_mesh(self, other, offset=(0, 0, 0)):
        """Append another EditableMesh (moved by offset); returns its new face indices."""
        base = len(self.vertices)
        self.vertices = np.vstack([self.vertices, other.vertices + np.asarray(offset, float)])
        start = len(self.faces)
        self.faces += [[v + base for v in f] for f in other.faces]
        self.colors += list(other.colors)
        return list(range(start, len(self.faces)))

    def translate(self, vertex_ids, delta):
        ids = list(vertex_ids)
        self.vertices[ids] += np.asarray(delta, float)

    def transform(self, matrix, vertex_ids=None):
        """Apply a 4x4 (or 3x3) matrix to some or all vertices."""
        m = np.asarray(matrix, float)
        ids = slice(None) if vertex_ids is None else list(vertex_ids)
        v = self.vertices[ids]
        if m.shape == (4, 4):
            v = v @ m[:3, :3].T + m[:3, 3]
        else:
            v = v @ m.T
        self.vertices[ids] = v

    def scale(self, factor, vertex_ids=None, center=None):
        ids = list(range(len(self.vertices))) if vertex_ids is None else list(vertex_ids)
        c = self.vertices[ids].mean(0) if center is None else np.asarray(center, float)
        self.vertices[ids] = c + (self.vertices[ids] - c) * np.asarray(factor, float)

    def set_color(self, faces, color):
        c = to_rgb(color)
        for fi in faces:
            self.colors[fi] = c

    def flip(self, faces):
        for fi in faces:
            self.faces[fi] = self.faces[fi][::-1]

    def delete_faces(self, faces):
        drop = set(faces)
        keep = [i for i in range(len(self.faces)) if i not in drop]
        self.faces = [self.faces[i] for i in keep]
        self.colors = [self.colors[i] for i in keep]
        self.remove_unused_vertices()

    def delete_vertices(self, vertex_ids):
        drop = set(vertex_ids)
        keep = [i for i, f in enumerate(self.faces) if not drop & set(f)]
        self.faces = [self.faces[i] for i in keep]
        self.colors = [self.colors[i] for i in keep]
        self.remove_unused_vertices()

    def remove_unused_vertices(self):
        used = sorted({v for f in self.faces for v in f})
        remap = {old: new for new, old in enumerate(used)}
        self.vertices = self.vertices[used] if used else np.zeros((0, 3))
        self.faces = [[remap[v] for v in f] for f in self.faces]

    def merge_by_distance(self, eps=1e-5):
        """Weld vertices closer than eps (and drop faces that collapse)."""
        if len(self.vertices) == 0:
            return self
        key = np.round(self.vertices / eps).astype(np.int64)
        _, first, inverse = np.unique(key, axis=0, return_index=True, return_inverse=True)
        self.vertices = self.vertices[first]
        faces, colors = [], []
        for f, c in zip(self.faces, self.colors):
            g = []
            for v in f:
                nv = int(inverse[v])
                if not g or g[-1] != nv:
                    g.append(nv)
            if len(g) > 1 and g[0] == g[-1]:
                g.pop()
            if len(set(g)) >= 3:
                faces.append(g)
                colors.append(c)
        self.faces, self.colors = faces, colors
        return self

    def extrude(self, faces, distance=1.0, direction=None):
        """Extrude a region of faces along their (average) normal; side walls are added and the
        original faces become the moved caps. Returns the cap face indices."""
        faces = list(faces)
        if not faces:
            return []
        region = set(faces)
        # edges on the region's boundary: used by exactly one region face
        count = {}
        for fi in faces:
            f = self.faces[fi]
            for i in range(len(f)):
                e = (f[i], f[(i + 1) % len(f)])
                k = (min(e), max(e))
                count[k] = count.get(k, []) + [(fi, e)]
        boundary = [owners[0][1] for owners in count.values() if len(owners) == 1]
        if direction is None:
            n = sum(self.face_normal(fi) for fi in faces)
            n = n / (np.linalg.norm(n) or 1)
        else:
            n = np.asarray(direction, float) / (np.linalg.norm(direction) or 1)
        verts = self.face_vertices(faces)
        dup = {v: self.add_vertex(self.vertices[v]) for v in verts}
        for a, b in boundary:
            fi = next(fi for fi in region if a in self.faces[fi])
            self.add_face([a, b, dup[b], dup[a]], self.colors[fi])
        for fi in faces:
            self.faces[fi] = [dup[v] for v in self.faces[fi]]
        self.translate(list(dup.values()), n * distance)
        return faces

    def inset(self, faces, amount=0.2):
        """Shrink each face toward its center, ringing it with new quads. Returns the inner faces."""
        out = []
        for fi in list(faces):
            f = self.faces[fi]
            c = self.face_center(fi)
            inner = [self.add_vertex(self.vertices[v] + (c - self.vertices[v]) * amount) for v in f]
            for i in range(len(f)):
                j = (i + 1) % len(f)
                self.add_face([f[i], f[j], inner[j], inner[i]], self.colors[fi])
            self.faces[fi] = inner
            out.append(fi)
        return out

    def subdivide(self, faces=None):
        """Split faces into quads around their centers (one level, edges split at midpoints and
        shared between neighbours). Returns the new face count."""
        targets = set(range(len(self.faces)) if faces is None else faces)
        mid = {}

        def midpoint(a, b):
            k = (min(a, b), max(a, b))
            if k not in mid:
                mid[k] = self.add_vertex((self.vertices[a] + self.vertices[b]) / 2)
            return mid[k]
        new_faces, new_colors = [], []
        for fi, (f, col) in enumerate(zip(self.faces, self.colors)):
            if fi not in targets:
                new_faces.append(f)
                new_colors.append(col)
                continue
            c = self.add_vertex(self.vertices[f].mean(0))
            m = [midpoint(f[i], f[(i + 1) % len(f)]) for i in range(len(f))]
            for i in range(len(f)):
                new_faces.append([f[i], m[i], c, m[i - 1]])
                new_colors.append(col)
        self.faces, self.colors = new_faces, new_colors
        return len(self.faces)

    def smooth(self, iterations=1, factor=0.5, vertex_ids=None):
        """Laplacian smoothing: move vertices toward the average of their neighbours."""
        nbrs = [set() for _ in range(len(self.vertices))]
        for a, b in self.edges():
            nbrs[a].add(b)
            nbrs[b].add(a)
        ids = range(len(self.vertices)) if vertex_ids is None else vertex_ids
        for _ in range(iterations):
            v = self.vertices.copy()
            for i in ids:
                if nbrs[i]:
                    v[i] += (self.vertices[list(nbrs[i])].mean(0) - self.vertices[i]) * factor
            self.vertices = v

    def triangulate(self, faces=None):
        targets = set(range(len(self.faces)) if faces is None else faces)
        nf, nc = [], []
        for fi, (f, c) in enumerate(zip(self.faces, self.colors)):
            if fi in targets and len(f) > 3:
                for i in range(1, len(f) - 1):
                    nf.append([f[0], f[i], f[i + 1]])
                    nc.append(c)
            else:
                nf.append(f)
                nc.append(c)
        self.faces, self.colors = nf, nc

    def mirror(self, axis=0, merge=True):
        """Add a mirrored copy across the plane axis = 0 (0 x, 1 y, 2 z)."""
        other = self.copy()
        other.vertices[:, axis] *= -1
        other.flip(range(len(other.faces)))
        self.add_mesh(other)
        if merge:
            self.merge_by_distance()

    # ----- output -----
    def to_meshdata(self, smooth=False, colors=None, highlight=None, highlight_color="#ffd040"):
        """gl3d.MeshData for rendering. colors overrides the per-face colors; highlight is a set of
        face indices drawn in highlight_color. The result's face_of_triangle maps back to faces."""
        from .gl3d import MeshData
        cols = list(colors) if colors is not None else list(self.colors)
        if highlight:
            hc = to_rgb(highlight_color)
            cols = [hc if i in highlight else c for i, c in enumerate(cols)]
        if not self.faces:
            return MeshData(np.zeros((0, 3)))
        return MeshData.from_indexed(self.vertices, self.faces, cols, smooth=smooth)

    def to_wire(self, color="black", vertex_ids=None):
        from .gl3d import MeshData
        segs = [(self.vertices[a], self.vertices[b]) for a, b in self.edges()]
        return MeshData.lines(segs, color) if segs else MeshData(np.zeros((0, 3)), mode="lines")

    def to_obj(self):
        lines = ["# made with vga32"]
        lines += [f"v {x:.6g} {y:.6g} {z:.6g}" for x, y, z in self.vertices]
        lines += ["f " + " ".join(str(v + 1) for v in f) for f in self.faces]
        return "\n".join(lines) + "\n"

    def save_obj(self, path):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(self.to_obj())
        with open(path + ".colors.json", "w", encoding="utf-8") as fh:
            json.dump([list(c) for c in self.colors], fh)

    @staticmethod
    def from_obj(text, color="#c8c8c8"):
        v, f = [], []
        for line in text.splitlines():
            p = line.split()
            if not p:
                continue
            if p[0] == "v":
                v.append(tuple(float(x) for x in p[1:4]))
            elif p[0] == "f":
                idx = [int(t.split("/")[0]) for t in p[1:]]
                f.append([i - 1 if i > 0 else len(v) + i for i in idx])
        return EditableMesh(v, f, color)

    @staticmethod
    def load_obj(path, color="#c8c8c8"):
        with open(path, encoding="utf-8", errors="replace") as fh:
            m = EditableMesh.from_obj(fh.read(), color)
        try:
            with open(path + ".colors.json", encoding="utf-8") as fh:
                cols = json.load(fh)
            if len(cols) == len(m.faces):
                m.colors = [tuple(c) for c in cols]
        except (OSError, ValueError):
            pass
        return m

    def to_dict(self):
        return {"vertices": self.vertices.round(6).tolist(), "faces": self.faces, "colors": [list(c) for c in self.colors]}

    @staticmethod
    def from_dict(d):
        return EditableMesh(d["vertices"], d["faces"], [tuple(c) for c in d.get("colors", [])] or None)
