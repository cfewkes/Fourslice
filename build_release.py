"""
build_release.py

Automated release packaging script for Fourslice.
Builds the standalone Windows application using PyInstaller, verifies all required
data assets and dependencies are bundled, and produces a distributable .zip for itch.io.
"""

import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from PIL import Image

VERSION = "1.0.0"
APP_NAME = "Fourslice"
REPO_ROOT = Path(__file__).resolve().parent
DIST_DIR = REPO_ROOT / "dist"
BUILD_DIR = REPO_ROOT / "build"
OUTPUT_FOLDER = DIST_DIR / APP_NAME
ZIP_NAME = f"{APP_NAME}-v{VERSION}-windows-x64.zip"
ZIP_PATH = DIST_DIR / ZIP_NAME

# Qt binaries that can never be loaded at runtime: their Python modules
# (PySide6.QtQml / QtQuick / QtPdf / QtVirtualKeyboard) are excluded by the
# spec and absent from the bundle, so no code path can reach these DLLs;
# they ride along because PySide6's Qt6Core/Gui/Widgets dependency scan
# pulls in the whole Qt6 family. Pruning them saves ~19 MB.
# opengl32sw.dll is deliberately kept: it is Qt's software-OpenGL fallback,
# needed by VM / remote-desktop / GPU-less users (PyInstaller adds it
# unconditionally via hook-PySide6's collect_extra_binaries).
DEAD_QT_DLLS = [
    "Qt6Qml.dll",
    "Qt6QmlMeta.dll",
    "Qt6QmlModels.dll",
    "Qt6QmlWorkerScript.dll",
    "Qt6Quick.dll",
    "Qt6Pdf.dll",
    "Qt6VirtualKeyboard.dll",
]


def ensure_icons():
    """Ensure the .ico file is generated and contains all standard Windows icon sizes."""
    png_path = REPO_ROOT / "fourslice" / "gui" / "assets" / "FourSliceLogo.png"
    ico_path = REPO_ROOT / "fourslice" / "gui" / "assets" / "FourSliceLogo.ico"

    if png_path.is_file():
        img = Image.open(png_path)
        img.save(
            ico_path,
            format="ICO",
            sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
        )
        print(f"[+] Generated multi-size icon: {ico_path}")


def run_pyinstaller():
    """Run PyInstaller with fourslice.spec."""
    spec_path = REPO_ROOT / "fourslice.spec"
    print(f"[*] Running PyInstaller with spec: {spec_path}")

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        str(spec_path),
    ]

    res = subprocess.run(cmd, cwd=str(REPO_ROOT))
    if res.returncode != 0:
        print(f"[!] PyInstaller build failed with return code {res.returncode}")
        sys.exit(res.returncode)
    print("[+] PyInstaller build completed successfully.")


def verify_build():
    """Check that all essential files and folders exist in the build output."""
    print("[*] Verifying build output...")
    exe_path = OUTPUT_FOLDER / f"{APP_NAME}.exe"
    if not exe_path.is_file():
        raise FileNotFoundError(f"Missing executable: {exe_path}")

    # Check data directory in _internal
    internal_dir = OUTPUT_FOLDER / "_internal"
    if not internal_dir.is_dir():
        internal_dir = OUTPUT_FOLDER  # PyInstaller 5 or earlier layout fallback

    data_dir = internal_dir / "data"
    if not data_dir.is_dir():
        raise FileNotFoundError(f"Missing data directory in bundle: {data_dir}")

    required_data_files = [
        "pokedex.js",
        "learnsets.ts",
        "moves.js",
        "abilities.js",
        "items.js",
        "formats.js",
        "formats-data.js",
    ]
    for fn in required_data_files:
        if not (data_dir / fn).is_file():
            raise FileNotFoundError(f"Missing essential bundled data file: {data_dir / fn}")

    # Guard rail: the developer's local crawl mirror (data/external) and
    # runtime stats-cache must never ride along in a release -- they're
    # dead weight that the app writes to app-data at runtime anyway.
    for stale_name in ("external", "stats-cache"):
        stale_path = data_dir / stale_name
        if stale_path.is_dir():
            raise RuntimeError(
                f"data/{stale_name} leaked into the bundle ({stale_path}); "
                "exclude it from fourslice.spec datas before releasing."
            )

    assets_dir = internal_dir / "fourslice" / "gui" / "assets"
    if not assets_dir.is_dir():
        raise FileNotFoundError(f"Missing assets directory in bundle: {assets_dir}")

    # Guard rail: third-party notices should ship next to the exe.
    notices = OUTPUT_FOLDER / "THIRD_PARTY_NOTICES.md"
    if not notices.is_file():
        raise FileNotFoundError(
            f"Missing {notices.name} beside the executable; stage_notices() "
            "should have copied it from the repo root."
        )

    # Guard rail: dead Qt binaries must stay pruned (see prune_bundle below).
    pyside_dir = internal_dir / "PySide6"
    for dll in DEAD_QT_DLLS:
        if (pyside_dir / dll).is_file():
            raise RuntimeError(
                f"Dead Qt binary {dll} reappeared in the bundle ({pyside_dir / dll}); "
                "prune_bundle() should have removed it before verification."
            )

    print("[+] Build verification passed.")


def prune_bundle():
    """Remove Qt binaries and translation files the app can never load.

    The Qt6Qml/QtQuick/QtPdf/QtVirtualKeyboard DLLs and every Qt translation
    are provably dead in this app:
      * the corresponding Python modules (PySide6.QtQml, ...) were excluded
        by the spec and are NOT in the bundle -- no code can instantiate a
        QML/Quick/Pdf/VirtualKeyboard engine;
      * the app never installs a QTranslator, so the .qm files are never
        loaded.
    Deleting them here -- rather than fighting PyInstaller's Qt6 family
    dependency scan -- is deterministic and auditable, and verify_build()
    enforces that they do not return on future builds.
    """
    internal_dir = OUTPUT_FOLDER / "_internal"
    if not internal_dir.is_dir():
        internal_dir = OUTPUT_FOLDER  # PyInstaller 5 or earlier layout fallback
    pyside_dir = internal_dir / "PySide6"
    if not pyside_dir.is_dir():
        return

    removed = []
    for dll in DEAD_QT_DLLS:
        p = pyside_dir / dll
        if p.is_file():
            p.unlink()
            removed.append(f"PySide6/{dll}")

    translations = pyside_dir / "translations"
    if translations.is_dir():
        for f in sorted(translations.iterdir()):
            if f.is_file():
                f.unlink()
                removed.append(f"PySide6/translations/{f.name}")

    if removed:
        print(f"[+] Pruned {len(removed)} dead Qt files "
              f"({', '.join(removed[:4])}{'...' if len(removed) > 4 else ''})")
    else:
        print("[+] prune_bundle: nothing to remove (already lean)")


def stage_notices():
    """Copy THIRD_PARTY_NOTICES.md next to the executable so end users see
    the Showdown/Smogon provenance as soon as they unzip the release."""
    src = REPO_ROOT / "THIRD_PARTY_NOTICES.md"
    if src.is_file():
        shutil.copy2(src, OUTPUT_FOLDER / "THIRD_PARTY_NOTICES.md")
        print(f"[+] Staged third-party notices into the bundle")
    else:
        print("[!] THIRD_PARTY_NOTICES.md not found at repo root; skipping")


def create_zip():
    """Create a distributable zip archive containing the entire Fourslice application."""
    print(f"[*] Creating distribution archive: {ZIP_PATH}")
    if ZIP_PATH.exists():
        ZIP_PATH.unlink()

    total_files = 0
    with zipfile.ZipFile(ZIP_PATH, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as zf:
        for root, dirs, files in os.walk(OUTPUT_FOLDER):
            for file in files:
                file_path = Path(root) / file
                rel_path = file_path.relative_to(DIST_DIR)
                zf.write(file_path, arcname=str(rel_path))
                total_files += 1

    zip_size_mb = ZIP_PATH.stat().st_size / (1024 * 1024)
    print(f"[+] Packaged {total_files} files into {ZIP_PATH.name} ({zip_size_mb:.2f} MB)")


def main():
    print(f"=== Building {APP_NAME} v{VERSION} ===")
    ensure_icons()
    run_pyinstaller()
    prune_bundle()
    stage_notices()
    verify_build()
    create_zip()
    print("\n=== Release Build Complete ===")
    print(f"Zip Location: {ZIP_PATH}")


if __name__ == "__main__":
    main()
