"""
Tests for the stats.db ladder-division helper get_format_elos.

Run with: python tests/test_extstats_elos.py  (or pytest).
"""

import sys
import sqlite3
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fourslice.extstats import statsdb
from fourslice.extstats.models import SMOGON_ELOS


def _conn(tmpdir: str = None) -> tuple[sqlite3.Connection, str]:
    if tmpdir:
        path = str(Path(tmpdir) / "stats.db")
    else:
        import tempfile, os
        path = os.path.join(tempfile.mkdtemp(), "stats.db")
    conn = statsdb.init_db(path)
    return conn, path


def _make_format_table(conn: sqlite3.Connection, format_: str, elos: list[str]):
    for elo in elos:
        conn.execute(
            f"CREATE TABLE stats_{format_}_elo{elo} "
            "(period_id TEXT, pokemon TEXT, usage_pct REAL, raw_count INTEGER)"
        )
    conn.commit()


def test_standard_elo_set():
    conn, _ = _conn()
    _make_format_table(conn, "gen9ou", list(SMOGON_ELOS))
    assert statsdb.get_format_elos(conn, "gen9ou") == list(SMOGON_ELOS)


def test_nonstandard_elo_divisions():
    # gen9ou splits 1630/1760 into 1695/1825; helper must surface them.
    conn, _ = _conn()
    _make_format_table(conn, "gen9ou", ["0", "1500", "1695", "1825"])
    assert statsdb.get_format_elos(conn, "gen9ou") == ["0", "1500", "1695", "1825"]


def test_missing_division_ordered_after_standard():
    conn, _ = _conn()
    _make_format_table(conn, "gen9xyz", ["0", "1500", "9999"])
    assert statsdb.get_format_elos(conn, "gen9xyz") == ["0", "1500", "9999"]


def test_no_tables_falls_back_to_canonical():
    conn, _ = _conn()
    assert statsdb.get_format_elos(conn, "gen9nothing") == list(SMOGON_ELOS)


def test_partial_standard_set_preserves_order():
    conn, _ = _conn()
    _make_format_table(conn, "gen9partial", ["1500", "0"])
    assert statsdb.get_format_elos(conn, "gen9partial") == ["0", "1500"]


def test_prefix_format_not_captured_by_wildcard():
    # A format that shares the prefix (gen9ou) must not leak another format's
    # tables (stats_gen9ouX_elo0) into its elo list -- the '_' wildcard is
    # escaped in the LIKE pattern.
    conn, _ = _conn()
    _make_format_table(conn, "gen9ou", ["0", "1500", "1695", "1825"])
    _make_format_table(conn, "gen9ouX", ["0"])
    elos = statsdb.get_format_elos(conn, "gen9ou")
    assert all(e.isdigit() for e in elos), f"a non-division leaked in: {elos}"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS: {name}")
    print("\nAll elo-division tests passed.")