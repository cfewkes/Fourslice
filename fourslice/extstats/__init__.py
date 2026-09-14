"""fourslice/extstats -- most-recent external stat snapshots.

Snapshots, caches and parses the most recent Smogon month and the most
recent Champions regulation from the raw mirrors under data/external,
holding parsed, ready-to-serve data in data/stats-cache. Nothing here
touches the network: fetching the mirrors is the scraping package's job.
"""

from .cache import CacheCorruptError, SnapshotStore
from .models import (
    MOST_RECENT,
    SMOGON_ELOS,
    ExtStatsError,
    Month,
    MostRecent,
    Period,
    PeriodUnavailableError,
    SnapshotFile,
    SnapshotRecord,
    utc_now,
)
from .smogon import ALL_FORMATS
from .paths import cache_dir, mirror_dir
from .pipeline import refresh, refresh_all_formats, summary

__all__ = [
    "CacheCorruptError",
    "ExtStatsError",
    "MOST_RECENT",
    "Month",
    "MostRecent",
    "Period",
    "PeriodUnavailableError",
    "SMOGON_ELOS",
    "SnapshotFile",
    "SnapshotRecord",
    "SnapshotStore",
    "ALL_FORMATS",
    "cache_dir",
    "mirror_dir",
    "refresh",
    "refresh_all_formats",
    "summary",
    "utc_now",
]