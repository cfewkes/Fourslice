"""
fourslice/gui/sprite_fetcher.py

Downloads gen-5 sprites on a background QThread so the Replays tab's
UI thread never blocks on the network. Same pattern as
bulk_import_worker.py: a plain QObject moved onto a QThread and wired
up by whoever owns it.

The worker is a long-lived service: the Replays tab creates ONE thread
and ONE worker for the tab's lifetime, and posts (species, back) batches
to it via the `request` signal (queued, so `fetch` always runs on the
worker thread). This is deliberate -- the previous design recreated a
QThread per fetch, and when a game selection arrived while the old
thread was still blocked on the network, the abandoned QThread was
destroyed while its OS thread was still running, which is a hard Qt
fatal error ("QThread: Destroyed while thread is still running").
A single reused thread can't hit that.

The actual resolution -- including the gen5ani -> gen5 -> ani fallback
chain and on-disk caching -- lives in fourslice.sprites.SpriteStore;
this worker just walks its request list off the UI thread. sprite_ready
fires once per species that actually downloaded, with everything the
Replays tab needs to swap the new art into whichever slots currently
show it. request_stop() makes an in-flight loop bail between species so
app shutdown never waits on a long queue.
"""

from PySide6.QtCore import QObject, Signal, Slot


class SpriteFetchWorker(QObject):
    request = Signal(object)            # a batch of (species, back) pairs
    sprite_ready = Signal(str, bool, bool)  # (species, back, animated)

    def __init__(self, store):
        super().__init__()
        self.store = store
        self._stop = False

    @Slot(object)
    def fetch(self, requests):
        """Walks one batch off the UI thread. Queued via `request`, so
        overlapping batches simply drain in order; the store's disk cache
        and per-session failure memory make re-walks cheap."""
        for species, back in requests:
            if self._stop:
                break
            path, animated = self.store.get_or_fetch(species, back)
            if path is not None:
                self.sprite_ready.emit(species, back, animated)

    def request_stop(self):
        """Tells a mid-fetch loop to bail at the next species. Safe to
        call from any thread (a plain attribute write, GIL-atomic)."""
        self._stop = True
