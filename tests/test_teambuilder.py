"""
Run directly with: python tests/test_teambuilder.py

Unit + wiring tests for the Team Builder (Teams tab) -- slice 2: the
pure data layer (fourslice/teambuilder_data.py) and the Qt wiring that
glues it to the TeamsTab UI. The data-layer half needs no display; the
wiring half reuses the smoke-test convention of forcing Qt's
"offscreen" platform so nothing pops a real window.

Covers (data layer): the empty slot/team model shape; format defaults
(level, generation, natdex="all"); the usage-then-alphabetical pokemon
picker order with illegal species excluded; Showdown paste export
omitting "-" placeholders; the gen-3+ stat formula with nature; and the
team-vs-meta counter math.

Covers (wiring): TeamsTab constructs; picking a species fills the slot
and opens the edit panel at the format-default level; picking a move
writes into the pending move slot; the EV editor caps a stat at 252 and
the total at 508; and a save/load round-trip keeps the picked species.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from fourslice import teambuilder_data as td
from fourslice.storage import (
    delete_tb_team, get_tb_team, get_tb_teams, init_db,
    save_tb_team, update_tb_team,
)


# ---------------------------------------------------------------------------
# Data layer
# ---------------------------------------------------------------------------

def test_empty_team_and_slot_shape():
    slot = td.empty_slot()
    assert slot["pid"] is None
    assert slot["species"] is None
    assert slot["nickname"] == ""
    assert slot["ability"] == ""
    assert slot["item"] == ""
    assert slot["moves"] == ["", "", "", ""]
    assert slot["level"] == 100
    assert slot["gender"] == "-"
    assert slot["shiny"] is False
    assert slot["tera"] == "-"
    assert slot["nature"] == "Serious"
    assert all(slot["evs"][s] == 0 for s in td.STATS)
    assert all(slot["ivs"][s] == 31 for s in td.STATS)

    team = td.empty_team("New Team", "[Gen 9] OU")
    assert team["name"] == "New Team"
    assert team["format"] == "[Gen 9] OU"
    assert len(team["slots"]) == 6
    assert all(s["species"] is None for s in team["slots"])
    print("PASS: empty team/slot model matches the spec defaults")


def test_format_generation():
    assert td.format_generation({"name": "[Gen 9] OU", "mod": "gen9"}) == 9
    assert td.format_generation({"name": "[Gen 5] UU", "mod": "gen5"}) == 5
    assert td.format_generation({"name": "NatDex OU", "mod": "gen9"}) == "all"
    assert td.format_generation({"name": "National Dex AG", "mod": "gen9"}) == "all"
    # any name containing 'champions' sources from the champions learnset
    # table (checked before the gen regex, since these names carry '[Gen 9]')
    assert td.format_generation({"name": "Champions", "mod": "gen9"}) == "champions"
    assert td.format_generation({"name": "[Gen 9 Champions] VGC 2026 Reg M-B"}) == "champions"
    # non-gen-named, non-natdex, non-champions -> generation 9
    assert td.format_generation({"name": "Custom Game", "mod": "gen9"}) == 9
    print("PASS: format generation resolves champions / genX / natdex / default-to-9")


def test_default_level_for_format():
    assert td.default_level_for_format({"name": "VGC 2025 Reg G"}) == 50
    assert td.default_level_for_format({"name": "Champions"}) == 50
    assert td.default_level_for_format({"name": "[Gen 9] LC"}) == 5
    assert td.default_level_for_format({"name": "Little Cup"}) == 5
    assert td.default_level_for_format({"name": "[Gen 9] OU"}) == 100
    print("PASS: default level is 50 for VGC/Champions, 5 for LC, else 100")


def test_showdown_poke_name():
    assert td._showdown_poke_name("garchomp") == "Garchomp"
    assert td._showdown_poke_name("landorus-therian") == "Landorus-Therian"
    assert td._showdown_poke_name("abomasnow-mega") == "Abomasnow-Mega"
    print("PASS: DB species names render as official Showdown names")


def test_catalog_abilities_have_no_leading_whitespace():
    """pokemon_complete.db stores 'chlorophyll, overgrow'; the catalog must
    strip so the Ability dropdown and legality checks see clean ids."""
    catalog = td.load_pokemon_catalog()
    for mon in catalog:
        for a in mon.get("abilities", []):
            assert a == a.strip(), f"{mon['slug']}: {a!r}"
    assert "overgrow" in td.catalog_by_slug()["bulbasaur"]["abilities"]
    print("PASS: catalog ability ids are whitespace-free")


def test_ordered_pokemon_for_format_usage_then_alpha():
    ou = td.find_format("[Gen 9] OU")
    assert ou is not None
    catalog = td.load_pokemon_catalog()
    ordered = td.ordered_pokemon_for_format(ou, catalog)

    # illegal species are excluded (missingno is banned everywhere)
    slugs = [m["slug"] for m in ordered]
    assert "missingno" not in slugs
    # usage leaders appear before the alphabetical tail
    assert slugs.index("kingambit") < slugs.index("abomasnow")
    # every legal entry carries a usable id + name
    for m in ordered[:20]:
        assert m["id"] and m["name"]
    print(f"PASS: {len(ordered)} legal pokemon, usage-sorted with alpha tail")


def test_slot_stats_number_formula_and_nature():
    slot = td.empty_slot()
    slot["pid"] = 445
    slot["species"] = "garchomp"
    slot["nature"] = "Jolly"          # +Spe -SpA
    slot["evs"] = {"atk": 252, "spd": 4, "spe": 252}
    slot["ivs"] = {s: 31 for s in td.STATS}

    # hp formula: ((2*108 + 31 + 0)*100)//100 + 100 + 10
    assert td.slot_stats_number(slot, "hp") == ((2 * 108 + 31) * 100) // 100 + 110
    # atk: ((2*130 + 31 + 252//4)*100)//100 + 5, no nature mod
    assert td.slot_stats_number(slot, "atk") == ((2 * 130 + 31 + 63) * 100) // 100 + 5
    # spe: Jolly raises spe +10%
    spe_neutral = ((2 * 102 + 31 + 63) * 100) // 100 + 5
    assert td.slot_stats_number(slot, "spe") == spe_neutral * 1.1 // 1
    # spa: Jolly lowers spa -10%
    spa_neutral = ((2 * 80 + 31 + 0) * 100) // 100 + 5
    assert td.slot_stats_number(slot, "spa") == spa_neutral * 0.9 // 1
    # empty slot -> 0
    assert td.slot_stats_number(td.empty_slot(), "atk") == 0
    print("PASS: stat formula honours IV/EV/level and the nature multiplier")


def test_export_one_showdown_paste():
    slot = td.empty_slot()
    slot["pid"] = 445
    slot["species"] = "garchomp"
    slot["ability"] = "rough-skin"
    slot["moves"] = ["earthquake", "stealthrock", "", ""]
    slot["level"] = 100
    slot["gender"] = "M"
    slot["tera"] = "dragon"
    slot["nature"] = "Jolly"
    slot["evs"] = {"atk": 252, "spd": 4, "spe": 252}

    body, dbg = td.export_one(slot)
    assert dbg["species"] == "garchomp"
    assert dbg["name"] == "Garchomp"
    lines = body.splitlines()
    assert lines[0] == "Garchomp (M)"
    assert "Ability: Rough-Skin" in lines
    assert "Tera Type: Dragon" in lines
    assert "EVs: 252 Atk / 4 SpD / 252 Spe" in lines
    assert "Jolly Nature" in lines
    assert "- Earthquake" in lines
    assert "- Stealth Rock" in lines
    # "-" placeholders are excluded
    assert "Level:" not in lines, "level 100 is omitted"
    assert "Shiny:" not in lines
    assert "@" not in lines[0], "no item set -> no @ suffix"
    print("PASS: export_one emits a clean Showdown paste")


def test_build_export_skips_empty_slots():
    team = td.empty_team("t", "[Gen 9] OU")
    team["slots"][0]["pid"] = 445
    team["slots"][0]["species"] = "garchomp"
    team["slots"][0]["moves"] = ["earthquake", "", "", ""]
    text = td.build_export(team)
    blocks = [b for b in text.split("\n\n") if b.strip()]
    assert len(blocks) == 1, "only the one filled slot is exported"
    assert "Garchomp" in text
    assert text.count("- Earthquake") == 1
    print("PASS: build_export drops empty roster slots")


def test_compute_team_counters_score_desc():
    team = td.empty_team("t", "[Gen 9] OU")
    team["slots"][0]["pid"] = 445
    team["slots"][0]["species"] = "garchomp"
    team["slots"][1]["pid"] = 1
    team["slots"][1]["species"] = "landorus-therian"

    rows = td.compute_team_counters(team, limit=5)
    assert rows, "expected some threats for a 2-mon team"
    assert all("name" in r and "score" in r for r in rows)
    scores = [r["score"] for r in rows]
    assert scores == sorted(scores, reverse=True), "counters sorted highest first"
    assert len(rows) <= 5
    print(f"PASS: {len(rows)} counters sorted highest-threat-first")


def test_storage_round_trip_retains_species():
    conn = init_db(":memory:")
    team = td.empty_team("RoundTrip", "[Gen 9] OU")
    team["slots"][0]["pid"] = 445
    team["slots"][0]["species"] = "garchomp"
    team["slots"][0]["moves"] = ["earthquake", "", "", ""]

    tid = save_tb_team(conn, team["name"], team["format"], team)
    loaded = get_tb_team(conn, tid)
    assert loaded is not None
    assert loaded["name"] == "RoundTrip"
    assert loaded["format"] == "[Gen 9] OU"
    assert loaded["data"]["slots"][0]["species"] == "garchomp"
    assert loaded["data"]["slots"][0]["moves"][0] == "earthquake"
    assert loaded["data"]["slots"][0]["pid"] == 445

    # get_tb_teams lists it
    names = [t["name"] for t in get_tb_teams(conn)]
    assert "RoundTrip" in names

    # update + delete
    update_tb_team(conn, tid, "Renamed", "[Gen 9] UU", team)
    assert get_tb_team(conn, tid)["name"] == "Renamed"
    delete_tb_team(conn, tid)
    assert get_tb_team(conn, tid) is None
    print("PASS: team storage round-trips species/moves and update/delete")


def test_ordered_items_legal_usage_first():
    ou = td.find_format("[Gen 9] OU")
    items = td.ordered_items_for_format(ou)
    assert items, "expected some legal items"
    ids = [i["id"] for i in items]
    # usage leaders first: Leftovers / Heavy-Duty Boots rank at the top
    assert ids[0] == "leftovers", ids[:3]
    assert "heavydutyboots" in ids[:5]
    # nonstandard items (mega stones) are excluded
    assert "abomasite" not in ids
    assert "leftoversz" not in ids
    # banned items for the format are excluded
    banned = td.resolved_bans("[Gen 9] OU")["items"]
    assert not (banned & set(ids)), "no format-banned items in the picker"
    # every entry carries a display name + description
    for i in items[:10]:
        assert i["name"] and "desc" in i
    print(f"PASS: {len(items)} legal items, usage-ordered, no mega stones or banned items")


def test_export_renders_item():
    slot = td.empty_slot()
    slot["pid"] = 445
    slot["species"] = "garchomp"
    slot["item"] = "leftovers"
    slot["moves"] = ["earthquake", "", "", ""]
    body, _ = td.export_one(slot)
    assert body.splitlines()[0] == "Garchomp @ Leftovers"
    assert "Leftovers" in body
    print("PASS: export renders the held item after the species name")


def test_stats_backed_formats_ignore_elo_divider():
    backed = td.get_stats_backed_formats()
    assert backed, "expected stats-backed formats"
    assert all(f.get("name") for f in backed)
    assert all(td.match_stats_format(f["name"]) for f in backed)
    # the elo divider is ignored: match_stats_format resolves the base id
    assert td.match_stats_format("[Gen 9] OU") == "gen9ou"
    assert td.match_stats_format("[Gen 9] OU") != "gen9ou_elo0"
    print(f"PASS: {len(backed)} formats backed by stats.db (elo divider ignored)")


def test_move_learnset_restricts_selection():
    learn = set(td.legal_moves_for_slug("garchomp", 9))
    assert "earthquake" in learn
    assert "stealthrock" in learn
    assert "recover" not in learn
    print("PASS: per-species learnset restricts which moves are selectable")


def test_data_freshness_manifest():
    """refresh_data_files_if_stale re-downloads stale/unstamped files and
    skips fresh ones -- no network hit when everything is current."""
    import tempfile
    data_dir = Path(tempfile.mkdtemp())
    (data_dir / "formats.js").write_text("stale")

    calls = []
    real_fetch = td.fetch_data_files

    def fake_fetch(dir_path, files, force=False):
        calls.append((list(files or []), force))
        got = {}
        for name in files or []:
            (Path(dir_path) / name).write_bytes(b"fresh")
            got[name] = Path(dir_path) / name
        td._stamp_manifest(Path(dir_path), list(got.keys()))
        return got

    td.fetch_data_files = fake_fetch
    try:
        # 1) file present but unstamped -> refreshed
        assert td.refresh_data_files_if_stale(7, files=["formats.js"], base_dir=data_dir) == ["formats.js"]
        assert calls == [(["formats.js"], True)]

        # 2) freshly stamped -> no fetch
        calls.clear()
        assert td.refresh_data_files_if_stale(7, files=["formats.js"], base_dir=data_dir) == []
        assert calls == []

        # 3) backdated stamp -> refreshed again
        manifest = json.loads((data_dir / ".manifest.json").read_text())
        manifest["formats.js"] = "2020-01-01T00:00:00+00:00"
        (data_dir / ".manifest.json").write_text(json.dumps(manifest))
        calls.clear()
        assert td.refresh_data_files_if_stale(7, files=["formats.js"], base_dir=data_dir) == ["formats.js"]
        assert calls == [(["formats.js"], True)]
    finally:
        td.fetch_data_files = real_fetch
    print("PASS: freshness checker fetches stale files and skips fresh ones")


# ---------------------------------------------------------------------------
# pokemon_complete.db builder (Step 3) -- pokemon_db.build_pokemon_db_from_pokedex
# ---------------------------------------------------------------------------

_FIXTURE_POKEDEX = """export const Pokedex: {[k: string]: SpeciesData} = {
	bulbasaur: {
		name: "Bulbasaur",
		num: 1,
		types: ["Grass", "Poison"],
		baseStats: {hp: 53, atk: 78, def: 66, spa: 65, spd: 66, spe: 45},
		abilities: {0: "Overgrow", H: "Chlorophyll"},
	},
	venusaur: {
		name: "Venusaur",
		num: 3,
		types: ["Grass", "Poison"],
		baseStats: {hp: 104, atk: 106, def: 101, spa: 104, spd: 100, spe: 68},
		abilities: {0: "Overgrow", 1: "Chlorophyll", H: "Thick Fat"},
	},
	venusaurmega: {
		name: "Venusaur-Mega",
		num: 3,
		forme: "Mega",
		types: ["Grass", "Poison"],
		baseStats: {hp: 104, atk: 106, def: 113, spa: 122, spd: 113, spe: 78},
		abilities: {0: "Thick Fat"},
	},
	missingno: {
		name: "MissingNo.",
		num: 0,
		types: ["Bird"],
		baseStats: {hp: 33, atk: 136, def: 0, spa: 6, spd: 6, spe: 29},
		abilities: {0: "Soundproof"},
	},
	pikachu: {
		name: "Pikachu",
		num: 25,
		types: ["Electric"],
		baseStats: {hp: 35, atk: 55, def: 40, spa: 50, spd: 50, spe: 90},
		abilities: {0: "Static", H: "Lightning Rod"},
	},
}"""


def _build_fixture_db(tmp: Path, pokedex_text: str | None = None) -> Path:
    """Write a minimal pokedex.js into a temp shipped-style data dir and build
    a pokemon_complete.db from it. Returns the DB path."""
    data_dir = tmp / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "pokedex.js").write_text(pokedex_text if pokedex_text is not None else _FIXTURE_POKEDEX)
    from fourslice import pokemon_db
    out = tmp / "pokemon_complete.db"
    return pokemon_db.build_pokemon_db_from_pokedex(out, base_dir=data_dir)


def test_builder_reads_pokedex_into_catalog():
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        db = _build_fixture_db(Path(tmp))
        assert db.exists()
        # catalog reads the freshly built DB
        catalog = td.load_pokemon_catalog(db)
        names = {m["name"] for m in catalog}
        assert names == {"Bulbasaur", "Venusaur", "Venusaur-Mega", "Pikachu"}
        assert "missingno" not in names, "num 0 placeholders are skipped"
        bulba = next(m for m in catalog if m["name"] == "Bulbasaur")
        assert bulba["id"] == 1
        assert bulba["types"] == ["Grass", "Poison"]
        # abilities comma-joined in key order (0 then H), whitespace-free
        assert bulba["abilities"] == ["overgrow", "chlorophyll"]
        assert bulba["stats"]["hp"] == 53 and bulba["stats"]["spe"] == 45
        venusaur = next(m for m in catalog if m["name"] == "Venusaur")
        mega = next(m for m in catalog if m["name"] == "Venusaur-Mega")
        # base keeps national number; forme gets a deterministic offset id
        assert venusaur["id"] == 3
        assert mega["id"] != 3 and mega["id"] > 3
        # forme keeps its own name/abilities; base species name unchanged
        # abilities are slugified to the id convention ("thickfat") like the
        # shipped DB and abilities.js keys, not the pokedex display name
        assert mega["abilities"] == ["thickfat"]
        print("PASS: builder turns a pokedex.js fixture into a usable catalog")

def test_builder_forme_ids_deterministic_and_noncolliding():
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        db = _build_fixture_db(Path(tmp))
        catalog = {m["name"]: m for m in td.load_pokemon_catalog(db)}
        # rebuilt from the same source -> identical ids (build twice)
        db2 = Path(tmp) / "pokemon_complete2.db"
        from fourslice import pokemon_db
        pokemon_db.build_pokemon_db_from_pokedex(db2, base_dir=Path(tmp) / "data")
        mega2 = {m["name"]: m for m in td.load_pokemon_catalog(db2)}["Venusaur-Mega"]
        assert catalog["Venusaur-Mega"]["id"] == mega2["id"]
        # no id is duplicated across the catalogue
        ids = [m["id"] for m in td.load_pokemon_catalog(db)]
        assert len(ids) == len(set(ids))
        print(f"PASS: forme ids are deterministic and unique (mega id = {catalog['Venusaur-Mega']['id']})")


def test_builder_best_effort_on_unparseable_source():
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "pokemon_complete.db"
        # a pre-existing runtime DB must survive a corrupt pokedex.js
        db.write_bytes(b"SQLite prior copy")
        data_dir = Path(tmp) / "data"
        data_dir.mkdir(parents=True)
        (data_dir / "pokedex.js").write_text("export const Pokedex = NOT_JAVASCRIPT")
        from fourslice import pokemon_db
        out = pokemon_db.build_pokemon_db_from_pokedex(db, base_dir=data_dir)
        assert out == db
        assert db.read_bytes() == b"SQLite prior copy", "failed build keeps old DB"
        print("PASS: an unparseable pokedex.js leaves the existing DB untouched")

def _app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication(sys.argv)


def _make_tab():
    from fourslice.gui.teams_tab import TeamsTab
    return TeamsTab(init_db(":memory:"))


def test_teams_tab_constructs_and_picks_species():
    _app()
    from PySide6.QtWidgets import QStackedWidget
    tab = _make_tab()

    assert tab.team["slots"][0]["species"] is None
    tab._active_slot_index = 0
    tab.pick_species("garchomp", 445)

    slot = tab.team["slots"][0]
    assert slot["pid"] == 445
    assert slot["species"] == "garchomp"
    assert slot["level"] == 100, "OU defaults to level 100"
    assert tab.content_stack.currentWidget() is tab.edit_panel, \
        "picking opens the edit panel"
    print("PASS: pick_species fills the slot and opens the edit panel")


def test_pick_move_writes_pending_slot():
    _app()
    tab = _make_tab()
    tab._active_slot_index = 0
    tab.pick_species("garchomp", 445)

    tab._pending_move_idx = 0
    tab.pick_move("earthquake")
    assert tab.team["slots"][0]["moves"][0] == "earthquake"

    tab._pending_move_idx = 2
    tab.pick_move("stealthrock")
    assert tab.team["slots"][0]["moves"][2] == "stealthrock"
    assert tab.team["slots"][0]["moves"][1] == ""
    print("PASS: pick_move writes into the chosen pending move slot")


def test_ev_editor_caps_stat_and_total():
    _app()
    tab = _make_tab()
    tab._active_slot_index = 0
    tab.pick_species("garchomp", 445)

    tab.open_ev_editor_for("atk")
    assert tab.content_stack.currentWidget() is tab.ev_editor
    editor = tab.ev_editor

    editor._bump("atk", 4)   # 4
    editor._bump("atk", 4)   # 8
    assert editor.slot["evs"]["atk"] == 8
    editor._bump("atk", 200)  # 208
    editor._bump("atk", 200)  # capped at 252
    assert editor.slot["evs"]["atk"] == 252
    # bumping above the 508 total across stats is refused
    editor._bump("def", 252)
    editor._bump("spa", 252)
    editor._bump("spd", 4)
    assert sum(editor.slot["evs"].values()) == 508
    editor._bump("hp", 4)
    assert sum(editor.slot["evs"].values()) == 508, "508 total cap holds"
    print("PASS: EV editor caps a stat at 252 and the team total at 508")


def test_save_via_ui_round_trips():
    _app()
    tab = _make_tab()
    tab._active_slot_index = 0
    tab.pick_species("garchomp", 445)
    tab.team["name"] = "UI Team"
    tab._save_team()

    assert tab._team_id is not None
    loaded = get_tb_team(tab.conn, tab._team_id)
    assert loaded["data"]["slots"][0]["species"] == "garchomp"
    # combo now lists it
    assert tab.team_combo.count() == 2  # "Open a team…" + ours
    print("PASS: UI save persists and the open-team combo lists it")


def test_item_picker_sets_slot_and_renders():
    _app()
    from PySide6.QtCore import Qt
    from fourslice.gui.teams_tab import ITEM_MODE
    tab = _make_tab()
    tab._active_slot_index = 0

    tab._open_item_picker(0)
    assert tab._browse_mode == ITEM_MODE
    assert tab.list_stack.currentWidget() is tab.item_panel

    tab.pick_item("leftovers")
    assert tab.team["slots"][0]["item"] == "leftovers"
    assert tab.edit_panel.item_field.text() == "Leftovers"
    # items_base is populated for the format and usage-ordered
    assert tab._items_base and tab._items_base[0]["id"] == "leftovers"
    print("PASS: item picker sets the slot, renders the field, and lists legal items")


def test_move_picker_restricted_to_learnset():
    _app()
    tab = _make_tab()
    tab._active_slot_index = 0
    tab.pick_species("garchomp", 445)

    # candidates exclude moves Garchomp cannot learn
    tab._pending_move_idx = 0
    tab._load_format_lists()  # populate _moves_base (normally built by the picker)
    ids = {m["id"] for m in tab._filtered_moves()}
    assert "earthquake" in ids
    assert "recover" not in ids
    # pick_move rejects a move the species can't learn
    tab.pick_move("recover")
    assert tab.team["slots"][0]["moves"][0] != "recover", "illegal move was not stored"
    # but accepts a learnable move
    tab.pick_move("earthquake")
    assert tab.team["slots"][0]["moves"][0] == "earthquake"
    print("PASS: move picker only offers learnable moves and rejects others")


def test_scroll_resets_to_top_on_browse():
    _app()
    tab = _make_tab()
    # simulate having scrolled somewhere
    tab.pokedex_scroll.verticalScrollBar().setValue(5)
    assert tab.pokedex_scroll.verticalScrollBar().value() > 0
    tab._show_browse()
    assert tab.pokedex_scroll.verticalScrollBar().value() == 0, \
        "returning to the search must reset the list to the top"
    print("PASS: returning to the search resets every list scroll to the top")


def test_format_combo_is_stats_backed_and_searchable():
    _app()
    from PySide6.QtCore import Qt
    tab = _make_tab()

    # only stats-backed formats are offered
    assert tab._format_items, "expected some formats"
    assert all(td.match_stats_format(n) for n in tab._format_items)
    # ordered most-to-least played (task G), mirroring the sidebar: the
    # most-played format leads, not formats.js file order
    assert tab._format_items[0] == "[Gen 9 Champions] VGC 2026 Reg M-B"
    assert "[Gen 9] OU" in tab._format_items

    # it's an editable type-to-filter combo (like the stats filters)
    assert tab.format_combo.isEditable()
    assert tab.format_combo.completer() is not None
    assert tab.format_combo.completer().filterMode() == Qt.MatchFlag.MatchContains
    print(f"PASS: format combo is searchable and limited to {len(tab._format_items)} stats-backed formats")


def test_edit_panel_builds_five_meta_squares(monkeypatch, tmp_path):
    _app()
    # hermetic: point the stats lookup at an empty db so the test never reads
    # the production stats.db (match_stats_format -> None -> lists stay empty)
    from fourslice import config
    monkeypatch.setattr(config, "get_stats_db_path", lambda *a, **k: tmp_path / "no-stats.db")
    tab = _make_tab()
    ep = tab.edit_panel

    # the five meta squares (counters/teammates + moves/abilities/items) and
    # their list bodies all exist after construction
    for attr in ("counters_card", "teammates_card", "moves_square_card",
                 "abilities_card", "items_card",
                 "counters_list", "teammates_list", "moves_list",
                 "abilities_list", "items_list"):
        assert hasattr(ep, attr), f"edit panel missing {attr}"

    # picking a Pokémon opens the edit panel and refreshes every square; with
    # no stats.db (match_stats_format -> None) they must stay empty, not raise
    tab._active_slot_index = 0
    tab.pick_species("garchomp", 445)
    assert tab.content_stack.currentWidget() is tab.edit_panel
    for name in ("counters_list", "teammates_list", "moves_list",
                 "abilities_list", "items_list"):
        assert getattr(ep, name).count() == 0, f"{name} not empty without stats.db"
    print("PASS: edit panel builds and refreshes all five meta squares safely")


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__]))
