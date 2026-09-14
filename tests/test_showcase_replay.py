"""
Showcase-replay tests: prove that tests/sample_logs/game_showcase_all_animations.json
-- the synthetic singles battle used for manual visual verification -- really does
exercise every animation the Replays viewer can play:

events        switch / move / damage / heal / faint / sub{create+break} / mega /
              dynamax / popup{ability+item+message} / stat change {boost+unboost}dropdownback
conditions    weather / terrain / room / side conditions / board tint
fx kinds      fakeout / dash / projectile / beam / burst / nova / slam / self /
              protect (success shield AND failed wisp) / generic surge /
              earthquake / thunder / wave / psychic / flame_blast / barrage /
              frost

Run with: python -m pytest tests/test_showcase_replay.py -v
"""
import base64
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from fourslice.gui.replays_tab import _MOVE_FX, _STAT_NAMES, ReplaysTab
from fourslice.replay_timeline import build_timeline
from fourslice.sprites import SpriteStore
from fourslice.storage import import_replay, init_db

SAMPLE_DIR = Path(__file__).resolve().parent / "sample_logs"

# A real 1x1 PNG so cached "sprites" actually decode into a QPixmap.
_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

SHOWCASE = "game_showcase_all_animations.json"
REPLAY_URL = "https://replay.pokemonshowdown.com/gen9ou-9999999000"

# The concrete move the sample log uses to trigger each bespoke animation.
FIXED_FX = {
    "fakeout": "Fake Out",
    "dash": "Close Combat",     # U-turn / Bullet Punch also dash
    "projectile": "Moonblast",  # Shadow Ball / Fire Blast / Hex too
    "beam": "Hyper Voice",      # Heat Wave also beams
    "burst": "Overheat",
    "nova": "Hyper Beam",
    "slam": "Giga Impact",
    "self": "Swords Dance",
    "protect": "Protect",
    "generic": "Flower Trick",
    # the 2026 bespoke set pieces
    "earthquake": "Earthquake",
    "thunder": "Thunderbolt",
    "wave": "Hydro Pump",
    "psychic": "Psychic",
    "flame_blast": "Fire Blast",
    "barrage": "Surging Strikes",
    "frost": "Ice Beam",
}

REQUIRED_EVENTS = {"switch", "move", "damage", "heal", "faint", "mega",
                   "dynamax", "popup", "boost", "unboost"}


def load_log(filename):
    with open(SAMPLE_DIR / filename, encoding="utf-8") as f:
        return json.load(f)["log"]


def seed_store(tmp_dir):
    """A SpriteStore with fake-but-decodable art for every mon in the battle."""
    def downloader(url):
        if "gen5ani" in url or "/ani/" in url:
            return None
        return _PNG_BYTES

    store = SpriteStore(base_dir=Path(tmp_dir), downloader=downloader)
    for species in (
        "Meowscarada", "Sylveon", "Garchomp", "Lucario", "Lucario-Mega",
        "Gholdengo", "Snorlax", "Scizor", "Pelipper", "Iron Moth",
        "Gengar", "Corviknight", "Grimmsnarl",
    ):
        store.get_or_fetch(species, back=True)
        store.get_or_fetch(species, back=False)
    return store


def _event_kinds(tl):
    kinds = set()
    for fr in tl["frames"]:
        for ev in fr.get("events", []):
            kinds.add(ev[0])
    return kinds


def _popup_kinds(tl):
    kinds = set()
    for fr in tl["frames"]:
        for ev in fr.get("events", []):
            if ev[0] == "popup":
                kinds.add(ev[2])
    return kinds


def test_showcase_log_imports_and_covers_all_events():
    app = QApplication.instance() or QApplication(sys.argv)
    conn = init_db(":memory:")
    log = load_log(SHOWCASE)
    result = import_replay(conn, log, REPLAY_URL, my_usernames={"Showcase"})
    assert result["status"] == "imported", result
    assert result["battle_size"] == "singles", result

    row = conn.execute(
        "SELECT my_side, winner FROM games WHERE game_id = ?",
        (result["game_id"],),
    ).fetchone()
    assert row[0] == "p1", "Showcase is p1, so the board renders from p1"
    assert row[1] == "Showcase"

    tl = build_timeline(log)
    frames = tl["frames"]
    assert tl["winner"] == "Showcase"
    assert len(frames) == 28 and frames[0]["turn"] == 0 and frames[-1]["turn"] == 27
    # The known autoplay pitfall: the board must populate before turn 1.
    assert frames[0]["board"].get("p1a") == "Meowscarada"
    assert frames[0]["board"].get("p2a") == "Scizor"

    kinds = _event_kinds(tl)
    assert REQUIRED_EVENTS <= kinds, f"missing event kinds: {REQUIRED_EVENTS - kinds}"

    fx_by_move, sub_states, protect_evs = {}, set(), []
    for fr in frames:
        for ev in fr.get("events", []):
            if ev[0] == "move":
                fx_by_move[ev[4]] = _MOVE_FX.get(ev[4], "generic")
                if ev[4] == "Protect":
                    protect_evs.append(ev)
            elif ev[0] == "sub":
                sub_states.add(bool(ev[2]) if len(ev) > 2 else False)

    for kind, move in FIXED_FX.items():
        got = fx_by_move.get(move)
        assert got == kind, f"{move} should animate as {kind!r}, got {got!r}"
    assert sub_states == {True, False}, f"doll must come up AND break: {sub_states}"
    assert _popup_kinds(tl) == {"ability", "item", "message"}
    assert any(ev[-1] is True for ev in protect_evs), "expected a failed Protect"
    assert any(ev[-1] is not True for ev in protect_evs), "expected a live Protect"

    # Stat-stage changes: a raise (Swords Dance) AND a drop (Close Combat's
    # user-side Defense/Sp. Def hits) must both land as their own events.
    stat_evs = [ev for fr in frames for ev in fr.get("events", [])
                if ev[0] in ("boost", "unboost")]
    assert ("boost", "p1a", "atk", 2, "Lucario") in stat_evs, stat_evs
    assert ("unboost", "p1a", "def", 1, "Lucario-Mega") in stat_evs, stat_evs
    assert ("unboost", "p1a", "spd", 1, "Lucario-Mega") in stat_evs, stat_evs

    # The condition strip sees weather, terrain, a room and side chips.
    assert any(f.get("weather") for f in frames)
    assert any(f.get("terrain") for f in frames)
    assert any(f.get("room") for f in frames)
    assert any("Tailwind" in f.get("side_conditions", {}).get("p2", [])
               for f in frames)
    assert any("Reflect" in f.get("side_conditions", {}).get("p2", [])
               for f in frames)

    conn.close()
    print("PASS: showcase log's event/condition/FX coverage is complete")
def test_showcase_log_renders_every_frame_with_animations_on():
    """
    Full headless play-through with animate=True: step across all 27 turns,
    watch the conditions strip follow the weather/room, then prove that each
    FX class in the sample actually spawns its effect widget (and Protect
    raises a real shield) when the viewer plays the stored event.
    """
    app = QApplication.instance() or QApplication([])
    conn = init_db(":memory:")
    import_replay(conn, load_log(SHOWCASE), REPLAY_URL, my_usernames={"Showcase"})

    with tempfile.TemporaryDirectory() as tmp_dir:
        tab = ReplaysTab(
            conn, usernames={"Showcase"}, sprite_store=seed_store(tmp_dir),
            auto_fetch_sprites=False, animate=True,
        )

        # Frame 0 is the pre-battle entry; step through every turn. Frame
        # 27 (the win) repeats, so guard the loop rather than trusting an
        # exact step count.
        target = len(tab._frames)
        guard = 0
        seen_rain = seen_terrain = seen_room = False
        while tab._frame_index < target and guard < target + 1:
            guard += 1
            tab.step_forward()
            fr = tab._frame()
            assert fr is not None, "stepping must never land past the last frame"
            if fr.get("weather"):
                seen_rain = True
                assert "Rain" in tab.conditions_label.text(), \
                    tab.conditions_label.text()
            if fr.get("terrain"):
                seen_terrain = True
            if fr.get("room"):
                seen_room = True
                assert "Trick Room" in tab.conditions_label.text(), \
                    tab.conditions_label.text()
        assert seen_rain and seen_terrain and seen_room
        assert tab._frame()["turn"] == 27
        QTest.qWait(1200)  # let the last animations unwind

        # Every bespoke FX class the sample promises must both exist in the
        # stored timeline AND spawn its visible effect when played back. The
        # viewer animates against the CURRENT board, so land on the event's
        # own frame first -- otherwise the aim target isn't on the field.
        frame_of_kind = {}
        for idx, fr in enumerate(tab._frames):
            for ev in fr.get("events", []):
                if ev[0] == "move":
                    frame_of_kind.setdefault(_MOVE_FX.get(ev[4], "generic"), (idx, ev))
        assert set(FIXED_FX) <= set(frame_of_kind), \
            f"sample is missing animation classes: {set(FIXED_FX) - set(frame_of_kind)}"

        for kind, move in FIXED_FX.items():
            idx, ev = frame_of_kind[kind]
            assert _MOVE_FX.get(ev[4], "generic") == kind, \
                f"{kind} picked the wrong move: {ev[4]}"
            tab._frame_index = idx
            tab._event_index = 0
            tab._render_frame()  # land on the event's board (moves get played too)
            before = len(tab._effect_widgets)
            before_anims = len(tab._animations)
            tab._play_event_effect(ev)
            if kind == "protect":
                assert tab._shields.get(ev[2]), f"Protect shield didn't rise on {ev[2]}"
            else:
                # Orb/beam/burst/shield spawn a widget; dash/generic instead
                # bounce the attacker's sprite (a kept animation).
                spawned = (len(tab._effect_widgets) > before
                           or len(tab._animations) > before_anims)
                assert spawned, f"no effect spawned for {kind}={move}"
        QTest.qWait(900)

        # Stat changes float a colored badge ('+2 Attack', '-1 Defense'...)
        # over the mon when played back. The sample's Swords Dance (turn 11)
        # and Close Combat (turn 12) supply the boost + unboost beats.
        stat_evs = [
            (idx, ev) for idx, fr in enumerate(tab._frames)
            for ev in fr.get("events", []) if ev[0] in ("boost", "unboost")
        ]
        assert len(stat_evs) >= 3, f"expected boost+unboost beats: {stat_evs}"
        for idx, ev in stat_evs:
            tab._frame_index = idx
            tab._event_index = 0
            tab._render_frame()  # land on the event's board first
            before = len(tab._effect_widgets)
            tab._play_event_effect(ev)
            assert len(tab._effect_widgets) > before, f"no stat badge for {ev}"
            badge = tab._effect_widgets[-1]
            expect = _STAT_NAMES.get(ev[2], ev[2])
            assert badge.text().endswith(expect), f"{badge.text()} for {ev}"
        QTest.qWait(900)

        # The side chips render somewhere across the play-through.
        all_chips = set()
        for fr in tab._frames:
            for conds in fr.get("side_conditions", {}).values():
                all_chips.update(conds)
        assert {"Tailwind", "Reflect"} <= all_chips, sorted(all_chips)

        tab._stop_all_animations()

    conn.close()
    print("PASS: every frame renders + every animation class plays (28 turns)")


if __name__ == "__main__":
    test_showcase_log_imports_and_covers_all_events()
    test_showcase_log_renders_every_frame_with_animations_on()
    print("\nAll showcase-replay tests passed.")