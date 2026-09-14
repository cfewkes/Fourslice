"""
fourslice/learnsets.py

Parses the static Pokemon Showdown learnset dump shipped with the app
at data/learnsets.ts into a SQLite database with ONE TABLE PER
GENERATION:

    gen_1 .. gen_9   one row per species, holding that generation's full
                     legal move list (a JSON array of move ids)

so a move/Pokemon picker for a chosen generation is a single indexed
lookup -- the transfer rules are baked into the tables at index time and
never re-evaluated at query time. No method/flag detail is stored, only
the resulting move ids.

How a move's availability is encoded in the source:

    species: { learnset: { move: [ "9M", "3L4", "7V", ... ] } }

Each entry in a move's flag array is "<generation><method><detail>":
"9M" is a TM in gen 9, "3L4" a level-up at level 4 in gen 3, "6S5" an
event in gen 6, "7V" a Virtual Console / Let's Go transfer in gen 7,
"8R" a Special-distribution move (Rotom Catalog etc.) in gen 8. Method
letters are decoded by parse_flag(), M/L/T/E/S/D/V/R.

Legality rules applied at index time:

    gen 1-8  a (species, move) pair is legal when the source lists the
             move for that species in ANY generation <= N -- moves
             transfer forward across the first eight generations.
    gen 9    a pair is legal ONLY for moves the source lists for gen 9
             itself (no transfer into gen 9).

Important limitation (same as Showdown's own data): learnsets.ts only
encodes generations 3-9 -- there are no per-move gen-1/gen-2 entries --
so gen_1 and gen_2 tables are created but stay empty, and looking up
gen 1/2 legality returns nothing. That matches what the published data
can express.

The per-generation tables are built from the shipped static file, so
everything here is unit-testable offline. The one exception is the
Champions table: that source is Showdown's data/mods/champions/learnsets.ts
on GitHub, fetched best-effort at startup (never blocking on a slow or
absent network -- an unsuccessful fetch leaves the existing table alone).
The module stays Qt-free throughout.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sqlite3
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fourslice import config
from fourslice.sprites import normalize_species

log = logging.getLogger(__name__)

# Hard-coded ceiling for parse_flag sanity-check and schema fallback.
# The *effective* max generation is derived from the indexed data at
# runtime via _get_max_generation() so Gen 10+ needs no code change.
MAX_GENERATION = 99

# The shipped Pokemon Showdown learnset dump (static, never re-downloaded).
DEFAULT_LEARNSETS_PATH = config.get_shipped_data_dir() / "learnsets.ts"

# Base learnsets (data/learnsets.ts from play.pokemonshowdown.com) -- fetched
# best-effort at startup so brand-new Pokemon/moves/legal moves appear without
# an app update. Mirrors the Champions flow: fetch -> sha256 -> re-index via
# meta table (base_index_is_current / ensure_base_indexed), 7-day staleness.
BASE_LEARNSETS_URL = "https://play.pokemonshowdown.com/data/learnsets.ts"
BASE_LEARNSETS_META_VERSION = "base_schema_version"
BASE_LEARNSETS_META_SHA = "base_source_sha256"
BASE_SCHEMA_VERSION = "1"

# Champions (the Showdown "Modded" meta) has its own learnset table --
# moves are legal in Champions regardless of their gen-9 transfer status,
# and Pokemon get retuned (e.g. before nerfing a move) differently than
# base gen 9. The source lives in Pokemon Showdown's GitHub repo and is
# fetched best-effort at startup (it changes between regulations).
CHAMPIONS_LEARNSETS_URL = (
    "https://raw.githubusercontent.com/smogon/pokemon-showdown/master/"
    "data/mods/champions/learnsets.ts"
)
# The shipped copy lives in data/ (read-only). The writable runtime copy
# lives in the per-user app-data showdown dir, seeded from the shipped
# copy on first use.
def _champions_runtime_path() -> Path:
    return config.get_showdown_data_dir() / "champions-learsets.ts"
# Champions tables are keyed under these meta entries so a champions index
# never collides with (or invalidates) the gen_* index's provenance.
CHAMPIONS_META_VERSION = "champions_schema_version"
CHAMPIONS_META_SHA = "champions_source_sha256"
CHAMPIONS_SCHEMA_VERSION = "1"

# Bump whenever the gen_* table layout changes, so previously-built DBs
# (e.g. an old per-(species, move) schema) are detected and re-indexed
# instead of failing at query time.
# 3 = dynamic generation count (meta.max_generation); old DBs re-index
SCHEMA_VERSION = 3

_GEN_TABLE_SCHEMA = """
    species TEXT PRIMARY KEY,
    moves   TEXT NOT NULL   -- JSON array of move ids, e.g. '["acidspray","growl"]'
"""

# Dynamically sized: the effective max generation is derived from the
# indexed data (meta.max_generation) so Gen 10+ needs no code change.
# A helper builds the table list for the current max generation.
def _gen_tables(max_gen: int | None = None) -> tuple[str, ...]:
    """Return ('gen_1', ..., 'gen_N') for the current max generation."""
    mg = max_gen if max_gen is not None else _get_max_generation()
    return tuple(f"gen_{g}" for g in range(1, mg + 1))

# Table picked by the team builder whenever a format is a Champions format.
CHAMPIONS_TABLE = "champions"

_WHITESPACE = frozenset(" \t\r\n")
_SIMPLE_TOKENS = {
    "{": "lbrace",
    "}": "rbrace",
    "[": "lbracket",
    "]": "rbracket",
    ":": "colon",
    ",": "comma",
}
_STRING_ESCAPES = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f", "0": "\0"}

_FLAG_RE = re.compile(r"^(\d{1,2})([A-Za-z])(.*)$")


# ---------------------------------------------------------------------------
# Tokenizer -- the learnsets file (TS or minified JS) is one big object
# literal, so a tiny non-eval tokenizer is both safe and fast.
# ---------------------------------------------------------------------------

def iter_tokens(text: str, start: int = 0):
    """Tokenize the object literal beginning at byte offset ``start``.

    Yields ``(kind, value)`` where kind is one of lbrace, rbrace,
    lbracket, rbracket, colon, comma, string, atom, eof. A *string* is a
    double- or single-quoted value; an *atom* is any bare identifier,
    number or keyword (null/true/false included). Whitespace is skipped
    and an ``("eof", None)`` token is emitted once at the end.
    """
    i, n = start, len(text)
    while True:
        while i < n and text[i] in _WHITESPACE:
            i += 1
        if i >= n:
            yield "eof", None
            return
        ch = text[i]
        kind = _SIMPLE_TOKENS.get(ch)
        if kind is not None:
            yield kind, ch
            i += 1
            continue
        if ch in ('"', "'"):
            quote = ch
            i += 1
            buf = []
            while True:
                if i >= n:
                    raise ValueError("unterminated string in learnsets data")
                c = text[i]
                if c == "\\":
                    i += 1
                    if i >= n:
                        raise ValueError("unterminated escape in learnsets data")
                    esc = text[i]
                    buf.append(_STRING_ESCAPES.get(esc, esc))
                elif c == quote:
                    i += 1
                    break
                else:
                    buf.append(c)
                i += 1
            yield "string", "".join(buf)
            continue
        if ch.isalnum() or ch in "_$":
            j = i
            while j < n and (text[j].isalnum() or text[j] in "_$.-"):
                j += 1
            yield "atom", text[i:j]
            i = j
            continue
        raise ValueError(f"unexpected character {ch!r} in learnsets data at offset {i}")


# ---------------------------------------------------------------------------
# Streaming object-literal parser. Only each species' learnset map is
# materialised; eventData/encounters bodies are skipped over wholesale so
# the whole 3.7 MB file never has to live in memory.
# ---------------------------------------------------------------------------

def _skip_value(it, start_kind: str):
    """Consume the rest of one value whose FIRST token has already been
    pulled from the iterator (`start_kind` is that token's kind).

    Nested objects/arrays are walked without building them. A top-level
    atom needs nothing; a top-level { or [ needs up until the matching
    closer, i.e. just count braces from the opener we already have."""
    depth = 1 if start_kind in ("lbrace", "lbracket") else 0
    while depth:
        tok, _ = next(it)
        if tok in ("lbrace", "lbracket"):
            depth += 1
        elif tok in ("rbrace", "rbracket"):
            depth -= 1


def _parse_flag_array(it) -> list[str]:
    """Parse ["flag", "flag", ...] after the '[' has been pulled."""
    flags = []
    while True:
        tok, value = next(it)
        if tok == "rbracket":
            return flags
        if tok != "string":
            raise ValueError(f"expected string flag, got {tok!r}")
        flags.append(value)
        tok, _ = next(it)
        if tok == "rbracket":
            return flags
        if tok != "comma":
            raise ValueError(f"expected ',' or ']' in flag array, got {tok!r}")


def _parse_learnset_table(it) -> dict[str, list[str]]:
    """Parse {move: [flags], ...} after the leading '{' has been pulled."""
    table = {}
    while True:
        tok, key = next(it)
        if tok == "rbrace":
            return table
        if tok not in ("atom", "string"):
            raise ValueError(f"expected move key, got {tok!r}")
        _, _ = next(it)  # colon
        tok, _ = next(it)
        if tok != "lbracket":
            raise ValueError(f"expected flag array for {key!r}")
        table[key] = _parse_flag_array(it)
        tok, _ = next(it)
        if tok == "rbrace":
            return table
        if tok != "comma":
            raise ValueError(f"expected ',' or '}}' after {key!r}, got {tok!r}")


def _parse_species_body(it) -> dict[str, list[str]] | None:
    """Parse one species object after its '{' has been pulled.

    Returns the learnset map, or None for entries that carry only
    eventData/encounters (e.g. the pokestar stage props)."""
    learnset = None
    while True:
        tok, key = next(it)
        if tok == "rbrace":
            return learnset
        if tok not in ("atom", "string"):
            raise ValueError(f"expected key in species object, got {tok!r}")
        _, _ = next(it)  # colon
        if key == "learnset":
            tok, _ = next(it)
            if tok != "lbrace":
                raise ValueError("expected object for learnset")
            learnset = _parse_learnset_table(it)
        else:
            tok, _ = next(it)  # first token of the value
            _skip_value(it, tok)
        tok, _ = next(it)
        if tok == "rbrace":
            return learnset
        if tok != "comma":
            raise ValueError(f"expected ',' or '}}' after {key!r}, got {tok!r}")


def iter_species(text: str):
    """Yield (species_id, learnset_or_None) for every top-level entry.

    ``learnset`` is {move_id: [flag, ...]}; species with no learnset key
    yield None. The leading "export const Learnsets: ... = {" TS header
    (or any "... = {" prologue) is located and skipped automatically.
    """
    root = text.index("{", text.index("="))
    it = iter_tokens(text, root)
    tok, _ = next(it)
    if tok != "lbrace":
        raise ValueError("learnsets data does not start with a top-level object")
    while True:
        tok, name = next(it)
        if tok == "rbrace":
            return
        if tok not in ("atom", "string"):
            raise ValueError(f"expected species key, got {tok!r} {name!r}")
        _, _ = next(it)  # colon
        tok, _ = next(it)
        if tok != "lbrace":
            raise ValueError(f"expected object for species {name!r}")
        yield name, _parse_species_body(it)
        tok, _ = next(it)
        if tok == "rbrace":
            return
        if tok != "comma":
            raise ValueError(f"expected ',' or '}}' after {name!r}, got {tok!r}")


def parse_learnsets(text: str) -> dict[str, dict[str, list[str]] | None]:
    """Fully parse the data into {species_id: learnset | None}.

    Production code uses the streaming iter_species(); this convenience
    dict form exists for tests and small inputs.
    """
    return dict(iter_species(text))


# ---------------------------------------------------------------------------
# Flag model -- "<generation><method><detail>"
# ---------------------------------------------------------------------------

def parse_flag(flag: str) -> tuple[int, str, str | None]:
    """Split a learnset flag into (generation, method, detail).

    Examples: "9M" -> (9, "M", None); "3L4" -> (3, "L", "4");
    "6S5" -> (6, "S", "5"); "7V" -> (7, "V", None); "8R" -> (8, "R", None).
    Raises ValueError for anything that doesn't start with a generation
    digit followed by a method letter, or generation < 1.
    """
    match = _FLAG_RE.match(flag)
    if not match:
        raise ValueError(f"invalid learnset flag {flag!r}")
    generation = int(match.group(1))
    if generation < 1:
        raise ValueError(f"invalid learnset flag {flag!r}")
    method = match.group(2).upper()
    detail = match.group(3) or None
    return generation, method, detail


def _get_max_generation() -> int:
    """Return the effective max generation.

    Reads `meta.max_generation` from the learnsets DB if present;
    otherwise falls back to the historical hard-coded 9. This makes
    Gen 10+ work without a code release — the indexer records the
    highest generation it saw in the source file.
    """
    try:
        db_path = config.get_learnsets_db_path()
        if not db_path.is_file():
            return 9
        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = 'max_generation'"
            ).fetchone()
            if row and row[0]:
                return max(9, int(row[0]))
        finally:
            conn.close()
    except Exception:
        pass
    return 9


def _legal_generations(move_flags: list[str]) -> list[int]:
    """Which generations a move is legal in, per the transfer rules.

    * gen 1..(max_gen-1): any flag listed for a generation <= N makes the
      move legal in N (moves transfer forward).
    * max_gen: only a native max_gen flag counts (no transfer into the
      current generation). Matches Showdown's model.

    Returns the generation numbers themselves (no method detail -- only
    the generation digit decides legality here).
    """
    parsed = [parse_flag(f) for f in move_flags]
    if not parsed:
        return []
    gens_present = {entry[0] for entry in parsed}
    max_gen = _get_max_generation()
    # Generations < max_gen get transfers; max_gen is native-only.
    # Cap parsed generations to the ceiling so future source flags
    # don't create ghost tables beyond what we can represent.
    capped = {min(g, max_gen) for g in gens_present}
    if not capped:
        return []
    min_gen = min(capped)
    if max_gen <= 8:
        # Historical: 1-8 transfer forward, 9 native
        legal = list(range(min_gen, max_gen))
        if max_gen in capped:
            legal.append(max_gen)
        return legal
    else:
        # max_gen >= 9: 1..max_gen-1 transfer, max_gen native
        legal = list(range(min_gen, max_gen))
        if max_gen in capped:
            legal.append(max_gen)
        return legal


# ---------------------------------------------------------------------------
# SQLite layer -- nine per-generation tables plus a provenance `meta` table
# ---------------------------------------------------------------------------

def init_db(db_path: str | Path) -> sqlite3.Connection:
    """Open (creating + migrating) the per-generation learnsets DB.

    Schema:
        meta                 -- provenance (source hash, schema version,
                               counts, indexed_at)
        gen_1 .. gen_N       -- one row per species: (species, moves) where
                               moves is a JSON array of the generation's
                               legal move ids. N is dynamic (meta.max_generation).
        champions            -- same shape, holding the Showdown Champions
                               learnset table (see index_champions_learnsets).

    Safe to call on every launch. Creates any missing tables and, when
    the DB carries a stale `schema_version` in meta (built by an older
    layout), drops and recreates the gen_* tables on the new schema --
    so an old-schema DB never survives a re-open; the next
    index/ensure_indexed repopulates it.
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    # Determine the effective max generation from meta (fallback to 9)
    max_gen = 9
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'max_generation'"
        ).fetchone()
        if row and row[0]:
            max_gen = max(9, int(row[0]))
    except Exception:
        pass
    for table in _gen_tables(max_gen):
        conn.execute(f"CREATE TABLE IF NOT EXISTS {table} ({_GEN_TABLE_SCHEMA})")
    conn.execute(f"CREATE TABLE IF NOT EXISTS {CHAMPIONS_TABLE} ({_GEN_TABLE_SCHEMA})")
    version = conn.execute(
        "SELECT value FROM meta WHERE key = 'schema_version'"
    ).fetchone()
    # Missing version => DB built by the old layout (it never wrote one);
    # stale version => a newer layout exists. Either way rebuild the
    # gen_* tables on the current schema.
    if version is None or version[0] != str(SCHEMA_VERSION):
        for table in _gen_tables(max_gen):
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        for table in _gen_tables(max_gen):
            conn.execute(f"CREATE TABLE {table} ({_GEN_TABLE_SCHEMA})")
    conn.commit()
    return conn


def _meta_update(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def index_learnsets(src_path: str | Path, db_path: str | Path | None = None) -> dict:
    """Parse learnsets data (the shipped .ts file, or any TS/JS fixture)
    and rebuild the per-generation tables.

    One pass over the file, accumulating each species' legal moves per
    generation (transfer rules applied once per move), then one row per
    (species, generation): species + a sorted JSON array of move ids.
    Idempotent: each call starts from empty tables. Returns a summary
    dict of what got indexed.
    """
    src_path = Path(src_path)
    db_path = Path(db_path) if db_path is not None else config.get_learnsets_db_path()
    raw = src_path.read_bytes()
    text = raw.decode("utf-8")
    conn = init_db(db_path)
    try:
        # Determine max generation from the source during the pass
        max_gen_seen = 9
        for table in _gen_tables():
            conn.execute(f"DELETE FROM {table}")
        species_count = 0
        move_pairs = 0
        rows = 0
        for species, learnset in iter_species(text):
            if learnset is None:
                continue
            species_id = normalize_species(species)
            species_count += 1
            by_gen: dict[int, list[str]] = {}
            for move, move_flags in learnset.items():
                move_pairs += 1
                for gen in _legal_generations(move_flags):
                    by_gen.setdefault(gen, []).append(move)
                    if gen > max_gen_seen:
                        max_gen_seen = gen
            for gen, moves in by_gen.items():
                conn.execute(
                    f"INSERT OR REPLACE INTO gen_{gen} (species, moves) VALUES (?, ?)",
                    (species_id, json.dumps(sorted(moves), separators=(",", ":"))),
                )
                rows += 1
            if species_count % 100 == 0:
                conn.commit()
        _meta_update(conn, "source_file", str(src_path))
        _meta_update(
            conn, "source_sha256", hashlib.sha256(raw).hexdigest()
        )
        _meta_update(conn, "schema_version", str(SCHEMA_VERSION))
        _meta_update(conn, "indexed_at", _utc_now())
        _meta_update(conn, "species_count", str(species_count))
        _meta_update(conn, "row_count", str(rows))
        _meta_update(conn, "max_generation", str(max_gen_seen))
        conn.commit()
        return {
            "db": str(db_path),
            "source": str(src_path),
            "species": species_count,
            "move_pairs": move_pairs,
            "rows": rows,
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Champions learnsets -- same table shape, sourced from Showdown's
# data/mods/champions/learnsets.ts (fetched best-effort, not shipped).
# ---------------------------------------------------------------------------

def _older_than(path: Path, max_age_days: int) -> bool:
    """True when a file's mtime is older than max_age_days (or unreadable)."""
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
    except OSError:
        return True
    return (datetime.now(timezone.utc) - mtime) > timedelta(days=max_age_days)


def fetch_champions_learnsets(target_path: str | Path | None = None) -> Path:
    """Download the Champions learnset dump from Pokemon Showdown's GitHub.

    Best-effort: on any network failure the target file is left untouched
    (so offline startup keeps whatever is already on disk) and the error
    is reported to stdout rather than raised.
    """
    target = Path(target_path) if target_path is not None else DEFAULT_CHAMPIONS_LEARNSETS_PATH
    try:
        req = urllib.request.Request(
            CHAMPIONS_LEARNSETS_URL, headers={"User-Agent": "Mozilla/5.0"}
        )
        target.write_bytes(urllib.request.urlopen(req, timeout=40).read())
    except Exception as exc:  # network is best-effort
        log.warning("could not fetch champions learnsets: %s", exc)
    return target


def index_champions_learnsets(src_path: str | Path, db_path: str | Path | None = None) -> dict:
    """Parse a Champions learnset dump into the `champions` table.

    The file uses the same species -> learnset -> move -> flags layout as
    the generation dump, but every listed move is simply legal in
    Champions -- there is no per-generation transfer logic, so a species'
    moves are exactly the keys of its learnset map. Entries that only
    `inherit` from the base mod (no learnset of their own) fall back to
    the gen_9 table so their moves aren't lost.

    Idempotent: each call starts from an empty table. Returns a summary
    dict of what got indexed.
    """
    src_path = Path(src_path)
    db_path = Path(db_path) if db_path is not None else config.get_learnsets_db_path()
    raw = src_path.read_bytes()
    text = raw.decode("utf-8")
    conn = init_db(db_path)
    try:
        conn.execute(f"DELETE FROM {CHAMPIONS_TABLE}")
        species_count = 0
        for species, learnset in iter_species(text):
            species_id = normalize_species(species)
            if learnset is None:
                # inherit-only entry: reuse the base gen-9 learnset (if it
                # happens to be indexed already -- otherwise skip).
                row = conn.execute(
                    "SELECT moves FROM gen_9 WHERE species = ?", (species_id,)
                ).fetchone()
                if not row:
                    continue
                moves = json.loads(row[0])
            else:
                moves = list(learnset.keys())
            conn.execute(
                f"INSERT OR REPLACE INTO {CHAMPIONS_TABLE} (species, moves) VALUES (?, ?)",
                (species_id, json.dumps(sorted(moves), separators=(",", ":"))),
            )
            species_count += 1
            if species_count % 100 == 0:
                conn.commit()
        _meta_update(conn, "champions_source_file", str(src_path))
        _meta_update(conn, CHAMPIONS_META_SHA, hashlib.sha256(raw).hexdigest())
        _meta_update(conn, CHAMPIONS_META_VERSION, CHAMPIONS_SCHEMA_VERSION)
        _meta_update(conn, "champions_indexed_at", _utc_now())
        _meta_update(conn, "champions_species_count", str(species_count))
        conn.commit()
        return {
            "db": str(db_path),
            "source": str(src_path),
            "species": species_count,
        }
    finally:
        conn.close()


def champions_index_is_current(db_path: str | Path, src_path: str | Path) -> bool:
    """True when the `champions` table was built from this exact source file
    (hash compare) by the current schema version."""
    db_path = Path(db_path)
    if not db_path.is_file():
        return False
    conn = init_db(db_path)
    try:
        sha = conn.execute(
            "SELECT value FROM meta WHERE key = ?", (CHAMPIONS_META_SHA,)
        ).fetchone()
        version = conn.execute(
            "SELECT value FROM meta WHERE key = ?", (CHAMPIONS_META_VERSION,)
        ).fetchone()
    finally:
        conn.close()
    if sha is None or version is None:
        return False
    if version[0] != CHAMPIONS_SCHEMA_VERSION:
        return False
    try:
        return sha[0] == hashlib.sha256(Path(src_path).read_bytes()).hexdigest()
    except OSError:
        return False


def ensure_champions_indexed(
    src_path: str | Path | None = None,
    db_path: str | Path | None = None,
    *,
    max_age_days: int = 7,
    force: bool = False,
) -> dict:
    """Make sure the Champions learnset table is populated and current.

    Fetches the source from GitHub when the local copy is missing or
    older than `max_age_days`, then indexes it if the table doesn't match
    the source hash. Best-effort: offline, or a failed fetch, leaves the
    DB exactly as it was. Returns a summary of what happened.
    """
    runtime_dir = config.get_showdown_data_dir()
    src = Path(src_path) if src_path is not None else runtime_dir / "champions-learsets.ts"
    db = Path(db_path) if db_path is not None else config.get_learnsets_db_path()

    # Seed the runtime copy from the shipped file on first use
    if not src.is_file():
        shipped = config.get_shipped_data_dir() / "champions-learsets.ts"
        if shipped.is_file():
            try:
                src.write_bytes(shipped.read_bytes())
            except Exception:
                pass

    if not src.is_file() or _older_than(src, max_age_days):
        fetch_champions_learnsets(src)
    if src.is_file() and (force or not champions_index_is_current(db, src)):
        return index_champions_learnsets(src, db)
    return {
        "db": str(db),
        "source": str(src),
        "species": None,  # already current; index_champions_learnsets reports counts
        "current": True,
    }


# ---------------------------------------------------------------------------
# Base learnsets (data/learnsets.ts from play.pokemonshowdown.com) -- fetched
# best-effort at startup so brand-new Pokemon/moves/legal moves appear without
# an app update. Mirrors the Champions flow: fetch -> sha256 -> re-index via
# meta table (base_index_is_current / ensure_base_indexed), 7-day staleness.
# ---------------------------------------------------------------------------

def fetch_base_learnsets(target_path: str | Path | None = None) -> Path:
    """Download the base learnset dump from play.pokemonshowdown.com.

    Best-effort: on any network failure the target file is left untouched
    (so offline startup keeps whatever is already on disk) and the error
    is reported to stdout rather than raised.
    """
    runtime_dir = config.get_showdown_data_dir()
    target = Path(target_path) if target_path is not None else runtime_dir / "learnsets.ts"
    try:
        req = urllib.request.Request(
            BASE_LEARNSETS_URL, headers={"User-Agent": "Mozilla/5.0"}
        )
        content = urllib.request.urlopen(req, timeout=40).read()
        # Validate-then-commit: never replace a good file with a truncated
        # or corrupt download (mirrors the fetch_data_files smoke check).
        if not _smoke_learnsets(content.decode("utf-8", errors="replace")):
            log.warning("base learnsets download failed smoke check; keeping existing file")
            return target
        target.write_bytes(content)
    except Exception as exc:  # network is best-effort
        log.warning("could not fetch base learnsets: %s", exc)
    return target


def _smoke_learnsets(text: str) -> bool:
    """Quick sanity check that the learnsets text looks like a valid dump:
    at least one species with a learnset map is present. Avoids replacing
    a good file with a truncated/corrupt download."""
    try:
        data = parse_learnsets(text)
    except Exception:
        return False
    if not isinstance(data, dict) or not data:
        return False
    # At least one species with a non-empty learnset
    for v in data.values():
        if isinstance(v, dict) and v:
            return True
    return False


def base_index_is_current(db_path: str | Path, src_path: str | Path) -> bool:
    """True when the `gen_*` tables were built from this exact source file
    (hash compare) by the current BASE_SCHEMA_VERSION.

    Requires the base provenance keys recorded by ``_stamp_base_index``
    (``index_learnsets`` itself only writes the generic ``source_sha256`` /
    ``schema_version`` keys). Also requires the gen-table schema to be
    current so a schema migration that just dropped/emptied the tables
    forces a rebuild instead of reporting a healthy index."""
    db_path = Path(db_path)
    if not db_path.is_file():
        return False
    conn = init_db(db_path)
    try:
        sha = conn.execute(
            "SELECT value FROM meta WHERE key = ?", (BASE_LEARNSETS_META_SHA,)
        ).fetchone()
        version = conn.execute(
            "SELECT value FROM meta WHERE key = ?", (BASE_LEARNSETS_META_VERSION,)
        ).fetchone()
        schema = conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
    finally:
        conn.close()
    if sha is None or version is None:
        return False
    if version[0] != BASE_SCHEMA_VERSION:
        return False
    if schema is None or schema[0] != str(SCHEMA_VERSION):
        return False
    try:
        return sha[0] == hashlib.sha256(Path(src_path).read_bytes()).hexdigest()
    except OSError:
        return False


def _stamp_base_index(db_path: str | Path, src_path: str | Path) -> None:
    """Record base provenance after ``index_learnsets`` so
    ``base_index_is_current`` can recognize the result as current.

    ``index_learnsets`` writes provenance under the generic keys
    (``source_sha256`` / ``schema_version``) so the shipped-file flow
    (``ensure_indexed``) and the runtime flow can coexist; the base flow
    tracks its own hash/version under ``base_source_sha256`` /
    ``base_schema_version``."""
    conn = init_db(db_path)
    try:
        _meta_update(conn, BASE_LEARNSETS_META_SHA,
                     hashlib.sha256(Path(src_path).read_bytes()).hexdigest())
        _meta_update(conn, BASE_LEARNSETS_META_VERSION, BASE_SCHEMA_VERSION)
        conn.commit()
    finally:
        conn.close()


def ensure_base_indexed(
    src_path: str | Path | None = None,
    db_path: str | Path | None = None,
    *,
    max_age_days: int = 7,
    force: bool = False,
) -> dict:
    """Make sure the base learnset tables (gen_1..gen_9) are populated and current.

    Fetches the source from play.pokemonshowdown.com when the local copy is
    missing or older than `max_age_days`, then indexes it if the tables don't
    match the source hash. Best-effort: offline, or a failed fetch, leaves the
    DB exactly as it was. Returns a summary of what happened.
    """
    runtime_dir = config.get_showdown_data_dir()
    src = Path(src_path) if src_path is not None else runtime_dir / "learnsets.ts"
    db = Path(db_path) if db_path is not None else config.get_learnsets_db_path()
    if not src.is_file() or _older_than(src, max_age_days):
        fetch_base_learnsets(src)
    if src.is_file() and (force or not base_index_is_current(db, src)):
        summary = index_learnsets(src, db)
        _stamp_base_index(db, src)
        return summary
    return {
        "db": str(db),
        "source": str(src),
        "species": None,  # already current; index_learnsets reports counts
        "current": True,
    }


def champions_legal_moves(conn: sqlite3.Connection, species: str) -> list[str]:
    """Every move `species` may legally know in Showdown Champions, sorted."""
    row = conn.execute(
        f"SELECT moves FROM {CHAMPIONS_TABLE} WHERE species = ?",
        (normalize_species(species),),
    ).fetchone()
    return json.loads(row[0]) if row is not None else []


def champions_legal_species(
    conn: sqlite3.Connection,
    limit: int | None = None,
    with_moves: bool = False,
):
    """Every species legal in Champions, sorted by species.

    With ``with_moves=True`` returns ``[(species, [move, ...]), ...]``.
    """
    sql = f"SELECT species FROM {CHAMPIONS_TABLE} ORDER BY species"
    if with_moves:
        sql = f"SELECT species, moves FROM {CHAMPIONS_TABLE} ORDER BY species"
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    if with_moves:
        return [(row[0], json.loads(row[1])) for row in conn.execute(sql).fetchall()]
    return [row[0] for row in conn.execute(sql).fetchall()]


def index_is_current(db_path: str | Path, src_path: str | Path) -> bool:
    """True when the DB exists, was built by this schema version, and its
    stored `source_sha256` matches the source file on disk -- i.e. no
    re-index is needed. A schema-version mismatch (e.g. a DB built by an
    older per-(species, move) layout) forces a rebuild even if the source
    hash is unchanged.
    """
    db_path = Path(db_path)
    if not db_path.is_file():
        return False
    conn = init_db(db_path)
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'source_sha256'"
        ).fetchone()
        version = conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
    finally:
        conn.close()
    if row is None or version is None:
        return False
    if version[0] != str(SCHEMA_VERSION):
        return False
    return row[0] == hashlib.sha256(Path(src_path).read_bytes()).hexdigest()


def ensure_indexed(src_path: str | Path | None = None, db_path: str | Path | None = None) -> Path:
    """Make sure learnsets.db is populated from the local learnsets file.

    Indexes only when the DB is missing or was built from a different
    source file (hash compare). Returns the DB path. The source ships
    with the app so this never needs the network.
    """
    src = Path(src_path) if src_path is not None else DEFAULT_LEARNSETS_PATH
    db = Path(db_path) if db_path is not None else config.get_learnsets_db_path()
    if not index_is_current(db, src):
        index_learnsets(src, db)
    return db


# ---------------------------------------------------------------------------
# Query API (all lookups are per-generation table scans, transfer rules
# already applied at index time)
# ---------------------------------------------------------------------------

def _check_generation(generation: int) -> int:
    gen = int(generation)
    max_gen = _get_max_generation()
    if not 1 <= gen <= max_gen:
        raise ValueError(
            f"generation must be between 1 and {max_gen}, got {generation!r}"
        )
    return gen


def legal_moves(conn: sqlite3.Connection, species: str, generation: int) -> list[str]:
    """Every move `species` may legally know in `generation`, sorted.

    Pickers call this once per species selection; it is a single PK lookup
    (species-normalised key) that decodes the stored JSON move list.
    """
    gen = _check_generation(generation)
    row = conn.execute(
        f"SELECT moves FROM gen_{gen} WHERE species = ?",
        (normalize_species(species),),
    ).fetchone()
    return json.loads(row[0]) if row is not None else []


def legal_species(
    conn: sqlite3.Connection,
    generation: int,
    limit: int | None = None,
    with_moves: bool = False,
):
    """Every species legal in `generation` (i.e. with at least one legal
    move), sorted by species.

    With ``with_moves=False`` (default) returns a list of species ids.
    With ``with_moves=True`` returns ``[(species, [move, ...]), ...]`` so a
    picker can build its full per-species move model in a single scan.
    ``limit`` caps the number of species rows for paged pickers.
    """
    gen = _check_generation(generation)
    sql = f"SELECT species FROM gen_{gen} ORDER BY species"
    if with_moves:
        sql = f"SELECT species, moves FROM gen_{gen} ORDER BY species"
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    if with_moves:
        return [(row[0], json.loads(row[1])) for row in conn.execute(sql).fetchall()]
    return [row[0] for row in conn.execute(sql).fetchall()]


# ---------------------------------------------------------------------------
# CLI -- python -m fourslice.learnsets {index | moves <species> <gen> |
#                                        species <gen>}
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m fourslice.learnsets",
        description=(
            "Index the shipped Pokemon Showdown learnsets.ts into a "
            "per-generation SQLite DB and query per-generation legality."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sp_index = sub.add_parser("index", help="parse learnsets into the per-generation tables")
    sp_index.add_argument("--src", default=None,
                          help="learnsets source file (default: data/learnsets.ts)")
    sp_index.add_argument("--db", default=None,
                          help="output DB path (default: app-data learnsets.db)")

    sp_moves = sub.add_parser("moves", help="list a Pokemon's legal moves in a generation")
    sp_moves.add_argument("species")
    sp_moves.add_argument("generation", type=int)
    sp_moves.add_argument("--db", default=None,
                          help="learnsets DB path (default: app-data learnsets.db)")

    sp_species = sub.add_parser("species", help="list every Pokemon legal in a generation")
    sp_species.add_argument("generation", type=int)
    sp_species.add_argument("--db", default=None,
                            help="learnsets DB path (default: app-data learnsets.db)")

    sp_champ_idx = sub.add_parser("champions-index",
                                   help="fetch + index the Champions learnsets")
    sp_champ_idx.add_argument("--src", default=None,
                              help="champions learnsets source file (default: data/champions-learsets.ts)")
    sp_champ_idx.add_argument("--db", default=None,
                              help="output DB path (default: app-data learnsets.db)")
    sp_champ_idx.add_argument("--force", action="store_true",
                              help="re-index even if the source hasn't changed")

    sp_champ_moves = sub.add_parser("champions-moves",
                                     help="list a Pokemon's legal moves in Champions")
    sp_champ_moves.add_argument("species")
    sp_champ_moves.add_argument("--db", default=None,
                                help="learnsets DB path (default: app-data learnsets.db)")

    sp_champ_sp = sub.add_parser("champions-species",
                                  help="list every Pokemon legal in Champions")
    sp_champ_sp.add_argument("--db", default=None,
                             help="learnsets DB path (default: app-data learnsets.db)")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    src = Path(args.src) if getattr(args, "src", None) else DEFAULT_LEARNSETS_PATH
    db = Path(args.db) if getattr(args, "db", None) else config.get_learnsets_db_path()

    if args.command == "index":
        if not src.is_file():
            log.error("no learnsets source at %s", src)
            return 2
        try:
            summary = index_learnsets(src, db)
        except (ValueError, OSError) as exc:
            log.error("%s", exc)
            return 2
        print(json.dumps(summary, indent=2))
        return 0

    if args.command in ("moves", "species"):
        if not db.is_file():
            log.error("no database at %s -- run 'index' first", db)
            return 1
        conn = init_db(db)
        try:
            if args.command == "moves":
                moves = legal_moves(conn, args.species, args.generation)
                if not moves:
                    log.error("no legal moves for %r in gen %s", args.species, args.generation)
                    return 1
                print(json.dumps(moves, indent=2))
            else:
                print(json.dumps(legal_species(conn, args.generation), indent=2))
        finally:
            conn.close()
        return 0

    if args.command == "champions-index":
        champ_src = Path(args.src) if getattr(args, "src", None) else DEFAULT_CHAMPIONS_LEARNSETS_PATH
        champ_db = Path(args.db) if getattr(args, "db", None) else db
        try:
            # Fetch the source when missing/stale, then index when the
            # table doesn't already match it -- no redundant rewrite.
            ensure_champions_indexed(
                champ_src, champ_db, force=getattr(args, "force", False)
            )
            summary = index_champions_learnsets(champ_src, champ_db)
        except OSError as exc:
            log.error("%s", exc)
            return 2
        print(json.dumps(summary, indent=2))
        return 0

    if args.command in ("champions-moves", "champions-species"):
        if not db.is_file():
            log.error("no database at %s -- run 'index' first", db)
            return 1
        conn = init_db(db)
        try:
            if args.command == "champions-moves":
                moves = champions_legal_moves(conn, args.species)
                if not moves:
                    log.error("no legal moves for %r in Champions", args.species)
                    return 1
                print(json.dumps(moves, indent=2))
            else:
                print(json.dumps(champions_legal_species(conn), indent=2))
        finally:
            conn.close()
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())