"""
storage.py

SQLite storage for parsed replay data. Deliberately minimal schema:
one row of identity per game, one row per turn/move/faint event.
Everything else -- move usage, turns on field, co-occurrence, KO
credit, win rates -- is a query against `events`, not a separate
tracked table. Nothing to keep in sync as new stats get added later.
"""

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config
from .parser import parse_game_events, extract_regulation, is_random_battle, detect_battle_size, parse_team_preview, is_mega_stone, game_id_sort_key

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS teams (
    team_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    nickname          TEXT NOT NULL,
    roster_key        TEXT NOT NULL UNIQUE,  -- sorted species list, for dedup matching
    created_at        TEXT NOT NULL,
    naming_pokemon     TEXT,   -- the species used when the nickname was auto-generated
    naming_regulation  TEXT    -- the (cleaned) regulation used when the nickname was auto-generated
);

CREATE TABLE IF NOT EXISTS team_pokemon (
    team_id      INTEGER NOT NULL REFERENCES teams(team_id),
    slot_order   INTEGER NOT NULL,      -- 0-5, team-preview order -- slot 0 names the default nickname
    species      TEXT NOT NULL,
    item         TEXT,                  -- held item from |showteam|; used to detect a Mega Stone for nicknaming
    moves        TEXT,                  -- comma-separated assumed moveset, from |showteam| (all 4, not just used ones)
    PRIMARY KEY (team_id, slot_order)
);

CREATE TABLE IF NOT EXISTS team_loadouts (
    loadout_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    team_id      INTEGER NOT NULL REFERENCES teams(team_id),
    loadout_key  TEXT NOT NULL,        -- item+moves for all 6, in a stable order -- exact-duplicate detection
    first_seen   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS team_loadout_pokemon (
    loadout_id   INTEGER NOT NULL REFERENCES team_loadouts(loadout_id),
    slot_order   INTEGER NOT NULL,
    species      TEXT NOT NULL,
    item         TEXT,
    moves        TEXT,
    PRIMARY KEY (loadout_id, slot_order)
);

CREATE TABLE IF NOT EXISTS games (
    game_id      TEXT PRIMARY KEY,
    replay_url   TEXT NOT NULL,
    p1_name      TEXT,
    p2_name      TEXT,
    winner       TEXT,
    my_side      TEXT,
    result       TEXT,
    format       TEXT,
    regulation   TEXT,
    battle_size  TEXT,
    imported_at  TEXT
);

CREATE TABLE IF NOT EXISTS logs (
    game_id     TEXT PRIMARY KEY REFERENCES games(game_id),
    log_text    TEXT NOT NULL,
    stored_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    event_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id     TEXT NOT NULL REFERENCES games(game_id),
    turn        INTEGER,
    side        TEXT,
    slot        TEXT,
    mon         TEXT,
    event_type  TEXT,
    move_name   TEXT,
    p1a TEXT, p1b TEXT, p2a TEXT, p2b TEXT
);

CREATE TABLE IF NOT EXISTS opponent_teams (
    game_id     TEXT NOT NULL REFERENCES games(game_id),
    slot_order  INTEGER NOT NULL,      -- 0-5, team-preview order
    species     TEXT NOT NULL,
    item        TEXT,
    moves       TEXT,
    PRIMARY KEY (game_id, slot_order)
);

CREATE INDEX IF NOT EXISTS idx_events_game ON events(game_id);
CREATE INDEX IF NOT EXISTS idx_events_mon  ON events(mon);
CREATE INDEX IF NOT EXISTS idx_logs_stored_at ON logs(stored_at);

-- User-built teams from the Team Builder (Teams tab). Deliberately kept
-- separate from the auto-detected replay `teams` table: these are
-- manually created/built teams, owned by the teambuilder UI.
CREATE TABLE IF NOT EXISTS tb_teams (
    team_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    format     TEXT NOT NULL DEFAULT '',
    data       TEXT NOT NULL,          -- full team JSON (slots, nickname, ...)
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def init_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    _ensure_column(conn, "games", "team_id", "INTEGER REFERENCES teams(team_id)")
    _ensure_column(conn, "team_pokemon", "item", "TEXT")
    _ensure_column(conn, "teams", "naming_pokemon", "TEXT")
    _ensure_column(conn, "teams", "naming_regulation", "TEXT")
    _ensure_column(conn, "events", "ko_credit_mon", "TEXT")
    _ensure_column(conn, "events", "ko_credit_side", "TEXT")
    _ensure_column(conn, "events", "hp1a", "TEXT")
    _ensure_column(conn, "events", "hp1b", "TEXT")
    _ensure_column(conn, "events", "hp2a", "TEXT")
    _ensure_column(conn, "events", "hp2b", "TEXT")
    return conn


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, column_def: str) -> None:
    """
    Adds a column to an already-existing table if it's not there yet.
    Idempotent, portable schema migration -- ALTER TABLE has no
    universal IF NOT EXISTS across SQLite versions, so this checks via
    PRAGMA table_info first instead of relying on one. Runs on every
    init_db call; a no-op once the column exists, same as the
    CREATE TABLE IF NOT EXISTS statements above.
    """
    existing_columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in existing_columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_def}")
        conn.commit()

def get_known_game_ids(conn: sqlite3.Connection) -> set[str]:
    """All game_ids already stored -- used to detect already-imported
    replays without a per-replay fetch or query."""
    return {row[0] for row in conn.execute("SELECT game_id FROM games").fetchall()}

def resolve_side_and_result(p1_name: str, p2_name: str, winner: str, my_usernames):
    """my_usernames: any collection of Showdown usernames that count as "me" --
    typically config.get_usernames(), but passed in explicitly so this function
    has no hidden assumptions about who's using it."""
    if p1_name in my_usernames:
        my_side = "p1"
    elif p2_name in my_usernames:
        my_side = "p2"
    else:
        my_side = None  # neither name matched -- don't guess, flag it

    if my_side is None:
        return None, None

    my_name = p1_name if my_side == "p1" else p2_name
    result = "W" if winner == my_name else "L"
    return my_side, result

def _pick_naming_pokemon(roster: list[dict]) -> str:
    """
    First Mega-eligible member's species (see parser.is_mega_stone),
    since that's usually the team's defining Pokemon; falls back to
    roster[0]'s species if nothing in the roster holds a Mega Stone.
    """
    for mon in roster:
        if is_mega_stone(mon.get("item", ""), mon["species"]):
            return mon["species"]
    return roster[0]["species"]


def _clean_regulation_for_naming(regulation: str) -> str:
    """
    Strips a leading "[Gen N] " prefix for naming purposes only --
    doesn't touch games.regulation itself, which stays as-is
    everywhere else (filters, display). VGC-style regulations
    (already just a letter or letter-dash, e.g. "G", "M-B") pass
    through unchanged; non-VGC tiers like "[Gen 9] OU" become "OU",
    since extract_regulation falls back to the full raw format string
    for anything without a "Reg X" pattern to pull out.
    """
    if regulation.startswith("[Gen") and "]" in regulation:
        return regulation.split("]", 1)[1].strip()
    return regulation


def _generate_team_name(conn: sqlite3.Connection, roster: list[dict], regulation: str | None) -> tuple[str, str, str]:
    """
    Returns (nickname, naming_pokemon, naming_regulation) for a
    brand-new team: "Pokemon-Regulation-Iteration", e.g.
    "Goodra-OU-1", then "Goodra-OU-2" for a second, distinct
    Goodra-led team also first seen in OU.

    naming_pokemon/naming_regulation get stored on the team row
    separately from the user-editable nickname, purely so a later
    manual rename doesn't throw off the iteration count for the NEXT
    new team sharing that same pokemon+regulation -- the count is
    queried against these stable columns, never against nickname text.
    """
    naming_pokemon = _pick_naming_pokemon(roster)
    naming_regulation = _clean_regulation_for_naming(regulation) if regulation else "Unknown"

    existing_count = conn.execute(
        "SELECT COUNT(*) FROM teams WHERE naming_pokemon = ? AND naming_regulation = ?",
        (naming_pokemon, naming_regulation),
    ).fetchone()[0]
    iteration = existing_count + 1

    nickname = f"{naming_pokemon}-{naming_regulation}-{iteration}"
    return nickname, naming_pokemon, naming_regulation


def _loadout_key(roster: list[dict]) -> str:
    """
    Canonical string for a roster's full loadout (species + item +
    moves, for all 6) -- used to detect an exact-duplicate loadout.
    Sorted by species so this is stable regardless of team-preview
    slot order; each mon's own moves are sorted too, in case the same
    4 moves ever show up in a different order across games.
    """
    parts = []
    for mon in sorted(roster, key=lambda m: m["species"]):
        moves_key = ",".join(sorted(mon["moves"]))
        parts.append(f"{mon['species']}|{mon.get('item', '')}|{moves_key}")
    return "||".join(parts)


def record_team_loadout(conn: sqlite3.Connection, team_id: int, roster: list[dict]) -> None:
    """
    Appends a new team_loadouts row (+ its team_loadout_pokemon rows)
    the first time this EXACT combination of species, item, and moves
    is seen for this team. Never overwrites or updates an existing
    loadout -- an exact repeat (via _loadout_key) is a no-op, and
    anything genuinely different (even a single move or item) becomes
    a new row, preserving the full history rather than collapsing it
    down to "whatever's current."
    """
    loadout_key = _loadout_key(roster)
    existing = conn.execute(
        "SELECT loadout_id FROM team_loadouts WHERE team_id = ? AND loadout_key = ?",
        (team_id, loadout_key),
    ).fetchone()
    if existing:
        return

    cursor = conn.execute(
        "INSERT INTO team_loadouts (team_id, loadout_key, first_seen) VALUES (?, ?, ?)",
        (team_id, loadout_key, datetime.now(timezone.utc).isoformat()),
    )
    loadout_id = cursor.lastrowid
    conn.executemany(
        "INSERT INTO team_loadout_pokemon (loadout_id, slot_order, species, item, moves) VALUES (?, ?, ?, ?, ?)",
        [(loadout_id, i, mon["species"], mon.get("item", ""), ",".join(mon["moves"])) for i, mon in enumerate(roster)],
    )


def resolve_team_id(conn: sqlite3.Connection, roster: list[dict], regulation: str | None = None) -> int | None:
    """
    Given my_side's team-preview roster (list of {"species", "item",
    "moves"} in preview order, from parser.parse_team_preview),
    returns the matching teams.team_id -- creating a new team (+
    team_pokemon rows, capturing this first-seen loadout) the first
    time this exact 6-species set is seen. Returns None if roster is
    empty (no |showteam| data in this log).

    Matching is by the SET of species, not order: same 6, any order,
    counts as the same team. A move or item tweak alone, with the
    same 6 species, is NOT a new team -- roster_key never looks at
    moves or items, only species.

    A brand-new team's nickname is auto-generated (see
    _generate_team_name) from the regulation this team was FIRST
    seen in -- pass it here so a new team gets it; existing matches
    ignore it entirely, since the nickname is only ever set once.

    Every call also records this roster's full loadout via
    record_team_loadout -- nothing here ever overwrites; a genuinely
    new loadout for an existing team is appended as one more row, an
    exact repeat is a no-op.
    """
    if not roster:
        return None

    species_list = [mon["species"] for mon in roster]
    roster_key = ",".join(sorted(species_list))

    existing = conn.execute("SELECT team_id FROM teams WHERE roster_key = ?", (roster_key,)).fetchone()
    if existing:
        team_id = existing[0]
        record_team_loadout(conn, team_id, roster)
        return team_id

    nickname, naming_pokemon, naming_regulation = _generate_team_name(conn, roster, regulation)
    cursor = conn.execute(
        "INSERT INTO teams (nickname, roster_key, created_at, naming_pokemon, naming_regulation) VALUES (?, ?, ?, ?, ?)",
        (nickname, roster_key, datetime.now(timezone.utc).isoformat(), naming_pokemon, naming_regulation),
    )
    team_id = cursor.lastrowid
    conn.executemany(
        "INSERT INTO team_pokemon (team_id, slot_order, species, item, moves) VALUES (?, ?, ?, ?, ?)",
        [(team_id, i, mon["species"], mon.get("item", ""), ",".join(mon["moves"])) for i, mon in enumerate(roster)],
    )
    record_team_loadout(conn, team_id, roster)
    return team_id

def get_teams_by_recency(conn: sqlite3.Connection, regulation: str | None = None) -> list[tuple[int, str]]:
    """
    Returns (team_id, nickname) for every team, most-recently-active
    first -- or, if `regulation` is given, only the teams that have
    at least one game IN that regulation, ranked by recency within
    that regulation alone (not their most recent game overall). A
    team with games in OTHER regulations but none in this one doesn't
    appear at all -- this drives the Stats tab's Team filter, and a
    team that's never been played under the selected regulation isn't
    a real option there.

    "Recently active" = the highest game_id_sort_key among the
    relevant games -- deliberately NOT teams.created_at.

    created_at is stamped at whatever wall-clock moment a team's row
    got INSERTed locally. For anything that came through
    backfill_teams.py or a bulk import, that's the order that scan
    happened to reach the team in (both walk newest-real-game-first),
    not the true order the team was actually first played in -- see
    parser.game_id_sort_key's docstring for the full explanation.
    Reading recency off each game's own replay ID instead sidesteps
    that, and as a bonus means picking an old team back up correctly
    bubbles it back to the top, since created_at never changes once a
    team's row exists but this does.

    Teams with zero games (shouldn't normally happen -- a team row is
    only ever created alongside a game, in resolve_team_id above)
    sort last rather than raising -- only relevant when regulation is
    None; with a regulation given, a team with zero QUALIFYING games
    simply isn't in the result at all, not sorted-last-with-zero.
    """
    if regulation:
        rows = conn.execute(
            "SELECT t.team_id, t.nickname, g.game_id "
            "FROM teams t JOIN games g ON g.team_id = t.team_id "
            "WHERE g.regulation = ?",
            (regulation,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT t.team_id, t.nickname, g.game_id "
            "FROM teams t LEFT JOIN games g ON g.team_id = t.team_id"
        ).fetchall()

    nickname_by_team: dict[int, str] = {}
    best_key_by_team: dict[int, int] = {}
    for team_id, nickname, game_id in rows:
        nickname_by_team[team_id] = nickname
        key = game_id_sort_key(game_id) if game_id else -1
        if key > best_key_by_team.get(team_id, -1):
            best_key_by_team[team_id] = key

    ordered_ids = sorted(nickname_by_team, key=lambda tid: best_key_by_team.get(tid, -1), reverse=True)
    return [(team_id, nickname_by_team[team_id]) for team_id in ordered_ids]


def get_mons_for_filter(conn: sqlite3.Connection, team_id: int | None = None, regulation: str | None = None) -> list[str]:
    """
    Species list for the Stats tab's Pokemon filter, scoped by
    whichever of team_id/regulation is active. team_id wins if both
    are set -- a roster doesn't depend on regulation (the same team
    can get played across more than one), so once a team is picked
    there's no reason to further narrow it by regulation too.

    team_id set: that team's actual 6-mon roster (team_pokemon, in
    slot_order -- team-preview order, not alphabetical), not just
    whichever of the 6 happened to get sent out and generate an
    events row. A mon that's been on the roster the whole time but
    never brought to field yet should still be selectable.

    Only regulation set: every species seen (events.mon) in a game
    under that regulation, across every team.

    Neither set: every species ever seen at all -- the original,
    unscoped behavior.
    """
    if team_id:
        rows = conn.execute(
            "SELECT species FROM team_pokemon WHERE team_id = ? ORDER BY slot_order", (team_id,)
        ).fetchall()
        return [r[0] for r in rows]

    if regulation:
        rows = conn.execute(
            "SELECT DISTINCT e.mon FROM events e JOIN games g ON e.game_id = g.game_id "
            "WHERE e.mon IS NOT NULL AND e.mon != '' AND g.regulation = ? ORDER BY e.mon",
            (regulation,),
        ).fetchall()
        return [r[0] for r in rows]

    rows = conn.execute(
        "SELECT DISTINCT mon FROM events WHERE mon IS NOT NULL AND mon != '' ORDER BY mon"
    ).fetchall()
    return [r[0] for r in rows]


def _to_db_row(game_id, r):
    if r["EventType"] in ("turnStart", "faint"):
        event_type, move_name = r["EventType"], None
    else:
        event_type, move_name = "move", r["EventType"]
    return (
        game_id, r["Turn"], r["Side"], r["Slot"], r["Mon"], event_type, move_name,
        r["P1a"], r["P1b"], r["P2a"], r["P2b"],
        r.get("KOCreditMon"), r.get("KOCreditSide"),
        r.get("HP1a"), r.get("HP1b"), r.get("HP2a"), r.get("HP2b"),
    )


def import_replay(conn: sqlite3.Connection, log_text: str, url: str, my_usernames, store_logs: bool = True) -> dict:
    """
    Parses + stores one replay. Safe to re-run on the same replay
    (idempotent). my_usernames: see resolve_side_and_result -- the
    caller decides who "me" is. store_logs controls whether the raw
    log text is persisted in the `logs` table (what the in-app replay
    player reads from); defaults to True, and callers that know the
    user turned log storage off pass False -- which also removes any
    previously stored log for a re-imported game.

    Returns a dict describing what happened, one of:
      {"status": "imported", "game_id": ..., "regulation": ..., "battle_size": ...}
      {"status": "skipped_random_battle", "format": ...}
      {"status": "skipped_unsupported_battle_size", "battle_size": "other"}
      {"status": "empty_log"}
    """
    battle_size = detect_battle_size(log_text)
    if battle_size == "other":
        return {"status": "skipped_unsupported_battle_size", "battle_size": battle_size}

    rows = parse_game_events(log_text, url)
    if not rows:
        return {"status": "empty_log"}

    format_str = rows[0]["Format"]
    if is_random_battle(format_str):
        return {"status": "skipped_random_battle", "format": format_str}

    game_id = rows[0]["GameID"]
    p1_name, p2_name, winner = rows[0]["P1Name"], rows[0]["P2Name"], rows[0]["Winner"]
    my_side, result = resolve_side_and_result(p1_name, p2_name, winner, my_usernames)
    regulation = extract_regulation(format_str)

    team_id = None
    if my_side is not None:
        team_preview = parse_team_preview(log_text)
        team_id = resolve_team_id(conn, team_preview[my_side], regulation)

    conn.execute(
        "INSERT OR REPLACE INTO games "
        "(game_id, replay_url, p1_name, p2_name, winner, my_side, result, format, regulation, battle_size, team_id, imported_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (game_id, url, p1_name, p2_name, winner, my_side, result, format_str, regulation, battle_size, team_id,
         datetime.now(timezone.utc).isoformat()),
    )

    # Store opponent team roster for attendance tracking
    team_preview = parse_team_preview(log_text)
    opponent_side = "p2" if my_side == "p1" else "p1" if my_side == "p2" else None
    if opponent_side and team_preview.get(opponent_side):
        conn.execute("DELETE FROM opponent_teams WHERE game_id = ?", (game_id,))
        conn.executemany(
            "INSERT INTO opponent_teams (game_id, slot_order, species, item, moves) VALUES (?, ?, ?, ?, ?)",
            [(game_id, i, mon["species"], mon.get("item", ""), ",".join(mon["moves"]))
             for i, mon in enumerate(team_preview[opponent_side])],
        )

    if store_logs:
        conn.execute(
            "INSERT OR REPLACE INTO logs (game_id, log_text, stored_at) VALUES (?, ?, ?)",
            (game_id, log_text, datetime.now(timezone.utc).isoformat()),
        )
    else:
        conn.execute("DELETE FROM logs WHERE game_id = ?", (game_id,))

    conn.execute("DELETE FROM events WHERE game_id = ?", (game_id,))
    conn.executemany(
        "INSERT INTO events "
        "(game_id, turn, side, slot, mon, event_type, move_name, p1a, p1b, p2a, p2b, ko_credit_mon, ko_credit_side, hp1a, hp1b, hp2a, hp2b) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [_to_db_row(game_id, r) for r in rows],
    )
    conn.commit()

    if my_side is None:
        log.warning("neither %r nor %r matched any configured username for game %s -- result left unresolved.",
              p1_name, p2_name, game_id)

    return {"status": "imported", "game_id": game_id, "regulation": regulation, "battle_size": battle_size}


def prune_old_logs(conn: sqlite3.Connection, retention_days: int) -> int:
    """
    Deletes stored raw logs older than `retention_days`. 0 means keep
    forever (a no-op). Returns how many rows were removed. Called at
    launch and after each sync so logs don't pile up past whatever
    window the user configured.
    """
    if retention_days <= 0:
        return 0
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
    cursor = conn.execute("DELETE FROM logs WHERE stored_at < ?", (cutoff,))
    conn.commit()
    return cursor.rowcount


def get_log_text(conn: sqlite3.Connection, game_id: str) -> str | None:
    """The stored raw battle log for a game, or None if not stored (pruned / disabled)."""
    row = conn.execute("SELECT log_text FROM logs WHERE game_id = ?", (game_id,)).fetchone()
    return row[0] if row else None


def get_my_side(conn: sqlite3.Connection, game_id: str) -> str | None:
    """Which side ("p1" / "p2") the importer resolved as the configured
    user's for a game, or None if no username matched at import time (or
    the game row is gone). The Replays tab uses this as the fallback
    when none of today's configured usernames appear in a log, so a
    replay from a since-removed username still renders from the right
    side of the board."""
    row = conn.execute("SELECT my_side FROM games WHERE game_id = ?", (game_id,)).fetchone()
    return row[0] if row else None


def get_replayable_games(conn: sqlite3.Connection) -> list[tuple]:
    """
    Every game that has a stored raw log -- the ones the Replays tab
    can actually play. Returns (game_id, p1_name, p2_name, winner,
    result, regulation, battle_size), newest battle first.

    Sorted by the battle number encoded in the game id's numeric suffix
    (Showdown assigns those as one monotonic counter across all battles),
    not by imported_at -- bulk imports crawl newest-first, so import
    timestamps within a batch run opposite to battle chronology.
    """
    return conn.execute(
        "SELECT g.game_id, g.p1_name, g.p2_name, g.winner, g.result, g.regulation, g.battle_size "
        "FROM games g JOIN logs l ON l.game_id = g.game_id "
        "ORDER BY CAST(substr(g.game_id, instr(g.game_id, '-') + 1) AS INTEGER) DESC"
    ).fetchall()



def get_pokemon_complete_db_path(base_dir: Path | None = None) -> Path:
    """Resolve the pokemon_complete.db the app should read, with the runtime
    (rebuilt-from-pokedex) copy preferred over the shipped one.

    Order:
      1. ``base_dir / "pokemon_complete.db"`` when ``base_dir`` is passed
         (tests, explicit override) and the file exists,
      2. the writable runtime DB under app-data (``config.get_pokemon_db_path``),
         rebuilt lazily from the refreshed pokedex.js,
      3. the shipped copy bundled with the app (repo checkout or package),
      4. a ``pokemon_complete.db`` in the current working directory (legacy),
      5. the runtime path as the default (so a builder can populate it).
    """
    if base_dir is not None:
        p = Path(base_dir) / "pokemon_complete.db"
        if p.exists():
            return p
    app_candidates = [
        config.get_pokemon_db_path(),
        config.get_shipped_data_dir().parent / "pokemon_complete.db",
        Path(__file__).resolve().parent.parent / "pokemon_complete.db",
        Path(__file__).resolve().parent / "pokemon_complete.db",
    ]
    for cand in app_candidates:
        if cand.exists():
            return cand
    p2 = Path("pokemon_complete.db")
    if p2.exists():
        return p2
    return config.get_pokemon_db_path()


def load_pokedex_pokemon(db_path: str | Path | None = None) -> list[dict]:
    if db_path is None:
        db_path = get_pokemon_complete_db_path()
    path = Path(db_path)
    if not path.exists():
        return []

    conn = sqlite3.connect(path)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT name, type1, type2, abilities, hp, attack, defense, sp_attack, sp_defense, speed
        FROM pokemon
        ORDER BY LOWER(name) ASC
    """)
    rows = cursor.fetchall()
    conn.close()

    result = []
    for row in rows:
        raw_name, t1, t2, ab_str, hp, atk, df, spa, spd, spe = row
        name = " ".join(w.capitalize() for w in raw_name.split("-")) if raw_name else ""
        types = [t.capitalize() for t in [t1, t2] if t]
        abilities = [" ".join(w.capitalize() for w in a.strip().split("-")) for a in ab_str.split(",") if a.strip()] if ab_str else []
        result.append({
            "name": name,
            "types": types,
            "abilities": abilities,
            "stats": {
                "hp": hp or 0,
                "atk": atk or 0,
                "def": df or 0,
                "spa": spa or 0,
                "spd": spd or 0,
                "spe": spe or 0,
            }
        })
    return result


# ---------------------------------------------------------------------------
# Team Builder storage (tb_teams)
# ---------------------------------------------------------------------------

def _tb_utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_tb_teams(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS tb_teams (
            team_id    INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT NOT NULL,
            format     TEXT NOT NULL DEFAULT '',
            data       TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    conn.commit()


def save_tb_team(conn: sqlite3.Connection, name: str, format_: str, data: dict) -> int:
    """Store a new Team Builder team. Returns the new team_id."""
    ensure_tb_teams(conn)
    now = _tb_utc_now()
    cur = conn.execute(
        "INSERT INTO tb_teams (name, format, data, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        (name, format_, json.dumps(data), now, now),
    )
    conn.commit()
    return cur.lastrowid


def update_tb_team(conn: sqlite3.Connection, team_id: int, name: str, format_: str, data: dict) -> None:
    ensure_tb_teams(conn)
    conn.execute(
        "UPDATE tb_teams SET name = ?, format = ?, data = ?, updated_at = ? WHERE team_id = ?",
        (name, format_, json.dumps(data), _tb_utc_now(), team_id),
    )
    conn.commit()


def get_tb_teams(conn: sqlite3.Connection) -> list[dict]:
    ensure_tb_teams(conn)
    rows = conn.execute(
        "SELECT team_id, name, format, data, created_at, updated_at FROM tb_teams ORDER BY updated_at DESC"
    ).fetchall()
    out = []
    for team_id, name, format_, data, created_at, updated_at in rows:
        try:
            payload = json.loads(data)
        except (ValueError, TypeError):
            payload = {}
        out.append({
            "team_id": team_id,
            "name": name,
            "format": format_,
            "data": payload,
            "created_at": created_at,
            "updated_at": updated_at,
        })
    return out


def get_tb_team(conn: sqlite3.Connection, team_id: int) -> dict | None:
    row = conn.execute(
        "SELECT team_id, name, format, data, created_at, updated_at FROM tb_teams WHERE team_id = ?",
        (team_id,),
    ).fetchone()
    if not row:
        return None
    team_id, name, format_, data, created_at, updated_at = row
    try:
        payload = json.loads(data)
    except (ValueError, TypeError):
        payload = {}
    return {
        "team_id": team_id,
        "name": name,
        "format": format_,
        "data": payload,
        "created_at": created_at,
        "updated_at": updated_at,
    }


def delete_tb_team(conn: sqlite3.Connection, team_id: int) -> None:
    ensure_tb_teams(conn)
    conn.execute("DELETE FROM tb_teams WHERE team_id = ?", (team_id,))
    conn.commit()
