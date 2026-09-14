"""
Entry point for the Fourslice desktop app.
"""

import os
import sys
import traceback
from pathlib import Path

from PySide6.QtCore import QThread
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMessageBox

from fourslice import config
from fourslice.storage import init_db, prune_old_logs
from fourslice.gui.main_window import MainWindow
from fourslice.gui.import_tab import prompt_for_usernames
from fourslice.gui.external_stats_updater import ExternalStatsUpdater
from fourslice.gui.data_refresh_worker import (
    start_data_refresh_async,
    _RefreshDoneBridge,
)
from fourslice.gui import startup
from fourslice.gui.imports_page import ASSETS_DIR
from fourslice.extstats import statsdb


def _setup_crash_handler():
    """Install a global exception hook to write crash logs and inform the user."""
    def excepthook(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return

        tb_lines = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        log_dir = config.get_app_data_dir() / "logs"
        crash_path = None
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            crash_path = log_dir / "crash.log"
            with open(crash_path, "a", encoding="utf-8") as f:
                import datetime
                f.write(f"\n--- Crash at {datetime.datetime.now().isoformat()} ---\n")
                f.write(tb_lines)
        except Exception:
            pass

        msg = f"An unexpected error occurred in Fourslice:\n\n{exc_value}\n"
        if crash_path:
            msg += f"\nDetailed crash log saved to:\n{crash_path}"

        app = QApplication.instance()
        if app is not None:
            try:
                QMessageBox.critical(None, "Fourslice - Unexpected Error", msg)
            except Exception:
                pass
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    sys.excepthook = excepthook


def _setup_windows_app_id():
    """Set explicit AppUserModelID on Windows so taskbar pin/grouping and icon work properly."""
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("fourslice.vgc.analytics.1.0")
        except Exception:
            pass


def _get_app_icon() -> QIcon:
    ico = ASSETS_DIR / "FourSliceLogo.ico"
    if ico.is_file():
        return QIcon(str(ico))
    png = ASSETS_DIR / "FourSliceLogo.png"
    if png.is_file():
        return QIcon(str(png))
    return QIcon()


def main():
    _setup_crash_handler()
    _setup_windows_app_id()

    app = QApplication(sys.argv)
    app_icon = _get_app_icon()
    if not app_icon.isNull():
        app.setWindowIcon(app_icon)

    usernames = config.get_usernames()
    if not usernames:
        usernames = prompt_for_usernames()
        if not usernames:
            sys.exit(0)  # user cancelled setup -- exit cleanly, don't guess
        config.set_usernames(usernames)

    conn = init_db(str(config.get_db_path()))
    prune_old_logs(conn, config.get_log_retention_days())
    statsdb.init_db(config.get_stats_db_path())

    # On a fresh install (or wiped app-data dir) the gating databases --
    # stats, learnsets, pokemon_complete -- don't exist yet. Opening the
    # window right away would leave it barren until the background workers
    # finish, so instead a splash screen runs a bounded bootstrap (the
    # shipped Showdown/learnsets files build locally in seconds; the Smogon
    # crawl is capped at 90s) before the window is constructed. Returning
    # users have all three ready and skip the splash entirely. Replay data
    # (fourslice.db) deliberately does NOT gate: per-account ingestion can
    # be long, so it stays a background sync that fills the window in.
    refresh_handle = None
    missing = startup.db_readiness()
    if missing:
        splash = startup.make_splash()
        splash.show()
        app.processEvents()
        # start_data_refresh_sync returns its handle so that, if its 60s
        # timeout fires first (e.g. a slow netless first fetch), the still-
        # running worker can still reach a refresh-done bridge after the
        # window opens and reload the Team Builder when it lands.
        refresh_handle = startup.run_firstrun_bootstrap(app, splash)
        splash.close()

    window = MainWindow(usernames, conn)
    window.showMaximized()

    # Showdown data files + Champions learnsets refresh, on its own thread
    # so the UI never blocks. When done, the bridge tells the Team Builder
    # to reload its catalogue and the sidebar to drop stale sprite caches.
    # The handle keeps the thread/worker alive (the module orphan list also
    # holds them until the thread finishes, so dropping it is safe).
    if refresh_handle is None:
        # Fast path (all DBs ready): start the refresh async, as before.
        refresh_handle = start_data_refresh_async()
    bridge = _RefreshDoneBridge(
        window.teams_tab,
        window.layout_widget.sidebar,
        parent=window,
    )
    # If the bootstrap sync already finished (e.g. local build completed before
    # the window was constructed), worker.done fired before we could connect.
    # Re-emit the result by calling the slot directly. (refresh_handle.thread
    # is the QThread stored on the handle; worker.thread() would be a method.)
    if refresh_handle.thread.isFinished():
        bridge.on_refresh_done(True)
    else:
        refresh_handle.worker.done.connect(bridge.on_refresh_done)

    # Kick off the Showdown replay sync right after the window is shown so
    # new replays appear without pressing Sync. It runs on its own QThread
    # inside the page (see ImportsPage.start_sync / bulk_import_worker.py)
    # and stops at the first already-known replay, so a repeat launch only
    # crawls what's actually new. Not started from ImportsPage.__init__ so
    # constructing the page in tests never touches the network.
    window.imports_page.start_sync()

    # Run external stats refresh in background
    updater_thread = QThread()
    updater = ExternalStatsUpdater(config.get_app_data_dir() / "external")
    updater.moveToThread(updater_thread)
    updater_thread.started.connect(updater.run)
    updater.finished.connect(updater_thread.quit)
    updater.finished.connect(updater.deleteLater)
    updater_thread.finished.connect(updater_thread.deleteLater)
    # Connect the refresh signals BEFORE the thread starts: the worker can
    # emit formats_updated (or finish) during its first event-loop cycle,
    # and a connect made after start() can miss it -- leaving the sidebar
    # stuck on whatever it rendered at init instead of the re-captured data.
    # refresh_formats is a main-thread widget slot, so these are auto-queued
    # into its event loop; finished emits bool, which the 0-arg slot drops.
    updater.formats_updated.connect(window.layout_widget.sidebar.refresh_formats)
    updater.finished.connect(window.layout_widget.sidebar.refresh_formats)
    updater_thread.start()

    # The external-stats DB (config.get_stats_db_path()) is persistent: it
    # stores parsed usage & moveset stats so they survive across launches.
    rc = app.exec()
    sys.exit(rc)


if __name__ == "__main__":
    main()