"""
fourslice/extstats/statsdb.py

Persistent SQLite query layer for external-stat data.

stats.db is the single source of truth for parsed Smogon data -- there
is no parallel JSON layer. Three kinds of tables live here:

    stats_<format>_elo<elo>   one row per Pokemon, written all-or-nothing
                               per (period, format, elo) straight from a
                               moveset text file (see extstats.smogon)
    capture_log                ledger of which (period, format, elo)
                               combos are fully captured -- this, not any
                               file on disk, is what "already done" means
    smogon_formats              the month's format standings (battles,
                               singles/doubles) -- replaces formats.json

The database is **persistent** across application launches -- once data
is ingested it does not need to be re-parsed. Schema evolution follows
fourslice.storage's conventions: CREATE TABLE IF NOT EXISTS plus an
idempotent _ensure_column migration, so init_db is safe to call on every
launch.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .models import SMOGON_ELOS, utc_now

# Base schema for all format-elo tables
TABLE_SCHEMA = """
    period_id          TEXT NOT NULL,
    format             TEXT NOT NULL,
    elo                TEXT NOT NULL,
    pokemon            TEXT NOT NULL,
    file_pos           INTEGER,          -- 1-based position in the source file
    usage_pct          REAL,             -- this Pokemon's usage %, straight from the usage ladder
    raw_count          INTEGER,
    avg_weight         REAL,
    viability_ceiling  INTEGER,
    abilities          TEXT,             -- JSON list of {name, pct}
    items              TEXT,
    spreads            TEXT,
    moves              TEXT,
    tera_types         TEXT,
    teammates          TEXT,
    checks_counters    TEXT,             -- JSON list of {name, score, winrate, moe, koed_pct, switched_pct}
    stored_at          TEXT NOT NULL,
    PRIMARY KEY (period_id, format, elo, pokemon)
"""

_MOVESET_COLUMNS = (
    "period_id", "format", "elo", "pokemon", "file_pos", "usage_pct", "raw_count",
    "avg_weight", "viability_ceiling", "abilities", "items", "spreads",
    "moves", "tera_types", "teammates", "checks_counters", "stored_at",
)
_JSON_COLUMNS = frozenset(
    {"abilities", "items", "spreads", "moves", "tera_types", "teammates", "checks_counters"}
)


def init_db(db_path: str | Path) -> sqlite3.Connection:
    """Open (creating + migrating) the stats DB. Safe to call every launch."""
    conn = sqlite3.connect(str(db_path))

    # Rename legacy moveset table if it exists (one-time migration)
    # Check explicitly whether the legacy table exists to avoid silently
    # swallowing real errors (e.g., target table already exists with data).
    legacy_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='moveset'"
    ).fetchone()
    if legacy_exists:
        target_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='stats_gen9ou_elo0'"
        ).fetchone()
        if not target_exists:
            conn.execute("ALTER TABLE moveset RENAME TO stats_gen9ou_elo0")
            conn.commit()
        else:
            # Both tables exist -- this shouldn't happen in normal operation.
            # Log a warning but don't silently drop data.
            import warnings
            warnings.warn(
                "Both legacy 'moveset' and 'stats_gen9ou_elo0' tables exist; "
                "legacy table not renamed to avoid data loss. Manual intervention required.",
                RuntimeWarning,
            )

    conn.execute(
        """CREATE TABLE IF NOT EXISTS capture_log (
            period_id  TEXT NOT NULL,
            format     TEXT NOT NULL,
            elo        TEXT NOT NULL,
            stored_at  TEXT NOT NULL,
            source     TEXT,
            PRIMARY KEY (period_id, format, elo)
        )"""
    )
    # Migrate pre-"source" capture_logs (which captured the moveset file and
    # baked wrong usage percentages into stats_* tables) so those old rows
    # are detected as stale below and re-captured from the usage file.
    _ensure_column(conn, "capture_log", "source", "TEXT")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS smogon_formats (
            period_id  TEXT NOT NULL,
            format     TEXT NOT NULL,
            battles    INTEGER,
            kind       TEXT,
            PRIMARY KEY (period_id, format)
        )"""
    )
    conn.commit()
    return conn


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, column_def: str) -> None:
    """Idempotent ALTER TABLE for future migrations (same as storage.py)."""
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_def}")
        conn.commit()


def insert_moveset(conn: sqlite3.Connection, row: dict, table_name: str = "stats_gen9ou_elo0") -> None:
    """Insert or replace one Pokemon's moveset row into the specified table."""
    values = []
    for column in _MOVESET_COLUMNS:
        value = row.get(column)
        if column in _JSON_COLUMNS and value is not None:
            value = json.dumps(value, ensure_ascii=False)
        values.append(value)
    placeholders = ", ".join("?" for _ in _MOVESET_COLUMNS)
    conn.execute(
        f"INSERT OR REPLACE INTO {table_name} ({', '.join(_MOVESET_COLUMNS)}) "
        f"VALUES ({placeholders})",
        tuple(values),
    )


def is_populated(conn: sqlite3.Connection) -> bool:
    """Return True if at least one ``stats_*`` table contains any rows.

    Used at startup to decide whether a synchronous bootstrap is needed
    before the main window loads.
    """
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'stats_%'"
    ).fetchall()
    for (table_name,) in tables:
        count = conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]
        if count > 0:
            return True
    return False


def get_moveset(conn: sqlite3.Connection, period_id: str, format_: str, elo: str, pokemon: str) -> dict | None:
    """Read one Pokemon's stored moveset row back (JSON columns decoded)."""
    table_name = f"stats_{format_}_elo{elo}"
    columns = ", ".join(_MOVESET_COLUMNS)
    try:
        row = conn.execute(
            f"SELECT {columns} FROM {table_name} "
            "WHERE period_id = ? AND format = ? AND elo = ? AND pokemon = ?",
            (period_id, format_, elo, pokemon),
        ).fetchone()
    except sqlite3.OperationalError:
        return None  # table doesn't exist yet

    if row is None:
        return None
    result = {}
    for column, value in zip(_MOVESET_COLUMNS, row):
        if column in _JSON_COLUMNS and value is not None:
            value = json.loads(value)
        result[column] = value
    return result


def get_format_elos(conn: sqlite3.Connection, format_: str) -> list[str]:
    """The ladder divisions that actually have a stats_{format_}_elo{elo}
    table in the database, in canonical order.

    Unlike get_captured_elos (which only knows what was written for the
    current capture period), this answers from the real tables, so a format
    keeps all of its historical divisions -- e.g. gen9ou is 0/1500/1695/1825
    even when the latest period only captured 0 and 1500. Returns the
    standard SMOGON_ELOS order first, then any non-standard divisions."""
    prefix = f"stats_{format_}_elo"
    # Escape the underscores so the pattern's _-s are literal (a bare like
    # would let "_" match any character and snag unrelated tables).
    pattern = prefix.replace("_", "\\_") + "%"
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE ? ESCAPE '\\'",
        (pattern,),
    ).fetchall()
    elos = {name[len(prefix):] for (name,) in rows}
    if not elos:
        return list(SMOGON_ELOS)
    canonical = [e for e in SMOGON_ELOS if e in elos]
    extra = sorted(e for e in elos if e not in SMOGON_ELOS)
    return canonical + extra


def get_format_rows(conn: sqlite3.Connection, period_id: str, format_: str, elo: str) -> list[dict]:
    """All Pokemon rows stored for one (period, format, elo), most-used first.
    Empty list if the table doesn't exist yet or has nothing for this period."""
    table_name = f"stats_{format_}_elo{elo}"
    try:
        rows = conn.execute(
            f"SELECT pokemon, usage_pct, raw_count FROM {table_name} "
            "WHERE period_id = ? ORDER BY usage_pct DESC",
            (period_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [{"pokemon": p, "usage_pct": u, "raw_count": r} for p, u, r in rows]


def get_latest_period_for_format(conn: sqlite3.Connection, format_: str, elo: str) -> str | None:
    """The most recent period_id that has stored Pokemon rows for one
    (format, elo), or None if the table doesn't exist or is empty.

    period_id is a YYYY-MM string, so lexicographic MAX is chronological MAX.
    This answers from the real stats table -- unlike get_latest_smogon_period
    (which answers from smogon_formats standings), it reflects what was
    actually CAPTURED for that format+elo, so the sidebar can always show
    data even when a format lags the month that just got standings."""
    table_name = f"stats_{format_}_elo{elo}"
    try:
        row = conn.execute(f"SELECT MAX(period_id) FROM {table_name}").fetchone()
    except sqlite3.OperationalError:
        return None
    return row[0] if row and row[0] else None


# ---------------------------------------------------------------------------
# Capture ledger -- replaces the old parsed/*.json manifest. stats.db is the
# only place "is format+elo X already captured" is answered from.
# ---------------------------------------------------------------------------

def mark_captured(
    conn: sqlite3.Connection,
    period_id: str,
    format_: str,
    elo: str,
    source: str | None = None,
) -> None:
    """Record that (period_id, format_, elo) has been fully written to its
    stats_* table. Call this only after every row has been inserted --
    capture is all-or-nothing per format+elo.

    source names the text file the rows were parsed from (e.g. "usage" or
    "moveset"). It lets a later code fix (one that changes how rows are
    derived, like switching percentages from the moveset file to the usage
    file) tell which captures are stale and need re-capturing."""
    conn.execute(
        "INSERT OR REPLACE INTO capture_log (period_id, format, elo, stored_at, source) "
        "VALUES (?, ?, ?, ?, ?)",
        (period_id, format_, elo, utc_now(), source),
    )


def get_captured_elos(conn: sqlite3.Connection, period_id: str, format_: str) -> set[str]:
    """Which ladder divisions of format_ are already fully captured for period_id."""
    rows = conn.execute(
        "SELECT elo FROM capture_log WHERE period_id = ? AND format = ?",
        (period_id, format_),
    ).fetchall()
    return {row[0] for row in rows}


def mark_capture_unavailable(
    conn: sqlite3.Connection,
    period_id: str,
    format_: str,
    *elos: str,
) -> None:
    """Flag (period_id, format_, elo) captures whose usage source is not
    recoverable from the mirror -- the usage ladder is gone and only the
    stale legacy rows survive. They stay in capture_log so the state is
    explicit and queryable, but they no longer count as pending re-capture
    work, so a missing ladder doesn't spin the updater every launch. If the
    ladder reappears they are re-captured normally (the pending check only
    matches source == 'usage'). With no elos, marks every row for the
    format."""
    if elos:
        conn.executemany(
            "UPDATE capture_log SET source = 'unavailable' "
            "WHERE period_id = ? AND format = ? AND elo = ?",
            [(period_id, format_, elo) for elo in elos],
        )
    else:
        conn.execute(
            "UPDATE capture_log SET source = 'unavailable' "
            "WHERE period_id = ? AND format = ?",
            (period_id, format_),
        )
    conn.commit()


def get_captured_sources(conn: sqlite3.Connection, period_id: str, format_: str) -> dict[str, str | None]:
    """{elo: source} for every captured (period_id, format_, elo) row.

    source is ``"usage"`` for fresh captures from the usage ladder file,
    ``"moveset"`` for the legacy path, or ``None`` for pre-source-schema
    rows that must be treated as stale."""
    rows = conn.execute(
        "SELECT elo, source FROM capture_log WHERE period_id = ? AND format = ?",
        (period_id, format_),
    ).fetchall()
    return {elo: source for elo, source in rows}


def is_captured(conn: sqlite3.Connection, period_id: str, format_: str, elo: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM capture_log WHERE period_id = ? AND format = ? AND elo = ?",
        (period_id, format_, elo),
    ).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# Format standings -- replaces formats.json
# ---------------------------------------------------------------------------

def save_format_standings(conn: sqlite3.Connection, period_id: str, standings: list[dict]) -> None:
    """Upsert the month's format standings (battles + singles/doubles kind)."""
    for entry in standings:
        conn.execute(
            "INSERT OR REPLACE INTO smogon_formats (period_id, format, battles, kind) VALUES (?, ?, ?, ?)",
            (period_id, entry["format"], entry.get("battles"), entry.get("kind")),
        )
    conn.commit()


def get_format_standings(conn: sqlite3.Connection, period_id: str) -> list[dict]:
    """The stored standings for period_id, most-played first. Empty list if none stored."""
    rows = conn.execute(
        "SELECT format, battles, kind FROM smogon_formats WHERE period_id = ? ORDER BY battles DESC",
        (period_id,),
    ).fetchall()
    return [{"format": f, "battles": b, "kind": k} for f, b, k in rows]


def get_latest_smogon_period(conn: sqlite3.Connection) -> str | None:
    """The most recent period_id with any stored standings, or None.
    period_id is a YYYY-MM string, so lexicographic MAX is chronological MAX."""
    row = conn.execute("SELECT MAX(period_id) FROM smogon_formats").fetchone()
    return row[0] if row and row[0] else None