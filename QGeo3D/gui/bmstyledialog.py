# -*- coding: utf-8 -*-
"""Blocks ▸ Colours…: edit a block model's colour classes, load / save QGIS styles (.qml),
take the style of its QGIS layer."""
import os

from qgis.PyQt.QtCore import QSettings, Qt
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (QAbstractItemView, QCheckBox, QColorDialog, QComboBox, QDialog, QDialogButtonBox,
                                 QFileDialog, QHeaderView, QLabel, QMessageBox, QPushButton, QTableWidget,
                                 QTableWidgetItem, QVBoxLayout)

from ..core import bmstyle
from . import panelwidgets as P

KEY_DIR = "/QGeo3D/lastStyleDir"


def _txt(v):
    return "" if v is None else (f"{v:g}" if isinstance(v, float) else str(v))


class BmStyleDialog(QDialog):

    def __init__(self, dock, bid, bm, field):
        super().__init__(dock)
        self.dock, self.bid, self.bm = dock, bid, bm
        bm.__dict__.setdefault("styles", {})
        self.setWindowTitle(f"Block colours - {bm.name}")
        self.resize(560, 460)
        lay = QVBoxLayout(self)
        self.cbField = QComboBox()
        fields = list(bm.numeric) + [f for f in bm.styles if f not in bm.numeric]
        self.cbField.addItems(fields)
        self.cbField.setCurrentText(field or "")
        self.cbKind = QComboBox()
        self.cbKind.addItem("Ranges (graduated)", "range")
        self.cbKind.addItem("Values (categorized)", "cat")
        lay.addLayout(P.hrow(QLabel("Colour by"), self.cbField, self.cbKind, stretch_at=1))
        self.tbl = QTableWidget(0, 4)
        self.tbl.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tbl.verticalHeader().setVisible(False)
        self.tbl.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.tbl.cellDoubleClicked.connect(self._cellDouble)
        lay.addWidget(self.tbl, 1)
        lay.addWidget(P.hint("Double-click a colour to change it · leave From / To empty for an open end · "
                             "a value left empty means 'all other values'."))
        bAdd = P.tool("/symbologyAdd.svg", "Add a class", self._add)
        bDel = P.tool("/symbologyRemove.svg", "Remove the selected classes", self._del)
        bDef = QPushButton("Default classes")
        bDef.setToolTip("QGeo 3D's own classes for this field (grade bins, categories or quantiles)")
        bDef.clicked.connect(self._default)
        bLoad = QPushButton(P.icon("open"), "Load QGIS style…")
        bLoad.setToolTip("A saved QGIS style (.qml) - graduated or categorized on a block-model field")
        bLoad.clicked.connect(self._loadQml)
        bSave = QPushButton(P.icon("save"), "Save as QGIS style…")
        bSave.clicked.connect(self._saveQml)
        self.layers = dock.bmQgisLayers(bm)
        bFrom = QPushButton(P.icon("/mActionAddOgrLayer.svg"), "From QGIS layer")
        bFrom.setToolTip("Take the style of the block-centre layer in QGIS" if self.layers else
                         "No QGIS layer of this block model's centres is loaded")
        bFrom.setEnabled(bool(self.layers))
        bFrom.clicked.connect(self._fromLayer)
        lay.addLayout(P.hrow(bAdd, bDel, bDef, None, bFrom, bLoad, bSave))
        self.cbFollow = QCheckBox("Follow the style of the QGIS layer (changes there show in 3D)")
        self.cbFollow.setChecked(bool(getattr(bm, "followQgis", True)))
        self.cbFollow.setEnabled(bool(self.layers))
        self.cbToQgis = QCheckBox("Also give the QGIS layer these colours")
        self.cbToQgis.setEnabled(bool(self.layers))
        lay.addWidget(self.cbFollow)
        lay.addWidget(self.cbToQgis)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Apply |
                              QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        bb.button(QDialogButtonBox.StandardButton.Apply).clicked.connect(self.apply)
        lay.addWidget(bb)
        self.cbField.currentTextChanged.connect(lambda _t: self._fill(self._current()))
        self.cbKind.currentIndexChanged.connect(self._kindChanged)
        self._fill(self._current())

    # ------------------------------------------------------------ table
    def _current(self):
        f = self.cbField.currentText()
        st = self.bm.styles.get(f)
        if st is not None:
            return st
        return self._defaultStyle(f)

    def _defaultStyle(self, f):
        import numpy as np
        if not f:
            return {"kind": "range", "classes": []}
        saved = self.bm.styles.pop(f, None)
        try:
            br = self.bm.breaks(f) if self.bm.data[f].dtype != object else None
            if br is not None:
                return {"kind": "range", "classes": [tuple(c) for c in br]}
            classes = []
            for lab, col, ci in self.bm.classify(f, np.arange(len(self.bm))) if self.bm.data[f].dtype != object else []:
                classes.append((bmstyle._num(self.bm.data[f][ci[0]]), col, lab))
            return {"kind": "cat", "classes": classes}
        finally:
            if saved is not None:
                self.bm.styles[f] = saved

    def _fill(self, st):
        self.cbKind.blockSignals(True)
        self.cbKind.setCurrentIndex(0 if st["kind"] == "range" else 1)
        self.cbKind.blockSignals(False)
        rng = st["kind"] == "range"
        self.tbl.setColumnCount(4 if rng else 3)
        self.tbl.setHorizontalHeaderLabels(["From", "To", "Colour", "Label"] if rng else ["Value", "Colour", "Label"])
        self.tbl.horizontalHeader().setSectionResizeMode(self.tbl.columnCount() - 1, QHeaderView.ResizeMode.Stretch)
        self.tbl.setRowCount(0)
        for c in st["classes"]:
            self._addRow(c)

    def _addRow(self, c):
        rng = self.cbKind.currentData() == "range"
        r = self.tbl.rowCount()
        self.tbl.insertRow(r)
        vals = ([_txt(c[0]), _txt(c[1])] if rng else [_txt(c[0])])
        for i, v in enumerate(vals):
            self.tbl.setItem(r, i, QTableWidgetItem(v))
        ci = len(vals)
        it = QTableWidgetItem(c[ci])
        it.setBackground(QColor(c[ci]))
        it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.tbl.setItem(r, ci, it)
        self.tbl.setItem(r, ci + 1, QTableWidgetItem(c[ci + 1]))

    def _cellDouble(self, r, c):
        if c != self.tbl.columnCount() - 2:
            return
        it = self.tbl.item(r, c)
        col = QColorDialog.getColor(QColor(it.text()), self, "Class colour")
        if col.isValid():
            it.setText(col.name())
            it.setBackground(col)

    def _add(self):
        rng = self.cbKind.currentData() == "range"
        self._addRow((None, None, "#9e9e9e", "new") if rng else (None, "#9e9e9e", "other"))

    def _del(self):
        for r in sorted({i.row() for i in self.tbl.selectedIndexes()}, reverse=True):
            self.tbl.removeRow(r)

    def _kindChanged(self):
        self._fill({"kind": self.cbKind.currentData(), "classes": []})

    def _default(self):
        self._fill(self._defaultStyle(self.cbField.currentText()))

    def style(self):
        rng = self.cbKind.currentData() == "range"
        out = []
        for r in range(self.tbl.rowCount()):
            t = [(self.tbl.item(r, c).text().strip() if self.tbl.item(r, c) else "") for c in range(self.tbl.columnCount())]
            if rng:
                def num(s):
                    if s == "":
                        return None
                    return float(s.replace(",", ""))
                lo, hi = num(t[0]), num(t[1])
                out.append((lo, hi, t[2], t[3] or bmstyle._rlabel(lo, hi) if (lo is not None or hi is not None) else t[3]))
            else:
                out.append((bmstyle._num(t[0]) if t[0] != "" else None, t[1], t[2] or t[0] or "other"))
        if rng:
            out.sort(key=lambda c: float("-inf") if c[0] is None else c[0])
        return {"kind": "range" if rng else "cat", "classes": out}

    # ------------------------------------------------------------ styles
    def _setFrom(self, field, st, where):
        f = bmstyle.match_field(self.bm, field)
        if f is None:
            QMessageBox.warning(self, "Block colours", f"The style in {where} colours by '{field}', which "
                                f"{self.bm.name} doesn't have.")
            return
        if self.cbField.findText(f) < 0:
            self.cbField.addItem(f)
        self.cbField.blockSignals(True)
        self.cbField.setCurrentText(f)
        self.cbField.blockSignals(False)
        self._fill(st)

    def _loadQml(self):
        path, _ = QFileDialog.getOpenFileName(self, "Load QGIS style", QSettings().value(KEY_DIR, ""),
                                              "QGIS style (*.qml)")
        if not path:
            return
        QSettings().setValue(KEY_DIR, os.path.dirname(path))
        try:
            field, st = bmstyle.load_qml(path, self.bm)
        except Exception as e:
            QMessageBox.warning(self, "Block colours", f"{os.path.basename(path)}: {e}")
            return
        self._setFrom(field, st, os.path.basename(path))

    def _saveQml(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save as QGIS style",
                                              os.path.join(QSettings().value(KEY_DIR, ""),
                                                           f"{self.bm.name}_{self.cbField.currentText()}.qml"),
                                              "QGIS style (*.qml)")
        if not path:
            return
        from ..core.q2thook import is_protected
        if is_protected(path):
            QMessageBox.warning(self, "Block colours", "That folder is protected - choose another.")
            return
        QSettings().setValue(KEY_DIR, os.path.dirname(path))
        try:
            bmstyle.save_qml(path, self.bm, self.cbField.currentText(), self.style())
        except Exception as e:
            QMessageBox.warning(self, "Block colours", str(e))

    def _fromLayer(self):
        for ml in self.layers:
            res = bmstyle.from_renderer(ml.renderer())
            if res:
                self._setFrom(res[0], res[1], f"layer '{ml.name()}'")
                return
        QMessageBox.information(self, "Block colours", "The QGIS layer has no graduated or categorized style on "
                                "a field (set one in its Symbology, or load a .qml here).")

    # ------------------------------------------------------------ apply
    def apply(self):
        try:
            st = self.style()
        except ValueError as e:
            QMessageBox.warning(self, "Block colours", f"A From / To value isn't a number: {e}")
            return False
        f = self.cbField.currentText()
        self.bm.followQgis = self.cbFollow.isChecked()
        self.dock.applyBmStyle(self.bid, f, st, to_qgis=self.cbToQgis.isChecked())
        return True

    def _ok(self):
        if self.apply():
            self.accept()
