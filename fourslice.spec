# -*- mode: python ; coding: utf-8 -*-
import sys
from pathlib import Path

block_cipher = None

repo_root = Path(__file__).resolve().parent if "__file__" in locals() else Path.cwd()

# Only the Showdown/learnset data files the app actually reads at runtime are
# bundled -- never the whole data/ tree. data/external/ is the developer's
# local crawl mirror (hundreds of MB of .gz/.json.gz), written to app-data at
# runtime by the crawlers, read from app-data, and dead weight in a release.
_SHIPPED_DATA_FILES = [
    "abilities.js",
    "formats-data.js",
    "formats.js",
    "items.js",
    "learnsets.ts",
    "moves.js",
    "pokedex.js",
    "champions-learsets.ts",
]

# PyInstaller treats the second element of a datas 2-tuple as the *target
# directory* and appends the source basename to it. Pointing the destination
# at "data/<name>" would therefore nest each file at data/<name>/<name>.
# Using "data" keeps the layout flat so the app's sys._MEIPASS/data lookups
# resolve -- build_release.verify_build() enforces this.
datas = [
    (str(repo_root / "data" / fn), "data") for fn in _SHIPPED_DATA_FILES
] + [
    (str(repo_root / "fourslice" / "gui" / "assets"), "fourslice/gui/assets"),
]

hiddenimports = [
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
    "matplotlib.backends.backend_qtagg",
    "platformdirs",
    "pandas",
    "sqlite3",
    "requests",
]

excludes = [
    "tkinter",
    # NOTE: "unittest" must NOT be excluded here. matplotlib pulls in
    # pyparsing, which eagerly imports pyparsing.testing -> unittest at
    # module load; excluding unittest crashes the frozen app at startup
    # with ModuleNotFoundError. Verified by the release smoke test.
    "test",
    "tests",
    "pytest",
    "IPython",
    "jupyter",
    "scipy",
    "PySide6.QtQml",
    "PySide6.QtQuick",
    "PySide6.QtQuick3D",
    "PySide6.Qt3DCore",
    "PySide6.Qt3DRender",
    "PySide6.Qt3DAnimation",
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick",
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
    "PySide6.QtDesigner",
    "PySide6.QtBluetooth",
    "PySide6.QtPositioning",
    "PySide6.QtSensors",
    "PySide6.QtSerialPort",
    "PySide6.QtNfc",
    "PySide6.QtPdf",
    "PySide6.QtPdfWidgets",
    "PySide6.QtSql",
    "PySide6.QtXml",
    "PySide6.QtHelp",
    "PySide6.QtTest",
]

a = Analysis(
    ["main.py"],
    pathex=[str(repo_root)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# Platform-specific icon selection
if sys.platform == "darwin":
    icns_file = repo_root / "fourslice" / "gui" / "assets" / "FourSliceLogo.icns"
    if icns_file.exists():
        app_icon = str(icns_file)
    else:
        app_icon = str(repo_root / "fourslice" / "gui" / "assets" / "FourSliceLogo.png")
elif sys.platform == "win32":
    app_icon = str(repo_root / "fourslice" / "gui" / "assets" / "FourSliceLogo.ico")
else:
    app_icon = str(repo_root / "fourslice" / "gui" / "assets" / "FourSliceLogo.png")

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Fourslice",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=app_icon,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Fourslice",
)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="Fourslice.app",
        icon=app_icon if app_icon.endswith(".icns") else None,
        bundle_identifier="io.itch.cfewkes.fourslice",
        info_plist={
            "CFBundleName": "Fourslice",
            "CFBundleDisplayName": "Fourslice",
            "CFBundleExecutable": "Fourslice",
            "CFBundleShortVersionString": "1.0.0",
            "CFBundleVersion": "1.0.0",
            "NSHighResolutionCapable": "True",
            "LSMinimumSystemVersion": "10.15",
            "NSHumanReadableCopyright": "Copyright © 2026",
        },
    )


