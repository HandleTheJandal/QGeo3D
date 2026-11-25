# -*- coding: utf-8 -*-
"""Block models: Micromine .DAT (EAST/NORTH/RL centres, _EAST/_NORTH/_RL sizes),
Datamine .dm (core/dmfile.py), Vulcan .bmf (core/bmf.py) and Surpac .mdl (core/surpac.py).

Read only. Block centres/sizes are kept in model coordinates; rotated models carry
`rot` (3x3, model -> world) and `origin` (world position of model 0,0,0). Blocks are filtered and colour-classified in Python, then sent to the 3D view
as one instanced box mesh per colour class (three.js r110 has no per-instance colour).
"""

import base64
import json
import os
import re

import numpy as np

from . import fieldnames as FN
from .mmdat import read_dat
from .compat import gdal_soft

# Grade classes used for Au (same as the assay layer)
AU_CLASSES = [(None, 0.1, "#d9d9d9", "< 0.1"), (0.1, 0.5, "#4f9bd9", "0.1 – 0.5"), (0.5, 1, "#5fb36b", "0.5 – 1"),
              (1, 3, "#f2c12e", "1 – 3"), (3, 10, "#f28e2b", "3 – 10"), (10, None, "#d62728", "≥ 10")]
CAT_PALETTE = ["#4f9bd9", "#f28e2b", "#5fb36b", "#d62728", "#a779d1", "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf"]
RESCAT_NAMES = {1: "Measured", 2: "Indicated", 3: "Inferred", 4: "Unclassified"}
RESCAT_COLORS = {1: "#d62728", 2: "#f2c12e", 3: "#4f9bd9", 4: "#bdbdbd"}


def _b64(a):
    return base64.b64encode(np.ascontiguousarray(a).tobytes()).decode("ascii")


_DIRS = np.array([[1, 0, 0], [-1, 0, 0], [0, 1, 0], [0, -1, 0], [0, 0, 1], [0, 0, -1]], float)


def _popcount(m):
    m = m.astype(np.uint8)
    return sum(((m >> b) & 1) for b in range(6))


def face_mask(xyz, size, q=0.5):
    """6-bit mask per block (bit k = draw face _DIRS[k]). A face is hidden when the block on
    the other side has the same size and is in the same set (same colour class), so the
    shared wall is never visible. Coordinates are snapped to a q-metre grid for matching."""
    n = len(xyz)
    if n == 0:
        return np.zeros(0, np.uint8)
    keyc = np.round(np.column_stack([xyz, size]) / q).astype(np.int64)
    lo = keyc.min(axis=0)
    span = keyc.max(axis=0) - lo + 3
    def enc(k):
        k = k - lo + 1
        out = np.zeros(len(k), np.int64)
        for d in range(k.shape[1]):
            out = out * int(span[d]) + k[:, d]
        return out
    have = np.sort(enc(keyc))
    mask = np.zeros(n, np.uint8)
    for b, dvec in enumerate(_DIRS):
        nb = np.round(np.column_stack([xyz + dvec * size, size]) / q).astype(np.int64)
        inside = np.all((nb - lo + 1 >= 0) & (nb - lo + 1 < span), axis=1)
        found = np.zeros(n, bool)
        if inside.any():
            e = enc(nb[inside])
            pos = np.searchsorted(have, e)
            pos[pos >= len(have)] = 0
            found[inside] = have[pos] == e
        mask |= (~found).astype(np.uint8) << b
    return mask


def infer_spacing(values, others=None, max_n=500000):
    """Regular block size along one axis from the centre coordinates: the most common gap between
    neighbouring centres in the same row (same other two coordinates), so sub-block offsets in
    other rows don't count. None if it can't be told."""
    v = np.asarray(values, float)
    o = None if others is None else np.column_stack([np.asarray(c, float) for c in others])
    ok = np.isfinite(v) if o is None else (np.isfinite(v) & np.isfinite(o).all(axis=1))
    v = np.round(v[ok][:max_n], 3)
    if len(v) < 2:
        return None
    if o is None:
        d = np.diff(np.unique(v))
    else:
        o = np.round(o[ok][:max_n], 3)
        order = np.lexsort((v, o[:, 1], o[:, 0]))
        vs, os_ = v[order], o[order]
        same = np.all(os_[1:] == os_[:-1], axis=1)
        d = np.diff(vs)[same]
    d = np.round(d[d > 1e-6], 3)
    if not len(d):
        return None
    vals, counts = np.unique(d, return_counts=True)
    if counts.max() < 3:            # no real grid (e.g. reprojected centres): can't tell
        return None
    return float(vals[np.argmax(counts)])


class BlockModel:

    def __init__(self, path):
        fields, rows = read_dat(path)
        self.path = path
        self.name = os.path.splitext(os.path.basename(path))[0]
        self.fields = [f[0] for f in fields]
        num = [f[0] for f in fields if f[1] in "RLF"]
        cols = list(zip(*rows)) if rows else [[] for _ in fields]
        self.data = {}
        for (name, t, _w, _d), col in zip(fields, cols):
            if t in "RLF":
                self.data[name] = np.array([np.nan if v is None else v for v in col], float)
            else:
                self.data[name] = np.array(col, object)
        from .micromine import block_cols
        cols = block_cols(fields)
        if cols is None:
            raise ValueError("Not a block model: no block centre and size fields found "
                             "(e.g. EAST/NORTH/RL with _EAST/_NORTH/_RL, or X/Y/Z with XINC/YINC/ZINC)")
        self.xyz = np.column_stack([self.data[cols[k]] for k in ("xc", "yc", "zc")])
        self.size = np.column_stack([self.data[cols[k]] for k in ("dx", "dy", "dz")])
        need = set(cols.values())
        ok = np.isfinite(self.xyz).all(axis=1) & np.isfinite(self.size).all(axis=1) & (self.size > 0).all(axis=1)
        notes = []
        if (~ok).any():
            notes.append(f"{int((~ok).sum())} record(s) without a centre or size were skipped")
            self.xyz, self.size = self.xyz[ok], self.size[ok]
            self.data = {k: v[ok] for k, v in self.data.items()}
        self.numeric = [n for n in num if n not in need]
        self.size_fields = need
        self._init_common(notes=notes)

    def _init_common(self, rot=None, origin=None, notes=None):
        self.rot = np.eye(3) if rot is None else np.asarray(rot, float)
        self.origin = np.zeros(3) if origin is None else np.asarray(origin, float)
        self.rotated = not np.allclose(self.rot, np.eye(3))
        self.notes = list(notes or [])
        self.shown = np.arange(len(self.xyz))      # indices currently sent to the 3D view
        self.classes = []                          # [(label, color, indices)]
        self.styles = {}                           # field -> colour style (core.bmstyle), from QGIS / by hand
        self.followQgis = True                     # take the style of its QGIS block-centre layer
        self.colorField = None
        self._up = {n.upper(): n for n in self.data}
        if not hasattr(self, "descriptions"):
            self.descriptions = {}
        num = [n for n in getattr(self, "numeric", []) if n in self.data]
        self.grades = FN.grade_fields(num)                 # grade fields, reported estimate first
        self.densityField = FN.density_field(num)
        self.tonnesField = FN.tonnes_field(num)
        self.propField = FN.proportion_field(num)
        self.categoryField = FN.category_field(num)

    @classmethod
    def from_dict(cls, d, path):
        """From dmfile.read_blockmodel / bmf.read_blockmodel output."""
        bm = cls.__new__(cls)
        bm.path = path
        bm.name = d["name"]
        bm.fields = list(d["fields"])
        bm.data = d["data"]
        bm.xyz = np.asarray(d["xyz"], float)
        bm.size = np.asarray(d["size"], float)
        bm.numeric = [n for n in bm.fields if bm.data[n].dtype != object]
        bm.descriptions = d.get("descriptions", {})
        bm._init_common(d.get("rot"), d.get("origin"), d.get("notes"))
        return bm

    @classmethod
    def from_table(cls, fields, rows, cols, name, path):
        """From a CSV / Excel table. cols: {xc, yc, zc: field names; dx, dy, dz: field name or a number
        (regular model)}. Rows without a centre are skipped."""
        names = [f[0] for f in fields]
        num = {f[0] for f in fields if f[1] in "RLFN"}
        data = {}
        for n in names:
            col = [r.get(n) for r in rows]
            data[n] = np.array([np.nan if v is None else v for v in col], float) if n in num else np.array(col, object)
        xyz = np.column_stack([data[cols["xc"]], data[cols["yc"]], data[cols["zc"]]])
        size = np.column_stack([data[cols[k]] if isinstance(cols[k], str) else np.full(len(rows), float(cols[k]))
                                for k in ("dx", "dy", "dz")])
        ok = np.isfinite(xyz).all(axis=1) & np.isfinite(size).all(axis=1) & (size > 0).all(axis=1)
        used = {cols[k] for k in ("xc", "yc", "zc", "dx", "dy", "dz") if isinstance(cols[k], str)}
        d = {"name": name, "fields": names, "data": {n: v[ok] for n, v in data.items()},
             "xyz": xyz[ok], "size": size[ok],
             "notes": [f"{int((~ok).sum())} row(s) without a centre or size were skipped"] if (~ok).any() else []}
        bm = cls.from_dict(d, path)
        bm.numeric = [n for n in bm.numeric if n not in used]
        bm.size_fields = used
        return bm

    @classmethod
    def load(cls, path):
        """Any supported block model by extension: .dat (Micromine), .dm (Datamine), .bmf (Vulcan)."""
        ext = os.path.splitext(path)[1].lower()
        if ext == ".dm":
            from .dmfile import read_blockmodel
            return cls.from_dict(read_blockmodel(path), path)
        if ext == ".bmf":
            from .bmf import read_blockmodel
            return cls.from_dict(read_blockmodel(path), path)
        if ext == ".mdl":
            from .surpac import read_mdl
            return cls.from_dict(read_mdl(path), path)
        return cls(path)

    def set_crs(self, src, dst):
        """Place the model, whose coordinates are in `src`, into `dst` (the project CRS).
        Rigid: the model centre is transformed exactly and the model turned by the grid
        convergence there. Can be called again with another src."""
        from .crs import rigid_placement
        if not hasattr(self, "_src_place"):
            self._src_place = (self.origin.copy(), self.rot.copy())
        o, r = self._src_place
        pivot = (self.xyz.min(axis=0) + self.xyz.max(axis=0)) / 2.0 if len(self.xyz) else np.zeros(3)
        self.origin, self.rot = rigid_placement(o, r, pivot.astype(float), src, dst)
        self.rotated = not np.allclose(self.rot, np.eye(3))
        self.src_crs = src

    def world(self, p):
        """Model coordinates (n,3) or (3,) -> world (project CRS) coordinates."""
        p = np.asarray(p, float)
        return p @ self.rot.T + self.origin

    def world_bbox(self, idx=None):
        """[xmin, ymin, xmax, ymax] of the (rotated) blocks in world coordinates."""
        idx = self.shown if idx is None else idx
        if not len(idx):
            return None
        lo = (self.xyz[idx] - self.size[idx] / 2).min(axis=0)
        hi = (self.xyz[idx] + self.size[idx] / 2).max(axis=0)
        corners = np.array([[x, y, z] for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2])])
        w = self.world(corners)
        return [float(w[:, 0].min()), float(w[:, 1].min()), float(w[:, 0].max()), float(w[:, 1].max())]

    def _f(self, name):
        """Actual field name for a case-insensitive name, or None."""
        return getattr(self, "_up", {}).get(name.upper())

    def __len__(self):
        return len(self.xyz)

    # ----------------------------------------------------------- display
    def default_field(self):
        """Colour field shown first: the reported grade estimate of the first element, else the
        resource category, else the first numeric field."""
        if self.grades:
            return self.grades[0]
        if self.categoryField:
            return self.categoryField
        return self.numeric[0] if self.numeric else None

    def grade_info(self, field):
        return FN.grade_info(field) if field else None

    def breaks(self, field):
        """[(lo, hi, colour, label)] graduated classes for a continuous field, or None for a
        coded one (resource category, or <= 10 distinct values)."""
        st = getattr(self, "styles", {}).get(field)
        if st is not None:
            return [tuple(c) for c in st["classes"]] if st["kind"] == "range" else None
        v = np.asarray(self.data[field], float)
        good = ~np.isnan(v)
        if field == self.categoryField or FN.is_category(field):
            return None
        info = FN.grade_info(field)
        if info and info["element"] == "AU" and info["unit"] in ("g/t", "ppm", "ppb", "oz/t"):
            f = {"g/t": 1.0, "ppm": 1.0, "ppb": 1000.0, "oz/t": 1 / 34.2857}[info["unit"]]
            def lab(x):
                return f"{x * f:g}" if f != 1 / 34.2857 else f"{x * f:.3f}"
            out = []
            for lo, hi, col, _l in AU_CLASSES:
                lo2, hi2 = (None if lo is None else lo * f), (None if hi is None else hi * f)
                text = f"< {lab(hi)}" if lo is None else (f"≥ {lab(lo)}" if hi is None else f"{lab(lo)} – {lab(hi)}")
                out.append((lo2, hi2, col, text))
            return out
        u = np.unique(v[good])
        if 0 < len(u) <= 10 and not info:
            return None
        qs = np.unique(np.quantile(v[good], np.linspace(0, 1, 7))) if good.any() else []
        ramp = ["#2c7bb6", "#00a6ca", "#90eb9d", "#f9d057", "#f29e2e", "#d7191c"]
        return [(qs[i], qs[i + 1] if i < len(qs) - 2 else None, ramp[min(i, 5)], f"{qs[i]:.3g} – {qs[i + 1]:.3g}")
                for i in range(len(qs) - 1)]

    def category_labels(self, field):
        """{code: name} for a resource category field: from the field description when it says,
        else the usual 1 = Measured, 2 = Indicated, 3 = Inferred."""
        d = (getattr(self, "descriptions", {}) or {}).get(field, "")
        return FN.category_names(d) or (RESCAT_NAMES if field == self.categoryField or FN.is_category(field) else {})

    def classify(self, field, idx):
        st = getattr(self, "styles", {}).get(field)
        if st is not None and st["kind"] == "cat":
            col = self.data[field][idx]
            text = col.dtype == object
            vals = col.astype(object) if text else np.asarray(col, float)
            out, used = [], np.zeros(len(idx), bool)
            rest = None
            for value, colour, label in st["classes"]:
                if value is None:
                    rest = (label, colour)
                    continue
                if text:
                    m = np.array([str(x) == str(value) for x in vals], bool)
                else:
                    try:
                        m = vals == float(value)
                    except (TypeError, ValueError):
                        m = np.zeros(len(idx), bool)
                m &= ~used
                used |= m
                out.append((label, colour, idx[m]))
            if rest is not None:
                out.append((rest[0], rest[1], idx[~used]))
            return [c for c in out if len(c[2])]
        v = np.asarray(self.data[field], float)[idx]
        out = []
        br = self.breaks(field)
        if br is None:
            names = self.category_labels(field)
            cat = bool(names)
            colours = {"Measured": "#d62728", "Indicated": "#f2c12e", "Inferred": "#4f9bd9", "Unclassified": "#bdbdbd"}
            spare = [c for c in CAT_PALETTE if c not in colours.values()]
            k = 0
            for code in np.unique(v[~np.isnan(v)]):
                c = int(code) if float(code).is_integer() else code
                name = names.get(c, "") if cat else ""
                col = colours.get(name)
                if col is None and cat:            # a code the category doesn't name (0, 9, -99 ...)
                    col = "#bdbdbd" if c in (0, 4, 9, 99, -9, -99) else spare[k % len(spare)]
                    k += col != "#bdbdbd"
                if col is None:
                    col = CAT_PALETTE[k % len(CAT_PALETTE)]
                    k += 1
                out.append((f"{code:g} {name}".strip(), col, idx[v == code]))
        else:
            good = ~np.isnan(v)
            for k, (lo, hi, col, lab) in enumerate(br):
                m = good.copy()
                if lo is not None:
                    m &= v >= lo
                if hi is not None:
                    m &= v < hi if (k < len(br) - 1 or hi is None) else v <= hi
                out.append((lab, col, idx[m]))
        return [c for c in out if len(c[2])]

    # ------------------------------------------------- hide (air / mined out)
    def default_hide_rule(self):
        """Rule that hides air / mined-out blocks, from the fields this model has (in-situ %,
        density / SG, tonnes - whatever they are called)."""
        for n in (self.propField, self.densityField, self.tonnesField):
            if n:
                return f"{n} <= 0"
        return ""

    def hide_suggestions(self):
        """[(rule, label)] guessed from field names and descriptions (e.g. '3 = air')."""
        out = []
        for n in (self.propField, self.densityField, self.tonnesField):
            if n:
                out.append((f"{n} <= 0", f"no {n} (air / void)"))
        words = r"(air|mined[ _-]?out|mined|void|stope|depleted?|fill|tails)"
        for n in self.numeric:
            d = (getattr(self, "descriptions", {}) or {}).get(n, "") or ""
            for code, what in re.findall(r"(-?\d+)\s*=\s*" + words, d, re.I):
                out.append((f"{n} = {code}", f"{n} {code} = {what.lower()}"))
            for what, code in re.findall(words + r"[^=0-9]{0,12}=\s*(-?\d+)", d, re.I):
                out.append((f"{n} = {code}", f"{n} {code} = {what.lower()}"))
            nu = n.upper()
            nc = re.sub(r"[^A-Z0-9]", "", nu)
            if nc in ("VOID", "MINED", "DEPLETE", "DEPLETED", "DEPLETION", "MINEDOUT", "STOPE", "STOPES", "AIR",
                      "ISAIR", "VOIDS", "UG", "UGVOID", "OPENPIT", "PIT", "EXCAVATED", "DEPL", "MINEDFLAG",
                      "VOIDFLAG", "AIRFLAG") and not d:
                out.append((f"{n} = 1", f"{n} = 1"))
            if nc in ("TOPO", "BELOWTOPO", "INSITUFLAG", "INTOPO", "UNDERTOPO", "BTOPO", "SURFACE") and not d:
                out.append((f"{n} = 0", f"{n} = 0 (above topography)"))
            if nc in ("ROCKTYPE", "ROCK", "MATERIAL", "MAT", "WEATH", "WEATHERING", "OXIDE", "REGOLITH") and \
                    not d and len(np.unique(self.data[n][~np.isnan(self.data[n])])) <= 12:
                vals = np.unique(self.data[n][~np.isnan(self.data[n])])
                if len(vals) and vals.min() <= 0:
                    out.append((f"{n} <= 0", f"{n} <= 0 (often air)"))
        seen, res = set(), []
        for r, lab in out:
            if r.lower() not in seen:
                seen.add(r.lower())
                res.append((r, lab))
        return res

    def parse_rule(self, rule):
        """'density <= 0 or rock = 3' -> [(field, op, value)]. Raises ValueError with a message."""
        rule = (rule or "").strip()
        if not rule:
            return []
        out = []
        for part in re.split(r"\s+or\s+|[,;|]", rule, flags=re.I):
            part = part.strip()
            if not part:
                continue
            m = re.match(r"^([A-Za-z_][\w%~]*)\s*(==|=|!=|<>|<=|>=|<|>)\s*(-?[0-9.]+(?:[eE][-+]?\d+)?)$", part)
            if not m:
                raise ValueError(f"can't read '{part}' (use e.g. density <= 0, rock = 3)")
            f = self._f(m.group(1))
            if f is None or f not in self.numeric:
                raise ValueError(f"no numeric field '{m.group(1)}'")
            op = {"=": "==", "<>": "!="}.get(m.group(2), m.group(2))
            out.append((f, op, float(m.group(3))))
        return out

    def hide_mask(self, rule):
        """True for blocks matching any rule (they are hidden)."""
        hide = np.zeros(len(self.xyz), bool)
        for f, op, v in self.parse_rule(rule):
            col = np.asarray(self.data[f], float)
            with np.errstate(invalid="ignore"):
                hit = {"==": np.isclose(col, v), "!=": ~np.isclose(col, v) & ~np.isnan(col),
                       "<": col < v, "<=": col <= v, ">": col > v, ">=": col >= v}[op]
            hide |= hit & ~np.isnan(col) if op != "!=" else hit
            if op == "<=" and v <= 0 and f in (self.densityField, self.propField, self.tonnesField):
                hide |= np.isnan(col)              # no density = air
        return hide

    def select(self, field, min_value=None, exclude_air=True):
        """Blocks to show: optional minimum of the colour field, and not hidden by the hide rule.
        exclude_air: True = the model's default rule, False/'' = hide nothing, or a rule string."""
        m = np.ones(len(self.xyz), bool)
        rule = self.default_hide_rule() if exclude_air is True else (exclude_air or "")
        if rule:
            m &= ~self.hide_mask(rule)
        if min_value is not None and field in self.data and self.data[field].dtype != object:
            m &= np.nan_to_num(self.data[field], nan=-np.inf) >= min_value
        return np.where(m)[0]

    def payload(self, field, min_value=None, exclude_air=True, opacity=1.0):
        self._last_args = (field, min_value, exclude_air, opacity)
        idx = self.select(field, min_value, exclude_air)
        self.shown = idx
        self.colorField = field
        self.classes = self.classify(field, idx)
        # what is drawn: blocks with no value in the colour field fall in no class and aren't shown
        idx = np.sort(np.concatenate([ci for _l, _c, ci in self.classes])) if self.classes else idx[:0]
        self.shown = idx
        sel = self.xyz[idx] if len(idx) else np.zeros((0, 3))
        mn, mx = (sel.min(axis=0), sel.max(axis=0)) if len(idx) else (np.zeros(3), np.zeros(3))
        offset = np.round((mn + mx) / 2.0, 3)
        classes = []
        for lab, col, ci in self.classes:
            mask = face_mask(self.xyz[ci], self.size[ci])
            classes.append({"label": lab, "color": col, "n": int(len(ci)), "faces": int(_popcount(mask).sum()),
                            "pos": _b64((self.xyz[ci] - offset).astype("<f4")),
                            "size": _b64(self.size[ci].astype("<f4")),
                            "mask": _b64(mask.astype("u1"))})
        spec = {"name": self.name, "offset": [float(x) for x in offset], "classes": classes,
                "opacity": float(opacity), "field": field}
        if self.rotated or np.any(self.origin):
            # positions stay in model coordinates; the 3D view places them with rot + world offset
            spec["offset"] = [float(x) for x in self.world(offset)]
            spec["rot"] = [float(x) for x in self.rot.ravel()]
        return spec

    def section_outlines(self, a, b, shrink=0.10, limit_ends=False, idx_classes=None):
        """Block cross-sections on the vertical plane through map points a -> b, as rectangles
        shrunk by `shrink` (fraction) about their centres. Uses the current colour classes.

        Returns (corners (n,4,3) world coords, colours [hex per rectangle]) or raises ValueError
        for a model tilted out of the horizontal (only rotations about Z are handled)."""
        if not np.allclose(self.rot[2], [0, 0, 1]) or not np.allclose(self.rot[:, 2], [0, 0, 1]):
            raise ValueError("block outlines on section need a model rotated about Z only")
        a = np.array([a[0], a[1], 0.0])
        b = np.array([b[0], b[1], 0.0])
        L = np.linalg.norm((b - a)[:2])
        if L <= 0:
            return np.zeros((0, 4, 3)), []
        Rt = self.rot.T
        p0 = Rt @ (a - self.origin)                     # section line in model coordinates
        u = Rt @ ((b - a) / L)
        out, cols = [], []
        for lab, col, ci in (self.classes if idx_classes is None else idx_classes):
            if not len(ci):
                continue
            c, h = self.xyz[ci], self.size[ci] / 2.0
            t0 = np.full(len(ci), -np.inf)
            t1 = np.full(len(ci), np.inf)
            ok = np.ones(len(ci), bool)
            for k in (0, 1):                             # Liang-Barsky against the block footprint
                if abs(u[k]) < 1e-12:
                    ok &= np.abs(p0[k] - c[:, k]) <= h[:, k]
                    continue
                ta = (c[:, k] - h[:, k] - p0[k]) / u[k]
                tb = (c[:, k] + h[:, k] - p0[k]) / u[k]
                t0 = np.maximum(t0, np.minimum(ta, tb))
                t1 = np.minimum(t1, np.maximum(ta, tb))
            if limit_ends:
                t0, t1 = np.maximum(t0, 0.0), np.minimum(t1, L)
            ok &= (t1 - t0) > 1e-6
            if not ok.any():
                continue
            t0, t1, cz, hz = t0[ok], t1[ok], c[ok, 2], h[ok, 2]
            tm, ht = (t0 + t1) / 2.0, (t1 - t0) / 2.0 * (1.0 - shrink)
            hz = hz * (1.0 - shrink)
            def P(t, z):
                q = p0[None, :] + t[:, None] * u[None, :]
                q[:, 2] = z
                return self.world(q)
            rect = np.stack([P(tm - ht, cz - hz), P(tm + ht, cz - hz), P(tm + ht, cz + hz), P(tm - ht, cz + hz)], axis=1)
            out.append(rect)
            cols += [col] * len(rect)
        return (np.concatenate(out) if out else np.zeros((0, 4, 3))), cols

    def legend(self):
        return [(lab, col, len(ci)) for lab, col, ci in self.classes]

    def row(self, i):
        rows = []
        if self.rotated or np.any(self.origin):
            w = self.world(self.xyz[i])
            rows += [("East (world)", float(w[0])), ("North (world)", float(w[1])), ("RL (world)", float(w[2])),
                     ("XC (model)", float(self.xyz[i, 0])), ("YC (model)", float(self.xyz[i, 1])),
                     ("ZC (model)", float(self.xyz[i, 2]))]
        if not getattr(self, "size_fields", None) and not self._f("XINC"):
            rows += [("Size X", float(self.size[i, 0])), ("Size Y", float(self.size[i, 1])),
                     ("Size Z", float(self.size[i, 2]))]
        return rows + [(n, self.data[n][i]) for n in self.fields]

    def block_tonnes(self, idx=None):
        """(tonnes per block, how) for blocks idx: the tonnes field, else volume × density
        (× in-situ proportion). (None, None) when the model has neither."""
        idx = np.arange(len(self)) if idx is None else np.asarray(idx)
        if self.tonnesField:
            return np.nan_to_num(self.data[self.tonnesField][idx]) * FN.tonnes_scale(self.tonnesField), self.tonnesField
        if self.densityField:
            t = np.prod(self.size[idx], axis=1) * np.nan_to_num(self.data[self.densityField][idx])
            how = f"volume × {self.densityField}"
            if self.propField:
                p = np.nan_to_num(self.data[self.propField][idx])
                fin = self.data[self.propField][~np.isnan(self.data[self.propField])]
                p = p / 100.0 if len(fin) and fin.max() > 1.0001 else p
                t = t * np.clip(p, 0, 1)
                how += f" × {self.propField}"
            return t, how
        return None, None

    def stats(self, idx=None):
        idx = self.shown if idx is None else idx
        out = {"blocks": int(len(idx))}
        t = None
        if self.tonnesField:
            t = np.nan_to_num(self.data[self.tonnesField][idx]) * FN.tonnes_scale(self.tonnesField)
            out["tonnes_from"] = self.tonnesField
        elif self.densityField:
            t = np.prod(self.size[idx], axis=1) * np.nan_to_num(self.data[self.densityField][idx])
            out["tonnes_from"] = f"volume × {self.densityField}"
            if self.propField:
                p = np.nan_to_num(self.data[self.propField][idx])
                fin = self.data[self.propField][~np.isnan(self.data[self.propField])]
                p = p / 100.0 if len(fin) and fin.max() > 1.0001 else p
                t = t * np.clip(p, 0, 1)
                out["tonnes_from"] += f" × {self.propField}"
        if t is not None:
            out["tonnes"] = float(t.sum())
            f = self.colorField if self.colorField in self.grades else (self.grades[0] if self.grades else None)
            info = FN.grade_info(f) if f else None
            if info and t.sum() > 0:
                g = np.nan_to_num(self.data[f][idx])
                out["grade_field"] = f
                out["grade"] = float((g * t).sum() / t.sum())
                out["grade_unit"] = info["unit"]
                out["metal"], out["metal_unit"] = FN.contained(float((g * t).sum()), info)
                if out["metal_unit"] == "oz":
                    out["oz"] = out["metal"]
        else:
            vol = np.prod(self.size[idx], axis=1)
            out["volume"] = float(vol.sum())
        return out


GPKG_SIZE = ("XINC", "YINC", "ZINC")
GPKG_MODEL_XYZ = ("_MODEL_X", "_MODEL_Y", "_MODEL_Z")
GPKG_META = "QGEO3D_BLOCKMODEL"


def read_gpkg_placement(path, layer):
    """(rot 3x3, origin, model-xyz field names) stored by write_gpkg for a rotated model, else None."""
    from osgeo import ogr
    try:
        ds = ogr.Open(path)
        L = ds.GetLayerByName(layer) if ds is not None else None
        txt = L.GetMetadataItem(GPKG_META) if L is not None else None
        if not txt:
            return None
        d = json.loads(txt)
        return np.asarray(d["rot"], float).reshape(3, 3), np.asarray(d["origin"], float), list(d["model_xyz"])
    except Exception:
        return None


@gdal_soft
def write_gpkg(bm, out_path, epsg, layer_name="blocks", idx=None):
    """Block centres as PointZ with every field (for QGIS 2D use, filtering by RL, stats).
    idx: only these blocks (large models)."""
    from osgeo import ogr, osr
    from .q2thook import is_protected
    if is_protected(out_path):
        raise PermissionError(f"Ringfenced location, not writing: {out_path}")
    if os.path.exists(out_path):
        os.remove(out_path)
    ds = ogr.GetDriverByName("GPKG").CreateDataSource(out_path)
    from .crs import make_srs
    srs = make_srs(epsg)      # EPSG code or a CRS
    lyr = ds.CreateLayer(layer_name, srs, ogr.wkbPoint25D)
    skip = {"fid", "_geom_x", "_geom_y", "_geom_z"}
    names = [n for n in bm.fields if n.lower() not in skip]
    for n in names:
        lyr.CreateField(ogr.FieldDefn(n, ogr.OFTReal if bm.data[n].dtype != object else ogr.OFTString))
    # block size (and, for a rotated / re-placed model, its model coordinates) so the GeoPackage
    # reopens as the same model - regular models keep their size outside the fields
    have = {n.lower() for n in names}
    extra = []
    for k, nm in enumerate(GPKG_SIZE):
        if nm.lower() not in have:
            extra.append((nm, bm.size[:, k]))
    if bm.rotated or np.any(bm.origin):
        for k, nm in enumerate(GPKG_MODEL_XYZ):
            extra.append((nm, bm.xyz[:, k]))
        lyr.SetMetadataItem(GPKG_META, json.dumps({"rot": [float(x) for x in np.asarray(bm.rot).ravel()],
                                                   "origin": [float(x) for x in bm.origin],
                                                   "model_xyz": list(GPKG_MODEL_XYZ)}))
    for nm, _v in extra:
        lyr.CreateField(ogr.FieldDefn(nm, ogr.OFTReal))
    defn = lyr.GetLayerDefn()
    cols = [(defn.GetFieldIndex(n), n, bm.data[n]) for n in names] + \
           [(defn.GetFieldIndex(nm), nm, np.asarray(v, float)) for nm, v in extra]
    wxyz = bm.world(bm.xyz)
    ds.StartTransaction()
    for r in (range(len(bm)) if idx is None else idx):
        f = ogr.Feature(defn)
        for i, n, col in cols:
            v = col[r]
            if v is None or (isinstance(v, float) and v != v):
                continue
            f.SetField(i, float(v) if col.dtype != object else str(v))
        g = ogr.Geometry(ogr.wkbPoint25D)
        g.AddPoint(float(wxyz[r, 0]), float(wxyz[r, 1]), float(wxyz[r, 2]))
        f.SetGeometry(g)
        lyr.CreateFeature(f)
    ds.CommitTransaction()
    ds = None


def outline_payload(corners, colours):
    """Rectangles (n,4,3) + hex colours -> overlay spec for Q2T4.setOverlay (per-vertex colours,
    drawn on top, every corner a snap point)."""
    n = len(corners)
    if n == 0:
        return None
    pts = corners.reshape(-1, 3)
    mn, mx = pts.min(axis=0), pts.max(axis=0)
    offset = np.round((mn + mx) / 2.0, 3)
    base = np.arange(n, dtype=np.uint32)[:, None] * 4
    seg = (base + np.array([0, 1, 1, 2, 2, 3, 3, 0], np.uint32)[None, :]).ravel()
    rgb = np.array([[int(c[i:i + 2], 16) for i in (1, 3, 5)] for c in colours], np.uint8)
    return {"offset": [float(x) for x in offset], "pos": _b64((pts - offset).astype("<f4")),
            "seg": _b64(seg.astype("<u4")), "snap": _b64(np.arange(4 * n, dtype="<u4")),
            "colors": _b64(np.repeat(rgb, 4, axis=0)), "color": "#ffffff", "pointSize": 0, "onTop": True}
