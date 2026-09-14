"""
fourslice/gui/pokemon_info.py

A standalone "Pokémon in format" info window for the sidebar's Top Pokemon
list: clicking a row opens a non-modal popup showing that Pokémon's commonly
used moves, items, abilities, checks/counters and teammates for the currently
selected format, with a header like "Kingambit in gen9ou".

The three small square/row builders live here too -- they are shared by the
Team Builder's EditPanel (via delegation) so the two surfaces render with the
same visual recipe and never drift apart. This module imports only
imports_page + teambuilder_data so it can be pulled in by both sidebar.py and
teams_tab.py without an import cycle (teams_tab already imports imports_page).
"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QScrollArea, QVBoxLayout, QWidget,
)

from fourslice.gui.imports_page import (
    THEME, FONT_STACK, PALE_VIOLET, INK, MUTED, WHITE,
)
from fourslice import teambuilder_data as td


# ---------------------------------------------------------------------------
# Shared square/row builders (source of truth; EditPanel delegates to these)
# ---------------------------------------------------------------------------

def kv_label(label_text):
    """A small muted uppercase-ish section label."""
    lbl = QLabel(label_text)
    lbl.setStyleSheet(f"color: {MUTED}; font-size: 11px; font-weight: 700; font-family: {FONT_STACK};")
    return lbl


def build_list_square(title, height=240, min_height=None, bg=PALE_VIOLET):
    """A titled card with a scrollable list body (the meta squares). `height`
    fixes the card's height (omit for a stretch card, e.g. Items); `min_height`
    floors a stretch card so it can't collapse to nothing. Returns (card,
    items_lay) where items_lay is the layout rows are added to."""
    card = QFrame()
    card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
    if height is not None:
        card.setFixedHeight(height)
    if min_height is not None:
        card.setMinimumHeight(min_height)
    card.setStyleSheet(f"QFrame {{ background-color: {bg}; border-radius: 12px; }}")
    lay = QVBoxLayout(card)
    lay.setContentsMargins(16, 14, 16, 14)
    lay.setSpacing(8)
    lay.addWidget(kv_label(title))

    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    scroll.setStyleSheet("background: transparent;")
    body = QWidget()
    body.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
    body.setStyleSheet("background: transparent;")
    items_lay = QVBoxLayout(body)
    items_lay.setContentsMargins(0, 0, 0, 0)
    items_lay.setSpacing(2)
    scroll.setWidget(body)
    lay.addWidget(scroll)
    return card, items_lay


def meta_row(name, value, compact=False):
    """One name / value line inside a meta square. Compact rows use tighter
    margins and a smaller font so three of them fit the abilities box."""
    tokens = THEME.get_tokens()
    row = QWidget()
    row.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
    h = QHBoxLayout(row)
    h.setContentsMargins(8, 2, 8, 2) if compact else h.setContentsMargins(8, 5, 8, 5)
    h.setSpacing(8)
    size = 11 if compact else 12
    name_lbl = QLabel(name)
    name_lbl.setStyleSheet(f"color: {tokens['INK']}; font-family: {FONT_STACK};"
                           f" font-size: {size}px; font-weight: 600;")
    val_lbl = QLabel(value)
    val_lbl.setStyleSheet(f"color: {tokens['MUTED']}; font-family: {FONT_STACK};"
                          f" font-size: {size}px; font-weight: 700;")
    h.addWidget(name_lbl)
    h.addStretch(1)
    h.addWidget(val_lbl)
    return row


def _clear_layout(lay):
    while lay.count():
        item = lay.takeAt(0)
        if item.widget():
            item.widget().deleteLater()


# ---------------------------------------------------------------------------
# PokeInfoWindow
# ---------------------------------------------------------------------------

class PokeInfoWindow(QWidget):
    """A separate, non-modal snapshot of one Pokémon's usage detail in one
    format/elo: the same five meta squares the Team Builder shows, but for a
    sidebar row. Deliberately independent of the Team Builder -- it neither
    reads nor mutates any team slot. Closing hides the window instead of
    deleting it (no WA_DeleteOnClose), so the Sidebar's dict entry can re-raise
    the same window on a later click of the same row."""

    # Square heights mirror EditPanel's meta area (Counters/Teammates/Moves
    # mid-sized, Abilities fits exactly 3 compact rows, Items stretches).
    _COUNTERS_H = 200
    _TEAMMATES_H = 200
    _MOVES_H = 200
    _ABILITIES_H = 110
    _ITEMS_MIN_H = 88

    def __init__(self, name, format_id, elo="0", theme_manager=None, parent=None):
        super().__init__(parent)
        self.tm = theme_manager or THEME
        self._name = name
        self._format = format_id
        self._elo = elo

        # No WA_DeleteOnClose: closing simply hides the window so the sidebar
        # can re-show the same instance for the same (name, format, elo) later.
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setWindowTitle(f"{name} — {format_id} (elo {elo})")
        self.resize(620, 620)

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(10)

        self.header = QLabel(f"{name} in {format_id}")
        root.addWidget(self.header)

        self.subtitle = QLabel(f"{format_id} · Elo {elo}")
        root.addWidget(self.subtitle)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet("background: transparent;")
        container = QWidget()
        container.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        container.setStyleSheet("background: transparent;")
        cols = QHBoxLayout(container)
        cols.setContentsMargins(0, 0, 0, 0)
        cols.setSpacing(12)

        left = QVBoxLayout()
        left.setSpacing(12)
        self.counters_card, self.counters_lay = build_list_square("Counters", height=self._COUNTERS_H)
        left.addWidget(self.counters_card)
        self.abilities_card, self.abilities_lay = build_list_square("Abilities", height=self._ABILITIES_H)
        left.addWidget(self.abilities_card)
        self.items_card, self.items_lay = build_list_square(
            "Items", height=None, min_height=self._ITEMS_MIN_H)
        left.addWidget(self.items_card, 1)
        cols.addLayout(left, 1)

        right = QVBoxLayout()
        right.setSpacing(12)
        self.teammates_card, self.teammates_lay = build_list_square("Teammates", height=self._TEAMMATES_H)
        right.addWidget(self.teammates_card)
        self.moves_card, self.moves_lay = build_list_square("Moves", height=self._MOVES_H)
        right.addWidget(self.moves_card)
        cols.addLayout(right, 1)

        scroll.setWidget(container)
        root.addWidget(scroll, 1)

        self.empty_lbl = QLabel("")
        self.empty_lbl.setVisible(False)
        root.addWidget(self.empty_lbl)

        # Per-square fetch spec, copied from EditPanel._refresh_meta_squares
        # (teams_tab.py) -- keep these two in lockstep. Note items is also
        # compact + skips the aggregated "Other" entry.
        fmt_pct = lambda v: f"{float(v):.1f}%"
        fmt_score = lambda v: f"{float(v):g}"
        self._squares = []
        for spec in (
            (self.counters_lay,  td.checks_counters_for, "score", fmt_score, None, False, False),
            (self.teammates_lay, td.teammates_for,       "pct",   fmt_pct,   None, False, False),
            (self.moves_lay,     td.moves_for,           "pct",   fmt_pct,   None, False, True),
            (self.abilities_lay, td.abilities_for,       "pct",   fmt_pct,   3,    True,  True),
            (self.items_lay,     td.items_for,           "pct",   fmt_pct,   None, True,  True),
        ):
            lay, fetch, key, fmt, cap, compact, skip_other = spec
            entries = [r for r in fetch(self._name, self._format, self._elo)
                       if r.get("name") and not (skip_other and r["name"] == "Other")]
            entries.sort(key=lambda r: float(r.get(key, 0) or 0), reverse=True)
            if cap:
                entries = entries[:cap]
            self._squares.append({
                "lay": lay, "entries": entries, "key": key, "fmt": fmt,
                "compact": compact,
            })
            self._set_rows(lay, entries, key, fmt, compact)

        if not any(sq["entries"] for sq in self._squares):
            self.empty_lbl.setText(f"No usage data for {name} in {format_id}")
            self.empty_lbl.setVisible(True)

        self.tm.theme_changed.connect(self._apply_theme)
        self._apply_theme()

    def _set_rows(self, lay, entries, key, fmt, compact):
        _clear_layout(lay)
        for r in entries:
            lay.addWidget(meta_row(r["name"], fmt(r.get(key, 0)), compact=compact))
        lay.addStretch(1)

    def _apply_theme(self, *_args):
        tokens = self.tm.get_tokens()
        self.setStyleSheet(f"background-color: {tokens['WHITE']};")
        self.header.setStyleSheet(
            f"color: {tokens['INK']}; font-family: {FONT_STACK}; font-size: 20px; font-weight: 800;")
        self.subtitle.setStyleSheet(
            f"color: {tokens['MUTED']}; font-family: {FONT_STACK}; font-size: 11px; font-weight: 700;")
        self.empty_lbl.setStyleSheet(
            f"color: {tokens['MUTED']}; font-family: {FONT_STACK}; font-size: 12px; font-weight: 600;")
        for card in (self.counters_card, self.teammates_card, self.moves_card,
                     self.abilities_card, self.items_card):
            card.setStyleSheet(
                f"QFrame {{ background-color: {tokens['PALE_VIOLET']}; border-radius: 12px; }}")
        # Rows bake token colors at build time; re-render so a theme switch
        # recolors them in place (same pattern as EditPanel._apply_theme).
        for sq in self._squares:
            self._set_rows(sq["lay"], sq["entries"], sq["key"], sq["fmt"], sq["compact"])