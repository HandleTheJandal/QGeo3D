# -*- coding: utf-8 -*-
"""QGeo 3D - geological strings & wireframes for the Qgis2threejs 3D view.

Companion plugin: hooks into a running Qgis2threejs exporter window rather than
forking it. Feature 1: wireframes with triangle edges. Feature 2: section slabs.
"""

import os

from qgis.PyQt.QtCore import QEvent, QObject, QSettings, Qt, QTimer
import qgis.utils
try:
    from qgis.PyQt import sip
except ImportError:                     # older PyQt builds
    import sip
from qgis.PyQt.QtWidgets import QComboBox, QFileDialog, QHBoxLayout, QLabel, QMenu, QToolButton, QWidget
from .core.compat import QAction
from qgis.core import Qgis, QgsProject, QgsWkbTypes

from .core import mesh as meshio
from .core.q2thook import Q3DHook, log
from .gui.dock import Q2T4Dock
from .gui.q2ttree import QgisTreeLayout
from .gui.viewertoolbar import ViewerToolbar
from qgis.PyQt.QtGui import QIcon as QIcon_

MENU = "&QGeo 3D"
SETTINGS_DIR = "/QGeo3D/lastDir"
SETTINGS_DOCK_IN_3D = "/QGeo3D/dockIn3DWindow"
SETTINGS_QGIS_TREE = "/QGeo3D/qgisLayerTree"
SETTINGS_VIEWER_STATE = "/QGeo3D/viewer/state"
SETTINGS_VIEWER_GEOMETRY = "/QGeo3D/viewer/geometry"


def _theme_icon(name):
    from qgis.core import QgsApplication
    return QgsApplication.getThemeIcon(name)


def q2t_icon():
    """Qgis2threejs' own icon (web/Qgis2threejs.png), found whether or not it has loaded yet."""
    import sys
    from qgis.PyQt.QtGui import QIcon
    dirs = []
    mod = sys.modules.get("Qgis2threejs")
    if mod is not None and getattr(mod, "__file__", None):
        dirs.append(os.path.dirname(mod.__file__))
    try:
        from qgis.utils import home_plugin_path, plugin_paths
        dirs += [os.path.join(p, "Qgis2threejs") for p in [home_plugin_path] + list(plugin_paths)]
    except Exception:
        pass
    for d in dirs:
        for rel in ("web/Qgis2threejs.png", "Qgis2threejs.png", "icon.png"):
            p = os.path.join(d, rel)
            if os.path.isfile(p):
                return QIcon(p)
    return QIcon()


class _CloseFilter(QObject):
    """Calls cb when the watched window is about to close."""

    def __init__(self, cb):
        super().__init__()
        self.cb = cb

    def eventFilter(self, obj, ev):
        if ev.type() == QEvent.Type.Close:
            self.cb()
        return False


def format_stats(s):
    parts = [f"{s['triangles']:,} triangles", f"{s['vertices']:,} vertices"]
    if s.get("closed"):
        parts.append(f"closed, volume {s['volume']:,.1f} m³")
        if s.get("inside_out"):
            parts.append("normals inward")
    elif "open_edges" in s:
        parts.append(f"OPEN ({s['open_edges']} open edges)")
    if s.get("nonmanifold_edges"):
        parts.append(f"{s['nonmanifold_edges']} non-manifold edges")
    if s.get("inconsistent_edges"):
        parts.append(f"{s['inconsistent_edges']} inconsistently wound edges")
    return ", ".join(parts)


class QGeo3DPlugin:

    def __init__(self, iface):
        self.iface = iface
        self.hook = Q3DHook()
        self.actions = []

    # ------------------------------------------------------------- gui
    def _report(self):
        from .gui.report import show_report
        self._reportDlg = show_report(self, include_error=True)

    def initGui(self):
        mw = self.iface.mainWindow()
        try:                                   # problem reports (QGeo 3D menu, and an offer after a QGeo 3D error)
            from .gui.report import ErrorWatch
            self.errorWatch = ErrorWatch(self)
            self.errorWatch.install()
        except Exception as e:
            self.errorWatch = None
            log(f"Error watch: {e}")
        try:
            from .core.crs import ensure_proj
            if not ensure_proj():
                log("GDAL's PROJ database doesn't match this PROJ version (mixed OSGeo4W install) - "
                    "QGeo 3D works around it, but QGIS itself may fall back to ballpark datum shifts. "
                    "Update the 'proj' and 'proj-data' packages with the OSGeo4W installer.")
        except Exception as e:
            log(f"PROJ check failed: {e}")
        try:
            from .core.crs import proj_db_problem
            msg = proj_db_problem()
            if msg:
                log(msg, Qgis.MessageLevel.Warning)
                QTimer.singleShot(4000, lambda m=msg: self._msg(m, Qgis.MessageLevel.Warning, 0))
        except Exception as e:
            log(f"PROJ database check failed: {e}")
        self.q2tWindow = None
        self.q2tMenu = None
        self.viewerToolbar = None
        self.treeLayout = None
        self._closeFilter = _CloseFilter(self._onQ2TClose)

        def act(text, slot, checkable=False, checked=False):
            a = QAction(text, mw)
            a.setCheckable(checkable)
            if checkable:
                a.setChecked(checked)
                a.toggled.connect(slot)
            else:
                a.triggered.connect(slot)
            self.iface.addPluginToMenu(MENU, a)
            self.actions.append(a)
            return a

        # dock panel (main UI)
        self.dock = None
        self.actPanel = None
        self._dropHandler = None
        self._makeDock()
        self.actions.append(self.actPanel)

        def sep():
            a = QAction(mw)
            a.setSeparator(True)
            self.iface.addPluginToMenu(MENU, a)
            self.actions.append(a)

        sep()
        # Add > : the same list as the panel's Add button (wireframes, Micromine / Datamine / Surpac /
        # OMF files, block models incl. GeoPackage, drillholes, saved wireframes), rebuilt each time
        self.actAdd = QAction(_theme_icon("/mActionAdd.svg"), "Add to 3D view", mw)
        self._addSubMenu = QMenu(mw)
        self._addSubMenu.aboutToShow.connect(self._fillAddMenu)
        self.actAdd.setMenu(self._addSubMenu)
        self.iface.addPluginToMenu(MENU, self.actAdd)
        self.actions.append(self.actAdd)
        self.actExport = act("Export… (Micromine / Datamine / Surpac / Leapfrog / DXF / OBJ / CSV)",
                             lambda: self._ensureDock().exportDialog())
        self.actExport.setIcon(_theme_icon("/mActionFileSaveAs.svg"))
        sep()
        self.actRecentre = act("Recentre 3D view (orbit around wireframes)", lambda: self.hook.recentre())
        self.actCentre = act("Centre 3D view on the QGIS map (view direction kept)", lambda: self.hook.centreOnMap())
        self.actOrbit = act("Set orbit point (click in 3D view)", self.hook.startOrbitPick)
        self.actClear = act("Clear wireframes from the 3D view", self.hook.clear)
        sep()
        self.actProtect = act("Protected folders (never written to)...", self.editProtected)
        self.actReport = act("Report a problem…", lambda: self._report())
        from .core import gpu as _gpu
        self.actGpu = act("Automatic GPU settings", self._gpuToggled, checkable=True,
                          checked=QSettings().value(_gpu.KEY_AUTO, True, type=bool))
        self.actGpu.setToolTip("Use the high-performance graphics card for QGIS (laptops with two GPUs) and set "
                               "the 3D view's drawing resolution for the card it runs on")
        self.hook.gpuWarning.connect(lambda m: self._msg(m, Qgis.MessageLevel.Warning, 15))
        if self.actGpu.isChecked():
            QTimer.singleShot(2000, self._gpuPrefer)

        # one toolbar dropdown
        self.toolButton = QToolButton(mw)
        self.toolButton.setText("QGeo 3D")
        from qgis.PyQt.QtGui import QIcon as _QIcon
        self.icon = _QIcon(os.path.join(os.path.dirname(__file__), "icon.svg"))
        self.toolButton.setIcon(self.icon)
        self.toolButton.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.toolButton.setToolTip("QGeo 3D")
        self.toolButton.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        m = QMenu(self.toolButton)
        for a in self.actions:
            m.addAction(a)
        self.toolButton.setMenu(m)
        self.toolbarAction = self.iface.addToolBarWidget(self.toolButton)

        # "3D viewer" button right next to it: open the Qgis2threejs window or bring it to the front
        import sys
        from qgis.PyQt.QtGui import QIcon
        self.actViewer = QAction(q2t_icon(), "3D viewer", mw)
        self.actViewer.setToolTip("Open the Qgis2threejs 3D viewer, or bring it to the front")
        self.actViewer.triggered.connect(self.showViewer)
        self.iface.addToolBarIcon(self.actViewer)
        m.insertAction(m.actions()[0] if m.actions() else None, self.actViewer)
        self.iface.addPluginToMenu(MENU, self.actViewer)
        # our 3D viewer button replaces Qgis2threejs' own toolbar button (its Web menu items stay);
        # Qgis2threejs may load after us, so try again once QGIS has finished starting
        self._q2tButtonHidden = None
        self._hideQ2TButton()
        try:
            self.iface.initializationCompleted.connect(self._hideQ2TButton)
            self.iface.initializationCompleted.connect(self._hidePanelInQgis)
        except Exception:
            pass
        QTimer.singleShot(3000, self._hideQ2TButton)
        QTimer.singleShot(3000, self._hidePanelInQgis)

        # menu + panel inside the Qgis2threejs window
        self.actDockIn3D = QAction("Dock panel in 3D window", mw)
        self.actDockIn3D.setCheckable(True)
        self.actDockIn3D.setChecked(QSettings().value(SETTINGS_DOCK_IN_3D, True, type=bool))
        self.actDockIn3D.toggled.connect(self._dockIn3DToggled)
        self.actQgisTree = QAction("Layer list matches QGIS Layers panel", mw)
        self.actQgisTree.setCheckable(True)
        self.actQgisTree.setChecked(QSettings().value(SETTINGS_QGIS_TREE, True, type=bool))
        self.actQgisTree.toggled.connect(self._qgisTreeToggled)
        self.treeLayout = None
        self.hook.windowConnected.connect(self._onQ2TWindow)

        # if a Q2T window is already open (e.g. plugin reload), re-attach to it
        try:
            if self.hook.window() is not None:
                self.hook.inject()
        except Exception:
            pass

    def _fillAddMenu(self):
        """Plugins / toolbar menu > Add: a fresh copy of the panel's Add menu."""
        m = self._addSubMenu
        m.clear()
        try:
            src = self._ensureDock()._addMenu()
        except Exception as e:
            log(f"Add menu: {e}")
            return
        self._addSrc = src                  # keeps the actions alive while they're listed
        for a in src.actions():
            m.addAction(a)

    # ------------------------------------------------- Qgis2threejs window
    def _makeDock(self):
        """Create the panel (again, if Qt destroyed it), its menu entry and its drop handling."""
        from .gui.dropper import DropFilter, MiningDropHandler
        old_act = self.actPanel
        self.dock = Q2T4Dock(self)
        # drag and drop: the panel takes every format we read; QGIS's window takes the mining-only ones
        self.dock.setAcceptDrops(True)
        self._dockDrop = DropFilter(self.dock)
        self.dock.installEventFilter(self._dockDrop)
        if self._dropHandler is not None:
            try:
                self.iface.unregisterCustomDropHandler(self._dropHandler)
            except Exception:
                pass
        self._dropHandler = MiningDropHandler(self.dock)
        self.iface.registerCustomDropHandler(self._dropHandler)
        self.iface.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.dock)
        self.dock.hide()                 # never open in the QGIS window unless asked for (menu entry)
        self.actPanel = self.dock.toggleViewAction()
        self.actPanel.triggered.connect(self._panelAsked)
        self.actPanel.setText("QGeo 3D panel")
        self.actPanel.setIcon(QIcon_(os.path.join(os.path.dirname(__file__), "icon.svg")))
        self.iface.addPluginToMenu(MENU, self.actPanel)
        if old_act is not None:                      # replace the dead entry everywhere it was listed
            self.actions = [self.actPanel if a is old_act else a for a in self.actions]
            try:
                m = self.toolButton.menu()
                m.insertAction(m.actions()[0] if m.actions() else None, self.actPanel)
            except Exception:
                pass

    def _gpuPrefer(self):
        from .core import gpu
        try:
            changed, msg = gpu.prefer_high_performance()
        except Exception as e:
            log(f"GPU preference not set: {e}")
            return
        if msg:
            log(msg + " (" + ", ".join(changed) + ")")
            self._msg(msg, Qgis.MessageLevel.Info, 12)

    def _gpuToggled(self, on):
        from .core import gpu
        QSettings().setValue(gpu.KEY_AUTO, bool(on))
        try:
            if on:
                self._gpuPrefer()
            else:
                gpu.undo_preferences()
        except Exception as e:
            log(f"GPU settings: {e}")
        if self.hook.window() is not None:
            self.hook.tuneGpu()

    def editProtected(self):
        from .gui.protectdialog import ProtectedFoldersDialog
        ProtectedFoldersDialog(self.iface.mainWindow()).exec()

    def _panelAsked(self, on):
        """The user ticked / unticked the panel entry: only then may it show in the QGIS window."""
        self._panelWanted = bool(on)

    def _hidePanelInQgis(self):
        """QGIS restores dock widgets it saw open last session; keep ours closed in the QGIS
        window unless the user asked for it this session."""
        try:
            if self.dock is None or sip.isdeleted(self.dock):
                return
            if self.dock.parent() is self.iface.mainWindow() and not getattr(self, "_panelWanted", False):
                self.dock.hide()
        except Exception:
            pass

    def _ensureDock(self):
        """The panel can die with a 3D window it was docked in: rebuild it rather than leave the plugin broken."""
        try:
            alive = self.dock is not None and not sip.isdeleted(self.dock)
        except Exception:
            alive = False
        if not alive:
            log("QGeo 3D panel was destroyed - recreating it")
            self._makeDock()
            self.actions = [a for a in self.actions if not sip.isdeleted(a)]
        return self.dock

    def showViewer(self):
        self._ensureDock()
        try:
            w = self.hook.window(open_if_needed=True)
        except Exception as e:
            self._msg(str(e), Qgis.MessageLevel.Critical, 10)
            return
        if w is None:
            return
        if w.isMinimized():
            if self.dock is not None and getattr(self.dock, "_3dState", None) is not None:
                self.dock._raise3DWindow()          # restore the state it had before Draw on map
            else:
                # keep maximised/full-screen: Qt keeps those flags alongside Minimized
                w.setWindowState((w.windowState() & ~Qt.WindowState.WindowMinimized) | Qt.WindowState.WindowActive)
        w.show()
        w.raise_()
        w.activateWindow()

    def _orbitControls(self, w):
        """Qgis2threejs 3.x starts with Map controls (left drag pans, right drag rotates) - the
        opposite of 2.x / QGIS 3 (left rotates, right pans). Use Orbit controls unless the user picked
        a scheme in View > Controls, which is remembered (QGeo 3D setting /QGeo3D/controls)."""
        ui = getattr(w, "ui", None)
        if ui is None or not hasattr(ui, "actionOrbitControls") or not hasattr(w.settings, "controls"):
            return                                   # Qgis2threejs 2.x: Orbit already
        s = QSettings()
        if not getattr(w, "_q2t4CtrlHooked", False):
            def picked(action):
                QSettings().setValue("/QGeo3D/controls", "Orbit" if action is ui.actionOrbitControls else "Map")
            try:
                ui.actionGroupControls.triggered.connect(picked)
                w._q2t4CtrlHooked = True
            except Exception:
                pass
        want = s.value("/QGeo3D/controls", "Orbit") or "Orbit"
        s.setValue("/Qgis2threejs/lastControls", want)      # Qgis2threejs' own default for new scenes
        if w.settings.controls() == want:
            (ui.actionOrbitControls if want == "Orbit" else ui.actionMapControls).setChecked(True)
            return
        w.settings.setControls(want)
        (ui.actionOrbitControls if want == "Orbit" else ui.actionMapControls).setChecked(True)
        try:
            if self.hook.sceneReady():               # already built with the other controls: rebuild
                w.controller.taskManager.addReloadPageTask()
        except Exception as e:
            log(f"Controls: {e}")

    def _onQ2TWindow(self, w):
        """A Qgis2threejs window was hooked: give it our menu and (optionally) the panel."""
        self._ensureDock()
        if self.hook._closing(w):
            return                          # never decorate (or dock the panel into) a closing window
        self.q2tWindow = w
        self._orbitControls(w)
        menu = QMenu("QGeo &3D", w)
        for a in self.actions:
            if not sip.isdeleted(a):
                menu.addAction(a)
        menu.addSeparator()
        menu.addAction(self.actDockIn3D)
        menu.addAction(self.actQgisTree)
        w.menuBar().addMenu(menu)
        self.q2tMenu = menu
        w.installEventFilter(self._closeFilter)
        if self.actDockIn3D.isChecked():
            self._dockToQ2T()
        try:
            self.viewerToolbar = ViewerToolbar(self, w)
        except Exception as e:          # fall back to the in-view button bar
            self.viewerToolbar = None
            self.hook.setWebToolbarVisible(True)
            log(f"Could not add the 3D window toolbar: {e}")
        self.treeLayout = QgisTreeLayout(w)
        self._addArrangeCombo(w)
        if self.actQgisTree.isChecked():
            try:
                self.treeLayout.apply()
            except Exception as e:      # never break the 3D window over a layer-list nicety
                log(f"Could not mirror the QGIS layer tree: {e}")
        try:
            from .gui.blocktree import BlockLayerTree
            self.blockTree = BlockLayerTree(self, w)
            self.blockTree.apply()
        except Exception as e:
            self.blockTree = None
            log(f"Block layers in the 3D layer list: {e}")
        try:
            from .gui.cursorfix import CursorFix
            old = getattr(self, "cursorFix", None)
            if old is not None:
                old.detach()
            self.cursorFix = CursorFix(w)
            QTimer.singleShot(2000, self.cursorFix.attach)       # docks / toolbars added after this
        except Exception as e:
            log(f"Cursor fix: {e}")
        self._restoreLayout(w)
        if self.hook.sceneReady():          # window was already loaded: add our JS + data now
            self.hook.refresh()
        QTimer.singleShot(1500, self.dock.autoShowSaved)     # saved wireframes back in the new view

    # ------------------------------------------------------------ 3D window layout
    LAYOUT_VERSION = 4103            # QMainWindow.saveState version tag for our layout

    def _saveLayout(self, w):
        """Toolbars, docks (sizes, sides, floating), window size and the panel's own state."""
        try:
            if self._badMaximised(w):
                return                       # don't keep proportions from a squashed window
            s = QSettings()
            s.setValue(SETTINGS_VIEWER_STATE, w.saveState(self.LAYOUT_VERSION))
            s.setValue(SETTINGS_VIEWER_GEOMETRY, w.saveGeometry())
            if self.dock is not None and self.dock.parent() is w:
                self.dock.saveUiState()
        except Exception as e:
            log(f"Could not save the 3D window layout: {e}")

    def _restoreLayout(self, w):
        s = QSettings()
        st = s.value(SETTINGS_VIEWER_STATE)
        if st:
            try:
                w.restoreState(st, self.LAYOUT_VERSION)       # puts our panel and toolbar back too
            except Exception as e:
                log(f"Could not restore the 3D window layout: {e}")
        if self.dock is not None:
            try:
                self.dock.restoreUiState()
            except Exception as e:
                log(f"Could not restore the panel state: {e}")
        # Qt 6 on Windows can bring a window back flagged maximised but at its old normal size
        # (it then fills only part of the screen): re-maximise it once it is on screen
        w._q3dSettled = False
        for ms in (300, 1500, 4000, 10000):
            QTimer.singleShot(ms, lambda: self._settleLayout(w))

    @staticmethod
    def _badMaximised(w):
        if not (w.windowState() & Qt.WindowState.WindowMaximized):
            return False
        avail = w.screen().availableGeometry()
        g = w.frameGeometry()
        return g.width() < avail.width() * 0.9 or g.height() < avail.height() * 0.9

    def _settleLayout(self, w):
        """Once the window is on screen at its real size: re-apply the saved dock layout (dock sizes
        are proportional to the window size at the time they are restored) and keep it readable."""
        try:
            if not w.isVisible():
                return
            if self._badMaximised(w):
                w.showNormal()
                w.showMaximized()
                log("3D window was maximised at the wrong size - re-maximised")
                QTimer.singleShot(200, lambda: self._settleLayout(w))
                return
            if getattr(w, "_q3dSettled", True):
                return
            w._q3dSettled = True
            st = QSettings().value(SETTINGS_VIEWER_STATE)
            if st:
                w.restoreState(st, self.LAYOUT_VERSION)
            self._readableDocks(w)
        except RuntimeError:
            pass

    @staticmethod
    def _readableDocks(w):
        """Qgis2threejs' Layers list taller than its Animation panel, and a left column wide enough
        to read layer names."""
        from qgis.PyQt.QtWidgets import QDockWidget
        lay = w.findChild(QDockWidget, "dockWidgetLayers")
        ani = w.findChild(QDockWidget, "dockWidgetAnimation")
        left = [d for d in (lay, ani) if d is not None and d.isVisible() and not d.isFloating()
                and w.dockWidgetArea(d) == Qt.DockWidgetArea.LeftDockWidgetArea]
        if lay in left and ani in left and lay.height() < ani.height():
            tot = lay.height() + ani.height()
            w.resizeDocks([lay, ani], [int(tot * 0.7), tot - int(tot * 0.7)], Qt.Orientation.Vertical)
        if left and min(d.width() for d in left) < 230:
            w.resizeDocks(left, [270] * len(left), Qt.Orientation.Horizontal)

    def _q2tAlive(self):
        w = self.q2tWindow
        if w is None:
            return None
        try:
            w.objectName()                  # raises if the C++ window is gone
        except RuntimeError:
            self.q2tWindow = self.q2tMenu = self.viewerToolbar = None
            return None
        return w

    def _dockToQ2T(self):
        w = self._q2tAlive()
        if w is None or self.dock is None or self.dock.parent() is w:
            return
        self.iface.removeDockWidget(self.dock)
        w.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.dock)
        self.dock.show()

    def _onQ2TClose(self):
        """The 3D window is closing: keep the panel, drop the toolbar (it dies with the window)."""
        w = self._q2tAlive()
        if w is not None:
            self._saveLayout(w)              # before the panel leaves the window
        self._dockToQgis()
        if self.viewerToolbar is not None:
            try:
                self.viewerToolbar.remove()
            except Exception:
                pass
            self.viewerToolbar = None

    def _dockToQgis(self):
        """Bring the panel back to the QGIS window (also called just before the 3D window closes,
        otherwise the panel would be destroyed with it)."""
        w = self._q2tAlive()
        if self.dock is None or w is None or self.dock.parent() is not w:
            return
        w.removeDockWidget(self.dock)
        self.iface.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.dock)
        self.dock.setVisible(False)          # back in the QGIS window: closed until asked for
        self._panelWanted = False

    def _addArrangeCombo(self, w):
        """'Arrange: Default / QGIS layers panel' selector above the 3D window's layer list."""
        tv = w.ui.treeView
        lay = tv.parentWidget().layout()
        row = QWidget()
        row.setObjectName("q2t4ArrangeRow")
        h = QHBoxLayout(row)
        h.setContentsMargins(2, 2, 2, 2)
        h.addWidget(QLabel("Arrange:"))
        cb = QComboBox()
        cb.addItem("Default (by type)", False)
        cb.addItem("QGIS Layers panel", True)
        cb.setCurrentIndex(1 if self.actQgisTree.isChecked() else 0)
        cb.setToolTip("Default: Qgis2threejs groups layers by type (DEM, Point, Line, Polygon).\n"
                      "QGIS Layers panel: same folders and order as the QGIS Layers panel.")
        cb.currentIndexChanged.connect(lambda i: self.actQgisTree.setChecked(bool(cb.itemData(i))))
        h.addWidget(cb, 1)
        lay.insertWidget(0, row)
        self.arrangeRow, self.arrangeCombo = row, cb

    def _removeArrangeCombo(self):
        row = getattr(self, "arrangeRow", None)
        self.arrangeRow = self.arrangeCombo = None
        if row is not None:
            try:
                row.setParent(None)
                row.deleteLater()
            except RuntimeError:
                pass

    def _qgisTreeToggled(self, on):
        QSettings().setValue(SETTINGS_QGIS_TREE, bool(on))
        cb = getattr(self, "arrangeCombo", None)
        if cb is not None:
            try:
                cb.blockSignals(True)
                cb.setCurrentIndex(1 if on else 0)
                cb.blockSignals(False)
            except RuntimeError:
                pass
        if self._q2tAlive() is None or self.treeLayout is None:
            return
        if on:
            self.treeLayout.apply()
        else:
            self.treeLayout.restore()

    def _dockIn3DToggled(self, on):
        QSettings().setValue(SETTINGS_DOCK_IN_3D, bool(on))
        if on:
            self._dockToQ2T()
        else:
            self._dockToQgis()
            self.dock.show()                 # asked for in the QGIS window
            self._panelWanted = True

    def unload(self):
        ew = getattr(self, "errorWatch", None)
        if ew is not None:
            ew.remove()
            self.errorWatch = None
        try:
            w = self._q2tAlive()
            if w is not None and self.dock is not None and not sip.isdeleted(self.dock):
                self._saveLayout(w)
        except Exception:
            pass
        try:
            if self.dock is not None and sip.isdeleted(self.dock):
                self.dock = None            # destroyed with a 3D window: nothing left to tidy
        except Exception:
            self.dock = None
        try:
            self.iface.unregisterCustomDropHandler(self._dropHandler)
            if self.dock is not None:
                self.dock.removeEventFilter(self._dockDrop)
        except Exception:
            pass
        try:
            self.hook.windowConnected.disconnect(self._onQ2TWindow)
        except TypeError:
            pass
        self._dockToQgis()
        w = self._q2tAlive()
        if w is not None and self.treeLayout is not None:
            try:
                self.treeLayout.restore()
            except Exception:
                pass
        self.treeLayout = None
        cf = getattr(self, "cursorFix", None)
        if cf is not None:
            try:
                cf.detach()
            except Exception:
                pass
        self.cursorFix = None
        bt = getattr(self, "blockTree", None)
        if bt is not None and w is not None:
            try:
                bt.restore()
            except Exception:
                pass
        self.blockTree = None
        self._removeArrangeCombo()
        if self.viewerToolbar is not None and w is not None:
            try:
                self.viewerToolbar.remove()
            except RuntimeError:
                pass
        self.viewerToolbar = None
        if w is not None:
            w.removeEventFilter(self._closeFilter)
            if self.q2tMenu is not None and not sip.isdeleted(self.q2tMenu):
                w.menuBar().removeAction(self.q2tMenu.menuAction())
                self.q2tMenu.deleteLater()
        self.q2tWindow = self.q2tMenu = None
        if getattr(self, "dock", None) is not None:
            self.dock.cleanup()             # needs the hook (stops digitise mode)
        self.hook.unload()
        if getattr(self, "dock", None) is not None:
            self.iface.removeDockWidget(self.dock)
            self.dock.deleteLater()
            self.dock = None
        for a in self.actions:
            if not sip.isdeleted(a):
                self.iface.removePluginMenu(MENU, a)
        self.iface.removeToolBarIcon(self.toolbarAction)
        try:
            self.iface.initializationCompleted.disconnect(self._hideQ2TButton)
            self.iface.initializationCompleted.disconnect(self._hidePanelInQgis)
        except Exception:
            pass
        self._restoreQ2TButton()
        if getattr(self, "actViewer", None) is not None:
            self.iface.removeToolBarIcon(self.actViewer)
            self.iface.removePluginMenu(MENU, self.actViewer)
        self.actions = []

    # ------------------------------------------- Qgis2threejs' own button
    def _hideQ2TButton(self):
        """Remove Qgis2threejs' 'Qgis2threejs Exporter' toolbar button while QGeo 3D is loaded."""
        q = qgis.utils.plugins.get("Qgis2threejs")
        a = getattr(q, "action", None) if q is not None else None
        if a is None or a is self._q2tButtonHidden:
            return
        try:
            self.iface.removeWebToolBarIcon(a)
            self._q2tButtonHidden = a
        except Exception:
            pass

    def _restoreQ2TButton(self):
        a = getattr(self, "_q2tButtonHidden", None)
        self._q2tButtonHidden = None
        q = qgis.utils.plugins.get("Qgis2threejs")
        if a is None or q is None or getattr(q, "action", None) is not a:
            return
        try:
            self.iface.addWebToolBarIcon(a)
        except RuntimeError:            # Qgis2threejs already unloaded
            pass

    # --------------------------------------------------------- helpers
    def _msg(self, text, level=Qgis.MessageLevel.Info, duration=6):
        self.iface.messageBar().pushMessage("QGeo 3D", text, level, duration)
        log(text, level)

    def addMesh(self, mesh, **style):
        """Public entry point (also used for scripted tests)."""
        stats = mesh.stats()
        mid = self.hook.addMesh(mesh, **style)
        text = f"{mesh.name}: {format_stats(stats)}"
        if mesh.warnings:
            text += " | " + "; ".join(mesh.warnings)
        self._msg(text, Qgis.MessageLevel.Warning if mesh.warnings else Qgis.MessageLevel.Success)
        return mid, stats

    def addFile(self, path, crs=None, **style):
        """Wireframe/surface from a file. Files that don't record a CRS (DXF, OBJ, Datamine, Surpac)
        ask for it (unless `crs` is given); the mesh is transformed into the project CRS."""
        from .core.crs import transform_xyz
        if path.lower().endswith(".tridb") and self.dock is not None:     # may hold many wireframes
            return self.dock.loadTridb(path, crs)
        if path.lower().endswith(".omf") and self.dock is not None:       # surfaces, lines, points, blocks
            return self.dock.openOmf(path, crs)
        mesh = meshio.read_file(path)
        if crs is None:
            from .gui.crsdialog import ask_source_crs
            crs = ask_source_crs(self.iface.mainWindow(), path, mesh.vertices)
            if crs is None:
                self._msg(f"{os.path.basename(path)}: import cancelled", Qgis.MessageLevel.Info)
                return None
        mesh.src_vertices = mesh.vertices.copy()
        mesh.src_crs = crs
        mesh.vertices = transform_xyz(mesh.src_vertices, crs, QgsProject.instance().crs())
        res = self.addMesh(mesh, **style)
        if self.dock is not None and res:
            self.dock.registerImport(res[0], "mesh", path, crs)
        return res

    def addLayer(self, layer, selected_only=False, **style):
        crs = QgsProject.instance().crs()
        return self.addMesh(meshio.read_qgis_layer(layer, crs, selected_only), **style)

    # --------------------------------------------------------- actions
    def addFromFile(self):
        s = QSettings()
        path, _ = QFileDialog.getOpenFileName(
            self.iface.mainWindow(), "Add wireframe", s.value(SETTINGS_DIR, ""),
            "Wireframes (*.dxf *.DXF *.obj *.gpkg *.shp *.dm *.DM *.dtm *.DTM *.tridb *.TRIDB *.msh *.omf);;Leapfrog mesh (*.msh);;Open Mining Format (*.omf);;Micromine wireframes (*.tridb *.TRIDB);;Surpac DTM (*.dtm *.DTM);;Datamine wireframe (*pt.dm *tr.dm *PT.dm *TR.dm);;All files (*)")
        if not path:
            return
        s.setValue(SETTINGS_DIR, os.path.dirname(path))
        try:
            self.addFile(path)
        except Exception as e:      # surface any reader/bridge error to the user
            self._msg(f"Failed to load {os.path.basename(path)}: {e}", Qgis.MessageLevel.Critical, 10)

    def addFromActiveLayer(self):
        layer = self.iface.activeLayer()
        if layer is None or layer.type() != Qgis.LayerType.Vector or \
                layer.geometryType() != Qgis.GeometryType.Polygon:
            self._msg("Select a polygon (PolygonZ / TIN / PolyhedralSurface) layer first.", Qgis.MessageLevel.Warning)
            return
        try:
            self.addLayer(layer, selected_only=layer.selectedFeatureCount() > 0)
        except Exception as e:
            self._msg(f"Failed to load {layer.name()}: {e}", Qgis.MessageLevel.Critical, 10)
