# -*- coding: utf-8 -*-
"""Import drillholes or a block model from CSV / Excel tables: pick file + sheet, check the
guessed field for each role, go."""

import contextlib
import os

from qgis.PyQt.QtCore import QSettings, pyqtSignal
from qgis.PyQt.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog, QFormLayout,
                                 QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                                 QPushButton, QSpinBox, QVBoxLayout, QWidget)

from ..core import tables as T

FILTER = "Tables (*.csv *.CSV *.txt *.tsv *.xlsx *.XLSX *.xlsm *.ods);;CSV (*.csv *.CSV *.txt *.tsv);;Excel (*.xlsx *.XLSX *.xlsm)"
KEY_DIR = "/QGeo3D/lastTableDir"
KEY_DIPDOWN = "/QGeo3D/dipPositiveDown"
NONE = "(none)"


class TableSource(QWidget):
    """File + sheet + header row; emits changed(fields) after (re)reading."""
    changed = pyqtSignal()

    def __init__(self, parent=None, path=None, sheet=None):
        super().__init__(parent)
        self.fields, self.rows, self.path = [], [], None
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        self.le = QLineEdit()
        self.le.setReadOnly(True)
        self.le.setPlaceholderText("CSV or Excel file…")
        b = QPushButton("…")
        b.setMaximumWidth(32)
        b.clicked.connect(self.browse)
        self.cbSheet = QComboBox()
        self.cbSheet.setMinimumWidth(110)
        self.cbSheet.currentIndexChanged.connect(lambda _i: self._read())
        self.spHead = QSpinBox()
        self.spHead.setRange(0, 60)
        self.spHead.setSpecialValueText("auto")
        self.spHead.setPrefix("header row ")
        self.spHead.setToolTip("Row holding the field names (1 = first row). 'auto' finds it.")
        self.spHead.valueChanged.connect(lambda _v: self._read())
        self.lbl = QLabel("")
        h.addWidget(self.le, 3)
        h.addWidget(b)
        h.addWidget(self.cbSheet, 1)
        h.addWidget(self.spHead)
        h.addWidget(self.lbl)
        if path:
            self.setFile(path, sheet)

    def browse(self):
        p, _ = QFileDialog.getOpenFileName(self, "Table", QSettings().value(KEY_DIR, ""), FILTER)
        if p:
            QSettings().setValue(KEY_DIR, os.path.dirname(p))
            self.setFile(p)

    def setFile(self, path, sheet=None):
        self.path = path
        self.le.setText(path)
        self.cbSheet.blockSignals(True)
        self.cbSheet.clear()
        try:
            names = T.sheets(path)
        except Exception as e:
            names = []
            self.lbl.setText(str(e))
        self.cbSheet.addItems(names)
        self.cbSheet.setVisible(len(names) > 1)
        if sheet in names:
            self.cbSheet.setCurrentText(sheet)
        self.cbSheet.blockSignals(False)
        self._read()

    def sheet(self):
        return (self.cbSheet.currentText() or None) if self.cbSheet.count() > 1 else None

    def sheetNames(self):
        return [self.cbSheet.itemText(i) for i in range(self.cbSheet.count())]

    def _read(self):
        if not self.path:
            return
        try:
            hr = self.spHead.value() - 1 if self.spHead.value() else None
            self.fields, self.rows, h = T.read_table(self.path, self.sheet(), hr)
            self.lbl.setText(f"{len(self.rows):,} rows" + ("" if self.spHead.value() else f" (header row {h + 1})"))
        except Exception as e:
            self.fields, self.rows = [], []
            self.lbl.setText(f"error: {e}")
        self.changed.emit()

    def spec(self):
        return {"path": self.path, "sheet": self.sheet(), "header": self.spHead.value() - 1 if self.spHead.value() else None}


class FieldBoxes:
    """One combo per role, refilled from a TableSource and preset to the guessed field."""

    def __init__(self, form, src, roles, optional=()):
        self.src, self.roles, self.optional, self.cb = src, roles, set(optional), {}
        for role, label in roles:
            c = QComboBox()
            c.setMinimumWidth(180)
            self.cb[role] = c
            form.addRow(label, c)
        src.changed.connect(self.fill)

    def fill(self):
        names = [f[0] for f in self.src.fields]
        for role, _label in self.roles:
            c = self.cb[role]
            c.clear()
            if role in self.optional:
                c.addItem(NONE)
            c.addItems(names)
            g = T.guess(self.src.fields, role if role not in ("shole", "ihole") else "hole")
            if g:
                c.setCurrentText(g)

    def get(self):
        out = {}
        for role, _l in self.roles:
            v = self.cb[role].currentText()
            out[role] = None if v in ("", NONE) else v
        return out

    def missing(self):
        return [lab for role, lab in self.roles if role not in self.optional and self.get()[role] is None]


class IntervalDialog(QDialog):
    def __init__(self, parent, path=None, sheet=None):
        super().__init__(parent)
        self.setWindowTitle("Interval table")
        f = QFormLayout(self)
        self.src = TableSource(self)
        f.addRow("Table", self.src)
        self.boxes = FieldBoxes(f, self.src, [("ihole", "Hole ID"), ("from", "From"), ("to", "To")])
        self.leName = QLineEdit()
        f.addRow("Layer name", self.leName)
        self.src.changed.connect(lambda: self.leName.setText(self.src.sheet() if self.src.cbSheet.count() > 1 else
                                                            os.path.splitext(os.path.basename(self.src.path or ""))[0]))
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        f.addRow(bb)
        if path:
            self.src.setFile(path, sheet)

    def spec(self):
        g = self.boxes.get()
        return dict(self.src.spec(), id=g["ihole"], **{"from": g["from"], "to": g["to"]}, name=self.leName.text().strip())


class DrillholeTableDialog(QDialog):
    """Collars (+ surveys) (+ interval tables) from CSV / Excel."""

    def __init__(self, parent=None, path=None):
        super().__init__(parent)
        self.setWindowTitle("Drillholes from CSV / Excel")
        self.resize(760, 640)
        v = QVBoxLayout(self)
        g = QGroupBox("Collars")
        f = QFormLayout(g)
        self.src = TableSource(self)
        f.addRow("Table", self.src)
        self.cBoxes = FieldBoxes(f, self.src, [("hole", "Hole ID"), ("east", "East"), ("north", "North"),
                                               ("rl", "RL"), ("depth", "Total depth"), ("azimuth", "Azimuth"),
                                               ("dip", "Dip")], optional=("rl", "depth", "azimuth", "dip"))
        v.addWidget(g)
        self.gS = QGroupBox("Surveys (untick for straight holes from the collar azimuth / dip)")
        self.gS.setCheckable(True)
        self.gS.setChecked(False)
        f = QFormLayout(self.gS)
        self.sSrc = TableSource(self)
        f.addRow("Table", self.sSrc)
        self.sBoxes = FieldBoxes(f, self.sSrc, [("shole", "Hole ID"), ("sdepth", "Depth"), ("azimuth", "Azimuth"),
                                                ("dip", "Dip")])
        v.addWidget(self.gS)
        g = QGroupBox("Interval tables (assays, lithology… : each becomes a layer)")
        gv = QVBoxLayout(g)
        self.lst = QListWidget()
        gv.addWidget(self.lst)
        h = QHBoxLayout()
        bA = QPushButton("Add…")
        bA.clicked.connect(lambda: self.addInterval())
        bE = QPushButton("Edit…")
        bE.clicked.connect(self.editInterval)
        bR = QPushButton("Remove")
        bR.clicked.connect(lambda: [self.lst.takeItem(self.lst.row(i)) for i in self.lst.selectedItems()])
        h.addWidget(bA)
        h.addWidget(bE)
        h.addWidget(bR)
        h.addStretch(1)
        gv.addLayout(h)
        v.addWidget(g)
        self.cbDipDown = QCheckBox("Positive dip = down (Datamine style: 60 = 60° below horizontal)")
        self.cbDipDown.setToolTip("Unticked (Micromine / Surpac style): negative = down, −90 = vertical down, "
                                  "positive = up (underground holes drilled upwards), 0 = horizontal.\n"
                                  "Ticked: every dip in the collar and survey tables is read with the opposite sign.")
        self.cbDipDown.setChecked(str(QSettings().value(KEY_DIPDOWN, "false")).lower() in ("true", "1"))
        v.addWidget(self.cbDipDown)
        note = QLabel("Unticked: negative = down (−90 = vertical down), positive = up, 0 = horizontal. "
                      "Depths, from and to must be in the same unit as the coordinates.")
        note.setWordWrap(True)
        note.setStyleSheet("color:#6b7b88")
        self.cbDipDown.toggled.connect(lambda on: note.setText(
            ("Ticked: positive = down (90 = vertical down), negative = up, 0 = horizontal. " if on else
             "Unticked: negative = down (−90 = vertical down), positive = up, 0 = horizontal. ") +
            "Depths, from and to must be in the same unit as the coordinates."))
        self.cbDipDown.toggled.emit(self.cbDipDown.isChecked())
        v.addWidget(note)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self.src.changed.connect(self._autofill)
        if isinstance(path, (list, tuple)):
            self._fromFiles(list(path))
        elif path:
            self.src.setFile(path)

    def _fromFiles(self, paths):
        """Several dropped tables: collar / survey / interval files picked from their names
        (a single workbook is handled by _autofill)."""
        if len(paths) == 1:
            self.src.setFile(paths[0])
            return
        base = {p: os.path.basename(p) for p in paths}
        coll = T.guess_sheet([base[p] for p in paths], "collar")
        surv = T.guess_sheet([base[p] for p in paths if base[p] != coll], "survey")
        cp = next((p for p in paths if base[p] == coll), paths[0])
        sp = next((p for p in paths if base[p] == surv and p != cp), None)
        self._filled = cp                       # don't let a workbook autofill fight the file list
        self.src.setFile(cp)
        if sp:
            self.sSrc.setFile(sp)
            self.gS.setChecked(True)
        for p in paths:
            if p in (cp, sp):
                continue
            d = IntervalDialog(self, p)
            self._addItem(d.spec())

    def _autofill(self):
        """A workbook with collar / survey / assay sheets: fill the rest from it."""
        names = self.src.sheetNames()
        if len(names) < 2 or getattr(self, "_filled", None) == self.src.path:
            return
        self._filled = self.src.path
        cs = T.guess_sheet(names, "collar")
        if cs and cs != self.src.sheet():
            self.src.cbSheet.setCurrentText(cs)
        ss = T.guess_sheet(names, "survey")
        if ss:
            self.sSrc.setFile(self.src.path, ss)
            self.gS.setChecked(True)
        if self.lst.count() == 0:
            for n in names:
                if n in (cs, ss):
                    continue
                fl = None
                with contextlib.suppress(Exception):
                    fl, _r, _h = T.read_table(self.src.path, n, max_rows=50)
                if fl is None:
                    continue
                if T.guess(fl, "from") and T.guess(fl, "to") and T.guess(fl, "hole"):
                    d = IntervalDialog(self, self.src.path, n)
                    self._addItem(d.spec())

    def _addItem(self, spec):
        it = QListWidgetItem(f"{spec['name']}  ·  {os.path.basename(spec['path'])}"
                             f"{' [' + spec['sheet'] + ']' if spec['sheet'] else ''}  ·  {spec['id']} / {spec['from']} / {spec['to']}")
        it.setData(256, spec)
        self.lst.addItem(it)

    def addInterval(self, path=None):
        d = IntervalDialog(self, path or self.src.path)
        if d.exec() == QDialog.DialogCode.Accepted:
            self._addItem(d.spec())

    def editInterval(self):
        its = self.lst.selectedItems()
        if not its:
            return
        sp = its[0].data(256)
        d = IntervalDialog(self, sp["path"], sp["sheet"])
        for k, role in (("id", "ihole"), ("from", "from"), ("to", "to")):
            d.boxes.cb[role].setCurrentText(sp[k])
        d.leName.setText(sp["name"])
        if d.exec() == QDialog.DialogCode.Accepted:
            self.lst.takeItem(self.lst.row(its[0]))
            self._addItem(d.spec())

    def _ok(self):
        from qgis.PyQt.QtWidgets import QMessageBox
        miss = self.cBoxes.missing() + (self.sBoxes.missing() if self.gS.isChecked() else [])
        if not self.src.path or miss:
            QMessageBox.warning(self, "Drillholes", "Choose the collar table and the fields: " + ", ".join(miss or ["table"]))
            return
        c = self.cBoxes.get()
        if not self.gS.isChecked() and not (c["azimuth"] and c["dip"]):
            QMessageBox.warning(self, "Drillholes", "Without a survey table the collar azimuth and dip are needed.")
            return
        self.accept()

    def config(self):
        c = self.cBoxes.get()
        cfg = {"collar": dict(self.src.spec(), id=c["hole"], east=c["east"], north=c["north"], rl=c["rl"],
                              depth=c["depth"], azimuth=c["azimuth"], dip=c["dip"]),
               "survey": None,
               "intervals": [self.lst.item(i).data(256) for i in range(self.lst.count())],
               "dip_positive_down": self.cbDipDown.isChecked()}
        QSettings().setValue(KEY_DIPDOWN, self.cbDipDown.isChecked())
        if self.gS.isChecked():
            s = self.sBoxes.get()
            cfg["survey"] = dict(self.sSrc.spec(), id=s["shole"], depth=s["sdepth"], azimuth=s["azimuth"], dip=s["dip"])
        return cfg


class BlockTableDialog(QDialog):
    """Block model from a CSV / Excel table: centre fields, size fields or fixed sizes."""

    def __init__(self, parent=None, path=None):
        super().__init__(parent)
        self.setWindowTitle("Block model from CSV / Excel")
        self.resize(640, 360)
        f = QFormLayout(self)
        self.src = TableSource(self)
        f.addRow("Table", self.src)
        self.boxes = FieldBoxes(f, self.src, [("xc", "Centre X / East"), ("yc", "Centre Y / North"), ("zc", "Centre Z / RL")])
        self.size = {}
        for role, lab in (("dx", "Size X"), ("dy", "Size Y"), ("dz", "Size Z")):
            w = QWidget()
            h = QHBoxLayout(w)
            h.setContentsMargins(0, 0, 0, 0)
            cb = QComboBox()
            sp = QDoubleSpinBox()
            sp.setRange(0.001, 10000)
            sp.setDecimals(3)
            sp.setValue(5.0)
            h.addWidget(cb, 2)
            h.addWidget(sp, 1)
            cb.currentTextChanged.connect(lambda t, sp=sp: sp.setEnabled(t == "(fixed size)"))
            self.size[role] = (cb, sp)
            f.addRow(lab, w)
        self.leName = QLineEdit()
        f.addRow("Name", self.leName)
        self.src.changed.connect(self._fill)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        f.addRow(bb)
        if path:
            self.src.setFile(path)

    def _fill(self):
        names = [x[0] for x in self.src.fields if x[1] == "R"]
        self.lfHeader = {}
        if self.src.path and os.path.splitext(self.src.path)[1].lower() in (".csv", ".txt"):
            try:
                from ..core.leapfrog import parse_bm_header
                self.lfHeader = parse_bm_header(T.csv_comments(self.src.path))
            except Exception:
                self.lfHeader = {}
        for role, (cb, sp) in self.size.items():
            cb.clear()
            cb.addItem("(fixed size)")
            cb.addItems(names)
            g = T.guess(self.src.fields, role)
            centre = self.boxes.get().get({"dx": "xc", "dy": "yc", "dz": "zc"}[role])
            if g and g != centre:
                cb.setCurrentText(g)
            elif self.lfHeader.get("size"):    # Leapfrog-style header: block size
                s = self.lfHeader["size"][("dx", "dy", "dz").index(role)]
                sp.setValue(s)
                sp.setToolTip(f"Block size from the file header ({s:g})")
            elif centre:                       # no size field: regular model, size = centre spacing
                from ..core.blockmodel import infer_spacing
                try:
                    cs = self.boxes.get()
                    oth = [k for k in ("xc", "yc", "zc") if k != {"dx": "xc", "dy": "yc", "dz": "zc"}[role]]
                    s = infer_spacing([r.get(centre) for r in self.src.rows],
                                      [[r.get(cs[k]) for r in self.src.rows] for k in oth]
                                      if all(cs.get(k) for k in oth) else None)
                except Exception:
                    s = None
                if s:
                    sp.setValue(s)
                    sp.setToolTip(f"From the spacing of {centre} values ({s:g})")
            sp.setEnabled(cb.currentText() == "(fixed size)")
        base = os.path.splitext(os.path.basename(self.src.path or ""))[0]
        self.leName.setText(base + (f" {self.src.sheet()}" if self.src.cbSheet.count() > 1 else ""))

    def config(self):
        cols = dict(self.boxes.get())
        for role, (cb, sp) in self.size.items():
            cols[role] = sp.value() if cb.currentText() == "(fixed size)" else cb.currentText()
        return dict(self.src.spec(), cols=cols, name=self.leName.text().strip() or "blocks",
                    header=getattr(self, "lfHeader", {}) or {})
