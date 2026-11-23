# -*- coding: utf-8 -*-
"""Drag and drop of mining files.

* 3D window and QGeo 3D panel: every format the plugin reads (wireframes, block models,
  strings, points, drillholes, CSV / Excel tables). Files the plugin doesn't know are passed
  on to Qgis2threejs (it loads glTF models / point clouds dropped on its view).
* QGIS main window / map canvas: only the mining formats QGIS can't open itself (.tridb, .dm,
  .bmf, .mdl, .dtm, .DHDB, Micromine / Surpac .str, Micromine .DAT); DXF, CSV, Excel,
  GeoPackage... keep QGIS's own behaviour there.
Drops are handled after the drop event returns, so Explorer isn't held while a CRS dialog is open.
"""

import os

from qgis.gui import QgsCustomDropHandler
from qgis.PyQt.QtCore import QEvent, QObject, QTimer

MINING = {".tridb", ".dm", ".bmf", ".mdl", ".dtm", ".dhdb", ".str", ".dat", ".omf", ".msh"}
WIREFRAME = {".dxf", ".obj", ".gpkg", ".shp"}
TABLES = {".csv", ".txt", ".tsv", ".xlsx", ".xlsm", ".ods"}
ALL = MINING | WIREFRAME | TABLES


def ext(p):
    return os.path.splitext(p)[1].lower()


def is_micromine(path):
    try:
        with open(path, "rb") as f:
            return f.read(37) == b"THIS IS MICROMINE EXTENDED DATA FILE!"
    except OSError:
        return False


def _paths(mime):
    if not mime.hasUrls():
        return []
    return [u.toLocalFile() for u in mime.urls() if u.isLocalFile() and u.toLocalFile()]


class DropFilter(QObject):
    """Event filter for the 3D view and the panel. on_unknown(urls): files we don't read (3D view only)."""

    def __init__(self, dock, on_unknown=None, parent=None):
        super().__init__(parent or dock)
        self.dock = dock
        self.on_unknown = on_unknown

    def eventFilter(self, obj, ev):
        t = ev.type()
        if t in (QEvent.Type.DragEnter, QEvent.Type.DragMove):
            ps = _paths(ev.mimeData())
            if any(ext(p) in ALL for p in ps):
                ev.acceptProposedAction()
                return True
            return False
        if t == QEvent.Type.Drop:
            ps = _paths(ev.mimeData())
            ours = [p for p in ps if ext(p) in ALL]
            if not ours:
                return False
            ev.acceptProposedAction()
            others = [u for u in ev.mimeData().urls() if not (u.isLocalFile() and ext(u.toLocalFile()) in ALL)]
            if others and self.on_unknown is not None:
                QTimer.singleShot(0, lambda urls=list(others): self.on_unknown(urls))
            QTimer.singleShot(0, lambda: self.dock.openDropped(ours))
            return True
        return False


class MiningDropHandler(QgsCustomDropHandler):
    """QGIS main window / canvas: mining formats QGIS can't open itself."""

    def __init__(self, dock):
        super().__init__()
        self.dock = dock

    def customUriProviderKey(self):
        return "qgeo3d"

    def handleFileDrop(self, file):
        e = ext(file)
        if e not in MINING:
            return False
        if e == ".dat" and not is_micromine(file):
            return False
        QTimer.singleShot(0, lambda: self.dock.openDropped([file]))
        return True


class ViewDropCatcher(QObject):
    """Qgis2threejs 3.x draws the 3D view in another process and doesn't pass drops on to QGIS
    (its view app has '# TODO: fileDropped'). While files are dragged over any part of the 3D
    window (toolbar, panels, status bar) a see-through catcher is laid over the view; dropping on
    it hands the files to on_drop(urls)."""

    def __init__(self, window, view_widget, on_drop):
        super().__init__(window)
        from qgis.PyQt.QtWidgets import QApplication
        self.w, self.view, self.on_drop = window, view_widget, on_drop
        self.overlay = None
        QApplication.instance().installEventFilter(self)

    def remove(self):
        from qgis.PyQt.QtWidgets import QApplication
        try:
            QApplication.instance().removeEventFilter(self)
        except RuntimeError:
            pass
        if self.overlay is not None:
            try:
                self.overlay.deleteLater()
            except RuntimeError:
                pass
            self.overlay = None

    def _make(self):
        from qgis.PyQt.QtCore import Qt
        from qgis.PyQt.QtWidgets import QLabel
        o = _Catcher(self)
        o.setParent(self.w, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint)
        o.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        o.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        o.setAcceptDrops(True)
        lab = QLabel("Drop here to add to the 3D view", o)
        lab.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lab.setStyleSheet("background: rgba(30, 90, 160, 60); border: 3px dashed rgba(30, 90, 160, 200);"
                          "color: #10305a; font-size: 16pt; font-weight: bold;")
        o.label = lab
        self.overlay = o
        return o

    def show(self):
        from qgis.PyQt.QtCore import QPoint, QRect
        try:
            v = self.view
            if v is None or not v.isVisible():
                return
            r = QRect(v.mapToGlobal(QPoint(0, 0)), v.size())
        except RuntimeError:
            return
        o = self.overlay or self._make()
        o.setGeometry(r)
        o.label.setGeometry(0, 0, r.width(), r.height())
        o.show()
        o.raise_()

    def hide(self):
        if self.overlay is not None:
            try:
                self.overlay.hide()
            except RuntimeError:
                self.overlay = None

    def eventFilter(self, obj, ev):
        if ev.type() != QEvent.Type.DragEnter:
            return False
        try:
            if obj is self.overlay or not hasattr(obj, "window") or obj.window() is not self.w:
                return False
        except RuntimeError:
            return False
        if any(_paths(ev.mimeData())):
            self.show()
        return False


try:
    from qgis.PyQt.QtWidgets import QWidget as _QW
except ImportError:          # pragma: no cover
    _QW = object


class _Catcher(_QW):
    def __init__(self, owner):
        super().__init__()
        self.owner = owner

    def dragEnterEvent(self, ev):
        if _paths(ev.mimeData()):
            ev.acceptProposedAction()

    def dragMoveEvent(self, ev):
        ev.acceptProposedAction()

    def dragLeaveEvent(self, ev):
        self.owner.hide()

    def dropEvent(self, ev):
        urls = list(ev.mimeData().urls())
        ev.acceptProposedAction()
        self.owner.hide()
        QTimer.singleShot(0, lambda: self.owner.on_drop(urls))
