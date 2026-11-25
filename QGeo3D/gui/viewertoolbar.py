# -*- coding: utf-8 -*-
"""Native toolbar and status bar for the Qgis2threejs 3D window.

Replaces the small in-view button bar with a proper Qt toolbar grouped as
View · Section · Tools · Display · Add, and adds permanent status-bar fields
(cursor E/N/RL, snap source, mode, GPU and triangle count).

The toolbar holds no state of its own: every control mirrors a widget in the
QGeo 3D panel so both stay in step.
"""

import contextlib
import os

from qgis.PyQt.QtCore import QSize, Qt
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import (QDoubleSpinBox, QLabel, QMenu, QToolBar, QToolButton)

from ..core.compat import QAction

ICON_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "icons")


def icon(name):
    if name.startswith("/"):                     # a QGIS theme icon
        from qgis.core import QgsApplication
        return QgsApplication.getThemeIcon(name)
    return QIcon(os.path.join(ICON_DIR, name + ".svg"))


class ViewerToolbar:
    """Builds and owns the toolbar + status widgets on one Q3DWindow."""

    def __init__(self, plugin, window):
        self.plugin = plugin
        self.dock = plugin.dock
        self.hook = plugin.hook
        self.w = window
        self._conns = []                # (signal, slot) on panel widgets, undone in remove()
        self._build()
        self._buildStatus()
        self._hookDrops()
        self.hook.setWebToolbarVisible(False)

    # ------------------------------------------------------------- toolbar
    def _act(self, name, text, slot=None, checkable=False, tip=None):
        a = QAction(icon(name), text, self.tb)
        a.setCheckable(checkable)
        a.setToolTip(tip or text)
        if slot is not None:
            (a.toggled if checkable else a.triggered).connect(slot)
        self.tb.addAction(a)
        return a

    def _label(self, text):
        lab = QLabel(text)
        lab.setStyleSheet("color:#6b7b88; font-size:10px; font-weight:600; padding:0 4px 0 8px;")
        self.tb.addWidget(lab)
        return lab

    def _build(self):
        d, h = self.dock, self.hook
        tb = self.tb = QToolBar("QGeo 3D", self.w)
        tb.setObjectName("QGeo3DToolbar")
        tb.setIconSize(QSize(20, 20))
        tb.setMovable(True)
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)

        # View
        self._label("VIEW")
        self._act("plan", "Plan view", lambda: h.setView("plan"), tip="Plan view: looking down, north up")
        for k, name in (("N", "north"), ("E", "east"), ("S", "south"), ("W", "west")):
            self._act("view" + k, f"Facing {name}", lambda _=False, k=k: h.setView(k),
                      tip=f"Facing {name} (keeps the orbit point and zoom)")
        self._act("recentre", "Recentre", lambda: h.recentre(), tip="Fit wireframes and orbit around their centre")
        self._act("/mActionPanToSelected.svg", "Centre on QGIS map", lambda: h.centreOnMap(),
                  tip="Centre the 3D view on the area of the QGIS map (the view direction is kept)")
        self._act("orbit", "Orbit point", h.startOrbitPick, tip="Click a surface to orbit around it")
        tb.addSeparator()

        # Section
        self._label("SECTION")
        self.aDraw = self._act("section", "Draw section on map", self._draw, checkable=True,
                               tip="Click A then B on the map (the 3D window minimises while you draw)")
        self.spWidth = QDoubleSpinBox()
        self.spWidth.setRange(0.1, 100000)
        self.spWidth.setDecimals(1)
        self.spWidth.setSuffix(" m")
        self.spWidth.setToolTip("Section width")
        self.spWidth.setValue(d.spWidth.value())
        self.spWidth.valueChanged.connect(d.spWidth.setValue)
        self._connect(d.spWidth.valueChanged, self._widthFromDock)
        tb.addWidget(self.spWidth)
        self.spStep = QDoubleSpinBox()
        self.spStep.setRange(0.0, 100000)
        self.spStep.setDecimals(1)
        self.spStep.setSuffix(" m")
        self.spStep.setSpecialValueText("step: auto")
        self.spStep.setPrefix("step ")
        self.spStep.setToolTip("Section step for ◀ / ▶. Auto (lowest value, type 0) = half the section width; "
                               "or type a distance")
        self.spStep.setValue(d.spStep.value())
        self.spStep.valueChanged.connect(d.spStep.setValue)
        self._connect(d.spStep.valueChanged, self._stepFromDock)
        tb.addWidget(self.spStep)
        self._act("back", "Step towards viewer", lambda: d.step(-1))
        self.lblOff = QLabel(" +0.0 m ")
        self.lblOff.setToolTip("Offset from the drawn section line")
        self.lblOff.setStyleSheet("font-family:Consolas,monospace; padding:0 2px;")
        tb.addWidget(self.lblOff)
        self._act("fwd", "Step away from viewer", lambda: d.step(+1))
        self._act("look", "Look along section", h.viewSection)
        self.aClip = self._act("clip", "Clip to section", d.cbClip.setChecked, checkable=True,
                               tip="Show only what lies inside the section slab")
        self.aClip.setChecked(d.cbClip.isChecked())
        self._connect(d.cbClip.toggled, lambda on: self._mirror(self.aClip, on))
        tb.addSeparator()

        # Tools
        self._label("TOOLS")
        self.aQuery = self._act("query", "Query", self._query, checkable=True,
                                tip="Click an object to see its attributes (Esc clears)")
        self.aString = self._act("string", "Digitise string", d.bDig.setChecked, checkable=True,
                                 tip="Digitise a string into the active string layer: click points in the 3D view · Backspace "
                                     "undoes a point · Enter / right-click / double-click finishes the string · Esc "
                                     "cancels it (twice: stop digitising)")
        self._connect(d.bDig.toggled, lambda on: self._mirror(self.aString, on))
        self._act("link", "Link selected", d.linkSelected,
                  tip="Link the strings selected in the list into the wireframe chosen in Into. Closed outlines are linked section by section; 2-point strings are tie lines. Ends stay open; use Close end.")
        self.aMeasure = self._act("measure", "Measure", d.setMeasure, checkable=True,
                                  tip="Click A and B: length, bearing, plunge; a third point C gives the dip and dip "
                                      "direction of the plane A B C. Snaps like digitising. Esc ends")
        self.aEdit = self._act("editv", "Edit vertices", d.setEditVertices, checkable=True,
                               tip="Drag vertices of the strings (and of the wireframes selected in Objects) in 3D; "
                                   "Delete removes the vertex under the cursor; saved to the strings GeoPackage")
        vb = QToolButton()
        vb.setIcon(icon("views"))
        vb.setToolTip("Saved views (camera and section)")
        vb.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        vm = QMenu(vb)
        vm.aboutToShow.connect(lambda: self._fillViewMenu(vm))
        vb.setMenu(vm)
        tb.addWidget(vb)
        tb.addSeparator()

        # Display
        self._label("DISPLAY")
        self.aEdges = self._act("edges", "Triangle edges", d.cbEdges.setChecked, checkable=True)
        self.aEdges.setChecked(d.cbEdges.isChecked())
        self._connect(d.cbEdges.toggled, lambda on: self._mirror(self.aEdges, on))
        self.aBlocks = self._act("blocks", "Block model", self._blocks, checkable=True,
                                 tip="Show / hide the loaded block model")
        self.aBlocks.setChecked(True)
        self.aGrid = self._act("grid", "Coordinate grid", h.setGrid, checkable=True,
                               tip="Coordinate grid while the view is aligned: plan (E/N), facing N/S (E/RL), "
                                   "facing E/W (N/RL) or along the section (chainage/RL)")
        self.aGrid.setChecked(h.gridOn)
        self.aScale = self._act("scalebar", "Scale bar", self._scaleBar, checkable=True,
                                tip="Scale bar with the CRS name and the view coordinate")
        self.aScale.setChecked(h.scaleOn)
        self.aCoord = self._act("viewcoord", "View coordinate", h.setViewCoord, checkable=True,
                                tip="Under the scale bar: the northing of the view when facing N/S, the easting "
                                    "facing E/W, the RL in plan")
        self.aCoord.setChecked(h.viewCoordOn)
        self.aCoord.setEnabled(h.scaleOn)
        self.aChip = self._act("chip", "Section label", h.setChip, checkable=True,
                               tip="Section label at the top left: azimuth, look direction, width, offset")
        self.aChip.setChecked(h.chipOn)
        self.aInset = self._act("inset", "Section window", lambda on: h.setInset(on=on), checkable=True,
                                tip="Plan window of the section line and slab (▾ bottom left / top left)")
        self.aInset.setChecked(h.insetOn)
        mi = QMenu(tb)
        self.aInsetBL = mi.addAction("Bottom left", lambda: self._insetPos("bl"))
        self.aInsetTL = mi.addAction("Top left", lambda: self._insetPos("tl"))
        for a in (self.aInsetBL, self.aInsetTL):
            a.setCheckable(True)
        (self.aInsetTL if h.insetPos == "tl" else self.aInsetBL).setChecked(True)
        self.aInset.setMenu(mi)
        btn = tb.widgetForAction(self.aInset)
        if btn is not None:
            btn.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        tb.addSeparator()

        # Add
        add = QToolButton()
        add.setIcon(icon("add"))
        add.setText("Add")
        add.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        add.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        m = QMenu(add)
        m.addAction("Wireframe from file (DXF / OBJ / GPKG / Micromine .tridb / Datamine / Surpac)…", self.plugin.addFromFile)
        m.addAction("Wireframe from active layer", self.plugin.addFromActiveLayer)
        m.addSeparator()
        m.addAction("Block model (Micromine .DAT / Datamine .dm / Vulcan .bmf / Surpac .mdl)…", d.loadBlockModelDialog)
        m.addAction("Datamine file (.dm: wireframe, block model or drillholes)…", d.openDatamineDialog)
        m.addAction("Surpac file (.str strings / .dtm surface or solid / .mdl block model)…", d.openSurpacDialog)
        m.addAction("Micromine file (.tridb wireframes / .STR strings / .DAT points, strings or block model / .DHDB)…",
                    d.openMicromineDialog)
        m.addAction("Open Mining Format (.omf) / Leapfrog mesh (.msh)…", d.openOmfDialog)
        m.addAction("Drillholes: active layer as snap targets", d.useDrillLayer)
        m.addAction("Drillholes: load Micromine database (.DHDB)…", d.loadDhdbDialog)
        m.addAction("Drillholes from CSV / Excel (collars, surveys, intervals)…", lambda: d.tableDrillholesDialog())
        m.addAction("Block model from CSV / Excel…", lambda: d.tableBlocksDialog())
        m.addSeparator()
        m.addAction("Saved wireframes from strings GeoPackage", d.showSavedWireframes)
        add.setMenu(m)
        add.setToolTip("Add data to the 3D view")
        tb.addWidget(add)
        self.aExport = self._act("export", "Export…", lambda: d.exportDialog(),
                                 tip="Export a wireframe, block model or GeoPackage / QGIS layer as Micromine, "
                                     "Datamine, Surpac, DXF, OBJ, CSV, Leapfrog or OMF")
        self._act("report", "Grade-tonnage report…", lambda: d.reportDialog(),
                  tip="Tonnes, grade and metal by cut-off and resource category (blocks shown, all, inside "
                      "wireframes or the section)")
        self._act("pdf", "Section plot (PDF)…", d.sectionPlotDialog,
                  tip="The 3D view with title block and legend on a print layout, saved as PDF")

        self.w.addToolBar(Qt.ToolBarArea.TopToolBarArea, tb)
        self.syncSection()

    def _scaleBar(self, on):
        self.hook.setScaleBar(on)
        if hasattr(self, "aCoord"):
            self.aCoord.setEnabled(on)      # the view coordinate lives inside the scale bar box

    def _insetPos(self, pos):
        self.aInsetBL.setChecked(pos == "bl")
        self.aInsetTL.setChecked(pos == "tl")
        self.hook.setInset(on=True, pos=pos)
        self._mirror(self.aInset, True)

    def _connect(self, sig, slot):
        sig.connect(slot)
        self._conns.append((sig, slot))

    def _mirror(self, action, on):
        try:
            if action.isChecked() != on:
                action.blockSignals(True)
                action.setChecked(on)
                action.blockSignals(False)
        except RuntimeError:            # toolbar already deleted with its window
            pass

    def _stepFromDock(self, v):
        if abs(self.spStep.value() - v) > 1e-9:
            self.spStep.blockSignals(True)
            self.spStep.setValue(v)
            self.spStep.blockSignals(False)

    def _widthFromDock(self, v):
        if abs(self.spWidth.value() - v) > 1e-9:
            self.spWidth.blockSignals(True)
            self.spWidth.setValue(v)
            self.spWidth.blockSignals(False)

    def _draw(self, on):
        self.dock.bDraw.setChecked(on)

    def _query(self, on):
        if on:
            self._mirror(self.aString, False)
        self.hook.setQueryMode(on)

    def _blocks(self, on):
        for bid in list(self.hook.blockModels):
            self.dock.setBlocksShown(bid, on)
        self.dock._fillWfList()

    def syncSection(self):
        """Called by the panel whenever the section changes."""
        d = self.dock
        self._mirror(self.aDraw, d.bDraw.isChecked())
        self.lblOff.setText(f" {d.offset:+.1f} m ")
        has = d.baseSection is not None
        for a in self.tb.actions():
            if a.text() in ("Step towards viewer", "Step away from viewer", "Look along section"):
                a.setEnabled(has)
        has_bm = bool(self.hook.blockModels)
        self.aBlocks.setEnabled(has_bm)
        if has_bm:
            self._mirror(self.aBlocks, any(b not in self.hook.hiddenMeshes for b in self.hook.blockModels))

    # ---------------------------------------------------------- status bar
    def _buildStatus(self):
        sb = self.w.statusBar()
        mono = "font-family:Consolas,monospace; padding:0 6px;"
        self.lblXYZ = QLabel("E —  N —  RL —")
        self.lblXYZ.setStyleSheet(mono)
        self.lblXYZ.setMinimumWidth(300)
        self.lblSrc = QLabel("")
        self.lblSrc.setStyleSheet("color:#6b7b88; padding:0 6px;")
        self.lblMode = QLabel("Navigate")
        self.lblMode.setStyleSheet("padding:0 6px; font-weight:600;")
        self.lblGpu = QLabel("")
        self.lblGpu.setStyleSheet("color:#6b7b88; padding:0 6px;")
        self.status = [self.lblXYZ, self.lblSrc, self.lblMode, self.lblGpu]
        for lab in self.status:
            sb.addPermanentWidget(lab)
        self._gpu = None

    def updateStatus(self, info):
        if not info:
            return
        c = info.get("c")
        if c:
            from qgis.core import QgsProject
            crs = QgsProject.instance().crs()
            self.lblXYZ.setText(f"E {c[0]:,.1f}  N {c[1]:,.1f}  RL {c[2]:,.1f}  ·  {crs.authid()}")
            self.lblXYZ.setToolTip(f"Cursor in the project coordinate system: {crs.description()}")
        src = info.get("src")
        self.lblSrc.setText({"section": "on section", "surface": "surface", "block": "block", "wireframe": "wireframe",
                             "line": "drillhole / line", "vertex": "snap: string vertex",
                             "drill": "snap: drillhole", "end": "snap: drillhole"}.get(src, f"snap: {src}" if src else ""))
        mode = info.get("mode") or "Navigate"
        self.lblMode.setText(mode)
        self._mirror(self.aQuery, mode == "Query")
        if self.hook.queryMode != (mode == "Query"):
            self.hook.queryMode = mode == "Query"
        if self._gpu is None:
            g = self.hook.gpuInfo()
            self._gpu = g.split(" · ")[0].replace("AMD ", "") + (" · WebGL 1" if "WebGL 1" in g else " · WebGL 2" if g else "") if g else ""
        tris = info.get("tris")
        self.lblGpu.setText(f"{self._gpu}  ·  {tris / 1000:,.0f}k tri" if tris is not None and self._gpu else self._gpu)

    def _fillViewMenu(self, m):
        d = self.dock
        m.clear()
        m.addAction("Save current view…", d.saveView)
        views = d._views()
        if views:
            m.addSeparator()
            for i, vw in enumerate(views):
                m.addAction(vw.get("name", f"View {i + 1}"), lambda i=i: d.goView(i))
            m.addSeparator()
            m.addAction("Previous view", lambda: d.stepView(-1))
            m.addAction("Next view", lambda: d.stepView(+1))

    # ------------------------------------------------------------- cleanup
    def _hookDrops(self):
        """Files dropped on the 3D view: ours are opened by the panel, the rest go to Qgis2threejs."""
        from .dropper import DropFilter
        self._dropView = None
        self._dropSig = None
        self._catcher = None
        view = getattr(self.w.ui, "webView", None)
        if view is not None:                         # Qgis2threejs 2.x: the web view is a widget here
            with contextlib.suppress(Exception):
                self._drop = DropFilter(self.dock, on_unknown=getattr(self.w, "fileDropped", None), parent=self.tb)
                view.installEventFilter(self._drop)
                self._dropView = view
            return
        # Qgis2threejs 3.x: the view (often in another process) reports drops through fileDropped(urls);
        # take ours and pass the rest on
        wv = getattr(self.w, "webView", None)
        sig = getattr(wv, "fileDropped", None)
        if sig is None:
            return
        try:
            sig.disconnect(self.w.fileDropped)
        except (TypeError, RuntimeError):
            pass
        sig.connect(self._viewDropped)
        self._dropSig = sig
        # ...but Qgis2threejs 3.3's separate view process never sends drops, so catch them ourselves
        cont = getattr(self.w.ui, "webViewContainer", None)
        if cont is not None:
            from .dropper import ViewDropCatcher
            try:
                self._catcher = ViewDropCatcher(self.w, cont, self._viewDropped)
            except Exception as e:
                from ..core.q2thook import log
                log(f"Drop catcher not available: {e}")

    def _viewDropped(self, urls):
        from .dropper import ALL, ext
        paths = []
        for u in urls:
            p = u.toLocalFile() if hasattr(u, "toLocalFile") else str(u)
            paths.append(p)
        ours = [p for p in paths if p and ext(p) in ALL]
        others = [u for u, p in zip(urls, paths) if not (p and ext(p) in ALL)]
        if others:
            self.w.fileDropped(others)
        if ours:
            self.dock.openDropped(ours)

    def remove(self):
        c = getattr(self, "_catcher", None)
        if c is not None:
            with contextlib.suppress(Exception):
                c.remove()
            self._catcher = None
        try:
            if getattr(self, "_dropView", None) is not None:
                self._dropView.removeEventFilter(self._drop)
            if getattr(self, "_dropSig", None) is not None:
                self._dropSig.disconnect(self._viewDropped)
                self._dropSig.connect(self.w.fileDropped)
        except (RuntimeError, TypeError):
            pass
        with contextlib.suppress(Exception):
            self.hook.setWebToolbarVisible(True)
        for sig, slot in self._conns:
            try:
                sig.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
        self._conns = []
        try:
            sb = self.w.statusBar()
            for lab in self.status:
                sb.removeWidget(lab)
                lab.deleteLater()
            self.w.removeToolBar(self.tb)
            self.tb.deleteLater()
        except RuntimeError:            # window already gone
            pass
