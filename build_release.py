"""
build_release.py

Automated cross-platform release packaging script for Fourslice.
Builds standalone Windows, macOS, or Linux applications using PyInstaller,
verifies all required data assets and dependencies are bundled, and produces
distributable archives (.zip / .tar.gz) ready for itch.io.
"""

import os
import platform
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path
from PIL import Image

VERSION = "1.0.0"
APP_NAME = "Fourslice"
REPO_ROOT = Path(__file__).resolve().parent
DIST_DIR = REPO_ROOT / "dist"
BUILD_DIR = REPO_ROOT / "build"
OUTPUT_FOLDER = DIST_DIR / APP_NAME
APP_BUNDLE = DIST_DIR / f"{APP_NAME}.app"

IS_WINDOWS = sys.platform == "win32"
IS_MACOS = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")

# Architecture tag for release filenames
machine = platform.machine().lower()
if machine in ("amd64", "x86_64"):
    ARCH_TAG = "x64"
elif machine in ("arm64", "aarch64"):
    ARCH_TAG = "arm64"
else:
    ARCH_TAG = machine

if IS_WINDOWS:
    OS_TAG = "windows"
    ARCHIVE_EXT = ".zip"
elif IS_MACOS:
    OS_TAG = "macos"
    ARCHIVE_EXT = ".zip"
else:
    OS_TAG = "linux"
    ARCHIVE_EXT = ".tar.gz"

ARCHIVE_NAME = f"{APP_NAME}-v{VERSION}-{OS_TAG}-{ARCH_TAG}{ARCHIVE_EXT}"
ARCHIVE_PATH = DIST_DIR / ARCHIVE_NAME

DEAD_QT_MODULE_PREFIXES = (
    "Qt6Qml",
    "Qt6Quick",
    "Qt6Pdf",
    "Qt6VirtualKeyboard",
    "libQt6Qml",
    "libQt6Quick",
    "libQt6Pdf",
    "libQt6VirtualKeyboard",
    "QtQml",
    "QtQuick",
    "QtPdf",
    "QtVirtualKeyboard",
)


def ensure_icons():
    """Ensure platform icon files are generated."""
    assets_dir = REPO_ROOT / "fourslice" / "gui" / "assets"
    png_path = assets_dir / "FourSliceLogo.png"
    ico_path = assets_dir / "FourSliceLogo.ico"
    icns_path = assets_dir / "FourSliceLogo.icns"

    if png_path.is_file():
        img = Image.open(png_path)
        img.save(
            ico_path,
            format="ICO",
            sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
        )
        print(f"[+] Generated multi-size ICO: {ico_path}")

        if IS_MACOS and shutil.which("iconutil"):
            iconset_dir = assets_dir / "FourSliceLogo.iconset"
            iconset_dir.mkdir(exist_ok=True)
            sizes = [16, 32, 64, 128, 256, 512]
            for s in sizes:
                img.resize((s, s), Image.LANCZOS).save(iconset_dir / f"icon_{s}x{s}.png")
                if s <= 256:
                    img.resize((s * 2, s * 2), Image.LANCZOS).save(iconset_dir / f"icon_{s}x{s}@2x.png")
            try:
                subprocess.run(
                    ["iconutil", "-c", "icns", str(iconset_dir), "-o", str(icns_path)],
                    check=True,
                )
                print(f"[+] Generated macOS ICNS icon: {icns_path}")
            except Exception as e:
                print(f"[!] Warning: Failed to generate ICNS with iconutil ({e})")
            finally:
                shutil.rmtree(iconset_dir, ignore_errors=True)


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


def _find_internal_dir() -> Path:
    """Locate the PyInstaller internal / bundle support directory across platforms."""
    candidates = [
        OUTPUT_FOLDER / "_internal",
        OUTPUT_FOLDER,
        APP_BUNDLE / "Contents" / "Resources" / "_internal",
        APP_BUNDLE / "Contents" / "Frameworks" / "_internal",
        APP_BUNDLE / "Contents" / "Resources",
    ]
    for c in candidates:
        if (c / "data").is_dir():
            return c
    return OUTPUT_FOLDER / "_internal" if (OUTPUT_FOLDER / "_internal").exists() else OUTPUT_FOLDER



def prune_bundle():
    """Remove Qt binaries and translation files the app can never load."""
    search_dirs = [OUTPUT_FOLDER, APP_BUNDLE]
    removed = []

    for base_dir in search_dirs:
        if not base_dir.exists():
            continue

        for root, dirs, files in os.walk(base_dir, topdown=True):
            # Check translation directories
            if os.path.basename(root) == "translations" and "PySide6" in root:
                for f in list(files):
                    fp = Path(root) / f
                    try:
                        fp.unlink()
                        removed.append(f"PySide6/translations/{f}")
                    except OSError:
                        pass

            # Prune dead Qt dynamic libraries / modules
            for fn in list(files):
                if any(fn.startswith(p) for p in DEAD_QT_MODULE_PREFIXES):
                    fp = Path(root) / fn
                    try:
                        fp.unlink()
                        removed.append(fn)
                    except OSError:
                        pass

    if removed:
        print(f"[+] Pruned {len(removed)} dead Qt files "
              f"({', '.join(removed[:4])}{'...' if len(removed) > 4 else ''})")
    else:
        print("[+] prune_bundle: nothing to remove (already lean)")


def stage_notices():
    """Copy THIRD_PARTY_NOTICES.md into the bundle / package output."""
    src = REPO_ROOT / "THIRD_PARTY_NOTICES.md"
    if not src.is_file():
        print("[!] THIRD_PARTY_NOTICES.md not found at repo root; skipping")
        return

    if OUTPUT_FOLDER.is_dir():
        shutil.copy2(src, OUTPUT_FOLDER / "THIRD_PARTY_NOTICES.md")
    if APP_BUNDLE.is_dir():
        resources = APP_BUNDLE / "Contents" / "Resources"
        resources.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, resources / "THIRD_PARTY_NOTICES.md")
        shutil.copy2(src, DIST_DIR / "THIRD_PARTY_NOTICES.md")
    print("[+] Staged third-party notices into the bundle")


def verify_build():
    """Check that all essential files and folders exist in the build output."""
    print("[*] Verifying build output...")

    # Verify executable
    if IS_WINDOWS:
        exe_path = OUTPUT_FOLDER / f"{APP_NAME}.exe"
    elif IS_MACOS and APP_BUNDLE.is_dir():
        exe_path = APP_BUNDLE / "Contents" / "MacOS" / APP_NAME
    else:
        exe_path = OUTPUT_FOLDER / APP_NAME

    if not exe_path.is_file():
        raise FileNotFoundError(f"Missing executable: {exe_path}")

    # Check data directory
    internal_dir = _find_internal_dir()
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

    print("[+] Build verification passed.")



def create_archive():
    """Create a distributable archive (.zip or .tar.gz) containing the application."""
    print(f"[*] Creating distribution archive: {ARCHIVE_PATH}")
    if ARCHIVE_PATH.exists():
        ARCHIVE_PATH.unlink()

    if IS_MACOS and APP_BUNDLE.is_dir():
        # Use macOS ditto if available (preserves file attributes, signatures, symlinks)
        if shutil.which("ditto"):
            cmd = [
                "ditto",
                "-c",
                "-k",
                "--sequesterRsrc",
                "--keepParent",
                str(APP_BUNDLE),
                str(ARCHIVE_PATH),
            ]
            subprocess.run(cmd, check=True)
        else:
            _create_zip_folder(APP_BUNDLE, APP_BUNDLE.name)
    elif IS_LINUX:
        # Create .tar.gz for Linux (standard for preserving Unix permissions)
        with tarfile.open(ARCHIVE_PATH, "w:gz") as tar:
            tar.add(OUTPUT_FOLDER, arcname=APP_NAME)
        # Also create a .zip for itch.io Linux users who prefer zip
        zip_path = DIST_DIR / f"{APP_NAME}-v{VERSION}-linux-{ARCH_TAG}.zip"
        _create_zip_folder(OUTPUT_FOLDER, APP_NAME, out_path=zip_path)
    else:
        # Windows .zip
        _create_zip_folder(OUTPUT_FOLDER, APP_NAME)

    archive_size_mb = ARCHIVE_PATH.stat().st_size / (1024 * 1024)
    print(f"[+] Packaged release archive: {ARCHIVE_PATH.name} ({archive_size_mb:.2f} MB)")


def _create_zip_folder(folder_to_zip: Path, arc_root_name: str, out_path: Path | None = None):
    """Zip a folder preserving file hierarchy and unix execute bits."""
    target_zip = out_path if out_path is not None else ARCHIVE_PATH
    if target_zip.exists():
        target_zip.unlink()

    total_files = 0
    with zipfile.ZipFile(target_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as zf:
        for root, dirs, files in os.walk(folder_to_zip):
            for file in files:
                file_path = Path(root) / file
                rel_path = Path(arc_root_name) / file_path.relative_to(folder_to_zip)
                zf.write(file_path, arcname=str(rel_path))
                total_files += 1

    print(f"[+] Packaged {total_files} files into {target_zip.name}")



def main():
    print(f"=== Building {APP_NAME} v{VERSION} on {sys.platform} ({ARCH_TAG}) ===")
    ensure_icons()
    run_pyinstaller()
    prune_bundle()
    stage_notices()
    verify_build()
    create_archive()
    print("\n=== Release Build Complete ===")
    print(f"Archive Location: {ARCHIVE_PATH}")


if __name__ == "__main__":
    main()

