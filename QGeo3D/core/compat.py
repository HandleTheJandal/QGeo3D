# -*- coding: utf-8 -*-
"""QGIS 3 (Qt5) / QGIS 4 (Qt6) differences in one place.

- QAction lives in QtWidgets under Qt5 and in QtGui under Qt6.
- Field types: QVariant.* (QGIS 3) or QMetaType.Type.* (QGIS 3.38+ / QGIS 4).
- QGIS enums: QGIS 4 (PyQt6) only knows the scoped spelling (e.g. QgsLayoutItemShape.Shape.Rectangle);
  enum() tries the given spellings in order and returns the first that exists.
"""

try:
    from qgis.PyQt.QtGui import QAction, QActionGroup      # Qt6
except ImportError:
    from qgis.PyQt.QtWidgets import QAction, QActionGroup  # Qt5

import qgis.core as _core


def enum(*names):
    """First of the dotted names (in qgis.core or qgis.gui) that resolves, e.g.
    enum('QgsLayoutExporter.ExportResult.Success', 'QgsLayoutExporter.Success')."""
    import qgis.gui as _gui
    for n in names:
        for mod in (_core, _gui):
            obj = mod
            try:
                for part in n.split("."):
                    obj = getattr(obj, part)
                return obj
            except AttributeError:
                continue
    raise AttributeError(f"none of {names} exists in this QGIS")


def field_type(kind):
    """Type for QgsField(name, type): 'int' | 'double' | 'string' | 'long'."""
    try:
        from qgis.PyQt.QtCore import QMetaType
        t = {"int": QMetaType.Type.Int, "double": QMetaType.Type.Double, "string": QMetaType.Type.QString,
             "long": QMetaType.Type.LongLong}[kind]
        from qgis.core import Qgis
        if Qgis.QGIS_VERSION_INT >= 33800:          # QgsField(QMetaType) since 3.38
            return t
    except (ImportError, AttributeError):
        pass
    from qgis.PyQt.QtCore import QVariant
    return {"int": QVariant.Int, "double": QVariant.Double, "string": QVariant.String,
            "long": QVariant.LongLong}[kind]


QT6 = False
try:
    from qgis.PyQt.QtCore import PYQT_VERSION_STR
    QT6 = PYQT_VERSION_STR.split(".")[0] == "6"
except ImportError:
    pass


def gdal_soft(fn):
    """Run fn with GDAL / OGR / OSR errors as return codes, not exceptions. QGIS 4's bindings raise on
    every CPL error, so a PROJ database out of step with GDAL (mixed OSGeo4W installs) breaks any
    GeoPackage read or write that touches a CRS; QGIS 3 only logged it."""
    import functools

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            from osgeo import gdal, ogr, osr
        except ImportError:
            return fn(*args, **kwargs)
        mods = [m for m in (gdal, ogr, osr) if hasattr(m, "GetUseExceptions") and m.GetUseExceptions()]
        for m in mods:
            m.DontUseExceptions()
        try:
            return fn(*args, **kwargs)
        finally:
            for m in mods:
                m.UseExceptions()
    return wrapper
