"""
fourslice/gui/imports_page.py

Redesigned "Imports" screen -- the full-width top header bar and the
main content area. The left sidebar (nav tabs, Top Pokemon widget,
logo) is handled separately by AppLayout.

Preview it with:  python -m fourslice.gui.imports_page

Styling conventions used throughout:
- Rounded corners on custom QFrame/QWidget backgrounds need
  setAttribute(Qt.WA_StyledBackground, True) + border-radius in QSS.
- Pill buttons fix a height and set border-radius: <height/2>px.
- Segmented controls are two checkable QPushButtons in an exclusive
  QButtonGroup inside a rounded-rect QFrame track; the checked state is
  styled via QSS :checked.
- Icons come from fourslice.gui.icons (Lucide geometry, drawn with
  QPainter -- no icon library dependency).
"""

import logging

from PySide6.QtCore import QObject, QPoint, QThread, QUrl, Qt, Signal
from PySide6.QtGui import QColor, QDesktopServices, QFont, QIcon, QPainter, QPixmap, QPalette
from PySide6.QtWidgets import (
    QButtonGroup, QFrame, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMenu,
    QPushButton, QScrollArea, QSizePolicy, QStackedWidget, QVBoxLayout, QWidget,
)

log = logging.getLogger(__name__)

# Worker for single import on background thread
class SingleImportWorker(QObject):
    finished = Signal(dict)
    error = Signal(str)

    def __init__(self, url: str, conn, usernames: list[str], store_logs: bool):
        super().__init__()
        self.url = url
        self.conn = conn
        self.usernames = usernames
        self.store_logs = store_logs

    def run(self):
        try:
            from fourslice.parser import fetch_replay_json
            from fourslice.storage import import_replay
            replay_json = fetch_replay_json(self.url)
            log_text = replay_json["log"]
            result = import_replay(
                self.conn, log_text, self.url, self.usernames,
                store_logs=self.store_logs,
            )
            self.finished.emit(result)
        except Exception as e:
            self.error.emit(str(e))

from fourslice.gui.icons import lucide, lucide_icon

from fourslice import config
from fourslice.gui.bulk_import_worker import BulkImportWorker

from pathlib import Path

from datetime import datetime, timezone

# Asset paths
def get_assets_dir() -> Path:
    import sys
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            d = Path(meipass) / "fourslice" / "gui" / "assets"
            if d.is_dir():
                return d
    return Path(__file__).resolve().parent / "assets"

ASSETS_DIR = get_assets_dir()

# gamesdata will be populated lazily by get_gamesdata() to avoid
# module-import-time DB access with a hardcoded path.
_gamesdata_cache = None

def get_gamesdata():
    """Lazily fetch game_ids from the configured database path."""
    global _gamesdata_cache
    if _gamesdata_cache is not None:
        return _gamesdata_cache

    _gamesdata_cache = []
    try:
        dbpath = config.get_db_path()
        if dbpath.exists():
            import sqlite3
            gamesdb = sqlite3.connect(dbpath)
            game_cursor = gamesdb.cursor()
            game_cursor.execute("SELECT game_id FROM games")
            _gamesdata_cache = game_cursor.fetchall()
            gamesdb.close()
    except Exception:
        _gamesdata_cache = []
    return _gamesdata_cache
# ---------------------------------------------------------------------------
# Theme system
# ---------------------------------------------------------------------------
LIGHT_TOKENS = {
    "WHITE": "#FFFFFF",
    "INK": "#18181B",
    "BODY": "#3F3F46",
    "MUTED": "#71717A",
    "PLACEHOLDER": "#A1A1AA",
    "VIOLET": "#5B1A8D",
    "PALE_VIOLET": "#F2E8F8",
    "BORDER": "#E4E4E7",
    "TRACK": "#F4F4F5",
    "DANGER": "#EF4444",
    "WARNING": "#F59E0B",
    "HEALTHY": "#22C55E",
}

DARK_TOKENS = {
    "WHITE": "#0A0A0A",       # Near-black background
    "INK": "#FFFFFF",         # Primary text (inverted)
    "BODY": "#D4D4D8",        # Secondary text
    "MUTED": "#71717A",       # Muted text (same as light)
    "PLACEHOLDER": "#52525B", # Placeholder text
    "VIOLET": "#A855F7",      # Lighter violet for dark mode
    "PALE_VIOLET": "#1E1B2E", # Dark purple for cards
    "BORDER": "#27272A",      # Dark border
    "TRACK": "#18181B",       # Dark track
    "DANGER": "#F87171",
    "WARNING": "#FBBF24",
    "HEALTHY": "#4ADE80",
}

class ThemeManager(QObject):
    theme_changed = Signal(str)

    def __init__(self):
        super().__init__()
        # Start from the persisted choice (config.json), falling back to
        # light for a first launch.
        saved = config.get_theme()
        self.current_theme = "dark" if saved == "dark" else "light"
        self.tokens = DARK_TOKENS if self.current_theme == "dark" else LIGHT_TOKENS

    def toggle(self):
        self.current_theme = "dark" if self.current_theme == "light" else "light"
        self.tokens = DARK_TOKENS if self.current_theme == "dark" else LIGHT_TOKENS
        config.set_theme(self.current_theme)
        self.theme_changed.emit(self.current_theme)
        return self.tokens

    def set_theme(self, name: str):
        """Apply a specific theme ('light' or 'dark') and persist it."""
        name = "dark" if name == "dark" else "light"
        if name == self.current_theme:
            return self.tokens
        self.current_theme = name
        self.tokens = DARK_TOKENS if name == "dark" else LIGHT_TOKENS
        config.set_theme(name)
        self.theme_changed.emit(name)
        return self.tokens

    def get_tokens(self):
        return self.tokens

# Defaulting tokens to the light set to preserve existing behavior initially
THEME = ThemeManager()
tokens = THEME.get_tokens()

# Update old references (this might need to be done iteratively)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Design tokens (the app-wide palette from the Imports redesign)
# ---------------------------------------------------------------------------
WHITE = "#FFFFFF"           # app / sidebar / main content background
INK = "#18181B"             # headings, dark accent fill
BODY = "#3F3F46"            # body text
MUTED = "#71717A"           # secondary / muted labels
PLACEHOLDER = "#A1A1AA"     # input placeholder text
VIOLET = "#5B1A8D"          # accent ("Slice", Search, ...)
PALE_VIOLET = "#F2E8F8"     # placeholder-list card tint
BORDER = "#E4E4E7"          # light borders (inputs, dropdowns)
TRACK = "#F4F4F5"           # segmented-control tracks, active-row highlight

FONT_STACK = '"Inter", "Segoe UI", sans-serif'


def _shade(hex_color, factor=120):
    """A lighter/darker variant of a hex color for hover states."""
    c = QColor(hex_color)
    if c.lightness() < 160:
        return c.lighter(factor).name()
    return c.darker(factor).name()


def _pill_qss(name, bg, fg, height):
    r = height // 2
    return f"""
    QPushButton#{name} {{
        background-color: {bg}; color: {fg};
        border: none; border-radius: {r}px;
        font-family: {FONT_STACK}; font-size: 13px; font-weight: 600;
        padding: 0 20px;
        min-width: 80px;
    }}
    QPushButton#{name}:hover {{ background-color: {_shade(bg)}; }}
    QPushButton#{name}:pressed {{ background-color: {_shade(bg, 88)}; }}
    """


def _segment_qss(track_bg, checked_bg, checked_fg, unchecked_fg,
                 track_h, button_h):
    return f"""
    QFrame#SegmentTrack {{
        background-color: {track_bg}; border: none;
        border-radius: {track_h // 2}px;
    }}
    QPushButton#Segment {{
        background-color: transparent; border: none;
        border-radius: {button_h // 2}px;
        color: {unchecked_fg};
        font-family: {FONT_STACK}; font-size: 13px; font-weight: 500;
        padding: 0 16px;
    }}
    QPushButton#Segment:checked {{
        background-color: {checked_bg}; color: {checked_fg};
    }}
    """

NAV_QSS = f"""
QFrame#NavRow {{ background-color: transparent; border: none; border-radius: 8px; }}
QFrame#NavRow[active="true"] {{ background-color: {TRACK}; }}
"""

def get_select_qss(tokens):
    return f"""
    QFrame#SelectButton {{
        background-color: {tokens['WHITE']}; border: 1px solid {tokens['BORDER']};
        border-radius: 8px;
    }}
    """

def get_search_qss(tokens):
    return f"""
    QLineEdit#SearchField {{
        background-color: {tokens['WHITE']}; border: 1px solid {tokens['BORDER']};
        border-radius: 16px; padding-left: 8px;
        color: {tokens['BODY']}; font-family: {FONT_STACK}; font-size: 13px;
    }}
    QLineEdit#SearchField:focus {{ border-color: {tokens['VIOLET']}; }}
    """

def get_menu_qss(tokens):
    return f"""
    QMenu {{
        background-color: {tokens['WHITE']}; border: 1px solid {tokens['BORDER']};
        border-radius: 8px; padding: 6px;
        font-family: {FONT_STACK}; font-size: 13px;
    }}
    QMenu::item {{ padding: 6px 14px; border-radius: 6px; color: {tokens['BODY']}; }}
    QMenu::item:selected {{ background-color: {tokens['TRACK']}; }}
    """

SELECT_QSS = get_select_qss(LIGHT_TOKENS)
SEARCH_QSS = get_search_qss(LIGHT_TOKENS)
MENU_QSS = get_menu_qss(LIGHT_TOKENS)


def set_placeholder_color(widget, color=PLACEHOLDER):
    """QSS has no placeholder-text color; set it through the palette."""
    pal = widget.palette()
    pal.setColor(QPalette.ColorRole.PlaceholderText, QColor(color))
    widget.setPalette(pal)


def make_placeholder_avatar(size, letter):
    """A rounded-square placeholder for a Pokemon sprite (real sprites
    get swapped in later via PokemonRow.set_avatar)."""
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(PALE_VIOLET))
    p.drawRoundedRect(0, 0, size, size, size * 0.28, size * 0.28)
    p.setPen(QColor(VIOLET))
    f = QFont()
    f.setPixelSize(int(size * 0.5))
    f.setBold(True)
    p.setFont(f)
    p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, letter)
    p.end()
    return pm


# ---------------------------------------------------------------------------
# Header bar
# ---------------------------------------------------------------------------
class SegmentedControl(QFrame):
    """A rounded track holding checkable pill buttons in an exclusive
    group (the Showdown/Champions and Singles/Doubles toggles)."""
    currentChanged = Signal(str)

    def __init__(self, labels, active_index=0, track_bg=TRACK,
                 checked_bg=INK, checked_fg=WHITE, unchecked_fg=BODY,
                 height=34, parent=None):
        super().__init__(parent)
        self.setObjectName("SegmentTrack")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedHeight(height)
        # Margin inside the track to keep the pill from touching the edges
        margin = 3
        button_h = height - (2 * margin)
        self.setStyleSheet(_segment_qss(
            track_bg, checked_bg, checked_fg, unchecked_fg,
            height, button_h,
        ))

        lay = QHBoxLayout(self)
        lay.setContentsMargins(margin, margin, margin, margin)
        lay.setSpacing(0)

        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.buttons = []
        for i, label in enumerate(labels):
            b = QPushButton(label)
            b.setObjectName("Segment")
            b.setCheckable(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setFixedHeight(button_h)
            if i == active_index:
                b.setChecked(True)
            self.group.addButton(b, i)
            lay.addWidget(b)
            self.buttons.append(b)
        self.group.buttonClicked.connect(self._on_clicked)

    def _on_clicked(self, button):
        self.currentChanged.emit(button.text())


class PillButton(QPushButton):
    """A fully-rounded ("pill") solid button -- the dark primary buttons
    in the header. Optionally carries a Lucide icon, trailing if the
    button is a two-word action like "Play Showdown ›"."""

    def __init__(self, text, bg=INK, fg=WHITE, height=34,
                 icon_name=None, icon_color=None, icon_size=14,
                 icon_path=None,
                 trailing=False, name="PillButton", parent=None):
        super().__init__(text, parent)
        self.setObjectName(name)
        self.setFixedHeight(height)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(_pill_qss(name, bg, fg, height))
        
        if icon_path:
            pm = QPixmap(icon_path)
            if not pm.isNull():
                # Scale to icon_size if provided, else keep as is but fit height
                if icon_size:
                    pm = pm.scaled(icon_size * 2, icon_size * 2, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
                    pm.setDevicePixelRatio(2.0)
                self.setIcon(QIcon(pm))
                self.setIconSize(pm.size() / pm.devicePixelRatio())
        elif icon_name:
            color = icon_color or fg
            self.setIcon(QIcon(lucide(icon_name, color, icon_size)))
            self.setIconSize(self.icon().pixmap(icon_size, icon_size).size())
            
        if self.icon() and not self.icon().isNull():
            if trailing:
                self.setLayoutDirection(Qt.LayoutDirection.RightToLeft)


class DarkToggle(QPushButton):
    """Circular dark-mode toggle with a sun/moon icon."""

    def __init__(self, theme_manager, parent=None):
        super().__init__(parent)
        self.theme_manager = theme_manager
        self.setObjectName("DarkToggle")
        self.setFixedSize(34, 34)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._update_style()
        self._update_icon()
        self.setToolTip("Toggle dark mode")

    def _update_style(self):
        # Button always uses tokens["INK"] (dark in light mode, white in dark mode)
        bg = self.theme_manager.tokens["INK"]
        self.setStyleSheet(f"""
            QPushButton#DarkToggle {{
                background-color: {bg}; border: none; border-radius: 17px;
            }}
            QPushButton#DarkToggle:hover {{ background-color: {_shade(bg)}; }}
        """)

    def _update_icon(self):
        # Based on your request: Sun for Light theme, Moon for Dark theme.
        icon_name = "moon" if self.theme_manager.current_theme == "dark" else "sun"
        # Icon color contrasts with button background (always tokens["INK"]):
        # Light Theme (Dark BG=INK=#18181B) -> White Icon (WHITE=#FFFFFF)
        # Dark Theme (Light BG=INK=#FFFFFF) -> Black Icon (WHITE=#0A0A0A)
        icon_color = self.theme_manager.tokens["WHITE"]
        self.setIcon(QIcon(lucide(icon_name, icon_color, 18)))
        self.setIconSize(self.icon().pixmap(16, 16).size())

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.theme_manager.toggle()
            self._update_style()
            self._update_icon()
            
            # Immediately update the logo in the HeaderBar
            p = self.parent()
            while p:
                if hasattr(p, "_update_logo"):
                    p._update_logo()
                    break
                p = p.parent()
        super().mouseReleaseEvent(event)


class HeaderBar(QWidget):
    """Full-width bar above the main content: Showdown/Champions segmented 
    control, then a right-aligned cluster of pills and the dark toggle."""

    def __init__(self, theme_manager=None, parent=None):
        super().__init__(parent)
        self.theme_manager = theme_manager or THEME
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedHeight(88)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(24, 10, 24, 10)
        lay.setSpacing(16)

        # -- Showdown / Champions segmented control --
        self.segmented = SegmentedControl(
            ["Showdown", "Champions"], active_index=0, height=36,
        )
        lay.addWidget(self.segmented)

        lay.addStretch(1)

        # -- right-aligned cluster: two stacked rows --
        cluster = QVBoxLayout()
        cluster.setSpacing(8)
        cluster.setAlignment(Qt.AlignmentFlag.AlignRight)

        row1 = QHBoxLayout()
        row1.setSpacing(8)
        row1.addStretch(1)
        self.coffee_button = PillButton("Buy me a coffee!", name="CoffeeBtn", height= 36)
        self.coffee_button.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl("https://buymeacoffee.com/catiedev"))
        )
        self.dark_toggle = DarkToggle(self.theme_manager)
        row1.addWidget(self.coffee_button)
        row1.addWidget(self.dark_toggle)
        cluster.addLayout(row1)

        row2 = QHBoxLayout()
        row2.addStretch(1)
        self.play_button = PillButton(
            "Play Showdown", icon_name="chevron-right", icon_color=None,
            icon_size=14, height=36, trailing=True, name="PlayShowdownBtn",
        )
        # Connect the click handler once during initialization, not on every theme change
        self.play_button.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl("https://play.pokemonshowdown.com"))
        )
        row2.addWidget(self.play_button)
        cluster.addLayout(row2)

        lay.addLayout(cluster)

    def _apply_theme(self):
        """Apply theme-aware styling to header bar widgets."""
        if not self.theme_manager:
            return
        tokens = self.theme_manager.get_tokens()
        self.setStyleSheet(f"background-color: {tokens['WHITE']};")

        # Segmented control
        self.segmented.setStyleSheet(_segment_qss(
            tokens['TRACK'], tokens['INK'], tokens['WHITE'], tokens['BODY'],
            36, 30,
        ))

        # Coffee button - dark pill
        self.coffee_button.setStyleSheet(_pill_qss("CoffeeBtn", tokens['INK'], tokens['WHITE'], 36))

        # Play Showdown button - dark pill with chevron
        self.play_button.setStyleSheet(_pill_qss("PlayShowdownBtn", tokens['INK'], tokens['WHITE'], 36))
        self.play_button.setIcon(lucide_icon("chevron-right", tokens['WHITE'], 14))
        self.play_button.setLayoutDirection(Qt.LayoutDirection.RightToLeft)

        # Dark toggle
        self.dark_toggle._update_style()
        self.dark_toggle._update_icon()

    # ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------
class NavRow(QFrame):
    """One row in the sidebar's nav list: icon + label, highlighted
    (light-gray rounded-rect) when it's the active page."""

    clicked = Signal(str)

    def __init__(self, key, label, icon_name, active=False, parent=None):
        super().__init__(parent)
        self.setObjectName("NavRow")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(NAV_QSS)
        self._key = key
        self._icon_name = icon_name


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
        # Use a slightly smaller icon size inside the 28x28 container to ensure no clipping
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


class SelectButton(QFrame):
    """A dropdown-styled select: bordered rounded box showing the current
    choice + a chevron-down icon; clicking pops a QMenu. Behaves like a
    QComboBox for callers (currentText(), changed signal, set_items)
    but is fully stylable to match the design."""

    changed = Signal(str)

    def __init__(self, items, current=None, parent=None, theme_manager=None):
        super().__init__(parent)
        self.theme_manager = theme_manager or THEME
        self.setObjectName("SelectButton")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedHeight(36)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        
        self._apply_style()
        
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 0, 10, 0)
        lay.setSpacing(6)
        self._value = QLabel()
        self._value.setStyleSheet(
            f"color: {self.theme_manager.tokens['BODY']}; font-size: 13px; font-weight: 500;"
            f"font-family: {FONT_STACK};"
        )
        chevron = QLabel()
        chevron.setPixmap(lucide("chevron-down", self.theme_manager.tokens['MUTED'], 14))
        lay.addWidget(self._value)
        lay.addStretch(1)
        lay.addWidget(chevron)

        self._menu = QMenu(self)
        self._menu.setStyleSheet(get_menu_qss(self.theme_manager.tokens))
        self._items = list(items)
        for item in self._items:
            action = self._menu.addAction(item)
            action.triggered.connect(
                lambda checked=False, value=item: self._choose(value)
            )

        self._current = None
        if self._items:
            self._choose(current if current is not None else self._items[0],
                         emit=False)
        else:
            self._value.setText("")

        # Connect to theme changes
        self.theme_manager.theme_changed.connect(self._on_theme_changed)

    def _on_theme_changed(self, theme_name):
        self._apply_style()
        self._value.setStyleSheet(
            f"color: {self.theme_manager.tokens['BODY']}; font-size: 13px; font-weight: 500;"
            f"font-family: {FONT_STACK};"
        )
        chevron = self.findChild(QLabel)
        if chevron and chevron.pixmap():
            # Recreate chevron with new color
            chevron.setPixmap(lucide("chevron-down", self.theme_manager.tokens['MUTED'], 14))
        self._menu.setStyleSheet(get_menu_qss(self.theme_manager.tokens))

    def _apply_style(self):
        self.setStyleSheet(get_select_qss(self.theme_manager.tokens))

    def currentText(self):
        return self._current

    def set_items(self, items, keep_current=True):
        self._menu.clear()
        self._items = list(items)
        for item in self._items:
            action = self._menu.addAction(item)
            action.triggered.connect(
                lambda checked=False, value=item: self._choose(value)
            )
        if not keep_current and self._items:
            self._choose(self._items[0], emit=False)

    def _choose(self, value, emit=True):
        if value == self._current:
            return
        self._current = value
        self._value.setText(value)
        if emit:
            self.changed.emit(value)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._items:
            self._menu.exec(self.mapToGlobal(QPoint(0, self.height())))
        super().mouseReleaseEvent(event)


class PokemonRow(QFrame):
    """One row in the sidebar's Top Pokemon list: sprite avatar, name,
    and right-aligned rank above a muted percentage line.

    Clicking a row emits ``clicked(str)`` with the Pokemon's FULL display
    name (the row's on-screen label may be clipped/truncated)."""

    clicked = Signal(str)

    def __init__(self, name="Kingambit", rank="#1", pct="36%",
                 sprite=None, parent=None, fullname=None):
        super().__init__(parent)
        self.setObjectName("PokemonRow")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet("QFrame#PokemonRow { background: transparent; }")
        self.setFixedHeight(56)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._fullname = fullname or name
        self._click_connected = False  # instance flag; survives set_data relabels

        # Reserve a fixed width for the rank/# column (fits "#999" = 52px at
        # 14px semibold) and a right margin so the rank never drifts with the
        # name's pixel width or sits flush against the vertical scrollbar.
        # The name gets every other pixel, so long/wide names are only
        # clipped when they truly can't fit, never at the cost of the rank.
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 8, 10, 8)
        lay.setSpacing(10)

        self._avatar = QLabel()
        self._avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        if sprite is not None:
            self.set_avatar(sprite)
        else:
            self.set_avatar(make_placeholder_avatar(36, name[0] if name else "?"))
        lay.addWidget(self._avatar)

        middle = QVBoxLayout()
        middle.setSpacing(0)
        self._name = QLabel(name)
        # Preferred (the default) lets the name size to its text normally but
        # shrink -- clipping, not overflowing -- when the fixed rank column
        # needs the room, so a wide name can't shove the rank off the row.
        self._name.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        self._name.setStyleSheet(
            f"color: {INK}; font-size: 14px; font-weight: 600;"
            f"font-family: {FONT_STACK};"
        )
        middle.addWidget(self._name)
        lay.addLayout(middle)

        lay.addStretch(1)

        right = QVBoxLayout()
        right.setSpacing(0)
        self._rank = QLabel(rank)
        self._rank.setAlignment(Qt.AlignmentFlag.AlignRight)
        # Pin the width so the rank/# sits in exactly the same column on
        # every row regardless of the name's pixel width or the rank's own
        # digit count. 54px fits "#999" (52px). Rarer 4-digit ranks widen the
        # column but keep the same right edge, so the numbers still line up.
        self._rank.setFixedWidth(54)
        self._rank.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
        self._rank.setStyleSheet(
            f"color: {INK}; font-size: 14px; font-weight: 600;"
            f"font-family: {FONT_STACK};"
        )
        self._pct = QLabel(pct)
        self._pct.setAlignment(Qt.AlignmentFlag.AlignRight)
        self._pct.setStyleSheet(
            f"color: {MUTED}; font-size: 11px; font-family: {FONT_STACK};"
        )
        right.addWidget(self._rank)
        right.addWidget(self._pct)
        lay.addLayout(right)

        self._apply_theme(THEME.get_tokens())

    def clear_data(self):
        """Blank the labels so a spare (hidden) row can't leak its old
        pokemon into a search filter or a later format swap."""
        self._fullname = ""
        self._name.setText("")
        self._rank.setText("")
        self._pct.setText("")

    def connect_click(self, slot):
        """Idempotently connect the clicked signal to `slot`. Rows are reused
        (relabeled) across format switches, so this must be safe to call again
        on the same row without stacking duplicates."""
        if not self._click_connected:
            self.clicked.connect(slot)
            self._click_connected = True

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._fullname:
            self.clicked.emit(self._fullname)
        super().mouseReleaseEvent(event)

    def _apply_theme(self, tokens):
        """Re-style text with the current theme (dark INK would be
        invisible on the dark sidebar)."""
        self._name.setStyleSheet(
            f"color: {tokens['INK']}; font-size: 14px; font-weight: 600;"
            f"font-family: {FONT_STACK}; background: transparent;"
        )
        self._rank.setStyleSheet(
            f"color: {tokens['INK']}; font-size: 14px; font-weight: 600;"
            f"font-family: {FONT_STACK}; background: transparent;"
        )
        self._pct.setStyleSheet(
            f"color: {tokens['MUTED']}; font-size: 11px; font-family: {FONT_STACK}; background: transparent;"
        )

    def set_avatar(self, pixmap):
        """Swap in a real sprite pixmap (from SpriteStore) later."""
        self._avatar.setPixmap(pixmap)
        self._avatar.setFixedSize(pixmap.size())

    def set_data(self, name, rank, pct, sprite=None, fullname=None):
        """Reuse this row for a different Pokemon instead of rebuilding the
        widget. Far cheaper than a full recreate on every format switch,
        which is what made the sidebar feel sluggish."""
        self._fullname = fullname or name
        self._name.setText(name)
        self._rank.setText(rank)
        self._pct.setText(pct)
        # Always sync the avatar: a pokemon with no sprite (e.g. a regional
        # form whose id is outside the assets folder) must revert to the
        # letter placeholder, never keep the previous pokemon's image.
        if sprite is not None:
            self.set_avatar(sprite)
        else:
            self.set_avatar(make_placeholder_avatar(36, name[0] if name else "?"))


class PokemonList(QScrollArea):
    """Scrollable vertical stack of PokemonRow widgets (a QScrollArea
    with a QVBoxLayout of custom rows, per the redesign notes)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("PokemonList")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setStyleSheet("QScrollArea#PokemonList { background: transparent; border: none; }")
        self.viewport().setAutoFillBackground(False)

        self._container = QWidget()
        self._container.setStyleSheet("background: transparent;")
        # Ignored horizontal policy: without it, setWidgetResizable grows the
        # container to the rows' minimum width (which follows the widest name
        # sizeHint). With the horizontal scrollbar off, that overflow pushed
        # the right-aligned rank/# past the viewport edge and under the
        # scrollbar. Clamping to the viewport makes every row lay out at the
        # true available width instead, so the rank always fits.
        self._container.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._lay = QVBoxLayout(self._container)
        self._lay.setContentsMargins(0, 0, 0, 0)
        self._lay.setSpacing(2)
        self._active_count = 0
        self.setWidget(self._container)

    def add_row(self, row):
        self._lay.addWidget(row)

    def clear_rows(self):
        while self._lay.count():
            item = self._lay.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def set_pokemon_rows(self, spec_rows):
        """Render a list of row dicts ({name, rank, pct, sprite}) into the
        list, reusing existing PokemonRow widgets when possible so switching
        formats is a relabel, not a rebuild. Spare widgets beyond the new
        count are hidden and cleared so stale names can never leak back into
        view; the widget count only grows the first time the longest format
        is seen."""
        existing = self.rows()
        for i, spec in enumerate(spec_rows):
            if i < len(existing):
                row = existing[i]
                row.set_data(spec["name"], spec["rank"], spec["pct"], spec.get("sprite"),
                             fullname=spec.get("fullname"))
                row.show()
            else:
                self.add_row(PokemonRow(
                    spec["name"], spec["rank"], spec["pct"], spec.get("sprite"),
                    fullname=spec.get("fullname")))
        for row in existing[len(spec_rows):]:
            row.clear_data()
            row.hide()
        self._active_count = len(spec_rows)

    def active_rows(self):
        """Only the rows currently representing a Pokemon (excludes the
        hidden spare widgets that are kept around for cheap format swaps)."""
        rows = self.rows()
        return rows[:self._active_count]

    def rows(self):
        return [
            self._lay.itemAt(i).widget()
            for i in range(self._lay.count())
            if self._lay.itemAt(i).widget() is not None
        ]



# ---------------------------------------------------------------------------
# Main content widgets
# ---------------------------------------------------------------------------

class PlaceholderListCard(QWidget):
    """Pale violet card with 7 'List item' rows."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        self._items = []
        gamesdata = get_gamesdata()
        for i in range(min(7, len(gamesdata))):
            # Extract game_id from tuple - gamesdata contains tuples like ('game_id',)
            game_id = gamesdata[i][0] if gamesdata[i] else ''
            item = QLabel(str(game_id))
            self._items.append(item)
            layout.addWidget(item)
    
    def set_theme_tokens(self, tokens):
        """Apply theme-aware styling to this card."""
        self.setStyleSheet(f"""
            QWidget {{
                background-color: {tokens['PALE_VIOLET']};
                border-radius: 16px;
            }}
        """)
        
        for item in self._items:
            item.setStyleSheet(f"""
                color: {tokens['BODY']};
                font-family: {FONT_STACK};
                font-size: 13px;
                font-weight: 500;
            """)


# ---------------------------------------------------------------------------
# Page assembly
# ---------------------------------------------------------------------------
class ImportsPage(QWidget):
    """Header bar over main content. The sidebar is now managed by AppLayout."""
    
    def __init__(self, theme_manager=None, usernames=None, conn=None, parent=None):
        super().__init__(parent)
        self.theme_manager = theme_manager
        self.usernames = usernames or []
        self.conn = conn
        self.bulk_thread = None
        self.bulk_worker = None
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Top header bar
        self.header = HeaderBar(theme_manager)
        root.addWidget(self.header)

        # Main content area (scrollable for narrow windows)
        self.main_scroll = QScrollArea()
        self.main_scroll.setWidgetResizable(True)
        self.main_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.main_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        self.main_content = QWidget()
        self.main_content.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        main_lay = QVBoxLayout(self.main_content)
        main_lay.setContentsMargins(40, 32, 40, 40)
        main_lay.setSpacing(0)

        # 1. Page heading: "Showdown Imports"
        self.page_title = QLabel("Showdown Imports")
        main_lay.addWidget(self.page_title)

        # 2. "Searching for: <username>" line + violet pills
        main_lay.addSpacing(12)
        search_row = QHBoxLayout()
        search_row.setSpacing(12)

        self.username_label = QLabel(f"Searching for: {self.usernames[0] if self.usernames else '(none)'}")
        search_row.addWidget(self.username_label)

        self.change_username_btn = PillButton("Change Username", name="ChangeUsernameBtn")
        self.change_username_btn.clicked.connect(self.change_username)

        self.sync_btn = PillButton("Sync", name="SyncBtn")
        self.sync_btn.clicked.connect(self.start_sync)
        
        search_row.addWidget(self.change_username_btn)
        search_row.addWidget(self.sync_btn)

        search_row.addStretch(1)
        self.last_synced_label = QLabel(self._last_synced_text())
        search_row.addWidget(self.last_synced_label)
        self._search_row = search_row
        main_lay.addLayout(search_row)

        # 3. "Manual Input" label
        main_lay.addSpacing(28)
        self.manual_input_label = QLabel("Manual Input")
        main_lay.addWidget(self.manual_input_label)

        # 4. URL input row
        main_lay.addSpacing(12)
        url_row = QHBoxLayout()
        url_row.setSpacing(12)

        self.url_input = QLineEdit()
        self.url_input.setObjectName("UrlInput")
        self.url_input.setPlaceholderText("Input Showdown Replay URL here...")
        self.url_input.setMinimumHeight(46)
        self.url_input.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        url_row.addWidget(self.url_input, 1)

        self.search_btn = QPushButton("Search")
        self.search_btn.setObjectName("SearchBtn")
        self.search_btn.clicked.connect(self.import_manual_replay)
        self.search_btn.setFixedSize(100, 46)
        self.search_btn.clicked.connect(self.import_manual_replay)
        url_row.addWidget(self.search_btn)
        main_lay.addLayout(url_row)

        # 5. Placeholder-list card
        main_lay.addSpacing(20)
        self.placeholder_card = PlaceholderListCard()
        main_lay.addWidget(self.placeholder_card)

        main_lay.addStretch(1)
        self.main_scroll.setWidget(self.main_content)

        # -- Champions placeholder mode: replaces the Showdown content when
        #    the Showdown/Champions toggle is switched to Champions. --
        self.champions_widget = QWidget()
        self.champions_widget.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        champ_lay = QVBoxLayout(self.champions_widget)
        champ_lay.setContentsMargins(40, 80, 40, 40)
        champ_lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.champions_label = QLabel("Coming soon!")
        champ_lay.addWidget(self.champions_label)

        self.content_stack = QStackedWidget()
        self.content_stack.addWidget(self.main_scroll)
        self.content_stack.addWidget(self.champions_widget)
        root.addWidget(self.content_stack)

        # Show the right page when the Showdown/Champions toggle changes.
        self.header.segmented.currentChanged.connect(self._on_mode_changed)

        # Initial theme application
        self._apply_theme()

        # Connect to theme manager if provided
        if self.theme_manager:
            self.theme_manager.theme_changed.connect(self._apply_theme)

    def _apply_theme(self, *_args):
        if not self.theme_manager:
            return

        tokens = self.theme_manager.get_tokens()

        # Self + main content backgrounds (pure white)
        self.setStyleSheet(
            f"QWidget {{ background-color: {tokens['WHITE']}; font-family: {FONT_STACK}; }}"
        )
        self.main_content.setStyleSheet(f"background-color: {tokens['WHITE']};")
        self.main_scroll.setStyleSheet(f"""
            QScrollArea {{ background-color: {tokens['WHITE']}; border: none; }}
        """)
        self.champions_widget.setStyleSheet(f"background-color: {tokens['WHITE']};")

        # Header bar
        if hasattr(self.header, '_apply_theme'):
            self.header._apply_theme()

        # Page title
        self.page_title.setStyleSheet(f"""
            color: {tokens['INK']};
            font-family: {FONT_STACK};
            font-size: 28px;
            font-weight: 700;
        """)

        # Username label
        self.username_label.setStyleSheet(f"""
            color: {tokens['BODY']};
            font-family: {FONT_STACK};
            font-size: 13px;
            font-weight: 500;
        """)

        # Last-synced / sync-status label (muted, right side of search row)
        self.last_synced_label.setStyleSheet(f"""
            color: {tokens['MUTED']};
            font-family: {FONT_STACK};
            font-size: 12px;
        """)

        # Violet pill buttons (Solid fill #5B1A8D, white text)
        self.change_username_btn.setStyleSheet(_pill_qss("ChangeUsernameBtn", tokens['VIOLET'], tokens['WHITE'], 32))
        self.sync_btn.setStyleSheet(_pill_qss("SyncBtn", tokens['VIOLET'], tokens['WHITE'], 32))

        # Manual input label (muted gray)
        self.manual_input_label.setStyleSheet(f"""
            color: {tokens['MUTED']};
            font-family: {FONT_STACK};
            font-size: 12px;
            font-weight: 600;
        """)

        # URL input
        self.url_input.setStyleSheet(f"""
            QLineEdit#UrlInput {{
                background-color: {tokens['WHITE']};
                border: 1px solid {tokens['BORDER']};
                border-radius: 10px;
                padding: 0 16px;
                font-family: {FONT_STACK};
                font-size: 14px;
                color: {tokens['BODY']};
            }}
            QLineEdit#UrlInput:focus {{
                border: 1px solid {tokens['VIOLET']};
            }}
        """)
        set_placeholder_color(self.url_input, tokens['PLACEHOLDER'])

        # Search button (solid violet, white text)
        self.search_btn.setStyleSheet(_pill_qss("SearchBtn", tokens['VIOLET'], tokens['WHITE'], 46))

        # Placeholder-list card
        if hasattr(self, 'placeholder_card'):
            self.placeholder_card.set_theme_tokens(tokens)

        # Champions placeholder page
        self.champions_label.setStyleSheet(f"""
            color: {tokens['INK']};
            font-family: {FONT_STACK};
            font-size: 28px;
            font-weight: 700;
        """)


    def _on_mode_changed(self, label):
        """Swap between the Showdown content and the Champions placeholder
        when the Showdown/Champions toggle in the header changes."""
        if not hasattr(self, 'content_stack'):
            return
        if label == "Champions":
            self.content_stack.setCurrentWidget(self.champions_widget)
        else:
            self.content_stack.setCurrentWidget(self.main_scroll)


    def change_username(self):
        from fourslice.gui.import_tab import prompt_for_usernames
        new_usernames = prompt_for_usernames(current_usernames=self.usernames)
        if new_usernames:
            self.usernames = new_usernames
            config.set_usernames(new_usernames)
            self.username_label.setText(f"Searching for: {', '.join(self.usernames)}")

    def _last_synced_text(self):
        timestamp = config.get_last_synced()
        if not timestamp:
            return "Last synced: never"
        local_time = datetime.fromisoformat(timestamp).astimezone()
        return f"Last synced: {local_time.strftime('%Y-%m-%d %H:%M')}"

    def _sync_label_max_width(self):
        """How wide the sync-status label may be before its text stretches the
        search row -- and with it the whole page -- wider than the scroll
        viewport. Measured from the row's real sibling widgets so it tracks
        both the fonts actually in use and window resizes. Falls back to the
        default window width if the page isn't shown yet."""
        viewport = self.main_scroll.viewport().width()
        if viewport <= 0:
            viewport = 832  # default window (1100) minus the 268px sidebar; overwritten once the real (resized) viewport is measurable
        row = self._search_row
        used = 0
        for i in range(row.count()):
            item = row.itemAt(i)
            if item is not None and item.widget() is not None and \
                    item.widget() is not self.last_synced_label:
                used += item.widget().sizeHint().width()
        used += (row.count() - 1) * row.spacing()
        margins = self.main_content.layout().contentsMargins()
        return max(80, viewport - margins.left() - margins.right() - used)

    def _set_sync_status(self, message):
        """Update the sync-status bubble, eliding to _sync_label_max_width
        so it never gets wide enough to stretch the page while importing.
        The full message stays in the label's tooltip."""
        label = self.last_synced_label
        font_metrics = label.fontMetrics()
        max_width = self._sync_label_max_width()
        if font_metrics.horizontalAdvance(message) > max_width:
            label.setText(
                font_metrics.elidedText(message, Qt.TextElideMode.ElideRight, max_width)
            )
        else:
            label.setText(message)
        label.setToolTip(message)
        self._sync_status_full = message

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # The sync-status bubble was elided for the previous width; if the
        # window shrank, re-elide against the new space so it stays capped
        # (see _set_sync_status). Nothing to do before the first sync message.
        full = getattr(self, "_sync_status_full", None)
        if full is not None:
            self._set_sync_status(full)

    def start_sync(self):
        """
        Scrapes and imports every saved replay for every configured
        username, back to back. Runs the "Sync" button AND the
        automatic startup sync (main.py calls this right after the
        window is shown) -- both land here.

        Each account's history is scanned newest-first and stops the
        moment it hits an already-imported replay, so a repeat run is
        cheap -- see BulkImportWorker._import_urls. Runs on a
        background QThread so the window never freezes; the worker
        opens its own sqlite connection (see bulk_import_worker.py).
        Deliberately NOT called from __init__ so constructing an
        ImportsPage in tests doesn't spin up a real network thread.
        """
        if self.bulk_thread is not None:
            return  # a sync is already running (or winding down)

        self.sync_btn.setEnabled(False)
        self.sync_btn.setText("Syncing…")
        self._set_sync_status(
            f"Starting sync for: {', '.join(self.usernames)}..."
        )

        self.bulk_thread = QThread()
        self.bulk_worker = BulkImportWorker(
            str(config.get_db_path()), self.usernames, self.usernames
        )
        self.bulk_worker.moveToThread(self.bulk_thread)

        self.bulk_thread.started.connect(self.bulk_worker.run)
        self.bulk_worker.progress.connect(self.on_sync_progress)
        self.bulk_worker.finished.connect(self.on_sync_finished)
        self.bulk_worker.finished.connect(self.bulk_thread.quit)
        self.bulk_worker.finished.connect(self.bulk_worker.deleteLater)
        self.bulk_thread.finished.connect(self.bulk_thread.deleteLater)
        self.bulk_thread.finished.connect(self._on_sync_thread_finished)

        self.bulk_thread.start()

    def on_sync_progress(self, done, total, message):
        self._set_sync_status(message)

    def _on_sync_thread_finished(self):
        """Drop our references to the sync thread/worker.

        QThread.finished fires only after the thread's event loop has
        fully exited, so it is safe to release them here.  Dropping them
        earlier (e.g. in on_sync_finished, while the thread is still
        winding down) destroys the still-running C++ QThread and Qt's
        QThread destructor calls abort() — killing the entire app
        with no traceback.
        """
        self.bulk_thread = None
        self.bulk_worker = None

    def on_sync_finished(self, summary):
        self.sync_btn.setEnabled(True)
        self.sync_btn.setText("Sync")

        config.set_last_synced(datetime.now(timezone.utc).isoformat(timespec="seconds"))
        imported = summary.get("imported", 0)
        if imported:
            detail = f"Synced -- {imported} new replay(s) imported."
        else:
            detail = "Synced -- no new replays."
        self._set_sync_status(f"{detail}   {self._last_synced_text()}")

    def import_manual_replay(self):
        url = self.url_input.text().strip()
        if not url:
            return
        self.search_btn.setEnabled(False)
        self.search_btn.setText("Importing…")

        worker = SingleImportWorker(url, self.conn, self.usernames, config.get_store_logs())
        thread = QThread()
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(lambda result: self.on_manual_import_finished(result, thread, worker))
        worker.error.connect(lambda err: self.on_manual_import_error(err, thread, worker))
        thread.start()

    def on_manual_import_finished(self, result, thread, worker):
        self.search_btn.setEnabled(True)
        self.search_btn.setText("Search")
        thread.quit()
        thread.wait()
        worker.deleteLater()
        thread.deleteLater()
        if result["status"] == "imported":
            self.url_input.clear()
        else:
            message_fn = {
                "skipped_random_battle": lambda r: f"Skipped (Random Battle, not tracked): {r.get('format', '')}",
                "skipped_unsupported_battle_size": lambda r: f"Skipped (not singles/doubles -- {r.get('battle_size', '?')}): unsupported format",
                "empty_log": lambda r: "No data could be parsed from this replay.",
            }.get(result["status"])
            log.info(message_fn(result) if message_fn else str(result))

    def on_manual_import_error(self, error_msg, thread, worker):
        self.search_btn.setEnabled(True)
        self.search_btn.setText("Search")
        thread.quit()
        thread.wait()
        worker.deleteLater()
        thread.deleteLater()
        url = self.url_input.text().strip()
        log.error(f"Failed to import {url}: {error_msg}")


def main():
    """Run a standalone preview window (python -m fourslice.gui.imports_page)."""
    import sys
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication(sys.argv)
    page = ImportsPage()
    page.resize(1100, 760)
    page.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
