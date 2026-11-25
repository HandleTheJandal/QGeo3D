# -*- coding: utf-8 -*-
"""Micromine files, read only: wireframe databases (.tridb), string files (.STR), and data files
(.DAT) detected as block model / strings / points / downhole intervals. Drillhole databases
(.DHDB) and block models are read by mmdat.py / blockmodel.py.

.tridb is SQLite: table GeneralInformation (ID, Name, Code, Title, Colour, extent, DTMPlane,
Volume, ...) and table Geometry (ID, Geometry BLOB). The blob is
int32 -1, int32 version, int32 GUID length, int32 x3, GUID text, then chunks of
[uint64 byte length][uint64 tag][data]: tags 1/2/3 = X/Y/Z float64 arrays, 4 = triangles
(3 x int32, 0-based). The database is opened read-only and immutable, so SQLite never writes
to it (no journal, no lock file); if that fails it is copied to a temp file first.
"""

import contextlib
import os
import re
import shutil
import sqlite3
import struct
import tempfile
from contextlib import contextmanager
from pathlib import Path

import numpy as np

from .mmdat import read_dat
from .compat import gdal_soft


# ------------------------------------------------------------------ .tridb
@contextmanager
def _tridb(path):
    tmp = None
    try:
        try:
            con = sqlite3.connect(Path(os.path.abspath(path)).as_uri() + "?mode=ro&immutable=1", uri=True)
            con.execute("select count(*) from GeneralInformation").fetchone()
        except sqlite3.Error:
            tmp = tempfile.mkdtemp(prefix="q2t4_tridb_")
            cp = os.path.join(tmp, "w.tridb")
            shutil.copy2(path, cp)
            con = sqlite3.connect(Path(cp).as_uri() + "?mode=ro&immutable=1", uri=True)
        yield con
    finally:
        with contextlib.suppress(Exception):
            con.close()
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


def colour_hex(c):
    """Micromine colour (Windows COLORREF 0x00BBGGRR) -> '#rrggbb'; None for 'default'."""
    try:
        c = int(c)
    except (TypeError, ValueError):
        return None
    if c < 0 or c > 0xFFFFFF:
        return None
    return "#%02x%02x%02x" % (c & 0xFF, (c >> 8) & 0xFF, (c >> 16) & 0xFF)


def list_tridb(path):
    """Wireframes in a .tridb: [{id, name, code, title, colour, is_dtm, extent, volume, validated,
    points, triangles}] (point/triangle counts read from the chunk headers, without loading)."""
    out = []
    with _tridb(path) as con:
        cur = con.execute("select * from GeneralInformation order by Name")
        names = [c[0] for c in cur.description]
        for r in cur.fetchall():
            d = dict(zip(names, r))
            hdr = con.execute("select substr(Geometry, 1, 400), length(Geometry) from Geometry where ID=?",
                              (d["ID"],)).fetchone()
            npts = ntri = None
            if hdr and hdr[0]:
                npts, ntri = _counts(bytes(hdr[0]), con, d["ID"])
            out.append({
                "id": d["ID"], "name": d.get("Name") or f"#{d['ID']}", "code": d.get("Code") or "",
                "title": d.get("Title") or "", "colour": colour_hex(d.get("Colour")),
                "is_dtm": bool(d.get("DTMPlane")), "validated": bool(d.get("Validated")),
                "volume": d.get("Volume"),
                "extent": [d.get(k) for k in ("XMinimum", "XMaximum", "YMinimum", "YMaximum", "ZMinimum", "ZMaximum")],
                "points": npts, "triangles": ntri, "bytes": hdr[1] if hdr else 0,
            })
    return out


def _counts(head, con, wid):
    """Point and triangle counts from the chunk headers (reads only small pieces of the blob)."""
    try:
        gl = struct.unpack_from("<i", head, 8)[0]
        o = 24 + gl
        cnt = {}
        for _ in range(8):
            piece = con.execute("select substr(Geometry, ?, 16) from Geometry where ID=?", (o + 1, wid)).fetchone()[0]
            if not piece or len(piece) < 16:
                break
            ln, tag = struct.unpack("<QQ", bytes(piece))
            cnt[tag] = ln
            o += 16 + ln
        return (cnt[1] // 8 if 1 in cnt else None), (cnt[4] // 12 if 4 in cnt else None)
    except Exception:
        return None, None


def parse_geometry(blob):
    b = memoryview(blob)
    gl = struct.unpack_from("<i", b, 8)[0]
    o = 24 + gl
    ch = {}
    while o + 16 <= len(b):
        ln, tag = struct.unpack_from("<QQ", b, o)
        o += 16
        if o + ln > len(b):
            raise ValueError(f"corrupt geometry chunk (tag {tag})")
        ch[tag] = (o, ln)
        o += ln
    for t in (1, 2, 3, 4):
        if t not in ch:
            raise ValueError(f"geometry has no chunk {t} (unsupported wireframe format)")
    xyz = [np.frombuffer(b, "<f8", ch[t][1] // 8, ch[t][0]) for t in (1, 2, 3)]
    if not (len(xyz[0]) == len(xyz[1]) == len(xyz[2])) or ch[4][1] % 12:
        raise ValueError("unexpected wireframe geometry layout")
    tri = np.frombuffer(b, "<i4", ch[4][1] // 4, ch[4][0]).reshape(-1, 3)
    return np.column_stack(xyz), tri.astype(np.int64)


def read_tridb(path, which=None):
    """Mesh(es) from a .tridb. which: wireframe name / id, a list of them, or None (all).
    Returns [(Mesh, info)]; Mesh.color is the Micromine colour (or None)."""
    from .mesh import Mesh
    items = list_tridb(path)
    if which is not None:
        keys = which if isinstance(which, (list, tuple, set)) else [which]
        keys = {str(k).lower() for k in keys}
        items = [w for w in items if w["name"].lower() in keys or str(w["id"]) in keys]
    out = []
    with _tridb(path) as con:
        for w in items:
            r = con.execute("select Geometry from Geometry where ID=?", (w["id"],)).fetchone()
            if not r or r[0] is None:
                continue
            v, t = parse_geometry(r[0])
            bad = (t < 0).any(axis=1) | (t >= len(v)).any(axis=1)
            m = Mesh(v.copy(), t[~bad], w["name"], f"{path}|{w['name']}")
            if bad.any():
                m.warnings.append(f"{int(bad.sum())} triangle(s) referenced missing points and were dropped")
            m.color = w["colour"]
            out.append((m, w))
    return out


# ------------------------------------------------------------ .DAT / .STR
def _norm(s):
    return re.sub(r"[^A-Z0-9]", "", s.upper())


_E = ("EAST", "EASTING", "X", "XCOORD", "MGAE", "UTME", "GDAE", "XM", "E")
_N = ("NORTH", "NORTHING", "Y", "YCOORD", "MGAN", "UTMN", "GDAN", "YM", "N")
_Z = ("RL", "Z", "ZCOORD", "ELEV", "ELEVATION", "ELEVM", "RLM", "HEIGHT", "ZM")


def coord_fields(fields):
    """(east, north, rl) field names of a Micromine file (rl may be None), or None. Exact names
    first (EAST / X / ...), then names starting with them ('East (UTM)', 'North_GDA94')."""
    names = [f[0] for f in fields if f[1] in ("R", "F", "L", "N")]
    nn = {_norm(n): n for n in names}

    def pick(cands, starts):
        for c in cands:
            if c in nn:
                return nn[c]
        for n in names:
            u = _norm(n)
            if any(u.startswith(s) for s in starts) and not u.startswith("_"):
                return n
        return None
    e = pick(_E, ("EAST", "MGAE", "GDAE", "UTME"))
    n = pick(_N, ("NORTH", "MGAN", "GDAN", "UTMN"))
    z = pick(_Z, ("RL", "ELEV"))
    if e is None or n is None or e == n:            # other conventions: X_MGA94, MGA_E, Easting (GDA2020) ...
        from .tables import guess
        e, n = guess(fields, "east"), guess(fields, "north")
        z = z or guess(fields, "rl")
    if e is None or n is None or e == n:
        return None
    return e, n, z


def _num(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).strip())
    except ValueError:
        return None


def dat_kind(path):
    """'blockmodel' | 'strings' | 'points' | 'intervals' | 'table' from the fields of a .DAT/.STR."""
    fields, _rows = read_dat_header(path)
    up = {_norm(f[0]) for f in fields}
    if block_cols(fields):
        return "blockmodel"
    cf = coord_fields(fields)
    if cf and ("JOIN" in up or "STRING" in up):
        return "strings"
    if cf:                      # includes .STR files without JOIN / STRING (converted point files)
        return "points"
    from .tables import guess
    if guess(fields, "from") and guess(fields, "to"):
        return "intervals"
    return "table"


def block_cols(fields):
    """{xc, yc, zc, dx, dy, dz: field name} when the fields hold block centres AND sizes
    (EAST/NORTH/RL + _EAST/_NORTH/_RL, X/Y/Z + _X/_Y/_Z, XC.. + XINC.., X_Centre + X_Size ...), else None."""
    from .tables import guess
    cols = {r: guess(fields, r) for r in ("xc", "yc", "zc", "dx", "dy", "dz")}
    if not all(cols.values()) or len(set(cols.values())) < 6:
        return None
    return cols


def has_block_sizes(fields):
    """True when the fields hold a block size along each axis (XINC/YINC/ZINC, _EAST/_NORTH/_RL ...) -
    point layers of block centres keep their centres in the geometry, so these alone are enough."""
    from .tables import guess
    s = [guess(fields, r) for r in ("dx", "dy", "dz")]
    return all(s) and len(set(s)) == 3


def read_dat_header(path):
    """Fields only (reads the whole file - Micromine headers are short, files usually small)."""
    return read_dat(path)


def _xyz(rows, idx):
    ie, inn, iz = idx
    out = np.full((len(rows), 3), np.nan)
    for k, r in enumerate(rows):
        out[k, 0] = _num(r[ie]) if _num(r[ie]) is not None else np.nan
        out[k, 1] = _num(r[inn]) if _num(r[inn]) is not None else np.nan
        out[k, 2] = (_num(r[iz]) if _num(r[iz]) is not None else np.nan) if iz is not None else 0.0
    return out


def read_points(path):
    """Points of a Micromine file: (xyz (n,3), fields, rows, coord field names). Rows without
    east/north are dropped; missing RL reads 0."""
    fields, rows = read_dat(path)
    cf = coord_fields(fields)
    if cf is None:
        raise ValueError("no east / north fields found")
    names = [f[0] for f in fields]
    idx = (names.index(cf[0]), names.index(cf[1]), names.index(cf[2]) if cf[2] else None)
    xyz = _xyz(rows, idx)
    ok = np.isfinite(xyz[:, 0]) & np.isfinite(xyz[:, 1])
    xyz[~np.isfinite(xyz[:, 2]), 2] = 0.0
    return xyz[ok], fields, [r for r, k in zip(rows, ok) if k], cf


def read_strings(path):
    """Micromine strings: consecutive records with the same JOIN value form one string (a change
    of JOIN, or a blank JOIN, starts a new one; without a JOIN field the STRING field is used, else
    the whole file is one string). Returns {fields, coord, strings: [{join, pts (n,3), closed,
    first: row of the first point}]}."""
    xyz, fields, rows, cf = read_points(path)
    names = [f[0] for f in fields]
    up = [_norm(n) for n in names]
    key = up.index("JOIN") if "JOIN" in up else (up.index("STRING") if "STRING" in up else None)
    strings, cur, cur_key = [], [], object()

    def flush():
        if cur:
            p = xyz[[i for i in cur]]
            closed = len(p) > 2 and np.allclose(p[0], p[-1], rtol=0, atol=1e-6)
            strings.append({"join": cur_key, "pts": p, "closed": bool(closed), "first": rows[cur[0]]})
    for i, r in enumerate(rows):
        k = (str(r[key]).strip() if key is not None and r[key] is not None else "") if key is not None else 0
        if key is not None and (k == "" or k != cur_key):
            flush()
            cur, cur_key = [], k
        cur.append(i)
    flush()
    return {"fields": fields, "coord": cf, "strings": strings}


def _ogr_fields(lyr, fields, skip):
    from osgeo import ogr
    out = []
    used = set()
    for name, t, _w, _d in fields:
        if name in skip:
            continue
        safe = re.sub(r"[^A-Za-z0-9_]", "_", name).strip("_") or "field"
        if safe.lower() in ("fid", "geom", "geometry"):      # reserved in GeoPackages
            safe += "_src"
        base, k = safe, 2
        while safe.lower() in used:
            safe, k = f"{base}_{k}", k + 1
        used.add(safe.lower())
        ot = ogr.OFTReal if t in ("R", "F") else ogr.OFTInteger64 if t == "L" else ogr.OFTString
        lyr.CreateField(ogr.FieldDefn(safe, ot))
        out.append((name, safe, ot))
    return out


def _set(f, ot, safe, v):
    from osgeo import ogr
    if v is None:
        return
    if ot == ogr.OFTString:
        f.SetField(safe, str(v))
    else:
        x = _num(v)
        if x is not None and x == x:
            f.SetField(safe, int(x) if ot == ogr.OFTInteger64 else x)


@gdal_soft
def _new_gpkg(out_path, layer, crs, gtype):
    from osgeo import ogr
    from .q2thook import is_protected
    from .crs import make_srs
    if is_protected(out_path):
        raise PermissionError(f"Ringfenced location, not writing: {out_path}")
    if os.path.exists(out_path):
        os.remove(out_path)
    ds = ogr.GetDriverByName("GPKG").CreateDataSource(out_path)
    return ds, ds.CreateLayer(layer, make_srs(crs), gtype)


def write_strings_gpkg(path, out_path, crs):
    """Micromine strings -> GeoPackage layer 'strings' (LineStringZ: join, points, closed, then the
    other fields of each string's first point). Returns (count, polylines in the file's CRS)."""
    from osgeo import ogr
    s = read_strings(path)
    ds, lyr = _new_gpkg(out_path, "strings", crs, ogr.wkbLineString25D)
    for name, t in (("join", ogr.OFTString), ("points", ogr.OFTInteger), ("closed", ogr.OFTInteger)):
        lyr.CreateField(ogr.FieldDefn(name, t))
    skip = {c for c in s["coord"] if c} | {f[0] for f in s["fields"] if _norm(f[0]) == "JOIN"}
    fmap = _ogr_fields(lyr, s["fields"], skip)
    names = [f[0] for f in s["fields"]]
    defn = lyr.GetLayerDefn()
    polys = []
    ds.StartTransaction()
    for st in s["strings"]:
        p = st["pts"]
        if len(p) < 2:
            continue
        f = ogr.Feature(defn)
        f.SetField("join", str(st["join"]))
        f.SetField("points", int(len(p)))
        f.SetField("closed", int(st["closed"]))
        for name, safe, ot in fmap:
            _set(f, ot, safe, st["first"][names.index(name)])
        g = ogr.Geometry(ogr.wkbLineString25D)
        for x, y, z in p:
            g.AddPoint(float(x), float(y), float(z))
        f.SetGeometry(g)
        lyr.CreateFeature(f)
        polys.append(p)
    ds.CommitTransaction()
    ds = None
    return len(polys), polys


def write_points_gpkg(path, out_path, crs):
    """Micromine points -> GeoPackage layer 'points' (PointZ, every field). Returns (count, xyz)."""
    from osgeo import ogr
    xyz, fields, rows, cf = read_points(path)
    ds, lyr = _new_gpkg(out_path, "points", crs, ogr.wkbPoint25D)
    fmap = _ogr_fields(lyr, fields, set())
    names = [f[0] for f in fields]
    defn = lyr.GetLayerDefn()
    ds.StartTransaction()
    for (x, y, z), r in zip(xyz, rows):
        f = ogr.Feature(defn)
        for name, safe, ot in fmap:
            _set(f, ot, safe, r[names.index(name)])
        g = ogr.Geometry(ogr.wkbPoint25D)
        g.AddPoint(float(x), float(y), float(z))
        f.SetGeometry(g)
        lyr.CreateFeature(f)
    ds.CommitTransaction()
    ds = None
    return len(xyz), xyz
