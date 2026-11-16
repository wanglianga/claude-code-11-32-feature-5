"""PostgreSQL connection management and schema bootstrap."""
from __future__ import annotations

from pathlib import Path

from psycopg_pool import ConnectionPool
from psycopg.rows import dict_row

from .config import Settings

SCHEMA_FILE = Path(__file__).with_name("schema.sql")


class Database:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.schema = settings.schema
        self.pool = ConnectionPool(
            conninfo=settings.database_url,
            min_size=1,
            max_size=10,
            kwargs={"row_factory": dict_row},
            open=False,
        )

    def open(self) -> None:
        self.pool.open()
        self.pool.wait(timeout=15)
        self.bootstrap()

    def close(self) -> None:
        self.pool.close()

    @property
    def q(self) -> str:
        return f'"{self.schema}"'

    def bootstrap(self) -> None:
        ddl = SCHEMA_FILE.read_text(encoding="utf-8")
        with self.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{self.schema}"')
                cur.execute(f'SET LOCAL search_path TO "{self.schema}"')
                cur.execute(ddl)
            conn.commit()

    def conn(self):
        return self.pool.connection()

    def scoped(self):
        """Return a connection whose search_path defaults to the service schema."""
        ctx = self.pool.connection()
        schema = self.schema

        class _Scoped:
            def __enter__(self):
                self.conn = ctx.__enter__()
                with self.conn.cursor() as cur:
                    cur.execute(f'SET search_path TO "{schema}"')
                return self.conn

            def __exit__(self, exc_type, exc, tb):
                return ctx.__exit__(exc_type, exc, tb)

        return _Scoped()
