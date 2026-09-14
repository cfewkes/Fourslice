# Power BI Dashboard Setup Guide

This folder contains everything you need to build a Power BI dashboard
from your Fourslice data. The app's **Export for Power BI** button
(on the Statistics page) writes thirteen CSV files that Power BI Desktop
can import directly — no ODBC driver, no database connection string,
just files. Five of them are the raw game/event/team tables; the other
eight are the app's stats **pre-computed** so you can skip DAX
entirely.

---

## Prerequisites

- **Power BI Desktop** (free download from Microsoft):
  https://powerbi.microsoft.com/desktop/
- At least a few games imported into Fourslice (the more data, the
  more interesting the dashboard).

---

## Step 1: Export your data

1. Open Fourslice.
2. Go to the **Statistics** page.
3. Click **Export for Power BI** (the dark button with a download icon).
4. Choose a folder (e.g. `C:\FourSlice\PowerBI_Data`).
5. You'll get 13 CSV files — 5 raw tables + 8 pre-computed stat tables:

**Raw data tables** (the star-schema building blocks):

| File | Description | Key Columns |
|------|-------------|-------------|
| `games.csv` | One row per game | `game_id`, `result`, `regulation`, `team_id`, `battle_size`, `my_side` |
| `events.csv` | One row per turn/move/faint | `game_id`, `turn`, `event_type`, `mon`, `move_name`, `p1a`–`p2b`, `ko_credit_mon` |
| `teams.csv` | One row per auto-detected team | `team_id`, `nickname`, `roster_key` |
| `team_pokemon.csv` | Six rows per team (roster) | `team_id`, `slot_order`, `species`, `item`, `moves` |
| `opponent_teams.csv` | Opponent roster per game | `game_id`, `slot_order`, `species`, `item` |

**Pre-computed stat tables** (drop-in — no DAX needed). Every stat CSV
that can be sliced by regulation / result carries one row per concrete
value (`regulation` = each actual regulation you've played, `result` =
`W`/`L`). Regulation is never empty and every game is exactly W or L,
so adding up the per-result rows reproduces overall totals — a Power BI
slicer shows per-slice breakdowns and, with no selection, plain totals.
There are deliberately **no** `"(all)"` sentinel rows: they would
double-count when a slicer has no selection. `side` in
`stats_move_usage.csv` is `mine`/`opponent`; `perspective` in the
mine-vs-opponent files is `mine`/`opponent` too.

| File | Description | Key Columns |
|------|-------------|-------------|
| `stats_turns_on_field.csv` | Turns each mon is active per game | `perspective`, `regulation`, `result`, `mon`, `total_turns`, `games_seen`, `avg_turns_per_game` |
| `stats_co_occurrence.csv` | Which duos hit the field together | `perspective`, `regulation`, `result`, `pair`, `total_turns`, `games_seen`, `avg_turns_per_game` |
| `stats_ko_credit.csv` | KOs credited per attacker | `perspective`, `regulation`, `result`, `mon`, `ko_count`, `games_seen`, `avg_kos_per_game` |
| `stats_attendance.csv` | Opponent bring rate per mon | `regulation`, `result`, `mon`, `team_games`, `bring_games`, `total_games`, `team_rate`, `bring_rate`, `bring_rate_overall` |
| `stats_move_usage.csv` | Long-format move usage per team | `team_id`, `team_nickname`, `regulation`, `result`, `side`, `species`, `slot_order`, `move_name`, `use_count`, `pct_of_uses` |
| `stats_opponent_records.csv` | All-time W/L vs each opponent mon | `regulation`, `mon`, `games`, `wins`, `losses`, `pct` |
| `stats_leads.csv` | Lead records, singles + doubles | `regulation`, `battle_size`, `lead`, `lead_a`, `lead_b`, `games`, `wins`, `losses`, `pct` |
| `stats_brought_records.csv` | Per-team brought-record W/L | `team_id`, `team_nickname`, `regulation`, `mon`, `games`, `wins`, `losses`, `pct` |

---

## Step 2: Import into Power BI Desktop

1. Open Power BI Desktop → **Get Data** → **Text/CSV**.
2. Import each of the 13 CSV files. Power BI auto-detects headers
   and data types. Accept the defaults.
3. After importing all of them, click **Transform Data** to open Power
   Query Editor — you shouldn't need to change anything, but verify
   the column types look right (especially that `game_id` and
   `team_id` are Text and Whole Number respectively).
4. Click **Close & Apply**.

> **Tip on the stat tables:** you don't need to build any relationships
> on the `stats_*.csv` tables — they are already denormalized (they
> carry `team_id`, `team_nickname`, `perspective`, `regulation`,
> `result` directly). Just drop them onto table visuals and let Power BI
> read the columns natively — no measures, no Power Query steps.

---

## Step 3: Model the relationships

In the **Model** view (left sidebar → the diagram icon), create these
relationships by dragging columns between tables:

| From | To | Cardinality | Cross-filter |
|------|----|-------------|--------------|
| `games.game_id` | `events.game_id` | 1:Many | Both |
| `games.team_id` | `teams.team_id` | Many:1 | Single |
| `teams.team_id` | `team_pokemon.team_id` | 1:Many | Both |
| `games.game_id` | `opponent_teams.game_id` | 1:Many | Both |

This gives you a clean star schema:

```
                    teams
                      │
                      │ team_id
                      │
team_pokemon ─────── games ─────── opponent_teams
                      │
                      │ game_id
                      │
                    events
```

---

## Step 4: Create DAX measures (optional)

The pre-computed `stats_*.csv` tables already contain every stat the app
computes, so **no DAX measures are required** for a full dashboard —
just drop those tables onto visuals and use Top N instead of DAX. This
step is only needed if you prefer to compute KPIs in the model yourself,
or want custom aggregations the export doesn't ship.

See **[DAX_measures.md](DAX_measures.md)** for the complete set of DAX
formulas that replicate every stat the app computes. The key ones:

| Measure | What it does |
|---------|-------------|
| `Win Rate %` | Overall win percentage |
| `Avg Turns on Field` | Average turns a Pokemon is active per game |
| `KO Count` | Total KOs credited to an attacker |
| `Co-occurrence Games` | How often two Pokemon appear on field together |
| `Bring Rate %` | How often an opponent brings a Pokemon to battle |
| `Move Usage %` | What share of a Pokemon's moves each move accounts for |

---

## Step 5: Build report pages

Suggested layout (three pages):

### Page 1: Battle Overview
- **Card visuals**: Total Games, Win Rate %, Avg Game Length
- **Clustered bar chart**: Turns on Field by Pokemon (use a Top N filter)
- **Diverging bar chart**: Win/Loss Turns on Field difference (build
  it as a stacked `W`/`L` total or use the `Turns on Field Swing`
  measure from DAX_measures.md)
- **Slicers**: Regulation, Team, Result (W/L)

### Page 2: Matchups & Leads
- **Table visual**: Opponent Pokemon with Games, Wins, Losses, Win%
  (conditional formatting: green ≥50%, red <50%)
- **Bar chart**: Best/Worst matchups (top 6 by win%, min 4 games)
- **Table visual**: Most common lead pairs with records
- **Slicers**: Regulation, Team

### Page 3: Move Usage & Attendance
- **Donut charts**: One per Pokemon on the selected team (use a
  Pokemon slicer to cycle through them, or a small multiples visual)
- **Grouped bar chart**: Attendance — Team Rate vs Bring Rate per
  opponent Pokemon
- **Slicers**: Regulation, Team, Pokemon

---

## Step 6: Publish (optional, free)

1. Sign in to Power BI Service (free account at
   https://app.powerbi.com).
2. **Publish** from Power BI Desktop → choose a workspace.
3. Share the published report URL in your portfolio / resume / GitHub
   README.

> **Note:** The free tier lets you publish reports to "My Workspace"
> and share read-only links. You can't schedule data refreshes on
> the free tier — re-export and re-publish when your data changes.

---

## Re-exporting after new games

Whenever you import more replays into Fourslice, just click
**Export for Power BI** again (same folder) to overwrite all 13 CSVs.
In Power BI Desktop, click **Refresh** on the Home ribbon to pick
up the new data. If you've published, re-publish to update the
online version.

---

## Tips for your resume

When describing this on a resume or in an interview:

- **"Built a Power BI dashboard over a custom SQLite data pipeline"**
  — shows you can work with non-standard data sources, not just
  pre-built connectors.
- **"Designed a star-schema data model with fact and dimension tables"**
  — shows you understand data modeling, not just drag-and-drop.
- **"Wrote DAX measures for calculated KPIs (win rates, averages,
  conditional aggregations)"** — shows you can write business logic
  in DAX, not just use default aggregations.
- **"Published interactive reports with slicers and conditional
  formatting to Power BI Service"** — shows end-to-end delivery.
