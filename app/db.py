from pathlib import Path
from typing import Iterable

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from .config import DATABASE_URL


MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"


def sqlalchemy_database_url() -> str:
    if DATABASE_URL.startswith("postgresql://"):
        return DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)
    return DATABASE_URL


engine = create_engine(sqlalchemy_database_url(), pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def get_connection():
    return psycopg.connect(DATABASE_URL)


def ensure_database_exists() -> None:
    params = conninfo_to_dict(DATABASE_URL)
    database_name = params.get("dbname")
    if not database_name:
        return

    admin_params = dict(params)
    admin_params["dbname"] = "postgres"
    admin_conninfo = make_conninfo(**admin_params)

    with psycopg.connect(admin_conninfo, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (database_name,))
            if cur.fetchone():
                return
            cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))


def iter_migration_files() -> Iterable[Path]:
    if not MIGRATIONS_DIR.exists():
        return []
    return sorted(MIGRATIONS_DIR.glob("*.sql"))


def run_migrations() -> None:
    ensure_database_exists()
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version TEXT PRIMARY KEY,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )

            cur.execute("SELECT version FROM schema_migrations")
            applied_versions = {row[0] for row in cur.fetchall()}

            for migration_file in iter_migration_files():
                version = migration_file.stem
                if version in applied_versions:
                    continue

                sql = migration_file.read_text(encoding="utf-8")
                cur.execute(sql)
                cur.execute(
                    "INSERT INTO schema_migrations (version) VALUES (%s)",
                    (version,),
                )

        conn.commit()
