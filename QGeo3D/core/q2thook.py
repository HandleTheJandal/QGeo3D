# -*- coding: utf-8 -*-
"""Bridge between QGeo 3D and a running Qgis2threejs exporter window.

No Qgis2threejs code is modified. We:
  * find the live Q3DWindow via qgis.utils.plugins['Qgis2threejs'].liveExporter
  * inject web/q2t4.js into its preview page (WebEngine or WebKit)
  * push meshes as base64 Float32/Uint32 payloads
  * re-inject + re-push after every sceneLoaded (page reloads wipe JS state)
"""

import base64
import json
import os
from collections import OrderedDict

from qgis.PyQt.QtCore import QObject, pyqtSignal
from qgis.core import Qgis, QgsMessageLog

LOG_TAG = "QGeo 3D"
JS_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "web", "q2t4.js")

PALETTE = ["#d9a441", "#4f9bd9", "#d9584f", "#5fb36b", "#a779d1", "#d98ad0", "#7fc7c2"]

# ---------------------------------------------------------------- ringfence
# Folders this plugin (and the Qgis2threejs window it hooks) must never write to.
# Kept per user in QSettings "/QGeo3D/protectedRoots" (list of paths), edited from
# QGeo 3D > Protected folders... Empty by default: no paths are written into the plugin.
PROTECTED_KEY = "/QGeo3D/protectedRoots"
OLD_PROTECTED_KEY = "/QGIS2Three4/protectedRoots"     # before the rename - still honoured
DEFAULT_PROTECTED = []
SANDBOX_KEY = "/QGeo3D/sandboxDir"


def migrate_settings():
    """Copy settings saved under the plugin's old name (QGIS2Three4) to QGeo3D, once.
    Old values are left in place; keys already set under the new name win."""
    from qgis.PyQt.QtCore import QSettings
    s = QSettings()
    if s.value("/QGeo3D/migratedFromQGIS2Three4", False) in (True, "true"):
        return 0
    s.beginGroup("QGIS2Three4")
    old = {k: s.value(k) for k in s.allKeys()}
    s.endGroup()
    n = 0
    for k, v in old.items():
        if s.value(f"/QGeo3D/{k}") is None:
            s.setValue(f"/QGeo3D/{k}", v)
            n += 1
    s.setValue("/QGeo3D/migratedFromQGIS2Three4", True)
    return n


def _norm(p):
    return os.path.normcase(os.path.abspath(p)).replace("\\", "/").rstrip("/")


def protected_roots(raw=False):
    """Protected folders, normalised for comparison (raw=True: as the user entered them)."""
    from qgis.PyQt.QtCore import QSettings
    out = []
    for key in (PROTECTED_KEY, OLD_PROTECTED_KEY):
        v = QSettings().value(key, DEFAULT_PROTECTED if key == PROTECTED_KEY else [])
        if isinstance(v, str):
            v = [v]
        for p in (v or []):
            q = p if raw else _norm(p)
            if p and q not in out:
                out.append(q)
        if key == PROTECTED_KEY and QSettings().contains(PROTECTED_KEY):
            break      # edited under the new name: that list is the user's current choice
    return out


def set_protected_roots(paths):
    from qgis.PyQt.QtCore import QSettings
    QSettings().setValue(PROTECTED_KEY, [p for p in paths if p])


def is_protected(path):
    if not path:
        return False
    p = _norm(path)
    return any(p == r or p.startswith(r + "/") for r in protected_roots())


def sandbox_dir():
    """Writable scratch folder: <repo>/sandbox in a development checkout, else <QGIS profile>/QGeo3D/sandbox."""
    from qgis.PyQt.QtCore import QSettings
    d = QSettings().value(SANDBOX_KEY, "")
    if not d:
        pkg = os.path.realpath(os.path.dirname(os.path.dirname(__file__)))   # resolves the junction
        repo = os.path.dirname(pkg)
        if os.path.isdir(os.path.join(repo, ".git")):                         # development checkout
            d = os.path.join(repo, "sandbox")
        else:                       # installed plugin: keep data out of the plugins folder (updates replace it)
            from qgis.core import QgsApplication
            d = os.path.join(QgsApplication.qgisSettingsDirPath(), "QGeo3D", "sandbox")
    os.makedirs(d, exist_ok=True)
    return d


def _q2t_settings_path():
    """Where Qgis2threejs saves a project's 3D settings (<project>.qto3settings).
    The helper moved from Qgis2threejs.utils (2.x) to Qgis2threejs.utils.qgis (3.x)."""
    for mod in ("Qgis2threejs.utils", "Qgis2threejs.utils.qgis", "Qgis2threejs.qgis2threejstools"):
        try:
            import importlib
            f = getattr(importlib.import_module(mod), "settingsFilePath", None)
            if f is not None:
                return f()
        except Exception:
            continue
    from qgis.core import QgsProject
    fn = QgsProject.instance().fileName()
    return fn + ".qto3settings" if fn else None


def guard_q2t_settings(window):
    """Redirect Qgis2threejs' on-close settings save away from protected folders."""
    s = window.settings
    if getattr(s, "_q2t4Guarded", False):
        return
    orig = s.saveSettings

    def guarded_save(filepath=None):
        # Runs inside Qgis2threejs' closeEvent: it must never raise, or Qgis2threejs skips
        # stopping its builder thread and Qt aborts QGIS when the window is destroyed.
        try:
            target = filepath or _q2t_settings_path()
            if target and is_protected(target):
                new = os.path.join(sandbox_dir(), os.path.basename(target))
                log(f"Ringfence: Qgis2threejs settings redirected from {target} to {new}")
                target = new
            if not target:
                return False
            return orig(target)
        except Exception as e:
            try:
                log(f"Qgis2threejs settings not saved: {e}", Qgis.MessageLevel.Warning)
            except Exception:
                pass
            return False

    s.saveSettings = guarded_save
    s._q2t4Guarded = True


def log(msg, level=Qgis.MessageLevel.Info):
    QgsMessageLog.logMessage(msg, LOG_TAG, level)


def _b64(arr):
    return base64.b64encode(arr.tobytes()).decode("ascii")


class Q3DHook(QObject):

    statusChanged = pyqtSignal(str)
    windowConnected = pyqtSignal(object)    # Q3DWindow we just hooked
    itemsChanged = pyqtSignal()             # meshes / overlays added, removed or shown/hidden
    gpuWarning = pyqtSignal(str)            # the 3D view is drawn without GPU acceleration

    def __init__(self, parent=None):
        super().__init__(parent)
        self.payloads = OrderedDict()       # id -> JSON payload string
        self.meshes = OrderedDict()         # id -> Mesh (python side, for stats/export later)
        self.tubes = OrderedDict()          # name -> tube spec (drillholes by grade)
        self.labels = OrderedDict()         # name -> label spec
        self.edgesVisible = True
        self.sectionSpec = None             # dict sent to Q2T4.setSection, re-pushed after reloads
        self.overlays = OrderedDict()       # name -> JSON spec (drillholes, strings)
        self.snapOpts = {}
        self.hiddenMeshes = set()           # mesh ids switched off (kept across page reloads)
        self.blockPayloads = OrderedDict()  # id -> JSON spec
        self.blockModels = OrderedDict()    # id -> BlockModel
        self.hudSpec = {"chip": None, "inset": None}
        self.webToolbar = False             # the in-view button bar: only when the native toolbar couldn't be added
        self.queryMode = False
        self.gridOn = True                  # coordinate grid in aligned views
        self.viewCoordOn = True             # view-plane coordinate under the scale bar
        try:
            from qgis.PyQt.QtCore import QSettings
            _s = QSettings()
            self.insetOn = str(_s.value("/QGeo3D/insetOn", "true")).lower() in ("true", "1")
            self.insetPos = _s.value("/QGeo3D/insetPos", "bl") or "bl"
            self.chipOn = str(_s.value("/QGeo3D/chipOn", "true")).lower() in ("true", "1")
            self.scaleOn = str(_s.value("/QGeo3D/scaleOn", "true")).lower() in ("true", "1")
        except Exception:
            self.insetOn, self.insetPos, self.chipOn, self.scaleOn = True, "bl", True, True
        self.hiddenOverlays = set()
        self._window = None
        self._page = None
        self._counter = 0

    # ------------------------------------------------------------ window
    @staticmethod
    def q2t_plugin():
        import qgis.utils
        return qgis.utils.plugins.get("Qgis2threejs")

    def window(self, open_if_needed=False):
        p = self.q2t_plugin()
        if p is None:
            raise RuntimeError("Qgis2threejs plugin is not installed/enabled.")
        w = p.liveExporter
        if w is not None and self._closing(w):
            # a window that was just closed is deleted later; let that happen before deciding
            from qgis.PyQt.QtCore import QCoreApplication, QEvent
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            QCoreApplication.processEvents()
            w = p.liveExporter
            if w is not None and self._closing(w):
                w = None                    # never hook (or dock the panel into) a closing window
        if w is None and open_if_needed and p.liveExporter is None and not getattr(self, "_noOpen", 0):
            p.openExporter()
            w = p.liveExporter
        if w is not None and w is not self._window:
            self._connect(w)
        return w

    def noOpen(self):
        """with hook.noOpen(): ... - background updates (e.g. a project CRS change) keep their data
        for the 3D view but never open the Qgis2threejs window themselves."""
        from contextlib import contextmanager

        @contextmanager
        def cm():
            self._noOpen = getattr(self, "_noOpen", 0) + 1
            try:
                yield
            finally:
                self._noOpen -= 1
        return cm()

    @staticmethod
    def _closing(w):
        """True for a Qgis2threejs window that has been closed (hidden, waiting to be deleted) or is gone."""
        try:
            return not w.isVisible() and not w.isMinimized()
        except RuntimeError:
            return True

    @staticmethod
    def find_page(w):
        """The web page of a Qgis2threejs window: .webPage (2.x, 3.x), else the page of its web view.
        None when the preview runs outside QGIS (browser mode) or is switched off."""
        page = getattr(w, "webPage", None)
        if page is None:
            view = getattr(getattr(w, "ui", None), "webView", None) or getattr(w, "webView", None)
            try:
                page = view.page() if view is not None and hasattr(view, "page") else None
            except RuntimeError:
                page = None
        return page

    def _connect(self, w):
        self._disconnect()
        page = self.find_page(w)
        if page is None:
            raise RuntimeError("The Qgis2threejs preview isn't running inside QGIS (browser or no-preview mode). "
                               "QGeo 3D needs the in-QGIS preview: open the exporter with that mode.")
        self._window, self._page = w, page
        guard_q2t_settings(w)
        bridge = getattr(page, "bridge", None)
        sig = getattr(bridge, "sceneLoaded", None)
        if sig is not None:
            sig.connect(self._onSceneLoaded)
        else:                                      # no load signal: notice the scene by polling
            log("Qgis2threejs page has no sceneLoaded signal - watching for the scene instead")
            from qgis.PyQt.QtCore import QTimer
            self._sceneTimer = QTimer(self)
            self._sceneTimer.setInterval(1000)
            self._sceneTimer.timeout.connect(self._watchScene)
            self._sceneTimer.start()
        w.destroyed.connect(self._onWindowDestroyed)
        log("Hooked into Qgis2threejs window")
        self.windowConnected.emit(w)

    def _watchScene(self):
        """Fallback for pages without a sceneLoaded signal: (re)inject when a new scene appears."""
        try:
            ok = self.sceneReady() and not self.js("!!(window.Q2T4 && Q2T4.group && Q2T4.group.parent)", wait=True)
        except Exception:
            return
        if ok:
            self._onSceneLoaded()

    def _disconnect(self):
        self._pollPending = False
        self._pollResult = None
        if getattr(self, "_sceneTimer", None) is not None:
            self._sceneTimer.stop()
            self._sceneTimer = None
        if self._page is not None:
            try:
                self._page.bridge.sceneLoaded.disconnect(self._onSceneLoaded)
            except (TypeError, RuntimeError, AttributeError):
                pass
        self._window = self._page = None

    def _onWindowDestroyed(self, *_):
        self._window = self._page = None

    def unload(self):
        try:
            if self._page is not None:
                self.js("window.Q2T4 && (Q2T4.clearSection(), Q2T4.clear())")
        except RuntimeError:
            pass
        self._disconnect()

    # --------------------------------------------------------------- js
    def remote(self):
        """True when the 3D view runs in another process (Qgis2threejs 3.x) - every answer from it
        comes back over a socket, so we never block QGIS for long waiting for one."""
        return self._page is not None and hasattr(self._page, "socketServer")

    def _js_wait(self, script, timeout_ms):
        from qgis.PyQt.QtCore import QEventLoop, QTimer
        res, loop = {}, QEventLoop()
        timer = QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(loop.quit)

        def done(v):
            res["v"] = v
            loop.quit()
        self._page.runJavaScript(script, done)
        timer.start(timeout_ms)
        if "v" not in res:
            loop.exec()
        if "v" not in res:
            self._slow = getattr(self, "_slow", 0) + 1
            if self._slow in (1, 10, 100):
                log(f"3D view slow to answer ({self._slow}x) - is Qgis2threejs still building layers?")
        return res.get("v")

    def js(self, script, wait=False, timeout_ms=4000):
        if self._page is None:
            return None
        if self.remote():
            if not wait:
                self._page.runJavaScript(script)
                return None
            return self._js_wait(script, timeout_ms)
        if wait and hasattr(self._page, "runJavaScript"):
            # QtWebEngine: our own wait, a few seconds at most. Qgis2threejs' runScript(wait=True)
            # waits up to 30 s per call, and with the panel's polling stacked on top the window
            # could hang for minutes while the page was busy building layers.
            return self._js_wait(script, timeout_ms)
        if hasattr(self._page, "runScript"):
            # message=None suppresses Q2T's debug logging of (large) scripts
            try:
                return self._page.runScript(script, message=None, wait=wait)
            except TypeError:                       # a runScript without these keywords
                return self._page.runScript(script)
        # a plain QWebEnginePage: run the script, waiting for its value when asked
        if not wait:
            self._page.runJavaScript(script)
            return None
        from qgis.PyQt.QtCore import QEventLoop, QTimer
        res, loop = {}, QEventLoop()
        self._page.runJavaScript(script, lambda v: (res.setdefault("v", v), loop.quit()))
        QTimer.singleShot(30000, loop.quit)
        loop.exec()
        return res.get("v")

    def sceneReady(self):
        if self._page is None:
            return False
        return bool(self.js("!!(window.app && app.scene && app.scene.userData && app.scene.userData.origin)", wait=True))

    def inject(self):
        with open(JS_PATH, "r", encoding="utf-8") as fh:
            self.js(fh.read())

    def _push(self, mid):
        self.js(f"Q2T4.addMesh({json.dumps(mid)}, {self.payloads[mid]});")
        if mid in self.hiddenMeshes:
            self.js(f"Q2T4.setVisible({json.dumps(mid)}, false)")

    def _onSceneLoaded(self):
        # called from the page's bridge: do the work after it returns, so the page is free to answer
        from qgis.PyQt.QtCore import QTimer
        QTimer.singleShot(0, self._sceneLoadedWork)

    def _sceneLoadedWork(self):
        # fires on first load, page reload and settings changes: make sure we're there
        if self._page is None:
            return
        self.inject()
        missing = self.js("window.Q2T4 ? Q2T4.ids().join('\\n') : ''", wait=True) or ""
        present = set(filter(None, missing.split("\n")))
        for mid in self.payloads:
            if mid not in present:
                self._push(mid)
        self.js(f"Q2T4.setEdgesVisible({'true' if self.edgesVisible else 'false'})")
        self._pushOverlays()
        self._pushBlocks()
        self._pushSection()
        self._pushUi()
        self._pushExtras()
        self.tuneGpu()

    # ------------------------------------------------------- tubes / labels
    def setTubes(self, name, spec):
        if spec is None:
            self.tubes.pop(name, None)
            self.js(f"window.Q2T4 && Q2T4.removeTubes && Q2T4.removeTubes({json.dumps(name)})")
        else:
            self.tubes[name] = spec
            self.js(f"Q2T4.setTubes({json.dumps(name)}, {json.dumps(spec)})")
        self.itemsChanged.emit()

    def setLabels(self, name, spec):
        if spec is None:
            self.labels.pop(name, None)
        else:
            self.labels[name] = spec
        self.js(f"window.Q2T4 && Q2T4.setLabels && Q2T4.setLabels({json.dumps(name)}, {json.dumps(spec)})")

    def _pushExtras(self):
        for n, sp in self.tubes.items():
            self.js(f"Q2T4.setTubes({json.dumps(n)}, {json.dumps(sp)})")
        for n, sp in self.labels.items():
            self.js(f"Q2T4.setLabels({json.dumps(n)}, {json.dumps(sp)})")

    # ---------------------------------------------------------------- GPU
    def tuneGpu(self):
        """Read the WebGL renderer of the 3D view and set the drawing resolution for it
        (core/gpu.py). Off: the screen's full resolution, as Qgis2threejs has it."""
        from qgis.PyQt.QtCore import QSettings
        from . import gpu
        auto = QSettings().value(gpu.KEY_AUTO, True, type=bool)
        try:
            raw = self.js("window.Q2T4 && Q2T4.gpuDetails ? Q2T4.gpuDetails() : ''", wait=True) or ""
            info = json.loads(raw) if raw else {}
        except Exception:
            info = {}
        if not info:
            return None
        t = gpu.tier(info.get("renderer"))
        cap = gpu.PIXEL_RATIO[t] if auto else None
        self.js(f"window.Q2T4 && Q2T4.setPixelRatioCap && Q2T4.setPixelRatioCap({json.dumps(cap)})")
        new = dict(info, tier=t, cap=cap, name=gpu.describe(info.get("renderer")), auto=auto)
        if new.get("renderer") != (getattr(self, "gpu", None) or {}).get("renderer"):
            log(f"3D view drawn by {new['name']} - {t} tier, "
                f"{'full screen resolution' if cap is None else f'resolution capped at {cap}x'}")
        self.gpu = new
        if t == "software" and not getattr(self, "_swWarned", False):
            self._swWarned = True
            self.gpuWarning.emit("The 3D view is drawn in software (no GPU acceleration), so it will be slow. "
                                 "Update the graphics driver, or avoid remote desktop for 3D work.")
        return new

    # ----------------------------------------------------------------- UI
    def _pushUi(self):
        self.js(f"window.Q2T4 && (Q2T4.setToolbarVisible({'true' if self.webToolbar else 'false'}), "
                f"Q2T4.setHud({json.dumps(self.hudSpec)}), Q2T4.setQueryMode({'true' if self.queryMode else 'false'}), "
                f"Q2T4.setGrid && Q2T4.setGrid({'true' if self.gridOn else 'false'}), "
                f"Q2T4.setViewCoord && Q2T4.setViewCoord({'true' if self.viewCoordOn else 'false'}), "
                f"Q2T4.setInset && Q2T4.setInset({'true' if self.insetOn else 'false'}, {json.dumps(self.insetPos)}), "
                f"Q2T4.setChip && Q2T4.setChip({'true' if self.chipOn else 'false'}), "
                f"Q2T4.setScaleBar && Q2T4.setScaleBar({'true' if self.scaleOn else 'false'}))")

    def setScaleBar(self, on):
        """Whole scale bar box (bar, CRS name, view coordinate). Remembered."""
        from qgis.PyQt.QtCore import QSettings
        self.scaleOn = bool(on)
        QSettings().setValue("/QGeo3D/scaleOn", self.scaleOn)
        self.js(f"window.Q2T4 && Q2T4.setScaleBar && Q2T4.setScaleBar({'true' if self.scaleOn else 'false'})")

    def setChip(self, on):
        """Section label (chip) at the top left of the 3D view. Remembered."""
        from qgis.PyQt.QtCore import QSettings
        self.chipOn = bool(on)
        QSettings().setValue("/QGeo3D/chipOn", self.chipOn)
        self.js(f"window.Q2T4 && Q2T4.setChip && Q2T4.setChip({'true' if self.chipOn else 'false'})")

    def setInset(self, on=None, pos=None):
        """Section plan inset: on/off and corner ('bl' bottom left, 'tl' top left). Remembered."""
        from qgis.PyQt.QtCore import QSettings
        if on is not None:
            self.insetOn = bool(on)
            QSettings().setValue("/QGeo3D/insetOn", self.insetOn)
        if pos in ("bl", "tl"):
            self.insetPos = pos
            QSettings().setValue("/QGeo3D/insetPos", pos)
        self.js(f"window.Q2T4 && Q2T4.setInset && Q2T4.setInset({'true' if self.insetOn else 'false'}, {json.dumps(self.insetPos)})")

    def setViewCoord(self, on):
        self.viewCoordOn = bool(on)
        self.js(f"window.Q2T4 && Q2T4.setViewCoord && Q2T4.setViewCoord({'true' if on else 'false'})")

    def setGrid(self, on):
        self.gridOn = bool(on)
        self.js(f"window.Q2T4 && Q2T4.setGrid && Q2T4.setGrid({'true' if on else 'false'})")

    def setHud(self, **kw):
        self.hudSpec.update(kw)
        if self._page is not None:
            self.js(f"window.Q2T4 && Q2T4.setHud({json.dumps(kw)})")

    def setWebToolbarVisible(self, on):
        self.webToolbar = bool(on)
        self.js(f"window.Q2T4 && Q2T4.setToolbarVisible({'true' if on else 'false'})")

    def setQueryMode(self, on):
        self.queryMode = bool(on)
        self.window(open_if_needed=True)
        if self.sceneReady():
            self.inject()
            self.js(f"Q2T4.setQueryMode({'true' if on else 'false'})")

    def poll(self):
        """One round trip: queued events plus cursor / mode / triangle count for the status bar."""
        if self._page is None:
            return None
        script = "window.Q2T4 && Q2T4.statusInfo ? Q2T4.statusInfo() : null"
        if self.remote() or hasattr(self._page, "runJavaScript"):
            # never wait: ask now, use the answer that came back since the last poll
            import time
            if getattr(self, "_pollPending", False) and time.time() - getattr(self, "_pollSent", 0) > 5:
                self._pollPending = False             # the answer never came (view restarted): ask again
            if not getattr(self, "_pollPending", False):
                self._pollPending = True
                self._pollSent = time.time()

                def got(v):
                    self._pollPending = False
                    self._pollResult = v
                try:
                    self._page.runJavaScript(script, got)
                except RuntimeError:
                    self._pollPending = False
            s, self._pollResult = getattr(self, "_pollResult", None), None
            return json.loads(s) if s else None
        try:
            s = self.js(script, wait=True)
        except RuntimeError:
            return None
        return json.loads(s) if s else None

    def gpuInfo(self):
        try:
            return self.js("window.Q2T4 && Q2T4.gpuInfo ? Q2T4.gpuInfo() : ''", wait=True) or ""
        except RuntimeError:
            return ""

    # ------------------------------------------------------- block models
    def _pushBlocks(self):
        for bid, spec in self.blockPayloads.items():
            self.js(f"Q2T4.setBlocks({json.dumps(bid)}, {spec})")
            if bid in self.hiddenMeshes:
                self.js(f"Q2T4.setBlocksVisible({json.dumps(bid)}, false)")

    def setBlocks(self, bid, bm, spec):
        self.blockModels[bid] = bm
        self.blockPayloads[bid] = json.dumps(spec)
        self.window(open_if_needed=True)
        if self.sceneReady():
            self.inject()
            self.js(f"Q2T4.setBlocks({json.dumps(bid)}, {self.blockPayloads[bid]})")
            if bid in self.hiddenMeshes:
                self.js(f"Q2T4.setBlocksVisible({json.dumps(bid)}, false)")
        self.itemsChanged.emit()

    def removeBlocks(self, bid):
        self.blockPayloads.pop(bid, None)
        self.blockModels.pop(bid, None)
        self.hiddenMeshes.discard(bid)
        self.js(f"window.Q2T4 && Q2T4.removeBlocks({json.dumps(bid)})")
        self.itemsChanged.emit()

    # ---------------------------------------------------------- overlays
    def _pushOverlays(self):
        for name, spec in self.overlays.items():
            self.js(f"Q2T4.setOverlay({json.dumps(name)}, {spec})")
            if name in self.hiddenOverlays:
                self.js(f"Q2T4.setOverlayVisible({json.dumps(name)}, false)")
        if self.snapOpts:
            self.js(f"Q2T4.setSnap({json.dumps(self.snapOpts)})")

    def setOverlay(self, name, spec):
        """spec: dict from strings.polyline_payload(), or None to remove."""
        if spec is None:
            self.overlays.pop(name, None)
            self.hiddenOverlays.discard(name)
            self.js(f"window.Q2T4 && Q2T4.removeOverlay({json.dumps(name)})")
            self.itemsChanged.emit()
            return
        self.overlays[name] = json.dumps(spec)
        if self._page is not None and self.sceneReady():
            self.inject()
            self.js(f"Q2T4.setOverlay({json.dumps(name)}, {self.overlays[name]})")
            if name in self.hiddenOverlays:
                self.js(f"Q2T4.setOverlayVisible({json.dumps(name)}, false)")
        self.itemsChanged.emit()

    def setOverlayVisible(self, name, visible):
        (self.hiddenOverlays.discard if visible else self.hiddenOverlays.add)(name)
        self.js(f"window.Q2T4 && Q2T4.setOverlayVisible({json.dumps(name)}, {'true' if visible else 'false'})")
        self.itemsChanged.emit()

    # -------------------------------------------------------- digitising
    def setSnap(self, **opts):
        self.snapOpts.update(opts)
        self.js(f"window.Q2T4 && Q2T4.setSnap({json.dumps(self.snapOpts)})")

    def startDigitise(self):
        w = self.window(open_if_needed=True)
        if not self.sceneReady():
            return False
        self.inject()
        self.js(f"Q2T4.startDigitise({json.dumps(self.snapOpts)})")
        if w is not None:
            w.raise_()
            w.activateWindow()
        return True

    def stopDigitise(self):
        self.js("window.Q2T4 && Q2T4.stopDigitise()")

    def takeEvents(self):
        if self._page is None:
            return []
        try:
            s = self.js("window.Q2T4 && Q2T4.takeEvents ? Q2T4.takeEvents() : '[]'", wait=True)
        except RuntimeError:            # page deleted
            return []
        return json.loads(s) if s else []

    # ----------------------------------------------------------- section
    def _pushSection(self):
        if self.sectionSpec is None:
            self.js("window.Q2T4 && Q2T4.clearSection()")
        else:
            self.js(f"window.Q2T4 && Q2T4.setSection({json.dumps(self.sectionSpec)})")

    def demProfileRange(self, section, samples=100):
        """(min, max) elevation of the DEM layers shown in the 3D view along the slab, or None."""
        if self._window is None:
            return None
        from qgis.core import QgsCoordinateTransform, QgsPointXY, QgsProject, QgsRasterLayer
        dest = self._window.qgisIface.mapCanvas().mapSettings().destinationCrs()
        lo, hi = None, None
        for lyr in self._window.settings.layers():
            if lyr.type != 0 or not lyr.visible or lyr.mapLayer is None:       # 0 = LayerType.DEM
                continue
            rl = lyr.mapLayer
            if not isinstance(rl, QgsRasterLayer):
                continue
            xf = QgsCoordinateTransform(dest, rl.crs(), QgsProject.instance()) if rl.crs() != dest else None
            prov = rl.dataProvider()
            for off in (-section.width / 2, 0.0, section.width / 2):
                s = section.offset(off)
                for i in range(samples + 1):
                    t = i / samples
                    p = QgsPointXY(s.a[0] + (s.b[0] - s.a[0]) * t, s.a[1] + (s.b[1] - s.a[1]) * t)
                    if xf:
                        p = xf.transform(p)
                    val, ok = prov.sample(p, 1)
                    if ok:
                        lo = val if lo is None else min(lo, val)
                        hi = val if hi is None else max(hi, val)
        return None if lo is None else (lo, hi)

    def setSection(self, spec):
        """spec: Section.to_spec() dict, or None to remove the section."""
        self.sectionSpec = spec
        if self._page is not None and self.sceneReady():
            self.inject()
            self._pushSection()

    def viewSection(self):
        self.window(open_if_needed=True)
        if self.sceneReady():
            self.inject()
            self._pushSection()
            self.js("window.Q2T4 && Q2T4.viewSection()")

    # ----------------------------------------------------------- meshes
    def addMesh(self, mesh, color=None, edgeColor="#1a1a1a", opacity=1.0, mid=None, zoom=True):
        if len(mesh.triangles) == 0:
            raise ValueError(f"'{mesh.name}' contains no triangles")
        self._counter += 1
        while mid is None and f"wf{self._counter}" in self.meshes:     # never reuse a live id
            self._counter += 1
        mid = mid or f"wf{self._counter}"
        color = color or PALETTE[(self._counter - 1) % len(PALETTE)]

        offset, local = mesh.local_frame()
        edges, _ = mesh.edges()
        spec = {
            "name": mesh.name,
            "offset": [float(x) for x in offset],
            "pos": _b64(local.astype("<f4")),
            "idx": _b64(mesh.triangles.astype("<u4")),
            "edges": _b64(edges.astype("<u4")),
            "color": color,
            "edgeColor": edgeColor,
            "opacity": float(opacity),
        }
        self.payloads[mid] = json.dumps(spec)
        self.meshes[mid] = mesh

        self.window(open_if_needed=True)
        if self.sceneReady():
            self.inject()
            self._push(mid)
            if zoom:
                self.js(f"Q2T4.zoomTo({json.dumps(mid)})")
        # else: _onSceneLoaded pushes it when the scene arrives
        self.itemsChanged.emit()
        return mid

    def removeMesh(self, mid):
        self.payloads.pop(mid, None)
        self.meshes.pop(mid, None)
        self.hiddenMeshes.discard(mid)
        self.js(f"window.Q2T4 && Q2T4.remove({json.dumps(mid)})")
        self.itemsChanged.emit()

    def setMeshVisible(self, mid, visible):
        (self.hiddenMeshes.discard if visible else self.hiddenMeshes.add)(mid)
        fn = "setBlocksVisible" if mid in self.blockPayloads else "setVisible"
        self.js(f"window.Q2T4 && Q2T4.{fn}({json.dumps(mid)}, {'true' if visible else 'false'})")
        self.itemsChanged.emit()

    def clear(self):
        self.payloads.clear()
        self.meshes.clear()
        self.hiddenMeshes.clear()
        self.js("window.Q2T4 && Q2T4.clear()")
        self.itemsChanged.emit()

    def setEdgesVisible(self, visible):
        self.edgesVisible = bool(visible)
        self.js(f"window.Q2T4 && Q2T4.setEdgesVisible({'true' if visible else 'false'})")

    def refresh(self):
        """Re-inject the JS module and re-push every mesh (after a plugin reload)."""
        if self._page is None or not self.sceneReady():
            return
        self.inject()
        for mid in self.payloads:
            self._push(mid)
        self.js(f"Q2T4.setEdgesVisible({'true' if self.edgesVisible else 'false'})")
        self._pushOverlays()
        self._pushBlocks()
        self._pushSection()
        self._pushUi()
        self._pushExtras()
        self.tuneGpu()

    def setView(self, name):
        """name: 'plan', 'N', 'E', 'S' or 'W'."""
        self.window(open_if_needed=True)
        if self.sceneReady():
            self.inject()
            self.js(f"window.Q2T4 && Q2T4.setView({json.dumps(name)})")

    def centreOnMap(self):
        """Toolbar button: move the orbit point to the centre of the QGIS map area, at the height of
        what is shown there, and fit that area - the view direction is kept."""
        self.window(open_if_needed=True)
        self.inject()
        self.js("window.Q2T4 && Q2T4.centreOnMap && Q2T4.centreOnMap(false)")

    def recentre(self, everything=False):
        self.window(open_if_needed=True)
        self.inject()
        self.js(f"window.Q2T4 && Q2T4.recentre({'true' if everything else 'false'})")

    def startOrbitPick(self):
        w = self.window(open_if_needed=True)
        self.inject()
        self.js("window.Q2T4 && Q2T4.startOrbitPick()")
        if w is not None:
            w.raise_()
            w.activateWindow()

    def zoomTo(self, mid=None):
        self.js(f"window.Q2T4 && Q2T4.zoomTo({json.dumps(mid) if mid else 'null'})")

    def info(self):
        s = self.js("window.Q2T4 ? Q2T4.info() : null", wait=True)
        return json.loads(s) if s else None
