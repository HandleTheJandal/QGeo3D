# -*- coding: utf-8 -*-
"""Coordinate systems for imported mining files.

Datamine, Vulcan, Surpac, Micromine, DXF and OBJ files don't record a coordinate system,
so the user is asked for it on import (core decides *whether* to ask; gui/crsdialog.py asks).
Data are transformed into the project CRS for the 3D view; the source CRS is kept with each
item so it can be changed afterwards.
"""

import contextlib
import os

import numpy as np

NO_CRS_EXT = {".dxf", ".obj", ".dm", ".dtm", ".str", ".mdl", ".bmf", ".dat", ".dhdb"}


def _qcrs(x):
    from qgis.core import QgsCoordinateReferenceSystem
    if x is None:
        return None
    if isinstance(x, QgsCoordinateReferenceSystem):
        return x
    c = QgsCoordinateReferenceSystem(str(x))
    if not c.isValid():
        c = QgsCoordinateReferenceSystem.fromWkt(str(x))
    return c if c.isValid() else None


def file_crs(path):
    """CRS stored with the file (GeoPackage/shapefile SRS or a .prj next to it), else None."""
    from qgis.core import QgsCoordinateReferenceSystem
    base, ext = os.path.splitext(path)
    for prj in (base + ".prj", path + ".prj"):
        if os.path.exists(prj):
            with open(prj, "r", encoding="latin-1") as f:
                c = QgsCoordinateReferenceSystem.fromWkt(f.read())
            if c.isValid():
                return c
    if ext.lower() in NO_CRS_EXT:
        return None
    with contextlib.suppress(Exception):
        from osgeo import ogr
        ds = ogr.Open(path)
        if ds is not None:
            for i in range(ds.GetLayerCount()):
                L = ds.GetLayerByIndex(i)
                if ext.lower() == ".gpkg":
                    c = gpkg_crs(path, L.GetName())
                    if c is not None:
                        return c
                srs = L.GetSpatialRef()
                if srs is not None:
                    c = QgsCoordinateReferenceSystem.fromWkt(srs.ExportToWkt())
                    if c.isValid():
                        return c
    return None


def describe_extent(xyz):
    """(text, hint) about where coordinates of an (n,3) array sit, to help pick a CRS."""
    p = np.asarray(xyz, float).reshape(-1, 3)
    p = p[np.isfinite(p).all(axis=1)]
    if not len(p):
        return "", ""
    lo, hi = p.min(axis=0), p.max(axis=0)
    text = f"X {lo[0]:,.1f} – {hi[0]:,.1f}   Y {lo[1]:,.1f} – {hi[1]:,.1f}   Z {lo[2]:,.1f} – {hi[2]:,.1f}"
    x, y = (lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2
    if abs(x) <= 180 and abs(y) <= 90 and hi[0] - lo[0] < 10:
        hint = "These look like latitude / longitude degrees."
    elif 100000 <= x <= 900000 and 1000000 <= y <= 10000000:
        hint = ("These look like UTM / MGA metres. The zone and datum (e.g. GDA2020 or GDA94) can't be "
                "told from the numbers.")
    elif abs(x) < 100000 and abs(y) < 100000:
        hint = "These look like a local mine grid."
    else:
        hint = ""
    return text, hint


def _osr(crs):
    from osgeo import osr
    from qgis.core import QgsCoordinateReferenceSystem
    s = osr.SpatialReference()
    s.ImportFromWkt(crs.toWkt(_wkt_gdal()))
    s.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    # GDAL's PROJ database can be out of step with QGIS's (mixed OSGeo4W installs): the WKT then
    # imports as an 'unknown' CRS. Put the name and authority back so the file still says EPSG:n.
    auth = crs.authid()
    if ":" in auth and not s.GetAuthorityCode(None):
        org, code = auth.split(":", 1)
        node = "PROJCS" if s.IsProjected() else ("GEOGCS" if s.IsGeographic() else None)
        if node and code.isdigit():
            with contextlib.suppress(Exception):
                s.SetAuthority(node, org, int(code))
                s.SetAttrValue(node, crs.description())
    return s


def same(a, b):
    a, b = _qcrs(a), _qcrs(b)
    return a is None or b is None or a == b


def transform_xyz(xyz, src, dst):
    """Transform x/y of an (n,3) array from src to dst (Z unchanged). Returns a new array."""
    src, dst = _qcrs(src), _qcrs(dst)
    p = np.array(xyz, float).reshape(-1, 3)
    if same(src, dst) or not len(p):
        return p
    # QGIS's transformer: same PROJ as the rest of QGIS, and it honours the project's chosen
    # datum transformations (GDAL's own PROJ may be a different, mismatched install).
    from qgis.core import QgsCoordinateTransform, QgsProject, QgsPointXY
    ct = QgsCoordinateTransform(src, dst, QgsProject.instance())
    ok = np.isfinite(p[:, :2]).all(axis=1)
    out = p.copy()
    for i in np.nonzero(ok)[0]:
        q = ct.transform(QgsPointXY(float(p[i, 0]), float(p[i, 1])))
        out[i, 0], out[i, 1] = q.x(), q.y()
    return out


def rigid_placement(origin, rot, pivot, src, dst):
    """Re-place a rigid model (world = origin + rot . model) in another CRS: the model's pivot
    point is transformed exactly and its axes are turned by the local grid convergence.
    Block sizes are kept (scale factor ignored, < 0.1% for projected CRSs over a mine)."""
    src, dst = _qcrs(src), _qcrs(dst)
    origin, rot, pivot = np.asarray(origin, float), np.asarray(rot, float), np.asarray(pivot, float)
    if same(src, dst):
        return origin.copy(), rot.copy()
    c = origin + rot @ pivot
    ex = c + rot @ np.array([100.0, 0, 0])
    w = transform_xyz(np.vstack([c, ex]), src, dst)
    d0 = (rot @ np.array([1.0, 0, 0]))[:2]
    d1 = (w[1] - w[0])[:2]
    t = np.arctan2(d1[1], d1[0]) - np.arctan2(d0[1], d0[0])      # convergence (radians, CCW)
    Rz = np.array([[np.cos(t), -np.sin(t), 0], [np.sin(t), np.cos(t), 0], [0, 0, 1]])
    rot2 = Rz @ rot
    origin2 = w[0] - rot2 @ pivot
    return origin2, rot2


def ogr_srs(crs):
    """osr.SpatialReference for writing GeoPackages (works for CRSs without an EPSG code)."""
    return _osr(_qcrs(crs))


def make_srs(x):
    """osr.SpatialReference from an EPSG int, an osr object, or anything _qcrs accepts."""
    from osgeo import osr
    if isinstance(x, osr.SpatialReference):
        return x
    if isinstance(x, (int, np.integer)) or (isinstance(x, str) and x.isdigit()):
        s = osr.SpatialReference()
        with contextlib.suppress(Exception):
            if s.ImportFromEPSG(int(x)) == 0:
                s.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
                return s
        x = f"EPSG:{int(x)}"     # GDAL can't look it up - let QGIS do it
    return ogr_srs(x)


def short_name(crs):
    """Compact CRS label for the 3D view, e.g. 'MGA94 50', 'MGA2020 50', 'AMG84 50',
    'WGS84 UTM 50S'; otherwise the CRS description."""
    import re
    c = _qcrs(crs)
    if c is None:
        return ""
    d = c.description()
    m = re.match(r"^\s*(GDA2020|GDA94|AGD84|AGD66)\s*/\s*(?:MGA|AMG) zone (\d+)", d, re.I)
    if m:
        datum, z = m.group(1).upper(), m.group(2)
        return {"GDA2020": f"MGA2020 {z}", "GDA94": f"MGA94 {z}", "AGD84": f"AMG84 {z}", "AGD66": f"AMG66 {z}"}[datum]
    m = re.match(r"^\s*WGS\s*84\s*/\s*UTM zone (\d+[NS]?)", d, re.I)
    if m:
        return f"WGS84 UTM {m.group(1).upper()}"
    return d or c.authid()


def _wkt_gdal():
    from .compat import enum
    return enum("Qgis.CrsWktVariant.PreferredGdal", "QgsCoordinateReferenceSystem.WktVariant.WKT_PREFERRED_GDAL",
                "QgsCoordinateReferenceSystem.WKT_PREFERRED_GDAL")


def ensure_proj():
    """Make GDAL's own PROJ find its database. Some installs (seen with QGIS 4 on Windows) leave
    osgeo.osr without it ("proj_create: no database context specified"), so writing a GeoPackage
    with a CRS fails; QGIS knows where proj.db is, so give GDAL the same search paths."""
    from osgeo import osr
    s = osr.SpatialReference()
    with contextlib.suppress(Exception):
        if s.ImportFromEPSG(4326) == 0:
            return True
    paths = []
    with contextlib.suppress(Exception):
        from qgis.core import QgsProjUtils
        paths = [p for p in QgsProjUtils.searchPaths() if p]
    import os
    import sys
    for cand in (os.environ.get("PROJ_DATA"), os.environ.get("PROJ_LIB"),
                 os.path.join(os.path.dirname(os.path.dirname(sys.executable)), "share", "proj"),
                 os.path.join(os.path.dirname(sys.executable), "..", "share", "proj")):
        if cand and os.path.isfile(os.path.join(cand, "proj.db")) and cand not in paths:
            paths.append(os.path.normpath(cand))
    if paths:
        osr.SetPROJSearchPaths(paths)
    try:
        return osr.SpatialReference().ImportFromEPSG(4326) == 0
    except Exception:
        return False


def proj_db_problem():
    """A sentence saying the PROJ database QGIS reads is from another PROJ version (so EPSG codes,
    datum shifts such as GDA94 <-> GDA2020 and CRS names silently fail) and how to fix it, or None.
    Seen on OSGeo4W installs whose share/proj/proj.db was left behind by an older PROJ."""
    import glob
    import os
    import sqlite3
    try:
        from qgis.core import QgsProjUtils
        major, minor = QgsProjUtils.projVersionMajor(), QgsProjUtils.projVersionMinor()
        paths = [p for p in QgsProjUtils.searchPaths() if p]
    except Exception:
        return None
    db = next((os.path.join(p, "proj.db") for p in paths if os.path.isfile(os.path.join(p, "proj.db"))), None)
    if db is None:
        return None

    def meta(path):
        try:
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                return dict(con.execute("select key, value from metadata").fetchall())
            finally:
                con.close()
        except Exception:
            return {}
    m = meta(db)
    ver = m.get("PROJ.VERSION", "")
    try:
        lay_minor = int(m.get("DATABASE.LAYOUT.VERSION.MINOR", 99))
    except ValueError:
        lay_minor = 99
    db_major = int(ver.split(".")[0]) if ver[:1].isdigit() else major
    if db_major >= major and not (major >= 9 and lay_minor < 4):
        return None
    good = []
    for cand in glob.glob(r"C:\Program Files\QGIS*\share\proj\proj.db") + glob.glob(r"C:\OSGeo4W*\share\proj\proj.db"):
        cm = meta(cand)
        if cm.get("PROJ.VERSION", "").split(".")[:2] == [str(major), str(minor)]:
            good.append(os.path.dirname(cand))
    fix = (f"In QGIS: Settings > Options > System > Environment, tick Use custom variables and add "
           f"Overwrite PROJ_DATA = {good[0]}, then restart QGIS" if good else
           "Upgrade the OSGeo4W 'proj' and 'proj-data' packages (OSGeo4W Setup > Advanced Install)")
    return (f"PROJ database mismatch: QGIS uses PROJ {major}.{minor} but reads {db} from PROJ {ver or '?'}. "
            f"EPSG codes, datum shifts (e.g. GDA94 <-> GDA2020, ~1.5 m) and CRS names fail silently - "
            f"projects can show 'unknown' CRSs. Fix: {fix}.")


def gpkg_crs(path, layer):
    """The CRS a GeoPackage layer was written with, read straight from gpkg_spatial_ref_sys and
    resolved by QGIS's own PROJ (not GDAL's)."""
    import sqlite3
    from qgis.core import QgsCoordinateReferenceSystem
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            row = con.execute(
                "select s.organization, s.organization_coordsys_id, s.definition from gpkg_geometry_columns g "
                "join gpkg_spatial_ref_sys s on s.srs_id = g.srs_id where g.table_name = ?", (layer,)).fetchone()
        finally:
            con.close()
    except Exception:
        return None
    if not row:
        return None
    org, code, wkt = row
    if org and str(org).upper() not in ("NONE", "") and code and int(code) > 0:
        c = QgsCoordinateReferenceSystem(f"{str(org).upper()}:{int(code)}")
        if c.isValid():
            return c
    if wkt and wkt.strip() and wkt.strip().lower() != "undefined":
        c = QgsCoordinateReferenceSystem.fromWkt(wkt)
        if c.isValid():
            return c
    return None


def open_gpkg_layer(path, layer, name):
    """QgsVectorLayer for one GeoPackage layer, with its CRS checked against the file. When GDAL's
    PROJ database is out of step with QGIS (a mixed OSGeo4W install, seen with QGIS 4) the layer
    otherwise loads with an 'unknown' CRS; QGIS can still read the right one from the file."""
    from qgis.core import QgsVectorLayer
    lyr = QgsVectorLayer(f"{path}|layername={layer}", name, "ogr")
    if lyr.isValid() and not lyr.crs().authid():
        c = gpkg_crs(path, layer)
        if c is not None and c.authid():
            lyr.setCrs(c)
    return lyr


def stamp_gpkg_crs(path, crs, tables=None):
    """Write crs (with its authority code) as the SRS of GeoPackage tables (all geometry tables when
    tables is None) straight into gpkg_spatial_ref_sys. Used where GDAL's own PROJ database is out of
    step and GDAL would store an 'unknown' CRS. Returns the tables changed."""
    import sqlite3
    from qgis.core import Qgis, QgsCoordinateReferenceSystem
    c = _qcrs(crs)
    if c is None or ":" not in c.authid():
        return []
    org, code = c.authid().split(":", 1)
    if not code.isdigit():
        return []
    code = int(code)
    wkt = c.toWkt(_wkt_gdal())
    con = sqlite3.connect(path)
    try:
        cur = con.cursor()
        row = cur.execute("select srs_id from gpkg_spatial_ref_sys where upper(organization)=? and organization_coordsys_id=?",
                          (org.upper(), code)).fetchone()
        if row:
            sid = row[0]
        else:
            taken = {r[0] for r in cur.execute("select srs_id from gpkg_spatial_ref_sys")}
            sid = code if code not in taken else max(taken | {100000}) + 1
            cur.execute("insert into gpkg_spatial_ref_sys (srs_name, srs_id, organization, organization_coordsys_id, "
                        "definition, description) values (?,?,?,?,?,?)",
                        (c.description(), sid, org.upper(), code, wkt, c.description()))
        names = [r[0] for r in cur.execute("select table_name from gpkg_geometry_columns")]
        done = []
        for t in names:
            if tables is not None and t not in tables:
                continue
            cur.execute("update gpkg_geometry_columns set srs_id=? where table_name=?", (sid, t))
            cur.execute("update gpkg_contents set srs_id=? where table_name=?", (sid, t))
            done.append(t)
        con.commit()
        return done
    finally:
        con.close()


def same_projection(a, b):
    """True when two CRSs have the same projection and parameters (datum names aside) - to recognise
    an 'unknown' CRS that GDAL wrote for a known one."""
    import re
    a, b = _qcrs(a), _qcrs(b)
    if a is None or b is None:
        return False

    def key(c):
        toks = [t for t in c.toProj().split() if not re.match(r"\+(datum|ellps|towgs84|no_defs|type)\b", t)]
        return " ".join(sorted(toks))
    return key(a) == key(b)


def repair_gpkg_crs(path, intended):
    """GeoPackage tables QGeo 3D wrote with an authority-less CRS whose projection matches `intended`:
    give them intended's EPSG code. Returns the tables repaired."""
    import sqlite3
    from qgis.core import QgsCoordinateReferenceSystem
    c = _qcrs(intended)
    if c is None or ":" not in c.authid():
        return []
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        rows = con.execute("select g.table_name, s.organization, s.definition from gpkg_geometry_columns g "
                           "left join gpkg_spatial_ref_sys s on s.srs_id = g.srs_id").fetchall()
        con.close()
    except Exception:
        return []
    bad = [t for t, org, wkt in rows if (org or "NONE").upper() == "NONE" and wkt and wkt.strip().lower() != "undefined"
           and same_projection(QgsCoordinateReferenceSystem.fromWkt(wkt), c)]
    return stamp_gpkg_crs(path, c, bad) if bad else []
