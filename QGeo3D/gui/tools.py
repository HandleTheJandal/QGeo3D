# -*- coding: utf-8 -*-
"""Panel tools added in 0.9.3, kept out of dock.py: measuring, 3D vertex editing, saved views and
the section plot PDF. Mixed into Q2T4Dock."""

import datetime
import json
import os

from qgis.PyQt.QtCore import QSettings, Qt
from qgis.PyQt.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
                                 QFormLayout, QGroupBox, QHBoxLayout, QInputDialog, QLineEdit, QListWidget,
                                 QListWidgetItem, QMessageBox, QPushButton, QVBoxLayout, QWidget)
from qgis.core import Qgis, QgsProject

from ..core.q2thook import is_protected, log

VIEWS_SCOPE, VIEWS_KEY = "QGeo3D", "views"
OLD_VIEWS_SCOPE = "QGIS2Three4"          # saved views written before the rename


class ToolsMixin:

    # ------------------------------------------------------------ events
    def _toolEvent(self, ev):
        t = ev.get("type")
        vt = self._vtb()
        if t == "measureMode":
            if vt is not None and hasattr(vt, "aMeasure"):
                vt._mirror(vt.aMeasure, bool(ev.get("active")))
        elif t == "editMode":
            if vt is not None and hasattr(vt, "aEdit"):
                vt._mirror(vt.aEdit, bool(ev.get("active")))
            if not ev.get("active"):
                self._editIds = None
                self._syncToolButtons(None)
                self._mirrorCheck(getattr(self, "bCap", None), False)
                if getattr(self, "_linkChain", None) is not None:
                    self._linkFinish()
        elif t == "measure":
            r = ev.get("result") or {}
            txt = (f"Measure: {r.get('length', 0):.2f} m (horizontal {r.get('horizontal', 0):.2f}, "
                   f"ΔRL {r.get('dz', 0):+.2f}) · bearing {r.get('bearing', 0):.1f}° · plunge {r.get('plunge', 0):.1f}°")
            if "dip" in r:
                txt += f" · plane dip {r['dip']:.1f}° → {r['dipDirection']:.1f}°"
            self.lastMeasure = dict(r, pts=ev.get("pts"))
            QApplication.clipboard().setText(txt)
            self.plugin._msg(txt + "  (copied)", Qgis.MessageLevel.Info, 8)
            log(txt)
        elif t in ("vertexMoved", "vertexDeleted"):
            self._applyVertexEdit(ev)
        elif t in ("vertexInserted", "stringCut", "stringCopy"):
            self._applyStringTool(ev)
        elif t == "stringsSelected":
            self._stringsSelected(ev.get("ids") or [])
        elif t == "stringsDelete":
            self._deleteStrings(ev.get("ids") or [])
        elif t == "pointsDelete":
            self._deletePoints(ev.get("points") or [])
        elif t == "snapToggled":
            self._mirrorCheck(getattr(self, "cbSnapOn", None), ev.get("on"))
            cb = getattr(self, "cbSnapMode", None)
            if cb is not None and ev.get("mode") and cb.currentData() != ev["mode"]:
                cb.blockSignals(True)
                cb.setCurrentIndex(max(0, cb.findData(ev["mode"])))
                cb.blockSignals(False)
            if hasattr(self, "_snapUi"):
                self._snapUi()
        elif t == "traceToggled":
            self._mirrorCheck(getattr(self, "cbTrace", None), ev.get("on"))
        elif t == "triMode":
            self._mirrorCheck(getattr(self, "bTri", None), ev.get("active"))
            if not ev.get("active"):
                self._validateEdited()
        elif t == "triangle":
            self._addTriangle(ev.get("pts"))
        elif t == "triDelMode":
            self._mirrorCheck(getattr(self, "bTriDel", None), ev.get("active"))
            if not ev.get("active"):
                self._validateEdited()
        elif t == "triangleDelete":
            self._deleteTriangle(ev)
        elif t == "linkPick":
            self._linkPick(ev)
        elif t == "linkUndo":
            self._linkUndo()
        elif t == "undo":
            self.undoAny()
        elif t == "redo":
            self.redoAny()
        elif t == "renameSelected":
            self.renameSelected3D()
        elif t == "capPick":
            self._capPick(ev)
        elif t == "capUndo":
            self.undoAny()

    @staticmethod
    def _mirrorCheck(w, on):
        if w is not None and w.isChecked() != bool(on):
            w.blockSignals(True)
            w.setChecked(bool(on))
            w.blockSignals(False)

    # ------------------------------------------------------------ measure
    def setMeasure(self, on):
        if on:
            self.hook.js("window.Q2T4 && Q2T4.startMeasure(%s)" % json.dumps(None))
            self.hook.js(f"Q2T4.setSnap({json.dumps(self._snapOpts())})")
        else:
            self.hook.js("window.Q2T4 && Q2T4.stopMeasure()")

    # ------------------------------------------------------------ editing
    def setEditVertices(self, on, mode="move"):
        if not on:
            self.hook.js("window.Q2T4 && Q2T4.stopEdit()")
            return
        items = self._editItems()
        if not items:
            self.plugin._msg("Nothing to edit: digitise strings, or select a wireframe in Objects first.",
                             Qgis.MessageLevel.Warning, 8)
            vt = self._vtb()
            if vt is not None and hasattr(vt, "aEdit"):
                vt._mirror(vt.aEdit, False)
            self._syncToolButtons(None)
            return
        self.hook.js(f"Q2T4.setSnap({json.dumps(self._snapOpts())})")
        self.hook.js(f"Q2T4.startEdit({json.dumps({'items': items, 'mode': mode})}); Q2T4.setEditMode({json.dumps(mode)})")
        self._editMode = mode
        self._syncToolButtons(mode)
        vt = self._vtb()
        if vt is not None and hasattr(vt, "aEdit"):
            vt._mirror(vt.aEdit, True)

    def _visibleStringItems(self):
        from ..core import editing as E
        if "strings" in self.hook.hiddenOverlays:
            return []
        hidden = self.hiddenStrings()
        fn = self._fidNo()
        return [it for it in E.string_items(self.store) if fn.get(int(it["id"][2:])) not in hidden]

    def _editItems(self):
        from ..core import editing as E
        items = []
        try:
            self.ensureStore()
            items += self._visibleStringItems()
        except Exception as e:
            log(f"Edit: strings not available: {e}")
        sel = []
        for it in (self.lstWf.selectedItems() if hasattr(self, "lstWf") else []):
            d = it.data(Qt.ItemDataRole.UserRole)
            if isinstance(d, (tuple, list)) and d[0] == "mesh" and not str(d[1]).startswith("bm:"):
                sel.append(d[1])
        if sel:
            items += E.mesh_items(self.hook, sel)
        return items

    # ------------------------------------------------------------ string tools
    def setStringTool(self, mode, sender=None):
        """Move / Insert / Cut / Copy on the Strings tab (one at a time; None ends editing)."""
        if mode != "link" and getattr(self, "_linkChain", None) is not None:
            self._linkFinish()                          # another tool ends linking one by one
        if mode != "cap":
            self._mirrorCheck(getattr(self, "bCap", None), False)
        if mode is None:
            if sender is not None and getattr(self, "_editMode", None) not in (None, sender):
                return                                  # another tool took over
            self._editMode = None
            self.hook.js("window.Q2T4 && Q2T4.stopEdit()")
            return
        if self.hook.js("!!(window.Q2T4 && Q2T4.edit && Q2T4.edit.active)", wait=True):
            self.hook.js(f"Q2T4.setEditMode({json.dumps(mode)})")
            self._editMode = mode
            self._syncToolButtons(mode)
        else:
            self.setEditVertices(True, mode)
        if mode == "select":
            self._listSelectionTo3D()

    # ------------------------------------------------------------ selecting strings
    def _fidNo(self):
        """{feature id: string_no} of the strings layer."""
        lyr = getattr(self.store, "layer", None)
        if lyr is None:
            return {}
        return {f.id(): f["string_no"] for f in lyr.getFeatures()}

    def _stringsSelected(self, ids):
        """3D selection -> the Wireframing list and the QGIS strings layer."""
        fids = [int(i[2:]) for i in ids if str(i).startswith("s:")]
        fn = self._fidNo()
        nos = {fn.get(f) for f in fids}
        self._selSync = True
        try:
            lst = self.lstStrings
            lst.blockSignals(True)
            first = None
            for i in range(lst.count()):
                it = lst.item(i)
                d = it.data(Qt.ItemDataRole.UserRole)
                if not isinstance(d, int):
                    continue                                  # wireframe / header rows keep their selection
                on = d in nos
                it.setSelected(on)
                if on and first is None:
                    first = it
            lst.blockSignals(False)
            if first is not None:
                lst.scrollToItem(first)
            lyr = getattr(self.store, "layer", None)
            if lyr is not None:
                lyr.selectByIds(fids)
        finally:
            self._selSync = False
        n = len(fids)
        self.plugin.iface.statusBarIface().showMessage(f"{n} string(s) selected" if n else "No strings selected", 3000)

    def _listSelectionTo3D(self):
        if getattr(self, "_selSync", False):
            return
        nos = set(self.selectedStringNos())
        ids = [f"s:{fid}" for fid, no in self._fidNo().items() if no in nos]
        self.hook.js(f"window.Q2T4 && Q2T4.setSelectedStrings && Q2T4.setSelectedStrings({json.dumps(ids)})")
        lyr = getattr(self.store, "layer", None)
        if lyr is not None:
            lyr.selectByIds([int(i[2:]) for i in ids])

    def _deleteStrings(self, ids):
        from qgis.PyQt.QtWidgets import QMessageBox
        from ..core.q2thook import is_protected
        fids = [int(i[2:]) for i in ids if str(i).startswith("s:")]
        lyr = getattr(self.store, "layer", None)
        if not fids or lyr is None:
            return
        if is_protected(self.store.path):
            self.plugin._msg("Protected folder - strings not deleted.", Qgis.MessageLevel.Warning, 6)
            return
        if QMessageBox.question(self, "Delete strings", f"Delete {len(fids)} selected string(s)?") != \
                QMessageBox.StandardButton.Yes:
            return
        if lyr.isEditable():
            lyr.deleteFeatures(fids)
        else:
            lyr.dataProvider().deleteFeatures(fids)
            lyr.reload()
        lyr.triggerRepaint()
        self.store.changed.emit()
        self.hook.js("window.Q2T4 && Q2T4.setSelectedStrings([])")
        self._refreshEditItems()
        self.plugin._msg(f"{len(fids)} string(s) deleted", Qgis.MessageLevel.Info, 4)

    def _selectedFids(self):
        nos = set(self.selectedStringNos())
        return [fid for fid, no in self._fidNo().items() if no in nos]

    def closeSelectedStrings(self):
        from ..core import editing as E
        fids = self._selectedFids()
        if not fids:
            self.plugin._msg("Select the open strings to close first (Select tool, or the Wireframing list).",
                             Qgis.MessageLevel.Warning, 6)
            return
        done = 0
        try:
            for fid in fids:
                done += bool(E.close_string(self.store, fid))
        except Exception as e:
            self.plugin._msg(f"Strings not closed: {e}", Qgis.MessageLevel.Critical, 8)
        skipped = len(fids) - done
        self.plugin._msg(f"{done} string(s) closed" + (f", {skipped} already closed or under 3 points" if skipped else ""),
                         Qgis.MessageLevel.Info, 5)
        self._refreshEditItems()

    def _deletePoints(self, points):
        from ..core import editing as E
        total = asked = 0
        try:
            for p in points:
                oid = str(p.get("id"))
                if not oid.startswith("s:"):
                    continue
                asked += len(p.get("idx") or [])
                total += E.delete_string_vertices(self.store, int(oid[2:]), p.get("idx") or [])
        except Exception as e:
            self.plugin._msg(f"Points not deleted: {e}", Qgis.MessageLevel.Critical, 8)
        msg = f"{total} point(s) deleted"
        if total < asked:
            msg += f" - {asked - total} kept (a string needs 2 points, a closed outline 3)"
        self.plugin._msg(msg, Qgis.MessageLevel.Info, 5)
        self._refreshEditItems()

    def _syncToolButtons(self, mode):
        for m, b in getattr(self, "strToolButtons", {}).items():
            self._mirrorCheck(b, m == mode)
        if mode is None:
            self._editMode = None

    def _refreshEditItems(self):
        self.hook.js(f"window.Q2T4 && Q2T4.setEditItems({json.dumps({'items': self._editItems()})})")
        self._sendTraceItems()

    def _sendTraceItems(self):
        from ..core import editing as E
        try:
            items = self._visibleStringItems()
        except Exception:
            items = []
        self.hook.js(f"window.Q2T4 && Q2T4.setTraceItems && Q2T4.setTraceItems({json.dumps({'items': items})})")

    def _applyStringTool(self, ev):
        from ..core import editing as E
        oid = str(ev.get("id"))
        if not oid.startswith("s:"):
            return
        fid = int(oid[2:])
        t = ev["type"]
        try:
            if t == "vertexInserted":
                n = E.insert_string_vertex(self.store, fid, int(ev["seg"]), ev["p"])
                self.plugin._msg(f"Point inserted ({n} points now)", Qgis.MessageLevel.Info, 3)
            elif t == "stringCut":
                kind, no = E.cut_string(self.store, fid, at_vertex=ev.get("i"), seg=ev.get("seg"), p=ev.get("p"))
                self.plugin._msg("Closed outline opened - the clicked segment was removed" if kind == "opened"
                                 else f"String cut in two - the second part is string {no}", Qgis.MessageLevel.Info, 5)
            elif t == "stringCopy":
                last = getattr(self, "_copyOffset", "0, 0, 0")
                txt, ok = QInputDialog.getText(self, "Copy string", "Offset of the copy: dX, dY, dZ (m)", text=last)
                if not ok:
                    return
                try:
                    off = [float(v) for v in txt.replace(";", ",").split(",")]
                    if len(off) != 3:
                        raise ValueError
                except ValueError:
                    self.plugin._msg("Give three numbers: dX, dY, dZ", Qgis.MessageLevel.Warning, 6)
                    return
                self._copyOffset = txt
                lay = self.activeLayer()
                no = E.copy_string(self.store, fid, off, layer=lay)
                self.plugin._msg(f"Copied as string {no} into layer '{lay}'", Qgis.MessageLevel.Info, 4)
        except Exception as e:
            self.plugin._msg(f"String not changed: {e}", Qgis.MessageLevel.Critical, 8)
        self._refreshEditItems()

    # ------------------------------------------------------------ link strings one by one
    def setLinkPick(self, on):
        if on:
            self.ensureStore()
            self._linkChain = []
            self._linkTarget = self.wfTarget()
            self.setStringTool("link")
            if not self.hook.js("!!(window.Q2T4 && Q2T4.edit && Q2T4.edit.active)", wait=True):
                self._linkChain = None
                self._mirrorCheck(self.bLinkPick, False)
                return
            self.hook.js("Q2T4.setSelectedStrings([])")
            tgt = self._linkTarget
            self.lblWf.setText("Click the strings in the 3D view in linking order"
                               + (f" - into {self.hook.meshes[tgt].name}" if tgt else "")
                               + " · Backspace undoes · Enter / Esc finishes")
        elif getattr(self, "_linkChain", None) is not None:
            if getattr(self, "_editMode", None) == "link":
                self.hook.js("window.Q2T4 && Q2T4.stopEdit()")
            self._linkFinish()

    def _linkPick(self, ev):
        if getattr(self, "_linkChain", None) is None:
            return
        oid = str(ev.get("id"))
        if not oid.startswith("s:"):
            return
        no = self._fidNo().get(int(oid[2:]))
        if no is None:
            return
        if self._linkChain and self._linkChain[-1] == no:
            self.plugin._msg(f"String {no} is already the last one picked - click the next string.",
                             Qgis.MessageLevel.Info, 3)
            return
        self._linkChain.append(no)
        if not self._linkPreview():
            self._linkChain.pop()
            self._linkPreview()

    def _linkUndo(self):
        if getattr(self, "_linkChain", None):
            self._linkChain.pop()
            self._linkPreview()

    def _linkMesh(self, chain, name="linking"):
        from ..core import wireframing
        from ..core.strings import string_records
        recs = {r[0]: r for r in string_records(self.store)}
        ch = [recs[n] for n in chain if n in recs]
        return wireframing.link_chain([r[2] for r in ch], [r[3] for r in ch], cap=False, name=name)

    def _linkPreview(self):
        chain = self._linkChain or []
        nf = {v: k for k, v in self._fidNo().items()}
        self.hook.js(f"window.Q2T4 && Q2T4.setSelectedStrings({json.dumps([f's:{nf[n]}' for n in chain if n in nf])})")
        if len(chain) < 2:
            if "linking" in self.hook.meshes:
                self.hook.removeMesh("linking")
            self.lblWf.setText(f"String {chain[0]} picked - click the next one" if chain else
                               "Click the strings in the 3D view in linking order")
            return True
        try:
            m = self._linkMesh(chain)
        except Exception as e:
            self.plugin._msg(f"Can't link string {chain[-1]}: {e}", Qgis.MessageLevel.Warning, 8)
            return False
        self.hook.addMesh(m, color="#f0a030", opacity=0.6, mid="linking", zoom=False)
        st = m.stats()
        self.lblWf.setText(" → ".join(str(n) for n in chain) + f" · {st['triangles']} triangles"
                           + (" · closed" if st.get("closed") else "") + " · Enter / Esc finishes")
        return True

    def _linkFinish(self):
        import copy
        from ..core.strings import save_wireframe
        chain, self._linkChain = (getattr(self, "_linkChain", None) or []), None
        self._mirrorCheck(getattr(self, "bLinkPick", None), False)
        if "linking" in self.hook.meshes:
            self.hook.removeMesh("linking")
        self.hook.js("window.Q2T4 && Q2T4.setSelectedStrings([])")
        if len(chain) < 2:
            self.lblWf.setText("")
            return
        sg = self.spSG.value()
        try:
            tgt = getattr(self, "_linkTarget", None)
            if tgt is not None and tgt in self.hook.meshes:
                m = self._linkMesh(chain)
                V = m.vertices
                self.mergeTriangles(tgt, [[V[a], V[b], V[c]] for a, b, c in m.triangles], "strings linked into it")
                getattr(self, "_editedWfs", set()).discard(tgt)
                self._validateIfClosed(tgt)
                self.lblWf.setText("")
                return
            name = self._askWfName(self._newWfDefault())
            if name is None:
                self.lblWf.setText("Linking discarded")
                return
            m = self._linkMesh(chain, name=name)
            st = m.stats()
            disk = copy.copy(m)
            disk.vertices = self.store.to_layer_crs(m.vertices)
            wf_no = save_wireframe(self.wireframeGpkg(), disk, name, list(chain), sg, st,
                                   crs=self.store.layer.crs() if self.store.layer is not None else None)
            m.name = f"{name} #{wf_no}"
            self.hook.addMesh(m, color="#e0503c", opacity=0.75, mid=f"built{wf_no}", zoom=False)
        except Exception as e:
            self.plugin._msg(f"Wireframing failed: {e}", Qgis.MessageLevel.Critical, 10)
            return
        self._reportBuilt(st, wf_no, sg)

    # ------------------------------------------------------------ close an end (cap) of a wireframe
    def setCapPick(self, on):
        if on:
            if getattr(self, "_linkChain", None) is not None:
                self._linkFinish()
            self.ensureStore()
            self.setStringTool("cap")
            if not self.hook.js("!!(window.Q2T4 && Q2T4.edit && Q2T4.edit.active)", wait=True):
                self._mirrorCheck(self.bCap, False)
        elif getattr(self, "_editMode", None) == "cap":
            self.hook.js("window.Q2T4 && Q2T4.stopEdit()")

    def _capTarget(self, ring):
        """Wireframe to cap: the active one if it uses the outline's points, else the one that does."""
        import numpy as np
        tgt = self.wfTarget()
        best = []
        for mid, m in self.hook.meshes.items():
            if str(mid).startswith("bm:") or mid == "linking" or len(m.vertices) > 2000000:
                continue
            V = np.asarray(m.vertices, float)
            lo, hi = ring.min(axis=0) - 0.01, ring.max(axis=0) + 0.01
            V = V[np.all((V >= lo) & (V <= hi), axis=1)]
            if not len(V):
                continue
            hits = sum(1 for p in ring if np.min(np.linalg.norm(V - p, axis=1)) <= 0.005)
            if hits == len(ring):
                best.append(mid)
        if tgt is not None and (tgt in best or not best):
            return tgt
        if not best:
            return None
        built = sorted((b for b in best if str(b).startswith("built")), key=lambda b: int(str(b)[5:]))
        return built[-1] if built else best[-1]

    def _capPick(self, ev):
        """Close the end of a wireframe at a closed outline. Tie lines (2-point strings) drawn across the
        outline between two of its vertices become edges of the cap. An existing cap on that outline is
        replaced, so an end can be redone."""
        import numpy as np
        from ..core import wireframing, editing as E
        from ..core.mesh import Mesh
        from ..core.strings import string_records
        oid = str(ev.get("id"))
        if not oid.startswith("s:"):
            return
        no = self._fidNo().get(int(oid[2:]))
        recs = string_records(self.store)
        rec = {r[0]: r for r in recs}.get(no)
        if rec is None:
            return
        if not rec[3]:
            self.plugin._msg(f"String {no} is open - close it first (Close in Edit strings), then cap it.",
                             Qgis.MessageLevel.Warning, 6)
            return
        ring, _ok = wireframing.clean_string(rec[2], closed=True)
        mid = self._capTarget(ring)
        if mid is None:
            self.plugin._msg(f"No wireframe uses the points of string {no} - select the wireframe in the list "
                             "(Wireframing) to cap it.", Qgis.MessageLevel.Warning, 8)
            return
        # tie lines across this outline
        n = len(ring)
        span = float(np.ptp(ring, axis=0).max()) or 1.0
        tol = max(0.02, 0.001 * span)
        chords, off_vertex = [], 0
        for r in recs:
            if r[3] or len(r[2]) != 2:
                continue
            ends = []
            for p in np.asarray(r[2], float):
                dv = np.linalg.norm(ring - p, axis=1)
                k = int(np.argmin(dv))
                if dv[k] <= tol:
                    ends.append(k)
                else:
                    seg = min(np.linalg.norm(np.cross(ring[(q + 1) % n] - ring[q], ring[q] - p)) /
                              (np.linalg.norm(ring[(q + 1) % n] - ring[q]) or 1e-300) for q in range(n))
                    if seg <= tol:
                        off_vertex += 1
            if len(ends) == 2 and ends[0] != ends[1] and (ends[1] - ends[0]) % n not in (1, n - 1):
                chords.append((ends[0], ends[1]))
        try:
            tri, used, skipped = wireframing.cap_with_chords(ring, chords)
        except ValueError as e:
            self.plugin._msg(f"String {no} can't be capped: {e}", Qgis.MessageLevel.Warning, 6)
            return
        cap = [[ring[a], ring[b], ring[c]] for a, b, c in tri]
        m = self.hook.meshes[mid]
        V0 = np.asarray(m.vertices, float)
        T0 = np.asarray(m.triangles, np.int64).reshape(-1, 3)
        # an existing cap on this outline (triangles with all corners on it) is replaced
        on = np.zeros(len(V0), bool)
        for p in ring:
            d = np.linalg.norm(V0 - p, axis=1)
            k = int(np.argmin(d))
            if d[k] <= 0.005:
                on[k] = True
        keep = ~on[T0].all(axis=1) if len(T0) else np.zeros(0, bool)
        replaced = int((~keep).sum())
        V, T = E.append_triangles(V0, T0[keep], cap)
        F = wireframing.orient_consistently(T)                   # cap wound like the walls, outward when closed
        st = Mesh(V, F).stats()
        if st.get("closed") and wireframing.signed_volume(V, F) < 0:
            F = F[:, [0, 2, 1]]
        self._pushTriUndo(("mesh", mid, m))
        what = f"end {'re-' if replaced else ''}closed at string {no}"
        if used:
            what += f" along {len(used)} tie line{'s' if len(used) > 1 else ''}"
        self._replaceMesh(mid, m, F, what, vertices=V)
        getattr(self, "_editedWfs", set()).discard(mid)
        notes = []
        if skipped:
            notes.append(f"{len(skipped)} tie line(s) not used (they cross another tie or run along the outline)")
        if off_vertex:
            notes.append(f"{off_vertex} tie end(s) lie between outline points - a tie must end on outline "
                         "vertices (snap: Nodes) to guide the cap")
        if notes:
            self.plugin._msg(f"String {no}: " + "; ".join(notes) + ".", Qgis.MessageLevel.Warning, 10)
        if Mesh(V, F).stats().get("closed"):              # both ends done: check it (an open end is expected)
            self.validateWireframes([mid], quiet_if_clean=True)

    # ------------------------------------------------------------ manual triangles
    def setTriangles(self, on):
        if on:
            mid = self.wfTarget()
            self.lblTri.setText(f"Adding triangles to: {self.hook.meshes[mid].name}" if mid else
                                "Adding triangles to a new wireframe (blue until saved) - select a wireframe "
                                "in the list to add to it instead")
            self.hook.js(f"Q2T4.setSnap({json.dumps(self._snapOpts())})")
            if not self.hook.js("!!(window.Q2T4 && Q2T4.startTriangles())", wait=True):
                self._mirrorCheck(self.bTri, False)
        else:
            self.hook.js("window.Q2T4 && Q2T4.stopTriangles()")

    def _addTriangle(self, pts):
        if not pts or len(pts) != 3:
            return
        tgt = self.wfTarget()
        if tgt is not None:
            self.mergeTriangles(tgt, [pts], "triangle added")
            return
        if not hasattr(self, "_manualTris"):
            self._manualTris = []
        self._manualTris.append(pts)
        self._showTriangles()

    def _showTriangles(self):
        from ..core import editing as E
        tris = getattr(self, "_manualTris", [])
        if not tris:
            if "manual" in self.hook.meshes:
                self.hook.removeMesh("manual")
            self.lblTri.setText("")
            return
        m = E.triangles_mesh(tris)
        self.hook.addMesh(m, color="#00b4d8", opacity=0.8, mid="manual", zoom=False)
        st = m.stats()
        self.lblTri.setText(f"{len(m.triangles)} triangle(s), {len(m.vertices)} corners - "
                            + ("closed" if st.get("closed") else f"{st.get('open_edges', 0)} open edges"))

    def mergeTriangles(self, mid, tris, what):
        """Add triangles (corner coordinates) to wireframe mid; undoable."""
        from ..core import editing as E
        m = self.hook.meshes.get(mid)
        if m is None:
            return
        nt, nv = len(m.triangles), len(m.vertices)
        V, T = E.append_triangles(m.vertices, m.triangles, tris)
        import numpy as np
        T = np.asarray(T, np.int64).reshape(-1, 3)
        seen = {tuple(sorted(t)) for t in T[:nt].tolist()}            # triangles already in it are not added again
        new = []
        for t in T[nt:].tolist():
            k = tuple(sorted(t))
            if k not in seen:
                seen.add(k)
                new.append(t)
        dup = len(T) - nt - len(new)
        T = np.vstack([T[:nt], np.asarray(new, np.int64).reshape(-1, 3)])
        if len(T) == nt:
            self.plugin._msg("Nothing added - " + ("those triangles are already in the wireframe." if dup else
                             "the triangle would have two corners on one vertex."), Qgis.MessageLevel.Warning, 5)
            return
        if dup:
            what += f" ({dup} already there, not added again)"
        self._pushTriUndo(("add", mid, nt, nv))
        self._replaceMesh(mid, m, T, what, vertices=V)
        if not str(mid).startswith("built") and mid != "manual" and not getattr(self, "_toldViewOnly", False):
            self._toldViewOnly = True
            self.plugin._msg(f"{m.name} isn't saved in the strings GeoPackage: the change is in the 3D view only - "
                             "Export writes it to a file.", Qgis.MessageLevel.Info, 8)

    def _pushTriUndo(self, entry, keep_redo=False):
        """One undo step: a wireframe change ('add' / 'del' / 'mesh') or a strings change ('strings')."""
        if not hasattr(self, "_triUndo"):
            self._triUndo = []
        self._triUndo.append(entry)
        del self._triUndo[:-50]
        if not keep_redo:
            self._redo = []

    # ------------------------------------------------------------ undo / redo (Ctrl+Z / Ctrl+Y)
    def _strSnapshot(self):
        """The whole strings layer (attributes + geometry) - a strings undo step."""
        lyr = getattr(self.store, "layer", None)
        if lyr is None:
            return None
        names = [n for n in lyr.fields().names() if n.lower() != "fid"]
        feats, keys = [], []
        for f in lyr.getFeatures():
            attrs = [f[n] for n in names]
            wkb = f.geometry().asWkb()
            feats.append((attrs, wkb))
            keys.append((repr(attrs), bytes(wkb)))
        keys.sort()
        return {"path": self.store.path, "names": names, "feats": feats, "key": hash(tuple(keys))}

    def _strChanged(self, *_):
        """Strings changed (here, in the 3D view or in QGIS): the state before becomes an undo step."""
        try:
            cur = self._strSnapshot()
        except Exception as e:
            log(f"Strings undo snapshot failed: {e}")
            return
        last = getattr(self, "_strLast", None)
        self._strLast = cur
        if getattr(self, "_strRestoring", False) or cur is None or last is None or last["path"] != cur["path"]:
            return
        if cur["key"] != last["key"]:
            self._pushTriUndo(("strings", last))

    def _strRestore(self, snap):
        from qgis.core import QgsFeature, QgsGeometry
        from ..core.q2thook import is_protected
        lyr = self.store.layer
        if lyr is None or snap is None or snap["path"] != self.store.path:
            raise RuntimeError("the strings file has changed since")
        if is_protected(self.store.path):
            raise PermissionError("Protected folder")
        fields = lyr.fields()
        feats = []
        for attrs, wkb in snap["feats"]:
            f = QgsFeature(fields)
            for n, v in zip(snap["names"], attrs):
                if fields.indexOf(n) >= 0:
                    f[n] = v
            g = QgsGeometry()
            g.fromWkb(wkb)
            f.setGeometry(g)
            feats.append(f)
        ids = [f.id() for f in lyr.getFeatures()]
        self._strRestoring = True
        try:
            if lyr.isEditable():
                lyr.deleteFeatures(ids)
                lyr.addFeatures(feats)
            else:
                pr = lyr.dataProvider()
                pr.deleteFeatures(ids)
                pr.addFeatures(feats)
                lyr.reload()
            lyr.triggerRepaint()
            self.store.changed.emit()
        finally:
            self._strRestoring = False
            self._strLast = self._strSnapshot()
        self.hook.js("window.Q2T4 && Q2T4.setSelectedStrings([])")
        self._refreshEditItems()

    def _meshCopy(self, mid):
        import numpy as np
        from ..core.mesh import Mesh
        m = self.hook.meshes.get(mid)
        if m is None:
            return None
        return Mesh(np.array(m.vertices, float), np.array(m.triangles, np.int64).reshape(-1, 3), name=m.name)

    def undoAny(self):
        """Ctrl+Z: undo the last string or wireframe edit."""
        st = getattr(self, "_triUndo", None)
        if st and st[-1][0] == "strings":
            entry = st.pop()
            try:
                cur = self._strSnapshot()
                self._strRestore(entry[1])
            except Exception as e:
                self.plugin._msg(f"Can't undo: {e}", Qgis.MessageLevel.Warning, 6)
                return
            self._redoPush(("strings", cur))
            self.plugin._msg("Undone: string edit (Ctrl+Y redoes)", Qgis.MessageLevel.Info, 3)
            return
        if st:
            snap = self._meshCopy(st[-1][1])
            if snap is not None:
                self._redoPush(("mesh", st[-1][1], snap))
        elif getattr(self, "_manualTris", None):
            self._redoPush(("manualtri", self._manualTris[-1]))
        else:
            self.plugin._msg("Nothing to undo.", Qgis.MessageLevel.Info, 3)
            return
        self.undoTriangle()

    def _redoPush(self, entry):
        if not hasattr(self, "_redo"):
            self._redo = []
        self._redo.append(entry)
        del self._redo[:-50]

    def redoAny(self):
        """Ctrl+Y / Ctrl+Shift+Z: redo what Ctrl+Z undid."""
        r = getattr(self, "_redo", None)
        if not r:
            self.plugin._msg("Nothing to redo.", Qgis.MessageLevel.Info, 3)
            return
        entry = r.pop()
        try:
            if entry[0] == "strings":
                cur = self._strSnapshot()
                self._strRestore(entry[1])
                self._pushTriUndo(("strings", cur), keep_redo=True)
                self.plugin._msg("Redone: string edit", Qgis.MessageLevel.Info, 3)
            elif entry[0] == "mesh":
                mid, snap = entry[1], entry[2]
                m = self.hook.meshes.get(mid)
                if m is None:
                    raise RuntimeError("that wireframe is no longer in the 3D view")
                self._pushTriUndo(("mesh", mid, self._meshCopy(mid)), keep_redo=True)
                self._replaceMesh(mid, m, snap.triangles, "redone", vertices=snap.vertices)
            elif entry[0] == "manualtri":
                self._manualTris = getattr(self, "_manualTris", []) + [entry[1]]
                self._showTriangles()
        except Exception as e:
            self.plugin._msg(f"Can't redo: {e}", Qgis.MessageLevel.Warning, 6)

    def undoTriangle(self):
        import numpy as np
        if getattr(self, "_triUndo", None):
            entry = self._triUndo.pop()
            m = self.hook.meshes.get(entry[1])
            if m is None:
                self.plugin._msg("That wireframe is no longer in the 3D view.", Qgis.MessageLevel.Warning, 5)
                return
            if entry[0] == "mesh":
                _, mid, old = entry
                self._replaceMesh(mid, m, old.triangles, "repair undone", vertices=old.vertices)
                return
            if entry[0] == "del":
                _, mid, face, tri = entry
                tris = np.insert(np.asarray(m.triangles), min(face, len(m.triangles)), tri, axis=0)
                self._replaceMesh(mid, m, tris, "triangle put back")
            else:
                _, mid, nt, nv = entry
                self._replaceMesh(mid, m, np.asarray(m.triangles)[:nt], "added triangles undone",
                                  vertices=np.asarray(m.vertices)[:nv])
            return
        if getattr(self, "_manualTris", None):
            self._manualTris.pop()
            self._showTriangles()

    # ------------------------------------------------------------ delete triangles
    def setTriDelete(self, on):
        if on:
            if not self.hook.js("!!(window.Q2T4 && Q2T4.startTriDelete())", wait=True):
                self._mirrorCheck(self.bTriDel, False)
        else:
            self.hook.js("window.Q2T4 && Q2T4.stopTriDelete()")

    def _deleteTriangle(self, ev):
        import numpy as np
        mid, face = ev.get("id"), int(ev.get("face", -1))
        m = self.hook.meshes.get(mid)
        if m is None:
            return
        T = np.asarray(m.triangles)
        V = np.asarray(m.vertices, float)
        pts = np.asarray(ev.get("pts") or [], float)
        if not (0 <= face < len(T)) or (len(pts) == 3 and
                                        np.abs(np.sort(V[T[face]], axis=0) - np.sort(pts, axis=0)).max() > 0.01):
            # the view's face number doesn't match (mesh changed meanwhile): find the triangle by its corners
            if len(pts) != 3:
                return
            d = np.array([np.abs(np.sort(V[t], axis=0) - np.sort(pts, axis=0)).max() for t in T])
            face = int(np.argmin(d))
            if d[face] > 0.01:
                self.plugin._msg("Triangle not found - try again.", Qgis.MessageLevel.Warning, 4)
                return
        if len(T) <= 1:
            self.plugin._msg("That's the last triangle - remove the whole wireframe from Objects instead.",
                             Qgis.MessageLevel.Warning, 6)
            return
        self._pushTriUndo(("del", mid, face, T[face].copy()))
        self._replaceMesh(mid, m, np.delete(T, face, axis=0), "triangle deleted")

    # ------------------------------------------------------------ validation
    def _validateEdited(self):
        mids = [m for m in getattr(self, "_editedWfs", set()) if m in self.hook.meshes]
        self._editedWfs = set()
        if mids:
            self.validateWireframes(mids, quiet_if_clean=True)

    def validateSelected(self):
        """Validate button: the wireframes selected in the Wireframing box (all, if none is)."""
        mids = [d[1] for d in (self._key(it) for it in self.lstStrings.selectedItems())
                if isinstance(d, (tuple, list)) and d[0] == "wf"]
        mids = mids or [k for k in self.hook.meshes if not str(k).startswith("bm:")]
        if not mids:
            self.plugin._msg("No wireframe in the 3D view.", Qgis.MessageLevel.Info, 5)
            return
        self.validateWireframes(mids, quiet_if_clean=False)

    def _markProblems(self, reports):
        """Open edges red, edges shared by 3+ triangles magenta, bad triangles yellow (3D overlays)."""
        import numpy as np
        from ..core.strings import polyline_payload
        oe = [np.asarray(e) for r in reports for e in r.get("open_edge_xyz", [])]
        nm = [np.asarray(e) for r in reports for e in r.get("nonmanifold_edge_xyz", [])]
        bt = [np.vstack([t, t[:1]]) for r in reports for t in map(np.asarray, r.get("bad_triangle_xyz", []))]
        for key, polys, col in (("check:open", oe, "#ff2020"), ("check:nonmanifold", nm, "#ff2bd6"),
                                ("check:bad", bt, "#ffe000")):
            spec = polyline_payload(polys, col, snap="ends", point_size=0) if polys else None
            if spec:
                spec["snap"] = ""            # marks only: nothing to snap to
                spec["onTop"] = True
            self.hook.setOverlay(key, spec)

    def clearProblemMarks(self):
        for key in ("check:open", "check:nonmanifold", "check:bad"):
            if key in self.hook.overlays:
                self.hook.setOverlay(key, None)

    def validateWireframes(self, mids, quiet_if_clean=False):
        """Check wireframes; clean ones get a short message, problems a prompt with Show / Repair."""
        from qgis.PyQt.QtWidgets import QMessageBox
        from ..core.validate import validate, repair
        reports = {mid: validate(self.hook.meshes[mid]) for mid in mids if mid in self.hook.meshes}
        bad = {mid: r for mid, r in reports.items() if r["issues"]}
        if not bad:
            self.clearProblemMarks()
            names = ", ".join(self.hook.meshes[m].name for m in reports)
            txt = []
            for m, r in reports.items():
                txt.append(f"{self.hook.meshes[m].name}: " + (f"closed solid, {r['volume']:,.1f} m³" if r.get("closed")
                                                              else "OK"))
            if quiet_if_clean:
                self.plugin._msg("Wireframe check passed - " + "; ".join(txt), Qgis.MessageLevel.Success, 6)
            else:
                QMessageBox.information(self, "Wireframe check", "No problems found.\n\n" + "\n".join(txt))
            return reports
        lines = []
        for mid, r in bad.items():
            lines.append(f"{self.hook.meshes[mid].name}  ({r['triangles']:,} triangles):")
            lines += [f"   • {i}" for i in r["issues"]]
        clean = [self.hook.meshes[m].name for m in reports if m not in bad]
        if clean:
            lines.append("No problems: " + ", ".join(clean))
        mb = QMessageBox(QMessageBox.Icon.Warning, "Wireframe check", "\n".join(lines), parent=self)
        bShow = mb.addButton("Show problems", QMessageBox.ButtonRole.ActionRole)
        fixable = any(r.get("coincident") or r.get("degenerate") or r.get("duplicates") or r.get("inconsistent_edges")
                      or r.get("inside_out") for r in bad.values())
        bFix = mb.addButton("Repair", QMessageBox.ButtonRole.AcceptRole) if fixable else None
        mb.addButton(QMessageBox.StandardButton.Close)
        mb.exec()
        if mb.clickedButton() is bShow:
            self._markProblems(list(bad.values()))
            self.plugin._msg("Problems marked in 3D: open edges red, edges on 3+ triangles magenta, bad triangles "
                             "yellow. Remove the marks in Objects (Wireframe check) or with a clean check.",
                             Qgis.MessageLevel.Info, 10)
        elif bFix is not None and mb.clickedButton() is bFix:
            for mid in bad:
                m = self.hook.meshes[mid]
                new, done = repair(m)
                if done:
                    self._pushTriUndo(("mesh", mid, m))
                    self._replaceMesh(mid, m, new.triangles, "repaired: " + ", ".join(done), vertices=new.vertices)
            self._editedWfs = set()
            left = {mid: validate(self.hook.meshes[mid]) for mid in bad}
            rest = [i for r in left.values() for i in r["issues"]]
            if rest:
                self._markProblems(list(left.values()))
                QMessageBox.information(self, "Wireframe check", "Repaired what could be fixed automatically. "
                                        "Still to do by hand (marked in 3D):\n\n" + "\n".join(f"• {i}" for i in rest))
            else:
                self.clearProblemMarks()
                self.plugin._msg("Repaired - the wireframe check now passes.", Qgis.MessageLevel.Success, 6)
        return reports

    def _replaceMesh(self, mid, m, tris, what, vertices=None):
        """Show (and for saved wireframes, save) mesh mid with new triangles; unused vertices are kept."""
        import numpy as np
        from ..core import editing as E
        from ..core.mesh import Mesh
        V = np.asarray(m.vertices if vertices is None else vertices, float)
        if not hasattr(self, "_editedWfs"):
            self._editedWfs = set()
        if mid != "manual":
            self._editedWfs.add(mid)
        new = Mesh(V, np.asarray(tris, np.int64).reshape(-1, 3), name=m.name,
                   source=getattr(m, "source", ""))
        spec = json.loads(self.hook.payloads[mid])
        self.hook.addMesh(new, color=spec.get("color"), edgeColor=spec.get("edgeColor", "#1a1a1a"),
                          opacity=spec.get("opacity", 1.0), mid=mid, zoom=False)
        if mid == "manual":                                    # keep the manual triangle list in step
            V = new.vertices
            self._manualTris = [[V[a].tolist(), V[b].tolist(), V[c].tolist()] for a, b, c in new.triangles]
        elif str(mid).startswith("built") and self.wireframeGpkg():
            try:
                E.update_saved_wireframe(self.wireframeGpkg(), int(str(mid)[5:]), new,
                                         self.store.layer.crs() if self.store.layer is not None else None)
            except Exception as e:
                self.plugin._msg(f"Shown, but not saved to the GeoPackage: {e}", Qgis.MessageLevel.Warning, 8)
        st = new.stats()
        self.plugin._msg(f"{new.name}: {what} - {len(new.triangles):,} triangles, " +
                         (f"closed, volume {st['volume']:,.1f} m³" if st.get("closed")
                          else f"{st.get('open_edges', 0)} open edges"), Qgis.MessageLevel.Info, 5)

    def clearTriangles(self):
        self._manualTris = []
        self._showTriangles()

    def saveTriangles(self):
        from ..core import editing as E
        from ..core.strings import save_wireframe
        import copy
        tris = getattr(self, "_manualTris", [])
        if not tris:
            self.plugin._msg("No triangles yet: Add triangles, then click three corners per triangle.",
                             Qgis.MessageLevel.Warning, 6)
            return
        try:
            self.ensureStore()
            name = self._askWfName(self._newWfDefault())
            if name is None:
                return
            import numpy as np
            others = [np.asarray(mm.vertices, float) for k, mm in self.hook.meshes.items()
                      if k != "manual" and len(mm.vertices) <= 300000]
            if others:
                tris = E.snap_corners(tris, np.vstack(others))       # exact wireframe vertex positions
            m = E.triangles_mesh(tris, name=name)
            disk = copy.copy(m)
            disk.vertices = self.store.to_layer_crs(m.vertices)
            wf_no = save_wireframe(self.wireframeGpkg(), disk, name, (), self.spSG.value(), m.stats(),
                                   crs=self.store.layer.crs() if self.store.layer is not None else None)
        except Exception as e:
            self.plugin._msg(f"Triangles not saved: {e}", Qgis.MessageLevel.Critical, 10)
            return
        m.name = f"{name} #{wf_no}"
        self.hook.addMesh(m, color="#e0503c", opacity=0.75, mid=f"built{wf_no}", zoom=False)
        self.clearTriangles()
        self._setActiveWf(f"built{wf_no}")
        self.plugin._msg(f"Saved as wireframe #{wf_no} ({len(m.triangles)} triangles)", Qgis.MessageLevel.Success, 6)
        self.validateWireframes([f"built{wf_no}"], quiet_if_clean=True)

    def addTrianglesToWireframe(self):
        """Move the pending (blue) triangles into a wireframe chosen from a list."""
        tris = getattr(self, "_manualTris", [])
        if not tris:
            self.plugin._msg("No pending (blue) triangles to move.", Qgis.MessageLevel.Warning, 6)
            return
        mid = self.wfTarget()
        if mid is None:
            self.plugin._msg("Select the wireframe to put them into in the Wireframing list first.",
                             Qgis.MessageLevel.Warning, 6)
            return
        self.mergeTriangles(mid, [list(t) for t in tris], f"{len(tris)} triangle(s) added")
        self.clearTriangles()

    def _applyVertexEdit(self, ev):
        from ..core import editing as E
        oid, i = str(ev.get("id")), int(ev.get("i"))
        try:
            if oid.startswith("s:"):
                fid = int(oid[2:])
                if ev["type"] == "vertexMoved":
                    E.move_string_vertex(self.store, fid, i, ev["p"])
                else:
                    E.delete_string_vertex(self.store, fid, i)
                self._refreshEditItems()          # the view shows exactly what was saved (no stale points)
            elif oid.startswith("m:"):
                mid = oid[2:]
                if ev["type"] != "vertexMoved":
                    self.plugin._msg("Wireframe vertices can be moved, not deleted (the triangles need them).",
                                     Qgis.MessageLevel.Warning, 6)
                    return
                m = E.move_mesh_vertex(self.hook, mid, i, ev["p"])
                if mid.startswith("built") and self.wireframeGpkg():
                    E.update_saved_wireframe(self.wireframeGpkg(), int(mid[5:]), m,
                                             self.store.layer.crs() if self.store.layer is not None else None)
                st = m.stats()
                self.plugin._msg(f"{m.name}: vertex {i} moved" +
                                 (f" - volume now {st['volume']:,.1f} m³" if st.get("closed") else " - mesh is open"),
                                 Qgis.MessageLevel.Info, 5)
        except Exception as e:
            self.plugin._msg(f"Edit not saved: {e}", Qgis.MessageLevel.Critical, 10)

    # ------------------------------------------------------------ saved views
    def _views(self):
        raw, ok = QgsProject.instance().readEntry(VIEWS_SCOPE, VIEWS_KEY, "[]")
        if not ok or raw in ("", "[]"):
            raw, ok = QgsProject.instance().readEntry(OLD_VIEWS_SCOPE, VIEWS_KEY, "[]")
        try:
            return json.loads(raw) if raw else []
        except ValueError:
            return []

    def _setViews(self, views):
        QgsProject.instance().writeEntry(VIEWS_SCOPE, VIEWS_KEY, json.dumps(views))
        self._fillViews()

    def _buildViews(self):
        from . import panelwidgets as P
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(5)
        self.lstViews = QListWidget()
        self.lstViews.setMinimumHeight(90)
        self.lstViews.setMaximumHeight(160)
        self.lstViews.setAlternatingRowColors(True)
        self.lstViews.setToolTip("Camera positions (and sections) saved in the project. Double-click to go to one.")
        self.lstViews.itemDoubleClicked.connect(lambda it: self.goView(self.lstViews.row(it)))
        v.addWidget(self.lstViews)
        bSave = QPushButton(P.icon("save"), "Save view…")
        bSave.setToolTip("Remember the camera (and section) as a named view")
        bSave.clicked.connect(self.saveView)
        v.addLayout(P.hrow(bSave, None,
                           P.tool("back", "Previous view", lambda: self.stepView(-1)),
                           P.tool("fwd", "Next view", lambda: self.stepView(+1)),
                           P.tool("look", "Go to the selected view", lambda: self.goView(self.lstViews.currentRow())),
                           P.tool("/mActionDeleteSelected.svg", "Forget the selected view", self.deleteView)))
        self._fillViews()
        try:
            QgsProject.instance().readProject.connect(lambda *_: self._fillViews())
        except Exception:
            pass
        return w

    def _fillViews(self):
        if not hasattr(self, "lstViews"):
            return
        cur = self.lstViews.currentRow()
        self.lstViews.clear()
        for vw in self._views():
            self.lstViews.addItem(QListWidgetItem(vw.get("name", "view")))
        if 0 <= cur < self.lstViews.count():
            self.lstViews.setCurrentRow(cur)

    def saveView(self):
        raw = self.hook.js("window.Q2T4 && Q2T4.getView ? Q2T4.getView() : ''", wait=True)
        if not raw:
            self.plugin._msg("Open the 3D view first.", Qgis.MessageLevel.Warning, 5)
            return
        vw = json.loads(raw)
        views = self._views()
        name, ok = QInputDialog.getText(self, "Save view", "Name:", text=f"View {len(views) + 1}")
        if not ok:
            return
        vw["name"] = name or f"View {len(views) + 1}"
        vw["offset"] = getattr(self, "offset", 0.0)
        s = self.currentSection()
        if s is not None and self.baseSection is not None:
            vw["sec"] = {"a": list(self.baseSection.a), "b": list(self.baseSection.b), "width": s.width,
                         "offset": self.offset}
        views.append(vw)
        self._setViews(views)
        self.lstViews.setCurrentRow(len(views) - 1)

    def goView(self, i):
        views = self._views()
        if not (0 <= i < len(views)):
            return
        vw = views[i]
        if vw.get("sec"):
            try:
                sc = vw["sec"]
                self.spWidth.setValue(sc["width"])
                self.setSection(tuple(sc["a"]), tuple(sc["b"]), look=False)
                self.offset = sc.get("offset", 0.0)
                self._update()
            except Exception as e:
                log(f"View section not restored: {e}")
        self.hook.js(f"window.Q2T4 && Q2T4.setViewState({json.dumps(vw)})")
        self.lstViews.setCurrentRow(i)

    def stepView(self, d):
        n = self.lstViews.count()
        if n:
            self.goView((self.lstViews.currentRow() + d) % n if self.lstViews.currentRow() >= 0 else 0)

    def deleteView(self):
        i = self.lstViews.currentRow()
        views = self._views()
        if 0 <= i < len(views):
            views.pop(i)
            self._setViews(views)

    # ------------------------------------------------------------ section plot PDF
    def sectionPlotDialog(self):
        w = self.hook.window()
        if w is None:
            self.plugin._msg("Open the 3D view first.", Qgis.MessageLevel.Warning, 5)
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("Section plot (PDF)")
        f = QFormLayout(dlg)
        s = self.currentSection()
        prj = QgsProject.instance()
        leTitle = QLineEdit(prj.baseName() or "Section")
        f.addRow("Title", leTitle)
        leSub = QLineEdit(self._plotSubtitle())
        f.addRow("Subtitle", leSub)
        cbPage = QComboBox()
        for k in ("A3 landscape", "A4 landscape", "A3 portrait", "A4 portrait", "A2 landscape"):
            cbPage.addItem(k)
        cbPage.setCurrentText(QSettings().value("/QGeo3D/plotPage", "A3 landscape"))
        f.addRow("Page", cbPage)
        leBy = QLineEdit(QSettings().value("/QGeo3D/plotBy", ""))
        f.addRow("Drawn by", leBy)
        cbKeep = QCheckBox("Keep the layout in the project (edit it in Layout Manager)")
        f.addRow("", cbKeep)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        f.addRow(bb)
        if not dlg.exec():
            return
        QSettings().setValue("/QGeo3D/plotPage", cbPage.currentText())
        QSettings().setValue("/QGeo3D/plotBy", leBy.text())
        start = QSettings().value("/QGeo3D/plotDir", "")
        name = (leTitle.text() or "section").replace(" ", "_")
        path, _ = QFileDialog.getSaveFileName(self, "Save section plot", os.path.join(start, name + ".pdf"),
                                              "PDF (*.pdf)")
        if not path:
            return
        if is_protected(path):
            QMessageBox.warning(self, "Section plot", "That folder is protected - choose another.")
            return
        QSettings().setValue("/QGeo3D/plotDir", os.path.dirname(path))
        try:
            from ..core.plot import section_pdf
            img = grab_view(w, self.hook)
            legend = self._plotLegend()
            section_pdf(path, img, leTitle.text(), leSub.text(), cbPage.currentText(), legend, leBy.text(),
                        keep=cbKeep.isChecked())
        except Exception as e:
            self.plugin._msg(f"Section plot failed: {e}", Qgis.MessageLevel.Critical, 10)
            return
        self.plugin._msg(f"Section plot saved: {path}", Qgis.MessageLevel.Success, 8)

    def _plotSubtitle(self):
        s = self.currentSection()
        prj = QgsProject.instance().crs()
        parts = []
        if s is not None:
            cx, cy = s.centre
            parts.append(f"Section {s.azimuth:.1f}° looking {s.view_azimuth:.1f}° · width {s.width:g} m · "
                         f"centre {cx:,.1f} E {cy:,.1f} N · offset {self.offset:+.1f} m")
        parts.append(prj.description() or prj.authid())
        return "  ·  ".join(parts)

    def _plotLegend(self):
        out = []
        bm = getattr(self, "bm", None)
        if bm is not None and getattr(self, "bmId", None) in self.hook.blockModels:
            out.append((f"Block model {bm.name}: {bm.colorField}", [(lab, col) for lab, col, _n in bm.legend()]))
        txt = self.lblDhLegend.text() if hasattr(self, "lblDhLegend") else ""
        if txt and "drill" in self.hook.tubes:
            import re
            pairs = re.findall(r"color:(#[0-9a-fA-F]{6})'>■</span> ([^<]+?)(?=\s*<span|$)", txt)
            if pairs:
                import html as _h
                out.append((f"Drillholes: {self.cbDhField.currentText()}", [(_h.unescape(l.strip()), c) for c, l in pairs]))
        wf = [(m.name, None) for mid, m in self.hook.meshes.items() if mid not in self.hook.hiddenMeshes
              and not str(mid).startswith("bm:")]
        if wf:
            out.append(("Wireframes", wf[:12]))
        return out


class LeapfrogMixin:
    """Open Mining Format (.omf) and Leapfrog files in the panel."""

    def openOmfDialog(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open Mining Format file", QSettings().value("/QGeo3D/omfDir", ""),
                                              "Open Mining Format (*.omf);;Leapfrog mesh (*.msh)")
        if path:
            QSettings().setValue("/QGeo3D/omfDir", os.path.dirname(path))
            if path.lower().endswith(".msh"):
                return self.plugin.addFile(path)
            return self.openOmf(path)

    def openOmf(self, path, crs=None, uids=None):
        import numpy as np
        from ..core import omf1
        from ..core.crs import transform_xyz
        from .pickdialog import pick_items
        try:
            items = omf1.summary(path)
        except Exception as e:
            self.plugin._msg(f"{os.path.basename(path)}: {e}", Qgis.MessageLevel.Critical, 12)
            return None
        if not items:
            self.plugin._msg(f"{os.path.basename(path)} holds no elements.", Qgis.MessageLevel.Warning, 8)
            return None
        if uids is None:
            rows = [(f"{nm}  ·  {kind}" + (f"  ·  {info}" if info else ""), kind != "grid surface", "")
                    for _u, nm, kind, info in items]
            sel = pick_items(self, "Open Mining Format", f"<b>{os.path.basename(path)}</b>: tick the elements to open.",
                             rows) if len(items) > 1 else [0]
            if not sel:
                return None
            uids = [items[i][0] for i in sel]
        els = []
        for u in uids:
            try:
                els.append(omf1.read_element(path, u))
            except Exception as e:
                self.plugin._msg(str(e), Qgis.MessageLevel.Warning, 8)
        if not els:
            return None
        if crs is None:
            from .crsdialog import ask_source_crs
            pts = [e.get("vertices") if e["kind"] != "blocks" else e["xyz"] @ e["rot"].T + e["origin"] for e in els]
            crs = ask_source_crs(self, path, np.vstack([p for p in pts if p is not None and len(p)]))
            if crs is None:
                return None
        prj = self.projectCrs()
        name0 = os.path.splitext(os.path.basename(path))[0]
        done = 0
        for e in els:
            k = e["kind"]
            if k == "surface":
                from ..core.mesh import Mesh
                m = Mesh(e["vertices"], e["triangles"], e["name"], path)
                m.src_vertices, m.src_crs = m.vertices.copy(), crs
                m.vertices = transform_xyz(m.src_vertices, crs, prj)
                res = self.plugin.addMesh(m, **({"color": e["color"]} if e.get("color") else {}))
                if res:
                    self.registerImport(res[0], "mesh", path, crs)
                    done += 1
            elif k in ("lines", "points"):
                from ..core.strings import mixed_payload
                v = transform_xyz(e["vertices"], crs, prj)
                if k == "lines":
                    polys, cur = [], None
                    for a, b in e["segments"]:
                        if cur is not None and cur[-1] == a:
                            cur.append(int(b))
                        else:
                            cur = [int(a), int(b)]
                            polys.append(cur)
                    pl = [v[c] for c in polys]
                else:
                    pl = [v[i:i + 1] for i in range(len(v))]
                key = f"omf:{name0}:{e['name']}"
                self.hook.setOverlay(key, mixed_payload(pl, e.get("color") or "#00c8ff", point_size=4))
                self.registerImport(key, "overlay", path, crs)
                done += 1
            elif k == "blocks":
                from ..core.blockmodel import BlockModel
                d = {"name": e["name"], "fields": [], "data": {}, "xyz": e["xyz"], "size": e["size"],
                     "rot": e["rot"], "origin": e["origin"], "notes": []}
                n = len(e["xyz"])
                for nm, loc, arr, legend in e["data"]:
                    if len(arr) != n:
                        continue
                    if legend is not None:
                        d["data"][nm] = np.array([legend[int(i)] if 0 <= int(i) < len(legend) else None for i in arr],
                                                 object)
                    else:
                        d["data"][nm] = np.asarray(arr, float)
                    d["fields"].append(nm)
                keep = np.ones(n, bool)
                num = [f for f in d["fields"] if d["data"][f].dtype != object]
                if num:                                  # cells with no value at all are empty (outside the model)
                    keep = np.any([np.isfinite(d["data"][f]) for f in num], axis=0)
                d["xyz"], d["size"] = d["xyz"][keep], d["size"][keep]
                d["data"] = {k2: v2[keep] for k2, v2 in d["data"].items()}
                bm = BlockModel.from_dict(d, path)
                self._addBlockModel(bm, path, crs=crs)
                done += 1
        self.plugin._msg(f"{done} element(s) from {os.path.basename(path)}", Qgis.MessageLevel.Success, 6)
        return done


def grab_view(w, hook=None):
    """Image of the 3D view with its HTML overlays (grid, scale bar, labels). Qgis2threejs 2.x: grab
    the web view widget. 3.x: the view is a native window of another process, so the screen area it
    covers is captured (the window must be on screen); failing that, the bare canvas from JavaScript."""
    view = getattr(w.ui, "webView", None)
    if view is not None:
        return view.grab().toImage()
    from qgis.PyQt.QtCore import QPoint
    area = getattr(w.ui, "webViewContainer", None) or w.centralWidget()
    if area is not None and area.isVisible() and not w.isMinimized():
        w.raise_()
        w.activateWindow()
        QApplication.processEvents()
        tl = area.mapToGlobal(QPoint(0, 0))
        scr = area.screen() if hasattr(area, "screen") else QApplication.primaryScreen()
        g = scr.geometry()
        pm = scr.grabWindow(0, tl.x() - g.x(), tl.y() - g.y(), area.width(), area.height())
        if not pm.isNull():
            return pm.toImage()
    if hook is not None:
        import base64
        from qgis.PyQt.QtGui import QImage
        url = hook.js("(function(){app.render(true); return app.renderer.domElement.toDataURL('image/png');})()",
                      wait=True) or ""
        if url.startswith("data:image"):
            img = QImage()
            img.loadFromData(base64.b64decode(url.split(",", 1)[1]))
            return img
    raise RuntimeError("could not capture the 3D view - bring its window on screen and try again")
