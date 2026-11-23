"""Block model from GeoPackage: pick from the point layers of the GeoPackages QGeo 3D knows about."""
import datetime
import glob
import os

from qgis.PyQt.QtCore import QSettings, Qt
from qgis.PyQt.QtWidgets import (QAbstractItemView, QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QLabel,
                                 QLineEdit, QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout)
from qgis.core import QgsProject

from . import panelwidgets as P

BLOCKISH = ("blocks",)
SIZE_HINTS = ("_x", "_y", "_z", "xinc", "yinc", "zinc", "dx", "dy", "dz", "_size", "size_x", "xsize")


def _is_blockish(path, layer):
    stem = os.path.splitext(os.path.basename(path))[0].lower()
    return layer.lower() in BLOCKISH or stem.endswith("_blocks") or "block" in stem


def search_folders(extra=()):
    """Folders to look in: the plugin work folder, the import output folder, the project folder, the
    last block-model folder and the folders of GeoPackages already in the project."""
    from .dock import OUT_KEY_DIR, work_dir
    s = QSettings()
    out = []
    for d in (work_dir(), s.value(OUT_KEY_DIR, "") or "", s.value("/QGeo3D/lastBmDir", "") or "",
              os.path.dirname(QgsProject.instance().fileName() or ""), *extra):
        if d and os.path.isdir(d):
            out.append(os.path.normpath(d))
    for lyr in QgsProject.instance().mapLayers().values():
        src = (lyr.source() or "").split("|")[0]
        if src.lower().endswith(".gpkg") and os.path.isfile(src):
            out.append(os.path.normpath(os.path.dirname(src)))
    seen, res = set(), []
    for d in out:
        k = os.path.normcase(d)
        if k not in seen:
            seen.add(k)
            res.append(d)
    return res


def find_gpkgs(folders, limit=400):
    files, seen = [], set()
    for d in folders:
        for pat in ("*.gpkg", "*/*.gpkg", "*/*/*.gpkg"):
            for p in glob.glob(os.path.join(d, pat)):
                k = os.path.normcase(os.path.normpath(p))
                if k not in seen:
                    seen.add(k)
                    files.append(os.path.normpath(p))
                if len(files) >= limit:
                    return files
    for lyr in QgsProject.instance().mapLayers().values():
        src = (lyr.source() or "").split("|")[0]
        k = os.path.normcase(os.path.normpath(src))
        if src.lower().endswith(".gpkg") and os.path.isfile(src) and k not in seen:
            seen.add(k)
            files.append(os.path.normpath(src))
    return files


def candidates(files):
    """(path, layer, count, has_z, mtime) for every point layer."""
    from ..core.layerblocks import point_layers
    out = []
    for p in files:
        try:
            for n, c, z in point_layers(p):
                out.append((p, n, c, z, os.path.getmtime(p)))
        except Exception:
            pass
    return out


class BlockGpkgDialog(QDialog):
    """Pick one or more GeoPackage point layers to open as block models."""

    def __init__(self, parent=None, files=None, title="Block model from GeoPackage"):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(760, 420)
        self._fixed = files is not None
        self._files = list(files or [])
        lay = QVBoxLayout(self)
        self.lblWhere = P.hint("")
        self.lblWhere.setWordWrap(True)
        lay.addWidget(self.lblWhere)
        self.leFilter = QLineEdit()
        self.leFilter.setPlaceholderText("Filter by name…")
        self.leFilter.setClearButtonEnabled(True)
        self.leFilter.textChanged.connect(self._applyFilter)
        self.cbAll = QCheckBox("Show all point layers (collars, samples…)")
        self.cbAll.setToolTip("Off: only layers that look like block centres (a 'blocks' layer or a *_blocks file)")
        self.cbAll.toggled.connect(self._applyFilter)
        bBrowse = QPushButton(P.icon("open"), "Browse…")
        bBrowse.setToolTip("Pick a GeoPackage or shapefile anywhere; its point layers are added to the list")
        bBrowse.clicked.connect(self._browse)
        lay.addLayout(P.hrow(self.leFilter, self.cbAll, bBrowse, stretch_at=0))
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Block model", "Layer", "Points", "Modified", "Folder"])
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSortingEnabled(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.itemDoubleClicked.connect(lambda *_: self.accept())
        self.tree.itemSelectionChanged.connect(self._sel)
        lay.addWidget(self.tree, 1)
        self.bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Open | QDialogButtonBox.StandardButton.Cancel)
        self.bb.accepted.connect(self.accept)
        self.bb.rejected.connect(self.reject)
        lay.addWidget(self.bb)
        if self._fixed:
            self.cbAll.setChecked(True)
        self._reload()

    def _reload(self):
        from qgis.PyQt.QtWidgets import QApplication
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            folders = [] if self._fixed else search_folders()
            files = list(self._files) + ([] if self._fixed else find_gpkgs(folders))
            seen, uniq = set(), []
            for f in files:
                k = os.path.normcase(f)
                if k not in seen:
                    seen.add(k)
                    uniq.append(f)
            rows = candidates(uniq)
        finally:
            QApplication.restoreOverrideCursor()
        self.tree.setSortingEnabled(False)
        self.tree.clear()
        for p, n, c, z, mt in rows:
            stem = os.path.splitext(os.path.basename(p))[0]
            it = QTreeWidgetItem([stem, n, f"{c:,}{'' if z else ' (no Z)'}",
                                  datetime.datetime.fromtimestamp(mt).strftime("%Y-%m-%d %H:%M"), os.path.dirname(p)])
            it.setData(0, Qt.ItemDataRole.UserRole, (p, n))
            it.setData(2, Qt.ItemDataRole.UserRole, c)
            it.setData(3, Qt.ItemDataRole.UserRole, mt)
            it.setData(1, Qt.ItemDataRole.UserRole, _is_blockish(p, n))
            it.setToolTip(0, p)
            it.setToolTip(4, os.path.dirname(p))
            self.tree.addTopLevelItem(it)
        self.tree.setSortingEnabled(True)
        self.tree.sortItems(3, Qt.SortOrder.DescendingOrder)
        for i in range(5):
            self.tree.resizeColumnToContents(i)
        self.tree.setColumnWidth(0, min(self.tree.columnWidth(0), 280))
        if self._fixed:
            where = "this file"
            self.lblWhere.setToolTip("")
        else:
            fs = search_folders()
            where = f"the GeoPackages of {len(fs)} folders (work, output, project, last used, project layers - hover for the list)"
            self.lblWhere.setToolTip("\n".join(fs))
        self.lblWhere.setText(f"Point layers in {where}. Select one or more (Ctrl/Shift-click) to open as "
                              "block models; double-click opens one. Browse… for anything else.")
        self._applyFilter()

    def _applyFilter(self, *_):
        t = self.leFilter.text().strip().lower()
        allp = self.cbAll.isChecked()
        first = None
        shown = 0
        for i in range(self.tree.topLevelItemCount()):
            it = self.tree.topLevelItem(i)
            hide = (not allp and not it.data(1, Qt.ItemDataRole.UserRole)) or \
                   (t and t not in (it.text(0) + " " + it.text(1) + " " + it.text(4)).lower())
            it.setHidden(bool(hide))
            if not hide:
                shown += 1
                first = first or it
        if not self.tree.selectedItems() and first is not None:
            first.setSelected(True)
            self.tree.setCurrentItem(first)
        self.setWindowTitle(self.windowTitle().split("  (")[0] + f"  ({shown} shown)")
        self._sel()

    def _sel(self):
        self.bb.button(QDialogButtonBox.StandardButton.Open).setEnabled(
            any(not it.isHidden() for it in self.tree.selectedItems()))

    def _browse(self):
        start = QSettings().value("/QGeo3D/lastBmDir", "")
        paths, _ = QFileDialog.getOpenFileNames(self, "GeoPackage / shapefile of block centres", start,
                                                "GeoPackage / shapefile (*.gpkg *.GPKG *.shp *.SHP)")
        if not paths:
            return
        QSettings().setValue("/QGeo3D/lastBmDir", os.path.dirname(paths[0]))
        self._files.extend(os.path.normpath(p) for p in paths)
        self._reload()
        keys = {os.path.normcase(os.path.normpath(p)) for p in paths}
        self.tree.clearSelection()
        for i in range(self.tree.topLevelItemCount()):
            it = self.tree.topLevelItem(i)
            p, _n = it.data(0, Qt.ItemDataRole.UserRole)
            if os.path.normcase(p) in keys:
                it.setHidden(False)
                it.setSelected(True)
                self.tree.scrollToItem(it)

    def chosen(self):
        return [it.data(0, Qt.ItemDataRole.UserRole) for it in self.tree.selectedItems() if not it.isHidden()]
