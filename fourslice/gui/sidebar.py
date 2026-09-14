from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QPixmap, QPainter
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget, QLineEdit,
)
from fourslice.gui.icons import lucide
from fourslice.gui.pokemon_info import PokeInfoWindow
from fourslice.gui.imports_page import (
    SegmentedControl, SelectButton, PokemonRow, PokemonList,
    WHITE, INK, BODY, MUTED, VIOLET, PALE_VIOLET, TRACK,
    FONT_STACK, NAV_QSS, SEARCH_QSS, set_placeholder_color,
    get_search_qss,
    ASSETS_DIR, THEME
)
from fourslice import config, storage
from fourslice.extstats.paths import cache_dir as extstats_cache_dir
from fourslice.extstats import statsdb
from fourslice.extstats.models import SMOGON_ELOS
from fourslice.extstats.smogon import (
    load_latest_formats, load_format_usage, get_format_groups
)
import os
import sqlite3

# ---------------------------------------------------------------------------
# Pokemon ID lookup (name -> pokedex number, for sprite images)
# ---------------------------------------------------------------------------
_POKEMON_ID_MAP: dict[str, int] | None = None

def _load_pokemon_id_map() -> dict[str, int]:
    """Build a lowercase name -> pokedex id dict from pokemon_complete.db.

    Uses the shared resolver so the runtime (pokedex.js-derived) copy is read
    when present, falling back to the shipped one otherwise.
    """
    global _POKEMON_ID_MAP
    if _POKEMON_ID_MAP is not None:
        return _POKEMON_ID_MAP
    db_path = storage.get_pokemon_complete_db_path()
    _POKEMON_ID_MAP = {}
    if db_path.is_file():
        try:
            conn = sqlite3.connect(str(db_path))
            for row in conn.execute("SELECT id, name FROM pokemon"):
                # Normalize with the same key the sprite lookup uses. The DB
                # `name` column holds Showdown ids ("gouging-fire") in the
                # shipped copy but display names ("Gouging Fire") in the
                # runtime pokedex.js-built copy, so raw .lower() keys would
                # only match one of them.
                _POKEMON_ID_MAP[_pokemon_sprite_key(row[1])] = row[0]
            conn.close()
        except Exception:
            pass
    return _POKEMON_ID_MAP


def reset_pokemon_id_map() -> None:
    """Drop the cached name -> pokedex-id map so the next sprite lookup
    rebuilds it from pokemon_complete.db.

    Called when the background data refresh finishes (a newer pokedex.js
    may have added Pokemon mid-session); without this the map would keep
    its stale, startup-era contents until the app restarts.
    """
    global _POKEMON_ID_MAP
    _POKEMON_ID_MAP = None

def _pokemon_sprite_key(name: str) -> str:
    """Normalise a Smogon display name into the key used by pokemon_complete.db
    and the sprite assets (lowercase, spaces→hyphens, strip punctuation,
    e.g. 'Great Tusk' → 'great-tusk', "Mr. Mime" → 'mr-mime')."""
    import re
    return re.sub(r"[^a-z0-9-]", "", name.lower().replace(" ", "-"))


def _get_pokemon_sprite(name: str, size: int = 36) -> QPixmap | None:
    """Return a scaled sprite QPixmap for *name*, or None if unavailable.

    Real alternate-form names (e.g. 'Ogerpon-Wellspring', 'Rotom-Heat')
    have their own entry in pokemon_complete.db, so the lookup matches
    directly.  When the DB has no entry, we do NOT fall back to the base
    form — a regional/form pokemon should show the letter placeholder,
    not the wrong base pokemon's sprite.  Names that are just the base
    name with odd punctuation (e.g. "Mr. Mime", "Farfetch'd") are
    normalized into the DB key on the first lookup; their DB entry has
    the full name, so the direct match succeeds without any fallback.
    """
    id_map = _load_pokemon_id_map()
    key = _pokemon_sprite_key(name)
    dex = id_map.get(key)
    if dex is None:
        return None
    path = os.path.join(ASSETS_DIR, f"{dex}.png")
    pm = QPixmap(path)
    if pm.isNull():
        return None
    return pm.scaledToHeight(size, Qt.TransformationMode.SmoothTransformation)

# ---------------------------------------------------------------------------
# NavRow
# ---------------------------------------------------------------------------
class NavRow(QFrame):
    clicked = Signal(str)

    def __init__(self, key, label, icon_name, active=False, parent=None):
        super().__init__(parent)
        self.setObjectName("NavRow")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(NAV_QSS)
        self._key = key
        self._icon_name = icon_name
        self._active = active
        self.setMinimumHeight(40)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 0, 12, 0)
        lay.setSpacing(12)
        
        self._icon = QLabel()
        self._icon.setFixedSize(28, 28)
        self._icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._label = QLabel(label)
        lay.addWidget(self._icon)
        lay.addWidget(self._label)
        lay.addStretch(1)

        self.set_active(active)
        self._icon.setStyleSheet("background: transparent;")

    @property
    def key(self):
        return self._key

    def is_active(self):
        return self._active

    def set_active(self, active):
        self._active = active
        self.setProperty("active", active)
        self.style().unpolish(self)
        self.style().polish(self)
        icon_color = VIOLET if active else MUTED
        self._icon.setPixmap(lucide(self._icon_name, icon_color, 20))
        self._label.setStyleSheet(
            f"background: transparent; color: {INK if active else BODY}; font-size: 14px;"
            f"font-family: {FONT_STACK};"
        )
        weight = QFont.Weight.Bold if active else QFont.Weight.Medium
        f = self._label.font()
        f.setWeight(weight)
        self._label.setFont(f)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self._key)
        super().mouseReleaseEvent(event)

# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------
class Sidebar(QWidget):
    """Fixed-width left column: the nav list and the Top Pokemon group."""
    nav_clicked = Signal(str)
    settings_clicked = Signal()

    def __init__(self, theme_manager=None, parent=None):
        super().__init__(parent)
        self.theme_manager = theme_manager or THEME
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedWidth(268)
        self.setStyleSheet(f"background-color: {WHITE};")

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 24)
        root.setSpacing(16)

        # -- Logo --
        self.logo = QLabel()
        root.addWidget(self.logo)
        self._update_logo()
        self.theme_manager.theme_changed.connect(self._update_logo)
        self.theme_manager.theme_changed.connect(self._apply_theme)

        # -- Tabs group --
        root.addWidget(self._group_label("Tabs"))
        self.nav_rows = {}
        for key, label, icon_name in (
            ("imports", "Imports", "download"),
            ("stats", "Statistics", "bar-chart-2"),
            ("teams", "Team Builder", "compass"),
            ("replays", "Replays", "play-circle"),
        ):
            row = NavRow(key, label, icon_name, active=(key == "imports"))
            row.clicked.connect(self._on_nav_clicked)
            self.nav_rows[key] = row
            root.addWidget(row)

        # -- Settings row (not a tab, always visible) --
        self.settings_row = NavRow("settings", "Settings", "settings")
        self.settings_row.clicked.connect(self._on_settings_clicked)
        root.addWidget(self.settings_row)

        root.addSpacing(10)

        # -- Top Pokemon group --
        root.addWidget(self._group_label("Top Pokemon"))
        self.banlist_toggle = SegmentedControl(
            ["Singles", "Doubles"], active_index=0,
            checked_bg=WHITE, checked_fg=INK, unchecked_fg=INK, height=30,
        )
        self.banlist_toggle.currentChanged.connect(self._on_kind_changed)
        root.addWidget(self.banlist_toggle)

        dropdowns = QHBoxLayout()
        dropdowns.setSpacing(8)

        # Load Smogon data
        self.formats_data = load_latest_formats(extstats_cache_dir())
        self.format_groups = get_format_groups(self.formats_data) if self.formats_data else {"Singles": [], "Doubles": []}

        # In-memory caches so switching back to a previously-viewed
        # format/elo combo is instant (no DB hit or widget rebuild).
        self._usage_cache: dict[tuple[str, str], list[dict]] = {}
        self._sprite_cache: dict[str, QPixmap | None] = {}
        # Open PokeInfoWindow per (name, format, elo). The dict is the retained
        # reference that keeps each non-modal window alive (otherwise PySide6
        # GCs the wrapper right after the click slot returns) and dedupes
        # repeat-click spam: re-clicking raises the same window. Windows are
        # hidden on close (not deleted), so each key stays in the dict for the
        # session and a later click on the same row re-shows that instance.
        self._info_windows: dict[tuple[str, str, str], PokeInfoWindow] = {}
        
        # Get formats for default kind (Singles)
        initial_formats = self.format_groups.get("Singles", [])
        initial_names = [f["name"] for f in initial_formats]
        
        self.format_select = SelectButton(
            initial_names, current=initial_names[0] if initial_names else "", theme_manager=self.theme_manager)
        # Elo divisions must be re-derived before the list refreshes so the
        # dropdown only ever offers divisions the new format actually has.
        self.format_select.changed.connect(self._update_usage_elos)
        self.format_select.changed.connect(self._refresh_list)

        self.usage_select = SelectButton(
            list(SMOGON_ELOS), current=SMOGON_ELOS[0], theme_manager=self.theme_manager)
        self.usage_select.changed.connect(self._refresh_list)

        dropdowns.addWidget(self.format_select)
        dropdowns.addWidget(self.usage_select)
        root.addLayout(dropdowns)

        self.search_input = QLineEdit()
        self.search_input.setObjectName("SearchField")
        self.search_input.setPlaceholderText("Search...")
        self.search_input.setFixedHeight(32)
        self.search_input.setStyleSheet(SEARCH_QSS)
        set_placeholder_color(self.search_input)
        search_icon = QIcon(lucide("search", MUTED, 16))
        self.search_input.addAction(search_icon, QLineEdit.ActionPosition.LeadingPosition)
        self.search_input.textChanged.connect(self._filter_list)
        root.addWidget(self.search_input)

        root.addSpacing(8)

        # -- Top Pokemon usage list --
        self.pokemon_list = PokemonList()
        root.addWidget(self.pokemon_list, 1)

        # Initial population
        self._update_usage_elos()
        self._refresh_list()

        self._apply_theme()

    def _group_label(self, text):
        label = QLabel(text)
        label.setProperty("group_label", True)
        label.setStyleSheet(
            f"color: {self.theme_manager.tokens['MUTED']}; font-size: 11px; font-weight: 600;"
            f"font-family: {FONT_STACK};"
        )
        return label

    def _apply_theme(self, *_args):
        tokens = self.theme_manager.get_tokens()
        self.setStyleSheet(f"background-color: {tokens['WHITE']};")
        self._update_logo()
        
        for row in self.nav_rows.values():
            row.style().unpolish(row)
            row.style().polish(row)
            # Trigger NavRow re-style
            row.set_active(row.is_active())

        # Settings row (always inactive, but re-style on theme change)
        self.settings_row.set_active(False)

        # Update group labels
        for child in self.findChildren(QLabel):
            if child.property("group_label"):
                child.setStyleSheet(
                    f"color: {tokens['MUTED']}; font-size: 11px; font-weight: 600;"
                    f"font-family: {FONT_STACK};"
                )
        
        # Refresh other widgets
        self.search_input.setStyleSheet(get_search_qss(tokens))
        set_placeholder_color(self.search_input)

        # Re-style the Top Pokemon rows (they bake in light text colors)
        for row in self.pokemon_list.rows():
            if hasattr(row, "_apply_theme"):
                row._apply_theme(tokens)

    def _update_logo(self):
        # Determine which asset to load
        is_dark = self.theme_manager.current_theme == "dark"
        filename = "FourSliceLogoDark.png" if is_dark else "FourSliceLogoLight.png"
        path = os.path.join(ASSETS_DIR, filename)

        pm = QPixmap(path)
        if not pm.isNull():
            target_h = 32
            pm = pm.scaledToHeight(target_h * 2, Qt.TransformationMode.SmoothTransformation)
            pm.setDevicePixelRatio(2.0)
            self.logo.setPixmap(pm)
            self.logo.setFixedSize(pm.size() / pm.devicePixelRatio())
        else:
            # Fallback to two-tone wordmark
            self.logo.setText('<span style="color:#18181B; font-weight:700;">Four</span>'
                              '<span style="color:#5B1A8D; font-weight:700;">Slice</span>')
            self.logo.setStyleSheet(f"font-family: {FONT_STACK}; font-size: 20px;")


    def _on_nav_clicked(self, key):
        for nav_key, row in self.nav_rows.items():
            row.set_active(nav_key == key)
        self.settings_row.set_active(False)
        self.nav_clicked.emit(key)

    def _on_settings_clicked(self):
        for row in self.nav_rows.values():
            row.set_active(False)
        self.settings_row.set_active(False)
        self.settings_clicked.emit()


    def refresh_formats(self):
        """Reload formats from cache and update the dropdown without
        resetting the user's current selection."""
        self.formats_data = load_latest_formats(extstats_cache_dir())
        self.format_groups = (get_format_groups(self.formats_data)
                              if self.formats_data else {"Singles": [], "Doubles": []})
        self._usage_cache.clear()

        kind = getattr(self, "current_kind", "Singles")
        names = [f["name"] for f in self.format_groups.get(kind, [])]

        # Keep current selection if valid in new list
        current = self.format_select.currentText()
        self.format_select.set_items(names, keep_current=current in names)

        # Elo divisions may have changed too.
        self._update_usage_elos()
        # Refresh Pokemon list
        self._refresh_list()


    def _on_kind_changed(self, kind):
        self.current_kind = kind
        formats = self.format_groups.get(kind, [])
        names = [f["name"] for f in formats]
        self.format_select.set_items(names, keep_current=False)
        self._update_usage_elos()
        self._refresh_list()

    def _update_usage_elos(self):
        """Point the elo dropdown at the ladder divisions that actually have a
        stats_{format}_elo{elo} table in stats.db. We read the real tables
        (not the capture ledger, which only knows what the latest period
        wrote) so a format keeps every division it has data for -- e.g.
        gen9ou is 0/1500/1695/1825. Formats with no tables fall back to the
        canonical Smogon set. If the current selection isn't among them, drop
        to the first one -- a stale elo would otherwise make the Pokemon list
        come back empty for formats whose divisions differ from the defaults."""
        order = list(SMOGON_ELOS)
        fmt = self.format_select.currentText()
        if fmt:
            conn = statsdb.init_db(config.get_stats_db_path())
            try:
                order = statsdb.get_format_elos(conn, fmt)
            finally:
                conn.close()
        self.usage_select.blockSignals(True)
        try:
            if self.usage_select.currentText() not in order:
                self.usage_select.set_items(order, keep_current=False)
            else:
                self.usage_select.set_items(order, keep_current=True)
        finally:
            self.usage_select.blockSignals(False)

    def _refresh_list(self, _value=None):
        # The argument (a format or elo name from SelectButton.changed) is
        # unused -- we read currentText() directly so a single slot works
        # for both dropdowns and ignores the emitted value.
        format_name = self.format_select.currentText()
        elo = self.usage_select.currentText()
        if not format_name or not self.formats_data:
            self.pokemon_list.clear_rows()
            return

        cache_key = (format_name, elo)
        rows = self._usage_cache.get(cache_key)
        if rows is None:
            period_id = self.formats_data["period"]
            usage_data = load_format_usage(
                extstats_cache_dir(), period_id, format_name, elo
            )
            rows = usage_data["rows"] if usage_data and "rows" in usage_data else []
            self._usage_cache[cache_key] = rows

        # Reuse existing PokemonRow widgets (relabel instead of rebuild) so
        # switching formats is cheap -- building fresh widgets per row is
        # what made it sluggish. We batch the repaint to the one layout pass
        # after all rows are updated.
        self.pokemon_list.setUpdatesEnabled(False)
        spec_rows = []
        for row in rows:
            fullname = row["pokemon"]
            name = fullname[0:11]
            sprite = self._sprite_cache.get(fullname)
            if fullname not in self._sprite_cache:
                sprite = _get_pokemon_sprite(fullname)
                self._sprite_cache[fullname] = sprite
            pct = f"{row['usage_pct']:.1f}%" if row.get("usage_pct") is not None else "—"
            spec_rows.append({
                "name": name,
                "rank": f"#{row['rank']}",
                "pct": pct,
                "sprite": sprite,
                "fullname": fullname,
            })
        self.pokemon_list.set_pokemon_rows(spec_rows)
        self.pokemon_list.setUpdatesEnabled(True)
        # Connect click → info window once per row. Rows are reused across
        # relabels, so connect_click is idempotent; hidden/spare rows are
        # excluded so cleared rows can never open a window for a stale name.
        for row in self.pokemon_list.active_rows():
            row.connect_click(self._open_poke_info)
        # Always run the filter to clear any stale visibility from a prior
        # search query — without this, rows hidden by the old filter can
        # stay hidden after a format switch even with an empty search box.
        self._filter_list(self.search_input.text())

    def _filter_list(self, query: str):
        q = query.strip().lower() if query else ""
        for row in self.pokemon_list.active_rows():
            if not q:
                row.setVisible(True)
            else:
                row.setVisible(q in row._name.text().lower())

    def _open_poke_info(self, fullname):
        """Open (or raise) the non-modal info window for a clicked Pokemon in
        the currently selected format+elo. The row's name, format and elo all
        come from the same rendered usage table, so there is no name/format
        mismatch to guard against -- only empty/hidden rows are filtered."""
        if not fullname:
            return
        fmt = self.format_select.currentText()
        elo = self.usage_select.currentText()
        key = (fullname, fmt, elo)
        win = self._info_windows.get(key)
        if win is not None:
            win.show()
            win.raise_()
            win.activateWindow()
            return
        win = PokeInfoWindow(fullname, fmt, elo, theme_manager=self.theme_manager)
        self._info_windows[key] = win
        # Safety net only -- the window is hidden (not destroyed) on close, so
        # this normally never fires. But if the C++ widget is ever torn down,
        # `destroyed` emits a QObject* argument: swallow it with *_ (a plain
        # `k=key` lambda would capture that arg instead of the tuple key and
        # never pop, leaving a dead wrapper behind).
        win.destroyed.connect(lambda *_: self._info_windows.pop(key, None))
        win.show()


