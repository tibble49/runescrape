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
   db.py
   collector.py
   dashboard.py
   README.md
   requirements.txt
   scripts/migrate_sqlite_to_postgres.py
   seed/osrs_hiscores_seed.sqlite3
```

---

## Usage

### Step 1 — Collect your first snapshot

```
python collector.py
```

This fetches tibble49's current stats and saves them to your active database:

- PostgreSQL if `DATABASE_URL` is set
- otherwise local SQLite (`osrs_hiscores.db`)

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
- **Skills overview** — bar chart of all current skill levels (including Sailing)
- **XP distribution** — pie chart of XP spread across your top 12 skills
- **Multi-player support** — use the Player dropdown to switch between tracked players

---

## Files created

| File | Description |
|------|-------------|
| `collector.py` | Fetches and stores snapshots |
| `dashboard.py` | Runs the local web dashboard |
| `db.py` | Shared database schema/engine (Postgres + SQLite fallback) |
| `scripts/migrate_sqlite_to_postgres.py` | One-time import from local SQLite to Postgres |
| `osrs_hiscores.db` | SQLite database (created automatically on first run) |
| `seed/osrs_hiscores_seed.sqlite3` | Seed snapshot used for first-run cloud initialization |

---

## Railway initial seed data

Yes — this project supports seeding initial dashboard data from your local SQLite snapshot.

- On first run, if the active DB path does not exist, both scripts copy from:
   `seed/osrs_hiscores_seed.sqlite3`
- DB path is configurable via env var:
   `OSRS_DB_PATH` (default: `osrs_hiscores.db`)

This gives Railway an initial dataset immediately after deploy.

---

## PostgreSQL migration (recommended for Railway)

Use Railway Postgres so both `web` and `collector` services share one live database.

### 1) Add Railway Postgres and set `DATABASE_URL`

Set `DATABASE_URL` on **both** services (`web` and `collector`) to the same Railway Postgres connection string.

### 2) One-time import from local SQLite

Run once (locally):

```
python scripts/migrate_sqlite_to_postgres.py
```

This copies all rows from your local SQLite DB into PostgreSQL.

---

## Railway + GitHub auto-deploy

This repo includes a GitHub Actions workflow at `.github/workflows/railway-deploy.yml`.
It deploys to Railway automatically on every push to `main`.

### 1) Add GitHub repository secrets

In GitHub: **Settings → Secrets and variables → Actions → New repository secret**

Add:

- `RAILWAY_TOKEN`
- `RAILWAY_PROJECT_ID`
- `RAILWAY_ENVIRONMENT_ID`
- `RAILWAY_SERVICE_ID`
- `RAILWAY_COLLECTOR_SERVICE_ID` (optional, enables auto-deploy of collector service too)

You can copy IDs from Railway project/service settings or via Railway CLI.

### 2) Railway web service start command

`railway.json` is configured with:

```json
{
   "deploy": {
      "startCommand": "python dashboard.py"
   }
}
```

`dashboard.py` is already configured to bind to `0.0.0.0` and use Railway's `PORT` env var.

---

## Railway cron job (run collector.py 3 times/day)

Create a second Railway service for the collector job (same repo), then set:

- **Start Command**: `python collector.py`
- **Schedule/Cron**: `0 8,12,20 * * *`

This runs at 8:00 AM, 12:00 PM, and 8:00 PM.

> Railway cron uses the environment timezone (commonly UTC unless configured otherwise).

> For production reliability, use the shared Postgres setup above.
