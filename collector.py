"""
collector.py — Fetch and store OSRS hiscore snapshots in SQLite.

Supports all game modes: regular, ironman, hardcore ironman, ultimate ironman, deadman, seasonal.

Run manually or schedule via Windows Task Scheduler to collect daily data.

Optionally attaches quest summary counts from JSON export:
    assets/quest_status.json (or OSRS_QUEST_EXPORT_PATH)

Examples:
    python collector.py
    python collector.py --player tibble49
    python collector.py --player xespis --mode hardcore_ironman
    python collector.py --player tibble49 --mode regular --player xespis --mode hardcore_ironman
"""

import argparse
import json
import os
import shutil
import requests
from datetime import datetime, timezone, timedelta
from html.parser import HTMLParser
from sqlalchemy import insert
from sqlalchemy import text

from db import (
    get_engine,
    get_database_url,
    init_db,
    is_postgres_url,
    snapshots_table,
    skill_data_table,
    minigame_data_table,
    quest_summary_table,
)

DB_FILE = os.getenv("OSRS_DB_PATH", "osrs_hiscores.db")
SEED_DB_FILE = os.getenv("OSRS_SEED_DB_PATH", "seed/osrs_hiscores_seed.sqlite3")
DEAD_HCIM_FILE = os.getenv("OSRS_DEAD_HCIM_PATH", "assets/dead_hcim_players.json")
QUEST_EXPORT_FILE = os.getenv("OSRS_QUEST_EXPORT_PATH", "assets/quest_status.json")

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
ANCHOR_MODE = "hardcore_ironman"
TRACK_AHEAD_COUNT = 10
TRACK_BEHIND_COUNT = 3
INACTIVE_DAYS_LIMIT = 30

BASE_TRACKED_ENTRIES = [
    ("tibble49", "regular"),
    (ANCHOR_PLAYER, ANCHOR_MODE),
]

HISCORE_OVERALL_PAGES = {
    "regular": "https://secure.runescape.com/m=hiscore_oldschool/overall",
    "ironman": "https://secure.runescape.com/m=hiscore_oldschool_ironman/overall",
    "hardcore_ironman": "https://secure.runescape.com/m=hiscore_oldschool_hardcore_ironman/overall",
    "ultimate_ironman": "https://secure.runescape.com/m=hiscore_oldschool_ultimate/overall",
    "deadman": "https://secure.runescape.com/m=hiscore_oldschool_deadman/overall",
    "seasonal": "https://secure.runescape.com/m=hiscore_oldschool_seasonal/overall",
}

SKILL_TABLE_IDS = {skill: index for index, skill in enumerate(SKILL_NAMES)}


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


def load_dead_hcim_players() -> set[str]:
    try:
        if not os.path.exists(DEAD_HCIM_FILE):
            return set()
        with open(DEAD_HCIM_FILE, "r", encoding="utf-8") as f:
            payload = json.load(f)
        if not isinstance(payload, dict):
            return set()
        players = payload.get("players", [])
        if not isinstance(players, list):
            return set()
        return {
            str(name).strip().lower()
            for name in players
            if str(name).strip()
        }
    except Exception:
        return set()


def save_dead_hcim_players(players: set[str]) -> None:
    parent = os.path.dirname(DEAD_HCIM_FILE)
    if parent:
        os.makedirs(parent, exist_ok=True)

    payload = {
        "players": sorted(players),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    with open(DEAD_HCIM_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


def has_no_recent_xp_movement(engine, player: str, mode: str, days: int = INACTIVE_DAYS_LIMIT) -> bool:
    cutoff_dt = datetime.now(timezone.utc) - timedelta(days=days)
    cutoff_iso = cutoff_dt.isoformat()

    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT sd.xp
            FROM skill_data sd
            JOIN snapshots s ON s.id = sd.snapshot_id
            WHERE s.player = :player
              AND s.mode = :mode
              AND sd.skill = 'Overall'
              AND sd.xp IS NOT NULL
              AND s.timestamp >= :cutoff
            ORDER BY s.timestamp
        """), {"player": player.lower(), "mode": mode, "cutoff": cutoff_iso}).fetchall()

    xp_values: list[int] = []
    for row in rows:
        try:
            xp_values.append(int(row[0]))
        except (TypeError, ValueError):
            continue

    if len(xp_values) < 2:
        return False

    return min(xp_values) == max(xp_values)


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


def get_skill_rank(player: str, skill: str, mode: str = "regular") -> int | None:
    try:
        lines = fetch_raw(player, mode)
    except Exception:
        return None

    if not lines:
        return None

    try:
        skill_index = SKILL_NAMES.index(skill)
    except ValueError:
        return None

    if skill_index >= len(lines):
        return None

    parts = lines[skill_index].split(",")
    if not parts:
        return None

    return parse_int(parts[0])


def fetch_hiscore_table_rows(page: int, mode: str, table_id: int) -> list[tuple[int, str]]:
    url = HISCORE_OVERALL_PAGES.get(mode, HISCORE_OVERALL_PAGES["regular"])
    resp = requests.get(
        url,
        params={"table": table_id, "page": page},
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=12,
    )
    resp.raise_for_status()

    parser = OverallTableParser()
    parser.feed(resp.text)
    return parser.rows


def get_neighbor_players(
    anchor_player: str,
    ahead_count: int,
    behind_count: int,
    mode: str = ANCHOR_MODE,
    skill: str = "Overall",
    excluded_players: set[str] | None = None,
    engine=None,
    skip_inactive: bool = False,
    inactive_cache: dict[tuple[str, str], bool] | None = None,
    max_expand_pages: int = 8,
) -> list[str]:
    anchor_rank = get_skill_rank(anchor_player, skill, mode)
    if not anchor_rank:
        return []

    table_id = SKILL_TABLE_IDS.get(skill)
    if table_id is None:
        return []

    excluded = {p.lower() for p in (excluded_players or set())}
    anchor_lower = anchor_player.lower()
    start_rank = max(1, anchor_rank - ahead_count)
    end_rank = anchor_rank + behind_count
    rank_to_player: dict[int, str] = {}
    fetched_pages: set[int] = set()

    def _is_inactive(player_name: str) -> bool:
        if not skip_inactive or engine is None:
            return False
        key = (player_name.lower(), mode)
        if inactive_cache is not None and key in inactive_cache:
            return inactive_cache[key]
        inactive = has_no_recent_xp_movement(engine, player_name, mode)
        if inactive_cache is not None:
            inactive_cache[key] = inactive
        return inactive

    def _fetch_window(window_start: int, window_end: int) -> None:
        page_start = max(1, ((window_start - 1) // 25) + 1)
        page_end = max(1, ((window_end - 1) // 25) + 1)
        candidate_pages: list[int] = []

        # OSRS hiscore UI commonly uses page=1 for ranks 1-25 (page=0 can mirror first page).
        for page_index in range(max(1, page_start - 1), page_end + 2):
            for candidate in (page_index - 1, page_index, page_index + 1):
                if candidate >= 0 and candidate not in candidate_pages:
                    candidate_pages.append(candidate)

        for page in candidate_pages:
            if page in fetched_pages:
                continue
            fetched_pages.add(page)
            try:
                rows = fetch_hiscore_table_rows(page, mode, table_id)
            except Exception:
                continue

            for rank, player_name in rows:
                if window_start <= rank <= window_end and player_name:
                    rank_to_player[rank] = player_name

    chosen_ahead: list[tuple[int, str]] = []
    chosen_behind: list[tuple[int, str]] = []
    expand_pages = 0
    while True:
        window_start = max(1, start_rank - expand_pages * 25)
        window_end = end_rank + expand_pages * 25
        _fetch_window(window_start, window_end)

        ahead_candidates: list[tuple[int, str]] = []
        behind_candidates: list[tuple[int, str]] = []
        seen_names: set[str] = set()

        for rank in sorted(rank_to_player.keys()):
            player_name = rank_to_player[rank]
            player_lower = player_name.lower()
            if player_lower in seen_names:
                continue
            seen_names.add(player_lower)

            if player_lower == anchor_lower or player_lower in excluded:
                continue
            if _is_inactive(player_name):
                continue

            if rank < anchor_rank:
                ahead_candidates.append((rank, player_name))
            elif rank > anchor_rank:
                behind_candidates.append((rank, player_name))

        chosen_ahead = ahead_candidates[-ahead_count:]
        chosen_behind = behind_candidates[:behind_count]

        enough_ahead = len(chosen_ahead) >= ahead_count
        enough_behind = len(chosen_behind) >= behind_count
        if enough_ahead and enough_behind:
            break
        if expand_pages >= max_expand_pages:
            break
        expand_pages += 1

    if not rank_to_player:
        return []

    ordered_names = [name for _, name in chosen_ahead]
    ordered_names.append(anchor_player)
    ordered_names.extend(name for _, name in chosen_behind)

    return ordered_names


def build_default_entries() -> list[tuple[str, str]]:
    entries = BASE_TRACKED_ENTRIES.copy()
    dead_hcim_players = load_dead_hcim_players()
    engine = get_engine()
    init_db(engine)
    inactive_cache: dict[tuple[str, str], bool] = {}

    for skill in SKILL_NAMES:
        neighbors = get_neighbor_players(
            ANCHOR_PLAYER,
            TRACK_AHEAD_COUNT,
            TRACK_BEHIND_COUNT,
            ANCHOR_MODE,
            skill,
            excluded_players=dead_hcim_players,
            engine=engine,
            skip_inactive=True,
            inactive_cache=inactive_cache,
        )

        if neighbors:
            entries.extend((name, ANCHOR_MODE) for name in neighbors)
            print(
                f"Resolved {skill} neighbors around {ANCHOR_PLAYER}: {len(neighbors)} players "
                f"(target {TRACK_AHEAD_COUNT + TRACK_BEHIND_COUNT + 1})."
            )
        else:
            print(
                f"WARNING: Could not resolve {skill} neighbors for {ANCHOR_PLAYER}; "
                "continuing with other skills."
            )

    deduped: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for player, mode in entries:
        key = (player.lower(), mode)
        if mode == ANCHOR_MODE and player.lower() in dead_hcim_players:
            continue
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


def _parse_non_negative_int(value) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _parse_quest_entry(entry: dict) -> dict | None:
    if not isinstance(entry, dict):
        return None

    player = str(entry.get("player", "")).strip().lower()
    if not player:
        return None

    mode = str(entry.get("mode", "regular")).strip().lower() or "regular"

    completed = _parse_non_negative_int(entry.get("completed"))
    in_progress = _parse_non_negative_int(entry.get("in_progress", entry.get("started")))
    not_started = _parse_non_negative_int(entry.get("not_started"))

    if completed is None and in_progress is None and not_started is None:
        return None

    source = str(entry.get("source", "runelite_export")).strip() or "runelite_export"
    return {
        "player": player,
        "mode": mode,
        "completed": completed,
        "in_progress": in_progress,
        "not_started": not_started,
        "source": source,
    }


def load_quest_export_index() -> dict[tuple[str, str], dict]:
    """Loads optional quest summary exports keyed by (player, mode)."""
    if not os.path.exists(QUEST_EXPORT_FILE):
        return {}

    try:
        with open(QUEST_EXPORT_FILE, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except Exception as ex:
        print(f"WARNING: Could not parse {QUEST_EXPORT_FILE}: {ex}")
        return {}

    entries: list[dict] = []
    if isinstance(payload, list):
        entries = payload
    elif isinstance(payload, dict):
        if isinstance(payload.get("players"), list):
            entries = payload["players"]
        else:
            entries = [payload]

    index: dict[tuple[str, str], dict] = {}
    for entry in entries:
        parsed = _parse_quest_entry(entry)
        if not parsed:
            continue
        index[(parsed["player"], parsed["mode"])] = parsed

    return index


def store_snapshot(
    engine,
    player: str,
    mode: str,
    lines: list[str],
    quest_export_index: dict[tuple[str, str], dict] | None = None,
) -> tuple[int, bool]:
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

        quest_added = False
        if quest_export_index:
            quest_entry = quest_export_index.get((player.lower(), mode))
            if quest_entry:
                conn.execute(insert(quest_summary_table), {
                    "snapshot_id": snap_id,
                    "completed": quest_entry["completed"],
                    "in_progress": quest_entry["in_progress"],
                    "not_started": quest_entry["not_started"],
                    "source": quest_entry["source"],
                })
                quest_added = True

    return snap_id, quest_added


def collect(entries: list[tuple[str, str]], skip_inactive: bool = False):
    """entries: list of (player_name, game_mode) tuples"""
    ensure_seed_db()
    engine = get_engine()
    init_db(engine)
    quest_export_index = load_quest_export_index()
    if quest_export_index:
        print(
            f"Loaded quest summary export for {len(quest_export_index)} player/mode entries "
            f"from {QUEST_EXPORT_FILE}."
        )
    dead_hcim_players = load_dead_hcim_players()
    dead_hcim_changed = False

    for player, mode in entries:
        normalized_player = player.lower()
        if mode == ANCHOR_MODE and normalized_player in dead_hcim_players:
            print(f"Skipping {player} ({mode}) — marked dead/removed from HCIM hiscores.")
            continue

        if skip_inactive and has_no_recent_xp_movement(engine, player, mode):
            print(f"Skipping {player} ({mode}) — no Overall XP movement in last {INACTIVE_DAYS_LIMIT} days.")
            continue

        label = f"{player} ({mode})"
        print(f"Fetching {label}...", end=" ")
        try:
            lines = fetch_raw(player, mode)

            if mode == ANCHOR_MODE and normalized_player in dead_hcim_players:
                dead_hcim_players.discard(normalized_player)
                dead_hcim_changed = True

            snap_id, quest_added = store_snapshot(engine, player, mode, lines, quest_export_index)
            overall = lines[0].split(",") if lines else ["?", "?", "?"]
            level = overall[1] if len(overall) > 1 else "?"
            xp    = overall[2] if len(overall) > 2 else "?"
            quest_note = ", quest summary linked" if quest_added else ""
            print(f"OK (snapshot #{snap_id}, total level {level}, xp {xp}{quest_note})")
        except Exception as e:
            if mode == ANCHOR_MODE and "not found" in str(e).lower():
                if normalized_player not in dead_hcim_players:
                    dead_hcim_players.add(normalized_player)
                    dead_hcim_changed = True
                print("SKIPPED — missing from HCIM hiscores (likely dead/de-ranked).")
                continue
            print(f"FAILED — {e}")

    if dead_hcim_changed:
        save_dead_hcim_players(dead_hcim_players)
        print(f"Updated dead HCIM list: {len(dead_hcim_players)} players in {DEAD_HCIM_FILE}.")


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
            f"Default tracking set from Overall + all skills: {TRACK_AHEAD_COUNT} ahead + {TRACK_BEHIND_COUNT} behind {ANCHOR_PLAYER} "
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

    collect(entries, skip_inactive=not args.players)


if __name__ == "__main__":
    main()
