"""
Run directly with: python tests/test_parser.py

Validates the parser + storage layer against two real, hand-verified
replays (one win, one loss) checked into tests/sample_logs/. Run this
after any change to fourslice/parser.py or fourslice/storage.py -- if it
passes, the core data pipeline is still working correctly.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fourslice.parser import (
    parse_game_events, extract_regulation, is_random_battle, detect_battle_size,
    game_id_from_url, find_all_replay_urls_for_user, parse_team_preview, game_id_sort_key,
)
from fourslice.storage import (
    init_db, import_replay, get_known_game_ids, resolve_team_id, is_mega_stone, get_teams_by_recency,
    get_mons_for_filter,
)

SAMPLE_DIR = Path(__file__).resolve().parent / "sample_logs"


def load_log(filename):
    with open(SAMPLE_DIR / filename) as f:
        return json.load(f)["log"]


def test_game1_win():
    log = load_log("game1_win_vs_datrandomguy787.json")
    rows = parse_game_events(
        log, "https://replay.pokemonshowdown.com/gen9championsvgc2026regmbbo3-2647404582"
    )

    turns = [r for r in rows if r["EventType"] == "turnStart"]
    faints = [r for r in rows if r["EventType"] == "faint"]
    moves = [r for r in rows if r["EventType"] not in ("turnStart", "faint")]

    assert len(turns) == 2, f"expected 2 turns, got {len(turns)}"
    assert len(faints) == 1, f"expected 1 faint, got {len(faints)}"
    assert len(moves) == 7, f"expected 7 moves, got {len(moves)}"
    assert rows[0]["Winner"] == "salmoncashew"
    print("PASS: game1 (win vs datrandomguy787)")


def test_game2_loss():
    log = load_log("game2_loss_vs_aletito.json")
    rows = parse_game_events(
        log, "https://replay.pokemonshowdown.com/gen9championsvgc2026regmbbo3-2647349643"
    )

    turns = [r for r in rows if r["EventType"] == "turnStart"]
    faints = [r for r in rows if r["EventType"] == "faint"]
    moves = [r for r in rows if r["EventType"] not in ("turnStart", "faint")]

    assert len(turns) == 5, f"expected 5 turns, got {len(turns)}"
    assert len(faints) == 6, f"expected 6 faints, got {len(faints)}"
    assert len(moves) == 15, f"expected 15 moves, got {len(moves)}"
    assert rows[0]["Winner"] == "aletito"
    print("PASS: game2 (loss vs aletito)")

def test_ko_credit_direct_hit():
    """
    game1's one faint: p2's Sneasler lands the finishing Close Combat
    on p1's Blastoise. The simple, unambiguous case -- one move, one
    fatal hit, no spread, no [from] tag.
    """
    log = load_log("game1_win_vs_datrandomguy787.json")
    rows = parse_game_events(
        log, "https://replay.pokemonshowdown.com/gen9championsvgc2026regmbbo3-2647404582"
    )
    faints = [r for r in rows if r["EventType"] == "faint"]

    assert len(faints) == 1
    assert faints[0]["Mon"] == "Blastoise"
    assert faints[0]["KOCreditMon"] == "Sneasler", f"got {faints[0]['KOCreditMon']!r}"
    assert faints[0]["KOCreditSide"] == "p2", f"got {faints[0]['KOCreditSide']!r}"
    print("PASS: a straightforward single-move, single-target KO is credited correctly")


def test_ko_credit_attributes_the_actual_finishing_move_not_the_prior_chip_damage():
    """
    game2, turn 4: Charizard's spread Heat Wave chips both Garchomp
    and Pyroar (14/100, 22/100), then Sylveon's spread Hyper Voice
    finishes BOTH off in the same turn. The correct credit for both
    KOs is Sylveon, not Charizard -- this is exactly the scenario
    that makes KO credit harder than "whoever moved last in the
    turn": Charizard's move IS the earlier one here, and a naive
    "last opposing move this turn" heuristic would get this right by
    coincidence (Sylveon happens to be last), but only because of
    where these two specific moves land in the log, not because it's
    actually tracking which hit was fatal.
    """
    log = load_log("game2_loss_vs_aletito.json")
    rows = parse_game_events(
        log, "https://replay.pokemonshowdown.com/gen9championsvgc2026regmbbo3-2647349643"
    )
    faints_turn4 = [r for r in rows if r["EventType"] == "faint" and r["Turn"] == 4]

    assert len(faints_turn4) == 2, f"expected 2 faints in turn 4, got {len(faints_turn4)}"
    for faint in faints_turn4:
        assert faint["Mon"] in ("Garchomp", "Pyroar"), f"unexpected victim {faint['Mon']!r}"
        assert faint["KOCreditMon"] == "Sylveon", \
            f"{faint['Mon']}: expected credit to Sylveon (the actual finishing hit), got {faint['KOCreditMon']!r}"
        assert faint["KOCreditSide"] == "p2"
    print("PASS: a double-KO turn correctly credits the move that actually landed the fatal hit, not an earlier chip-damage move")


def test_ko_credit_leaves_unattributed_when_the_source_is_unclear():
    """
    game2's final faint: Basculegion dies right after Charizard's
    Solar Beam ([still] tag, no target on the |move| line itself --
    the real target only shows up on a separate |-anim| line this
    parser doesn't read). No [from] tag on the fatal damage line
    either, so there's no clean signal at all -- the honest answer is
    "don't know", not a guess. This is the documented, accepted gap:
    charge/delayed moves (Solar Beam, Fly, Future Sight, etc.) can
    result in an unattributed KO rather than a correctly-credited one.
    """
    log = load_log("game2_loss_vs_aletito.json")
    rows = parse_game_events(
        log, "https://replay.pokemonshowdown.com/gen9championsvgc2026regmbbo3-2647349643"
    )
    faints = [r for r in rows if r["EventType"] == "faint"]
    basculegion_faint = next(f for f in faints if f["Mon"] == "Basculegion")

    assert basculegion_faint["KOCreditMon"] is None, f"got {basculegion_faint['KOCreditMon']!r}"
    assert basculegion_faint["KOCreditSide"] is None
    print("PASS: a KO whose real cause isn't visible on the |move| line is left unattributed, not guessed at")


def test_ko_credit_full_trace_matches_hand_verified_ground_truth():
    """
    Every single faint across both sample games, hand-traced against
    the raw log line by line (move/damage/[from]/[spread]/faint) --
    the complete picture, not just the three illustrative cases above.
    """
    log1 = load_log("game1_win_vs_datrandomguy787.json")
    log2 = load_log("game2_loss_vs_aletito.json")
    rows1 = parse_game_events(log1, "https://replay.pokemonshowdown.com/gen9championsvgc2026regmbbo3-2647404582")
    rows2 = parse_game_events(log2, "https://replay.pokemonshowdown.com/gen9championsvgc2026regmbbo3-2647349643")

    faints2 = [(r["Turn"], r["Mon"], r["KOCreditMon"], r["KOCreditSide"]) for r in rows2 if r["EventType"] == "faint"]
    expected_game2 = [
        (3, "Garchomp", "Whimsicott", "p1"),
        (3, "Whimsicott", "Sylveon", "p2"),
        (4, "Garchomp", "Sylveon", "p2"),
        (4, "Pyroar", "Sylveon", "p2"),
        (5, "Sylveon", "Basculegion", "p1"),
        (5, "Basculegion", None, None),
    ]
    assert faints2 == expected_game2, f"got {faints2}"

    faints1 = [(r["Turn"], r["Mon"], r["KOCreditMon"], r["KOCreditSide"]) for r in rows1 if r["EventType"] == "faint"]
    assert faints1 == [(2, "Blastoise", "Sneasler", "p2")], f"got {faints1}"

    print("PASS: every faint across both sample games matches the full hand-traced ground truth")

def test_storage_roundtrip():
    db_path = Path(__file__).resolve().parent / "_test_roundtrip.db"
    if db_path.exists():
        db_path.unlink()

    conn = init_db(str(db_path))
    import_replay(
        conn, load_log("game1_win_vs_datrandomguy787.json"),
        "https://replay.pokemonshowdown.com/gen9championsvgc2026regmbbo3-2647404582",
        my_usernames={"salmoncashew"},
    )
    import_replay(
        conn, load_log("game2_loss_vs_aletito.json"),
        "https://replay.pokemonshowdown.com/gen9championsvgc2026regmbbo3-2647349643",
        my_usernames={"salmoncashew"},
    )

    games = conn.execute("SELECT game_id, my_side, result FROM games ORDER BY game_id").fetchall()
    assert len(games) == 2, f"expected 2 games stored, got {len(games)}"

    results = {row[2] for row in games}
    assert results == {"W", "L"}, f"expected one W and one L, got {results}"

    event_count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    assert event_count == 36, f"expected 36 total events, got {event_count}"

    conn.close()
    db_path.unlink()
    print("PASS: storage roundtrip (parse -> SQLite -> query)")

def test_parse_team_preview_extracts_full_roster():
    log = load_log("game2_loss_vs_aletito.json")
    teams = parse_team_preview(log)

    assert len(teams["p1"]) == 6, f"expected 6 mons, got {len(teams['p1'])}"
    assert len(teams["p2"]) == 6, f"expected 6 mons, got {len(teams['p2'])}"

    whimsicott = teams["p1"][0]
    assert whimsicott["species"] == "Whimsicott", f"got {whimsicott['species']!r}"
    assert whimsicott["moves"] == ["Protect", "Tailwind", "SunnyDay", "Moonblast"], \
        f"got {whimsicott['moves']!r}"

    print("PASS: parse_team_preview extracts all 6 mons per side with their full assumed moveset")


def test_parse_team_preview_empty_when_no_showteam_line():
    teams = parse_team_preview("|player|p1|someone|\n|turn|1\n")
    assert teams == {"p1": [], "p2": []}
    print("PASS: parse_team_preview returns empty lists, not an error, when there's no team preview data")


def test_resolve_team_id_matches_same_roster_across_games():
    """
    game1 and game2's sample logs are salmoncashew's SAME team, played
    on opposite sides of the room (p2 in one, p1 in the other) --
    confirmed directly against the real showteam lines. Importing both
    should resolve to the same team_id, not create two.
    """
    conn = init_db(":memory:")
    result1 = import_replay(
        conn, load_log("game1_win_vs_datrandomguy787.json"),
        "https://replay.pokemonshowdown.com/gen9championsvgc2026regmbbo3-2647404582",
        my_usernames={"salmoncashew"},
    )
    result2 = import_replay(
        conn, load_log("game2_loss_vs_aletito.json"),
        "https://replay.pokemonshowdown.com/gen9championsvgc2026regmbbo3-2647349643",
        my_usernames={"salmoncashew"},
    )

    team_id_1 = conn.execute(
        "SELECT team_id FROM games WHERE game_id = ?", (result1["game_id"],)
    ).fetchone()[0]
    team_id_2 = conn.execute(
        "SELECT team_id FROM games WHERE game_id = ?", (result2["game_id"],)
    ).fetchone()[0]

    assert team_id_1 is not None, "expected a resolved team_id, got None"
    assert team_id_1 == team_id_2, f"expected the same team, got {team_id_1} and {team_id_2}"

    team_count = conn.execute("SELECT COUNT(*) FROM teams").fetchone()[0]
    assert team_count == 1, f"expected exactly one team row, got {team_count}"

    roster_size = conn.execute(
        "SELECT COUNT(*) FROM team_pokemon WHERE team_id = ?", (team_id_1,)
    ).fetchone()[0]
    assert roster_size == 6, f"expected 6 team_pokemon rows, got {roster_size}"

    conn.close()
    print("PASS: the same 6-mon roster across two games resolves to one shared team, not two")


def test_resolve_team_id_empty_roster_returns_none():
    conn = init_db(":memory:")
    assert resolve_team_id(conn, []) is None
    assert conn.execute("SELECT COUNT(*) FROM teams").fetchone()[0] == 0
    conn.close()
    print("PASS: an empty roster resolves to no team, and creates nothing")


def test_init_db_migration_is_idempotent():
    """
    games.team_id is added via ALTER TABLE, not CREATE TABLE IF NOT
    EXISTS -- this confirms calling init_db twice against the same
    file (every real app launch) doesn't error the second time.
    """
    db_path = Path(__file__).resolve().parent / "_test_migration.db"
    if db_path.exists():
        db_path.unlink()

    conn1 = init_db(str(db_path))
    conn1.close()
    conn2 = init_db(str(db_path))  # should be a silent no-op for the already-added column
    conn2.close()

    db_path.unlink()
    print("PASS: re-running init_db against an already-migrated file doesn't error")


def test_is_mega_stone():
    cases = [
        ("Blastoisinite", "Blastoise", True),
        ("Charizardite X", "Charizard", True),
        ("Charizardite Y", "Charizard", True),
        ("Floettite", "Floette-Eternal", True),
        ("Pyroarite", "Pyroar", True),
        ("SitrusBerry", "Incineroar", False),
        ("FocusSash", "Whimsicott", False),
        ("", "Whimsicott", False),
        ("Life Orb", "Garchomp", False),
        ("Graphite", "Pikachu", False),
        ("Charizardite Z", "Charizard", True),
        ("Charizardite A", "Charizard", False),
    ]
    for item, species, expected in cases:
        actual = is_mega_stone(item, species)
        assert actual == expected, f"is_mega_stone({item!r}, {species!r}) = {actual}, expected {expected}"
    print("PASS: is_mega_stone correctly matches real Mega Stone naming patterns and rejects near-misses")


def test_resolve_team_id_nickname_prefers_mega_holder():
    """
    game1's real roster has Pyroar holding Pyroarite -- a genuine
    Mega Stone match even though Whimsicott is roster[0]. Confirms
    the Mega holder takes naming priority, on real data.
    """
    conn = init_db(":memory:")
    import_replay(
        conn, load_log("game1_win_vs_datrandomguy787.json"),
        "https://replay.pokemonshowdown.com/gen9championsvgc2026regmbbo3-2647404582",
        my_usernames={"salmoncashew"},
    )
    nickname = conn.execute("SELECT nickname FROM teams").fetchone()[0]
    assert nickname == "Pyroar-M-B-1", f"got {nickname!r}"
    conn.close()
    print("PASS: a team with a real Mega-holder (Pyroar/Pyroarite) is nicknamed after it, not roster[0]")


def test_resolve_team_id_falls_back_to_first_pokemon_without_a_mega():
    conn = init_db(":memory:")
    roster = [
        {"species": "Landorus-Therian", "item": "Choice Scarf", "moves": ["Earthquake", "UTurn", "StoneEdge", "Protect"]},
        {"species": "Rillaboom", "item": "Assault Vest", "moves": ["GrassyGlide", "WoodHammer", "UTurn", "Fake Out"]},
    ]
    team_id = resolve_team_id(conn, roster)
    nickname = conn.execute("SELECT nickname FROM teams WHERE team_id = ?", (team_id,)).fetchone()[0]
    assert nickname == "Landorus-Therian-Unknown-1", f"got {nickname!r}"
    conn.close()
    print("PASS: with no Mega Stone anywhere in the roster, nickname falls back to roster[0] as before")

def test_parse_team_preview_falls_back_to_poke_lines():
    """
    Real log, no |showteam| at all -- only bare |poke| lines (species
    + gender). Confirms the fallback correctly extracts species-only
    rosters, with item/moves blank, not empty rosters.
    """
    log = load_log("game3_poke_only_no_showteam.json")
    teams = parse_team_preview(log)

    assert len(teams["p1"]) == 6, f"expected 6 mons, got {len(teams['p1'])}"
    assert len(teams["p2"]) == 6, f"expected 6 mons, got {len(teams['p2'])}"

    landorus = teams["p1"][0]
    assert landorus["species"] == "Landorus-Therian", f"got {landorus['species']!r}"
    assert landorus["item"] == "", f"got {landorus['item']!r}"
    assert landorus["moves"] == [], f"got {landorus['moves']!r}"

    p2_species = [mon["species"] for mon in teams["p2"]]
    assert "Regidrago" in p2_species and "Great Tusk" in p2_species, f"got {p2_species!r}"

    print("PASS: parse_team_preview falls back to |poke| lines and extracts species (item/moves blank) on real data")


def test_parse_team_preview_prefers_showteam_when_both_present():
    log = load_log("game1_win_vs_datrandomguy787.json")
    teams = parse_team_preview(log)
    whimsicott = teams["p1"][0]
    assert whimsicott["item"] == "FocusSash", f"expected showteam's item data to win, got {whimsicott['item']!r}"
    print("PASS: |showteam| data takes priority over the |poke| fallback when both are present")

def test_game_id_sort_key_extracts_trailing_number():
    assert game_id_sort_key("gen9championsvgc2026regmbbo3-2647404582") == 2647404582
    assert game_id_sort_key("gen9championsvgc2026regmbbo3-2647349643") == 2647349643
    print("PASS: game_id_sort_key extracts the trailing replay-id number")


def test_game_id_sort_key_falls_back_when_no_trailing_digits():
    assert game_id_sort_key("not-a-real-id") == -1
    print("PASS: game_id_sort_key falls back to -1 (sorts oldest) instead of raising")


def test_get_teams_by_recency_orders_by_replay_id_not_import_order():
    """
    Two synthetic teams, deliberately built so DB-insertion order and
    true chronological order DISAGREE: the team resolved (inserted)
    FIRST here -- which a naive created_at- or team_id-based sort
    would always rank as "newest" -- is linked to the LOWER-numbered
    (i.e. actually older) replay id, and the team resolved SECOND
    gets the HIGHER (actually newer) one.

    This is exactly the shape of the real bug: backfill_teams.py and
    the bulk importer both walk Showdown's replay list newest-first,
    so a team from an OLD real game can still land in the local teams
    table before a team from a NEW real game, if the new one simply
    hadn't been played yet at backfill time and only turns up in a
    later sync. See game_id_sort_key's and get_teams_by_recency's
    docstrings for the full explanation.

    (game1/game2 aren't used here because they resolve to the SAME
    team -- see test_resolve_team_id_matches_same_roster_across_games
    above -- so they can't demonstrate a two-team ordering at all.)
    """
    conn = init_db(":memory:")

    older_game_team_id = resolve_team_id(conn, [
        {"species": "Landorus-Therian", "item": "", "moves": []},
        {"species": "Rillaboom", "item": "", "moves": []},
    ])
    conn.execute(
        "INSERT INTO games (game_id, replay_url, my_side, team_id) VALUES (?, ?, ?, ?)",
        ("gen9vgc2026rega-1000000001", "https://example.com/older", "p1", older_game_team_id),
    )

    newer_game_team_id = resolve_team_id(conn, [
        {"species": "Flutter Mane", "item": "", "moves": []},
        {"species": "Chien-Pao", "item": "", "moves": []},
    ])
    conn.execute(
        "INSERT INTO games (game_id, replay_url, my_side, team_id) VALUES (?, ?, ?, ?)",
        ("gen9vgc2026rega-9999999999", "https://example.com/newer", "p1", newer_game_team_id),
    )

    ordered_ids = [team_id for team_id, _ in get_teams_by_recency(conn)]
    assert ordered_ids == [newer_game_team_id, older_game_team_id], (
        f"expected the higher-replay-id team first ({newer_game_team_id}), got {ordered_ids} -- "
        "if this instead matches insertion order, get_teams_by_recency has regressed to "
        "sorting by local DB order"
    )

    conn.close()
    print("PASS: get_teams_by_recency orders by each team's highest replay id, not DB insertion order")


def test_get_teams_by_recency_empty_db():
    conn = init_db(":memory:")
    assert get_teams_by_recency(conn) == []
    conn.close()
    print("PASS: get_teams_by_recency returns an empty list, not an error, against an empty DB")


def test_get_teams_by_recency_regulation_filter():
    """
    game1/game2 both resolve to the same team ("Pyroar-M-B-1"),
    regulation M-B (see test_resolve_team_id_matches_same_roster_across_games
    above). Adding a second, synthetic team in regulation H confirms
    regulation scoping actually EXCLUDES a team with zero games in
    the requested regulation, not just sorts it lower.
    """
    conn = init_db(":memory:")
    import_replay(
        conn, load_log("game1_win_vs_datrandomguy787.json"),
        "https://replay.pokemonshowdown.com/gen9championsvgc2026regmbbo3-2647404582",
        my_usernames={"salmoncashew"},
    )
    import_replay(
        conn, load_log("game2_loss_vs_aletito.json"),
        "https://replay.pokemonshowdown.com/gen9championsvgc2026regmbbo3-2647349643",
        my_usernames={"salmoncashew"},
    )
    mb_team_id = conn.execute("SELECT team_id FROM teams").fetchone()[0]

    h_team_id = resolve_team_id(conn, [
        {"species": "Flutter Mane", "item": "", "moves": []},
        {"species": "Chien-Pao", "item": "", "moves": []},
    ], regulation="H")
    conn.execute(
        "INSERT INTO games (game_id, replay_url, my_side, team_id, regulation, result) VALUES (?, ?, ?, ?, ?, ?)",
        ("synthetic-reg-h-game", "https://example.com/reg-h", "p1", h_team_id, "H", "W"),
    )

    all_teams = {tid for tid, _ in get_teams_by_recency(conn)}
    mb_only = {tid for tid, _ in get_teams_by_recency(conn, regulation="M-B")}
    h_only = {tid for tid, _ in get_teams_by_recency(conn, regulation="H")}

    assert all_teams == {mb_team_id, h_team_id}, f"got {all_teams}"
    assert mb_only == {mb_team_id}, f"got {mb_only}"
    assert h_only == {h_team_id}, f"got {h_only}"

    conn.close()
    print("PASS: get_teams_by_recency(regulation=...) excludes teams with no games in that regulation")


def test_get_mons_for_filter_team_id_takes_priority():
    """
    With a team_id given, returns that team's actual roster
    (team_pokemon, slot order) -- including a mon that's never been
    sent out in any game (Incineroar, on this real roster but never
    once moved in either sample game), which a species-seen-in-events
    query would miss entirely. regulation is ignored when team_id is
    set, even if passed.
    """
    conn = init_db(":memory:")
    import_replay(
        conn, load_log("game1_win_vs_datrandomguy787.json"),
        "https://replay.pokemonshowdown.com/gen9championsvgc2026regmbbo3-2647404582",
        my_usernames={"salmoncashew"},
    )
    import_replay(
        conn, load_log("game2_loss_vs_aletito.json"),
        "https://replay.pokemonshowdown.com/gen9championsvgc2026regmbbo3-2647349643",
        my_usernames={"salmoncashew"},
    )
    team_id = conn.execute("SELECT team_id FROM teams").fetchone()[0]

    roster = get_mons_for_filter(conn, team_id=team_id)
    assert roster == ["Whimsicott", "Sneasler", "Basculegion", "Incineroar", "Pyroar", "Garchomp"], f"got {roster}"

    # regulation is ignored once team_id is set, even for one this team's never played under
    still_full_roster = get_mons_for_filter(conn, team_id=team_id, regulation="some-regulation-this-team-never-played")
    assert still_full_roster == roster

    conn.close()
    print("PASS: get_mons_for_filter(team_id=...) returns the full roster (incl. never-sent-out mons), ignoring regulation")


def test_get_mons_for_filter_regulation_fallback():
    """
    No team_id, but a regulation given: falls back to species seen
    (in events) under that regulation. Incineroar never moved at all,
    so -- unlike the team_id case above -- it correctly does NOT
    appear here, since this is scoped to what's observable in events,
    not a team's declared roster.
    """
    conn = init_db(":memory:")
    import_replay(
        conn, load_log("game1_win_vs_datrandomguy787.json"),
        "https://replay.pokemonshowdown.com/gen9championsvgc2026regmbbo3-2647404582",
        my_usernames={"salmoncashew"},
    )
    import_replay(
        conn, load_log("game2_loss_vs_aletito.json"),
        "https://replay.pokemonshowdown.com/gen9championsvgc2026regmbbo3-2647349643",
        my_usernames={"salmoncashew"},
    )

    mb_mons = set(get_mons_for_filter(conn, regulation="M-B"))
    assert "Incineroar" not in mb_mons, "Incineroar never moved -- shouldn't appear via the regulation fallback"
    assert "Whimsicott" in mb_mons and "Garchomp" in mb_mons

    no_such_regulation = get_mons_for_filter(conn, regulation="not-a-real-regulation")
    assert no_such_regulation == []

    conn.close()
    print("PASS: get_mons_for_filter(regulation=...) falls back to species actually seen under that regulation")


def test_get_mons_for_filter_no_scope_returns_everything():
    conn = init_db(":memory:")
    import_replay(
        conn, load_log("game1_win_vs_datrandomguy787.json"),
        "https://replay.pokemonshowdown.com/gen9championsvgc2026regmbbo3-2647404582",
        my_usernames={"salmoncashew"},
    )
    import_replay(
        conn, load_log("game2_loss_vs_aletito.json"),
        "https://replay.pokemonshowdown.com/gen9championsvgc2026regmbbo3-2647349643",
        my_usernames={"salmoncashew"},
    )

    everything = get_mons_for_filter(conn)
    assert "Incineroar" not in everything, "never moved -- shouldn't appear with no scope either"
    assert "Whimsicott" in everything

    conn.close()
    print("PASS: get_mons_for_filter with neither team_id nor regulation returns every species ever seen")


def test_hp_capture_across_switch_damage_heal_and_faint():
    """
    hp_probe.json exercises the HP-carrying protocol lines: switch-in
    HP ("100/100", "362/362"), a damage line ("28/100"), a heal line
    ("38/100"), a fatal "0 fnt" hit (no slash-form -- prior HP stays,
    and the slot is cleared at faint), and a re-entry with a status
    suffix ("534/534 par"). This locks in the HP columns the replay
    player's timeline reads from.
    """
    log = load_log("hp_probe.json")
    rows = parse_game_events(log, "https://replay.pokemonshowdown.com/hpprobe-1")

    turns = [r for r in rows if r["EventType"] == "turnStart"]
    moves = [r for r in rows if r["EventType"] not in ("turnStart", "faint")]
    faints = [r for r in rows if r["EventType"] == "faint"]

    assert len(turns) == 2, f"expected 2 turns, got {len(turns)}"
    assert len(moves) == 1, f"expected 1 move, got {len(moves)}"
    assert len(faints) == 1, f"expected 1 faint, got {len(faints)}"

    # Turn 1 start: both sides show their switch-in HP.
    assert turns[0]["HP1a"] == "100/100", f"got {turns[0]['HP1a']!r}"
    assert turns[0]["HP2a"] == "362/362", f"got {turns[0]['HP2a']!r}"

    # The move row precedes its damage -- HP is still pre-hit here.
    assert moves[0]["EventType"] == "U-turn"
    assert moves[0]["HP1a"] == "100/100", f"got {moves[0]['HP1a']!r}"

    # Turn 2 start reflects the turn-1 damage; the heal comes after.
    assert turns[1]["HP1a"] == "28/100", f"got {turns[1]['HP1a']!r}"

    # Fatal hits arrive as "0 fnt" (not "0/100") so no HP is parsed
    # from them; the faint row carries the last known value and the
    # slot is cleared. KO credit still works: U-turn was the fatal
    # move, target p1a, no [from] tag.
    assert faints[0]["Mon"] == "Uxie"
    assert faints[0]["HP1a"] == "38/100", f"got {faints[0]['HP1a']!r}"
    assert faints[0]["KOCreditMon"] == "Landorus-Therian", f"got {faints[0]['KOCreditMon']!r}"
    assert faints[0]["KOCreditSide"] == "p2", f"got {faints[0]['KOCreditSide']!r}"
    print("PASS: HP is captured across switch, damage, heal, and re-entry lines")


if __name__ == "__main__":
    test_game1_win()
    test_game2_loss()
    test_storage_roundtrip()
    test_parse_team_preview_extracts_full_roster()
    test_parse_team_preview_empty_when_no_showteam_line()
    test_parse_team_preview_falls_back_to_poke_lines()
    test_parse_team_preview_prefers_showteam_when_both_present()
    test_resolve_team_id_matches_same_roster_across_games()
    test_is_mega_stone()
    test_resolve_team_id_nickname_prefers_mega_holder()
    test_resolve_team_id_falls_back_to_first_pokemon_without_a_mega()
    test_resolve_team_id_empty_roster_returns_none()
    test_init_db_migration_is_idempotent()
    test_game_id_sort_key_extracts_trailing_number()
    test_game_id_sort_key_falls_back_when_no_trailing_digits()
    test_get_teams_by_recency_orders_by_replay_id_not_import_order()
    test_get_teams_by_recency_empty_db()
    test_get_teams_by_recency_regulation_filter()
    test_get_mons_for_filter_team_id_takes_priority()
    test_get_mons_for_filter_regulation_fallback()
    test_get_mons_for_filter_no_scope_returns_everything()
    test_ko_credit_direct_hit()
    test_ko_credit_attributes_the_actual_finishing_move_not_the_prior_chip_damage()
    test_ko_credit_leaves_unattributed_when_the_source_is_unclear()
    test_ko_credit_full_trace_matches_hand_verified_ground_truth()
    test_hp_capture_across_switch_damage_heal_and_faint()
    print("\nAll tests passed.")
