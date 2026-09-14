"""
fourslice/extstats/cache.py

SnapshotStore -- the on-disk cache of external-stat snapshots.

Layout under the cache root (default <repo>/data/stats-cache):

    <source>/state.json           provenance of the current snapshot
    <source>/<period_id>/         raw copies of the snapshotted files
    <source>/<period_id>/parsed/  parsed JSON documents

state.json holds exactly one record per source -- the most recent period.
Writing a record for a DIFFERENT period prunes the previous period's
directory first, so the cache cannot grow unbounded. Keeping more than
one period (explicit user-triggered / historical requests) is a later
feature; retention is already a per-write decision via the
prune_previous flag, so nothing about the store needs to change for it.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from .models import ExtStatsError, SnapshotFile, SnapshotRecord

_STATE_VERSION = 1
_STATE_NAME = "state.json"
_COMPONENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class CacheCorruptError(ExtStatsError):
    """A state.json could not be read or does not match the schema."""


@contextmanager
def _file_lock(path: Path, timeout: float = 30.0, poll_interval: float = 0.1):
    """
    Cross-platform advisory file lock using a lock file.
    On Windows: uses msvcrt.locking on the file handle.
    On Unix: uses fcntl.flock.
    """
    lock_path = path.with_name(path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    # Create the lock file
    lock_file = lock_path.open("w")

    start_time = time.monotonic()
    acquired = False

    try:
        if os.name == "nt":
            # Windows: use msvcrt.locking
            import msvcrt
            while not acquired:
                try:
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                    acquired = True
                except OSError:
                    if time.monotonic() - start_time >= timeout:
                        raise TimeoutError(f"Could not acquire lock on {lock_path} within {timeout}s")
                    time.sleep(poll_interval)
        else:
            # Unix: use fcntl.flock
            import fcntl
            while not acquired:
                try:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except OSError:
                    if time.monotonic() - start_time >= timeout:
                        raise TimeoutError(f"Could not acquire lock on {lock_path} within {timeout}s")
                    time.sleep(poll_interval)

        yield
    finally:
        if acquired:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()
        # Best effort cleanup of lock file
        try:
            lock_path.unlink()
        except OSError:
            pass


def _require_component(name: str, what: str) -> None:
    """Source/period components must be safe, non-empty path segments."""
    if not isinstance(name, str) or not _COMPONENT_RE.match(name):
        raise ValueError(f"invalid {what} {name!r}")


def _require_in_period(source: str, period_id: str, rel_path: str) -> None:
    """Every recorded path must live inside <root>/<source>/<period_id>/.

    This is the safety invariant that makes pruning (delete whole period
    directory) safe without touching anything else in the cache.
    """
    if not isinstance(rel_path, str) or "\x00" in rel_path:
        raise ValueError(f"invalid snapshot path {rel_path!r}")
    if re.match(r"^[A-Za-z]:", rel_path) or "\\" in rel_path:
        raise ValueError(f"invalid snapshot path {rel_path!r}")
    path = PurePosixPath(rel_path)
    if path.is_absolute():
        raise ValueError(f"invalid snapshot path {rel_path!r}")
    parts = path.parts
    if len(parts) < 3 or parts[0] != source or parts[1] != period_id:
        raise ValueError(
            f"snapshot path {rel_path!r} escapes its period directory "
            f"{source}/{period_id}"
        )
    if any(not part or part in (".", "..") for part in parts):
        raise ValueError(f"invalid snapshot path {rel_path!r}")


def _record_to_dict(record: SnapshotRecord) -> dict:
    return {
        "source": record.source,
        "period_id": record.period_id,
        "fetched_at": record.fetched_at,
        "files": [
            {"rel_path": f.rel_path, "sha256": f.sha256, "size": f.size}
            for f in record.files
        ],
        "parsed": list(record.parsed),
    }


def _record_from_dict(data) -> SnapshotRecord:
    if not isinstance(data, dict):
        raise CacheCorruptError("snapshot record is not an object")
    try:
        files = tuple(
            SnapshotFile(f["rel_path"], f["sha256"], f["size"]) for f in data["files"]
        )
        parsed = tuple(data["parsed"])
        return SnapshotRecord(
            data["source"], data["period_id"], data["fetched_at"], files, parsed
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise CacheCorruptError(f"malformed snapshot record: {exc}") from exc


class SnapshotStore:
    """JSON-backed cache of the most-recent snapshot for each source."""

    def __init__(self, root: Path | str):
        self.root = Path(root)

    # ---- path helpers ---------------------------------------------------------
    def source_dir(self, source: str) -> Path:
        _require_component(source, "source")
        return self.root / source

    def period_dir(self, source: str, period_id: str) -> Path:
        _require_component(source, "source")
        _require_component(period_id, "period id")
        return self.source_dir(source) / period_id

    def parsed_dir(self, source: str, period_id: str) -> Path:
        return self.period_dir(source, period_id) / "parsed"

    def state_path(self, source: str) -> Path:
        return self.source_dir(source) / _STATE_NAME

    # ---- reads ----------------------------------------------------------------
    def latest(self, source: str) -> SnapshotRecord | None:
        """The recorded snapshot for source, or None when none is cached."""
        _require_component(source, "source")
        path = self.state_path(source)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise CacheCorruptError(f"{path}: {exc}") from exc
        if not isinstance(payload, dict) or payload.get("version") != _STATE_VERSION:
            version = payload.get("version") if isinstance(payload, dict) else "?"
            raise CacheCorruptError(f"{path}: unsupported or malformed state (version {version})")
        record = _record_from_dict(payload["record"]) if payload.get("record") else None
        return record

    def all(self) -> dict[str, SnapshotRecord]:
        """Every cached snapshot, keyed by source."""
        result: dict[str, SnapshotRecord] = {}
        if not self.root.exists():
            return result
        for source_dir in sorted(self.root.iterdir()):
            if source_dir.is_dir():
                record = self.latest(source_dir.name)
                if record is not None:
                    result[record.source] = record
        return result

    # ---- writes ---------------------------------------------------------------
    def put(
        self, record: SnapshotRecord, *, prune_previous: bool = True
    ) -> SnapshotRecord | None:
        """Persist a snapshot record; replaces an existing one for the same
        period. When a different period is already recorded and
        prune_previous is set (the default), that period's whole directory
        is deleted first -- this is how the cache stays "most recent only".

        Returns the replaced SnapshotRecord when one was pruned, else None.

        This method uses an advisory file lock on the state.json to prevent
        race conditions when multiple threads/processes concurrently call put()
        for the same source.
        """
        _require_component(record.source, "source")
        _require_component(record.period_id, "period id")
        for file in record.files:
            _require_in_period(record.source, record.period_id, file.rel_path)
        for rel in record.parsed:
            _require_in_period(record.source, record.period_id, rel)

        state_path = self.state_path(record.source)

        # Use advisory file lock to prevent concurrent writes for the same source
        with _file_lock(state_path):
            replaced = None
            previous = self.latest(record.source)
            if previous is not None and previous.period_id != record.period_id:
                replaced = previous
                if prune_previous:
                    shutil.rmtree(
                        self.period_dir(record.source, previous.period_id), ignore_errors=True
                    )
            self._write_state(record)
            return replaced

    def remove(self, source: str, *, period_id: str | None = None) -> None:
        """Delete a source entirely, or just one recorded period of it."""
        _require_component(source, "source")
        if period_id is None:
            shutil.rmtree(self.source_dir(source), ignore_errors=True)
            return
        _require_component(period_id, "period id")
        shutil.rmtree(self.period_dir(source, period_id), ignore_errors=True)
        previous = self.latest(source)
        if previous is not None and previous.period_id == period_id:
            self._write_empty_state(source)

    def _write_empty_state(self, source: str) -> None:
        self._write_payload(source, {"version": _STATE_VERSION, "record": None})

    def _write_state(self, record: SnapshotRecord) -> None:
        self._write_payload(record.source, {"version": _STATE_VERSION, "record": _record_to_dict(record)})

    def _write_payload(self, source: str, payload: dict) -> None:
        path = self.state_path(source)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(path)