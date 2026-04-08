"""
dashboard.py — OSRS Hiscore Trend Dashboard
Run with: python dashboard.py
Then open http://127.0.0.1:8050 in your browser.

Requirements:
    pip install dash plotly pandas
"""

import os
import json
import shutil
import requests
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from functools import lru_cache
from html.parser import HTMLParser
from sqlalchemy import text
import dash
from dash import dcc, html, Input, Output, State, callback
from datetime import datetime, timezone, timedelta

from db import get_engine, get_database_url, init_db, is_postgres_url

DB_FILE = os.getenv("OSRS_DB_PATH", "osrs_hiscores.db")
SEED_DB_FILE = os.getenv("OSRS_SEED_DB_PATH", "seed/osrs_hiscores_seed.sqlite3")
DEAD_HCIM_FILE = os.getenv("OSRS_DEAD_HCIM_PATH", "assets/dead_hcim_players.json")
QUEST_EXPORT_FILE = os.getenv("OSRS_QUEST_EXPORT_PATH", "assets/quest_status.json")

SKILL_NAMES = [
    "Overall", "Attack", "Defence", "Strength", "Hitpoints", "Ranged",
    "Prayer", "Magic", "Cooking", "Woodcutting", "Fletching", "Fishing",
    "Firemaking", "Crafting", "Smithing", "Mining", "Herblore", "Agility",
    "Thieving", "Slayer", "Farming", "Runecraft", "Hunter", "Construction", "Sailing"
]

# Colour per skill (OSRS-inspired)
SKILL_COLORS = {
    "Overall": "#c8aa6e", "Attack": "#e03030", "Defence": "#4a90d9",
    "Strength": "#3cb371", "Hitpoints": "#808080", "Ranged": "#6aaa2a",
    "Prayer": "#e8d48b", "Magic": "#5555ff", "Cooking": "#b05020",
    "Woodcutting": "#2e8b57", "Fletching": "#3a7d44", "Fishing": "#4682b4",
    "Firemaking": "#ff8c00", "Crafting": "#c8a050", "Smithing": "#888888",
    "Mining": "#708090", "Herblore": "#228b22", "Agility": "#778899",
    "Thieving": "#9370db", "Slayer": "#cc2200", "Farming": "#8fbc8f",
    "Runecraft": "#daa520", "Hunter": "#8b4513", "Construction": "#d2b48c",
    "Sailing": "#20b2aa",
}

BG = "#0d0d0f"
CARD_BG = "#141418"
BORDER = "#2a2a35"
TEXT = "#e8e0d0"
TEXT_DIM = "#7a7a8a"
ACCENT = "#c8aa6e"
GREEN = "#4caf50"
RED = "#f44336"

ANCHOR_PLAYER = "XESPIS"
ANCHOR_MODE = "hardcore_ironman"
DISPLAY_AHEAD_COUNT = 3
DISPLAY_BEHIND_COUNT = 3
OVERALL_OUTLIER_XP_LIMIT = 30_000_000
SKILL_OUTLIER_XP_LIMIT = 5_000_000
INACTIVE_DAYS_LIMIT = 30

FALLBACK_COMPARE_PLAYER_NAMES = [
    "XESPIS",
]

DEFAULT_PLAYER = "tibble49"

HISCORE_LITE_ENDPOINTS = {
    "regular": "https://secure.runescape.com/m=hiscore_oldschool/index_lite.ws",
    "ironman": "https://secure.runescape.com/m=hiscore_oldschool_ironman/index_lite.ws",
    "hardcore_ironman": "https://secure.runescape.com/m=hiscore_oldschool_hardcore_ironman/index_lite.ws",
    "ultimate_ironman": "https://secure.runescape.com/m=hiscore_oldschool_ultimate/index_lite.ws",
    "deadman": "https://secure.runescape.com/m=hiscore_oldschool_deadman/index_lite.ws",
    "seasonal": "https://secure.runescape.com/m=hiscore_oldschool_seasonal/index_lite.ws",
}

HISCORE_TABLE_ENDPOINTS = {
    "regular": "https://secure.runescape.com/m=hiscore_oldschool/overall",
    "ironman": "https://secure.runescape.com/m=hiscore_oldschool_ironman/overall",
    "hardcore_ironman": "https://secure.runescape.com/m=hiscore_oldschool_hardcore_ironman/overall",
    "ultimate_ironman": "https://secure.runescape.com/m=hiscore_oldschool_ultimate/overall",
    "deadman": "https://secure.runescape.com/m=hiscore_oldschool_deadman/overall",
    "seasonal": "https://secure.runescape.com/m=hiscore_oldschool_seasonal/overall",
}

SKILL_TABLE_IDS = {skill: idx for idx, skill in enumerate(SKILL_NAMES)}

# Stores the latest rank-progress failure detail for UI diagnostics.
_RANK_PROGRESS_LAST_ERROR = ""


def _set_rank_progress_error(message: str) -> None:
    global _RANK_PROGRESS_LAST_ERROR
    _RANK_PROGRESS_LAST_ERROR = message


def _get_rank_progress_error() -> str:
    return _RANK_PROGRESS_LAST_ERROR


class HiscoreTableParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows: list[tuple[int, str, int | None]] = []
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
            if len(self._current_row) < 3:
                return

            rank_idx = None
            for idx, cell in enumerate(self._current_row):
                candidate = cell.replace(",", "").strip()
                if candidate.isdigit():
                    rank_idx = idx
                    break

            if rank_idx is None or rank_idx + 1 >= len(self._current_row):
                return

            rank_text = self._current_row[rank_idx].replace(",", "").strip()
            player_name = self._current_row[rank_idx + 1].strip()
            if rank_text.isdigit() and player_name:
                # Try to capture XP directly from the row to avoid secondary lookups.
                xp_value: int | None = None
                for cell in reversed(self._current_row):
                    numeric = cell.replace(",", "").strip()
                    if numeric.isdigit():
                        n = int(numeric)
                        if n > 200:  # avoids selecting rank/level in most cases
                            xp_value = n
                            break
                self.rows.append((int(rank_text), player_name, xp_value))

    def handle_data(self, data):
        if self._in_td and data:
            self._current_td += data


@lru_cache(maxsize=128)
def _fetch_hiscore_rows(mode: str, skill: str, page: int) -> list[tuple[int, str, int | None]]:
    table_id = SKILL_TABLE_IDS.get(skill)
    if table_id is None:
        return []

    url = HISCORE_TABLE_ENDPOINTS.get(mode, HISCORE_TABLE_ENDPOINTS["regular"])
    try:
        resp = requests.get(
            url,
            params={"table": table_id, "page": page},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=12,
        )
        resp.raise_for_status()
    except requests.RequestException as ex:
        raise RuntimeError(f"hiscore table fetch failed [{mode}/{skill} page {page}] via {url}: {ex}") from ex

    parser = HiscoreTableParser()
    parser.feed(resp.text)
    return parser.rows


@lru_cache(maxsize=512)
def _fetch_player_skill_snapshot(player: str, mode: str, skill: str) -> tuple[int | None, int | None, int | None]:
    skill_idx = SKILL_NAMES.index(skill)
    url = HISCORE_LITE_ENDPOINTS.get(mode, HISCORE_LITE_ENDPOINTS["regular"])
    try:
        resp = requests.get(
            url,
            params={"player": player},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        resp.raise_for_status()
    except requests.RequestException as ex:
        raise RuntimeError(f"hiscore lite fetch failed [{mode}/{skill}] player={player} via {url}: {ex}") from ex

    lines = resp.text.strip().splitlines()
    if skill_idx >= len(lines):
        return None, None, None

    parts = lines[skill_idx].split(",")
    rank = int(parts[0]) if len(parts) > 0 and parts[0].strip().isdigit() and int(parts[0]) != -1 else None
    level = int(parts[1]) if len(parts) > 1 and parts[1].strip().isdigit() and int(parts[1]) != -1 else None
    xp = int(parts[2]) if len(parts) > 2 and parts[2].strip().isdigit() and int(parts[2]) != -1 else None
    return rank, level, xp


def _xp_for_level(level: int) -> int:
    if level <= 1:
        return 0
    points = 0
    for lvl in range(1, level):
        points += int(lvl + 300 * (2 ** (lvl / 7.0)))
    return points // 4


def _level_for_xp(xp: int) -> int:
    if xp <= 0:
        return 1
    level = 1
    for candidate in range(2, 127):
        if _xp_for_level(candidate) <= xp:
            level = candidate
        else:
            break
    return level


def _find_lowest_ranked_player(mode: str, skill: str) -> tuple[int, str, int | None] | None:
    first_page = _fetch_hiscore_rows(mode, skill, 1)
    if not first_page:
        return None

    low = 1
    high = 1
    while True:
        rows = _fetch_hiscore_rows(mode, skill, high)
        if not rows:
            break
        low = high
        high *= 2
        if high > 200_000:
            break

    left = low + 1
    right = high
    last_non_empty = low
    while left <= right:
        mid = (left + right) // 2
        rows = _fetch_hiscore_rows(mode, skill, mid)
        if rows:
            last_non_empty = mid
            left = mid + 1
        else:
            right = mid - 1

    tail_rows = _fetch_hiscore_rows(mode, skill, last_non_empty)
    if not tail_rows:
        return None
    return tail_rows[-1]


def get_rank_progress(skill: str, player: str, mode: str, level: int | None, xp: int | None, rank: int | None) -> dict | None:
    if skill not in SKILL_NAMES:
        _set_rank_progress_error(f"Unsupported skill: {skill}")
        return None

    try:
        if isinstance(rank, int) and rank > 1:
            page = max(1, ((rank - 1) // 25) + 1)
            rows = _fetch_hiscore_rows(mode, skill, page)
            target_name = None
            target_xp = None
            for row_rank, row_name, row_xp in rows:
                if row_rank == rank - 1:
                    target_name = row_name
                    target_xp = row_xp
                    break

            if not target_name and page > 1:
                prev_rows = _fetch_hiscore_rows(mode, skill, page - 1)
                for row_rank, row_name, row_xp in prev_rows:
                    if row_rank == rank - 1:
                        target_name = row_name
                        target_xp = row_xp
                        break

            if not target_name:
                _set_rank_progress_error(
                    f"Could not find rank #{rank - 1} row for {skill} ({mode}); table row parsing returned no target."
                )
                return None

            if target_xp is None:
                _, _, target_xp = _fetch_player_skill_snapshot(target_name, mode, skill)
            if target_xp is None or xp is None:
                _set_rank_progress_error(
                    f"Missing XP values for next-rank calculation ({skill}, {mode})."
                )
                return None

            xp_needed = max(0, target_xp - xp + 1)
            levels_needed = None
            if isinstance(level, int) and level < 99:
                target_level_from_xp = _level_for_xp(target_xp + 1)
                levels_needed = max(0, target_level_from_xp - level)

            _set_rank_progress_error("")
            return {
                "target": "Next Rank",
                "xp_needed": xp_needed,
                "levels_needed": levels_needed,
            }

        if rank in (None, 0):
            cutoff = _find_lowest_ranked_player(mode, skill)
            if not cutoff:
                _set_rank_progress_error(
                    f"Could not find ranked cutoff for {skill} ({mode}); hiscore table returned no rows."
                )
                return None

            _, cutoff_player, cutoff_xp = cutoff
            if cutoff_xp is None:
                _, _, cutoff_xp = _fetch_player_skill_snapshot(cutoff_player, mode, skill)
            if cutoff_xp is None:
                _set_rank_progress_error(
                    f"Could not resolve cutoff XP for {skill} ({mode})."
                )
                return None

            current_xp = xp if isinstance(xp, int) else 0
            xp_needed = max(0, cutoff_xp - current_xp + 1)

            levels_needed = None
            if isinstance(level, int) and level < 99:
                target_level_from_xp = _level_for_xp(cutoff_xp + 1)
                levels_needed = max(0, target_level_from_xp - level)

            _set_rank_progress_error("")
            return {
                "target": "Get Ranked",
                "xp_needed": xp_needed,
                "levels_needed": levels_needed,
            }
    except Exception as ex:
        _set_rank_progress_error(str(ex))
        return None

    _set_rank_progress_error("Rank target unavailable for current state.")
    return None


def build_rank_progress_rows(player: str, mode: str) -> list[dict]:
    rows: list[dict] = []
    latest = get_latest_skills(player, mode)
    if latest.empty:
        return rows

    for skill in SKILL_NAMES:
        skill_row = latest[latest["skill"] == skill]
        if skill_row.empty:
            continue

        level = int(skill_row["level"].iloc[0]) if pd.notna(skill_row["level"].iloc[0]) else None
        xp = int(skill_row["xp"].iloc[0]) if pd.notna(skill_row["xp"].iloc[0]) else None
        rank = int(skill_row["rank"].iloc[0]) if pd.notna(skill_row["rank"].iloc[0]) else None

        progress = get_rank_progress(skill, player, mode, level, xp, rank)
        if not progress:
            continue

        rows.append({
            "skill": skill,
            "target": progress["target"],
            "xp_needed": progress["xp_needed"],
            "levels_needed": progress.get("levels_needed"),
        })

    rows.sort(key=lambda row: row["xp_needed"])
    return rows


def make_xp_to_target_trend(player: str, skill: str, mode: str = "regular") -> go.Figure:
    fig = go.Figure()

    df = get_skill_history(player, skill, mode)
    df_plot = df.dropna(subset=["timestamp", "xp"]).copy()
    if df_plot.empty:
        fig.add_annotation(
            text="No XP history available",
            xref="paper", yref="paper", x=0.5, y=0.5,
            showarrow=False, font=dict(color=TEXT_DIM, size=14)
        )
        _style_fig(fig, f"{skill} — XP to rank target ({player})")
        return fig

    latest = df_plot.iloc[-1]
    latest_level = int(latest["level"]) if pd.notna(latest.get("level")) else None
    latest_xp = int(latest["xp"])
    latest_rank = int(latest["rank"]) if pd.notna(latest.get("rank")) else None

    progress = get_rank_progress(skill, player, mode, latest_level, latest_xp, latest_rank)
    if not progress:
        reason = _get_rank_progress_error() or "Unknown rank-target lookup failure"
        if len(reason) > 180:
            reason = reason[:180] + "..."
        fig.add_annotation(
            text=(
                "Could not resolve live rank target right now"
                f"<br><span style='font-size:11px'>{reason}</span>"
            ),
            xref="paper", yref="paper", x=0.5, y=0.5,
            showarrow=False, font=dict(color=TEXT_DIM, size=14)
        )
        _style_fig(fig, f"{skill} — XP to rank target ({player})")
        return fig

    target_xp = latest_xp + int(progress["xp_needed"])
    df_plot["xp_to_target"] = (target_xp - df_plot["xp"]).clip(lower=0)

    fig.add_trace(go.Scatter(
        x=df_plot["timestamp"],
        y=df_plot["xp_to_target"],
        mode="lines+markers",
        line=dict(color=ACCENT, width=2.5),
        marker=dict(size=5, color=ACCENT),
        name="XP to target",
        hovertemplate="<b>%{x|%d %b %Y %H:%M UTC}</b><br>XP to target: %{y:,.0f}<extra></extra>",
    ))

    fig.add_annotation(
        text=f"Live target: {progress['target']} (estimated)",
        xref="paper", yref="paper", x=0.01, y=0.98,
        showarrow=False, font=dict(color=TEXT_DIM, size=11, family="monospace")
    )

    _style_fig(fig, f"{skill} — XP to rank target ({player})")
    fig.update_yaxes(title="XP")
    return fig


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


ensure_seed_db()
init_db(get_engine())


# ── helpers ──────────────────────────────────────────────────────────────────

def get_conn():
    return get_engine().connect()


def get_players() -> list[dict]:
    """Returns list of dicts with player name, mode, and a display label."""
    mode_labels = {
        "hardcore_ironman": "HCIM",
    }
    dead_hcim_players = load_dead_hcim_players()

    try:
        with get_conn() as conn:
            rows = conn.execute(text(
                "SELECT DISTINCT player, mode FROM snapshots ORDER BY player, mode"
            )).fetchall()
        results = []
        seen_values: set[str] = set()
        for player, mode in rows:
            raw_player = (player or "").strip()
            normalized_player = raw_player.lower()
            mode = (mode or "regular").strip().lower() or "regular"
            mode_display = mode_labels.get(mode, mode.replace("_", " "))
            display_player = raw_player or normalized_player
            value = f"{normalized_player}|{mode}"

            if not normalized_player or value in seen_values:
                continue

            if mode == ANCHOR_MODE and normalized_player in dead_hcim_players:
                continue

            seen_values.add(value)
            label = f"{display_player} ({mode_display})" if mode != "regular" else display_player
            results.append({"player": normalized_player, "mode": mode, "label": label, "value": value})
        return results
    except Exception:
        return []


def parse_player_value(player_value: str | None) -> tuple[str, str]:
    if not player_value:
        return "", "regular"

    player, mode = (player_value.split("|", 1) + ["regular"])[:2]
    normalized_player = player.strip().lower()
    normalized_mode = (mode or "regular").strip().lower() or "regular"
    return normalized_player, normalized_mode


def choose_player_value(players: list[dict], current_value: str | None = None) -> str | None:
    if not players:
        return None

    available_values = {p["value"] for p in players}
    if current_value in available_values:
        return current_value

    exact_default = f"{DEFAULT_PLAYER}|regular"
    if exact_default in available_values:
        return exact_default

    for player in players:
        if player["player"].lower() == DEFAULT_PLAYER:
            return player["value"]

    return players[0]["value"]


def get_skill_history(player: str, skill: str, mode: str = "regular") -> pd.DataFrame:
    with get_conn() as conn:
        df = pd.read_sql_query(text("""
        SELECT s.timestamp, sd.rank, sd.level, sd.xp
        FROM skill_data sd
        JOIN snapshots s ON s.id = sd.snapshot_id
        WHERE s.player = :player AND s.mode = :mode AND sd.skill = :skill
        ORDER BY s.timestamp
    """), conn, params={"player": player.lower(), "mode": mode, "skill": skill})
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
    for col in ["rank", "level", "xp"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["timestamp"])
    df = df.drop_duplicates(subset=["timestamp"], keep="last").reset_index(drop=True)
    return df


def get_latest_skills(player: str, mode: str = "regular") -> pd.DataFrame:
    with get_conn() as conn:
        df = pd.read_sql_query(text("""
        SELECT sd.skill, sd.rank, sd.level, sd.xp
        FROM skill_data sd
        JOIN snapshots s ON s.id = sd.snapshot_id
        WHERE s.player = :player AND s.mode = :mode
          AND s.id = (
              SELECT id FROM snapshots
              WHERE player = :player AND mode = :mode
              ORDER BY timestamp DESC LIMIT 1
          )
    """), conn, params={"player": player.lower(), "mode": mode})
    return df


def get_snapshot_count(player: str, mode: str = "regular") -> int:
    with get_conn() as conn:
        n = conn.execute(text(
            "SELECT COUNT(*) FROM snapshots WHERE player = :player AND mode = :mode"
        ), {"player": player.lower(), "mode": mode}).scalar_one()
    return int(n)


def _parse_quest_export_entry(entry: dict) -> dict | None:
    if not isinstance(entry, dict):
        return None

    entry_player = str(entry.get("player", "")).strip().lower()
    if not entry_player:
        return None

    entry_mode = str(entry.get("mode", "regular")).strip().lower() or "regular"

    def _to_int(value):
        try:
            parsed = int(value)
            return parsed if parsed >= 0 else None
        except (TypeError, ValueError):
            return None

    completed = _to_int(entry.get("completed"))
    in_progress = _to_int(entry.get("in_progress", entry.get("started")))
    not_started = _to_int(entry.get("not_started"))

    if completed is None and in_progress is None and not_started is None:
        return None

    source = str(entry.get("source", "runelite_export")).strip() or "runelite_export"
    return {
        "player": entry_player,
        "mode": entry_mode,
        "completed": completed,
        "in_progress": in_progress,
        "not_started": not_started,
        "source": source,
    }


def get_latest_quest_summary_from_export(player: str, mode: str = "regular") -> dict | None:
    if not os.path.exists(QUEST_EXPORT_FILE):
        return None

    try:
        with open(QUEST_EXPORT_FILE, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except Exception:
        return None

    entries: list[dict] = []
    if isinstance(payload, list):
        entries = payload
    elif isinstance(payload, dict):
        if isinstance(payload.get("players"), list):
            entries = payload["players"]
        else:
            entries = [payload]

    player_key = player.lower()
    mode_key = mode.lower() or "regular"
    for entry in entries:
        parsed = _parse_quest_export_entry(entry)
        if not parsed:
            continue
        if parsed["player"] == player_key and parsed["mode"] == mode_key:
            return {
                "completed": parsed["completed"],
                "in_progress": parsed["in_progress"],
                "not_started": parsed["not_started"],
                "source": parsed["source"],
            }

    return None


def get_latest_quest_summary(player: str, mode: str = "regular") -> dict | None:
    try:
        with get_conn() as conn:
            row = conn.execute(text("""
                SELECT qs.completed, qs.in_progress, qs.not_started, qs.source
                FROM quest_summary qs
                JOIN snapshots s ON s.id = qs.snapshot_id
                WHERE s.player = :player AND s.mode = :mode
                ORDER BY s.timestamp DESC
                LIMIT 1
            """), {"player": player.lower(), "mode": mode}).fetchone()

        if row:
            return {
                "completed": int(row[0]) if row[0] is not None else None,
                "in_progress": int(row[1]) if row[1] is not None else None,
                "not_started": int(row[2]) if row[2] is not None else None,
                "source": str(row[3]) if row[3] is not None else "",
            }
    except Exception:
        pass

    return get_latest_quest_summary_from_export(player, mode)


def get_first_last_dates(player: str, mode: str = "regular") -> tuple[str | None, str | None]:
    with get_conn() as conn:
        row = conn.execute(text(
            "SELECT MIN(date), MAX(date) FROM snapshots WHERE player = :player AND mode = :mode"
        ), {"player": player.lower(), "mode": mode}).fetchone()
    if not row:
        return None, None
    return row[0], row[1]


def get_7d_avg_daily_overall_xp_gain(player: str, mode: str = "regular") -> int | None:
    """Returns 7-day average daily XP gain for Overall, based on latest available data."""
    hist = get_skill_history(player, "Overall", mode)
    if hist.empty or len(hist) < 2:
        return None

    df = hist.dropna(subset=["xp", "timestamp"]).copy()
    if df.empty or len(df) < 2:
        return None

    latest_ts = df["timestamp"].iloc[-1]
    latest_xp = int(df["xp"].iloc[-1])
    window_start = latest_ts - pd.Timedelta(days=7)

    # Prefer the last point at or before the 7-day window to capture gain across the full span.
    before_or_at = df[df["timestamp"] <= window_start]
    if not before_or_at.empty:
        base_row = before_or_at.iloc[-1]
    else:
        within_window = df[df["timestamp"] >= window_start]
        if within_window.empty:
            return None
        base_row = within_window.iloc[0]

    base_ts = base_row["timestamp"]
    base_xp = int(base_row["xp"])

    elapsed_days = (latest_ts - base_ts).total_seconds() / 86400
    if elapsed_days <= 0:
        return None

    gained_xp = latest_xp - base_xp
    if gained_xp < 0:
        return None

    return int(round(gained_xp / elapsed_days))


# ── chart builders ────────────────────────────────────────────────────────────

def make_xp_trend(player: str, skill: str, mode: str = "regular") -> go.Figure:
    df = get_skill_history(player, skill, mode)
    df_plot = df.dropna(subset=["xp"]).copy()
    color = SKILL_COLORS.get(skill, ACCENT)

    fig = go.Figure()

    if df_plot.empty or len(df_plot) < 2:
        fig.add_annotation(
            text="Not enough data yet — run collector.py daily to build history",
            xref="paper", yref="paper", x=0.5, y=0.5,
            showarrow=False, font=dict(color=TEXT_DIM, size=14)
        )
    else:
        first_ts = df_plot["timestamp"].iloc[0]
        last_ts = df_plot["timestamp"].iloc[-1]
        first_xp = float(df_plot["xp"].iloc[0])
        max_xp = float(df_plot["xp"].max())

        # XP area
        fig.add_trace(go.Scatter(
            x=df_plot["timestamp"], y=df_plot["xp"],
            mode="lines+markers",
            name="XP",
            line=dict(color=color, width=2.5),
            marker=dict(size=6, color=color),
            hovertemplate="<b>%{x|%d %b %Y %H:%M UTC}</b><br>XP: %{y:,.0f}<extra></extra>"
        ))

        fig.update_xaxes(range=[first_ts, last_ts])
        if max_xp > first_xp:
            fig.update_yaxes(range=[first_xp, max_xp])

    _style_fig(fig, f"{skill} — XP over time ({player})")
    return fig


def make_rank_trend(player: str, skill: str, mode: str = "regular") -> go.Figure:
    df = get_skill_history(player, skill, mode)
    df_plot = df.dropna(subset=["rank"]).copy()
    color = SKILL_COLORS.get(skill, ACCENT)

    fig = go.Figure()

    if df_plot.empty or len(df_plot) < 2:
        fig.add_annotation(
            text="Not enough data yet — run collector.py daily to build history",
            xref="paper", yref="paper", x=0.5, y=0.5,
            showarrow=False, font=dict(color=TEXT_DIM, size=14)
        )
    else:
        first_ts = df_plot["timestamp"].iloc[0]
        last_ts = df_plot["timestamp"].iloc[-1]
        first_rank = float(df_plot["rank"].iloc[0])
        min_rank = float(df_plot["rank"].min())

        fig.add_trace(go.Scatter(
            x=df_plot["timestamp"], y=df_plot["rank"],
            mode="lines+markers",
            name="Rank",
            line=dict(color=ACCENT, width=2.5),
            marker=dict(size=6, color=ACCENT),
            hovertemplate="<b>%{x|%d %b %Y %H:%M UTC}</b><br>Rank: #%{y:,.0f}<extra></extra>"
        ))

        fig.update_xaxes(range=[first_ts, last_ts])
        if first_rank > min_rank:
            fig.update_yaxes(range=[first_rank, min_rank], autorange="reversed")

    _style_fig(fig, f"{skill} — Rank over time ({player})")
    # Invert y-axis: lower rank number = better
    fig.update_yaxes(autorange="reversed")
    return fig


def make_avg_daily_xp_trend(player: str, mode: str = "regular") -> go.Figure:
    """Shows trend of average Overall XP earned per day between snapshots."""
    df = get_skill_history(player, "Overall", mode)
    df_plot = df.dropna(subset=["xp", "timestamp"]).copy()

    fig = go.Figure()

    if df_plot.empty or len(df_plot) < 2:
        fig.add_annotation(
            text="Not enough data yet — run collector.py daily to build history",
            xref="paper", yref="paper", x=0.5, y=0.5,
            showarrow=False, font=dict(color=TEXT_DIM, size=14)
        )
        _style_fig(fig, f"Overall — Average XP/day trend ({player})")
        return fig

    df_plot = df_plot.sort_values("timestamp").reset_index(drop=True)
    df_plot["prev_timestamp"] = df_plot["timestamp"].shift(1)
    df_plot["delta_days"] = df_plot["timestamp"].diff().dt.total_seconds() / 86400
    df_plot["delta_xp"] = df_plot["xp"].diff()

    # Only keep valid forward progress intervals to avoid noisy/invalid rates.
    df_plot = df_plot[(df_plot["delta_days"] > 0) & (df_plot["delta_xp"] >= 0)].copy()
    if df_plot.empty:
        fig.add_annotation(
            text="Not enough valid intervals to calculate XP/day",
            xref="paper", yref="paper", x=0.5, y=0.5,
            showarrow=False, font=dict(color=TEXT_DIM, size=14)
        )
        _style_fig(fig, f"Overall — Average XP/day trend ({player})")
        return fig

    df_plot["xp_per_day"] = df_plot["delta_xp"] / df_plot["delta_days"]
    # Smooth short-term spikes so the trend is easier to read.
    df_plot["xp_per_day_ma"] = df_plot["xp_per_day"].rolling(window=3, min_periods=1).mean()

    fig.add_trace(go.Scatter(
        x=df_plot["timestamp"],
        y=df_plot["xp_per_day"],
        mode="lines+markers",
        name="Interval Avg XP/day",
        line=dict(color="#5f86ff", width=1.8),
        marker=dict(size=5, color="#5f86ff"),
        customdata=df_plot[["prev_timestamp"]].to_numpy(),
        hovertemplate=(
            "<b>%{customdata[0]|%d %b %Y %H:%M UTC} → %{x|%d %b %Y %H:%M UTC}</b>"
            "<br>Avg XP/day: %{y:,.0f}<extra></extra>"
        )
    ))

    fig.add_trace(go.Scatter(
        x=df_plot["timestamp"],
        y=df_plot["xp_per_day_ma"],
        mode="lines",
        name="3-point moving average",
        line=dict(color=ACCENT, width=2.8),
        hovertemplate="<b>%{x|%d %b %Y %H:%M UTC}</b><br>Smoothed XP/day: %{y:,.0f}<extra></extra>"
    ))

    _style_fig(fig, f"Overall — Average XP/day trend ({player})")
    fig.update_yaxes(title="XP/day")
    return fig


def make_skills_overview(player: str, mode: str = "regular") -> go.Figure:
    df = get_latest_skills(player, mode)
    if df.empty:
        fig = go.Figure()
        fig.add_annotation(
            text="No data — run collector.py first",
            xref="paper", yref="paper", x=0.5, y=0.5,
            showarrow=False, font=dict(color=TEXT_DIM, size=16)
        )
        _style_fig(fig, "Skills Overview")
        return fig

    chart_skills = [s for s in SKILL_NAMES if s != "Overall"]
    skills_df = df[df["skill"].isin(chart_skills)].copy()
    skills_df["skill"] = pd.Categorical(skills_df["skill"], categories=chart_skills, ordered=True)
    skills_df = skills_df.sort_values("skill")
    skills_df["color"] = skills_df["skill"].map(SKILL_COLORS).fillna(ACCENT)
    skills_df["level"] = skills_df["level"].fillna(1).astype(int)
    skills_df["pct"] = (skills_df["level"] / 99 * 100).clip(0, 100)

    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=skills_df["skill"],
        y=skills_df["level"],
        marker_color=skills_df["color"].tolist(),
        text=skills_df["level"],
        textposition="outside",
        textfont=dict(color=TEXT, size=11),
        hovertemplate="<b>%{x}</b><br>Level: %{y}<extra></extra>",
        name="Level"
    ))

    _style_fig(fig, f"All Skills — Current Levels ({player})")
    fig.update_yaxes(range=[0, 108])
    fig.update_layout(showlegend=False)
    return fig


def make_xp_distribution(player: str, mode: str = "regular") -> go.Figure:
    df = get_latest_skills(player, mode)
    if df.empty:
        fig = go.Figure()
        _style_fig(fig, "XP Distribution")
        return fig

    skills_df = df[df["skill"].isin(SKILL_NAMES) & (df["skill"] != "Overall")].dropna(subset=["xp"])
    skills_df = skills_df.sort_values("xp", ascending=False).head(12)
    skills_df["color"] = skills_df["skill"].map(SKILL_COLORS).fillna(ACCENT)

    fig = go.Figure(go.Pie(
        labels=skills_df["skill"],
        values=skills_df["xp"],
        marker=dict(colors=skills_df["color"].tolist(),
                    line=dict(color=BG, width=2)),
        textinfo="label+percent",
        textfont=dict(color=TEXT, size=12),
        hole=0.45,
        hovertemplate="<b>%{label}</b><br>XP: %{value:,.0f}<br>%{percent}<extra></extra>"
    ))
    _style_fig(fig, f"XP Distribution — Top 12 Skills ({player})")
    fig.update_layout(showlegend=False)
    return fig


def get_fixed_compare_players() -> list[dict]:
    players = get_players()
    resolved: list[dict] = []
    used_values: set[str] = set()

    for target_name in FALLBACK_COMPARE_PLAYER_NAMES:
        target = target_name.lower()
        hcim_match = next(
            (
                p for p in players
                if p["mode"] == ANCHOR_MODE and (
                    p["label"].lower() == target
                    or p["player"].lower() == target
                    or p["label"].lower().startswith(f"{target} (")
                )
            ),
            None,
        )
        match = hcim_match
        if match and match["value"] not in used_values:
            used_values.add(match["value"])
            resolved.append({"name": target_name, "value": match["value"]})

    return resolved


def get_latest_skill_ranks(skill: str) -> list[dict]:
    if skill not in SKILL_NAMES:
        return []

    try:
        with get_conn() as conn:
            df = pd.read_sql_query(text("""
                WITH latest AS (
                    SELECT player, mode, MAX(timestamp) AS max_ts
                    FROM snapshots
                    GROUP BY player, mode
                )
                SELECT s.player, s.mode, sd.rank
                FROM snapshots s
                JOIN latest l
                  ON l.player = s.player
                 AND l.mode = s.mode
                 AND l.max_ts = s.timestamp
                JOIN skill_data sd
                  ON sd.snapshot_id = s.id
                                WHERE sd.skill = :skill AND sd.rank IS NOT NULL
                        """), conn, params={"skill": skill})

        if df.empty:
            return []

        df["player"] = df["player"].astype(str).str.strip()
        df["mode"] = df["mode"].fillna("regular").astype(str).str.strip().str.lower()
        df["rank"] = pd.to_numeric(df["rank"], errors="coerce")
        df = df.dropna(subset=["rank"])
        dead_hcim_players = load_dead_hcim_players()
        if dead_hcim_players:
            df = df[
                ~(
                    (df["mode"] == ANCHOR_MODE)
                    & (df["player"].str.lower().isin(dead_hcim_players))
                )
            ]

        if df.empty:
            return []

        df["rank"] = df["rank"].astype(int)
        df = df.sort_values("rank").drop_duplicates(subset=["player", "mode"], keep="first")
        return df.to_dict("records")
    except Exception:
        return []


def _to_compare_payload(rows: list[dict]) -> list[dict]:
    payload: list[dict] = []
    seen_values: set[str] = set()

    for row in rows:
        player = str(row["player"]).strip().lower()
        mode = str(row["mode"]).strip().lower() or "regular"
        if not player:
            continue

        value = f"{player}|{mode}"
        if value in seen_values:
            continue

        seen_values.add(value)
        payload.append({"name": player, "value": value})

    return payload


def get_anchor_group(skill: str, ahead_count: int, behind_count: int) -> list[dict]:
    """Returns N ahead + anchor + M behind for the anchor mode and skill."""
    rows = get_latest_skill_ranks(skill)
    if not rows:
        fallback = get_fixed_compare_players()
        return fallback

    anchor_candidates = [row for row in rows if str(row["player"]).lower() == ANCHOR_PLAYER.lower()]
    if not anchor_candidates:
        fallback = get_fixed_compare_players()
        return fallback

    anchor = next((row for row in anchor_candidates if row["mode"] == ANCHOR_MODE), anchor_candidates[0])
    anchor_mode = anchor["mode"]
    anchor_rank = int(anchor["rank"])

    same_mode = [row for row in rows if row["mode"] == anchor_mode]
    ahead = sorted(
        (row for row in same_mode if int(row["rank"]) < anchor_rank),
        key=lambda row: int(row["rank"]),
        reverse=True,
    )
    behind = sorted(
        (row for row in same_mode if int(row["rank"]) > anchor_rank),
        key=lambda row: int(row["rank"]),
    )

    group_rows = sorted(
        ahead[:ahead_count] + [anchor] + behind[:behind_count],
        key=lambda row: int(row["rank"]),
    )

    group_players = _to_compare_payload(group_rows)

    # If dynamic set is too small, use historical fixed players fallback.
    if len(group_players) < 3:
        return get_fixed_compare_players()

    return group_players


def get_latest_skill_xp_for_player(player: str, mode: str, skill: str) -> int | None:
    if skill not in SKILL_NAMES:
        return None

    try:
        with get_conn() as conn:
            row = conn.execute(text("""
                SELECT sd.xp
                FROM skill_data sd
                JOIN snapshots s ON s.id = sd.snapshot_id
                WHERE s.player = :player
                  AND s.mode = :mode
                  AND sd.skill = :skill
                  AND sd.xp IS NOT NULL
                ORDER BY s.timestamp DESC
                LIMIT 1
            """), {"player": player.lower(), "mode": mode, "skill": skill}).fetchone()
    except Exception:
        return None

    if not row or row[0] is None:
        return None

    try:
        return int(row[0])
    except (TypeError, ValueError):
        return None


def filter_compare_outliers(skill: str, compare_players: list[dict]) -> list[dict]:
    if not compare_players:
        return compare_players

    threshold = OVERALL_OUTLIER_XP_LIMIT if skill == "Overall" else SKILL_OUTLIER_XP_LIMIT
    anchor_entry = next(
        (
            p for p in compare_players
            if parse_player_value(p.get("value", ""))[0].lower() == ANCHOR_PLAYER.lower()
            and parse_player_value(p.get("value", ""))[1] == ANCHOR_MODE
        ),
        None,
    )

    if not anchor_entry:
        return compare_players

    anchor_player, anchor_mode = parse_player_value(anchor_entry["value"])
    anchor_xp = get_latest_skill_xp_for_player(anchor_player, anchor_mode, skill)
    if anchor_xp is None:
        return compare_players

    filtered: list[dict] = []
    for player_cfg in compare_players:
        player, mode = parse_player_value(player_cfg.get("value", ""))
        if player.lower() == ANCHOR_PLAYER.lower() and mode == ANCHOR_MODE:
            filtered.append(player_cfg)
            continue

        xp = get_latest_skill_xp_for_player(player, mode, skill)
        if xp is None:
            filtered.append(player_cfg)
            continue

        if abs(xp - anchor_xp) <= threshold:
            filtered.append(player_cfg)

    return filtered or [anchor_entry]


def has_recent_skill_xp_movement(player: str, mode: str, skill: str, days: int = INACTIVE_DAYS_LIMIT) -> bool:
    if skill not in SKILL_NAMES:
        return False

    cutoff_dt = datetime.now(timezone.utc) - timedelta(days=days)
    cutoff_iso = cutoff_dt.isoformat()

    try:
        with get_conn() as conn:
            rows = conn.execute(text("""
                SELECT sd.xp
                FROM skill_data sd
                JOIN snapshots s ON s.id = sd.snapshot_id
                WHERE s.player = :player
                  AND s.mode = :mode
                  AND sd.skill = :skill
                  AND sd.xp IS NOT NULL
                  AND s.timestamp >= :cutoff
                ORDER BY s.timestamp
            """), {
                "player": player.lower(),
                "mode": mode,
                "skill": skill,
                "cutoff": cutoff_iso,
            }).fetchall()
    except Exception:
        return True

    xp_values: list[int] = []
    for row in rows:
        try:
            xp_values.append(int(row[0]))
        except (TypeError, ValueError):
            continue

    if len(xp_values) < 2:
        return False

    return min(xp_values) != max(xp_values)


def filter_inactive_compare_players(skill: str, compare_players: list[dict]) -> list[dict]:
    if not compare_players:
        return compare_players

    filtered: list[dict] = []
    for player_cfg in compare_players:
        player, mode = parse_player_value(player_cfg.get("value", ""))
        if not player:
            continue
        if has_recent_skill_xp_movement(player, mode, skill):
            filtered.append(player_cfg)

    return filtered


def make_multi_player_xp_trend(skill: str, fixed_players: list[dict]) -> go.Figure:
    fig = go.Figure()

    if not skill or not fixed_players:
        fig.add_annotation(
            text="No matching fixed players found in the dataset",
            xref="paper", yref="paper", x=0.5, y=0.5,
            showarrow=False, font=dict(color=TEXT_DIM, size=14)
        )
        _style_fig(fig, "XP over time — Group comparison")
        return fig

    global_min_time = None
    global_max_time = None
    global_min_xp = None
    global_max_xp = None

    for player_cfg in fixed_players:
        player_value = player_cfg["value"]
        player_label = player_cfg["name"]
        player, mode = (player_value.split("|") + ["regular"])[:2]
        df = get_skill_history(player, skill, mode)
        if df.empty:
            continue

        first_ts = df["timestamp"].iloc[0]
        last_ts = df["timestamp"].iloc[-1]
        first_xp = int(df["xp"].iloc[0])
        last_xp = int(df["xp"].iloc[-1])
        gained_xp = last_xp - first_xp

        color = SKILL_COLORS.get(skill, ACCENT)

        fig.add_trace(go.Scatter(
            x=df["timestamp"], y=df["xp"],
            mode="lines+markers",
            name=player_label,
            line=dict(width=2),
            marker=dict(size=5),
            hovertemplate=(
                "<b>%{x|%d %b %Y %H:%M UTC}</b>"
                "<br>Player: " + player_label +
                "<br>XP: %{y:,.0f}<extra></extra>"
            )
        ))

        fig.add_annotation(
            x=last_ts,
            y=last_xp,
            text=f"+{gained_xp:,}",
            showarrow=False,
            xshift=12,
            font=dict(color=color, size=10, family="monospace"),
            bgcolor="rgba(20,20,24,0.7)"
        )

        global_min_time = first_ts if global_min_time is None else min(global_min_time, first_ts)
        global_max_time = last_ts if global_max_time is None else max(global_max_time, last_ts)
        global_min_xp = first_xp if global_min_xp is None else min(global_min_xp, first_xp)
        global_max_xp = last_xp if global_max_xp is None else max(global_max_xp, last_xp)

    if not fig.data:
        fig.add_annotation(
            text="No matching history found for selected players",
            xref="paper", yref="paper", x=0.5, y=0.5,
            showarrow=False, font=dict(color=TEXT_DIM, size=14)
        )
    else:
        if global_min_time is not None and global_max_time is not None:
            time_span = global_max_time - global_min_time
            time_pad = time_span * 0.08 if time_span > pd.Timedelta(0) else pd.Timedelta(hours=6)
            fig.update_xaxes(range=[global_min_time, global_max_time + time_pad])
        if global_min_xp is not None and global_max_xp is not None and global_max_xp > global_min_xp:
            pad = max(int((global_max_xp - global_min_xp) * 0.05), 1)
            fig.update_yaxes(range=[global_min_xp - pad, global_max_xp + pad])

    _style_fig(fig, f"XP over time for selected players ({skill})")
    fig.update_layout(
        margin=dict(l=50, r=90, t=65, b=95),
        legend=dict(orientation="h", yanchor="top", y=-0.16, xanchor="left", x=0),
    )
    return fig


# ── layout helpers ────────────────────────────────────────────────────────────

def _hex_to_rgb(hex_color: str) -> str:
    hex_color = hex_color.lstrip("#")
    r, g, b = int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16)
    return f"{r},{g},{b}"


def _style_fig(fig: go.Figure, title: str):
    fig.update_layout(
        title=dict(text=title, font=dict(color=TEXT, size=16, family="Georgia, serif"), x=0.02),
        paper_bgcolor=CARD_BG,
        plot_bgcolor=CARD_BG,
        font=dict(color=TEXT, family="Georgia, serif"),
        margin=dict(l=50, r=30, t=55, b=45),
        xaxis=dict(
            gridcolor=BORDER, zeroline=False,
            tickfont=dict(color=TEXT_DIM, size=11)
        ),
        yaxis=dict(
            gridcolor=BORDER, zeroline=False,
            tickfont=dict(color=TEXT_DIM, size=11)
        ),
        legend=dict(
            bgcolor="rgba(0,0,0,0)", font=dict(color=TEXT_DIM, size=12)
        ),
        hoverlabel=dict(
            bgcolor="#1e1e28", font_color=TEXT,
            bordercolor=BORDER
        )
    )


def stat_card(
    label: str,
    value: str,
    delta: str = "",
    delta_positive: bool = True,
    tooltip: str | None = None,
):
    delta_color = GREEN if delta_positive else RED
    label_content = (
        html.Abbr(label, title=tooltip, style={"textDecoration": "underline dotted", "cursor": "help"})
        if tooltip
        else label
    )
    return html.Div([
        html.Div(label_content, style={"color": TEXT_DIM, "fontSize": "11px",
                                       "textTransform": "uppercase", "letterSpacing": "1.5px",
                                       "marginBottom": "6px", "fontFamily": "Georgia, serif"}),
        html.Div(value, style={"color": ACCENT, "fontSize": "26px",
                               "fontWeight": "bold", "fontFamily": "Georgia, serif",
                               "lineHeight": "1"}),
        html.Div(delta, style={"color": delta_color, "fontSize": "12px",
                               "marginTop": "4px", "fontFamily": "monospace"}) if delta else html.Div()
    ], style={
        "background": CARD_BG,
        "border": f"1px solid {BORDER}",
        "borderRadius": "8px",
        "padding": "18px 22px",
        "minWidth": "140px",
        "flex": "1 1 180px"
    })


# ── app ───────────────────────────────────────────────────────────────────────

app = dash.Dash(
    __name__,
    title="OSRS Hiscore Dashboard",
    suppress_callback_exceptions=True
)


def main_page_layout():
    players = get_players()
    default_player_value = choose_player_value(players)
    return html.Div([

    # Header
    html.Div([
        html.Div([
            html.Span("⚔", style={"fontSize": "28px", "marginRight": "12px"}),
            html.Span("OSRS Hiscore Dashboard",
                      style={"fontSize": "22px", "fontWeight": "bold",
                             "color": ACCENT, "fontFamily": "Georgia, serif",
                             "letterSpacing": "1px"}),
        ], className="top-header-left", style={"display": "flex", "alignItems": "center"}),
        html.Div([
            dcc.Link("XP Compare Page", href="/xp-compare", style={
                "color": ACCENT,
                "fontSize": "12px",
                "fontFamily": "monospace",
                "textDecoration": "none",
                "marginRight": "14px"
            }),
            html.Span(id="current-player-label", style={
                "color": TEXT_DIM,
                "fontSize": "12px",
                "fontFamily": "monospace",
                "marginRight": "14px"
            }),
            html.Span(id="last-updated", style={"color": TEXT_DIM, "fontSize": "12px",
                                                 "fontFamily": "monospace"})
        ], className="top-header-right", style={"display": "flex", "alignItems": "center"})
    ], className="top-header", style={
        "display": "flex", "justifyContent": "space-between", "alignItems": "center",
        "padding": "18px 32px", "borderBottom": f"1px solid {BORDER}",
        "background": CARD_BG
    }),

    # Controls bar
    html.Div([
        html.Div([
            html.Label("Player", style={"color": TEXT_DIM, "fontSize": "11px",
                                        "textTransform": "uppercase",
                                        "letterSpacing": "1px", "marginBottom": "6px",
                                        "fontFamily": "Georgia, serif"}),
            dcc.Dropdown(
                id="player-dropdown",
                options=[{"label": p["label"], "value": p["value"]} for p in players],
                value=default_player_value,
                clearable=False,
                className="responsive-dropdown",
                style={"width": "260px", "fontFamily": "Georgia, serif"},
            )
        ], className="control-group"),
        html.Div([
            html.Label("Skill", style={"color": TEXT_DIM, "fontSize": "11px",
                                       "textTransform": "uppercase",
                                       "letterSpacing": "1px", "marginBottom": "6px",
                                       "fontFamily": "Georgia, serif"}),
            dcc.Dropdown(
                id="skill-dropdown",
                options=[{"label": s, "value": s} for s in SKILL_NAMES],
                value="Overall",
                clearable=False,
                className="responsive-dropdown",
                style={"width": "200px", "fontFamily": "Georgia, serif"},
            )
        ], className="control-group"),
    ], className="controls-bar", style={
        "display": "flex", "gap": "32px", "alignItems": "flex-end",
        "padding": "20px 32px", "borderBottom": f"1px solid {BORDER}",
        "background": "#10101a"
    }),

    # Stat cards row
    html.Div(id="stat-cards", style={
        "display": "flex", "gap": "16px",
        "flexWrap": "wrap",
        "padding": "20px 32px"
    }),

    # Per-skill rank progress table
    html.Div(id="rank-target-table", style={
        "padding": "0 32px 16px 32px"
    }),

    # Charts grid
    html.Div([
        # Top row: XP trend + Rank trend
        html.Div([
            dcc.Graph(id="xp-trend-chart",
                      config={"displayModeBar": False},
                      className="chart-card",
                      style={"flex": "1", "minWidth": "0", "height": "360px"}),
            dcc.Graph(id="rank-trend-chart",
                      config={"displayModeBar": False},
                      className="chart-card",
                      style={"flex": "1", "minWidth": "0", "height": "360px"}),
        ], className="chart-row", style={"display": "flex", "gap": "16px", "marginBottom": "16px"}),

        # Bottom row: skills bar + pie
        html.Div([
            dcc.Graph(id="skills-overview-chart",
                      config={"displayModeBar": False},
                      className="chart-card",
                      style={"flex": "2", "minWidth": "0", "height": "360px"}),
            dcc.Graph(id="xp-distribution-chart",
                      config={"displayModeBar": False},
                      className="chart-card",
                      style={"flex": "1", "minWidth": "0", "height": "360px"}),
        ], className="chart-row", style={"display": "flex", "gap": "16px"}),

        # Third row: average XP/day trend
        html.Div([
            dcc.Graph(id="avg-xp-day-chart",
                      config={"displayModeBar": False},
                      className="chart-card",
                      style={"flex": "1", "minWidth": "0", "height": "340px"}),
            dcc.Graph(id="xp-to-target-chart",
                      config={"displayModeBar": False},
                      className="chart-card",
                      style={"flex": "1", "minWidth": "0", "height": "340px"}),
        ], className="chart-row", style={"display": "flex", "gap": "16px", "marginTop": "16px"}),

    ], className="charts-grid", style={"padding": "0 32px 32px 32px"}),

    # Footer hint
    html.Div(
        "Run  python collector.py  daily (or via Task Scheduler) to add snapshots and build trend history.",
        style={"color": TEXT_DIM, "fontSize": "12px", "fontFamily": "monospace",
               "textAlign": "center", "padding": "0 0 20px", "borderTop": f"1px solid {BORDER}",
               "paddingTop": "14px"}
    )

], style={"background": BG, "minHeight": "100vh", "color": TEXT})


def compare_page_layout():
    return html.Div([
        html.Div([
            html.Div([
                html.Span("⚔", style={"fontSize": "28px", "marginRight": "12px"}),
                html.Span("XP Compare", style={
                    "fontSize": "22px", "fontWeight": "bold",
                    "color": ACCENT, "fontFamily": "Georgia, serif", "letterSpacing": "1px"
                }),
            ], className="top-header-left", style={"display": "flex", "alignItems": "center"}),
            dcc.Link("← Back to Dashboard", href="/", style={
                "color": ACCENT,
                "fontSize": "12px",
                "fontFamily": "monospace",
                "textDecoration": "none"
            })
        ], className="top-header", style={
            "display": "flex", "justifyContent": "space-between", "alignItems": "center",
            "padding": "18px 32px", "borderBottom": f"1px solid {BORDER}", "background": CARD_BG
        }),

        html.Div([
            html.Div([
                html.Label("Skill", style={"color": TEXT_DIM, "fontSize": "11px",
                                           "textTransform": "uppercase", "letterSpacing": "1px",
                                           "marginBottom": "6px", "fontFamily": "Georgia, serif"}),
                dcc.Dropdown(
                    id="compare-skill-dropdown",
                    options=[{"label": s, "value": s} for s in SKILL_NAMES],
                    value="Sailing" if "Sailing" in SKILL_NAMES else "Overall",
                    clearable=False,
                    className="responsive-dropdown",
                    style={"width": "220px", "fontFamily": "Georgia, serif"},
                )
            ], className="control-group"),
            html.Div(
                "Stored tracking (collector): 10 ahead + XESPIS + 3 behind for Overall and each skill. Display: 3 ahead + XESPIS + 3 behind per skill, excluding outliers (>5M Overall, >1M skill XP from XESPIS) and no-movement players (30 days).",
                className="compare-notes",
                style={"color": TEXT_DIM, "fontSize": "12px", "fontFamily": "monospace", "maxWidth": "640px"}
            ),
        ], className="controls-bar", style={
            "display": "flex", "gap": "32px", "alignItems": "flex-end",
            "padding": "20px 32px", "borderBottom": f"1px solid {BORDER}", "background": "#10101a"
        }),

        html.Div([
            dcc.Graph(
                id="xp-compare-chart",
                config={"displayModeBar": False},
                style={"height": "540px"}
            )
        ], style={"padding": "20px 32px 8px 32px"}),

        html.Div([
            dcc.Graph(
                id="xp-overall-compare-chart",
                config={"displayModeBar": False},
                style={"height": "540px"}
            )
        ], style={"padding": "20px 32px 32px 32px"})
    ], style={"background": BG, "minHeight": "100vh", "color": TEXT})


app.layout = html.Div([
    dcc.Location(id="url", refresh=False),
    html.Div(id="page-content")
])


# ── callbacks ─────────────────────────────────────────────────────────────────

@app.callback(
    Output("page-content", "children"),
    Input("url", "pathname")
)
def render_page(pathname):
    if pathname == "/xp-compare":
        return compare_page_layout()
    return main_page_layout()

@app.callback(
    Output("player-dropdown", "options"),
    Output("player-dropdown", "value"),
    Input("player-dropdown", "id"),
    State("player-dropdown", "value")
)
def refresh_players(_, current_value):
    players = get_players()
    options = [{"label": p["label"], "value": p["value"]} for p in players]
    value = choose_player_value(players, current_value)
    return options, value


@app.callback(
    Output("skill-dropdown", "value"),
    Input("skills-overview-chart", "clickData"),
    Input("xp-distribution-chart", "clickData"),
    Input("skill-dropdown", "value"),
)
def update_skill_from_chart(bar_click, pie_click, current_skill):
    ctx = dash.callback_context
    if not ctx.triggered:
        return current_skill

    trigger_id = ctx.triggered[0]["prop_id"].split(".")[0]

    if trigger_id == "skills-overview-chart" and bar_click:
        clicked = bar_click["points"][0].get("x")
        if clicked in SKILL_NAMES:
            return clicked

    if trigger_id == "xp-distribution-chart" and pie_click:
        clicked = pie_click["points"][0].get("label")
        if clicked in SKILL_NAMES:
            return clicked

    return current_skill


@app.callback(
    Output("stat-cards", "children"),
    Output("current-player-label", "children"),
    Output("last-updated", "children"),
    Input("player-dropdown", "value"),
    Input("skill-dropdown", "value"),
)
def update_stat_cards(player_value, skill):
    if not player_value:
        return [], "", ""

    player, mode = parse_player_value(player_value)
    mode_label = mode.replace("_", " ").title()
    player_label = player if mode == "regular" else f"{player} ({mode_label})"
    header_player_text = f"Current player: {player_label}"

    df_latest = get_latest_skills(player, mode)
    n_snaps = get_snapshot_count(player, mode)
    first_date, last_date = get_first_last_dates(player, mode)

    if df_latest.empty:
        return [stat_card("Snapshots", str(n_snaps))], header_player_text, "No data yet"

    row = df_latest[df_latest["skill"] == skill]
    level = int(row["level"].iloc[0]) if not row.empty and pd.notna(row["level"].iloc[0]) else "—"
    xp    = int(row["xp"].iloc[0])    if not row.empty and pd.notna(row["xp"].iloc[0])    else "—"
    rank  = int(row["rank"].iloc[0])  if not row.empty and pd.notna(row["rank"].iloc[0])  else "—"

    hist = get_skill_history(player, skill, mode)
    xp_gained = ""
    if len(hist) >= 2:
        gained = int(hist["xp"].iloc[-1]) - int(hist["xp"].iloc[0])
        xp_gained = f"+{gained:,} XP since {hist['timestamp'].iloc[0].strftime('%d %b %Y %H:%M UTC')}"

    overall_row = df_latest[df_latest["skill"] == "Overall"]
    total_level = int(overall_row["level"].iloc[0]) if not overall_row.empty and pd.notna(overall_row["level"].iloc[0]) else "—"
    total_xp    = int(overall_row["xp"].iloc[0])    if not overall_row.empty and pd.notna(overall_row["xp"].iloc[0])    else "—"
    avg_daily_7d = get_7d_avg_daily_overall_xp_gain(player, mode)
    avg_daily_7d_display = f"{avg_daily_7d:,}" if isinstance(avg_daily_7d, int) else "—"
    rank_progress = get_rank_progress(
        skill,
        player,
        mode,
        level if isinstance(level, int) else None,
        xp if isinstance(xp, int) else None,
        rank if isinstance(rank, int) else None,
    )
    rank_target = rank_progress["target"] if rank_progress else "Rank Progress"
    rank_xp_needed = f"{rank_progress['xp_needed']:,}" if rank_progress else "—"
    rank_level_note = ""
    if rank_progress and isinstance(rank_progress.get("levels_needed"), int):
        lvls = rank_progress["levels_needed"]
        rank_level_note = f"~{lvls} level{'s' if lvls != 1 else ''}"

    quest_summary = get_latest_quest_summary(player, mode)
    quest_completed = "—"
    quest_in_progress = "—"
    quest_not_started = "—"
    quest_tooltip = "No quest summary export linked yet."
    if quest_summary:
        if isinstance(quest_summary["completed"], int):
            quest_completed = f"{quest_summary['completed']:,}"
        if isinstance(quest_summary["in_progress"], int):
            quest_in_progress = f"{quest_summary['in_progress']:,}"
        if isinstance(quest_summary["not_started"], int):
            quest_not_started = f"{quest_summary['not_started']:,}"
        source = quest_summary.get("source") or "quest export"
        quest_tooltip = f"Latest quest state counts from {source}."

    cards = [
        stat_card("Total Level",    f"{total_level:,}" if isinstance(total_level, int) else total_level),
        stat_card("Total XP",       f"{total_xp:,}"    if isinstance(total_xp, int)    else total_xp),
        stat_card(
            "7D Avg XP/Day",
            avg_daily_7d_display,
            tooltip="Overall XP gained over the latest 7-day window divided by elapsed days between snapshots.",
        ),
        stat_card("Quests Completed", quest_completed, tooltip=quest_tooltip),
        stat_card("Quests In Progress", quest_in_progress, tooltip=quest_tooltip),
        stat_card("Quests Not Started", quest_not_started, tooltip=quest_tooltip),
        stat_card(
            "Rank Target",
            rank_target,
            tooltip="For selected skill: next rank if already ranked, or first ranked threshold if currently unranked.",
        ),
        stat_card(
            "XP To Target",
            rank_xp_needed,
            delta=rank_level_note,
            delta_positive=True,
            tooltip="Estimated XP needed for selected rank target; threshold moves as other players gain XP.",
        ),
        stat_card(f"{skill} Level", f"{level}"         if isinstance(level, int)        else level),
        stat_card(f"{skill} XP",    f"{xp:,}"          if isinstance(xp, int)           else xp,
                  delta=xp_gained, delta_positive=True),
        stat_card(f"{skill} Rank",  f"#{rank:,}"       if isinstance(rank, int)         else rank),
        stat_card("Mode",           mode_label),
        stat_card("Snapshots",      str(n_snaps)),
    ]

    last_updated = f"Last updated: {last_date}  |  Tracking since: {first_date}" if last_date else ""
    return cards, header_player_text, last_updated


@app.callback(
    Output("rank-target-table", "children"),
    Input("player-dropdown", "value"),
)
def update_rank_target_table(player_value):
    if not player_value:
        return html.Div()

    player, mode = parse_player_value(player_value)
    rows = build_rank_progress_rows(player, mode)
    if not rows:
        return html.Div(
            "Rank progress table unavailable right now.",
            style={
                "color": TEXT_DIM,
                "fontSize": "12px",
                "fontFamily": "monospace",
                "padding": "10px 4px",
            }
        )

    header_cells = [
        html.Th("Skill", style={"textAlign": "left", "padding": "8px 10px", "color": TEXT_DIM}),
        html.Th("Target", style={"textAlign": "left", "padding": "8px 10px", "color": TEXT_DIM}),
        html.Th("XP To Target", style={"textAlign": "right", "padding": "8px 10px", "color": TEXT_DIM}),
        html.Th("Est. Levels", style={"textAlign": "right", "padding": "8px 10px", "color": TEXT_DIM}),
    ]

    body_rows = []
    for row in rows:
        lvl_text = "—"
        if isinstance(row.get("levels_needed"), int):
            lvls = row["levels_needed"]
            lvl_text = f"{lvls}"
        body_rows.append(
            html.Tr([
                html.Td(row["skill"], style={"padding": "7px 10px"}),
                html.Td(row["target"], style={"padding": "7px 10px"}),
                html.Td(f"{int(row['xp_needed']):,}", style={"padding": "7px 10px", "textAlign": "right"}),
                html.Td(lvl_text, style={"padding": "7px 10px", "textAlign": "right"}),
            ], style={"borderTop": f"1px solid {BORDER}"})
        )

    return html.Div([
        html.Div(
            "Per-skill rank progress (live estimate)",
            style={"color": TEXT_DIM, "fontSize": "12px", "fontFamily": "monospace", "marginBottom": "8px"}
        ),
        html.Table([
            html.Thead(html.Tr(header_cells, style={"borderBottom": f"1px solid {BORDER}"})),
            html.Tbody(body_rows),
        ], style={"width": "100%", "borderCollapse": "collapse", "fontSize": "13px"}),
    ], style={
        "background": CARD_BG,
        "border": f"1px solid {BORDER}",
        "borderRadius": "8px",
        "padding": "12px 12px 10px 12px",
    })


@app.callback(
    Output("xp-trend-chart", "figure"),
    Output("rank-trend-chart", "figure"),
    Output("avg-xp-day-chart", "figure"),
    Output("xp-to-target-chart", "figure"),
    Input("player-dropdown", "value"),
    Input("skill-dropdown", "value"),
)
def update_trend_charts(player_value, skill):
    if not player_value or not skill:
        empty = go.Figure()
        _style_fig(empty, "")
        return empty, empty, empty, empty
    player, mode = parse_player_value(player_value)
    return (
        make_xp_trend(player, skill, mode),
        make_rank_trend(player, skill, mode),
        make_avg_daily_xp_trend(player, mode),
        make_xp_to_target_trend(player, skill, mode),
    )


@app.callback(
    Output("skills-overview-chart", "figure"),
    Output("xp-distribution-chart", "figure"),
    Input("player-dropdown", "value"),
)
def update_overview_charts(player_value):
    if not player_value:
        empty = go.Figure()
        _style_fig(empty, "")
        return empty, empty
    player, mode = parse_player_value(player_value)
    return make_skills_overview(player, mode), make_xp_distribution(player, mode)


@app.callback(
    Output("xp-compare-chart", "figure"),
    Output("xp-overall-compare-chart", "figure"),
    Input("compare-skill-dropdown", "value"),
)
def update_compare_chart(skill):
    selected_skill = skill if skill in SKILL_NAMES else "Overall"
    selected_players = get_anchor_group(selected_skill, DISPLAY_AHEAD_COUNT, DISPLAY_BEHIND_COUNT)
    overall_players = get_anchor_group("Overall", DISPLAY_AHEAD_COUNT, DISPLAY_BEHIND_COUNT)
    selected_players = filter_compare_outliers(selected_skill, selected_players)
    overall_players = filter_compare_outliers("Overall", overall_players)
    selected_players = filter_inactive_compare_players(selected_skill, selected_players)
    overall_players = filter_inactive_compare_players("Overall", overall_players)
    selected_skill_figure = make_multi_player_xp_trend(selected_skill, selected_players)
    overall_figure = make_multi_player_xp_trend("Overall", overall_players)
    return selected_skill_figure, overall_figure


if __name__ == "__main__":
    print("Starting OSRS Hiscore Dashboard...")
    host = "0.0.0.0"
    port = int(os.getenv("PORT", "8050"))
    print(f"Listening on http://{host}:{port}")
    app.run(host=host, port=port, debug=False)
