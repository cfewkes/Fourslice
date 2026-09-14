"""
fourslice/gui/teams_tab.py

The "Teams" tab: a full team builder in FourSlice style.

State A (browse) : format selector, team header, 2x3 roster grid, and a
                   usage-ordered picker with a search bar that acts as a
                   filter (type / move / ability chips + live name filter).
State B (edit)   : per-slot edit panel with Identity / Details / Moves /
                   Stats zones, plus a Showdown-style EV/nature editor.
Extras          : team save/load (storage.tb_teams), Showdown-paste export
                   to the clipboard, and a "top threats to your team"
                   counter strip computed across the whole roster.
"""

from collections import defaultdict

from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QApplication, QComboBox, QCompleter, QFrame, QGridLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QScrollArea, QSizePolicy, QStackedWidget,
    QVBoxLayout, QWidget,
)

from fourslice.gui.icons import lucide
from fourslice.gui.imports_page import (
    THEME, FONT_STACK, PillButton, set_placeholder_color,
    WHITE, INK, BODY, MUTED, VIOLET, PALE_VIOLET, BORDER, TRACK,
)
from fourslice.storage import (
    get_tb_teams, get_tb_team, load_pokedex_pokemon,
    save_tb_team, update_tb_team, delete_tb_team,
)
from fourslice import teambuilder_data as td
from fourslice.gui.pokemon_info import (
    build_list_square,
    kv_label as _kv_label,
    meta_row,
)

TYPE_COLORS = {
    "normal": "#A8A878",
    "fire": "#EF4444",
    "water": "#3B82F6",
    "grass": "#22C55E",
    "electric": "#EAB308",
    "ice": "#06B6D4",
    "fighting": "#B91C1C",
    "poison": "#A855F7",
    "ground": "#D97706",
    "flying": "#8B5CF6",
    "psychic": "#EC4899",
    "bug": "#84CC16",
    "rock": "#78350F",
    "ghost": "#6B21A8",
    "dragon": "#4338CA",
    "dark": "#374151",
    "steel": "#64748B",
    "fairy": "#F472B6",
}

CATEGORY_COLORS = {
    "Physical": "#E07474",
    "Special": "#6CA3E0",
    "Status": "#9AA3AF",
}

DEFAULT_FORMAT = "[Gen 9] OU"
POKEMON_MODE = "pokemon"
MOVE_MODE = "move"
ITEM_MODE = "item"

# Edit-panel meta squares: Abilities is a fixed card sized for 3 compact rows;
# Items has no fixed height and stretches to fill exactly (240 - ABILITIES_H -
# spacing), which is what keeps the two columns bottomed out on one line.
ABILITIES_H = 110
ITEMS_MIN_H = 88


def make_type_chip(text, color):
    chip = QLabel(text)
    chip.setAlignment(Qt.AlignmentFlag.AlignCenter)
    chip.setFixedHeight(22)
    chip.setContentsMargins(8, 0, 8, 0)
    chip.setProperty("orig_color", color)
    chip.setStyleSheet(f"""
        background-color: {color};
        color: white;
        border-radius: 11px;
        font-weight: 800;
        font-size: 10px;
        font-family: {FONT_STACK};
    """)
    return chip


def _slot_label(slot):
    """Roster / identity label for a slot: Showdown name (+ gender if set)."""
    if not slot.get("species"):
        return ""
    name = td._showdown_poke_name(slot["species"])
    if slot.get("gender") in ("M", "F"):
        name = f"{name} ({slot['gender']})"
    return name


def _card_qss():
    t = THEME.get_tokens()
    return f"QFrame {{ background-color: {t['WHITE']}; border: 1px solid {t['BORDER']}; border-radius: 12px; }}"


def _input_qss():
    t = THEME.get_tokens()
    return f"""
        QLineEdit {{
            background-color: {t['WHITE']};
            color: {t['INK']};
            border: 1px solid {t['BORDER']};
            border-radius: 6px;
            padding: 0 6px;
            font-family: {FONT_STACK};
            font-size: 12px;
        }}
        QLineEdit:focus {{ border-color: {t['VIOLET']}; }}
    """


def _combo_qss():
    t = THEME.get_tokens()
    return f"""
        QComboBox {{
            background-color: {t['WHITE']};
            color: {t['INK']};
            border: 1px solid {t['BORDER']};
            border-radius: 8px;
            padding: 0 8px;
            font-family: {FONT_STACK};
            font-size: 12px;
        }}
        QComboBox::drop-down {{ border: none; width: 22px; }}
        QComboBox::down-arrow {{ width: 0; height: 0; border-left: 4px solid transparent;
            border-right: 4px solid transparent; border-top: 5px solid {t['MUTED']}; }}
        QComboBox QAbstractItemView {{
            background-color: {t['WHITE']};
            color: {t['INK']};
            border: 1px solid {t['BORDER']};
            selection-background-color: {t['PALE_VIOLET']};
            selection-color: {t['INK']};
            font-family: {FONT_STACK};
            font-size: 12px;
        }}
    """


def _pill_qss_for(bg, fg=None, h=44):
    tokens = THEME.get_tokens()
    fg = fg or tokens['WHITE']
    return f"""
        QPushButton {{
            background-color: {bg};
            color: {fg};
            border: none;
            border-radius: {h // 2}px;
            font-family: {FONT_STACK};
            font-size: 12px;
            font-weight: 700;
            padding: 0 20px;
            min-width: 84px;
        }}
    """


def _counter_chip_qss(tokens):
    """QSS for the 'Top threats to your team' counter chips."""
    return f"""
        QPushButton {{
            background-color: {tokens['PALE_VIOLET']};
            color: {tokens['INK']};
            border: 1px solid {tokens['BORDER']};
            border-radius: 14px;
            padding: 0 12px;
            font-family: {FONT_STACK};
            font-size: 11px;
            font-weight: 700;
        }}
        QPushButton:hover {{ border-color: {tokens['VIOLET']}; }}
    """


def _catalog_row_style(role, tokens):
    """Stylesheet for one picker-row text label role.

    Row widgets are built once (baking token colors at build time); on a
    theme change they are recolored in place with this -- never rebuilt.
    """
    specs = {
        "pmon_name":    ("INK", "13px", "700"),
        "pmon_ability": ("INK", "11px", "500"),
        "pmon_stat":    ("INK", "12px", "400"),
        "pmon_bst":     ("INK", "12px", "700"),
        "move_name":    ("INK", "12px", "700"),
        "item_name":    ("INK", "12px", "700"),
        "move_desc":    ("MUTED", "11px", "400"),
        "item_desc":    ("MUTED", "11px", "400"),
    }
    spec = specs.get(role)
    if spec is None:
        return ""
    key, size, weight = spec
    return (f"color: {tokens[key]}; font-family: {FONT_STACK}; font-size: {size};"
            f" font-weight: {weight}; background: transparent;")


# ---------------------------------------------------------------------------
# Small building blocks
# ---------------------------------------------------------------------------

class ClickableLabel(QLabel):
    """A QLabel that emits clicked() — used for picker rows and fields."""
    clicked = Signal()

    def __init__(self, text="", parent=None):
        super().__init__(text, parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class FilterChip(QPushButton):
    """A search suggestion / active-filter chip. One click toggles it."""

    def __init__(self, label, color=None, active=False, kind=None, value=None, parent=None):
        super().__init__(parent)
        self.kind = kind
        self.value = value
        self._color = color if color is not None else THEME.get_tokens()['VIOLET']
        self._active = active
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(28)
        self.setText(f"{label}  ✕" if active else label)
        self._apply_style()

    def _apply_style(self):
        if self._active:
            bg, fg, border = self._color, "#FFFFFF", self._color
        else:
            bg, fg, border = "transparent", self._color, self._color
        self.setStyleSheet(f"""
            QPushButton {{
                background-color: {bg};
                color: {fg};
                border: 1px solid {border};
                border-radius: 14px;
                padding: 0 12px;
                font-family: {FONT_STACK};
                font-size: 11px;
                font-weight: 700;
            }}
        """)


class MoveField(QFrame):
    """One of the four move slots in the edit panel: name or '+ Move'."""
    clicked = Signal()

    def __init__(self, name="", parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(36)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 0, 8, 0)
        self.label = QLabel()
        self.label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.label.setStyleSheet("background: transparent;")
        lay.addWidget(self.label)
        lay.addStretch(1)
        self._name = ""
        self.set_move(name)
        self._apply_theme(THEME.get_tokens())

    def set_move(self, name):
        self._name = name
        self.setProperty("filled", bool(name))
        self.style().unpolish(self)
        self.style().polish(self)
        self._apply_theme(THEME.get_tokens())

    def _apply_theme(self, tokens):
        if self._name:
            self.setStyleSheet(f"""
                MoveField {{ background-color: {tokens['WHITE']};
                    border: 1px solid {tokens['BORDER']}; border-radius: 8px; }}
            """)
            self.label.setStyleSheet(f"background: transparent; color: {tokens['INK']};"
                                     f" font-family: {FONT_STACK}; font-size: 12px; font-weight: 600;")
            self.label.setText(td.move_display_name(self._name))
        else:
            self.setStyleSheet(f"""
                MoveField {{ background-color: {tokens['PALE_VIOLET']};
                    border: 1px dashed {tokens['VIOLET']}; border-radius: 8px; }}
            """)
            self.label.setStyleSheet(f"background: transparent; color: {tokens['VIOLET']};"
                                     f" font-family: {FONT_STACK}; font-size: 12px; font-weight: 600;")
            self.label.setText("＋ Move")

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class StatRow(QFrame):
    """A stat card row: label, effective number, bar, EV value. Click to edit."""
    clicked = Signal(str)

    def __init__(self, stat_key, label, parent=None):
        super().__init__(parent)
        self.stat_key = stat_key
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(30)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 0, 8, 0)
        lay.setSpacing(8)

        self.lbl = QLabel(label)
        self.lbl.setFixedWidth(34)
        lay.addWidget(self.lbl)

        self.num = QLabel("")
        self.num.setFixedWidth(40)
        lay.addWidget(self.num)

        self.bar_fill = QFrame()
        self.bar_fill.setFixedHeight(8)
        self.bar_fill.setMinimumWidth(6)
        lay.addWidget(self.bar_fill, 1)

        self.ev_lbl = QLabel("")
        self.ev_lbl.setFixedWidth(44)
        self.ev_lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
        lay.addWidget(self.ev_lbl)

        self._apply_theme(THEME.get_tokens())

    def _apply_theme(self, tokens):
        self.setStyleSheet(f"StatRow {{ background-color: {tokens['WHITE']}; border-radius: 8px; }}")
        self.lbl.setStyleSheet(f"color: {tokens['MUTED']}; font-size: 11px; font-weight: 700;"
                               f" font-family: {FONT_STACK}; background: transparent;")
        self.num.setStyleSheet(f"color: {tokens['INK']}; font-size: 13px; font-weight: 700;"
                               f" font-family: {FONT_STACK}; background: transparent;")
        self.bar_fill.setStyleSheet(f"background-color: {tokens['VIOLET']}; border-radius: 4px;")
        self.ev_lbl.setStyleSheet(f"color: {tokens['INK']}; font-size: 11px; font-weight: 600;"
                                  f" font-family: {FONT_STACK}; background: transparent;")

    def set_values(self, number, ev, bar_frac):
        self.num.setText(str(number) if number is not None else "—")
        self.ev_lbl.setText(f"{ev} EV" if ev else "—")
        self.bar_fill.setFixedWidth(int(max(6, min(160, bar_frac * 160))))

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.stat_key)
        super().mouseReleaseEvent(event)


# ---------------------------------------------------------------------------
# Roster slot card
# ---------------------------------------------------------------------------

class RosterSlot(QFrame):
    """A single 2x3 roster slot card: artwork (or a big +) and a bold name."""
    clicked = Signal(int)

    def __init__(self, index, parent=None):
        super().__init__(parent)
        self.index = index
        self.setMinimumSize(120, 95)
        self.setMaximumHeight(155)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(10, 10, 10, 8)
        self.layout.setSpacing(4)

        self.art_label = QLabel()
        self.art_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.art_label.setMinimumHeight(80)
        self.art_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.layout.addWidget(self.art_label, 1)

        self.name_label = QLabel("Add Pokemon")
        self.name_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.name_label.setWordWrap(True)
        self.layout.addWidget(self.name_label)

        self.set_slot(None)
        self._apply_theme(THEME.get_tokens())

    def _apply_theme(self, tokens):
        selected = self.property("selected") is True
        if selected:
            bg = f"background-color: {tokens['TRACK']}; border: none;"
        else:
            bg = f"background-color: {tokens['WHITE']}; border: 1px solid {tokens['BORDER']};"
        self.setStyleSheet(f"RosterSlot {{ {bg} border-radius: 12px; }}")
        self.name_label.setStyleSheet(
            f"color: {tokens['INK']}; font-family: {FONT_STACK}; font-size: 13px; font-weight: 700; background: transparent;")

    def set_slot(self, pid, label=""):
        toks = THEME.get_tokens()
        if pid is not None:
            path = td.art_path_of(pid)
            pm = QPixmap(str(path)) if path else QPixmap()
            if pm.isNull():
                self.art_label.setPixmap(lucide("plus", toks["MUTED"], 46))
            else:
                # Dynamically scale to fit current width, max height 100
                w = max(100, self.width() - 20)
                pm = pm.scaled(w, 100,
                               Qt.AspectRatioMode.KeepAspectRatio,
                               Qt.TransformationMode.SmoothTransformation)
                self.art_label.setPixmap(pm)
            self.name_label.setText(label or "Add Pokemon")
        else:
            self.art_label.setPixmap(lucide("plus", toks["MUTED"], 46))
            self.name_label.setText("Add Pokemon")

    def set_selected(self, selected):
        self.setProperty("selected", selected)
        self.style().unpolish(self)
        self.style().polish(self)
        self._apply_theme(THEME.get_tokens())

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.index)
        super().mousePressEvent(event)


# ---------------------------------------------------------------------------
# EV editor (the "little second screen", Showdown-style)
# ---------------------------------------------------------------------------

class _EvStatRow(QWidget):
    def _btn_style(self, tokens):
        return f"""
            QPushButton {{
                background-color: {tokens['PALE_VIOLET']};
                color: {tokens['VIOLET']};
                border: 1px solid {tokens['BORDER']};
                border-radius: 14px;
                font-size: 16px;
                font-weight: 700;
                font-family: {FONT_STACK};
            }}
            QPushButton:hover {{ background-color: {tokens['TRACK']}; }}
        """

    def __init__(self, stat_key, label, parent=None):
        super().__init__(parent)
        self.stat_key = stat_key
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 4, 0, 4)
        lay.setSpacing(8)

        self.lbl = QLabel(label)
        self.lbl.setFixedWidth(34)
        lay.addWidget(self.lbl)

        self.minus = QPushButton("−")
        self.minus.setFixedSize(28, 28)
        lay.addWidget(self.minus)

        self.ev_edit = QLineEdit()
        self.ev_edit.setFixedWidth(58)
        self.ev_edit.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.ev_edit)

        self.plus = QPushButton("+")
        self.plus.setFixedSize(28, 28)
        lay.addWidget(self.plus)

        self.result = QLabel("")
        self.result.setFixedWidth(70)
        self.result.setAlignment(Qt.AlignmentFlag.AlignRight)
        lay.addWidget(self.result)

        lay.addStretch(1)
        self._apply_theme(THEME.get_tokens())

    def _apply_theme(self, tokens):
        self.minus.setStyleSheet(self._btn_style(tokens))
        self.plus.setStyleSheet(self._btn_style(tokens))
        self.ev_edit.setStyleSheet(f"""
            QLineEdit {{
                background-color: {tokens['WHITE']};
                color: {tokens['INK']};
                border: 1px solid {tokens['BORDER']};
                border-radius: 8px;
                font-family: {FONT_STACK};
                font-size: 12px; font-weight: 700;
            }}
            QLineEdit:focus {{ border-color: {tokens['VIOLET']}; }}
        """)
        self.result.setStyleSheet(f"color: {tokens['INK']}; font-size: 13px; font-weight: 700;"
                                  f" font-family: {FONT_STACK}; background: transparent;")
        self.lbl.setStyleSheet(f"color: {tokens['MUTED']}; font-size: 11px; font-weight: 700;"
                               f" font-family: {FONT_STACK}; background: transparent;")


class EVsEditor(QWidget):
    """Showdown-flavoured EV + nature editor bound to one slot dict."""

    def __init__(self, tab, parent=None):
        super().__init__(parent)
        self.tab = tab
        self.slot = None
        self._listening = True
        lay = QVBoxLayout(self)
        lay.setContentsMargins(28, 20, 28, 20)
        lay.setSpacing(14)

        head = QHBoxLayout()
        self.back_btn = QPushButton("← Back")
        self.back_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.back_btn.setFixedHeight(32)
        self.back_btn.clicked.connect(self._go_back)
        head.addWidget(self.back_btn)
        self.title_lbl = QLabel("EVs & Nature")
        self.title_lbl.setStyleSheet(f"color: {INK}; font-family: {FONT_STACK}; font-size: 16px; font-weight: 800;")
        head.addWidget(self.title_lbl)
        head.addStretch(1)
        self.species_lbl = QLabel("")
        self.species_lbl.setStyleSheet(f"color: {MUTED}; font-family: {FONT_STACK}; font-size: 12px; font-weight: 600;")
        head.addWidget(self.species_lbl)
        lay.addLayout(head)

        self.total_lbl = QLabel("")
        self.total_lbl.setStyleSheet(f"color: {MUTED}; font-family: {FONT_STACK}; font-size: 11px; font-weight: 700;")
        lay.addWidget(self.total_lbl)

        rows_box = QWidget()
        rows_lay = QVBoxLayout(rows_box)
        rows_lay.setContentsMargins(0, 0, 0, 0)
        rows_lay.setSpacing(0)
        self.rows = {}
        for key in td.STATS:
            row = _EvStatRow(key, td.STAT_LABELS[key])
            row.minus.clicked.connect(lambda _=False, k=key: self._bump(k, -4))
            row.plus.clicked.connect(lambda _=False, k=key: self._bump(k, 4))
            row.ev_edit.editingFinished.connect(lambda k=key: self._commit_edit(k))
            rows_lay.addWidget(row)
            self.rows[key] = row
        lay.addWidget(rows_box)

        nat_row = QHBoxLayout()
        self.nat_lbl = QLabel("Nature")
        self.nat_lbl.setFixedWidth(50)
        self.nat_lbl.setStyleSheet(f"color: {MUTED}; font-size: 11px; font-weight: 700; font-family: {FONT_STACK};")
        nat_row.addWidget(self.nat_lbl)
        self.nature_combo = QComboBox()
        self.nature_combo.addItems(td.NATURE_NAMES)
        self.nature_combo.setFixedWidth(200)
        self.nature_combo.setFixedHeight(30)
        self.nature_combo.setStyleSheet(_combo_qss())
        self.nature_combo.currentTextChanged.connect(self._on_nature)
        nat_row.addWidget(self.nature_combo)
        nat_row.addStretch(1)
        lay.addLayout(nat_row)

        btns = QHBoxLayout()
        self.reset_btn = PillButton("Reset EVs", height=34)
        self.reset_btn.clicked.connect(self._reset_evs)
        btns.addWidget(self.reset_btn)
        self.max_btn = PillButton("Max a stat", height=34, bg=WHITE, fg=INK)
        self.max_btn.clicked.connect(self._max_stat)
        btns.addWidget(self.max_btn)
        btns.addStretch(1)
        lay.addLayout(btns)
        lay.addStretch(1)

        self._apply_theme(THEME.get_tokens())

    def _apply_theme(self, tokens):
        from fourslice.gui.imports_page import _pill_qss
        self.back_btn.setStyleSheet(f"""
            QPushButton {{ background-color: transparent; color: {tokens['MUTED']};
                border: none; font-family: {FONT_STACK}; font-size: 12px; font-weight: 700; text-align: left; }}
            QPushButton:hover {{ color: {tokens['INK']}; }}
        """)
        self.title_lbl.setStyleSheet(f"color: {tokens['INK']}; font-family: {FONT_STACK}; font-size: 16px; font-weight: 800;")
        self.species_lbl.setStyleSheet(f"color: {tokens['MUTED']}; font-family: {FONT_STACK}; font-size: 12px; font-weight: 600;")
        self.total_lbl.setStyleSheet(f"color: {tokens['MUTED']}; font-family: {FONT_STACK}; font-size: 11px; font-weight: 700;")
        self.nat_lbl.setStyleSheet(f"color: {tokens['MUTED']}; font-size: 11px; font-weight: 700; font-family: {FONT_STACK};")
        self.nature_combo.setStyleSheet(_combo_qss())
        self.reset_btn.setStyleSheet(_pill_qss("PillButton", tokens['INK'], tokens['WHITE'], 34))
        self.max_btn.setStyleSheet(_pill_qss("PillButton", tokens['WHITE'], tokens['INK'], 34))
        for row in self.rows.values():
            row._apply_theme(tokens)

    # -- control --
    def set_slot(self, slot, focus_stat=None):
        self.slot = slot
        self.species_lbl.setText(_slot_label(slot) or "")
        self._listening = False
        for key, row in self.rows.items():
            row.ev_edit.setText(str(slot["evs"].get(key, 0)))
        idx = td.NATURE_NAMES.index(str(slot.get("nature") or "Serious").title())
        self.nature_combo.blockSignals(True)
        self.nature_combo.setCurrentIndex(max(0, idx))
        self.nature_combo.blockSignals(False)
        self._listening = True
        self._refresh_values()
        if focus_stat in self.rows:
            self.rows[focus_stat].ev_edit.setFocus()
            self.rows[focus_stat].ev_edit.selectAll()

    def _refresh_values(self):
        if not self.slot:
            return
        stats = td.stat_values(self.slot)
        evs = self.slot["evs"]
        total = sum(evs.values())
        self.total_lbl.setText(f"Total EVs: {total} / 508 · max 252 per stat")
        for key, row in self.rows.items():
            row.result.setText(str(stats[key]))

    def _go_back(self):
        self.tab._close_ev_editor()

    def _bump(self, key, delta):
        if not self.slot:
            return
        evs = self.slot["evs"]
        cur = int(evs.get(key, 0))
        after = min(252, max(0, cur + delta))
        other = sum(v for k, v in evs.items() if k != key)
        if other + after > 508:
            return
        evs[key] = after
        self.rows[key].ev_edit.setText(str(after))
        self._commit_raw()

    def _commit_edit(self, key):
        if not self.slot:
            return
        try:
            val = max(0, min(252, int(self.rows[key].ev_edit.text())))
        except (ValueError, TypeError):
            val = int(self.slot["evs"].get(key, 0))
        evs = self.slot["evs"]
        other = sum(v for k, v in evs.items() if k != key)
        if other + val > 508:
            val = max(0, 508 - other)
        evs[key] = val
        self._commit_raw()

    def _commit_raw(self):
        self._refresh_values()
        self.tab._slot_edited()

    def _on_nature(self, text):
        if not self.slot or not text:
            return
        self.slot["nature"] = text
        self._refresh_values()
        self.tab._slot_edited()

    def _reset_evs(self):
        if not self.slot:
            return
        for k in td.STATS:
            self.slot["evs"][k] = 0
        for key, row in self.rows.items():
            row.ev_edit.setText("0")
        self._refresh_values()
        self.tab._slot_edited()

    def _max_stat(self):
        if not self.slot:
            return
        evs = self.slot["evs"]
        total = sum(evs.values())
        if total >= 508:
            self._refresh_values()
            return
        best = max(td.STATS, key=lambda k: (evs[k], td.slot_stats_number(self.slot, k)))
        room = 508 - total
        evs[best] = min(252, evs[best] + room)
        self.rows[best].ev_edit.setText(str(evs[best]))
        self._refresh_values()
        self.tab._slot_edited()


# ---------------------------------------------------------------------------
# Edit panel (State B)
# ---------------------------------------------------------------------------

class EditPanel(QFrame):
    """The detail/edit panel for one slot: Identity / Details / Moves / Stats."""

    def __init__(self, tab, parent=None):
        super().__init__(parent)
        self.tab = tab
        self.slot_index = 0
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(12)

        top = QHBoxLayout()
        back = QPushButton("← Back\nto team")
        back.setMinimumSize(80, 40)
        back.setCursor(Qt.CursorShape.PointingHandCursor)
        back.setStyleSheet(f"QPushButton {{ background-color: transparent; color: {MUTED}; border: none;"
                           f" font-family: {FONT_STACK}; font-size: 12px; font-weight: 700; text-align: left; }}")
        back.clicked.connect(lambda: self.tab._show_browse())
        top.addWidget(back)

        self.poke_tab = PillButton("Pokémon", height=32, bg=WHITE, fg=INK)
        self.move_tab = PillButton("Moves", height=32, bg=WHITE, fg=INK)
        self.poke_tab.setMinimumWidth(80)
        self.move_tab.setMinimumWidth(80)
        self.poke_tab.clicked.connect(lambda: self.tab._open_pokemon_picker(self.slot_index))
        self.move_tab.clicked.connect(lambda: self.tab._open_move_picker(self.slot_index, 0))
        top.addWidget(self.poke_tab)
        top.addWidget(self.move_tab)
        top.addStretch(1)
        outer.addLayout(top)

        zones = QHBoxLayout()
        zones.setSpacing(16)
        zones.setContentsMargins(24, 24, 24, 24)
        outer.addLayout(zones)

        zones.addWidget(self._build_identity(), 1)
        zones.addWidget(self._build_details(), 1)
        zones.addWidget(self._build_moves(), 1)
        zones.addWidget(self._build_stats(), 1)

        meta = QHBoxLayout()
        meta.setSpacing(16)
        meta.setContentsMargins(24, 0, 24, 24)
        outer.addLayout(meta)

        # Two columns (Counters/Abilities/Items left, Teammates/Moves right).
        # Items has no fixed height and stretches, so the left column absorbs
        # exactly the leftover below Counters + Abilities and both columns end
        # on one line -- no magic pixel arithmetic (see ABILITIES_H/ITEMS_MIN_H).
        left_col = QVBoxLayout()
        left_col.setSpacing(12)
        left_col.addWidget(self._build_counters_square())
        left_col.addWidget(self._build_abilities_square())
        left_col.addWidget(self._build_items_square(), 1)
        meta.addLayout(left_col, 1)

        right_col = QVBoxLayout()
        right_col.setSpacing(12)
        right_col.addWidget(self._build_teammates_square())
        right_col.addWidget(self._build_moves_square())
        meta.addLayout(right_col, 1)

        self._apply_theme(THEME.get_tokens())

    # -- zone builders --
    def _build_identity(self):
        self.identity_card = QFrame()
        self.identity_card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        lay = QVBoxLayout(self.identity_card)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(10)

        self.portrait = QLabel()
        self.portrait.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.portrait.setFixedHeight(108)
        self.portrait.setStyleSheet(f"background-color: {PALE_VIOLET}; border-radius: 10px;")
        lay.addWidget(self.portrait)

        species_row = QVBoxLayout()
        species_row.setSpacing(3)
        species_row.addWidget(_kv_label("Pokémon"))
        self.species_field = ClickableLabel("Choose a Pokémon…")
        self.species_field.setStyleSheet(f"color: {MUTED}; font-family: {FONT_STACK}; font-size: 14px; font-weight: 700;")
        self.species_field.clicked.connect(lambda: self.tab._open_pokemon_picker(self.slot_index))
        species_row.addWidget(self.species_field)
        lay.addLayout(species_row)

        nick_row = QVBoxLayout()
        nick_row.setSpacing(3)
        nick_row.addWidget(_kv_label("Nickname"))
        self.nickname_edit = QLineEdit()
        self.nickname_edit.setFixedHeight(30)
        self.nickname_edit.setStyleSheet(_input_qss())
        self.nickname_edit.textChanged.connect(self._on_nickname)
        nick_row.addWidget(self.nickname_edit)
        lay.addLayout(nick_row)

        lay.addStretch(1)
        self.identity_card.setStyleSheet(_card_qss())
        return self.identity_card

    def _build_details(self):
        self.details_card = QFrame()
        self.details_card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        lay = QVBoxLayout(self.details_card)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(10)

        row1 = QHBoxLayout()
        row1.setSpacing(10)

        lv_box = QVBoxLayout(); lv_box.setSpacing(3)
        lv_box.addWidget(_kv_label("Level"))
        self.level_edit = QLineEdit()
        self.level_edit.setMinimumWidth(40)
        self.level_edit.setFixedHeight(30)
        self.level_edit.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.level_edit.setStyleSheet(_input_qss())
        self.level_edit.textChanged.connect(self._on_level)
        lv_box.addWidget(self.level_edit)
        row1.addLayout(lv_box)

        gd_box = QVBoxLayout(); gd_box.setSpacing(3)
        gd_box.addWidget(_kv_label("Gender"))
        self.gender_edit = QLineEdit("-")
        self.gender_edit.setMinimumWidth(35)
        self.gender_edit.setFixedHeight(30)
        self.gender_edit.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.gender_edit.setStyleSheet(_input_qss())
        self.gender_edit.textChanged.connect(self._on_gender)
        gd_box.addWidget(self.gender_edit)
        row1.addLayout(gd_box)

        sh_box = QVBoxLayout(); sh_box.setSpacing(3)
        sh_box.addWidget(_kv_label("Shiny"))
        self.shiny_combo = QComboBox()
        self.shiny_combo.addItems(["No", "Yes"])
        self.shiny_combo.setMinimumWidth(50)
        self.shiny_combo.setFixedHeight(30)
        self.shiny_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.shiny_combo.setStyleSheet(_combo_qss())
        self.shiny_combo.currentTextChanged.connect(self._on_shiny)
        sh_box.addWidget(self.shiny_combo)
        row1.addLayout(sh_box)
        row1.addStretch(1)
        lay.addLayout(row1)

        ter_row = QVBoxLayout(); ter_row.setSpacing(3)
        ter_row.addWidget(_kv_label("Tera Type"))
        self.tera_combo = QComboBox()
        self.tera_combo.addItems(["-"] + sorted(t.capitalize() for t in TYPE_COLORS))
        self.tera_combo.setMinimumWidth(100)
        self.tera_combo.setFixedHeight(30)
        self.tera_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.tera_combo.setStyleSheet(_combo_qss())
        self.tera_combo.currentTextChanged.connect(self._on_tera)
        ter_row.addWidget(self.tera_combo)
        lay.addLayout(ter_row)

        ab_row = QVBoxLayout(); ab_row.setSpacing(3)
        ab_row.addWidget(_kv_label("Ability"))
        self.ability_combo = QComboBox()
        self.ability_combo.setFixedHeight(30)
        self.ability_combo.setStyleSheet(_combo_qss())
        self.ability_combo.currentTextChanged.connect(self._on_ability)
        ab_row.addWidget(self.ability_combo)
        lay.addLayout(ab_row)

        it_row = QVBoxLayout(); it_row.setSpacing(3)
        it_row.addWidget(_kv_label("Item"))
        self.item_field = ClickableLabel("—")
        self.item_field.setStyleSheet(f"color: {MUTED}; font-family: {FONT_STACK}; font-size: 12px; font-weight: 700;")
        self.item_field.clicked.connect(lambda: self.tab._open_item_picker(self.slot_index))
        it_row.addWidget(self.item_field)
        lay.addLayout(it_row)

        self.types_row = QHBoxLayout()
        self.types_row.setSpacing(8)
        self.type_chip_labels = []
        lay.addLayout(self.types_row)
        lay.addStretch(1)
        self.details_card.setStyleSheet(_card_qss())
        return self.details_card

    def _build_moves(self):
        self.moves_card = QFrame()
        self.moves_card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        lay = QVBoxLayout(self.moves_card)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(8)
        lay.addWidget(_kv_label("Moves"))
        self.move_fields = []
        for i in range(4):
            field = MoveField()
            field.clicked.connect(lambda _=False, idx=i: self.tab._open_move_picker(self.slot_index, idx))
            lay.addWidget(field)
            self.move_fields.append(field)
        lay.addStretch(1)
        self.moves_card.setStyleSheet(_card_qss())
        return self.moves_card

    def _build_stats(self):
        self.stats_card = QFrame()
        self.stats_card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.stats_card.setStyleSheet(f"QFrame {{ background-color: {PALE_VIOLET}; border-radius: 12px; }}")
        lay = QVBoxLayout(self.stats_card)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(6)

        hdr = QHBoxLayout()
        hdr.addWidget(_kv_label("Stats"))
        hdr.addStretch(1)
        hdr.addWidget(_kv_label("EV"))
        lay.addLayout(hdr)

        self.stat_rows = []
        for key in td.STATS:
            row = StatRow(key, td.STAT_LABELS[key])
            row.clicked.connect(self.tab.open_ev_editor_for)
            lay.addWidget(row)
            self.stat_rows.append(row)

        self.nature_row = StatRow("nature", "Nat")
        self.nature_row.num.setFixedWidth(72)
        self.nature_row.clicked.connect(self.tab.open_ev_editor_for)
        lay.addWidget(self.nature_row)

        self.level_note = QLabel("")
        self.level_note.setStyleSheet(f"color: {MUTED}; font-family: {FONT_STACK}; font-size: 10px; font-weight: 600;")
        lay.addWidget(self.level_note)
        lay.addStretch(1)
        return self.stats_card

    def _build_list_square(self, title, height=240, min_height=None):
        """A titled card with a scrollable list body (used for the five
        meta squares below the four zone cards). `height` fixes the card's
        height (omit for a stretch card, e.g. Items); `min_height` floors
        a stretch card so it can't collapse to nothing."""
        return build_list_square(title, height, min_height)

    def _build_counters_square(self):
        self.counters_card, self.counters_list = self._build_list_square("Counters")
        return self.counters_card

    def _build_teammates_square(self):
        self.teammates_card, self.teammates_list = self._build_list_square("Teammates")
        return self.teammates_card

    def _build_moves_square(self):
        self.moves_square_card, self.moves_list = self._build_list_square("Moves")
        return self.moves_square_card

    def _build_abilities_square(self):
        self.abilities_card, self.abilities_list = self._build_list_square(
            "Abilities", height=ABILITIES_H)
        return self.abilities_card

    def _build_items_square(self):
        self.items_card, self.items_list = self._build_list_square(
            "Items", height=None, min_height=ITEMS_MIN_H)
        return self.items_card

    def _meta_row(self, name, value, compact=False):
        """One name / value line inside a meta square (counters / teammates /
        moves / abilities / items). Compact rows use tighter margins and a
        smaller font so three of them fit the fixed-height abilities box."""
        return meta_row(name, value, compact)

    def _refresh_meta_squares(self, slot):
        smogon_id = td.match_stats_format(self.tab._fmt.get("name"))
        species = slot.get("species")

        fmt_pct = lambda v: f"{float(v):.1f}%"
        fmt_score = lambda v: f"{float(v):g}"
        # lay, fetch, sort-key, value-formatter, row cap, compact rows,
        # skip the aggregated "Other" entry (the moveset source files append one)
        specs = (
            (self.counters_list, td.checks_counters_for, "score", fmt_score, None, False, False),
            (self.teammates_list, td.teammates_for, "pct", fmt_pct, None, False, False),
            (self.moves_list, td.moves_for, "pct", fmt_pct, None, False, True),
            (self.abilities_list, td.abilities_for, "pct", fmt_pct, 3, True, True),
            (self.items_list, td.items_for, "pct", fmt_pct, None, True, True),
        )

        for lay, fetch, key, fmt, cap, compact, skip_other in specs:
            while lay.count():
                it = lay.takeAt(0)
                if it.widget():
                    it.widget().deleteLater()
            if not species or not smogon_id:
                continue
            name = td._showdown_poke_name(species)
            entries = [r for r in fetch(name, smogon_id, "0")
                       if r.get("name") and not (skip_other and r["name"] == "Other")]
            entries.sort(key=lambda r: float(r.get(key, 0) or 0), reverse=True)
            if cap:
                entries = entries[:cap]
            for e in entries:
                lay.addWidget(self._meta_row(e["name"], fmt(e.get(key, 0)), compact=compact))
            lay.addStretch(1)

    # -- data binding --
    def set_slot_index(self, index):
        self.slot_index = index
        self._refresh()

    def _refresh(self):
        slot = self.tab.team["slots"][self.slot_index]
        self._refresh_identity(slot)
        self._refresh_details(slot)
        self._refresh_moves(slot)
        self._refresh_stats(slot)
        self._refresh_meta_squares(slot)

    def _refresh_identity(self, slot):
        if slot.get("pid"):
            path = td.art_path_of(slot["pid"])
            pm = QPixmap(str(path)) if path else QPixmap()
            if pm.isNull():
                self.portrait.clear()
            else:
                pm = pm.scaled(120, 100, Qt.AspectRatioMode.KeepAspectRatio,
                               Qt.TransformationMode.SmoothTransformation)
                self.portrait.setPixmap(pm)
            self.species_field.setText(td._showdown_poke_name(slot["species"]))
            self.species_field.setStyleSheet(f"color: {THEME.get_tokens()['INK']}; font-family: {FONT_STACK}; font-size: 14px; font-weight: 700;")
        else:
            self.portrait.clear()
            self.species_field.setText("Choose a Pokémon…")
            self.species_field.setStyleSheet(f"color: {THEME.get_tokens()['MUTED']}; font-family: {FONT_STACK}; font-size: 14px; font-weight: 700;")

        self.level_edit.blockSignals(True)
        self.level_edit.setText(str(slot.get("level") or 100))
        self.level_edit.blockSignals(False)

        self.nickname_edit.blockSignals(True)
        self.nickname_edit.setText(slot.get("nickname") or "")
        self.nickname_edit.blockSignals(False)

        self.gender_edit.blockSignals(True)
        self.gender_edit.setText(slot.get("gender") or "-")
        self.gender_edit.blockSignals(False)

        self.shiny_combo.blockSignals(True)
        self.shiny_combo.setCurrentIndex(1 if slot.get("shiny") else 0)
        self.shiny_combo.blockSignals(False)

        self.tera_combo.blockSignals(True)
        tera = str(slot.get("tera") or "-")
        idx = self.tera_combo.findText(tera.capitalize())
        self.tera_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.tera_combo.blockSignals(False)

    def _refresh_details(self, slot):
        self.ability_combo.blockSignals(True)
        self.ability_combo.clear()
        if slot.get("species"):
            mon = td.catalog_by_slug().get(td.slugify(slot["species"])) or {}
            ids = td.ordered_abilities_for(mon, self.tab._fmt)
            labels = [td.ability_display_name(a) for a in ids]
            self.ability_combo.addItems(labels)
            cur = slot.get("ability") or ""
            if cur in ids:
                idx = ids.index(cur)
            else:
                slot["ability"] = ids[0] if ids else ""
                idx = 0
            self.ability_combo.setCurrentIndex(max(0, idx))
        else:
            self.ability_combo.addItem("—")
        self.ability_combo.blockSignals(False)

        while self.types_row.count():
            it = self.types_row.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        self.type_chip_labels = []
        if slot.get("species"):
            mon = td.catalog_by_slug().get(td.slugify(slot["species"])) or {}
            for t in (mon.get("types") or [])[:2]:
                chip = make_type_chip(t.upper(), TYPE_COLORS.get(t.lower(), THEME.get_tokens()['VIOLET']))
                self.types_row.addWidget(chip)
                self.type_chip_labels.append(chip)
        self.types_row.addStretch(1)

        item_id = slot.get("item") or ""
        if item_id:
            self.item_field.setText(td.item_display_name(item_id))
            self.item_field.setStyleSheet(f"color: {THEME.get_tokens()['INK']}; font-family: {FONT_STACK}; font-size: 12px; font-weight: 700;")
        else:
            self.item_field.setText("—")
            self.item_field.setStyleSheet(f"color: {THEME.get_tokens()['MUTED']}; font-family: {FONT_STACK}; font-size: 12px; font-weight: 700;")

    def _refresh_moves(self, slot):
        for i, field in enumerate(self.move_fields):
            mid = slot["moves"][i] if i < len(slot["moves"]) else ""
            field.set_move(mid)

    def _refresh_stats(self, slot):
        stats = td.stat_values(slot)
        evs = slot.get("evs", {})
        for row in self.stat_rows:
            base = _base_of(slot, row.stat_key)
            row.set_values(stats[row.stat_key], evs.get(row.stat_key, 0), base / 255.0 if base else 0)
        nature = str(slot.get("nature") or "Serious")
        self.nature_row.set_values(str(nature).capitalize(), 0, 0)
        lvl = slot.get("level") or 100
        self.level_note.setText(f"Level {lvl} · click any row to open the EV editor")

    # -- field handlers --
    def _on_nickname(self, text):
        self.tab.team["slots"][self.slot_index]["nickname"] = text
        self.tab._slot_edited(roster_only=True)

    def _on_level(self, text):
        slot = self.tab.team["slots"][self.slot_index]
        try:
            slot["level"] = max(1, min(100, int(text)))
        except (ValueError, TypeError):
            return
        self._refresh_stats(slot)
        self.tab._slot_edited()

    def _on_gender(self, text):
        slot = self.tab.team["slots"][self.slot_index]
        text = text.strip()
        if text.upper() in ("M", "F"):
            slot["gender"] = text.upper()
        elif text in ("-", ""):
            slot["gender"] = "-"
        else:
            slot["gender"] = text
        self.tab._slot_edited(roster_only=True)

    def _on_shiny(self, text):
        self.tab.team["slots"][self.slot_index]["shiny"] = text == "Yes"
        self.tab._slot_edited(roster_only=True)

    def _on_tera(self, text):
        self.tab.team["slots"][self.slot_index]["tera"] = text
        self.tab._slot_edited()

    def _on_ability(self, text):
        slot = self.tab.team["slots"][self.slot_index]
        if not slot.get("species"):
            return
        ids = td.ordered_abilities_for(
            td.catalog_by_slug().get(td.slugify(slot["species"])) or {}, self.tab._fmt)
        for a in ids:
            if td.ability_display_name(a) == text:
                slot["ability"] = a
                break
        self.tab._slot_edited()

    def _apply_theme(self, tokens):
        self.setStyleSheet(f"EditPanel {{ background-color: {tokens['WHITE']}; border-radius: 12px; }}")
        self.portrait.setStyleSheet(f"background-color: {tokens['PALE_VIOLET']}; border-radius: 10px;")
        # The four zone cards are built once at init, so they need re-styling
        # whenever the theme changes, or they keep their old (light) colors.
        for attr in ("identity_card", "details_card", "moves_card"):
            card = getattr(self, attr, None)
            if card is not None:
                card.setStyleSheet(_card_qss())
        if getattr(self, "stats_card", None) is not None:
            self.stats_card.setStyleSheet(
                f"QFrame {{ background-color: {tokens['PALE_VIOLET']}; border-radius: 12px; }}")
        for attr in ("counters_card", "teammates_card", "moves_square_card",
                 "abilities_card", "items_card"):
            card = getattr(self, attr, None)
            if card is not None:
                card.setStyleSheet(
                    f"QFrame {{ background-color: {tokens['PALE_VIOLET']}; border-radius: 12px; }}")
        for row in self.stat_rows:
            row._apply_theme(tokens)
        self.nature_row._apply_theme(tokens)
        # Theme-sensitive field text (dark INK text is unreadable in dark mode).
        if self.tab.team.get("slots"):
            slot = self.tab.team["slots"][self.slot_index]
            if getattr(self, "species_field", None) is not None:
                if slot.get("pid"):
                    self.species_field.setStyleSheet(
                        f"color: {tokens['INK']}; font-family: {FONT_STACK}; font-size: 14px; font-weight: 700;")
                else:
                    self.species_field.setStyleSheet(
                        f"color: {tokens['MUTED']}; font-family: {FONT_STACK}; font-size: 14px; font-weight: 700;")
            if getattr(self, "item_field", None) is not None:
                if slot.get("item"):
                    self.item_field.setStyleSheet(
                        f"color: {tokens['INK']}; font-family: {FONT_STACK}; font-size: 12px; font-weight: 700;")
                else:
                    self.item_field.setStyleSheet(
                        f"color: {tokens['MUTED']}; font-family: {FONT_STACK}; font-size: 12px; font-weight: 700;")
            # Re-render the counters/teammates rows so their text colors match
            # the active theme (they pick tokens up at build time otherwise).
            if getattr(self, "counters_list", None) is not None:
                self._refresh_meta_squares(slot)


def _base_of(slot, stat_key):
    if not slot.get("species"):
        return 0
    mon = td.catalog_by_slug().get(td.slugify(slot["species"])) or {}
    return int((mon.get("stats") or {}).get(stat_key, 0))


# ---------------------------------------------------------------------------
# TeamsTab
# ---------------------------------------------------------------------------

class TeamsTab(QWidget):
    def __init__(self, conn):
        super().__init__()
        self.conn = conn
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        self.team = td.empty_team("New Team", DEFAULT_FORMAT)
        self._team_id = None
        self._dirty = False
        self._fmt = td.find_format(DEFAULT_FORMAT) or {"name": DEFAULT_FORMAT, "mod": None, "ruleset": [], "banlist": []}

        self._catalog = td.load_pokemon_catalog()
        self._species_base = []
        self._moves_base = []
        self._items_base = []
        self._move_to_species = None
        self._visible_species = []
        self._visible_moves = []
        self._visible_items = []

        self._browse_mode = POKEMON_MODE
        self._search_text = ""
        self._type_filter = None
        self._move_filter = None
        self._ability_filter = None
        self._active_slot_index = 0
        self._pending_move_idx = 0

        self._rebuild_timer = QTimer(self)
        self._rebuild_timer.setSingleShot(True)
        self._rebuild_timer.setInterval(160)
        self._rebuild_timer.timeout.connect(self._rebuild_current_list)
        self._counters_timer = QTimer(self)
        self._counters_timer.setSingleShot(True)
        self._counters_timer.setInterval(400)
        self._counters_timer.timeout.connect(self._refresh_counters)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setStyleSheet("background: transparent;")
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        self.container = QWidget()
        self.container.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.layout = QVBoxLayout(self.container)
        self.layout.setContentsMargins(32, 28, 32, 28)
        self.layout.setSpacing(20)

        self.scroll.setWidget(self.container)
        main_layout.addWidget(self.scroll)

        self._build_header()
        self._build_roster()
        self._build_counters()
        self._build_content_stack()

        self._reload_team_combo()

        self._show_browse()
        # Start the picker on the real, usage-ordered roster for the
        # default format instead of the raw alphabetical pokedex -- the
        # list should be correct from the first frame, not only after the
        # user interacts with the format selector.
        self._reset_and_render_pokemon_list()

        THEME.theme_changed.connect(self._apply_theme)
        self._apply_theme()

    # ------------------------------------------------------------------
    # Header
    # ------------------------------------------------------------------
    def _build_header(self):
        header_layout = QHBoxLayout()
        header_layout.setSpacing(12)

        self.team_name_card = QFrame()
        tn_lay = QVBoxLayout(self.team_name_card)
        tn_lay.setContentsMargins(12, 8, 12, 8)
        tn_lay.setSpacing(2)
        self.tn_title = QLabel("Team Name")
        self.tn_value = QLineEdit(self.team["name"])
        self.tn_value.setMinimumWidth(100)
        self.tn_value.setFixedHeight(30)
        self.tn_value.setStyleSheet(_input_qss())
        self.tn_value.textChanged.connect(self._on_team_name)
        tn_lay.addWidget(self.tn_title)
        tn_lay.addWidget(self.tn_value, 1)
        header_layout.addWidget(self.team_name_card)

        fmt_box = QVBoxLayout(); fmt_box.setSpacing(0)
        self.fmt_disclaimer = QLabel("Formats and information may be out of date")
        self.fmt_disclaimer.setWordWrap(True)
        self.fmt_disclaimer.setContentsMargins(2, 0, 0, 2)
        self.fmt_disclaimer.setStyleSheet(f"color: {MUTED}; font-size: 10px; font-style: italic;"
                                          f" font-family: {FONT_STACK};")
        self.fmt_lbl = QLabel("Format")
        self.fmt_lbl.setStyleSheet(f"color: {MUTED}; font-size: 11px; font-weight: 700;"
                                   f" font-family: {FONT_STACK}; padding-left: 2px;")
        self.format_combo = QComboBox()
        self.format_combo.setMinimumWidth(150)
        self.format_combo.setFixedHeight(30)
        self.format_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.format_combo.setEditable(True)
        self.format_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        completer = self.format_combo.completer()
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self.format_combo.lineEdit().setStyleSheet(_input_qss())
        self.format_combo.setStyleSheet(_combo_qss())
        self._populate_formats()
        self.format_combo.textActivated.connect(self._on_format_changed)
        fmt_box.addWidget(self.fmt_disclaimer)
        fmt_box.addWidget(self.fmt_lbl)
        fmt_box.addWidget(self.format_combo)
        header_layout.addLayout(fmt_box)

        saved_box = QVBoxLayout(); saved_box.setSpacing(0)
        saved_lbl = QLabel("My Teams")
        saved_lbl.setStyleSheet(f"color: {MUTED}; font-size: 11px; font-weight: 700;"
                                f" font-family: {FONT_STACK}; padding-left: 2px;")
        self.team_combo = QComboBox()
        self.team_combo.setMinimumWidth(120)
        self.team_combo.setFixedHeight(30)
        self.team_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.team_combo.setStyleSheet(_combo_qss())
        self.team_combo.activated.connect(self._on_team_combo)
        saved_box.addWidget(saved_lbl)
        saved_box.addWidget(self.team_combo)
        header_layout.addLayout(saved_box)

        self.new_btn = PillButton("New", height=36, bg=WHITE, fg=INK)
        self.new_btn.clicked.connect(self._new_team)
        header_layout.addWidget(self.new_btn)

        self.save_btn = PillButton("Save", height=36, bg=WHITE, fg=INK)
        self.save_btn.clicked.connect(self._save_team)
        header_layout.addWidget(self.save_btn)

        self.delete_btn = PillButton("Delete", height=36, bg=WHITE, fg=INK)
        self.delete_btn.clicked.connect(self._delete_team)
        self.delete_btn.setVisible(False)
        header_layout.addWidget(self.delete_btn)

        header_layout.addStretch(1)

        self.export_btn = QPushButton("Export\nShowdown")
        self.export_btn.setMinimumSize(80, 40)
        self.export_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.export_btn.clicked.connect(self._on_export)
        self.export_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {WHITE};
                color: {INK};
                border: 1px solid {BORDER};
                border-radius: 12px;
                font-family: {FONT_STACK};
                font-size: 12px;
                font-weight: 700;
            }}
            QPushButton:hover {{ border-color: {VIOLET}; color: {VIOLET}; }}
        """)
        header_layout.addWidget(self.export_btn)

        self.toast = QLabel("")
        self.toast.setStyleSheet(f"color: {VIOLET}; font-family: {FONT_STACK}; font-size: 11px; font-weight: 700;")
        header_layout.addWidget(self.toast)

        self.layout.addLayout(header_layout)

    def _populate_formats(self):
        # Only offer formats that actually have usage data in stats.db so
        # the list isn't cluttered with dead formats; fall back to the full
        # buildable set if stats.db has nothing usable. Order by games
        # played (most -> least), like the sidebar, instead of formats.js
        # file order.
        formats = td.get_stats_backed_formats() or td.get_buildable_formats()
        formats = td.order_formats_by_usage(formats)
        self.format_combo.blockSignals(True)
        self.format_combo.clear()
        self._format_items = []
        for fmt in formats:
            name = fmt.get("name", "")
            if name:
                self._format_items.append(name)
                self.format_combo.addItem(name)
        idx = self._format_items.index(DEFAULT_FORMAT) if DEFAULT_FORMAT in self._format_items else 0
        self.format_combo.setCurrentIndex(max(0, idx))
        self.format_combo.blockSignals(False)

    def _sync_format_edit_text(self, name):
        """Keep the editable format combo's text on the last valid format."""
        if not name:
            return
        self.format_combo.blockSignals(True)
        idx = self._format_items.index(name) if name in self._format_items else 0
        self.format_combo.setCurrentIndex(max(0, idx))
        self.format_combo.setEditText(name)
        self.format_combo.blockSignals(False)

    # ------------------------------------------------------------------
    # Roster + counters
    # ------------------------------------------------------------------
    def _build_roster(self):
        self.roster_grid = QGridLayout()
        self.roster_grid.setSpacing(16)
        self.slots = []
        for i in range(6):
            slot = RosterSlot(i)
            self.slots.append(slot)
            self.roster_grid.addWidget(slot, i // 3, i % 3)
            slot.clicked.connect(self._on_slot_clicked)
        self.layout.addLayout(self.roster_grid)

    def _build_counters(self):
        box = QHBoxLayout()
        self.counters_title = QLabel("Top threats to your team:")
        self.counters_title.setStyleSheet(f"color: {MUTED}; font-family: {FONT_STACK};"
                                          f" font-size: 11px; font-weight: 700;")
        box.addWidget(self.counters_title)
        self.counters_chips_lay = QHBoxLayout()
        self.counters_chips_lay.setSpacing(8)
        box.addLayout(self.counters_chips_lay)
        box.addStretch(1)
        self.layout.addLayout(box)

    # ------------------------------------------------------------------
    # Content stack
    # ------------------------------------------------------------------
    def _build_content_stack(self):
        self.content_stack = QStackedWidget()
        # No stretch factor (stretch=0) so the content stack reports its true
        # size hint. With setWidgetResizable(True) on the scroll area, this
        # allows the scroll area to:
        #   - Expand the widget to fill the viewport when content is small
        #   - Show scrollbars when content exceeds the viewport
        self.layout.addWidget(self.content_stack)

        self.browse_view = self._build_browse_view()
        self.content_stack.addWidget(self.browse_view)
        self.edit_panel = EditPanel(self)
        self.content_stack.addWidget(self.edit_panel)
        self.ev_editor = EVsEditor(self)
        self.content_stack.addWidget(self.ev_editor)

    def _build_browse_view(self):
        view = QWidget()
        lay = QVBoxLayout(view)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        mode_row = QHBoxLayout()
        mode_row.setSpacing(8)
        self.mode_tabs = {}
        for mode, label in ((POKEMON_MODE, "Pokémon"), (MOVE_MODE, "Moves"), (ITEM_MODE, "Items")):
            btn = PillButton(label, height=30, bg=WHITE, fg=INK)
            btn.clicked.connect(lambda _=False, m=mode: self._set_browse_mode(m))
            mode_row.addWidget(btn)
            self.mode_tabs[mode] = btn
        mode_row.addStretch(1)
        lay.addLayout(mode_row)

        self.search_bar = QLineEdit()
        self.search_bar.setFixedHeight(46)
        self.search_bar.setPlaceholderText("Search Pokémon / Move / Ability / Type")
        self.search_bar.textChanged.connect(self._on_search_edited)
        lay.addWidget(self.search_bar)

        self.chips_scroll = QScrollArea()
        self.chips_scroll.setWidgetResizable(True)
        self.chips_scroll.setFixedHeight(34)
        self.chips_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.chips_scroll.setStyleSheet("background: transparent;")
        self.chips_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.chips_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.chips_host = QWidget()
        self.chips_host.setStyleSheet("background: transparent;")
        self.chips_lay = QHBoxLayout(self.chips_host)
        self.chips_lay.setContentsMargins(2, 0, 2, 0)
        self.chips_lay.setSpacing(8)
        self.chips_scroll.setWidget(self.chips_host)
        lay.addWidget(self.chips_scroll)
        self.chips_scroll.hide()

        self.list_stack = QStackedWidget()
        lay.addWidget(self.list_stack, 1)

        self._build_pokemon_list_page()
        self._build_move_list_page()
        self._build_item_list_page()
        return view

    def _build_pokemon_list_page(self):
        self.pokedex_panel = QFrame()
        self.pokedex_panel.setMinimumHeight(360)
        poke_lay = QVBoxLayout(self.pokedex_panel)
        poke_lay.setContentsMargins(16, 16, 16, 16)
        poke_lay.setSpacing(8)

        self.pokedex_scroll = QScrollArea()
        self.pokedex_scroll.setWidgetResizable(True)
        self.pokedex_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.pokedex_scroll.setStyleSheet("background: transparent;")
        self.pokedex_scroll.verticalScrollBar().valueChanged.connect(self._on_pokedex_scroll)

        self.pokedex_grid_container = QWidget()
        self.pokedex_grid_container.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.pokedex_grid = QGridLayout(self.pokedex_grid_container)
        self.pokedex_grid.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.pokedex_grid.setContentsMargins(0, 0, 0, 0)
        self.pokedex_grid.setHorizontalSpacing(16)
        self.pokedex_grid.setVerticalSpacing(10)
        self.pokedex_grid.setColumnStretch(0, 2)
        self.pokedex_grid.setColumnStretch(1, 2)
        self.pokedex_grid.setColumnStretch(2, 3)
        for col in range(3, 10):
            self.pokedex_grid.setColumnStretch(col, 1)

        self.pokedex_headers = []
        headers_text = ["Name", "Types", "Abilities", "HP", "ATK", "DEF", "SPA", "SPD", "SPE", "BST"]
        for col, text in enumerate(headers_text):
            lbl = QLabel(text)
            if col >= 3:
                lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            else:
                lbl.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            self.pokedex_headers.append(lbl)
            self.pokedex_grid.addWidget(lbl, 0, col)

        self.pokedex_scroll.setWidget(self.pokedex_grid_container)
        poke_lay.addWidget(self.pokedex_scroll)
        self.list_stack.addWidget(self.pokedex_panel)

    def _build_move_list_page(self):
        self.move_panel = QFrame()
        self.move_panel.setMinimumHeight(360)
        move_lay = QVBoxLayout(self.move_panel)
        move_lay.setContentsMargins(16, 16, 16, 16)
        move_lay.setSpacing(8)

        self.move_scroll = QScrollArea()
        self.move_scroll.setWidgetResizable(True)
        self.move_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.move_scroll.setStyleSheet("background: transparent;")
        self.move_scroll.verticalScrollBar().valueChanged.connect(self._on_move_scroll)

        self.move_grid_container = QWidget()
        self.move_grid_container.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.move_grid = QGridLayout(self.move_grid_container)
        self.move_grid.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.move_grid.setContentsMargins(0, 0, 0, 0)
        self.move_grid.setHorizontalSpacing(20)
        self.move_grid.setVerticalSpacing(8)
        self.move_grid.setColumnStretch(0, 1)
        self.move_grid.setColumnStretch(1, 0)
        self.move_grid.setColumnStretch(2, 0)
        self.move_grid.setColumnStretch(3, 4)

        self.move_headers = []
        for col, text in enumerate(["Move", "Type", "Cat", "Description"]):
            lbl = QLabel(text)
            lbl.setStyleSheet(f"color: {MUTED}; font-size: 11px; font-weight: 700;"
                              f" font-family: {FONT_STACK};")
            self.move_headers.append(lbl)
            self.move_grid.addWidget(lbl, 0, col)

        self.move_scroll.setWidget(self.move_grid_container)
        move_lay.addWidget(self.move_scroll)
        self.list_stack.addWidget(self.move_panel)

    def _build_item_list_page(self):
        self.item_panel = QFrame()
        self.item_panel.setMinimumHeight(360)
        item_lay = QVBoxLayout(self.item_panel)
        item_lay.setContentsMargins(16, 16, 16, 16)
        item_lay.setSpacing(8)

        self.item_scroll = QScrollArea()
        self.item_scroll.setWidgetResizable(True)
        self.item_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.item_scroll.setStyleSheet("background: transparent;")
        self.item_scroll.verticalScrollBar().valueChanged.connect(self._on_item_scroll)

        self.item_grid_container = QWidget()
        self.item_grid_container.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.item_grid = QGridLayout(self.item_grid_container)
        self.item_grid.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.item_grid.setContentsMargins(0, 0, 0, 0)
        self.item_grid.setHorizontalSpacing(20)
        self.item_grid.setVerticalSpacing(8)
        self.item_grid.setColumnStretch(0, 0)
        self.item_grid.setColumnStretch(1, 4)

        self.item_headers = []
        for col, text in enumerate(["Item", "Effect"]):
            lbl = QLabel(text)
            lbl.setStyleSheet(f"color: {MUTED}; font-size: 11px; font-weight: 700;"
                              f" font-family: {FONT_STACK};")
            self.item_headers.append(lbl)
            self.item_grid.addWidget(lbl, 0, col)

        self.item_scroll.setWidget(self.item_grid_container)
        item_lay.addWidget(self.item_scroll)
        self.list_stack.addWidget(self.item_panel)

    # ------------------------------------------------------------------
    # Team model actions
    # ------------------------------------------------------------------
    def _on_team_name(self, text):
        self.team["name"] = text
        self._dirty = True
        self._update_save_btn()

    def _on_format_changed(self, name):
        if not name:
            return
        fmt = td.find_format(name) or {}
        if not fmt.get("name"):
            # editable combo: only react to a real, known format (typed
            # partial strings and the completer's intermediate text are
            # ignored), and restore the previous valid selection.
            self._fmt = self._fmt or {}
            prev = self.team.get("format") or DEFAULT_FORMAT
            self.team["format"] = prev
            self._sync_format_edit_text(prev)
            return
        self._fmt = fmt
        self.team["format"] = name
        lvl = td.default_level_for_format(fmt)
        for slot in self.team["slots"]:
            if not slot.get("pid"):
                slot["level"] = lvl
        self._clear_filters()
        self._load_format_lists(rebuild=True)
        self._reset_and_render_pokemon_list()
        self._reset_and_render_move_list()
        self._reset_and_render_item_list()
        if self.content_stack.currentWidget() is not self.browse_view:
            self._show_browse()
        self._refresh_roster()
        self._dirty = True
        self._update_save_btn()

    def _load_format_lists(self, rebuild=False):
        if not self._species_base or rebuild:
            self._species_base = td.ordered_pokemon_for_format(self._fmt, self._catalog)
            self._moves_base = td.ordered_moves_for_format(self._fmt)
            self._items_base = td.ordered_items_for_format(self._fmt)
            self._move_to_species = None

    def _species_by_move_index(self):
        if self._move_to_species is None:
            gen = td.format_generation(self._fmt)
            idx = defaultdict(set)
            for m in self._species_base:
                learn = td.legal_moves_for_slug(m["slug"], gen)
                for mid in learn:
                    idx[mid].add(m["slug"])
            self._move_to_species = dict(idx)
        return self._move_to_species

    def _on_team_combo(self, index):
        if index <= 0:
            return
        tid = self.team_combo.itemData(index)
        if tid is None:
            return
        self._load_team(tid)
        self.team_combo.setCurrentIndex(0)

    def _load_team(self, team_id):
        row = get_tb_team(self.conn, team_id)
        if not row:
            return
        data = row["data"]
        fmt_name = row.get("format") or (data or {}).get("format") or DEFAULT_FORMAT
        self.team = data
        self._team_id = team_id
        self._dirty = False
        self.team["slots"] = self.team.get("slots") or [td.empty_slot() for _ in range(6)]
        while len(self.team["slots"]) < 6:
            self.team["slots"].append(td.empty_slot())
        if not self.team.get("name"):
            self.team["name"] = row.get("name") or "Untitled Team"
        self.team["format"] = fmt_name
        fmt = td.find_format(fmt_name) or {}
        self._fmt = fmt if fmt.get("name") else self._fmt

        self.tn_value.blockSignals(True)
        self.tn_value.setText(self.team["name"])
        self.tn_value.blockSignals(False)
        self._sync_format_edit_text(fmt_name if fmt_name in self._format_items else DEFAULT_FORMAT)

        self._clear_filters()
        self._load_format_lists(rebuild=True)
        self._reset_and_render_pokemon_list()
        self._reset_and_render_move_list()
        self._refresh_roster()
        self.delete_btn.setVisible(self._team_id is not None)
        self._update_save_btn()
        self._refresh_counters()
        self._show_browse()
        self._toast("Loaded")

    def _new_team(self):
        fmt_name = self.team.get("format") or DEFAULT_FORMAT
        fmt = td.find_format(fmt_name) or {}
        self.team = td.empty_team("New Team", fmt_name)
        self._team_id = None
        self._dirty = False
        lvl = td.default_level_for_format(fmt)
        for slot in self.team["slots"]:
            slot["level"] = lvl
        self.tn_value.blockSignals(True)
        self.tn_value.setText("New Team")
        self.tn_value.blockSignals(False)
        self._clear_filters()
        self._reset_and_render_pokemon_list()
        self._reset_and_render_move_list()
        self._refresh_roster()
        self.delete_btn.setVisible(False)
        self._update_save_btn()
        self._refresh_counters()
        self._show_browse()

    def _save_team(self):
        if not self.team["name"].strip():
            self.team["name"] = "Untitled Team"
            self.tn_value.setText(self.team["name"])
        if self._team_id is None:
            tid = save_tb_team(self.conn, self.team["name"], self.team.get("format") or "", self.team)
            self._team_id = tid
        else:
            update_tb_team(self.conn, self._team_id, self.team["name"], self.team.get("format") or "", self.team)
        self._dirty = False
        self._reload_team_combo(select=self._team_id)
        self.delete_btn.setVisible(True)
        self._update_save_btn()
        self._toast("Team saved")

    def _delete_team(self):
        if self._team_id is not None:
            delete_tb_team(self.conn, self._team_id)
        self._team_id = None
        self._reload_team_combo()
        self.delete_btn.setVisible(False)
        self._toast("Deleted")
        self._new_team()

    def _reload_team_combo(self, select=None):
        self.team_combo.blockSignals(True)
        self.team_combo.clear()
        self.team_combo.addItem("Open a team…")
        for t in get_tb_teams(self.conn):
            self.team_combo.addItem(t["name"], t["team_id"])
        if select is not None:
            for i in range(self.team_combo.count()):
                if self.team_combo.itemData(i) == select:
                    self.team_combo.setCurrentIndex(i)
                    break
        else:
            self.team_combo.setCurrentIndex(0)
        self.team_combo.blockSignals(False)

    def _on_export(self):
        text = td.build_export(self.team)
        if not text.strip():
            self._toast("Add a Pokémon first")
            return
        QApplication.clipboard().setText(text)
        self._toast("Copied team to clipboard ✓")

    def _toast(self, msg):
        self.toast.setText(msg)
        QTimer.singleShot(2600, lambda: self.toast.setText(""))

    def _update_save_btn(self):
        tokens = THEME.get_tokens()
        if self._dirty:
            bg, fg = tokens['VIOLET'], tokens['WHITE']
        else:
            # Ink pill, dark in light mode / white in dark mode; text
            # contrasts with the pill fill.
            bg, fg = tokens['INK'], tokens['WHITE']
        self.save_btn.setStyleSheet(_pill_qss_for(bg, fg=fg))

    # ------------------------------------------------------------------
    # Roster
    # ------------------------------------------------------------------
    def _refresh_roster(self):
        for slot in self.slots:
            data = self.team["slots"][slot.index]
            slot.set_slot(data.get("pid"), _slot_label(data))

    def _on_slot_clicked(self, index):
        self._active_slot_index = index
        slot = self.team["slots"][index]
        if slot.get("pid"):
            self._open_edit_panel(index)
        else:
            self._open_pokemon_picker(index)

    # ------------------------------------------------------------------
    # Browse mode / search / chips
    # ------------------------------------------------------------------
    def _set_browse_mode(self, mode):
        self._browse_mode = mode
        panel = {MOVE_MODE: self.move_panel, ITEM_MODE: self.item_panel}.get(mode, self.pokedex_panel)
        self.list_stack.setCurrentWidget(panel)
        self._mode_tab_refresh()
        self._refresh_chips()
        self._reset_scroll_positions()
        if mode == POKEMON_MODE:
            self._reset_and_render_pokemon_list()
        elif mode == MOVE_MODE:
            self._reset_and_render_move_list()
        else:
            self._reset_and_render_item_list()

    def _mode_tab_refresh(self):
        t = THEME.get_tokens()
        for mode, btn in self.mode_tabs.items():
            active = mode == self._browse_mode
            btn.setStyleSheet(_pill_qss_for(t['VIOLET'] if active else t['WHITE'],
                                            t['WHITE'] if active else t['INK'], h=30))
        for mode, btn in ((POKEMON_MODE, self.edit_panel.poke_tab), (MOVE_MODE, self.edit_panel.move_tab)):
            active = mode == self._browse_mode
            btn.setStyleSheet(_pill_qss_for(t['VIOLET'] if active else t['WHITE'],
                                            t['WHITE'] if active else t['INK'], h=30))

    def _on_search_edited(self, text):
        self._search_text = text.strip().lower()
        self._refresh_chips()
        self._rebuild_timer.start()

    def _refresh_chips(self):
        while self.chips_lay.count():
            it = self.chips_lay.takeAt(0)
            if it.widget():
                it.widget().deleteLater()

        if self._browse_mode == MOVE_MODE:
            chips = self._move_mode_chips()
        elif self._browse_mode == ITEM_MODE:
            chips = self._item_mode_chips()
        else:
            chips = self._pokemon_mode_chips()

        for chip in chips:
            self.chips_lay.addWidget(chip)
        self.chips_lay.addStretch(1)
        self.chips_scroll.setVisible(bool(chips))

    def _toggle_filter(self, kind, value):
        if kind == "type":
            self._type_filter = None if self._type_filter == value else value
        elif kind == "move":
            self._move_filter = None if self._move_filter == value else value
        elif kind == "ability":
            self._ability_filter = None if self._ability_filter == value else value
        self.search_bar.blockSignals(True)
        self.search_bar.clear()
        self.search_bar.blockSignals(False)
        self._search_text = ""
        self._refresh_chips()
        self._rebuild_timer.start()

    def _clear_filters(self):
        """Reset all search/filter state and rebuild the browse list."""
        self._type_filter = None
        self._move_filter = None
        self._ability_filter = None
        self.search_bar.blockSignals(True)
        self.search_bar.clear()
        self.search_bar.blockSignals(False)
        self._search_text = ""
        self._refresh_chips()
        self._rebuild_timer.start()

    def _pokemon_mode_chips(self):
        out = []
        text = self._search_text

        if self._type_filter:
            c = FilterChip(self._type_filter.capitalize(), TYPE_COLORS.get(self._type_filter, THEME.get_tokens()['VIOLET']),
                           active=True, kind="type", value=self._type_filter)
            c.clicked.connect(lambda: self._toggle_filter("type", self._type_filter))
            out.append(c)
        if self._ability_filter:
            c = FilterChip(td.ability_display_name(self._ability_filter), "#0E7490",
                           active=True, kind="ability", value=self._ability_filter)
            c.clicked.connect(lambda: self._toggle_filter("ability", self._ability_filter))
            out.append(c)
        if self._move_filter:
            c = FilterChip(td.move_display_name(self._move_filter),
                           active=True, kind="move", value=self._move_filter)
            c.clicked.connect(lambda: self._toggle_filter("move", self._move_filter))
            out.append(c)

        if text:
            for tname in TYPE_COLORS:
                if tname in text:
                    c = FilterChip(f"{tname.capitalize()} type", TYPE_COLORS[tname])
                    c.clicked.connect(lambda _=False, k=tname: self._toggle_filter("type", k))
                    out.append(c)
                    break
            move_count = sum(1 for x in out if x.kind == "move")
            for m in self._moves_base:
                if text in m["id"] or text in m["name"].lower():
                    c = FilterChip(m["name"], kind="move", value=m["id"])
                    c.clicked.connect(lambda _=False, mid=m["id"]: self._toggle_filter("move", mid))
                    out.append(c)
                    move_count += 1
                    if move_count >= 4:
                        break
            ab_count = sum(1 for x in out if x.kind == "ability")
            seen_ab = set()
            for m in self._species_base:
                if ab_count >= 4:
                    break
                for aid in (m.get("abilities") or ()):
                    if aid in seen_ab:
                        continue
                    seen_ab.add(aid)
                    nm = td.ability_display_name(aid)
                    if text and (text in nm.lower() or text in aid):
                        c = FilterChip(nm, "#0E7490", kind="ability", value=aid)
                        c.clicked.connect(lambda _=False, a=aid: self._toggle_filter("ability", a))
                        out.append(c)
                        ab_count += 1
                        if ab_count >= 4:
                            break
            picked = 0
            for m in self._species_base:
                if picked >= 3:
                    break
                shown = td._showdown_poke_name(m["name"])
                if text in shown.lower() or text in m["slug"]:
                    c = FilterChip(f"Pokémon: {shown}", THEME.get_tokens()['INK'])
                    c.clicked.connect(lambda _=False, slug=m["slug"], pid=m["id"]: self.pick_species(slug, pid))
                    out.append(c)
                    picked += 1
        return out

    def _move_mode_chips(self):
        out = []
        text = self._search_text
        if self._type_filter:
            c = FilterChip(self._type_filter.capitalize(), TYPE_COLORS.get(self._type_filter, THEME.get_tokens()['VIOLET']),
                           active=True, kind="type", value=self._type_filter)
            c.clicked.connect(lambda: self._toggle_filter("type", self._type_filter))
            out.append(c)
        if text:
            for tname in TYPE_COLORS:
                if tname in text:
                    c = FilterChip(f"{tname.capitalize()} type", TYPE_COLORS[tname])
                    c.clicked.connect(lambda _=False, k=tname: self._toggle_filter("type", k))
                    out.append(c)
                    break
        return out

    def _item_mode_chips(self):
        # Items filter live by name substring in the list itself; no
        # type/move/ability chips make sense for them.
        return []

    # ------------------------------------------------------------------
    # List rendering
    # ------------------------------------------------------------------
    def _rebuild_current_list(self):
        if self._browse_mode == POKEMON_MODE:
            self._reset_and_render_pokemon_list()
        elif self._browse_mode == MOVE_MODE:
            self._reset_and_render_move_list()
        else:
            self._reset_and_render_item_list()

    def _reset_and_render_pokemon_list(self):
        self._load_format_lists()
        self._visible_species = self._filtered_species()
        self.clear_pokedex_grid()
        self._render_species_chunk(90)

    def _filtered_species(self):
        rows = self._species_base
        if self._type_filter:
            rows = [m for m in rows if self._type_filter in (m.get("types") or "")]
        if self._ability_filter:
            rows = [m for m in rows if self._ability_filter in (m.get("abilities") or ())]
        if self._move_filter:
            allowed = self._species_by_move_index().get(self._move_filter, set())
            rows = [m for m in rows if m["slug"] in allowed]
        if self._search_text and not (self._type_filter or self._ability_filter or self._move_filter):
            rows = [m for m in rows if self._search_text in m["name"].lower() or self._search_text in m["slug"]]
        sel = None
        act = self.team["slots"][self._active_slot_index]
        if act.get("species"):
            sel = td.slugify(act["species"])
        if sel:
            rows = [m for m in rows if m["slug"] == sel] + [m for m in rows if m["slug"] != sel]
        return rows

    def _render_species_chunk(self, count=90):
        rows = self._visible_species
        start = self._current_load_index
        end = min(len(rows), start + count)
        for i in range(start, end):
            self.add_pokemon_row(self._row_to_mon(rows[i]))
        self._current_load_index = end
        if end == len(rows) and self._current_load_index == end:
            self._schedule_species_fill()

    def _schedule_species_fill(self):
        QTimer.singleShot(60, self._check_species_fill)

    def _check_species_fill(self):
        rows = self._visible_species
        if self._current_load_index >= len(rows):
            return
        if not self.pokedex_scroll.isVisible():
            return
        sb = self.pokedex_scroll.verticalScrollBar()
        if sb.maximum() == 0:
            self._render_species_chunk(90)

    def _row_to_mon(self, m):
        return {
            "name": td._showdown_poke_name(m["name"]),
            "slug": m["slug"],
            "pid": m["id"],
            "types": [t.capitalize() for t in (m.get("types") or [])],
            "abilities": [td.ability_display_name(a) for a in (m.get("abilities") or [])],
            "stats": m.get("stats", {}),
        }

    def add_pokemon_row(self, mon_data):
        """Adds a Pokémon entry to the pokedex grid (row 0 is the header).
        Accepts the legacy keys (name/types/abilities/stats) plus optional
        'slug'/'pid' which make the row clickable in the picker."""
        self._loaded_rows_count = getattr(self, '_loaded_rows_count', 0) + 1
        row_idx = self._loaded_rows_count
        tokens = THEME.get_tokens()

        name_widget = QWidget()
        n_lay = QHBoxLayout(name_widget)
        n_lay.setContentsMargins(0, 4, 0, 4)
        n_lay.setSpacing(8)
        if mon_data.get("icon"):
            icon_lbl = QLabel()
            icon_lbl.setPixmap(mon_data["icon"])
            n_lay.addWidget(icon_lbl)
        if mon_data.get("slug"):
            name_lbl = ClickableLabel(mon_data.get("name", "Unknown"))
            slug = mon_data["slug"]
            pid = mon_data.get("pid")
            name_lbl.clicked.connect(lambda: self.pick_species(slug, pid))
        else:
            name_lbl = QLabel(mon_data.get("name", "Unknown"))
        name_lbl.setStyleSheet(f"color: {tokens['INK']}; font-family: {FONT_STACK};"
                               f" font-size: 13px; font-weight: 700; background: transparent;")
        name_lbl.setProperty("cat_role", "pmon_name")
        n_lay.addWidget(name_lbl)
        n_lay.addStretch(1)
        self.pokedex_grid.addWidget(name_widget, row_idx, 0)

        types_widget = QWidget()
        t_lay = QHBoxLayout(types_widget)
        t_lay.setContentsMargins(0, 4, 0, 4)
        t_lay.setSpacing(4)
        for t_name in (mon_data.get("types") or [])[:2]:
            color = TYPE_COLORS.get(t_name.lower(), tokens['VIOLET'])
            t_lay.addWidget(make_type_chip(t_name.upper(), color))
        t_lay.addStretch(1)
        self.pokedex_grid.addWidget(types_widget, row_idx, 1)

        abilities_widget = QWidget()
        ab_lay = QGridLayout(abilities_widget)
        ab_lay.setContentsMargins(0, 4, 0, 4)
        ab_lay.setHorizontalSpacing(12)
        ab_lay.setVerticalSpacing(2)
        abilities = list(mon_data.get("abilities") or [])
        for adx, ab_name in enumerate(abilities[:4]):
            c = adx // 2
            r = adx % 2
            ab_lbl = QLabel(ab_name)
            ab_lbl.setStyleSheet(f"color: {tokens['INK']}; font-family: {FONT_STACK};"
                                 f" font-size: 11px; font-weight: 500; background: transparent;")
            ab_lbl.setProperty("cat_role", "pmon_ability")
            ab_lay.addWidget(ab_lbl, r, c)
        self.pokedex_grid.addWidget(abilities_widget, row_idx, 2)

        stats = mon_data.get("stats", {})
        stat_vals = [stats.get(k, 0) for k in ("hp", "atk", "def", "spa", "spd", "spe")]
        all_vals = stat_vals + [sum(stat_vals)]
        for i, val in enumerate(all_vals):
            lbl = QLabel(str(val))
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            weight = "700" if i == 6 else "400"
            lbl.setStyleSheet(f"color: {tokens['INK']}; font-family: {FONT_STACK};"
                              f" font-size: 12px; font-weight: {weight}; background: transparent;")
            lbl.setProperty("cat_role", "pmon_bst" if i == 6 else "pmon_stat")
            self.pokedex_grid.addWidget(lbl, row_idx, 3 + i)

    def _reset_and_render_move_list(self):
        self._load_format_lists()
        self._visible_moves = self._filtered_moves()
        self._move_load_index = 0
        while self.move_grid.count() > len(self.move_headers):
            item = self.move_grid.takeAt(len(self.move_headers))
            if item and item.widget():
                item.widget().deleteLater()
        self._render_move_chunk(90)

    def _filtered_moves(self):
        slot = None
        if 0 <= self._active_slot_index < len(self.team["slots"]):
            slot = self.team["slots"][self._active_slot_index]
        slug = td.slugify(slot["species"]) if (slot and slot.get("species")) else None

        # Use per-species move ordering when a species is selected
        if slug:
            rows = td.ordered_species_moves(self._fmt.get("name", ""), slug)
        else:
            # No species selected: fall back to global format move list
            rows = self._moves_base

        if self._type_filter:
            rows = [m for m in rows if (m.get("type") or "").lower() == self._type_filter]
        if self._search_text and not self._type_filter:
            rows = [m for m in rows if self._search_text in m["id"] or self._search_text in m["name"].lower()]
        cur = ""
        if slot and slot.get("moves"):
            cur = slot["moves"][self._pending_move_idx]
        if cur and rows and any(m["id"] == cur for m in rows):
            rows = [m for m in rows if m["id"] == cur] + [m for m in rows if m["id"] != cur]
        return rows

    def _render_move_chunk(self, count=90):
        rows = self._visible_moves
        start = self._move_load_index
        end = min(len(rows), start + count)
        tokens = THEME.get_tokens()
        for i in range(start, end):
            m = rows[i]
            row_idx = i + 1
            typ = (m.get("type") or "").lower()
            chip = make_type_chip((typ or "?").upper(), TYPE_COLORS.get(typ, tokens['VIOLET']))
            chip_host = QWidget()
            ch = QHBoxLayout(chip_host)
            ch.setContentsMargins(0, 2, 0, 2)
            ch.addWidget(chip)
            ch.addStretch(1)
            self.move_grid.addWidget(chip_host, row_idx, 1)

            cat = (m.get("category") or "Status")
            cat_lbl = QLabel(cat)
            cat_lbl.setStyleSheet(f"color: {CATEGORY_COLORS.get(cat, MUTED)}; font-family: {FONT_STACK};"
                                  f" font-size: 11px; font-weight: 700; background: transparent;")
            cat_lbl.setProperty("cat_role", "move_cat")
            cat_lbl.setProperty("cat_category", cat)
            self.move_grid.addWidget(cat_lbl, row_idx, 2)

            name_lbl = ClickableLabel(m["name"])
            name_lbl.setStyleSheet(f"color: {tokens['INK']}; font-family: {FONT_STACK};"
                                   f" font-size: 12px; font-weight: 700; background: transparent;")
            name_lbl.setProperty("cat_role", "move_name")
            name_lbl.clicked.connect(lambda _=False, mid=m["id"]: self.pick_move(mid))
            self.move_grid.addWidget(name_lbl, row_idx, 0)

            desc_lbl = ClickableLabel(m.get("shortDesc") or "")
            desc_lbl.setStyleSheet(f"color: {MUTED}; font-family: {FONT_STACK}; font-size: 11px; font-weight: 400;"
                                   f" background: transparent;")
            desc_lbl.setProperty("cat_role", "move_desc")
            desc_lbl.clicked.connect(lambda _=False, mid=m["id"]: self.pick_move(mid))
            self.move_grid.addWidget(desc_lbl, row_idx, 3)
        self._move_load_index = end
        self._check_move_fill()

    def _check_move_fill(self):
        rows = self._visible_moves
        if self._move_load_index >= len(rows):
            return
        if not self.move_scroll.isVisible():
            return
        sb = self.move_scroll.verticalScrollBar()
        if sb.maximum() == 0:
            QTimer.singleShot(60, self._render_move_chunk)

    def _reset_and_render_item_list(self):
        self._load_format_lists()
        self._visible_items = self._filtered_items()
        self._item_load_index = 0
        while self.item_grid.count() > len(self.item_headers):
            item = self.item_grid.takeAt(len(self.item_headers))
            if item and item.widget():
                item.widget().deleteLater()
        self._render_item_chunk(90)

    def _filtered_items(self):
        rows = self._items_base
        if self._search_text:
            rows = [m for m in rows if self._search_text in m["id"] or self._search_text in m["name"].lower()]
        cur = ""
        slot = None
        if 0 <= self._active_slot_index < len(self.team["slots"]):
            slot = self.team["slots"][self._active_slot_index]
        if slot:
            cur = slot.get("item") or ""
        if cur and rows and any(m["id"] == cur for m in rows):
            rows = [m for m in rows if m["id"] == cur] + [m for m in rows if m["id"] != cur]
        return rows

    def _render_item_chunk(self, count=90):
        rows = self._visible_items
        start = self._item_load_index
        end = min(len(rows), start + count)
        tokens = THEME.get_tokens()
        for i in range(start, end):
            m = rows[i]
            row_idx = i + 1
            name_lbl = ClickableLabel(m["name"])
            name_lbl.setStyleSheet(f"color: {tokens['INK']}; font-family: {FONT_STACK};"
                                   f" font-size: 12px; font-weight: 700; background: transparent;")
            name_lbl.setProperty("cat_role", "item_name")
            name_lbl.clicked.connect(lambda _=False, iid=m["id"]: self.pick_item(iid))
            self.item_grid.addWidget(name_lbl, row_idx, 0)
            desc_lbl = ClickableLabel(m.get("desc") or "")
            desc_lbl.setStyleSheet(f"color: {MUTED}; font-family: {FONT_STACK}; font-size: 11px; font-weight: 400;"
                                   f" background: transparent;")
            desc_lbl.setProperty("cat_role", "item_desc")
            desc_lbl.clicked.connect(lambda _=False, iid=m["id"]: self.pick_item(iid))
            self.item_grid.addWidget(desc_lbl, row_idx, 1)
        self._item_load_index = end
        self._check_item_fill()

    def _check_item_fill(self):
        rows = self._visible_items
        if self._item_load_index >= len(rows):
            return
        if not self.item_scroll.isVisible():
            return
        sb = self.item_scroll.verticalScrollBar()
        if sb.maximum() == 0:
            QTimer.singleShot(60, self._render_item_chunk)

    # ------------------------------------------------------------------
    # Scroll + legacy compatibility
    # ------------------------------------------------------------------
    def _on_pokedex_scroll(self, value):
        vbar = self.pokedex_scroll.verticalScrollBar()
        if vbar.maximum() <= 0 or value <= vbar.maximum() - 200:
            return
        if self._visible_species:
            self._render_species_chunk(90)
        else:
            self.fetch_more_pokedex_rows(50)

    def _on_move_scroll(self, value):
        vbar = self.move_scroll.verticalScrollBar()
        if vbar.maximum() > 0 and value > vbar.maximum() - 200:
            self._render_move_chunk(90)

    def _on_item_scroll(self, value):
        vbar = self.item_scroll.verticalScrollBar()
        if vbar.maximum() > 0 and value > vbar.maximum() - 200:
            self._render_item_chunk(90)

    def _reset_scroll_positions(self):
        """Return every list back to the top (the spec: re-opening the
        search/picker should never land you mid-list)."""
        for scroll in (self.pokedex_scroll, self.move_scroll, self.item_scroll):
            sb = scroll.verticalScrollBar()
            if sb:
                sb.setValue(0)

    def clear_pokedex_grid(self):
        self._current_load_index = 0
        self._loaded_rows_count = 0
        while self.pokedex_grid.count() > len(self.pokedex_headers):
            item = self.pokedex_grid.takeAt(len(self.pokedex_headers))
            if item and item.widget():
                item.widget().deleteLater()

    def load_pokedex(self, db_path=None, initial_count=30):
        self.clear_pokedex_grid()
        self.pokedex_data = load_pokedex_pokemon(db_path)
        limit = len(self.pokedex_data) if initial_count is None else min(len(self.pokedex_data), initial_count)
        for mon in self.pokedex_data[:limit]:
            self.add_pokemon_row(mon)
        self._current_load_index = limit

    def fetch_more_pokedex_rows(self, count=50):
        if not hasattr(self, 'pokedex_data') or not self.pokedex_data:
            return
        limit = min(len(self.pokedex_data), self._current_load_index + count)
        while self._current_load_index < limit:
            self.add_pokemon_row(self.pokedex_data[self._current_load_index])
            self._current_load_index += 1

    @property
    def table(self):
        class _TableCompat:
            def __init__(self, tab):
                self._tab = tab

            def rowCount(self):
                return max(1, getattr(self._tab, '_loaded_rows_count', 0) + 1)
        return _TableCompat(self)

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------
    def _show_browse(self):
        self._reset_scroll_positions()
        self.content_stack.setCurrentWidget(self.browse_view)
        self._mode_tab_refresh()

    def _open_edit_panel(self, index):
        self._active_slot_index = index
        self.edit_panel.set_slot_index(index)
        self.content_stack.setCurrentWidget(self.edit_panel)
        self._mode_tab_refresh()

    def _open_pokemon_picker(self, index):
        self._active_slot_index = index
        self._browse_mode = POKEMON_MODE
        self._set_browse_mode(POKEMON_MODE)
        self._refresh_roster()
        self.content_stack.setCurrentWidget(self.browse_view)
        self.search_bar.setFocus()

    def _open_move_picker(self, index, move_idx):
        self._active_slot_index = index
        self._pending_move_idx = move_idx
        slot = self.team["slots"][index]
        if not slot.get("species"):
            self._toast("Pick a Pokémon first")
            return
        self._browse_mode = MOVE_MODE
        self._set_browse_mode(MOVE_MODE)
        self._refresh_roster()
        self.content_stack.setCurrentWidget(self.browse_view)
        self.search_bar.setFocus()

    def _open_item_picker(self, index):
        self._active_slot_index = index
        self._browse_mode = ITEM_MODE
        self._set_browse_mode(ITEM_MODE)
        self._refresh_roster()
        self.content_stack.setCurrentWidget(self.browse_view)
        self.search_bar.setFocus()

    def open_ev_editor_for(self, stat_key):
        slot = self.team["slots"][self._active_slot_index]
        if not slot.get("pid"):
            self._toast("Pick a Pokémon first")
            return
        self.ev_editor.set_slot(slot, focus_stat=None if stat_key == "nature" else stat_key)
        self.content_stack.setCurrentWidget(self.ev_editor)

    def _close_ev_editor(self):
        self.content_stack.setCurrentWidget(self.edit_panel)
        self._refresh_edit_panel()

    # ------------------------------------------------------------------
    # Slot editing
    # ------------------------------------------------------------------
    def pick_species(self, slug, pid=None):
        slot = self.team["slots"][self._active_slot_index]
        if pid is None:
            mon = td.catalog_by_slug().get(slug) or {}
            pid = mon.get("id")
        slot["pid"] = pid
        slot["species"] = slug
        lvl = slot.get("level")
        slot["level"] = lvl if isinstance(lvl, int) else td.default_level_for_format(self._fmt)
        slot["ability"] = ""
        slot["gender"] = "-"
        slot["tera"] = "-"
        self._mark_slot_changed()
        self._open_edit_panel(self._active_slot_index)

    def pick_move(self, move_id):
        slot = self.team["slots"][self._active_slot_index]
        if slot.get("species"):
            slug = td.slugify(slot["species"])
            candidate_ids = {m["id"] for m in td.ordered_species_moves(self._fmt.get("name", ""), slug)}
            if move_id not in candidate_ids:
                self._toast(f"{td._showdown_poke_name(slot['species'])} can't learn that here")
                return
        slot["moves"][self._pending_move_idx] = move_id
        self._mark_slot_changed()
        self._open_edit_panel(self._active_slot_index)

    def pick_item(self, item_id):
        slot = self.team["slots"][self._active_slot_index]
        slot["item"] = item_id
        self._mark_slot_changed()
        self._open_edit_panel(self._active_slot_index)

    def _mark_slot_changed(self):
        self._dirty = True
        self._update_save_btn()
        self._refresh_roster()
        self._counters_timer.start()

    def _slot_edited(self, roster_only=False):
        self._dirty = True
        self._update_save_btn()
        if roster_only:
            self._refresh_roster()
        else:
            self._refresh_edit_panel()
        self._counters_timer.start()

    def _refresh_edit_panel(self):
        if self.content_stack.currentWidget() is self.edit_panel:
            self.edit_panel.set_slot_index(self._active_slot_index)
        elif self.content_stack.currentWidget() is self.ev_editor:
            self.ev_editor._refresh_values()

    def _refresh_counters(self):
        while self.counters_chips_lay.count():
            it = self.counters_chips_lay.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        if not any(s.get("pid") for s in self.team["slots"]):
            return
        rows = td.compute_team_counters(self.team, limit=5)
        tokens = THEME.get_tokens()
        for r in rows:
            chip = QPushButton(f"{r['name']}  {r['score']}")
            chip.setCursor(Qt.CursorShape.PointingHandCursor)
            chip.setFixedHeight(28)
            chip.setStyleSheet(_counter_chip_qss(tokens))
            self.counters_chips_lay.addWidget(chip)

    def _apply_theme(self, *_args):
        tokens = THEME.get_tokens()
        self.container.setStyleSheet(f"background-color: {tokens['WHITE']};")
        self.team_name_card.setStyleSheet(f"background-color: {tokens['TRACK']}; border-radius: 8px;")
        self.tn_title.setStyleSheet(f"color: {tokens['MUTED']}; font-family: {FONT_STACK};"
                                    f" font-size: 11px; font-weight: 700;")
        self.fmt_disclaimer.setStyleSheet(f"color: {tokens['MUTED']}; font-size: 10px; font-style: italic;"
                                          f" font-family: {FONT_STACK};")
        self.fmt_lbl.setStyleSheet(f"color: {tokens['MUTED']}; font-size: 11px; font-weight: 700;"
                                   f" font-family: {FONT_STACK}; padding-left: 2px;")
        self.search_bar.setStyleSheet(f"""
            QLineEdit {{
                background-color: {tokens['PALE_VIOLET']};
                border: none;
                border-radius: 23px;
                padding: 0 20px;
                color: {tokens['BODY']};
                font-family: {FONT_STACK};
                font-size: 14px;
            }}
        """)
        set_placeholder_color(self.search_bar, tokens['PLACEHOLDER'])
        icon = lucide("search", tokens['MUTED'], 18)
        for action in self.search_bar.actions():
            self.search_bar.removeAction(action)
        self.search_bar.addAction(QIcon(icon), QLineEdit.ActionPosition.TrailingPosition)

        self.pokedex_panel.setStyleSheet(f"background-color: {tokens['PALE_VIOLET']}; border-radius: 12px;")
        self.move_panel.setStyleSheet(f"background-color: {tokens['PALE_VIOLET']}; border-radius: 12px;")
        for lbl in self.pokedex_headers:
            lbl.setStyleSheet(f"color: {tokens['MUTED']}; font-size: 11px; font-weight: 700;"
                              f" font-family: {FONT_STACK}; background: transparent;")
        for lbl in self.move_headers:
            lbl.setStyleSheet(f"color: {tokens['MUTED']}; font-size: 11px; font-weight: 700;"
                              f" font-family: {FONT_STACK}; background: transparent;")
        for slot in self.slots:
            slot._apply_theme(tokens)
        self.edit_panel._apply_theme(tokens)
        if hasattr(self, "ev_editor"):
            self.ev_editor._apply_theme(tokens)
        self._theme_header(tokens)
        self._mode_tab_refresh()
        self._update_save_btn()
        # Recolor the catalog rows in place. A theme switch must only restyle
        # -- the old reload_catalog() here flushed every data cache, reparsed
        # pokemon_complete.db and destroyed+recreated ~300 widgets just to
        # rebake the token colors into the rows, which is most of the lag.
        self._retheme_catalog(tokens)

    def _theme_header(self, tokens):
        """Re-style the header buttons/inputs that were built once at init
        with the light-mode defaults (PillButton defaults, _input_qss/_combo_qss)."""
        from fourslice.gui.imports_page import _pill_qss
        self.new_btn.setStyleSheet(_pill_qss("PillButton", tokens['WHITE'], tokens['INK'], 44))
        self.delete_btn.setStyleSheet(_pill_qss("PillButton", tokens['WHITE'], tokens['INK'], 44))
        self.export_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {tokens['WHITE']};
                color: {tokens['INK']};
                border: 1px solid {tokens['BORDER']};
                border-radius: 12px;
                font-family: {FONT_STACK};
                font-size: 12px;
                font-weight: 700;
                padding: 0 14px;
            }}
            QPushButton:hover {{ border-color: {tokens['VIOLET']}; color: {tokens['VIOLET']}; }}
        """)
        if hasattr(self, "tn_value"):
            self.tn_value.setStyleSheet(_input_qss())
            self.format_combo.setStyleSheet(_combo_qss())
            self.format_combo.lineEdit().setStyleSheet(_input_qss())
            self.team_combo.setStyleSheet(_combo_qss())

    def reload_catalog(self):
        """Reload the Pokemon catalogue and every picker list from the
        (possibly refreshed) data files.

        Called from the main thread when the background data refresh
        finishes (see ``_RefreshDoneBridge`` in data_refresh_worker.py).
        Safe to call at any time: every sub-step is the same idempotent
        data-binding path ``__init__`` uses, so with unchanged data the
        lists are rebuilt to the same content.
        """
        td._clear_data_caches()  # invalidate teambuilder_data lru_caches only
        self._catalog = td.load_pokemon_catalog()
        self._load_format_lists(rebuild=True)
        self._populate_formats()
        self._reset_and_render_pokemon_list()
        self._reset_and_render_move_list()
        self._reset_and_render_item_list()
        self._refresh_roster()
        self._refresh_counters()

    def _retheme_catalog(self, tokens):
        """Recolor the existing picker rows and top-threats chips for the
        active theme, in place.

        The row labels bake token colors in at build time (see
        add_pokemon_row / _render_{move,item}_chunk); this walks the built
        rows and re-applies their per-role stylesheet with the new tokens.
        Cheaper than reload_catalog(): no cache flush, no re-parse of the
        pokemon DB, no widget destruction/recreation -- a theme switch only
        needs the colors to change.
        """
        for container in (self.pokedex_grid_container, self.move_grid_container,
                          self.item_grid_container):
            for lbl in container.findChildren(QLabel):
                role = lbl.property("cat_role")
                if not role:
                    continue
                if role == "move_cat":
                    cat = str(lbl.property("cat_category") or "Status")
                    lbl.setStyleSheet(
                        f"color: {CATEGORY_COLORS.get(cat, tokens['MUTED'])};"
                        f" font-family: {FONT_STACK}; font-size: 11px; font-weight: 700;"
                        f" background: transparent;")
                else:
                    lbl.setStyleSheet(_catalog_row_style(role, tokens))
        # The top-threats counter chips are similarly rebuilt with baked
        # colors; recolor them rather than re-running compute_team_counters.
        for i in range(self.counters_chips_lay.count()):
            item = self.counters_chips_lay.itemAt(i)
            if item is not None and item.widget() is not None:
                item.widget().setStyleSheet(_counter_chip_qss(tokens))