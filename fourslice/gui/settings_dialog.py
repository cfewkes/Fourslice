"""
fourslice/gui/settings_dialog.py

Settings for Fourslice: whether raw replay logs are persisted (they're
what the Replays tab plays back) and, when they are, how many days
they're kept before pruning -- plus whether the Replays tab fetches
and shows Pokemon Showdown's gen-5 sprites.

Pruning happens immediately on OK so the change never waits for the
next launch: turning storage OFF deletes every stored log right away
(the Replays tab empties accordingly), and shrinking the retention
window drops anything already too old. base_dir is threaded through
purely so tests can point this at a temp config instead of touching
the real one -- real app code never passes it.
"""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFormLayout, QFrame, QHBoxLayout, QLabel,
    QSpinBox, QVBoxLayout, QWidget,
)

from fourslice import config
from fourslice.storage import prune_old_logs
from fourslice.gui.imports_page import (
    THEME, WHITE, INK, BODY, MUTED, VIOLET, PALE_VIOLET, BORDER, TRACK, FONT_STACK, PillButton
)


def _get_combo_qss_style(tokens):
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


class SettingsDialog(QDialog):
    # Emitted after settings are written to config, so live widgets
    # (e.g. the Replays tab's sprite store) can refresh.
    settings_applied = Signal()

    def __init__(self, conn, base_dir=None, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.base_dir = base_dir
        self.setWindowTitle("Settings")

        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(32, 28, 32, 28)
        main_layout.setSpacing(20)

        # -- Header Section --
        header_layout = QVBoxLayout()
        header_layout.setSpacing(4)

        self.title_label = QLabel("Settings")
        header_layout.addWidget(self.title_label)

        self.subtitle_label = QLabel("Configure data storage, replay playback, and visual preferences.")
        header_layout.addWidget(self.subtitle_label)
        main_layout.addLayout(header_layout)

        # -- Settings Card --
        self.settings_card = QFrame()
        self.settings_card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        card_layout = QVBoxLayout(self.settings_card)
        card_layout.setContentsMargins(20, 16, 20, 16)
        card_layout.setSpacing(16)

        self.intro_label = QLabel(
            "Stored raw logs are what the Replays tab plays back. "
            "Turning this off deletes every stored log immediately, "
            "and future imports won't save new ones."
        )
        self.intro_label.setWordWrap(True)
        card_layout.addWidget(self.intro_label)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        form.setFormAlignment(Qt.AlignmentFlag.AlignLeft)
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(12)

        self.store_logs_check = QCheckBox("Store raw replay logs")
        self.store_logs_check.setChecked(config.get_store_logs(base_dir))
        self.store_logs_check.toggled.connect(self._on_store_logs_toggled)
        form.addRow("Playback data:", self.store_logs_check)

        self.retention_spin = QSpinBox()
        self.retention_spin.setRange(0, 3650)
        self.retention_spin.setValue(config.get_log_retention_days(base_dir))
        self.retention_spin.setSuffix(" days")
        self.retention_spin.setSpecialValueText("Keep forever")
        self.retention_spin.setToolTip(
            "Raw logs older than this are deleted. 0 = keep forever."
        )
        # An explicit, discoverable shortcut to "keep forever": toggling
        # it pins the spinbox to 0 and disables up-down fiddling.
        self.keep_forever_check = QCheckBox("Keep forever")
        self.keep_forever_check.setToolTip(
            "Never delete stored raw logs. Uncheck to prune old logs "
            "after a set number of days."
        )
        self.keep_forever_check.toggled.connect(self._on_keep_forever_toggled)

        retention_row = QWidget()
        retention_row_layout = QHBoxLayout(retention_row)
        retention_row_layout.setContentsMargins(0, 0, 0, 0)
        retention_row_layout.setSpacing(8)
        retention_row_layout.addWidget(self.retention_spin)
        retention_row_layout.addWidget(self.keep_forever_check)
        retention_row_layout.addStretch(1)
        form.addRow("Keep logs for:", retention_row)

        # If the config already says 0 days (forever), reflect that in
        # the checkbox so the two stay in sync on open.
        self.keep_forever_check.setChecked(self.retention_spin.value() == 0)

        self.use_sprites_check = QCheckBox(
            "Use sprites in the Replays tab "
            "(fetched from play.pokemonshowdown.com and cached locally)"
        )
        self.use_sprites_check.setChecked(config.get_use_sprites(base_dir))
        self.use_sprites_check.setToolTip(
            "Showdown sprites, downloaded once and cached on "
            "disk. Turn off for fully offline use -- missing or not-yet-"
            "downloaded sprites fall back to the plain name."
        )
        form.addRow("Replays sprites:", self.use_sprites_check)

        self.sprite_style_combo = QComboBox()
        self.sprite_style_combo.addItems(["Gen 5", "3D (XY)"])
        self.sprite_style_combo.setCurrentText(
            "Gen 5" if config.get_sprite_style(base_dir) == "gen5" else "3D (XY)"
        )
        self.sprite_style_combo.setToolTip(
            "Gen 5: the community-drawn gen-5 style sprites Showdown's web "
            "player uses. 3D (XY): the rendered 3D models from the XY-era sets."
        )
        form.addRow("Sprite style:", self.sprite_style_combo)

        self.animate_board_check = QCheckBox(
            "Animate the replay board "
            "(move flashes, HP drain, faint fade-outs, idle motion)"
        )
        self.animate_board_check.setChecked(config.get_animate_board(base_dir))
        self.animate_board_check.setToolTip(
            "Showdown-lite effects on the board: attackers surge, targets "
            "flash red, HP bars drain, sprites fade on switch/faint, and "
            "idle sprites bob gently. Turn off for a quieter board."
        )
        form.addRow("Board animation:", self.animate_board_check)

        card_layout.addLayout(form)

        # Button row
        button_row = QHBoxLayout()
        button_row.addStretch(1)

        self.cancel_button = PillButton("Cancel", bg=TRACK, fg=BODY, height=36)
        self.cancel_button.clicked.connect(self.reject)
        button_row.addWidget(self.cancel_button)

        self.ok_button = PillButton("OK", bg=VIOLET, fg=WHITE, height=36)
        self.ok_button.clicked.connect(self.accept)
        button_row.addWidget(self.ok_button)

        card_layout.addLayout(button_row)

        main_layout.addWidget(self.settings_card)

        # Apply theme
        THEME.theme_changed.connect(self._apply_theme)
        self._apply_theme()

        # Initial enabled state: storage off disables the whole retention
        # row; "keep forever" additionally disables the day-count spinbox.
        _initial_store = self.store_logs_check.isChecked()
        self.retention_spin.setEnabled(
            _initial_store and not self.keep_forever_check.isChecked()
        )
        self.keep_forever_check.setEnabled(_initial_store)

    def accept(self):
        store = self.store_logs_check.isChecked()
        days = 0 if self.keep_forever_check.isChecked() else self.retention_spin.value()
        config.set_store_logs(store, self.base_dir)
        config.set_log_retention_days(days, self.base_dir)
        config.set_use_sprites(self.use_sprites_check.isChecked(), self.base_dir)
        config.set_animate_board(self.animate_board_check.isChecked(), self.base_dir)
        config.set_sprite_style(
            "3d" if self.sprite_style_combo.currentText().startswith("3D") else "gen5",
            self.base_dir,
        )

        if store:
            prune_old_logs(self.conn, days)
        else:
            # Storage is off: there's no reason to keep anything, so
            # wipe the whole table rather than trusting the retention
            # window (a fresh log wouldn't match any age cutoff anyway).
            self.conn.execute("DELETE FROM logs")
            self.conn.commit()

        # Tell live widgets (the Replays tab's sprite store) to pick up
        # whatever they need from the newly-written config.
        self.settings_applied.emit()

        super().accept()

    def _on_keep_forever_toggled(self, checked):
        """Pinning the retention to forever (checkbox on) either grants
        or revokes the spinbox's up/down steppers: while "Keep forever"
        is checked the box reads 0 / "Keep forever" and the steppers are
        disabled; unchecking hands the controls back to the user at the
        last concrete day count."""
        if checked:
            self._prior_days = self.retention_spin.value()
            self.retention_spin.setValue(0)
            self.retention_spin.setEnabled(False)
        else:
            self.retention_spin.setEnabled(self.store_logs_check.isChecked())
            self.retention_spin.setValue(
                getattr(self, "_prior_days", 7) or 7
            )

    def _on_store_logs_toggled(self, checked):
        """Turning raw-log storage off disables the retention controls
        (there's nothing to prune); turning it back on restores them
        unless the user has pinned the retention to "keep forever"."""
        self.retention_spin.setEnabled(
            checked and not self.keep_forever_check.isChecked()
        )
        self.keep_forever_check.setEnabled(checked)

    def _apply_theme(self, *_args):
        tokens = THEME.get_tokens()

        self.setStyleSheet(f"background-color: {tokens['WHITE']};")
        self.title_label.setStyleSheet(f"color: {tokens['INK']}; font-family: {FONT_STACK}; font-size: 24px; font-weight: 700;")
        self.subtitle_label.setStyleSheet(f"color: {tokens['MUTED']}; font-family: {FONT_STACK}; font-size: 14px;")

        self.settings_card.setStyleSheet(f"""
            QFrame {{
                background-color: {tokens['WHITE']};
                border: 1px solid {tokens['BORDER']};
                border-radius: 12px;
            }}
        """)

        self.intro_label.setStyleSheet(f"color: {tokens['BODY']}; font-family: {FONT_STACK}; font-size: 13px;")

        label_qss = f"color: {tokens['BODY']}; font-family: {FONT_STACK}; font-size: 14px; font-weight: 500;"
        check_qss = f"color: {tokens['INK']}; font-family: {FONT_STACK}; font-size: 13px;"
        
        # Style checkboxes
        for check in [self.store_logs_check, self.use_sprites_check, self.animate_board_check, self.keep_forever_check]:
            check.setStyleSheet(check_qss)

        self.sprite_style_combo.setStyleSheet(_get_combo_qss_style(tokens))

        # Style spinbox (with explicit steppers so the up/down arrows
        # stay clickable and visible on every platform)
        self.retention_spin.setStyleSheet(f"""
            QSpinBox {{
                background-color: {tokens['TRACK']};
                color: {tokens['INK']};
                border: 1px solid {tokens['BORDER']};
                border-radius: 8px;
                padding: 4px 8px;
                font-family: {FONT_STACK};
                font-size: 13px;
                min-height: 32px;
            }}
            QSpinBox:focus {{
                border-color: {tokens['VIOLET']};
            }}
            QSpinBox::up-button {{
                subcontrol-origin: border;
                subcontrol-position: top right;
                width: 22px;
                border-left: 1px solid {tokens['BORDER']};
                background-color: {tokens['TRACK']};
                border-top-right-radius: 8px;
            }}
            QSpinBox::down-button {{
                subcontrol-origin: border;
                subcontrol-position: bottom right;
                width: 22px;
                border-left: 1px solid {tokens['BORDER']};
                background-color: {tokens['TRACK']};
                border-bottom-right-radius: 8px;
            }}
            QSpinBox::up-button:hover, QSpinBox::down-button:hover {{
                background-color: {tokens['PALE_VIOLET']};
            }}
            QSpinBox::up-button:disabled, QSpinBox::down-button:disabled {{
                background-color: {tokens['TRACK']};
            }}
            QSpinBox::up-arrow, QSpinBox::down-arrow {{
                width: 9px;
                height: 9px;
            }}
        """)

        # Style buttons
        from fourslice.gui.imports_page import _pill_qss
        self.cancel_button.setStyleSheet(_pill_qss("PillButton", tokens['TRACK'], tokens['BODY'], 36))
        self.ok_button.setStyleSheet(_pill_qss("PillButton", tokens['VIOLET'], tokens['WHITE'], 36))
