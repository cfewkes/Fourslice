"""
fourslice/stats/turns_on_field.py

Average and total turns each Pokemon spends active, per side,
optionally filtered by regulation, win/loss result (or the
win-vs-loss difference), which side ("mine" vs "opponent's"), team,
and/or a single Pokemon.
"""

import pandas as pd


def turns_on_field(conn, regulation=None, result=None, my_side=None, team_id=None, mon=None) -> pd.DataFrame:
    """
    Returns one row per (mon, side) with total_turns, games_seen, and
    avg_turns_per_game. `side` is 'p1' or 'p2' and counts both sides
    by default.

    my_side: 'mine', 'opponent', or None (default, both). A mon on
    'p1' is mine in some games and the opponent's in others, so this
    is resolved per-row against that game's games.my_side rather than
    filterable after the fact. When set, `side` is dropped from the
    grouping too -- once every remaining row is the same perspective,
    splitting the same mon across p1/p2 doesn't mean anything anymore.

    team_id: restricts to games played with that team (games.team_id)
    -- see storage.resolve_team_id / storage.get_teams_by_recency.

    mon: restricts to a single Pokemon's rows. Mostly here because
    it's a shared filter with move_usage (which requires it); useful
    on its own too if you just want one mon's turn counts without
    scanning the full chart for it.

    result="diff": returns win-average minus loss-average per row
    instead of a single avg_turns_per_game -- see
    _turns_on_field_diff below. The GUI only ever offers this while
    chart_type == "bar" is selected (see stats/__init__.py's
    DIFF_CAPABLE_CHART_TYPES); calling it directly with any other
    result value is up to the caller, this doesn't enforce that.
    """
    if result == "diff":
        return _turns_on_field_diff(conn, regulation=regulation, my_side=my_side, team_id=team_id, mon=mon)

    query = """
        SELECT e.game_id, e.turn, e.p1a, e.p1b, e.p2a, e.p2b, g.my_side AS game_my_side
        FROM events e
        JOIN games g ON e.game_id = g.game_id
        WHERE e.event_type = 'turnStart'
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

    turns_df = pd.read_sql(query, conn, params=params)

    melted = turns_df.melt(
        id_vars=["game_id", "turn", "game_my_side"], value_vars=["p1a", "p1b", "p2a", "p2b"],
        var_name="slot", value_name="mon",
    ).dropna(subset=["mon"])
    melted["side"] = melted["slot"].str[:2]

    if mon:
        melted = melted[melted["mon"] == mon]

    if my_side in ("mine", "opponent"):
        is_mine = melted["side"] == melted["game_my_side"]
        melted = melted[is_mine if my_side == "mine" else ~is_mine]

    group_cols = ["mon"] if my_side in ("mine", "opponent") else ["mon", "side"]
    summary = (
        melted.groupby(group_cols)
        .agg(total_turns=("turn", "count"), games_seen=("game_id", "nunique"))
        .reset_index()
    )
    summary["avg_turns_per_game"] = summary["total_turns"] / summary["games_seen"]
    return summary.sort_values("total_turns", ascending=False).reset_index(drop=True)


def _turns_on_field_diff(conn, regulation, my_side, team_id, mon) -> pd.DataFrame:
    """
    win-average minus loss-average, per (mon[, side]) -- an INNER
    join of the win-only and loss-only results, so a mon that's only
    ever shown up in a win (or only ever in a loss) has nothing to
    diff against and gets dropped, rather than diffed against a
    fabricated 0. A fabricated 0 would read as "does badly in losses"
    when the truth is just "no losses on record for this mon yet" --
    silently wrong is worse than silently incomplete here.
    """
    wins = turns_on_field(conn, regulation=regulation, result="W", my_side=my_side, team_id=team_id, mon=mon)
    losses = turns_on_field(conn, regulation=regulation, result="L", my_side=my_side, team_id=team_id, mon=mon)

    merge_cols = ["mon"] if my_side in ("mine", "opponent") else ["mon", "side"]
    merged = wins.merge(losses, on=merge_cols, suffixes=("_win", "_loss"))
    merged["diff"] = merged["avg_turns_per_game_win"] - merged["avg_turns_per_game_loss"]

    keep_cols = merge_cols + ["diff", "avg_turns_per_game_win", "avg_turns_per_game_loss"]
    return merged[keep_cols].sort_values("diff", key=lambda s: s.abs(), ascending=False).reset_index(drop=True)


def render_turns_on_field(df, fig, top_n=15):
    """
    Horizontal bar chart on a single subplot filling the whole
    figure. Normal mode: avg_turns_per_game for the top_n mons by
    total_turns (a mon seen once that happened to survive long isn't
    "top" -- total_turns is the frequency-weighted pick; what's
    actually plotted is avg_turns_per_game).

    Diff mode (a "diff" column present -- see _turns_on_field_diff
    above): a diverging chart instead, green for "more turns in
    wins", red for "more turns in losses", top_n by the SIZE of the
    swing in either direction -- a mon that's dramatically worse in
    losses is just as interesting as one that's dramatically better
    in wins, so this isn't just "the biggest positive bars".

    Takes the whole Figure, not a pre-made Axes, to match every
    render_fn's shared signature (see stats/__init__.py) -- even
    though this one only ever needs a single subplot. move_usage's
    6-panel grid is what actually needs figure-level control.
    """
    ax = fig.add_subplot(111)
    if df.empty:
        ax.text(0.5, 0.5, "No data for this filter", ha="center", va="center", transform=ax.transAxes)
        ax.set_xticks([])
        ax.set_yticks([])
        return

    if "diff" in df.columns:
        top = df.head(top_n).iloc[::-1]
        colors = ["#2ca02c" if v >= 0 else "#d62728" for v in top["diff"]]
        ax.barh(top["mon"], top["diff"], color=colors)
        ax.axvline(0, color="black", linewidth=0.8)
        ax.set_xlabel("Avg turns on field: win minus loss")
        ax.set_title(f"Turns on Field -- Win/Loss Difference (top {len(top)})")
        return

    top = df.head(top_n).iloc[::-1]  # reversed so the biggest bar lands on top, not the bottom
    ax.barh(top["mon"], top["avg_turns_per_game"])
    ax.set_xlabel("Avg turns on field per game")
    ax.set_title(f"Turns on Field (top {len(top)})")