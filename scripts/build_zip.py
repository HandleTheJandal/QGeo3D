"""Build an installable plugin zip: dist/QGeo3D-<version>.zip
(QGIS ▸ Plugins ▸ Manage and Install Plugins ▸ Install from ZIP). Run from anywhere:
    python scripts/build_zip.py
Skips caches, GeoPackages and other run-time files that may sit in the package folder."""
import configparser
import os
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
TOP = os.path.dirname(HERE)
PKG = "QGeo3D"
SKIP_DIRS = {"__pycache__"}
SKIP_EXT = (".pyc", ".gpkg", ".gpkg-wal", ".gpkg-shm", ".qto3settings", ".zip")


def build():
    cp = configparser.ConfigParser()
    cp.read(os.path.join(TOP, PKG, "metadata.txt"), encoding="utf-8")
    ver = cp["general"]["version"]
    os.makedirs(os.path.join(TOP, "dist"), exist_ok=True)
    out = os.path.join(TOP, "dist", f"{PKG}-{ver}.zip")
    n = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for d, dirs, files in os.walk(os.path.join(TOP, PKG)):
            dirs[:] = sorted(x for x in dirs if x not in SKIP_DIRS and not x.startswith("."))
            for f in sorted(files):
                if f.endswith(SKIP_EXT) or f.startswith("."):
                    continue
                p = os.path.join(d, f)
                info = zipfile.ZipInfo.from_file(p, os.path.relpath(p, TOP).replace(os.sep, "/"))
                info.external_attr = 0o100644 << 16      # plain rw-r--r-- files (no executable bits)
                info.compress_type = zipfile.ZIP_DEFLATED
                with open(p, "rb") as fh:
                    z.writestr(info, fh.read())
                n += 1
    print(f"{out}: {n} files, {os.path.getsize(out):,} bytes")
    return out


if __name__ == "__main__":
    build()
