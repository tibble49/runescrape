import os
from functools import lru_cache

from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine
from sqlalchemy.engine import Engine

SQLITE_DB_PATH = os.getenv("OSRS_DB_PATH", "osrs_hiscores.db")

metadata = MetaData()

snapshots_table = Table(
    "snapshots",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("player", String, nullable=False),
    Column("mode", String, nullable=False, server_default="regular"),
    Column("timestamp", String, nullable=False),
    Column("date", String, nullable=False),
)

skill_data_table = Table(
    "skill_data",
    metadata,
    Column("snapshot_id", Integer, nullable=False),
    Column("skill", String, nullable=False),
    Column("rank", Integer),
    Column("level", Integer),
    Column("xp", Integer),
)

minigame_data_table = Table(
    "minigame_data",
    metadata,
    Column("snapshot_id", Integer, nullable=False),
    Column("activity", String, nullable=False),
    Column("rank", Integer),
    Column("score", Integer),
)


def get_database_url() -> str:
    return os.getenv("DATABASE_URL", "").strip()


def is_postgres_url(database_url: str) -> bool:
    return database_url.startswith("postgresql://") or database_url.startswith("postgres://")


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    database_url = get_database_url()
    if is_postgres_url(database_url):
        return create_engine(database_url, future=True, pool_pre_ping=True)

    sqlite_url = f"sqlite:///{SQLITE_DB_PATH.replace('\\', '/')}"
    return create_engine(sqlite_url, future=True)


def init_db(engine: Engine) -> None:
    metadata.create_all(engine)
