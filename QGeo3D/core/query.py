# -*- coding: utf-8 -*-
"""3D query: find the feature under a click in the Qgis2threejs view.

The JS side sends the click ray (world coords), the pixel size and the scene transform.
Line and point layers are tested here against the ray in world space (so thick lines,
drillhole intervals and strings can be picked within a pixel tolerance), restricted to the
active section. Surfaces (DEM, polygons, wireframes) come from the JS raycast hit.
"""

import html
import math

import numpy as np
from qgis.core import (Qgis, QgsCoordinateTransform, QgsGeometry, QgsPointXY, QgsProject, QgsRasterLayer,
                       QgsRectangle, QgsVectorLayer, QgsWkbTypes)


class SceneXf:
    """map <-> world transform used by Qgis2threejs vector layers (see q2t4.js mapToWorld)."""

    def __init__(self, sc):
        self.o = sc["origin"]
        self.be = sc["be"]
        self.zs = float(sc["zScale"])
        r = math.radians(self.be.get("rotation") or 0.0)
        self.c, self.s = math.cos(r), math.sin(r)

    def to_world(self, xyz):
        p = np.asarray(xyz, float)
        dx, dy = p[..., 0] - self.be["cx"], p[..., 1] - self.be["cy"]
        x = self.c * dx - self.s * dy + (self.be["cx"] - self.o["x"])
        y = self.s * dx + self.c * dy + (self.be["cy"] - self.o["y"])
        z = (p[..., 2] - self.o["z"]) * self.zs
        return np.stack([x, y, z], axis=-1)

    def to_map(self, w):
        w = np.asarray(w, float)
        x, y = w[..., 0] - (self.be["cx"] - self.o["x"]), w[..., 1] - (self.be["cy"] - self.o["y"])
        dx = self.c * x + self.s * y
        dy = -self.s * x + self.c * y
        return np.stack([dx + self.be["cx"], dy + self.be["cy"], w[..., 2] / self.zs + self.o["z"]], axis=-1)


# ------------------------------------------------------------------ geometry
def layer_segments(layer, dest_crs):
    """(fids, A (n,3), B (n,3)) for every line segment / point of a vector layer, map coords."""
    xf = None
    if layer.crs().isValid() and dest_crs.isValid() and layer.crs() != dest_crs:
        xf = QgsCoordinateTransform(layer.crs(), dest_crs, QgsProject.instance())
    fids, A, B = [], [], []
    gtype = layer.geometryType()
    for f in layer.getFeatures():
        g = f.geometry()
        if g.isNull():
            continue
        if xf:
            g = QgsGeometry(g)
            g.transform(xf)
        for part in g.constParts():
            pts = [(v.x(), v.y(), v.z() if v.is3D() and not math.isnan(v.z()) else 0.0) for v in part.vertices()]
            if gtype == Qgis.GeometryType.Point or len(pts) == 1:
                for p in pts:
                    fids.append(f.id()); A.append(p); B.append(p)
            else:
                for p, q in zip(pts[:-1], pts[1:]):
                    fids.append(f.id()); A.append(p); B.append(q)
    if not fids:
        return np.zeros(0, int), np.zeros((0, 3)), np.zeros((0, 3))
    return np.array(fids), np.array(A, float), np.array(B, float)


def clip_to_section(A, B, sec):
    """Clip segments (map coords) to the section slab. Returns (A', B', keep mask)."""
    if not sec or not sec.get("enabled", True):
        return A, B, np.ones(len(A), bool)
    a, b = np.array(sec["a"], float), np.array(sec["b"], float)
    u = (b - a) / np.linalg.norm(b - a)
    n = np.array([-u[1], u[0]])
    h = sec["width"] / 2.0
    t0, t1 = np.zeros(len(A)), np.ones(len(A))

    def clip(s0, s1, lo, hi):
        nonlocal t0, t1
        ds = s1 - s0
        with np.errstate(divide="ignore", invalid="ignore"):
            ta = (lo - s0) / ds
            tb = (hi - s0) / ds
        tmin = np.where(ds == 0, np.where((s0 >= lo) & (s0 <= hi), 0.0, np.inf), np.minimum(ta, tb))
        tmax = np.where(ds == 0, np.where((s0 >= lo) & (s0 <= hi), 1.0, -np.inf), np.maximum(ta, tb))
        t0 = np.maximum(t0, tmin)
        t1 = np.minimum(t1, tmax)

    clip((A[:, :2] - a) @ n, (B[:, :2] - a) @ n, -h, h)
    if sec.get("limitEnds"):
        L = np.linalg.norm(b - a)
        clip((A[:, :2] - a) @ u, (B[:, :2] - a) @ u, 0.0, L)
    keep = t0 <= t1
    D = B - A
    return A + D * t0[:, None], A + D * t1[:, None], keep


def ray_segments(o, d, A, B):
    """Closest approach between ray o + t d (|d| = 1, t >= 0) and segments A-B (world).
    Returns (dist, t_ray, s_seg)."""
    u = B - A
    w0 = A - o
    a = np.einsum("ij,ij->i", u, u)
    b = u @ d
    c = 1.0
    dd = w0 @ d
    e = np.einsum("ij,ij->i", u, w0)
    den = a * c - b * b
    with np.errstate(divide="ignore", invalid="ignore"):
        s = np.where(den > 1e-12, (b * dd - c * e) / den, 0.0)
    s = np.clip(np.nan_to_num(s), 0.0, 1.0)
    P = A + u * s[:, None]
    t = np.maximum((P - o) @ d, 0.0)
    Q = o + np.outer(t, d)
    # re-project onto segment for the clamped ray point
    with np.errstate(divide="ignore", invalid="ignore"):
        s2 = np.where(a > 1e-12, np.einsum("ij,ij->i", Q - A, u) / a, 0.0)
    s2 = np.clip(np.nan_to_num(s2), 0.0, 1.0)
    P = A + u * s2[:, None]
    t = np.maximum((P - o) @ d, 0.0)
    Q = o + np.outer(t, d)
    return np.linalg.norm(P - Q, axis=1), t, s2


# ------------------------------------------------------------------ query
def query(ev, candidates, section, dest_crs, tol_px=8):
    """ev: JS query event. candidates: [(label, QgsVectorLayer)] line/point layers to test.
    Returns dict {title, rows: [(k, v)], point: map xyz, highlight: [[x,y,z],...] or None}."""
    xf = SceneXf(ev["scene"])
    o, d = np.array(ev["o"], float), np.array(ev["d"], float)
    d /= np.linalg.norm(d)
    k = float(ev["k"])

    cands = []                       # (t, dist_px, label, layer, fid, world point)
    for label, lyr in candidates:
        fids, A, B = layer_segments(lyr, dest_crs)
        if not len(fids):
            continue
        A, B, keep = clip_to_section(A, B, section)
        if not keep.any():
            continue
        fids, A, B = fids[keep], A[keep], B[keep]
        Aw, Bw = xf.to_world(A), xf.to_world(B)
        dist, t, s = ray_segments(o, d, Aw, Bw)
        # world size of one pixel at each candidate (constant for an orthographic camera)
        pix = k * (np.ones_like(t) if ev.get("ortho") else np.maximum(t, 1e-6))
        dpx = dist / pix
        for j in np.where(dpx <= tol_px)[0]:
            P = Aw[j] + (Bw[j] - Aw[j]) * s[j]
            cands.append((float(t[j]), float(dpx[j]), float(pix[j]), label, lyr, int(fids[j]), P))

    best = None
    if cands:
        # front-most object, but among things at about the same depth (touching intervals,
        # parallel holes) take the one nearest the cursor
        tmin = min(c[0] for c in cands)
        near = [c for c in cands if c[0] <= tmin + max(3 * tol_px * c[2], 0.25 * xf.zs)]
        best = min(near, key=lambda c: c[1])

    hit = ev.get("hit")
    # channel samples sit on drive walls: a line up to ~1 m behind a surface still wins
    if hit is not None and (best is None or hit["t"] < best[0] - max(3 * tol_px * best[2], 1.0 * xf.zs)):
        return _surface_result(hit, xf, dest_crs, ev)
    if best is None:
        return None
    t, _dpx, _pix, label, lyr, fid, Pw = best
    f = lyr.getFeature(fid)
    rows = [(fld.name(), f[fld.name()]) for fld in lyr.fields()]
    pm = xf.to_map(Pw)
    g = f.geometry()
    if lyr.crs() != dest_crs:
        g = QgsGeometry(g)
        g.transform(QgsCoordinateTransform(lyr.crs(), dest_crs, QgsProject.instance()))
    hl = [[v.x(), v.y(), v.z() if v.is3D() and not math.isnan(v.z()) else 0.0] for v in g.vertices()]
    return {"title": f"{label}", "rows": rows, "point": [float(x) for x in pm], "highlight": hl,
            "layer_id": lyr.id(), "fid": fid}


def _surface_result(hit, xf, dest_crs, ev):
    pm = [float(x) for x in xf.to_map(np.array(hit["p"], float))]
    rows = []
    title = hit.get("name") or "Surface"
    lyr = QgsProject.instance().mapLayer(hit["qgisLayerId"]) if hit.get("qgisLayerId") else None
    if isinstance(lyr, QgsVectorLayer):
        title = lyr.name()
        tol = max(0.5, float(ev["k"]) * 6 * (1.0 if ev.get("ortho") else max(hit["t"], 1e-6)) / xf.zs)
        pt = QgsGeometry.fromPointXY(QgsPointXY(pm[0], pm[1]))
        src = pt
        if lyr.crs() != dest_crs:
            src = QgsGeometry(pt)
            src.transform(QgsCoordinateTransform(dest_crs, lyr.crs(), QgsProject.instance()))
        rect = src.boundingBox().buffered(tol)
        cand = list(lyr.getFeatures(rect))
        cand.sort(key=lambda f: f.geometry().distance(src))
        if cand:
            f = cand[0]
            rows = [(fld.name(), f[fld.name()]) for fld in lyr.fields()]
            return {"title": title, "rows": rows, "point": pm, "highlight": None,
                    "layer_id": lyr.id(), "fid": f.id()}
    elif isinstance(lyr, QgsRasterLayer):
        title = lyr.name()
        p = QgsPointXY(pm[0], pm[1])
        if lyr.crs() != dest_crs:
            p = QgsCoordinateTransform(dest_crs, lyr.crs(), QgsProject.instance()).transform(p)
        v, ok = lyr.dataProvider().sample(p, 1)
        rows = [("Band 1", v if ok else None)]
    for kk, vv in (hit.get("info") or {}).items():
        rows.append((kk, vv))
    return {"title": title, "rows": rows, "point": pm, "highlight": None}


def to_html(res):
    if res is None:
        return "<b>Nothing here</b><div class='q2t4-q-note'>No object within the section under the cursor.</div>"
    p = res["point"]
    out = [f"<div class='q2t4-q-title'>{html.escape(str(res['title']))}</div>",
           f"<div class='q2t4-q-xyz'>E {p[0]:,.2f} &nbsp; N {p[1]:,.2f} &nbsp; RL {p[2]:,.2f}</div><table>"]
    for k, v in res["rows"]:
        if v is None or (hasattr(v, "isNull") and v.isNull()) or v == "" or str(k).startswith("q2t4_") or k == "fid":
            continue
        if isinstance(v, float):
            v = f"{v:.2f}" if abs(v) >= 1e5 else (f"{v:.4f}".rstrip("0").rstrip(".") or "0")
        out.append(f"<tr><td>{html.escape(str(k))}</td><td>{html.escape(str(v))}</td></tr>")
    out.append("</table>")
    return "".join(out)
