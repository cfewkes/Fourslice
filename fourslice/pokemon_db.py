"""
fourslice/pokemon_db.py

Runtime ``pokemon_complete.db`` builder.

In dev the app shipped a static ``pokemon_complete.db`` (PokeAPI-derived).
That file ages out quickly for a released app: every time Showdown adds a
new Pokemon, publishing a new DB would mean a new app release. This module
rebuilds the catalogue at runtime from the already-refreshed ``pokedex.js``
(the same file that drives tier / pokedex lookups), so new Pokemon appear in
the Team Builder automatically.

The schema matches the shipped DB exactly (``id, name, type1, type2,
abilities, hp, attack, defense, sp_attack, sp_defense, speed``) so
``load_pokemon_catalog`` / ``load_pokedex_pokemon`` / the sidebar name->id
map all keep working unchanged.

ID assignment:
  * Base species keep ``id == pokedex ``num`` (national dex number), which is
    the key the bundled artwork uses (``gui/assets/<num>.png``).
  * Formes (entries with a ``forme`` field) get ``num * 1000 + k`` where ``k``
    is the 0-based index among that species' formes sorted by slug, so the
    ids are stable across rebuilds and sort right beside their base species.
    Any collision with a base-species ``num`` is resolved by bumping to the
    next free id.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from fourslice import config
from fourslice import teambuilder_data as td

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pokemon (
    id INTEGER PRIMARY KEY,
    name TEXT UNIQUE,
    type1 TEXT,
    type2 TEXT,
    abilities TEXT,
    hp INTEGER,
    attack INTEGER,
    defense INTEGER,
    sp_attack INTEGER,
    sp_defense INTEGER,
    speed INTEGER
)
"""

# Abilities are stored comma-joined in deterministic order (0/1/2/3 = normal,
# S = signature, H = hidden). Only the names are kept -- the renderer looks
# display names up in abilities.js separately.
_ABILITY_KEY_ORDER = ("0", "1", "2", "3", "S", "H")


def build_pokemon_db_from_pokedex(
    out_path: Path,
    base_dir: Path | None = None,
) -> Path:
    """(Re)create ``out_path`` from the current pokedex.js runtime copy.

    Returns ``out_path``. Best-effort: a missing or unparseable pokedex.js
    leaves any existing runtime DB untouched.
    """
    pokedex_path = td._resolve_data_file("pokedex.js", base_dir)
    if not pokedex_path.exists():
        return out_path

    try:
        raw = td.parse_data_file(pokedex_path)
    except Exception as exc:
        # Unparseable source (format drift, truncated download): never
        # replace a good DB with nothing.
        log.warning("build_pokemon_db_from_pokedex: cannot parse pokedex.js: %s", exc)
        return out_path
    if not isinstance(raw, dict) or not raw:
        return out_path

    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(".tmp")
    conn = sqlite3.connect(str(tmp))
    try:
        conn.execute("DROP TABLE IF EXISTS pokemon")
        conn.execute(_SCHEMA)

        # Group entries by national number so formes sit beside their base.
        by_num: dict[int, list[tuple[str, dict]]] = {}
        for slug, entry in raw.items():
            if not isinstance(entry, dict):
                continue
            num = entry.get("num")
            if not isinstance(num, int) or num <= 0:
                continue  # joke placeholders (num 0)
            by_num.setdefault(num, []).append((slug, entry))

        base_ids: set[int] = set()
        for num, entries in sorted(by_num.items()):
            if any("forme" not in e for _s, e in entries):
                base_ids.add(num)

        used: set[int] = set()
        rows: list[tuple[int, dict]] = []
        for num, entries in sorted(by_num.items()):
            base = next((e for s, e in entries if "forme" not in e), None)
            if base is not None:
                rows.append((num, _to_row(base)))
                used.add(num)
            formes = [(s, e) for s, e in entries if "forme" in e]
            formes.sort(key=lambda se: se[0])
            for k, (_slug, entry) in enumerate(formes):
                fid = num * 1000 + k
                while fid in used or fid in base_ids:
                    fid += 1
                rows.append((fid, _to_row(entry)))
                used.add(fid)

        # National order by id == num order, formes adjacent to their base.
        rows.sort(key=lambda r: r[0])
        conn.executemany(
            "INSERT OR REPLACE INTO pokemon "
            "(id, name, type1, type2, abilities, hp, attack, defense, sp_attack, sp_defense, speed) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [(fid, row["name"], row["type1"], row["type2"], row["abilities"],
              row["hp"], row["attack"], row["defense"],
              row["sp_attack"], row["sp_defense"], row["speed"])
             for fid, row in rows],
        )
        conn.commit()
    finally:
        conn.close()
    tmp.replace(out_path)
    return out_path


def _to_row(entry: dict) -> dict:
    types = entry.get("types") or []
    base_stats = entry.get("baseStats") or {}
    abilities = entry.get("abilities") or {}
    # pokedex.js stores display names ("Magic Bounce") but everything
    # downstream -- abilities.js keys, ban sets, the shipped DB, slot ids --
    # uses lowercase ids ("magicbounce"), so slugify like td.slugify does.
    ordered = [td.slugify(str(abilities[k])) for k in _ABILITY_KEY_ORDER if abilities.get(k)]
    return {
        "name": entry.get("name") or "",
        "type1": types[0] if types else None,
        "type2": types[1] if len(types) > 1 else None,
        "abilities": ", ".join(ordered),
        "hp": int(base_stats.get("hp") or 0),
        "attack": int(base_stats.get("atk") or 0),
        "defense": int(base_stats.get("def") or 0),
        "sp_attack": int(base_stats.get("spa") or 0),
        "sp_defense": int(base_stats.get("spd") or 0),
        "speed": int(base_stats.get("spe") or 0),
    }


def ensure_pokemon_db(base_dir: Path | None = None) -> Path:
    """Make sure the runtime pokemon_complete.db exists and is current.

    Rebuilds it from pokedex.js when the runtime DB is missing or older than
    the pokedex.js it would be built from (i.e. the source has changed since
    it was last built). Best-effort -- a failed build leaves the existing DB
    (or the shipped copy) in place. Returns the DB path resolvers will use.
    """
    out = config.get_pokemon_db_path(base_dir)
    pokedex_path = td._resolve_data_file("pokedex.js", base_dir)

    if out.exists() and (not pokedex_path.exists()
                         or out.stat().st_mtime >= pokedex_path.stat().st_mtime):
        return out  # current

    try:
        build_pokemon_db_from_pokedex(out, base_dir)
    except Exception:
        return out  # best-effort: keep existing installation intact
    return out