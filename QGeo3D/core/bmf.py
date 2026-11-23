# -*- coding: utf-8 -*-
"""Maptek Vulcan block model (.bmf, 'TBMS2.0') reader - read only.

Layout worked out from a Vulcan 2023 model and checked value-for-value against the same
model exported to Datamine (147,070 sub-blocks, every variable identical):

  * The file is a sequence of 2056-byte pages: an 8-byte page header + 2048 bytes of payload.
    Page 0 is the file header: 'TBMS2.0', and at byte 24 an int64 file offset of the index
    page that lists the current metadata pages.
  * Page header byte 0/1: 01 01 = index page (payload = int64 file offsets of data pages,
    0 = page not stored, i.e. every value is the variable's default),
    02 01 = two-level index (offsets of index pages, 256 slots each),
    00 02 = text page (bytes 2-3 = bytes used), 00 04/05/06/07 = data pages.
  * Metadata is a text dictionary: "key" = value, nested { } for schemas and variables.
    Each variable has name, type (float, double, int, short, byte, longlong), default and
    location = file offset of its index page (0 = not stored: the default applies to all).
  * Values are flat arrays of n_blocks, in block order. Block extents come from the
    special variables __lower_x/y/z and __upper_x/y/z, in model coordinates.
  * origin_x/y/z and orientation_3 (bearing of the model X axis, 90 = unrotated) place the
    model: world = origin + R . model, R = clockwise rotation by (bearing - 90).
    orientation_1/2 (dip/plunge) other than 0 are reported, not applied.
"""

import os
import re
import struct

import numpy as np

PAGE = 2056
PAYLOAD = 2048
DTYPES = {"float": "<f4", "double": "<f8", "int": "<i4", "short": "<i2", "byte": "<i1",
          "longlong": "<i8", "integer": "<i4", "char": "<i1"}


def _parse_meta(text):
    """Vulcan metadata dictionary -> nested dicts."""
    tok = re.compile(r'\s*(?:(\{)|(\})|(,)|(=)|"((?:[^"\\]|\\.)*)"|([-+0-9.eE]+))')
    pos, n = 0, len(text)

    def nxt():
        nonlocal pos
        m = tok.match(text, pos)
        if not m:
            return None
        pos = m.end()
        return m

    def value(m):
        if m.group(1):
            return obj()
        if m.group(5) is not None:
            return m.group(5)
        s = m.group(6)
        try:
            return int(s)
        except ValueError:
            return float(s)

    def obj():
        out = {}
        while True:
            m = nxt()
            if m is None or m.group(2):
                return out
            if m.group(3):
                continue
            key = m.group(5)
            nxt()                       # '='
            out[key] = value(nxt())

    m = nxt()
    return obj() if m and m.group(1) else {}


class BmfFile:

    def __init__(self, path):
        self.path = path
        with open(path, "rb") as f:
            self.raw = f.read()
        if not self.raw.startswith(b"TBMS"):
            raise ValueError(f"Not a Vulcan block model (.bmf): {path}")
        if len(self.raw) % PAGE:
            raise ValueError("Unexpected .bmf layout (file size is not a whole number of 2056-byte pages)")
        self.version = self.raw[:8].rstrip(b"\0").decode("ascii", "replace")
        self.meta = self._metadata()
        self.n = int(self.meta.get("n_blocks", 0))
        self.variables = {}
        for k, v in self.meta.items():
            if isinstance(v, dict) and (k.startswith("var_") or k.startswith("special_")) and "name" in v:
                self.variables[v["name"]] = v

    # ------------------------------------------------------------- pages
    def _hdr(self, off):
        return self.raw[off:off + 8]

    def _slots(self, off):
        """Data-page offsets for an index page (0 = page not stored)."""
        h = self._hdr(off)
        q = np.frombuffer(self.raw, "<i8", PAYLOAD // 8, off + 8)
        if h[:2] == b"\x01\x01":
            return list(q)
        if h[:2] == b"\x02\x01":
            out = []
            for o in q:
                out += self._slots(int(o)) if o else [0] * (PAYLOAD // 8)
            return out
        raise ValueError(f"Unexpected .bmf index page at {off}: {h.hex()}")

    def _metadata(self):
        idx = struct.unpack_from("<q", self.raw, 24)[0]
        chunks = []
        for o in self._slots(idx):
            if not o:
                break
            h = self._hdr(o)
            used = struct.unpack_from("<H", h, 2)[0] or PAYLOAD
            chunks.append(self.raw[o + 8:o + 8 + used])
        text = b"".join(chunks).decode("latin-1")
        text = text[text.find("{"):]
        return _parse_meta(text)

    # ------------------------------------------------------------- data
    def names(self, special=False):
        return [n for n in self.variables if special or not n.startswith("__")]

    def column(self, name):
        v = self.variables[name]
        dt = np.dtype(DTYPES.get(v.get("type", "float"), "<f4"))
        try:
            default = float(v.get("default", 0))
        except (TypeError, ValueError):
            default = 0
        per = PAYLOAD // dt.itemsize
        npages = (self.n + per - 1) // per
        out = np.full(npages * per, default, dt)
        loc = int(v.get("location", 0) or 0)
        if loc:
            for i, o in enumerate(self._slots(loc)[:npages]):
                if o:
                    out[i * per:(i + 1) * per] = np.frombuffer(self.raw, dt, per, int(o) + 8)
        return out[:self.n]


def read_blockmodel(path):
    """Same dict shape as dmfile.read_blockmodel."""
    f = BmfFile(path)
    need = ["__lower_x", "__upper_x", "__lower_y", "__upper_y", "__lower_z", "__upper_z"]
    if not all(n in f.variables for n in need):
        raise ValueError("This .bmf has no block extents (__lower/__upper variables); regular models "
                         "without them aren't supported yet")
    lo = np.column_stack([f.column(n).astype(float) for n in need[0::2]])
    hi = np.column_stack([f.column(n).astype(float) for n in need[1::2]])
    xyz, size = (lo + hi) / 2.0, hi - lo
    m = f.meta
    notes = []
    bearing = float(m.get("orientation_3", 90) or 90)
    dip = float(m.get("orientation_1", 0) or 0)          # rotation about X (axis 1)
    plunge = float(m.get("orientation_2", 0) or 0)       # rotation about Y (axis 2)
    from .rotations import bearing_plunge_dip
    R = bearing_plunge_dip(bearing, plunge, dip, "vulcan")
    if abs(dip) > 1e-9 or abs(plunge) > 1e-9:
        notes.append(f"plunge {plunge:g}° and dip {dip:g}° applied with Vulcan's left-hand convention "
                     "(positive tilts the X / Y axis down) - check the position against Vulcan once")
    origin = np.array([float(m.get("origin_x", 0)), float(m.get("origin_y", 0)), float(m.get("origin_z", 0))])
    fields = f.names()
    data = {}
    for n in fields:
        col = f.column(n).astype(float)
        try:
            d = float(f.variables[n].get("default", ""))
            if d <= -99:                # Vulcan "no value" defaults (-99, -999)
                col[col == d] = np.nan
        except (TypeError, ValueError):
            pass
        data[n] = col
    return {"name": os.path.splitext(os.path.basename(path))[0], "fields": fields, "data": data,
            "xyz": xyz, "size": size, "rot": R, "origin": origin, "notes": notes,
            "descriptions": {n: f.variables[n].get("description", "") for n in fields}}
