"""Tests for the usage-ladder capture source + capture_log source markers.

Covers the fix that makes stats.db usage numbers come from the authoritative
NON-moveset usage ladders (<fmt>-<elo>.txt) instead of being recomputed
from the moveset file, plus the capture_log source marker that lets old
moveset-source captures be detected as stale and re-captured.

These tests never open the real stats.db: `_capture_format_elo` is exercised
with a direct temp-sqlite connection, and any path that resolves the DB
through config.get_stats_db_path() is monkeypatched to a temp file.

Run with:  python -m pytest tests/test_usage_capture.py
"""

import gzip
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from fourslice.extstats import SnapshotStore, statsdb, smogon
from fourslice.extstats.models import Month

# A usage table that mirrors the real per-Pokemon figures (Great Tusk-style):
# usage % is the file's own column, raw is the accompanying count.
USAGE_TEXT = """Total battles: 730502
 Avg. weight/team: 1.0
 + ---- + --------------------------- + --------- + ------ + ------- + ------ + ------- +
 | Rank | Pokemon                    | Usage %   | Raw    | %       | Real   | %       |
 + ---- + --------------------------- + --------- + ------ + ------- + ------ + ------- +
 | 1    | Great Tusk                 | 28.58425% | 417617 | 28.584% | 334940 | 28.308% |
 | 2    | Iron Treads                | 22.53147% | 329157 | 22.531% | 268691 | 22.709% |
 | 3    | Landorus-Therian           | 19.03547% | 278068 | 19.035% | 224053 | 18.933% |
 """

MOVESET_TEXT = """+--------------------------------------------------------------------+
| Great Tusk                                                        |
+--------------------------------------------------------------------+
| Raw count: 389490                                                  |
| Avg. weight: 1                                                     |
| Viability Ceiling: 92                                              |
+--------------------------------------------------------------------+
| Abilities                                                          |
| Protosynthesis 100.000%                                            |
+--------------------------------------------------------------------+
| Items                                                              |
| Booster Energy 58.711%                                             |
+--------------------------------------------------------------------+
| Moves                                                              |
| Headlong Rush 92.543%                                              |
| Earthquake 85.400%                                                 |
+--------------------------------------------------------------------+
| Teras                                                              |
| Ground 34.385%                                                     |
+--------------------------------------------------------------------+
"""


def _period_dir(tmp_path: Path) -> Path:
    """A period cache dir holding the usage ladder plus a moveset file."""
    period = tmp_path / "cache" / "smogon" / "2026-08"
    period.mkdir(parents=True, exist_ok=True)
    (period / "gen9ou-0.txt").write_text(USAGE_TEXT, encoding="utf-8")
    moveset = period / "moveset"
    moveset.mkdir(exist_ok=True)
    (moveset / "gen9ou-0.txt").write_text(MOVESET_TEXT, encoding="utf-8")
    return period


def _connected_db(tmp_path: Path) -> sqlite3.Connection:
    return statsdb.init_db(tmp_path / "stats.db")


def test_capture_uses_usage_ladder_percentages(tmp_path):
    """usage_pct/raw_count come straight from the usage file column, not a
    recomputed share of the moveset file's raw counts (the old bug showed
    Great Tusk at ~4% instead of the real 28.58425%)."""
    conn = _connected_db(tmp_path)
    try:
        ok = smogon._capture_format_elo(
            conn, "2026-08", "gen9ou", "0", _period_dir(tmp_path)
        )
        assert ok
        rows = statsdb.get_format_rows(conn, "2026-08", "gen9ou", "0")
        assert len(rows) == 3
        great_tusk = rows[0]  # ordered usage_pct DESC
        assert great_tusk["pokemon"] == "Great Tusk"
        assert great_tusk["usage_pct"] == pytest.approx(28.58425)
        assert great_tusk["raw_count"] == 417617
    finally:
        conn.close()


def test_capture_merges_moveset_detail_by_name(tmp_path):
    """The moveset columns still land on each row, merged from the moveset
    file by Pokemon name; Pokemon absent from the moveset file keep an
    usage-only row."""
    conn = _connected_db(tmp_path)
    try:
        assert smogon._capture_format_elo(conn, "2026-08", "gen9ou", "0", _period_dir(tmp_path))
        row = statsdb.get_moveset(conn, "2026-08", "gen9ou", "0", "Great Tusk")
        assert row["moves"] == [
            {"name": "Headlong Rush", "pct": 92.543},
            {"name": "Earthquake", "pct": 85.4},
        ]
        assert row["abilities"] == [{"name": "Protosynthesis", "pct": 100.0}]
        assert row["items"][0]["name"] == "Booster Energy"
        assert row["avg_weight"] == 1
        assert row["viability_ceiling"] == 92
        lando = statsdb.get_moveset(conn, "2026-08", "gen9ou", "0", "Landorus-Therian")
        assert lando["pokemon"] == "Landorus-Therian"
        assert lando["moves"] is None
        assert lando["usage_pct"] == pytest.approx(19.03547)
    finally:
        conn.close()


def test_capture_reads_gzipped_usage_ladder(tmp_path):
    conn = _connected_db(tmp_path)
    try:
        period = tmp_path / "cache" / "smogon" / "2026-08"
        period.mkdir(parents=True, exist_ok=True)
        with gzip.open(period / "gen9ou-0.txt.gz", "wt", encoding="utf-8") as f:
            f.write(USAGE_TEXT)
        assert smogon._capture_format_elo(conn, "2026-08", "gen9ou", "0", period)
        rows = statsdb.get_format_rows(conn, "2026-08", "gen9ou", "0")
        assert rows[0]["usage_pct"] == pytest.approx(28.58425)
    finally:
        conn.close()


def test_capture_marks_capture_log_with_source_usage(tmp_path):
    conn = _connected_db(tmp_path)
    try:
        assert smogon._capture_format_elo(conn, "2026-08", "gen9ou", "0", _period_dir(tmp_path))
        assert statsdb.get_captured_sources(conn, "2026-08", "gen9ou") == {"0": "usage"}
    finally:
        conn.close()


def test_capture_leaves_uncaptured_when_usage_ladder_missing(tmp_path):
    """No usage file in the period dir -> nothing written, nothing marked.
    This is the transient 'not crawled yet' state: returning False means the
    caller leaves it for retry, never claiming the format is done."""
    conn = _connected_db(tmp_path)
    try:
        period = tmp_path / "cache" / "smogon" / "2026-08"
        period.mkdir(parents=True, exist_ok=True)
        moveset = period / "moveset"
        moveset.mkdir(exist_ok=True)
        (moveset / "gen9ou-0.txt").write_text(MOVESET_TEXT, encoding="utf-8")
        assert not smogon._capture_format_elo(conn, "2026-08", "gen9ou", "0", period)
        assert statsdb.get_captured_sources(conn, "2026-08", "gen9ou") == {}
        assert statsdb.get_format_rows(conn, "2026-08", "gen9ou", "0") == []
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# capture_log source-marking helpers
# ---------------------------------------------------------------------------

def test_mark_capture_unavailable_preserves_rows_and_is_queryable(tmp_path):
    conn = _connected_db(tmp_path)
    try:
        statsdb.mark_captured(conn, "2026-08", "gen9ou", "0")
        statsdb.mark_captured(conn, "2026-08", "gen9ou", "1500", source="usage")
        statsdb.mark_capture_unavailable(conn, "2026-08", "gen9ou", "0")
        assert statsdb.get_captured_sources(conn, "2026-08", "gen9ou") == {
            "0": "unavailable", "1500": "usage",
        }
        # no-elos variant marks every row for the format
        statsdb.mark_capture_unavailable(conn, "2026-08", "gen9ou")
        assert statsdb.get_captured_sources(conn, "2026-08", "gen9ou") == {
            "0": "unavailable", "1500": "unavailable",
        }
    finally:
        conn.close()


def test_get_captured_sources_reports_legacy_none(tmp_path):
    """Marking with the pre-source API leaves source=NULL -- the gating
    checks treat NULL/moveset as stale even though they're 'captured'."""
    conn = _connected_db(tmp_path)
    try:
        statsdb.mark_captured(conn, "2026-08", "gen9ou", "0")  # old-style call
        assert statsdb.get_captured_sources(conn, "2026-08", "gen9ou") == {"0": None}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Gating: run_all_formats + _is_fully_captured against a temp mirror + DB
# ---------------------------------------------------------------------------

def _mirror(tmp_path: Path, root: Path | None = None) -> Path:
    root = root or (tmp_path / "mirror")
    day = root / "raw" / "stats" / "2026-08"
    day.mkdir(parents=True, exist_ok=True)
    (day / "gen9ou-0.txt").write_text(USAGE_TEXT, encoding="utf-8")
    (day / "gen9ou-1500.txt").write_text(USAGE_TEXT, encoding="utf-8")
    (day / "gen9uu-0.txt").write_text(USAGE_TEXT, encoding="utf-8")
    return root


def _patch_stats_db_path(monkeypatch, tmp_path: Path) -> Path:
    """Point fourslice.config.get_stats_db_path (the module smogon and the
    updater both import) at a temp stats.db; return that path."""
    from fourslice import config as f_config

    db_path = tmp_path / "stats.db"
    monkeypatch.setattr(f_config, "get_stats_db_path", lambda: db_path)
    return db_path


def _open_db(db_path: Path) -> sqlite3.Connection:
    return statsdb.init_db(db_path)


def test_run_all_formats_recaptures_legacy_moveset_source(tmp_path, monkeypatch):
    """A capture_log row with legacy source (NULL/"moveset") is stale:
    run_all_formats must re-capture from the usage ladder even though
    capture_log already lists the elo, and the new source must be 'usage'."""
    mirror = _mirror(tmp_path)
    store = SnapshotStore(tmp_path / "cache")
    db_path = _patch_stats_db_path(monkeypatch, tmp_path)
    conn = _open_db(db_path)
    statsdb.mark_captured(conn, "2026-08", "gen9ou", "0")  # legacy, no source
    conn.commit()
    conn.close()

    smogon.run_all_formats(mirror, store)

    conn = _open_db(db_path)
    try:
        # both ladders the mirror has (0 + 1500) recaptured fresh
        assert statsdb.get_captured_sources(conn, "2026-08", "gen9ou") == {
            "0": "usage", "1500": "usage",
        }
        rows = statsdb.get_format_rows(conn, "2026-08", "gen9ou", "0")
        assert rows[0]["usage_pct"] == pytest.approx(28.58425)
    finally:
        conn.close()


def test_run_all_formats_parks_stale_capture_with_no_ladder(tmp_path, monkeypatch):
    """The edge case: a legacy capture exists but the mirror has no usage
    ladder for that format (only old moveset rows). The format must be
    marked 'unavailable' -- a visible, terminal state -- instead of being
    silently skipped or retried every run."""
    mirror = _mirror(tmp_path)
    store = SnapshotStore(tmp_path / "cache")
    db_path = _patch_stats_db_path(monkeypatch, tmp_path)
    conn = _open_db(db_path)
    statsdb.mark_captured(conn, "2026-08", "gen9ou", "0")  # legacy
    statsdb.mark_captured(conn, "2026-08", "gen9ou", "1500")  # legacy
    conn.commit()
    conn.close()
    # Remove gen9ou's usage ladders from the mirror (moveset-only month)
    for name in ("gen9ou-0.txt", "gen9ou-1500.txt"):
        (mirror / "raw" / "stats" / "2026-08" / name).unlink()

    smogon.run_all_formats(mirror, store)

    conn = _open_db(db_path)
    try:
        assert statsdb.get_captured_sources(conn, "2026-08", "gen9ou") == {
            "0": "unavailable", "1500": "unavailable",
        }
    finally:
        conn.close()


def test_run_all_formats_parked_quiet_without_ladder_recaptured_with_it(
    tmp_path, monkeypatch
):
    """A parked ('unavailable') format is not reprocessed while its ladder
    is absent, and is re-captured to 'usage' as soon as the ladder exists."""
    mirror = _mirror(tmp_path)
    store = SnapshotStore(tmp_path / "cache")
    db_path = _patch_stats_db_path(monkeypatch, tmp_path)
    day = mirror / "raw" / "stats" / "2026-08"
    conn = _open_db(db_path)
    statsdb.mark_captured(conn, "2026-08", "gen9ou", "0")
    statsdb.mark_captured(conn, "2026-08", "gen9ou", "1500")
    conn.commit()
    statsdb.mark_capture_unavailable(conn, "2026-08", "gen9ou", "0", "1500")
    conn.close()

    # ladder gone -> parked rows stay put
    for name in ("gen9ou-0.txt", "gen9ou-1500.txt"):
        (day / name).unlink()
    smogon.run_all_formats(mirror, store)
    conn = _open_db(db_path)
    assert statsdb.get_captured_sources(conn, "2026-08", "gen9ou") == {
        "0": "unavailable", "1500": "unavailable",
    }
    conn.close()

    # ladder returns -> the normal path re-captures them
    (day / "gen9ou-0.txt").write_text(USAGE_TEXT, encoding="utf-8")
    (day / "gen9ou-1500.txt").write_text(USAGE_TEXT, encoding="utf-8")
    smogon.run_all_formats(mirror, store)
    conn = _open_db(db_path)
    try:
        assert statsdb.get_captured_sources(conn, "2026-08", "gen9ou") == {
            "0": "usage", "1500": "usage",
        }
    finally:
        conn.close()


def test_run_all_formats_skips_current_usage_source_formats(tmp_path, monkeypatch):
    """Fully usage-captured formats (all mirror ladders with source='usage')
    are left alone -- idempotent backfill, no re-parse."""
    mirror = _mirror(tmp_path)
    store = SnapshotStore(tmp_path / "cache")
    db_path = _patch_stats_db_path(monkeypatch, tmp_path)
    conn = _open_db(db_path)
    statsdb.mark_captured(conn, "2026-08", "gen9ou", "0", source="usage")
    statsdb.mark_captured(conn, "2026-08", "gen9ou", "1500", source="usage")
    statsdb.mark_captured(conn, "2026-08", "gen9uu", "0", source="usage")
    conn.commit()
    conn.close()

    smogon.run_all_formats(mirror, store)

    conn = _open_db(db_path)
    try:
        assert statsdb.get_captured_sources(conn, "2026-08", "gen9ou") == {
            "0": "usage", "1500": "usage",
        }
    finally:
        conn.close()


def test_is_fully_captured_semantics(tmp_path, monkeypatch):
    """_is_fully_captured considers [all-usage] fully done, [any stale]
    pending, and [parked/unavailable] accounted-but-not-pending (so an
    unrecoverable format doesn't force an update every launch)."""
    from fourslice.gui.external_stats_updater import _is_fully_captured

    # _is_fully_captured expects out_dir whose "smogon" subdir is the mirror root.
    datadir = tmp_path / "datadir"
    _mirror(tmp_path, root=datadir / "smogon")
    db_path = _patch_stats_db_path(monkeypatch, tmp_path)

    def seed(mark):
        conn = _open_db(db_path)
        try:
            mark(conn)
            conn.commit()
        finally:
            conn.close()

    # all fresh -> fully captured
    def fresh(conn):
        statsdb.mark_captured(conn, "2026-08", "gen9ou", "0", source="usage")
        statsdb.mark_captured(conn, "2026-08", "gen9ou", "1500", source="usage")
        statsdb.mark_captured(conn, "2026-08", "gen9uu", "0", source="usage")
    seed(fresh)
    assert _is_fully_captured(datadir, "2026-08") is True

    # any stale -> not fully captured
    def stale(conn):
        statsdb.mark_captured(conn, "2026-08", "gen9ou", "1500")  # legacy again
    seed(stale)
    assert _is_fully_captured(datadir, "2026-08") is False
    seed(fresh)

    # parked -> not pending even when the mirror has no ladder at all
    for name in ("gen9ou-0.txt", "gen9ou-1500.txt"):
        (datadir / "smogon" / "raw" / "stats" / "2026-08" / name).unlink()
    def park(conn):
        statsdb.mark_capture_unavailable(conn, "2026-08", "gen9ou", "0", "1500")
    seed(park)
    assert _is_fully_captured(datadir, "2026-08") is True


if __name__ == "__main__":
    import tempfile
    def _tmp():
        return Path(tempfile.mkdtemp())

    test_capture_uses_usage_ladder_percentages(_tmp())
    test_capture_merges_moveset_detail_by_name(_tmp())
    test_capture_reads_gzipped_usage_ladder(_tmp())
    test_capture_marks_capture_log_with_source_usage(_tmp())
    test_capture_leaves_uncaptured_when_usage_ladder_missing(_tmp())
    test_mark_capture_unavailable_preserves_rows_and_is_queryable(_tmp())
    test_get_captured_sources_reports_legacy_none(_tmp())
    print("\nAll capture-source tests passed.")