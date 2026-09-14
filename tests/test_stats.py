"""
Run directly with: python tests/test_stats.py

Validates the stats registry pattern and both real stats
(turns_on_field, move_usage) against the two real, hand-verified
sample games.
"""

import json
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")  # headless -- no display needed, just confirms a chart draws
from matplotlib.figure import Figure
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fourslice.storage import init_db, import_replay, resolve_team_id
from fourslice.stats import (
    REGISTRY, turns_on_field, render_turns_on_field, move_usage, render_move_usage,
    co_occurrence, render_co_occurrence, ko_credit, render_ko_credit, DIFF_CAPABLE_CHART_TYPES,
)

SAMPLE_DIR = Path(__file__).resolve().parent / "sample_logs"


def load_log(filename):
    with open(SAMPLE_DIR / filename) as f:
        return json.load(f)["log"]


def _populated_conn():
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
    return conn


def test_registry_contains_turns_on_field():
    assert "Turns on Field" in REGISTRY
    assert REGISTRY["Turns on Field"].data_fn is turns_on_field
    assert REGISTRY["Turns on Field"].render_fn is render_turns_on_field
    assert REGISTRY["Turns on Field"].chart_type == "bar"
    print("PASS: stats registry exposes turns_on_field with a data_fn, render_fn, and chart_type='bar'")


def test_registry_contains_move_usage():
    assert "Move Usage" in REGISTRY
    assert REGISTRY["Move Usage"].data_fn is move_usage
    assert REGISTRY["Move Usage"].render_fn is render_move_usage
    assert REGISTRY["Move Usage"].chart_type == "pie"
    print("PASS: stats registry exposes move_usage with a data_fn, render_fn, and chart_type='pie'")


def test_registry_contains_co_occurrence():
    assert "Co-occurrence" in REGISTRY
    assert REGISTRY["Co-occurrence"].data_fn is co_occurrence
    assert REGISTRY["Co-occurrence"].render_fn is render_co_occurrence
    assert REGISTRY["Co-occurrence"].chart_type == "bar"
    print("PASS: stats registry exposes co_occurrence with a data_fn, render_fn, and chart_type='bar'")


def test_registry_contains_ko_credit():
    assert "KO Credit" in REGISTRY
    assert REGISTRY["KO Credit"].data_fn is ko_credit
    assert REGISTRY["KO Credit"].render_fn is render_ko_credit
    assert REGISTRY["KO Credit"].chart_type == "bar"
    print("PASS: stats registry exposes ko_credit with a data_fn, render_fn, and chart_type='bar'")


def test_diff_capable_chart_types_is_bar_only():
    assert DIFF_CAPABLE_CHART_TYPES == {"bar"}, f"got {DIFF_CAPABLE_CHART_TYPES}"
    assert REGISTRY["Turns on Field"].chart_type in DIFF_CAPABLE_CHART_TYPES
    assert REGISTRY["Co-occurrence"].chart_type in DIFF_CAPABLE_CHART_TYPES
    assert REGISTRY["KO Credit"].chart_type in DIFF_CAPABLE_CHART_TYPES
    assert REGISTRY["Move Usage"].chart_type not in DIFF_CAPABLE_CHART_TYPES
    print("PASS: only bar charts are diff-capable -- Move Usage's pie chart is excluded")


def test_turns_on_field_matches_hand_verified_counts():
    conn = _populated_conn()
    df = turns_on_field(conn)

    # Whimsicott appears on p1 in game2 (2 turns) -- confirms the
    # underlying counts still match what was hand-verified earlier
    # in the project, now reached through the registry/stat function
    # instead of an ad hoc script.
    whimsicott_row = df[(df["mon"] == "Whimsicott") & (df["side"] == "p1")]
    assert len(whimsicott_row) == 1, "expected exactly one Whimsicott/p1 row"
    assert whimsicott_row.iloc[0]["total_turns"] == 3, f"got {whimsicott_row.iloc[0]['total_turns']}"

    conn.close()
    print("PASS: turns_on_field matches hand-verified counts from earlier validation")


def test_turns_on_field_result_filter():
    conn = _populated_conn()
    wins_only = turns_on_field(conn, result="W")
    losses_only = turns_on_field(conn, result="L")

    # game1 (the win) is 2 turns total -- nothing in wins_only should exceed that
    assert wins_only["total_turns"].max() <= 2
    # game2 (the loss) is 5 turns total
    assert losses_only["total_turns"].max() <= 5

    conn.close()
    print("PASS: result filter (W/L) correctly narrows the query")


def test_turns_on_field_my_side_partitions_cleanly():
    conn = _populated_conn()
    both = turns_on_field(conn)
    mine = turns_on_field(conn, my_side="mine")
    opponent = turns_on_field(conn, my_side="opponent")

    assert mine["total_turns"].sum() + opponent["total_turns"].sum() == both["total_turns"].sum(), \
        "mine + opponent should account for every turn in the unfiltered view"
    assert "side" not in mine.columns, "side shouldn't survive grouping once perspective is fixed"
    assert mine["mon"].is_unique, "each mon should appear once, not split across p1/p2"

    conn.close()
    print("PASS: my_side splits mine/opponent without losing or double-counting turns")


def test_render_turns_on_field_draws_bars():
    conn = _populated_conn()
    df = turns_on_field(conn)
    conn.close()

    fig = Figure()
    render_turns_on_field(df, fig)

    assert len(fig.axes[0].patches) > 0, "expected at least one bar to be drawn"
    print("PASS: render_turns_on_field draws bars for real data")


def test_render_turns_on_field_handles_empty_data():
    fig = Figure()
    render_turns_on_field(pd.DataFrame(columns=["mon", "total_turns", "avg_turns_per_game"]), fig)
    print("PASS: render_turns_on_field handles an empty DataFrame without crashing")


def test_turns_on_field_team_filter():
    """
    game1 and game2 actually resolve to the SAME team (see
    test_resolve_team_id_matches_same_roster_across_games in
    test_parser.py -- salmoncashew plays the identical roster on
    opposite sides of the room in each), so this checks the filter
    mechanics directly instead: filtering by that shared team_id
    should keep every turn, and filtering by a second, genuinely
    different team -- a real team row, just with no games pointing
    at it -- should return nothing.
    """
    conn = _populated_conn()

    real_team_id = conn.execute(
        "SELECT team_id FROM games WHERE team_id IS NOT NULL LIMIT 1"
    ).fetchone()[0]
    unrelated_team_id = resolve_team_id(conn, [
        {"species": "Flutter Mane", "item": "", "moves": []},
        {"species": "Chien-Pao", "item": "", "moves": []},
    ])

    unfiltered_total = turns_on_field(conn)["total_turns"].sum()
    matching_total = turns_on_field(conn, team_id=real_team_id)["total_turns"].sum()
    unrelated_df = turns_on_field(conn, team_id=unrelated_team_id)

    assert matching_total == unfiltered_total, \
        f"expected filtering by the shared team ({matching_total}) to match the unfiltered total ({unfiltered_total})"
    assert unrelated_df.empty, "expected a team with no linked games to return an empty DataFrame"

    conn.close()
    print("PASS: team_id filter narrows turns_on_field correctly (matches the real team, empty for an unrelated one)")


def test_turns_on_field_mon_filter():
    """
    Whimsicott's per-side split (p1=3, p2=1 turns) was already
    hand-verified with no filter at all in
    test_turns_on_field_matches_hand_verified_counts -- confirms the
    mon filter returns exactly that pair of rows, and nothing else,
    once narrowed to just this species.
    """
    conn = _populated_conn()
    df = turns_on_field(conn, mon="Whimsicott")

    assert set(df["mon"]) == {"Whimsicott"}, f"expected only Whimsicott rows, got {set(df['mon'])}"
    assert len(df) == 2, f"expected 2 rows (p1 and p2), got {len(df)}"
    assert df[df["side"] == "p1"].iloc[0]["total_turns"] == 3
    assert df[df["side"] == "p2"].iloc[0]["total_turns"] == 1

    conn.close()
    print("PASS: mon filter narrows turns_on_field to just that species")


def test_turns_on_field_diff():
    """
    Hand-verified against the real data: with my_side="mine", game1
    (the win) has Basculegion averaging 1.0 turn and Whimsicott 1.0;
    game2 (the loss) has Basculegion averaging 4.0 and Whimsicott 3.0.
    Sneasler/Garchomp/Pyroar each only show up in ONE of win/loss, so
    they should be dropped entirely rather than diffed against a
    fabricated 0 -- see test_turns_on_field_diff_drops_mons below.
    """
    conn = _populated_conn()
    diff = turns_on_field(conn, my_side="mine", result="diff")

    assert set(diff["mon"]) == {"Basculegion", "Whimsicott"}, \
        f"expected only mons with both a win and a loss on record, got {set(diff['mon'])}"
    assert diff[diff["mon"] == "Basculegion"].iloc[0]["diff"] == -3.0
    assert diff[diff["mon"] == "Whimsicott"].iloc[0]["diff"] == -2.0

    # sorted by the SIZE of the swing, not raw value -- Basculegion's
    # -3.0 is a bigger swing than Whimsicott's -2.0, so it goes first
    assert list(diff["mon"]) == ["Basculegion", "Whimsicott"], f"got {list(diff['mon'])}"

    conn.close()
    print("PASS: result='diff' computes win-average minus loss-average, sorted by swing size")


def test_turns_on_field_diff_drops_mons_without_both_a_win_and_a_loss():
    """
    Sneasler only ever appears in the win (game1) with my_side="mine"
    -- confirms it's excluded from diff entirely (inner join), not
    given a fabricated 0 for "loss average".
    """
    conn = _populated_conn()
    wins_only_mon_diff = turns_on_field(conn, my_side="mine", result="diff", mon="Sneasler")
    assert wins_only_mon_diff.empty, f"expected no rows, got {wins_only_mon_diff}"
    conn.close()
    print("PASS: a mon with only a win (or only a loss) on record is dropped from diff, not faked")


def test_render_turns_on_field_diff_draws_diverging_bars():
    conn = _populated_conn()
    diff = turns_on_field(conn, my_side="mine", result="diff")
    conn.close()

    fig = Figure()
    render_turns_on_field(diff, fig)
    assert len(fig.axes[0].patches) == 2, f"expected 2 bars (Basculegion, Whimsicott), got {len(fig.axes[0].patches)}"
    print("PASS: render_turns_on_field draws a diverging bar chart in diff mode")


def test_render_turns_on_field_handles_empty_diff():
    fig = Figure()
    empty_diff = pd.DataFrame(columns=["mon", "diff", "avg_turns_per_game_win", "avg_turns_per_game_loss"])
    render_turns_on_field(empty_diff, fig)
    print("PASS: render_turns_on_field handles an empty diff DataFrame without crashing")


def test_move_usage_no_team_returns_empty_dict():
    conn = _populated_conn()
    assert move_usage(conn) == {}
    conn.close()
    print("PASS: move_usage with no team selected returns an empty dict")


def test_move_usage_covers_the_whole_roster_in_team_preview_order():
    """
    All 6 roster slots should be present, in team-preview order
    (team_pokemon.slot_order) -- not just the ones that happened to
    move, and not alphabetical.
    """
    conn = _populated_conn()
    team_id = conn.execute("SELECT team_id FROM teams").fetchone()[0]
    result = move_usage(conn, team_id=team_id)

    assert list(result.keys()) == ["Whimsicott", "Sneasler", "Basculegion", "Incineroar", "Pyroar", "Garchomp"], \
        f"got {list(result.keys())}"

    conn.close()
    print("PASS: move_usage returns all 6 roster slots, in team-preview order")


def test_move_usage_matches_hand_verified_counts():
    """
    Hand-counted directly from the events table, per species, across
    both sample games with no side filter:
      Whimsicott: Tailwind x2, Moonblast x2 (50/50)
      Basculegion: Flip Turn x4, Last Respects x1 (80/20)
      Pyroar: Heat Wave x1 (100%)
      Incineroar: never moved at all -> N/A
    Sneasler and Garchomp are covered separately below -- both have a
    same-species mon on the OTHER side too, which is exactly the
    scenario my_side exists to disambiguate.
    """
    conn = _populated_conn()
    team_id = conn.execute("SELECT team_id FROM teams").fetchone()[0]
    result = move_usage(conn, team_id=team_id)

    whimsicott = result["Whimsicott"]
    assert set(whimsicott["move_name"]) == {"Tailwind", "Moonblast"}
    for move_name in ("Tailwind", "Moonblast"):
        row = whimsicott[whimsicott["move_name"] == move_name].iloc[0]
        assert row["use_count"] == 2 and row["pct_of_uses"] == 50.0, f"{move_name}: got {row.to_dict()}"

    basculegion = result["Basculegion"]
    assert basculegion[basculegion["move_name"] == "Flip Turn"].iloc[0]["pct_of_uses"] == 80.0
    assert basculegion[basculegion["move_name"] == "Last Respects"].iloc[0]["pct_of_uses"] == 20.0

    pyroar = result["Pyroar"]
    assert len(pyroar) == 1 and pyroar.iloc[0]["move_name"] == "Heat Wave" and pyroar.iloc[0]["pct_of_uses"] == 100.0

    assert result["Incineroar"].iloc[0]["move_name"] == "N/A", "Incineroar never moved -- should be N/A"

    conn.close()
    print("PASS: move_usage matches hand-verified per-species counts across the whole roster")


def test_move_usage_scopes_to_the_selected_teams_own_games():
    """
    Garchomp is on the roster, but salmoncashew's own Garchomp never
    actually made a move in either game. With NO side filter,
    Garchomp's panel is still populated -- with the OPPONENT's
    Garchomp's moves (Dragon Claw, Earthquake, from game2), because
    scoping is by games.team_id ("games where I brought THIS team"),
    not "my side only" -- see move_usage's docstring. That's
    deliberate, but exactly the kind of surprising-if-unverified
    behavior worth pinning to real data: adding my_side="mine"
    correctly flips Garchomp to N/A, since MY Garchomp genuinely never
    moved.
    """
    conn = _populated_conn()
    team_id = conn.execute("SELECT team_id FROM teams").fetchone()[0]

    unscoped = move_usage(conn, team_id=team_id)["Garchomp"]
    assert set(unscoped["move_name"]) == {"Dragon Claw", "Earthquake"}, f"got {set(unscoped['move_name'])}"

    mine_only = move_usage(conn, team_id=team_id, my_side="mine")["Garchomp"]
    assert mine_only.iloc[0]["move_name"] == "N/A", \
        f"expected N/A once scoped to 'mine' (my Garchomp never moved), got {mine_only}"

    conn.close()
    print("PASS: unscoped, a roster panel can reflect the opponent's use of that species; my_side='mine' correctly excludes it")


def test_move_usage_my_side_distinguishes_same_species_different_owner():
    """
    Sneasler is on BOTH teams in game1 -- mine (p2: Protect, Close
    Combat) and the opponent's (p1: Fake Out, Quick Guard), same
    species, different Pokemon. With no side filter these mix
    together (4 moves, 25% each); my_side="mine" should isolate just
    the two that are actually mine.
    """
    conn = _populated_conn()
    team_id = conn.execute("SELECT team_id FROM teams").fetchone()[0]

    both = move_usage(conn, team_id=team_id)["Sneasler"]
    mine_only = move_usage(conn, team_id=team_id, my_side="mine")["Sneasler"]

    assert set(both["move_name"]) == {"Close Combat", "Fake Out", "Protect", "Quick Guard"}, f"got {set(both['move_name'])}"
    assert set(mine_only["move_name"]) == {"Close Combat", "Protect"}, \
        f"expected just my Sneasler's moves, got {set(mine_only['move_name'])}"
    assert (mine_only["pct_of_uses"] == 50.0).all()

    conn.close()
    print("PASS: my_side correctly isolates 'my' Sneasler from the opponent's same-species Sneasler")


def test_move_usage_struggle_is_always_excluded():
    """
    Directly mirrors the requested behavior: a mon that also used
    Struggle should have the IDENTICAL pie chart to one that never
    did -- Struggle isn't a real move choice, so it's excluded
    regardless of any filter.
    """
    conn = _populated_conn()
    team_id = conn.execute("SELECT team_id FROM teams").fetchone()[0]
    before = move_usage(conn, team_id=team_id)["Whimsicott"]

    conn.execute(
        "INSERT INTO events (game_id, turn, side, slot, mon, event_type, move_name) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("gen9championsvgc2026regmbbo3-2647404582", 99, "p2", "p2a", "Whimsicott", "move", "Struggle"),
    )
    conn.execute(
        "INSERT INTO events (game_id, turn, side, slot, mon, event_type, move_name) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("gen9championsvgc2026regmbbo3-2647404582", 100, "p2", "p2a", "Whimsicott", "move", "Struggle"),
    )
    after = move_usage(conn, team_id=team_id)["Whimsicott"]

    assert before.equals(after), f"Struggle usage changed the result:\nbefore:\n{before}\nafter:\n{after}"
    assert "Struggle" not in set(after["move_name"])

    conn.close()
    print("PASS: Struggle is always excluded, regardless of how many times it was used")


def test_render_move_usage_prompts_for_a_team_and_draws_a_grid():
    conn1 = _populated_conn()
    fig1 = Figure()
    render_move_usage(move_usage(conn1), fig1)  # no team -> prompt path
    assert len(fig1.axes) == 1, "expected a single 'select a team' prompt axis"
    assert len(fig1.axes[0].patches) == 0, "the prompt shouldn't draw any pie wedges"
    conn1.close()

    conn2 = _populated_conn()
    team_id = conn2.execute("SELECT team_id FROM teams").fetchone()[0]
    fig2 = Figure()
    render_move_usage(move_usage(conn2, team_id=team_id), fig2)
    assert len(fig2.axes) == 6, f"expected a 6-panel grid, got {len(fig2.axes)}"
    assert [ax.get_title() for ax in fig2.axes] == \
        ["Whimsicott", "Sneasler", "Basculegion", "Incineroar", "Pyroar", "Garchomp"]
    conn2.close()

    print("PASS: render_move_usage prompts for a team with none selected, and draws a titled 6-panel grid with one")

def test_co_occurrence_matches_hand_verified_pairs():
    """
    Hand-traced from the real turnStart board snapshots, my_side="mine":
      game1 (win, my side p2): Basculegion+Sneasler (t1), Sneasler+Whimsicott (t2)
      game2 (loss, my side p1): Basculegion+Whimsicott (t1 AND t3 -- 2 turns),
                                 Garchomp+Whimsicott (t2), Basculegion+Pyroar (t4)
    Pair names are always alphabetically canonicalized, and turn 5 of
    game2 (only Basculegion active, the other slot empty) correctly
    contributes no pair at all.
    """
    conn = _populated_conn()
    df = co_occurrence(conn, my_side="mine")

    expected_pairs = {
        "Basculegion + Sneasler": 1,
        "Sneasler + Whimsicott": 1,
        "Basculegion + Whimsicott": 2,
        "Garchomp + Whimsicott": 1,
        "Basculegion + Pyroar": 1,
    }
    assert set(df["pair"]) == set(expected_pairs), f"got {set(df['pair'])}"
    for pair, expected_turns in expected_pairs.items():
        row = df[df["pair"] == pair].iloc[0]
        assert row["total_turns"] == expected_turns, f"{pair}: expected {expected_turns} turns, got {row['total_turns']}"
        assert row["games_seen"] == 1, f"{pair}: expected 1 game, got {row['games_seen']}"

    conn.close()
    print("PASS: co_occurrence matches hand-verified pairs and turn counts from the real board snapshots")


def test_co_occurrence_mon_filter():
    conn = _populated_conn()
    df = co_occurrence(conn, my_side="mine", mon="Whimsicott")
    assert set(df["pair"]) == {"Basculegion + Whimsicott", "Garchomp + Whimsicott", "Sneasler + Whimsicott"}, \
        f"got {set(df['pair'])}"
    conn.close()
    print("PASS: mon filter narrows co_occurrence to pairs including that species")


def test_co_occurrence_diff():
    """
    The two real sample games don't share any pair on the same side
    (nothing overlaps between the win and the loss), so real-data
    diff mode should legitimately come back empty -- confirmed here
    -- while a synthetic win/loss pair with a known duration
    difference proves the actual diff arithmetic.
    """
    conn = _populated_conn()
    real_diff = co_occurrence(conn, my_side="mine", result="diff")
    assert real_diff.empty, f"expected no overlapping pair between the win and the loss, got {real_diff}"
    conn.close()

    synth = init_db(":memory:")
    synth.execute("INSERT INTO games (game_id, replay_url, my_side, result) VALUES ('synth-win', 'http://x/1', 'p1', 'W')")
    synth.execute("INSERT INTO games (game_id, replay_url, my_side, result) VALUES ('synth-loss', 'http://x/2', 'p1', 'L')")
    for turn in (1, 2, 3):
        synth.execute(
            "INSERT INTO events (game_id, turn, event_type, p1a, p1b) VALUES ('synth-win', ?, 'turnStart', 'Flutter Mane', 'Chien-Pao')",
            (turn,),
        )
    synth.execute(
        "INSERT INTO events (game_id, turn, event_type, p1a, p1b) VALUES ('synth-loss', 1, 'turnStart', 'Flutter Mane', 'Chien-Pao')"
    )
    diff_row = co_occurrence(synth, my_side="mine", result="diff").iloc[0]
    assert diff_row["pair"] == "Chien-Pao + Flutter Mane"
    assert diff_row["diff"] == 2.0, f"got {diff_row['diff']}"
    synth.close()

    print("PASS: co_occurrence diff drops non-overlapping pairs on real data and computes the swing correctly on synthetic data")


def test_render_co_occurrence_draws_bars():
    conn = _populated_conn()
    df = co_occurrence(conn, my_side="mine")
    conn.close()

    fig = Figure()
    render_co_occurrence(df, fig)
    assert len(fig.axes[0].patches) == 5, f"expected 5 bars, got {len(fig.axes[0].patches)}"
    print("PASS: render_co_occurrence draws bars for real data")


def test_render_co_occurrence_handles_empty_data():
    fig = Figure()
    render_co_occurrence(pd.DataFrame(columns=["pair", "games_seen", "total_turns", "avg_turns_per_game"]), fig)
    print("PASS: render_co_occurrence handles an empty DataFrame without crashing")


def test_ko_credit_matches_hand_verified_counts():
    """
    Hand-traced from the full move/damage/faint sequence (see
    test_parser.py's KO-credit tests for the line-by-line trace):
    Sylveon (opponent) credited with 3 KOs, Basculegion/Sneasler/
    Whimsicott (mine) each credited with 1. The 7th faint
    (Basculegion, game2 turn 5) is unattributed and correctly doesn't
    appear in this output at all -- not as a 0, not as an "Unknown"
    row, just absent.
    """
    conn = _populated_conn()
    df = ko_credit(conn)

    assert set(df["mon"]) == {"Sylveon", "Basculegion", "Sneasler", "Whimsicott"}, f"got {set(df['mon'])}"
    assert df[df["mon"] == "Sylveon"].iloc[0]["ko_count"] == 3
    for mon in ("Basculegion", "Sneasler", "Whimsicott"):
        assert df[df["mon"] == mon].iloc[0]["ko_count"] == 1, f"{mon}: expected 1 KO"
    assert df["ko_count"].sum() == 6, "expected 6 credited KOs total (7 faints minus 1 unattributed)"

    conn.close()
    print("PASS: ko_credit matches hand-verified counts, with the unattributed faint correctly absent rather than a 0")


def test_ko_credit_my_side_filters_by_the_attackers_side_not_the_victims():
    """
    my_side here means "whose Pokemon secured the KO", not "who got
    KO'd" -- mine=Basculegion/Sneasler/Whimsicott (1 each, salmoncashew's
    side both games), opponent=Sylveon (3, the opponent's side).
    """
    conn = _populated_conn()
    mine = ko_credit(conn, my_side="mine")
    opponent = ko_credit(conn, my_side="opponent")

    assert set(mine["mon"]) == {"Basculegion", "Sneasler", "Whimsicott"}, f"got {set(mine['mon'])}"
    assert set(opponent["mon"]) == {"Sylveon"}, f"got {set(opponent['mon'])}"
    assert opponent.iloc[0]["ko_count"] == 3

    conn.close()
    print("PASS: my_side filters ko_credit by the credited attacker's side, not the victim's")


def test_ko_credit_mon_filter():
    conn = _populated_conn()
    df = ko_credit(conn, mon="Sylveon")
    assert list(df["mon"]) == ["Sylveon"]
    assert df.iloc[0]["ko_count"] == 3
    conn.close()
    print("PASS: mon filter narrows ko_credit to KOs credited to that specific Pokemon")


def test_ko_credit_diff():
    """
    None of the real sample data's credited mons secured a KO in
    BOTH a win and a loss (each only shows up in one or the other),
    so real-data diff mode should legitimately come back empty --
    confirmed here -- while synthetic win/loss data with a known KO
    count difference proves the actual diff arithmetic.
    """
    conn = _populated_conn()
    real_diff = ko_credit(conn, result="diff")
    assert real_diff.empty, f"expected no mon with KOs in both a win and a loss, got {real_diff}"
    conn.close()

    synth = init_db(":memory:")
    synth.execute("INSERT INTO games (game_id, replay_url, my_side, result) VALUES ('synth-win', 'http://x/1', 'p1', 'W')")
    synth.execute("INSERT INTO games (game_id, replay_url, my_side, result) VALUES ('synth-loss', 'http://x/2', 'p1', 'L')")
    for turn in (1, 2):
        synth.execute(
            "INSERT INTO events (game_id, turn, event_type, ko_credit_mon, ko_credit_side) "
            "VALUES ('synth-win', ?, 'faint', 'Flutter Mane', 'p1')",
            (turn,),
        )
    synth.execute(
        "INSERT INTO events (game_id, turn, event_type, ko_credit_mon, ko_credit_side) "
        "VALUES ('synth-loss', 1, 'faint', 'Flutter Mane', 'p1')"
    )
    diff_row = ko_credit(synth, result="diff").iloc[0]
    assert diff_row["mon"] == "Flutter Mane"
    assert diff_row["diff"] == 1.0, f"got {diff_row['diff']}"
    synth.close()

    print("PASS: ko_credit diff drops mons without both a win and a loss on real data and computes the swing correctly on synthetic data")


def test_render_ko_credit_draws_bars():
    conn = _populated_conn()
    df = ko_credit(conn)
    conn.close()

    fig = Figure()
    render_ko_credit(df, fig)
    assert len(fig.axes[0].patches) == 4, f"expected 4 bars, got {len(fig.axes[0].patches)}"
    print("PASS: render_ko_credit draws bars for real data")


def test_render_ko_credit_handles_empty_data():
    fig = Figure()
    render_ko_credit(pd.DataFrame(columns=["mon", "ko_count", "games_seen", "avg_kos_per_game"]), fig)
    print("PASS: render_ko_credit handles an empty DataFrame without crashing")

if __name__ == "__main__":
    test_registry_contains_turns_on_field()
    test_registry_contains_move_usage()
    test_diff_capable_chart_types_is_bar_only()
    test_turns_on_field_matches_hand_verified_counts()
    test_turns_on_field_result_filter()
    test_turns_on_field_my_side_partitions_cleanly()
    test_turns_on_field_team_filter()
    test_turns_on_field_mon_filter()
    test_turns_on_field_diff()
    test_turns_on_field_diff_drops_mons_without_both_a_win_and_a_loss()
    test_render_turns_on_field_draws_bars()
    test_render_turns_on_field_handles_empty_data()
    test_render_turns_on_field_diff_draws_diverging_bars()
    test_render_turns_on_field_handles_empty_diff()
    test_move_usage_no_team_returns_empty_dict()
    test_move_usage_covers_the_whole_roster_in_team_preview_order()
    test_move_usage_matches_hand_verified_counts()
    test_move_usage_scopes_to_the_selected_teams_own_games()
    test_move_usage_my_side_distinguishes_same_species_different_owner()
    test_move_usage_struggle_is_always_excluded()
    test_render_move_usage_prompts_for_a_team_and_draws_a_grid()
    test_co_occurrence_matches_hand_verified_pairs()
    test_co_occurrence_mon_filter()
    test_co_occurrence_diff()
    test_render_co_occurrence_draws_bars()
    test_render_co_occurrence_handles_empty_data()
    test_ko_credit_matches_hand_verified_counts()
    test_ko_credit_my_side_filters_by_the_attackers_side_not_the_victims()
    test_ko_credit_mon_filter()
    test_ko_credit_diff()
    test_render_ko_credit_draws_bars()
    test_render_ko_credit_handles_empty_data()
    print("\nAll stats tests passed.")