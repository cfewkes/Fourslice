"""
Run directly with: python tests/test_learnsets.py

Offline tests (no Qt widgets, no network) for fourslice/learnsets.py:

  * the string-based TS object-literal tokenizer/parser
  * the "<generation><method><detail>" flag model
  * the nine per-generation SQLite tables + provenance meta
  * the transfer rules baked in at index time (gen <= 8 forwards,
    gen 9 native-only)
  * the query API with species-name normalisation
  * CLI subprocess smoke tests (index / moves / species)
"""

import json
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fourslice.learnsets import (
    CHAMPIONS_META_SHA,
    CHAMPIONS_META_VERSION,
    CHAMPIONS_SCHEMA_VERSION,
    SCHEMA_VERSION,
    champions_index_is_current,
    champions_legal_moves,
    champions_legal_species,
    ensure_indexed,
    index_champions_learnsets,
    index_learnsets,
    index_is_current,
    init_db,
    iter_tokens,
    legal_moves,
    legal_species,
    main,
    parse_flag,
    parse_learnsets,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

# A mini stand-in for the real data/learnsets.ts: same TS header, tab
# indentation, unquoted "return:" move key and an event-only entry.
FIXTURE_TS = (
    "export const Learnsets: import('../sim/dex-species').LearnsetDataTable = {\n"
    "\tbulbasaur: {\n"
    "\t\tlearnset: {\n"
    "\t\t\tacidspray: [\"9M\"],\n"
    "\t\t\tdoubleedge: [\"3T\"],\n"
    "\t\t\tgrowl: [\"9L9\", \"8L9\", \"7L9\", \"6L9\", \"5L9\", \"4L9\", \"3L9\"],\n"
    "\t\t\tstrength: [\"6M\", \"5M\", \"4M\", \"3M\"],\n"
    "\t\t\ttackle: [\"9L1\", \"8L1\", \"7L1\", \"6L1\", \"5L1\", \"4L1\", \"3L1\"],\n"
    "\t\t},\n"
    "\t},\n"
    "\tflamigo: {\n"
    "\t\tlearnset: {\n"
    "\t\t\tchillingwater: [\"9M\"],\n"
    "\t\t\tdoublekick: [\"9L1\"],\n"
    "\t\t},\n"
    "\t},\n"
    "\tmeowthgalar: {\n"
    "\t\tlearnset: {\n"
    "\t\t\treturn: [\"8M\", \"7M\", \"6M\"],\n"
    "\t\t},\n"
    "\t},\n"
    "\trotomheat: {\n"
    "\t\tlearnset: {\n"
    "\t\t\toverheat: [\"9R\", \"8R\", \"7R\", \"6R\", \"5R\", \"4R\"],\n"
    "\t\t\tvoltswitch: [\"9M\", \"8M\"],\n"
    "\t\t},\n"
    "\t},\n"
    "\tsneasler: {\n"
    "\t\tlearnset: {\n"
    "\t\t\tdireclaw: [\"8L1\"],\n"
    "\t\t\tliquidation: [\"8M\"],\n"
    "\t\t},\n"
    "\t},\n"
    "\tpokestargiantpropo2: {\n"
    "\t\teventData: [\n"
    "\t\t\t{generation: 5, level: 99, moves: ['crushgrip', 'stomp']},\n"
    "\t\t],\n"
    "\t},\n"
    "};\n"
)


def _indexed_fixture(tmp_dir: str):
    """Write the fixture TS into tmp_dir and index it; return
    (src_path, db_path, summary)."""
    src = Path(tmp_dir) / "learnsets.ts"
    src.write_text(FIXTURE_TS, encoding="utf-8")
    db = Path(tmp_dir) / "learnsets.db"
    return src, db, index_learnsets(src, db)


# ---------------------------------------------------------------------------
# Parser + flag-model unit tests
# ---------------------------------------------------------------------------

def test_parse_flag_forms():
    assert parse_flag("9M") == (9, "M", None)
    assert parse_flag("3L4") == (3, "L", "4")
    assert parse_flag("6S5") == (6, "S", "5")
    assert parse_flag("7V") == (7, "V", None)
    assert parse_flag("8R") == (8, "R", None)
    assert parse_flag("9m") == (9, "M", None)  # PS uses lowercase flags


def test_parse_flag_rejects_junk():
    for bad in ("M", "0M", "100M", "M9", "", "9"):
        try:
            parse_flag(bad)
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for {bad!r}")
    # two-digit gens are now valid (dynamic gen support)
    assert parse_flag("17M") == (17, "M", None)
    # a lowercase method letter is fine (PS emits "9m" etc.)
    assert parse_flag("9l1") == (9, "L", "1")


def test_iter_tokens_decodes_string_escapes():
    # the tokenizer is meant for the object literal body, so skip the
    # "x = " prologue the same way iter_species does.
    text = 'x = {"a\\nb": [1], \'c\\\\d\': [2], "e\\tf": [3]}\n'
    strings = [
        pair[1]
        for pair in iter_tokens(text, text.index("{"))
        if pair[0] == "string"
    ]
    assert strings == ["a\nb", "c\\d", "e\tf"]  # both quote styles, escapes decoded


def test_parse_learnsets_handles_header_and_unquoted_return():
    data = parse_learnsets(FIXTURE_TS)
    assert data["bulbasaur"]["strength"] == ["6M", "5M", "4M", "3M"]
    # Showdown emits bare keys (no quotes) for "return", which the
    # tokenizer must accept.
    assert data["meowthgalar"]["return"] == ["8M", "7M", "6M"]
    assert data["flamigo"]["doublekick"] == ["9L1"]


def test_parse_learnsets_skips_event_only_species():
    data = parse_learnsets(FIXTURE_TS)
    assert "pokestargiantpropo2" in data
    assert data["pokestargiantpropo2"] is None  # no learnset key
    assert len(data) == 6


def test_parse_learnsets_accepts_single_quoted_strings():
    # the real file mixes quote styles inside eventData/encounters
    # (moves: ['sheercold', 'blizzard', ...]); skipping those values
    # must not trip up parsing of the species that follow.
    source = (
        "export const X: any = {\n"
        "\ttestmon: {\n"
        "\t\tlearnset: { slam: [\"9L1\", \"3M\"] },\n"
        "\t},\n"
        "\teventonly: {\n"
        "\t\teventData: [\n"
        "\t\t\t{generation: 8, level: 70, shiny: 1,\n"
        "\t\t\t isHidden: true, moves: ['sheercold', 'blizzard'],\n"
        "\t\t\t source: \"gen8bdsp\"},\n"
        "\t\t],\n"
        "\t\tencounters: [{generation: 1, level: 50}],\n"
        "\t\teventOnly: true,\n"
        "\t},\n"
        "\tothermon: {\n"
        "\t\tlearnset: { tackle: [\"9L1\"] },\n"
        "\t},\n"
        "};\n"
    )
    data = parse_learnsets(source)
    assert data["eventonly"] is None
    assert data["testmon"]["slam"] == ["9L1", "3M"]
    assert data["othermon"]["tackle"] == ["9L1"]


# ---------------------------------------------------------------------------
# Indexing + schema
# ---------------------------------------------------------------------------

def test_index_builds_nine_gen_tables_and_meta():
    with tempfile.TemporaryDirectory() as tmp_dir:
        _, db, summary = _indexed_fixture(tmp_dir)
        assert summary["species"] == 5  # pokestar prop has no learnset
        assert summary["move_pairs"] == 12
        assert summary["rows"] > 0
        conn = init_db(db)
        try:
            tables = {
                row[0]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            for gen in range(1, 10):
                assert f"gen_{gen}" in tables, f"missing gen_{gen}"
            assert "meta" in tables
            meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
            assert meta["species_count"] == "5"
            assert meta["schema_version"] == str(SCHEMA_VERSION)
            assert len(meta["source_sha256"]) == 64
            assert "indexed_at" in meta
        finally:
            conn.close()


def test_gen_tables_are_exactly_two_columns_species_and_moves():
    with tempfile.TemporaryDirectory() as tmp_dir:
        _, db, _ = _indexed_fixture(tmp_dir)
        conn = init_db(db)
        try:
            cols = [
                row[1]
                for row in conn.execute("PRAGMA table_info(gen_8)").fetchall()
            ]
            assert cols == ["species", "moves"]
            # species is the primary key (indexes the per-species lookups)
            (pk,) = conn.execute(
                "SELECT COUNT(*) FROM pragma_table_info('gen_8') WHERE pk = 1"
            ).fetchone()
            assert pk == 1
        finally:
            conn.close()


def test_moves_column_is_sorted_json_array_of_move_ids():
    with tempfile.TemporaryDirectory() as tmp_dir:
        _, db, _ = _indexed_fixture(tmp_dir)
        conn = init_db(db)
        try:
            (raw,) = conn.execute(
                "SELECT moves FROM gen_8 WHERE species = 'bulbasaur'"
            ).fetchone()
            moves = json.loads(raw)
            assert moves == sorted(moves)
            assert all(isinstance(m, str) and m for m in moves)
            # no method/provenance detail survives -- just the move ids
            assert all(m in ("acidspray", "doubleedge", "growl", "strength", "tackle")
                       for m in moves)
        finally:
            conn.close()


def test_move_is_deduplicated_across_flags():
    with tempfile.TemporaryDirectory() as tmp_dir:
        _, db, _ = _indexed_fixture(tmp_dir)
        conn = init_db(db)
        try:
            # strength has 3M/4M/5M/6M, so it is legal in gens 3-8 but the
            # 3M/4M/5M/6M flags must collapse to a single "strength" entry.
            for gen in (3, 4, 5, 6, 7, 8):
                moves = legal_moves(conn, "bulbasaur", gen)
                assert moves.count("strength") == 1, f"gen {gen}: {moves}"
        finally:
            conn.close()


def test_legal_species_with_moves_returns_pairs():
    with tempfile.TemporaryDirectory() as tmp_dir:
        _, db, _ = _indexed_fixture(tmp_dir)
        conn = init_db(db)
        try:
            pairs = legal_species(conn, 8, with_moves=True)
            by_species = dict(pairs)
            assert list(by_species) == sorted(by_species)
            assert "strength" in by_species["bulbasaur"]
            assert by_species["sneasler"] == ["direclaw", "liquidation"]
            # without with_moves it stays a plain list of species ids
            assert legal_species(conn, 8) == list(by_species)
        finally:
            conn.close()


def test_gens_one_and_two_stay_empty():
    with tempfile.TemporaryDirectory() as tmp_dir:
        _, db, _ = _indexed_fixture(tmp_dir)
        conn = init_db(db)
        try:
            for gen in (1, 2):
                (count,) = conn.execute(f"SELECT COUNT(*) FROM gen_{gen}").fetchone()
                assert count == 0, f"gen_{gen} should be empty"
                assert legal_moves(conn, "bulbasaur", gen) == []
                assert legal_species(conn, gen) == []
        finally:
            conn.close()


def test_reindex_is_idempotent():
    with tempfile.TemporaryDirectory() as tmp_dir:
        _, db, first = _indexed_fixture(tmp_dir)
        second = index_learnsets(Path(tmp_dir) / "learnsets.ts", db)
        assert first["rows"] == second["rows"]
        assert index_is_current(db, Path(tmp_dir) / "learnsets.ts")
        conn = init_db(db)
        try:
            (rows,) = conn.execute("SELECT COUNT(*) FROM gen_9").fetchone()
        finally:
            conn.close()
        # one row per (species, gen): bulbasaur, flamigo, rotomheat
        assert rows == 3


def test_schema_version_mismatch_forces_reindex():
    with tempfile.TemporaryDirectory() as tmp_dir:
        src = Path(tmp_dir) / "learnsets.ts"
        src.write_text(FIXTURE_TS, encoding="utf-8")
        db = Path(tmp_dir) / "learnsets.db"
        ensure_indexed(src, db)
        assert index_is_current(db, src)
        # Simulate a DB built by an older schema: source hash unchanged,
        # but the schema_version is stale -> must be re-indexed.
        conn = init_db(db)
        try:
            conn.execute(
                "UPDATE meta SET value = '1' WHERE key = 'schema_version'"
            )
            conn.commit()
        finally:
            conn.close()
        assert not index_is_current(db, src)
        ensure_indexed(src, db)
        assert index_is_current(db, src)


def test_ensure_indexed_only_reindexes_when_stale():
    with tempfile.TemporaryDirectory() as tmp_dir:
        src = Path(tmp_dir) / "learnsets.ts"
        src.write_text(FIXTURE_TS, encoding="utf-8")
        db = Path(tmp_dir) / "learnsets.db"
        ensure_indexed(src, db)
        assert db.is_file()
        assert index_is_current(db, src)
        conn = init_db(db)
        try:
            (rows_before,) = conn.execute("SELECT COUNT(*) FROM gen_3").fetchone()
        finally:
            conn.close()
        with open(src, "a", encoding="utf-8") as fh:  # touch the source
            fh.write("\n// changed\n")
        assert not index_is_current(db, src)
        ensure_indexed(src, db)
        assert index_is_current(db, src)
        conn = init_db(db)
        try:
            (rows_after,) = conn.execute("SELECT COUNT(*) FROM gen_3").fetchone()
        finally:
            conn.close()
        assert rows_after == rows_before


# ---------------------------------------------------------------------------
# Transfer rules baked in at index time
# ---------------------------------------------------------------------------

def test_transfer_forward_upto_gen8_strength():
    with tempfile.TemporaryDirectory() as tmp_dir:
        _, db, _ = _indexed_fixture(tmp_dir)
        conn = init_db(db)
        try:
            for gen in (3, 4, 5, 6, 7, 8):
                assert "strength" in legal_moves(conn, "bulbasaur", gen), f"gen {gen}"
            assert "strength" not in legal_moves(conn, "bulbasaur", 9)
            assert "strength" not in legal_moves(conn, "bulbasaur", 2)
        finally:
            conn.close()


def test_gen9_only_native_moves():
    with tempfile.TemporaryDirectory() as tmp_dir:
        _, db, _ = _indexed_fixture(tmp_dir)
        conn = init_db(db)
        try:
            # acidspray is only 9M for bulbasaur: legal ONLY in gen 9
            for gen in range(3, 9):
                assert "acidspray" not in legal_moves(conn, "bulbasaur", gen)
            assert "acidspray" in legal_moves(conn, "bulbasaur", 9)
            # growl spans gens 3-9, so it's legal everywhere 3..9
            moves9 = legal_moves(conn, "bulbasaur", 9)
            assert "growl" in moves9 and "tackle" in moves9
            assert "doubleedge" not in moves9  # tutor-only does not transfer to 9
        finally:
            conn.close()


def test_rotom_catalog_special_and_transfer():
    with tempfile.TemporaryDirectory() as tmp_dir:
        _, db, _ = _indexed_fixture(tmp_dir)
        conn = init_db(db)
        try:
            # 4R..8R make overheat transfer-legal in 4-8; the 9R flag adds gen 9
            for gen in range(4, 10):
                assert "overheat" in legal_moves(conn, "rotomheat", gen), f"gen {gen}"
            assert "overheat" not in legal_moves(conn, "rotomheat", 3)
            assert "voltswitch" in legal_moves(conn, "rotomheat", 9)
        finally:
            conn.close()


def test_gen8_native_species_is_gen9_illegal():
    with tempfile.TemporaryDirectory() as tmp_dir:
        _, db, _ = _indexed_fixture(tmp_dir)
        conn = init_db(db)
        try:
            assert "direclaw" in legal_moves(conn, "sneasler", 8)
            assert "direclaw" not in legal_moves(conn, "sneasler", 9)
            assert "sneasler" in legal_species(conn, 8)
            assert "sneasler" not in legal_species(conn, 9)
            assert "flamigo" in legal_species(conn, 9)
            assert "flamigo" not in legal_species(conn, 8)
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Query API
# ---------------------------------------------------------------------------

def test_species_legality_per_generation():
    with tempfile.TemporaryDirectory() as tmp_dir:
        _, db, _ = _indexed_fixture(tmp_dir)
        conn = init_db(db)
        try:
            assert legal_species(conn, 3) == ["bulbasaur"]
            assert legal_species(conn, 8) == [
                "bulbasaur", "meowthgalar", "rotomheat", "sneasler",
            ]
            assert legal_species(conn, 9) == ["bulbasaur", "flamigo", "rotomheat"]
            assert len(legal_species(conn, 9, limit=2)) == 2
        finally:
            conn.close()


def test_species_names_are_normalised():
    with tempfile.TemporaryDirectory() as tmp_dir:
        _, db, _ = _indexed_fixture(tmp_dir)
        conn = init_db(db)
        try:
            assert legal_moves(conn, "Bulbasaur", 8) == legal_moves(conn, "bulbasaur", 8)
            # display names resolve to the stored compact id (meowth-gal -> meowthgalar)
            assert "Meowth-Galar" not in legal_species(conn, 8)
            assert "meowthgalar" in legal_species(conn, 8)
            assert legal_moves(conn, "Meowth-Galar", 8) == ["return"]
        finally:
            conn.close()


def test_legacy_database_is_migrated_on_open():
    # Simulate a DB produced by the previous per-(species, move, methods)
    # layout: old columns, no schema_version meta key.
    with tempfile.TemporaryDirectory() as tmp_dir:
        db = Path(tmp_dir) / "legacy.db"
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO meta (key, value) VALUES ('source_sha256', 'old')")
        for gen in range(1, 10):
            conn.execute(
                f"CREATE TABLE gen_{gen} ("
                "species TEXT NOT NULL, move TEXT NOT NULL, "
                "methods TEXT NOT NULL, PRIMARY KEY (species, move))"
            )
        conn.execute(
            "INSERT INTO gen_8 (species, move, methods) VALUES "
            "('bulbasaur', 'strength', '[[3,\"M\",null]]')"
        )
        conn.commit()
        conn.close()

        # Opening migrates the tables to the new two-column layout.
        conn = init_db(db)
        try:
            cols = [
                row[1]
                for row in conn.execute("PRAGMA table_info(gen_8)").fetchall()
            ]
            assert cols == ["species", "moves"]
            assert legal_moves(conn, "bulbasaur", 8) == []  # old rows dropped
        finally:
            conn.close()

        # Reindexing on top of the migrated DB repopulates it.
        src = Path(tmp_dir) / "learnsets.ts"
        src.write_text(FIXTURE_TS, encoding="utf-8")
        ensure_indexed(src, db)
        assert index_is_current(db, src)
        conn = init_db(db)
        try:
            assert "strength" in legal_moves(conn, "bulbasaur", 8)
        finally:
            conn.close()


def test_generation_bounds_are_checked():
    with tempfile.TemporaryDirectory() as tmp_dir:
        _, db, _ = _indexed_fixture(tmp_dir)
        conn = init_db(db)
        try:
            for bad in (0, 10, -1):
                try:
                    legal_moves(conn, "bulbasaur", bad)
                except ValueError:
                    continue
                raise AssertionError(f"expected ValueError for gen {bad}")
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Champions learnsets (data/mods/champions/learnsets.ts shape)
# ---------------------------------------------------------------------------

# A gen-9 base dump so the inherit-only Champions entry has a table to
# fall back into.
CHAMPIONS_INHERIT_BASE_TS = (
    "export const Learnsets: import('../sim/dex-species').LearnsetDataTable = {\n"
    "\tpikachu: {\n"
    "\t\tlearnset: {\n"
    "\t\t\tthunderbolt: [\"9M\"],\n"
    "\t\t\tquickattack: [\"9L1\"],\n"
    "\t\t},\n"
    "\t},\n"
    "};\n"
)

# The Champions mod dump: same table shape, flat "9M"-everywhere legality.
CHAMPIONS_FIXTURE_TS = (
    "export const Learnsets: import('../../../sim/dex-species').ModdedLearnsetDataTable = {\n"
    "\tpikachu: {\n"
    "\t\tinherit: true,\n"
    "\t},\n"
    "\tvenusaur: {\n"
    "\t\tlearnset: {\n"
    "\t\t\tacidspray: [\"9M\"],\n"
    "\t\t\tsludgebomb: [\"9M\"],\n"
    "\t\t},\n"
    "\t},\n"
    "};\n"
)


def _champions_fixture(tmp_dir: str):
    """Write the champions fixture (and its gen-9 base) into tmp_dir and
    index both into the same DB. Returns (src, db)."""
    base = Path(tmp_dir) / "base.ts"
    base.write_text(CHAMPIONS_INHERIT_BASE_TS, encoding="utf-8")
    db = Path(tmp_dir) / "learnsets.db"
    index_learnsets(base, db)
    src = Path(tmp_dir) / "champions.ts"
    src.write_text(CHAMPIONS_FIXTURE_TS, encoding="utf-8")
    return src, db


def test_champions_index_and_query():
    with tempfile.TemporaryDirectory() as tmp_dir:
        src, db = _champions_fixture(tmp_dir)
        summary = index_champions_learnsets(src, db)
        # pikachu comes in via the inherit->gen9 fallback; venusaur is direct.
        assert summary["species"] == 2
        assert champions_index_is_current(db, src)

        conn = init_db(db)
        try:
            assert champions_legal_moves(conn, "venusaur") == ["acidspray", "sludgebomb"]
            assert champions_legal_moves(conn, "pikachu") == ["quickattack", "thunderbolt"]
            # a species with no learnset in the champions dump is not legal
            assert champions_legal_moves(conn, "rotomheat") == []
            assert champions_legal_species(conn) == ["pikachu", "venusaur"]

            # provenance meta: schema version + source-count stamp
            ver = conn.execute(
                "SELECT value FROM meta WHERE key = ?", (CHAMPIONS_META_VERSION,)
            ).fetchone()
            assert ver is not None and ver[0] == CHAMPIONS_SCHEMA_VERSION
            count = conn.execute(
                "SELECT value FROM meta WHERE key = 'champions_species_count'"
            ).fetchone()
            assert count is not None and count[0] == "2"
        finally:
            conn.close()


def test_champions_reindexes_only_when_source_changes():
    with tempfile.TemporaryDirectory() as tmp_dir:
        src, db = _champions_fixture(tmp_dir)
        index_champions_learnsets(src, db)
        assert champions_index_is_current(db, src)
        with open(src, "a", encoding="utf-8") as fh:  # touch the source
            fh.write("\n// changed\n")
        assert not champions_index_is_current(db, src)
        index_champions_learnsets(src, db)
        assert champions_index_is_current(db, src)
        # schema mismatch (simulated stale build) forces a reindex too
        conn = init_db(db)
        try:
            conn.execute(
                "UPDATE meta SET value = '0' WHERE key = ?", (CHAMPIONS_META_VERSION,)
            )
            conn.commit()
        finally:
            conn.close()
        assert not champions_index_is_current(db, src)
        index_champions_learnsets(src, db)
        assert champions_index_is_current(db, src)


# ---------------------------------------------------------------------------
# CLI subprocess smoke tests
# ---------------------------------------------------------------------------

def _run_cli(*args) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "fourslice.learnsets", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_cli_index_moves_species_roundtrip():
    with tempfile.TemporaryDirectory() as tmp_dir:
        src, db, summary = _indexed_fixture(tmp_dir)

        result = _run_cli("index", "--src", str(src), "--db", str(db))
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["rows"] == summary["rows"]

        result = _run_cli("moves", "Bulbasaur", "8", "--db", str(db))
        assert result.returncode == 0, result.stderr
        moves = json.loads(result.stdout)
        assert "strength" in moves and "growl" in moves

        result = _run_cli("moves", "Bulbasaur", "9", "--db", str(db))
        assert result.returncode == 0, result.stderr
        assert "strength" not in json.loads(result.stdout)

        result = _run_cli("species", "8", "--db", str(db))
        assert result.returncode == 0, result.stderr
        assert "sneasler" in json.loads(result.stdout)


def test_cli_moves_without_database_fails_cleanly():
    with tempfile.TemporaryDirectory() as tmp_dir:
        result = _run_cli(
            "moves", "Bulbasaur", "8", "--db", str(Path(tmp_dir) / "missing.db")
        )
        assert result.returncode == 1
        assert "run 'index' first" in result.stderr


def test_cli_champions_roundtrip():
    with tempfile.TemporaryDirectory() as tmp_dir:
        src, db = _champions_fixture(tmp_dir)

        result = _run_cli(
            "champions-index", "--src", str(src), "--db", str(db), "--force"
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["species"] == 2

        result = _run_cli("champions-moves", "pikachu", "--db", str(db))
        assert result.returncode == 0, result.stderr
        assert "thunderbolt" in json.loads(result.stdout)

        result = _run_cli("champions-species", "--db", str(db))
        assert result.returncode == 0, result.stderr
        assert "pikachu" in json.loads(result.stdout)


# ---------------------------------------------------------------------------
# Direct-runner (mirrors test_sprites.py)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import traceback

    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception:
                failures += 1
                print(f"FAIL {name}")
                traceback.print_exc()
    sys.exit(1 if failures else 0)