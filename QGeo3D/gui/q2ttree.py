# -*- coding: utf-8 -*-
"""Make the Qgis2threejs window's layer list mirror the QGIS Layers panel.

Qgis2threejs groups its layer list by type (DEM / Point / Line / Polygon / Point Cloud).
This replaces that, for one window, with the QGIS layer-tree structure: same groups,
nesting and order (and group expanded state). Layers Qgis2threejs can't show are left out,
empty groups are hidden, and layers that aren't in the QGIS project (flat planes, point
cloud URLs, DEM provider plugins) go in a final "Other 3D layers" group.

Only methods of that one tree-view instance are replaced, and restore() puts Qgis2threejs's
own type grouping back. Logical depth (group = 0, layer = 1, material = 2) is kept so the
Qgis2threejs context menus, double-click and material switching keep working unchanged.
"""

from qgis.PyQt.QtCore import QObject, Qt, QTimer
from qgis.PyQt.QtGui import QStandardItem
from qgis.PyQt.QtWidgets import QMenu
from qgis.core import QgsApplication, QgsIconUtils, QgsLayerTreeGroup, QgsProject

KIND = Qt.ItemDataRole.UserRole + 2         # "group" | "other" | "layer" | "mtl"
ID = Qt.ItemDataRole.UserRole + 1           # Q2T's own item.data() role

PATCHED = ("addLayer", "itemFromLayerId", "uncheckAll", "clearLayers", "removeLayer",
           "indexDepth", "currentChanged", "showContextMenu", "updateLayerMaterials")


def _walk(item):
    for r in range(item.rowCount()):
        ch = item.child(r)
        yield ch
        yield from _walk(ch)


class QgisTreeLayout(QObject):

    def __init__(self, window):
        super().__init__(window)
        self.wnd = window
        self.tv = window.ui.treeView
        self._orig = {}
        self.active = False
        self._groups = {}               # path tuple -> QStandardItem
        self._other = None
        self._rebuildTimer = QTimer(self)
        self._rebuildTimer.setSingleShot(True)
        self._rebuildTimer.setInterval(250)
        self._rebuildTimer.timeout.connect(self.rebuild)

    # ------------------------------------------------------------ on / off
    def apply(self):
        if self.active:
            return
        tv = self.tv
        for name in PATCHED:
            self._orig[name] = getattr(tv, name)
        tv.addLayer = self.addLayer
        tv.itemFromLayerId = self.itemFromLayerId
        tv.uncheckAll = self.uncheckAll
        tv.clearLayers = self.clearLayers
        tv.removeLayer = self.removeLayer
        tv.indexDepth = self.indexDepth
        tv.currentChanged = self.currentChanged
        tv.showContextMenu = self.showContextMenu
        tv.updateLayerMaterials = self.updateLayerMaterials
        # Q2T connected these bound methods in setup(): reconnect to ours
        tv.customContextMenuRequested.disconnect()
        tv.customContextMenuRequested.connect(self.showContextMenu)
        self.active = True
        self._watch(True)
        self.rebuild()

    def restore(self):
        if not self.active:
            return
        self._watch(False)
        tv = self.tv
        for name in PATCHED:
            try:
                delattr(tv, name)               # back to the class methods
            except AttributeError:
                pass
        tv.customContextMenuRequested.disconnect()
        tv.customContextMenuRequested.connect(tv.showContextMenu)
        self.active = False
        # rebuild Q2T's type groups
        root = tv.model().invisibleRootItem()
        root.removeRows(0, root.rowCount())
        tv.layerGroupItems = {}
        for typ, name in tv.LAYER_GROUP_ITEMS:
            item = QStandardItem(name)
            item.setData(typ)
            item.setIcon(tv.icons[typ])
            item.setEditable(False)
            tv.layerGroupItems[typ] = item
            root.appendRow([item])
        tv.addLayers(self.wnd.settings.layers())
        tv.expandAll()

    def _watch(self, on):
        root = QgsProject.instance().layerTreeRoot()
        sigs = (root.addedChildren, root.removedChildren, root.nameChanged, root.layerOrderChanged)
        for s in sigs:
            try:
                if on:
                    s.connect(self._rebuildTimer.start)
                else:
                    s.disconnect(self._rebuildTimer.start)
            except (TypeError, RuntimeError):
                pass

    # -------------------------------------------------------------- build
    def rebuild(self):
        if not self.active:
            return
        tv = self.tv
        layers = list(self.wnd.settings.layers())
        order = {tl.layerId(): i for i, tl in enumerate(QgsProject.instance().layerTreeRoot().findLayers())}
        layers.sort(key=lambda l: order.get(l.layerId, len(order) + 1))     # stable: extras keep their order
        self.clearLayers()
        for lyr in layers:
            self.addLayer(lyr)
        self._applyExpanded()

    def _groupPath(self, layerId):
        """[(path, QgsLayerTreeGroup), ...] from the top group down, or None if not in the project."""
        node = QgsProject.instance().layerTreeRoot().findLayer(layerId)
        if node is None:
            return None
        chain = []
        p = node.parent()
        while p is not None and p.parent() is not None:     # stop at the root
            chain.append(p)
            p = p.parent()
        chain.reverse()
        out, path = [], ()
        for g in chain:
            path = path + (g.parent().children().index(g),)
            out.append((path, g))
        return out

    def _parentItem(self, layer):
        root = self.tv.model().invisibleRootItem()
        groups = self._groupPath(layer.layerId)
        if groups is None:
            if self._other is None:
                it = QStandardItem("Other 3D layers")
                it.setData("other", KIND)
                it.setIcon(QgsApplication.getThemeIcon("/mActionFolder.svg"))
                it.setEditable(False)
                root.appendRow([it])
                self._other = it
            return self._other
        parent = root
        for path, g in groups:
            it = self._groups.get(path)
            if it is None:
                it = QStandardItem(g.name())
                it.setData("group", KIND)
                it.setData(path, Qt.ItemDataRole.UserRole + 3)
                it.setIcon(QgsApplication.getThemeIcon("/mActionFolder.svg"))
                it.setEditable(False)
                parent.appendRow([it])
                self._groups[path] = it
            parent = it
        return parent

    def _applyExpanded(self):
        tv = self.tv
        root = QgsProject.instance().layerTreeRoot()
        for path, it in self._groups.items():
            node = root
            for i in path:
                node = node.children()[i]
            tv.setExpanded(it.index(), isinstance(node, QgsLayerTreeGroup) and node.isExpanded())
        if self._other is not None:
            tv.setExpanded(self._other.index(), True)

    # ------------------------------------------------- replaced methods
    def addLayer(self, layer):
        item = QStandardItem(layer.name)
        item.setCheckable(True)
        item.setData(layer.layerId)
        item.setData("layer", KIND)
        item.setEditable(False)
        ml = QgsProject.instance().mapLayer(layer.layerId)
        if ml is not None:
            item.setIcon(QgsIconUtils.iconForLayer(ml))
        if layer.visible:
            item.setCheckState(Qt.CheckState.Checked)
            font = item.font()
            font.setBold(True)
            item.setFont(font)
        self._parentItem(layer).appendRow([item])
        self.updateLayerMaterials(item)
        return item

    def updateLayerMaterials(self, layerItem, layer=None):
        self._orig["updateLayerMaterials"](layerItem, layer)
        for r in range(layerItem.rowCount()):
            layerItem.child(r).setData("mtl", KIND)

    def _layerItems(self):
        for it in _walk(self.tv.model().invisibleRootItem()):
            if it.data(KIND) == "layer":
                yield it

    def itemFromLayerId(self, layerId):
        for it in self._layerItems():
            if it.data() == layerId:
                return it
        return None

    def uncheckAll(self):
        for it in list(self._layerItems()):
            it.setCheckState(Qt.CheckState.Unchecked)

    def clearLayers(self):
        root = self.tv.model().invisibleRootItem()
        root.removeRows(0, root.rowCount())
        self._groups = {}
        self._other = None

    def removeLayer(self, layerId):
        it = self.itemFromLayerId(layerId)
        if it is None:
            return
        parent = it.parent() or self.tv.model().invisibleRootItem()
        parent.removeRow(it.row())
        # drop groups left empty
        while parent is not self.tv.model().invisibleRootItem() and parent.rowCount() == 0:
            gp = parent.parent() or self.tv.model().invisibleRootItem()
            if parent is self._other:
                self._other = None
            else:
                self._groups = {k: v for k, v in self._groups.items() if v is not parent}
            gp.removeRow(parent.row())
            parent = gp

    def indexDepth(self, idx):
        """Logical depth: 0 group (or empty space), 1 layer, 2 material."""
        if not idx.isValid():
            return -1
        return {"layer": 1, "mtl": 2}.get(self.tv.model().data(idx, KIND), 0)

    def currentChanged(self, current, previous):
        from qgis.PyQt.QtWidgets import QTreeView
        QTreeView.currentChanged(self.tv, current, previous)
        tv = self.tv
        if not current.isValid() or tv.model().data(current, KIND) != "mtl":
            return
        layer = tv.layerFromIndex(current.parent())
        if layer:
            layer.properties["mtlId"] = tv.model().data(current, ID)
            layer = layer.clone()
            try:                                    # Qgis2threejs 3.x: build options are an argument
                from Qgis2threejs.core.exportsettings import BuildDEMOptions
            except ImportError:
                BuildDEMOptions = None
            if BuildDEMOptions is not None:
                tv.controller.taskManager.addBuildLayerTask(layer, BuildDEMOptions(onlyMaterial=True))
            else:                                   # 2.x: on the layer
                layer.opt.onlyMaterial = True
                tv.controller.taskManager.addBuildLayerTask(layer)
        item = tv.model().itemFromIndex(current)
        parent = item.parent()
        font = item.font()
        for row in range(parent.rowCount()):
            font.setBold(row == item.row())
            parent.child(row).setFont(font)

    def showContextMenu(self, pos):
        tv = self.tv
        idx = tv.indexAt(pos)
        kind = tv.model().data(idx, KIND) if idx.isValid() else None
        m = None
        if kind == "layer":
            layerId = tv.model().data(idx, ID) or ""
            m = getattr(tv, "contextMenuPC", None) if layerId.startswith("pc:") else \
                tv.contextMenuFP if layerId.startswith("fp:") else tv.contextMenuLyr
            m = m or tv.contextMenuLyr          # Qgis2threejs 3.x has no point-cloud menu
        elif kind == "mtl":
            m = tv.contextMenuMtl
        else:                           # group, "Other 3D layers" or empty space
            m = QMenu(tv)
            for name in ("actionAddPlane", "actionAddPointCloudLayer"):    # the latter: 2.x only
                act = getattr(self.wnd.ui, name, None)
                if act is not None:
                    m.addAction(act)
        m.exec(tv.mapToGlobal(pos))
