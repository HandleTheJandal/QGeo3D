# -*- coding: utf-8 -*-
"""Grade-tonnage / wireframe report dialog: tonnes, grade and metal by cut-off and resource category,
for the blocks shown, all blocks, blocks inside wireframes or inside the section slab."""

import os

import numpy as np
from qgis.PyQt.QtCore import QPointF, QRectF, QSettings, Qt
from qgis.PyQt.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from qgis.PyQt.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout,
                                 QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                                 QMessageBox, QPushButton, QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout,
                                 QWidget)

from ..core import reports as R

KEY_CUTS = "/QGeo3D/reportCutoffs"


class CurveWidget(QWidget):
    """Grade-tonnage curve: tonnes (blue, left axis) and grade (gold, right axis) against cut-off."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(200)
        self.pts = []
        self.unit = ""

    def setData(self, rows, unit):
        self.pts = [(r["cutoff"], r.get("tonnes", 0.0), r.get("grade")) for r in rows if r["category"] == "All"]
        self.unit = unit
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor("#1f2428"))
        if len(self.pts) < 2:
            p.setPen(QColor("#9fb0bc"))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Calculate with two or more cut-offs for a curve")
            return
        L, Rm, T, B = 64, 56, 14, 30
        w, h = self.width() - L - Rm, self.height() - T - B
        xs = [c for c, _t, _g in self.pts]
        ts = [t for _c, t, _g in self.pts]
        gs = [g for _c, _t, g in self.pts if g is not None]
        x0, x1 = min(xs), max(xs) or 1
        t1 = max(ts) or 1
        g1 = max(gs) if gs else 1
        X = lambda v: L + (v - x0) / ((x1 - x0) or 1) * w
        YT = lambda v: T + h - v / t1 * h
        YG = lambda v: T + h - v / (g1 * 1.1 or 1) * h
        p.setPen(QPen(QColor("#45505a"), 1))
        for k in range(5):
            y = T + h * k / 4
            p.drawLine(QPointF(L, y), QPointF(L + w, y))
        p.setFont(QFont("Segoe UI", 8))
        p.setPen(QColor("#9fb0bc"))
        for k in range(5):
            y = T + h * k / 4
            tv = t1 * (1 - k / 4)
            p.drawText(QRectF(0, y - 8, L - 6, 16), int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
                       f"{tv / 1e6:.2f} Mt" if t1 >= 1e6 else f"{tv / 1e3:,.0f} kt")
            gv = g1 * 1.1 * (1 - k / 4)
            p.drawText(QRectF(L + w + 6, y - 8, Rm, 16), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                       f"{gv:.2f}")
        for c in xs:
            p.drawText(QRectF(X(c) - 30, T + h + 4, 60, 16), int(Qt.AlignmentFlag.AlignCenter), f"{c:g}")
        p.drawText(QRectF(L, self.height() - 14, w, 14), int(Qt.AlignmentFlag.AlignCenter), f"cut-off ({self.unit})")
        for vals, Y, col in ((ts, YT, "#4f9bd9"), ([g for _c, _t, g in self.pts], YG, "#f2c12e")):
            path = QPainterPath()
            first = True
            for c, v in zip(xs, vals):
                if v is None:
                    continue
                pt = QPointF(X(c), Y(v))
                path.moveTo(pt) if first else path.lineTo(pt)
                first = False
            p.setPen(QPen(QColor(col), 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(path)
            p.setBrush(QColor(col))
            for c, v in zip(xs, vals):
                if v is not None:
                    p.drawEllipse(QPointF(X(c), Y(v)), 3, 3)
        p.setPen(QColor("#4f9bd9"))
        p.drawText(QRectF(L + 6, T, 120, 14), int(Qt.AlignmentFlag.AlignLeft), "tonnes")
        p.setPen(QColor("#f2c12e"))
        p.drawText(QRectF(L + w - 126, T, 120, 14), int(Qt.AlignmentFlag.AlignRight), f"grade ({self.unit})")


class ReportDialog(QDialog):

    COLS = ("Cut-off", "Category", "Blocks", "Tonnes", "Grade", "Metal", "Volume m³")

    def __init__(self, dock, scope=None, mesh_ids=None):
        super().__init__(dock)
        self.dock = dock
        self.bm = dock.bm
        self.setWindowTitle(f"Grade-tonnage report - {self.bm.name}")
        self.resize(820, 640)
        v = QVBoxLayout(self)
        f = QFormLayout()
        self.cbGrade = QComboBox()
        self.cbGrade.addItems(list(self.bm.grades) + [n for n in self.bm.numeric if n not in self.bm.grades])
        if self.bm.colorField in self.bm.grades:
            self.cbGrade.setCurrentText(self.bm.colorField)
        f.addRow("Grade field", self.cbGrade)
        self.leCuts = QLineEdit(QSettings().value(KEY_CUTS, "0, 0.3, 0.5, 1, 1.5, 2, 3, 5"))
        self.leCuts.setToolTip("Cut-off grades, separated by commas or spaces")
        f.addRow("Cut-offs", self.leCuts)
        self.cbCat = QCheckBox(f"Split by resource category ({self.bm.categoryField})" if self.bm.categoryField
                               else "Split by resource category (no category field found)")
        self.cbCat.setChecked(bool(self.bm.categoryField))
        self.cbCat.setEnabled(bool(self.bm.categoryField))
        f.addRow("", self.cbCat)
        self.cbScope = QComboBox()
        self.cbScope.addItem("Blocks shown in the 3D view", "shown")
        self.cbScope.addItem("All blocks (hide rule applied)", "all")
        self.cbScope.addItem("Inside the ticked wireframes", "mesh")
        self.cbScope.addItem("Inside the section slab", "section")
        f.addRow("Blocks", self.cbScope)
        v.addLayout(f)
        self.lstMesh = QListWidget()
        self.lstMesh.setMaximumHeight(110)
        for mid, m in dock.hook.meshes.items():
            if str(mid).startswith("bm:"):
                continue
            st = m.stats()
            it = QListWidgetItem(f"{m.name}  ({'closed' if st.get('closed') else 'OPEN - results unreliable'})")
            it.setData(Qt.ItemDataRole.UserRole, mid)
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Checked if (mesh_ids and mid in mesh_ids) else Qt.CheckState.Unchecked)
            self.lstMesh.addItem(it)
        v.addWidget(self.lstMesh)
        self.cbScope.currentIndexChanged.connect(lambda _=0: self.lstMesh.setEnabled(self.cbScope.currentData() == "mesh"))
        if scope:
            self.cbScope.setCurrentIndex(self.cbScope.findData(scope))
        self.lstMesh.setEnabled(self.cbScope.currentData() == "mesh")
        h = QHBoxLayout()
        b = QPushButton("Calculate")
        b.setDefault(True)
        b.clicked.connect(self.calculate)
        h.addWidget(b)
        self.bCsv = QPushButton("Export CSV…")
        self.bCsv.clicked.connect(lambda: self.export("csv"))
        self.bXls = QPushButton("Export Excel…")
        self.bXls.clicked.connect(lambda: self.export("xlsx"))
        self.bCopy = QPushButton("Copy")
        self.bCopy.clicked.connect(self.copy)
        for x in (self.bCsv, self.bXls, self.bCopy):
            x.setEnabled(False)
            h.addWidget(x)
        h.addStretch(1)
        v.addLayout(h)
        self.lbl = QLabel("")
        self.lbl.setWordWrap(True)
        v.addWidget(self.lbl)
        sp = QSplitter(Qt.Orientation.Vertical)
        self.tbl = QTableWidget(0, len(self.COLS))
        self.tbl.setHorizontalHeaderLabels(self.COLS)
        self.tbl.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.tbl.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        sp.addWidget(self.tbl)
        self.curve = CurveWidget()
        sp.addWidget(self.curve)
        sp.setSizes([320, 220])
        v.addWidget(sp, 1)
        self.rows = []

    def _cuts(self):
        import re
        vals = sorted({float(x) for x in re.findall(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", self.leCuts.text())})
        if not vals:
            raise ValueError("enter at least one cut-off")
        return vals

    def _blocks(self):
        bm, d = self.bm, self.dock
        scope = self.cbScope.currentData()
        if scope == "shown":
            return np.asarray(bm.shown), "blocks shown in the 3D view"
        rule = d._hideRule(bm) if hasattr(d, "_hideRule") else bm.default_hide_rule()
        base = bm.select(None, None, rule)
        if scope == "all":
            return base, "all blocks" + (f" (hidden: {rule})" if rule else "")
        w = bm.world(bm.xyz[base])
        if scope == "section":
            s = d.currentSection()
            if s is None:
                raise ValueError("no section - draw one first")
            return base[R.inside_slab(w, s)], f"blocks in the {s.width:g} m section slab"
        ids = [self.lstMesh.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.lstMesh.count())
               if self.lstMesh.item(i).checkState() == Qt.CheckState.Checked]
        if not ids:
            raise ValueError("tick one or more wireframes")
        m = np.zeros(len(base), bool)
        names = []
        for mid in ids:
            mesh = d.hook.meshes[mid]
            m |= R.inside_mesh(w, mesh.vertices, mesh.triangles)
            names.append(mesh.name)
        return base[m], "block centres inside " + ", ".join(names)

    def calculate(self):
        from qgis.PyQt.QtWidgets import QApplication
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            cuts = self._cuts()
            QSettings().setValue(KEY_CUTS, self.leCuts.text())
            idx, what = self._blocks()
            g = self.cbGrade.currentText() or None
            self.rows, meta = R.grade_tonnage(self.bm, idx, g, cuts, self.cbCat.isChecked())
            self.meta = dict(meta, what=what, grade_field=g, model=self.bm.name)
        except Exception as e:
            QApplication.restoreOverrideCursor()
            QMessageBox.warning(self, "Report", str(e))
            return
        QApplication.restoreOverrideCursor()
        u = self.meta["grade_unit"]
        self.tbl.setRowCount(len(self.rows))
        for i, r in enumerate(self.rows):
            vals = [f"{r['cutoff']:g}", r["category"], f"{r['blocks']:,}",
                    f"{r['tonnes']:,.0f}" if "tonnes" in r else "",
                    f"{r['grade']:.3f}" if "grade" in r else "",
                    f"{r['metal']:,.0f} {r['metal_unit']}" if "metal" in r else "", f"{r['volume']:,.0f}"]
            for j, val in enumerate(vals):
                it = QTableWidgetItem(val)
                if j != 1:
                    it.setTextAlignment(int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter))
                if r["category"] == "All":
                    fnt = it.font()
                    fnt.setBold(True)
                    it.setFont(fnt)
                self.tbl.setItem(i, j, it)
        self.lbl.setText(f"{self.bm.name}: {self.meta['what']} · grade {g or '-'} ({u}) · "
                         f"tonnes from {self.meta['tonnes_from'] or 'n/a (no density or tonnes field: volume only)'}")
        self.curve.setData(self.rows, u)
        for x in (self.bCsv, self.bXls, self.bCopy):
            x.setEnabled(True)

    def _table(self):
        head = ["cutoff", "category", "blocks", "tonnes", "grade", "metal", "metal_unit", "volume_m3"]
        out = [[r.get("cutoff"), r.get("category"), r.get("blocks"), r.get("tonnes"), r.get("grade"), r.get("metal"),
                r.get("metal_unit", ""), r.get("volume")] for r in self.rows]
        return head, out

    def copy(self):
        from qgis.PyQt.QtWidgets import QApplication
        head, rows = self._table()
        QApplication.clipboard().setText("\n".join("\t".join("" if v is None else str(v) for v in r)
                                                   for r in [head] + rows))

    def export(self, fmt):
        from ..core.q2thook import is_protected
        start = QSettings().value("/QGeo3D/reportDir", "")
        filt = "CSV (*.csv)" if fmt == "csv" else "Excel (*.xlsx)"
        path, _ = QFileDialog.getSaveFileName(self, "Save report", os.path.join(start, f"{self.bm.name}_gt.{fmt}"),
                                              filt)
        if not path:
            return
        if is_protected(path):
            QMessageBox.warning(self, "Report", "That folder is protected - choose another.")
            return
        QSettings().setValue("/QGeo3D/reportDir", os.path.dirname(path))
        head, rows = self._table()
        try:
            if fmt == "csv":
                import csv
                with open(path, "w", newline="", encoding="utf-8") as fh:
                    fh.write(f"# {self.lbl.text()}\n")
                    w = csv.writer(fh)
                    w.writerow(head)
                    w.writerows([["" if v is None else v for v in r] for r in rows])
            else:
                write_xlsx(path, head, rows, sheet="grade_tonnage", note=self.lbl.text())
        except Exception as e:
            QMessageBox.warning(self, "Report", f"Could not write {os.path.basename(path)}: {e}")
            return
        self.dock.plugin._msg(f"Report saved: {path}")


def write_xlsx(path, head, rows, sheet="report", note=""):
    """Excel sheet through GDAL's XLSX driver (numbers stay numbers)."""
    from osgeo import ogr
    if os.path.exists(path):
        os.remove(path)
    ds = ogr.GetDriverByName("XLSX").CreateDataSource(path)
    lyr = ds.CreateLayer(sheet, geom_type=ogr.wkbNone)
    types = []
    for k, h in enumerate(head):
        num = all(isinstance(r[k], (int, float)) or r[k] is None for r in rows) and any(r[k] is not None for r in rows)
        types.append(ogr.OFTReal if num else ogr.OFTString)
        lyr.CreateField(ogr.FieldDefn(h, types[-1]))
    for r in rows:
        f = ogr.Feature(lyr.GetLayerDefn())
        for k, v in enumerate(r):
            if v is not None:
                f.SetField(k, float(v) if types[k] == ogr.OFTReal else str(v))
        lyr.CreateFeature(f)
    if note:
        n = ds.CreateLayer("notes", geom_type=ogr.wkbNone)
        n.CreateField(ogr.FieldDefn("note", ogr.OFTString))
        f = ogr.Feature(n.GetLayerDefn())
        f.SetField(0, note)
        n.CreateFeature(f)
    ds = None
