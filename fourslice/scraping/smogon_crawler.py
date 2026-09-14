"""
fourslice/scraping/smogon_crawler.py

Crawler for smogon.com/stats -- Pokemon Showdown usage statistics
published monthly since Nov 2014.

The stats server exposes Apache auto-index pages at every level:
/stats/ lists topic months (e.g. 2025-07/, plus DLC1/H1/H2 variants),
and each month folder lists its stat files plus more subfolders
(chaos/, leads/, metagame/, monotype/ -- which itself nests chaos/,
leads/, metagame/ -- and moveset/). The crawler walks that tree,
fetching every index page and downloading every stat file it finds --
usage text files, .gz copies, and the JSON chaos data -- storing each
one raw in the mirror with a record in the shared manifest.
"""

import html
import re
from urllib.parse import urljoin, urlparse

from .base import BaseCrawler

HREF_RE = re.compile(r'href="([^"]+)"')
USAGE_TXT_RE = re.compile(r'^(.+)-([0-9]+)\.txt$')
# also matches the gzip copies Smogon publishes alongside each .txt
USAGE_TXT_GZ_RE = re.compile(r'^(.+)-([0-9]+)\.txt(\.gz)?$')


class SmogonCrawler(BaseCrawler):
    source = "smogon"
    base_url = "https://www.smogon.com/stats/"

    def __init__(
        self,
        out_dir,
        *,
        delay: float = 0.05,
        limit=None,
        dry_run: bool = False,
        resume: bool = True,
        include_media: bool = True,
        log_every: int = 25,
        pipeline_only: bool = False,
    ):
        """Same as BaseCrawler, plus *pipeline_only*: when True, only the
        files the stats pipeline actually reads are crawled -- the
        <format>-0 usage ladders (for Total-battles standings) and the
        moveset/*.txt(.gz) data files -- skipping chaos/, leads/,
        metagame/, the nested monotype/ tree, and the 1500/1630/1760
        usage ladders, none of which the pipeline consumes."""
        super().__init__(
            out_dir,
            delay=delay,
            limit=limit,
            dry_run=dry_run,
            resume=resume,
            include_media=include_media,
            log_every=log_every,
        )
        self.pipeline_only = pipeline_only

    def _pipeline_needed(self, url):
        """True when a URL is something the stats pipeline consumes.

        Per month the pipeline needs exactly:
          * the month root's <format>-0 usage file (the -0/overall ladder,
            read for Total battles when building format standings), and
          * every moveset/<format>-<elo>.txt(.gz) data file (the real
            per-Pokemon payload captured into stats.db).
        Everything else the server publishes -- chaos/*.json, leads/,
        metagame/, the nested monotype/ tree, and the 1500/1630/1760
        ladders -- is never read by format_standings() or the moveset
        capture path, so fetching it just wastes time and disk.
        """
        path = urlparse(url).path
        rel = path[len("/stats/"):] if path.startswith("/stats/") else path
        parts = [p for p in rel.strip("/").split("/") if p]
        if len(parts) <= 1:
            return True  # root / month index: needed to discover children
        if url.endswith("/"):
            # sub-folder index -- only moveset/ holds data the pipeline reads
            return parts[-1] == "moveset"
        # a data file
        match = USAGE_TXT_GZ_RE.match(parts[-1])
        if not match:
            return False  # e.g. chaos/*.json
        parent = parts[-2]  # safe: data files always sit under a folder
        if parent == "moveset":
            return True  # moveset/<fmt>-<elo>.txt(.gz)
        # usage file directly in the month root: keep only the -0 ladder
        return match.group(2) == "0"

    def is_index(self, url: str) -> bool:
        return url.endswith("/")

    def kind_for(self, url: str) -> str:
        return "index" if url.endswith("/") else "data"

    def discover_children(self, url: str, payload: bytes) -> list[str]:
        text = payload.decode("utf-8", errors="replace") if payload else ""
        children: list[str] = []
        for href in HREF_RE.findall(text):
            href = html.unescape(href.strip())
            if not href or href.startswith(("http://", "https://", "/")):
                continue
            if href.split("/")[0] in ("..", "."):
                continue
            if "?" in href or "#" in href:
                continue
            child = urljoin(url, href)
            if not child.startswith(self.base_url):
                continue
            if child not in children:
                children.append(child)
        if self.pipeline_only:
            children = [c for c in children if self._pipeline_needed(c)]
        return children

    def section_for(self, url: str) -> str:
        path = urlparse(url).path
        rel = path[len("/stats/"):] if path.startswith("/stats/") else path
        parts = [p for p in rel.strip("/").split("/") if p]
        if not parts:
            return ""
        if url.endswith("/") and parts:
            return "/".join(parts)
        return "/".join(parts[:-1])

    def is_usage_txt(self, url: str) -> bool:
        """Check if URL is a usage text file (format-elo.txt)."""
        filename = url.split("/")[-1]
        is_txt = filename.endswith(".txt") or filename.endswith(".txt.gz")
        return is_txt and USAGE_TXT_RE.match(filename) is not None

    def discover_format_standings(self, month_url: str) -> dict[str, int]:
        """
        Crawl a month folder and return a dict of {format_name: total_battles}.
        Only processes .txt files (format-elo.txt) and sums battles from the
        "-0" (overall) ladder for each format.
        """
        standings: dict[str, int] = {}
        
        def walk(url: str) -> None:
            result = self.fetcher.get(url)
            if result.status != 200:
                return
            
            children = self.discover_children(url, result.data)
            for child in children:
                if self.is_index(child):
                    walk(child)
                elif self.is_usage_txt(child):
                    # Extract format name from format-elo.txt
                    filename = child.split("/")[-1]
                    match = USAGE_TXT_RE.match(filename)
                    if match:
                        fmt_name = match.group(1)
                        elo = match.group(2)
                        # Only read the -0 (overall) ladder for total battles
                        if elo == "0":
                            text = result.data.decode("utf-8", errors="replace")
                            battles = self._extract_total_battles(text)
                            if battles > 0:
                                standings[fmt_name] = battles
        
        walk(month_url)
        return standings

    def _extract_total_battles(self, text: str) -> int:
        """Extract 'Total battles: N' from usage text file."""
        for line in text.splitlines():
            if line.strip().startswith("Total battles:"):
                match = re.search(r'Total battles:\s*([0-9]+)', line)
                if match:
                    return int(match.group(1))
        return 0

    def crawl_format(self, month_url: str, format_name: str, elos: list[str] = None) -> None:
        """
        Crawl only the specified format's .txt files for given ELOs.
        If elos is None, crawls all available ELOs (0, 1500, 1630, 1760).
        """
        if elos is None:
            elos = ["0", "1500", "1630", "1760"]

        # Build expected filenames
        expected_files = {f"{format_name}-{elo}.txt" for elo in elos}

        def walk(url: str) -> None:
            result = self.fetcher.get(url)
            if result.status != 200:
                return

            children = self.discover_children(url, result.data)
            for child in children:
                if self.is_index(child):
                    walk(child)
                elif self.is_usage_txt(child):
                    filename = child.split("/")[-1]
                    if filename in expected_files:
                        self.enqueue(child)

        walk(month_url)
        # Process the queued files - use _queue like the base crawl() method does
        while self._queue:
            url = self._queue.pop(0)
            if self.is_index(url):
                self._process_index(url)
            else:
                self._process_resource(url)