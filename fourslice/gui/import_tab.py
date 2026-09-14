"""
fourslice/gui/import_tab.py

The "Import" tab: single-replay import, bulk import ("Sync Now"),
and a regulation-filterable recent-imports list. Extracted from what
used to be the entire main window, now that the app has a second
concern (stats) worth its own tab -- see main_window.py.

Single-replay import runs on a background QThread so the UI never
freezes, even on a slow/hung server. Bulk import also runs on a
background QThread via BulkImportWorker -- see that file for why a
second sqlite connection is used there.

The bulk-import machinery doubles as "sync": main.py triggers it
once automatically right after the window is shown, and the
"Sync Now" button re-runs it on demand. Cheap either way, since both
the network fetch and the parse/store step stop at the first
already-known replay -- see parser.find_all_replay_urls_for_user and
BulkImportWorker._import_urls. Deliberately NOT triggered from
__init__ itself, so constructing an ImportTab in tests doesn't spin
up a real background network thread.
"""

from datetime import datetime, timezone

from PySide6.QtCore import Qt, QThread, QObject, Signal
from PySide6.QtWidgets import (
    QComboBox, QFrame, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget,
    QProgressBar, QPushButton, QVBoxLayout, QWidget,
)

from fourslice import config
from fourslice.parser import fetch_replay_json
from fourslice.storage import import_replay
from fourslice.gui.bulk_import_worker import BulkImportWorker
from fourslice.gui.imports_page import (
    THEME, WHITE, INK, BODY, MUTED, VIOLET, PALE_VIOLET, BORDER, TRACK, FONT_STACK, PillButton, set_placeholder_color
)


class SingleImportWorker(QObject):
    """Worker that fetches and imports a single replay on a background thread."""
    finished = Signal(dict)  # result dict from import_replay or error
    error = Signal(str)      # error message

    def __init__(self, url: str, conn, usernames: list[str], store_logs: bool):
        super().__init__()
        self.url = url
        self.conn = conn
        self.usernames = usernames
        self.store_logs = store_logs

    def run(self):
        try:
            replay_json = fetch_replay_json(self.url)
            log_text = replay_json["log"]
            result = import_replay(
                self.conn, log_text, self.url, self.usernames,
                store_logs=self.store_logs,
            )
            self.finished.emit(result)
        except Exception as e:
            self.error.emit(str(e))


def prompt_for_usernames(current_usernames=None):
    """
    Prompts for Showdown username(s), comma-separated. Used both on
    first launch (current_usernames=None, blank field) and later when
    changing the saved username(s) (current_usernames pre-fills the
    field so you're editing, not retyping from scratch). Returns a
    list of usernames, or None if cancelled -- callers should treat
    a cancelled first-run as "exit", not guess a default.
    """
    default_text = ", ".join(current_usernames) if current_usernames else ""
    title = "Fourslice" if current_usernames else "Welcome to Fourslice"
    text, ok = QInputDialog.getText(
        None, title,
        "Showdown username(s), comma-separated if you use more than one:",
        text=default_text,
    )
    if not ok or not text.strip():
        return None
    return [u.strip() for u in text.split(",") if u.strip()]


STATUS_MESSAGES = {
    "skipped_random_battle": lambda r: f"Skipped (Random Battle, not tracked): {r.get('format', '')}",
    "skipped_unsupported_battle_size": lambda r: f"Skipped (not singles/doubles -- {r.get('battle_size', '?')}): unsupported format",
    "empty_log": lambda r: "No data could be parsed from this replay.",
}

class ImportTab(QWidget):
    def __init__(self, usernames, conn):
        super().__init__()
        self.usernames = usernames
        self.conn = conn
        self.bulk_thread = None
        self.bulk_worker = None
        self.single_thread = None
        self.single_worker = None

        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(32, 28, 32, 28)
        main_layout.setSpacing(20)

        # -- Header Section --
        header_layout = QVBoxLayout()
        header_layout.setSpacing(4)

        self.title_label = QLabel("Import Replays")
        header_layout.addWidget(self.title_label)

        self.subtitle_label = QLabel("Sync and import your Pokémon Showdown battles to track your play statistics.")
        header_layout.addWidget(self.subtitle_label)
        main_layout.addLayout(header_layout)

        # -- Card 1: Account / Identity & Sync Card --
        self.identity_card = QFrame()
        self.identity_card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        identity_layout = QHBoxLayout(self.identity_card)
        identity_layout.setContentsMargins(16, 12, 16, 12)
        identity_layout.setSpacing(12)

        self.username_label = QLabel(f"Tracking replays for: {', '.join(usernames)}")
        identity_layout.addWidget(self.username_label, 1)

        # We'll use PillButtons instead of standard QPushButtons
        self.change_button = PillButton("Change Account", bg=TRACK, fg=BODY, height=36)
        self.change_button.clicked.connect(self.change_usernames)
        identity_layout.addWidget(self.change_button)

        self.bulk_import_button = PillButton("Sync Now", bg=VIOLET, fg=WHITE, height=36)
        self.bulk_import_button.clicked.connect(self.start_bulk_import)
        identity_layout.addWidget(self.bulk_import_button)

        main_layout.addWidget(self.identity_card)

        # -- Card 2: Single Replay Import --
        self.import_card = QFrame()
        self.import_card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        import_layout = QHBoxLayout(self.import_card)
        import_layout.setContentsMargins(16, 12, 16, 12)
        import_layout.setSpacing(12)

        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText("Paste a Showdown replay URL...")
        self.url_input.setFixedHeight(36)
        import_layout.addWidget(self.url_input, 1)

        self.import_button = PillButton("Import Replay", bg=INK, fg=WHITE, height=36)
        self.import_button.clicked.connect(self.import_one_replay)
        import_layout.addWidget(self.import_button)

        main_layout.addWidget(self.import_card)

        # -- Card 3: Recent Imports and Filter --
        self.recent_card = QFrame()
        self.recent_card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        recent_layout = QVBoxLayout(self.recent_card)
        recent_layout.setContentsMargins(20, 16, 20, 16)
        recent_layout.setSpacing(16)

        filter_row = QHBoxLayout()
        self.filter_label = QLabel("Show:")
        filter_row.addWidget(self.filter_label)

        self.regulation_filter = QComboBox()
        self.regulation_filter.addItem("All regulations")
        self.regulation_filter.currentTextChanged.connect(self.refresh_recent_imports)
        filter_row.addWidget(self.regulation_filter, 1)
        
        self.db_path_label = QLabel(f"Data stored at: {config.get_db_path()}")
        filter_row.addWidget(self.db_path_label)
        recent_layout.addLayout(filter_row)

        self.recent_list = QListWidget()
        recent_layout.addWidget(self.recent_list)

        # Progress elements
        self.bulk_progress = QProgressBar()
        self.bulk_progress.setVisible(False)
        self.bulk_progress.setFixedHeight(12)
        recent_layout.addWidget(self.bulk_progress)

        self.last_synced_label = QLabel(self._last_synced_text())
        recent_layout.addWidget(self.last_synced_label)

        main_layout.addWidget(self.recent_card, 1)

        # Apply theme-aware styling
        THEME.theme_changed.connect(self._apply_theme)
        self._apply_theme()

        self.refresh_regulation_filter()
        self.refresh_recent_imports()

    def _last_synced_text(self):
        timestamp = config.get_last_synced()
        if not timestamp:
            return "Last synced: never"
        local_time = datetime.fromisoformat(timestamp).astimezone()
        return f"Last synced: {local_time.strftime('%Y-%m-%d %H:%M')}"

    def change_usernames(self):
        new_usernames = prompt_for_usernames(current_usernames=self.usernames)
        if new_usernames:
            self.usernames = new_usernames
            config.set_usernames(new_usernames)
            self.username_label.setText(f"Tracking replays for: {', '.join(self.usernames)}")

    def import_one_replay(self):
        url = self.url_input.text().strip()
        if not url:
            self.recent_list.insertItem(0, "Enter a replay URL first.")
            return

        self.import_button.setEnabled(False)
        self.recent_list.insertItem(0, f"Importing {url}...")

        worker = SingleImportWorker(url, self.conn, self.usernames, config.get_store_logs())
        thread = QThread()
        worker.moveToThread(thread)
        # Keep the thread/worker alive for their whole lifecycle, like the
        # bulk import does. Without a reference here, import_one_replay's
        # locals would drop the still-running QThread right as the function
        # returns -- destroying a running QThread is a fatal Qt abort, and
        # it would fire before the queued finished/error could ever arrive.
        self.single_thread = thread
        self.single_worker = worker
        thread.started.connect(worker.run)
        worker.finished.connect(self.on_single_import_finished)
        worker.error.connect(self.on_single_import_error)
        # Both terminal signals quit the thread and schedule the worker's
        # deletion -- an *error* must tear the thread down exactly like
        # success does, otherwise the thread lives on forever and destroying
        # it while it still runs is a fatal Qt abort.
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        worker.error.connect(thread.quit)
        worker.error.connect(worker.deleteLater)
        thread.finished.connect(self._on_single_thread_finished)
        thread.finished.connect(thread.deleteLater)
        thread.start()

    def on_single_import_finished(self, result):
        self.import_button.setEnabled(True)
        if result["status"] == "imported":
            self.url_input.clear()
            self.refresh_regulation_filter()
            self.refresh_recent_imports()
        else:
            message_fn = STATUS_MESSAGES.get(result["status"])
            self.recent_list.insertItem(0, message_fn(result) if message_fn else str(result))

    def on_single_import_error(self, error_msg):
        self.import_button.setEnabled(True)
        url = self.url_input.text().strip()
        self.recent_list.insertItem(0, f"Failed to import {url}: {error_msg}")

    def _on_single_thread_finished(self):
        """Drop references to the single-import thread/worker once the thread
        fully exits (mirrors the bulk-import cleanup)."""
        self.single_thread = None
        self.single_worker = None

    def start_bulk_import(self):
        """
        Scrapes and imports every saved replay for every configured
        username, back to back -- no separate prompt, since the app
        already knows who you are. Called once automatically right
        after the window is shown (see main.py) and again any time
        "Sync Now" is clicked.

        Each account's history is scanned newest-first and stops the
        moment it hits an already-imported replay -- true of both the
        network fetch (find_all_replay_urls_for_user's stop_when_seen)
        and the parse/store step (_import_urls), so a repeat run is
        cheap on request count too, not just on parsing work.
        """
        # Re-entry guard: don't start a second bulk import if one is already running
        if self.bulk_thread is not None:
            return

        self.import_button.setEnabled(False)
        self.bulk_import_button.setEnabled(False)
        self.bulk_progress.setVisible(True)
        self.bulk_progress.setValue(0)
        self.recent_list.insertItem(0, f"Starting bulk import for: {', '.join(self.usernames)}...")

        self.bulk_thread = QThread()
        self.bulk_worker = BulkImportWorker(str(config.get_db_path()), self.usernames, self.usernames)
        self.bulk_worker.moveToThread(self.bulk_thread)

        self.bulk_thread.started.connect(self.bulk_worker.run)
        self.bulk_worker.progress.connect(self.on_bulk_progress)
        self.bulk_worker.finished.connect(self.on_bulk_finished)
        self.bulk_worker.finished.connect(self.bulk_thread.quit)
        self.bulk_worker.finished.connect(self.bulk_worker.deleteLater)
        self.bulk_thread.finished.connect(self.bulk_thread.deleteLater)
        self.bulk_thread.finished.connect(self._on_bulk_thread_finished)

        self.bulk_thread.start()

    def _on_bulk_thread_finished(self):
        """Drop references to the bulk thread/worker after the thread fully exits."""
        self.bulk_thread = None
        self.bulk_worker = None

    def on_bulk_progress(self, done, total, message):
        self.bulk_progress.setMaximum(total if total > 0 else 1)
        self.bulk_progress.setValue(done)
        self.recent_list.insertItem(0, message)

    def on_bulk_finished(self, summary):
        self.import_button.setEnabled(True)
        self.bulk_import_button.setEnabled(True)
        self.bulk_progress.setVisible(False)

        imported = summary.get("imported", 0)
        if imported:
            self.recent_list.insertItem(0, f"Synced -- {imported} new replay(s) imported.")
        else:
            self.recent_list.insertItem(0, "Synced -- no new replays.")

        config.set_last_synced(datetime.now(timezone.utc).isoformat(timespec="seconds"))
        self.last_synced_label.setText(self._last_synced_text())

        self.refresh_regulation_filter()
        self.refresh_recent_imports()

    def _apply_theme(self, *_args):
        tokens = THEME.get_tokens()

        self.setStyleSheet(f"background-color: {tokens['WHITE']};")
        self.title_label.setStyleSheet(f"color: {tokens['INK']}; font-family: {FONT_STACK}; font-size: 24px; font-weight: 700;")
        self.subtitle_label.setStyleSheet(f"color: {tokens['MUTED']}; font-family: {FONT_STACK}; font-size: 14px;")

        card_qss = f"""
            QFrame {{
                background-color: {tokens['WHITE']};
                border: 1px solid {tokens['BORDER']};
                border-radius: 12px;
            }}
        """
        self.identity_card.setStyleSheet(card_qss)
        self.import_card.setStyleSheet(card_qss)
        self.recent_card.setStyleSheet(card_qss)

        self.username_label.setStyleSheet(f"color: {tokens['BODY']}; font-family: {FONT_STACK}; font-size: 14px; font-weight: 500;")
        self.db_path_label.setStyleSheet(f"color: {tokens['MUTED']}; font-family: {FONT_STACK}; font-size: 11px;")
        self.filter_label.setStyleSheet(f"color: {tokens['MUTED']}; font-family: {FONT_STACK}; font-size: 12px; font-weight: 600;")
        self.last_synced_label.setStyleSheet(f"color: {tokens['MUTED']}; font-family: {FONT_STACK}; font-size: 12px;")

        # Style line edit
        self.url_input.setStyleSheet(f"""
            QLineEdit {{
                background-color: {tokens['WHITE']};
                border: 1px solid {tokens['BORDER']};
                border-radius: 8px;
                padding: 6px 12px;
                color: {tokens['INK']};
                font-family: {FONT_STACK};
                font-size: 13px;
            }}
            QLineEdit:focus {{
                border-color: {tokens['VIOLET']};
            }}
        """)
        set_placeholder_color(self.url_input, tokens['PLACEHOLDER'])

        # Style combobox
        self.regulation_filter.setStyleSheet(f"""
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
        """)

        # Style list widget
        self.recent_list.setStyleSheet(f"""
            QListWidget {{
                background-color: {tokens['WHITE']};
                color: {tokens['INK']};
                border: 1px solid {tokens['BORDER']};
                border-radius: 8px;
                padding: 4px;
                font-family: {FONT_STACK};
                font-size: 13px;
                outline: none;
            }}
            QListWidget::item {{
                padding: 10px 14px;
                border-bottom: 1px solid {tokens['TRACK']};
            }}
            QListWidget::item:selected {{
                background-color: {tokens['PALE_VIOLET']};
                color: {tokens['VIOLET']};
                border-radius: 4px;
            }}
        """)

        # Style progress bar
        self.bulk_progress.setStyleSheet(f"""
            QProgressBar {{
                border: 1px solid {tokens['BORDER']};
                border-radius: 6px;
                background-color: {tokens['TRACK']};
                text-align: center;
                color: {tokens['INK']};
                font-family: {FONT_STACK};
                font-size: 11px;
            }}
            QProgressBar::chunk {{
                background-color: {tokens['VIOLET']};
                border-radius: 5px;
            }}
        """)

        # Update button stylesheets manually since PillButton has static stylesheet in constructor
        from fourslice.gui.imports_page import _pill_qss
        self.change_button.setStyleSheet(_pill_qss("PillButton", tokens['TRACK'], tokens['BODY'], 36))
        self.bulk_import_button.setStyleSheet(_pill_qss("PillButton", tokens['VIOLET'], tokens['WHITE'], 36))
        self.import_button.setStyleSheet(_pill_qss("PillButton", tokens['INK'], tokens['WHITE'], 36))

    def refresh_regulation_filter(self):
        rows = self.conn.execute(
            "SELECT DISTINCT regulation FROM games WHERE regulation IS NOT NULL ORDER BY regulation"
        ).fetchall()

        previous_selection = self.regulation_filter.currentText()
        self.regulation_filter.blockSignals(True)
        self.regulation_filter.clear()
        self.regulation_filter.addItem("All regulations")
        for (regulation,) in rows:
            self.regulation_filter.addItem(regulation)
        restored_index = self.regulation_filter.findText(previous_selection)
        self.regulation_filter.setCurrentIndex(restored_index if restored_index >= 0 else 0)
        self.regulation_filter.blockSignals(False)

    def refresh_recent_imports(self, *_signal_args, limit=10):
        self.recent_list.clear()

        selected_regulation = self.regulation_filter.currentText()
        if selected_regulation and selected_regulation != "All regulations":
            rows = self.conn.execute(
                "SELECT p1_name, p2_name, winner, result, regulation, battle_size FROM games "
                "WHERE regulation = ? ORDER BY imported_at DESC LIMIT ?",
                (selected_regulation, limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT p1_name, p2_name, winner, result, regulation, battle_size FROM games "
                "ORDER BY imported_at DESC LIMIT ?",
                (limit,),
            ).fetchall()

        if not rows:
            self.recent_list.addItem("No replays match this filter yet.")
            return

        for p1_name, p2_name, winner, result, regulation, battle_size in rows:
            result_text = result if result else "? (side not resolved)"
            self.recent_list.addItem(
                f"[{regulation} {battle_size}] {p1_name} vs {p2_name} -- winner: {winner} -- {result_text}"
            )