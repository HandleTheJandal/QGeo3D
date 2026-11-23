# -*- coding: utf-8 -*-
"""Wireframe validation and safe repairs.

validate(mesh) reports what would stop a wireframe being a clean solid (or a clean surface):
open edges, edges shared by more than two triangles, inconsistent winding, an inside-out solid,
zero-area and duplicate triangles, corners that sit on the same point but aren't joined, and
unused vertices. repair(mesh) fixes the safe ones (it never invents triangles, so holes stay)."""

import numpy as np

from .mesh import Mesh


def _coincident_groups(v, tol):
    """Map each vertex to the first vertex within tol of it (grid hashing on tol cells)."""
    n = len(v)
    rep = np.arange(n)
    if n == 0 or tol <= 0:
        return rep
    cells = {}
    keys = np.floor(v / tol).astype(np.int64)
    for i in range(n):
        k = tuple(keys[i])
        hit = None
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    for j in cells.get((k[0] + dx, k[1] + dy, k[2] + dz), ()):
                        if np.linalg.norm(v[i] - v[j]) <= tol:
                            hit = j
                            break
                    if hit is not None:
                        break
                if hit is not None:
                    break
            if hit is not None:
                break
        if hit is None:
            cells.setdefault(k, []).append(i)
        else:
            rep[i] = rep[hit]
    return rep


def validate(mesh, tol=0.001):
    """Problems of a mesh: dict with counts, the problem edges / triangles (for display) and
    'issues' - a list of plain-language lines (empty when the wireframe is clean)."""
    v = np.asarray(mesh.vertices, float).reshape(-1, 3)
    f = np.asarray(mesh.triangles, np.int64).reshape(-1, 3)
    out = {"triangles": len(f), "vertices": len(v), "issues": []}
    if not len(f):
        out["issues"].append("no triangles")
        return out
    rep_idx = (f[:, 0] == f[:, 1]) | (f[:, 1] == f[:, 2]) | (f[:, 0] == f[:, 2])
    area = 0.5 * np.linalg.norm(np.cross(v[f[:, 1]] - v[f[:, 0]], v[f[:, 2]] - v[f[:, 0]]), axis=1)
    degenerate = rep_idx | (area < 1e-9)
    srt = np.sort(f, axis=1)
    _, first, counts = np.unique(srt, axis=0, return_index=True, return_counts=True)
    dup_mask = np.ones(len(f), bool)
    dup_mask[first] = False
    rep = _coincident_groups(v, tol)
    used = np.unique(f)
    coincident = int(sum(1 for i in used if rep[i] != i and rep[i] in set(used.tolist())))
    good = f[~degenerate & ~dup_mask]
    e = np.vstack([good[:, [0, 1]], good[:, [1, 2]], good[:, [2, 0]]])
    es = np.sort(e, axis=1)
    uniq, ecount = np.unique(es, axis=0, return_counts=True)
    d_uniq, dcount = np.unique(e, axis=0, return_counts=True)
    open_e = uniq[ecount == 1]
    nonman = uniq[ecount > 2]
    inconsistent = int((dcount > 1).sum())
    closed = len(open_e) == 0 and len(nonman) == 0
    vol = None
    inside_out = None
    if closed and len(good):
        c = v[used].mean(axis=0)
        a, b, cc = v[good[:, 0]] - c, v[good[:, 1]] - c, v[good[:, 2]] - c
        sv = float(np.einsum("ij,ij->i", a, np.cross(b, cc)).sum() / 6.0)
        vol, inside_out = abs(sv), sv < 0
    out.update({
        "open_edges": len(open_e), "nonmanifold_edges": len(nonman), "inconsistent_edges": inconsistent,
        "degenerate": int(degenerate.sum()), "duplicates": int(dup_mask.sum()), "coincident": coincident,
        "unused_vertices": int(len(v) - len(used)), "closed": closed, "volume": vol, "inside_out": inside_out,
        "open_edge_xyz": v[open_e].tolist(), "nonmanifold_edge_xyz": v[nonman].tolist(),
        "bad_triangle_xyz": v[f[degenerate | dup_mask]].tolist(),
    })
    I = out["issues"]
    if out["coincident"]:
        I.append(f"{coincident} corner(s) sit on another vertex but aren't joined to it (cracks) - Repair joins them")
    if out["degenerate"]:
        I.append(f"{out['degenerate']} zero-area triangle(s) - Repair removes them")
    if out["duplicates"]:
        I.append(f"{out['duplicates']} duplicate triangle(s) - Repair removes them")
    if len(open_e):
        I.append(f"{len(open_e)} open edge(s): holes or a surface, so no volume (shown red) - fill them with Add triangles")
    if len(nonman):
        I.append(f"{len(nonman)} edge(s) shared by more than two triangles (shown magenta) - delete the extra triangles")
    if inconsistent:
        I.append(f"{inconsistent} edge(s) where neighbouring triangles face opposite ways - Repair re-orients them")
    if inside_out:
        I.append("the solid is inside out (normals point in) - Repair turns it the right way")
    return out


def repair(mesh, tol=0.001):
    """Safe fixes: join coincident corners, drop zero-area and duplicate triangles, wind every
    triangle consistently, turn an inside-out solid the right way, drop unused vertices.
    Returns (new Mesh, list of what was done)."""
    from .wireframing import orient_consistently
    v = np.asarray(mesh.vertices, float).reshape(-1, 3)
    f = np.asarray(mesh.triangles, np.int64).reshape(-1, 3)
    done = []
    rep = _coincident_groups(v, tol)
    f2 = rep[f]
    n_join = int((f2 != f).any(axis=1).sum())
    if n_join:
        done.append(f"joined corners on {n_join} triangle(s)")
    bad = (f2[:, 0] == f2[:, 1]) | (f2[:, 1] == f2[:, 2]) | (f2[:, 0] == f2[:, 2])
    area = 0.5 * np.linalg.norm(np.cross(v[f2[:, 1]] - v[f2[:, 0]], v[f2[:, 2]] - v[f2[:, 0]]), axis=1)
    bad |= area < 1e-9
    if bad.any():
        done.append(f"removed {int(bad.sum())} zero-area triangle(s)")
    f2 = f2[~bad]
    _, first = np.unique(np.sort(f2, axis=1), axis=0, return_index=True)
    if len(first) < len(f2):
        done.append(f"removed {len(f2) - len(first)} duplicate triangle(s)")
    f2 = f2[np.sort(first)]
    before = f2.copy()
    f2 = orient_consistently(f2)
    if (f2 != before).any():
        done.append(f"re-oriented {int((f2 != before).any(axis=1).sum())} triangle(s)")
    used = np.unique(f2)
    remap = -np.ones(len(v), np.int64)
    remap[used] = np.arange(len(used))
    v2, f2 = v[used], remap[f2]
    if len(used) < len(v):
        done.append(f"dropped {len(v) - len(used)} unused vertex(es)")
    new = Mesh(v2, f2, name=mesh.name, source=getattr(mesh, "source", ""))
    st = new.stats()
    if st.get("closed") and st.get("inside_out"):
        new = Mesh(v2, f2[:, ::-1].copy(), name=mesh.name, source=getattr(mesh, "source", ""))
        done.append("turned the solid the right way out")
    return new, done
