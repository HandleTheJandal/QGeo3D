# -*- coding: utf-8 -*-
"""3D vertex editing: what can be edited (strings in the strings GeoPackage, wireframes in the 3D
view) and how a moved / deleted vertex is written back."""

import json

import numpy as np
from qgis.core import QgsGeometry, QgsLineString, QgsPoint, QgsProject

from .crs import transform_xyz
from .q2thook import is_protected
from .compat import gdal_soft

MAX_MESH_VERTS = 30000


def string_items(store):
    """[{id: 's:<fid>', pts, closed}] for every string (project CRS)."""
    out = []
    if store is None or store.layer is None:
        return out
    prj = QgsProject.instance().crs()
    for f in store.layer.getFeatures():
        g = f.geometry()
        if g is None or g.isNull():
            continue
        pts = np.array([(v.x(), v.y(), v.z() if v.is3D() else 0.0) for v in g.vertices()])
        if len(pts) < 2:
            continue
        pts = transform_xyz(pts, store.layer.crs(), prj)
        closed = len(pts) > 3 and np.linalg.norm(pts[0] - pts[-1]) < 1e-3
        out.append({"id": f"s:{f.id()}", "pts": pts.tolist(), "closed": bool(closed)})
    return out


def mesh_items(hook, ids=None):
    out = []
    for mid, m in hook.meshes.items():
        if ids is not None and mid not in ids:
            continue
        if len(m.vertices) > MAX_MESH_VERTS:
            continue
        out.append({"id": f"m:{mid}", "pts": np.asarray(m.vertices, float).tolist(), "closed": False})
    return out


def _write_string(store, fid, pts):
    if is_protected(store.path):
        raise PermissionError("Ringfenced location")
    pts = store.to_layer_crs(np.asarray(pts, float))
    geom = QgsGeometry(QgsLineString([QgsPoint(float(x), float(y), float(z)) for x, y, z in pts]))
    lyr = store.layer
    if lyr.isEditable():
        lyr.changeGeometry(fid, geom)
    else:
        lyr.dataProvider().changeGeometryValues({fid: geom})
        lyr.reload()
    lyr.triggerRepaint()
    store.changed.emit()


def set_string_layer(store, nos, layer, rename_from=None):
    """Put strings nos into string layer `layer`. With rename_from, strings whose name equals the old layer
    name are renamed too (renaming a layer)."""
    if is_protected(store.path):
        raise PermissionError("Ringfenced location")
    lyr = store.layer
    store.ensure_layer_field()
    il, iname = lyr.fields().indexOf("layer"), lyr.fields().indexOf("name")
    if il < 0:
        raise ValueError("The strings layer has no 'layer' field")
    nos = {int(n) for n in nos}
    changes = {}
    for f in lyr.getFeatures():
        if f["string_no"] in (None, "") or int(f["string_no"]) not in nos:
            continue
        ch = {il: layer}
        if rename_from is not None and iname >= 0 and (f["name"] or "") == rename_from:
            ch[iname] = layer
        changes[f.id()] = ch
    if not changes:
        return 0
    if lyr.isEditable():
        for fid, ch in changes.items():
            for idx, v in ch.items():
                lyr.changeAttributeValue(fid, idx, v)
    else:
        lyr.dataProvider().changeAttributeValues(changes)
        lyr.reload()
    lyr.triggerRepaint()
    store.changed.emit()
    return len(changes)


def rename_string(store, no, name):
    """Give string number no a new name."""
    if is_protected(store.path):
        raise PermissionError("Ringfenced location")
    lyr = store.layer
    idx = lyr.fields().indexOf("name")
    if idx < 0:
        raise ValueError("The strings layer has no 'name' field")
    fids = [f.id() for f in lyr.getFeatures() if f["string_no"] == no]
    if not fids:
        raise KeyError(f"No string {no}")
    if lyr.isEditable():
        for fid in fids:
            lyr.changeAttributeValue(fid, idx, name)
    else:
        lyr.dataProvider().changeAttributeValues({fid: {idx: name} for fid in fids})
        lyr.reload()
    lyr.triggerRepaint()
    store.changed.emit()


def string_points(store, fid):
    f = store.layer.getFeature(fid)
    g = f.geometry()
    pts = np.array([(v.x(), v.y(), v.z() if v.is3D() else 0.0) for v in g.vertices()])
    return transform_xyz(pts, store.layer.crs(), QgsProject.instance().crs())


def move_string_vertex(store, fid, i, p):
    pts = string_points(store, fid)
    closed = len(pts) > 3 and np.linalg.norm(pts[0] - pts[-1]) < 1e-3
    pts[i] = p
    if closed and i in (0, len(pts) - 1):           # keep a closed outline closed
        pts[0] = pts[-1] = p
    _write_string(store, fid, pts)


def delete_string_vertex(store, fid, i):
    pts = string_points(store, fid)
    closed = len(pts) > 3 and np.linalg.norm(pts[0] - pts[-1]) < 1e-3
    if len(pts) <= 2:
        return False
    pts = np.delete(pts, i, axis=0)
    if closed and i in (0, len(pts)):
        pts[-1 if i == 0 else 0] = pts[0 if i == 0 else -1]
    _write_string(store, fid, pts)
    return True


def move_mesh_vertex(hook, mid, i, p):
    m = hook.meshes[mid]
    m.vertices = np.array(m.vertices, float)
    m.vertices[i] = p
    spec = json.loads(hook.payloads[mid])
    hook.addMesh(m, color=spec.get("color"), edgeColor=spec.get("edgeColor", "#1a1a1a"),
                 opacity=spec.get("opacity", 1.0), mid=mid, zoom=False)
    return m


@gdal_soft
def update_saved_wireframe(path, wf_no, mesh, layer_crs=None):
    """Rewrite the TIN of wireframe wf_no in the strings GeoPackage (and its stats)."""
    from osgeo import ogr
    if is_protected(path):
        raise PermissionError("Ringfenced location")
    v = np.asarray(mesh.vertices, float)
    if layer_crs is not None:
        v = transform_xyz(v, QgsProject.instance().crs(), layer_crs)
    ds = ogr.Open(path, 1)
    lyr = ds.GetLayerByName("wireframes") if ds is not None else None
    if lyr is None:
        return False
    st = mesh.stats()
    lyr.SetAttributeFilter(f"wf_no = {int(wf_no)}")
    ok = False
    for feat in lyr:
        tin = ogr.Geometry(ogr.wkbTINZ)
        for a, b, c in mesh.triangles:
            ring = ogr.Geometry(ogr.wkbLinearRing)
            for k in (a, b, c, a):
                ring.AddPoint(float(v[k, 0]), float(v[k, 1]), float(v[k, 2]))
            tri = ogr.Geometry(ogr.wkbTriangleZ)
            tri.AddGeometry(ring)
            tin.AddGeometry(tri)
        feat.SetGeometry(tin)
        feat.SetField("closed", 1 if st.get("closed") else 0)
        feat.SetField("area_m2", float(st.get("area", 0.0)))
        if st.get("volume") is not None:
            feat.SetField("volume_m3", float(st["volume"]))
            i_sg = feat.GetFieldIndex("sg")
            sg = feat.GetField(i_sg) if i_sg >= 0 and feat.IsFieldSetAndNotNull(i_sg) else None
            if sg:
                feat.SetField("tonnes", float(st["volume"]) * float(sg))
        lyr.SetFeature(feat)
        ok = True
    ds = None
    return ok


# ------------------------------------------------------------------ string tools
def _is_closed(pts):
    return len(pts) > 3 and np.linalg.norm(np.asarray(pts[0]) - np.asarray(pts[-1])) < 1e-3


def insert_string_vertex(store, fid, seg, p):
    """New vertex p after vertex `seg` (on segment seg -> seg+1)."""
    pts = string_points(store, fid)
    seg = int(seg)
    if not 0 <= seg < len(pts) - 1:
        raise ValueError("no such segment")
    pts = np.insert(pts, seg + 1, np.asarray(p, float), axis=0)
    _write_string(store, fid, pts)
    return len(pts)


def _copy_feature(store, fid, pts, name_suffix="", layer=None):
    """A new string with the attributes of fid and these points (project CRS). Returns its string_no."""
    from qgis.core import QgsFeature
    import datetime
    src = store.layer.getFeature(fid)
    f = QgsFeature(store.layer.fields())
    for fld in store.layer.fields().names():
        if fld.lower() != "fid":
            f[fld] = src[fld]
    lp = store.to_layer_crs(np.asarray(pts, float))
    f.setGeometry(QgsGeometry(QgsLineString([QgsPoint(float(x), float(y), float(z)) for x, y, z in lp])))
    no = store.next_number()
    f["string_no"] = no
    if name_suffix and "name" in store.layer.fields().names():
        f["name"] = f"{src['name'] or 'string'}{name_suffix}"
    if layer and "layer" in store.layer.fields().names():
        f["layer"] = layer
    if "created" in store.layer.fields().names():
        f["created"] = datetime.datetime.now().isoformat(timespec="seconds")
    lyr = store.layer
    if lyr.isEditable():
        ok = lyr.addFeature(f)
    else:
        ok, _ = lyr.dataProvider().addFeatures([f])
        lyr.reload()
    if not ok:
        raise IOError("Failed to add string")
    lyr.triggerRepaint()
    return no


def cut_string(store, fid, at_vertex=None, seg=None, p=None):
    """Cut a string at a vertex, or at point p on segment seg. An open string becomes two strings;
    a closed outline becomes an open string without the clicked segment (or, for a vertex, the
    segment leading into it)."""
    if is_protected(store.path):
        raise PermissionError("Ringfenced location")
    pts = string_points(store, fid)
    closed = _is_closed(pts)
    if at_vertex is not None:
        i = int(at_vertex)
        if closed:                      # drop the segment leading into vertex i: open from i round to i-1
            ring = pts[:-1]
            i %= len(ring)
            opened = np.vstack([ring[i:], ring[:i]])
            _write_string(store, fid, opened)
            return "opened", None
        if i <= 0 or i >= len(pts) - 1:
            raise ValueError("cut at an end vertex does nothing - pick an inside vertex or a segment")
        a, b = pts[:i + 1], pts[i:]
    else:
        seg = int(seg)
        if not 0 <= seg < len(pts) - 1:
            raise ValueError("no such segment")
        q = (pts[seg] + pts[seg + 1]) / 2.0 if p is None else np.asarray(p, float).reshape(3)
        if closed:                      # drop the clicked segment: open from its far end round to its near end
            ring = pts[:-1]
            n = len(ring)
            k = (seg + 1) % n
            opened = np.vstack([ring[k:], ring[:k]])
            _write_string(store, fid, opened)
            return "opened", None
        a = np.vstack([pts[:seg + 1], [q]])
        b = np.vstack([[q], pts[seg + 1:]])
    _write_string(store, fid, a)
    no = _copy_feature(store, fid, b)
    store.changed.emit()
    return "split", no


def copy_string(store, fid, offset=(0.0, 0.0, 0.0), layer=None):
    """Duplicate a string, moved by offset (dx, dy, dz in project units), into string layer `layer` (else the
    source string's layer). Returns the new string_no."""
    if is_protected(store.path):
        raise PermissionError("Ringfenced location")
    pts = string_points(store, fid) + np.asarray(offset, float)
    no = _copy_feature(store, fid, pts, name_suffix="" if layer else " copy", layer=layer)
    store.changed.emit()
    return no


def triangles_mesh(tris, name="manual triangles"):
    """Mesh from a list of triangles [[p0, p1, p2], ...] (map coordinates); shared corners merged."""
    from .mesh import Mesh
    verts, index, faces = [], {}, []
    for t in tris:
        f = []
        for p in t:
            key = tuple(np.round(np.asarray(p, float), 4))
            if key not in index:
                index[key] = len(verts)
                verts.append(key)
            f.append(index[key])
        if len(set(f)) == 3:
            faces.append(f)
    return Mesh(np.array(verts, float).reshape(-1, 3), np.array(faces, np.int64).reshape(-1, 3), name=name)


def snap_corners(tris, vertices, tol=0.005):
    """Corners within tol (m) of an existing vertex take its exact position, so triangles added to a
    wireframe share its vertices (clicked positions come back through the 3D view in single precision)."""
    V = np.asarray(vertices, float)
    out = []
    for t in tris:
        tt = []
        for p in t:
            p = np.asarray(p, float)
            if len(V):
                d = np.linalg.norm(V - p, axis=1)
                k = int(np.argmin(d))
                if d[k] <= tol:
                    p = V[k]
            tt.append([float(c) for c in p])
        out.append(tt)
    return out


def close_string(store, fid):
    """Join the last point of an open string back to its first. False if already closed / too short."""
    pts = string_points(store, fid)
    if _is_closed(pts) or len(pts) < 3:
        return False
    _write_string(store, fid, np.vstack([pts, pts[:1]]))
    return True


def delete_string_vertices(store, fid, idx):
    """Delete several vertices of one string (highest first). Returns how many went; keeps >= 2 points
    (3 + closure for a closed outline)."""
    pts = string_points(store, fid)
    closed = _is_closed(pts)
    n_unique = len(pts) - (1 if closed else 0)
    idx = sorted({int(i) % n_unique if closed else int(i) for i in idx}, reverse=True)
    keep_min = 3 if closed else 2
    gone = 0
    ring = pts[:-1] if closed else pts
    for i in idx:
        if len(ring) - 1 < keep_min:
            break
        ring = np.delete(ring, i, axis=0)
        gone += 1
    out = np.vstack([ring, ring[:1]]) if closed else ring
    _write_string(store, fid, out)
    return gone


def append_triangles(vertices, triangles, tris, tol=0.005):
    """Add triangles (corner coordinates) to a mesh's arrays: a corner within tol of an existing vertex
    uses it, else it becomes a new vertex. Returns (vertices, triangles) - new arrays, old ones untouched,
    new triangles at the end (so they can be undone by truncating)."""
    V = np.asarray(vertices, float).reshape(-1, 3)
    T = np.asarray(triangles, np.int64).reshape(-1, 3)
    newv, newt = [], []
    for t in tris:
        f = []
        for p in t:
            p = np.asarray(p, float)
            k = -1
            if len(V):
                d = np.linalg.norm(V - p, axis=1)
                j = int(np.argmin(d))
                if d[j] <= tol:
                    k = j
            if k < 0:
                for j, q in enumerate(newv):
                    if np.linalg.norm(q - p) <= tol:
                        k = len(V) + j
                        break
            if k < 0:
                newv.append(p)
                k = len(V) + len(newv) - 1
            f.append(k)
        if len(set(f)) == 3:
            newt.append(f)
    V2 = np.vstack([V] + ([np.array(newv)] if newv else []))
    T2 = np.vstack([T] + ([np.array(newt, np.int64)] if newt else []))
    return V2, T2
