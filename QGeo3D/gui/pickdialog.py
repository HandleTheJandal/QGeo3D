# -*- coding: utf-8 -*-
"""Small 'tick the items you want' dialog (wireframes in a .tridb, sheets in a workbook...)."""

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QListWidget,
                                 QListWidgetItem, QPushButton, QVBoxLayout)


def pick_items(parent, title, text, items, multi=True):
    """items: [(label, checked, tooltip)]. Returns the ticked indices, or None if cancelled."""
    dlg = QDialog(parent)
    dlg.setWindowTitle(title)
    dlg.resize(560, 420)
    v = QVBoxLayout(dlg)
    lab = QLabel(text)
    lab.setWordWrap(True)
    v.addWidget(lab)
    lst = QListWidget()
    for label, checked, tip in items:
        it = QListWidgetItem(label)
        if multi:
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        if tip:
            it.setToolTip(tip)
        lst.addItem(it)
    if not multi and lst.count():
        lst.setCurrentRow(0)
    v.addWidget(lst)
    if multi:
        h = QHBoxLayout()
        for name, on in (("All", True), ("None", False)):
            b = QPushButton(name)
            b.clicked.connect(lambda _=False, on=on: [lst.item(i).setCheckState(
                Qt.CheckState.Checked if on else Qt.CheckState.Unchecked) for i in range(lst.count())])
            h.addWidget(b)
        h.addStretch(1)
        v.addLayout(h)
    bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
    bb.accepted.connect(dlg.accept)
    bb.rejected.connect(dlg.reject)
    v.addWidget(bb)
    if not multi:
        lst.itemDoubleClicked.connect(lambda _it: dlg.accept())
    if dlg.exec() != QDialog.DialogCode.Accepted:
        return None
    if multi:
        return [i for i in range(lst.count()) if lst.item(i).checkState() == Qt.CheckState.Checked]
    return [lst.currentRow()] if lst.currentRow() >= 0 else []
