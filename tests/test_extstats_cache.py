"""Offline tests for SnapshotStore: round-trips, retention, pruning, safety.

Run with:  python -m pytest tests/test_extstats_cache.py
"""

import json

import pytest

from fourslice.extstats import CacheCorruptError, SnapshotFile, SnapshotRecord, SnapshotStore


def _record(
    source="smogon",
    period="2026-07",
    parsed=(),
    fetched="2026-08-01T00:00:00+00:00",
):
    return SnapshotRecord(
        source=source,
        period_id=period,
        fetched_at=fetched,
        files=(
            SnapshotFile(
                rel_path=f"{source}/{period}/gen7ou-0.txt",
                sha256="a" * 64,
                size=10,
            ),
        ),
        parsed=parsed,
    )


def test_empty_store_returns_nothing(tmp_path):
    store = SnapshotStore(tmp_path)
    assert store.latest("smogon") is None
    assert store.all() == {}


def test_put_and_reload_roundtrip(tmp_path):
    store = SnapshotStore(tmp_path)
    store.put(_record())

    reloaded = SnapshotStore(tmp_path)
    rec = reloaded.latest("smogon")
    assert rec is not None
    assert rec.period_id == "2026-07"
    assert rec.files[0].rel_path == "smogon/2026-07/gen7ou-0.txt"
    assert rec.files[0].sha256 == "a" * 64
    assert reloaded.all() == {"smogon": rec}


def test_state_file_shape(tmp_path):
    store = SnapshotStore(tmp_path)
    store.put(_record())
    payload = json.loads(store.state_path("smogon").read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert payload["record"]["period_id"] == "2026-07"


def test_put_same_period_overwrites(tmp_path):
    store = SnapshotStore(tmp_path)
    store.put(_record())
    later = _record(fetched="2026-08-02T00:00:00+00:00")

    replaced = store.put(later)

    assert replaced is None  # same period: nothing pruned
    assert store.latest("smogon").fetched_at == later.fetched_at


def test_put_new_period_prunes_old_files(tmp_path):
    store = SnapshotStore(tmp_path)
    store.put(_record())
    # simulate the next month's snapshot files already having been copied in
    new_dir = store.period_dir("smogon", "2026-08")
    new_dir.mkdir(parents=True)
    (new_dir / "gen7ou-0.txt").write_text("new", encoding="utf-8")
    old_parsed = store.parsed_dir("smogon", "2026-07")
    old_parsed.mkdir(parents=True)
    (old_parsed / "gen7ou.json").write_text("{}", encoding="utf-8")
    new_parsed = store.parsed_dir("smogon", "2026-08")
    new_parsed.mkdir(parents=True)
    (new_parsed / "gen7ou.json").write_text("{}", encoding="utf-8")

    replaced = store.put(
        _record(
            period="2026-08",
            parsed=("smogon/2026-08/parsed/gen7ou.json",),
        )
    )

    assert replaced is not None and replaced.period_id == "2026-07"
    assert store.latest("smogon").period_id == "2026-08"
    # the old period directory (raw copies AND parsed) is gone, new one intact
    assert not store.period_dir("smogon", "2026-07").exists()
    assert (store.period_dir("smogon", "2026-08") / "gen7ou-0.txt").exists()
    assert (store.parsed_dir("smogon", "2026-08") / "gen7ou.json").exists()
    assert store.all() == {"smogon": store.latest("smogon")}


def test_pruning_only_touches_its_own_period(tmp_path):
    store = SnapshotStore(tmp_path)
    store.put(_record())
    store.put(_record(source="other", period="v1"))

    replaced = store.put(_record(period="2026-08"))

    assert replaced.period_id == "2026-07"
    assert store.latest("other").period_id == "v1"  # untouched
    assert sorted(store.all()) == ["other", "smogon"]


def test_put_with_prune_previous_false_keeps_old_period(tmp_path):
    store = SnapshotStore(tmp_path)
    store.put(_record())
    old_dir = store.period_dir("smogon", "2026-07")
    (old_dir / "gen7ou-0.txt").parent.mkdir(parents=True)
    (old_dir / "gen7ou-0.txt").write_text("old", encoding="utf-8")

    store.put(_record(period="2026-08"), prune_previous=False)

    # the old period survives on disk (retention is a per-write decision)
    assert (old_dir / "gen7ou-0.txt").read_text(encoding="utf-8") == "old"
    assert store.latest("smogon").period_id == "2026-08"


def test_put_rejects_paths_outside_the_period_dir(tmp_path):
    store = SnapshotStore(tmp_path)

    def record_with(rel_path, period="2026-07"):
        return SnapshotRecord(
            source="smogon",
            period_id=period,
            fetched_at="t",
            files=(SnapshotFile(rel_path, "f" * 64, 1),),
        )

    with pytest.raises(ValueError):
        store.put(record_with("smogon/2026-08/gen7ou-0.txt"))  # other period
    with pytest.raises(ValueError):
        store.put(record_with("../escape.txt"))  # traversal
    with pytest.raises(ValueError):
        store.put(record_with("C:/evil.txt"))  # drive prefix
    with pytest.raises(ValueError):
        store.put(record_with("smogon\\2026-07\\evil.txt"))  # backslash smuggling


def test_put_rejects_invalid_period_id(tmp_path):
    store = SnapshotStore(tmp_path)
    with pytest.raises(ValueError):
        store.put(_record(period="2026 07"))


def test_corrupt_state_raises(tmp_path):
    store = SnapshotStore(tmp_path)
    state = store.state_path("smogon")
    state.parent.mkdir(parents=True)

    state.write_text("{not json", encoding="utf-8")
    with pytest.raises(CacheCorruptError):
        store.latest("smogon")


def test_state_with_unsupported_version_raises(tmp_path):
    store = SnapshotStore(tmp_path)
    state = store.state_path("smogon")
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"version": 99, "record": None}), encoding="utf-8")
    with pytest.raises(CacheCorruptError):
        store.latest("smogon")


def test_remove_period_clears_state(tmp_path):
    store = SnapshotStore(tmp_path)
    store.put(_record())
    store.remove("smogon", period_id="2026-07")
    assert store.latest("smogon") is None


def test_remove_source_nukes_its_directory(tmp_path):
    store = SnapshotStore(tmp_path)
    store.put(_record())
    store.remove("smogon")
    assert store.latest("smogon") is None
    assert not store.source_dir("smogon").exists()