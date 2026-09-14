# DAX Measures for Fourslice Power BI Dashboard

Every measure below reproduces one of the stats the Fourslice app
computes in Python/pandas. Copy-paste these into Power BI Desktop's
**Modeling → New Measure** (or into a dedicated Measures table).

All measures assume the five-table star schema from README.md is set
up with the documented relationships.

> **You probably don't need any of this.** Since the 13-file export,
> the **Export for Power BI** button also writes the same stats
> pre-computed as `stats_*.csv` tables (see README.md for the column
> reference). Import those CSVs and connect slicers directly — every
> measure in this document is then redundant. It is kept for power
> users who prefer to calculate KPIs in the model, or who want to
> build custom aggregations on top of the raw five tables.
> If you take the precomputed-tables path, jump straight to
> README.md's **Step 5: Build report pages**.

---

## Table of Contents

1. [Core KPIs](#core-kpis)
2. [Turns on Field](#turns-on-field)
3. [KO Credit](#ko-credit)
4. [Co-occurrence](#co-occurrence)
5. [Move Usage](#move-usage)
6. [Attendance (Opponent Bring Rate)](#attendance)
7. [Matchup Records](#matchup-records)
8. [Lead Records](#lead-records)
9. [Utility Measures](#utility-measures)

---

## Core KPIs

```dax
Total Games =
COUNTROWS(games)

Total Wins =
COUNTROWS(FILTER(games, games[result] = "W"))

Total Losses =
COUNTROWS(FILTER(games, games[result] = "L"))

Win Rate % =
DIVIDE([Total Wins], [Total Games], 0) * 100
```

---

## Turns on Field

These measures replicate `turns_on_field.py`.

```dax
Total Turns =
COUNTROWS(events)

Turns on Field =
DIVIDE([Total Turns], DISTINCTCOUNT(events[game_id]), 0)
```


---

## KO Credit

These measures replicate `ko_credit.py`.

```dax
Total KOs =
COUNTROWS(FILTER(events, events[event_type] = "faint" && NOT(ISBLANK(events[ko_credit_mon]))))

KOs per Game =
DIVIDE([Total KOs], DISTINCTCOUNT(events[game_id]), 0)
```

---

## Co-occurrence

These measures replicate `co_occurrence.py`.

```dax
Total Co-occurrence Turns =
COUNTROWS(FILTER(events, NOT(ISBLANK(events[p1a])) && NOT(ISBLANK(events[p1b]))))

Avg Co-occurrence Turns per Game =
DIVIDE([Total Co-occurrence Turns], DISTINCTCOUNT(events[game_id]), 0)
```

---

## Move Usage

Replicates `move_usage.py`.

```dax
Total Move Uses =
COUNTROWS(FILTER(events, events[event_type] = "move" && events[move_name] <> "Struggle"))

Pct of Moves =
DIVIDE([Total Move Uses], CALCULATE([Total Move Uses], ALL(events[move_name])), 0)
```

---

## Attendance

Replicates `attendance.py`.

```dax
Team Games =
DISTINCTCOUNT(opponent_teams[game_id])

Team Rate % =
DIVIDE([Team Games], DISTINCTCOUNT(games[game_id]), 0) * 100
```

---

## Matchup Records

Replicates `matchups.py`.

```dax
Matchup Wins =
COUNTROWS(FILTER(games, games[result] = "W"))

Matchup Losses =
COUNTROWS(FILTER(games, games[result] = "L"))

Matchup Win Rate % =
DIVIDE([Matchup Wins], [Total Games], 0) * 100
```

---

## Lead Records

Replicates `matchups.py`.

```dax
Lead Win Rate % =
DIVIDE([Matchup Wins], [Total Games], 0) * 100
```

---

## Utility Measures

```dax
// Win-minus-loss swing. The old *_diff.csv export tables were removed
// because a diff is trivially reproducible in DAX: each stat CSV has
// one row per (regulation, result, perspective), so on a visual that
// groups by mon/pair, SUM of the per-result averages IS the average,
// and the subtraction below is the win-minus-loss swing.

Turns on Field Swing =
VAR Wins =
    CALCULATE(SUM(stats_turns_on_field[avg_turns_per_game]),
              stats_turns_on_field[result] = "W")
VAR Losses =
    CALCULATE(SUM(stats_turns_on_field[avg_turns_per_game]),
              stats_turns_on_field[result] = "L")
RETURN Wins - Losses

KO Credit Swing =
VAR Wins =
    CALCULATE(SUM(stats_ko_credit[avg_kos_per_game]),
              stats_ko_credit[result] = "W")
VAR Losses =
    CALCULATE(SUM(stats_ko_credit[avg_kos_per_game]),
              stats_ko_credit[result] = "L")
RETURN Wins - Losses

Co-occurrence Swing =
VAR Wins =
    CALCULATE(SUM(stats_co_occurrence[avg_turns_per_game]),
              stats_co_occurrence[result] = "W")
VAR Losses =
    CALCULATE(SUM(stats_co_occurrence[avg_turns_per_game]),
              stats_co_occurrence[result] = "L")
RETURN Wins - Losses
```

