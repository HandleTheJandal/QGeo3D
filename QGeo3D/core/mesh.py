# -*- coding: utf-8 -*-
"""Indexed triangle mesh + readers (OGR / OBJ / QGIS layer) for QGeo 3D.

All coordinates are kept in float64 map units (project CRS). Conversion to a
float32 local frame only happens when sending to the 3D view.
"""

import os
import numpy as np
from .compat import gdal_soft

try:
    from osgeo import ogr
except ImportError:     # pragma: no cover
    ogr = None


class Mesh:

    def __init__(self, vertices, triangles, name="", source=""):
        self.vertices = np.asarray(vertices, dtype=np.float64).reshape(-1, 3)
        self.triangles = np.asarray(triangles, dtype=np.int64).reshape(-1, 3)
        self.name = name
        self.source = source
        self.warnings = []
        self._edges = None

    # ------------------------------------------------------------------ build
    @classmethod
    def from_triangle_soup(cls, tris, name="", source="", tol=1e-4):
        """tris: (n, 3, 3) array of triangle corner coordinates.
        Vertices closer than `tol` (map units) are merged."""
        tris = np.asarray(tris, dtype=np.float64).reshape(-1, 3, 3)
        if len(tris) == 0:
            return cls(np.zeros((0, 3)), np.zeros((0, 3), dtype=np.int64), name, source)

        pts = tris.reshape(-1, 3)
        key = np.round(pts / tol).astype(np.int64)
        _, first, inv = np.unique(key, axis=0, return_index=True, return_inverse=True)
        inv = np.asarray(inv).reshape(-1)
        verts = pts[first]
        faces = inv.reshape(-1, 3)

        ok = (faces[:, 0] != faces[:, 1]) & (faces[:, 1] != faces[:, 2]) & (faces[:, 0] != faces[:, 2])
        mesh = cls(verts, faces[ok], name, source)
        dropped = int((~ok).sum())
        if dropped:
            mesh.warnings.append(f"{dropped} degenerate triangle(s) dropped")
        return mesh

    # ------------------------------------------------------------- topology
    def edges(self):
        """Unique undirected edges (m, 2) and how many triangles use each."""
        if self._edges is None:
            f = self.triangles
            e = np.vstack([f[:, [0, 1]], f[:, [1, 2]], f[:, [2, 0]]])
            e.sort(axis=1)
            uniq, counts = np.unique(e, axis=0, return_counts=True)
            self._edges = (uniq, counts)
        return self._edges

    def stats(self):
        v, f = self.vertices, self.triangles
        out = {"name": self.name, "vertices": len(v), "triangles": len(f)}
        if len(f) == 0:
            return out
        edges, counts = self.edges()
        out["edges"] = len(edges)
        out["open_edges"] = int((counts == 1).sum())
        out["nonmanifold_edges"] = int((counts > 2).sum())

        # orientation consistency: in a consistently wound manifold every
        # directed edge appears exactly once
        d = np.vstack([f[:, [0, 1]], f[:, [1, 2]], f[:, [2, 0]]])
        _, dcounts = np.unique(d, axis=0, return_counts=True)
        out["inconsistent_edges"] = int((dcounts > 1).sum())
        out["closed"] = out["open_edges"] == 0 and out["nonmanifold_edges"] == 0

        c = v.mean(axis=0)
        a, b, cc = v[f[:, 0]] - c, v[f[:, 1]] - c, v[f[:, 2]] - c
        cross = np.cross(b - a, cc - a)
        out["area"] = float(0.5 * np.linalg.norm(cross, axis=1).sum())
        vol = float(np.einsum("ij,ij->i", a, np.cross(b, cc)).sum() / 6.0)
        out["volume"] = abs(vol) if out["closed"] else None
        out["inside_out"] = (vol < 0) if out["closed"] else None
        mn, mx = v.min(axis=0), v.max(axis=0)
        out["extent"] = [float(x) for x in (*mn, *mx)]
        return out

    # -------------------------------------------------------- serialisation
    def local_frame(self):
        """Return (offset float64[3], vertices float32 relative to offset)."""
        if len(self.vertices) == 0:
            return np.zeros(3), np.zeros((0, 3), dtype=np.float32)
        mn, mx = self.vertices.min(axis=0), self.vertices.max(axis=0)
        offset = np.round((mn + mx) / 2.0, 3)
        return offset, (self.vertices - offset).astype(np.float32)


# ====================================================================== readers
def _ring_points(ring):
    pts = ring.GetPoints() or []
    if pts and len(pts[0]) == 2:
        pts = [(p[0], p[1], 0.0) for p in pts]
    # drop closing point and consecutive duplicates
    out = []
    for p in pts:
        if not out or p != out[-1]:
            out.append(p)
    if len(out) > 1 and out[0] == out[-1]:
        out.pop()
    return out


def _triangulate_ring(pts, tris, counters):
    n = len(pts)
    if n < 3:
        counters["degenerate"] += 1
        return
    if n == 3:
        tris.append(pts)
    elif n == 4:                    # 3DFACE / quad
        tris.append([pts[0], pts[1], pts[2]])
        tris.append([pts[0], pts[2], pts[3]])
    else:                           # fan; fine for convex planar faces
        counters["fanned"] += 1
        for i in range(1, n - 1):
            tris.append([pts[0], pts[i], pts[i + 1]])


def _collect_ogr(geom, tris, counters):
    if geom is None:
        return
    name = geom.GetGeometryName().upper()
    if name in ("POLYGON", "TRIANGLE"):
        if geom.GetGeometryCount() > 1:
            counters["holes"] += 1
        if geom.GetGeometryCount():
            _triangulate_ring(_ring_points(geom.GetGeometryRef(0)), tris, counters)
        if not geom.Is3D():
            counters["no_z"] += 1
    elif geom.GetGeometryCount():           # Multi*, PolyhedralSurface, TIN, collections
        for i in range(geom.GetGeometryCount()):
            _collect_ogr(geom.GetGeometryRef(i), tris, counters)
    else:
        counters["skipped"] += 1           # points / lines


def _finish(tris, counters, name, source):
    mesh = Mesh.from_triangle_soup(np.array(tris, dtype=np.float64).reshape(-1, 3, 3), name, source)
    if counters.get("fanned"):
        mesh.warnings.append(f"{counters['fanned']} face(s) with >4 vertices fan-triangulated")
    if counters.get("holes"):
        mesh.warnings.append(f"{counters['holes']} polygon hole(s) ignored")
    if counters.get("no_z"):
        mesh.warnings.append(f"{counters['no_z']} face(s) had no Z (set to 0)")
    if counters.get("skipped"):
        mesh.warnings.append(f"{counters['skipped']} non-polygon geometries skipped")
    return mesh


def _new_counters():
    return {"fanned": 0, "holes": 0, "no_z": 0, "skipped": 0, "degenerate": 0}


@gdal_soft
def read_ogr(path, layer_filter=None):
    """Read every polygonal surface in an OGR dataset (DXF, GPKG, SHP...)."""
    if ogr is None:
        raise RuntimeError("GDAL/OGR python bindings not available")
    ds = ogr.Open(path)
    if ds is None:
        raise IOError(f"Cannot open {path}")
    tris, counters = [], _new_counters()
    for lyr in ds:
        lyr.ResetReading()
        for feat in lyr:
            if layer_filter and feat.GetFieldIndex("Layer") >= 0 and feat.GetField("Layer") not in layer_filter:
                continue
            _collect_ogr(feat.GetGeometryRef(), tris, counters)
    return _finish(tris, counters, os.path.splitext(os.path.basename(path))[0], path)


def read_obj(path):
    verts, faces = [], []
    with open(path, "r", errors="ignore") as fh:
        for line in fh:
            if line.startswith("v "):
                p = line.split()
                verts.append((float(p[1]), float(p[2]), float(p[3]) if len(p) > 3 else 0.0))
            elif line.startswith("f "):
                idx = []
                for tok in line.split()[1:]:
                    i = int(tok.split("/")[0])
                    idx.append(i - 1 if i > 0 else len(verts) + i)
                for k in range(1, len(idx) - 1):
                    faces.append((idx[0], idx[k], idx[k + 1]))
    v = np.asarray(verts, dtype=np.float64)
    tris = v[np.asarray(faces, dtype=np.int64)] if faces else np.zeros((0, 3, 3))
    return Mesh.from_triangle_soup(tris, os.path.splitext(os.path.basename(path))[0], path)


def read_file(path):
    low = path.lower()
    if low.endswith(".obj"):
        return read_obj(path)
    if low.endswith(".dm"):                 # Datamine wireframe: <name>pt.dm + <name>tr.dm
        from .dmfile import read_wireframe
        mesh, info = read_wireframe(path)
        if info.get("dropped"):
            mesh.warnings.append(f"{info['dropped']} triangle(s) referenced missing points and were dropped")
        return mesh
    if low.endswith(".msh"):                # Leapfrog mesh
        from .leapfrog import read_msh
        return read_msh(path)
    if low.endswith(".dtm"):                # Surpac DTM (+ the string file it names)
        from .surpac import read_dtm
        return read_dtm(path)[0]
    return read_ogr(path)


def read_qgis_layer(layer, dest_crs=None, selected_only=False):
    """Read PolygonZ / MultiPolygonZ / TIN / PolyhedralSurfaceZ features."""
    from qgis.core import QgsCoordinateTransform, QgsProject

    xform = None
    if dest_crs is not None and layer.crs().isValid() and layer.crs() != dest_crs:
        xform = QgsCoordinateTransform(layer.crs(), dest_crs, QgsProject.instance())

    feats = layer.getSelectedFeatures() if selected_only else layer.getFeatures()
    tris, counters = [], _new_counters()
    for f in feats:
        g = f.geometry()
        if g.isNull():
            continue
        if xform:
            g.transform(xform)
        og = ogr.CreateGeometryFromWkb(bytes(g.asWkb()))
        _collect_ogr(og, tris, counters)
    return _finish(tris, counters, layer.name(), layer.id())
