"""Offline tests for the Smogon snapshot/parse pipeline.

Run with:  python -m pytest tests/test_extstats_smogon.py
"""

from pathlib import Path

import pytest

from fourslice.extstats import PeriodUnavailableError, SnapshotStore, smogon
from fourslice.extstats import statsdb


@pytest.fixture
def stats_db_path(tmp_path, monkeypatch):
    """Point config.get_stats_db_path at a temp DB so pipeline runs stay
    offline and isolated. The pipeline's derived store is stats.db (not
    parsed/*.json), and every entry point reaches it through this one
    config function -- everything in this test file otherwise agrees."""
    from fourslice import config

    db = tmp_path / "stats.db"
    monkeypatch.setattr(config, "get_stats_db_path", lambda *a, **k: db)
    return db


def _captured(stats_db, period_id):
    """{format: frozenset(elds)} captured (source="usage") for a period."""
    conn = statsdb.init_db(stats_db)
    try:
        formats = {
            fmt
            for (fmt,) in conn.execute(
                "SELECT DISTINCT format FROM capture_log WHERE period_id = ?",
                (period_id,),
            )
        }
        return {
            fmt: frozenset(statsdb.get_captured_elos(conn, period_id, fmt))
            for fmt in sorted(formats)
        }
    finally:
        conn.close()

USAGE_TEXT = """ Total battles: 18989
 Avg. weight/team: 1.0
 + ---- + ------------------ + --------- + ------ + ------- + ------ + ------- +
 | Rank | Pokemon            | Usage %   | Raw    | %       | Real   | %       |
 + ---- + ------------------ + --------- + ------ + ------- + ------ + ------- +
 | 1    | Tauros             | 83.06651% | 31547  | 83.067% | 24390  | 74.154% |
 | 2    | Snorlax            | 73.63210% | 27964  | 73.632% | 24012  | 73.005% |
 | 3    | Chansey            | 69.77197% | 26498  | 69.772% | 23562  | 71.637% |
 | 4    | Exeggutor          | 62.06488% | 23571  | 62.065% | 21379  | 65.000% |
 """


def _usage_text(battles):
    return USAGE_TEXT.replace("18989", str(battles))


def _mirror(tmp_path, months=("2025-07", "2026-07")):
    """Every month has gen1ou (most battles), vgc2026 (doubles, #2),
    gen3uu (#3), gen3doublesou (doubles, #4) and gen2ou (least); gen1ou
    also publishes the 1500/1630/1760 ladders."""
    root = tmp_path / "mirror"
    for month in months:
        day = root / "raw" / "stats" / month
        day.mkdir(parents=True)
        (day / "gen1ou-0.txt").write_text(USAGE_TEXT, encoding="utf-8")
        (day / "gen1ou-1500.txt").write_text(_usage_text(9000), encoding="utf-8")
        (day / "gen1ou-1630.txt").write_text(_usage_text(7000), encoding="utf-8")
        (day / "gen1ou-1760.txt").write_text(_usage_text(5000), encoding="utf-8")
        (day / "vgc2026-0.txt").write_text(_usage_text(15000), encoding="utf-8")
        (day / "gen3uu-0.txt").write_text(_usage_text(12000), encoding="utf-8")
        (day / "gen3doublesou-0.txt").write_text(_usage_text(10000), encoding="utf-8")
        (day / "gen2ou-0.txt").write_text(_usage_text(4000), encoding="utf-8")
        (day / "gen1ou-0.txt.gz").write_text("ignored", encoding="utf-8")
    return root


def test_discover_latest_month_picks_newest(tmp_path):
    month = smogon.discover_latest_month(_mirror(tmp_path))
    assert month.source == "smogon"
    assert month.id == "2026-07"
    assert month.elos == ("0",)


def test_discover_latest_month_raises_when_empty(tmp_path):
    with pytest.raises(PeriodUnavailableError):
        smogon.discover_latest_month(tmp_path / "mirror")


def test_resolve_month_explicit_and_missing(tmp_path):
    mirror = _mirror(tmp_path)
    month = smogon.resolve_month(mirror, "2025-07")
    assert month.id == "2025-07"
    with pytest.raises(PeriodUnavailableError):
        smogon.resolve_month(mirror, "2024-01")
    with pytest.raises(ValueError):
        smogon.resolve_month(mirror, "not-a-month")


def test_parse_usage_text_real_format():
    doc = smogon.parse_usage_text(USAGE_TEXT, period="2026-07", fmt="gen1ou", elo="0")
    assert doc["source"] == "smogon"
    assert doc["period"] == "2026-07"
    assert doc["format"] == "gen1ou"
    assert doc["elo"] == "0"
    assert doc["total_battles"] == 18989
    assert len(doc["rows"]) == 4
    first = doc["rows"][0]
    assert first == {
        "rank": 1,
        "pokemon": "Tauros",
        "usage_pct": 83.06651,
        "raw": 31547,
        "raw_pct": 83.067,
        "real": 24390,
        "real_pct": 74.154,
    }


def test_parse_usage_text_no_table_raises():
    with pytest.raises(ValueError):
        smogon.parse_usage_text("just some text\n", period="2026-07", fmt="gen1ou", elo="0")


def test_format_standings_sorted_and_kinded(tmp_path):
    mirror = _mirror(tmp_path)
    standings = smogon.format_standings(mirror, smogon.Month(id="2026-07"))
    assert [(s["format"], s["battles"], s["kind"]) for s in standings] == [
        ("gen1ou", 18989, "singles"),
        ("vgc2026", 15000, "doubles"),
        ("gen3uu", 12000, "singles"),
        ("gen3doublesou", 10000, "doubles"),
        ("gen2ou", 4000, "singles"),
    ]


def test_most_played_format_picks_highest_battles(tmp_path):
    mirror = _mirror(tmp_path)
    assert smogon.most_played_format(mirror, smogon.Month(id="2026-07")) == "gen1ou"


def test_most_played_format_tie_goes_to_first_sorted(tmp_path):
    root = tmp_path / "mirror"
    day = root / "raw" / "stats" / "2026-07"
    day.mkdir(parents=True)
    (day / "gen2ou-0.txt").write_text(_usage_text(1000), encoding="utf-8")
    (day / "gen1ou-0.txt").write_text(_usage_text(1000), encoding="utf-8")
    assert smogon.most_played_format(root, smogon.Month(id="2026-07")) == "gen1ou"


def test_most_played_format_raises_without_overall_tables(tmp_path):
    root = tmp_path / "mirror"
    day = root / "raw" / "stats" / "2026-07"
    day.mkdir(parents=True)
    (day / "gen1ou-1500.txt").write_text(_usage_text(1000), encoding="utf-8")
    with pytest.raises(PeriodUnavailableError):
        smogon.most_played_format(root, smogon.Month(id="2026-07"))


def test_ladder_files_for_lists_every_division(tmp_path):
    mirror = _mirror(tmp_path)
    files = smogon.ladder_files_for(mirror, smogon.Month(id="2026-07"), "gen1ou")
    assert [elo for _, elo, _ in files] == ["0", "1500", "1630", "1760"]
    assert all(fmt == "gen1ou" for fmt, _, _ in files)

def test_run_snapshots_chosen_format_all_divisions(tmp_path, stats_db_path):
    """run() captures format standings + Pokemon rows into stats.db for the
    top Singles + top Doubles formats (gen1ou all 4 ladders, vgc2026's 0)
    and nothing else. Parsed JSONs no longer exist -- stats.db is the only
    derived store (see extstats.smogon.parse_and_cache)."""
    mirror = _mirror(tmp_path)
    store = SnapshotStore(tmp_path / "cache")

    # Default initial_formats=2 picks top Singles + top Doubles
    record = smogon.run(mirror, store)

    assert record.source == "smogon" and record.period_id == "2026-07"
    assert store.latest("smogon").period_id == "2026-07"

    conn = statsdb.init_db(stats_db_path)
    try:
        # gen1ou has 4 elos, vgc2026 has only 0 -- captured into stats.db
        assert statsdb.get_captured_elos(conn, "2026-07", "gen1ou") == {"0", "1500", "1630", "1760"}
        assert statsdb.get_captured_elos(conn, "2026-07", "vgc2026") == {"0"}
        # no other format of the same month is captured
        assert statsdb.get_captured_elos(conn, "2026-07", "gen2ou") == set()
        assert statsdb.get_captured_elos(conn, "2026-07", "gen3uu") == set()
        assert statsdb.get_captured_elos(conn, "2026-07", "gen3doublesou") == set()

        # Pokemon rows land in stats.db, straight from the usage ladder
        rows = statsdb.get_format_rows(conn, "2026-07", "gen1ou", "0")
        assert rows[0]["pokemon"] == "Tauros"
        assert len(statsdb.get_format_rows(conn, "2026-07", "gen1ou", "1500")) == 4
    finally:
        conn.close()

    # Raw .txt files are cleaned from the CACHE once captured (the mirror is untouched)
    period = store.period_dir("smogon", "2026-07")
    for elo in ("0", "1500", "1630", "1760"):
        assert not (period / f"gen1ou-{elo}.txt").exists()
    assert not (period / "vgc2026-0.txt").exists()
    assert not (period / "gen2ou-0.txt").exists()


def test_run_writes_format_standings(tmp_path, stats_db_path):
    """run() stores the full month standings (not just the captured formats)
    in the smogon_formats table, most-played first with singles/doubles kind."""
    mirror = _mirror(tmp_path)
    store = SnapshotStore(tmp_path / "cache")

    smogon.run(mirror, store)

    conn = statsdb.init_db(stats_db_path)
    try:
        standings = statsdb.get_format_standings(conn, "2026-07")
        assert standings[0]["format"] == "gen1ou"
        assert [s["format"] for s in standings] == [
            "gen1ou", "vgc2026", "gen3uu", "gen3doublesou", "gen2ou",
        ]
        assert all("kind" in s for s in standings)
    finally:
        conn.close()


def test_run_same_month_replaces_broader_previous_snapshot(tmp_path):
    mirror = _mirror(tmp_path)
    store = SnapshotStore(tmp_path / "cache")
    # simulate an older, broader-scope snapshot of the same month:
    stale = store.period_dir("smogon", "2026-07") / "gen2ou-0.txt"
    stale.parent.mkdir(parents=True)
    stale.write_text(_usage_text(99), encoding="utf-8")

    smogon.run(mirror, store)

    assert not (store.period_dir("smogon", "2026-07") / "gen2ou-0.txt").exists()


def test_run_new_month_prunes_previous(tmp_path, stats_db_path):
    mirror = _mirror(tmp_path)
    store = SnapshotStore(tmp_path / "cache")

    smogon.run(mirror, store, spec="2025-07")
    smogon.run(mirror, store, spec="2026-07")

    assert not store.period_dir("smogon", "2025-07").exists()
    assert store.latest("smogon").period_id == "2026-07"
    # each run wrote to the same isolated DB, and the newer month wins
    assert _captured(stats_db_path, "2026-07") == {
        "gen1ou": frozenset({"0", "1500", "1630", "1760"}),
        "vgc2026": frozenset({"0"}),
    }


def test_run_picks_top_singles_and_doubles_by_default(tmp_path, stats_db_path):
    """run() with default initial_formats=2 picks top Singles + top Doubles."""
    mirror = _mirror(tmp_path)
    store = SnapshotStore(tmp_path / "cache")

    record = smogon.run(mirror, store)

    # Should have exactly 2 formats captured: gen1ou (singles #1), vgc2026 (doubles #1)
    assert _captured(stats_db_path, record.period_id) == {
        "gen1ou": frozenset({"0", "1500", "1630", "1760"}),
        "vgc2026": frozenset({"0"}),
    }

    # smogon_formats lists ALL formats in order
    conn = statsdb.init_db(stats_db_path)
    try:
        standings = statsdb.get_format_standings(conn, record.period_id)
        assert [s["format"] for s in standings] == [
            "gen1ou", "vgc2026", "gen3uu", "gen3doublesou", "gen2ou"
        ]
    finally:
        conn.close()


def test_run_with_initial_formats_all_snapshots_everything(tmp_path, stats_db_path):
    """run(initial_formats=0 or ALL_FORMATS) captures all formats."""
    mirror = _mirror(tmp_path)
    store = SnapshotStore(tmp_path / "cache")

    record = smogon.run(mirror, store, initial_formats=0)

    assert _captured(stats_db_path, record.period_id) == {
        "gen1ou": frozenset({"0", "1500", "1630", "1760"}),
        "vgc2026": frozenset({"0"}),
        "gen3uu": frozenset({"0"}),
        "gen3doublesou": frozenset({"0"}),
        "gen2ou": frozenset({"0"}),
    }


def test_run_all_formats_backfills_remaining(tmp_path, stats_db_path):
    """run_all_formats() after run() picks up the remaining formats."""
    mirror = _mirror(tmp_path)
    store = SnapshotStore(tmp_path / "cache")

    # Stage 1: run() with default top 2
    record1 = smogon.run(mirror, store)
    assert _captured(stats_db_path, record1.period_id) == {
        "gen1ou": frozenset({"0", "1500", "1630", "1760"}),
        "vgc2026": frozenset({"0"}),
    }

    # Stage 2: run_all_formats() backfills the rest
    smogon.run_all_formats(mirror, store)
    assert _captured(stats_db_path, record1.period_id) == {
        "gen1ou": frozenset({"0", "1500", "1630", "1760"}),
        "vgc2026": frozenset({"0"}),
        "gen3uu": frozenset({"0"}),
        "gen3doublesou": frozenset({"0"}),
        "gen2ou": frozenset({"0"}),
    }


def _stored_row_count(db_path: Path, period_id: str) -> int:
    """Total Pokemon rows in stats.db for a period (capture is all-or-nothing
    per format+elo, so every captured elo contributes its parsed rows)."""
    conn = statsdb.init_db(db_path)
    try:
        total = 0
        for fmt, elos in _captured(db_path, period_id).items():
            for elo in elos:
                total += len(statsdb.get_format_rows(conn, period_id, fmt, elo))
        return total
    finally:
        conn.close()


def test_run_all_formats_idempotent(tmp_path, stats_db_path):
    """Running run_all_formats() twice doesn't duplicate or error."""
    mirror = _mirror(tmp_path)
    store = SnapshotStore(tmp_path / "cache")

    smogon.run(mirror, store)
    smogon.run_all_formats(mirror, store)
    captured1 = _captured(stats_db_path, "2026-07")
    rows1 = _stored_row_count(stats_db_path, "2026-07")

    # Run again
    smogon.run_all_formats(mirror, store)
    captured2 = _captured(stats_db_path, "2026-07")
    rows2 = _stored_row_count(stats_db_path, "2026-07")

    assert captured1 == captured2  # all 5 formats, same elos
    assert rows1 == rows2  # re-run captures nothing new (INSERT OR REPLACE idempotent)


def test_run_single_format_idempotent(tmp_path, stats_db_path):
    """run_single_format() can be called repeatedly without wiping other formats."""
    mirror = _mirror(tmp_path)
    store = SnapshotStore(tmp_path / "cache")

    # First, cache all formats
    smogon.run(mirror, store, initial_formats=0)
    captured_before = _captured(stats_db_path, "2026-07")
    rows_before = _stored_row_count(stats_db_path, "2026-07")

    # Get the standings
    standings = smogon.format_standings(mirror, smogon.Month(id="2026-07"))
    month = smogon.resolve_month(mirror, "2026-07")

    # Re-run single format for gen1ou
    smogon.run_single_format(mirror, store, month, "gen1ou", standings)
    captured_after = _captured(stats_db_path, "2026-07")
    rows_after = _stored_row_count(stats_db_path, "2026-07")

    # Same formats and row counts -- nothing wiped, nothing re-added
    assert captured_after == captured_before
    assert rows_after == rows_before
