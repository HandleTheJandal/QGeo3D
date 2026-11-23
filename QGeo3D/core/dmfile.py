# -*- coding: utf-8 -*-
"""Datamine binary .dm reader (read only).

Format, as found in Studio RM / DmFile component files:
  * Pages of 512 words. Extended (double) precision: 8-byte words, 4096-byte pages;
    single precision: 4-byte words, 2048-byte pages. Text is 4 characters per word
    (in extended files the other 4 bytes are spaces).
  * Page 1 header (word index):
        0-1 file name, 2-3 database name, 4-13 description, 20-21 'SYSTEM',
        25 number of field entries, 26 last page number, 27 records on the last page,
        28.. field entries, 7 words each (68 on page 1, then 73 per further header page):
            name (2 words), type ('N'/'A'), stored word position (0 = implicit/constant),
            word number within the field (alpha fields span several words), unit, default.
  * Data pages hold floor(508 / words_per_record) records; the last 4 words are unused.
  * Absent numeric data is -1e30.
"""

import os
import struct

import numpy as np
from .compat import gdal_soft

ABSENT = -1.0e30
PAGE_WORDS = 512
DATA_WORDS = 508


class DmFile:

    def __init__(self, path):
        self.path = path
        with open(path, "rb") as f:
            self.raw = f.read()
        self._detect()
        self._header()

    def _detect(self):
        b = self.raw
        def sane(v):
            return v == v and 0 < v < 100000 and v == int(v)
        ext = struct.unpack_from("<d", b, 25 * 8)[0]
        sgl = struct.unpack_from("<f", b, 25 * 4)[0]
        if sane(ext):
            self.wsize, self.ftype = 8, "<f8"
        elif sane(sgl):
            self.wsize, self.ftype = 4, "<f4"
        else:
            raise ValueError(f"Not a Datamine .dm file: {self.path}")
        self.psize = PAGE_WORDS * self.wsize

    def _num(self, i):
        return struct.unpack_from("<d" if self.wsize == 8 else "<f", self.raw, i * self.wsize)[0]

    def _txt(self, i):
        o = i * self.wsize
        return self.raw[o:o + 4].decode("latin-1")

    def _header(self):
        t = self._txt
        self.description = "".join(t(i) for i in range(4, 14)).strip()
        nent = int(self._num(25))
        self.last_page = int(self._num(26))
        self.last_records = int(self._num(27))
        entries, w = [], 28
        for k in range(nent):
            if w < PAGE_WORDS and k == 68:                      # page 1 holds 68 entries
                w = PAGE_WORDS
            elif w >= PAGE_WORDS and w + 7 > (w // PAGE_WORDS + 1) * PAGE_WORDS:   # 73 per later page
                w = (w // PAGE_WORDS + 1) * PAGE_WORDS
            name = (t(w) + t(w + 1)).strip()
            typ = t(w + 2).strip()
            pos = int(round(self._num(w + 3)))
            wif = int(round(self._num(w + 4)))
            unit = t(w + 5).strip()
            dflt = self._num(w + 6) if typ == "N" else t(w + 6)
            entries.append((name, typ, pos, wif, unit, dflt))
            w += 7
        self.header_pages = w // PAGE_WORDS + (1 if w % PAGE_WORDS else 0)
        # merge multi-word alpha fields
        self.fields, seen = [], {}
        for name, typ, pos, wif, unit, dflt in entries:
            if typ == "A" and name in seen:
                seen[name]["words"].append(pos)
                continue
            fd = {"name": name, "type": typ, "words": [pos] if pos else [], "unit": unit,
                  "default": dflt, "implicit": pos == 0}
            self.fields.append(fd)
            seen[name] = fd
        self.nwords = max([w for f in self.fields for w in f["words"]] or [0])
        self.per_page = DATA_WORDS // self.nwords if self.nwords else 0
        ndata = self.last_page - self.header_pages
        self.nrecords = max(0, (ndata - 1) * self.per_page + self.last_records) if ndata > 0 else 0
        self.constants = {f["name"]: f["default"] for f in self.fields if f["implicit"]}

    def names(self):
        return [f["name"] for f in self.fields]

    def field(self, name):
        for f in self.fields:
            if f["name"].upper() == name.upper():
                return f
        return None

    def _words(self):
        """All data records as an array (nrecords, nwords) of raw words (as uint bytes view)."""
        if getattr(self, "_w", None) is None:
            start = self.header_pages * self.psize
            ndata = self.last_page - self.header_pages
            pages = np.frombuffer(self.raw, dtype=np.uint8, count=ndata * self.psize, offset=start)
            pages = pages.reshape(ndata, PAGE_WORDS * self.wsize)[:, :self.per_page * self.nwords * self.wsize]
            rec = pages.reshape(ndata * self.per_page, self.nwords * self.wsize)[:self.nrecords]
            self._w = np.ascontiguousarray(rec)
        return self._w

    def column(self, name):
        """numpy array: float64 (absent -> NaN) for numeric fields, str for alpha fields."""
        f = self.field(name)
        if f is None:
            raise KeyError(name)
        if f["implicit"]:
            if f["type"] == "N":
                return np.full(self.nrecords, np.nan if f["default"] <= ABSENT / 10 else f["default"])
            return np.full(self.nrecords, str(f["default"]).strip(), dtype=object)
        w = self._words()
        ws = self.wsize
        if f["type"] == "N":
            p = f["words"][0] - 1
            v = w[:, p * ws:(p + 1) * ws].copy().view(self.ftype).ravel().astype(np.float64)
            v[v <= ABSENT / 10] = np.nan
            return v
        parts = [w[:, (p - 1) * ws:(p - 1) * ws + 4] for p in f["words"]]
        txt = np.concatenate(parts, axis=1).view(f"S{4 * len(parts)}").ravel()
        return np.array([s.decode("latin-1").strip() for s in txt], dtype=object)

    def table(self, names=None):
        names = names or self.names()
        return {n: self.column(n) for n in names}


# ------------------------------------------------------------------ wireframes
def find_pair(path):
    """(points .dm, triangles .dm) for a Datamine wireframe given either file
    (<name>pt.dm / <name>tr.dm, also <name>_pt.dm / <name>_tr.dm)."""
    d, fn = os.path.split(path)
    stem, ext = os.path.splitext(fn)
    low = stem.lower()
    if low.endswith("pt"):
        return path, _sibling(d, stem[:-2], "tr", ext)
    if low.endswith("tr"):
        return _sibling(d, stem[:-2], "pt", ext), path
    raise ValueError("Datamine wireframes come as <name>pt.dm + <name>tr.dm")


def _sibling(d, base, suffix, ext):
    for cand in (base + suffix, base + suffix.upper()):
        p = os.path.join(d, cand + ext)
        if os.path.exists(p):
            return p
    raise FileNotFoundError(os.path.join(d, base + suffix + ext))


def kind(path):
    """'wireframe' | 'blockmodel' | 'drillholes' | 'points' | 'table' for a .dm file."""
    n = {x.upper() for x in DmFile(path).names()}
    if {"PID1", "PID2", "PID3"} <= n or ({"XP", "YP", "ZP", "PID"} <= n and len(n) <= 6):
        return "wireframe"
    if {"XC", "YC", "ZC"} <= n and ({"XINC", "YINC", "ZINC"} <= n):
        return "blockmodel"
    if "BHID" in n and {"X", "Y", "Z"} <= n:
        return "drillholes"
    if {"XPT", "YPT", "ZPT"} <= n or {"XP", "YP", "ZP"} <= n:
        return "points"
    return "table"


def read_wireframe(path):
    """Mesh from a Datamine wireframe pair. Returns (Mesh, info)."""
    from .mesh import Mesh
    ptp, trp = find_pair(path)
    pt, tr = DmFile(ptp), DmFile(trp)
    pid = pt.column("PID").astype(np.int64)
    xyz = np.column_stack([pt.column("XP"), pt.column("YP"), pt.column("ZP")])
    lut = np.full(int(pid.max()) + 1, -1, np.int64)
    lut[pid] = np.arange(len(pid))
    tri = np.column_stack([tr.column(c).astype(np.int64) for c in ("PID1", "PID2", "PID3")])
    tri = lut[tri]
    bad = (tri < 0).any(axis=1)
    tri = tri[~bad]
    name = os.path.splitext(os.path.basename(ptp))[0][:-2].rstrip("_")
    info = {"points": len(pid), "triangles": len(tri), "dropped": int(bad.sum())}
    if "SURFACE" in [n.upper() for n in tr.names()]:
        s = tr.column("SURFACE")
        info["surfaces"] = int(len(np.unique(s[~np.isnan(s)])))
    return Mesh(xyz, tri, name=name, source=ptp), info


# ------------------------------------------------------------------ drillholes
def read_drillholes(path):
    """Desurveyed Datamine drillholes -> list of (BHID, from, to, start xyz, end xyz, row dict).

    Uses X/Y/Z (sample midpoint) with A0/B0 (azimuth / dip, Datamine: dip positive down)
    and LENGTH; if the file is a point/string file (XP/YP/ZP with PTN) intervals are joined
    point to point within each BHID."""
    dm = DmFile(path)
    names = {n.upper(): n for n in dm.names()}
    T = dm.table()
    n = dm.nrecords
    out = []
    if {"X", "Y", "Z", "A0", "B0"} <= set(names) and ("LENGTH" in names or {"FROM", "TO"} <= set(names)):
        X, Y, Z = (T[names[k]] for k in ("X", "Y", "Z"))
        a = np.radians(T[names["A0"]])
        dip = np.radians(T[names["B0"]])
        # FROM/TO first: LENGTH can be a different length (e.g. composite) in some files
        L = T[names["TO"]] - T[names["FROM"]] if {"FROM", "TO"} <= set(names) else T[names["LENGTH"]]
        ux, uy, uz = np.sin(a) * np.cos(dip), np.cos(a) * np.cos(dip), -np.sin(dip)
        h = L / 2.0
        s = np.column_stack([X - ux * h, Y - uy * h, Z - uz * h])
        e = np.column_stack([X + ux * h, Y + uy * h, Z + uz * h])
    else:
        raise ValueError("Drillhole file needs X, Y, Z, A0, B0 and LENGTH (or FROM/TO)")
    keep = [k for k in dm.names() if k.upper() not in ("XP", "YP", "ZP", "PTN", "PVALUE")]
    bh = T.get(names.get("BHID", ""), np.array([""] * n, dtype=object))
    fr = T.get(names.get("FROM", ""), np.full(n, np.nan))
    to = T.get(names.get("TO", ""), np.full(n, np.nan))
    seen = set()
    for i in range(n):
        if not np.isfinite(s[i]).all() or not np.isfinite(e[i]).all():
            continue
        key = (bh[i], round(float(fr[i]), 4) if fr[i] == fr[i] else i, round(float(to[i]), 4) if to[i] == to[i] else i)
        if key in seen:             # string-style files repeat the last interval at the hole's end point
            continue
        seen.add(key)
        out.append((bh[i], fr[i], to[i], s[i], e[i], {k: T[k][i] for k in keep}))
    return out, dm


# ------------------------------------------------------------------ block models
def _axis_rot(axis, deg):
    """Datamine rotation: positive = clockwise looking down the axis towards the origin
    (verified for Z against a Studio RM model: 30 degrees turns the model X axis to bearing 120)."""
    t = np.radians(deg)
    c, s = np.cos(t), np.sin(t)
    if axis == 3:
        return np.array([[c, s, 0], [-s, c, 0], [0, 0, 1]])
    if axis == 1:
        return np.array([[1, 0, 0], [0, c, s], [0, -s, c]])
    return np.array([[c, 0, -s], [0, 1, 0], [s, 0, c]])


def datamine_angles(R):
    """(ANGLE1, ANGLE2, ANGLE3) about axes 3, 1, 3 (Datamine's default order) for a model -> world
    rotation matrix R, so that R = _axis_rot(3, a1) @ _axis_rot(1, a2) @ _axis_rot(3, a3)."""
    R = np.asarray(R, float)
    # _axis_rot(ax, a) is the right-hand rotation by -a: R = Rz(-a1) Rx(-a2) Rz(-a3) (ZXZ Euler)
    b = np.arccos(np.clip(R[2, 2], -1, 1))
    if abs(np.sin(b)) > 1e-9:
        al = np.arctan2(R[0, 2], -R[1, 2])
        ga = np.arctan2(R[2, 0], R[2, 1])
    else:                                       # rotation about Z only: put it all in ANGLE1
        al = np.arctan2(R[1, 0], R[0, 0]) if R[2, 2] > 0 else np.arctan2(-R[1, 0], R[0, 0])
        ga = 0.0
    a1, a2, a3 = (float(-np.degrees(x)) for x in (al, b, ga))
    if a2 < -1e-9:                              # same rotation, read more naturally: ANGLE2 >= 0
        a1, a2, a3 = a1 + 180.0, -a2, a3 + 180.0
    w = lambda x: round(((x + 180.0) % 360.0) - 180.0, 10) or 0.0
    return w(a1), w(a2), w(a3)


def read_blockmodel(path):
    """Datamine block model -> dict(name, fields, data, xyz (model coords), size, rot, origin, notes).

    Cell centres XC/YC/ZC and sizes XINC/YINC/ZINC per record (sub-cells allowed). Rotated
    models carry X0/Y0/Z0 and ANGLE1-3 about ROTAXIS1-3 as implicit fields; then
    world = (X0, Y0, Z0) + R . (XC, YC, ZC)."""
    dm = DmFile(path)
    names = {n.upper(): n for n in dm.names()}
    T = dm.table()
    xyz = np.column_stack([T[names[k]] for k in ("XC", "YC", "ZC")])
    size = np.column_stack([T[names[k]] for k in ("XINC", "YINC", "ZINC")])
    R, origin, notes = np.eye(3), np.zeros(3), []
    c = {k.upper(): v for k, v in dm.constants.items()}
    for k in ("X0", "Y0", "Z0", "ANGLE1", "ANGLE2", "ANGLE3", "ROTAXIS1", "ROTAXIS2", "ROTAXIS3"):
        if k not in c and k in names and len(T[names[k]]):      # written as ordinary (constant) fields
            c[k] = float(T[names[k]][0])
    angles = [(int(c.get(f"ROTAXIS{i}", 0) or 0), float(c.get(f"ANGLE{i}", 0) or 0)) for i in (1, 2, 3)]
    if any(abs(a) > 1e-9 for _ax, a in angles) or "X0" in c:
        for ax, a in angles:
            if abs(a) > 1e-9 and ax in (1, 2, 3):
                R = R @ _axis_rot(ax, a)
                if ax != 3:
                    notes.append(f"rotation of {a:g}° about axis {ax} applied (Datamine convention: clockwise "
                                 "looking down the axis, each rotation about the already rotated axes)")
        origin = np.array([float(c.get("X0", 0) or 0), float(c.get("Y0", 0) or 0), float(c.get("Z0", 0) or 0)])
    skip = {"XC", "YC", "ZC", "XINC", "YINC", "ZINC", "IJK", "XMORIG", "YMORIG", "ZMORIG", "NX", "NY", "NZ",
            "X0", "Y0", "Z0", "ANGLE1", "ANGLE2", "ANGLE3", "ROTAXIS1", "ROTAXIS2", "ROTAXIS3"}
    fields = [f["name"] for f in dm.fields if not f["implicit"] or f["name"].upper() not in skip]
    fields = [n for n in fields if n.upper() not in skip]
    data = {n: T[n] for n in fields}
    return {"name": os.path.splitext(os.path.basename(path))[0], "fields": fields, "data": data,
            "xyz": xyz, "size": size, "rot": R, "origin": origin, "notes": notes}


@gdal_soft
def write_drillholes_gpkg(path, out_path, epsg):
    """Datamine drillholes -> GeoPackage layer 'intervals' (LineStringZ, every field). Returns count."""
    from osgeo import ogr, osr
    from .q2thook import is_protected
    if is_protected(out_path):
        raise PermissionError(f"Ringfenced location, not writing: {out_path}")
    rows, dm = read_drillholes(path)
    if os.path.exists(out_path):
        os.remove(out_path)
    ds = ogr.GetDriverByName("GPKG").CreateDataSource(out_path)
    from .crs import make_srs
    srs = make_srs(epsg)      # EPSG code or a CRS
    lyr = ds.CreateLayer("intervals", srs, ogr.wkbLineString25D)
    names = list(rows[0][5].keys()) if rows else []
    types = {f["name"]: f["type"] for f in dm.fields}
    used = set()
    out_names = []
    for n in names:
        nn = n
        while nn.upper() in used:          # GeoPackage field names are case-insensitive
            nn += "_"
        used.add(nn.upper())
        out_names.append(nn)
        lyr.CreateField(ogr.FieldDefn(nn, ogr.OFTString if types.get(n) == "A" else ogr.OFTReal))
    defn = lyr.GetLayerDefn()
    ds.StartTransaction()
    for bh, fr, to, s, e, row in rows:
        f = ogr.Feature(defn)
        for n, nn in zip(names, out_names):
            v = row[n]
            if isinstance(v, str):
                if v:
                    f.SetField(nn, v)
            elif v == v:
                f.SetField(nn, float(v))
        g = ogr.Geometry(ogr.wkbLineString25D)
        g.AddPoint(*map(float, s))
        g.AddPoint(*map(float, e))
        f.SetGeometry(g)
        lyr.CreateFeature(f)
    ds.CommitTransaction()
    ds = None
    return len(rows)
