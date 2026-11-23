# -*- coding: utf-8 -*-
"""Micromine drillhole database (.DHDB) -> desurveyed GeoPackage.

Reads the collar / survey / interval .DAT files the .DHDB points at (read only), desurveys
each hole (minimum curvature, desurvey.py) and writes:
    collars   PointZ       one per hole, all collar fields
    traces    LineStringZ  collar to total depth
    <interval file stem>   LineStringZ per interval, all fields + mid point
"""

import collections
import datetime
import os

from osgeo import ogr, osr

from . import desurvey as DS
from .mmdat import read_dat_dicts, read_dhdb
from .q2thook import is_protected
from .compat import gdal_soft


def _ogr_type(t):
    return ogr.OFTReal if t in ("R", "F") else ogr.OFTInteger if t == "L" else ogr.OFTString


def _add_fields(lyr, fields, used=None):
    names = []
    used = used if used is not None else set()
    for name, t, _w, _d in fields:
        n = name or "field"
        if n.lower() in ("fid", "geom", "geometry"):          # reserved in GeoPackages
            n += "_src"
        base, k = n, 1
        while n.lower() in used:
            k += 1
            n = f"{base}_{k}"
        used.add(n.lower())
        lyr.CreateField(ogr.FieldDefn(n, _ogr_type(t)))
        names.append(n)
    return names


def hole_key(v):
    """Hole IDs compare as trimmed text; numeric IDs read as 12.0 in one table and '12' in another match."""
    if v is None:
        return None
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).strip()


def _resolve(spec, keys):
    """Copy of a table spec whose role -> field names are the table's own spelling (case / spaces ignored).
    A name that matches no field is set to None (and reported by the caller as missing data)."""
    spec = dict(spec)
    names = [f[0] for f in spec.get("fields", [])]
    norm = {str(n).strip().lower(): n for n in names}
    for k in keys:
        v = spec.get(k)
        if not v:
            spec[k] = None
        elif v not in names:
            spec[k] = norm.get(str(v).strip().lower())
    return spec


def build_gpkg(dhdb_path, out_path, epsg):
    """Micromine .DHDB -> GeoPackage (the tables it names are read through mmdat)."""
    db = read_dhdb(dhdb_path)
    c = db["collar"]
    collars, cfields = read_dat_dicts(c["file"])
    collar = dict(c, rows=collars, fields=cfields)
    survey = None
    if db["survey"]:
        s = db["survey"]
        rows, sfields = read_dat_dicts(s["file"])
        survey = dict(s, rows=rows, fields=sfields)
    intervals = []
    for spec in db["intervals"]:
        rows, ifields = read_dat_dicts(spec["file"])
        intervals.append(dict(spec, rows=rows, fields=ifields,
                              name=os.path.splitext(os.path.basename(spec["file"]))[0]))
    return build_from_tables(collar, survey, intervals, out_path, epsg, dhdb_path)


@gdal_soft
def build_from_tables(collar, survey, intervals, out_path, epsg, source, dip_positive_down=False):
    """Desurveyed GeoPackage from tables already read.
    collar: {rows (dicts), fields, id, east, north, rl, depth?, azimuth?, dip?}
    survey: {rows, fields, id, depth, azimuth, dip} or None (collar azimuth/dip, straight holes)
    intervals: [{rows, fields, id, from, to, name}]
    dip_positive_down: dips in the tables are positive downwards (Datamine style); they are flipped to
    the desurvey convention (negative = down)."""
    sgn = -1.0 if dip_positive_down else 1.0
    if is_protected(out_path):
        raise PermissionError(f"Ringfenced location, not writing: {out_path}")
    # field names are matched case-insensitively (Micromine is: a .DHDB may say "Dip" for a field "DIP")
    collar = _resolve(collar, ("id", "east", "north", "rl", "depth", "azimuth", "dip"))
    survey = _resolve(survey, ("id", "depth", "azimuth", "dip")) if survey else None
    intervals = [_resolve(sp, ("id", "from", "to")) for sp in intervals]
    c = collar
    collars, cfields = c["rows"], c["fields"]
    surveys = collections.defaultdict(list)
    if survey:
        s = survey
        for r in s["rows"]:
            if r.get(s["depth"]) is None or r.get(s["azimuth"]) is None or r.get(s["dip"]) is None:
                continue
            surveys[hole_key(r[s["id"]])].append((r[s["depth"]], r[s["azimuth"]], sgn * r[s["dip"]]))
        for k in surveys:
            surveys[k].sort(key=lambda t: t[0])

    holes, report = {}, {"holes": 0, "no_survey": [], "intervals": {}, "missing_hole": collections.Counter()}
    for r in collars:
        hid = hole_key(r.get(c["id"])) if c.get("id") else None
        if hid is None or not c.get("east") or not c.get("north") or r.get(c["east"]) is None or r.get(c["north"]) is None:
            continue
        ss = surveys.get(hid) or []
        if not ss and c.get("azimuth") and c.get("dip") and r.get(c["azimuth"]) is not None and r.get(c["dip"]) is not None:
            ss = [(0.0, r[c["azimuth"]], sgn * r[c["dip"]])]
        rl = r.get(c["rl"]) if c.get("rl") else None
        xyz0 = [r[c["east"]], r[c["north"]], rl or 0.0]
        if not ss:
            report["no_survey"].append(hid)
            holes[hid] = (r, None, None, xyz0)
            continue
        dep, xyz = DS.stations(xyz0, ss, r.get(c["depth"]) if c.get("depth") else None)
        holes[hid] = (r, dep, xyz, xyz0)
    report["holes"] = len(holes)

    if os.path.exists(out_path):
        os.remove(out_path)
    drv = ogr.GetDriverByName("GPKG")
    ds = drv.CreateDataSource(out_path)
    from .crs import make_srs
    srs = make_srs(epsg)      # EPSG code or a CRS

    # collars
    lyr = ds.CreateLayer("collars", srs, ogr.wkbPoint25D)
    names = _add_fields(lyr, cfields)
    for hid, (r, dep, xyz, xyz0) in holes.items():
        f = ogr.Feature(lyr.GetLayerDefn())
        for n, (fn, *_x) in zip(names, cfields):
            if r[fn] is not None:
                f.SetField(n, r[fn])
        g = ogr.Geometry(ogr.wkbPoint25D)
        g.AddPoint(*map(float, xyz0))
        f.SetGeometry(g)
        lyr.CreateFeature(f)

    # traces
    lyr = ds.CreateLayer("traces", srs, ogr.wkbLineString25D)
    lyr.CreateField(ogr.FieldDefn("Hole_ID", ogr.OFTString))
    lyr.CreateField(ogr.FieldDefn("Hole_Type", ogr.OFTString))
    lyr.CreateField(ogr.FieldDefn("Depth", ogr.OFTReal))
    for hid, (r, dep, xyz, _x0) in holes.items():
        if dep is None or len(dep) < 2:
            continue
        f = ogr.Feature(lyr.GetLayerDefn())
        f.SetField("Hole_ID", str(hid))
        if r.get("Hole_Type") is not None:
            f.SetField("Hole_Type", r.get("Hole_Type"))
        f.SetField("Depth", float(dep[-1]))
        g = ogr.Geometry(ogr.wkbLineString25D)
        for p in xyz:
            g.AddPoint(*map(float, p))
        f.SetGeometry(g)
        lyr.CreateFeature(f)

    # intervals
    taken = {"collars", "traces", "q2t4_source"}
    for spec in intervals:
        rows, ifields = spec["rows"], spec["fields"]
        lname = spec.get("name") or "intervals"
        base, k = lname, 2
        while lname.lower() in taken:
            lname, k = f"{base}_{k}", k + 1
        taken.add(lname.lower())
        lyr = ds.CreateLayer(lname, srs, ogr.wkbLineString25D)
        used = set()
        names = _add_fields(lyr, ifields, used)
        for extra in ("q2t4_mid_x", "q2t4_mid_y", "q2t4_mid_z"):
            lyr.CreateField(ogr.FieldDefn(extra, ogr.OFTReal))
        n_ok = 0
        ds.StartTransaction()
        for r in rows:
            h = holes.get(hole_key(r.get(spec["id"])))
            d0, d1 = r.get(spec["from"]), r.get(spec["to"])
            if h is None or h[1] is None or d0 is None or d1 is None or d1 <= d0:
                report["missing_hole"][lname] += 1
                continue
            path = DS.interval_path(h[1], h[2], d0, d1)
            mid = DS.position(h[1], h[2], (d0 + d1) / 2.0)
            f = ogr.Feature(lyr.GetLayerDefn())
            for n, (fn, *_x) in zip(names, ifields):
                if r[fn] is not None:
                    f.SetField(n, r[fn])
            f.SetField("q2t4_mid_x", float(mid[0]))
            f.SetField("q2t4_mid_y", float(mid[1]))
            f.SetField("q2t4_mid_z", float(mid[2]))
            g = ogr.Geometry(ogr.wkbLineString25D)
            for p in path:
                g.AddPoint(*map(float, p))
            f.SetGeometry(g)
            lyr.CreateFeature(f)
            n_ok += 1
        ds.CommitTransaction()
        report["intervals"][lname] = (n_ok, len(rows))

    meta = ds.CreateLayer("q2t4_source", None, ogr.wkbNone)
    for k in ("source", "built", "method"):
        meta.CreateField(ogr.FieldDefn(k, ogr.OFTString))
    f = ogr.Feature(meta.GetLayerDefn())
    f.SetField("source", source)
    f.SetField("built", datetime.datetime.now().isoformat(timespec="seconds"))
    f.SetField("method", "minimum curvature (QGeo 3D)")
    meta.CreateFeature(f)
    ds = None
    return report
