# -*- coding: utf-8 -*-
"""Open Mining Format 1.0 (.omf) - the open exchange format Leapfrog, Maptek, Deswik and others read
and write. Pure Python (no omf package needed).

File: 60-byte header = magic 84 83 82 81, 32-byte version ('OMF-v0.9.0'), 16-byte project uid,
uint64 offset of the JSON; then the binary blob of zlib-compressed arrays; then UTF-8 JSON - a
dictionary of objects keyed by uid, each with '__class__', referring to others by uid. Arrays are
{'start', 'length', 'dtype'} pointers into the blob.

Read: point sets, line sets, surfaces (triangulated), volume grids (regular block models, with
their axes = rotation) and their data. Write: surfaces, line sets, point sets, regular block models
as VolumeElement and sub-blocked models as point sets with dX dY dZ data.
OMF 2 files (a ZIP with Parquet arrays) are recognised and reported - they need the omf2 library.
"""

import datetime
import json
import os
import struct
import uuid
import zlib

import numpy as np

MAGIC = b"\x84\x83\x82\x81"
VERSION = b"OMF-v0.9.0"


def is_omf2(path):
    with open(path, "rb") as f:
        return f.read(4) == b"PK\x03\x04"


# ----------------------------------------------------------------- reading
class OmfFile:

    def __init__(self, path):
        self.path = path
        self.f = open(path, "rb")
        head = self.f.read(60)
        if head[:4] == b"PK\x03\x04":
            self.f.close()
            raise ValueError("This is an OMF 2 file (ZIP / Parquet). Export OMF 1.0 from Leapfrog "
                             "(or use DXF / OBJ meshes and CSV block models).")
        if head[:4] != MAGIC:
            self.f.close()
            raise ValueError("not an OMF file")
        self.version = head[4:36].rstrip(b"\x00").decode("ascii", "replace")
        (jstart,) = struct.unpack("<Q", head[52:60])
        self.f.seek(jstart)
        self.json = json.loads(self.f.read().decode("utf-8"))
        self.project = next((v for v in self.json.values() if isinstance(v, dict) and v.get("__class__") == "Project"),
                            None)

    def close(self):
        self.f.close()

    def obj(self, ref):
        if isinstance(ref, dict):
            return ref
        return self.json.get(ref) if isinstance(ref, str) else None

    def array(self, ref, cols=None):
        """numpy array from an array object, a uid, a pointer dict or an inline list."""
        o = self.obj(ref) if isinstance(ref, str) else ref
        if o is None:
            return None
        a = o.get("array", o) if isinstance(o, dict) else o
        if isinstance(a, dict) and "start" in a:
            self.f.seek(int(a["start"]))
            buf = zlib.decompress(self.f.read(int(a["length"])))
            arr = np.frombuffer(buf, dtype=np.dtype(a.get("dtype", "<f8")))
        elif isinstance(a, list):
            arr = np.asarray(a)
        else:
            return None
        if cols:
            arr = arr.reshape(-1, cols)
        return arr

    def elements(self):
        """[(uid, element dict)] in project order."""
        if self.project:
            uids = self.project.get("elements") or []
            return [(u, self.json[u]) for u in uids if u in self.json]
        return [(k, v) for k, v in self.json.items() if isinstance(v, dict) and
                str(v.get("__class__", "")).endswith("Element")]

    def origin(self, geom):
        o = np.zeros(3)
        if self.project and self.project.get("origin") is not None:
            o = o + np.asarray(self.project["origin"], float)
        if geom and geom.get("origin") is not None:
            o = o + np.asarray(geom["origin"], float)
        return o

    def data(self, el):
        """[(name, location, values, legend)] of an element's data."""
        out = []
        for ref in el.get("data") or []:
            d = self.obj(ref)
            if not d:
                continue
            cls = d.get("__class__", "")
            arr = self.array(d.get("array"))
            if arr is None:
                continue
            legend = None
            if cls == "MappedData":
                legs = [self.obj(x) for x in d.get("legends") or []]
                for lg in legs:
                    vals = self.array(lg.get("values")) if lg else None
                    if vals is not None and vals.dtype.kind in "USO":
                        legend = list(vals)
                        break
            if cls in ("Vector3Data", "Vector2Data", "ColorData"):
                continue
            out.append((d.get("name") or "data", d.get("location", ""), arr, legend))
        return out


def summary(path):
    """[(uid, name, kind, info)] for a pick list."""
    f = OmfFile(path)
    try:
        out = []
        for uid, el in f.elements():
            cls = el.get("__class__", "")
            g = f.obj(el.get("geometry")) or {}
            kind = {"SurfaceElement": "surface", "LineSetElement": "lines", "PointSetElement": "points",
                    "VolumeElement": "blocks"}.get(cls, cls)
            info = ""
            try:
                if kind == "surface" and g.get("triangles"):
                    o = f.obj(g["triangles"]) or {}
                    a = o.get("array", {})
                    info = "triangulated surface" if a else ""
                elif kind == "surface":
                    kind, info = "grid surface", "(not supported)"
                elif kind == "blocks":
                    tu = f.array(g.get("tensor_u"))
                    tv = f.array(g.get("tensor_v"))
                    tw = f.array(g.get("tensor_w"))
                    info = f"{len(tu)} × {len(tv)} × {len(tw)} cells"
            except Exception:
                pass
            out.append((uid, el.get("name") or cls, kind, info))
        return out
    finally:
        f.close()


def read_element(path, uid):
    """{'kind': 'surface'|'lines'|'points'|'blocks', 'name', 'color', ...} for one element."""
    f = OmfFile(path)
    try:
        el = f.json[uid]
        cls = el.get("__class__", "")
        g = f.obj(el.get("geometry")) or {}
        o = f.origin(g)
        name = el.get("name") or cls
        col = el.get("color")
        if isinstance(col, (list, tuple)) and len(col) >= 3:
            col = "#%02x%02x%02x" % tuple(int(c) for c in col[:3])
        res = {"name": name, "color": col if isinstance(col, str) else None, "data": f.data(el)}
        if cls == "SurfaceElement":
            if not g.get("triangles"):
                raise ValueError(f"{name}: grid surfaces are not supported (only triangulated ones)")
            res.update(kind="surface", vertices=f.array(g["vertices"], 3).astype(float) + o,
                       triangles=f.array(g["triangles"], 3).astype(np.int64))
        elif cls == "LineSetElement":
            res.update(kind="lines", vertices=f.array(g["vertices"], 3).astype(float) + o,
                       segments=f.array(g["segments"], 2).astype(np.int64))
        elif cls == "PointSetElement":
            res.update(kind="points", vertices=f.array(g["vertices"], 3).astype(float) + o)
        elif cls == "VolumeElement":
            tu, tv, tw = (np.asarray(f.array(g.get(k)), float) for k in ("tensor_u", "tensor_v", "tensor_w"))
            axes = np.array([g.get("axis_u", [1, 0, 0]), g.get("axis_v", [0, 1, 0]), g.get("axis_w", [0, 0, 1])],
                            float)
            axes = axes / np.linalg.norm(axes, axis=1)[:, None]
            cu, cv, cw = (np.cumsum(t) - t / 2 for t in (tu, tv, tw))
            # cell data are ordered with u fastest, then v, then w
            W, V, U = np.meshgrid(np.arange(len(tw)), np.arange(len(tv)), np.arange(len(tu)), indexing="ij")
            U, V, W = U.ravel(), V.ravel(), W.ravel()
            xyz = np.column_stack([cu[U], cv[V], cw[W]])
            size = np.column_stack([tu[U], tv[V], tw[W]])
            res.update(kind="blocks", xyz=xyz, size=size, rot=axes.T, origin=o)
        else:
            raise ValueError(f"{name}: {cls} is not supported")
        return res
    finally:
        f.close()


# ----------------------------------------------------------------- writing
def _uid():
    """A uid whose bytes read the same big- or little-endian, so either reading of the header
    uid finds the project."""
    b = bytearray(uuid.uuid4().bytes)
    b[3], b[2] = b[0], b[1]
    b[5] = b[4]
    b[7] = b[6]
    return uuid.UUID(bytes=bytes(b))


class OmfWriter:

    def __init__(self, path, name="QGeo 3D export", description="", units="m", origin=(0, 0, 0)):
        from .q2thook import is_protected
        if is_protected(path):
            raise PermissionError(f"Ringfenced location, not writing: {path}")
        self.path = path
        self.objs = {}
        self.blob = []
        self.pos = 60
        now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.now = now
        self.puid = _uid()
        self.project = self._obj("Project", name=name, description=description, author="QGeo 3D",
                                 revision="", units=units, origin=[float(x) for x in origin], elements=[],
                                 uid=str(self.puid))

    def _obj(self, cls, uid=None, **props):
        u = uid or str(_uid())
        d = {"__class__": cls, "uid": u, "date_created": self.now, "date_modified": self.now}
        d.update(props)
        self.objs[u] = d
        return d

    def _ptr(self, arr, dtype):
        buf = zlib.compress(np.ascontiguousarray(arr, dtype=dtype).tobytes())
        p = {"start": self.pos, "length": len(buf), "dtype": dtype}
        self.blob.append(buf)
        self.pos += len(buf)
        return p

    def _array(self, cls, arr, dtype):
        return self._obj(cls, array=self._ptr(arr, dtype))["uid"]

    def _data(self, fields, rows, location):
        out = []
        for name, t in fields:
            vals = [r.get(name) if r else None for r in rows]
            if t == "R":
                a = np.array([np.nan if v is None else float(v) for v in vals], float)
                arr = self._array("ScalarArray", a, "<f8")
                out.append(self._obj("ScalarData", name=name, description="", location=location, array=arr)["uid"])
            else:
                s = ["" if v is None else str(v) for v in vals]
                cats = sorted(set(s))
                if len(cats) <= max(50, len(s) // 4):
                    idx = np.array([cats.index(x) for x in s], np.int64)
                    arr = self._array("ScalarArray", idx, "<i8")
                    leg = self._obj("Legend", name=name, description="",
                                    values=self._obj("StringArray", array=cats)["uid"])
                    out.append(self._obj("MappedData", name=name, description="", location=location,
                                         array=arr, legends=[leg["uid"]])["uid"])
                else:
                    sa = self._obj("StringArray", array=s)["uid"]
                    out.append(self._obj("StringData", name=name, description="", location=location, array=sa)["uid"])
        return out

    def _add(self, cls, name, geom, data, color=None, **extra):
        col = [int(color[i:i + 2], 16) for i in (1, 3, 5)] if isinstance(color, str) and color.startswith("#") else None
        el = self._obj(cls, name=name, description="", geometry=geom, data=data, color=col or [180, 180, 180],
                       **extra)
        self.project["elements"].append(el["uid"])
        return el

    def surface(self, name, vertices, triangles, color=None):
        g = self._obj("SurfaceGeometry", origin=[0.0, 0.0, 0.0],
                      vertices=self._array("Vector3Array", np.asarray(vertices, float).reshape(-1, 3), "<f8"),
                      triangles=self._array("Int3Array", np.asarray(triangles, np.int64).reshape(-1, 3), "<i8"))
        self._add("SurfaceElement", name, g["uid"], [], color, subtype="surface", textures=[])

    def lines(self, name, polylines, fields=(), rows=(), color=None):
        v, seg, k = [], [], 0
        for pl in polylines:
            pl = np.asarray(pl, float).reshape(-1, 3)
            v.append(pl)
            seg += [(k + i, k + i + 1) for i in range(len(pl) - 1)]
            k += len(pl)
        g = self._obj("LineSetGeometry", origin=[0.0, 0.0, 0.0],
                      vertices=self._array("Vector3Array", np.vstack(v) if v else np.zeros((0, 3)), "<f8"),
                      segments=self._array("Int2Array", np.asarray(seg, np.int64).reshape(-1, 2), "<i8"))
        # one row per polyline -> repeated for each of its segments
        seg_rows = []
        for pl, r in zip(polylines, list(rows) or [None] * len(polylines)):
            seg_rows += [r] * max(len(pl) - 1, 0)
        data = self._data(fields, seg_rows, "segments") if fields and rows else []
        self._add("LineSetElement", name, g["uid"], data, color, subtype="line")

    def points(self, name, xyz, fields=(), rows=(), color=None, size=None):
        g = self._obj("PointSetGeometry", origin=[0.0, 0.0, 0.0],
                      vertices=self._array("Vector3Array", np.asarray(xyz, float).reshape(-1, 3), "<f8"))
        data = self._data(fields, rows, "vertices") if fields else []
        if size is not None:
            for k, nm in enumerate(("dX", "dY", "dZ")):
                arr = self._array("ScalarArray", np.asarray(size, float)[:, k], "<f8")
                data.append(self._obj("ScalarData", name=nm, description="", location="vertices", array=arr)["uid"])
        self._add("PointSetElement", name, g["uid"], data, color, subtype="point", textures=[])

    def blocks(self, name, xyz, size, fields=(), rows=(), rot=None, origin=None):
        """Regular model -> VolumeElement (grid filled, empty cells NaN); sub-blocked -> points + sizes.
        xyz in model coordinates when rot/origin are given (world = origin + rot @ xyz)."""
        xyz = np.asarray(xyz, float)
        size = np.asarray(size, float)
        R = np.eye(3) if rot is None else np.asarray(rot, float)
        o = np.zeros(3) if origin is None else np.asarray(origin, float)
        if not len(xyz) or not np.allclose(size, size[0]):
            w = xyz @ R.T + o
            return self.points(name, w, fields, rows, size=size)
        s = size[0]
        lo = (xyz - s / 2).min(axis=0)
        n = np.round(((xyz + s / 2).max(axis=0) - lo) / s).astype(int)
        ijk = np.floor((xyz - lo) / s).astype(int)
        flat = ijk[:, 0] + n[0] * (ijk[:, 1] + n[1] * ijk[:, 2])       # u fastest
        ncell = int(np.prod(n))
        g = self._obj("VolumeGridGeometry", origin=[float(x) for x in (o + R @ lo)],
                      tensor_u=self._ptr(np.full(n[0], s[0]), "<f8"), tensor_v=self._ptr(np.full(n[1], s[1]), "<f8"),
                      tensor_w=self._ptr(np.full(n[2], s[2]), "<f8"),
                      axis_u=[float(x) for x in R[:, 0]], axis_v=[float(x) for x in R[:, 1]],
                      axis_w=[float(x) for x in R[:, 2]])
        data = []
        for fname, t in fields:
            if t != "R":
                continue
            full = np.full(ncell, np.nan)
            full[flat] = [np.nan if (r.get(fname) is None) else float(r.get(fname)) for r in rows]
            arr = self._array("ScalarArray", full, "<f8")
            data.append(self._obj("ScalarData", name=fname, description="", location="cells", array=arr)["uid"])
        self._add("VolumeElement", name, g["uid"], data, None, subtype="volume")

    def save(self):
        js = json.dumps(self.objs).encode("utf-8")
        with open(self.path, "wb") as f:
            f.write(MAGIC)
            f.write(VERSION.ljust(32, b"\x00"))
            f.write(self.puid.bytes)
            f.write(struct.pack("<Q", self.pos))
            for b in self.blob:
                f.write(b)
            f.write(js)
        return self.path
