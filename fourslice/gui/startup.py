"""
fourslice/gui/startup.py

First-run bootstrap for the databases the app's main views depend on.

On a fresh install (or a wiped app-data dir) the main window used to open
immediately while ``stats.db``, ``learnsets.db`` and ``pokemon_complete.db``
were still being created by background workers -- leaving the window barren
(empty Top Pokemon list, empty Team Builder catalogue) until they landed.
This module gates the window on those three databases being *ready*: when any
is missing or incomplete it shows a splash screen and runs the existing,
already-bounded bootstrap machinery (``start_data_refresh_sync`` for the
Showdown/learnsets/pokemon build, ``ensure_stats_db_populated`` for the Smogon
crawl) before the window is constructed.

Which databases gate the app opening is deliberate:

  * ``stats.db``     -- side bar / external-stats data; empty until crawled.
  * ``learnsets.db`` -- per-generation move legality; empty until indexed.
  * ``pokemon_complete.db`` -- the Team Builder catalogue; missing until built
                               from the seeded ``pokedex.js``.

``fourslice.db`` (the replay database) does NOT gate: replay ingestion crawls
Showdown per configured account and can legitimately take much longer than the
bounded bootsraps above, so it stays a background sync behind the window --
its rows fill in as they land, exactly as before this module existed.
"""

from __future__ import annotations

import logging
import sqlite3

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPixmap
from PySide6.QtWidgets import QSplashScreen

from fourslice import config
from fourslice.extstats import statsdb

log = logging.getLogger(__name__)

# The databases whose completeness gates opening the main window. Replays
# (fourslice.db) deliberately stays out -- see the module docstring.
GATED_DBS = ("stats", "learnsets", "pokemon_complete")


# ---------------------------------------------------------------------------
# Database readiness -- "can the app open yet?"
# ---------------------------------------------------------------------------

def _db_has_rows(path, query: str) -> bool:
    """True when ``path`` is a readable SQLite file whose ``query`` returns a
    nonzero COUNT. Any failure (missing file/table, corrupt DB) is False --
    the caller can never raise on a broken database."""
    if not path.is_file():
        return False
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            return conn.execute(query).fetchone()[0] > 0
        finally:
            conn.close()
    except Exception:
        return False


def db_readiness(base_dir=None) -> list[str]:
    """Names of the gating databases that are missing or incomplete.

    An empty list means every gating DB is ready and the app may open
    immediately (the fast path for returning users). ``base_dir`` threads
    through to the config/learnsets/statsdb helpers exactly like their own
    ``base_dir`` parameters, so tests can point this at a temp app-data dir.
    Never raises -- a broken DB just reads as "not ready". Safe offline.
    """
    missing: list[str] = []

    # stats.db: any stats_* table with at least one row (statsdb.is_populated).
    # init_db also creates the schema file, matching what main() already does.
    try:
        conn = statsdb.init_db(str(config.get_stats_db_path(base_dir)))
        try:
            if not statsdb.is_populated(conn):
                missing.append("stats")
        finally:
            conn.close()
    except Exception:
        missing.append("stats")

    # learnsets.db: the base per-generation tables must actually hold data
    # (a schema-only DB from a previous interrupted launch is not ready).
    if not _db_has_rows(
        config.get_learnsets_db_path(base_dir),
        "SELECT COUNT(*) FROM gen_9",
    ):
        missing.append("learnsets")

    # pokemon_complete.db: the runime catalogue DB (rebuilt by the data
    # refresh from the seeded pokedex.js when stale) must exist. Checked at
    # its runtime path, not via get_pokemon_complete_db_path, so the result
    # is hermetic under a test base_dir (that resolver would otherwise fall
    # through to the real app-data dir when base_dir has no DB yet).
    if not config.get_pokemon_db_path(base_dir).is_file():
        missing.append("pokemon_complete")

    return missing


# ---------------------------------------------------------------------------
# Splash screen -- painted in the app palette, self-drawn progress text
# ---------------------------------------------------------------------------

_SPLASH_W = 480
_SPLASH_H = 300


class _SplashScreen(QSplashScreen):
    """A QSplashScreen that paints the Fourslice title + progress message in
    the app's palette, with an animated ellipsis so the user can tell the app
    is not frozen.

    ``showMessage`` is overridden to store the incoming text and repaint,
    so the progress lines fed by ``DataRefreshWorker.progress`` render with
    our typography instead of Qt's default boxed message on a foreign
    background. A QTimer cycles the trailing ellipsis through 0→1→2→3→0
    dots every 400 ms while any message is displayed.
    """

    def __init__(self):
        from PySide6.QtCore import QTimer
        pixmap = _make_splash_pixmap()
        super().__init__(pixmap)
        self._base_message = ""
        self._dot_phase = 0
        self._message = ""

        self._dot_timer = QTimer(self)
        self._dot_timer.setInterval(400)
        self._dot_timer.timeout.connect(self._tick_dots)

    def showMessage(self, message: str, alignment: Qt.Alignment | None = None,
                    color: QColor | None = None) -> None:
        self._base_message = message
        self._dot_phase = 0
        if message and not self._dot_timer.isActive():
            self._dot_timer.start()
        elif not message:
            self._dot_timer.stop()
        self._repaint_message()

    def clearMessage(self) -> None:
        """Override QSplashScreen.clearMessage to reset internal state."""
        self._base_message = ""
        self._dot_phase = 0
        self._dot_timer.stop()
        self._message = ""
        self.update()

    def _tick_dots(self) -> None:
        self._dot_phase = (self._dot_phase + 1) % 4
        self._repaint_message()

    def _repaint_message(self) -> None:
        self._message = self._base_message + "." * self._dot_phase
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)  # paints the base pixmap
        p = QPainter(self)
        try:
            from fourslice.gui.imports_page import THEME
            tokens = THEME.get_tokens()
            font = QFont("Segoe UI", 12)
            font.setWeight(QFont.Weight.DemiBold)
            p.setFont(font)
            p.setPen(QColor(tokens["MUTED"]))
            msg_rect = self.rect().adjusted(36, 0, -28, -32)
            p.drawText(
                msg_rect,
                Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignLeft
                | Qt.TextFlag.TextWordWrap,
                self._message,
            )
        finally:
            p.end()


def _make_splash_pixmap():
    """Draw the splash backdrop: theme background, the FourSlice logo, the
    Fourslice title and a muted subtitle. The bottom band stays clear for the
    progress message painted by _SplashScreen.paintEvent."""
    from pathlib import Path
    from PySide6.QtGui import QPixmap as QPix, QTransform

    from fourslice.gui.imports_page import THEME
    tokens = THEME.get_tokens()

    pm = QPixmap(_SPLASH_W, _SPLASH_H)
    pm.fill(QColor(tokens["WHITE"]))

    p = QPainter(pm)
    try:
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Logo – scaled to fit a 34×34 box, centered in the accent slot.
        from fourslice.gui.imports_page import ASSETS_DIR
        logo_path = ASSETS_DIR / "FourSliceLogo.png"
        if logo_path.is_file():
            raw_logo = QPix(str(logo_path))
            logo = raw_logo.scaled(
                34, 34,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            # Center in the 34×34 box at (36, 52).
            lx = 36 + (34 - logo.width()) // 2
            ly = 52 + (34 - logo.height()) // 2
            p.drawPixmap(lx, ly, logo)
        else:
            # Fallback: violet block with F (same as before).
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(tokens["VIOLET"]))
            p.drawRoundedRect(36, 52, 34, 34, 9, 9)
            p.setPen(QColor(tokens["WHITE"]))
            fb_font = QFont("Segoe UI", 17)
            fb_font.setWeight(QFont.Weight.Bold)
            p.setFont(fb_font)
            p.drawText(QRect(36, 52, 34, 34),
                       Qt.AlignmentFlag.AlignCenter, "F")

        # Title + subtitle.
        title_font = QFont("Segoe UI", 30)
        title_font.setWeight(QFont.Weight.Bold)
        p.setFont(title_font)
        p.setPen(QColor(tokens["INK"]))
        p.drawText(QRect(36, 118, _SPLASH_W - 72, 44),
                   Qt.AlignmentFlag.AlignLeft, "Fourslice")

        p.setFont(QFont("Segoe UI", 12))
        p.setPen(QColor(tokens["MUTED"]))
        p.drawText(QRect(36, 164, _SPLASH_W - 72, 24),
                   Qt.AlignmentFlag.AlignLeft, "Preparing your data...")
    finally:
        p.end()
    return pm


def make_splash() -> QSplashScreen:
    """Build the startup splash screen (not yet shown)."""
    return _SplashScreen()


# ---------------------------------------------------------------------------
# Bounded bootstrap -- drive the existing helpers, keep the splash informed
# ---------------------------------------------------------------------------

def run_firstrun_bootstrap(app, splash, base_dir=None):
    """Populate the gating databases before the main window opens.

    Runs the two existing, already-bounded helpers sequentially -- each keeps
    Qt's event loop pumping so the splash stays responsive -- then returns the
    ``DataRefreshHandle`` from the data-refresh sync (``None`` if there is
    none) so the caller can wire a refresh-done bridge even when the sync
    timed out and its worker is still finishing in the background.

    * ``start_data_refresh_sync`` builds pokemon_complete.db + learnsets.db
      (local, fast; the shiped files seed the runtime dir) and refreshes stale
      Showdown data files -- bounded by its own 60s timeout.
    * ``ensure_stats_db_populated`` crawls Smogon when stats.db is empty --
      bounded by its own 90s timeout, and a no-op network-wise when stats are
      already present.

    Best-effort throughout: a failure is logged and the app still opens
    (whatever got built stays; the background workers finish the rest).
    """
    from fourslice.gui.data_refresh_worker import start_data_refresh_sync
    from fourslice.gui.external_stats_updater import ensure_stats_db_populated

    splash.showMessage("Preparing Pokemon data")
    data_handle = None
    try:
        data_handle = start_data_refresh_sync(app, splash)
    except Exception:
        log.exception("start_data_refresh_sync during startup bootstrap")

    splash.showMessage("Gathering Smogon stats (first crawl can take a minute)")
    try:
        ensure_stats_db_populated(
            config.get_stats_db_path(base_dir),
            config.get_app_data_dir(base_dir) / "external",
        )
    except Exception:
        log.exception("ensure_stats_db_populated during startup bootstrap")

    return data_handle