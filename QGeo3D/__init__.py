# -*- coding: utf-8 -*-
import contextlib


def classFactory(iface):
    from .core.q2thook import migrate_settings
    with contextlib.suppress(Exception):
        migrate_settings()          # settings saved under the old name, QGIS2Three4
    from .plugin import QGeo3DPlugin
    return QGeo3DPlugin(iface)
