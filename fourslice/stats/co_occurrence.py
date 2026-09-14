"""
fourslice/stats/co_occurrence.py

Which two Pokemon get sent out together, and how often -- one row
per unordered pair, mirroring turns_on_field's shape closely enough
to reuse its diff-mode pattern almost verbatim.
"""

import pandas as pd


def co_occurrence(conn, regulation=None, result=None, my_side=None, team_id=None, mon=None) -> pd.DataFrame:
    """
    Returns one row per (pair, side) with total_turns, games_seen,
    and avg_turns_per_game -- same shape as turns_on_field, just
    grouped by an unordered pair of species instead of one mon.
    `pair` is always canonicalized alphabetically ("Pyroar +
    Whimsicott", never "Whimsicott + Pyroar"), so the same two mons
    always land in the same row regardless of which slot (a/b)
    either happened to be in.

    my_side / team_id / regulation / result: same meaning and same
    per-row resolution as turns_on_field -- see its docstring. When
    my_side is set, `side` drops out of the grouping, same reasoning
    as there: once every remaining pair is from the same
    perspective, splitting by literal p1/p2 slot doesn't mean
    anything anymore.

    mon: restricts to pairs that INCLUDE this Pokemon -- "who does
    Whimsicott get paired with most" rather than one specific pair.

    result="diff": win-average minus loss-average TURNS
    co-occurring per game (not games_seen -- see
    render_co_occurrence) -- "does this pairing last longer together
    when I'm winning". Same inner-join-drops-partial-pairs behavior
    as turns_on_field's diff mode: a pair only seen in wins (or only
    in losses) is dropped, not diffed against a fabricated 0.

    Singles games never have co-occurrence data (only 1 mon per side
    on field at a time), so they are filtered out by default. If
    only singles games match the filters, an empty DataFrame is
    returned so the renderer can show "Not applicable".
    """
    if result == "diff":
        return _co_occurrence_diff(conn, regulation=regulation, my_side=my_side, team_id=team_id, mon=mon)

    query = """
        SELECT e.game_id, e.turn, e.p1a, e.p1b, e.p2a, e.p2b, g.my_side AS game_my_side
        FROM events e
        JOIN games g ON e.game_id = g.game_id
        WHERE e.event_type = 'turnStart'
          AND COALESCE(g.battle_size, 'doubles') = 'doubles'
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

    sides = []
    for side, col_a, col_b in [("p1", "p1a", "p1b"), ("p2", "p2a", "p2b")]:
        side_df = turns_df[["game_id", "turn", "game_my_side", col_a, col_b]].dropna(subset=[col_a, col_b])
        side_df = side_df.rename(columns={col_a: "mon_a", col_b: "mon_b"})
        side_df["side"] = side
        sides.append(side_df)
    paired = pd.concat(sides, ignore_index=True)

    if mon:
        paired = paired[(paired["mon_a"] == mon) | (paired["mon_b"] == mon)]

    if my_side in ("mine", "opponent"):
        is_mine = paired["side"] == paired["game_my_side"]
        paired = paired[is_mine if my_side == "mine" else ~is_mine]

    paired = paired.copy()
    if paired.empty:
        paired["pair"] = pd.Series(dtype=str)
    else:
        paired["pair"] = paired.apply(lambda r: " + ".join(sorted([r["mon_a"], r["mon_b"]])), axis=1)

    group_cols = ["pair"] if my_side in ("mine", "opponent") else ["pair", "side"]
    summary = (
        paired.groupby(group_cols)
        .agg(total_turns=("turn", "count"), games_seen=("game_id", "nunique"))
        .reset_index()
    )
    summary["avg_turns_per_game"] = summary["total_turns"] / summary["games_seen"]
    return summary.sort_values("games_seen", ascending=False).reset_index(drop=True)


def _co_occurrence_diff(conn, regulation, my_side, team_id, mon) -> pd.DataFrame:
    """
    win-average minus loss-average TURNS co-occurring per game, per
    (pair[, side]) -- an INNER join of win-only and loss-only
    results, same reasoning as
    turns_on_field._turns_on_field_diff.
    """
    wins = co_occurrence(conn, regulation=regulation, result="W", my_side=my_side, team_id=team_id, mon=mon)
    losses = co_occurrence(conn, regulation=regulation, result="L", my_side=my_side, team_id=team_id, mon=mon)

    merge_cols = ["pair"] if my_side in ("mine", "opponent") else ["pair", "side"]
    merged = wins.merge(losses, on=merge_cols, suffixes=("_win", "_loss"))
    merged["diff"] = merged["avg_turns_per_game_win"] - merged["avg_turns_per_game_loss"]

    keep_cols = merge_cols + ["diff", "avg_turns_per_game_win", "avg_turns_per_game_loss"]
    return merged[keep_cols].sort_values("diff", key=lambda s: s.abs(), ascending=False).reset_index(drop=True)


def render_co_occurrence(df, fig, top_n=15):
    """
    Horizontal bar chart on a single subplot. Normal mode:
    games_seen for the top_n pairs -- "how often do I actually bring
    these two together" -- deliberately NOT avg_turns_per_game the
    way turns_on_field plots for individual mons; a pairing's average
    per-game duration is a noisier, less directly useful default
    here than how often the pairing happens at all. total_turns
    still comes back in the DataFrame if you want it, it's just not
    what's plotted.

    Diff mode (a "diff" column present -- see _co_occurrence_diff):
    same diverging-bar treatment as turns_on_field's diff mode, but
    over avg turns co-occurring per game rather than games_seen --
    that's the metric that's actually comparable across a different
    number of total wins vs losses without a separate normalization
    step this doesn't do; games_seen isn't.
    """
    ax = fig.add_subplot(111)
    if df.empty:
        # Co-occurrence is only meaningful in doubles; if only singles
        # games match the filters (or no games at all), show a clear
        # message instead of an empty chart.
        ax.text(0.5, 0.5, "Not applicable (singles format)", ha="center", va="center", transform=ax.transAxes)
        ax.set_xticks([])
        ax.set_yticks([])
        return

    if "diff" in df.columns:
        top = df.head(top_n).iloc[::-1]
        colors = ["#2ca02c" if v >= 0 else "#d62728" for v in top["diff"]]
        ax.barh(top["pair"], top["diff"], color=colors)
        ax.axvline(0, color="black", linewidth=0.8)
        ax.set_xlabel("Avg turns co-occurring per game: win minus loss")
        ax.set_title(f"Co-occurrence -- Win/Loss Difference (top {len(top)})")
        return

    top = df.head(top_n).iloc[::-1]
    ax.barh(top["pair"], top["games_seen"])
    ax.set_xlabel("Games brought out together")
    ax.set_title(f"Co-occurrence (top {len(top)})")