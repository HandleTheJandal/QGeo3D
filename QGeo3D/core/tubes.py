# -*- coding: utf-8 -*-
"""Drillhole intervals as coloured tubes (hexagonal prisms) for the 3D view, and label specs.

Colour classes: Au-type grades use the assay classes (scaled for ppb / oz/t), other numbers six
quantile classes, codes / text one colour per value (up to 12, the rest grey)."""

import base64

import numpy as np

from . import fieldnames as FN
from .blockmodel import AU_CLASSES, CAT_PALETTE

NO_VALUE = "#6b6b6b"
RAMP = ["#2c7bb6", "#00a6ca", "#90eb9d", "#f9d057", "#f29e2e", "#d7191c"]


def _b64(a):
    return base64.b64encode(np.ascontiguousarray(a).tobytes()).decode("ascii")


def classes(name, values):
    """[(label, colour, test(v) -> bool)] and a function value -> colour."""
    vals = [v for v in values if v is not None and not (isinstance(v, float) and v != v)]
    num = vals and all(isinstance(v, (int, float)) for v in vals)
    info = FN.grade_info(name) if name else None
    if num and info and info["element"] == "AU":
        f = {"g/t": 1.0, "ppm": 1.0, "ppb": 1000.0, "oz/t": 1 / 34.2857}.get(info["unit"], 1.0)
        cl = [((None if lo is None else lo * f), (None if hi is None else hi * f), col, lab) for lo, hi, col, lab in AU_CLASSES]
    elif num and len(set(vals)) > 12:
        a = np.asarray(vals, float)
        qs = np.unique(np.quantile(a, np.linspace(0, 1, 7)))
        cl = [(qs[i], qs[i + 1] if i < len(qs) - 2 else None, RAMP[min(i, 5)], f"{qs[i]:.3g} – {qs[i + 1]:.3g}")
              for i in range(len(qs) - 1)]
    else:
        cats = sorted(set(vals), key=lambda x: (str(type(x)), x))
        lut = {c: CAT_PALETTE[i % len(CAT_PALETTE)] if i < 12 else NO_VALUE for i, c in enumerate(cats)}
        legend = [(f"{c:g}" if isinstance(c, float) else str(c), lut[c]) for c in cats[:12]]
        return legend, (lambda v: lut.get(v, NO_VALUE))

    def colour(v):
        if v is None or (isinstance(v, float) and v != v) or not isinstance(v, (int, float)):
            return NO_VALUE
        for lo, hi, col, _l in cl:
            if (lo is None or v >= lo) and (hi is None or v < hi):
                return col
        return cl[-1][2]
    return [(lab, col) for _lo, _hi, col, lab in cl], colour


def tube_spec(segments, colours, radius=0.6, sides=6, max_verts=65000):
    """segments: (n, 2, 3) map coords; colours: hex per segment. -> spec for Q2T4.setTubes."""
    seg = np.asarray(segments, float).reshape(-1, 2, 3)
    if not len(seg):
        return None
    offset = np.round((seg.reshape(-1, 3).min(axis=0) + seg.reshape(-1, 3).max(axis=0)) / 2, 3)
    rgb = np.array([[int(c[i:i + 2], 16) for i in (1, 3, 5)] for c in colours], np.uint8)
    ang = np.linspace(0, 2 * np.pi, sides, endpoint=False)
    per = 2 * sides                              # vertices per tube (no caps: they're open-ended pipes)
    n_chunk = max_verts // per
    chunks = []
    for s0 in range(0, len(seg), n_chunk):
        S = seg[s0:s0 + n_chunk] - offset
        a, b = S[:, 0], S[:, 1]
        d = b - a
        L = np.linalg.norm(d, axis=1)
        ok = L > 1e-6
        d[ok] /= L[ok, None]
        d[~ok] = [0, 0, -1]
        ref = np.where(np.abs(d[:, 2:3]) < 0.9, np.array([[0, 0, 1.0]]), np.array([[1.0, 0, 0]]))
        u = np.cross(d, ref)
        u /= np.linalg.norm(u, axis=1)[:, None]
        v = np.cross(d, u)
        ring = np.cos(ang)[None, :, None] * u[:, None, :] + np.sin(ang)[None, :, None] * v[:, None, :]   # (m, sides, 3)
        P = np.concatenate([a[:, None, :] + radius * ring, b[:, None, :] + radius * ring], axis=1)   # (m, 2s, 3)
        N = np.concatenate([ring, ring], axis=1)
        m = len(S)
        base = (np.arange(m) * per)[:, None]
        k = np.arange(sides)
        k2 = (k + 1) % sides
        quads = np.stack([k, k2, sides + k2, k, sides + k2, sides + k], axis=1).ravel()   # 6 per side
        I = (base + quads[None, :]).ravel().astype(np.uint16)
        C = np.repeat(rgb[s0:s0 + n_chunk], per, axis=0)
        chunks.append({"pos": _b64(P.reshape(-1, 3).astype("<f4")), "nrm": _b64(N.reshape(-1, 3).astype("<f4")),
                       "col": _b64(C.astype("u1")), "idx": _b64(I.astype("<u2"))})
    return {"offset": [float(x) for x in offset], "chunks": chunks}


def layer_intervals(layer, dest_crs=None, field=None, hole_field=None):
    """[(p0, p1, value, hole)] from a LineStringZ layer (first and last vertex of each feature;
    multi-vertex traces are split into their segments)."""
    from qgis.core import QgsCoordinateTransform, QgsProject
    tr = None
    if dest_crs is not None and layer.crs() != dest_crs:
        tr = QgsCoordinateTransform(layer.crs(), dest_crs, QgsProject.instance())
    names = [f.name() for f in layer.fields()]
    if hole_field is None:
        from .tables import guess
        hole_field = guess([(n, "C") for n in names], "hole", numeric=False)
    out = []
    for f in layer.getFeatures():
        g = f.geometry()
        if g is None or g.isEmpty():
            continue
        if tr is not None:
            g.transform(tr)
        val = f[field] if field else None
        if hasattr(val, "isNull") and val.isNull():
            val = None
        hole = f[hole_field] if hole_field else None
        for part in g.constParts():
            pts = [(p.x(), p.y(), p.z() if p.is3D() else 0.0) for p in part.vertices()]
            for i in range(len(pts) - 1):
                out.append((pts[i], pts[i + 1], val, hole))
    return out, hole_field


def label_specs(intervals, field=None, fmt=None, max_grade_labels=4000):
    """(hole id labels at the top of each hole, grade labels at interval midpoints)."""
    tops = {}
    for p0, p1, _v, h in intervals:
        if h is None:
            continue
        for p in (p0, p1):
            if h not in tops or p[2] > tops[h][2]:
                tops[h] = p
    holes = [{"p": list(p), "text": str(h), "color": "#ffffff"} for h, p in tops.items()]
    grades = []
    if field:
        for p0, p1, v, _h in intervals:
            if isinstance(v, (int, float)) and v == v:
                grades.append({"p": [(a + b) / 2 for a, b in zip(p0, p1)],
                               "text": (fmt or "{:.2f}").format(v), "color": "#f2c12e"})
                if len(grades) >= max_grade_labels:
                    break
    return holes, grades
