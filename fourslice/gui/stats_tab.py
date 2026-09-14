"""
fourslice/gui/stats_tab.py

The "Stats" tab: a chart picker plus filters shared across every
stat (regulation/team/pokemon/result/side), driving a matplotlib
canvas from the stats registry.

Redesigned to match the Imports screen design system:
pure-white background, styled card containers, theme tokens,
Lucide vector icons, and dynamic theme switching.
"""

from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.figure import Figure
from PySide6.QtCore import Qt, QThread, Slot
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QComboBox, QCompleter, QFileDialog, QFrame, QHBoxLayout, QLabel,
    QLineEdit, QMessageBox, QProgressDialog, QPushButton, QScrollArea,
    QStackedWidget, QVBoxLayout, QWidget,
)

from pathlib import Path

from fourslice import config
from fourslice.gui.export_worker import ExportWorker
from fourslice.gui.icons import lucide
from fourslice.gui.imports_page import (
    THEME, WHITE, INK, BODY, MUTED, VIOLET, PALE_VIOLET, BORDER, TRACK,
    FONT_STACK, PillButton,
)
from fourslice.gui.stats_widgets import MatchupGridView, BestWorstView, LeadsView, BroughtRecordView
from fourslice.stats import REGISTRY, DIFF_CAPABLE_CHART_TYPES
from fourslice.stats.matchups import every_opponent_mon, lead_records, dominant_battle_size
from fourslice.storage import get_teams_by_recency, get_mons_for_filter


# The three non-chart stats added to the Chart dropdown -- they are NOT
# matplotlib stats, so they must not be registered in REGISTRY (the
# canvas/chart machinery would try to call data_fn/render_fn on them).
# The GUI routes them to dedicated widget pages instead.
_MATCHUP_STATS = ("Top 6 Opponents", "Best/Worst Matchups", "Most Common Leads", "Brought Record")
_MATCHUP_PAGE = {name: index for index, name in enumerate(_MATCHUP_STATS, start=1)}
_NA_SENTINEL = "NA"  # non-None so findData() restore can never resurrect "Both" (data=None)


def _is_matchup(name):
    return name in _MATCHUP_STATS


def _make_searchable(combo: QComboBox) -> None:
    """
    Turns a QComboBox into a type-to-filter search box while
    preserving existing items.
    """
    combo.setEditable(True)
    combo.setInsertPolicy(QComboBox.NoInsert)
    completer = combo.completer()
    if completer:
        completer.setCaseSensitivity(Qt.CaseInsensitive)
        completer.setFilterMode(Qt.MatchContains)
        completer.setCompletionMode(QCompleter.PopupCompletion)


def _get_combo_qss(tokens):
    return f"""
        QComboBox {{
            background-color: {tokens['TRACK']};
            color: {tokens['INK']};
            border: 1px solid {tokens['BORDER']};
            border-radius: 8px;
            padding: 4px 10px;
            font-family: {FONT_STACK};
            font-size: 13px;
            min-height: 32px;
        }}
        QComboBox:hover {{
            border-color: {tokens['VIOLET']};
        }}
        QComboBox:disabled {{
            background-color: {tokens['TRACK']};
            color: {tokens['MUTED']};
            border-color: {tokens['BORDER']};
        }}
        QComboBox QAbstractItemView {{
            background-color: {tokens['WHITE']};
            color: {tokens['INK']};
            border: 1px solid {tokens['BORDER']};
            selection-background-color: {tokens['PALE_VIOLET']};
            selection-color: {tokens['VIOLET']};
            border-radius: 8px;
            padding: 4px;
        }}
    """


class StatsTab(QWidget):
    def __init__(self, conn):
        super().__init__()
        self.conn = conn

        # Background "Export for Power BI" state. The thread/worker/dialog
        # keep their references until thread.finished so Qt never destroys
        # a still-running thread (see _on_export_thread_finished).
        self._export_thread = None
        self._export_worker = None
        self._export_dialog = None
        self._export_cancel_btn = None  # our own button via setCancelButton (PySide6 lacks cancelButton())
        self._export_folder = None

        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(32, 28, 32, 28)
        main_layout.setSpacing(20)

        # -- Header Section --
        header_layout = QVBoxLayout()
        header_layout.setSpacing(4)
        
        self.title_label = QLabel("Statistics")
        header_layout.addWidget(self.title_label)

        self.subtitle_label = QLabel("Analyze match outcomes, move usage, lead rates, and performance across regulations and teams.")
        header_layout.addWidget(self.subtitle_label)
        main_layout.addLayout(header_layout)

        # -- Control Panel Card --
        self.controls_card = QFrame()
        self.controls_card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        controls_layout = QVBoxLayout(self.controls_card)
        controls_layout.setContentsMargins(20, 16, 20, 20)
        controls_layout.setSpacing(16)

        # Row 1: Chart Selector & Refresh Button
        chart_row = QHBoxLayout()
        chart_row.setSpacing(12)

        self.chart_icon_lbl = QLabel()
        self.chart_icon_lbl.setPixmap(lucide("bar-chart-2", VIOLET, 20))
        chart_row.addWidget(self.chart_icon_lbl)

        self.chart_label = QLabel("Chart:")
        self.chart_label.setProperty("filter_label", False)  # Distinguish from filter labels
        # Style applied in _apply_theme
        chart_row.addWidget(self.chart_label)

        self.chart_picker = QComboBox()
        self.chart_picker.addItems(list(REGISTRY.keys()) + list(_MATCHUP_STATS))
        self.chart_picker.currentTextChanged.connect(self.on_chart_changed)
        chart_row.addWidget(self.chart_picker, 1)

        refresh_icon = lucide("rotate-cw", WHITE, 16)
        self.refresh_btn = PillButton("Refresh", bg=VIOLET, fg=WHITE, height=36)
        self.refresh_btn.setIcon(QIcon(refresh_icon))
        self.refresh_btn.setToolTip("Pick up anything synced since this tab was last opened")
        self.refresh_btn.clicked.connect(self.refresh_everything)
        chart_row.addWidget(self.refresh_btn)

        export_icon = lucide("download", WHITE, 16)
        self.export_btn = PillButton("Export to CSV", bg=INK, fg=WHITE, height=36)
        self.export_btn.setIcon(QIcon(export_icon))
        self.export_btn.setToolTip(
            "Export all data tables plus pre-computed stats (13 CSVs) for Power BI"
        )
        self.export_btn.clicked.connect(self.export_for_powerbi)
        chart_row.addWidget(self.export_btn)

        controls_layout.addLayout(chart_row)

        # Separator line
        self.sep_line = QFrame()
        self.sep_line.setFrameShape(QFrame.Shape.HLine)
        self.sep_line.setFixedHeight(1)
        controls_layout.addWidget(self.sep_line)

        # Row 2: Filter Controls Row
        filter_row = QHBoxLayout()
        filter_row.setSpacing(16)

        filters_def = [
            ("Regulation", "regulation_filter"),
            ("Team", "team_filter"),
            ("Pokemon", "mon_filter"),
            ("Result", "result_filter"),
            ("Side", "side_filter"),
        ]

        self._filter_combos = []

        for label_text, attr_name in filters_def:
            col_layout = QVBoxLayout()
            col_layout.setSpacing(4)

            lbl = QLabel(label_text)
            lbl.setProperty("filter_label", True)
            # Style applied in _apply_theme
            col_layout.addWidget(lbl)

            combo = QComboBox()
            setattr(self, attr_name, combo)
            self._filter_combos.append(combo)

            _make_searchable(combo)

            col_layout.addWidget(combo)
            filter_row.addLayout(col_layout, 1)

        self.regulation_filter.currentIndexChanged.connect(self.on_regulation_changed)
        self.team_filter.currentIndexChanged.connect(self.on_team_changed)
        self.mon_filter.currentIndexChanged.connect(self.on_mon_changed)
        self.result_filter.currentIndexChanged.connect(self.on_result_changed)
        self.side_filter.currentIndexChanged.connect(self.on_side_changed)

        controls_layout.addLayout(filter_row)
        main_layout.addWidget(self.controls_card)

        # -- Matplotlib Figure Canvas Card --
        self.canvas_card = QFrame()
        self.canvas_card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        canvas_layout = QVBoxLayout(self.canvas_card)
        canvas_layout.setContentsMargins(16, 16, 16, 16)

        self.figure = Figure()
        self.canvas = FigureCanvasQTAgg(self.figure)
        canvas_layout.addWidget(self.canvas)

        # -- Content stack: existing canvas (index 0) + the three non-chart views --
        self.view_stack = QStackedWidget()
        self.matchup_view = MatchupGridView()
        self.best_worst_view = BestWorstView()
        self.leads_view = LeadsView()
        self.brought_view = BroughtRecordView()

        self.view_stack.addWidget(self.canvas_card)      # index 0 -- Charts
        self.view_stack.addWidget(self.matchup_view)     # index 1 -- Top 6 Opponents
        self.view_stack.addWidget(self.best_worst_view)  # index 2 -- Best/Worst Matchups
        self.view_stack.addWidget(self.leads_view)       # index 3 -- Most Common Leads
        self.view_stack.addWidget(self.brought_view)     # index 4 -- Brought Record

        main_layout.addWidget(self.view_stack, 1)

        self._team_explicitly_set = False
        self._side_explicitly_set = False

        THEME.theme_changed.connect(self._apply_theme)
        self._apply_theme()

        self.refresh_regulation_options()
        self.refresh_team_options()
        self.refresh_mon_options()
        self.on_chart_changed()

    def _apply_theme(self, *_args):
        tokens = THEME.get_tokens()

        self.setStyleSheet(f"background-color: {tokens['WHITE']};")
        self.title_label.setStyleSheet(f"color: {tokens['INK']}; font-family: {FONT_STACK}; font-size: 24px; font-weight: 700;")
        self.subtitle_label.setStyleSheet(f"color: {tokens['MUTED']}; font-family: {FONT_STACK}; font-size: 14px;")

        card_qss = f"""
            QFrame {{
                background-color: {tokens['WHITE']};
                border: 1px solid {tokens['BORDER']};
                border-radius: 14px;
            }}
        """
        self.controls_card.setStyleSheet(card_qss)
        self.canvas_card.setStyleSheet(card_qss)
        self.sep_line.setStyleSheet(f"background-color: {tokens['BORDER']}; border: none;")

        # Chart icon + refresh button track the theme violet.
        self.chart_icon_lbl.setPixmap(lucide("bar-chart-2", tokens['VIOLET'], 20))
        from fourslice.gui.imports_page import _pill_qss
        self.refresh_btn.setStyleSheet(_pill_qss("PillButton", tokens['VIOLET'], tokens['WHITE'], 36))
        self.export_btn.setStyleSheet(_pill_qss("PillButton", tokens['INK'], tokens['WHITE'], 36))

        combo_qss = _get_combo_qss(tokens)
        self.chart_picker.setStyleSheet(combo_qss)
        for combo in self._filter_combos:
            combo.setStyleSheet(combo_qss)

        # Update filter labels
        for lbl in self.findChildren(QLabel):
            if lbl.property("filter_label"):
                lbl.setStyleSheet(f"color: {tokens['MUTED']}; font-family: {FONT_STACK}; font-size: 12px; font-weight: 600;")

        # Update chart label
        if hasattr(self, 'chart_label') and self.chart_label:
            self.chart_label.setStyleSheet(f"color: {tokens['INK']}; font-family: {FONT_STACK}; font-size: 14px; font-weight: 600;")

        if _is_matchup(self.chart_picker.currentText()):
            self.matchup_view.apply_theme(tokens)
            self.best_worst_view.apply_theme(tokens)
            self.leads_view.apply_theme(tokens)
            self.brought_view.apply_theme(tokens)
        else:
            self.refresh_chart()

    def refresh_chart(self, *_signal_args):
        stat_name = self.chart_picker.currentText()
        if not stat_name or _is_matchup(stat_name):
            # The three matchup/lead views are not matplotlib stats; they
            # refresh through refresh_current_view instead.
            return
        stat = REGISTRY[stat_name]

        df = stat.data_fn(
            self.conn,
            regulation=self.regulation_filter.currentData(),
            team_id=self.team_filter.currentData(),
            mon=self.mon_filter.currentData(),
            result=self.result_filter.currentData(),
            my_side=self.side_filter.currentData(),
        )

        self.figure.clear()
        stat.render_fn(df, self.figure)

        tokens = THEME.get_tokens()
        bg_hex = tokens['WHITE']
        fg_hex = tokens['INK']
        muted_hex = tokens['MUTED']
        border_hex = tokens['BORDER']

        self.figure.patch.set_facecolor(bg_hex)
        for ax in self.figure.axes:
            ax.set_facecolor(bg_hex)
            ax.title.set_color(fg_hex)
            ax.xaxis.label.set_color(muted_hex)
            ax.yaxis.label.set_color(muted_hex)
            ax.tick_params(colors=muted_hex, which='both')
            # Pie slice labels + autopct annotations (move names, "N/A")
            # are Text objects on ax.texts, not ticks -- they keep a
            # hardcoded black default that's invisible on the dark
            # WHITE background. Re-color them to the readable fg.
            for text in ax.texts:
                text.set_color(fg_hex)
            for spine in ax.spines.values():
                spine.set_color(border_hex)

        self.figure.tight_layout()
        self.canvas.draw()

    def on_mon_changed(self, *_signal_args):
        self.refresh_current_view()

    def on_result_changed(self, *_signal_args):
        self.refresh_chart()

    def on_side_changed(self, *_signal_args):
        """
        Any real user change here counts as an explicit choice -- from
        that point on, the selection is restored (never re-defaulted)
        across chart switches and refreshes, mirroring
        _team_explicitly_set. Programmatic population never touches it
        (refresh_side_options blocks signals), so this only ever fires
        on a genuine pick.
        """
        self._side_explicitly_set = True
        self.refresh_chart()

    def on_chart_changed(self, *_signal_args):
        """
        The chart_picker signal handler:

        - Whether "Difference" belongs in the Result dropdown depends
          on which chart is now selected (see refresh_result_options)
          -- resolved before refresh_chart runs, so a leftover
          "Difference" selection from the previous chart can't drive
          one render against a chart_type it was never meant for.
        - Whether "Opponent's Pokemon"/"Both" belong in the Side
          dropdown also depends on the selected chart (see
          refresh_side_options) -- Move Usage's panels are titled
          with YOUR roster's species, so those two options don't read
          as sensibly labeled there.
        - Whether the Pokemon filter does anything also depends on
          the selected chart (Stat.uses_mon_filter) -- grayed out
          (not cleared) rather than left implying it affects a chart
          it doesn't, e.g. Move Usage's whole-roster grid. Disabled,
          not hidden or reset, so a mon chosen under Turns on Field
          is still there if you switch back to it.
        """
        stat_name = self.chart_picker.currentText()

        if _is_matchup(stat_name):
            self.view_stack.setCurrentIndex(_MATCHUP_PAGE[stat_name])
            self._lock_result_side_na()
            self.refresh_mon_options()       # mode-aware: matchup mon options for this view
            self.refresh_current_view()
            return

        # A real chart: back to the canvas page; Result/Side unlock and
        # restore their real options (the N/A lock used a non-None
        # sentinel, so findData won't resurrect "Both"), and the Pokemon
        # filter reverts to the chart-scoped option set.
        self.view_stack.setCurrentIndex(0)
        self.mon_filter.setEnabled(REGISTRY[stat_name].uses_mon_filter if stat_name else True)
        self.refresh_result_options()
        self.refresh_side_options()
        self.refresh_mon_options()
        self.refresh_chart()

    def on_regulation_changed(self, *_signal_args):
        """
        Regulation is the top of the cascade: Team narrows to teams
        with at least one game in the selected regulation, and
        Pokemon narrows to whichever team ends up selected (or, with
        no team selected, to species seen in this regulation) -- both
        need rebuilding, in that order, before this ends in a chart
        refresh. refresh_team_options can itself change which team is
        selected (if the previous one doesn't qualify anymore under
        the new regulation), which is exactly why refresh_mon_options
        has to run AFTER it, not before or in parallel.
        """
        self.refresh_team_options()
        self.refresh_mon_options()
        self.refresh_current_view()

    def on_team_changed(self, *_signal_args):
        """
        Any real user change here counts as an explicit choice -- from
        that point on, the selection is restored (never re-defaulted)
        across tab switches, syncs, and regulation changes. Set before
        the refresh so the chart that refresh draws is already running
        under the new regime.

        Team narrows Pokemon to that team's actual roster (see
        get_mons_for_filter) -- has to happen before refresh_chart so
        the Pokemon dropdown doesn't show a stale, wrong-team list for
        one render.
        """
        self._team_explicitly_set = True
        self.refresh_mon_options()
        self.refresh_current_view()

    def refresh_regulation_options(self):
        """
        Was previously only ever populated once, in __init__ -- since
        this tab is built once and never rebuilt, that meant new
        regulations silently never showed up until the whole app
        restarted. Called from refresh_everything (the Refresh button,
        and MainWindow on tab-switch); regulations only ever grow, so
        re-querying on every minor filter tweak elsewhere isn't
        worth the extra cost.
        """
        previous_selection = self.regulation_filter.currentData()
        self.regulation_filter.blockSignals(True)
        self.regulation_filter.clear()
        self.regulation_filter.addItem("All regulations", None)
        rows = self.conn.execute(
            "SELECT DISTINCT regulation FROM games WHERE regulation IS NOT NULL ORDER BY regulation"
        ).fetchall()
        for (regulation,) in rows:
            self.regulation_filter.addItem(regulation, regulation)
        restored_index = self.regulation_filter.findData(previous_selection)
        self.regulation_filter.setCurrentIndex(restored_index if restored_index >= 0 else 0)
        self.regulation_filter.blockSignals(False)

    def refresh_team_options(self):
        """
        Same reasoning and shape as refresh_regulation_options -- new
        teams only ever get added, never removed, so this just needs
        to run whenever the tab becomes visible again (or Regulation
        changes -- see on_regulation_changed), not on every filter
        tweak. Uses get_teams_by_recency, scoped to the currently
        selected Regulation, so this list is both most-recently-used
        team first AND limited to teams actually played under it --
        same ordering as the Teams tab when Regulation is "All".

        Two selection paths, decided by whether the user has made an
        explicit team choice yet (see _team_explicitly_set, set by
        on_team_changed -- programmatic population never touches it):

        - User has chosen: restore exactly that selection (including
          a deliberate "All teams"); if the previously-selected team
          doesn't qualify under the new regulation, findData below
          returns -1 and this falls back to "All teams", same pattern
          as every other filter here.
        - User hasn't chosen yet: default to the MOST RECENT team --
          index 1, the first real entry after "All teams", since
          get_teams_by_recency orders most-recently-active first. This
          re-defaults on every tab switch / refresh, so a newly-synced
          team surfaces automatically, until the user picks something
          (or "All teams") explicitly. Falls back to "All teams" only
          when there are no teams at all.
        """
        previous_selection = self.team_filter.currentData()
        self.team_filter.blockSignals(True)
        self.team_filter.clear()
        self.team_filter.addItem("All teams", None)
        for team_id, nickname in get_teams_by_recency(self.conn, regulation=self.regulation_filter.currentData()):
            self.team_filter.addItem(nickname, team_id)
        if self._team_explicitly_set:
            restored_index = self.team_filter.findData(previous_selection)
            self.team_filter.setCurrentIndex(restored_index if restored_index >= 0 else 0)
        elif self.team_filter.count() > 1:
            self.team_filter.setCurrentIndex(1)
        else:
            self.team_filter.setCurrentIndex(0)
        self.team_filter.blockSignals(False)

    def refresh_mon_options(self):
        """
        Repopulates the Pokemon filter depending on which dropdown entry
        is active. Charts keep the chart-scoped option set (get_mons_for_filter
        -- prioritizes Team over Regulation). The two matchup grids list
        every opponent species ever fought (opponent side = not
        games.my_side). Most Common Leads lists the selected team's
        actual roster, gated to enabled only when that view will actually
        show doubles pairs (a team must be selected AND doubles leads
        must exist) -- the pairing filter is doubles-only by design.

        Attendance lists opponent species that have team roster data
        (from opponent_teams table), scoped by regulation/team.
        """
        previous_selection = self.mon_filter.currentData()
        stat_name = self.chart_picker.currentText()
        self.mon_filter.blockSignals(True)
        self.mon_filter.clear()
        self.mon_filter.addItem("All Pokemon", None)

        if stat_name == "Most Common Leads":
            team_id = self.team_filter.currentData()
            roster = []
            if team_id is not None:
                roster = [
                    r[0] for r in self.conn.execute(
                        "SELECT species FROM team_pokemon WHERE team_id = ? ORDER BY slot_order",
                        (team_id,),
                    )
                ]
            for species in roster:
                self.mon_filter.addItem(species, species)
            buckets = lead_records(
                self.conn,
                regulation=self.regulation_filter.currentData(),
                team_id=team_id,
            )
            self.mon_filter.setEnabled(
                team_id is not None and dominant_battle_size(buckets) == "doubles"
            )
        elif _is_matchup(stat_name):
            for mon in every_opponent_mon(
                self.conn,
                regulation=self.regulation_filter.currentData(),
                team_id=self.team_filter.currentData(),
            ):
                self.mon_filter.addItem(mon, mon)
            self.mon_filter.setEnabled(True)
        elif stat_name == "Attendance":
            # Attendance tracks opponent team roster -> bring rates
            # Show opponent species from opponent_teams table
            query = """
                SELECT DISTINCT ot.species
                FROM opponent_teams ot
                JOIN games g ON ot.game_id = g.game_id
                WHERE g.battle_size = 'doubles'
                  AND g.my_side IS NOT NULL
                  AND g.result IN ('W', 'L')
            """
            params = []
            reg = self.regulation_filter.currentData()
            team_id = self.team_filter.currentData()
            if reg:
                query += " AND g.regulation = ?"
                params.append(reg)
            if team_id:
                query += " AND g.team_id = ?"
                params.append(team_id)
            query += " ORDER BY ot.species"
            for (species,) in self.conn.execute(query, params).fetchall():
                self.mon_filter.addItem(species, species)
            self.mon_filter.setEnabled(True)
        else:
            for mon in get_mons_for_filter(
                self.conn, team_id=self.team_filter.currentData(), regulation=self.regulation_filter.currentData()
            ):
                self.mon_filter.addItem(mon, mon)
            # charts gate enables through uses_mon_filter in on_chart_changed

        restored_index = self.mon_filter.findData(previous_selection)
        self.mon_filter.setCurrentIndex(restored_index if restored_index >= 0 else 0)
        self.mon_filter.blockSignals(False)

    def refresh_result_options(self):
        """
        Rebuilds the Result dropdown from scratch around whichever
        chart is currently selected: "Difference" (win average minus
        loss average) is only offered when that chart's chart_type is
        in DIFF_CAPABLE_CHART_TYPES (bar charts, for now -- see
        stats/__init__.py). Unlike regulation/team/mon, this can't
        just grow monotonically over time -- it needs to actively
        DROP "Difference" when switching to a chart_type that doesn't
        support it, e.g. Move Usage's pie chart -- so this runs from
        on_chart_changed, not refresh_everything.

        If "Difference" was selected and the newly-picked chart drops
        it, findData below correctly returns -1, so this falls back
        to "All results" rather than leaving an invalid filter active
        against a chart it was never meant for.
        """
        if _is_matchup(self.chart_picker.currentText()):
            self._lock_filter_na(self.result_filter)
            return
        self.result_filter.setEnabled(True)
        stat_name = self.chart_picker.currentText()
        chart_type = REGISTRY[stat_name].chart_type if stat_name else None

        previous_selection = self.result_filter.currentData()
        self.result_filter.blockSignals(True)
        self.result_filter.clear()
        self.result_filter.addItem("All results", None)
        self.result_filter.addItem("Wins", "W")
        self.result_filter.addItem("Losses", "L")
        if chart_type in DIFF_CAPABLE_CHART_TYPES:
            self.result_filter.addItem("Difference (win - loss)", "diff")
        restored_index = self.result_filter.findData(previous_selection)
        self.result_filter.setCurrentIndex(restored_index if restored_index >= 0 else 0)
        self.result_filter.blockSignals(False)

    def refresh_side_options(self):
        """
        Rebuilds the Side dropdown from scratch around whichever
        chart is currently selected: "Opponent's Pokemon" and "Both"
        are dropped entirely when that chart's Stat.mine_only is set
        (Move Usage, for now -- see stats/__init__.py), since its
        panels are always titled with YOUR roster's species names, so
        those two options don't read as sensibly-labeled choices
        there the way they do for a stat like Turns on Field.

        Same fallback pattern as refresh_result_options: if the
        previous selection doesn't survive the rebuild (e.g.
        "Opponent's Pokemon" was picked, then Move Usage was
        selected), findData below returns -1 and this falls back to
        "My Pokemon" -- the only option that's always present.

        Default: until the user makes an explicit pick, this is
        ALWAYS "My Pokemon", never "Both" (see on_side_changed /
        _side_explicitly_set). On the very first population the
        previously-selected data is None (empty combo), and
        findData(None) would match "Both" -- the one item whose data
        IS None -- so before that flag is set the previous selection
        is ignored and index 0 (My Pokemon) is used instead.
        """
        if _is_matchup(self.chart_picker.currentText()):
            self._lock_filter_na(self.side_filter)
            return
        self.side_filter.setEnabled(True)
        stat_name = self.chart_picker.currentText()
        mine_only = REGISTRY[stat_name].mine_only if stat_name else False

        previous_selection = self.side_filter.currentData()
        self.side_filter.blockSignals(True)
        self.side_filter.clear()
        self.side_filter.addItem("My Pokemon", "mine")
        if not mine_only:
            self.side_filter.addItem("Opponent's Pokemon", "opponent")
            self.side_filter.addItem("Both", None)
        if self._side_explicitly_set:
            restored_index = self.side_filter.findData(previous_selection)
            self.side_filter.setCurrentIndex(restored_index if restored_index >= 0 else 0)
        else:
            self.side_filter.setCurrentIndex(0)
        self.side_filter.blockSignals(False)

    def _lock_filter_na(self, combo):
        """Lock a filter to a single DISABLED "N/A" item. Uses a non-None
        sentinel as the item data so a later findData() restore can never
        resurrect an item whose data IS None -- "All results" and "Both"
        both use data=None, so a data=None lock would silently come back
        as one of those instead of falling back to index 0."""
        combo.blockSignals(True)
        combo.clear()
        combo.addItem("N/A", _NA_SENTINEL)
        combo.setEnabled(False)
        combo.blockSignals(False)

    def _lock_result_side_na(self):
        self._lock_filter_na(self.result_filter)
        self._lock_filter_na(self.side_filter)

    def refresh_current_view(self):
        """
        Refresh whatever the Chart dropdown currently points at: a real
        chart re-renders the canvas, a matchup/lead entry re-queries and
        rebuilds that entry's widget page. This is the single refresh
        entry the filter-change handlers funnel through.
        """
        stat_name = self.chart_picker.currentText()
        if not _is_matchup(stat_name):
            self.refresh_chart()
            return
        page = _MATCHUP_PAGE[stat_name]
        reg = self.regulation_filter.currentData()
        team = self.team_filter.currentData()
        mon = self.mon_filter.currentData()
        if page == 1:
            self.matchup_view.refresh(self.conn, regulation=reg, team_id=team, mon=mon)
        elif page == 2:
            self.best_worst_view.refresh(self.conn, regulation=reg, team_id=team, mon=mon)
        elif page == 3:
            self.leads_view.refresh(self.conn, regulation=reg, team_id=team, mon=mon)
        elif page == 4:
            self.brought_view.refresh(self.conn, regulation=reg, team_id=team, mon=mon)

    def refresh_everything(self, *_signal_args):
        self.refresh_regulation_options()
        self.refresh_team_options()
        self.refresh_mon_options()
        self.refresh_current_view()

    def export_for_powerbi(self):
        """
        Picks an export folder, then writes all 16 export CSVs (5 raw
        data tables + 11 pre-computed stat tables) to it on a
        background worker thread so the GUI keeps responding. A themed
        modal progress dialog tracks each file as it lands on disk and
        can cancel the job between files (already-written CSVs stay).
        """
        if self._export_thread is not None:
            return  # an export is already running

        folder = QFileDialog.getExistingDirectory(
            self, "Choose export folder for Power BI CSVs",
        )
        if not folder:
            return  # user cancelled the folder picker

        self.export_btn.setEnabled(False)
        self._export_folder = folder

        dialog = QProgressDialog(
            "Preparing export…", "Cancel", 0, 16, self)
        dialog.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        dialog.setWindowTitle("Exporting for Power BI")
        dialog.setWindowModality(Qt.WindowModal)
        dialog.setMinimumDuration(0)
        dialog.setAutoClose(False)
        dialog.setAutoReset(False)
        dialog.setValue(0)
        dialog.setStyleSheet(self._export_dialog_style())
        # PySide6 does not wrap QProgressDialog::cancelButton(), so we hand
        # the dialog its own button via setCancelButton() (which takes
        # ownership) and keep a reference so we can disable it on cancel.
        cancel_btn = QPushButton("Cancel")
        dialog.setCancelButton(cancel_btn)
        self._export_cancel_btn = cancel_btn
        self._export_dialog = dialog

        thread = QThread(self)
        worker = ExportWorker(str(config.get_db_path()), folder)
        worker.moveToThread(thread)
        self._export_thread = thread
        self._export_worker = worker

        thread.started.connect(worker.run)
        worker.progress.connect(self._on_export_progress)
        worker.finished.connect(self._on_export_finished)
        worker.error.connect(self._on_export_error)
        dialog.canceled.connect(self._on_export_cancelled)
        thread.finished.connect(self._on_export_thread_finished)

        dialog.show()
        thread.start()

    @Slot(int, int, str)
    def _on_export_progress(self, done, total, message):
        dialog = self._export_dialog
        if dialog is None:
            return
        dialog.setMaximum(total)
        dialog.setValue(done)
        dialog.setLabelText(f"{message} — file {done} of {total}")

    @Slot()
    def _on_export_cancelled(self):
        worker = self._export_worker
        if worker is None:
            return
        worker.cancel()
        dialog = self._export_dialog
        if dialog is not None:
            dialog.setLabelText("Cancelling…")
        cancel_btn = self._export_cancel_btn
        if cancel_btn is not None:
            cancel_btn.setEnabled(False)

    @Slot(list, int, bool)
    def _on_export_finished(self, written, total_rows, cancelled):
        dialog = self._export_dialog
        if dialog is not None:
            dialog.close()
        if cancelled:
            QMessageBox.information(
                self,
                "Export cancelled",
                f"{len(written)} of 13 CSV files were written to:\n"
                f"{self._export_folder}\n\n"
                "Every file on disk is complete and usable. Run Export\n"
                "for Power BI again and choose the same folder to pick\n"
                "up the remaining CSVs.",
            )
        else:
            names = "\n".join(f"  • {Path(p).name}" for p in written)
            QMessageBox.information(
                self,
                "Export complete",
                f"Exported {len(written)} CSV files ({total_rows:,} total rows) to:\n"
                f"{self._export_folder}\n\n{names}\n\n"
                "Open Power BI Desktop → Get Data → Text/CSV and point\n"
                "it at these files. The stats_*.csv tables are pre-computed,\n"
                "so no DAX measures are required. See powerbi/README.md\n"
                "for the full setup walkthrough.",
            )
        self._shut_down_export_thread()

    @Slot(str)
    def _on_export_error(self, message):
        dialog = self._export_dialog
        if dialog is not None:
            dialog.close()
        QMessageBox.critical(
            self,
            "Export failed",
            f"Could not write CSV files:\n\n{message}",
        )
        self._shut_down_export_thread()

    @Slot()
    def _on_export_thread_finished(self):
        # The worker thread's event loop has fully stopped, so it is now
        # safe to drop our references and let Qt clean up the C++ side.
        self._export_thread = None
        self._export_worker = None
        self._export_dialog = None
        self._export_cancel_btn = None
        self._export_folder = None
        self.export_btn.setEnabled(True)

    def _shut_down_export_thread(self):
        """Stop the worker thread's event loop. run() has already
        returned by the time the worker's signals arrive (they are
        queued), so quit() just ends the idle event loop and triggers
        thread.finished, which clears the refs and re-enables the
        button."""
        thread = self._export_thread
        if thread is not None:
            thread.quit()

    def _export_dialog_style(self):
        """Stylesheet for the export progress dialog, kept in step with
        the app's current theme tokens."""
        t = THEME.get_tokens()
        return f"""
        QProgressDialog {{
            background: {t["WHITE"]};
            color: {t["INK"]};
            font-family: {FONT_STACK};
            font-size: 13px;
        }}
        QProgressDialog QLabel {{
            color: {t["INK"]};
            font-family: {FONT_STACK};
        }}
        QProgressDialog QProgressBar {{
            background: {t["TRACK"]};
            border: none;
            border-radius: 6px;
            min-height: 10px;
            max-height: 10px;
        }}
        QProgressDialog QProgressBar::chunk {{
            background: {t["VIOLET"]};
            border-radius: 6px;
        }}
        QProgressDialog QPushButton {{
            background: {t["PALE_VIOLET"]};
            color: {t["INK"]};
            border: 1px solid {t["BORDER"]};
            border-radius: 8px;
            padding: 6px 18px;
            font-family: {FONT_STACK};
        }}
        QProgressDialog QPushButton:hover {{
            background: {t["BORDER"]};
        }}
        QProgressDialog QPushButton:disabled {{
            color: {t["MUTED"]};
            background: {t["TRACK"]};
        }}
        """