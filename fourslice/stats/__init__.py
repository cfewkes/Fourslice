"""
fourslice/stats/__init__.py

Each stat is a pair of functions plus some metadata, bundled as one Stat:

  data_fn(conn, regulation=None, result=None, my_side=None, team_id=None, mon=None) -> DataFrame (or dict of DataFrames -- see move_usage)
  render_fn(df, fig) -> None -- draws onto the given matplotlib
      Figure (not a pre-made Axes -- a stat adds its own subplot(s),
      since some need more than one, e.g. move_usage's 6-panel grid).
      All filtering/business logic belongs in data_fn; render_fn only
      draws.
  chart_type -- "bar" or "pie" so far. See DIFF_CAPABLE_CHART_TYPES.
  uses_mon_filter -- whether the shared Pokemon filter means anything
      for this stat. True for anything that narrows to one mon (e.g.
      turns_on_field); False for anything that always shows a whole
      team's roster regardless of it (move_usage) -- the GUI grays
      out (not hides) the Pokemon filter rather than clearing it when
      the selected chart doesn't use it, so a selection made under a
      different chart survives switching back. Defaults to True since
      that's the common case.
  mine_only -- whether the Side filter should only ever offer "My
      Pokemon", dropping "Opponent's Pokemon" and "Both" entirely.
      True for move_usage: its panels are titled with YOUR roster's
      species names, so "the opponent's Sneasler" or "both" don't
      read as sensibly-labeled options the way they do for a stat
      like turns_on_field. Defaults to False.

Not every stat needs every shared filter to do something -- an
unused kwarg on a given stat's data_fn is fine, the registry doesn't
enforce that every stat uses every filter.

To add a new one:

  1. Write both functions in their own file in this folder (copy
     turns_on_field.py -- bar -- or move_usage.py -- pie -- as a
     starting template, whichever shape fits).
  2. Import them below and add one line to REGISTRY.

That's the whole process. Nothing else in the app -- not the GUI,
not storage.py -- needs to change for a new stat to "show up"; the
GUI reads from REGISTRY rather than hardcoding a list of known stats.

Planned next (not yet written): co_occurrence, ko_credit -- same
pattern as turns_on_field, querying `events` with the same
regulation/result/my_side/team_id/mon filter shape.
"""

from typing import Callable, NamedTuple

import pandas as pd

from .turns_on_field import turns_on_field, render_turns_on_field
from .move_usage import move_usage, render_move_usage
from .co_occurrence import co_occurrence, render_co_occurrence
from .ko_credit import ko_credit, render_ko_credit
from .attendance import attendance, render_attendance


class Stat(NamedTuple):
    data_fn: Callable
    render_fn: Callable
    chart_type: str
    uses_mon_filter: bool = True
    mine_only: bool = False


REGISTRY = {
    "Turns on Field": Stat(turns_on_field, render_turns_on_field, "bar"),
    "Move Usage": Stat(move_usage, render_move_usage, "pie", uses_mon_filter=False, mine_only=True),
    "Co-occurrence": Stat(co_occurrence, render_co_occurrence, "bar"),
    "KO Credit": Stat(ko_credit, render_ko_credit, "bar"),
    "Attendance": Stat(attendance, render_attendance, "bar"),
}

# Which chart_types a win/loss "Difference" result filter has a
# sensible meaning for. Deliberately an allowlist rather than
# "everything that isn't pie": a future chart_type should have to
# opt in to diff on purpose, not get it by default just because it
# happens not to be a pie chart. The GUI (see gui/stats_tab.py's
# refresh_result_options) only ever offers "Difference" as a Result
# option when the selected chart's chart_type is in this set.
DIFF_CAPABLE_CHART_TYPES = {"bar"}