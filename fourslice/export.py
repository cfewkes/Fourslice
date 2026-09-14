"""
fourslice/export.py

Exports everything Fourslice knows to CSV files for Power BI (or any
other external BI tool).

 * The five raw tables mirror the database schema column-for-column,
   so Power BI's "Get Data → Text/CSV" flow maps directly to the
   schema documented in storage.py without any column-rename step.
 * The eight stats_*.csv files are the app's pre-computed stats,
   denormalized for Power BI. They are produced by the SAME
   fourslice/stats functions the GUI charts call (single source of
   truth — no duplicated stat math), dimensioned by regulation /
   result / perspective. Regulation is never null and every game is
   exactly W or L, so W + L rows sum to the overall totals: there are
   no "(all)" sentinel rows to double-count on a no-selection slicer.

Usage from the GUI: the Stats tab's "Export for Power BI" button calls
export_all_csvs(conn, folder) → writes 13 CSV files into the chosen
folder. Usage from a script or REPL:

    from fourslice.storage import init_db
    from fourslice.config import get_db_path
    from fourslice.export import export_all_csvs

    conn = init_db(str(get_db_path()))
    written = export_all_csvs(conn, "C:/MyExports")
    print(written)  # list of 13 Path objects

Raw data tables (star-schema model):

    games.csv          -- fact table: one row per game
    events.csv         -- detail fact: one row per turn/move/faint event
    teams.csv          -- dimension: auto-detected team rosters
    team_pokemon.csv   -- dimension: 6 rows per team (species + item + moves)
    opponent_teams.csv -- dimension: opponent roster per game

Pre-computed stat tables (drop-in — no DAX measures required):

    stats_turns_on_field.csv        -- turns on field, mine/opponent split
    stats_co_occurrence.csv         -- duo co-occurrence, mine/opponent split
    stats_ko_credit.csv             -- KO credit, mine/opponent split
    stats_attendance.csv            -- opponent bring rates
    stats_move_usage.csv            -- per-team long-form move usage
    stats_opponent_records.csv      -- all-time matchup records per mon
    stats_leads.csv                 -- lead records (singles + doubles)
    stats_brought_records.csv       -- per-team brought-record W/L

See powerbi/README.md for the full column reference and setup.
"""

import logging
from pathlib import Path

import pandas as pd

from .stats.turns_on_field import turns_on_field
from .stats.co_occurrence import co_occurrence
from .stats.ko_credit import ko_credit
from .stats.attendance import attendance
from .stats.move_usage import move_usage
from .stats.matchups import opponent_records, lead_records
from .stats.brought_record import brought_records

log = logging.getLogger(__name__)

# The tables to export and the SQL that produces each one. Ordering
# within each query matches the CREATE TABLE column order from
# storage.py's SCHEMA so the CSVs read naturally in a spreadsheet.
_EXPORT_TABLES = {
    "games": """
        SELECT game_id, replay_url, p1_name, p2_name, winner,
               my_side, result, regulation, battle_size,
               team_id
        FROM games
        ORDER BY game_id
    """,
    "events": """
        SELECT event_id, game_id, turn, mon, event_type,
               move_name, p1a, p1b, p2a, p2b,
               ko_credit_mon, ko_credit_side
        FROM events
        ORDER BY event_id
    """,
    "teams": """
        SELECT team_id, nickname, roster_key, created_at,
               naming_pokemon, naming_regulation
        FROM teams
        ORDER BY team_id
    """,
    "team_pokemon": """
        SELECT team_id, slot_order, species, item, moves
        FROM team_pokemon
        ORDER BY team_id, slot_order
    """,
    "opponent_teams": """
        SELECT game_id, slot_order, species, item, moves
        FROM opponent_teams
        ORDER BY game_id, slot_order
    """,
}

# Total number of CSVs export_all_csvs writes (the 5 raw data tables
# above plus the 8 pre-computed stat tables below). The GUI's export
# worker uses this for its progress dialog range.
EXPORT_FILE_COUNT = len(_EXPORT_TABLES) + 8


# ---------------------------------------------------------------------------
# Pre-computed stat CSV helpers
#
# Every stat CSV merges the app's shared stats functions run once per
# dimension combination (regulation x result x perspective where
# supported). The stat functions themselves are the single source of
# truth; this module only tags each run with the slice it belongs to
# and flattens the result into one CSV per stat. Per-regulation rows
# cover every game (regulation is never null) and the W/L rows
# partition each regulation, so totals fall out of plain Power BI
# summing -- no "(all)" sentinel rows that double-count when a slicer
# has no selection.
# ---------------------------------------------------------------------------


def _get_regulations(conn) -> list:
    """Distinct non-null regulation values from games, ascending."""
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT regulation FROM games "
        "WHERE regulation IS NOT NULL ORDER BY regulation"
    ).fetchall()]


def _get_teams(conn) -> list:
    """All teams as (team_id, nickname), by team_id."""
    return conn.execute(
        "SELECT team_id, nickname FROM teams ORDER BY team_id"
    ).fetchall()


def _write_csv(df, folder, name, written, progress_cb=None):
    """Write *df* to *folder*/*name*.csv and record the Path."""
    path = folder / f"{name}.csv"
    df.to_csv(path, index=False, encoding="utf-8")
    written.append(path)
    log.info("Exported %s (%d rows) -> %s", name, len(df), path)
    if progress_cb is not None:
        progress_cb(name, len(df))


def _expand_side(conn, data_fn, regulations, **kw) -> pd.DataFrame:
    """
    Side-capable stat across (regulation x result x perspective).
    Returns one flat DataFrame with regulation / result / perspective
    columns prepended. `data_fn` is the shared stats function
    (turns_on_field, co_occurrence, ko_credit) called with the same
    kwargs the GUI would use for a fully unfiltered view.
    """
    frames = []
    for reg in regulations:
        for result in ("W", "L"):
            for perspective in ("mine", "opponent"):
                df = data_fn(conn, regulation=reg, result=result,
                             my_side=perspective, **kw).copy()
                df.insert(0, "regulation", reg)
                df.insert(1, "result", result)
                df.insert(2, "perspective", perspective)
                frames.append(df)
    if not frames:
        # Empty DB: regulations is empty, so the loops above ran zero
        # times. Emit a header-only frame with the exact export schema.
        sample = data_fn(conn, regulation=None, result=None,
                         my_side="mine", **kw)
        cols = ["regulation", "result", "perspective"] + list(sample.columns)
        return pd.DataFrame(columns=cols)
    return pd.concat(frames, ignore_index=True)


def _expand_attendance(conn, regulations) -> pd.DataFrame:
    """Attendance across (regulation x result). No perspective column:
    it is inherently the opponent's side. Only includes opponent
    Pokémon that were actually brought to at least one game
    (bring_games > 0)."""
    frames = []
    for reg in regulations:
        for result in ("W", "L"):
            df = attendance(conn, regulation=reg, result=result).copy()
            # Filter out opponent Pokémon never brought (bring_games == 0)
            df = df[df["bring_games"] > 0]
            if not df.empty:
                df.insert(0, "regulation", reg)
                df.insert(1, "result", result)
                frames.append(df)
    if not frames:
        sample = attendance(conn, regulation=None, result=None)
        cols = ["regulation", "result"] + list(sample.columns)
        return pd.DataFrame(columns=cols)
    return pd.concat(frames, ignore_index=True)


def _expand_move_usage(conn, regulations, teams) -> pd.DataFrame:
    """
    Per-team move usage in one long-format table across
    (team x regulation x result x side). move_usage returns a dict of
    per-species DataFrames. Rows with zero move usage (use_count == 0)
    are omitted since they represent Pokémon that never actually used
    a move in that slice.
    """
    cols = ["team_id", "team_nickname", "regulation", "result", "side",
            "species", "slot_order", "move_name", "use_count", "pct_of_uses"]
    rows = []
    for team_id, nickname in teams:
        roster = conn.execute(
            "SELECT slot_order, species FROM team_pokemon "
            "WHERE team_id = ? ORDER BY slot_order", (team_id,)
        ).fetchall()
        slot_map = {species: slot for slot, species in roster}
        for reg in regulations:
            for result in ("W", "L"):
                for side in ("mine", "opponent"):
                    usage = move_usage(
                        conn, regulation=reg, result=result,
                        my_side=side, team_id=team_id,
                    )
                    for species, sdf in usage.items():
                        # Skip N/A placeholder (mon never used any move in this slice)
                        if len(sdf) == 1 and sdf.iloc[0]["move_name"] == "N/A":
                            continue
                        # Filter out any zero-use rows
                        use_rows = sdf[sdf["use_count"] > 0].to_dict("records")
                        for rec in use_rows:
                            rows.append({
                                "team_id": team_id,
                                "team_nickname": nickname,
                                "regulation": reg,
                                "result": result,
                                "side": side,
                                "species": species,
                                "slot_order": slot_map.get(species, -1),
                                **rec,
                            })
    if not rows:
        return pd.DataFrame(columns=cols)
    return pd.DataFrame(rows, columns=cols)


def _expand_opponent_records(conn, regulations) -> pd.DataFrame:
    """All-time matchup records per opponent mon, across regulation.
    Rows already carry games/wins/losses/pct, so no result dimension."""
    frames = []
    for reg in regulations:
        df = opponent_records(conn, regulation=reg).copy()
        df.insert(0, "regulation", reg)
        frames.append(df)
    if not frames:
        cols = ["regulation"] + list(opponent_records(conn).columns)
        return pd.DataFrame(columns=cols)
    return pd.concat(frames, ignore_index=True)


def _expand_leads(conn, regulations) -> pd.DataFrame:
    """Lead records (singles + doubles buckets) across regulation, with
    a battle_size column instead of a dict-of-DataFrames."""
    frames = []
    for reg in regulations:
        for battle_size, df in lead_records(conn, regulation=reg).items():
            df = df.copy()
            df.insert(0, "regulation", reg)
            df.insert(1, "battle_size", battle_size)
            frames.append(df)
    if not frames:
        sample = next(iter(lead_records(conn).values()))
        cols = ["regulation", "battle_size"] + list(sample.columns)
        return pd.DataFrame(columns=cols)
    return pd.concat(frames, ignore_index=True)


def _expand_brought_records(conn, regulations, teams) -> pd.DataFrame:
    """Per-team brought records across regulation. Only includes Pokémon
    that were actually brought to at least one game (games > 0)."""
    cols = ["team_id", "team_nickname", "regulation", "mon", "games",
            "wins", "losses", "pct"]
    frames = []
    for team_id, nickname in teams:
        for reg in regulations:
            df = brought_records(conn, regulation=reg, team_id=team_id).copy()
            # Filter out Pokémon never brought (games == 0)
            df = df[df["games"] > 0]
            if not df.empty:
                df.insert(0, "team_id", team_id)
                df.insert(1, "team_nickname", nickname)
                df.insert(2, "regulation", reg)
                frames.append(df)
    if not frames:
        return pd.DataFrame(columns=cols)
    return pd.concat(frames, ignore_index=True)


def _export_stats_csvs(conn, folder: Path, progress_cb=None) -> list[Path]:
    """
    Write all 8 pre-computed stat CSVs and return their Paths.
    Called by export_all_csvs after the raw data tables.
    """
    written: list[Path] = []
    regulations = _get_regulations(conn)
    teams = _get_teams(conn)

    # Side-capable stats, split mine/opponent.
    _write_csv(_expand_side(conn, turns_on_field, regulations),
               folder, "stats_turns_on_field", written, progress_cb)
    _write_csv(_expand_side(conn, co_occurrence, regulations),
               folder, "stats_co_occurrence", written, progress_cb)
    _write_csv(_expand_side(conn, ko_credit, regulations),
               folder, "stats_ko_credit", written, progress_cb)

    # Non-side stats.
    _write_csv(_expand_attendance(conn, regulations),
               folder, "stats_attendance", written, progress_cb)
    _write_csv(_expand_move_usage(conn, regulations, teams),
               folder, "stats_move_usage", written, progress_cb)
    _write_csv(_expand_opponent_records(conn, regulations),
               folder, "stats_opponent_records", written, progress_cb)
    _write_csv(_expand_leads(conn, regulations),
               folder, "stats_leads", written, progress_cb)
    _write_csv(_expand_brought_records(conn, regulations, teams),
               folder, "stats_brought_records", written, progress_cb)

    return written


def export_all_csvs(conn, folder: str | Path, progress_cb=None) -> list[Path]:
    """
    Write every export CSV into *folder*, creating the folder if it
    doesn't exist. Returns the list of Paths written (5 raw data
    tables + 8 pre-computed stat tables = 13 files).

    *progress_cb*, if given, is called as progress_cb(name,
    row_count) immediately after each file is written -- the GUI's
    background worker uses it to drive a progress dialog and to stop
    between files.

    Raises on I/O errors (caller - the GUI - catches and shows a
    message box). Never raises on an empty database: every CSV is
    still written, just header-only (Power BI handles that
    gracefully).
    """
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    for name, query in _EXPORT_TABLES.items():
        df = pd.read_sql(query, conn)
        path = folder / f"{name}.csv"
        df.to_csv(path, index=False, encoding="utf-8")
        written.append(path)
        log.info("Exported %s (%d rows) -> %s", name, len(df), path)
        if progress_cb is not None:
            progress_cb(name, len(df))

    # Pre-computed stat CSVs from the shared stats functions.
    written += _export_stats_csvs(conn, folder, progress_cb)

    return written
