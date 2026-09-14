"""
Run directly with: python tests/test_export_worker.py

Validates the background ExportWorker end to end WITHOUT a running
GUI: the worker's signals are captured via direct connections and
run() is invoked inline -- the exact code path the QThread's started
signal would call.
"""

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fourslice.gui.export_worker import ExportWorker
from fourslice.storage import init_db, import_replay

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


def _make_db(db_path):
    """Build a small but real file-backed database at *db_path* so the
    worker's own init_db connection can read it."""
    conn = init_db(str(db_path))
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
    conn.close()


def test_export_worker_writes_all_files_with_progress():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "fourslice.db")
        _make_db(db_path)
        out = os.path.join(tmpdir, "out")

        worker = ExportWorker(db_path, out)
        progress = []
        finished = []
        errors = []
        worker.progress.connect(lambda *args: progress.append(args))
        worker.finished.connect(lambda *args: finished.append(args))
        worker.error.connect(lambda *args: errors.append(args))

        worker.run()

        assert errors == [], f"expected no errors, got {errors}"
        assert len(finished) == 1, f"expected one finished emit, got {len(finished)}"

        paths, total_rows, cancelled = finished[0]
        assert cancelled is False
        assert {Path(p).name for p in paths} == _EXPECTED_FILES
        assert all(Path(p).is_file() for p in paths)
        assert total_rows > 0, "exported data should have rows"

        assert len(progress) == 13, f"expected 13 progress emits, got {len(progress)}"
        done_values = [done for done, _, _ in progress]
        assert done_values == list(range(1, 14)), "progress should count 1..13"
        assert all(total == 13 for _, total, _ in progress)

    print("PASS: ExportWorker writes 13 files and reports per-file progress")


def test_export_worker_cancel_stops_between_files():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "fourslice.db")
        _make_db(db_path)
        out = os.path.join(tmpdir, "out")

        worker = ExportWorker(db_path, out)
        finished = []
        errors = []
        worker.finished.connect(lambda *args: finished.append(args))
        worker.error.connect(lambda *args: errors.append(args))

        def cancel_after_third(done, total, message):
            if done == 3:
                worker.cancel()

        worker.progress.connect(cancel_after_third)

        worker.run()

        assert errors == [], f"expected no errors, got {errors}"
        assert len(finished) == 1, f"expected one finished emit, got {len(finished)}"

        paths, total_rows, cancelled = finished[0]
        assert cancelled is True
        assert len(paths) == 3, f"expected the first 3 files, got {len(paths)}"

        # Cancel takes effect at the file boundary, so exactly the first
        # three CSVs should be on disk and nothing after that.
        names_on_disk = sorted(p.name for p in Path(out).glob("*.csv"))
        written_names = sorted(Path(p).name for p in paths)
        assert names_on_disk == written_names, (
            f"on disk {names_on_disk} != reported {written_names}"
        )
        assert names_on_disk == ["events.csv", "games.csv", "teams.csv"], (
            f"cancel should stop after the 3rd raw table, got {names_on_disk}"
        )
        assert total_rows > 0, "the three written files should have rows"

    print("PASS: ExportWorker cancel stops cleanly at the next file boundary")


def test_export_worker_reports_errors():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "fourslice.db")
        _make_db(db_path)
        blocked = os.path.join(tmpdir, "blocked")
        with open(blocked, "w") as f:
            f.write("not a folder")

        worker = ExportWorker(db_path, blocked)
        finished = []
        errors = []
        worker.finished.connect(lambda *args: finished.append(args))
        worker.error.connect(lambda *args: errors.append(args))

        worker.run()

        assert finished == [], f"expected no finished, got {finished}"
        assert len(errors) == 1, f"expected one error, got {errors}"
        assert "blocked" in errors[0][0]

    print("PASS: ExportWorker reports failures through the error signal")


if __name__ == "__main__":
    test_export_worker_writes_all_files_with_progress()
    test_export_worker_cancel_stops_between_files()
    test_export_worker_reports_errors()
    print("\nAll export worker tests passed.")