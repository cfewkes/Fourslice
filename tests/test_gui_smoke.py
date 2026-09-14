"""
Run directly with: python tests/test_gui_smoke.py

Headless smoke tests for the GUI layer -- confirms the import tab,
stats tab, teams tab, replays tab, settings dialog, and their assembly
into the main window construct without crashing, and that a real failed
network fetch is caught and shown instead of crashing the app. Forces
Qt's "offscreen" platform so this never pops up a real window or needs a
display (not how you should run the real app -- just run main.py
normally for that).

NOT covered here: the interactive first-run/change-username dialog.
It blocks waiting for real keyboard input, so there's no way to drive
it headlessly. Test that one by hand: click "Change username(s)" in
the running app, or delete config.get_config_path() and relaunch.

The network-failure test below is a REAL failure, not a mock: this
sandbox's network is restricted to an allowlist that doesn't include
pokemonshowdown.com, so the fetch genuinely fails every time this
runs here. On your machine, with normal internet access, this exact
test would likely fail differently (probably a 404, since the URL is
fake) -- either way it's exercising the same try/except path.
"""

import base64
import gc
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from fourslice.stats import REGISTRY

from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtGui import QShowEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QGraphicsOpacityEffect, QLabel

from fourslice import config
from fourslice.gui import sidebar as sidebar_module
from fourslice.gui.sidebar import Sidebar, _POKEMON_ID_MAP
from fourslice.gui import imports_page as page_module
from fourslice.gui.imports_page import ImportsPage, THEME
from fourslice.gui.main_window import MainWindow
from fourslice.gui.import_tab import ImportTab
from fourslice.gui.replays_tab import ReplaysTab, _SUB_OPACITY
from fourslice.gui.settings_dialog import SettingsDialog
from fourslice.gui.stats_tab import StatsTab
from fourslice.gui.stats_widgets import LeadCard, MonCard, RecordLabel
from fourslice.sprites import SpriteStore
from fourslice.storage import init_db, import_replay, resolve_team_id
from fourslice.gui.teams_tab import TeamsTab

SAMPLE_DIR = Path(__file__).resolve().parent / "sample_logs"


def _replays_row_for(tab, game_id):
    """Row index of a stored game in a ReplaysTab's list regardless of the
    list's sort order (battle number, not import order)."""
    for i in range(tab.game_list.count()):
        if tab.game_list.item(i).data(Qt.UserRole) == game_id:
            return i
    raise AssertionError(f"game not in the list: {game_id}")

# A real 1x1 PNG so cached "sprites" actually decode into a QPixmap.
_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def load_log(filename):
    with open(SAMPLE_DIR / filename) as f:
        return json.load(f)["log"]


# The emoji set the condition chips used to render (item C). The chips
# must not contain any of these anymore.
_EMOJI_CHARS = set("☀🌧💨❄⚡🌱🌸🔮🌀🧲✨🌫🌪🛡🔆🌈⚠🪨☠🕸")


def _contains_emoji(text):
    return any(ch in _EMOJI_CHARS for ch in text)


def test_import_tab_constructs():
    app = QApplication.instance() or QApplication(sys.argv)
    conn = init_db(":memory:")
    tab = ImportTab(["salmoncashew"], conn)

    assert tab.url_input is not None
    assert tab.import_button is not None
    assert tab.recent_list is not None
    assert tab.regulation_filter is not None
    assert tab.regulation_filter.itemText(0) == "All regulations"
    assert tab.bulk_import_button is not None
    assert tab.bulk_progress is not None
    print("PASS: import tab constructs with all expected widgets")


def test_import_handles_fetch_failure_without_crashing():
    """
    A failed single-replay fetch is caught on the worker and shown in the
    list -- the error is not raised into the UI thread. The fetch is driven
    to fail OFFLINE (the worker's network call is stubbed out): the old
    version hit the live replay.pokemonshowdown.com with a bogus URL, which
    was only guaranteed-fast in a network-restricted sandbox; on a real
    connection it hangs for the full 3x15s retry window and this test
    becomes a multi-minute network gamble. The failure path exercised on the
    worker (error.emit -> thread teardown) is identical either way.
    """
    from fourslice.gui.import_tab import SingleImportWorker

    app = QApplication.instance() or QApplication(sys.argv)
    conn = init_db(":memory:")
    tab = ImportTab(["salmoncashew"], conn)

    original_run = SingleImportWorker.run

    def fail_fast(self):
        self.error.emit("connection refused (offline test)")

    try:
        SingleImportWorker.run = fail_fast  # no network in tests

        tab.url_input.setText("https://replay.pokemonshowdown.com/this-will-fail-here")
        tab.import_one_replay()  # should not raise
        # "Importing..." is recorded synchronously by import_one_replay itself.
        assert tab.recent_list.count() >= 1

        # The failure comes back through the queued error signal: pump the
        # event loop until the worker's thread is gone (thread.quit is wired
        # to worker.error too, exactly as for finished), then the error line
        # must be on top and the button re-enabled.
        deadline = time.monotonic() + 10
        while tab.single_thread is not None and time.monotonic() < deadline:
            QTest.qWait(20)
        assert tab.single_thread is None, "the single-import thread should have finished and been cleaned up"

        first_item = tab.recent_list.item(0).text()
        assert "Failed to import" in first_item, f"got {first_item!r}"
        assert tab.import_button.isEnabled(), "the Import button should be re-enabled after the failure"
        print("PASS: a failed fetch is caught and shown, not raised")
    finally:
        SingleImportWorker.run = original_run


def test_imports_page_sync_wires_background_worker_and_reenables_button():
    app = QApplication.instance() or QApplication(sys.argv)
    page = ImportsPage(usernames=["SalmonCashew"])

    captured = {}

    class FakeBulkWorker(QObject):
        progress = Signal(int, int, str)
        finished = Signal(dict)

        def __init__(self, db_path, usernames, my_usernames):
            super().__init__()
            captured["db_path"] = db_path
            captured["usernames"] = usernames
            captured["my_usernames"] = my_usernames

        def run(self):
            pass  # no network in tests -- start_sync wiring is what we're checking

    original_worker = page_module.BulkImportWorker
    page_module.BulkImportWorker = FakeBulkWorker

    # Keep the offscreen run off the real config file: stub the
    # last-synced bookkeeping the page writes when a sync finishes.
    original_set_last_synced = page_module.config.set_last_synced
    original_get_last_synced = page_module.config.get_last_synced
    set_calls = []
    page_module.config.set_last_synced = lambda ts: set_calls.append(ts)
    page_module.config.get_last_synced = lambda: set_calls[-1] if set_calls else None

    thread = None
    try:
        page.start_sync()

        assert page.bulk_thread is not None, "start_sync should create a QThread"
        thread = page.bulk_thread
        assert thread.isRunning(), "sync should run on a background QThread"
        assert isinstance(page.bulk_worker, FakeBulkWorker)
        assert captured["db_path"] == str(config.get_db_path())
        assert captured["usernames"] == ["SalmonCashew"]
        assert captured["my_usernames"] == ["SalmonCashew"]
        assert not page.sync_btn.isEnabled(), "Sync button stays disabled while syncing"
        assert page.sync_btn.text() == "Syncing…"

        # Worker reports done -> button re-enables, refs clear, last-synced stamps.
        page.bulk_worker.finished.emit({"imported": 0})

        for _ in range(200):
            app.processEvents()
            if page.bulk_thread is None:
                break

        assert page.sync_btn.isEnabled(), "Sync button re-enables once the sync finishes"
        assert page.sync_btn.text() == "Sync"
        assert page.bulk_thread is None, "thread/worker refs are cleared after finish"
        assert page.bulk_worker is None, "thread/worker refs are cleared after finish"
        assert set_calls, "finishing a sync stamps config.set_last_synced"
        print("PASS: Sync wires a background BulkImportWorker and cleanly re-enables")
    finally:
        if thread is not None:
            try:
                thread.quit()
                thread.wait(2000)
            except RuntimeError:
                pass  # thread object already deleted
        page_module.BulkImportWorker = original_worker
        page_module.config.set_last_synced = original_set_last_synced
        page_module.config.get_last_synced = original_get_last_synced


def test_stats_tab_constructs():
    app = QApplication.instance() or QApplication(sys.argv)
    conn = init_db(":memory:")  # deliberately empty -- also proves the no-data-yet path is clean
    tab = StatsTab(conn)

    assert tab.chart_picker.count() >= 1
    assert tab.chart_picker.itemText(0) == "Turns on Field"
    assert tab.regulation_filter.itemText(0) == "All regulations"
    assert tab.team_filter.itemText(0) == "All teams"
    assert tab.mon_filter.itemText(0) == "All Pokemon"
    assert tab.side_filter.count() == 3
    assert tab.side_filter.currentData() == "mine", "a fresh Stats tab should default the Side filter to My Pokemon, not Both"
    # default chart (Turns on Field) is chart_type="bar", so Difference should already be offered
    assert tab.result_filter.findData("diff") >= 0, "expected 'Difference' to be offered for a bar chart"
    print("PASS: stats tab constructs with all expected widgets, even against an empty DB")


def test_stats_tab_result_options_track_chart_type():
    """
    "Difference" should only ever be offered while a bar-chart stat
    is selected -- confirms switching to Move Usage (a pie chart)
    drops it, and that having "Difference" selected at the time of
    the switch falls back to "All results" instead of leaving an
    invalid filter silently active against a chart it doesn't mean
    anything for.
    """
    app = QApplication.instance() or QApplication(sys.argv)
    conn = init_db(":memory:")
    tab = StatsTab(conn)

    diff_index = tab.result_filter.findData("diff")
    tab.result_filter.setCurrentIndex(diff_index)
    assert tab.result_filter.currentData() == "diff"

    tab.chart_picker.setCurrentText("Move Usage")
    assert tab.result_filter.findData("diff") == -1, "Move Usage is a pie chart -- Difference shouldn't be offered"
    assert tab.result_filter.currentData() is None, "a stale 'diff' selection should fall back to All results"

    tab.chart_picker.setCurrentText("Turns on Field")
    assert tab.result_filter.findData("diff") >= 0, "switching back to a bar chart should restore Difference"

    print("PASS: the Result dropdown's 'Difference' option tracks the selected chart's chart_type")


def test_stats_tab_cascading_filters():
    """
    Regulation -> Team -> Pokemon: picking a regulation narrows Team
    to teams actually played under it, and picking a team further
    narrows Pokemon to that team's actual roster -- including a mon
    on the roster that's never been sent out (Incineroar, real data,
    confirmed never moved in either sample game), which a
    species-seen-in-events list would miss entirely.
    """
    app = QApplication.instance() or QApplication(sys.argv)
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
    mb_team_id = conn.execute("SELECT team_id FROM teams").fetchone()[0]

    h_team_id = resolve_team_id(conn, [
        {"species": "Flutter Mane", "item": "", "moves": []},
        {"species": "Chien-Pao", "item": "", "moves": []},
    ], regulation="H")
    conn.execute(
        "INSERT INTO games (game_id, replay_url, my_side, team_id, regulation, result) VALUES (?, ?, ?, ?, ?, ?)",
        ("synthetic-reg-h-game", "https://example.com/reg-h", "p1", h_team_id, "H", "W"),
    )

    tab = StatsTab(conn)
    assert tab.team_filter.count() == 3, f"expected 'All teams' + 2 real teams, got {tab.team_filter.count()}"

    mb_index = tab.regulation_filter.findData("M-B")
    tab.regulation_filter.setCurrentIndex(mb_index)
    assert tab.team_filter.count() == 2, "expected only the M-B team to remain"
    assert tab.team_filter.findData(h_team_id) == -1, "the H-regulation team shouldn't be offered under M-B"

    team_index = tab.team_filter.findData(mb_team_id)
    tab.team_filter.setCurrentIndex(team_index)
    assert tab.mon_filter.count() == 7, f"expected 'All Pokemon' + the 6-mon roster, got {tab.mon_filter.count()}"
    assert tab.mon_filter.findData("Incineroar") >= 0, "a roster mon that's never been sent out should still be offered"

    conn.close()
    print("PASS: Regulation narrows Team, and Team narrows Pokemon to that team's real roster")


def test_stats_tab_mon_filter_disables_for_move_usage():
    """
    The Pokemon filter is grayed out (disabled, not cleared) while
    Move Usage is selected -- see Stat.uses_mon_filter -- since Move
    Usage always shows a team's whole roster regardless of it. A
    value picked under Turns on Field should still be sitting there,
    just inactive, when switching to Move Usage and back.
    """
    app = QApplication.instance() or QApplication(sys.argv)
    conn = init_db(":memory:")
    import_replay(
        conn, load_log("game1_win_vs_datrandomguy787.json"),
        "https://replay.pokemonshowdown.com/gen9championsvgc2026regmbbo3-2647404582",
        my_usernames={"salmoncashew"},
    )
    tab = StatsTab(conn)

    whimsicott_index = tab.mon_filter.findData("Whimsicott")
    tab.mon_filter.setCurrentIndex(whimsicott_index)
    assert tab.mon_filter.isEnabled()

    tab.chart_picker.setCurrentText("Move Usage")
    assert not tab.mon_filter.isEnabled(), "Move Usage shouldn't leave the Pokemon filter enabled"
    assert tab.mon_filter.currentData() == "Whimsicott", "the selection should be preserved, not cleared"

    tab.chart_picker.setCurrentText("Turns on Field")
    assert tab.mon_filter.isEnabled(), "switching back to a chart that uses it should re-enable it"
    assert tab.mon_filter.currentData() == "Whimsicott"

    conn.close()
    print("PASS: the Pokemon filter disables (without clearing) for Move Usage, and re-enables for Turns on Field")


def test_stats_tab_filters_are_searchable():
    app = QApplication.instance() or QApplication(sys.argv)
    conn = init_db(":memory:")
    tab = StatsTab(conn)

    filters = {
        "regulation_filter": tab.regulation_filter,
        "team_filter": tab.team_filter,
        "mon_filter": tab.mon_filter,
        "result_filter": tab.result_filter,
        "side_filter": tab.side_filter,
    }
    for name, combo in filters.items():
        assert combo.isEditable(), f"{name}: expected type-to-filter search to be enabled"
        assert combo.completer() is not None, f"{name}: expected a completer"
        assert combo.completer().filterMode() == Qt.MatchContains, f"{name}: expected substring matching"

    print("PASS: every filter dropdown is set up as a type-to-filter search box")


def test_teams_tab_constructs():
    app = QApplication.instance() or QApplication(sys.argv)
    conn = init_db(":memory:")  # deliberately empty -- proves the no-teams-yet placeholder path is clean
    tab = TeamsTab(conn)

    assert tab.table is not None
    # The picker loads the real, usage-ordered roster for the default
    # format on construction (task B), not the raw alphabetical pokedex:
    # only legal species, led by the meta's #1-usage mon.
    assert len(tab._visible_species) > 0
    assert len(tab._visible_species) < 1351  # a subset of the whole catalogue
    assert tab._visible_species[0]["name"] == "Great Tusk"  # gen9ou #1
    print(f"PASS: teams tab constructs and loads the {len(tab._visible_species)}-mon OU roster")


def test_teams_tab_pokemon_grid_layout():
    app = QApplication.instance() or QApplication(sys.argv)
    conn = init_db(":memory:")
    tab = TeamsTab(conn)

    # Verify initial headers (Row 0)
    assert tab.pokedex_grid.columnCount() == 10
    assert len(tab.pokedex_headers) == 10

    # Clear pokedex grid to isolate single entry test
    tab.clear_pokedex_grid()

    # Add a sample Pokemon entry with 2 types and 4 abilities
    sample_mon = {
        "name": "Charizard",
        "types": ["Fire", "Flying"],
        "abilities": ["Blaze", "Solar Power", "Tough Claws", "Drought"],
        "stats": {"hp": 78, "atk": 84, "def": 78, "spa": 109, "spd": 85, "spe": 100}
    }
    tab.add_pokemon_row(sample_mon)

    # Row 0 is header, Row 1 is Charizard entry
    assert tab.table.rowCount() == 2

    # Check Abilities cell widget at (row 1, col 2)
    abilities_item = tab.pokedex_grid.itemAtPosition(1, 2)
    assert abilities_item is not None
    abilities_widget = abilities_item.widget()
    assert abilities_widget is not None
    # Abilities widget inner grid layout
    ab_layout = abilities_widget.layout()
    assert ab_layout.count() == 4
    # Check 2 columns x 2 rows mapping:
    # Item 0 ("Blaze") at (0, 0), Item 1 ("Solar Power") at (1, 0)
    # Item 2 ("Tough Claws") at (0, 1), Item 3 ("Drought") at (1, 1)
    assert ab_layout.itemAtPosition(0, 0).widget().text() == "Blaze"
    assert ab_layout.itemAtPosition(1, 0).widget().text() == "Solar Power"
    assert ab_layout.itemAtPosition(0, 1).widget().text() == "Tough Claws"
    assert ab_layout.itemAtPosition(1, 1).widget().text() == "Drought"

    print("PASS: teams tab pokemon grid layout renders multi-type and 2x2 abilities cleanly")


def test_teams_tab_grid_loads_usage_ordered_immediately():
    app = QApplication.instance() or QApplication(sys.argv)
    conn = init_db(":memory:")
    tab = TeamsTab(conn)

    # The grid is pre-populated from the real format roster on construction
    # (task B), led by gen9ou's #1-usage mon -- not the alphabetical pokedex.
    assert tab._visible_species[0]["name"] == "Great Tusk"

    # Row 1 in the rendered grid is the usage leader
    row1_widget = tab.pokedex_grid.itemAtPosition(1, 0).widget()
    labels1 = row1_widget.findChildren(QLabel)
    assert any(lbl.text() == "Great Tusk" for lbl in labels1)

    # Row 2 is the #2-usage mon
    row2_widget = tab.pokedex_grid.itemAtPosition(2, 0).widget()
    labels2 = row2_widget.findChildren(QLabel)
    assert any(lbl.text() == "Gholdengo" for lbl in labels2)

    print("PASS: pokedex grid populates the usage-ordered format roster immediately")



def test_main_window_assembles_all_tabs():
    app = QApplication.instance() or QApplication(sys.argv)
    conn = init_db(":memory:")
    window = MainWindow(["salmoncashew"], conn)

    assert window.windowTitle() == "Fourslice", f"got {window.windowTitle()!r}"
    assert window.import_tab is not None
    assert window.stats_tab is not None
    assert window.teams_tab is not None
    assert window.replays_tab is not None

    tabs = window.centralWidget()
    assert tabs.count() == 4
    assert tabs.tabText(0) == "Import"
    assert tabs.tabText(1) == "Stats"
    assert tabs.tabText(2) == "Teams"
    assert tabs.tabText(3) == "Replays"
    print("PASS: main window assembles the import, stats, teams, and replays tabs")


def test_start_data_refresh_async_completes_cleanly():
    """start_data_refresh_async returns a handle with a live thread/worker,
    and the worker finishing shuts the thread down without "QThread
    destroyed while running" (the module orphan list keeps the pair alive
    until thread.finished). The worker's run is stubbed so the test stays
    offline."""
    from fourslice.gui import data_refresh_worker as drw

    app = QApplication.instance() or QApplication(sys.argv)

    original_run = drw.DataRefreshWorker.run

    def fake_run(self):
        self.done.emit(True)  # no network -- done fires from the worker thread

    try:
        drw.DataRefreshWorker.run = fake_run
        handle = drw.start_data_refresh_async()
        assert handle.thread is not None
        assert handle.worker is not None
        assert handle.thread.isRunning(), "async start should launch the thread"

        # The thread is registered in the orphan list until it finishes.
        assert (handle.thread, handle.worker) in drw._data_refresh_orphans

        # Pump the event loop so done -> thread.quit gets delivered.
        deadline = time.monotonic() + 10
        while handle.thread.isRunning() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.02)

        assert not handle.thread.isRunning(), "worker done should stop the thread"

        # The thread.finished → _forget delivery may still be queued for one
        # more main-loop tick after isRunning() flips False; pump a few more
        # times so the orphan-list removal is guaranteed to land.
        for _ in range(50):
            app.processEvents()
            time.sleep(0.01)
        assert (handle.thread, handle.worker) not in drw._data_refresh_orphans, \
            "finished thread should be forgotten from the orphan list"
        print("PASS: start_data_refresh_async returns a live handle and cleans up on finish")
    finally:
        drw.DataRefreshWorker.run = original_run


def test_refresh_done_bridge_reloads_teams_tab_and_sidebar():
    """_RefreshDoneBridge.on_refresh_done(True) reloads the Team Builder and
    resets the sidebar's sprite/id caches; on False it does nothing."""
    from fourslice.gui.data_refresh_worker import _RefreshDoneBridge
    from fourslice.gui.sidebar import _load_pokemon_id_map, reset_pokemon_id_map

    app = QApplication.instance() or QApplication(sys.argv)
    conn = init_db(":memory:")
    tab = TeamsTab(conn)
    sidebar = Sidebar()

    # Prime the id map so we can detect the reset, then stub the tab reload.
    # _load_pokemon_id_map sets the module-global; we must read it from the
    # module (not the stale local binding captured by the top-level import).
    _load_pokemon_id_map()
    assert sidebar_module._POKEMON_ID_MAP is not None, "sidebar should build its id map once"
    sidebar._sprite_cache["Great Tusk"] = "stale-pixmap"

    calls = []
    original_reload = tab.reload_catalog
    original_reset = sidebar_module.reset_pokemon_id_map
    try:
        tab.reload_catalog = lambda: calls.append("reload")
        sidebar_module.reset_pokemon_id_map = lambda: calls.append("reset")

        bridge = _RefreshDoneBridge(tab, sidebar)
        bridge.on_refresh_done(False)
        assert calls == [], "a failed refresh must not touch the UI"

        bridge.on_refresh_done(True)
        assert calls == ["reset", "reload"], f"expected reset then reload, got {calls}"
        assert sidebar._sprite_cache == {}, "the sprite cache must be cleared on refresh done"
    finally:
        tab.reload_catalog = original_reload
        sidebar_module.reset_pokemon_id_map = original_reset
        reset_pokemon_id_map()  # leave the module-global clean for other tests
    print("PASS: refresh done bridge reloads the team builder and clears sidebar caches")


def test_teams_tab_reload_catalog_is_safe_with_unchanged_data():
    """reload_catalog re-reads the catalogue and rebuilds the picker lists
    without breaking the tab when the underlying data hasn't changed --
    the visible species stay valid and usage-ordered."""
    app = QApplication.instance() or QApplication(sys.argv)
    conn = init_db(":memory:")
    tab = TeamsTab(conn)

    initial = list(tab._visible_species)
    assert len(initial) > 0, "the catalogue should be populated at construction"
    assert initial[0]["name"] == "Great Tusk", "the picker should stay usage-ordered"

    tab.reload_catalog()

    assert len(tab._visible_species) == len(initial)
    assert tab._visible_species[0]["name"] == "Great Tusk"
    assert tab.pokedex_grid.itemAtPosition(1, 0) is not None, \
        "the pokedex grid should still be populated after reload"
    print("PASS: reload_catalog refreshes picker lists without breaking the tab")


def test_replays_tab_constructs_and_plays_back():
    app = QApplication.instance() or QApplication(sys.argv)
    conn = init_db(":memory:")

    # Empty DB: shows the placeholder, no controls active.
    tab = ReplaysTab(conn, auto_fetch_sprites=False)
    assert tab.game_list.count() == 1
    assert "No replays stored yet" in tab.game_list.item(0).text()
    assert not tab.play_button.isEnabled()

    # With a stored log, it populates the list and renders the first turn.
    import_replay(
        conn, load_log("game1_win_vs_datrandomguy787.json"),
        "https://replay.pokemonshowdown.com/gen9championsvgc2026regmbbo3-2647404582",
        my_usernames={"salmoncashew"},
    )
    tab.refresh_replays()
    assert tab.game_list.count() == 1

    # Current row is auto-selected on refresh -> timeline built, first frame rendered.
    assert tab._frames, "expected a timeline with at least one frame"
    assert tab.play_button.isEnabled()

    # Step forward/back advance and return.
    initial_turn = tab._frame()["turn"]
    tab.step_forward()
    assert tab._frame()["turn"] > initial_turn
    tab.step_back()
    assert tab._frame()["turn"] == initial_turn

    # Board labels were populated for whoever's on the field.
    slot_texts = [tab.board_labels[s].text() for s in ("p1a", "p1b", "p2a", "p2b")]
    assert any(t != "--" for t in slot_texts), f"expected at least one mon on the board, got {slot_texts}"

    conn.close()
    print("PASS: replays tab lists stored games, builds a timeline, and steps back/forward")


def test_replays_tab_pruned_logs_show_placeholder():
    """
    Deleting stored logs (settings-off or pruning) empties the Replays
    list back to the empty-state placeholder even though the game rows
    themselves survive.
    """
    app = QApplication.instance() or QApplication(sys.argv)
    conn = init_db(":memory:")
    import_replay(
        conn, load_log("game1_win_vs_datrandomguy787.json"),
        "https://replay.pokemonshowdown.com/gen9championsvgc2026regmbbo3-2647404582",
        my_usernames={"salmoncashew"},
        store_logs=True,
    )

    tab = ReplaysTab(conn, auto_fetch_sprites=False)
    assert tab.game_list.count() == 1

    conn.execute("DELETE FROM logs")
    conn.commit()
    tab.refresh_replays()
    assert tab.game_list.count() == 1
    assert "No replays stored yet" in tab.game_list.item(0).text()

    conn.close()
    print("PASS: clearing stored logs empties the replays list as expected")


def test_replays_tab_shows_cached_gen5_sprites_and_falls_back_to_text():
    """
    With a SpriteStore whose cache is pre-seeded, the board renders
    sprites (QPixmap, not text) for mons that have art and keeps the
    plain name label for mons without -- mirroring the real server,
    where Sneasler has no gen-5 sprite. Frame 0 of game1 also shows
    the mega evolution: p1b is Blastoise-Mega with its own (front-view)
    sprite, and the label reads "Mega Blastoise".
    """
    app = QApplication.instance() or QApplication(sys.argv)
    conn = init_db(":memory:")
    import_replay(
        conn, load_log("game1_win_vs_datrandomguy787.json"),
        "https://replay.pokemonshowdown.com/gen9championsvgc2026regmbbo3-2647404582",
        my_usernames={"salmoncashew"},
    )

    with tempfile.TemporaryDirectory() as tmp_dir:
        # Fake server: no animated art at all, and no gen-5 art for
        # Sneasler (the real play.pokemonshowdown.com 404s there too).
        # Everything else resolves to the gen-5 static set.
        def downloader(url):
            if "sneasler" in url or "gen5ani" in url or "/ani/" in url:
                return None
            return _PNG_BYTES

        store = SpriteStore(base_dir=Path(tmp_dir), downloader=downloader)

        # Seed exactly the frame-0 board, then let the tab render it:
        # salmoncashew is p2 in game1, so the user's side (p2) is the
        # back view and p1 the front view.
        for species, back in [
            ("Sneasler", False), ("Blastoise-Mega", False),
            ("Whimsicott", True), ("Sneasler", True),
        ]:
            store.get_or_fetch(species, back)

        tab = ReplaysTab(conn, usernames={"salmoncashew"}, sprite_store=store, auto_fetch_sprites=False)
        tab.step_forward()  # frame 0 is the pre-battle entry; turn 1 mega'd
        frame = tab._frame()
        assert tab._back_side == "p2"  # salmoncashew plays p2 in game1
        assert frame["board"].get("p1b") == "Blastoise-Mega"  # mega'd on turn 1

        # Blastoise-Mega and Whimsicott have (fake) gen-5 art -> QPixmap shown.
        assert tab.sprite_labels["p1b"].pixmap()
        assert tab.sprite_labels["p2a"].pixmap()
        assert tab.sprite_labels["p1b"].movie() is None  # static sprite, not an animation

        # Sneasler has none -> text-only fallback, name label intact.
        # (An unset QLabel returns a null QPixmap, which is falsy.)
        assert not tab.sprite_labels["p1a"].pixmap()
        assert not tab.sprite_labels["p2b"].pixmap()
        assert tab.board_labels["p1a"].text() == "Sneasler"
        assert tab.board_labels["p1b"].text() == "Mega Blastoise"

        # Stepping to a later turn keeps the pipeline working.
        tab.step_forward()
        assert tab._frame()["turn"] > frame["turn"]

    conn.close()
    print("PASS: replays tab renders cached gen-5 sprites and falls back to text when missing")


def test_replays_tab_renders_from_the_users_point_of_view():
    """
    The board is rendered from the user's point of view: whoever
    matches a configured username (falling back to the stored
    games.my_side result) is the bottom row with back-view sprites,
    both rows are labelled with player names, HP bars carry a live
    percentage, and the narrative names the players instead of slot
    codes. Mega evolutions show up in the board the turn they happen.
    """
    app = QApplication.instance() or QApplication(sys.argv)
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

    tab = ReplaysTab(conn, usernames={"salmoncashew"}, auto_fetch_sprites=False)

    GAME2 = "gen9championsvgc2026regmbbo3-2647349643"  # salmoncashew is p1
    GAME1 = "gen9championsvgc2026regmbbo3-2647404582"  # salmoncashew is p2

    # game2: salmoncashew is p1, so their row is the bottom/back side
    # and the opponent's name sits on top.
    tab.game_list.setCurrentRow(_replays_row_for(tab, GAME2))
    assert tab._back_side == "p1"
    assert tab.side_labels["p1"].text() == "salmoncashew (you)"
    assert tab.side_labels["p2"].text() == "aletito"

    # game1: same user is p2 -- the layout must flip so their side
    # still ends up as the bottom/back row.
    tab.game_list.setCurrentRow(_replays_row_for(tab, GAME1))
    assert tab._back_side == "p2"
    assert tab.side_labels["p2"].text() == "salmoncashew (you)"
    assert tab.side_labels["p1"].text() == "datrandomguy787"

    # Mega evolution happened on turn 1: step past the pre-battle entry,
    # then the slot's species updates and the label converts the raw log
    # name for display.
    tab.step_forward()
    frame = tab._frame()
    assert frame["board"]["p1b"] == "Blastoise-Mega"
    assert tab.board_labels["p1b"].text() == "Mega Blastoise"
    QTest.qWait(700)  # let the HP drain from 100% settle at 82%

    # HP bars show a live percentage when HP is known...
    assert tab.hp_bars["p1b"].text() == "82%"
    assert tab.hp_bars["p2a"].text() == "100%"
    assert tab.hp_bars["p2b"].text() == "100%"

    # ...and the narrative is written from the players' names, with the
    # post-mega species on display.
    narrative = tab.event_browser.toPlainText()
    assert "salmoncashew's Sneasler used Protect!" in narrative
    assert "datrandomguy787's Sneasler used Fake Out on Sneasler!" in narrative
    assert "salmoncashew's Basculegion used Flip Turn on Mega Blastoise!" in narrative
    assert "datrandomguy787's Mega Blastoise used Shell Smash!" in narrative
    assert "salmoncashew's Whimsicott switched in!" in narrative

    # With no configured usernames, the importer's stored side still
    # orients the board correctly (salmoncashew was p2 in game1).
    fallback = ReplaysTab(conn, auto_fetch_sprites=False)
    fallback.game_list.setCurrentRow(_replays_row_for(fallback, GAME1))
    assert fallback._back_side == "p2"
    assert fallback.side_labels["p2"].text() == "salmoncashew (you)"

    conn.close()
    print("PASS: replays tab renders from the user's POV with mega formes, HP %, and named narrative")


def test_replays_tab_board_animations():
    """
    With board animation enabled and sprites seeded, a frame render
    starts the Showdown-lite effects: attackers surge and targets
    flash, HP drains instead of snapping, idle bobs run on sprite
    labels, and a faint fades the sprite out before the slot clears.
    """
    app = QApplication.instance() or QApplication(sys.argv)
    conn = init_db(":memory:")
    import_replay(
        conn, load_log("game1_win_vs_datrandomguy787.json"),
        "https://replay.pokemonshowdown.com/gen9championsvgc2026regmbbo3-2647404582",
        my_usernames={"salmoncashew"},
    )

    with tempfile.TemporaryDirectory() as tmp_dir:
        # Static gen-5 art only (the animated set is "missing"), like
        # the other sprite tests -- Sneasler still has no art at all.
        def downloader(url):
            if "sneasler" in url or "gen5ani" in url or "/ani/" in url:
                return None
            return _PNG_BYTES

        store = SpriteStore(base_dir=Path(tmp_dir), downloader=downloader)
        for species, back in [
            # game1's pre-battle leads (frame 0): Sneasler and Blastoise
            # up top, Basculegion and Sneasler down the back.
            ("Sneasler", False), ("Blastoise", False), ("Basculegion", True),
            ("Sneasler", True), ("Whimsicott", True), ("Blastoise-Mega", False),
        ]:
            store.get_or_fetch(species, back)

        tab = ReplaysTab(
            conn, usernames={"salmoncashew"}, sprite_store=store,
            auto_fetch_sprites=False, animate=True,
        )
        assert tab._animate

        # Idle bobs are deliberately deferred until the board is laid out
        # (sprites rendered pre-show must not fling into a (0,0) corner),
        # so "showing" the tab is what kicks them off.
        assert tab._idle_bob_timers == {}
        tab.showEvent(QShowEvent())
        QApplication.processEvents()
        QTest.qWait(20)

        # Frame 0 of game1 is the pre-battle entry: the lead switch-in
        # fades are running, and every sprite-bearing slot is bobbing.
        assert len(tab._animations) > 0
        for slot in ("p1b", "p2a"):
            assert tab._idle_bob_timers.get(slot) is not None, f"{slot} should be bobbing"
        for slot in ("p1a", "p2b"):
            assert tab._idle_bob_timers.get(slot) is None, f"{slot} has no sprite to bob"

        # HP was set directly on the first frame (nothing to drain to)...
        assert tab.hp_bars["p1b"].value() == 100

        # ...so force a change and let it drain, while the flashes/fades
        # unwind on their own.
        tab._set_hp_bar("p2b", 60)
        QTest.qWait(1000)
        assert tab.hp_bars["p2b"].value() == 60
        assert tab.hp_bars["p1b"].value() == 100
        assert tab._slot_effect.get("p1b") is None, "flash effects should detach after finishing"

        # Turn 2: Blastoise fainted -> its sprite fades out, then the
        # slot clears to "--" with an empty HP bar.
        tab.step_forward()  # -> turn 1 (the mega + moves)
        tab.step_forward()  # -> turn 2 (Blastoise faints)
        assert tab._frame()["turn"] == 2
        assert tab._fading_slots, "a faint fade should be in flight"
        QTest.qWait(1000)
        assert tab.board_labels["p1b"].text() == "--"
        assert not tab.sprite_labels["p1b"].isVisible()
        assert tab.hp_bars["p1b"].value() == 0

        # Animation can be disabled entirely (the Settings toggle), which
        # leaves the board static but fully functional.
        quiet = ReplaysTab(conn, usernames={"salmoncashew"}, sprite_store=store,
                           auto_fetch_sprites=False, animate=False)
        assert not quiet._animate
        assert not quiet._idle_bob_timers, "no bobbing when animations are off"

    conn.close()
    print("PASS: replays tab animates moves, HP, faints, switches, megas, and idle bobs")


def test_replays_tab_move_effects_and_conditions():
    """
    Move-specific effects and the board-conditions scene render on the
    board: Moonblast launches a projectile, Protect raises a shield,
    Fake Out jabs, spread moves flash every target, the conditions strip
    and per-side chips follow weather/side conditions across frames, and
    the board tint shifts when the field changes. With animation
    disabled, no effect widgets spawn but the conditions still update.
    """
    app = QApplication.instance() or QApplication(sys.argv)
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

    with tempfile.TemporaryDirectory() as tmp_dir:
        # A server where every mon has art, so every move effect has a
        # sprite to aim at (unlike the other sprite tests' fake server,
        # which intentionally 404s Sneasler). Static gen-5 art only:
        # animated .gif files would be held open by QMovie on Windows
        # and block the temp-dir cleanup.
        def downloader(url):
            if "gen5ani" in url or "/ani/" in url:
                return None
            return _PNG_BYTES

        store = SpriteStore(base_dir=Path(tmp_dir), downloader=downloader)
        for species, back in [
            ("Whimsicott", True), ("Basculegion", True),
            ("Aerodactyl", False), ("Garchomp", False),
            ("Sneasler", False), ("Sneasler", True),
            ("Blastoise-Mega", False),
        ]:
            store.get_or_fetch(species, back)

        tab = ReplaysTab(
            conn, usernames={"salmoncashew"}, sprite_store=store,
            auto_fetch_sprites=False, animate=True,
        )

        # game2 (selected explicitly -- the list is sorted by battle
        # number, not import order): frame 0 is the pre-battle entry,
        # so step to turn 1 -- no weather yet, p2's Tailwind is up, and
        # Moonblast launches a projectile.
        tab.game_list.setCurrentRow(_replays_row_for(
            tab, "gen9championsvgc2026regmbbo3-2647349643"))
        tab.step_forward()
        assert tab._frame()["turn"] == 1
        assert tab._frame()["weather"] is None
        assert tab.conditions_label.text() == ""
        # The background track color uses the theme token -- compare against
        # whatever theme is active (theme persistence changed the default
        # from light-only to config-dependent)
        theme_track = page_module.THEME.tokens["TRACK"]
        assert theme_track.lower() in tab.board_container.styleSheet().lower()
        assert tab._frames[1]["side_conditions"].get("p2") == ["Tailwind"]
        assert "Tailwind" in tab.side_cond_labels["p2"].text()
        assert "Tailwind" not in tab.side_cond_labels["p1"].text()
        assert [m[5] for m in tab._frame()["moves"] if m[3] == "Moonblast"][0] == ["p2b"]
        assert tab._effect_widgets, "Moonblast should spawn a projectile"

        # Turn 2: p1 (the user) gets Tailwind too.
        tab.step_forward()
        assert tab._frame()["turn"] == 2
        assert "Tailwind" in tab.side_cond_labels["p1"].text()
        assert "Tailwind" in tab.side_cond_labels["p2"].text()

        # Turn 4: Charizard's Drought brings harsh sunlight (board
        # tinted), and p2's Tailwind expires.
        tab.step_forward()
        tab.step_forward()
        assert tab._frame()["turn"] == 4
        assert tab._frame()["weather"] == "Harsh sunlight"
        assert "Harsh sunlight" in tab.conditions_label.text()
        assert "#fff3d6" in tab.board_container.styleSheet()
        assert tab._frames[4]["side_conditions"].get("p2") is None
        assert "Tailwind" not in tab.side_cond_labels["p2"].text()
        assert "Tailwind" in tab.side_cond_labels["p1"].text()

        # Let the transient effects unwind before switching games.
        QTest.qWait(1200)

        # game1 turn 1: Protect raises a shield, Fake Out jabs, Flip
        # Turn dashes, and Shell Smash pulses -- all with sprites up.
        tab.game_list.setCurrentRow(_replays_row_for(
            tab, "gen9championsvgc2026regmbbo3-2647404582"))
        tab.step_forward()  # frame 0 is the pre-battle entry
        assert tab._frame()["turn"] == 1
        move_names = {m[3] for m in tab._frame()["moves"]}
        assert move_names == {"Protect", "Fake Out", "Flip Turn", "Shell Smash"}
        assert tab._effect_widgets, "Protect/Fake Out/Flip Turn should spawn effects"

        # Conditions are info, not animation: a quiet tab still updates
        # the strips and tint but spawns no effects.
        quiet = ReplaysTab(
            conn, usernames={"salmoncashew"}, sprite_store=store,
            auto_fetch_sprites=False, animate=False,
        )
        quiet.game_list.setCurrentRow(_replays_row_for(
            quiet, "gen9championsvgc2026regmbbo3-2647349643"))
        assert not quiet._effect_widgets
        quiet.step_forward()  # -> turn 1 (Tailwind up)
        assert "Tailwind" in quiet.side_cond_labels["p2"].text()
        quiet.step_forward()  # -> turn 2 (p1's Tailwind comes up)
        assert quiet._frame()["turn"] == 2
        assert "Tailwind" in quiet.side_cond_labels["p1"].text()

    conn.close()
    print("PASS: replays tab animates moves specifically and renders board conditions")


def _synthetic_battle_frames():
    """A plain single-turn timeline the board can render headlessly:
    your Whimsicott Moonblasts the opponent's Garchomp (one real hit)."""
    move = ("p1", "p1a", "Whimsicott", "Moonblast", "p2a", ["p2a"], {"p2a": "Garchomp"})
    return {
        "turn": 1,
        "board": {"p1a": "Whimsicott", "p2a": "Garchomp"},
        "hp": {"p1a": "100/100", "p2a": "100/100"},
        "substitutes": {},
        "weather": None, "terrain": None, "room": None,
        "side_conditions": {}, "side_condition_remaining": {},
        "faints": [], "switches": [], "megas": [],
        "events": [
            ("switch", "p1", "p1a", "Whimsicott", "100/100"),
            ("switch", "p2", "p2a", "Garchomp", "100/100"),
            ("move", "p1", "p1a", "Whimsicott", "Moonblast", "p2a", "Garchomp",
             ["p2a"], {"p2a": "Garchomp"}, False),
            ("damage", "p2a", "44/100", "Garchomp"),
        ],
        "moves": [move],
    }


def test_replays_tab_hit_animates_once_on_the_move_beat():
    """One hurt animation per real hit (item A): the move's own beat draws
    the target's shake/flash, so the damage event a beat later must NOT
    replay it. Only move targets are put on the skip list -- a recoil or
    status hit on the attacker is no move target and still flashes on its
    own beat."""
    app = QApplication.instance() or QApplication(sys.argv)
    conn = init_db(":memory:")
    with tempfile.TemporaryDirectory() as tmp_dir:
        def downloader(url):
            return None if "gen5ani" in url else _PNG_BYTES
        store = SpriteStore(base_dir=Path(tmp_dir), downloader=downloader)
        store.get_or_fetch("Whimsicott", True)   # your active mon
        store.get_or_fetch("Garchomp", False)    # their active mon
        tab = ReplaysTab(conn, usernames=set(), sprite_store=store,
                         auto_fetch_sprites=False, animate=True)
        tab._frames = [_synthetic_battle_frames()]
        tab._frame_index = 0
        tab._event_index = 0
        flashed = []
        tab._damage_flash = lambda s: flashed.append(s)
        tab._advance_one_event()   # turn entrance + Whimsicott switch-in
        assert flashed == [], "switch-ins must not shake anyone"
        tab._advance_one_event()   # Garchomp switch-in
        tab._advance_one_event()   # the move beat: hurt deferred to the orb
        assert flashed == [], "the hurt face must wait for the projectile to connect"
        tab._advance_one_event()   # the damage beat: must not replay the hurt
        assert flashed == [], "the move beat already owns this hit -- no second flash"
        QTest.qWait(1200)  # let the orb land; the deferred hurt fires exactly once
        assert flashed == ["p2a"], "expected exactly one hurt animation per real hit"
        QTest.qWait(500)
    conn.close()
    print("PASS: the damage beat doesn't replay the move's hit animation")


def test_replays_tab_spriteless_mons_still_protect_and_take_aim():
    """No art, no excuse (item B): a spriteless mon still raises its
    protect shield (anchored over the name tag), and a spriteless target
    is still aimable by an orb instead of the move degrading to a generic
    surge."""
    from fourslice.gui.replays_tab import _MOVE_FX
    app = QApplication.instance() or QApplication(sys.argv)
    conn = init_db(":memory:")

    # Shield on a fully spriteless holder.
    tab1 = ReplaysTab(conn, usernames=set(), sprite_store=None,
                      auto_fetch_sprites=False, animate=True)
    tab1._frames = [_synthetic_battle_frames()]
    tab1._frame_index = 0
    tab1._animate_move("p1a", "Protect", None, [])
    assert tab1._shields.get("p1a") is not None, \
        "spriteless protect must still raise a shield"

    # Orb aimed at a spriteless target (only the attacker has art here).
    with tempfile.TemporaryDirectory() as tmp_dir:
        def downloader(url):
            return None if "gen5ani" in url else _PNG_BYTES
        store = SpriteStore(base_dir=Path(tmp_dir), downloader=downloader)
        store.get_or_fetch("Whimsicott", True)   # the attacker
        tab2 = ReplaysTab(conn, usernames=set(), sprite_store=store,
                          auto_fetch_sprites=False, animate=True)
        tab2._frames = [_synthetic_battle_frames()]
        tab2._frame_index = 0
        fr = tab2._frames[0]
        tab2._render_board_state(fr["board"], fr["hp"], fr["substitutes"])
        # Whimsicott's sprite is up; Garchomp has no art and stays spriteless.
        before = len(tab2._effect_widgets)
        tab2._animate_move("p1a", "Moonblast", "p2a", ["p2a"])
        assert len(tab2._effect_widgets) > before, \
            "an orb must still fly into a spriteless target"
        QTest.qWait(900)

    # The new effect kinds introduced for item D classify as expected.
    assert _MOVE_FX["Blizzard"] == "frost"
    assert _MOVE_FX["Ice Beam"] == "frost"
    assert _MOVE_FX["Thunderbolt"] == "thunder"
    assert _MOVE_FX["Earthquake"] == "earthquake"
    assert _MOVE_FX["Surf"] == "wave"
    assert _MOVE_FX["Fire Blast"] == "flame_blast"
    assert _MOVE_FX["Hyper Beam"] == "nova"
    assert _MOVE_FX["Overheat"] == "burst"
    conn.close()
    print("PASS: spriteless protect shields and spriteless targets still animate")
def test_replays_tab_overlapping_sprite_fetches_reuse_one_thread_and_shut_down_cleanly():
    """
    Regression for "QThread: Destroyed while thread '' is still running":
    every return to the Replays tab used to spawn a fresh QThread and
    abandon the previous one while it was still blocked on the network,
    which Qt treats as a hard fatal error. The fetch service must stay a
    single thread that queues request batches, and shutdown must stop it
    without destroying the QThread while its OS thread runs.
    """
    app = QApplication.instance() or QApplication(sys.argv)
    conn = init_db(":memory:")
    import_replay(
        conn, load_log("game1_win_vs_datrandomguy787.json"),
        "https://replay.pokemonshowdown.com/gen9championsvgc2026regmbbo3-2647404582",
        my_usernames={"salmoncashew"},
    )

    gate = threading.Event()

    def downloader(url):
        # No animated art anywhere (so sprites render as QPixmap, not
        # QMovie, keeping the assertion simple) and no gen-5 art for
        # Sneasler -- mirroring the real server.
        if "sneasler" in url or "gen5ani" in url:
            return None
        gate.wait(5.0)  # hold the first pass in flight until released
        return _PNG_BYTES

    with tempfile.TemporaryDirectory() as tmp_dir:
        store = SpriteStore(base_dir=Path(tmp_dir), downloader=downloader)

        # auto_fetch=True means __init__'s refresh_replays() already kicks
        # the first fetch, which parks in the gated downloader. Pin the
        # sprites-enabled default so a sprites-off config on the host
        # machine can't silently no-op this test.
        tab = ReplaysTab(conn, sprite_store=store, auto_fetch_sprites=True)
        tab._sprites_enabled = True
        try:
            thread_a = tab._sprite_thread
            assert thread_a is not None and thread_a.isRunning()

            # Every return to the tab re-runs the fetch. It must NOT
            # create a second thread, drop the running one, or let the
            # worker be garbage-collected mid-fetch.
            for _ in range(3):
                tab._start_sprite_fetch()
                assert tab._sprite_thread is thread_a
                assert tab._sprite_worker is not None
                assert thread_a.isRunning()

            # Release the downloader; queued re-requests drain from the
            # disk cache. Pump the event loop so sprite_ready handlers
            # (queued from the worker thread) actually run.
            gate.set()
            for _ in range(100):
                app.processEvents()
                time.sleep(0.02)

            assert tab.sprite_labels["p1b"].pixmap()  # Blastoise landed
            assert not tab.sprite_labels["p1a"].pixmap()  # Sneasler -> text

            # Clean shutdown: worker told to stop, thread joined, refs dropped.
            tab._shutdown_sprite_thread()
            assert tab._sprite_thread is None
            assert tab._sprite_worker is None
            assert not thread_a.isRunning()
        finally:
            gate.set()
            tab._shutdown_sprite_thread()
            # Stop any playing animations so no QMovie holds a sprite
            # file open while the temp cache dir is removed.
            for slot in ("p1a", "p1b", "p2a", "p2b"):
                tab._clear_slot_sprite(slot)

    conn.close()
    print("PASS: overlapping sprite fetches reuse one thread and shut down cleanly")


def test_settings_dialog_updates_config_and_prunes():
    app = QApplication.instance() or QApplication(sys.argv)
    conn = init_db(":memory:")
    import_replay(
        conn, load_log("game1_win_vs_datrandomguy787.json"),
        "https://replay.pokemonshowdown.com/gen9championsvgc2026regmbbo3-2647404582",
        my_usernames={"salmoncashew"},
    )

    # A throwaway config dir so the real user config is never touched.
    with tempfile.TemporaryDirectory() as tmp_dir:
        base_dir = Path(tmp_dir)
        # Defaults are reflected in a fresh dialog.
        dialog = SettingsDialog(conn, base_dir=base_dir)
        assert dialog.store_logs_check.isChecked()
        assert dialog.retention_spin.value() == config.DEFAULT_LOG_RETENTION_DAYS
        assert dialog.use_sprites_check.isChecked()  # gen-5 sprites default on

        # Turning storage off (and sprites off) and OKing wipes the
        # logs immediately and persists both settings.
        dialog.store_logs_check.setChecked(False)
        dialog.use_sprites_check.setChecked(False)
        dialog.accept()
        assert config.get_store_logs(base_dir) is False
        assert config.get_use_sprites(base_dir) is False
        assert conn.execute("SELECT COUNT(*) FROM logs").fetchone()[0] == 0

        # A fresh dialog reflects the saved settings.
        dialog2 = SettingsDialog(conn, base_dir=base_dir)
        assert not dialog2.store_logs_check.isChecked()
        assert not dialog2.use_sprites_check.isChecked()

    conn.close()
    print("PASS: settings dialog persists store_logs and prunes logs on OK")


def test_stats_tab_side_options_track_chart_type():
    """
    "Opponent's Pokemon" and "Both" should only ever be offered while
    a stat with mine_only=False is selected -- confirms Move Usage
    drops them to just "My Pokemon", and that having "Opponent's
    Pokemon" selected at the time of the switch falls back to "My
    Pokemon" instead of leaving an invalid filter silently active.
    """
    app = QApplication.instance() or QApplication(sys.argv)
    conn = init_db(":memory:")
    tab = StatsTab(conn)

    opponent_index = tab.side_filter.findData("opponent")
    tab.side_filter.setCurrentIndex(opponent_index)
    assert tab.side_filter.currentData() == "opponent"

    tab.chart_picker.setCurrentText("Move Usage")
    assert tab.side_filter.count() == 1, "Move Usage should only offer 'My Pokemon'"
    assert tab.side_filter.currentData() == "mine", "a stale 'opponent' selection should fall back to My Pokemon"

    tab.chart_picker.setCurrentText("Turns on Field")
    assert tab.side_filter.count() == 3, "switching back to a chart without mine_only should restore all options"

    print("PASS: the Side dropdown's options track the selected chart's mine_only setting")


def test_stats_tab_team_filter_defaults_to_most_recent_team():
    """
    A fresh Stats tab (nothing picked yet) should default the Team
    filter to the most recent team rather than "All teams" -- and it
    should RE-default to the newest team on each refresh until the
    user makes an explicit choice. Once the user picks something,
    that choice (including a deliberate "All teams") is restored
    instead.
    """
    app = QApplication.instance() or QApplication(sys.argv)
    conn = init_db(":memory:")

    older_team_id = resolve_team_id(conn, [
        {"species": "Landorus-Therian", "item": "", "moves": []},
        {"species": "Rillaboom", "item": "", "moves": []},
    ])
    conn.execute(
        "INSERT INTO games (game_id, replay_url, my_side, team_id) VALUES (?, ?, ?, ?)",
        ("gen9vgc2026rega-1000000001", "https://example.com/older", "p1", older_team_id),
    )

    newer_team_id = resolve_team_id(conn, [
        {"species": "Flutter Mane", "item": "", "moves": []},
        {"species": "Chien-Pao", "item": "", "moves": []},
    ])
    conn.execute(
        "INSERT INTO games (game_id, replay_url, my_side, team_id) VALUES (?, ?, ?, ?)",
        ("gen9vgc2026rega-9999999999", "https://example.com/newer", "p1", newer_team_id),
    )

    tab = StatsTab(conn)
    assert tab.team_filter.currentData() == newer_team_id, \
        f"expected the most recent team ({newer_team_id}), got {tab.team_filter.currentData()}"

    # With no explicit team choice, a refresh re-defaults to the newest team.
    tab.refresh_everything()
    assert tab.team_filter.currentData() == newer_team_id, \
        f"after refresh, expected the most recent team, got {tab.team_filter.currentData()}"

    # An explicit "All teams" choice is restored on later refreshes, not re-defaulted.
    tab.team_filter.setCurrentIndex(0)  # "All teams" -- a real user interaction
    tab.regulation_filter.setCurrentIndex(0)  # force a refresh
    assert tab.team_filter.currentData() is None
    tab.refresh_everything()
    assert tab.team_filter.currentData() is None, \
        f"expected the explicit 'All teams' choice to be preserved, got {tab.team_filter.currentData()}"

    conn.close()
    print("PASS: a fresh Stats tab defaults the Team filter to the most recent team, and an explicit choice is preserved")


def test_stats_tab_every_registered_chart_renders_without_crashing():
    """
    Cycles through every entry in REGISTRY (not just the two that
    have their own dedicated tests above) against real data, with a
    team selected -- confirms the full GUI wiring (chart_picker
    selection -> refresh_chart -> data_fn -> render_fn) works
    end-to-end for each one, so a new stat added to the registry
    without a bespoke GUI test still gets basic crash coverage here
    for free.
    """
    app = QApplication.instance() or QApplication(sys.argv)
    conn = init_db(":memory:")
    import_replay(
        conn, load_log("game1_win_vs_datrandomguy787.json"),
        "https://replay.pokemonshowdown.com/gen9championsvgc2026regmbbo3-2647404582",
        my_usernames={"salmoncashew"},
    )
    tab = StatsTab(conn)

    team_index = tab.team_filter.findData(1)
    tab.team_filter.setCurrentIndex(team_index)

    for chart_name in REGISTRY:
        tab.chart_picker.setCurrentText(chart_name)
        assert len(tab.figure.get_axes()) >= 1, f"{chart_name}: expected at least one axis drawn"

    conn.close()
    print(f"PASS: every registered chart ({', '.join(REGISTRY.keys())}) renders without crashing")


def test_replays_tab_sequential_playback_and_countdown_chips():
    """
    The four approved replay-viewer fixes:
      (A) the board guarantees room for both rows (minimum size),
      (B) autoplay advances ONE battle-ordered event per tick and the
          movelog renders the turn's events in log order,
      (C) condition chips carry no emoji icons,
      (D) weather / terrain / room / side chips show their turn countdowns.
    """
    app = QApplication.instance() or QApplication(sys.argv)
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

    tab = ReplaysTab(conn, usernames={"salmoncashew"}, auto_fetch_sprites=False, animate=False)

    # game2 is selected explicitly (the list is sorted by battle number,
    # not import order); nothing played yet.
    tab.game_list.setCurrentRow(_replays_row_for(
        tab, "gen9championsvgc2026regmbbo3-2647349643"))
    assert tab._frame_index == 0
    assert tab._event_index == 0

    # (A) the board must NOT force a ~900x600 minimum (that ballooned the
    # whole window); it stays small enough to fit where it's placed, lays
    # both rows out below each other instead of clipping the bottom row
    # out of existence, and is always fully visible (no scroll area).
    tab.show()
    app.processEvents()
    mins = tab.board_container.minimumSize()
    assert mins.width() < 600 and mins.height() < 500, \
        f"board still forces a huge minimum: {mins.width()}x{mins.height()}"
    assert tab.width() < 900, f"tab forced too wide: {tab.width()}px"
    back = tab._back_side
    top_side = "p2" if back == "p1" else "p1"
    top = tab.cell_widgets[top_side + "a"].geometry()
    bot = tab.cell_widgets[back + "b"].geometry()
    assert bot.y() > top.y(), "bottom row must be laid out below the top row"
    assert bot.width() > 0 and bot.height() > 0, "bottom-row cells must have real geometry"
    # Every tile keeps its HP bar out from under its sprite: the sprite's
    # bottom edge must sit at or above the HP bar's top edge (the sprite is
    # the taller, centered widget; the HP bar is pinned to the tile bottom).
    for slot in tab.sprite_labels:
        sprite = tab.sprite_labels[slot].geometry()
        hp = tab.hp_bars[slot].geometry()
        assert hp.top() >= sprite.bottom(), \
            f"{slot}: sprite overlaps its HP bar (sprite bottom {sprite.bottom()} vs hp top {hp.top()})"

    # Frame 0 is the pre-battle entry; step to turn 1 so the movelog
    # below exercises a real battle turn.
    tab.step_forward()
    assert tab._frame_index == 1
    assert tab._event_index == 0

    # (B) the movelog lists this turn's events in the exact battle order
    # the timeline recorded them in.
    events = tab._frame()["events"]
    n0 = len(events)
    assert n0 >= 1
    text = tab.event_browser.toPlainText()
    idx = 0
    for ev in events:
        if ev[0] == "move":
            j = text.find(f"used {ev[4]}", idx)
            assert j != -1, f"movelog missing {ev[4]!r}"
            idx = j + 1

    # One event at a time: each autoplay tick consumes exactly one event.
    tab._advance_one_event()
    assert tab._event_index == 1
    assert tab._frame()["turn"] == 1  # still inside the same turn
    highlighted = False
    blk = tab.event_browser.document().firstBlock()
    while blk.isValid():
        if blk.blockFormat().background().color().name() == "#fff0c2":
            highlighted = True
            break
        blk = blk.next()
    assert highlighted, "the just-replayed event should be highlighted in the movelog"

    # Exhausting the turn's events advances the player to the next turn
    # (one extra call per turn to trigger the boundary).
    while tab._frame()["turn"] == 1:
        tab._advance_one_event()
    assert tab._frame()["turn"] > 1
    assert tab._event_index <= 1

    # Stepping back is turn-level: back to turn 1, movelog reset.
    tab.step_back()
    assert tab._frame()["turn"] == 1
    assert tab._event_index == 0

    # (C + D) Tailwind shows its turn countdown and no emoji icons.
    side_text = tab.side_cond_labels["p2"].text()
    assert "Tailwind" in side_text
    assert "(4)" in side_text, side_text
    assert not _contains_emoji(side_text), side_text
    assert not _contains_emoji(tab.conditions_label.text())

    # Turn 4: Harsh sunlight chip carries its own countdown (5 left) and
    # still no emoji.
    while tab._frame()["turn"] < 4:
        tab.step_forward()
    cond_text = tab.conditions_label.text()
    assert "Harsh sunlight" in cond_text
    assert "(5)" in cond_text, cond_text
    assert not _contains_emoji(cond_text), cond_text

    conn.close()
    print("PASS: replays tab plays one battle event at a time with countdown chips (no emoji)")


def test_replays_tab_layout_bob_deferral_shield_and_decode_fallback():
    """The remaining replay-viewer fixes:
      (1) each tile orders its children by row -- your side reads
          name-BELOW-sprite, the opponent name-ABOVE-sprite -- so neither
          row ends up with a name crammed into the middle,
      (2) idle bobs don't start until the board is actually laid out
          (geometry is (0,0) before the widget is shown), which was what
          flung sprites into the tile corner,
      (3) a Protect-style shield pops in and PERSISTS for the rest of its
          turn, dropping only when the next turn renders,
      (4) if the preferred animated sprite can't be decoded, the slot
          falls back to the static variant instead of going blank.
    """
    app = QApplication.instance() or QApplication(sys.argv)
    conn = init_db(":memory:")
    import_replay(
        conn, load_log("game1_win_vs_datrandomguy787.json"),
        "https://replay.pokemonshowdown.com/gen9championsvgc2026regmbbo3-2647404582",
        my_usernames={"salmoncashew"},
    )

    with tempfile.TemporaryDirectory() as tmp_dir:
        def downloader(url):
            if "sneasler" in url or "gen5ani" in url or "/ani/" in url:
                return None
            return _PNG_BYTES

        store = SpriteStore(base_dir=Path(tmp_dir), downloader=downloader)
        for species, back in [
            # game1's pre-battle leads (frame 0) plus the turn-1 mega form.
            ("Blastoise", False), ("Basculegion", True),
            ("Blastoise-Mega", False), ("Whimsicott", True),
            ("Sneasler", False), ("Sneasler", True),
        ]:
            store.get_or_fetch(species, back)

        tab = ReplaysTab(
            conn, usernames={"salmoncashew"}, sprite_store=store,
            auto_fetch_sprites=False, animate=True,
        )
        tab.game_list.setCurrentRow(_replays_row_for(
            tab, "gen9championsvgc2026regmbbo3-2647404582"))  # game1: salmoncashew is p2 (bottom row)

        # (1) Under the 5-layer layout each tile holds exactly its sprite;
        # the name + HP bar live in the top ui_layer (reparented out of the
        # tile so they stack above hazards/screens). The per-row convention
        # -- opponent name ABOVE the sprite, your name BELOW it -- is applied
        # by _place_ui_layer once the board is laid out.
        bot = tab.cell_widgets["p2a"].layout()
        order_bot = [bot.itemAt(i).widget() for i in range(bot.count()) if bot.itemAt(i).widget() is not None]
        assert order_bot == [tab.sprite_labels["p2a"]], \
            "a tile holds only its sprite under the 5-layer layout"

        top = tab.cell_widgets["p1a"].layout()
        order_top = [top.itemAt(i).widget() for i in range(top.count()) if top.itemAt(i).widget() is not None]
        assert order_top == [tab.sprite_labels["p1a"]], \
            "the opponent tile holds only its sprite too"
        assert tab.board_labels["p1a"].parent() is tab.ui_layer, \
            "opponent name renders in ui_layer, above hazards/screens"
        assert tab.board_labels["p2a"].parent() is tab.ui_layer, \
            "your name renders in ui_layer, above hazards/screens"
        assert tab.hp_bars["p1a"].parent() is tab.ui_layer, \
            "HP bars render in ui_layer, above hazards/screens"

        # (2) Sprites rendered, but the tab was never shown -> NO idle bobs
        # yet (geometry not laid out). Once it's shown, bobs start anchored
        # to real tile positions.
        assert tab.sprite_labels["p1b"].pixmap() or tab.sprite_labels["p2a"].pixmap()
        assert tab._idle_bob_timers == {}, "bobs must not start before layout is final"
        tab.showEvent(QShowEvent())
        QApplication.processEvents()
        QTest.qWait(20)
        for slot in ("p1b", "p2a"):
            assert tab._idle_bob_timers.get(slot) is not None, f"{slot} should bob after show"
        for slot in ("p1a", "p2b"):
            assert tab._idle_bob_timers.get(slot) is None, f"{slot} has no art to bob"

        # (2b) Re-rendering the same frame must KEEP already-shown sprites,
        # never blank them. Regression: the static fallback used to treat
        # _set_slot_sprite's "already showing this exact file" as a decode
        # failure, so the second render of any frame with real art wiped the
        # slot (that is why the app showed NO sprites at all).
        tab._render_frame()
        assert bool(tab.sprite_labels["p1b"].pixmap() or tab.sprite_labels["p2a"].pixmap()), \
            "re-rendering a frame must keep its already-shown sprites, not blank them"
        assert ("p1b" in tab._slot_sprite_key or "p2a" in tab._slot_sprite_key)

        # (3) A shield pops in and PERSISTS through the rest of the turn,
        # only dropping when the next turn actually renders.
        tab._spawn_shield("p2b")
        QTest.qWait(300)  # longer than the pop-in (_EFFECT_SHIELD_MS ~220)
        assert tab._shields.get("p2b") is not None, "shield should survive its pop-in"
        shield = tab._shields["p2b"]
        assert shield in tab._effect_widgets, "shield must not be retired after the pop-in"
        QTest.qWait(500)
        assert tab._shields.get("p2b") is shield, "shield must hold until the turn's render clears it"
        tab.step_forward()  # renders the next turn -> _clear_turn_effects runs
        assert shield not in tab._effect_widgets, "the next turn's render must retire the old shield"
        assert tab._shields.get("p2b") is not shield

        # (4) An animated file that can't be decoded falls back to static.
        bad = SpriteStore(base_dir=Path(tmp_dir), downloader=downloader)
        bad.path_for("whimsicott", False, True).write_bytes(b"not-an-image-0x00000000")  # undecodable
        bad.path_for("whimsicott", False, False).write_bytes(_PNG_BYTES)  # valid static
        tab._sprite_store = bad
        assert tab._set_best_sprite("p1a", "Whimsicott", False)
        assert tab.sprite_labels["p1a"].movie() is None, "animated file is undecodable, should fall back to static"
        assert tab.sprite_labels["p1a"].pixmap(), "static fallback should render a pixmap"
        # Release the sprite with the failed QMovie so the temp cache dir
        # isn't held open on Windows.
        tab._clear_slot_sprite("p1a")
        gc.collect()
        QApplication.processEvents()

    conn.close()
    print("PASS: replay viewer name placement, deferred bobs, persistent shields, and decode fallback")


def test_replays_tab_substitute_dolls_fold_and_popups():
    """The A-G viewer backlog: the pre-battle 'turn 0' frame shows the leads
    before the first move; the movelog folds a move's own damage/popup lines
    under it (item C); an immune partner joins the move's target list with
    its move-time species (D+F); a Substitute doll appears beside the mon and
    persists in the frame snapshot (E); item/ability reveals float as popup
    badges and read as movelog lines (G); and a bare |fail| line flags the
    active protect (B)."""
    app = QApplication.instance() or QApplication(sys.argv)
    conn = init_db(":memory:")
    log = "\n".join([
        "|player|p1|Alice|user|1",
        "|player|p2|Bob|user|2",
        "|switch|p1a: Corviknight|Corviknight, L50, M|100/100",
        "|switch|p1b: Blastoise|Blastoise, L50, M|100/100",
        "|switch|p2a: Garchomp|Garchomp, L50, M|100/100",
        "|switch|p2b: Aerodactyl|Aerodactyl, L50, M|100/100",
        "|turn|1",
        "|move|p2a: Garchomp|Earthquake|p1a: Corviknight|[spread] p1a,p1b",
        "|-immune|p2b: Aerodactyl",
        "|-damage|p1a: Corviknight|75/100",
        "|-damage|p1b: Blastoise|80/100",
        "|-ability|p2a: Garchomp|Rough Skin",
        "|move|p1a: Corviknight|Protect|p1a: Corviknight",
        "|fail|p1a: Corviknight",
        "|move|p1a: Corviknight|Substitute|p1a: Corviknight",
        "|-damage|p1a: Corviknight|70/100|[from] submission",
        "|-substitute|p1a: Corviknight|70/100",
        "|move|p1b: Blastoise|Flip Turn|p2a: Garchomp",
        "|-damage|p2a: Garchomp|40/100",
        "|-damage|p1b: Blastoise|92/100|[from] ability: Rough Skin|[of] p2a: Garchomp",
        "|switch|p1b: Sneasler|Sneasler, L50, M|100/100|[from] Flip Turn",
        "|turn|2",
        "|move|p2b: Aerodactyl|Iron Head|p1a: Corviknight",
        "|-enditem|p2a: Garchomp|Choice Scarf",
        "|win|Alice",
    ])
    import_replay(
        conn, log,
        "https://replay.pokemonshowdown.com/gen9doublesou-fake",
        my_usernames={"Bob"},
    )
    with tempfile.TemporaryDirectory() as tmp_dir:
        def downloader(url):
            if "gen5ani" in url or "/ani/" in url:
                return None
            return _PNG_BYTES
        store = SpriteStore(base_dir=Path(tmp_dir), downloader=downloader)
        for species, back in [
            ("Corviknight", False), ("Blastoise", False),
            ("Garchomp", True), ("Aerodactyl", True),
        ]:
            store.get_or_fetch(species, back)
        tab = ReplaysTab(
            conn, usernames={"Bob"}, sprite_store=store,
            auto_fetch_sprites=False, animate=True,
        )

        # (A) Frame 0 is the pre-battle entry: the leads only.
        assert tab._frame()["turn"] == 0
        assert [e[0] for e in tab._frame()["events"]] == ["switch"] * 4

        tab.step_forward()
        assert tab._frame()["turn"] == 1

        # (C + F) The movelog folds the move's own lines under it and lists
        # the immune partner as a hit target.
        text = tab.event_browser.toPlainText()
        assert "used Earthquake on Corviknight, Blastoise and Aerodactyl!" in text, text
        assert "Garchomp's Rough Skin activated!" in text, text

        # A mid-turn switch (Flip Turn pulling the mover out) can't rewrite
        # who took the hit: Blastoise took the Rough Skin damage even though
        # Sneasler runs in later that same turn, and the ability popup stays
        # credited to its owner's name.
        assert "Blastoise used Flip Turn on Garchomp!" in text, text
        assert "Blastoise took damage (92/100)!" in text, text
        assert "Sneasler took damage" not in text, text

        # (E) Substitute doll: raised on the slot and snapshotted in the frame.
        assert "Corviknight made a Substitute!" in text
        assert tab._frames[1]["substitutes"] == {"p1a": True}
        assert "p1a" in tab._sub_dolls
        # The real mon fades behind its doll while the substitute is up.
        sub_eff = tab.sprite_labels["p1a"].graphicsEffect()
        assert isinstance(sub_eff, QGraphicsOpacityEffect), (
            f"substitute user's sprite should carry an opacity effect, got "
            f"{type(sub_eff).__name__}"
        )
        assert abs(sub_eff.opacity() - _SUB_OPACITY) < 1e-6, sub_eff.opacity()
        # ...and returns to full opacity once the doll is removed.
        tab._set_sub_doll("p1a", False)
        assert tab.sprite_labels["p1a"].graphicsEffect() is None, \
            "the fade should clear when the substitute doll is removed"

        # (B) The bare |fail| line flagged the active protect.
        failed = [e for e in tab._frames[1]["events"]
                  if e[0] == "move" and e[4] == "Protect"][0]
        assert failed[-1] is True

        # (G) Item reveals float as popup badges and read as movelog lines.
        tab.step_forward()
        assert tab._frame()["turn"] == 2
        assert "Garchomp's Choice Scarf was consumed!" in tab.event_browser.toPlainText()
        tab._play_event_effect(("popup", "p2a", "item", "Choice Scarf"))
        assert any(p.text() == "Choice Scarf" for p in tab._effect_widgets), tab._effect_widgets

    conn.close()
    print("PASS: turn-0 entry, folded movelog, substitute dolls, immune targets, and popups")


def test_replays_tab_hazards_render_on_field_and_layers_differ():
    """
    The entry-hazard system: Spikes, Stealth Rock, Reflect, Light Screen,
    Aurora Veil, Sticky Web and Toxic Spikes are painted onto the
    battlefield scene (Showdown-style) instead of only reading as text
    chips. Distinct stack depths draw distinct pictograms -- one layer of
    Spikes reads clearly lighter than a full x3 stack -- and the board's
    scene overlay becomes a drawn (non-null) pixmap once a side has any
    hazards or screens up.
    """
    from fourslice.gui.replays_tab import (
    _make_hazard_overlay, _make_scene_overlay, _make_side_cond_icon,
)
    from PySide6.QtGui import QImage, qAlpha
    app = QApplication.instance() or QApplication(sys.argv)

    def painted(img):
        """Count of fully-opaque or translucent-but-drawn pixels -- the
        amount of a pictogram a layer actually paints."""
        img = img.convertToFormat(QImage.Format_ARGB32)
        return sum(1 for y in range(img.height()) for x in range(img.width())
                   if qAlpha(img.pixel(x, y)) != 0)

    # (A) Pure pictogram distinctness -- "different numbers of spikes show
    # different outputs" at the icon level: each extra Spikes / Toxic Spikes
    # layer paints unambiguously MORE (and a differently-arranged) image, and
    # pictograms for different hazards aren't interchangeable either.
    spike = [_make_side_cond_icon("Spikes", n).toImage() for n in (1, 2, 3)]
    s_pix = list(map(painted, spike))
    assert s_pix[0] < s_pix[1] < s_pix[2], f"Spikes must grow with depth: {s_pix}"
    toxic = [_make_side_cond_icon("Toxic Spikes", n).toImage() for n in (1, 2)]
    t_pix = list(map(painted, toxic))
    assert t_pix[0] < t_pix[1], f"Toxic Spikes must grow with depth: {t_pix}"
    assert painted(_make_side_cond_icon("Stealth Rock", 1).toImage()) != s_pix[0]
    assert painted(_make_side_cond_icon("Sticky Web", 1).toImage()) != s_pix[0]

    # (B) The hazards+screens overlay bakes screens AND hazards in, is
    # layer-aware, and null on a clear field; the background scene is
    # weather/room/terrain only, null without any scenery.
    sc = {"p1": ["Stealth Rock", "Spikes"], "p2": ["Reflect", "Toxic Spikes"]}
    one = _make_hazard_overlay(400, 320, sc,
                               {"p1": {"Spikes": 1}, "p2": {"Toxic Spikes": 1}}, "p1")
    three = _make_hazard_overlay(400, 320, sc,
                                 {"p1": {"Spikes": 3}, "p2": {"Toxic Spikes": 1}}, "p1")
    assert not one.isNull(), "hazards+screens must render a non-null overlay"
    assert painted(one.toImage()) != painted(three.toImage()), \
        "the hazards+screens overlay must change with Spikes depth"
    assert _make_hazard_overlay(400, 320, {}, {}, "p1").isNull(), \
        "a clear field should paint no hazards+screens overlay"
    only_screen = _make_hazard_overlay(400, 320, {"p1": ["Reflect"]}, {}, "p1")
    assert not only_screen.isNull(), \
        "a screen alone still renders in the hazards+screens overlay"
    assert _make_scene_overlay(400, 320, None, None, None).isNull(), \
        "the background scene is weather-only and null without scenery"

    # (C) End-to-end through the real frame data: a stored game whose log
    # stacks Spikes works its way into the board's scene overlay as a drawn
    # pixmap (not left as the empty scene).
    conn = init_db(":memory:")
    log = "\n".join([
        "|player|p1|Alice|user|1",
        "|player|p2|Bob|user|2",
        "|switch|p1a: Ferrothorn|Ferrothorn, L50, M|100/100",
        "|switch|p1b: Blissey|Blissey, L50, M|100/100",
        "|switch|p2a: Garchomp|Garchomp, L50, M|100/100",
        "|switch|p2b: Landorus-Therian|Landorus-Therian, L50, M|100/100",
        "|turn|1",
        "|move|p1a: Ferrothorn|Stealth Rock|p2a: Garchomp",
        "|-sidestart|Side1|Stealth Rock",
        "|move|p2a: Garchomp|Spikes|p1a: Ferrothorn",
        "|-sidestart|Side2|Spikes",
        "|-sidestart|Side2|Spikes",
        "|-sidestart|Side2|Spikes",
        "|move|p1b: Blissey|Reflect",
        "|-sidestart|p1: Alice|Reflect",
        "|turn|2",
        "|win|Alice",
    ])
    import_replay(
        conn, log,
        "https://replay.pokemonshowdown.com/gen9doublesou-hazard-test",
        my_usernames={"Bob"},
    )
    tab = ReplaysTab(conn, usernames={"Bob"}, auto_fetch_sprites=False, animate=False)
    # Frame 0 is the pre-battle entry (the lead switches); frame 1 is turn 1,
    # where Stealth Rock + Reflect went up on p1 and Spikes stacked x3 on p2.
    frame = tab._frames[1]
    assert frame["side_conditions"]["p1"] == ["Reflect", "Stealth Rock"], \
        frame["side_conditions"]
    assert frame["side_condition_layers"]["p2"]["Spikes"] == 3, \
        "three logged Spikes sidestarts must stack to x3"

    tab.show()
    QApplication.processEvents()
    tab._update_conditions(tab._frames[1])
    # Hazards AND screens (Stealth Rock + Reflect p1, Spikes x3 p2) live in
    # the raised hazards+screens layer -- above sprites and move FX, below
    # the UI. It's non-null once the side conditions are up.
    hz_pix = tab.hz_overlay.pixmap()
    assert hz_pix is not None and not hz_pix.isNull(), \
        "hazards+screens must land in the raised hz_overlay layer"
    assert tab.hz_overlay.isVisible(), \
        "the hazards+screens overlay is shown on top of the board"
    # The background scene is weather-only, so this no-weather frame must be
    # null (scenery lives there, not screens).
    assert tab.scene_overlay.pixmap() is None or tab.scene_overlay.pixmap().isNull(), \
        "the weather-only background scene stays empty without scenery"
    # The UI (names + HP bars) is reparented into the top ui_layer sibling,
    # the final layer above everything.
    assert hasattr(tab, "ui_layer"), "the UI layer must exist as the top sibling"
    assert tab.board_labels["p1a"].parent() is tab.ui_layer, \
        "name labels live in the UI layer, above hazards/screens"
    conn.close()
    print("PASS: entry hazards render on the field with visually distinct spike layers")


def test_replays_tab_hazards_position_by_format():
    """Hazard rows land where Showdown-style placement dictates once the
    board has real geometry: in DOUBLES each side's row dead-centers on
    its tile band (between the two mons, clear of the name/HP bars); in
    SINGLES each side's row sits directly on top of its lone Pokémon tile
    (overlapping the sprite).  All hazard rows are mildly transparent
    (drawn at _HAZARD_OPACITY) so they never fully occlude the sprites
    underneath."""
    from fourslice.gui.replays_tab import _HAZARD_OPACITY
    from PySide6.QtGui import QImage, qAlpha
    app = QApplication.instance() or QApplication(sys.argv)

    def painted_rows(img):
        """Rows with at least one drawn pixel (hazards only -- the logs
        below set no screens, so the only paint is the hazard strips)."""
        return {y for y in range(img.height())
                if any(qAlpha(img.pixel(x, y)) != 0 for x in range(img.width()))}

    def x_center(img, rows):
        """Horizontal center of the painted pixels on the given rows."""
        xs = [x for y in rows for x in range(img.width())
              if qAlpha(img.pixel(x, y)) != 0]
        return (min(xs) + max(xs)) // 2

    def build(log, name):
        conn = init_db(":memory:")
        import_replay(conn, log, "https://replay.pokemonshowdown.com/" + name,
                      my_usernames={"Bob"})
        tab = ReplaysTab(conn, usernames={"Bob"}, auto_fetch_sprites=False,
                         animate=False)
        tab.resize(860, 640)
        tab.show()
        # Let the grid layout settle, then render the turn-1 conditions.
        # Rendering sets a board stylesheet which itself triggers one more
        # grid relayout, so settle again and re-render: the overlay painted
        # on the second pass uses the same geometry we assert against.
        for _ in range(4):
            QApplication.processEvents()
            QTest.qWait(30)
        tab._update_conditions(tab._frames[1])
        for _ in range(4):
            QApplication.processEvents()
            QTest.qWait(30)
        tab._update_conditions(tab._frames[1])
        img = tab.hz_overlay.pixmap().toImage().convertToFormat(QImage.Format_ARGB32)
        return conn, tab, img

    # ---- Singles: opponent's row just below its tile, user's just above ----
    singles = "\n".join([
        "|player|p1|Alice|user|1",
        "|player|p2|Bob|user|2",
        "|switch|p1a: Ferrothorn|Ferrothorn, L50, M|100/100",
        "|switch|p2a: Garchomp|Garchomp, L50, M|100/100",
        "|turn|1",
        "|move|p1a: Ferrothorn|Stealth Rock|p2a: Garchomp",
        "|-sidestart|Side1|Stealth Rock",
        "|move|p2a: Garchomp|Stealth Rock|p1a: Ferrothorn",
        "|-sidestart|Side2|Stealth Rock",
        "|turn|2",
        "|win|Alice",
    ])
    conn, tab, img = build(singles, "gen9ou-hazard-position-singles")
    assert tab._game_type == "singles", tab._game_type
    top_a = tab.cell_widgets["p1a"].geometry()
    bottom_a = tab.cell_widgets["p2a"].geometry()
    rows = painted_rows(img)
    # Singles: _paint_side_hazards dead-centers each side's strip directly
    # on top of that side's lone Pokémon tile (overlapping the sprite), so
    # the painted rows must sit inside the tile's vertical span, centered on
    # its middle -- never drifting into the gap below/above.
    def check_tile(tile, label):
        half_rows = {y for y in rows
                     if (tile.center().y() < img.height() // 2 and y < img.height() // 2)
                     or (tile.center().y() >= img.height() // 2 and y >= img.height() // 2)}
        assert half_rows, f"{label}: hazards must paint inside their own half"
        assert min(half_rows) >= tile.top(), \
            f"{label}: strip spills above the tile ({sorted(half_rows)}, {tile})"
        assert max(half_rows) <= tile.bottom(), \
            f"{label}: strip spills below the tile ({sorted(half_rows)}, {tile})"
        assert abs((min(half_rows) + max(half_rows)) // 2 - tile.center().y()) <= 2, \
            f"{label}: vertical dead-center ({sorted(half_rows)}, {tile})"

    check_tile(top_a, "opponent")
    check_tile(bottom_a, "user")
    # Every painted pixel sits inside one of the two tiles -- no edge-pinned
    # fallback strips anywhere.
    tile_span = ({y for y in range(top_a.top(), top_a.bottom() + 1)}
                 | {y for y in range(bottom_a.top(), bottom_a.bottom() + 1)})
    assert rows <= tile_span, (rows - tile_span,
                               "stray hazard paint outside the tile-anchored rows")
    # Each strip is horizontally anchored on its tile's center and not
    # floating toward the board edge.
    strip_hw = 20
    top_rows = rows & {y for y in range(top_a.top(), top_a.bottom() + 1)}
    bot_rows = rows & {y for y in range(bottom_a.top(), bottom_a.bottom() + 1)}
    def _check_x(rows_in_band, tile, label):
        xs = [x for y in rows_in_band for x in range(img.width())
              if qAlpha(img.pixel(x, y)) != 0]
        if not xs:
            return
        lo, hi = min(xs), max(xs)
        assert lo >= tile.center().x() - strip_hw, \
            f"{label}: strip too far left (x_min={lo})"
        assert hi <= tile.center().x() + strip_hw, \
            f"{label}: strip too far right (x_max={hi})"
    _check_x(top_rows, top_a, "opponent strip x")
    _check_x(bot_rows, bottom_a, "user strip x")
    conn.close()

    # ---- Doubles: each row dead-centers on its side's tile band ----
    doubles = "\n".join([
        "|player|p1|Alice|user|1",
        "|player|p2|Bob|user|2",
        "|switch|p1a: Ferrothorn|Ferrothorn, L50, M|100/100",
        "|switch|p1b: Blissey|Blissey, L50, M|100/100",
        "|switch|p2a: Garchomp|Garchomp, L50, M|100/100",
        "|switch|p2b: Landorus-Therian|Landorus-Therian, L50, M|100/100",
        "|turn|1",
        "|move|p1a: Ferrothorn|Stealth Rock|p2a: Garchomp",
        "|-sidestart|Side1|Stealth Rock",
        "|move|p2a: Garchomp|Stealth Rock|p1a: Ferrothorn",
        "|-sidestart|Side2|Stealth Rock",
        "|turn|2",
        "|win|Alice",
    ])
    conn, tab, img = build(doubles, "gen9doublesou-hazard-position-doubles")
    assert tab._game_type == "doubles", tab._game_type
    top_band = tab.cell_widgets["p1a"].geometry().united(
        tab.cell_widgets["p1b"].geometry())
    bottom_band = tab.cell_widgets["p2a"].geometry().united(
        tab.cell_widgets["p2b"].geometry())
    w, h = img.width(), img.height()
    rows = painted_rows(img)

    def check_band(band, label):
        in_half = {y for y in rows
                   if (band.center().y() < h // 2 and y < h // 2)
                   or (band.center().y() >= h // 2 and y >= h // 2)}
        assert in_half, f"{label}: hazards must paint inside their own half"
        assert min(in_half) >= band.top() + 24, \
            f"{label}: strip overlaps the name bar ({sorted(in_half)}, {band})"
        assert max(in_half) <= band.bottom() - 22, \
            f"{label}: strip overlaps the HP bar ({sorted(in_half)}, {band})"
        assert abs((min(in_half) + max(in_half)) // 2 - band.center().y()) <= 2, \
            f"{label}: vertical dead-center ({sorted(in_half)}, {band})"
        assert abs(x_center(img, in_half) - band.center().x()) <= 20, \
            f"{label}: horizontal dead-center ({sorted(in_half)}, {band})"

    check_band(top_band, "opponent band")
    check_band(bottom_band, "user band")
    conn.close()
    print("PASS: hazard rows position by format -- singles mirrored, doubles centered")


def _stats_tab_with_samples():
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
    return StatsTab(conn), conn


def test_stats_tab_new_matchup_entries_sit_after_the_charts():
    """
    The three non-chart stats are selectable items in the existing
    Chart dropdown, APPENDED after every REGISTRY chart -- nothing is
    removed, reordered, or otherwise disturbed.
    """
    app = QApplication.instance() or QApplication(sys.argv)
    conn = init_db(":memory:")
    tab = StatsTab(conn)

    names = [tab.chart_picker.itemText(i) for i in range(tab.chart_picker.count())]
    assert names == list(REGISTRY.keys()) + ["Top 6 Opponents", "Best/Worst Matchups", "Most Common Leads"], names
    assert tab.chart_picker.itemText(0) == "Turns on Field", "default selection should be untouched"

    conn.close()
    print("PASS: the new stats join the Chart dropdown after every existing chart")


def test_stats_tab_matchup_entry_renders_grid_and_centered_mon():
    """
    Selecting 'Top 6 Opponents' locks Result and Side to a single
    disabled N/A, offers every fought opponent in the Pokemon filter,
    renders the six-card grid, and collapses to one centered card when
    a mon is picked.
    """
    app = QApplication.instance() or QApplication(sys.argv)
    tab, conn = _stats_tab_with_samples()

    tab.chart_picker.setCurrentText("Top 6 Opponents")
    assert tab.view_stack.currentWidget() is tab.matchup_view

    for combo in (tab.result_filter, tab.side_filter):
        assert combo.count() == 1 and combo.itemText(0) == "N/A", "Result/Side should lock to N/A"
        assert not combo.isEnabled(), "the locked filter must be disabled"

    mon_names = {tab.mon_filter.itemText(i) for i in range(1, tab.mon_filter.count())}
    assert mon_names == {"Sneasler", "Blastoise", "Aerodactyl", "Garchomp", "Charizard", "Sylveon"}, mon_names

    cards = tab.matchup_view.findChildren(MonCard)
    assert len(cards) == 6, f"expected the 6-card grid, got {len(cards)}"
    assert sorted(c._mon for c in cards) == ["Aerodactyl", "Blastoise", "Charizard", "Garchomp", "Sneasler", "Sylveon"]

    tab.mon_filter.setCurrentIndex(tab.mon_filter.findText("Sneasler"))
    centered = tab.matchup_view.findChildren(MonCard)
    assert len(centered) == 1 and centered[0]._mon == "Sneasler", \
        f"a mon selection should collapse the grid to one centered card, got {len(centered)}"
    assert (centered[0].record._wins, centered[0].record._losses) == (1, 0)

    conn.close()
    print("PASS: Top 6 Opponents locks filters, lists opponent mons, renders the grid, and collapses to a single card")


def test_stats_tab_best_worst_toggle_and_min_games_empty_state():
    """
    'Best/Worst Matchups' carries a Best/Worst toggle and keeps the
    N/A locks; with the sample data (1 game per opponent) it shows the
    min-4-games empty-state message in both directions.
    """
    app = QApplication.instance() or QApplication(sys.argv)
    tab, conn = _stats_tab_with_samples()

    tab.chart_picker.setCurrentText("Best/Worst Matchups")
    assert tab.view_stack.currentWidget() is tab.best_worst_view
    assert not tab.result_filter.isEnabled() and not tab.side_filter.isEnabled()
    assert [b.text() for b in tab.best_worst_view.toggle.buttons] == ["Best", "Worst"]

    msg = tab.best_worst_view._message
    assert msg is not None and "need >= 4" in msg.text(), f"got {msg.text() if msg else None}"

    tab.best_worst_view.toggle.buttons[1].click()  # flip to Worst
    assert tab.best_worst_view._message is not None and "need >= 4" in tab.best_worst_view._message.text()

    conn.close()
    print("PASS: Best/Worst shows the toggle, keeps N/A locks, and renders the min-games empty state both ways")


def test_stats_tab_leads_renders_double_pairs_and_pairs_a_team_mon():
    """
    'Most Common Leads' defaults to the dominant battle size (doubles
    here), renders one LeadCard per pair, and the Pokemon filter is the
    selected team's roster -- picking a mon narrows to just the pairs
    it led with, in win% order.
    """
    app = QApplication.instance() or QApplication(sys.argv)
    tab, conn = _stats_tab_with_samples()

    tab.chart_picker.setCurrentText("Most Common Leads")
    assert tab.view_stack.currentWidget() is tab.leads_view
    assert tab.leads_view.size_note.text() == "Doubles leads"

    pairs = tab.leads_view.findChildren(LeadCard)
    assert len(pairs) == 2, f"expected the two double pairs, got {len(pairs)}"
    # cards render arts in the game's slot order (lead_a, lead_b): game1's
    # win pair is Basculegion+Sneasler, game2's loss pair Whimsicott+Basculegion
    assert sorted(tuple(a.name_label.text() for a in c._arts) for c in pairs) == [
        ("Basculegion", "Sneasler"), ("Whimsicott", "Basculegion"),
    ]

    roster = [tab.mon_filter.itemText(i) for i in range(tab.mon_filter.count())]
    assert roster == ["All Pokemon", "Whimsicott", "Sneasler", "Basculegion", "Incineroar", "Pyroar", "Garchomp"], roster
    assert tab.mon_filter.isEnabled(), "with a team selected and doubles leads present, the roster filter should work"

    tab.mon_filter.setCurrentIndex(tab.mon_filter.findText("Basculegion"))
    filtered = tab.leads_view.findChildren(LeadCard)
    assert len(filtered) == 2, "Basculegion led with both partners"
    first = tuple(a.name_label.text() for a in filtered[0]._arts)
    assert first == ("Basculegion", "Sneasler"), f"pairs ordered by win% desc, got {first}"

    conn.close()
    print("PASS: Leads renders double pairs and the roster filter narrows to that mon's winning pairs")


def test_stats_tab_returning_from_a_matchup_restores_chart_filters():
    """
    Leaving a new stat back to a real chart must restore Result and
    Side to their full, enabled option sets (the N/A sentinel lock
    never leaks), and the Pokemon filter back to chart-scoped choices.
    """
    app = QApplication.instance() or QApplication(sys.argv)
    tab, conn = _stats_tab_with_samples()

    tab.chart_picker.setCurrentText("Top 6 Opponents")
    assert not tab.result_filter.isEnabled()

    tab.chart_picker.setCurrentText("Turns on Field")
    assert tab.view_stack.currentIndex() == 0
    assert tab.result_filter.isEnabled()
    assert tab.result_filter.count() == 4, "a bar chart should offer All results, Wins, Losses, Difference"
    assert tab.side_filter.isEnabled()
    assert tab.side_filter.count() == 3, "a non-mine_only chart should offer all three side options"
    assert tab.side_filter.currentData() == "mine", "the restore must not resurrect 'Both' via the N/A lock"
    assert tab.mon_filter.findData("Incineroar") >= 0, "the Pokemon filter should be back on chart-scoped options"

    conn.close()
    print("PASS: returning to a real chart restores Result/Side/Pokemon filters with no leakage")


def test_record_label_colors_follow_win_percentage():
    """
    The record label renders W-L (pct%): the win number is healthy, the
    loss number danger, and the percentage flips healthy/danger at the 50%
    threshold -- driven by the theme's HEALTHY/DANGER tokens.
    """
    app = QApplication.instance() or QApplication(sys.argv)
    tokens = THEME.get_tokens()
    healthy, danger = tokens["HEALTHY"], tokens["DANGER"]

    lbl = RecordLabel()
    lbl.set_record(3, 1)  # 75% -> healthy pct
    assert lbl.text().count(f'color:{healthy};') == 2, f"win number + a >=50% pct are healthy ({healthy})"
    assert lbl.text().count(f'color:{danger};') == 1, f"loss number is danger ({danger})"

    lbl.set_record(1, 3)  # 25% -> danger pct
    assert lbl.text().count(f'color:{danger};') == 2, f"loss number + a <50% pct are danger ({danger})"
    assert lbl.text().count(f'color:{healthy};') == 1, f"win number is still healthy ({healthy})"

    print("PASS: the record label colors wins healthy, losses danger, and the pct by the 50% threshold")


if __name__ == "__main__":
    test_import_tab_constructs()
    test_import_handles_fetch_failure_without_crashing()
    test_stats_tab_constructs()
    test_stats_tab_result_options_track_chart_type()
    test_stats_tab_cascading_filters()
    test_stats_tab_mon_filter_disables_for_move_usage()
    test_stats_tab_filters_are_searchable()
    test_main_window_assembles_all_tabs()
    test_replays_tab_constructs_and_plays_back()
    test_replays_tab_pruned_logs_show_placeholder()
    test_replays_tab_shows_cached_gen5_sprites_and_falls_back_to_text()
    test_replays_tab_overlapping_sprite_fetches_reuse_one_thread_and_shut_down_cleanly()
    test_replays_tab_renders_from_the_users_point_of_view()
    test_replays_tab_board_animations()
    test_replays_tab_move_effects_and_conditions()
    test_replays_tab_sequential_playback_and_countdown_chips()
    test_replays_tab_layout_bob_deferral_shield_and_decode_fallback()
    test_replays_tab_substitute_dolls_fold_and_popups()
    test_replays_tab_hazards_render_on_field_and_layers_differ()
    test_replays_tab_hazards_position_by_format()
    test_settings_dialog_updates_config_and_prunes()
    test_stats_tab_side_options_track_chart_type()
    test_stats_tab_team_filter_defaults_to_most_recent_team()
    test_stats_tab_every_registered_chart_renders_without_crashing()
    test_imports_page_sync_wires_background_worker_and_reenables_button()
    test_start_data_refresh_async_completes_cleanly()
    test_refresh_done_bridge_reloads_teams_tab_and_sidebar()
    test_teams_tab_reload_catalog_is_safe_with_unchanged_data()
    test_stats_tab_new_matchup_entries_sit_after_the_charts()
    test_stats_tab_matchup_entry_renders_grid_and_centered_mon()
    test_stats_tab_best_worst_toggle_and_min_games_empty_state()
    test_stats_tab_leads_renders_double_pairs_and_pairs_a_team_mon()
    test_stats_tab_returning_from_a_matchup_restores_chart_filters()
    test_record_label_colors_follow_win_percentage()
    print("\nAll GUI smoke tests passed.")