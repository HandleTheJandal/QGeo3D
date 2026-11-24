# -*- coding: utf-8 -*-
"""QGeo 3D dock panel: wireframes, camera, sections."""

import json
import os

import numpy as np

from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtGui import QColor, QIcon
from qgis.PyQt.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDockWidget, QFrame, QInputDialog, QListWidget, QListWidgetItem, QDoubleSpinBox, QFileDialog, QFormLayout, QGroupBox,
                                 QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMenu, QPushButton, QToolButton, QScrollArea, QSpinBox, QTabWidget, QTableWidget, QTableWidgetItem,
                                 QVBoxLayout, QWidget, QStyledItemDelegate)
from qgis.core import Qgis, QgsGeometry, QgsIconUtils, QgsPointXY, QgsProject, QgsVectorLayer, QgsWkbTypes

from ..core.q2thook import is_protected, log, sandbox_dir
from ..core.section import Section
from ..core.strings import (StringStore, delete_section, layer_polylines, load_sections, load_wireframes,
                            polyline_payload, save_section, save_wireframe, string_records)
from ..core import wireframing
from ..core.crs import open_gpkg_layer
from .maptools import SectionLineTool
from .tools import LeapfrogMixin, ToolsMixin
from . import panelwidgets as _pw
from ..core.compat import enum as _enum
from ..core.compat import gdal_soft

DRILL_COLOR = "#d24bff"
STRING_COLOR = "#ffd400"


def work_dir():
    return os.path.join(os.path.dirname(sandbox_dir()), "work")


OUT_KEY_MODE = "/QGeo3D/importOutMode"      # work | project | source | custom | ask
OUT_DEFAULT = "project"                      # default: the project's "QGeo 3D data" subfolder
OUT_KEY_DIR = "/QGeo3D/importOutDir"
COORD_FIELDS = {"EAST", "NORTH", "RL", "_EAST", "_NORTH", "_RL", "X", "Y", "Z", "XC", "YC", "ZC", "EASTING",
                "NORTHING", "ELEVATION", "ELEV", "_GEOM_X", "_GEOM_Y", "_GEOM_Z", "XCENTRE", "YCENTRE", "ZCENTRE"}
BM_GPKG_KEY = "/QGeo3D/blockGpkgMode"            # auto | ask | never
SUBDIR = "QGeo 3D data"                         # subfolder for the project / source-file choices
OLD_SUBDIR = "QGIS2Three4 data"                  # kept when it already exists (data from before the rename)


def _subdir(base):
    """<base>/QGeo 3D data. (Files already saved in an older "QGIS2Three4 data" folder stay there; projects
    refer to them by their full path.)"""
    return os.path.join(base, SUBDIR)


OUT_MODES = [("work", "Plugin work folder"), ("project", "Project folder (\"QGeo 3D data\" subfolder)"),
             ("source", "Next to the imported file (\"QGeo 3D data\" subfolder)"), ("custom", "Fixed folder…"),
             ("ask", "Ask each time")]


def _row(*widgets):
    h = QHBoxLayout()
    h.setContentsMargins(0, 0, 0, 0)
    for w in widgets:
        h.addWidget(w)
    return h


class _NameDelegate(QStyledItemDelegate):
    """In-place rename in the Wireframing list (F2, or click a selected item): the editor shows the bare
    name, and the new name is written to the strings / wireframes GeoPackage."""

    def __init__(self, dock):
        super().__init__(dock.lstStrings)
        self.dock = dock

    def setEditorData(self, editor, index):
        editor.setText(index.data(Qt.ItemDataRole.UserRole + 1) or "")
        editor.selectAll()

    def setModelData(self, editor, model, index):
        key, text = index.data(Qt.ItemDataRole.UserRole), editor.text()
        QTimer.singleShot(0, lambda: self.dock.renameItem(key, text))


class Q2T4Dock(ToolsMixin, LeapfrogMixin, QDockWidget):

    def __init__(self, plugin):
        super().__init__("QGeo 3D")
        self.setObjectName("QGeo3DDock")
        self.plugin = plugin
        self.iface = plugin.iface
        self.hook = plugin.hook
        self.canvas = self.iface.mapCanvas()

        self.baseSection = None     # as drawn
        self.offset = 0.0           # current step offset from base (m, + = away from viewer)

        # canvas overlays (not layers: nothing is added to the project)
        self.rbSlab = QgsRubberBand_(self.canvas, Qgis.GeometryType.Polygon, QColor(232, 52, 28, 40), QColor(232, 52, 28, 160), 1)
        self.rbLine = QgsRubberBand_(self.canvas, Qgis.GeometryType.Line, None, QColor(232, 52, 28, 230), 2)

        self.rbStrings = QgsRubberBand_(self.canvas, Qgis.GeometryType.Line, None, QColor(230, 170, 0, 255), 2)
        from qgis.gui import QgsRubberBand
        self.rbStrings.setIcon(_enum("QgsRubberBand.IconType.ICON_CIRCLE", "QgsRubberBand.ICON_CIRCLE"))
        self.rbStrings.setIconSize(5)

        self.tool = SectionLineTool(self.canvas)
        self.tool.sectionDrawn.connect(self._onSectionDrawn)

        self.store = StringStore(self)
        self.store.changed.connect(self.refreshStrings)
        self.store.changed.connect(self._strChanged)               # strings undo steps
        self._lastProjectCrs = QgsProject.instance().crs()
        QgsProject.instance().crsChanged.connect(self._projectCrsChanged)
        QgsProject.instance().cleared.connect(self._projectCleared)
        self.drillLayerId = None

        self._build()

        # JS -> Python events (finished strings, mode changes) are polled
        self.pollTimer = QTimer(self)
        self.pollTimer.setInterval(200)
        self.pollTimer.timeout.connect(self._poll)
        self.pollTimer.start()

        # show strings already saved for this project
        QTimer.singleShot(0, self._openExisting)

    def _openExisting(self):
        try:
            if self.store.layer is None and os.path.exists(self.defaultGpkg()):
                self.ensureStore()
        except Exception as e:
            log(f"Could not open strings GeoPackage: {e}")

    # ------------------------------------------------------------------ ui
    def _page(self, *sections, stretch_last=False):
        body = QWidget()
        body.setObjectName("q3dPage")
        v = QVBoxLayout(body)
        v.setContentsMargins(4, 4, 4, 4)
        v.setSpacing(6)
        for i, s in enumerate(sections):
            v.addWidget(s, 1 if (stretch_last and i == len(sections) - 1) else 0)
        if not stretch_last:
            v.addStretch(1)
        sa = QScrollArea()
        sa.setWidgetResizable(True)
        sa.setFrameShape(QFrame.Shape.NoFrame)
        sa.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        sa.setWidget(body)
        return sa, body

    def _addMenu(self):
        """Everything the panel can open, in one menu (also on the 3D toolbar's Add ▾)."""
        m = QMenu(self)
        for text, ic, slot in (
                ("Wireframe from file…", "open", self.plugin.addFromFile),
                ("Wireframe from active layer", "/mActionAddOgrLayer.svg", self.plugin.addFromActiveLayer),
                (None, None, None),
                ("Micromine file (.tridb / .STR / .DAT / .DHDB)…", None, self.openMicromineDialog),
                ("Datamine file (.dm)…", None, self.openDatamineDialog),
                ("Surpac file (.str / .dtm / .mdl)…", None, self.openSurpacDialog),
                ("Leapfrog / OMF (.omf)…", None, getattr(self, "openOmfDialog", None)),
                (None, None, None),
                ("Block model (.DAT / .dm / .bmf / .mdl)…", "blocks", self.loadBlockModelDialog),
                ("Block model from GeoPackage…", "/mIconGeoPackage.svg", lambda: self.loadBlockModelGpkgDialog()),
                ("Block model from CSV / Excel…", "/mActionOpenTable.svg", lambda: self.tableBlocksDialog()),
                ("Block model from active point layer", "/mActionAddOgrLayer.svg", self.loadBlockModelActiveLayer),
                (None, None, None),
                ("Drillholes: Micromine .DHDB…", None, self.loadDhdbDialog),
                ("Drillholes from CSV / Excel…", "/mActionOpenTable.svg", lambda: self.tableDrillholesDialog()),
                (None, None, None),
                ("Saved wireframes (strings GeoPackage)", None, self.showSavedWireframes)):
            if text is None:
                m.addSeparator()
                continue
            if slot is None:
                continue
            a = m.addAction(_pw.icon(ic) if ic else QIcon(), text)
            a.triggered.connect(lambda _=False, f=slot: f())
        return m

    def _build(self):
        """Inspector: Objects · Section · Blocks · Strings · Query tabs, each a column of
        collapsible sections. View/camera controls live on the 3D window's toolbar."""
        P = _pw
        self._remember = {}

        def section(key, title, expanded=True, tools=()):
            s = P.Section(title, expanded, tools)
            self._remember["sec_" + key] = s
            return s

        # ================================================================ Objects
        bAdd = QPushButton(P.icon("add"), "Add")
        bAdd.setProperty("role", "primary")
        bAdd.setToolTip("Open a wireframe, block model, drillholes, strings or points - or drop files on the 3D view")
        bAdd.setMenu(self._addMenu())
        bExp = QPushButton(P.icon("export"), "Export…")
        bExp.setToolTip("Write the selected wireframe / block model, or any GeoPackage / QGIS layer, as Micromine, "
                        "Datamine, Surpac, Leapfrog, OMF, DXF, OBJ or CSV")
        bExp.clicked.connect(lambda: self.exportDialog())

        sObj = section("objects", "In the 3D view")
        sObj.add(P.hrow(bAdd, bExp, None))
        self.lstWf = QListWidget()
        self.lstWf.setMinimumHeight(150)
        self.lstWf.setAlternatingRowColors(True)
        self.lstWf.setToolTip("Tick to show, untick to hide. Double-click to zoom to it.")
        self.lstWf.itemChanged.connect(self._wfItemChanged)
        self.lstWf.itemDoubleClicked.connect(self._wfZoom)
        self.lstWf.itemClicked.connect(self._objClicked)
        self.lstWf.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.lstWf.customContextMenuRequested.connect(self._objListMenu)
        sObj.add(self.lstWf, 1)
        self.cbEdges = QCheckBox("Triangle edges")
        self.cbEdges.setChecked(True)
        self.cbEdges.toggled.connect(self.hook.setEdgesVisible)
        tAll = P.tool("/mActionShowAllLayers.svg", "Show all", lambda: self._wfSetAll(True))
        tNone = P.tool("/mActionHideAllLayers.svg", "Hide all", lambda: self._wfSetAll(False))
        tZoom = P.tool("zoom", "Zoom to the selected item",
                       lambda: self.lstWf.currentItem() and self._wfZoom(self.lstWf.currentItem()))
        tCrs = P.tool("/mActionSetProjection.svg", "Coordinate system of the selected item's file - change it if it "
                      "was set wrong on import", self.changeCrs)
        tRem = P.tool("/mActionRemoveLayer.svg", "Remove the selected item from the 3D view (files and GeoPackages "
                      "are not touched)", self._wfRemove)
        tClr = P.tool("delete", "Clear all wireframes from the 3D view", self.hook.clear)
        sObj.add(P.hrow(tAll, tNone, tZoom, tCrs, tRem, None, self.cbEdges, tClr))
        self.hook.itemsChanged.connect(self._fillWfList)
        self.hook.itemsChanged.connect(lambda: QTimer.singleShot(0, self._watchBmStyles))
        self._onLayersAdded = lambda *_: QTimer.singleShot(0, self._watchBmStyles)
        QgsProject.instance().layersAdded.connect(self._onLayersAdded)
        self.hook.itemsChanged.connect(self._updateHud)

        # drillholes
        sDh = section("drill", "Drillholes: styles and labels")
        self.sDh = sDh
        self.cbDhLayer = QComboBox()
        self.cbDhLayer.setToolTip("Drillhole / channel layer to style: the line layers in the 3D view (Objects list). "
                                  "Clicking one in the list above, or right-click ▸ Styles and labels, picks it here. "
                                  "Its interval ends snap in the 3D view.")
        self.cbDhLayer.addItem("(none)", None)
        self.cbDhLayer.currentIndexChanged.connect(self._dhLayerChosen)
        bDhClr = P.tool("delete", "Stop styling / labelling the drillhole layer", self.clearDrill)
        sDh.add(P.hrow(QLabel("Layer"), self.cbDhLayer, bDhClr, stretch_at=1))
        self.lblDrill = P.hint("Pick a drillhole layer - or click one in the list above")
        sDh.add(self.lblDrill)
        self.cbDhField = QComboBox()
        self.cbDhField.setToolTip("Field the interval tubes are coloured by (grade classes for Au, quantiles for "
                                  "other numbers, one colour per code)")
        self.cbDhTubes = QCheckBox("Tubes")
        self.cbDhTubes.setToolTip("Draw the intervals as coloured tubes")
        self.spDhRad = QDoubleSpinBox()
        self.spDhRad.setRange(0.05, 50)
        self.spDhRad.setDecimals(2)
        self.spDhRad.setValue(0.6)
        self.spDhRad.setPrefix("radius ")
        self.spDhRad.setSuffix(" m")
        self.spDhRad.setToolTip("Tube radius")
        self.cbDhIds = QCheckBox("Hole IDs")
        self.cbDhIds.setToolTip("Hole ID at the top of each hole")
        self.cbDhGrade = QCheckBox("Values")
        self.cbDhGrade.setToolTip("Value of the colour field at each interval (on the section when one is set)")
        bDhApply = QPushButton("Apply")
        bDhApply.clicked.connect(self.drillDisplay)
        f = QFormLayout()
        f.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        f.addRow("Colour by", self.cbDhField)
        f.addRow("Show", P.hrow(self.cbDhTubes, self.spDhRad, None))
        f.addRow("Labels", P.hrow(self.cbDhIds, self.cbDhGrade, None, bDhApply))
        sDh.add(f)
        self.lblDhLegend = QLabel("")
        self.lblDhLegend.setWordWrap(True)
        self.lblDhLegend.setTextFormat(Qt.TextFormat.RichText)
        sDh.add(self.lblDhLegend)

        # where imports write
        sOut = section("output", "Import settings", expanded=False)
        from qgis.PyQt.QtCore import QSettings
        self.cbOut = QComboBox()
        for k, lab in OUT_MODES:
            self.cbOut.addItem(lab, k)
        i = self.cbOut.findData(QSettings().value(OUT_KEY_MODE, OUT_DEFAULT) or OUT_DEFAULT)
        self.cbOut.setCurrentIndex(max(0, i))
        self.cbOut.currentIndexChanged.connect(self._outModeChanged)
        bOut = P.tool("/mActionFileOpen.svg", "Choose the fixed folder", self._chooseOutDir)
        sOut.add(QLabel("GeoPackages written by imports go to:"))
        sOut.add(P.hrow(self.cbOut, bOut, stretch_at=0))
        self.lblOut = P.PathLabel("")
        sOut.add(self.lblOut)
        self._outLabel()
        self.cbBmGpkg = QComboBox()
        for k, lab in (("auto", "Write automatically"), ("ask", "Ask each time"), ("never", "Don't write")):
            self.cbBmGpkg.addItem(lab, k)
        self.cbBmGpkg.setCurrentIndex(max(0, self.cbBmGpkg.findData(QSettings().value(BM_GPKG_KEY, "auto") or "auto")))
        self.cbBmGpkg.setToolTip("Loading a block model can also write a GeoPackage of its block centres and add it "
                                 "to QGIS (for the 2D map). The source file is never changed.")
        self.cbBmGpkg.currentIndexChanged.connect(
            lambda _=0: QSettings().setValue(BM_GPKG_KEY, self.cbBmGpkg.currentData()))
        f = QFormLayout()
        f.addRow("GeoPackage", self.cbBmGpkg)
        sOut.add(f)

        # ================================================================ Section
        sSec = section("section", "Section line")
        self.bDraw = QPushButton(P.icon("section"), "Draw on map")
        self.bDraw.setCheckable(True)
        self.bDraw.setProperty("role", "primary")
        self.bDraw.setToolTip("Click A then B on the map canvas (snapping applies). Right-click/Esc cancels.")
        self.bDraw.toggled.connect(self._toggleDraw)
        self.canvas.mapToolSet.connect(self._mapToolChanged)
        bSel = QPushButton("Use selected line")
        bSel.setToolTip("First to last vertex of the selected line feature in the active layer")
        bSel.clicked.connect(self._useSelectedLine)
        bClr = P.tool("delete", "Clear the section", self.clearSection)
        sSec.add(P.hrow(self.bDraw, bSel, None, bClr))
        self.lblInfo = P.card("No section")
        sSec.add(self.lblInfo)

        self.spWidth = QDoubleSpinBox()
        self.spWidth.setRange(0.1, 100000)
        self.spWidth.setDecimals(1)
        self.spWidth.setValue(10.0)
        self.spWidth.setSuffix(" m")
        self.spWidth.setToolTip("Total section width (half either side of the section line)")
        self.spWidth.valueChanged.connect(self._update)
        self.spStep = QDoubleSpinBox()
        self.spStep.setRange(0.0, 100000)            # 0 = auto: half the section width
        self.spStep.setDecimals(1)
        self.spStep.setSuffix(" m")
        self.spStep.setSpecialValueText("Auto (½ width)")
        self.spStep.setToolTip("How far ◀ / ▶ move the section. Auto (the lowest value, type 0): half the "
                               "section width, so each step overlaps the last by half. Or type a distance.")
        from qgis.PyQt.QtCore import QSettings as _QS
        try:
            self.spStep.setValue(float(_QS().value("/QGeo3D/sectionStep", 0.0)))
        except (TypeError, ValueError):
            self.spStep.setValue(0.0)
        self.spStep.valueChanged.connect(lambda v: _QS().setValue("/QGeo3D/sectionStep", float(v)))
        self.spStep.valueChanged.connect(lambda _v: self._update())
        g2 = QGridLayout()
        g2.setContentsMargins(0, 0, 0, 0)
        g2.addWidget(QLabel("Width"), 0, 0)
        g2.addWidget(self.spWidth, 0, 1)
        g2.addWidget(QLabel("Step"), 0, 2)
        g2.addWidget(self.spStep, 0, 3)
        g2.setColumnStretch(1, 1)
        g2.setColumnStretch(3, 1)
        sSec.add(g2)

        bBack = QPushButton(P.icon("back"), "")
        bBack.setToolTip("Move the section towards the viewer by one step")
        bBack.clicked.connect(lambda: self.step(-1))
        self.lblOffset = QLabel("offset 0.0 m")
        self.lblOffset.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lblOffset.setStyleSheet("font-weight:600")
        bFwd = QPushButton(P.icon("fwd"), "")
        bFwd.setToolTip("Move the section away from the viewer by one step")
        bFwd.clicked.connect(lambda: self.step(+1))
        bLook = QPushButton(P.icon("look"), "Look along")
        bLook.setToolTip("Turn the 3D view to look along the section")
        bLook.clicked.connect(self.hook.viewSection)
        sSec.add(P.hrow(bBack, self.lblOffset, bFwd, bLook, stretch_at=1))

        self.cbClip = QCheckBox("Clip 3D view to section")
        self.cbClip.setChecked(True)
        self.cbClip.toggled.connect(self._update)
        self.cbEnds = QCheckBox("Limit to line ends")
        self.cbEnds.toggled.connect(self._update)
        self.cbFrame = QCheckBox("Section frame")
        self.cbFrame.setChecked(True)
        self.cbFrame.toggled.connect(self._update)
        sSec.add(P.hrow(self.cbClip, self.cbFrame, None))
        sSec.add(P.hrow(self.cbEnds, None))

        sSaved = section("savedsec", "Saved sections")
        self._buildSavedSections(sSaved)
        sViews = section("views", "Saved views")
        sViews.add(self._buildViews())

        # ================================================================ Blocks
        sBm = section("blocks", "Block model")
        bBm = QPushButton(P.icon("open"), "Load block model…")
        bBm.setProperty("role", "primary")
        bBm.setToolTip("Micromine .DAT, Datamine .dm, Vulcan .bmf or Surpac .mdl block model (read only). "
                       "Also GeoPackage / shapefile point layers of block centres. Sub-blocked and rotated models are supported.")
        bBm.clicked.connect(self.loadBlockModelDialog)
        bBmCsv = P.tool("/mActionOpenTable.svg", "Block model from CSV / Excel", lambda: self.tableBlocksDialog())
        bBmLyr = P.tool("/mActionAddOgrLayer.svg", "Block model from the active point layer (block centres, "
                        "e.g. a GeoPackage)", self.loadBlockModelActiveLayer)
        bBmGpkg = P.tool("/mIconGeoPackage.svg", "Block model from GeoPackage: choose from the block-centre "
                         "GeoPackages in the work / output / project folders, or browse", lambda: self.loadBlockModelGpkgDialog())
        sBm.add(P.hrow(bBm, bBmGpkg, bBmCsv, bBmLyr, None))
        self.cbBmField = QComboBox()
        self.cbBmField.currentIndexChanged.connect(lambda _=0: self._bmUpdate())
        self.cbBmMin = QCheckBox("Only ≥")
        self.spBmMin = QDoubleSpinBox()
        self.spBmMin.setRange(-1e9, 1e9)
        self.spBmMin.setDecimals(3)
        self.spBmMin.setValue(0.5)
        self.cbBmMin.toggled.connect(lambda _=0: self._bmUpdate())
        self.spBmMin.editingFinished.connect(self._bmUpdate)
        self.cbBmAir = QCheckBox("Hide")
        self.cbBmAir.setChecked(True)
        self.cbBmAir.setToolTip("Hide air, mined-out, void or other blocks matching the rule")
        self.cbBmAir.toggled.connect(lambda _=0: self._bmUpdate())
        self.leBmHide = QLineEdit()
        self.leBmHide.setPlaceholderText("e.g. density <= 0 or rock = 3")
        self.leBmHide.setToolTip("Blocks matching any part are hidden. Parts: <field> <op> <number>, "
                                 "ops = == != < <= > >=, joined with 'or' or commas.\n"
                                 "Examples: density <= 0 · insitu_pct <= 0 · rock = 3 or rock = 4 · void = 1")
        self.leBmHide.editingFinished.connect(self._bmUpdate)
        self.bBmHide = QToolButton()
        self.bBmHide.setIcon(_pw.icon("/mActionOptions.svg"))
        self.bBmHide.setAutoRaise(True)
        self.bBmHide.setStyleSheet("QToolButton::menu-indicator { image: none; }")
        self.bBmHide.setToolTip("Suggested rules from this model's fields")
        self.bBmHide.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.bBmHide.setMenu(QMenu(self.bBmHide))
        self.bBmHide.menu().aboutToShow.connect(self._fillHideMenu)
        self.spBmOp = QDoubleSpinBox()
        self.spBmOp.setRange(0.1, 1.0)
        self.spBmOp.setSingleStep(0.1)
        self.spBmOp.setValue(1.0)
        self.spBmOp.setToolTip("Opacity of the blocks")
        self.spBmOp.editingFinished.connect(self._bmUpdate)
        self.cbBmDisplay = QComboBox()
        self.cbBmDisplay.addItem("Solid blocks", "solid")
        self.cbBmDisplay.addItem("Outlines on section", "outline")
        self.cbBmDisplay.addItem("Solid + outlines", "both")
        self.cbBmDisplay.setToolTip("Outlines: each block cut by the section line, drawn as its outline "
                                    "shrunk towards its centre and coloured by class (needs a section)")
        self.cbBmDisplay.currentIndexChanged.connect(lambda _=0: self._bmDisplayChanged())
        self.spBmShrink = QSpinBox()
        self.spBmShrink.setRange(0, 50)
        self.spBmShrink.setValue(10)
        self.spBmShrink.setPrefix("shrink ")
        self.spBmShrink.setSuffix(" %")
        self.spBmShrink.setToolTip("Outlines are shrunk by this much about each block's centre, "
                                   "so neighbouring blocks stay apart")
        self.spBmShrink.valueChanged.connect(lambda _=0: self._bmSectionUpdate())
        f = QFormLayout()
        f.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        tBmCol = P.tool("/symbology.svg", "Colours…: edit the colour classes, load or save a QGIS style (.qml), "
                        "or take the style of the block-centre layer in QGIS", self.bmStyleDialog, text="Colours…")
        f.addRow("Colour by", P.hrow(self.cbBmField, tBmCol, stretch_at=0))
        f.addRow("Filter", P.hrow(self.cbBmMin, self.spBmMin, None))
        f.addRow(self.cbBmAir, P.hrow(self.leBmHide, self.bBmHide, stretch_at=0))
        f.addRow("Display", P.hrow(self.cbBmDisplay, self.spBmShrink, stretch_at=0))
        f.addRow("Opacity", P.hrow(self.spBmOp, None))
        sBm.add(f)

        sBmRes = section("bmresult", "Tonnes and grade")
        self.lblBm = P.card("Load a block model to see tonnes, grade and metal of the blocks shown.")
        sBmRes.add(self.lblBm)
        bGt = QPushButton(P.icon("report"), "Grade-tonnage report…")
        bGt.setToolTip("Tonnes, grade and metal by cut-off and resource category - for the blocks shown, all blocks, "
                       "blocks inside wireframes or in the section slab; curve, CSV / Excel export")
        bGt.clicked.connect(lambda: self.reportDialog())
        bWr = QPushButton("Inside wireframe…")
        bWr.setToolTip("Report of the blocks whose centres are inside the selected / ticked wireframes")
        bWr.clicked.connect(lambda: self.reportDialog("mesh"))
        bSaveG = QPushButton(P.icon("save"), "Save as GeoPackage…")
        bSaveG.setToolTip("Write the block centres (all blocks or those shown) with every field to a GeoPackage "
                          "and add it to QGIS")
        bSaveG.clicked.connect(self.saveBlocksGpkg)
        sBmRes.add(P.hrow(bGt, bWr))
        sBmRes.add(P.hrow(bSaveG, None))

        # ================================================================ Strings
        # ---- strings file: where strings go, and what is shown
        sFile = section("strfile", "Strings file")
        self.lblGpkg = P.PathLabel("")
        bGpkg = P.tool("/mActionFileOpen.svg", "Choose / create the GeoPackage that strings are saved to",
                       self.chooseGpkg)
        tAdd = P.tool("/mActionAddOgrLayer.svg", "Add the strings layer to the project for 2D editing "
                      "(refused for ringfenced projects)", self.addStringsLayerToProject)
        sFile.add(P.hrow(QLabel("File"), self.lblGpkg, bGpkg, tAdd, stretch_at=1))
        self.cbStrMap = QCheckBox("Show on map")
        self.cbStrMap.setChecked(True)
        self.cbStrMap.setToolTip("Show the strings on the QGIS map. When the strings layer is in the project this ticks "
                                 "it on / off in the Layers panel (and follows that tick).")
        self.cbStrMap.toggled.connect(self._strMapToggled)
        QgsProject.instance().layerTreeRoot().visibilityChanged.connect(self._syncStrMap)
        self.cbStr3D = QCheckBox("Show in 3D")
        self.cbStr3D.setChecked(True)
        self.cbStr3D.setToolTip("Show the strings in the 3D view (hidden strings can't be snapped to or edited)")
        self.cbStr3D.toggled.connect(self._strings3D)
        sFile.add(P.hrow(self.cbStrMap, self.cbStr3D, None))
        self.hook.itemsChanged.connect(self._syncStrings3D)
        self.lblStrings = P.hint("")
        sFile.add(self.lblStrings)

        # ---- digitise new strings
        sStr = section("strings", "Digitise")
        self.leName = QLineEdit("string")          # kept for older code paths; the layer picker replaces it
        self.leName.hide()
        self.cbLayer = QComboBox()
        self.cbLayer.setEditable(True)
        self.cbLayer.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.cbLayer.setMinimumContentsLength(12)
        self.cbLayer.setToolTip("String layer that new strings (and copies) go into. Pick one, or type a new name "
                                "to start a new layer. Click a layer in the Wireframing list to make it active.")
        self.cbLayer.setEditText("strings")
        self.cbLayer.currentTextChanged.connect(lambda *_: QTimer.singleShot(0, self._fillStringList))
        self.bDig = QPushButton(P.icon("string"), "Digitise in 3D")
        self.bDig.setCheckable(True)
        self.bDig.setProperty("role", "primary")
        self.bDig.setToolTip("Click points in the 3D view. Backspace: undo point · Enter / right-click / "
                             "double-click: finish string · Esc: cancel string (twice: stop)")
        self.bDig.toggled.connect(self._toggleDigitise)
        sStr.add(P.hrow(QLabel("Layer"), self.cbLayer, self.bDig, stretch_at=1))
        self.cbClosed = QCheckBox("Close outlines")
        self.cbClosed.setChecked(True)
        self.cbClosed.setToolTip("Join the last point to the first. Closed outlines are needed to link strings "
                                 "into a solid wireframe.")
        self.cbTrace = QCheckBox("Trace (T)")
        self.cbTrace.setToolTip("Two clicks on the same string add the vertices between them (the shorter way round "
                                "a closed outline); two clicks on a surface follow the surface. "
                                "Press T in the 3D view to toggle it.")
        self.cbTrace.toggled.connect(lambda on: self.hook.js(f"window.Q2T4 && Q2T4.setTrace({'true' if on else 'false'})"))
        tDel = P.tool("/mActionUndo.svg", "Delete the last string", self.deleteLastString)
        sStr.add(P.hrow(self.cbClosed, self.cbTrace, None, tDel))

        # ---- snapping: one set of controls for digitising, editing, measuring and triangles
        sSnap = section("snapping", "Snapping")
        self.cbSnapOn = QCheckBox("Snap (S)")
        self.cbSnapOn.setChecked(True)
        self.cbSnapOn.setToolTip("Snap while digitising, editing, measuring or adding triangles. "
                                 "Press S in the 3D view to toggle it.")
        self.cbSnapOn.toggled.connect(lambda on: self.hook.js(f"window.Q2T4 && Q2T4.toggleSnap({'true' if on else 'false'})"))
        self.cbSnapMode = QComboBox()
        for k, lab, tip in (("all", "All", "Vertices first, then surfaces, then the section plane (the ticks below apply)"),
                            ("node", "Nodes", "Vertices only: string and wireframe vertices, drillhole interval ends"),
                            ("string", "String lines", "Anywhere along a string line (vertices included)"),
                            ("surface", "Surfaces", "Surfaces only: wireframes, DEM, Qgis2threejs layers")):
            self.cbSnapMode.addItem(lab, k)
            self.cbSnapMode.setItemData(self.cbSnapMode.count() - 1, tip, Qt.ItemDataRole.ToolTipRole)
        self.cbSnapMode.setToolTip("What points snap to. Nothing in reach: the section plane, else the plane "
                                   "through the orbit point.")
        from qgis.PyQt.QtCore import QSettings as _QS
        self.cbSnapMode.setCurrentIndex(max(0, self.cbSnapMode.findData(_QS().value("/QGeo3D/snapMode", "all"))))
        self.cbSnapMode.currentIndexChanged.connect(self._snapModeChanged)
        self.spTol = QSpinBox()
        self.spTol.setRange(2, 50)
        self.spTol.setValue(12)
        self.spTol.setSuffix(" px")
        self.spTol.setPrefix("within ")
        self.spTol.setToolTip("Snap distance on screen")
        self.spTol.valueChanged.connect(self._snapChanged)
        sSnap.add(P.hrow(self.cbSnapOn, self.cbSnapMode, self.spTol, None))
        self.cbSnapSec = QCheckBox("Section plane")
        self.cbSnapSurf = QCheckBox("Surfaces")
        self.cbSnapDh = QCheckBox("Drillholes")
        self.cbSnapStr = QCheckBox("String vertices")
        for cb in (self.cbSnapSec, self.cbSnapSurf, self.cbSnapDh, self.cbSnapStr):
            cb.setChecked(True)
            cb.toggled.connect(self._snapChanged)
        self.cbSnapSurf.setToolTip("DEM / wireframes / Q2T layers (inside the section). With a section active, "
                                   "hits are projected onto the section plane.")
        self.wSnapKinds = QWidget()
        grid = QGridLayout(self.wSnapKinds)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.addWidget(QLabel("Include"), 0, 0)
        grid.addWidget(self.cbSnapStr, 0, 1)
        grid.addWidget(self.cbSnapSurf, 0, 2)
        grid.addWidget(self.cbSnapDh, 1, 1)
        grid.addWidget(self.cbSnapSec, 1, 2)
        grid.setColumnStretch(3, 1)
        sSnap.add(self.wSnapKinds)
        self.cbSnapOn.toggled.connect(self._snapUi)
        QTimer.singleShot(0, self._snapUi)

        # ---- edit strings already digitised
        sEd = section("strtools", "Edit strings")
        from qgis.PyQt.QtWidgets import QButtonGroup
        self._toolGroup = QButtonGroup(self)
        self._toolGroup.setExclusive(False)
        self.strToolButtons = {}
        for mode, ic, text, tip in (
                ("select", "select", "Select", "Click a string to select it · Ctrl+click adds / removes · drag a box "
                                              "on empty space · Delete deletes the selected strings. Selection follows "
                                              "the Wireframing list and the QGIS strings layer."),
                ("move", "editv", "Move", "Drag vertices of strings (and of a wireframe selected in Objects); "
                                         "Delete removes the vertex under the cursor"),
                ("insert", "/mActionAddNodesItem.svg", "Insert", "Click on a string segment to add a point there"),
                ("cut", "string_cut", "Cut", "Click a vertex or segment: an open string splits in two there; "
                                                         "a closed outline opens (the clicked segment is removed)"),
                ("copy", "/mActionEditCopy.svg", "Copy", "Click a string to duplicate it (asks for an offset)")):
            b = P.tool(ic, tip, text=text, checkable=True)
            b.toggled.connect(lambda on, m=mode: self.setStringTool(m if on else None, sender=m))
            self.strToolButtons[mode] = b
            self._toolGroup.addButton(b)
        tClose = P.tool("string_close", "Close the selected open strings (join the last point to the "
                        "first). Select them with Select, or in the Wireframing list.", self.closeSelectedStrings,
                        text="Close")
        sEd.add(P.hrow(*self.strToolButtons.values(), tClose, None))

        sWf = section("wireframing", "Wireframing")
        sWf.add(P.hint("Wireframes and strings · tick = shown in 3D · double-click zooms. Select a wireframe to add "
                       "triangles / linked strings to it; with none selected a new wireframe is made."))
        self.lstStrings = QListWidget()
        self.lstStrings.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.lstStrings.setMinimumHeight(110)
        self.lstStrings.setAlternatingRowColors(True)
        self.lstStrings.setToolTip("Wireframes in the 3D view and the strings in the strings GeoPackage (File above).\n"
                                   "Tick = shown in 3D · double-click zooms.\n"
                                   "Select a wireframe: Add triangles and Link selected put their triangles into it.\n"
                                   "Select two or more strings (Ctrl/Shift-click) to link: closed outlines are ordered "
                                   "section by section, open strings link in number order.")
        sWf.add(self.lstStrings, 1)
        self.lstStrings.itemSelectionChanged.connect(self._listSelectionTo3D)
        self.lstStrings.itemChanged.connect(self._wfListTicked)
        self.lstStrings.itemDoubleClicked.connect(self._zoomString)
        self.lstStrings.itemClicked.connect(self._listClicked)
        self.lstStrings.itemPressed.connect(self._listPressed)
        self.lstStrings.viewport().installEventFilter(self)
        self.lstStrings.setEditTriggers(QAbstractItemView.EditTrigger.EditKeyPressed |
                                        QAbstractItemView.EditTrigger.SelectedClicked)
        self.lstStrings.setItemDelegate(_NameDelegate(self))
        self.lstStrings.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.lstStrings.customContextMenuRequested.connect(self._stringListMenu)
        self.lstStrings.itemSelectionChanged.connect(self._targetLabel)
        self.hook.itemsChanged.connect(self._fillStringList)
        self.bCap = QPushButton(P.icon("string_cap"), "Close end")
        self.bCap.setCheckable(True)
        self.bCap.setToolTip("Click a closed outline in the 3D view to cap the wireframe there (its outline is "
                             "triangulated and joined to the wireframe that uses its points - or to the wireframe "
                             "selected in the list). Tie lines (2-point strings) drawn across the outline between "
                             "two of its vertices become edges of the cap. Clicking a capped outline again redoes "
                             "its cap. Linking itself leaves the ends open. Backspace undoes, Esc ends.")
        self.bCap.toggled.connect(self.setCapPick)
        self.spSG = QDoubleSpinBox()
        self.spSG.setRange(0.1, 20)
        self.spSG.setDecimals(2)
        self.spSG.setValue(2.70)
        self.spSG.setPrefix("SG ")
        self.spSG.setToolTip("Density (t/m³) used for tonnage")
        bLink = QPushButton(P.icon("link"), "Link selected")
        bLink.setToolTip("Link the strings selected in the list into the wireframe chosen in Into. Closed outlines are linked section by section; 2-point strings are tie lines. Ends stay open; use Close end.")
        self.bLink = bLink
        bLink.setProperty("role", "primary")
        bLink.clicked.connect(self.linkSelected)
        tLoad = P.tool("/mActionShowAllLayers.svg", "Show the wireframes saved in the strings GeoPackage",
                       self.showSavedWireframes)
        tWfRem = P.tool("/mActionRemoveLayer.svg", "Remove the wireframes selected in this box from the 3D view - "
                        "asks whether to delete them from their GeoPackage too", self._removeSelectedWireframes)
        tCheck = P.tool("/mActionCheckGeometry.svg", "Check the wireframes selected in this box (all, if none is): "
                        "open edges, edges on 3+ triangles, winding, inside-out, zero-area / duplicate triangles, "
                        "unjoined corners - with Show problems and Repair", self.validateSelected, text="Validate")
        sWf.add(P.hrow(self.spSG, None, tCheck, tLoad, tWfRem))
        self.lblWfFile = P.PathLabel("")
        bWfFile = P.tool("/mActionFileOpen.svg", "Save wireframes in the strings GeoPackage or in a separate one",
                         self.chooseWireframeGpkg)
        sWf.add(P.hrow(QLabel("Saved in"), self.lblWfFile, bWfFile, stretch_at=1))
        QgsProject.instance().readProject.connect(lambda *_: self._wfFileLabel())
        QTimer.singleShot(0, self._wfFileLabel)
        self.bLinkPick = QPushButton(P.icon("/mActionSelectFreehand.svg"), "Link one by one")
        self.bLinkPick.setCheckable(True)
        self.bLinkPick.setToolTip("Click strings in the 3D view one after another: each is linked to the one before "
                                  "(triangles follow both strings by proportion of their length, not the nearest "
                                  "point). The wireframe builds as you go (orange). Backspace undoes the last string, "
                                  "Enter / Esc finishes and saves. With a wireframe selected in the list the "
                                  "triangles go into it.")
        self.bLinkPick.toggled.connect(self.setLinkPick)
        self.cbWf = QComboBox()
        self.cbWf.setToolTip("Wireframe that Link selected, Link one by one, Close end and Add triangles put their "
                             "triangles into. 'New wireframe' makes a new one (asks for a name), which then becomes "
                             "active. Click a wireframe in the list to make it active.")
        self.cbWf.addItem("New wireframe", None)
        self.cbWf.currentIndexChanged.connect(lambda *_: self._targetLabel())
        sWf.add(P.hrow(QLabel("Into"), self.cbWf, stretch_at=1))
        sWf.add(P.hrow(bLink, self.bLinkPick, self.bCap, stretch_at=0))
        self.bTri = QPushButton(P.icon("/mActionCapturePolygon.svg"), "Add triangles")
        self.bTri.setCheckable(True)
        self.bTri.setToolTip("Asks which wireframe to put them in, then click three corners per triangle in the 3D "
                             "view - snaps as set in Snapping (S toggles it). "
                             "Backspace removes the last corner, Esc ends.")
        self.bTri.toggled.connect(self.setTriangles)
        self.bTriDel = QPushButton(P.icon("/mActionDeleteSelected.svg"), "Delete triangles")
        self.bTriDel.setCheckable(True)
        self.bTriDel.setToolTip("Click a triangle of any wireframe in the 3D view to delete it (it shows red under the "
                                "cursor). Saved wireframes are updated in the strings GeoPackage. Undo puts it back. "
                                "Esc ends.")
        self.bTriDel.toggled.connect(self.setTriDelete)
        tTriUndo = P.tool("/mActionUndo.svg", "Undo the last string or wireframe edit (Ctrl+Z)", self.undoAny)
        tTriRedo = P.tool("/mActionRedo.svg", "Redo (Ctrl+Y or Ctrl+Shift+Z)", self.redoAny)
        from qgis.PyQt.QtGui import QKeySequence
        try:
            from qgis.PyQt.QtGui import QShortcut                  # Qt6
        except ImportError:
            from qgis.PyQt.QtWidgets import QShortcut              # Qt5
        for keys, slot in (("Ctrl+Z", self.undoAny), ("Ctrl+Y", self.redoAny), ("Ctrl+Shift+Z", self.redoAny)):
            sc = QShortcut(QKeySequence(keys), self)
            sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            sc.activated.connect(slot)
        tTriSave = P.tool("save", "Save the triangles as a new wireframe (strings GeoPackage)", self.saveTriangles)
        tTriAdd = P.tool("/mActionAddNodesItem.svg", "Put the blue (pending) triangles into a wireframe - "
                         "choose it from the list", self.addTrianglesToWireframe)
        self.tTriAdd = tTriAdd
        tTriClr = P.tool("delete", "Delete what is selected in the list above (strings and / or wireframes, from the "
                         "GeoPackage - it warns first); with nothing selected, discard the pending (blue) triangles",
                         self.deleteSelected)
        sWf.add(P.hint("Triangles · snapping as set above"))
        sWf.add(P.hrow(self.bTri, self.bTriDel, None, tTriUndo, tTriRedo, tTriAdd, tTriSave, tTriClr))
        self.lblTri = P.hint("")
        sWf.add(self.lblTri)
        self.lblWf = P.hint("")
        sWf.add(self.lblWf)

        # ================================================================ Query
        sQ = section("query", "Query")
        self._buildQuery(sQ)

        # ================================================================ tabs
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.setObjectName("q3dPanel")
        self.tabs.setStyleSheet(P.STYLE)
        bodies = []
        for title, ic, secs, stretch in (("Objects", "open", (sObj, sDh, sOut), False),
                                         ("Section", "section", (sSec, sSaved, sViews), False),
                                         ("Blocks", "blocks", (sBm, sBmRes), False),
                                         ("Strings", "string", (sFile, sStr, sSnap, sEd, sWf), False),
                                         ("Query", "query", (sQ,), True)):
            sa, body = self._page(*secs, stretch_last=stretch)
            bodies.append(body)
            self.tabs.addTab(sa, P.icon(ic), title)
        self.tabs.setMinimumWidth(min(420, max(b.minimumSizeHint().width() for b in bodies) + 24))
        self.setWidget(self.tabs)
        self._remember["queryHeader"] = self.tblQ.horizontalHeader()

    def _buildSavedSections(self, sec):
        P = _pw
        self.lstSec = QListWidget()
        self.lstSec.setMinimumHeight(90)
        self.lstSec.setMaximumHeight(160)
        self.lstSec.setAlternatingRowColors(True)
        self.lstSec.setToolTip("Sections saved in the strings GeoPackage. Double-click to go to one.")
        self.lstSec.itemDoubleClicked.connect(lambda _it: self.loadSavedSection())
        sec.add(self.lstSec)
        bSave = QPushButton(P.icon("save"), "Save current…")
        bSave.clicked.connect(self.saveSection)
        tGo = P.tool("look", "Go to the selected section", self.loadSavedSection)
        tDel = P.tool("/mActionDeleteSelected.svg", "Delete the selected section", self.deleteSavedSection)
        sec.add(P.hrow(bSave, None, tGo, tDel))

    def _buildQuery(self, sec):
        P = _pw
        self.bQuery = QPushButton(P.icon("query"), "Query mode")
        self.bQuery.setCheckable(True)
        self.bQuery.setProperty("role", "primary")
        self.bQuery.setToolTip("Click objects in the 3D view to see their attributes here (Esc clears)")
        self.bQuery.toggled.connect(self._toggleQuery)
        self.bQSel = P.tool("select", "Select the queried feature in QGIS", self._querySelect, text="Select in QGIS")
        self.bQZoom = P.tool("orbit", "Make the queried point the 3D orbit centre", self._queryZoom, text="Orbit here")
        sec.add(P.hrow(self.bQuery, None, self.bQSel, self.bQZoom))
        self.lblQTitle = P.hint("Click an object in the 3D view with Query on.")
        self.lblQTitle.setTextFormat(Qt.TextFormat.RichText)
        sec.add(self.lblQTitle)
        self.tblQ = QTableWidget(0, 2)
        self.tblQ.setHorizontalHeaderLabels(["Field", "Value"])
        self.tblQ.horizontalHeader().setStretchLastSection(True)
        self.tblQ.verticalHeader().setVisible(False)
        self.tblQ.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tblQ.setAlternatingRowColors(True)
        self.tblQ.verticalHeader().setDefaultSectionSize(20)
        self.tblQ.setMinimumHeight(240)
        sec.add(self.tblQ, 1)
        self.bQSel.setEnabled(False)
        self.bQZoom.setEnabled(False)
        self._lastQuery = None

    # ------------------------------------------------------------ remembered panel state
    UI_KEY = "/QGeo3D/panel/"

    def saveUiState(self):
        from qgis.PyQt.QtCore import QSettings
        s = QSettings()
        s.setValue(self.UI_KEY + "tab", self.tabs.currentIndex())
        for key, w in self._uiRemember().items():
            try:
                if hasattr(w, "isExpanded"):
                    s.setValue(self.UI_KEY + key, w.isExpanded())
                elif hasattr(w, "saveState"):
                    s.setValue(self.UI_KEY + key, w.saveState())
            except RuntimeError:
                pass

    def restoreUiState(self):
        from qgis.PyQt.QtCore import QSettings
        s = QSettings()
        try:
            self.tabs.setCurrentIndex(int(s.value(self.UI_KEY + "tab", 0) or 0))
        except (TypeError, ValueError):
            pass
        for key, w in self._uiRemember().items():
            v = s.value(self.UI_KEY + key)
            if v is None:
                continue
            try:
                if hasattr(w, "setExpanded"):
                    w.setExpanded(str(v).lower() in ("true", "1"))
                elif hasattr(w, "restoreState"):
                    w.restoreState(v)
            except (RuntimeError, TypeError):
                pass

    def _uiRemember(self):
        """name -> widget whose state is kept between sessions (collapsible sections, splitters, headers)."""
        return dict(getattr(self, "_remember", {}))

    # ------------------------------------------------- wireframe on/off list
    OVERLAY_LABELS = {"drill": "Drillholes", "strings": "Strings", "blocksec": "Block outlines on section",
                      "check:open": "Wireframe check: open edges", "check:nonmanifold": "Wireframe check: edges on 3+ triangles",
                      "check:bad": "Wireframe check: bad triangles"}

    def _fillWfList(self):
        lst = self.lstWf
        sel = {it.data(Qt.ItemDataRole.UserRole) for it in lst.selectedItems()}
        lst.blockSignals(True)
        lst.clear()
        for mid, mesh in self.hook.meshes.items():
            it = QListWidgetItem(f"{mesh.name}  ·  {len(mesh.triangles):,} tri")
            it.setData(Qt.ItemDataRole.UserRole, ("mesh", mid))
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Unchecked if mid in self.hook.hiddenMeshes else Qt.CheckState.Checked)
            lst.addItem(it)
            it.setSelected(("mesh", mid) in sel)
        for bid, bm in self.hook.blockModels.items():
            it = QListWidgetItem(f"{bm.name}  ·  {len(bm.shown):,} blocks")
            it.setData(Qt.ItemDataRole.UserRole, ("mesh", bid))
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Unchecked if self.blocksHidden(bid) else Qt.CheckState.Checked)
            lst.addItem(it)
            it.setSelected(("mesh", bid) in sel)
        for name in self.hook.overlays:
            label = self.OVERLAY_LABELS.get(name, name)
            if name.startswith("surpac:"):
                label = f"Surpac strings: {name[7:]}"
            elif name.startswith("mm:"):
                label = f"Micromine strings: {name[3:]}"
            elif name.startswith("mmpts:"):
                label = f"Micromine points: {name[6:]}"
            if name == "drill" and self.drillLayerId:
                lyr = QgsProject.instance().mapLayer(self.drillLayerId)
                if lyr is not None:
                    label += f": {lyr.name()}"
            it = QListWidgetItem(label)
            it.setData(Qt.ItemDataRole.UserRole, ("overlay", name))
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Unchecked if name in self.hook.hiddenOverlays else Qt.CheckState.Checked)
            lst.addItem(it)
            it.setSelected(("overlay", name) in sel)
        for lid, name, shown in self._q2tEntries():   # Qgis2threejs layers ticked in the 3D layer list
            it = QListWidgetItem(QgsIconUtils.iconForLayer(QgsProject.instance().mapLayer(lid)), f"{name}  ·  3D layer")
            it.setData(Qt.ItemDataRole.UserRole, ("q2t", lid))
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Checked if shown else Qt.CheckState.Unchecked)
            it.setToolTip(f"{name}: a QGIS layer shown by Qgis2threejs (ticked in the 3D window's layer list).\n"
                          "Tick / untick shows / hides it there too · double-click zooms · Remove takes it off this "
                          "list (and hides it).")
            lst.addItem(it)
            it.setSelected(("q2t", lid) in sel)
        gp = os.path.basename(self.store.path) if getattr(self.store, "path", None) else ""
        for i in range(lst.count()):             # where strings and built wireframes are kept
            it = lst.item(i)
            kind, key = it.data(Qt.ItemDataRole.UserRole)
            if kind == "overlay" and key == "strings" and gp:
                shown, total = getattr(self, "_stringCounts", (None, None))
                cnt = f"{shown} of {total} shown" if total is not None and shown != total else f"{total} strings"
                it.setText(f"Strings  ·  {cnt}  ·  {gp}")
                it.setToolTip(f"Digitised strings, saved in\n{self.store.path}\n\nUntick to hide them all. "
                              "Strings tab ▸ Wireframing list: tick / untick single strings.")
            elif kind == "mesh" and str(key).startswith("built") and self.wireframeGpkg():
                wp = self.wireframeGpkg()
                it.setText(f"{it.text()}  ·  saved in {os.path.basename(wp)}")
                it.setToolTip(f"Wireframe built in QGeo 3D, saved in layer 'wireframes' of\n{wp}\n"
                              "(Strings tab ▸ Wireframing ▸ Wireframes saved in)")
        if "strings" not in self.hook.overlays and gp and getattr(self, "_stringCounts", (0, 0))[1]:
            it = QListWidgetItem(f"Strings  ·  all {self._stringCounts[1]} hidden  ·  {gp}")    # all ticked off
            it.setData(Qt.ItemDataRole.UserRole, ("strings", "all"))
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Unchecked)
            it.setToolTip(f"Digitised strings, saved in\n{self.store.path}\n\nTick to show them all again.")
            lst.addItem(it)
        for i in range(lst.count()):             # source file + CRS of imported items
            it = lst.item(i)
            info = getattr(self, "imports", {}).get(it.data(Qt.ItemDataRole.UserRole)[1])
            if info and info.get("crs") is not None:
                c = info["crs"]
                it.setText(f"{it.text()}  ·  {c.authid() or 'custom CRS'}")
                it.setToolTip(f"{os.path.basename(info['path'])}\nCoordinate system: {c.authid()} {c.description()}"
                              "\n(CRS… to change)")
        lst.blockSignals(False)
        self._fillDhLayers()

    def _q2tEntries(self):
        """[(layerId, name, shown)] of the Qgis2threejs layers pinned to the Objects list."""
        w = self.hook._window
        out = []
        if w is None:
            return out
        try:
            for lid in list(getattr(self, "_q2tPinned", [])):
                lyr = w.settings.getLayer(lid)
                if lyr is None or QgsProject.instance().mapLayer(lid) is None:
                    self._q2tPinned.remove(lid)
                    continue
                out.append((lid, lyr.name, bool(lyr.visible)))
        except RuntimeError:                          # window gone
            pass
        return out

    def q2tLayerToggled(self, layerId, on):
        """A layer was ticked / unticked in the 3D window's layer list: keep it on the Objects list."""
        pins = self.__dict__.setdefault("_q2tPinned", [])
        if on and layerId not in pins:
            pins.append(layerId)
        QTimer.singleShot(0, self._fillWfList)

    def _wfItemChanged(self, it):
        if it is None:                       # the list was rebuilt while Qt was still delivering the signal
            return
        kind, key = it.data(Qt.ItemDataRole.UserRole)
        on = it.checkState() == Qt.CheckState.Checked
        if kind == "q2t":
            bt = getattr(self.plugin, "blockTree", None)
            if bt is not None:
                bt.setLayerShown(key, on)
            return
        if kind == "strings":                    # every string had been ticked off one by one
            if on:
                self._hiddenStrings = set()
                QgsProject.instance().writeEntry(*self.HIDDEN_KEY, "[]")
                self.hook.setOverlayVisible("strings", True)
                self.refreshStrings()
            return
        if kind == "mesh" and key in self.hook.blockModels:
            self.setBlocksShown(key, on)
        elif kind == "mesh":
            self.hook.setMeshVisible(key, on)
        else:
            self.hook.setOverlayVisible(key, on)

    def _strings3D(self, on):
        self.hook.setOverlayVisible("strings", on)
        if not on and getattr(self, "_editMode", None):           # nothing left to edit
            self.setStringTool(None)
            self._syncToolButtons(None)

    def _strMapLayers(self):
        """Project layers showing the strings GeoPackage's strings table."""
        p = os.path.normcase(os.path.normpath(self.store.path)) if getattr(self.store, "path", None) else ""
        if not p:
            return []
        out = []
        for l in QgsProject.instance().mapLayers().values():
            src = l.source()
            if os.path.normcase(os.path.normpath(src.split("|")[0])) == p and "layername=strings" in src.lower():
                out.append(l)
        return out

    def _strMapToggled(self, on):
        """Show on map: tick / untick the strings layer in the Layers panel (or draw them as a map overlay
        when the layer isn't in the project)."""
        root = QgsProject.instance().layerTreeRoot()
        for l in self._strMapLayers():
            n = root.findLayer(l.id())
            if n is None:
                continue
            if on:
                n.setItemVisibilityCheckedParentRecursive(True)      # its group too, else it stays hidden
            else:
                n.setItemVisibilityChecked(False)
        self.refreshStrings()

    def _syncStrMap(self, *_):
        """Layers panel tick -> the Show on map box."""
        try:
            lays = self._strMapLayers()
            if not lays:
                return
            root = QgsProject.instance().layerTreeRoot()
            vis = any(root.findLayer(l.id()) is not None and root.findLayer(l.id()).isVisible() for l in lays)
            if self.cbStrMap.isChecked() != vis:
                self.cbStrMap.blockSignals(True)
                self.cbStrMap.setChecked(vis)
                self.cbStrMap.blockSignals(False)
        except RuntimeError:
            pass

    def _syncStrings3D(self):
        on = "strings" not in self.hook.hiddenOverlays
        if self.cbStr3D.isChecked() != on:
            self.cbStr3D.blockSignals(True)
            self.cbStr3D.setChecked(on)
            self.cbStr3D.blockSignals(False)

    def _snapUi(self, *_):
        """Dim the snap controls when snapping is off; the Include ticks only apply in All mode."""
        on = self.cbSnapOn.isChecked()
        self.cbSnapMode.setEnabled(on)
        self.spTol.setEnabled(on)
        self.wSnapKinds.setVisible(self.cbSnapMode.currentData() == "all")
        self.wSnapKinds.setEnabled(on)

    def _snapModeChanged(self, *_):
        from qgis.PyQt.QtCore import QSettings
        m = self.cbSnapMode.currentData()
        QSettings().setValue("/QGeo3D/snapMode", m)
        self.cbSnapOn.blockSignals(True)
        self.cbSnapOn.setChecked(True)
        self.cbSnapOn.blockSignals(False)
        self.hook.js(f"window.Q2T4 && Q2T4.setSnapMode && Q2T4.setSnapMode({json.dumps(m)})")
        self._snapUi()

    def _wfSetAll(self, on):
        for mid in list(self.hook.meshes):
            self.hook.setMeshVisible(mid, on)
        for bid in list(self.hook.blockModels):
            self.setBlocksShown(bid, on)
        bt = getattr(self.plugin, "blockTree", None)
        if bt is not None:
            for lid, _n, shown in self._q2tEntries():
                if shown != on:
                    bt.setLayerShown(lid, on)

    def _wfZoom(self, it):
        """Double-click: show the item if it was ticked off, then zoom to it (view direction kept)."""
        if it is None:
            return
        kind, key = it.data(Qt.ItemDataRole.UserRole)
        if kind == "q2t":
            bt = getattr(self.plugin, "blockTree", None)
            if bt is not None:
                bt.zoomLayer(key)
            return
        outline_mode = key in self.hook.blockModels and self.cbBmDisplay.currentData() == "outline"
        if kind == "mesh" and key in self.hook.blockModels and self.blocksHidden(key):
            self.setBlocksShown(key, True)
        elif kind == "mesh" and key in self.hook.hiddenMeshes and not outline_mode:
            self.hook.setMeshVisible(key, True)
        elif kind == "overlay" and key in self.hook.hiddenOverlays:
            self.hook.setOverlayVisible(key, True)
        self.hook.zoomTo(key)

    def _wfRemove(self):
        keys = [it.data(Qt.ItemDataRole.UserRole) for it in self.lstWf.selectedItems()]
        q2t = [k[1] for k in keys if isinstance(k, (tuple, list)) and k[0] == "q2t"]
        if q2t:                                   # 3D layers: hide and take off the list (the layer stays in QGIS)
            bt = getattr(self.plugin, "blockTree", None)
            for lid in q2t:
                if bt is not None:
                    bt.setLayerShown(lid, False)
                if lid in getattr(self, "_q2tPinned", []):
                    self._q2tPinned.remove(lid)
            self._fillWfList()
        rest = [k for k in keys if isinstance(k, (tuple, list)) and k[0] != "q2t"]
        if rest or not q2t:
            self.removeItems(rest)

    def _removeSelectedWireframes(self):
        keys = [("mesh", d[1]) for d in (self._key(it) for it in self.lstStrings.selectedItems())
                if isinstance(d, (tuple, list)) and d[0] == "wf"]
        if not keys:
            self.plugin._msg("Select the wireframes to remove in the Wireframing box first.", Qgis.MessageLevel.Info, 5)
            return
        self.removeItems(keys)

    def removeItems(self, keys):
        """Remove items from the 3D view; offers to delete what QGeo 3D saved for them as well
        (a saved wireframe's entry in its GeoPackage, or GeoPackages written on import). Source files
        (.dm, .DAT, DXF, ...) are never deleted, nor anything in a protected folder."""
        from qgis.PyQt.QtWidgets import QMessageBox
        if not keys:
            self.plugin._msg("Select what to remove first.", Qgis.MessageLevel.Info, 4)
            return
        files, saved = set(), []
        for kind, key in keys:
            if kind == "mesh" and str(key).startswith("built") and self.wireframeGpkg():
                saved.append(key)
            for f in getattr(self, "gpkgOf", {}).get(key, ()):
                if os.path.exists(f) and not is_protected(f):
                    files.add(f)
        delete = False
        if files or saved:
            lines = [f"  • wireframe '{self.hook.meshes[k].name}' in {os.path.basename(self.wireframeGpkg())}"
                     for k in saved if k in self.hook.meshes]
            lines += [f"  • {os.path.basename(f)}  ({os.path.dirname(f)})" for f in sorted(files)]
            mb = QMessageBox(QMessageBox.Icon.Question, "Remove",
                             f"Remove {len(keys)} item(s) from the 3D view.\n\nAlso delete what QGeo 3D saved for them?\n"
                             + "\n".join(lines) + "\n\nSource files are never deleted.", parent=self)
            bView = mb.addButton("Remove from view only", QMessageBox.ButtonRole.AcceptRole)
            bDel = mb.addButton("Remove and delete", QMessageBox.ButtonRole.DestructiveRole)
            mb.addButton(QMessageBox.StandardButton.Cancel)
            mb.setDefaultButton(bView)
            mb.exec()
            if mb.clickedButton() not in (bView, bDel):
                return
            delete = mb.clickedButton() is bDel
        for kind, key in keys:
            if key != "strings":
                getattr(self, "imports", {}).pop(key, None)
            if kind == "mesh" and key in self.hook.blockModels:
                self.hook.removeBlocks(key)
                self._bmSectionUpdate()
                self._bmUpdateLabel()
            elif kind == "mesh":
                self.hook.removeMesh(key)
                if key == "manual":
                    self._manualTris = []
            elif key == "drill":
                self.clearDrill()
            elif key == "blocksec":
                self.cbBmDisplay.setCurrentIndex(0)
            elif str(key).startswith(("surpac:", "mm:", "mmpts:", "csv:")):
                self.hook.setOverlay(key, None)
            elif key == "strings":
                self.plugin._msg("Strings stay while a strings GeoPackage is open - untick to hide them, or "
                                 "delete them with Edit strings ▸ Select.", Qgis.MessageLevel.Info)
        if not delete:
            return
        from ..core.strings import delete_saved_wireframe
        done, failed = [], []
        for k in saved:
            try:
                if delete_saved_wireframe(self.wireframeGpkg(), int(str(k)[5:])):
                    done.append(f"wireframe #{str(k)[5:]}")
            except Exception as e:
                failed.append(f"wireframe #{str(k)[5:]}: {e}")
        for f in sorted(files):
            try:
                self._dropLayersUsing(f)
                os.remove(f)
                done.append(os.path.basename(f))
            except OSError as e:
                failed.append(f"{os.path.basename(f)}: {e} (QGIS may still hold it open - try after a restart)")
        for k in [k for k, v in getattr(self, "gpkgOf", {}).items() if v & files]:
            self.gpkgOf[k] -= files
        self._fillWfList()
        if done:
            self.plugin._msg("Deleted: " + ", ".join(done), Qgis.MessageLevel.Info, 8)
        if failed:
            self.plugin._msg("Not deleted: " + "; ".join(failed), Qgis.MessageLevel.Warning, 12)

    # ---------------------------------------------------------------- query
    def _queryCandidates(self):
        """Visible line/point layers to test against the click ray: label, QgsVectorLayer."""
        out, seen = [], set()
        w = self.hook._window
        if w is not None:
            for lyr in w.settings.layers():
                if lyr.visible and lyr.type in (1, 2) and lyr.mapLayer is not None:     # POINT, LINESTRING
                    out.append((lyr.name, lyr.mapLayer))
                    seen.add(lyr.mapLayer.id())
        if self.drillLayerId and "drill" in self.hook.overlays and "drill" not in self.hook.hiddenOverlays:
            dl = QgsProject.instance().mapLayer(self.drillLayerId)
            if dl is not None and dl.id() not in seen:
                out.append((dl.name(), dl))
                seen.add(dl.id())
        if self.store.layer is not None and "strings" in self.hook.overlays and "strings" not in self.hook.hiddenOverlays:
            if self.store.layer.id() not in seen:
                out.append(("Strings", self.store.layer))
        return out

    def _handleQuery(self, ev):
        from ..core import query as Q
        import json
        hit = ev.get("hit")
        if hit and hit.get("block") is not None and hit["block"] in self.hook.blockModels:
            bm = self.hook.blockModels[hit["block"]]
            try:
                i = int(bm.classes[hit["cls"]][2][hit["instance"]])
                rows = bm.row(i)
                res = {"title": f"{bm.name}  ·  block {i + 1}", "rows": rows,
                       "point": [float(x) for x in bm.world(bm.xyz[i])], "highlight": None}
                html = self._showQuery(res)
                c, sz = bm.xyz[i], bm.size[i] / 2.0
                corners = [[c[0] + dx * sz[0], c[1] + dy * sz[1], c[2] + dz * sz[2]] for dx, dy, dz in
                           ((-1, -1, -1), (1, -1, -1), (1, 1, -1), (-1, 1, -1), (-1, -1, -1), (-1, -1, 1), (1, -1, 1),
                            (1, 1, 1), (-1, 1, 1), (-1, -1, 1), (-1, 1, 1), (-1, 1, -1), (1, 1, -1), (1, 1, 1),
                            (1, -1, 1), (1, -1, -1))]
                corners = bm.world(corners).tolist()
                self.hook.js(f"window.Q2T4 && Q2T4.showQueryResult({json.dumps(html)}, {json.dumps(corners)}, {json.dumps(res['point'])})")
            except (IndexError, KeyError, TypeError) as e:
                log(f"Block query failed: {e}")
            return
        if hit:
            if hit.get("mesh") is not None and hit["mesh"] in self.hook.meshes:
                m = self.hook.meshes[hit["mesh"]]
                st = m.stats()
                hit["name"] = m.name
                info = {"Triangles": st["triangles"], "Closed": "yes" if st.get("closed") else "no"}
                if st.get("volume") is not None:
                    info["Volume m³"] = round(st["volume"], 2)
                info["Source"] = m.source
                hit["info"] = info
            elif hit.get("jsLayerId") is not None and self.hook._window is not None:
                for lyr in self.hook._window.settings.layers():
                    if str(lyr.jsLayerId) == str(hit["jsLayerId"]):
                        hit["name"] = lyr.name
                        if lyr.mapLayer is not None:
                            hit["qgisLayerId"] = lyr.mapLayer.id()
                        break
        try:
            res = Q.query(ev, self._queryCandidates(), self.hook.sectionSpec,
                          self.canvas.mapSettings().destinationCrs())
            htmlStr = self._showQuery(res)
        except Exception as e:
            res, htmlStr = None, f"<b>Query failed</b><div>{e}</div>"
            log(f"Query failed: {e}")
        hl = res.get("highlight") if res else None
        pt = res.get("point") if res else None
        self.hook.js(f"window.Q2T4 && Q2T4.showQueryResult({json.dumps(htmlStr)}, {json.dumps(hl)}, {json.dumps(pt)})")

    # ---------------------------------------------------- query: inspector

    @staticmethod
    def _shown(k, v):
        return not (v is None or (hasattr(v, "isNull") and v.isNull()) or v == "" or
                    str(k).startswith("q2t4_") or k == "fid")

    @staticmethod
    def _fmt(v):
        if isinstance(v, float):
            return f"{v:,.2f}" if abs(v) >= 1e5 else (f"{v:.4f}".rstrip("0").rstrip(".") or "0")
        return str(v)

    def _grade(self, rows):
        """(field, value) of the headline grade: the reported estimate of the first element
        (Au_OK, AUppm, Cu_pct, Li2O, CuEq ...), else None."""
        from ..core.fieldnames import grade_fields
        d = {str(k): v for k, v in rows if self._shown(k, v) and isinstance(v, (int, float))}
        up = {k.upper() for k in d}
        coords = {k for k in d if k.upper() in COORD_FIELDS}
        if not ({"X", "Z"} & up or {"EAST", "NORTH", "RL"} & up):
            coords -= {k for k in d if k.upper() == "Y"}        # a lone Y can be yttrium
        g = grade_fields([k for k in d if k not in coords])
        return (g[0], d[g[0]]) if g else None

    def _showQuery(self, res):
        """Fill Inspector ▸ Query with every field and return the compact HTML card for the 3D view."""
        import html as H
        self._lastQuery = res
        self.tblQ.setRowCount(0)
        if res is None:
            self.lblQTitle.setText("<b>Nothing here</b> - no object within the section under the cursor.")
            self.bQSel.setEnabled(False)
            self.bQZoom.setEnabled(False)
            return "<b>Nothing here</b><div class='q2t4-q-note'>No object within the section under the cursor.</div>"
        p = res["point"]
        self.lblQTitle.setText(f"<b>{H.escape(str(res['title']))}</b><br>"
                               f"<span style='font-family:Consolas,monospace'>E {p[0]:,.2f} &nbsp;N {p[1]:,.2f} "
                               f"&nbsp;RL {p[2]:,.2f}</span>")
        rows = [(k, v) for k, v in res["rows"] if self._shown(k, v)]
        self.tblQ.setRowCount(len(rows))
        for r, (k, v) in enumerate(rows):
            self.tblQ.setItem(r, 0, QTableWidgetItem(str(k)))
            it = QTableWidgetItem(self._fmt(v))
            if isinstance(v, (int, float)):
                it.setTextAlignment(int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter))
            self.tblQ.setItem(r, 1, it)
        self.tblQ.resizeColumnToContents(0)
        self.bQSel.setEnabled(bool(res.get("layer_id")) and res.get("fid") is not None)
        self.bQZoom.setEnabled(True)

        card = [f"<div class='q2t4-q-title'>{H.escape(str(res['title']))}</div>",
                f"<div class='q2t4-q-xyz'>E {p[0]:,.1f} &nbsp; N {p[1]:,.1f} &nbsp; RL {p[2]:,.1f}</div>"]
        g = self._grade(rows)                     # the grade leads the short list (no big headline)
        skip = COORD_FIELDS
        others = ([g] if g else []) + \
            [(k, v) for k, v in rows if (not g or k != g[0]) and str(k).upper() not in skip][:4]
        if others:
            card.append("<table>" + "".join(f"<tr><td>{H.escape(str(k))}</td><td>{H.escape(self._fmt(v))}</td></tr>"
                                            for k, v in others) + "</table>")
        card.append(f"<div class='q2t4-q-note'>{len(rows)} fields · Inspector ▸ Query</div>")
        if self.tabs.currentIndex() != 4 and self.isVisible():
            self.tabs.setCurrentIndex(4)
        return "".join(card)

    def _toggleQuery(self, on):
        self.hook.setQueryMode(on)
        vt = self._vtb()
        if vt is not None:
            vt._mirror(vt.aQuery, on)

    def _querySelect(self):
        q = self._lastQuery
        lyr = QgsProject.instance().mapLayer(q.get("layer_id")) if q else None
        if lyr is None:
            return
        lyr.selectByIds([int(q["fid"])])
        self.iface.setActiveLayer(lyr)
        self.plugin._msg(f"Selected feature {q['fid']} in {lyr.name()}")

    def _queryZoom(self):
        q = self._lastQuery
        if q:
            x, y, z = q["point"]
            self.hook.js(f"window.Q2T4 && Q2T4._setOrbit(Q2T4.mapToWorld({x}, {y}, {z}))")

    # --------------------------------------------------- saved sections
    def _fillSecList(self):
        self.lstSec.clear()
        for r in load_sections(self.store.path) if self.store.path else []:
            it = QListWidgetItem(f"{r['name']}  ·  {r['width']:g} m")
            it.setData(Qt.ItemDataRole.UserRole, r)
            self.lstSec.addItem(it)

    def saveSection(self):
        s = self.currentSection()
        if s is None:
            self.plugin._msg("Draw a section first.", Qgis.MessageLevel.Warning)
            return
        name, ok = QInputDialog.getText(self, "Save section", "Name:",
                                        text=f"Az {s.azimuth:.0f} {self.offset:+.0f} m")
        if not ok or not name.strip():
            return
        try:
            self.ensureStore()
            save_section(self.store.path, s, name.strip())
        except Exception as e:
            self.plugin._msg(f"Could not save section: {e}", Qgis.MessageLevel.Critical, 10)
            return
        self._fillSecList()

    def loadSavedSection(self):
        it = self.lstSec.currentItem()
        if it is None:
            return
        r = it.data(Qt.ItemDataRole.UserRole)
        self.spWidth.blockSignals(True)
        self.spWidth.setValue(r["width"])
        self.spWidth.blockSignals(False)
        self.setSection(r["a"], r["b"])

    def deleteSavedSection(self):
        it = self.lstSec.currentItem()
        if it is None:
            return
        try:
            delete_section(self.store.path, it.data(Qt.ItemDataRole.UserRole)["fid"])
        except Exception as e:
            self.plugin._msg(f"Could not delete section: {e}", Qgis.MessageLevel.Critical, 10)
        self._fillSecList()

    # ------------------------------------------------------------- HUD
    def _vtb(self):
        return getattr(self.plugin, "viewerToolbar", None)

    def _updateHud(self, *_):
        s = self.currentSection()
        chip = None if s is None else (f"Section {s.azimuth:.1f}° · looking {s.view_azimuth:.1f}° · "
                                       f"{s.width:g} m · {self.offset:+.1f} m")
        items = []
        for mid, m in self.hook.meshes.items():
            if mid not in self.hook.hiddenMeshes and len(m.vertices):
                mn, mx = m.vertices.min(axis=0), m.vertices.max(axis=0)
                items.append({"bbox": [float(mn[0]), float(mn[1]), float(mx[0]), float(mx[1])], "color": "#E2A72E"})
        for bid, bm in self.hook.blockModels.items():
            bb = bm.world_bbox() if bid not in self.hook.hiddenMeshes else None
            if bb:
                items.append({"bbox": bb, "color": "#5FB36B"})
        if self.drillLayerId and "drill" not in self.hook.hiddenOverlays:
            lyr = QgsProject.instance().mapLayer(self.drillLayerId)
            if lyr is not None:
                e = lyr.extent()
                if not e.isEmpty():
                    items.append({"bbox": [e.xMinimum(), e.yMinimum(), e.xMaximum(), e.yMaximum()], "color": "#D24BFF"})
        inset = None
        if items or s is not None:
            inset = {"items": items}
            if s is not None:
                inset.update(slab=[list(p) for p in s.slab_polygon()], line=[list(s.a), list(s.b)], dir=list(s.n))
        from ..core.crs import short_name
        prj = self.projectCrs()
        u = self._mapUnit(prj)
        sec_txt = None
        if s is not None:
            cx, cy = s.centre
            az = s.azimuth % 180.0
            if min(abs(az - 0), abs(az - 180)) < 0.05:          # N-S line: an easting section
                sec_txt = f"Section {cx:,.1f} {u}E"
            elif abs(az - 90) < 0.05:                            # E-W line: a northing section
                sec_txt = f"Section {cy:,.1f} {u}N"
            else:
                sec_txt = f"Section az {s.azimuth:.1f}° · centre {cx:,.1f} {u}E  {cy:,.1f} {u}N"
            if abs(self.offset) > 1e-9:
                sec_txt += f"  ({self.offset:+.1f} {u})"
        self.hook.setHud(chip=chip, inset=inset,
                         scale={"crs": short_name(prj), "crsFull": f"{prj.authid()} {prj.description()}",
                                "section": sec_txt, "unit": self._mapUnit(prj)})
        vt = self._vtb()
        if vt is not None:
            try:
                vt.syncSection()
            except RuntimeError:
                pass

    @staticmethod
    def _mapUnit(crs):
        """'ft' for foot-based CRSs, else 'm' (labels: mE / mN / mRL or ftE / ftN / ftRL)."""
        try:
            from qgis.core import QgsUnitTypes
            name = QgsUnitTypes.toAbbreviatedString(crs.mapUnits()).lower()
        except Exception:
            name = ""
        return "ft" if "ft" in name or "feet" in name or "foot" in name else "m"

    # ------------------------------------------------------------ Datamine
    def openDatamineDialog(self):
        from qgis.PyQt.QtCore import QSettings
        start = QSettings().value("/QGeo3D/lastDmDir", "")
        path, _ = QFileDialog.getOpenFileName(self, "Open Datamine file", start, "Datamine (*.dm *.DM)")
        if path:
            QSettings().setValue("/QGeo3D/lastDmDir", os.path.dirname(path))
            self.openDatamine(path)

    def openDatamine(self, path):
        """Route a .dm file by its fields: wireframe (pt/tr), block model or drillholes."""
        from ..core import dmfile
        try:
            k = dmfile.kind(path)
        except Exception as e:
            self.plugin._msg(f"Could not read {os.path.basename(path)}: {e}", Qgis.MessageLevel.Critical, 10)
            return None
        if k == "wireframe":
            try:
                return self.plugin.addFile(path)
            except Exception as e:
                self.plugin._msg(f"Could not load wireframe: {e}", Qgis.MessageLevel.Critical, 10)
                return None
        if k == "blockmodel":
            return self.loadBlockModel(path)
        if k == "drillholes":
            return self.loadDmDrillholes(path)
        self.plugin._msg(f"{os.path.basename(path)} is a Datamine {k} file; only wireframes, block models "
                         "and desurveyed drillholes are loaded so far.", Qgis.MessageLevel.Warning, 10)
        return None

    def loadDmDrillholes(self, path, crs=None):
        """Desurveyed Datamine drillholes -> work/<name>_drillholes.gpkg (in their own CRS),
        QGIS group, 3D snap overlay (reprojected to the project CRS by QGIS)."""
        from ..core.dmfile import read_drillholes, write_drillholes_gpkg
        import numpy as np
        name = os.path.splitext(os.path.basename(path))[0]
        od = self._importDir(path)
        if od is None:
            return None
        out = os.path.join(od, f"{name}_drillholes.gpkg")
        if crs is None:
            from .crsdialog import ask_source_crs
            try:
                rows, _dm = read_drillholes(path)
                pts = np.array([r[3] for r in rows] + [r[4] for r in rows]) if rows else None
            except Exception:
                pts = None
            crs = ask_source_crs(self, path, pts)
            if crs is None:
                return None
        out = self._freshGpkg(out)
        try:
            n = write_drillholes_gpkg(path, out, crs)
        except Exception as e:
            self.plugin._msg(f"Could not load drillholes: {e}", Qgis.MessageLevel.Critical, 10)
            return None
        prj = QgsProject.instance()
        root = prj.layerTreeRoot()
        gname = f"{name} drillholes (Datamine)"
        old = root.findGroup(gname)
        if old is not None:
            root.removeChildNode(old)
        grp = root.insertGroup(0, gname)
        lyr = open_gpkg_layer(out, "intervals", f"{name} intervals")
        self._noteGpkg("drill", out)
        if lyr.isValid():
            prj.addMapLayer(lyr, False)
            grp.addLayer(lyr)
            self.iface.setActiveLayer(lyr)
            self.useDrillLayer()
        self.registerImport("drill", "overlay", path, crs, reload=lambda c, p=path: self.loadDmDrillholes(p, c))
        self.plugin._msg(f"{n} intervals from {os.path.basename(path)} written to {out}", Qgis.MessageLevel.Success, 8)
        return n

    # -------------------------------------------------------------- Surpac
    def openSurpacDialog(self):
        from qgis.PyQt.QtCore import QSettings
        start = QSettings().value("/QGeo3D/lastSurpacDir", "")
        path, _ = QFileDialog.getOpenFileName(self, "Open Surpac file", start,
                                              "Surpac (*.str *.STR *.dtm *.DTM *.mdl *.MDL);;Strings (*.str *.STR);;"
                                              "DTM / solids (*.dtm *.DTM)")
        if path:
            QSettings().setValue("/QGeo3D/lastSurpacDir", os.path.dirname(path))
            self.openSurpac(path)

    def openSurpac(self, path):
        ext = os.path.splitext(path)[1].lower()
        if ext == ".dtm":
            try:
                return self.plugin.addFile(path)
            except Exception as e:
                self.plugin._msg(f"Could not load DTM: {e}", Qgis.MessageLevel.Critical, 10)
                return None
        if ext == ".str":
            return self.loadSurpacStrings(path)
        if ext == ".mdl":
            return self.loadBlockModel(path)
        self.plugin._msg(f"Not a Surpac file: {os.path.basename(path)}", Qgis.MessageLevel.Warning)
        return None

    def loadSurpacStrings(self, path, crs=None):
        """Surpac strings -> work/<name>_surpac.gpkg (in their own CRS), a QGIS layer and a 3D overlay
        (reprojected to the project CRS; vertices snap)."""
        from ..core.surpac import read_str, write_strings_gpkg
        from ..core.strings import polyline_payload
        from ..core.crs import transform_xyz
        name = os.path.splitext(os.path.basename(path))[0]
        od = self._importDir(path)
        if od is None:
            return None
        out = os.path.join(od, f"{name}_surpac.gpkg")
        if crs is None:
            from .crsdialog import ask_source_crs
            try:
                rec = read_str(path)["records"]
            except Exception:
                rec = None
            crs = ask_source_crs(self, path, rec)
            if crs is None:
                return None
        out = self._freshGpkg(out)
        try:
            n, polys = write_strings_gpkg(path, out, crs)
            polys = [transform_xyz(pl, crs, self.projectCrs()) for pl in polys]
        except Exception as e:
            self.plugin._msg(f"Could not load strings: {e}", Qgis.MessageLevel.Critical, 10)
            return None
        prj = QgsProject.instance()
        root = prj.layerTreeRoot()
        gname = "Surpac strings"
        grp = root.findGroup(gname) or root.insertGroup(0, gname)
        for node in list(grp.findLayers()):
            if node.layer() is not None and node.layer().source().startswith(out):
                grp.removeChildNode(node)
        lyr = open_gpkg_layer(out, "strings", name)
        self._noteGpkg(f"surpac:{name}", out)
        if lyr.isValid():
            prj.addMapLayer(lyr, False)
            grp.addLayer(lyr)
        self.hook.setOverlay(f"surpac:{name}", polyline_payload(polys, "#00b4ff", snap="all", point_size=0))
        self.registerImport(f"surpac:{name}", "overlay", path, crs,
                            reload=lambda c, p=path: self.loadSurpacStrings(p, c))
        self.plugin._msg(f"{n} string segment(s) from {os.path.basename(path)}", Qgis.MessageLevel.Success, 6)
        return n

    # ------------------------------------------------------------ Micromine
    MM_FILTER = ("Micromine (*.tridb *.TRIDB *.STR *.str *.DAT *.dat *.DHDB *.dhdb);;Wireframes (*.tridb *.TRIDB);;"
                 "Strings (*.STR *.str);;Data files: points / strings / block model (*.DAT *.dat);;"
                 "Drillhole database (*.DHDB *.dhdb)")

    def openMicromineDialog(self):
        from qgis.PyQt.QtCore import QSettings
        start = QSettings().value("/QGeo3D/lastMmDir", "")
        path, _ = QFileDialog.getOpenFileName(self, "Open Micromine file", start, self.MM_FILTER)
        if path:
            QSettings().setValue("/QGeo3D/lastMmDir", os.path.dirname(path))
            self.openMicromine(path)

    def openMicromine(self, path):
        """Route a Micromine file: .tridb wireframes, .DHDB drillholes, .STR/.DAT by their fields
        (block model, strings, points; downhole interval files need their .DHDB)."""
        from ..core import micromine as mm
        ext = os.path.splitext(path)[1].lower()
        try:
            if ext == ".tridb":
                return self.loadTridb(path)
            if ext == ".dhdb":
                return self.loadDhdb(path)
            k = mm.dat_kind(path)
        except Exception as e:
            self.plugin._msg(f"Could not read {os.path.basename(path)}: {e}", Qgis.MessageLevel.Critical, 10)
            return None
        if k == "blockmodel":
            return self.loadBlockModel(path)
        if k == "strings":
            return self.loadMmStrings(path)
        if k == "points":
            return self.loadMmPoints(path)
        if k == "intervals":
            self.plugin._msg(f"{os.path.basename(path)} is a downhole interval file (FROM / TO, no coordinates): "
                             "open the drillhole database (.DHDB) that uses it.", Qgis.MessageLevel.Warning, 10)
            return None
        self.plugin._msg(f"{os.path.basename(path)} has no east / north fields, so there is nothing to place in 3D.",
                         Qgis.MessageLevel.Warning, 10)
        return None

    def loadTridb(self, path, crs=None, names=None):
        """Wireframes from a Micromine .tridb (ticked in a list when the file holds more than one).
        Each becomes a mesh in the Micromine colour; CRS asked once for the file."""
        from qgis.PyQt.QtWidgets import QApplication, QMessageBox
        from ..core import micromine as mm
        from ..core.crs import transform_xyz
        from .pickdialog import pick_items
        items = mm.list_tridb(path)
        if not items:
            self.plugin._msg(f"{os.path.basename(path)} holds no wireframes.", Qgis.MessageLevel.Warning, 8)
            return None
        if names is None:
            if len(items) == 1:
                names = [items[0]["name"]]
            else:
                rows = []
                for w in items:
                    tri = f"{w['triangles']:,} tri" if w["triangles"] is not None else "?"
                    extra = " · ".join(x for x in (w["code"], w["title"]) if x)
                    rows.append((f"{w['name']}  ·  {tri}" + (f"  ·  {extra}" if extra else ""),
                                 (w["triangles"] or 0) <= 500000,
                                 f"{w['points'] or '?'} points; extent E {w['extent'][0]}–{w['extent'][1]}, "
                                 f"N {w['extent'][2]}–{w['extent'][3]}, RL {w['extent'][4]}–{w['extent'][5]}"))
                sel = pick_items(self, "Open wireframes",
                                 f"<b>{os.path.basename(path)}</b> holds {len(items)} wireframes. Tick the ones to open "
                                 "(large surfaces are unticked: over 500,000 triangles is slow in the 3D view).", rows)
                if not sel:
                    return None
                names = [items[i]["name"] for i in sel]
        big = sum((w["triangles"] or 0) for w in items if w["name"] in names)
        if big > 3000000 and QMessageBox.question(
                self, "Large wireframes", f"{big:,} triangles in total. The 3D view may become slow or run out of "
                "memory. Open anyway?") != QMessageBox.StandardButton.Yes:
            return None
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            meshes = mm.read_tridb(path, names)
        except Exception as e:
            self.plugin._msg(f"Could not read {os.path.basename(path)}: {e}", Qgis.MessageLevel.Critical, 10)
            return None
        finally:
            QApplication.restoreOverrideCursor()
        if not meshes:
            return None
        if crs is None:
            from .crsdialog import ask_source_crs
            import numpy as np
            crs = ask_source_crs(self, path, np.vstack([m.vertices for m, _w in meshes]))
            if crs is None:
                return None
        out = []
        prj = self.projectCrs()
        for m, w in meshes:
            m.src_vertices = m.vertices.copy()
            m.src_crs = crs
            m.vertices = transform_xyz(m.src_vertices, crs, prj)
            style = {"color": m.color} if m.color else {}
            res = self.plugin.addMesh(m, **style)
            if res:
                self.registerImport(res[0], "mesh", path, crs)
                out.append(res)
        return out

    def _mmGroup(self, gname, out):
        prj = QgsProject.instance()
        root = prj.layerTreeRoot()
        grp = root.findGroup(gname) or root.insertGroup(0, gname)
        for node in list(grp.findLayers()):
            if node.layer() is not None and node.layer().source().startswith(out):
                grp.removeChildNode(node)
        return prj, grp

    def loadMmStrings(self, path, crs=None):
        """Micromine strings (.STR or a .DAT with JOIN) -> work/<name>_mmstr.gpkg, QGIS layer, 3D overlay."""
        from ..core import micromine as mm
        from ..core.strings import mixed_payload
        from ..core.crs import transform_xyz
        name = os.path.splitext(os.path.basename(path))[0]
        od = self._importDir(path)
        if od is None:
            return None
        if crs is None:
            from .crsdialog import ask_source_crs
            try:
                xyz = mm.read_points(path)[0]
            except Exception:
                xyz = None
            crs = ask_source_crs(self, path, xyz)
            if crs is None:
                return None
        out = self._freshGpkg(os.path.join(od, f"{name}_mmstr.gpkg"))
        try:
            n, polys = mm.write_strings_gpkg(path, out, crs)
            single = [st["pts"] for st in mm.read_strings(path)["strings"] if len(st["pts"]) == 1]
            prj_polys = [transform_xyz(p, crs, self.projectCrs()) for p in polys + single]
        except Exception as e:
            self.plugin._msg(f"Could not load strings: {e}", Qgis.MessageLevel.Critical, 10)
            return None
        prj, grp = self._mmGroup("Micromine strings", out)
        lyr = open_gpkg_layer(out, "strings", name)
        self._noteGpkg(f"mm:{name}", out)
        if lyr.isValid():
            prj.addMapLayer(lyr, False)
            grp.addLayer(lyr)
        key = f"mm:{name}"
        self.hook.setOverlay(key, mixed_payload(prj_polys, "#ffb000", point_size=5))
        self.registerImport(key, "overlay", path, crs, reload=lambda c, p=path: self.loadMmStrings(p, c))
        extra = f" and {len(single)} single point(s)" if single else ""
        self.plugin._msg(f"{n} string(s){extra} from {os.path.basename(path)}", Qgis.MessageLevel.Success, 6)
        return n

    def loadMmPoints(self, path, crs=None):
        """Micromine points (.DAT / .STR with east, north[, RL]) -> work/<name>_mmpts.gpkg (PointZ, all
        fields), QGIS layer, 3D point overlay (vertices snap)."""
        from ..core import micromine as mm
        from ..core.strings import mixed_payload
        from ..core.crs import transform_xyz
        name = os.path.splitext(os.path.basename(path))[0]
        od = self._importDir(path)
        if od is None:
            return None
        if crs is None:
            from .crsdialog import ask_source_crs
            try:
                xyz = mm.read_points(path)[0]
            except Exception as e:
                self.plugin._msg(f"Could not read {os.path.basename(path)}: {e}", Qgis.MessageLevel.Critical, 10)
                return None
            crs = ask_source_crs(self, path, xyz)
            if crs is None:
                return None
        out = self._freshGpkg(os.path.join(od, f"{name}_mmpts.gpkg"))
        try:
            n, xyz = mm.write_points_gpkg(path, out, crs)
            p = transform_xyz(xyz, crs, self.projectCrs())
        except Exception as e:
            self.plugin._msg(f"Could not load points: {e}", Qgis.MessageLevel.Critical, 10)
            return None
        prj, grp = self._mmGroup("Micromine points", out)
        lyr = open_gpkg_layer(out, "points", name)
        self._noteGpkg(f"mmpts:{name}", out)
        if lyr.isValid():
            prj.addMapLayer(lyr, False)
            grp.addLayer(lyr)
        key = f"mmpts:{name}"
        self.hook.setOverlay(key, mixed_payload([q[None, :] for q in p], "#ff5ad2", point_size=4))
        self.registerImport(key, "overlay", path, crs, reload=lambda c, p=path: self.loadMmPoints(p, c))
        self.plugin._msg(f"{n:,} point(s) from {os.path.basename(path)}" +
                         (" (3D view shows 300,000 of them)" if n > 300000 else ""), Qgis.MessageLevel.Success, 6)
        return n

    # ------------------------------------------------------- drag and drop
    def openDropped(self, paths):
        """Open dropped files by type. Several CSV / Excel files dropped together go into one drillhole
        dialog (collar / survey / interval tables assigned from their names)."""
        from .dropper import ext, is_micromine, TABLES
        from qgis.PyQt.QtGui import QCursor
        from qgis.PyQt.QtWidgets import QMenu
        tables = [p for p in paths if ext(p) in TABLES]
        for p in paths:
            e = ext(p)
            if e in TABLES:
                continue
            try:
                if e == ".tridb":
                    self.loadTridb(p)
                elif e == ".dm":
                    self.openDatamine(p)
                elif e in (".bmf", ".mdl"):
                    self.loadBlockModel(p)
                elif e == ".dhdb":
                    self.loadDhdb(p)
                elif e in (".str", ".dat"):
                    if is_micromine(p):
                        self.openMicromine(p)
                    elif e == ".str":
                        self.loadSurpacStrings(p)
                    else:
                        self.plugin._msg(f"{os.path.basename(p)} is not a Micromine data file.", Qgis.MessageLevel.Warning, 8)
                elif e == ".omf":
                    self.openOmf(p)
                elif e in (".gpkg", ".shp"):
                    from ..core.layerblocks import face_layers, point_layers
                    pts, faces = point_layers(p), face_layers(p)
                    if faces or not pts:
                        self.plugin.addFile(p)
                    if len(pts) == 1:
                        self.loadBlockModelFile(p, pts[0][0])
                    elif pts:
                        self.loadBlockModelFile(p)
                elif e in (".dtm", ".dxf", ".obj", ".msh"):
                    self.plugin.addFile(p)
            except Exception as ex:
                self.plugin._msg(f"Could not open {os.path.basename(p)}: {ex}", Qgis.MessageLevel.Critical, 10)
        if tables:
            m = QMenu(self)
            names = ", ".join(os.path.basename(t) for t in tables[:3]) + (" …" if len(tables) > 3 else "")
            m.addSection(names)
            aD = m.addAction("Drillholes (collars, surveys, intervals)…")
            aB = m.addAction("Block model…") if len(tables) == 1 else None
            a = m.exec(QCursor.pos())
            if a is aD:
                self.tableDrillholesDialog(tables)
            elif a is not None and a is aB:
                self.tableBlocksDialog(tables[0])

    # -------------------------------------------------------------- export
    def exportDialog(self, preselect=None):
        """Export a 3D object or a vector layer (GeoPackage etc.) to a mining-package format."""
        from .exportdialog import ExportDialog, export_sources
        from qgis.PyQt.QtWidgets import QDialog
        srcs = export_sources(self)
        if not srcs:
            self.plugin._msg("Nothing to export: load a wireframe / block model or add a point, line or polygon layer.",
                             Qgis.MessageLevel.Info, 8)
            return None
        if preselect is None:
            its = self.lstWf.selectedItems()
            if its:
                kind, key = its[0].data(Qt.ItemDataRole.UserRole)
                preselect = ("blocks", key) if key in self.hook.blockModels else ("mesh", key) if kind == "mesh" else \
                    ("layer", key) if kind == "q2t" else None
            if preselect is None and self.iface.activeLayer() is not None:
                preselect = ("layer", self.iface.activeLayer().id())
        pf = QgsProject.instance().fileName()
        base = _subdir(os.path.dirname(pf)) if pf else work_dir()
        exp = os.path.join(work_dir() if is_protected(base) else base, "export")   # starting folder only
        os.makedirs(exp, exist_ok=True)
        dlg = ExportDialog(self, srcs, preselect, exp)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return None
        return self.runExport(dlg.config())

    def runExport(self, cfg):
        import json
        from qgis.PyQt.QtWidgets import QApplication
        from ..core import writers as W
        from ..core import exporting as X
        kind, (stype, sid), crs = cfg["kind"], cfg["source"], cfg["crs"]
        note = None
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            if stype == "mesh":
                m = self.hook.meshes[sid]
                col = json.loads(self.hook.payloads[sid]).get("color") if sid in self.hook.payloads else None
                data, fields = X.mesh_in(m, crs, col), []
            elif stype == "blocks":
                bmx = self.hook.blockModels[sid]
                if cfg["fmt"] in ("lf_csv", "omf", "dm_bm"):         # these hold a rotation: world centroids + azimuth / axes
                    data, fields, note = X.blocks_rotated_in(bmx, crs)
                else:
                    data, fields, note = X.blocks_in(bmx, crs)
            else:
                lyr = QgsProject.instance().mapLayer(sid)
                fn = {"strings": X.layer_strings, "points": X.layer_points, "blocks": X.layer_blocks,
                      "wireframe": X.layer_wireframe}[kind]
                data, fields = fn(lyr, crs, cfg.get("selected", False))
                if kind == "wireframe" and not len(data.triangles):
                    raise ValueError("the layer has no polygon / TIN faces")
            name = cfg["label"].split(":", 1)[-1].split("·")[0].strip()
            msg = W.export(kind, cfg["fmt"], cfg["path"], data, fields, name=name)
        except Exception as e:
            self.plugin._msg(f"Export failed: {e}", Qgis.MessageLevel.Critical, 12)
            return None
        finally:
            QApplication.restoreOverrideCursor()
        self.plugin._msg(f"Exported ({crs.authid()}): {msg}" + (f" - note: {note}" if note else ""),
                         Qgis.MessageLevel.Warning if note else Qgis.MessageLevel.Success, 12)
        return msg

    # ------------------------------------------------------ CSV / Excel
    def tableDrillholesDialog(self, path=None):
        from .tabledialog import DrillholeTableDialog
        from qgis.PyQt.QtWidgets import QDialog
        dlg = DrillholeTableDialog(self, path)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            return self.loadTableDrillholes(dlg.config())
        return None

    def tableBlocksDialog(self, path=None):
        from .tabledialog import BlockTableDialog
        from qgis.PyQt.QtWidgets import QDialog
        dlg = BlockTableDialog(self, path)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            return self.loadTableBlocks(dlg.config())
        return None

    @staticmethod
    def _readSpec(spec):
        from ..core import tables as T
        fields, rows, _h = T.read_table(spec["path"], spec.get("sheet"), spec.get("header"))
        return dict(spec, fields=fields, rows=rows)

    @gdal_soft
    def loadTableDrillholes(self, cfg, crs=None):
        """Collars (+ surveys, + interval tables) from CSV / Excel -> desurveyed GeoPackage in work/,
        QGIS group, 3D snap overlay of the first interval table (or the traces)."""
        from qgis.PyQt.QtWidgets import QApplication
        from ..core.drillholes import build_from_tables
        from osgeo import ogr
        import numpy as np
        try:
            collar = self._readSpec(cfg["collar"])
            survey = self._readSpec(cfg["survey"]) if cfg.get("survey") else None
            intervals = [self._readSpec(sp) for sp in cfg.get("intervals", [])]
        except Exception as e:
            self.plugin._msg(f"Could not read the tables: {e}", Qgis.MessageLevel.Critical, 10)
            return None
        src_path = cfg["collar"]["path"]
        name = os.path.splitext(os.path.basename(src_path))[0]
        if cfg["collar"].get("sheet") and len(intervals) == 0 and survey is None:
            name += f" {cfg['collar']['sheet']}"
        if crs is None:
            from .crsdialog import ask_source_crs
            c = cfg["collar"]
            pts = np.array([[r.get(c["east"]), r.get(c["north"]), r.get(c["rl"]) if c.get("rl") else 0]
                            for r in collar["rows"] if r.get(c["east"]) is not None and r.get(c["north"]) is not None],
                           float) if collar["rows"] else None
            crs = ask_source_crs(self, src_path, pts)
            if crs is None:
                return None
        od = self._importDir(src_path)
        if od is None:
            return None
        out = self._freshGpkg(os.path.join(od, f"{name}_drillholes.gpkg"))
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            rep = build_from_tables(collar, survey, intervals, out, crs, src_path,
                                    dip_positive_down=cfg.get("dip_positive_down", False))
        except Exception as e:
            self.plugin._msg(f"Could not build drillholes: {e}", Qgis.MessageLevel.Critical, 10)
            return None
        finally:
            QApplication.restoreOverrideCursor()
        prj = QgsProject.instance()
        root = prj.layerTreeRoot()
        gname = f"{name} drillholes (table)"
        old = root.findGroup(gname)
        if old is not None:
            root.removeChildNode(old)
        grp = root.insertGroup(0, gname)
        ds = ogr.Open(out)
        snap = None
        for i in range(ds.GetLayerCount()):
            ln = ds.GetLayerByIndex(i).GetName()
            if ln == "q2t4_source":
                continue
            lyr = open_gpkg_layer(out, ln, ln)
            self._noteGpkg("drill", out)
            if lyr.isValid():
                prj.addMapLayer(lyr, False)
                grp.addLayer(lyr)
                if ln != "collars" and (snap is None or snap.name() == "traces"):
                    snap = lyr
        ds = None
        if snap is not None:
            self.iface.setActiveLayer(snap)
            self.useDrillLayer()
            self.registerImport("drill", "overlay", src_path, crs,
                                reload=lambda c, cfg=cfg: self.loadTableDrillholes(cfg, c))
        ints = ", ".join(f"{k} {a:,}/{b:,}" for k, (a, b) in rep["intervals"].items())
        msg = f"{rep['holes']} holes" + (f"; intervals placed: {ints}" if ints else "")
        if rep["no_survey"]:
            msg += f"; {len(rep['no_survey'])} hole(s) without survey or collar azimuth/dip (collar only)"
        self.plugin._msg(msg + f" -> {out}", Qgis.MessageLevel.Success, 10)
        return rep

    def loadTableBlocks(self, cfg, crs=None):
        """Block model from CSV / Excel (centres + sizes or fixed sizes) -> same path as other models."""
        from ..core.blockmodel import BlockModel
        from qgis.PyQt.QtWidgets import QApplication
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            t = self._readSpec(cfg)
            bm = BlockModel.from_table(t["fields"], t["rows"], cfg["cols"], cfg["name"], cfg["path"])
            hdr = cfg.get("header") or {}
            if hdr.get("azimuth"):                  # Leapfrog-style rotated model: world centroids -> model
                from ..core.leapfrog import place
                pl = place(bm.xyz, bm.size[0] if len(bm.size) else [1, 1, 1], hdr)
                if pl is not None:
                    bm.xyz, R, o, note = pl
                    bm.rot, bm.origin, bm.rotated = R, o, True
                    bm.notes.append(note)
        except Exception as e:
            self.plugin._msg(f"Could not read block model table: {e}", Qgis.MessageLevel.Critical, 10)
            return None
        finally:
            QApplication.restoreOverrideCursor()
        return self._addBlockModel(bm, cfg["path"], crs=crs)

    # ------------------------------------------------------- drillholes DB
    def loadDhdbDialog(self):
        from qgis.PyQt.QtCore import QSettings
        start = QSettings().value("/QGeo3D/lastDhdbDir", "")
        path, _ = QFileDialog.getOpenFileName(self, "Micromine drillhole database", start,
                                              "Micromine drillhole database (*.DHDB *.dhdb)")
        if path:
            QSettings().setValue("/QGeo3D/lastDhdbDir", os.path.dirname(path))
            self.loadDhdb(path)

    @gdal_soft
    def loadDhdb(self, path, crs=None):
        """Desurvey (minimum curvature) into work/<name>_desurveyed.gpkg (in the database's CRS)
        and add a QGIS group."""
        from qgis.PyQt.QtWidgets import QApplication
        from ..core.drillholes import build_gpkg
        from osgeo import ogr
        name = os.path.splitext(os.path.basename(path))[0]
        od = self._importDir(path)
        if od is None:
            return None
        out = self._freshGpkg(os.path.join(od, f"{name}_desurveyed.gpkg"))  # work/ is ours: rebuild from the (read-only) source
        if crs is None:
            from .crsdialog import ask_source_crs
            crs = ask_source_crs(self, path, None)
            if crs is None:
                return None
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            rep = build_gpkg(path, out, crs)
        except Exception as e:
            self.plugin._msg(f"Could not load drillhole database: {e}", Qgis.MessageLevel.Critical, 10)
            return None
        finally:
            QApplication.restoreOverrideCursor()
        prj = QgsProject.instance()
        root = prj.layerTreeRoot()
        gname = f"{name} drillholes (desurveyed)"
        old = root.findGroup(gname)
        if old is not None:
            root.removeChildNode(old)
        grp = root.insertGroup(0, gname)
        ds = ogr.Open(out)
        first_interval = traces_lyr = None
        for i in range(ds.GetLayerCount()):
            ln = ds.GetLayerByIndex(i).GetName()
            if ln == "q2t4_source":
                continue
            lyr = open_gpkg_layer(out, ln, ln)
            self._noteGpkg("drill", out)
            if lyr.isValid():
                prj.addMapLayer(lyr, False)
                grp.addLayer(lyr)
                if ln not in ("collars", "traces") and first_interval is None:
                    first_interval = lyr
                if ln == "traces":
                    traces_lyr = lyr
        ds = None
        if first_interval is None and traces_lyr is not None and traces_lyr.featureCount() > 0:
            first_interval = traces_lyr          # planning databases: no interval files, show the traces
        if first_interval is not None:
            self.iface.setActiveLayer(first_interval)
            self.useDrillLayer()
            self.registerImport("drill", "overlay", path, crs, reload=lambda c, p=path: self.loadDhdb(p, c))
        ns = len(rep["no_survey"])
        if ns:
            self.plugin._msg(f"{rep['holes']} holes into {out}; {ns} have no survey and no collar azimuth / dip, so "
                             f"they have a collar but no trace (e.g. {', '.join(map(str, rep['no_survey'][:5]))})",
                             Qgis.MessageLevel.Warning, 15)
        else:
            self.plugin._msg(f"{rep['holes']} holes desurveyed into {out}", Qgis.MessageLevel.Success, 8)
        return rep

    # ------------------------------------------- where import GeoPackages go
    def _importDir(self, src=None, quiet_ask=False):
        """Folder for an import's GeoPackage, from the 'Save GeoPackages to' setting. Falls back to the
        plugin work folder when the choice is impossible (unsaved project, ringfenced or read-only folder).
        Returns None if the user cancels the 'ask' dialog."""
        from qgis.PyQt.QtCore import QSettings
        from ..core.q2thook import is_protected
        mode = QSettings().value(OUT_KEY_MODE, OUT_DEFAULT) or OUT_DEFAULT
        cand, why = None, None
        if mode == "project":
            pf = QgsProject.instance().fileName()
            cand = _subdir(os.path.dirname(pf)) if pf else None
            why = None if pf else "the project hasn't been saved yet"
        elif mode == "source" and src:
            cand = _subdir(os.path.dirname(os.path.abspath(src)))
        elif mode == "custom":
            cand = QSettings().value(OUT_KEY_DIR, "") or None
            why = None if cand else "no folder chosen"
        elif mode == "ask":
            if quiet_ask and getattr(self, "_lastAskDir", None):
                cand = self._lastAskDir
            else:
                start = getattr(self, "_lastAskDir", None) or QSettings().value(OUT_KEY_DIR, "") or work_dir()
                name = os.path.basename(src) if src else "the import"
                cand = QFileDialog.getExistingDirectory(self, f"Save the GeoPackage for {name} in…", start)
                if not cand:
                    return None
                self._lastAskDir = cand
        if cand:
            plug = os.path.normcase(os.path.realpath(os.path.dirname(os.path.dirname(__file__))))
            if os.path.normcase(os.path.realpath(cand)).startswith(plug):
                why = f"{cand} is inside the plugin's own folder (it is replaced on every plugin update)"
                cand = None
            elif is_protected(cand):
                why = f"{cand} is ringfenced (protected data)"
                cand = None
            else:
                try:
                    os.makedirs(cand, exist_ok=True)
                    probe = os.path.join(cand, ".q2t4_write_test")
                    with open(probe, "w") as f:
                        f.write("")
                    os.remove(probe)
                except OSError as e:
                    why = f"can't write to {cand} ({e.strerror or e})"
                    cand = None
        if cand is None:
            cand = work_dir()
            os.makedirs(cand, exist_ok=True)
            if mode != "work" and why:
                self.plugin._msg(f"GeoPackage saved in the plugin work folder instead: {why}.", Qgis.MessageLevel.Info, 8)
        return cand

    def _outModeChanged(self, i=None):
        from qgis.PyQt.QtCore import QSettings
        mode = self.cbOut.currentData()
        if mode == "custom" and not QSettings().value(OUT_KEY_DIR, ""):
            self._chooseOutDir()
        QSettings().setValue(OUT_KEY_MODE, mode)
        self._outLabel()

    def _chooseOutDir(self):
        from qgis.PyQt.QtCore import QSettings
        d = QFileDialog.getExistingDirectory(self, "Save imported GeoPackages in…",
                                             QSettings().value(OUT_KEY_DIR, "") or work_dir())
        if d:
            from ..core.q2thook import is_protected
            if is_protected(d):
                self.plugin._msg("That folder is ringfenced (protected data) - choose another.", Qgis.MessageLevel.Warning, 8)
                return
            QSettings().setValue(OUT_KEY_DIR, d)
            i = self.cbOut.findData("custom")
            self.cbOut.blockSignals(True)
            self.cbOut.setCurrentIndex(i)
            self.cbOut.blockSignals(False)
            QSettings().setValue(OUT_KEY_MODE, "custom")
        self._outLabel()

    def _outLabel(self):
        from qgis.PyQt.QtCore import QSettings
        mode = self.cbOut.currentData()
        pf = QgsProject.instance().fileName()
        txt = {"work": work_dir(),
               "project": os.path.join(os.path.dirname(pf), SUBDIR) if pf else "(save the project first - "
                          "until then: plugin work folder)",
               "source": "<folder of each imported file>\\" + SUBDIR,
               "custom": QSettings().value(OUT_KEY_DIR, "") or "(choose a folder)",
               "ask": "asked for at each import"}[mode]
        self.cbOut.setToolTip(f"Where imports write their GeoPackages (drillholes, strings, points, block centres):\n{txt}\n"
                              "Ringfenced or read-only folders fall back to the plugin work folder.")
        self.lblOut.setText(txt if len(txt) < 70 else "…" + txt[-68:])

    # ------------------------------------------------- GeoPackages we write
    def _noteGpkg(self, key, path):
        """Remember the GeoPackage QGeo 3D wrote for a 3D item (so Remove can offer to delete it)."""
        if not hasattr(self, "gpkgOf"):
            self.gpkgOf = {}
        if key == "drill":
            self.gpkgOf["drill"] = {path}             # one drillhole set at a time
        else:
            self.gpkgOf.setdefault(key, set()).add(path)

    def _dropLayersUsing(self, path):
        """Remove project layers reading `path` (Windows keeps open GeoPackages locked, so they
        must go before the file is rewritten) and any group left empty."""
        prj = QgsProject.instance()
        key = os.path.normcase(os.path.abspath(path))
        groups = set()
        for lyr in list(prj.mapLayers().values()):
            src = lyr.source().split("|")[0]
            if os.path.normcase(os.path.abspath(src)) == key:
                node = prj.layerTreeRoot().findLayer(lyr.id())
                if node is not None and node.parent() is not None:
                    groups.add(node.parent())
                if lyr.id() == self.drillLayerId:
                    self.drillLayerId = None
                prj.removeMapLayer(lyr.id())
        for g in groups:
            if g is not prj.layerTreeRoot() and not g.children() and g.parent() is not None:
                g.parent().removeChildNode(g)
        from qgis.PyQt.QtWidgets import QApplication
        QApplication.processEvents()

    def _freshGpkg(self, out):
        """Path to (re)write a GeoPackage of ours: drops layers using it and deletes it; if QGIS still
        holds the file open (its connection pool does), uses <name>_2.gpkg, _3 ... instead."""
        base, ext = os.path.splitext(out)
        cands = [out] + [f"{base}_{i}{ext}" for i in range(2, 50)]
        for c in cands:
            self._dropLayersUsing(c)
        for c in cands:
            if not os.path.exists(c):
                return c
            try:
                os.remove(c)
                return c
            except OSError:
                continue
        raise OSError(f"No free file name next to {out}")

    # -------------------------------------------------- coordinate systems
    def projectCrs(self):
        return QgsProject.instance().crs()

    def registerImport(self, key, kind, path, crs, reload=None):
        """Remember where an item came from and its source CRS, so the CRS can be changed later."""
        if not hasattr(self, "imports"):
            self.imports = {}
        from ..core.crs import _qcrs
        self.imports[key] = {"kind": kind, "path": path, "crs": _qcrs(crs), "reload": reload}
        self._fillWfList()

    @staticmethod
    def _bmCorners(bm):
        import numpy as np
        if not len(bm.xyz):
            return None
        lo = (bm.xyz - bm.size / 2).min(axis=0)
        hi = (bm.xyz + bm.size / 2).max(axis=0)
        return bm.world(np.array([[x, y, z] for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2])],
                                 float))

    def changeCrs(self):
        """Objects ▸ Coordinate system…: say which CRS the selected item's file is really in."""
        import json
        from .crsdialog import CrsDialog
        from qgis.PyQt.QtWidgets import QDialog
        its = self.lstWf.selectedItems() or ([self.lstWf.currentItem()] if self.lstWf.currentItem() else [])
        if not its:
            self.plugin._msg("Select an item in the list first.", Qgis.MessageLevel.Info)
            return
        kind, key = its[0].data(Qt.ItemDataRole.UserRole)
        info = getattr(self, "imports", {}).get(key)
        prj = self.projectCrs()
        if info is None and key == "drill" and self.drillLayerId:
            lyr = QgsProject.instance().mapLayer(self.drillLayerId)
            dlg = CrsDialog(self, "Coordinate system", f"Coordinate system of the layer <b>{lyr.name()}</b>:",
                            None, lyr.crs(), allow_remember=False)
            if dlg.exec() == QDialog.DialogCode.Accepted and dlg.crs().isValid():
                lyr.setCrs(dlg.crs())
                self.iface.setActiveLayer(lyr)
                self.useDrillLayer()
            return
        if info is None:
            self.plugin._msg("This item came from a QGIS layer, which already has a coordinate system - "
                             "change it in the layer's properties.", Qgis.MessageLevel.Info, 8)
            return
        name = os.path.basename(info["path"])
        dlg = CrsDialog(self, "Coordinate system",
                        f"Which coordinate system is <b>{name}</b> really in? It is currently read as "
                        f"<b>{info['crs'].authid() or info['crs'].description()}</b>.", None, info["crs"],
                        allow_remember=False)
        if dlg.exec() != QDialog.DialogCode.Accepted or not dlg.crs().isValid():
            return
        new = dlg.crs()
        from qgis.PyQt.QtWidgets import QApplication
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            self.reprojectImport(key, new)
        except Exception as e:
            self.plugin._msg(f"Could not change the coordinate system: {e}", Qgis.MessageLevel.Critical, 10)
            return
        finally:
            QApplication.restoreOverrideCursor()
        self.plugin._msg(f"{name}: now read as {new.authid()} {new.description()}", Qgis.MessageLevel.Success, 6)

    def reprojectImport(self, key, src=None, refresh_qgis_layers=True):
        """Re-place an imported item: its file is in `src` (default: unchanged), shown in the
        project CRS. Used by CRS… and when the project CRS changes."""
        import json
        info = self.imports[key]
        src = src or info["crs"]
        prj = self.projectCrs()
        if info["kind"] == "mesh":
            from ..core.crs import transform_xyz
            m = self.hook.meshes[key]
            m.vertices = transform_xyz(m.src_vertices, src, prj)
            m.src_crs = src
            st = json.loads(self.hook.payloads[key])
            self.hook.addMesh(m, color=st.get("color"), edgeColor=st.get("edgeColor", "#1a1a1a"),
                              opacity=st.get("opacity", 1.0), mid=key, zoom=False)
        elif info["kind"] == "blocks":
            bm = self.hook.blockModels[key]
            bm.set_crs(src, prj)
            self.hook.setBlocks(key, bm, bm.payload(*bm._last_args))
            if refresh_qgis_layers:
                self._bmToQgis(bm)              # block-centre layer in the new placement
            if key == getattr(self, "bmId", None):
                self._bmSectionUpdate()
                self._bmUpdateLabel()
        elif info.get("reload"):
            info["reload"](src)
        info["crs"] = src
        self.imports[key] = info
        self._fillWfList()
        self._updateHud()

    def _projectCrsChanged(self):
        """The project CRS changed: re-place every import, the drillhole overlay, strings and the section.
        Never opens the 3D window - the data waits for it."""
        with self.hook.noOpen():
            self._projectCrsChangedImpl()

    def _projectCrsChangedImpl(self):
        new = self.projectCrs()
        old = getattr(self, "_lastProjectCrs", None)
        self._lastProjectCrs = new
        if old is None or old == new:
            return
        from ..core.crs import transform_xyz
        if self.baseSection is not None:               # keep the section where it was on the ground
            a, b = transform_xyz([[*self.baseSection.a, 0], [*self.baseSection.b, 0]], old, new)
            self.baseSection = Section((a[0], a[1]), (b[0], b[1]), self.spWidth.value())
        for key in list(getattr(self, "imports", {})):
            try:
                self.reprojectImport(key, refresh_qgis_layers=False)   # centre layers carry their own CRS
            except Exception as e:
                log(f"Could not re-place {key} in {new.authid()}: {e}")
        if self.drillLayerId and "drill" not in getattr(self, "imports", {}):
            lyr = QgsProject.instance().mapLayer(self.drillLayerId)
            if lyr is not None:
                self.hook.setOverlay("drill", polyline_payload(layer_polylines(lyr, new), DRILL_COLOR,
                                                               snap="ends", point_size=4))
        self.refreshStrings()
        self._update()

    # ---------------------------------------------------------- block model
    def loadBlockModelDialog(self):
        from qgis.PyQt.QtCore import QSettings
        start = QSettings().value("/QGeo3D/lastBmDir", "")
        path, _ = QFileDialog.getOpenFileName(
            self, "Load block model", start,
            "Block models (*.DAT *.dat *.dm *.DM *.bmf *.BMF *.mdl *.MDL *.csv *.CSV *.xlsx *.XLSX *.gpkg *.GPKG *.shp *.SHP);;"
            "Micromine (*.DAT *.dat);;Datamine (*.dm *.DM);;Vulcan (*.bmf *.BMF);;Surpac (*.mdl *.MDL);;"
            "CSV / Excel (*.csv *.CSV *.txt *.xlsx *.XLSX *.xlsm);;GeoPackage / shapefile point layer (*.gpkg *.GPKG *.shp *.SHP)")
        if path:
            QSettings().setValue("/QGeo3D/lastBmDir", os.path.dirname(path))
            from ..core.tables import TABLE_EXT
            e = os.path.splitext(path)[1].lower()
            if e in (".gpkg", ".shp"):
                self.loadBlockModelFile(path)
            elif e in TABLE_EXT:
                self.tableBlocksDialog(path)
            else:
                self.loadBlockModel(path)

    def loadBlockModelFile(self, path, layer_name=None):
        """Block model from a point layer of a GeoPackage / shapefile (asks which, if several)."""
        from ..core.layerblocks import point_layers
        pts = point_layers(path)
        if not pts:
            self.plugin._msg(f"{os.path.basename(path)} has no point layer to read as a block model.",
                             Qgis.MessageLevel.Warning, 8)
            return None
        if layer_name is None:
            if len(pts) == 1:
                layer_name = pts[0][0]
            else:
                from .gpkgpicker import BlockGpkgDialog
                dlg = BlockGpkgDialog(self, files=[path], title=f"Block model - {os.path.basename(path)}")
                if not dlg.exec() or not dlg.chosen():
                    return None
                res = None
                for p, n in dlg.chosen():
                    res = self.loadBlockModelFile(p, n) or res
                return res
        lyr = open_gpkg_layer(path, layer_name, layer_name) if path.lower().endswith(".gpkg") \
            else QgsVectorLayer(path, layer_name, "ogr")
        if not lyr.isValid():
            self.plugin._msg(f"Could not open layer {layer_name} of {os.path.basename(path)}",
                             Qgis.MessageLevel.Critical, 8)
            return None
        return self.loadBlockModelLayer(lyr, path, layer_name=layer_name)

    def showTab(self, name):
        """Bring the panel forward on one of its tabs (Objects, Section, Blocks, Strings, Query)."""
        for i in range(self.tabs.count()):
            if self.tabs.tabText(i).strip().lower().startswith(name.lower()):
                self.tabs.setCurrentIndex(i)
                break
        self.show()
        self.raise_()

    def loadBlockModelGpkgDialog(self):
        """Blocks ▸ from GeoPackage: list the block-centre layers of the GeoPackages in the work, output and
        project folders (and those already in the project); open the ones selected."""
        from .gpkgpicker import BlockGpkgDialog
        dlg = BlockGpkgDialog(self)
        if not dlg.exec():
            return None
        res = None
        for p, n in dlg.chosen():
            res = self.loadBlockModelFile(p, n) or res
        return res

    def loadBlockModelActiveLayer(self):
        lyr = self.iface.activeLayer()
        if lyr is None or lyr.type() != Qgis.LayerType.Vector or lyr.geometryType() != Qgis.GeometryType.Point:
            self.plugin._msg("Select a point layer (block centres) in the Layers panel first.",
                             Qgis.MessageLevel.Warning, 8)
            return None
        return self.loadBlockModelLayer(lyr, None, selected_only=lyr.selectedFeatureCount() > 0)

    def loadBlockModelLayer(self, lyr, path=None, selected_only=False, layer_name=None):
        """Block model from a QGIS point layer: centre / size fields under the usual names, else the
        point geometry and the centre spacing. The layer's own CRS is used."""
        from ..core.blockmodel import BlockModel
        from ..core.layerblocks import block_columns, layer_table
        from qgis.PyQt.QtWidgets import QApplication
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            fields, rows = layer_table(lyr, selected_only)
            cols, notes = block_columns(fields, rows)
            place = None
            if path and path.lower().endswith(".gpkg"):
                from ..core.blockmodel import read_gpkg_placement
                place = read_gpkg_placement(path, layer_name or lyr.name())
                names = {f[0] for f in fields}
                if place and all(n in names for n in place[2]):
                    # saved by QGeo 3D from a rotated / re-placed model: model coordinates + placement
                    cols["xc"], cols["yc"], cols["zc"] = place[2]
                    notes = [n for n in notes if not n.startswith(("centre fields differ", "block centres from"))]
                else:
                    place = None
        except Exception as e:
            QApplication.restoreOverrideCursor()
            self.plugin._msg(f"{lyr.name()}: can't read it as a block model - {e}", Qgis.MessageLevel.Critical, 10)
            return None
        QApplication.restoreOverrideCursor()
        sizes = [cols[k] for k in ("dx", "dy", "dz")]
        if not all(isinstance(v, str) for v in sizes):          # regular size guessed: let the user check it
            txt = ", ".join(v if isinstance(v, str) else ("" if v is None else f"{v:g}") for v in sizes)
            txt, ok = QInputDialog.getText(self, "Block size", f"{lyr.name()} has no block size fields.\n"
                                           "Block size X, Y, Z (m), or a field name for each:", text=txt)
            if not ok:
                return None
            parts = [p.strip() for p in txt.replace(";", ",").split(",")]
            if len(parts) != 3:
                self.plugin._msg("Give three sizes: X, Y, Z", Qgis.MessageLevel.Warning, 6)
                return None
            for k, p in zip(("dx", "dy", "dz"), parts):
                try:
                    cols[k] = float(p)
                except ValueError:
                    cols[k] = p
            notes = [n for n in notes if not n.startswith("regular block size")]
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            name = lyr.name()
            for tail in (" (block centres)", " (block centres, shown blocks only)"):
                name = name.replace(tail, "")
            if path and name.lower() in ("blocks", "points", "layer", os.path.splitext(os.path.basename(path))[0].lower()):
                name = os.path.splitext(os.path.basename(path))[0]       # generic layer name: use the file's
                if name.lower().endswith("_blocks"):
                    name = name[:-7]
            bm = BlockModel.from_table(fields, rows, cols, name, path or lyr.source())
            bm.notes.extend(notes)
            if place:
                bm.rot, bm.origin = place[0], place[1]
                bm.rotated = not np.allclose(bm.rot, np.eye(3))
            from ..core.blockmodel import GPKG_MODEL_XYZ
            junk = {"fid", "_geom_x", "_geom_y", "_geom_z"} | {n.lower() for n in GPKG_MODEL_XYZ}
            bm.numeric = [n for n in bm.numeric if n.lower() not in junk]
        except Exception as e:
            self.plugin._msg(f"Could not read block model from {lyr.name()}: {e}", Qgis.MessageLevel.Critical, 10)
            return None
        finally:
            QApplication.restoreOverrideCursor()
        crs = lyr.crs() if lyr.crs().isValid() else None
        return self._addBlockModel(bm, path or lyr.source().split("|")[0], add_to_qgis=False, crs=crs)

    def loadBlockModel(self, path, add_to_qgis=True, crs=None):
        from ..core.blockmodel import BlockModel
        from qgis.PyQt.QtWidgets import QApplication
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            bm = BlockModel.load(path)
        except Exception as e:
            self.plugin._msg(f"Could not read block model: {e}", Qgis.MessageLevel.Critical, 10)
            return None
        finally:
            QApplication.restoreOverrideCursor()
        return self._addBlockModel(bm, path, add_to_qgis, crs)

    def _addBlockModel(self, bm, path, add_to_qgis=True, crs=None):
        for note in bm.notes:
            self.plugin._msg(f"{bm.name}: {note}", Qgis.MessageLevel.Warning, 10)
        if crs is None:
            from .crsdialog import ask_source_crs
            crs = ask_source_crs(self, path, self._bmCorners(bm))
            if crs is None:
                self.plugin._msg(f"{bm.name}: import cancelled", Qgis.MessageLevel.Info)
                return None
        bm.set_crs(crs, self.projectCrs())
        self.bmId = "bm:" + bm.name
        self.registerImport(self.bmId, "blocks", path, crs)
        self.bm = bm
        self.cbBmField.blockSignals(True)
        self.cbBmField.clear()
        self.cbBmField.addItems(bm.numeric)
        self.cbBmField.setCurrentText(bm.default_field() or "")
        self.cbBmField.blockSignals(False)
        f = bm.default_field()
        self.leBmHide.setText(bm.default_hide_rule())
        self.leBmHide.setStyleSheet("")
        if f and len(bm.select(f, None, bm.default_hide_rule() if self.cbBmAir.isChecked() else "")) > 500000 \
                and not self.cbBmMin.isChecked():
            # millions of sub-blocks are too much for the 3D view: start with a grade cut-off
            self.cbBmMin.blockSignals(True)
            self.cbBmMin.setChecked(True)
            self.cbBmMin.blockSignals(False)
            self.plugin._msg(f"{bm.name} has {len(bm):,} blocks - showing {f} ≥ {self.spBmMin.value():g} only. "
                             "Change or untick 'Only ≥' on the Blocks tab.", Qgis.MessageLevel.Info, 10)
        self._bmUpdate()
        if add_to_qgis and self._wantBmGpkg(bm):
            self._bmToQgis(bm)
        return bm

    def _wantBmGpkg(self, bm):
        """Block-centre GeoPackage on load: per the Import settings (write / ask / don't write)."""
        from qgis.PyQt.QtCore import QSettings
        from qgis.PyQt.QtWidgets import QCheckBox as _CB, QMessageBox
        mode = QSettings().value(BM_GPKG_KEY, "auto") or "auto"
        if mode == "never":
            return False
        if mode == "auto":
            return True
        mb = QMessageBox(QMessageBox.Icon.Question, "Block model GeoPackage",
                         f"Write a GeoPackage of the {len(bm):,} block centres of {bm.name} and add it to QGIS "
                         "(for the 2D map)?\nThe 3D view doesn't need it; Blocks ▸ Save as GeoPackage… can write "
                         "one later.", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, self)
        cb = _CB("Don't ask again")
        mb.setCheckBox(cb)
        yes = mb.exec() == QMessageBox.StandardButton.Yes
        if cb.isChecked():
            QSettings().setValue(BM_GPKG_KEY, "auto" if yes else "never")
            i = self.cbBmGpkg.findData("auto" if yes else "never")
            if i >= 0:
                self.cbBmGpkg.setCurrentIndex(i)
        return yes

    def saveBlocksGpkg(self):
        """Blocks ▸ Save as GeoPackage…: all blocks or the blocks shown, to a file you choose."""
        bm = getattr(self, "bm", None)
        if bm is None or getattr(self, "bmId", None) not in self.hook.blockModels:
            self.plugin._msg("Load a block model first.", Qgis.MessageLevel.Warning, 6)
            return
        which = ["All blocks", f"Blocks shown in 3D ({len(bm.shown):,})"]
        ch, ok = QInputDialog.getItem(self, "Save block model as GeoPackage",
                                      f"{bm.name}: {len(bm):,} blocks. Write:", which, 0, False)
        if not ok:
            return
        od = self._importDir(getattr(bm, "path", None), quiet_ask=True) or work_dir()
        path, _ = QFileDialog.getSaveFileName(self, "Save block model as GeoPackage",
                                              os.path.join(od, f"{bm.name}_blocks.gpkg"), "GeoPackage (*.gpkg)")
        if not path:
            return
        if not path.lower().endswith(".gpkg"):
            path += ".gpkg"
        if is_protected(path):
            self.plugin._msg("That folder is protected - choose another.", Qgis.MessageLevel.Warning, 8)
            return
        self._bmToQgis(bm, out=path, idx=None if ch == which[0] else bm.shown)

    def _bmToQgis(self, bm, out=None, idx="auto"):
        from ..core.blockmodel import write_gpkg
        big = len(bm) > 500000                  # millions of sub-blocks: only the blocks shown in 3D
        if idx == "auto":
            idx = bm.shown if big else None
        else:
            big = idx is not None
        if out is None:
            od = self._importDir(getattr(bm, "path", None), quiet_ask=True)
            out = self._freshGpkg(os.path.join(od or work_dir(), f"{bm.name}_blocks.gpkg"))
        elif os.path.exists(out):
            try:
                os.remove(out)
            except OSError as e:
                self.plugin._msg(f"Can't replace {out}: {e}", Qgis.MessageLevel.Warning, 8)
                return
        try:
            write_gpkg(bm, out, self.canvas.mapSettings().destinationCrs(), idx=idx)
        except Exception as e:
            self.plugin._msg(f"Block model shown in 3D, but the QGIS layer could not be written: {e}",
                             Qgis.MessageLevel.Warning, 10)
            return
        from qgis.core import QgsGraduatedSymbolRenderer, QgsMarkerSymbol, QgsRendererRange
        prj = QgsProject.instance()
        root = prj.layerTreeRoot()
        gname = f"Block model: {bm.name}"
        old = root.findGroup(gname)
        if old is not None:
            root.removeChildNode(old)
        grp = root.insertGroup(0, gname)
        lyr = open_gpkg_layer(out, "blocks", f"{bm.name} (block centres{', shown blocks only' if big else ''})")
        self._noteGpkg(f"bm:{bm.name}", out)
        if not lyr.isValid():
            return
        f = bm.default_field()
        if f:
            def sym(col):
                return QgsMarkerSymbol.createSimple({"name": "square", "size": "1.2", "color": col, "outline_style": "no"})
            br = bm.breaks(f)
            if br:
                ranges = [QgsRendererRange(-1e30 if lo is None else float(lo), 1e30 if hi is None else float(hi),
                                           sym(col), lab) for lo, hi, col, lab in br]
                lyr.setRenderer(QgsGraduatedSymbolRenderer(f, ranges))
            else:                      # coded field (resource category ...): one colour per code
                from qgis.core import QgsCategorizedSymbolRenderer, QgsRendererCategory
                cats = []
                for lab, col, ci in bm.classify(f, np.arange(len(bm))):
                    code = float(bm.data[f][ci[0]])
                    cats.append(QgsRendererCategory(code, sym(col), lab))
                lyr.setRenderer(QgsCategorizedSymbolRenderer(f, cats))
        prj.addMapLayer(lyr, False)
        grp.addLayer(lyr)
        root.findLayer(lyr.id()).setItemVisibilityChecked(False)     # 100k points: off until wanted

    def _hideRule(self, bm):
        """Current hide rule for bm ('' = hide nothing); invalid rules are reported and ignored."""
        if not self.cbBmAir.isChecked():
            return ""
        rule = self.leBmHide.text().strip()
        try:
            bm.parse_rule(rule)
            self.leBmHide.setStyleSheet("")
            return rule
        except ValueError as e:
            self.leBmHide.setStyleSheet("border: 1px solid #d62728;")
            self.plugin._msg(f"Hide rule: {e}", Qgis.MessageLevel.Warning, 6)
            return getattr(bm, "hide_rule", bm.default_hide_rule())

    def _fillHideMenu(self):
        m = self.bBmHide.menu()
        m.clear()
        bm = getattr(self, "bm", None)
        if bm is None:
            m.addAction("Load a block model first").setEnabled(False)
            return
        sugg = bm.hide_suggestions()
        for rule, label in sugg:
            m.addAction(f"{label}    [{rule}]", lambda r=rule: self._setHideRule(r))
        if len(sugg) > 1:
            m.addSeparator()
            allr = " or ".join(r for r, _l in sugg if not r.lower().endswith("= 2") or "fill" not in _l)
            m.addAction(f"All of the above    [{allr}]", lambda r=allr: self._setHideRule(r))
        m.addSeparator()
        m.addAction("Show everything (no rule)", lambda: self._setHideRule(""))

    def _setHideRule(self, rule):
        self.leBmHide.setText(rule)
        self.cbBmAir.setChecked(bool(rule))
        self._bmUpdate()

    def _bmUpdate(self):
        bm = getattr(self, "bm", None)
        if bm is None:
            return
        field = self.cbBmField.currentText() or bm.default_field()
        rule = self._hideRule(bm)
        bm.hide_rule = rule
        spec = bm.payload(field, self.spBmMin.value() if self.cbBmMin.isChecked() else None,
                          rule, self.spBmOp.value())
        self.hook.setBlocks(self.bmId, bm, spec)
        if self.cbBmDisplay.currentData() == "outline":
            self.hook.setMeshVisible(self.bmId, False)
        self._bmSectionUpdate()
        self._bmUpdateLabel()

    # ------------------------------------------------- block colours / QGIS styles
    def bmStyleDialog(self):
        bm, bid = getattr(self, "bm", None), getattr(self, "bmId", None)
        if bm is None or bid not in self.hook.blockModels:
            self.plugin._msg("Load a block model first.", Qgis.MessageLevel.Info, 5)
            return
        from .bmstyledialog import BmStyleDialog
        dlg = BmStyleDialog(self, bid, bm, self.cbBmField.currentText() or bm.default_field())
        dlg.exec()

    def bmQgisLayers(self, bm):
        """QGIS point layers holding this block model's centres."""
        from .blocktree import bm_name, is_block_layer
        out = []
        src = os.path.normcase(os.path.abspath(bm.path)) if getattr(bm, "path", None) else None
        for ml in QgsProject.instance().mapLayers().values():
            try:
                if not is_block_layer(ml):
                    continue
                p = os.path.normcase(os.path.abspath((ml.source() or "").split("|")[0]))
                if bm_name(ml) == bm.name or (src and p == src):
                    out.append(ml)
            except Exception:
                continue
        return out

    def applyBmStyle(self, bid, field, style, to_qgis=False):
        """Colour block model bid by field with style (core.bmstyle); optionally style its QGIS layer too."""
        bm = self.hook.blockModels.get(bid)
        if bm is None or not field:
            return
        bm.__dict__.setdefault("styles", {})[field] = style
        if to_qgis:
            from ..core import bmstyle
            self._styleFromUs = True
            try:
                for ml in self.bmQgisLayers(bm):
                    qf = next((f.name() for f in ml.fields() if f.name().lower() == field.lower()), None)
                    if qf:
                        ml.setRenderer(bmstyle.to_renderer(qf, style))
                        ml.triggerRepaint()
                        self.iface.layerTreeView().refreshLayerSymbology(ml.id())
            finally:
                self._styleFromUs = False
        self._restyleBm(bid, field)

    def _restyleBm(self, bid, field):
        bm = self.hook.blockModels.get(bid)
        if bm is None:
            return
        if bm is getattr(self, "bm", None):
            self.cbBmField.blockSignals(True)
            if self.cbBmField.findText(field) < 0:
                self.cbBmField.addItem(field)
            self.cbBmField.setCurrentText(field)
            self.cbBmField.blockSignals(False)
            self._bmUpdate()
            return
        args = list(getattr(bm, "_last_args", (field, None, True, 1.0)))
        args[0] = field
        self.hook.setBlocks(bid, bm, bm.payload(*args))

    def _watchBmStyles(self, *_):
        """Block models follow the style of their QGIS block-centre layer (Symbology changes, loaded styles)."""
        if getattr(self, "_dead", False):
            return
        watched = self.__dict__.setdefault("_styleWatch", {})
        for bid, bm in list(self.hook.blockModels.items()):
            for ml in self.bmQgisLayers(bm):
                key = (bid, ml.id())
                if key in watched:
                    continue
                cb = (lambda b=bid, lid=ml.id(): self._qgisStyleChanged(b, lid))
                try:
                    ml.rendererChanged.connect(cb)
                except (AttributeError, TypeError):
                    continue
                watched[key] = cb
                self._qgisStyleChanged(bid, ml.id(), initial=True)

    def _unwatchBmStyles(self):
        for (bid, lid), cb in list(getattr(self, "_styleWatch", {}).items()):
            ml = QgsProject.instance().mapLayer(lid)
            if ml is not None:
                try:
                    ml.rendererChanged.disconnect(cb)
                except (TypeError, RuntimeError):
                    pass
        self._styleWatch = {}

    def _qgisStyleChanged(self, bid, lid, initial=False):
        try:
            self._qgisStyleChangedImpl(bid, lid, initial)
        except RuntimeError:                      # panel being torn down
            pass

    def _qgisStyleChangedImpl(self, bid, lid, initial=False):
        if getattr(self, "_styleFromUs", False):
            return
        from ..core import bmstyle
        bm = self.hook.blockModels.get(bid)
        ml = QgsProject.instance().mapLayer(lid)
        if bm is None or ml is None or not getattr(bm, "followQgis", True):
            return
        res = bmstyle.from_renderer(ml.renderer())
        if res is None:
            return
        field = bmstyle.match_field(bm, res[0])
        if field is None:
            return
        if initial and getattr(bm, "styles", {}).get(field) == res[1]:
            return
        bm.__dict__.setdefault("styles", {})[field] = res[1]
        self._restyleBm(bid, field)

    def blocksHidden(self, bid):
        """True when the user ticked the block model off (in Outlines mode the solid blocks are hidden
        on purpose, so hiddenMeshes alone can't tell)."""
        bm = self.hook.blockModels.get(bid)
        if bm is None:
            return True
        if "userHidden" in bm.__dict__:
            return bm.userHidden
        if bid == getattr(self, "bmId", None) and self.cbBmDisplay.currentData() == "outline":
            return "blocksec" in self.hook.hiddenOverlays
        return bid in self.hook.hiddenMeshes

    def setBlocksShown(self, bid, on):
        """Show / hide a block model: its solid blocks and its outlines on the section line."""
        bm = self.hook.blockModels.get(bid)
        if bm is None:
            return
        bm.userHidden = not on
        current = bid == getattr(self, "bmId", None)
        mode = self.cbBmDisplay.currentData() if current else "solid"
        self.hook.setMeshVisible(bid, on and mode != "outline")
        if current and "blocksec" in self.hook.overlays:
            self.hook.setOverlayVisible("blocksec", on)
        self._fillWfList()

    def _bmDisplayChanged(self):
        mode = self.cbBmDisplay.currentData()
        bid = getattr(self, "bmId", None)
        if bid in self.hook.blockModels:
            self.hook.setMeshVisible(bid, mode != "outline" and not self.blocksHidden(bid))
        if mode != "solid" and self.currentSection() is None:
            self.plugin._msg("Block outlines are drawn on the section line - draw a section first.",
                             Qgis.MessageLevel.Info, 6)
        self._bmSectionUpdate()
        self._fillWfList()

    def _bmSectionUpdate(self):
        """Block outlines on the section line (overlay 'blocksec')."""
        from ..core.blockmodel import outline_payload
        bm, s = getattr(self, "bm", None), self.currentSection()
        mode = self.cbBmDisplay.currentData() if hasattr(self, "cbBmDisplay") else "solid"
        if bm is None or s is None or mode == "solid" or getattr(self, "bmId", None) not in self.hook.blockModels:
            if "blocksec" in self.hook.overlays:
                self.hook.setOverlay("blocksec", None)
                self._bmUpdateLabel()
            return
        try:
            corners, cols = bm.section_outlines(s.a, s.b, self.spBmShrink.value() / 100.0, self.cbEnds.isChecked())
        except ValueError as e:
            self.plugin._msg(str(e), Qgis.MessageLevel.Warning, 8)
            return
        self._bmSectionCount = len(corners)
        self.hook.setOverlay("blocksec", outline_payload(corners, cols))
        if self.blocksHidden(self.bmId):                 # block model ticked off: its outlines too
            self.hook.setOverlayVisible("blocksec", False)
        self._bmUpdateLabel()

    def reportDialog(self, scope=None):
        if getattr(self, "bm", None) is None:
            self.plugin._msg("Load a block model first (Blocks tab).", Qgis.MessageLevel.Warning, 6)
            return
        from .reportdialog import ReportDialog
        sel = []
        for it in (self.lstWf.selectedItems() if hasattr(self, "lstWf") else []):
            d = it.data(Qt.ItemDataRole.UserRole)
            if isinstance(d, (tuple, list)) and d[0] == "mesh":
                sel.append(d[1])
        self._report = ReportDialog(self, scope, sel)
        self._report.show()

    def _bmUpdateLabel(self):
        bm = getattr(self, "bm", None)
        if bm is None or getattr(self, "bmId", None) not in self.hook.blockModels:
            self.lblBm.setText("")
            return
        st = bm.stats()
        lines = [f"<b>{bm.name}</b>: {st['blocks']:,} of {len(bm):,} blocks shown"]
        if "tonnes" in st:
            t = f"{st['tonnes']:,.0f} t"
            if "grade" in st:
                u = st["grade_unit"]
                g = f"{st['grade']:.2f}" if st["grade"] >= 0.1 or u not in ("ppm", "ppb") else f"{st['grade']:.3g}"
                t += f" @ {g} {u} {st['grade_field']} = {st['metal']:,.0f} {st['metal_unit']}"
            lines.append(t)
            if st.get("tonnes_from"):
                lines.append(f"<span style='color:#888'>tonnes from {st['tonnes_from']}</span>")
        if "blocksec" in self.hook.overlays:
            lines.append(f"{getattr(self, '_bmSectionCount', 0):,} block outlines on the section")
        import html as _H
        leg = " ".join(f"<span style='color:{c}'>■</span> {_H.escape(lab)} ({n:,})" for lab, c, n in bm.legend())
        lines.append(leg)
        self.lblBm.setText("<br>".join(lines))

    # ------------------------------------------------------------ section
    def currentSection(self):
        if self.baseSection is None:
            return None
        return self.baseSection.offset(self.offset).with_width(self.spWidth.value())

    def setSection(self, a, b, look=True):
        self.baseSection = Section(a, b, self.spWidth.value())
        self.offset = 0.0
        self._update()
        if look:
            self.hook.viewSection()

    def step(self, direction):
        if self.baseSection is None:
            return
        d = direction * self.stepDistance()
        self.offset += d
        self._update()
        # keep zoom/angle: slide the camera with the section
        self.hook.js(f"window.Q2T4 && Q2T4.shiftView({d})")

    def stepDistance(self):
        """◀ / ▶ step: the manual distance, or (Auto) half the section width."""
        v = self.spStep.value()
        return v if v > 0 else self.spWidth.value() / 2.0

    def clearSection(self):
        self.baseSection = None
        self.offset = 0.0
        self._update()

    def _update(self, *_):
        s = self.currentSection()
        if s is None:
            self.lblInfo.setText("No section")
            self.lblOffset.setText("offset 0.0 m")
            self.rbSlab.reset(Qgis.GeometryType.Polygon)
            self.rbLine.reset(Qgis.GeometryType.Line)
            self.hook.setSection(None)
            self._bmSectionUpdate()
            self._updateHud()
            return
        self.lblInfo.setText(s.describe())
        self.lblOffset.setText(f"offset {self.offset:+.1f} m")
        self.rbSlab.setToGeometry(QgsGeometry.fromPolygonXY([[QgsPointXY(*p) for p in s.slab_polygon()]]), None)
        self.rbLine.setToGeometry(QgsGeometry.fromPolylineXY([QgsPointXY(*s.a), QgsPointXY(*s.b)]), None)
        spec = s.to_spec(enabled=self.cbClip.isChecked(),
                         limit_ends=self.cbEnds.isChecked(),
                         show_frame=self.cbFrame.isChecked())
        try:
            rng = self.hook.demProfileRange(s)
        except Exception:       # sampling is a nicety; never block the section
            rng = None
        if rng:
            spec["surfMin"], spec["surfMax"] = rng
        self.hook.setSection(spec)
        self._bmSectionUpdate()
        self._updateHud()

    def _onSectionDrawn(self, a, b):
        self.bDraw.blockSignals(True)       # don't restore the 3D window twice
        self.bDraw.setChecked(False)
        self.bDraw.blockSignals(False)
        if self.canvas.mapTool() is self.tool:
            self.canvas.unsetMapTool(self.tool)
        self.setSection((a.x(), a.y()), (b.x(), b.y()))
        self._raise3DWindow()

    def _raiseQgis(self):
        """Bring the map to the front. The Qgis2threejs window is owned by the QGIS main window,
        and Windows always keeps an owned window above its owner, so raising QGIS isn't enough:
        the 3D window is minimised while drawing and restored afterwards."""
        w = self.hook._window
        try:
            if w is not None and w.isVisible() and not w.isMinimized():
                self._3dState = w.windowState()
                w.showMinimized()
        except RuntimeError:
            pass
        mw = self.iface.mainWindow()
        if mw.isMinimized():
            mw.showNormal()
        mw.raise_()
        mw.activateWindow()
        self.canvas.setFocus()

    def _raise3DWindow(self):
        w = self.hook._window
        if w is None:
            return
        try:
            if w.isMinimized():
                st = getattr(self, "_3dState", None)
                if st is not None:
                    w.setWindowState(st & ~Qt.WindowState.WindowMinimized)
                    w.show()
                else:
                    w.showNormal()
            self._3dState = None
            w.raise_()
            w.activateWindow()
        except RuntimeError:            # window already closed
            pass

    def _toggleDraw(self, on):
        vt = self._vtb()
        if vt is not None:
            vt._mirror(vt.aDraw, on)
        if on:
            self.canvas.setMapTool(self.tool)
            self._raiseQgis()           # the button may be in the 3D window; drawing happens on the map
        else:
            if self.canvas.mapTool() is self.tool:
                self.canvas.unsetMapTool(self.tool)
            if getattr(self, "_3dState", None) is not None:     # cancelled: bring the 3D view back
                self._raise3DWindow()

    def _mapToolChanged(self, new, old=None):
        if new is not self.tool and self.bDraw.isChecked():
            self.bDraw.blockSignals(True)
            self.bDraw.setChecked(False)
            self.bDraw.blockSignals(False)
            vt = self._vtb()
            if vt is not None:
                vt._mirror(vt.aDraw, False)
            if getattr(self, "_3dState", None) is not None:
                self._raise3DWindow()

    def _useSelectedLine(self):
        layer = self.iface.activeLayer()
        feats = list(layer.getSelectedFeatures()) if layer is not None and hasattr(layer, "getSelectedFeatures") else []
        geoms = [f.geometry() for f in feats if f.geometry().type() == Qgis.GeometryType.Line]
        if not geoms:
            self.plugin._msg("Select one line feature in the active layer first.", Qgis.MessageLevel.Warning)
            return
        g = QgsGeometry(geoms[0])
        if layer.crs() != self.canvas.mapSettings().destinationCrs():
            from qgis.core import QgsCoordinateTransform, QgsProject
            g.transform(QgsCoordinateTransform(layer.crs(), self.canvas.mapSettings().destinationCrs(), QgsProject.instance()))
        verts = [v for v in g.vertices()]
        self.setSection((verts[0].x(), verts[0].y()), (verts[-1].x(), verts[-1].y()))

    # ------------------------------------------------------------ strings
    def defaultGpkg(self):
        """<project folder>/QGeo 3D data/<project>_strings.gpkg - unless the project already has a strings file in
        the work folder (kept, so its strings and wireframes stay in use), or the project is unsaved or in a
        protected folder (work folder)."""
        proj = QgsProject.instance().baseName() or "untitled"
        name = f"{proj}_strings.gpkg"
        old = os.path.join(work_dir(), name)
        if os.path.exists(old):
            return old
        pf = QgsProject.instance().fileName()
        if pf:
            d = _subdir(os.path.dirname(pf))
            if not is_protected(d):
                try:
                    os.makedirs(d, exist_ok=True)
                    return os.path.join(d, name)
                except OSError:
                    pass
        return old

    def ensureStore(self):
        if self.store.layer is None:
            self.store.open(self.defaultGpkg(), self.canvas.mapSettings().destinationCrs())
            self.lblGpkg.setText(self.store.path)
        return self.store

    def chooseGpkg(self):
        path, _ = QFileDialog.getSaveFileName(self, "Strings GeoPackage", self.store.path or self.defaultGpkg(),
                                              "GeoPackage (*.gpkg)", options=QFileDialog.Option.DontConfirmOverwrite)
        if not path:
            return
        try:
            self.store.open(path, self.canvas.mapSettings().destinationCrs())
            self.lblGpkg.setText(path)
            self.showSavedWireframes()           # that GeoPackage's wireframes come with it
        except Exception as e:
            self.plugin._msg(str(e), Qgis.MessageLevel.Critical, 10)

    def _snapOpts(self):
        return {"section": self.cbSnapSec.isChecked(), "surface": self.cbSnapSurf.isChecked(),
                "drill": self.cbSnapDh.isChecked(), "strings": self.cbSnapStr.isChecked(),
                "tol": self.spTol.value()}

    def _snapChanged(self, *_):
        self.hook.setSnap(**self._snapOpts())

    def _toggleDigitise(self, on):
        if on:
            try:
                self.ensureStore()
            except Exception as e:
                self.plugin._msg(str(e), Qgis.MessageLevel.Critical, 10)
                self._setDigButton(False)
                return
            self.hook.snapOpts.update(self._snapOpts())
            if not self.hook.startDigitise():
                self.plugin._msg("3D view is still loading - try again in a moment.", Qgis.MessageLevel.Warning)
                self._setDigButton(False)
                return
            self._sendTraceItems()
            self.hook.js(f"Q2T4.dig.snapMode = {json.dumps(self.cbSnapMode.currentData())}; "
                         f"Q2T4.dig.snapOn = {'true' if self.cbSnapOn.isChecked() else 'false'}; "
                         f"Q2T4.dig.trace = {'true' if self.cbTrace.isChecked() else 'false'};")
        else:
            self.hook.stopDigitise()

    def _setDigButton(self, on):
        self.bDig.blockSignals(True)
        self.bDig.setChecked(on)
        self.bDig.blockSignals(False)
        vt = self._vtb()                         # keep the 3D window's toolbar button in step too
        if vt is not None and hasattr(vt, "aString"):
            vt._mirror(vt.aString, bool(on))

    def _sectionTag(self):
        s = self.currentSection()
        if s is None:
            return ""
        cx, cy = s.centre
        return f"az {s.azimuth:05.1f} off {self.offset:+.1f} w {s.width:g} c {cx:.1f},{cy:.1f}"

    def _poll(self):
        if self.hook._page is None:
            # notice a Qgis2threejs window opened on its own: hook it (ringfence, menu, panel)
            try:
                self.hook.window()
            except Exception:
                pass
            if self.hook._page is None:
                return
        try:
            info = self.hook.poll()
        except Exception:
            return
        if not info:
            return
        events = info.get("events") or []
        if isinstance(events, str):
            import json
            events = json.loads(events)
        vt = self._vtb()
        if vt is not None:
            try:
                vt.updateStatus(info)
            except RuntimeError:
                pass
        q = info.get("mode") == "Query"
        if self.bQuery.isChecked() != q:
            self.bQuery.blockSignals(True)
            self.bQuery.setChecked(q)
            self.bQuery.blockSignals(False)
        for ev in events:
            if ev.get("type") == "mode":
                self._setDigButton(bool(ev.get("active")))
            elif ev.get("type") == "query":
                self._handleQuery(ev)
            elif ev.get("type") in ("measure", "measureMode", "editMode", "vertexMoved", "vertexDeleted",
                                    "vertexInserted", "stringCut", "stringCopy", "snapToggled", "traceToggled",
                                    "stringsSelected", "stringsDelete", "pointsDelete", "triDelMode", "triangleDelete",
                                    "triMode", "triangle", "linkPick", "linkUndo", "capPick", "capUndo", "renameSelected", "undo", "redo"):
                self._toolEvent(ev)
            elif ev.get("type") == "string":
                try:
                    self.ensureStore()
                    pts, kinds = list(ev["pts"]), list(ev.get("kinds") or [])
                    if self.cbClosed.isChecked() and len(pts) >= 3:
                        pts.append(pts[0])
                        kinds.append("close")
                    lay = self.activeLayer()
                    no = self.store.add(pts, lay, self._sectionTag(), kinds, layer=lay)
                    log(f"String {no} saved ({len(pts)} points) to {self.store.path}")
                except Exception as e:
                    self.plugin._msg(f"Could not save string: {e}", Qgis.MessageLevel.Critical, 10)

    # strings hidden one by one (ticks in the Wireframing list), kept in the project
    HIDDEN_KEY = ("QGeo3D", "hiddenStrings")

    def hiddenStrings(self):
        if getattr(self, "_hiddenStrings", None) is None:
            import json as _json
            raw, ok = QgsProject.instance().readEntry(*self.HIDDEN_KEY, "[]")
            try:
                self._hiddenStrings = {int(n) for n in _json.loads(raw or "[]")}
            except (ValueError, TypeError):
                self._hiddenStrings = set()
        return self._hiddenStrings

    def setStringShown(self, no, on):
        import json as _json
        h = self.hiddenStrings()
        (h.discard if on else h.add)(int(no))
        QgsProject.instance().writeEntry(*self.HIDDEN_KEY, _json.dumps(sorted(h)))
        self._pushStrings()
        if getattr(self, "_editMode", None) or getattr(self.bDig, "isChecked", lambda: False)():
            self._refreshEditItems()
        self._fillWfList()

    def _pushStrings(self):
        """Visible strings -> the 3D overlay and the 2D map band."""
        hidden = self.hiddenStrings()
        recs = string_records(self.store)
        gone = hidden - {no for no, *_ in recs}
        if gone and self.store.layer is not None:        # deleted strings: their numbers may be reused
            import json as _json
            hidden -= gone
            QgsProject.instance().writeEntry(*self.HIDDEN_KEY, _json.dumps(sorted(hidden)))
        polys = [pts for no, _n, pts, _c in recs if no not in hidden]
        self._stringCounts = (len(polys), len(recs))
        self.hook.setOverlay("strings", polyline_payload(polys, STRING_COLOR, snap="all", point_size=6) if polys else None)
        return polys

    def refreshStrings(self, *_):
        polys = self._pushStrings()
        inProject = self.store.layer is not None and QgsProject.instance().mapLayer(self.store.layer.id()) is not None
        if polys and self.cbStrMap.isChecked() and not inProject:
            self.rbStrings.setToGeometry(QgsGeometry.fromMultiPolylineXY(
                [[QgsPointXY(p[0], p[1]) for p in pl] for pl in polys]), None)
        else:
            self.rbStrings.reset(Qgis.GeometryType.Line)
        self._fillStringList()
        self._fillSecList()
        self._syncStrMap()
        n = self.store.count()
        self.lblStrings.setText(f"{n} string(s) in {os.path.basename(self.store.path)}" if self.store.path else "")

    def deleteLastString(self):
        no = self.store.delete_last()
        if no is not None:
            self.plugin._msg(f"Deleted string {no}")

    def addStringsLayerToProject(self):
        proj = QgsProject.instance().fileName()
        if is_protected(proj):
            self.plugin._msg("This project is ringfenced, so the strings layer isn't added to it. "
                             f"Strings stay in {self.store.path or self.defaultGpkg()} and show on the map as an overlay.",
                             Qgis.MessageLevel.Warning, 10)
            return
        try:
            self.ensureStore()
        except Exception as e:
            self.plugin._msg(str(e), Qgis.MessageLevel.Critical, 10)
            return
        lyr = open_gpkg_layer(self.store.path, "strings", "QGeo 3D strings")
        QgsProject.instance().addMapLayer(lyr)
        self.store.adoptProjectLayer(lyr)

    # ---------------------------------------------------------- wireframing
    def _fillStringList(self, *_):
        """Wireframing box: the wireframes in the 3D view, then the strings."""
        keep = {self._key(it) for it in self.lstStrings.selectedItems()}
        hidden = self.hiddenStrings()
        self.lstStrings.blockSignals(True)
        self.lstStrings.clear()
        from qgis.PyQt.QtGui import QFont

        collapsed = self._collapsedGroups()
        arrow = lambda g: "▸" if g in collapsed else "▾"

        def header(text, gkey):
            h = QListWidgetItem(f"{arrow(gkey)} {text}")
            h.setData(Qt.ItemDataRole.UserRole, ("hdr", gkey))
            h.setToolTip("Click the arrow or double-click to minimise / maximise")
            h.setFlags(Qt.ItemFlag.ItemIsEnabled)
            f = QFont(h.font())
            f.setBold(True)
            h.setFont(f)
            self.lstStrings.addItem(h)
        wfs = [(mid, m) for mid, m in self.hook.meshes.items() if not str(mid).startswith("bm:")]
        header(f"Wireframes ({len(wfs)})", "wf")
        for mid, m in wfs:
            where = ("saved" if str(mid).startswith("built") else
                     "pending - not saved" if mid == "manual" else "3D view only")
            it = QListWidgetItem(f"  {m.name}  ·  {len(m.triangles):,} tri  ·  {where}")
            it.setData(Qt.ItemDataRole.UserRole, ("wf", mid))
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Unchecked if mid in self.hook.hiddenMeshes else Qt.CheckState.Checked)
            it.setToolTip("Select to put new triangles / linked strings into this wireframe"
                          + (" · F2 or right-click renames" if str(mid).startswith("built") else ""))
            if str(mid).startswith("built"):
                it.setFlags(it.flags() | Qt.ItemFlag.ItemIsEditable)
                it.setData(Qt.ItemDataRole.UserRole + 1, m.name.rsplit(" #", 1)[0])
            self.lstStrings.addItem(it)
            it.setHidden("wf" in collapsed)
            if ("wf", mid) in keep:
                it.setSelected(True)
        recs = string_records(self.store)
        from ..core.strings import string_layers
        lay = string_layers(self.store)
        groups = {}
        for r in recs:
            groups.setdefault(lay.get(r[0]) or r[1] or "strings", []).append(r)
        active = self.activeLayer()
        header(f"String layers ({len(groups)})  ·  {len(recs)} strings", "layers")
        for lname, rs in groups.items():
            shown = sum(1 for r in rs if r[0] not in hidden)
            h = QListWidgetItem(f"{arrow('layer:' + lname)} {'● ' if lname == active else '   '}{lname}  "
                                f"({len(rs)} string{'s' if len(rs) != 1 else ''})")
            h.setData(Qt.ItemDataRole.UserRole, ("layer", lname))
            h.setData(Qt.ItemDataRole.UserRole + 1, lname)
            h.setFlags(h.flags() | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEditable)
            h.setCheckState(Qt.CheckState.Checked if shown == len(rs) else
                            Qt.CheckState.Unchecked if shown == 0 else Qt.CheckState.PartiallyChecked)
            f = QFont(h.font())
            f.setBold(lname == active)
            h.setFont(f)
            h.setToolTip(f"String layer '{lname}'" + (" - active: new strings go here" if lname == active else
                         " - click to make it the active layer") + "\nTick shows / hides the whole layer · selecting "
                         "it selects its strings · arrow or double-click minimises / maximises · F2 or right-click "
                         "renames")
            self.lstStrings.addItem(h)
            h.setHidden("layers" in collapsed)
            if ("layer", lname) in keep:
                h.setSelected(True)
            for no, name, pts, closed in rs:
                n = len(pts) - (1 if closed else 0)
                label = f"{name}  ·  " if name and name != lname else ""
                it = QListWidgetItem(f"        {no}  {label}{n} pts  ·  {'closed' if closed else 'open'}")
                it.setData(Qt.ItemDataRole.UserRole, no)
                it.setData(Qt.ItemDataRole.UserRole + 1, name)
                it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEditable)
                it.setToolTip(f"String {no} in layer '{lname}' · double-click zooms · F2 renames · right-click: "
                              "move to another layer")
                it.setCheckState(Qt.CheckState.Unchecked if no in hidden else Qt.CheckState.Checked)
                self.lstStrings.addItem(it)
                it.setHidden("layers" in collapsed or ("layer:" + lname) in collapsed)
                if no in keep:
                    it.setSelected(True)
        self.lstStrings.blockSignals(False)
        self._fillLayerCombo(list(groups))
        self._fillWfCombo()
        self._targetLabel()

    @staticmethod
    def _key(it):
        return it.data(Qt.ItemDataRole.UserRole)

    def selectedStringNos(self):
        """Strings selected in the Wireframing list; a selected layer counts as all its strings."""
        out, lay = [], None
        for d in (self._key(it) for it in self.lstStrings.selectedItems()):
            if isinstance(d, int):
                out.append(d)
            elif isinstance(d, (tuple, list)) and d[0] == "layer":
                if lay is None:
                    from ..core.strings import string_layers
                    lay = string_layers(self.store)
                out += [n for n, l in sorted(lay.items()) if l == d[1]]
        return list(dict.fromkeys(out))

    # ------------------------------------------------------------ string layers / active wireframe
    def activeLayer(self):
        cb = getattr(self, "cbLayer", None)
        t = cb.currentText().strip() if cb is not None else ""
        return t or "strings"

    def setActiveLayer(self, name):
        if getattr(self, "cbLayer", None) is not None and name:
            self.cbLayer.setEditText(name)

    def _fillLayerCombo(self, names):
        cb = getattr(self, "cbLayer", None)
        if cb is None:
            return
        cur = cb.currentText()
        names = list(dict.fromkeys(list(names) + ([cur] if cur.strip() else [])))
        if [cb.itemText(i) for i in range(cb.count())] == names:
            return
        cb.blockSignals(True)
        cb.clear()
        cb.addItems(names)
        cb.setEditText(cur)
        cb.blockSignals(False)

    def _fillWfCombo(self):
        cb = getattr(self, "cbWf", None)
        if cb is None:
            return
        cur = cb.currentData()
        items = [("New wireframe", None)] + [(m.name, mid) for mid, m in self.hook.meshes.items()
                                             if not str(mid).startswith("bm:") and mid not in ("manual", "linking")]
        if [(cb.itemText(i), cb.itemData(i)) for i in range(cb.count())] != items:
            cb.blockSignals(True)
            cb.clear()
            for t, d in items:
                cb.addItem(t, d)
            cb.blockSignals(False)
        i = cb.findData(cur) if cur is not None else 0
        cb.blockSignals(True)
        cb.setCurrentIndex(max(0, i))
        cb.blockSignals(False)

    def _setActiveWf(self, mid):
        self._fillWfCombo()
        cb = getattr(self, "cbWf", None)
        if cb is not None:
            i = cb.findData(mid) if mid is not None else 0
            if i >= 0:
                cb.setCurrentIndex(i)
        self._targetLabel()

    # ------------------------------------------------------------ minimise / maximise groups
    GROUPS_KEY = "/QGeo3D/collapsedGroups"

    def _collapsedGroups(self):
        if getattr(self, "_collapsed", None) is None:
            from qgis.PyQt.QtCore import QSettings
            v = QSettings().value(self.GROUPS_KEY, []) or []
            self._collapsed = set([v] if isinstance(v, str) else v)
        return self._collapsed

    @staticmethod
    def _groupKey(d):
        if isinstance(d, (tuple, list)) and d[0] == "hdr" and d[1] in ("wf", "layers"):
            return d[1]
        if isinstance(d, (tuple, list)) and d[0] == "layer":
            return "layer:" + d[1]
        return None

    def toggleGroup(self, g, collapse=None):
        from qgis.PyQt.QtCore import QSettings
        c = self._collapsedGroups()
        collapse = (g not in c) if collapse is None else collapse
        (c.add if collapse else c.discard)(g)
        QSettings().setValue(self.GROUPS_KEY, sorted(c))
        QTimer.singleShot(0, self._fillStringList)

    def setAllLayersCollapsed(self, collapse):
        from ..core.strings import string_layers
        for l in set(string_layers(self.store).values()):
            (self._collapsedGroups().add if collapse else self._collapsedGroups().discard)("layer:" + l)
        self._collapsedGroups().discard("layers")
        self.toggleGroup("wf", collapse)

    def _listPressed(self, it):
        """A press on a group row's arrow minimises / maximises the group."""
        if it is None:
            return
        g = self._groupKey(self._key(it))
        if not g:
            return
        try:
            from qgis.PyQt.QtGui import QCursor
            from qgis.PyQt.QtWidgets import QStyle, QStyleOptionViewItem
            L = self.lstStrings
            pp = getattr(self, "_pressPos", None)              # where the press happened (event filter)
            x = pp.x() if pp is not None else L.viewport().mapFromGlobal(QCursor.pos()).x()
            idx = L.indexFromItem(it)
            opt = QStyleOptionViewItem()
            L.initViewItemOption(opt)
            opt.rect = L.visualRect(idx)
            L.itemDelegate().initStyleOption(opt, idx)
            tr = L.style().subElementRect(QStyle.SubElement.SE_ItemViewItemText, opt, L)
            x0 = tr.left()
        except Exception:
            x0 = 20 if it.flags() & Qt.ItemFlag.ItemIsUserCheckable else 2
        if x0 - 3 <= x <= x0 + self.lstStrings.fontMetrics().horizontalAdvance("▾ ") + 4:
            self._arrowClick = True                 # the click that follows only opens / closes the group
            self.toggleGroup(g)

    def eventFilter(self, obj, ev):
        """Remember where the Wireframing list was pressed (for the group arrows)."""
        try:
            from qgis.PyQt.QtCore import QEvent
            if ev.type() == QEvent.Type.MouseButtonPress and obj is self.lstStrings.viewport():
                p = ev.position().toPoint() if hasattr(ev, "position") else ev.pos()
                self._pressPos = p
        except (AttributeError, RuntimeError):
            pass
        return super().eventFilter(obj, ev)

    def _listClicked(self, it):
        if getattr(self, "_arrowClick", False):
            self._arrowClick = False
            return
        if it is None:                       # the list was rebuilt while Qt was still delivering the click
            return
        d = self._key(it)
        if isinstance(d, (tuple, list)) and d[0] == "layer":
            if d[1] != self.activeLayer():
                self.setActiveLayer(d[1])
        elif isinstance(d, (tuple, list)) and d[0] == "wf" and d[1] in self.hook.meshes and d[1] != "manual":
            self._setActiveWf(d[1])

    def setLayerShown(self, lname, on):
        import json as _json
        from ..core.strings import string_layers
        h = self.hiddenStrings()
        for n, l in string_layers(self.store).items():
            if l == lname:
                (h.discard if on else h.add)(int(n))
        QgsProject.instance().writeEntry(*self.HIDDEN_KEY, _json.dumps(sorted(h)))
        self._pushStrings()
        if getattr(self, "_editMode", None) or getattr(self.bDig, "isChecked", lambda: False)():
            self._refreshEditItems()
        self._fillWfList()
        self._fillStringList()

    def moveStringsToLayer(self, nos, layer=None):
        from ..core import editing as E
        if layer is None:
            layer, ok = QInputDialog.getText(self, "New string layer", "Name of the new layer:", text="")
            if not ok:
                return
        layer = (layer or "").strip()
        if not layer or not nos:
            return
        try:
            n = E.set_string_layer(self.store, nos, layer)
        except Exception as e:
            self.plugin._msg(f"Not moved: {e}", Qgis.MessageLevel.Critical, 8)
            return
        self.plugin._msg(f"{n} string(s) moved to layer '{layer}'", Qgis.MessageLevel.Info, 4)

    def _wfListTicked(self, it):
        if it is None:
            return
        d = self._key(it)
        on = it.checkState() == Qt.CheckState.Checked
        if isinstance(d, (tuple, list)) and d[0] == "layer":
            on = it.checkState() != Qt.CheckState.Unchecked
            QTimer.singleShot(0, lambda n=d[1], o=on: self.setLayerShown(n, o))
            return
        # after the click has been delivered: showing / hiding rebuilds this list, which deletes the item
        if isinstance(d, int):
            QTimer.singleShot(0, lambda n=d, o=on: self.setStringShown(n, o))
        elif isinstance(d, (tuple, list)) and d[0] == "wf":
            QTimer.singleShot(0, lambda m=d[1], o=on: self.hook.setMeshVisible(m, o))

    def _targetLabel(self):
        if not hasattr(self, "lblTri"):
            return
        tgt = self.wfTarget()
        if getattr(self, "bTri", None) is not None and self.bTri.isChecked():
            self.lblTri.setText(f"Adding triangles to: {self.hook.meshes[tgt].name}" if tgt else
                                "Adding triangles to a new wireframe (blue until saved)")

    def _askWfName(self, default):
        """Name for a new wireframe (None = cancelled)."""
        txt, ok = QInputDialog.getText(self, "New wireframe", "Name of the new wireframe:", text=default)
        if not ok:
            return None
        return txt.strip() or default

    def _newWfDefault(self):
        n = 1 + sum(1 for k in self.hook.meshes if str(k).startswith("built"))
        return f"wireframe {n}"

    def _stringListMenu(self, pos):
        """Right-click in the Wireframing list: Rename, Zoom to, Open path, Delete … from GeoPackage, Delete layer."""
        from qgis.PyQt.QtWidgets import QMenu
        it = self.lstStrings.itemAt(pos)
        if it is None:
            return
        key = self._key(it)
        m = QMenu(self.lstStrings)
        m.setToolTipsVisible(True)
        g = self._groupKey(key)
        if g:
            col = g in self._collapsedGroups()
            m.addAction("Maximise" if col else "Minimise", lambda: self.toggleGroup(g))
            m.addAction("Minimise all", lambda: self.setAllLayersCollapsed(True))
            m.addAction("Maximise all", lambda: self.setAllLayersCollapsed(False))
            m.addSeparator()
        if isinstance(key, (tuple, list)) and key[0] == "layer":
            if key[1] != self.activeLayer():
                m.addAction("Make active layer (new strings go here)", lambda: self.setActiveLayer(key[1]))
            m.addAction("Rename layer…", lambda: self.renameItem(key))
            a = m.addAction("Open path", lambda: self.revealPath(self.store.path))
            a.setToolTip(self.store.path or "")
        if isinstance(key, int) or (isinstance(key, (tuple, list)) and key[0] == "wf" and str(key[1]).startswith("built")):
            m.addAction("Rename…", lambda: self.renameItem(key))
        if isinstance(key, int) or (isinstance(key, (tuple, list)) and key[0] == "wf"):
            m.addAction("Zoom to", lambda: self._zoomString(it))
            path = self.store.path if isinstance(key, int) else self.itemPath("mesh", key[1])
            a = m.addAction("Open path", lambda p=path: self.revealPath(p))
            a.setEnabled(bool(path))
            a.setToolTip(path or "Not from a file")
        # what the delete actions work on: the selection if the clicked item is in it, else the clicked item
        sel = [self._key(i) for i in self.lstStrings.selectedItems()] if it.isSelected() else [key]
        nos = [k for k in sel if isinstance(k, int)]
        if any(isinstance(k, (tuple, list)) and k[0] == "layer" for k in sel):
            from ..core.strings import string_layers
            lay = string_layers(self.store)
            for k in sel:
                if isinstance(k, (tuple, list)) and k[0] == "layer":
                    nos += [n for n, l in sorted(lay.items()) if l == k[1]]
            nos = list(dict.fromkeys(nos))
        if nos and not (isinstance(key, (tuple, list)) and key[0] == "layer"):
            from ..core.strings import string_layers
            mv = m.addMenu(f"Move {len(nos)} string{'s' if len(nos) > 1 else ''} to layer")
            for lname in dict.fromkeys(string_layers(self.store).values()):
                mv.addAction(lname, lambda l=lname: self.moveStringsToLayer(nos, l))
            mv.addSeparator()
            mv.addAction("New layer…", lambda: self.moveStringsToLayer(nos, None))
        wfs = [k[1] for k in sel if isinstance(k, (tuple, list)) and k[0] == "wf"]
        if nos or wfs:
            m.addSeparator()
        if nos:
            m.addAction(f"Delete {len(nos)} string{'s' if len(nos) > 1 else ''} from GeoPackage…",
                        lambda: self.deleteStringsFromGpkg(nos))
        if wfs:
            built = [w for w in wfs if str(w).startswith("built")]
            if built:
                m.addAction(f"Delete {len(built)} wireframe{'s' if len(built) > 1 else ''} from GeoPackage…",
                            lambda: self.deleteWireframesFromGpkg(built))
            others = [w for w in wfs if not str(w).startswith("built")]
            if others:
                m.addAction("Remove from 3D view", lambda: self.removeItems([("mesh", w) for w in others]))
        self._addDeleteLayerMenu(m)
        m.exec(self.lstStrings.viewport().mapToGlobal(pos))

    def _addDeleteLayerMenu(self, m):
        from ..core.strings import gpkg_layer_count, LAYER_NAME, WF_LAYER
        sub = m.addMenu("Delete layer")
        sp, wp = self.store.path, self.wireframeGpkg()
        ns = gpkg_layer_count(sp, LAYER_NAME) if sp else None
        nw = gpkg_layer_count(wp, WF_LAYER) if wp else None
        a = sub.addAction(f"Strings layer '{LAYER_NAME}'" + (f"  ({ns} strings)" if ns is not None else "") + "…",
                          lambda: self.deleteGpkgLayer("strings"))
        a.setEnabled(ns is not None)
        a.setToolTip(sp or "No strings GeoPackage")
        a = sub.addAction(f"Wireframes layer '{WF_LAYER}'" + (f"  ({nw} wireframes)" if nw is not None else "") + "…",
                          lambda: self.deleteGpkgLayer("wireframes"))
        a.setEnabled(nw is not None)
        a.setToolTip(wp or "No wireframes GeoPackage")
        sub.setToolTipsVisible(True)

    def _confirmDelete(self, title, what, undo_note):
        """Warning listing exactly what will be deleted; True = go ahead."""
        from qgis.PyQt.QtWidgets import QMessageBox
        mb = QMessageBox(QMessageBox.Icon.Warning, title, what + "\n\n" + undo_note, parent=self)
        bDel = mb.addButton("Delete", QMessageBox.ButtonRole.DestructiveRole)
        bCancel = mb.addButton(QMessageBox.StandardButton.Cancel)
        mb.setDefaultButton(bCancel)
        mb.exec()
        return mb.clickedButton() is bDel

    def deleteSelected(self):
        """Delete button: the strings / wireframes selected in the Wireframing list (warns first); with nothing
        selected, the pending (blue) triangles."""
        sel = [self._key(i) for i in self.lstStrings.selectedItems()]
        nos = [k for k in sel if isinstance(k, int)]
        wfs = [k[1] for k in sel if isinstance(k, (tuple, list)) and k[0] == "wf"]
        if nos:
            self.deleteStringsFromGpkg(nos)
        built = [w for w in wfs if str(w).startswith("built")]
        if built:
            self.deleteWireframesFromGpkg(built)
        others = [w for w in wfs if not str(w).startswith("built")]
        if others:
            self.removeItems([("mesh", w) for w in others])
        if nos or wfs:
            return
        n = len(getattr(self, "_manualTris", []) or [])
        if n:
            if self._confirmDelete("Discard triangles", f"Discard the {n} pending (blue, not saved) triangle(s)?",
                                   "This can't be undone."):
                self.clearTriangles()
            return
        self.plugin._msg("Nothing to delete - select strings or wireframes in the Wireframing list first.",
                         Qgis.MessageLevel.Info, 5)

    def deleteStringsFromGpkg(self, nos):
        recs = {r[0]: r for r in string_records(self.store)}
        nos = [n for n in nos if n in recs]
        if not nos:
            return
        if is_protected(self.store.path):
            self.plugin._msg("Protected folder - strings not deleted.", Qgis.MessageLevel.Warning, 6)
            return
        lines = [f"  • {n}  {recs[n][1] or '(no name)'}  ·  {len(recs[n][2]) - (1 if recs[n][3] else 0)} pts  ·  "
                 f"{'closed' if recs[n][3] else 'open'}" for n in nos[:25]]
        if len(nos) > 25:
            lines.append(f"  … and {len(nos) - 25} more")
        if not self._confirmDelete(
                "Delete strings",
                f"Delete {len(nos)} string(s) from\n{self.store.path}\n(layer '{self.store.layer.name()}'):\n\n"
                + "\n".join(lines) + "\n\nWireframes built from them are kept.",
                "Ctrl+Z (Undo) brings them back while QGIS stays open."):
            return
        fids = [fid for fid, no in self._fidNo().items() if no in set(nos)]
        lyr = self.store.layer
        if lyr.isEditable():
            lyr.deleteFeatures(fids)
        else:
            lyr.dataProvider().deleteFeatures(fids)
            lyr.reload()
        lyr.triggerRepaint()
        self.store.changed.emit()
        self.hook.js("window.Q2T4 && Q2T4.setSelectedStrings([])")
        self._refreshEditItems()
        self.plugin._msg(f"{len(fids)} string(s) deleted from {os.path.basename(self.store.path)} (Ctrl+Z undoes)",
                         Qgis.MessageLevel.Info, 6)

    def deleteWireframesFromGpkg(self, mids):
        from ..core.strings import delete_saved_wireframe
        wp = self.wireframeGpkg()
        mids = [mid for mid in mids if str(mid).startswith("built")]
        if not mids or not wp:
            return
        if is_protected(wp):
            self.plugin._msg("Protected folder - wireframes not deleted.", Qgis.MessageLevel.Warning, 6)
            return
        lines = []
        for mid in mids:
            mm = self.hook.meshes.get(mid)
            st = mm.stats() if mm is not None else {}
            vol = f"  ·  {st['volume']:,.1f} m³" if st.get("closed") and st.get("volume") else ""
            lines.append(f"  • {mm.name if mm is not None else mid}  ·  {len(mm.triangles) if mm is not None else '?'} "
                         f"triangles{vol}")
        if not self._confirmDelete(
                "Delete wireframes",
                f"Delete {len(mids)} wireframe(s) from\n{wp}\n(layer 'wireframes') and from the 3D view:\n\n"
                + "\n".join(lines) + "\n\nThe strings they were built from are kept.",
                "This can't be undone."):
            return
        done, failed = 0, []
        for mid in mids:
            try:
                if delete_saved_wireframe(wp, int(str(mid)[5:])):
                    done += 1
                if mid in self.hook.meshes:
                    self.hook.removeMesh(mid)
                self._triUndo = [e for e in getattr(self, "_triUndo", []) if e[1] != mid]
                self._redo = [e for e in getattr(self, "_redo", []) if e[1] != mid]
            except Exception as e:
                failed.append(f"#{str(mid)[5:]}: {e}")
        self._fillStringList()
        self._fillWfList()
        self.plugin._msg(f"{done} wireframe(s) deleted from {os.path.basename(wp)}"
                         + (f" · not deleted: {'; '.join(failed)}" if failed else ""),
                         Qgis.MessageLevel.Warning if failed else Qgis.MessageLevel.Info, 8)

    def deleteGpkgLayer(self, which):
        """Delete the whole strings or wireframes layer (table) from its GeoPackage; the file stays."""
        from ..core.strings import delete_gpkg_layer, gpkg_layer_count, LAYER_NAME, WF_LAYER
        if which == "strings":
            path, lname = self.store.path, LAYER_NAME
        else:
            path, lname = self.wireframeGpkg(), WF_LAYER
        if not path:
            return
        if is_protected(path):
            self.plugin._msg("Protected folder - nothing deleted.", Qgis.MessageLevel.Warning, 6)
            return
        n = gpkg_layer_count(path, lname)
        if n is None:
            self.plugin._msg(f"No layer '{lname}' in {os.path.basename(path)}.", Qgis.MessageLevel.Info, 5)
            return
        prj = [l for l in QgsProject.instance().mapLayers().values()
               if os.path.normcase(os.path.normpath(l.source().split("|")[0])) == os.path.normcase(os.path.normpath(path))
               and f"layername={lname}" in l.source()]
        if which == "strings":
            body = (f"Delete the whole layer '{lname}' - all {n} string(s) - from\n{path}\n\n"
                    "The strings disappear from the 3D view and the map. Wireframes are kept.")
        else:
            built = [mid for mid in self.hook.meshes if str(mid).startswith("built")]
            body = (f"Delete the whole layer '{lname}' - all {n} saved wireframe(s) - from\n{path}\n\n"
                    f"{len(built)} of them shown in the 3D view are removed from it. Strings are kept.")
        if prj:
            body += "\n\nThese QGIS layers use it and are removed from the project:\n" + \
                    "\n".join(f"  • {l.name()}" for l in prj)
        body += "\n\nThe GeoPackage file itself stays."
        if not self._confirmDelete("Delete layer", body, "This can't be undone."):
            return
        try:
            if which == "strings":
                self.hook.js("window.Q2T4 && Q2T4.stopEdit && Q2T4.stopEdit()")
                self.store.close()
                self._triUndo = [e for e in getattr(self, "_triUndo", []) if e[0] != "strings"]
                self._redo = [e for e in getattr(self, "_redo", []) if e[0] != "strings"]
                self._strLast = None
            else:
                for mid in [k for k in self.hook.meshes if str(k).startswith("built")]:
                    self.hook.removeMesh(mid)
                    self._triUndo = [e for e in getattr(self, "_triUndo", []) if e[1] != mid]
                    self._redo = [e for e in getattr(self, "_redo", []) if e[1] != mid]
            if prj:
                QgsProject.instance().removeMapLayers([l.id() for l in prj])
            ok = delete_gpkg_layer(path, lname)
        except Exception as e:
            self.plugin._msg(f"Layer not deleted: {e}", Qgis.MessageLevel.Critical, 10)
            return
        self._fillStringList()
        self._fillWfList()
        self.plugin._msg(f"Layer '{lname}' deleted from {os.path.basename(path)}" if ok else
                         f"No layer '{lname}' found in {os.path.basename(path)}", Qgis.MessageLevel.Info, 8)

    def _objClicked(self, it):
        """Clicking a drillhole line layer in Objects picks it in Drillholes: styles and labels."""
        if it is None:
            return
        d = it.data(Qt.ItemDataRole.UserRole)
        if isinstance(d, (tuple, list)) and d[0] == "q2t" and any(lid == d[1] for _t, lid in self._dhLayerCandidates()):
            if d[1] != getattr(self, "drillLayerId", None):
                QTimer.singleShot(0, lambda lid=d[1]: self.styleDrillLayer(lid))

    def _objListMenu(self, pos):
        """Right-click in Objects ▸ In the 3D view: Zoom to, Open path."""
        from qgis.PyQt.QtWidgets import QMenu
        it = self.lstWf.itemAt(pos)
        if it is None:
            return
        d = it.data(Qt.ItemDataRole.UserRole)
        if not isinstance(d, (tuple, list)) or len(d) != 2:
            return
        kind, key = d
        m = QMenu(self.lstWf)
        m.setToolTipsVisible(True)
        if kind != "strings":
            m.addAction("Zoom to", lambda: self._wfZoom(it))
        dh = key if kind == "q2t" else (getattr(self, "drillLayerId", None) if (kind, key) == ("overlay", "drill") else None)
        if dh and any(lid == dh for _t, lid in self._dhLayerCandidates()):
            m.addAction("Styles and labels…", lambda: self.styleDrillLayer(dh))
        path = self.itemPath(kind, key)
        a = m.addAction("Open path", lambda: self.revealPath(path))
        a.setEnabled(bool(path))
        a.setToolTip(path or "Not from a file")
        if kind == "mesh" and str(key).startswith("built"):
            m.addSeparator()
            m.addAction("Delete wireframe from GeoPackage…", lambda: self.deleteWireframesFromGpkg([key]))
        if (kind == "overlay" and key == "strings") or kind == "strings" or \
                (kind == "mesh" and str(key).startswith("built")):
            if not (kind == "mesh" and str(key).startswith("built")):
                m.addSeparator()
            self._addDeleteLayerMenu(m)
        m.exec(self.lstWf.viewport().mapToGlobal(pos))

    def itemPath(self, kind, key):
        """The file an item in the 3D view came from (or is saved in); '' if none."""
        try:
            if (kind == "overlay" and key == "strings") or kind == "strings":
                return self.store.path or ""
            if kind == "mesh" and str(key).startswith("built"):
                return self.wireframeGpkg() or ""
            imp = getattr(self, "imports", {}).get(key)
            if imp and imp.get("path"):
                return str(imp["path"])
            lid = key if kind == "q2t" else (self.drillLayerId if kind == "overlay" and key == "drill" else None)
            if lid:
                lyr = QgsProject.instance().mapLayer(lid)
                if lyr is not None:
                    from qgis.core import QgsProviderRegistry
                    parts = QgsProviderRegistry.instance().decodeUri(lyr.providerType(), lyr.source())
                    return parts.get("path") or lyr.source().split("|")[0]
            if kind == "mesh":
                obj = self.hook.meshes.get(key) or self.hook.blockModels.get(key)
                src = getattr(obj, "source", "") or getattr(obj, "path", "")
                if src:
                    return str(src)
        except Exception as e:
            log(f"Path of {kind} {key}: {e}")
        return ""

    def revealPath(self, path):
        """Show a file in Explorer (selected), or open a folder."""
        import subprocess
        import sys
        p = str(path or "").split("|")[0].strip()
        if p.lower().startswith("file:///"):
            p = p[8:]
        if not p or not os.path.exists(p):
            self.plugin._msg(f"Not found: {p or 'no path'}", Qgis.MessageLevel.Warning, 6)
            return
        p = os.path.normpath(p)
        try:
            if sys.platform.startswith("win"):
                if os.path.isdir(p):
                    os.startfile(p)
                else:
                    subprocess.Popen(f'explorer /select,"{p}"')
            elif sys.platform == "darwin":
                subprocess.Popen(["open", "-R", p])
            else:
                from qgis.PyQt.QtCore import QUrl
                from qgis.PyQt.QtGui import QDesktopServices
                QDesktopServices.openUrl(QUrl.fromLocalFile(p if os.path.isdir(p) else os.path.dirname(p)))
        except Exception as e:
            self.plugin._msg(f"Can't open {p}: {e}", Qgis.MessageLevel.Warning, 6)

    def renameItem(self, key, name=None):
        """Rename a string (key = string_no) or a saved wireframe (key = ('wf', 'built<n>')); asks when
        name is None."""
        from ..core.strings import rename_saved_wireframe
        from ..core import editing as E
        if isinstance(key, (tuple, list)) and key[0] == "wf" and str(key[1]).startswith("built"):
            mid = key[1]
            m = self.hook.meshes.get(mid)
            if m is None:
                return
            old = m.name.rsplit(" #", 1)[0]
        elif isinstance(key, int):
            old = next((r[1] for r in string_records(self.store) if r[0] == key), "")
        elif isinstance(key, (tuple, list)) and key[0] == "layer":
            old = key[1]
        else:
            return
        if name is None:
            name, ok = QInputDialog.getText(self, "Rename", "New name:", text=old)
            if not ok:
                return
        name = (name or "").strip()
        if not name or name == old:
            return
        try:
            if isinstance(key, (tuple, list)) and key[0] == "layer":
                from ..core.strings import string_layers
                nos = [n for n, l in string_layers(self.store).items() if l == old]
                E.set_string_layer(self.store, nos, name, rename_from=old)
                if self.activeLayer() == old:
                    self.setActiveLayer(name)
            elif isinstance(key, int):
                E.rename_string(self.store, key, name)
            else:
                no = int(str(mid)[5:])
                if not rename_saved_wireframe(self.wireframeGpkg(), no, name):
                    raise KeyError(f"wireframe #{no} not found in {self.wireframeGpkg()}")
                m.name = f"{name} #{no}"
                try:
                    self.hook.itemsChanged.emit()
                except Exception:
                    pass
        except Exception as e:
            self.plugin._msg(f"Not renamed: {e}", Qgis.MessageLevel.Critical, 8)
            return
        self._fillStringList()
        self.plugin._msg(f"Renamed to {name}", Qgis.MessageLevel.Info, 3)

    def renameSelected3D(self):
        """F2 in the 3D view (Select tool): rename the one selected string."""
        nos = self.selectedStringNos()
        if len(nos) != 1:
            self.plugin._msg("Select one string (Select tool or the Wireframing list) to rename it.",
                             Qgis.MessageLevel.Info, 4)
            return
        self.renameItem(nos[0])

    def _zoomString(self, it):
        if it is None:
            return
        no = it.data(Qt.ItemDataRole.UserRole)
        g = self._groupKey(no)
        if g:                                       # a group row: double-click minimises / maximises it
            self.toggleGroup(g)
            return
        if isinstance(no, (tuple, list)):
            if no[0] == "wf":
                if no[1] in self.hook.hiddenMeshes:
                    it.setCheckState(Qt.CheckState.Checked)
                self.hook.zoomTo(no[1])
            return
        for n, _name, pts, _c in string_records(self.store):
            if n == no:
                import numpy as np
                p = np.asarray(pts, float)
                if no in self.hiddenStrings():
                    it.setCheckState(Qt.CheckState.Checked)
                c = (p.min(axis=0) + p.max(axis=0)) / 2
                r = max(float(np.linalg.norm(p.max(axis=0) - p.min(axis=0))) / 2, 5.0)
                self.hook.js(f"window.Q2T4 && Q2T4.zoomToBox && Q2T4.zoomToBox({c.tolist()}, {r})")
                return

    def buildWireframe(self, string_nos, cap=True, sg=None, name=None, save=True):
        """Link the given strings; returns (mesh, stats, wf_no). Used by the button and by tests."""
        recs = {r[0]: r for r in string_records(self.store)}
        chosen = [recs[n] for n in string_nos]
        name = name or "wireframe"
        outlines = [r[2] for r in chosen if r[3]]
        ties = [r[2] for r in chosen if not r[3] and len(r[2]) == 2]
        opens = [r for r in chosen if not r[3] and len(r[2]) != 2]
        if outlines and opens:
            raise ValueError("Mix of open and closed strings selected - link one kind at a time "
                             "(2-point open strings are taken as tie lines)")
        if outlines:
            # several outlines on one section (branching), outlines inside outlines (holes) or tie lines:
            # the general builder; otherwise the plain section-by-section linker
            groups = wireframing.group_sections([wireframing.clean_string(o, closed=True)[0] for o in outlines])
            if ties or any(len(g) > 1 for g in groups):
                mesh = wireframing.build_solid(outlines, ties, name=name, cap=cap)
            else:
                mesh = wireframing.build_wireframe(outlines, closed=True, cap=cap, name=name)
        else:
            mesh = wireframing.build_wireframe([r[2] for r in chosen], closed=False, cap=False, name=name)
        stats = mesh.stats()
        wf_no = None
        if save:
            import copy
            disk = copy.copy(mesh)                       # saved in the strings GeoPackage's own CRS
            disk.vertices = self.store.to_layer_crs(mesh.vertices)
            wf_no = save_wireframe(self.wireframeGpkg(), disk, name, string_nos, sg, stats,
                                   crs=self.store.layer.crs() if self.store.layer is not None else None)
            mesh.name = f"{name} #{wf_no}"
        return mesh, stats, wf_no

    def _fillWfTarget(self):
        self._fillStringList()

    def wfTarget(self):
        """The active wireframe ('Into' picker; None = make a new one)."""
        cb = getattr(self, "cbWf", None)
        mid = cb.currentData() if cb is not None else None
        return mid if mid in self.hook.meshes and mid not in ("manual", "linking") else None

    def linkSelected(self):
        nos = self.selectedStringNos()
        if len(nos) < 2:
            self.plugin._msg("Select at least two strings in the list.", Qgis.MessageLevel.Warning)
            return
        sg = self.spSG.value()
        tgt = self.wfTarget()
        if tgt is not None:                       # into an existing wireframe
            try:
                mesh, st, _ = self.buildWireframe(sorted(nos), cap=False, sg=sg, save=False)
            except Exception as e:
                self.plugin._msg(f"Wireframing failed: {e}", Qgis.MessageLevel.Critical, 10)
                return
            V = mesh.vertices
            self.mergeTriangles(tgt, [[V[a], V[b], V[c]] for a, b, c in mesh.triangles], "strings linked into it")
            getattr(self, "_editedWfs", set()).discard(tgt)
            self._validateIfClosed(tgt)
            return
        try:
            name = self._askWfName(self._newWfDefault())
            if name is None:
                return
            mesh, st, wf_no = self.buildWireframe(sorted(nos), cap=False, sg=sg, name=name)
            self.hook.addMesh(mesh, color="#e0503c", opacity=0.75, mid=f"built{wf_no}", zoom=False)
        except Exception as e:
            self.plugin._msg(f"Wireframing failed: {e}", Qgis.MessageLevel.Critical, 10)
            return
        self._reportBuilt(st, wf_no, sg)

    def _reportBuilt(self, st, wf_no, sg):
        if wf_no is not None:
            self._setActiveWf(f"built{wf_no}")           # further links / caps / triangles go into it
        if st.get("closed"):
            txt = (f"#{wf_no}: {st['triangles']} triangles, closed. Volume {st['volume']:,.1f} m³ × SG {sg:g} "
                   f"= {st['volume'] * sg:,.1f} t")
            level = Qgis.MessageLevel.Success
        else:
            txt = (f"#{wf_no}: {st['triangles']} triangles, open ({st['open_edges']} open edges), "
                   f"area {st['area']:,.1f} m². Use Close end on the end outlines to make it a solid.")
            level = Qgis.MessageLevel.Info
        if st.get("nonmanifold_edges") or st.get("inconsistent_edges"):
            txt += f" Check: {st.get('nonmanifold_edges', 0)} non-manifold, {st.get('inconsistent_edges', 0)} inconsistent edges."
            level = Qgis.MessageLevel.Warning
        self.lblWf.setText(txt)
        self.plugin._msg(txt, level, 10)
        if wf_no is not None:
            self._validateIfClosed(f"built{wf_no}")

    def _validateIfClosed(self, mid):
        """Check a wireframe once it is a closed solid (open ends while still building are expected)."""
        m = self.hook.meshes.get(mid)
        if m is not None and m.stats().get("closed"):
            self.validateWireframes([mid], quiet_if_clean=True)

    def autoShowSaved(self):
        """When a 3D window opens: put the wireframes saved in the strings GeoPackage back in the view
        (they live in the GeoPackage; the 3D view itself starts empty after a QGIS restart)."""
        try:
            if self.store.layer is None:
                if not os.path.exists(self.defaultGpkg()):
                    return
                self.ensureStore()
            if any(str(k).startswith("built") for k in self.hook.meshes):
                return
            from ..core.strings import load_wireframes
            wp = self.wireframeGpkg()
            n = len(list(load_wireframes(wp))) if wp and os.path.exists(wp) else 0
            if n:
                self.showSavedWireframes()
                self.plugin._msg(f"{n} saved wireframe(s) shown from {os.path.basename(wp)}",
                                 Qgis.MessageLevel.Info, 5)
        except Exception as e:
            log(f"Could not show saved wireframes: {e}")

    # ---------------------------------------------------------- where wireframes are saved
    WF_KEY = ("QGeo3D", "wireframeGpkg")

    def wireframeGpkg(self):
        """GeoPackage that built / hand-made wireframes go to: the strings GeoPackage unless the project
        sets a separate one (Wireframing ▸ Wireframes saved in)."""
        sep, ok = QgsProject.instance().readEntry(*self.WF_KEY, "")
        return sep if sep else self.store.path

    def _wfFileLabel(self):
        if not hasattr(self, "lblWfFile"):
            return
        sep, _ = QgsProject.instance().readEntry(*self.WF_KEY, "")
        self.lblWfFile.setText(sep if sep else "same file as the strings")
        self.lblWfFile.setToolTip(sep or (self.store.path or ""))

    def chooseWireframeGpkg(self):
        """Wireframes saved in: the strings file, or a separate GeoPackage (new or existing)."""
        from qgis.PyQt.QtWidgets import QMessageBox
        mb = QMessageBox(QMessageBox.Icon.Question, "Wireframes saved in",
                         "Save built and hand-made wireframes in the strings GeoPackage, or in a separate one?", parent=self)
        bSame = mb.addButton("Same file as strings", QMessageBox.ButtonRole.AcceptRole)
        bSep = mb.addButton("Separate file…", QMessageBox.ButtonRole.ActionRole)
        mb.addButton(QMessageBox.StandardButton.Cancel)
        mb.exec()
        if mb.clickedButton() is bSame:
            path = ""
        elif mb.clickedButton() is bSep:
            start = os.path.dirname(self.store.path) if self.store.path else work_dir()
            path, _ = QFileDialog.getSaveFileName(self, "GeoPackage for wireframes",
                                                  os.path.join(start, "wireframes.gpkg"), "GeoPackage (*.gpkg)",
                                                  options=QFileDialog.Option.DontConfirmOverwrite)
            if not path:
                return
            if not path.lower().endswith(".gpkg"):
                path += ".gpkg"
            if is_protected(path):
                self.plugin._msg("That folder is protected - choose another.", Qgis.MessageLevel.Warning, 8)
                return
        else:
            return
        QgsProject.instance().writeEntry(*self.WF_KEY, path)
        self._wfFileLabel()
        for mid in [k for k in self.hook.meshes if str(k).startswith("built")]:   # show that file's wireframes
            self.hook.removeMesh(mid)
        self.showSavedWireframes()
        self._fillWfList()
        self.plugin._msg(f"Wireframes now saved in {os.path.basename(self.wireframeGpkg() or '')}",
                         Qgis.MessageLevel.Info, 5)

    def showSavedWireframes(self):
        wp = self.wireframeGpkg()
        if not wp or not os.path.exists(wp):
            return
        try:
            from ..core.crs import transform_xyz, gpkg_crs
            src_crs = gpkg_crs(wp, "wireframes") or (self.store.layer.crs() if self.store.layer is not None else None)
            for wf_no, nm, mesh in load_wireframes(wp):
                if src_crs is not None:
                    mesh.vertices = transform_xyz(mesh.vertices, src_crs, self.projectCrs())
                mesh.name = f"{nm} #{wf_no}"
                self.hook.addMesh(mesh, color="#e0503c", opacity=0.75, mid=f"built{wf_no}", zoom=False)
        except Exception as e:
            self.plugin._msg(f"Could not load wireframes: {e}", Qgis.MessageLevel.Critical, 10)

    # ---------------------------------------------------------- drillholes
    def useDrillLayer(self, layer=None):
        """Use a line layer of drillhole intervals / traces (default: the active QGIS layer) for snapping,
        colours, tubes and labels."""
        layer = layer if layer is not None and not isinstance(layer, bool) else self.iface.activeLayer()
        if layer is None or not hasattr(layer, "geometryType") or \
                layer.geometryType() != Qgis.GeometryType.Line:
            self.plugin._msg("Select a line (LineStringZ) layer of drillhole intervals first.", Qgis.MessageLevel.Warning)
            return
        polys = layer_polylines(layer, self.canvas.mapSettings().destinationCrs())
        if not polys:
            self.plugin._msg("No line features in that layer.", Qgis.MessageLevel.Warning)
            return
        if not QgsWkbTypes.hasZ(layer.wkbType()):
            self.plugin._msg("Layer has no Z - drillholes will sit at RL 0.", Qgis.MessageLevel.Warning)
        in3d = layer.id() in {lid for lid, _n, _s in self._q2tEntries()}
        # a layer the 3D window already draws (and snaps to) needs no second copy; others get thin lines
        self.hook.setOverlay("drill", None if in3d else polyline_payload(polys, DRILL_COLOR, snap="ends", point_size=4))
        if self.drillLayerId != layer.id():
            getattr(self, "imports", {}).pop("drill", None)     # re-registered by the importers after this
        self.drillLayerId = layer.id()
        self.lblDrill.setText(f"{layer.name()}: {len(polys)} intervals - choose colours and labels, then Apply")
        self._fillDhFields(layer)
        self._fillWfList()
        self._fillDhLayers()

    def _dhLayerCandidates(self):
        """[(label, layerId)] - line layers in the 3D view, plus the drillhole layer in use."""
        out, ids = [], set()
        for lid, name, _shown in self._q2tEntries():
            l = QgsProject.instance().mapLayer(lid)
            if l is not None and hasattr(l, "geometryType") and l.geometryType() == Qgis.GeometryType.Line:
                node = QgsProject.instance().layerTreeRoot().findLayer(lid)
                grp = node.parent().name() if node is not None and node.parent() is not None else ""
                out.append((f"{name}  ·  {grp}" if grp else f"{name}  ·  3D layer", lid))
                ids.add(lid)
        cur = getattr(self, "drillLayerId", None)
        if cur and cur not in ids:
            l = QgsProject.instance().mapLayer(cur)
            if l is not None:
                out.append((l.name(), cur))
        return out

    def _fillDhLayers(self):
        cb = getattr(self, "cbDhLayer", None)
        if cb is None:
            return
        items = [("(none)", None)] + self._dhLayerCandidates()
        cb.blockSignals(True)
        if [(cb.itemText(i), cb.itemData(i)) for i in range(cb.count())] != items:
            cb.clear()
            for t, d in items:
                cb.addItem(t, d)
        cb.setCurrentIndex(max(0, cb.findData(getattr(self, "drillLayerId", None))))
        cb.blockSignals(False)

    def _dhLayerChosen(self, *_):
        lid = self.cbDhLayer.currentData()
        if lid is None:
            if getattr(self, "drillLayerId", None):
                self.clearDrill()
            return
        if lid == getattr(self, "drillLayerId", None):
            return
        lyr = QgsProject.instance().mapLayer(lid)
        if lyr is not None:
            self.useDrillLayer(lyr)

    def styleDrillLayer(self, lid):
        """Objects list ▸ Styles and labels: pick the layer here and bring the section into view."""
        self.showTab("Objects")
        lyr = QgsProject.instance().mapLayer(lid) if lid else None
        if lyr is not None and lid != getattr(self, "drillLayerId", None):
            self.useDrillLayer(lyr)
        self._fillDhLayers()
        try:
            self.sDh.btn.setChecked(True)
            from qgis.PyQt.QtWidgets import QScrollArea
            w = self.sDh.parentWidget()
            while w is not None and not isinstance(w, QScrollArea):
                w = w.parentWidget()
            if w is not None:
                QTimer.singleShot(0, lambda: w.ensureWidgetVisible(self.sDh))
        except (AttributeError, RuntimeError):
            pass

    def _fillDhFields(self, layer):
        from ..core.fieldnames import grade_fields
        names = [f.name() for f in layer.fields() if f.name().lower() != "fid" and not f.name().startswith("q2t4_")]
        cur = self.cbDhField.currentText()
        self.cbDhField.blockSignals(True)
        self.cbDhField.clear()
        self.cbDhField.addItem("(none)")
        self.cbDhField.addItems(names)
        g = grade_fields(names)
        self.cbDhField.setCurrentText(cur if cur in names else (g[0] if g else "(none)"))
        self.cbDhField.blockSignals(False)

    def drillDisplay(self):
        """Tubes coloured by the chosen field and / or hole ID and value labels for the drill layer."""
        import html as _H
        from ..core import tubes as TB
        layer = QgsProject.instance().mapLayer(self.drillLayerId) if getattr(self, "drillLayerId", None) else None
        if layer is None:
            self.plugin._msg("Pick the interval layer first (select it, then Use active layer).", Qgis.MessageLevel.Warning)
            return
        field = self.cbDhField.currentText()
        field = None if field in ("", "(none)") else field
        groups = []                              # which feature (interval) each segment belongs to
        ivs, hole_field = TB.layer_intervals(layer, self.canvas.mapSettings().destinationCrs(), field, groups=groups)
        legend_html = ""
        if self.cbDhTubes.isChecked() and ivs:
            legend, colour = TB.classes(field, [v for _a, _b, v, _h in ivs]) if field else ([], lambda v: "#c060ff")
            spec = TB.tube_spec([(a, b) for a, b, _v, _h in ivs], [colour(v) for _a, _b, v, _h in ivs],
                                self.spDhRad.value())
            self.hook.setTubes("drill", spec)
            legend_html = " ".join(f"<span style='color:{c}'>■</span> {_H.escape(str(l))}" for l, c in legend)
            # the snap lines stay (they snap) but in dark grey, so the tube colours read
            self.hook.setOverlay("drill", polyline_payload(layer_polylines(layer, self.canvas.mapSettings().destinationCrs()),
                                                           "#3a3a3a", snap="ends", point_size=0))
        else:
            self.hook.setTubes("drill", None)
            self.hook.setOverlay("drill", polyline_payload(layer_polylines(layer, self.canvas.mapSettings().destinationCrs()),
                                                           DRILL_COLOR, snap="ends", point_size=4))
        holes, grades = TB.label_specs(ivs, field if self.cbDhGrade.isChecked() else None, groups=groups)
        self.hook.setLabels("drillIds", {"items": holes, "size": 11} if self.cbDhIds.isChecked() and holes else None)
        self.hook.setLabels("drillVals", {"items": grades, "size": 10} if grades else None)
        self.lblDhLegend.setText((f"<b>{_H.escape(field)}</b>: " if field else "") + legend_html +
                                 ("" if hole_field or not self.cbDhIds.isChecked() else
                                  "<br>No hole ID field found in this layer."))

    def clearDrill(self):
        getattr(self, "imports", {}).pop("drill", None)
        self.hook.setOverlay("drill", None)
        self.hook.setTubes("drill", None)
        self.hook.setLabels("drillIds", None)
        self.hook.setLabels("drillVals", None)
        self.lblDhLegend.setText("")
        self.drillLayerId = None
        self.lblDrill.setText("Pick a drillhole layer - or click one in the list above")
        self._fillDhLayers()

    # ------------------------------------------------------------- cleanup
    def _projectCleared(self):
        """Another project is opening (or New project): what the 3D view and the panel hold belongs to
        the old one - its wireframes, block models, drillholes, section and strings GeoPackage. Let it
        go, so nothing is re-placed into the new project's CRS or digitised into the old file."""
        try:
            with self.hook.noOpen():
                for mid in list(self.hook.meshes):
                    self.hook.removeMesh(mid)
                for bid in list(self.hook.blockModels):
                    self.hook.removeBlocks(bid)
                for name in list(self.hook.overlays):
                    self.hook.setOverlay(name, None)
                for name in list(getattr(self.hook, "tubes", {})):
                    self.hook.setTubes(name, None)
                for name in list(getattr(self.hook, "labels", {})):
                    self.hook.setLabels(name, None)
            self._unwatchBmStyles()
            self.imports = {}
            self.bm, self.bmId = None, None
            self.drillLayerId = None
            self._q2tPinned = []
            self._hiddenStrings = set()
            self.baseSection = None
            self.offset = 0.0
            self.store.close()
            self.lblGpkg.setText("")
            if getattr(self, "_manualTris", None):
                log(f"{len(self._manualTris)} unsaved manual triangle(s) kept from the previous project - "
                    "Strings tab > Discard to drop them")
            self._lastProjectCrs = None          # the new project's CRS is taken as it is, nothing re-placed
            QTimer.singleShot(0, self._afterProjectSwitch)
        except RuntimeError:                     # panel being torn down with QGIS
            pass

    def _afterProjectSwitch(self):
        try:
            self._lastProjectCrs = QgsProject.instance().crs()
            self.cbBmField.clear()
            self._bmUpdateLabel()
            self._update()
            self._fillWfList()
            self._fillStringList()
            self._wfFileLabel()
        except Exception as e:
            log(f"Project switch: {e}")

    def cleanup(self):
        self.pollTimer.stop()
        try:
            QgsProject.instance().cleared.disconnect(self._projectCleared)
        except (TypeError, RuntimeError):
            pass
        self._unwatchBmStyles()
        try:
            QgsProject.instance().layersAdded.disconnect(self._onLayersAdded)
        except (TypeError, RuntimeError, AttributeError):
            pass
        try:
            QgsProject.instance().layerTreeRoot().visibilityChanged.disconnect(self._syncStrMap)
        except (TypeError, RuntimeError, AttributeError):
            pass
        self._dead = True
        try:
            QgsProject.instance().crsChanged.disconnect(self._projectCrsChanged)
        except TypeError:
            pass
        try:
            self.hook.stopDigitise()
        except Exception:
            pass
        try:
            self.canvas.mapToolSet.disconnect(self._mapToolChanged)
        except TypeError:
            pass
        if self.canvas.mapTool() is self.tool:
            self.canvas.unsetMapTool(self.tool)
        self.tool.cleanup()
        for rb in (self.rbSlab, self.rbLine, self.rbStrings):
            rb.reset()
            self.canvas.scene().removeItem(rb)


def QgsRubberBand_(canvas, gtype, fill, stroke, width):
    from qgis.gui import QgsRubberBand
    rb = QgsRubberBand(canvas, gtype)
    if fill is not None:
        rb.setFillColor(fill)
    rb.setStrokeColor(stroke)
    rb.setWidth(width)
    return rb
