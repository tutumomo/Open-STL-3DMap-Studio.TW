# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build definition for 3D Kaart Bouwer NL.

Build with:  .venv\\Scripts\\pyinstaller.exe packaging\\3dmaps.spec --noconfirm
Set BUILD_CONSOLE=1 first to keep a console window for debugging.
"""

import os

from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
CONSOLE = os.environ.get("BUILD_CONSOLE") == "1"

datas = [(os.path.join(ROOT, "web"), "web")]
binaries = []
hiddenimports = []

# These three ship data files or native libraries that PyInstaller cannot infer
# from imports alone: pyproj its PROJ grids, trimesh its resources, ifcopenshell
# its compiled geometry kernel and schema definitions.
for package in ("pyproj", "trimesh", "ifcopenshell"):
    pkg_datas, pkg_binaries, pkg_hidden = collect_all(package)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hidden

# uvicorn resolves its protocol implementations by string at runtime.
hiddenimports += collect_submodules("uvicorn")
hiddenimports += [
    "mapbox_earcut",
    "shapely",
    "lxml._elementpath",
    "encodings.idna",
]

a = Analysis(
    [os.path.join(ROOT, "run_app.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Pillow is only used by make_icon.py at build time, never at runtime.
    excludes=["matplotlib", "scipy", "IPython", "pytest", "PyQt5", "PySide2",
              "PIL", "pandas", "setuptools", "pip"],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Open STL-3DMap Studio",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=CONSOLE,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join(SPECPATH, "app.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Open STL-3DMap Studio",
)
