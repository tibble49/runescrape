import argparse
import sys
from pathlib import Path

from sqlalchemy import func, inspect, select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from db import (
    get_engine,
    minigame_data_table,
    quest_summary_table,
    skill_data_table,
    snapshots_table,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Check whether data exists for a player.")
    parser.add_argument("username")
    args = parser.parse_args()

    player = args.username.strip().lower()
    s = snapshots_table

    with get_engine().connect() as conn:
        modes = conn.execute(
            select(
                s.c.mode,
                func.count(s.c.id),
                func.min(s.c.timestamp),
                func.max(s.c.timestamp),
            )
            .where(func.lower(s.c.player) == player)
            .group_by(s.c.mode)
            .order_by(s.c.mode)
        ).fetchall()

        if not modes:
            print(f"No data found for '{args.username}'.")
            raise SystemExit(1)

        snapshot_ids = select(s.c.id).where(func.lower(s.c.player) == player).scalar_subquery()
        existing_tables = set(inspect(conn).get_table_names())
        counts = {
            name: conn.execute(
                select(func.count()).select_from(table).where(table.c.snapshot_id.in_(snapshot_ids))
            ).scalar_one()
            for name, table in (
                ("skill rows", skill_data_table),
                ("minigame rows", minigame_data_table),
                ("quest summaries", quest_summary_table),
            )
            if table.name in existing_tables
        }

    print(f"Data found for '{args.username}':")
    for mode, count, first, last in modes:
        print(f"  {mode}: {count} snapshots ({first} -> {last})")
    for name, count in counts.items():
        print(f"  {name}: {count}")


if __name__ == "__main__":
    main()
