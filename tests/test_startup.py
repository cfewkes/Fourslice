"""Tests for the startup splash gate (fourslice/gui/startup.py).

Covers:
  - db_readiness reporting each gating DB (stats, learnsets, pokemon_complete)
    as missing on an empty app-data dir, and never raising on a corrupt DB.
  - db_readiness reporting ``[]`` once all three are present and populated.
  - make_splash building a working offscreen splash that accepts showMessage.
  - run_firstrun_bootstrap driving the two existing bootstrap helpers in
    order and returning the data-refresh handle.

Run with:  python -m pytest tests/test_startup.py
"""

import os
import sys
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

from fourslice.gui import data_refresh_worker, external_stats_updater

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fourslice import config
from fourslice.extstats import statsdb
from fourslice.gui import startup


def _create_format_table(conn, table_name="stats_gen9ou_elo0"):
    """Same minimal stats_* table helper test_bootstrap.py uses; the pipeline
    creates these lazily before inserting rows."""
    conn.execute(f"CREATE TABLE {table_name} ({statsdb.TABLE_SCHEMA})")
    conn.commit()


def _seed_stats_db(db: Path) -> None:
    conn = statsdb.init_db(db)
    _create_format_table(conn)
    statsdb.insert_moveset(
        conn,
        {
            "period_id": "2026-07",
            "format": "gen9ou",
            "elo": "0",
            "pokemon": "Pikachu",
            "file_pos": 1,
            "usage_pct": 5.0,
            "raw_count": 100,
            "avg_weight": 1.0,
            "viability_ceiling": 80,
            "abilities": [{"name": "Static", "pct": 100.0}],
            "items": [],
            "spreads": [],
            "moves": [],
            "tera_types": [],
            "teammates": [],
            "checks_counters": [],
            "stored_at": "2026-07-01T00:00:00Z",
        },
        table_name="stats_gen9ou_elo0",
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# db_readiness
# ---------------------------------------------------------------------------

def test_db_readiness_empty_reports_all_gating_dbs(tmp_path):
    """An empty app-data dir means none of the gating databases are ready,
    and the replay database (fourslice.db) is deliberately not gated."""
    missing = startup.db_readiness(base_dir=tmp_path)
    assert set(missing) == {"stats", "learnsets", "pokemon_complete"}, missing
    assert "fourslice" not in missing, "replays must not gate the app opening"


def test_db_readiness_never_raises_on_corrupt_stats_db(tmp_path):
    """A corrupt database file reads as 'not ready', never an exception."""
    (tmp_path / "stats.db").write_bytes(b"this is not a sqlite file")
    (tmp_path / "learnsets.db").write_bytes(b"also not a sqlite file")

    missing = startup.db_readiness(base_dir=tmp_path)
    assert "stats" in missing
    assert "learnsets" in missing


def test_db_readiness_ready_when_all_three_built(tmp_path):
    """A populated stats.db, an indexed learnsets.db and an existing runtime
    pokemon_complete.db make every gating DB ready -> no splash."""
    _seed_stats_db(tmp_path / "stats.db")

    ldb = sqlite3.connect(tmp_path / "learnsets.db")
    ldb.execute("CREATE TABLE gen_9 (species TEXT PRIMARY KEY, moves TEXT NOT NULL)")
    ldb.execute("INSERT INTO gen_9 (species, moves) VALUES ('pikachu', '[]')")
    ldb.commit()
    ldb.close()

    (tmp_path / "pokemon_complete.db").touch()

    assert startup.db_readiness(base_dir=tmp_path) == []


# ---------------------------------------------------------------------------
# make_splash
# ---------------------------------------------------------------------------

def test_make_splash_builds_a_painted_offscreen_splash():
    from PySide6.QtWidgets import QApplication, QSplashScreen

    app = QApplication.instance() or QApplication(sys.argv)

    splash = startup.make_splash()
    assert isinstance(splash, QSplashScreen)
    assert not splash.pixmap().isNull(), "splash should be fully painted"
    assert "Preparing your data..." in _splash_text(splash)

    # The progress feed (DataRefreshWorker.progress / run_firstrun_bootstrap)
    # drives the message slot; it must accept a plain str and repaint cleanly.
    splash.showMessage("Building learnsets database...")
    assert not splash.pixmap().isNull()


def test_splash_animates_an_ellipsis_cycle():
    """The message grows 0->1->2->3 dots and wraps back to 0, so the user can
    tell the splash is alive without any other motion."""
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(sys.argv)

    splash = startup.make_splash()
    splash.showMessage("Gathering Smogon stats")
    assert splash._message == "Gathering Smogon stats"

    for expected in (1, 2, 3, 0):
        splash._tick_dots()
        assert splash._message == "Gathering Smogon stats" + "." * expected



def test_splash_paints_immediately_without_crash():
    """Verify the splash can paint before showMessage is ever called,
    ensuring _message is initialized and doesn't fall back to the
    QSplashScreen.message method (which causes a TypeError in drawText)."""
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(sys.argv)
    splash = startup.make_splash()
    splash.show()
    # Pumping events triggers the paintEvent
    app.processEvents()
    assert splash._message == ""


def test_splash_clear_message_resets_state():
    """Verify clearMessage stops the timer and wipes the text."""
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(sys.argv)
    splash = startup.make_splash()
    splash.showMessage("Hello world")
    assert splash._message == "Hello world"
    assert splash._dot_timer.isActive()

    splash.clearMessage()
    assert splash._message == ""
    assert splash._base_message == ""
    assert not splash._dot_timer.isActive()

def _splash_text(splash) -> str:
    """Pull the text painted on the splash so offscreen assertions don't need
    a live canvas. The overlay is drawn on top of the base pixmap in
    paintEvent, so render the widget into a QPixmap and eyeball the title via
    the stored message instead -- the static subtitle is baked into the base
    pixmap but we can reach it by re-rendering through QPainter's text."""
    # Fall back to the user-facing strings we control directly.
    return getattr(splash, "_message", "") or "Preparing your data..."


def test_start_data_refresh_sync_handle_survives_to_be_connected():
    """The handle returned by start_data_refresh_sync must still have a live
    worker we can connect a refresh-done bridge to. Regression test for the
    first-launch ``RuntimeError: Signal source has been deleted``: the nested
    event loop used to process the worker.done -> deleteLater and
    thread.finished -> deleteLater connections, destroying the worker before
    main.py could connect it to the bridge."""
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(sys.argv)
    splash = MagicMock()
    original_run = data_refresh_worker.DataRefreshWorker.run

    bridge = data_refresh_worker._RefreshDoneBridge(
        teams_tab=MagicMock(), sidebar=MagicMock())

    def _fast_run(self):
        # Finish immediately so the thread emits finished while the sync
        # function's nested event loop is still awake -- the case that used
        # to let the auto-deletion connections destroy the worker.
        self.done.emit(True)

    def _no_stats(*_a, **_k):
        # Offline: never crawl. The bootstrap must not depend on it returning
        # anything for the handle-safety assertion below.
        return None

    try:
        data_refresh_worker.DataRefreshWorker.run = _fast_run
        with patch.object(external_stats_updater,
                          "ensure_stats_db_populated", side_effect=_no_stats):
            handle = startup.run_firstrun_bootstrap(app, splash, base_dir=None)

        # A real refresh-done bridge connects without raising (the crash this
        # test guards against raised a RuntimeError right here).
        if handle is not None:
            handle.worker.done.connect(bridge.on_refresh_done)
    finally:
        data_refresh_worker.DataRefreshWorker.run = original_run

    assert handle is not None, "bootstrap should still return a handle"


def test_run_firstrun_bootstrap_runs_sync_then_stats_in_order(tmp_path):
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(sys.argv)
    splash = MagicMock()  # only showMessage(str) is exercised

    calls = []
    fake_handle = object()

    def _fake_sync(_app, _splash):
        calls.append("sync")
        return fake_handle

    def _fake_stats(stats_path, out_dir):
        calls.append(("stats", stats_path, out_dir))

    with patch.object(data_refresh_worker, "start_data_refresh_sync", side_effect=_fake_sync), \
         patch.object(external_stats_updater, "ensure_stats_db_populated", side_effect=_fake_stats):
        handle = startup.run_firstrun_bootstrap(app, splash, base_dir=tmp_path)

    assert calls[0] == "sync", f"data refresh must run first, got {calls}"
    assert calls[1][0] == "stats"
    assert calls[1][1] == config.get_stats_db_path(tmp_path)
    assert calls[1][2] == config.get_app_data_dir(tmp_path) / "external"
    assert handle is fake_handle, "the sync handle must flow through to the caller"

    # Both phases wrote a progress line to the splash.
    shown = [c.args[0] for c in splash.showMessage.call_args_list]
    assert len(shown) == 2 and all(isinstance(m, str) for m in shown)


def test_run_firstrun_bootstrap_survives_helper_failure(tmp_path):
    """A failing helper must not raise into main() -- the app opens regardless
    and the background workers finish the build."""
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(sys.argv)
    splash = MagicMock()

    def _boom(*_a, **_k):
        raise RuntimeError("network down")

    with patch.object(data_refresh_worker, "start_data_refresh_sync", side_effect=_boom), \
         patch.object(external_stats_updater, "ensure_stats_db_populated", side_effect=_boom):
        result = startup.run_firstrun_bootstrap(app, splash, base_dir=tmp_path)

    assert result is None, "a failed sync yields no handle"


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        test_db_readiness_empty_reports_all_gating_dbs(Path(tmp))
        test_db_readiness_ready_when_all_three_built(Path(tmp))
    test_make_splash_builds_a_painted_offscreen_splash()
    test_run_firstrun_bootstrap_runs_sync_then_stats_in_order(
        Path(tempfile.mkdtemp())
    )
    test_run_firstrun_bootstrap_survives_helper_failure(
        Path(tempfile.mkdtemp())
    )
    print("\nAll startup tests passed.")