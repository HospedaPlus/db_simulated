"""F6 local events: load the team's CSV into operation.local_events.

    python -m seed events                       # data/local_events.csv
    SEED_EVENTS_CSV=/path/file.csv python -m seed events

The CSV is reference data the team maintains (statement: F6 local events), one row
per hotel and event: hotel_code,name,start_date,end_date,category (low|medium|high).
A 'high' event forces the overbooking limit of that hotel to 0 on those nights (CA-02),
so after loading we call refresh_overbooking_limits().

The whole table is replaced in one transaction, so running it twice gives the same
result. The same file is also the raw F6b copy the pipeline uploads to S3 Bronze.
"""

from __future__ import annotations

import csv
import os
from datetime import date
from pathlib import Path

import psycopg

from .catalog import HOTELS
from .config import ROOT

DEFAULT_EVENTS_CSV = ROOT / "data" / "local_events.csv"
COLUMNS = ["hotel_code", "name", "start_date", "end_date", "category"]
CATEGORIES = {"low", "medium", "high"}


def read_events(path: Path) -> list[tuple]:
    """Read and validate the CSV. Raises ValueError listing every bad row."""
    hotel_codes = {h.code for h in HOTELS}
    rows, errors = [], []
    with path.open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames != COLUMNS:
            raise ValueError(f"{path.name}: header must be {','.join(COLUMNS)}, got {reader.fieldnames}")
        for n, r in enumerate(reader, start=2):
            try:
                start, end = date.fromisoformat(r["start_date"]), date.fromisoformat(r["end_date"])
            except ValueError:
                errors.append(f"row {n}: bad date {r['start_date']!r}/{r['end_date']!r}")
                continue
            if r["hotel_code"] not in hotel_codes:
                errors.append(f"row {n}: unknown hotel {r['hotel_code']!r}")
            if r["category"] not in CATEGORIES:
                errors.append(f"row {n}: category must be low, medium or high")
            if end < start:
                errors.append(f"row {n}: end_date before start_date")
            if not r["name"].strip() or len(r["name"]) > 120:
                errors.append(f"row {n}: name must have 1-120 characters")
            rows.append((r["hotel_code"], r["name"].strip(), start, end, r["category"]))
    if errors:
        raise ValueError(f"{path.name}:\n  " + "\n  ".join(errors))
    return rows


def load_events(url: str, path: Path | None = None) -> tuple[int, int]:
    """Replace operation.local_events with the CSV and refresh the overbooking limits.
    Returns (events loaded, inventory rows whose limit changed)."""
    path = path or Path(os.getenv("SEED_EVENTS_CSV", DEFAULT_EVENTS_CSV))
    rows = read_events(path)
    with psycopg.connect(url) as conn, conn.cursor() as cur:
        cur.execute("SET search_path = operation, pg_temp")
        cur.execute("DELETE FROM local_events")
        cur.executemany(
            "INSERT INTO local_events (hotel_code, name, start_date, end_date, category) "
            "VALUES (%s, %s, %s, %s, %s)", rows)
        cur.execute("SELECT refresh_overbooking_limits()")
        changed = cur.fetchone()[0]
    return len(rows), changed
