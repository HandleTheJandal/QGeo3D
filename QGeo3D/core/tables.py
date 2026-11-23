# -*- coding: utf-8 -*-
"""Tables from CSV / TXT / Excel (.xlsx, .xlsm, .ods) through GDAL/OGR, read only.

Cells are read as text with headers off, then the header row is found (the first row of
mostly non-numeric labels within the first 30 rows - sheets often have a title block above
the table) and each column typed: 'R' when every non-empty cell is a number, else 'C'.
Field tuples follow mmdat: (name, type, width, decimals).
"""

import os
import re

TABLE_EXT = (".csv", ".txt", ".tsv", ".xlsx", ".xlsm", ".ods")


def _open(path, raw=True):
    from osgeo import gdal
    ext = os.path.splitext(path)[1].lower()
    if ext in (".csv", ".txt", ".tsv"):
        opts = ["HEADERS=NO", "AUTODETECT_TYPE=NO"] if raw else ["AUTODETECT_TYPE=YES"]
        if ext == ".tsv":
            opts.append("SEPARATOR=TAB")
        ds = gdal.OpenEx(path, gdal.OF_VECTOR | gdal.OF_READONLY, allowed_drivers=["CSV"], open_options=opts)
    else:
        opts = ["HEADERS=DISABLE", "FIELD_TYPES=STRING"] if raw else []
        ds = gdal.OpenEx(path, gdal.OF_VECTOR | gdal.OF_READONLY, open_options=opts)
    if ds is None:
        raise ValueError(f"Cannot open {os.path.basename(path)} as a table")
    return ds


def sheets(path):
    ds = _open(path)
    return [ds.GetLayer(i).GetName() for i in range(ds.GetLayerCount())]


def _isnum(s):
    try:
        float(str(s).replace(",", "")) if s not in (None, "") else None
        return s not in (None, "")
    except ValueError:
        return False


def _tonum(s):
    if s is None or s == "":
        return None
    try:
        return float(str(s).replace(",", ""))
    except ValueError:
        return None


def read_table(path, sheet=None, header_row=None, max_rows=None):
    """Returns (fields, rows as dicts, header_row index). sheet: name or index (default first)."""
    if os.path.splitext(path)[1].lower() in (".csv", ".txt", ".tsv"):
        raw = _csv_rows(path, max_rows)
        return _finish(raw, header_row, len(raw[0]) if raw else 0)
    ds = _open(path)
    if sheet is None:
        lyr = ds.GetLayer(0)
    elif isinstance(sheet, int):
        lyr = ds.GetLayer(sheet)
    else:
        lyr = ds.GetLayerByName(sheet)
    if lyr is None:
        raise ValueError(f"No sheet {sheet!r} in {os.path.basename(path)}")
    nf = lyr.GetLayerDefn().GetFieldCount()
    raw = []
    lyr.ResetReading()
    for f in lyr:
        raw.append([f.GetFieldAsString(i) if f.IsFieldSetAndNotNull(i) else "" for i in range(nf)])
        if max_rows and len(raw) >= max_rows + 40:
            break
    ds = None
    return _finish(raw, header_row, nf)


def _csv_rows(path, max_rows=None):
    import csv
    with open(path, "rb") as fh:
        head = fh.read(65536)
    enc = "utf-8-sig"
    try:
        head.decode(enc)
    except UnicodeDecodeError:
        enc = "cp1252"
    sample = "\n".join(l for l in head.decode(enc, "replace").splitlines() if not l.lstrip().startswith("#"))
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    raw = []
    with open(path, newline="", encoding=enc, errors="replace") as fh:
        # '#' lines are comments (Leapfrog's embedded block model header, other exporters' notes)
        lines = (l for l in fh if not l.lstrip().startswith("#"))
        for r in csv.reader(lines, dialect):
            raw.append(r)
            if max_rows and len(raw) >= max_rows + 40:
                break
    return raw


def csv_comments(path, max_lines=400):
    """'#' comment lines at the top of a CSV, plus a Leapfrog sidecar header (<file>.txt), as text lines
    without the '#'."""
    out = []
    for p in (path, path + ".txt"):
        if not os.path.isfile(p):
            continue
        try:
            with open(p, encoding="utf-8-sig", errors="replace") as fh:
                for i, l in enumerate(fh):
                    if i > max_lines:
                        break
                    t = l.strip()
                    if p == path:
                        if t.startswith("#"):
                            out.append(t.lstrip("#").strip())
                        elif t:
                            break
                    else:
                        out.append(t.lstrip("#").strip())
        except OSError:
            pass
    return out


def _finish(raw, header_row, nf):
    while raw and not any(c.strip() for c in raw[-1]):
        raw.pop()
    if not raw:
        return [], [], 0
    if header_row is None:
        # the header is the last row of labels above the first row holding numbers
        # (sheets often carry a title block or a 'COMPULSORY / OPTIONAL' row above the real header)
        header_row = 0
        first_data = next((i for i, r in enumerate(raw[:60]) if sum(_isnum(c) for c in r) >= 2), None)
        lab = lambda r: (lambda cells: len(cells) >= 2 and sum(not _isnum(c) for c in cells) >= 0.8 * len(cells))(
            [c for c in r if c.strip()])
        if first_data is not None:
            for i in range(first_data - 1, -1, -1):
                if lab(raw[i]):
                    header_row = i
                    break
        else:
            header_row = next((i for i, r in enumerate(raw[:30]) if lab(r)), 0)
    head = raw[header_row]
    ncol = max(i + 1 for i, c in enumerate(head) if c.strip()) if any(c.strip() for c in head) else nf
    names, used = [], set()
    for i in range(ncol):
        n = head[i].strip() or f"Field{i + 1}"
        base, k = n, 2
        while n.lower() in used:
            n, k = f"{base}_{k}", k + 1
        used.add(n.lower())
        names.append(n)
    data = [r[:ncol] + [""] * (ncol - len(r)) for r in raw[header_row + 1:] if any(c.strip() for c in r[:ncol])]
    types = []
    for i in range(ncol):
        vals = [r[i] for r in data if r[i].strip()]
        types.append("R" if vals and all(_isnum(v) for v in vals) else "C")
    fields = [(n, t, 8 if t == "R" else 255, 0) for n, t in zip(names, types)]
    rows = []
    for r in data:
        rows.append({n: (_tonum(v) if t == "R" else (v.strip() or None)) for (n, t, _w, _d), v in zip(fields, r)})
    return fields, rows, header_row


# ------------------------------------------------------------ field guessing
def _n(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


# grid / datum / unit words that decorate a field name without changing what it is
# ("X_MGA94", "MGA_E", "RL_AHD", "Depth_m", "Azimuth_Grid", "Easting (GDA2020 z50)")
_DECOR = re.compile(r"^(mga|gda|utm|wgs|nad|amg|agd|ahd|asl|msl|isg|local|loc|grid|nat|national|m|ft|feet|"
                    r"metres?|meters?|deg|degs|degrees?|true|tn|gn|final|adj|adjusted|surveyed|planned|plan|"
                    r"actual|act|dgps|gps|rtk|z|zone|mine|minegrid|lg|proj|projected|crs|epsg)\d*[a-z]?$")
_MAGNETIC = re.compile(r"(^|[^a-z])mag(netic)?([^a-z]|$)", re.I)


def _core(s):
    """Name without grid / datum / unit words: 'X_MGA94' -> 'x', 'MGA_N' -> 'n', 'RL_AHD' -> 'rl',
    'Depth (m)' -> 'depth'. Falls back to the plain normalised name."""
    toks = [t for t in re.split(r"[^a-z0-9]+", s.lower()) if t]
    keep = [t for t in toks if not _DECOR.match(t) and not re.fullmatch(r"\d+", t)]
    if not keep and toks:            # 'Z' alone, 'Zone' alone, 'M' ...: keep the name as it is
        return _n(s)
    return "".join(keep)


ROLES = {
    "hole": ["holeid", "hole", "bhid", "holename", "holeno", "holenumber", "holecode", "dhid", "drillhole",
             "drillholeid", "dh", "dhname", "borehole", "boreholeid", "bh", "bhname", "id", "siteid", "site",
             "channelid", "trenchid", "traverseid", "name"],
    "east": ["east", "easting", "x", "mgae", "utme", "gdae", "amge", "nateast", "eastutm", "e", "xcoord",
             "eastings", "collarx", "collareast", "xcollar"],
    "north": ["north", "northing", "y", "mgan", "utmn", "gdan", "amgn", "natnorth", "northutm", "n", "ycoord",
              "northings", "collary", "collarnorth", "ycollar"],
    "rl": ["rl", "z", "elev", "elevation", "natrl", "height", "rlm", "zcoord", "collarrl", "collarz", "zcollar",
           "collarelev", "rlcollar", "alt", "altitude", "ahd"],
    "depth": ["maxdepth", "depth", "eoh", "totaldepth", "td", "finaldepth", "length", "eohm", "holedepth",
              "enddepth", "eohdepth", "holelength", "drilleddepth", "maxdep", "dep"],
    "azimuth": ["azimuth", "azi", "az", "collarazimuth", "natazimuth", "bearing", "brg", "dipdir",
                "dipdirection", "azim", "collaraz", "plannedazimuth"],
    "dip": ["dip", "collardip", "inclination", "incl", "inc", "plunge", "angle", "declination"],
    "sdepth": ["depth", "surveydepth", "at", "distance", "depthm", "surveyat", "md", "station", "sdepth",
               "dep"],
    "from": ["from", "depthfrom", "fromm", "mfrom", "start", "sampfrom", "samplefrom", "fromdepth", "intfrom",
             "top", "frm", "fr"],
    "to": ["to", "depthto", "tom", "mto", "end", "sampto", "sampleto", "todepth", "intto", "bottom", "bot"],
    "xc": ["east", "x", "xc", "xcentre", "xcenter", "xcentroid", "easting", "xmid", "centrex", "centerx",
           "centroidx", "midx", "xcen", "xcent", "xpt", "xp"],
    "yc": ["north", "y", "yc", "ycentre", "ycenter", "ycentroid", "northing", "ymid", "centrey", "centery",
           "centroidy", "midy", "ycen", "ycent", "ypt", "yp"],
    "zc": ["rl", "z", "zc", "zcentre", "zcenter", "zcentroid", "elevation", "zmid", "centrez", "centerz",
           "centroidz", "midz", "zcen", "zcent", "elev", "zpt", "zp"],
    "dx": ["east", "xinc", "dx", "xsize", "sizex", "xdim", "xlen", "blockx", "xblock", "blkx", "xblk", "dimx",
           "lenx", "xlength", "eastsize", "sizeeast", "xwidth", "xextent", "xspan", "incx", "blocksizex"],
    "dy": ["north", "yinc", "dy", "ysize", "sizey", "ydim", "ylen", "blocky", "yblock", "blky", "yblk", "dimy",
           "leny", "ylength", "northsize", "sizenorth", "ywidth", "yextent", "yspan", "incy", "blocksizey"],
    "dz": ["rl", "zinc", "dz", "zsize", "sizez", "zdim", "zlen", "blockz", "zblock", "blkz", "zblk", "dimz",
           "lenz", "zlength", "rlsize", "sizerl", "zheight", "zextent", "zspan", "incz", "blocksizez"],
}
# Micromine block sizes are the centre name with a leading underscore: _EAST, _X, _RL ...
_UNDERSCORE_SIZE = {"dx": ("east", "x", "e", "easting", "xc"), "dy": ("north", "y", "n", "northing", "yc"),
                    "dz": ("rl", "z", "elevation", "elev", "zc")}


def guess(fields, role, numeric=None):
    """Best field name for a role, or None. Exact names first (also with grid / datum / unit words
    removed, so 'X_MGA94', 'MGA_E' and 'Depth (m)' match), then names starting or ending with one."""
    if numeric is None:
        numeric = role not in ("hole",)
    cands = [f[0] for f in fields if (f[1] in "RLFN") or not numeric]
    # metres before feet ("Max_Depth_m" over "Max_Depth_ft"); grid / true azimuths before magnetic
    cands.sort(key=lambda c: (1 if re.search(r"(ft|feet)$", _n(c)) else 0) +
               (2 if role == "azimuth" and _MAGNETIC.search(c) else 0))
    pats = ROLES[role]
    if role in ("dx", "dy", "dz"):
        for c in cands:
            if c.startswith("_") and (_n(c) in _UNDERSCORE_SIZE[role] or _core(c) in _UNDERSCORE_SIZE[role]):
                return c
        pats = pats[1:]
    plain = [c for c in cands if not (role not in ("dx", "dy", "dz") and c.startswith("_"))]
    for key in (_n, _core):
        nn = {}
        for c in plain:
            nn.setdefault(key(c), c)
        for p in pats:
            if p in nn:
                return nn[p]
    for p in pats:
        if len(p) < 3 and p not in ("rl", "dh", "bh"):
            continue
        for c in plain:
            if c.startswith("_"):
                continue
            for u in {_n(c), _core(c)}:
                hit = u.endswith(p) if len(p) < 3 else (u.startswith(p) or u.endswith(p) or
                                                        re.sub(r"(m|ft|feet)$", "", u).endswith(p))
                if not hit:
                    continue
                if role == "depth" and ("from" in u or "to" == u[-2:] or u.startswith(("surv", "samp"))):
                    continue
                if role == "dip" and "dir" in u:
                    continue
                if role in ("east", "north", "xc", "yc") and u.startswith(("local", "loc")) and len(plain) > 1:
                    continue
                return c
    return None


def guess_sheet(names, role):
    keys = {"collar": ("collar", "header", "holes", "hole"), "survey": ("surv", "dhsurv", "survey"),
            "interval": ("assay", "lith", "sample", "interval", "geol", "log")}[role]
    for n in names:
        if any(k in n.lower() for k in keys):
            return n
    return None
