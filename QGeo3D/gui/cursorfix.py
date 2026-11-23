# -*- coding: utf-8 -*-
"""Qgis2threejs 3.x shows the 3D view as a window of another process inside the 3D window. Windows
gives that window a resize border, so the mouse picks up a resize cursor (diagonal arrows) at the
view's edge, and Qt doesn't put its own cursor back when the mouse moves on into the panels - the
corner arrow stays over the QGeo 3D panel, layer list and toolbar. Re-apply the cursor whenever
the mouse enters one of them."""
from qgis.PyQt.QtCore import QEvent, QObject, Qt
from qgis.PyQt.QtWidgets import QApplication, QDockWidget, QMenuBar, QStatusBar, QToolBar


class CursorFix(QObject):

    def __init__(self, window):
        super().__init__(window)
        self.wnd = window
        self._on = []
        self.attach()

    def attach(self):
        w = self.wnd
        targets = [x for x in w.findChildren(QDockWidget) + w.findChildren(QToolBar) +
                   w.findChildren(QStatusBar) + w.findChildren(QMenuBar) if x.window() is w]
        for t in targets:
            if t not in self._on:
                t.installEventFilter(self)
                self._on.append(t)

    def detach(self):
        for t in self._on:
            try:
                t.removeEventFilter(self)
            except RuntimeError:
                pass
        self._on = []

    def eventFilter(self, obj, ev):
        if ev.type() == QEvent.Type.Enter:
            # an override set and removed makes Qt send the cursor of the widget under the mouse again
            QApplication.setOverrideCursor(Qt.CursorShape.ArrowCursor)
            QApplication.restoreOverrideCursor()
        return False
