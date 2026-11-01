"""Database helpers: connect, run the SQL files, bulk-load rows."""

from __future__ import annotations

from pathlib import Path

import psycopg

from .config import SQL_DIR
from .generator import TABLE_COLUMNS, TABLE_ORDER, Dataset


SCHEMA = "operation"  # F3 tables live here; F2's ledger lives in "streaming"


def connect(url: str, autocommit: bool = False) -> psycopg.Connection:
    """Open a connection. Short timeout so a wrong host fails fast.

    Without autocommit the connection starts a transaction pointed at the
    `operation` schema (SET LOCAL: it also works behind Neon's pooler), so
    the queries below can say `hotels` instead of `operation.hotels`."""
    conn = psycopg.connect(url, autocommit=autocommit, connect_timeout=10)
    if not autocommit:
        conn.execute(f"SET LOCAL search_path TO {SCHEMA}")
    return conn


def run_sql_file(url: str, path: Path) -> None:
    # The files manage their own BEGIN/COMMIT, so run them in autocommit.
    with connect(url, autocommit=True) as conn:
        conn.execute(path.read_text(encoding="utf-8"))


def init_schema(url: str) -> None:
    """Create the schema, tables, indexes and functions. Never drops anything;
    functions are CREATE OR REPLACE, so running it again updates them."""
    run_sql_file(url, SQL_DIR / "schema.sql")
    run_sql_file(url, SQL_DIR / "indexes.sql")
    run_sql_file(url, SQL_DIR / "functions.sql")


def drop_schema(url: str) -> None:
    run_sql_file(url, SQL_DIR / "reset.sql")


def load(url: str, ds: Dataset) -> dict:
    """Replace all F3 data with the given dataset in a single transaction.

    TRUNCATE ... RESTART IDENTITY makes ids start at 1 again, which is what
    makes every run produce the same ids. If anything fails half way, the
    transaction rolls back and the previous data stays as it was.
    """
    # Rejections and relocations are written by the running system; they point
    # at ids that are about to be reused, so they go too. local_events and
    # cancellation_rate_by_lead (F6/Spark, keyed by hotel code) are kept.
    tables = [TABLE_COLUMNS[name][0] for name in TABLE_ORDER] + ["reservation_rejections", "relocations"]
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
