# -*- coding: utf-8 -*-
"""Best graphics settings for the device QGIS runs on.

1. Windows with more than one real GPU (laptop: integrated + NVIDIA / AMD / Arc): ask Windows to
   run QGIS and its web-view process (QtWebEngineProcess, which draws the 3D view) on the
   high-performance GPU - the per-user setting from Settings > Display > Graphics. Only added when
   the user hasn't chosen for that program already; takes effect the next time QGIS starts.
2. In the 3D view (q2t4.js Q2T4.tuneGpu): read the WebGL renderer and set the drawing resolution
   for it - full resolution on a real GPU, lower on a weak one, 1:1 on a software renderer.

Qgis2threejs itself already starts Chromium with --ignore-gpu-blocklist --enable-gpu-rasterization.
"""

import contextlib
import os
import re
import sys

KEY_AUTO = "/QGeo3D/gpuAuto"
KEY_SET = "/QGeo3D/gpuPrefsSet"         # programs we gave a GPU preference (so it can be undone)
_PREFS = r"Software\Microsoft\DirectX\UserGpuPreferences"
_CLASS = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
# adapters that can't draw 3D themselves (USB docks, remote desktop, virtual displays)
_NOT_3D = re.compile(r"displaylink|basic (display|render)|remote|virtual|hyper-v|citrix|parsec|mirage|idd|"
                     r"spacedesk|duet|splashtop|teamviewer|vnc|rdp|indirect", re.I)


def _vendor(dev_id, name):
    d, n = (dev_id or "").upper(), (name or "").lower()
    if "VEN_10DE" in d or "nvidia" in n:
        return "nvidia"
    if "VEN_1002" in d or "amd" in n or "radeon" in n:
        return "amd"
    if "VEN_8086" in d or "intel" in n:
        return "intel"
    if "qualcomm" in n or "adreno" in n:
        return "qualcomm"
    if "apple" in n:
        return "apple"
    return "other"


def is_discrete(vendor, name):
    n = (name or "").lower()
    if vendor == "nvidia":
        return True
    if vendor == "amd":       # 'Radeon(TM) 890M Graphics', 'Radeon Vega 8 Graphics' are integrated
        return bool(re.search(r"\brx\b|radeon pro|firepro|\bw\d{4}|vii|instinct", n)) and \
            not re.search(r"\d{3,4}m\b|vega \d+ graphics|radeon\(tm\) graphics", n)
    if vendor == "intel":     # Arc A-/B-series cards are discrete; 'Arc Graphics', Iris, UHD are not
        return bool(re.search(r"arc\(tm\) [ab]\d|arc [ab]\d", n))
    return False


def adapters():
    """[{name, vendor, discrete, vram}] of the GPUs that can draw 3D (Windows; [] elsewhere)."""
    if sys.platform != "win32":
        return []
    import winreg
    out = []
    try:
        k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _CLASS)
    except OSError:
        return []
    i = 0
    while True:
        try:
            sub = winreg.EnumKey(k, i)
        except OSError:
            break
        i += 1
        try:
            s = winreg.OpenKey(k, sub)
        except OSError:
            continue

        def q(n):
            try:
                return winreg.QueryValueEx(s, n)[0]
            except OSError:
                return None
        name, dev = q("DriverDesc"), q("MatchingDeviceId")
        if not name or _NOT_3D.search(name) or (dev or "").upper().startswith(("USB", "ROOT", "SWD")):
            continue
        mem = q("HardwareInformation.qwMemorySize") or q("HardwareInformation.MemorySize")
        if isinstance(mem, bytes):
            mem = int.from_bytes(mem[:8], "little")
        v = _vendor(dev, name)
        out.append({"name": name, "vendor": v, "discrete": is_discrete(v, name), "vram": int(mem or 0)})
    return out


def _programs():
    """QGIS and the Qt web-view process that does the 3D drawing."""
    from qgis.PyQt.QtCore import QCoreApplication, QLibraryInfo
    exe = os.path.normpath(QCoreApplication.applicationFilePath())
    progs = [exe]
    cand = []
    loc = ""
    with contextlib.suppress(Exception):
        if hasattr(QLibraryInfo, "LibraryPath"):            # Qt6
            loc = QLibraryInfo.path(QLibraryInfo.LibraryPath.LibraryExecutablesPath)
        else:                                               # Qt5
            loc = QLibraryInfo.location(getattr(QLibraryInfo, "LibraryExecutablesPath"))
    for d in (loc, os.path.join(os.path.dirname(os.path.dirname(exe)), "apps", "Qt5", "bin"),
              os.path.join(os.path.dirname(os.path.dirname(exe)), "apps", "Qt6", "bin"), os.path.dirname(exe)):
        if d:
            cand.append(os.path.join(d, "QtWebEngineProcess.exe"))
    for c in cand:
        if os.path.isfile(c):
            progs.append(os.path.normpath(c))
            break
    return progs


def prefer_high_performance():
    """Windows, more than one real GPU: run QGIS + QtWebEngineProcess on the high-performance one.
    Returns (changed programs, message) - message None when nothing needed doing."""
    if sys.platform != "win32":
        return [], None
    ad = adapters()
    if len(ad) < 2:
        return [], None                    # one GPU: Windows has nothing to choose
    import winreg
    from qgis.PyQt.QtCore import QSettings
    try:
        k = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, _PREFS, 0, winreg.KEY_READ | winreg.KEY_SET_VALUE)
    except OSError:
        return [], None
    changed = []
    for p in _programs():
        try:
            winreg.QueryValueEx(k, p)
            continue                       # the user (or we, earlier) already chose for this program
        except OSError:
            pass
        try:
            winreg.SetValueEx(k, p, 0, winreg.REG_SZ, "GpuPreference=2;")
            changed.append(p)
        except OSError:
            pass
    if changed:
        s = QSettings()
        done = list(s.value(KEY_SET, []) or [])
        s.setValue(KEY_SET, sorted(set(done) | set(changed)))
        best = next((a["name"] for a in ad if a["discrete"]), ad[0]["name"])
        return changed, (f"QGIS set to use the high-performance GPU ({best}) for the 3D view - "
                         "restart QGIS for it to take effect.")
    return [], None


def undo_preferences():
    """Remove only the GPU preferences this plugin added."""
    if sys.platform != "win32":
        return
    import winreg
    from qgis.PyQt.QtCore import QSettings
    s = QSettings()
    done = list(s.value(KEY_SET, []) or [])
    try:
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _PREFS, 0, winreg.KEY_READ | winreg.KEY_SET_VALUE)
    except OSError:
        s.remove(KEY_SET)
        return
    for p in done:
        try:
            if winreg.QueryValueEx(k, p)[0] == "GpuPreference=2;":
                winreg.DeleteValue(k, p)
        except OSError:
            pass
    s.remove(KEY_SET)


def tier(renderer):
    """'software' | 'low' | 'mid' | 'high' from a WebGL UNMASKED_RENDERER string such as
    'ANGLE (AMD, AMD Radeon(TM) 890M Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)'."""
    r = (renderer or "").lower()
    if not r or re.search(r"swiftshader|llvmpipe|softpipe|software|basic render|microsoft basic|mesa offscreen", r):
        return "software"
    v = _vendor("", r)
    if is_discrete(v, r) or re.search(r"apple m\d (pro|max|ultra)", r):
        return "high"
    if re.search(r"intel.*(hd graphics|uhd graphics \d{3}\b|hd \d{3,4})|mali|powervr|adreno \d{3}\b", r):
        return "low"
    return "mid"            # Iris Xe, Arc iGPU, Radeon 680M/780M/890M, Apple M-series, Adreno 7xx


# drawing resolution (pixel ratio cap) per tier; None = the screen's own (full sharpness)
PIXEL_RATIO = {"software": 1.0, "low": 1.25, "mid": None, "high": None}


def describe(renderer):
    """'AMD Radeon(TM) 890M Graphics (Direct3D 11)' from a WebGL renderer string."""
    m = re.match(r"ANGLE \(([^,]+), (.+?)(?: Direct3D(\d+).*?)?(?:, (D3D\d+|OpenGL[^)]*|Vulkan[^)]*))?\)$",
                 renderer or "")
    if m:
        api = m.group(4) or (f"D3D{m.group(3)}" if m.group(3) else "")
        api = {"D3D11": "Direct3D 11", "D3D9": "Direct3D 9"}.get(api, api)
        return f"{m.group(2).strip()}" + (f" ({api})" if api else "")
    return renderer or "unknown"
