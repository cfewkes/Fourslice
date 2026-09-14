"""
fourslice/extstats/models.py

Core value types for the external-stats snapshot pipeline.

Two data sources, two period kinds:

    smogon    -- one Month  of ladder usage,    keyed like "2026-07"

Snapshots are requested with the SAME explicit sentinel by default:

    MOST_RECENT   "whatever the source has published most recently"

MOST_RECENT is deliberately a singleton object, not a date string: a
request expresses intent ("refresh the current month"), and the concrete
period id is resolved against the source's own listing when the snapshot
runs. A stale month string can never be baked into a request by
accident.

The on-disk cache (SnapshotStore in cache.py) keeps at most one snapshot
per source at a time -- the most recent -- and prunes a previous period
the moment a newer one is snapshotted.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

SMOGON_ELOS = ("0", "1500", "1630", "1760")


class ExtStatsError(Exception):
    """Base class for all external-stats pipeline errors."""


class PeriodUnavailableError(ExtStatsError):
    """A requested period could not be found in the source's data (for
    example MOST_RECENT when the raw mirror holds no month listings)."""


class MostRecent:
    """Singleton sentinel meaning "the source's most recently published period".

    Period ids are always strings ("2026-07"); this object is not,
    so it can never be mistaken for a concrete id, and vice versa.
    """

    _instance: MostRecent | None = None

    def __new__(cls) -> MostRecent:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "MOST_RECENT"

    def __reduce__(self):
        return (MostRecent, ())


MOST_RECENT = MostRecent()


@dataclass(frozen=True)
class Month:
    """A Smogon monthly usage release -- one folder under /stats/.

    elos is informational: the pipeline snapshots every ladder division
    the mirror publishes for the month's most-played format, whatever
    this field says. SMOGON_ELOS lists the complete division set the
    site publishes.
    """

    id: str
    source: Literal["smogon"] = "smogon"
    elos: tuple[str, ...] = ("0",)


Period = Month


def utc_now() -> str:
    """ISO-8601 UTC timestamp, used as snapshot provenance."""
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class SnapshotFile:
    """One raw file captured for a snapshot, keyed by its cache-relative path."""

    rel_path: str
    sha256: str
    size: int


@dataclass(frozen=True)
class SnapshotRecord:
    """Everything captured for one period of one source.

    files  -- raw copies of the source's files (paths relative to the
              cache root)
    parsed -- parsed JSON documents (paths relative to the cache root)

    Every rel path must start "<source>/<period_id>/"; SnapshotStore.put
    enforces that, and the invariant is what makes whole-period pruning
    safe.
    """

    source: str
    period_id: str
    fetched_at: str
    files: tuple[SnapshotFile, ...] = ()
    parsed: tuple[str, ...] = ()
