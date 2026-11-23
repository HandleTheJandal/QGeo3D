# -*- coding: utf-8 -*-
"""Small building blocks for the QGeo 3D panel: collapsible sections, compact icon buttons,
path labels that shorten instead of widening the panel, and the panel's style sheet."""

import os

from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import (QFrame, QHBoxLayout, QLabel, QSizePolicy, QToolButton, QVBoxLayout,
                                 QWidget)

ICON_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "icons")


def icon(name):
    """Plugin icon (icons/<name>.svg) or a QGIS theme icon ('/mActionAdd.svg')."""
    if name.startswith("/"):
        from qgis.core import QgsApplication
        return QgsApplication.getThemeIcon(name)
    return QIcon(os.path.join(ICON_DIR, name + ".svg"))


def tool(icon_name, tip, slot=None, text=None, checkable=False, menu=None):
    """Flat icon button for list toolbars and section headers."""
    b = QToolButton()
    b.setAutoRaise(True)
    if icon_name:
        b.setIcon(icon(icon_name))
    if text:
        b.setText(text)
        b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon if icon_name
                             else Qt.ToolButtonStyle.ToolButtonTextOnly)
    b.setToolTip(tip)
    b.setCheckable(checkable)
    if menu is not None:
        b.setMenu(menu)
        b.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
    if slot is not None:
        (b.toggled if checkable else b.clicked).connect(slot)
    return b


def hrow(*items, spacing=4, stretch_at=None):
    """Horizontal layout of widgets / layouts; None items become stretch."""
    h = QHBoxLayout()
    h.setContentsMargins(0, 0, 0, 0)
    h.setSpacing(spacing)
    for i, w in enumerate(items):
        if w is None:
            h.addStretch(1)
        elif isinstance(w, QWidget):
            h.addWidget(w, 1 if i == stretch_at else 0)
        else:
            h.addLayout(w)
    return h


class Section(QWidget):
    """A titled block that folds away: header (arrow + title + optional tool buttons), body."""

    toggled = pyqtSignal(bool)

    def __init__(self, title, expanded=True, tools=(), parent=None):
        super().__init__(parent)
        self.setObjectName("q3dSection")
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        head = QWidget()
        head.setObjectName("q3dSecHeadRow")
        h = QHBoxLayout(head)
        h.setContentsMargins(2, 2, 2, 2)
        h.setSpacing(2)
        self.btn = QToolButton()
        self.btn.setObjectName("q3dSecHead")
        self.btn.setText(title)
        self.btn.setCheckable(True)
        self.btn.setChecked(expanded)
        self.btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.btn.toggled.connect(self.setExpanded)
        h.addWidget(self.btn, 1)
        self.tools = list(tools)
        for t in self.tools:
            h.addWidget(t)
        v.addWidget(head)
        self.body = QFrame()
        self.body.setObjectName("q3dSecBody")
        self.lay = QVBoxLayout(self.body)
        self.lay.setContentsMargins(8, 4, 6, 8)
        self.lay.setSpacing(5)
        v.addWidget(self.body)
        self._arrow(expanded)
        self.body.setVisible(expanded)

    def _arrow(self, on):
        self.btn.setArrowType(Qt.ArrowType.DownArrow if on else Qt.ArrowType.RightArrow)

    def setExpanded(self, on):
        on = bool(on)
        if self.btn.isChecked() != on:
            self.btn.setChecked(on)          # comes back here through toggled
            return
        self.body.setVisible(on)
        for t in self.tools:
            t.setVisible(on)
        self._arrow(on)
        self.toggled.emit(on)

    def isExpanded(self):
        return self.btn.isChecked()

    def add(self, item, stretch=0):
        if isinstance(item, QWidget):
            self.lay.addWidget(item, stretch)
        else:
            self.lay.addLayout(item, stretch)
        return item


class PathLabel(QLabel):
    """Shows a file path shortened in the middle to fit; the whole path is in the tooltip."""

    def __init__(self, text="", parent=None):
        super().__init__(parent)
        self.setProperty("role", "hint")
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(40)
        self._full = ""
        self.setText(text)

    def setText(self, text):
        self._full = text or ""
        self.setToolTip(self._full)
        self._elide()

    def text(self):
        return self._full

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._elide()

    def _elide(self):
        fm = self.fontMetrics()
        super().setText(fm.elidedText(self._full, Qt.TextElideMode.ElideMiddle, max(40, self.width() - 4)))


def hint(text=""):
    lab = QLabel(text)
    lab.setProperty("role", "hint")
    lab.setWordWrap(True)
    return lab


def card(text=""):
    lab = QLabel(text)
    lab.setProperty("role", "card")
    lab.setWordWrap(True)
    lab.setTextFormat(Qt.TextFormat.RichText)
    lab.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return lab


STYLE = """
QWidget#q3dPanel QToolButton#q3dSecHead {
    border: none; font-weight: 600; text-align: left; padding: 3px 2px;
}
QWidget#q3dPanel QWidget#q3dSecHeadRow {
    border-bottom: 1px solid palette(mid);
}
QWidget#q3dPanel QLabel[role="hint"] { color: #6b7b88; }
QWidget#q3dPanel QLabel[role="card"] {
    background: palette(base); border: 1px solid palette(midlight); border-radius: 4px; padding: 5px 7px;
}
QWidget#q3dPanel QLabel[role="status"] { color: #2e6b3a; }
QWidget#q3dPanel QPushButton[role="primary"] {
    font-weight: 600; padding: 5px 10px;
}
QWidget#q3dPanel QPushButton[role="primary"]:checked {
    background: #d9822b; color: white; border: 1px solid #b86a1d; border-radius: 3px;
}
QWidget#q3dPanel QListWidget, QWidget#q3dPanel QTableWidget { border: 1px solid palette(midlight); }
QWidget#q3dPanel QTabBar::tab { padding: 4px 9px; }
"""
