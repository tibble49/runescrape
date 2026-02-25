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
        # Defaults: collect both accounts
        entries = [
            ("tibble49", "regular"),
            ("xespis",   "hardcore_ironman"),
        ]
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
