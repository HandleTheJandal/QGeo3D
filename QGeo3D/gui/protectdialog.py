# -*- coding: utf-8 -*-
"""Edit the folders QGeo 3D must never write to (kept in this user's QGIS settings)."""

from qgis.PyQt.QtWidgets import (QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout, QLabel, QListWidget,
                                 QPushButton, QVBoxLayout)

from ..core.q2thook import protected_roots, set_protected_roots


class ProtectedFoldersDialog(QDialog):

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("QGeo 3D - protected folders")
        self.resize(560, 300)
        v = QVBoxLayout(self)
        v.addWidget(QLabel("QGeo 3D never writes into these folders or anything under them "
                           "(imports, exports, saved wireframes and Qgis2threejs settings go elsewhere). "
                           "Kept in your QGIS settings on this computer only."))
        self.lst = QListWidget()
        self.lst.addItems(protected_roots(raw=True))
        v.addWidget(self.lst)
        h = QHBoxLayout()
        b = QPushButton("Add folder...")
        b.clicked.connect(self._add)
        h.addWidget(b)
        b = QPushButton("Remove")
        b.clicked.connect(lambda: [self.lst.takeItem(self.lst.row(i)) for i in self.lst.selectedItems()])
        h.addWidget(b)
        h.addStretch(1)
        v.addLayout(h)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def _add(self):
        d = QFileDialog.getExistingDirectory(self, "Folder to protect")
        if d and not self.lst.findItems(d, __import__("qgis.PyQt.QtCore", fromlist=["Qt"]).Qt.MatchFlag.MatchExactly):
            self.lst.addItem(d)

    def _ok(self):
        set_protected_roots([self.lst.item(i).text() for i in range(self.lst.count())])
        self.accept()
