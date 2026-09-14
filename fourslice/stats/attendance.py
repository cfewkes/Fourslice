"""
fourslice/stats/attendance.py

Opponent Attendance: tracks how often an opponent's Pokemon appears on their
6-mon team roster vs. how often they actually bring it to battle.

Only meaningful for doubles/VGC formats where you bring 4 of 6.
For singles, every Pokemon on the team is always brought (100% bring rate).
"""

import pandas as pd


def attendance(conn, regulation=None, result=None, my_side=None, team_id=None, mon=None) -> pd.DataFrame:
    """
    Returns one row per opponent Pokemon species with:
        mon, team_games, bring_games, total_games, team_rate, bring_rate, bring_rate_overall

    - team_games: distinct games where this species was on opponent's 6-mon roster
    - bring_games: distinct games where this species appeared on field (brought)
    - total_games: total games in the filtered set (opponent had a team)
    - team_rate: 100 * team_games / total_games -- % of games opponent has this mon on roster
    - bring_rate: 100 * bring_games / team_games -- % of roster appearances that are brought
    - bring_rate_overall: 100 * bring_games / total_games -- % of all games this mon is brought

    Filters:
    - regulation: filter by regulation
    - result: "W", "L", or "diff" (win-average minus loss-average of bring_rate)
    - my_side: "mine" or "opponent" -- whose perspective; "mine" = opponent is the other side
    - team_id: filter to games where user brought this specific team
    - mon: restrict to a single Pokemon species

    Only doubles games are considered (singles always brings all 6).
    Games without opponent team data are excluded.
    """
    if result == "diff":
        return _attendance_diff(conn, regulation=regulation, my_side=my_side, team_id=team_id, mon=mon)

    # Get all qualifying games (doubles, with opponent team data, resolved my_side, W/L result)
    query = """
        SELECT g.game_id, g.my_side, g.result, g.battle_size
        FROM games g
        JOIN opponent_teams ot ON ot.game_id = g.game_id
        WHERE g.battle_size = 'doubles'
          AND g.my_side IS NOT NULL
          AND g.result IN ('W', 'L')
    """
    params = []
    if regulation:
        query += " AND g.regulation = ?"
        params.append(regulation)
    if result:
        query += " AND g.result = ?"
        params.append(result)
    if team_id:
        query += " AND g.team_id = ?"
        params.append(team_id)

    games_df = pd.read_sql(query, conn, params=params)
    if games_df.empty:
        return pd.DataFrame(columns=["mon", "team_games", "bring_games", "total_games", "team_rate", "bring_rate", "bring_rate_overall"])

    # Get opponent team rosters for these games
    game_ids = tuple(games_df["game_id"].tolist())
    placeholders = ",".join("?" for _ in game_ids)
    roster_query = f"""
        SELECT game_id, species FROM opponent_teams
        WHERE game_id IN ({placeholders})
    """
    roster_df = pd.read_sql(roster_query, conn, params=list(game_ids))

    # Get which Pokemon actually appeared on field for these games (opponent side only)
    # We need to resolve opponent side per game from my_side
    appeared_query = f"""
        SELECT e.game_id, e.turn, e.p1a, e.p1b, e.p2a, e.p2b, g.my_side
        FROM events e
        JOIN games g ON e.game_id = g.game_id
        WHERE e.event_type = 'turnStart'
          AND e.game_id IN ({placeholders})
    """
    appeared_df = pd.read_sql(appeared_query, conn, params=list(game_ids))

    # Melt to get all active slots, then filter to opponent side
    melted = appeared_df.melt(
        id_vars=["game_id", "turn", "my_side"], value_vars=["p1a", "p1b", "p2a", "p2b"],
        var_name="slot", value_name="mon",
    ).dropna(subset=["mon"])
    melted["side"] = melted["slot"].str[:2]
    # Keep only opponent side
    is_mine = melted["side"] == melted["my_side"]
    opp_appeared = melted[~is_mine].copy()
    opp_appeared = opp_appeared.drop_duplicates(subset=["game_id", "mon"])[["game_id", "mon"]]

    # Compute per-species stats
    total_games = games_df["game_id"].nunique()

    # Team games: distinct games where species was on opponent roster
    team_games = roster_df.groupby("species")["game_id"].nunique().reset_index()
    team_games.columns = ["mon", "team_games"]

    # Bring games: distinct games where species appeared on field
    bring_games = opp_appeared.groupby("mon")["game_id"].nunique().reset_index()
    bring_games.columns = ["mon", "bring_games"]

    # Merge
    summary = team_games.merge(bring_games, on="mon", how="left")
    summary["bring_games"] = summary["bring_games"].fillna(0).astype(int)
    summary["total_games"] = total_games
    summary["team_rate"] = 100.0 * summary["team_games"] / summary["total_games"]
    summary["bring_rate"] = 100.0 * summary["bring_games"] / summary["team_games"]
    summary["bring_rate_overall"] = 100.0 * summary["bring_games"] / summary["total_games"]

    if mon:
        summary = summary[summary["mon"] == mon]

    if my_side in ("mine", "opponent"):
        # For attendance, my_side doesn't change the grouping (always opponent's Pokemon)
        # But it could filter which games we consider based on who "we" are
        # Currently we always track opponent attendance, so this is a no-op for grouping
        pass

    return summary.sort_values("team_rate", ascending=False).reset_index(drop=True)


def _attendance_diff(conn, regulation, my_side, team_id, mon) -> pd.DataFrame:
    """
    Win-average minus loss-average of bring_rate, per Pokemon.
    Inner join: only Pokemon that appear in both wins and losses.
    """
    wins = attendance(conn, regulation=regulation, result="W", my_side=my_side, team_id=team_id, mon=mon)
    losses = attendance(conn, regulation=regulation, result="L", my_side=my_side, team_id=team_id, mon=mon)

    merged = wins.merge(losses, on="mon", suffixes=("_win", "_loss"))
    merged["bring_rate_diff"] = merged["bring_rate_win"] - merged["bring_rate_loss"]
    merged["team_rate_diff"] = merged["team_rate_win"] - merged["team_rate_loss"]

    keep_cols = ["mon", "bring_rate_diff", "team_rate_diff",
                 "bring_rate_win", "bring_rate_loss",
                 "team_rate_win", "team_rate_loss"]
    return merged[keep_cols].sort_values("bring_rate_diff", key=lambda s: s.abs(), ascending=False).reset_index(drop=True)


def render_attendance(df, fig, top_n=8):
    """
    Grouped horizontal bar chart: Team Rate vs Bring Rate per Pokemon.
    Diff mode: diverging bars for bring_rate_diff.
    """
    import matplotlib.pyplot as plt
    import numpy as np

    ax = fig.add_subplot(111)
    if df.empty:
        ax.text(0.5, 0.5, "No data for this filter\n(Doubles games with opponent team data only)",
                ha="center", va="center", transform=ax.transAxes)
        ax.set_xticks([])
        ax.set_yticks([])
        return

    if "bring_rate_diff" in df.columns:
        # Diff mode
        top = df.head(top_n).iloc[::-1]
        colors = ["#2ca02c" if v >= 0 else "#d62728" for v in top["bring_rate_diff"]]
        ax.barh(top["mon"], top["bring_rate_diff"], color=colors)
        ax.axvline(0, color="black", linewidth=0.8)
        ax.set_xlabel("Bring Rate: win minus loss (% points)")
        ax.set_title(f"Attendance -- Win/Loss Bring Rate Difference (top {len(top)})")
        return

    # Normal mode: grouped bars
    top = df.head(top_n).iloc[::-1]
    y_pos = np.arange(len(top))
    height = 0.35

    ax.barh(y_pos + height/2, top["team_rate"], height, label="Team Rate (% of games on roster)", color="#4c78a8")
    ax.barh(y_pos - height/2, top["bring_rate"], height, label="Bring Rate (% of roster apps brought)", color="#f58518")

    ax.set_yticks(y_pos)
    ax.set_yticklabels(top["mon"])
    ax.set_xlabel("Percentage")
    ax.set_title(f"Opponent Attendance (top {len(top)} by Team Rate)")
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.15), ncol=2)
    fig.tight_layout()
    ax.set_xlim(0, 105)