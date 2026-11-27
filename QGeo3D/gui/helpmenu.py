# -*- coding: utf-8 -*-
"""QGeo 3D ▸ Help: Discussions, Report a problem, Shortcut keys, User guide, About.
Everything here only opens a web page or shows a dialog - nothing is sent from the plugin."""

import os
import sys

from qgis.PyQt.QtCore import Qt, QUrl
from qgis.PyQt.QtGui import QDesktopServices
from qgis.PyQt.QtWidgets import QDialog, QDialogButtonBox, QLabel, QVBoxLayout

REPO = "https://github.com/HandleTheJandal/QGeo3D"
DISCUSSIONS = REPO + "/discussions"
ISSUES_NEW = REPO + "/issues/new"
GUIDE = REPO + "#readme"
PLUGIN_PAGE = "https://plugins.qgis.org/plugins/QGeo3D/"

SHORTCUTS = [
    ("3D view and QGeo 3D panel", [
        ("D", "Digitise in 3D on / off"),
        ("E", "Select strings"),
        ("M", "Move vertices"),
        ("I", "Insert vertices"),
        ("U", "Cut strings"),
        ("C", "Copy strings"),
        ("O", "Close the selected strings"),
        ("Ctrl+Z or Backspace", "Undo: the last click of what you're doing (a point, a corner, a picked string), "
                                "otherwise the last finished edit"),
        ("Ctrl+Y / Ctrl+Shift+Z", "Redo"),
    ]),
    ("While digitising, measuring or adding triangles", [
        ("S", "Snapping on / off"),
        ("T", "Trace along a string (digitising)"),
        ("Enter / right-click / double-click", "Finish the string"),
        ("Esc", "Cancel the string (twice: stop digitising)"),
    ]),
    ("Edit strings", [
        ("Delete", "Delete the selected strings / points (Select), or the vertex under the cursor (Move)"),
        ("F2", "Rename the selected string"),
    ]),
    ("Qgis2threejs (3D view)", [
        ("L", "Labels on / off"),
        ("W", "Wireframe view on / off"),
        ("R", "Auto-rotate on / off"),
        ("Shift+R", "Reset the view"),
        ("Shift+S", "Save image"),
        ("Shift+I", "Qgis2threejs info (plain I is Insert in QGeo 3D)"),
    ]),
]


def open_url(url):
    QDesktopServices.openUrl(QUrl(url))


def _version(pkg_dir):
    try:
        with open(os.path.join(pkg_dir, "metadata.txt"), encoding="utf-8") as f:
            for line in f:
                if line.startswith("version="):
                    return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return "?"


PKG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _dialog(parent, title, html, width=520):
    dlg = QDialog(parent)
    dlg.setWindowTitle(title)
    dlg.setMinimumWidth(width)
    lay = QVBoxLayout(dlg)
    lab = QLabel(html)
    lab.setTextFormat(Qt.TextFormat.RichText)
    lab.setOpenExternalLinks(True)
    lab.setWordWrap(True)
    lay.addWidget(lab)
    bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
    bb.rejected.connect(dlg.reject)
    lay.addWidget(bb)
    dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    dlg.show()
    return dlg


def shortcuts_html():
    rows = []
    for group, keys in SHORTCUTS:
        rows.append(f"<tr><td colspan='2' style='padding-top:8px'><b>{group}</b></td></tr>")
        for k, what in keys:
            rows.append(f"<tr><td style='padding-right:14px; white-space:nowrap'><code>{k}</code></td>"
                        f"<td>{what}</td></tr>")
    return "<table>" + "".join(rows) + "</table>"


def show_shortcuts(parent):
    return _dialog(parent, "Shortcut keys - QGeo 3D", shortcuts_html(), 560)


def show_about(parent):
    q2t = sys.modules.get("Qgis2threejs")
    q2t_ver = _version(os.path.dirname(q2t.__file__)) if q2t is not None else "not loaded"
    from qgis.core import Qgis
    html = (f"<h3>QGeo 3D {_version(PKG_DIR)}</h3>"
            "<p>Mining 3D for the Qgis2threejs window: wireframes, block models, drillholes, sections, "
            "strings and point clouds, with Micromine / Datamine / Surpac / Vulcan / Leapfrog files.</p>"
            f"<p>By Jandals · GNU GPL v2 or later<br>Needs Qgis2threejs (installed: {q2t_ver}) · "
            f"QGIS {Qgis.QGIS_VERSION}</p>"
            f"<p><a href='{REPO}'>GitHub</a> · <a href='{DISCUSSIONS}'>Discussions</a> · "
            f"<a href='{PLUGIN_PAGE}'>plugins.qgis.org</a></p>")
    return _dialog(parent, "About QGeo 3D", html)


def issue_url(title, body, limit=6000):
    """New-issue link with the title and report filled in (long reports are cut; the full text is on the
    clipboard)."""
    from urllib.parse import quote
    if len(body) > limit:
        body = body[:limit] + "\n\n[...cut short - the full report is on your clipboard: paste it here]"
    return f"{ISSUES_NEW}?title={quote(title)}&body={quote(body)}"
