# -*- coding: utf-8 -*-


def classFactory(iface):
    from .core.q2thook import migrate_settings
    try:
        migrate_settings()          # settings saved under the old name, QGIS2Three4
    except Exception:
        pass
    from .plugin import QGeo3DPlugin
    return QGeo3DPlugin(iface)
