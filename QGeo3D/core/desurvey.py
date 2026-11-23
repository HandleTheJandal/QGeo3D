# -*- coding: utf-8 -*-
"""Drillhole desurvey (pure numpy).

Conventions (Micromine): azimuth in degrees clockwise from grid north, dip in degrees,
negative = down. Minimum curvature between survey stations; the first station is
extended to the collar and the last to total depth. Dip 0 (channels, horizontal holes)
is handled like any other direction.
"""

import numpy as np


def _dirvec(az, dip):
    a, d = np.radians(az), np.radians(dip)
    return np.array([np.sin(a) * np.cos(d), np.cos(a) * np.cos(d), np.sin(d)])


def stations(collar_xyz, surveys, total_depth=None):
    """surveys: list of (depth, azimuth, dip) sorted or not. Returns (depths (n,), xyz (n,3))
    at every survey depth plus 0 and total depth."""
    s = sorted((float(d), float(a), float(p)) for d, a, p in surveys if d is not None and a is not None and p is not None)
    if not s:
        raise ValueError("no surveys")
    if s[0][0] > 0:
        s.insert(0, (0.0, s[0][1], s[0][2]))
    if total_depth is not None and total_depth > s[-1][0]:
        s.append((float(total_depth), s[-1][1], s[-1][2]))
    depths = np.array([x[0] for x in s])
    xyz = np.zeros((len(s), 3))
    xyz[0] = collar_xyz
    out_d, out_p = [depths[0]], [xyz[0]]
    for i in range(1, len(s)):
        md = depths[i] - depths[i - 1]
        t1, t2 = _dirvec(s[i - 1][1], s[i - 1][2]), _dirvec(s[i][1], s[i][2])
        dl = np.arccos(np.clip(np.dot(t1, t2), -1.0, 1.0))
        rf = 1.0 if dl < 1e-9 else 2.0 / dl * np.tan(dl / 2.0)
        xyz[i] = xyz[i - 1] + md / 2.0 * (t1 + t2) * rf
        # points along the arc between stations, so intervals and traces follow the curve rather
        # than the chord (sparse surveys on a curving hole put the chord metres off the hole)
        if 1e-4 < dl < np.pi - 1e-3 and md > 0:
            n = int(min(max(np.ceil(md / 5.0), np.ceil(np.degrees(dl) / 2.0)), 200))
            for k in range(1, n):
                m = md * k / n
                out_d.append(depths[i - 1] + m)
                out_p.append(_arc_point(xyz[i - 1], t1, t2, dl, m / md, m))
        out_d.append(depths[i])
        out_p.append(xyz[i])
    return np.array(out_d), np.array(out_p)


def _arc_point(p1, t1, t2, dl, f, m):
    """Point m metres along the minimum-curvature arc from p1 (directions t1 -> t2, dogleg dl);
    f = m / segment length."""
    a = dl * f
    tm = (np.sin(dl - a) * t1 + np.sin(a) * t2) / np.sin(dl)         # slerp
    rf = 1.0 if a < 1e-9 else 2.0 / a * np.tan(a / 2.0)
    return p1 + m / 2.0 * (t1 + tm) * rf


def position(depths, xyz, d):
    """Interpolate a point at depth d along the station polyline."""
    d = min(max(d, depths[0]), depths[-1])
    k = int(np.searchsorted(depths, d, side="right") - 1)
    k = min(max(k, 0), len(depths) - 2)
    seg = depths[k + 1] - depths[k]
    t = 0.0 if seg <= 0 else (d - depths[k]) / seg
    return xyz[k] + t * (xyz[k + 1] - xyz[k])


def interval_path(depths, xyz, d0, d1):
    """Polyline from depth d0 to d1 including the stations in between."""
    pts = [position(depths, xyz, d0)]
    for d, p in zip(depths, xyz):
        if d0 < d < d1:
            pts.append(p)
    pts.append(position(depths, xyz, d1))
    return np.array(pts)
