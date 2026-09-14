"""
fourslice/stats/ko_credit.py

Which Pokemon get credited with securing a KO, and how often.

Attribution happens in parser.parse_game_events, not here: a faint
is credited to a specific attacker only when the fatal |-damage|
line has no [from] tag AND its slot matches the most recently logged
move's target(s) (including spread-move targets). Anything else --
residual damage, recoil, item/ability procs, or a fatal hit whose
real cause isn't visible on the |move| line itself (charge/delayed
moves like Solar Beam, Fly, or Future Sight reveal their real target
on a separate |-anim| line this doesn't read) -- is left
unattributed rather than guessed at. Unattributed faints simply
don't count toward anyone's total here; there's no "Unknown" bucket
in the output, they're just absent from it.
"""

import pandas as pd


def ko_credit(conn, regulation=None, result=None, my_side=None, team_id=None, mon=None) -> pd.DataFrame:
    """
    Returns one row per (mon[, side]) with ko_count, games_seen (the
    number of distinct games this mon secured at least one credited
    KO in), and avg_kos_per_game (ko_count / games_seen -- same
    "restrict the denominator to games where this entity actually
    mattered" convention turns_on_field uses for avg_turns_per_game).

    my_side / team_id / regulation / result: same meaning as
    turns_on_field. `side` here is the CREDITED ATTACKER's side, not
    the victim's -- so my_side="mine" means "KOs my Pokemon secured",
    not anything about who got KO'd.

    mon: restricts to KOs credited to this specific Pokemon.

    result="diff": win-average minus loss-average KOs credited per
    game, per (mon[, side]) -- same inner-join-drops-partial-pairs
    behavior as turns_on_field's diff mode.
    """
    if result == "diff":
        return _ko_credit_diff(conn, regulation=regulation, my_side=my_side, team_id=team_id, mon=mon)

    query = """
        SELECT e.game_id, e.ko_credit_mon AS mon, e.ko_credit_side AS side, g.my_side AS game_my_side
        FROM events e
        JOIN games g ON e.game_id = g.game_id
        WHERE e.event_type = 'faint' AND e.ko_credit_mon IS NOT NULL
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
    if mon:
        query += " AND e.ko_credit_mon = ?"
        params.append(mon)

    credit_df = pd.read_sql(query, conn, params=params)

    if my_side in ("mine", "opponent"):
        is_mine = credit_df["side"] == credit_df["game_my_side"]
        credit_df = credit_df[is_mine if my_side == "mine" else ~is_mine]

    group_cols = ["mon"] if my_side in ("mine", "opponent") else ["mon", "side"]
    summary = (
        credit_df.groupby(group_cols)
        .agg(ko_count=("game_id", "count"), games_seen=("game_id", "nunique"))
        .reset_index()
    )
    summary["avg_kos_per_game"] = summary["ko_count"] / summary["games_seen"]
    return summary.sort_values("ko_count", ascending=False).reset_index(drop=True)


def _ko_credit_diff(conn, regulation, my_side, team_id, mon) -> pd.DataFrame:
    """
    win-average minus loss-average KOs credited per game, per
    (mon[, side]) -- an INNER join of win-only and loss-only
    results, same reasoning as turns_on_field._turns_on_field_diff.
    """
    wins = ko_credit(conn, regulation=regulation, result="W", my_side=my_side, team_id=team_id, mon=mon)
    losses = ko_credit(conn, regulation=regulation, result="L", my_side=my_side, team_id=team_id, mon=mon)

    merge_cols = ["mon"] if my_side in ("mine", "opponent") else ["mon", "side"]
    merged = wins.merge(losses, on=merge_cols, suffixes=("_win", "_loss"))
    merged["diff"] = merged["avg_kos_per_game_win"] - merged["avg_kos_per_game_loss"]

    keep_cols = merge_cols + ["diff", "avg_kos_per_game_win", "avg_kos_per_game_loss"]
    return merged[keep_cols].sort_values("diff", key=lambda s: s.abs(), ascending=False).reset_index(drop=True)


def render_ko_credit(df, fig, top_n=15):
    """
    Horizontal bar chart. Normal mode: ko_count (a raw total, not an
    average) for the top_n mons -- unlike turns_on_field, a KO is a
    relatively rare, meaningful event rather than something that
    accumulates just from being sent out a lot, so "total KOs
    secured" is directly useful on its own without averaging it down
    to a per-game rate. Diff mode still needs the rate
    (avg_kos_per_game) to be comparable across a different number of
    total wins vs losses, same reasoning as turns_on_field's diff.
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
        ax.set_xlabel("Avg KOs credited per game: win minus loss")
        ax.set_title(f"KO Credit -- Win/Loss Difference (top {len(top)})")
        return

    top = df.head(top_n).iloc[::-1]
    ax.barh(top["mon"], top["ko_count"])
    ax.set_xlabel("KOs credited")
    ax.set_title(f"KO Credit (top {len(top)})")