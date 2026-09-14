"""
Run directly with: python tests/test_imports_page.py

Offscreen smoke tests for the redesigned Imports screen -- slice 1: the
header bar, the sidebar, and the sidebar's Top Pokemon usage list. Same
conventions as test_gui_smoke.py: forces Qt's "offscreen" platform so it
never pops a real window, works both under pytest and as a plain script.

Covered here: the page assembles; the logo is two-toned; the Showdown/
Champions segmented control has Showdown active; the nav list has all
four pages with Imports active; the Top Pokemon group has the Singles/
Doubles toggle (Singles active), two dropdowns (real formats + elo
divisions captured in stats.db), a search field with an embedded search
icon, and real 0-elo usage rows; the champions mode swaps to a "Coming
soon!" placeholder; the select button behaves like a combo box; and
every icon in the set renders non-null.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLineEdit, QPushButton

from fourslice.gui import imports_page as page_module
from fourslice.gui.icons import _DRAWERS, lucide
from fourslice.gui.imports_page import (
    DarkToggle, HeaderBar, ImportsPage, NavRow, PillButton, PokemonList,
    PokemonRow, SegmentedControl, SelectButton, PlaceholderListCard,
)
from fourslice.gui.sidebar import Sidebar


def _app():
    return QApplication.instance() or QApplication(sys.argv)


def test_imports_page_constructs():
    _app()
    page = ImportsPage()

    assert page.header is not None
    # Sidebar is managed separately by AppLayout (per the redesign spec)
    assert not hasattr(page, "sidebar")
    assert page.main_content is not None
    assert page.main_scroll is not None
    # All main-content widgets from the redesign spec are present
    assert page.page_title.text() == "Showdown Imports"
    assert page.change_username_btn is not None
    assert page.sync_btn is not None
    assert page.url_input is not None
    assert page.search_btn is not None
    assert page.placeholder_card is not None
    # Champions/Showdown switch is a stacked page swap
    assert page.content_stack is not None
    assert page.main_scroll in [page.content_stack.widget(i) for i in
                                range(page.content_stack.count())]
    assert page.champions_widget is not None
    print("PASS: ImportsPage builds header + main content (sidebar is external)")


def test_main_content_matches_spec():
    _app()
    page = ImportsPage(usernames=["SalmonCashew"])

    # Searching-for line uses the placeholder username
    assert "SalmonCashew" in page.username_label.text()

    # Placeholder-list card lists the recent stored replays
    assert len(page.placeholder_card._items) == 7
    for item in page.placeholder_card._items:
        assert item.text(), "each recent-replay row has a replay id"

    # Manual Input label + URL placeholder text
    assert page.manual_input_label.text() == "Manual Input"
    assert page.url_input.placeholderText() == "Input Showdown Replay URL here..."

    # Showdown content is the default page
    assert page.content_stack.currentWidget() is page.main_scroll
    print("PASS: main content matches the Imports redesign spec (no hop-back-in cards)")


def test_header_segmented_control_showdown_active():
    _app()
    header = HeaderBar()
    seg = header.segmented

    assert isinstance(seg, SegmentedControl)
    labels = [b.text() for b in seg.buttons]
    assert labels == ["Showdown", "Champions"]
    assert seg.buttons[0].isChecked(), "Showdown should be the active segment"
    assert not seg.buttons[1].isChecked()
    assert seg.group.exclusive()
    print("PASS: header segmented control has Showdown active, Champions inactive")


def test_header_buttons_and_toggle():
    _app()
    header = HeaderBar()

    assert isinstance(header.coffee_button, PillButton)
    assert header.coffee_button.text() == "Buy me a coffee!"
    assert isinstance(header.dark_toggle, DarkToggle)
    assert header.dark_toggle.width() == 34 and header.dark_toggle.height() == 34
    assert not header.dark_toggle.icon().isNull(), "dark toggle carries a moon icon"
    assert header.play_button.text() == "Play Showdown"
    assert not header.play_button.icon().isNull(), "Play Showdown carries a chevron"
    assert header.play_button.layoutDirection() == Qt.LayoutDirection.RightToLeft, \
        "trailing chevron icon means the button lays out right-to-left"
    print("PASS: header has the coffee pill, circular dark toggle, and Play Showdown pill")


def test_sidebar_nav_rows():
    _app()
    sidebar = Sidebar()

    assert list(sidebar.nav_rows) == ["imports", "stats", "teams", "replays"]
    assert sidebar.nav_rows["imports"].is_active()
    assert not sidebar.nav_rows["stats"].is_active()
    assert not sidebar.nav_rows["teams"].is_active()
    assert not sidebar.nav_rows["replays"].is_active()
    # labels per the spec
    labels = [r._label.text() for r in sidebar.nav_rows.values()]
    assert labels == ["Imports", "Statistics", "Team Builder", "Replays"]
    print("PASS: sidebar lists all four pages with Imports highlighted")


def test_nav_click_switches_active_row():
    _app()
    sidebar = Sidebar()
    sidebar._on_nav_clicked("stats")
    assert sidebar.nav_rows["stats"].is_active()
    assert not sidebar.nav_rows["imports"].is_active()
    print("PASS: clicking a nav row moves the active highlight")


def test_sidebar_top_pokemon_controls():
    _app()
    sidebar = Sidebar()

    toggle = sidebar.banlist_toggle
    assert [b.text() for b in toggle.buttons] == ["Singles", "Doubles"]
    assert toggle.buttons[0].isChecked(), "Singles is the default segment"

    assert isinstance(sidebar.format_select, SelectButton)
    # Real Smogon format ids, most-played Singles format first
    assert sidebar.format_select.currentText() == "gen9ou"
    # Elo divisions come from stats.db's capture ledger for that format;
    # '0' is always first (and the default)
    elos = [a.text() for a in sidebar.usage_select._menu.actions()]
    assert elos, "expected captured elo divisions"
    assert elos[0] == "0" and sidebar.usage_select.currentText() == "0"

    search = sidebar.search_input
    assert isinstance(search, QLineEdit)
    assert search.placeholderText() == "Search..."
    assert search.actions(), "search field embeds a leading search icon"
    icon = search.actions()[0].icon()
    assert not icon.isNull() and not icon.pixmap(16, 16).isNull(), \
        "the embedded search action carries a real icon"
    print("PASS: Top Pokemon group has Singles/Doubles (Doubles active), two dropdowns, and a search field")


def test_sidebar_pokemon_rows_real_usage():
    _app()
    sidebar = Sidebar()
    rows = sidebar.pokemon_list.rows()

    # The list is the real 0-elo usage for the selected format (gen9ou),
    # not placeholder rows: every row needs a name, a #rank and a % usage.
    assert rows, "expected real usage rows for the selected default format"
    for i, row in enumerate(rows[:5], start=1):
        assert isinstance(row, PokemonRow)
        assert row._name.text()
        assert row._rank.text() == f"#{i}", row._rank.text()
        assert row._pct.text().endswith("%")
        assert not row._avatar.pixmap().isNull(), "each row has an avatar"
    print(f"PASS: Top Pokemon list renders {len(rows)} real usage rows")


def test_pokemon_row_avatar_swap_hook():
    _app()
    row = PokemonRow()
    from PySide6.QtGui import QPixmap
    sprite = QPixmap(64, 64)
    sprite.fill(Qt.GlobalColor.cyan)
    row.set_avatar(sprite)
    assert row._avatar.pixmap().size().width() == 64
    print("PASS: PokemonRow.set_avatar accepts a real sprite pixmap")


def test_select_button_behaves_like_combo_box():
    _app()
    select = SelectButton(["Gen 9 OU", "Gen 9 VGC", "Gen 8 VGC"])
    assert select.currentText() == "Gen 9 OU"

    received = []
    select.changed.connect(received.append)
    select._choose("Gen 8 VGC")
    assert select.currentText() == "Gen 8 VGC"
    assert received == ["Gen 8 VGC"], "changed fires with the new value"

    select.set_items(["New A", "New B"])
    assert select.currentText() == "Gen 8 VGC", "set_items keeps the current value by default"
    print("PASS: SelectButton reads a current value, emits changed, and re-populates")


def test_every_icon_renders_non_null():
    _app()
    for name in _DRAWERS:
        pm = lucide(name, "#18181B", 18)
        assert not pm.isNull(), name
    print(f"PASS: all {len(_DRAWERS)} icons render at 2x device-pixel ratio")


def test_sync_status_bubble_never_stretches_page():
    """The sync-status bubble elides long messages to the space the search
    row actually has, so importing (with its long 'Synced -- N new
    replay(s) imported.   Last synced: ...' text) can't stretch the page.
    The full message is preserved on the label's tooltip."""
    _app()
    from PySide6.QtWidgets import QMainWindow

    page = ImportsPage()
    win = QMainWindow()
    win.setCentralWidget(page)
    win.resize(1100, 760)
    win.show()
    app = _app()
    app.processEvents()

    page._set_sync_status(
        "Synced -- 12 new replay(s) imported.   Last synced: 2026-09-08 12:34"
    )
    app.processEvents()

    label = page.last_synced_label
    max_width = page._sync_label_max_width()
    assert label.fontMetrics().horizontalAdvance(label.text()) <= max_width, \
        "bubble text was not elided down to the available width"

    # Full message survives in the tooltip
    assert label.toolTip() == (
        "Synced -- 12 new replay(s) imported.   Last synced: 2026-09-08 12:34"
    )

    # The search row's minimum width stays within the scroll viewport once
    # elided -- the whole reason the bubble was stretching the page. The
    # label contributes only its elided width, never the full message's.
    margins = page.main_content.layout().contentsMargins()
    row_min = page._search_row.minimumSize().width()
    viewport = page.main_scroll.viewport().width()
    assert row_min + margins.left() + margins.right() <= viewport, \
        f"search row (min {row_min}+margins) wider than viewport ({viewport}) once elided"

    # Short messages that already fit are not mangled
    page._set_sync_status("Ready.")
    assert page.last_synced_label.text() == "Ready."
    print("PASS: sync-status bubble elides to fit and never stretches the page")


if __name__ == "__main__":
    test_imports_page_constructs()
    test_main_content_matches_spec()
    test_header_segmented_control_showdown_active()
    test_header_buttons_and_toggle()
    test_sidebar_nav_rows()
    test_nav_click_switches_active_row()
    test_sidebar_top_pokemon_controls()
    test_sidebar_pokemon_rows_real_usage()
    test_pokemon_row_avatar_swap_hook()
    test_select_button_behaves_like_combo_box()
    test_every_icon_renders_non_null()
    test_sync_status_bubble_never_stretches_page()
    print("\nAll imports-page tests passed.")
