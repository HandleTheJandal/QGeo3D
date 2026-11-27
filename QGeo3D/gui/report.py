# -*- coding: utf-8 -*-
"""Problem reports: QGeo 3D menu > Report a problem..., and an offer to report when QGeo 3D hits an error.

Nothing is sent by itself: the report is shown first (it can be edited), then copied and/or opened in
the user's email program addressed to REPORT_EMAIL."""
import contextlib
import collections
import datetime
import os
import platform
import sys
import traceback
from urllib.parse import quote

from qgis.PyQt.QtCore import Qt, QUrl
from qgis.PyQt.QtGui import QDesktopServices, QGuiApplication
from qgis.PyQt.QtWidgets import (QDialog, QDialogButtonBox, QLabel, QLineEdit, QPlainTextEdit, QPushButton,
                                 QVBoxLayout)
from qgis.core import Qgis, QgsApplication, QgsProject

REPORT_EMAIL = "qgeo3d@gmail.com"
PKG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAILTO_MAX = 1800                      # mail programs cut long mailto: links; the full text is on the clipboard


class ErrorWatch:
    """Keeps recent QGeo 3D / Qgis2threejs log lines and the last QGeo 3D error, and offers a report
    (message-bar button) when an uncaught error comes from QGeo 3D's own code."""

    def __init__(self, plugin):
        self.plugin = plugin
        self.lines = collections.deque(maxlen=150)
        self.last_error = None
        self._prev_hook = None
        self._offered_at = None

    def install(self):
        QgsApplication.messageLog().messageReceived.connect(self._logged)
        self._prev_hook = sys.excepthook
        sys.excepthook = self._hook

    def remove(self):
        try:
            QgsApplication.messageLog().messageReceived.disconnect(self._logged)
        except (TypeError, RuntimeError):
            pass
        if sys.excepthook is self._hook and self._prev_hook is not None:
            sys.excepthook = self._prev_hook

    def _logged(self, msg, tag, level):
        if tag in ("QGeo 3D", "Qgis2threejs") or (tag in ("Python error", "Python warning") and "QGeo3D" in msg):
            lvl = {0: "info", 1: "warning", 2: "critical", 3: "success"}.get(int(getattr(level, "value", level)), "?")
            self.lines.append(f"{datetime.datetime.now():%H:%M:%S} [{tag}/{lvl}] {msg[:600]}")

    def _hook(self, etype, value, tb):
        try:
            text = "".join(traceback.format_exception(etype, value, tb))
            ours = PKG_DIR.lower() in text.lower() or "qgeo3d" in text.lower()
            if ours:
                self.last_error = (datetime.datetime.now(), text)
        except Exception:
            ours = False
        if self._prev_hook is not None:
            self._prev_hook(etype, value, tb)          # QGIS's own error window as before
        if ours:
            self._offer()

    def _offer(self):
        """One message-bar button per error burst (not one per repeated error)."""
        now = datetime.datetime.now()
        if self._offered_at and (now - self._offered_at).total_seconds() < 20:
            return
        self._offered_at = now
        with contextlib.suppress(Exception):
            bar = self.plugin.iface.messageBar()
            item = bar.createMessage("QGeo 3D", "Something went wrong in QGeo 3D.")
            b = QPushButton("Report this problem…")
            b.clicked.connect(lambda: (show_report(self.plugin, include_error=True), bar.popWidget(item)))
            item.layout().addWidget(b)
            bar.pushWidget(item, Qgis.MessageLevel.Warning, 30)


def _version(pkg_dir):
    try:
        with open(os.path.join(pkg_dir, "metadata.txt"), encoding="utf-8") as f:
            for line in f:
                if line.startswith("version="):
                    return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return "?"


def diagnostics(plugin, include_error=True):
    from qgis.PyQt.QtCore import QT_VERSION_STR
    q2t = sys.modules.get("Qgis2threejs")
    q2t_ver = _version(os.path.dirname(q2t.__file__)) if q2t is not None else "not loaded"
    try:
        from qgis.core import QgsProjUtils
        proj = f"{QgsProjUtils.projVersionMajor()}.{QgsProjUtils.projVersionMinor()}"
    except Exception:
        proj = "?"
    try:
        from ..core.crs import proj_db_problem
        projp = proj_db_problem() or "ok"
    except Exception as e:
        projp = f"check failed: {e}"
    prj = QgsProject.instance()
    h = plugin.hook
    w = None
    with contextlib.suppress(Exception):
        w = h._window
    out = [
        "QGeo 3D problem report",
        f"Date: {datetime.datetime.now():%Y-%m-%d %H:%M}",
        f"QGeo 3D {_version(PKG_DIR)} | Qgis2threejs {q2t_ver} | QGIS {Qgis.QGIS_VERSION} | Qt {QT_VERSION_STR} | "
        f"Python {platform.python_version()} | PROJ {proj} | {platform.system()} {platform.release()}",
        f"PROJ database: {projp}",
        f"Project CRS: {prj.crs().authid() or prj.crs().description() or 'none'} | 3D window open: "
        f"{bool(w is not None and w.isVisible())}",
        f"3D items: {len(getattr(h, 'meshes', {}))} wireframes, {len(getattr(h, 'blockModels', {}))} block models, "
        f"overlays {sorted(getattr(h, 'overlays', {}))}",
    ]
    ew = getattr(plugin, "errorWatch", None)
    if include_error and ew is not None and ew.last_error:
        when, text = ew.last_error
        out += ["", f"Last QGeo 3D error ({when:%H:%M:%S}):", text.strip()]
    if ew is not None and ew.lines:
        out += ["", "Recent QGeo 3D / Qgis2threejs messages:"] + list(ew.lines)[-40:]
    return "\n".join(out)


class ReportDialog(QDialog):

    def __init__(self, plugin, include_error=True):
        super().__init__(plugin.iface.mainWindow())
        self.setWindowTitle("Report a problem - QGeo 3D")
        self.resize(760, 560)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("What were you doing when it happened? (optional)"))
        self.leWhat = QLineEdit()
        self.leWhat.setPlaceholderText("e.g. ticked a block model in the 3D layer list, then zoomed to it")
        lay.addWidget(self.leWhat)
        lay.addWidget(QLabel(f"Send it by email to <b>{REPORT_EMAIL}</b> (private) or as a GitHub issue (public). "
                             "Nothing is sent until you do it yourself - check and edit it first (it can include "
                             "file and project names)."))
        self.txt = QPlainTextEdit(diagnostics(plugin, include_error))
        self.txt.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        f = self.txt.font()
        f.setFamily("Consolas")
        self.txt.setFont(f)
        lay.addWidget(self.txt, 1)
        bb = QDialogButtonBox()
        bCopy = bb.addButton("Copy", QDialogButtonBox.ButtonRole.ActionRole)
        bMail = bb.addButton("Email…", QDialogButtonBox.ButtonRole.AcceptRole)
        bIssue = bb.addButton("Open as GitHub issue…", QDialogButtonBox.ButtonRole.ActionRole)
        bIssue.setToolTip("Start a public issue on GitHub with this report filled in (you need a GitHub account; "
                          "check it for file or project names you'd rather not publish)")
        bb.addButton(QDialogButtonBox.StandardButton.Close)
        bCopy.clicked.connect(self.copy)
        bMail.clicked.connect(self.email)
        bIssue.clicked.connect(self.issue)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.note = QLabel("")
        self.note.setWordWrap(True)
        lay.addWidget(self.note)

    def text(self):
        what = self.leWhat.text().strip()
        return (f"What I was doing: {what}\n\n" if what else "") + self.txt.toPlainText()

    def copy(self):
        QGuiApplication.clipboard().setText(self.text())
        self.note.setText("Copied - paste it into an email to " + REPORT_EMAIL + ".")

    def email(self):
        body = self.text()
        QGuiApplication.clipboard().setText(body)
        short = body if len(body) <= MAILTO_MAX else body[:MAILTO_MAX] + "\n\n[...cut short - the full report is " \
            "on your clipboard: paste it here (Ctrl+V)]"
        url = f"mailto:{REPORT_EMAIL}?subject={quote('QGeo 3D problem report')}&body={quote(short)}"
        ok = QDesktopServices.openUrl(QUrl(url))
        self.note.setText("Your email program should open with the report - press Send there. The full report is "
                          "also on the clipboard." if ok else
                          f"No email program answered. The report is on the clipboard: paste it into an email to "
                          f"{REPORT_EMAIL}.")


def _issue(self):
    from .helpmenu import issue_url
    body = self.text()
    QGuiApplication.clipboard().setText(body)
    what = self.leWhat.text().strip()
    title = ("QGeo 3D: " + what[:80]) if what else "QGeo 3D problem"
    ok = QDesktopServices.openUrl(QUrl(issue_url(title, "```\n" + body + "\n```")))
    self.note.setText("Your browser should open a new GitHub issue with the report - sign in, check it and press "
                      "Submit. The full report is also on the clipboard." if ok else
                      "No browser answered. The report is on the clipboard.")


ReportDialog.issue = _issue


def show_report(plugin, include_error=True):
    dlg = ReportDialog(plugin, include_error)
    dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    dlg.show()
    return dlg
