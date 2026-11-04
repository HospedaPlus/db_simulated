"""End-to-end tests against a real PostgreSQL (TEST_DATABASE_URL)."""

import psycopg
import pytest

from seed import checks, db
from seed.generator import generate


from .conftest import FIXED_START, load_fresh


@pytest.fixture(scope="module")
def seeded(db_url):
    return load_fresh(db_url, FIXED_START)


def test_all_checks_pass(seeded):
    results = checks.run_checks(seeded.database_url)
    failed = [r for r in results if not r.ok]
    assert not failed, failed


def test_schema_can_run_twice(seeded):
    db.init_schema(seeded.database_url)  # IF NOT EXISTS: no error, no data loss
    with psycopg.connect(seeded.database_url) as conn:
        assert conn.execute("SELECT count(*) FROM operation.hotels").fetchone()[0] == 28


def test_seeding_twice_is_idempotent(seeded):
    before = checks.fingerprint(seeded.database_url)
    db.load(seeded.database_url, generate(seeded))
    assert checks.fingerprint(seeded.database_url) == before


def test_database_rejects_overbooking(seeded):
    """Safety net under reservar(): never more sold than rooms + allowed overbooking."""
    with psycopg.connect(seeded.database_url) as conn:
        with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
            conn.execute("UPDATE operation.inventory "
                         "SET sold_rooms = total_rooms + overbooking_limit + 1 WHERE id = 1")
        with pytest.raises(psycopg.errors.GeneratedAlways), conn.transaction():
            conn.execute("UPDATE operation.inventory SET available_rooms = 5 WHERE id = 1")


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
                conn.execute(f"UPDATE operation.reservations SET {change} WHERE id = 1")


def test_cannot_book_a_night_that_already_passed(seeded):
    """A new booking (created_at = now) can't start yesterday; today is fine."""
    insert = """INSERT INTO operation.reservations (code, source, external_code, hotel_id, room_type_id,
                    channel_id, guest_id, check_in, check_out, adults, currency, first_night_amount,
                    total_amount)
                SELECT %s, 'F3', %s, h.id, 1, 1, 1, d, d + 1, 1, h.currency, 100000, 100000
                FROM operation.hotels h,
                     LATERAL (SELECT (now() AT TIME ZONE h.timezone)::date + %s AS d) x
                WHERE h.code = 'HP-CO-001'"""
    with psycopg.connect(seeded.database_url) as conn:
        with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
            conn.execute(insert, ("TEST-YESTERDAY", "TEST-YESTERDAY", -1))
        conn.execute(insert, ("TEST-TODAY", "TEST-TODAY", 0))  # same-day booking is allowed
        conn.rollback()


def test_updated_at_moves_on_every_change(seeded):
    with psycopg.connect(seeded.database_url) as conn:
        rid, before = conn.execute(
            "SELECT id, updated_at FROM operation.reservations WHERE status = 'confirmed' LIMIT 1").fetchone()
        conn.execute("UPDATE operation.reservations SET status = 'no_show' WHERE id = %s", (rid,))
        after = conn.execute("SELECT updated_at FROM operation.reservations WHERE id = %s", (rid,)).fetchone()[0]
        conn.rollback()
    assert after > before
