# QGeo 3D

Mining 3D for QGIS: wireframes, block models, drillholes, sections, strings and the file formats of
Micromine, Datamine, Surpac, Vulcan and Leapfrog - inside the 3D window of the
[Qgis2threejs](https://plugins.qgis.org/plugins/Qgis2threejs/) plugin.

QGeo 3D is a **companion plugin**: Qgis2threejs supplies the 3D window and three.js, QGeo 3D
adds the mining tools to it. Qgis2threejs itself is not modified.

## Install

1. In QGIS: **Plugins ▸ Manage and Install Plugins ▸ All** - install **Qgis2threejs**.
2. Install QGeo 3D from the same list, or download a release zip and use **Install from ZIP**.
3. Its **3D viewer** button opens the Qgis2threejs window with the QGeo 3D toolbar and panel.

Tested on QGIS 3.44 LTR with Qgis2threejs 2.10, and on QGIS 4.2 with Qgis2threejs 3.3 (see *QGIS 4* below).

## What it does

**3D view**
- Wireframes with triangle edges; plan / N / E / S / W views, recentre, orbit point, saved views
- Coordinate grid (mE / mN / mRL), scale bar with the CRS name, view coordinate
- Vertical sections with stepping and clipping, a section label and a plan window of the section
- Query any object; measure length, bearing, plunge and the dip of a plane through three points
- Section plot to PDF (title block, legend) through a QGIS print layout

**Block models** - Micromine .DAT, Datamine .dm, Vulcan .bmf, Surpac .mdl, Leapfrog / CSV / Excel, OMF
- Sub-blocked and rotated models (Datamine angles about any axes, Vulcan / Surpac bearing, dip, plunge)
- Colour by grade (Au classes, quantiles) or resource category; hide air / mined-out blocks by rule
- Grade-tonnage report by cut-off and category - for the blocks shown, all blocks, blocks inside
  wireframes or in the section slab - with a curve and CSV / Excel export

**Drillholes** - Micromine .DHDB, Datamine, CSV / Excel collars, surveys and interval tables
- Minimum-curvature desurvey; either dip sign convention
- Intervals as tubes coloured by grade, hole ID and value labels

**Strings and wireframes**
- Digitise 3D strings with snapping (drillhole interval ends, strings, surfaces, section plane)
- Link outlines into solids: branching (one outline into two), holes, tie lines; volume and tonnes
- Drag vertices of strings and wireframes in 3D

**Files** (read and write unless noted)

| Data | Formats |
|---|---|
| Wireframes | Micromine .tridb, Datamine pt/tr .dm, Surpac .dtm, Leapfrog .msh, DXF, OBJ, OMF, GeoPackage |
| Strings / points | Micromine .STR / .DAT, Datamine .dm, Surpac .str, DXF, CSV, OMF |
| Block models | Micromine .DAT, Datamine .dm, Leapfrog CSV (with header), CSV, OMF; Vulcan .bmf and Surpac .mdl read only |
| Drillholes | Micromine .DHDB, Datamine, CSV / Excel (read) |

Files that don't record a coordinate system ask for one on import (changeable afterwards). Drag and
drop works on the 3D window, the panel and the QGIS window.

**Column names in any convention** - centres and sizes (EAST + _EAST, X + XINC, X_Centre + X_Size …),
grades for any element or oxide with units (Au_OK, AUppm, Cu_pct, CuEq, Li2O_%, U3O8_ppm …), density /
SG / BD, tonnes, in-situ %, resource category (RESCAT, CLASS, JORC_Class …), and coordinate names with
grid or datum words (X_MGA94, MGA_E, RL_AHD, Depth (m)).

## Protected folders

**QGeo 3D ▸ Protected folders…** lists folders the plugin must never write to (imports,
exports, saved wireframes and Qgis2threejs settings go elsewhere). The list is kept in your QGIS
settings on your computer and is empty in a new install.

## Graphics

**QGeo 3D ▸ Automatic GPU settings** (on by default): on Windows laptops with two graphics chips,
QGIS and the 3D view are set to the high-performance one (after a QGIS restart); the 3D view's drawing
resolution is set for the graphics chip in use. Turning it off undoes what the plugin set.

## QGIS 4

The code runs under Qt5 and Qt6: QAction and field types come through a small compatibility module,
QGIS enums use their scoped names, and the three.js calls work with three.js r110 and later. The hook
finds the Qgis2threejs web page through its `webPage` attribute or the window's web view; the 3D view
must be shown in the QGIS window (Qgis2threejs 3.x's embedded view, not its external browser mode).
Qgis2threejs 3.x draws the view in a separate process: QGeo 3D's calls to it are short and status
polling is asynchronous, so a view busy building layers can't freeze QGIS.

Some OSGeo4W installs have a `share/proj/proj.db` older than the PROJ that GDAL uses (QGIS logs
"proj.db ... comes from another PROJ installation"). QGeo 3D works around it, but QGIS itself then
falls back to approximate datum shifts (e.g. GDA94 to GDA2020): upgrade `proj` and `proj-data` with
the OSGeo4W setup.

## Building

    python scripts/build_zip.py        # dist/QGeo3D-<version>.zip

Tagging a release `vX.Y.Z` on GitHub builds the zip and attaches it to the release
(`.github/workflows/release.yml`).

## Licence

GNU General Public License v2 or later (see LICENSE), like QGIS and Qgis2threejs.
