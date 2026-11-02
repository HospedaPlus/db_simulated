"""F6 local events CSV: validation (always) and loading into Neon (needs TEST_DATABASE_URL)."""

from datetime import date, timedelta

import psycopg
import pytest

from seed.events import DEFAULT_EVENTS_CSV, load_events, read_events

from .conftest import FIXED_START, load_fresh


def test_shipped_csv_is_valid_and_has_high_events():
    rows = read_events(DEFAULT_EVENTS_CSV)
    assert len(rows) >= 20
    assert any(r[4] == "high" for r in rows)


def test_bad_rows_are_reported(tmp_path):
    bad = tmp_path / "events.csv"
    bad.write_text("hotel_code,name,start_date,end_date,category\n"
                   "HP-XX-999,Evento,2026-12-01,2026-11-30,huge\n", encoding="utf-8")
    with pytest.raises(ValueError) as exc:
        read_events(bad)
    assert "unknown hotel" in str(exc.value)
    assert "category" in str(exc.value)
    assert "end_date before start_date" in str(exc.value)


@pytest.fixture(scope="module")
def seeded(db_url):
    return load_fresh(db_url, FIXED_START)


def test_high_event_forces_limit_zero(seeded, db_url, tmp_path):
    start = FIXED_START + timedelta(days=10)
    csv = tmp_path / "events.csv"
    csv.write_text("hotel_code,name,start_date,end_date,category\n"
                   f"HP-CO-008,Evento alto,{start},{start + timedelta(days=2)},high\n",
                   encoding="utf-8")
    loaded, _ = load_events(db_url, csv)
    assert loaded == 1
    assert load_events(db_url, csv)[0] == 1  # replaces, never duplicates
    with psycopg.connect(db_url) as conn:
        n_events = conn.execute("SELECT count(*) FROM operation.local_events").fetchone()[0]
        limits = conn.execute(
            """SELECT max(i.overbooking_limit - greatest(0, i.sold_rooms - i.total_rooms))
                 FROM operation.inventory i JOIN operation.hotels h ON h.id = i.hotel_id
                WHERE h.code = 'HP-CO-008' AND i.inventory_date BETWEEN %s AND %s""",
            (start, start + timedelta(days=2))).fetchone()[0]
    assert n_events == 1
    assert limits == 0
