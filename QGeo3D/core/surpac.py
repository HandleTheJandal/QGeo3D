# -*- coding: utf-8 -*-
"""Surpac string (.str) and DTM (.dtm) files - read only.

String file (text):
    line 1  header: name, date, purpose, memo
    line 2  axis record: 0, y1, x1, z1, y2, x2, z2
    then    string_no, Y, X, Z[, d1, d2, ...]   one point per line (note: northing first)
            0, 0, 0, 0[, ...]                    ends a segment (several segments may share a number)
            0, 0, 0, 0, END                      ends the file
DTM file (text): line 1 = "<string file>,<ssi>;algorithm=...", optional string-1 spot records,
    then OBJECT, n / TRISOLATION, n, ... blocks of
            triangle_no, p1, p2, p3, n1, n2, n3  (n = neighbouring triangles, 0 = none)
    p1-p3 are record numbers in the string file counted from the first record after the
    axis line, separator records included (checked on a real DTM: every one of 77,482
    triangles lands on real points and all wind the same way; without the separators
    3 triangles flip).
"""

import os

import numpy as np
from .compat import gdal_soft


def _num(s):
    try:
        return float(s)
    except ValueError:
        return np.nan


def read_str(path):
    """-> dict(header, records (n,3) X/Y/Z with NaN for separators, strings=[...]).

    strings: list of dict(string, pts (m,3) X/Y/Z, desc [list of str per point], closed)."""
    header, records, strings = "", [], []
    cur, cur_desc, cur_no = [], [], None

    def flush():
        nonlocal cur, cur_desc, cur_no
        if len(cur) >= 1:
            p = np.array(cur)
            closed = len(p) >= 4 and np.allclose(p[0], p[-1], rtol=0, atol=1e-6)
            strings.append({"string": cur_no, "pts": p, "desc": cur_desc, "closed": bool(closed)})
        cur, cur_desc, cur_no = [], [], None

    with open(path, "r", encoding="latin-1") as f:
        for ln, line in enumerate(f):
            if ln == 0:
                header = line.strip()
                continue
            if ln == 1:
                continue                                # axis record
            parts = [p.strip() for p in line.rstrip("\r\n").split(",")]
            if not parts or parts == [""]:
                continue
            try:
                no = int(float(parts[0]))
            except ValueError:
                continue
            if no == 0:
                flush()
                if any(p.upper() == "END" for p in parts):
                    break
                records.append((np.nan, np.nan, np.nan))
                continue
            y, x, z = (_num(parts[i]) if i < len(parts) else np.nan for i in (1, 2, 3))
            if cur_no is not None and no != cur_no:
                flush()
            cur_no = no
            cur.append((x, y, z))
            cur_desc.append([p for p in parts[4:] if p != ""])
            records.append((x, y, z))
    flush()
    return {"header": header, "records": np.array(records, float).reshape(-1, 3), "strings": strings}


def read_dtm(path):
    """Mesh from a Surpac DTM and the string file it names. Returns (Mesh, info)."""
    from .mesh import Mesh
    with open(path, "r", encoding="latin-1") as f:
        lines = f.read().splitlines()
    head = lines[0].split(",")[0].strip()
    spath = os.path.join(os.path.dirname(path), head)
    if not os.path.exists(spath):
        alt = os.path.splitext(path)[0] + ".str"
        if os.path.exists(alt):
            spath = alt
        else:
            raise FileNotFoundError(f"The DTM's string file isn't next to it: {head}")
    tris, objects, in_tri = [], 0, False
    for line in lines[1:]:
        u = line.strip().upper()
        if u.startswith("OBJECT"):
            objects += 1
            continue
        if u.startswith("TRISOLATION"):
            in_tri = True
            continue
        if u.startswith("END"):
            break
        if not in_tri:
            continue
        parts = line.split(",")
        if len(parts) >= 4:
            try:
                tris.append((int(parts[1]), int(parts[2]), int(parts[3])))
            except ValueError:
                pass
    tri = np.array(tris, np.int64).reshape(-1, 3)
    rec = read_str(spath)["records"]
    if len(tri) == 0:
        raise ValueError("No triangles in DTM")
    if tri.max() > len(rec) or tri.min() < 1:
        raise ValueError("DTM point numbers don't fit its string file (was the string file edited?)")
    tri = tri - 1
    used = np.unique(tri)
    V = rec[used]
    bad_pts = ~np.isfinite(V).all(axis=1)
    remap = np.full(len(rec), -1, np.int64)
    remap[used] = np.arange(len(used))
    tri = remap[tri]
    bad = bad_pts[tri].any(axis=1)
    tri = tri[~bad]
    V = np.nan_to_num(V)
    name = os.path.splitext(os.path.basename(path))[0]
    mesh = Mesh(V, tri, name=name, source=path)
    if bad.any():
        mesh.warnings.append(f"{int(bad.sum())} triangle(s) referenced separator records and were dropped")
    return mesh, {"triangles": len(tri), "objects": objects, "string_file": spath}


@gdal_soft
def write_strings_gpkg(path, out_path, epsg):
    """Surpac strings -> GeoPackage layer 'strings' (LineStringZ; string, segment, closed, d1..dn,
    first point's descriptions). Returns (count, polylines)."""
    from osgeo import ogr, osr
    from .q2thook import is_protected
    if is_protected(out_path):
        raise PermissionError(f"Ringfenced location, not writing: {out_path}")
    s = read_str(path)
    if os.path.exists(out_path):
        os.remove(out_path)
    ds = ogr.GetDriverByName("GPKG").CreateDataSource(out_path)
    from .crs import make_srs
    srs = make_srs(epsg)      # EPSG code or a CRS
    lyr = ds.CreateLayer("strings", srs, ogr.wkbLineString25D)
    nd = max([len(d) for st in s["strings"] for d in st["desc"][:1]] or [0])
    for name, t in (("string", ogr.OFTInteger), ("segment", ogr.OFTInteger), ("closed", ogr.OFTInteger),
                    ("points", ogr.OFTInteger)):
        lyr.CreateField(ogr.FieldDefn(name, t))
    for i in range(nd):
        lyr.CreateField(ogr.FieldDefn(f"d{i + 1}", ogr.OFTString))
    defn = lyr.GetLayerDefn()
    seg_no, polys = {}, []
    ds.StartTransaction()
    for st in s["strings"]:
        p = st["pts"]
        if len(p) < 2:
            continue
        seg_no[st["string"]] = seg_no.get(st["string"], 0) + 1
        f = ogr.Feature(defn)
        f.SetField("string", int(st["string"]))
        f.SetField("segment", seg_no[st["string"]])
        f.SetField("closed", int(st["closed"]))
        f.SetField("points", int(len(p)))
        for i, d in enumerate(st["desc"][0][:nd]):
            f.SetField(f"d{i + 1}", d)
        g = ogr.Geometry(ogr.wkbLineString25D)
        for x, y, z in p:
            g.AddPoint(float(x), float(y), float(z if z == z else 0.0))
        f.SetGeometry(g)
        lyr.CreateFeature(f)
        polys.append(p)
    ds.CommitTransaction()
    ds = None
    return len(polys), polys


# ------------------------------------------------------------------ block models (.mdl)
MDL_TYPES = {0x12: ">f4", 0x01: ">i4"}


def read_mdl(path):
    """Surpac block model (.mdl, 'Version 4.0') -> dict like dmfile.read_blockmodel.

    Worked out from a Surpac 6.3 model and checked: 3,067,207 blocks whose volumes add up
    to exactly the model extent (500 x 400 x 150 m) with none outside it.
      * Big-endian. Text lines '### Block Model ###', 'Version 4.0', then NUL-terminated
        name and description, int32 x3 (?, levels above the user block, maximum level),
        9 doubles: origin xyz, octree root size xyz (power-of-two user blocks), model extent xyz,
        int32 0, int32 attribute count, then per attribute: name, int32 0, int32 decimals,
        int32 type (0x12 float, 0x01 integer), 4-byte background value, description.
      * int32 0, then 56-byte block records: uint32 A, uint32 B, then one 4-byte value per
        attribute. Level = A & 0xF (block size = root / 2^level); octant digit for level l
        (1..9) = (A >> (2 + 3l)) & 7, level 10 digit = B & 7; digit bit 2 = x, bit 1 = y,
        bit 0 = z (upper half). Records end with ff ff ff e0 ff ff ff ff.
      * The audit trail after that is plain text with every byte inverted (except CR/LF).
    """
    import struct
    with open(path, "rb") as f:
        b = f.read()
    if not b.startswith(b"### Block Model ###"):
        raise ValueError("Not a Surpac block model (.mdl)")
    p = b.index(b"\n", b.index(b"\n") + 1) + 1          # after the Version line
    ver = b[b.index(b"\n") + 1:p].decode("latin-1").strip()

    def cstr():
        nonlocal p
        e = b.index(b"\0", p)
        s = b[p:e].decode("latin-1")
        p = e + 1
        return s

    def i32():
        nonlocal p
        v = struct.unpack_from(">i", b, p)[0]
        p += 4
        return v

    if b[p] == 0:
        p += 1
    name, desc = cstr(), cstr()
    _flag, _ulev, maxlev = i32(), i32(), i32()
    dbl = struct.unpack_from(">9d", b, p)
    p += 72
    origin, root, extent = np.array(dbl[0:3]), np.array(dbl[3:6]), np.array(dbl[6:9])
    _zero = i32()
    natt = i32()
    atts = []
    for _ in range(natt):
        an = cstr()
        _z, dec, typ = i32(), i32(), i32()
        if typ not in MDL_TYPES:
            raise ValueError(f"Unsupported attribute type {typ:#x} for '{an}'")
        bgv = struct.unpack_from(">f" if typ == 0x12 else ">i", b, p)[0]
        p += 4
        atts.append({"name": an, "decimals": dec, "type": MDL_TYPES[typ], "bgv": float(bgv), "desc": cstr()})
    p += 4                                               # int32 0
    rec = 8 + 4 * natt
    nmax = (len(b) - p) // rec
    raw = np.frombuffer(b, np.uint8, nmax * rec, p).reshape(nmax, rec)
    A = raw[:, 0:4].copy().view(">u4").ravel()
    end = np.where(A == 0xFFFFFFE0)[0]
    n = int(end[0]) if len(end) else nmax
    raw, A = raw[:n], A[:n].astype(np.int64)
    B = raw[:, 4:8].copy().view(">u4").ravel().astype(np.int64)
    L = A & 0xF
    if L.max() > max(maxlev, 10):
        raise ValueError("Unexpected block levels in .mdl")
    lo = np.zeros((n, 3), np.float64)
    for l in range(1, int(L.max()) + 1):
        d = ((A >> (2 + 3 * l)) & 7) if l < 10 else ((B >> (3 * (l - 10))) & 7)
        d = np.where(L >= l, d, 0)
        sz = root / 2.0 ** l
        lo[:, 0] += ((d >> 2) & 1) * sz[0]
        lo[:, 1] += ((d >> 1) & 1) * sz[1]
        lo[:, 2] += (d & 1) * sz[2]
    size = root[None, :] / (2.0 ** L[:, None])
    xyz = (lo + size / 2.0).astype(np.float32)
    data, fields, descr = {}, [], {}
    for k, at in enumerate(atts):
        col = raw[:, 8 + 4 * k:12 + 4 * k].copy().view(at["type"]).ravel().astype(np.float32)
        if at["bgv"] <= -99:                            # Surpac "no value" background
            col[col == at["bgv"]] = np.nan
        data[at["name"]] = col
        fields.append(at["name"])
        descr[at["name"]] = at["desc"]
    notes = []
    R = np.eye(3)
    # the rotation is only in the audit trail (model definition entries)
    trail = bytes(((~x) & 0xFF) if x not in (13, 10) else x for x in b[p + n * rec + 8:]).decode("latin-1", "replace")
    import re
    created = [blk for blk in re.split(r"\n(?=BLOCK MODEL|BM )", trail) if "xorg=" in blk]   # model definition entries
    ang = {}
    for key in ("bearing", "dip", "plunge"):
        m = re.findall(rf'\b{key}="([-0-9.]+)"', created[-1]) if created else []
        if m:
            ang[key] = float(m[-1])
    if ang and any(abs(ang.get(k, 0.0)) > 1e-9 for k in ("bearing", "dip", "plunge")):
        # Surpac's bearing is that of the model Y axis (0 = unrotated, as real unrotated models' audit trails show)
        from .rotations import bearing_plunge_dip
        R = bearing_plunge_dip(ang.get("bearing", 0.0) + 90.0, ang.get("plunge", 0.0), ang.get("dip", 0.0), "surpac")
        notes.append(f"rotated model (bearing {ang.get('bearing', 0):g}°, dip {ang.get('dip', 0):g}°, plunge "
                     f"{ang.get('plunge', 0):g}°) applied about the model origin - check the position against Surpac once")
    return {"name": os.path.splitext(os.path.basename(path))[0], "fields": fields, "data": data,
            "xyz": xyz, "size": size.astype(np.float32), "rot": R, "origin": origin, "notes": notes,
            "descriptions": descr, "info": {"version": ver, "description": desc, "extent": extent.tolist(),
                                             "root": root.tolist(), "blocks": n}}
