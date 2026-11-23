# -*- coding: utf-8 -*-
"""Export a GeoPackage / QGIS layer or a 3D object as Micromine, Datamine, Surpac, DXF, OBJ or CSV."""

import os
import re

from qgis.core import Qgis, QgsProject
from qgis.gui import QgsProjectionSelectionWidget
from qgis.PyQt.QtCore import QSettings
from qgis.PyQt.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
                                 QHBoxLayout, QLabel, QLineEdit, QPushButton, QWidget)

from ..core import writers as W
from ..core.exporting import layer_kind

KEY_DIR = "/QGeo3D/lastExportDir"
KIND_LABEL = {"wireframe": "wireframe", "strings": "strings", "points": "points", "blocks": "block model"}


class ExportDialog(QDialog):
    """sources: [(label, kind or 'points+blocks', key, default crs)] - key is ('layer', id) or ('mesh'|'blocks', id)."""

    def __init__(self, parent, sources, preselect=None, out_dir=""):
        super().__init__(parent)
        self.setWindowTitle("Export to Micromine / Datamine / Surpac / DXF")
        self.resize(640, 260)
        self.sources = sources
        f = QFormLayout(self)
        self.cbSrc = QComboBox()
        for lab, kind, key, _crs in sources:
            self.cbSrc.addItem(lab)
        for cb in (self.cbSrc,):
            cb.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            cb.setMinimumContentsLength(50)
        f.addRow("Export", self.cbSrc)
        self.cbKind = QComboBox()
        f.addRow("As", self.cbKind)
        self.cbFmt = QComboBox()
        f.addRow("Format", self.cbFmt)
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        self.leOut = QLineEdit()
        b = QPushButton("…")
        b.setMaximumWidth(32)
        b.clicked.connect(self._browse)
        h.addWidget(self.leOut)
        h.addWidget(b)
        f.addRow("File", w)
        self.crs = QgsProjectionSelectionWidget()
        f.addRow("Coordinates in", self.crs)
        self.cbSel = QCheckBox("Selected features only")
        f.addRow("", self.cbSel)
        self.lblNote = QLabel("")
        self.lblNote.setWordWrap(True)
        self.lblNote.setStyleSheet("color:#6b7b88")
        f.addRow(self.lblNote)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        f.addRow(bb)
        self.outDir = QSettings().value(KEY_DIR, "") or out_dir
        self.cbSrc.currentIndexChanged.connect(self._src)
        self.cbKind.currentIndexChanged.connect(self._kind)
        self.cbFmt.currentIndexChanged.connect(self._fmt)
        if preselect is not None:
            for i, s in enumerate(sources):
                if s[2] == preselect:
                    self.cbSrc.setCurrentIndex(i)
        self._src()

    def _src(self):
        if not self.sources:
            return
        lab, kind, key, crs = self.sources[self.cbSrc.currentIndex()]
        self.cbKind.blockSignals(True)
        self.cbKind.clear()
        kinds = ["points", "blocks"] if kind == "points+blocks" else [kind]
        for k in kinds:
            self.cbKind.addItem(KIND_LABEL[k], k)
        self.cbKind.blockSignals(False)
        if crs is not None and crs.isValid():
            self.crs.setCrs(crs)
        self.cbSel.setVisible(key[0] == "layer")
        if key[0] == "layer":
            lyr = QgsProject.instance().mapLayer(key[1])
            self.cbSel.setEnabled(bool(lyr and lyr.selectedFeatureCount()))
            self.cbSel.setChecked(bool(lyr and lyr.selectedFeatureCount()))
        self._kind()

    def _kind(self):
        k = self.cbKind.currentData()
        self.cbFmt.blockSignals(True)
        self.cbFmt.clear()
        for label, ext, key in W.FORMATS.get(k, []):
            self.cbFmt.addItem(label, (key, ext))
        last = QSettings().value(f"/QGeo3D/lastExportFmt_{k}", "")
        for i in range(self.cbFmt.count()):
            if self.cbFmt.itemData(i)[0] == last:
                self.cbFmt.setCurrentIndex(i)
        self.cbFmt.blockSignals(False)
        self._fmt()

    def _fmt(self):
        if self.cbFmt.currentData() is None:
            return
        key, ext = self.cbFmt.currentData()
        lab = self.sources[self.cbSrc.currentIndex()][0]
        stem = re.sub(r"[\\/:*?\"<>|]", "_", lab.split(":", 1)[-1].split("·")[0].strip()) or "export"
        cur = self.leOut.text()
        d = os.path.dirname(cur) if cur else self.outDir
        self.leOut.setText(os.path.join(d, stem + ext) if d else stem + ext)
        notes = {"tridb": "Added to the .tridb if it exists (a wireframe of the same name is replaced).",
                 "dm_wf": "Writes <name>pt.dm and <name>tr.dm (extended precision).",
                 "sp_dtm": "Writes <name>.dtm and the <name>.str holding its points.",
                 "dm_bm": "IJK from a parent cell of the largest block size; origin at the lower corner.",
                 "csv": "Plain table - importable by Vulcan, Leapfrog, Surpac, Deswik and Excel."}
        self.lblNote.setText(notes.get(key, ""))

    def _browse(self):
        key, ext = self.cbFmt.currentData()
        p, _ = QFileDialog.getSaveFileName(self, "Export to", self.leOut.text(), f"{self.cbFmt.currentText()} (*{ext})")
        if p:
            if not p.lower().endswith(ext.lower()):
                p += ext
            self.leOut.setText(p)

    def _ok(self):
        from ..core.q2thook import is_protected
        from qgis.PyQt.QtWidgets import QMessageBox
        p = self.leOut.text().strip()
        if not p or not os.path.isabs(p):
            QMessageBox.warning(self, "Export", "Choose where to write the file.")
            return
        if is_protected(p):
            QMessageBox.warning(self, "Export", "That folder is ringfenced (protected data) - choose another.")
            return
        key = self.cbFmt.currentData()[0]
        if os.path.exists(p) and key != "tridb" and QMessageBox.question(
                self, "Export", f"{os.path.basename(p)} exists. Overwrite?") != QMessageBox.StandardButton.Yes:
            return
        QSettings().setValue(KEY_DIR, os.path.dirname(p))
        QSettings().setValue(f"/QGeo3D/lastExportFmt_{self.cbKind.currentData()}", key)
        self.accept()

    def config(self):
        lab, kind, key, _crs = self.sources[self.cbSrc.currentIndex()]
        return {"source": key, "label": lab, "kind": self.cbKind.currentData(), "fmt": self.cbFmt.currentData()[0],
                "path": self.leOut.text().strip(), "crs": self.crs.crs(), "selected": self.cbSel.isChecked()}


def export_sources(dock):
    """Everything exportable: 3D wireframes and block models, then vector layers of the project."""
    out = []
    h = dock.hook
    imports = getattr(dock, "imports", {})
    prj = QgsProject.instance().crs()
    for mid, m in h.meshes.items():
        out.append((f"3D wireframe: {m.name}", "wireframe", ("mesh", mid), getattr(m, "src_crs", None) or prj))
    for bid, bm in h.blockModels.items():
        out.append((f"3D block model: {bm.name}", "blocks", ("blocks", bid), getattr(bm, "src_crs", None) or prj))
    for lyr in QgsProject.instance().mapLayers().values():
        if lyr.type() != Qgis.LayerType.Vector:
            continue
        k = layer_kind(lyr)
        if k:
            src = lyr.source().split("|")[0]
            out.append((f"Layer: {lyr.name()}  ·  {os.path.basename(src)}", k, ("layer", lyr.id()), lyr.crs()))
    return out
