"""
fourslice/replay_timeline.py

Builds a turn-by-turn timeline from a stored raw battle log -- the
data the Replays tab's native player renders. One lightweight pass
over the protocol lines, producing a flat list of "frames":

  Turn number, player names/winner, and for each frame:
    - moves: [(side, slot, mon, move, target_slot, [target_slots])] -- what
      was used this turn, with the full spread-target list when a move hit
      more than the nominal target
    - hp:    slot -> "cur/max" as of the END of that turn's actions
    - faints_this_turn: [slot] -- slots whose mon fainted during the turn
    - ko_credit: slot -> mon for each faint, when determinable
    - switches: [(side, slot, mon)] -- re-entries mid-/post-turn
    - events: [(kind, ...)] -- the battle actions this turn, in the exact
      order the log records them (switch / move / damage / heal / faint /
      mega / sub / popup). This is the single source of truth for the player's movelog
      and its one-move-at-a-time playback; the lists above are the same
      events in their old per-category buckets.

  Board conditions are tracked through the log and snapshotted into every
  frame: weather/terrain/rooms (|weather|, |terrain|, |fieldactivate|
  trickroom, ...) and per-side conditions (Tailwind, Reflect, hazards --
  |sidestart|/|sideend| in both the Side1 and pX: name forms). The Replays
  tab renders them as the battle-scene tint and chips. Stacking hazards
  (Spikes, Toxic Spikes) gain a layer every time the same side logs
  another |sidestart| for them, snapshot as side_condition_layers so the
  player can draw exactly how deep the hazard is (1..3 for Spikes,
  1..2 for Toxic Spikes).

  Each timed condition also carries how many turns it has left
  (weather_remaining / terrain_remaining / room_remaining /
  side_condition_remaining), counted down per turn from its base duration:
  weather, terrain, and rooms last 5 turns (8 when the setter's held item
  -- Heat Rock, Damp Rock, Smooth Rock, Icy Rock, Terrain Extender, revealed
  by |showteam| -- extends them), Tailwind 4, and the screens 5. Weather
  also ends naturally the first turn the log stops mentioning it, which
  fixes the old "sunny day that never ends" when a log lacks an explicit
  clear line.

Also tracks forme changes -- Mega Evolution, Primal, and other
|detailschange| or old-style |mega| log lines swap that slot's species
in the board snapshot (HP untouched), so the player sees the mega
sprite and name from the turn it happens. pretty_species() converts
raw forme names like "Blastoise-Mega" into display text like
"Mega Blastoise".

Substitute dolls are tracked too (|-substitute| lines, or |-start| /
|-end| ... Substitute), snapshotted per frame so the player can overlay
a doll over the hidden mon, and item/ability reveals (|-ability|,
|-enditem|, [from] item/ability tags) become "popup" events the player
floats over the battlefield as small name badges.

Every real log also gets a pre-battle "turn 0" frame carrying the lead
switch-ins, so the player can watch both teams walk out before the
first move (synthetic logs with no lead switches still start at 1).

Deliberately independent of the event-table pipeline: constructed
straight from the log so it can never drift from what the player
shows, and cheap enough to rebuild per game on demand rather than
storing another table.
"""

import re

_SLOT_ORDER = ("p1a", "p1b", "p2a", "p2b")

_HP_RE = re.compile(r"^\s*(\d+)/(\d+)")

# Protect-family moves. A logged Protect that never activates (repeated use,
# a |fail| line naming it) is recorded with a trailing True flag on its move
# event so the player shows a wispy break instead of a full shield. Everything
# else here just means "a shield can come up".
_PROTECT_MOVES = frozenset({
    "Protect", "Detect", "King's Shield", "Spiky Shield", "Baneful Bunker",
    "Obstruct", "Silk Trap", "Burning Bulwark", "Wide Guard", "Quick Guard",
    "Mat Block", "Crafty Shield",
})

# Valid Terastallize types, so |terastallize| event types can be picked out of
# a line regardless of how the log formats them (actor + type token).
_TERA_TYPES = frozenset({
    "Normal", "Fire", "Water", "Electric", "Grass", "Ice", "Fighting",
    "Poison", "Ground", "Flying", "Psychic", "Bug", "Rock", "Ghost",
    "Dragon", "Dark", "Steel", "Fairy", "Stellar",
})

# Turn-count durations for timed conditions: weather/terrain/rooms last 5
# turns (8 with an extending held item), Tailwind 4, the screens 5.
_WEATHER_EXTENDERS = {
    "Heat Rock": "Harsh sunlight", "Damp Rock": "Rain",
    "Smooth Rock": "Sandstorm", "Icy Rock": "Hail",
}
_TERRAIN_EXTENDER = "Terrain Extender"
_SIDE_COND_BASE = {
    "Tailwind": 4,
    "Reflect": 5, "Light Screen": 5, "Aurora Veil": 5,
    "Safeguard": 5, "Mist": 5,
}

# Hazards that stack in layers instead of leaving after a turn countdown:
# the value is the maximum stack. Every |sidestart| for the same hazard on
# the same side adds one more layer (Showdown logs a fresh line per layer),
# and a numeric token on the line is honored too; anything not listed here
# is a single sheet that never counts up.
_LAYERED_SIDE_CONDS = {
    "Spikes": 3,
    "Toxic Spikes": 2,
}


def _parse_slot(raw: str) -> str:
    return raw.split(": ")[0].strip()


# Board-condition display names. Raw Showdown tokens (SunnyDay,
# GrassyTerrain, trickroom, "move: Tailwind") normalize to the friendly
# strings the Replays tab renders; anything unknown keeps a title-cased
# version of the raw token.
_WEATHER_TEXT = {
    "SunnyDay": "Harsh sunlight", "Sunny": "Harsh sunlight",
    "RainDance": "Rain", "Rain": "Rain",
    "Sandstorm": "Sandstorm", "Sand": "Sandstorm",
    "Hail": "Hail", "Snow": "Snow", "Snowy": "Snow",
}
_TERRAIN_TEXT = {
    "ElectricTerrain": "Electric Terrain", "Electric": "Electric Terrain",
    "GrassyTerrain": "Grassy Terrain", "Grassy": "Grassy Terrain",
    "MistyTerrain": "Misty Terrain", "Misty": "Misty Terrain",
    "PsychicTerrain": "Psychic Terrain", "Psychic": "Psychic Terrain",
}
_ROOM_TEXT = {
    "trickroom": "Trick Room", "TrickRoom": "Trick Room",
    "gravity": "Gravity", "Gravity": "Gravity",
    "magicroom": "Magic Room", "MagicRoom": "Magic Room",
    "wonderroom": "Wonder Room", "WonderRoom": "Wonder Room",
}
_SIDE_COND_TEXT = {
    "Tailwind": "Tailwind", "tailwind": "Tailwind",
    "Reflect": "Reflect", "reflect": "Reflect",
    "Light Screen": "Light Screen", "LightScreen": "Light Screen",
    "lightscreen": "Light Screen",
    "Aurora Veil": "Aurora Veil", "AuroraVeil": "Aurora Veil",
    "Safeguard": "Safeguard", "safeguard": "Safeguard",
    "Mist": "Mist", "mist": "Mist",
    "Spikes": "Spikes", "spikes": "Spikes",
    "Stealth Rock": "Stealth Rock", "StealthRock": "Stealth Rock",
    "stealthrock": "Stealth Rock",
    "Toxic Spikes": "Toxic Spikes", "ToxicSpikes": "Toxic Spikes",
    "Sticky Web": "Sticky Web", "StickyWeb": "Sticky Web",
}


def _condition_text(raw: str, table: dict) -> str:
    """Normalizes a raw condition token to a display string: strips a
    'move: ' prefix, looks the name up in `table`, and falls back to a
    title-cased version of the token itself (so the same input maps to
    the same text for both sidestart and sideend)."""
    name = raw.strip()
    if name.lower().startswith("move: "):
        name = name[6:].strip()
    if name in table:
        return table[name]
    key = name.title()
    return table.get(key, key)


def _parse_side(raw: str) -> str:
    """'p1: Alice' / 'Side1' -> 'p1' (which side a condition belongs to)."""
    raw = raw.strip()
    if raw.startswith("Side"):
        return "p1" if raw[4:] == "1" else "p2"
    return raw[:2]


def _parse_showteam_items(line: str) -> dict:
    """Maps team members' species -> held item from a |showteam| line.

    The payload is one chunk per mon joined by ']', each chunk a pipe-
    separated record in the shape Species||Item|Ability|Moves|... (the
    nickname field between Species and Item is empty on Showdown, so the
    item is always the third field):

        |showteam|p1|Sneasler||FocusSash|PoisonTouch|...|]Kingambit||...

    Unknown/empty items are skipped; what's found is enough to detect the
    duration-extending weather/terrain items (Heat Rock etc.)."""
    items = {}
    payload = line.split("|", 3)[3] if line.count("|") >= 3 else ""
    for chunk in payload.split("]"):
        fields = chunk.split("|")
        if not fields or not fields[0].strip():
            continue
        species = fields[0].strip()
        item = fields[2].strip() if len(fields) > 2 else ""
        if species and item:
            items[species] = item
    return items


def _setter_item(tokens: list, board: dict, items: dict) -> str | None:
    """The held item of whoever set a field condition, when the log names
    them via '[of] p1a: Ninetales' (None when it can't be determined)."""
    for field in tokens[3:]:
        if field.startswith("[of] "):
            return items.get(board.get(_parse_slot(field[5:])))
    return None


def pretty_species(species: str) -> str:
    """Human-friendly display name for a raw species string. The board
    snapshot keeps Showdown's raw names (sprites.py needs them to find
    art), so labels/narrative convert here instead: 'Blastoise-Mega' ->
    'Mega Blastoise', 'Charizard-Mega-Y' -> 'Mega Charizard Y',
    'Groudon-Primal' -> 'Primal Groudon'. Anything else passes through
    unchanged."""
    if species.endswith("-Mega-X"):
        return "Mega " + species[:-7] + " X"
    if species.endswith("-Mega-Y"):
        return "Mega " + species[:-7] + " Y"
    if species.endswith("-Mega"):
        return "Mega " + species[:-5]
    if species.endswith("-Primal"):
        return "Primal " + species[:-7]
    return species


def build_timeline(log_text: str) -> dict:
    """
    Returns {"p1_name", "p2_name", "winner", "format", "gametype",
    "frames": [frame, ...]} where "gametype" is "singles"/"doubles" when
    the log declares it (|gametype|), otherwise "" (callers can fall back
    to infer_game_type); and each frame is:

      {"turn": int,
       "moves": [[side, slot, mon, move, target_slot_or_None,
                  [target_slots]], ...],
       "hp": {slot: "cur/max"},
       "faints": [slot],
       "ko_credit": {slot: [mon, side]},
       "switches": [[side, slot, mon]],
       "megas": [slot] -- slots that forme-changed (Mega etc.) this turn,
       "substitutes": {slot: True} -- slots with a Substitute doll up,
       "weather"/"terrain"/"room": display str or None -- the active
          weather, terrain, and room (Trick Room etc.) this frame,
       "side_conditions": {side: [display str]} -- per-side conditions
          such as Tailwind, Reflect/Light Screen, and hazards,
       "side_condition_layers": {side: {name: int}} -- stack depth for
          layered hazards (Spikes 1..3, Toxic Spikes 1..2); absent when
          none are down}

    HP is a snapshot taken AFTER the turn's moves/damage/heals, so a
    frame is directly renderable. A slot absent from "hp" means its
    mon is unknown/off the field (start of game, or after a faint
    with no visible values yet).
    """
    frames = []
    p1_name = p2_name = winner = format_str = ""
    game_type = ""
    current_hp = {}
    current_faint = []      # slots that fainted, in log order
    current_ko = {}
    current_moves = []
    current_switches = []
    current_megas = []                     # slots that forme-changed (mega etc.)
    current_events = []                    # ordered battle events for this turn
    initial_events = []                    # pre-turn switches (frame 0 only)
    current_faint_events = set()           # slots already given a faint event
    current_frame = None
    current_board = {}                     # slot -> mon currently on the field
    current_move_attacker = (None, None)   # (mon, side) of the most recently used move
    current_move_targets = set()           # slots that move's damage can be credited to
    current_weather = None                 # active weather (display text)
    weather_upkeeps = 0                    # turns the weather has already lasted
    weather_base = 5                       # 5 turns, or 8 with an extending item
    weather_saw_line = False               # weather was mentioned this turn
    weather_was_active_prev = False        # weather was active last frame
    current_terrain = None                 # active terrain (display text)
    terrain_upkeeps = 0
    terrain_base = 5
    terrain_was_active_prev = False
    current_room = None                    # active room (display text)
    room_upkeeps = 0
    room_base = 5
    room_was_active_prev = False
    current_side_conditions = {}           # side -> set of display names
    current_side_layers = {}               # side -> {display name -> layers}
    side_upkeeps = {}                      # (side, name) -> turns already lasted
    side_was_prev = set()                  # timed (side, name) active last frame
    items = {}                             # species -> held item (from |showteam|)
    current_substitutes = {}               # slot -> True while its Substitute doll is up
    current_move_slot = None               # slot of the most recently used move
    current_targets_list = None            # live [target_slots] of the active move event
    current_target_species = {}            # target slot -> species at hit time
    current_teras = {}                     # slot -> tera type while its mon is terastallized
    current_dynamaxed = {}                 # slot -> turns it has stayed dynamaxed (reverts at 3)
    current_ohko = []                      # slots one-hit-KO'd this turn (full HP felled by a hit)

    # Protect outcome tracking, per turn: the move event of the active
    # protect (and its matching moves-list entry), and whether a later
    # |fail| line reported it never came up.
    current_protect_event_index = None
    current_protect_move_index = None
    current_protect_slot = None
    current_protect_failed = False

    def _start_frame(turn_no):
        nonlocal current_frame, current_hp, current_faint, current_ko, current_moves, current_switches, current_megas, current_events, current_faint_events
        nonlocal current_protect_event_index, current_protect_move_index, current_protect_slot, current_protect_failed
        nonlocal current_targets_list, current_move_slot, current_target_species
        nonlocal current_teras, current_dynamaxed, current_ohko
        _apply_turn_transition()
        current_frame = {"turn": turn_no, "faints": [], "moves": [], "switches": []}
        current_faint = []
        current_ko = {}
        current_moves = []
        current_switches = []
        current_megas = []
        current_events = []
        current_faint_events = set()
        current_ohko = []
        # The dynamax clock: three turns after a mon Dynamaxes it reverts to
        # base form, so the (dynamaxed) indicator drops off the field even
        # when the mon stays in (a faint / switch-out clears it sooner).
        for slot in list(current_dynamaxed):
            current_dynamaxed[slot] += 1
            if current_dynamaxed[slot] >= 3:
                current_dynamaxed.pop(slot)
        current_protect_event_index = None
        current_protect_move_index = None
        current_protect_slot = None
        current_protect_failed = False
        current_targets_list = None
        current_move_slot = None
        current_target_species = {}

    def _apply_turn_transition():
        """Advance the timed conditions at the top of a turn: weather
        expires the first turn the log stops mentioning it (and extends to
        8 turns with an item), while terrain/rooms and timed side
        conditions count down to zero and end naturally."""
        nonlocal current_weather, weather_upkeeps, weather_base, weather_saw_line, weather_was_active_prev
        nonlocal current_terrain, terrain_upkeeps, terrain_base, terrain_was_active_prev
        nonlocal current_room, room_upkeeps, room_base, room_was_active_prev
        nonlocal side_upkeeps, side_was_prev, current_side_conditions, current_side_layers
        if current_weather is not None and weather_was_active_prev:
            if not weather_saw_line:
                # No weather line at all this turn: it ended naturally.
                current_weather = None
                weather_upkeeps = 0
                weather_base = 5
            else:
                weather_upkeeps += 1
            # Total duration stays fixed: 5 turns, or 8 only when a
            # |showteam|-confirmed Heat/Damp/Smooth/Icy Rock extends it.
            # The former brightens naturally the first turn the log stops
            # mentioning it (caught by weather_saw_line on the next turn).
        if current_terrain is not None and terrain_was_active_prev:
            terrain_upkeeps += 1
            if terrain_upkeeps >= terrain_base:
                current_terrain = None
                terrain_upkeeps = 0
                terrain_base = 5
        if current_room is not None and room_was_active_prev:
            room_upkeeps += 1
            if room_upkeeps >= room_base:
                current_room = None
                room_upkeeps = 0
                room_base = 5
        for key in list(side_was_prev):
            side, name = key
            base = _SIDE_COND_BASE.get(name)
            if name in current_side_conditions.get(side, ()) and base is not None:
                n = side_upkeeps.get(key, 0) + 1
                if n >= base:
                    # Duration spent; the log may also log a sideend, but
                    # the countdown is the safety net.
                    current_side_conditions[side].discard(name)
                    current_side_layers.get(side, {}).pop(name, None)
                    if not current_side_conditions[side]:
                        current_side_conditions.pop(side, None)
                    if side in current_side_layers and not current_side_layers[side]:
                        current_side_layers.pop(side, None)
                    side_upkeeps.pop(key, None)
                else:
                    side_upkeeps[key] = n
            else:
                side_upkeeps.pop(key, None)
        side_was_prev.clear()
        weather_saw_line = False

    def _remaining(base: int, upkeeps: int) -> int:
        return max(1, base - upkeeps)

    def _include_followup_target(slot):
        """Extends the ACTIVE move's target list with a slot that just took
        or avoided its hit even though the log's [spread] field didn't name
        it -- an immune partner, a Protect-blocked target, a dodge, or a
        clean hit the spread list skipped. Species are captured at hit time,
        so the player names exactly who was in the slot when the move
        connected. A no-op between moves: the window is closed by the next
        |move| or |turn| (and the lists are finalized with the frame, so
        this never mutates an already-snapshotted turn)."""
        if current_targets_list is None or not slot:
            return
        if slot == current_move_slot:
            return
        if slot in current_move_targets:
            return
        current_move_targets.add(slot)
        current_targets_list.append(slot)
        current_targets_list.sort()
        current_target_species[slot] = current_board.get(slot)

    def _finalize_frame():
        nonlocal current_frame, current_hp, current_faint, current_ko, current_moves, current_switches, current_megas, current_events, current_side_layers
        nonlocal current_protect_event_index, current_protect_move_index, current_protect_failed
        nonlocal weather_was_active_prev, terrain_was_active_prev, room_was_active_prev, side_was_prev
        nonlocal current_teras, current_dynamaxed, current_ohko
        if current_frame is None:
            return
        current_frame["board"] = dict(current_board)
        current_frame["hp"] = dict(current_hp)
        if current_faint:
            current_frame["faints"] = current_faint
        if current_ko:
            current_frame["ko_credit"] = current_ko
        if current_moves:
            current_frame["moves"] = current_moves
        if current_switches:
            current_frame["switches"] = current_switches
        if current_megas:
            current_frame["megas"] = current_megas
        if current_events:
            current_frame["events"] = current_events
        if current_substitutes:
            current_frame["substitutes"] = dict(current_substitutes)
        current_frame["teras"] = dict(current_teras)
        current_frame["dynamaxed"] = dict(current_dynamaxed)
        current_frame["ohko"] = (
            "DOUBLE OHKO" if len(current_ohko) >= 2
            else ("OHKO" if len(current_ohko) == 1 else None)
        )
        current_frame["ohko_slots"] = sorted(set(current_ohko))
        current_frame["weather"] = current_weather
        current_frame["terrain"] = current_terrain
        current_frame["room"] = current_room
        current_frame["side_conditions"] = {
            side: sorted(names) for side, names in current_side_conditions.items()
        }
        if current_side_layers:
            current_frame["side_condition_layers"] = {
                side: dict(names) for side, names in current_side_layers.items()
            }
        current_frame["weather_remaining"] = (
            _remaining(weather_base, weather_upkeeps) if current_weather else None
        )
        current_frame["terrain_remaining"] = (
            _remaining(terrain_base, terrain_upkeeps) if current_terrain else None
        )
        current_frame["room_remaining"] = (
            _remaining(room_base, room_upkeeps) if current_room else None
        )
        side_cond_remaining = {}
        for side, names in current_side_conditions.items():
            for name in names:
                base = _SIDE_COND_BASE.get(name)
                if base is not None:
                    side_cond_remaining.setdefault(side, {})[name] = _remaining(
                        base, side_upkeeps.get((side, name), 0)
                    )
        if side_cond_remaining:
            current_frame["side_condition_remaining"] = side_cond_remaining
        weather_was_active_prev = current_weather is not None
        terrain_was_active_prev = current_terrain is not None
        room_was_active_prev = current_room is not None
        for side, names in current_side_conditions.items():
            for name in names:
                if name in _SIDE_COND_BASE:
                    side_was_prev.add((side, name))
        # A protect that failed (reported |fail|) gets its move event a
        # trailing flag so the player shows a wispy break, not a shield.
        if (current_protect_event_index is not None
                and 0 <= current_protect_event_index < len(current_events)
                and current_protect_failed
                and current_events[current_protect_event_index][0] == "move"):
            move_ev = current_events[current_protect_event_index]
            current_events[current_protect_event_index] = move_ev + (True,)
        # The moves entry gets the same trailing flag so a WHOLE-TURN render
        # (step_forward / step_back / tab open) suppresses the shield too --
        # not just the per-event autoplay path.
        if (current_protect_move_index is not None
                and 0 <= current_protect_move_index < len(current_moves)
                and current_protect_failed
                and len(current_moves[current_protect_move_index]) == 7):
            current_moves[current_protect_move_index].append(True)
        frames.append(current_frame)
        current_frame = None
        current_faint = []
        current_ko = {}
        current_moves = []
        current_switches = []
        current_megas = []
        current_events = []

    def _emit_turn_zero_frame_if_needed():
        """Every REAL log gets a pre-battle 'turn 0' entry -- the lead
        switch-ins only, so the player can watch both teams walk out before
        the first move. Synthetic logs with no lead switches keep starting
        at turn 1 (frames stay empty until the first |turn| line)."""
        nonlocal current_frame
        if frames or not initial_events or current_frame is not None:
            return
        current_frame = {
            "turn": 0,
            "faints": [],
            "moves": [],
            "switches": [list(s) for s in current_switches],
            "events": list(initial_events),
            "board": dict(current_board),
            "hp": dict(current_hp),
            "weather": current_weather,
            "terrain": current_terrain,
            "room": current_room,
            "side_conditions": {
                side: sorted(names) for side, names in current_side_conditions.items()
            },
            "weather_remaining": None,
            "terrain_remaining": None,
            "room_remaining": None,
        }
        frames.append(current_frame)
        current_frame = None

    for line in log_text.split("\n"):
        tokens = line.split("|")
        if len(tokens) < 2:
            continue
        tag = tokens[1]

        if tag == "player":
            if len(tokens) > 3 and tokens[2] == "p1" and tokens[3]:
                p1_name = tokens[3]
            if len(tokens) > 3 and tokens[2] == "p2" and tokens[3]:
                p2_name = tokens[3]

        elif tag == "win":
            winner = tokens[2]

        elif tag == "tier":
            format_str = tokens[2]

        elif tag == "gametype":
            game_type = tokens[2].strip().lower() if len(tokens) > 2 else ""

        elif tag == "turn":
            _finalize_frame()  # finalize the previous turn, if any
            _emit_turn_zero_frame_if_needed()
            _start_frame(int(tokens[2]))

        elif tag in ("detailschange", "mega"):
            # Forme change -- Mega Evolution, Primal, and other
            # detailschange forms: the mon in that slot changes identity
            # but keeps its HP, so only the board species is updated.
            # (The |-mega| info line is deliberately NOT handled: it
            # names the base species for the item animation, so taking
            # it as the new species would revert the mega.)
            slot = _parse_slot(tokens[2])
            if len(tokens) > 3 and tokens[3]:
                current_board[slot] = tokens[3].split(",")[0].strip()
                current_megas.append(slot)
                current_events.append(("mega", slot))

        elif tag in ("switch", "drag"):
            slot = _parse_slot(tokens[2])
            mon = tokens[3].split(",")[0].strip()
            current_board[slot] = mon
            m = _HP_RE.match(tokens[4]) if len(tokens) > 4 else None
            hp_str = ""
            if m:
                hp_str = f"{m.group(1)}/{m.group(2)}"
                current_hp[slot] = hp_str
            current_switches.append([tokens[2][:2], slot, mon])
            ev = ("switch", tokens[2][:2], slot, mon, hp_str)
            if not frames and current_frame is None:
                # Pre-battle switch-ins belong to frame 0's event list.
                initial_events.append(ev)
            else:
                current_events.append(ev)
            # A mon switching out takes its Substitute doll with it.
            if current_substitutes.pop(slot, None):
                current_events.append(("sub", slot, False))
            # ...and its terastallization / dynamax indicator too (the
            # mon leaves the field; a later switch-in isn't flagged).
            current_teras.pop(slot, None)
            current_dynamaxed.pop(slot, None)

        elif tag == "move":
            slot = _parse_slot(tokens[2])
            side = "p1" if slot[1] == "1" else "p2"
            mon = tokens[2].split(": ", 1)[1].split(",")[0].strip() if ": " in tokens[2] else ""
            target = None
            current_move_targets = set()
            if len(tokens) > 4 and ": " in tokens[4] and not tokens[4].startswith("["):
                target = _parse_slot(tokens[4])
                current_move_targets.add(target)
            for field in tokens[5:]:
                if field.startswith("[spread]"):
                    current_move_targets = {t.strip() for t in field[len("[spread]"):].split(",") if t.strip()}
            current_move_attacker = (mon, side)
            targets = sorted(current_move_targets) if current_move_targets else ([target] if target else [])
            current_move_slot = slot
            current_targets_list = targets
            current_target_species = {t: current_board.get(t) for t in targets if current_board.get(t)}
            current_moves.append([side, slot, mon, tokens[3], target, targets, current_target_species])
            target_mon = current_board.get(target) if target else None
            # (kind, side, slot, mon, move, target, target_mon, targets,
            #  target_species) -- target_species is the per-target species
            # AT MOVE TIME (item D); a failed protect appends a trailing
            # True flag for the player's wispy-break animation.
            current_events.append(("move", side, slot, mon, tokens[3], target, target_mon, targets, current_target_species))
            if tokens[3] in _PROTECT_MOVES:
                current_protect_slot = slot
                current_protect_failed = False
                current_protect_event_index = len(current_events) - 1
                current_protect_move_index = len(current_moves) - 1

        elif tag == "fail":
            # A |fail| line naming the active protect means that protect
            # never activated (repeated use / a locking move), so the player
            # shows a wispy break instead of a full shield.
            if len(tokens) > 2 and ": " in tokens[2]:
                # A bare '|fail|pX: mon|' -- with no move token -- is the
                # protect failing too (some logs print only the actor).
                if (current_protect_event_index is not None
                        and _parse_slot(tokens[2]) == current_protect_slot):
                    reason = " ".join(tokens[3:]).lower()
                    if (not reason or
                            any(k in reason for k in
                                ("protect", "guard", "shield", "block"))):
                        current_protect_failed = True

        elif tag in ("-immune", "immune"):
            # A mon that shrugged the current move off (Earthquake on a
            # Flying partner, Volt Absorb, ...) is still part of that move's
            # story: count it as a target so the player sees everyone the
            # attack touched (item F).
            if len(tokens) > 2 and ": " in tokens[2]:
                _include_followup_target(_parse_slot(tokens[2]))

        elif tag == "-miss":
            # |-miss|p1a: Attacker|p2a: Dodger names the specific slot that
            # avoided the attack -- treat it as a (missed) target too.
            if len(tokens) > 3 and ": " in tokens[3]:
                _include_followup_target(_parse_slot(tokens[3]))

        elif tag == "-activate":
            # |-activate|p2a: Mon|move: Protect -- a shield coming up means
            # the attack was blocked there; the shielded mon counts as a
            # target (it was aimed at, after all).
            if len(tokens) > 3 and any(
                k in tokens[3].lower() for k in
                ("protect", "guard", "shield", "block", "king", "mat")
            ):
                _include_followup_target(_parse_slot(tokens[2]))

        elif tag in ("-substitute", "substitute"):
            # |-substitute|p1a: Mon|25/100 creates/refreshes the doll (the
            # HP token is the leftover HP); |-substitute|p1a: Mon| with an
            # empty HP token means it broke (item E).
            if len(tokens) > 2 and ": " in tokens[2]:
                slot = _parse_slot(tokens[2])
                if len(tokens) > 3 and tokens[3]:
                    current_substitutes[slot] = True
                    current_events.append(("sub", slot, True))
                elif current_substitutes.pop(slot, None):
                    current_events.append(("sub", slot, False))

        elif tag == "-start":
            if (len(tokens) > 3 and tokens[3].strip() == "Substitute"
                    and ": " in tokens[2]):
                slot = _parse_slot(tokens[2])
                current_substitutes[slot] = True
                current_events.append(("sub", slot, True))

        elif tag == "-end":
            if (len(tokens) > 3 and tokens[3].strip() == "Substitute"
                    and ": " in tokens[2]):
                slot = _parse_slot(tokens[2])
                if current_substitutes.pop(slot, None):
                    current_events.append(("sub", slot, False))

        elif tag in ("-boost", "boost", "-unboost", "unboost"):
            # |-boost|p1a: Lucario|atk|2 and |-unboost|... -- a stat going
            # up (Swords Dance) or down (Close Combat's defense drop, Draco
            # Meteor's spatk drop). Recorded as their own events so the
            # player can float a rising/falling stat badge and fold the
            # change into the move's movelog line. Trailing [from]/[silent]
            # tags are narration, not data, and are ignored.
            if len(tokens) > 2 and ": " in tokens[2]:
                slot = _parse_slot(tokens[2])
                stat = tokens[3].strip() if len(tokens) > 3 else ""
                stages = 1
                if len(tokens) > 4:
                    try:
                        stages = min(6, max(-6, int(float(tokens[4]))))
                    except (TypeError, ValueError):
                        pass
                if stat:
                    current_events.append(
                        (tag[1:], slot, stat, stages, current_board.get(slot) or "")
                    )

        elif tag in ("-ability", "ability"):
            # |-ability|p1a: mon|Ability -- a passive activation or reveal.
            # Unnerve / Mold Breaker are announced as full messages rather
            # than a floating name badge, matching the in-game text (item G).
            # Popups carry the mon that was in the slot AT THE LINE, so a
            # mid-turn switch can't rename who activated.
            if len(tokens) > 3 and ": " in tokens[2]:
                slot = _parse_slot(tokens[2])
                name = tokens[3].strip()
                mon = current_board.get(slot)
                if name in ("Unnerve", "Mold Breaker"):
                    current_events.append(("popup", slot, "message", name, mon))
                else:
                    current_events.append(("popup", slot, "ability", name, mon))

        elif tag in ("-enditem", "-startitem"):
            # |-enditem|p1a: mon|Item -- the item vanished (consumed or
            # knocked off), announced as a floating popup (item G).
            if len(tokens) > 3 and ": " in tokens[2]:
                slot = _parse_slot(tokens[2])
                current_events.append(
                    ("popup", slot, "item", tokens[3].strip(), current_board.get(slot))
                )

        elif tag in ("moldbreaker", "-moldbreaker"):
            # |moldbreaker|p1a: mon| -- announce-style message line.
            if len(tokens) > 2 and ": " in tokens[2]:
                slot = _parse_slot(tokens[2])
                current_events.append(
                    ("popup", slot, "message", "breaks the mold", current_board.get(slot))
                )

        elif tag in ("-supereffective", "-resisted", "-crit",
                     "supereffective", "resisted", "crit"):
            # A move's announcement box-text ("It's super effective!", "A
            # critical hit!") -- surfaced in the movelog like an ability
            # reveal (folded under its move), but with NO floating board
            # bubble. A trailing numeric token (layer/BP indicator) is
            # narration detail, not part of the message.
            if len(tokens) > 2 and ": " in tokens[2]:
                slot = _parse_slot(tokens[2])
                current_events.append((
                    "notice", slot, current_board.get(slot), {
                        "-supereffective": "It's super effective!",
                        "supereffective": "It's super effective!",
                        "-resisted": "It's not very effective...",
                        "resisted": "It's not very effective...",
                        "-crit": "A critical hit!",
                        "crit": "A critical hit!",
                    }.get(tag, "")
                ))

        elif tag in ("terastallize", "-terastallize"):
            # Terastallization: |terastallize|p1a: Corviknight|Flying. The
            # tera forme (if any) arrives as its own |detailschange|; here we
            # just record the act and, when named, the tera type, so the
            # player can narrate it and flash a crystalline aura.
            if len(tokens) > 2 and ": " in tokens[2]:
                slot = _parse_slot(tokens[2])
                mon = tokens[2].split(": ", 1)[1].split(",")[0].strip()
                tera_type = next((f for f in tokens[3:] if f in _TERA_TYPES), None)
                current_teras[slot] = tera_type
                current_events.append(("tera", slot, mon, tera_type))

        elif tag in ("dynamax", "-dynamax"):
            # |dynamax|p1a: GigantamaxName -- the form itself is tracked via
            # |detailschange| when the log prints it; this line is the act.
            if len(tokens) > 2 and ": " in tokens[2]:
                slot = _parse_slot(tokens[2])
                mon = tokens[2].split(": ", 1)[1].split(",")[0].strip()
                current_board[slot] = mon
                current_dynamaxed[slot] = 0
                current_events.append(("dynamax", slot, mon))

        elif tag in ("weather", "-weather"):
            weather_saw_line = True
            if len(tokens) > 2 and tokens[2]:
                if tokens[2].lower() in ("none", "end", "clear", "cleared"):
                    current_weather = None
                    weather_upkeeps = 0
                    weather_base = 5
                else:
                    current_weather = _condition_text(tokens[2], _WEATHER_TEXT)
                    # A set/reset starts a fresh countdown; an "[upkeep]"
                    # restatement just continues the existing one.
                    if not any(f.startswith("[upkeep]") for f in tokens[3:]):
                        weather_upkeeps = 0
                        weather_base = (
                            8 if _setter_item(tokens, current_board, items) in _WEATHER_EXTENDERS
                            else 5
                        )
                    # A setter's ability reveal (Drought, ...) floats a
                    # popup beside whoever set the sun (item G). [of] names
                    # the setter; it comes AFTER the [from] tag, so resolve
                    # the setter first and credit the popup to that mon.
                    setter_slot = None
                    for field in tokens[3:]:
                        if field.startswith("[of] "):
                            setter_slot = _parse_slot(field[5:])
                    for field in tokens[3:]:
                        if field.startswith("[from] ability: "):
                            ab = field[len("[from] ability: "):].strip()
                            if ab and ab not in ("Unnerve", "Mold Breaker"):
                                current_events.append((
                                    "popup", setter_slot, "ability", ab,
                                    current_board.get(setter_slot) if setter_slot else None))
            else:
                current_weather = None
                weather_upkeeps = 0

        elif tag in ("terrain", "-terrain", "terrainstart"):
            if len(tokens) > 2 and tokens[2]:
                if tokens[2].lower() in ("none", "end", "clear", "cleared"):
                    current_terrain = None
                    terrain_upkeeps = 0
                    terrain_base = 5
                else:
                    current_terrain = _condition_text(tokens[2], _TERRAIN_TEXT)
                    if not any(f.startswith("[upkeep]") for f in tokens[3:]):
                        terrain_upkeeps = 0
                        terrain_base = (
                            8 if _setter_item(tokens, current_board, items) == _TERRAIN_EXTENDER
                            else 5
                        )
            else:
                current_terrain = None
                terrain_upkeeps = 0

        elif tag == "terrainend":
            current_terrain = None
            terrain_upkeeps = 0
            terrain_base = 5

        elif tag in ("fieldactivate", "fieldstart"):
            if len(tokens) > 2 and tokens[2]:
                current_room = _condition_text(tokens[2], _ROOM_TEXT)
                room_upkeeps = 0
                room_base = 5

        elif tag == "fieldend":
            current_room = None
            room_upkeeps = 0
            room_base = 5

        elif tag == "trickroom":
            # Legacy protocol: an empty token starts Trick Room, "0" ends it.
            if len(tokens) > 2 and tokens[2] == "0":
                current_room = None
                room_upkeeps = 0
                room_base = 5
            else:
                current_room = "Trick Room"
                room_upkeeps = 0
                room_base = 5

        elif tag in ("sidestart", "-sidestart"):
            if len(tokens) > 3:
                side = _parse_side(tokens[2])
                name = _condition_text(tokens[3], _SIDE_COND_TEXT)
                if name:
                    current_side_conditions.setdefault(side, set()).add(name)
                    if name in _SIDE_COND_BASE:
                        side_upkeeps[(side, name)] = 0
                    max_layers = _LAYERED_SIDE_CONDS.get(name)
                    if max_layers is not None:
                        # Stacking hazard: another layer joins the pile
                        # (Showdown logs one line per layer). A numeric
                        # token on the line is honored as the depth too.
                        layers = current_side_layers.setdefault(side, {}).get(name, 0) + 1
                        for field in tokens[4:]:
                            if field.strip().isdigit():
                                layers = max(layers, int(field.strip()))
                        current_side_layers[side][name] = min(layers, max_layers)

        elif tag in ("sideend", "-sideend"):
            if len(tokens) > 3:
                side = _parse_side(tokens[2])
                name = _condition_text(tokens[3], _SIDE_COND_TEXT)
                conds = current_side_conditions.get(side)
                if conds is not None:
                    conds.discard(name)
                    if not conds:
                        current_side_conditions.pop(side, None)
                current_side_layers.get(side, {}).pop(name, None)
                if side in current_side_layers and not current_side_layers[side]:
                    current_side_layers.pop(side, None)
                side_upkeeps.pop((side, name), None)

        elif tag in ("-damage", "-heal"):
            slot = _parse_slot(tokens[2])
            # Capture the species standing in the slot at THIS LINE, not the
            # turn-end board, so a mid-turn switch (Flip Turn pulling the
            # mover out for a replacement) can't rename who took the hit.
            mon = current_board.get(slot)
            # The HP going INTO this hit -- used to spot a one-hit KO (the
            # target was at full HP when the move felled it).
            prev_hp = current_hp.get(slot)
            m = _HP_RE.match(tokens[3]) if len(tokens) > 3 else None
            if m:
                hp_str = f"{m.group(1)}/{m.group(2)}"
                current_hp[slot] = hp_str
                current_events.append((tag[1:], slot, hp_str, mon))
            if tag == "-damage":
                # A clean hit on a slot the [spread] list didn't name still
                # counts as a target; recoil/status [from] lines (always on
                # the attacker, and always tagged) never do.
                if not any(f.startswith("[from]") for f in tokens[4:]):
                    _include_followup_target(slot)
            # Item/ability activations from damage/heal lines float a
            # small popup beside the mon (item G) -- Leftovers ticking, a
            # Life Orb recoil, Rough Skin, and friends. The [of] tag names
            # the ability's OWNER (Rough Skin sits on Garchomp, not on
            # whoever took the damage), and [of] always trails [from], so
            # resolve the owner first and credit the popup to that mon.
            owner_slot, owner_mon = slot, mon
            for field in tokens[4:]:
                if field.startswith("[of] "):
                    raw = field[len("[of] "):].strip()
                    if raw:
                        owner_slot = _parse_slot(raw)
                        owner_mon = (
                            raw.split(": ", 1)[1].split(",")[0].strip()
                            if ": " in raw else current_board.get(owner_slot)
                        )
            for field in tokens[4:]:
                if field.startswith("[from] item: "):
                    current_events.append(
                        ("popup", slot, "item",
                         field[len("[from] item: "):].strip(), mon))
                elif field.startswith("[from] ability: "):
                    ab = field[len("[from] ability: "):].strip()
                    if ab and ab not in ("Unnerve", "Mold Breaker"):
                        current_events.append(("popup", owner_slot, "ability", ab, owner_mon))
            if tag == "-damage" and len(tokens) > 3 and "fnt" in tokens[3]:
                if slot not in current_faint:
                    current_faint.append(slot)
                if slot not in current_faint_events:
                    current_faint_events.add(slot)
                    current_events.append(("faint", slot, current_board.get(slot) or ""))
                has_from = any(f.startswith("[from]") for f in tokens[4:])
                if not has_from and slot in current_move_targets:
                    current_ko[slot] = list(current_move_attacker)
                    # One-hit KO: the target sat at FULL HP going into the
                    # hit. Count it so the player can banner OHKO (two in a
                    # turn reads DOUBLE OHKO).
                    pm = _HP_RE.match(prev_hp) if prev_hp else None
                    if pm and pm.group(1) == pm.group(2):
                        current_ohko.append(slot)
                else:
                    current_ko[slot] = [None, None]
                # A faint clears the mon's tera / dynamax indicator.
                current_teras.pop(slot, None)
                current_dynamaxed.pop(slot, None)
                if current_substitutes.pop(slot, None):
                    current_events.append(("sub", slot, False))

        elif tag == "faint":
            slot = _parse_slot(tokens[2])
            mon = tokens[2].split(": ", 1)[1].split(",")[0].strip() if ": " in tokens[2] else ""
            current_board[slot] = None
            current_teras.pop(slot, None)
            current_dynamaxed.pop(slot, None)
            if slot not in current_faint:
                current_faint.append(slot)
            if slot not in current_faint_events:
                current_faint_events.add(slot)
                current_events.append(("faint", slot, mon))
            if current_substitutes.pop(slot, None):
                current_events.append(("sub", slot, False))

        elif tag == "showteam":
            # Reveals held items, letting the countdown extend weather
            # (Heat Rock etc.) and terrain (Terrain Extender) to 8 turns.
            items.update(_parse_showteam_items(line))

    _finalize_frame()
    return {
        "p1_name": p1_name,
        "p2_name": p2_name,
        "winner": winner,
        "format": format_str,
        "gametype": game_type,
        "frames": frames,
    }


def infer_game_type(frames) -> str:
    """Classifies a built timeline as "singles" or "doubles" from which
    slots ever held a mon: any b-slot that fielded something means the
    battle used both slots per side, i.e. doubles. Fallback for logs
    that don't carry an explicit |gametype| line."""
    for frame in frames:
        board = frame.get("board") or {}
        if board.get("p1b") or board.get("p2b"):
            return "doubles"
    return "singles"