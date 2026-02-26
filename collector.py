"""
collector.py — Fetch and store OSRS hiscore snapshots in SQLite.

Supports all game modes: regular, ironman, hardcore ironman, ultimate ironman, deadman, seasonal.

Run manually or schedule via Windows Task Scheduler to collect daily data.

Examples:
    python collector.py
    python collector.py --player tibble49
    python collector.py --player xespis --mode hardcore_ironman
    python collector.py --player tibble49 --mode regular --player xespis --mode hardcore_ironman
"""

import argparse
import os
import shutil
import requests
from datetime import datetime, timezone
from html.parser import HTMLParser
from sqlalchemy import insert

from db import (
    get_engine,
    get_database_url,
    init_db,
    is_postgres_url,
    snapshots_table,
    skill_data_table,
    minigame_data_table,
)

DB_FILE = os.getenv("OSRS_DB_PATH", "osrs_hiscores.db")
SEED_DB_FILE = os.getenv("OSRS_SEED_DB_PATH", "seed/osrs_hiscores_seed.sqlite3")

# All available game mode API endpoints
GAME_MODES = {
    "regular":          "https://secure.runescape.com/m=hiscore_oldschool/index_lite.ws",
    "ironman":          "https://secure.runescape.com/m=hiscore_oldschool_ironman/index_lite.ws",
    "hardcore_ironman": "https://secure.runescape.com/m=hiscore_oldschool_hardcore_ironman/index_lite.ws",
    "ultimate_ironman": "https://secure.runescape.com/m=hiscore_oldschool_ultimate/index_lite.ws",
    "deadman":          "https://secure.runescape.com/m=hiscore_oldschool_deadman/index_lite.ws",
    "seasonal":         "https://secure.runescape.com/m=hiscore_oldschool_seasonal/index_lite.ws",
}

SKILL_NAMES = [
    "Overall", "Attack", "Defence", "Strength", "Hitpoints", "Ranged",
    "Prayer", "Magic", "Cooking", "Woodcutting", "Fletching", "Fishing",
    "Firemaking", "Crafting", "Smithing", "Mining", "Herblore", "Agility",
    "Thieving", "Slayer", "Farming", "Runecraft", "Hunter", "Construction", "Sailing"
]

MINIGAME_NAMES = [
    "League Points", "Deadman Points",
    "Bounty Hunter - Hunter", "Bounty Hunter - Rogue",
    "Bounty Hunter (Legacy) - Hunter", "Bounty Hunter (Legacy) - Rogue",
    "Clue Scrolls (all)", "Clue Scrolls (beginner)", "Clue Scrolls (easy)",
    "Clue Scrolls (medium)", "Clue Scrolls (hard)", "Clue Scrolls (elite)",
    "Clue Scrolls (master)", "LMS - Rank", "PvP Arena - Rank",
    "Soul Wars Zeal", "Rifts closed", "Colosseum Glory",
    "Collections Logged", "Theatre of Blood",
    "Theatre of Blood: Hard Mode", "Chambers of Xeric",
    "Chambers of Xeric: Challenge Mode", "Tombs of Amascut",
    "Tombs of Amascut: Expert Mode", "TzKal-Zuk", "TzTok-Jad",
    "Corporeal Beast", "Nightmare", "Phosani's Nightmare",
    "Obor", "Bryophyta", "Mimic", "Hespori", "Skotizo",
    "Scurrius", "Vorkath", "Zalcano", "Wintertodt",
    "Tempoross", "Guardians of the Rift",
    "Abyssal Sire", "Cerberus", "Chaos Elemental", "Chaos Fanatic",
    "Commander Zilyana", "Crazy Archaeologist", "Dagannoth Prime",
    "Dagannoth Rex", "Dagannoth Supreme", "Deranged Archaeologist",
    "Duke Sucellus", "General Graardor", "Giant Mole",
    "Grotesque Guardians", "Kalphite Queen", "King Black Dragon",
    "Kraken", "Kree'Arra", "K'ril Tsutsaroth", "Lunar Chests",
    "Phantom Muspah", "Sarachnis", "Scorpia", "Sol Heredit",
    "Spindel", "Vardorvis", "Vetion", "Venenatis", "Zulrah"
]

ANCHOR_PLAYER = "XESPIS"
ANCHOR_MODE = "regular"
TRACK_AHEAD_COUNT = 10
TRACK_BEHIND_COUNT = 3

BASE_TRACKED_ENTRIES = [
    ("tibble49", "regular"),
    (ANCHOR_PLAYER, ANCHOR_MODE),
]


class OverallTableParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows: list[tuple[int, str]] = []
        self._in_tr = False
        self._in_td = False
        self._current_td = ""
        self._current_row: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._in_tr = True
            self._current_row = []
        elif tag == "td" and self._in_tr:
            self._in_td = True
            self._current_td = ""

    def handle_endtag(self, tag):
        if tag == "td" and self._in_td:
            self._in_td = False
            self._current_row.append(" ".join(self._current_td.split()))
        elif tag == "tr" and self._in_tr:
            self._in_tr = False
            if len(self._current_row) >= 3:
                rank_idx = None
                for idx, cell in enumerate(self._current_row):
                    candidate = cell.replace(",", "").strip()
                    if candidate.isdigit():
                        rank_idx = idx
                        break

                if rank_idx is not None and rank_idx + 1 < len(self._current_row):
                    player_name = self._current_row[rank_idx + 1].strip()
                    rank_text = self._current_row[rank_idx].replace(",", "").strip()
                    if rank_text.isdigit() and player_name:
                        self.rows.append((int(rank_text), player_name))

    def handle_data(self, data):
        if self._in_td and data:
            self._current_td += data


def ensure_seed_db() -> None:
    if is_postgres_url(get_database_url()):
        return

    if os.path.exists(DB_FILE):
        return

    db_dir = os.path.dirname(DB_FILE)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    if os.path.exists(SEED_DB_FILE):
        shutil.copyfile(SEED_DB_FILE, DB_FILE)
        print(f"Seeded SQLite DB from {SEED_DB_FILE}")


def fetch_raw(player: str, mode: str) -> list[str]:
    url = GAME_MODES[mode]
    resp = requests.get(url, params={"player": player}, timeout=10)
    if resp.status_code == 404:
        raise ValueError(f"Player '{player}' not found on {mode} hiscores.")
    resp.raise_for_status()
    return resp.text.strip().splitlines()


def get_overall_rank(player: str, mode: str = "regular") -> int | None:
    try:
        lines = fetch_raw(player, mode)
    except Exception:
        return None

    if not lines:
        return None

    parts = lines[0].split(",")
    if not parts:
        return None

    return parse_int(parts[0])


def fetch_overall_page_rows(page: int) -> list[tuple[int, str]]:
    url = "https://secure.runescape.com/m=hiscore_oldschool/overall"
    resp = requests.get(
        url,
        params={"table": 0, "page": page},
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=12,
    )
    resp.raise_for_status()

    parser = OverallTableParser()
    parser.feed(resp.text)
    return parser.rows


def get_neighbor_players(anchor_player: str, ahead_count: int, behind_count: int) -> list[str]:
    anchor_rank = get_overall_rank(anchor_player, ANCHOR_MODE)
    if not anchor_rank:
        return []

    start_rank = max(1, anchor_rank - ahead_count)
    end_rank = anchor_rank + behind_count

    # OSRS hiscore UI commonly uses page=1 for ranks 1-25 (page=0 can mirror first page).
    page_start = max(1, ((start_rank - 1) // 25) + 1)
    page_end = max(1, ((end_rank - 1) // 25) + 1)
    candidate_pages: list[int] = []

    for page_index in range(max(1, page_start - 1), page_end + 2):
        for candidate in (page_index - 1, page_index, page_index + 1):
            if candidate >= 0 and candidate not in candidate_pages:
                candidate_pages.append(candidate)

    rank_to_player: dict[int, str] = {}
    required_ranks = set(range(start_rank, end_rank + 1))

    for page in candidate_pages:
        try:
            rows = fetch_overall_page_rows(page)
        except Exception:
            continue

        for rank, player_name in rows:
            if start_rank <= rank <= end_rank and player_name:
                rank_to_player[rank] = player_name

        if required_ranks.issubset(rank_to_player.keys()):
            break

    if not rank_to_player:
        return []

    ordered_names: list[str] = []
    for rank in range(start_rank, end_rank + 1):
        name = rank_to_player.get(rank)
        if name and name.lower() not in {n.lower() for n in ordered_names}:
            ordered_names.append(name)

    if anchor_player.lower() not in {name.lower() for name in ordered_names}:
        ordered_names.append(anchor_player)

    return ordered_names


def build_default_entries() -> list[tuple[str, str]]:
    entries = BASE_TRACKED_ENTRIES.copy()
    neighbors = get_neighbor_players(ANCHOR_PLAYER, TRACK_AHEAD_COUNT, TRACK_BEHIND_COUNT)

    if neighbors:
        entries.extend((name, ANCHOR_MODE) for name in neighbors)
        print(
            f"Resolved neighbors around {ANCHOR_PLAYER}: {len(neighbors)} players "
            f"(target {TRACK_AHEAD_COUNT + TRACK_BEHIND_COUNT + 1})."
        )
    else:
        print("WARNING: Could not resolve XESPIS neighbors from overall hiscores; collecting base entries only.")

    deduped: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for player, mode in entries:
        key = (player.lower(), mode)
        if key in seen:
            continue
        seen.add(key)
        deduped.append((player, mode))

    return deduped


def parse_int(value: str) -> int | None:
    try:
        v = int(value)
        return None if v == -1 else v
    except (ValueError, TypeError):
        return None


def store_snapshot(engine, player: str, mode: str, lines: list[str]):
    now = datetime.now(timezone.utc)
    timestamp = now.isoformat()
    date = now.strftime("%Y-%m-%d")

    skill_rows = []
    minigame_rows = []

    for i, line in enumerate(lines):
        parts = line.split(",")
        if i < len(SKILL_NAMES):
            skill_rows.append((
                None,
                SKILL_NAMES[i],
                parse_int(parts[0]),
                parse_int(parts[1]),
                parse_int(parts[2]) if len(parts) > 2 else None
            ))
        else:
            mi = i - len(SKILL_NAMES)
            name = MINIGAME_NAMES[mi] if mi < len(MINIGAME_NAMES) else f"Activity {mi+1}"
            minigame_rows.append((
                None,
                name,
                parse_int(parts[0]),
                parse_int(parts[1]) if len(parts) > 1 else None
            ))

    with engine.begin() as conn:
        result = conn.execute(
            insert(snapshots_table).values(
                player=player.lower(),
                mode=mode,
                timestamp=timestamp,
                date=date,
            )
        )
        snap_id = result.inserted_primary_key[0]

        skill_payload = [
            {
                "snapshot_id": snap_id,
                "skill": row[1],
                "rank": row[2],
                "level": row[3],
                "xp": row[4],
            }
            for row in skill_rows
        ]
        minigame_payload = [
            {
                "snapshot_id": snap_id,
                "activity": row[1],
                "rank": row[2],
                "score": row[3],
            }
            for row in minigame_rows
        ]

        conn.execute(insert(skill_data_table), skill_payload)
        conn.execute(insert(minigame_data_table), minigame_payload)

    return snap_id


def collect(entries: list[tuple[str, str]]):
    """entries: list of (player_name, game_mode) tuples"""
    ensure_seed_db()
    engine = get_engine()
    init_db(engine)

    for player, mode in entries:
        label = f"{player} ({mode})"
        print(f"Fetching {label}...", end=" ")
        try:
            lines = fetch_raw(player, mode)
            snap_id = store_snapshot(engine, player, mode, lines)
            overall = lines[0].split(",") if lines else ["?", "?", "?"]
            level = overall[1] if len(overall) > 1 else "?"
            xp    = overall[2] if len(overall) > 2 else "?"
            print(f"OK (snapshot #{snap_id}, total level {level}, xp {xp})")
        except Exception as e:
            print(f"FAILED — {e}")


def main():
    parser = argparse.ArgumentParser(
        description="Collect OSRS hiscore snapshots",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python collector.py
  python collector.py --player tibble49 --mode regular
  python collector.py --player xespis --mode hardcore_ironman
  python collector.py --player tibble49 --mode regular --player xespis --mode hardcore_ironman

Available modes:
  regular, ironman, hardcore_ironman, ultimate_ironman, deadman, seasonal
        """
    )
    parser.add_argument("--player", action="append", dest="players", default=None,
                        help="Player name (can be used multiple times)")
    parser.add_argument("--mode", action="append", dest="modes", default=None,
                        help="Game mode for the corresponding --player (default: regular)")

    args = parser.parse_args()

    # Build list of (player, mode) pairs
    if not args.players:
        entries = build_default_entries()
        print(
            f"Default tracking set: {TRACK_AHEAD_COUNT} ahead + {TRACK_BEHIND_COUNT} behind {ANCHOR_PLAYER} "
            f"(total {len(entries)} players/modes)."
        )
    else:
        players = args.players
        modes   = args.modes or []
        # Pad modes with 'regular' if fewer modes than players were given
        while len(modes) < len(players):
            modes.append("regular")
        entries = list(zip(players, modes))

    # Validate modes
    for player, mode in entries:
        if mode not in GAME_MODES:
            print(f"ERROR: Unknown mode '{mode}' for player '{player}'.")
            print(f"Valid modes: {', '.join(GAME_MODES.keys())}")
            return

    collect(entries)


if __name__ == "__main__":
    main()
