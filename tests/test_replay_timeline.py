"""Run directly with: python tests/test_replay_timeline.py

Unit tests for fourslice/replay_timeline.py: the turn-by-turn timeline
the Replays tab renders. Focuses on forme changes -- |detailschange|
and old-style |mega| lines -- which the timeline must track so the
board shows Mega Evolution the turn it happens (HP untouched), plus
board-condition tracking (weather, terrain, rooms, per-side conditions)
and spread-move targets, and the pretty_species() display-name helper.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fourslice.replay_timeline import build_timeline, infer_game_type, pretty_species

_LOG = "\n".join([
    "|player|p1|Alice|user|1234",
    "|player|p2|Bob|user|2345",
    "|tier|[Gen 9] OU",
    "|switch|p1a: Charizard|Charizard, L50, M|100/100",
    "|switch|p2a: Blastoise|Blastoise, L50, M|100/100",
    "|turn|1",
    "|detailschange|p1a: Charizard|Charizard-Mega-Y, L50, M",
    "|-mega|p1a: Charizard|Charizard|Charizardite Y",
    "|move|p1a: Charizard|Heat Wave|p2a: Blastoise",
    "|-damage|p2a: Blastoise|80/100",
    "|turn|2",
    "|mega|p2a: Blastoise|Blastoise-Mega, L50, M",
    "|win|Alice",
])

# Weather, terrain, Trick Room, per-side conditions, and a spread move.
_COND_LOG = "\n".join([
    "|player|p1|Alice|user|1",
    "|player|p2|Bob|user|2",
    "|switch|p1a: Charizard|Charizard, L50, M|100/100",
    "|switch|p2a: Blastoise|Blastoise, L50, M|100/100",
    "|turn|1",
    "|weather|SunnyDay",
    "|terrain|GrassyTerrain",
    "|fieldactivate|trickroom",
    "|-sidestart|p2: Bob|move: Tailwind",
    "|sidestart|Side1|Spikes",
    "|move|p1a: Charizard|Flamethrower|p2a: Blastoise",
    "|-damage|p2a: Blastoise|80/100",
    "|turn|2",
    "|move|p1a: Charizard|Earthquake|p2a: Blastoise|[spread] p1a,p2a",
    "|-damage|p2a: Blastoise|60/100",
    "|-weather|SunnyDay|[upkeep]",
    "|fieldend|trickroom",
    "|sideend|Side1|Spikes",
    "|turn|3",
    "|weather|none",
    "|win|Alice",
])


def test_detailschange_swaps_the_slot_species_for_the_next_frame():
    frames = build_timeline(_LOG)["frames"]
    assert [f["turn"] for f in frames] == [0, 1, 2]

    # Turn 0 is the pre-battle entry: just the lead switch-ins.
    assert frames[0]["turn"] == 0
    assert frames[0]["board"]["p1a"] == "Charizard"
    assert frames[0]["events"] == [
        ("switch", "p1", "p1a", "Charizard", "100/100"),
        ("switch", "p2", "p2a", "Blastoise", "100/100"),
    ]

    # Turn 1: the detailschange updates p1a to the mega form.
    assert frames[1]["board"]["p1a"] == "Charizard-Mega-Y"
    # The |-mega| info line (which carries the BASE species) must not
    # revert it.
    assert frames[1]["board"]["p1a"] == "Charizard-Mega-Y"
    assert frames[1]["board"]["p2a"] == "Blastoise"

    # Turn 2: the old-style |mega| line mega-evolves p2a.
    assert frames[2]["board"]["p2a"] == "Blastoise-Mega"
    assert frames[2]["board"]["p1a"] == "Charizard-Mega-Y"

    # HP is untouched by either forme change.
    assert frames[0]["hp"]["p2a"] == "100/100"
    assert frames[1]["hp"]["p2a"] == "80/100"
    assert frames[2]["hp"]["p2a"] == "80/100"
    assert frames[2]["hp"]["p1a"] == "100/100"

    # The move line's mon field predates the mega; the board snapshot is
    # the authority for what's actually on the field.
    assert frames[1]["moves"][0][2] == "Charizard"

    # Forme changes are recorded per frame so the player can flash.
    assert frames[1]["megas"] == ["p1a"]
    assert frames[2]["megas"] == ["p2a"]


def test_mega_line_tolerates_bare_names_without_details():
    log = _LOG.replace("|mega|p2a: Blastoise|Blastoise-Mega, L50, M",
                       "|mega|p2a: Blastoise|Blastoise-Mega")
    frames = build_timeline(log)["frames"]
    assert frames[2]["board"]["p2a"] == "Blastoise-Mega"


def test_pretty_species_formats_formes_for_display():
    assert pretty_species("Blastoise-Mega") == "Mega Blastoise"
    assert pretty_species("Charizard-Mega-X") == "Mega Charizard X"
    assert pretty_species("Charizard-Mega-Y") == "Mega Charizard Y"
    assert pretty_species("Groudon-Primal") == "Primal Groudon"
    assert pretty_species("Sneasler") == "Sneasler"
    assert pretty_species("Rotom-Wash") == "Rotom-Wash"


def test_board_conditions_and_move_targets_track_across_frames():
    """Weather, terrain, rooms, and per-side conditions persist across
    frame boundaries until the log clears them, and spread moves record
    every target they hit."""
    frames = build_timeline(_COND_LOG)["frames"]
    assert [f["turn"] for f in frames] == [0, 1, 2, 3]

    # Turn 0 (the pre-battle entry) has nothing up yet.
    assert frames[0]["weather"] is None
    assert frames[0]["side_conditions"] == {}

    # Spread moves carry the full target list; the nominal target stays
    # in the 5th field for backward compatibility.
    eq = [m for m in frames[2]["moves"] if m[3] == "Earthquake"][0]
    assert eq[4] == "p2a"
    assert eq[5] == ["p1a", "p2a"]

    # Single-target moves get a one-element list.
    fl = [m for m in frames[1]["moves"] if m[3] == "Flamethrower"][0]
    assert fl[5] == ["p2a"]

    # Weather/terrain/room snapshot into every frame until they change.
    assert frames[1]["weather"] == "Harsh sunlight"
    assert frames[1]["terrain"] == "Grassy Terrain"
    assert frames[1]["room"] == "Trick Room"
    assert frames[2]["weather"] == "Harsh sunlight"  # upkeep restates it
    assert frames[2]["room"] is None                 # |fieldend|trickroom
    assert frames[3]["weather"] is None              # |weather|none
    assert frames[3]["terrain"] == "Grassy Terrain"  # terrain persists

    # Per-side conditions track across frames (both the Side1 and
    # pX: name forms of sidestart/sideend).
    assert frames[1]["side_conditions"] == {"p1": ["Spikes"], "p2": ["Tailwind"]}
    assert frames[2]["side_conditions"] == {"p2": ["Tailwind"]}
    assert frames[3]["side_conditions"] == {"p2": ["Tailwind"]}


def test_events_record_battle_order():
    """The per-frame 'events' list interleaves switches, megas, moves,
    damage, heals, and faints in the exact order the log records them, so
    the player can step through one action at a time."""
    frames = build_timeline(_LOG)["frames"]

    # Frame 0 is the pre-battle entry: only the lead switch-ins.
    assert [e[0] for e in frames[0]["events"]] == ["switch", "switch"]

    # Frame 1: the mega, the move, then its damage -- in log order.
    events = frames[1]["events"]
    kinds = [e[0] for e in events]
    assert kinds == ["mega", "move", "damage"]
    assert events[0][0] == "mega" and events[0][1] == "p1a"
    move_ev = [e for e in events if e[0] == "move"][0]
    # (kind, side, slot, mon, move, target, target_mon, targets, target_species)
    assert move_ev[2] == "p1a" and move_ev[3] == "Charizard"
    assert move_ev[4] == "Heat Wave"
    assert move_ev[5] == "p2a" and move_ev[6] == "Blastoise"
    assert move_ev[7] == ["p2a"]
    assert move_ev[8] == {"p2a": "Blastoise"}  # species captured at move time
    # The damage event follows its move, with the post-damage HP and the
    # species that was standing in the slot AT the damage line.
    assert events[2] == ("damage", "p2a", "80/100", "Blastoise")

    # Frame 2 has only the mega (the HP carries over in the snapshot).
    assert [e[0] for e in frames[2]["events"]] == ["mega"]

    cond = build_timeline(_COND_LOG)["frames"]
    assert [e[0] for e in cond[0]["events"]] == ["switch", "switch"]
    assert [e[0] for e in cond[1]["events"]] == ["move", "damage"]
    assert [e[0] for e in cond[2]["events"]] == ["move", "damage"]
    assert cond[2]["events"][1] == ("damage", "p2a", "60/100", "Blastoise")


def test_timed_conditions_count_down_and_expire():
    """Weather/terrain/rooms and timed side conditions count down per turn
    (weather_remaining etc.), and weather ends naturally the first turn
    the log stops mentioning it."""
    frames = build_timeline(_COND_LOG)["frames"]

    # Turn 0 (pre-battle entry) has no conditions yet.
    assert frames[0]["weather_remaining"] is None
    assert "side_condition_remaining" not in frames[0]

    assert frames[1]["weather_remaining"] == 5
    assert frames[1]["terrain_remaining"] == 5
    assert frames[1]["room_remaining"] == 5
    assert frames[1]["side_condition_remaining"] == {"p2": {"Tailwind": 4}}

    # Upkeep keeps the sun going; Trick Room is cleared by fieldend; the
    # terrain and Tailwind tick down.
    assert frames[2]["weather_remaining"] == 4
    assert frames[2]["terrain_remaining"] == 4
    assert frames[2]["room_remaining"] is None
    assert frames[2]["side_condition_remaining"] == {"p2": {"Tailwind": 3}}

    # |weather|none clears the sun; the terrain persists and ticks down,
    # as does the still-uncleared Tailwind.
    assert frames[3]["weather_remaining"] is None
    assert frames[3]["terrain_remaining"] == 3
    assert frames[3]["side_condition_remaining"] == {"p2": {"Tailwind": 2}}

    # Weather that simply stops being mentioned expires on its own the
    # next turn (still active through the turn it's last mentioned).
    no_clear = _COND_LOG.replace("|weather|none\n", "")
    no_clear = no_clear.replace("|win|Alice", "|turn|4\n|win|Alice")
    frames = build_timeline(no_clear)["frames"]
    assert frames[3]["weather"] == "Harsh sunlight"  # active through turn 3
    assert frames[4]["weather"] is None              # turn 4: no mention -> gone


def test_showteam_items_extend_weather_and_terrain():
    """|showteam| reveals held items, so a Heat Rock setter's weather and
    a Terrain Extender setter's terrain last 8 turns instead of 5."""
    ext_log = "\n".join([
        "|player|p1|Alice|user|1",
        "|player|p2|Bob|user|2",
        "|showteam|p1|Torkoal||Heat Rock|Drought|SunnyDay|Quiet||F|||50|",
        "|showteam|p1|Indeedee||Terrain Extender|Psychic Surge|TrickRoom|Quiet||F|||50|",
        "|switch|p1a: Torkoal|Torkoal, L50, F|100/100",
        "|switch|p2a: Indeedee|Indeedee, L50, F|100/100",
        "|turn|1",
        "|-weather|SunnyDay|[from] ability: Drought|[of] p1a: Torkoal",
        "|terrainstart|PsychicTerrain|[of] p2a: Indeedee",
        "|turn|2",
        "|-weather|SunnyDay|[upkeep]",
        "|turn|3",
        "|-weather|SunnyDay|[upkeep]",
        "|turn|4",
        "|-weather|SunnyDay|[upkeep]",
        "|turn|5",
        "|-weather|SunnyDay|[upkeep]",
        "|turn|6",
        "|-weather|SunnyDay|[upkeep]",
        "|win|Alice",
    ])
    frames = build_timeline(ext_log)["frames"]
    # Turn 0 is the pre-battle entry (no weather yet); then the base-5
    # weather would reach "1 left" at turn 5, but the extended weather
    # skips the expiry and keeps counting from 8 instead.
    assert [f["weather_remaining"] for f in frames] == [None, 8, 7, 6, 5, 4, 3]
    assert [f["terrain_remaining"] for f in frames] == [None, 8, 7, 6, 5, 4, 3]

    # No showteam (game3-style logs): no item info, so the base duration
    # stays 5. The synthetic log keeps mentioning the weather through its
    # last turn (it would naturally fade the turn after the log stops
    # mentioning it), while the 5-turn terrain ticks to zero and ends at
    # turn 6.
    no_team = ext_log.replace("|showteam|p1|Torkoal||Heat Rock|Drought|SunnyDay|Quiet||F|||50|\n", "")
    no_team = no_team.replace("|showteam|p1|Indeedee||Terrain Extender|Psychic Surge|TrickRoom|Quiet||F|||50|\n", "")
    frames = build_timeline(no_team)["frames"]
    assert [f["weather_remaining"] for f in frames] == [None, 5, 4, 3, 2, 1, 1]
    assert [f["terrain_remaining"] for f in frames] == [None, 5, 4, 3, 2, 1, None]


def test_tera_dynamax_and_protect_fail():
    """|terastallize| / |dynamax| lines become 'tera' / 'dynamax' events, and
    a protect that a |fail| line reports never activated is flagged with a
    trailing True on its move event (so the player shows a break, not a
    shield). A protect with no fail line stays unflagged."""
    log = "\n".join([
        "|player|p1|Alice|user|1",
        "|player|p2|Bob|user|2",
        "|switch|p1a: Corviknight|Corviknight, L50, M|100/100",
        "|switch|p2a: Grimmsnarl|Grimmsnarl, L50, M|100/100",
        "|turn|1",
        "|dynamax|p2a: Grimmsnarl|Grimmsnarl",
        "|terastallize|p1a: Corviknight|Flying",
        "|move|p1a: Corviknight|Body Press|p2a: Grimmsnarl",
        "|-damage|p2a: Grimmsnarl|60/100",
        "|turn|2",
        "|move|p2a: Grimmsnarl|Protect|p2a: Grimmsnarl",
        "|fail|p2a: Grimmsnarl|move|Protect",
        "|turn|3",
        "|move|p1a: Corviknight|Protect|p1a: Corviknight",
        "|win|Alice",
    ])
    frames = build_timeline(log)["frames"]
    # Turn 0 is the pre-battle entry; the dynamax/tera are on turn 1.
    assert frames[0]["turn"] == 0
    events1 = frames[1]["events"]
    assert ("dynamax", "p2a", "Grimmsnarl") in events1
    assert ("tera", "p1a", "Corviknight", "Flying") in events1

    # The failed protect keeps its combat shape plus a trailing True flag
    # (now sitting behind the move-time target_species map at index 8).
    failed = [e for e in frames[2]["events"] if e[0] == "move"][0]
    assert failed[4] == "Protect"
    assert failed[8] == {"p2a": "Grimmsnarl"}
    assert failed[9] is True

    # The moves entry gets the same trailing flag so the whole-turn render
    # (step_forward / re-render, which reads frame["moves"]) also shows a
    # break instead of a shield -- not just the per-event autoplay path.
    failed_mv = [m for m in frames[2]["moves"] if m[3] == "Protect"][0]
    assert len(failed_mv) == 8 and failed_mv[7] is True, failed_mv

    # A protect that never meets a fail line stays the plain 9-tuple.
    ok = [e for e in frames[3]["events"] if e[0] == "move"][0]
    assert len(ok) == 9
    # ...and its moves entry keeps the plain 7 fields, no flag attached.
    ok_mv = [m for m in frames[3]["moves"] if m[3] == "Protect"][0]
    assert len(ok_mv) == 7, ok_mv


def test_turn_zero_substitute_targets_and_popups():
    """Items A/D/E/F/G on the backlog: the pre-battle 'turn 0' frame; an
    immune partner counted in a move's targets with its move-time species;
    a bare |fail| line (no move token) still flagging the active protect;
    Substitute dolls tracked per event; and item/ability reveals surfacing
    as popup events."""
    log = "\n".join([
        "|player|p1|Alice|user|1",
        "|player|p2|Bob|user|2",
        "|switch|p1a: Torkoal|Torkoal, L50, F|100/100",
        "|switch|p2a: Corviknight|Corviknight, L50, M|100/100",
        "|switch|p2b: Garchomp|Garchomp, L50, M|100/100",
        "|turn|1",
        "|move|p2b: Garchomp|Earthquake|p1a: Torkoal|[spread] p1a",
        "|-immune|p2a: Corviknight",
        "|-damage|p1a: Torkoal|80/100",
        "|-ability|p2a: Corviknight|Mirror Armor",
        "|move|p2a: Corviknight|Protect|p2a: Corviknight",
        "|fail|p2a: Corviknight",
        "|move|p1a: Torkoal|Substitute|p1a: Torkoal",
        "|-damage|p1a: Torkoal|75/100|[from] submission",
        "|-substitute|p1a: Torkoal|75/100",
        "|move|p2a: Corviknight|Iron Head|p1a: Torkoal",
        "|-damage|p1a: Torkoal|75/100",
        "|turn|2",
        "|-substitute|p1a: Torkoal|",
        "|-heal|p2a: Corviknight|100/100|[from] item: Leftovers",
        "|win|Alice",
    ])
    frames = build_timeline(log)["frames"]

    # (A) Turn 0 is the pre-battle entry carrying the leads.
    assert [f["turn"] for f in frames] == [0, 1, 2]
    assert [e[0] for e in frames[0]["events"]] == ["switch", "switch", "switch"]
    assert frames[0]["moves"] == []

    # (F + D) The immune partner is folded into the Earthquake's targets,
    # and the species map records what was in each slot AT MOVE TIME.
    eq = [m for m in frames[1]["moves"] if m[3] == "Earthquake"][0]
    assert eq[5] == ["p1a", "p2a"]
    assert eq[6] == {"p1a": "Torkoal", "p2a": "Corviknight"}
    move_ev = [e for e in frames[1]["events"] if e[0] == "move"][0]
    assert move_ev[8] == {"p1a": "Torkoal", "p2a": "Corviknight"}

    # (B) A bare |fail| line (no move token) still flags the active protect.
    failed = [e for e in frames[1]["events"]
              if e[0] == "move" and e[4] == "Protect"][0]
    assert failed[-1] is True

    # (E) The substitute comes up (and survives in the snapshot)...
    assert ("sub", "p1a", True) in frames[1]["events"]
    assert frames[1]["substitutes"] == {"p1a": True}
    # ...and breaks the next turn, leaving the frame doll-free.
    assert ("sub", "p1a", False) in frames[2]["events"]
    assert frames[2].get("substitutes") in (None, {})

    # (G) Ability reveals are popup events, not moves, carrying the mon
    # that was in the slot at reveal time.
    assert ("popup", "p2a", "ability", "Mirror Armor", "Corviknight") in frames[1]["events"]
    # ...and so are item reveals carried on damage/heal lines (Leftovers).
    assert ("popup", "p2a", "item", "Leftovers", "Corviknight") in frames[2]["events"]


def test_flip_turn_damage_names_and_of_attribution():
    """A move that switches its user out mid-turn (Flip Turn) must keep the
    DAMAGED mon as the species that took the hit (Basculegion, not the
    replacement Pyroar), and a Rough Skin-style [from] ability line must be
    credited to its [of] OWNER (Garchomp), not to whoever absorbed the
    recoil -- the two lines together used to read 'Pyroar's Rough Skin did
    damage to Pyroar'."""
    log = "\n".join([
        "|player|p1|Alice|user|1",
        "|player|p2|Bob|user|2",
        "|switch|p1a: Basculegion|Basculegion, L50, M|100/100",
        "|switch|p1b: Whimsicott|Whimsicott, L50, F|100/100",
        "|switch|p2a: Garchomp|Garchomp, L50, M|100/100",
        "|switch|p2b: Sylveon|Sylveon, L50, F|100/100",
        "|turn|1",
        "|move|p1a: Basculegion|Flip Turn|p2a: Garchomp",
        "|-damage|p2a: Garchomp|64/100",
        "|-damage|p1a: Basculegion|96/100|[from] ability: Rough Skin|[of] p2a: Garchomp",
        "|switch|p1a: Pyroar|Pyroar, L50, F, shiny|100/100|[from] Flip Turn",
        "|turn|2",
        "|win|Alice",
    ])
    frames = build_timeline(log)["frames"]

    # The damage events remember the mon taking the hit at THAT moment,
    # so the folded movelog can't blame Pyroar for Basculegion's damage.
    events1 = [e for e in frames[1]["events"] if e[0] in ("move", "damage", "popup")]
    assert ("damage", "p2a", "64/100", "Garchomp") in events1
    assert ("damage", "p1a", "96/100", "Basculegion") in events1
    # The Rough Skin popup is attributed to Garchomp's slot (the [of]
    # owner), with Garchomp as its subject of activation.
    assert ("popup", "p2a", "ability", "Rough Skin", "Garchomp") in frames[1]["events"]
    # Pyroar lands in that slot later that same turn (board end state), but
    # the damage event's species was locked in before the switch.
    assert frames[1]["board"]["p1a"] == "Pyroar"


def test_boost_and_unboost_become_stat_events():
    """|-boost| / |-unboost| lines (Swords Dance raising Attack, Close
    Combat dropping the user's Defense...) become their own events carrying
    slot, stat, stage count, and the mon at that moment, so the player can
    narrate them and float a stat badge. The lines themselves never touch
    HP or the board."""
    log = "\n".join([
        "|player|p1|Alice|user|1",
        "|player|p2|Bob|user|2",
        "|switch|p1a: Lucario|Lucario, L50, M|100/100",
        "|switch|p2a: Scizor|Scizor, L50, M|100/100",
        "|turn|1",
        "|move|p1a: Lucario|Swords Dance|p1a: Lucario",
        "|-boost|p1a: Lucario|atk|2",
        "|move|p2a: Scizor|Close Combat|p1a: Lucario",
        "|-damage|p1a: Lucario|60/100",
        "|-unboost|p2a: Scizor|def|1",
        "|-unboost|p2a: Scizor|spd|1|[silent]",
        "|turn|2",
        "|win|Alice",
    ])
    frames = build_timeline(log)["frames"]
    events = [ev for fr in frames for ev in fr.get("events", [])]

    boosts = [ev for ev in events if ev[0] == "boost"]
    unboosts = [ev for ev in events if ev[0] == "unboost"]
    assert boosts == [("boost", "p1a", "atk", 2, "Lucario")], boosts
    # A trailing |[silent]| tag is narration, not a stat.
    assert unboosts == [
        ("unboost", "p2a", "def", 1, "Scizor"),
        ("unboost", "p2a", "spd", 1, "Scizor"),
    ], unboosts

    # Stat lines don't move HP or the board.
    assert frames[1]["hp"]["p1a"] == "60/100"
    assert frames[1]["board"]["p1a"] == "Lucario"
    assert frames[1]["board"]["p2a"] == "Scizor"


def test_hazard_layers_stack_to_their_caps():
    """Spikes and Toxic Spikes pile up a layer per |sidestart| for the
    same side (capped at 3 and 2). side_condition_layers carries the
    depth while side_conditions still lists the hazard just once, and a
    sideend strips both the hazard and its stack."""
    log = "\n".join([
        "|player|p1|Alice|user|1",
        "|player|p2|Bob|user|2",
        "|switch|p1a: Ferrothorn|Ferrothorn, L50, M|100/100",
        "|switch|p2a: Garchomp|Garchomp, L50, M|100/100",
        "|turn|1",
        "|move|p2a: Garchomp|Spikes|p1a: Ferrothorn",
        "|-sidestart|p1: Alice|Spikes",
        "|move|p1a: Ferrothorn|Stealth Rock|p2a: Garchomp",
        "|-sidestart|p2: Bob|Stealth Rock",
        "|turn|2",
        "|move|p2a: Garchomp|Spikes|p1a: Ferrothorn",
        "|-sidestart|p1: Alice|Spikes",
        "|move|p1a: Ferrothorn|Toxic Spikes|p2a: Garchomp",
        "|-sidestart|p2: Bob|Toxic Spikes",
        "|turn|3",
        "|move|p2a: Garchomp|Spikes|p1a: Ferrothorn",
        "|-sidestart|p1: Alice|Spikes",
        "|move|p1a: Ferrothorn|Spikes|p2a: Garchomp",
        "|-sidestart|p2: Bob|Spikes",
        "|-sidestart|p2: Bob|Spikes",
        "|turn|4",
        "|move|p2a: Garchomp|Spikes|p1a: Ferrothorn",
        "|-sidestart|p1: Alice|Spikes",
        "|sideend|Side2|Spikes",
        "|win|Alice",
    ])
    frames = build_timeline(log)["frames"]
    assert [f["turn"] for f in frames] == [0, 1, 2, 3, 4]

    # One layer from the first line; Stealth Rock never layers.
    assert frames[1]["side_conditions"] == {"p1": ["Spikes"], "p2": ["Stealth Rock"]}
    assert frames[1].get("side_condition_layers", {}).get("p1", {}).get("Spikes") == 1
    assert "Stealth Rock" not in frames[1].get("side_condition_layers", {}).get("p2", {})

    # A second sidestart deepens p1's Spikes; Toxic Spikes starts at 1.
    assert frames[2]["side_conditions"]["p1"] == ["Spikes"]
    assert frames[2]["side_condition_layers"]["p1"] == {"Spikes": 2}
    assert frames[2]["side_condition_layers"]["p2"] == {"Toxic Spikes": 1}

    # Spikes cap at 3 even with a fourth line; the opponent's pile stacks
    # twice within the same turn on top of its Stealth Rock.
    assert frames[3]["side_condition_layers"]["p1"] == {"Spikes": 3}
    assert frames[3]["side_conditions"]["p2"] == ["Spikes", "Stealth Rock", "Toxic Spikes"]
    assert frames[3]["side_condition_layers"]["p2"] == {"Spikes": 2, "Toxic Spikes": 1}

    # sideend clears the hazard AND its layers; the other hazards remain.
    assert frames[4]["side_conditions"] == {"p1": ["Spikes"], "p2": ["Stealth Rock", "Toxic Spikes"]}
    assert frames[4]["side_condition_layers"] == {"p1": {"Spikes": 3}, "p2": {"Toxic Spikes": 1}}


def test_hazard_sidestart_honors_explicit_layer_token():
    """Some logs spell the layer count out on the sidestart line
    (|Spikes|2); that depth wins, and a later bare line still stacks on
    top of it."""
    log = "\n".join([
        "|player|p1|Alice|user|1",
        "|player|p2|Bob|user|2",
        "|switch|p1a: Ferrothorn|Ferrothorn, L50, M|100/100",
        "|switch|p2a: Garchomp|Garchomp, L50, M|100/100",
        "|turn|1",
        "|-sidestart|p1: Alice|Spikes|2",
        "|-sidestart|p2: Bob|Toxic Spikes|1",
        "|-sidestart|p2: Bob|Toxic Spikes",
        "|win|Alice",
    ])
    frames = build_timeline(log)["frames"]
    assert frames[1]["side_condition_layers"] == {
        "p1": {"Spikes": 2},
        "p2": {"Toxic Spikes": 2},
    }


def test_gametype_capture_and_inference():
    """build_timeline captures the declared gametype, and infer_game_type
    correctly infers singles vs doubles from which slots were used."""
    # Explicit doubles declaration ------------------------------------------------
    doubles_log = "\n".join([
        "|gametype|Doubles",
        "|player|p1|Alice|user|1",
        "|player|p2|Bob|user|2",
        "|switch|p1a: Charizard|Charizard, L50, M|100/100",
        "|switch|p1b: Venusaur|Venusaur, L50, M|100/100",
        "|switch|p2a: Blastoise|Blastoise, L50, M|100/100",
        "|switch|p2b: Rhydon|Rhydon, L50, M|100/100",
        "|turn|1",
        "|win|Alice",
    ])
    result = build_timeline(doubles_log)
    assert result["gametype"] == "doubles"
    assert infer_game_type(result["frames"]) == "doubles"

    # Explicit singles declaration ------------------------------------------------
    singles_log = "\n".join([
        "|gametype|Singles",
        "|player|p1|Alice|user|1",
        "|player|p2|Bob|user|2",
        "|switch|p1a: Charizard|Charizard, L50, M|100/100",
        "|switch|p2a: Blastoise|Blastoise, L50, M|100/100",
        "|turn|1",
        "|win|Alice",
    ])
    result = build_timeline(singles_log)
    assert result["gametype"] == "singles"
    assert infer_game_type(result["frames"]) == "singles"

    # Missing gametype line -- infer_game_type must still work ---------------------
    no_gt_log = "\n".join([
        "|player|p1|Alice|user|1",
        "|player|p2|Bob|user|2",
        "|switch|p1a: Charizard|Charizard, L50, M|100/100",
        "|switch|p2a: Blastoise|Blastoise, L50, M|100/100",
        "|turn|1",
        "|win|Alice",
    ])
    result = build_timeline(no_gt_log)
    assert result["gametype"] == ""
    assert infer_game_type(result["frames"]) == "singles"

    # Missing gametype but b-slots used → doubles ----------------------------------
    no_gt_doubles = "\n".join([
        "|player|p1|Alice|user|1",
        "|player|p2|Bob|user|2",
        "|switch|p1a: Charizard|Charizard, L50, M|100/100",
        "|switch|p1b: Venusaur|Venusaur, L50, M|100/100",
        "|switch|p2a: Blastoise|Blastoise, L50, M|100/100",
        "|switch|p2b: Rhydon|Rhydon, L50, M|100/100",
        "|turn|1",
        "|win|Alice",
    ])
    result = build_timeline(no_gt_doubles)
    assert result["gametype"] == ""
    assert infer_game_type(result["frames"]) == "doubles"


if __name__ == "__main__":
    test_detailschange_swaps_the_slot_species_for_the_next_frame()
    test_mega_line_tolerates_bare_names_without_details()
    test_pretty_species_formats_formes_for_display()
    test_board_conditions_and_move_targets_track_across_frames()
    test_events_record_battle_order()
    test_timed_conditions_count_down_and_expire()
    test_showteam_items_extend_weather_and_terrain()
    test_tera_dynamax_and_protect_fail()
    test_turn_zero_substitute_targets_and_popups()
    test_flip_turn_damage_names_and_of_attribution()
    test_boost_and_unboost_become_stat_events()
    test_hazard_layers_stack_to_their_caps()
    test_hazard_sidestart_honors_explicit_layer_token()
    test_gametype_capture_and_inference()
    print("\nAll replay-timeline tests passed.")
