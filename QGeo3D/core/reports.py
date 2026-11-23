# -*- coding: utf-8 -*-
"""Grade-tonnage and wireframe reports for block models (pure numpy).

inside_mesh: block centres inside a closed triangulated surface (ray cast upward, parity of the
crossings; robust to the ray grazing an edge by jittering it a hair).
grade_tonnage: tonnes, grade and contained metal above each cut-off, optionally per resource
category, for any subset of blocks.
"""

import numpy as np

from . import fieldnames as FN


def inside_mesh(points, vertices, triangles, chunk=400000):
    """Boolean mask: points (n, 3) inside the closed mesh. Counts crossings of the triangles with a
    vertical ray from each point upwards; odd = inside."""
    P = np.asarray(points, float)
    V = np.asarray(vertices, float)
    T = np.asarray(triangles, np.int64)
    if not len(P) or not len(T):
        return np.zeros(len(P), bool)
    # a tiny fixed offset keeps rays off shared edges / vertices of a regular grid of centres
    jit = np.array([1.37e-4, 2.71e-4])
    order = np.argsort(P[:, 0])
    xs = P[order, 0] + jit[0]
    ys = P[order, 1] + jit[1]
    zs = P[order, 2]
    count = np.zeros(len(P), np.int32)
    a, b, c = V[T[:, 0]], V[T[:, 1]], V[T[:, 2]]
    xmin = np.minimum(np.minimum(a[:, 0], b[:, 0]), c[:, 0])
    xmax = np.maximum(np.maximum(a[:, 0], b[:, 0]), c[:, 0])
    ymin = np.minimum(np.minimum(a[:, 1], b[:, 1]), c[:, 1])
    ymax = np.maximum(np.maximum(a[:, 1], b[:, 1]), c[:, 1])
    i0s = np.searchsorted(xs, xmin, "left")
    i1s = np.searchsorted(xs, xmax, "right")
    for t in range(len(T)):
        i0, i1 = i0s[t], i1s[t]
        if i1 <= i0:
            continue
        sel = np.arange(i0, i1)
        y = ys[sel]
        m = (y >= ymin[t]) & (y <= ymax[t])
        if not m.any():
            continue
        sel = sel[m]
        x, y, z = xs[sel], ys[sel], zs[sel]
        (ax, ay, az), (bx, by, bz), (cx, cy, cz) = a[t], b[t], c[t]
        d = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
        if abs(d) < 1e-12:
            continue                               # vertical triangle: the ray can't cross it
        l1 = ((by - cy) * (x - cx) + (cx - bx) * (y - cy)) / d
        l2 = ((cy - ay) * (x - cx) + (ax - cx) * (y - cy)) / d
        l3 = 1.0 - l1 - l2
        hit = (l1 >= 0) & (l2 >= 0) & (l3 >= 0)
        if not hit.any():
            continue
        zt = l1 * az + l2 * bz + l3 * cz
        hit &= zt > z
        count[sel[hit]] += 1
    out = np.zeros(len(P), bool)
    out[order] = (count % 2) == 1
    return out


def inside_slab(world_xyz, section):
    """Centres within the section slab (half width either side of the section plane)."""
    import math
    (ax, ay), (bx, by) = section.a, section.b
    L = math.hypot(bx - ax, by - ay) or 1.0
    nx, ny = -(by - ay) / L, (bx - ax) / L
    d = (world_xyz[:, 0] - ax) * nx + (world_xyz[:, 1] - ay) * ny
    return np.abs(d) <= section.width / 2.0


def grade_tonnage(bm, idx, grade_field, cutoffs, by_category=True):
    """Rows [{cutoff, category, blocks, tonnes, grade, metal, metal_unit, volume}] for blocks idx
    at or above each cut-off. Categories from the model's category field (with names), plus 'All'."""
    idx = np.asarray(idx, np.int64)
    t, how = bm.block_tonnes(idx)
    vol = np.prod(bm.size[idx], axis=1)
    g = np.asarray(bm.data[grade_field], float)[idx] if grade_field else np.zeros(len(idx))
    info = FN.grade_info(grade_field) if grade_field else None
    cats = [("All", np.ones(len(idx), bool))]
    cf = bm.categoryField if by_category else None
    if cf:
        cv = np.asarray(bm.data[cf], float)[idx]
        names = bm.category_labels(cf)
        for code in np.unique(cv[~np.isnan(cv)]):
            c = int(code) if float(code).is_integer() else code
            cats.append((f"{code:g} {names.get(c, '')}".strip(), cv == code))
    rows = []
    for co in cutoffs:
        above = np.nan_to_num(g, nan=-np.inf) >= co if grade_field else np.ones(len(idx), bool)
        for cname, cm in cats:
            m = above & cm
            r = {"cutoff": co, "category": cname, "blocks": int(m.sum()), "volume": float(vol[m].sum())}
            if t is not None:
                tt = t[m]
                ts = float(tt.sum())
                r["tonnes"] = ts
                if grade_field and ts > 0:
                    gg = np.nan_to_num(g[m])
                    r["grade"] = float((gg * tt).sum() / ts)
                    if info:
                        r["metal"], r["metal_unit"] = FN.contained(float((gg * tt).sum()), info)
            rows.append(r)
    return rows, {"tonnes_from": how, "grade_unit": info["unit"] if info else "", "category_field": cf}
