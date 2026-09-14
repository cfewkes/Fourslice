"""
fourslice/gui/bulk_import_worker.py

Runs on a background QThread so scraping+importing potentially dozens
or hundreds of replays doesn't freeze the window.

Takes a LIST of usernames (your configured identity, typically) and
scrapes each one's full replay history in turn -- newest first,
stopping the moment it hits a replay already in the database, since
Showdown's search results come back reverse-chronologically. That
means a repeat run only ever processes what's actually new.

Opens its OWN database connection rather than sharing the main
window's -- sqlite3 connections aren't safe to use across threads.
"""

from PySide6.QtCore import QObject, Signal

from fourslice import config
from fourslice.parser import find_all_replay_urls_for_user, fetch_replay_json, game_id_from_url
from fourslice.storage import init_db, get_known_game_ids, import_replay


class BulkImportWorker(QObject):
    progress = Signal(int, int, str)  # (done_count, total_count, latest_status_message)
    finished = Signal(dict)           # summary counts by outcome, across all usernames

    def __init__(self, db_path: str, usernames, my_usernames):
        super().__init__()
        self.db_path = db_path
        self.usernames = usernames
        self.my_usernames = my_usernames

    def run(self):
        conn = init_db(self.db_path)
        summary = {}
        known_ids = get_known_game_ids(conn)

        for username in self.usernames:
            try:
                urls = find_all_replay_urls_for_user(username, stop_when_seen=known_ids)
            except Exception as e:
                self.progress.emit(0, 0, f"Could not fetch replay list for {username}: {e}")
                summary["fetch_list_error"] = summary.get("fetch_list_error", 0) + 1
                continue

            self._import_urls(conn, username, urls, known_ids, summary)

        conn.close()
        self.finished.emit(summary)

    def _import_urls(self, conn, username, urls, known_ids, summary):
        """
        Processes one username's URL list (already newest-first).
        Split out from run() so this can be tested directly with a
        hand-built url list -- no network access needed to prove the
        stop-early behavior works.

        Stops at the first game_id that was ALREADY in known_ids before
        this call started -- not one added during this same call.
        Without that distinction, a duplicate URL within one page-crawl
        (Showdown's pagination isn't a stable snapshot against a live,
        growing replay list -- one new upload mid-crawl can shift page
        boundaries and repeat an entry) looks identical to "reached the
        old, already-known history" and stops the whole import early,
        even on a first-ever run against an empty database.
        """
        known_before_this_call = set(known_ids)
        total = len(urls)
        for i, url in enumerate(urls, start=1):
            game_id = game_id_from_url(url)
            if game_id in known_before_this_call:
                self.progress.emit(i, total, f"{username}: reached already-imported replay, stopping here")
                return
            if game_id in known_ids:
                continue  # duplicate URL within this same crawl (pagination overlap) -- already processed, don't stop for it

            try:
                replay_json = fetch_replay_json(url)
                result = import_replay(
                    conn, replay_json["log"], url, self.my_usernames,
                    store_logs=config.get_store_logs(),
                )
                status = result["status"]
                if status == "imported":
                    known_ids.add(game_id)
            except Exception as e:
                status = "error"
                self.progress.emit(i, total, f"{username} [{i}/{total}] error on {url}: {e}")
            else:
                self.progress.emit(i, total, f"{username} [{i}/{total}] {status}: {url}")

            summary[status] = summary.get(status, 0) + 1