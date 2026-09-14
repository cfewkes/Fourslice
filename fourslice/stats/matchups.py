"""
fourslice/stats/matchups.py

Matchup and lead stats for the Statistics page's new dropdown entries.
All queries resolve the opponent side per game from games.my_side (p1↔p2)
and score records only against games with a W/L result so win% is always
defined — same W/L discipline as best/worst's min-games gate.
"""

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# shared base: every turnStart snapshot scoped by regulation / team
# ---------------------------------------------------------------------------

def _turn_starts(conn, regulation=None, team_id=None):
    """
    Every turnStart row joined to its game row (my_side / result /
    regulation / team_id / battle_size). Games without a resolved
    my_side are excluded — neither side can be classified as mine from
    them, and their games rows carry no team_id anyway. Only games
    with a W/L result are included so win% is always defined.
    """
    query = """
        SELECT e.game_id, e.turn, e.p1a, e.p1b, e.p2a, e.p2b,
               g.my_side    AS game_my_side,
               g.result     AS game_result,
               g.battle_size AS battle_size,
               g.regulation AS regulation,
               g.team_id    AS team_id
        FROM events e
        JOIN games g ON e.game_id = g.game_id
        WHERE e.event_type = 'turnStart'
          AND g.my_side IS NOT NULL
          AND g.result IN ('W', 'L')
    """
    params: list = []
    if regulation:
        query += " AND g.regulation = ?"
        params.append(regulation)
    if team_id is not None:
        query += " AND g.team_id = ?"
        params.append(team_id)
    return pd.read_sql(query, conn, params=params)


# ---------------------------------------------------------------------------
# opponent records: one row per opponent species seen anywhere on the
# opponent's side across the scoped games, with its record vs you
# ---------------------------------------------------------------------------

def opponent_records(conn, regulation=None, team_id=None) -> pd.DataFrame:
    """
    One row per opponent Pokemon seen in the scoped games, with the
    all-time record vs it:

        mon, games, wins, losses, pct

    Opponent is resolved per game: the side that is NOT games.my_side.
    `games` counts DISTINCT games where that mon appeared on the
    opponent's field at any point (union of the opponent's two slots
    across every turnStart snapshot of that game). So a mon that
    survived many turns in one game counts once.

    `pct` = 100 * wins / games, guarded to 0.0 when games == 0 (can't
    happen in normal data — options only include mons seen in ≥1 game —
    but kept defensive for headless/empty-DB construction).

    Ordered games desc, pct desc, mon asc so head(6) is deterministic.
    """
    turns_df = _turn_starts(conn, regulation=regulation, team_id=team_id)
    if turns_df.empty:
        return pd.DataFrame(columns=["mon", "games", "wins", "losses", "pct"])

    melted = turns_df.melt(
        id_vars=["game_id", "turn", "game_my_side", "game_result"],
        value_vars=["p1a", "p1b", "p2a", "p2b"],
        var_name="slot",
        value_name="mon",
    ).dropna(subset=["mon"])
    # defensive: some parsers may emit "" for an empty slot
    melted = melted[melted["mon"] != ""]
    melted["side"] = melted["slot"].str[:2]
    is_mine = melted["side"] == melted["game_my_side"]
    opp = melted[~is_mine].copy()

    # distinct per (game, mon)
    opp = opp.drop_duplicates(subset=["game_id", "mon"])

    summary = (
        opp.groupby("mon", as_index=False)
        .agg(
            games=("game_id", "nunique"),
            wins=("game_result", lambda s: (s == "W").sum()),
        )
    )
    summary["losses"] = summary["games"] - summary["wins"]
    summary["pct"] = np.where(
        summary["games"] > 0,
        summary["wins"] / summary["games"] * 100.0,
        0.0,
    )
    return summary.sort_values(
        ["games", "pct", "mon"], ascending=[False, False, True]
    ).reset_index(drop=True)


def every_opponent_mon(conn, regulation=None, team_id=None) -> list[str]:
    """
    Distinct species ever seen on the opponent's side in the scoped
    games (opponent side = not games.my_side), sorted alphabetically.
    Drives the two matchup views' Pokemon filter options.
    """
    df = opponent_records(conn, regulation=regulation, team_id=team_id)
    if df.empty:
        return []
    return sorted(df["mon"].tolist())


def top_n_opponents(conn, n: int = 6, regulation=None, team_id=None) -> pd.DataFrame:
    """Top n opponent mons by games seen — the Top 6 Opponents grid."""
    df = opponent_records(conn, regulation=regulation, team_id=team_id)
    return df.head(n).reset_index(drop=True)


def best_worst_matchups(
    conn, *, best: bool = True, min_games: int = 4, n: int = 6,
    regulation=None, team_id=None,
) -> pd.DataFrame:
    """
    Best (highest win%) or worst (lowest win%) opponent mons, with a
    minimum-games gate (default 4 — per the feature spec). Returns
    whatever qualifies, sorted accordingly. Tie-break games desc, then
    mon asc, so a 4-0 at 100% outranks a 6-2 also at 75% etc.,
    deterministically.
    """
    df = opponent_records(conn, regulation=regulation, team_id=team_id)
    if df.empty:
        return df
    df = df[df["games"] >= min_games].copy()
    if df.empty:
        return df.reset_index(drop=True)
    # best: pct desc; worst: pct asc — both tie-break games desc, mon asc
    if best:
        return df.sort_values(
            ["pct", "games", "mon"], ascending=[False, False, True]
        ).head(n).reset_index(drop=True)
    return df.sort_values(
        ["pct", "games", "mon"], ascending=[True, False, True]
    ).head(n).reset_index(drop=True)


# ---------------------------------------------------------------------------
# leads: one row per game = my side's first turnStart's active slots
# ---------------------------------------------------------------------------

def per_game_leads(conn, regulation=None, team_id=None) -> pd.DataFrame:
    """
    One row per game where a lead can be read:

        game_id, battle_size, result, lead_a, lead_b

    The lead is my side's non-None slots in the FIRST turnStart
    snapshot of that game (smallest turn). For doubles both slots are
    populated; for singles lead_b is None. Games whose minimal
    turnStart has no usable my-side slot are dropped.
    """
    turns_df = _turn_starts(conn, regulation=regulation, team_id=team_id)
    if turns_df.empty:
        return pd.DataFrame(columns=["game_id", "battle_size", "result", "lead_a", "lead_b"])

    first = (
        turns_df.sort_values(["game_id", "turn"])
        .groupby("game_id", as_index=False)
        .first()
    )
    # vectorized per-row side resolution (no .lookup — removed in pandas 2)
    is_p1 = first["game_my_side"] == "p1"
    first["lead_a"] = np.where(is_p1, first["p1a"], first["p2a"])
    first["lead_b"] = np.where(is_p1, first["p1b"], first["p2b"])
    # normalise empty strings / NaN for the lead columns
    first["lead_a"] = first["lead_a"].replace("", np.nan)
    first["lead_b"] = first["lead_b"].replace("", np.nan)

    out = first[["game_id", "battle_size", "game_result", "lead_a", "lead_b"]].copy()
    out = out.rename(columns={"game_result": "result"})
    # drop games where we couldn't read a lead at all
    out = out.dropna(subset=["lead_a"])
    # singles: collapse the NaN lead_b to None for a clean column
    out["lead_b"] = out["lead_b"].where(out["lead_b"].notna(), None)
    # battle_size may be NULL for synthetic games inserted without it;
    # treat that as singles when figuring bucket membership downstream —
    # but keep the stored value so the caller can see it.
    return out.reset_index(drop=True)


def lead_records(conn, regulation=None, team_id=None) -> dict[str, pd.DataFrame]:
    """
    Groups per_game_leads by battle size. Returns a dict with keys
    "singles" and "doubles", each a DataFrame with

        lead (label), lead_a, lead_b, games, wins, losses, pct

    Singles: lead == lead_a, lead_b is None.
    Doubles: lead == canonical "A + B" (sorted, like co_occurrence's
    pair) — so the same unordered pair always lands in the same row
    regardless of which slot (a/b) either happened to be in.
    Each bucket is sorted games desc, pct desc, lead asc.
    """
    leads = per_game_leads(conn, regulation=regulation, team_id=team_id)
    empty_cols = ["lead", "lead_a", "lead_b", "games", "wins", "losses", "pct"]
    if leads.empty:
        return {
            "singles": pd.DataFrame(columns=empty_cols),
            "doubles": pd.DataFrame(columns=empty_cols),
        }

    # normalise battle_size for bucketing (None/missing -> singles-ish,
    # but keep display label as the stored value's bucket)
    leads["_bucket"] = np.where(leads["battle_size"] == "doubles", "doubles", "singles")

    buckets: dict[str, pd.DataFrame] = {}
    for bucket in ("singles", "doubles"):
        sub = leads[leads["_bucket"] == bucket].copy()
        if sub.empty:
            buckets[bucket] = pd.DataFrame(columns=empty_cols)
            continue
        if bucket == "singles":
            sub["lead"] = sub["lead_a"]
            # lead_b already None
            group_col = "lead"
            agg_spec = {
                "lead_a": ("lead_a", "first"),
                "lead_b": ("lead_b", "first"),
                "games": ("game_id", "nunique"),
                "wins": ("result", lambda s: (s == "W").sum()),
            }
        else:
            # canonical unordered pair label for grouping
            sub["lead"] = sub.apply(
                lambda r: " + ".join(sorted([str(r["lead_a"]), str(r["lead_b"])])),
                axis=1,
            )
            group_col = "lead"
            agg_spec = {
                "lead_a": ("lead_a", "first"),
                "lead_b": ("lead_b", "first"),
                "games": ("game_id", "nunique"),
                "wins": ("result", lambda s: (s == "W").sum()),
            }
        summary = sub.groupby(group_col, as_index=False).agg(**agg_spec)
        summary["losses"] = summary["games"] - summary["wins"]
        summary["pct"] = np.where(
            summary["games"] > 0,
            summary["wins"] / summary["games"] * 100.0,
            0.0,
        )
        summary = summary[empty_cols].sort_values(
            ["games", "pct", "lead"], ascending=[False, False, True]
        ).reset_index(drop=True)
        buckets[bucket] = summary

    return buckets


def dominant_battle_size(lead_buckets: dict) -> str | None:
    """
    Which battle size's leads to show when both have data — the one
    with MORE total lead-games (the feature's "only the more-common
    battle size"). A tie prefers doubles (its pairs are the richer
    view). Returns "singles", "doubles", or None when neither bucket
    has any games.
    """
    singles = lead_buckets.get("singles", pd.DataFrame())
    doubles = lead_buckets.get("doubles", pd.DataFrame())
    s_total = int(singles["games"].sum()) if not singles.empty else 0
    d_total = int(doubles["games"].sum()) if not doubles.empty else 0
    if s_total == 0 and d_total == 0:
        return None
    if d_total > s_total:
        return "doubles"
    if s_total > d_total:
        return "singles"
    return "singles"  # exact tie


def doubles_lead_with_teammates(
    conn, mon: str, team_id: int, regulation=None,
) -> pd.DataFrame:
    """
    In doubles, the lead pairs that INCLUDE `mon` — i.e. mon paired
    with every other teammate it has actually led with — ordered by win%
    descending (max 5 pairs for a 6-mon roster). Each row has the same
    columns as the doubles bucket of lead_records (lead, lead_a,
    lead_b, games, wins, losses, pct). Pairs that never actually led
    together simply don't appear (no fabricated 0-0 rows).
    """
    if not mon or team_id is None:
        return pd.DataFrame(columns=["lead", "lead_a", "lead_b", "games", "wins", "losses", "pct"])

    leads = per_game_leads(conn, regulation=regulation, team_id=team_id)
    if leads.empty:
        return pd.DataFrame(columns=["lead", "lead_a", "lead_b", "games", "wins", "losses", "pct"])

    doubles = leads[leads["battle_size"] == "doubles"].copy()
    if doubles.empty:
        return pd.DataFrame(columns=["lead", "lead_a", "lead_b", "games", "wins", "losses", "pct"])

    doubles["lead"] = doubles.apply(
        lambda r: " + ".join(sorted([str(r["lead_a"]), str(r["lead_b"])])),
        axis=1,
    )
    # keep only pairs containing mon
    doubles = doubles[(doubles["lead_a"] == mon) | (doubles["lead_b"] == mon)]
    if doubles.empty:
        return pd.DataFrame(columns=["lead", "lead_a", "lead_b", "games", "wins", "losses", "pct"])

    summary = doubles.groupby("lead", as_index=False).agg(
        lead_a=("lead_a", "first"),
        lead_b=("lead_b", "first"),
        games=("game_id", "nunique"),
        wins=("result", lambda s: (s == "W").sum()),
    )
    summary["losses"] = summary["games"] - summary["wins"]
    summary["pct"] = np.where(
        summary["games"] > 0,
        summary["wins"] / summary["games"] * 100.0,
        0.0,
    )
    summary = summary[["lead", "lead_a", "lead_b", "games", "wins", "losses", "pct"]].sort_values(
        ["pct", "games", "lead"], ascending=[False, False, True]
    ).reset_index(drop=True)
    return summary.head(5).reset_index(drop=True)
