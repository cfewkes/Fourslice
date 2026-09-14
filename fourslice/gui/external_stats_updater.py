"""
fourslice/gui/external_stats_updater.py

Background worker for fetching and parsing external stats (Smogon).
Runs on a background thread so the UI doesn't block while waiting for
network responses or heavy parsing.

Also provides ``ensure_stats_db_populated()``, a synchronous bootstrap
function called at startup: if stats.db is empty it crawls Smogon and
populates the database before the main window loads.
"""

import logging
import re
from pathlib import Path

import requests
from PySide6.QtCore import QEventLoop, QObject, QThread, QTimer, Signal, Slot

from fourslice.scraping.smogon_crawler import SmogonCrawler
from fourslice.extstats import pipeline, smogon

log = logging.getLogger(__name__)


def _find_latest_smogon_month() -> str | None:
    """Fetch the Smogon stats index and return the latest month folder (e.g. '2026-07/')."""
    try:
        resp = requests.get(SmogonCrawler.base_url, timeout=10)
        months = re.findall(r'href="(\d{4}-\d{2}/)"', resp.text)
        return sorted(months, reverse=True)[0] if months else None
    except Exception:
        return None


def _is_fully_captured(out_dir: Path, month_id: str) -> bool:
    """True only if stats.db already has every format+elo the mirror
    currently knows about for month_id, captured from the authoritative
    USAGE ladders. Re-checked against the live mirror every time -- a
    month is never trusted as "done" just because an earlier,
    possibly-interrupted run got that far.

    Three source states in capture_log:
      * "usage"      -- fresh, captured from the usage ladder
      * "unavailable" -- parked: the usage ladder is gone from the mirror
                         and can't be re-captured; not pending work
      * anything else
         (NULL/"moveset") -- stale legacy capture; counts as pending so a
                             stats.db filled under the old bug is re-done
    """
    from fourslice import config
    from fourslice.extstats import statsdb

    mirror = out_dir / "smogon"
    try:
        month = smogon.resolve_month(mirror, month_id)
        standings = smogon.format_standings(mirror, month)
    except Exception:
        return False
    if not standings:
        return False

    conn = statsdb.init_db(config.get_stats_db_path())
    try:
        for fmt_info in standings:
            fmt = fmt_info["format"]
            available = {
                elo for _, elo, _ in smogon.ladder_files_for(mirror, month, fmt)
            }
            captured_sources = statsdb.get_captured_sources(conn, month_id, fmt)
            stale = {e for e, s in captured_sources.items() if s not in ("usage", "unavailable")}
            if stale:
                return False  # legacy moveset-source rows still need a re-capture
            if not available:
                continue  # no ladders mirrored; nothing pending unless stale
            usage_captured = {e for e, s in captured_sources.items() if s == "usage"}
            if not available <= usage_captured:
                return False
    finally:
        conn.close()
    return True


class ExternalStatsUpdater(QObject):
    """
    Worker that crawls external stat mirrors and refreshes stats.db.
    """
    progress = Signal(str)  # status message
    formats_updated = Signal() # NEW: signaled when cache has new format data
    finished = Signal(bool) # success/failure

    def __init__(self, out_dir: str | Path):
        super().__init__()
        self.out_dir = Path(out_dir)

    def run(self):
        """
        Execute crawl + refresh pipeline (Stage 1: Top formats)
        followed by background parsing (Stage 2: Remaining formats).

        Re-crawls whenever stats.db isn't already fully caught up with
        what the mirror has for the latest month -- never trusts a bare
        "same month as last time" check, since that's exactly what let a
        one-time interrupted crawl look permanently "done".
        """
        try:
            latest_month = _find_latest_smogon_month()

            if latest_month:
                latest_month_id = latest_month.rstrip('/')
                if _is_fully_captured(self.out_dir, latest_month_id):
                    self.progress.emit(f"Stats already current ({latest_month_id}).")
                    # Still ping the UI: the DB may have been re-captured or
                    # drifted since the sidebar last read it, and this early
                    # return normally emits no formats_updated at all -- the
                    # sidebar would otherwise live on its init-time render.
                    self.formats_updated.emit()
                    self.finished.emit(True)
                    return

            # 1. Scrape Smogon (only latest month)
            self.progress.emit(f"Updating Smogon usage stats ({latest_month or 'latest'})...")
            smogon_crawler = SmogonCrawler(self.out_dir, delay=0.05, resume=True, pipeline_only=True)
            # If we know the latest month, crawl only that subtree
            start_url = SmogonCrawler.base_url + latest_month if latest_month else SmogonCrawler.base_url
            smogon_crawler.crawl(start_url)

        # 2. Scrape Champions - REMOVED
        # self.progress.emit("Updating Champions battle data...")
        # champions = ChampionsCrawler(self.out_dir, delay=0.05, resume=True, include_media=False)
        # champions.crawl(ChampionsCrawler.base_url)

            # 3. Refresh Pipeline (Stage 1: Top Formats)
            self.progress.emit("Processing top formats for UI...")
            smogon_initial_formats = 2
            pipeline.refresh(mirror_root=self.out_dir, smogon_initial_formats=smogon_initial_formats)
            self.formats_updated.emit()
            
            # 4. Background Parsing (Stage 2: Remaining Formats) - use new pipeline function
            self.progress.emit("Parsing remaining formats in background...")
            pipeline.refresh_all_formats(mirror_root=self.out_dir)
            self.formats_updated.emit()
            
            self.progress.emit("External stats updated successfully.")
            self.finished.emit(True)
        except Exception as e:
            log.exception("Failed to update stats")
            self.progress.emit(f"Failed to update stats: {e}")
            self.finished.emit(False)


# ---------------------------------------------------------------------------
# Synchronous startup bootstrap
# ---------------------------------------------------------------------------

_BOOTSTRAP_TIMEOUT_MS = 90_000  # 90 seconds

# QThreads that outlived ensure_stats_db_populated's wait (the worker was
# still crawling when the timeout fired). Destroying a backginned *still
# running* QThread is a fatal Qt error, so we kep reference to these until
# their crawl finishes, instead of letting the local go out of scope.
_bootstrap_orphans: list[tuple] = []


def _forget_bootstrap_thread(thread: QThread, worker: QObject) -> None:
    """Drop our reference to an orphaned bootstrap thread/worker pair once
    its crawl has finished (so it can be garbage collected / deleteLater'd)."""
    for entry in list(_bootstrap_orphans):
        if entry[0] is thread:
            _bootstrap_orphans.remove(entry)
    del worker  # (worker's deleteLater is connected to done already)


class _BootstrapResult(QObject):
    """Holds the outcome of ``ensure_stats_db_populated`` while it waits.

    Lives on the *main* thread. The worker's ``done`` signal is connected to
    ``on_done`` (a proper ``@Slot``), because a plain Python callable has no
    thread affinity: on this PySide6 build a cross-thread emit targeting such
    a callable is delivered to a dispatcher that never runs, so a fast worker
    would look like a timeout forever. A real slot on a QObject that the main
    thread owns gets its queued call processed by the event loop instead.
    """

    def __init__(self, loop: QEventLoop, result: list[bool], finished_normally: list[bool]):
        super().__init__()
        self._loop = loop
        self._result = result
        self._finished_normally = finished_normally

    @Slot(bool)
    def on_done(self, success: bool) -> None:
        log.info(f"Bootstrap worker done: success={success}")
        self._result[0] = success
        self._finished_normally[0] = True
        self._loop.quit()


class _BootstrapWorker(QObject):
    """Internal worker that runs the Smogon crawl + pipeline on a QThread."""

    done = Signal(bool)  # True = data was populated, False = failed/nothing

    def __init__(self, out_dir: Path):
        super().__init__()
        self.out_dir = out_dir

    @Slot()
    def _start(self) -> None:
        """Entry point driven by ``thread.started``; delegates to ``run``.

        Going through a real ``@Slot`` keeps ``run`` executing on the
        worker thread even when it has been replaced by a plain Python
        function (e.g. monkeypatched in tests). Connecting
        ``thread.started`` directly to ``run`` only works while ``run`` is a
        class-body method -- PySide6 gives such a method the object's thread
        affinity and runs it on the worker thread. Once ``run`` is a plain
        function with no affinity, the same connection delivers the call to
        the main thread instead, where it would block the UI and starve the
        wait loop's timer/event delivery.
        """
        self.run()

    def run(self) -> None:
        try:
            log.info("Bootstrap: crawling Smogon for initial stats…")
            smogon_crawler = SmogonCrawler(self.out_dir, delay=0.05, resume=True, pipeline_only=True)

            # Discover the latest month from the index so we only fetch what
            # we need (same logic the full updater uses).
            latest = _find_latest_smogon_month()

            start_url = (
                SmogonCrawler.base_url + latest if latest else SmogonCrawler.base_url
            )
            smogon_crawler.crawl(start_url)

            log.info("Bootstrap: running pipeline (top 2 formats)…")
            pipeline.refresh(
                mirror_root=self.out_dir,
                sources=("smogon",),
                smogon_initial_formats=2,
            )

            self.done.emit(True)
        except Exception:
            log.exception("Bootstrap: crawl/pipeline failed")
            self.done.emit(False)


def ensure_stats_db_populated(
    stats_db_path: Path,
    out_dir: Path,
    timeout_ms: int = _BOOTSTRAP_TIMEOUT_MS,
) -> bool:
    """Block (with event processing) until stats.db has data or *timeout_ms* elapses.

    Called once during startup, before ``MainWindow`` is shown.  If the
    database already contains rows, returns ``True`` immediately.  Otherwise
    a Smogon crawl + pipeline is executed on a helper thread and this
    function waits (while keeping the Qt event loop responsive for the
    splash screen) until the work is done or the timeout fires — whichever
    comes first.

    Returns ``True`` when stats.db ends up populated, ``False`` otherwise
    (timeout, network error, etc.).  The caller should proceed to the main
    window regardless. Either way, once this function returns, a full
    backfill of every remaining format keeps running on its own plain
    background thread -- independent of the timeout above, so an
    unlucky/slow first crawl no longer leaves the app stuck at just the
    top formats until someone happens to click "Update Stats" again.
    """
    from fourslice.extstats import statsdb
    from fourslice.extstats.moveset import run_in_background

    conn = statsdb.init_db(stats_db_path)
    try:
        if statsdb.is_populated(conn):
            log.info("stats.db already populated — skipping bootstrap.")
            return True
    finally:
        conn.close()

    log.info("stats.db is empty — starting synchronous bootstrap…")

    try:
        loop = QEventLoop()
        result: list[bool] = [False]  # mutable holder for closure
        finished_normally: list[bool] = [False]  # did the worker complete before timeout?

        worker = _BootstrapWorker(out_dir)
        result_holder = _BootstrapResult(loop, result, finished_normally)
        thread = QThread()
        worker.moveToThread(thread)

        thread.started.connect(worker._start)
        worker.done.connect(result_holder.on_done)
        worker.done.connect(thread.quit)
        worker.done.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)

        # Safety net: if the worker takes too long, bail out.
        timer = QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(loop.quit)
        timer.start(timeout_ms)

        thread.start()
        # Drive the nested event loop until result_holder.on_done quits it
        # (worker finished) or the timeout timer fires. The loop must be
        # running before quit() is ever called: quitting a not-yet-running
        # QEventLoop is a silent no-op. It also keeps queued painter events
        # from the splash screen flowing for the whole wait, as the docstring
        # promises. No thread.wait() here -- that blocks the main thread and
        # prevents both of those.
        loop.exec()

        timed_out = not finished_normally[0]
        if timed_out:
            try:
                if thread.isRunning():
                    log.warning("Bootstrap timed out after %d ms — proceeding anyway.", timeout_ms)
                    # The worker will finish its top-formats pass in the
                    # background; we just stop waiting on it here. Crucially,
                    # the thread must NOT be destroyed while it is still
                    # running (a fatal Qt error) -- hand it + the worker to
                    # the module-level orphan list until its crawl completes,
                    # at which point the existing thread.finished ->
                    # thread.deleteLater connection cleans them up.
                    _bootstrap_orphans.append((thread, worker))
                    thread.finished.connect(
                        lambda: _forget_bootstrap_thread(thread, worker)
                    )
            except RuntimeError:
                # C++ object already deleted; nothing to do.
                pass

        return result[0]
    except Exception:
        log.exception("Bootstrap failed with exception")
        return False
    finally:
        # Backfill every remaining format on its own plain background
        # thread, independent of the Qt worker/timeout above -- runs
        # whether the top-formats pass above finished in time or not.
        run_in_background(pipeline.refresh_all_formats, mirror_root=out_dir)