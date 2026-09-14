"""
fourslice/config.py

Fourslice's persistent settings (Showdown usernames, log-storage
options, gen-5 sprite display) and where its data lives on disk.
Nothing here assumes any specific
person -- on first launch the app asks for a username instead of
defaulting to one, and everything is stored in the OS's standard
per-app data location (via platformdirs) rather than inside the
project/install folder itself. That matters for two reasons: a
public release shouldn't ship with anyone's personal data baked in,
and an installed app's own folder may not even be writable.

base_dir is threaded through every function below purely so tests
can point this at a temp directory instead of touching the real
config -- real app code should never pass it.
"""

import json
from pathlib import Path

import platformdirs

APP_NAME = "Fourslice"


def get_app_data_dir(base_dir: Path | None = None) -> Path:
    d = base_dir if base_dir is not None else Path(platformdirs.user_data_dir(APP_NAME, appauthor=False))
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_shipped_data_dir() -> Path:
    """Read-only directory of the Showdown data files bundled with the app
    (formats.js, pokedex.js, learnsets.ts, ...). These seed the writable
    runtime data dir on first launch. Resolution order:
      * ``sys._MEIPASS/data`` (PyInstaller bundle)
      * ``<package>/data`` via importlib.resources (works for wheels and, once
        package data is declared, for PyInstaller bundles),
      * ``<package>/data`` on disk (source checkout with data inside the pkg),
      * the repo's ``data/`` sibling of the package (current source layout).
    Nothing in the app ever *writes* here -- it is the frozen bootstrap copy.
    """
    # PyInstaller: data files are extracted to sys._MEIPASS
    import sys
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            d = Path(meipass) / "data"
            if d.is_dir():
                return d
    try:
        from importlib.resources import files as _res_files
        res = _res_files("fourslice") / "data"
        if res.is_dir():
            return Path(str(res))
    except Exception:
        pass
    pkg = Path(__file__).resolve().parent
    if (pkg / "data").is_dir():
        return pkg / "data"
    return pkg.parent / "data"


def get_showdown_data_dir(base_dir: Path | None = None) -> Path:
    """Writable per-user directory for the downloaded Showdown JS data files
    (formats.js, formats-data.js, pokedex.js, moves.js, abilities.js,
    items.js, learnsets.ts). Lives under app-data so an installed app's own
    folder (possibly read-only, or a PyInstaller temp dir) is never written
    to. ``base_dir`` is the *app-data* root for tests."""
    d = get_app_data_dir(base_dir) / "showdown"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_pokemon_db_path(base_dir: Path | None = None) -> Path:
    """Writable runtime pokemon_complete.db rebuilt from refreshed pokedex.js.
    The shipped file stays as a read-only fallback."""
    return get_app_data_dir(base_dir) / "pokemon_complete.db"


def get_config_path(base_dir: Path | None = None) -> Path:
    return get_app_data_dir(base_dir) / "config.json"


def get_db_path(base_dir: Path | None = None) -> Path:
    return get_app_data_dir(base_dir) / "fourslice.db"


def get_stats_db_path(base_dir: Path | None = None) -> Path:
    """SQLite store for external (Smogon/Champions) stat data -- kept
    separate from fourslice.db so the replay database stays untouched,
    but in the same app-data folder."""
    return get_app_data_dir(base_dir) / "stats.db"


def get_learnsets_db_path(base_dir: Path | None = None) -> Path:
    """SQLite store of parsed learnset legality -- one table per
    generation (gen_1 .. gen_9), each holding every legal (species,
    move) pair for that generation."""
    return get_app_data_dir(base_dir) / "learnsets.db"


def load_config(base_dir: Path | None = None) -> dict:
    path = get_config_path(base_dir)
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        # Corrupt or unreadable config -- start fresh
        return {}


def save_config(cfg: dict, base_dir: Path | None = None) -> None:
    try:
        with open(get_config_path(base_dir), "w") as f:
            json.dump(cfg, f, indent=2)
    except OSError:
        # Config directory may not be writable (e.g., read-only install)
        pass


DEFAULT_LOG_RETENTION_DAYS = 7


def get_store_logs(base_dir: Path | None = None) -> bool:
    """Whether raw replay logs are persisted for the in-app replay player."""
    return load_config(base_dir).get("store_logs", True)


def set_store_logs(value: bool, base_dir: Path | None = None) -> None:
    cfg = load_config(base_dir)
    cfg["store_logs"] = value
    save_config(cfg, base_dir)


def get_log_retention_days(base_dir: Path | None = None) -> int:
    """
    How many days a stored raw log is kept before pruning. 0 means
    keep forever.
    """
    return int(load_config(base_dir).get("log_retention_days", DEFAULT_LOG_RETENTION_DAYS))


def set_log_retention_days(days: int, base_dir: Path | None = None) -> None:
    cfg = load_config(base_dir)
    cfg["log_retention_days"] = max(0, int(days))
    save_config(cfg, base_dir)


def get_theme(base_dir: Path | None = None) -> str:
    """The app theme: 'light' or 'dark'. Persisted so the choice survives
    restarting the app."""
    return load_config(base_dir).get("theme", "light")


def set_theme(value: str, base_dir: Path | None = None) -> None:
    cfg = load_config(base_dir)
    cfg["theme"] = "dark" if value == "dark" else "light"
    save_config(cfg, base_dir)


def get_use_sprites(base_dir: Path | None = None) -> bool:
    """Whether the Replays tab fetches and shows Pokemon Showdown's
    gen-5 sprites. Sprites are cached on disk, so turning this off
    only stops new fetches (already-cached ones stay cached)."""
    return load_config(base_dir).get("use_sprites", True)


def set_use_sprites(value: bool, base_dir: Path | None = None) -> None:
    cfg = load_config(base_dir)
    cfg["use_sprites"] = value
    save_config(cfg, base_dir)


def get_animate_board(base_dir: Path | None = None) -> bool:
    """Whether the Replays board plays Showdown-lite animations (attack
    surges, damage flashes, HP drain, faint fade-outs, switch fade-ins,
    mega flashes, idle bobbing). Purely cosmetic -- playback works the
    same with it off."""
    return load_config(base_dir).get("animate_board", True)


def set_animate_board(value: bool, base_dir: Path | None = None) -> None:
    cfg = load_config(base_dir)
    cfg["animate_board"] = value
    save_config(cfg, base_dir)


def get_sprite_style(base_dir: Path | None = None) -> str:
    """Sprite style for the Replays tab: 'gen5' (gen-5 animated/static) or '3d' (XY/current-gen 3D models)."""
    return load_config(base_dir).get("sprite_style", "gen5")


def set_sprite_style(value: str, base_dir: Path | None = None) -> None:
    cfg = load_config(base_dir)
    cfg["sprite_style"] = value
    save_config(cfg, base_dir)


def get_usernames(base_dir: Path | None = None) -> list[str]:
    """Empty list means nobody's been set up yet -- callers should treat that as "first run"."""
    return load_config(base_dir).get("usernames", [])


def set_usernames(usernames: list[str], base_dir: Path | None = None) -> None:
    cfg = load_config(base_dir)
    cfg["usernames"] = usernames
    save_config(cfg, base_dir)

def get_last_synced(base_dir: Path | None = None) -> str | None:
    """ISO timestamp of the last completed sync, or None if never synced."""
    return load_config(base_dir).get("last_synced")


def set_last_synced(timestamp: str, base_dir: Path | None = None) -> None:
    cfg = load_config(base_dir)
    cfg["last_synced"] = timestamp
    save_config(cfg, base_dir)