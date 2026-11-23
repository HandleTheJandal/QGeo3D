# -*- coding: utf-8 -*-
"""Map canvas tools: draw a section line (two clicks, with snapping)."""

from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor
from qgis.core import Qgis, QgsGeometry, QgsPointLocator, QgsPointXY
from qgis.gui import QgsMapTool, QgsRubberBand, QgsSnapIndicator


class SectionLineTool(QgsMapTool):
    """Click A, click B. Right-click or Esc cancels. Uses project snapping settings."""

    sectionDrawn = pyqtSignal(QgsPointXY, QgsPointXY)

    def __init__(self, canvas):
        super().__init__(canvas)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.rb = QgsRubberBand(canvas, Qgis.GeometryType.Line)
        self.rb.setColor(QColor(232, 52, 28, 220))
        self.rb.setWidth(2)
        self.snapIndicator = QgsSnapIndicator(canvas)
        self.a = None

    def _point(self, e):
        m = self.canvas().snappingUtils().snapToMap(e.pos())
        self.snapIndicator.setMatch(m)
        return QgsPointXY(m.point()) if m.isValid() else self.toMapCoordinates(e.pos())

    def canvasMoveEvent(self, e):
        p = self._point(e)
        if self.a is not None:
            self.rb.setToGeometry(QgsGeometry.fromPolylineXY([self.a, p]), None)

    def canvasReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.RightButton:
            self.reset()
            return
        p = self._point(e)
        if self.a is None:
            self.a = p
        else:
            a = self.a
            self.reset()
            if a.distance(p) > 0:
                self.sectionDrawn.emit(a, p)

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Escape:
            self.reset()

    def reset(self):
        self.a = None
        self.rb.reset(Qgis.GeometryType.Line)
        self.snapIndicator.setMatch(QgsPointLocator.Match())

    def deactivate(self):
        self.reset()
        super().deactivate()

    def cleanup(self):
        self.reset()
        self.canvas().scene().removeItem(self.rb)
