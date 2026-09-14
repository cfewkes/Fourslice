"""
test_external_scrapers.py

Offline tests for the two external-stat crawlers
(fourslice/scraping/). No network: discovery, storage layout,
manifest round-trips and the crawl loop are all exercised against
in-memory fixtures / a fake fetch layer.

Run with:  python -m pytest tests/test_external_scrapers.py
"""

import json

from fourslice.scraping.base import (
    BaseCrawler,
    FetchResult,
    Manifest,
    rel_path_from_url,
    write_mirror,
)
from fourslice.scraping.smogon_crawler import SmogonCrawler

SMOGON_INDEX_HTML = b"""<!DOCTYPE html>
<html><head><title>Index of /stats/2025-07/</title></head><body>
<h1>Index of /stats/2025-07/</h1>
<table>
<tr><th colspan="5">Index</th></tr>
<tr><td valign="top"><a href="../">Parent Directory</a></td></tr>
<tr><td><a href="chaos/">chaos/</a></td><td>01-Aug-2025 13:39</td></tr>
<tr><td><a href="gen1ou-0.txt">gen1ou-0.txt</a></td><td>12181</td></tr>
<tr><td><a href="gen1ou-0.txt.gz">gen1ou-0.txt.gz</a></td><td>3138</td></tr>
<tr><td><a href="?C=N;O=D">Name</a></td></tr>
<tr><td><a href="/stats/other/">absolute link</a></td></tr>
</table>
</body></html>"""


def test_rel_path_from_url():
    assert rel_path_from_url("https://www.smogon.com/stats/2025-07/gen1ou-0.txt") == (
        "raw/stats/2025-07/gen1ou-0.txt"
    )
    assert rel_path_from_url("https://championsbattledata.com/api/") == "raw/api"
    assert rel_path_from_url("https://example.com/") == "raw/root"


def test_write_mirror(tmp_path):
    data = b"row1\nrow2\n"
    write_mirror(tmp_path, "raw/sub/file.txt", data)
    target = tmp_path / "raw/sub/file.txt"
    assert target.read_bytes() == data
    assert not list(tmp_path.rglob("*.part"))  # no temp files left behind


def test_smogon_discover_children():
    crawler = SmogonCrawler(out_dir="ignored", dry_run=True)
    children = crawler.discover_children(
        "https://www.smogon.com/stats/2025-07/", SMOGON_INDEX_HTML
    )
    assert "https://www.smogon.com/stats/2025-07/chaos/" in children
    assert "https://www.smogon.com/stats/2025-07/gen1ou-0.txt" in children
    assert "https://www.smogon.com/stats/2025-07/gen1ou-0.txt.gz" in children
    # parent, sort links, and absolute links must be ignored
    assert "https://www.smogon.com/stats/" not in children
    assert "https://www.smogon.com/stats/2025-07/?C=N;O=D" not in children
    assert "https://www.smogon.com/stats/other.txt" not in children


def test_smogon_discover_children_pipeline_only():
    """pipeline_only=True narrows discovery to what the stats pipeline
    reads: the <format>-0 usage ladders and moveset/*.txt(.gz) files. All
    sibling trees (chaos/, leads/, metagame/, monotype/) and the non-0
    usage ladders are dropped."""
    crawler = SmogonCrawler(out_dir="ignored", dry_run=True, pipeline_only=True)
    month_html = b"""<!DOCTYPE html>
<html><body>
<h1>Index of /stats/2026-08/</h1>
<tr><td><a href="../">Parent Directory</a></td></tr>
<tr><td><a href="chaos/">chaos/</a></td></tr>
<tr><td><a href="leads/">leads/</a></td></tr>
<tr><td><a href="metagame/">metagame/</a></td></tr>
<tr><td><a href="monotype/">monotype/</a></td></tr>
<tr><td><a href="moveset/">moveset/</a></td></tr>
<tr><td><a href="gen1ou-0.txt">gen1ou-0.txt</a></td></tr>
<tr><td><a href="gen1ou-0.txt.gz">gen1ou-0.txt.gz</a></td></tr>
<tr><td><a href="gen1ou-1500.txt.gz">gen1ou-1500.txt.gz</a></td></tr>
<tr><td><a href="gen1ou-1760.txt.gz">gen1ou-1760.txt.gz</a></td></tr>
<tr><td><a href="vgc2026-0.txt.gz">vgc2026-0.txt.gz</a></td></tr>
</body></html>"""
    kept = set(crawler.discover_children(
        "https://www.smogon.com/stats/2026-08/", month_html
    ))
    # only the -0 ladders survive from the month root
    assert "https://www.smogon.com/stats/2026-08/gen1ou-0.txt" in kept
    assert "https://www.smogon.com/stats/2026-08/gen1ou-0.txt.gz" in kept
    assert "https://www.smogon.com/stats/2026-08/vgc2026-0.txt.gz" in kept
    assert "https://www.smogon.com/stats/2026-08/gen1ou-1500.txt.gz" not in kept
    assert "https://www.smogon.com/stats/2026-08/gen1ou-1760.txt.gz" not in kept
    # only moveset/ descends any further
    assert "https://www.smogon.com/stats/2026-08/moveset/" in kept
    assert "https://www.smogon.com/stats/2026-08/chaos/" not in kept
    assert "https://www.smogon.com/stats/2026-08/leads/" not in kept
    assert "https://www.smogon.com/stats/2026-08/metagame/" not in kept
    assert "https://www.smogon.com/stats/2026-08/monotype/" not in kept

    moveset_html = b"""<!DOCTYPE html>
<html><body>
<h1>Index of /stats/2026-08/moveset/</h1>
<tr><td><a href="../">Parent Directory</a></td></tr>
<tr><td><a href="gen1ou-0.txt.gz">gen1ou-0.txt.gz</a></td></tr>
<tr><td><a href="gen1ou-1500.txt.gz">gen1ou-1500.txt.gz</a></td></tr>
<tr><td><a href="gen1ou-1760.txt.gz">gen1ou-1760.txt.gz</a></td></tr>
<tr><td><a href="vgc2026-0.txt.gz">vgc2026-0.txt.gz</a></td></tr>
</body></html>"""
    moved = set(crawler.discover_children(
        "https://www.smogon.com/stats/2026-08/moveset/", moveset_html
    ))
    # every moveset division is data the pipeline needs
    assert "https://www.smogon.com/stats/2026-08/moveset/gen1ou-0.txt.gz" in moved
    assert "https://www.smogon.com/stats/2026-08/moveset/gen1ou-1500.txt.gz" in moved
    assert "https://www.smogon.com/stats/2026-08/moveset/gen1ou-1760.txt.gz" in moved
    assert "https://www.smogon.com/stats/2026-08/moveset/vgc2026-0.txt.gz" in moved


def test_smogon_section_for():
    crawler = SmogonCrawler(out_dir="ignored", dry_run=True)
    assert crawler.section_for("https://www.smogon.com/stats/2025-07/") == "2025-07"
    assert crawler.section_for("https://www.smogon.com/stats/2025-07/chaos/") == "2025-07/chaos"
    assert (
        crawler.section_for("https://www.smogon.com/stats/2025-07/monotype/chaos/gen9monotype-0.json")
        == "2025-07/monotype/chaos"
    )







def test_manifest_roundtrip(tmp_path):
    m1 = Manifest("fake", tmp_path, "http://fake/", "0.1.0")
    m1.add_success("http://fake/a.txt", kind="data", section="s", rel_path="raw/a.txt",
                   content_type="text/plain", size=3, sha256="abc", fetched_at="t")
    m1.add_success("http://fake/b.txt", kind="data", section="s", rel_path="raw/b.txt",
                   content_type="text/plain", size=4, sha256="def", fetched_at="t")
    m1.add_error("http://fake/missing.txt", kind="data", section="s", error="HTTP 404", fetched_at="t")
    m1.save()

    # already_done() also requires the mirror file to still exist on disk
    (tmp_path / "raw").mkdir()
    (tmp_path / "raw/a.txt").write_text("abc", encoding="utf-8")
    (tmp_path / "raw/b.txt").write_text("defg", encoding="utf-8")

    m2 = Manifest.load("fake", tmp_path, "http://fake/", "0.1.0")
    assert len(m2.entries) == 3
    assert m2.already_done("http://fake/a.txt")
    assert not m2.already_done("http://fake/missing.txt")
    ok = sum(1 for e in m2.entries if e["error"] is None)
    failed = sum(1 for e in m2.entries if e["error"] is not None)
    assert (ok, failed) == (2, 1)
    # deleting the mirror file invalidates the manifest record
    (tmp_path / "raw/a.txt").unlink()
    assert not m2.already_done("http://fake/a.txt")
    written = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert written["totals"]["bytes"] == 7


class _FakeFetcher:
    """Returns canned bytes for any URL without touching the network."""

    def get(self, url):
        return FetchResult(
            url=url, status=200, content_type="text/plain",
            data=b"content for " + url.encode("utf-8"),
            fetched_at="t",
        )


class _FakeCrawler(BaseCrawler):
    source = "fake"
    base_url = "http://fake/root/"

    def is_index(self, url):
        return url in ("http://fake/root/", "http://fake/root/dir/")

    def discover_children(self, url, payload):
        if url == "http://fake/root/":
            return ["http://fake/root/a.txt", "http://fake/root/dir/"]
        if url == "http://fake/root/dir/":
            return ["http://fake/root/dir/b.txt"]
        return []

    def kind_for(self, url):
        return "data"

    def section_for(self, url):
        return "fake-section"


def test_crawl_loop_stores_mirror_and_manifest(tmp_path):
    crawler = _FakeCrawler(tmp_path, dry_run=False)
    crawler.fetcher = _FakeFetcher()
    totals = crawler.crawl("http://fake/root/")

    assert totals["ok"] == 4  # root index, a.txt, dir/ index, b.txt
    for rel in ("root/a.txt", "root/dir/b.txt"):
        assert (tmp_path / "fake/raw" / rel).exists()
    # listing pages land under .indexes, not at data paths
    assert (tmp_path / "fake/raw/.indexes/root/index.html").exists()
    assert (tmp_path / "fake/raw/.indexes/root/dir/index.html").exists()
    m = json.loads((tmp_path / "fake/manifest.json").read_text(encoding="utf-8"))
    assert m["source"] == "fake"
    assert len(m["entries"]) == 4


def test_crawl_loop_resume_skips_existing(tmp_path):
    first_run = _FakeCrawler(tmp_path, dry_run=False)
    first_run.fetcher = _FakeFetcher()
    first_run.crawl("http://fake/root/")
    first = json.loads((tmp_path / "fake/manifest.json").read_text(encoding="utf-8"))
    assert len(first["entries"]) == 4

    second_run = _FakeCrawler(tmp_path, dry_run=False)
    second_run.fetcher = _FakeFetcher()
    totals = second_run.crawl("http://fake/root/")
    second = json.loads((tmp_path / "fake/manifest.json").read_text(encoding="utf-8"))

    assert totals["downloaded_this_run"] == 0  # everything already stored
    assert len(second["entries"]) == len(first["entries"])  # no duplicated records


def test_dry_run_downloads_nothing(tmp_path):
    crawler = _FakeCrawler(tmp_path, dry_run=True)
    crawler.fetcher = _FakeFetcher()
    totals = crawler.crawl("http://fake/root/")
    # dry-run counts only data/media resources; the two index listings are
    # refetched for discovery and intentionally never counted
    assert totals["downloaded_this_run"] == 2
    assert not (tmp_path / "fake/raw").exists()  # no mirror files written


def test_log_does_not_raise(tmp_path):
    """Regression: _log() previously raised NameError because the logging
    import was missing from fourslice.scraping.base."""
    crawler = _FakeCrawler(tmp_path, dry_run=True)
    # Should not raise NameError (or any exception)
    crawler._log("http://fake/a.txt", "data", "ok")
    crawler._log("http://fake/b.txt", "data", "error")