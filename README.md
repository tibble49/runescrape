# OSRS Hiscore Dashboard

Track your Old School RuneScape skill levels, XP, and ranks over time with a local dashboard.

---

## Setup

### 1. Install dependencies

```
pip install -r requirements.txt
```

### 2. Put all three files in the same folder

```
📁 osrs-dashboard/
   collector.py
   dashboard.py
   README.md
   requirements.txt
```

---

## Usage

### Step 1 — Collect your first snapshot

```
python collector.py
```

This fetches tibble49's current stats and saves them to a local database file (`osrs_hiscores.db`).

To track a different or additional player:
```
python collector.py --player zezima
python collector.py --player tibble49 zezima woox
```

### Step 2 — Open the dashboard

```
python dashboard.py
```

Then open **http://127.0.0.1:8050** in your browser.

---

## Building trend history (important!)

The trend line charts need multiple snapshots taken on different days.
Run the collector once a day to build up history:

```
python collector.py
```

### Automate with Windows Task Scheduler

1. Open **Task Scheduler** (search for it in the Start menu)
2. Click **Create Basic Task**
3. Name it something like `OSRS Hiscore Collector`
4. Set the trigger to **Daily**
5. Set the action to **Start a Program**
6. Program: `python`
7. Arguments: `C:\path\to\your\folder\collector.py`
8. Start in: `C:\path\to\your\folder\`
9. Click Finish

After a week or two you'll have enough data for meaningful trend lines.

---

## Dashboard features

- **Stat cards** — current level, XP, rank, and XP gained since tracking started
- **XP trend chart** — XP over time with daily XP-gained bars
- **Rank trend chart** — rank over time (lower = better, y-axis inverted)
- **Skills overview** — bar chart of all current skill levels
- **XP distribution** — pie chart of XP spread across your top 12 skills
- **Multi-player support** — use the Player dropdown to switch between tracked players

---

## Files created

| File | Description |
|------|-------------|
| `collector.py` | Fetches and stores snapshots |
| `dashboard.py` | Runs the local web dashboard |
| `osrs_hiscores.db` | SQLite database (created automatically on first run) |
