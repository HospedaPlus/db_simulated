"""End-to-end tests against a real PostgreSQL (TEST_DATABASE_URL)."""

from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest

from seed import checks, db
from seed.generator import generate


@pytest.fixture(scope="module")
def seeded(db_url):
    from datetime import date

    from seed.config import load_settings

    s = load_settings(database_url=db_url, mode="dev", seed=42, start_date=date(2026, 10, 14))
    db.drop_schema(db_url)
    db.init_schema(db_url)
    db.load(db_url, generate(s))
    return s


def test_all_checks_pass(seeded):
    results = checks.run_checks(seeded.database_url)
    failed = [r for r in results if not r.ok]
    assert not failed, failed


def test_schema_can_run_twice(seeded):
    db.init_schema(seeded.database_url)  # IF NOT EXISTS: no error, no data loss
    with psycopg.connect(seeded.database_url) as conn:
        assert conn.execute("SELECT count(*) FROM hotels").fetchone()[0] == 28


def test_seeding_twice_is_idempotent(seeded):
    before = checks.fingerprint(seeded.database_url)
    db.load(seeded.database_url, generate(seeded))
    assert checks.fingerprint(seeded.database_url) == before


def test_database_rejects_overbooking(seeded):
    with psycopg.connect(seeded.database_url) as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("UPDATE inventory SET available_rooms = -1 WHERE id = 1")


def test_database_rejects_bad_reservations(seeded):
    bad = [
        "check_out = check_in",          # zero nights
        "adults = 0",                    # nobody staying
        "status = 'lost'",               # unknown status
        "currency = 'cop'",              # lower-case ISO code
    ]
    with psycopg.connect(seeded.database_url) as conn:
        for change in bad:
            with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
                conn.execute(f"UPDATE reservations SET {change} WHERE id = 1")


def test_last_room_is_sold_once_under_concurrency(seeded):
    """50 concurrent buyers, 1 room left: exactly one sale (CA-08 with no overbooking).
    The single conditional UPDATE is the pattern the booking API should use."""
    url = seeded.database_url
    with psycopg.connect(url) as conn:
        inv_id = conn.execute("SELECT id FROM inventory ORDER BY id DESC LIMIT 1").fetchone()[0]
        conn.execute("UPDATE inventory SET available_rooms = 1 WHERE id = %s", (inv_id,))

    def buy(_):
        with psycopg.connect(url) as conn:
            cur = conn.execute(
                "UPDATE inventory SET available_rooms = available_rooms - 1 "
                "WHERE id = %s AND available_rooms >= 1", (inv_id,))
            return cur.rowcount

    with ThreadPoolExecutor(max_workers=50) as pool:
        sold = sum(pool.map(buy, range(50)))

    assert sold == 1
    with psycopg.connect(url) as conn:
        assert conn.execute("SELECT available_rooms FROM inventory WHERE id = %s",
                            (inv_id,)).fetchone()[0] == 0
    # Put the row back so other tests see consistent data.
    db.load(url, generate(seeded))


def test_cannot_book_a_night_that_already_passed(seeded):
    """A new booking (created_at = now) can't start yesterday; today is fine."""
    insert = """INSERT INTO reservations (code, hotel_id, room_type_id, channel_id, guest_id,
                    check_in, check_out, adults, currency, first_night_amount, total_amount)
                SELECT %s, h.id, 1, 1, 1, d, d + 1, 1, h.currency, 100000, 100000
                FROM hotels h,
                     LATERAL (SELECT (now() AT TIME ZONE h.timezone)::date + %s AS d) x
                WHERE h.code = 'HP-CO-001'"""
    with psycopg.connect(seeded.database_url) as conn:
        with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
            conn.execute(insert, ("TEST-YESTERDAY", -1))
        conn.execute(insert, ("TEST-TODAY", 0))  # same-day booking is allowed
        conn.rollback()
