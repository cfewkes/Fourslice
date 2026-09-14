"""
fourslice/gui/stats_widgets.py

Non-chart stat views for the Statistics page's new dropdown entries:

  Top 6 Opponents  -> MatchupGridView   (3x2 grid of records, or one centered card)
  Best/Worst       -> BestWorstView     (same grid, Best/Worst toggle, min-4 gate)
  Common Leads     -> LeadsView         (lead pairs/singles + record)

Each view owns its data querying (see fourslice/stats/matchups.py) and
exposes `refresh(conn, regulation, team_id, mon)`; internal toggles just
re-run the stored filter args. Records render W-L (pct%) with the win
number green, the loss number red, and pct green when >= 50 (theme
tokens HEALTHY / DANGER), exactly as the Statistics-page spec requires.
"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame, QGridLayout, QHBoxLayout, QLabel, QVBoxLayout, QWidget,
)

from fourslice.gui.imports_page import (
    THEME, FONT_STACK, SegmentedControl,
)
from fourslice.gui.sidebar import _get_pokemon_sprite
from fourslice.stats.matchups import (
    opponent_records, top_n_opponents, best_worst_matchups, lead_records,
    dominant_battle_size, doubles_lead_with_teammates,
)
from fourslice.stats.brought_record import brought_records


ART_SIZE = 84


# ---------------------------------------------------------------------------
# Record label  --  "3-1 (75%)" with per-part colors
# ---------------------------------------------------------------------------

class RecordLabel(QLabel):
    """Renders 'W-L (pct%)'. Wins green, losses red, the percentage
    green when >= 50 else red; separators take the theme's BODY token.
    The percentage color depends only on the number, so `render` is a
    pure text change -- no layout churn on theme switch."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._wins = 0
        self._losses = 0
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)

    def set_record(self, wins: int, losses: int) -> "RecordLabel":
        self._wins = int(wins)
        self._losses = int(losses)
        self.render()
        return self

    def render(self):
        tokens = THEME.get_tokens()
        green = tokens["HEALTHY"]
        red = tokens["DANGER"]
        total = self._wins + self._losses
        pct = 100.0 * self._wins / total if total else 0.0
        pct_color = green if pct >= 50 else red
        body = tokens["BODY"]
        self.setText(
            f'<span style="color:{green}; font-weight:700;">{self._wins}</span>'
            f'<span style="color:{body};">-</span>'
            f'<span style="color:{red}; font-weight:700;">{self._losses}</span>'
            f' <span style="color:{body};">(</span>'
            f'<span style="color:{pct_color}; font-weight:700;">{pct:.0f}%</span>'
            f'<span style="color:{body};">)</span>'
        )
        self.setStyleSheet(f"color: {body}; background: transparent; font-family: {FONT_STACK}; font-size: 13px;")


# ---------------------------------------------------------------------------
# cards  --  one matchup cell / one lead (up to two mons side by side)
# ---------------------------------------------------------------------------

class MonCard(QFrame):
    """One card: artwork (or letter placeholder), bold name, and a
    W-L (pct%) RecordLabel. 'mon' drives the artwork; the record is set
    via set_record. Uses minimum size so it scales with available space."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("MonCard")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setMinimumSize(160, 160)
        self._mon = None
        self._has_pixmap = False

        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(4)

        self.art_label = QLabel()
        self.art_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.art_label, 1)

        self.name_label = QLabel()
        self.name_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.name_label)

        self.record = RecordLabel()
        lay.addWidget(self.record)

        self.apply_theme(THEME.get_tokens())

    def set_data(self, mon: str, wins: int, losses: int) -> "MonCard":
        self._mon = mon
        self.name_label.setText(mon)
        self.record.set_record(wins, losses)
        self._set_art()
        return self

    def _set_art(self):
        tokens = THEME.get_tokens()
        pm = _get_pokemon_sprite(self._mon, ART_SIZE)
        self._has_pixmap = pm is not None
        if pm is not None:
            self.art_label.setPixmap(pm)
            self.art_label.setFixedSize(pm.size())
            self.art_label.setStyleSheet("background: transparent;")
        else:
            self.art_label.setText(self._mon[:4].strip() or "?")
            self.art_label.setFixedSize(ART_SIZE, ART_SIZE)
            self.art_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.art_label.setStyleSheet(
                f"background-color: {tokens['TRACK']}; color: {tokens['MUTED']};"
                f"border-radius: {ART_SIZE // 2}px;"
                f"font-family: {FONT_STACK}; font-size: {ART_SIZE // 4}px; font-weight: 700;"
            )

    def apply_theme(self, tokens) -> None:
        self.setStyleSheet(
            f"QFrame#MonCard {{ background-color: {tokens['WHITE']};"
            f" border: 1px solid {tokens['BORDER']}; border-radius: 12px; }}"
        )
        self.name_label.setStyleSheet(
            f"color: {tokens['INK']}; background: transparent;"
            f"font-family: {FONT_STACK}; font-size: 14px; font-weight: 700;"
        )
        if self._mon is not None and not self._has_pixmap:
            tokens_now = THEME.get_tokens()
            self.art_label.setStyleSheet(
                f"background-color: {tokens_now['TRACK']}; color: {tokens_now['MUTED']};"
                f"border-radius: {ART_SIZE // 2}px;"
                f"font-family: {FONT_STACK}; font-size: {ART_SIZE // 4}px; font-weight: 700;"
            )
        self.record.render()


class _MonArt(QWidget):
    """One mini (artwork + name) column inside a LeadCard."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("MonArt")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        self.art_label = QLabel()
        self.art_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.art_label)
        self.name_label = QLabel()
        self.name_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.name_label)
        self._mon = None
        self._has_pixmap = False

    def set_mon(self, mon: str) -> "_MonArt":
        self._mon = mon
        self.name_label.setText(mon)
        self._set_art()
        return self

    def _set_art(self):
        pm = _get_pokemon_sprite(self._mon, ART_SIZE)
        self._has_pixmap = pm is not None
        if pm is not None:
            self.art_label.setPixmap(pm)
            self.art_label.setFixedSize(pm.size())
            self.art_label.setStyleSheet("background: transparent;")
        else:
            tokens = THEME.get_tokens()
            self.art_label.setText(self._mon[:4].strip() or "?")
            self.art_label.setFixedSize(ART_SIZE, ART_SIZE)
            self.art_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.art_label.setStyleSheet(
                f"background-color: {tokens['TRACK']}; color: {tokens['MUTED']};"
                f"border-radius: {ART_SIZE // 2}px;"
                f"font-family: {FONT_STACK}; font-size: {ART_SIZE // 4}px; font-weight: 700;"
            )

    def apply_theme(self, tokens) -> None:
        self.setStyleSheet("QWidget#MonArt { background: transparent; }")
        self.name_label.setStyleSheet(
            f"color: {tokens['INK']}; background: transparent;"
            f"font-family: {FONT_STACK}; font-size: 13px; font-weight: 600;"
        )
        if self._mon is not None and not self._has_pixmap:
            tokens_now = THEME.get_tokens()
            self.art_label.setStyleSheet(
                f"background-color: {tokens_now['TRACK']}; color: {tokens_now['MUTED']};"
                f"border-radius: {ART_SIZE // 2}px;"
                f"font-family: {FONT_STACK}; font-size: {ART_SIZE // 4}px; font-weight: 700;"
            )


class LeadCard(QFrame):
    """A matchup-style card holding ONE or TWO (art + name) sets side by
    side -- doubles leads show both partners -- with a W-L (pct%) footer.
    Uses minimum size so it scales with available space."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("LeadCard")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setMinimumSize(260, 160)
        self._arts: list[_MonArt] = []
        self.record = RecordLabel()

        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(4)
        self.row = QHBoxLayout()
        self.row.setSpacing(10)
        lay.addLayout(self.row, 1)
        self.record.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.record)

        self.apply_theme(THEME.get_tokens())

    def set_leads(self, mons: list[str], wins: int, losses: int) -> "LeadCard":
        self._clear_arts()
        for mon in mons:
            art = _MonArt(self)
            art.set_mon(mon)
            self.row.addWidget(art, 1)
            self._arts.append(art)
        self.row.addStretch(1)
        self.record.set_record(wins, losses)
        return self

    def _clear_arts(self):
        while self.row.count():
            item = self.row.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        self._arts = []

    def apply_theme(self, tokens) -> None:
        self.setStyleSheet(
            f"QFrame#LeadCard {{ background-color: {tokens['WHITE']};"
            f" border: 1px solid {tokens['BORDER']}; border-radius: 12px; }}"
        )
        for art in self._arts:
            art.apply_theme(tokens)
        self.record.render()


# ---------------------------------------------------------------------------
# shared view scaffolding
# ---------------------------------------------------------------------------

def _clear_layout(layout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        w = item.widget()
        if w is not None:
            w.setParent(None)
            w.deleteLater()
        sub = item.layout()
        if sub is not None:
            _clear_layout(sub)


class _StatView(QWidget):
    """Base for the three stat views: a muted title row up top, then a
    rebuildable body (message / grid of cards / one centered card) that
    scales with the available window space."""

    def __init__(self, title_text: str, parent=None):
        super().__init__(parent)
        self._last = None
        self._cards: list[QWidget] = []
        self._message: QLabel | None = None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 20)
        lay.setSpacing(12)

        self.header_row = QHBoxLayout()
        self.header_row.setSpacing(12)
        self.title_label = QLabel(title_text)
        self.header_row.addWidget(self.title_label)
        self.header_row.addStretch(1)
        lay.addLayout(self.header_row)

        self.body_layout = QVBoxLayout()
        lay.addLayout(self.body_layout, 1)

        self.apply_theme(THEME.get_tokens())

    def apply_theme(self, tokens) -> None:
        self.title_label.setStyleSheet(
            f"color: {tokens['MUTED']}; background: transparent;"
            f"font-family: {FONT_STACK}; font-size: 13px; font-weight: 600;"
        )
        for lbl in self.findChildren(QLabel):
            if lbl.property("stat_note"):
                lbl.setStyleSheet(
                    f"color: {tokens['MUTED']}; background: transparent;"
                    f"font-family: {FONT_STACK}; font-size: 12px;"
                )
        if self._message is not None:
            self._message.setStyleSheet(
                f"color: {tokens['MUTED']}; background: transparent;"
                f"font-family: {FONT_STACK}; font-size: 14px; padding: 24px;"
            )
        for card in self._cards:
            card.apply_theme(tokens)

    def refresh(self, conn, regulation=None, team_id=None, mon=None) -> None:
        self._last = (conn, regulation, team_id, mon)
        self._rebuild()

    def _rebuild(self):
        raise NotImplementedError

    def _clear_body(self) -> None:
        _clear_layout(self.body_layout)
        self._cards = []
        self._message = None

    def _show_message(self, text: str) -> None:
        self._clear_body()
        msg = QLabel(text)
        msg.setProperty("stat_note", True)
        msg.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._message = msg
        self.body_layout.addWidget(msg, 1)
        self.apply_theme(THEME.get_tokens())

    def _show_grid(self, cards: list[QWidget]) -> None:
        self._clear_body()
        self._cards = cards
        host = QWidget()
        grid = QGridLayout(host)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(16)
        for i, card in enumerate(cards):
            grid.addWidget(card, i // 3, i % 3, Qt.AlignmentFlag.AlignCenter)
        # push the used rows up and spread columns evenly when < 6 cards
        grid.setRowStretch(0, 0)
        grid.setRowStretch(1, 0)
        grid.setRowStretch(2, 1)
        wrapper = QWidget()
        hl = QHBoxLayout(wrapper)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.addStretch(1)
        hl.addWidget(host)
        hl.addStretch(1)
        self.body_layout.addWidget(wrapper, 1)

    def _show_centered(self, card: QWidget) -> None:
        self._clear_body()
        self._cards = [card]
        host = QWidget()
        v = QVBoxLayout(host)
        v.setContentsMargins(0, 0, 0, 0)
        v.addStretch(1)
        v.addWidget(card, 0, Qt.AlignmentFlag.AlignCenter)
        v.addStretch(1)
        self.body_layout.addWidget(host, 1)


# ---------------------------------------------------------------------------
# the three views
# ---------------------------------------------------------------------------

class MatchupGridView(_StatView):
    """Top 6 Opponents: a 3x2 grid of the mons fought most (by games
    seen), each with its record. A mon filter selection replaces the
    grid with one centered card for that mon."""

    def __init__(self, parent=None):
        super().__init__("Top 6 Opponents", parent)

    def _rebuild(self):
        if self._last is None:
            return
        conn, regulation, team_id, mon = self._last

        if mon:
            df = opponent_records(conn, regulation=regulation, team_id=team_id)
            df = df[df["mon"] == mon]
            if df.empty:
                self._show_message("No data for this filter")
                return
            r = df.iloc[0]
            card = MonCard().set_data(r["mon"], int(r["wins"]), int(r["losses"]))
            self._show_centered(card)
            return

        df = top_n_opponents(conn, regulation=regulation, team_id=team_id)
        if df.empty:
            self._show_message("No data for this filter")
            return
        cards = [
            MonCard().set_data(r["mon"], int(r["wins"]), int(r["losses"]))
            for _, r in df.iterrows()
        ]
        self._show_grid(cards)



class BroughtRecordView(_StatView):
    """Brought Record: a grid of your team's roster, with the record of
    when each Pokemon was brought to battle. A mon filter selection
    replaces the grid with one centered card for that mon."""

    def __init__(self, parent=None):
        super().__init__("Brought Record", parent)

    def _rebuild(self):
        if self._last is None:
            return
        conn, regulation, team_id, mon = self._last

        if team_id is None:
            self._show_message("Select a team to view brought records")
            return

        df = brought_records(conn, regulation=regulation, team_id=team_id)
        if df.empty:
            self._show_message("No data for this team")
            return

        if mon:
            df = df[df["mon"] == mon]
            if df.empty:
                self._show_message("No data for this filter")
                return
            r = df.iloc[0]
            card = MonCard().set_data(r["mon"], int(r["wins"]), int(r["losses"]))
            self._show_centered(card)
            return

        cards = [
            MonCard().set_data(r["mon"], int(r["wins"]), int(r["losses"]))
            for _, r in df.iterrows()
        ]
        self._show_grid(cards)


class BestWorstView(_StatView):
    """Best/Worst Matchups: same card grid as the opponents view, but
    ranked by win% against you with a minimum of 4 games. The toggle
    flips between the six highest (Best) and lowest (Worst) win%s."""

    def __init__(self, parent=None):
        super().__init__("Best / Worst Matchups", parent)
        self.note = QLabel("min 4 games")
        self.note.setProperty("stat_note", True)
        self.header_row.addWidget(self.note)
        self.toggle = SegmentedControl(["Best", "Worst"], active_index=0, height=32)
        self.toggle.currentChanged.connect(lambda _t: self._rebuild())
        self.header_row.addWidget(self.toggle)
        self.apply_theme(THEME.get_tokens())

    def _rebuild(self):
        if self._last is None:
            return
        conn, regulation, team_id, mon = self._last

        if mon:
            df = opponent_records(conn, regulation=regulation, team_id=team_id)
            df = df[df["mon"] == mon]
            if df.empty:
                self._show_message("No data for this filter")
                return
            r = df.iloc[0]
            card = MonCard().set_data(r["mon"], int(r["wins"]), int(r["losses"]))
            self._show_centered(card)
            return

        all_records = opponent_records(conn, regulation=regulation, team_id=team_id)
        if all_records.empty:
            self._show_message("No data for this filter")
            return
        best = self.toggle.buttons[0].isChecked()
        q = best_worst_matchups(conn, best=best, regulation=regulation, team_id=team_id)
        if q.empty:
            self._show_message("Not enough games against any Pokemon (need >= 4)")
            return
        cards = [
            MonCard().set_data(r["mon"], int(r["wins"]), int(r["losses"]))
            for _, r in q.iterrows()
        ]
        self._show_grid(cards)


class LeadsView(_StatView):
    """Most Common Leads: the more-common battle size's leads with their
    records. Doubles leads are a pair (two artworks); singles leads are
    one. A mon filter (team roster, doubles only) selects one mon and
    shows it paired with every other teammate, by win% descending."""

    def __init__(self, parent=None):
        super().__init__("Most Common Leads", parent)
        self.size_note = QLabel("")
        self.size_note.setProperty("stat_note", True)
        self.header_row.addWidget(self.size_note)
        self.apply_theme(THEME.get_tokens())

    def _rebuild(self):
        if self._last is None:
            return
        conn, regulation, team_id, mon = self._last

        buckets = lead_records(conn, regulation=regulation, team_id=team_id)
        size = dominant_battle_size(buckets)
        if size is None:
            self.size_note.setText("")
            self._show_message("No data for this filter")
            return
        self.size_note.setText("Doubles leads" if size == "doubles" else "Singles leads")

        if size == "doubles" and mon is not None and team_id is not None:
            df = doubles_lead_with_teammates(conn, mon, team_id, regulation=regulation)
            if df.empty:
                self._show_message("No data for this filter")
                return
            rows = df
        else:
            bucket = buckets["doubles" if size == "doubles" else "singles"]
            rows = bucket.head(6)
        if rows.empty:
            self._message = None
            self._show_message("No data for this filter")
            return

        cards = []
        for _, r in rows.iterrows():
            mons = [r["lead_a"]]
            if r["lead_b"] is not None and pd_notna(r["lead_b"]):
                mons.append(r["lead_b"])
            cards.append(LeadCard().set_leads(mons, int(r["wins"]), int(r["losses"])))
        self._show_grid(cards)


def pd_notna(value) -> bool:
    """QSS/None-safe isna: works for pandas NaN scalars AND None/""."""
    if value is None:
        return False
    try:
        from pandas import isna
        return not bool(isna(value))
    except Exception:
        return value != ""