# -*- coding: utf-8 -*-
"""Read Micromine "extended" .DAT files and drillhole databases (.DHDB). Read only.

Adapted from micromine-mcp/micromine/mm_dat.py. Character fields in files whose codepage
is BOCU-1 store each ASCII byte >= 0x30 shifted up by 0x50; plain cp1252 otherwise.
"""

import os
import sqlite3
import struct
import tempfile
import shutil


def _bocu(b):
    """BOCU-1 decode for single-byte (ASCII) text: bytes <= 0x20 are themselves, otherwise
    code point = 0x40 + (byte - 0x90), i.e. byte - 0x50 (previous-char state stays 0x40 for
    ASCII). Multi-byte sequences (non-ASCII) are not expected in these files: '?' instead."""
    out = []
    for x in b:
        if x <= 0x20:
            out.append(chr(x))
        elif 0x50 <= x < 0xD0:
            out.append(chr(x - 0x50))
        else:
            out.append("?")
    return "".join(out)


def read_dat(path):
    """Returns (fields, rows). fields: [(name, type, width, decimals)]; rows: lists of values."""
    with open(path, "rb") as f:
        b = f.read()
    bocu = b"BOCU-1" in b[:2048]
    i = b.find(b"VARIABLES")
    if i < 0:
        raise ValueError(f"Not a Micromine extended .DAT file: {path}")
    st = b.rfind(b"\x00", 0, i) + 1
    nf = int(b[st:i].strip())
    p = i + 11
    fields = []
    for _ in range(nf):
        e = b.index(b"\r\n", p)
        ln = b[p:e]
        long = ln[17:].strip()
        if long.startswith(b"|"):
            name = (_bocu(long[1:]) if bocu else long[1:].decode("cp1252", "replace")).strip()
        else:
            name = (_bocu(ln[:10]) if bocu else ln[:10].decode("cp1252", "replace")).strip()
        fields.append((name, chr(ln[10]), int(ln[11:14]), int(ln[15:17].strip() or 0)))
        p = e + 2
    w = sum(f[2] for f in fields)
    rows = []
    for k in range(p, len(b) - w + 1, w + 2):
        r = b[k:k + w]
        o = 0
        row = []
        for _n, t, wd, _dc in fields:
            s = r[o:o + wd]
            o += wd
            if t == "R":
                v = struct.unpack("<d", s)[0]
                row.append(None if v != v else v)
            elif t == "L":
                v = struct.unpack("<i", s)[0]
                row.append(None if v == -2147483648 else v)
            elif t == "F":
                v = struct.unpack("<f", s)[0]
                row.append(None if v != v else v)
            else:
                row.append((_bocu(s) if bocu else s.decode("cp1252", "replace")).rstrip())
        rows.append(row)
    return fields, rows


def read_dat_dicts(path):
    fields, rows = read_dat(path)
    names = [f[0] for f in fields]
    return [dict(zip(names, r)) for r in rows], fields


def read_dhdb(path):
    """Drillhole database definition. Returns dict with collar / survey / interval file specs
    (file paths resolved next to the .DHDB). The .DHDB is copied to a temp file first so the
    original is never opened by SQLite (SQLite may write to a database it opens)."""
    folder = os.path.dirname(path)
    tmp = tempfile.mkdtemp(prefix="q2t4_dhdb_")
    try:
        cp = os.path.join(tmp, "db.dhdb")
        shutil.copy2(path, cp)
        con = sqlite3.connect(cp)
        def q(sql):
            cur = con.execute(sql)
            names = [d[0] for d in cur.description]
            return [dict(zip(names, r)) for r in cur.fetchall()]
        collar = q("select * from CollarFiles")
        survey = q("select * from SurveyFiles")
        intervals = q("select * from IntervalFiles")
        gen = q("select * from GenInfo")
        con.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    def res(f):
        return f if os.path.isabs(f) else os.path.join(folder, f)
    c = collar[0]
    out = {
        "collar": {"file": res(c["File Name"]), "id": c["Hole ID Field1"], "east": c["East Field"],
                   "north": c["North Field"], "rl": c["RL Field"], "depth": c["Total Depth Field"],
                   "azimuth": c["Azimuth Field"], "dip": c["Dip Field"]},
        "survey": None,
        "intervals": [{"file": res(r["File Name"]), "id": r["Hole ID Field1"], "from": r["From Field"],
                       "to": r["To Field"]} for r in intervals],
        "gen": gen[0] if gen else {},
    }
    if survey:
        s = survey[0]
        out["survey"] = {"file": res(s["File Name"]), "id": s["Hole ID Field1"], "depth": s["Survey Depth Field"],
                         "azimuth": s["Azimuth Field"], "dip": s["Dip Field"]}
    return out
