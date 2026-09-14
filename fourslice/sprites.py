"""
fourslice/sprites.py

Resolves Pokemon species names (as they appear in replay logs) to the
gen-5 style sprite art Pokemon Showdown serves on
play.pokemonshowdown.com, and keeps an on-disk cache so the Replays
tab never downloads the same sprite twice.

The gen-5 folders -- gen5/ + gen5-back/ (static) and gen5ani/ +
gen5ani-back/ (animated) -- are the community-drawn "gen 5 style" set
that Showdown's own web player uses. They cover almost every Pokemon
across ALL generations, but not quite every one (Sneasler 404s, for
example). Resolution therefore walks a fallback chain per orientation:

    gen-5 animated -> gen-5 static -> current-gen animated -> text

so a battle can mix eras without ever showing a broken image.

Filename rules (confirmed against the live server): the sprite folders
keep the hyphen between a species and its forme (landorus-therian.gif,
arcanine-hisui.png) but strip every other non-alphanumeric character
(nidoranm.gif, mrmime.gif, farfetchd.gif). Rather than maintaining a
forme list that drifts as new formes are added, each species is tried
under BOTH spellings.

SpriteStore is deliberately Qt-free: the GUI layers downloads onto a
background thread (gui/sprite_fetcher.py), and everything here is
unit-testable without a display or an event loop.
"""

import base64
import re
import unicodedata
from pathlib import Path

import requests

from fourslice import config

# Showdown's own production server; the files it serves are the same
# sprites its web client loads.
SPRITE_BASE_URL = "https://play.pokemonshowdown.com"

# Official Pokémon Showdown Substitute doll assets (Gen 5 style)
SUBSTITUTE_FRONT_URL = "https://play.pokemonshowdown.com/sprites/substitutes/gen5/substitute.png"
SUBSTITUTE_BACK_URL = "https://play.pokemonshowdown.com/sprites/substitutes/gen5-back/substitute.png"

# Base64 fallback assets for Substitute doll (front and back) so offline / test environments
# display the actual official substitute doll art.
SUBSTITUTE_FRONT_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAGAAAABgCAMAAADVRocKAAAAIVBMVEUAAAB7jFqUpWulvYS91py9vbXWzsX///9SUkpKYzo"
    "AAACTT2uAAAAAAXRSTlMAQObYZgAAARtJREFUeNrt1IEGJDEQhOGpVDaZqvd/4EtnY9wdgB4W/UFbqF9Yc5VSSimllFLKDw"
    "Ke+wqQ2rf3uPlE0uv03v3OPjb2Hk9JF9Ot8XD+fO9svXfCNxcl77M39mCTLxTQW1/ONtMDIhv/p9SAw78RZwYMATiJNtdpyA"
    "wYEIJtac6JoPTCoXlqV3rhkR+QDQk4pOSASPl+HiEyuSDG5PiMMWKYi7Of4DnHDOMbgHMDnhHYhkEChjIL5wGnIEBIDdzz7w"
    "C2zILu8QTGkJQcEO4x5mevD4nkNyE4aR9R+Iwzv2kHcgpSBHZBPBqEaDjtO+QIqElnfwfsKyXwFCxAMb/IduanVPICIPsfFB"
    "yu5SnE73f46yqllFJKKaWUH/QH2xoLUP4wm70AAAAASUVORK5CYII="
)
SUBSTITUTE_BACK_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAGAAAABgCAMAAADVRocKAAAAHlBMVEUAAAB7jFqlvYQAAABKYzqUpWu91pz///9SUkrWzsX"
    "qMKBPAAAAAXRSTlMAQObYZgAAAOBJREFUeNrt0zEOAyEQQ1E8NrC5/4WzKIsI6SINnV9F5S+EKP8zMzMzMzOTygCc2o8Yha"
    "s1HAuAZLTWdGQfUgAxNKCkW/O3lp9QBGK51JAdQHwDGw7tV9xqhHIDVWseM1HyUHWY470CFBP3A8LSe6eAzABADOsGaz8FiV"
    "1yQBB+qSSisCNZluw3hhQRLMfeWIjb2TeOkFJvIPxIfgNpK4jsV+Z+FbAVXq/er9z9raDUK1DCFqgxkOl/TMDWUNYXmLh1ao"
    "ROBgbm/jHioZzAwg885pksufiY52JmZmZmZmbJ3hdXBMzHKbCvAAAAAElFTkSuQmCC"
)

# (folder, animated) preference order per orientation. gen-5 art is
# always tried first; the default animated set is the last resort
# before the caller gives up and shows a plain text label.
# Style-aware fallback chains:
#   gen5: gen5ani -> gen5 -> ani (current behavior)
#   3d:   xyani -> xy -> ani (Showdown's 3D/XY model rips)
_FRONT_FOLDERS_GEN5 = (
    ("gen5ani", True),
    ("gen5", False),
    ("ani", True),
)
_BACK_FOLDERS_GEN5 = (
    ("gen5ani-back", True),
    ("gen5-back", False),
    ("ani-back", True),
)

_FRONT_FOLDERS_3D = (
    ("xyani", True),
    ("xy", False),
    ("ani", True),
)
_BACK_FOLDERS_3D = (
    ("xyani-back", True),
    ("xy-back", False),
    ("ani-back", True),
)

_KEEP_WORD_HYPHENS = re.compile(r"[^a-z0-9-]+")
_COLLAPSE_HYPHENS = re.compile(r"-+")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _strip_diacritics(text: str) -> str:
    """NFD-normalize then drop combining marks, so Flabébé -> Flabebe."""
    return "".join(
        ch for ch in unicodedata.normalize("NFD", text)
        if not unicodedata.combining(ch)
    )


def normalize_species(species: str) -> str:
    """
    Showdown-style id: lowercase, diacritics stripped, every
    non-alphanumeric character removed. Mirrors Pokemon Showdown's own
    toId(): Nidoran-M -> nidoranm, Mr. Mime -> mrmime, Farfetch'd ->
    farfetchd, Ho-Oh -> hooh, Type: Null -> typenull.
    """
    return _NON_ALNUM.sub("", _strip_diacritics(species.lower()))


def sprite_id_candidates(species: str) -> list[str]:
    """
    Ordered sprite filenames (no extension) to try for a species:
      1. hyphens between words kept -- landorus-therian,
         arcanine-hisui, jangmo-o -- how the sprite folders name
         formes;
      2. fully collapsed -- landorustherian, nidoranm -- how they
         name everything else.
    Deduplicated; always at least one entry.
    """
    lowered = _strip_diacritics(species.lower())
    with_hyphens = _COLLAPSE_HYPHENS.sub("-", _KEEP_WORD_HYPHENS.sub("", lowered)).strip("-")
    collapsed = _NON_ALNUM.sub("", lowered)
    candidates = []
    for candidate in (with_hyphens, collapsed):
        if candidate and candidate not in candidates:
            candidates.append(candidate)
    return candidates


def _folders(back: bool, style: str = "gen5"):
    if style == "3d":
        return _BACK_FOLDERS_3D if back else _FRONT_FOLDERS_3D
    return _BACK_FOLDERS_GEN5 if back else _FRONT_FOLDERS_GEN5


def _extension(animated: bool) -> str:
    return "gif" if animated else "png"


def _default_downloader(url: str, timeout: float = 5.0):
    """requests-based downloader: URL -> bytes, or None on any failure."""
    try:
        response = requests.get(url, timeout=timeout)
    except Exception:
        return None
    if response.status_code != 200:
        return None
    return response.content


class SpriteStore:
    """
    Downloads sprites into {app_data}/sprites and resolves species ->
    local file. Missing URLs are remembered for the session so the GUI
    never re-attempts a 404/timeout while the app is open; files that
    did download persist on disk and are reused without any network
    call on later launches.

    base_dir is threaded through purely so tests can point this at a
    temp directory instead of touching the real app-data folder --
    real app code never passes it. downloader lets tests stub the
    network: any callable taking a URL and returning bytes or None.

    style: "gen5" (default) or "3d" -- selects the sprite set and
    encodes the style into cache filenames to avoid collisions.
    """

    def __init__(self, base_dir: Path | None = None, downloader=None, style: str = "gen5"):
        self.base_dir = base_dir
        self._downloader = downloader if downloader is not None else _default_downloader
        self._failed: set[tuple[str, bool, bool, str]] = set()
        self._style = style

    @property
    def style(self) -> str:
        return self._style

    def set_style(self, style: str):
        """Change sprite style and clear per-session failure cache."""
        if style != self._style:
            self._style = style
            self._failed.clear()

    @property
    def cache_dir(self) -> Path:
        d = config.get_app_data_dir(self.base_dir) / "sprites"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def path_for(self, sprite_id: str, back: bool = False, animated: bool = True, folder: str | None = None) -> Path:
        orientation = "back" if back else "front"
        if animated:
            # Include folder in filename to distinguish xyani vs ani, gen5ani vs ani
            kind = folder if folder else "ani"
        else:
            kind = "static"
        # Include style in filename to avoid gen5 vs 3d collisions
        return self.cache_dir / f"{sprite_id}-{orientation}-{kind}-{self._style}.{_extension(animated)}"

    # ---------------------------------------------------------- lookup

    def cached(self, species: str, back: bool = False):
        """
        Best sprite already on disk for this species -- never touches
        the network. Returns (Path, animated) or (None, None).
        """
        for folder, animated in _folders(back, self._style):
            for sprite_id in sprite_id_candidates(species):
                path = self.path_for(sprite_id, back, animated, folder)
                if path.exists():
                    return path, animated
        return None, None

    def cached_static(self, species: str, back: bool = False):
        """Best STATIC (non-animated) sprite on disk for this species --
        used by the GUI as a fallback when the preferred animated file
        can't actually be decoded on the platform. Returns (Path, False)
        or (None, None)."""
        for sprite_id in sprite_id_candidates(species):
            path = self.path_for(sprite_id, back, animated=False)
            if path.exists():
                return path, False
        return None, None

    def get_or_fetch(self, species: str, back: bool = False):
        """
        Best sprite for this species, downloading anything missing
        along the way. Returns (Path, animated) or (None, None) when
        no spelling exists in any folder.
        """
        for folder, animated in _folders(back, self._style):
            for sprite_id in sprite_id_candidates(species):
                path = self.path_for(sprite_id, back, animated, folder)
                if path.exists():
                    return path, animated
                key = (sprite_id, back, animated, folder, self._style)
                if key in self._failed:
                    continue
                url = (
                    f"{SPRITE_BASE_URL}/sprites/{folder}/{sprite_id}."
                    f"{_extension(animated)}"
                )
                data = self._download(url)
                if data is None:
                    self._failed.add(key)
                    continue
                try:
                    path.write_bytes(data)
                except OSError:
                    self._failed.add(key)
                    continue
                return path, animated
        return None, None

    def get_substitute(self, back: bool = False) -> Path:
        """
        Returns Path to local image file for the substitute doll sprite.
        Downloads from Showdown's substitute assets if missing, falling back to
        built-in official PNG bytes if offline or download is unavailable.
        """
        filename = f"substitute-{'back' if back else 'front'}.png"
        path = self.cache_dir / filename
        if path.exists():
            return path
        folder = "substitutes/gen5-back" if back else "substitutes/gen5"
        url = f"{SPRITE_BASE_URL}/sprites/{folder}/substitute.png"
        data = self._download(url)
        if not data or not data.startswith(b"\x89PNG"):
            data = base64.b64decode(SUBSTITUTE_BACK_B64 if back else SUBSTITUTE_FRONT_B64)
        try:
            path.write_bytes(data)
        except OSError:
            pass
        return path

    def _download(self, url: str):
        try:
            return self._downloader(url)
        except Exception:
            return None

