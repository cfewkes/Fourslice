"""
fourslice/teambuilder_data.py

Pure-Python data layer for the Team Builder (Teams tab). Deliberately
Qt-free so every function is unit-testable without a display or an
event loop. It owns all of the "what is legal / how is it sorted /
what does the paste look like" logic:

* Format catalogue + rules, parsed from Showdown's shipped data files
  (data/formats.js, data/formats-data.js) so format selection, per-gen
  move legality, level defaults and ban lists all come from one source.
* A Pokemon catalogue (from the bundled pokemon_complete.db) keyed by
  that DB's `id` so roster/portrait artwork (gui/assets/<id>.png)
  resolves without guesswork.
* Move legality per generation from learnsets.db (one table per gen),
  with a National-Dex "all generations" union.
* Usage ordering from stats.db (Smogon 0-elos) so the Pokemon and move
  pickers list the metagame order first and everything else
  alphabetically afterwards.
* Team model + Showdown paste generation, and the team-vs-meta
  "top counters" math.

Every network-sourced file ships inside data/ (formats.js, pokedex.js,
formats-data.js, moves.js, abilities.js). If a file is missing the
module falls back to fetching it from Showdown; the fetch helper is
importable on its own so a CLI (or the app startup) can refresh them.

Exact legality is intentionally an approximation documented below:
tier references in ban lists ("Uber", "AG", ...) are resolved through
formats-data.js; literal move/ability bans and a small set of clause
expansions (Sleep Moves, Evasion Abilities) are handled; exotic clause
interactions are not.
"""

from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
import urllib.request
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

from fourslice import config
from fourslice.storage import get_pokemon_complete_db_path

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data file locations
# ---------------------------------------------------------------------------

def _shipped_data_dir() -> Path:
    """Read-only shipped copies of the Showdown data files (bundled with the
    app). These seed the writable runtime dir on first launch; nothing is
    ever written here."""
    return config.get_shipped_data_dir()


def _ensure_runtime_data_seeded(base_dir: Path | None = None) -> None:
    """Copy the shipped Showdown data files into the writable runtime dir on
    first use, and stamp them as fresh so they aren't immediately treated as
    stale. Best-effort: a missing/unreadable shipped file is skipped, never
    fatal. Existing runtime files (e.g. already refreshed) are left alone."""
    if base_dir is not None:
        runtime = Path(base_dir)
        runtime.mkdir(parents=True, exist_ok=True)
    else:
        runtime = config.get_showdown_data_dir()
    shipped = _shipped_data_dir()
    for name in DATA_FILES:
        rt = runtime / name
        if rt.exists():
            continue
        sh = shipped / name
        if not sh.exists():
            continue
        try:
            rt.write_bytes(sh.read_bytes())
        except OSError:
            continue
    if not _manifest_path(runtime).exists():
        seeded = [n for n in DATA_FILES if (runtime / n).exists()]
        if seeded:
            _stamp_manifest(runtime, seeded)


def _data_dir(base_dir: Path | None = None) -> Path:
    """Folder holding the downloaded Showdown data files.

    * With no argument (production): the per-user writable dir
      ``<app_data>/showdown``. On first use the shipped copies bundled with
      the app are seeded into it, so parsers have data even before the first
      network call, and the installed app's own folder is never written to.
    * With ``base_dir`` (tests): that exact directory is used as the data dir
      with the historical semantics -- callers that hand a temp dir get the
      old behaviour unchanged.
    """
    if base_dir is not None:
        d = Path(base_dir)
        d.mkdir(parents=True, exist_ok=True)
        return d
    d = config.get_showdown_data_dir()
    if not any((d / n).exists() for n in DATA_FILES):
        _ensure_runtime_data_seeded()
    return d


def _resolve_data_file(name: str, base_dir: Path | None = None) -> Path:
    """Return the runtime copy of a data file if present, else the shipped
    copy (fallback), else the runtime path anyway (so fetch can populate it)."""
    d = _data_dir(base_dir)
    rt = d / name
    if rt.exists():
        return rt
    sh = _shipped_data_dir() / name
    if sh.exists():
        return sh
    return rt


# The Showdown JS/TS data files that are seeded from the shipped read-only
# copies into the writable runtime dir, then refreshed periodically. Order
# doesn't matter; refresh is per-file.
DATA_FILES = [
    "formats.js",
    "formats-data.js",
    "pokedex.js",
    "moves.js",
    "abilities.js",
    "items.js",
    "learnsets.ts",
]

_MANIFEST_NAME = ".manifest.json"
_SHOWDOWN_BASE = "https://play.pokemonshowdown.com"

# Per-file smoke validators — called on the raw decoded Python value from
# parse_data_file(). Must return True to accept the file; False to reject.
# A rejection leaves the existing good file untouched.
def _smoke_formats(val) -> bool:
    return isinstance(val, list) and len(val) > 0 and any(
        isinstance(x, dict) and x.get("name") for x in val
    )


def _smoke_dict_nonempty(val) -> bool:
    return isinstance(val, dict) and len(val) > 0


_SMOKE_BY_FILE = {
    "formats.js": _smoke_formats,
    "formats-data.js": _smoke_dict_nonempty,
    "pokedex.js": _smoke_dict_nonempty,
    "moves.js": _smoke_dict_nonempty,
    "abilities.js": _smoke_dict_nonempty,
    "items.js": _smoke_dict_nonempty,
    "learnsets.ts": _smoke_dict_nonempty,
}


def _manifest_path(dir_path: Path) -> Path:
    return dir_path / _MANIFEST_NAME


def _load_manifest(dir_path: Path) -> dict[str, str]:
    p = _manifest_path(dir_path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}


def _stamp_manifest(dir_path: Path, names: list[str]) -> None:
    """Record the fetch time for freshly downloaded files so the launch
    freshness check (refresh_data_files_if_stale) can judge their age."""
    if not names:
        return
    manifest = _load_manifest(dir_path)
    now = datetime.now(timezone.utc).isoformat()
    for name in names:
        manifest[name] = now
    try:
        _manifest_path(dir_path).write_text(json.dumps(manifest), encoding="utf-8")
    except OSError:
        pass


def fetch_data_files(dir_path: Path | None = None, files: list[str] | None = None,
                     force: bool = False) -> dict[str, Path]:
    """Download the Showdown data files we rely on. Returns {name: path}.

    Idempotent: already-present files are left untouched unless `force`
    is set (the app never overwrites local copies transparently). Every
    successful download stamps data/.manifest.json with its fetch time so
    refresh_data_files_if_stale can age-check it later. Network failures
    are collected and reported rather than raised, so startup continues
    with whatever is on disk.

    Validate-then-commit: each file is downloaded to a temp path, decoded
    with parse_data_file(), and passed through its smoke validator. Only
    on validation success is the temp file atomically moved over the target.
    """
    d = Path(dir_path) if dir_path is not None else _data_dir()
    out: dict[str, Path] = {}
    failures: list[str] = []
    fetched: list[str] = []
    for name in files or DATA_FILES:
        target = d / name
        if target.exists() and not force:
            out[name] = target
            continue
        url = f"{_SHOWDOWN_BASE}/data/{name}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            content = urllib.request.urlopen(req, timeout=40).read()
            # Validate before commit
            tmp = target.with_suffix(".tmp")
            tmp.write_bytes(content)
            raw = parse_data_file(tmp)
            validator = _SMOKE_BY_FILE.get(name, lambda _: True)
            if not validator(raw):
                tmp.unlink(missing_ok=True)
                raise ValueError(f"smoke check failed for {name}")
            # Validation passed — atomic replace
            tmp.replace(target)
            out[name] = target
            fetched.append(name)
        except Exception as exc:  # network is best-effort
            failures.append(f"{name}: {exc}")
    if fetched:
        _stamp_manifest(d, fetched)
        _clear_data_caches()
    if failures:
        log.warning("could not fetch: %s", failures)
    return out


def refresh_data_files_if_stale(max_age_days: int = 7, files: list[str] | None = None,
                                base_dir: Path | None = None) -> list[str]:
    """App-launch freshness check for the Showdown data files.

    Any file that is missing, has no recorded fetch timestamp, or was
    fetched more than `max_age_days` ago is re-downloaded (overwriting the
    local copy) so stale tier / ban / learnset data never survives into
    the team builder. Fresh files are left untouched -- no network.

    A missing/failed download never deletes the existing file (best-effort,
    same as fetch_data_files). Returns the list of files refreshed.
    """
    d = _data_dir(base_dir)
    manifest = _load_manifest(d)
    now = datetime.now(timezone.utc)
    threshold = timedelta(days=max_age_days)
    stale: list[str] = []
    for name in files or DATA_FILES:
        target = d / name
        age_ok = False
        ts = manifest.get(name)
        if ts:
            try:
                age_ok = (now - datetime.fromisoformat(ts)) < threshold
            except ValueError:
                age_ok = False
        if not target.exists() or not age_ok:
            stale.append(name)
    if not stale:
        return []
    refreshed = list(fetch_data_files(d, stale, force=True).keys())
    if refreshed:
        log.info("refreshed data files: %s", ", ".join(sorted(refreshed)))
    return refreshed


# ---------------------------------------------------------------------------
# JS-object decoding (the Showdown data files are `exports.X = [...]` / `...{...}`)
# ---------------------------------------------------------------------------

def _decode_js(text: str, pos: int, end: int):
    """Decode a single JSON-ish JS value starting at `pos` (and the index
    just past it). Supports strings, numbers, objects, arrays, barewords,
    true/false/null/undefined and line/block comments. Returns (value, i)."""
    while pos < end:
        c = text[pos]
        if c in " \t\r\n":
            pos += 1
        elif text.startswith("//", pos):
            nl = text.find("\n", pos)
            pos = end if nl == -1 else nl + 1
        elif text.startswith("/*", pos):
            bl = text.find("*/", pos + 2)
            pos = end if bl == -1 else bl + 2
        else:
            break
    if pos >= end:
        raise ValueError(f"unexpected end at {pos}")

    c = text[pos]
    if c == "{":
        return _decode_object(text, pos, end)
    if c == "[":
        return _decode_array(text, pos, end)
    if c in ('"', "'"):
        # JSON string -- the JS dumps use double quotes, but the learnset
        # TS file uses single quotes for its move-flag arrays, so accept both.
        quote = c
        buf = []
        pos += 1
        while pos < end:
            ch = text[pos]
            if ch == "\\":
                nxt = text[pos + 1] if pos + 1 < end else ""
                buf.append({"n": "\n", "t": "\t", "r": "\r", '"': '"', "'": "'", "\\": "\\"}.get(nxt, nxt))
                pos += 2
            elif ch == quote:
                return "".join(buf), pos + 1
            else:
                buf.append(ch)
                pos += 1
        raise ValueError("unterminated string")
    if c == "-" or c.isdigit():
        j = pos
        while j < end and (text[j].isdigit() or text[j] in ".eE+-"):
            j += 1
        raw = text[pos:j]
        return float(raw) if ("." in raw or "e" in raw or "E" in raw) else int(raw), j
    # barewords
    for word, val in (("true", True), ("false", False), ("null", None), ("undefined", None)):
        if text.startswith(word, pos):
            return val, pos + len(word)
    # bare identifier (only valid for keys, but tolerate)
    j = pos
    while j < end and (text[j].isalnum() or text[j] in "_$"):
        j += 1
    if j == pos:
        raise ValueError(f"cannot parse at {pos}: {text[pos:pos + 20]!r}")
    return text[pos:j], j


def _skip_ws(text: str, pos: int, end: int) -> int:
    while pos < end:
        c = text[pos]
        if c in " \t\r\n":
            pos += 1
        elif text.startswith("//", pos):
            nl = text.find("\n", pos)
            pos = end if nl == -1 else nl + 1
        elif text.startswith("/*", pos):
            bl = text.find("*/", pos + 2)
            pos = end if bl == -1 else bl + 2
        else:
            break
    return pos


def _decode_object(text: str, pos: int, end: int):
    obj: dict = {}
    pos += 1  # consume {
    while True:
        pos = _skip_ws(text, pos, end)
        if pos >= end:
            raise ValueError("unterminated object")
        c = text[pos]
        if c == "}":
            return obj, pos + 1
        if c == ",":
            pos += 1
            continue
        key, pos = _decode_js(text, pos, end)
        while pos < end and text[pos] in " \t\r\n:":
            pos += 1
        val, pos = _decode_js(text, pos, end)
        obj[key] = val


def _decode_array(text: str, pos: int, end: int):
    arr = []
    pos += 1  # consume [
    while True:
        pos = _skip_ws(text, pos, end)
        if pos >= end:
            raise ValueError("unterminated array")
        if text[pos] == "]":
            return arr, pos + 1
        if text[pos] == ",":
            pos += 1
            continue
        val, pos = _decode_js(text, pos, end)
        arr.append(val)


def _scan_collection(text: str, pos: int, end: int):
    """Scan a top-level `{...}` or `[...]` by brace balance (robust against
    whatever the recursive tokenizer trips over inside these huge files)
    and decode it with a key-scanning splitter.

    Used for the two big top-level shapes the files ship as: array-of-
    objects (formats.js) and object-of-objects (moves/pokedex/items...).
    Returns (value, index-past-collection)."""
    pos = _skip_ws(text, pos, end)
    if pos >= end:
        raise ValueError("empty collection")
    if text[pos] == "[":
        arr: list = []
        i = pos + 1
        while i < end:
            i = _skip_ws(text, i, end)
            if i >= end:
                raise ValueError("unterminated array")
            if text[i] == "]":
                return arr, i + 1
            if text[i] == ",":
                i += 1
                continue
            # each array element is expected to be a JS value
            if text[i] == "{":
                obj, i = _scan_object(text, i, end)
                arr.append(obj)
            else:
                val, i = _decode_js(text, i, end)
                arr.append(val)
    else:
        return _scan_object(text, pos, end)
    raise ValueError("unreachable")


def _find_object_end(text: str, start: int, end: int) -> int:
    """Index just past the closing brace of the object whose `{` is at
    `start`, ignoring braces inside quoted strings."""
    depth = 0
    i = start
    while i < end:
        c = text[i]
        if c in ('"', "'"):
            _, i = _decode_js(text, i, end)
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise ValueError(f"unterminated object at {start}")


def _scan_object(text: str, pos: int, end: int):
    pos += 1  # consume {
    obj: dict = {}
    while True:
        i = _skip_ws(text, pos, end)
        if i >= end:
            raise ValueError("unterminated object")
        c = text[i]
        if c == "}":
            return obj, i + 1
        if c == ",":
            pos = i + 1
            continue
        # key
        if c in ('"', "'"):
            key, i = _decode_js(text, i, end)
        else:
            j = i
            while j < end and (text[j].isalnum() or text[j] in "_$é"):
                j += 1
            key = text[i:j]
            i = j
        # divider
        while i < end and text[i] in " \t\r\n:":
            i += 1
        # value
        vc = text[i]
        if vc == "{":
            # a nested collection -- find its balanced end, decode the chunk
            ob_end = _find_object_end(text, i, end)
            body = text[i:ob_end]
            val = _decode_object_body(body)
            i = ob_end
        elif vc == "[":
            arr_end = _find_array_end(text, i, end)
            body = text[i:arr_end]
            val = _decode_array_body(body)
            i = arr_end
        else:
            val, i = _decode_js(text, i, end)
        # reassign a captured `key` (handles `key:value` in one line)
        obj[str(key)] = val
        pos = i
    return obj, pos


def _find_array_end(text: str, start: int, end: int) -> int:
    depth = 0
    i = start
    while i < end:
        c = text[i]
        if c in ('"', "'"):
            _, i = _decode_js(text, i, end)
            continue
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise ValueError(f"unterminated array at {start}")


def _decode_object_body(body: str):
    """Decode a `{...}` chunk that has no braces nested past depth 1 from
    the collection splitter. Values are still decoded with the tokenizer."""
    inner = _scan_collection(body, 0, len(body))
    return inner[0]


def _decode_array_body(body: str):
    inner = _scan_collection(body, 0, len(body))
    return inner[0]


def parse_data_file(path: Path):
    """Decode an `exports.Name = <value>;` data file into a Python value."""
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    eq = text.find("=")
    end = text.rfind(";")
    if eq == -1:
        return None
    end = len(text) if end == -1 else end
    val, _ = _scan_collection(text, eq + 1, end)
    return val


# ---------------------------------------------------------------------------
# Formats (data/formats.js)
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def load_formats() -> list[dict]:
    """All formats in file order. Sections are dicts with only a `section` key."""
    path = _resolve_data_file("formats.js")
    if not path.exists():
        fetch_data_files()
    raw = parse_data_file(path)
    return raw if isinstance(raw, list) else []


def is_random_format(fmt: dict) -> bool:
    team = fmt.get("team") or ""
    return (
        "random" in str(team)
        or "Random Battle" in str(fmt.get("name", ""))
        or "Challenge Cup" in str(fmt.get("name", ""))
    )


def is_skip_format(fmt: dict) -> bool:
    """Helper metas a normal team builder shouldn't offer: random battles,
    custom games, drafts, multi/free-for-all and Anonymous-only formats."""
    name = str(fmt.get("name", ""))
    if is_random_format(fmt):
        return True
    if "Custom" in name or "Draft" in name or "Free-For-All" in name:
        return True
    if fmt.get("gameType") in ("multi", "freeforall"):
        return True
    return False


def get_buildable_formats() -> list[dict]:
    """Formats worth offering in the team builder (not random/custom/etc.)."""
    return [f for f in load_formats() if f.get("name") and not is_skip_format(f)]


def get_stats_backed_formats() -> list[dict]:
    """Buildable formats that actually have usage data in stats.db.

    Stats tables carry an elo divider (`stats_gen9ou_elo0`): we only care
    that the base Smogon id exists, regardless of which elo bucket, so the
    picker is never cluttered with dead formats nobody has played.
    """
    return [f for f in get_buildable_formats()
            if f.get("name") and match_stats_format(f["name"])]


def smogon_battle_counts() -> dict[str, int]:
    """{smogon format id: battles} for the latest standings period stored
    in stats.db. Empty when no standings have been stored yet."""
    conn = _stats_conn()
    try:
        from fourslice.extstats import statsdb as _statsdb
        period_id = _statsdb.get_latest_smogon_period(conn)
        if not period_id:
            return {}
        return {
            s["format"]: (s["battles"] or 0)
            for s in _statsdb.get_format_standings(conn, period_id)
        }
    except Exception:
        return {}


def order_formats_by_usage(formats: list[dict]) -> list[dict]:
    """Order format dicts by games played -- most to least -- mirroring
    the sidebar, which orders standings by battles descending.

    Formats without a standings battle count (or with no standings at all)
    keep their original relative order at the end. The sort is stable, so
    equal-battle formats stay in formats.js file order.
    """
    battles = smogon_battle_counts()
    if not battles:
        return list(formats)
    return sorted(
        formats,
        key=lambda fmt: -battles.get(match_stats_format(fmt.get("name")), 0),
    )


def find_format(name: str) -> dict | None:
    for fmt in load_formats():
        if fmt.get("name") == name:
            return fmt
    return None


def format_sections() -> list[str]:
    """Section titles in file order, for a grouped dropdown."""
    out: list[str] = []
    for fmt in load_formats():
        s = fmt.get("section")
        if s and (fmt.get("name") or "").strip() == "":
            out.append(s)
    return out


def _generation_of_mod(mod: str | None) -> int:
    m = re.match(r"gen(\d+)", mod or "")
    if m:
        return int(m.group(1))
    # champions / championsregma / gen9predlc / any other modern meta run
    # the current max generation. Delegates to learnsets._get_max_generation
    # so Gen 10+ needs no code change.
    from fourslice import learnsets
    return learnsets._get_max_generation()


def format_generation(fmt: dict) -> int | str:
    """Moves-bucket generation for a format: an int gen, or 'all' for
    National Dex style formats that pile every generation together.

    The note that drives this: if the format name contains 'genX' use
    that generation; 'natdex'/'national dex' uses every generation; and
    anything else defaults to generation 9.
    """
    name = str(fmt.get("name", "")).lower()
    if "champions" in name:
        # The Champions roster comes from the dedicated learnsets.champions
        # table, so it gets its own moves-bucket key. Checked first: the
        # name still contains '[Gen 9]' and would otherwise hit the regex.
        return "champions"
    if "natdex" in name or "national dex" in name:
        return "all"
    m = re.search(r"gen\s*(\d{1,2})", name)
    if m:
        return int(m.group(1))
    return _generation_of_mod(fmt.get("mod"))


def default_level_for_format(fmt: dict) -> int:
    name = str(fmt.get("name", "")).lower()
    if "vgc" in name or "champions" in name:
        return 50
    if "lc" in name or "little cup" in name:
        return 5
    return 100


# ---------------------------------------------------------------------------
# Roster label / id helpers
# ---------------------------------------------------------------------------

def slugify(name: str) -> str:
    """Showdown-internal id style: lowercase, only a-z0-9.
    'Landorus-Therian' / 'landorus-therian' -> 'landorustherian'.
    Stats/learnset/pokedex files all key on ids in this form."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


def display_name_from_db(name: str) -> str:
    """A presentable, Showdown-ish label for a pokemon_complete.db row:
    'abomasnow-mega' -> 'Abomasnow-Mega'. Kept separate from the pokedex
    list rendering (which the spec says to leave untouched)."""
    if not name:
        return name
    return "-".join(part.capitalize() for part in name.split("-"))


# ---------------------------------------------------------------------------
# Pokemon catalogue (pokemon_complete.db)
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _pokemon_db_path() -> Path:
    return Path(get_pokemon_complete_db_path())


def load_pokemon_catalog(db_path: str | Path | None = None) -> list[dict]:
    """Every row of pokemon_complete.db as:
        {id, name, slug, types, abilities, stats{hp,atk,def,spa,spd,spe}}
    ordered by id (national order, matching the artwork numbering).
    """
    if db_path is None:
        # No explicit path (the normal GUI path): make sure the runtime DB
        # exists. The DataRefreshWorker already rebuilt it on launch, but this
        # covers the case where Team Builder is reached without the worker
        # having run (or a fresh app-data dir). Best-effort and cheap: it only
        # builds when the runtime DB is missing or older than pokedex.js.
        # (Lazy import: pokemon_db imports this module, so a top-level import
        # here would be a circular import.)
        from fourslice.pokemon_db import ensure_pokemon_db
        ensure_pokemon_db()
    path = Path(db_path) if db_path is not None else _pokemon_db_path()
    if not path.exists():
        return []
    conn = sqlite3.connect(str(path))
    try:
        rows = conn.execute(
            "SELECT id, name, type1, type2, abilities, hp, attack, defense, sp_attack, sp_defense, speed "
            "FROM pokemon ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    out = []
    for pid, name, t1, t2, ab_str, hp, atk, df, spa, spd, spe in rows:
        out.append({
            "id": pid,
            "name": name,
            "slug": slugify(name),
            "types": [t for t in (t1, t2) if t],
            "abilities": [a.strip() for a in (ab_str or "").split(",") if a.strip()],
            "stats": {
                "hp": hp or 0, "atk": atk or 0, "def": df or 0,
                "spa": spa or 0, "spd": spd or 0, "spe": spe or 0,
            },
        })
    return out


def catalog_by_slug() -> dict[str, dict]:
    return {m["slug"]: m for m in load_pokemon_catalog()}


def art_path_of(pid: int) -> Path | None:
    """gui/assets/<id>.png -- the official artwork bundled with Fourslice,
    keyed exactly by pokemon_complete.db id. Returns None when a forme has
    no artwork (misses are common for gmax/mega/totem forms)."""
    import sys
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            d = Path(meipass) / "fourslice" / "gui" / "assets" / f"{pid}.png"
            if d.is_file():
                return d

    pkg_root = Path(__file__).resolve().parent.parent
    candidates = [
        pkg_root / "fourslice" / "gui" / "assets" / f"{pid}.png",
        Path("fourslice/gui/assets") / f"{pid}.png",
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


# ---------------------------------------------------------------------------
# Moves (data/moves.js for meta; learnsets.db for per-gen legality)
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def load_moves_meta() -> dict[str, dict]:
    """{move_id: {name, type, category, shortDesc, ...}} from moves.js."""
    path = _resolve_data_file("moves.js")
    if not path.exists():
        fetch_data_files()
    raw = parse_data_file(path)
    return raw if isinstance(raw, dict) else {}


@lru_cache(maxsize=1)
def load_abilities_meta() -> dict[str, dict]:
    """{ability_id: {name, shortDesc, ...}} from abilities.js."""
    path = _resolve_data_file("abilities.js")
    if not path.exists():
        fetch_data_files()
    raw = parse_data_file(path)
    return raw if isinstance(raw, dict) else {}


@lru_cache(maxsize=1)
def load_items_meta() -> dict[str, dict]:
    """{item_id: {name, desc, isNonstandard, ...}} from items.js, keyed by
    the slugified id ('leftovers', 'heavydutyboots') -- the same id form
    the export and the usage column use."""
    path = _resolve_data_file("items.js")
    if not path.exists():
        fetch_data_files()
    raw = parse_data_file(path)
    if not isinstance(raw, dict):
        return {}
    return {slugify(k): v for k, v in raw.items() if isinstance(v, dict)}


def item_display_name(item_id: str) -> str:
    return (load_items_meta().get(item_id) or {}).get("name") or display_name_from_db(item_id)


@lru_cache(maxsize=1)
def _learnsets_conn() -> sqlite3.Connection:
    return sqlite3.connect(str(config.get_learnsets_db_path()))


def get_generations() -> list[int]:
    """Return the list of indexed generations (1..max_gen).

    Delegates to learnsets._get_max_generation() so Gen 10+ needs no
    code change. Falls back to the historical 1..9 when the DB is absent.
    """
    from fourslice import learnsets
    max_gen = learnsets._get_max_generation()
    return list(range(1, max_gen + 1))


# Backwards-compat for code that still references the old constant.
GENERATIONS = get_generations()


@lru_cache(maxsize=1)
def _gen_table_species(gen) -> set[str]:
    conn = _learnsets_conn()
    try:
        return {r[0] for r in conn.execute(f"SELECT species FROM gen_{gen}")}
    except sqlite3.OperationalError:
        return set()


@lru_cache(maxsize=1)
def _gen_moves(gen) -> set[str]:
    """Union of every move legal in a generation (or in Champions), as
    learned by any species."""
    conn = _learnsets_conn()
    moves: set[str] = set()
    table = "champions" if gen == "champions" else f"gen_{gen}"
    try:
        for (row,) in conn.execute(f"SELECT moves FROM {table}"):
            moves |= set(json.loads(row))
    except sqlite3.OperationalError:
        pass
    return moves


@lru_cache(maxsize=1)
def _all_generations_moves() -> set[str]:
    moves: set[str] = set()
    for gen in GENERATIONS:
        moves |= _gen_moves(gen)
    return moves


def _species_learnset(gen, species_slug: str) -> set[str] | None:
    """Learnable moves for a species slug in a gen ('all' for the union of
    every generation, 'champions' for the Champions roster). Returns None
    when the species isn't present."""
    conn = _learnsets_conn()
    try:
        if gen == "all":
            out: set[str] = set()
            for g in GENERATIONS:
                row = conn.execute(f"SELECT moves FROM gen_{g} WHERE species = ?", (species_slug,)).fetchone()
                if row:
                    out |= set(json.loads(row[0]))
            return out
        table = "champions" if gen == "champions" else f"gen_{gen}"
        row = conn.execute(f"SELECT moves FROM {table} WHERE species = ?", (species_slug,)).fetchone()
        return set(json.loads(row[0])) if row else None
    except sqlite3.OperationalError:
        return None


def legal_moves_for_slug(species_slug: str, gen: int | str) -> list[str]:
    learn = _species_learnset(gen, species_slug)
    return sorted(learn) if learn else []


def legal_move_set_for_gen(gen: int | str) -> set[str]:
    return _all_generations_moves() if gen == "all" else _gen_moves(gen)


def move_display_name(move_id: str) -> str:
    meta = load_moves_meta().get(move_id)
    return (meta or {}).get("name") or display_name_from_db(move_id)


def move_meta(move_id: str) -> dict:
    return load_moves_meta().get(move_id, {})


def ability_display_name(ability_id: str) -> str:
    meta = load_abilities_meta().get(ability_id)
    return (meta or {}).get("name") or display_name_from_db(ability_id)


def type_key(t: str) -> str:
    return t.lower()


# ---------------------------------------------------------------------------
# Stats.db access (usage + counters)
# ---------------------------------------------------------------------------

def _stats_conn() -> sqlite3.Connection:
    return sqlite3.connect(str(config.get_stats_db_path()))


def available_stats_formats() -> list[str]:
    """Smogon format ids that have usage data, e.g. 'gen9ou'."""
    conn = _stats_conn()
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'stats\\_%\\_elo0' ESCAPE '\\'"
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    out = []
    for (name,) in rows:
        mid = name[len("stats_"):]
        if mid.endswith("_elo0"):
            out.append(mid[:-len("_elo0")])
    return out


def normalize_unknown_fmt(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


KNOWN_STATS_ALIASES = {
    "gen9nationaldexbss": "gen9natdexbss",
    "gen9nationaldexdoubles": "gen9nationaldexdoubles",
}


def match_stats_format(format_name: str | None) -> str | None:
    """Best-effort mapping from a formats.js name ('[Gen 9] OU',
    '[Gen 9] VGC 2026 Reg M-B') to the Smogon table id in stats.db
    ('gen9ou', 'gen9championsvgc2026regmb'). None when no usage data
    exists for the format (caller falls back to alphabetical)."""
    if not format_name:
        return None
    available = available_stats_formats()
    if not available:
        return None
    norm = normalize_unknown_fmt(format_name)
    for cand in available:
        if norm == normalize_unknown_fmt(cand):
            return cand
    # substring fallbacks: 'gen9vgc2026regmb' vs 'gen9championsvgc2026regmb'
    for cand in available:
        cnorm = normalize_unknown_fmt(cand)
        if norm and (norm in cnorm or cnorm in norm):
            return cand
    return None


def _stats_table(smogon_id: str, elo: str = "0") -> str:
    return f"stats_{smogon_id}_elo{elo}"


def pokemon_usage_rows(smogon_id: str, elo: str = "0") -> list[dict]:
    """[(name, usage_pct)] for the format's 0-elo usage, highest first."""
    conn = _stats_conn()
    table = _stats_table(smogon_id, elo)
    try:
        rows = conn.execute(
            f"SELECT pokemon, usage_pct FROM {table} WHERE elo = ? ORDER BY usage_pct DESC",
            (elo,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [{"name": r[0], "usage_pct": r[1]} for r in rows]


def move_usage_order(smogon_id: str, elo: str = "0", legal_moves: set[str] | None = None) -> dict[str, int]:
    """Move-id -> ordering rank for a format, most used first. Aggregated
    across every pokemon's moves column; moves outside `legal_moves` are
    skipped so banned/rough moves never rank."""
    conn = _stats_conn()
    table = _stats_table(smogon_id, elo)
    totals: dict[str, float] = {}
    try:
        rows = conn.execute(f"SELECT moves FROM {table} WHERE elo = ?", (elo,)).fetchall()
    except sqlite3.OperationalError:
        return {}
    for (row,) in rows:
        if not row:
            continue
        try:
            entries = json.loads(row)
        except (ValueError, TypeError):
            continue
        for e in entries:
            name = e.get("name")
            if not name or name == "Other":
                continue
            mid = slugify(name)
            if legal_moves is not None and mid not in legal_moves:
                continue
            totals[mid] = totals.get(mid, 0.0) + float(e.get("pct", 0.0))
    return {mid: rank for rank, (mid, _) in enumerate(sorted(totals.items(), key=lambda kv: (-kv[1], kv[0])))}


def item_usage_order(smogon_id: str, elo: str = "0", legal_items: set[str] | None = None) -> dict[str, int]:
    """Item-id -> ordering rank for a format, most used first. Aggregated
    across every pokemon's items column; ids outside `legal_items` are
    skipped the same way move_usage_order handles moves."""
    conn = _stats_conn()
    table = _stats_table(smogon_id, elo)
    totals: dict[str, float] = {}
    try:
        rows = conn.execute(f"SELECT items FROM {table} WHERE elo = ?", (elo,)).fetchall()
    except sqlite3.OperationalError:
        return {}
    for (row,) in rows:
        if not row:
            continue
        try:
            entries = json.loads(row)
        except (ValueError, TypeError):
            continue
        for e in entries:
            name = e.get("name")
            if not name or name == "Other":
                continue
            iid = slugify(name)
            if legal_items is not None and iid not in legal_items:
                continue
            totals[iid] = totals.get(iid, 0.0) + float(e.get("pct", 0.0))
    return {iid: rank for rank, (iid, _) in enumerate(sorted(totals.items(), key=lambda kv: (-kv[1], kv[0])))}


def _stats_list_for(column: str, name: str, smogon_id: str, elo: str = "0") -> list[dict]:
    """Read a JSON-list column of one pokemon's row in the format's stats table.

    Covers checks_counters / teammates / moves / abilities / items. The stored
    entries are display-name dicts ({'name': 'U-turn', 'pct': 92.049}), so the
    raw list is returned as-is. [] on any miss (missing table, pokemon, value
    or unparseable JSON). `column` is always a module-level literal at the
    call site, never user input.
    """
    conn = _stats_conn()
    table = _stats_table(smogon_id, elo)
    try:
        row = conn.execute(
            f"SELECT {column} FROM {table} WHERE pokemon = ? AND elo = ?", (name, elo)
        ).fetchone()
    except sqlite3.OperationalError:
        return []
    if not row or not row[0]:
        return []
    try:
        return json.loads(row[0])
    except (ValueError, TypeError):
        return []


def checks_counters_for(name: str, smogon_id: str, elo: str = "0") -> list[dict]:
    """The checks/counters list for a pokemon (display name) in a format."""
    return _stats_list_for("checks_counters", name, smogon_id, elo)


def teammates_for(name: str, smogon_id: str, elo: str = "0") -> list[dict]:
    """The teammate usage list for a pokemon (display name) in a format."""
    return _stats_list_for("teammates", name, smogon_id, elo)


def moves_for(name: str, smogon_id: str, elo: str = "0") -> list[dict]:
    """Common moves for a pokemon (display name) in a format, from stats.db."""
    return _stats_list_for("moves", name, smogon_id, elo)


def abilities_for(name: str, smogon_id: str, elo: str = "0") -> list[dict]:
    """Ability usage for a pokemon (display name) in a format, from stats.db."""
    return _stats_list_for("abilities", name, smogon_id, elo)


def items_for(name: str, smogon_id: str, elo: str = "0") -> list[dict]:
    """Common items for a pokemon (display name) in a format, from stats.db."""
    return _stats_list_for("items", name, smogon_id, elo)


# ---------------------------------------------------------------------------
# Legal / ordered pokemon list for a format
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def load_tiers() -> dict[str, dict]:
    """species id -> entry from formats-data.js {tier, doublesTier,
    natDexTier, isNonstandard, ...}."""
    path = _resolve_data_file("formats-data.js")
    if not path.exists():
        fetch_data_files()
    raw = parse_data_file(path)
    entries = raw if isinstance(raw, dict) else {}
    return {slugify(k): (v or {}) for k, v in entries.items()}


def _tier_for(slug: str, fmt: dict) -> str | None:
    entry = load_tiers().get(slug)
    if not entry:
        return None
    name = str(fmt.get("name", "")).lower()
    if "natdex" in name or "national dex" in name:
        return entry.get("natDexTier")
    if fmt.get("gameType") == "doubles":
        return entry.get("doublesTier")
    return entry.get("tier")


# Clause expansions handled natively (documented approximation).
_SLEEP_MOVES = {
    "darkvoid", "grasswhistle", "hypnosis", "lovelykiss", "relicsong",
    "sing", "sleeppowder", "spore", "yawn",
}
_EVASION_ABILITIES = {"sandveil", "snowcloak", "tangledfeet"}


def _clause_bans(fmt_ruleset: list) -> set[str]:
    bans: set[str] = set()
    for clause in fmt_ruleset or []:
        key = slugify(str(clause))
        if key in ("sleepmovesclause", "sleepclausemod"):
            bans |= _SLEEP_MOVES
        elif key == "evasionabilitiesclause":
            bans |= _EVASION_ABILITIES
    return bans


def format_legal_rulesets(fmt: dict, _seen: set | None = None) -> tuple[list, list]:
    """Merge a format's own ruleset/banlist with those of any formats it
    inherits from ('[Gen 9] UU' -> '[Gen 9] OU'). Returns (ruleset, banlist).

    Tier-looking ban tokens stay in the banlist for a second, explicit
    resolution pass that needs each pokemon's tier."  """
    _seen = _seen or set()
    name = str(fmt.get("name", ""))
    if name in _seen:
        return [], []
    _seen = _seen | {name}
    ruleset = list(fmt.get("ruleset") or [])
    banlist = list(fmt.get("banlist") or [])
    for ref in ruleset:
        if ref and not ref.startswith("!!"):
            parent = find_format(ref)
            if parent is not None:
                pr, pb = format_legal_rulesets(parent, _seen)
                ruleset = merge_keep_order(ruleset, pr)
                banlist = merge_keep_order(banlist, pb)
    return ruleset, banlist


def merge_keep_order(base: list, added: list) -> list:
    out = list(base)
    for item in added:
        if item not in out:
            out.append(item)
    return out


@lru_cache(maxsize=1)
def _item_ids() -> set[str]:
    path = _resolve_data_file("items.js")
    if path.exists():
        raw = parse_data_file(path)
        if isinstance(raw, dict):
            return {slugify(k) for k in raw}
    return set()


@lru_cache(maxsize=64)
def resolved_bans(format_name: str) -> dict:
    """Classified bans for a format, split by kind so each picker can drop
    only what it owns:

        {'moves': set, 'abilities': set, 'items': set, 'species': set,
         'tiers': set}

    Each literal ban token is resolved by membership against the move /
    ability / item / species datasets; tokens that match nothing are left
    in 'tiers' (they're tier references like 'Uber' that only matter via
    per-pokemon tier lookup). Clause expansions ('Sleep Moves',
    'Evasion Abilities') add move/ability bans on top.
    """
    fmt = find_format(format_name) or {}
    ruleset, banlist = format_legal_rulesets(fmt)
    moves: set[str] = set()
    abilities: set[str] = set()
    items: set[str] = set()
    species: set[str] = set()
    tiers: set[str] = set()

    for token in _clause_bans(ruleset):
        moves.add(token)  # clause expansion only ever bans moves/abilities
    for token in (banlist or []):
        slug = slugify(token)
        if slug in load_moves_meta():
            moves.add(slug)
        elif slug in load_abilities_meta():
            abilities.add(slug)
        elif slug in _item_ids():
            items.add(slug)
        elif slug in load_tiers():
            species.add(slug)
        else:
            tiers.add(token)
    return {"moves": moves, "abilities": abilities, "items": items, "species": species, "tiers": tiers}


def _nonstandard_blocks(species_slug: str, fmt: dict, gen: int | str) -> bool:
    """True when Showdown flags this pokemon as unusable in the format.

    Plain gen-N formats: any isNonstandard flag ('Past', 'LGPE', 'Custom',
    'Future', 'CAP' outside a CAP format) or a tier of 'Illegal' blocks the
    mon. National Dex-style 'all' formats instead follow natDexTier, so
    past-transfer mons with a natDexTier stay legal while dead forms
    (gmax, forme-less) drop out.
    """
    entry = load_tiers().get(species_slug) or {}
    ns = entry.get("isNonstandard")
    if gen == "all":
        if ns in ("Custom", "Future"):
            return True
        if ns == "CAP" and "cap" not in str(fmt.get("name", "")).lower():
            return True
        # no natDexTier => the form is not usable in National Dex
        return not entry.get("natDexTier")
    if entry.get("tier") == "Illegal":
        return True
    if ns and ns != "CAP":
        return True
    if ns == "CAP" and "cap" not in str(fmt.get("name", "")).lower():
        return True
    return False


def is_pokemon_legal_slug(species_slug: str, fmt: dict, gen: int | str | None = None) -> bool:
    """Is a species selectable in this format?

    Combination of (a) the pokemon has a gen-legal learnset, (b) Showdown
    doesn't flag it nonstandard/illegal for the format, (c) it isn't a
    literally-banned species, and (d) its tier isn't one of the format's
    banned tiers ('Uber' in OU, etc.)."""
    gen = format_generation(fmt) if gen is None else gen
    learnset = _species_learnset(gen, species_slug)
    if learnset is None:
        return False
    if gen == "champions":
        # The Champions legal roster IS the learnsets.champions table --
        # Showdown's format tiers / nonstandard flags describe the regular
        # metas, not Champions. Only the format's explicit species bans
        # still apply on top.
        bans = resolved_bans(str(fmt.get("name", "")))
        return species_slug not in bans["species"]
    if _nonstandard_blocks(species_slug, fmt, gen):
        return False
    bans = resolved_bans(str(fmt.get("name", "")))
    if species_slug in bans["species"]:
        return False
    tier = _tier_for(species_slug, fmt)
    if tier and tier in bans["tiers"]:
        return False
    return True


def ordered_pokemon_for_format(fmt: dict, catalog: list[dict] | None = None) -> list[dict]:
    """The pokemon catalogue for a format, sorted the way the picker wants:
    by Smogon 0-elo usage descending first, then everything else
    alphabetically. Only legal species are included."""
    catalog = catalog if catalog is not None else load_pokemon_catalog()
    gen = format_generation(fmt)
    smogon = match_stats_format(fmt.get("name"))
    usage_rows = pokemon_usage_rows(smogon) if smogon else []

    legal: list[dict] = []
    for mon in catalog:
        if is_pokemon_legal_slug(mon["slug"], fmt, gen):
            legal.append(mon)

    usage_rank: dict[str, int] = {}
    seen: set[str] = set()
    for rank, row in enumerate(usage_rows):
        slug = slugify(row["name"])
        usage_rank[slug] = rank
        seen.add(slug)

    used = [m for m in legal if m["slug"] in usage_rank]
    used.sort(key=lambda m: (usage_rank[m["slug"]], m["slug"]))
    unused = [m for m in legal if m["slug"] not in usage_rank]
    unused.sort(key=lambda m: m["name"])
    return used + unused


def ordered_moves_for_format(fmt: dict) -> list[dict]:
    """Move ids for a format, most used first then alphabetical, with the
    format's banned moves removed. Each entry is {id, name, type, category,
    shortDesc} so the picker can render a row without re-querying meta."""
    gen = format_generation(fmt)
    legal = legal_move_set_for_gen(gen) - resolved_bans(str(fmt.get("name", "")))["moves"]
    smogon = match_stats_format(fmt.get("name"))
    order = move_usage_order(smogon, legal_moves=legal) if smogon else {}
    used = sorted(legal, key=lambda mid: (order.get(mid, 1 << 30), mid))
    meta = load_moves_meta()
    out = []
    for mid in used:
        m = meta.get(mid, {})
        out.append({
            "id": mid,
            "name": m.get("name") or display_name_from_db(mid),
            "type": m.get("type"),
            "category": m.get("category"),
            "shortDesc": m.get("shortDesc", ""),
        })
    return out


@lru_cache(maxsize=256)
def species_usage_moves(smogon_id: str, species_name: str, elo: str = "0") -> list[tuple[str, float]]:
    """Return [(move_id, pct)] for a specific species in a format's 0-elo,
    most-used first. Reads the species' own row from stats.db."""
    conn = _stats_conn()
    table = _stats_table(smogon_id, elo)
    try:
        row = conn.execute(
            f"SELECT moves FROM {table} WHERE pokemon = ? AND elo = ?",
            (species_name, elo),
        ).fetchone()
    except sqlite3.OperationalError:
        return []
    if not row or not row[0]:
        return []
    try:
        entries = json.loads(row[0])
    except (ValueError, TypeError):
        return []
    out = []
    for e in entries:
        name = e.get("name")
        if not name or name == "Other":
            continue
        mid = slugify(name)
        out.append((mid, float(e.get("pct", 0.0))))
    out.sort(key=lambda x: (-x[1], x[0]))
    return out


@lru_cache(maxsize=256)
def ordered_species_moves(fmt_name: str, species_slug: str) -> list[dict]:
    """Per-species move picker: stats.db zero-elo moves (most->least used) first,
    then the remainder of the learnset alphabetized. Ladder moves are included
    even if absent from the static learnset (they prove format legality)."""
    fmt = find_format(fmt_name)
    if not fmt:
        return []
    gen = format_generation(fmt)
    legal = legal_move_set_for_gen(gen) - resolved_bans(fmt_name)["moves"]
    smogon = match_stats_format(fmt_name)

    # Stats moves for this species (most-used first)
    stats_moves = []
    if smogon:
        species_display = _showdown_poke_name(species_slug)
        stats_moves = species_usage_moves(smogon, species_display)

    # Static learnset for this species in this gen
    learnset = set(legal_moves_for_slug(species_slug, gen))

    meta = load_moves_meta()
    seen = set()
    out = []

    # First: stats moves (most-used first), if legal and has meta
    for mid, pct in stats_moves:
        if mid in legal and mid in meta and mid not in seen:
            m = meta[mid]
            out.append({
                "id": mid,
                "name": m.get("name") or display_name_from_db(mid),
                "type": m.get("type"),
                "category": m.get("category"),
                "shortDesc": m.get("shortDesc", ""),
            })
            seen.add(mid)

    # Then: remainder of learnset alphabetically
    for mid in sorted(learnset - seen):
        if mid in legal and mid in meta:
            m = meta[mid]
            out.append({
                "id": mid,
                "name": m.get("name") or display_name_from_db(mid),
                "type": m.get("type"),
                "category": m.get("category"),
                "shortDesc": m.get("shortDesc", ""),
            })

    return out


def ordered_items_for_format(fmt: dict) -> list[dict]:
    """Items usable in a format, most used first then alphabetical.

    Each entry is {id, name, desc}. Excludes the format's banned items and
    anything Showdown flags nonstandard (mega stones for held items etc.),
    so the picker only offers things a real team would actually hold.
    """
    banned = resolved_bans(str(fmt.get("name", "")))["items"]
    smogon = match_stats_format(fmt.get("name"))
    order = item_usage_order(smogon) if smogon else {}
    used, unused = [], []
    for iid, meta in load_items_meta().items():
        if iid in banned:
            continue
        if meta.get("isNonstandard"):
            continue
        item = {
            "id": iid,
            "name": meta.get("name") or display_name_from_db(iid),
            "desc": meta.get("desc", ""),
        }
        (used if iid in order else unused).append(item)
    used.sort(key=lambda it: (order[it["id"]], it["id"]))
    unused.sort(key=lambda it: it["name"])
    return used + unused


def ordered_abilities_for(mon: dict, fmt: dict | None = None) -> list[str]:
    """Ability ids for a pokemon (from pokemon_complete.db), display-name
    sorted, minus any the format bans outright (e.g. Shadow Tag in OU)."""
    ids = set(mon.get("abilities", []))
    if fmt:
        ids -= resolved_bans(str(fmt.get("name", "")))["abilities"]
    return sorted(ids)


# ---------------------------------------------------------------------------
# Team model + Showdown paste
# ---------------------------------------------------------------------------

STATS = ["hp", "atk", "def", "spa", "spd", "spe"]
STAT_LABELS = {"hp": "HP", "atk": "Atk", "def": "Def", "spa": "SpA", "spd": "SpD", "spe": "Spe"}
NATURES = [
    ("Hardy", "atk", "atk"), ("Lonely", "atk", "def"), ("Adamant", "atk", "spa"),
    ("Naughty", "atk", "spd"), ("Brave", "atk", "spe"), ("Bold", "def", "atk"),
    ("Docile", "def", "def"), ("Impish", "def", "spa"), ("Lax", "def", "spd"),
    ("Relaxed", "def", "spe"), ("Modest", "spa", "atk"), ("Mild", "spa", "def"),
    ("Bashful", "spa", "spa"), ("Rash", "spa", "spd"), ("Quiet", "spa", "spe"),
    ("Calm", "spd", "atk"), ("Gentle", "spd", "def"), ("Careful", "spd", "spa"),
    ("Quirky", "spd", "spd"), ("Sassy", "spd", "spe"), ("Timid", "spe", "atk"),
    ("Hasty", "spe", "def"), ("Jolly", "spe", "spa"), ("Naive", "spe", "spd"),
    ("Serious", "spe", "spe"),
]
NATURE_NAMES = [n[0] for n in NATURES]


def empty_team(name: str = "Untitled Team", format_name: str | None = None) -> dict:
    return {
        "name": name,
        "format": format_name or "",
        "slots": [empty_slot() for _ in range(6)],
    }


def empty_slot() -> dict:
    return {
        "pid": None,            # pokemon_complete.db id
        "species": None,        # DB name (e.g. 'landorus-therian')
        "nickname": "",
        "ability": "",
        "item": "",
        "moves": ["", "", "", ""],
        "level": 100,
        "gender": "-",
        "shiny": False,
        "tera": "-",
        "nature": "Serious",
        "evs": {"hp": 0, "atk": 0, "def": 0, "spa": 0, "spd": 0, "spe": 0},
        "ivs": {"hp": 31, "atk": 31, "def": 31, "spa": 31, "spd": 31, "spe": 31},
    }


def slot_stats_number(slot: dict, stat: str) -> int:
    """Actual in-game stat number for a filled slot, from base + IV + EV +
    nature + level (standard gen-3+ formula). Returns 0 for empty slots."""
    if not slot.get("pid") or not slot.get("species"):
        return 0
    base = _base_stat(slot["species"], stat)
    ev = int(slot["evs"].get(stat, 0))
    iv = int(slot["ivs"].get(stat, 31))
    level = int(slot.get("level") or 100)
    nature = str(slot.get("nature") or "Serious").title()
    if stat == "hp":
        return ((2 * base + iv + ev // 4) * level) // 100 + level + 10
    val = ((2 * base + iv + ev // 4) * level) // 100 + 5
    nature_map = dict((n, (up, dn)) for n, up, dn in NATURES)
    up, dn = nature_map.get(nature, ("atk", "atk"))
    if up == stat and dn != stat:
        return math.floor(val * 1.1)
    if dn == stat and up != stat:
        return math.floor(val * 0.9)
    return val


def stat_values(slot: dict) -> dict[str, int]:
    return {s: slot_stats_number(slot, s) for s in STATS}


def stat_bst(slot: dict) -> int:
    return sum(_base_stat(slot["species"], s) for s in STATS)


def _base_stat(species_db_name: str, stat: str) -> int:
    mon = catalog_by_slug().get(slugify(species_db_name)) or {}
    return int(mon.get("stats", {}).get(stat, 0))


def _showdown_poke_name(species_db_name: str) -> str:
    """Official Showdown species name for a DB name, via pokedex.js when
    available ('landorus-therian' -> 'Landorus-Therian')."""
    meta = load_tiers()  # formats-data is keyed like pokedex for names too
    del meta
    entry = load_pokedex_names().get(slugify(species_db_name)) or {}
    return entry.get("name") or display_name_from_db(species_db_name)


@lru_cache(maxsize=1)
def load_pokedex_names() -> dict[str, dict]:
    path = _resolve_data_file("pokedex.js")
    if not path.exists():
        fetch_data_files()
    raw = parse_data_file(path)
    entries = raw if isinstance(raw, dict) else {}
    return {slugify(k): (v or {}) for k, v in entries.items()}


def build_export(team: dict) -> str:
    """Showdown-paste style export for a whole team. Lines that would be
    empty (level at 100, '-' genders, 31 IVs, no EVs) are omitted from the
    paste, matching the reference output."""
    lines: list[str] = []
    for slot in team.get("slots", []):
        if not slot.get("pid") or not slot.get("species"):
            continue
        body, _ = export_one(slot)
        if lines:
            lines.append("")
        lines.append(body)
    return "\n".join(lines)


def _item_display_name(item_id: str) -> str:
    return item_display_name(item_id)


def export_one(slot: dict) -> tuple[str, dict]:
    """Export a single pokemon slot. Returns (paste_block, debug_dict).

    Slots store lowercase id-ish values (move ids, db species names,
    item/ability ids); the paste renders official display names so it
    imports cleanly on Showdown.
    """
    species = slot["species"]
    name = _showdown_poke_name(species)
    label = name
    if slot.get("gender") in ("M", "F"):
        label = f"{name} ({slot['gender']})"
    if slot.get("nickname"):
        label = f"{slot['nickname']} ({label})"
    if slot.get("item"):
        label += f" @ {_item_display_name(slot['item'])}"

    parts = [label]
    if slot.get("ability"):
        parts.append(f"Ability: {ability_display_name(slot['ability'])}")
    if slot.get("shiny"):
        parts.append("Shiny: Yes")
    if slot.get("tera") and slot["tera"] != "-":
        parts.append(f"Tera Type: {str(slot['tera']).capitalize()}")
    evs = slot.get("evs", {})
    ev_str = " / ".join(f"{evs[s]} {STAT_LABELS[s]}" for s in STATS if evs.get(s, 0))
    if ev_str:
        parts.append(f"EVs: {ev_str}")
    nature = slot.get("nature") or "Serious"
    if str(nature).lower() != "serious":
        parts.append(f"{str(nature).capitalize()} Nature")
    level = int(slot.get("level") or 100)
    if level != 100:
        parts.append(f"Level: {level}")
    ivs = slot.get("ivs", {})
    iv_parts = []
    for s in STATS:
        if ivs.get(s, 31) != 31:
            iv_parts.append(f"{ivs[s]} {STAT_LABELS[s]}")
    if iv_parts:
        parts.append(f"IVs: {' / '.join(iv_parts)}")
    for move in (slot.get("moves") or []):
        if move:
            parts.append(f"- {move_display_name(move)}")
    return "\n".join(parts), {"species": species, "name": name}


# ---------------------------------------------------------------------------
# Team-vs-meta counters
# ---------------------------------------------------------------------------

def compute_team_counters(team: dict, smogon_id: str | None = None, elo: str = "0",
                          limit: int = 10) -> list[dict]:
    """Top threats to the team.

    For every candidate counter X (anything that appears as a check/
    counter of any team member, or whose own check/counter list contains a
    team member):
        score(X) = sum over team members m of X's check score against m
                 - sum over X's own checks c of c's score, when c is also
                   on the team ('the team answers X back').
    Returns sorted, highest threat first.
    """
    fmt = team.get("format")
    if not fmt:
        return []
    smogon_id = smogon_id or match_stats_format(fmt)
    if not smogon_id:
        return []
    members = [s for s in team.get("slots", []) if s.get("species")]
    if not members:
        return []

    member_names = set()
    member_species = {}
    for slot in members:
        name = _showdown_poke_name(slot["species"])
        member_names.add(name)
        member_species.setdefault(name, slot)

    # Candidate X -> {counter_of_team: score, x_counts: {team member: score}}
    candidates: dict[str, dict] = {}
    for slot in members:
        name = _showdown_poke_name(slot["species"])
        for cc in checks_counters_for(name, smogon_id, elo):
            x = cc.get("name")
            if not x:
                continue
            cand = candidates.setdefault(x, {"team_hits": 0.0, "answered": 0.0})
            cand["team_hits"] += float(cc.get("score", 0.0) or 0.0)

    for slot in members:
        name = _showdown_poke_name(slot["species"])
        # for every X who is countered by this member (so the member would answer X)
        for x in candidates:
            xcc = checks_counters_for(x, smogon_id, elo)
            for cc2 in xcc:
                if cc2.get("name") == name:
                    candidates[x]["answered"] += float(cc2.get("score", 0.0) or 0.0)

    scored = [{"name": x, "score": round(d["team_hits"] - d["answered"], 2)}
              for x, d in candidates.items()]
    scored = sorted(scored, key=lambda r: (-r["score"], r["name"]))
    return scored[:limit]


def _clear_data_caches() -> None:
    """Invalidate all lru_cache loaders when source files are refreshed.

    Called after fetch_data_files replaces files, after
    build_pokemon_db_from_pokedex rewrites the runtime pokemon DB,
    and after learnsets.index_learnsets / index_champions_learnsets
    re-indexes. This ensures mid-session data freshness without
    requiring an app restart.
    """
    load_formats.cache_clear()
    load_moves_meta.cache_clear()
    load_abilities_meta.cache_clear()
    load_items_meta.cache_clear()
    load_tiers.cache_clear()
    load_pokedex_names.cache_clear()
    _item_ids.cache_clear()
    resolved_bans.cache_clear()
    _gen_table_species.cache_clear()
    _gen_moves.cache_clear()
    _all_generations_moves.cache_clear()
    _pokemon_db_path.cache_clear()
    # Sidebar module-level map (imported lazily to avoid circular deps)
    try:
        from fourslice.gui.sidebar import _load_pokemon_id_map
        _load_pokemon_id_map.__globals__["_POKEMON_ID_MAP"] = None
    except Exception:
        pass