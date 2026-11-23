# -*- coding: utf-8 -*-
"""Wireframing: link strings on successive sections into a triangulated solid.

Pure numpy (no QGIS imports) so it can be unit-tested outside QGIS.

Method (Micromine/Datamine-style "link strings"):
  * Each string is a polyline in 3D. Closed outlines are treated as rings.
  * Successive strings are linked pairwise with the shortest-diagonal rule: walk both
    strings together and, at each step, add the triangle whose new diagonal is shorter.
    Diagonals are compared in centroid/scale-normalised coordinates, so outlines of
    different size or position still link sensibly.
  * Rings are aligned first: same winding (Newell normals agree) and B's start vertex is
    the one nearest A's start in normalised coordinates.
  * End caps: ear clipping of each end ring in its best-fit plane.
  * Finally faces are made consistently wound and outward facing (positive volume).
"""

import numpy as np

from .mesh import Mesh


# ------------------------------------------------------------------ strings
def clean_string(pts, closed=None, close_tol=1e-3):
    """Drop consecutive duplicates. Returns (pts, closed). A string is closed if its
    first and last vertices coincide (then the duplicate is dropped) or closed=True."""
    p = np.asarray(pts, dtype=np.float64).reshape(-1, 3)
    keep = np.ones(len(p), dtype=bool)
    keep[1:] = np.linalg.norm(np.diff(p, axis=0), axis=1) > close_tol
    p = p[keep]
    is_closed = len(p) > 2 and np.linalg.norm(p[0] - p[-1]) <= close_tol
    if is_closed:
        p = p[:-1]
    if closed is not None:
        is_closed = bool(closed) and len(p) >= 3
    return p, is_closed


def newell_normal(ring):
    """Area-weighted normal of a (closed) ring; length = 2 x area."""
    r = ring - ring.mean(axis=0)
    nxt = np.roll(r, -1, axis=0)
    return np.array([
        np.sum((r[:, 1] - nxt[:, 1]) * (r[:, 2] + nxt[:, 2])),
        np.sum((r[:, 2] - nxt[:, 2]) * (r[:, 0] + nxt[:, 0])),
        np.sum((r[:, 0] - nxt[:, 0]) * (r[:, 1] + nxt[:, 1])),
    ])


def _normalised(p):
    c = p.mean(axis=0)
    s = np.sqrt(((p - c) ** 2).sum(axis=1).mean()) or 1.0
    return (p - c) / s


def align_rings(a, b):
    """Return b re-ordered to match a: same winding and nearest start vertex."""
    if np.dot(newell_normal(a), newell_normal(b)) < 0:
        b = b[::-1].copy()
    na, nb = _normalised(a), _normalised(b)
    start = int(np.argmin(np.linalg.norm(nb - na[0], axis=1)))
    return np.roll(b, -start, axis=0)


def align_open(a, b):
    """Return b possibly reversed so that its ends match a's ends."""
    same = np.linalg.norm(a[0] - b[0]) + np.linalg.norm(a[-1] - b[-1])
    flip = np.linalg.norm(a[0] - b[-1]) + np.linalg.norm(a[-1] - b[0])
    return b[::-1].copy() if flip < same else b


# ------------------------------------------------------------------- linking
def link_pair(a, b, closed, ia0=0, ib0=0):
    """Shortest-diagonal triangulation between strings a (n,3) and b (m,3).
    Vertex indices are offset by ia0 / ib0. Returns list of (i, j, k) triangles."""
    n, m = len(a), len(b)
    na, nb = _normalised(a), _normalised(b)
    # walk index sequences (rings repeat their first vertex at the end)
    sa = list(range(n)) + ([0] if closed else [])
    sb = list(range(m)) + ([0] if closed else [])
    i = j = 0
    tris = []
    while i < len(sa) - 1 or j < len(sb) - 1:
        if i == len(sa) - 1:
            adv_a = False
        elif j == len(sb) - 1:
            adv_a = True
        else:
            d_a = np.linalg.norm(na[sa[i + 1]] - nb[sb[j]])     # diagonal if we advance on A
            d_b = np.linalg.norm(na[sa[i]] - nb[sb[j + 1]])     # diagonal if we advance on B
            adv_a = d_a <= d_b
        if adv_a:
            tris.append((ia0 + sa[i], ia0 + sa[i + 1], ib0 + sb[j]))
            i += 1
        else:
            tris.append((ia0 + sa[i], ib0 + sb[j + 1], ib0 + sb[j]))
            j += 1
    return tris


# ---------------------------------------------------------------------- caps
def _plane_basis(ring):
    n = newell_normal(ring)
    nn = np.linalg.norm(n)
    if nn == 0:
        raise ValueError("Degenerate (zero-area) outline - cannot cap")
    n /= nn
    u = np.cross(n, [0, 0, 1.0]) if abs(n[2]) < 0.9 else np.cross(n, [1.0, 0, 0])
    u /= np.linalg.norm(u)
    v = np.cross(n, u)
    return u, v


def _area2(p):
    x, y = p[:, 0], p[:, 1]
    return np.sum(x * np.roll(y, -1) - np.roll(x, -1) * y)


def _same(p, q, eps=1e-9):
    return abs(p[0] - q[0]) <= eps and abs(p[1] - q[1]) <= eps


def ear_clip(ring):
    """Triangulate a simple polygon (3D ring, roughly planar). Returns list of local
    index triples, wound the same way as the ring."""
    n = len(ring)
    if n < 3:
        return []
    u, v = _plane_basis(ring)
    c = ring.mean(axis=0)
    p2 = np.column_stack([(ring - c) @ u, (ring - c) @ v])
    ccw = _area2(p2) > 0
    idx = list(range(n))
    if not ccw:
        idx.reverse()

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    def inside(pt, a, b, c_):
        return cross(a, b, pt) >= 0 and cross(b, c_, pt) >= 0 and cross(c_, a, pt) >= 0

    tris = []
    guard = 0
    while len(idx) > 3 and guard < 10 * n * n:
        guard += 1
        k = len(idx)
        clipped = False
        for t in range(k):
            i0, i1, i2 = idx[(t - 1) % k], idx[t], idx[(t + 1) % k]
            a, b, c_ = p2[i0], p2[i1], p2[i2]
            if cross(a, b, c_) <= 1e-12:          # reflex or collinear
                continue
            if any(inside(p2[q], a, b, c_) for q in idx if q not in (i0, i1, i2) and
                   not (_same(p2[q], a) or _same(p2[q], b) or _same(p2[q], c_))):
                continue
            tris.append((i0, i1, i2))
            idx.pop(t)
            clipped = True
            break
        if not clipped:            # self-intersecting / numerically awkward: fan the rest
            for t in range(1, len(idx) - 1):
                tris.append((idx[0], idx[t], idx[t + 1]))
            idx = []
            break
    if len(idx) == 3:
        tris.append(tuple(idx))
    if not ccw:                    # restore the ring's own winding
        tris = [(a, c_, b) for a, b, c_ in tris]
    return tris


# --------------------------------------------------------------- orientation
def orient_consistently(tris):
    """Flip triangles so that neighbours share edges in opposite directions (BFS per
    connected component). Returns new (k,3) array."""
    f = np.asarray(tris, dtype=np.int64).copy()
    edge_faces = {}
    for fi, (a, b, c) in enumerate(f):
        for e in ((a, b), (b, c), (c, a)):
            edge_faces.setdefault(frozenset(e), []).append(fi)
    seen = np.zeros(len(f), dtype=bool)
    for start in range(len(f)):
        if seen[start]:
            continue
        seen[start] = True
        stack = [start]
        while stack:
            fi = stack.pop()
            a, b, c = f[fi]
            for e in ((a, b), (b, c), (c, a)):
                for fj in edge_faces[frozenset(e)]:
                    if fj == fi or seen[fj]:
                        continue
                    x, y, z = f[fj]
                    directed = {(x, y), (y, z), (z, x)}
                    if e in directed:            # same direction -> flip neighbour
                        f[fj] = (x, z, y)
                    seen[fj] = True
                    stack.append(fj)
    return f


def signed_volume(vertices, tris):
    v = np.asarray(vertices)
    f = np.asarray(tris)
    c = v.mean(axis=0)
    a, b, d = v[f[:, 0]] - c, v[f[:, 1]] - c, v[f[:, 2]] - c
    return float(np.einsum("ij,ij->i", a, np.cross(b, d)).sum() / 6.0)


# ---------------------------------------------------------------------- build
def order_strings(strings):
    """Sort closed outlines along their mean normal (i.e. section by section)."""
    if len(strings) < 3:
        return strings
    normals = []
    for s in strings:
        nrm = newell_normal(s)
        if normals and np.dot(nrm, normals[0]) < 0:
            nrm = -nrm
        normals.append(nrm)
    mean_n = np.sum(normals, axis=0)
    if np.linalg.norm(mean_n) == 0:
        return strings
    keys = [float(np.dot(s.mean(axis=0), mean_n)) for s in strings]
    return [s for _, s in sorted(zip(keys, strings), key=lambda t: t[0])]


def build_wireframe(strings, closed=True, cap=True, sort=True, name="wireframe"):
    """strings: list of (n,3) arrays (map coords, in linking order unless sort=True).
    Returns Mesh (closed solid when closed outlines are capped)."""
    rings = []
    for s in strings:
        p, is_closed = clean_string(s, closed=closed if closed else None)
        if closed and not is_closed:
            raise ValueError("String has fewer than 3 points - cannot be a closed outline")
        if len(p) < 2:
            raise ValueError("String has fewer than 2 points")
        rings.append(p)
    if len(rings) < 2:
        raise ValueError("Select at least two strings to link")
    if closed and sort:
        rings = order_strings(rings)

    # align each ring to its predecessor
    for k in range(1, len(rings)):
        rings[k] = align_rings(rings[k - 1], rings[k]) if closed else align_open(rings[k - 1], rings[k])

    verts, tris, base = [], [], []
    off = 0
    for r in rings:
        base.append(off)
        verts.append(r)
        off += len(r)
    verts = np.vstack(verts)

    for k in range(len(rings) - 1):
        tris += link_pair(rings[k], rings[k + 1], closed, base[k], base[k + 1])

    if closed and cap:
        for k in (0, len(rings) - 1):
            tris += [(base[k] + a, base[k] + b, base[k] + c) for a, b, c in ear_clip(rings[k])]

    f = orient_consistently(tris)
    if closed and cap and signed_volume(verts, f) < 0:
        f = f[:, [0, 2, 1]]
    mesh = Mesh(verts, f, name=name)
    return mesh


# ------------------------------------------------------------------- slicing
def slice_mesh(mesh, point, normal, tol=1e-9):
    """Intersect a mesh with a plane. Returns list of polylines (k,3); closed loops
    are returned with first point repeated at the end."""
    v, f = mesh.vertices, mesh.triangles
    n = np.asarray(normal, float)
    n = n / np.linalg.norm(n)
    d = (v - np.asarray(point, float)) @ n
    d[np.abs(d) < tol] = tol            # nudge vertices exactly on the plane
    segs = []
    for tri in f:
        dd = d[tri]
        pos = dd > 0
        if pos.all() or (~pos).all():
            continue
        pts = []
        for a, b in ((0, 1), (1, 2), (2, 0)):
            if pos[a] != pos[b]:
                t = dd[a] / (dd[a] - dd[b])
                ia, ib = tri[a], tri[b]
                pts.append(((min(ia, ib), max(ia, ib)), v[ia] + t * (v[ib] - v[ia])))
        if len(pts) == 2:
            segs.append(pts)
    # chain segments by shared mesh edges
    adj = {}
    for s in segs:
        (ka, pa), (kb, pb) = s
        adj.setdefault(ka, []).append((kb, pa, pb))
        adj.setdefault(kb, []).append((ka, pb, pa))
    point_of = {}
    for s in segs:
        for k, p in s:
            point_of[k] = p
    used = set()
    loops = []
    for start in list(adj.keys()):
        if start in used:
            continue
        chain = [start]
        used.add(start)
        cur = start
        while True:
            nxt = [k for k, _, _ in adj[cur] if k not in used]
            if not nxt:
                break
            cur = nxt[0]
            used.add(cur)
            chain.append(cur)
        pts = np.array([point_of[k] for k in chain])
        if start in [k for k, _, _ in adj[chain[-1]]] and len(chain) > 2:
            pts = np.vstack([pts, pts[:1]])
        loops.append(pts)
    return loops


# ====================================================================== general solids
# Several outlines per section (branching), outlines inside outlines (holes) and tie lines.

def _plane2d(rings):
    """Common 2D basis for rings of one section: (origin, u, v)."""
    n = np.sum([newell_normal(r) for r in rings], axis=0)
    if np.linalg.norm(n) == 0:
        n = np.array([0, 0, 1.0])
    n = n / np.linalg.norm(n)
    u = np.cross(n, [0, 0, 1.0]) if abs(n[2]) < 0.9 else np.cross(n, [1.0, 0, 0])
    u /= np.linalg.norm(u)
    v = np.cross(n, u)
    return np.vstack(rings).mean(axis=0), u, v


def _in_poly(pt, poly):
    x, y = pt
    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / ((y2 - y1) or 1e-300) + x1:
            inside = not inside
    return inside


def group_sections(rings, tol=None):
    """Closed rings -> [[ring, ...] per section], sections in order along the mean normal."""
    normals = []
    for r in rings:
        nrm = newell_normal(r)
        if normals and np.dot(nrm, normals[0]) < 0:
            nrm = -nrm
        normals.append(nrm)
    mean_n = np.sum(normals, axis=0)
    mean_n = mean_n / (np.linalg.norm(mean_n) or 1.0)
    keys = np.array([float(np.dot(r.mean(axis=0), mean_n)) for r in rings])
    span = keys.max() - keys.min() if len(keys) else 0
    tol = tol if tol is not None else max(0.05, 0.002 * span)
    order = np.argsort(keys)
    groups, last = [], None
    for i in order:
        if last is None or keys[i] - last > tol:
            groups.append([])
        groups[-1].append(rings[i])
        last = keys[i]
    return groups


def nest(section_rings):
    """[(outer ring, [hole rings])] for one section: a ring inside another ring is its hole."""
    o, u, v = _plane2d(section_rings)
    p2 = [np.column_stack([(r - o) @ u, (r - o) @ v]) for r in section_rings]
    parent = [None] * len(section_rings)
    area = [abs(_area2(p)) for p in p2]
    for i in range(len(section_rings)):
        best = None
        for j in range(len(section_rings)):
            if i != j and area[j] > area[i] and _in_poly(p2[i].mean(axis=0), p2[j]):
                if best is None or area[j] < area[best]:
                    best = j
        parent[i] = best
    out = []
    for i, r in enumerate(section_rings):
        if parent[i] is None:
            out.append((r, [section_rings[k] for k in range(len(section_rings)) if parent[k] == i]))
        elif parent[parent[i]] is not None:
            raise ValueError("Islands inside holes are not supported - link those outlines separately")
    return out


def _insert_at(ring, p, tol):
    """Index of ring vertex at p, inserting p on the nearest edge when no vertex is within tol."""
    d = np.linalg.norm(ring - p, axis=1)
    k = int(np.argmin(d))
    if d[k] <= tol:
        return ring, k
    n = len(ring)
    best, bt, bk = None, 0.0, 0
    for i in range(n):
        a, b = ring[i], ring[(i + 1) % n]
        ab = b - a
        t = float(np.clip(np.dot(p - a, ab) / (np.dot(ab, ab) or 1e-300), 0, 1))
        dd = np.linalg.norm(a + t * ab - p)
        if best is None or dd < best:
            best, bt, bk = dd, t, i
    q = ring[bk] + bt * (ring[(bk + 1) % n] - ring[bk])
    return np.insert(ring, bk + 1, q, axis=0), bk + 1


def link_with_ties(a, b, ties, ia0, ib0):
    """Link rings a, b (aligned) through tie point pairs [(ia, ib)]: each stretch between consecutive
    ties is linked on its own, so every tie becomes an edge of the wireframe."""
    if not ties:
        return link_pair(a, b, True, ia0, ib0)
    n, m = len(a), len(b)
    ties = sorted(set(ties))
    tris = []
    for k in range(len(ties)):
        i0, j0 = ties[k]
        i1, j1 = ties[(k + 1) % len(ties)]
        la = (i1 - i0) % n or n
        lb = (j1 - j0) % m or m
        sa = [(i0 + t) % n for t in range(la + 1)]
        sb = [(j0 + t) % m for t in range(lb + 1)]
        tris += _link_index_paths(a, b, sa, sb, ia0, ib0)
    return tris


def _link_index_paths(a, b, sa, sb, ia0, ib0):
    """Shortest-diagonal walk along index paths sa (into a) and sb (into b)."""
    na, nb = _normalised(a), _normalised(b)
    i = j = 0
    tris = []
    while i < len(sa) - 1 or j < len(sb) - 1:
        if i == len(sa) - 1:
            adv_a = False
        elif j == len(sb) - 1:
            adv_a = True
        else:
            adv_a = np.linalg.norm(na[sa[i + 1]] - nb[sb[j]]) <= np.linalg.norm(na[sa[i]] - nb[sb[j + 1]])
        if adv_a:
            tris.append((ia0 + sa[i], ia0 + sa[i + 1], ib0 + sb[j]))
            i += 1
        else:
            tris.append((ia0 + sa[i], ib0 + sb[j + 1], ib0 + sb[j]))
            j += 1
    return [t for t in tris if len(set(t)) == 3]


def cap_with_holes(outer, holes, base_outer, base_holes):
    """Triangles of a planar cap with holes: each hole is bridged into the outer ring (nearest vertex
    pair) and the merged ring ear-clipped. Returns triangles in global indices."""
    ring = [(base_outer + i, outer[i]) for i in range(len(outer))]
    for h, bh in sorted(zip(holes, base_holes), key=lambda t: -float(np.max(t[0][:, 0]))):
        if np.dot(newell_normal(h), newell_normal(outer)) > 0:      # holes run the other way round
            h = h[::-1]
            idx_h = [bh + (len(h) - 1 - i) for i in range(len(h))]
        else:
            idx_h = [bh + i for i in range(len(h))]
        pts = np.array([p for _i, p in ring])
        best = None
        for hi, hp in enumerate(h):
            d = np.linalg.norm(pts - hp, axis=1)
            k = int(np.argmin(d))
            if best is None or d[k] < best[0]:
                best = (d[k], k, hi)
        _d, k, hi = best
        hole_seq = [(idx_h[(hi + t) % len(h)], h[(hi + t) % len(h)]) for t in range(len(h) + 1)]
        ring = ring[:k + 1] + hole_seq + ring[k:]
    pts = np.array([p for _i, p in ring])
    ids = [i for i, _p in ring]
    # tiny inward nudge of duplicated bridge vertices keeps the ear clipper's inside test honest
    local = ear_clip(pts)
    return [(ids[a], ids[b], ids[c]) for a, b, c in local if len({ids[a], ids[b], ids[c]}) == 3]


def branch_cut(parent, kids):
    """Where the parent outline is cut for a 1 -> 2 branch: the two points where the line half way
    between the children (perpendicular to the line joining their centres, in the section plane)
    crosses the parent. Returns (parent with those points inserted, index a, index b)."""
    if len(kids) != 2:
        raise ValueError("Branching into more than two outlines: link it in steps (one split at a time)")
    o, u, v = _plane2d([parent] + list(kids))
    to2 = lambda P: np.column_stack([(P - o) @ u, (P - o) @ v])
    c1, c2 = to2(kids[0]).mean(axis=0), to2(kids[1]).mean(axis=0)
    d = c2 - c1
    if np.linalg.norm(d) == 0:
        raise ValueError("Branch: the two outlines have the same centre")
    mid = (c1 + c2) / 2
    p2 = to2(parent)
    f = (p2 - mid) @ d                      # signed side of the dividing line
    n = len(parent)
    cross = []
    for k in range(n):
        a, b = f[k], f[(k + 1) % n]
        if a == 0:
            cross.append((k, 0.0))
        elif a * b < 0:
            cross.append((k, a / (a - b)))
    if len(cross) != 2:
        raise ValueError(f"Branch: the dividing line crosses the parent outline {len(cross)} times - "
                         "add tie lines or link in steps")
    pts = []
    for k, t in cross:
        pts.append((k, parent[k] + t * (parent[(k + 1) % n] - parent[k]), t))
    out = parent.copy()
    idx = []
    for k, q, t in sorted(pts, key=lambda x: -x[0]):    # insert from the back so indices stay valid
        if t == 0.0:
            idx.append(k)
            continue
        out = np.insert(out, k + 1, q, axis=0)
        idx = [x + 1 if x > k else x for x in idx]
        idx.append(k + 1)
    ia, ib = sorted(idx)
    return out, ia, ib


def branch(parent, kids, pbase, kbases, verts, ia, ib, n_chord=3):
    """Link a parent outline (already cut at ia, ib by branch_cut) to two child outlines. Each half of the
    parent is closed by a chord (its middle points raised half way to the children) and linked to the
    child on its side; the chord is shared by both halves, so the crotch is watertight."""
    n = len(parent)
    kc = np.vstack(kids).mean(axis=0)
    pc = parent.mean(axis=0)
    nrm = newell_normal(parent)
    nrm = nrm / (np.linalg.norm(nrm) or 1.0)
    lift = float(np.dot(kc - pc, nrm)) / 2.0
    qa, qb = parent[ia], parent[ib]
    chord = []
    for t in np.linspace(0, 1, n_chord + 2)[1:-1]:
        q = qa + t * (qb - qa) + lift * nrm
        chord.append(len(verts))
        verts.append(q)
    path1 = [(ia + t) % n for t in range(((ib - ia) % n) + 1)]       # ia -> ib
    path2 = [(ib + t) % n for t in range(((ia - ib) % n) + 1)]       # ib -> ia
    V = lambda gi: np.asarray(verts[gi], float)
    pieces = [([pbase + i for i in path1] + chord[::-1]),             # closes ib -> ... -> ia
              ([pbase + i for i in path2] + chord)]                   # closes ia -> ... -> ib
    cents = [np.mean([V(g) for g in pc_], axis=0) for pc_ in pieces]
    k0 = int(np.argmin([np.linalg.norm(cents[0] - k.mean(axis=0)) for k in kids]))
    order = [k0, 1 - k0]
    tris = []
    for piece, ki in zip(pieces, order):
        P = np.array([V(g) for g in piece])
        child = kids[ki]
        c = align_rings(P, child)
        cidx = [kbases[ki] + int(np.argmin(np.linalg.norm(child - q, axis=1))) for q in c]
        sa = list(range(len(P))) + [0]
        sb = list(range(len(c))) + [0]
        for t in _link_index_paths(P, c, sa, sb, 0, 10 ** 6):
            tris.append(tuple(piece[x] if x < 10 ** 6 else cidx[x - 10 ** 6] for x in t))
    return tris


def build_solid(strings, ties=(), name="wireframe", cap=True, tie_tol=None):
    """General solid from closed outlines: several per section (branching 1 -> n), holes (outlines
    inside outlines) and tie lines (2-point strings joining outlines on neighbouring sections)."""
    rings = []
    for st in strings:
        p, ok = clean_string(st, closed=True)
        if not ok:
            raise ValueError("Outlines must be closed (3 or more points)")
        rings.append(p)
    groups = group_sections(rings)
    if len(groups) < 2:
        raise ValueError("The outlines all lie on one section - nothing to link")
    secs = [[[o, list(hs)] for o, hs in nest(g)] for g in groups]     # mutable: [outer, holes]
    span = np.ptp(np.vstack(rings), axis=0).max() or 1.0
    tol = tie_tol or 0.02 * span

    # 1. tie lines: put their end points into the outlines (as vertices) before anything is linked
    tie_pts = []                               # [(sec, outer idx, point), (sec, outer idx, point)]
    for t in ties:
        t = np.asarray(t, float).reshape(-1, 3)
        ends = []
        for p in (t[0], t[-1]):
            best = None
            for si, sec in enumerate(secs):
                for oi, (o, _h) in enumerate(sec):
                    d = float(np.min(np.linalg.norm(o - p, axis=1)))
                    e = float(min(np.linalg.norm(np.cross(o[(k + 1) % len(o)] - o[k], o[k] - p)) /
                                  (np.linalg.norm(o[(k + 1) % len(o)] - o[k]) or 1e-300) for k in range(len(o))))
                    d = min(d, e)
                    if best is None or d < best[0]:
                        best = (d, si, oi)
            if best is not None and best[0] <= tol:
                ends.append((best[1], best[2], p))
        if len(ends) == 2 and abs(ends[0][0] - ends[1][0]) == 1:
            for si, oi, p in ends:
                o, h = secs[si][oi]
                o, _k = _insert_at(o, p, tol * 0.02)
                secs[si][oi] = [o, h]
            tie_pts.append(tuple(sorted(ends, key=lambda e: e[0])))

    # 2. branches: cut each parent outline where its two children divide (before anything is linked)
    cuts = {}
    for s_ in range(len(secs) - 1):
        A, B = secs[s_], secs[s_ + 1]
        if (len(A) == 1) != (len(B) == 1):
            if any(h for _o, h in A) or any(h for _o, h in B):
                raise ValueError("Branching outlines with holes are not supported")
            ps, kids = (s_, [o for o, _h in B]) if len(A) == 1 else (s_ + 1, [o for o, _h in A])
            if ps in cuts.values() or any(c[0] == ps for c in cuts.values()):
                raise ValueError("An outline branches on both sides - link it in steps")
            out, ia, ib = branch_cut(secs[ps][0][0], kids)
            secs[ps][0][0] = out
            cuts[s_] = (ps, ia, ib)

    verts, base = [], {}

    def add(r):
        k = id(r)
        if k not in base:
            base[k] = len(verts)
            verts.extend(list(r))
        return base[k]

    tris, linked = [], set()
    for s in range(len(secs) - 1):
        A, B = secs[s], secs[s + 1]
        if s in cuts:                                           # 1 -> 2 or 2 -> 1: branch
            ps, ia, ib = cuts[s]
            parent = secs[ps][0][0]
            kids = [o for o, _h in (B if ps == s else A)]
            pb = add(parent)
            kb = [add(k) for k in kids]
            tris += branch(parent, kids, pb, kb, verts, ia, ib)
            linked |= {(s, i, +1) for i in range(len(A))} | {(s + 1, j, -1) for j in range(len(B))}
            continue
        ca = [o.mean(axis=0) for o, _h in A]
        cb = [o.mean(axis=0) for o, _h in B]
        pairs, used = [], set()
        for i in np.argsort([min(np.linalg.norm(c - d) for d in cb) for c in ca]):
            j = min((j for j in range(len(B)) if j not in used), key=lambda j: np.linalg.norm(ca[i] - cb[j]),
                    default=None)
            if j is not None:
                used.add(j)
                pairs.append((int(i), j))
        for i, j in pairs:
            oa, ha = A[i]
            ob, hb = B[j]
            if id(ob) not in base:                    # not linked yet: free to re-order
                ob = align_rings(oa, ob)
                B[j] = [ob, hb]
            a0, b0 = add(oa), add(ob)
            tie_idx = []
            for (s1, o1, p1), (s2, o2, p2) in tie_pts:
                if s1 == s and o1 == i and s2 == s + 1 and o2 == j:
                    tie_idx.append((int(np.argmin(np.linalg.norm(oa - p1, axis=1))),
                                    int(np.argmin(np.linalg.norm(ob - p2, axis=1)))))
            tris += link_with_ties(oa, ob, tie_idx, a0, b0)
            used_h = set()
            for h in ha:
                k = min((k for k in range(len(hb)) if k not in used_h),
                        key=lambda k: np.linalg.norm(h.mean(axis=0) - hb[k].mean(axis=0)), default=None)
                if k is None:
                    continue
                used_h.add(k)
                if id(hb[k]) not in base:
                    hb[k] = align_rings(h, hb[k])
                tris += link_pair(h, hb[k], True, add(h), add(hb[k]))
                linked |= {("h", id(h), +1), ("h", id(hb[k]), -1)}
            linked |= {(s, i, +1), (s + 1, j, -1)}
    if cap:
        for s, sec in enumerate(secs):
            for i, (o, hs) in enumerate(sec):
                if (s, i, -1) not in linked and (s, i, +1) not in linked:
                    continue                           # an outline linked to nothing: leave it out
                for side in (-1, +1):
                    if (s, i, side) not in linked:
                        tris += cap_with_holes(o, hs, add(o), [add(h) for h in hs])
                    else:                              # a hole that stops inside the solid is closed off
                        for h in hs:
                            if ("h", id(h), side) not in linked:
                                hb_ = add(h)
                                tris += [(hb_ + a, hb_ + b, hb_ + c) for a, b, c in ear_clip(h)]
    V = np.array(verts, float)
    f = orient_consistently(tris)
    if signed_volume(V, f) < 0:
        f = f[:, [0, 2, 1]]
    return Mesh(V, f, name=name)


# ================================================================ link strings one by one
# Strings picked in order, each linked to the one before. Triangles follow both strings at the same
# proportion of their length ("equal proportion" linking), so they fan along each string instead of
# jumping to whichever vertex happens to be nearest.

def arc_fractions(p, closed):
    """Cumulative length along a string as a fraction of its total (len(p)+1 values for a ring)."""
    q = np.vstack([p, p[:1]]) if closed else p
    d = np.concatenate([[0.0], np.cumsum(np.linalg.norm(np.diff(q, axis=0), axis=1))])
    return d / (d[-1] or 1.0)


def points_at(p, t, closed):
    """Points at length fractions t along string p."""
    q = np.vstack([p, p[:1]]) if closed else p
    f = arc_fractions(p, closed)
    return np.column_stack([np.interp(t, f, q[:, k]) for k in range(3)])


def align_proportional(a, b, closed):
    """b re-started / reversed so that points at the same fraction of length on a and b correspond best
    (compared about each string's own centre, so an offset between them does not matter)."""
    A = a - a.mean(axis=0)
    ta = arc_fractions(a, closed)[:len(a)]
    if closed:
        cands = [np.roll(bb, -s, axis=0) for bb in (b, b[::-1]) for s in range(len(b))]
    else:
        cands = [b, b[::-1]]
    best, bc = None, None
    for c in cands:
        cost = float(np.sum((points_at(c - c.mean(axis=0), ta, closed) - A) ** 2))
        if best is None or cost < best - 1e-12:
            best, bc = cost, c
    return np.ascontiguousarray(bc)


def link_proportional(a, b, closed, ia0=0, ib0=0):
    """Triangles between aligned strings a and b advancing along each by proportion of length."""
    n, m = len(a), len(b)
    fa, fb = arc_fractions(a, closed), arc_fractions(b, closed)
    sa = list(range(n)) + ([0] if closed else [])
    sb = list(range(m)) + ([0] if closed else [])
    i = j = 0
    tris = []
    while i < len(sa) - 1 or j < len(sb) - 1:
        if i == len(sa) - 1:
            adv_a = False
        elif j == len(sb) - 1:
            adv_a = True
        elif abs(fa[i + 1] - fb[j + 1]) < 1e-9:               # level: take the shorter diagonal
            adv_a = np.linalg.norm(a[sa[i + 1]] - b[sb[j]]) <= np.linalg.norm(a[sa[i]] - b[sb[j + 1]])
        else:
            adv_a = fa[i + 1] < fb[j + 1]
        if adv_a:
            tris.append((ia0 + sa[i], ia0 + sa[i + 1], ib0 + sb[j]))
            i += 1
        else:
            tris.append((ia0 + sa[i], ib0 + sb[j + 1], ib0 + sb[j]))
            j += 1
    return tris


def link_chain(strings, closed_flags, cap=True, name="wireframe"):
    """Link strings in the given order (each to the one before) by equal proportion.
    strings: list of (n,3) arrays; closed_flags: bool per string (all must agree)."""
    if len(strings) < 2:
        raise ValueError("Pick at least two strings")
    if len(set(bool(c) for c in closed_flags)) > 1:
        raise ValueError("Closed outlines and open strings can't be linked to each other")
    closed = bool(closed_flags[0])
    ss = []
    for st in strings:
        p, ok = clean_string(st, closed=closed)
        if closed and not ok:
            raise ValueError("Outlines need 3 or more points")
        if len(p) < 2:
            raise ValueError("A string has fewer than 2 points")
        ss.append(p)
    for k in range(1, len(ss)):
        ss[k] = align_proportional(ss[k - 1], ss[k], closed)
    verts, base, off = [], [], 0
    for p in ss:
        base.append(off)
        verts.append(p)
        off += len(p)
    V = np.vstack(verts)
    tris = []
    for k in range(len(ss) - 1):
        tris += link_proportional(ss[k], ss[k + 1], closed, base[k], base[k + 1])
    if closed and cap:
        for k in (0, len(ss) - 1):
            tris += [(base[k] + a, base[k] + b, base[k] + c) for a, b, c in ear_clip(ss[k])]
    # merge coincident corners (a string picked twice, strings sharing points)
    key, idx, newV = {}, [], []
    for p in V:
        t = tuple(np.round(p, 4))
        if t not in key:
            key[t] = len(newV)
            newV.append(p)
        idx.append(key[t])
    idx = np.array(idx)
    T = [tuple(idx[list(t)]) for t in tris]
    T = [t for t in T if len(set(t)) == 3]
    f = orient_consistently(T)
    V2 = np.array(newV, float)
    if closed and cap and signed_volume(V2, f) < 0:
        f = f[:, [0, 2, 1]]
    return Mesh(V2, f, name=name)


def cap_with_chords(ring, chords):
    """Triangulate a closed outline so that each chord (i, j) - a tie line across the outline between two
    of its vertices - is an edge: the outline is split along the chords and each piece ear-clipped.
    Returns (triangles as local index triples, chords used, chords skipped)."""
    pieces = [list(range(len(ring)))]
    used, skipped = [], []
    for i, j in chords:
        for k, pc in enumerate(pieces):
            if i in pc and j in pc:
                a, b = pc.index(i), pc.index(j)
                if a > b:
                    a, b = b, a
                if b - a < 2 or (a == 0 and b == len(pc) - 1):        # an edge of this piece already
                    skipped.append((i, j))
                    break
                pieces[k:k + 1] = [pc[a:b + 1], pc[b:] + pc[:a + 1]]
                used.append((i, j))
                break
        else:
            skipped.append((i, j))                                    # crosses an earlier chord
    tris = []
    for pc in pieces:
        if len(pc) == 3:
            loc = [(0, 1, 2)]
        else:
            loc = ear_clip(ring[pc])
        tris += [(pc[a], pc[b], pc[c]) for a, b, c in loc]
    return tris, used, skipped
