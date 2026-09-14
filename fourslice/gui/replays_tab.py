"""
fourslice/gui/replays_tab.py

The "Replays" tab: a native playback view of your own stored battles.
Left side lists every game with a stored raw log (see
storage.get_replayable_games); right side plays back that game
turn-by-turn from the log via replay_timeline.build_timeline -- no
browser, no external player, just Qt widgets rendering each frame.

Playback model is deliberately simple: each frame is one turn, and the
board view is a snapshot of that turn's end state (who's on the field
in each of the four slots, their HP, what happened that turn). The
controls are Play/Pause, step backward/forward, restart, and a speed
selector. A QTimer drives autoplay; stepping works with playback
stopped too.

The tab never mutates the database -- it only reads. Settings for
whether logs are even stored (and for how long) live in the Settings
dialog (settings_dialog.py); this tab just shows what's actually in
the logs table right now.

Since 2026 the board also shows Pokemon Showdown's gen-5 sprites:
each mon on the field renders its sprite (back-view for your side,
front-view for the opponent's, matching Showdown's own orientation)
with the name kept underneath. Sprites are downloaded on a background
thread from play.pokemonshowdown.com and cached on disk
(fourslice/sprites.py + gui/sprite_fetcher.py); anything that isn't
cached yet -- or can't be fetched, e.g. offline -- falls back to the
plain name label, so playback never depends on the network.

The board is rendered from YOUR point of view: whoever matches a
configured username (falling back to the importer's games.my_side
result) is always the bottom row with back-view sprites, and both rows
are labelled with the players' names. Mega evolutions and other forme
changes update the slot's species the turn they happen (see
replay_timeline.pretty_species), HP bars show the live percentage, and
the event narrative uses the players' names -- "salmoncashew's
Blastoise used Shell Smash!" -- instead of bare slot codes.

The board also plays Showdown-lite animations: known moves get bespoke
effects (a colored orb or beam for projectiles, a dash for contact
moves, a shield for Protect & friends, a pulse for setup moves), while
everything else keeps the generic surge; targets shake and flash red,
HP bars drain smoothly, faints fade the sprite out, switches fade in,
mega evolutions flash gold, and idle sprites bob gently. Board
conditions are visible too: the active weather, terrain, and room
(Trick Room...) tint the field and appear as a condition strip above the
board, and each side's conditions (Tailwind, screens, hazards) show
under the player name. Every effect is per-slot,
QTimer/animation-driven on the UI thread, purely cosmetic, and
toggleable via Settings (config.animate_board).
"""

import functools
import math

from PySide6.QtCore import (
    QByteArray, QEasingCurve, QPoint, QPointF, QPropertyAnimation,
    QRect, QRectF, QSize, QThread, QTimer, Qt, QVariantAnimation,
)
from PySide6.QtGui import (
    QBrush, QColor, QIcon, QLinearGradient, QMovie, QPainter, QPainterPath, QPen,
    QPixmap, QPolygonF, QRadialGradient, QTransform,
)
from PySide6.QtWidgets import (
    QApplication, QComboBox, QFrame, QGraphicsOpacityEffect,
    QGraphicsProxyWidget, QGraphicsScene, QGraphicsView,
    QGridLayout, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QProgressBar, QPushButton,
    QTextBrowser, QVBoxLayout, QWidget,
)

from fourslice import config
from fourslice.gui.icons import lucide
from fourslice.gui.imports_page import (
    THEME, WHITE, INK, BODY, MUTED, VIOLET, PALE_VIOLET, BORDER, TRACK,
    FONT_STACK, PillButton, SEARCH_QSS, set_placeholder_color, get_search_qss,
)
from fourslice.replay_timeline import build_timeline, infer_game_type, pretty_species
from fourslice.storage import get_log_text, get_my_side, get_replayable_games
from fourslice.gui.sprite_fetcher import SpriteFetchWorker
from fourslice.sprites import (
    SUBSTITUTE_BACK_B64, SUBSTITUTE_FRONT_B64, SpriteStore,
)

def _get_combo_qss(tokens):
    return f"""
        QComboBox {{
            background-color: {tokens['TRACK']};
            color: {tokens['INK']};
            border: 1px solid {tokens['BORDER']};
            border-radius: 8px;
            padding: 4px 10px;
            font-family: {FONT_STACK};
            font-size: 13px;
            min-height: 32px;
        }}
        QComboBox:hover {{
            border-color: {tokens['VIOLET']};
        }}
        QComboBox QAbstractItemView {{
            background-color: {tokens['WHITE']};
            color: {tokens['INK']};
            border: 1px solid {tokens['BORDER']};
            selection-background-color: {tokens['PALE_VIOLET']};
            selection-color: {tokens['VIOLET']};
            border-radius: 8px;
            padding: 4px;
        }}
    """

_SLOT_ORDER = ("p1a", "p1b", "p2a", "p2b")

# Board rows are NOT fixed to p1/p2: the user's side is always placed
# on the bottom row (see _relayout_board), matching Showdown's layout
# from the viewer's point of view. No _ROWS table here -- positions
# are computed per game selection.

# (label, playback interval ms at 1x)
# (label, playback interval ms at 1x) -- the interval is also the gap
# between individually-replayed events during autoplay, so it sets how
# long a beat the player has between moves (item F: longer gap).
_SPEEDS = {
    "0.5x": 4500,
    "1x": 2200,
    "2x": 1100,
    "4x": 550,
}

# Gen-5 sprites are 96x96; the default animated set varies, so every
# sprite is scaled to a consistent square.
_SPRITE_SIZE = 144

# Board-animation timings / parameters for the Showdown-lite effects:
# attack surge, damage flash, smooth HP drain, faint fade-out, switch
# fade-in, mega-evolution flash, and idle bobbing. All per-slot, all
# QTimer/QVariantAnimation-driven on the UI thread, all optional
# (config.animate_board / the Settings dialog toggle).
_MOVE_FLASH_MS = 620
_HP_DRAIN_MS = 950
_FAINT_MS = 720
_SWITCH_FADE_MS = 480
_MEGA_FLASH_MS = 740
_BOB_PERIOD_S = 1.8
_BOB_AMPLITUDE = 2
_BOB_TICK_MS = 33

# Move-specific effect timings and lookups (see _render_frame): known
# moves play a bespoke animation (orb, beam, dash, shield, pulse), and
# everything else keeps the generic surge + target shake. All of it is
# drawn with styled QLabels on top of the board -- no art assets.
_EFFECT_PROJECTILE_MS = 560
_EFFECT_DASH_MS = 480
_EFFECT_IMPACT_MS = 360
# Shield pop-in duration only -- a protect-style shield then PERSISTS for
# the rest of its turn (retired by _clear_turn_effects when the next turn
# renders), so this is just how long the grow-in takes.
_EFFECT_SHIELD_MS = 220
_EFFECT_SELF_MS = 560

# Bespoke set-piece timings (whole effect, first frame to final fade).
_EFFECT_QUAKE_MS = 560
_EFFECT_LIGHTNING_MS = 460
_EFFECT_WAVE_MS = 640
_EFFECT_PSYCHIC_MS = 520
_EFFECT_FLAME_MS = 620
_EFFECT_FROST_MS = 540

_MOVE_FX = {
    # shielding (Protect and friends)
    "Protect": "protect", "Detect": "protect", "King's Shield": "protect",
    "Quick Guard": "protect", "Wide Guard": "protect", "Spiky Shield": "protect",
    "Baneful Bunker": "protect", "Obstruct": "protect", "Silk Trap": "protect",
    "Burning Bulwark": "protect", "Mat Block": "protect", "Crafty Shield": "protect",
    # the quick 1-2 jab
    "Fake Out": "fakeout", "Feint": "fakeout",
    # contact moves: the attacker dashes into the target
    "Close Combat": "dash", "Dragon Claw": "dash", "Flip Turn": "dash",
    "U-turn": "dash", "Knock Off": "dash", "Rapid Spin": "dash",
    "Aqua Jet": "dash", "Sucker Punch": "dash", "Play Rough": "dash",
    "Throat Chop": "dash", "Aqua Tail": "dash",
    "Body Press": "dash", "Body Slam": "dash", "Brave Bird": "dash",
    "Brick Break": "dash", "Bullet Punch": "dash",
    "Cross Chop": "dash", "Crunch": "dash", "Dig": "dash",
    "Dragon Rush": "dash", "Drain Punch": "dash", "Extreme Speed": "dash",
    "Fire Punch": "dash", "First Impression": "dash", "Flare Blitz": "dash",
    "Foul Play": "dash", "Gyro Ball": "dash", "Head Smash": "dash",
    "Heavy Slam": "dash", "Ice Fang": "dash", "Ice Punch": "dash",
    "Ice Shard": "dash", "Iron Head": "dash", "Iron Tail": "dash",
    "Low Kick": "dash", "Lunge": "dash", "Mach Punch": "dash",
    "Pluck": "dash", "Poison Jab": "dash", "Psycho Cut": "dash",
    "Quick Attack": "dash", "Rock Slide": "dash", "Sacred Sword": "dash",
    "Shadow Claw": "dash", "Skitter Smack": "dash", "Spirit Break": "dash",
    "Superpower": "dash", "Tackle": "dash",
    "Thunder Fang": "dash", "Volt Switch": "dash",
    "Waterfall": "dash", "Wild Charge": "dash", "X-Scissor": "dash",
    "Zen Headbutt": "dash",
    # heavy impacts: a lunge PLUS a ground burst on landing
    "Explosion": "slam", "Giga Impact": "slam",
    "Outrage": "slam", "Self-Destruct": "slam",
    # ground-shaking moves: shake the whole board + open fissures
    "Earthquake": "earthquake", "Bulldoze": "earthquake",
    "High Horsepower": "earthquake", "Stomping Tantrum": "earthquake",
    "Fissure": "earthquake", "Magnitude": "earthquake",
    "Precipice Blades": "earthquake",
    # lightning strikes from the sky
    "Thunderbolt": "thunder", "Thunder": "thunder",
    "Zap Cannon": "thunder", "Volt Tackle": "thunder",
    "Bolt Strike": "thunder",
    # tidal waves sweep across the field
    "Surf": "wave", "Muddy Water": "wave", "Wave Crash": "wave",
    "Origin Pulse": "wave", "Hydro Pump": "wave",
    # psychic distortion rings ripple over the target
    "Psychic": "psychic", "Psyshock": "psychic", "Psybeam": "psychic",
    "Expanding Force": "psychic", "Future Sight": "psychic",
    "Mist Ball": "psychic", "Luster Purge": "psychic",
    # fire blasts detonate into a flame star
    "Fire Blast": "flame_blast", "Eruption": "flame_blast",
    "Sacred Fire": "flame_blast", "Pyro Ball": "flame_blast",
    "Magma Storm": "flame_blast",
    # rapid multi-hit barrages (Close Combat and friends)
    "Surging Strikes": "barrage", "Population Bomb": "barrage",
    "Triple Axel": "barrage", "Scale Shot": "barrage",
    "Dual Wingbeat": "barrage",
    # ice crystals streak to a frozen flash
    "Ice Beam": "frost", "Blizzard": "frost", "Freeze-Dry": "frost",
    "Glaciate": "frost", "Ice Spinner": "frost",
    # projectiles: a colored orb flies across the board
    "Moonblast": "projectile", "Shadow Ball": "projectile",
    "Energy Ball": "projectile", "Sludge Bomb": "projectile",
    "Aura Sphere": "projectile", "Flamethrower": "projectile",
    "Last Respects": "projectile", "Dark Pulse": "projectile",
    "Dragon Pulse": "projectile",
    "Flash Cannon": "projectile", "Focus Blast": "projectile",
    "Hex": "projectile", "Mystical Fire": "projectile", "Power Gem": "projectile",
    "Hurricane": "projectile",
    # big orbs that DETONATE on arrival
    "Bug Buzz": "burst", "Dragon Energy": "burst",
    "Overheat": "burst", "Tera Blast": "burst",
    # beams: a wide energy streak
    "Heat Wave": "beam", "Hyper Voice": "beam", "Dazzling Gleam": "beam",
    "Solar Beam": "beam", "Discharge": "beam",
    "Earth Power": "beam", "Icy Wind": "beam", "Sludge Wave": "beam",
    "Weather Ball": "beam",
    # nova: a beam that ERUPTS at the far end
    "Hyper Beam": "nova", "Water Spout": "nova",
    # setup / self-boost: a pulse around the mon
    "Shell Smash": "self", "Swords Dance": "self", "Nasty Plot": "self",
    "Calm Mind": "self", "Dragon Dance": "self", "Quiver Dance": "self",
    "Tailwind": "self", "Agility": "self", "Bulk Up": "self",
    "Acupressure": "self", "Coil": "self", "Curse": "self",
    "Defense Curl": "self", "Hone Claws": "self", "Iron Defense": "self",
    "Work Up": "self",
}

# Protect-family names used both for narration ("X protected itself!") and
# for the fail branch of the shield animation.
_PROTECT_DISPLAY = frozenset({
    "Protect", "Detect", "King's Shield", "Spiky Shield", "Baneful Bunker",
    "Obstruct", "Silk Trap", "Burning Bulwark", "Wide Guard", "Quick Guard",
    "Mat Block", "Crafty Shield",
})

# Showdown's short stat tokens (from |-boost|/-unboost| lines) to the
# friendly names used in the movelog narration and the floating badge.
_STAT_NAMES = {
    "atk": "Attack", "def": "Defense", "spa": "Sp. Atk", "spd": "Sp. Def",
    "spe": "Speed", "acc": "accuracy", "eva": "evasiveness",
}

# How far a Substitute doll's real mon is shifted sideways so it reads as
# hiding behind the doll (left for your side, right for the opponent's).
_SUB_SHIFT = 16

# How much a substitute-user's real sprite fades while its doll is up
# (1.0 = fully visible, 0.0 = invisible). Kept around half so the mon still
# reads as peeking out from behind the doll while the doll reads as the
# active body.
_SUB_OPACITY = 0.5

_PROJECTILE_COLORS = {
    "Moonblast": "#e573b0", "Shadow Ball": "#7b4fbf",
    "Last Respects": "#b39ddb", "Energy Ball": "#66bb6a",
    "Sludge Bomb": "#8d6e63", "Aura Sphere": "#81d4fa",
    "Flamethrower": "#ff8a50", "Ice Beam": "#7fd8ff",
    "Thunderbolt": "#ffd54f", "Heat Wave": "#ff7043",
    "Hyper Voice": "#ffb74d", "Dazzling Gleam": "#f48fb1",
    "Solar Beam": "#aed581",
    "Dark Pulse": "#6a4fa3", "Dragon Pulse": "#5aa0e8",
    "Fire Blast": "#ff5722", "Flash Cannon": "#b0bec5",
    "Focus Blast": "#ffca28", "Hex": "#8e24aa",
    "Mystical Fire": "#7e57c2", "Power Gem": "#f48fb1",
    "Psyshock": "#d39ddb", "Hurricane": "#90caf9",
    "Bug Buzz": "#aed581", "Dragon Energy": "#7986cb",
    "Eruption": "#ff7043", "Tera Blast": "#4fc3f7",
    "Overheat": "#ff3d00", "Blizzard": "#b3e5fc",
    "Discharge": "#ffee58", "Earth Power": "#a1887f",
    "Icy Wind": "#4fc3f7", "Sludge Wave": "#795548",
    "Surf": "#29b6f6", "Weather Ball": "#ffab91",
    "Hyper Beam": "#ef5350", "Water Spout": "#4fc3f7",
}
_PROJECTILE_FALLBACK = "#ffffff"

# Board-condition scene: the tint, icons, and chip colors for the active
# weather / terrain / room and per-side conditions (from the timeline's
# display strings, e.g. "Harsh sunlight", "Grassy Terrain", "Trick Room").
_BOARD_TINT_DEFAULT = TRACK
_TINT_BY_CONDITION = {
    "Harsh sunlight": "#fff3d6",
    "Rain": "#e3f0ff",
    "Sandstorm": "#f5ead6",
    "Hail": "#eef6ff",
    "Snow": "#eef6ff",
    "Electric Terrain": "#f7f3c9",
    "Grassy Terrain": "#e6f6e3",
    "Misty Terrain": "#f7e3f0",
    "Psychic Terrain": "#f0e3f7",
    "Trick Room": "#efe3f7",
    "Gravity": "#f0f0f0",
    "Magic Room": "#f7eef7",
    "Wonder Room": "#eef4f7",
}
_SIDE_COND_COLORS = {
    "Tailwind": "#0288d1", "Reflect": "#5e35b1", "Light Screen": "#f9a825",
    "Aurora Veil": "#ab47bc", "Safeguard": "#43a047", "Mist": "#90a4ae",
    "Spikes": "#e65100", "Stealth Rock": "#6d4c41",
    "Toxic Spikes": "#7b1fa2", "Sticky Web": "#37474f",
}


def _possessive(name: str) -> str:
    """Player name -> possessive form for the narrative: 'salmoncashew'
    -> "salmoncashew's", 'chris' -> "chris'\"."""
    if not name:
        return name
    return name + "'" if name.endswith("s") else name + "'s"


def _blend_colors(*hex_colors: str) -> str:
    """Averages #rrggbb colors into one -- the board tint when weather
    AND terrain (AND a room) are active at once."""
    parts = [tuple(int(c[i:i + 2], 16) for i in (1, 3, 5)) for c in hex_colors]
    if not parts:
        return _BOARD_TINT_DEFAULT
    n = len(parts)
    avg = tuple(sum(p[i] for p in parts) // n for i in range(3))
    return "#%02x%02x%02x" % avg


def _condition_chip(name: str, color: str, remaining=None, layers=None) -> str:
    """One plain-text chip for the conditions strip, colored by kind.
    `remaining` (when known) shows the turn countdown next to the name,
    e.g. 'Harsh sunlight (4)' -- passed in by _update_conditions from the
    timeline's weather/terrain/room/side_condition *_remaining counts.
    `layers` is the stack depth of a layered hazard (Spikes, Toxic
    Spikes) and renders as a ×N suffix, e.g. 'Spikes ×2' -- two layers of
    spikes are visibly different from one."""
    text = f"{name} ×{layers}" if layers else name
    if remaining:
        text = f"{text} ({remaining})"
    return f'<span style="color:{color};font-weight:bold;">{text}</span>'


# ---------------------------------------------------------------- painters
#
# Bespoke move FX (earthquake cracks, lightning bolts, tidal-wave walls,
# fire stars, ice shards) are painted into transparent QPixmaps by these
# module-level helpers and shown as plain QLabels -- the same trick the
# beam projectile uses, with no extra widget classes.

# Deterministic zig-zag step tables: every replay draws the same cracks
# and bolts instead of a random-looking jumble.
_CRACK_ZIGS = ((-14, -10), (12, -14), (-16, -16), (12, -12), (-10, -16))
_BOLT_ZIGS = ((20, 26), (-24, 20), (28, 24), (-18, 18), (12, 14))


def _trace_zig(path, x_s, y_s, zigs, scale=1.0):
    """Extends `path` from (x_s, y_s) along each (dx, dy) step of `zigs`,
    returning the final (x, y)."""
    for dzx, dzy in zigs:
        x_s += dzx * scale
        y_s += dzy * scale
        path.lineTo(x_s, y_s)
    return x_s, y_s


def _quake_lurch(board, base, progress):
    """One tick of the earthquake's whole-board shake: a decaying
    low-frequency sine wiggle, at amplitude ~0 at both ends."""
    decay = 1.0 - progress
    dx = int(9 * decay * math.sin(progress * math.pi * 7))
    dy = int(6 * decay * math.sin(progress * math.pi * 9))
    board.move(base.x() + dx, base.y() + dy)


def _make_crack_pixmap(base_w, base_h):
    """The jagged charred fissure + warm glowing core that opens in the
    ground under a target when an earthquake hits."""
    pix = QPixmap(base_w, base_h)
    pix.fill(QColor(0, 0, 0, 0))
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    cx = base_w // 2
    path = QPainterPath(QPointF(cx, base_h - 6))
    _trace_zig(path, cx, base_h - 6, _CRACK_ZIGS, 2.4)
    p.setPen(QPen(QColor(60, 36, 15, 235), 3))
    p.drawPath(path)
    p.setPen(QPen(QColor(255, 190, 100, 80), 1))
    p.drawPath(path)
    # The glowing break: a hot core at the base of the crack.
    p.setPen(Qt.NoPen)
    p.setBrush(QBrush(QColor(255, 168, 79, 150)))
    p.drawEllipse(QPointF(cx, base_h - 4), 26, 9)
    p.setBrush(QBrush(QColor(140, 88, 38, 130)))
    p.drawEllipse(QPointF(cx, base_h - 8), 40, 16)
    # Broken-ground flecks scattered around the split.
    p.setBrush(QColor(92, 60, 30, 225))
    for fx, fy in ((cx - 42, base_h - 10), (cx + 34, base_h - 20),
                   (cx - 52, base_h - 2), (cx + 14, base_h - 4)):
        p.drawEllipse(QPointF(fx, fy), 3, 3)
    p.end()
    return pix


def _make_bolt_pixmap(bolt_w, bolt_h):
    """A two-forked lightning bolt on a transparent pixmap."""
    pix = QPixmap(bolt_w, bolt_h)
    pix.fill(QColor(0, 0, 0, 0))
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    scale = bolt_h / 150.0
    main = QPainterPath(QPointF(bolt_w * 0.5 - 8, 0))
    _trace_zig(main, bolt_w * 0.5 - 8, 0, _BOLT_ZIGS, scale)
    # A secondary fork springs off the upper shaft.
    fork = QPainterPath(QPointF(bolt_w * 0.5 - 2, bolt_h * 0.3))
    _trace_zig(fork, bolt_w * 0.5 - 2, bolt_h * 0.3,
               ((26, 8), (16, 24), (14, 8)), scale)
    p.setPen(QPen(QColor(255, 176, 30, 140), 5))
    p.drawPath(main)
    p.drawPath(fork)
    p.setPen(QPen(QColor(255, 250, 170, 235), 2))
    p.drawPath(main)
    p.setPen(QPen(QColor(255, 240, 160, 110), 2))
    p.drawPath(fork)
    p.end()
    return pix


def _make_wave_wall_pixmap(width, height):
    """A vertical wall of sea: deep-to-foam gradient with two foam crests
    and flecks, ready to sweep across the field (Surf / Hydro Pump)."""
    pix = QPixmap(max(1, width), max(1, height))
    pix.fill(QColor(0, 0, 0, 0))
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    grad = QLinearGradient(0, 0, 0, height)
    grad.setColorAt(0.0, QColor(180, 220, 255, 30))
    grad.setColorAt(0.35, QColor(30, 130, 220, 160))
    grad.setColorAt(0.8, QColor(24, 88, 168, 205))
    grad.setColorAt(1.0, QColor(8, 40, 96, 220))
    p.fillRect(QRect(0, 0, width, height), QBrush(grad))
    for crest_y, alpha, w in ((height * 0.45, 255, 3), (height * 0.68, 170, 2)):
        path = QPainterPath(QPointF(0, crest_y))
        for i in range(1, 15):
            x_p = width * i / 14
            y_p = crest_y + math.sin(i / 14.0 * math.pi * 4) * height * 0.16
            path.lineTo(x_p, y_p)
        p.setPen(QPen(QColor(255, 255, 255, alpha), w))
        p.drawPath(path)
    p.setBrush(QColor(255, 255, 255, 190))
    for i in range(6):
        p.drawEllipse(QPointF(width * (i + 1) / 7.0, height * 0.28), 2, 2)
    p.end()
    return pix


def _make_flame_star_pixmap(size):
    """A 4-pointed fire star (Fire Blast's detonation) on transparent."""
    pix = QPixmap(size, size)
    pix.fill(QColor(0, 0, 0, 0))
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    c = size / 2.0
    p.setPen(Qt.NoPen)
    p.setBrush(QBrush(QColor(255, 96, 24, 80)))
    p.drawEllipse(QPointF(c, c), size * 0.42, size * 0.42)
    star = QPolygonF()
    inner = QPolygonF()
    for i in range(8):
        ang = i * math.pi / 4.0
        star.append(QPointF(c + math.cos(ang) * size * 0.5,
                            c + math.sin(ang) * size * 0.5))
        rad = size * 0.3 if i % 2 == 0 else size * 0.11
        inner.append(QPointF(c + math.cos(ang) * rad, c + math.sin(ang) * rad))
    p.setBrush(QBrush(QColor(255, 122, 32, 240)))
    p.drawPolygon(star)
    p.setBrush(QBrush(QColor(255, 220, 120, 240)))
    p.drawPolygon(inner)
    p.end()
    return pix


def _make_ice_shard_pixmap(size):
    """A six-armed snowflake flash (Ice Beam / Blizzard impact)."""
    pix = QPixmap(size, size)
    pix.fill(QColor(0, 0, 0, 0))
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    c = size / 2.0
    shard = QPolygonF()
    for i in range(12):
        ang = i * math.pi / 6.0 - math.pi / 2.0
        rad = size * 0.42 if i % 2 == 0 else size * 0.16
        shard.append(QPointF(c + math.cos(ang) * rad, c + math.sin(ang) * rad))
    p.setBrush(QBrush(QColor(215, 240, 255, 235)))
    p.setPen(QPen(QColor(140, 210, 255, 180), 1))
    p.drawPolygon(shard)
    p.setPen(Qt.NoPen)
    p.setBrush(QBrush(QColor(255, 255, 255, 235)))
    p.drawEllipse(QPointF(c, c), size * 0.1, size * 0.1)
    p.end()
    return pix


# ------------------------------------------------------------ scene art
#
# Field conditions are stripped to a plain board tint for now. The
# _scene_* painters stay unwired so that the shapes can be rebuilt
# on top of the tint later.
_ICON_W = 40
_ICON_H = 26
_ICON_GAP = 4


def _scene_weather(p, w, h, name):
    """The prevailing weather as Showdown-style scene dressing: a faint
    full-field wash for the light, then the signature element -- a bright
    sun with rays, slanting rain, a dusty sand band, or drifting flakes.
    Deterministic (index arithmetic, no randomness) so a frame always
    repaints identically."""
    if name == "Harsh sunlight":
        p.fillRect(QRectF(0, 0, w, h), QColor(255, 240, 196, 46))
        r = max(14.0, min(w, h) * 0.09)
        cx, cy = w * 0.82, h * 0.18
        p.setPen(QPen(QColor(255, 218, 118, 130), 3))
        p.setBrush(QBrush(QColor(255, 233, 152, 150)))
        p.drawEllipse(QPointF(cx, cy), r, r)
        p.setPen(QPen(QColor(255, 214, 90, 110), 2))
        for i in range(8):
            a = i * math.pi / 4.0
            p.drawLine(QPointF(cx + math.cos(a) * r * 1.7, cy + math.sin(a) * r * 1.7),
                       QPointF(cx + math.cos(a) * r * 2.4, cy + math.sin(a) * r * 2.4))
    elif name == "Rain":
        p.fillRect(QRectF(0, 0, w, h), QColor(120, 165, 225, 36))
        p.setPen(QPen(QColor(150, 195, 255, 115), 1))
        stepx = max(16.0, w / 16.0)
        x0 = stepx * 0.5
        i = 0
        while x0 < w:
            y0 = (i * 53) % int(h)          # stagger the columns
            p.drawLine(QPointF(x0, y0), QPointF(x0 - 6, y0 + 16))
            x0 += stepx
            i += 1
    elif name == "Sandstorm":
        p.fillRect(QRectF(0, 0, w, h), QColor(222, 190, 140, 36))
        p.setPen(QPen(QColor(204, 168, 116, 130), 2))
        ny = 5
        step = h / (ny + 1)
        for j in range(ny):
            y = (j + 1) * step
            off = (j * 47) % 60
            p.drawLine(QPointF(off, y), QPointF(off + 46, y))
            p.drawLine(QPointF(off + 96, y + 8), QPointF(off + 158, y + 8))
        p.setPen(Qt.NoPen)                    # dusty band hugging the ground
        p.setBrush(QBrush(QColor(196, 164, 116, 84)))
        p.drawRect(QRectF(0, h * 0.62, w, h * 0.09))
    elif name in ("Hail", "Snow"):
        p.fillRect(QRectF(0, 0, w, h), QColor(226, 240, 255, 36))
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(QColor(238, 250, 255, 150)))
        stepx = max(20.0, w / 12.0)
        x0 = stepx * 0.5
        i = 0
        while x0 < w:
            y0 = (i * 71) % int(h)          # scatter, not a neat row
            p.drawEllipse(QPointF(x0, y0), 2.8, 2.8)
            x0 += stepx
            i += 1


def _scene_terrain(p, w, h, name):
    """Showdown's terrain reads as a distinct floor laid across the ground
    with its own motif -- the cyan circuit grid, grass tufts, magenta
    waves, or a soft teal mist. The band hugs the base of the field (behind
    the sprites), with a brightening under the tiles and the pattern on
    top."""
    floor_h = h * 0.30
    base = h - floor_h
    colors = {
        "Electric Terrain": QColor(64, 190, 220, 100),
        "Grassy Terrain": QColor(96, 200, 92, 96),
        "Misty Terrain": QColor(150, 190, 214, 92),
        "Psychic Terrain": QColor(214, 84, 210, 96),
    }
    fill = colors.get(name, QColor(150, 160, 175, 90))
    p.setPen(Qt.NoPen)
    p.setBrush(QBrush(fill))
    p.drawRect(QRectF(0, base, w, floor_h))
    p.setPen(QPen(QColor(255, 255, 255, 70), 1))   # crisp upper edge
    p.drawLine(QPointF(0, base), QPointF(w, base))

    if name == "Electric Terrain":
        p.setPen(QPen(QColor(220, 250, 255, 130), 1))
        g = 42.0
        x = g
        while x < w:
            p.drawLine(QPointF(x, base), QPointF(x, h))
            x += g
        y = base + g * 0.5
        while y < h:
            p.drawLine(QPointF(0, y), QPointF(w, y))
            y += g
        p.setPen(Qt.NoPen)                          # the circuit nodes
        p.setBrush(QBrush(QColor(235, 255, 255, 170)))
        yy = base + g * 0.5
        while yy < h:
            xx = g
            while xx < w:
                p.drawEllipse(QPointF(xx, yy), 2.0, 2.0)
                xx += g
            yy += g
    elif name == "Grassy Terrain":
        p.setPen(QPen(QColor(70, 150, 80, 160), 1))
        stepx = 24.0
        x = stepx * 0.5
        i = 0
        while x < w:
            y = base + (i % 3) * 6
            p.drawLine(QPointF(x, y + 10), QPointF(x - 3, y))
            p.drawLine(QPointF(x, y + 10), QPointF(x + 3, y))
            x += stepx
            i += 1
    elif name == "Psychic Terrain":
        p.setPen(QPen(QColor(255, 205, 250, 160), 2))
        stepx = 26.0
        x0 = 4.0
        i = 0
        while x0 < w - 10:
            y0 = base + 10 + (i % 2) * 9
            p.drawArc(QRectF(x0, y0 - 8, 26, 16), 180 * 16, 180 * 16)
            x0 += stepx
            i += 1
    elif name == "Misty Terrain":
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(QColor(224, 242, 255, 95)))
        stepx = 64.0
        x = stepx * 0.5
        while x < w:
            p.drawEllipse(QPointF(x, base + floor_h * 0.42), 18, 9)
            x += stepx


def _scene_room(p, w, h, name):
    """A Showdown-style room frame: a dim open rectangle ruled in brackets
    in the middle of the field -- the familiar '[" _ "]"' silhouettes for
    Trick Room -- tinted per room. Huge rooms sit inside the tile area
    (behind the sprites), so it reads as the room being 'in effect'."""
    ring = QColor(200, 120, 250, 160)             # Trick Room magenta
    if name == "Gravity":
        ring = QColor(140, 150, 175, 130)
    elif name == "Magic Room":
        ring = QColor(120, 180, 250, 140)
    elif name == "Wonder Room":
        ring = QColor(120, 214, 190, 140)
    bw = w * 0.50
    bh = h * 0.40
    x0 = (w - bw) / 2.0
    y0 = (h - bh) / 2.0
    x1 = x0 + bw
    y1 = y0 + bh
    p.fillRect(QRectF(x0, y0, bw, bh),
               QColor(ring.red(), ring.green(), ring.blue(), 26))  # faint wash
    p.setPen(QPen(ring, 3))
    p.setBrush(Qt.NoBrush)
    m = 24.0
    # left / right brackets
    p.drawLine(QPointF(x0, y0 + m), QPointF(x0, y1 - m))
    p.drawLine(QPointF(x1, y0 + m), QPointF(x1, y1 - m))
    # the four corner ticks completing the open frame
    p.drawLine(QPointF(x0, y0), QPointF(x0 + m, y0))
    p.drawLine(QPointF(x1 - m, y0), QPointF(x1, y0))
    p.drawLine(QPointF(x0, y1), QPointF(x0 + m, y1))
    p.drawLine(QPointF(x1 - m, y1), QPointF(x1, y1))


# Screens (and the Aurora Veil wall) render as translucent slabs over the side
# that set them -- Showdown's full-height glow. Grounded hazards sit as a row
# of pictograms along that side's outer field edge, layer-accurate (Spikes x3
# draws three spikes). _HAZARD_ORDER keeps multiple dry hazards tiling in a
# deterministic left-to-right order; _SCREEN_NAMES is the draw order so an
# Aurora Veil won't stack on top of a Reflect.
_SCREEN_NAMES = ("Reflect", "Light Screen", "Aurora Veil")
_HAZARD_ORDER = ("Stealth Rock", "Spikes", "Toxic Spikes", "Sticky Web")
# Clearance (px) the user-side hazard row keeps from the board's bottom
# edge so it sits above the HP bars pinned to the tile bottoms.
_HAZARD_EDGE_CLEAR = 100
# Gap (px) kept between a Pokémon's tile and that side's hazard row when
# anchoring hazards to the tile in singles (opponent: below the tile,
# user: above it).  Kept for backward compatibility but no longer used
# by the positioning logic (hazards now sit directly on top of the tile).
_HAZARD_TILE_GAP = 12
# Painter opacity applied to every hazard strip so the icons are always
# mildly transparent -- visible but not occluding the sprites beneath.
_HAZARD_OPACITY = 0.6


def _paint_side_screens(p, w, h, side_conditions, bottom_side):
    """Draws each side's screen (Reflect / Light Screen / Aurora Veil) as
    a translucent wall over that side's half of the board, the brighter
    inner edge reading as a vertical barrier. Screens render in the
    hazards+screens layer above the sprites and move FX but below the UI,
    at ~50% opacity. Staggered screens blend additively into a slightly
    stronger glow."""
    if not side_conditions:
        return
    for side in ("p1", "p2"):
        names = side_conditions.get(side, ())
        if not names:
            continue
        is_bottom = side == bottom_side
        half = h * 0.5
        y0 = half if is_bottom else 0.0
        y1 = h if is_bottom else half
        for screen in _SCREEN_NAMES:
            if screen not in names:
                continue
            col = QColor(_SIDE_COND_COLORS.get(screen, "#888888"))
            col.setAlpha(128)  # ~50% as requested; above sprites, below UI
            p.fillRect(QRectF(0, y0, w, y1 - y0), col)
            edge = QColor(col)
            edge.setAlpha(190)
            p.setPen(QPen(edge, 2))
            p.drawLine(QPointF(0, y0), QPointF(w, y0))


def _paint_side_hazards(p, w, h, side_conditions, side_layers, bottom_side, layout=None):
    """Draws each side's grounded entry hazards (Stealth Rock, Spikes,
    Toxic Spikes, Sticky Web) as a row of pictograms beside that side's
    Pokémon.  Every strip is drawn at ``_HAZARD_OPACITY`` so the icons
    are always mildly transparent and never fully occlude the sprites
    underneath.  Where the row lands follows the board `layout`:
      * singles  -- centered directly on top of that side's lone Pokémon
        tile (overlapping the sprite), so hazards sit right where the
        mon is without drifting into open space;
      * doubles  -- dead-center of that side's tile band (vertical AND
        horizontal), reading clearly between the two mons and always
        clear of the name/HP bars pinned to the row's outer edge.
    `layout` is None until the board has real geometry; the painter then
    falls back to the old edge-pinned rows so a pre-layout paint stays
    stable. Layer counts come from `side_layers`, so Spikes x3 paints
    three spikes and reads clearly distinct from Spikes x1."""
    if not side_conditions:
        return
    prev_opacity = p.opacity()
    p.setOpacity(_HAZARD_OPACITY)
    for side in ("p1", "p2"):
        names = side_conditions.get(side, ())
        if not names:
            continue
        layers = (side_layers or {}).get(side, {})
        is_bottom = side == bottom_side
        hazards = [n for n in _HAZARD_ORDER if n in names]
        if not hazards:
            continue
        strip = _make_side_strip(hazards, layers)
        if strip.isNull():
            continue
        if layout is not None:
            band = layout["bottom"] if is_bottom else layout["top"]
            if band.width() <= 0 or band.height() <= 0:
                continue
            if layout.get("singles"):
                # Centered directly on top of the Pokémon tile.
                a_rect = layout["bottom_a"] if is_bottom else layout["top_a"]
                x = a_rect.x() + (a_rect.width() - strip.width()) // 2
                y = a_rect.y() + (a_rect.height() - strip.height()) // 2
            else:
                # Doubles: dead center of the side's tile band.
                x = band.x() + (band.width() - strip.width()) // 2
                y = band.y() + (band.height() - strip.height()) // 2
            x = max(0, min(x, w - strip.width()))
            p.drawPixmap(x, y, strip)
            continue
        x = (w - strip.width()) // 2
        y = (h - strip.height() - _HAZARD_EDGE_CLEAR) if is_bottom else 4
        p.drawPixmap(x, y, strip)
    p.setOpacity(prev_opacity)


def _make_scene_overlay(w, h, weather, terrain, room):
    """The board's painted background scenery: the weather/terrain/room
    motifs (the board tint carries their gist; these add the shapes).
    Pinned lowest, behind everything. Returns a null pixmap -- which the
    board hides -- when the board has no laid-out size yet or there's no
    scenery to draw."""
    if w < 10 or h < 10:
        return QPixmap()
    if not (weather or terrain or room):
        return QPixmap()
    pix = QPixmap(w, h)
    pix.fill(QColor(0, 0, 0, 0))
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    if weather:
        _scene_weather(p, w, h, weather)
    if terrain:
        _scene_terrain(p, w, h, terrain)
    if room:
        _scene_room(p, w, h, room)
    p.end()
    return pix


def _make_hazard_overlay(w, h, side_conditions, side_layers, bottom_side="p1", layout=None):
    """The field's entry hazards AND screens, painted into one transparent
    pixmap raised above the sprites and move FX but below the UI.  Screens
    are ~50% opacity walls; hazards are drawn at ``_HAZARD_OPACITY`` so
    their icons are always mildly transparent.  `layout` (see
    _paint_side_hazards) anchors the hazard rows to the real tile
    geometry: centered on each side's Pokémon tile in singles, dead-
    centered on each side's tile band in doubles.  Falls back to
    edge-pinned rows before the board has a laid-out size.  A null pixmap
    -- which the board hides -- when there's no laid-out size yet or
    neither side has any hazard or screen up."""
    if w < 10 or h < 10:
        return QPixmap()
    if not side_conditions:
        return QPixmap()
    pix = QPixmap(w, h)
    pix.fill(QColor(0, 0, 0, 0))
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    _paint_side_screens(p, w, h, side_conditions, bottom_side)
    _paint_side_hazards(p, w, h, side_conditions, side_layers, bottom_side, layout)
    p.end()
    return pix


# ------------------------------------------------- condition icon painters
#
# Each side's conditions get a painted pictogram row under the player name
# (the same QPixmap-into-QLabel trick as the move FX) so hazards, screens,
# and weather-likes read at a glance. Spikes and Toxic Spikes draw their
# EXACT layer count -- one/two/three spikes, one/two toxin pools -- so a
# 1-layer field is clearly lighter than a fully stacked one.

def _icon_spikes(p, layers):
    """N steel spikes hammered into the ground line."""
    step = 11
    start = max(2, (_ICON_W - (layers - 1) * step) // 2)
    p.setBrush(QBrush(QColor(230, 90, 20, 215)))
    p.setPen(QPen(QColor(255, 190, 60, 200), 1))
    for i in range(layers):
        cx = start + i * step
        p.drawPolygon(QPolygonF([QPointF(cx - 5, 20), QPointF(cx + 5, 20),
                                 QPointF(cx, 9)]))
    p.setPen(QPen(QColor(140, 60, 10, 170), 2))
    p.drawLine(QPointF(2, 21), QPointF(_ICON_W - 2, 21))


def _icon_toxic_spikes(p, x):
    """One or two seeping toxin pools (then stray droplets)."""
    layers = x
    step = 17
    start = max(6, (_ICON_W - (layers - 1) * step) // 2)
    for i in range(layers):
        cx = start + i * step
        p.setBrush(QBrush(QColor(123, 31, 162, 200)))
        p.setPen(QPen(QColor(160, 40, 190, 160), 1))
        p.drawEllipse(QPointF(cx, 16), 6, 5)
        p.setBrush(QBrush(QColor(180, 90, 210, 200)))
        p.setPen(Qt.NoPen)
        p.drawEllipse(QPointF(cx - 1, 14), 3, 3)
    p.setPen(QPen(QColor(150, 60, 180, 140), 1))
    p.drawEllipse(QPointF(8, 12), 2, 2)
    p.drawEllipse(QPointF(_ICON_W - 8, 17), 2, 2)


def _icon_stealth_rock(p, layers):
    """A cluster of levitating jagged boulders above the baseline."""
    rocks = ((10, 13, 1.0), (24, 9, 0.8), (17, 19, 0.6))
    p.setPen(QPen(QColor(60, 40, 30, 190), 1))
    for i, (dx, dy, s) in enumerate(rocks):
        r2 = 5.0 * s
        ang = i * 0.9
        q = QPolygonF()
        for k in range(4):
            a = ang + k * math.pi / 2.0
            q.append(QPointF(dx + math.cos(a) * r2, dy + math.sin(a) * r2))
        p.setBrush(QBrush(QColor(150 + i * 12, 110 + i * 8, 76 + i * 6, 220)))
        p.drawPolygon(q)
    p.setPen(QPen(QColor(255, 236, 200, 110), 1))
    p.drawLine(QPointF(10, 26), QPointF(30, 26))


def _icon_sticky_web(p, layers):
    """A full web: dark hub circle, radial threads, ring arcs."""
    p.setBrush(QBrush(QColor(55, 71, 79, 210)))
    p.setPen(Qt.NoPen)
    p.drawEllipse(QPointF(20, 13), 15, 13)
    p.setPen(QPen(QColor(236, 239, 241, 220), 1))
    for a in range(8):
        ang = a * math.pi / 4.0
        p.drawLine(QPointF(20, 13),
                   QPointF(20 + math.cos(ang) * 14, 13 + math.sin(ang) * 12))
    for r2 in (4, 9, 13):
        p.drawArc(QRectF(20 - r2, 13 - r2 * 0.86, 2 * r2, 2 * r2 * 0.86),
                  0, 360 * 16)


def _icon_screen(p, layers):
    """A translucent barrier wall with a sheen (indigo for Reflect, gold
    for Light Screen -- the caller's pen/brush colors tell which)."""
    rect = QRectF(8, 4, 13, 18)
    grad = QLinearGradient(rect.topLeft(), rect.topRight())
    grad.setColorAt(0.0, p.pen().color().lighter(150))
    grad.setColorAt(1.0, p.pen().color())
    p.setBrush(grad)
    p.drawRoundedRect(rect, 2, 2)
    p.setPen(QPen(QColor(255, 255, 255, 95), 1))
    p.drawLine(QPointF(24, 6), QPointF(33, 12))
    p.drawLine(QPointF(22, 12), QPointF(31, 18))
    p.drawLine(QPointF(24, 19), QPointF(32, 23))


def _icon_aurora(p, layers):
    """Three drifting ribbons of light."""
    p.setPen(Qt.NoPen)
    for i, (yy, col) in enumerate(
            ((7, (86, 204, 140)), (13, (160, 210, 250)), (19, (220, 130, 200)))):
        p.setBrush(QBrush(QColor(*col, 150)))
        path = QPainterPath(QPointF(2, yy + 3))
        for x in range(0, _ICON_W + 3, 4):
            path.lineTo(QPointF(x, yy + 3 + math.sin(x * 0.6 + i) * 2.4))
        path.lineTo(QPointF(_ICON_W, yy + 3))
        path.closeSubpath()
        p.drawPath(path)


def _icon_safeguard(p, layers):
    """A protective aura bubble with spark ticks."""
    p.setPen(QPen(QColor(67, 160, 71, 190), 2))
    p.drawArc(QRectF(8, 4, 24, 18), 20 * 16, 140 * 16)
    p.setPen(Qt.NoPen)
    p.setBrush(QBrush(QColor(220, 250, 220, 160)))
    p.drawEllipse(QPointF(20, 13), 6, 5)
    p.setPen(QPen(QColor(150, 240, 160, 160), 1))
    for a in (0.4, 1.4, 2.4):
        p.drawLine(QPointF(20 + math.cos(a) * 8, 13 + math.sin(a) * 7),
                   QPointF(20 + math.cos(a) * 11, 13 + math.sin(a) * 10))


def _icon_mist(p, layers):
    """A soft white veil of cloud puffs."""
    p.setPen(Qt.NoPen)
    p.setBrush(QBrush(QColor(150, 168, 178, 150)))
    p.drawEllipse(QPointF(12, 16), 9, 5)
    p.setBrush(QBrush(QColor(190, 205, 215, 130)))
    p.drawEllipse(QPointF(24, 14), 10, 5)
    p.setBrush(QBrush(QColor(210, 224, 232, 110)))
    p.drawEllipse(QPointF(18, 9), 11, 4)


def _icon_tailwind(p, layers):
    """Two flat chevrons pointing the way the wind blows."""
    p.setPen(QPen(QColor(2, 136, 209, 200), 2))
    p.setBrush(Qt.NoBrush)
    for yy in (9, 16):
        tip = QPointF(_ICON_W - 8, yy)
        p.drawLine(QPointF(4, yy), QPointF(tip.x() - 6, yy))
        p.drawLine(tip, QPointF(tip.x() - 7, yy - 4))
        p.drawLine(tip, QPointF(tip.x() - 7, yy + 4))


def _icon_default(p, layers):
    """Fallback diamond tinted like the condition's chip color."""
    col = QColor(_SIDE_COND_COLORS.get(layers if isinstance(layers, str) else "",
                                       "#888888"))
    p.setPen(QPen(col, 2))
    p.setBrush(QBrush(QColor(col.red(), col.green(), col.blue(), 120)))
    cx, cy = _ICON_W / 2.0, _ICON_H / 2.0
    p.drawPolygon(QPolygonF([QPointF(cx, cy - 6), QPointF(cx + 6, cy),
                             QPointF(cx, cy + 6), QPointF(cx - 6, cy)]))


_ICON_DRAWERS = {
    "Spikes": _icon_spikes,
    "Toxic Spikes": _icon_toxic_spikes,
    "Stealth Rock": _icon_stealth_rock,
    "Sticky Web": _icon_sticky_web,
    "Reflect": _icon_screen,
    "Light Screen": _icon_screen,
    "Aurora Veil": _icon_aurora,
    "Safeguard": _icon_safeguard,
    "Mist": _icon_mist,
    "Tailwind": _icon_tailwind,
}


def _make_side_cond_icon(name, layers=1):
    """One 40x26 pictogram for a side condition. Layered hazards paint
    `layers` visible units so Spikes ×2 looks different from Spikes ×1."""
    pix = QPixmap(_ICON_W, _ICON_H)
    pix.fill(QColor(0, 0, 0, 0))
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    if name == "Reflect":
        p.setPen(QPen(QColor(126, 87, 194), 1))
    elif name == "Light Screen":
        p.setPen(QPen(QColor(230, 170, 40), 1))
    elif name in _ICON_DRAWERS:
        p.setPen(QPen(QColor(90, 90, 95), 1))
    _ICON_DRAWERS.get(name, _icon_default)(p, layers if layers else 1)
    p.end()
    return pix


def _make_side_strip(names, layers):
    """Joins one icon per active side condition into a single transparent
    strip for the side's icon label. Returns a null pixmap for no icons."""
    icons = [_make_side_cond_icon(name, layers.get(name, 1)) for name in names]
    if not icons:
        return QPixmap()
    total = _ICON_W * len(icons) + _ICON_GAP * (len(icons) - 1)
    pix = QPixmap(total, _ICON_H)
    pix.fill(QColor(0, 0, 0, 0))
    p = QPainter(pix)
    x = 0
    for icon in icons:
        p.drawPixmap(x, 0, icon)
        x += _ICON_W + _ICON_GAP
    p.end()
    return pix


class ReplaysTab(QWidget):
    def __init__(self, conn, usernames=None, sprite_store=None, auto_fetch_sprites=True, animate=None):
        super().__init__()
        self.conn = conn
        self._timeline = None
        self._frames = []
        self._frame_index = -1
        self._step_turn = None  # last finalized turn, for step-back

        # Sprite support. The store is created lazily on the
        # first fetch so tests (and a sprites-off install) never touch
        # the on-disk cache or the network.
        self._sprite_store = sprite_store
        self._auto_fetch_sprites = auto_fetch_sprites
        self._sprite_style = config.get_sprite_style()
        self._sprite_thread = None
        self._sprite_worker = None
        self._sprites_enabled = config.get_use_sprites()
        self._slot_movies = {}      # slot -> QMovie currently animating
        self._slot_frame_hooks = {} # slot -> frameChanged hook (disconnect handle)
        self._slot_sprite_key = {}  # slot -> (path, animated) currently shown

        # Which side is "you" for the currently selected game. Resolved
        # per game from configured usernames, falling back to the
        # importer's games.my_side result, then to p1 (see
        # _detect_back_side). The board always renders your side as the
        # bottom row with back-view sprites.
        self.usernames = set(usernames) if usernames else set()
        self._back_side = "p1"
        self._game_type = ""          # "singles"/"doubles" for the current game
        self._side_names = {"p1": "Player 1", "p2": "Player 2"}

        # Board animation (Showdown-lite). `animate` overrides the saved
        # setting so tests can force it one way or the other.
        self._animate = config.get_animate_board() if animate is None else animate
        self._prev_hp_pct = {}           # slot -> hp% from the previous frame
        self._synced_moves = set()       # (frame, move) whose damage was already drawn
        self._hit_events = {}            # (frame, damage-ev) -> hurt already shown on the move's beat
        self._animations = []            # live animations, kept referenced
        self._idle_bob_timers = {}       # slot -> QTimer driving the idle bob
        self._idle_bob_base = {}         # slot -> QRect the bob oscillates around
        self._slot_effect = {}           # slot -> QGraphicsEffect currently attached
        self._slot_effect_anim = {}      # slot -> animation driving that effect
        self._surge_restore = {}         # slot -> callable restoring label geometry
        self._pending_switch_in = set()  # slots whose current frame saw a switch
        self._pending_mega_flash = set() # slots that forme-changed this frame
        self._fading_slots = set()       # slots mid faint-fade (sprite still up)
        self._effect_widgets = []        # transient projectile/shield/burst labels
        self._shields = {}               # slot -> QLabel holdover shield (persists the turn)
        self._sub_dolls = {}             # slot -> QLabel Substitute doll (item E)
        self._sub_base = {}              # slot -> QRect the doll/mon sit on (layout base)
        self._sub_pixmaps = {}           # 'front'/'back' -> cached official doll pixmap
        self._hp_anims = {}              # slot -> QPropertyAnimation draining its bar
        self._board_laid_out = False     # true once the board has painted with real geometry

        # Autoplay plays the battle one event at a time (item B): _frame_index
        # pins the current turn and _event_index walks that turn's
        # battle-ordered |move|/switch/damage/... events (frame["events"]) one
        # per timer tick, so the movelog fills in in real replay order.
        self._event_index = 0

        # ONE background thread + worker serve the whole tab lifetime.
        # Recreating a QThread per fetch was a crash bug: when the old
        # thread was still blocked on the network, dropping its reference
        # destroyed the QThread while its OS thread ran, and Qt aborts
        # ("QThread: Destroyed while thread is still running"). Stop the
        # single thread before the app quits so that can never happen.
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self._shutdown_sprite_thread)
        
        # Apply theme-aware styling
        THEME.theme_changed.connect(self._apply_theme)
        self._tokens = THEME.get_tokens()

        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        layout = QVBoxLayout(self)


        # -- match header --
        header_layout = QVBoxLayout()
        header_layout.setSpacing(4)
        
        self.title_label = QLabel("Replays")
        self.title_label.setStyleSheet(f"""
            color: {INK};
            font-family: {FONT_STACK};
            font-size: 24px;
            font-weight: 700;
        """)
        header_layout.addWidget(self.title_label)
        
        self.subtitle_label = QLabel("Play back your battles turn-by-turn.")
        self.subtitle_label.setStyleSheet(f"""
            color: {MUTED};
            font-family: {FONT_STACK};
            font-size: 14px;
        """)
        header_layout.addWidget(self.subtitle_label)
        layout.addLayout(header_layout)

        # -- Toolbar Card --
        self.toolbar_card = QFrame()
        self.toolbar_card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        toolbar_layout = QHBoxLayout(self.toolbar_card)
        toolbar_layout.setContentsMargins(16, 12, 16, 12)
        toolbar_layout.setSpacing(12)

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Filter replays...")
        self.search_input.setFixedHeight(34)
        self.search_input.setStyleSheet(SEARCH_QSS)
        set_placeholder_color(self.search_input)
        search_icon = QIcon(lucide("search", MUTED, 16))
        self.search_input.addAction(search_icon, QLineEdit.ActionPosition.LeadingPosition)
        # Assuming you want to add filtering logic later, for now just placeholder
        toolbar_layout.addWidget(self.search_input, 1)

        # Sprite style selector (Gen 5 vs 3D/XY)
        self.sprite_style_combo = QComboBox()
        self.sprite_style_combo.addItems(["2D (Gen 5)", "3D (XY)"])
        self.sprite_style_combo.setCurrentText("Gen 5" if config.get_sprite_style() == "gen5" else "3D (XY)")
        self.sprite_style_combo.setFixedWidth(140)
        self.sprite_style_combo.setFixedHeight(34)
        self.sprite_style_combo.setStyleSheet(_get_combo_qss(THEME.get_tokens()))
        self.sprite_style_combo.currentTextChanged.connect(self._on_sprite_style_changed)
        toolbar_layout.addWidget(self.sprite_style_combo)

        refresh_icon = lucide("rotate-cw", WHITE, 16)
        self.refresh_btn = PillButton("Refresh", bg=VIOLET, fg=WHITE, height=36)
        self.refresh_btn.setIcon(QIcon(refresh_icon))
        self.refresh_btn.clicked.connect(self.refresh_replays)
        toolbar_layout.addWidget(self.refresh_btn)
        
        layout.addWidget(self.toolbar_card)

        # -- body: game list on the left, board on the right --
        body = QHBoxLayout()
        body.setSpacing(20)

        self.game_list = QListWidget()
        self.game_list.currentItemChanged.connect(self._on_game_selected)
        body.addWidget(self.game_list, 1)

        player_pane = QVBoxLayout()

        self.turn_label = QLabel("--")
        self.turn_label.setAlignment(Qt.AlignCenter)

        # -- board conditions strip: weather · terrain · rooms --
        self.conditions_label = QLabel("")
        self.conditions_label.setWordWrap(True)
        self.match_label = QLabel("")
        self.match_label.setAlignment(Qt.AlignCenter)
        self.match_label.setWordWrap(True)

        # Tight header stack: turn / battle name / conditions
        header_stack = QVBoxLayout()
        header_stack.setSpacing(2)
        header_stack.addWidget(self.turn_label)
        header_stack.addWidget(self.match_label)
        header_stack.addWidget(self.conditions_label)
        player_pane.addLayout(header_stack)

        self.conditions_label.setAlignment(Qt.AlignCenter)

        # -- board: 2x2 slots + a side-name label per row --
        # Each slot is a plain QWidget cell so _relayout_board can move
        # the cells between grid positions when a game's user is p2
        # (the user's side always ends up as the bottom row). The side
        # labels live in column 0 and show who's who. The board sits in
        # a QFrame so active conditions can tint the whole field.
        self.board_container = QFrame()
        self.board_container.setObjectName("boardContainer")
        self.board_container.setStyleSheet(
            f"background: {_BOARD_TINT_DEFAULT}; border-radius: 10px;"
        )
        self.board_container.setMinimumSize(0, 0)
        self.board_container.setMaximumSize(16777215, 16777215)
        self.board_container.resize(640, 480)
        self.board_grid = QGridLayout(self.board_container)
        self.board_grid.setContentsMargins(6, 6, 6, 6)
        self.board_grid.setColumnStretch(1, 1)
        self.board_grid.setColumnStretch(2, 1)
        self.board_grid.setRowStretch(0, 1)
        self.board_grid.setRowStretch(1, 1)
        self.board_labels = {}
        self.hp_bars = {}
        self.sprite_labels = {}
        self.cell_widgets = {}
        for slot in _SLOT_ORDER:
            cell = QWidget()
            # The tile is see-through so the weather/terrain scene (a
            # lowered sibling underneath) shows around and behind the
            # sprite. Without this, QStyleSheetStyle paints every styled,
            # borderless-background widget an opaque palette square that
            # hides the board art while keeping the same z-order.
            cell.setStyleSheet("background: transparent;")
            cell_box = QVBoxLayout(cell)
            cell_box.setContentsMargins(2, 2, 2, 2)
            sprite_label = QLabel()
            sprite_label.setAlignment(Qt.AlignCenter)
            sprite_label.setFixedSize(_SPRITE_SIZE, _SPRITE_SIZE)
            sprite_label.setVisible(False)
            self.sprite_labels[slot] = sprite_label
            slot_label = QLabel("--")
            slot_label.setAlignment(Qt.AlignCenter)
            slot_label.setStyleSheet(
                "font-weight: bold; padding: 4px; border: 1px solid #888;"
                "border-radius: 4px; background: transparent;"
            )
            self.board_labels[slot] = slot_label
            hp_bar = QProgressBar()
            hp_bar.setRange(0, 100)
            hp_bar.setValue(0)
            hp_bar.setFormat("")
            self.hp_bars[slot] = hp_bar
            cell_box.addWidget(sprite_label)
            cell_box.addWidget(slot_label)
            cell_box.addWidget(hp_bar)
            self.cell_widgets[slot] = cell
        self.side_labels = {}
        self.side_cond_labels = {}
        self.side_cond_icons = {}
        for side in ("p1", "p2"):
            side_label = QLabel("--")
            side_label.setAlignment(Qt.AlignCenter)
            self.side_labels[side] = side_label
            cond_label = QLabel("")
            cond_label.setAlignment(Qt.AlignCenter)
            cond_label.setWordWrap(True)
            self.side_cond_labels[side] = cond_label
            icon_label = QLabel("")
            icon_label.setAlignment(Qt.AlignCenter)
            icon_label.setFixedHeight(_ICON_H + 4)
            icon_label.setScaledContents(False)
            icon_label.hide()
            self.side_cond_icons[side] = icon_label

        self.scene_overlay = QLabel(self.board_container)  # weather/room/terrain
        self.scene_overlay.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.scene_overlay.setStyleSheet("background: transparent;")
        self.scene_overlay.hide()
        self.fx_layer = QWidget(self.board_container)                      # move animation FX
        self.fx_layer.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.fx_layer.setStyleSheet("background: transparent;")
        self.hz_overlay = QLabel(self.board_container)                     # hazards + screens
        self.hz_overlay.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.hz_overlay.setStyleSheet("background: transparent;")
        self.hz_overlay.hide()
        self.ui_layer = QWidget(self.board_container)      # names, HP bars, chips
        self.ui_layer.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.ui_layer.setStyleSheet("background: transparent;")
        self.ohko_label = QLabel(self.board_container)                     # "OHKO" celebration text
        self.ohko_label.setAlignment(Qt.AlignCenter)
        self.ohko_label.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.ohko_label.setStyleSheet(
            "color: #ffffff; background: rgba(0, 0, 0, 0.5);"
            "border-radius: 14px; font-size: 54pt; font-weight: 900;"
            "letter-spacing: 8px;"
        )
        self.ohko_label.hide()
        self._scene_conds = (0, 0, None, None, None)
        self._scene_side_conditions = {}   # side -> active condition names
        self._scene_side_layers = {}       # side -> {hazard name -> stack depth}
        self._relayout_board()

        self.scene = QGraphicsScene()
        self.proxy = self.scene.addWidget(self.board_container)
        self.board_view = QGraphicsView(self.scene)
        self.board_view.setRenderHint(QPainter.Antialiasing)
        self.board_view.setFrameShape(QFrame.NoFrame)
        self.board_view.setStyleSheet("background: transparent;")
        self.board_view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.board_view.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        # The whole board renders through a QGraphicsProxyWidget, so QMovie
        # frame updates only reach the screen when the viewport repaints them.
        # The default MinimalViewportUpdate mode can leave stale/blank regions
        # behind an animated sprite (the "blink in and out" symptom); repainting
        # the full viewport on every update keeps the QMovie always visible.
        # The board is only a few hundred pixels, so the cost is negligible.
        self.board_view.setViewportUpdateMode(
            QGraphicsView.ViewportUpdateMode.FullViewportUpdate
        )
        player_pane.addWidget(self.board_view, 1)

        # -- event narrative for the current turn --
        self.event_browser = QTextBrowser()
        self.event_browser.setMaximumHeight(140)
        self.event_browser.setOpenExternalLinks(False)
        player_pane.addWidget(self.event_browser)

        # -- transport controls --
        controls = QHBoxLayout()
        self.step_back_button = QPushButton("< Prev")
        self.step_back_button.setToolTip("Back to the previous turn")
        self.step_back_button.clicked.connect(self.step_back)
        controls.addWidget(self.step_back_button)

        self.play_button = QPushButton("Play")
        self.play_button.setToolTip("Play / pause autoplay")
        self.play_button.clicked.connect(self.toggle_play)
        controls.addWidget(self.play_button)

        self.step_forward_button = QPushButton("Next >")
        self.step_forward_button.setToolTip("Advance to the next turn")
        self.step_forward_button.clicked.connect(self.step_forward)
        controls.addWidget(self.step_forward_button)

        self.speed_combo = QComboBox()
        for label in _SPEEDS:
            self.speed_combo.addItem(label)
        self.speed_combo.setCurrentText("1x")
        self.speed_combo.currentTextChanged.connect(self._update_timer_interval)
        controls.addWidget(self.speed_combo)

        player_pane.addLayout(controls)
        body.addLayout(player_pane, 2)
        layout.addLayout(body, 1)

        # -- autoplay timer --
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self._update_timer_interval()
        self._update_controls_enabled()

        self._apply_theme()

        self.refresh_replays()

    def showEvent(self, event):
        """Flags the board as laid out so deferred idle bobs can start,
        and kick-starts any that were pending (see _start_idle_bob). The
        first show may arrive without a resize, so the board is laid out
        now too -- a later resizeEvent re-pins it."""
        super().showEvent(event)
        self._board_laid_out = True
        self._layout_board()
        QTimer.singleShot(0, self._ensure_idle_bobs)

    def _layout_board(self):
        """Sizes the board canvas to the view and re-pins the layered
        scene + UI to it: dynamic horizontal scaling (expanding the 480-tall
        canvas to fill the viewport, clamped 480-1200), re-rendering the
        painted backdrop when the canvas size changed, and fitting the view.
        Shared between resizeEvent and showEvent so opening the tab gives
        the same layout a window resize would -- the first open may never
        see a resize event."""
        if hasattr(self, 'board_view') and getattr(self, "board_container", None):
            # Dynamic horizontal scaling: if there's significant extra horizontal
            # space, expand the logical canvas width to fill it.
            vw = self.board_view.viewport().width()
            vh = self.board_view.viewport().height()
            if vh > 10:
                aspect = vw / vh
                # Baseline height is 480; compute width to fill the aspect ratio.
                new_w = int(480 * aspect)
                # Clamp between 480 (minimum squish) and 1200 (ultra-wide max).
                new_w = max(480, min(new_w, 1200))
                if new_w != self.board_container.width():
                    self.proxy.resize(new_w, 480)
                    self.board_container.resize(new_w, 480)

        overlay = getattr(self, "scene_overlay", None)
        if overlay is None:
            return
        self._place_scene_overlay()
        w, h = self.board_container.width(), self.board_container.height()
        if (w, h) != self._scene_conds[:2] and w >= 10 and h >= 10:
            self._paint_scene(self._scene_conds[2], self._scene_conds[3],
                              self._scene_conds[4])

        # Finally, fit the view to the updated scene canvas
        if hasattr(self, 'board_view') and getattr(self, 'proxy', None):
            self.scene.setSceneRect(self.proxy.sceneBoundingRect())
            self.board_view.fitInView(self.proxy, Qt.KeepAspectRatio)

    def resizeEvent(self, event):
        """Keeps the painted scene backdrop pinned to the board: when the
        board's layout grows or shrinks the scenery is re-rendered at the
        new size (or dropped once the board is gone)."""
        super().resizeEvent(event)
        self._layout_board()

    def _apply_theme(self, *_args):
        self._tokens = THEME.get_tokens()
        tokens = self._tokens
        self.setStyleSheet(f"background-color: {tokens['WHITE']};")
        
        # Header
        self.title_label.setStyleSheet(f"color: {tokens['INK']}; font-family: {FONT_STACK}; font-size: 24px; font-weight: 700;")
        self.subtitle_label.setStyleSheet(f"color: {tokens['MUTED']}; font-family: {FONT_STACK}; font-size: 14px;")

        # Toolbar
        card_qss = f"""
            QFrame {{
                background-color: {tokens['WHITE']};
                border: 1px solid {tokens['BORDER']};
                border-radius: 12px;
            }}
        """
        self.toolbar_card.setStyleSheet(card_qss)

        # Replay Control Buttons
        btn_qss = f"""
            QPushButton {{
                background-color: {tokens['WHITE']};
                color: {tokens['INK']};
                border: 1px solid {tokens['BORDER']};
                border-radius: 6px;
                padding: 5px 10px;
            }}
            QPushButton:hover {{ background-color: {tokens['TRACK']}; }}
        """
        self.step_back_button.setStyleSheet(btn_qss)
        self.play_button.setStyleSheet(btn_qss)
        self.step_forward_button.setStyleSheet(btn_qss)
        self.speed_combo.setStyleSheet(btn_qss)

        self.search_input.setStyleSheet(get_search_qss(tokens))
        set_placeholder_color(self.search_input, tokens['PLACEHOLDER'])

        # Body/List
        list_qss = f"""
            QListWidget {{
                background-color: {tokens['WHITE']};
                color: {tokens['INK']};
                border: 1px solid {tokens['BORDER']};
                border-radius: 12px;
                font-family: {FONT_STACK};
                font-size: 13px;
                outline: 0;
            }}
            QListWidget::item {{
                padding: 10px 14px;
                border-bottom: 1px solid {tokens['TRACK']};
            }}
            QListWidget::item:selected {{
                background-color: {tokens['PALE_VIOLET']};
                color: {tokens['VIOLET']};
            }}
        """
        self.game_list.setStyleSheet(list_qss)
        
        browser_qss = f"""
            QTextBrowser {{
                background-color: {tokens['TRACK']};
                color: {tokens['INK']};
                border: 1px solid {tokens['BORDER']};
                border-radius: 12px;
                font-family: {FONT_STACK};
                font-size: 13px;
                padding: 8px;
            }}
        """
        self.event_browser.setStyleSheet(browser_qss)

        # Board styling
        self.board_container.setStyleSheet(
            f"background: {tokens['TRACK']}; border: 2px solid {tokens['BORDER']}; border-radius: 12px;"
        )
        
        # Turn label and conditions
        self.turn_label.setStyleSheet(f"color: {tokens['INK']}; font-weight: bold; font-size: 16px;")
        self.conditions_label.setStyleSheet(f"color: {tokens['MUTED']}; font-size: 13px;")
        self.match_label.setStyleSheet(f"color: {tokens['INK']}; font-family: {FONT_STACK}; font-size: 15px; font-weight: 600;")

        # Slot labels
        for label in self.board_labels.values():
            label.setStyleSheet(f"""
                font-weight: bold; padding: 4px; border: 1px solid {tokens['BORDER']};
                border-radius: 4px; background: {tokens['WHITE']}; color: {tokens['INK']};
            """)
            
        # Side names
        for side, label in self.side_labels.items():
            color = tokens['VIOLET'] if side == self._back_side else tokens['MUTED']
            label.setStyleSheet(f"font-weight: bold; padding: 2px; color: {color};")
        for label in self.side_cond_labels.values():
            label.setStyleSheet(f"color: {tokens['MUTED']}; font-size: 11px;")

        # Sprite-style combo (theme-aware)
        self.sprite_style_combo.setStyleSheet(_get_combo_qss(tokens))

        # Re-render current frame to apply HP bar color updates
        if self._frame_index >= 0 and self._frames:
            self._render_frame()


    # ---------------------------------------------------------------- data

    def refresh_replays(self):
        """Reloads the playable-game list. Keeps the current selection
        if it's still playable; otherwise falls back to the top item."""
        self._sprites_enabled = config.get_use_sprites()
        if not self._sprites_enabled:
            for slot in _SLOT_ORDER:
                self._clear_slot_sprite(slot)
        current_id = None
        item = self.game_list.currentItem()
        if item is not None:
            current_id = item.data(Qt.UserRole)

        self.game_list.clear()
        games = get_replayable_games(self.conn)
        if not games:
            self.game_list.addItem("No replays stored yet. Enable 'Store raw replay logs' in Settings, then sync/import.")
            self._timeline = None
            self._frames = []
            self._frame_index = -1
            self._update_controls_enabled()
            return

        selected_row = 0
        for i, (game_id, p1, p2, winner, result, regulation, battle_size) in enumerate(games):
            result_text = result if result else "?"
            label = f"[{regulation} {battle_size}] {p1} vs {p2} -- winner: {winner} -- {result_text}"
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, game_id)
            self.game_list.addItem(item)
            if game_id == current_id:
                selected_row = i
        self.game_list.setCurrentRow(selected_row)

    # ------------------------------------------------------------ selection

    def _on_game_selected(self, current, _previous):
        if current is None:
            return
        game_id = current.data(Qt.UserRole)
        self._stop_all_animations()
        log_text = get_log_text(self.conn, game_id)
        if not log_text:
            self.match_label.setText("This game's log is no longer stored (pruned or disabled).")
            self._timeline = None
            self._frames = []
            self._frame_index = -1
            self._side_names = {"p1": "Player 1", "p2": "Player 2"}
            self._back_side = "p1"
            self._relayout_board()
            for slot in _SLOT_ORDER:
                self._clear_slot_sprite(slot)
            self._update_controls_enabled()
            return
        self._timeline = build_timeline(log_text)
        self._frames = self._timeline["frames"]
        p1_name = self._timeline.get("p1_name", "") or ""
        p2_name = self._timeline.get("p2_name", "") or ""
        self._side_names = {
            "p1": p1_name or "Player 1",
            "p2": p2_name or "Player 2",
        }
        self._back_side = self._detect_back_side(game_id, p1_name, p2_name)
        game_type = (self._timeline.get("gametype") or "").lower()
        if game_type not in ("singles", "doubles"):
            game_type = infer_game_type(self._frames)
        self._game_type = game_type
        self._relayout_board()  # your side goes to the bottom row
        self.match_label.setText(
            f"{self._timeline.get('p1_name', '?')} vs {self._timeline.get('p2_name', '?')}"
            f"{' -- winner: ' + self._timeline['winner'] if self._timeline.get('winner') else ''}"
            f"{'  [format: ' + self._timeline.get('format', '') + ']' if self._timeline.get('format') else ''}"
        )
        self._frame_index = 0
        self._event_index = 0
        self._render_frame()
        self._start_sprite_fetch()
        self._update_controls_enabled()

    # --------------------------------------------------------------- render

    def _frame(self):
        if 0 <= self._frame_index < len(self._frames):
            return self._frames[self._frame_index]
        return None

    def _hp_values(self, hp_str):
        """'cur/max' -> (cur, max) ints, or (0, 0) when unknown."""
        if not hp_str or "/" not in hp_str:
            return 0, 0
        try:
            cur, max_hp = hp_str.split("/", 1)
            return int(cur), int(max_hp)
        except ValueError:
            return 0, 0

    def _render_frame(self):
        # A fresh full-frame render restarts beat-level sync bookkeeping
        # (stepping around, restarting play from the top, or loading a new
        # game) so each move gets its damage drawn exactly once.
        self._synced_moves = set()
        self._hit_events = {}
        frame = self._frame()
        if frame is None:
            self.turn_label.setText("--")
            for slot in _SLOT_ORDER:
                self.board_labels[slot].setText("--")
                self._clear_slot_sprite(slot)
                self.hp_bars[slot].setValue(0)
                self.hp_bars[slot].setFormat("")
                self.hp_bars[slot].setStyleSheet("QProgressBar { background: transparent; border: none; }")
            self.event_browser.setPlainText("")
            self._prev_hp_pct = {}
            self._pending_switch_in = set()
            self._pending_mega_flash = set()
            self._update_conditions(None)
            self._update_ohko(None)
            return

        self._clear_turn_effects()
        self.turn_label.setText(f"Turn {frame['turn']}")

        board = frame.get("board", {})
        hp = frame.get("hp", {})
        faints = set(frame.get("faints", []))
        switches = set(slot for _side, slot, _mon in frame.get("switches", []))
        megas = set(frame.get("megas", []))
        self._pending_switch_in = set(switches)
        self._pending_mega_flash = set(megas)
        self._fading_slots = set()

        for slot in _SLOT_ORDER:
            mon = board.get(slot)
            if not mon:
                self._remove_shield(slot)  # don't strand a shield on an empty slot
                if slot in faints and self._animate and slot in self._slot_sprite_key:
                    self._fading_slots.add(slot)
                    self._fade_out_slot(slot, lambda s=slot: self._finalize_empty_slot(s))
                    continue
                self._finalize_empty_slot(slot)
                continue
            self.board_labels[slot].setText(self._name_markup(frame, slot, mon))
            side = "p1" if slot.startswith("p1") else "p2"
            back = side == self._back_side
            accent = self._tokens['VIOLET'] if back else self._tokens['MUTED']

            showing = False
            if self._sprites_enabled and self._sprite_store is not None:
                showing = self._set_best_sprite(slot, mon, back)
            if showing:
                self._style_slot_label(slot, has_sprite=True)
            else:
                self._clear_slot_sprite(slot)
                self._style_slot_label(slot, has_sprite=False)
            cur, max_hp = self._hp_values(hp.get(slot, ""))
            pct = int(cur * 100 / max_hp) if max_hp else 0
            self._set_hp_bar(slot, pct)
            self.hp_bars[slot].setFormat("%p%" if max_hp else "")
            
            # Determine HP color token
            if cur == 0:
                hp_color = self._tokens['MUTED']
            elif pct <= 20:
                hp_color = self._tokens['DANGER']
            elif pct <= 50:
                hp_color = self._tokens['WARNING']
            else:
                hp_color = self._tokens['HEALTHY']
            
            self.hp_bars[slot].setStyleSheet(f"QProgressBar {{ background: transparent; border: none; }} QProgressBar::chunk {{ background: {hp_color}; }}")

        # Manual whole-turn render (call, opening the tab, or turn-for-turn
        # step): animate every move of the frame's `moves` list so the turn's
        # Showdown-lite effects (orb/beam/dash/fake-out/shield/pulse) all play.
        if self._animate:
            for move_info in frame.get("moves", []):
                # A failed protect carries a trailing True flag (same
                # convention as its move event) so the whole-turn render
                # shows the wispy break, never a persistent shield.
                _side, slot, _mon, move, target, targets, _species, *_rest = move_info
                failed = bool(_rest and _rest[-1] is True)
                self._animate_move(slot, move, target, targets, protect_failed=failed)
            for slot in _SLOT_ORDER:
                if slot in self._slot_sprite_key and slot not in self._idle_bob_timers:
                    self._start_idle_bob(slot)
        self._apply_sub_dolls(frame.get("substitutes"))

        self._update_movelog(active_index=None)
        self._update_conditions(frame)
        self._update_ohko(frame)

    def _point_state(self, frame_index, event_index):
        """(board, hp, substitutes) maps once `event_index` events of frame
        `frame_index` have been applied. A frame starts from the previous
        turn's end state (an empty field for the very first frame); each
        event mutates it -- a damage/heal changes one slot's HP, a faint
        empties a slot, a switch brings a new mon onto the field, a mega
        swaps its species, a sub raises/breaks a Substitute doll. Rebuilding
        from the incoming state keeps stepping reversible (Prev just renders
        a smaller event_index)."""
        board = {}
        hp = {}
        subs = {}
        if frame_index > 0:
            prev = self._frames[frame_index - 1]
            board = dict(prev.get("board", {}))
            hp = dict(prev.get("hp", {}))
            subs = dict(prev.get("substitutes", {}))
        frame = self._frames[frame_index]
        for ev in frame.get("events", [])[:event_index]:
            self._apply_event_to_board(board, hp, subs, ev, frame)
        return board, hp, subs

    def _apply_event_to_board(self, board, hp, subs, ev, frame):
        """Applies a single battle event to the running board/hp/sub maps. Damage
        reads the HP value its own -damage line reported, so each bar drains
        exactly once per move instead of snapping to the end of the turn."""
        kind = ev[0]
        if kind == "switch":
            slot = ev[2]
            board[slot] = ev[3]
            hp[slot] = ev[4] if len(ev) > 4 else ""
        elif kind == "damage":
            slot = ev[1]
            hp[slot] = ev[2] if len(ev) > 2 else ""
        elif kind == "heal":
            slot = ev[1]
            hp[slot] = ev[2] if len(ev) > 2 else ""
        elif kind == "faint":
            slot = ev[1]
            board.pop(slot, None)
            hp.pop(slot, None)
        elif kind == "mega":
            slot = ev[1]
            if slot in board:
                board[slot] = frame.get("board", {}).get(slot) or board[slot]
        elif kind == "sub":
            # A Substitute doll coming up or breaking (item E).
            slot = ev[1]
            if len(ev) > 2 and ev[2]:
                subs[slot] = True
            else:
                subs.pop(slot, None)
        # "move" and "popup" intentionally change nothing here: a move does
        # not alter the board's composition (its HP arrives as its own event),
        # and popups are pure narration.

    def _initial_event_index(self):
        """Where playback begins on a just-opened game: the leading pre-battle
        switch-in events of the first frame are applied so both teams already
        stand on the field before the first move plays."""
        if not self._frames:
            return 0
        events = self._frames[0].get("events", [])
        k = 0
        for ev in events:
            if ev[0] == "switch":
                k += 1
            else:
                break
        return k

    def _animate_move(self, slot, move, target, targets, protect_failed=False, on_hit=None):
        """One move's bespoke animation (orb / beam / dash / fake-out /
        shield / pulse, else a generic surge) plus a damage flash on each
        target. Shared by _render_frame (whole turn at once) and the
        autoplay player (just the event that was replayed).

        `on_hit` (autoplay only) defers the damage-taking animation -- the
        target shake, the bar drain, the '-N' badge -- to the moment the
        effect actually lands, so the hit reads as one connected event
        instead of the bar draining while the projectile is still flying.
        Returns True when the hit was deferred to the effect's arrival;
        False when the effect draws right away (or there was no art to
        animate), and the caller should fire on_hit immediately."""
        if not self._animate:
            return False
        if not targets:
            targets = [target] if target else []
        kind = _MOVE_FX.get(move, "generic")
        # A protect-family move is all shield and no travel: raise it even
        # for a spriteless holder (item B) so the protection still reads.
        if kind == "protect":
            if protect_failed:
                # The protect never came up: a brief shatter of pale light
                # instead of the persistent shield (item D).
                center = self._slot_center(slot)
                self._impact_burst(center, "#9aa7e0", size=110, grow=1.5)
                self._flash_sprite(slot, QColor(160, 170, 220))
            else:
                self._spawn_shield(slot)
                self._flash_sprite(slot, QColor(120, 210, 255))
            return False  # no travel: the caller fires on_hit on this beat
        if slot in self._fading_slots or slot not in self._slot_sprite_key:
            return False  # no attacker art to animate
        color = _PROJECTILE_COLORS.get(move, _PROJECTILE_FALLBACK)
        aim = self._aim_target(targets, slot)
        if kind == "projectile":
            if aim:
                self._launch_orb(slot, self._effect_label(aim), color, on_hit=on_hit)
                return True
        elif kind == "burst":
            if aim:
                self._launch_orb(slot, self._effect_label(aim), color, on_hit=on_hit,
                                 orb_size=26, burst_grow=3.6)
                return True
        elif kind == "beam":
            if aim:
                self._launch_beam(slot, self._effect_label(aim), color, on_hit=on_hit)
                return True
        elif kind == "nova":
            if aim:
                self._launch_beam(slot, self._effect_label(aim), color, on_hit=on_hit,
                                  burst_size=44, burst_grow=3.0)
                return True
        elif kind == "dash":
            if aim:
                self._dash_attack(slot, self._effect_label(aim), on_hit=on_hit)
                return True
        elif kind == "slam":
            if aim:
                self._dash_attack(slot, self._effect_label(aim), on_hit=on_hit, burst=True)
                return True
        elif kind == "fakeout":
            if aim:
                self._fake_out(slot, self._effect_label(aim), on_hit=on_hit)
                return True
        elif kind == "earthquake":
            # The whole field rumbles violently and the ground cracks open.
            # No single aim: every target on the board takes the shock.
            self._earthquake_fx(slot, [t for t in targets if t in self._slot_sprite_key])
            if on_hit is not None:
                QTimer.singleShot(200, on_hit)
            return True
        elif kind == "thunder":
            if aim:
                self._lightning_fx(slot, self._effect_label(aim), on_hit=on_hit)
                return True
        elif kind == "wave":
            if aim:
                self._wave_fx(slot, self._effect_label(aim), on_hit=on_hit)
                return True
        elif kind == "psychic":
            if aim:
                self._psychic_distortion_fx(slot, self._effect_label(aim), on_hit=on_hit)
                return True
        elif kind == "flame_blast":
            if aim:
                self._flame_blast_fx(slot, self._effect_label(aim), on_hit=on_hit)
                return True
        elif kind == "barrage":
            if aim:
                self._barrage_fx(slot, self._effect_label(aim), on_hit=on_hit)
                return True
        elif kind == "frost":
            if aim:
                self._ice_crystal_fx(slot, self._effect_label(aim), on_hit=on_hit)
                return True
        elif kind == "self":
            self._self_pulse(slot)
        else:  # generic
            self._attack_surge(slot)
        if on_hit is None:
            for t in targets:
                if (t != slot and t not in self._fading_slots
                        and t in self._slot_sprite_key):
                    self._damage_flash(t)
        return False

    def _event_text(self, ev, frame):
        """One sentence for a single battle event, written from the
        players' names and the board's current species, for the
        battle-ordered movelog. Events come straight from
        replay_timeline, in the exact order the log recorded them."""
        kind = ev[0]
        board = frame.get("board", {})
        if kind in ("switch", "move"):
            side = ev[1]
        else:
            slot_ref = ev[1] or ""
            side = "p1" if slot_ref.startswith("p1") else "p2"
        owner = self._side_names.get(side, side)
        if kind == "switch":
            mon = ev[3]
            return f"{_possessive(owner)} {pretty_species(mon)} switched in!"
        if kind == "move":
            slot, mon = ev[2], ev[3]
            move = ev[4]
            targets = ev[7] if len(ev) > 7 else []
            species_at_move = (
                ev[8] if len(ev) > 8 and isinstance(ev[8], dict) else {}
            )
            field_mon = board.get(slot) or mon
            if field_mon != mon and not field_mon.startswith(mon + "-"):
                field_mon = mon  # this mover switched out mid-turn
            seen = set()
            names = []
            for t in targets:
                if t and t != slot:
                    # Prefer the species that was in the slot AT MOVE TIME
                    # (item D) so a mid-turn switch/mega can't rewrite what
                    # the move actually hit.
                    tmon = species_at_move.get(t) or board.get(t) or self._last_known_mon(t)
                    n = pretty_species(tmon) if tmon else None
                    if n and n not in seen:
                        seen.add(n)
                        names.append(n)
            if move in _PROTECT_DISPLAY and ev[-1] is True:
                who = pretty_species(field_mon)
                return f"{_possessive(owner)} {who}'s {move} failed!"
            if len(names) >= 2:
                target_text = f" on {', '.join(names[:-1])} and {names[-1]}"
            elif names:
                target_text = f" on {names[0]}"
            else:
                target_text = ""
            return f"{_possessive(owner)} {pretty_species(field_mon)} used {move}{target_text}!"
        if kind == "tera":
            mon = board.get(ev[1]) or self._last_known_mon(ev[1])
            name = pretty_species(mon) if mon else "a Pok\u00e9mon"
            ttype = ev[3] if len(ev) > 3 and ev[3] else ""
            suffix = f" into its {ttype} form!" if ttype else "!"
            return f"{_possessive(owner)} {name} Terastallized{suffix}"
        if kind == "dynamax":
            mon = board.get(ev[1]) or self._last_known_mon(ev[1])
            name = pretty_species(mon) if mon else "a Pok\u00e9mon"
            return f"{_possessive(owner)} {name} Dynamaxed!"
        if kind == "mega":
            mon = board.get(ev[1]) or self._last_known_mon(ev[1])
            name = pretty_species(mon) if mon else "a Pok\u00e9mon"
            return f"{_possessive(owner)} {name} Mega Evolved!"
        if kind in ("boost", "unboost"):
            # A stat-stage change -- Swords Dance raising Attack, Close
            # Combat dropping the user's Defense, and so on. Read from the
            # event's own species (locked in at the |-boost|/-unboost| line).
            mon = ev[4] if len(ev) > 4 and ev[4] else (
                board.get(ev[1]) or self._last_known_mon(ev[1])
            )
            name = pretty_species(mon) if mon else "a Pok" "\u00e9mon"
            stat = _STAT_NAMES.get(ev[2], ev[2] or "stats")
            stages = ev[3] if len(ev) > 3 else 1
            verb = "rose" if kind == "boost" else "fell"
            return f"{_possessive(owner)} {name}'s {stat} {verb} by {stages}!"
        if kind in ("damage", "heal"):
            # The event's own species (captured at the -damage/-heal line)
            # wins so a mid-turn switch (Flip Turn pulling the mover out)
            # can't rename who took the hit to the replacement.
            mon = ev[3] if len(ev) > 3 and ev[3] else (
                board.get(ev[1]) or self._last_known_mon(ev[1])
            )
            name = pretty_species(mon) if mon else "a Pok\u00e9mon"
            hp = ev[2] if len(ev) > 2 else ""
            suffix = f" ({hp})" if hp else ""
            verb = "took damage" if kind == "damage" else "recovered HP"
            return f"{_possessive(owner)} {name} {verb}{suffix}!"
        if kind == "faint":
            name = pretty_species(ev[2]) if ev[2] else "a Pok\u00e9mon"
            return f"{_possessive(owner)} {name} fainted!"
        if kind == "sub":
            # A Substitute doll coming up or breaking (item E).
            mon = board.get(ev[1]) or self._last_known_mon(ev[1])
            name = pretty_species(mon) if mon else "a Pok\u00e9mon"
            on = bool(ev[2]) if len(ev) > 2 else False
            if on:
                return f"{_possessive(owner)} {name} made a Substitute!"
            return f"{_possessive(owner)} {name}'s Substitute broke!"
        if kind == "popup":
            # Small floating name badges (item G): an item consumed, an
            # ability that activated, or an announce-style message line.
            ptype = ev[2] if len(ev) > 2 else ""
            name = ev[3] if len(ev) > 3 else ""
            # Popups carry the mon that activated at the log moment (item G):
            # Rough Skin-style [from] lines credit the [of] owner, and a
            # mid-turn switch can't rename the subject. Fall back to the
            # board for any shorter/shared popups.
            mon = ev[4] if len(ev) > 4 and ev[4] else (
                board.get(ev[1]) or self._last_known_mon(ev[1])
            )
            nm = pretty_species(mon) if mon else "a Pok\u00e9mon"
            if ptype == "message":
                if name.startswith(("breaks", "suppresses")):
                    return f"{nm} {name}!"
                return f"{_possessive(nm)} {name}!"
            if ptype == "item":
                return f"{_possessive(nm)} {name} was consumed!"
            return f"{_possessive(nm)} {name} activated!"
        if kind == "notice":
            # A move's announce box-text ("It's super effective!", "A critical
            # hit!") -- folded under its move's entry like an ability reveal,
            # but it never gets a floating board bubble.
            return ev[3] if len(ev) > 3 and ev[3] else (
                " \u00b7 ".join(str(part) for part in ev)
            )
        return " \u00b7 ".join(str(part) for part in ev)

    def _update_movelog(self, active_index=None, limit=None):
        """Renders the current turn's movelog as rich text in real
        battle order. `active_index` highlights the event the autoplay
        player just replayed; None (frame freshly entered, or a manual
        turn-step) shows the whole turn with nothing highlighted. Plain
        labels only -- no emoji.

        `limit` (as many events as have been revealed so far) makes the
        autoplay movelog fill in progressively -- only the events actually
        shown so far appear, so a turn reads move-by-move instead of all
        at once (item B)."""
        frame = self._frame()
        if frame is None:
            self.event_browser.clear()
            return
        events = frame.get("events", [])
        if not events:
            self.event_browser.setHtml("<i>(no battle events this turn)</i>")
            return
        if limit is not None:
            events = events[:limit]
            if not events:
                self.event_browser.setHtml("<i>(no battle events this turn)</i>")
                return
        blocks = []
        i = 0
        n = len(events)
        while i < n:
            ev = events[i]
            if ev[0] == "move":
                # Fold the move's own outcome lines (its damage/heal and any
                # item/ability popups) into ONE movelog entry so a turn reads
                # move-by-move instead of line-by-line (item C).
                j = i + 1
                while j < n and events[j][0] in (
                    "damage", "heal", "popup", "boost", "unboost", "notice",
                ):
                    j += 1
                lines = [self._event_text(events[k], frame) for k in range(i, j)]
                body = lines[0] + "".join(
                    "<br>&nbsp;&nbsp;" + line for line in lines[1:]
                )
                if active_index is not None and i <= active_index < j:
                    blocks.append(
                        f'<div style="background:#fff0c2;color:#3d2e8f;'
                        f'font-weight:bold;">{body}</div>'
                    )
                else:
                    blocks.append(f"<div>{body}</div>")
                i = j
                continue
            text = self._event_text(ev, frame)
            if i == active_index:
                blocks.append(
                    f'<div style="background:#fff0c2;color:#3d2e8f;'
                    f'font-weight:bold;">{text}</div>'
                )
            else:
                blocks.append(f"<div>{text}</div>")
            i += 1
        self.event_browser.setHtml("".join(blocks))

    def _play_event_effect(self, ev, on_hit=None):
        """Animates just the event the autoplay player stepped to -- the
        move's bespoke effect, a damage flash, a heal pulse, a switch-in
        fade, a mega flash, or a faint fade-out. For a move, `on_hit` is
        deferred to the moment the effect actually lands; returns True
        when the hit was deferred (the caller must NOT run it yet)."""
        if not self._animate:
            return False
        kind = ev[0]
        if kind == "move":
            _side, slot, _mon, move, target, _target_mon, targets, _species, *_rest = ev[1:]
            failed = bool(_rest and _rest[-1] is True)
            return self._animate_move(
                slot, move, target, targets, protect_failed=failed, on_hit=on_hit
            )
        elif kind == "damage":
            slot = ev[1]
            if slot in self._slot_sprite_key and slot not in self._fading_slots:
                self._damage_flash(slot)
        elif kind == "heal":
            slot = ev[1]
            if slot in self._slot_sprite_key and slot not in self._fading_slots:
                self._self_pulse(slot)
        elif kind in ("boost", "unboost"):
            # A stat going up/down floats a small colored badge over the
            # mon: '+2 ATK' rising green for a boost, '-1 Sp. Def' falling
            # red for a drop, matching the item/ability popup language.
            slot = ev[1]
            stat = ev[2] if len(ev) > 2 else ""
            stages = ev[3] if len(ev) > 3 else 1
            self._spawn_stat_badge(slot, stat, stages, down=(kind == "unboost"))
        elif kind == "switch":
            slot = ev[2]
            if slot in self._slot_sprite_key:
                self._fade_in_slot(slot)
        elif kind == "mega":
            slot = ev[1]
            if slot in self._slot_sprite_key:
                self._mega_flash(slot)
        elif kind == "tera":
            slot = ev[1]
            if slot in self._slot_sprite_key:
                label = self.sprite_labels[slot]
                center = label.mapTo(self.board_container, label.rect().center())
                self._impact_burst(center, "#a7cbf5", size=86, grow=2.0)
                self._flash_sprite(slot, QColor(150, 215, 255))
        elif kind == "dynamax":
            slot = ev[1]
            if slot in self._slot_sprite_key:
                label = self.sprite_labels[slot]
                center = label.mapTo(self.board_container, label.rect().center())
                self._impact_burst(center, "#ff7043", size=110, grow=1.7)
                self._flash_sprite(slot, QColor(255, 120, 90))
        elif kind == "faint":
            slot = ev[1]
            self._remove_shield(slot)  # never leave a shield over an empty slot
            if slot in self._slot_sprite_key:
                self._fade_out_slot(slot, lambda s=slot: self._finalize_empty_slot(s))
        elif kind == "sub":
            # The doll itself is drawn from the running substitutes map by
            # the render; here just pop a small tell at the slot (item E).
            slot = ev[1]
            on = bool(ev[2]) if len(ev) > 2 else False
            if slot in self._slot_sprite_key:
                label = self.sprite_labels[slot]
                center = label.mapTo(self.board_container, label.rect().center())
                self._impact_burst(center, "#4fc3f7" if on else "#90a4ae",
                                   size=70, grow=1.5)
        elif kind == "popup":
            # A floating item/ability badge beside the mon (item G);
            # announce-style messages only write to the movelog.
            if len(ev) > 3 and ev[2] != "message":
                self._spawn_popup(ev[1], ev[2], ev[3])
        return False

    # ------------------------------------------------ board conditions

    def _update_conditions(self, frame):
        """Renders the battle-scene conditions for a frame: the central
        strip (weather / terrain / room), the board tint, each side's chip
        row (Tailwind, screens, hazards) with a ×N depth suffix on layered
        hazards. The glyph icons and the scene backdrop are stripped for
        now -- the board tint and the chips carry the conditions.
        render -- conditions are information, so they update even with
        animation disabled."""
        chips = []
        tints = []
        if frame is not None:
            for name, key, color in (
                (frame.get("weather"), "weather_remaining", "#a06a00"),
                (frame.get("terrain"), "terrain_remaining", "#2e7d32"),
                (frame.get("room"), "room_remaining", "#6a1b9a"),
            ):
                if name:
                    chips.append(_condition_chip(name, color, frame.get(key)))
                    tints.append(_TINT_BY_CONDITION.get(name))
        self.conditions_label.setText("&nbsp;·&nbsp;".join(chips) if chips else "")
        tints = [t for t in tints if t]
        tint = _blend_colors(*tints) if tints else self._tokens['TRACK']
        self.board_container.setStyleSheet(
            f"background: {tint}; border-radius: 10px;"
        )
        layer_map = (frame.get("side_condition_layers") or {}) if frame is not None else {}
        for side in ("p1", "p2"):
            conds = (frame.get("side_conditions", {}).get(side, [])
                     if frame is not None else [])
            remaining = (frame.get("side_condition_remaining", {}).get(side, {})
                         if frame is not None else {})
            side_layers = layer_map.get(side, {})
            if conds:
                chips = []
                for name in conds:
                    color = _SIDE_COND_COLORS.get(name, "#555555")
                    chips.append(_condition_chip(
                        name, color, remaining.get(name), side_layers.get(name)))
                self.side_cond_labels[side].setText("&nbsp;·&nbsp;".join(chips))
                # Painted glyph icons are stripped for now -- the chip
                # row above already carries the same info.
            else:
                self.side_cond_labels[side].setText("")
                self.side_cond_icons[side].setPixmap(QPixmap())
                self.side_cond_icons[side].hide()
        self._render_scene(frame)

    def _update_ohko(self, frame):
        """Shows a large 'OHKO' / 'DOUBLE OHKO' overlay when the frame's
        ending turn one-hit-KO'd something (or two things) from full HP,
        pursuer-of-a-full-HP-target style. Hidden the rest of the time."""
        text = frame.get("ohko") if frame is not None else None
        if not text:
            self.ohko_label.hide()
            return
        self.ohko_label.setText(text)
        self.ohko_label.setGeometry(self.board_container.rect())
        self.ohko_label.raise_()
        self.ohko_label.show()
        # The slam is a punch, not a persistent banner: auto-hide it ~3s
        # after it appears (scaled with playback speed -- 1.5s at 2x,
        # 0.75s at 4x) instead of leaving it up until the next render.
        interval = _SPEEDS.get(self.speed_combo.currentText(), _SPEEDS["1x"])
        duration = int(3000 * interval / _SPEEDS["1x"])
        QTimer.singleShot(max(200, duration), self.ohko_label.hide)

    def _name_markup(self, frame, slot, mon):
        """Renders the board name chip with a persistent (Tera: type) /
        (dynamaxed) suffix while the mon is terastallized / dynamaxed. The
        tags mirror the timeline state, so a faint or switch-out drops them,
        and a dynamax's 3-turn clock clears it when it reverts. Any markup
        is wrapped in a leading span so QLabel's AutoText detects the rich
        text instead of printing the <span> tags literally."""
        text = pretty_species(mon)
        tags = []
        if (frame.get("teras") or {}).get(slot):
            tags.append(
                "<span style='color:#1e88e5;font-weight:bold;'>"
                f"(Tera: {frame['teras'][slot]})</span>"
            )
        if (frame.get("dynamaxed") or {}).get(slot) is not None:
            tags.append(
                "<span style='color:#fb8c00;font-weight:bold;'>"
                "(dynamaxed)</span>"
            )
        if not tags:
            return text
        return "<span>" + text + "</span> " + " ".join(tags)

    def _render_scene(self, frame):
        """Snapshots the frame's weather/terrain/room AND each side's
        conditions (screens, hazards) into the scene and (re)paints the
        painted backdrop art."""
        if frame is None:
            self._scene_conds = (0, 0, None, None, None)
            self._scene_side_conditions = {}
            self._scene_side_layers = {}
            self._paint_scene(None, None, None)
            return
        self._scene_conds = (
            self.board_container.width(), self.board_container.height(),
            frame.get("weather"), frame.get("terrain"), frame.get("room"),
        )
        self._scene_side_conditions = frame.get("side_conditions", {})
        self._scene_side_layers = frame.get("side_condition_layers", {})
        self._paint_scene(frame.get("weather"), frame.get("terrain"), frame.get("room"))

    def _paint_scene(self, weather, terrain, room):
        """Rebuilds the scenery pixmaps at the board's current size. The
        weather/terrain/room motifs render in the background layer, behind
        the tiles; each side's screens and grounded hazards render in the
        raised hazards+screens layer (above sprites and move FX, below the
        UI, both ~50%). Each is hidden when there's nothing to draw / the
        board has no laid-out size yet -- the scale read from resizeEvent
        then paints it on first show."""
        w, h = self.board_container.width(), self.board_container.height()
        self._scene_conds = (w, h, weather, terrain, room)
        self._place_scene_overlay()
        bg = _make_scene_overlay(w, h, weather, terrain, room)
        self.scene_overlay.setPixmap(bg)
        if bg.isNull():
            self.scene_overlay.hide()
        else:
            self.scene_overlay.show()
        conditions = getattr(self, "_scene_side_conditions", {})
        hz = _make_hazard_overlay(w, h, conditions,
                                  getattr(self, "_scene_side_layers", {}),
                                  self._back_side,
                                  self._hazard_layout())
        self.hz_overlay.setPixmap(hz)
        if hz.isNull():
            self.hz_overlay.hide()
        else:
            self.hz_overlay.show()

    def _place_scene_overlay(self):
        """Pins both scenery labels to the whole board area, keeping the
        background (weather + screens) behind the tiles and the hazards
        on top of them."""
        # Background scenery sits lowest, pinned over the board, under tiles.
        bg = getattr(self, "scene_overlay", None)
        if bg is not None:
            bg.setGeometry(self.board_container.rect())
            bg.lower()
        # The FX / hazards+screens / UI layers are siblings of the board
        # (and of each other) at this widget's level, so Qt can order them.
        # FX and UI each cover the whole widget; hazards+screens covers the
        # board area. Enforced back-to-front: board < FX < hazards < UI.
        fx = getattr(self, "fx_layer", None)
        if fx is not None:
            fx.setGeometry(self.board_container.rect())
            fx.raise_()
        hz = getattr(self, "hz_overlay", None)
        if hz is not None:
            hz.setGeometry(self.board_container.rect())
            hz.raise_()
        ui = getattr(self, "ui_layer", None)
        if ui is not None:
            ui.setGeometry(self.board_container.rect())
            ui.raise_()
        self._place_ui_layer()

    def _hazard_layout(self):
        """Computes board-container–relative anchor rects for the hazard
        painter based on the current tile geometry.  Returns:
          * "singles" bool: True when the game is 1-vs-1;
          * "top"/"bottom": the *union rect* of each side's a+b cells (the
            full tile band -- used for doubles dead-centering);
          * "top_a"/"bottom_a": the geometry of the side's *a* cell
            (the lone Pokémon tile in singles).
        None until the board has been realised with real pixel geometry
        (``_board_laid_out`` is True and both band rects are valid),
        falling back to the old edge-pinned rows in the caller."""
        if not getattr(self, "_board_laid_out", False):
            return None

        def _band(side):
            rects = []
            for s in ("a", "b"):
                cell = self.cell_widgets[side + s]
                if cell.width() > 0 and cell.height() > 0:
                    rects.append(cell.geometry())
            if not rects:
                return None
            u = QRect(rects[0])
            for r in rects[1:]:
                u = u.united(r)
            return u

        top = self._top_side if hasattr(self, "_top_side") else "p2"
        bottom = self._back_side
        top_band = _band(top)
        bottom_band = _band(bottom)
        if top_band is None or bottom_band is None:
            return None
        top_a = self.cell_widgets[top + "a"]
        bottom_a = self.cell_widgets[bottom + "a"]
        return {
            "singles": self._game_type == "singles",
            "top": top_band,
            "bottom": bottom_band,
            "top_a": top_a.geometry(),
            "bottom_a": bottom_a.geometry(),
        }

    def _place_ui_layer(self):
        """Positions the per-tile name + HP bar widgets (the UI, on top of
        every scene layer) to mirror each tile's current geometry. UI
        widgets live in `ui_layer`, reparented out of the tiles so they can
        stack above the hazards/screens layer. Call after the grid lays out
        and on every resize."""
        ui = getattr(self, "ui_layer", None)
        if ui is None or not hasattr(self, "cell_widgets"):
            return
        ui.setGeometry(self.board_container.rect())
        laid_out = getattr(self, "_board_laid_out", False)
        top_side = getattr(self, "_top_side", "p2")
        for slot in _SLOT_ORDER:
            cell = self.cell_widgets[slot]
            name = self.board_labels[slot]
            hp = self.hp_bars[slot]
            # Reparent into the UI layer whenever we run so names + HP bars
            # stack above the hazards/screens layer even before the board is
            # realized; only the exact pixel placement waits for layout.
            name.setParent(ui)
            hp.setParent(ui)
            name.show()
            hp.show()
            if not laid_out or cell.width() <= 0:
                continue
            # ui is a child of board_container, and cell is also a child
            # of board_container, so cell.pos() is directly relative to ui.
            origin = cell.pos()
            cw, ch = cell.width(), cell.height()
            # Preserve the row convention: the opponent's name sits ABOVE
            # the sprite, your name BELOW it (just above the HP bar).
            name_above = slot[:2] == top_side
            ny = origin.y() if name_above else origin.y() + ch - 22 - 24
            name.setGeometry(origin.x(), ny, cw, 24)
            hp.setGeometry(origin.x() + 2, origin.y() + ch - 22, cw - 4, 20)

    # ----------------------------------------------------- board animation

    def _finalize_empty_slot(self, slot):
        """End-of-frame state for an empty board slot: no art, "--"
        name, zeroed HP bar."""
        self.board_labels[slot].setText("--")
        self._style_slot_label(slot, empty=True)
        self._clear_slot_sprite(slot)
        self._set_sub_doll(slot, False)
        self._stop_hp_drain(slot)
        self.hp_bars[slot].setValue(0)
        self.hp_bars[slot].setFormat("")
        self.hp_bars[slot].setStyleSheet("QProgressBar { background: transparent; border: none; }")
        self._prev_hp_pct[slot] = 0

    def _style_slot_label(self, slot, has_sprite=False, empty=False):
        """Apply consistent themed styling to a slot's name label."""
        tokens = self._tokens
        label = self.board_labels[slot]
        
        if empty:
            label.setStyleSheet(f"""
                font-weight: bold; padding: 2px; border: 1px solid {tokens['BORDER']};
                border-radius: 4px; background: {tokens['WHITE']}; color: {tokens['MUTED']};
            """)
        elif has_sprite:
            label.setStyleSheet(f"""
                font-weight: bold; font-size: 10px; padding: 2px; border: 1px solid {tokens['BORDER']};
                border-radius: 4px; background: {tokens['WHITE']}; color: {tokens['INK']};
            """)
        else:
            label.setStyleSheet(f"""
                font-weight: bold; padding: 2px; border: 1px solid {tokens['BORDER']};
                border-radius: 4px; background: {tokens['WHITE']}; color: {tokens['INK']};
            """)


    def _sub_pixmap(self, back=False):
        """The official Pokémon substitute doll pixmap for an active
        Substitute (item E). Uses the Gen-5 Showdown Substitute doll, cached
        per orientation -- front opponents hide behind it, your own back
        sprite peaks out in front. Loads from the SpriteStore cache or the
        built-in fallback bytes, so it needs no network after the first
        fetch and renders identically in offline/tests."""
        key = "back" if back else "front"
        if key not in self._sub_pixmaps:
            pix = QPixmap()
            if self._sprite_store is not None and self._animate:
                try:
                    sub_path = self._sprite_store.get_substitute(back=back)
                    if sub_path.exists():
                        pix.load(str(sub_path))
                except Exception:
                    pix = QPixmap()
            if pix.isNull():
                b64 = SUBSTITUTE_BACK_B64 if back else SUBSTITUTE_FRONT_B64
                pix.loadFromData(QByteArray.fromBase64(b64.encode("ascii")))
            if not pix.isNull():
                pix = pix.scaled(
                    _SPRITE_SIZE, _SPRITE_SIZE, Qt.KeepAspectRatio,
                    Qt.SmoothTransformation,
                )
            self._sub_pixmaps[key] = pix
        return self._sub_pixmaps[key]

    def _set_sub_opacity(self, slot):
        """Fades a substitute-user's real sprite behind its doll (item E)
        so the doll reads as the active body. Idempotent -- skips when the
        fade is already in place -- but otherwise replaces whatever effect
        was on the sprite, clearing a lingering switch-fade."""
        current = self._slot_effect.get(slot)
        if (isinstance(current, QGraphicsOpacityEffect)
                and abs(current.opacity() - _SUB_OPACITY) < 1e-6):
            return
        effect = QGraphicsOpacityEffect(self.sprite_labels[slot])
        effect.setOpacity(_SUB_OPACITY)
        self._set_slot_effect(slot, effect)

    def _set_sub_doll(self, slot, active, back=False):
        """Overlays (or hides) a Substitute doll on a slot. The doll stays
        fully opaque while the real mon fades behind it and peeks out to the
        side -- your side to the LEFT, the opponent's to the RIGHT (matching
        Showdown's perspective). When the doll breaks the mon returns to
        full opacity at its original position."""
        label = self.sprite_labels[slot]
        doll = self._sub_dolls.get(slot)
        if active:
            if slot in self._sub_base:
                if doll is not None:
                    doll.show()
                # Re-assert the fade on each frame, but never clobber a
                # transient animation (a faint) currently driving the
                # sprite's effect -- it finishes on its own.
                if slot not in self._slot_effect_anim:
                    self._set_sub_opacity(slot)
                return
            base = label.geometry()
            self._sub_base[slot] = base
            self._stop_idle_bob(slot)
            if doll is None:
                doll = QLabel(self.cell_widgets[slot])
                doll.setPixmap(self._sub_pixmap(back=back))
                doll.setAttribute(Qt.WA_TransparentForMouseEvents)
                self._sub_dolls[slot] = doll
            doll.setGeometry(base)
            doll.show()
            if back:
                doll.lower()
                label.raise_()
                shift = -_SUB_SHIFT
            else:
                doll.raise_()
                label.lower()
                shift = _SUB_SHIFT
            label.setGeometry(base.translated(shift, 0))
            # The real mon fades behind its doll so the ghost reads as the
            # active body; the doll itself stays fully opaque (item E).
            self._set_sub_opacity(slot)
        else:
            if doll is None and not self._sub_base.get(slot):
                return
            base = self._sub_base.pop(slot, None)
            if doll is not None:
                doll.hide()
                doll.deleteLater()
                self._sub_dolls.pop(slot, None)
            if base is not None:
                label.setGeometry(base)
                self._start_idle_bob(slot)
            self._set_slot_effect(slot, None)

    def _apply_sub_dolls(self, substitutes):
        """Syncs every slot's doll to the substitutes map. Called from both
        the whole-turn and autoplay render paths, so a doll raised mid-turn
        survives the next event's re-render and a broken one is dropped."""
        subs = substitutes or {}
        for slot in _SLOT_ORDER:
            has_sprite = slot in self._slot_sprite_key
            self._set_sub_doll(
                slot, bool(subs.get(slot)) and has_sprite,
                back=slot.startswith(self._back_side))

    def _set_hp_bar(self, slot, pct):
        """Sets a slot's HP value, draining from the previous frame's
        percentage when board animation is on (so damage reads
        smoothly instead of snapping)."""
        bar = self.hp_bars[slot]
        prev = self._prev_hp_pct.get(slot)
        if self._animate and prev is not None and prev != pct:
            self._stop_hp_drain(slot)
            anim = QPropertyAnimation(bar, b"value", self)
            anim.setStartValue(prev)
            anim.setEndValue(pct)
            anim.setDuration(_HP_DRAIN_MS)
            anim.setEasingCurve(QEasingCurve.OutCubic)
            self._hp_anims[slot] = anim
            anim.finished.connect(lambda s=slot, a=anim: self._hp_anims.pop(s, None))
            self._keep_animation(anim)
            anim.start()
        else:
            self._stop_hp_drain(slot)
            bar.setValue(pct)
        self._prev_hp_pct[slot] = pct

    def _stop_hp_drain(self, slot):
        """Cancels a slot's in-flight HP drain (used when the mon leaves
        the field), so a stale animation can't overwrite the final value."""
        anim = self._hp_anims.pop(slot, None)
        if anim is not None:
            anim.stop()
            self._drop_animation(anim)

    # -- graphics effects (a label can hold exactly one at a time) --

    def _get_slot_opacity_effect(self, slot):
        """The slot sprite's current QGraphicsOpacityEffect, creating one
        (at full opacity) and attaching it when the label has none.

        Slot fades (switch-in / faint) reuse the same effect object for the
        label's whole lifetime, so a follow-up fade never re-routes the
        proxied sprite through a brand-new offscreen effect source; whoever
        ends the fade (or clears the sprite) detaches it."""
        current = self._slot_effect.get(slot)
        if isinstance(current, QGraphicsOpacityEffect):
            old_anim = self._slot_effect_anim.pop(slot, None)
            if old_anim is not None:
                old_anim.stop()
                self._drop_animation(old_anim)
            return current
        effect = QGraphicsOpacityEffect(self.sprite_labels[slot])
        effect.setOpacity(1.0)
        self._set_slot_effect(slot, effect)
        return effect

    def _set_slot_effect(self, slot, effect):
        """Installs a graphics effect on a slot's sprite label. Qt
        itself deletes the effect it replaces, so any animation still
        driving the old effect is stopped first and the stale wrapper
        is dropped (never touch a replaced effect -- its C++ object is
        already gone)."""
        old_anim = self._slot_effect_anim.pop(slot, None)
        if old_anim is not None:
            old_anim.stop()
            self._drop_animation(old_anim)
        self._slot_effect.pop(slot, None)
        label = self.sprite_labels[slot]
        if effect is not None:
            label.setGraphicsEffect(effect)
            self._slot_effect[slot] = effect
        else:
            label.setGraphicsEffect(None)  # detaches (and deletes) whatever was up

    def _remove_slot_effect(self, slot):
        anim = self._slot_effect_anim.pop(slot, None)
        if anim is not None:
            anim.stop()
            self._drop_animation(anim)
        if slot not in self._slot_effect:
            return
        self._slot_effect.pop(slot, None)
        self.sprite_labels[slot].setGraphicsEffect(None)  # Qt deletes the effect

    def _remove_effect_if_current(self, slot, effect):
        if self._slot_effect.get(slot) is effect:
            self._remove_slot_effect(slot)

    def _flash_sprite(self, slot, color, peak=0.9, duration_ms=_MOVE_FLASH_MS):
        """Brief color wash over a slot's sprite (white = attacking, red =
        damaged, gold = mega). Painted as a transient translucent rgba
        overlay on the fx layer instead of a QGraphicsColorizeEffect on the
        sprite label: attaching a widget effect to the movie-playing label
        inside the proxied board re-routes it through an offscreen source
        that Windows composites a frame late -- exactly the blink out and
        back in seen whenever a move lands. The overlay carries no effect
        lifecycle, so the sprite's own backing store is never touched."""
        label = self._effect_label(slot)
        if not label.isVisible():
            return  # a hidden sprite needs no flash (and no effect churn)
        board = self.board_container
        r, g, b = color.red(), color.green(), color.blue()
        overlay = QLabel(self.fx_layer)
        overlay.setAttribute(Qt.WA_TransparentForMouseEvents)
        overlay.resize(label.size())
        overlay.move(label.mapTo(board, label.rect().topLeft()))
        self._effect_widgets.append(overlay)
        anim = QVariantAnimation(self)
        anim.setDuration(duration_ms)
        anim.setKeyValueAt(0.0, 0.0)
        anim.setKeyValueAt(0.35, peak)
        anim.setKeyValueAt(1.0, 0.0)
        anim.setEasingCurve(QEasingCurve.InOutQuad)

        def _tint(alpha):
            # Track the label while it hops / shakes so the wash rides the
            # sprite instead of sliding off it mid-animation.
            try:
                cur = label.mapTo(board, label.rect().topLeft())
                if overlay.pos() != cur:
                    overlay.move(cur)
                overlay.setStyleSheet(
                    f"background: rgba({r}, {g}, {b}, {int(alpha * 255)});"
                )
            except RuntimeError:
                pass  # the sprite was cleared mid-flash; retire handles it

        anim.valueChanged.connect(_tint)
        anim.finished.connect(lambda w=overlay: self._retire_effect(w))
        self._keep_animation(anim)
        overlay.show()
        overlay.raise_()
        anim.start()

    def _bounce_geometry(self, slot, label, base, keyvalues, duration_ms):
        """Animates a label's geometry through keyframes (a hop or a
        shake), restoring it afterwards and re-starting its idle bob.
        Only the most recent bounce per slot may restore, so rapid
        step-through can't leave a sprite displaced."""
        restore = lambda: label.setGeometry(base)
        self._surge_restore[slot] = restore
        label.raise_()
        anim = QPropertyAnimation(label, b"geometry", self)
        anim.setDuration(duration_ms)
        for t, rect in keyvalues:
            anim.setKeyValueAt(t, rect)
        anim.setEasingCurve(QEasingCurve.InOutQuad)

        def _done():
            if self._surge_restore.get(slot) is restore:
                self._surge_restore.pop(slot, None)
                restore()
                self._restart_idle_bob(slot)

        anim.finished.connect(_done)
        self._keep_animation(anim)
        anim.start()

    def _attack_surge(self, slot):
        """The attacking sprite hops forward while flashing white."""
        self._stop_idle_bob(slot)
        label = self.sprite_labels[slot]
        base = label.geometry()
        self._bounce_geometry(slot, label, base, [
            (0.0, base), (0.3, base.translated(0, -7)), (1.0, base),
        ], _MOVE_FLASH_MS)
        self._flash_sprite(slot, QColor(255, 255, 255))

    def _damage_flash(self, slot):
        """The target shakes side to side while flashing red."""
        self._stop_idle_bob(slot)
        label = self.sprite_labels[slot]
        base = label.geometry()
        dx = 4
        self._bounce_geometry(slot, label, base, [
            (0.0, base), (0.2, base.translated(dx, 0)),
            (0.4, base.translated(-dx, 0)),
            (0.6, base.translated(dx, 0)), (1.0, base),
        ], int(_MOVE_FLASH_MS * 0.75))
        self._flash_sprite(slot, QColor(255, 60, 60))

    def _fade_out_slot(self, slot, on_done=None):
        """Fades a slot's sprite out to transparent (a faint), then
        runs on_done so the slot's end state takes over."""
        self._stop_idle_bob(slot)
        effect = self._get_slot_opacity_effect(slot)
        effect.setOpacity(1.0)
        anim = QVariantAnimation(self)
        anim.setStartValue(1.0)
        anim.setEndValue(0.0)
        anim.setDuration(_FAINT_MS)
        anim.setEasingCurve(QEasingCurve.InCubic)
        anim.valueChanged.connect(effect.setOpacity)
        if on_done is not None:
            anim.finished.connect(on_done)
        anim.finished.connect(lambda s=slot, e=effect: self._remove_effect_if_current(s, e))
        self._slot_effect_anim[slot] = anim
        self._keep_animation(anim)
        anim.start()

    def _fade_in_slot(self, slot):
        """Sprite fades in when it switches onto the field."""
        effect = self._get_slot_opacity_effect(slot)
        effect.setOpacity(0.0)
        anim = QVariantAnimation(self)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setDuration(_SWITCH_FADE_MS)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.valueChanged.connect(effect.setOpacity)
        anim.finished.connect(lambda s=slot, e=effect: self._remove_effect_if_current(s, e))
        self._slot_effect_anim[slot] = anim
        self._keep_animation(anim)
        anim.start()

    def _mega_flash(self, slot):
        """Golden pulse when a mon mega-evolves (or otherwise
        forme-changes) this turn."""
        self._flash_sprite(slot, QColor(255, 214, 90), peak=0.95, duration_ms=_MEGA_FLASH_MS)

    # -- move-specific effects --

    def _aim_target(self, targets, slot):
        """The first target an effect can visibly land on: a slot with a
        sprite, or a spriteless mon (whose name tag stands in as the aim
        point -- item B). None when every target is the user, is gone, or
        is mid-faint."""
        for t in targets:
            if t and t != slot and t not in self._fading_slots:
                return t
        return None

    def _effect_label(self, slot):
        """The widget that wears a slot's art for aiming effects: the
        sprite label when one is showing, else the name tag, so spriteless
        mons are still fly-to / dash-into targets (item B)."""
        if slot in self._slot_sprite_key:
            return self.sprite_labels[slot]
        return self.board_labels[slot]

    def _slot_center(self, slot):
        """Board-space center of a slot's art -- its sprite when one is
        up, or its name tag on a spriteless mon (item B)."""
        label = self._effect_label(slot)
        return label.mapTo(self.board_container, label.rect().center())

    def _make_orb(self, color, size=18):
        """A round glowing projectile label."""
        orb = QLabel(self.fx_layer)
        orb.setFixedSize(size, size)
        orb.setAttribute(Qt.WA_TransparentForMouseEvents)
        orb.setStyleSheet(
            f"background: qradialgradient(cx:0.4, cy:0.4, radius:0.6,"
            f"fx:0.4, fy:0.4, stop:0 #ffffff, stop:0.45 {color},"
            f"stop:1 rgba(0,0,0,0)); border-radius: {size // 2}px;"
        )
        return orb

    def _launch_orb(self, slot, dst_label, color, on_hit=None,
                    orb_size=None, burst_grow=None):
        """A colored orb flies from the attacker's sprite to a target,
        bursting on arrival (Moonblast, Flamethrower, ...). `orb_size` /
        `burst_grow` scale the big 'burst' kind (Overheat, Tera Blast, ...).
        `on_hit` (autoplay) runs exactly when the orb connects so the
        damage-taking animation lands with the burst."""
        label = self.sprite_labels[slot]
        start = label.mapTo(self.board_container, label.rect().center())
        end = dst_label.mapTo(self.board_container, dst_label.rect().center())
        orb = self._make_orb(color, size=orb_size or 18)
        self._effect_widgets.append(orb)
        orb.move(start.x() - orb.width() // 2, start.y() - orb.height() // 2)
        orb.show()
        orb.raise_()
        anim = QPropertyAnimation(orb, b"pos", self)
        anim.setStartValue(orb.pos())
        anim.setEndValue(QPoint(end.x() - orb.width() // 2, end.y() - orb.height() // 2))
        anim.setDuration(_EFFECT_PROJECTILE_MS)
        anim.setEasingCurve(QEasingCurve.OutQuad)

        def _done():
            self._retire_effect(orb)
            self._impact_burst(end, color, grow=burst_grow or 2.5)
            if on_hit is not None:
                on_hit()

        anim.finished.connect(_done)
        self._keep_animation(anim)
        anim.start()

    def _retire_effect(self, widget):
        """Finished/discarded transient effect: untrack it and schedule
        its deletion. The C++ object may already be gone (its _done
        callback ran during an event-loop spin), so every call is
        guarded against RuntimeError."""
        try:
            widget.hide()
        except RuntimeError:
            pass
        try:
            widget.deleteLater()
        except RuntimeError:
            pass
        try:
            self._effect_widgets.remove(widget)
        except ValueError:
            pass

    def _launch_beam(self, slot, dst_label, color, on_hit=None,
                     burst_size=None, burst_grow=None):
        """A wide energy streak sweeps across the board and fades
        (Heat Wave, Hyper Voice, Solar Beam, ...). The beam is drawn as a
        gradient bar on a transparent pixmap, rotated to the line between
        the attacker and the target. `burst_size` / `burst_grow` scale the
        end blast for 'nova' moves (Hyper Beam, ...). `on_hit` (autoplay)
        runs exactly when the beam peaks at the target."""
        label = self.sprite_labels[slot]
        start = label.mapTo(self.board_container, label.rect().center())
        end = dst_label.mapTo(self.board_container, dst_label.rect().center())
        dx, dy = end.x() - start.x(), end.y() - start.y()
        length = max(10, int(math.hypot(dx, dy)))
        angle = math.degrees(math.atan2(dy, dx))
        pix = QPixmap(length, 12)
        pix.fill(QColor(0, 0, 0, 0))
        painter = QPainter(pix)
        grad = QLinearGradient(0, 6, length, 6)
        grad.setColorAt(0.0, QColor(255, 255, 255, 0))
        grad.setColorAt(0.55, QColor(color))
        grad.setColorAt(1.0, QColor(255, 255, 255, 0))
        painter.fillRect(QRect(0, 0, length, 12), QBrush(grad))
        painter.end()
        rotated = pix.transformed(QTransform().rotate(angle))
        beam = QLabel(self.fx_layer)
        beam.setAttribute(Qt.WA_TransparentForMouseEvents)
        beam.setPixmap(rotated)
        beam.setFixedSize(rotated.width(), rotated.height())
        self._effect_widgets.append(beam)
        mid = beam.rect().center()
        beam.move(start.x() - mid.x(), start.y() - mid.y())
        effect = QGraphicsOpacityEffect(beam)
        beam.setGraphicsEffect(effect)
        effect.setOpacity(0.0)
        anim = QVariantAnimation(self)
        anim.setStartValue(0.0)
        anim.setKeyValueAt(0.3, 1.0)
        anim.setKeyValueAt(0.7, 1.0)
        anim.setEndValue(0.0)
        anim.setDuration(_EFFECT_PROJECTILE_MS + 80)
        anim.setEasingCurve(QEasingCurve.InOutSine)
        anim.valueChanged.connect(effect.setOpacity)

        def _done():
            self._retire_effect(beam)  # the opacity effect dies with its widget
            self._impact_burst(end, color, size=burst_size or 26, grow=burst_grow or 2.5)
            if on_hit is not None:
                on_hit()

        anim.finished.connect(_done)
        self._keep_animation(anim)
        beam.show()
        beam.raise_()
        anim.start()

    def _impact_burst(self, center, color, size=26, grow=2.5, ms=_EFFECT_IMPACT_MS):
        """A hit connects: a brief expanding flash at `center` (a QPoint
        in the tab's coordinates)."""
        burst = QLabel(self.fx_layer)
        burst.setAttribute(Qt.WA_TransparentForMouseEvents)
        burst.setStyleSheet(
            f"background: qradialgradient(cx:0.5, cy:0.5, radius:0.5,"
            f"fx:0.5, fy:0.5, stop:0 rgba(255,255,255,0.9),"
            f"stop:0.45 {color}, stop:1 rgba(0,0,0,0));"
            f"border-radius: {int(size * grow) // 2}px;"
        )
        self._effect_widgets.append(burst)
        anim = QVariantAnimation(self)
        anim.setStartValue(QRect(center.x() - size // 2, center.y() - size // 2, size, size))
        end_w = int(size * grow)
        anim.setEndValue(QRect(center.x() - end_w // 2, center.y() - end_w // 2, end_w, end_w))
        anim.setDuration(ms)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.valueChanged.connect(burst.setGeometry)

        def _done():
            self._retire_effect(burst)

        anim.finished.connect(_done)
        self._keep_animation(anim)
        burst.show()
        burst.raise_()
        anim.start()

    def _spawn_shield(self, slot):
        """A translucent shield pops in over the defending mon and then
        STAYS for the rest of the turn (Protect, Quick Guard, King's
        Shield, ...). It holds because autoplay only re-imports a frame
        at turn boundaries; the next turn's _render_frame drops it via
        _clear_turn_effects. One shield per slot -- re-protecting simply
        replaces it rather than stacking several."""
        existing = self._shields.pop(slot, None)
        if existing is not None:
            self._retire_effect(existing)
        # No art, no problem: anchor the dome to whatever stands in for the
        # mon (its name tag when there is no sprite, item B).
        center = self._slot_center(slot)
        size = 84
        shield = QLabel(self.fx_layer)
        shield.setAttribute(Qt.WA_TransparentForMouseEvents)
        shield.setStyleSheet(
            "background: rgba(77, 208, 255, 0.32);"
            "border: 2px solid rgba(140, 225, 255, 0.9);"
            "border-radius: 42px;"
        )
        self._shields[slot] = shield
        self._effect_widgets.append(shield)
        anim = QVariantAnimation(self)
        anim.setStartValue(QRect(center.x() - 6, center.y() - 6, 12, 12))
        anim.setEndValue(QRect(center.x() - size // 2, center.y() - size // 2, size, size))
        anim.setDuration(_EFFECT_SHIELD_MS)
        anim.setEasingCurve(QEasingCurve.OutBack)
        anim.valueChanged.connect(shield.setGeometry)
        # On finish the shield is simply left sitting (no _done retire);
        # _clear_turn_effects removes it when the turn ends.
        self._keep_animation(anim)
        shield.show()
        shield.raise_()
        anim.start()

    def _remove_shield(self, slot):
        """Drops a slot's persistent protector shield (item D). Called when
        the protect actually fails to be replaced, or when its mon faints so
        a shield can never be left floating over an empty slot. Safe to call
        for a slot with no shield."""
        shield = self._shields.pop(slot, None)
        if shield is not None:
            self._retire_effect(shield)

    def _clear_turn_effects(self):
        """Retires every transient/persistent effect widget drawn since
        the last frame (projectiles, bursts, and any holdover shields).
        Ran at the top of _render_frame, so a protect shield survives
        every remaining event of its turn and is dropped when the next
        turn actually renders."""
        for w in list(self._effect_widgets):
            self._retire_effect(w)
        self._shields.clear()
        # Substitute dolls are frame state too: drop any that belonged to
        # the previous turn (a fresh frame re-raises the ones still up).
        for slot in list(self._sub_dolls):
            self._set_sub_doll(slot, False)

    def _dash_attack(self, slot, dst_label, on_hit=None, burst=False):
        """A contact move: the attacker lunges toward the target and
        back while flashing white (Close Combat, Flip Turn, ...). `burst`
        adds a big ground burst on landing ('slam' moves -- Earthquake,
        Self-Destruct, ...). `on_hit` (autoplay) fires at the lunge's
        peak -- the moment contact lands."""
        self._stop_idle_bob(slot)
        label = self.sprite_labels[slot]
        base = label.geometry()
        src_c = label.mapTo(self.board_container, label.rect().center())
        dst_c = dst_label.mapTo(self.board_container, dst_label.rect().center())
        dx, dy = dst_c.x() - src_c.x(), dst_c.y() - src_c.y()
        mid = (int(dx * 0.45), int(dy * 0.45))
        self._bounce_geometry(slot, label, base, [
            (0.0, base),
            (0.35, base.translated(mid[0], mid[1])),
            (0.6, base.translated(mid[0], mid[1])),
            (1.0, base),
        ], _EFFECT_DASH_MS)
        self._flash_sprite(slot, QColor(255, 255, 255))
        if burst:
            self._impact_burst(dst_c, "#ffd180", size=46, grow=3.0)
        if on_hit is not None:
            # Contact lands about 60% through the lunge; sync the damage
            # flash / bar drain / '-N' badge to that moment, not the launch.
            QTimer.singleShot(int(_EFFECT_DASH_MS * 0.6), on_hit)

    def _fake_out(self, slot, dst_label, on_hit=None):
        """Fake Out: a quick 1-2 jab into the target. `on_hit` (autoplay)
        fires with the second jab so the damage lands on contact."""
        self._stop_idle_bob(slot)
        label = self.sprite_labels[slot]
        base = label.geometry()
        src_c = label.mapTo(self.board_container, label.rect().center())
        dst_c = dst_label.mapTo(self.board_container, dst_label.rect().center())
        dx, dy = dst_c.x() - src_c.x(), dst_c.y() - src_c.y()
        jab = (int(dx * 0.25), int(dy * 0.25))
        self._bounce_geometry(slot, label, base, [
            (0.0, base),
            (0.18, base.translated(jab[0], jab[1])),
            (0.34, base),
            (0.52, base.translated(jab[0], jab[1])),
            (1.0, base),
        ], 260)
        self._impact_burst(dst_c, "#ff5252", size=22, grow=2.5)
        if on_hit is not None:
            QTimer.singleShot(210, on_hit)

    def _earthquake_fx(self, slot, target_slots):
        """Earthquake & friends: the whole board rumbles while jagged
        fissures crack the ground open under every target. `on_hit`
        (autoplay) is already scheduled by _animate_move, so this only
        draws the shake + cracks + dust."""
        self._stop_idle_bob(slot)
        self._flash_sprite(slot, QColor(255, 214, 90))
        board = self.board_container
        base = board.pos()
        restore = lambda: board.move(base)
        self._surge_restore["__quake__"] = restore
        shake = QVariantAnimation(self)
        shake.setDuration(_EFFECT_QUAKE_MS)
        shake.setEasingCurve(QEasingCurve.OutCubic)
        shake.setStartValue(0.0)
        shake.setEndValue(1.0)
        shake.valueChanged.connect(
            lambda v: _quake_lurch(board, base, v)
        )

        def _done():
            if self._surge_restore.get("__quake__") is restore:
                self._surge_restore.pop("__quake__", None)
                restore()

        shake.finished.connect(_done)
        self._keep_animation(shake)
        for t in target_slots:
            self._spawn_crack(t)
        shake.start()

    def _spawn_crack(self, slot):
        """One earthquake fissure: fade a jagged crack in under the slot's
        art, kick a dust puff up at the rumble's peak, then retire it."""
        label = self._effect_label(slot)
        base_pt = label.mapTo(self.board_container, QPoint(label.width() // 2, label.height()))
        base_w, base_h = 150, 96
        crack = QLabel(self.fx_layer)
        crack.setAttribute(Qt.WA_TransparentForMouseEvents)
        crack.setPixmap(_make_crack_pixmap(base_w, base_h))
        crack.setFixedSize(base_w, base_h)
        crack.move(base_pt.x() - base_w // 2, base_pt.y() - base_h)
        self._effect_widgets.append(crack)
        crack.show()
        crack.raise_()
        effect = QGraphicsOpacityEffect(crack)
        crack.setGraphicsEffect(effect)
        effect.setOpacity(0.0)
        alpha = QVariantAnimation(self)
        alpha.setDuration(int(_EFFECT_QUAKE_MS * 0.75))
        alpha.setEasingCurve(QEasingCurve.OutCubic)
        alpha.setStartValue(0.0)
        alpha.setKeyValueAt(0.2, 1.0)
        alpha.setKeyValueAt(0.7, 0.95)
        alpha.setEndValue(0.0)
        alpha.valueChanged.connect(effect.setOpacity)
        alpha.finished.connect(lambda: self._retire_effect(crack))
        self._keep_animation(alpha)
        QTimer.singleShot(
            int(_EFFECT_QUAKE_MS * 0.45),
            lambda c=base_pt: self._impact_burst(c, "#8d6e63", size=16, grow=2.8)
        )
        alpha.start()

    def _lightning_fx(self, slot, dst_label, on_hit=None):
        """Thunderbolt & friends: a jagged bolt drops from the sky onto
        the target, flash-flickering yellow-gold, with a thinner fork
        arcing off the shaft."""
        self._stop_idle_bob(slot)
        self._flash_sprite(slot, QColor(255, 235, 119))
        tip = dst_label.mapTo(self.board_container, QPoint(dst_label.width() // 2, 0))
        bolt_w, bolt_h = 130, 160
        bolt = QLabel(self.fx_layer)
        bolt.setAttribute(Qt.WA_TransparentForMouseEvents)
        bolt.setPixmap(_make_bolt_pixmap(bolt_w, bolt_h))
        bolt.setFixedSize(bolt_w, bolt_h)
        bolt.move(tip.x() - bolt_w // 2, tip.y() - int(bolt_h * 0.85))
        self._effect_widgets.append(bolt)
        bolt.show()
        bolt.raise_()
        effect = QGraphicsOpacityEffect(bolt)
        bolt.setGraphicsEffect(effect)
        effect.setOpacity(0.0)
        flicker = QVariantAnimation(self)
        flicker.setDuration(_EFFECT_LIGHTNING_MS)
        flicker.setEasingCurve(QEasingCurve.OutCubic)
        flicker.setStartValue(0.0)
        flicker.setKeyValueAt(0.08, 1.0)
        flicker.setKeyValueAt(0.13, 0.15)
        flicker.setKeyValueAt(0.19, 0.95)
        flicker.setKeyValueAt(0.27, 0.35)
        flicker.setKeyValueAt(0.5, 0.9)
        flicker.setEndValue(0.0)
        flicker.valueChanged.connect(effect.setOpacity)
        flicker.finished.connect(lambda: self._retire_effect(bolt))
        self._keep_animation(flicker)
        flicker.start()
        land_ms = int(_EFFECT_LIGHTNING_MS * 0.32)
        QTimer.singleShot(
            land_ms,
            lambda c=tip: self._impact_burst(c, "#ffe082", size=30, grow=2.6)
        )
        if on_hit is not None:
            QTimer.singleShot(land_ms, on_hit)

    def _wave_fx(self, slot, dst_label, on_hit=None):
        """Surf / Hydro Pump: a wall of sea sweeps from the attacker
        across the field and breaks over the target at the end."""
        self._stop_idle_bob(slot)
        self._flash_sprite(slot, QColor(120, 200, 255))
        label = self.sprite_labels[slot]
        start = label.mapTo(self.board_container, label.rect().center())
        end = dst_label.mapTo(self.board_container, dst_label.rect().center())
        dx, dy = end.x() - start.x(), end.y() - start.y()
        dist = max(70, int(math.hypot(dx, dy)))
        wall_w, wall_h = dist + 30, 34
        wave = QLabel(self.fx_layer)
        wave.setAttribute(Qt.WA_TransparentForMouseEvents)
        wave.setPixmap(_make_wave_wall_pixmap(wall_w, wall_h))
        wave.setFixedSize(wall_w, wall_h)
        self._effect_widgets.append(wave)
        wave.show()
        wave.raise_()
        effect = QGraphicsOpacityEffect(wave)
        wave.setGraphicsEffect(effect)
        effect.setOpacity(0.0)
        alpha = QVariantAnimation(self)
        alpha.setDuration(int(_EFFECT_WAVE_MS * 1.1))
        alpha.setStartValue(0.0)
        alpha.setKeyValueAt(0.2, 0.95)
        alpha.setKeyValueAt(0.75, 0.95)
        alpha.setEndValue(0.0)
        alpha.valueChanged.connect(effect.setOpacity)
        self._keep_animation(alpha)
        alpha.start()
        anim = QVariantAnimation(self)
        anim.setDuration(_EFFECT_WAVE_MS)
        anim.setEasingCurve(QEasingCurve.InOutQuad)
        anim.setStartValue(QPoint(start.x() - wall_w, start.y() - wall_h // 2))
        anim.setEndValue(QPoint(end.x() - wall_w, end.y() - wall_h // 2))
        anim.valueChanged.connect(wave.move)
        anim.finished.connect(lambda: self._retire_effect(wave))
        self._keep_animation(anim)
        anim.start()

        splash_ms = int(_EFFECT_WAVE_MS * 0.72)
        QTimer.singleShot(splash_ms, lambda c=end: self._wave_splash(c))
        if on_hit is not None:
            QTimer.singleShot(splash_ms, on_hit)

    def _wave_splash(self, center):
        """The wave lands: a long blue break with a white foam pop."""
        self._impact_burst(center, "#29b6f6", size=30, grow=3.0)
        self._impact_burst(center, "#ffffff", size=14, grow=2.0)

    def _spawn_ring(self, center, color, size, grow, ms, delay=0):
        """One translucent distortion ripple that swells outward from
        `center` (tab coords), fading in and back out."""
        ring = QLabel(self.fx_layer)
        ring.setAttribute(Qt.WA_TransparentForMouseEvents)

        def style(s):
            return ("background: rgba(0,0,0,0);"
                    f"border: 3px solid {color}; border-radius: {s // 2}px;")

        ring.setStyleSheet(style(size))
        self._effect_widgets.append(ring)
        ring.show()
        ring.raise_()
        anim = QVariantAnimation(self)
        anim.setStartValue(
            QRect(center.x() - size // 2, center.y() - size // 2, size, size))
        end_s = int(size * grow)
        anim.setEndValue(
            QRect(center.x() - end_s // 2, center.y() - end_s // 2, end_s, end_s))
        anim.setDuration(ms)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.valueChanged.connect(
            lambda r, w=ring: self._ring_draw(w, r, style))
        anim.finished.connect(lambda w=ring: self._retire_effect(w))
        self._keep_animation(anim)
        effect = QGraphicsOpacityEffect(ring)
        ring.setGraphicsEffect(effect)
        effect.setOpacity(0.0)
        alpha = QVariantAnimation(self)
        alpha.setDuration(ms + 80)
        alpha.setStartValue(0.0)
        alpha.setKeyValueAt(0.3, 0.95)
        alpha.setKeyValueAt(0.7, 0.95)
        alpha.setEndValue(0.0)
        alpha.valueChanged.connect(effect.setOpacity)
        self._keep_animation(alpha)
        alpha.start()
        if delay:
            QTimer.singleShot(delay, anim.start)
        else:
            anim.start()

    @staticmethod
    def _ring_draw(ring, r, style):
        try:
            ring.setGeometry(r)
            ring.setStyleSheet(style(r.width()))
        except RuntimeError:
            pass

    def _psychic_distortion_fx(self, slot, dst_label, on_hit=None):
        """Psychic & friends: violet distortion rings swell around the
        target -- the first drives through, a second wider one sweeps
        past, then a soft violet pop marks the hit."""
        end = dst_label.mapTo(self.board_container, dst_label.rect().center())
        self._spawn_ring(end, "#ab7fff", size=30, grow=7.5, ms=_EFFECT_PSYCHIC_MS)
        self._spawn_ring(end, "#7e57c2", size=64, grow=5.5,
                         ms=_EFFECT_PSYCHIC_MS, delay=140)
        QTimer.singleShot(
            int(_EFFECT_PSYCHIC_MS * 0.9),
            lambda c=end: self._impact_burst(c, "#b39ddb", size=26, grow=2.2))
        if on_hit is not None:
            QTimer.singleShot(int(_EFFECT_PSYCHIC_MS * 0.72), on_hit)

    def _flame_blast_fx(self, slot, dst_label, on_hit=None):
        """Fire Blast & friends: a burning comet streaks at the target,
        trailing embers, and detonates into a four-pointed fire star."""
        self._stop_idle_bob(slot)
        self._flash_sprite(slot, QColor(255, 176, 80))
        label = self.sprite_labels[slot]
        start = label.mapTo(self.board_container, label.rect().center())
        end = dst_label.mapTo(self.board_container, dst_label.rect().center())
        flame = self._make_orb("#ff6d2a", 26)
        self._effect_widgets.append(flame)
        flame.move(start.x() - flame.width() // 2, start.y() - flame.height() // 2)
        flame.show()
        flame.raise_()
        anim = QVariantAnimation(self)
        anim.setStartValue(flame.pos())
        anim.setEndValue(QPoint(end.x() - flame.width() // 2,
                                end.y() - flame.height() // 2))
        anim.setDuration(_EFFECT_FLAME_MS)
        anim.setEasingCurve(QEasingCurve.OutQuad)
        anim.valueChanged.connect(flame.move)
        self._keep_animation(anim)
        for frac in (0.35, 0.65):
            fpos = QPoint(int(start.x() + (end.x() - start.x()) * frac),
                          int(start.y() + (end.y() - start.y()) * frac))
            QTimer.singleShot(int(_EFFECT_FLAME_MS * frac),
                              lambda pos=fpos: self._spawn_ember(pos))

        def _blast():
            self._retire_effect(flame)
            self._spawn_flame_star(end)
            self._impact_burst(end, "#ff8a50", size=30, grow=3.0)
            if on_hit is not None:
                on_hit()

        anim.finished.connect(_blast)
        anim.start()

    def _spawn_ember(self, pos):
        """A tiny fading fire flake trailing the flame-blast comet."""
        ember = QLabel(self.fx_layer)
        ember.setAttribute(Qt.WA_TransparentForMouseEvents)
        ember.setStyleSheet(
            "background: qradialgradient(cx:0.5, cy:0.5, radius:0.5,"
            "fx:0.5, fy:0.5, stop:0 #fff3c4, stop:0.5 #ff9a3a,"
            "stop:1 rgba(255,90,20,0)); border-radius: 10px;"
        )
        ember.setFixedSize(20, 20)
        ember.move(pos.x() - 10, pos.y() - 10)
        self._effect_widgets.append(ember)
        ember.show()
        ember.raise_()
        effect = QGraphicsOpacityEffect(ember)
        ember.setGraphicsEffect(effect)
        effect.setOpacity(0.0)
        anim = QVariantAnimation(self)
        anim.setDuration(240)
        anim.setStartValue(0.0)
        anim.setKeyValueAt(0.25, 0.9)
        anim.setEndValue(0.0)
        anim.valueChanged.connect(effect.setOpacity)
        anim.finished.connect(lambda: self._retire_effect(ember))
        self._keep_animation(anim)
        anim.start()

    def _spawn_flame_star(self, center):
        """The 4-point fire star pops out at the blast's detonation."""
        size = 96
        star = QLabel(self.fx_layer)
        star.setAttribute(Qt.WA_TransparentForMouseEvents)
        star.setFixedSize(size, size)
        star.setPixmap(_make_flame_star_pixmap(size))
        star.move(center.x() - size // 2, center.y() - size // 2)
        self._effect_widgets.append(star)
        star.show()
        star.raise_()
        effect = QGraphicsOpacityEffect(star)
        star.setGraphicsEffect(effect)
        effect.setOpacity(0.0)
        alpha = QVariantAnimation(self)
        alpha.setDuration(360)
        alpha.setEasingCurve(QEasingCurve.OutCubic)
        alpha.setStartValue(0.0)
        alpha.setKeyValueAt(0.15, 1.0)
        alpha.setKeyValueAt(0.65, 0.9)
        alpha.setEndValue(0.0)
        alpha.valueChanged.connect(effect.setOpacity)
        alpha.finished.connect(lambda: self._retire_effect(star))
        self._keep_animation(alpha)
        alpha.start()

    def _barrage_fx(self, slot, dst_label, on_hit=None):
        """Surging Strikes / Population Bomb / Triple Axel: the attacker
        lunges in and a rapid string of hits pops over the target; the
        last one marks the real damage (`on_hit`)."""
        self._stop_idle_bob(slot)
        label = self.sprite_labels[slot]
        base = label.geometry()
        src_c = label.mapTo(self.board_container, label.rect().center())
        dst_c = dst_label.mapTo(self.board_container, dst_label.rect().center())
        dx, dy = dst_c.x() - src_c.x(), dst_c.y() - src_c.y()
        mid = (int(dx * 0.3), int(dy * 0.3))
        self._bounce_geometry(slot, label, base, [
            (0.0, base),
            (0.2, base.translated(mid[0], mid[1])),
            (0.45, base.translated(mid[0], mid[1])),
            (1.0, base),
        ], 640)
        self._flash_sprite(slot, QColor(255, 255, 255))
        hits = 6
        pop = ("#ffffff", "#ffd54f", "#ff8a50")
        for i in range(hits):
            t = 0.15 + 0.13 * i
            spot = QPoint(int(src_c.x() + dx * t), int(src_c.y() + dy * t))
            delay = 120 + i * 70
            QTimer.singleShot(delay, lambda s=spot, j=i: self._impact_burst(
                s, pop[j % 3], size=8 + j * 3, grow=2.0, ms=220))
        last_ms = 120 + (hits - 1) * 70
        QTimer.singleShot(last_ms, lambda c=dst_c: self._impact_burst(
            c, "#ff7043", size=30, grow=2.6))
        if on_hit is not None:
            QTimer.singleShot(last_ms, on_hit)

    def _ice_crystal_fx(self, slot, dst_label, on_hit=None):
        """Ice Beam / Blizzard: an ice shard streaks to the target and
        blooms into a frozen 20-flash of pale blue."""
        self._stop_idle_bob(slot)
        self._flash_sprite(slot, QColor(180, 224, 255))
        label = self.sprite_labels[slot]
        start = label.mapTo(self.board_container, label.rect().center())
        end = dst_label.mapTo(self.board_container, dst_label.rect().center())
        size = 30
        shard = QLabel(self.fx_layer)
        shard.setAttribute(Qt.WA_TransparentForMouseEvents)
        shard.setFixedSize(size, size)
        shard.setPixmap(_make_ice_shard_pixmap(size))
        shard.move(start.x() - size // 2, start.y() - size // 2)
        self._effect_widgets.append(shard)
        shard.show()
        shard.raise_()
        anim = QVariantAnimation(self)
        anim.setStartValue(shard.pos())
        anim.setEndValue(QPoint(end.x() - size // 2, end.y() - size // 2))
        anim.setDuration(int(_EFFECT_FROST_MS * 0.6))
        anim.setEasingCurve(QEasingCurve.OutQuad)
        anim.valueChanged.connect(shard.move)
        self._keep_animation(anim)

        def _crash():
            self._retire_effect(shard)
            self._impact_burst(end, "#e1f5fe", size=30, grow=2.6)
            self._impact_burst(end, "#ade7ff", size=50, grow=2.0)
            if on_hit is not None:
                on_hit()

        anim.finished.connect(_crash)
        anim.start()

    def _self_pulse(self, slot):
        """A teal pulse around a setup move (Shell Smash, Swords Dance,
        Tailwind, ...)."""
        label = self.sprite_labels[slot]
        center = label.mapTo(self.board_container, label.rect().center())
        self._impact_burst(center, "#26c6da", size=34, grow=2.0, ms=_EFFECT_SELF_MS)

    # -- idle bobbing --

    def _start_idle_bob(self, slot):
        """Gentle Showdown-style vertical bobbing for a sprite label.

        Deferred until the board has actually been laid out: right after a
        game is selected (or on the very first render) the tile layout
        hasn't assigned real geometry yet and a sprite label's geometry can
        still be the default (0,0) rect. Bobbing from that base would eject
        the sprite into the tile corner (or, on the bottom row, off-tile).
        _ensure_idle_bobs re-invites slots once the layout has settled."""
        if not self._animate or slot in self._idle_bob_timers:
            return
        if not self._board_laid_out:
            return
        label = self.sprite_labels[slot]
        base = label.geometry()
        if base.width() <= 0 or base.height() <= 0:
            return
        phase = [0.0]
        timer = QTimer(self)
        timer.timeout.connect(lambda: self._bob_tick(slot, label, base, phase))
        timer.start(_BOB_TICK_MS)
        self._idle_bob_timers[slot] = timer
        self._idle_bob_base[slot] = base

    def _ensure_idle_bobs(self):
        """(Re)starts idle bobs for every slot currently showing art.
        Called once the board is laid out (showEvent) and again shortly
        after each _relayout_board, so deferred bobs begin anchored to
        real tile geometry instead of a stale (0,0) rect."""
        if not self._board_laid_out:
            return
        for slot in _SLOT_ORDER:
            if slot in self._slot_sprite_key:
                self._start_idle_bob(slot)

    def _bob_tick(self, slot, label, base, phase):
        phase[0] += _BOB_TICK_MS / 1000.0
        cur = label.geometry()
        # If base was captured before layout positioned the label (0,0), or if
        # mid-bob relayout shifted the label, drift base to actual position.
        if base.x() == 0 and base.y() == 0:
            if cur.x() != 0 or cur.y() != 0:
                base = cur
                self._idle_bob_base[slot] = base
            else:
                return
        elif abs(cur.y() - base.y()) > _BOB_AMPLITUDE * 4:
            base = cur
            self._idle_bob_base[slot] = base
        dy = int(_BOB_AMPLITUDE * math.sin(2 * math.pi * phase[0] / _BOB_PERIOD_S))
        label.move(base.x(), base.y() + dy)

    def _stop_idle_bob(self, slot):
        timer = self._idle_bob_timers.pop(slot, None)
        if timer is not None:
            timer.stop()
            timer.deleteLater()
        base = self._idle_bob_base.pop(slot, None)
        if base is not None:
            self.sprite_labels[slot].setGeometry(base)

    def _restart_idle_bob(self, slot):
        if slot in self._surge_restore:
            return  # a bounce animation still owns this slot's motion
        if slot in self._slot_sprite_key:
            self._start_idle_bob(slot)

    # -- animation lifecycle --

    def _keep_animation(self, anim):
        """Holds a reference to a running animation so it can't be
        garbage-collected mid-flight, and prunes it once it finishes."""
        self._animations.append(anim)
        anim.finished.connect(lambda a=anim: self._drop_animation(a))

    def _drop_animation(self, anim):
        if anim in self._animations:
            self._animations.remove(anim)

    def _stop_all_animations(self):
        """Stops every running board effect. Called when the selected
        game changes so nothing from one replay leaks into the next."""
        for slot in list(self._idle_bob_timers):
            self._stop_idle_bob(slot)
        for slot, restore in list(self._surge_restore.items()):
            restore()
        self._surge_restore.clear()
        for anim in self._animations:
            anim.stop()
        self._animations.clear()
        for slot in _SLOT_ORDER:
            self._remove_slot_effect(slot)
        for w in list(self._effect_widgets):
            self._retire_effect(w)
        self._fading_slots = set()
        self._prev_hp_pct = {}  # a new game drains from nothing, not the old game

    # ------------------------------------------------------------ layout

    def _detect_back_side(self, game_id, p1_name, p2_name):
        """Which side is "you" for a game. A configured username match
        wins (the log's own player names are authoritative for that
        replay); otherwise the importer's resolved side (games.my_side)
        is used; p1 is the last resort for games whose players never
        matched any username."""
        if self.usernames:
            if p1_name in self.usernames:
                return "p1"
            if p2_name in self.usernames:
                return "p2"
        my_side = get_my_side(self.conn, game_id)
        if my_side in ("p1", "p2"):
            return my_side
        return "p1"

    def _relayout_board(self):
        """Arranges the board grid so the user's side is always the
        bottom row (where back-view sprites live), and writes the
        player-name labels -- "(you)" marks your side. The slot cells
        and side labels are created once in __init__ and just moved
        between grid cells on game selection."""
        while self.board_grid.count():
            self.board_grid.takeAt(0)
        top_side = "p2" if self._back_side == "p1" else "p1"
        self._top_side = top_side  # row convention for name placement
        for row, side in enumerate((top_side, self._back_side)):
            # Column 0 is a small vbox: the player name over their
            # side's condition chips (Tailwind, screens, hazards...).
            side_cell = QWidget()
            side_box = QVBoxLayout(side_cell)
            side_box.setContentsMargins(4, 2, 4, 2)
            label = self.side_labels[side]
            name = self._side_names.get(side, "")
            if side == self._back_side:
                label.setText(f"{name} (you)" if name else "You")
                label.setStyleSheet(f"font-weight: bold; padding: 2px; color: {self._tokens['VIOLET']};")
            else:
                label.setText(name or "Opponent")
                label.setStyleSheet(f"font-weight: bold; padding: 2px; color: {self._tokens['MUTED']};")
            side_box.addWidget(label)
            side_box.addWidget(self.side_cond_icons[side])
            side_box.addWidget(self.side_cond_labels[side])
            side_box.addStretch(1)
            self.board_grid.addWidget(side_cell, row, 0)
            for col, slot in enumerate((side + "a", side + "b")):
                name_above = side == top_side  # opponent row: name on top
                self._configure_slot_cell(slot, name_above)
                self.board_grid.addWidget(self.cell_widgets[slot], row, col + 1)
        # Idle bobs are deferred so they anchor to real tile geometry after
        # this synchronous pass. The UI-layer placement runs now through
        # _place_scene_overlay (it ends by calling _place_ui_layer); the
        # authoritative reposition lands on the first real layout/size.
        QTimer.singleShot(0, self._ensure_idle_bobs)
        self._place_scene_overlay()

    def _configure_slot_cell(self, slot, name_above):
        """One tile now holds just the sprite (name_above is retained for
        layout symmetry but the name + HP bar are UI). The name + HP bar
        live in the top ui_layer -- positioned over this tile by
        _place_ui_layer -- so they stack above the hazards+screens layer
        while the sprite stays a step below it."""
        cell = self.cell_widgets[slot]
        box = cell.layout()
        while box.count():
            box.takeAt(0)
        box.addStretch(1)
        box.addWidget(self.sprite_labels[slot])
        box.addStretch(1)

    def _last_known_mon(self, slot):
        """Species last seen in a slot, scanning back from the current
        frame -- a faint is recorded in a frame whose end-state board
        already cleared the slot, so the name comes from just before."""
        for frame in reversed(self._frames[: self._frame_index + 1]):
            mon = frame.get("board", {}).get(slot)
            if mon:
                return mon
        return None

    # ------------------------------------------------------------ sprites

    def _sprite_requests(self):
        """(species, back) pairs this game needs, deduplicated. The
        user's side (self._back_side) renders as back sprites and the
        opponent's as front sprites, matching Showdown's orientation."""
        requests = set()
        for frame in self._frames:
            for slot, mon in frame.get("board", {}).items():
                if mon:
                    side = "p1" if slot.startswith("p1") else "p2"
                    requests.add((mon, side == self._back_side))
        return sorted(requests)

    def _start_sprite_fetch(self):
        """Ensures the background sprite service exists and queues the
        selected game's sprite list to it. Called on every game
        selection (including every switch back to this tab), so it must
        be cheap and safe to re-run while a previous fetch is still in
        flight: only ONE thread + worker are ever created, and queued
        re-requests just re-check the disk cache for anything already
        resolved. Offline-safe: failures just never emit sprite_ready,
        and the board keeps its text labels."""
        if not self._auto_fetch_sprites or not self._sprites_enabled:
            return
        requests = self._sprite_requests()
        if not requests:
            return
        if self._sprite_store is None:
            self._sprite_store = SpriteStore(style=self._sprite_style)
        self._ensure_sprite_thread()
        self._sprite_worker.request.emit(requests)

    def _ensure_sprite_thread(self):
        """Creates the tab's single sprite-fetching QThread + worker on
        first use and wires their signals. Idempotent: later calls just
        post more request batches onto the same thread. The worker is
        kept referenced on self so it can never be garbage-collected
        while its thread is mid-fetch."""
        if self._sprite_thread is not None:
            return
        thread = QThread()
        worker = SpriteFetchWorker(self._sprite_store)
        worker.moveToThread(thread)
        worker.request.connect(worker.fetch)  # queued -> runs on worker thread
        worker.sprite_ready.connect(self._on_sprite_ready)
        self._sprite_thread = thread
        self._sprite_worker = worker
        thread.start()

    def _shutdown_sprite_thread(self):
        """Stops the background sprite service. Called on app quit (and
        by tests) so the QThread object is never destroyed while its OS
        thread is still running -- that combination is a hard Qt fatal
        error. request_stop() aborts the worker between species, so at
        most one in-flight download (bounded by its own timeout) can
        delay the join. wait(5000) ensures we don't block shutdown
        indefinitely if the worker is stuck."""
        if self._sprite_worker is not None:
            self._sprite_worker.request_stop()
        if self._sprite_thread is not None:
            self._sprite_thread.quit()
            # Wait up to 5 seconds; if the thread doesn't exit, force-terminate
            if not self._sprite_thread.wait(5000):
                self._sprite_thread.terminate()
                self._sprite_thread.wait()
        self._sprite_thread = None
        self._sprite_worker = None

    def _on_sprite_ready(self, species, back, animated):
        """A sprite landed: swap it into any current-frame slot that
        shows that species, without forcing a full re-render."""
        if not self._sprites_enabled or self._sprite_store is None:
            return
        frame = self._frame()
        if frame is None:
            return
        for slot in _SLOT_ORDER:
            side = "p1" if slot.startswith("p1") else "p2"
            if (side == self._back_side) == back and frame.get("board", {}).get(slot) == species:
                self._set_best_sprite(slot, species, back)

    def _flush_slot_frame(self, slot, _frame_number=-1):
        """Per-frame synchronous flush so a QMovie frame can never sit
        stale in the QGraphicsProxyWidget's backing store and blink in
        and out on a real display.

        The board view's FullViewportUpdate plus an async viewport
        update() only force the VIEW to repaint -- the proxy can still
        serve the previous frame from its backing store, and update()
        is deferred/coalesced, so the ordering between the widget's
        frame paint and the viewport paint is a race. repaint() paints
        immediately (the documented tool for animations), so:

          1. the slot's sprite label paints NOW -- flushing this frame
             into the proxy's widget tree / backing store, and
          2. the viewport paints NOW -- blitting the fresh frame.

        QMovie.frameChanged fires from the movie's timer, never during
        a paint pass, so repaint() here cannot re-enter painting. Both
        steps are guarded for teardown (slot cleared / view destroyed
        mid-fetch).
        """
        try:
            if slot in self._slot_movies:
                self.sprite_labels[slot].repaint()  # frame -> backing store
        except (AttributeError, RuntimeError):
            pass
        try:
            self.board_view.viewport().repaint()  # backing store -> screen
        except (AttributeError, RuntimeError):
            pass

    def _set_slot_sprite(self, slot, path, animated):
        """Shows a cached sprite in a board slot, replacing any
        previous art (and stopping the old animation). Returns True if
        the art actually changed -- callers use that to fire the
        switch-in / mega-evolution effects exactly once."""
        key = (str(path), animated)
        if self._slot_sprite_key.get(slot) == key:
            return False  # already showing this exact file -- avoid flicker
        self._clear_slot_sprite(slot)
        label = self.sprite_labels[slot]
        if animated:
            movie = QMovie(str(path))
            # If the animated file can't be decoded on this platform
            # (invalid/empty frames), bail out (False) so the caller's
            # _set_best_sprite can fall back to the static variant --
            # never render a blank slot.
            if not movie.isValid():
                return False
            # Load first frame to determine native aspect ratio
            movie.jumpToFrame(0)
            native_size = movie.currentImage().size()
            if native_size.isValid() and native_size.width() > 0 and native_size.height() > 0:
                # Calculate size preserving aspect ratio within max bounds
                max_size = QSize(_SPRITE_SIZE, _SPRITE_SIZE)
                scaled_size = native_size.scaled(max_size, Qt.KeepAspectRatio)
                movie.setScaledSize(scaled_size)
            else:
                # Fallback to square if we can't determine native size
                movie.setScaledSize(QSize(_SPRITE_SIZE, _SPRITE_SIZE))
            label.setMovie(movie)
            self._slot_movies[slot] = movie
            # Per-frame synchronous flush: force this frame into the
            # proxy's backing store (label.repaint) and then onto the
            # screen (viewport.repaint). FullViewportUpdate alone only
            # repaints the VIEW, so it cannot cure a stale backing store.
            try:
                hook = functools.partial(self._flush_slot_frame, slot)
                movie.frameChanged.connect(hook)
                self._slot_frame_hooks[slot] = hook
            except Exception:
                self._slot_frame_hooks.pop(slot, None)
                # QMovie.frameChanged is standard -- compatibility no-op.
            movie.start()
        else:
            pixmap = QPixmap(str(path))
            if pixmap.isNull():
                return False
            label.setPixmap(
                pixmap.scaled(
                    _SPRITE_SIZE, _SPRITE_SIZE,
                    Qt.KeepAspectRatio, Qt.SmoothTransformation,
                )
            )
        label.setVisible(True)
        self._slot_sprite_key[slot] = key
        if self._animate:
            self._start_idle_bob(slot)
            if slot in self._pending_switch_in:
                self._fade_in_slot(slot)
            elif slot in self._pending_mega_flash:
                self._mega_flash(slot)
        return True

    def _set_best_sprite(self, slot, species, back):
        """Shows the best decodable art for a species in a slot. Prefers
        the animated sprite, but if that file can't be decoded on this
        platform (_set_slot_sprite returns False) falls back to the static
        variant before giving up, so a slot is never left blank over a
        decode hiccup. Returns True if a sprite is showing.

        IMPORTANT: re-rendering the same frame (step, game switch, a late
        download) must NEVER blank a slot. _set_slot_sprite returns False
        both when the art just failed to decode AND when it's already
        showing that exact file -- only the former should trigger a static
        fallback, and a fallback to a file we're already showing is still a
        success, not a reason to clear the slot.
        """
        if self._sprite_store is None:
            return False
        path, animated = self._sprite_store.cached(species, back)
        if path is not None:
            if self._slot_sprite_key.get(slot) == (str(path), animated):
                return True  # already showing exactly this art -- keep it
            if self._set_slot_sprite(slot, path, animated):
                return True
            # The preferred file couldn't be decoded -- fall back to the
            # static variant, and don't blank a slot already showing it.
            if animated:
                static, _ = self._sprite_store.cached_static(species, back)
                if static is not None:
                    if self._slot_sprite_key.get(slot) == (str(static), False):
                        return True
                    if self._set_slot_sprite(slot, static, False):
                        return True
        return False

    def _on_sprite_style_changed(self, text):
        """User picked a new sprite style (Gen 5 <-> 3D/XY) in the toolbar.
        Persists the choice, updates the store (which rebuilds its style-
        aware folder chain + failure cache), and re-renders the board
        (fetching the new art for whatever's on the field right now)."""
        style = "3d" if text.startswith("3D") else "gen5"
        if style == self._sprite_style:
            return
        self._sprite_style = style
        config.set_sprite_style(style)
        if self._sprite_store is not None:
            self._sprite_store.set_style(style)
        # Wipe every slot's current art so the new style is fetched/shown
        for slot in _SLOT_ORDER:
            self._clear_slot_sprite(slot)
        if self._frame_index >= 0 and self._frames:
            self._render_frame()

    def on_settings_changed(self):
        """
        Called when the Settings dialog writes new config (via the
        settings_applied signal). Re-reads the sprite-related settings
        from disk and refreshes the board so the UI picks up changes
        made in the Settings dialog immediately, without needing a restart.
        """
        # Sprite style (Gen 5 vs 3D/XY)
        new_style = config.get_sprite_style()
        if new_style != self._sprite_style:
            self._sprite_style = new_style
            if self._sprite_store is not None:
                self._sprite_store.set_style(new_style)
            for slot in _SLOT_ORDER:
                self._clear_slot_sprite(slot)

        # Sprites on/off
        new_sprites_enabled = config.get_use_sprites()
        if new_sprites_enabled != self._sprites_enabled:
            self._sprites_enabled = new_sprites_enabled
            if not self._sprites_enabled:
                for slot in _SLOT_ORDER:
                    self._clear_slot_sprite(slot)

        # Board animation on/off
        self._animate = config.get_animate_board()

        # Re-render the current frame with updated settings
        if self._frame_index >= 0 and self._frames:
            self._render_frame()

    def _clear_slot_sprite(self, slot):
        """Drops whatever art a slot shows and hides its sprite label,
        so the name text underneath stands alone again."""
        self._stop_idle_bob(slot)
        self._remove_slot_effect(slot)
        movie = self._slot_movies.pop(slot, None)
        if movie is not None:
            self._slot_frame_hooks.pop(slot, None)
            # PySide6 cannot disconnect a functools.partial by identity
            # (it emits "Failed to disconnect" and raises TypeError), so
            # drop EVERY connection on this movie instead -- the only
            # frameChanged subscribers this app ever registers are the
            # flush hooks added in _set_slot_sprite, so this is exactly
            # equivalent and always succeeds quietly.
            try:
                movie.frameChanged.disconnect()
            except (RuntimeError, TypeError):
                pass
            movie.stop()
        label = self.sprite_labels[slot]
        label.setMovie(None)
        label.clear()
        label.setVisible(False)
        self._slot_sprite_key.pop(slot, None)

    # ---------------------------------------------------------- transport

    def _update_controls_enabled(self):
        has_frames = bool(self._frames)
        self.play_button.setEnabled(has_frames)
        self.step_back_button.setEnabled(has_frames)
        self.step_forward_button.setEnabled(has_frames)
        self.speed_combo.setEnabled(has_frames)

    def _update_timer_interval(self):
        self.timer.setInterval(_SPEEDS.get(self.speed_combo.currentText(), 1500))

    def toggle_play(self):
        if self.timer.isActive():
            self._stop_playback()
            return
        if self._frame_index >= len(self._frames) - 1 or self._frame_index < 0:
            # At the end (or empty): restart from the first event.
            self._frame_index = 0
            self._event_index = 0
            self._render_frame()
        self.timer.start()
        self.play_button.setText("Pause")

    def _advance_one_event(self):
        """Autoplay tick: play ONE real battle event (move / switch / damage /
        heal / faint / mega) in the exact order the log records it, spaced out
        by the timer's beat. This is what makes a turn read move-by-move:

          * entering a new turn draws its INCOMING state (the prior turn's end
            board) -- the whole turn is never snapshotted/burst at once, and
          * each tick then renders just that event's change (its HP drain,
            switch-in, mega, faint, or move animation) with the movelog
            highlighting exactly that event.

        Turns that recorded no events are passed over without a visible pause.
        """
        if not self._frames:
            self._stop_playback()
            return
        if self._frame_index < 0:
            self._frame_index = 0
        frame = self._frame()
        while frame is not None and self._event_index >= len(frame.get("events", [])):
            self._frame_index += 1
            self._event_index = 0
            if self._frame_index >= len(self._frames):
                self._frame_index = len(self._frames) - 1
                self._event_index = 0
                self._render_frame()
                self._stop_playback()
                return
            frame = self._frame()
        if frame is None:
            self._stop_playback()
            return
        fi = self._frame_index  # the frame the event being played belongs to
        if self._event_index == 0:
            # New turn: draw the field as it reaches it.
            self._render_turn_entrance(frame)
        ev = frame["events"][self._event_index]
        first = self._event_index
        self._event_index += 1
        self._render_autoplay_frame(frame, self._event_index)
        self._update_movelog(active_index=first, limit=self._event_index)
        if ev[0] == "move":
            _side, slot, _mon, _move, target, _target_mon, targets, _species, *_rest = ev[1:]
            if not targets:
                targets = [target] if target else []

            events = frame.get("events", [])
            # Item A: the move's beat will draw its own damage, so mark that
            # damage event as covered -- when ITS beat arrives, the hurt
            # animation must NOT play a second time. Move targets only: a
            # recoil / status hit on the attacker is no move target and
            # still flashes on its own beat.
            hit_idx = self._following_damage_index(frame, first)
            if (hit_idx is not None
                    and events[hit_idx][0] in ("damage", "heal")
                    and events[hit_idx][1] in targets):
                self._hit_events[(fi, hit_idx)] = False  # pending: this beat shows it

            def _on_hit():
                # The move's effect has landed: only now do the target
                # shake, the bar drain, and the '-N' badge appear, timed
                # with the orb's burst / the lunge's contact.
                if not self._animate:
                    return
                for t in targets:
                    if (t and t != slot and t not in self._fading_slots
                            and t in self._slot_sprite_key):
                        self._damage_flash(t)
                self._sync_move_damage(frame, first, fi)

            if not self._play_event_effect(ev, on_hit=_on_hit):
                _on_hit()  # no travel (surge/pulse/shield): hit lands on the beat
        else:
            if (ev[0] in ("damage", "heal")
                    and (fi, first) in self._hit_events):
                pass  # already drawn on the move's beat (item A)
            else:
                self._play_event_effect(ev)
            # Punch in the OHKO banner the moment the KO target's faint
            # ("Sylveon fainted!") is revealed -- not when the turn ends.
            if ev[0] == "faint" and ev[1] in (frame.get("ohko_slots") or []):
                self._update_ohko(frame)

    def _render_turn_entrance(self, frame):
        """Fresh autoplay entry into a turn: just the incoming board, the
        turn number and conditions -- no end-of-turn snapshot, no move effects,
        so nothing is pre-applied (item B)."""
        self._clear_turn_effects()  # a prior turn's protects must not linger
        self.turn_label.setText(f"Turn {frame.get('turn', '?')}")
        self._fading_slots = set()
        board, hp, subs = self._point_state(self._frame_index, 0)
        self._render_board_state(board, hp, subs)
        self._update_conditions(frame)

    def _render_autoplay_frame(self, frame, event_index):
        """Draws the board after `event_index` events of the current turn have
        been applied, so each autoplay tick shows exactly one event's HP/board
        change (a damage drains the bar, a faint empties the slot, a switch
        brings its mon in). Keeps movelog/conditions current; the visual effect
        itself is handled by _play_event_effect in the caller."""
        board, hp, subs = self._point_state(self._frame_index, event_index)
        events = frame.get("events", [])
        active = event_index - 1
        self._fading_slots = (
            {events[active][1]}
            if (0 <= active < len(events) and events[active][0] == "faint") else set()
        )
        self._render_board_state(board, hp, subs)
        self._update_conditions(frame)

    def _render_board_state(self, board, hp, substitutes=None):
        """Renders slots from the running board/hp maps. Empty slots that are
        mid-faint-fade keep their sprite up (the fade callback finalizes the
        slot to '--'); everything else shows this exact battle moment."""
        for slot in _SLOT_ORDER:
            mon = board.get(slot)
            if not mon:
                if slot in self._fading_slots:
                    continue  # faint fade in flight: keep the sprite up
                self._finalize_empty_slot(slot)
                continue
            self.board_labels[slot].setText(
                self._name_markup(self._frame(), slot, mon))
            side = "p1" if slot.startswith("p1") else "p2"
            back = side == self._back_side
            accent = self._tokens['VIOLET'] if back else self._tokens['MUTED']
            showing = False
            if self._sprites_enabled and self._sprite_store is not None:
                showing = self._set_best_sprite(slot, mon, back)
            if showing:
                self._style_slot_label(slot, has_sprite=True)
            else:
                self._clear_slot_sprite(slot)
                self._style_slot_label(slot, has_sprite=False)
            cur, max_hp = self._hp_values(hp.get(slot, ""))
            pct = int(cur * 100 / max_hp) if max_hp else 0
            self._set_hp_bar(slot, pct)
            self.hp_bars[slot].setFormat("%p%" if max_hp else "")
            
            # Determine HP color token
            if cur == 0:
                hp_color = self._tokens['MUTED']
            elif pct <= 20:
                hp_color = self._tokens['DANGER']
            elif pct <= 50:
                hp_color = self._tokens['WARNING']
            else:
                hp_color = self._tokens['HEALTHY']
                
            self.hp_bars[slot].setStyleSheet(f"QProgressBar {{ background: transparent; border: none; }} QProgressBar::chunk {{ background: {hp_color}; }}")
        self._apply_sub_dolls(substitutes)

    def _following_damage_index(self, frame, move_index):
        """Index of the first damage/heal event right after a move (skipping
        only the item/ability popups the parser inserts between them), or
        None when the move has no hit/heal to draw. That is the event whose
        own beat must not replay the move's hurt animation (item A)."""
        events = frame.get("events", [])
        nxt = move_index + 1
        while nxt < len(events) and events[nxt][0] == "popup":
            nxt += 1
        if nxt < len(events) and events[nxt][0] in ("damage", "heal"):
            return nxt
        return None

    def _sync_move_damage(self, frame, move_index, frame_index=None):
        """Item A: pull a move's very next damage/heal onto the move's own
        beat -- the target's bar drains and a '-N' popup floats up while the
        attack lands, instead of only a beat later. Purely cosmetic: the
        event stream still advances exactly one event per tick, and the
        damage event that follows re-applies the same value (a no-op drain).

        Autoplay now defers this until the move's effect arrives (its orb
        bursts / the lunge connects), so callers normally go through the
        move's on_hit callback; `frame_index` pins the frame the beat
        belongs to for that deferred moment. A (frame, move) is only ever
        synced once, so a replay-restart or a fast beat can't double-fire
        the '-N' badge."""
        if not self._animate:
            return
        if frame_index is None:
            try:
                frame_index = self._frames.index(frame)
            except ValueError:
                return
        key = (frame_index, move_index)
        if key in self._synced_moves:
            return
        events = frame.get("events", [])
        nxt = move_index + 1
        # Skip over any item/ability popups between the move and its damage
        # so the drain still lands on the move's own beat (item G popups).
        while nxt < len(events) and events[nxt][0] == "popup":
            nxt += 1
        if nxt >= len(events) or events[nxt][0] not in ("damage", "heal"):
            return
        slot = events[nxt][1]
        if slot not in self._slot_sprite_key or slot in self._fading_slots:
            return
        hp_new = events[nxt][2] if len(events[nxt]) > 2 else ""
        _board, hp_pre, _subs = self._point_state(frame_index, nxt)
        old_cur, _old_max = self._hp_values(hp_pre.get(slot, ""))
        new_cur, capacity = self._hp_values(hp_new)
        if not new_cur or new_cur == old_cur:
            return
        self._synced_moves.add(key)
        self._hit_events[(frame_index, nxt)] = True  # beat drawn: don't replay it
        self._set_hp_bar(slot, int(100 * new_cur / capacity) if capacity else 0)
        lost = old_cur - new_cur
        if lost > 0:
            self._spawn_damage_popup(slot, lost)

    def _spawn_damage_popup(self, slot, lost):
        """A small '-N' badge that rises from a target sprite and fades as
        it goes -- the move-synced damage text for item A."""
        label = self.sprite_labels[slot]
        center = label.mapTo(self.board_container, label.rect().center())
        pop = QLabel(f"-{lost}", self.fx_layer)
        pop.setAttribute(Qt.WA_TransparentForMouseEvents)
        pop.setStyleSheet(
            "color: #fff; font-weight: bold; font-size: 12px;"
            "background: rgba(211, 47, 47, 0.82); border-radius: 5px;"
            "padding: 1px 4px;"
        )
        pop.adjustSize()
        start = QPoint(center.x() - pop.width() // 2, center.y() - 24)
        pop.move(start)
        pop.show()
        pop.raise_()
        self._effect_widgets.append(pop)
        anim = QVariantAnimation(self)
        anim.setStartValue(start)
        anim.setEndValue(QPoint(start.x(), start.y() - 30))
        anim.setDuration(750)
        anim.valueChanged.connect(pop.move)
        anim.finished.connect(lambda: self._retire_effect(pop))
        self._keep_animation(anim)
        anim.start()

    def _spawn_popup(self, slot, kind, name):
        """A small floating badge beside a mon naming the item/ability that
        just activated -- rises and fades like the '-N' damage badge
        (item G). Announced as a badge for items/abilities; pure announce
        messages only write to the movelog."""
        if slot and slot in self._slot_sprite_key:
            label = self.sprite_labels[slot]
            center = label.mapTo(self.board_container, label.rect().center())
            start = QPoint(center.x(), center.y() - 40)
        else:
            center = self.board_container.rect().center()
            center = self.board_container.mapTo(self.board_container, center)
            start = QPoint(center.x(), center.y() - 40)
        pop = QLabel(f"{name}", self.fx_layer)
        pop.setAttribute(Qt.WA_TransparentForMouseEvents)
        color = "#f9a825" if kind == "item" else "#5e35b1"
        pop.setStyleSheet(
            f"color: #fff; font-weight: bold; font-size: 11px;"
            f"background: {color}; border-radius: 5px; padding: 1px 5px;"
        )
        pop.adjustSize()
        start = QPoint(start.x() - pop.width() // 2, start.y())
        pop.move(start)
        pop.show()
        pop.raise_()
        self._effect_widgets.append(pop)
        anim = QVariantAnimation(self)
        anim.setStartValue(start)
        anim.setEndValue(QPoint(start.x(), start.y() - 26))
        anim.setDuration(1100)
        anim.valueChanged.connect(pop.move)
        anim.finished.connect(lambda: self._retire_effect(pop))
        self._keep_animation(anim)
        anim.start()

    def _spawn_stat_badge(self, slot, stat, stages, down=False):
        """A tiny rising '+2 ATK' / '-1 Sp. Def' badge beside the mon whose
        stat just changed -- green for boosts, red for drops (Swords Dance,
        Close Combat's defense drop, ...). Built like the item/ability
        popup so it reads as part of the same visual language; spriteless
        mons get the badge over the board's center like other effects."""
        stat_name = _STAT_NAMES.get(stat, stat or "stats")
        sign = "-" if down else "+"
        text = f"{sign}{stages} {stat_name}"
        if slot and slot in self._slot_sprite_key:
            label = self.sprite_labels[slot]
            center = label.mapTo(self.board_container, label.rect().center())
        else:
            center = self.board_container.rect().center()
            center = self.board_container.mapTo(self.board_container, center)
        pop = QLabel(text, self.fx_layer)
        pop.setAttribute(Qt.WA_TransparentForMouseEvents)
        pop.setStyleSheet(
            f"color: #fff; font-weight: bold; font-size: 12px;"
            f"background: {'#c62828' if down else '#2e7d32'};"
            f"border-radius: 6px; padding: 1px 6px;"
        )
        pop.adjustSize()
        start = QPoint(center.x() - pop.width() // 2, center.y() - 46)
        pop.move(start)
        pop.show()
        pop.raise_()
        self._effect_widgets.append(pop)
        anim = QVariantAnimation(self)
        anim.setStartValue(start)
        anim.setEndValue(QPoint(start.x(), start.y() - 30))
        anim.setDuration(1000)
        anim.valueChanged.connect(pop.move)
        anim.finished.connect(lambda: self._retire_effect(pop))
        self._keep_animation(anim)
        anim.start()

    def _stop_playback(self):
        self.timer.stop()
        self.play_button.setText("Play")

    def _tick(self):
        self._advance_one_event()

    def step_forward(self):
        if self.timer.isActive():
            self._stop_playback()
        if self._frames and self._frame_index < len(self._frames) - 1:
            self._frame_index += 1
            self._event_index = 0
            self._render_frame()

    def step_back(self):
        if self.timer.isActive():
            self._stop_playback()
        if self._frames and self._frame_index > 0:
            self._frame_index -= 1
            self._event_index = 0
            self._render_frame()

