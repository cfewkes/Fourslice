"""
fourslice/scraping/__init__.py

Crawlers for the two external stat sources Fourslice ingests:
smogon.com/stats and championsbattledata.com/api. Both crawlers write
the same on-disk shape (see scraping/base.py), so downstream code can
read either source with the same loader.
"""

from .base import Fetcher, Manifest, write_mirror, rel_path_from_url  # noqa: F401
from .smogon_crawler import SmogonCrawler  # noqa: F401

CRAWLER_VERSION = "0.1.0"
