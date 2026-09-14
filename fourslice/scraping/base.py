"""
fourslice/scraping/base.py

Shared machinery for Fourslice's external-stat scrapers. Both the
Smogon crawler and the Champions crawler emit the SAME on-disk shape,
which is the whole point of this module:

    <out_dir>/<source>/
        manifest.json   # one record per fetched resource + crawl totals
        raw/...         # the exact bytes/text the site served

manifest.json carries a fixed, source-agnostic record schema:

    {
        "source": "smogon" | "champions",
        "url":    "https://...",        # the exact URL that was fetched
        "kind":   "index" | "data" | "media",
        "section": "...",               # human bucket (e.g. "2025-07/chaos", "battle_data/M4/.../Doubles")
        "rel_path": "raw/...",          # mirror location, absent on failure
        "content_type": "...",          # as served
        "size": 12345,                  # bytes on disk
        "sha256": "...",                # content checksum
        "fetched_at": "...",            # ISO-8601 UTC
        "error": null                   # set to a message when a fetch failed
    }

Nothing in this schema is source-specific, so a later chunk can read
either source's manifest with the same code. Every resource -- success
OR failure -- gets a record, so a manifest is a faithful log of what
was tried, not just what worked.
"""

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

import requests

log = logging.getLogger(__name__)

DEFAULT_USER_AGENT = "Fourslice-stats-crawler/0.1 (local Pokemon stats collector)"
MANIFEST_NAME = "manifest.json"


def utc_now() -> str:
    """ISO-8601 UTC timestamp, e.g. 2026-08-08T12:34:56.789012+00:00."""
    return datetime.now(timezone.utc).isoformat()


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sanitize_segment(segment: str) -> str:
    """Make one URL path segment safe as a filesystem name.

    Keeps letters/digits and _-.() and maps everything else to '_',
    so odd-but-legal characters ('?', '%', unicode) can never escape
    the mirror directory or collide with the manifest name.
    """
    cleaned = re.sub(r"[^A-Za-z0-9_.()\-]+", "_", segment)
    cleaned = cleaned.strip("._")
    return cleaned or "unnamed"


def rel_path_from_url(url: str) -> str:
    """Map a URL onto a mirror path under raw/.

    Used by crawlers that don't already know a clean relative path
    (i.e. Smogon, whose URLs mirror their server directory tree). The
    champions crawler uses its asset paths directly instead.
    """
    path = unquote(urlparse(url).path).strip("/")
    if not path:
        return "raw/root"
    segments = [sanitize_segment(seg) for seg in path.split("/") if seg]
    return "raw/" + "/".join(segments)


def write_mirror(out_dir: Path, rel_path: str, data: bytes) -> None:
    """Write resource bytes to the mirror tree, atomically per file.

    Writes to a temp name in the same directory then renames, so a
    crash mid-write can never leave a truncated file behind a real
    name in the manifest. A trailing slash on rel_path (directory-ish
    URLs) is stripped so a listing page can never clobber a directory.
    """
    rel_path = rel_path.rstrip("/") or "raw"
    dest = out_dir / rel_path
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    tmp.write_bytes(data)
    tmp.replace(dest)


@dataclass
class FetchResult:
    url: str
    status: int
    content_type: str
    data: bytes
    fetched_at: str
    final_url: str = ""


class FetchError(Exception):
    """Raised after retries are exhausted; the crawler records it as an error entry."""


class Fetcher:
    """Polite HTTP GET with retries, backoff, and an optional inter-request delay."""

    def __init__(
        self,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout: float = 60.0,
        retries: int = 4,
        backoff_base: float = 1.0,
        delay: float = 0.0,
        session: requests.Session | None = None,
    ):
        self.timeout = timeout
        self.retries = retries
        self.backoff_base = backoff_base
        self.delay = delay
        self._last_request_at = 0.0
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = user_agent
        self.session.headers["Accept-Encoding"] = "identity"  # store bytes exactly as served

    def _pace(self) -> None:
        if self.delay > 0:
            wait = self._last_request_at + self.delay - time.monotonic()
            if wait > 0:
                time.sleep(wait)
        self._last_request_at = time.monotonic()

    def get(self, url: str) -> FetchResult:
        """GET a URL, returning the final response bytes.

        404/410 are returned as a FetchResult (callers record them as
        errors but keep crawling). Other HTTP errors and network
        failures are retried with exponential backoff, then raised as
        FetchError.
        """
        self._pace()
        last_exc: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                resp = self.session.get(url, timeout=self.timeout, stream=True)
                if resp.status_code in (404, 410):
                    return FetchResult(
                        url=url,
                        status=resp.status_code,
                        content_type=resp.headers.get("Content-Type", "").split(";")[0],
                        data=b"",
                        fetched_at=utc_now(),
                        final_url=resp.url,
                    )
                resp.raise_for_status()
                data = resp.content
                return FetchResult(
                    url=url,
                    status=resp.status_code,
                    content_type=resp.headers.get("Content-Type", "").split(";")[0],
                    data=data,
                    fetched_at=utc_now(),
                    final_url=resp.url,
                )
            except requests.RequestException as exc:
                last_exc = exc
                if attempt < self.retries:
                    time.sleep(self.backoff_base * (2 ** (attempt - 1)))
        raise FetchError(f"{url} failed after {self.retries} tries: {last_exc}") from last_exc


class Manifest:
    """Append-only record of every resource a crawl tried, saved as JSON."""

    def __init__(self, source: str, out_dir: Path, base_url: str, crawler_version: str):
        self.source = source
        self.out_dir = out_dir
        self.base_url = base_url
        self.crawler_version = crawler_version
        self.started_at = utc_now()
        self.finished_at: str | None = None
        self.entries: list[dict] = []
        self._done_urls: set[str] = set()  # urls already stored successfully
        self._done_paths: dict[str, str] = {}  # url -> rel_path for on-disk check
        self._dirty_since_save = 0
        self._manifest_path = out_dir / MANIFEST_NAME

    @classmethod
    def load(cls, source: str, out_dir: Path, base_url: str, crawler_version: str) -> "Manifest":
        """Restore a previous crawl's manifest (for resume) or start fresh."""
        manifest = cls(source, out_dir, base_url, crawler_version)
        if manifest._manifest_path.exists():
            try:
                prev = json.loads(manifest._manifest_path.read_text(encoding="utf-8"))
                manifest.started_at = prev.get("started_at", manifest.started_at)
                for entry in prev.get("entries", []):
                    if entry.get("rel_path"):
                        manifest._done_urls.add(entry["url"])
                        manifest._done_paths[entry["url"]] = entry["rel_path"]
                manifest.entries = prev.get("entries", [])
            except (ValueError, OSError):
                # corrupt/unreadable manifest -- start over, don't guess
                manifest.entries = []
                manifest._done_urls = set()
        return manifest

    def already_done(self, url: str):
        """True when the URL was stored successfully AND its mirror file
        is still on disk. A manifest record alone never counts: anything
        that removed the file (a crash, a manual delete, the stats
        pipeline's raw-file cleanup) must not let a later resume skip
        re-fetching it, or the mirror silently loses data the pipeline
        still needs."""
        rel_path = self._done_paths.get(url)
        if rel_path is None:
            return False
        return (self.out_dir / rel_path).is_file()

    def add_success(self, url, *, kind, section, rel_path, content_type, size, sha256, fetched_at) -> None:
        self.entries.append(
            {
                "source": self.source,
                "url": url,
                "kind": kind,
                "section": section,
                "rel_path": rel_path,
                "content_type": content_type,
                "size": size,
                "sha256": sha256,
                "fetched_at": fetched_at,
                "error": None,
            }
        )
        self._done_urls.add(url)
        self._done_paths[url] = rel_path
        self._maybe_save()

    def add_error(self, url, *, kind, section, error, fetched_at) -> None:
        self.entries.append(
            {
                "source": self.source,
                "url": url,
                "kind": kind,
                "section": section,
                "rel_path": None,
                "content_type": None,
                "size": None,
                "sha256": None,
                "fetched_at": fetched_at,
                "error": error,
            }
        )
        self._maybe_save()

    def _maybe_save(self) -> None:
        self._dirty_since_save += 1
        if self._dirty_since_save >= 200:  # crash-safe without rewriting on every record
            self.save()

    def save(self) -> None:
        self.finished_at = utc_now()
        doc = {
            "source": self.source,
            "base_url": self.base_url,
            "crawler_version": self.crawler_version,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "totals": {
                "entries": len(self.entries),
                "ok": sum(1 for e in self.entries if e["error"] is None),
                "failed": sum(1 for e in self.entries if e["error"] is not None),
                "bytes": sum(e["size"] or 0 for e in self.entries),
            },
            "entries": self.entries,
        }
        self.out_dir.mkdir(parents=True, exist_ok=True)
        tmp = self._manifest_path.with_name(self._manifest_path.name + ".part")
        tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self._manifest_path)
        self._dirty_since_save = 0


class BaseCrawler:
    """Common crawl loop: fetch a URL, discover more, store every resource.

    Subclasses implement the source-specific pieces:
      * is_index(url)          -- directory listings / API roots that get expanded
      * discover_children()    -- child URLs extracted from an index payload
      * kind_for(url)          -- 'index' | 'data' | 'media'
      * section_for(url)       -- a human bucket recorded in the manifest
      * mirror_path_for(url)   -- where the resource is stored under raw/
    """

    source: str = "base"
    base_url: str = ""
    crawler_version: str = "0.1.0"

    def __init__(
        self,
        out_dir: Path,
        *,
        delay: float = 0.05,
        limit: int | None = None,
        dry_run: bool = False,
        resume: bool = True,
        include_media: bool = True,
        log_every: int = 25,
    ):
        self.out_dir = Path(out_dir)
        self.source_dir = self.out_dir / self.source
        self.delay = delay
        self.limit = limit
        self.dry_run = dry_run
        self.resume = resume
        self.include_media = include_media
        self.log_every = log_every
        self.fetcher = Fetcher(delay=delay if not dry_run else 0.0)
        self.manifest = Manifest.load(
            self.source, self.source_dir, self.base_url, self.crawler_version
        )
        self._queued: set[str] = set()
        self._queue: list[str] = []
        self._downloaded = 0  # data/media resources stored this run
        self._started = time.monotonic()

    # ---- source-specific hooks, overridden by subclasses -------------------
    def is_index(self, url: str) -> bool:  # pragma: no cover - abstract
        raise NotImplementedError

    def discover_children(self, url: str, payload: bytes) -> list[str]:  # pragma: no cover
        raise NotImplementedError

    def kind_for(self, url: str) -> str:  # pragma: no cover
        raise NotImplementedError

    def section_for(self, url: str) -> str:  # pragma: no cover
        return ""

    def mirror_path_for(self, url: str) -> str:  # pragma: no cover
        """Default mirror layout.

        Directory listings (URLs ending in '/') live under
        raw/.indexes/<dir-path>/index.html so a listing page can never
        collide with the data directory of the same name. Data files
        mirror their URL path under raw/.
        """
        if url.endswith("/"):
            rel = unquote(urlparse(url).path).strip("/")
            segments = [sanitize_segment(s) for s in rel.split("/") if s] or ["root"]
            return "raw/.indexes/" + "/".join(segments) + "/index.html"
        return rel_path_from_url(url)

    # ---- common crawl machinery ------------------------------------------------
    def enqueue(self, url: str) -> None:
        if url in self._queued:
            return
        self._queued.add(url)
        self._queue.append(url)

    def crawl(self, start_url: str) -> dict:
        """Fetch everything reachable from start_url and return totals."""
        self.enqueue(start_url)
        while self._queue:
            if self.limit is not None and self._downloaded >= self.limit:
                break
            url = self._queue.pop(0)
            try:
                if self.is_index(url):
                    self._process_index(url)
                else:
                    self._process_resource(url)
            except Exception as exc:  # keep the crawl alive no matter what
                self.manifest.add_error(
                    url, kind=self.kind_for(url), section=self.section_for(url),
                    error=f"{type(exc).__name__}: {exc}", fetched_at=utc_now(),
                )
        self.manifest.save()
        return self.totals()

    def _process_index(self, url: str) -> None:
        """Index pages are always refetched (even on resume, and even in
        dry-run) so newly published subfolders/files -- new Smogon months,
        new Champions dailies -- get discovered. Discovery needs the
        listing content; data files are what resume skips."""
        if self.dry_run:
            payload = b""
            try:
                result = self.fetcher.get(url)
            except FetchError:
                return
            if result.status == 200:
                payload = result.data
        else:
            result = self.fetcher.get(url)
            if result.status != 200:
                self.manifest.add_error(
                    url, kind="index", section=self.section_for(url),
                    error=f"HTTP {result.status}", fetched_at=result.fetched_at,
                )
                return  # no children to discover from a failed listing
            if not (self.resume and self.manifest.already_done(url)):
                self._store(url, result, kind="index", section=self.section_for(url))
            payload = result.data
        for child in self.discover_children(url, payload):
            self.enqueue(child)

    def _process_resource(self, url: str) -> None:
        if self.resume and self.manifest.already_done(url):
            return
        kind = self.kind_for(url)
        section = self.section_for(url)
        if self.dry_run:
            self._downloaded += 1
            self._log(url, kind, "would-fetch")
            return
        result = self.fetcher.get(url)
        if result.status != 200:
            self.manifest.add_error(
                url, kind=kind, section=section,
                error=f"HTTP {result.status}", fetched_at=result.fetched_at,
            )
            self._log(url, kind, f"HTTP {result.status}")
            return
        self._store(url, result, kind=kind, section=section)
        self._downloaded += 1
        self._log(url, kind, "ok")

    def _store(self, url: str, result: FetchResult, *, kind: str, section: str) -> None:
        rel_path = self.mirror_path_for(url)
        if not self.dry_run:
            write_mirror(self.source_dir, rel_path, result.data)
        self.manifest.add_success(
            url, kind=kind, section=section, rel_path=rel_path,
            content_type=result.content_type, size=len(result.data),
            sha256=sha256_hex(result.data), fetched_at=result.fetched_at,
        )
        self.on_resource_stored(url, result, kind=kind, section=section)

    def on_resource_stored(self, url: str, result: FetchResult, *, kind: str, section: str) -> None:
        """Hook for subclass side-effects after a resource lands on disk,
        e.g. parsing a CSV to discover more URLs. Default: nothing."""

    def _log(self, url: str, kind: str, outcome: str) -> None:
        if self._downloaded % self.log_every == 0 or outcome != "ok":
            elapsed = time.monotonic() - self._started
            log.info(
                "[%s] %10s %6s #%6d (%ds) %s",
                self.source, outcome, kind, self._downloaded, int(elapsed), url
            )

    def totals(self) -> dict:
        return {
            "source": self.source,
            "queued": len(self._queued),
            "downloaded_this_run": self._downloaded,
            "ok": sum(1 for e in self.manifest.entries if e["error"] is None),
            "failed": sum(1 for e in self.manifest.entries if e["error"] is not None),
            "bytes": sum(e["size"] or 0 for e in self.manifest.entries),
        }