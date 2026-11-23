# -*- coding: utf-8 -*-
"""Ask which coordinate system an imported file is in (the file doesn't say)."""

import os

from qgis.PyQt.QtCore import QSettings
from qgis.PyQt.QtWidgets import QCheckBox, QDialog, QDialogButtonBox, QLabel, QVBoxLayout
from qgis.core import QgsCoordinateReferenceSystem, QgsProject
from qgis.gui import QgsProjectionSelectionWidget

from ..core.crs import describe_extent

KEY_FOLDER = "/QGeo3D/importCrs/folder/"
KEY_SKIP = "/QGeo3D/importCrs/skip/"
KEY_LAST = "/QGeo3D/importCrs/last"


def _fkey(folder):
    return os.path.normcase(os.path.abspath(folder)).replace("\\", "/").replace("/", "|")


def remembered(path):
    """(crs, dont_ask) remembered for the file's folder."""
    s = QSettings()
    k = _fkey(os.path.dirname(path))
    c = QgsCoordinateReferenceSystem(s.value(KEY_FOLDER + k, "") or "")
    return (c if c.isValid() else None), s.value(KEY_SKIP + k, False, type=bool)


def remember(path, crs, dont_ask):
    s = QSettings()
    k = _fkey(os.path.dirname(path))
    s.setValue(KEY_FOLDER + k, crs.authid() or crs.toWkt())
    s.setValue(KEY_SKIP + k, bool(dont_ask))
    s.setValue(KEY_LAST, crs.authid() or crs.toWkt())


def forget_folder(path):
    s = QSettings()
    s.remove(KEY_SKIP + _fkey(os.path.dirname(path)))


class CrsDialog(QDialog):

    def __init__(self, parent, title, message, xyz=None, default=None, allow_remember=True):
        super().__init__(parent)
        self.setWindowTitle(title)
        v = QVBoxLayout(self)
        lab = QLabel(message)
        lab.setWordWrap(True)
        v.addWidget(lab)
        if xyz is not None:
            text, hint = describe_extent(xyz)
            if text:
                ext = QLabel(f"<span style='font-family:Consolas,monospace'>{text}</span>"
                             + (f"<br><i>{hint}</i>" if hint else ""))
                ext.setWordWrap(True)
                v.addWidget(ext)
        self.sel = QgsProjectionSelectionWidget(self)
        self.sel.setOptionVisible(QgsProjectionSelectionWidget.CrsOption.ProjectCrs, True)
        if default is not None and default.isValid():
            self.sel.setCrs(default)
        v.addWidget(self.sel)
        prj = QgsProject.instance().crs()
        v.addWidget(QLabel(f"It will be shown in the project's coordinate system: {prj.authid()} {prj.description()}"))
        self.cbRemember = QCheckBox("Use this for every file in this folder without asking again")
        self.cbRemember.setVisible(allow_remember)
        v.addWidget(self.cbRemember)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self.setMinimumWidth(520)

    def crs(self):
        return self.sel.crs()


def ask_source_crs(parent, path, xyz=None, force=False, current=None):
    """CRS the file's coordinates are in, or None if cancelled.
    Asks unless the file carries its own CRS or the folder is set to 'don't ask again'."""
    from ..core.crs import file_crs
    own = file_crs(path)
    if own is not None and not force:
        return own
    rem, skip = remembered(path)
    if rem is not None and skip and not force:
        return rem
    last = QgsCoordinateReferenceSystem(QSettings().value(KEY_LAST, "") or "")
    default = current or rem or (last if last.isValid() else None) or QgsProject.instance().crs()
    name = os.path.basename(path)
    msg = (f"<b>{name}</b> doesn't record its coordinate system. Which one are its coordinates in?"
           if own is None else f"Coordinate system of <b>{name}</b>:")
    dlg = CrsDialog(parent, "Coordinate system", msg, xyz, default)
    dlg.cbRemember.setChecked(bool(skip))
    if dlg.exec() != QDialog.DialogCode.Accepted or not dlg.crs().isValid():
        return None
    remember(path, dlg.crs(), dlg.cbRemember.isChecked())
    return dlg.crs()
