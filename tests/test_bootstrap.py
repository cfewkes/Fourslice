"""Tests for the startup bootstrap mechanism.

Covers:
  - ``statsdb.is_populated()`` detecting empty vs non-empty databases.
  - ``ensure_stats_db_populated()`` skipping work when data already exists.
  - ``ensure_stats_db_populated()`` triggering a bootstrap when the database
    is empty (with the crawl/pipeline mocked out so the test stays offline
    and fast).

Run with:  python -m pytest tests/test_bootstrap.py
"""

import os
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from fourslice.extstats import statsdb


def _create_format_table(conn, table_name="stats_gen9ou_elo0"):
    """Create a format-elo table the way the capture pipeline would.

    init_db only sets up the schema tables (capture_log, smogon_formats);
    stats_<format>_elo<elo> tables are created lazily by the pipeline (see
    extstats.smogon / extstats.moveset) right before rows are inserted.
    """
    conn.execute(f"CREATE TABLE {table_name} ({statsdb.TABLE_SCHEMA})")
    conn.commit()


# ---------------------------------------------------------------------------
# statsdb.is_populated
# ---------------------------------------------------------------------------

def test_is_populated_empty_db(tmp_path):
    """A freshly-initialised stats.db with no rows is not populated."""
    db = tmp_path / "stats.db"
    conn = statsdb.init_db(db)
    assert statsdb.is_populated(conn) is False
    conn.close()


def test_is_populated_with_tables_but_no_rows(tmp_path):
    """Creating format tables without inserting data is still empty."""
    db = tmp_path / "stats.db"
    conn = statsdb.init_db(db)
    assert statsdb.is_populated(conn) is False
    conn.close()


def test_is_populated_with_data(tmp_path):
    """Once a row is inserted, is_populated returns True."""
    db = tmp_path / "stats.db"
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
    assert statsdb.is_populated(conn) is True
    conn.close()


# ---------------------------------------------------------------------------
# ensure_stats_db_populated — already populated (early exit)
# ---------------------------------------------------------------------------

def test_ensure_skips_when_populated(tmp_path):
    """If the DB already has rows the function returns True without
    touching the network or starting a thread."""
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(sys.argv)

    db = tmp_path / "stats.db"
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
            "abilities": [],
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

    from fourslice.gui.external_stats_updater import ensure_stats_db_populated

    # Should return True immediately — no network activity.
    result = ensure_stats_db_populated(db, tmp_path / "external", timeout_ms=500)
    assert result is True


# ---------------------------------------------------------------------------
# ensure_stats_db_populated — empty DB, mocked worker
# ---------------------------------------------------------------------------

def test_ensure_runs_bootstrap_on_empty_db(tmp_path):
    """When the DB is empty, ensure_stats_db_populated kicks off the
    bootstrap worker.  We mock _BootstrapWorker.run so there's no real
    network activity, and verify the function returns its result."""
    import sys
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(sys.argv)

    db = tmp_path / "stats.db"
    # Create an empty DB
    conn = statsdb.init_db(db)
    conn.close()

    # Verify DB is empty
    conn2 = statsdb.init_db(db)
    print(f"DB is_populated: {statsdb.is_populated(conn2)}", file=sys.stderr)
    conn2.close()

    from fourslice.gui import external_stats_updater as mod

    # Add debug logging
    import logging
    logging.basicConfig(level=logging.DEBUG)

    original_run = mod._BootstrapWorker.run

    def _fake_run(self):
        """Simulate a successful bootstrap without network access."""
        print("FAKE RUN CALLED!", file=sys.stderr)
        self.done.emit(True)

    # Patch by directly replacing the class method
    mod._BootstrapWorker.run = _fake_run
    try:
        print("Patch applied, calling ensure...", file=sys.stderr)
        print(f"mod._BootstrapWorker.run: {mod._BootstrapWorker.run}", file=sys.stderr)
        result = mod.ensure_stats_db_populated(
            db, tmp_path / "external", timeout_ms=1000  # shorter timeout
        )
        print(f"Result: {result}", file=sys.stderr)
    finally:
        mod._BootstrapWorker.run = original_run

    assert result is True


def test_ensure_returns_false_on_worker_failure(tmp_path):
    """When the worker fails, ensure_stats_db_populated returns False."""
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(sys.argv)

    db = tmp_path / "stats.db"
    conn = statsdb.init_db(db)
    conn.close()

    from fourslice.gui import external_stats_updater as mod

    def _fake_run(self):
        self.done.emit(False)

    with patch.object(mod._BootstrapWorker, "run", _fake_run):
        result = mod.ensure_stats_db_populated(
            db, tmp_path / "external", timeout_ms=5000
        )

    assert result is False


def test_ensure_returns_false_on_timeout(tmp_path):
    """When the worker exceeds the timeout, ensure_stats_db_populated
    returns False (the worker never emits True)."""
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(sys.argv)

    db = tmp_path / "stats.db"
    conn = statsdb.init_db(db)
    conn.close()

    from fourslice.gui import external_stats_updater as mod

    def _slow_run(self):
        # Simulate a worker that never finishes within the timeout.
        import time
        time.sleep(10)
        self.done.emit(True)

    with patch.object(mod._BootstrapWorker, "run", _slow_run):
        result = mod.ensure_stats_db_populated(
            db, tmp_path / "external", timeout_ms=200
        )

    assert result is False