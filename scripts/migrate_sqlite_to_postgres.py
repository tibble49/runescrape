import os
import sys
import sqlite3
from pathlib import Path

from sqlalchemy import delete, insert, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from db import (
    get_database_url,
    get_engine,
    init_db,
    is_postgres_url,
    minigame_data_table,
    quest_summary_table,
    skill_data_table,
    snapshots_table,
)


def fetch_rows(conn: sqlite3.Connection, query: str) -> list[dict]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(query).fetchall()
    return [dict(row) for row in rows]


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def main() -> None:
    sqlite_path = os.getenv("SQLITE_PATH", os.getenv("OSRS_DB_PATH", "osrs_hiscores.db"))
    database_url = get_database_url()

    if not is_postgres_url(database_url):
        raise SystemExit("DATABASE_URL must point to PostgreSQL for this migration")

    if not os.path.exists(sqlite_path):
        raise SystemExit(f"SQLite file not found: {sqlite_path}")

    sqlite_conn = sqlite3.connect(sqlite_path)
    snapshots = fetch_rows(sqlite_conn, "SELECT id, player, mode, timestamp, date FROM snapshots")
    skills = fetch_rows(sqlite_conn, "SELECT snapshot_id, skill, rank, level, xp FROM skill_data")
    minigames = fetch_rows(sqlite_conn, "SELECT snapshot_id, activity, rank, score FROM minigame_data")
    quests = (
        fetch_rows(
            sqlite_conn,
            "SELECT snapshot_id, completed, in_progress, not_started, source FROM quest_summary",
        )
        if table_exists(sqlite_conn, "quest_summary")
        else []
    )
    sqlite_conn.close()

    engine = get_engine()
    init_db(engine)

    with engine.begin() as conn:
        conn.execute(delete(minigame_data_table))
        conn.execute(delete(quest_summary_table))
        conn.execute(delete(skill_data_table))
        conn.execute(delete(snapshots_table))

        if snapshots:
            conn.execute(insert(snapshots_table), snapshots)
        if skills:
            conn.execute(insert(skill_data_table), skills)
        if minigames:
            conn.execute(insert(minigame_data_table), minigames)
        if quests:
            conn.execute(insert(quest_summary_table), quests)

        conn.execute(text("""
            SELECT setval(
                pg_get_serial_sequence('snapshots', 'id'),
                COALESCE((SELECT MAX(id) FROM snapshots), 1),
                true
            )
        """))

    print(
        f"Migrated {len(snapshots)} snapshots, {len(skills)} skill rows, "
        f"{len(minigames)} minigame rows, and {len(quests)} quest summary rows "
        f"from {sqlite_path} to PostgreSQL."
    )


if __name__ == "__main__":
    main()
