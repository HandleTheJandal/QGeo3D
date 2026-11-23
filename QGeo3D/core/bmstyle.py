# -*- coding: utf-8 -*-
"""Block-model colour classes <-> QGIS styles.

A style is {"kind": "range", "classes": [(lo, hi, colour, label), ...]} (lo / hi None = open end)
or {"kind": "cat", "classes": [(value, colour, label), ...]} (value None = all other values).
They come from a QGIS graduated / categorized renderer (a layer's, or a saved .qml), or are
edited by hand, and are kept per field on the block model (bm.styles)."""
import math

OPEN = 1e30


def _hex(sym):
    try:
        return sym.color().name()
    except Exception:
        return "#bdbdbd"


def from_renderer(r):
    """(field, style) from a QGIS renderer, or None (single symbol, rule-based, expressions ...)."""
    if r is None:
        return None
    t = r.type()
    if t == "graduatedSymbol":
        out = []
        for rg in r.ranges():
            if hasattr(rg, "renderState") and not rg.renderState():
                continue
            lo, hi = rg.lowerValue(), rg.upperValue()
            lo = None if (lo is None or lo <= -OPEN / 10 or not math.isfinite(lo)) else float(lo)
            hi = None if (hi is None or hi >= OPEN / 10 or not math.isfinite(hi)) else float(hi)
            out.append((lo, hi, _hex(rg.symbol()), rg.label() or _rlabel(lo, hi)))
        out.sort(key=lambda c: -math.inf if c[0] is None else c[0])
        return (r.classAttribute(), {"kind": "range", "classes": out}) if out else None
    if t == "categorizedSymbol":
        out = []
        for c in r.categories():
            if hasattr(c, "renderState") and not c.renderState():
                continue
            v = c.value()
            if v is None or v == "" or (hasattr(v, "isNull") and v.isNull()):
                v = None
            elif isinstance(v, (list, tuple)):      # a category holding several values: first one
                v = v[0] if v else None
            out.append((_num(v), _hex(c.symbol()), c.label() or str(v)))
        return (r.classAttribute(), {"kind": "cat", "classes": out}) if out else None
    return None


def _num(v):
    if v is None:
        return None
    try:
        f = float(v)
        return int(f) if f.is_integer() else f
    except (TypeError, ValueError):
        return str(v)


def _rlabel(lo, hi):
    if lo is None:
        return f"< {hi:g}"
    if hi is None:
        return f"≥ {lo:g}"
    return f"{lo:g} – {hi:g}"


def to_renderer(field, style):
    from qgis.core import (QgsCategorizedSymbolRenderer, QgsGraduatedSymbolRenderer, QgsMarkerSymbol,
                           QgsRendererCategory, QgsRendererRange)

    def sym(col):
        return QgsMarkerSymbol.createSimple({"name": "square", "size": "1.2", "color": col, "outline_style": "no"})
    if style["kind"] == "range":
        return QgsGraduatedSymbolRenderer(field, [
            QgsRendererRange(-OPEN if lo is None else float(lo), OPEN if hi is None else float(hi), sym(col), lab)
            for lo, hi, col, lab in style["classes"]])
    return QgsCategorizedSymbolRenderer(field, [
        QgsRendererCategory("" if v is None else v, sym(col), lab) for v, col, lab in style["classes"]])


def _scratch(bm):
    """A memory point layer with the block model's fields (for reading / writing .qml)."""
    from qgis.core import QgsField, QgsVectorLayer
    from qgis.PyQt.QtCore import QMetaType
    lyr = QgsVectorLayer("PointZ?crs=EPSG:4326", "style", "memory")
    pr = lyr.dataProvider()
    flds = []
    for n in bm.fields:
        num = n in bm.data and getattr(bm.data[n], "dtype", None) is not None and bm.data[n].dtype != object
        flds.append(QgsField(n, QMetaType.Type.Double if num else QMetaType.Type.QString))
    pr.addAttributes(flds)
    lyr.updateFields()
    return lyr


def load_qml(path, bm):
    """(field, style) of a saved QGIS style file, or raises ValueError saying why not."""
    lyr = _scratch(bm)
    msg, ok = lyr.loadNamedStyle(path)
    if not ok:
        raise ValueError(msg or "not a QGIS style file")
    res = from_renderer(lyr.renderer())
    if res is None:
        raise ValueError("the style isn't graduated or categorized on a field (single symbol, rules or "
                         "expressions can't colour blocks)")
    return res


def save_qml(path, bm, field, style):
    lyr = _scratch(bm)
    lyr.setRenderer(to_renderer(field, style))
    msg, ok = lyr.saveNamedStyle(path)
    if not ok:
        raise ValueError(msg or "could not write the style")


def match_field(bm, field):
    """The block model's own spelling of a field named in a style (case-insensitive), or None."""
    if not field:
        return None
    if field in bm.data:
        return field
    low = {f.lower(): f for f in bm.data}
    return low.get(str(field).strip('"').lower())
