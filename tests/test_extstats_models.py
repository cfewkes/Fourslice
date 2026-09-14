"""Offline tests for the extstats model types (sentinel + periods).

Run with:  python -m pytest tests/test_extstats_models.py
"""

import pytest

from fourslice.extstats import (
    MOST_RECENT,
    SMOGON_ELOS,
    Month,
    MostRecent,
)


def test_most_recent_is_a_singleton():
    assert MostRecent() is MOST_RECENT
    assert repr(MOST_RECENT) == "MOST_RECENT"


def test_most_recent_is_not_a_period():
    # The whole point of the sentinel: it can never be confused with,
    # or used as, a concrete period id.
    assert not isinstance(MOST_RECENT, str)
    assert MOST_RECENT != "2026-07"
    assert MOST_RECENT != "M4"
    assert MOST_RECENT is not None


def test_month_defaults():
    month = Month(id="2026-07")
    assert month.source == "smogon"
    assert month.id == "2026-07"
    assert month.elos == ("0",)


def test_month_is_frozen():
    month = Month(id="2026-07")
    with pytest.raises(Exception) as exc:
        month.id = "2025-07"
    assert type(exc.value).__name__ == "FrozenInstanceError"





def test_published_buckets_constants():
    assert SMOGON_ELOS == ("0", "1500", "1630", "1760")