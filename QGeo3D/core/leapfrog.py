# -*- coding: utf-8 -*-
"""Leapfrog Geo exchange formats (read and write, no Leapfrog needed).

- .msh meshes (Leapfrog's own mesh export): a text header with an [index] of arrays
  ("Location Double 3 N;", "Tri Integer 3 M;") then the arrays back to back after [binary].
- Block model CSVs: "CSV with embedded header" (definition as '#' lines above the table) and
  "CSV + text header" (the same lines in <file>.csv.txt). Centroids X, Y, Z (world), sizes dX, dY,
  dZ for sub-blocked models. The header (base point, block size, size in blocks, azimuth / dip /
  pitch) is read when present; Leapfrog itself doesn't need it on import (it finds the grid from
  the centroids), so ours is written for people and for Leapfrog's grid prefill.
- Drillholes, points and meshes otherwise go through shared formats Leapfrog reads and writes:
  CSV tables, DXF / OBJ meshes, Datamine .dm, Surpac .str / .dtm / .mdl, and OMF (core/omf1.py).
"""

import os
import re

import numpy as np

_DT = {"integer": np.int32, "int": np.int32, "int32": np.int32, "long": np.int64, "int64": np.int64,
       "double": np.float64, "float64": np.float64, "float": np.float32, "float32": np.float32,
       "byte": np.uint8, "uint8": np.uint8}


def read_msh_arrays(path):
    """{name: array} from a Leapfrog .msh file."""
    with open(path, "rb") as f:
        head = f.read(65536)
        hb = head.find(b"[binary]")
        hi = head.find(b"[index]")
        if hb < 0 or hi < 0:
            raise ValueError("not a Leapfrog mesh (.msh): no [index] / [binary] sections")
        specs = []
        for line in head[hi + 7:hb].decode("utf-8", "replace").splitlines():
            line = line.strip().rstrip(";").strip()
            if not line:
                continue
            parts = line.split()
            name, dt, dims = parts[0], parts[1].lower(), [int(x) for x in parts[2:] if x.lstrip("-").isdigit()]
            if dt not in _DT:
                raise ValueError(f"unknown array type {parts[1]!r} in {os.path.basename(path)}")
            specs.append((name, _DT[dt], list(reversed(dims))))
        size = os.path.getsize(path)
        need = sum(int(np.prod(sh)) * np.dtype(dt).itemsize for _n, dt, sh in specs)
        # the arrays end the file; the 8-byte tag is followed by a few bytes of padding (12 in
        # the files seen), so take the start that makes the arrays fill the file exactly
        start = size - need if size - need >= hb + 8 else hb + 0x14
        f.seek(start)
        out = {}
        for name, dt, sh in specs:
            n = int(np.prod(sh))
            a = np.fromfile(f, dtype=np.dtype(dt).newbyteorder("<"), count=n)
            if len(a) != n:
                raise ValueError(f"{os.path.basename(path)} ends early (array {name})")
            out[name] = a.reshape(sh) if len(sh) > 1 else a
    return out


def read_msh(path):
    """Leapfrog .msh -> core.mesh.Mesh."""
    from .mesh import Mesh
    d = read_msh_arrays(path)
    key = {k.lower(): k for k in d}
    v = d[key.get("location") or key.get("vertices") or key.get("points")]
    t = d[key.get("tri") or key.get("triangles") or key.get("faces")]
    v = np.asarray(v, float).reshape(-1, 3)
    t = np.asarray(t, np.int64).reshape(-1, 3)
    if len(t) and (t.min() < 0 or t.max() >= len(v)):
        raise ValueError(f"{os.path.basename(path)}: triangle indices out of range")
    return Mesh(v, t, os.path.splitext(os.path.basename(path))[0], path)


def write_msh(path, vertices, triangles):
    """Leapfrog-style .msh (same layout as read_msh expects)."""
    from .q2thook import is_protected
    if is_protected(path):
        raise PermissionError(f"Ringfenced location, not writing: {path}")
    v = np.ascontiguousarray(vertices, "<f8").reshape(-1, 3)
    t = np.ascontiguousarray(triangles, "<i4").reshape(-1, 3)
    head = (f"%% Leapfrog mesh written by QGeo 3D\n[index]\nTri Integer 3 {len(t)};\n"
            f"Location Double 3 {len(v)};\n[binary]").encode("ascii")
    with open(path, "wb") as f:
        f.write(head + b"\x00" * 12)
        f.write(t.tobytes())
        f.write(v.tobytes())


# ------------------------------------------------------------ block model CSV
_NUM = r"[-+]?\d+(?:\.\d*)?(?:[eE][-+]?\d+)?"


def parse_bm_header(lines):
    """Grid definition from Leapfrog-style header lines ('Base point: 250, 2070, 1180',
    'Block size: 20 10 20', 'Size in blocks = 52, 29, 30', 'Azimuth: 310', 'Dip 0', 'Pitch: 0').
    Returns {origin, size, count, azimuth, dip, pitch, subblocks} with what was found."""
    out = {}
    keys = (("origin", ("base point", "origin", "minimum corner", "min corner", "model origin", "base")),
            ("size", ("parent block size", "block size", "parent size", "cell size")),
            ("count", ("size in blocks", "number of blocks", "block count", "dimensions", "n blocks")),
            ("subblocks", ("sub-blocking", "subblocking", "sub blocks", "sub-blocks", "subblock count")),
            ("boundary", ("boundary size", "extent", "extents")))
    for raw in lines:
        line = raw.strip().lstrip("#").strip()
        low = line.lower()
        for key, names in keys:
            if key in out:
                continue
            for nm in names:
                if low.startswith(nm):
                    nums = [float(x) for x in re.findall(_NUM, line[len(nm):])]
                    if len(nums) >= 3:
                        out[key] = nums[:3]
                    break
        for ang in ("azimuth", "dip", "pitch", "bearing", "plunge"):
            m = re.search(ang + r"\s*[:=]?\s*(" + _NUM + ")", low)
            if m and ang not in out:
                out[ang] = float(m.group(1))
        m = re.search(r"azimuth\s*/\s*dip\s*[:=]?\s*(" + _NUM + r")\D+(" + _NUM + ")", low)
        if m:
            out["azimuth"], out["dip"] = float(m.group(1)), float(m.group(2))
    if "count" in out:
        out["count"] = [int(round(c)) for c in out["count"]]
    if "count" not in out and "boundary" in out and "size" in out:
        out["count"] = [int(round(b / s)) for b, s in zip(out["boundary"], out["size"])]
    return out


def rot_z(azimuth):
    """Model -> world rotation for a model turned `azimuth` degrees clockwise (seen from above)."""
    a = np.radians(azimuth or 0.0)
    c, s = np.cos(a), np.sin(a)
    return np.array([[c, s, 0.0], [-s, c, 0.0], [0.0, 0.0, 1.0]])


def grid_fit(xyz_world, size, origin, R):
    """How well centroids sit on the block grid in model coordinates (mean distance from the nearest
    half-block position, as a fraction of the block size); small = this rotation is the right one."""
    m = (np.asarray(xyz_world, float) - origin) @ R
    sz = np.asarray(size, float)
    f = m / sz - 0.5
    return float(np.mean(np.abs(f - np.round(f))))


def place(xyz_world, size, hdr):
    """(model xyz, R, origin, note) for world centroids of a model with a header azimuth: the
    sign that puts centroids on the grid is used (exporters differ in the direction they quote)."""
    az = hdr.get("azimuth") or 0.0
    o = np.asarray(hdr.get("origin") or np.min(xyz_world, axis=0) - np.asarray(size) / 2, float)
    if abs(az) < 1e-9 or not np.isfinite(az):
        return None
    sz = np.asarray(size, float) if np.ndim(size) == 1 else np.median(np.asarray(size, float), axis=0)
    cands = [(grid_fit(xyz_world, sz, o, rot_z(a)), a) for a in (az, -az)]
    fit, a = min(cands)
    R = rot_z(a)
    note = f"rotated {az:g}° about Z (from the file header)"
    if hdr.get("dip") or hdr.get("pitch"):
        note += "; dip / pitch in the header are not applied"
    return (np.asarray(xyz_world, float) - o) @ R, R, o, note


def write_bm_csv(path, xyz, size, rows, fields, name="block model", origin=None, azimuth=0.0, crs_name=""):
    """Block model CSV with an embedded Leapfrog-style header. xyz: world centroids, size: (n, 3)."""
    import csv
    from .q2thook import is_protected
    if is_protected(path):
        raise PermissionError(f"Ringfenced location, not writing: {path}")
    xyz = np.asarray(xyz, float)
    size = np.asarray(size, float)
    regular = len(size) and np.allclose(size, size[0])
    # extents in model coordinates (the grid of a rotated model runs along its own axes)
    base0 = np.asarray(origin, float) if origin is not None else None
    if azimuth and base0 is not None:
        m = (xyz - base0) @ rot_z(azimuth)
    else:
        m = xyz
    lo = (m - size / 2).min(axis=0) if len(m) else np.zeros(3)
    hi = (m + size / 2).max(axis=0) if len(m) else np.zeros(3)
    base = base0 if base0 is not None else lo
    head = [f"# Block model: {name}", "# Written by QGeo 3D (CSV with embedded header)",
            f"# Base point: {base[0]:.3f}, {base[1]:.3f}, {base[2]:.3f}"]
    if regular:
        s = size[0]
        n = np.round((hi - lo) / s).astype(int)
        head += [f"# Block size: {s[0]:g}, {s[1]:g}, {s[2]:g}", f"# Size in blocks: {n[0]}, {n[1]}, {n[2]}"]
    else:
        ps = size.max(axis=0)
        head += [f"# Parent block size: {ps[0]:g}, {ps[1]:g}, {ps[2]:g}", "# Sub-blocked (sizes in dX, dY, dZ)"]
    head += [f"# Boundary size: {hi[0] - lo[0]:g}, {hi[1] - lo[1]:g}, {hi[2] - lo[2]:g}",
             f"# Azimuth: {azimuth:g}", "# Dip: 0", "# Pitch: 0", f"# Blocks: {len(xyz)}"]
    if crs_name:
        head.append(f"# Coordinate system: {crs_name}")
    names = [f[0] for f in fields if f[0].upper() not in ("X", "Y", "Z", "DX", "DY", "DZ")]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        fh.write("\n".join(head) + "\n")
        w = csv.writer(fh)
        w.writerow(["X", "Y", "Z", "dX", "dY", "dZ"] + names)
        for i in range(len(xyz)):
            r = rows[i] if rows else {}
            vals = []
            for n in names:
                v = r.get(n)
                vals.append("" if v is None or (isinstance(v, float) and v != v) else
                            (f"{v:.6g}" if isinstance(v, float) else v))
            w.writerow([f"{xyz[i, 0]:.3f}", f"{xyz[i, 1]:.3f}", f"{xyz[i, 2]:.3f}",
                        f"{size[i, 0]:g}", f"{size[i, 1]:g}", f"{size[i, 2]:g}"] + vals)
    return len(xyz)
