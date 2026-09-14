"""
Run directly with: python tests/test_export.py

Validates the export module writes all 13 expected CSV files (5 raw
tables + 8 pre-computed stat tables) with correct headers and row
counts, and that the pre-computed stat rows stay faithful to the
shared stats functions.
"""

import json
import sys
import tempfile
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fourslice.storage import init_db, import_replay, resolve_team_id
from fourslice.export import export_all_csvs
from fourslice.stats.turns_on_field import turns_on_field
from fourslice.stats.co_occurrence import co_occurrence
from fourslice.stats.ko_credit import ko_credit

SAMPLE_DIR = Path(__file__).resolve().parent / "sample_logs"

# The complete file set export_all_csvs is expected to write.
_EXPECTED_FILES = {
    "games.csv",
    "events.csv",
    "teams.csv",
    "team_pokemon.csv",
    "opponent_teams.csv",
    "stats_turns_on_field.csv",
    "stats_co_occurrence.csv",
    "stats_ko_credit.csv",
    "stats_attendance.csv",
    "stats_move_usage.csv",
    "stats_opponent_records.csv",
    "stats_leads.csv",
    "stats_brought_records.csv",
}


def load_log(filename):
    with open(SAMPLE_DIR / filename) as f:
        return json.load(f)["log"]


def _populated_conn():
    conn = init_db(":memory:")
    import_replay(
        conn, load_log("game1_win_vs_datrandomguy787.json"),
        "https://replay.pokemonshowdown.com/gen9championsvgc2026regmbbo3-2647404582",
        my_usernames={"salmoncashew"},
    )
    import_replay(
        conn, load_log("game2_loss_vs_aletito.json"),
        "https://replay.pokemonshowdown.com/gen9championsvgc2026regmbbo3-2647349643",
        my_usernames={"salmoncashew"},
    )
    return conn


def test_export_all_csvs_writes_expected_files():
    conn = _populated_conn()

    with tempfile.TemporaryDirectory() as tmpdir:
        written = export_all_csvs(conn, tmpdir)

        # Should write exactly 13 files
        assert len(written) == 13, f"expected 13 files, got {len(written)}"

        # Names should match the expected set
        names = {p.name for p in written}
        assert names == _EXPECTED_FILES, f"got {names}, expected {_EXPECTED_FILES}"

        # Every file should have the correct header columns
        for p in written:
            df = pd.read_csv(p)
            assert list(df.columns) == _EXPECTED_COLUMNS[p.name], (
                f"columns mismatch in {p.name}: got {list(df.columns)}, "
                f"expected {_EXPECTED_COLUMNS[p.name]}"
            )

        # Row counts should be reasonable (games has 2, teams has at least 1)
        games_df = pd.read_csv(Path(tmpdir) / "games.csv")
        assert len(games_df) == 2, f"expected 2 games, got {len(games_df)}"

        events_df = pd.read_csv(Path(tmpdir) / "events.csv")
        assert len(events_df) > 0, "events should have rows"

        teams_df = pd.read_csv(Path(tmpdir) / "teams.csv")
        assert len(teams_df) >= 1, "should have at least 1 team"

        team_pokemon_df = pd.read_csv(Path(tmpdir) / "team_pokemon.csv")
        assert len(team_pokemon_df) >= 6, "should have at least 6 roster slots"

        opponent_teams_df = pd.read_csv(Path(tmpdir) / "opponent_teams.csv")
        assert len(opponent_teams_df) >= 6, "should have at least 6 opponent roster slots"

        # Core stat tables should all carry rows for the populated DB.
        for name in sorted(_EXPECTED_FILES):
            if name.startswith("stats_"):
                stat_df = pd.read_csv(Path(tmpdir) / name)
                assert len(stat_df) > 0, f"{name} should have rows for a populated DB"

    conn.close()
    print("PASS: export_all_csvs writes all 13 expected CSV files with correct headers")


def test_export_all_csvs_creates_folder_if_missing():
    conn = _populated_conn()

    with tempfile.TemporaryDirectory() as tmpdir:
        subfolder = Path(tmpdir) / "new" / "nested" / "folder"
        written = export_all_csvs(conn, subfolder)
        assert len(written) == 13
        for p in written:
            assert p.exists(), f"{p} should exist"
            assert p.parent == subfolder, f"parent should be {subfolder}"

    conn.close()
    print("PASS: export_all_csvs creates nested folders if they don't exist")


def test_progress_callback_reports_each_file_with_row_counts():
    """progress_cb fires once per file, in write order, carrying the
    file name and its row count; callback counts must match the files
    on disk."""
    conn = _populated_conn()

    with tempfile.TemporaryDirectory() as tmpdir:
        calls = []
        written = export_all_csvs(
            conn, tmpdir, progress_cb=lambda name, n: calls.append((name, n)))

        assert len(calls) == len(written) == 13, (
            f"expected 13 files and 13 progress calls, "
            f"got {len(written)} files / {len(calls)} calls"
        )

        # Callback names must mirror the write order of the returned paths.
        called_names = [name for name, _ in calls]
        written_names = [p.name[:-4] for p in written]  # strip ".csv"
        assert called_names == written_names, (
            "callback names should match the write order"
        )

        # Row counts reported by the callback must match the files on disk.
        for name, row_count in calls:
            on_disk = len(pd.read_csv(Path(tmpdir) / f"{name}.csv"))
            assert row_count == on_disk, (
                f"{name}: callback reported {row_count} rows, "
                f"file has {on_disk}"
            )

    conn.close()
    print("PASS: progress_cb fires once per file with matching row counts")


def test_export_all_csvs_handles_empty_tables():
    """Empty DB should still produce all 13 CSVs with headers only."""
    conn = init_db(":memory:")

    with tempfile.TemporaryDirectory() as tmpdir:
        written = export_all_csvs(conn, tmpdir)
        assert len(written) == 13

        for p in written:
            df = pd.read_csv(p)
            assert list(df.columns) == _EXPECTED_COLUMNS[p.name], (
                f"columns mismatch in {p.name}: got {list(df.columns)}, "
                f"expected {_EXPECTED_COLUMNS[p.name]}"
            )
            assert len(df) == 0, f"expected 0 rows in empty DB, got {len(df)}"

    conn.close()
    print("PASS: export_all_csvs produces header-only CSVs for an empty database")


def test_stat_csvs_split_dimensions():
    """Side-capable stat CSVs split mine/opponent; per-regulation rows
    cover every game and the W/L rows partition each regulation, so
    summing across regulation/result reproduces unfiltered totals with
    no '(all)' sentinel rows (which would double-count in Power BI when
    no slicer selection is made)."""
    conn = _populated_conn()

    with tempfile.TemporaryDirectory() as tmpdir:
        export_all_csvs(conn, tmpdir)
        folder = Path(tmpdir)

        for name in ("stats_turns_on_field.csv", "stats_co_occurrence.csv",
                     "stats_ko_credit.csv"):
            df = pd.read_csv(folder / name)
            assert "perspective" in df.columns, f"{name} missing perspective"
            assert set(df["perspective"].unique()) == {"mine", "opponent"}, (
                f"{name}: unexpected perspective values"
            )

        for p in folder.glob("stats_*.csv"):
            df = pd.read_csv(p)
            if "regulation" in df.columns:
                assert len(df) > 0, f"{p.name} should have rows for a populated DB"
                assert "(all)" not in df["regulation"].values, (
                    f"{p.name} must not carry '(all)' regulation rows"
                )

        for name in ("stats_turns_on_field.csv", "stats_co_occurrence.csv",
                     "stats_ko_credit.csv", "stats_attendance.csv"):
            df = pd.read_csv(folder / name)
            assert set(df["result"].unique()) == {"W", "L"}, (
                f"{name}: result dimension should be exactly W/L"
            )

    conn.close()
    print("PASS: stat CSVs split dimensions with no (all) sentinel rows")


def test_flat_stat_rows_match_stat_functions():
    """The union of every (regulation, result) slice in each merged stat
    CSV must reproduce the shared stats functions' unfiltered output
    once summed per mon per perspective (single source of truth)."""
    conn = _populated_conn()

    with tempfile.TemporaryDirectory() as tmpdir:
        export_all_csvs(conn, tmpdir)
        folder = Path(tmpdir)

        # turns on field, mine perspective
        csv_df = pd.read_csv(folder / "stats_turns_on_field.csv")
        mine = csv_df[csv_df["perspective"] == "mine"]
        fn_df = turns_on_field(conn, my_side="mine")
        mine_grouped = mine.groupby("mon", as_index=False)[
            ["total_turns", "games_seen"]].sum()
        assert len(mine_grouped) == len(fn_df), (
            f"CSV has {len(mine_grouped)} mons, function returns {len(fn_df)}"
        )
        assert mine_grouped["total_turns"].sum() == fn_df["total_turns"].sum()
        assert mine_grouped["games_seen"].sum() == fn_df["games_seen"].sum()

        # KO credit, opponent perspective
        ko_df = pd.read_csv(folder / "stats_ko_credit.csv")
        opp = ko_df[ko_df["perspective"] == "opponent"]
        fn_ko = ko_credit(conn, my_side="opponent")
        opp_grouped = opp.groupby("mon", as_index=False)[
            ["ko_count", "games_seen"]].sum()
        assert len(opp_grouped) == len(fn_ko)
        assert opp_grouped["ko_count"].sum() == fn_ko["ko_count"].sum()
        assert opp_grouped["games_seen"].sum() == fn_ko["games_seen"].sum()

        # Co-occurrence, mine perspective
        co_df = pd.read_csv(folder / "stats_co_occurrence.csv")
        co_mine = co_df[co_df["perspective"] == "mine"]
        fn_co = co_occurrence(conn, my_side="mine")
        co_grouped = co_mine.groupby("pair", as_index=False)[
            ["total_turns", "games_seen"]].sum()
        assert len(co_grouped) == len(fn_co)
        assert co_grouped["total_turns"].sum() == fn_co["total_turns"].sum()
        assert co_grouped["games_seen"].sum() == fn_co["games_seen"].sum()

    conn.close()
    print("PASS: flat stat CSV rows match the shared stats functions")


_EXPECTED_COLUMNS = {
    "games.csv": [
        "game_id", "replay_url", "p1_name", "p2_name", "winner",
        "my_side", "result", "regulation", "battle_size",
        "team_id"
    ],
    "events.csv": [
        "event_id", "game_id", "turn", "mon", "event_type",
        "move_name", "p1a", "p1b", "p2a", "p2b",
        "ko_credit_mon", "ko_credit_side"
    ],
    "teams.csv": [
        "team_id", "nickname", "roster_key", "created_at",
        "naming_pokemon", "naming_regulation"
    ],
    "team_pokemon.csv": [
        "team_id", "slot_order", "species", "item", "moves"
    ],
    "opponent_teams.csv": [
        "game_id", "slot_order", "species", "item", "moves"
    ],
    "stats_turns_on_field.csv": [
        "regulation", "result", "perspective", "mon", "total_turns",
        "games_seen", "avg_turns_per_game"
    ],
    "stats_co_occurrence.csv": [
        "regulation", "result", "perspective", "pair", "total_turns",
        "games_seen", "avg_turns_per_game"
    ],
    "stats_ko_credit.csv": [
        "regulation", "result", "perspective", "mon", "ko_count",
        "games_seen", "avg_kos_per_game"
    ],
    "stats_attendance.csv": [
        "regulation", "result", "mon", "team_games", "bring_games",
        "total_games", "team_rate", "bring_rate", "bring_rate_overall"
    ],
    "stats_move_usage.csv": [
        "team_id", "team_nickname", "regulation", "result", "side",
        "species", "slot_order", "move_name", "use_count", "pct_of_uses"
    ],
    "stats_opponent_records.csv": [
        "regulation", "mon", "games", "wins", "losses", "pct"
    ],
    "stats_leads.csv": [
        "regulation", "battle_size", "lead", "lead_a", "lead_b",
        "games", "wins", "losses", "pct"
    ],
    "stats_brought_records.csv": [
        "team_id", "team_nickname", "regulation", "mon", "games",
        "wins", "losses", "pct"
    ],
}


if __name__ == "__main__":
    test_export_all_csvs_writes_expected_files()
    test_export_all_csvs_creates_folder_if_missing()
    test_export_all_csvs_handles_empty_tables()
    test_progress_callback_reports_each_file_with_row_counts()
    test_stat_csvs_split_dimensions()
    test_flat_stat_rows_match_stat_functions()
    print("\nAll export tests passed.")