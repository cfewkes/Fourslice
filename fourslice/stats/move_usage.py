"""
fourslice/stats/move_usage.py

Move usage breakdown for a whole team at once: one pie chart per
roster slot (6 total), each showing what share of that Pokemon's
actual move usage went to each move. Struggle is always excluded
(auto-triggered when nothing else is legal, not a real choice), and
a move that was never clicked simply never appears -- this only ever
counts real usage, never a mon's theoretical full moveset.

Requires a team to be selected (team_id) -- "all 6" only means
something in the context of a specific roster, so there's no
sensible unscoped version of this chart the way turns_on_field has
one. The Pokemon filter is irrelevant here (see
stats/__init__.py's uses_mon_filter) and ignored if set.
"""

import pandas as pd

NA_PLACEHOLDER = pd.DataFrame([{"move_name": "N/A", "use_count": 1, "pct_of_uses": 100.0}])


def move_usage(conn, regulation=None, result=None, my_side=None, team_id=None, mon=None) -> dict:
    """
    Returns {species: DataFrame}, one entry per roster slot in
    team-preview order (team_pokemon.slot_order), each DataFrame
    shaped like before (move_name, use_count, pct_of_uses -- or the
    single-row N/A placeholder for a mon with zero qualifying uses).

    Returns {} (empty dict) if team_id isn't set -- see
    render_move_usage, which draws a "select a team" prompt for that
    case rather than a 6-panel grid with nothing in it.

    Every query is scoped to games.team_id = team_id, not just
    events.mon = <species> -- without that, a mon that's ALSO on the
    opponent's team that game (same species, different Pokemon), or
    on a DIFFERENT one of your own teams, would silently blend into
    the count. `my_side` still narrows further within that: "mine"
    or "opponent" isolates which side's usage of that species counts,
    scoped to games where you specifically brought THIS team -- e.g.
    "opponent" answers "what does the other guy tend to do with a
    Sneasler when I bring this team", not "...across every game I've
    ever played".

    `mon` is accepted (every stat's data_fn gets called with the same
    kwargs -- see stats/__init__.py) but ignored: this always shows
    the whole roster regardless of it.

    result="diff" doesn't have a pie-chart meaning -- the GUI never
    actually offers "Difference" while a pie-chart stat is selected
    (see stats/__init__.py's DIFF_CAPABLE_CHART_TYPES), so this is
    purely a defensive fallback if it's ever called directly with it
    anyway: treated the same as no result filter.
    """
    if not team_id:
        return {}

    roster = [
        r[0] for r in conn.execute(
            "SELECT species FROM team_pokemon WHERE team_id = ? ORDER BY slot_order", (team_id,)
        ).fetchall()
    ]

    query = """
        SELECT e.mon, e.move_name, COUNT(*) AS use_count
        FROM events e
        JOIN games g ON e.game_id = g.game_id
        WHERE e.event_type = 'move' AND e.move_name != 'Struggle' AND g.team_id = ?
    """
    params = [team_id]
    if regulation:
        query += " AND g.regulation = ?"
        params.append(regulation)
    if result and result != "diff":
        query += " AND g.result = ?"
        params.append(result)
    if my_side == "mine":
        query += " AND e.side = g.my_side"
    elif my_side == "opponent":
        query += " AND e.side != g.my_side"

    query += " GROUP BY e.mon, e.move_name"

    usage_df = pd.read_sql(query, conn, params=params)

    usage_by_species = {}
    for species in roster:
        species_df = usage_df[usage_df["mon"] == species].drop(columns=["mon"])
        if species_df.empty:
            usage_by_species[species] = NA_PLACEHOLDER.copy()
            continue
        species_df = species_df.copy()
        species_df["pct_of_uses"] = species_df["use_count"] / species_df["use_count"].sum() * 100
        usage_by_species[species] = species_df.sort_values("use_count", ascending=False).reset_index(drop=True)

    return usage_by_species


def render_move_usage(usage_by_species: dict, fig):
    """
    2x3 grid, one subplot per roster slot, each titled with its
    species name, in the same team-preview order move_usage returns
    them in (so this always reads left-to-right/top-to-bottom in
    that order, not alphabetically).

    An empty dict (no team selected -- see move_usage) draws a
    single "select a team" prompt across the whole figure instead --
    there's nothing meaningful to show 6 panels of yet.
    """
    if not usage_by_species:
        ax = fig.add_subplot(111)
        ax.text(0.5, 0.5, "Select a team to see its move usage", ha="center", va="center", transform=ax.transAxes)
        ax.set_xticks([])
        ax.set_yticks([])
        return

    for i, (species, df) in enumerate(usage_by_species.items()):
        ax = fig.add_subplot(2, 3, i + 1)
        if df.empty or df.iloc[0]["move_name"] == "N/A":
            ax.pie([100], labels=["N/A"], colors=["lightgray"])
        else:
            ax.pie(df["use_count"], labels=df["move_name"], autopct="%1.0f%%")
        ax.set_title(species, fontsize=10)