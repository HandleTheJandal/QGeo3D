# -*- coding: utf-8 -*-
"""Section plot: the 3D view (with its grid, scale bar and labels) on a print layout with a
title block and legend, exported to PDF through QGIS's layout engine."""

import datetime
import html
import os
import tempfile

from qgis.core import (QgsLayoutExporter, QgsLayoutItemLabel, QgsLayoutItemPage, QgsLayoutItemPicture,
                       QgsLayoutItemShape, QgsLayoutPoint, QgsLayoutSize, QgsPrintLayout, QgsProject, QgsUnitTypes)
from qgis.PyQt.QtGui import QColor, QFont

from .compat import enum

PAGES = {"A4": (297, 210), "A3": (420, 297), "A2": (594, 420)}


def _mm(v):
    return enum("Qgis.LayoutUnit.Millimeters", "QgsUnitTypes.LayoutMillimeters")


def _label(layout, text, x, y, w, h, size=9, bold=False, html_mode=False, color="#222222"):
    lab = QgsLayoutItemLabel(layout)
    lab.setText(text)
    if html_mode:
        lab.setMode(enum("QgsLayoutItemLabel.Mode.ModeHtml", "QgsLayoutItemLabel.ModeHtml"))
    f = QFont("Arial", size)
    f.setBold(bold)
    try:
        from qgis.core import QgsTextFormat
        tf = QgsTextFormat()
        tf.setFont(f)
        tf.setSize(size)
        tf.setColor(QColor(color))
        lab.setTextFormat(tf)
    except Exception:
        lab.setFont(f)
        lab.setFontColor(QColor(color))
    layout.addLayoutItem(lab)
    lab.attemptMove(QgsLayoutPoint(x, y, _mm(0)))
    lab.attemptResize(QgsLayoutSize(w, h, _mm(0)))
    return lab


def section_pdf(path, image, title, subtitle, page="A3 landscape", legend=(), drawn_by="", keep=False):
    prj = QgsProject.instance()
    size, orient = (page.split() + ["landscape"])[:2]
    W, H = PAGES.get(size, PAGES["A3"])
    if orient.startswith("port"):
        W, H = H, W
    layout = QgsPrintLayout(prj)
    layout.initializeDefaults()
    name = f"QGeo 3D section - {title}"
    layout.setName(name)
    pg = layout.pageCollection().page(0)
    pg.setPageSize(QgsLayoutSize(W, H, _mm(0)))
    m = 8.0
    legend_w = 62.0 if legend else 0.0
    tb_h = 24.0
    # picture of the 3D view
    tmp = os.path.join(tempfile.gettempdir(), f"q2t4_plot_{datetime.datetime.now():%Y%m%d%H%M%S}.png")
    image.save(tmp, "PNG")
    pic = QgsLayoutItemPicture(layout)
    pic.setPicturePath(tmp)
    pic.setResizeMode(enum("QgsLayoutItemPicture.ResizeMode.Zoom", "QgsLayoutItemPicture.Zoom"))
    layout.addLayoutItem(pic)
    pw, ph = W - 2 * m - legend_w - (4 if legend else 0), H - 2 * m - tb_h - 4
    ar = image.width() / max(1, image.height())          # fit the picture box to the view's shape
    if pw / ph > ar:
        pw = ph * ar
    else:
        ph = pw / ar
    pic.attemptMove(QgsLayoutPoint(m, m, _mm(0)))
    pic.attemptResize(QgsLayoutSize(pw, ph, _mm(0)))
    frame = QgsLayoutItemShape(layout)
    frame.setShapeType(enum("QgsLayoutItemShape.Shape.Rectangle", "QgsLayoutItemShape.Rectangle"))
    try:
        from qgis.core import QgsFillSymbol
        frame.setSymbol(QgsFillSymbol.createSimple({"color": "0,0,0,0", "outline_color": "#333333",
                                                    "outline_width": "0.3"}))
    except Exception:
        pass
    layout.addLayoutItem(frame)
    frame.attemptMove(QgsLayoutPoint(m, m, _mm(0)))
    frame.attemptResize(QgsLayoutSize(pw, ph, _mm(0)))
    # legend
    if legend:
        legend_x = m + pw + 4
        rows = []
        for head, items in legend:
            rows.append(f"<div style='font-weight:bold;margin:6px 0 2px'>{html.escape(str(head))}</div>")
            for lab, col in items:
                sw = f"<span style='color:{col};font-size:11pt'>■</span>&nbsp;" if col else "·&nbsp;"
                rows.append(f"<div style='font-size:8pt;margin:1px 0'>{sw}{html.escape(str(lab))}</div>")
        _label(layout, "<div style='font-family:Arial;font-size:9pt'>" + "".join(rows) + "</div>",
               legend_x, m, legend_w, ph, size=8, html_mode=True)
    # title block
    y = H - m - tb_h
    box = QgsLayoutItemShape(layout)
    box.setShapeType(enum("QgsLayoutItemShape.Shape.Rectangle", "QgsLayoutItemShape.Rectangle"))
    try:
        from qgis.core import QgsFillSymbol
        box.setSymbol(QgsFillSymbol.createSimple({"color": "255,255,255,255", "outline_color": "#333333",
                                                  "outline_width": "0.3"}))
    except Exception:
        pass
    layout.addLayoutItem(box)
    box.attemptMove(QgsLayoutPoint(m, y, _mm(0)))
    box.attemptResize(QgsLayoutSize(W - 2 * m, tb_h, _mm(0)))
    _label(layout, title or "Section", m + 3, y + 2, W * 0.55, 9, size=14, bold=True)
    _label(layout, subtitle or "", m + 3, y + 11, W * 0.62, 11, size=8)
    meta = [f"Date: {datetime.date.today():%d %b %Y}"]
    if drawn_by:
        meta.append(f"Drawn: {drawn_by}")
    if prj.fileName():
        meta.append(f"Project: {os.path.basename(prj.fileName())}")
    meta.append("3D view: Qgis2threejs + QGeo 3D")
    _label(layout, "\n".join(meta), W - m - 70, y + 2, 67, tb_h - 4, size=7)
    ex = QgsLayoutExporter(layout)
    res = ex.exportToPdf(path, QgsLayoutExporter.PdfExportSettings())
    ok = res == enum("QgsLayoutExporter.ExportResult.Success", "QgsLayoutExporter.Success")
    if keep:
        mgr = prj.layoutManager()
        old = mgr.layoutByName(name)
        if old is not None:
            mgr.removeLayout(old)
        mgr.addLayout(layout)
    if not ok:
        raise IOError(f"PDF export failed (code {res})")
    return path
