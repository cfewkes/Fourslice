"""
fourslice/gui/main_window.py

Top-level window with the redesigned Imports screen as the main view.
"""

from PySide6.QtWidgets import QMainWindow
from PySide6.QtGui import QIcon
from fourslice.gui.app_layout import AppLayout
from fourslice.gui.imports_page import ImportsPage, THEME, ASSETS_DIR
from fourslice.gui.stats_tab import StatsTab
from fourslice.gui.teams_tab import TeamsTab
from fourslice.gui.replays_tab import ReplaysTab
from fourslice.gui.settings_dialog import SettingsDialog

class MainWindow(QMainWindow):
    def __init__(self, usernames, conn):
        super().__init__()
        self.setWindowTitle("Fourslice")
        self.resize(1100, 760)

        # Set application icon if available
        ico = ASSETS_DIR / "FourSliceLogo.ico"
        png = ASSETS_DIR / "FourSliceLogo.png"
        icon_path = ico if ico.is_file() else (png if png.is_file() else None)
        if icon_path:
            self.setWindowIcon(QIcon(str(icon_path)))

        self.conn = conn

        self.layout_widget = AppLayout()
        self.setCentralWidget(self.layout_widget)

        # Wire sidebar Settings entry to open the settings dialog
        self.layout_widget.sidebar.settings_clicked.connect(self.open_settings)

        self.imports_page = ImportsPage(theme_manager=THEME, usernames=usernames, conn=conn)
        self.import_tab = self.imports_page
        self.stats_tab = StatsTab(conn)
        self.teams_tab = TeamsTab(conn)
        self.replays_tab = ReplaysTab(conn)

        self.layout_widget.add_page(self.imports_page, "imports")
        self.layout_widget.add_page(self.stats_tab, "stats")
        self.layout_widget.add_page(self.teams_tab, "teams")
        self.layout_widget.add_page(self.replays_tab, "replays")

    def open_settings(self):
        dialog = SettingsDialog(self.conn, parent=self)
        dialog.settings_applied.connect(self.replays_tab.on_settings_changed)
        dialog.exec()
