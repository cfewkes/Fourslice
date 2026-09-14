"""
parser.py

Parses Pokemon Showdown VGC doubles replay logs into a tidy,
turn-by-turn event table. This is a direct port of the validated
Apps Script logic from the original Google Sheets version
(normalizeMon / PARSEGAMEEVENTS) -- same behavior, same output
shape, just Python instead of JS.
"""

import re
import time

import requests


def extract_regulation(format_str: str) -> str:
    match = re.search(r"Reg\s+([A-Za-z0-9\-]+)", format_str)
    return match.group(1) if match else format_str


def is_random_battle(format_str: str) -> bool:
    return "random" in format_str.lower()


def detect_battle_size(log_text: str) -> str:
    sides = {"p1": set(), "p2": set()}
    for line in log_text.split("\n"):
        tokens = line.split("|")
        if len(tokens) < 2:
            continue
        tag = tokens[1]
        if tag == "turn":
            break
        if tag in ("switch", "drag"):
            slot = tokens[2].split(": ")[0]
            sides[slot[:2]].add(slot)

    if len(sides["p1"]) == 1 and len(sides["p2"]) == 1:
        return "singles"
    if len(sides["p1"]) == 2 and len(sides["p2"]) == 2:
        return "doubles"
    return "other"

def normalize_mon(raw_name: str) -> str:
    name = raw_name.split(",")[0]
    if name == "Palafin-Hero":
        name = "Palafin"
    if name in ("Terapagos-Terastal", "Terapagos-Stellar"):
        name = "Terapagos"
    if "-Teal-Tera" in name:
        name = "Ogerpon"
    if "-Tera" in name:
        name = name.replace("-Tera", "")
    if "-Mega-Y" in name:
        name = name.replace("-Mega-Y", "")
    if "-Mega-X" in name:
        name = name.replace("-Mega-X", "")
    if "-Mega" in name:
        name = name.replace("-Mega", "")
    return name


def _parse_hp_field(raw: str) -> str | None:
    """
    Extracts the leading current/max HP from a Showdown HP field,
    e.g. "100/100", "28/100", "0 fnt", or "534/534 par".
    Returns None when nothing parseable is there.
    """
    if not raw:
        return None
    match = re.match(r"^\s*(\d+)/(\d+)", raw)
    return f"{match.group(1)}/{match.group(2)}" if match else None


def _row(game_id, url, turn, side, slot, mon, event_type, board, hp_snapshot):
    return {
        "GameID": game_id, "ReplayURL": url, "Turn": turn,
        "Side": side, "Slot": slot, "Mon": mon, "EventType": event_type,
        "P1a": board[0], "P1b": board[1], "P2a": board[2], "P2b": board[3],
        "HP1a": hp_snapshot[0], "HP1b": hp_snapshot[1], "HP2a": hp_snapshot[2], "HP2b": hp_snapshot[3],
    }


def parse_game_events(log_text: str, url: str) -> list[dict]:
    """
    Parses one battle's raw log text into a list of event rows: one
    per turn-start, one per move, one per faint, each carrying the
    full 4-slot board state at that moment, plus (for faints) which
    Pokemon gets KO credit -- see the "elif tag == '-damage'" branch
    below for the attribution logic and its documented limits.

    NOTE: does not yet include the Zoroark/illusion backward-correction
    that the original REPLAYTODATA does -- a mon revealed as illusion
    mid-game will show up under the fake name until that's ported too.
    """
    game_id = url.rstrip("/").split("/")[-1]
    active = {"p1a": None, "p1b": None, "p2a": None, "p2b": None}
    hp = {"p1a": None, "p1b": None, "p2a": None, "p2b": None}
    turn_num = 0
    rows = []
    p1_name = p2_name = winner = format_str = ""

    # KO-credit bookkeeping -- see the "elif tag == '-damage'" branch
    # below for the actual attribution logic and its documented limits.
    current_move_attacker = (None, None)  # (mon, side) of whoever most recently used a move
    current_move_targets = set()          # slots that move's damage can be credited to
    ko_credit_by_slot = {}                # slot -> (attacker_mon, attacker_side) or (None, None)

    def board_snapshot():
        return [active["p1a"], active["p1b"], active["p2a"], active["p2b"]]

    def hp_snapshot():
        return [hp["p1a"], hp["p1b"], hp["p2a"], hp["p2b"]]

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

        elif tag == "turn":
            # Guard against malformed turn tokens (e.g., |turn| or |turn|abc)
            if len(tokens) < 3:
                continue
            try:
                turn_num = int(tokens[2])
            except (ValueError, IndexError):
                continue
            rows.append(_row(game_id, url, turn_num, "", "", "", "turnStart", board_snapshot(), hp_snapshot()))

        elif tag in ("switch", "drag"):
            if len(tokens) < 4:
                continue
            try:
                slot = tokens[2].split(": ")[0]
                active[slot] = normalize_mon(tokens[3])
                parsed_hp = _parse_hp_field(tokens[4]) if len(tokens) > 4 else None
                if parsed_hp:
                    hp[slot] = parsed_hp
            except (IndexError, ValueError):
                continue

        elif tag == "move":
            slot = tokens[2].split(": ")[0]
            side = "p1" if slot[1] == "1" else "p2"
            rows.append(_row(game_id, url, turn_num, side, slot, active[slot], tokens[3], board_snapshot(), hp_snapshot()))

            current_move_attacker = (active[slot], side)
            current_move_targets = set()
            if len(tokens) > 4 and ": " in tokens[4] and not tokens[4].startswith("["):
                explicit_target = tokens[4].split(": ")[0]
                if explicit_target in active:
                    current_move_targets.add(explicit_target)
            for field in tokens[5:]:
                if field.startswith("[spread]"):
                    # [spread] lists the slots that ACTUALLY took damage (after redirects,
                    # misses on one of two targets, immunities, etc.) -- overrides the
                    # explicit single-target field above, which is only the nominal target.
                    current_move_targets = {t.strip() for t in field[len("[spread]"):].split(",") if t.strip()}

        elif tag in ("-damage", "-heal"):
            if len(tokens) < 3:
                continue
            try:
                target_slot = tokens[2].split(": ")[0]
                parsed_hp = _parse_hp_field(tokens[3]) if len(tokens) > 3 else None
                if parsed_hp:
                    # Fatal hits arrive as "0 fnt" -- the slot stays synced
                    # so the replay timeline shows an empty bar.
                    hp[target_slot] = parsed_hp

                if tag == "-damage":
                    # KO-credit attribution (the replay timeline shows a "knocked
                    # out" marker rather than an empty bar). Fatal hits arrive
                    # as "0 fnt" in the HP field.
                    is_fatal = len(tokens) > 3 and "fnt" in tokens[3]
                    if is_fatal:
                        has_from_tag = any(f.startswith("[from]") for f in tokens[4:])
                        if not has_from_tag and target_slot in current_move_targets:
                            # Clean case: this exact damage line is a direct result of the
                            # most recently logged move, and that move's targets include this
                            # slot -- credit its attacker.
                            ko_credit_by_slot[target_slot] = current_move_attacker
                        else:
                            # A [from]-tagged fatal hit (residual damage, recoil, an item/ability
                            # proc) isn't a move-credited KO -- and a fatal hit with NO [from] tag
                            # that still doesn't match the current move's targets means the real
                            # cause wasn't captured by this parser (e.g. a charge/delayed move --
                            # Solar Beam, Fly, Future Sight -- whose real target only shows up on
                            # a separate |-anim| line this doesn't read, not on |move| itself).
                            # Either way: don't guess. Left unattributed.
                            ko_credit_by_slot[target_slot] = (None, None)
            except (IndexError, ValueError):
                continue

        elif tag == "faint":
            if len(tokens) < 3:
                continue
            try:
                slot = tokens[2].split(": ")[0]
                side = "p1" if slot[1] == "1" else "p2"
                row = _row(game_id, url, turn_num, side, slot, active[slot], "faint", board_snapshot(), hp_snapshot())
                row["KOCreditMon"], row["KOCreditSide"] = ko_credit_by_slot.pop(slot, (None, None))
                rows.append(row)
                active[slot] = None
                hp[slot] = None
            except (IndexError, ValueError):
                continue

    for row in rows:
        row["P1Name"] = p1_name
        row["P2Name"] = p2_name
        row["Winner"] = winner
        row["Format"] = format_str

    return rows

def is_mega_stone(item: str, species: str) -> bool:
    if not item:
        return False
    words = item.split(" ")
    if len(words) not in (1, 2):
        return False
    if len(words) == 2 and words[1] not in ("X", "Y", "Z"):
        return False
    if not words[0].endswith("ite"):
        return False
    return words[0][:4].lower() == species[:4].lower()


def parse_team_preview(log_text: str) -> dict:
    showteam_data = {"p1": [], "p2": []}
    poke_data = {"p1": [], "p2": []}

    for line in log_text.split("\n"):
        tokens = line.split("|")
        if len(tokens) < 4:
            continue

        if tokens[1] == "showteam":
            side = tokens[2]
            packed = "|".join(tokens[3:])
            for mon_str in packed.split("]"):
                fields = mon_str.split("|")
                if len(fields) < 5:
                    continue
                species = normalize_mon(fields[1] or fields[0])
                item = fields[2]
                moves = fields[4].split(",") if fields[4] else []
                showteam_data[side].append({"species": species, "item": item, "moves": moves})

        elif tokens[1] == "poke":
            side = tokens[2]
            species = normalize_mon(tokens[3])
            poke_data[side].append({"species": species, "item": "", "moves": []})

    return {
        "p1": showteam_data["p1"] or poke_data["p1"],
        "p2": showteam_data["p2"] or poke_data["p2"],
    }

def fetch_replay_json(url: str) -> dict:
    """Fetch replay JSON with retries and a proper User-Agent."""
    if url.endswith("?p2"):
        url = url[:-3]

    headers = {
        "User-Agent": "Fourslice-stats-crawler/0.1 (local Pokemon stats collector)"
    }

    # Retry up to 3 times with exponential backoff
    for attempt in range(3):
        try:
            resp = requests.get(url + ".json", timeout=15, headers=headers)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.JSONDecodeError:
            # Non-JSON response (HTML error page, block page, etc.)
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)  # Exponential backoff: 1s, 2s
        except requests.exceptions.RequestException:
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)

    # Should not reach here, but just in case
    raise requests.exceptions.RequestException(f"Failed to fetch {url} after 3 attempts")


def find_all_replay_urls_for_user(username: str, stop_when_seen: set[str] | None = None, max_pages: int = 100) -> list[str]:
    """
    Fetch replay URLs for a user, with protection against unbounded pagination.

    Args:
        username: Showdown username
        stop_when_seen: Set of already-imported game_ids to stop early
        max_pages: Maximum pages to fetch (default 100, prevents unbounded crawl)
    """
    urls = []
    page = 1
    last_id_seen = None
    seen_ids = set()  # For deduplication within this crawl

    while page <= max_pages:
        resp = requests.get(
            "https://replay.pokemonshowdown.com/search.json",
            params={"user": username, "page": page},
            timeout=15,
        )
        resp.raise_for_status()
        results = resp.json()
        if not results:
            break

        if last_id_seen is not None and results and "id" in results[0] and results[0]["id"] == last_id_seen:
            results = results[1:]

        reached_known = False
        for entry in results:
            # Guard against entries missing "id" field
            if "id" not in entry:
                continue
            game_id = entry["id"]

            # Skip duplicates within this crawl (handles pagination boundary shifts)
            if game_id in seen_ids:
                continue
            seen_ids.add(game_id)

            if stop_when_seen and game_id in stop_when_seen:
                reached_known = True
                break
            urls.append(f"https://replay.pokemonshowdown.com/{game_id}")
            last_id_seen = game_id
        if reached_known:
            break

        page += 1
    return urls

def game_id_from_url(url: str) -> str:
    return url.rstrip("/").split("/")[-1]


def game_id_sort_key(game_id: str) -> int:
    match = re.search(r"(\d+)$", game_id)
    return int(match.group(1)) if match else -1