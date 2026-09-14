from PySide6.QtWidgets import QHBoxLayout, QStackedWidget, QWidget
from fourslice.gui.sidebar import Sidebar
from fourslice.gui.imports_page import THEME

class AppLayout(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.lay = QHBoxLayout(self)
        self.lay.setContentsMargins(0, 0, 0, 0)
        self.lay.setSpacing(0)

        self.sidebar = Sidebar(THEME)
        self.pages = QStackedWidget()

        self.lay.addWidget(self.sidebar)
        self.lay.addWidget(self.pages)

        self.sidebar.nav_clicked.connect(self._on_nav_clicked)

    def add_page(self, widget, key):
        self.pages.addWidget(widget)
        # Store key to find index later if needed, or just rely on index order
        widget.setProperty("page_key", key)

    def _on_nav_clicked(self, key):
        for i in range(self.pages.count()):
            page = self.pages.widget(i)
            if page.property("page_key") == key:
                self.pages.setCurrentIndex(i)
                break

    def count(self):
        return self.pages.count()

    def tabText(self, index):
        titles = {
            "imports": "Import",
            "stats": "Stats",
            "teams": "Teams",
            "replays": "Replays",
        }
        if 0 <= index < self.pages.count():
            key = self.pages.widget(index).property("page_key")
            return titles.get(key, "")
        return ""
