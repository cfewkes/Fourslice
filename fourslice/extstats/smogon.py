"""
fourslice/extstats/smogon.py

Snapshot + parse the most recent Smogon month from the raw mirror.

The mirror (data/external/smogon/raw/stats/<month>/) holds the exact
usage text files the site publishes, named <format>-<elo>.txt for each
ladder division (0 / 1500 / 1630 / 1760). For the month, the pipeline
stores the most popular formats by default (top Singles + top Doubles)
for immediate UI, with a separate entry point to snapshot all remaining
formats on demand. Every format the mirror publishes can be stored and
parsed into stats.db.

The FULL format ordering for the month (most played -> least played) is
stored in stats.db's smogon_formats table: every format the month
published, its overall battles, and whether it is a singles or a doubles
format (site doubles formats carry "doubles" or "VGC" in the name).

Per-Pokemon data comes from TWO files per format+elo: the usage ladder
<fmt>-<elo>.txt is authoritative for ranks, usage percentages and raw
counts (the same file whose 'Total battles:' line drives format
standings); the parallel moveset/<fmt>-<elo>.txt, when present, fills in
each Pokemon's moveset breakdown. Capture into stats.db is all-or-nothing
per format+elo and is tracked in stats.db's capture_log (with the source
file named), not in any JSON manifest, so "is this already done" always
reflects what's actually in the database.

    resolve_month(mirror, spec)        MOST_RECENT | "2026-07" | Month -> Month
    format_standings(mirror, m)        all formats, most -> least played
    most_played_format(mirror, m)      the #1 format of format_standings()
    ladder_files_for(mirror, m, fmt)   every <fmt>-<elo>.txt the month has
    moveset_files_for(mirror, m, fmt)  every moveset/<fmt>-<elo>.txt the month has
    snapshot_month_for_formats(...)    copy listed formats' files
    parse_and_cache(store, rec, st)    parse files straight into stats.db
    run(mirror, store, spec)           resolve + snapshot + parse + cache

Everything here reads the mirror only -- no network. Keeping the mirror
fresh is the scraping package's job.
"""

from __future__ import annotations

import gzip
import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

from fourslice import config

from . import statsdb
from .cache import SnapshotStore
from .files import copy_with_checksum, sha256_hex
from .models import (
    MOST_RECENT,
    Month,
    PeriodUnavailableError,
    SnapshotRecord,
    SnapshotFile,
    utc_now,
)

MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
BATTLES_RE = re.compile(r"^\s*Total battles:\s*([0-9]+)\s*$")
USAGE_FILE_RE = re.compile(r"^(.+)-([0-9]+)\.txt(\.gz)?$")
PCT_RE = re.compile(r"^([\d.]+)%$")

# Sentinel for "snapshot every format the month publishes" instead of a
# fixed top-N. initial_formats <= 0 is interpreted as ALL_FORMATS.
ALL_FORMATS = -1


def month_dir(mirror: Path, month_id: str) -> Path:
    """The mirror folder holding one Smogon month's usage files."""
    return mirror / "raw" / "stats" / month_id


def _existing_months(mirror: Path) -> list[str]:
    base = mirror / "raw" / "stats"
    if not base.is_dir():
        return []
    return sorted(
        entry.name for entry in base.iterdir()
        if entry.is_dir() and MONTH_RE.match(entry.name)
    )


def discover_latest_month(mirror: Path) -> Month:
    """The newest YYYY-MM folder present in the mirror's stats tree."""
    months = _existing_months(mirror)
    if not months:
        raise PeriodUnavailableError(
            "no Smogon month data in mirror "
            "(expected folders under raw/stats/YYYY-MM)"
        )
    return Month(id=months[-1])


def resolve_month(mirror: Path, spec) -> Month:
    """Turn MOST_RECENT / an explicit month string / a Month into a Month
    whose data is actually present in the mirror."""
    if spec is MOST_RECENT or spec is None:
        return discover_latest_month(mirror)
    if isinstance(spec, Month):
        month = spec
    elif isinstance(spec, str):
        if not MONTH_RE.match(spec):
            raise ValueError(f"invalid Smogon month {spec!r} (want YYYY-MM)")
        month = Month(id=spec)
    else:
        raise TypeError(f"cannot resolve Smogon month from {spec!r}")
    if not month_dir(mirror, month.id).is_dir():
        raise PeriodUnavailableError(f"Smogon month {month.id} is not in the mirror")
    return month


def _total_battles(path: Path) -> int | None:
    """The leading 'Total battles:' figure of a usage file (no full parse)."""
    try:
        # Handle both .txt and .txt.gz
        opener = gzip.open if path.suffix == ".gz" else open
        mode = "rt" if path.suffix == ".gz" else "r"
        
        with opener(path, mode, encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                match = BATTLES_RE.match(raw.strip())
                if match:
                    return _int_or_none(match.group(1))
    except (OSError, EOFError):
        return None
    return None


def _format_kind(fmt: str) -> str:
    """'doubles' when the site's format name says so, else 'singles'."""
    return "doubles" if "doubles" in fmt.lower() or "vgc" in fmt.lower() else "singles"


def format_standings(mirror: Path, month: Month) -> list[dict]:
    """Every <format>-0 table of the month, most played first.

    Each entry: {"format", "battles", "kind"} with kind "doubles"/"singles"
    per _format_kind. This is the only place the OTHER formats of the
    month survive: the snapshot itself stores none of their files.
    """
    base = month_dir(mirror, month.id)
    if not base.is_dir():
        raise PeriodUnavailableError(f"Smogon month {month.id} is not in the mirror")
    standings = []
    for entry in sorted(base.iterdir()):
        if not entry.is_file():
            continue
        match = USAGE_FILE_RE.match(entry.name)
        if not match or match.group(2) != "0":
            continue
        battles = _total_battles(entry)
        if battles is not None:
            standings.append(
                {
                    "format": match.group(1),
                    "battles": battles,
                    "kind": _format_kind(match.group(1)),
                }
            )
    standings.sort(key=lambda s: s["battles"], reverse=True)
    return standings


def most_played_format(mirror: Path, month: Month) -> str:
    """The month's most-played format: #1 in format_standings()."""
    standings = format_standings(mirror, month)
    if not standings:
        raise PeriodUnavailableError(
            f"no usable usage tables in month {month.id} (want <format>-0.txt)"
        )
    return standings[0]["format"]


def ladder_files_for(mirror: Path, month: Month, fmt: str) -> list[tuple[str, str, Path]]:
    """(format, elo, path) for every <fmt>-<elo>.txt the month publishes,
    i.e. all ladder divisions of that format present in the mirror."""
    base = month_dir(mirror, month.id)
    if not base.is_dir():
        raise PeriodUnavailableError(f"Smogon month {month.id} is not in the mirror")
    found = []
    seen: set[str] = set()
    for entry in sorted(base.iterdir()):
        if not entry.is_file():
            continue
        match = USAGE_FILE_RE.match(entry.name)
        if match and match.group(1) == fmt and match.group(2) not in seen:
            found.append((match.group(1), match.group(2), entry))
            seen.add(match.group(2))
    return found


def moveset_files_for(mirror: Path, month: Month, fmt: str) -> list[tuple[str, str, Path]]:
    """(format, elo, path) for every moveset/<fmt>-<elo>.txt the month publishes."""
    base = month_dir(mirror, month.id) / "moveset"
    if not base.is_dir():
        return []
    found = []
    for entry in sorted(base.iterdir()):
        if not entry.is_file():
            continue
        match = USAGE_FILE_RE.match(entry.name)
        if match and match.group(1) == fmt:
            found.append((match.group(1), match.group(2), entry))
    return found


def snapshot_month(store: SnapshotStore, mirror: Path, month: Month) -> SnapshotRecord:
    """Copy the month's most-played format across every division it has.

    Files from all OTHER formats of the same month are deliberately not
    snapshotted; run() wipes the period directory first, so the cache
    holds exactly this narrowed snapshot.
    """
    fmt = most_played_format(mirror, month)
    files = []
    for _, _, src in ladder_files_for(mirror, month, fmt):
        dst = store.period_dir("smogon", month.id) / src.name
        rel = f"smogon/{month.id}/{src.name}"
        files.append(copy_with_checksum(src, dst, rel_path=rel))
    if not files:
        raise PeriodUnavailableError(
            f"no <format>-<elo>.txt files for format {fmt} in month {month.id}"
        )
    record = SnapshotRecord(
        source="smogon",
        period_id=month.id,
        fetched_at=utc_now(),
        files=tuple(files),
    )
    store.put(record)
    return record

def _int_or_none(value: str) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _pct_or_none(value: str) -> float | None:
    try:
        return float(value.rstrip("%"))
    except (TypeError, ValueError):
        return None


def parse_usage_text(text: str, *, period: str, fmt: str, elo: str) -> dict:
    """One usage table -> a plain dict with rows of rank/pokemon/percentages.

    Real files vary in width (some classic generations publish fewer
    columns), so every trailing column may be absent.
    """
    total_battles = None
    header = None
    rows = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        battles = BATTLES_RE.match(line)
        if battles:
            total_battles = _int_or_none(battles.group(1))
            continue
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        while cells and cells[-1] == "":
            cells.pop()
        if len(cells) < 2:
            continue
        if header is None:
            if cells[0].lower() == "rank" and cells[1].lower() == "pokemon":
                header = cells
            continue
        if "----" in cells[0] or not cells[0].isdigit():
            continue
        rows.append(
            {
                "rank": _int_or_none(cells[0]),
                "pokemon": cells[1],
                "usage_pct": _pct_or_none(cells[2]) if len(cells) > 2 else None,
                "raw": _int_or_none(cells[3]) if len(cells) > 3 else None,
                "raw_pct": _pct_or_none(cells[4]) if len(cells) > 4 else None,
                "real": _int_or_none(cells[5]) if len(cells) > 5 else None,
                "real_pct": _pct_or_none(cells[6]) if len(cells) > 6 else None,
            }
        )
    if header is None:
        raise ValueError(f"no usage table found in {fmt}-{elo}.txt")
    return {
        "source": "smogon",
        "period": period,
        "format": fmt,
        "elo": elo,
        "total_battles": total_battles,
        "rows": rows,
    }


def _cached_usage_elos(period_dir: Path, fmt: str) -> list[tuple[str, Path]]:
    """(elo, path) for every <fmt>-<elo>.txt(.gz) usage ladder copied into
    this period's cache directory (see snapshot_month_for_formats).

    The usage file is the authoritative per-format source -- total battles,
    ranks, usage percentages and raw counts all come from it -- so this,
    not the moveset listing, drives what gets captured."""
    found = []
    seen: set[str] = set()
    for entry in sorted(period_dir.iterdir()):
        if not entry.is_file():
            continue
        match = USAGE_FILE_RE.match(entry.name)
        if match and match.group(1) == fmt and match.group(2) not in seen:
            found.append((match.group(2), entry))
            seen.add(match.group(2))
    return found


def _read_text_or_gz(path: Path) -> str:
    opener, mode = (gzip.open, "rt") if path.suffix == ".gz" else (open, "r")
    with opener(path, mode, encoding="utf-8", errors="replace") as f:
        return f.read()


def _capture_format_elo(conn, period_id: str, format_name: str, elo: str, period_dir: Path) -> bool:
    """Parse ONE format+elo's USAGE ladder straight into stats.db and mark
    it captured in capture_log.

    The usage file (the same <format>-<elo>.txt whose 'Total battles:' line
    drives the format's battle counts) is authoritative for ranks, usage
    percentages and raw counts. The parallel moveset file, when present,
    adds each Pokemon's moves/abilities/items/etc. -- so the single
    stats_{format}_elo{elo} table ends up one row per Pokemon holding the
    correct usage numbers AND the moveset detail.

    All-or-nothing: nothing is written unless the whole usage file is read
    successfully. Returns False (writing nothing) if the usage file isn't in
    the cache yet -- that just means the crawl hasn't reached it, not an
    error, so the caller should leave this format+elo for a future run.
    """
    from fourslice.extstats.moveset import iter_moveset_blocks

    usage_file = period_dir / f"{format_name}-{elo}.txt"
    if not usage_file.is_file():
        usage_file = period_dir / f"{format_name}-{elo}.txt.gz"
    if not usage_file.is_file():
        return False

    try:
        usage = parse_usage_text(
            _read_text_or_gz(usage_file), period=period_id, fmt=format_name, elo=elo
        )
    except Exception as exc:
        # Unreadable (truncated download, HTML error page, a format the
        # parser doesn't recognize yet) -- leave it uncaptured so it's
        # retried rather than silently accepted as "done" with nothing.
        log.warning(
            "usage file for %s-%s unreadable (%s) -- leaving uncaptured for retry: %s",
            format_name, elo, type(exc).__name__, usage_file,
        )
        return False

    if not usage["rows"]:
        log.warning(
            "usage file for %s-%s parsed to zero Pokemon rows -- "
            "leaving uncaptured for retry: %s", format_name, elo, usage_file,
        )
        return False

    # The moveset file (if present) supplies each Pokemon's detailed
    # columns, keyed by the same display name the usage file uses.
    moveset_file = period_dir / "moveset" / f"{format_name}-{elo}.txt"
    if not moveset_file.is_file():
        moveset_file = period_dir / "moveset" / f"{format_name}-{elo}.txt.gz"
    by_name: dict[str, dict] = {}
    if moveset_file.is_file():
        opener, mode = (gzip.open, "rt") if moveset_file.suffix == ".gz" else (open, "r")
        with opener(moveset_file, mode, encoding="utf-8", errors="replace") as mf:
            for block in iter_moveset_blocks(mf):
                by_name.setdefault(block["pokemon"], block)

    table_name = f"stats_{format_name}_elo{elo}"
    conn.execute(f"CREATE TABLE IF NOT EXISTS {table_name} ({statsdb.TABLE_SCHEMA})")
    conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{table_name}_pokemon ON {table_name}(pokemon)")
    conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{table_name}_period ON {table_name}(period_id, format, elo)")

    for entry in usage["rows"]:
        block = by_name.get(entry["pokemon"]) or {}
        statsdb.insert_moveset(
            conn,
            {
                "period_id": period_id,
                "format": format_name,
                "elo": elo,
                "pokemon": entry["pokemon"],
                "file_pos": block.get("file_pos"),
                "usage_pct": entry.get("usage_pct"),
                "raw_count": entry.get("raw"),
                "avg_weight": block.get("avg_weight"),
                "viability_ceiling": block.get("viability_ceiling"),
                "abilities": block.get("abilities"),
                "items": block.get("items"),
                "spreads": block.get("spreads"),
                "moves": block.get("moves"),
                "tera_types": block.get("tera_types"),
                "teammates": block.get("teammates"),
                "checks_counters": block.get("checks_counters"),
                "stored_at": utc_now(),
            },
            table_name=table_name,
        )
    statsdb.mark_captured(conn, period_id, format_name, elo, source="usage")
    conn.commit()
    return True



def _cleanup_raw_txt_files(
    period_dir: Path,
    mirror: Path | None = None,
    period_id: str | None = None,
    format_names: list[str] | None = None,
) -> None:
    """Delete raw .txt/.txt.gz files (usage & moveset) from the CACHE's
    period_dir once their data has been captured into stats.db.

    Only the cache is cleaned. The raw mirror is deliberately never
    touched, even when a *mirror* is passed in: manifest.json treats
    mirror files as durable -- a later resume crawl skips re-fetching
    them, and format_standings() still needs the month's <format>-0
    ladders present on disk. Deleting them here is what let an emptied
    stats.db combine with a stale manifest into a "no files for formats
    []" failure later.
    """
    base_dir = period_dir
    if not base_dir.exists():
        return

    for entry in list(base_dir.iterdir()):
        if entry.is_file() and (entry.name.endswith(".txt") or entry.name.endswith(".txt.gz")):
            if format_names is None:
                try:
                    entry.unlink()
                except OSError:
                    pass
            else:
                match = USAGE_FILE_RE.match(entry.name)
                if match and match.group(1) in format_names:
                    try:
                        entry.unlink()
                    except OSError:
                        pass

    moveset_dir = base_dir / "moveset"
    if moveset_dir.is_dir():
        for entry in list(moveset_dir.iterdir()):
            if entry.is_file() and (entry.name.endswith(".txt") or entry.name.endswith(".txt.gz")):
                if format_names is None:
                    try:
                        entry.unlink()
                    except OSError:
                        pass
                else:
                    match = USAGE_FILE_RE.match(entry.name)
                    if match and match.group(1) in format_names:
                        try:
                            entry.unlink()
                        except OSError:
                            pass
        try:
            if not any(moveset_dir.iterdir()):
                moveset_dir.rmdir()
        except OSError:
            pass


def parse_and_cache(
    store: SnapshotStore,
    record: SnapshotRecord,
    standings: list[dict],
    mirror: Path | None = None,
) -> SnapshotRecord:
    """Parse every snapshotted format straight into stats.db and store the
    month's format standings there too -- stats.db is the only derived
    store, nothing is written to parsed/*.json anymore. Raw .txt files are
    cleaned up once captured.
    """
    period_dir = store.period_dir(record.source, record.period_id)
    format_names = sorted({
        match.group(1)
        for f in record.files
        if (match := USAGE_FILE_RE.match(Path(f.rel_path).name))
    })

    conn = statsdb.init_db(config.get_stats_db_path())
    captured_formats: set[str] = set()
    try:
        statsdb.save_format_standings(conn, record.period_id, standings)
        for fmt in format_names:
            for elo, _path in _cached_usage_elos(period_dir, fmt):
                if _capture_format_elo(conn, record.period_id, fmt, elo, period_dir):
                    captured_formats.add(fmt)
    finally:
        conn.close()

    _cleanup_raw_txt_files(
        period_dir,
        mirror=mirror,
        period_id=record.period_id,
        format_names=list(captured_formats) or format_names,
    )

    remaining_files = tuple(
        f for f in record.files
        if (store.root / f.rel_path).is_file()
    )

    return SnapshotRecord(
        record.source, record.period_id, record.fetched_at, remaining_files, ()
    )


def _pick_initial_formats(standings: list[dict], n: int) -> list[str]:
    """Pick top n formats: top Singles, then top Doubles, etc."""
    singles = [s["format"] for s in standings if s["kind"] == "singles"]
    doubles = [s["format"] for s in standings if s["kind"] == "doubles"]
    
    picked = []
    if singles:
        picked.append(singles.pop(0))
    if doubles:
        picked.append(doubles.pop(0))
    
    # Fill remaining from singles then doubles if needed
    while len(picked) < n:
        if singles:
            picked.append(singles.pop(0))
        elif doubles:
            picked.append(doubles.pop(0))
        else:
            break
    return picked


def snapshot_month_for_formats(
    store: SnapshotStore, mirror: Path, month: Month, format_names: list[str]
) -> SnapshotRecord:
    """Copy all ladder files for multiple formats to the cache."""
    period_dir = store.period_dir("smogon", month.id)
    period_dir.mkdir(parents=True, exist_ok=True)

    files = []
    for fmt in format_names:
        # Copy usage files
        for _, _, src in ladder_files_for(mirror, month, fmt):
            dst = period_dir / src.name
            rel = f"smogon/{month.id}/{src.name}"
            files.append(copy_with_checksum(src, dst, rel_path=rel))
        # Copy moveset files
        moveset_dir = period_dir / "moveset"
        moveset_dir.mkdir(exist_ok=True)
        for _, _, src in moveset_files_for(mirror, month, fmt):
            dst = moveset_dir / src.name
            rel = f"smogon/{month.id}/moveset/{src.name}"
            files.append(copy_with_checksum(src, dst, rel_path=rel))
            
    if not files:
        raise PeriodUnavailableError(f"no files for formats {format_names} in month {month.id}")
        
    return SnapshotRecord(
        source="smogon",
        period_id=month.id,
        fetched_at=utc_now(),
        files=tuple(files),
    )


def run(
    mirror: Path,
    store: SnapshotStore,
    spec=MOST_RECENT,
    *,
    initial_formats: int = 2,
) -> SnapshotRecord:
    """Resolve, snapshot, parse and cache the requested Smogon month.

    The month's previous snapshot (even the same month, from an earlier
    broader-scope run) is wiped first, so the cache ends up holding only
    the chosen initial formats (defaulting to top Singles + top Doubles)
    plus formats.json with the full ordering.

    To snapshot ALL formats instead, pass initial_formats=ALL_FORMATS
    (or any value <= 0).
    """
    month = resolve_month(mirror, spec)
    standings = format_standings(mirror, month)
    
    # Pick which formats to snapshot: top-N if initial_formats > 0,
    # otherwise ALL formats the month publishes.
    if initial_formats and initial_formats > 0:
        to_cache = _pick_initial_formats(standings, initial_formats)
    else:
        to_cache = [s["format"] for s in standings]
    
    store.remove("smogon", period_id=month.id)
    record = snapshot_month_for_formats(store, mirror, month, to_cache)
    record = parse_and_cache(store, record, standings, mirror=mirror)
    store.put(record)
    return store.latest("smogon")


def run_all_formats(
    mirror: Path,
    store: SnapshotStore,
    spec=MOST_RECENT,
) -> SnapshotRecord:
    """Snapshot + parse + cache every format for the given Smogon month
    that isn't already fully captured.

    "Already captured" is answered by stats.db's capture_log -- not by
    anything in the snapshot cache. For each format, every elo the mirror
    currently publishes a USAGE ladder for must already be captured from
    that usage file (source="usage", see capture_log). A legacy
    moveset-source capture (source NULL or "moveset") is stale and is
    re-done, since it baked the wrong usage percentages.

    A stale format whose usage ladders are GONE from the mirror (an old
    crawl wrote only moveset files) cannot be repaired from what's on
    disk, and re-trying every run would re-copy the same moveset files
    forever without ever capturing. Such elos are marked "unavailable" so
    the state is explicit and the retry stops; they are re-captured
    automatically if the ladder ever appears again (the source check
    still fails for them). A failure on one format is logged and skipped
    so it can't block the rest of the backfill.

    Use this after run() to backfill the remaining formats.
    """
    month = resolve_month(mirror, spec)
    standings = format_standings(mirror, month)

    conn = statsdb.init_db(config.get_stats_db_path())
    try:
        # Formats come from the mirror's standings, plus any format that has
        # capture_log rows for this month -- a format whose ladders have all
        # vanished from the mirror drops out of standings, and it's exactly
        # those stale rows we must still park rather than leave silently.
        formats = [s["format"] for s in standings]
        known = set(formats)
        for (fmt,) in conn.execute(
            "SELECT DISTINCT format FROM capture_log WHERE period_id = ?", (month.id,)
        ):
            if fmt not in known:
                formats.append(fmt)
                known.add(fmt)

        for fmt in formats:
            available = {elo for _, elo, _ in ladder_files_for(mirror, month, fmt)}
            captured_sources = statsdb.get_captured_sources(conn, month.id, fmt)
            usage_captured = {e for e, s in captured_sources.items() if s == "usage"}
            if not available:
                # Nothing in the mirror for this format. If a stale capture
                # still exists it can never be re-done from what's here, so
                # park it instead of failing identically every run. Already
                # parked ("unavailable") or fresh ("usage") rows are left
                # alone -- parked formats stay quiet until their ladder
                # reappears, at which point the normal path re-captures them.
                stale = {e for e, s in captured_sources.items() if s not in ("usage", "unavailable")}
                if stale:
                    log.error(
                        "format %s (%s) has stale captures %s but no usage "
                        "ladders in the mirror -- marking unavailable; re-fetch "
                        "the usage ladder before it can be re-captured",
                        fmt, month.id, sorted(stale),
                    )
                    statsdb.mark_capture_unavailable(
                        conn, month.id, fmt, *sorted(stale)
                    )
                continue
            if available <= usage_captured:
                continue  # already fully captured from the usage source
            try:
                run_single_format(mirror, store, month, fmt, standings)
            except Exception:
                log.exception("run_all_formats: failed on format %s (%s)", fmt, month.id)
                continue
    finally:
        conn.close()

    return store.latest("smogon")


def run_single_format(
    mirror: Path,
    store: SnapshotStore,
    month: Month,
    format_name: str,
    standings: list[dict],
) -> SnapshotRecord:
    """
    Snapshot + parse + cache ONLY one specific format for the given month.
    Does NOT wipe other formats from cache if they exist -- new raw files
    are merged into the existing snapshot record so the cache accumulates.
    Capture state itself lives in stats.db's capture_log, not here.
    """
    record = snapshot_month_for_format(store, mirror, month, format_name)
    record = parse_and_cache_format(store, record, standings, format_name, mirror=mirror)

    # Merge with the existing snapshot so previously-cached formats' raw
    # files remain accounted for.
    existing = store.latest("smogon")
    if existing and existing.period_id == month.id:
        merged_files = {f.rel_path: f for f in existing.files if (store.root / f.rel_path).is_file()}
        merged_files.update({f.rel_path: f for f in record.files if (store.root / f.rel_path).is_file()})
        merged = SnapshotRecord(
            "smogon",
            month.id,
            existing.fetched_at,
            tuple(merged_files.values()),
            (),
        )
        store.put(merged)
    else:
        store.put(record)

    return store.latest("smogon")


def snapshot_month_for_format(
    store: SnapshotStore,
    mirror: Path,
    month: Month,
    format_name: str,
) -> SnapshotRecord:
    """
    Copy usage AND moveset files for the specified format to the cache.
    """
    period_dir = store.period_dir("smogon", month.id)
    period_dir.mkdir(parents=True, exist_ok=True)

    # Find all ladder files for this format
    format_files = ladder_files_for(mirror, month, format_name)
    if not format_files:
        conn = statsdb.init_db(config.get_stats_db_path())
        try:
            already_captured = bool(statsdb.get_captured_elos(conn, month.id, format_name))
        finally:
            conn.close()
        if already_captured:
            # Nothing new to copy, but stats.db already has this format --
            # not an error, just nothing to do this call.
            return SnapshotRecord("smogon", month.id, utc_now(), (), ())
        raise PeriodUnavailableError(
            f"no usage files found for format {format_name!r} in month {month.id}"
        )

    copied: list[SnapshotFile] = []
    for _, _, src in format_files:
        rel = f"smogon/{month.id}/{src.name}"
        copied.append(copy_with_checksum(src, period_dir / src.name, rel_path=rel))

    # Also copy moveset files for this format
    moveset_files = moveset_files_for(mirror, month, format_name)
    for _, _, src in moveset_files:
        rel = f"smogon/{month.id}/moveset/{src.name}"
        dst = period_dir / "moveset" / src.name
        copied.append(copy_with_checksum(src, dst, rel_path=rel))

    record = SnapshotRecord(
        "smogon", month.id, utc_now(), tuple(copied), ()
    )
    return record


def parse_and_cache_format(
    store: SnapshotStore,
    record: SnapshotRecord,
    standings: list[dict],
    format_name: str,
    mirror: Path | None = None,
) -> SnapshotRecord:
    """
    Parse only format_name's usage ladders straight into stats.db (one
    file per elo is the only source needed -- see _capture_format_elo),
    and keep stats.db's format standings table up to date. Raw .txt files
    for format_name are cleaned up once captured.
    """
    if not record.files:
        return record

    period_dir = store.period_dir("smogon", record.period_id)

    conn = statsdb.init_db(config.get_stats_db_path())
    captured = False
    try:
        statsdb.save_format_standings(conn, record.period_id, standings)
        for elo, _path in _cached_usage_elos(period_dir, format_name):
            if _capture_format_elo(conn, record.period_id, format_name, elo, period_dir):
                captured = True
    finally:
        conn.close()

    if captured:
        _cleanup_raw_txt_files(period_dir, mirror=mirror, period_id=record.period_id, format_names=[format_name])

    remaining_files = tuple(
        f for f in record.files
        if (store.root / f.rel_path).is_file()
    )

    return SnapshotRecord(
        record.source, record.period_id, record.fetched_at, remaining_files, ()
    )


def load_latest_formats(cache_root: Path | None = None) -> dict | None:
    """The latest month's format standings, straight from stats.db.

    cache_root is accepted (and ignored) for compatibility with existing
    callers -- standings now live in stats.db, not a cache-root JSON file.
    """
    conn = statsdb.init_db(config.get_stats_db_path())
    try:
        period_id = statsdb.get_latest_smogon_period(conn)
        if not period_id:
            return None
        formats = statsdb.get_format_standings(conn, period_id)
        if not formats:
            return None
        return {
            "source": "smogon",
            "period": period_id,
            "most_played": formats[0]["format"],
            "formats": formats,
        }
    finally:
        conn.close()


def load_format_usage(cache_root: Path | None, period_id: str, format_name: str, elo: str) -> dict | None:
    """The stored Pokemon rows for one format+elo, reshaped to look like
    the old parsed usage table (rank/pokemon/usage_pct/raw), straight
    from stats.db. cache_root is accepted (and ignored) for compatibility.

    period_id names the month the CALLER believes is current (e.g. the
    latest month in smogon_formats standings), but the rows returned are
    the most RECENT captured month for this format+elo, whichever that
    is. That keeps the sidebar on real, captured data instead of a month
    whose standalone says "current" but whose stats_* rows are stale or
    missing -- the exact failure mode where a format's capture lags the
    standings and the old (pre-fix) percentages were still on screen.
    The resolved period is reported back in the result's "period" key.
    """
    conn = statsdb.init_db(config.get_stats_db_path())
    try:
        actual = statsdb.get_latest_period_for_format(conn, format_name, elo)
        if not actual:
            return None
        rows = statsdb.get_format_rows(conn, actual, format_name, elo)
        if not rows:
            return None
        return {
            "source": "smogon",
            "period": actual,
            "format": format_name,
            "elo": elo,
            "total_battles": None,
            "rows": [
                {
                    "rank": i + 1,
                    "pokemon": r["pokemon"],
                    "usage_pct": r["usage_pct"],
                    "raw": r["raw_count"],
                    "raw_pct": None,
                    "real": None,
                    "real_pct": None,
                }
                for i, r in enumerate(rows)
            ],
        }
    finally:
        conn.close()


def get_format_groups(formats_data: dict) -> dict[str, list[dict]]:
    """Split formats into Singles and Doubles groups, sorted by battles desc."""
    singles = []
    doubles = []
    for fmt in formats_data.get("formats", []):
        item = {
            "name": fmt["format"],
            "key": fmt["format"],
            "battles": fmt["battles"],
            "kind": fmt["kind"],
            "period": formats_data["period"],
        }
        if fmt["kind"] == "singles":
            singles.append(item)
        else:
            doubles.append(item)
    # The formats list is already sorted by battles descending in format_standings,
    # so these lists maintain that order.
    return {"Singles": singles, "Doubles": doubles}