"""
fourslice/extstats/moveset.py

Parsers for Smogon's per-format moveset usage files, e.g.

    https://www.smogon.com/stats/<month>/moveset/<format>-<elo>.txt

A moveset file is a sequence of fixed-width, pipe-bordered blocks, one
per Pokemon (see iter_moveset_blocks for the exact shape). Two entry
points:

    find_pokemon_moveset(pokemon, txt_file)
        skim for ONE Pokemon, return all of its data (or None)
    parse_moveset_all(txt_file, db_path)
        parse the WHOLE file and store every Pokemon in SQLite
    run_in_background(fn, *args)
        thin off-main-thread wrapper (a GUI can use QThread later)

The parser is a pure line-by-line state machine: it never materialises
the whole file in memory, so multi-megabyte files are fine.
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
import threading
from pathlib import Path

from fourslice import config

from . import statsdb
from .models import utc_now

SECTION_HEADERS = {
    "Abilities", "Items", "Spreads", "Moves", "Tera Types",
    "Teammates", "Checks and Counters", "Leads",
}
LEADS_HEADER = "Common Metagame Leads"
_SECTION_KEYS = {
    "Abilities": "abilities",
    "Items": "items",
    "Spreads": "spreads",
    "Moves": "moves",
    "Tera Types": "tera_types",
    "Teammates": "teammates",
    "Leads": "leads",
}
_META_RE = re.compile(r"^(Raw count|Real count|Avg\. weight|Viability Ceiling|Weight):\s*([\d.]+)$")
_META_KEYS = {
    "Raw count": ("raw_count", int),
    "Real count": ("real_count", int),
    "Avg. weight": ("avg_weight", float),
    "Viability Ceiling": ("viability_ceiling", int),
    "Weight": ("weight", float),
}
_ENTRY_RE = re.compile(r"^(?P<name>.+?)\s+(?P<pct>[\d.]+)%$")
_CAC_RE = re.compile(
    r"^(?P<name>.+?)\s+(?P<score>[\d.]+)\s+\((?P<winrate>[\d.]+)±(?P<moe>[\d.]+)\)$"
)
_CAC_SUB_RE = re.compile(
    r"^\((?P<koed>[\d.]+)% KOed / (?P<switched>[\d.]+)% switched out\)$"
)
_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
# Valid characters for format names and elo values used in table names
# Format: lowercase letters, numbers, underscore (e.g., gen9ou, gen9vgc2024)
# Elo: digits only (e.g., 0, 1500, 1630, 1760)
_FORMAT_RE = re.compile(r"^[a-z0-9_]+$")
_ELO_RE = re.compile(r"^\d+$")


def _sanitize_table_component(name: str, pattern: re.Pattern, component_name: str) -> str:
    """Validate that a table name component matches the expected pattern."""
    if not pattern.match(name):
        raise ValueError(f"invalid {component_name} {name!r} -- must match {pattern.pattern}")
    return name


def _iter_content(lines):
    """Yield ("border", "") for +---+ lines and ("content", text) for |...|
    lines; skips blanks. Content is the inner text, stripped."""
    for raw in lines:
        stripped = raw.strip()
        if not stripped:
            continue
        if stripped.startswith("+") and stripped.endswith("+"):
            yield ("border", "")
        elif stripped.startswith("|") and stripped.endswith("|"):
            yield ("content", stripped[1:-1].strip())


def _add_meta(block: dict, text: str) -> None:
    match = _META_RE.match(text)
    if not match:
        return
    key, converter = _META_KEYS[match.group(1)]
    block[key] = converter(match.group(2))


def _add_data(block: dict, section: str | None, text: str) -> None:
    if section is None:
        return
    if section == "Checks and Counters":
        match = _CAC_RE.match(text)
        if match:
            block.setdefault("checks_counters", []).append(
                {
                    "name": match.group("name").strip(),
                    "score": float(match.group("score")),
                    "winrate": float(match.group("winrate")),
                    "moe": float(match.group("moe")),
                }
            )
            return
        sub = _CAC_SUB_RE.match(text)
        counters = block.get("checks_counters")
        if sub and counters:
            counters[-1]["koed_pct"] = float(sub.group("koed"))
            counters[-1]["switched_pct"] = float(sub.group("switched"))
        return
    match = _ENTRY_RE.match(text)
    if not match:
        return
    key = _SECTION_KEYS.get(section)
    if key:
        block.setdefault(key, []).append(
            {"name": match.group("name").strip(), "pct": float(match.group("pct"))}
        )


def iter_moveset_blocks(lines):
    """Yield one dict per Pokemon in a moveset usage file.

    Block shape (verified against smogon.com/stats/<month>/moveset/<fmt>-<elo>.txt):

        +----------------------------------------+
        | Great Tusk                             |   <- Pokemon name
        +----------------------------------------+
        | Raw count: 389490                      |   <- meta (unbordered run)
        | Avg. weight: 1                         |
        | Viability Ceiling: 92                  |
        +----------------------------------------+
        | Abilities                              |   <- section header
        | Protosynthesis 100.000%                |   <- "entry pct%"
        +----------------------------------------+
        ... one bordered section per category ...
        | Checks and Counters                    |
        | Iron Valiant 79.760 (81.02±0.32)       |
        |   (42.4% KOed / 38.6% switched out)    |
        +----------------------------------------+
        +----------------------------------------+
        | <Next Pokemon>                         |

    A content line right after a border is structural (meta, section
    header, or the next Pokemon name); a content line that follows data
    belongs to the current section. Data rows never carry a preceding
    border. A "Common Metagame Leads" section at the top is skipped.
    """
    current: dict | None = None
    section: str | None = None
    pending = False  # last event was a border -> next content is structural
    pos = 0
    for kind, text in _iter_content(lines):
        if kind == "border":
            pending = True
            continue
        if pending:
            pending = False
            if text == LEADS_HEADER:
                section = None
                continue
            if _META_RE.match(text):
                if current is not None:
                    _add_meta(current, text)
                continue
            if text in SECTION_HEADERS:
                if current is not None:
                    section = text
                continue
            # a Pokemon name: close the previous block, start a new one
            if current is not None:
                yield current
            pos += 1
            current = {"pokemon": text, "file_pos": pos}
            section = None
            continue
        # a data line (or an unbordered meta run right after the header)
        if current is None:
            continue
        if _META_RE.match(text):
            _add_meta(current, text)
            continue
        _add_data(current, section, text)
    if current is not None:
        yield current

def find_pokemon_moveset(pokemon: str, txt_file: str | Path) -> dict | None:
    """Skim a moveset usage file for ONE Pokemon and return everything the
    site reports for it (see iter_moveset_blocks for the dict shape), or
    None when the Pokemon isn't present. Case-insensitive; streams the
    file and stops as soon as the block is found.
    """
    target = (pokemon or "").strip().casefold()
    if not target:
        raise ValueError("pokemon name is required")
    with _open_moveset(txt_file) as fh:
        for block in iter_moveset_blocks(fh):
            if block["pokemon"].casefold() == target:
                return block
    return None


def _open_moveset(path: str | Path):
    if str(path).endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    return open(path, "r", encoding="utf-8", errors="replace")


def infer_context(txt_file: str | Path) -> tuple[str | None, str | None, str | None]:
    """Best-effort (period_id, format, elo) from the file's location/name,
    e.g. .../2026-07/moveset/gen9ou-0.txt -> ("2026-07", "gen9ou", "0")."""
    path = Path(txt_file)
    # layout: .../<month>/moveset/<format>-<elo>.txt -- find the month
    # by scanning the path parts rather than assuming a fixed depth
    month = next((part for part in reversed(path.parts) if _MONTH_RE.match(part)), None)
    match = re.match(r"^(.+)-(\d+)\.txt(\.gz)?$", path.name)
    fmt, elo = (match.group(1), match.group(2)) if match else (None, None)
    return month, fmt, elo


def parse_moveset_all(
    txt_file: str | Path,
    db_path: str | Path | None = None,
    *,
    period_id: str | None = None,
    format_: str | None = None,
    elo: str | None = None,
    conn=None,
    commit_every: int = 200,
) -> dict:
    """Parse the WHOLE moveset file and store every Pokemon into SQLite.

    One row per Pokemon in statsdb's `moveset` table. period_id/format/elo
    come from the explicit kwargs, else are inferred from the file path
    (e.g. .../2026-07/moveset/gen9ou-0.txt). Pass an open `conn` to share
    a caller-managed connection (e.g. one opened on a worker thread);
    otherwise a fresh connection to db_path is opened and closed here.

    Returns a summary dict like {"pokemon_count": N, "period_id": ...}.
    """
    if conn is None:
        if db_path is None:
            db_path = config.get_stats_db_path()
        conn = statsdb.init_db(str(db_path))
        own_connection = True
    else:
        own_connection = False
    try:
        if period_id is None or format_ is None or elo is None:
            inferred_period, inferred_format, inferred_elo = infer_context(txt_file)
            period_id = period_id if period_id is not None else inferred_period
            format_ = format_ if format_ is not None else inferred_format
            elo = elo if elo is not None else inferred_elo
        if not (period_id and format_ and elo is not None):
            raise ValueError(
                "period_id, format and elo are required -- pass them explicitly "
                "or use a path like <month>/moveset/<format>-<elo>.txt"
            )
        # Sanitize format and elo to prevent SQL injection in table name
        format_ = _sanitize_table_component(format_, _FORMAT_RE, "format")
        elo = _sanitize_table_component(elo, _ELO_RE, "elo")
        table_name = f"stats_{format_}_elo{elo}"
        # Ensure table exists
        conn.execute(f"CREATE TABLE IF NOT EXISTS {table_name} ({statsdb.TABLE_SCHEMA})")
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{table_name}_pokemon ON {table_name}(pokemon)")
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{table_name}_period ON {table_name}(period_id, format, elo)")

        count = 0
        with _open_moveset(txt_file) as fh:
            for block in iter_moveset_blocks(fh):
                statsdb.insert_moveset(
                    conn,
                    {
                        "period_id": period_id,
                        "format": format_,
                        "elo": elo,
                        "pokemon": block["pokemon"],
                        "file_pos": block.get("file_pos"),
                        "usage_pct": block.get("usage_pct"),
                        "raw_count": block.get("raw_count"),
                        "avg_weight": block.get("avg_weight"),
                        "viability_ceiling": block.get("viability_ceiling"),
                        "abilities": block.get("abilities"),
                        "items": block.get("items"),
                        "spreads": block.get("spreads"),
                        "moves": block.get("moves"),
                        "tera_types": block.get("tera_types"),
                        "teammates": block.get("teammates"),
                        "checks_counters": block.get("checks_counters"),
                        "stored_at": utc_now(),
                    },
                    table_name=table_name
                )
                count += 1
                if count % commit_every == 0:
                    conn.commit()
        conn.commit()
        return {
            "pokemon_count": count,
            "period_id": period_id,
            "format": format_,
            "elo": elo,
        }
    finally:
        if own_connection:
            conn.close()


def run_in_background(fn, *args, **kwargs) -> threading.Thread:
    """Run fn(*args, **kwargs) on a daemon background thread.

    Returns the Thread so the caller can join()/check alive(). GUI code
    can swap this for a QThread worker later; the pure function it wraps
    is the same either way.
    """
    thread = threading.Thread(target=fn, args=args, kwargs=kwargs, daemon=True)
    thread.start()
    return thread


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m fourslice.extstats.moveset",
        description="Parse a Smogon moveset usage file into SQLite (or print one Pokemon).",
    )
    parser.add_argument(
        "txt_file",
        help="path to a moveset file, e.g. .../2026-07/moveset/gen9ou-0.txt or gen9ou-0.txt.gz",
    )
    parser.add_argument(
        "--pokemon", default=None,
        help="print only this Pokemon's data (case-insensitive)",
    )
    parser.add_argument(
        "--db", default=None,
        help="stats DB path (default: app-data stats.db)",
    )
    parser.add_argument(
        "--period", default=None,
        help="period id, e.g. 2026-07 (default: inferred from the path)",
    )
    parser.add_argument(
        "--format", dest="format_", default=None,
        help="format, e.g. gen9ou (default: inferred from the file name)",
    )
    parser.add_argument(
        "--elo", default=None,
        help="elo bucket, e.g. 0 (default: inferred from the file name)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    import logging
    log = logging.getLogger(__name__)
    args = build_parser().parse_args(argv)
    path = Path(args.txt_file)
    if not path.is_file():
        log.error("%s is not a file", path)
        return 2
    if args.pokemon:
        block = find_pokemon_moveset(args.pokemon, path)
        if block is None:
            log.error("no moveset data for %r in %s", args.pokemon, path)
            return 1
        print(json.dumps(block, indent=2, ensure_ascii=False))
        return 0
    try:
        summary = parse_moveset_all(
            path,
            db_path=args.db,
            period_id=args.period,
            format_=args.format_,
            elo=args.elo,
        )
    except ValueError as exc:
        log.error("%s", exc)
        return 2
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
