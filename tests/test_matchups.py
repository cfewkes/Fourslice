"""
Run directly with: python tests/test_matchups.py

Validates the matchup/lead data layer (fourslice/stats/matchups.py)
against the two real, hand-verified sample games plus synthetic
fixtures for the min-games gate and singles-shape cases.
"""

import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fourslice.storage import init_db, import_replay, resolve_team_id
from fourslice.stats.matchups import (
    opponent_records, every_opponent_mon, top_n_opponents,
    best_worst_matchups, per_game_leads, lead_records,
    dominant_battle_size, doubles_lead_with_teammates,
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


def test_opponent_records_matches_hand_verified_counts():
    """
    game1 (win, my side p2): opponents that entered battle = {Sneasler, Blastoise}.
    game2 (loss, my side p1): opponents = {Aerodactyl, Charizard, Garchomp, Sylveon}.
    Each appeared in exactly one game, so 1-0 / 0-1 and 100% / 0%.
    """
    conn = _populated_conn()
    df = opponent_records(conn)

    assert set(df["mon"]) == {"Sneasler", "Blastoise", "Aerodactyl", "Garchomp", "Charizard", "Sylveon"}, \
        f"got {set(df['mon'])}"
    rows = df.set_index("mon")
    for mon in ("Sneasler", "Blastoise"):
        assert (rows.loc[mon]["games"], rows.loc[mon]["wins"], rows.loc[mon]["losses"]) == (1, 1, 0)
        assert rows.loc[mon]["pct"] == 100.0
    for mon in ("Aerodactyl", "Garchomp", "Charizard", "Sylveon"):
        assert (rows.loc[mon]["games"], rows.loc[mon]["wins"], rows.loc[mon]["losses"]) == (1, 0, 1)
        assert rows.loc[mon]["pct"] == 0.0

    conn.close()
    print("PASS: opponent_records matches the hand-verified per-game opponent rosters and records")


def test_opponent_records_team_and_regulation_scope():
    conn = _populated_conn()
    real_team_id = conn.execute("SELECT team_id FROM games WHERE team_id IS NOT NULL LIMIT 1").fetchone()[0]

    by_team = opponent_records(conn, team_id=real_team_id)
    assert len(by_team) == 6, f"expected both games' opponents under the shared team, got {len(by_team)}"

    unrelated_team_id = resolve_team_id(conn, [
        {"species": "Flutter Mane", "item": "", "moves": []},
        {"species": "Chien-Pao", "item": "", "moves": []},
    ])
    assert opponent_records(conn, team_id=unrelated_team_id).empty, \
        "a team with no games should contribute nothing"

    assert len(opponent_records(conn, regulation="M-B")) == 6, "both sample games are M-B"
    assert opponent_records(conn, regulation="H").empty, "no H games in the sample data"

    conn.close()
    print("PASS: opponent_records narrows by team_id and regulation correctly")


def test_every_opponent_mon_is_sorted_and_distinct():
    conn = _populated_conn()
    mons = every_opponent_mon(conn)
    assert mons == sorted(mons) == ["Aerodactyl", "Blastoise", "Charizard", "Garchomp", "Sneasler", "Sylveon"], \
        f"got {mons}"
    assert len(mons) == len(set(mons)), "no duplicates -- a mon seen in two games is one option"
    assert every_opponent_mon(conn, regulation="H") == []
    conn.close()
    print("PASS: every_opponent_mon returns sorted, distinct opponent species for the Pokemon filter")


def test_top_n_opponents_head():
    conn = _populated_conn()
    df = top_n_opponents(conn, n=3)
    assert len(df) == 3
    # all mon/games paired rows; ordering by games desc is trivially satisfied here (all 1)
    assert set(df["mon"]) == {"Sneasler", "Blastoise", "Aerodactyl"}, \
        f"first two by 1-0 / 100% (tie broken by mon asc), got {list(df['mon'])}"
    conn.close()
    print("PASS: top_n_opponents returns head(n)")


def test_best_worst_matchups_min_games_real_data_is_empty():
    """Every sample opponent has exactly 1 game, so both Best and Worst
    must come back empty at min_games=4 -- the GUI's empty-state path."""
    conn = _populated_conn()
    assert best_worst_matchups(conn).empty, "no opponent has >= 4 games in the sample data"
    assert best_worst_matchups(conn, best=False).empty
    # at min_games=1 everything qualifies again
    df = best_worst_matchups(conn, min_games=1)
    assert len(df) == 6
    conn.close()
    print("PASS: best_worst_matchups enforces the min-games gate (empty on 1-game sample)")


def test_best_worst_matchups_synthetic_ordering_and_boundary():
    """
    4 win-games vs Landorus + Urshifu (each 4-0, 100%); 4 loss-games vs
    Indeedee + Oranguru (each 0-4, 0%); 3 loss-games vs Baxcalibur (0-3).
    Best lists Landorus before Urshifu (equal, mon asc); Worst lists
    Indeedee before Oranguru; Baxcalibur appears only at min_games=3.
    """
    synth = init_db(":memory:")
    for i in range(4):
        sid = f"w{i}"
        synth.execute(
            "INSERT INTO games (game_id, replay_url, my_side, result, battle_size) VALUES (?, 'http://x', 'p1', 'W', 'doubles')",
            (sid,),
        )
        synth.execute(
            "INSERT INTO events (game_id, turn, event_type, p1a, p1b, p2a, p2b) "
            "VALUES (?, 1, 'turnStart', 'A', 'B', 'Landorus', 'Urshifu')",
            (sid,),
        )
    for i in range(4):
        sid = f"l{i}"
        synth.execute(
            "INSERT INTO games (game_id, replay_url, my_side, result, battle_size) VALUES (?, 'http://x', 'p1', 'L', 'doubles')",
            (sid,),
        )
        synth.execute(
            "INSERT INTO events (game_id, turn, event_type, p1a, p1b, p2a, p2b) "
            "VALUES (?, 1, 'turnStart', 'A', 'B', 'Indeedee', 'Oranguru')",
            (sid,),
        )
    for i in range(3):
        sid = f"bx{i}"
        synth.execute(
            "INSERT INTO games (game_id, replay_url, my_side, result, battle_size) VALUES (?, 'http://x', 'p1', 'L', 'doubles')",
            (sid,),
        )
        synth.execute(
            "INSERT INTO events (game_id, turn, event_type, p1a, p1b, p2a, p2b) "
            "VALUES (?, 1, 'turnStart', 'A', 'B', 'Baxcalibur', 'Baxcalibur')",
            (sid,),
        )

    # All four 4-game mons clear the min-4 gate -- Best ranks win% desc
    # (Landorus/Urshifu's 100% ahead of Indeedee/Oranguru's 0%) and the
    # 3-game Baxcalibur is excluded no matter how it sorts.
    best = best_worst_matchups(synth)
    assert list(best["mon"]) == ["Landorus", "Urshifu", "Indeedee", "Oranguru"], \
        f"got {list(best['mon'])}"
    assert "Baxcalibur" not in set(best["mon"]), "3 games must NOT clear the min-4 gate"

    worst = best_worst_matchups(synth, best=False)
    assert list(worst["mon"]) == ["Indeedee", "Oranguru", "Landorus", "Urshifu"], \
        f"got {list(worst['mon'])}"

    worst_three = best_worst_matchups(synth, best=False, min_games=3)
    assert "Baxcalibur" in set(worst_three["mon"]), "exactly 3 games should clear min_games=3"

    synth.close()
    print("PASS: best_worst_matchups orders Best/Worst by pct and enforces the min-games boundary")


def test_per_game_leads_doubles_are_the_turn1_pair():
    """game2 (loss, my p1) leads Whimsicott+Basculegion; game1 (win, my p2)
    leads Basculegion+Sneasler -- exactly the turn-1 board from my side."""
    conn = _populated_conn()
    leads = per_game_leads(conn)
    assert set(leads["battle_size"]) == {"doubles"}

    g1 = leads[leads["game_id"].str.endswith("2647404582")].iloc[0]  # the win, my p2
    assert (g1["lead_a"], g1["lead_b"]) == ("Basculegion", "Sneasler"), f"got {g1['lead_a']}/{g1['lead_b']}"
    assert g1["result"] == "W"

    g2 = leads[leads["game_id"].str.endswith("2647349643")].iloc[0]  # the loss, my p1
    assert (g2["lead_a"], g2["lead_b"]) == ("Whimsicott", "Basculegion"), f"got {g2['lead_a']}/{g2['lead_b']}"
    assert g2["result"] == "L"

    conn.close()
    print("PASS: per_game_leads reads my turn-1 pair as the lead, per game")


def test_lead_records_doubles_pair_records():
    conn = _populated_conn()
    buckets = lead_records(conn)
    assert dominant_battle_size(buckets) == "doubles"
    assert buckets["singles"].empty

    df = buckets["doubles"].set_index("lead")
    assert set(df.index) == {"Basculegion + Sneasler", "Basculegion + Whimsicott"}
    bs = df.loc["Basculegion + Sneasler"]
    assert (bs["games"], bs["wins"], bs["losses"]) == (1, 1, 0) and bs["pct"] == 100.0
    bw = df.loc["Basculegion + Whimsicott"]
    assert (bw["games"], bw["wins"], bw["losses"]) == (1, 0, 1) and bw["pct"] == 0.0

    conn.close()
    print("PASS: lead_records builds canonical pair records with wins/losses/pct")


def test_doubles_lead_with_teammates_orders_by_pct_desc():
    conn = _populated_conn()
    team_id = conn.execute("SELECT team_id FROM teams").fetchone()[0]
    df = doubles_lead_with_teammates(conn, "Basculegion", team_id)
    # Sneasler pair is 1-0 (100%) and must outrank the Whimsicott pair (0-1, 0%)
    assert list(df["lead"]) == ["Basculegion + Sneasler", "Basculegion + Whimsicott"], f"got {list(df['lead'])}"
    assert df.iloc[0]["pct"] == 100.0
    assert df.iloc[1]["pct"] == 0.0
    conn.close()
    print("PASS: doubles_lead_with_teammates orders pairs by winning percentage, descending")


def test_leads_singles_shape_with_synthetic_singles_game():
    synth = init_db(":memory:")
    synth.execute(
        "INSERT INTO games (game_id, replay_url, my_side, result, battle_size) VALUES ('synth-singles', 'http://x', 'p1', 'W', 'singles')"
    )
    synth.execute(
        "INSERT INTO events (game_id, turn, event_type, p1a, p2a) VALUES ('synth-singles', 1, 'turnStart', 'Flutter Mane', 'Chien-Pao')"
    )

    leads = per_game_leads(synth)
    assert leads.iloc[0]["lead_a"] == "Flutter Mane"
    assert leads.iloc[0]["lead_b"] is None, f"singles leads must have no second slot, got {leads.iloc[0]['lead_b']}"
    assert leads.iloc[0]["battle_size"] == "singles"

    buckets = lead_records(synth)
    assert dominant_battle_size(buckets) == "singles"
    assert buckets["doubles"].empty
    row = buckets["singles"].set_index("lead").loc["Flutter Mane"]
    assert (row["games"], row["wins"], row["losses"]) == (1, 1, 0) and row["pct"] == 100.0

    synth.close()
    print("PASS: a synthetic singles game produces a single-mon lead with lead_b None")


def test_leading_with_teammates_dominance_tie_and_absent_pairs():
    """
    With only doubles games, dominance is doubles; a pair never actually
    led together never gets a fabricated 0-0 row (Incineroar/Pyroar/Garchomp
    never led in the sample games).
    """
    conn = _populated_conn()
    team_id = conn.execute("SELECT team_id FROM teams").fetchone()[0]
    df = doubles_lead_with_teammates(conn, "Basculegion", team_id)
    assert len(df) == 2, "only pairs that actually led together should appear"
    conn.close()
    print("PASS: absent lead partners produce no fabricated rows")


def test_empty_database_is_clean():
    conn = init_db(":memory:")
    assert opponent_records(conn).empty
    assert every_opponent_mon(conn) == []
    assert top_n_opponents(conn).empty
    assert best_worst_matchups(conn).empty
    buckets = lead_records(conn)
    assert buckets["singles"].empty and buckets["doubles"].empty
    assert dominant_battle_size(buckets) is None
    assert per_game_leads(conn).empty
    conn.close()
    print("PASS: all matchup/lead functions are safe on an empty database")


if __name__ == "__main__":
    test_opponent_records_matches_hand_verified_counts()
    test_opponent_records_team_and_regulation_scope()
    test_every_opponent_mon_is_sorted_and_distinct()
    test_top_n_opponents_head()
    test_best_worst_matchups_min_games_real_data_is_empty()
    test_best_worst_matchups_synthetic_ordering_and_boundary()
    test_per_game_leads_doubles_are_the_turn1_pair()
    test_lead_records_doubles_pair_records()
    test_doubles_lead_with_teammates_orders_by_pct_desc()
    test_leads_singles_shape_with_synthetic_singles_game()
    test_leading_with_teammates_dominance_tie_and_absent_pairs()
    test_empty_database_is_clean()
    print("\nAll matchup tests passed.")