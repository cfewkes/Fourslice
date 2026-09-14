# Fourslice

A native Python app for analyzing Pokemon Showdown VGC doubles replays --
turns on field, ally/foe co-occurrence, KO credit, and win/loss-filterable
move usage, all computed from one shared events table instead of separate
tracked stats.

This is where that *new* analytics work is being built going forward,
instead of adding it to the Google Sheet. The existing PASRS 7.0 sheet
(`REPLAYTODATA`, `BUILDGAMEEVENTS`, Usage/Matchup Stats/Move Usage tabs)
keeps working exactly as it does now -- it's unaffected and isn't part
of this project.

Built with public release in mind: nothing here assumes it's running on
any one specific person's machine or under any one specific username --
see "Where your data lives" below.

## Status

- Done -- `fourslice/parser.py`: parses a raw Showdown replay log into
  turn/move/faint events with full 4-slot board state and KO credit.
  Validated against three real replays (see `tests/`).
- Done -- `fourslice/storage.py`: stores parsed games in SQLite; resolves
  win/loss and side (p1/p2) from whatever usernames the caller passes in
  -- no username is hardcoded anywhere in this module. Also tracks teams
  (auto-detected from team previews, deduped by 6-species roster) and
  exact-duplicate loadouts (species + item + moves).
- Done -- `fourslice/config.py`: reads/writes settings (currently just
  usernames) and resolves an OS-appropriate folder for the app's data,
  instead of anything living inside the project folder itself.
- Done -- auto-pulling replays by username. Sync scrapes each configured
  account's replay history newest-first and stops at the first
  already-imported replay, so repeat runs are cheap. Runs once on launch
  and again any time "Sync Now" is clicked.
- Done -- stats as reusable functions (`fourslice/stats/`): turns on
  field, ally/foe co-occurrence, KO credit, and win/loss-filterable move
  usage -- all computed via a shared registry from the one events table,
  each with a matplotlib renderer, and bar-chart stats also support a
  "win average minus loss average" diff view.
- Done -- GUI (`fourslice/gui/`): a main window with a left sidebar and a
  stacked set of pages. Imports page (the primary view: single-replay
  import, background "Sync Now" bulk sync, last-synced timestamp, Showdown
  vs Champions mode, regulation-filterable recent imports), Stats tab
  (chart picker driven by the registry, regulation/team/mon/side/result
  filters, live chart rendering), Teams tab (auto-detected teams with
  editable nicknames), and Replays tab (native turn-by-turn playback of
  your stored battles -- board, HP bars, turn narrative, play/step/speed
  controls).
- Done -- gen-5 sprite replay board (`fourslice/sprites.py` +
  `gui/sprite_fetcher.py`): the Replays tab shows Pokemon Showdown's
  gen-5 sprite art (back-view for your side, front-view for the
  opponent's). Sprites are fetched from play.pokemonshowdown.com on a
  background thread, cached on disk under the app-data folder, and fall
  back to a plain name label when offline or when a Pokemon has no
  gen-5 art (e.g. Sneasler). Toggle off in Settings to stay fully
  offline.
- Done -- packaging via PyInstaller (`fourslice.spec` +
  `build_release.py`): builds a standalone windowed `Fourslice.exe`,
  prunes Qt binaries the app can't reach (QML/Quick/Pdf/VirtualKeyboard),
  verifies every required data asset is bundled, then zips a Windows
  release (`dist/Fourslice-v<version>-windows-x64.zip`) ready for
  itch.io. Only the data files the app actually reads are shipped -- the
  developer's local Smogon crawl mirror is never bundled, and the verifier
  aborts the build if the crawl mirror, stats cache, or dead Qt binaries
  ever creep back in.

## Setup

1. Install Python 3.10+.
2. Open a terminal in this folder and create a virtual environment:
   ```
   python -m venv .venv
   ```
3. Activate it:
   - Windows: `.venv\Scripts\activate`
   - Mac/Linux: `source .venv/bin/activate`
4. Install dependencies:
   ```
   pip install -r requirements.txt
   ```
5. Confirm everything works:
   ```
   python tests/test_parser.py
   python tests/test_config.py
   python tests/test_stats.py
   python tests/test_sprites.py
   python tests/test_gui_smoke.py
   ```
   Each should print `PASS` lines and end with an "All ... passed." message.
   Or run the whole suite once with `pytest tests/`.
6. Launch the app:
   ```
   python main.py
   ```
   First time, it'll ask for your Showdown username(s) -- comma-separated
   if you play under more than one. After that it remembers.

## Project layout

```
fourslice/
├── main.py                    entry point -- launches the GUI
├── build_release.py           PyInstaller release build + zip for itch.io
├── fourslice.spec             PyInstaller spec (bundles only the data the app reads)
├── requirements.txt
├── fourslice/
│   ├── parser.py               raw replay log -> event rows
│   ├── storage.py              event rows -> SQLite, win/loss resolution, team/loadout tracking
│   ├── config.py                settings + data-folder locations
│   ├── replay_html.py          embed replays in a local HTML page
│   ├── sprites.py              gen-5 sprite URLs + on-disk cache (fallback chain, no Qt)
│   ├── learnsets.py            per-generation move legality (learnsets.db)
│   ├── teambuilder_data.py     Pokemon/no-team catalogue (pokemon_complete.db)
│   ├── stats/                  stat registry + per-stat compute/renderers
│   ├── extstats/               Smogon stats pipeline (crawl -> parse -> stats.db)
│   ├── scraping/               offline-testable crawlers (Smogon)
│   └── gui/
│       ├── main_window.py      assembles the four tabs + sidebar
│       ├── import_tab.py       single import + Sync Now + recent imports
│       ├── stats_tab.py        chart picker + filters + live charts + matchups
│       ├── teams_tab.py        auto-detected teams with editable nicknames
│       ├── replays_tab.py      native turn-by-turn replay player with gen-5 sprites
│       ├── startup.py          first-run splash + bounded DB bootstrap
│       ├── bulk_import_worker.py  background QThread for bulk sync
│       └── sprite_fetcher.py   background QThread for sprite downloads
└── tests/
    ├── sample_logs/            three real replays (1 win, 1 loss, 1 poke-only-no-showteam)
    ├── test_parser.py          parser + storage + team resolution (run after parser/storage changes)
    ├── test_config.py          config read/write (run after config.py changes)
    ├── test_stats.py           stats registry + hand-verified stat values/renderers
    ├── test_sprites.py         sprite filename resolution + cache/fallback chain (no network)
    └── test_gui_smoke.py       headless check that the main window builds and charts render
```

## Where your data lives

Nothing personal is stored in this folder or in source code. On first
launch, Fourslice creates a config file and a SQLite database in your
OS's standard per-app data location (via `platformdirs`) -- typically
something like `%LOCALAPPDATA%\Fourslice` on Windows or
`~/.local/share/Fourslice` on Linux. `fourslice/config.get_db_path()`
and `get_config_path()` will tell you the exact path if you ever need
to find it (DB Browser for SQLite can open the database directly).

Gen-5 sprites downloaded by the Replays tab live in a `sprites/`
subfolder of the same app-data directory. Each sprite is fetched at
most once and reused from disk on later launches; anything the
Pokemon Showdown server doesn't have (or that can't be reached, e.g.
offline) is skipped and the board just shows the Pokemon's name.
