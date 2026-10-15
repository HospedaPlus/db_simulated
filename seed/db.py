"""Database helpers: connect, run the SQL files, bulk-load rows."""

from __future__ import annotations

from pathlib import Path

import psycopg

from .config import SQL_DIR
from .generator import TABLE_COLUMNS, TABLE_ORDER, Dataset


def connect(url: str, autocommit: bool = False) -> psycopg.Connection:
    # Short timeout so a wrong host fails fast instead of hanging.
    return psycopg.connect(url, autocommit=autocommit, connect_timeout=10)


def run_sql_file(url: str, path: Path) -> None:
    # The files manage their own BEGIN/COMMIT, so run them in autocommit.
    with connect(url, autocommit=True) as conn:
        conn.execute(path.read_text(encoding="utf-8"))


def init_schema(url: str) -> None:
    """Create tables and indexes. Never drops anything."""
    run_sql_file(url, SQL_DIR / "schema.sql")
    run_sql_file(url, SQL_DIR / "indexes.sql")


def drop_schema(url: str) -> None:
    run_sql_file(url, SQL_DIR / "reset.sql")


def load(url: str, ds: Dataset) -> dict:
    """Replace all F3 data with the given dataset in a single transaction.

    TRUNCATE ... RESTART IDENTITY makes ids start at 1 again, which is what
    makes every run produce the same ids. If anything fails half way, the
    transaction rolls back and the previous data stays as it was.
    """
    tables = [TABLE_COLUMNS[name][0] for name in TABLE_ORDER]
    with connect(url) as conn:
        with conn.cursor() as cur:
            cur.execute(f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE")
            for name in TABLE_ORDER:
                table, columns = TABLE_COLUMNS[name]
                rows = getattr(ds, name)
                with cur.copy(f"COPY {table} ({', '.join(columns)}) FROM STDIN") as copy:
                    for row in rows:
                        copy.write_row(row)
                # Sanity check: ids must line up with list positions.
                cur.execute(f"SELECT count(*), coalesce(max(id), 0) FROM {table}")
                count, max_id = cur.fetchone()
                if count != len(rows) or max_id != len(rows):
                    raise RuntimeError(f"{table}: expected {len(rows)} rows with ids 1..N, "
                                       f"got {count} rows, max id {max_id}")
        conn.commit()
    return ds.counts()
