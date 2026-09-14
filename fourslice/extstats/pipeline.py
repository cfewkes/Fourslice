"""
fourslice/extstats/pipeline.py

High-level entry point for the stats backend: turn the raw mirrors into
parsed snapshots.

The mirrors under data/external/<source> are the raw, unparsed data the
scraping package pulls from the sites. This pipeline turns each source's
requested period into a parsed snapshot under data/stats-cache:

    refresh(...)            snapshot + parse + cache the most popular Smogon
                           formats; defaults to the most recent Smogon month
    refresh_all_formats(...)  backfill ALL remaining Smogon formats (idempotent)
    summary(...)            a human-readable recap of what was stored

Smogon keeps the top formats (by default top Singles + top Doubles) in
refresh() for an immediate UI, with refresh_all_formats() storing every
other format the month publishes.

Nothing here touches the network -- it only reads mirrors the scraping
package has already populated.
"""

from __future__ import annotations

import json
from pathlib import Path

from .cache import SnapshotStore
from .models import (
    MOST_RECENT,
    ExtStatsError,
    PeriodUnavailableError,
    SnapshotRecord,
)
from .paths import cache_dir, mirror_dir
from .smogon import run as smogon_run, run_all_formats

DEFAULT_SOURCES = ("smogon",)


def refresh(
    *,
    mirror_root: Path | str | None = None,
    cache_root: Path | str | None = None,
    base_dir: Path | None = None,
    sources: tuple[str, ...] = DEFAULT_SOURCES,
    smogon_spec=MOST_RECENT,
    season: str = "Current",
    smogon_initial_formats: int = 2,
) -> dict[str, SnapshotRecord]:
    """Snapshot, parse and cache the requested period for each source."""
    mirror_root = Path(mirror_root) if mirror_root is not None else mirror_dir("smogon", base_dir).parent
    cache_root = Path(cache_root) if cache_root is not None else cache_dir(base_dir)
    store = SnapshotStore(cache_root)
    records: dict[str, SnapshotRecord] = {}
    failures: dict[str, str] = {}

    if "smogon" in sources:
        try:
            records["smogon"] = smogon_run(
                mirror_dir("smogon", base_dir),
                store,
                smogon_spec,
                initial_formats=smogon_initial_formats
            )
        except PeriodUnavailableError as exc:
            failures["smogon"] = str(exc)

    if failures:
        done = ", ".join(sorted(records)) or "nothing"
        raise ExtStatsError(f"refreshed {done}; failed sources: {failures}")

    return records


def refresh_all_formats(
    *,
    mirror_root: Path | str | None = None,
    cache_root: Path | str | None = None,
    base_dir: Path | None = None,
) -> SnapshotRecord | None:
    """Backfill every remaining Smogon format for the latest cached month.

    Compares the mirror's format standings against stats.db's capture_log
    and calls smogon.run_single_format() for each format not yet fully
    captured there. Idempotent: safe to call after refresh() or re-run if
    an earlier call was interrupted or hit an error on some format.

    Returns the updated Smogon SnapshotRecord, or None if no Smogon snapshot exists.
    """
    mirror_root = Path(mirror_root) if mirror_root is not None else mirror_dir("smogon", base_dir).parent
    cache_root = Path(cache_root) if cache_root is not None else cache_dir(base_dir)
    store = SnapshotStore(cache_root)

    # Load latest cached Smogon snapshot to identify which month to backfill
    cached = store.latest("smogon")
    if not cached:
        return None

    return run_all_formats(
        mirror_dir("smogon", base_dir),
        store,
        spec=cached.period_id
    )


def _read_json(path: Path) -> dict | None:
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def summary(records: dict[str, SnapshotRecord], *, base_dir: Path | None = None) -> str:
    """Human-readable recap of the stored snapshots (for CLIs and logs)."""
    root = cache_dir(base_dir)
    store = SnapshotStore(root)
    lines = ["External stats cache refreshed from local mirrors"]
    for source in ("smogon",):
        record = records.get(source)
        if record is None:
            continue
        stamp = record.fetched_at  # ISO-8601 UTC timestamp string
        lines.append(f"{source}[{record.period_id}] fetched {stamp}")
        lines.append("  raw:     " + ", ".join(f.rel_path for f in record.files))
        if source == "smogon":
            from . import statsdb
            from fourslice import config as _config

            conn = statsdb.init_db(_config.get_stats_db_path())
            try:
                standings = statsdb.get_format_standings(conn, record.period_id)
                captured = []
                total_rows = 0
                for entry in standings:
                    elos = statsdb.get_captured_elos(conn, record.period_id, entry["format"])
                    if elos:
                        captured.append(f"{entry['format']} ({', '.join(sorted(elos))})")
                        for elo in elos:
                            total_rows += len(
                                statsdb.get_format_rows(conn, record.period_id, entry["format"], elo)
                            )
            finally:
                conn.close()
            lines.append(f"  captured: {'; '.join(captured) if captured else '(none yet)'}")
            lines.append(f"  {total_rows} Pokemon rows stored across captured format/elo tables")
            if standings:
                shown = ", ".join(
                    f"{e['format']} ({e['battles']}, {e['kind']})" for e in standings
                )
                lines.append(f"  order (most played -> least): {shown}")
    return "\n".join(lines)