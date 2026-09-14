"""
Run directly with: python tests/test_sprites.py

Pure-Python tests (no Qt widgets, no display) for fourslice/sprites.py:
species -> sprite filename resolution and the on-disk SpriteStore
cache, using a fake downloader so nothing here touches the network or
the real app-data folder. Also exercises the background-thread worker
(gui/sprite_fetcher.py) with its signals connected directly.
"""

import base64
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fourslice.sprites import (
    SPRITE_BASE_URL,
    SpriteStore,
    normalize_species,
    sprite_id_candidates,
)

# A real 1x1 PNG so "downloaded" sprite bytes actually decode.
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def test_sprite_id_candidates_keep_forme_hyphens_first():
    assert sprite_id_candidates("Landorus-Therian") == [
        "landorus-therian", "landorustherian",
    ]
    assert sprite_id_candidates("Ogerpon-Wellspring-Tera") == [
        "ogerpon-wellspring-tera", "ogerponwellspringtera",
    ]


def test_sprite_id_candidates_collapse_everything_else():
    assert sprite_id_candidates("Nidoran-M") == ["nidoran-m", "nidoranm"]
    assert sprite_id_candidates("Mr. Mime") == ["mrmime"]  # no hyphen in the name to preserve
    assert sprite_id_candidates("Farfetch'd") == ["farfetchd"]
    assert sprite_id_candidates("Flabebe") == ["flabebe"]
    assert sprite_id_candidates("Blastoise") == ["blastoise"]


def test_sprite_id_candidates_cover_mega_formes():
    assert sprite_id_candidates("Blastoise-Mega") == ["blastoise-mega", "blastoisemega"]
    assert sprite_id_candidates("Charizard-Mega-Y") == ["charizard-mega-y", "charizardmegay"]
    assert sprite_id_candidates("Groudon-Primal") == ["groudon-primal", "groudonprimal"]


def test_sprite_store_downloads_mega_formes_like_base_species():
    with tempfile.TemporaryDirectory() as tmp_dir:
        calls = []
        store = SpriteStore(
            base_dir=Path(tmp_dir),
            downloader=lambda url: (calls.append(url) or PNG_BYTES),
        )

        path, animated = store.get_or_fetch("Blastoise-Mega", back=True)
        assert path is not None and path.exists()
        assert animated is True
        assert calls[0] == f"{SPRITE_BASE_URL}/sprites/gen5ani-back/blastoise-mega.gif"


def test_normalize_species_matches_showdown_toid():
    assert normalize_species("Landorus-Therian") == "landorustherian"
    assert normalize_species("Nidoran-M") == "nidoranm"
    assert normalize_species("Mr. Mime") == "mrmime"
    assert normalize_species("Farfetch'd") == "farfetchd"
    assert normalize_species("Flabebe") == "flabebe"
    assert normalize_species("Type: Null") == "typenull"
    assert normalize_species("Ho-Oh") == "hooh"


def test_sprite_store_downloads_and_caches_to_disk():
    with tempfile.TemporaryDirectory() as tmp_dir:
        calls = []
        store = SpriteStore(
            base_dir=Path(tmp_dir),
            downloader=lambda url: (calls.append(url) or PNG_BYTES),
        )

        path, animated = store.get_or_fetch("Blastoise", back=True)
        assert path is not None and path.exists()
        assert animated is True  # the gen-5 animated set is tried first
        assert calls[0] == f"{SPRITE_BASE_URL}/sprites/gen5ani-back/blastoise.gif"
        assert path.read_bytes() == PNG_BYTES

        # A second lookup is served from disk -- no new download.
        again, _ = store.get_or_fetch("Blastoise", back=True)
        assert again == path
        assert len(calls) == 1

        # cached() never touches the downloader.
        cached, _ = store.cached("Blastoise", back=True)
        assert cached == path
        assert len(calls) == 1


def test_sprite_store_falls_back_to_gen5_static_when_animated_missing():
    with tempfile.TemporaryDirectory() as tmp_dir:
        store = SpriteStore(
            base_dir=Path(tmp_dir),
            downloader=lambda url: None if "gen5ani" in url or "/ani/" in url else PNG_BYTES,
        )
        path, animated = store.get_or_fetch("Whimsicott", back=False)
        assert path is not None
        assert animated is False
        assert path.name == "whimsicott-front-static-gen5.png"


def test_sprite_store_tries_hyphenated_then_collapsed_forme():
    with tempfile.TemporaryDirectory() as tmp_dir:
        calls = []

        def downloader(url):
            calls.append(url)
            if "landorus-therian" in url:
                return None
            return PNG_BYTES

        store = SpriteStore(base_dir=Path(tmp_dir), downloader=downloader)
        path, animated = store.get_or_fetch("Landorus-Therian", back=True)
        assert path is not None
        assert path.name == "landorustherian-back-gen5ani-back-gen5.gif"
        assert any("landorus-therian" in url for url in calls)


def test_sprite_store_style_param_selects_3d_folder_chain_and_encodes_style_in_filename():
    with tempfile.TemporaryDirectory() as tmp_dir:
        calls = []

        def downloader(url):
            calls.append(url)
            # gen-5 static always missing; serve the 3D (xy) animated set
            return None if "gen5" in url else PNG_BYTES

        # 3d store: prefers xyani -> xy -> ani; filenames carry the style
        store = SpriteStore(base_dir=Path(tmp_dir), downloader=downloader, style="3d")
        assert store.style == "3d"
        path, animated = store.get_or_fetch("Greninja", back=False)
        assert path is not None
        assert path.name == "greninja-front-xyani-3d.gif"
        # The 3D chain must hit xyani first, not gen5ani
        assert calls[0] == f"{SPRITE_BASE_URL}/sprites/xyani/greninja.gif"
        assert not any("gen5ani" in url for url in calls)

        # Switching style clears the per-session failure cache and changes filenames
        store.set_style("gen5")
        assert store.style == "gen5"
        calls.clear()
        path2, animated2 = store.get_or_fetch("Greninja", back=False)
        assert path2 is not None
        assert path2.name == "greninja-front-ani-gen5.gif"
        assert calls[0] == f"{SPRITE_BASE_URL}/sprites/gen5ani/greninja.gif"


def test_sprite_store_style_default_is_gen5():
    with tempfile.TemporaryDirectory() as tmp_dir:
        store = SpriteStore(base_dir=Path(tmp_dir), downloader=lambda url: PNG_BYTES)
        assert store.style == "gen5"


def test_sprite_store_returns_none_when_no_sprite_exists_and_does_not_retry():
    with tempfile.TemporaryDirectory() as tmp_dir:
        calls = []
        store = SpriteStore(
            base_dir=Path(tmp_dir),
            downloader=lambda url: (calls.append(url) or None),
        )

        assert store.get_or_fetch("Sneasler", back=True) == (None, None)
        attempts = len(calls)
        assert attempts > 0  # it really tried every fallback URL

        # Failures are remembered for the session -- no re-downloading.
        assert store.get_or_fetch("Sneasler", back=True) == (None, None)
        assert len(calls) == attempts


def test_sprite_fetch_worker_emits_ready_only_for_fetched_sprites():
    from PySide6.QtCore import QCoreApplication

    QCoreApplication.instance() or QCoreApplication([])

    with tempfile.TemporaryDirectory() as tmp_dir:
        store = SpriteStore(
            base_dir=Path(tmp_dir),
            downloader=lambda url: None if "sneasler" in url else PNG_BYTES,
        )

        from fourslice.gui.sprite_fetcher import SpriteFetchWorker

        ready = []
        worker = SpriteFetchWorker(store)
        worker.sprite_ready.connect(lambda *args: ready.append(args))
        worker.fetch([("Blastoise", True), ("Whimsicott", False), ("Sneasler", False)])

        assert ("Blastoise", True, True) in ready
        assert ("Whimsicott", False, True) in ready
        assert all(args[0] != "Sneasler" for args in ready), (
            "the worker should only report species that actually resolved"
        )


def test_sprite_fetch_worker_request_stop_aborts_between_species():
    from PySide6.QtCore import QCoreApplication

    QCoreApplication.instance() or QCoreApplication([])

    with tempfile.TemporaryDirectory() as tmp_dir:
        calls = []
        store = SpriteStore(
            base_dir=Path(tmp_dir),
            downloader=lambda url: (calls.append(url) or PNG_BYTES),
        )

        from fourslice.gui.sprite_fetcher import SpriteFetchWorker

        # Stopped before any work: nothing is fetched, nothing downloaded.
        worker = SpriteFetchWorker(store)
        worker.request_stop()
        ready = []
        worker.sprite_ready.connect(lambda *args: ready.append(args))
        worker.fetch([("Blastoise", True), ("Whimsicott", False)])
        assert ready == []
        assert calls == []

        # Stopped mid-batch: the loop bails at the next species.
        worker2 = SpriteFetchWorker(store)

        def stop_on_second(url):
            calls.append(url)
            if len(calls) == 1:
                worker2.request_stop()
            return PNG_BYTES

        store._downloader = stop_on_second
        ready2 = []
        worker2.sprite_ready.connect(lambda *args: ready2.append(args))
        worker2.fetch([("Blastoise", True), ("Whimsicott", False), ("Sneasler", False)])
        assert len(ready2) <= 1, "stop must abort before the whole batch drains"


if __name__ == "__main__":
    test_sprite_id_candidates_keep_forme_hyphens_first()
    test_sprite_id_candidates_collapse_everything_else()
    test_normalize_species_matches_showdown_toid()
    test_sprite_store_downloads_and_caches_to_disk()
    test_sprite_store_falls_back_to_gen5_static_when_animated_missing()
    test_sprite_store_tries_hyphenated_then_collapsed_forme()
    test_sprite_store_returns_none_when_no_sprite_exists_and_does_not_retry()
    test_sprite_store_style_param_selects_3d_folder_chain_and_encodes_style_in_filename()
    test_sprite_store_style_default_is_gen5()
    test_sprite_fetch_worker_emits_ready_only_for_fetched_sprites()
    test_sprite_fetch_worker_request_stop_aborts_between_species()
    test_sprite_id_candidates_cover_mega_formes()
    test_sprite_store_downloads_mega_formes_like_base_species()
    print("\nAll sprite tests passed.")

