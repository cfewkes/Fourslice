"""
fourslice/extstats/files.py

Tiny filesystem helpers shared by the source snapshot modules.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from .models import SnapshotFile


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def copy_with_checksum(src: Path, dst: Path, rel_path: str) -> SnapshotFile:
    """Copy src to dst atomically and return a SnapshotFile describing the copy.

    rel_path is measured from the cache root, so it can be used directly
    as a SnapshotRecord.files entry; dst encodes the same location.

    Uses a temp file + rename to ensure atomicity: the destination file
    never exists in a partially-written state.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    data = src.read_bytes()
    tmp = dst.with_name(dst.name + ".tmp")
    tmp.write_bytes(data)
    # Atomic rename on POSIX; on Windows replace() is atomic for same-volume moves
    tmp.replace(dst)
    return SnapshotFile(rel_path=rel_path, sha256=sha256_hex(data), size=len(data))