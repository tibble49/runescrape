"""
dashboard.py — OSRS Hiscore Trend Dashboard
Run with: python dashboard.py
Then open http://127.0.0.1:8050 in your browser.

Requirements:
    pip install dash plotly pandas
"""

import os
import shutil
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from sqlalchemy import text
import dash
from dash import dcc, html, Input, Output, State, callback
from datetime import datetime

from db import get_engine, get_database_url, init_db, is_postgres_url

DB_FILE = os.getenv("OSRS_DB_PATH", "osrs_hiscores.db")
SEED_DB_FILE = os.getenv("OSRS_SEED_DB_PATH", "seed/osrs_hiscores_seed.sqlite3")

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
TRACK_AHEAD_COUNT = 10
TRACK_BEHIND_COUNT = 3
DISPLAY_AHEAD_COUNT = 3
DISPLAY_BEHIND_COUNT = 3

COMPARE_PLAYER_NAMES = [
    "XESPIS",
]

DEFAULT_PLAYER = "tibble49"


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


def get_first_last_dates(player: str, mode: str = "regular") -> tuple[str | None, str | None]:
    with get_conn() as conn:
        row = conn.execute(text(
            "SELECT MIN(date), MAX(date) FROM snapshots WHERE player = :player AND mode = :mode"
        ), {"player": player.lower(), "mode": mode}).fetchone()
    if not row:
        return None, None
    return row[0], row[1]


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

    skills_df = df[df["skill"].isin(SKILL_NAMES)].copy()
    skills_df["skill"] = pd.Categorical(skills_df["skill"], categories=SKILL_NAMES, ordered=True)
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

    for target_name in COMPARE_PLAYER_NAMES:
        target = target_name.lower()
        match = next(
            (
                p for p in players
                if p["label"].lower() == target
                or p["player"].lower() == target
                or p["label"].lower().startswith(f"{target} (")
            ),
            None,
        )
        if match and match["value"] not in used_values:
            used_values.add(match["value"])
            resolved.append({"name": target_name, "value": match["value"]})

    return resolved


def get_latest_overall_ranks() -> list[dict]:
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
                WHERE sd.skill = 'Overall' AND sd.rank IS NOT NULL
            """), conn)

        if df.empty:
            return []

        df["player"] = df["player"].astype(str).str.strip()
        df["mode"] = df["mode"].fillna("regular").astype(str).str.strip().str.lower()
        df["rank"] = pd.to_numeric(df["rank"], errors="coerce")
        df = df.dropna(subset=["rank"])
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


def get_anchor_groups() -> tuple[list[dict], list[dict]]:
    """
    Returns:
      tracked_players: 10 ahead + anchor + 3 behind
      overall_display_players: 3 ahead + anchor + 3 behind
    """
    rows = get_latest_overall_ranks()
    if not rows:
        fallback = get_fixed_compare_players()
        return fallback, fallback

    anchor_candidates = [row for row in rows if str(row["player"]).lower() == ANCHOR_PLAYER.lower()]
    if not anchor_candidates:
        fallback = get_fixed_compare_players()
        return fallback, fallback

    anchor = next((row for row in anchor_candidates if row["mode"] == "regular"), anchor_candidates[0])
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

    tracked_rows = sorted(
        ahead[:TRACK_AHEAD_COUNT] + [anchor] + behind[:TRACK_BEHIND_COUNT],
        key=lambda row: int(row["rank"]),
    )
    display_rows = sorted(
        ahead[:DISPLAY_AHEAD_COUNT] + [anchor] + behind[:DISPLAY_BEHIND_COUNT],
        key=lambda row: int(row["rank"]),
    )

    tracked_players = _to_compare_payload(tracked_rows)
    overall_display_players = _to_compare_payload(display_rows)

    if not tracked_players:
        fallback = get_fixed_compare_players()
        return fallback, fallback

    return tracked_players, overall_display_players


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


def stat_card(label: str, value: str, delta: str = "", delta_positive: bool = True):
    delta_color = GREEN if delta_positive else RED
    return html.Div([
        html.Div(label, style={"color": TEXT_DIM, "fontSize": "11px",
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
        ], style={"display": "flex", "alignItems": "center"}),
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
        ], style={"display": "flex", "alignItems": "center"})
    ], style={
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
                style={"width": "260px", "fontFamily": "Georgia, serif"},
            )
        ]),
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
                style={"width": "200px", "fontFamily": "Georgia, serif"},
            )
        ]),
    ], style={
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

    # Charts grid
    html.Div([
        # Top row: XP trend + Rank trend
        html.Div([
            dcc.Graph(id="xp-trend-chart",
                      config={"displayModeBar": False},
                      style={"flex": "1", "minWidth": "0", "height": "360px"}),
            dcc.Graph(id="rank-trend-chart",
                      config={"displayModeBar": False},
                      style={"flex": "1", "minWidth": "0", "height": "360px"}),
        ], style={"display": "flex", "gap": "16px", "marginBottom": "16px"}),

        # Bottom row: skills bar + pie
        html.Div([
            dcc.Graph(id="skills-overview-chart",
                      config={"displayModeBar": False},
                      style={"flex": "2", "minWidth": "0", "height": "360px"}),
            dcc.Graph(id="xp-distribution-chart",
                      config={"displayModeBar": False},
                      style={"flex": "1", "minWidth": "0", "height": "360px"}),
        ], style={"display": "flex", "gap": "16px"}),

    ], style={"padding": "0 32px 32px 32px"}),

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
            ], style={"display": "flex", "alignItems": "center"}),
            dcc.Link("← Back to Dashboard", href="/", style={
                "color": ACCENT,
                "fontSize": "12px",
                "fontFamily": "monospace",
                "textDecoration": "none"
            })
        ], style={
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
                    style={"width": "220px", "fontFamily": "Georgia, serif"},
                )
            ]),
            html.Div(
                "Tracking: 10 ahead + XESPIS + 3 behind. Overall display: 3 ahead + XESPIS + 3 behind.",
                style={"color": TEXT_DIM, "fontSize": "12px", "fontFamily": "monospace", "maxWidth": "640px"}
            ),
        ], style={
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

    cards = [
        stat_card("Total Level",    f"{total_level:,}" if isinstance(total_level, int) else total_level),
        stat_card("Total XP",       f"{total_xp:,}"    if isinstance(total_xp, int)    else total_xp),
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
    Output("xp-trend-chart", "figure"),
    Output("rank-trend-chart", "figure"),
    Input("player-dropdown", "value"),
    Input("skill-dropdown", "value"),
)
def update_trend_charts(player_value, skill):
    if not player_value or not skill:
        empty = go.Figure()
        _style_fig(empty, "")
        return empty, empty
    player, mode = parse_player_value(player_value)
    return make_xp_trend(player, skill, mode), make_rank_trend(player, skill, mode)


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
    tracked_players, overall_display_players = get_anchor_groups()
    selected_players = overall_display_players if skill == "Overall" else tracked_players
    selected_skill_figure = make_multi_player_xp_trend(skill, selected_players)
    overall_figure = make_multi_player_xp_trend("Overall", overall_display_players)
    return selected_skill_figure, overall_figure


if __name__ == "__main__":
    print("Starting OSRS Hiscore Dashboard...")
    host = "0.0.0.0"
    port = int(os.getenv("PORT", "8050"))
    print(f"Listening on http://{host}:{port}")
    app.run(host=host, port=port, debug=False)
