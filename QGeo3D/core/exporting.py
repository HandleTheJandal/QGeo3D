# -*- coding: utf-8 -*-
"""Collect data for writers.py from QGIS layers, 3D meshes and block models, in a chosen CRS."""

import numpy as np
from qgis.core import Qgis, QgsCoordinateTransform, QgsProject, QgsWkbTypes

from .crs import transform_xyz
from .tables import guess


def layer_kind(layer):
    """'strings' | 'points' | 'wireframe' | None for a vector layer; points with block sizes -> 'blocks?'."""
    try:
        gt = layer.geometryType()
    except Exception:
        return None
    if gt == Qgis.GeometryType.Line:
        return "strings"
    if gt == Qgis.GeometryType.Point:
        from .micromine import block_cols, has_block_sizes
        f = layer_fields(layer)
        return "points+blocks" if (block_cols(f) or has_block_sizes(f)) else "points"
    if gt == Qgis.GeometryType.Polygon:
        return "wireframe"
    return None


def layer_fields(layer):
    out = []
    for fd in layer.fields():
        if fd.name().lower() == "fid":          # GeoPackage feature id, not data
            continue
        out.append((fd.name(), "R" if fd.isNumeric() else "C"))
    return out


def _val(v):
    try:
        if v is None or (hasattr(v, "isNull") and v.isNull()):
            return None
    except Exception:
        pass
    if hasattr(v, "toString") and not isinstance(v, (int, float, str)):
        return v.toString()
    return v


def _feats(layer, selected_only):
    return layer.getSelectedFeatures() if selected_only and layer.selectedFeatureCount() else layer.getFeatures()


def _xf(layer, crs):
    if crs is None or not layer.crs().isValid() or layer.crs() == crs:
        return None
    return QgsCoordinateTransform(layer.crs(), crs, QgsProject.instance())


def layer_strings(layer, crs=None, selected_only=False):
    xf = _xf(layer, crs)
    fields = layer_fields(layer)
    names = [f[0] for f in fields]
    out = []
    for f in _feats(layer, selected_only):
        g = f.geometry()
        if g.isNull():
            continue
        if xf:
            g.transform(xf)
        attrs = {n: _val(f[n]) for n in names}
        for part in g.constParts():
            pts = np.array([(v.x(), v.y(), v.z() if v.is3D() else 0.0) for v in part.vertices()], float)
            if len(pts) == 0:
                continue
            closed = len(pts) > 2 and np.allclose(pts[0], pts[-1], rtol=0, atol=1e-6)
            if closed:
                pts = pts[:-1]
            out.append({"pts": pts, "closed": bool(closed), "attrs": attrs})
    return out, fields


def layer_points(layer, crs=None, selected_only=False):
    xf = _xf(layer, crs)
    fields = layer_fields(layer)
    names = [f[0] for f in fields]
    xyz, rows = [], []
    for f in _feats(layer, selected_only):
        g = f.geometry()
        if g.isNull():
            continue
        if xf:
            g.transform(xf)
        for v in g.vertices():
            xyz.append((v.x(), v.y(), v.z() if v.is3D() else 0.0))
            rows.append({n: _val(f[n]) for n in names})
    return (np.array(xyz, float).reshape(-1, 3), rows), fields


def layer_blocks(layer, crs=None, selected_only=False):
    (xyz, rows), fields = layer_points(layer, crs, selected_only)
    cols = [guess(fields, r) for r in ("dx", "dy", "dz")]
    size = np.array([[float(r.get(c) or 0) for c in cols] for r in rows], float).reshape(-1, 3)
    keep = [f for f in fields if f[0] not in cols]
    return (xyz, size, rows), keep


def layer_wireframe(layer, crs=None, selected_only=False):
    from .mesh import read_qgis_layer
    return read_qgis_layer(layer, crs, selected_only), []


class _M:
    """A mesh copy in another CRS (writers need vertices, triangles, name, color, stats)."""

    def __init__(self, mesh, vertices):
        from .mesh import Mesh
        self._m = Mesh(vertices, mesh.triangles, mesh.name, getattr(mesh, "source", ""))
        self.vertices, self.triangles, self.name = self._m.vertices, self._m.triangles, mesh.name
        self.color = getattr(mesh, "color", None)

    def stats(self):
        return self._m.stats()


def mesh_in(mesh, crs, color=None):
    """Mesh (held in the project CRS) as written in `crs` (exact source vertices if crs is its file CRS)."""
    prj = QgsProject.instance().crs()
    src = getattr(mesh, "src_crs", None)
    if src is not None and crs is not None and src == crs and getattr(mesh, "src_vertices", None) is not None:
        v = mesh.src_vertices
    elif crs is None or crs == prj:
        v = mesh.vertices
    else:
        v = transform_xyz(mesh.vertices, prj, crs)
    m = _M(mesh, v)
    if color:
        m.color = color
    return m


def blocks_in(bm, crs):
    """(xyz, size, rows), fields, note. Unrotated models: centres in `crs`; rotated models keep model
    coordinates (an axis-aligned block can't be rotated in a plain table) and say so."""
    names = list(bm.numeric) + [n for n in bm.fields if n in bm.data and bm.data[n].dtype == object]
    skip = {n.upper() for n in (getattr(bm, "size_fields", None) or ())}
    names = [n for n in names if n.upper() not in skip and n.upper() not in (
        "EAST", "NORTH", "RL", "_EAST", "_NORTH", "_RL", "XC", "YC", "ZC", "XINC", "YINC", "ZINC", "IJK",
        "XMORIG", "YMORIG", "ZMORIG", "NX", "NY", "NZ")]
    fields = [(n, "R" if bm.data[n].dtype != object else "C") for n in names]
    rows = []
    for i in range(len(bm)):
        r = {}
        for n, t in fields:
            v = bm.data[n][i]
            r[n] = (None if v != v else float(v)) if t == "R" else v
        rows.append(r)
    note = None
    prj = QgsProject.instance().crs()
    src = getattr(bm, "src_crs", None)
    o0, r0 = bm._src_place if hasattr(bm, "_src_place") else (bm.origin, bm.rot)
    if not np.allclose(r0, np.eye(3), atol=1e-9):
        # truly rotated model (in its own file): a plain table can't hold the rotation
        xyz = bm.xyz
        note = "the model is rotated: centres are written in model coordinates (origin and rotation not applied)"
    elif src is not None and crs is not None and src == crs:
        xyz = bm.xyz + o0                              # exactly the file's own coordinates
    else:
        w = bm.world(bm.xyz)
        xyz = w if crs is None or crs == prj else transform_xyz(w, prj, crs)
        ang = np.degrees(np.arctan2(bm.rot[1, 0], bm.rot[0, 0]))
        if abs(ang) > 0.01:
            note = (f"grid convergence between the file CRS and the project CRS turns the model {ang:.3f}°; "
                    "blocks are written axis-aligned at their transformed centres")
    return (np.asarray(xyz, float), np.asarray(bm.size, float), rows), fields, note


def blocks_rotated_in(bm, crs):
    """(xyz world in crs, size, rows, {azimuth, origin, rot}), fields, note - for formats that keep a model
    rotation (Leapfrog CSV header, OMF axes). Rotations other than about Z fall back to blocks_in."""
    (xyz, size, rows), fields, note = blocks_in(bm, crs)
    o0, r0 = bm._src_place if hasattr(bm, "_src_place") else (bm.origin, bm.rot)
    if np.allclose(r0, np.eye(3), atol=1e-9):
        return (xyz, size, rows, {}), fields, note
    if not (np.allclose(r0[2], [0, 0, 1]) and np.allclose(r0[:, 2], [0, 0, 1])):
        return (xyz, size, rows, {}), fields, note + " (rotation is not about Z only)"
    src = getattr(bm, "src_crs", None)
    w = bm.xyz @ np.asarray(r0).T + o0                 # in the file's own coordinates
    if crs is not None and src is not None and crs != src:
        w = transform_xyz(w, src, crs)
        note = "centroids transformed to the chosen CRS; the rotation is kept as in the file"
    else:
        note = None
    az = float(np.degrees(np.arctan2(r0[0, 1], r0[0, 0])))
    return (w, size, rows, {"azimuth": az, "origin": np.asarray(o0, float), "rot": np.asarray(r0, float),
                            "model_xyz": bm.xyz}), fields, note
