# -*- coding: utf-8 -*-
"""Point clouds (LAS / LAZ / COPC) for the 3D view.

LAS 1.0 - 1.4, point formats 0 - 10, are read directly with numpy (memory-mapped, so a large file
is thinned without reading it all). LAZ and COPC (compressed) are first converted to a temporary
LAS with QGIS's own PDAL tools (Processing ▸ Point cloud conversion), which ship with QGIS.

The 3D view gets an overlay of coloured dots: by the file's RGB, elevation, intensity,
classification or any extra-bytes attribute (e.g. CDI conductivity)."""

import base64
import contextlib
import os
import struct
import tempfile

import numpy as np

# point record layouts: fields after X, Y, Z (int32 each)
_CORE = {
    # legacy formats 0-5
    0: [("intensity", "<u2"), ("bits", "u1"), ("cls_byte", "u1"), ("scan_angle", "i1"), ("user_data", "u1"),
        ("point_source", "<u2")],
}
_GPS = [("gps_time", "<f8")]
_RGB = [("red", "<u2"), ("green", "<u2"), ("blue", "<u2")]
_NIR = [("nir", "<u2")]
_WAVE = [("wp_index", "u1"), ("wp_offset", "<u8"), ("wp_size", "<u4"), ("wp_loc", "<f4"),
         ("xt", "<f4"), ("yt", "<f4"), ("zt", "<f4")]
_EXT = [("intensity", "<u2"), ("bits", "u1"), ("bits2", "u1"), ("classification", "u1"), ("user_data", "u1"),
        ("scan_angle_i16", "<i2"), ("point_source", "<u2"), ("gps_time", "<f8")]

# extra-bytes data types (LAS 1.4 table 24): 1..10
_EB_TYPES = {1: "u1", 2: "i1", 3: "<u2", 4: "<i2", 5: "<u4", 6: "<i4", 7: "<u8", 8: "<i8", 9: "<f4", 10: "<f8"}

# ASPRS classification colours (codes 0-18), the usual LAS viewer palette
CLASS_COLOURS = {0: (200, 200, 200), 1: (170, 170, 170), 2: (166, 118, 29), 3: (180, 230, 120),
                 4: (80, 190, 60), 5: (20, 120, 30), 6: (230, 80, 60), 7: (255, 0, 255), 8: (255, 255, 0),
                 9: (40, 120, 230), 10: (120, 80, 40), 11: (100, 100, 100), 12: (255, 180, 0),
                 13: (255, 220, 120), 14: (250, 250, 120), 15: (200, 120, 255), 16: (255, 140, 200),
                 17: (120, 200, 255), 18: (255, 60, 60)}

COMPRESSED = (".laz", ".copc")


def _dtype(fmt, rec_len):
    base = [("X", "<i4"), ("Y", "<i4"), ("Z", "<i4")]
    if fmt <= 5:
        f = base + _CORE[0]
        if fmt in (1, 3, 4, 5):
            f += _GPS
        if fmt in (2, 3, 5):
            f += _RGB
        if fmt in (4, 5):
            f += _WAVE
    elif fmt <= 10:
        f = base + _EXT
        if fmt in (7, 8, 10):
            f += _RGB
        if fmt in (8, 10):
            f += _NIR
        if fmt in (9, 10):
            f += _WAVE
    else:
        raise ValueError(f"LAS point format {fmt} is not supported")
    size = np.dtype(f).itemsize
    if rec_len < size:
        raise ValueError(f"LAS point records are {rec_len} bytes, format {fmt} needs {size}")
    return f, size


def read_header(path):
    """LAS public header + the VLRs that matter here (CRS, extra bytes)."""
    with open(path, "rb") as fh:
        h = fh.read(375)
        if h[:4] != b"LASF":
            raise ValueError("not a LAS file (no LASF signature)")
        major, minor = h[24], h[25]
        hdr_size = struct.unpack_from("<H", h, 94)[0]
        off_points = struct.unpack_from("<I", h, 96)[0]
        n_vlr = struct.unpack_from("<I", h, 100)[0]
        fmt = h[104] & 0x3F                       # high bits flag compression (LAZ)
        compressed = bool(h[104] & 0xC0)
        rec_len = struct.unpack_from("<H", h, 105)[0]
        count = struct.unpack_from("<I", h, 107)[0]
        if (major, minor) >= (1, 4) and len(h) >= 255:
            count = struct.unpack_from("<Q", h, 247)[0] or count
        scale = struct.unpack_from("<3d", h, 131)
        offset = struct.unpack_from("<3d", h, 155)
        mx, mnx, my, mny, mz, mnz = struct.unpack_from("<6d", h, 179)
        out = {"version": f"{major}.{minor}", "format": fmt, "compressed": compressed, "record_length": rec_len,
               "count": count, "scale": scale, "offset": offset, "offset_to_points": off_points,
               "bounds": (mnx, mx, mny, my, mnz, mz), "wkt": None, "epsg": None, "extra": []}
        fh.seek(hdr_size)
        for _ in range(n_vlr):
            vh = fh.read(54)
            if len(vh) < 54:
                break
            user = vh[2:18].split(b"\0")[0].decode("ascii", "replace")
            rid = struct.unpack_from("<H", vh, 18)[0]
            ln = struct.unpack_from("<H", vh, 20)[0]
            body = fh.read(ln)
            if user == "LASF_Projection" and rid == 2112:
                out["wkt"] = body.split(b"\0")[0].decode("ascii", "replace")
            elif user == "LASF_Projection" and rid == 34735 and len(body) >= 8:
                keys = struct.unpack_from(f"<{len(body) // 2}H", body)
                for i in range(1, keys[3] + 1):
                    kid, loc, _cnt, val = keys[4 * i:4 * i + 4]
                    if loc == 0 and val not in (0, 32767):
                        if kid == 3072:                  # projected CRS wins
                            out["epsg"] = val
                        elif kid == 2048 and not out["epsg"]:
                            out["epsg"] = val
            elif user == "LASF_Spec" and rid == 4:
                for k in range(len(body) // 192):
                    d = body[k * 192:(k + 1) * 192]
                    dtype, options = d[2], d[3]
                    name = d[4:36].split(b"\0")[0].decode("ascii", "replace").strip() or f"extra{k + 1}"
                    sc = struct.unpack_from("<d", d, 112)[0] if options & 8 else 1.0
                    of = struct.unpack_from("<d", d, 136)[0] if options & 16 else 0.0
                    size = np.dtype(_EB_TYPES[dtype]).itemsize if dtype in _EB_TYPES else (
                        options if dtype == 0 else 0)
                    out["extra"].append({"name": name, "type": dtype, "size": size, "scale": sc, "offset": of})
    return out


def las_crs(path):
    """QgsCoordinateReferenceSystem recorded in a LAS file (WKT or GeoTIFF keys), else None."""
    from qgis.core import QgsCoordinateReferenceSystem
    with contextlib.suppress(Exception):
        h = read_header(path)
        c = None
        if h["wkt"]:
            c = QgsCoordinateReferenceSystem.fromWkt(h["wkt"])
        elif h["epsg"]:
            c = QgsCoordinateReferenceSystem(f"EPSG:{h['epsg']}")
        if c is not None and c.isValid():
            return c
    return None


def to_las(path):
    """A LAS copy of a LAZ / COPC file, made with QGIS's PDAL tools. Returns the temporary path."""
    import processing
    out = os.path.join(tempfile.mkdtemp(prefix="qgeo3d_pc_"), os.path.splitext(os.path.basename(path))[0]
                       .replace(".copc", "") + ".las")
    processing.run("pdal:convertformat", {"INPUT": path, "OUTPUT": out})
    if not os.path.exists(out):
        raise RuntimeError("QGIS's PDAL tools could not convert the file to LAS")
    return out


def read_las(path, max_points=1000000):
    """{'xyz': (n,3) float64, 'fields': {name: (n,) array}, 'total': all points, 'header': ...}.
    Points are thinned evenly through the file to max_points (None or 0: all of them)."""
    h = read_header(path)
    if h["compressed"]:
        raise ValueError("compressed (LAZ) points - convert first")
    f, size = _dtype(h["format"], h["record_length"])
    pos = size
    for e in h["extra"]:                          # extra bytes follow the standard fields, in order
        t = _EB_TYPES.get(e["type"])
        if t and pos + e["size"] <= h["record_length"]:
            f.append(("eb_" + e["name"], t))
        elif e["size"]:
            f.append((f"_pad{pos}", f"V{e['size']}"))
        pos += e["size"]
    if pos < h["record_length"]:
        f.append(("_rest", f"V{h['record_length'] - pos}"))
    dt = np.dtype(f)
    total = h["count"]
    avail = (os.path.getsize(path) - h["offset_to_points"]) // h["record_length"]
    total = min(total, avail) if total else avail
    rec = np.memmap(path, dtype=dt, mode="r", offset=h["offset_to_points"], shape=(total,))
    if max_points and total > max_points:
        idx = np.linspace(0, total - 1, int(max_points)).astype(np.int64)
        rec = rec[idx]
    else:
        rec = np.array(rec)
    sx, sy, sz = h["scale"]
    ox, oy, oz = h["offset"]
    xyz = np.column_stack([rec["X"] * sx + ox, rec["Y"] * sy + oy, rec["Z"] * sz + oz]).astype(np.float64)
    fields = {"Intensity": rec["intensity"].astype(np.float64)}
    if "classification" in rec.dtype.names:
        fields["Classification"] = rec["classification"].astype(np.int32)
    else:
        fields["Classification"] = (rec["cls_byte"] & 0x1F).astype(np.int32)
    if "red" in rec.dtype.names:
        rgb = np.column_stack([rec["red"], rec["green"], rec["blue"]]).astype(np.float64)
        if rgb.max(initial=0) > 255:              # 16-bit colours (the spec), else already 8-bit
            rgb = rgb / 257.0
        fields["RGB"] = np.clip(rgb, 0, 255).astype(np.uint8)
    for e in h["extra"]:
        k = "eb_" + e["name"]
        if k in rec.dtype.names:
            fields[e["name"]] = rec[k].astype(np.float64) * e["scale"] + e["offset"]
    del rec
    return {"xyz": xyz, "fields": fields, "total": int(total), "header": h}


def colour_options(fields):
    """What a cloud can be coloured by, best first: [(key, label)]."""
    opts = []
    if "RGB" in fields:
        opts.append(("RGB", "Colours in the file (RGB)"))
    opts.append(("Z", "Elevation"))
    for k, v in fields.items():
        if k in ("RGB", "Classification") or not len(v):
            continue
        if np.nanmax(v) != np.nanmin(v):
            opts.append((k, k))
    cl = fields.get("Classification")
    if cl is not None and len(cl) and cl.max() != cl.min():
        opts.append(("Classification", "Classification"))
    return opts


def ramp(t):
    """0..1 -> blue / cyan / green / yellow / red (the usual geophysics rainbow), uint8 (n,3)."""
    t = np.clip(np.nan_to_num(t, nan=0.0), 0, 1)
    stops = np.array([0, 0.25, 0.5, 0.75, 1.0])
    cols = np.array([[0, 40, 255], [0, 210, 255], [40, 220, 60], [255, 230, 0], [240, 20, 20]], float)
    out = np.empty((len(t), 3))
    for c in range(3):
        out[:, c] = np.interp(t, stops, cols[:, c])
    return out.astype(np.uint8)


def colours(cloud, by="RGB", log=False, clip=(2, 98)):
    """uint8 (n,3) colours, plus a legend dict {'label','lo','hi'} for ramps."""
    f = cloud["fields"]
    if by == "RGB" and "RGB" in f:
        return f["RGB"], None
    if by == "Classification":
        cl = f["Classification"]
        pal = np.array([CLASS_COLOURS.get(i, (180, 180, 180)) for i in range(256)], np.uint8)
        return pal[np.clip(cl, 0, 255)], {"label": "Classification", "classes": sorted(set(int(x) for x in np.unique(cl)))}
    v = cloud["xyz"][:, 2] if by == "Z" else f.get(by, cloud["xyz"][:, 2])
    v = np.asarray(v, float)
    if log:
        v = np.where(v > 0, np.log10(np.where(v > 0, v, 1)), np.nan)
    ok = np.isfinite(v)
    if not ok.any():
        return np.full((len(v), 3), 200, np.uint8), None
    lo, hi = np.percentile(v[ok], clip)
    if hi <= lo:
        lo, hi = float(v[ok].min()), float(v[ok].max()) or 1.0
    t = (v - lo) / ((hi - lo) or 1)
    return ramp(t), {"label": ("log10 " if log else "") + ("Elevation" if by == "Z" else by), "lo": float(lo),
                     "hi": float(hi)}


def _b64(a):
    return base64.b64encode(np.ascontiguousarray(a).tobytes()).decode("ascii")


def payload(xyz, rgb, point_size=2):
    """Overlay spec for Q2T4.setOverlay: a point cloud (no lines, no snapping)."""
    xyz = np.asarray(xyz, float).reshape(-1, 3)
    ok = np.isfinite(xyz).all(axis=1)
    xyz, rgb = xyz[ok], np.asarray(rgb, np.uint8).reshape(-1, 3)[ok]
    mn, mx = xyz.min(axis=0), xyz.max(axis=0)
    offset = np.round((mn + mx) / 2.0, 3)
    return {"cloud": True, "offset": [float(x) for x in offset], "pos": _b64((xyz - offset).astype("<f4")),
            "colors": _b64(rgb), "pointSize": float(point_size), "count": int(len(xyz))}
