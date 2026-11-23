# -*- coding: utf-8 -*-
"""Writers for mining-package formats, so GeoPackages / QGIS layers / 3D objects can go back out.

Data kinds (what an exporter receives, all coordinates already in the output CRS):
    wireframe   Mesh-like: .vertices (n,3) float, .triangles (m,3) int, .name
    strings     list of {"pts": (n,3) array, "closed": bool, "attrs": {field: value}}
    points      (xyz (n,3), fields [(name, type 'R'|'C'|'L')], rows [dict])
    blocks      (xyz centres (n,3), sizes (n,3), fields, rows)

Formats: Micromine (.tridb, .STR, .DAT), Datamine (.dm: pt/tr wireframe pair, strings, points,
block model), Surpac (.str, .dtm + .str), DXF (R12: 3DFACE / 3D POLYLINE / POINT), OBJ, CSV.
Every writer refuses ringfenced locations (q2thook.is_protected).
"""

import csv
import datetime
import math
import os
import re
import sqlite3
import struct
import uuid

import numpy as np

from .q2thook import is_protected


def _guard(path):
    if is_protected(path):
        raise PermissionError(f"Ringfenced location, not writing: {path}")
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)


def _num(v):
    if v is None:
        return None
    if isinstance(v, (int, float, np.integer, np.floating)):
        f = float(v)
        return None if f != f else f
    try:
        return float(str(v).strip())
    except ValueError:
        return None


# =============================================================== Micromine
MM_MAGIC = b"THIS IS MICROMINE EXTENDED DATA FILE!\x07\x1a\x01\r\n"
MM_XML = (b'<?xml version="1.0" encoding="utf-8"?><metadata version="1.0"><meta-object type="title" version="1.0" '
          b'time-stamp="67452301efcdab89" compulsory="true"><data title="" description=""/></meta-object><meta-object '
          b'type="encoding" version="1.0" time-stamp="67452301efcdab89" compulsory="true"><data codepage="UTF-8"/>'
          b'</meta-object></metadata>')
MM_NAN = struct.pack("<d", float("nan"))


def write_mm_dat(path, fields, rows):
    """Micromine extended data file (as Micromine 2026 writes it, UTF-8 codepage).
    fields: [(name, type 'R' | 'C' | 'N' | 'L', width, decimals)]; rows: lists of values."""
    _guard(path)
    out = [MM_MAGIC + MM_XML + b"\x00" * (4096 - len(MM_MAGIC) - len(MM_XML)),
           f"{len(fields):<4}VARIABLES\r\n".encode()]
    for n, t, w, d in fields:
        short = n.encode("utf-8")[:10].decode("utf-8", "ignore")
        ln = short.ljust(10).encode("utf-8") + t.encode() + str(w).rjust(3).encode() + str(d).rjust(3).encode()
        if len(n.encode("utf-8")) > 10:
            ln += ("|" + n).encode("utf-8")[:256].ljust(256)
        out.append(ln + b"\r\n")
    for r in rows:
        rec = []
        for (n, t, w, d), v in zip(fields, r):
            if t == "R":
                x = _num(v)
                rec.append(MM_NAN if x is None else struct.pack("<d", x))
            elif t == "L":
                x = _num(v)
                rec.append(struct.pack("<i", -2147483648 if x is None else int(round(x))))
            else:
                s = "" if v is None else (str(int(v)) if isinstance(v, float) and v.is_integer() else str(v))
                rec.append(s.encode("utf-8")[:w].ljust(w))
        out.append(b"".join(rec) + b"\r\n")
    with open(path, "wb") as f:
        f.write(b"".join(out))
    return len(rows)


def _mm_fields(fields, rows, skip=()):
    """Micromine field specs for attribute fields: numbers R 8, text C (widest value, 1-255)."""
    out = []
    for name, t in fields:
        if name in skip:
            continue
        if t in ("R", "L", "F", "N") and t != "C":
            out.append((name, "R", 8, 3))
        else:
            w = max([len(str(r.get(name) or "").encode("utf-8")) for r in rows] + [1])
            out.append((name, "C", min(255, w), 0))
    return out


def mm_points(path, xyz, fields, rows):
    attr = _mm_fields(fields, rows, skip={"EAST", "NORTH", "RL"})
    f = [("EAST", "R", 8, 3), ("NORTH", "R", 8, 3), ("RL", "R", 8, 3)] + attr
    return write_mm_dat(path, f, [[p[0], p[1], p[2]] + [r.get(a[0]) for a in attr] for p, r in zip(xyz, rows)])


def mm_strings(path, strings, fields=()):
    """Micromine string file: EAST NORTH RL JOIN (+ attributes per point). Closed strings repeat the
    first point; each string gets its own JOIN value."""
    rows_all = [s.get("attrs", {}) for s in strings]
    attr = _mm_fields(fields, rows_all, skip={"EAST", "NORTH", "RL", "JOIN"})
    f = [("EAST", "R", 8, 3), ("NORTH", "R", 8, 3), ("RL", "R", 8, 3), ("JOIN", "N", 12, 0)] + attr
    rows = []
    for k, s in enumerate(strings, 1):
        p = np.asarray(s["pts"], float)
        if s.get("closed") and len(p) > 2 and not np.allclose(p[0], p[-1], rtol=0, atol=1e-6):
            p = np.vstack([p, p[:1]])
        a = s.get("attrs", {})
        for q in p:
            rows.append([q[0], q[1], q[2], str(k)] + [a.get(x[0]) for x in attr])
    return write_mm_dat(path, f, rows)


def mm_blocks(path, xyz, size, fields, rows):
    attr = _mm_fields(fields, rows, skip={"EAST", "NORTH", "RL", "_EAST", "_NORTH", "_RL"})
    f = [("EAST", "R", 8, 3), ("NORTH", "R", 8, 3), ("RL", "R", 8, 3),
         ("_EAST", "R", 8, 3), ("_NORTH", "R", 8, 3), ("_RL", "R", 8, 3)] + attr
    return write_mm_dat(path, f, [list(p) + list(s) + [r.get(a[0]) for a in attr]
                                  for p, s, r in zip(xyz, size, rows)])


TRIDB_SCHEMA = [
    "CREATE TABLE DatabaseInformation (Version INTEGER, CreateDateTime INTEGER, OriginalProject TEXT)",
    "CREATE TABLE GeneralInformation (ID INTEGER, Name TEXT, Code TEXT, Title TEXT, Colour INTEGER, CreateDateTime "
    "INTEGER, CreateBy TEXT, CreateCompany TEXT, EditDateTime INTEGER, EditBy TEXT, EditCompany TEXT, MetaNotes TEXT, "
    "Accuracy INTEGER, XMinimum REAL, XMaximum REAL, YMinimum REAL, YMaximum REAL, ZMinimum REAL, ZMaximum REAL, "
    "DTMPlane INTEGER, Volume REAL, SurfaceArea REAL, Validated INTEGER, IsLocked INTEGER, LockBy TEXT, LockReason "
    "TEXT, LockDateTime INTEGER, PRIMARY KEY (ID), UNIQUE (NAME))",
    "CREATE TABLE Geometry (ID INTEGER, Geometry BLOB, PRIMARY KEY (ID))",
    "CREATE TABLE UserAttributeDefinition (ID INTEGER, Name TEXT, Type INTEGER, Precision INTEGER, DefaultValueText "
    "TEXT, DefaultValueInteger INTEGER, DefaultValueReal REAL, Properties, ProtoProperties, PRIMARY KEY (ID))",
    "CREATE TABLE UserAttributeValue (TriangulationID INTEGER, AttributeID INTEGER, ValueText TEXT, ValueInteger "
    "INTEGER, ValueReal REAL, PRIMARY KEY (TriangulationID, AttributeID), FOREIGN KEY (AttributeID) REFERENCES "
    "UserAttributeDefinition (ID) ON DELETE CASCADE)",
    "CREATE TABLE PointAttributeDefinition (ID INTEGER, Name TEXT, Type INTEGER, Precision INTEGER, DefaultValueText "
    "TEXT, DefaultValueInteger INTEGER, DefaultValueReal REAL, PRIMARY KEY (ID))",
    "CREATE TABLE PointAttributeValue (TriangulationID INTEGER, AttributeID INTEGER, AttributeValues BLOB, PRIMARY KEY "
    "(TriangulationID, AttributeID), FOREIGN KEY (AttributeID) REFERENCES PointAttributeDefinition (ID) ON DELETE CASCADE)",
    "CREATE TABLE TriangleAttributeDefinition (ID INTEGER, Name TEXT, Type INTEGER, Precision INTEGER, DefaultValueText "
    "TEXT, DefaultValueInteger INTEGER, DefaultValueReal REAL, PRIMARY KEY (ID))",
    "CREATE TABLE TriangleAttributeValue (TriangulationID INTEGER, AttributeID INTEGER, AttributeValues BLOB, PRIMARY "
    "KEY (TriangulationID, AttributeID), FOREIGN KEY (AttributeID) REFERENCES TriangleAttributeDefinition (ID) ON DELETE CASCADE)",
]


def _colorref(hexcol):
    try:
        h = hexcol.lstrip("#")
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        return r | (g << 8) | (b << 16)
    except Exception:
        return 6553600


def tridb_blob(v, t):
    v = np.asarray(v, "<f8").reshape(-1, 3)
    t = np.asarray(t, "<i4").reshape(-1, 3)
    guid = str(uuid.uuid1()).encode()
    out = [struct.pack("<6i", -1, 327680, len(guid), 0, 9, 0), guid]
    for tag, arr in ((1, v[:, 0]), (2, v[:, 1]), (3, v[:, 2]), (4, t.reshape(-1))):
        data = np.ascontiguousarray(arr).tobytes()
        out.append(struct.pack("<QQ", len(data), tag))
        out.append(data)
    return b"".join(out)


def write_tridb(path, meshes, append=True):
    """Wireframes into a Micromine .tridb (created if missing; an existing one gets new wireframes,
    replacing any of the same name). meshes: objects with vertices, triangles, name and optional color."""
    _guard(path)
    new = not os.path.exists(path)
    con = sqlite3.connect(path)
    try:
        if new:
            for sql in TRIDB_SCHEMA:
                con.execute(sql)
        elif not append:
            con.execute("delete from GeneralInformation")
            con.execute("delete from Geometry")
        now = int(datetime.datetime.now().timestamp())
        user = os.environ.get("USERNAME") or os.environ.get("USER") or ""
        for m in meshes:
            v, t = np.asarray(m.vertices, float), np.asarray(m.triangles, np.int64)
            row = con.execute("select ID from GeneralInformation where Name=?", (m.name,)).fetchone()
            if row:
                wid = row[0]
                con.execute("delete from GeneralInformation where ID=?", (wid,))
                con.execute("delete from Geometry where ID=?", (wid,))
            else:
                wid = (con.execute("select max(ID) from GeneralInformation").fetchone()[0] or 0) + 1
            st = m.stats() if hasattr(m, "stats") else {}
            mn, mx = v.min(axis=0), v.max(axis=0)
            con.execute("insert into GeneralInformation values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (wid, m.name, "", "", _colorref(getattr(m, "color", None) or ""), now, user, "", now, user, "",
                         "Written by QGeo 3D", 0, float(mn[0]), float(mx[0]), float(mn[1]), float(mx[1]),
                         float(mn[2]), float(mx[2]), 0,
                         float(st["volume"]) if st.get("closed") else -1.0, float(st.get("area", 0.0)), 0, 0, "", "", 0))
            con.execute("insert into Geometry values (?,?)", (wid, tridb_blob(v, t)))
        con.commit()
    finally:
        con.close()
    return len(meshes)


# ================================================================ Datamine
DM_ABSENT = -1.0e30


def write_dm(path, fields, columns, description="Written by QGeo 3D"):
    """Datamine .dm, extended (double) precision. fields: [(name <= 8 chars, 'N' | 'A', chars for A)];
    columns: {name: sequence}. Alpha fields take one 4-character word per 4 characters."""
    _guard(path)
    n = len(next(iter(columns.values()))) if columns else 0
    entries, words = [], 0                       # (name, type, stored word, word-in-field)
    for name, typ, width in fields:
        if typ == "A":
            nw = max(1, int(math.ceil(width / 4.0)))
            for k in range(nw):
                words += 1
                entries.append((name, "A", words, k + 1))
        else:
            words += 1
            entries.append((name, "N", words, 1))
    # header layout: page 1 holds 68 entries (from word 28), each further page 73
    pos, w = [], 28
    for k in range(len(entries)):
        if w < 512 and k == 68:
            w = 512
        elif w >= 512 and w + 7 > (w // 512 + 1) * 512:
            w = (w // 512 + 1) * 512
        pos.append(w)
        w += 7
    hpages = w // 512 + (1 if w % 512 else 0)
    per = 508 // words if words else 1
    dpages = max(1, int(math.ceil(n / per))) if n else 1
    last = n - (dpages - 1) * per if n else 0
    buf = bytearray(b"\x00" * ((hpages + dpages) * 4096))

    def put_txt(i, s):
        buf[i * 8:i * 8 + 8] = s[:4].ljust(4).encode("latin-1", "replace") + b"    "

    def put_num(i, x):
        struct.pack_into("<d", buf, i * 8, float(x))
    for i in range(4):
        put_txt(i, "")
    desc = (description or "")[:40].ljust(64)
    for i in range(16):
        put_txt(4 + i, desc[i * 4:i * 4 + 4])
    put_txt(20, "SYST")
    put_txt(21, "EM")
    put_num(22, 0)
    put_num(23, 2)
    put_num(24, 456789)
    put_num(25, len(entries))
    put_num(26, hpages + dpages)
    put_num(27, last)
    for (name, typ, sw, wif), p in zip(entries, pos):
        put_txt(p, name[:4])
        put_txt(p + 1, name[4:8])
        put_txt(p + 2, typ)
        put_num(p + 3, sw)
        put_num(p + 4, wif)
        put_txt(p + 5, "")
        if typ == "N":
            put_num(p + 6, DM_ABSENT)
        else:
            put_txt(p + 6, "")
    # data
    cols = []
    for name, typ, width in fields:
        c = columns[name]
        if typ == "N":
            a = np.array([DM_ABSENT if _num(x) is None else _num(x) for x in c], "<f8")
            cols.append(("N", a, 1))
        else:
            nw = max(1, int(math.ceil(width / 4.0)))
            cols.append(("A", [("" if x is None else str(x))[:nw * 4].ljust(nw * 4) for x in c], nw))
    rec = np.zeros((n, words * 8), np.uint8)
    o = 0
    for typ, a, nw in cols:
        if typ == "N":
            rec[:, o:o + 8] = a.view(np.uint8).reshape(-1, 8)
            o += 8
        else:
            for k in range(nw):
                chunk = np.frombuffer(b"".join(s[k * 4:k * 4 + 4].encode("latin-1", "replace") + b"    " for s in a),
                                      np.uint8).reshape(-1, 8) if n else np.zeros((0, 8), np.uint8)
                rec[:, o:o + 8] = chunk
                o += 8
    for pg in range(dpages):
        part = rec[pg * per:(pg + 1) * per].reshape(-1)
        start = (hpages + pg) * 4096
        buf[start:start + len(part)] = part.tobytes()
    with open(path, "wb") as f:
        f.write(bytes(buf))
    return n


def _dm_name(name, used):
    s = re.sub(r"[^A-Za-z0-9_]", "", name).upper()[:8] or "FIELD"
    base, k = s, 2
    while s in used:
        suf = str(k)
        s, k = base[:8 - len(suf)] + suf, k + 1
    used.add(s)
    return s


def _dm_attrs(fields, rows, reserved):
    used = set(reserved)
    out = []
    for name, t in fields:
        dn = _dm_name(name, used)
        if t == "C":
            w = max([len(str(r.get(name) or "")) for r in rows] + [4])
            out.append((name, dn, "A", min(w, 64)))
        else:
            out.append((name, dn, "N", 0))
    return out


def dm_wireframe(path, mesh):
    """Datamine wireframe pair: <stem>pt.dm (XP YP ZP PID) and <stem>tr.dm (PID1 PID2 PID3 TRIANGLE).
    `path` may name either file or the stem."""
    stem = re.sub(r"(pt|tr)?\.dm$", "", path, flags=re.I)
    v, t = np.asarray(mesh.vertices, float), np.asarray(mesh.triangles, np.int64)
    pid = np.arange(1, len(v) + 1, dtype=float)
    write_dm(stem + "pt.dm", [("XP", "N", 0), ("YP", "N", 0), ("ZP", "N", 0), ("PID", "N", 0)],
             {"XP": v[:, 0], "YP": v[:, 1], "ZP": v[:, 2], "PID": pid}, f"{mesh.name} points")
    write_dm(stem + "tr.dm", [("PID1", "N", 0), ("PID2", "N", 0), ("PID3", "N", 0), ("TRIANGLE", "N", 0)],
             {"PID1": t[:, 0] + 1.0, "PID2": t[:, 1] + 1.0, "PID3": t[:, 2] + 1.0,
              "TRIANGLE": np.arange(1, len(t) + 1, dtype=float)}, f"{mesh.name} triangles")
    return stem + "pt.dm", stem + "tr.dm"


def dm_strings(path, strings, fields=()):
    """Datamine string file: XP YP ZP PTN PVALUE (+ attributes); closed strings repeat the first point."""
    rows_all = [s.get("attrs", {}) for s in strings]
    attrs = _dm_attrs(fields, rows_all, {"XP", "YP", "ZP", "PTN", "PVALUE"})
    cols = {k: [] for k in ("XP", "YP", "ZP", "PTN", "PVALUE")}
    for a in attrs:
        cols[a[1]] = []
    for k, s in enumerate(strings, 1):
        p = np.asarray(s["pts"], float)
        if s.get("closed") and len(p) > 2 and not np.allclose(p[0], p[-1], rtol=0, atol=1e-6):
            p = np.vstack([p, p[:1]])
        at = s.get("attrs", {})
        for i, q in enumerate(p, 1):
            cols["XP"].append(q[0]); cols["YP"].append(q[1]); cols["ZP"].append(q[2])
            cols["PTN"].append(i); cols["PVALUE"].append(k)
            for name, dn, typ, w in attrs:
                cols[dn].append(at.get(name))
    f = [("XP", "N", 0), ("YP", "N", 0), ("ZP", "N", 0), ("PTN", "N", 0), ("PVALUE", "N", 0)] + \
        [(dn, typ, w) for _n, dn, typ, w in attrs]
    return write_dm(path, f, cols, "strings")


def dm_points(path, xyz, fields, rows):
    attrs = _dm_attrs(fields, rows, {"XPT", "YPT", "ZPT"})
    cols = {"XPT": xyz[:, 0], "YPT": xyz[:, 1], "ZPT": xyz[:, 2]}
    for name, dn, typ, w in attrs:
        cols[dn] = [r.get(name) for r in rows]
    return write_dm(path, [("XPT", "N", 0), ("YPT", "N", 0), ("ZPT", "N", 0)] + [(dn, t, w) for _n, dn, t, w in attrs],
                    cols, "points")


def dm_blocks(path, xyz, size, fields, rows, rot=None, origin=None):
    """Datamine block model: IJK, XC YC ZC, XINC YINC ZINC (+ attributes). Parent cell = the largest
    block size; origin = the lower corner; rows sorted by IJK. Implicit NX/NY/NZ/XMORIG... are stored
    as ordinary fields too (simple but readable by Studio as a table)."""
    xyz, size = np.asarray(xyz, float), np.asarray(size, float)
    lo = (xyz - size / 2).min(axis=0)
    hi = (xyz + size / 2).max(axis=0)
    cell = size.max(axis=0)
    nxyz = np.maximum(1, np.ceil((hi - lo) / cell - 1e-9)).astype(int)
    idx = np.clip(np.floor((xyz - lo) / cell).astype(int), 0, nxyz - 1)
    ijk = (idx[:, 2] * nxyz[1] + idx[:, 1]) * nxyz[0] + idx[:, 0]
    order = np.argsort(ijk, kind="stable")
    attrs = _dm_attrs(fields, rows, {"IJK", "XC", "YC", "ZC", "XINC", "YINC", "ZINC", "XMORIG", "YMORIG", "ZMORIG",
                                     "NX", "NY", "NZ"})
    cols = {"IJK": ijk[order].astype(float), "XC": xyz[order, 0], "YC": xyz[order, 1], "ZC": xyz[order, 2],
            "XINC": size[order, 0], "YINC": size[order, 1], "ZINC": size[order, 2]}
    n = len(xyz)
    for nm, val in (("XMORIG", lo[0]), ("YMORIG", lo[1]), ("ZMORIG", lo[2]),
                    ("NX", nxyz[0]), ("NY", nxyz[1]), ("NZ", nxyz[2])):
        cols[nm] = np.full(n, float(val))
    rotk = []
    if rot is not None and not np.allclose(rot, np.eye(3)):
        # rotated model: XC.. stay in model coordinates; world = (X0, Y0, Z0) + R . (XC, YC, ZC)
        from .dmfile import datamine_angles
        a1, a2, a3 = datamine_angles(rot)
        o = np.zeros(3) if origin is None else np.asarray(origin, float)
        for nm, val in (("X0", o[0]), ("Y0", o[1]), ("Z0", o[2]), ("ANGLE1", a1), ("ANGLE2", a2), ("ANGLE3", a3),
                        ("ROTAXIS1", 3), ("ROTAXIS2", 1), ("ROTAXIS3", 3)):
            cols[nm] = np.full(n, float(val))
            rotk.append(nm)
    for name, dn, typ, w in attrs:
        cols[dn] = [rows[i].get(name) for i in order]
    f = [(k, "N", 0) for k in ("IJK", "XC", "YC", "ZC", "XINC", "YINC", "ZINC", "XMORIG", "YMORIG", "ZMORIG",
                               "NX", "NY", "NZ")] + [(k, "N", 0) for k in rotk] + [(dn, t, w) for _n, dn, t, w in attrs]
    return write_dm(path, f, cols, "block model")


# ================================================================== Surpac
def _sp_head(name, purpose=""):
    return f"{name},{datetime.date.today().strftime('%d-%b-%y')},{purpose},QGeo 3D\n" \
           "0,           0.000,           0.000,           0.000,           0.000,           0.000,           0.000\n"


def sp_strings(path, strings, fields=(), string_field=None):
    """Surpac string file. String number: `string_field` if numeric, else 1 for all; each polyline is a
    segment (0 record between). Closed strings repeat the first point. d1.. = attribute fields."""
    _guard(path)
    names = [f[0] for f in fields if f[0] != string_field]
    out = [_sp_head(os.path.splitext(os.path.basename(path))[0], "strings")]
    for s in strings:
        p = np.asarray(s["pts"], float)
        if s.get("closed") and len(p) > 2 and not np.allclose(p[0], p[-1], rtol=0, atol=1e-6):
            p = np.vstack([p, p[:1]])
        a = s.get("attrs", {})
        sn = _num(a.get(string_field)) if string_field else None
        sn = int(sn) if sn is not None and 1 <= sn <= 32000 else 1
        desc = ",".join(str("" if a.get(n) is None else a.get(n)).replace(",", ";") for n in names)
        for q in p:
            out.append(f"{sn}, {q[1]:.3f}, {q[0]:.3f}, {q[2]:.3f}" + (f", {desc}" if desc else ",") + "\n")
        out.append("0, 0.000, 0.000, 0.000,\n")
    out.append("0, 0.000, 0.000, 0.000, END\n")
    with open(path, "w", encoding="latin-1", errors="replace", newline="\r\n") as f:
        f.write("".join(out))
    return len(strings)


def sp_points(path, xyz, fields, rows, string_no=1):
    strings = [{"pts": p[None, :], "attrs": r} for p, r in zip(np.asarray(xyz, float), rows)]
    _guard(path)
    names = [f[0] for f in fields]
    out = [_sp_head(os.path.splitext(os.path.basename(path))[0], "points")]
    for s in strings:
        q, a = s["pts"][0], s["attrs"]
        desc = ",".join(str("" if a.get(n) is None else a.get(n)).replace(",", ";") for n in names)
        out.append(f"{string_no}, {q[1]:.3f}, {q[0]:.3f}, {q[2]:.3f}" + (f", {desc}" if desc else ",") + "\n")
    out.append("0, 0.000, 0.000, 0.000,\n0, 0.000, 0.000, 0.000, END\n")
    with open(path, "w", encoding="latin-1", errors="replace", newline="\r\n") as f:
        f.write("".join(out))
    return len(strings)


def sp_dtm(path, mesh, string_no=1):
    """Surpac DTM + its string file (<stem>.str holds the points, string `string_no`). One object,
    one trisolation; neighbours are written as 0 (Surpac rebuilds them)."""
    stem = os.path.splitext(path)[0]
    spath, dpath = stem + ".str", stem + ".dtm"
    _guard(spath)
    v, t = np.asarray(mesh.vertices, float), np.asarray(mesh.triangles, np.int64)
    out = [_sp_head(os.path.basename(stem), "dtm")]
    for q in v:
        out.append(f"{string_no}, {q[1]:.3f}, {q[0]:.3f}, {q[2]:.3f},\n")
    out.append("0, 0.000, 0.000, 0.000,\n0, 0.000, 0.000, 0.000, END\n")
    with open(spath, "w", encoding="latin-1", newline="\r\n") as f:
        f.write("".join(out))
    closed = "yes" if (mesh.stats().get("closed") if hasattr(mesh, "stats") else False) else "no"
    lines = [f"{os.path.basename(spath)},{int(datetime.datetime.now().timestamp())};algorithm=standard;fields=x,y\n",
             "OBJECT, 1,\n", f"TRISOLATION, 1, neighbours=no,validated=false,closed={closed}\n"]
    for k, (a, b, c) in enumerate(t, 1):
        lines.append(f"{k}, {a + 1}, {b + 1}, {c + 1}, 0, 0, 0,\n")
    lines.append("END\n")
    with open(dpath, "w", encoding="latin-1", newline="\r\n") as f:
        f.write("".join(lines))
    return spath, dpath


# ===================================================================== DXF
def _dxf_layer(name):
    return re.sub(r"[^A-Za-z0-9_\-]", "_", name or "0")[:31] or "0"


def dxf_write(path, meshes=(), strings=(), points=None, layer="QGeo 3D"):
    """DXF R12: meshes as 3DFACE, strings as 3D POLYLINE (closed flag), points as POINT."""
    _guard(path)
    o = ["0\nSECTION\n2\nHEADER\n9\n$ACADVER\n1\nAC1009\n0\nENDSEC\n0\nSECTION\n2\nENTITIES\n"]
    for m in meshes:
        ly = _dxf_layer(getattr(m, "name", layer))
        v, t = np.asarray(m.vertices, float), np.asarray(m.triangles, np.int64)
        for a, b, c in t:
            pa, pb, pc = v[a], v[b], v[c]
            o.append(f"0\n3DFACE\n8\n{ly}\n10\n{pa[0]:.4f}\n20\n{pa[1]:.4f}\n30\n{pa[2]:.4f}\n"
                     f"11\n{pb[0]:.4f}\n21\n{pb[1]:.4f}\n31\n{pb[2]:.4f}\n12\n{pc[0]:.4f}\n22\n{pc[1]:.4f}\n32\n{pc[2]:.4f}\n"
                     f"13\n{pc[0]:.4f}\n23\n{pc[1]:.4f}\n33\n{pc[2]:.4f}\n")
    for s in strings:
        p = np.asarray(s["pts"], float)
        if len(p) < 2:
            continue
        flag = 8 | (1 if s.get("closed") else 0)
        o.append(f"0\nPOLYLINE\n8\n{_dxf_layer(layer)}\n66\n1\n10\n0.0\n20\n0.0\n30\n0.0\n70\n{flag}\n")
        for q in p:
            o.append(f"0\nVERTEX\n8\n{_dxf_layer(layer)}\n10\n{q[0]:.4f}\n20\n{q[1]:.4f}\n30\n{q[2]:.4f}\n70\n32\n")
        o.append(f"0\nSEQEND\n8\n{_dxf_layer(layer)}\n")
    if points is not None:
        for q in np.asarray(points, float):
            o.append(f"0\nPOINT\n8\n{_dxf_layer(layer)}\n10\n{q[0]:.4f}\n20\n{q[1]:.4f}\n30\n{q[2]:.4f}\n")
    o.append("0\nENDSEC\n0\nEOF\n")
    with open(path, "w", newline="\r\n") as f:
        f.write("".join(o))


def obj_write(path, mesh):
    _guard(path)
    v, t = np.asarray(mesh.vertices, float), np.asarray(mesh.triangles, np.int64)
    with open(path, "w") as f:
        f.write(f"# {mesh.name} - QGeo 3D\no {mesh.name}\n")
        f.writelines(f"v {a:.4f} {b:.4f} {c:.4f}\n" for a, b, c in v)
        f.writelines(f"f {a + 1} {b + 1} {c + 1}\n" for a, b, c in t)


def csv_write(path, header, rows):
    _guard(path)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        for r in rows:
            w.writerow(["" if v is None or (isinstance(v, float) and v != v) else v for v in r])
    return len(rows)


# ============================================================ format table
# kind -> [(label, extension, function name)]
FORMATS = {
    "wireframe": [("Micromine wireframe database (.tridb)", ".tridb", "tridb"),
                  ("Datamine wireframe (pt.dm + tr.dm)", ".dm", "dm_wf"),
                  ("Surpac DTM / solid (.dtm + .str)", ".dtm", "sp_dtm"),
                  ("DXF 3DFACE (Vulcan, Leapfrog, Deswik, AutoCAD)", ".dxf", "dxf"),
                  ("Wavefront OBJ", ".obj", "obj"),
                  ("Leapfrog mesh (.msh)", ".msh", "lf_msh"),
                  ("Open Mining Format 1.0 (.omf: Leapfrog, Maptek, Deswik)", ".omf", "omf")],
    "strings": [("Micromine string file (.STR)", ".STR", "mm_str"),
                ("Datamine string file (.dm)", ".dm", "dm_str"),
                ("Surpac string file (.str)", ".str", "sp_str"),
                ("DXF 3D polylines", ".dxf", "dxf"),
                ("CSV (one row per point)", ".csv", "csv"),
                ("Open Mining Format 1.0 (.omf line set)", ".omf", "omf")],
    "points": [("Micromine data file (.DAT)", ".DAT", "mm_pts"),
               ("Datamine points (.dm)", ".dm", "dm_pts"),
               ("Surpac string file (.str, one point per record)", ".str", "sp_pts"),
               ("DXF points", ".dxf", "dxf"),
               ("CSV", ".csv", "csv"),
               ("Open Mining Format 1.0 (.omf point set)", ".omf", "omf")],
    "blocks": [("Micromine block model (.DAT)", ".DAT", "mm_bm"),
               ("Datamine block model (.dm)", ".dm", "dm_bm"),
               ("Leapfrog block model CSV (embedded header; X Y Z dX dY dZ)", ".csv", "lf_csv"),
               ("CSV (centres + sizes: Vulcan, Surpac import)", ".csv", "csv"),
               ("Open Mining Format 1.0 (.omf; regular models as a grid)", ".omf", "omf")],
}


def export(kind, fmt, path, data, fields=(), name="export"):
    """Write `data` of `kind` as format key `fmt`. Returns a short description of what was written."""
    if fmt in ("omf", "lf_msh", "lf_csv"):
        return _export_open(kind, fmt, path, data, fields, name)
    if kind == "wireframe":
        meshes = data if isinstance(data, (list, tuple)) else [data]
        if fmt == "tridb":
            write_tridb(path, meshes)
            return f"{len(meshes)} wireframe(s) -> {path}"
        if fmt == "dm_wf":
            if len(meshes) == 1:
                a, b = dm_wireframe(path, meshes[0])
                return f"{a} + {os.path.basename(b)}"
            outs = []
            for m in meshes:
                st = re.sub(r"(pt|tr)?\.dm$", "", path, flags=re.I) + "_" + re.sub(r"[^A-Za-z0-9]", "", m.name)
                outs.append(dm_wireframe(st, m)[0])
            return f"{len(outs)} pt/tr pairs next to {path}"
        if fmt == "sp_dtm":
            if len(meshes) == 1:
                s, d = sp_dtm(path, meshes[0])
                return f"{d} + {os.path.basename(s)}"
            for m in meshes:
                sp_dtm(os.path.splitext(path)[0] + "_" + re.sub(r"[^A-Za-z0-9]", "", m.name) + ".dtm", m)
            return f"{len(meshes)} DTMs next to {path}"
        if fmt == "dxf":
            dxf_write(path, meshes=meshes)
            return f"{sum(len(m.triangles) for m in meshes):,} 3DFACEs -> {path}"
        if fmt == "obj":
            if len(meshes) == 1:
                obj_write(path, meshes[0])
            else:
                for m in meshes:
                    obj_write(os.path.splitext(path)[0] + "_" + re.sub(r"[^A-Za-z0-9]", "", m.name) + ".obj", m)
            return path
    if kind == "strings":
        st = data
        if fmt == "mm_str":
            return f"{mm_strings(path, st, fields):,} points in {len(st)} strings -> {path}"
        if fmt == "dm_str":
            return f"{dm_strings(path, st, fields):,} points in {len(st)} strings -> {path}"
        if fmt == "sp_str":
            sp_strings(path, st, fields)
            return f"{len(st)} strings -> {path}"
        if fmt == "dxf":
            dxf_write(path, strings=st, layer=name)
            return f"{len(st)} polylines -> {path}"
        if fmt == "csv":
            names = [f[0] for f in fields]
            rows = [[k, i, q[0], q[1], q[2]] + [s.get("attrs", {}).get(n) for n in names]
                    for k, s in enumerate(st, 1) for i, q in enumerate(np.asarray(s["pts"], float), 1)]
            csv_write(path, ["STRING", "POINT", "EAST", "NORTH", "RL"] + names, rows)
            return f"{len(rows):,} rows -> {path}"
    if kind == "points":
        xyz, rows = data
        xyz = np.asarray(xyz, float)
        if fmt == "mm_pts":
            return f"{mm_points(path, xyz, fields, rows):,} points -> {path}"
        if fmt == "dm_pts":
            return f"{dm_points(path, xyz, fields, rows):,} points -> {path}"
        if fmt == "sp_pts":
            return f"{sp_points(path, xyz, fields, rows):,} points -> {path}"
        if fmt == "dxf":
            dxf_write(path, points=xyz, layer=name)
            return f"{len(xyz):,} points -> {path}"
        if fmt == "csv":
            names = [f[0] for f in fields if f[0].upper() not in ("EAST", "NORTH", "RL")]
            csv_write(path, ["EAST", "NORTH", "RL"] + names, [list(p) + [r.get(n) for n in names] for p, r in zip(xyz, rows)])
            return f"{len(xyz):,} rows -> {path}"
    if kind == "blocks":
        xyz, size, rows = data[:3]
        if fmt == "mm_bm":
            return f"{mm_blocks(path, xyz, size, fields, rows):,} blocks -> {path}"
        if fmt == "dm_bm":
            ex = data[3] if len(data) > 3 else {}
            if ex.get("rot") is not None:
                return f"{dm_blocks(path, ex['model_xyz'], size, fields, rows, ex['rot'], ex['origin']):,} rotated blocks -> {path}"
            return f"{dm_blocks(path, xyz, size, fields, rows):,} blocks -> {path}"
        if fmt == "csv":
            names = [f[0] for f in fields]
            csv_write(path, ["XC", "YC", "ZC", "XINC", "YINC", "ZINC"] + names,
                      [list(p) + list(s) + [r.get(n) for n in names] for p, s, r in zip(xyz, size, rows)])
            return f"{len(xyz):,} blocks -> {path}"
    raise ValueError(f"Can't write {kind} as {fmt}")


def _export_open(kind, fmt, path, data, fields, name):
    """Leapfrog (.msh, block model CSV) and OMF 1.0 writers."""
    _guard(path)
    from . import leapfrog as LF
    if fmt == "lf_msh":
        meshes = data if isinstance(data, (list, tuple)) else [data]
        for k, m in enumerate(meshes):
            p = path if len(meshes) == 1 else os.path.splitext(path)[0] + "_" + re.sub(r"[^A-Za-z0-9]", "", m.name) + ".msh"
            LF.write_msh(p, m.vertices, m.triangles)
        return f"{len(meshes)} mesh(es) -> {path}"
    if fmt == "lf_csv":
        xyz, size, rows = data[:3]
        ex = data[3] if len(data) > 3 else {}
        n = LF.write_bm_csv(path, xyz, size, rows, fields, name, origin=ex.get("origin"),
                            azimuth=ex.get("azimuth", 0.0))
        return f"{n:,} blocks -> {path}"
    from .omf1 import OmfWriter
    w = OmfWriter(path, name=name)
    if kind == "wireframe":
        meshes = data if isinstance(data, (list, tuple)) else [data]
        for m in meshes:
            w.surface(m.name, m.vertices, m.triangles, getattr(m, "color", None))
        what = f"{len(meshes)} surface(s)"
    elif kind == "strings":
        polys = [np.asarray(s["pts"], float) for s in data]
        rows = [s.get("attrs", {}) for s in data]
        w.lines(name, polys, fields, rows)
        what = f"{len(polys)} strings"
    elif kind == "points":
        xyz, rows = data
        w.points(name, xyz, fields, rows)
        what = f"{len(xyz):,} points"
    elif kind == "blocks":
        xyz, size, rows = data[:3]
        ex = data[3] if len(data) > 3 else {}
        if ex.get("rot") is not None:
            w.blocks(name, ex["model_xyz"], size, fields, rows, rot=ex["rot"], origin=ex["origin"])
        else:
            w.blocks(name, xyz, size, fields, rows)
        what = f"{len(xyz):,} blocks"
    else:
        raise ValueError(f"Can't write {kind} as OMF")
    w.save()
    return f"{what} -> {path}"
