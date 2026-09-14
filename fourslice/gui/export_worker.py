"""
fourslice/gui/export_worker.py

Runs "Export for Power BI" on a background QThread so that writing the
13 CSVs (the 8 pre-computed stat tables can take a while on a large
replay history) never freezes the window.

Opens its OWN database connection rather than sharing the GUI thread's
-- sqlite3 connections aren't safe to use across threads.

Cancellation is cooperative: export_all_csvs reports each file the
moment it lands on disk (progress_cb), and run() checks the cancel
flag at exactly that boundary, so a cancel takes effect between files
-- every CSV written so far stays on disk and is complete.
"""

import threading
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from fourslice.export import EXPORT_FILE_COUNT, export_all_csvs
from fourslice.storage import init_db


class _ExportCancelled(Exception):
    """Raised at a file boundary when the user hits Cancel."""


class ExportWorker(QObject):
    progress = Signal(int, int, str)    # (file_index, EXPORT_FILE_COUNT, "name (N rows)")
    finished = Signal(list, int, bool)  # (written paths, total rows, cancelled)
    error = Signal(str)

    def __init__(self, db_path: str, folder: str):
        super().__init__()
        self.db_path = db_path
        self.folder = folder
        self._cancel_event = threading.Event()

    def cancel(self):
        """Request a cooperative stop at the next file boundary. Safe to
        call from the GUI thread while run() is executing."""
        self._cancel_event.set()

    def run(self):
        """
        Write all 13 export CSVs into self.folder on this worker's
        thread. Emits progress once per file, then finished(written,
        total_rows, cancelled). Any failure emits error(str) instead
        of finished.
        """
        try:
            conn = init_db(self.db_path)
        except Exception as exc:
            self.error.emit(str(exc))
            return

        written: list[str] = []
        total_rows = 0

        def _on_file_written(name, row_count):
            nonlocal total_rows
            total_rows += row_count
            written.append(str(Path(self.folder) / f"{name}.csv"))
            self.progress.emit(
                len(written), EXPORT_FILE_COUNT,
                f"{name} ({row_count:,} rows)",
            )
            if self._cancel_event.is_set():
                raise _ExportCancelled()

        try:
            all_written = export_all_csvs(
                conn, self.folder, progress_cb=_on_file_written)
        except _ExportCancelled:
            conn.close()
            self.finished.emit(written, total_rows, True)
        except Exception as exc:
            conn.close()
            self.error.emit(str(exc))
        else:
            conn.close()
            self.finished.emit([str(p) for p in all_written], total_rows, False)