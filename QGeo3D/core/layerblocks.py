# -*- coding: utf-8 -*-
"""Block models from point layers: GeoPackage / shapefile layers or any point layer in QGIS
(including the block-centre GeoPackages QGeo 3D writes). Centre and size fields are recognised
under the usual names; the point geometry is the centre when there are no centre fields, and a
regular block size is taken from the centre spacing when there are no size fields."""

import numpy as np
from .compat import gdal_soft

GEOM = ("_GEOM_X", "_GEOM_Y", "_GEOM_Z")


def _val(v):
    if v is None:
        return None
    try:
        if hasattr(v, "isNull") and v.isNull():
            return None
    except TypeError:
        pass
    return v


def layer_table(layer, selected_only=False):
    """(fields, rows) of a QGIS point layer in the table format the block-model reader takes;
    the point geometry is added as _GEOM_X / _GEOM_Y / _GEOM_Z."""
    fl = layer.fields()
    names = [f.name() for f in fl]
    numeric = [f.isNumeric() for f in fl]
    fields = [(n, "R" if isnum else "C", 8, 0) for n, isnum in zip(names, numeric)] + [(g, "R", 8, 0) for g in GEOM]
    rows = []
    feats = layer.getSelectedFeatures() if selected_only else layer.getFeatures()
    for ft in feats:
        attrs = ft.attributes()
        r = {}
        for n, isnum, v in zip(names, numeric, attrs):
            v = _val(v)
            if v is not None and isnum:
                try:
                    v = float(v)
                except (TypeError, ValueError):
                    v = None
            r[n] = v
        g = ft.geometry()
        if g is not None and not g.isEmpty():
            try:
                p = next(g.vertices())
                r[GEOM[0]], r[GEOM[1]] = p.x(), p.y()
                z = p.z()
                r[GEOM[2]] = z if z == z else None
            except StopIteration:
                pass
        rows.append(r)
    return fields, rows


def block_columns(fields, rows):
    """{xc, yc, zc, dx, dy, dz} for BlockModel.from_table (sizes may be numbers) and notes.
    Raises ValueError when there is no centre or the block size can't be told."""
    from .micromine import block_cols
    from .tables import guess
    from .blockmodel import infer_spacing
    real = [f for f in fields if f[0] not in GEOM]
    cols = block_cols(real)
    notes = []
    if cols is not None:
        _prefer_geometry(cols, rows, notes)
        return cols, notes
    cols = {r: guess(real, r) for r in ("xc", "yc", "zc", "dx", "dy", "dz")}
    if not (cols["xc"] and cols["yc"] and cols["zc"]):
        if not any(r.get(GEOM[2]) is not None for r in rows):
            if cols["xc"] or cols["yc"] or cols["zc"]:
                raise ValueError("only some centre fields found and the points have no Z")
            raise ValueError("the points have no Z and there are no centre X / Y / Z fields")
        cols["xc"], cols["yc"], cols["zc"] = GEOM
        notes.append("block centres from the point geometry")
    if not (cols["dx"] and cols["dy"] and cols["dz"]):
        c = [np.array([np.nan if r.get(cols[k]) is None else r.get(cols[k]) for r in rows], float)
             for k in ("xc", "yc", "zc")]
        for i, k in enumerate(("dx", "dy", "dz")):
            if cols[k]:
                continue
            cols[k] = infer_spacing(c[i], [c[j] for j in range(3) if j != i]) or None
        notes.append("regular block size from the centre spacing (no size fields)")
    return cols, notes


def _prefer_geometry(cols, rows, notes):
    """Centre fields can be in another CRS (or model coordinates) than the points - e.g. the
    block-centre GeoPackages QGeo 3D writes keep the source file's XC/YC/ZC but place the points
    in the project CRS. The layer's CRS belongs to the geometry, so use it when they disagree."""
    import math
    n = d = 0
    for r in rows[:2000]:
        g = [r.get(k) for k in GEOM]
        c = [r.get(cols[k]) for k in ("xc", "yc", "zc")]
        if None in g or None in c:
            continue
        n += 1
        if math.dist(g, c) > 0.01:
            d += 1
    if n and d > n * 0.05:
        cols["xc"], cols["yc"], cols["zc"] = GEOM
        notes.append("centre fields differ from the point positions (another CRS or model coordinates) - "
                     "the point positions are used")


@gdal_soft
def point_layers(path):
    """Names of the point layers in a GeoPackage / shapefile (feature count, has Z)."""
    from osgeo import ogr
    ds = ogr.Open(path)
    out = []
    if ds is None:
        return out
    for i in range(ds.GetLayerCount()):
        L = ds.GetLayerByIndex(i)
        gt = ogr.GT_Flatten(L.GetGeomType())
        if gt in (ogr.wkbPoint, ogr.wkbMultiPoint):
            out.append((L.GetName(), L.GetFeatureCount(), ogr.GT_HasZ(L.GetGeomType()) != 0))
    return out


@gdal_soft
def face_layers(path):
    """Names of the layers that can be wireframes (polygons, TINs, polyhedral surfaces)."""
    from osgeo import ogr
    ds = ogr.Open(path)
    out = []
    if ds is None:
        return out
    for i in range(ds.GetLayerCount()):
        L = ds.GetLayerByIndex(i)
        gt = ogr.GT_Flatten(L.GetGeomType())
        if gt in (ogr.wkbPolygon, ogr.wkbMultiPolygon, ogr.wkbTIN, ogr.wkbPolyhedralSurface, ogr.wkbTriangle,
                  ogr.wkbUnknown):
            out.append(L.GetName())
    return out
