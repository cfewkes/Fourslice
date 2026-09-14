"""
fourslice/stats/brought_record.py

Record of your own Pokemon brought to a game (Win/Loss record per bring).
Useful for VGC/BSS where you only bring a subset of your team.
"""

import numpy as np
import pandas as pd


def brought_records(conn, regulation=None, team_id=None) -> pd.DataFrame:
    """
    One row per Pokemon in the selected team's roster, with its record
    when actually brought to a game:

        mon, games, wins, losses, pct

    A mon is 'brought' if it appears on your side in at least one
    turnStart snapshot of that game.

    If team_id is None, returns an empty DataFrame as this stat is
    defined per-team roster.
    """
    if team_id is None:
        return pd.DataFrame(columns=["mon", "games", "wins", "losses", "pct"])

    # 1. Get the full roster for this team
    roster = [
        r[0] for r in conn.execute(
            "SELECT species FROM team_pokemon WHERE team_id = ? ORDER BY slot_order",
            (team_id,)
        ).fetchall()
    ]
    if not roster:
        return pd.DataFrame(columns=["mon", "games", "wins", "losses", "pct"])

    # 2. Get qualifying games
    query = """
        SELECT game_id, result, my_side
        FROM games
        WHERE team_id = ?
          AND result IN ('W', 'L')
          AND my_side IS NOT NULL
    """
    params = [team_id]
    if regulation:
        query += " AND regulation = ?"
        params.append(regulation)

    games_df = pd.read_sql(query, conn, params=params)
    if games_df.empty:
        # Return roster with 0-0 records
        return pd.DataFrame({
            "mon": roster,
            "games": 0,
            "wins": 0,
            "losses": 0,
            "pct": 0.0
        })

    # 3. Get all turnStart events for these games to see who was brought
    game_ids = games_df["game_id"].unique().tolist()
    # Chunking might be needed if there are thousands of games, but let's keep it simple for now
    placeholders = ",".join("?" for _ in game_ids)
    events_query = f"""
        SELECT game_id, p1a, p1b, p2a, p2b
        FROM events
        WHERE event_type = 'turnStart'
          AND game_id IN ({placeholders})
    """
    events_df = pd.read_sql(events_query, conn, params=game_ids)

    if events_df.empty:
        return pd.DataFrame({
            "mon": roster,
            "games": 0,
            "wins": 0,
            "losses": 0,
            "pct": 0.0
        })

    # 4. Resolve brought status per (game, mon)
    # Merge games and events to know my_side for each event
    df = events_df.merge(games_df, on="game_id")

    # Melt slots
    melted = df.melt(
        id_vars=["game_id", "result", "my_side"],
        value_vars=["p1a", "p1b", "p2a", "p2b"],
        var_name="slot",
        value_name="mon"
    ).dropna(subset=["mon"])
    melted = melted[melted["mon"] != ""]

    # Keep only my side's mons
    melted["side"] = melted["slot"].str[:2]
    is_mine = melted["side"] == melted["my_side"]
    brought = melted[is_mine].copy()

    # Distinct per (game, mon)
    brought = brought.drop_duplicates(subset=["game_id", "mon"])

    # 5. Aggregate records
    summary = (
        brought.groupby("mon", as_index=False)
        .agg(
            games=("game_id", "nunique"),
            wins=("result", lambda s: (s == "W").sum()),
        )
    )
    summary["losses"] = summary["games"] - summary["wins"]
    summary["pct"] = np.where(
        summary["games"] > 0,
        100.0 * summary["wins"] / summary["games"],
        0.0
    )

    # 6. Ensure all roster mons are present (filling 0s for those never brought)
    roster_df = pd.DataFrame({"mon": roster})
    out = roster_df.merge(summary, on="mon", how="left").fillna(0)
    out["games"] = out["games"].astype(int)
    out["wins"] = out["wins"].astype(int)
    out["losses"] = out["losses"].astype(int)

    return out
