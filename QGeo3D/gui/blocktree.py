# -*- coding: utf-8 -*-
"""Block-centre layers in the 3D window's layer list open as QGeo 3D block models.

Ticking a point layer of block centres (a *_blocks GeoPackage, a "(block centres)" layer, or any
point layer with block-size fields) in the Qgis2threejs layer list shows it as solid blocks -
the same as Blocks > Load - instead of Qgis2threejs points. Unticking hides the blocks; the tick
follows the block model's visibility in the panel. Every other layer behaves as before.
"""
import contextlib
import os
import re

from qgis.PyQt.QtCore import QObject, Qt, QTimer
from qgis.core import Qgis, QgsProject, QgsVectorLayer

from ..core.q2thook import log

TIP = "Shown as solid blocks by QGeo 3D (Blocks tab for colour, filter and display)"


def _source(ml):
    src = ml.source() or ""
    path = src.split("|")[0]
    m = re.search(r"layername=([^|]+)", src)
    return path, (m.group(1) if m else None)


def is_block_layer(ml):
    if not isinstance(ml, QgsVectorLayer) or ml.geometryType() != Qgis.GeometryType.Point:
        return False
    name = ml.name().lower()
    if "(block centres" in name:
        return True
    path, lname = _source(ml)
    stem = os.path.splitext(os.path.basename(path))[0].lower()
    if path.lower().endswith(".gpkg") and (stem.endswith("_blocks") or (lname or "").lower() == "blocks"):
        return True
    try:
        from ..core.micromine import block_cols, has_block_sizes
        num = [(f.name(), "R") for f in ml.fields() if f.isNumeric()]
        return block_cols(num) is not None or has_block_sizes(num)
    except Exception:
        return False


def bm_name(ml):
    """The name loadBlockModelLayer gives the model (so an already-loaded one is reused)."""
    name = ml.name()
    for tail in (" (block centres)", " (block centres, shown blocks only)"):
        name = name.replace(tail, "")
    path, _ = _source(ml)
    if path and name.lower() in ("blocks", "points", "layer", os.path.splitext(os.path.basename(path))[0].lower()):
        name = os.path.splitext(os.path.basename(path))[0]
        if name.lower().endswith("_blocks"):
            name = name[:-7]
    return name


class BlockLayerTree(QObject):

    def __init__(self, plugin, window):
        super().__init__(window)
        self.plugin = plugin
        self.hook = plugin.hook
        self.wnd = window
        self.tv = window.ui.treeView
        self.model = self.tv.model()
        self._sync = False
        self.active = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(200)
        self._timer.timeout.connect(self.syncChecks)

    def apply(self):
        if self.active:
            return
        try:
            self.model.dataChanged.disconnect(self.tv.treeDataChanged)
        except (TypeError, RuntimeError):
            return                                  # not the Qgis2threejs tree we know: leave it
        self.model.dataChanged.connect(self._changed)
        # context menu / double-click on a block layer act on the block model, not Q2T points
        self._rewire = []
        for sig, orig, ours in ((self.tv.actionZoomToLayer.triggered, self.tv.zoomToLayer, self._zoom),
                                (self.tv.actionProperties.triggered, self.tv.onDoubleClicked, self._props),
                                (self.tv.doubleClicked, self.tv.onDoubleClicked, self._props)):
            try:
                sig.disconnect(orig)
            except (TypeError, RuntimeError):
                continue
            sig.connect(ours)
            self._rewire.append((sig, orig, ours))
        self.model.rowsInserted.connect(self._timer.start)
        self.hook.itemsChanged.connect(self._timer.start)
        self.active = True
        self._timer.start()

    def restore(self):
        if not self.active:
            return
        self.active = False
        for sig, slot in ((self.model.dataChanged, self._changed), (self.model.rowsInserted, self._timer.start),
                          (self.hook.itemsChanged, self._timer.start)):
            try:
                sig.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
        try:
            self.model.dataChanged.connect(self.tv.treeDataChanged)
        except RuntimeError:
            pass
        for sig, orig, ours in getattr(self, "_rewire", []):
            try:
                sig.disconnect(ours)
                sig.connect(orig)
            except (TypeError, RuntimeError):
                pass
        self._rewire = []

    # ---------------------------------------------------------------- events
    def _changed(self, topLeft, bottomRight, roles=()):
        if self._sync:
            return
        item = self.model.itemFromIndex(topLeft)
        ml = QgsProject.instance().mapLayer(item.data()) if item is not None and isinstance(item.data(), str) else None
        if Qt.ItemDataRole.CheckStateRole in roles and ml is not None and is_block_layer(ml):
            self._toggle(item, ml, item.checkState() == Qt.CheckState.Checked)
            return
        self.tv.treeDataChanged(topLeft, bottomRight, roles)
        if Qt.ItemDataRole.CheckStateRole in roles and ml is not None:     # a Qgis2threejs layer: Objects list
            try:
                self.plugin._ensureDock().q2tLayerToggled(ml.id(), item.checkState() == Qt.CheckState.Checked)
            except Exception as e:
                log(f"Objects list: {e}")

    def _toggle(self, item, ml, on):
        # never let Qgis2threejs build it as points
        lyr = self.wnd.settings.getLayer(ml.id())
        if lyr is not None and lyr.visible:
            lyr.visible = False
            with contextlib.suppress(Exception):
                self.wnd.controller.hideLayer(lyr)
        self._bold(item, on)
        bid = "bm:" + bm_name(ml)
        if on:
            if bid in self.hook.blockModels:
                self.plugin._ensureDock().setBlocksShown(bid, True)
            else:
                dock = self.plugin._ensureDock()
                path, lname = _source(ml)
                bm = dock.loadBlockModelLayer(ml, path if os.path.isfile(path) else None, layer_name=lname)
                if bm is None:                      # cancelled / unreadable: untick again
                    self._setCheck(item, False)
                    return
        elif bid in self.hook.blockModels:
            self.plugin._ensureDock().setBlocksShown(bid, False)

    def _current(self):
        idx = self.tv.currentIndex()
        item = self.model.itemFromIndex(idx) if idx.isValid() else None
        lid = item.data() if item is not None else None
        ml = QgsProject.instance().mapLayer(lid) if isinstance(lid, str) else None
        return (item, ml) if ml is not None and is_block_layer(ml) else (None, None)

    def _ensureShown(self, item, ml):
        """Load / show the block model of a block layer; its id, or None."""
        bid = "bm:" + bm_name(ml)
        if bid not in self.hook.blockModels or self.plugin._ensureDock().blocksHidden(bid):
            self._setCheck(item, True)
            self._toggle(item, ml, True)
        return bid if bid in self.hook.blockModels else None

    def _zoom(self, *_):
        item, ml = self._current()
        if ml is None:
            return self._zoomQ2T()
        bid = self._ensureShown(item, ml)
        if bid:
            self.hook.zoomTo(bid)

    def setLayerShown(self, layerId, on):
        """Tick / untick a layer in the 3D window's layer list (Qgis2threejs builds / hides it)."""
        it = self.tv.itemFromLayerId(layerId)
        if it is not None and (it.checkState() == Qt.CheckState.Checked) != on:
            it.setCheckState(Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)

    def zoomLayer(self, layerId):
        """Objects list double-click: show the layer, then zoom to it (as the context menu does)."""
        it = self.tv.itemFromLayerId(layerId)
        if it is None:
            return
        self.setLayerShown(layerId, True)
        self.tv.setCurrentIndex(it.index())
        lyr = self.wnd.settings.getLayer(layerId)
        # a layer just ticked is still being built: give Qgis2threejs a moment before zooming
        QTimer.singleShot(0 if lyr is not None and self._hasObjects(lyr) else 1500, lambda: self._zoomQ2T(lyr))

    def _hasObjects(self, lyr):
        n = self.hook.js(f"(function(){{var L=app.scene.mapLayers[{int(lyr.jsLayerId)}];"
                         "return L ? (L.boundingBox().isEmpty() ? 0 : 1) : -1;})()", wait=True)
        return n != 0

    def _zoomQ2T(self, lyr=None):
        """Qgis2threejs' own zoom, which silently does nothing when the layer has no 3D objects -
        usually because its features lie outside the area the scene covers (the QGIS map extent)."""
        from qgis.PyQt.QtWidgets import QMessageBox
        lyr = lyr or self.tv.layerFromIndex(self.tv.currentIndex())
        if lyr is None:
            return
        if not lyr.visible:
            QMessageBox.information(self.wnd, "Zoom to Layer Objects", f"Tick {lyr.name} first to show it in 3D.")
            return
        n = self.hook.js(f"(function(){{var L=app.scene.mapLayers[{int(lyr.jsLayerId)}];"
                         "return L ? (L.boundingBox().isEmpty() ? 0 : 1) : -1;})()", wait=True)
        if n != 0:                                  # has objects (or can't tell): Qgis2threejs zooms
            return self.hook.js(f"app.cameraAction.zoomToLayer(app.scene.mapLayers[{int(lyr.jsLayerId)}])")
        ml = QgsProject.instance().mapLayer(lyr.layerId)
        if ml is None:
            return
        canvas = self.plugin.iface.mapCanvas()
        prj = QgsProject.instance().crs()
        text = (f"{lyr.name} has nothing in the 3D scene.\n\nThe scene only covers the area of the QGIS map, "
                "and this layer's features are outside it (or are still being built).")
        mb = QMessageBox(QMessageBox.Icon.Question, "Zoom to Layer Objects", text, parent=self.wnd)
        bSwitch = None
        if ml.crs().isValid() and prj.isValid() and ml.crs() != prj:
            mb.setInformativeText(f"The layer is in {ml.crs().authid()} ({ml.crs().description()}); the project "
                                  f"is in {prj.authid()} ({prj.description()}). If it's a different area, switch "
                                  "the project CRS to the layer's.")
            bSwitch = mb.addButton(f"Use {ml.crs().authid()} and go there", QMessageBox.ButtonRole.AcceptRole)
        bMove = mb.addButton("Move the map to it", QMessageBox.ButtonRole.AcceptRole)
        mb.addButton(QMessageBox.StandardButton.Cancel)
        mb.exec()
        if mb.clickedButton() not in (bSwitch, bMove) or mb.clickedButton() is None:
            return
        if mb.clickedButton() is bSwitch:
            QgsProject.instance().setCrs(ml.crs())
        from qgis.core import QgsCoordinateTransform
        dst = canvas.mapSettings().destinationCrs()
        ext = ml.extent()
        if ml.crs() != dst:
            ext = QgsCoordinateTransform(ml.crs(), dst, QgsProject.instance()).transformBoundingBox(ext)
        if ext.isEmpty():
            ext.grow(50)
        else:
            ext.scale(1.1)
        canvas.setExtent(ext)
        canvas.refresh()

    def _props(self, *_):
        item, ml = self._current()
        if ml is None:
            return self.tv.onDoubleClicked()
        if self._ensureShown(item, ml):
            self.plugin._ensureDock().showTab("Blocks")

    # ------------------------------------------------------------------ sync
    def _items(self, parent=None):
        parent = parent or self.model.invisibleRootItem()
        for r in range(parent.rowCount()):
            ch = parent.child(r)
            yield ch
            yield from self._items(ch)

    def syncChecks(self):
        """Ticks of block-centre layers follow their block model's visibility."""
        if not self.active:
            return
        dock = self.plugin._ensureDock()
        try:
            for it in list(self._items()):
                lid = it.data()
                if not isinstance(lid, str) or not it.isCheckable():
                    continue
                ml = QgsProject.instance().mapLayer(lid)
                if ml is None:
                    continue
                if not is_block_layer(ml):
                    if it.checkState() == Qt.CheckState.Checked and lid not in getattr(dock, "_q2tPinned", []):
                        dock.q2tLayerToggled(lid, True)        # ticked before: on the Objects list too
                    continue
                bid = "bm:" + bm_name(ml)
                shown = bid in self.hook.blockModels and not dock.blocksHidden(bid)
                it.setToolTip(TIP)
                lyr = self.wnd.settings.getLayer(lid)
                if lyr is not None and lyr.visible:      # ticked before QGeo 3D took it over
                    lyr.visible = False
                    with contextlib.suppress(Exception):
                        self.wnd.controller.hideLayer(lyr)
                if (it.checkState() == Qt.CheckState.Checked) != shown:
                    self._setCheck(it, shown)
                self._bold(it, shown)
        except RuntimeError as e:                    # window / model gone
            log(f"block layer list: {e}")

    def _setCheck(self, item, on):
        self._sync = True
        try:
            item.setCheckState(Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)
        finally:
            self._sync = False

    def _bold(self, item, on):
        self._sync = True
        try:
            f = item.font()
            f.setBold(on)
            item.setFont(f)
        finally:
            self._sync = False
