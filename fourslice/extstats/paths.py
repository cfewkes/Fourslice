"""
fourslice/extstats/paths.py

Filesystem anchors for the snapshot pipeline.

The raw mirrors live under <app_data>/external/<source> (written by the
scraping package); snapshots and parsed JSON land under
<app_data>/stats-cache. Using the OS's per-app data directory (via
platformdirs) keeps every entry point -- CLIs, tests, the GUI -- pointing
at the same folders no matter where the process is launched from, and works
correctly for installed apps where the package folder may not be writable.

A `base_dir` override is accepted for testing (pointing at a temp dir).
"""

import platformdirs
from pathlib import Path

APP_NAME = "Fourslice"


def get_app_data_dir(base_dir: Path | None = None) -> Path:
    """Root directory for all fourslice data (same as config.get_app_data_dir)."""
    d = base_dir if base_dir is not None else Path(platformdirs.user_data_dir(APP_NAME, appauthor=False))
    d.mkdir(parents=True, exist_ok=True)
    return d


def mirror_dir(source: str, base_dir: Path | None = None) -> Path:
    """Root of one source's raw mirror, e.g. .../data/external/smogon."""
    return get_app_data_dir(base_dir) / "external" / source


def cache_dir(base_dir: Path | None = None) -> Path:
    """The snapshot cache's root directory (SnapshotStore's default home)."""
    return get_app_data_dir(base_dir) / "stats-cache"