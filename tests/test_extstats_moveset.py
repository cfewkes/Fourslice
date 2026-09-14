"""Offline tests for the moveset parser + stats DB.

Run with:  python -m pytest tests/test_extstats_moveset.py
"""

import json
import sqlite3

import pytest

from fourslice.extstats import moveset, statsdb

MOVESET_TEXT = """\
+----------------------------------------+
| Great Tusk                             |
+----------------------------------------+
| Raw count: 389490                      |
| Avg. weight: 1                         |
| Viability Ceiling: 92                  |
+----------------------------------------+
| Abilities                              |
| Protosynthesis 100.000%                |
+----------------------------------------+
| Items                                  |
| Heavy-Duty Boots 34.368%               |
| Booster Energy 23.006%                 |
| Other 3.434%                           |
+----------------------------------------+
| Spreads                                |
| Jolly:0/252/4/0/0/252 22.866%          |
| Other 35.087%                          |
+----------------------------------------+
| Moves                                  |
| Rapid Spin 92.049%                     |
| Other 19.383%                          |
+----------------------------------------+
| Tera Types                             |
| Steel 32.829%                          |
| Other 4.359%                           |
+----------------------------------------+
| Teammates                              |
| Kingambit 28.896%                      |
+----------------------------------------+
| Checks and Counters                    |
| Iron Valiant 79.760 (81.02±0.32)       |
|\t(42.4% KOed / 38.6% switched out)     |
| Serperior 79.400 (82.44±0.76)          |
|\t(39.8% KOed / 42.6% switched out)     |
+----------------------------------------+
+----------------------------------------+
| Gholdengo                              |
+----------------------------------------+
| Raw count: 172041                      |
| Avg. weight: 1                         |
| Viability Ceiling: 89                  |
+----------------------------------------+
| Moves                                  |
| Make It Rain 84.123%                   |
+----------------------------------------+
"""


def _write_fixture(tmp_path):
    path = tmp_path / "gen9ou-0.txt"
    path.write_text(MOVESET_TEXT, encoding="utf-8")
    return path


def test_parse_all_blocks():
    blocks = list(moveset.iter_moveset_blocks(MOVESET_TEXT.splitlines()))
    assert [b["pokemon"] for b in blocks] == ["Great Tusk", "Gholdengo"]
    g = blocks[0]
    assert g["file_pos"] == 1
    assert g["raw_count"] == 389490
    assert g["avg_weight"] == 1.0
    assert g["viability_ceiling"] == 92
    assert g["abilities"] == [{"name": "Protosynthesis", "pct": 100.0}]
    assert g["moves"][0] == {"name": "Rapid Spin", "pct": 92.049}
    assert g["spreads"][0] == {"name": "Jolly:0/252/4/0/0/252", "pct": 22.866}
    assert g["tera_types"][0] == {"name": "Steel", "pct": 32.829}
    cc = g["checks_counters"]
    assert cc[0] == {
        "name": "Iron Valiant",
        "score": 79.760,
        "winrate": 81.02,
        "moe": 0.32,
        "koed_pct": 42.4,
        "switched_pct": 38.6,
    }
    assert cc[1]["switched_pct"] == 42.6
    # block without Tera/Tera section in second pokemon stays minimal
    assert "tera_types" not in blocks[1]


def test_find_pokemon_moveset(tmp_path):
    path = _write_fixture(tmp_path)
    found = moveset.find_pokemon_moveset("great tusk", path)  # case-insensitive
    assert found is not None
    assert found["pokemon"] == "Great Tusk"
    assert found["raw_count"] == 389490
    assert found["moves"][0] == {"name": "Rapid Spin", "pct": 92.049}
    assert moveset.find_pokemon_moveset("Gholdengo", path)["raw_count"] == 172041


def test_find_pokemon_moveset_missing(tmp_path):
    path = _write_fixture(tmp_path)
    assert moveset.find_pokemon_moveset("MissingNo", path) is None


def test_infer_context(tmp_path):
    month, fmt, elo = moveset.infer_context(tmp_path / "2026-07" / "moveset" / "gen9ou-0.txt")
    assert (month, fmt, elo) == ("2026-07", "gen9ou", "0")
    month2, fmt2, elo2 = moveset.infer_context(tmp_path / "whatever.txt")
    assert month2 is None and fmt2 is None and elo2 is None


def test_parse_all_stores_in_sqlite(tmp_path):
    path = _write_fixture(tmp_path)
    db = tmp_path / "stats.db"
    summary = moveset.parse_moveset_all(path, db, period_id="2026-07", format_="gen9ou", elo="0")
    assert summary == {"pokemon_count": 2, "period_id": "2026-07", "format": "gen9ou", "elo": "0"}

    conn = statsdb.init_db(str(db))
    try:
        row = statsdb.get_moveset(conn, "2026-07", "gen9ou", "0", "Great Tusk")
        assert row is not None
        assert row["moves"][0] == {"name": "Rapid Spin", "pct": 92.049}
        assert row["checks_counters"][0]["koed_pct"] == 42.4
        assert statsdb.get_moveset(conn, "2026-07", "gen9ou", "0", "MissingNo") is None
        # re-ingesting the same file is idempotent (INSERT OR REPLACE)
        conn.execute("UPDATE stats_gen9ou_elo0 SET raw_count = 0 WHERE pokemon = 'Great Tusk'")
        moveset.parse_moveset_all(path, db, period_id="2026-07", format_="gen9ou", elo="0", conn=conn)
        assert statsdb.get_moveset(conn, "2026-07", "gen9ou", "0", "Great Tusk")["raw_count"] == 389490
    finally:
        conn.close()


def test_read_back_json_columns(tmp_path, monkeypatch):
    """teambuilder_data moves_for/abilities_for/items_for read the JSON
    columns back (display-name dicts) from a fixture-built stats.db, and
    the existing readers still work through the shared helper."""
    from fourslice import config
    import fourslice.teambuilder_data as td

    path = _write_fixture(tmp_path)
    db = tmp_path / "stats.db"
    moveset.parse_moveset_all(path, db, period_id="2026-07", format_="gen9ou", elo="0")
    monkeypatch.setattr(config, "get_stats_db_path", lambda *a, **k: db)

    assert td.moves_for("Great Tusk", "gen9ou", "0") == [
        {"name": "Rapid Spin", "pct": 92.049},
        {"name": "Other", "pct": 19.383},
    ]
    assert td.abilities_for("Great Tusk", "gen9ou", "0") == [
        {"name": "Protosynthesis", "pct": 100.0},
    ]
    assert td.items_for("Great Tusk", "gen9ou", "0") == [
        {"name": "Heavy-Duty Boots", "pct": 34.368},
        {"name": "Booster Energy", "pct": 23.006},
        {"name": "Other", "pct": 3.434},
    ]
    # regression: the readers the new helpers replaced still answer
    assert td.teammates_for("Great Tusk", "gen9ou", "0") == [
        {"name": "Kingambit", "pct": 28.896},
    ]
    # missing pokemon / missing table both come back empty, never raise
    assert td.moves_for("MissingNo", "gen9ou", "0") == []
    assert td.moves_for("Great Tusk", "gen9zzz", "0") == []


def test_parse_moveset_all_infers_context(tmp_path):
    path = tmp_path / "2026-07" / "moveset" / "gen9ou-0.txt"
    path.parent.mkdir(parents=True)
    path.write_text(MOVESET_TEXT, encoding="utf-8")
    summary = moveset.parse_moveset_all(path, tmp_path / "i.db")
    assert summary["period_id"] == "2026-07"
    assert summary["format"] == "gen9ou"
    assert summary["elo"] == "0"


def test_run_in_background(tmp_path):
    path = _write_fixture(tmp_path)
    db = tmp_path / "bg.db"
    results = {}

    def job():
        results["summary"] = moveset.parse_moveset_all(
            path, db, period_id="2026-07", format_="gen9ou", elo="0"
        )

    thread = moveset.run_in_background(job)
    thread.join(timeout=30)
    assert not thread.is_alive()
    assert results["summary"]["pokemon_count"] == 2
    conn = statsdb.init_db(str(db))
    try:
        assert statsdb.get_moveset(conn, "2026-07", "gen9ou", "0", "Gholdengo") is not None
    finally:
        conn.close()


def test_cli_prints_one_pokemon(capsys, tmp_path):
    path = _write_fixture(tmp_path)
    rc = moveset.main([str(path), "--pokemon", "gholdengo"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["pokemon"] == "Gholdengo"
    assert out["raw_count"] == 172041


def test_cli_missing_file_returns_two(capsys, tmp_path):
    rc = moveset.main([str(tmp_path / "nope.txt")])
    assert rc == 2



