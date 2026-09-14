"""
fourslice/gui/data_refresh_worker.py

A background worker that refreshes Showdown data files and Champions learnsets
so the main thread (and the splash screen) stays responsive. Best-effort:
network failures are logged and ignored — the worker never raises into the UI
and the app continues with whatever is on disk.
"""

import logging
from dataclasses import dataclass

from PySide6.QtCore import QObject, QThread, Signal, Slot

log = logging.getLogger(__name__)

_data_refresh_orphans: list[tuple] = []


class DataRefreshWorker(QObject):
    progress = Signal(str)
    done = Signal(bool)

    def __init__(self):
        super().__init__()

    @Slot()
    def _start(self) -> None:
        self.run()

    def run(self) -> None:
        try:
            from fourslice import teambuilder_data as td
            from fourslice import learnsets
            from fourslice.pokemon_db import ensure_pokemon_db

            self.progress.emit("Refreshing Showdown data files…")
            try:
                td.refresh_data_files_if_stale(max_age_days=7)
            except Exception as exc:
                log.warning("DataRefreshWorker: refresh_data_files_if_stale failed: %s", exc)

            # Rebuild the Team Builder catalogue from the (possibly refreshed)
            # pokedex.js so brand-new Pokemon appear without an app update.
            self.progress.emit("Refreshing Pokemon catalogue…")
            try:
                ensure_pokemon_db()
            except Exception as exc:
                log.warning("DataRefreshWorker: ensure_pokemon_db failed: %s", exc)

            self.progress.emit("Refreshing base learnsets…")
            try:
                learnsets.ensure_base_indexed(max_age_days=7)
            except Exception as exc:
                log.warning("DataRefreshWorker: ensure_base_indexed failed: %s", exc)

            self.progress.emit("Refreshing Champions learnsets…")
            try:
                learnsets.ensure_champions_indexed(max_age_days=7)
            except Exception as exc:
                log.warning("DataRefreshWorker: ensure_champions_indexed failed: %s", exc)

            self.done.emit(True)
        except Exception:
            log.exception("DataRefreshWorker: unexpected exception")
            try:
                self.done.emit(False)
            except Exception:
                pass


@dataclass
class DataRefreshHandle:
    """Holds live references to a running ``DataRefreshWorker`` and its
    ``QThread`` so the pair is never garbage-collected (and thus destroyed
    while the thread is still running, which is a fatal Qt error) before
    the worker has finished. Drop the handle any time after ``thread`` has
    stopped; the module-level orphan list keeps the pair alive meanwhile
    and hands them to ``deleteLater`` on ``finished``."""

    thread: QThread
    worker: DataRefreshWorker


def start_data_refresh_async() -> DataRefreshHandle:
    """Start a ``DataRefreshWorker`` on a background ``QThread`` and return
    immediately — no waiting, no event loop.

    The worker's ``progress``/``done`` signals are emitted from the worker
    thread; connect ``done`` to any slot on a main-thread ``QObject`` (e.g.
    ``_RefreshDoneBridge.on_refresh_done``) and Qt's auto-connection will
    marshal the call into the main thread's event loop.

    The returned handle must be kept alive while the worker runs. If the
    caller drops it first, the module-level ``_data_refresh_orphans`` list
    keeps the thread/worker referenced (with ``thread.finished`` cleanup)
    so a still-running ``QThread`` is never destroyed.
    """
    worker = DataRefreshWorker()
    thread = QThread()
    worker.moveToThread(thread)
    thread.started.connect(worker._start)
    worker.done.connect(thread.quit)
    worker.done.connect(worker.deleteLater)
    thread.finished.connect(thread.deleteLater)

    # Safety net: keep the pair referenced even if the handle is dropped
    # mid-run (destroying a *running* QThread is a fatal Qt error). The
    # entry is removed once the thread actually finishes, at which point
    # the existing finished -> deleteLater connection cleans up.
    _data_refresh_orphans.append((thread, worker))

    def _forget() -> None:
        try:
            _data_refresh_orphans.remove((thread, worker))
        except ValueError:
            pass

    thread.finished.connect(_forget)

    thread.start()
    return DataRefreshHandle(thread=thread, worker=worker)


def start_data_refresh_sync(
    app,
    splash=None,
    timeout_ms: int = 60_000,
) -> DataRefreshHandle | None:
    """Run a DataRefreshWorker synchronously on a helper thread.

    ``app`` is the ``QApplication`` whose event loop drives the nested
    ``QEventLoop``. ``splash`` is the ``QSplashScreen`` whose message is
    fed by ``progress`` if provided.

    Behavious is the same as ``ensure_stats_db_populated``'s internal
    bootstrap waiter: if the worker finishes normally we proceed; if the
    timeout fires first we orphan the still-running QThread so Qt is not
    asked to destroy a running thread (fatal abort on exit). Reuses
    ``start_data_refresh_async`` for the thread setup.

    Returns the live ``DataRefreshHandle`` so the caller can keep wiring
    a refresh-done bridge even in the timeout case (where the orphaned
    worker is still finishing in the background); callers that only run
    the sync to completion can ignore the return value.
    """
    from PySide6.QtCore import QEventLoop, QTimer

    loop = QEventLoop()
    finished: list[bool] = [False]

    class _Result(QObject):
        @Slot(bool)
        def on_done(self, _ok: bool) -> None:
            if not finished[0]:
                finished[0] = True
                loop.quit()

    result = _Result(loop)
    handle = start_data_refresh_async()

    # Disconnect the auto-deletion connections so the worker and thread stay
    # alive after this function returns. Without this, the nested event loop
    # below would process the deleteLater events queued by worker.done /
    # thread.finished, destroying the worker before the caller can connect
    # handle.worker.done to a refresh-done bridge.
    try:
        handle.worker.done.disconnect(handle.worker.deleteLater)
    except (TypeError, RuntimeError):
        pass
    try:
        handle.thread.finished.disconnect(handle.thread.deleteLater)
    except (TypeError, RuntimeError):
        pass

    handle.worker.done.connect(result.on_done)
    if splash is not None:
        handle.worker.progress.connect(lambda msg: splash.showMessage(msg))  # type: ignore[arg-type]

    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(loop.quit)
    timer.start(timeout_ms)

    app.processEvents()
    loop.exec()

    if timer.isActive():
        timer.stop()
    # else: timed out — start_data_refresh_async already registered the
    # thread/worker in the orphan list, so it finishes in the background.
    # No extra bookkeeping needed here.

    return handle


class _RefreshDoneBridge(QObject):
    """Main-thread bridge from ``DataRefreshWorker.done`` to the UI.

    The worker emits ``done`` from its own thread. PySide6 queues a
    cross-thread emit into the *receiver thread's* event loop only when the
    target is a real ``@Slot`` on a ``QObject`` livd on that thread — a
    plain callable would be delivered to a dispatcher that never runs (the
    same trap documented on ``_BootstrapResult`` in
    ``external_stats_updater.py``). Keep this object on the main thread and
    connect ``handle.worker.done`` to it; the slot then safely touches
    widgets.
    """

    def __init__(self, teams_tab, sidebar, parent=None):
        super().__init__(parent)
        self._teams_tab = teams_tab
        self._sidebar = sidebar

    @Slot(bool)
    def on_refresh_done(self, success: bool) -> None:
        if not success:
            return
        # The Team Builder rebuilt pokemon_complete.db / learnsets: drop the
        # sidebar's stale name->id map and instance sprite cache so sprites
        # for any newly-added Pokemon appear on the next list render.
        from fourslice.gui import sidebar as sidebar_mod

        sidebar_mod.reset_pokemon_id_map()
        self._sidebar._sprite_cache.clear()
        self._teams_tab.reload_catalog()
