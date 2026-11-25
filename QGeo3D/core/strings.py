# -*- coding: utf-8 -*-
"""3D strings (LineStringZ) stored in a GeoPackage, plus polyline payloads for the 3D view.

Strings are written with the data provider, so the open project is not touched. If the
project isn't ringfenced the same GeoPackage layer can also be added to the project; edits
made there (2D editing) are picked up through the layer's dataChanged signal.
"""

import contextlib
import base64
import datetime
import os

import numpy as np
from qgis.PyQt.QtCore import QObject, QTimer, pyqtSignal
from qgis.core import (Qgis, QgsCoordinateReferenceSystem, QgsFeature, QgsField, QgsFields, QgsGeometry,
                       QgsLineString, QgsPoint, QgsProject, QgsVectorFileWriter, QgsVectorLayer, QgsWkbTypes)

from .compat import enum, field_type
from .q2thook import is_protected, log
from .compat import gdal_soft

LAYER_NAME = "strings"
LAYER_FIELD = "layer"          # string layer (a group of strings) inside the one strings table
# edit-buffer edits emit layerModified (not dataChanged); commit/rollback/provider reloads the others
WATCH_SIGNALS = ("layerModified", "afterRollBack", "afterCommitChanges", "dataChanged")


def _b64(arr):
    return base64.b64encode(np.ascontiguousarray(arr).tobytes()).decode("ascii")


def polyline_payload(polylines, color="#00c8ff", snap="ends", point_size=0, point_color=None):
    """polylines: list of (n,3) float64 arrays in map coords -> overlay spec for Q2T4.setOverlay.
    snap: 'ends' (first/last vertex of each polyline, i.e. interval boundaries) or 'all'."""
    polylines = [np.asarray(p, dtype=np.float64).reshape(-1, 3) for p in polylines if len(p) >= 2]
    if not polylines:
        return None
    allp = np.vstack(polylines)
    mn, mx = allp.min(axis=0), allp.max(axis=0)
    offset = np.round((mn + mx) / 2.0, 3)
    seg, snp, base = [], [], 0
    for p in polylines:
        n = len(p)
        idx = np.arange(base, base + n - 1)
        seg.append(np.column_stack([idx, idx + 1]).reshape(-1))
        snp.append([base, base + n - 1] if snap == "ends" else np.arange(base, base + n))
        base += n
    spec = {
        "offset": [float(x) for x in offset],
        "pos": _b64((allp - offset).astype("<f4")),
        "seg": _b64(np.concatenate(seg).astype("<u4")),
        "snap": _b64(np.unique(np.concatenate([np.asarray(s) for s in snp])).astype("<u4")),
        "color": color,
        "pointSize": point_size,
    }
    if point_color:
        spec["pointColor"] = point_color
    return spec


def mixed_payload(polylines, color="#00c8ff", point_size=4, max_points=300000):
    """Overlay of polylines (>= 2 points, drawn as lines) and single points (drawn as dots); every
    vertex snaps. Point clouds above max_points are thinned for display (not for the GeoPackage)."""
    polylines = [np.asarray(p, dtype=np.float64).reshape(-1, 3) for p in polylines if len(p)]
    if not polylines:
        return None
    lines = [p for p in polylines if len(p) >= 2]
    singles = [p for p in polylines if len(p) == 1]
    pts1 = np.vstack(singles) if singles else np.zeros((0, 3))
    if len(pts1) > max_points:
        pts1 = pts1[np.linspace(0, len(pts1) - 1, max_points).astype(np.int64)]
    allp = np.vstack(lines + [pts1]) if lines else pts1
    mn, mx = allp.min(axis=0), allp.max(axis=0)
    offset = np.round((mn + mx) / 2.0, 3)
    seg, base = [], 0
    for p in lines:
        n = len(p)
        idx = np.arange(base, base + n - 1)
        seg.append(np.column_stack([idx, idx + 1]).reshape(-1))
        base += n
    spec = {
        "offset": [float(x) for x in offset],
        "pos": _b64((allp - offset).astype("<f4")),
        "seg": _b64((np.concatenate(seg) if seg else np.zeros(0)).astype("<u4")),
        "snap": _b64(np.arange(len(allp)).astype("<u4")),
        "color": color,
        "pointSize": point_size if len(pts1) else 0,
    }
    return spec


def layer_polylines(layer, dest_crs=None):
    """All (Multi)LineString(Z) parts of a QGIS vector layer as (n,3) arrays in dest_crs."""
    from qgis.core import QgsCoordinateTransform
    xf = None
    if dest_crs is not None and layer.crs().isValid() and layer.crs() != dest_crs:
        xf = QgsCoordinateTransform(layer.crs(), dest_crs, QgsProject.instance())
    out = []
    for f in layer.getFeatures():
        g = f.geometry()
        if g.isNull() or g.type() != Qgis.GeometryType.Line:
            continue
        if xf:
            g.transform(xf)
        for part in g.constParts():
            pts = [(v.x(), v.y(), v.z() if v.is3D() else 0.0) for v in part.vertices()]
            if len(pts) >= 2:
                out.append(np.array(pts))
    return out


class StringStore(QObject):

    changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.path = None
        self.layer = None
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(300)
        self._debounce.timeout.connect(self.changed)

    # ---------------------------------------------------------------- file
    def open(self, path, crs):
        if is_protected(path):
            raise PermissionError(f"Ringfenced location, not writing strings to: {path}")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if not os.path.exists(path):
            self._create(path, crs)
        else:                                    # files made while GDAL stored 'unknown' CRSs: give the code back
            try:
                from .crs import repair_gpkg_crs
                fixed = repair_gpkg_crs(path, crs)
                if fixed:
                    log(f"{os.path.basename(path)}: coordinate system {crs.authid()} restored on {', '.join(fixed)}")
            except Exception as e:
                log(f"Strings GeoPackage CRS check failed: {e}")
        from .crs import open_gpkg_layer
        lyr = open_gpkg_layer(path, LAYER_NAME, "QGeo 3D strings")
        if not lyr.isValid():
            raise IOError(f"Cannot open strings layer in {path}")
        self._disconnect()
        self.path, self.layer = path, lyr
        self.ensure_layer_field()
        self._connect(lyr)
        log(f"Strings GeoPackage: {path}")
        self.changed.emit()

    def ensure_layer_field(self):
        """Strings files from before 0.15.21 have no 'layer' field: add it; each string's layer = its name."""
        lyr = self.layer
        try:
            if lyr is None or lyr.fields().indexOf(LAYER_FIELD) >= 0 or is_protected(self.path or ""):
                return
            pr = lyr.dataProvider()
            if not pr.addAttributes([QgsField(LAYER_FIELD, field_type("string"))]):
                return
            lyr.updateFields()
            idx = lyr.fields().indexOf(LAYER_FIELD)
            if idx >= 0:
                import re                         # copies were named "<name> copy (copy ...)": same layer
                pr.changeAttributeValues({f.id(): {idx: (re.sub(r"( copy)+$", "", f["name"] or "") or "strings")}
                                          for f in lyr.getFeatures()})
                lyr.reload()
            log(f"Strings: 'layer' field added to {self.path}")
        except Exception as e:
            log(f"Strings: could not add the 'layer' field: {e}")

    def _create(self, path, crs):
        fields = QgsFields()
        fields.append(QgsField("string_no", field_type("int")))
        fields.append(QgsField("name", field_type("string")))
        fields.append(QgsField("layer", field_type("string")))
        fields.append(QgsField("section", field_type("string")))
        fields.append(QgsField("snaps", field_type("string")))
        fields.append(QgsField("created", field_type("string")))
        opts = QgsVectorFileWriter.SaveVectorOptions()
        opts.driverName = "GPKG"
        opts.layerName = LAYER_NAME
        w = QgsVectorFileWriter.create(path, fields, enum("Qgis.WkbType.LineStringZ", "QgsWkbTypes.LineStringZ"),
                                       crs if crs.isValid() else QgsCoordinateReferenceSystem(),
                                       QgsProject.instance().transformContext(), opts)
        if w.hasError() != enum("QgsVectorFileWriter.WriterError.NoError", "QgsVectorFileWriter.NoError"):
            raise IOError(w.errorMessage())
        del w
        try:                                     # keep the EPSG code even if GDAL's PROJ is out of step
            from .crs import stamp_gpkg_crs
            stamp_gpkg_crs(path, crs, [LAYER_NAME])
        except Exception as e:
            log(f"Strings GeoPackage CRS not stamped: {e}")

    def _connect(self, lyr):
        for name in WATCH_SIGNALS:
            getattr(lyr, name).connect(self._debounce.start)

    def _disconnect(self):
        if self.layer is not None:
            for name in WATCH_SIGNALS:
                try:
                    getattr(self.layer, name).disconnect(self._debounce.start)
                except (TypeError, RuntimeError):
                    pass

    def close(self):
        """Let go of the GeoPackage (another project is opening: it has its own strings file)."""
        with contextlib.suppress(Exception):
            self._disconnect()
        self.layer = None
        self.path = None
        self.changed.emit()

    def adoptProjectLayer(self, layer):
        """Use a project layer (same GeoPackage) so 2D edits are seen live."""
        self._disconnect()
        self.layer = layer
        self.ensure_layer_field()
        self._connect(layer)
        try:                                     # removed from the project: go back to the file itself
            layer.willBeDeleted.connect(self._adoptedGone)
        except (AttributeError, TypeError, RuntimeError):
            pass
        self.changed.emit()

    def _adoptedGone(self):
        """The project layer the strings were shown through is being deleted (removed from the project):
        keep working on the GeoPackage through a layer of our own."""
        path = self.path
        self.layer = None
        if path:
            QTimer.singleShot(0, lambda p=path: self._reopen(p))

    def _reopen(self, path):
        if self.layer is not None or self.path != path:
            return
        try:
            from .crs import open_gpkg_layer
            lyr = open_gpkg_layer(path, LAYER_NAME, "QGeo 3D strings")
            if lyr.isValid():
                self.layer = lyr
                self.ensure_layer_field()
                self._connect(lyr)
                log(f"Strings layer removed from the project - still using {path}")
        except Exception as e:
            log(f"Strings: could not reopen {path}: {e}")
        self.changed.emit()

    # -------------------------------------------------------------- strings
    def next_number(self):
        if self.layer is None:
            return 1
        idx = self.layer.fields().indexOf("string_no")
        mx = self.layer.maximumValue(idx)
        return (int(mx) if mx not in (None, "") else 0) + 1

    def add(self, pts, name="", section="", kinds=None, layer=None):
        if self.layer is None:
            raise RuntimeError("No strings GeoPackage open")
        if is_protected(self.path):
            raise PermissionError("Ringfenced location")
        f = QgsFeature(self.layer.fields())
        pts = self.to_layer_crs(pts)        # digitised in the project CRS
        f.setGeometry(QgsGeometry(QgsLineString([QgsPoint(x, y, z) for x, y, z in pts])))
        no = self.next_number()
        f["string_no"] = no
        f["name"] = name
        if self.layer.fields().indexOf(LAYER_FIELD) >= 0:
            f[LAYER_FIELD] = layer or name or "strings"
        f["section"] = section
        f["snaps"] = ",".join(kinds or [])
        f["created"] = datetime.datetime.now().isoformat(timespec="seconds")
        if self.layer.isEditable():
            ok = self.layer.addFeature(f)          # into the user's edit session
        else:
            ok, _ = self.layer.dataProvider().addFeatures([f])
            self.layer.reload()
        self.layer.triggerRepaint()
        if not ok:
            raise IOError("Failed to add string")
        self.changed.emit()
        return no

    def delete_last(self):
        if self.layer is None:
            return None
        idx = self.layer.fields().indexOf("string_no")
        mx = self.layer.maximumValue(idx)
        if mx in (None, ""):
            return None
        fids = [f.id() for f in self.layer.getFeatures(f'"string_no" = {int(mx)}')]
        if self.layer.isEditable():
            self.layer.deleteFeatures(fids)
        else:
            self.layer.dataProvider().deleteFeatures(fids)
            self.layer.reload()
        self.layer.triggerRepaint()
        self.changed.emit()
        return int(mx)

    def polylines(self):
        """Strings in the project CRS (the GeoPackage keeps its own CRS)."""
        if self.layer is None:
            return []
        return layer_polylines(self.layer, QgsProject.instance().crs())

    def to_layer_crs(self, pts):
        """Project-CRS points -> the strings layer's CRS (for writing)."""
        from .crs import transform_xyz
        if self.layer is None:
            return pts
        return transform_xyz(pts, QgsProject.instance().crs(), self.layer.crs())

    def count(self):
        return self.layer.featureCount() if self.layer is not None else 0


# ------------------------------------------------------------------ wireframes
WF_LAYER = "wireframes"


def string_layers(store):
    """{string_no: layer name} - the 'layer' field, else the string's name, else 'strings'."""
    out = {}
    if store.layer is None:
        return out
    has = store.layer.fields().indexOf(LAYER_FIELD) >= 0
    for f in store.layer.getFeatures():
        no = f["string_no"]
        if no in (None, ""):
            continue
        lay = f[LAYER_FIELD] if has else None
        out[int(no)] = (str(lay) if lay not in (None, "") and str(lay) != "NULL" else "") or f["name"] or "strings"
    return out


def string_records(store):
    """[(string_no, name, pts (n,3), closed)] for every string in the store, by number."""
    out = []
    if store.layer is None:
        return out
    for f in store.layer.getFeatures():
        g = f.geometry()
        if g.isNull():
            continue
        pts = np.array([(v.x(), v.y(), v.z() if v.is3D() else 0.0) for v in g.vertices()])
        from .crs import transform_xyz
        pts = transform_xyz(pts, store.layer.crs(), QgsProject.instance().crs())
        if len(pts) < 2:
            continue
        closed = len(pts) > 3 and np.linalg.norm(pts[0] - pts[-1]) < 1e-3
        no = f["string_no"]
        out.append((int(no) if no not in (None, "") else -1, f["name"] or "", pts, bool(closed)))
    out.sort(key=lambda r: r[0])
    return out


def _layer_srs(path, src):
    """SRS of the strings layer for a new layer beside it. GDAL's own PROJ can be out of step with
    QGIS's (mixed OSGeo4W installs) and fail here, so fall back to the CRS QGIS reads from the file."""
    if src is None:
        return None
    with contextlib.suppress(Exception):
        srs = src.GetSpatialRef()
        if srs is not None and (srs.GetAuthorityCode(None) or srs.IsProjected() or srs.IsGeographic()):
            return srs
    try:
        from .crs import gpkg_crs, ogr_srs
        c = gpkg_crs(path, src.GetName())
        return ogr_srs(c) if c is not None else None
    except Exception:
        return None


def _ogr_get(feat, name):
    """Field value, or None when the layer has no such field."""
    i = feat.GetFieldIndex(name)
    return feat.GetField(i) if i >= 0 and feat.IsFieldSetAndNotNull(i) else None


@gdal_soft
def save_wireframe(path, mesh, name="", string_nos=(), sg=None, stats=None, crs=None):
    """Append a mesh as one TIN Z feature to layer 'wireframes' of a GeoPackage (the strings GeoPackage,
    or a separate one, created if missing - crs: the CRS the vertices are in, for a new file)."""
    from osgeo import ogr
    if is_protected(path):
        raise PermissionError(f"Ringfenced location, not writing wireframes to: {path}")
    stats = stats or mesh.stats()
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        ogr.GetDriverByName("GPKG").CreateDataSource(path)
    ds = ogr.Open(path, 1)
    if ds is None:
        raise IOError(f"Cannot open {path} for writing")
    lyr = ds.GetLayerByName(WF_LAYER)
    if lyr is None:
        src = ds.GetLayerByName(LAYER_NAME)
        srs = _layer_srs(path, src) if src is not None else None
        if srs is None and crs is not None:
            from .crs import ogr_srs
            srs = ogr_srs(crs)
        lyr = ds.CreateLayer(WF_LAYER, srs, ogr.wkbTINZ)
    have = {lyr.GetLayerDefn().GetFieldDefn(i).GetName().lower() for i in range(lyr.GetLayerDefn().GetFieldCount())}
    for fname, ftype in (("wf_no", ogr.OFTInteger), ("name", ogr.OFTString), ("strings", ogr.OFTString),
                         ("triangles", ogr.OFTInteger), ("closed", ogr.OFTInteger),
                         ("area_m2", ogr.OFTReal), ("volume_m3", ogr.OFTReal),
                         ("sg", ogr.OFTReal), ("tonnes", ogr.OFTReal), ("created", ogr.OFTString)):
        if fname not in have:           # also repairs a layer left half-made by an earlier failed write
            lyr.CreateField(ogr.FieldDefn(fname, ftype))
    nos = [_ogr_get(f, "wf_no") for f in lyr]
    lyr.ResetReading()
    no = max([int(n) for n in nos if n is not None] + [lyr.GetFeatureCount()]) + 1
    tin = ogr.Geometry(ogr.wkbTINZ)
    v = mesh.vertices
    for a, b, c in mesh.triangles:
        ring = ogr.Geometry(ogr.wkbLinearRing)
        for i in (a, b, c, a):
            ring.AddPoint(float(v[i, 0]), float(v[i, 1]), float(v[i, 2]))
        tri = ogr.Geometry(ogr.wkbTriangleZ)
        tri.AddGeometry(ring)
        tin.AddGeometry(tri)
    feat = ogr.Feature(lyr.GetLayerDefn())
    feat.SetGeometry(tin)
    feat.SetField("wf_no", no)
    feat.SetField("name", name)
    feat.SetField("strings", ",".join(str(n) for n in string_nos))
    feat.SetField("triangles", int(stats["triangles"]))
    feat.SetField("closed", 1 if stats.get("closed") else 0)
    feat.SetField("area_m2", float(stats.get("area", 0.0)))
    if stats.get("volume") is not None:
        feat.SetField("volume_m3", float(stats["volume"]))
        if sg:
            feat.SetField("sg", float(sg))
            feat.SetField("tonnes", float(stats["volume"]) * float(sg))
    feat.SetField("created", datetime.datetime.now().isoformat(timespec="seconds"))
    err = lyr.CreateFeature(feat)
    ds = None
    if err != 0:
        raise IOError("Failed to write wireframe feature")
    with contextlib.suppress(Exception):
        from .crs import repair_gpkg_crs, stamp_gpkg_crs
        if crs is not None:
            repair_gpkg_crs(path, crs)
    return no


@gdal_soft
def load_wireframes(path):
    """[(wf_no, name, Mesh)] from the 'wireframes' layer (empty list if none)."""
    from osgeo import ogr
    from .mesh import _collect_ogr, _finish, _new_counters
    out = []
    ds = ogr.Open(path)
    lyr = ds.GetLayerByName(WF_LAYER) if ds is not None else None
    if lyr is None:
        return out
    for feat in lyr:
        tris, counters = [], _new_counters()
        _collect_ogr(feat.GetGeometryRef(), tris, counters)
        no = _ogr_get(feat, "wf_no")
        if no is None:
            no = feat.GetFID()
        nm = _ogr_get(feat, "name") or f"wireframe {no}"
        out.append((no, nm, _finish(tris, counters, nm, path)))
    return out


# ------------------------------------------------------------------ saved sections
SEC_LAYER = "sections"


@gdal_soft
def save_section(path, sec, name):
    """Store a Section (its current position) as a LineString in layer 'sections'."""
    from osgeo import ogr
    if is_protected(path):
        raise PermissionError(f"Ringfenced location, not writing sections to: {path}")
    ds = ogr.Open(path, 1)
    if ds is None:
        raise IOError(f"Cannot open {path} for writing")
    lyr = ds.GetLayerByName(SEC_LAYER)
    if lyr is None:
        src = ds.GetLayerByName(LAYER_NAME)
        lyr = ds.CreateLayer(SEC_LAYER, _layer_srs(path, src), ogr.wkbLineString)
        for fname, ftype in (("name", ogr.OFTString), ("width", ogr.OFTReal), ("azimuth", ogr.OFTReal),
                             ("created", ogr.OFTString)):
            lyr.CreateField(ogr.FieldDefn(fname, ftype))
    g = ogr.Geometry(ogr.wkbLineString)
    g.AddPoint_2D(*sec.a)
    g.AddPoint_2D(*sec.b)
    f = ogr.Feature(lyr.GetLayerDefn())
    f.SetGeometry(g)
    f.SetField("name", name)
    f.SetField("width", float(sec.width))
    f.SetField("azimuth", float(sec.azimuth))
    f.SetField("created", datetime.datetime.now().isoformat(timespec="seconds"))
    err = lyr.CreateFeature(f)
    fid = f.GetFID()
    ds = None
    if err != 0:
        raise IOError("Failed to save section")
    return fid


@gdal_soft
def load_sections(path):
    """[{fid, name, a, b, width}] from layer 'sections' (empty list if none)."""
    from osgeo import ogr
    if not path or not os.path.exists(path):
        return []
    ds = ogr.Open(path)
    lyr = ds.GetLayerByName(SEC_LAYER) if ds is not None else None
    out = []
    if lyr is None:
        return out
    for f in lyr:
        g = f.GetGeometryRef()
        if g is None or g.GetPointCount() < 2:
            continue
        p0, p1 = g.GetPoint_2D(0), g.GetPoint_2D(g.GetPointCount() - 1)
        out.append({"fid": f.GetFID(), "name": _ogr_get(f, "name") or f"Section {f.GetFID()}",
                    "a": p0, "b": p1, "width": _ogr_get(f, "width") or 10.0})
    return out


@gdal_soft
def delete_section(path, fid):
    from osgeo import ogr
    if is_protected(path):
        raise PermissionError("Ringfenced location")
    ds = ogr.Open(path, 1)
    lyr = ds.GetLayerByName(SEC_LAYER) if ds is not None else None
    if lyr is not None:
        lyr.DeleteFeature(int(fid))
    ds = None


@gdal_soft
def rename_saved_wireframe(path, wf_no, name):
    """Rename wireframe wf_no in the 'wireframes' layer of a GeoPackage. True if found."""
    from osgeo import ogr
    if is_protected(path):
        raise PermissionError(f"Ringfenced location, not changing: {path}")
    ds = ogr.Open(path, 1)
    lyr = ds.GetLayerByName(WF_LAYER) if ds is not None else None
    if lyr is None:
        return False
    found = False
    lyr.ResetReading()
    for f in [f for f in lyr if _ogr_get(f, "wf_no") == int(wf_no)]:
        f.SetField("name", name)
        lyr.SetFeature(f)
        found = True
    ds = None
    return found


@gdal_soft
def gpkg_layer_count(path, layer_name):
    """Number of features in a layer of a GeoPackage, or None if it has no such layer."""
    from osgeo import ogr
    if not path or not os.path.exists(path):
        return None
    ds = ogr.Open(path, 0)
    lyr = ds.GetLayerByName(layer_name) if ds is not None else None
    n = lyr.GetFeatureCount() if lyr is not None else None
    ds = None
    return n


@gdal_soft
def delete_gpkg_layer(path, layer_name):
    """Drop a whole layer (table) from a GeoPackage; the file itself stays. True if it was there."""
    from osgeo import ogr
    if is_protected(path):
        raise PermissionError(f"Ringfenced location, not changing: {path}")
    ds = ogr.Open(path, 1)
    if ds is None:
        raise IOError(f"Cannot open {path}")
    for i in range(ds.GetLayerCount()):
        if ds.GetLayerByIndex(i).GetName() == layer_name:
            if ds.DeleteLayer(i) != 0:
                raise IOError(f"Could not delete layer '{layer_name}'")
            ds = None
            return True
    ds = None
    return False


@gdal_soft
def delete_saved_wireframe(path, wf_no):
    """Delete wireframe wf_no from the 'wireframes' layer of a GeoPackage. True if one went."""
    from osgeo import ogr
    if is_protected(path):
        raise PermissionError(f"Ringfenced location, not changing: {path}")
    ds = ogr.Open(path, 1)
    lyr = ds.GetLayerByName(WF_LAYER) if ds is not None else None
    if lyr is None:
        return False
    fids = [f.GetFID() for f in lyr if _ogr_get(f, "wf_no") == int(wf_no)]
    for fid in fids:
        lyr.DeleteFeature(fid)
    ds = None
    return bool(fids)
